"""聚合编排模块（任务域无关）。

把"从**一次聚合窗口**的 N 份**私有记忆**里提炼经验、沉淀进公共经验图谱"这段编排收在这。
**聚合引擎本体在 openkage.memory.graph.BrainGraph.consolidate**（聚合 Agent = 跟副脑反思
Agent 同构的 ReActAgent）—— 本模块只负责：①统计前后 concept/facet/edge 计数
②拼 user_input（build_aggregate_input）③调主脑 .consolidate ④把计数变化 + Agent 一句话
总结留痕。裁决/执行不分离——Agent 就是执行者。

★ **一次聚合 = 一个 (epoch, batch, window)**（2026-08-09 加 window 维）：
- `batch` 是**题间**聚合（切题目列表），math 用它；
- `window` 是**题内**聚合（切一条轨迹的内部），minecraft 用它 —— 一条 365 夜的轨迹每 K 夜
  聚合一次，副脑经验层规模因此稳定在「K 夜的产出」而不是「整条轨迹的产出」。
两者正交，round 号编码见 `openkage.roundcode`（编码/解码的唯一真相，别自己写乘法）。

★★ **聚合看不到私有记忆** ★★
反思 Agent 自己同时写两层（memory 层只给明天的自己；experience 层是它归纳出的、可共享的），
聚合**只读 experience 层**（`_dump_arm_graph(g, LAYER_EXPERIENCE)`）——这是「记忆是私有的」
的代码保证，而不是靠 prompt 请求它别搬运。聚合的职责是**跨副脑归并 + 一致性加权 + 冲突消解**，
两侧的经验层用的是同一套推理关系，不需要翻译关系词。

设计约束：不依赖 config/backend 业务模块；副脑显示名由调用方以 arm 显示名字典传入
（不在模块里读 arms/*.md）。
"""

from __future__ import annotations

import asyncio
import logging
import time

from openkage.memory.graph import BrainGraph
from openkage.memory.prompt_store import AGGREGATE
from openkage.memory.schema import LAYER_EXPERIENCE, LAYER_MEMORY
from openkage.roundcode import FIRST_WINDOW, encode, fmt

logger = logging.getLogger("aggregate")


def _graph_counts(graph: BrainGraph) -> dict:
    """统计一份图谱的 concept/facet/edge 计数，用来对比聚合前后主脑图谱是否真的涨了。"""
    conn = graph._conn  # noqa: SLF001  实验脚本直接读连接统计
    counts = {}
    for key, table in (("concept", "concept"), ("facet", "facet"), ("edge", "edge")):
        try:
            counts[key] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        except Exception:  # pylint: disable=broad-except
            counts[key] = -1
    return counts


def _dump_arm_graph(graph: BrainGraph, layer: str) -> str:
    """把一份**副脑**图谱的某一层导出成给聚合 Agent 通读的富文本（**带 facet 正文**）。

    ★ **必须带正文**，这不是"要不要省 token"的偏好：聚合 Agent 的 10 个工具全绑在**主脑**
    连接上（`consolidate` 里的 `_build_tools(self._conn, …)`），副脑是独立的 SQLite 文件、
    只被 `build_aggregate_input` 读来拼文本、**从不交给 Agent**。所以副脑 slug 对它
    `get_concept` 恒返回"不存在" —— 不给正文它就只能照 name+summary 编一条写进主脑，
    而且**完全静默**（2026-08-08 查出并修）。归并/加权/冲突消解全都要读正文。

    `layer` **必填**：调用方一律传 `LAYER_EXPERIENCE` —— 记忆层的**概念与正文**根本不会进
    聚合 Agent 的上下文，这是「记忆是私有的」的代码保证。
    它同时作为 `peer_layer` 界定「哪一层算层内」。
    ⚠️ **跨层边不再被丢掉**（2026-08-10 用户定案，`cross_layer="mark"`）：记忆层的 **slug 名**
    会作为关系对端出现（标成 `rel→day_1_death[memory]`）。因为实测反思建的边几乎全是跨层的，
    丢掉它们会让聚合看到的关系全军覆没。露出的只有名字，**facet 正文与 summary 仍然不出现**；
    「不许把它写进公共图谱」改由 prompt 约束（见 aggregate.yaml 第 2 节「输入长什么样」）。
    """
    return graph.dump_layer_full(layer)


