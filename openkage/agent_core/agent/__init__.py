"""ReAct 这条链需要的 agent 类。裁剪自 backend/agent_core/agent/（见上级 __init__ 的说明）。

未复制 plan_and_solve / reflection —— 那是另两种推理范式，OpenKAGE只用 ReAct。
"""

from .base import AgentResult, BaseAgent
from .events import (
    AgentFinalEvent,
    AgentFinishReason,
    AgentStreamEvent,
    AgentThinkChunkEvent,
    AgentThinkEvent,
    AgentToolCallChunkEvent,
    AgentToolCallEvent,
    AgentToolResultEvent,
    AgentToolRunEvent,
    extract_event_text,
)
from .react import ReActAgent

__all__ = [
    "AgentResult",
    "BaseAgent",
    "AgentFinalEvent",
    "AgentFinishReason",
    "AgentStreamEvent",
    "AgentThinkChunkEvent",
    "AgentThinkEvent",
    "AgentToolCallChunkEvent",
    "AgentToolCallEvent",
    "AgentToolResultEvent",
    "AgentToolRunEvent",
    "extract_event_text",
    "ReActAgent",
]
