"""训练参数配置模块（OpenKAGE框架运行期参数）。

把 configs/openkage_default.yml 的加载 + 运行期取值收敛成一个不可变 TrainConfig，
yml 是运行期规模参数的唯一真相（任务域无关，三个域共用同一份参数结构）。

规模参数语义：
    train.arms      副脑个数
    train.epochs    训练轮数
    train.n_train   训练题数，"all" = 全部
    train.batches   分成几个 batch = 聚合(consolidate)几次（batch size = n_train/batches）
    train.n_samples 一个题目 rollout 几次
    eval.n_eval     评估题数，"all" = 全部
    eval.k          一次评估里每题重复跑几次（取均值压噪）

三层优先级：命令行 > smoke 节（--smoke 时）> train/eval 节，在 experiments/run.py 的 main()
里解析。argparse 的规模 flag 默认值一律 None，用于区分「未给出」与「显式给出」。
"all" 由 resolve_n() 在拿到题库大小后解析。

`RetrievalConfig`（yml 的 `memory_graph.*` 节）同理：yml 是检索参数的唯一真相，
python 侧不留默认值；`BrainGraph`/`ArmGraph` 不传时各自 `RetrievalConfig.load()` 读同一份 yml。

`LLMConfig`（yml 的 `llm.*` 节）同理，装网关 token 预算与**三层嵌套重试**。它单独成节而不
并进 memory_graph：那节是「图谱怎么检索」，这节是「怎么打网关」，两者的调参场合不同
（换 embedding 模型 vs 网关抖动），混在一起会让人以为改重试会影响召回。

`SkillConfig`（yml 的 `skill.*` 节）也单独成节：技能走各臂 SkillStore 的独立向量库，与记忆图谱
检索通道物理隔离，调参场合又不同（技能查重阈值 vs 记忆召回阈值），混进 memory_graph 同样会让人
以为两者联动。详见 SkillConfig 的 docstring。
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


#: yml 的默认路径（`RetrievalConfig.load()` 不传 path 时读它）。
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "configs" / "openkage_default.yml"


@dataclass(frozen=True)
class RetrievalConfig:
    """记忆图谱的检索/步数参数（yml 的 `memory_graph.*` 节）—— python 侧没有默认值。

    yml 是唯一真相：缺字段直接 KeyError（配置是运行前置，不做静默兜底）。
    """

    #: ── anchor（向量锚定入口，yml `memory_graph.anchor.*`）──
    #: 向量库取回多少候选；随后两道分数门取严者过滤。**入口数量不再硬截断**
    #: （原 anchor_max_entries 已删：该留几个入口由语义关系决定，不该由一个跟内容无关的数字拍死）。
    anchor_topk: int
    #: 绝对分数门。0 = 关掉（余弦绝对值不能跨 embedding 模型/语料比较，见 yml 那段推导）。
    anchor_min_score: float
    #: 相对分数门：只留跟最佳匹配差 (1-gap_ratio) 以内的。**可移植**的那个判据。
    anchor_gap_ratio: float
    #: ── expand（图扩展，yml `memory_graph.expand.*`）──
    #: 一次检索最多走几跳（**上限**，实际跳数由任务域 Agent 自己指定）。
    expand_max_hops: int
    #: 边概率的 top-p（核采样）剪枝：softmax **之后**按概率降序累加，只保留累积到 top_p
    #: 之内保留的边。1.0 = 不剪枝。剪枝放在 softmax **之后**：归一化概率有可移植的刻度，
    #: 而余弦量级的绝对门没有（换 embedding 模型就要重标）。
    expand_top_p: float
    #: 边展开概率的 softmax 温度：越小越"只走最优那条"，越大越摊平（弱边也有机会）。
    #: 每条候选边**各自按自己的概率独立**决定走不走 —— Σp=1 让扇出天然自限，
    #: 所以不需要任何条数截断（原 hop_sample_size / neighbor_budget / neighbor_fanout 已删）。
    expand_temperature: float
    #: ── 反思/聚合 ──
    #: `search_concepts` 工具的 limit 上限。**防倾泻，不是调优**：limit 由模型自己填，
    #: 而它没有理由知道图谱有多大 —— 一个 limit=100000 会把整个图谱塞进上下文。
    #: 与上面那些不同，这个值同时进**工具的 JSON schema**（maximum）和 run() 的校验。
    search_max_limit: int
    #: **三个 dump 共用**的护栏值（`dump_memory_layer` / `dump_layer_index` /
    #: `dump_layer_full`）—— 与 search_max_limit 同性质（防倾泻），不是常规截断。
    #: 共用一个值：三者是同一件事的三种形态，阈值没理由各设一个。
    #: 只作用于记忆层：经验层不设上限，靠每窗口聚合后的 `clear_layer` 收敛。
    memory_dump_max_concepts: int
    #: **记忆层注入窗口的桶预算**（yml `memory_graph.memory_dump_budgets`）：
    #: `dump_memory_layer` 按概念类别（concept.category）分桶注入，各桶独立 updated_at 倒序；
    #: 桶不满时名额回填给全局排名。四类分别为 episodic / spatial / lesson / status。
    #: ⚠️ key 必须与 schema.CATEGORIES 一致（from_dict 里校验），否则某个桶会静默拿 0 预算。
    memory_dump_budgets: dict[str, int]
    #: ── 强度增益（yml `memory_graph.strength.*`）──
    #: 只由任务域 Agent 检索时触发，进边打分（边得分 = 关系权重 × 落点强度）。
    #: **比例**是设计立场（主动 expand 要压过语义碰巧相近），绝对值无量纲。
    strength_gain_expand: float
    strength_gain_retrieve: float
    #: ── 跨 batch 去重预筛（yml `memory_graph.dedup.*`）──
    #: ⚠️ similarity 是余弦**绝对值**，与 anchor_min_score 同一个不可移植性；这里能用绝对门
    #: 只因后果良性（多提示几个非重复候选，Agent 一看便知），所以宁松不严。
    dedup_similarity: float
    dedup_top_k: int
    #: 反思 ReActAgent 步数上限（消化 1 条轨迹）。
    max_reflect_steps: int
    #: 聚合 ReActAgent 步数上限（独立于反思：聚合要融合 N 份副脑图谱，工作量更大）。
    max_consolidate_steps: int
    #: ── facet 硬上限（yml `memory_graph.facet.*`）──
    #: 单条 facet 的 `content` 字符上限：`update_facet` 整条覆盖，热 facet 会随窗口膨胀；
    #: 分片 = name + kind + content，max_chars=400 时最坏分片 480 字，仍在嵌入器 512 token 窗口内。
    #: 换 embedding 模型要连同 index.part_max_chars 一起重标。
    facet_max_chars: int
    #: 一个 Concept 最多几条 facet，同时界定索引过取倍数（每概念最多 1+本值 条分片）。
    facet_max_per_concept: int
    #: ── 分块索引的融合权重（yml `memory_graph.index.*`）──
    #: 一个概念存 **1+N** 条向量：identity = `name\nsummary`，每条 facet 各一条 `{kind}: {content}`。
    #: 检索时按概念分组融合：`score = (w_id·cos_id + w_facet·agg(cos_facet_k)) / (参与权重之和)`；
    #: 除以权重和把输出按回余弦量程，否则在旧分布上标定的门会整体位移。
    index_w_identity: float
    index_w_facet: float
    #: 多条 facet 分片怎么聚成一个分数：`max`（最匹配的那条决定概念得分 —— 命中一个细分知识点
    #: 就能拉起整个概念，这是分块索引想要的）或 `mean`（要求整体相关）。
    index_facet_agg: str
    #: 单个索引分片的字符告警线 + 截断阈值（替代原 1200 的整概念护栏）。分片 =
    #:   `name\n{kind}: {content}`（identity 分片是 `name\nsummary`）—— ★ **带 name**，
    #:   只嵌 `{kind}: {content}` 的话这条分片是没有锚点的裸句子（见 tools.`_facet_doc`）。
    #:   故最坏分片 = `facet_max_chars`(400) + name(37) + kind(≤40) + 3 = 480 字；本值取
    #:   **512 = 直接对齐 KaLM `max_seq_length=512` token**，连「1 字 = 1 token」的最悲观换算，
    #:   正常写入也不会被截断。会响的只剩 `merge_concepts` 拼接出的超长（那条是有意放行的）。
    index_part_max_chars: int

    @classmethod
    def from_dict(cls, mg: dict) -> RetrievalConfig:
        """从 yml 的 `memory_graph` 节构造。缺字段直接 KeyError —— 配置是运行前置。"""
        anchor, expand = mg["anchor"], mg["expand"]
        strength, dedup = mg["strength"], mg["dedup"]
        facet, index = mg["facet"], mg["index"]
        budgets_raw = mg["memory_dump_budgets"]
        budgets = {str(k): int(v) for k, v in budgets_raw.items()}
        # 桶 key 必须恰好是四个类别 —— 多/少/拼错都会让某个桶静默拿 0 预算。
        from openkage.memory.schema import CATEGORIES
        if set(budgets) != set(CATEGORIES):
            raise ValueError(
                f"memory_graph.memory_dump_budgets 的键必须是 {list(CATEGORIES)}，"
                f"当前是 {sorted(budgets)} —— 多/少/拼错的桶拿不到预算，等于该类别全体掉出注入窗口"
            )
        agg = str(index.get("facet_agg", ""))
        # 拼错会静默退化成另一种聚合语义（max↔mean 召回形状完全不同），必须前置报错。
        if agg not in ("max", "mean"):
            raise ValueError(
                f"memory_graph.index.facet_agg 只能是 'max' 或 'mean'，当前是 {agg!r}"
            )
        return cls(
            anchor_topk=int(anchor["topk"]),
            anchor_min_score=float(anchor["min_score"]),
            anchor_gap_ratio=float(anchor["gap_ratio"]),
            expand_max_hops=int(expand["max_hops"]),
            expand_top_p=float(expand["top_p"]),
            expand_temperature=float(expand["temperature"]),
            search_max_limit=int(mg["search_max_limit"]),
            memory_dump_max_concepts=int(mg["memory_dump_max_concepts"]),
            memory_dump_budgets=budgets,
            strength_gain_expand=float(strength["gain_expand"]),
            strength_gain_retrieve=float(strength["gain_retrieve"]),
            dedup_similarity=float(dedup["similarity"]),
            dedup_top_k=int(dedup["top_k"]),
            max_reflect_steps=int(mg["max_reflect_steps"]),
            max_consolidate_steps=int(mg["max_consolidate_steps"]),
            facet_max_chars=int(facet["max_chars"]),
            facet_max_per_concept=int(facet["max_per_concept"]),
            index_w_identity=float(index["w_identity"]),
            index_w_facet=float(index["w_facet"]),
            index_facet_agg=str(index["facet_agg"]),
            index_part_max_chars=int(index["part_max_chars"]),
        )

    @classmethod
    def load(cls, path: Path | None = None) -> RetrievalConfig:
        """读 yml（默认 configs/openkage_default.yml），**带进程级缓存**。

        缓存的理由：`BrainGraph.__init__` 不传 retrieval 时就走这里，而训练循环每个
        epoch×batch 会新建 N 份 ArmGraph —— 不缓存就是每次建图都读一遍磁盘 yml。
        """
        return _load_retrieval_cached(str(path or DEFAULT_CONFIG_PATH))


def _read_yml(path_str: str) -> dict:
    """读一份参数 yml。缺文件直接报错 —— python 侧没有默认值，缺文件无法构造。"""
    import yaml

    p = Path(path_str)
    if not p.exists():
        raise FileNotFoundError(
            f"参数配置不存在：{p}。检索/LLM 参数没有 python 侧默认值（唯一真相在 yml），"
            f"缺文件无法构造。")
    return yaml.safe_load(p.read_text(encoding="utf-8"))


@lru_cache(maxsize=4)
def _load_retrieval_cached(path_str: str) -> RetrievalConfig:
    """`RetrievalConfig.load()` 的缓存内核（key 是路径字符串 —— Path 也可哈希，
    但统一成 str 免得同一个文件的不同 Path 写法各占一份缓存）。"""
    return RetrievalConfig.from_dict(_read_yml(path_str)["memory_graph"])


@dataclass(frozen=True)
class SkillConfig:
    """技能库参数（yml 的 `skill` 节）。python 侧零默认值，yml 唯一真相——与 `RetrievalConfig`
    同纪律。

    ★ **单独成节、不进 `memory_graph`**：`memory_graph` 是「记忆图谱怎么检索」的配置，而技能走
      各臂 `SkillStore` 自己的**独立向量库**（见 skill_store.py），与记忆图谱物理隔离、检索通道
      不同。把技能参数塞进 `memory_graph` 会让人以为调记忆检索阈值会顺带影响技能，且会让
      `SkillStore`/`SkillRegistry`（它们的构造链完全不碰 `RetrievalConfig`）被迫依赖一张语义
      错位的配置表。单独成节 = 「技能库怎么查重」自成一处调参场合，与 LLMConfig 单独成节同理。
    """

    #: create_skill 时按 intent 跨臂语义检索，相似度 ≥ 此值的已有技能会塞进返回 `warning`
    #: （**只警告不拦**，宁松不严）。与 `RetrievalConfig.dedup_similarity` 同理辩护：余弦绝对值
    #: 不可跨 embedding 模型/语料移植，但后果良性——多警告一次、Agent 一看便知真重复还是同领域
    #: 合理分工，故不设严门、不自动合并。
    similar_warn_threshold: float

    #: 执行期签名块默认注入的技能条数（`_inject_all_skills` 签名层）。
    #: 选法 = **全局排序取前 N**：`total_success_count`（全程累计成功，改代码也不清零）
    #: ↓ → `success_count`（当前版本窗口）↓ → `updated_at` ↓ → `created_at` ↓ → `slug` ↑。
    #: 被验证好用的技能稳定顶在签名块前面，不随当天 goal 轮换（成功次数越多越重要）。
    #: **替代旧硬编码 top-8 goal 余弦检索**——goal 与选择无关后，签名块每天固定，让好技能
    #: 稳定曝光；范围外技能代码层仍全量注入，Agent 想用可调 retrieve_skills 查签名后 await。
    signature_topk: int

    @classmethod
    def from_dict(cls, sk: dict) -> "SkillConfig":
        """从 yml 的 `skill` 节构造。缺字段直接 KeyError —— 配置是运行前置，不做静默兜底。"""
        return cls(
            similar_warn_threshold=float(sk["similar_warn_threshold"]),
            signature_topk=int(sk["signature_topk"]),
        )

    @classmethod
    def load(cls, path: Path | None = None) -> "SkillConfig":
        """读 yml（默认 configs/openkage_default.yml），**带进程级缓存**。

        缓存理由同 `RetrievalConfig.load`：`BrainGraph` 在训练循环里每 epoch×batch 新建 N 份
        `SkillRegistry` 时若各自读一遍磁盘 yml，是纯浪费。
        """
        return _load_skill_cached(str(path or DEFAULT_CONFIG_PATH))


@lru_cache(maxsize=4)
def _load_skill_cached(path_str: str) -> SkillConfig:
    """`SkillConfig.load()` 的缓存内核（key 是路径字符串，同 `_load_retrieval_cached`）。"""
    return SkillConfig.from_dict(_read_yml(path_str)["skill"])


#: 三个角色的键名（也是 yml `train.model` / `llm.sdk.timeout` / `llm.reasoning_effort`
#: 下的子键名）。顺序即日志/报错里的展示顺序。
#: ★ 为什么是**这三个**而不是别的切分：它们是三个**不同的 Agent**，各自有独立的
#:   system prompt、工具集、步数预算（见 `RetrievalConfig.max_reflect_steps` /
#:   `max_consolidate_steps` 已经分开的那两个），换模型/调 thinking 的诉求天然落在这个粒度上。
MODEL_ROLES = ("task", "reflect", "consolidate")

#: `reasoning_effort` 的合法档位，**由服务端校验**（实测 `'banana'` / `''` 都回
#: 400 invalid_request_error）—— 所以拼错是硬失败而不是静默忽略，值得在配置层提前拦。
#: ⚠️ 顺序即"思考量"从小到大，但**耗时对档位不单调**（实测 reflect 上 low 比 minimal 快
#:    一倍多，consolidate 上反而慢三倍）—— 别按这个顺序推断快慢，每个角色各自实测。
REASONING_EFFORTS = ("none", "minimal", "low", "medium", "high")


def _role_dict(raw, *, what: str, cast) -> dict:
    """把 yml 里一个**按角色分**的子节解析成 `{role: 值}`，缺/多角色都直接报错。

    ★ 与 `ModelConfig.from_dict` 同一条纪律：**不接受裸标量**（"给一个值就三个角色都用它"）。
      那会让 yml 里同时存在两种合法写法，而读配置的人无法从 `timeout: 60` 这一行看出三个
      角色是不是同一个值 —— 正是本仓反复吃过亏的静默双轨（见 `RetrievalConfig` 的 docstring）。
      要三个一样就把值写三遍（显式胜于省字）。
    """
    if not isinstance(raw, dict):
        raise ValueError(
            f"{what} 必须是按角色分的 dict（{'/'.join(MODEL_ROLES)}），收到 "
            f"{type(raw).__name__}: {raw!r}。**不支持裸标量** —— 三个角色要用同一个值就把它"
            f"写三遍（理由见 _role_dict 的 docstring）"
        )
    missing = [r for r in MODEL_ROLES if r not in raw]
    unknown = [k for k in raw if k not in MODEL_ROLES]
    if missing or unknown:
        raise ValueError(
            f"{what} 的角色键不对：缺 {missing or '无'}、多 {unknown or '无'}；"
            f"必须恰好是 {list(MODEL_ROLES)}"
        )
    return {r: cast(raw[r]) for r in MODEL_ROLES}


def _role_value(d: dict, role: str, *, what: str):
    """从按角色分的 dict 取值。未知 role **直接报错**，不静默回落。

    静默回落（比如 `d.get(role, d['task'])`）会让"新增了第四个角色但忘了配"表现成
    "它悄悄用了 task 的值"，而那正是最难查的一类配置漂移。
    """
    if role not in d:
        raise KeyError(
            f"{what} 没有角色 {role!r} 的值（有 {sorted(d)}）—— 新增角色要同步补 yml，"
            f"这里不回落到任何其它角色的值"
        )
    return d[role]


@dataclass(frozen=True)
class LLMConfig:
    """LLM token 预算 + 三层重试参数（yml 的 `llm.*` 节）—— python 侧没有默认值。

    重试是三层嵌套的，最坏尝试次数 =
    `rollout_attempts × api_error_retries × (sdk_max_retries + 1)`；
    yml 让这个乘积在一处可见，调参时先确定在调哪一层。

    `max_tokens_answer` 与 `max_tokens_reflect_step` 是一对：`llm._ensure_max_tokens`
    按 role 区分补哪个——task 补 answer、reflect/consolidate 补 reflect_step（调用方显式
    传了 max_tokens 则不动）。故 reflect_step 必须明显小于 answer（见 `__post_init__`）。

    `sdk_timeout` 与 `reasoning_effort` 都按角色分：三个角色的单步性质不同，取值一律走
    `sdk_timeout_for(role)` / `reasoning_effort_for(role)`，别直接读字段。

    `reasoning_effort` 是"不配就会坏"的参数：不传时 reasoning 模型走最高档，而 reasoning
    token 与可见输出共用 `max_tokens`，首步会吃满预算返回 `finish_reason="length"` 且没有
    tool_call。`sdk.timeout` 必须按冷启动首步的时间量级设定；耗时对档位不单调，每个角色
    需要各自确认。
    """

    #: 任务域 rollout 的长推导/长动作预算（rollout 显式传给 LLM）。
    max_tokens_answer: int
    #: 反思/聚合**单步工具调用**的预算（调用方不传 max_tokens 时补的默认上限）。
    max_tokens_reflect_step: int
    #: 夜间指导文本这类短自由文本生成的预算（不走工具调用）。
    night_note_max_tokens: int
    #: ── ① SDK 层 HTTP 重试（openkage/llm.py 里的）──
    #: **按角色分**的单次调用超时（秒），键 = MODEL_ROLES。经 `sdk_timeout_for()` 取值。
    sdk_timeout: dict
    sdk_max_retries: int
    sdk_retry_base_delay: float
    sdk_retry_max_delay: float
    #: ── ② 网关畸形响应层（choices=[] → finish_reason="error"，不抛异常）──
    api_error_retries: int
    api_error_base_delay: float
    #: ── ③ rollout 层（强制 function-calling 没拿到 tool_call）──
    rollout_attempts: int
    rollout_call_delay: float
    #: thinking 档位，**按角色分**（键 = MODEL_ROLES）。经 `reasoning_effort_for()` 取值。
    reasoning_effort: dict
    #: 采样温度，**按角色分**（键 = MODEL_ROLES）。经 `temperature_for()` 取值。
    #: ★ 此前没有这字段、temperature 被 `_strip_unsupported` 静默剥掉 = 裸跑网关默认；
    #:   现显式按角色定（task 偏确定 / reflect/consolidate 归纳带多样性，见 yml 那段）。
    temperature: dict

    def __post_init__(self) -> None:
        """钉住按 role 区分补预算那个机制赖以成立的前提。

        `_ensure_max_tokens` 按 role 区分：task 补 answer、reflect/consolidate 补 reflect_step，
        而这个区分只在 reflect_step < answer 时才有意义 —— 两者相等的话 task 和 reflect 拿到
        一样的预算，区分失效，改 yml 的人很容易把它们调成一样却看不出机制已经失效。配置是
        运行前置，这里直接报错。
        """
        if self.max_tokens_reflect_step >= self.max_tokens_answer:
            raise ValueError(
                f"llm.max_tokens.reflect_step({self.max_tokens_reflect_step}) 必须小于 "
                f"llm.max_tokens.answer({self.max_tokens_answer})：前者是反思/聚合的单步工具"
                f"调用预算、后者是 rollout 的长推导预算，二者靠大小关系区分用途"
                f"（见 openkage/llm.py 的 _ensure_max_tokens）"
            )

    @property
    def worst_case_attempts(self) -> int:
        """三层嵌套下单次逻辑调用最坏会打网关几次（供日志/诊断，别拿它当预算用）。"""
        return (self.rollout_attempts * self.api_error_retries
                * (self.sdk_max_retries + 1))

    def sdk_timeout_for(self, role: str) -> int:
        """该角色的单次调用超时（秒）。未知 role 直接报错，不静默回落到某个角色的值。"""
        return int(_role_value(self.sdk_timeout, role, what="llm.sdk.timeout"))

    def reasoning_effort_for(self, role: str) -> str:
        """该角色的 thinking 档位（`none`/`minimal`/`low`/`medium`/`high`）。

        取值一律经这里，**别直接读 `reasoning_effort` 字段** —— 那是个 dict，直接读会
        在"忘了按角色取"的时候把整个 dict 当值传给网关（400），或更糟：传给某个不校验
        的模型被静默忽略。
        """
        v = str(_role_value(self.reasoning_effort, role, what="llm.reasoning_effort"))
        if v not in REASONING_EFFORTS:
            raise ValueError(
                f"llm.reasoning_effort.{role}={v!r} 不是合法档位，必须是 "
                f"{list(REASONING_EFFORTS)} 之一。★ 这个枚举**由网关校验**（实测非法值回 "
                f"400 invalid_request_error），拼错不会被静默忽略、而是整个 run 打不出第一个"
                f"请求 —— 所以这里提前拦住，别等跑起来才发现"
            )
        return v

    def temperature_for(self, role: str) -> float:
        """该角色的采样温度。

        取值一律经这里，**别直接读 `temperature` 字段** —— 那是个 dict，直接读会在"忘了按角色取"
        的时候把整个 dict 当值传给网关（或被静默忽略），与 `reasoning_effort_for` 同款理由。
        float 无枚举校验（不像 effort 由网关校验合法档位），只拦未知角色（走 `_role_value` 直接 KeyError）。
        """
        return float(_role_value(self.temperature, role, what="llm.temperature"))

    @classmethod
    def from_dict(cls, llm: dict) -> LLMConfig:
        """从 yml 的 `llm` 节构造。缺字段直接 KeyError —— 配置是运行前置。"""
        mt, sdk, gw, ro = llm["max_tokens"], llm["sdk"], llm["api"], llm["rollout"]
        return cls(
            max_tokens_answer=int(mt["answer"]),
            max_tokens_reflect_step=int(mt["reflect_step"]),
            night_note_max_tokens=int(llm["night_note_max_tokens"]),
            sdk_timeout=_role_dict(sdk["timeout"], what="llm.sdk.timeout", cast=int),
            sdk_max_retries=int(sdk["max_retries"]),
            sdk_retry_base_delay=float(sdk["retry_base_delay"]),
            sdk_retry_max_delay=float(sdk["retry_max_delay"]),
            api_error_retries=int(gw["error_retries"]),
            api_error_base_delay=float(gw["error_base_delay"]),
            rollout_attempts=int(ro["attempts"]),
            rollout_call_delay=float(ro["call_delay"]),
            reasoning_effort=_role_dict(
                llm["reasoning_effort"], what="llm.reasoning_effort", cast=str),
            temperature=_role_dict(
                llm["temperature"], what="llm.temperature", cast=float),
        )

    @classmethod
    def load(cls, path: Path | None = None) -> LLMConfig:
        """读 yml（默认 configs/openkage_default.yml），带进程级缓存。

        与 `RetrievalConfig.load()` 同一个策略：`get_llm` / rollout 在热路径上被反复
        调用，不缓存就是每次都读一遍磁盘。
        """
        return _load_llm_cached(str(path or DEFAULT_CONFIG_PATH))


@lru_cache(maxsize=4)
def _load_llm_cached(path_str: str) -> LLMConfig:
    """`LLMConfig.load()` 的缓存内核（与 `_load_retrieval_cached` 同形）。"""
    return LLMConfig.from_dict(_read_yml(path_str)["llm"])


@dataclass(frozen=True)
class ModelConfig:
    """三个角色各自的底座模型（yml 的 `train.model` 节）—— python 侧没有默认值。

    按角色分是因为三个 Agent 的负载性质不同：
      · task        每天几十步串行，步数多、单步短 → 吞吐优先
      · reflect     每天一次，消化一条轨迹
      · consolidate 每窗口一次，融合 N 份副脑图谱，输入最大、最吃推理质量

    `train.model` 只接受 dict，不接受裸字符串：缺角色直接报错，要三个一样就把同一个名字
    写三遍。

    注意两点：长上下文不等于最坏情况，冷启动首步（`[system, user]`、零 assistant 轮次）
    才是——reasoning 模型在首步可能吃满 `max_tokens` 返回 `finish_reason="length"` 且没有
    tool_call；靠 `llm.reasoning_effort`（按角色）压住思考量，见 `LLMConfig`。另外部分
    reasoning 模型不支持强制 `tool_choice={"type":"function"}`；`rollout.call_tool_once`
    在收到 400 后会自动降级为 `"auto"`。
    """

    #: 任务域 rollout（做任务的那个 Agent）。MC 域即每天的探索 ReAct。
    task: str
    #: 反思 Agent（消化一条轨迹 → 写**副脑**图谱，`ArmGraph.reflect`）。
    reflect: str
    #: 聚合 Agent（融合 N 份副脑经验 → 写**主脑**图谱，`BrainGraph.consolidate`）。
    consolidate: str

    def __post_init__(self) -> None:
        for role in MODEL_ROLES:
            v = getattr(self, role)
            if not isinstance(v, str) or not v.strip():
                raise ValueError(
                    f"train.model.{role} 必须是非空模型名字符串，收到 {v!r}"
                )

    @property
    def all_same(self) -> bool:
        """三个角色是否用同一个模型（供日志把三行压成一行，不参与任何取值逻辑）。"""
        return len({getattr(self, r) for r in MODEL_ROLES}) == 1

    def summary(self) -> str:
        """给日志/结果 json 用的一行摘要。"""
        if self.all_same:
            return f"{self.task}（三角色同一）"
        return " ".join(f"{r}={getattr(self, r)}" for r in MODEL_ROLES)

    def as_dict(self) -> dict:
        """落进 results/<run>.json 的形态 —— 复盘时要知道每个角色实际用了什么。"""
        return {r: getattr(self, r) for r in MODEL_ROLES}

    @classmethod
    def from_dict(cls, model: dict) -> ModelConfig:
        """从 yml 的 `train.model` 节构造。缺角色/多写角色都直接报错 —— 配置是运行前置。

        校验走共用的 `_role_dict`（`llm.sdk.timeout` / `llm.reasoning_effort` 同一条纪律）：
        多写也报错的理由是 `model: {task: ..., reflct: ...}`（拼错）在"只查缺键"下会被报成
        "缺 reflect"，但人看着 yml 里明明有一行 reflct 会以为配置生效了。把未知键一并列出来，
        错误信息才指向真正的手误。
        """
        return cls(**_role_dict(model, what="train.model", cast=lambda v: str(v).strip()))


@dataclass(frozen=True)
class TrainConfig:
    """一次 run 的全部可调参数（memory_graph / llm / train / eval / smoke）。

    规模参数语义见 yml 头注释：
      train.arms / epochs / n_train / batches / n_samples / model
      eval.n_eval / k
    `n_train`、`n_eval` 支持字符串 "all"（用全部题）——由 resolve_n() 在拿到题库大小后解析。

    `domain_scale` 是**域私有**规模（`{域名: {...}}`），框架不解释其语义，见该字段注释。
    """

    # 记忆图谱的检索/步数参数，见 RetrievalConfig。**主脑与副脑共用同一个实例** ——
    # 此前副脑不传、落到 python 默认值，导致两边口径静默分叉（见 RetrievalConfig 的 docstring）。
    memory_graph: RetrievalConfig
    # 网关 token 预算 + 三层重试，见 LLMConfig。llm.py / rollout.py 不传时自己 load() 同一份 yml。
    llm: LLMConfig
    # 训练侧规模（arms/epochs/n_train/batches/n_samples/model）。
    train: dict
    # 评测侧规模（n_eval/k）。
    eval: dict
    # --smoke 规模覆盖（只含要覆盖的键）。
    smoke: dict
    # 域私有规模：{域名: {参数: 值}}（如 minecraft 的 max_nights / aggregate_every）。
    # ★ **框架层不解释它的语义**，原样透传给 runner，由 `_build_domain` 交给对应域的构造函数
    #   —— 「一条轨迹几个夜晚」这类参数只有域自己认识，放进 train 节会污染那份任务域无关的结构。
    #   缺节/缺域按空字典处理：域自己的构造函数签名里有默认值，且不是每个域都需要私有规模。
    domain_scale: dict
    # 原始 dict，保底供未来新增字段读取。
    raw: dict

    def domain(self, name: str) -> dict:
        """取某个域的私有规模（无则空字典）。调用方拿到后逐键 `.get(k, 默认)`。"""
        return dict(self.domain_scale.get(name) or {})

    @property
    def models(self) -> ModelConfig:
        """三个角色的底座模型（yml `train.model`）。

        走 property 而不是 dataclass 字段：`train` 那一节是原样透传的 dict（runner 还要在
        它上面叠命令行/smoke 覆盖），把 model 拆成字段会出现"dict 里那份和字段那份谁是真的"
        的双轨。这里每次从当前 `self.train` 解析，覆盖完再读也一定是最终值。
        """
        return ModelConfig.from_dict(self.train["model"])

    @property
    def n_samples(self) -> int:
        """一个题目 rollout 几次（OpenKAGE 侧按 cfg.n_samples 取值，未显式传参时的回落）。"""
        return int(self.train["n_samples"])


ALL = "all"


def resolve_n(value, total: int, *, what: str = "n") -> int:
    """把 n_train / n_eval 的配置值解析成实际题数。

    支持 "all"（大小写不敏感，也接受 "ALL"）= 用全部 total 题；其余按 int 取，
    并 clamp 到 [0, total] —— 配置写超了不报错，但会 clamp 并由调用方日志说明，
    因为「题库比配置少」是常态（换数据集/子集时），中断整个 run 不值得。
    """
    if isinstance(value, str):
        if value.strip().lower() == ALL:
            return total
        try:
            value = int(value.strip())
        except ValueError as e:
            raise ValueError(
                f"{what} 只能是整数或 \"all\"，收到 {value!r}"
            ) from e
    n = int(value)
    if n < 0:
        raise ValueError(f"{what} 不能为负数，收到 {n}")
    return min(n, total)


def load_config(path: Path) -> TrainConfig:
    """加载训练参数 yml 并构造 TrainConfig。缺文件/字段直接报错——配置是运行前置，不做静默兜底。"""
    import yaml

    cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
    # 检索参数的解析逻辑只有一份（RetrievalConfig.from_dict）—— 图谱侧不传 retrieval 时
    # 走 RetrievalConfig.load() 读同一份 yml、同一个解析器，不存在第二条口径。
    memory_graph = RetrievalConfig.from_dict(cfg["memory_graph"])
    # train/eval 两节都是必需的：配置是运行前置，缺键直接 KeyError 报出来比静默用错规模好。
    return TrainConfig(
        memory_graph=memory_graph,
        llm=LLMConfig.from_dict(cfg["llm"]),
        train=cfg["train"],
        eval=cfg["eval"],
        smoke=cfg.get("smoke", {}),
        # 域私有规模是**可选**节：不是每个域都有私有规模参数（math 就没有），缺了按空字典
        # —— 与 train/eval 的「缺键直接报错」不同，因为那两节是所有域都必需的。
        domain_scale=cfg.get("domain_scale", {}) or {},
        raw=cfg,
    )
