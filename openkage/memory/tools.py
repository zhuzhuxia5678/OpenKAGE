"""反思/聚合 ReActAgent 的 10 个窄 CRUD 工具 + 框架级的 retrieve/expand 检索工具。

create_concept/update_concept/add_facet/update_facet/merge_concepts/
manage_edge/search_concepts/get_concept/del_facet/del_concept —— 反思 Agent 自己决定这次经验
该挂到哪个已有概念下、还是新建概念、连哪条边（manage_edge 统一负责边的增/删/改）。

这 10 个工具只触碰传入的 conn/vec_store/lock/round_num（经 _BoundTool 绑定），不认识
BrainGraph——所以能从图谱引擎里独立出来。BrainGraph.reflect/consolidate 起 ReActAgent 时调
_build_tools 挂上。

`RetrieveMemoryTool`/`ExpandMemoryTool` 是本模块另一半：它们认识 BrainGraph/ArmGraph
（走 `.retrieve_subgraph()`/`.expand_concept()`/`.render_subgraph()`），供**任意**域的任务型
ReAct Agent 复用（目前唯一的调用方是 `domains/minecraft/react_tools.py`，经它重新导出）。

★ **反思/聚合故意不用它们**（曾经改过、又改回来了）：`retrieve_subgraph`/`expand_concept`
底层的多跳扩展是"关系语义权重 × 落点强度 → softmax → 概率采样"，专为任务域 Agent 在不确定
该往哪走时**随机探索**设计的（同一个 query 两次检索允许带回不同邻居，见 graph.py `_sample_edges`
docstring）——这对反思/聚合"这个概念是不是已经记过了"这种是非判断题是负资产：查两次结果不一样，
且采样权重要读每个候选的 `strength`，而"这个概念历史上被任务域 Agent 用得多不多"跟"图谱里有没有
记过这件事"毫无关系。`search_concepts`/`get_concept` 才是给写知识的 Agent 用的：纯向量锚定、
不做阈值过滤、不碰也不受 strength 影响，结果稳定可复现。

## 提示词不在本文件里（2026-08-08）

这些工具的 description / 参数说明在各 Agent 的 yaml（`prompts/{reflect,aggregate}/*.yaml`），
而它们**返回给模型**的每一句文案在 `prompts/default.yaml`（框架层共用一份，取值走
`AgentPrompts.result`，本文件里的 `_TOOL_RESULT`）。本文件只留控制流、SQL 与校验判据。
判据同 08-07 那次外置：**代码契约 vs 措辞** —— JSON 的键名（`ok`/`error`/`note`/`warning`）
是契约，留在这儿；键**值**里给模型看的话是措辞，全在 yaml。
"""

from __future__ import annotations

import contextvars
import json
import logging
import sqlite3
import threading

from openkage.memory._common import (
    _BoundTool,
    _concept_relations,
    _concept_row_to_dict,
    _facet_kinds,
    _slugify,
    fuse_part_hits,
)
from openkage.memory.schema import LAYER_EXPERIENCE, LAYER_MEMORY
from openkage.memory.vector_store import part_biz_id
from openkage.memory.prompt_store import AGGREGATE, REFLECT, SKILL_MAINTENANCE

# mc_server 目录（_js_syntax_invalid 找 acorn 用：node_modules 是 index.js 的，Python 侧无 JS 解析器）
from pathlib import Path as _Path
_PKG_MC_SERVER = _Path(__file__).resolve().parents[2] / "domains" / "minecraft" / "mc_server"

# ── 检索结果的块边界标记（2026-09-13 全点去重）──────────────────────────────────
# retrieve / retrieve_skills 命中时自动包裹结果（Agent 主动调用与框架注入统一同一契约），
# 域侧（domains/minecraft/react_tools.py）从这里导入同一常量——注入块、_compact 压缩保留、
# 已见集合扫描（_collect_seen）三处共用一个出处，不存在两份漂移。
# ★ 对 LLM 无害：就是一行分隔符文本；对代码是可 grep 的边界契约（同 OBS_SEPARATOR 的思路）。
EXP_BLOCK_MARKER = "<EXP>"
SKILL_BLOCK_MARKER = "<SKILL>"

# ──────────────────────────────────────────────────────────────────────────────
# facet → 来源步 的旁路记录
#
# ★ **有意不写进 facet 表**：视频是观测产物，不该混进 Agent 的记忆结构 —— 加一列意味着
#   改既有 schema、旧图谱要读时容错、聚合到主脑时还得决定这列怎么传。而
#   `(concept_slug, kind)` 在 facet 表里本就唯一（`_facet_exists` 保证），足够当外部映射的键。
#   所以这里只把「哪条 facet 是从哪几个 JS 步看出来的」交给一个可插拔的 sink，
#   由域侧决定落到哪个文件；没装 sink 时整个机制是 no-op。
#
# ★★ 用 **contextvar 而不是普通全局**：多个副脑并发跑各自的反思（asyncio 任务），
#    普通全局只有一份 —— alice 的 facet 会被写进 bob 的映射文件，而且这种错是静默的。
#    contextvar 天然按任务隔离（与 openkage/logctx.py 用它区分日志作用域同一套路）。
#
# ★★★ sink 收的是**事件**（set / drop / move_slug），不是最终状态。因为 facet 会被改、
#    被删、概念会改 slug —— 而一条 facet 被推翻之后，旧的来源步指向的是**被推翻的那个说法**。
#    留着它，人点开视频会看到与正文矛盾的画面，而"记忆能纠错自愈"正是本实验要证的主张之一，
#    这条链路上不能有这种假证据。哪些操作要发什么事件，见下面几个调用点。
_facet_source_sink: contextvars.ContextVar = contextvars.ContextVar(
    "openkage_facet_source_sink", default=None)


def set_facet_source_sink(fn):
    """装一个 facet 来源事件的回调（None=卸载）。

    签名：`fn(concept_slug, kind, steps, *, op="set", new_slug="")`。

    返回 token，可传给 `reset_facet_source_sink` 精确还原（contextvar 的标准用法）。
    """
    return _facet_source_sink.set(fn)


def reset_facet_source_sink(token) -> None:
    try:
        _facet_source_sink.reset(token)
    except (ValueError, LookupError):
        _facet_source_sink.set(None)   # 跨任务 reset 会失败，退化成置空


def _record_facet_source(concept_slug: str, kind: str, steps) -> None:
    """记「这条 facet 是从这几步看出来的」。**任何异常都吞掉** —— 记录视频索引失败绝不能
    影响记忆写入。

    `steps` 清洗后为空就**什么都不发**：留一条空 steps 会让前端显示"看 0 步"的死入口。
    ⚠️ 于是「模型这次没填来源步」与「它明确说了没有来源」不可区分 —— 这是有意的：
    update_facet 改内容却不填来源步时，旧映射保留比清掉更有用（内容多是补充而非推翻，
    而清掉就永远拿不回来了）。真要推翻，模型有 del_facet。
    """
    sink = _facet_source_sink.get()
    if sink is None or not steps:
        return
    try:
        clean = []
        for s in steps if isinstance(steps, (list, tuple)) else []:
            try:
                clean.append(int(s))
            except (TypeError, ValueError):
                continue  # 模型可能给出 "第12步" 这类字符串，取不出数字就跳过
        if clean:
            sink(concept_slug, kind, clean)
    except Exception:  # pylint: disable=broad-except
        logger.debug("[memory] 记录 facet 来源步失败（已忽略）", exc_info=False)


def _drop_facet_source(concept_slug: str, kind: str = "") -> None:
    """让某条 facet（`kind` 为空 = 该概念**所有** facet）的来源映射失效。

    del_facet / del_concept 之后调：那条 facet 已经不在图谱里了，它的录像片段是**假证据**。
    """
    sink = _facet_source_sink.get()
    if sink is None:
        return
    try:
        sink(concept_slug, kind, [], op="drop")
    except Exception:  # pylint: disable=broad-except
        logger.debug("[memory] 失效 facet 来源步失败（已忽略）", exc_info=False)


def _move_facet_source(old_slug: str, new_slug: str) -> None:
    """概念改了 slug（或被并进另一个概念）→ 它名下**所有** facet 的映射键要跟着搬。

    不搬的后果是片段凭空丢失：facet 行已经跟着迁到新 slug 了（`update_concept` 的
    `UPDATE facet SET concept_slug`、`merge_concepts` 的 `_merge_body`），而映射还挂在旧名字上，
    前端按新 slug 查不到 —— 表现为"改个名字，录像入口全没了"。
    """
    sink = _facet_source_sink.get()
    if sink is None or not new_slug or new_slug == old_slug:
        return
    try:
        sink(old_slug, "", [], op="move_slug", new_slug=new_slug)
    except Exception:  # pylint: disable=broad-except
        logger.debug("[memory] 迁移 facet 来源步失败（已忽略）", exc_info=False)


# 技能来源步的旁路落盘（与上面的 facet 那套同构，见 domains/minecraft/footage.py 的
# `mark_skill_source`）。独立一个 contextvar：sink 签名与 facet 不同——skill 无 kind、
# slug 不可变，只有 `set`/`drop`，故签名是 `fn(slug, steps, *, op="set")`。
# ★ `update_skill` **不碰**来源步（用户定案）：来源步只在 create_skill 时落一次、之后不可变，
#   见 CreateSkillTool / mark_skill_source 的注释。
_skill_source_sink: contextvars.ContextVar = contextvars.ContextVar(
    "openkage_skill_source_sink", default=None)


def set_skill_source_sink(fn):
    """装一个 skill 来源事件的回调（None=卸载）。

    签名：`fn(slug, steps, *, op="set")`。返回 token，可传给 `reset_skill_source_sink`。
    """
    return _skill_source_sink.set(fn)


def reset_skill_source_sink(token) -> None:
    try:
        _skill_source_sink.reset(token)
    except (ValueError, LookupError):
        _skill_source_sink.set(None)   # 跨任务 reset 会失败，退化成置空


def _record_skill_source(slug: str, steps) -> None:
    """记「这个技能是从这几步产出的」。**任何异常都吞掉**（同 `_record_facet_source`）。

    `steps` 清洗后为空就什么都不发——「模型没填来源步」与「明确没有来源」不可区分是有意的，
    与 facet 同理。skill 只在 create_skill 调本函数；update_skill 不碰来源步（用户定案）。
    """
    sink = _skill_source_sink.get()
    if sink is None or not steps:
        return
    try:
        clean = []
        for s in steps if isinstance(steps, (list, tuple)) else []:
            try:
                clean.append(int(s))
            except (TypeError, ValueError):
                continue  # 模型可能给出 "第12步" 这类字符串，取不出数字就跳过
        if clean:
            sink(slug, clean)
    except Exception:  # pylint: disable=broad-except
        logger.debug("[memory] 记录 skill 来源步失败（已忽略）", exc_info=False)


def _drop_skill_source(slug: str) -> None:
    """让某个技能的来源映射失效。

    delete_skill 之后调：技能已从库里抹除，它的录像片段指向的是不存在的技能——留着是假证据
    （同 `_drop_facet_source`）。skill 的 slug 不可变，没有 move 这回事。
    """
    sink = _skill_source_sink.get()
    if sink is None:
        return
    try:
        sink(slug, [], op="drop")
    except Exception:  # pylint: disable=broad-except
        logger.debug("[memory] 失效 skill 来源步失败（已忽略）", exc_info=False)


logger = logging.getLogger(__name__)

def _identity_doc(conn: sqlite3.Connection, slug: str,
                  name: str | None = None, summary: str | None = None) -> str:
    """概念 **identity 分片**的嵌入文本 = `name\\nsummary`。

    分块索引（2026-09-24）的两类分片之一，另一类是 `_facet_doc`。

    ★ **为什么不再拼全部 facet**（取代 2026-09-21 的 `_concept_doc` 口径）：拼接长文会撞嵌入器
      真实窗口 —— KaLM `max_seq_length=512` **token**（中文 1 字≈0.6~1 token → 约 512~850 字），
      服务端截断是**静默**的。实测 Full run 的拼接正文 p90=2273 字、**34% 的概念超窗**，而旧护栏
      `_INDEX_DOC_MAX_CHARS=1200` 还**高于**真实窗口 → 512~1200 段既被截断又不告警。且截断保头
      去尾，而聚合 Agent 每次 `update_facet` 都把新内容追加在尾部 —— 最近并入的知识检索不到。
      分片化后每个嵌入单元都落在窗口内，`_FACET_MAX_CHARS` 从写入侧保证这一点。

    `name` / `summary` 可显式传入（调用方刚写完、库里还没提交或已知新值时），不传则回库里读。
    查不到该概念返回空串（调用方据此跳过 upsert）。
    """
    if name is None or summary is None:
        row = conn.execute(
            "SELECT name, summary FROM concept WHERE slug = ?", (slug,)
        ).fetchone()
        if row is None:
            return ""
        name = row[0] if name is None else name
        summary = row[1] if summary is None else summary
    return f"{name}\n{summary or ''}"


def _facet_doc(name: str, kind: str, content: str) -> str:
    """单条 facet 分片的嵌入文本 = `name\\n{kind}: {content}`。

    ★ **为什么要带上 name**（2026-09-24）：只嵌 `{kind}: {content}` 的话这条分片是个
      **没有主语的谓词** —— 「怎么做: 阶梯斜井而不是竖井」脱离了它所属的 Concept，
      想检索「下挖怎么才安全」的人根本对不上。给 chunk 拼上它所属条目的标题是
      contextual chunk embedding 的标准做法；代价只有 name 的长度（实测 p50=9 字，
      约占分片 2%），内容仍占绝对主导，`max` 聚合照样能挑出最匹配的那条。
      概念级信号另有 identity 分片（`name\\nsummary`）承载，两者分工不重叠。

    带 kind 而不是只嵌 content：kind 是「这条内容属于哪一类」的短标签（`怎么做` /
    `什么时候不成立`），本身就是检索时会被问出来的语义，白扔可惜。
    """
    return f"{name}\n{kind}: {content}"


def _guard_part_len(slug: str, what: str, doc: str, part_max_chars: int) -> str:
    """分片级长度护栏：超了记日志并截断。

    ⚠️ 与旧 `_INDEX_DOC_MAX_CHARS` 那条**整概念**护栏的分野：护栏现在贴在真正的嵌入单元上，
    所以「响了」就等于「真的有一段没被嵌进去」。正常路径下不该响 —— 写侧有
    `_FACET_MAX_CHARS` 硬拒，会响的只有 `merge_concepts` 拼接产生的超限
    （那条是**有意放行**的，它的第一职责是不丢内容）。
    """
    if len(doc) > part_max_chars:
        logger.warning("[memory] 概念 %s 的 %s 分片 %d 字 > 上限 %d，已截断"
                       "（超出的部分检索不到）", slug, what, len(doc), part_max_chars)
        return doc[:part_max_chars]
    return doc


def _index_concept(conn: sqlite3.Connection, vec_store, slug: str, *,
                   part_max_chars: int,
                   name: str | None = None, summary: str | None = None) -> None:
    """重建一个概念的**全部**索引分片（identity + 每条 facet 各一条）。

    用于概念级变动：新建、改 name/summary/slug、merge、批量 ingest。
    ⚠️ 单条 facet 的增删改**不要**调它 —— 那会把 embed 调用数从 1 涨到 1+N
    （N = facet 条数）。用 `_index_facet` / `_unindex_facet` 做增量，见各自的 docstring。

    ⚠️ 锁外调用（同 `create_concept` 的 upsert）：embed 要打 HTTP，不该压着 sqlite 锁。
    """
    doc = _identity_doc(conn, slug, name=name, summary=summary)
    if not doc:
        return
    row = conn.execute("SELECT name FROM concept WHERE slug = ?", (slug,)).fetchone()
    _n = row[0] if row else slug
    vec_store.upsert(part_biz_id(slug), _guard_part_len(slug, "identity", doc, part_max_chars),
                     {"slug": slug, "kind": None, "part": "identity", "name": _n})
    # 按 kind 排序：分片写入顺序必须**稳定**，否则同一内容两次嵌入顺序不同会引入无谓抖动
    # （分片各自独立嵌入，顺序本身不影响向量，但影响日志与剪枝差集的可读性）。
    for kind, content in conn.execute(
        "SELECT kind, content FROM facet WHERE concept_slug = ? ORDER BY kind", (slug,)
    ):
        vec_store.upsert(part_biz_id(slug, kind),
                         _guard_part_len(slug, kind, _facet_doc(_n, kind, content), part_max_chars),
                         {"slug": slug, "kind": kind, "part": "facet", "name": _n})
    _prune_parts(conn, vec_store, slug)


