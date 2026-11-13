"""memory 包内共享 helper（供 graph.py 与 tools.py 共用，避免循环 import）。

内容：slug 规整、Concept 富文本渲染、所有 CRUD 工具的公共基类 _BoundTool。
纯 helper，不依赖 BrainGraph / tools。

两个写图谱 Agent 的全部提示词（system prompt + 受控词表 + probe + 工具文案 + user_input 模板）
收在 `prompts/reflect/reflect.yaml` 与 `prompts/aggregate/aggregate.yaml`，装载走
`openkage/memory/prompt_store.py` 的 `AgentPrompts`；本模块不含 prompt 加载逻辑。
"""

from __future__ import annotations

import sqlite3
import threading

from openkage.memory.vector_store import split_part_biz_id


def fuse_part_hits(
    hits: list[dict],
    *,
    w_identity: float = 1.0,
    w_facet: float = 1.0,
    facet_agg: str = "max",
) -> list[tuple[str, float]]:
    """把**分片级**的向量命中融合成**概念级**分数，返回 `[(slug, score)]` 按分数降序。

    ★ 分块索引（2026-09-24）的读取侧。写入侧见 `tools._index_concept`：一个概念存 1+N 条
    向量（identity = `name\\nsummary`，每条 facet 各一条 `{kind}: {content}`），所以 `search`
    回的是**行**，同一个概念会占多行，必须先归并再排序 —— 否则 top_k 个名额会被同一个概念的
    多个分片占满，真正相关的概念反而挤不进来。

    融合式：`score = (w_identity·cos_id + w_facet·agg_k(cos_facet_k)) / (实际参与的分片权重和)`

    ★★ **除以权重和不是装饰**。不做归一化时 score 的量程是 `w_id + w_facet` 倍（默认 2 倍），
    于是 `dedup.similarity=0.65`、`anchor.min_score` 这些**在旧（单条向量）分布上标定出来**的
    绝对余弦门会整体位移 —— 而它们位移时不报错，只是"门突然变松/变紧"。归一化后输出仍在
    余弦量程 `[-1,1]`，既有标定继续可用（相对门 `gap_ratio` 本来就是尺度无关的）。

    `agg`：`max` = 最匹配的那条 facet 决定概念得分（分块索引要的效果：命中一个细分知识点就能
    拉起整个概念）；`mean` = 要求概念的 facet 整体相关。

    ⚠️ **旧格式兼容**：分片化之前一个概念只有一条 biz_id == slug 的向量，`split_part_biz_id`
    把它判成 identity → 分母只有 `w_identity`，退化成「只看 name+summary」而不报错。
    """
    id_score: dict[str, float | None] = {}
    facet_scores: dict[str, list[float]] = {}
    order: list[str] = []
    for h in hits or []:
        biz_id = h.get("biz_id") or ""
        if not biz_id:
            continue
        # payload 里冗余了 slug/kind，优先读它；没有再按 biz_id 回解（旧库 payload 无这些键）。
        payload = h.get("payload") or {}
        slug = payload.get("slug") or split_part_biz_id(biz_id)[0]
        if not slug:
            continue
        if slug not in id_score:
            id_score[slug] = None
            facet_scores[slug] = []
            order.append(slug)
        score = float(h.get("score") or 0.0)
        kind = payload.get("kind")
        if kind is None:
            kind = split_part_biz_id(biz_id)[1]
        if kind is None:
            id_score[slug] = score
        else:
            facet_scores[slug].append(score)

    out: list[tuple[str, float]] = []
    for slug in order:
        num, den = 0.0, 0.0
        cos_id = id_score[slug]
        if cos_id is not None:
            num += w_identity * cos_id
            den += w_identity
        fs = facet_scores[slug]
        if fs:
            agg = max(fs) if facet_agg == "max" else (sum(fs) / len(fs))
            num += w_facet * agg
            den += w_facet
        if den <= 0:
            continue
        out.append((slug, num / den))
    # 稳定降序：同分时保持向量库回的顺序（不引入与内容无关的抖动）
    out.sort(key=lambda kv: kv[1], reverse=True)
    return out


def _slugify(text: str) -> str:
    """把任意文本规整成 snake_case 的 ASCII slug；非 ASCII 字符（比如夹杂的中文）
    直接丢弃——slug 只是给人/Agent 用的可读标识，不要求完整还原语义。"""
    import re

    ascii_text = "".join(c if c.isascii() else " " for c in text)
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", ascii_text).strip("_").lower()
    return slug or "concept"


def _concept_row_to_dict(r) -> dict:
    """`SELECT slug, name, summary, layer` 的行 → dict。

    第 4 列是 `concept.layer`（层标记 memory / experience，见 schema.LAYER_*）。
    """
    return {"slug": r[0], "name": r[1], "summary": r[2], "layer": r[3]}


