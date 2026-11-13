"""Minecraft 域 ReAct 提示词：框架 `AgentPrompts` 的一个子类，读 `prompts/react.yaml`。

    from .prompt_store import REACT
    REACT.render_system(default_ticks=…, max_ticks=…, …)  # 域级 system prompt（占位符由调用方填）
    REACT.tool_desc("createplan")                     # 工具 description
    REACT.tool_text("write_code", "code")             # 某个参数的说明
    REACT.result("plan_empty")                        # 工具返回给 LLM 的文案（基类实现）
    REACT.render_user_input("day_start", {...})       # 每轮 ReAct 的开场话
    REACT.night_system                                # 夜反思那次轻量调用的 system

框架侧「有没有词表」由 yaml 自己声明（有 `vocab:` 那一节就是有），本 Agent 没有词表；
本文件只声明本 Agent 独有的东西：两节顶层键 + 五个工具的参数清单。读 yaml、占位符渲染与
校验、`render_user_input`、`tool_text`、`result` 全部继承自基类。

`tool_results` 在基类是**可选**节（reflect/aggregate 不写，全走 `prompts/default.yaml`），
本域把它列进 `SECTIONS` 升成**必需** —— 本域有一堆自己的键（`plan_*` / `write_code_empty` …），
少了那一节就是漏写。框架层 retrieve/expand 的文案不在本域 yaml 里，`result()` 会落到 default。

依赖方向仍是单向的：框架 `openkage/` 不认识任何域，是域来复用框架的装载器。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from openkage.memory.prompt_store import AgentPrompts

_PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

#: 每个工具**必须**有说明的参数（键名与 py 侧 `parameters.properties` 一一对应）。
#: 加参数时要同步在这里和 yaml 里补 —— 漏了就在 import 时炸，而不是让模型收到一个
#: 没有 description 的字段（那种情况它只能猜，且日志里看不出来）。
_REQUIRED_PARAMS: dict[str, tuple[str, ...]] = {
    "createplan": ("goal", "steps"),
    "updateplan": ("step_no", "status", "note", "revise", "reason"),
    # hops：多跳扩展的跳数，由 Agent 自己定（图谱层用 MAX_HOPS 夹上限）。
    # layer：私有图谱分 memory/experience 两层，默认 experience（记忆层有滑动窗口常驻注入）；
    #   2026-09-05 恢复（2026-08-08 曾撤掉，现按用户要求默认只查经验层、显式 memory 才查记忆层）。
    "retrieve": ("query", "scope", "hops", "layer"),
    "expand": ("slug", "relation", "scope"),
    "write_code": ("explain", "code", "timeout_ticks"),
}

#: user_inputs 必须有的模板（react_day 的两条路径：新的一天 / 同一天续跑）。
_REQUIRED_TEMPLATES = ("day_start", "day_resume")


class ReActPrompts(AgentPrompts):
    """任务域的 ReAct Agent。**没有 `vocab:`**（它不写图谱），
    而「没有词表」这件事由 yaml 里没那一节自己表达，不需要在这儿声明。"""

    #: 本 Agent **必需**的顶层键（基类那三个不用重复写）。
    #: `tool_results` 在基类是可选的（缺的键落到 default.yaml），本域有一堆自己的键 ——
    #: 列在这里是为了「整节漏写」也能在 import 期炸出来。
    SECTIONS = ("tool_results", "night_reflection")

    def __init__(self, agent: str = "react") -> None:
        # nested=False：域私有目录里就一份 yaml（`prompts/react.yaml`），
        # 不像框架 `prompts/` 那样每个 Agent 套一层同名子目录。
        super().__init__(agent, base_dir=_PROMPTS_DIR, nested=False)
        # `tool_results` 由基类赋值（它是可选节）；本域的 SECTIONS 只是把它升成**必需**。
        self._night: dict[str, Any] = self.raw["night_reflection"]

    def _extra_validate(self) -> None:
        """本 Agent 特有的检查（基类已管：顶层键齐全 / system_subs 占位符真的存在）。

        本钩子在基类 `__init__` 中途被调用（`self.tools` / `self.tool_results` 还没赋值），
        所以这里一律读 `self.raw` —— 那是最早就准备好的原始 dict。
        """
        p = self.path
        tools = self.raw.get("tools") or {}
        user_inputs = self.raw.get("user_inputs") or {}
        # ① 五个工具都在，且 desc + 每个参数说明齐全
        for tool, params in _REQUIRED_PARAMS.items():
            spec = tools.get(tool)
            if not isinstance(spec, dict):
                raise ValueError(f"{p}：tools 缺少 `{tool}:`（ReAct 的 5 个工具都必须有文案）")
            if not str(spec.get("desc") or "").strip():
                raise ValueError(f"{p}：tools.{tool} 缺 `desc`。空 description 会让模型完全"
                                 f"不知道这个工具干什么，而这种错读日志也看不出来")
            missing = [k for k in params if not str(spec.get(k) or "").strip()]
            if missing:
                raise ValueError(
                    f"{p}：tools.{tool} 缺参数说明 {missing}。模型会收到一个没有 description "
                    f"的字段、只能靠猜 —— 加参数时要同步补 yaml 与 _REQUIRED_PARAMS"
                )
            extra = [k for k in spec if k != "desc" and k not in params]
            if extra:
                raise ValueError(
                    f"{p}：tools.{tool} 有多余的键 {extra}。它们**不会**被送给模型"
                    f"（py 侧只按 _REQUIRED_PARAMS 取）—— 要么是参数改了名、要么是写错了位置"
                )
        # ② 两个 user_input 模板都在且非空
        for tpl in _REQUIRED_TEMPLATES:
            if not user_inputs.get(tpl):
                raise ValueError(f"{p}：user_inputs 缺少（或为空）`{tpl}:`")
        # ③ 夜反思的 system / user 非空
        if not str((self.raw.get("night_reflection") or {}).get("system") or "").strip():
            raise ValueError(f"{p}：night_reflection.system 不能为空")
        if not str((self.raw.get("night_reflection") or {}).get("user") or "").strip():
            raise ValueError(f"{p}：night_reflection.user 不能为空")

    # ---- 夜反思 ----
    @property
    def night_system(self) -> str:
        return str(self._night["system"]).strip()

    def night_user(self, day_idx: int) -> str:
        """夜反思镜像调用的 user 指令（文案在 `night_reflection.user`）。

        `day_idx`：刚结束的那一天（第 N 天）；次日 = day_idx + 1（占位符 {next_day_idx}）。
        字面替换 `{day_idx}`/`{next_day_idx}`，未填占位符抛错（与 result() 同防线，
        避免残缺指令静默送给模型）。
        """
        text = str(self._night["user"])
        values = {"day_idx": day_idx, "next_day_idx": day_idx + 1}
        for name, val in values.items():
            text = text.replace("{" + name + "}", str(val))
        left = re.findall(r"\{(\w+)\}", text)
        if left:
            raise KeyError(
                f"{self.path}：night_reflection.user 还有未填的占位符 {left}，"
                f"已传：{sorted(values)}"
            )
        return text.strip()


#: 单例（模块加载时读一次 yaml + 全量校验）
REACT = ReActPrompts()