def _find_dedup_hints(
    primary: BrainGraph, ephemeral_graphs: dict[str, BrainGraph], arm_display: dict[str, str]
) -> str:
    """跨 batch 去重预筛：聚合 Agent 看不到主脑现状全景（那块已删，见 build_aggregate_input），只有
    name/summary/facet kind 列表，没有相似度分数）和本 batch 的副脑图谱，多次独立聚合调用之间
    互不知情——上一批刚建的概念，这一批完全看不到"这跟我要建的是同一件事"，会堆出同义重复概念。

    这里对本 batch 各副脑图谱里每个候选概念，用向量相似度预先跟主脑图谱全量概念做一次近邻
    检索，命中阈值以上的显式列成"疑似重复"提示块塞进 user_input，供聚合 Agent 参考（不自动
    合并/不跳过 Agent 的判断——Agent 仍需 get_concept 核实语义后再决定 update_facet/
    merge_concepts 还是确实该维持新建，分数只是提示不是结论）。

    ⚠️ **候选只取经验层**（与 `_dump_arm_graph(g, LAYER_EXPERIENCE)` 一致）：记忆层概念
    根本不参与聚合，拿它们预筛既是白查、又会把专名（「我的第一个营地」这类）经提示行泄进
    聚合 Agent 的上下文 —— 那是「记忆是私有的」在正门堵住、却从预筛侧门漏出去。

    阈值与 top_k 走 yml 的 `memory_graph.dedup.*`（**从主脑图谱实例读**它已加载的那份
    RetrievalConfig，不另开一条读配置的路 —— 那正是 ArmGraph 那次静默分叉的成因）。"""
    dedup_top_k = primary._retrieval.dedup_top_k  # noqa: SLF001
    dedup_similarity = primary._retrieval.dedup_similarity  # noqa: SLF001
    lines = []
    for pkey, g in ephemeral_graphs.items():
        rows = g._conn.execute(  # noqa: SLF001  实验脚本直接读连接，跟 _graph_counts 同风格
            "SELECT slug, name, summary FROM concept WHERE layer = ? ORDER BY slug",
            (LAYER_EXPERIENCE,),
        ).fetchall()
        for slug, name, summary in rows:
            # ★ 走 `search_concept_hits` 而不是 `primary._vec_store.search` 直接读 biz_id：
            #   分块索引（2026-09-24）之后 biz_id 是**分片 key**（`{slug}::{kind}`），
            #   直接把 biz_id 当 slug 报给 Agent 会指向一条不存在的概念，而且不报错。
            #   本方法返回概念级的 `(slug, score)`，已按概念融合去重。
            fused = primary.search_concept_hits(f"{name}\n{summary}", top_k=dedup_top_k)
            close = [(s, sc) for s, sc in fused if sc >= dedup_similarity]
            if close:
                hint = "、".join(f"{s}（相似度{sc:.2f}）" for s, sc in close)
                lines.append(f"- 「{name}」（来自 {arm_display.get(pkey, pkey)} 的候选 slug={slug}）"
                             f"与主脑图谱已有概念疑似重复：{hint}")
    if not lines:
        return ""
    return (
        "\n\n## ⚠️ 向量相似度预筛（仅供参考，不是结论）：\n"
        "以下候选概念跟主脑图谱已有概念高度相似，请优先用 search_concepts/get_concept 核实语义后"
        "选择 update_facet/merge_concepts，而非直接 create_concept 造出重复概念；"
        "核实后确认并非同一回事的，正常新建即可：\n" + "\n".join(lines)
    )


