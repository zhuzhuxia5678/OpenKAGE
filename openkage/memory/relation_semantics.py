"""
记忆图谱的关系语义打分 —— 图扩展的边权来源（消费方是 `graph.BrainGraph._expand`）。

起点是照抄 backend/kg/relation_semantics.py 的 probe 均值向量机制：把"边的类型"也看作可被
embedding 的语义，query 向量跟每种关系的 probe 均值向量做 cosine，得到一张连续权重表，驱动
图扩展该往哪些边走。每种关系配几条"会想沿这条边走"的 query 语气句作为 probe。

★ **本模块已经分叉出关键一步：权重多了「入口」这一元**（`score_relations_from_entry`）。
    原版：权重 = f(query, 关系)               —— 与图上位置无关，同一种关系在整张图上处处同分
    本模块：权重 = f(query, **入口概念**, 关系) —— 同一种关系站在不同概念上拿到不同权重
做法是把入口概念名拼进**每条 probe**（不是拼在 query 上，见 `_entry_relation_vecs` 里
"公式容易搞反"那段），得到「**站在这个概念上**，这类关系听起来像什么」。
`score_relations`（不带入口的原版口径）保留给不需要图上下文的调用方。

★★ **权重文本 = probe 若有，否则关系名本身**（2026-08-09，随 kind/relation 词表放开）★★

上面说的 probe 是关系配在 yaml 里的语气句。现在关系名由 Agent **自由命名**、根本不在 yaml
里，所以每种关系的**权重文本**这样取：

    权重文本(关系) = yaml 里配了 probe → 那几条 probe（取均值，老行为逐字节不变）
                   = 没配（= 全部自造关系）→ **`[关系名]` 本身这一条**

没有这个 fallback 就是**全图零 probe**：每种关系都拿不到向量、`_expand` 一律用缺省 0.0，
softmax 退化成均匀分布 —— 关系语义打分整段静默失效，只剩向量锚定 + 强度。
（这也是为什么两份 yaml 的 system prompt 里明写「关系名会直接参与检索打分」：自由命名下，
关系名**就是**那段被拿去比语义的文本，起成 `related_to` 等于这条边永远不会被走到。）

★ **候选关系集合由调用方传入**（`relations` 参数）：自由命名下没有"全部合法关系"这份名单了，
唯一的真相是**图里现存的边**。`graph._expand` 每跳把本跳 SQL 查出来的 relation 值传进来。
这同时改变了 z-score 的参照组 —— 见 `score_relations_from_entry` 的 ⚠️ 那段。

★ **两套关系词表 = 两套 probe**：私有图谱（ArmGraph）走反思那套（时空指称 + 推理 ——
它同时装记忆层与经验层），公共图谱（BrainGraph）走聚合那套（只有推理类）。
两套 probe 各自算均值向量、各自打分 —— **绝不能混用**：拿公共图谱那套给私有图谱打分，
时空边全都拿不到 probe，检索质量会静默变差。
⚠️ **词表放开后两套都是空表**，于是两者当前行为相同（都走关系名 fallback）；
填回词表时上面那条禁令立刻重新生效，别因为"现在没差别"就把 vocab 参数去掉。

★ **两套各在自己 Agent 的 yaml 里**（唯一真相，本模块不内联任何 dict 字面量）：
`prompts/reflect/reflect.yaml` 与 `prompts/aggregate/aggregate.yaml` 的 `vocab.relations`，
经 `openkage.memory.prompt_store` 的 `REFLECT` / `AGGREGATE` 读取。
"""

from __future__ import annotations

import logging
import math
from functools import lru_cache

logger = logging.getLogger(__name__)

from openkage.memory.prompt_store import AGGREGATE, REFLECT

#: 两套 probe，各来自对应 Agent 的 yaml。私有图谱用 REFLECT_PROBES（时空指称 + 推理，
#: 因为它同时写两层），公共图谱用 AGGREGATE_PROBES（只有推理类）—— 见模块 docstring 的「绝不能混用」。
#: ⚠️ **当前两者都是空 dict**（kind/relation 词表已放开）→ 每种关系的权重文本都退到
#: 「关系名本身」。这不是 bug，是设计好的 fallback（见模块 docstring 那一段）。
REFLECT_PROBES: dict[str, list[str]] = REFLECT.relation_probes
AGGREGATE_PROBES: dict[str, list[str]] = AGGREGATE.relation_probes

