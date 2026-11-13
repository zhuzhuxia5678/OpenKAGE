"""工具基类与注册表。裁剪自 backend/agent_core/tool/（见上级 __init__ 的说明）。

只要 base + registry —— OpenKAGE的工具都是自己写的窄工具
（openkage/memory/tools.py 的记忆 CRUD、domains/*/ 的域工具），
不需要 backend 那十几个内置工具（filesystem / shell / web / 图像生成 …）。
"""

from .base import BaseTool
from .registry import ToolRegistry

__all__ = ["BaseTool", "ToolRegistry"]
