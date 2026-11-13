"""Agent 上下文留痕：**把真正发给 LLM 的那份 messages 落盘**（框架层，唯一写盘入口）。

    from openkage.agent_io import ContextDumper

    dumper = ContextDumper(run_dir / "contexts", "reflect_alice", kind="reflect")
    ctx_builder = SimpleContextBuilder(system_prompt, on_build=dumper)   # 挂上就行

三个 Agent 共用这一份：minecraft 的 ReAct（一天一个文件）、副脑反思、主脑聚合。

## 为什么必须落盘（不是"顺手存一份"）

这份内容**是复现所需的全部信息**：system prompt、前置的记忆块/图谱全景/已用词表、
每一步的观察与工具返回、累积对话。诊断"哪一步开始跑偏""记忆到底喂了什么进去"只能靠它。

之前只有 minecraft ReAct 有，而且**藏在一个默认关闭的 CLI flag 后面**（`--dump-step-inputs`）
—— 结果 2026-08-10 那轮 30 夜实验跑到第 7 天才发现一个字都没存下来。反思与聚合更是从来没有过：
它们各自是一次几十步的 ReAct、每次几十 KB 上下文，跑完只在日志里留一句话总结。
所以现在：**框架层提供、三个 Agent 都接、默认开**。

## 为什么不打进日志

完整 messages 动辄几十 KB（system prompt 一项就 ~10KB）。混进 `logs/*.log` 会把真正要看的
进度行冲得没法读 —— 而那份日志是判断 run 健康度的唯一入口。

## 写盘策略：一次运行一个文件，**每次覆盖**

ReAct/反思/聚合的一次运行都是**一段连续对话**（history 只增不减），后一次 LLM 调用的
messages 天然包含前一次的全部内容。按调用次数一份份存会有大量冗余（末次已含全部历史），
拆开还看不到全貌。故覆盖同一个文件：跑完它就是这次运行的完整对话。
覆盖会丢掉"调了多少次 LLM"，所以 `n_llm_calls` 显式记进 payload。

## 绝不能影响主流程

任何异常只**警告一次**（`_warned` 去重），绝不抛出。留痕是附带产物 ——
为它中断一条几十分钟的轨迹、或让一次已经烧掉 10 分钟的聚合失败，都不值得。
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def sanitize_tag(s: str) -> str:
    """把任意标识压成安全的文件名片段（字母/数字/下划线/连字符/中文，其余折成 `_`）。

    与 `domains/minecraft/__init__.py` 的 `_sanitize_tag` 同一套判据 —— 那边是域私有的
    人设标签，这里是框架层的通用版本，供 `ContextDumper` 自己拼文件名用。
    """
    return re.sub(r"[^0-9A-Za-z_一-鿿-]+", "_", s).strip("_") or "x"


class ContextDumper:
    """可调用对象：`dumper(messages)` 就把这份 messages 写进 `<out_dir>/<tag>.json`。

    直接当 `SimpleContextBuilder(..., on_build=dumper)` 的回调用 —— 挂在那里的理由很实：
    **那是压缩之后、请求之前的最后一站**，存下来的正是真正发出去的东西。
    挂在别处（比如 ReActAgent 里）会存到未压缩的版本，那不是模型看到的内容。

    `tool_defs_provider`：可选的**零参可调用**，返回工具定义（`ToolRegistry.get_definitions()`）。
    为什么是 provider 而不是直接传值：工具 schema 里的 `examples` 是**建工具时按库里已有
    kind/relation 算出来的**（见 `memory/tools.py` 的 `_kind_enum`），传值会把某一刻的快照
    钉死；而它每次运行都可能不同，那正是"这次模型看到的可选项有哪些"这个要看的信息。
    """

    def __init__(self, out_dir: "str | Path", tag: str, *, kind: str = "",
                 model_name: str | None = None, tool_defs_provider: Any = None,
                 extra: "dict | None" = None) -> None:
        self.out_dir = Path(out_dir)
        self.tag = sanitize_tag(tag)
        self.kind = kind
        self.model_name = model_name
        self._tool_defs_provider = tool_defs_provider
        self.extra = dict(extra or {})
        #: 本 dumper 已被调用几次 = 这次运行调了多少次 LLM（覆盖写会丢掉它，故显式记）
        self.n_calls = 0
        #: 只警告一次 —— 写盘失败通常是路径/权限问题，会**每次调用都失败**，
        #: 不去重会在一次运行里刷出几十条同样的 warning，把日志冲烂
        #: （而这个类存在的理由之一就是别冲烂日志）。
        self._warned = False

    def __call__(self, messages: list[dict]) -> None:
        self.n_calls += 1
        try:
            self.out_dir.mkdir(parents=True, exist_ok=True)
            payload: dict[str, Any] = {
                "tag": self.tag,
                "kind": self.kind,
                "model": self.model_name,
                "n_llm_calls": self.n_calls,
                # 按 role 列一份便于肉眼扫（谁在说话、多少轮），同时保留原始 messages 供程序复现
                "n_messages": len(messages),
                "roles": [m.get("role") for m in messages],
                "messages": messages,
                **self.extra,
            }
            if self._tool_defs_provider is not None:
                payload["tool"] = self._tool_defs_provider()
            (self.out_dir / f"{self.tag}.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:  # pylint: disable=broad-except
            if not self._warned:
                self._warned = True
                logger.warning(
                    "上下文留痕写盘失败（%s，**不影响主流程**，后续同类失败不再重复告警）",
                    self.out_dir / f"{self.tag}.json", exc_info=False)


def dump_reasonings(
    out_dir: "str | Path",
    tag: str,
    reasonings: list[dict],
    *,
    model_name: str | None = None,
    extra: "dict | None" = None,
    round_key: "str | None" = None,
) -> None:
    """把一次 ReAct 运行各步的 reasoning（thinking 模型思维链）落盘到 `<tag>_reasoning.json`。

    与 `ContextDumper` 的关系：ContextDumper 落盘**请求侧** messages（发给 LLM 的，覆盖写），
    本函数落盘**响应侧** reasoning（LLM 想了什么，跑完整轮一次性写）。两者同目录、同 tag 前缀、
    互不干扰：复盘时 `<tag>.json` 看发了什么、`<tag>_reasoning.json` 看想了什么。

    ★ **reasoning 绝不进上下文**（2026-09-16 用户硬约束）：本函数只把 reasoning 写进独立文件，
    不触碰 history / messages / 任何回灌路径。reasoning 由 ReActAgent 收集进
    `AgentResult.metadata["reasonings"]`（见 react.py），调用方跑完取出传给本函数。

    `reasonings` 是 `[{"step": 1, "reasoning": "..."}, ...]`，空串 reasoning 仍记一条占位以保持
    步号对齐。写盘失败只警告、不打断主流程（同 ContextDumper 纪律）。

    ★ `round_key`（续跑多轮用，任务域专属）：不传=覆盖写（reflect/consolidate 每夜每窗口只跑
      一轮，无续跑）。传了=读回已有文件、把本轮 reasonings 作为 `rounds[round_key]` 追加进
      `rounds` 字典、写回。任务域同一天可能续跑多次（STOP 但天没亮 → 重开一轮），每轮一个
      round_key（如 "r0"/"r1"/"r2"），写到同一个 `<tag>_reasoning.json`，避免覆盖丢前几轮思维链。
      读回解析失败（文件损坏/非 JSON）= 当作空 rounds 重新开始，不抛。
    """
    out = Path(out_dir)
    path = out / f"{sanitize_tag(tag)}_reasoning.json"
    try:
        out.mkdir(parents=True, exist_ok=True)
        if round_key is not None:
            # 读回-合并-写回：续跑多轮追加进 rounds 字典
            rounds: dict[str, list] = {}
            base_payload: dict[str, Any] = {
                "tag": tag, "model": model_name, "rounds": rounds,
            }
            if extra:
                base_payload.update(extra)
            if path.exists():
                try:
                    prev = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(prev, dict) and isinstance(prev.get("rounds"), dict):
                        rounds.update(prev["rounds"])
                        # 保留旧文件里的 tag/model/extra 元信息
                        for k in ("tag", "model"):
                            if k in prev and base_payload.get(k) is None:
                                base_payload[k] = prev[k]
                except Exception:  # pylint: disable=broad-except
                    rounds = {}  # 损坏则从头来，不抛
            rounds[round_key] = reasonings
            all_reasonings = [r for rk in sorted(rounds) for r in rounds[rk]]
            payload = {**base_payload, "rounds": rounds,
                       "n_steps": sum(len(v) for v in rounds.values()),
                       "n_reasoning_steps": sum(1 for r in all_reasonings if r.get("reasoning"))}
        else:
            payload: dict[str, Any] = {
                "tag": tag,
                "model": model_name,
                "n_steps": len(reasonings),
                # 非空 reasoning 步数：一眼看出"这次运行到底有没有在思考"
                "n_reasoning_steps": sum(1 for r in reasonings if r.get("reasoning")),
                "reasonings": reasonings,
            }
            if extra:
                payload.update(extra)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:  # pylint: disable=broad-except
        logger.warning(
            "reasoning 落盘失败（%s，**不影响主流程**）", path, exc_info=False)