#: z-score 的标准差下限（`score_relations_from_entry`）：小于它就当"所有关系同分"、全部返回 0，
#: 而不是去除一个≈0 的数 —— 那会把纯浮点噪声放大成看起来煞有介事的"区分度"。
#: 取 1e-9 而非 0：余弦是浮点运算，真正退化的情形下 sd 是 1e-17 量级而非精确 0。
_SD_EPS = 1e-9


def _cos(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def _mean_vec(vecs: list[list[float]]) -> list[float]:
    valid = [v for v in vecs if v]
    if not valid:
        return []
    dim = len(valid[0])
    out = [0.0] * dim
    for v in valid:
        for i, x in enumerate(v):
            out[i] += x
    n = float(len(valid))
    return [x / n for x in out]


class _MemoryRelationProbes:
    def __init__(self, embedder, probe_table: "dict[str, list[str]]", label: str = "") -> None:
        self.relation_emb: dict[str, list[float]] = {}
        self.enabled = False
        self._label = label
        self._build(embedder, probe_table)

    def _build(self, embedder, probe_table: "dict[str, list[str]]") -> None:
        if not embedder or getattr(embedder, "dim", 0) <= 0:
            logger.info("memory_relation_semantics: embedder 未启用，关系语义打分关闭")
            return

        flat_texts: list[str] = []
        index_map: list[str] = []
        for rel, probes in probe_table.items():
            for p in probes:
                flat_texts.append(p)
                index_map.append(rel)

        try:
            vecs = embedder.embed_many(flat_texts)
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("memory_relation_semantics: probe 嵌入失败 err=%s", exc)
            return

        bucket: dict[str, list[list[float]]] = {}
        for rel, v in zip(index_map, vecs):
            bucket.setdefault(rel, []).append(v)
        for rel, vs in bucket.items():
            mean = _mean_vec(vs)
            if mean:
                self.relation_emb[rel] = mean

        self.enabled = bool(self.relation_emb)
        if self.enabled:
            logger.info("memory_relation_semantics: 已加载 %d 种%s关系 probe",
                        len(self.relation_emb), self._label)


#: 词表名 → (probe 表, 日志用标签)。`vocab` 参数就用这里的 key（同 tools._VOCABS）。
_PROBE_TABLES = {
    "reflect": (REFLECT_PROBES, "私有图谱"),
    "aggregate": (AGGREGATE_PROBES, "公共图谱"),
}


@lru_cache(maxsize=len(_PROBE_TABLES))
def _get_probes(vocab: str) -> _MemoryRelationProbes:
    """按词表取（并缓存）probe 均值向量。lru_cache 的 key 是 vocab —— 两套各建一次、各自缓存。

    ⚠️ maxsize 必须 >= 词表数：设成 1 会让两套 probe 互相踢出缓存，每次检索都重新
    embedding 一遍全部 probe 句（走 embed server，慢且毫无必要）。
    """
    from openkage.embedder import get_embedder

    try:
        table, label = _PROBE_TABLES[vocab]
    except KeyError:
        raise ValueError(
            f"未知的关系词表 {vocab!r}，可用：{sorted(_PROBE_TABLES)}"
        ) from None
    return _MemoryRelationProbes(get_embedder(), table, label)


def score_relations(query_emb: list[float], vocab: str = "aggregate") -> dict[str, float]:
    """对 query embedding 给出每种关系类型的余弦相似度，范围 [-1, 1]；未启用时返回空 dict。

    `vocab` 决定用哪套 probe（"reflect" = 私有图谱，含时空指称；"aggregate" = 公共图谱，推理类）。
    **必须与图谱类型匹配** —— 混用会让该图谱的边全都拿不到权重，图扩展静默退化成只有向量锚定
    （见模块 docstring）。

    ⚠️ 这是**不带入口上下文**的版本（probe 均值向量与图上位置无关，原版口径）。图扩展走
    `score_relations_from_entry` —— 那个把入口概念名拼进每条权重文本，
    让同一种关系在不同入口上拿到不同权重。本函数保留给不需要入口上下文的调用方。

    ⚠️ **它只认 probe 表里的关系，没有关系名 fallback** —— 词表放开后 probe 表是空的，
    所以本函数当前**恒返回空 dict**。这是有意的：它是"与图无关"的口径，压根拿不到
    「图里有哪些关系」这个信息（那只能由调用方在图上查）。要给自造关系打分，用带入口的那个。
    """
    probes = _get_probes(vocab)
    if not probes.enabled or not query_emb:
        return {}
    return {rel: _cos(query_emb, vec) for rel, vec in probes.relation_emb.items()}


def _dot_over_qnorm(q: list[float], v: list[float]) -> float:
    """`q·v / |q|` —— **只归一化 query 那一侧**，不归一化 v。

    为什么不直接用 `_cos`：「先分别算相似度再取平均」(B) 与「先平均向量再算相似度」(A)
    是**两个不同的量**。因为本仓 embedder 输出已归一化，两者精确相差一个因子：

        mean_i cos(q, v_i)  =  |v̄| · cos(q, v̄)        （v̄ = mean(v_i)）
        └── 先分别算再平均(B) ──┘   └─ probe 聚拢度 ─┘ └ 先平均再算(A) ┘

    `|v̄|` 是这组 probe 彼此有多一致（全同向→1，发散→趋 0）。所以 **B 自带一个「这个关系的
    probe 定义得清不清楚」的惩罚**：probe 写得东一句西一句的关系拿不到高权重。A 会把发散的
    probe 平均成一个"四不像"向量再去比，发散本身被抹掉了——两者排序会不同，而权重进 softmax
    后既决定每条边被展开的概率、又决定 top-p 按什么顺序剪尾（见 `graph._sample_edges`），
    排序变化会真实改变走哪些边，故这不是等价改写。

    我们要 B。而 B 又**不需要**保留全部原始 probe 向量：B 对 q 是线性的，展开即

        mean_i cos(q, v_i) = mean_i (q·v_i) = q·mean(v_i) / |q|      （v_i 已归一化）

    所以只缓存**未归一化的均值向量**、算一次点积再除 `|q|` 就等于逐条算再平均，成本与 A
    完全相同。
    """
    nq = math.sqrt(sum(x * x for x in q))
    if nq == 0.0 or not v or len(q) != len(v):
        return 0.0
    return sum(a * b for a, b in zip(q, v)) / nq


@lru_cache(maxsize=4096)
def _entry_relation_vecs(
    entry_name: str, vocab: str, relations: "tuple[str, ...]"
) -> "tuple[tuple[str, tuple[float, ...]], ...]":
    """把「入口概念名 + 每条权重文本」逐条嵌入，按关系取均值向量（**不归一化**，见下）。

    返回 `((relation, mean_vec), …)`（元组而非 dict —— `lru_cache` 要求返回值可哈希才好复用）。

    ★ **`relations` 是要打分的关系名**（调用方给，通常是图里本跳实际存在的那些）。
    每种关系的**权重文本**取 `probe 表里配的那几条` —— 没配就用 **`[关系名]` 本身**
    （见模块 docstring 的 fallback 那段）。所以自造关系走的是
    `embed(入口名 + " " + 关系名)` 这一条，与配了 probe 的关系走同一条代码路径、
    只是文本条数为 1。
    ⚠️ 参数必须是**元组**（`lru_cache` 要求可哈希）。调用方请传**排序去重**后的元组，
    否则同一组关系的不同顺序会各占一个缓存槽、各打一次 embedding。

    ⚠️ 均值向量**不要归一化**：消费方 `_dot_over_qnorm` 依赖它保留模长 —— 那个模长正是
    「这组 probe 有多聚拢」，是「先分别算相似度再平均」这个口径的组成部分（见那边的推导）。
    归一化掉它就退化成了「先平均再算」，是另一个量。
    （只有一条文本时模长恒为 1 —— 也就是说**自造关系不吃"聚拢度惩罚"**，
    这是知情的：一个名字谈不上内部一致性。配了 probe 的关系仍受此惩罚，两者不完全同刻度，
    但 z-score 是入口内做的，混合入口里两类关系仍在同一组内被标定。）

    ⚠️ 公式容易搞反，注意区分：
        入口名拼在**每条权重文本**上（本函数做的）：
            cos( embed(query), mean_probes( embed(入口名 + probe) ) )
        入口名拼在 query 上（不要这样）：
            cos( embed(query + 入口名), probe均值向量 )
    差别不是措辞：拼在 probe 上，得到的是**「站在这个概念上，这类关系听起来像什么」**
    这个受入口调制的关系语义；而 query 那一侧保持干净，就是 Agent 原本的意图。
    拼在 query 上则相当于污染了意图向量，且关系侧仍是与位置无关的通用 probe 均值。

    `lru_cache` 的意义很实：权重文本由 (入口名, 关系集合) 完全确定，
    同一个概念在多次检索、多跳里反复当入口时直接命中缓存，零网络开销。
    maxsize=4096 远超单份图谱的概念数（几百量级）—— 但注意自由命名下 key 多了一维
    （关系集合），同一个入口在不同跳可能带不同的关系集合，缓存条目会比此前多。
    """
    from openkage.embedder import get_embedder

    # ⚠️ **不要在这里调 `_get_probes`**：那个函数会把**裸 probe**全部嵌入一遍去算与位置无关的
    # 均值向量 —— 而本函数要的是「入口名 + probe」，用不上它。只为拿一个 `enabled` 标志就触发
    # 一遍多余的 embedding，纯属浪费。直接问 embedder 本人是否可用即可。
    embedder = get_embedder()
    if not embedder or getattr(embedder, "dim", 0) <= 0:
        return ()
    table, _label = _PROBE_TABLES[vocab]
    flat: list[str] = []
    index: list[str] = []
    for rel in relations:
        # probe 表里配了就用那几条；没配（自造关系的常态）就用关系名本身当唯一一条。
        for p in (table.get(rel) or [rel]):
            # 「入口名 + 权重文本」：`我的1号营地 那里面有什么` / `我的1号营地 contains`
            flat.append(f"{entry_name} {p}")
            index.append(rel)
    if not flat:
        return ()
    try:
        vecs = embedder.embed_many(flat)
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("memory_relation_semantics: 入口条件 probe 嵌入失败 entry=%r err=%s",
                       entry_name, exc)
        return ()
    bucket: dict[str, list[list[float]]] = {}
    for rel, v in zip(index, vecs):
        bucket.setdefault(rel, []).append(v)
    out = []
    for rel, vs in bucket.items():
        mean = _mean_vec(vs)
        if mean:
            out.append((rel, tuple(mean)))
    return tuple(out)


def score_relations_from_entry(
    query_emb: list[float], entry_name: str, vocab: str = "aggregate",
    *, relations: "tuple[str, ...] | None" = None,
) -> dict[str, float]:
    """站在 `entry_name` 这个概念上，给出每种关系的权重（**入口内 z-score 标定**）。

    ★ 这是「入口条件化的关系语义」那一步（模块 docstring 的分叉点）：

        原始分(关系) = mean_i cos( embed(query), embed(entry_name + 权重文本_i) )
        权重(关系)   = (原始分 - 本入口均值) / 本入口标准差          ← z-score

    即「入口名拼在**每条权重文本**上、逐条算相似度、再按关系取平均」（`_dot_over_qnorm` 里有
    为什么不是"先平均向量再算相似度"的完整推导）。
    query 那一侧保持干净 —— 它就是 Agent 原本的意图。

    ★ **`relations`：要给哪些关系打分**（关键字参数）。自由命名后没有"全部合法关系"这份名单，
    所以由调用方给 —— `graph._expand` 传的是**本跳 SQL 查出来的 relation 值**（排序去重）。
    不传则退回 probe 表里配了的那些（词表放开后是空的 → 返回空 dict）；
    这个默认值只服务于填回词表后的老口径调用方，热路径一律显式传。

    ★ **为什么必须做 z-score**（2026-08-08 用户定案）：原始分是余弦量级的数，而各条拼接
    文本共享同一个入口名前缀（入口名往往 20 字、probe 只有 6 字），于是所有向量被整体平移到
    高相似区，**差异只剩小数点后两位** —— 实测同一入口内 12 种关系全挤在 0.372~0.423
    （带宽 0.051）。送进 softmax 后 `exp(0.051)≈1.05`，最高/最低概率只差 **1.1 倍**，
    等于均匀分布：`expand.top_p` 无从下手，「关系语义驱动图扩展」名存实亡。
    z-score 把它换算成「离本入口均值几个标准差」（实测范围 -1.6~+1.3、带宽 3.2），
    softmax 后最高/最低概率差拉到 **约 25 倍**，头尾终于分得开。

    这不改变任何判断：z-score 是**单调变换**，关系的相对次序逐位不变（实测
    `blocked_by 1.32 > precedes 1.18 > requires 0.87` 与原始次序一致）。它只重新标定刻度。

    ⚠️ 只减均值**没有用**：softmax 对输入平移天然免疫（`exp(x-μ)` 的 `e^-μ` 是公因子，
    归一化时被约掉）。实测减均值后概率比仍是 1.1 倍 —— **必须除标准差**才改变分布形状。

    ⚠️ 这是**入口内**归一化，所以会抹掉「这个入口整体对不对题」的信息（每个入口的权重
    都被拉到均值 0）。当前算法里这是可接受的：概率是对所有入口的所有候选边**一起**做
    softmax 的，而入口间的强弱本该由 `_anchor` 的分数门（gap_ratio）与落点强度表达 ——
    不该由关系权重的绝对高度兼职表达。若将来要让"整体不对题的入口少扩展"，需另加一个
    入口级系数，不要退回绝对权重。

    ⚠️ **参照组现在是「本入口本跳实际存在的关系」**（2026-08-09，随 `relations` 参数）：
    此前是那 12 个固定关系，同一入口两次检索的 z 值可比。现在它随传入集合变化 ——
    同一个关系在"只有 2 种边"的入口上和在"有 9 种边"的入口上会拿到不同的 z。
    这对算法本身**更对题**（它要在其中做选择的就是这个集合），但意味着 z 值**跨入口/跨跳
    更加不可比** —— 别把它当成可跨调用比较的绝对分去做阈值判断。
    当前消费者只有 `_expand`，那里概率是每跳全局 softmax 一次、`rel_cache` 也是每跳新建的，
    所以不引入新问题。

    ⚠️ 返回值**可以是负数**，消费方不能再用 `weight × strength` 那种乘法
    （负权重乘大强度会让分数更低 = 强度的作用被反转）。`graph._expand` 已改为
    `z + ln(strength)`：在 softmax 里 `exp(z + ln s) = e^z · s`，强度仍是概率的
    **乘性**因子，语义不变且不翻转符号。

    这是图扩展打分的第一项（第二项是落点概念的强度，见 `graph.BrainGraph._expand`）。
    """
    if not query_emb or not entry_name:
        return {}
    if relations is None:
        table, _label = _PROBE_TABLES[vocab]
        relations = tuple(sorted(table))
    if not relations:
        return {}
    pairs = _entry_relation_vecs(entry_name, vocab, relations)
    raw = {rel: _dot_over_qnorm(query_emb, list(vec)) for rel, vec in pairs}
    if len(raw) < 2:
        # 单个关系无所谓相对位置（标准差为 0，z 无定义）→ 给 0.0 = "就是本组均值"。
        # 返回原始余弦会让它与其它入口的 z 值混在一起比较，量级不可比。
        return {rel: 0.0 for rel in raw}
    vals = list(raw.values())
    mu = sum(vals) / len(vals)
    sd = math.sqrt(sum((v - mu) ** 2 for v in vals) / len(vals))
    if sd <= _SD_EPS:
        # 全部关系同分（probe 表退化 / embedding 异常）→ 全 0，让 softmax 退回均匀分布。
        # 不能除一个≈0 的数：那会把纯浮点噪声放大成天文数字般的"区分度"。
        return {rel: 0.0 for rel in raw}
    return {rel: (v - mu) / sd for rel, v in raw.items()}
