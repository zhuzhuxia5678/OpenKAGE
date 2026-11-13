"""LLM 抽象基类。裁剪自 backend/agent_core/llm/（见上级 __init__ 的说明）。

只要 base；实例由 openkage/llm.py 的 get_llm() 提供（OpenAICompatLLM 是 BaseLLM 子类）。
"""

from .base import BaseLLM, LLMResponse, MessageRole

__all__ = ["BaseLLM", "LLMResponse", "MessageRole"]
