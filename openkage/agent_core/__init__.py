"""OpenKAGE自带的 agent 底座 —— 从 `backend/agent_core/` **裁剪复制**而来（2026-08-02）。

# 为什么复制而不是继续 import backend/

原先OpenKAGE直接 `from agent_core.agent.react import ReActAgent`，靠 `openkage/llm.py` 把
`backend/` 塞进 sys.path 实现（见那个文件的注释）。但要给 ReActAgent 加 `should_stop` 钩子
（每天一轮 ReAct、天亮时从外部中止循环）就得**改 react.py**，而：

- `backend/agent_core/` 是**宿主项目的生产代码**，被 git 跟踪，且有 **12 处**生产
  调用点在用 ReActAgent（chat_agent / tutor / immersive / md_author / daily_brief …）。
  为跑实验去改它，风险落在生产链路上。
- `OpenKAGE/` 本身**不在 git 里**，是独立演进的实验仓（日后发 PyPI + 发论文），
  本就不该依赖同仓另一个项目的内部包。

故复制**最小闭包**（8 个文件 / 约 2119 行），只保留 ReAct 这条链真正用到的东西：

    agent/react.py  base.py  events.py
    context/base.py  simple.py
    llm/base.py
    tool/base.py  registry.py

**刻意没复制**的：agent 的 plan_and_solve / reflection（另两种范式，OpenKAGE不用）、context 的
skill_prompt / profile_skill（生产侧技能与画像上下文）、tool 下十几个内置工具
（filesystem / shell / web / 各种图像生成…）、llm/openai_llm（LLM 走 openkage/llm.py 的 OpenAICompatLLM）。

★ 与 backend 版本的**唯一差异**是 react.py 新增的 `should_stop` 钩子；其余文件逐字未改，
  便于日后与上游对比：`diff -r backend/agent_core OpenKAGE/openkage/agent_core`。
"""

__version__ = "0.1.0"

from .agent.base import AgentResult, BaseAgent
from .agent.events import AgentFinishReason
from .agent.react import ReActAgent
from .context.base import BaseContextBuilder
from .context.simple import SimpleContextBuilder
from .llm.base import BaseLLM, LLMResponse, MessageRole
from .tool.base import BaseTool
from .tool.registry import ToolRegistry

__all__ = [
    "AgentResult",
    "BaseAgent",
    "AgentFinishReason",
    "ReActAgent",
    "BaseContextBuilder",
    "SimpleContextBuilder",
    "BaseLLM",
    "LLMResponse",
    "MessageRole",
    "BaseTool",
    "ToolRegistry",
]
