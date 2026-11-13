"""上下文构造器。裁剪自 backend/agent_core/context/（见上级 __init__ 的说明）。

未复制 skill_prompt / profile_skill —— 那是生产侧的技能与用户画像上下文，OpenKAGE不用。
"""

from .base import BaseContextBuilder
from .simple import SimpleContextBuilder

__all__ = ["BaseContextBuilder", "SimpleContextBuilder"]