def _facet_kinds(conn: sqlite3.Connection, concept_slug: str) -> list[str]:
    """这个 concept 下已有哪些 kind——不带 content，只提供"大概有哪几类内容"的信息，
    完整内容（含每条的 facet_id）要用 get_concept 精确 drill 进去看。

    因为同 (concept_slug, kind) 是联合主键、每种 kind 最多一条，kind 列表本身就是完整索引，
    不需要额外的 facet id。仍做一次保序去重作为防御（老库若曾被别的路径写脏不至于渲染出重复项）。
    dict.fromkeys 保序去重，让展示顺序稳定（便于模型形成定式）。"""
    rows = conn.execute(
        "SELECT kind FROM facet WHERE concept_slug = ? ORDER BY kind", (concept_slug,)
    ).fetchall()
    return list(dict.fromkeys(r[0] for r in rows))


def _concept_relations(
    conn: sqlite3.Connection, concept_slug: str, *, peer_layer: str | None = None,
    cross_layer: str = "drop",
) -> list[dict]:
    """这个 concept 的所有边（出边 + 入边），每条给出对端 slug、方向、**对端所在层**。

    对端必须真实存在（`JOIN concept`）：避免孤儿边（对端已被 del_concept 删除但边未清
    干净的老库）渲染进 prompt，让 Agent 沿着死边去 get_concept 白花一步。

    `peer_layer` = 「哪一层算**层内**」。`cross_layer` 决定层外的边怎么处理：

    - `"drop"`（默认，老行为）：**丢掉**跨层边，只留层内边；
    - `"mark"`：**保留**跨层边，并在返回的 dict 里带上 `peer_layer` 键，让渲染方标注出来
      （见 `_format_concepts_rich`）。

    ★ **为什么会有 `"mark"`**（2026-08-10 用户定案）：聚合读副脑经验层时，`"drop"` 会让
    关系**全军覆没** —— 实测反思 Agent 建的边几乎全是**跨层**的（`那次死亡 --验证了-->
    夜间等待的风险`），经验层内部一条边都没有。于是聚合拿到的每一条经验都写着「已有关系：无」，
    「跨副脑归并」少了整整一维输入（实测 smoke：12 条边，聚合可见 0 条）。
    `"mark"` 把这一维还给它：**代价是聚合会看到记忆层的 slug 名**（`day_1_death`）——
    这是知情的取舍，不是疏漏。约束改为在 prompt 侧讲清（见 aggregate.yaml）：
    那些 slug 只是**连接结构的锚点**，`get_concept` 查不到、也不许把它们写进公共图谱。
    ⚠️ 只有 slug/层名会露出来，**facet 正文与 summary 仍然完全不出现** ——
    `_dump_layer(layer=…)` 过滤的是概念**行**，那一层没变。
    """
    # ⚠️ `(from_slug = ? OR to_slug = ?)` 必须**带括号**：SQL 里 AND 比 OR 结合更紧，
    # 少了括号就变成 `from_slug=? OR (to_slug=? AND p.layer=?)` —— 出边会完全绕过层过滤。
    sql = (
        "SELECT e.relation, e.from_slug, e.to_slug, p.layer FROM edge e "
        "JOIN concept p ON p.slug = "
        "  (CASE WHEN e.from_slug = ? THEN e.to_slug ELSE e.from_slug END) "
        "WHERE (e.from_slug = ? OR e.to_slug = ?)"
    )
    params: tuple = (concept_slug, concept_slug, concept_slug)
    if peer_layer is not None and cross_layer == "drop":
        sql += " AND p.layer = ?"
        params = (*params, peer_layer)
    rows = conn.execute(sql, params).fetchall()
    return [
        {"relation": relation, "peer_slug": (to_slug if from_slug == concept_slug else from_slug),
         "direction": "out" if from_slug == concept_slug else "in",
         # 跨层标注只在 mark 模式下有意义：层内边的 peer_layer 与自己相同，渲染方不该加标记。
         "peer_layer": (plyr if (peer_layer is not None and plyr != peer_layer) else None)}
        for relation, from_slug, to_slug, plyr in rows
    ]


def _facet_contents(conn: sqlite3.Connection, concept_slug: str) -> list[tuple[str, str]]:
    """这个 concept 下的 `(kind, content)` **全文**，按 kind 排序。

    与 `_facet_kinds` 的分工 = 「有没有」vs「写了什么」。谁该用哪个由**调用方能不能
    `get_concept`** 决定，不是省 token 的偏好：
    - 能（反思/聚合读**自己那份**图谱）→ 用 kind 索引，正文按需 drill；
    - 不能 → 必须给正文。聚合读**副脑**图谱就是这种：它的 10 个工具全绑在主脑连接上，
      副脑的 slug 对它 `get_concept` 恒返回"不存在"（2026-08-08 查出的静默 bug）。
    """
    return [(k, c) for k, c in conn.execute(
        "SELECT kind, content FROM facet WHERE concept_slug = ? ORDER BY kind",
        (concept_slug,),
    ).fetchall()]