def build_aggregate_input(
    primary: BrainGraph,
    ephemeral_graphs: dict[str, BrainGraph],
    arm_display: dict[str, str],
) -> str:
    """拼装聚合 Agent 的 user_input：N 份副脑的**经验层全文** + 去重预筛提示。

    聚合走"纯通读内容自主裁量 + 一致性加权（多副脑独立复现的经验更可信）"——不带任何
    correctness/reward 权重信号（那个方向存疑：踩坑反思往往比做对的反思更有沉淀价值，
    correctness≠价值高，故不作为聚合先验）。

    ★ **私有图谱只导出 experience 层**：记忆层（专名、带坐标、只给明天的自己）的**概念与正文
    根本不会出现在聚合 Agent 的上下文里** —— 这是「记忆是私有的」的代码保证，而不是靠 prompt
    请求它"别搬运"。
    ⚠️ **一个知情的例外（2026-08-10）**：跨层边的**对端 slug 名**会露出来
    （`rel→day_1_death[memory]`），因为丢掉跨层边会让聚合看到的关系全军覆没
    （实测反思建的边几乎全是记忆→经验）。只有名字，没有正文/summary；
    「不许写进公共图谱」这一条由 prompt 承担（aggregate.yaml 第 2 节那三条规矩）。
    ⚠️ 而写入侧那道保证**也已经不在了**：kind 放开为自由命名后，工具 enum 不再能物理拒绝
    记忆侧的 kind（见 memory/tools.py 的 `_kind_enum` 上方那段）。所以现在「公共图谱里
    不该有专名个例」全靠 prompt —— 反推词表时要专门检查有没有漏进来。

    ★ **不前置「主脑现状全景」**（2026-08-08 用户定案，此前有一块 `primary_view`）：
    主脑随 epoch×batch **无界增长**（实测 0805 run 三批后 120 concept = 索引形态 240 行），
    而那块的信息量已被另外两处覆盖 —— 预筛会点名"这个候选跟主脑的谁撞了、相似度多少"，
    没被点名的本来也该 `search_concepts` 查（system prompt 第 2 步："严禁凭印象直接新建"）。
    全景在这里只起一个坏作用：让 Agent 以为自己已经知道主脑有什么，从而**不去查**。
    ⚠️ 所以现在「主脑有什么」**只有 Agent 自己查得到** —— 删这块的同时必须保证 prompt 里
    那条"先 search_concepts / get_concept"的要求足够硬，否则就是把去重整个关掉了。
    """
    ephemeral_blocks = []
    for pkey, g in ephemeral_graphs.items():
        ephemeral_blocks.append(
            f"### 来自「{arm_display.get(pkey, pkey)}」的经验：\n"
            f"{_dump_arm_graph(g, LAYER_EXPERIENCE)}"
        )

    # 文案在 prompts/aggregate/aggregate.yaml 的 `user_inputs.batch_merge`。这里只算值。
    #
    # ★ `used_vocab` = 主脑**已经用过**的 kind/relation 名 + 次数（`dump_used_vocab`）。
    #   kind/relation 已放开为自由命名（2026-08-09），而聚合 Agent **独立处理每一批、
    #   看不到上一批的对话** —— 不给这份清单，同一个范畴每批都会拿到一个新名字。
    #   ⚠️ 它**不违反**上面那条「不前置主脑现状全景」的定案：全景是按概念逐条列的、随
    #   epoch×batch 无界增长，且会让 Agent 以为自己已经知道主脑有什么而不去 search；
    #   本块只有「标签名 + 次数」两列（行数由词表规模决定、不由概念数决定），
    #   回答的是完全不同的问题 —— 不是"有哪些概念"，而是"这个图谱管这类内容叫什么"。
    return AGGREGATE.render_user_input("batch_merge", {
        "arm_blocks": "\n\n".join(ephemeral_blocks),
        "dedup_hints": _find_dedup_hints(primary, ephemeral_graphs, arm_display),
        "used_vocab": primary.dump_used_vocab(),
    })


