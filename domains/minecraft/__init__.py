"""Minecraft 开放世界探索域（原 solve.py 的 minecraft 部分 + reflect/evaluate 的 minecraft 分支）。

「一次采样」= 一整轮昼夜探索：白天 N 步 obs→生成JS代码→执行→观察；每个夜晚触发
night_cb —— ① 生成次日指导文本拼回对话，② 把当日轨迹写进副脑图谱（探索版反思，无对错）。
判分/eval = 探索得分（unique_items + W·tier_rank），无 won/correct 二元。env 封装在 .env。
tier_rank 取**沿轨迹高水位**、判定物为**合成成品工具**（对齐 Voyager 语义，见 env.TECH_TIERS
注释）；对外对报 Voyager Table 1 用 tier_first_step / tier_unlock_rate。

★ cadence 差异内聚在此：反思发生在 rollout 的 night_cb 里（逐夜），故 domain.reflect 是 no-op
——这正是把「反思时机」这个域差异从框架编排里拿掉、收进域自己的关键。

术语提示（本域「轮」有两义，靠限定词区分；框架侧全套约定见 openkage/brain.py 顶部）：
- **「一轮探索」= 一次 rollout** = 一整轮昼夜（本域的「一次采样」）。写作时务必带「探索」二字。
- **「训练轮次 / epoch」** = 全部训练题跑一遍。本域 n_train 被钉为 1，故 1 epoch = 1 轮探索
  ×arms 条 —— 两者数值上巧合，但语义不同，别混着写。
- 图谱层的 `round_num`（= `roundcode.encode(epoch,batch,window)`）与上面两者都不是一回事。
- **「每 K 夜聚合一次」= window（题内聚合）**，见 `openkage/orchestrate/window.py` 与
  yml 的 `domain_scale.minecraft.aggregate_every`。
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import shutil
from pathlib import Path
from typing import Any

from openkage.agent_io import ContextDumper, dump_reasonings
from openkage.domain import EvalScore, Sample, Task

# mc_server 目录（_skill_js_syntax_error 找 acorn 用；与 MinecraftDomain 构造默认 server_dir 同源）
_MC_SERVER_DIR = Path(__file__).resolve().parent / "mc_server"


class SkillSyntaxError(RuntimeError):
    """技能库存在 JS 语法损坏的技能（fail-fast）。

    _inject_all_skills 逐技能 acorn 校验，发现坏技能当场抛出（不隔离、不降级）。
    这是「环境/数据级故障」信号，**必须穿出所有 broad-except 层**，沿 rollout →
    loop.py 的「rollout 失败 = abort 整 run」路径立刻停整 run。停后人工修库或 demote
    坏技能，--resume-window N 续跑。继承 RuntimeError 而非更基类：不会被
    `except ValueError`（工具参数校验）误捕。
    """


class HealFuseError(RuntimeError):
    """连续自愈失败熔断。

    env.py `_run_step_body` 的 resume/step 两处 broad-except 会把自愈失败降级为
    「喂 agent 一条错误反馈」继续下一步，mineflayer 自愈死循环在这条路径上可能长期空转。
    此异常在连续失败达到 _MAX_HEAL_FAIL_STREAK 时抛出，与 SkillSyntaxError 同款
    「环境级故障」语义：**必须穿出所有 broad-except 层**（tool registry 的
    async_execute 已按 _ABORT_EXCEPTION_NAMES 放行，见 registry.py），停整 run——
    checkpoint 完整，人工查日志后 --resume-window N 续跑。
    """


class WorldStallError(RuntimeError):
    """世界时钟停摆熔断。

    env.py 时钟停摆护栏（timeTicks 连续 STALL_ABORT_STEPS 步不涨）必须抛一个在
    registry 的 _ABORT_EXCEPTION_NAMES 放行名单里的异常：否则会被工具层 broad-except
    吞成普通错误反馈喂回 LLM，agent 在死循环里继续 probe，护栏每步重抛每步被吞。
    此异常与 HealFuseError 同款「环境级故障」语义：**必须穿出所有 broad-except 层**
    （见 registry.py），达阈值即 abort 整 run 留人工——checkpoint 完整，查日志后
    --resume-window N 续跑。
    """

from openkage.orchestrate.checkpoint import (
    checkpoint_path, read_progress, read_window_progress,
    latest_window_with_world,
    write_arm_graph_checkpoint, write_progress,
)
from openkage.orchestrate.window import WindowAggregationError
from openkage.config import LLMConfig
from openkage.llm import get_llm
from openkage.logctx import current_scope
from openkage.memory.prompt_store import REFLECT
# `call_tool_once` / `retrieve_memory_block` 不再需要：前者是「每步一次 write_code」旧调用
# 入口，后者是「每步前置 RAG」的取块函数（检索现在走 CreatePlanTool 的 memory_hook 与
# Agent 自己的 retrieve/expand）。两者在 openkage.rollout 里仍导出 —— math 域还在用 call_tool_once。
from openkage.rollout import CALL_DELAY_SECONDS, build_context_messages

from .env import (
    _death_line,
    load_control_primitives,
    load_wait_until_primitive,
    progress_frontier,
    run_exploration,
)
from .prompt_store import REACT
from .worker_pool import MinecraftWorldPool, provision_workers

logger = logging.getLogger("domain.minecraft")

_TIER_WEIGHT = 5.0  # eval：tier_rank 每升一级折算成的 unique_items 当量（深度 vs 广度权衡）
# 注：tier 判定按 Voyager 语义（rank 上限 4：none=0…diamond=4），各级都难达成得多，深度项
# 在 score 里的实际贡献比表面权重看起来小。权重仍留 5.0 未调；若后续发现广度项完全盖住深度项，
# 再单独议这个数，并记为一次指标定义变更。

#: 每次死亡在 score 里扣多少 unique_items 当量，见 MinecraftDomain.score。
#: 取 3.0 的理由：一夜的探索净增大约几种独特物品，取 3.0 使「死一次」≈ 抹掉一夜的广度收益 ——
#: 够疼到让谨慎有价值，又不足以让「怕死躲着不动」占优（躲一夜的收益是 0，仍然差于「冒险探到
#: 几种但死一次」）。这个不等式是选值的判据，调它时要重新验算，别只调数字。
#: ⚠️ 改这个值 = **一次指标定义变更**，新旧 run 的曲线不可比。
_DEATH_PENALTY = 3.0

# 单步代码时限的默认/上限（与 mc_server/index.js 的 STEP_CODE_TIMEOUT_* 保持一致；Node 侧会 clamp
# 兜底）。**单位是游戏刻**（game ticks）：Agent 生活在 MC 时间里——世界以 20 刻/秒流逝、一天
# 24000 刻，时限也按刻给与执行，对它没有任何「真实秒」概念。bot 每步自选 —— 慢动作（采集/寻路）
# 配长时限、轻动作配短时限，由它自己判断。
_STEP_TIMEOUT_DEFAULT_TICKS = 500
# 上限 3000 刻：覆盖 prompt 指导的最重合法动作（远距寻路/探索「上千刻」），同时把单个失控步
# 的代价封顶（2x 下 75s 墙钟）。★ 不再用 10000 刻（旧值=夜窗长度，原意是「一步等到天亮」）：
#   实测 Agent 主动过夜用 bot.sleep 跳夜（夜注入 header 轻引导「夜里也有能做的事」、不强推），
#   不会用 waitUntil 真等一夜，10000 刻是无人调用的安全网；且 Agent 倾向
#   「能申多申」——长步 step26/27 申请 10000 刻却 17-21s 就完成、真实消耗 <1000 刻。降到 3000 既够用又逼 Agent 按动作大小估时限。
# ⚠️ **仍必须 < 夜晚段 8400 刻**（官方 night 段 13800-22200）：一旦上限 ≥8400，一个步就能
#   「白天起步→吞掉整夜→又在白天结束」，两端 is_night 都 False，那次黎明永不出现在步边界上
#   → 那天不结束、不反思、n_nights 不涨。
#   详见 mc_server/index.js 里 STEP_CODE_TIMEOUT_MAX_TICKS 的注释（那里另有步内夜→昼跳变熔断
#   dayBreakPromise 作双保险）。改这个值要与 Node 侧同步改，否则以较小者生效。
_STEP_TIMEOUT_MAX_TICKS = 3000


def _signature_sort_key(sk: dict) -> tuple:
    """签名层全局排序键：成功次数越多越重要，其次更新/创建时间新近，最后 slug 兜底。

    返回元组用于 `sorted(..., key=_signature_sort_key)`，**升序**取负数实现 DESC：
      total_success_count DESC → success_count DESC → updated_at DESC → created_at DESC → slug ASC。
    - total_success_count（全程累计成功，改代码也不清零）：最能代表技能整体可靠性，主键。
    - success_count（当前代码版本窗口）：偏向「现在这版代码好不好用」。
    - updated_at / created_at：计数相同时新近优先（覆盖新建/修订/计数回填）。
    - slug：纯确定性兜底，保证可复现。
    字段用 `.get(k, 0) or 0` 兜底（缺字段时不崩）。
    """
    return (
        -int(sk.get("total_success_count", 0) or 0),
        -int(sk.get("success_count", 0) or 0),
        -int(sk.get("updated_at", 0) or 0),
        -int(sk.get("created_at", 0) or 0),
        sk.get("slug", ""),
    )

# 域级 system prompt（全副脑共用的客观世界规则）。
# ★ 正文在 `prompts/react.yaml` 的 `system:` 块里：这个 Agent 的全部提示词 ——
#   system、5 个工具、工具返回文案、每轮开场话、夜反思 —— 都在一份文件里。
#   装载走框架的 `AgentPrompts`（见 .prompt_store 的模块 docstring）。
# **标题行在正文第一节里**（`## 1. 你是谁`），与 rollout.build_context_messages 给人设加的
# 「【你的角色人设】」对称 —— 两条 system 发送前会被 llm._merge_system_messages
# 合并成一条，没有标题时模型分不出哪句是共用硬规则、哪句是本副脑个性。
#
# system prompt 在构造函数渲染：default/max ticks 来自域实例配置，rollout 统一引用
# `self._system_prompt`。render_system 的占位符校验不变：传多了/漏传了当场抛错。

# 真·自由探索：所有轮用同一个**中性目标** + **阶段无关统一标签**，不给「先找树/再挖矿」这类
# 脚手架 —— 探索方向完全靠记忆 + 夜反思自涌现（正是「记忆替代 curriculum」要验证的：evolving
# 组能否自己长出阶段感）。question 仅作记忆检索 query + 中性 objective，不编码任何阶段顺序；
# topic_tag 统一为 "explore"，只用于检索命名空间。
#
# ★ **有意置空**（2026-08-17 用户定案）：三个长期目标（生存/探索/建造）已经完整写在
#   `prompts/react.yaml` 的 `system` 节里，那才是唯一出处。这里再写一句"尽可能发现更多物品、
#   推进科技树"是**同一件事的第二个出处**，而且更糟 —— 它比 yaml 里那份窄得多（只提物品和
#   科技树，不提建造和生存），一旦两者同时可见，等于给 Agent 两个不一致的目标描述。
#   空字符串在下游是**显式的"没有额外目标"**：reflect 的 `长期目标：` 那一行由 `when:` 守卫，
#   不会拼出一个空洞（见 reflect.yaml `minecraft_daily`）。
#   ⚠️ 这个字段留着不删，是为后续做 Voyager ④「zero-shot 指定任务」预留位置：那时每个 eval
#   task 会带**各自具体的** question（如"造一把钻石镐"），届时 gold 也要一起给上。
_EXPLORE_OBJECTIVE = ""
_EXPLORE_TAG = "explore"

# 第 1 天（还没有昨夜夜反思产出）的【昨日复盘与今日目标】固定开场（只有目标段、无复盘）。
# 不编码阶段顺序（不写「先找树/再挖矿」），与 _EXPLORE_OBJECTIVE="" 同一意图：探索方向靠
# 记忆 + 夜反思自涌现。第 1 天夜末的夜反思产出即成为第 2 天的【昨日经历】，故这个开场仅第 1 天用。
# ★ 2026-09-13 改（用户定）：删掉「今天是你第一天来到这个世界。」——续跑首天（关上下文管理时
#   所有天都走此兜底）套这句第1天开场会与「今天是第13天」矛盾。保留后半句作为非空合法目标，
#   不破坏「day_goal 必须非空」约束与兜底链。
_DAY1_GOAL_OPENING = (
    "围绕「生存 / 探索 / 建造」三个长期方向，自由决定今天要取得什么进展。"
)

# 「次日目标」镜像调用的重试次数（返空/异常都重试）。天亮反思期间世界冻着（/tick freeze），
# 不能无限挂——故给有限次（5），仍失败则用 _DAY1_GOAL_OPENING 兜底（一段合法目标，不让
# day_start 退化成「无目标的场景B」、不让 night_cb 返空崩下游）。
_NIGHT_GOAL_RETRIES = 5


def _day_events_txt(resume: bool, reflection_txt: str,
                    events: list[tuple[str, str]]) -> str:
    """算 day_start / day_resume 的「上一步执行反馈」块文本（与 react.yaml 的 events_txt 块对应）。

    「昨夜复盘」（reflection_txt）与「上一步执行反馈」（events_txt）在模板里互斥，py 侧算完给值。

    ★ **新一天（not resume）恒空**（2026-09-10 修跨天 last_events 残留污染）：
      day_start 的「上一步执行反馈」按设计本就「仅第 1 天/边界非空」，而第 1 天 last_events 来自
      spawn 初始 [] / 续跑注入的 dawn_reflection（后者 reflection_txt 非空已跳过）——合法路径下
      新一天 events_txt 都该空。若非空，必是跨天未清的 last_events 残留：env 的 _react_day_loop 进
      新一天前不清 last_events，前一日界熔断步的 [onChat] 跨过午夜了 / [onLog] … 噪音会原样漏进次日。
      实验组靠 reflection_txt 非空互斥挡住；0909-0910 对照组（no_graph）夜反思强制
      reflection="" → reflection_txt 恒空 → 旧互斥兜底失效，残留旧步反馈漏出（2026-09-10
      实测 glm nograph 4/5 臂 day02 污染）。
      根治=新一天直接判空，不依赖「reflection_txt 占位」这类对某组不成立的假设
      （0911 对照组已归还夜反思，但本判空仍保留——它本来就是正确语义）。
    ★ **续跑（同一天）保留原逻辑**：续跑时 last_events 是当天当前步反馈，语义正确，仍照发
      （resume 时 reflection_txt 在调用前已被置空，故这里不再判 reflection_txt）。
    """
    if not resume:
        return ""
    if not events:
        return ""
    return "\n".join(f"  [{k}] {v[:300]}" for k, v in events)



class MinecraftDomain:
    """Minecraft 开放世界探索域。实现 TaskDomain 协议。持有一个 **worker 池** + programs + 昼夜预算。

    ★ **副脑并行**：每个副脑一台独立 MC server + 一个独立 mineflayer（worker 池，
    见 worker_pool.py），故 `parallel_arms=True`，墙钟从 `arms × epochs × 一条轨迹` 降到
    `epochs × 一条轨迹`——重生世界要停整个 MC server，给每人一台 server 才能把重生的影响面
    收缩到单个 worker 内。
    同 arm 内的多次采样仍**串行**（`parallel_samples=False`）：它们共用该 arm 借到的那一个 world。

    ★ **轨迹长度只由 max_nights 决定**：不与步数上限并存 —— 每夜实际所需步数会随内容波动，
    若同时设步数上限，「昼夜节律」很容易被步数上限静默截断成「按步数跑满」。节律是昼夜，
    步数不是控制轴。

    ★ **训练侧规模的自由度：epochs + max_nights + aggregate_every**
      （框架侧的 batches = n_train = n_samples 全钉为 1；**n_eval 可 >1**）。
    前者是**轮数**，后两者是**一条轨迹的长度与它内部的聚合粒度**：

        总 consolidation 次数 = epochs × ceil(max_nights / aggregate_every)
                              （aggregate_every=0 时后项取 1）

    为什么框架的三个规模参数在本域无效（不是约定，是机制决定的）：

    - 本域 task 内容**完全相同**（真·自由探索，见 _explore_task），且每次 rollout 前都
      **重生世界 + 清库存/成就** —— 训练侧同一目标重复采样不增信息，纯重复计费
      （同 arm 的多次采样共用它借到的那一台 world，只能串行 → N 倍墙钟）。
      注：世界恢复（删存档按固定 seed 重生）由 _fresh_world 负责，`world.reset()` 只清库存+成就
      （index.js 的 reset:hard = /clear + /kill + /advancement revoke）。两者配合才做到
      「只有记忆跨轮携带」。
      ⚠️ 出生点随机（默认）→ 「同 task 采 N 次」严格说**不再等价**（开局地点不同）。但仍固定
      n_samples=1：要更多样本请调 epochs（只有 epoch 能让记忆累积），否则同一轮内多采样会把
      「出生点方差」记成「同轮内的重复」。
    - **arm 图谱按 (epoch,batch) 新建，下一轮不读上一轮那份**（文件保留存档，见 brain.py）→ 副脑经验
      只能靠 consolidate 汇进主脑才跨轮存活。**epoch 是唯一让记忆真正累积的轴**，也正是
      学习曲线的 X 轴。
    - n_train=1 时 _split(tasks,batches) 恒返回 1 个 batch → batches 自动坍缩，设别的值无效。
      **这正是 `aggregate_every` 存在的原因**：batches 想表达的「聚合几次」在本域表达不了，
      因为它切的是题目列表，而本域一个题就是一整条轨迹。题内那一维叫 window。
    - **n_eval 例外，是有用的自由度**：本域探索方差大（同一目标这轮挖到铁、下轮死在洞里），
      n_eval=K 让 evolving/frozen 各跑 K 条取平均压噪。本域 aggregate_eval 返回的是
      **每轮得分均值**（不是各域惯例的求和），故 gap 的量纲不随 n_eval 变，跨 run 可比。
    """

    name = "minecraft"
    # 每个副脑一台独立 MC server（worker 池）→ 各 arm 可并行。worker 数 < arms 时多余的 arm
    # 在 pool.acquire() 上排队，`--mc-workers 1` 自动退化成旧的串行行为。
    parallel_arms = True
    # 一轮探索就是一次采样，且同 arm 的多次采样共用它借到的那**一个** world → 仍串行。
    # （n_samples 本就被钉为 1，见下方注释；这个标志是双保险。）
    parallel_samples = False
    # ★ 画**训练曲线**而非逐轮 eval 曲线：本域讲的是「终身学习」的故事 ——
    # X 轴 = 训练轮次，Y 轴 = 本轮各 arm 训练得分均值，只要呈增长趋势即说明记忆在起作用。
    # 为何逐轮 eval 在本域近乎无意义：eval task 与 train task 是**同一个中性探索目标**
    # （_explore_task，内容全同，见 build_tasks），且每次 rollout 前都重生世界 + 清库存 ——
    # 所以 eval 并不测「泛化到没见过的题」（那是 math 的 eval 语义），只是在同条件下
    # 多采一条样，不提供 train 之外的新信息，却要付一条完整轨迹的墙钟（几分钟；且 eval 是
    # 单条串行跑的，见 loop._eval_one_group，并不吃 worker 池的并行）。
    # 注：框架仍会在**第 1 轮前跑一次 frozen 基线**（空记忆水平线），否则「得分上升」无法与
    # 「世界随机性/运气」区分。那只花 1 条轨迹，换来曲线的可解释性。
    train_curve = True
    # 强制单采样：task 全同 → 同一轮内重复采样不提供记忆维度上的新信息（random seed 下它只是
    # 在采地形方差），多采样纯属重复计费。要加样本请调 epochs（见类 docstring）。另：run_arm_batch
    # 为多采样准备的 answer_distribution / n_correct 在本域全是空转（无 answer_letter、correct 恒 None）。
    n_samples = 1

    def __init__(
        self,
        *,
        mc_port_base: int = 25600,
        mineflayer_port_base: int = 3100,
        workers: int = 1,
        workers_dir: str | Path = "/path/to/mc_probe/openkage_workers",
        reprovision: bool = False,
        server_dir: str | Path | None = None,
        max_nights: int = 3,
        aggregate_every: int = 0,
        smoke_dawn_nudge: bool = False,
        mc_dir: str | Path | None = None,
        world_seed: str = "7",
        spawn_radius: int = 1000,
        random_spawn: bool = True,
        spawn_seed: str = "20260703",
        context_dir: str | Path | None = None,
        footage_dir: str | Path | None = None,
        checkpoint_dir: str | Path | None = None,
        no_graph: bool = False,
        # ★ leave-one-out 消融字符串（2026-09-18，run_ablation.py 专用；None=非消融 run）：
        #   单一字符串（0915 教训「domain 不收散布尔」），domain 侧按值分支工具装配。
        #   · flat_retrieval —— expand 工具不装配（子图遍历通道没了；retrieve 降级在
        #     graph.py 的 retrieve_subgraph flat 短路，domain 无需动作）
        #    · no_exp_skills —— retrieve_skills 工具不装配 + _inject_all_skills 签名块注入跳过
        #     + _render_skill_delta 跳过（技能库本身已是 NullSkillStore——brain.py 装配，
        #     恒空；工具下架让 Agent 不会浪费步数查一个恒 miss 的工具）
        #   · no_memory_layer / no_exp_text —— 反思层指令的 yaml 条件块（reflect.yaml 的
        #     system 渲染时按本值去掉「两层都要写」段、只指路未拆层）；写入拒绝在
        #     tools.py 的 create_concept 校验。
        #   · no_shared_graph —— domain 无动作（拆除点全在 brain.py：主脑 NullGraph +
        #     聚合 no-op + 技能 arm_only_view）。
        ablation: "str | None" = None,
        # ★ 上下文管理（2026-09-13 加；**2026-09-24 改为记忆层连带，独立开关已删**，用户定）：
        #   【昨日目标】/【昨日经历】这套跨天上下文管理（夜反思镜像调用 dawn_reflection 注入 +
        #   createplan goal 跨天带过）**是私有记忆层的连带机制**，不再单独开关——记忆层承载的
        #   就是「我在哪、昨天做到哪」的跨天通道，昨日目标/复盘与它同生同灭：
        #     · 记忆层在（Full / −S / −D₁ / −D₂ / −R / −E / −K）→ 昨日目标/复盘照跑；
        #     · 记忆层没了（−M 拆记忆层 / no_graph 对照组 NullGraph）→ 整套跟着消失。
        #   由此 baseline（no_graph）不带昨日目标/复盘是设计而非配置；−M 拆的「情景跨天记忆」
        #   天然含这两块。本参数保留但**仅作旧调用兼容**（传入值被忽略，见下方派生处）。
        context_mgmt: bool = True,
        tick_rate: float = 1,
        difficulty: str = "normal",
        react_max_steps: int = 999,
    ) -> None:
        # mc_server 内聚在本域下（domains/minecraft/mc_server/）；不传 server_dir 时默认它。
        # **不按 worker 复制**：index.js 只读文件系统，N 个 node 进程共用同一 cwd，各自监听
        # 自己的端口即可（node_modules 1.1G，复制 N 份纯属浪费）。
        server_dir = server_dir or (Path(__file__).resolve().parent / "mc_server")
        # ★ 高级函数原语已移除（2026-08-27，用户定）：control_primitives/*.js（mineBlock/craftItem/
        #   smeltItem/… 共 16 个）此前经 programs 全量注入游戏端执行环境，同时 react.yaml「3.1 速查表」
        #   告知 LLM 它们的签名。两层都去掉 —— 实验组与对照组差距拉不开，怀疑这些预封装函数太强、
        #   把图谱带来的差异抹平，故改成只给 LLM 第 3 节列出的 mineflayer **原生** API（findBlock/dig/
        #   pathfinder.goto/equip/waitForTicks…），让 Agent 自己从底层拼出采集/合成/烧炼。
        #   要恢复：把下面这行改回 `self._programs = load_control_primitives()` 即可（函数本身仍保留在
        #   env.py，control_primitives/ 目录原样在），并把 react.yaml 3.1 速查表加回来。
        # ★ waitUntil 单独保留（2026-09-08）：它不是「能力」原语（不替 Agent 采集合成），而是「等待」
        #   原语——用服务器 timeOfDay 计时、dawn 兜底天亮自动醒。没有它 Agent 夜里只能自制
        #   for(N){waitForTicks(n)} 循环，而 waitForTicks 等的是客户端 physicsTick、不推进游戏时间，
        #   循环跑完天也不亮 → 熬不到天亮被 retire（bob day3 实测）。详见 load_wait_until_primitive。
        # ★ 2026-10-03 修复：`1672b69`（remove pillar-up）删掉了 `programs = load_wait_until_primitive()`
        #   这行却保留了下面的引用 → `NameError: name 'programs' is not defined`，build_domain 处即崩，
        #   任何新起/续跑 run 都在域构造时秒退（10-03 12:18 起全舰队熔断的真因）。waitUntil 按注释
        #   「单独保留」，故恢复其默认装配；pillar-up 仍保持移除态。
        self._programs = load_wait_until_primitive()
        if not mc_dir:
            raise ValueError(
                "minecraft 域需要 --mc-dir（**模板** MC server 目录）："
                "worker 目录是从它整目录复制出来的，没有模板就造不出 worker。"
            )
        specs = provision_workers(
            mc_dir, workers_dir, max(1, int(workers)),
            mc_port_base=mc_port_base, mineflayer_port_base=mineflayer_port_base,
            world_seed=world_seed, force=reprovision, difficulty=difficulty,
        )
        # ★ OPENKAGE 倍速：透传 tick_rate 到每个 worker 的 MinecraftWorld，再由它进 /start payload。
        # 保留浮点：2.5 等小数倍速合法（Node 侧 /tick rate ${20*N} 接受 50 TPS），int() 会静默截成 2。
        self._tick_rate = max(1.0, float(tick_rate))
        self._pool = MinecraftWorldPool(specs, server_dir=server_dir,
                                        tick_rate=self._tick_rate)
        # ★ 任务域 ReActAgent 每天的最大步数（防死循环兜底，不是动作预算，见 yml react_max_steps 注释）。
        #   缺键/未传 → 999。下游 _make_react_day 透传给 ReActAgent。
        self._react_max_steps = max(1, int(react_max_steps))
        self._max_nights = max_nights  # 自然昼夜流速，一轮过 max_nights 个夜晚封顶（黎明反思）
                                       # **轨迹长度的唯一闸门**
        # ★ **题内聚合周期**（每 K 夜一次副脑经验层→主脑；0=关闭）。
        # 本域一条轨迹就是一整条 max_nights 天的探索，而框架的 `batches` 切的是**题目列表**
        # —— 「一条 365 夜的轨迹每 7 夜聚合一次」在 batches 上表达不出来（且本域 n_train 钉为 1，
        # batches 恒等于 1）。故它是另一维：window，见 openkage/orchestrate/window.py。
        # 它同时钉住**副脑经验层的规模上限** = K 夜的产出，而不是整条轨迹的产出。
        self._aggregate_every = max(0, int(aggregate_every))
        if self._aggregate_every and self._aggregate_every >= max_nights:
            # 不报错（这是个合法配置，只是没有题内聚合的效果）：K≥N 时第一个边界就落在轨迹末尾，
            # 与批末聚合重合。说清楚比静默生效好 —— 否则会以为"我开了每 K 夜聚合"而实际没开。
            logger.info(
                "[minecraft] aggregate_every=%d ≥ max_nights=%d —— 第一个聚合窗口就落在轨迹末尾，"
                "等价于不做题内聚合（要真的轮内多次聚合，K 必须小于夜数）",
                self._aggregate_every, max_nights)

        self._smoke_dawn_nudge = smoke_dawn_nudge  # 仅 smoke：开局设到将黎明，加速验证反思链路
        # ReAct 上下文留痕目录（None=不落；runner **默认传**一个，见 experiments/run.py）。
        # **不写进主日志**：完整 messages 一次几十 KB，混进 logs/*.log 会把真正要看的进度行
        # 冲得没法读。落成一天一个 JSON —— 那就是复现这一天所需的全部信息。
        # 反思/聚合的留痕由框架层做（同一个目录、同一份 ContextDumper），见 openkage.agent_io。
        self._context_dir = Path(context_dir) if context_dir else None
        # 关键事件录制的落盘目录（None=不录制，完全是原有行为）。与 context_dir 同款：
        # runner 传 `results/<domain>_<ts>/footage`，见 experiments/run.py。
        self._footage_dir = Path(footage_dir) if footage_dir else None
        # 透明分段 checkpoint 目录（None=关闭分段容灾，现有行为）。runner 传 run_dir，
        # rollout 开头读 checkpoint 决定"续跑还是新跑"（自洽，无需 brain 透传 resume 信号）。
        self._checkpoint_dir = Path(checkpoint_dir) if checkpoint_dir else None
        # ★ 对照组 ablation 标志（见 openkage/brain.py 的 no_graph 参数）：True 时 night_cb 里
        #   跳过「私有图谱 checkpoint」（NullGraph 无 snapshot_with_vectors，会炸）、跳过阶段1/2
        #   图谱反思与技能回填；但**保留** world 快照 + progress.json（含 last_reflection/last_goal）
        #   —— 续跑靠后者，与实验组同机制。rollout 开头读 checkpoint 的逻辑（read_progress）与
        #   图谱无关，对照组天然能续。⚠ 2026-09-24 起 context_mgmt=记忆层连带：对照组（no_graph
        #   = 无记忆层）不再跑夜反思/【昨日目标】（0911-0923 的「归还对照组同跑」口径作废，回见
        #   brain.py no_graph docstring）。
        self._no_graph = bool(no_graph)
        # ★ leave-one-out 消融字符串（2026-09-18）：消费点见上面参数注释（react_day 工具装配/
        #   _inject_all_skills/reflect system 渲染）。no_graph 与 ablation 互斥（run_ablation.py
        #   已校验：no_graph 不传 ablation）——防御性再拦一道，防误构造出语义不明的叠加态。
        _ABLATION_DOMAIN_VALUES = {
            None, "no_shared_graph", "flat_retrieval",
            "no_memory_layer", "no_exp_text", "no_exp_layer", "no_exp_skills",
            "raw_shared_pool",
        }
        if ablation not in _ABLATION_DOMAIN_VALUES:
            raise ValueError(
                f"ablation={ablation!r} 不是 minecraft 域认识的 rung"
                f"（合法：{sorted(v for v in _ABLATION_DOMAIN_VALUES if v)}）；"
                f"no_graph 由独立布尔表达，不进本参数。")
        if no_graph and ablation is not None:
            raise ValueError("no_graph 与 ablation 互斥：no_graph 是全程 NullGraph 对照组，"
                             "leave-one-out rung 各有自己的装配，不可叠加。")
        self._ablation = ablation
        # ★ 上下文管理（2026-09-24 定，用户）：**记忆层连带，不再独立开关**。昨日目标/复盘与
        #   私有记忆层同生同灭——记忆层承载的正是「我在哪、昨天做到哪」的跨天通道，它没了这套
        #   上下文管理也没有载体：−M 拆记忆层/对照组 NullGraph → 昨日目标/复盘整套消失；其余
        #   rung 记忆层照常 → 照跑。旧 --no-context-mgmt 独立开关已删（历史：0913-0923 曾独立，
        #   0915 起对照组默认关——该口径下主表 6 条 nograph baseline 均 cm=False，与今日「连带」
        #   语义一致；消融套件 7 rung 均 cm=True，其中 −M/nomen 一条在旧开关下也恰好等价于连带
        #   口径，其余 rung 不受影响）。构造参数 context_mgmt 保留但被忽略（旧调用兼容），真值
        #   只看 ablation/no_graph 派生。门控点：night_cb ⑦ 反思与 ⑦' goal 搬运、续跑
        #   dawn_reflection 注入、续跑 prev_day_goals 恢复。渲染层（react_day）各 rung 同链路
        #   不动——断源在调用方。
        #   不动——断源在调用方。
        self._context_mgmt = not self._no_graph and ablation != "no_memory_layer"
        self._system_prompt = REACT.render_system(
            default_ticks=_STEP_TIMEOUT_DEFAULT_TICKS,
            max_ticks=_STEP_TIMEOUT_MAX_TICKS,
        )
        # ★ 世界协议：**固定 seed（地形恒定、别人可复现）+ 每个副脑每一轮重生世界（抹掉上一条
        #   轨迹的改造）+ 出生点在半径内随机（提供样本多样性，训练空间不至于太宽泛）**。
        self._world_seed = world_seed
        self._spawn_radius = spawn_radius
        self._random_spawn = random_spawn
        self._spawn_seed = spawn_seed
        # 出生点用**独立的确定性 RNG**（不用全局 random，免得被别处 seed 干扰）：
        # 同一 spawn_seed 复跑 → 出生点序列逐个相同，整个实验完全可复现。
        #
        # ★ **每个副脑一条独立的 RNG 流**：按 arm_key 派生独立流，各 arm 的出生点序列只由
        # (spawn_seed, arm_key) 决定，既并发可复现、又互相独立（若共用一条流，抽取顺序会
        # 受调度影响，「同 spawn_seed 复跑得到同一串出生点」就不成立了）。
        # `random.Random` 接受 str seed（内部走 sha512，不受 PYTHONHASHSEED 影响，跨进程稳定）。
        self._spawn_rngs: dict[str, random.Random] = {}
        # 已重生世界的次数（留痕/日志）。并发下多个 arm 会同时 +1，而 asyncio 单线程里
        # `+= 1` 不会被打断（GIL + 无 await 点），计数不会丢。
        self._world_counter = 0

    @property
    def n_workers(self) -> int:
        """池里有几台 MC server（= 最多能有几个副脑同时跑）。供 runner 记进结果 JSON ——
        worker 数 < arms 时副脑是排队跑的，墙钟与全并发的 run 不可直接比。"""
        return self._pool.size

    def prepare(self, *, model_name: str | None = None) -> str | None:
        """不碰生产库、从空图起步。"""
        return None

    def build_tasks(self, cfg: dict) -> tuple[list[Task], list[Task]]:
        # 真·自由探索：train/eval 都是同一个中性探索目标重复 N 次（无预设阶段方向）。
        # id 编号仅为区分轮次（记忆 round/留痕用），内容全同 → 方向靠记忆自涌现。
        # 本域**没有固定题库**（task 是现造的），所以 "all" 在这里无意义 —— 退化为 1
        # （由 runner 的 _apply_n_limits 统一解析：题数上限就是这里造出来的数量）。
        n_train = _as_count(cfg["n_train"])
        n_eval = _as_count(cfg["n_eval"])
        train = [_explore_task(f"mc_explore_train{i}") for i in range(n_train)]
        eval_tasks = [_explore_task(f"mc_explore_eval{i}") for i in range(n_eval)]
        return train, eval_tasks

    def _start_recorder(self, world, *, arm_key: str | None, round_num: int = 0,
                        start_night: int = 0):
        """给这条轨迹**造**一个录制器（不启动）。没配 footage_dir 就返回 None。

        ★ 2026-08-20 改纯串行：本方法只构造录制器对象，**不启动客户端**。客户端启动（`start()`，
        等客户端进世界 + mesh 就绪，~25-40s）由调用方 rollout 用 `asyncio.to_thread` 同步等 ——
        那一步不碰 bot（bot 还没起），无死锁。附身 + 开录由 `run_exploration` 在 bot 出生后调
        `attach_and_record()` 触发。这样录像只丢 ~4.5s 命令时间，而非旧并行架构的 ~70s。
        （旧版在此调 `start_in_background()` 后台起客户端、与 bot 出生并行 —— 那是时差根因。）

        ★ `round_num` 用来算 **slot**（`e1b1` 这类），它进落盘路径 `<arm>/<slot>/`。
        **必须带**：不带的话第 2 个 epoch 的录像会**覆盖**第 1 个（ffmpeg 带 `-y`），而 epoch
        正是学习曲线的 X 轴 —— 等于"跑得越多、留下的素材越少"，且完全静默。window 段不取：
        题内聚合窗口不换世界、也不换轨迹，一条轨迹就该是一个目录。

        **失败绝不影响训练**：造不出来就返回 None，轨迹按无录制的原有路径跑。
        """
        if not self._footage_dir:
            return None
        try:
            from openkage.roundcode import decode

            from .footage import FootageRecorder

            # 解码走 roundcode（唯一真相，别自己写 divmod —— 那个约定漂移过一次）
            epoch, batch, _window = decode(round_num)

            rec = FootageRecorder(
                self._footage_dir,
                arm_key=arm_key or "eval",
                slot=f"e{epoch}b{batch}",
                mc_port=world.mc_port,
                # bot 名字由 domain 侧按 arm key 设过（见 MinecraftWorld.set_bot_name），
                # /spectate 要的就是这个游戏内用户名。
                bot_name=getattr(world, "bot_name", "bot") or "bot",
                mc_dir=getattr(world, "mc_dir", None),
                # 续跑时第一段从 start_night+1 天起录（与 env 侧 day_idx 对齐），
                # 否则首段文件名/索引/facet 归属会错位成第1天（见 FootageRecorder._day）。
                initial_day=start_night + 1,
            )
            return rec
        except Exception:  # pylint: disable=broad-except
            logger.warning("[minecraft] 录制器构造异常，本轮不录制（训练继续）", exc_info=False)
            return None

    async def _fresh_world(self, world, *, round_num: int, task_id: str,
                           arm_key: str | None, arm_name: str | None = None,
                           resume: bool = False) -> dict:
        """**每个副脑、每一轮探索前**把世界恢复原状 + 抽一个出生点。返回世界留痕（进 metrics）。

        `world` 是调用方从 worker 池**独占借到**的那一台（见 rollout）。重生只影响这一台
        server —— 这正是并发化的关键。

        协议三件套：

        ① **固定 seed**（`world_seed`，默认 `7`，自然出生点=plains）：地形逐块恒定。
           好处是**训练空间不至于太宽泛**（记忆面对的是同一片地形，学到的东西能反复用上）
           且**别人容易复现**（同 seed + 同出生点表 = 同实验）。

        ② **每 arm 每轮重生世界**：`world.reset()` 只清库存/成就，**地形不清**。不重生的话第 2 轮的
           bot 站在第 1 轮砍光树、挖开洞、放好工作台的地形里 → 「evolving 随 epoch 上升」无法区分
           「记忆变好」与「世界被前几轮开发过」。粒度是 per-arm-per-round 而非
           per-round：副脑串行共用一个 server，只在轮首重生会让 arm2 继承 arm1 开发过的地形。

        ③ **出生点半径内随机**：地形固定后若出生点也固定，每条轨迹的开局就完全一样，样本多样性
           只剩 LLM 采样噪声。随机出生点让 bot 面对同一世界的不同局部（这片林子/那片山），
           既保留"同一训练空间"又提供多样性。**随机但可复现**：出生点由独立 RNG（`spawn_seed`）
           确定性产出，同 spawn_seed 复跑得到同一串坐标。

        ⚠️ **随机出生点的真障碍与真解法**：bot 被传到**未生成区块**时，地形虽会按需现场生成，
        但 mineflayer 只等固定时长就超时 → `/start` 卡死。**卡的不是传送本身** —— 服务器日志显示
        传送已成功，卡的是传送**之后**等 tick。解法是 `world.prewarm_and_spawn()`：**传送前只预热
        目标点周围一小圈**再过去。预热量与随机半径无关，故半径可以放得很大。

        重生失败**直接抛**：世界脏了这条轨迹就没有实验价值，让它继续跑只会把污染样本混进曲线。
        """
        # 并发守卫（禁止共用）：重生世界要**停整个 MC server**，所以真正不能容忍的是**两个并发的
        # rollout 共用一台 server** —— 那时一个 arm 的重生会把另一个的 bot 一起掀翻、存档互相
        # 覆盖，数据静默错（跑得通、但轨迹张冠李戴）。现在每个副脑从池里独占借一台（worker_pool），
        # 这个前提已满足，故守卫改为守剩下的两条：
        # ① 同 arm 的多次采样共用它借到的这一台 → parallel_samples 必须仍为 False；
        # ② world 必须是池里的成员（防止哪天有人绕过 acquire() 直接传个野 world 进来）。
        if self.parallel_samples:
            raise RuntimeError(
                "parallel_samples 与「每轮重生世界」不兼容：同一个副脑的多次采样共用它借到的"
                "那一台 MC server，并发采样会互相掀翻。要并发采样得让每次采样各借一台 worker。"
            )
        assert world in self._pool._worlds, (  # noqa: SLF001  故意查私有：这是不变量断言
            "传进来的 world 不是 worker 池的成员 —— 独占性无从保证，拒绝重生世界。"
            "world 必须经 pool.acquire() 获得。"
        )
        # 给这个副脑一个**独立的游戏内身份**：用 arm key 当用户名，游戏内围观时能分清谁是谁，
        # 也是日后多 Agent 联机协作的基础。
        # 必须在 regenerate_world **之前**调 —— 它会写 ops.json，而重生世界后紧接着 /start 就要
        # 用到 OP 权限（/forceload 预热、/spreadplayers 出生）。set_bot_name 同时负责把新名字的
        # 离线 UUID 写进 ops.json，否则 bot 静默失去 OP、斜杠指令全部无声失败。
        # 用 **arm_name**（英文人名 Alice/Bob）而不是 arm_key（文件名 alice/bob）：MC 用户名
        # 大小写敏感，而 name 才是这个 Agent 的身份（key 只是机器 id / 文件名）。
        # eval 分支两者都为 None（frozen 基线），沿用默认 "bot"。
        if arm_name or arm_key:
            await asyncio.to_thread(world.set_bot_name, arm_name or arm_key)
        # ★ 放置埋点（2026-09-14）：**写本地盘缓存，checkpoint 边界拷回共享盘**（2026-09-18
        #   二次实锤改造，见下）。run 目录下每臂一个 place_log_<arm>.jsonl，Node 侧
        #   blockPlaced 事件逐条 append。
        #   ★ **FUSE 事故两次实锤**（0914 多进程共写 + 0918 单写者也丢）：
        #     - 0914：5 臂 5 node 进程并发 appendFileSync 同一 place_log.jsonl → 头部 NUL
        #       空洞+早期行丢，当时判「多进程并发 append 缺陷」，修法=每臂一文件（单写者）。
        #     - 0918：dslow/glmexp/glmctl 三 run × 3 臂 × place+eat 全部 18 个文件**无一幸免**，
        #       头部 NUL 空洞 0-98%（dslow Carol eat_log 前 89% 全 NUL，首条记录 day27）。
        #       单进程单写者照样丢 → 0914 的「每臂一文件」修法被证伪，根因是 **FUSE spaceclient
        #       对本地盘→共享盘路径下高频 appendFileSync 的缓存回写缺陷**，与进程数无关。
        #   ★ **现修法（本地盘缓存+边界拷回）**：Node 侧写 `/path/to/logs/<run_stem>/`
        #     （本地盘，无 FUSE），窗口边界由 _snapshot_world 旁的 _sync_local_logs 把两份日志
        #     copy2 整文件拷回共享盘 run 目录（一次性整写，无 append）。shutdown 兜底再拷一次，
        #     防 SIGTERM 中途停丢「最后窗口之后」的段。读侧（plot_build3d/inspect_metrics）
        #     读共享盘路径不变。
        #   arm 名取 world.bot_name（set_bot_name 刚设定，=游戏内 username，即日志里的 arm
        #   字段）。eval/无 checkpoint 不埋，保持原有行为。
        if self._checkpoint_dir is not None:
            _pl_arm = (world.bot_name or arm_key or "bot").replace(" ", "_")
            # 本地缓存目录：/path/to/logs/<run目录名>/（本地盘，绕开 FUSE）
            _local_dir = Path("/path/to/logs") / self._checkpoint_dir.name
            _local_dir.mkdir(parents=True, exist_ok=True)
            world.place_log_path = str(_local_dir / f"place_log_{_pl_arm}.jsonl")
            # ★ 进食/血量埋点（2026-09-17）：同款本地盘缓存+边界拷回（见上方 place_log 注释）。
            #   trigger=eat/heal/damage/death + elapsedTime/worldDay/died 对齐字段，
            #   供量化「生存」长期目标。复用 _pl_arm（同 bot_name），同目录同命名规约。
            world.eat_log_path = str(_local_dir / f"eat_log_{_pl_arm}.jsonl")
            # 拷回映射：Node 只管写本地路径；窗口边界/shutdown 时按此映射 copy2 到共享盘。
            # eval 分支（arm_key=None）也写本地但不拷回（无 checkpoint_dir 语义，维持旧行为）。
            world._log_sync_targets = {
                world.place_log_path:
                    str(Path(self._checkpoint_dir) / f"place_log_{_pl_arm}.jsonl"),
                world.eat_log_path:
                    str(Path(self._checkpoint_dir) / f"eat_log_{_pl_arm}.jsonl"),
            }
            # ★ 跨机续跑 seed（2026-09-18）：本地缓存在每机 /path/to/ 下——若前段在别的机跑、
            #   本机 /path/to/ 无缓存，Node 会从空文件 append，窗口拷回时**只有本段**覆盖共享盘
            #   完整副本 = 前段日志静默丢。续跑起把共享盘已有文件 seed 拷下来，Node 在完整
            #   历史上继续 append（同机续跑本地文件已在，此步为 no-op）。
            for _local, _remote in world._log_sync_targets.items():
                try:
                    _lp, _rp = Path(_local), Path(_remote)
                    if (not _lp.exists() or _lp.stat().st_size == 0) and _rp.is_file() and _rp.stat().st_size > 0:
                        shutil.copy2(_rp, _lp)
                        logger.info("[minecraft] 续跑日志 seed：%s ← 共享盘 %s（%dB）",
                                    _lp.name, _remote, _rp.stat().st_size)
                except Exception:  # pylint: disable=broad-except
                    logger.exception("[minecraft] 续跑日志 seed 失败 %s（不影响轨迹）", _local)
        else:
            world.place_log_path = None
            world.eat_log_path = None
            world._log_sync_targets = {}
        if resume:
            # ★ 透明分段续跑：不重生世界。读上段存档——地形/物品/位置/时间全部从存档恢复，
            # 等价于一次性连续跑（地基已实测：SIGTERM 停服后 resume_world 续起，bot 位置+库存完全恢复）。
            # 续跑段的世界_info 标记为 resume，便于 metrics 留痕区分（"这条轨迹的第 N 段"）。
            #
            # ★ 续跑缺陷②（2026-08-19）：优先从最近窗口边界的 `checkpoint{W}/arm_{key}/world/`
            # 拷回干净存档，而非读被中途 save_world 覆盖的活存档。SIGTERM/SIGINT 中途停时活存档
            # 可能存于"bot 死亡重生时刻"→ 库存丢失（8/19 alice/carol 32/39种→0/1种）。
            # 窗口边界的 world/ 是 unfreeze 后 _snapshot_world 拷的干净状态。self._resume_window
            # 由 rollout 读 checkpoint 时定位（见上方"续跑回退到窗口边界"）。
            if self._resume_window is not None and world.mc_dir:
                ckpt_world = (Path(self._checkpoint_dir) / f"checkpoint{self._resume_window}"
                              / f"arm_{arm_key or 'eval'}" / "world")
                if ckpt_world.is_dir():
                    await asyncio.to_thread(_restore_world_from_checkpoint,
                                            ckpt_world, world.mc_dir, arm_key or "eval",
                                            self._resume_window)
            if not await asyncio.to_thread(world.resume_world):
                raise RuntimeError(
                    f"世界续跑读档失败（round={round_num} arm={arm_key} task={task_id} "
                    f"mc_port={world.mc_port}）：存档可能损坏或被清。排查：worker 目录存档是否还在、"
                    "上段是否在 unfreeze 后才 checkpoint（freeze 状态存档会让续跑世界冻死）。")
            self._world_counter += 1
            return {
                "world_seed": self._world_seed,
                "world_index": self._world_counter,
                "world_resumed": True,   # 续跑段标记：metrics 区分"第 N 段"vs"全新轨迹"
                "random_spawn": False,   # 续跑不重抽出生点（bot 回到上段结束位置）
                "spawn_seed": self._spawn_seed,
                "mc_port": world.mc_port,
            }
        if not await asyncio.to_thread(world.regenerate_world, self._world_seed):
            raise RuntimeError(
                f"世界重生失败（round={round_num} arm={arm_key} task={task_id} "
                f"seed={self._world_seed} mc_port={world.mc_port}）：地形会残留上一条轨迹的开发"
                "痕迹，该样本无实验价值，中止本次 rollout。排查：worker 目录是否完好、"
                "MC server 端口是否被别的进程占住、Java 21 是否可用。"
            )
        self._world_counter += 1
        # 出生点不在这里定 —— 交一个**取点函数**给 env 层，由它「预热→检查能否站→不能就换点」
        # 循环到成功（见 MinecraftWorld.prewarm_and_spawn）。这里只负责"下一个候选点是什么"，
        # 因为随机序列的确定性（可复现）属于域的实验协议，而"落点能不能站"属于环境细节。
        return {
            "world_seed": self._world_seed,
            "world_index": self._world_counter,
            "random_spawn": self._random_spawn,
            "spawn_seed": self._spawn_seed,
            # 哪台 worker 跑的这条轨迹：并发下同一轮有多条轨迹同时在跑，出问题时要能对上
            # 是哪台 server 的 openkage_server.log。
            "mc_port": world.mc_port,
        }


    def _make_spawn_pick(self, arm_key: str | None):
        """返回一个 `pick() -> (x, z)` 取点函数；`random_spawn=False` 时返回 None（自然出生点）。

        在 ±spawn_radius 的方形内均匀取 (x, z)，**不给 y** —— 落地高度由 `/spreadplayers` 决定
        （放到该柱体地表最高可站方块），自己猜 y 会把 bot 塞进石头或悬在半空。

        每次调用推进**该副脑自己的** RNG，所以「换点重抽」也是确定性的：同 spawn_seed 复跑会走
        完全相同的候选序列、落在完全相同的出生点上（**含被跳过的水面点**），实验可复现。
        每个 arm 一条独立流（见 __init__ 里 `_spawn_rngs` 的注释）—— 并发下各 arm 抽点互不干扰，
        eval/基线路径（`arm_key=None`）自己也有一条。
        """
        if not self._random_spawn:
            return None
        r = self._spawn_radius
        key = arm_key or "_eval"
        rng = self._spawn_rngs.get(key)
        if rng is None:
            # str seed（Random 内部走 sha512）→ 跨进程/平台稳定，不受 PYTHONHASHSEED 影响。
            rng = random.Random(f"{self._spawn_seed}:{key}")
            self._spawn_rngs[key] = rng
        return lambda: (rng.randint(-r, r), rng.randint(-r, r))

    async def rollout(
        self, task: Task, *, primary=None, secondary=None, arm_role=None, arm_key=None,
        model_name=None, round_num=0, arm_name=None, on_window=None, on_night=None,
        on_spawn_ready=None,
        resume: bool = False, start_night: int = 0,
        checkpoint_dir: str | Path | None = None,
        skill_registry=None,
    ) -> Sample:
        """跑一整轮昼夜探索。secondary=None（eval）时探索不写图谱（night_cb 里的 reflect 被守卫跳过）。

        `arm_key` 是副脑的机器 id（如 `alice`；eval/基线路径为 None）。本域拿它做两件
        事：① 出生点 RNG 按 arm 派生独立流（并发可复现，见 `_make_spawn_pick`）；② worker 借用
        日志的标识。整轮探索期间从池里**独占借一台** world（每副脑一台独立 MC server）。

        `on_window` = **题内聚合**回调（框架给，见 `openkage.domain.WindowHook`）：
        开了 `aggregate_every=K` 时每过 K 夜调一次 —— 它就地给本副脑图谱拍快照 + 清**经验层**
        （记忆层保留），聚合在后台跑，本轮探索**不等它**、立刻进入第 K+1 天。
        ★ 三栅栏拆分后 `on_window` 暴露 `arrive`/`collect` 两步（见 loop.py `_WindowHook`）：
        `arrive`（栅栏1 后）等齐并起后台聚合、放行进阶段2；`collect`（栅栏3）等聚合结果，
        失败抛 `WindowAggregationError`（abort batch）。night_cb 据此重组。

        `on_night` = **每晚三栅栏**回调（框架给，见 `openkage.domain.NightHook`）：纯等齐，
        不聚合。night_cb 在阶段1 后调 `on_night(stage=1, day_idx)`（栅栏1，等所有臂经验/记忆
        反思完）、阶段2 后调 `on_night(stage=2, day_idx)`（栅栏2，等所有臂技能维护完）。栅栏3
        由 `on_window.collect` 担任（仅聚合天数）。各栅栏 `secondary is not None` 门控下调用
        （NullGraph 非 None，no_graph 臂也到达栅栏，否则别的臂永久等它）。
        """

        # ★ 透明分段自洽：rollout 开头读 checkpoint，若本 arm 本轮轨迹有未完成进度，自动续跑。
        # 这样 resume 信号无需 brain/run_arm_batch 透传——brain 的 start_epoch 逻辑天然正确
        # （没跑完的 epoch 没 train_curve 点，会被重跑），重跑时 rollout 自洽读 checkpoint 决定
        # "从第 N 夜续"而非"从头新跑"。checkpoint_dir=None 时此机制完全关闭（现有行为）。
        # 显式传入的 resume/start_night（若有）优先级低于 checkpoint——checkpoint 是事实来源。
        # 续跑昨夜复盘文本（从 checkpoint 读取，无则空串）
        last_reflection_text = ""
        # 续跑「昨日目标」文本（从 checkpoint 的 last_goal 读取，无则空串）。供 react_state
        # 初始化 prev_day_goals，续跑首天 day_start【昨日目标】块据此渲染。
        last_goal_text = ""
        # 续跑缺陷①（2026-08-19）：从 checkpoint 的 metrics_snapshot 恢复指标累积状态
        # （step_idx/items_seen/tier_progress/…），让续跑段 tier_first_step/n_steps/unique_items 含前段。
        resume_metrics: dict | None = None
        # 续跑缺陷②（2026-08-19）：从最近窗口边界的干净 world 存档恢复，而非被中途 save_world
        # 覆盖的活存档。_resume_window 记录用哪个窗口的 world/，供 _fresh_world 拷回。
        self._resume_window: int | None = None
        if self._checkpoint_dir is not None and arm_key is not None:
            ckpt = read_progress(self._checkpoint_dir, arm_key, round_num)
            if ckpt is not None and not ckpt.get("complete"):
                resume = True
                start_night = int(ckpt.get("n_nights_done", 0))
                last_reflection_text = ckpt.get("last_reflection", "")
                # ★ 续跑恢复「昨日目标」：从 checkpoint 的 last_goal 字段读（与 last_reflection
                #   同款，渲染好的换行文本）。旧 checkpoint 无该字段 → 返空 → 续跑首天 prev_goals 空
                #   → day_start【昨日目标】块跳过（向后兼容，不崩）。0911-0923 两组同写（独立 flag
                #   时代）；0924 起 context_mgmt=记忆层连带，−M/no_graph 写空串（字段仍在=结构兼容）。
                #   仅 0909-0910 期间的对照组 checkpoint 无此字段。
                last_goal_text = ckpt.get("last_goal", "")
                # 定位带 world/ 的最近窗口：续跑从该窗口边界恢复干净存档，start_night 回退到 W*K。
                # ★铁律（用户反复强调，0912 再踩）：任何续跑都从 checkpoint{W}/world/ 拷回 world，
                #   **不能读活存档**——活存档可能被异常 run（崩溃中途停/单臂重生/死亡重生 save_world）
                #   污染，续跑读它=污染静默传递。故只要存在带 world/ 的窗口 W，就设 _resume_window
                #   让 _fresh_world 拷回干净存档，与 start_night 是否回退无关（见下）。
                # 没有 window world/（第一窗口之前停 / 早期 run 无 _snapshot_world）才退回——此时
                #   无干净存档可拷，是早期 run 向后兼容的退化路径，非正常续跑应走路径。
                w = latest_window_with_world(self._checkpoint_dir, arm_key)
                if w is not None and self._aggregate_every:
                    resume_start = w * self._aggregate_every
                    # ★ world 恢复无条件：设 _resume_window 让 _fresh_world 从 checkpoint{W}/world/
                    #   拷回（即使停在窗口边界 resume_start==start_night 也要恢复——活存档不可信）。
                    self._resume_window = w
                    if resume_start < start_night:
                        logger.info(
                            "[minecraft] 续跑回退到窗口%d 边界（第%d夜）恢复干净存档——"
                            "原 n_nights_done=%d，重跑窗口间的 %d 夜（防中途存档库存丢失）",
                            w, resume_start, start_night, start_night - resume_start)
                        start_night = resume_start
                        # 回退重跑窗口之间的几天：扁平 JSON 的 metrics_snapshot 是 n_nights_done 夜的
                        # （多算了窗口间那几天），不能直接用。但窗口目录 checkpoint{W}/arm_{key}/
                        # progress.json 里有一份**窗口边界夜定格**的 snapshot（由 _checkpoint_cb 在
                        # 窗口边界夜镜像写入，2026-08-22）——它正好对得上回退后的 start_night(=W*K)，
                        # 用它恢复前段指标(step_idx/deaths/advancements/tier_progress)，零丢失。
                        # 早期 run 的窗口目录 progress.json 无 snapshot 字段 → win_ckpt 为 None 或无
                        # snapshot → 退回原「从 W*K 重算」行为，向后兼容。
                        #
                        # ★ **last_reflection / last_goal 同源覆盖**（2026-09-11，与 metrics_snapshot
                        #   同口径）：扁平 JSON 的 last_reflection/last_goal 是活数据「未跑完的最后状态」，
                        #   与 n_nights_done 可能不自洽——env 层 n_nights 在 night_cb 反思之前就递增
                        #   （env.py:3703 先于 __init__.py:852 _make_next_day_goal），中断在二者之间时
                        #   扁平出现「nights=N 但反思停第 N-1 天」的撕裂态；更危险的形态是 _checkpoint_cb
                        #   写了 nights=N + 第N天反思但 world save 未完整。回退到 W*K 重跑第 W*K+1 夜时，
                        #   「昨日」正是第 W*K 夜=窗口边界夜，故用窗口目录定格的 reflection/goal 才正确，
                        #   而非扁平里那份可能与回退起点错夜的活数据。无该字段（早期 run）→ 不动，向后兼容。
                        win_ckpt = read_window_progress(self._checkpoint_dir, arm_key, w)
                        if win_ckpt is not None:
                            if win_ckpt.get("metrics_snapshot"):
                                resume_metrics = win_ckpt["metrics_snapshot"]
                            if win_ckpt.get("last_reflection"):
                                last_reflection_text = win_ckpt["last_reflection"]
                            if win_ckpt.get("last_goal"):
                                last_goal_text = win_ckpt["last_goal"]
                            logger.info(
                                "[minecraft] 回退到窗口%d 边界 → 用该窗口定格快照恢复前段"
                                "（指标%s，含第%d夜反思/目标）", w,
                                "已恢复" if resume_metrics else "从该夜重算",
                                resume_start)
                if resume_metrics is None and start_night == int(ckpt.get("n_nights_done", 0)):
                    # 未回退（停在窗口边界 n_nights_done==W*K）：用扁平 progress.json 的
                    # metrics_snapshot（最新夜 == 续跑起点夜）。
                    resume_metrics = ckpt.get("metrics_snapshot")
                    if self._resume_window is not None:
                        # 窗口边界续跑：start_night 未回退，但 world 仍从 checkpoint{W}/ 拷回
                        # （铁律：不读活存档）。打这行让巡检 grep「恢复干净存档」能命中窗口边界情形。
                        logger.info(
                            "[minecraft] 续跑停在窗口%d 边界（第%d夜）——world 仍从 checkpoint 拷回"
                            "恢复干净存档（不读活存档），不重跑", self._resume_window, start_night)
                logger.info("[minecraft] 检出未完成 checkpoint：arm=%s round=%s 已过%d/%d夜 → 续跑"
                            "%s", arm_key, round_num, start_night, ckpt.get("max_nights"),
                            "（恢复指标累积）" if resume_metrics else "（回退重跑，指标从该夜重算）")
            elif ckpt is not None and ckpt.get("complete"):
                # 上一条轨迹已跑完——这是 brain 重跑已完成 epoch 的罕见情形（如手动 resume）。
                # 不续跑（会重复），正常新跑。清掉旧 checkpoint 避免下次再误判。
                logger.info("[minecraft] checkpoint 显示 arm=%s round=%s 已完成，正常新跑（不清旧档）",
                            arm_key, round_num)

        # base = 固定的人设+域 system_prompt（不含记忆块——记忆改为每步重检、见下）。
        # 复用 build_context_messages 但不传图谱 → 只拿到 [system]，primary_ctx/secondary_ctx 为 None。
        base_messages, _, _ = build_context_messages(
            "", system_prompt=self._system_prompt, arm_role=arm_role,
            # 告诉它自己叫什么：聊天框里 `<Alice> …` 就是它自己说的话（游戏内用户名同此，
            # 见 _fresh_world 的 set_bot_name），日后多 Agent 联机时要靠名字分辨谁是谁。
            agent_name=arm_name,
        )
        # 每步重检主+副脑、只留最新一块记忆挂在当前观察前（白天累积对话 + 每步刷记忆块）。
        # 每步输入落盘的文件名前缀：同一个目录下会有多个 arm × 多轮 × 多条轨迹的步，
        # 不带这些维度就会互相覆盖。**用日志作用域（如 `e1b1 a1 #1`）当标识** —— 它已经精确
        # 表达了「哪轮 batch / 哪个副脑 / 第几次采样」，与日志行首的 `[作用域]` 列天然对得上，
        # 方便拿着日志里的一行去找对应的输入 JSON。arm_role 是人设散文（首行是一句话，不是
        # arm key，框架不把 key 传进 rollout），拿它当文件名会得到一串认不出的中文。
        traj_tag = f"{_sanitize_tag(current_scope() or 'run')}_{task.id}"
        # 整轮 ReAct 代价累加器（write_code / retrieve / LLM 调用次数…）：`tier_first_step`
        # 报的是 write_code 步数，而一步 write_code 背后还有若干不推进世界的 ReAct 轮 ——
        # 两者必须一起报，否则「几步解锁 stone」这个数字无法与别的系统横向核验。
        # 详见 _make_react_day 的 `cost` 参数说明。
        react_cost: dict = {}
        # ★ 共享容器（与 react_cost 同款模式）：react_day 把当天进行中的 ReActAgent 写进
        #   react_state["agent"]，night_cb（同 rollout 闭包）读它的 .history 做「次日目标」镜像调用。
        #   必须用容器：night_cb 在 async_run 返回**之前**就触发（天亮那一步的 WriteCodeTool 经
        #   do_step 执行期间），那时 result.history 还没产出，只能靠这层引用拿到当天进行中的 history。
        #   ★ prev_day_goals：次日 day_start【昨日目标】块要读的昨日 goal 列表。续跑时从 checkpoint
        #     的 last_goal（渲染好的换行文本）恢复成单元素 list、新跑时为空（第 1 天无昨日目标）。
        #     night_cb 末尾把当天 createplan 立的 goal 列表（react_state["last_goals"]，CreatePlanTool
        #     追加写、去重保序）搬进 prev_day_goals（context_mgmt=记忆层连带：记忆层在的 rung 搬，
        #     0924 起 −M/no_graph 不搬；0911-0923 曾两组同搬）。
        #     last_goals：当天多次 createplan 立的 goal 列表（CreatePlanTool.run 追加写），night_cb 读它
        #     跨天带过。一天多次 createplan 全部进入次日【昨日目标】（2026-09-10 改，与【昨日经历】读
        #     整天 history 同口径），不再只取最后一条。
        react_state: dict = {
            "agent": None,
            "last_goals": [],
            # ★ 2026-09-24：context_mgmt=记忆层连带（独立开关已删）。False（−M 拆记忆层 /
            #   对照组 no_graph）不恢复【昨日目标】——旧 checkpoint 的 last_goal 非空也不恢复
            #   （react_day 渲染层各 rung 同链路，断源全在调用方：这里 + night_cb ⑦'）。
            "prev_day_goals": ([last_goal_text]
                               if (resume and last_goal_text and self._context_mgmt)
                               else []),
        }
        react_day = _make_react_day(
            list(base_messages), model_name,
            primary=primary, secondary=secondary, topic_tag=task.topic_tag, task_id=task.id,
            context_dir=self._context_dir, traj_tag=traj_tag, cost=react_cost,
            skill_registry=skill_registry, react_state=react_state,
            start_night=start_night,
            react_max_steps=self._react_max_steps,
            # ★ leave-one-out 消融（2026-09-18）：react_day 是模块级闭包、看不到 self，
            #   经参数透传（flat_retrieval→expand 下架；no_exp_skills→retrieve_skills 下架+
            #   签名注入跳过）。
            ablation=self._ablation,
        )
        # ★ **不加 `or "..."` 兜底**：那个兜底会把刚刚从 _EXPLORE_OBJECTIVE 删掉的那句话
        #   原地复活（它就是同一句话的第三个出处），使置空完全失效。空目标是**有效取值**，
        #   下游 reflect 模板用 `when: objective` 守着它，不会拼出空洞。
        objective = task.question
        primary_ctx = secondary_ctx = None  # 每步刷记忆，无整轮固定 ctx 可留痕
        # night_cb 定义在 `async with pool.acquire` 之前（react_day 要先于借 world 构造），
        # 故 world 在 night_cb 闭包里靠这个外层变量延迟绑定：acquire 后赋值，night_cb 里 nonlocal 读。
        _held_world = None
        # ★ 续跑时注入「昨夜复盘」文本：从 checkpoint 读到、传给 run_exploration 的首步 last_events。
        # 正常新跑时为空列表（不注入任何额外事件）。
        # ★ 2026-09-24：context_mgmt=记忆层连带（独立开关已删）。False（−M / no_graph）不注入
        #   ——即使旧 checkpoint 的 last_reflection 非空也不注入，保持「无昨日上下文」口径。
        _resume_initial_events: list = (
            [("dawn_reflection", last_reflection_text)]
            if resume and last_reflection_text and self._context_mgmt
            else []
        )
        # 闭包容器：_checkpoint_cb 写进度 JSON 时需要带上最新一条反思文本，而 _make_next_day_goal
        # 在 night_cb 末尾才产出，用 dict 包装避免对不可变外层变量 rebind。
        _last_reflection: dict = {"text": ""}
        # ★ _last_goal 同款闭包容器：night_cb 末尾把当天 createplan 的 goal 列表渲染成换行文本
        #   写进这里，_checkpoint_cb 据此写 checkpoint 的 last_goal 字段（供续跑恢复 prev_day_goals）。
        #   checkpoint 存的是渲染好的 str（换行无编号、去重保序），续跑读回成单元素 list——链路与
        #   last_reflection 同款（都是 str），向后兼容旧 checkpoint（单条 goal 的 str 直接当整块文本）。
        #   有记忆层的 rung 同写；0924 起 −M/no_graph 写空串（字段仍在=结构兼容；0911-0923 曾两组同写）。
        _last_goal: dict = {"text": ""}

        async def night_cb(day_text: str, day_idx: int, discoveries: dict,
                           day_steps: list | None = None) -> str:
            # ② 写**私有记忆**（每天黎明一次）。eval 时 secondary=None → 跳过（只读主脑、不写）。
            # 时机是「熬过一夜、天刚亮」且世界已 /tick freeze 冻结 —— bot 不挨打、不乱动，
            # SQLite 也无并发（见 env.py 那段黎明处理）。
            #
            # ★ 三栅栏控制流（2026-08-29，见 plans/joyful-painting-twilight.md）：
            #   ① _apply_skill_usage_counts（计数回填，各臂独立）
            #   ② 阶段1 _reflect_exploration（经验/记忆反思）
            #   ③ 栅栏1：on_night(stage=1) —— 等所有臂阶段1 完，才起聚合（arrive）+ 进阶段2
            #   ④ 阶段2 _reflect_skill_maintenance（技能维护，与聚合并行）
            #   ⑤ 栅栏2：on_night(stage=2) —— 等所有臂阶段2 完（技能库稳定）
            #   ⑥ 栅栏3：on_window.collect（仅聚合天数）—— 等聚合结果；失败抛 WindowAggregationError
            #   ⑦ _make_next_day_goal（次日经历，不依赖聚合，context_mgmt=记忆层连带：记忆层在的
            #      rung 跑；−M/no_graph 消失）
            #   ⑧ checkpoint 写盘（依赖栅栏3 的 on_window_ok）
            #   栅栏1/2 的 on_night 调用都在 `secondary is not None` 门控下（NullGraph 非 None，
            #   no_graph 臂也到达栅栏，否则别的臂在栅栏永久等它 → 死锁）。no_graph 跳过阶段1/2 的
            #   图谱反思/技能回填（对照组不建图谱、无技能库），但**仍调 on_night** 参与栅栏同步；
            #   ⑦ 的夜反思 0911-0923 曾「两组同跑」（独立 flag 时代），2026-09-24 起随记忆层连带
            #   ——对照组（no_graph）不跑。
            #   no_graph（对照组）：on_window（题内聚合边界）仍照常触发 —— 那是拿窗口级 checkpoint
            #   的唯一入口，对照组续跑靠它（聚合内部走 no-op 短路，见 window.py）。
            is_aggregate_day = bool(
                on_window is not None and secondary is not None
                and self._aggregate_every and day_idx % self._aggregate_every == 0)
            window = day_idx // self._aggregate_every if is_aggregate_day else 0
            on_window_ok = False

            if secondary is not None and not self._no_graph:
                # ★ 技能调用计数回填（见 plans/skill_aggregation_maintenance.md）：把当天 onLog
                #   埋点累加的 skill_calls（{slug:{ok,fail}}）确定性写进副脑 skill 表的
                #   success_count/fail_count。这是聚合"降级高失败率技能"的数据基础，纯计数无需 LLM。
                #   只回填本副脑已有的技能 slug（retrieve 命中的他臂技能不写回这里，那属于主脑）。
                _skill_calls = (discoveries or {}).get("skill_calls") or {}
                _skill_sources = (discoveries or {}).get("skill_sources") or {}
                if _skill_calls:
                    _apply_skill_usage_counts(secondary, primary, _skill_calls,
                                              _skill_sources, round_num,
                                              skill_registry=skill_registry)
                # ② 阶段1：经验/记忆反思。
                # ★ 重建 trajectory_text：用 agent.history 的思考链+计划 + day_steps 的关键信号
                #   观察+代码反馈死亡成就，替代 env 的 day_text（完整观察版，网格视野淹没反思）。
                #   见 _build_reflection_trajectory。拿不到 history（退化天）则 fallback 到 day_text，
                #   不让反思因缺数据而崩。
                _agent = react_state.get("agent")
                _history = list(getattr(_agent, "history", None) or [])
                if _history and day_steps:
                    _traj_text = _build_reflection_trajectory(_history, day_steps, day_idx)
                else:
                    _traj_text = day_text
                try:
                    await _reflect_exploration(
                        secondary, topic_tag=task.topic_tag, objective=objective,
                        trajectory_text=_traj_text, discoveries=discoveries,
                        arm_role=arm_role, qid=f"{task.id}-d{day_idx}", day_idx=day_idx,
                    )
                except Exception:  # pylint: disable=broad-except
                    logger.exception("[minecraft] 第%d夜 探索反思失败", day_idx)

            # ③ 栅栏1：等所有臂阶段1（经验/记忆反思）完。聚合在栅栏1 后才 fire（arrive 起后台聚合），
            # 保证阶段2 看到的各臂累计计数是当天已回填的（防"A demote 时 B 还在用旧技能"）。
            # 聚合天数：arrive 起后台聚合、放行进阶段2（不等聚合）。非聚合天数：仍要等齐（栅栏2 同理，
            # 各臂技能库状态需对齐），只是没有 arrive。
            if on_night is not None and secondary is not None:
                await on_night(stage=1, day_idx=day_idx)
            if is_aggregate_day:
                # on_window.arrive：等齐 + leader 起后台聚合（不 await 聚合结果）。放行进阶段2。
                # 聚合与阶段2 并行：聚合耗时可被阶段2 覆盖，但进下一天要等两者都完（栅栏3）。
                try:
                    await on_window.arrive(window, day_idx)
                except WindowAggregationError:
                    logger.exception(
                        "[minecraft] 第%d天 聚合窗口%d arrive 失败 —— abort 整个 batch"
                        "（经验层未清、完整保留，可事后手工聚合）", day_idx, window)
                    raise

            if secondary is not None and not self._no_graph:
                # ④ 阶段2：技能维护（2026-08-29）。与阶段1 完全独立的 LLM 会话——独立
                #   system prompt + 仅 5 个技能工具 + 独立 user_input（累计计数+本次失败原因）
                #   + 独立落盘（contexts/..._reflect_skill_maintenance_at<NNN>.json）。
                #   每晚必跑：告知 Agent 各技能全员累计成败 + 本次失败原因，让它 update/demote。
                #   必须在栅栏1 之后跑：栅栏1 保证各臂计数已当天回填、且聚合已 fire 不再改经验层；
                #   阶段2 读技能表当天新累计做 update/demote。no_graph（NullGraph）跳过本块（见上）。
                try:
                    await _reflect_skill_maintenance(
                        secondary, day_idx=day_idx, discoveries=discoveries,
                        arm_role=arm_role,
                    )
                except Exception:  # pylint: disable=broad-except
                    logger.exception("[minecraft] 第%d夜 技能维护反思失败", day_idx)

            # ⑤ 栅栏2：等所有臂阶段2（技能维护）完 → 技能库稳定。下一天 createplan 时
            # _inject_all_skills 读 SkillRegistry.list_all_verified()，栅栏2 保证读到一致的库。
            if on_night is not None and secondary is not None:
                await on_night(stage=2, day_idx=day_idx)

            # ⑥ 栅栏3：仅聚合天数，等聚合结果（栅栏1 起的后台 future）。失败抛 WindowAggregationError
            # → abort batch（此处抛、穿透 env.py 的 except —— 见 env.py 对应 except 修复）。
            # 非聚合天数无 future，collect 返回空计数、不阻塞。
            if is_aggregate_day:
                try:
                    await on_window.collect(window)
                    on_window_ok = True
                except WindowAggregationError:
                    # ★ 聚合失败 = abort 整个 batch（栅栏同步配套语义）：失败的窗口继续跑会把
                    #   「主脑缺这一窗口贡献」的污染带进后续训练。此时各副脑经验层未清（清层在聚合
                    #   成功之后），数据完整可复盘。重新抛出 → 穿透 env.py night_cb 的 except
                    #   （已修为 except WindowAggregationError: raise）→ loop.py rollout except
                    #   （按类型放行）→ run_arm_batch → brain.py gather → abort 整个 batch。
                    logger.exception(
                        "[minecraft] 第%d天 题内聚合窗口%d 失败 —— abort 整个 batch"
                        "（经验层未清、完整保留，可事后手工聚合）", day_idx, window)
                    raise

            # ⑦ 夜反思镜像调用（不依赖聚合）：「夜反思→昨日经历」是跨天上下文压缩，归记忆层连带。
            # ★ 2026-09-24 定（用户）：由 **记忆层派生的 context_mgmt** 控制（独立开关已删）——
            #   记忆层在（Full/−S/−D₁/−D₂/−R/−E/−K）照跑；记忆层没了（−M 拆层 / no_graph 对照组）
            #   整套消失。消失时：不调 _make_next_day_goal（不烧反思 token）。discoveries 指标
            #   本就逐夜无条件落盘（见 env.py，0908 解除反思门控），不受影响。第1天（与消失时
            #   的每一天）由 _DAY1_GOAL_OPENING 给固定开场（见 react_day day_goal 算值）。
            #   消失时 reflection="" → env.py `if reflection:` 不注入 dawn_reflection（bot 天亮
            #   步反馈也不带复盘）→ 次日 react_day 取回恒空。变量仍赋值：下方 checkpoint 写盘
            #   （write_progress 的 last_reflection 形参）与 return 都引用它。
            if self._context_mgmt:
                reflection = await _make_next_day_goal(
                    react_state.get("agent"), day_idx, discoveries, model_name)
                _last_reflection["text"] = reflection
            else:
                reflection = ""
                _last_reflection["text"] = ""

            # ★ 【昨日目标】跨天带过（context_mgmt=记忆层在时；消失时不搬，见 ⑦）：
            #   当天 createplan 立的 goal 在 CreatePlanTool.run 里已**追加**进 react_state["last_goals"]
            #   （DayPlan 权威值，一天多次 createplan 全收录、去重保序）。这里把它搬进
            #   react_state["prev_day_goals"] 供次日 day_start【昨日目标】块渲染（换行无编号），
            #   并把渲染好的换行文本写进 _last_goal 供 checkpoint 落盘（续跑恢复成单元素 list）。
            #   ★ **搬完必须清空 last_goals**（2026-09-11 晚修，用户发现）：last_goals 语义是
            #     「**当天**立的 goal 列表」，CreatePlanTool 只追加不清——不在这里清空，第 2 天
            #     的 last_goals = 第 1 天全部 + 第 2 天新增，逐日滚雪球，【昨日目标】块变成
            #     「从第 1 天起的全部历史目标」（实锤：180104 alice checkpoint last_goal 已堆
            #     12 天目标）。prev_day_goals 是覆盖写不受影响；清空只动「当天」容器。
            #   context_mgmt=False（−M / no_graph）：last_goals 仍按天清空（防它在他处被读到时
            #     堆雪球），但不进 prev_day_goals、_last_goal 写空串（checkpoint 字段仍存在=
            #     结构兼容，内容空=续跑恢复空列表）。
            _today_goals = list(react_state.get("last_goals", []))
            react_state["last_goals"] = []
            if not self._context_mgmt:
                react_state["prev_day_goals"] = []
                _last_goal["text"] = ""
            else:
                react_state["prev_day_goals"] = _today_goals
                _last_goal["text"] = "\n".join(_today_goals)

            # ⑧ checkpoint 写盘（依赖栅栏3 的 on_window_ok）：reflection 已生成，此刻一次性写完整
            # （private.db + vec + world/ + progress.json with last_reflection）。
            # on_window_ok 在非窗口边界夜为 False（is_aggregate_day=False）。聚合失败时已 raise，
            # 到不了这里。world 快照在 reflection 之前可写，但与现结构一致地放这里。
            if (on_window_ok and self._checkpoint_dir is not None and arm_key is not None):
                try:
                    await asyncio.to_thread(_snapshot_world,
                                            self._checkpoint_dir, _held_world,
                                            arm_key, window)
                except Exception:  # pylint: disable=broad-except
                    logger.exception("[minecraft] 窗口%d world 快照失败"
                                     "（不影响轨迹与聚合）", window)
                # ★ 埋点日志本地→共享盘拷回（2026-09-18）：place/eat 日志 Node 侧写本地盘
                #   （/path/to/logs/，FUSE 事故两次实锤后改），窗口边界整文件 copy2
                #   拷回共享盘 run 目录。失败只记日志——窗口拷不回还有 shutdown 兜底。
                try:
                    await asyncio.to_thread(
                        _sync_local_logs, _held_world,
                        label=f"窗口{window} arm={arm_key}")
                except Exception:  # pylint: disable=broad-except
                    logger.exception("[minecraft] 窗口%d 埋点日志拷回失败"
                                     "（shutdown 还有兜底）", window)
                if self._no_graph:
                    # ★ 对照组：secondary 是 NullGraph，write_arm_graph_checkpoint 会调
                    #   snapshot_with_vectors → AttributeError。跳过图谱快照，但**单独写
                    #   progress.json**（complete 判定 + last_reflection 注入续跑要读它）。
                    #   world 快照已在上面 _snapshot_world 写过 → checkpoint{W}/arm_{key}/world/。
                    #   这样对照组续跑读的文件与实验组同结构（progress.json + world/），
                    #   只是少了 private.db（对照组本就无图谱可快照）。
                    try:
                        await asyncio.to_thread(
                            write_progress, self._checkpoint_dir, arm_key, round_num,
                            day_idx, self._max_nights, reflection,
                        )
                    except Exception:  # pylint: disable=broad-except
                        logger.exception("[minecraft] 窗口%d 对照组 progress 写入失败"
                                         "（不影响轨迹，但该窗口无续跑点）", window)
                else:
                    try:
                        await asyncio.to_thread(
                            write_arm_graph_checkpoint,
                            self._checkpoint_dir, secondary, arm_key,
                            window, round_num, day_idx, reflection,
                        )
                    except Exception:  # pylint: disable=broad-except
                        logger.exception("[minecraft] 窗口%d 图谱 checkpoint 失败（不影响轨迹）", window)
            return reflection


        # ★ 从 worker 池独占借一台 world，整轮探索期间都归我 —— 这是「每副脑一台独立 MC server」
        # 的落地处，也是 parallel_arms=True 得以成立的原因（重生世界只影响我这一台）。
        # 借出/归还都会打日志，出问题时能看清是哪台 server（mc_port）跑的哪条轨迹。
        async with self._pool.acquire(label=arm_key or "eval") as world:
            # 让 night_cb 闭包拿到这台 worker（见 _held_world 声明处的说明）。
            _held_world = world
            # ★ 恢复世界 + 抽出生点（必须在 run_exploration 之前——它内部才 world.reset() 起 bot）。
            # **每个副脑、每一轮都做**：这是「只有记忆跨轮携带」的真正实现处，见 _fresh_world。
            world_info = await self._fresh_world(
                world, round_num=round_num, task_id=task.id, arm_key=arm_key,
                arm_name=arm_name, resume=resume,
            )

            # ★ 观战客户端必须在 _fresh_world **之后**才起：那一步会 regenerate_world()
            #   （停服→删档→重起），任何早于它的客户端都会被踢下线。
            #
            # ★★ **纯串行：先等客户端就绪，再起 bot**（2026-08-20 改）：`start()` 只到客户端进世界
            #   + mesh 就绪（不碰 bot，~25-40s），用 `to_thread` 在线程池同步等、不阻塞 event loop。
            #   就绪后紧接着 `run_exploration` 才起 bot，bot 一上线 `attach_and_record` 附身秒成 ——
            #   录像只丢 ~4.5s 命令时间。旧并行架构（客户端后台起、与 bot 出生并行）虽省墙钟，但
            #   bot 先动、客户端 ~70s 后才就绪，开头 70s 必然录不到 —— 时差根因正是并行。
            #   `start()` 失败则放弃录制（stop 清理已起的客户端），轨迹按无录制继续。
            recorder = self._start_recorder(world, arm_key=arm_key, round_num=round_num,
                                            start_night=start_night)
            if recorder is not None:
                if not await asyncio.to_thread(recorder.start):
                    logger.warning("[minecraft] %s 录制器客户端未就绪，本轮不录制（训练继续）",
                                   arm_key or "eval")
                    await asyncio.to_thread(recorder.stop)
                    recorder = None
            # ★ 观战相机探针接线（2026-09-15）：把观战账号名经 /start body.spectatorName 传给
            #   Node 侧 spectatorCam 采集器——它周期发 /data get entity <名> Pos 探「录像相机
            #   是否跟着 bot」，env 步末看门狗据此触发 resync_spectate（防脱附空镜，判据见
            #   env._spectator_needs_reattach）。recorder=None（对照组）不设，探针停用。
            if recorder is not None:
                world.spectator_name = recorder.spectator_name
            # 反思 Agent 报的「facet ← 哪几个 JS 步」旁路落盘。用 contextvar 装，
            # 所以多个副脑并发反思时各写各的文件，不会串台（见 memory/tools.py 的注释）。
            # 技能的来源步同理（落 skill_steps.jsonl），sink 签名不同故独立一个 contextvar。
            _sink_token = None
            _skill_sink_token = None
            if recorder is not None:
                from openkage.memory.tools import (reset_facet_source_sink,
                                                  reset_skill_source_sink,
                                                  set_facet_source_sink,
                                                  set_skill_source_sink)
                _sink_token = set_facet_source_sink(recorder.mark_facet_source)
                _skill_sink_token = set_skill_source_sink(recorder.mark_skill_source)
            try:
                # ★ 透明分段续跑参数：
                #   - start_night：续跑段从第 start_night 夜之后续起（day_idx/n_nights 偏移）
                #   - resume_spawn：段首不清库存/不重抽出生点，bot 带存档状态回来
                #   - spawn_pick：续跑段传 None（不重抽），新生段才传取点函数
                #   - checkpoint_cb：聚合窗口边界（每 aggregate_every 夜 unfreeze 后）自动存档 +
                #     写进度文件，供 --resume 续跑读"已到第 N 夜"。None=不存档。
                if self._checkpoint_dir is not None:
                    async def _checkpoint_cb(n_nights_done: int,
                                             metrics_snapshot: dict | None = None) -> None:
                        await asyncio.to_thread(world.save_world)
                        write_progress(self._checkpoint_dir, arm_key, round_num,
                                       n_nights_done, self._max_nights,
                                       last_reflection=_last_reflection["text"],
                                       last_goal=_last_goal["text"],
                                       metrics_snapshot=metrics_snapshot)
                        # ★ 窗口边界夜的 metrics_snapshot 镜像进 checkpoint{W}/arm_{key}/progress.json
                        # （2026-08-22，修续跑"回退路径丢前段指标"）：扁平 JSON 每夜覆盖只留最新夜，
                        # 而续跑为保库存会回退到窗口边界(W*K)——回退路径要的是**窗口边界夜**的 snapshot。
                        # 此前窗口目录的 progress.json 由 write_arm_graph_checkpoint 写、不带 snapshot，
                        # 回退时拿不到对得上的 snapshot → resume_metrics=None → 前12夜的 step_idx/
                        # deaths/advancements/tier_progress 从0重算（见下方 rollout 开头回退逻辑）。
                        # 这里在窗口边界夜把刚写好的扁平 JSON（含 snapshot，夜号==W*K）拷一份进窗口目录，
                        # 回退路径就能读到「正好对得上续跑起点」的 snapshot，前段指标零丢失。
                        # ★ 时机正确性：本回调在 night_cb（含 write_arm_graph_checkpoint）返回之后由
                        # env.py 调用，此刻扁平 JSON 已是带 snapshot 的完整版 → 覆盖窗口目录那份残缺的。
                        #   window 号 = n_nights_done // aggregate_every（与 night_cb 的窗口判定同口径，
                        #   night_cb 在 day_idx % aggregate_every == 0 时触发，而 n_nights_done == day_idx）。
                        if (metrics_snapshot and self._aggregate_every
                                and n_nights_done % self._aggregate_every == 0):
                            _w = n_nights_done // self._aggregate_every
                            _win_prog = (Path(self._checkpoint_dir)
                                         / f"checkpoint{_w}" / f"arm_{arm_key}"
                                         / "progress.json")
                            _flat = checkpoint_path(self._checkpoint_dir, arm_key, round_num)
                            if _flat.is_file():
                                try:
                                    shutil.copy2(_flat, _win_prog)
                                except Exception:  # pylint: disable=broad-except
                                    logger.exception(
                                        "[checkpoint] 窗口%d 边界 snapshot 镜像失败"
                                        "（arm=%s）—— 回退续跑该窗口将无前段指标",
                                        _w, arm_key)
                else:
                    _checkpoint_cb = None
                ep = await run_exploration(
                    world, react_day, programs=self._programs,
                    max_nights=self._max_nights, night_cb=night_cb,
                    smoke_dawn_nudge=self._smoke_dawn_nudge,
                    spawn_pick=None if resume else self._make_spawn_pick(arm_key),
                    recorder=recorder,
                    start_night=start_night,
                    resume_spawn=resume,
                    checkpoint_cb=_checkpoint_cb,
                    initial_last_events=_resume_initial_events or None,
                    resume_metrics=resume_metrics,
                    on_spawn_ready=on_spawn_ready,
                )
            finally:
                if _sink_token is not None:
                    reset_facet_source_sink(_sink_token)
                if _skill_sink_token is not None:
                    reset_skill_source_sink(_skill_sink_token)
                if recorder is not None:
                    # stop() 内部会先等启动线程落定（可能还在起客户端），避免留孤儿进程
                    await asyncio.to_thread(recorder.stop)
        return Sample(
            text=ep["trajectory_text"],
            correct=None,  # 探索无对错
            metrics={
                # 世界留痕：world_seed（固定）+ spawn_seed + 实际落点/生物群系，
                # 就是复现这条轨迹初始条件所需的全部信息。
                **world_info,
                "spawn": ep.get("spawn_actual"),
                "spawn_biome": ep.get("spawn_biome"),
                "unique_items": ep["unique_items"], "items_seen": ep["items_seen"],
                # tier 三件套（对齐 Voyager 语义，见 env.TECH_TIERS）：
                # tier_rank = **高水位**（打分用；里程碑一旦达成不被死亡收回）；
                # final_tier = 最终快照，peak>final 即「达成过但丢了」；
                # tier_first_step = 各级首达步序，直接对报 Voyager Table 1 的 iteration 数。
                "peak_tier": ep["peak_tier"], "tier_rank": ep["tier_rank"],
                "final_tier": ep["final_tier"], "final_tier_rank": ep["final_tier_rank"],
                "tier_first_step": ep["tier_first_step"],
                "tier_progress": ep["tier_progress"], "n_steps": ep["n_steps"],
                "n_nights": ep["n_nights"], "n_deaths": ep.get("n_deaths", 0),
                # map coverage（对标 Voyager 的 distance traveled，口径见 env._summarize_path）：
                # path_len 与 max_dist_from_spawn **必须并列看** —— 本域死亡频繁，「跑回死亡点
                # 捡装备」的往返会把 path_len 刷高而 bot 其实没走远。
                "path_len": ep.get("path_len", 0.0),
                "max_dist_from_spawn": ep.get("max_dist_from_spawn"),
                "n_biomes": ep.get("n_biomes", 0),
                "biomes": ep.get("biomes", []),
                # 被判为重生/传送而剔除的坐标跳变段数 + 采到坐标的步数：这两个是上面两个
                # 距离的**可信度旁证**，不报就无从判断距离是否被低估（见 _summarize_path）。
                "n_pos_jumps": ep.get("n_pos_jumps", 0),
                "n_pos_samples": ep.get("n_pos_samples", 0),
                # ReAct 代价（tier_first_step 的分母，见 _make_react_day 的 cost 参数）：
                # n_write_code 应与 n_steps 一致（两处独立计数，不一致即有 bug）。
                # ★ 续跑段例外（2026-08-21）：n_steps 改用 step_idx 含前段累计（见 env.py return），
                #   而 n_write_code 是 react_cost 只算本段 → 续跑段 n_steps > n_write_code 是预期、
                #   非bug。新跑段两者仍应相等。
                **{f"cost_{k}": v for k, v in react_cost.items()},
                # 死亡明细 + 按死因聚合（服务器权威播报，见 env.py/onSystem.js）：
                # 「怎么死的」是记忆纠错自愈的主证据 —— 同一死因是否随轮次减少。
                "deaths": ep.get("deaths", []),
                "deaths_summary": ep.get("deaths_summary", ""),
                # 服务器成就 = 权威科技树里程碑，对标 Voyager tech tree 的第二个独立指标
                # （与 tier_rank 互为印证：前者看服务器认定、后者看造出了什么工具，本应同向）。
                "n_advancements": ep.get("n_advancements", 0),
                "advancements": [a.get("advancement") for a in ep.get("advancements", [])],
                "n_night_reflections": len(ep.get("night_reflections", [])),
                # MC 累计 XP（score 数据源，2026-08-25）：跨命累计总 XP，死亡不扣减。
                # 见 env.py _xp_total 记账。score() 直接读它；旧公式（unique_items+5*tier-3*deaths）
                # 仍保留在 aggregate_eval.detail 里画曲线，便于与 0825 之前的 run 口径对照。
                "xp_total": ep.get("xp_total", 0),
                # MC 记分板权威值末夜快照（交叉校验旁证，2026-08-26）：{deathCount,totalKillCount,
                # xp,health,food,armor}，与上面的 n_deaths/xp_total 等自统计值并排留存。
                # 每夜时序在 daily_log[i].discoveries.scoreboard。只落盘双值、不比对、不修正；
                # 口径差见 env.py _extract_scoreboard（记分板 xp=当前持有会随重生压低，≠ xp_total）。
                "scoreboard": ep.get("scoreboard", {}),
                # ★ **每日量化指标时序**（2026-08-22）：night_reflections 每元素 {day_idx, discoveries, reflection}，
                #   discoveries 现在带 health/food/equipment/buildings/unique_items_so_far/xp_total 等（见 env.py
                #   discoveries 构造）。此前只记了数量 n_night_reflections、每天的实际指标跑完即丢。
                #   随 sample_metrics 带出（sample 落盘可复盘）；2026-09-12 起 brain 不再把它展平进
                #   主 JSON 的 daily_metrics（纯冗余副本、epoch 边界才落盘），消费端读 checkpoint。
                "daily_log": ep.get("night_reflections", []),
                "final_inventory": ep["final_inventory"],
                # 最终穿在身上的护甲/副手 —— 装备槽不在 final_inventory 里（槽 5-8/45 不在
                # bot.inventory 的 9-44 区间），不单独留痕就无从复盘「它到底穿甲了没有」。
                "final_equipped": ep.get("final_equipped", []),
            },
            primary_context=primary_ctx,
            secondary_context=secondary_ctx,
        )

    async def reflect(
        self, memory, task: Task, samples: list[Sample], *, arm_role=None, round_num=0,
    ) -> None:
        """no-op：Minecraft 的反思已在 rollout 的 night_cb 里逐夜发生（cadence 差异内聚在域内）。"""
        return

    def score(self, sample: Sample) -> float:
        """探索得分 = MC 累计 XP（跨命累计总 XP，死亡不扣减）。

        = MC 死亡界面那个"得分"的跨命累计版：MC 原生死亡分数是"自上次死亡以来挣的 XP"、
        重生后服务器压低 bot.experience.points 重新算；这里要的是一轮 30 夜里**所有命挣过
        的 XP 之和**，故 env.py 维护单调递增的 _xp_total（只累加正增量，死亡产生的负增量
        自动跳过），score 直接读它。

        ⚠️ **指标定义变更（2026-08-25）**：旧 score = 广度(独特物品) + 深度(5×tier_rank) −
        死亡惩罚(3×n_deaths)。改为累计 XP 后，与 0825 及之前所有 run 的 score **不可比**
        （量纲/尺度/语义全变）。旧公式不再用于打分，但其三个分量(unique_items/tier_rank/
        n_deaths)仍保留在 aggregate_eval.detail 里画曲线，score_legacy 字段保留旧公式值供对照。

        ⚠️ 已知错位（用户已知悉接受）：XP 主要来自杀怪 + 挖矿（煤矿/石英给大量 XP），钻石矿
        本身 XP 极少——故 XP 衡量的是"刷怪刷矿产出"而非"上科技树"。钻石达成率走 tier_unlock_rate
        （不依赖 score），仍是验证"图谱帮 agent 跨台阶"的有效信号。
        """
        return float(sample.metrics.get("xp_total", 0))

    def aggregate_eval(self, samples: list[Sample]) -> EvalScore:
        """一组 eval 的分 = **每轮探索得分的均值**（复用 self.score，与训练侧同一把尺）。

        与 math/alfworld 求和的区别：那两个域的 eval 是**一批不同的题**，「N 题里对了几题」
        求和天然可比；本域的 eval 是**同一个中性目标重复 n_eval 次**（task 内容全同），
        求和会让 gap 随 n_eval 线性放大、不同 n_eval 的 run 不可比。取均值后，n_eval 只影响
        估计精度、不影响量纲 —— 这正是「多跑几轮取平均 gap 压方差」要的（本域探索方差大：
        同一目标这轮挖到铁、下轮死在洞里）。

        detail 里同时给广度/深度分列（画两条曲线）+ n_deaths/n_advancements 均值：
        死亡率下降与成就里程碑是「记忆是否真的在帮它少踩坑」的直接证据。
        另给 tier_unlock_rate（各级达成率）+ tier_first_step_mean（各级首达平均步序）：
        **这两个才是对报 Voyager Table 1 的量**（它报的是「达成该级用了几个 iteration」+
        括号里的 (k/3) 达成率），tier_rank 均值只是本域自用的合成标量。
        """
        n = len(samples) or 1
        avg = lambda k: sum(s.metrics.get(k, 0) for s in samples) / n  # noqa: E731
        # 各 base 深度(1-9)达成率 + 首达步序均值（只在达成的样本上取均值；未达成的样本不计入
        # 分母，与 Voyager「N/A (0/3)」的报法一致：没达成就没有步数可报，不能拿 max_steps 冒充）。
        # ★ 2026-09-05 改：tier_first_step 的 key 从 9 级 tier 名(workbench..end)改成 base depth
        #   整数(1-9)，与 env.py tier_progress 存 depth 一致。旧 run 存旧名 key，此处取不到 →
        #   该 depth 报 0 达成率（旧 run 本就要重跑，不可横比）。
        tier_unlock_rate, tier_first_step_mean = {}, {}
        for depth in range(1, 10):
            hit = [s.metrics.get("tier_first_step", {}).get(depth) for s in samples]
            hit = [h for h in hit if h is not None]
            tier_unlock_rate[depth] = len(hit) / n
            if hit:
                tier_first_step_mean[depth] = sum(hit) / len(hit)
        return EvalScore(
            score=sum(self.score(s) for s in samples) / n,
            total=len(samples),
            detail={
                # score_legacy = 旧公式值(unique_items + 5*tier_rank - 3*n_deaths)，2026-08-25
                # score 改用 xp_total 后保留此列做对照（画曲线/与 0825 之前的 run 比时用）。
                # 不参与 self.score()，纯留痕。
                "score_legacy": avg("unique_items") + _TIER_WEIGHT * avg("tier_rank")
                                - _DEATH_PENALTY * avg("n_deaths"),
                "xp_total": avg("xp_total"),
                # MC 记分板权威值末夜快照的**跨样本均值**（交叉校验旁证，2026-08-26）：6 个准则各取
                # 样本末夜值的均值，缺失(None)不计入分母。与上面的 xp_total/n_deaths 自统计均值
                # 并排留存对照。口径差见 env.py _extract_scoreboard（记分板 xp≠xp_total）。
                "scoreboard": {
                    k: (lambda vs: sum(vs) / len(vs) if vs else None)(
                        [s.metrics.get("scoreboard", {}).get(k)
                         for s in samples
                         if isinstance(s.metrics.get("scoreboard", {}).get(k), (int, float))])
                    for k in ("deathCount", "totalKillCount", "xp", "health", "food", "armor")
                },
                "unique_items": avg("unique_items"),
                "tier_rank": avg("tier_rank"),
                "max_tier_rank": max((s.metrics.get("tier_rank", 0) for s in samples), default=0),
                "n_deaths": avg("n_deaths"),
                "n_advancements": avg("n_advancements"),
                # 对报 Voyager Table 1 的两列：达成率 (k/n) 与达成所需步数
                "tier_unlock_rate": tier_unlock_rate,
                "tier_first_step_mean": tier_first_step_mean,
                # map coverage（对报 Voyager 的 distance traveled）。**两个距离都报**：
                # path_len 会被死亡往返刷高，max_dist_from_spawn 不会 —— 只报前者会把
                # 「死了很多次、来回跑」误读成「探索范围大」（口径见 env._summarize_path）。
                "path_len": avg("path_len"),
                "max_dist_from_spawn": _avg_opt(samples, "max_dist_from_spawn"),
                "n_biomes": avg("n_biomes"),
                # 距离的可信度旁证：跳变段数（剔掉的重生瞬移）与坐标采样步数。
                # 不报这两个，距离被低估是静默的。
                "n_pos_jumps": avg("n_pos_jumps"),
                "n_pos_samples": avg("n_pos_samples"),
                # ★ tier_first_step_mean 的**代价分母**：它数的是 write_code 步数，而一步
                # write_code 背后还有若干不推进世界的 ReAct 轮。报「几步解锁 stone」而不报
                # 一步多贵，这个数字就无法与别的系统横向核验（见 _make_react_day 的 cost）。
                "cost_n_write_code": avg("cost_n_write_code"),
                "cost_n_react_turns": avg("cost_n_react_turns"),
                "cost_n_llm_calls": avg("cost_n_llm_calls"),
                "cost_n_retrieve": avg("cost_n_retrieve"),
                "cost_n_expand": avg("cost_n_expand"),
                # 世界留痕：seed 固定（应全部相同），出生点每条不同 —— 后者才是本域样本多样性来源，
                # 也是复现单条轨迹初始条件所需的信息。
                "spawns": [s.metrics.get("spawn") for s in samples],
                # 落点生物群系：随机出生时这是解释分数波动的第一现场
                # （落森林能砍树、落雪原/海边可能什么都做不了）。
                "spawn_biomes": [s.metrics.get("spawn_biome") for s in samples],
                # ★ 累计全量清单（直接落盘留痕，2026-08-25）：n_advancements / unique_items
                #   只是个数；此前全量清单只存在 sample.metrics 里、没进落盘的 detail。这里取
                #   **并集**而非均值——它门是只增不减的累积集合，多样本时并集才是该 arm 的全集。
                #   - items_seen：见 env.py，步末采样库存+装备的累积 set（sorted list 落盘）。
                #   - advancements：服务器权威科技树里程碑名列表。
                "items_seen": sorted(set().union(*(s.metrics.get("items_seen") or []
                                                   for s in samples))),
                "advancements": sorted(set().union(*(s.metrics.get("advancements") or []
                                                     for s in samples))),
            },
        )

    def shutdown(self) -> None:
        """停掉所有 worker（bot + mineflayer + **MC server**）。由 runner 的 finally 兜住。

        worker 的 MC server 必须停：它们是本次 run 的临时资产（每台约 2.5G RSS），
        不停就是纯泄漏。手工起的模板服（:25567）不在池里，不受影响。
        """
        # ★ 埋点日志兜底拷回（2026-09-18）：窗口边界拷过，但「最后窗口之后到停 run」
        #   这段的日志只在本地盘。SIGTERM/跑满 finally 都会走到这里，先拷再停。
        #   pool._worlds 各臂的 _log_sync_targets 在 _fresh_world 设定，覆盖全部臂。
        for _w in self._pool._worlds:  # pylint: disable=protected-access
            try:
                _sync_local_logs(_w, label="shutdown")
            except Exception:  # pylint: disable=broad-except
                logger.exception("[minecraft] shutdown 埋点日志拷回失败（arm 见映射）")
        self._pool.shutdown_all()


def _avg_opt(samples, key: str) -> float | None:
    """在**取到值的样本上**求均值；一个都没取到则返回 None（不是 0）。

    存在的理由：`aggregate_eval` 里那个 `avg` 用的是 `metrics.get(k, 0)` —— 对
    `max_dist_from_spawn` 这种**可能为 None**（拿不到出生坐标）的指标，缺失会被当 0 计入
    分母，把均值静默拉低，而 0 在这里的字面意思是"一步都没离开出生点"。两种情况读起来
    一样、含义完全相反。与 `tier_first_step_mean` 的「未达成不计入分母」是同一条纪律。
    """
    vals = [s.metrics.get(key) for s in samples]
    vals = [v for v in vals if isinstance(v, (int, float))]
    return sum(vals) / len(vals) if vals else None


def _as_count(value) -> int:
    """把 n_train/n_eval 配置值转成本域要造几个 task。

    本域没有固定题库（task 是按数量现造的），故 "all" 无边界可言 → 取 1。
    其余按 int 解析，非法值退回 1（宁可少跑一轮也不要因配置笔误炸掉整个 run）。
    """
    if isinstance(value, str):
        if value.strip().lower() == "all":
            return 1
        try:
            return max(0, int(value.strip()))
        except ValueError:
            logger.warning("[minecraft] n_* 配置值非法(%r)，按 1 处理", value)
            return 1
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 1


def _explore_task(task_id: str) -> Task:
    """一个自由探索 Task：统一中性目标 + 阶段无关标签（无预设方向，见 _EXPLORE_OBJECTIVE 说明）。"""
    return Task(id=task_id, topic_tag=_EXPLORE_TAG, question=_EXPLORE_OBJECTIVE, gold=None, payload=None)


# ──────────────── MC 存档快照 ────────────────
# latest_window_with_world 已提升至 openkage/orchestrate/checkpoint.py（框架层公共函数），
# 供 world 恢复与本域续跑判定共用同一窗口号来源（单一真相，见 resume_graph_source）。


def _snapshot_world(checkpoint_dir, world, arm_key: str, window: int) -> None:
    """把 MC 世界存档拷到 checkpoint{W}/arm_{key}/world/（只读拷贝）。

    由 rollout night_cb 在 on_window 成功后调用——框架层 submit() 已写图谱 checkpoint，
    此函数补 MC 专属的 world/ 部分，两者合成 checkpoint{W}/ 自包含目录。
    失败只 logger.exception——存档缺了图谱仍在，最坏是这个窗口的续跑时 bot 出生点不确定。
    """
    import shutil
    arm = arm_key or "eval"
    arm_dir = Path(checkpoint_dir) / f"checkpoint{window}" / f"arm_{arm}"
    arm_dir.mkdir(parents=True, exist_ok=True)
    if world is None or not world.mc_dir:
        return
    try:
        world.save_world()
    except Exception:  # pylint: disable=broad-except
        logger.exception("[minecraft] 窗口%d save_world 失败（arm=%s）—— 存档可能非最新", window, arm)
    world_save = Path(world.mc_dir) / "world"
    if world_save.is_dir():
        dest = arm_dir / "world"
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        try:
            shutil.copytree(world_save, dest)
        except Exception:  # pylint: disable=broad-except
            logger.exception("[minecraft] 窗口%d 拷 world/ 失败（arm=%s）", window, arm)


def _sync_local_logs(world, *, label: str = "") -> None:
    """埋点日志本地盘→共享盘整文件拷回（2026-09-18 FUSE 二次事故后建）。

    place_log/eat_log 由 Node 侧 appendFileSync 写到本地盘 /path/to/logs/<run>/，
    本函数在**窗口边界**与 **shutdown 兜底**两个时机把已有文件 copy2 到共享盘 run 目录
    （整文件覆盖写，无 FUSE 高频 append）。映射来自 _fresh_world 设的
    world._log_sync_targets（{本地路径: 共享盘目标}）。

    - 本地文件不存在/为空：跳过（该臂此日志无事件，正常）。
    - 单文件失败不阻断其他文件；全部失败也只记日志（窗口拷不回还有 shutdown 兜底，
      shutdown 拷不回则这段日志永久留在本地盘，可手工救）。
    """
    import shutil
    targets = getattr(world, "_log_sync_targets", None) or {}
    for local, remote in targets.items():
        try:
            src = Path(local)
            if not src.is_file() or src.stat().st_size == 0:
                continue
            dest = Path(remote)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
        except Exception:  # pylint: disable=broad-except
            logger.exception("[minecraft] 埋点日志拷回失败 %s %s→%s",
                             label, local, remote)


def _restore_world_from_checkpoint(ckpt_world: Path, mc_dir: str,
                                    arm_key: str, window: int) -> None:
    """续跑缺陷②（2026-08-19）：把窗口边界的干净 world/ 拷回 worker 的活存档目录。

    `_snapshot_world` 在窗口边界（unfreeze 后）拷的 world/ 是干净状态——bot 库存/位置完整。
    SIGTERM/SIGINT 中途停时活存档被一次"死亡重生时刻 save_world"覆盖、库存丢失，续跑若读
    活存档就继承了这个丢档。本函数在 resume_world 之前把干净 world/ 拷回 mc_dir/world/，
    让 resume_world 读到窗口边界状态（库存不丢），代价是重跑窗口之间的几天（start_night 已回退）。

    MC server 起服时从 `mc_dir/world/`（level-name）读存档，故拷到这里即可被 resume_world 读到。
    """
    import shutil
    dest = Path(mc_dir) / "world"
    try:
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        shutil.copytree(ckpt_world, dest)
        logger.info("[minecraft] 续跑从窗口%d 干净存档恢复 world/（arm=%s）—— 防中途存档库存丢失",
                    window, arm_key)
    except Exception:  # pylint: disable=broad-except
        logger.exception("[minecraft] 续跑拷回窗口%d world/ 失败（arm=%s）—— 退回活存档，"
                         "库存可能不全", window, arm_key)

# ⚠️ 这里原有一个域私有的 `_dump_step_input()`（一天一份、覆盖写、失败只警告）——
#    2026-08-10 换成框架层的 `openkage.agent_io.ContextDumper`：三个 Agent
#    （本域 ReAct / 副脑反思 / 主脑聚合）共用同一份留痕实现与同一个输出目录。
#    留痕语义**一字未改**（一次运行一个文件、覆盖写、记 n_llm_calls、异常只警告一次），
#    只是不再各写一份。见 `_make_react_day` 里挂 dumper 那处。

def _sanitize_tag(s: str) -> str:
    """把人设首行/标识压成安全的文件名片段（只留字母数字下划线连字符）。"""
    return re.sub(r"[^0-9A-Za-z_一-鿿-]+", "_", s).strip("_") or "x"


def _apply_skill_usage_counts(secondary, primary, skill_calls: dict,
                              skill_sources: "dict | None" = None,
                              round_num: int = 0,
                              skill_registry=None) -> None:
    """把当天技能调用计数（{slug:{ok,fail}}）按**注入来源**分流回填进对应库的 skill 表。

    纯确定性操作（无需 LLM）：这是降级高失败率技能的数据基础。

    ★ **新路径**（`skill_registry` 传入时）：
      - `skill_sources` 现在是 `{slug: arm_key}`（arm_key 即哪个臂的 SkillStore 产出了该技能）。
      - 通过 `skill_registry.store_for(arm_key).add_usage_counts(slug, ok, fail)` 路由到作者库。
      - 技能不进公有脑，主脑 primary 不参与 skill 计数。

    ★ **旧路径**（`skill_registry=None`，向后兼容旧调用点/测试）：
      - `skill_sources` 是 `{slug: "primary"|"secondary"}`。
      - "secondary" 或来源缺失 → 写副脑 secondary；"primary" → 写主脑 primary。
    """
    if not skill_calls:
        return

    if skill_registry is not None:
        # ★ 新路径：arm_key 路由到各臂 SkillStore
        _sources = skill_sources or {}
        for slug, counts in skill_calls.items():
            ok = int((counts or {}).get("ok", 0))
            fail = int((counts or {}).get("fail", 0))
            if ok == 0 and fail == 0:
                continue
            arm_key = _sources.get(slug)
            if arm_key is None:
                continue
            store = skill_registry.store_for(arm_key)
            if store is None:
                continue
            try:
                store.add_usage_counts(slug, ok=ok, fail=fail, at=round_num)
            except Exception:  # pylint: disable=broad-except
                logger.warning("[minecraft技能] SkillStore(%s) 计数回填失败 slug=%s",
                               arm_key, slug, exc_info=True)
        return

    # ★ 旧路径（向后兼容）：primary / secondary 分流
    _sources = skill_sources or {}

    def _split(src_filter):
        """从 skill_calls 里筛出归属 src_filter 的 (slug, counts) 对（只留有计数的）。"""
        out = []
        for slug, counts in skill_calls.items():
            ok = int((counts or {}).get("ok", 0))
            fail = int((counts or {}).get("fail", 0))
            if ok == 0 and fail == 0:
                continue
            if _sources.get(slug) == src_filter:
                out.append((slug, ok, fail))
        return out

    def _do_update(conn, items, tag):
        """把 [(slug,ok,fail),...] 累加进 skill 表已有行（UPDATE 不命中=非本库技能，跳过）。"""
        for slug, ok, fail in items:
            cur = conn.execute(
                "UPDATE skill SET success_count = success_count + ?, "
                "fail_count = fail_count + ?, updated_at = ? "
                "WHERE slug = ?",
                (ok, fail, round_num, slug),
            )
            if cur.rowcount > 0 and (ok or fail):
                logger.info("[minecraft技能] %s 当天 +%d成功 +%d失败（回填%s）",
                            slug, ok, fail, tag)
        conn.commit()

    # 副脑 secondary：各臂私有库。归属判定 = 来源 "secondary" 或来源缺失（向后兼容旧埋点）。
    sconn = getattr(secondary, "_conn", None) if secondary is not None else None
    if sconn is not None:
        _s_items = []
        for slug, counts in skill_calls.items():
            ok = int((counts or {}).get("ok", 0))
            fail = int((counts or {}).get("fail", 0))
            if ok == 0 and fail == 0:
                continue
            if _sources.get(slug) == "primary":  # 公有技能归主脑，不进副脑
                continue
            _s_items.append((slug, ok, fail))
        if _s_items:
            _s_lock = getattr(secondary, "_lock", None)
            try:
                if _s_lock is not None:
                    with _s_lock:
                        _do_update(sconn, _s_items, "副脑")
                else:
                    _do_update(sconn, _s_items, "副脑")
            except Exception:  # pylint: disable=broad-except
                logger.warning("[minecraft技能] 副脑 skill_calls 回填失败（不影响反思）",
                               exc_info=True)

    # 主脑 primary：多臂共享库。来源 "primary" 的写这里，必须持锁。
    pconn = getattr(primary, "_conn", None) if primary is not None else None
    if pconn is not None:
        _p_items = _split("primary")
        if _p_items:
            _p_lock = getattr(primary, "_lock", None)
            try:
                if _p_lock is not None:
                    with _p_lock:
                        _do_update(pconn, _p_items, "主脑")
                else:
                    _do_update(pconn, _p_items, "主脑")
            except Exception:  # pylint: disable=broad-except
                logger.warning("[minecraft技能] 主脑 skill_calls 回填失败（不影响反思）",
                               exc_info=True)


def _skill_js_syntax_error(js_code: str) -> str | None:
    """技能 js_code 的 JS 语法校验（acorn 经 node 子进程）。返回错误摘要；None=合法。

    ★ 注入层熔断的判据（0914 加，见 _inject_all_skills 调用处注释）。与入库校验
    openkage.memory.tools._js_syntax_invalid 同一套解析（acorn、ecmaVersion 2022、
    allowAwaitOutsideFunction——代码注入后会被包进 async IIFE，顶层 await 合法），
    但独立实现一份：那侧在 memory 工具闭包里、且注入层不能因 memory 模块重构而失守
    （两道防线各自独立失效域）。环境不可用（无 node / 无 acorn）返回 None 放行
    ——注入层是第二道防线，主防线在入库侧；环境缺失时不阻断注入主流程。
    性能：一天注入一次、每技能一个 ~10ms 的 node 子进程，技能数个位数，可忽略。
    """
    code = (js_code or "").strip()
    if not code:
        return None
    import shutil
    import subprocess
    import tempfile
    node = shutil.which("node")
    if not node:
        return None
    acorn = _MC_SERVER_DIR / "node_modules" / "acorn"
    if not (acorn / "package.json").exists():
        return None
    # 代码走临时文件不走 argv：js_code 含任意引号/反斜杠，argv 有注入与转义两层坑
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
        return None
    finally:
        import os
        os.unlink(tmp)
    if r.returncode != 0:
        err = (r.stderr or "").strip().splitlines()
        return err[0] if err else "语法错误"
    return None


def _wrap_skill_for_injection(skill: dict) -> str:
    """把单个技能的 js_code 包一层**埋点代理**，返回可拼进 programs 注入的代码字符串。

    原 `async function NAME(...){...}` 改名为 `_skill_<slug>_raw`（原函数体原样保留），
    外层 NAME 变成 try/catch 代理：成功/失败都把结构化计数 push 进 `bot.openkageSkill`
    （onSkill 观察插件），失败照常 throw（让 Agent 看到错误反馈）。步末 onSkill 事件随
    /step 响应回传 Python，由 env._collect_skill_events 读结构化数组累加进 skill_calls
    （见 _apply_skill_usage_counts → 聚合降级的数据基础）。

    ★ 走结构化通道而非 console.log('[skill] ...') 字符串（2026-08-26 改）：旧法让 JS 输出
      格式与 Python 解析逻辑靠字符串契约维持同步，任一端漂移就静默丢数且永不报警（同款病：
      world_healthy 字段死一个月）；且 onLog 是给 LLM 看的文本通道（40 行配额），被并发日志
      挤掉/截断就丢。onSkill 是机读事件通道（与 onSystem 同机制），丢数变可检测。
    ★ 代码不改写技能逻辑本身——只套壳，原函数体原样保留为 _raw，Agent 调用方式不变。
    ★ 结构异常（抽不到 async function 名）：原样注入不埋点（兜底，retrieve_skills 已校验过）。

    抽成模块级纯函数（2026-08-26）以便 smoke 直接测真代码：原逻辑埋在 react_day 闭包
    深处无法外部调用。这里只依赖 skill dict（slug/js_code/name），无副作用。
    """
    import re as _re
    slug = skill["slug"]
    js = skill["js_code"]
    # 抽函数名（与 openkage.memory.tools._js_func_name 同逻辑，但这里在域侧）
    m = _re.search(r"async\s+function\s+([A-Za-z_$][\w$]*)", js)
    if not m:
        # 结构异常（retrieve_skills 已校验过，兜底）：原样注入不埋点
        return f"// 技能 {slug}：{skill['name']}\n{js}"
    fname = m.group(1)
    # 原 `async function NAME(` → `async function _skill_<slug>_raw(`
    raw_name = f"_skill_{slug}_raw"
    wrapped = js.replace(f"async function {fname}(", f"async function {raw_name}(", 1)
    # 外层代理：成功/失败都 push 结构化计数到 bot.openkageSkill，失败照常 throw（让 Agent 看到错误反馈）。
    # ★ slug 由 _slugify 产出（仅 [a-z0-9_]），进 JS 单引号字符串字面量安全（无引号/反斜杠需转义）。
    # ★ 埋点 push 包在自己的 try/catch 里：埋点失败绝不影响技能执行本身（不吞技能错误，只吞埋点错误）。
    # ★ `bot.openkageSkill &&` 守卫：旧版 Node（未注册 onSkill 插件）或 bot 未就绪时不炸。
    proxy = (
        f"async function {fname}(...args){{"
        f"try{{const r=await {raw_name}(...args);"
        f"try{{bot.openkageSkill&&bot.openkageSkill.push({{slug:'{slug}',ok:1}});}}catch(_m){{}}"
        f"return r;}}"
        f"catch(e){{try{{bot.openkageSkill&&bot.openkageSkill.push("
        f"{{slug:'{slug}',ok:0,err:((e&&e.message)||e)}});}}catch(_m){{}}"
        f"throw e;}}"
        f"}}"
    )
    return f"// 技能 {slug}：{skill['name']}\n{wrapped}\n{proxy}"


# 全点增量去重的已见集合提取（2026-09-13）：统一注入（createplan/revise/入夜）与 Agent 主动
# retrieve/retrieve_skills 都以上文已出现内容为排除集，只注入增量——旧消息一字不改（前缀缓存
# 友好：改写旧段会在每个注入点制造缓存大 miss），失效模式良性（漏去重=多几个 token）。
#: 概念头渲染契约（MemorySubgraph.render）：`「名」(slug=xxx)`。
_SEEN_SLUG_RE = re.compile(r"slug=([a-z0-9_]+)")
#: 签名行渲染契约（_render_signature_block / RetrieveSkillsTool，格式必须一致）：
#: `- {签名} —— {意图}（前置：…）`。签名可含空格/括号，截到「 —— 」为止。
_SEEN_SIG_RE = re.compile(r"^- (.+?) —— ", re.MULTILINE)


def _collect_seen(history: list) -> tuple[set[str], set[str]]:
    """扫当天 history，返回上文已注入过的 `(概念 slug 集合, 技能签名集合)`。

    供统一注入（_unified_inject）与 Agent 主动检索（RetrieveMemoryTool.seen_provider）
    共用同一口径：上文已经有的概念/签名不再重复注入/返回（增量去重）。

    扫描范围（与 marker 契约对齐，见 react_tools.py 的词汇表三件套）：
    - role=tool 消息的 EXP_BLOCK 段（createplan/revise/入夜注入 + Agent retrieve 结果
      同一渲染契约）、SKILL_BLOCK 段（createplan/revise 附的签名块）；
    - role=user 消息的 MEM_BLOCK 段（day_start/day_resume 的【记忆】块）——记忆层概念
      全量常驻，retrieve 查 layer=memory/both 时应全被去重（dump_memory_layer 走
      MemorySubgraph.render，输出天然带 `(slug=…)`）。
    marker 段之外的正文不扫（观察/指令等无 slug 渲染契约，避免误匹配）。
    slug 是主键、跨层唯一，EXP 段与 MEM 段的概念合并成一个排除集（layer=experience
    查询时记忆层 slug 恒不命中，无害）。

    ★ history 即 LLM 实际所见：_compact 的 stale_plan 分支只丢计划正文（marker 段
      按标记保留）、stale_obs 分支只砍观察块（EXP 段保留），故被扫到的内容必然还在
      上下文里——不存在"扫到但已被压缩掉"的口径分裂。
    """
    from .react_tools import (EXP_BLOCK_MARKER, MEM_BLOCK_MARKER, SKILL_BLOCK_MARKER,
                              INJECT_BLOCK_MARKER)
    slugs: set[str] = set()
    sigs: set[str] = set()

    def _scan_exp_and_mem(body: str) -> None:
        for marker in (EXP_BLOCK_MARKER, MEM_BLOCK_MARKER):
            if marker in body:
                slugs.update(_SEEN_SLUG_RE.findall(body.split(marker, 1)[1]))

    for m in history or []:
        role = m.get("role")
        body = str(m.get("content") or "")
        if role == "tool":
            _scan_exp_and_mem(body)
            if SKILL_BLOCK_MARKER in body:
                seg = body.split(SKILL_BLOCK_MARKER, 1)[1]
                # 截到消息末尾（SKILL 段之后无其它 marker；_compact 截段逻辑同款边界）
                sigs.update(g.group(1).strip() for g in _SEEN_SIG_RE.finditer(seg))
        elif role == "user":
            _scan_exp_and_mem(body)
    return slugs, sigs


def _make_react_day(
    base_messages: list[dict], model_name: str | None,
    *, primary=None, secondary=None, topic_tag: str = "", task_id: str = "?",
    context_dir: "Path | None" = None, traj_tag: str = "traj",
    cost: "dict | None" = None, skill_registry=None, react_state: "dict | None" = None,
    start_night: int = 0,
    react_max_steps: int = 999,
    ablation: "str | None" = None,
):
    """返回 `react_day` 回调：跑**一整天**的 ReAct。

    Agent 自主 createplan / updateplan / retrieve / expand / write_code，一天一轮，
    天亮由 env 侧置 `state["day_over"]`、`should_stop` 读它收尾。

    每天的上下文是重建的：base（人设 + 域规则）+ 私有记忆全量 + 公共经验条数
    + 昨夜复盘 + 当前观察。昨天的经历只通过**记忆图谱与夜反思文本**带过来
    （每天新建 ReActAgent，history 不跨天）。

    `cost`：调用方给的**累加器 dict**（可选）。工具计数器是每天新建的（见下），一天结束就
    丢掉，只留在日志行里 —— 而「解锁 stone 用了多少代价」需要整轮的累计值。所以这里每天
    往调用方的 dict 上累加，由它落进 `Sample.metrics`。
    ★ **为什么必须报这个**：`tier_first_step` 数的是 write_code 步数，可直接对报 Voyager
      Table 1 的 iteration 数 —— 但我们一步 write_code 背后还有 createplan/retrieve/expand
      这些**不推进世界**的 ReAct 轮（实测第 1 天 alice 48 个 ReAct 轮里只有 34 次 write_code）。
      只报 write_code 步数会让「我们更快」变成一个无法核验的说法：审稿人只需问一句"你们
      一个 iteration 是不是更贵"。分母必须和分子一起报。
    """
    # ★ day_counter 从 start_night 起算（2026-09-03 修续跑 day_idx 错位）：
    # 续跑从第 start_night 夜之后接（env 主循环 day_idx=start_night+1），但本计数器原本
    # 从 0 重数 → 续跑第一天 react_day 内 day_idx=1，与 env 的 17 脱节，导致 user_input
    # 渲染成「今天是第 1 天」却配上「第17天（今天）的核心目标」自相矛盾（day_goal 取自
    # checkpoint 的 last_reflection 是对的，错在 day_idx）。从 start_night 起算后，续跑首天
    # n=start_night(16) → 进 if +1 → 17，与 env 对齐；dump 文件名 day17 也对齐。同一天 STOP
    # 续跑（resume=True 且 plan 非空）不进 if，day_idx 不变，行为不变。
    day_counter = {"n": start_night}
    #: 跨「续跑」保留的当天状态——见 react_day 里 resume 的说明
    #: - plan：本轮计划（STOP 续跑时复用，不清空）。
    #: - prev_history：上一轮 ReAct 的 result.history（天没亮就续跑时存下，供下一轮灌入，
    #:   实现「接着上下文继续追问」而非从空白重开。见 async_run 的 initial_history 参数）。
    day_state: dict = {"plan": None, "prev_history": None, "resume_idx": 0}  # resume_idx=当天续跑序号,reasoning落盘按轮分段

    async def react_day(state: dict, do_step, render_obs, get_events,
                        resume: bool = False):
        """跑**一整天**的 ReAct。天亮由 env 侧置 state["day_over"]，should_stop 读它立刻收尾。"""
        from openkage.agent_core.agent.react import ReActAgent
        from openkage.agent_core.tool.registry import ToolRegistry
        from openkage.llm import get_llm

        from .react_tools import (
            EXP_BLOCK_MARKER, MEM_BLOCK_MARKER, OBS_SEPARATOR, SKILL_BLOCK_MARKER,
            INJECT_BLOCK_MARKER,
            CompactingContextBuilder, CreatePlanTool, DayPlan, ExpandMemoryTool,
            RetrieveMemoryTool, RetrieveSkillsTool, UpdatePlanTool, WriteCodeTool,
        )

        # ── 新的一天 vs 续跑同一天 ────────────────────────────────
        # ReActAgent 有**两个**出口：EXTERNAL_STOP（天亮，我们要的）和 STOP（LLM 不再调工具）。
        # LLM 可能在计划全标 success 后就 STOP —— 它以为"活到天亮"这一步做完了，而游戏里
        # 天还没亮。若把这当成新的一天（day_idx+1、计划清空、dump 换文件），"第 2 天"
        # 其实会是同一个夜晚的后半段。
        # 现在 STOP 且天没亮 → 外层带 resume=True 再调本函数：**保留计划与 day_idx**，
        # 只是重开一轮 ReAct 接着跑。plan 存在 day_state 里跨轮复用。
        # ★ 续跑时 prev_history 也跨轮复用（2026-09-06）：把上一轮 result.history 灌进新一轮
        #   ReActAgent（async_run 的 initial_history），让续跑轮「接着上下文继续追问」而非从
        #   空白重开 —— 否则续跑轮丢失当天前半段对话、只能靠 plan 状态 + 最后一步反馈「猜」
        #   自己之前干了什么。新一天（非 resume）时 prev_history 置 None，与跨天清空一致：
        #   跨天本来就该只靠记忆图谱 + 黎明复盘文本带过来（见下 own_memory_block 的信息瓶颈）。
        if not resume or day_state.get("plan") is None:
            day_counter["n"] += 1
            day_state["plan"] = DayPlan()
            day_state["prev_history"] = None  # 新一天：清前轮对话，跨天只走记忆图谱
            day_state["resume_idx"] = 0  # 新一天：续跑序号归零
        else:
            # 续跑同一天：reasoning 落盘按轮分段（r0/r1/r2…），避免覆盖丢前几轮思维链
            day_state["resume_idx"] = day_state.get("resume_idx", 0) + 1
        day_idx = day_counter["n"]
        resume_idx = day_state.get("resume_idx", 0)
        prev_history = day_state.get("prev_history")
        plan = day_state["plan"]
        # 工具每轮新建（计数器归零，用于日志看这一轮动了几步）；plan 实例则按上面的规则复用。
        wc = WriteCodeTool(
            do_step=lambda code, ms, explain: do_step(code, ms, explain),
            default_timeout_ticks=_STEP_TIMEOUT_DEFAULT_TICKS,
            max_timeout_ticks=_STEP_TIMEOUT_MAX_TICKS,
        )
        # retrieve：Agent 自主检索入口。★ seen_provider（2026-09-13 全点去重）：Agent 调用
        # 自动带"上文已注入概念"排除集——上文已有的概念只给增量+dedup note（工具内自动），
        # expand 不去重（逃生门）。框架注入直调时传显式 exclude，以显式值优先。
        # ★ holder 存 **agent 引用**（不是 history 取值）：工具构造在 agent 之前，而
        #   self.history 在 ReActAgent.__init__ 里**不存在**（react.py 只在 _run/_async_run
        #   开头赋）——若在这里存取值，拿到的永远是 None 且不再更新（去重静默失效）。
        #   存引用、provider 调用时**惰性读** getattr(agent, "history", None)：工具只在
        #   异步循环里被调，那时属性必已赋值；与 night_cb 读 react_state["agent"].history
        #   同一模式（原地 append 的列表引用，全程指向当天进行中的对话）。
        _seen_agent: dict = {"agent": None}
        _cur_history = lambda: getattr(_seen_agent["agent"], "history", None)  # noqa: E731
        retr = RetrieveMemoryTool(primary=primary, secondary=secondary, texts=REACT,
                                   seen_provider=lambda: _collect_seen(_cur_history() or [])[0])
        # expand：沿记忆图谱的边深挖。与 retrieve 分工 —— retrieve 管入口
        # （向量锚定），expand 管遍历（Agent 自己选方向）。见 ExpandMemoryTool 的 docstring。
        # ★ 消融 flat_retrieval（2026-09-18）：expand 工具下架——平面检索没有「沿边走一跳」
        #   可言（检索降级后 Agent 也看不到边，expand 给的是空方向的徒劳步）。工具列表少一个
        #   expand，与 RAG baseline 的工具面对齐。
        exp = (None if ablation == "flat_retrieval"
               else ExpandMemoryTool(primary=primary, secondary=secondary, texts=REACT))
        # retrieve_skills：按意图检索已学技能的**签名**。立计划时已默认按成功数+时间排序注入
        # top-N 签名块 + goal 检索补充（见 _render_skill_delta）；本工具供 Agent 执行中想用范围外
        # 技能时主动查签名（代码已全量注入 programs，查到签名就能直接 await）。与 retrieve（查经验记忆）分工。
        # ★ 检索源 = skill_registry.retrieve_all（查 skill_{arm}_vec.db）——技能不在 concept 图谱里，
        #   绑 primary/secondary 会恒 0 命中（0902 实测 113/113 全 miss）。registry=None（eval）→ miss 文案。
        # ★ seen_provider：已注入签名只给增量+dedup note（同 retrieve 机制）。
        # ★ 消融 no_exp_skills（2026-09-18）：工具下架——库已是 NullSkillStore（brain.py 装配）
        #   恒空，留着只会让 Agent 花步数查一个恒 miss 的工具。签名块注入同步跳过（见
        #   _unified_inject 的 SKILL 段门控）。
        sk_retr = (None if ablation in ("no_exp_layer", "no_exp_skills")
                   else RetrieveSkillsTool(primary=primary, secondary=secondary, texts=REACT,
                                           registry=skill_registry,
                                           seen_provider=lambda: _collect_seen(_cur_history() or [])[1]))
        # ★ 三处注入点统一（2026-09-13）：createplan(goal) / updateplan revise(reason) /
        #   入夜(固定 query) 共用一个 _unified_inject，块结构统一为
        #   site header → ────────── → EXP_BLOCK(经验增量) → SKILL_BLOCK(技能增量)。
        #   header 在 marker **内**（块内自包含）——_compact 的 stale_obs 救援是
        #   split(MARKER)[1]，header 放 marker 前会在观察剥离时丢失。
        #   **增量去重（姿势2）**：以上文已出现的概念 slug/技能签名为排除集，只注入增量；
        #   旧消息一字不改（前缀缓存友好）。全重复 → night_fell_no_new 一行（中性取舍句
        #   别删——query 全防御词，header 是唯一对冲）。
        # **复用 retr 而不是另写一份检索**：口径、渲染、日志、命中计数全都跟 Agent 自己调
        # retrieve 时完全一致 —— 两份实现必然漂移，而这里漂移的后果是"自动检索和手动检索
        # 拿到的东西不一样"，极难察觉。scope=both 与工具默认值一致。
        # 注意 n_calls 会把这次自动检索也计入：日志里 retrieve 次数因此含「立计划带的那次」，
        # 这是有意的 —— 它确实发生了一次检索、确实付了 token。
        def _unified_inject(query: str, site: str) -> str:
            """统一检索注入：返回完整块文本（header + EXP/SKILL 段，含 marker），空串=全跳过。"""
            seen_slugs, seen_sigs = _collect_seen(_cur_history() or [])
            if primary is None and secondary is None and site != "createplan":
                return ""
            header_key = {
                "createplan": "createplan_memory_header",
                "revise": "updateplan_memory_header",
                "night": "night_fell_memory_header",
            }.get(site, "createplan_memory_header")
            # ── EXP 段：经验增量（createplan/revise/夜都查）──
            exp_body = ""
            if primary is not None or secondary is not None:
                try:
                    exp_body = retr.run(query=query, scope="both",
                                        exclude=seen_slugs or None)
                except Exception:  # pylint: disable=broad-except
                    logger.warning("[minecraft注入] %s 经验检索失败（不影响主流程）", site,
                                   exc_info=True)
                    exp_body = ""
            # ── SKILL 段：createplan = base top-N 打底+goal 补充（当天首次，无重复）+ 代码注入；
            #    revise/夜 = 只 query 补充 delta（base 不重注——技能代码当天已全量注入）──
            # ★ 消融 no_exp_skills：SKILL 段整体跳过（_inject_all_skills/_render_skill_delta 内部
            #   也各自短路，这里再拦一道 = 双保险 + 全重复分支不误发 night_fell_no_new）。
            skill_body = ""
            if ablation in ("no_exp_layer", "no_exp_skills"):
                pass
            elif site == "createplan":
                skill_body = _inject_all_skills(query, seen_sigs)
            elif skill_registry is not None and (query or "").strip():
                try:
                    skill_body = _render_skill_delta(query, seen_sigs)
                except Exception:  # pylint: disable=broad-except
                    logger.warning("[minecraft注入] %s 技能补充检索失败（不影响主流程）", site,
                                   exc_info=True)
                    skill_body = ""
            if not exp_body and not skill_body:
                # 全重复（检索本可命中但内容全部已在上文）：夜 site 回一行中性提示（取舍句
                # 必须保留）；cp/revise 不再拼 header——空 header 反而误导 Agent 去上文找不存在的段。
                if site == "night":
                    try:
                        return (INJECT_BLOCK_MARKER + "\n"
                                + REACT.result("night_fell_no_new"))
                    except KeyError:
                        return ""
                return ""
            try:
                header = REACT.result(header_key)
            except KeyError:
                header = ""
            # ★ 外层 INJECT_BLOCK_MARKER 包整个注入块（header + 内层 EXP 段 + 内层 SKILL 段）：
            #   _compact 的 stale_plan/stale_obs 救援都只保留 marker 之后的内容——header 放
            #   marker 内才不被压缩砍掉（stale 后块缺引导语，2026-09-13 自查发现）。Agent 可见
            #   形态：INJECT → header → ────── → 内层 EXP 正文 → 内层 SKILL 正文。
            # ★ 两段内层 marker 的来源不同，别混：
            #   · exp_body = retr.run() 返回值，**自带内层 EXP_BLOCK_MARKER**（检索工具渲染契约，
            #     _collect_seen 扫它取 slug）——直接拼，不再包。
            #   · skill_body = _inject_all_skills/_render_skill_delta → _render_sig_lines 返回值，
            #     **不带 marker**（裸 header+签名行）——要手动包 SKILL_BLOCK_MARKER 前缀
            #     （_collect_seen 扫它取签名）。⚠ 与 RetrieveSkillsTool.run 不同：那个 Agent
            #     工具自带 SKILL_BLOCK_MARKER 包裹，本框架注入路径的 _render_sig_lines 不带。
            #   外层用 INJECT_BLOCK 不复用 EXP_BLOCK——旧法外层 EXP 包整块又与内层 retr.run
            #   自带 EXP 同名 → 双 marker 冗余（2026-09-13 真实图谱 smoke 巡检发现）。
            parts = [INJECT_BLOCK_MARKER + "\n" + header + "\n──────────\n"]
            if exp_body:
                parts.append(exp_body)
            if skill_body:
                parts.append(SKILL_BLOCK_MARKER + "\n" + skill_body)
            return "\n".join(p for p in parts if p.strip())

        # 过夜强制检索的固定 query（A 方案）：框架强制、不依赖 Agent 主动 retrieve。
        # 用固定字符串而非 plan.goal+过夜 —— plan.goal 是白天目标（如「挖铁」），夜里可能不相关；
        # 过夜经验（防御/怪物/避难）是通用的，固定 query 命中更稳。Agent 仍可自己 retrieve 追问细节。
        # ★ 末尾拼生产性词（照明/夜间挖矿/合成整理，2026-09-12）：纯防御 query 会让入口锚定
        #   全落在避难类概念上，注入结果清一色「躲」—— 既推动「入夜=装死」的行为偏置，又经
        #   strength.gain_retrieve（检索命中 +0.1）给夜生存概念日复一日喂强度、富者愈富。
        #   生产性词让「火把照明/隧道防攒怪/夜间整理」这类机制知识也有机会进入口。
        _NIGHT_FALL_QUERY = "过夜 防御 怪物 避难所 夜间生存 照明 夜间挖矿 合成整理"

        def _night_memory_inject(history: list) -> None:
            # ReActAgent 每步工具执行后调（on_step 钩子）：若 env 打了入夜标记，强制检索过夜经验、
            # 拼进**触发入夜那步 write_code 的 toolresult 末尾**（统一注入块，EXP_BLOCK_MARKER 标
            # 边界，与 createplan/updateplan 同款），不再插独立 role=user 消息——那种块插在 tool
            # 消息间会被 LLM 当成额外指令（0827 弃用常驻 user 块的同款理由，2026-09-12 改）。
            # ★ 标记由 env 在昼→夜跳变那步打（env.py 入夜 elif，那步必是 write_code——只有它
            #   推进世界时间）；检索在 react 层做（retr 在本闭包）。跨层经共享 state 传标记，
            #   检索文本经 toolresult 进 LLM 上下文。一天只触发一次：先清标记再检索，防检索
            #   抛异常时重复触发。_compact 压缩旧观察时据 EXP_BLOCK_MARKER 保留这段（见
            #   react_tools._compact 的 stale_obs 分支）。
            # ★ 增量注入（2026-09-13）：_unified_inject 内部按当天上文已注入概念去重——
            #   0912 run 实测入夜注入与当天 cp/revise 内容平均重叠 62%，只注入真正新增的。
            #   seen 集合读 _cur_history()（agent.history，与 on_step 的 history 参数同一列表）。
            if not state.get("night_just_fell"):
                return
            state["night_just_fell"] = False  # 先清标记，防检索抛异常时重复触发
            # 找末条 write_code 的 tool result（入夜那步的落点）：从尾往前找 role=tool 且
            # name=write_code。找不到（理论不会——入夜必发生在某步 write_code 期间，而本钩子
            # 在工具结果写进 history 之后才调）则只好跳过：不回退成 user 消息，否则两种注入
            # 形态并存、口径分裂。找不到时经验这晚不注入，Agent 仍可自己 retrieve。
            idx = next((i for i in range(len(history) - 1, -1, -1)
                        if history[i].get("role") == "tool"
                        and history[i].get("name") == "write_code"), -1)
            if idx == -1:
                logger.warning("[minecraft] 入夜注入：history 里找不到 write_code toolresult，跳过本次注入")
                return
            if primary is None and secondary is None:
                return
            try:
                block = _unified_inject(_NIGHT_FALL_QUERY, "night")
            except Exception:  # pylint: disable=broad-except
                logger.warning("[minecraft] 入夜强制检索失败（不影响 ReAct）", exc_info=True)
                return
            if not block:
                return
            # 与 CreatePlanTool.run 同款拼法：marker 换行隔开，_compact 据标记识别保留。
            history[idx]["content"] = (
                str(history[idx].get("content") or "")
                + "\n" + block
            )

        # ★ 消融（2026-09-18）：flat_retrieval 下架 expand、no_skills 下架 retrieve_skills
        #   （上面 exp/sk_retr 为 None）。工具列表过滤 None 后装配——Agent 的工具面即该 rung
        #   拆除后的真实能力面。
        tools = ToolRegistry([
            CreatePlanTool(plan, memory_hook=lambda goal: _unified_inject(goal, "createplan"),
                           react_state=react_state),
            UpdatePlanTool(plan, memory_hook=lambda reason: _unified_inject(reason, "revise")),
            *[t for t in (retr, exp, sk_retr, wc) if t is not None],
        ])

        # 记忆规模告知：让 Agent 自己判断值不值得 retrieve。
        # 查空是有用信息，但反复空查浪费步数 —— 先告诉它有多少可查。
        def _n_facets(mem) -> int:
            """数这份图谱有多少条 facet。直接读连接 —— 与 orchestrate.consolidate._graph_counts
            同一做法（BrainGraph 没有对外的计数 API，NullGraph 更没有连接）。"""
            conn = getattr(mem, "_conn", None)
            if conn is None:  # NullGraph（frozen 对照组）走这里
                return 0
            try:
                return int(conn.execute("SELECT COUNT(*) FROM facet").fetchone()[0])
            except Exception:  # pylint: disable=broad-except
                return -1

        def _inject_all_skills(goal: str, seen_sigs: set[str] | None = None) -> str:
            """把两图谱全部 verified 技能注入当天执行环境（**代码全量 + 签名块默认 top-N + goal 检索补充 + 去重**）。

            分两层：
            - **代码层（全量）**：js_code 拼进 state["extra_programs"]，每步 write_code 的 programs
              带上，Agent 代码里 `await skillX(bot,...)` 直接可调（与 mineBlock 原语同通道，
              **不进 LLM 上下文**）。全量无 token 成本，且 Agent 任何时候都能用任何 verified 技能。
            - **签名层（默认 top-N 打底 + goal 检索补充 + 去重）**：**返回**签名块字符串（由
              _unified_inject 拼进 createplan 注入块，用 SKILL_BLOCK_MARKER 标边界、_compact 压缩时
              保留）。两份合并去重：
              ① **base** = 全局按成功数+时间排序取前 N（N=yml `skill.signature_topk`，默认 10）——
                 排序键见 `_signature_sort_key`：total_success_count ↓ → success_count ↓ → updated_at ↓
                 → created_at ↓ → slug ↑。成功次数越多越重要：被验证好用的技能稳定常驻打底，
                 不随 goal 轮换稀释曝光。
              ② **supplement** = goal 向量检索命中、但不在 base 里的技能——把当天目标相关、却因
                 成功数不高排不进 base 的补进来（如新技能冷启动：成功数 0 但与 goal 强相关）。
              base 在前、supplement 在后，按 slug 去重合并。总数 = N + 补充数（goal 检索只取 N 个，
              最多补 N 个），不另设上限。空 goal 无 supplement，仅 base（★ 2026-09-01 改造，
              替代旧「仅按 goal 余弦检索 top-8」——既给好技能稳定曝光，又让 goal 相关的冷启动技能有机会露脸）。
              签名块进 LLM 有 token 成本，全量常驻（曾 33 个≈1000 tokens/步）会诱导 LLM 反复调用
              排榜首的 gotoNear（0827 实验 612 次调用里 276 次是 goto，有图谱组 0/5 diamond 退化到
              对照组之下）——故签名层仍以 N 打底、代码层全量。范围外的技能代码已全量注入、可直接
              await；Agent 想用时可调 retrieve_skills 工具查签名（签名块末尾有提示行）。

            ★ `seen_sigs`（2026-09-13 全点去重）：当天上文已注入过的技能签名集合。createplan
              是当天首次注入（一次 createplan 后第二次 createplan 的 goal 变了、但 base top-N 同批），
              base 不参与排除（打底常驻、当天不变），supplement 命中里已在上文的签名跳过——
              避免「一天多次 createplan 重复贴同批 base」。

            ★ **依赖 skill_registry**（必传，非 None）：从 `list_all_verified()` 取全臂 verified 技能、
              从 `default_topk()` 取 N（yml `skill.signature_topk`，唯一真相，无 python 兜底）。
              生产路径 brain 构造 SkillRegistry 时已传 `SkillConfig.load()`，故 default_topk 永不缺 config。
            ★ 跨臂 slug 去重取 first-seen 臂（老库迁移残留兜底），排序用该臂的计数/时间——与
              代码注入的取一份一致。无技能（对照组 NullSkillStore 被过滤）→ list_all_verified 返回 []，
              等价于不注入，基线 programs 照旧。

            ★ 代码层全量是埋点/计数/分流的数据基础（见 plans/skill_aggregation_maintenance.md）：
              包装层把 {slug,ok,err} push 进 bot.openkageSkill，步末 onSkill 事件回传，
              night_cb 据来源分流回填。签名层改「默认+检索」不影响这套——代码仍全量注入。
            ★ 注入的代码**当天所有步共享**：技能函数定义进 programs，次日重新注入覆盖。
            ★ 只有 createplan 走本函数（有代码注入副作用）；revise/夜的技能补充走
              _render_skill_delta（纯渲染、无副作用——这是两函数分开的原因，revise 不该
              覆盖当天已注入的 extra_programs / 重置 skill_sources）。
            """
            # ★ 无 registry（eval 路径：loop._eval_one_group 不传 skill_registry）→ 不注入技能。
            #   eval 只读主脑图谱、且公有脑不存 skill（技能在各臂 skill.db），本就无技能可注入；
            #   registry=None 直接返回空，等价于旧 BrainGraph 路径返回 []。生产训练路径 brain 总传 registry。
            # ★ 消融 no_exp_skills（2026-09-18）：跳过注入——库是 NullSkillStore 恒空，走原路径
            #   也会在 `if not seen` 处返回空，但显式短路让「技能通道已拆」在日志上可见，
            #   且不依赖 NullSkillStore 的 list_all_verified 实现细节。
            if skill_registry is None or ablation in ("no_exp_layer", "no_exp_skills"):
                state["extra_programs"] = ""
                state["skill_sources"] = {}
                return ""
            # ── 代码层：全量（写 state，供埋点/分流）──────────────────────────────
            seen: dict[str, dict] = {}
            # skill_sources：记每个注入技能的归属来源（arm_key），供计数回填分流。
            skill_sources: dict[str, str] = {}
            # ★ 从 SkillRegistry 拿全臂 verified 技能（带 from_user）。生产路径 brain 总传 registry。
            try:
                for sk in skill_registry.list_all_verified():
                    slug = sk["slug"]
                    if slug not in seen:
                        seen[slug] = sk
                        skill_sources[slug] = sk.get("from_user", "?")
            except Exception:  # pylint: disable=broad-except
                logger.warning("[minecraft技能] registry.list_all_verified 失败（不注入）",
                               exc_info=True)
            if not seen:
                state["extra_programs"] = ""
                state["skill_sources"] = {}
                return ""
            # 拼代码：每个技能包一层**调用埋点代理**再注入（逻辑见 _wrap_skill_for_injection）。
            # ★ 逐技能语法熔断（0914 定稿，fail-fast）：入库校验（openkage.memory.tools
            #   ._skill_js_invalid → acorn）拦新坏的，这里拦**存量**坏技能——0914 实测
            #   erin 存量技能含数学负号 U+2212，全量拼进 programs 后 5 臂每步 eval 全抛
            #   Invalid or unexpected token，Agent 的代码本身语法是对的，只会不停换写法
            #   重试白烧 token（一段 963 次报错）。发现任何坏技能**当场 raise**（用户定
            #   0914：出现错误直接熔断结束，不烧 token）：异常穿出 memory_hook 的调用方
            #   （react_tools.CreatePlanTool 已 broad-except 包住？——不，_inject_all_skills
            #   在 _unified_inject 里被 try 吞过一层，故这里 raise 的必须是自己类型的
            #   二次防线——见下 SkillSyntaxError 直接 raise 不被吞的路径说明）。
            #   语义同 0912 的「rollout 失败 = abort 整 run」：坏技能是环境/数据级故障，
            #   继续跑只会让 5 臂每步全部 Invalid token 干烧。人工修库（或下线该技能）
            #   后 --resume-window N 续跑，checkpoint 完整。
            code_parts = []
            for sk in seen.values():
                err = _skill_js_syntax_error(sk.get("js_code", ""))
                if err:
                    raise SkillSyntaxError(
                        f"技能 {sk['slug']} js_code 语法损坏，熔断不注入：{err}。"
                        f"修复方法：改库（REPLACE 数学负号 U+2212 → ASCII '-'）或 demote 该技能后重跑")
                code_parts.append(_wrap_skill_for_injection(sk))
            state["extra_programs"] = "\n".join(code_parts)
            # ★ 来源映射随技能一起进 state，生命周期与 skill_calls 对齐：
            #   env._run_step_body 累加 skill_calls、黎明把两者一起拷进 discoveries 透传 night_cb、
            #   night_cb 据此按来源分流回填。env 每天清零 skill_calls 时同步清零它。
            state["skill_sources"] = skill_sources

            # ── 签名层：默认 top-N 打底（成功数+时间全局排序）+ goal 检索补充 + 去重 ──────
            # ★ 两份都要（2026-09-01 定调）：
            #   ① base = 全局按成功数+时间排序取前 N（默认 10）——被验证好用的技能稳定常驻，
            #      不随 goal 轮换稀释曝光；排序键见模块级 _signature_sort_key。
            #   ② supplement = goal 向量检索命中、但不在 base 里的技能——把当天目标相关、却因
            #      成功数不高排不进 base 的技能补进来（如新技能冷启动：成功数 0 但与 goal 强相关）。
            #   两份按 slug 去重合并（base 优先），base 在前、supplement 在后。总数 = N + 补充数，
            #   不另设上限——goal 检索本就只取 N 个，最多补 N 个。空 goal 无 supplement，仅 base。
            #   代码层仍全量注入，范围外技能 Agent 可调 retrieve_skills 查签名后直接 await。
            topk = skill_registry.default_topk()
            top = sorted(seen.values(), key=_signature_sort_key)[:topk]
            top_slugs = {sk["slug"] for sk in top}
            # goal 检索补充：命中但不在 base 里的，从 seen 取一份（拿全字段、与 base 同源）。
            # ★ 全点去重（2026-09-13）：supplement 里签名已在上文（seen_sigs）的跳过——
            #   一天多次 createplan 时 goal 检索常命中同批技能，重复贴无信息量。
            _seen_sigs = seen_sigs or set()
            if (goal or "").strip():
                try:
                    goal_hits = skill_registry.retrieve_all(goal, limit=topk)
                except Exception:  # pylint: disable=broad-except
                    goal_hits = []
                    logger.warning("[minecraft技能] registry.retrieve_all 失败（goal 补充退化为空）",
                                   exc_info=True)
                n_supplement = 0
                for sk in goal_hits:
                    slug = sk["slug"]
                    if slug in top_slugs or slug not in seen:
                        continue
                    if sk.get("signature") in _seen_sigs:
                        continue
                    top.append(seen[slug])  # 从 seen 取全字段（含排序用的计数/时间）
                    top_slugs.add(slug)
                    n_supplement += 1
                if n_supplement:
                    logger.info("[minecraft技能] goal 检索补充 %d 个技能（不在默认 top-%d 里）",
                                n_supplement, topk)
            n_hidden = len(seen) - len(top)
            logger.info("[minecraft技能] 代码全量注入 %d 个技能（带埋点）；签名块默认 top-%d + goal 补充 = %d（隐藏 %d）",
                        len(seen), topk, len(top), n_hidden)
            return _render_sig_lines(top, n_hidden)

        def _render_sig_lines(skills: list[dict], n_hidden: int = 0) -> str:
            """把技能列表渲染成签名块文本（行格式 `- {签名} —— {意图}（前置）` + more_hint + header）。

            抽出来共三个调用方共用（_inject_all_skills / _render_skill_delta），行格式只有
            一处定义——_collect_seen 的 _SEEN_SIG_RE 与 RetrieveSkillsTool 都按这个格式
            提取签名，改格式必三处同步。
            """
            lines = []
            for sk in skills:
                pre = f"（前置：{sk['preconditions']}）" if sk.get("preconditions") else ""
                lines.append(f"- {sk['signature']} —— {sk['intent']}{pre}")
            # 范围外技能提示行：告诉 Agent 还有更多技能、可用 retrieve_skills 查签名（代码已全量注入）
            if n_hidden > 0:
                try:
                    lines.append(REACT.result("createplan_skills_more_hint", n=n_hidden))
                except KeyError:
                    lines.append(f"（另有 {n_hidden} 个技能未展示；需要时用 retrieve_skills('你的意图') 查询签名，代码已注入可直接 await）")
            if not lines:
                return ""
            try:
                header = REACT.result("createplan_skills_header")
            except KeyError:
                header = "\n【默认按成功次数排序 + 按本次目标检索补充的技能（代码已注入，直接 await 调用）】"
            return header + "\n" + "\n".join(lines)

        def _render_skill_delta(query: str, seen_sigs: set[str]) -> str:
            """revise/夜注入的技能补充（**纯渲染、无副作用**）：按 query 检索、排除已见签名。

            不重注 base top-N（当天 createplan 已打底，重贴同批只是重复），只补 query 命中
            且不在上文的新签名。技能代码当天已全量注入 programs，这里不碰 extra_programs /
            skill_sources（revise 不该覆盖当天已注入技能 + 重置分流计数——这是它与
            _inject_all_skills 分开的原因）。
            """
            if (skill_registry is None or ablation in ("no_exp_layer", "no_exp_skills")
                    or not (query or "").strip()):
                return ""
            try:
                hits = skill_registry.retrieve_all(query, limit=skill_registry.default_topk())
            except Exception:  # pylint: disable=broad-except
                logger.warning("[minecraft技能] registry.retrieve_all 失败（revise/夜补充退化为空）",
                               exc_info=True)
                return ""
            fresh = [h for h in hits if h.get("signature") not in seen_sigs]
            if not fresh:
                return ""
            return _render_sig_lines(fresh)

        # 公共经验只报数量（它靠 retrieve 按意图取）。私有记忆**不报数量、直接全量注入**
        # —— 见下面 own_memory_block。
        n_public = _n_facets(primary) if primary is not None else 0
        mem_note = REACT.result(
            "public_memory_note",
            n_public=n_public if n_public >= 0 else REACT.result("public_memory_unknown"),
        ) if primary is not None else ""

        # ★ **私有图谱的记忆层全量常驻注入**：「我的营地在哪」「昨天有什么没做完」**没人会想到去
        #   retrieve 它** —— 必须常驻，否则 Agent 每天醒来都是个新人：不知道在哪扎过营、
        #   箱子放哪、昨天计划挖矿但天黑没去。**记忆层就是跨天的上下文压缩**：一整天几万 token
        #   的轨迹被反思压成几个专名概念，第二天原样带回来。
        #   规模上完全付得起：一条轨迹几天 × 每天几个专名概念（见 dump_memory_layer 的护栏）。
        #   经验层不走这里 —— 它是通名的、要聚合进主脑，由 Agent 用 retrieve 按当下意图取。
        own_memory_block = ""
        if secondary is not None:
            try:
                # ★ header 留空（2026-08-29）：day_start 模板已有外层【记忆】头，再带【你自己的记忆】
                #   会叠成两层标签。day_resume 裸渲染处由模板自己补【记忆】头（见 react.yaml）。
                #   故 dump 不自带标签，由渲染上下文决定加不加头。
                own_memory_block = secondary.dump_memory_layer(header="") or ""
            except Exception:  # pylint: disable=broad-except
                logger.exception("[minecraft] 私有记忆注入失败，本天将只带公共经验")
        if own_memory_block:
            # 隔一个空行再挂尾注（"\n\n" 显式写在这里，不藏在 yaml 的前导空行里）。
            own_memory_block += "\n\n" + REACT.result("own_memory_tail")

        # 一天的输入：两个 system（在 base_messages 里）+ 当前观察 + 前一夜反思（若有）。
        # last_events 在天亮时被 env 侧拼进了黎明复盘文本，所以「前一夜反思」就在这里面。
        #
        # ★ **文案全在 prompts/react.yaml 的 `user_inputs.{day_start,day_resume}`**，
        #   与 reflect/aggregate 的做法对齐。这里只算值、不写话。
        events = get_events() or []
        # ★ 取回锚点：按专属 key "dawn_reflection" 精确匹配（2026-08-29 改）。旧版注入用
        #   "onChat" + 【第N天黎明复盘】前缀、取回靠 "黎明复盘" in v 字符串匹配；现注入存裸文本、
        #   专属 key，取回改按 key 匹配——既不靠易撞车的字符串、又让 day_goal 不再带前缀标签。
        reflection_txt = "\n".join(v for k, v in events if k == "dawn_reflection")
        # ★ day_goal = 昨夜夜反思产出（【昨日经历】一句事实陈述，经「黎明复盘」通道取回）。
        #   必须在下面 resume 清零 reflection_txt **之前**算：续跑那天也有自己的经历（它的黎明
        #   复盘在更早的轮次已注入），不可被清零后误判成第 1 天。
        #   ★ 2026-09-13 改（用户定）：统一开场后，day_goal 不再兜底 _DAY1_GOAL_OPENING——
        #     第 1 天/记忆层没了时 reflection_txt 为空 → day_goal="" → 模板 when:day_goal 跳过【昨日经历】
        #     块（第 1 天本就无昨日，不注入）。_DAY1_GOAL_OPENING 退化为**仅** _make_next_day_goal
        #     返空/异常时的兜底（保 night_cb 不返空），不再进 day_start。
        #   ★ context_mgmt 控注入的断源**不在 react_day**（本函数各 rung 同链路、无 context_mgmt/no_graph
        #     分支——测试 test_react_day_no_graph_param_removed 守着这个契约），而在**调用方 rollout**
        #     的 night_cb：记忆层没了（−M/no_graph）不调 _make_next_day_goal（reflection="" → env
        #     `if reflection:` 不注入 dawn_reflection → 这里取回恒空）、不搬 prev_day_goals（续跑
        #     恢复也置空）。故记忆层没了时 day_goal/prev_goal 都空 → day_start 两块 when 全跳过，
        #     渲染与第 1 天一致；开场/观察逐字相同、记忆块随图谱口径。差异收敛为 context_mgmt
        #     =记忆层连带这一个变量（2026-09-24 定）。
        #   ★ 2026-09-11 定→2026-09-24 终局：对照组（no_graph）不再跑夜反思（记忆层连带，
        #     见 ⑦）；0911-0923 的「归还对照组」口径作废。0909-0910 的 no_graph 清空分支
        #     本就删了——今日语义下取回恒空，无需分支。
        #   ★ 2026-09-09 改②：night_reflection 措辞改为只产出事实、不替 Agent 定目标（见 react.yaml
        #     night_reflection.system）。day_goal 这个内部 key 名保留（改会牵动多处），但内容已是
        #     「昨日经历」而非「今日目标」——Agent 醒来后自己 createplan 定目标，不再被 LLM 替定。
        day_goal = reflection_txt
        # ★ prev_goal = 昨日 createplan 立的真实 goal，渲染成换行无编号文本。
        #   经 react_state 跨天带过：正常跨天由 night_cb 末尾把当天 react_state["last_goals"] 搬进
        #   prev_day_goals；续跑时从 checkpoint 的 last_goal（渲染好的换行 str）恢复成单元素 list
        #   （见 rollout 闭包 react_state 初始化）。一天多次 createplan 全部进入次日【昨日目标】
        #   （2026-09-10 改，与【昨日经历】读整天 history 同口径），换行无编号、相同 goal 去重保序。
        #   第 1 天 → 空列表 → 渲染空串 → day_start【昨日目标】块 when:prev_goal 跳过。
        #   记忆层没了（−M/no_graph）时 prev_day_goals 在调用方已恒空（见 day_goal 注释 0924 条），
        #   同样跳过。
        _prev_goals = react_state.get("prev_day_goals") if react_state else None
        prev_goal = "\n".join(g for g in (_prev_goals or []) if g).strip()
        # ★ 2026-09-13 改（用户定）：删掉 day_instr 双版本——指令已并入 day_start 统一开场
        #   （「请先用 createplan 立本轮计划…」），模板不再有 {day_instr} 块，py 侧不再算/传它。
        # 「昨夜复盘」与「上一步执行反馈」原本是 if/elif **互斥**的，而 render_blocks 只有
        # `when` 没有 else —— 故互斥在这里算完，给模板两个已经互斥的值。
        # resume 时不发复盘（同一天续跑，昨夜那段已经在上一轮的上下文里出现过了）。
        if resume:
            reflection_txt = ""
        # events_txt = 「上一步执行反馈」块内容。新一天恒空（根治跨天 last_events 残留污染），
        # 续跑保留当天当前步反馈。逻辑与边界见模块级 `_day_events_txt` 的 docstring。
        events_txt = _day_events_txt(resume, reflection_txt, events)
        # day_start 把私有记忆 + 公共经验数量合成一块【记忆】（day_resume 仍各自单独用，
        # 故 own_memory_block / mem_note 保留原样传，多传无害）。
        memory_block = "\n\n".join(p for p in (own_memory_block, mem_note) if p)
        user_input = REACT.render_user_input(
            "day_resume" if resume else "day_start",
            {
                "day_idx": day_idx,
                # day_start 专用：昨日目标 + 当天经历 + 合并后的记忆块。指令已并入统一开场，
                # 不再有 day_instr（2026-09-13 改）。
                "prev_goal": prev_goal,
                "day_goal": day_goal,
                "memory_block": memory_block,
                # 私有记忆在模板里排在**公共经验数量之前**：它是「我是谁、我在哪、我做到
                # 哪了」，是读后面一切（计划、观察）的基准。resume 时同样要带 —— 续跑也是
                # 新一轮 ReAct、上下文同样是重建的。
                "own_memory_block": own_memory_block,
                "mem_note": mem_note,
                "reflection_txt": reflection_txt,
                "events_txt": events_txt,
                "obs": render_obs(),
                # 观察块的边界标记：与 write_code 返回的观察同款 OBS_SEPARATOR，让
                # CompactingContextBuilder 能把黎明的旧观察纳入「保留最近 3 份」滑动窗口砍掉。
                # day_start/day_resume 的观察都放在指令段**之后**（末尾块），压缩时 partition(SEP)[0]
                # 留下目标/记忆/反馈/指令、只砍末尾观察段。
                "OBS_SEPARATOR": OBS_SEPARATOR,
                # 只有 day_resume 模板用它（把已有计划带回去，明确「这一天没结束」——
                # 否则它会以为自己已经完成、再次立刻停下）。day_start 不引用，
                # render_blocks 只替换模板里出现的占位符，多传无害。
                "plan_render": plan.render(),
            },
        )

        # CompactingContextBuilder：送给 LLM 前只保留最近 3 份完整观察（旧的砍掉观察块、
        # 留执行反馈），但 history 与夜反思读的 day_steps 都不受影响。见其 docstring。
        # ReAct 输入留痕：挂在它的 on_build 上 —— 那是**压缩之后、请求之前**的最后一站，
        # 存下来的正是真正发出去的东西（顺序由基类 `build_messages = _emit(_assemble(...))`
        # 保证，见 openkage/agent_core/context/simple.py 那处的 ⚠️）。
        #
        # ★ **一天一个文件，每次覆盖写**（tag 里带 day）：ReAct 的一天是一段**连续对话**，
        #   history 只增不减 —— 后一次调用的 messages 天然包含前一次的全部内容。按 LLM 调用
        #   次数一份份存会有大量冗余，且拆开后反而看不到一天的全貌。
        #   `n_llm_calls` 由 ContextDumper 自己记（覆盖写会丢掉"这天调了多少次"）。
        #
        # ⚠️ `tool_defs_provider` 传的是**可调用**而不是 `tools.get_definitions()` 的值：
        #    工具 schema 里的 `examples`（已用过的 kind/relation）是建工具时按库里现状算的，
        #    传值会把某一刻的快照钉死 —— 而"这次模型看到的可选项"正是要留的信息。
        dumper = ContextDumper(
            context_dir, f"{traj_tag}_day{day_idx:02d}",
            kind="react_day", model_name=model_name,
            tool_defs_provider=tools.get_definitions,
            extra={"traj": traj_tag, "day": day_idx},
        ) if context_dir is not None else None

        ctx_builder = CompactingContextBuilder(
            _react_day_system_prompt(base_messages), on_build=dumper,
            # 经验段 + 技能签名块已拼进 createplan/revise/入夜的注入块（_unified_inject 拼，
            # EXP_BLOCK_MARKER/SKILL_BLOCK_MARKER 标边界），_compact 压缩旧 createplan 时据标记
            # 保留——不再用独立 role=user 常驻块（那种块插在 tool 消息间会误导 LLM）。
        )
        agent = ReActAgent(
            llm=get_llm(model_name, role="task"),
            # CompactingContextBuilder：送给 LLM 前只保留最近 3 份完整观察（旧的砍掉观察块、
            # 留执行反馈），但 history 与夜反思读的 day_steps 都不受影响。见其 docstring。
            context_builder=ctx_builder,
            tool_registry=tools,
            # max_steps：防死循环兜底，不是动作预算（详见 yml domain_scale.minecraft.react_max_steps）。
            # ReAct 的 step 计数含 createplan/retrieve 这些不推进世界时间的调用，真正出口是天亮
            # （should_stop）。值从 yml 经 _make_react_day 闭包变量透传；缺键/未传 → 999。
            # 即便真撞顶，续跑轮也会灌前轮 history 接着跑（见 async_run 的 initial_history），不丢当天对话。
            max_steps=react_max_steps,
            should_stop=lambda: bool(state.get("day_over") or state.get("run_over")),
            # A 方案：入夜强制检索过夜经验（增量）。env 在昼→夜跳变那步打 night_just_fell 标记，
            # 本钩子每步工具后读标记 → _unified_inject 检索 → 拼进入夜那步 write_code 的
            # toolresult（EXP_BLOCK_MARKER 标边界，与 createplan 同款）。
            on_step=_night_memory_inject,
        )
        # ★ 全点去重的已见集合接线：把 **agent 引用**挂进 _seen_agent，provider/_unified_inject
        #   调用时经 _cur_history() 惰性读 agent.history（当天进行中的对话，add_*_message 原地
        #   append，见 react.py——列表引用全程有效，含当天全部上文；当前 toolresult 还没写、
        #   也不该算——正在生成的内容不参与排除）。
        #   ⚠️ 必须存**引用**而非取值：self.history 在 ReActAgent.__init__ 里不存在（react.py
        #   只在 _run/_async_run 开头赋）——此刻取值只会拿到 None 且永不更新（去重静默失效，
        #   2026-09-13 自查修正）。存引用 + getattr 惰性读：工具只在异步循环里被调，那时属性
        #   必已赋值；ERROR 路径（循环炸了 history 没赋）getattr 兜 None，provider 里 or [] 接住。
        _seen_agent["agent"] = agent
        # ★ 把当天 agent 写进共享容器：night_cb 在 async_run **返回前**就被天亮那一步触发，
        #   那时 result.history 还没产出，只能经 react_state["agent"].history 读当天进行中的对话
        #   （见 react.py _async_run 里 self.history = history 的注释）。必须在 async_run 前设，
        #   且每天重建 agent 后重设——续跑会新建 agent，旧引用要被覆盖。
        if react_state is not None:
            react_state["agent"] = agent
        # ★ 续跑时灌入前一轮 history（2026-09-06）：prev_history 来自上一轮 result.history
        #   （day_state 跨轮存）。灌入后本轮 user_input(day_resume) 接在末尾，形成「接着上下文
        #   继续追问」——续跑轮不再丢失当天前半段对话。新一天 prev_history=None，与跨天清空一致。
        #   ContextDumper 一天一个文件覆盖写：灌入后存下的就是含前轮的完整当天对话（留痕=行为一致）。
        result = await agent.async_run(user_input, initial_history=prev_history)
        # ★ reasoning 观测落盘（2026-09-16）：跑完一轮落一个 round，续跑按 r0/r1/r2 追加进同一
        #   <traj_tag>_day{NN}_reasoning.json（dump_reasonings 的 round_key 机制）。只读不回灌上下文。
        #   tag 与上面 ContextDumper 同前缀；context_dir=None（留痕关）时跳过。
        if context_dir is not None:
            _reasonings = (result.metadata or {}).get("reasonings") or []
            dump_reasonings(
                context_dir, f"{traj_tag}_day{day_idx:02d}", _reasonings,
                model_name=model_name, round_key=f"r{resume_idx}",
                extra={"traj": traj_tag, "day": day_idx},
            )
        # ★ 存回本轮 history 供下一轮续跑灌入（天没亮还会再续跑时用）。天亮了（进新一天）会被
        #   上面 `day_state["prev_history"] = None` 清掉，不会跨天泄漏。
        day_state["prev_history"] = list(result.history or [])
        # ★ 报的是**消息条数**而非 ReAct 步数（history 里每步会有 assistant + 每个工具一条 tool，
        #   故条数约等于步数×2）。真要看步数上限余量，看 finish_reason 是否 max_steps。
        reason = getattr(result.finish_reason, "value", result.finish_reason)
        # ★ 消融（2026-09-18）：flat_retrieval 下架 expand、no_skills 下架 retrieve_skills——
        #   计数器为 None 时记 0（"该通道本 run 不存在"，与"调了 0 次"在日志上同形，
        #   工具集差异由 run 目录的消融段区分，不靠这里区分）。
        _exp_calls = exp.n_calls if exp is not None else 0
        _exp_hits = exp.n_hits if exp is not None else 0
        logger.info(
            "[minecraft] 第%d天%s ReAct 结束：%s | write_code=%d 次 retrieve=%d 次(命中%d) "
            "expand=%d 次(命中%d) | 计划更新=%d 次 | history=%d 条消息 | 压缩：旧观察%d份 历史计划%d份",
            day_idx, "（续跑）" if resume else "", reason,
            wc.n_calls, retr.n_calls, retr.n_hits, _exp_calls, _exp_hits,
            len([s for s in plan.steps if s["status"] != "pending"]),
            len(result.history or []), ctx_builder.n_trimmed,
            ctx_builder.n_plan_trimmed,
        )
        # 返回结束原因给外层天循环：它据此区分「天亮了（下一天）」与「LLM 自己停了（续跑同一天）」。
        #
        # ★ 累加整轮代价（见本函数 docstring 的 `cost`）：**必须在这里累加，不能在轮末读计数器**
        #   —— 工具与 dumper 都是每天（每次续跑也算一次）新建的，读终值只会拿到最后那一天的。
        #   `n_llm_calls` 由 ContextDumper 记；留痕关闭（context_dir=None）时 dumper 是 None，
        #   那一项就加不上 —— 故用 `-1` 显式标记"没在统计"，不要让它静默停在 0
        #   （0 会被读成"一次 LLM 都没调"，而真相是"没人数"）。
        if cost is not None:
            cost["n_react_days"] = cost.get("n_react_days", 0) + 1
            cost["n_write_code"] = cost.get("n_write_code", 0) + wc.n_calls
            cost["n_retrieve"] = cost.get("n_retrieve", 0) + retr.n_calls
            cost["n_retrieve_hits"] = cost.get("n_retrieve_hits", 0) + retr.n_hits
            cost["n_expand"] = cost.get("n_expand", 0) + _exp_calls
            cost["n_expand_hits"] = cost.get("n_expand_hits", 0) + _exp_hits
            # ReAct 轮数的代理量：history 里 assistant 消息数 = LLM 决策轮数
            # （每轮一条 assistant + 每个工具一条 tool，见 ReActAgent._run）。
            cost["n_react_turns"] = cost.get("n_react_turns", 0) + len(
                [m for m in (result.history or []) if m.get("role") == "assistant"])
            if dumper is not None:
                cost["n_llm_calls"] = cost.get("n_llm_calls", 0) + dumper.n_calls
            else:
                cost["n_llm_calls"] = -1
        return reason

    return react_day


def _react_day_system_prompt(base_messages: list[dict]) -> str:
    """把两条 system（角色人设 + 域规则）合成 ReAct 的一条 system prompt。

    为什么要合：CompactingContextBuilder 只接一个 system 字符串，而原结构是两条 system 消息
    （角色人设 + 域规则+primitives）。内容一字不改地拼起来，语义等价。

    ★ **这里不自己拼任何 prompt 正文**：规则全在 `prompts/react.yaml` 的 `system` 节，
    避免 system prompt 有第二个出处 —— 一旦两处都对 LLM 可见却互相矛盾，模型会读到冲突的
    规则，且改 prompt 要翻 py、读 yaml 又看不到真正送出去的全文。
    **以后加规则一律改那份 yaml**，别再往这里塞字符串。
    """
    sys_parts = [m.get("content", "") for m in base_messages if m.get("role") == "system"]
    return "\n\n".join(p for p in sys_parts if p)


# ── 反思输入重建：思考链 + 关键信号观察 ──────────────────────────────
# 旧版 _day_trajectory_text（env.py:2690）每步塞完整观察（3×3 网格 + 视野 9 扇区 + 库存装备
# 进度箱子），实测 alice 第1天反思 user_input 12.3 万字符、每步 2147 字符——网格视野对归纳
# 经验几乎无用，反而淹没反思注意力（反思只跑 5 个 LLM 调用就写完）。同时当天 ReAct 对话
# （agent.history）里有反思完全看不到的、真正能变经验的东西：Agent 的决策思考（"我在 y63
# 地下卡住，最简单方案垂直往上挖通"这类推理）与计划结构（createplan goal/steps）。
#
# 本函数把两份数据按 write_code 出现顺序 zip 成新 trajectory_text：
#   - 思考 + 计划：从 history 取（新增，核心盲区）
#   - 关键信号观察（时间/位置/生命/饥饿/XP/装备/进度/科技）+ 代码 + 反馈 + 死亡 + 成就：
#     从 env 侧 day_steps 取（env 权威；且每步都有反馈，避开 history 末步无配对 tool result
#     的时序坑——night_cb 在天亮那步 write_code 执行中途触发，history 末尾 tool_call 还没配
#     tool result）
# 关键事实见 plans/purring-splashing-bentley.md：write_code 顺序对齐（已实测 56=56）、
# day_steps 每天（非每轮）清空故当天累计、history 含续跑前半段（initial_history 灌入）。
#
# ★ 关键信号取舍（2026-09-09 用户定）：**不含「附近生物/威胁」**——附近生物每步都变、大部分
#   是被动生物，对反思归纳是扰乱信号而非事实锚。保留的是 Agent 状态的硬信号：生命骤降=被打、
#   饥饿掉=耗竭、XP 涨=挖矿/合成、装备变化=换工具、进度变化=达成里程碑、科技=工具材质档。
#   这些才是反思能沉淀 pitfall（怎么死的、什么时候状态崩了）与进展趋势的依据。


def _extract_key_signals(obs_text: str) -> str:
    """从一份完整观察文本里提取反思复盘所需的关键信号，丢弃网格/视野/附近生物/库存等细节。

    保留：时间阶段+刻、位置 xyz、生命/饥饿/XP（同行）、装备六槽、进度五树前沿、科技档位。
    丢弃：光照、附近生物（扰乱信号）、附近方块种类、库存、箱子、3×3 网格、视野扇区。
    """
    if not obs_text:
        return ""
    sigs = []
    # 时间：到括号止（不吞后面的光照字段）——如 "sunrise (219/24000，距日落 11781 刻)"
    t = re.search(r"时间：(\S+ \(\d+/\d+，[^)]+\))", obs_text)
    if t:
        sigs.append(f"时间:{t.group(1)}")
    pos = re.search(r"位置：([^\n]+)", obs_text)
    if pos:
        sigs.append(f"位置:{pos.group(1).strip()}")
    # 生命/饥饿/XP 同行："生命：20/20  饥饿：20/20(饱和5)  XP：Lv.0(0pts)"
    st = re.search(r"生命：(\S+)\s+饥饿：(\S+)\s+XP：(\S+)", obs_text)
    if st:
        sigs.append(f"生命:{st.group(1)} 饥饿:{st.group(2)} XP:{st.group(3)}")
    eq = re.search(r"装备：([^\n]+)", obs_text)
    if eq:
        sigs.append(f"装备:{eq.group(1).strip()}")
    prog = re.search(r"(?<=\n)进度：([^\n]+)", obs_text)
    if prog:
        sigs.append(f"进度:{prog.group(1).strip()}")
    tech = re.search(r"(?<=\n)科技：(\S+)", obs_text)
    if tech:
        sigs.append(f"科技:{tech.group(1)}")
    return "  ".join(sigs)


def _parse_history_for_reflection(history: list) -> tuple[list[str], str, list[str]]:
    """从当天 ReAct history 提取反思需要的思考链与计划。

    history 结构：[system, user, assistant{content:str, tool_calls:[...]}, tool, ...]。
    遍历每个 assistant 消息，收集：
      - 每个 write_code 调用对应的思考文本（assistant.content，按 write_code 出现顺序）；
      - 首条 createplan 的原始 arguments（goal+steps）；
      - 所有 updateplan 的原始 arguments（状态机，精简展示）。

    返回 (write_code_thinks, createplan_args, updateplan_args_list)。
    write_code_thinks[k] 与 day_steps[k] 一一对应（day_steps 只在 write_code 执行时 append，
    createplan/updateplan/retrieve/expand 不进 day_steps）。
    """
    wc_thinks: list[str] = []
    createplan_args = ""
    updateplan_args: list[str] = []
    for m in history or []:
        if m.get("role") != "assistant":
            continue
        think = (m.get("content") or "").strip()
        # tool_calls 可能是 OpenAI 风格（list[{id,function:{name,arguments}}]）
        tcs = m.get("tool_calls") or []
        for tc in tcs:
            fn = tc.get("function") or {}
            name = fn.get("name")
            args = fn.get("arguments", "")
            if name == "write_code":
                wc_thinks.append(think)
            elif name == "createplan" and not createplan_args:
                createplan_args = args
            elif name == "updateplan":
                updateplan_args.append(args)
    return wc_thinks, createplan_args, updateplan_args


def _render_plan_header(createplan_args: str, updateplan_args: list[str]) -> str:
    """把 createplan + updateplan 渲染成反思 trajectory 开头的计划块。

    createplan 的 arguments 是 JSON 字符串（{goal, steps:[...]}），解析失败就原样展示。
    updateplan 是状态机（{step_no, status}），信息量低，精简成一行汇总而非逐条罗列。
    """
    lines = []
    if createplan_args:
        try:
            plan = json.loads(createplan_args)
            goal = plan.get("goal", "")
            steps = plan.get("steps", []) or []
            lines.append(f"[今日计划] {goal}")
            if steps:
                lines.append("  步骤：" + " → ".join(steps))
        except Exception:  # pylint: disable=broad-except
            lines.append(f"[今日计划] {createplan_args}")
    if updateplan_args:
        # 汇总各步终态：取每个 step_no 最后一次 status（状态机会反复 running→success/fail）
        final_status: dict[int, str] = {}
        for ua in updateplan_args:
            try:
                u = json.loads(ua)
                sn = u.get("step_no")
                st = u.get("status")
                if sn is not None and st:
                    final_status[sn] = st
            except Exception:  # pylint: disable=broad-except
                pass
        if final_status:
            parts = [f"步骤{sn}→{st}" for sn, st in sorted(final_status.items())]
            lines.append("[计划执行] " + "  ".join(parts))
    return "\n".join(lines)


def _build_reflection_trajectory(history: list, day_steps: list[dict], day_idx: int) -> str:
    """重建反思用的当日轨迹文本：思考链 + 关键信号观察 + 代码 + 反馈 + 死亡 + 成就。

    替代旧 `_day_trajectory_text`（env.py:2690，每步完整观察）的反思用途。
    history 提供 Agent 的思考与计划（反思原本看不到的决策意图），day_steps 提供环境事实
    锚点（关键信号观察、死亡、成就）与代码反馈（env 权威、每步齐全）。

    两者按 write_code 出现顺序对齐：history 第 k 个 write_code 思考 ↔ day_steps[k]。
    思考按索引取，越界（history 比 day_steps 短，如续跑或末步时序）则该步无思考、其余
    仍渲染——不丢环境事实，不静默错位（错位会让反思把第 k 步的思考配上第 k+1 步的反馈，
    比缺一条更糟）。
    """
    wc_thinks, createplan_args, updateplan_args = _parse_history_for_reflection(history)
    lines = [f"第 {day_idx} 天的探索轨迹："]

    plan_header = _render_plan_header(createplan_args, updateplan_args)
    if plan_header:
        lines.append(plan_header)

    n_thinks = len(wc_thinks)
    # 遍历 day_steps（环境事实锚点的权威来源），思考按索引取——越界则该步无思考。
    # 这样 history 为空（退化天）时 day_steps 仍渲染（只是缺思考链），不丢环境事实。
    for k in range(len(day_steps)):
        rec = day_steps[k]
        lines.append(f"── 第{rec.get('step', k)}步 ──")
        # 关键信号观察（降级自完整观察，丢弃网格视野库存装备）
        sig = _extract_key_signals(rec.get("obs", ""))
        if sig:
            lines.append(f"  [{sig}]")
        # 思考：history 的决策意图（核心新增）。越界（history 比 day_steps 短）则该步无思考。
        think = wc_thinks[k] if k < n_thinks else ""
        if think:
            lines.append(f"  思考：{think}")
        # 代码 + 反馈：env 权威，每步齐全（避开 history 末步无 tool result 的时序坑）
        code = (rec.get("code") or "").strip()
        if code:
            lines.append(f"  代码：{code}")
        events = rec.get("events") or []
        if events:
            # events 是 [(kind, text)]：kind=onChat/onLog/onError（反馈来源）对反思 Agent 是噪音，
            # 反馈本就是文本，按序拼文本即可，不把来源标签塞进去。
            ev_txt = "; ".join(str(v) for _, v in events)
            lines.append(f"  反馈：{ev_txt}")
        # 死亡/成就：服务器权威，单独成行（反思沉淀 pitfall 的硬事实源）
        for d in rec.get("deaths") or []:
            lines.append("  [死亡] " + _death_line(d))
        for a in rec.get("advancements") or []:
            lines.append(f"  [成就] {a}")

    return "\n".join(lines)


async def _make_next_day_goal(agent, day_idx: int, discoveries: dict, model_name: str | None) -> str:
    """夜末「次日目标」镜像调用：把**第 N 天的 ReAct 对话** + 一句新 user 消息发给
    **同一模型**，产出第 N+1 天（次日）的探索目标。

    与旧的 _make_night_reflection 的区别：旧版用 `disc + day_text`（env 侧轨迹摘要）做一次
    无上下文轻量调用；新版把当天完整 ReAct 对话（history）作为前缀续上，让模型「接着昨天的话
    往下说」定下今天的目标——这正是用户要的「昨夜复盘改逻辑」。

    ★ 产出复用现有「黎明复盘」通道承载（env.py 注入 last_events + checkpoint 的 last_reflection），
      次日 react_day 经 get_events() 匹配「黎明复盘」取回、注入 day_start 的【昨日复盘与今日目标】块。
      故这里返回的文本同时是「夜反思产出（复盘+目标）」与「黎明复盘」——同一文本、两个身份。

    `agent`：当天进行中的 ReActAgent（由 react_day 在 async_run 前写进 react_state）。它的
    `agent.history` 是 `_async_run` 里**原地变更**的当天完整对话引用。history **原样保留**、
    只补齐末尾不配对的 tool_call（见 _patch_history_for_mirror）：night_cb 在天亮那步 write_code
    执行中途触发，此刻末尾 tool_call 还没 add_tool_result，原样发会 400 tool_call_sequence_broken；
    补一条占位 tool result（「天亮中止」语义，还原这步本该有的反馈）即可配对，不必丢任何上下文。

    ★ **必须拿到非空目标**（2026-08-28 用户定）：day_goal 为空会让 day_start 退化成「无目标的
      场景B」。故返空/异常都**重试**（_NIGHT_GOAL_RETRIES 次），不轻言放弃。真到重试用尽（极小概率，
      如网关持续不可用），最后用固定开场 `_DAY1_GOAL_OPENING` 兜底——它是一段合法目标，不会让
      day_start 退化、不会让 night_cb 返空崩下游（last_events/last_reflection 都假设非空）。
      agent=None / history 为空（零轮 ReAct 的退化天，正常不会发生）同理直接用兜底，不再发空 history
      的无谓调用。
    """
    history = list(getattr(agent, "history", None) or [])
    if not history:
        # 拿不到当天 history：正常不会发生（一天至少 createplan + 若干 write_code）。
        # 不发空 history 的无谓调用，直接兜底。
        logger.warning("[minecraft] 第%d夜「次日目标」拿不到当天 history（agent=%s），用固定开场兜底",
                       day_idx, agent)
        return _DAY1_GOAL_OPENING
    # ★ history 原样保留、只补齐末尾不配对的 tool_call（2026-08-29 修，用户纠正后定）：night_cb 在
    #   天亮那一步 write_code 工具执行中途触发，此刻 react.py 已 add_assistant_message（末尾带
    #   write_code 的 tool_call）但还没 add_tool_result（要等工具 return 后才加），故读到的 history
    #   末尾有一个 tool_call 而无对应 tool 结果 → 原样发触发网关 400 tool_call_sequence_broken。
    #   末尾那步是被天亮中止的（世界已 freeze），其 tool result 本就该是「天亮中止」——补一条占位
    #   role=tool 是还原这步本该有的反馈语义，不是 hack。详见 _patch_history_for_mirror。
    mirrored = _patch_history_for_mirror(history)
    messages = (
        [{"role": "system", "content": REACT.night_system}]
        + mirrored
        + [{"role": "user", "content": REACT.night_user(day_idx)}]
    )
    # ★ 失败判据**三种都要重试**（2026-08-29 修）：
    #   ① 返空（content 空/纯空白）——常见根因 night_note_max_tokens 太小被 reasoning token 吃光
    #     （见 react.py LENGTH_TRUNCATED 同类陷阱），抬预算 + 重试才能稳定拿到非空目标；
    #   ② finish_reason == "error" ——网关/SDK 把请求期错误（400 tool_call_sequence_broken、超
    #     上下文窗等不可重试错误）收敛成 LLMResponse(finish_reason="error", content="Error:…")，
    #     不抛异常，必须显式判 finish_reason 才能识别；
    #   ③ content 以 "Error:" 开头 ——同一错误的另一道防线（LLM 层把异常拼进 content）。
    #   旧代码 `if text: return text` 会把报错串当成合法目标返回 → 报错串次日被注入【今天的
    #   目标】、还混进观察反馈，这就是 0828 实验败北的真凶。命中任一即重试，间隔指数退避。
    for attempt in range(1, _NIGHT_GOAL_RETRIES + 1):
        try:
            resp = await get_llm(model_name, role="task").async_think(
                messages=messages,
                config={"max_tokens": LLMConfig.load().night_note_max_tokens},
            )
            text = (resp.content or "").strip()
            finish = getattr(resp, "finish_reason", "?")
            is_error_str = text.lower().startswith("error:") if text else False
            if text and finish != "error" and not is_error_str:
                return text
            logger.warning("[minecraft] 第%d夜「次日目标」第%d次失败（finish=%s 空=%s 报错串=%s），重试",
                           day_idx, attempt, finish, not text, is_error_str)
        except Exception:  # pylint: disable=broad-except
            logger.warning("[minecraft] 第%d夜「次日目标」镜像生成失败（第%d次）", day_idx, attempt)
        if attempt < _NIGHT_GOAL_RETRIES:
            await asyncio.sleep(CALL_DELAY_SECONDS * attempt)
    logger.error("[minecraft] 第%d夜「次日目标」重试 %d 次仍未拿到非空目标，用固定开场兜底",
                 day_idx, _NIGHT_GOAL_RETRIES)
    return _DAY1_GOAL_OPENING


def _patch_history_for_mirror(history: list) -> list:
    """把当天 ReAct history **原样**复制一份，补齐末尾可能不配对的 tool_call，供「次日目标」
    镜像调用续上下文用。

    ★ **为什么要补、不剥**（2026-08-29，用户纠正后定）：night_cb 在天亮那一步 write_code 工具
      **执行中途**触发（env.py `_run_step_body` 的 dawn 分支调 night_cb，此刻仍在 react.py:290
      的 `async_execute` 里）。那时 react.py 已 `add_assistant_message`（末尾 assistant 带 write_code
      的 tool_call）但**还没** `add_tool_result`（要等工具执行返回、`_run_step_body` return 之后才
      在 react.py:299 加）。所以 night_cb 读到的 history **末尾必有一个 tool_call 而无对应 tool 结果**，
      原样发给服务端 → `400 tool_call_sequence_broken`。
    ★ 末尾那步 write_code 是被天亮中止的（世界已 /tick freeze）——它的 tool result 本来就该是
      「天亮中止、未完整执行」之类，而不是正常执行反馈。故**补一条占位 tool result 不是 hack、
      是还原这步本该有的反馈语义**。其余历史（中间步的 tool_call↔tool result）都已配对，不动。

    做法：深拷贝 history（不污染 agent 进行中的 history 引用）；扫描每条 assistant 的 tool_calls，
    收集已有 role=tool 结果的 tool_call_id；对末尾 assistant 里**缺结果的 tool_call**补一条占位
    role=tool（content="（天亮中止：本步未完整执行，世界已暂停进入复盘）"）。中间步不会缺结果
    （night_cb 只在最后一步执行中触发），故只补末尾，但实现上对任意缺失都补，更稳。

    返回可直接拼进 messages 的 list（含原始 system/user/assistant/tool 全角色，顺序不变）。
    """
    import copy
    out = [copy.deepcopy(m) for m in history]
    # 已有 tool 结果的 tool_call_id 集合
    answered = {
        m.get("tool_call_id") for m in out
        if m.get("role") == "tool" and m.get("tool_call_id")
    }
    # 找最后一条带 tool_calls 的 assistant（night_cb 时机下只有末尾会缺结果）
    last_assistant_with_tc = None
    for m in out:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            last_assistant_with_tc = m
    if last_assistant_with_tc is not None:
        for tc in last_assistant_with_tc["tool_calls"]:
            tcid = tc.get("id")
            if tcid and tcid not in answered:
                # 占位 tool result：还原「天亮中止」这步本该有的反馈语义
                out.append({
                    "role": "tool",
                    "tool_call_id": tcid,
                    "name": tc.get("function", {}).get("name", "write_code"),
                    "content": "（跨过午夜中止：本步未完整执行，世界已暂停进入复盘。）",
                })
                answered.add(tcid)
    return out


def _render_skill_diagnosis(skill_store, disc: dict | None) -> str:
    """渲染阶段2（技能维护）的诊断块：各技能累计成败 + 本次窗口失败原因。

    ★ **两阶段反思**（2026-08-29）：阶段1（经验/记忆）和阶段2（技能维护）是两个独立 LLM 会话。
      本函数只服务阶段2——把「该维护什么」的事实拼成文本，喂给技能维护 Agent 自己 update/demote。

    数据源（两份，符合用户定调「全员使用次数 + 本次失败原因」）：
    - **累计计数**：从本臂 `skill_store.all_skills_brief()` 读 `success_count`/`fail_count`（跨天
      全员回填到作者库 = 全员使用全貌）+ `total_success/total_fail`（历史归档）+ `revise_count`。
    - **本次失败原因**：从 `disc["skill_calls"][slug]["errs"]` 读（env 每天黎明清零 = 本次反思
      窗口的失败样本，去重每技能≤10）。

    渲染规则：
    - 列本臂**全部**技能（含没被调过的——它们累计计数为 0，Agent 要看到全库）。
    - 按累计 fail_count 倒序：坏技能排前面。全失败（累计 ok=0 且 fail>0）标 ⚠️。
    - 每行：slug | 累计ok/fail | 历史ok/fail | 改过N次 | 本次失败原因(去重≤3条展示)。
    - 无技能（空库/对照组）→ ""（调用方据此设 skill_diagnosis_available=False，when 不渲染）。
    """
    briefs = []
    if skill_store is not None:
        try:
            briefs = skill_store.all_skills_brief()
        except Exception:  # pylint: disable=broad-except
            briefs = []
    if not briefs:
        return ""
    # 本次窗口的失败原因（按 slug 索引）。disc["skill_calls"] 是当夜增量，errs 已去重封顶10。
    cur_errs = {}
    for slug, c in ((disc or {}).get("skill_calls") or {}).items():
        es = [e for e in (c.get("errs") or []) if e]
        if es:
            cur_errs[slug] = es
    lines = ["你的技能库现状（累计=当前代码版本评价；历史=所有版本归档；本次原因=本次反思窗口的失败样本）："]
    # 按累计失败数倒序，坏技能排前面。
    ordered = sorted(briefs, key=lambda b: (-(b.get("fail_count", 0)), b.get("slug", "")))
    for b in ordered:
        slug = b.get("slug", "?")
        ok = int(b.get("success_count", 0) or 0)
        fail = int(b.get("fail_count", 0) or 0)
        tok = int(b.get("total_success_count", 0) or 0)
        tfail = int(b.get("total_fail_count", 0) or 0)
        rev = int(b.get("revise_count", 0) or 0)
        mark = "⚠️ 全失败" if (ok == 0 and fail > 0) else (f"失败{fail}次" if fail > 0 else "稳定")
        errs = cur_errs.get(slug, [])[:3]
        err_str = f"  本次原因：{' / '.join(errs)}" if errs else ""
        ver = "（已下线）" if not b.get("verified", 1) else ""
        lines.append(
            f"  {slug:<32} 累计{ok}成功/{fail}失败  历史{tok}/{tfail}  改{rev}次  ← {mark}{ver}{err_str}")
    return "\n".join(lines) if len(lines) > 1 else ""


def _render_own_skill_names(memory) -> str:
    """阶段1 反思前置的**本臂已用函数名**（2026-09-24 加）。

    ★ 只要函数名、不要签名全文：`create_skill` 撞函数名会被拒（见 skill_store 的防同名门控），
      而"我已经用过哪些名字"这个问题**不需要** intent/前置/参数——那些由 `search_skills`
      按意图查。给全文等于把签名块再复制一份进反思上下文（实测全臂 59~70 条 × ~140 字符）。
    ⚠️ 函数名是**跨臂**命名的真正冲突点（两个臂都写 `mineIron` 会在 programs 里互相覆盖），
      本臂清单只挡住"跟自己撞"那半，跨臂那半仍由 `create_skill` 的拒绝兜住（用户定：只列本臂）。
    """
    store = getattr(memory, "_skill_store", None)
    if store is None:
        return ""
    try:
        rows = store.list_verified_skills()
    except Exception:  # pylint: disable=broad-except
        return ""
    from openkage.memory.skill_store import _js_func_name  # 局部导入：与阶段2 同款，避免模块级环
    names = set()
    for r in rows or []:
        nm = _js_func_name(r.get("js_code") or "")
        if not nm:
            nm = (r.get("signature") or "").split("(")[0].strip()
        if nm:
            names.add(nm)
    return "、".join(sorted(names))


async def _reflect_skill_maintenance(
    secondary, *, day_idx: int, discoveries: dict | None = None,
    arm_role: str | None = None,
) -> None:
    """反思阶段2：技能维护（独立 LLM 会话，2026-08-29）。

    阶段1（`_reflect_exploration`，经验/记忆）跑完后调用本函数。两者**完全独立**：不同 system
    prompt、不同工具集（仅 5 个技能工具）、不同 user_input、不同上下文落盘
    （contexts/..._reflect_skill_maintenance_at<NNN>.json）。

    告知 Agent 各技能「全员累计成功/失败次数 + 本次窗口失败原因」，让它据此 update_skill
    （改代码重置计数 + 归档历史）或 demote_skill（下线坏技能）。每晚必跑（即使无失败也让它检查）。

    ★ 走 `secondary.reflect(..., system_prompt=SKILL_MAINTENANCE.system, vocab="skill_maintenance")`，
      复用通用反思核心，只换 prompt + 工具集 + log_tag。no_graph（NullGraph）→ reflect 是 no-op。
    """
    from openkage.memory.prompt_store import SKILL_MAINTENANCE

    skill_store = getattr(secondary, "_skill_store", None)
    # ★ 空库跳过（2026-08-29）：技能库为空时维护无对象——update/demote/create_skill 三个工具
    #   对空库无可下手处，发起 LLM 调用只是空转烧 token（如某臂第 1 天尚未 create_skill 时）。
    #   这里直接 return、不落盘 context（没有 at001.json 留痕即说明跳过）。对照组（NullSkillStore）
    #   在调用方 `self._no_graph` 门控就已跳过本函数，此处主要挡正常臂的空库夜。
    if skill_store is not None:
        try:
            _empty_lib = not skill_store.all_skills_brief()
        except Exception:  # pylint: disable=broad-except
            _empty_lib = True
        if _empty_lib:
            logger.info("[reflect_skill_maintenance] 第%d天 技能库为空，跳过维护（无对象可动手）",
                        day_idx)
            return

    skill_diag = _render_skill_diagnosis(skill_store, discoveries)
    values = {
        "arm_role": (arm_role or "").strip(),
        "day_idx": day_idx,
        "skill_diagnosis": skill_diag,
        "skill_diagnosis_available": bool(skill_diag),
    }
    user_input = SKILL_MAINTENANCE.render_user_input("minecraft_daily", values)
    await secondary.reflect(
        day_idx, user_input,
        log_tag="reflect_skill_maintenance",
        log_extra=f"第{day_idx}天技能维护",
        system_prompt=SKILL_MAINTENANCE.system,
        vocab="skill_maintenance",
    )


async def _reflect_exploration(
    memory, *, topic_tag: str, objective: str, trajectory_text: str,
    discoveries: dict | None = None, arm_role: str | None = None, qid: str = "?",
    day_idx: int = 0,
) -> None:
    """每天结束时把这一天写进**自己的私有记忆**（ArmGraph）。走 memory.reflect 通用核心。

    ★ **这是"记忆"而不是"经验"**：产出的是专名概念（`day_N`、营地、矿洞）
    + 带出处的经历，供**明天的自己**接着用 —— 上下文每天清空，这份记忆是唯一的跨天通道。
    通名经验（"夜间移动要带火把"）由**聚合阶段**从这些记忆里提炼，不在这里写。

    `day_idx` 必须传，它有**两个**用途：
    ① Agent 要用它命名当天的概念（`day_3`）并接上时间线（`day_2 precedes day_3`）；
    ② 它就是写进 `created_at`/`updated_at` 的值 —— **副脑图谱的时间刻度是"第几天"**
       （2026-08-08 用户定案）。此前这里传的是主脑的 `round_num`（见 openkage.roundcode），
       而 night_cb 每天传的是同一个值 → 同一条轨迹里所有概念的 created_at 全相同，
       "按时间排序/丢最老的"那类逻辑（`_dump_layer` 的护栏、`search_concepts` 不带 query
       时的排序）全部退化成 slug 字典序。现在它真的单调递增。
    """
    # ★ **文案全在 prompts/reflect/reflect.yaml 的 `user_inputs.minecraft_daily`**。
    #   这里只负责**算值**，不拼任何正文 —— 条件分支（第 1 天不发 precedes 指令）由模板的
    #   `when:` 表达。
    disc = discoveries or {}
    # ★ **前置「图谱全景」**（索引形态：概念 + facet kind + 关系，不含正文）。
    #   为什么要前置而不是让它自己 search：反思 prompt 反复要求"别重复建一个近义概念"，
    #   但同一份 ArmGraph 上的多次反思（第 1/2/3 天）是**互不知情的独立会话** —— 昨天建的
    #   `夜间移动风险`，今天完全看不到，只能靠它想起来去 search、且想对查询词。全景把这件事
    #   变成确定的。
    #   ★ **不传 layer = 两层都给**（2026-08-08 用户定案；此前只给经验层）：记忆层同样有去重
    #   需求（"已经记过的地方若有变化就补充进去"），而它此前唯一的支撑是自己 search_concepts ——
    #   那个不带 query 时按 `updated_at DESC, created_at DESC, slug` 排 —— 在同日修好副脑
    #   时间刻度（写天数）之前，这两个值在一条轨迹内是常量，整个排序退化成 slug 字典序
    #   （`day_1/day_10/day_2/…`）。刻度修好后排序能用了，但**全景仍然该给** ——
    #   它让"我记过什么"变成确定的，而不是取决于排序和查询词。
    #   两层比单层实测只多约 35 token（922 vs 391 字符），这个量级不值得留一个盲区。
    #   ⚠️ 护栏**只管记忆层**（`memory_dump_max_concepts`，2026-08-15 解耦）：经验层全量输出，
    #   撞顶时被省略的只会是最老的记忆概念，且会在全景第一行告知 Agent（见 `_dump_layer`）。
    #   此前两层混算同一个额度，经验概念会被记忆概念挤掉 —— 那正是该聚合进主脑的那部分。
    #   ⚠️ 记忆层在**白天 ReAct** 那边也注入过（own_memory_block），但那是**另一个 LLM 会话**、
    #   反思看不到 —— 别把那份当成这里可以省掉的理由（我曾这样误判过）。
    #   为什么是索引形态而不是正文：反思要的是"这个概念我记过了吗、slug 叫什么"；真要改某条
    #   facet 它**必须**先 `get_concept` 读原文（update_facet 整条覆盖），给正文反而诱导它跳过。
    exp_panorama = memory.dump_layer_index()
    # ★ 原「前置已用词表」块（`dump_used_vocab`）已于 2026-09-24 从模板摘除（用户定：删冗余）。
    #   计算一并摘掉，不留死代码；要恢复就把模板那个块加回来 + 恢复这一行。
    #   ⚠️ 摘除的代价记在这里：它是自由命名下**唯一**让反思复用自己 kind/关系名的通道 ——
    #   不给清单，同一个范畴会在第 1 天叫 `where`、第 7 天叫 `location`，「同一概念每种 kind
    #   只有一条」的归并压力失效、关系名的检索证据被摊薄（关系名直接参与打分），跑完也
    #   反推不出词表。剩下的可见性只有全景里每个概念的「已有 facet kind」。
    # ★ 前置「本臂已用技能函数名」（2026-09-24）：阶段1 会建技能，而撞函数名会被拒 —— 先让它
    #   看见自己已经用过哪些名字，省掉一次 search_skills 或一次撞名重试。见
    #   `_render_own_skill_names`（只要函数名、不要签名全文，体积近乎为零）。
    skill_names = _render_own_skill_names(memory)
    # ★ 技能维护已拆成独立阶段2（_reflect_skill_maintenance，2026-08-29）：阶段1专注经验/记忆，
    #   不再带技能诊断块。诊断（累计计数 + 本次失败原因）归阶段2 的 user_input。
    values = {
        "arm_role": arm_role.strip() if arm_role else "",
        "day_idx": day_idx,
        "day_slug": f"day_{day_idx}" if day_idx else "day_?",
        # prev_slug / is_first_day 是一对互斥开关，喂给模板的两个 when 分支
        "prev_slug": f"day_{day_idx - 1}" if day_idx > 1 else "",
        "is_first_day": not (day_idx > 1),
        "objective": objective,
        "has_discoveries": bool(disc),
        "new_items": disc.get("new_items", []),
        "n_new_items": disc.get("n_new_items", 0),
        # 5 棵进度树各自的前沿（累计 advancements_so_far 算，与观察字段同源同口径）——
        # 让反思 Agent 知道今天在每棵树上走到哪，写经验时不只盯着 base 一棵。
        "progress_frontier": progress_frontier(set(disc.get("advancements_so_far") or [])),
        "n_steps": disc.get("n_steps", "?"),
        # 死亡/成就是服务器权威播报，是"今天到底发生了什么"最硬的事实源。
        "deaths_summary": disc.get("deaths_summary", ""),
        "advancements_line": ", ".join(disc.get("advancements_today") or []),
        "trajectory_text": trajectory_text,
        "exp_panorama": exp_panorama,
        "skill_names": skill_names,
    }
    user_input = REFLECT.render_user_input("minecraft_daily", values)
    # `at=day_idx`：副脑的时间刻度是天数（见本函数 docstring 的 ②）。
    await memory.reflect(
        day_idx, user_input,
        log_tag="reflect_exploration", log_extra=f"[{topic_tag}] 第{day_idx}天",
    )
