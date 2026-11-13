"""域无关的 rollout 模板（供各任务域跑「一次采样」复用）。

「拼 messages → 带工具调 LLM（含重试/兜底）→（多步则）把动作追加回对话」这套骨架对各任务域
**同构**，差异只在：system prompt、动作工具 schema、怎么从 resp 里取动作、以及「单步提交」还是
「多步 obs→action 循环」。本模块把同构部分收成两个 helper，各域（domains/*）在自己的 rollout 里调：

- `build_context_messages`：副脑角色人设 + system prompt + 主脑/副脑记忆检索上下文，拼成 base messages。
  检索只依赖 query，与采样随机性/角色无关 → 每 task 只做一次，复用给 N 次并行采样。
- `call_tool_once`：给定 messages + 一个工具，强制 function-calling 调一次 LLM，含重试/退避；
  返回工具参数 dict（失败返回 None）。单步域直接用它取结果；多步域在自己的 env 循环里每步用它选动作。

设计约束：不依赖任何 backend 业务模块；LLM 走 openkage.llm.get_llm（自带 max_tokens 上限与重试）。
"""

from __future__ import annotations

import asyncio
import collections
import logging

from openkage.config import LLMConfig
from openkage.llm import get_llm

logger = logging.getLogger("rollout")

# 这里的 delay 只用于「重试之间的退避」。
#
# ★ 两个值都来自 yml（`llm.rollout.call_delay` / `llm.max_tokens.answer`），模块级读一次。
#   仍以模块常量的形式导出，是因为 domains/minecraft 直接 import CALL_DELAY_SECONDS 复用同一个
#   退避节奏 —— 那边是**线性**退避（delay × 次数），本模块的 call_tool_once 是**指数**
#   （delay × 2^(n-1)）；两者共用基数、不共用曲线。
CALL_DELAY_SECONDS = LLMConfig.load().rollout_call_delay
MAX_ANSWER_TOKENS = LLMConfig.load().max_tokens_answer

#: 本进程是否还该尝试**强制**指定函数的 tool_choice。首次被网关以 400 拒掉后置 False，
#: 之后一律用 "auto"。见 `call_tool_once` 的 docstring（thinking 类模型不支持强制）。
#: 进程级而非按模型名缓存：一个 run 只跑一个模型，多一层字典只是多一处状态。
_FORCED_TOOL_CHOICE_OK = True


def summarize_mem(mem_nodes: list[dict], mem_context: str | None) -> str:
    """把一次图谱检索命中量化成一行（概念数/切面数/各 kind 分布/字数），供各域检索日志复用。
    主脑/副脑图谱同结构，共用这个摘要。全文仍留在结果 JSON 的逐 task 留痕里。"""
    if not mem_nodes:
        return "命中0"
    n_concepts = len({f["concept_slug"] for f in mem_nodes})
    kind_str = " ".join(
        f"{k}:{v}" for k, v in sorted(collections.Counter(f["kind"] for f in mem_nodes).items())
    )
    return f"概念{n_concepts} 切面{len(mem_nodes)}（{kind_str}）共{len(mem_context or '')}字"


