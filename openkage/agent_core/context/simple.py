"""简单上下文构建器。

适用于 system prompt 固定、无需动态拼装的场景。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from ..llm.base import MessageRole
from .base import BaseContextBuilder

logger = logging.getLogger(__name__)


class SimpleContextBuilder(BaseContextBuilder):
    """固定 system prompt 的上下文构建器。

    适合快速使用，不需要动态拼装 system prompt 的场景。

    Example::

        ctx = SimpleContextBuilder("你是一个教育助手。")

        # 初始化对话
        messages = ctx.build_messages([], "帮我解这道题")
        # → [{"role": "system", ...}, {"role": "user", ...}]

        # 刷新 system + 已有历史，不追加新用户消息
        messages = ctx.build_messages(history)
        # → [{"role": "system", ...}, ...history...]

        # 落盘真正发出去的那份（见 openkage.agent_io.ContextDumper）
        ctx = SimpleContextBuilder(prompt, on_build=dumper)
    """

    def __init__(self, system_prompt: str, on_build=None):
        """
        Args:
            system_prompt: 固定的 system prompt 文本。
            on_build: 可选回调 `(messages) -> None`，**每次构建完消息后**调用一次。
                用于落盘"真正发给 LLM 的那份"（见 `openkage.agent_io.ContextDumper`）。
                ★ 挂在这一层而不是 ReActAgent 里：这里是**送 LLM 前的最后一站**
                （子类的压缩也已经做完，见 `_emit`），存下来的就是模型真正看到的东西。
                回调抛异常只警告、不打断主流程（留痕是附带产物）。
        """
        self._system_prompt = system_prompt
        self._on_build = on_build

    def build_system_prompt(self, **kwargs: Any) -> str:
        return self._system_prompt

    def build_messages(
        self,
        history: List[Dict[str, Any]] = None,
        current_message: Optional[str] = None,
        **kwargs: Any,
    ) -> List[Dict[str, Any]]:
        """构建消息列表：[system] + history + [user]，然后过 `on_build` 回调。

        ⚠️ **子类要改消息内容就覆写 `_assemble`，不要覆写本方法** —— 本方法负责
        「组装完 → 通知 on_build」这个不变量，覆写它会让子类压缩后的版本绕过留痕，
        存下来的就不是模型真正看到的东西了（那种错是静默的：文件照写、内容对不上）。

        Args:
            history:         历史对话消息列表。
            current_message: 用户输入，为 None 时不追加 user 消息。

        Returns:
            OpenAI 格式的消息列表。
        """
        return self._emit(self._assemble(history, current_message, **kwargs))

    def _emit(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """把最终消息交给 `on_build` 回调后原样返回。**留痕失败绝不影响主流程。**"""
        if self._on_build is not None:
            try:
                self._on_build(messages)
            except Exception:  # pylint: disable=broad-except
                logger.warning("上下文留痕回调失败（不影响主流程）", exc_info=False)
        return messages

    def _assemble(
        self,
        history: List[Dict[str, Any]] = None,
        current_message: Optional[str] = None,
        **kwargs: Any,
    ) -> List[Dict[str, Any]]:
        """真正拼消息的那一步。**子类要动消息就覆写这里**（见 `build_messages` 的 ⚠️）。"""
        if not history:
            history = []
        messages: List[Dict[str, Any]] = [
            {"role": MessageRole.SYSTEM.value, "content": self._system_prompt},
            *history,
        ]
        if current_message is not None:
            messages.append(
                {"role": MessageRole.USER.value, "content": current_message}
            )
        return messages