def _index_facet(conn: sqlite3.Connection, vec_store, slug: str, kind: str,
                 *, part_max_chars: int) -> None:
    """**只**重嵌一条 facet 分片（add_facet / update_facet 用）。

    ★ 保持 embed 调用数不涨的关键：分片化之后若每次 facet 改动都整概念重嵌，代价是 1+N 次
      HTTP；而 add/update facet **不改变** identity 文本（name/summary 没动），也没动别的
      facet —— 只需嵌这一条。实测聚合每窗 20~40 次 update_facet，差一个数量级。
    """
    row = conn.execute(
        "SELECT content FROM facet WHERE concept_slug = ? AND kind = ?", (slug, kind)
    ).fetchone()
    if row is None:
        return
    nrow = conn.execute("SELECT name FROM concept WHERE slug = ?", (slug,)).fetchone()
    _n = nrow[0] if nrow else slug
    vec_store.upsert(part_biz_id(slug, kind),
                     _guard_part_len(slug, kind, _facet_doc(_n, kind, row[0]), part_max_chars),
                     {"slug": slug, "kind": kind, "part": "facet", "name": _n})
    _prune_parts(conn, vec_store, slug)


def _unindex_facet(vec_store, slug: str, kind: str) -> None:
    """删掉一条 facet 分片（del_facet 用）。**不触发任何 embed**（删除不需要重算向量）。"""
    vec_store.delete(part_biz_id(slug, kind))


def _unindex_concept(vec_store, slug: str) -> None:
    """删掉一个概念的**全部**分片（del_concept / clear_layer 用）。

    ⚠️ 必须删干净：分片键是 `{slug}::{kind}`，只删 identity（旧的单条口径）会留下 N 条孤儿
    分片 —— `search` 仍会命中它们，而对应的 facet 已经不存在，读出来是空内容。
    """
    vec_store.delete(part_biz_id(slug))
    for kind in vec_store.part_kinds(slug):
        vec_store.delete(part_biz_id(slug, kind))


def _rename_concept_parts(vec_store, old_slug: str, new_slug: str,
                          conn: sqlite3.Connection, name: str | None = None) -> bool:
    """改 slug 时把**所有**分片搬到新 key，**复用已有向量、不重新 embed**。

    与 `vec_store.rename` 同款优化：改 slug 不改变任何嵌入文本，重嵌是纯白跑一趟网络。
    返回是否全部搬成功（任一失败 → 调用方回退到 `_index_concept` 整重建，见
    `UpdateConceptTool` 里那段注释）。
    """
    payload = {"name": name} if name else None
    ok = vec_store.rename(part_biz_id(old_slug), part_biz_id(new_slug), payload)
    for kind in vec_store.part_kinds(old_slug):
        ok = vec_store.rename(part_biz_id(old_slug, kind), part_biz_id(new_slug, kind),
                              payload) and ok
    return ok


def _prune_parts(conn: sqlite3.Connection, vec_store, slug: str) -> None:
    """剪掉该 slug 下「库里还留着、但 facet 表已没有」的孤儿分片。

    分片键是 kind，所以删 facet / 改 kind 之后可能留下旧 kind 的分片。留着它，`search` 会命中
    一个内容已不存在的分片并把概念的分抬上去，而那条内容读不出来 —— 比不命中更糟。
    """
    live = {k for (k,) in conn.execute(
        "SELECT kind FROM facet WHERE concept_slug = ?", (slug,))}
    for kind in vec_store.part_kinds(slug):
        if kind not in live:
            vec_store.delete(part_biz_id(slug, kind))
            logger.info("[memory] 概念 %s 剪掉孤儿索引分片 kind=%s", slug, kind)


# BaseTool 的顶层 import 会触发循环导入，须先把 openkage.agent_core.agent.events 引进来
# —— RetrieveMemoryTool/ExpandMemoryTool 是模块级类，需要这个
# 才能在 import 期就拿到 BaseTool；_build_tools 内的 10 个 CRUD 工具类是函数内定义，
# 本来就在调用期才 import，不受影响（那处 import 原样保留）。
import openkage.agent_core.agent.events  # noqa: E402,F401
from openkage.agent_core.tool.base import BaseTool  # noqa: E402


class RetrieveMemoryTool(BaseTool):
    """自主检索记忆图谱（主脑=公共、副脑=自己）：向量锚定 + 图扩展。

    只给任务域 ReAct Agent 用（minecraft 目前唯一的调用方）——理由见本模块顶部的
    docstring。`texts`：鸭子类型，需要 `.tool_desc(name)` / `.tool_text(name, field)` /
    `.result(key, **kw)`（域私有文案，取代硬编码某个域的 prompt store）。
    """

    def __init__(self, primary=None, secondary=None, *, texts, seen_provider=None):
        # ⚠️ 顺序要紧：`BaseTool.__init__` 会读 `self.name`/`self.description` 校验非空
        # （见 tool/base.py），而 `description` 这个 property 要用 `self._texts` —— 必须先
        # 把它赋好，再调 `super().__init__()`。
        self._primary = primary
        self._secondary = secondary
        self._texts = texts
        #: 可选回调 `() -> set[str]`：返回上文已注入过的概念 slug（域侧扫 history 汇出）。
        #:   Agent 主动调用不传 exclude → 自动带上这个集合做增量去重（结果只含新概念，
        #:   全重复时返回 miss+dedup note，Agent 知道是去重而非真无命中）；框架注入
        #:   直调时传显式 exclude，以显式值优先。None 则完全不去重（eval/消融）。
        #:   expand 工具**不接**这个（逃生门：想看重复概念的细节仍可 expand）。
        self._seen_provider = seen_provider
        self.n_calls = 0
        self.n_hits = 0
        super().__init__()  # BaseTool 在这里初始化 _before_call_hook 等内部状态，勿删

    @property
    def _max_hops(self) -> int:
        """跳数上限**从图谱实例读**（它构造时已从 yml 拿到 RetrievalConfig）。

        不自己去读一遍 yml：那就又是"同一个参数两个来源"—— 正是 2026-08-08 这次要消灭的形态
        （schema.py 常量 vs yml 曾静默分叉两天，见 RetrievalConfig 的 docstring）。
        两个图谱都没有（NullGraph 对照组）时给 1：没有图可走，跳数上限没有意义。
        """
        for mem in (self._primary, self._secondary):
            cfg = getattr(mem, "_retrieval", None)
            if cfg is not None:
                return cfg.expand_max_hops
        return 1

    @property
    def name(self) -> str:
        return "retrieve"

    @property
    def description(self) -> str:
        return self._texts.tool_desc("retrieve")

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": self._texts.tool_text("retrieve", "query")},
                "scope": {
                    "type": "string", "enum": ["own", "public", "both"],
                    "description": self._texts.tool_text("retrieve", "scope"),
                },
                # layer：私有图谱分 memory/experience 两层，默认只查经验层（记忆层有滑动窗口
                # 常驻注入）。公有图谱无层级，本参数对它无影响。both = 不过滤（旧行为）。
                "layer": {
                    "type": "string", "enum": ["experience", "memory", "both"],
                    "description": self._texts.tool_text("retrieve", "layer"),
                },
                # 跳数由 Agent 自己定：它才知道这次是想就近看看，还是要顺着依赖链往深处挖。
                "hops": {
                    "type": "integer", "minimum": 1, "maximum": self._max_hops,
                    "description": self._texts.tool_text("retrieve", "hops"),
                },
            },
            "required": ["query"],
        }

    def run(self, query: str = "", scope: str = "both", hops: int = 1,
            layer: str = "experience", exclude=None, **_kw) -> str:
        query = (query or "").strip()
        if not query:
            return self._texts.result("retrieve_empty_query")
        try:
            hops = max(1, min(int(hops or 1), self._max_hops))
        except (TypeError, ValueError):
            hops = 1
        # layer → retrieve_subgraph 的 layer 参数：experience/memory 只查该层，both = 不过滤（旧行为）。
        # 公有图谱只有 experience 层，传 memory 对它返回空、传 experience 等价全查，皆无害。
        layer_arg: str | None = None if layer == "both" else layer
        # ★ 增量去重（2026-09-13）：exclude（python-only，不进 schema）优先——框架注入
        #   直调时传；Agent 调用不传 → 从 seen_provider 取上文已注入的概念 slug。
        #   滤掉已见概念只留增量，旧消息一字不改（前缀缓存友好）。
        #   exclude_came_from_provider 记录来源：全排除时框架路径返回 ""（走「无新增」）、
        #   Agent 路径返回 dedup note（空 toolresult 会被误读成真 miss）。
        exclude_came_from_provider = False
        if exclude is None and self._seen_provider is not None:
            try:
                exclude = self._seen_provider() or None
                exclude_came_from_provider = exclude is not None
            except Exception:  # pylint: disable=broad-except
                logger.warning("[retrieve] seen_provider 失败（本次不去重）", exc_info=True)
                exclude = None
        self.n_calls += 1
        parts = []
        #: 出错的 scope 标签。**必须单独记**：只 `continue` 的话，另一个 scope 成功就会让
        #: 整次调用照打「→ 命中」，而 Agent 实际只拿到半边记忆（比如自己的经验层丢了、
        #: 只剩公共的）—— 那是静默降级，事后读日志完全看不出来。2026-08-10 那轮 7 次
        #: `sqlite3.InterfaceError` 全是这么被盖住的。
        failed: list[str] = []
        want_public = scope in ("public", "both")
        want_own = scope in ("own", "both")
        for label, mem, want in (
            (self._texts.result("scope_label_public"), self._primary, want_public),
            (self._texts.result("scope_label_own"), self._secondary, want_own),
        ):
            if not (want and mem is not None):
                continue
            try:
                # 走子图路径：比扁平列表多给「入口标记」与「概念之间怎么连」——
                # 后者是扁平列表里彻底丢失的信息。
                sg = mem.retrieve_subgraph(query, hops=hops, layer=layer_arg)
                if exclude:
                    sg = sg.without_slugs(exclude)
                ctx = None if sg.empty else mem.render_subgraph(sg)
            except Exception:  # pylint: disable=broad-except
                logger.warning("[retrieve] %s 检索失败", label, exc_info=True)
                failed.append(label)
                continue
            if ctx:
                parts.append(self._texts.result("retrieve_hit", label=label, ctx=ctx))
        # 把降级写进结果行本身，别让「命中」独自代表这次调用的全部真相。
        _status = "命中" if parts else "无命中"
        if exclude:
            _status += f"（排除上文已注入 {len(exclude)} 个概念后）"
        if failed:
            _status += f"（⚠️ {'、'.join(failed)} 检索出错，本次结果**不完整**）"
        logger.info("[retrieve] query=%r scope=%s hops=%d → %s", query[:60], scope, hops, _status)
        if not parts:
            if failed:
                return self._texts.result("retrieve_miss")
            # 全重复：检索本可命中，但内容全部已在上文（exclude 滤光）。两条路径不同返回：
            # · 框架注入直调（显式传 exclude）→ 空串，调用方据此走「无新增」分支；
            # · Agent 主动调用（exclude 来自 seen_provider）→ dedup note——空 toolresult 会
            #   被当成真 miss，Agent 不知道相关经验其实在上文。
            if exclude:
                if exclude_came_from_provider:
                    return self._texts.result("retrieve_dedup_note")
                return ""
            return self._texts.result("retrieve_miss")
        self.n_hits += 1
        # ★ EXP_BLOCK_MARKER 包裹（与框架注入块同一契约）：Agent 主动调用的结果也天然
        #   可被域侧 _collect_seen 扫到——全点去重对所有检索点一个口径。
        return EXP_BLOCK_MARKER + "\n" + "\n\n".join(parts)


class RetrieveSkillsTool(BaseTool):
    """自主检索已学技能的**签名**（按意图向量检索 verified 技能）。

    立计划时已默认按成功数+时间排序注入 top-N 签名块 + goal 检索补充（见 domains/minecraft `_inject_all_skills`）；
    本工具供 Agent 在执行中想用范围外技能时，主动按当前意图检索更多技能签名。
    ★ 技能 **js_code 已全量注入 programs**，这里只返回签名（让 Agent 知道有这个技能、
      怎么调），不重复传代码。查到签名 → write_code 里 `await 技能名(bot,…)` 直接可调。

    与 `RetrieveMemoryTool` 的分工：后者检索**经验记忆**（概念/facet，渲染成文本引导），
    本工具检索**代码技能**（签名，可直接 await 执行）。

    ★ **检索源 = SkillRegistry.retrieve_all**（查各臂 skill_{arm}_vec.db，跨臂语义检索）。
      技能归属重构（2026-08-27）后技能只存在各臂常驻 skill.db + skill_{arm}_vec.db，
      **公有/私有 concept 图谱都不存 skill 向量**——此前绑 primary/secondary（ArmGraph/
      BrainGraph）查的是 concept 向量库，恒 0 命中（见 0902 实测 113/113 全 miss）。
      retrieve_all 与签名层 _inject_all_skills、反思期 search_skills 同源，走 skill_{arm}_vec.db。

    ★ NullSkillStore / registry=None（eval 路径、no_graph 对照组）：retrieve_all 返回 []，
      本工具返回 miss 文案——等价于"没有可学技能"，与全量注入在对照组返回空一致。
    """

    def __init__(self, primary=None, secondary=None, *, texts, registry=None,
                 seen_provider=None):
        # ⚠️ 顺序同 RetrieveMemoryTool：BaseTool.__init__ 读 self.name/description，
        # 而 description property 要用 self._texts —— 必须先赋好再 super().__init__()。
        # primary/secondary 保留参数位（向后兼容旧构造点/测试），但 run() 不再用它们检索
        # 技能——技能检索走 registry.retrieve_all（查 skill_{arm}_vec.db）。memory 经验检索
        # 才用 primary/secondary。
        self._primary = primary
        self._secondary = secondary
        self._registry = registry
        self._texts = texts
        #: 可选回调 `() -> set[str]`：返回上文已注入过的技能签名（域侧扫 history 汇出）。
        #:   Agent 主动调用不传 exclude_sigs → 自动带上做增量去重；全排除 → miss+dedup note。
        #:   与 RetrieveMemoryTool.seen_provider 同机制（见那边注释）。
        self._seen_provider = seen_provider
        self.n_calls = 0
        self.n_hits = 0
        super().__init__()  # 勿删：初始化 _before_call_hook 等内部状态

    @property
    def name(self) -> str:
        return "retrieve_skills"

    @property
    def description(self) -> str:
        return self._texts.tool_desc("retrieve_skills")

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": self._texts.tool_text("retrieve_skills", "query"),
                },
                "limit": {
                    "type": "integer", "minimum": 1, "maximum": 3,
                    "description": self._texts.tool_text("retrieve_skills", "limit"),
                },
            },
            "required": ["query"],
        }

    def run(self, query: str = "", limit: int = 1, exclude_sigs=None, **_kw) -> str:
        query = (query or "").strip()
        if not query:
            return self._texts.result("retrieve_skills_empty_query")
        try:
            limit = max(1, min(int(limit or 1), 3))
        except (TypeError, ValueError):
            limit = 1
        # ★ 增量去重（2026-09-13）：exclude_sigs（python-only，不进 schema）优先——框架
        #   注入直调时传；Agent 调用不传 → 从 seen_provider 取上文已注入的技能签名。
        #   来源标记与 RetrieveMemoryTool 同款：全排除时框架路径 ""、Agent 路径 dedup note。
        exclude_from_provider = False
        if exclude_sigs is None and self._seen_provider is not None:
            try:
                exclude_sigs = self._seen_provider() or None
                exclude_from_provider = exclude_sigs is not None
            except Exception:  # pylint: disable=broad-except
                logger.warning("[retrieve_skills] seen_provider 失败（本次不去重）",
                               exc_info=True)
                exclude_sigs = None
        self.n_calls += 1
        # ★ 检索源 = SkillRegistry.retrieve_all（跨臂查 skill_{arm}_vec.db）。技能不在 concept
        #   图谱里，primary/secondary（ArmGraph/BrainGraph）查不到技能向量——见类 docstring。
        #   registry=None（eval/no_graph）→ 无技能可检，直接 miss。
        if self._registry is None:
            logger.info("[retrieve_skills] query=%r limit=%d → 无命中（registry=None）",
                        query[:60], limit)
            return self._texts.result("retrieve_skills_miss")
        try:
            hits = self._registry.retrieve_all(query, limit=limit)
        except Exception:  # pylint: disable=broad-except
            logger.warning("[retrieve_skills] registry.retrieve_all 检索失败", exc_info=True)
            hits = []
        if exclude_sigs:
            hits = [h for h in hits if h.get("signature") not in exclude_sigs]
        if not hits:
            logger.info("[retrieve_skills] query=%r limit=%d → 无命中%s",
                        query[:60], limit,
                        f"（排除上文已注入 {len(exclude_sigs)} 个签名后）" if exclude_sigs else "")
            if exclude_sigs:
                # 全重复：命中被去重滤光。与 RetrieveMemoryTool 同款——框架注入方（显式
                # exclude_sigs）拿空串走「无新增」；Agent 调用方（seen_provider 路径）
                # 拿 dedup note（空 toolresult 会被误读成真 miss）。
                if exclude_from_provider:
                    return self._texts.result("retrieve_skills_dedup_note")
                return ""
            return self._texts.result("retrieve_skills_miss")
        lines = []
        for h in hits:
            pre = f"（前置：{h['preconditions']}）" if h.get("preconditions") else ""
            lines.append(f"- {h['signature']} —— {h['intent']}{pre}")
        logger.info("[retrieve_skills] query=%r limit=%d → 命中 %d 条", query[:60], limit, len(lines))
        self.n_hits += 1
        # retrieve_all 跨臂不去重（两臂同意图技能各留各的），单段渲染即可——与签名层 _inject_all_skills
        # 的行格式完全一致（签名 —— 意图（前置）），不标来源/计数。
        # ★ SKILL_BLOCK_MARKER 包裹（与 retrieve 的 EXP_BLOCK 同一契约，见那边注释）。
        return SKILL_BLOCK_MARKER + "\n" + self._texts.result(
            "retrieve_skills_hit", label=self._texts.result("retrieve_skills_label"),
            lines="\n".join(lines))