def build_context_messages(
    query: str,
    *,
    system_prompt: str,
    primary_memory=None,
    secondary_memory=None,
    arm_role: str | None = None,
    agent_name: str | None = None,
    primary_prefix: str = "",
    secondary_prefix: str = "",
    log_tag: str = "记忆检索",
    task_id: str = "?",
) -> tuple[list[dict], str | None, str | None]:
    """检索 + 拼 base messages（一次性），返回 (messages, primary_ctx, secondary_ctx)。

    上下文顺序：system(副脑角色人设) → system(域 system_prompt) → 主脑ctx(公共先验) →
    副脑ctx(该副脑个人经验)。**人设与域规则是两条独立 system**（便于留痕里分辨来源；发送前
    由 llm._merge_system_messages 合并成一条，对模型等价，见下）。
    单步域随后 append user(任务)；多步域随后在 env 循环里 append user(obs)/assistant(action)。
    primary_memory/secondary_memory 分别是主脑/副脑图谱，传 None 则跳过该级检索
    （eval 侧只传 primary_memory → 只读主脑）。

    primary_prefix/secondary_prefix：交互/探索类域会给记忆块加「过往经验（公共/自己）：」这类前缀，
    单步域可传空串（记忆上下文不加前缀）。
    """
    if primary_memory is not None:
        p_nodes = primary_memory.retrieve(query)
        primary_ctx = primary_memory.render_context(p_nodes)
    else:
        p_nodes, primary_ctx = [], None
    if secondary_memory is not None:
        s_nodes = secondary_memory.retrieve(query)
        secondary_ctx = secondary_memory.render_context(s_nodes)
    else:
        s_nodes, secondary_ctx = [], None

    # 人设与域规则**拆成两条 system**：二者来源与生命周期不同 ——
    # 人设来自 arms/<domain>/*.md（每个副脑一份），域规则来自 domain 的
    # _SYSTEM_PROMPT（全副脑共用）。
    # 发送前 llm._merge_system_messages 会把所有 system 用 "\n\n".join 合并成一条
    # （部分提供方只接受一条 system）。**正因为最终会合并**，两段之间必须自带
    # 标题行标明层级：否则人设与域规则**都以「你是一个…」开头、只隔一个空行**，模型读到的
    # 是连着两次「你是谁」，分不出哪句是全副脑共用的硬规则、哪句是本副脑独有的性格。
    # 标题用「探索者/解题者」这类域中性词不合适（math 是解题、minecraft 是探索），故此处
    # 只说「你的角色人设」——各域再在自己的 _SYSTEM_PROMPT 标题里点明本域共同规则的范围。
    # agent_name：本副脑在环境里的**身份标识**（minecraft 用它当游戏内用户名，取自 arm key）。
    # 放在人设那条 system 的开头 —— 身份与性格同属"这个副脑是谁"，且都是 per-arm 的实验变量。
    # 域中性写法：框架不知道这个名字在各域怎么用（minecraft 是玩家名，别的域可能只是代号）。
    messages: list[dict] = []
    if arm_role or agent_name:
        parts = []
        if agent_name:
            parts.append(f"你的名字是 **{agent_name}**。")
        if arm_role:
            parts.append(arm_role.strip())
        messages.append({"role": "system",
                         "content": "【你的角色人设】\n" + "\n\n".join(parts)})
    messages.append({"role": "system", "content": system_prompt})
    if primary_ctx:
        messages.append({"role": "system", "content": f"{primary_prefix}{primary_ctx}"})
    if secondary_ctx:
        messages.append({"role": "system", "content": f"{secondary_prefix}{secondary_ctx}"})

    if primary_memory is not None:
        logger.info("[%s主脑] task=%s %s", log_tag, task_id, summarize_mem(p_nodes, primary_ctx))
    if secondary_memory is not None:
        logger.info("[%s副脑] task=%s %s", log_tag, task_id, summarize_mem(s_nodes, secondary_ctx))
    return messages, primary_ctx, secondary_ctx


def retrieve_memory_block(
    query: str,
    *,
    primary_memory=None,
    secondary_memory=None,
    primary_prefix: str = "",
    secondary_prefix: str = "",
    log_tag: str = "记忆检索",
    task_id: str = "?",
) -> str | None:
    """每步重检主+副脑，把两级检索结果渲染成**一条** system 消息内容（供多步域每步刷新记忆块）。

    与 build_context_messages 同一套 retrieve/render，但：① 不含 system_prompt/角色人设（那是固定
    base，不随步刷）；② 主+副脑两块拼成单条返回（调用方每步删旧块、append 这条新块，只留最新一块，
    见 M2.6「白天累积对话 + 每步重检刷记忆块」）。两级都命中空则返回 None（该步不挂记忆块）。
    query 用当前观察摘要 → 记忆随处境动态贴合（对标 Voyager 每 iter 重检 skill）。
    """
    parts: list[str] = []
    if primary_memory is not None:
        p_nodes = primary_memory.retrieve(query)
        p_ctx = primary_memory.render_context(p_nodes)
        logger.info("[%s主脑] task=%s %s", log_tag, task_id, summarize_mem(p_nodes, p_ctx))
        if p_ctx:
            parts.append(f"{primary_prefix}{p_ctx}")
    if secondary_memory is not None:
        s_nodes = secondary_memory.retrieve(query)
        s_ctx = secondary_memory.render_context(s_nodes)
        logger.info("[%s副脑] task=%s %s", log_tag, task_id, summarize_mem(s_nodes, s_ctx))
        if s_ctx:
            parts.append(f"{secondary_prefix}{s_ctx}")
    return "\n\n".join(parts) if parts else None


