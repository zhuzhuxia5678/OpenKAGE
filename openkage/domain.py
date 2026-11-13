"""框架 ↔ 任务域的唯一契约。

`TaskDomain` 协议把「因域而异的决策点」收成一份协议：框架侧（brain 循环 +
orchestrate/loop 编排）只写一份泛型引擎，对 domain 只做多态调用；每个具体域（插件包 domains/*）
实现这个协议，把自己的 prompt / 动作工具 / 打分 / 反思 user_input 组织内聚在自己文件里。

设计边界：本模块只定义契约，不 import 任何具体域，也不 import 记忆引擎实现。
`Memory` 用 Protocol 描述框架需要 BrainGraph 暴露的那几个能力（retrieve/render_context/reflect），
避免契约层反向依赖 memory.graph。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


# ---------------------------------------------------------------------------
# 数据契约：Task / Sample / EvalScore
# domain 产出它们，框架消费它们。各 dataclass 都留了一个 dict 字段兜住域私有留痕信息。
# ---------------------------------------------------------------------------


@dataclass
class Task:
    """一道题 / 一个交互任务 / 一次探索目标。

    - id / topic_tag：稳定标识 + 知识点(任务类型/探索阶段)标签，检索与日志都要。
    - question：题干 / 任务目标 / 探索目标（自然语言）。
    - gold：参考答案（数学）或任务完成描述（交互式任务）；探索域（minecraft）无 gold，留 None。
    - payload：域私有字段（数学的 options、minecraft 的世界/端口参数等）。
    """

    id: str
    topic_tag: str = ""
    question: str = ""
    gold: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class Sample:
    """「一次采样」的产物：数学=一次 submit_answer，Minecraft=一轮昼夜探索（交互式任务=一整条 episode）。

    - text：作答/轨迹的文本化（喂反思 Agent + 结果 JSON 留痕）。
    - correct：判分基元的布尔投影（探索域用 score>阈值 无意义，可留 None）。
    - metrics：域指标（数学的 answer_letter，Minecraft 的 unique_items/tier_rank…）。
    - primary_context / secondary_context：主脑/副脑图谱检索留痕（每次采样带回，便于结果 JSON 复盘）。
    """

    text: str
    correct: bool | None = None
    metrics: dict[str, Any] = field(default_factory=dict)
    primary_context: str | None = None
    secondary_context: str | None = None


@dataclass
class EvalScore:
    """一组 eval 的聚合分。math=正确数，minecraft=探索得分(items+tier)。
    detail 装分列指标（画广度/深度两条曲线用）。"""

    score: float
    total: int
    detail: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Memory：框架对「记忆图谱」的最小依赖面（BrainGraph / ArmGraph 都满足）
# 用 Protocol 而非 import BrainGraph，让 domain 契约不反向依赖记忆引擎实现。
# ---------------------------------------------------------------------------


@runtime_checkable
class Memory(Protocol):
    """框架/domain 需要的记忆图谱能力子集。BrainGraph 与 ArmGraph 天然满足。

    图检索有两组接口：
    - `retrieve` + `render_context`：扁平 facet 列表。rollout 的
      `build_context_messages` / `retrieve_memory_block` 按它消费（math 域走这条），
      代价是丢掉图结构——概念之间怎么连、谁是枢纽、还能往哪走，全看不到。
    - `retrieve_subgraph` / `expand_concept` + `render_subgraph`：返回 `MemorySubgraph`
      （节点 + 内部边 + 可展开的外部边 + 入口标记）。新域优先用这组；要让 Agent 自己
      沿边多跳（minecraft 的 expand 工具）只能用它。
    两组共用同一套召回/排序打点（`_collect_facets`），不会因走哪个入口而结果不一致。
    NullGraph（frozen 对照组）必须同时实现这五个，缺一个 Agent 一调就 AttributeError。

    ★ **检索不带层过滤**：「要哪一层」由 query 措辞 + hops 表达（曾短暂加过一个 `layer`
    参数，2026-08-08 用户定案撤掉 —— 它是给同一个意图开第二个入口，却要四处过滤点都对）。
    ⚠️ **dump_* 三个不在本 Protocol 里**（`dump_memory_layer` / `dump_layer_index` /
    `dump_layer_full`）—— 它们是域代码与聚合编排直接调的，Protocol 挡不住，缺了就是运行时
    AttributeError。回归钉在 tests 的 test_null_graph_satisfies_reflect_call_sites。
    """

    def retrieve(self, query: str, hops: int = 1) -> list[dict]: ...

    def render_context(self, facets: list[dict]) -> str | None: ...

    def retrieve_subgraph(self, query: str, hops: int = 1): ...

    def expand_concept(self, slug: str, relation: str | None = None): ...

    def render_subgraph(self, sg) -> str | None: ...

    async def reflect(
        self, round_num: int, user_input: str, *,
        log_tag: str = "reflect", log_extra: str = "",
    ) -> None: ...


# ---------------------------------------------------------------------------
# TaskDomain：框架 ↔ 任务域的行为契约
# ---------------------------------------------------------------------------


@runtime_checkable
class WindowHook(Protocol):
    """**题内聚合**回调：域在「一次采样内部的一个聚合边界」上调它一次。

    `await on_window(window, at)`，返回该副脑经验层被清掉的计数
    （`{"concepts","facets","edges","vectors"}`，域一般只用来记日志/留痕）。

    - `window`：该边界是本次采样里的第几个聚合窗口（**1 起**，见 `roundcode.FIRST_WINDOW`）。
    - `at`：域自己的时间刻度（minecraft = 第几天）。只进留痕，用来算记忆反馈回路的滞后
      （`lag_days`）—— 聚合是不阻塞的，「这批经验落进主脑时各副脑已跑到第几天」不记就成了暗账。

    调它会**就地同步**完成两件事（毫秒级、不含 LLM）：给该副脑图谱拍个快照、清掉它的
    **经验层**（记忆层一行不碰）。返回后域应当**立刻继续**跑下一个单位 —— 真正的聚合在攒齐
    各副脑后于后台进行，域不必也不应等它。实现见 `openkage/orchestrate/window.py`。

    ★ **只有域知道自己的采样有没有内部结构**：math 的一次采样是一问一答（无边界，忽略即可），
    minecraft 的一次采样是一整条 N 夜轨迹（每 K 夜一个边界）。所以框架不猜边界在哪，
    只把这个钩子递进去。
    """

    async def __call__(self, window: int, at: int) -> dict: ...


@runtime_checkable
class NightHook(Protocol):
    """**每晚三栅栏**回调：域在每晚反思的各阶段边界上调它一次（2026-08-29）。

    `await on_night(stage, day_idx)`：在该阶段所有臂到齐前阻塞，到齐后放行。纯等齐、不聚合、
    不返回有用值。三阶段：
    - `stage=1`：阶段1（经验/记忆反思）后 → 一起进阶段2。
    - `stage=2`：阶段2（技能维护）后 → 技能库稳定，下一天 createplan 读到一致的库。
    - `stage=3`：聚合（on_window/collect）后 → 仅聚合天数；由 WindowAggregator.collect 担任，
      本钩子的 stage=3 不单独调（collect 本身就是栅栏3）。

    ★ 解决跨臂技能库竞态：各臂 rollout 并行、天数进度独立，一臂改技能时另一臂可能还在用旧的。
      栅栏1/2 保证同阶段同步。`day_idx` 是域的时间刻度（minecraft = 第几天）。
    """
    async def __call__(self, stage: int, day_idx: int) -> None: ...


class SpawnHook(Protocol):
    """**开局出生栅栏**回调：域在 bot 出生（+附身尝试）后、迈第一步训练前调它一次（2026-09-12）。

    `await on_spawn_ready()`：在所有臂到齐前阻塞，到齐后放行。纯等齐、不聚合、不返回有用值。
    解决「各臂不等齐就开训」：录像客户端卡死的臂降级后晚出生，先出生的臂在此等齐，最后一个
    到齐放行——保证「人齐才开始」。与 NightHook 的区别：开局一次性事件，无 (stage, day_idx)。

    ★ eval/单臂路径 on_spawn_ready=None，域内 None 守卫跳过（不调本钩子）。
    """
    async def __call__(self) -> None: ...


@runtime_checkable
class TaskDomain(Protocol):
    """一个任务域 = 「怎么建题 / 怎么跑一次采样 / 怎么反思 / 怎么判分」四件因域而异的事。

    框架（OpenKAGE 循环 + orchestrate/loop）拿到一个 TaskDomain 就能跑完整的
    epoch→batch→arm→consolidate→eval 循环，全程只对 domain 多态调用，
    不出现任何 minecraft/gold_answer 等域关键词。
    """

    #: 域名（"math" / "minecraft"），用于日志/结果 JSON。
    name: str
    #: 域强制的「每 task 采样次数」。None（默认）= 用调用方/config 的 n_samples。
    #: 某些域多采样是冗余维度（如 Minecraft：task 内容全同 + 每轮 reset 世界，
    #: 「同 task 采 N 次」与「N 个 task 各采 1 次」产出等价），域自己声明 1 来关掉它。
    #: 与 parallel_arms / parallel_samples 同属「域声明的编排约束」。
    n_samples: int | None
    #: 学习曲线画法。False（默认）= 每轮训练后跑一次 evolving eval，
    #: 曲线 Y 轴是 held-out eval 分（math：eval 是一批**不同的题**，测泛化才有意义）。
    #: True = **训练曲线**：Y 轴直接取本轮各 arm 的训练得分均值，不跑每轮 eval。
    #: Minecraft 这类**终身学习**域适用 True：eval 与 train 是同一个中性探索目标（内容全同），
    #: 每轮 reset 世界 → 额外跑 eval 只是在同条件下多采一条样，不提供 train 之外的新信息，
    #: 却要付一条完整轨迹的墙钟（单 bot 串行）。训练轮次本身就是学习曲线的 X 轴。
    #: 注：为 True 时框架仍在**第 1 轮前跑一次 frozen 基线**（空记忆水平线），否则「得分上升」
    #: 无法与「世界随机性/运气」区分。
    train_curve: bool

    def prepare(self, *, model_name: str | None = None) -> "str | None":
        """跑循环前的域私有前置，返回主脑种子库路径（None = 空图起步）。

        math：备份生产 KG（可回滚 + frozen 底座）+ 导入教材种子 → 返回种子 db 路径。
        minecraft：不碰生产库、空图起步 → 返回 None。
        """
        ...

    def build_tasks(self, cfg: dict) -> tuple[list[Task], list[Task]]:
        """产出 (train_tasks, eval_tasks)。cfg 是本次 run 的域相关配置（题量/任务类型/步数预算等）。"""
        ...

    async def rollout(
        self,
        task: Task,
        *,
        primary: Memory | None,
        secondary: Memory | None,
        arm_role: str | None,
        arm_key: str | None,
        model_name: str | None,
        round_num: int,
        arm_name: str | None = None,
        on_window: "WindowHook | None" = None,
        on_night: "NightHook | None" = None,
        on_spawn_ready: "SpawnHook | None" = None,
    ) -> Sample:
        """跑「一次采样」：读侧记忆 = primary(主脑) + secondary(副脑，训练时该副脑自己的图)，
        eval 时只传 primary。arm_role 是该副脑的视角透镜文本（前置进 system prompt）。
        产出一个 Sample。所有 LLM 调用经 openkage.llm 走全局限流。

        arm_key 是该副脑的**机器 id**（如 `alice`，其 name 为 `Alice`），eval/基线路径为 None。
        与 arm_role（人设散文，喂给模型的内容）分开传：域侧需要一个**稳定短标识**来绑定
        per-arm 的进程/资源/随机流（minecraft 用它借 MC worker + 派生出生点 RNG），
        拿人设首行凑标识会得到一串认不出的中文，且人设改一个字标识就变了。

        `on_window` = **题内聚合**回调（见 WindowHook）。**一次采样内部有没有聚合边界，
        只有域自己知道** —— math 的一次采样是一问一答，没有内部结构，忽略它即可（签名里
        接住就行，框架恒传）；minecraft 的一次采样是一整条 N 夜轨迹，故每 K 夜调一次。
        eval 路径恒为 None（只读、不写图谱，无经验可聚合）。

        `on_spawn_ready` = **开局出生栅栏**回调（见 SpawnHook）：域在 bot 出生后、第一步训练前
        调一次，等齐所有臂到齐放行。eval 路径恒为 None（无并发不等齐问题）。
        """
        ...

    async def reflect(
        self,
        memory: Memory,
        task: Task,
        samples: list[Sample],
        *,
        arm_role: str | None,
        round_num: int,
    ) -> None:
        """把本任务的 N 份采样交给反思 Agent 写进副脑图谱（memory=该副脑的 ArmGraph）。

        cadence 差异内聚在此：多采样 graded 类域在这里做「分布+参考」的反思；某些域（如 Minecraft
        逐夜探索）已在 rollout 内反思过 → 这里 no-op。
        """
        ...

    def score(self, sample: Sample) -> float:
        """判分基元（用于日志/留痕/eval 聚合的单样本分）。math=对错，minecraft=探索得分。"""
        ...

    def aggregate_eval(self, samples: list[Sample]) -> EvalScore:
        """把一组 eval 采样聚合成一个 EvalScore（evolving/frozen 各调一次，gap=两者之差）。"""
        ...