def _warn_layer_violations(
    ephemeral_graphs: dict[str, BrainGraph], arm_display: dict[str, str]
) -> None:
    """检查各私有图谱的**分层是否健康**，异常就告警（不静默、也不中止）。

    ★ 真正危险的失败模式是「**经验层为空**」：聚合只读 experience 层，所以一个副脑若整批
    只写了记忆层，它这批的贡献**恒定为 0** —— 而且完全静默（聚合照常跑、只是没东西可归并，
    日志里看不出是谁没产出）。这通常意味着反思 Agent 没照 prompt 的第 2 节做「两层都要写」。

    另一侧也报：有记忆层内容是正常且期望的（那是它的主体），但**一个概念都没有**说明
    反思整个失败了。两种情况都只告警不抛错 —— 一条轨迹的墙钟已经烧掉了（MC 一夜好几分钟），
    为分层问题丢掉整批真实经历不划算。

    ★ 消融 no_exp_text（2026-09-18）：经验层恒空是**该 rung 的设计**（create_concept 拒写
    experience），不是反思失败。「经验层为 0」warning 在此 rung 下每窗必现 = 纯噪音，会把
    真故障信号训练成可忽略——抑制为一条 info 说明行。判定从**图谱实例的 _ablation 属性**读
    （ArmGraph 构造时传入），不在本函数签名上开参数口。
    """
    for pkey, g in ephemeral_graphs.items():
        name = arm_display.get(pkey, pkey)
        try:
            counts = dict(g._conn.execute(  # noqa: SLF001  与本模块其它统计同风格
                "SELECT layer, COUNT(*) FROM concept GROUP BY layer"
            ).fetchall())
        except Exception:  # pylint: disable=broad-except
            continue
        n_exp = counts.get(LAYER_EXPERIENCE, 0)
        n_mem = counts.get(LAYER_MEMORY, 0)
        _abl = getattr(g, "_ablation", None)
        _no_exp = _abl in ("no_exp_text", "no_exp_layer")
        if not counts:
            logger.warning("[聚合] %s 的私有图谱是空的 —— 本批它没有任何产出（反思是否失败？）", name)
        elif n_exp == 0:
            if _no_exp:
                # 文案带真实 rung 名（no_exp_text / no_exp_layer 走同一分支，写死一个会报错口径）
                logger.info("[聚合] %s：经验层 0 概念（消融 %s 设计如此——本 run 不收集经验文本）"
                            "／记忆层 %d 概念（私有，不参与）", name, _abl, n_mem)
            else:
                logger.warning(
                    "[聚合] %s **经验层为 0 个概念**（记忆层 %d 个）—— 它这批对公共图谱的贡献是 0。"
                    "聚合只读经验层，说明反思 Agent 只写了记忆、没做归纳（见 reflect.yaml system §2）",
                    name, n_mem,
                )
        else:
            logger.info("[聚合] %s：经验层 %d 概念（将被归并）／记忆层 %d 概念（私有，不参与）",
                        name, n_exp, n_mem)
        # 词表外的层标记（绕过工具直接写库 / 迁移判错）—— 这些既不会被聚合、也不该存在
        for tag, n in counts.items():
            if tag not in (LAYER_MEMORY, LAYER_EXPERIENCE):
                logger.warning(
                    "[聚合] %s 有 %d 个概念的层标记是 %r（应为 %r 或 %r）—— 它们不会进聚合。"
                    "多半是绕过工具直接写库，或老库迁移判层失败",
                    name, n, tag, LAYER_MEMORY, LAYER_EXPERIENCE,
                )


