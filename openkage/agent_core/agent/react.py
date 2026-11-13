"""ReAct Agent 实现。

范式：Reason + Act
每一步：LLM 推理（可能包含 Thought）→ 若有工具调用则执行 → 将结果追加 history → 继续推理
直到 LLM 不再调用工具（输出最终答案）或达到 max_steps。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncGenerator, Callable, Dict, List, Optional, Tuple

from ..context.base import BaseContextBuilder
from ..context.simple import SimpleContextBuilder
from ..llm.base import BaseLLM, StreamChunkEvent, ToolCallDeltaEvent, StreamFinishEvent
from ..tool.registry import ToolRegistry
from .base import AgentResult, BaseAgent
from .events import (
    AgentFinishReason,
    AgentFinalEvent,
    AgentStreamEvent,
    AgentThinkChunkEvent,
    AgentThinkEvent,
    AgentToolCallChunkEvent,
    AgentToolCallEvent,
    AgentToolResultEvent,
    extract_event_text,
)

logger = logging.getLogger(__name__)

# ReActAgent 默认 system prompt
_DEFAULT_REACT_SYSTEM_PROMPT = """你是一个智能助手，能够借助工具逐步解决复杂问题。

## 工作流程

每一步你必须先思考，再决定行动：

Thought: <分析当前情况，明确已知信息、缺少什么、下一步应该做什么>

然后：
- 如果需要更多信息 → 调用合适的工具
- 如果信息已经足够 → 直接给出完整、准确的最终答案

## 规则

