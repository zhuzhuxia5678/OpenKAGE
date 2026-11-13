"""domains —— 任务域插件包（依赖框架 openkage，反向不成立）。

框架 openkage/ 只定义契约 openkage.domain.TaskDomain，不 import 本包任何东西；本包每个域实现
该契约，依赖 openkage.rollout / openkage.memory / openkage.llm 等框架能力。runner 按 --domain
到这里 build_domain 拿实例。

本包提供 build_domain（按域名构造 TaskDomain 实例，构造参数因域而异）和 load_domain_prompt
（各域 system prompt 的统一加载器）。
"""

from __future__ import annotations

from pathlib import Path

from openkage.domain import TaskDomain

_DOMAINS_DIR = Path(__file__).resolve().parent


def build_domain(name: str, **kwargs) -> TaskDomain:
    """按域名构造一个 TaskDomain 实例。kwargs 透传给该域构造函数（因域而异）。

    子域在函数内延迟 import（而非模块顶部）：各域 `__init__` 会回头
    `from domains import load_domain_prompt` 读自己的 prompt，顶部 import 会形成循环。
    """
    if name == "math":
        from domains.math import MathMCQDomain
        return MathMCQDomain()
    if name == "minecraft":
        from domains.minecraft import MinecraftDomain
        return MinecraftDomain(**kwargs)
    raise ValueError(f"未知任务域：{name!r}（可选 math / minecraft）")


def load_domain_prompt(domain: str, name: str = "system",
                       subs: "dict[str, object] | None" = None) -> str:
    """读 `domains/<domain>/prompts/<name>.md` 作为该域的 prompt（域模块加载时调用一次）。

    约定：用本函数的域把 system prompt 以 md 独立存储在 `domains/<域>/prompts/system.md`。
    与框架级 `prompts/`（`memory/prompt_store.py`）分工：那边放领域无关的记忆反思/聚合
    Agent prompt，这边放域私有的任务规则，两者不混。抽成独立 md 而不内嵌成 Python 字符串，
    是为了这些 prompt 能反复审阅、迭代措辞，而不必在字符串拼接里数引号。

    做两件事：

    ① 剥掉开头的编者引言：md 顶部用 `> ` 引用块写文档维护说明，一级标题（`# xxx`）与其后的
       `---` 分隔线同样剥掉。规则：从文件开头起，连续的空行 / `> …` / `---` / `# …`
       全部跳过，遇到第一行正文即停。

    ② 按字面替换占位符（`subs`），如时限 `{default_sec}`。不用 str.format——
       md 里含 JS/JSON 代码块（`const { Vec3 } = …`），format 会把那些花括号当字段名
       误处理。字面 replace 只动显式列出的键，代码块不受影响。
    """
    path = _DOMAINS_DIR / domain / "prompts" / f"{name}.md"
    lines = path.read_text(encoding="utf-8").splitlines()
    start = 0
    for i, ln in enumerate(lines):
        s = ln.strip()
        if s == "" or s.startswith(">") or s == "---" or s.startswith("# "):
            start = i + 1
        else:
            break
    body = "\n".join(lines[start:]).strip()
    # 校验 subs 里每个占位符都在正文出现，避免占位符被误删后 sub 静默不生效
    missing = [k for k in (subs or {}) if "{" + k + "}" not in body]
    if missing:
        raise ValueError(
            f"domains/{domain}/prompts/{name}.md：传了 subs {missing} 但正文里没有对应占位符"
            f"（形如 `{{{missing[0]}}}`）。要么是占位符被误删，要么是这个 sub 已经不需要了 ——"
            f"两种情况都得改，别留一个算了却没用上的 sub"
        )
    for key, val in (subs or {}).items():
        body = body.replace("{" + key + "}", str(val))
    return body


DOMAIN_NAMES = ("math", "minecraft")
