"""OpenKAGE: federated graph memory for capability growth and emergent self-awareness.

框架核心（可独立发布）——只含记忆引擎 + 联邦编排 + 门面 + 任务域**契约**，
**不 import 任何具体任务域**（框架不认识自己的插件）：

    from openkage import OpenKAGE, Arm, TaskDomain

    # 任务域实现是外部插件包 domains（依赖 openkage，反向不成立）：
    from domains import build_domain
    domain = build_domain("math", ...)          # math / minecraft
    octo = OpenKAGE(brain_config=..., arms=[...], domain=domain, ...)
    train, eval_tasks = domain.build_tasks(cfg)
    octo.run(train, epochs=3, eval_tasks=eval_tasks)

底层记忆原语（给自定义 Agent 挂载图记忆）：

    from openkage import BrainGraph

日志归属（把 frozen/evolving/各副脑的日志分开）：runner 给 handler 装上 ScopeFormatter，
框架内部用 log_scope 标注归属，任意深度（含 httpx 等第三方库）的日志都会自动带上行首标签：

    from openkage import ScopeFormatter, log_scope

自定义任务域：实现 openkage.domain.TaskDomain 协议即可接入框架，无需改框架任何代码。
"""

from openkage.brain import OpenKAGE, Arm
from openkage.domain import Task, Sample, EvalScore, TaskDomain
from openkage.logctx import ScopeFormatter, log_scope, current_scope
from openkage.memory.graph import BrainGraph

__all__ = [
    "OpenKAGE", "Arm", "BrainGraph",
    "Task", "Sample", "EvalScore", "TaskDomain",
    "ScopeFormatter", "log_scope", "current_scope",
]