async def consolidate(
    primary: BrainGraph,
    ephemeral_graphs: dict[str, BrainGraph],
    arm_display: dict[str, str],
    epoch: int,
    batch_id: int,
    window: int = FIRST_WINDOW,
    *,
    lag_days: dict[str, int] | None = None,
    absent_arms: list[str] | None = None,
) -> dict:
    """从这一次窗口的 N 份私有记忆里提炼经验、沉淀进公共图谱：跑 primary.consolidate。

    arm_display: {arm_key: 显示名}，只用于拼 user_input 里各私有记忆的来源标注。
    round 约定 = `roundcode.encode(epoch, batch_id, window)`——聚合新建/更新的行据此打
    created_at / updated_at。

    `window` = **题内**聚合窗口序号（1 起）。不传 = 1，即「一个 batch 只在末尾聚合一次」
    这个原有行为（见 roundcode.FIRST_WINDOW 为何不用哨兵值）。

    `lag_days` / `absent_arms` 只进留痕，**不影响聚合本身**，但必须记（见下）：
    - `lag_days` {arm_key: 快照那一刻它在第几天}。题内聚合是**不阻塞**的（各副脑快照后立刻
      继续探索，聚合在后台跑），所以「这批经验落进主脑时，各副脑已经跑到第几天了」是记忆反馈
      回路的**真实滞后**。不记它，滞后就成了一个无人知晓的混杂因素 —— 而它直接削弱「记忆让它
      变强」这个主张的证据强度（第 K+1 天的 Agent 可能还没读到第 K 天的教训）。
    - `absent_arms` 本窗口**缺席**的副脑（rollout 已结束或异常退出，不再供货）。一致性加权的
      分母变了，日志与留痕里必须看得见是谁缺席。
    """
    round_num = encode(epoch, batch_id, window)
    _warn_layer_violations(ephemeral_graphs, arm_display)
    before = _graph_counts(primary)

    if getattr(primary, "_ablation", None) == "raw_shared_pool":
        _t_agg = time.perf_counter()
        added = await asyncio.to_thread(primary.ingest_raw_pool, round_num, ephemeral_graphs)
        aggregate_sec = time.perf_counter() - _t_agg
        after = _graph_counts(primary)
        logger.info("[聚合][%s] raw_shared_pool：原样追加 %s，公共图谱 %s → %s，耗时=%.1fs",
                    fmt(round_num), added, before, after, aggregate_sec)
        return {
            "epoch": epoch, "batch_id": batch_id, "window": window, "round_num": round_num,
            "mode": "raw_shared_pool", "n_arms": len(ephemeral_graphs),
            "primary_before": before, "primary_after": after, "agent_summary": "",
            "aggregate_sec": round(aggregate_sec, 1),
            **({"lag_days": dict(lag_days)} if lag_days else {}),
            **({"absent_arms": list(absent_arms)} if absent_arms else {}),
            "note": "raw_shared_pool — 经验层原样追加，不归并/去重/冲突消解",
        }

    user_input = build_aggregate_input(primary, ephemeral_graphs, arm_display)

    # ★ **聚合不再碰技能**（2026-08-27）：技能归各臂常驻 SkillStore 维护（谁写的谁维护），
    #   聚合只归并 concept/facet/edge 经验。原技能候选上交/adopt/merge/demote 已全部删除。
    logger.info(
        "[聚合][%s] 公共图谱前=%s，从 %d 份私有记忆提炼（consolidate ReActAgent）%s",
        fmt(round_num), before, len(ephemeral_graphs),
        f"；⚠️ 缺席副脑 {[arm_display.get(a, a) for a in absent_arms]}" if absent_arms else "",
    )
    if lag_days:
        logger.info("[聚合][%s] 快照时各副脑进度（天）：%s —— 这批经验落进主脑时它们已跑到这里",
                    fmt(round_num), {arm_display.get(k, k): v for k, v in lag_days.items()})

    _t_agg = time.perf_counter()
    summary = await primary.consolidate(round_num, user_input)
    aggregate_sec = time.perf_counter() - _t_agg

    after = _graph_counts(primary)
    logger.info(
        "[聚合][%s] 主脑图谱后=%s（Δconcept=%+d Δfacet=%+d Δedge=%+d）耗时=%.1fs",
        fmt(round_num), after,
        after["concept"] - before["concept"],
        after["facet"] - before["facet"],
        after["edge"] - before["edge"],
        aggregate_sec,
    )
    return {
        "epoch": epoch,
        "batch_id": batch_id,
        "window": window,
        "round_num": round_num,
        "mode": "react_aggregate",
        "n_arms": len(ephemeral_graphs),
        "primary_before": before,
        "primary_after": after,
        "agent_summary": summary,
        "aggregate_sec": round(aggregate_sec, 1),
        **({"lag_days": dict(lag_days)} if lag_days else {}),
        **({"absent_arms": list(absent_arms)} if absent_arms else {}),
        "note": "consolidate — 聚合 Agent = 跟反思 Agent 同构的 ReActAgent，自主读写主脑图谱",
    }