class ExpandMemoryTool(BaseTool):
    """沿记忆图谱的边走一跳。只给任务域 ReAct Agent 用，理由与 `RetrieveMemoryTool` 相同。

    ★ 存在的理由：此前 Agent 只有 `retrieve(query)` 一个窗口 ——
    图遍历完全在 `graph._expand` 内部自动做掉（固定 1 跳、走哪些边由 query 语义自动选），
    返回的又是一张不带 slug/不带关系的扁平列表。于是**我们建了图，却只给它开了一个向量
    搜索的窗口**：它不知道自己站在图的哪个位置，想深挖只能换 query 重新猜概念的抽象名字。

    本工具把图显式交给 Agent：由它自己判断"我需要顺着 requires 往上找前置条件"。
    这本身就是模型推理能力参与「记忆怎么被使用」的体现，而不只是被动接收检索结果。
    """

    def __init__(self, primary=None, secondary=None, *, texts):
        # 顺序同 RetrieveMemoryTool：`super().__init__()` 会读 self.description，
        # 必须先把 `_texts` 赋好。
        self._primary = primary
        self._secondary = secondary
        self._texts = texts
        self.n_calls = 0
        self.n_hits = 0
        super().__init__()  # 同上，勿删

    @property
    def name(self) -> str:
        return "expand"

    @property
    def description(self) -> str:
        return self._texts.tool_desc("expand")

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "slug": {"type": "string", "description": self._texts.tool_text("expand", "slug")},
                "relation": {"type": "string", "description": self._texts.tool_text("expand", "relation")},
                "scope": {
                    "type": "string", "enum": ["own", "public", "both"],
                    "description": self._texts.tool_text("expand", "scope"),
                },
            },
            "required": ["slug"],
        }

    def run(self, slug: str = "", relation: str = "", scope: str = "both", **_kw) -> str:
        slug = (slug or "").strip()
        if not slug:
            return self._texts.result("expand_empty_slug")
        rel = (relation or "").strip() or None
        # ⚠️ 此处原有一道「relation 必须在 REFLECT 词表里」的预校验，已随关系词表放开删除
        #    （2026-08-09）：自由命名下没有名单可查，"合法关系"就是图里现存的边。
        #    不需要替代品 —— 查不到时下面的 `expand_miss_with_relation` 文案本来就更有用，
        #    它说的是「这个方向上还没有沉淀」，而那正是自由命名下的真实含义。
        self.n_calls += 1
        parts = []
        for label, mem, want in (
            (self._texts.result("scope_label_public"), self._primary, scope in ("public", "both")),
            (self._texts.result("scope_label_own"), self._secondary, scope in ("own", "both")),
        ):
            if not (want and mem is not None):
                continue
            try:
                sg = mem.expand_concept(slug, relation=rel)
            except Exception:  # pylint: disable=broad-except
                logger.warning("[expand] %s 展开失败 slug=%s", label, slug, exc_info=True)
                continue
            # 子图自带节点+边+入口标记，渲染统一交给 render_subgraph（只有一处渲染）
            if sg.empty:
                continue
            center = (sg.concepts.get(slug) or {}).get("name") or slug
            ctx = mem.render_subgraph(sg)
            if ctx:
                parts.append(self._texts.result("expand_hit", label=label, center=center, ctx=ctx))
        logger.info("[expand] slug=%r relation=%s scope=%s → %s",
                    slug[:50], rel or "全部", scope, "命中" if parts else "无命中")
        if not parts:
            # 两条 miss 文案各自写全（不做前缀+中段+后缀的三段拼接），见 yaml 里的注释。
            return (self._texts.result("expand_miss_with_relation", slug=slug, relation=rel)
                    if rel else self._texts.result("expand_miss_no_relation", slug=slug))
        self.n_hits += 1
        return "\n\n".join(parts)


#: 词表名 → 提供该 Agent 的层名单与工具文案的 AgentPrompts。`vocab` 参数取这里的 key。
#:
#: ★ **按 Agent 分，不按层分**：
#: - `"reflect"`（私有图谱 ArmGraph）**能写两层** —— memory（专名/带出处/只给明天的自己）
#:   **和** experience（通名/可共享），因为**只有经验层会被聚合**。层是 `create_concept` 的
#:   **显式参数**（由 Agent 声明，enum 取 `prompts.layers`）。
#: - `"aggregate"`（公共图谱 BrainGraph）只有 experience 一层 → 连 layer 参数都不出现
#:   （`_MULTI_LAYER` 为假时直接取 `layers[0]`）。
#:
#: ⚠️ **kind/relation 的 enum 已经没有了**（2026-08-09）：此前 aggregate 侧的工具 enum 会
#: **物理拒绝** `what_happened` 这类记忆 kind（"公共图谱里不该有专名个例"的第二道保证）。
#: 词表放开后那道保证只剩 system prompt 的「概念必须是通名」在管，而那是请求不是拦截。
#: 现在两套 vocab 的**唯一实质差别**就是层名单。
_VOCABS = {
    "reflect": REFLECT,
    "aggregate": AGGREGATE,
    "skill_maintenance": SKILL_MAINTENANCE,
}


