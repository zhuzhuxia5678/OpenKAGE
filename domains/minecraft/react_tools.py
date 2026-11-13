"""Minecraft 域的 ReAct 工具集：一天一轮 ReAct 的五个工具。

# 五个工具的分工

- `createplan`  —— 开一天之初立计划（goal + steps），每个 step 初始 `pending`；
                   顺带按 goal 自动检索一次记忆（见该类的 memory_hook）
- `updateplan`  —— 改某步状态（running/success/failure）或整体重排剩余步骤
- `retrieve`    —— **自主**检索记忆的**入口**（向量锚定；query 自己写，scope 选公共/自己/两者）
- `expand`      —— 沿记忆图谱的边**遍历**（方向由 Agent 自己选，见该类 docstring）
- `write_code`  —— **唯一让 bot 做出动作**的工具；返回「执行反馈 + 完整新观察」
                   （注意：**不是**唯一推进时间的 —— 时间一直在走，见该类 docstring）

`retrieve`/`expand` 现在是框架层工具（`openkage.memory.tools.RetrieveMemoryTool` /
`ExpandMemoryTool`），本文件只是重新导出 + 在下面 `_make_react_day` 里传 `texts=REACT` 构造它们；
框架层不认识 minecraft，所以这两个类不能硬编码本域的 prompt store，措辞由调用方传入。

# 三条规则

- **只有 write_code 计动作步**。ReAct 的 max_steps 是防死循环的兜底（给 999），
  但真实动作预算独立计数 —— 否则 Agent 反复 retrieve 就能在天亮前耗尽步数。
- **计划块永不压缩**。观察可以只留最近几份（见 render_history_compact），但计划必须始终可见。
- **反思读的不是 ReAct 的 history**，而是 env 侧独立累积的 `day_steps`（env.py:1520）。
  所以压缩上下文**不会**让反思少看到东西 —— 这两套本来就是分离的。

# 提示词都不在本文件里

五个工具的 description、参数说明，以及它们**返回给 LLM** 的文案，全部在
`prompts/react.yaml`，经 `prompt_store.REACT` 读取。本文件只留控制流与状态机
（`_STEP_STATES` / `_STATE_MARK` / `OBS_SEPARATOR` 那些是代码契约，不是措辞）。
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Optional

from openkage.agent_core.context.simple import SimpleContextBuilder
from openkage.agent_core.tool.base import BaseTool
from openkage.memory.tools import (  # noqa: F401  重新导出
    EXP_BLOCK_MARKER,
    SKILL_BLOCK_MARKER,
    ExpandMemoryTool,
    RetrieveMemoryTool,
    RetrieveSkillsTool,
)

from .prompt_store import REACT

# ★ 技能语法熔断异常（0914）：CreatePlanTool 的 memory_hook 包装 broad-except 需放行它
#   （from __init__ import 会循环导入——__init__ 先 import 本模块；异常类只能反向单点定义，
#   放 __init__ 顶部、这里延迟导入取引用即可。实际上无循环：本模块被 __init__ import 时
#   这行还没执行到（模块级 import 在 __init__ 的 import react_tools 之后），但为稳妥仍走
#   函数内延迟导入——见 CreatePlanTool.run 的 except SkillSyntaxError。）
from . import HealFuseError, SkillSyntaxError  # noqa: E402  循环导入安全：类定义在 __init__ 顶部、早于本模块被 import

logger = logging.getLogger(__name__)

#: write_code 的 tool result 里保留**完整观察**的最近几份（只留最近 3 份）。
#: 更早的把观察块砍掉、只留执行反馈那一段。
KEEP_FULL_OBSERVATIONS = 3
#: 观察块在 tool result 里的分隔标记（由 env._run_step_body 拼出，两处必须一致）。
OBS_SEPARATOR = "─" * 40
#: 经验段/技能段的边界标记：**定义在框架层**（openkage/memory/tools.py，retrieve/
#: retrieve_skills 命中时自动包裹），域侧从这里重新导出——注入块拼接（_unified_inject）、
#: _compact 压缩保留、已见集合扫描（_collect_seen）共用同一出处，不存在两份漂移。
# ★ 为什么用显式标记而不是靠 header 文案特判：旧法靠 `_mem_header_marker` 匹配「顺带按你这个目标
#   查了过往经验」那行保留经验段——文案一改就失效（正是当年 world_healthy 字段死一个月的同款病）。
#   显式 marker 是代码契约（像 OBS_SEPARATOR），文案怎么改都不影响保留逻辑。
# ★ 词汇表四件套（2026-09-13 统一 + 外层注入块还债）：
#   INJECT = **外层**注入块边界（_unified_inject 包整个 createplan/revise/入夜 注入块：
#            header + EXP 段 + SKILL 段）——_compact 据 marker 整块保留（stale_obs/stale_plan
#            救援 split(MARKER)[1]，header 在 marker 内才不被砍）。它包的不只是经验，故名
#            INJECT 而非复用 EXP（旧法外层用 EXP_BLOCK 包整块、又与内层 retr.run 自带的
#            EXP_BLOCK 同名 → 双 marker 冗余，Agent 可见文本多一行裸 <<<EXP_BLOCK>>>）。
#   MEM   = 记忆层常驻块（day_start/day_resume 的【记忆】）——只标边界供 _collect_seen 扫描；
#   EXP   = 检索经验段（**内层**：retrieve 命中正文，_unified_inject 拼进 INJECT 块内 +
#            Agent retrieve 结果按同一渲染契约）；
#   SKILL = 技能签名段（**内层**：retrieve_skills/_render_sig_lines 命中正文）。
#   外层 INJECT 包整块、内层 EXP/SKILL 各标自己那段——_compact 截段靠内层两 marker 区分
#   EXP/SKILL、整块保留靠外层 INJECT；_collect_seen 扫内层 EXP/SKILL/MEM 取 slug/签名。
#   旧名 MEM_BLOCK_MARKER 裹的实际是经验层内容，与 memory/experience 严格分层命名打架——
#   2026-09-13 还债：EXP 接管经验段，MEM 语义修正后标记【记忆】块。⚠️ 无持久化兼容问题：
#   checkpoint 只存进度 JSON + 图谱快照，不存 LLM history/marker（orchestrate/checkpoint.py）。
#: 记忆层块边界（域私有：day_start/day_resume 模板拼出，_collect_seen 扫描）。
MEM_BLOCK_MARKER = "<MEM>"
#: **外层**注入块边界（域私有：_unified_inject 拼 createplan/revise/入夜 注入块时包裹，
#: _compact 整块保留 + _collect_seen 不直接扫它——扫的是内层 EXP/SKILL/MEM）。
INJECT_BLOCK_MARKER = "<BLOCK>"


class CompactingContextBuilder(SimpleContextBuilder):
    """在**送给 LLM 之前**压缩历史观察，但不动 history 本身。

    一天几十步，每份完整观察 ~800 token，ReAct 的 history 在一天之内只增不减 → 后期上下文里
    会堆着几十份**坐标/库存全已失效**的旧观察。只保留最近 3 份完整的，更早的砍掉观察块、
    留执行反馈（"这一步干了什么、成没成"仍可见）。

    ★ 砍掉就是**真的砍掉**，不留原文也不加"已省略"之类的标注：落盘的 dump 走的是同一个
      build_messages 出口，所以 dump **就等于**真实发出去的上下文 —— 这正是 dump 的唯一价值。
      真要看当时的完整观察，去 env 侧的 trajectory / day_steps（那里逐步存着原文）。

    同理压**计划快照**：createplan/updateplan 每次都返回**当时的完整计划**，一天调十来次 →
    上下文里会堆着很多互相包含的过期中间态。**只保留最后一份计划正文**，更早的收敛成那一行
    状态变更（`第 N 步：pending → success` 是历史事实、不过期，留着有用）。这不违反
    「计划常驻上下文」—— 最新的那份完整计划始终在。

    ★ **只在 build_messages 里做，不改 history** —— 两个原因：
      ① 夜反思读的是 env 侧独立累积的 `day_steps`，本来就与这里无关，压缩不影响它；
      ② history 是 ReActAgent 的事实记录，就地裁剪等于篡改自己的轨迹，调试时会看不出真相。
    """

    def __init__(self, system_prompt: str = "", on_build=None):
        # `on_build`（落盘真正发出去的那份）由基类统一持有与调用 —— 见基类 `_emit`。
        # ⚠️ 本类**只覆写 `_assemble`**：压缩必须发生在 on_build **之前**，
        #    否则存下来的是未压缩版本、与模型实际看到的不一致（而那种错是静默的）。
        #    基类 `build_messages` = `_emit(_assemble(...))`，顺序由它保证，别在这里覆写它。
        super().__init__(system_prompt, on_build=on_build)
        #: 累计砍掉过多少份旧观察 —— 供外部日志确认压缩**真的生效**。
        #: （压缩只发生在送 LLM 的消息里、不进日志，没有这个计数就只能靠读 dump 才知道有没有生效。）
        self.n_trimmed = 0
        #: 同上，但统计被收敛掉的**历史计划快照**份数。
        self.n_plan_trimmed = 0

    def _assemble(self, history=None, current_message=None, **kwargs):
        # 经验段 + 技能签名块已拼进 createplan 的 toolresult（见 CreatePlanTool.run），
        # _compact 压缩旧 createplan 时靠 INJECT_BLOCK_MARKER 整块保留它们（外层标记包
        # header+内层 EXP/SKILL 段）。不再有独立常驻 user 消息——曾那样做是为绕开压缩，
        # 但 role=user 块插在 tool 消息间会误导 LLM（当成额外指令），故改回拼进 toolresult、
        # 用显式 marker 保压缩安全。
        msgs = super()._assemble(history, current_message, **kwargs)
        msgs = self._compact(msgs)
        return msgs

    def _compact(self, msgs: list) -> list:
        # ① 观察：找出所有带观察块的消息，保留最后 N 份完整。
        #   两类都算「观察」，共享同一个「保留最近 KEEP_FULL_OBSERVATIONS 份」滑动窗口：
        #     · role=tool 且含 OBS_SEPARATOR —— write_code 的返回（执行反馈 + 观察正文）；
        #     · role=user 且含 OBS_SEPARATOR —— day_start / day_resume 的开场观察（目标/记忆/反馈/
        #       指令 + 末尾观察段，2026-08-29 改：观察挪到末尾并用 SEP 标边界，黎明那份和 write_code
        #       旧观察一样会过期，必须能被砍）。
        #   按消息顺序排序后取最后 N 份，更早的整段砍掉观察块、留前半（持久内容）。
        obs_idx = [
            i for i, m in enumerate(msgs)
            if m.get("role") in ("tool", "user") and OBS_SEPARATOR in str(m.get("content", ""))
        ]
        stale_obs = set(obs_idx[:-KEEP_FULL_OBSERVATIONS]) if len(obs_idx) > KEEP_FULL_OBSERVATIONS else set()
        # ② 计划：createplan/updateplan 的返回**只留最后一份正文**。
        #    它们每次都返回当时的完整计划，互相包含 —— 留着中间态只是让 Agent 自己分辨哪份是现状。
        plan_idx = [
            i for i, m in enumerate(msgs)
            if m.get("role") == "tool" and m.get("name") in ("createplan", "updateplan")
        ]
        stale_plan = set(plan_idx[:-1]) if len(plan_idx) > 1 else set()
        self.n_trimmed = len(stale_obs)
        self.n_plan_trimmed = len(stale_plan)
        if not stale_obs and not stale_plan:
            return msgs
        out = []
        for i, m in enumerate(msgs):
            if i in stale_obs:
                # 旧观察：**整段砍掉，不留原文、不加标注**。
                # 只留「执行反馈」那半段 —— 那是「这一步干了什么、成没成」，是历史事实、不过期；
                # 而观察正文（坐标/库存/时间/视野）已经失效，留着既占上下文又可能被当成现状。
                body = str(m.get("content", ""))
                kept = body.partition(OBS_SEPARATOR)[0].rstrip()
                # ★ 入夜经验段例外（2026-09-12 改）：_night_memory_inject 把过夜经验拼进
                #   入夜那步 write_code 的 toolresult（INJECT_BLOCK_MARKER 标外层边界，与
                #   createplan 同款）。观察会被砍，但这段经验不该陪葬——它按当天傍晚的处境检索、
                #   整晚有效，砍掉就再也找不回（Agent 未必想起来重新 retrieve）。与 stale_plan
                #   分支同款：据外层标记整段保留。无 marker 的旧观察行为不变（day_start 的 user
                #   观察块不带 marker，不受影响）。
                if INJECT_BLOCK_MARKER in body:
                    seg = body.split(INJECT_BLOCK_MARKER, 1)[1].strip()
                    if seg:
                        kept += "\n" + INJECT_BLOCK_MARKER + "\n" + seg
                out.append({**m, "content": kept})
            elif i in stale_plan:
                # 计划正文丢掉，但**保留那一行状态变更** —— 「第3步：pending → success」是历史
                # 事实、不会过期，是这一天进度的时间线；而计划正文的最新版就在后面。
                # ★ 经验段 + 技能签名块拼在 createplan toolresult 末尾（外层 INJECT_BLOCK_MARKER
                #   包整块、内层 EXP_BLOCK/SKILL_BLOCK 标各段），压缩旧 createplan 时要**整块保留**
                #   ——它们是按那次 goal 检索的经验/技能，丢掉就再也找不回（最新的在最后一条
                #   createplan 里，但旧 goal 检索到的不同经验也有参考价值）。updateplan revise 时也
                #   按 reason 附经验段（无 skill 段），同样据 INJECT_BLOCK_MARKER 保留；updateplan
                #   改状态则无 marker、仍只留 first_line。stale_plan 分支不区分工具名，谁带 marker
                #   谁就被保留。入夜经验段拼在 write_code toolresult 里（见 stale_obs 分支，同款保留）。
                body = str(m.get("content", ""))
                first_line = body.split("\n", 1)[0].rstrip()
                kept = first_line
                # ★ 注入块（header + 内层 EXP 段 + 内层 SKILL 段）整体被 INJECT_BLOCK_MARKER
                #   包裹（_unified_inject 拼），整块保留即可——不必再逐内层 EXP/SKILL 截段
                #   （旧法外层用 EXP_BLOCK、split 取外层后含 header；外层换 INJECT_BLOCK 后
                #   逐内层截会丢 header，故改整块保留，与 stale_obs 同款）。
                if INJECT_BLOCK_MARKER in body:
                    seg = body.split(INJECT_BLOCK_MARKER, 1)[1].strip()
                    if seg:
                        kept += "\n" + INJECT_BLOCK_MARKER + "\n" + seg
                out.append({**m, "content": kept})
            else:
                out.append(m)
        return out

#: 计划里每个 step 的合法状态。pending=未开始，running=正在做，success/failure=已结束。
_STEP_STATES = ("pending", "running", "success", "failure")
#: 状态渲染成文本时的前缀符号（让 Agent 一眼看出进度，不用逐字读状态词）。
_STATE_MARK = {"pending": "○", "running": "▶", "success": "✓", "failure": "✗"}


class DayPlan:
    """一天的计划：goal + 带状态的 steps。**不是** LLM 消息，而是外部持有的状态对象。

    为什么单独建类而不是塞进 history 让 LLM 自己维护：history 会被压缩、也会被 LLM 自己的
    叙述带偏，而计划是**需要精确对齐的结构**（第 3 步是 running 还是 success 决定下一步做什么）。
    由外部持有 + 每轮重新渲染进上下文，才能保证 Agent 每次看到的是**当前真实进度**。
    """

    def __init__(self) -> None:
        self.goal: str = ""
        self.steps: list[dict] = []  # [{"no": 1, "text": ..., "status": ..., "note": ...}]

    @property
    def empty(self) -> bool:
        return not self.steps

    def create(self, goal: str, steps: list[str]) -> None:
        self.goal = (goal or "").strip()
        self.steps = [
            {"no": i, "text": (s or "").strip(), "status": "pending", "note": ""}
            for i, s in enumerate(steps or [], 1)
            if (s or "").strip()
        ]

    def set_status(self, step_no: int, status: str, note: str = "") -> str:
        """改某步状态。返回一句人类可读的结果说明（成功或为什么失败）。"""
        if status not in _STEP_STATES:
            return REACT.result("status_illegal", status=status,
                                states="/".join(_STEP_STATES))
        for s in self.steps:
            if s["no"] == step_no:
                old = s["status"]
                s["status"] = status
                if note:
                    s["note"] = note.strip()
                return REACT.result("status_changed", step_no=step_no,
                                    old=old, status=status)
        return REACT.result("step_missing", step_no=step_no, n_steps=len(self.steps))

    def revise(self, new_steps: list[str], reason: str = "") -> str:
        """整体重排**剩余**步骤：已 success/failure 的保留（那是既成事实，改了就等于篡改历史），
        pending/running 的换成新给的。"""
        done = [s for s in self.steps if s["status"] in ("success", "failure")]
        fresh = [
            {"no": 0, "text": (t or "").strip(), "status": "pending", "note": ""}
            for t in new_steps or []
            if (t or "").strip()
        ]
        self.steps = done + fresh
        for i, s in enumerate(self.steps, 1):  # 重编号，保持连续
            s["no"] = i
        return REACT.result("revised", n_done=len(done),
                            reason=reason or REACT.result("revise_reason_missing"))

    def render(self) -> str:
        """渲染成常驻上下文的那一块。空计划时提示先立计划。

        文案在 yaml，**逐行的结构**（序号/状态符号/note 缩进）留在这里 —— 那是给
        Agent 精确对齐进度用的格式，不是措辞。
        """
        if self.empty:
            return REACT.result("plan_empty")
        lines = [REACT.result("plan_header",
                              goal=self.goal or REACT.result("plan_goal_missing"))]
        for s in self.steps:
            mark = _STATE_MARK.get(s["status"], "?")
            note = f"  ← {s['note']}" if s["note"] else ""
            lines.append(f"  {s['no']}. [{mark} {s['status']}] {s['text']}{note}")
        return "\n".join(lines)


class CreatePlanTool(BaseTool):
    """立当天计划。**顺带按 goal 检索一次记忆**（见 __init__ 的 memory_hook）。"""

    def __init__(self, plan: DayPlan, memory_hook=None, react_state=None):
        # ★ 必须调 super().__init__()：BaseTool 在那里初始化 _before_call_hook /
        #   _on_result_hook 等内部状态，漏调会在 async_call 里 AttributeError。
        super().__init__()
        self._plan = plan
        #: 可选回调 `(goal:str) -> str`：立完计划后按 goal 统一检索经验+技能（见 _make_react_day
        #:   的 _unified_inject），**返回**一段完整注入文本（外层 INJECT_BLOCK 包 site header +
        #:   内层 EXP_BLOCK/SKILL_BLOCK 段），由本工具直接拼进 createplan toolresult 末尾——
        #:   _compact 压缩旧 createplan 时据 INJECT_BLOCK_MARKER 整块保留。
        #:   为什么把检索挂在这里：① **时机**：立计划是整天唯一「意图明确、尚未动手」的时刻，
        #:      goal 天然是一句意图，比拿观察全文做检索 query 更贴近记忆的语义；
        #:      ② **成本**：每步检索成本高（记忆块常驻上下文），这里一天只付 1-2 次；
        #:      ③ **补空档**：ReAct 化之后检索全靠 Agent 主动想起来，容易忘，开局塞一次
        #:      相关经验 = 给它一个起点，而不是等它想起来。
        #: 传 None 则完全不检索（eval / frozen 组、或想做消融时）。
        self._memory_hook = memory_hook
        #: 可选共享容器（rollout 闭包级 dict，与 react_state["agent"] 同款）：立完计划后把
        #:   `plan.goal`（DayPlan 权威值）**追加**进 react_state["last_goals"]，供 night_cb 跨天带成
        #:   次日 day_start 的【昨日目标】块（实验组专属上下文管理，见 __init__ night_cb 门控）。
        #:   ★ 一天多次 createplan 全部进入次日【昨日目标】（2026-09-10 改，用户纠正）：当天 Agent
        #:   可能反复立计划——大方向变了 createplan 覆盖 plan.goal，但每一版都是它当时认定的目标，
        #:   次日都该看到（与【昨日经历】读整天 history、本就含全部 createplan 同口径）。故这里**追加
        #:   进 list**而非覆盖单值；完全相同的 goal 文本去重保序（一次 createplan 重述同一目标不重复列）。
        #:   传 None 则不写（eval）。
        self._react_state = react_state

    @property
    def name(self) -> str:
        return "createplan"

    @property
    def description(self) -> str:
        return REACT.tool_desc("createplan")

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "goal": {
                    "type": "string",
                    "description": REACT.tool_text("createplan", "goal"),
                },
                "steps": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": REACT.tool_text("createplan", "steps"),
                },
            },
            "required": ["goal", "steps"],
        }

    def run(self, goal: str = "", steps: Optional[list] = None, **_: Any) -> str:
        self._plan.create(goal, list(steps or []))
        logger.info("[minecraft计划] 立计划：%s（%d 步）", self._plan.goal, len(self._plan.steps))
        out = REACT.result("plan_created") + "\n" + self._plan.render()
        # 按 goal 统一检索一次经验+技能（_unified_inject）。memory_hook **返回**一段完整注入文本
        #（site header + EXP_BLOCK/SKILL_BLOCK 段，header 在 marker 内——stale_obs 救援是
        # split(MARKER)[1]，header 放 marker 前会在观察剥离时丢失）。_compact 压缩旧 createplan
        # 时据 marker 保留这两段（见 _compact 的 stale_plan 分支）。这样 exp/skill 跟着 toolresult
        # 走、role=tool，不再用独立 role=user 常驻块（那种块插在 tool 消息间会误导 LLM）。
        # 检索失败绝不能影响立计划本身 —— 计划是 ReAct 的骨架，为了一次检索让它报错不值得。
        # ★ 例外：SkillSyntaxError 必须穿出（0914 熔断）——技能库存在语法损坏技能时，
        #   隔离/降级都会让当天每步 write_code 继续 Invalid token 干烧 token，
        #   唯一正确动作是 abort 整 run（穿到 loop.py 的 rollout-abort 路径）。
        # ★ HealFuseError 同理穿出（0930）：连续自愈失败熔断，见 __init__.py 该异常的
        #   docstring；它可能从 memory_hook 内部的检索链路穿上来，吞掉就是又一次失明。
        if self._memory_hook is not None and self._plan.goal:
            try:
                block = self._memory_hook(self._plan.goal)
            except (SkillSyntaxError, HealFuseError):
                raise
            except Exception:  # pylint: disable=broad-except
                logger.warning("[minecraft计划] 立计划后检索记忆失败（不影响计划）", exc_info=True)
                block = ""
            if block:
                out += "\n" + block
        # ★ 把当天 createplan 立的 goal（DayPlan 权威值）追加进共享容器，供 night_cb 跨天带成
        #   次日 day_start 的【昨日目标】块（实验组专属上下文管理）。一天多次 createplan 全部进入
        #   次日【昨日目标】（2026-09-10 改，用户纠正）：每一版都是当天认定的目标，次日都该看到，
        #   与【昨日经历】读整天 history 同口径。完全相同的 goal 文本去重保序（重述同一目标不重复列）。
        #   react_state=None（eval）则不写。
        if self._react_state is not None and self._plan.goal:
            goals = self._react_state.setdefault("last_goals", [])
            if self._plan.goal not in goals:
                goals.append(self._plan.goal)
        return out


class UpdatePlanTool(BaseTool):
    """更新计划：改某步状态，或重排剩余步骤。"""

    def __init__(self, plan: DayPlan, memory_hook: Optional[Callable[[str], str]] = None):
        # ★ 必须调 super().__init__()：BaseTool 在那里初始化 _before_call_hook /
        #   _on_result_hook 等内部状态，漏调会在 async_call 里 AttributeError。
        super().__init__()
        self._plan = plan
        #: revise（重排）时按 reason 统一检索的回调 `(reason:str)->str`，返回一段完整注入文本
        #:（_unified_inject：site header + EXP_BLOCK/SKILL_BLOCK 段，skill 段只 query 补充 delta、
        #: 不重注 base top-N——技能代码当天已全量注入 programs，Agent 想用范围外技能可自己调
        #: retrieve_skills）。只在 revise 操作时触发，改状态（step_no+status）不检索。
        #: 返回值拼进 updateplan toolresult，_compact 压缩旧 updateplan 时据 marker 保留
        #:（与 createplan 同机制，stale_plan 分支不区分工具名）。传 None 则不检索（eval/消融）。
        self._memory_hook = memory_hook

    @property
    def name(self) -> str:
        return "updateplan"

    @property
    def description(self) -> str:
        return REACT.tool_desc("updateplan")

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "step_no": {"type": "integer",
                            "description": REACT.tool_text("updateplan", "step_no")},
                "status": {
                    "type": "string",
                    "enum": list(_STEP_STATES),
                    "description": REACT.tool_text("updateplan", "status"),
                },
                "note": {"type": "string",
                         "description": REACT.tool_text("updateplan", "note")},
                "revise": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": REACT.tool_text("updateplan", "revise"),
                },
                "reason": {"type": "string",
                           "description": REACT.tool_text("updateplan", "reason")},
            },
        }

    def run(
        self,
        step_no: Optional[int] = None,
        status: str = "",
        note: str = "",
        revise: Optional[list] = None,
        reason: str = "",
        **_: Any,
    ) -> str:
        if self._plan.empty:
            return REACT.result("updateplan_no_plan")
        msgs = []
        # 先按当前编号标状态，再重排剩余步骤：revise() 会把所有步骤重新编号，若同一次调用
        # 两者都传、又先 revise，set_status 用的 step_no 就会打在重排后的新第几步上（静默错位）。
        # 先 set_status 还有个附带好处：revise() 保留 success/failure 步骤，刚标完的这步会被
        # 正确当作"已完成"留在新计划里，而不是被换掉。
        if step_no is not None and status:
            msgs.append(self._plan.set_status(int(step_no), status, note))
        if revise:
            msgs.append(self._plan.revise(list(revise), reason))
        if not msgs:
            return REACT.result("updateplan_nothing")
        logger.info("[minecraft计划] %s", "；".join(msgs))
        out = "；".join(msgs) + "\n" + self._plan.render()
        # revise（重排）才按 reason 统一检索；改状态（step_no+status）绝不检索。
        # query 用 reason —— 它是 Agent 写的重排原因，天然描述新方向。
        # 返回文本含 EXP_BLOCK/SKILL_BLOCK marker（与 createplan 同款，header 在 marker 内），
        # _compact 压缩旧 updateplan 时据 marker 保留。
        if revise and self._memory_hook is not None and (reason or "").strip():
            try:
                block = self._memory_hook(reason)
            except Exception:  # pylint: disable=broad-except
                logger.warning("[minecraft计划] 重排后检索记忆失败（不影响计划）", exc_info=True)
                block = ""
            if block:
                out += "\n" + block
        return out


class WriteCodeTool(BaseTool):
    """写一段 JS 在游戏里执行 —— **唯一让 bot 做出动作**的工具。

    ⚠️ 不是「唯一推进世界时间」的工具：昼夜自然流逝，`/pause`
    （/tick freeze + physicsEnabled=false）只在黎明反思那一下调，LLM 思考 / createplan /
    retrieve 期间世界照常走。

    返回值 = 执行反馈 + **完整的新观察**。为什么要带观察：
    `world.step(code)` 执行后整个世界都变了（时间流逝、怪物移动、可能死亡重生），
    观察不是这段代码的"返回值"，而是一次完整的环境快照 —— 只给代码执行结果不足以指导下一步。
    """

    def __init__(
        self,
        do_step: Callable[[str, Optional[int], str], Awaitable[str]],
        default_timeout_ticks: int,
        max_timeout_ticks: int,
    ):
        super().__init__()  # 同上，勿删
        self._do_step = do_step
        self._default_ticks = default_timeout_ticks
        self._max_ticks = max_timeout_ticks
        self.n_calls = 0

    @property
    def name(self) -> str:
        return "write_code"

    @property
    def description(self) -> str:
        return REACT.tool_desc("write_code")

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "explain": {"type": "string",
                            "description": REACT.tool_text("write_code", "explain")},
                "code": {
                    "type": "string",
                    "description": REACT.tool_text("write_code", "code"),
                },
                "timeout_ticks": {
                    "type": "integer",
                    # 两个时限是**构造参数**（随 env 侧 STEP_CODE_TIMEOUT_* 走），不是模块
                    # 常量 —— 故在这里填，yaml 里只留 {default_ticks}/{max_ticks} 占位。
                    # ★ 值是**游戏刻**（字段名 timeout_ticks，单位即刻）。见 react.yaml §2。
                    "description": REACT.tool_text("write_code", "timeout_ticks")
                    .replace("{default_ticks}", str(self._default_ticks))
                    .replace("{max_ticks}", str(self._max_ticks)),
                },
            },
            "required": ["code", "timeout_ticks"],
        }

    async def async_run(
        self, explain: str = "", code: str = "", timeout_ticks: Any = None, **_: Any
    ) -> str:
        code = (code or "").strip()
        if not code:
            return REACT.result("write_code_empty")
        try:
            ms = int(timeout_ticks) if timeout_ticks is not None else self._default_ticks
        except (TypeError, ValueError):
            ms = self._default_ticks
        self.n_calls += 1
        return await self._do_step(code, ms, (explain or "").strip())

    def run(self, **kwargs: Any) -> str:  # pragma: no cover - ReAct 走 async 路径
        raise NotImplementedError("write_code 只支持异步执行（async_run）")