def _format_concepts_rich(
    conn: sqlite3.Connection, rows: list[dict], *, peer_layer: str | None = None,
    with_content: bool = False, cross_layer: str = "drop",
) -> str:
    """把 Concept 列表格式化成给 Agent 看的文本，每条附带**层**、facet、relations。

    `with_content` 决定 facet 那一行是**索引**还是**正文**，这是全文件最容易选错的一个参数：

    - `False`（默认，索引形态）：只出 `已有facet kind：a、b`。给**能 `get_concept` 补上
      正文**的调用方 —— 反思/聚合读自己那份图谱。这样初始 prompt 不被摊开的长文本挤爆。
    - `True`（正文形态）：每条 facet 一行 `[kind] 全文`。给**拿不到正文**的调用方 ——
      聚合读**副脑**图谱只能走这个：它的工具绑在主脑连接上，副脑 slug 对它
      `get_concept` 恒"不存在"，索引形态下它只能照着 name+summary 编内容写进主脑，
      而且**完全静默**（2026-08-08 查出）。归并/加权/冲突消解全都要读正文才做得了。

    ★ **`[层]` 必须渲染出来**：候选列表**跨两层召回**（私有图谱同时装
    memory + experience），而 Agent 拿这份列表决定"复用哪个 / 往谁身上挂 facet"——
    看不到层就会挑错：往 memory 层概念上挂经验 kind 会被拒（白花一步），
    更糟的是它可能以为某条经验还不存在而重复新建一个通名概念。
    `rows` 的 `layer` 键由 `_concept_row_to_dict` 提供；缺键时降级不显示（聚合侧同构调用也安全）。

    `peer_layer` 透传给 `_concept_relations`：哪一层算层内。`cross_layer` 决定层外边的去向 ——
    `"drop"` 丢掉、`"mark"` 保留并标成 `rel→peer[层名]`（聚合读副脑时用 `"mark"`，
    理由与代价见 `_concept_relations` 的 ★）。

    ★ **关系必须带方向**（2026-08-09 修）：本行渲染的是**出边和入边混在一起**的清单
    （`_concept_relations` 不限方向），而此前一律出成 `relation→peer` —— 入边被渲染成了
    **反的**。`day_1 precedes day_2` 在 day_2 那行读作「precedes→day_1」，
    整条时间线是倒的；`causes` / `requires` / `blocked_by` 同理。
    出边 `rel→peer`、入边 `rel←peer`：箭头始终从**主语**指向**宾语**，
    与边清单 `a --rel--> b` 同一读法（`←` 表示对端是主语、自己是宾语）。
    """
    if not rows:
        return "（无）"
    lines = []
    for r in rows:
        relations = _concept_relations(conn, r["slug"], peer_layer=peer_layer,
                                       cross_layer=cross_layer)
        # 跨层边标上对端所在层（`rel→day_1_death[memory]`）—— 不标的话聚合会把
        # `day_1_death` 当成一个它能 get_concept 的经验概念去查，白花一步且困惑。
        rel_str = "、".join(
            f"{rel['relation']}{'→' if rel['direction'] == 'out' else '←'}{rel['peer_slug']}"
            + (f"[{rel['peer_layer']}]" if rel.get("peer_layer") else "")
            for rel in relations
        ) or "无"
        layer = r.get("layer")
        layer_str = f"[{layer}] " if layer else ""
        lines.append(f"- {layer_str}slug={r['slug']} 「{r['name']}」{r['summary']}")
        if with_content:
            facets = _facet_contents(conn, r["slug"])
            # 缩进 4 格与 dump_memory_layer 的正文形态对齐，视觉上归属到所属概念。
            lines.extend(f"    [{kind}] {content}" for kind, content in facets)
            if not facets:
                lines.append("    （这个概念下还没有任何 facet）")
            lines.append(f"  已有关系：{rel_str}")
        else:
            kinds_str = "、".join(_facet_kinds(conn, r["slug"])) or "无"
            lines.append(f"  已有facet kind：{kinds_str}｜已有关系：{rel_str}")
    return "\n".join(lines)


class _BoundTool:
    """本包里 10 个 CRUD 工具的公共基类：绑定同一个 sqlite 连接 + 向量库 + 锁 + 当前轮次号。

    MRO 是 具体Tool -> _BoundTool -> BaseTool，super().__init__() 走到 BaseTool.__init__()，
    补上 _on_result_hook/_before_call_hook 等属性——不调用会导致工具执行时 AttributeError。

    BaseTool.async_run() 默认用 asyncio.to_thread() 包装同步 run()，ReAct 循环里一步有
    多个 tool_calls 时会并发跑在不同线程，需要 lock 序列化对同一个 sqlite 连接的访问
    （sqlite3.connect 要带 check_same_thread=False 配合）。"""

    def __init__(self, conn: sqlite3.Connection, vec_store, lock: threading.Lock, round_num: int):
        super().__init__()
        self._conn = conn
        self._vec_store = vec_store
        self._lock = lock
        self._round_num = round_num

    def _concept_exists(self, slug: str) -> bool:
        return self._conn.execute(
            "SELECT 1 FROM concept WHERE slug = ?", (slug,)
        ).fetchone() is not None

    def _facet_exists(self, concept_slug: str, kind: str) -> bool:
        return self._conn.execute(
            "SELECT 1 FROM facet WHERE concept_slug = ? AND kind = ?", (concept_slug, kind)
        ).fetchone() is not None