async def call_tool_once(
    messages: list[dict],
    tool: dict,
    tool_name: str,
    model_name: str | None,
    *,
    max_attempts: int | None = None,
    max_tokens: int = MAX_ANSWER_TOKENS,
) -> dict | None:
    """强制 function-calling 调一次 LLM（含重试/指数退避），返回工具参数 dict；全部失败返回 None。

    用 tool_choice 强制模型走 function calling 输出结构化结果，而不是从自由文本里正则抠答案
    （推导很长时易被截断/格式对不齐抠不出）。单步域拿参数直接用；多步域拿参数里的动作/代码字段。

    ★ **强制模式不是所有模型都支持**（2026-08-14 加降级）：thinking 类模型（实测
    `deepseek-v4-flash-202607`）对 `tool_choice={"type":"function",...}` 直接回
    HTTP 400 `Thinking mode does not support this tool_choice`，而这类模型在
    `tool_choice="auto"` 下**照样乖乖调工具**（实测同一 prompt 返回
    finish_reason=tool_calls + 完整 arguments）。

    ⚠️ 这个 400 **不会以异常形式到达这里**：`OpenAICompatLLM` 的重试层把耗尽后的异常收敛成
    `LLMResponse(finish_reason="error", content=f"Error: {exc}")`（见其 `think`）。
    所以降级判据必须**认 content 里的字符串**，不能 try/except —— 按异常写过一版，
    实测三次重试全部撞同一个 400、开关一次都没翻（重试还白烧了两次请求）。

    首次识别出这个 400 就置位模块级开关，之后同进程内不再尝试强制。
    **不做静态模型白名单**：网关随时上新模型，白名单必然过期，而"试一次、被拒就记住"
    对任何模型都自动正确。降级后**保留** `not resp.tool_calls` 那条重试判据 ——
    auto 模式下模型有权不调工具，那条判据从"几乎不可能触发"变成真正的兜底。

    `max_attempts` 不传 = yml 的 `llm.rollout.attempts`。这是三层嵌套重试的第 ③ 层
    （内层还有网关畸形响应重试与 SDK 的 HTTP 重试），乘积见 `LLMConfig.worst_case_attempts`。
    """
    global _FORCED_TOOL_CHOICE_OK  # noqa: PLW0603  进程级能力探测缓存，见上方 docstring
    if max_attempts is None:
        max_attempts = LLMConfig.load().rollout_attempts
    last_reason = ""
    attempt = 0
    while attempt < max_attempts:
        attempt += 1
        if _FORCED_TOOL_CHOICE_OK:
            tool_choice: object = {"type": "function", "function": {"name": tool_name}}
        else:
            tool_choice = "auto"
        resp = await get_llm(model_name, role="task").async_think(
            messages=messages, tools=[tool], tool_choice=tool_choice,
            config={"max_tokens": max_tokens},
        )
        if (
            _FORCED_TOOL_CHOICE_OK
            and resp.finish_reason == "error"
            and "tool_choice" in (resp.content or "")
        ):
            # 本模型不支持强制指定函数 → 永久降级为 auto，且**这次不算一次尝试**
            # （请求根本没到模型，不该消耗重试预算，也不该退避等待）。
            _FORCED_TOOL_CHOICE_OK = False
            attempt -= 1
            logger.warning(
                "模型不支持强制 tool_choice → 本进程后续一律用 auto 模式：%s",
                (resp.content or "")[:160],
            )
            continue
        if resp.finish_reason == "error":
            last_reason = f"网关错误: {(resp.content or '')[:80]!r}"
        elif resp.finish_reason == "length":
            last_reason = "输出过长被截断（finish_reason=length）"
        elif not resp.tool_calls:
            last_reason = f"未走 tool_call，content={(resp.content or '')[:80]!r}"
        else:
            return dict(resp.tool_calls[0].arguments)

        if attempt < max_attempts:
            logger.warning(
                "工具调用未拿到有效结果（第%d/%d次尝试），重试: %s", attempt, max_attempts, last_reason
            )
            await asyncio.sleep(CALL_DELAY_SECONDS * (2 ** (attempt - 1)))
    logger.warning("工具调用 %d 次后仍失败：%s", max_attempts, last_reason)
    return None