1. 每次调用工具前必须先写 Thought，不得省略。
2. 工具结果返回后，继续写 Thought 分析结果，再决定下一步。
3. 不要重复调用已经得到结果的工具。
4. 最终答案要直接回答用户问题，简洁清晰，不要包含 Thought 标记。"""


class ReActAgent(BaseAgent):
    """ReAct 范式 Agent。

    ``context_builder`` 可选，不传时自动使用内置的 ReAct 专用 system prompt。

    循环结构::

        history = [user_message]
        messages = build_messages(history)
        loop (max_steps):
            response = think(messages, tools)
            add_assistant_message(history, response)
            if response.has_tool_calls:
                执行所有工具 → add_tool_result(history, ...)
                messages = build_messages(history)   # 重建，不追加新 user
            else:
                返回 STOP，response.content 即最终答案

    子类可覆写 ``_on_tool_result()`` 在每次工具返回后插入额外逻辑。
    """

    def __init__(
        self,
        llm: BaseLLM,
        context_builder: Optional[BaseContextBuilder] = None,
        tool_registry: Optional[ToolRegistry] = None,
        max_steps: int = 20,
        llm_config: Optional[Dict[str, Any]] = None,
        should_stop: Optional[Callable[[], bool]] = None,
        on_step: Optional[Callable[[List[Dict[str, Any]]], None]] = None,
    ):
        """
        Args:
            llm:             LLM 实例。
            context_builder: 上下文构建器。为 None 时使用内置 ReAct 专用 prompt。
            tool_registry:   工具注册表，为 None 时表示无工具。
            max_steps:       推理循环最大步数。
            llm_config:      每次调用 LLM 时透传的额外参数（temperature 等）。
            should_stop:     **OpenKAGE新增**（唯一与 backend 版本的差异）。无参回调，返回 True 时
                             循环在**下一次 think 之前**干净收尾，返回 EXTERNAL_STOP。

                             为什么需要：原本的出口只有两个 —— LLM 不再调工具、或撞 max_steps。
                             但OpenKAGE的 Minecraft 域是「**一天一轮 ReAct**」，出口是**天亮**这个
                             外部事件（节律由昼夜决定，不由步数决定，见 domains/minecraft/env.py
                             开头对 max_nights 的说明）。没有这个钩子就只能靠「在工具返回值里
                             写『天亮了请结束』让 LLM 自觉停」（不可靠）或抛异常打断（history
                             会带着异常收尾）。

                             检查点选在 think **之前**：此时上一轮工具结果已写进 history，
                             收尾时 history 是完整且自洽的（不会停在「调了工具但没记结果」的
                             半状态），外层拿到的轨迹可以直接交给夜反思。
            on_step:         **OpenKAGE新增**（与 should_stop 同类的小钩子）。每步**工具执行完毕后、
                             build_messages 之前**调一次，传入当前 `history` 引用。回调可往 history
                             追加消息（如一次性事件提示），这条消息随即进入本步 build_messages、
                             供下次 think 看到。

                             为什么需要：Minecraft 域的「入夜」是环境事件，不是某次工具的结果——
                             env 在 write_code 执行期间打标记，域侧用本钩子读标记、用闭包里的检索
                             把过夜经验拼进那步 write_code 的 toolresult 末尾（EXP_BLOCK_MARKER
                             标边界，不依赖 Agent 主动 retrieve，也不插独立 user 消息）。
                             回调自身异常不应拖垮轨迹，故在调用处吞掉。
        """
        if context_builder is None:
            context_builder = SimpleContextBuilder(_DEFAULT_REACT_SYSTEM_PROMPT)
        super().__init__(
            llm=llm,
            context_builder=context_builder,
            tool_registry=tool_registry,
            max_steps=max_steps,
            llm_config=llm_config,
        )
        self.should_stop = should_stop
        self.on_step = on_step

    def _external_stop(self) -> bool:
        """外部是否要求停（should_stop 未传则恒 False）。回调自身异常不应拖垮轨迹，故吞掉。"""
        if self.should_stop is None:
            return False
        try:
            return bool(self.should_stop())
        except Exception:  # pylint: disable=broad-except
            logger.warning("[ReAct] should_stop 回调抛异常，按「不停」处理", exc_info=True)
            return False

    def _run_on_step(self, history: List[Dict[str, Any]]) -> None:
        """每步工具执行完毕后调域侧 on_step 钩子（与 should_stop 同类，OpenKAGE新增）。

        钩子可往 history 追加消息（一次性事件提示），随即进入本步 build_messages、供下次 think
        看到。回调自身异常不应拖垮轨迹，故吞掉。on_step 未传则 no-op。
        """
        if self.on_step is None:
            return
        try:
            self.on_step(history)
        except Exception:  # pylint: disable=broad-except
            logger.warning("[ReAct] on_step 回调抛异常，已忽略", exc_info=True)

    def _run(self, user_input: str, initial_history: Optional[List[Dict[str, Any]]] = None,
             **kwargs: Any) -> AgentResult:
        # ★ initial_history（OpenKAGE新增）：续跑同一段对话时，把上一轮的 result.history 灌进来，
        #   这轮就从「接着上下文继续追问」开始，而不是从空白重开。None（默认）时行为不变：
        #   history 从 [] 起，与新对话完全一致。灌入的是不含 system 的 OpenAI 格式消息
        #   （system 由 context_builder.build_messages 每次单独拼），故接 user_input 后序列合法。
        #   ⚠️ 必须深拷贝：灌入的 history 通常是上一轮 agent 的 result.history 引用，而本函数
        #      会原地 append 到 history —— 不拷贝会污染上一轮的返回值（调用方可能还持有它）。
        import copy
        history: List[Dict[str, Any]] = (
            copy.deepcopy(initial_history) if initial_history else []
        )
        # OpenKAGE新增：把进行中的 history 暴露到 self 上（与 should_stop 同类，见下方 _async_run）。
        self.history = history

        # 将 user 消息加入 history，再构建完整 messages（system + history）
        self.context_builder.add_user_message(history, user_input)
        messages = self.context_builder.build_messages(history, **kwargs)

        tools = self.tool_registry.get_definitions() or None

        # ★ reasoning 观测收集（见 _async_run 同名注释）：只进 metadata、不进 history。
        reasonings: List[Dict[str, Any]] = []

        for step in range(self.max_steps):
            # OpenKAGE新增：外部中止检查。放在 think **之前** —— 此时上一轮的工具结果已写进
            # history，收尾时轨迹完整自洽（不会停在「调了工具但没记结果」的半状态）。
            if self._external_stop():
                logger.info("[ReAct] 外部条件要求停止，已跑 %d 步", step)
                return AgentResult(
                    finish_reason=AgentFinishReason.EXTERNAL_STOP,
                    final_response=None,
                    history=history,
                    metadata={"reasonings": reasonings},
                )
            logger.debug("[ReAct] ── step %d ──", step + 1)

            response = self.think(messages, tools=tools)

            # ★ 收集本步 reasoning（空串=该步无思考，仍记一条占位以保持步号对齐）
            reasonings.append({"step": step + 1, "reasoning": response.reasoning})

            # 打印 Thought（LLM 在工具调用前输出的思考过程）
            if response.content:
                logger.debug("[ReAct] Thought: %s", response.content)

            # 将 assistant 消息追加到 history
            self.context_builder.add_assistant_message(history, response)

            if not response.has_tool_calls:
                logger.debug("[ReAct] 完成，共 %d 步", step + 1)
                return AgentResult(
                    finish_reason=AgentFinishReason.STOP,
                    final_response=response,
                    history=history,
                    metadata={"reasonings": reasonings},
                )

            # 执行所有工具调用，结果追加到 history
            for tc in response.tool_calls:
                logger.debug("[ReAct] Action: %s(%s)", tc.name, tc.arguments)
                result = self.tool_registry.execute(tc.name, tc.arguments)
                logger.debug(
                    "[ReAct] Observation: %s",
                    result[:300] if isinstance(result, str) else result,
                )
                self.context_builder.add_tool_result(history, tc.id, tc.name, result)
                self._on_tool_result(tc.name, tc.arguments, result, history, **kwargs)

            # OpenKAGE新增：每步工具执行后的域侧钩子（与 should_stop 同类）。在 build_messages 前，
            # 这样钩子改写/追加的消息会进本步 messages、供下次 think 看到。minecraft 用它做入夜
            # 强制检索：env 打标记 → 这里读标记、检索、把过夜经验拼进本步 write_code 的 toolresult。
            self._run_on_step(history)

            # 重建 messages（system + 更新后的 history，不追加新 user）
            messages = self.context_builder.build_messages(history, **kwargs)

        logger.warning("[ReAct] 达到最大步数 %d，强制结束", self.max_steps)
        return AgentResult(
            finish_reason=AgentFinishReason.MAX_STEPS,
            final_response=None,
            history=history,
            metadata={"reasonings": reasonings},
        )

    # ------------------------------------------------------------------
    # 异步推理循环
    # ------------------------------------------------------------------

    async def _async_run(self, user_input: str,
                         initial_history: Optional[List[Dict[str, Any]]] = None,
                         **kwargs: Any) -> AgentResult:
        """异步版本的 ReAct 推理循环。

        与同步 ``_run()`` 逻辑完全一致，但使用：
        - ``async_think()`` 异步调用 LLM
        - ``tool_registry.async_execute()`` 异步执行工具

        这使得在 IO 密集场景（网络请求、文件读写）下不会阻塞事件循环。

        Args:
            user_input: 本轮的用户输入。
            initial_history: OpenKAGE新增——续跑同一段对话时灌入的上一轮 history（不含 system 的
                OpenAI 格式消息）。None 时从空白开始（默认，与新对话一致）。灌入后本轮的
                ``user_input`` 作为新 user 消息接在末尾，形成「接着上下文继续追问」。
        """
        # ★ initial_history（OpenKAGE新增）：见 _run 同名参数的注释。Minecraft 域「天内续跑」用
        #   它把前一轮 ReAct 的 result.history 续上，避免续跑轮从零丢失当天前半段对话。
        #   ⚠️ 深拷贝同 _run：灌入的 history 多为上一轮 result.history 引用，原地 append 会污染它。
        import copy
        history: List[Dict[str, Any]] = (
            copy.deepcopy(initial_history) if initial_history else []
        )

        # OpenKAGE新增：暴露「当天进行中的 history」给域侧。
        # MC 域的 night_cb（黎明反思）在 **async_run 返回之前** 就被触发——它由天亮那一步的
        # WriteCodeTool 经 do_step 在下面的 `asyncio.gather(async_execute(...))` 执行期间调用
        # （见 domains/minecraft/env.py 的 _do_one_step 天亮处理）。那时 `result.history` 还没产出，
        # 而 history 是本函数的局部变量。Minecraft 域的「次日目标」镜像调用要拿这份当天完整对话
        # （history 原样 + 末尾一句 user 消息）发给同一模型，故需要从外部读到它。
        # history 列表是**原地变更**的（add_user_message/add_assistant_message/add_tool_result
        # 都 append 到它），故这个引用在 async_run 全程都指向当天进行中的完整对话，
        # 结束后自然等于 AgentResult.history。与 should_stop 同属「一天一轮 ReAct」的域需求钩子。
        self.history = history

        self.context_builder.add_user_message(history, user_input)
        messages = self.context_builder.build_messages(history, **kwargs)

        tools = self.tool_registry.get_definitions() or None

        # ★ reasoning 观测收集（2026-09-16）：每步 LLM 响应的内部思维链（response.reasoning）
        #   收集到这里，跑完整轮随 AgentResult.metadata 一次性落盘（见 agent_io.dump_reasonings）。
        #   **只进 metadata、不进 history**：add_assistant_message 只写 content+tool_calls，
        #   reasoning 不会进 history → 不会进请求 messages、不会进续跑灌入的 initial_history。
        reasonings: List[Dict[str, Any]] = []

        for step in range(self.max_steps):
            # OpenKAGE新增：外部中止检查（同步版同理，见那边的注释）
            if self._external_stop():
                logger.info("[ReAct-Async] 外部条件要求停止，已跑 %d 步", step)
                return AgentResult(
                    finish_reason=AgentFinishReason.EXTERNAL_STOP,
                    final_response=None,
                    history=history,
                    metadata={"reasonings": reasonings},
                )
            logger.debug("[ReAct-Async] ── step %d ──", step + 1)

            response = await self.async_think(messages, tools=tools)

            # ★ 收集本步 reasoning（空串=该步无思考，仍记一条占位以保持步号对齐）
            reasonings.append({"step": step + 1, "reasoning": response.reasoning})

            if response.content:
                logger.debug("[ReAct-Async] Thought: %s", response.content)

            self.context_builder.add_assistant_message(history, response)

            if not response.has_tool_calls:
                # ⚠️ **区分「真的做完了」与「被 max_tokens 截断」**（2026-08-14 加）：
                # 两者都表现为"没有 tool_calls"，但含义相反。thinking 类模型
                # （实测 deepseek-v4-flash-202607）的 reasoning_tokens 与可见输出**共用**
                # max_tokens 预算 —— 思考稍长就把预算吃光，返回 finish_reason="length" +
                # content="" + 无 tool_calls。旧代码把它当 STOP 正常收工，于是：
                #   聚合 Agent 查了 7 次概念、正要写库时被截断 → 循环 return → Δconcept=+0，
                #   **全程无一条告警**（实测：3/99 步就"完成"，agent_summary 是空字符串）。
                # 判据用 finish_reason 而非"content 是否为空"：正常收工也可能只回一句极短的话，
                # 而 length 是网关明确告诉我们"没写完"的唯一可靠信号。
                if response.finish_reason == "length":
                    logger.error(
                        "[ReAct-Async] 第 %d 步响应被 max_tokens 截断（finish_reason=length，"
                        "content 空、无 tool_call）—— **这不是完成**。thinking 模型的思考 token "
                        "与输出共用预算，请抬高 llm.max_tokens.reflect_step。"
                        "本次按 LENGTH_TRUNCATED 结束，已写入的部分保留。", step + 1,
                    )
                    return AgentResult(
                        finish_reason=AgentFinishReason.LENGTH_TRUNCATED,
                        final_response=response,
                        history=history,
                        metadata={"reasonings": reasonings},
                    )
                # 同一类陷阱的另一半：网关/SDK 把**请求期错误**收敛成
                # `LLMResponse(finish_reason="error", content="Error: …")`（见 OpenAICompatLLM.think
                # 的 except 分支），它同样没有 tool_calls，于是也会掉进这个 `if` 里被当成"做完了"。
                # 最要紧的一种是**超上下文窗**：实测 deepseek-v4-flash 上限 1,048,576 tok，
                # 超了回 400 且 `retryable=False`（重试也救不回来）。MC 长轨迹的反思/聚合
                # 上下文实测已到 8~10 万 tok 量级，离上限尚远但**同一方向**在涨。
                # 不喊出来的话，表现是"Agent 这一步就收工了"，与正常结束无法区分。
                if response.finish_reason == "error":
                    logger.error(
                        "[ReAct-Async] 第 %d 步 LLM 调用失败（finish_reason=error，无 tool_call）"
                        "—— **这不是完成**。常见原因：超模型上下文窗（不可重试）、网关畸形响应。"
                        "原文：%s", step + 1, (response.content or "")[:300],
                    )
                    return AgentResult(
                        finish_reason=AgentFinishReason.ERROR,
                        final_response=response,
                        history=history,
                        metadata={"reasonings": reasonings},
                    )
                logger.debug("[ReAct-Async] 完成，共 %d 步", step + 1)
                return AgentResult(
                    finish_reason=AgentFinishReason.STOP,
                    final_response=response,
                    history=history,
                    metadata={"reasonings": reasonings},
                )

            # 并行执行所有工具调用
            async def _exec(tc_item: Any) -> Tuple[Any, str]:
                res = await self.tool_registry.async_execute(
                    tc_item.name, tc_item.arguments
                )
                return tc_item, res

            pairs = await asyncio.gather(*[_exec(tc) for tc in response.tool_calls])

            # 按原始顺序写入 history
            for tc, result in pairs:
                logger.debug(
                    "[ReAct-Async] Observation[%s]: %s",
                    tc.name,
                    result[:300] if isinstance(result, str) else result,
                )
                self.context_builder.add_tool_result(history, tc.id, tc.name, result)
                await self._async_on_tool_result(
                    tc.name, tc.arguments, result, history, **kwargs
                )

            # OpenKAGE新增：每步工具执行后的域侧钩子（同步版同理，见 _run 注释）。在 build_messages 前，
            # 钩子改写/追加的消息会进本步 messages、供下次 think 看到。minecraft 用它做入夜强制检索
            # （拼进入夜那步 write_code 的 toolresult，EXP_BLOCK_MARKER 标边界）。
            self._run_on_step(history)

            messages = self.context_builder.build_messages(history, **kwargs)

        logger.warning("[ReAct-Async] 达到最大步数 %d，强制结束", self.max_steps)
        return AgentResult(
            finish_reason=AgentFinishReason.MAX_STEPS,
            final_response=None,
            history=history,
            metadata={"reasonings": reasonings},
        )

    # ------------------------------------------------------------------
    # 扩展钩子
    # ------------------------------------------------------------------

    def _on_tool_result(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        result: str,
        history: List[Dict[str, Any]],
        **kwargs: Any,
    ) -> None:
        """每次工具执行完成后的同步钩子，子类可覆写。

        Args:
            tool_name:  工具名称。
            arguments:  工具调用参数。
            result:     工具执行结果字符串。
            history:    当前 history（已包含本次 tool result）。
        """

    async def _async_on_tool_result(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        result: str,
        history: List[Dict[str, Any]],
        **kwargs: Any,
    ) -> None:
        """每次工具执行完成后的异步钩子，子类可覆写。

        默认调用同步版 ``_on_tool_result()``。

        Args:
            tool_name:  工具名称。
            arguments:  工具调用参数。
            result:     工具执行结果字符串。
            history:    当前 history（已包含本次 tool result）。
        """
        self._on_tool_result(tool_name, arguments, result, history, **kwargs)

    # ------------------------------------------------------------------
    # 流式推理（异步生成器）
    # ------------------------------------------------------------------

    async def async_stream(
        self, user_input: str, **kwargs: Any
    ) -> AsyncGenerator[AgentStreamEvent, None]:
        """异步流式推理，每个关键节点 yield 一个事件。

        事件类型（按发生顺序）：
        - AgentThinkChunkEvent — LLM 思考文本的实时分片（流式）
        - AgentThinkEvent      — 本轮思考完成汇总（含完整 Thought 文本）
        - AgentToolCallEvent   — 即将调用某个工具
        - AgentToolResultEvent — 工具执行完毕
        - AgentFinalEvent      — 最终回复（推理结束）

        Args:
            user_input: 用户输入文本。
            **kwargs:   透传给 context_builder.build_messages() 的额外参数。

        Yields:
            AgentStreamEvent
        """
        history: List[Dict[str, Any]] = []
        self.context_builder.add_user_message(history, user_input)
        messages = self.context_builder.build_messages(history, **kwargs)
        tools = self.tool_registry.get_definitions() or None

        for step in range(1, self.max_steps + 1):
            logger.debug("[ReAct-Stream] ── step %d ──", step)

            # 使用 stream_think 实时流出文本
            response = None
            async for event in self.llm.stream_think(
                messages,
                tools=tools,
                tool_choice="auto" if tools else None,
                config=self.llm_config,
            ):
                if isinstance(event, StreamChunkEvent):
                    yield AgentThinkChunkEvent(
                        delta=event.delta,
                        content=event.content,
                        step=step,
                    )
                elif isinstance(event, ToolCallDeltaEvent):
                    yield AgentToolCallChunkEvent(
                        index=event.index,
                        id=event.id,
                        tool_name=event.name,
                        arguments_delta=event.arguments_delta,
                        arguments_so_far=event.arguments_so_far,
                        step=step,
                    )
                elif isinstance(event, StreamFinishEvent):
                    response = event.response

            # stream_think 异常时 response 可能为 None
            if response is None or response.is_error:
                error_msg = (
                    response.metadata.get("error_message", "未知错误")
                    if response
                    else "流式调用异常"
                )
                logger.error("[ReAct-Stream] LLM 错误: %s", error_msg)
                yield AgentFinalEvent(
                    content=f"[Error] {error_msg}",
                    finish_reason=AgentFinishReason.ERROR,
                )
                return

            # 本轮思考完成，yield 汇总事件
            if response.content:
                logger.debug("[ReAct-Stream] Thought: %s", response.content)
                yield AgentThinkEvent(content=response.content, step=step)

            self.context_builder.add_assistant_message(history, response)

            if not response.has_tool_calls:
                # 最终回复
                logger.debug("[ReAct-Stream] 完成，共 %d 步", step)
                yield AgentFinalEvent(
                    content=response.content,
                    finish_reason=AgentFinishReason.STOP,
                )
                return

            # 并行执行所有工具调用，先 yield 所有 AgentToolCallEvent
            for tc in response.tool_calls:
                yield AgentToolCallEvent(
                    id=tc.id,
                    tool_name=tc.name,
                    arguments=tc.arguments,
                    step=step,
                    index=tc.index,
                )
                logger.debug("[ReAct-Stream] Action: %s(%s)", tc.name, tc.arguments)

            # 并行流式执行：用 Queue 汇聚多个生成器的事件
            # queue 中放 AgentToolRunEvent / AgentToolResultEvent，或 None 表示某个工具完成
            _DONE = object()  # 哨兵
            queue: asyncio.Queue = asyncio.Queue()

            # 本轮起始 index = 当前累积调用总数，本轮结束后加上本轮工具数
            round_start = self.llm.tool_call_count

            async def _run_tool(tc_item: Any, tc_index: int) -> None:
                """在独立 task 中流式执行单个工具，结果推入 queue。"""
                async for run_event in self.tool_registry.async_stream_execute(
                    tc_item.id, tc_item.name, tc_item.arguments, step, tc_index
                ):
                    await queue.put(run_event)
                await queue.put(_DONE)

            # 启动所有工具 task，全局 index = round_start + tc.index
            tasks = [
                asyncio.create_task(_run_tool(tc, round_start + tc.index))
                for tc in response.tool_calls
            ]
            pending = len(tasks)

            # 按到达顺序消费事件，同时收集每个工具的最终结果
            results: Dict[str, Any] = {}
            while pending > 0:
                item = await queue.get()
                if item is _DONE:
                    pending -= 1
                    continue
                yield item
                # AgentToolResultEvent 携带完整 result，直接记录
                if isinstance(item, AgentToolResultEvent):
                    results[item.id] = item.result  # str 或 AgentStreamEvent

            # 等待所有 task 正常退出（异常已在 _run_tool 内部处理）
            await asyncio.gather(*tasks, return_exceptions=True)

            # 本轮工具调用结束，累积调用总数
            self.llm.tool_call_count += len(response.tool_calls)

            # 按原始顺序写入 history，保证 LLM 上下文顺序一致
            for tc in response.tool_calls:
                result = results.get(tc.id, "")
                result_str = extract_event_text(result)
                logger.debug(
                    "[ReAct-Stream] Observation[%s]: %s", tc.name, result_str[:300]
                )

                self.context_builder.add_tool_result(
                    history, tc.id, tc.name, result_str
                )
                await self._async_on_tool_result(
                    tc.name, tc.arguments, result_str, history, **kwargs
                )
            # OpenKAGE新增：每步工具执行后的域侧钩子（与 _run/_async_run 同步一致）。
            self._run_on_step(history)
            messages = self.context_builder.build_messages(history, **kwargs)

        logger.warning("[ReAct-Stream] 达到最大步数 %d，强制结束", self.max_steps)
        yield AgentFinalEvent(
            content="",
            finish_reason=AgentFinishReason.MAX_STEPS,
        )