def _build_tools(
    conn: sqlite3.Connection,
    vec_store,
    lock: threading.Lock,
    round_num: int,
    vocab: str = "aggregate",
    retrieval=None,
    skill_store=None,
    skill_registry=None,
    ablation: "str | None" = None,
) -> list:
    """构造反思/聚合用的 10 个窄工具。

    `vocab` 选哪个 Agent 的层名单与文案（见 _VOCABS）：私有图谱传 `"reflect"`，公共图谱传
    `"aggregate"`。`facet.kind` / `edge.relation` 字段本身是 TEXT，从来不需要改表结构。

    `retrieval`：`openkage.config.RetrievalConfig`，目前只用它的 `search_max_limit`。
    不传则自己 `RetrievalConfig.load()` 读 yml —— 和 `BrainGraph.__init__` 同一个兜底策略，
    **python 侧不留第二个默认值**（2026-08-08 用户定案；那次静默分叉的教训见 RetrievalConfig）。

    ★ **层是 `create_concept` 的显式参数**：反思 Agent 一个人要写两层
    （memory = 只给明天的自己，experience = **唯一会被聚合的**），由它自己声明属于哪层。
    合法值取 `prompts.layers`（yaml 的 `vocab.layers`）—— **层仍然是受控的**。

    ⚠️ **kind 与 relation 已放开为自由命名**（2026-08-09）：不再有 enum，只剩非空 / ≤40 字符 /
    无换行的卫生检查（见 `_label_invalid`）。随之删掉的还有 `layer × kind` 一致性校验 ——
    那是一次**知情的能力回退**，理由、后果与恢复方式见 `_kind_enum` 上方那段大注释。
    工具**返回给模型**的文案在 `prompts/default.yaml`（下面的 `_TOOL_RESULT`）。

    ★ **技能工具已重构（2026-08-27），两阶段再分工（2026-08-29）**：技能不再存 ArmGraph /
    公有脑，改存各臂常驻 SkillStore（见 openkage/memory/skill_store.py）。
    - `vocab="reflect"`（阶段1 经验/记忆反思）：建技能组 create_skill / list_skills /
      search_skills **+ delete_skill**（建错纠偏，绑**本臂 SkillStore**）。阶段1 有当天轨迹 +「今天跑通了」的事实，负责建技能；
      但**不给 update_skill / demote_skill**——改进/下线的判据是跨天累计成败 + 本次失败原因（诊断块
      只拼给阶段2），阶段1 没这份数据，凭单次印象维护会误下线稳定技能、或覆盖阶段2 刚校准的代码。
      阶段1 没有 update_skill，建错/写废/撞名后唯一的纠偏是删掉重建，故给 delete_skill（硬删除，
      删行+删向量、释放 slug/函数名、不可恢复；与阶段2 demote 的软下线分工）。
      无 skill_store（如旧调用/测试）则不注册技能工具（保持 concept 工具照常）。
    - `vocab="skill_maintenance"`（阶段2 技能维护）：**全量 5 个技能工具**（建技能组 + update/demote），
      无 concept 工具。阶段2 拿累计成败 + 本次失败原因诊断块，负责 update/demote，偶尔补建（create）。
    - `vocab="aggregate"`：**不再有技能工具**（聚合不碰技能，谁写的谁维护）。原先的
      adopt/merge/demote/search_skill 已删，`skill_candidates` 形参已移除。

    `ablation`（2026-09-18 leave-one-out，graph.reflect 透传）：no_memory_layer 时
    create_concept 拒绝 memory 层、no_exp_text 时拒绝 experience 层——被拆层的写入
    在**工具校验层**拦（enum 校验后第一道），报错文案指路另一层，反思 Agent 下一步
    就能把同一内容写进未拆的层。其余值/None 不校验。"""

    import openkage.agent_core.agent.events  # noqa: F401  避免下面 BaseTool 导入触发循环导入
    from openkage.agent_core.tool.base import BaseTool

    try:
        prompts = _VOCABS[vocab]
    except KeyError:
        raise ValueError(f"未知的词表 {vocab!r}，可用：{sorted(_VOCABS)}") from None
    # ★ skill_maintenance 词表不注册 concept 工具，但 _build_tools 无条件定义 concept 工具类
    #   （类体里调 prompts.tool_text / prompts.kind_field_desc 等）—— skill_maintenance.yaml
    #   的 tools 段是空的、没有 concept 工具文案。让 concept 工具的文案来源回退到 REFLECT
    #   （通用文案，反正 skill_maintenance 不返回 concept 工具、文案加载了也不注入给 Agent）。
    #   避免在 skill_maintenance.yaml 里重复 70 行 concept 工具描述。阶段2 的 system/user_input
    #   仍走 SKILL_MAINTENANCE（在 graph.reflect 那侧用，不经本函数 prompts）。
    if vocab == "skill_maintenance":
        prompts = REFLECT
    if retrieval is None:
        from openkage.config import RetrievalConfig
        retrieval = RetrievalConfig.load()
    _SEARCH_MAX_LIMIT = retrieval.search_max_limit
    # ── 分块索引 + facet 硬上限（2026-09-24，yml `memory_graph.facet.*` / `index.*`）──
    #: 单条 facet `content` 的字符硬上限。★ 承重改动，推导见 yml 与 `_identity_doc`。
    _FACET_MAX_CHARS = retrieval.facet_max_chars
    #: 每概念 facet 条数上限。界定过取倍数，推导见 `AddFacetTool` 里的注释。
    _FACET_MAX_PER_CONCEPT = retrieval.facet_max_per_concept
    #: 单个索引分片的字符告警线（截断阈值）。分片 = `name\n{kind}: {content}`
    #: （identity 分片是 `name\nsummary`）—— 推导见 yml `memory_graph.index.part_max_chars`。
    _PART_MAX_CHARS = retrieval.index_part_max_chars
    #: 融合权重与聚合方式，见 `_common.fuse_part_hits`。
    _W_IDENTITY = retrieval.index_w_identity
    _W_FACET = retrieval.index_w_facet
    _FACET_AGG = retrieval.index_facet_agg
    #: 过取倍数：一个概念最多占 1+max_per_concept 条分片行，故想稳拿 top_k 个**不同概念**，
    #: 最多得取 top_k × (1+max_per_concept) 行。KNN 行数从 4 涨到 ≤28，sqlite-vec 侧可忽略。
    _OVERFETCH_MULT = 1 + _FACET_MAX_PER_CONCEPT
    # ⚠️ 这里原先取 `prompts.kind_names, prompts.relation_names` 做工具 enum。
    #    两份词表已放开为自由命名（都是空列表），enum 也随之删掉 —— 不留一个恒空的变量
    #    （空 enum 会让任何值都非法，正是这次要避免的那个崩法）。
    _TOOL_TEXT = prompts.tool_text
    #: 技能工具的文案来源 —— **一律 reflect.yaml 的 tools 段**（见该段顶部注释：一套类、
    #: 两个 Agent 共用，reflect.yaml 是唯一真相；skill_maintenance 词表下上面的重定向已保证
    #: `prompts` 就是 REFLECT，这里无需再分 vocab 取）。
    _SKILL_TOOL_TEXT = REFLECT.tool_text
    #: 工具**返回给模型**的文案 —— 都在 `prompts/default.yaml`（两个 Agent 共用那一份；
    #: 它们自己的 yaml 里没有 `tool_results:` 一节，缺的键自动落到 default）。
    #: ⚠️ 占位符是**字面替换**，拿不到 `!r`：原文写 `{slug!r}` 的地方，调用点要传
    #:    `slug=repr(slug)`（`!r` 对含单引号的串会输出双引号，只有 `repr()` 逐字节等价）。
    _TOOL_RESULT = prompts.result
    #: 本 Agent 是否要自己选层（反思=True 两层；聚合=False 只有经验层）。
    _MULTI_LAYER = len(prompts.layers) > 1

    # ── kind / relation 的**卫生检查**：一处定义，多个工具共用 ──
    #
    # ★★ **2026-08-09：kind 与 relation 不再是 enum** ★★
    # 此前两者都是受控词表：`_kind_schema()` 出 `"enum": [...]`、`manage_edge` 同理，
    # 词表外的词在 `BaseTool.validate_params` 就被拒。现在词表放开为自由命名
    # （见两份 yaml 顶部），schema 里只剩 `"type": "string"`。
    #
    # ⚠️ **不能只是把 enum 换成空列表**：空 enum 会让**任何值都非法**，而 minecraft 从空图
    #    起步 —— 第一天就一条 facet 都写不进去、一条边都建不了。这是删 enum 而不是留空。
    #
    # 留下的卫生检查与词表无关，防的是另一件事：**把正文塞进标签**。
    # kind/relation 会进 prompt、当分组键用、还要参与检索打分（关系名现在就是权重文本），
    # 一个 200 字带换行的"标签"会同时破坏渲染、归并与打分。
    _LABEL_MAX_LEN = 40

    def _label_invalid(value: str, *, what: str) -> str | None:
        """kind / relation 这类**短标签**的共用卫生检查。返回错误信息；None 表示合法。

        `what` 是给模型看的字段名（`kind` / `relation` / `new_relation`）。
        判据只有三条，且都不涉及"这个词在不在某个名单里"—— 名单已经没有了。
        """
        v = (value or "").strip()
        if not v:
            return _TOOL_RESULT(f"{what}_empty" if what == "kind" else "edge_relation_empty")
        if len(v) > _LABEL_MAX_LEN:
            return _TOOL_RESULT("label_too_long", what=what, n=len(v), max_len=_LABEL_MAX_LEN)
        if any(ch in v for ch in "\n\r\t"):
            return _TOOL_RESULT("label_has_newline", what=what)
        return None

    def _kind_enum() -> list[str]:
        """本图谱**已经用过**的 kind，按次数降序 —— 只作为 schema 里的 `examples` 提示。

        ⚠️ **这不再是 enum**（词表放开后没有合法名单可言）：它的用途是让模型在填 kind 时
        看到"这个图谱已经在用哪些标签"，从而倾向复用而不是每次新造一个。
        真正把这件事说透的是 user_input 里那块 `dump_used_vocab`（带次数、kind 与关系都有）；
        这里只是顺手在参数层面再提示一次，**不做任何拦截**。

        空图（第一天）返回空列表 —— JSON Schema 的 `examples` 为空数组是合法的、
        不影响取值，这与当年空 `enum` 会拒掉一切完全不同。
        """
        return [k for (k,) in conn.execute(
            "SELECT kind FROM facet WHERE kind IS NOT NULL AND kind != '' "
            "GROUP BY kind ORDER BY COUNT(*) DESC, kind")]

    def _kind_schema() -> dict:
        out = {"type": "string", "description": prompts.kind_field_desc}
        used = _kind_enum()
        if used:
            out["examples"] = used
        return out

    def _source_steps_schema() -> dict:
        """`source_steps` 的 schema —— create_concept / add_facet / update_facet 共用一处，
        别各写一份（三处 items/type 若漂移，模型在三个工具间会收到不一致的约束）。"""
        return {"type": "array", "items": {"type": "integer"},
                "description": prompts.source_steps_field_desc}

    def _concept_layer(concept_slug: str, conn: sqlite3.Connection) -> str:
        """读某个已有概念的层（`concept.layer`）。查不到返回空串。"""
        row = conn.execute(
            "SELECT layer FROM concept WHERE slug = ?", (concept_slug,)
        ).fetchone()
        return (row[0] if row else "") or ""

    # ★★ **`_layer_kind_conflict` 已删除（2026-08-09）** ★★
    #
    # 它做的是「声明 layer=experience 却挂 what_happened 就拒」这道一致性校验，实现依赖
    # `prompts.kind_layers`（kind→层的映射）—— 而那张表是从 yaml 的两份 kind 列表推出来的，
    # 随词表放开一起消失了。自由命名下这道校验**在原理上不可能存在**：`my_note` 属于哪一层，
    # 没有任何静态信息能回答。
    #
    # ⚠️ **这是一次知情的能力回退，不是重构**。它原本堵住两个方向的静默漏洞：
    #   · `layer=memory` 挂经验类 kind → 那条经验**永远不会被聚合**（聚合只读经验层），
    #     且完全静默 —— 只是白写了；
    #   · `layer=experience` 挂个例类 kind → 带坐标日期的个例被聚合进公共图谱，
    #     而那些坐标对下一段探索是假的（世界每轮重生）。
    # 现在剩下的兜底只有两道，都更粗：
    #   ① `consolidate._warn_layer_violations`：某个副脑**整层为 0 个概念**才响；
    #   ② 两份 yaml 的 system prompt 反复讲「层是概念级的、两种东西别塞进同一个概念」——
    #      那是**请求**，不是拦截。
    # 所以反推词表时要专门看：公共图谱里有没有混进专名/坐标，经验层与记忆层的 kind 有没有串。
    # 词表填回来之后**应当把这个函数与它的两个调用点一起恢复**
    # （原实现见 git history：`create_concept` 与 `add_facet` 各一处）。

    def _slug_invalid(slug: str) -> str | None:
        """slug 塌缩到无信息就返回错误信息，否则 None（兜底断言）。

        `_slugify` **丢弃所有非 ASCII 字符**（见 _common._slugify）：`"我的1号营地"` → `"1"`，
        `"东边那片矿洞"` 一个 ASCII 都没有 → 空串 → 兜底成 `"concept"`，再来一个就是
        `"concept_2"`。而 slug 不只是显示名，它是 Agent 后续 `manage_edge` / `get_concept`
        的**唯一句柄** —— 塌成 `concept`/`concept_2` 之后模型看着一堆无语义 slug 没法连边。
        """
        s = (slug or "").strip()
        if not s:
            return _TOOL_RESULT("slug_empty")
        collapsed = _slugify(s)
        # 判据是「**有没有丢信息**」，不是「结果够不够长」。
        # ⚠️ 别拿长度当判据：真实 slug 长 18~49 字符，但 `tnt` / `bed` / `axe` / `hp` 这类
        #    **三字母以内的合法英文**也是完全正常的 slug，按 `len<=2` 拦会误伤。而本断言要防的
        #    是**中文被吃掉**这一件事，那件事的准确信号是「原文有非 ASCII 字符」+
        #    「规整后没剩下什么」，与长度无关。
        dropped_non_ascii = any(not c.isascii() for c in s)
        no_info_left = collapsed == "concept" or collapsed.replace("_", "").isdigit()
        if dropped_non_ascii and no_info_left:
            return _TOOL_RESULT("slug_no_info", slug=repr(s), collapsed=repr(collapsed))
        return None

    def _kind_invalid(kind: str) -> str | None:
        """facet kind 的校验。返回错误信息字符串；None 表示合法。

        ★ **只剩卫生检查**（2026-08-09）：kind 已放开为自由命名，没有"合法名单"可查
        （见 `_label_invalid` 上方那段）。非空 / ≤40 字符 / 无换行三条，与词表无关。
        """
        return _label_invalid(kind, what="kind")

    def _reason_invalid(reason: str) -> str | None:
        """删除类工具的 `reason` 校验。返回错误信息；None 表示合法。

        为什么它不能是空的：`reason` 是**删除的唯一凭证** —— 删掉的东西不留副本，
        事后想知道"这条经验为什么没了"只有它。
        也拦"就一两个字"（`r` / `错了`）：那种理由在日志里等于噪音。
        """
        r = (reason or "").strip()
        if not r:
            return _TOOL_RESULT("reason_empty")
        if len(r) < 4:
            return _TOOL_RESULT("reason_too_short", reason=repr(r))
        return None

    def _content_invalid(content: str) -> str | None:
        """facet 内容的最低限度校验。返回错误信息；None 表示合法。

        为什么必须拦空值：空 content 会造成两层后果 ——
          ① 检索会把它渲染成 `[where] ` 一个空条目，白占上下文；
          ② `add_facet` 从此对这个 kind 报"已经有一条了"，把 Agent 引向 `update_facet` ——
             而它并不知道那条是空的，于是这个 (concept, kind) 槽位被一条空记录占死。
        ★ **2026-09-24 加长度上限，推翻了这里原先"不设长度上限"的定案。** 那条定案的前提是
        「facet 长一点没关系」，而它没有考虑到**索引侧**：
        ① 嵌入器 KaLM `max_seq_length=512` **token**（中文 1 字≈0.6~1 token → 约 512~850 字），
           服务端截断是**静默**的；
        ② 聚合 prompt 的「同一 kind 只有一条」+ `update_facet` 整条覆盖，让热 facet 随窗口
           **单调膨胀**（实测 Full run 拼接正文 p90=2273 字、34% 的概念超窗）；
        ③ 截断是**保头去尾**，而 Agent 每次把新内容追加在尾部 —— 最近并入的知识检索不到。
        所以「限长会误伤」这句话在长尾上正好说反了：不限长的代价是**静默丢失**。
        ⚠️ 超限是**硬拒**（返回 ok:false）而不是截断：截断等于替 Agent 决定丢哪一半，而它
        丢的那半恰好是刚写进去的。硬拒让它自己压缩或拆成新概念 —— 沿用 `_LABEL_MAX_LEN`
        那套「给模型一个明确信号」的模式。
        """
        if not (content or "").strip():
            return _TOOL_RESULT("content_empty")
        if len(content) > _FACET_MAX_CHARS:
            return _TOOL_RESULT("content_too_long", n=len(content),
                                max_chars=_FACET_MAX_CHARS)
        return None

    class CreateConceptTool(_BoundTool, BaseTool):
        name = "create_concept"
        description = _TOOL_TEXT("create_concept", "desc")
        _props = {
            "name": {"type": "string", "description": _TOOL_TEXT("create_concept", "name")},
            "slug": {"type": "string", "description": _TOOL_TEXT("create_concept", "slug")},
            "summary": {"type": "string", "description": _TOOL_TEXT("create_concept", "summary")},
            "kind": _kind_schema(),
            "content": {"type": "string", "description": _TOOL_TEXT("create_concept", "content")},
        }
        _required = ["name", "slug", "summary", "kind", "content"]
        if _MULTI_LAYER:
            _props["layer"] = {
                "type": "string", "enum": list(prompts.layers),
                "description": _TOOL_TEXT("create_concept", "layer"),
            }
            _required.insert(0, "layer")  # 放最前：先决定属于哪一层，再想名字和内容
            # ★ **category 只在能写记忆层时才有意义**（2026-09-12）：它是记忆层概念的
            #   四分类（episodic/spatial/lesson/status），供 dump_memory_layer 按桶配额注入。
            #   experience 层不进注入窗口（走聚合-清空循环），没有类别；聚合 Agent 是单层
            #   （_MULTI_LAYER=False）、结构上进不了这个分支。
            #   ⚠️ **enum 但不进 _required**：required 放在 run() 里按 layer 分支校验 ——
            #   experience 的 create 不该被迫填一个无意义的类别；memory 的 create 缺了
            #   category 会在 run() 里被拒（没有降级兜底，2026-09-12 用户定案）。
            _props["category"] = {
                "type": "string", "enum": list(prompts.categories),
                "description": _TOOL_TEXT("create_concept", "category"),
            }
            # ★ `source_steps` 这里**也要有**，理由是数据：本工具自己就插一条 facet（见下方
            #   那条 INSERT），而实测模型的习惯是「建概念时把内容一并写好，之后不再补」——
            #   真实 run 里 create 建的 facet 占 71%~99%（alice 123/124、bob 149/150、
            #   carol 100/140）。只给 add_facet 加，等于绝大多数 facet 永远挂不上录像。
            #   与 add_facet 共用同一个 sink 和同一套清洗（见 _record_facet_source）。
            _props["source_steps"] = _source_steps_schema()
        parameters = {"type": "object", "properties": _props, "required": _required}

        def run(self, name: str = "", slug: str = "", summary: str = "",
                 kind: str = "", content: str = "", layer: str = "",
                 category: str = "", source_steps=None, **_kw) -> str:
            _err = _slug_invalid(slug) or _kind_invalid(kind) or _content_invalid(content)
            if _err:
                return json.dumps({"ok": False, "error": _err}, ensure_ascii=False)
            if _MULTI_LAYER:
                if layer not in prompts.layers:
                    return json.dumps(
                        {"ok": False,
                         "error": _TOOL_RESULT("layer_invalid", layers=list(prompts.layers),
                                               layer=repr(layer))},
                        ensure_ascii=False)
                # ⚠️ 此处原有一道 `_layer_kind_conflict(layer, kind)` 校验，已随 kind 词表
                #    放开一起删除（理由与后果见本文件 `_kind_enum` 上方那段大注释）。
                #    **layer 本身仍然受控** —— 上面那个 enum 检查照旧。
                # ★ leave-one-out 消融层拒绝（2026-09-18）：被拆的层在工具校验层拦下。
                #    报错文案明确说这层不可用+指路另一层，反思 Agent 下一步就能把同一内容
                #    写进未拆的层（不是静默丢弃——静默丢会让"该层恒空"变成"反思在乱写"的错觉）。
                if ablation == "no_memory_layer" and layer == LAYER_MEMORY:
                    return json.dumps(
                        {"ok": False,
                         "error": ("本次运行的记忆层已关闭（消融 no_memory_layer）：memory 层不可写。"
                                   "请把这条内容按经验层口径改写——去坐标/日期/第一人称，"
                                   "用通名概念 + layer=experience 重新提交。")},
                        ensure_ascii=False)
                if ablation in ("no_exp_text", "no_exp_layer") and layer == LAYER_EXPERIENCE:
                    return json.dumps(
                        {"ok": False,
                         "error": (f"本次运行的经验文本已关闭（消融 {ablation}）：experience 层"
                                   "不可写。请只保留记忆层记录（layer=memory，带坐标/日期的"
                                   "situated 记录）；通名经验的归纳本运行不收集。")},
                        ensure_ascii=False)
            else:
                # 单层 Agent：层就是它唯一能写的那一层（聚合 → experience）
                layer = prompts.layers[0]
            # ★ category 校验（2026-09-12）：只在记忆层收。缺了/不认识都拒——报错文案里
            #   给全判据（哪类填什么），让它下一步就能改对，而不是猜。
            #   experience 层不收（传了也忽略——enum 只在 layer=memory 分支里查）。
            category_val: str | None = None
            if layer == LAYER_MEMORY:
                if category not in prompts.categories:
                    return json.dumps(
                        {"ok": False,
                         "error": _TOOL_RESULT("category_invalid",
                                               categories=list(prompts.categories),
                                               category=repr(category))},
                        ensure_ascii=False)
                category_val = category
            with self._lock:
                final_slug = _slugify(slug)
                # ★ **撞名就拒，不再静默加后缀**：静默改名会让 Agent 拿到一个它不知道的新
                # slug，而 user_input 模板/后续连边往往假设"我刚建的就是这个名字"，模型可能
                # 照原名连边，导致新建的那份内容一条边都没有、永远走不到——看起来完好无损，
                # 实际上分叉了一条死支。
                #
                # 为什么对专名尤其致命：`day_N` 是**可预测的确定性名字**，模型有充分理由认为
                # "我要建 day_3"就是 day_3。撞名的真实含义是"今天已经记过了"，正确反应是去
                # update/get，而不是造第二个第 3 天。
                # （加后缀对通名去重本来是合理的 —— 但那种情况应该由 Agent 看到冲突后自己
                # 决定换名还是复用，而不是工具替它选一个它不知道的名字。）
                #
                # 报错里**带上已有那个概念的现状**（层 + 已有 kind），让它一步就能决定
                # 下一步是 update_facet / add_facet / get_concept 还是换个 slug。
                if self._concept_exists(final_slug):
                    exist_layer = _concept_layer(final_slug, self._conn)
                    exist_kinds = _facet_kinds(self._conn, final_slug)
                    return json.dumps(
                        {"ok": False,
                         # `slug` 是 repr 形态（原文 `{final_slug!r}`），而 `plain_slug` 是
                         # 给它照抄进 `get_concept("…")` 的裸名字 —— 那处本来就没有 !r。
                         "error": _TOOL_RESULT("concept_exists", slug=repr(final_slug),
                                               plain_slug=final_slug,
                                               layer=exist_layer or _TOOL_RESULT("filler_unknown_layer"),
                                               kinds=exist_kinds or _TOOL_RESULT("filler_no_kinds"))},
                        ensure_ascii=False)
                self._conn.execute(
                    "INSERT INTO concept (slug, name, summary, layer, category, "
                    "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (final_slug, name, summary, layer, category_val,
                     self._round_num, self._round_num),
                )
                self._conn.execute(
                    "INSERT INTO facet (concept_slug, kind, content, "
                    "created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                    (final_slug, kind, content, self._round_num, self._round_num),
                )
                self._conn.commit()
            # ★ 索引分片化（2026-09-24）：identity + 首条 facet 共 2 条向量，
            #   不再拼成一条长文（拼接会撞嵌入器 512 token 窗口，见 `_identity_doc`）。
            #   此处 facet 已随概念在同一事务里 commit，读得到刚写的那条。
            _index_concept(self._conn, self._vec_store, final_slug,
                           part_max_chars=_PART_MAX_CHARS, name=name, summary=summary)
            # 落成功之后才记来源步。键用 **final_slug**（_slugify 之后的那个）——
            # facet 行存的就是它，用模型传的原始 slug 会让前端按 slug 查不到映射。
            _record_facet_source(final_slug, kind, source_steps)
            return json.dumps({"ok": True, "slug": final_slug}, ensure_ascii=False)

    class UpdateConceptTool(_BoundTool, BaseTool):
        name = "update_concept"
        description = _TOOL_TEXT("update_concept", "desc")
        parameters = {
            "type": "object",
            "properties": {
                "concept_slug": {"type": "string", "description": _TOOL_TEXT("update_concept", "concept_slug")},
                "name": {"type": "string", "description": _TOOL_TEXT("update_concept", "name")},
                "slug": {"type": "string", "description": _TOOL_TEXT("update_concept", "slug")},
                "summary": {"type": "string", "description": _TOOL_TEXT("update_concept", "summary")},
            },
            "required": ["concept_slug"],
        }

        def run(self, concept_slug: str = "", name: str | None = None, slug: str | None = None,
                 summary: str | None = None, **_kw) -> str:
            if name is None and slug is None and summary is None:
                return json.dumps(
                    {"ok": False, "error": _TOOL_RESULT("update_concept_no_field")}, ensure_ascii=False
                )
            with self._lock:
                row = self._conn.execute(
                    "SELECT name, summary FROM concept WHERE slug = ?", (concept_slug,)
                ).fetchone()
                if row is None:
                    return json.dumps(
                        {"ok": False, "error": _TOOL_RESULT("concept_not_found", slug=concept_slug)},
                        ensure_ascii=False)
                cur_name, cur_summary = row
                new_name = name if name is not None else cur_name
                new_summary = summary if summary is not None else cur_summary

                new_slug = concept_slug
                if slug is not None:
                    # slug 塌缩断言在这里**同样要做**：漏了这一半，`update_concept(slug="我的1号
                    # 营地")` 照样能把 slug 改成 `"1"`，且比 create 更糟——它返回 ok:true，概念
                    # 连着 facet 和边一起被搬到那个无信息的 slug 上。
                    _err = _slug_invalid(slug)
                    if _err:
                        return json.dumps({"ok": False, "error": _err}, ensure_ascii=False)
                    candidate = _slugify(slug)
                    if candidate != concept_slug:
                        if self._concept_exists(candidate):
                            return json.dumps(
                                {"ok": False, "error": _TOOL_RESULT("slug_taken", slug=candidate)},
                                ensure_ascii=False,
                            )
                        new_slug = candidate

                # slug 变更要在**同一个事务内**改完父行 + 所有子行引用，且必须把 FK 检查
                # 推迟到 commit（`defer_foreign_keys` 只对本事务生效、commit 后自动复位）。
                # 否则先 UPDATE 父行的那一刻子行瞬时孤立 → FK 开启后当场 IntegrityError
                # （见 graph.py 的 PRAGMA foreign_keys=ON）。
                if new_slug != concept_slug:
                    self._conn.execute("PRAGMA defer_foreign_keys=ON")
                self._conn.execute(
                    "UPDATE concept SET slug = ?, name = ?, summary = ?, updated_at = ? WHERE slug = ?",
                    (new_slug, new_name, new_summary, self._round_num, concept_slug),
                )
                if new_slug != concept_slug:
                    # cascade 只管删不管改（没有 ON UPDATE CASCADE），引用要手动迁
                    self._conn.execute(
                        "UPDATE facet SET concept_slug = ? WHERE concept_slug = ?", (new_slug, concept_slug)
                    )
                    self._conn.execute(
                        "UPDATE edge SET from_slug = ? WHERE from_slug = ?", (new_slug, concept_slug)
                    )
                    self._conn.execute(
                        "UPDATE edge SET to_slug = ? WHERE to_slug = ?", (new_slug, concept_slug)
                    )
                self._conn.commit()
            # ★ 映射键跟着 slug 搬。**放在这里**（commit 之后、向量库分支之前）：下面有两个
            #   return 出口（rename 成功 / 重嵌入），挂在任一个上都会漏掉另一条路径。
            _move_facet_source(concept_slug, new_slug)
            # 向量库：被嵌入的文本是 `name\nsummary`，所以**只有它变了才需要重嵌入**。
            # ★ 只改 slug（name/summary 都没传）时走 `rename`：复用已有向量、不调 embedder ——
            #   rowid 由 biz_id 哈希而来，"换 slug"在向量库里就是"把同一条 embedding 搬个位置"，
            #   不需要重新打一次 HTTP 到 embedding 服务算一个本该完全相同的向量。
            text_changed = (new_name, new_summary) != (cur_name, cur_summary)
            if not text_changed and new_slug != concept_slug:
                # ★ 分片版 rename：identity 与每条 facet 分片**全部**搬到新 key（见
                #   `_rename_concept_parts`）。只搬 identity 会留下 N 条孤儿 facet 分片。
                #   rename 失败（老条目不在库里，比如 embedder 当时未启用）→ 回退到重嵌入，
                #   否则这个概念会在向量库里彻底缺失、再也检索不到。
                if _rename_concept_parts(self._vec_store, concept_slug, new_slug,
                                         self._conn, name=new_name):
                    return json.dumps({"ok": True, "slug": new_slug}, ensure_ascii=False)
            if new_slug != concept_slug:
                _unindex_concept(self._vec_store, concept_slug)
            _index_concept(self._conn, self._vec_store, new_slug,
                           part_max_chars=_PART_MAX_CHARS,
                           name=new_name, summary=new_summary)
            return json.dumps({"ok": True, "slug": new_slug}, ensure_ascii=False)

    class AddFacetTool(_BoundTool, BaseTool):
        name = "add_facet"
        description = _TOOL_TEXT("add_facet", "desc")
        _props = {
            "concept_slug": {"type": "string"},
            "kind": _kind_schema(),
            "content": {"type": "string"},
        }
        # ★ `source_steps` **只给反思 Agent**（与 create_concept 的 `layer` 同一套路：
        #   用 _MULTI_LAYER 区分两个 Agent）。理由是它对聚合毫无意义 —— 聚合读的是各副脑
        #   的经验层通名结论，早已脱离具体轨迹，报不出步号；给了只会诱导它编。
        #   顺带也免了在 aggregate.yaml 里补一段"请勿填写"的废话文案。
        if _MULTI_LAYER:
            _props["source_steps"] = _source_steps_schema()
        parameters = {
            "type": "object",
            "properties": _props,
            "required": ["concept_slug", "kind", "content"],
        }

        def run(self, concept_slug: str = "", kind: str = "", content: str = "",
                source_steps=None, **_kw) -> str:
            _err = _kind_invalid(kind) or _content_invalid(content)
            if _err:
                return json.dumps({"ok": False, "error": _err}, ensure_ascii=False)
            with self._lock:
                if not self._concept_exists(concept_slug):
                    return json.dumps(
                        {"ok": False, "error": _TOOL_RESULT("concept_not_found", slug=concept_slug)},
                        ensure_ascii=False)
                # ⚠️ 此处原有一道跨层校验（`_layer_kind_conflict`）—— **那个静默漏洞的堵点**：
                #    往 memory 层概念上挂经验类 kind，那条经验永远不会被聚合（聚合只读经验层），
                #    而且不报错、无日志，只是它白写了。它已随 kind 词表放开一起删除，
                #    **那个漏洞现在是敞开的**（见本文件 `_kind_enum` 上方那段大注释）。
                if self._facet_exists(concept_slug, kind):
                    return json.dumps(
                        {"ok": False, "error": _TOOL_RESULT("facet_exists", slug=concept_slug, kind=kind)},
                        ensure_ascii=False,
                    )
                # ★ 条数上限（2026-09-24）：**保险而非承重** —— 实测 25 个 run 每概念最多 8 条、
                #   p90 仅 1~4 条，真正撑爆嵌入窗口的是单条长度（那道由 `_content_invalid` 管）。
                #   它的实质作用是**界定过取倍数**：每概念最多 1+本值 条索引分片，
                #   「取回 top_k 个不同概念」要过取多少行才有上界（见 `_OVERFETCH_MULT`）。
                n_facets = self._conn.execute(
                    "SELECT COUNT(*) FROM facet WHERE concept_slug = ?", (concept_slug,)
                ).fetchone()[0]
                if n_facets >= _FACET_MAX_PER_CONCEPT:
                    return json.dumps(
                        {"ok": False,
                         "error": _TOOL_RESULT("facet_limit", slug=concept_slug,
                                               n=n_facets, max_n=_FACET_MAX_PER_CONCEPT)},
                        ensure_ascii=False,
                    )
                self._conn.execute(
                    "INSERT INTO facet (concept_slug, kind, content, "
                    "created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                    (concept_slug, kind, content, self._round_num, self._round_num),
                )
                # ★ 碰了概念的内容 → 推进父 concept.updated_at（2026-08-25）。
                #   这是 dump_memory_layer / _dump_layer 撞顶滑动窗口的判据：护栏要留的是
                #   「最近被活动过的」记忆，而不是「最近创建的」。add_facet 是记忆层最常见的
                #   编辑（往 day_N 上补今天发生的事），不回写则 updated_at 建后不动、
                #   排序退化为 created_at。回写必须在 commit 前、同事务。
                self._conn.execute(
                    "UPDATE concept SET updated_at = ? WHERE slug = ?",
                    (self._round_num, concept_slug),
                )
                self._conn.commit()
            # facet 写成功之后才记来源步（写失败的 facet 不该留下映射）。
            _record_facet_source(concept_slug, kind, source_steps)
            # ★ 只重嵌这一条 facet 分片，不整概念重嵌：add_facet 不改 name/summary、
            #   也不动别的 facet，整重建会把 embed 调用数从 1 涨到 1+N。
            _index_facet(self._conn, self._vec_store, concept_slug, kind,
                         part_max_chars=_PART_MAX_CHARS)
            return json.dumps({"ok": True}, ensure_ascii=False)

    class UpdateFacetTool(_BoundTool, BaseTool):
        name = "update_facet"
        description = _TOOL_TEXT("update_facet", "desc")
        _props = {
            "concept_slug": {"type": "string"},
            "kind": _kind_schema(),
            "content": {"type": "string"},
        }
        # ★ `source_steps` 这里也要有（只给反思侧，同 add_facet）：本工具**整条覆盖** content，
        #   改完之后旧的来源步指向的是**被推翻的那个说法** —— 留着它，人点开录像会看到与正文
        #   矛盾的画面，还以为是记忆出了错。而纠错自愈正是本实验的主张之一，这条链路上不能有
        #   假证据。不填则沿用旧映射（多数 update 是补充而非推翻，见 `_record_facet_source`）。
        if _MULTI_LAYER:
            _props["source_steps"] = _source_steps_schema()
        parameters = {
            "type": "object",
            "properties": _props,
            "required": ["concept_slug", "kind", "content"],
        }

        def run(self, concept_slug: str = "", kind: str = "", content: str = "",
                source_steps=None, **_kw) -> str:
            # kind 校验也做在这里（`add_facet` / `del_facet` 同样都校验），避免词表外的
            # kind 和合法但不存在的 kind 收到同一句"没有这条 facet，先用 add_facet 新建"——
            # 前者照建议去 add_facet 会再被 kind 校验拒一次，白花两步。
            _err = _kind_invalid(kind) or _content_invalid(content)
            if _err:
                return json.dumps({"ok": False, "error": _err}, ensure_ascii=False)
            with self._lock:
                if not self._facet_exists(concept_slug, kind):
                    # kind 本身合法（上面查过了），所以这里只剩两种可能，分别说清楚
                    exists = self._concept_exists(concept_slug)
                    return json.dumps(
                        {"ok": False,
                         "error": (_TOOL_RESULT("facet_kind_missing", slug=concept_slug, kind=kind,
                                                kinds=_facet_kinds(self._conn, concept_slug)
                                                or _TOOL_RESULT("filler_no_facets"))
                                   if exists else
                                   _TOOL_RESULT("facet_concept_missing", slug=repr(concept_slug)))},
                        ensure_ascii=False,
                    )
                self._conn.execute(
                    "UPDATE facet SET content = ?, updated_at = ? "
                    "WHERE concept_slug = ? AND kind = ?",
                    (content, self._round_num, concept_slug, kind),
                )
                # ★ 同 add_facet：碰了概念内容 → 推进父 concept.updated_at（2026-08-25）。
                self._conn.execute(
                    "UPDATE concept SET updated_at = ? WHERE slug = ?",
                    (self._round_num, concept_slug),
                )
                self._conn.commit()
            # 内容改成功之后才改来源步（同 add_facet：写失败的 facet 不该动映射）
            _record_facet_source(concept_slug, kind, source_steps)
            # ★ 同 add_facet：只重嵌这一条 facet 分片。update_facet 是**整条覆盖**，
            #   不重嵌则向量里留的是被推翻的那个说法。
            _index_facet(self._conn, self._vec_store, concept_slug, kind,
                         part_max_chars=_PART_MAX_CHARS)
            return json.dumps({"ok": True}, ensure_ascii=False)

    class MergeConceptsTool(_BoundTool, BaseTool):
        name = "merge_concepts"
        description = _TOOL_TEXT("merge_concepts", "desc")
        parameters = {
            "type": "object",
            "properties": {"primary_slug": {"type": "string"}, "secondary_slug": {"type": "string"}},
            "required": ["primary_slug", "secondary_slug"],
        }

        def run(self, primary_slug: str = "", secondary_slug: str = "", **_kw) -> str:
            if primary_slug == secondary_slug:
                return json.dumps({"ok": False, "error": _TOOL_RESULT("merge_same_slug")}, ensure_ascii=False)
            with self._lock:
                if not self._concept_exists(primary_slug) or not self._concept_exists(secondary_slug):
                    return json.dumps({"ok": False, "error": _TOOL_RESULT("merge_missing")},
                                      ensure_ascii=False)

                # ★ **跨层合并要拒**（与 add_facet 同一道防线）：往 experience 层并入 memory 层
                # 的内容，会让带坐标日期的个例躺进经验层、随聚合进了公共图谱（而那些坐标对
                # 下一段探索是假的，世界每轮重生）。
                # 只在两边层都已知且不同时拒（老库/种子库的层可能为空，那种情况不误报）。
                p_layer = _concept_layer(primary_slug, self._conn)
                s_layer = _concept_layer(secondary_slug, self._conn)
                if p_layer and s_layer and p_layer != s_layer:
                    return json.dumps(
                        {"ok": False,
                         "error": _TOOL_RESULT("merge_cross_layer", primary=primary_slug,
                                               p_layer=repr(p_layer), secondary=secondary_slug,
                                               s_layer=repr(s_layer))},
                        ensure_ascii=False)

                # ★ **整个合并必须是原子的**：它要动 facet + 边 + 概念行三张表，中途任何一步
                # 失败而没有回滚，会留下"facet 已删、概念还在、边搬了一半"这种修不回来的中间态
                # （悬挂事务还可能被下一个工具的 commit 顺手提交）。失败就整体回滚、原样报错。
                try:
                    self._merge_body(primary_slug, secondary_slug)
                except Exception as exc:  # pylint: disable=broad-except
                    self._conn.rollback()
                    logger.exception("merge_concepts 失败并已回滚：%s → %s",
                                     secondary_slug, primary_slug)
                    return json.dumps(
                        {"ok": False,
                         "error": _TOOL_RESULT("merge_failed_rolled_back", primary=primary_slug,
                                               secondary=secondary_slug, exc=exc)},
                        ensure_ascii=False)
            _unindex_concept(self._vec_store, secondary_slug)
            # ★ secondary 的 facet 已经搬到 primary 名下了（见 _merge_body），映射键跟着搬。
            #   同 kind 撞车时 `_merge_body` 是**拼接内容**、保留 primary 那条 —— 于是 primary
            #   已有的映射该赢，secondary 那条丢掉。这正是 `move_slug` 的语义（见 viewer 侧的
            #   回放：搬过去时不覆盖目标已有的键），所以这里不需要特殊处理。
            _move_facet_source(secondary_slug, primary_slug)
            # ★ secondary 的 facet 已搬到 primary 名下（`_merge_body` 里同 kind 是**拼接**），
            #   故 primary 的索引正文变了，必须重嵌；secondary 刚被 delete，不用管。
            #   ⚠️ merge 是**唯一**可能撞 `_FACET_MAX_CHARS` 的路径（拼接天然会变长）——
            #   它是有意放行的：合并的第一职责是不丢内容，不能因为撞上限就拒绝合并。
            #   超长由 `_guard_part_len` 截断并告警，随后由 Agent 用 update_facet 精炼。
            _index_concept(self._conn, self._vec_store, primary_slug,
                           part_max_chars=_PART_MAX_CHARS)
            return json.dumps({"ok": True, "kept_slug": primary_slug}, ensure_ascii=False)

        def _merge_body(self, primary_slug: str, secondary_slug: str) -> None:
            """合并的实际写库动作（调用方已持锁，并负责失败回滚）。"""
            secondary_facets = self._conn.execute(
                "SELECT kind, content FROM facet WHERE concept_slug = ?",
                (secondary_slug,),
            ).fetchall()
            # 同 kind 在 primary 已有时**拼接**而非并存：(concept_slug,kind) 是联合主键，
            # 一个概念的每种 kind 只有一条。拼接保证不丢内容，随后由 Agent 用 update_facet
            # 把拼出来的长文本精炼成一条（description 里已要求）。
            for kind, content in secondary_facets:
                existing = self._conn.execute(
                    "SELECT content FROM facet WHERE concept_slug = ? AND kind = ?",
                    (primary_slug, kind),
                ).fetchone()
                if existing:
                    merged_content = (f"{existing[0]}\n"
                                      f"{_TOOL_RESULT('merged_from', slug=secondary_slug)}{content}")
                    self._conn.execute(
                        "UPDATE facet SET content = ?, updated_at = ? "
                        "WHERE concept_slug = ? AND kind = ?",
                        (merged_content, self._round_num, primary_slug, kind),
                    )
                else:
                    self._conn.execute(
                        "INSERT INTO facet (concept_slug, kind, content, "
                        "created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                        (primary_slug, kind, content, self._round_num, self._round_num),
                    )
            self._conn.execute("DELETE FROM facet WHERE concept_slug = ?", (secondary_slug,))
            # ── 边的迁移：secondary 的边改指到 primary ──
            # ⚠️ **不能直接 UPDATE**：`edge` 有 `UNIQUE(from_slug, to_slug, relation)`，
            # 而"两个概念被同一个第三方以同一种关系指向"是**图里常见的形态**，例如
            #     wood_to_stick_chain --requires--> tech_tree_wood_stone_iron
            #     wood_to_stick_chain --requires--> torch_crafting_placement
            # 把后者并进前者，`UPDATE ... SET to_slug=primary` 就会撞上已存在的同一三元组，
            # 抛 IntegrityError 崩掉整个工具（此时 facet 已经 DELETE、概念行还在、边搬了一半，
            # 是个修不回来的中间态）。
            #
            # 正解：撞车的那条**直接删掉**（primary 已经有一条同 from/to/relation 的边了，
            # 两条表达的是同一个事实），其余的正常改指。
            for col, other in (("from_slug", "to_slug"), ("to_slug", "from_slug")):
                self._conn.execute(
                    f"DELETE FROM edge WHERE {col} = ? AND EXISTS ("
                    f"  SELECT 1 FROM edge t WHERE t.{col} = ? "
                    f"    AND t.{other} = edge.{other} AND t.relation = edge.relation)",
                    (secondary_slug, primary_slug),
                )
                # 剩下的没有撞车对象，直接改指
                self._conn.execute(
                    f"UPDATE edge SET {col} = ? WHERE {col} = ?",
                    (primary_slug, secondary_slug),
                )
            # 自环（a→b 的边在 b 并进 a 之后变成 a→a）直接删
            self._conn.execute("DELETE FROM edge WHERE from_slug = to_slug")
            # ★ secondary 的 facet/边都已搬进 primary → primary 被碰过，推进 updated_at（2026-08-25）。
            #   secondary 那行紧接着被 DELETE，回写它无意义。
            self._conn.execute(
                "UPDATE concept SET updated_at = ? WHERE slug = ?",
                (self._round_num, primary_slug),
            )
            self._conn.execute("DELETE FROM concept WHERE slug = ?", (secondary_slug,))
            self._conn.commit()

    class ManageEdgeTool(_BoundTool, BaseTool):
        name = "manage_edge"
        description = _TOOL_TEXT("manage_edge", "desc")
        # ★ **relation 不再是 enum**（2026-08-09，随关系词表放开）：由 Agent 自由命名，
        #   schema 只声明类型，合法性只剩卫生检查（`_label_invalid`）。
        #   `examples` 给出**图里已经用过**的关系名，让模型倾向复用而不是每次新造一个
        #   —— 它只是提示、不做拦截（真正把这件事说透的是 user_input 里那块 dump_used_vocab）。
        _relation_examples = [r for (r,) in conn.execute(
            "SELECT relation FROM edge WHERE relation IS NOT NULL AND relation != '' "
            "GROUP BY relation ORDER BY COUNT(*) DESC, relation")]
        _relation_schema = {
            "type": "string",
            "description": _TOOL_TEXT("manage_edge", "relation"),
            **({"examples": _relation_examples} if _relation_examples else {}),
        }
        _new_relation_schema = {
            "type": "string",
            "description": _TOOL_TEXT("manage_edge", "new_relation"),
            **({"examples": _relation_examples} if _relation_examples else {}),
        }
        parameters = {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["add", "delete", "update"]},
                "from_slug": {"type": "string"},
                "to_slug": {"type": "string"},
                "relation": _relation_schema,
                "new_relation": _new_relation_schema,
            },
            "required": ["action", "from_slug", "to_slug", "relation"],
        }

        def run(self, action: str = "", from_slug: str = "", to_slug: str = "", relation: str = "",
                 new_relation: str = "", **_kw) -> str:
            # `action` 的 enum 校验平时走不到（`BaseTool.__call__` 在调 run() 之前就按 schema
            # 跑 `validate_params`）。**保留**是因为它是绕过 registry 直接调 run() 时的最后一道
            # 防线（测试就这么调）。
            # ⚠️ 而 **relation 的校验现在是真的会走到这里**：它已不是 enum，schema 层不再拦
            # 任何字符串值，所以下面 `_label_invalid` 是 Agent 实际会看到的那道检查。
            if action not in ("add", "delete", "update"):
                return json.dumps({"ok": False, "error": _TOOL_RESULT("edge_action_invalid")},
                                  ensure_ascii=False)
            _err = _label_invalid(relation, what="relation")
            if _err:
                return json.dumps({"ok": False, "error": _err}, ensure_ascii=False)
            with self._lock:
                # 三种失败**分别报**：笼统一句 "from_slug/to_slug 不存在或相同" 会盖住三种完全
                # 不同的原因，模型既不知道该改哪个参数，也分不清是拼错了 slug 还是那个概念根本
                # 没建。反思 Agent 撞这个的概率不低 —— 它刚建完一批概念就要连边，`day_3` 还是
                # `day_03` 全靠记；撞一次就得多花一步 search_concepts 去猜，而反思有步数上限。
                # **把存在的那一端也说出来**，等于告诉它"另一头是对的，问题只在这一头"。
                if from_slug == to_slug:
                    return json.dumps(
                        {"ok": False, "error": _TOOL_RESULT("edge_self_loop", slug=repr(from_slug))},
                        ensure_ascii=False)
                missing = [s for s in (from_slug, to_slug) if not self._concept_exists(s)]
                if missing:
                    which = "from_slug" if missing[0] == from_slug else "to_slug"
                    other = to_slug if missing[0] == from_slug else from_slug
                    both = len(missing) == 2
                    return json.dumps(
                        {"ok": False,
                         "error": (_TOOL_RESULT("edge_both_missing", from_slug=repr(from_slug),
                                                to_slug=repr(to_slug))
                                   if both else
                                   _TOOL_RESULT("edge_one_missing", which=which,
                                                missing=repr(missing[0]), other=repr(other)))
                                  + _TOOL_RESULT("edge_missing_tail")},
                        ensure_ascii=False)

                if action == "add":
                    # 返回值区分「新建」和「早就有了」：模型能借此分清自己是**建了一条新关系**
                    # 还是**在重复劳动**。"已存在"是**纯幂等**——不累加任何计数，只是如实告诉
                    # 它这条边已经在图里了。
                    prev = self._conn.execute(
                        "SELECT 1 FROM edge "
                        "WHERE from_slug = ? AND to_slug = ? AND relation = ?",
                        (from_slug, to_slug, relation),
                    ).fetchone()
                    if prev is None:
                        self._conn.execute(
                            "INSERT INTO edge (id, from_slug, to_slug, relation, created_at) "
                            "VALUES (lower(hex(randomblob(16))), ?, ?, ?, ?)",
                            (from_slug, to_slug, relation, self._round_num),
                        )
                        # ★ 新建了一条边 → 两端概念都被碰过，推进 updated_at（2026-08-25）。
                        #   created=False（幂等，边早就在）那条不回写：没碰任何东西。
                        self._conn.execute(
                            "UPDATE concept SET updated_at = ? WHERE slug IN (?, ?)",
                            (self._round_num, from_slug, to_slug),
                        )
                        self._conn.commit()
                        return json.dumps({"ok": True, "created": True}, ensure_ascii=False)
                    return json.dumps(
                        {"ok": True, "created": False,
                         "note": _TOOL_RESULT("edge_exists_note", from_slug=from_slug,
                                              relation=relation, to_slug=to_slug)},
                        ensure_ascii=False)

                # delete / update 都要先确认这条边真的存在
                existing = self._conn.execute(
                    "SELECT 1 FROM edge WHERE from_slug = ? AND to_slug = ? AND relation = ?",
                    (from_slug, to_slug, relation),
                ).fetchone()
                if existing is None:
                    return json.dumps(
                        {"ok": False, "error": _TOOL_RESULT("edge_not_found", from_slug=from_slug,
                                                            to_slug=to_slug, relation=relation)},
                        ensure_ascii=False,
                    )

                if action == "delete":
                    self._conn.execute(
                        "DELETE FROM edge WHERE from_slug = ? AND to_slug = ? AND relation = ?",
                        (from_slug, to_slug, relation),
                    )
                    # ★ 删边也是碰了两端概念 → 推进 updated_at（2026-08-25）。
                    self._conn.execute(
                        "UPDATE concept SET updated_at = ? WHERE slug IN (?, ?)",
                        (self._round_num, from_slug, to_slug),
                    )
                    self._conn.commit()
                    return json.dumps({"ok": True}, ensure_ascii=False)

                # action == "update"
                if not new_relation or not new_relation.strip():
                    return json.dumps({"ok": False, "error": _TOOL_RESULT("edge_update_needs_new_relation")},
                                      ensure_ascii=False)
                # 同一套卫生检查（`new_relation` 同样不再是 enum）。
                # 空值单独报在上面那条：它的引导语不同（"update 需要提供 new_relation"
                # vs "这个标签不能为空"）。
                _err = _label_invalid(new_relation, what="new_relation")
                if _err:
                    return json.dumps({"ok": False, "error": _err}, ensure_ascii=False)
                if new_relation == relation:
                    return json.dumps({"ok": False, "error": _TOOL_RESULT("edge_new_relation_same")},
                                      ensure_ascii=False)
                target = self._conn.execute(
                    "SELECT 1 FROM edge WHERE from_slug = ? AND to_slug = ? AND relation = ?",
                    (from_slug, to_slug, new_relation),
                ).fetchone()
                if target is not None:
                    # 同方向的 new_relation 边已存在：直接删掉旧的那条，避开 UNIQUE 约束。
                    self._conn.execute(
                        "DELETE FROM edge WHERE from_slug = ? AND to_slug = ? AND relation = ?",
                        (from_slug, to_slug, relation),
                    )
                else:
                    self._conn.execute(
                        "UPDATE edge SET relation = ? WHERE from_slug = ? AND to_slug = ? AND relation = ?",
                        (new_relation, from_slug, to_slug, relation),
                    )
                # ★ 改了边的关系名 → 两端概念被碰过，推进 updated_at（2026-08-25）。
                self._conn.execute(
                    "UPDATE concept SET updated_at = ? WHERE slug IN (?, ?)",
                    (self._round_num, from_slug, to_slug),
                )
                self._conn.commit()
                return json.dumps({"ok": True}, ensure_ascii=False)

    class SearchConceptsTool(_BoundTool, BaseTool):
        name = "search_concepts"
        description = _TOOL_TEXT("search_concepts", "desc")
        parameters = {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": _TOOL_TEXT("search_concepts", "query")},
                # minimum/maximum 由 BaseTool.validate_params 在 run() 之前就拦下（_check_range）。
                # run() 里**仍然重复校验**：validate_params 只在经 `__call__` 进来时跑，
                # 而测试与内部调用直接调 run()，两处都拦才不留后门。
                "limit": {"type": "integer", "default": 10,
                          "minimum": 1, "maximum": _SEARCH_MAX_LIMIT},
            },
        }

        def run(self, query: str = "", limit: int = 10, **_kw) -> str:
            # ── limit 校验：10 个工具里它是唯一曾经裸传的参数 ──
            # schema 的 `"default": 10` 不是约束（模型显式传值时不生效），而两端越界都不是
            # "少给几条"这种良性降级：
            #   limit=0  → 向量分支拿到空 hits、SQL 分支 LIMIT 0 → 两边都空 → 被误读成"没记过"
            #   limit=-1 → SQL 分支 LIMIT -1 是**不限**（整个图谱倾泻进上下文）；
            #              向量分支让 sqlite-vec 抛 "k must be >= 0"，被 search() 的 broad
            #              except 吞掉变成空 → 同样误读成"没记过"
            # 所以**报错而不夹取**：夹取会让"我要了 0 条"和"图谱里没有"变得不可区分。
            try:
                limit = int(limit)
            except (TypeError, ValueError):
                limit = -1  # 非整数（模型偶尔传 "3"/"all"）统一走下面的越界报错
            if not 1 <= limit <= _SEARCH_MAX_LIMIT:
                return json.dumps(
                    {"results": [], "error": _TOOL_RESULT("search_limit_invalid", limit=limit,
                                                          max_limit=_SEARCH_MAX_LIMIT)},
                    ensure_ascii=False)
            with self._lock:
                scores: dict[str, float] = {}
                n_stale = 0  # 命中了但概念表里已不存在的 slug 数（向量库与主库漂移）
                if query:
                    # ★ 分块索引（2026-09-24）：过取**分片行** → 按概念融合 → 取前 limit 个**概念**。
                    #   不融合的话，一个概念占 1+N 行，limit 个名额会被少数几个概念的分片吃满。
                    hits = self._vec_store.search(query, top_k=limit * _OVERFETCH_MULT)
                    fused = fuse_part_hits(hits, w_identity=_W_IDENTITY, w_facet=_W_FACET,
                                           facet_agg=_FACET_AGG)[:limit]
                    slugs = [s for s, _ in fused]
                    # ★ 空 hits 有**两种完全不同**的成因，必须分开告知（2026-08-08 修）：
                    #   ① 图谱真的还没有任何概念 → "直接 create_concept"是对的
                    #   ② 向量通道不可用（embedder 未配 → NullEmbedder；sqlite-vec 加载失败；
                    #      embedding 服务 HTTP 挂 —— 后者被 MemoryVectorStore.search 的 broad
                    #      except 吞成 `[]`）→ 此时图谱里可能有几十个概念
                    # 此前两者共用"图谱还是空的，你还没记过任何东西"这一句，于是 ② 会**指示
                    # 反思 Agent 把已有概念全部重建一遍**。`COUNT(*)` 一句就能分开。
                    # （检索本身不做阈值过滤：库里有东西就一定给 top_k 条，即便完全无关也返回。
                    #   "这些都不相关"要靠 similarity 让模型自己看出来，不是靠这里返回空。）
                    if not slugs:
                        n_concepts = self._conn.execute(
                            "SELECT COUNT(*) FROM concept").fetchone()[0]
                        logger.warning(
                            "[search_concepts] query=%r limit=%d → 向量检索 0 命中"
                            "（图谱有 %d 个概念）%s",
                            query[:60], limit, n_concepts,
                            "" if not n_concepts else "—— 检索通道疑似不可用")
                        note = (_TOOL_RESULT("search_channel_down", n_concepts=n_concepts)
                                if n_concepts else _TOOL_RESULT("search_graph_empty"))
                        return json.dumps({"results": [], "note": note}, ensure_ascii=False)
                    scores = {s: round(sc, 3) for s, sc in fused}
                    placeholders = ",".join("?" * len(slugs))
                    rows = self._conn.execute(
                        f"SELECT slug, name, summary, layer FROM concept WHERE slug IN ({placeholders})",
                        tuple(slugs),
                    ).fetchall()
                    # ⚠️ **按 hits 的顺序重排**：`WHERE slug IN (...)` 不保证结果顺序跟 IN 列表
                    # 一致，直接用会让**向量检索的相关度排名被丢掉**——最相关的那条可能排到后面。
                    # 模型对列表靠前的项注意力更高，排序错乱等于把检索质量白扔。
                    by_slug = {r[0]: r for r in rows}
                    rows = [by_slug[s] for s in slugs if s in by_slug]
                    # ★ 被丢掉的 slug 要**数出来并说出去**：向量库与主库会真的漂移
                    # （del_concept 先删库、锁外才 _vec_store.delete()，失败只 warning；
                    #   create_concept 同样是 commit 之后才 upsert）。静默丢弃有两种后果：
                    #   部分丢 → limit=10 悄悄变 9，模型以为看到的就是全部 top-10；
                    #   全丢   → 空 results 配一句"按相关度排列"，自相矛盾且无法与"没记过"区分。
                    n_stale = len(slugs) - len(rows)
                    if n_stale:
                        logger.warning(
                            "[search_concepts] 向量库与主库漂移：%d/%d 条命中指向已不存在的概念 %s",
                            n_stale, len(slugs), [s for s in slugs if s not in by_slug][:5])
                    if not rows:
                        return json.dumps(
                            {"results": [],
                             "note": _TOOL_RESULT("search_all_stale", n_stale=n_stale)},
                            ensure_ascii=False)
                else:
                    # 不传 query = "列最近的几个"。ORDER BY 要带 created_at 兜底：
                    # **同一次反思里新建的概念 updated_at 都相同**（同一个 `at`），
                    # 只按它排等于任意序。
                    # ⚠️ 跨次反思之间现在是能分辨的：副脑的 `at` 是第几天（2026-08-08 起），
                    # 此前是主脑的 round、一条轨迹内恒定 → 整个排序都退化成 slug 字典序。
                    rows = self._conn.execute(
                        "SELECT slug, name, summary, layer FROM concept "
                        "ORDER BY updated_at DESC, created_at DESC, slug LIMIT ?",
                        (limit,),
                    ).fetchall()
                out = []
                for r in rows:
                    d = _concept_row_to_dict(r)
                    d["facet_kinds"] = _facet_kinds(self._conn, r[0])
                    d["relations"] = _concept_relations(self._conn, r[0])
                    # 相似度让模型自己判断"这条是真相关还是勉强凑数" —— 检索总会返回 top_k 条，
                    # 没有分数它无法区分 0.53 的强相关和 0.18 的噪音。
                    if r[0] in scores:
                        d["similarity"] = scores[r[0]]
                    out.append(d)
            # 结果按相关度从高到低排列，这件事要**说出来** —— 否则模型不知道第一条最相关。
            note = _TOOL_RESULT("search_note_ranked" if query else "search_note_recent")
            if n_stale:
                note += _TOOL_RESULT("search_note_stale_suffix", n_stale=n_stale)
            # 事后可诊断性：此前本工具**一行日志都不打**（对照 RetrieveMemoryTool 有
            # `[retrieve] … → 命中/无命中`）—— 上次追"检索恒 0 命中"时无法从日志判断
            # 反思 Agent 到底查了什么、命中没命中，只能靠改代码重跑。
            top_sim = out[0].get("similarity") if out else None
            logger.info("[search_concepts] query=%r limit=%d → %d 条%s%s",
                        query, limit, len(out),
                        "" if top_sim is None else f"（top similarity={top_sim}）",
                        f"，跳过 {n_stale} 条脏 slug" if n_stale else "")
            return json.dumps({"results": out, "note": note}, ensure_ascii=False)

    class GetConceptTool(_BoundTool, BaseTool):
        name = "get_concept"
        description = _TOOL_TEXT("get_concept", "desc")
        parameters = {"type": "object", "properties": {"concept_slug": {"type": "string"}}, "required": ["concept_slug"]}

        def run(self, concept_slug: str = "", **_kw) -> str:
            with self._lock:
                row = self._conn.execute(
                    "SELECT slug, name, summary, layer FROM concept WHERE slug = ?", (concept_slug,)
                ).fetchone()
                if row is None:
                    return json.dumps(
                        {"ok": False, "error": _TOOL_RESULT("concept_not_found", slug=concept_slug)},
                        ensure_ascii=False)
                facet_rows = self._conn.execute(
                    "SELECT kind, content FROM facet "
                    "WHERE concept_slug = ? ORDER BY kind",
                    (concept_slug,),
                ).fetchall()
                relations = _concept_relations(self._conn, concept_slug)
            d = _concept_row_to_dict(row)
            d["ok"] = True
            d["facets"] = [
                {"kind": kind, "content": content} for kind, content in facet_rows
            ]
            d["relations"] = relations
            return json.dumps(d, ensure_ascii=False)

    class DelFacetTool(_BoundTool, BaseTool):
        name = "del_facet"
        description = _TOOL_TEXT("del_facet", "desc")
        parameters = {
            "type": "object",
            "properties": {
                "concept_slug": {"type": "string"},
                "kind": _kind_schema(),
                # reason 的说明**必须给**：它是必填字段，若没有 description，模型收到一个
                # 没有说明的必填参数只能靠猜该填什么。
                "reason": {"type": "string", "description": _TOOL_TEXT("del_facet", "reason")},
            },
            "required": ["concept_slug", "kind", "reason"],
        }

        def run(self, concept_slug: str = "", kind: str = "", reason: str = "", **_kw) -> str:
            # kind 与 reason 校验先做：避免词表外的 kind 与"概念根本不存在"都只回一句
            # "XX 下没有 YY facet"，让模型以为是 kind 写错了。
            _err = _kind_invalid(kind) or _reason_invalid(reason)
            if _err:
                return json.dumps({"ok": False, "error": _err}, ensure_ascii=False)
            with self._lock:
                if not self._concept_exists(concept_slug):
                    return json.dumps(
                        {"ok": False,
                         "error": _TOOL_RESULT("del_facet_concept_missing", slug=repr(concept_slug))},
                        ensure_ascii=False)
                if not self._facet_exists(concept_slug, kind):
                    return json.dumps(
                        {"ok": False,
                         "error": _TOOL_RESULT("del_facet_kind_missing", slug=concept_slug, kind=kind,
                                               kinds=_facet_kinds(self._conn, concept_slug)
                                               or _TOOL_RESULT("filler_no_facets"))},
                        ensure_ascii=False)
                # 留痕**在删之前**：日志是删除的唯一凭证，写日志本身若抛异常，放在删之后就会
                # 留下"删了但没留痕、事务还悬着"的半状态。先记后删：最坏情况是记了一条没发生
                # 的删除，比反过来安全得多。
                logger.info("del_facet: %s/%s 即将删除，理由: %s", concept_slug, kind, reason)
                self._conn.execute(
                    "DELETE FROM facet WHERE concept_slug = ? AND kind = ?", (concept_slug, kind)
                )
                # ★ 删 facet 也是「碰了概念」→ 推进父 concept.updated_at（2026-08-25）。
                #   删最后一条 facet 会留空壳，那是另一回事（见下方 warning）；updated_at 照推进：
                #   这个概念刚被活动过，滑动窗口的语义是「最近被碰过的优先留」。
                self._conn.execute(
                    "UPDATE concept SET updated_at = ? WHERE slug = ?",
                    (self._round_num, concept_slug),
                )
                self._conn.commit()
                left = _facet_kinds(self._conn, concept_slug)
            # 这条 facet 没了 → 它的录像片段是假证据，让映射失效（见 _drop_facet_source）
            _drop_facet_source(concept_slug, kind)
            # ★ 只删这一条 facet 分片（**不触发 embed**：删除不需要重算任何向量，
            #   旧口径整概念重嵌会白白打一次 embedding HTTP）。空壳时退化成只剩 identity 分片，
            #   这正是我们要的 —— 空壳不该再靠一条已不存在的 facet 参与检索。
            _unindex_facet(self._vec_store, concept_slug, kind)
            out: dict = {"ok": True, "remaining_kinds": left}
            # ★ **空壳概念要警告**（工具侧警告，不在检索侧静默过滤）：删掉最后一条 facet 会
            # 留下一个"有身份、没内容"的概念，而它**照样在向量库里**，可能抢占 anchor 名额、
            # 挤掉真正有内容的概念；Agent 拿它的 slug 去 expand 只会得到一个空节点。
            # 不在检索侧过滤，是因为那样 Agent 永远不知道自己造了个空壳。
            if not left:
                out["warning"] = _TOOL_RESULT("del_facet_now_empty", slug=concept_slug)
                logger.warning("del_facet: %s 已无任何 facet —— 空壳概念会污染检索候选",
                               concept_slug)
            return json.dumps(out, ensure_ascii=False)

    class DelConceptTool(_BoundTool, BaseTool):
        name = "del_concept"
        description = _TOOL_TEXT("del_concept", "desc")
        parameters = {
            "type": "object",
            "properties": {
                "concept_slug": {"type": "string"},
                "reason": {"type": "string", "description": _TOOL_TEXT("del_concept", "reason")},
            },
            "required": ["concept_slug", "reason"],
        }

        def run(self, concept_slug: str = "", reason: str = "", **_kw) -> str:
            _err = _reason_invalid(reason)
            if _err:
                return json.dumps({"ok": False, "error": _err}, ensure_ascii=False)
            with self._lock:
                if not self._concept_exists(concept_slug):
                    return json.dumps(
                        {"ok": False,
                         "error": _TOOL_RESULT("del_concept_not_found", slug=repr(concept_slug))},
                        ensure_ascii=False)
                # ★ **先数清要连带删掉什么**：级联删除是对的（不留孤儿），但如果不告知，删掉
                # `day_2` 会让 `day_1→day_2→day_3` 的时间线断成两截、`day_1` 与 `day_3` 再无
                # 连接，而 Agent 以为自己只删了一个概念。
                # 分开数「别的概念指向它」与「它指向别的概念」：前者才是**破坏了别人的结构**，
                # 需要它考虑补一条边（比如把 day_1 直接 precedes 到 day_3）。
                inbound = self._conn.execute(
                    "SELECT from_slug, relation FROM edge WHERE to_slug = ?",
                    (concept_slug,),
                ).fetchall()
                outbound = self._conn.execute(
                    "SELECT relation, to_slug FROM edge WHERE from_slug = ?",
                    (concept_slug,),
                ).fetchall()
                n_facets = len(_facet_kinds(self._conn, concept_slug))
                # 留痕在删之前，理由同 del_facet。
                logger.info("del_concept: %s 即将删除（连带 %d 条 facet / %d 条入边 / %d 条出边），理由: %s",
                            concept_slug, n_facets, len(inbound), len(outbound), reason)
                # facet 与边**显式删**，不依赖 FK cascade：cascade 现在确实开了
                # （graph.py 的 `PRAGMA foreign_keys=ON`），但它只覆盖 facet 和
                # 以本概念为端点的 edge —— 而 `PRAGMA foreign_keys` 是**连接级**的，
                # 任何一处新建连接漏掉这行，删除就会静默退化成留孤儿（孤儿边还会渲染进 prompt，
                # 指向一个不存在的概念）。显式删让本工具的行为不依赖连接设置，cascade 只当
                # 第二道保险。
                self._conn.execute("DELETE FROM facet WHERE concept_slug = ?", (concept_slug,))
                self._conn.execute(
                    "DELETE FROM edge WHERE from_slug = ? OR to_slug = ?",
                    (concept_slug, concept_slug),
                )
                self._conn.execute("DELETE FROM concept WHERE slug = ?", (concept_slug,))
                self._conn.commit()
            # ★ 删**全部**分片（identity + 每条 facet），只删 identity 会留下 N 条孤儿 facet 分片。
            _unindex_concept(self._vec_store, concept_slug)
            # 概念连带全部 facet 都没了 → 整个概念的录像映射失效（kind 留空 = 全部）
            _drop_facet_source(concept_slug)
            out: dict = {"ok": True, "deleted_facets": n_facets,
                         "deleted_edges": len(inbound) + len(outbound)}
            if inbound:
                # 只有入边需要它回头看：那些是**别的概念**原先指向这里的结构，
                # 删掉之后那几个概念之间可能就断了（时间线是最典型的例子）。
                out["warning"] = _TOOL_RESULT(
                    "del_concept_inbound_warning", slug=concept_slug,
                    edges="、".join(f"{f} --{r}--> {concept_slug}" for f, r in inbound[:5]),
                    more=(_TOOL_RESULT("del_concept_inbound_more", n=len(inbound))
                          if len(inbound) > 5 else ""))
            return json.dumps(out, ensure_ascii=False)

    # ═══════════════════════════════════════════════════════════════════════════
    # 技能（可直接复用的 JS 代码）—— 与文本经验 facet 平行的另一类产出
    # ═══════════════════════════════════════════════════════════════════════════
    # facet.content 存的是「文本经验」（间接引导 Agent 决策）；skill 存的是「可直接调用
    # 的 JS 代码」（执行期 retrieve 命中后拼进 programs，Agent 的 write_code 代码里
    # `await skillX(bot, ...)` 直接可用，与 mineBlock 原语同机制）。详见
    # plans/reflection_js_skills.md。
    #
    # ★ **产出门槛在 prompt 端**（reflect.yaml 引导"只存当天执行成功且达成意图的"），
    #   工具不猜意图是否达成 —— 它只做结构校验（js_code 必须含 async function、
    #   signature 与函数名一致）。verified 字段默认 1。
    # ★ **向量检索**：skill.intent 走与 concept 同一个 vec_store，biz_id 用 `skill:`
    #   前缀与 concept 区分（_SKILL_VEC_PREFIX）。retrieve_skills 按 query 检索后过滤
    #   `skill:` 前缀的命中。
    _SKILL_VEC_PREFIX = "skill:"

    def _js_func_name(js_code: str) -> str | None:
        """从 js_code 里抽 `async function NAME` 的 NAME。抽不到返回 None。

        用于校验 signature 与函数名一致 —— 注入 programs 后 Agent 按 signature 调用，
        函数名对不上就调不到。也是跨臂撞名的判别依据（两臂同名函数会注入互相覆盖）。
        """
        import re
        m = re.search(r"async\s+function\s+([A-Za-z_$][\w$]*)", js_code or "")
        return m.group(1) if m else None

    def _js_syntax_invalid(js_code: str) -> str | None:
        """JS 语法真解析（acorn，经 node 子进程）。返回编译报错原文；None 表示合法。

        ★ 0914 加（Invalid or unexpected token 白烧事件）：LLM 会生成含隐形 Unicode 字符
          的代码（实测 erin 技能《深部上行脱困》第12行把 ASCII `-`(U+002D) 写成数学负号
          `−`(U+2212)），旧判据（含 `async function` 即可）拦不住。这种代码一旦入库并
          全量注入 programs，**每一步 write_code 的 eval 都抛 Invalid token**——Agent 的
          代码本身语法是对的，它只会不停换写法重试，token 白烧（实测一段 963 次报错）。
        ★ 拒收信息只回编译报错原文（用户定 0914），不加提示词。
        acorn 复用 mc_server/node_modules 里现成的（index.js 的 injectYieldIntoLoops 同款
        依赖），node 一次性调用；node/acorn 不可用时退回 None（不阻断入库主流程，注入层
        还有第二道防线，见 domains/minecraft/__init__.py _inject_all_skills）。
        解析选项与 index.js evaluateCode 的 eval 环境对齐：顶层 await 合法
          （allowAwaitOutsideFunction），代码会被包进 async IIFE。
        """
        code = (js_code or "").strip()
        if not code:
            return None  # 非空判据在 _skill_js_invalid，这里只管语法
        import shutil
        import subprocess
        node = shutil.which("node")
        if not node:
            return None
        acorn = _PKG_MC_SERVER / "node_modules" / "acorn"
        if not (acorn / "package.json").exists():
            return None
        # 只传文件路径不传代码本体：代码含任意引号/反斜杠，走 argv 有注入与转义两层坑
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as f:
            f.write(code)
            tmp = f.name
        try:
            r = subprocess.run(
                [node, "-e",
                 "const a=require(process.argv[1]);"
                 "try{a.parse(require('fs').readFileSync(process.argv[2],'utf8'),"
                 "{ecmaVersion:2022,allowAwaitOutsideFunction:true});}"
                 "catch(e){console.error(e.message);process.exit(1)}",
                 str(acorn), tmp],
                capture_output=True, text=True, timeout=15)
        except (subprocess.TimeoutExpired, OSError):
            return None  # 环境问题不拦业务（注入层还有第二道防线）
        finally:
            import os
            os.unlink(tmp)
        if r.returncode != 0:
            err = (r.stderr or "").strip().splitlines()
            detail = err[0] if err else "语法错误"
            return f"技能代码 JS 语法错误（acorn 解析失败：{detail}）"  # 用户定稿 0914 格式
        return None

    def _skill_js_invalid(js_code: str) -> str | None:
        """js_code 结构校验。返回错误信息；None 表示合法。

        判据：① 非空 ② 含 `async function` 定义（注入 programs 后才能被 await 调用）
        ③ acorn 语法解析通过（0914 加，防隐形 Unicode 字符入库——见 _js_syntax_invalid）。
        """
        code = (js_code or "").strip()
        if not code:
            return "技能代码不能为空（js_code）"
        if "async function" not in code:
            return ("技能代码必须含 `async function` 定义（如 `async function mineIron(bot, n){...}`），"
                    "执行期才会被注入 programs 供 Agent 的代码 await 调用")
        return _js_syntax_invalid(code)

    def _skill_sig_invalid(signature: str, js_code: str) -> str | None:
        """signature 与 js_code 函数名一致性校验。"""
        sig = (signature or "").strip()
        if not sig:
            return "调用签名不能为空（signature，如 mineIron(bot, count=5)）"
        fname = _js_func_name(js_code)
        if fname and not sig.startswith(fname + "("):
            return (f"签名 {sig!r} 与代码里的函数名 `{fname}` 不一致 —— "
                    f"执行期 Agent 按签名调用，对不上就调不到。签名应形如 `{fname}(bot, ...)`")
        return None

    # ★ 技能工具（2026-08-27 重构）：技能不再存 ArmGraph/公有脑，改存各臂常驻 SkillStore。
    #   这些工具绑**本臂 skill_store**（不是 conn/ArmGraph），create_skill 还跨臂查重防同名。
    #   无 skill_store（旧调用/非反思 Agent）→ 不注册技能工具，concept 工具照常。
    #   工具类不继承 _BoundTool（它绑的是 concept 库的 conn），直接用 self._store / self._registry。
    #   description/参数文案走 `_SKILL_TOOL_TEXT`（reflect.yaml tools 段）；schema（type/required/
    #   enum）留在这里——与 concept 工具同款分工：yaml 只存模型可见文案，结构在代码。
    class ListSkillsTool(BaseTool):
        """列出**本臂**全部技能现状（slug/签名/意图/成功失败计数/是否验证）。

        ★ 反思期看自己的全量（本臂 skill 库只有自己写的，几个量级，全量列最直接、最准——
          判断"今天又造了已有的吗"看全量避免漏判重复 create）。
        ★ 要看别的臂的技能用 search_skills（跨臂检索）。
        """
        name = "list_skills"
        description = _SKILL_TOOL_TEXT("list_skills", "desc")
        parameters = {"type": "object", "properties": {}}

        def __init__(self, store, **_kw):
            super().__init__()
            self._store = store

        def run(self, **_kw) -> str:
            briefs = self._store.all_skills_brief()
            results = [{"slug": b["slug"], "name": b["name"], "intent": b["intent"],
                        "signature": b["signature"], "preconditions": b["preconditions"],
                        "success_count": b["success_count"], "fail_count": b["fail_count"],
                        "verified": b["verified"]} for b in briefs]
            return json.dumps({"results": results, "n": len(results)}, ensure_ascii=False)

    class CreateSkillTool(BaseTool):
        name = "create_skill"
        description = _SKILL_TOOL_TEXT("create_skill", "desc")
        parameters = {
            "type": "object",
            "properties": {
                "slug": {"type": "string",
                         "description": _SKILL_TOOL_TEXT("create_skill", "slug")},
                "name": {"type": "string",
                         "description": _SKILL_TOOL_TEXT("create_skill", "name")},
                "intent": {"type": "string",
                           "description": _SKILL_TOOL_TEXT("create_skill", "intent")},
                "js_code": {"type": "string",
                            "description": _SKILL_TOOL_TEXT("create_skill", "js_code")},
                "signature": {"type": "string",
                              "description": _SKILL_TOOL_TEXT("create_skill", "signature")},
                "preconditions": {"type": "string",
                                  "description": _SKILL_TOOL_TEXT("create_skill", "preconditions")},
                "source_steps": {"type": "array", "items": {"type": "integer"},
                                 "description": _SKILL_TOOL_TEXT("create_skill", "source_steps")},
            },
            "required": ["slug", "name", "intent", "js_code", "signature"],
        }

        def __init__(self, store, registry, round_num, **_kw):
            super().__init__()
            self._store = store
            self._registry = registry
            self._round_num = round_num

        def run(self, slug: str = "", name: str = "", intent: str = "",
                js_code: str = "", signature: str = "", preconditions: str = "",
                source_steps=None, **_kw) -> str:
            _err = _slug_invalid(slug) or _skill_js_invalid(js_code) or \
                _skill_sig_invalid(signature, js_code)
            if _err:
                return json.dumps({"ok": False, "error": _err}, ensure_ascii=False)
            if not (name or "").strip():
                return json.dumps({"ok": False, "error": "技能名不能为空（name）"}, ensure_ascii=False)
            if not (intent or "").strip():
                return json.dumps({"ok": False, "error": "意图不能为空（intent）"}, ensure_ascii=False)
            final_slug = _slugify(slug)
            # ★ **跨臂查重（从源头防同名）**：函数名和 slug 全员唯一。两个臂都写 mineIron 会
            #   注入 programs 互相覆盖、Agent 调不到。撞了就拒，告诉作者是谁——让作者改函数名。
            #   exclude_arm=本臂：本臂已有同名（自己更新）走 update_skill，不在这挡。
            _my_arm = getattr(self._store, "arm_key", None)
            fname = _js_func_name(js_code)
            if self._registry is not None:
                taker = self._registry.funcname_taken(fname, exclude_arm=_my_arm) \
                    or self._registry.slug_taken(final_slug, exclude_arm=_my_arm)
                if taker:
                    return json.dumps(
                        {"ok": False,
                         "error": f"函数名 `{fname}` / slug `{final_slug}` 已被别的用户（{taker}）占用"},
                        ensure_ascii=False)
            ok, msg = self._store.create_skill(
                final_slug, name.strip(), intent.strip(), js_code, signature,
                preconditions, at=self._round_num)
            if not ok:
                return json.dumps({"ok": False, "error": msg}, ensure_ascii=False)
            # ★ 落盘「这个技能产自哪几步」（与 facet 同构，见 _record_skill_source）。
            #   source_steps 由反思 Agent 自报、schema 收了原本却没用——这里是补上的接线。
            #   update_skill 不碰来源步（用户定案：来源固定指向诞生现场），delete_skill 走 _drop。
            _record_skill_source(final_slug, source_steps)
            # ★ **语义查重警告（补精确撞名那道门管不到的缺口）**：funcname_taken/slug_taken 只挡
            #   「不同臂、同函数名/slug」；两个臂写**不同函数名但同一意图**的技能（如 mineIron 与
            #   smeltOreToIngot）能绕过去、悄悄堆出语义重复技能。这里按 intent 跨臂语义检索，超阈值的
            #   已有技能塞进返回 `warning`——**只警告不拦**（语义相似度是模糊的，硬拦易误伤同领域
            #   合理分工；与 del_facet 的空壳警告同一套路），Agent 看到 warning 可自行 delete_skill 纠偏。
            # threshold 取 SkillRegistry 持有的 SkillConfig（brain 构造时注入）；无 config（旧调用/
            #   对照组 NullSkillStore）→ 跳过警告、创建照常。
            out: dict = {"ok": True, "slug": final_slug, "signature": signature}
            try:
                cfg = getattr(self._registry, "_skill_config", None)
                if self._registry is not None and cfg is not None:
                    sim = self._registry.find_similar(
                        intent.strip(), cfg.similar_warn_threshold,
                        exclude_arm=_my_arm, limit=3)
                    # 双保险排除自己：exclude_arm 已跳过本臂，但若跨臂恰好有同 slug 也不该算。
                    sim = [s for s in sim if s["slug"] != final_slug]
                    if sim:
                        hits = "、".join(
                            f"{s['slug']}(来自{s['from_user']}，相似度{s['score']:.2f})"
                            for s in sim)
                        out["warning"] = _TOOL_RESULT(
                            "create_skill_similar_warning", hits=hits)
                        logger.info("[create_skill] %s 与已有技能语义相似：%s",
                                    final_slug, hits)
            except Exception:  # pylint: disable=broad-except
                # 警告是旁路：检索/读配置任何失败都绝不影响创建本身（同 _record_facet_source
                # 吞异常的作风，见本文件 L87-109）。
                logger.warning("[create_skill] 相似技能警告检索失败，已跳过（不影响创建）",
                               exc_info=False)
            out["note"] = ("已存进你的技能库（verified=1）。")
            return json.dumps(out, ensure_ascii=False)

    class UpdateSkillTool(BaseTool):
        name = "update_skill"
        description = _SKILL_TOOL_TEXT("update_skill", "desc")
        parameters = {
            "type": "object",
            "properties": {
                "slug": {"type": "string",
                         "description": _SKILL_TOOL_TEXT("update_skill", "slug")},
                "js_code": {"type": "string",
                            "description": _SKILL_TOOL_TEXT("update_skill", "js_code")},
                "signature": {"type": "string",
                              "description": _SKILL_TOOL_TEXT("update_skill", "signature")},
                "intent": {"type": "string",
                           "description": _SKILL_TOOL_TEXT("update_skill", "intent")},
                "name": {"type": "string",
                         "description": _SKILL_TOOL_TEXT("update_skill", "name")},
                "preconditions": {"type": "string",
                                  "description": _SKILL_TOOL_TEXT("update_skill", "preconditions")},
            },
            "required": ["slug", "js_code", "signature"],
        }

        def __init__(self, store, registry, round_num, **_kw):
            super().__init__()
            self._store = store
            self._registry = registry
            self._round_num = round_num

        def run(self, slug: str = "", js_code: str = "", signature: str = "",
                intent: str = "", name: str = "", preconditions: str = "",
                **_kw) -> str:
            _err = _skill_js_invalid(js_code) or _skill_sig_invalid(signature, js_code)
            if _err:
                return json.dumps({"ok": False, "error": _err}, ensure_ascii=False)
            final_slug = _slugify(slug)
            # 函数名若变了，新名仍须跨臂唯一（exclude_arm=本臂：改自己已有的不算撞）
            _my_arm = getattr(self._store, "arm_key", None)
            fname = _js_func_name(js_code)
            if self._registry is not None and fname:
                taker = self._registry.funcname_taken(fname, exclude_arm=_my_arm)
                if taker:
                    return json.dumps(
                        {"ok": False,
                         "error": f"新函数名 `{fname}` 已被别的用户（{taker}）占用"},
                        ensure_ascii=False)
            ok, msg = self._store.update_skill(
                final_slug, name=name if (name or "").strip() else None,
                intent=intent if (intent or "").strip() else None,
                js_code=js_code, signature=signature,
                preconditions=preconditions if preconditions is not None else None,
                at=self._round_num)
            if not ok:
                return json.dumps({"ok": False, "error": msg}, ensure_ascii=False)
            return json.dumps({"ok": True, "slug": final_slug, "signature": signature},
                              ensure_ascii=False)

    class DemoteSkillTool(BaseTool):
        """把你**自己**某个技能下线（verified=0，不再注入/检索）。

        ★ **止损用**：反思时看到某技能反复失败、一时改不好——下线它比让它明天继续被注入、
          继续浪费执行预算强。改好后用 update_skill 重写会重新 verified。
        ★ 只动本臂技能（store.demote 只作用于本臂 SkillStore）；他臂技能会返回"不在本臂"，
          提示用文本经验记提醒、别用 update/demote 动别人的。
        """
        name = "demote_skill"
        description = _SKILL_TOOL_TEXT("demote_skill", "desc")
        parameters = {
            "type": "object",
            "properties": {
                "slug": {"type": "string",
                         "description": _SKILL_TOOL_TEXT("demote_skill", "slug")},
            },
            "required": ["slug"],
        }

        def __init__(self, store, registry, round_num, **_kw):
            super().__init__()
            self._store = store
            self._registry = registry  # noqa: SLF001  与兄弟工具同构（demote 不需查重，留位对称）
            self._round_num = round_num

        def run(self, slug: str = "", **_kw) -> str:
            final_slug = _slugify(slug)
            ok, msg = self._store.demote(final_slug, at=self._round_num)
            if not ok:
                return json.dumps(
                    {"ok": False, "error": msg},
                    ensure_ascii=False)
            return json.dumps(
                {"ok": True, "slug": final_slug,
                 "note": "已下线（verified=0）。"},
                ensure_ascii=False)

    class DeleteSkillTool(BaseTool):
        """把你**自己**某个技能硬删除（删行+删向量，释放 slug/函数名，不可恢复）。

        ★ 阶段1 专属纠偏：你刚 create_skill 建错了/写歪了/函数名撞了又不想改 —— 删掉重建比
          留个废技能占着名字槽强（demote 是软下线、仍占名，这里要的是彻底抹掉）。
        ★ 与 demote 的分工：demote（阶段2）给成熟技能软下线（保留槽位防撞、可 update 恢复）；
          delete 给刚建的废技能硬抹除（释放名字、不可恢复，别人可 create 同名）。
        ★ 只动本臂技能；他臂技能返回"不在本臂"，提示用文本经验记提醒、别动别人的。
        """
        name = "delete_skill"
        description = _SKILL_TOOL_TEXT("delete_skill", "desc")
        parameters = {
            "type": "object",
            "properties": {
                "slug": {"type": "string",
                         "description": _SKILL_TOOL_TEXT("delete_skill", "slug")},
                "reason": {"type": "string",
                           "description": _SKILL_TOOL_TEXT("delete_skill", "reason")},
            },
            "required": ["slug", "reason"],
        }

        def __init__(self, store, round_num, **_kw):
            super().__init__()
            self._store = store
            self._round_num = round_num

        def run(self, slug: str = "", reason: str = "", **_kw) -> str:
            # reason 是删除的唯一凭证（删掉的东西不留副本，事后想知道"这技能为什么没了"只有它），
            # 与 del_concept/del_facet 同一套校验（_reason_invalid 也拦"就一两个字"的噪音理由）。
            _err = _reason_invalid(reason)
            if _err:
                return json.dumps({"ok": False, "error": _err}, ensure_ascii=False)
            final_slug = _slugify(slug)
            logger.info("delete_skill: %s 即将硬删除，理由: %s", final_slug, reason)
            ok, msg = self._store.delete_skill(final_slug)
            if not ok:
                return json.dumps(
                    {"ok": False, "error": msg},
                    ensure_ascii=False)
            # 技能已抹除，来源步指向的是不存在的技能——失效（同 del_facet → _drop_facet_source）。
            _drop_skill_source(final_slug)
            return json.dumps(
                {"ok": True, "slug": final_slug,
                 "note": "已彻底删除。"},
                ensure_ascii=False)

    class SearchSkillsTool(BaseTool):
        """跨臂检索所有臂的技能（按意图向量语义匹配），结果标来源臂 + 成功/失败计数。

        ★ **全员共享**：检索所有臂的技能库（不只是自己的），每条带 from_user + 计数——
          让你判断"这条是不是我写的、要不要维护"，以及"是不是已经有人解决过这类问题"。
        ★ 不去重：两个臂同意图的技能各留各的，都返回（计数帮你判断该用哪个/要不要改进自己的）。
        ★ 不传 query 列所有臂最近创建的。
        """
        name = "search_skills"
        description = _SKILL_TOOL_TEXT("search_skills", "desc")
        parameters = {
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": _SKILL_TOOL_TEXT("search_skills", "query")},
                "limit": {"type": "integer", "default": 10, "minimum": 1, "maximum": _SEARCH_MAX_LIMIT},
            },
        }

        def __init__(self, registry, **_kw):
            super().__init__()
            self._registry = registry

        def run(self, query: str = "", limit: int = 10, **_kw) -> str:
            try:
                limit = int(limit)
            except (TypeError, ValueError):
                limit = -1
            if not 1 <= limit <= _SEARCH_MAX_LIMIT:
                return json.dumps({"results": [], "error": f"limit 越界：{limit}"}, ensure_ascii=False)
            if (query or "").strip():
                hits = self._registry.retrieve_all(query, limit=limit)
                if not hits:
                    briefs = self._registry.all_briefs()
                    return json.dumps(
                        {"results": [],
                         "note": ("还没有任何技能" if not briefs else
                                  f"共 {len(briefs)} 个技能，向量检索未命中")},
                        ensure_ascii=False)
                results = [{"slug": h["slug"], "name": h["name"], "intent": h["intent"],
                            "signature": h["signature"], "preconditions": h.get("preconditions"),
                            "from_user": h["from_user"],
                            "success_count": h.get("success_count", 0),
                            "fail_count": h.get("fail_count", 0)} for h in hits]
            else:
                briefs = self._registry.all_briefs()[:limit]
                results = [{"slug": b["slug"], "name": b["name"], "intent": b["intent"],
                            "signature": b["signature"], "preconditions": b["preconditions"],
                            "from_user": b["from_user"],
                            "success_count": b["success_count"],
                            "fail_count": b["fail_count"],
                            "verified": b["verified"]} for b in briefs]
            return json.dumps({"results": results}, ensure_ascii=False)

    # ★ 10 个 concept/facet/edge 工具（与 vocab 无关，反思/聚合都用）。
    concept_tools = [
        CreateConceptTool(conn, vec_store, lock, round_num),
        UpdateConceptTool(conn, vec_store, lock, round_num),
        MergeConceptsTool(conn, vec_store, lock, round_num),
        DelConceptTool(conn, vec_store, lock, round_num),
        SearchConceptsTool(conn, vec_store, lock, round_num),
        GetConceptTool(conn, vec_store, lock, round_num),

        AddFacetTool(conn, vec_store, lock, round_num),
        UpdateFacetTool(conn, vec_store, lock, round_num),
        DelFacetTool(conn, vec_store, lock, round_num),

        ManageEdgeTool(conn, vec_store, lock, round_num),
    ]
    # ★ 技能工具（绑本臂 SkillStore + registry 跨臂查重/检索）。无 skill_store → 空。
    #   两个反思阶段**分工不同**（见下 if-vocab 分支），故这里先各成一组：
    #   - **建技能组**（create / list / search）：阶段1 专属。阶段1 手里有当天完整轨迹 +
    #     「今天真的跑通了」的事实，`create_skill` 的产出门槛（只存当天执行成功且达成意图的代码）
    #     只有它判断得了。list/search 是 create 的前置查重，跟着留。
    #   - **建技能纠偏组**（delete）：阶段1 专属。阶段1 没有 update_skill（维护归阶段2），
    #     建错/写废/撞名后唯一的纠正手段是删掉重建 → 需要硬删除（删行+删向量、释放 slug/函数名，
    #     不可恢复）。它和 demote 的分工：delete 抹掉刚建的废技能（释放名字）；demote（阶段2）
    #     软下线成熟技能（保留槽位防撞、可恢复）。
    #   - **维护技能组**（update / demote）：阶段2 专属。要不要改/下线一个技能，判据是它的
    #     **跨天累计成败 + 本次失败原因**（诊断块只拼给阶段2，阶段1 user_input 里没有）。
    #     让没诊断数据的阶段1 凭单次印象 update/demote，会把累计稳定的技能误下线、或把阶段2
    #     刚校准的代码又覆盖回去 —— 信息不全的会话不该做需要全量信息的决策。所以阶段1 不给这俩。
    skill_create_tools = []
    skill_delete_tools = []
    skill_maintain_tools = []
    if skill_store is not None:
        skill_create_tools = [
            CreateSkillTool(skill_store, skill_registry, round_num),
            ListSkillsTool(skill_store),
            SearchSkillsTool(skill_registry),
        ]
        skill_delete_tools = [
            DeleteSkillTool(skill_store, round_num),
        ]
        skill_maintain_tools = [
            UpdateSkillTool(skill_store, skill_registry, round_num),
            DemoteSkillTool(skill_store, skill_registry, round_num),
        ]

    if vocab == "reflect":
        # ★ 反思词表（阶段1）：concept 工具 + **建技能组**（create/list/search）+ **建技能纠偏**（delete）。
        #   不带 update/demote——技能的改进与下线归阶段2（见 skill_maintenance 分支），阶段1 没有
        #   累计失败诊断数据、不该做需要全量信息的维护决策。但阶段1 没有 update_skill，建错时需要
        #   delete 纠偏（删掉重建），所以 delete 留在阶段1。经验/记忆反思 + 顺带建技能，仍是阶段1。
        out = concept_tools + skill_create_tools + skill_delete_tools
    elif vocab == "skill_maintenance":
        # ★ 技能维护词表（2026-08-29，反思阶段2）：**仅**技能工具，且是**全量 5 个**
        #   （建技能组 + 维护技能组，无 delete——阶段2 有 update_skill 做纠偏，删废技能用不上硬删除）。
        #   阶段2 有累计成败 + 本次失败原因诊断块，负责 update/demote，也偶尔补建（create）。
        #   无 concept 工具、无 skill_store → 空。
        out = skill_create_tools + skill_maintain_tools
    elif vocab == "aggregate":
        # ★ 聚合词表**不再有技能工具**（2026-08-27）：技能由各臂自己维护（谁写的谁维护），
        #   聚合只归并 concept/facet/edge 经验。
        out = concept_tools
    else:
        out = concept_tools
    return out
