"""Run the adapted Voyager baseline.

    python experiments/run_voyager.py --smoke
    python experiments/run_voyager.py --model <model-id> --tag voy1
    python experiments/run_voyager.py --resume <run.json>

A controlled re-implementation of Voyager's automatic curriculum, iterative
code generation, self-verification and skill library on the same Minecraft
world and bare action API as OpenKAGE. N isolated agents explore 30 game
days with no shared state; goals and metrics use the same protocol as the
main experiments so results are directly comparable.

Each run writes results/minecraft_<ts>_<host>_voyager_<model>/ with a result
JSON, world snapshots, per-night checkpoints and the agents' skill stores.
--model is required; --agents N sets the number of isolated agents (default 3).
The differences from the original Voyager system (single backbone, native
Mineflayer actions, day/night rhythm and checkpoint-based resumption) are
listed in the paper's appendix.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import signal
import socket
import sys
from datetime import datetime
from pathlib import Path

_EXPERIMENTS_DIR = Path(__file__).resolve().parent
_PKG_ROOT = _EXPERIMENTS_DIR.parent
_RESULTS_ROOT = (Path(os.environ["OPENKAGE_RESULTS_DIR"]).expanduser()
                 if os.environ.get("OPENKAGE_RESULTS_DIR") else _PKG_ROOT / "results")
for _p in (str(_PKG_ROOT), str(_EXPERIMENTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:  # same as run.py: repository-root .env, real environment variables win
    from dotenv import load_dotenv
    if (_PKG_ROOT / ".env").exists():
        load_dotenv(_PKG_ROOT / ".env", override=False)
except ImportError:
    pass

from openkage.logctx import ScopeFormatter  # noqa: E402  行首作用域列（各智能体并行可辨）
from domains import build_domain  # noqa: E402  世界工厂（MinecraftDomain）
from domains.minecraft import _snapshot_world  # noqa: E402  world 存档快照（复用，勿另写）
from openkage.orchestrate.checkpoint import (  # noqa: E402
    read_progress,
    rollback_to_window,
    write_progress,
)
from openkage.config import load_config  # noqa: E402  domain_scale（max_nights/tick_rate/种子）
from voyager.voyager_agent import VoyagerAgent  # noqa: E402

logger = logging.getLogger("run_voyager")

_RUN_TS = datetime.now().strftime("%Y%m%d_%H%M%S")
_RUN_HOST = socket.gethostname().split(".")[0]

#: 孤岛标签（与 arms/minecraft/*.md 的 key 同名）。⚠ 出生点口径（2026-10-02 对齐）：
#: 主表 OpenKAGE 走 --fixed-spawn（自然出生点），本入口 random_spawn=False 同口径 ⇒
#: 三孤岛与 OpenKAGE 三臂同 seed 同世界自然出生点。标签同名仍保出生点 RNG 流兼容
#: （若将来切 --no-fixed-spawn 即恢复同流同出生点序列）。
DEFAULT_AGENT_KEYS = ("alice", "bob", "carol", "dave", "erin")


# ═════════════════════════════════════════════════════════════════════════════
# Voyager 任务循环（react_day 回调形态——与 OpenKAGE 的 _make_react_day 同一挂点）
# ═════════════════════════════════════════════════════════════════════════════

def _make_voyager_day(agent: VoyagerAgent):
    """返回 `voyager_day(state, do_step, render_obs, get_events, resume)` 回调。

    与 env._react_day_loop 的契约：跑「一整天」的 Voyager 任务循环；天亮由 env 置
    `state["day_over"]`（should_stop 语义由返回值承担——本回调**顺序执行代码步**、
    不用 ReActAgent 的异步循环，天亮检测靠每次 do_step 返回后查 state）。

    任务状态机（每个任务一个 round_state dict，跨天持久——checkpoint 恢复后接着跑）：
        task            当前任务（None = 该向课程要新任务）
        round           已生成的代码轮数（≥4 放弃 → failed_tasks）
        last_code       上一轮代码（回灌）
        events          上一轮执行反馈（回灌）
        critique        上一轮 critic 判语（回灌）
        parse_fail_streak 连续解析失败计数（≥2 放弃）
    """
    # 跨天/跨段的任务状态（voyager_day 闭包持有，checkpoint 时由调用方经 state_dict 存取）。
    # ★ 从 checkpoint 恢复在 run_voyager_day 的调用方做（main 的 _resume 分支直接
    #   agent.load_state + 恢复 pending task dict）。
    task_state: dict = {"task": None, "context": "", "round": 0, "last_code": None,
                        "events": None, "critique": "", "parse_fail_streak": 0}

    async def voyager_day(state: dict, do_step, render_obs, get_events, resume: bool = False):
        # pylint: disable=unused-argument  render_obs 契约参数（OpenKAGE 用它渲染观察，Voyager
        # 的观察走 state_adapter.assemble_state_block——原文口径，见适配清单 4）
        """跑一整天：任务循环直到 env 置 day_over/run_over。"""
        from domains.minecraft import _STEP_TIMEOUT_DEFAULT_TICKS  # 500 刻默认时限

        def _over() -> bool:
            return bool(state.get("day_over") or state.get("run_over"))

        # 技能代码注入：检索 top-5 相关技能拼进 extra_programs（当天所有步共享——
        # 与 OpenKAGE 的代码层注入同通道 state["extra_programs"]，但**按当前任务**检索、
        # 换任务时刷新。原版是每轮代码生成后重检（query = context + summarize_chatlog）、
        # 把源码贴进 system prompt；我们走 programs 注入通道，query 构成照原码（见
        # retrieve_skills docstring），刷新时机降为「每任务」（差异记适配清单）。
        # 技能代码不占 LLM 上下文，且复用 env 的埋点链路（onSkill 计数——经
        # _wrap_skill_for_injection 包装，与 OpenKAGE 同款代理）。
        # ★ env 在天亮时清空 extra_programs/skill_sources（night_cb 尾部），故每天首个
        #   任务都会重新注入（下面 ① 分支每次换任务都调，覆盖了「新一天恢复当天首注入」）。
        from domains.minecraft import _wrap_skill_for_injection

        def _refresh_skill_injection(task: str, context: str,
                                     events=None) -> None:
            hits = agent.retrieve_skills(task, events=events, context=context)
            code_parts = []
            for sk in hits:
                js = sk.get("js_code") or ""
                if not js:
                    continue
                code_parts.append(_wrap_skill_for_injection(sk))
            state["extra_programs"] = "\n".join(code_parts)
            state["skill_sources"] = {sk["slug"]: "secondary" for sk in hits if sk.get("slug")}
            if hits:
                logger.info("[voyager:%s] 技能注入 %d 个（task=%r）", agent.name, len(hits),
                            task[:40])

        # 连续「任务零执行就放弃」计数（熔断）：课程出任务 → run_task_step 立刻 None
        # （轮数耗尽外的另一形态：LLM 网关挂/连续解析失败）→ 不执行任何 do_step → env 的
        # idle guard 触发守夜。这里先熔断：连续 3 个任务零执行就主动歇到天亮（让 idle
        # guard 的 waitUntil 自然接手），别在坏网关上无限烧 LLM 调用。
        zero_exec_streak = 0
        _MAX_ZERO_EXEC_STREAK = 3

        while not _over():
            # ── ① 无当前任务 → 向课程要一个 ──
            if not task_state["task"]:
                obs = state["obs"]
                proposed = await agent.propose_next_task(obs)
                if proposed is None:
                    # 课程连续失败兜底：用上次的任务重试（agent.propose_next_task 内部已
                    # 尝试「重试上一任务」；这里再兜一个固定首任务，保证循环不空转——
                    # env 的 idle guard 会强制守夜等天亮，白扔一整天）。
                    proposed = ("Obtain a wood log", "")
                    logger.warning("[voyager:%s] 课程两次未出任务，兜底任务 %r",
                                   agent.name, proposed[0])
                task, context = proposed
                task_state.update(task=task, context=context, round=0, last_code=None,
                                  events=None, critique="", parse_fail_streak=0)
                _refresh_skill_injection(task, context)
                logger.info("[voyager:%s] 新任务（完成%d/失败%d）：%s", agent.name,
                            len(agent.completed_tasks), len(agent.failed_tasks), task)

            # ── ② 生成下一轮代码（agent.run_task_step 内含轮数上限判定）──
            step = await agent.run_task_step(task_state["task"], state["obs"], task_state)
            if step is None:
                # 轮数用尽/解析连败 → 放弃本任务，记 failed（原文 "too hard" 语义）
                agent.mark_task(task_state["task"], success=False)
                logger.info("[voyager:%s] 任务放弃（%d 轮未过验证）：%s", agent.name,
                            task_state["round"], task_state["task"])
                task_state["task"] = None
                zero_exec_streak += 1
                if zero_exec_streak >= _MAX_ZERO_EXEC_STREAK:
                    # 熔断：连续多个任务一步没执行（LLM 侧坏了），等天亮重开。
                    # 不 raise——天还要照常过（checkpoint/日界在 env 侧），只是今天不再出任务。
                    logger.error("[voyager:%s] 连续 %d 个任务零执行（LLM 侧异常？）——"
                                 "本日熔断，等天亮", agent.name, zero_exec_streak)
                    await do_step(
                        "await waitUntil(bot, 3000, '任务循环熔断守夜', ['dawn']);",
                        3000, "熔断守夜")
                    break
                continue
            zero_exec_streak = 0

            # ── ③ 执行（do_step = env 的 _do_one_step：时间栅栏/记账/反馈全复用）──
            # ★ _ensure_invocation：Voyager 响应格式是「只定义函数」，本仓 eval 不会自动调
            #   （smoke 实抓，见其 docstring）——垫上调用行再执行。
            feedback = await do_step(_ensure_invocation(step["code"]),
                                     _STEP_TIMEOUT_DEFAULT_TICKS,
                                     f"voyager task: {task_state['task']}")
            if _over():
                # 天亮恰好落在本步：反馈已拿到但天结束了——把 events 留在 task_state 里
                # （checkpoint 带走），次日带着这轮反馈继续。任务本身不判成败。
                task_state["events"] = _events_from_feedback(feedback, get_events())
                break
            # 执行反馈 → events（onError=执行错误，其余=chat log；原文两类都回灌）
            task_state["events"] = _events_from_feedback(feedback, get_events())

            # ── ④ 自验证 ──
            # ★ 原码 critic.py:39-42：本步有 onError（执行报错）时 critic **不调 LLM 直接判失败**
            #   （代码都没跑成，无需验证）。这个短路是原版行为，照抄。
            _has_err = any(k == "onError" for k, _ in task_state["events"] or [])
            if _has_err:
                success, critique = False, ""
            else:
                verdict = await agent.check_task_success(
                    task_state["task"], state["obs"],
                    context=task_state.get("context", ""))
                if verdict is None:
                    # critic 解析失败：按原文语义算未通过、critique 提示重试
                    verdict = (False, "Self-verification unavailable; check the task again.")
                success, critique = verdict
            task_state["critique"] = critique
            if success:
                agent.mark_task(task_state["task"], success=True)
                logger.info("[voyager:%s] 任务达成（第%d轮）：%s", agent.name,
                            task_state["round"], task_state["task"])
                # 技能入库（失败不阻断——见 add_skill docstring）
                await agent.add_skill(step["code"], at=int(state.get("step_idx", 0)))
                task_state["task"] = None
            else:
                logger.info("[voyager:%s] 验证未过（第%d轮）：%s | critique=%s", agent.name,
                            task_state["round"], task_state["task"], critique[:80])
        return "DAY_OVER"

    return voyager_day, task_state


def _ensure_invocation(code: str) -> str:
    """「只定义了 async function、没调用」的代码补上调用行（纯函数，单测覆盖）。

    ★ smoke 实抓（2026-10-01）：Voyager 原文响应格式就是「定义 async function NAME(bot)」
      （Prompt 4 的 RESPONSE FORMAT），官方实现的服务器侧会自动调用；而本仓 eval 包装是
      `await (async () => { <code> })()`——函数**只定义不调用**，整步 1 秒静默返回、
      无任何执行反馈（连续 4 轮白烧）。OpenKAGE 无此问题：它的 code 是「包进函数体执行的
      片段」（react.yaml write_code.code 明写），不含函数定义。

    修法在**调用侧垫一层**（不改 Node/env——它们是全表共用的床）：检测「代码只定义了
    `async function NAME(bot)` 且正文没有 NAME 的调用/引用」，末尾补 `\nawait NAME(bot);`。
    已有顶层语句或已自我调用的代码原样返回（幂等）。
    """
    import re
    m = re.search(r"async\s+function\s+([A-Za-z_$][\w$]*)\s*\(\s*bot\s*\)", code or "")
    if not m:
        return code  # 没有顶层 async function NAME(bot) 定义——照原样（片段式代码）
    name = m.group(1)
    # 除定义行外的引用（调用/递归/赋值都算已用）
    body_wo_def = (code or "").replace(m.group(0), "", 1)
    if re.search(rf"\b{re.escape(name)}\b", body_wo_def):
        return code  # 正文已引用（自我调用/互相调用）——不重复补
    return f"{code}\nawait {name}(bot);"


def _events_from_feedback(feedback: str, raw_events) -> "list[tuple[str, str]] | None":
    """do_step 返回 + env 的 last_events → 回灌 events 列表。

    raw_events（get_events()）是 env 侧的 [(kind, text), …]（onChat/onLog/onError），
    优先用它（结构化）；异常/为空时从 feedback 文本降级抽（纯文本兜底，不丢反馈）。
    """
    try:
        evs = list(raw_events or [])
        if evs:
            return evs
    except Exception:  # pylint: disable=broad-except
        pass
    if not feedback:
        return None
    return [("onLog", feedback[:2000])]


# ═════════════════════════════════════════════════════════════════════════════
# 单个孤岛的整轮探索（借 worker + 世界恢复 + checkpoint）
# ═════════════════════════════════════════════════════════════════════════════

#: 续跑段世界恢复的串行锁：_fresh_world 的 resume 分支读 domain._resume_window
#: （实例字段，非 per-arm）——多孤岛并行时会互相覆盖窗口号（A 设 3、B 设 5，A 恢复成 B 的
#: 窗口 = 错配）。锁内「设窗口号 + _fresh_world」原子化；代价只是各续跑段起跑串行
#: （~30s/孤岛），轨迹本身照常并行。新跑段（不碰 _resume_window）无需锁，但不分流——
#: 统一走锁，简单优先（起跑串行 vs 17h 轨迹可忽略）。
_FRESH_WORLD_LOCK = asyncio.Lock()

async def run_one_voyager(domain, agent: VoyagerAgent, skill_dir: Path,
                          checkpoint_dir: Path, programs: str,
                          context_dir: "Path | None", model_name: "str | None",
                          max_nights: int, aggregate_every: int,
                          smoke: bool, resume_round: int = 1) -> dict:
    """跑一个孤岛的整轮（30 夜）探索，返回 metrics dict（run.py 的 Sample.metrics 同构）。"""
    from openkage.memory.skill_store import SkillStore

    # 技能库：常驻文件（跨夜跨段存活；续跑直开同文件追加——SkillStore 语义）。
    # close 在 finally：轨迹异常也要关连接（SQLite 跨进程残留锁会挡住续跑段打开）。
    skill_store = SkillStore(skill_dir / f"skill_{agent.name}.db",
                             vec_db_path=skill_dir / f"skill_{agent.name}_vec.db",
                             arm_key=agent.name)
    agent.skill_store = skill_store
    # LLM 留痕（工程纪律：每个调用落盘真正发出的 messages，见 openkage.agent_io）。
    # 每类调用一个文件、每次覆盖（Voyager 的调用不是累积对话，末次≠全集——但每类调用
    # 的 messages 结构固定，kind 区分 + n_llm_calls 计数已够复盘）。
    if context_dir is not None:
        from openkage.agent_io import ContextDumper
        dumpers: dict[str, ContextDumper] = {}

        def _dump(kind: str, messages: list[dict]) -> None:
            d = dumpers.get(kind)
            if d is None:
                d = dumpers[kind] = ContextDumper(
                    context_dir, f"voyager_{agent.name}_{kind}", kind=f"voyager_{kind}",
                    model_name=model_name)
            d(messages)

        agent.context_dump = _dump
    try:
        return await _run_one_voyager_core(
            domain, agent, skill_store, checkpoint_dir, programs,
            max_nights, aggregate_every, smoke, resume_round)
    finally:
        skill_store.close()


async def _run_one_voyager_core(domain, agent: VoyagerAgent, skill_store,
                                checkpoint_dir: Path, programs: str,
                                max_nights: int, aggregate_every: int, smoke: bool,
                                resume_round: int) -> dict:
    """run_one_voyager 的主体（技能库/留痕已由外层装配）。"""
    from domains.minecraft.env import run_exploration

    voyager_day, task_state = _make_voyager_day(agent)
    # 续跑恢复**任务半途态**（task/round/last_code/critique——课程任务清单已在 main 的
    # load_state 恢复）：扁平 JSON 的 metrics_snapshot.voyager 有每夜定格。
    if getattr(agent, "_resume", False):
        ckpt = read_progress(checkpoint_dir, agent.name, resume_round)
        voy = (ckpt or {}).get("metrics_snapshot", {}).get("voyager") or {}
        if voy.get("task"):
            task_state.update(task=voy["task"], context=voy.get("context", ""),
                              round=int(voy.get("round", 0)),
                              last_code=voy.get("last_code"),
                              critique=voy.get("critique", ""),
                              events=None,   # 隔夜反馈已过期，不带
                              parse_fail_streak=0)
            logger.info("[voyager:%s] 续跑恢复半途任务：%r（第%d轮）", agent.name,
                        voy["task"], voy.get("round", 0))

    async def night_cb(day_text, day_idx, discoveries, day_steps=None):  # noqa: ARG001
        """no-op 夜反思：Voyager 无夜反思（跨天记忆=技能库+任务清单）。
        discoveries 已由 env 无条件落盘（与对照组同机制），这里只打一行。"""
        logger.info("[voyager:%s] 第%d天过夜（新物品%d depth=%s）—— 无反思（Voyager 语义）",
                    agent.name, day_idx, discoveries.get("n_new_items", 0),
                    discoveries.get("tier"))
        return ""

    async with domain._pool.acquire(label=agent.name) as world:  # pylint: disable=protected-access
        # 世界恢复/重生（复用域的 _fresh_world：出生点 RNG 按 agent.name 派生，与
        # OpenKAGE 各臂同流）。resume 判定在 main 做好、经 agent/参数带进来。
        # voyager 侧的续跑状态恢复见 main 的 --resume 分支（agent.load_state）。
        resume = bool(getattr(agent, "_resume", False))
        start_night = int(getattr(agent, "_start_night", 0))
        if resume:
            # ★ 铁律（与 OpenKAGE rollout 同款，见 memory openkage_run_resume）：续跑**不读活存档**
            #   ——活存档可能被中断时刻的 save_world 污染（死亡重生瞬间的库存丢失等）。
            #   _fresh_world 的 resume 分支按 domain._resume_window 从 checkpoint{W}/world/
            #   拷回干净存档；start_night 回退到 W*K（重跑窗口间的几夜，换库存不丢——
            #   与 OpenKAGE 完全同语义，只是 K 这里是 checkpoint 粒度 aggregate_every）。
            from openkage.orchestrate.checkpoint import latest_window_with_world
            w = latest_window_with_world(checkpoint_dir, agent.name)
            if w is not None and aggregate_every:
                resume_start = w * aggregate_every
                domain._resume_window = w  # pylint: disable=protected-access
                if resume_start < start_night:
                    # 回退重跑窗口之间的夜（扁平 JSON 的 metrics_snapshot 是最新夜的、
                    # 对不上回退点 → env 侧从该夜重算指标，与 OpenKAGE 回退路径同口径）。
                    logger.info("[voyager:%s] 续跑回退到窗口%d 边界（第%d夜）恢复干净存档"
                                "（原 %d 夜，重跑 %d 夜防中途存档库存丢失）",
                                agent.name, w, resume_start, start_night,
                                start_night - resume_start)
                    start_night = resume_start
                    agent._start_night = resume_start  # pylint: disable=protected-access
                    # 半途任务态对不上回退点：清回「向课程要新任务」（任务清单保留——课程
                    # 进度是跨夜合法状态，只是这半个任务从窗口边界重开）。
                    task_state.update(task=None, round=0, last_code=None,
                                      critique="", events=None, parse_fail_streak=0)
            else:
                logger.info("[voyager:%s] 续跑但无窗口 world 存档（第一窗口前停）——"
                            "从活档续跑（早期段兜底）", agent.name)
        # ★ _fresh_world 全程持锁（_resume_window 是实例字段，并行写会串窗口号，见锁注释）。
        async with _FRESH_WORLD_LOCK:
            world_info = await domain._fresh_world(  # pylint: disable=protected-access
                world, round_num=resume_round, task_id="voyager_explore",
                arm_key=agent.name, arm_name=agent.name.capitalize(), resume=resume)

        async def _checkpoint_cb(n_nights_done: int, metrics_snapshot: dict | None) -> None:
            """每夜存档点（env 在日界 resume 后调用）：world 快照 + 扁平进度 + voyager 状态。"""
            await asyncio.to_thread(world.save_world)
            # voyager 状态塞进 metrics_snapshot 的独立键（env 的指标照写不冲突）
            snap = dict(metrics_snapshot or {})
            snap["voyager"] = {
                **agent.state_dict(),
                "task": task_state["task"], "context": task_state.get("context", ""),
                "round": task_state["round"],
                "last_code": task_state["last_code"], "critique": task_state["critique"],
            }
            write_progress(checkpoint_dir, agent.name, resume_round, n_nights_done,
                           max_nights, last_reflection="", metrics_snapshot=snap)
            # 窗口边界（每 aggregate_every 夜）：world 存档进 checkpoint{W}/（续跑恢复源；
            # latest_window_with_world 按 world/ 目录扫窗口号——OpenKAGE 的续跑判定全复用）
            if n_nights_done % aggregate_every == 0:
                w = n_nights_done // aggregate_every
                _snapshot_world(checkpoint_dir, world, agent.name, w)
                # 技能库快照同窗口（与 write_arm_graph_checkpoint 的 skill.db 同位、
                # 回退续跑有精确恢复点——SkillStore 本身常驻，快照是回退对齐用）
                arm_dir = Path(checkpoint_dir) / f"checkpoint{w}" / f"arm_{agent.name}"
                arm_dir.mkdir(parents=True, exist_ok=True)
                with contextlib.suppress(Exception):
                    skill_store.snapshot_with_vectors(arm_dir / "skill.db")
                # ★★ 窗口 progress.json（2026-10-02 补，voy1 续跑实踩）：
                #   `rollback_to_window` 的第①步校验、第④步回退**都强依赖**
                #   checkpoint{W}/arm_{X}/progress.json —— 缺它 `--resume --resume-window W`
                #   在第①步就抛 ValueError「无法回退扁平 JSON 到 N 边界夜」，续跑直接起不来。
                #   上面 write_progress 写的是**扁平** JSON（每夜覆盖、只剩最新夜），
                #   窗口这份才是回退点的**定格**——两者不是一回事，别以为写了扁平就够了。
                #   ⚠ 不静默：写失败必须留痕（0912/0925 老坑就是 copy 静默失败、窗口件缺失
                #   到要续跑时才发现）。
                try:
                    (arm_dir / "progress.json").write_text(json.dumps({
                        "round_num": resume_round, "arm_key": agent.name,
                        "n_nights_done": n_nights_done, "max_nights": max_nights,
                        "complete": False, "last_reflection": "",
                        "metrics_snapshot": snap,
                    }, ensure_ascii=False), encoding="utf-8")
                except Exception:  # pylint: disable=broad-except
                    logger.exception("[voyager:%s] 窗口%d 的 progress.json 写入失败——"
                                     "该窗口将不可续跑（--resume-window %d 会 ValueError）",
                                     agent.name, w, w)

        # 续跑指标恢复（OpenKAGE 同款缺陷①修复）：env 的 resume_metrics 从扁平 JSON 的
        # metrics_snapshot 恢复前段累积（step_idx/items_seen/tier_progress/…），否则
        # tier_first_step/n_steps/unique_items 只算续跑段。回退路径（start_night 被回退到
        # 窗口边界）时快照对不上回退点 → 传 None 让 env 从该夜重算（与 OpenKAGE 同口径）。
        resume_metrics = None
        if resume:
            ckpt = read_progress(checkpoint_dir, agent.name, resume_round)
            if ckpt and ckpt.get("metrics_snapshot"):
                if start_night == int(ckpt.get("n_nights_done", 0)):
                    resume_metrics = ckpt["metrics_snapshot"]  # 未回退：正好对上
                # 回退路径：不传（对不上，从回退点重算）

        ep = await run_exploration(
            world, voyager_day, programs=programs,
            max_nights=max_nights, night_cb=night_cb,
            smoke_dawn_nudge=smoke,
            spawn_pick=None if resume else domain._make_spawn_pick(agent.name),  # pylint: disable=protected-access
            recorder=None,           # 录像后开（--mc-footage，phase 2 接线）
            start_night=start_night,
            resume_spawn=resume,
            checkpoint_cb=_checkpoint_cb,
            resume_metrics=resume_metrics,
        )
    # ★ 必须经 _voyager_metrics 翻译：原始 ep 的 advancements/deaths 元素是 dict，而
    #   aggregate_eval 消费的是「名字列表」（域 rollout 落 Sample.metrics 的口径）——
    #   smoke 实抓：直接落 ep 会在 aggregate_eval 的 set().union 上炸 unhashable dict。
    return _voyager_metrics(agent, ep, world_info)


# ═════════════════════════════════════════════════════════════════════════════
# CLI / 编排 / 落盘
# ═════════════════════════════════════════════════════════════════════════════

def _voyager_metrics(agent: VoyagerAgent, ep: dict, world_info: dict) -> dict:
    """env 的 ep dict + voyager 侧状态 → 结果 JSON 的 metrics（OpenKAGE Sample.metrics 同构
    + voyager 专属列）。指标键名与 domains/minecraft rollout 的完全一致——消费端零改动。"""
    return {
        **world_info,
        "spawn": ep.get("spawn_actual"), "spawn_biome": ep.get("spawn_biome"),
        "unique_items": ep["unique_items"], "items_seen": ep["items_seen"],
        "peak_tier": ep["peak_tier"], "tier_rank": ep["tier_rank"],
        "final_tier": ep["final_tier"], "final_tier_rank": ep["final_tier_rank"],
        "tier_first_step": ep["tier_first_step"],
        "tier_progress": ep["tier_progress"], "n_steps": ep["n_steps"],
        "n_nights": ep["n_nights"], "n_deaths": ep.get("n_deaths", 0),
        "path_len": ep.get("path_len", 0.0),
        "max_dist_from_spawn": ep.get("max_dist_from_spawn"),
        "n_biomes": ep.get("n_biomes", 0), "biomes": ep.get("biomes", []),
        "n_pos_jumps": ep.get("n_pos_jumps", 0),
        "n_pos_samples": ep.get("n_pos_samples", 0),
        "deaths": ep.get("deaths", []), "deaths_summary": ep.get("deaths_summary", ""),
        "n_advancements": ep.get("n_advancements", 0),
        "advancements": [a.get("advancement") for a in ep.get("advancements", [])],
        "n_night_reflections": len(ep.get("night_reflections", [])),
        "xp_total": ep.get("xp_total", 0),
        "scoreboard": ep.get("scoreboard", {}),
        "daily_log": ep.get("night_reflections", []),
        "final_inventory": ep["final_inventory"],
        "final_equipped": ep.get("final_equipped", []),
        # ── voyager 专属列（公平性/机制观察）──
        "voyager_completed_tasks": list(agent.completed_tasks),
        "voyager_failed_tasks": list(agent.failed_tasks),
        "voyager_n_skills": len(agent.skill_store.list_verified_skills())
        if hasattr(agent.skill_store, "list_verified_skills") else 0,
        "voyager_llm_calls": dict(agent.n_llm_calls),
    }


def main() -> None:
    p = argparse.ArgumentParser(description="adapted Voyager 基线（孤岛智能体群，零共享）")
    p.add_argument("--model", required=True,
                   help="骨干模型（OPENAI_BASE_URL 端点上的模型名，如 deepseek-flash）。"
                        "必填：Voyager 不是本仓方法，不继承 yml 的 train.model。")
    p.add_argument("--agents", type=int, default=3,
                   help="孤岛智能体数（默认 3；标签 alice/bob/carol，出生点 RNG 与 "
                        "OpenKAGE 各臂同流）。smoke 自动收到 1。")
    p.add_argument("--max-nights", type=int, default=None, help="覆盖 yml（默认 30）")
    p.add_argument("--tick-rate", type=float, default=None, help="覆盖 yml（默认 2.5）")
    p.add_argument("--difficulty", type=str, default=None, help="覆盖 yml（默认 hard）")
    p.add_argument("--world-seed", type=str, default=None, help="覆盖 yml（默认 7）")
    p.add_argument("--spawn-seed", type=str, default=None, help="覆盖 yml（默认 20260926）")
    p.add_argument("--smoke", action="store_true",
                   help="smoke：1 智能体 1 夜 + 开局拨到将黎明（验证链路，~20min）")
    p.add_argument("--config", type=str, default=str(_PKG_ROOT / "configs" / "openkage_default.yml"))
    p.add_argument("--mc-dir", type=str, default="/path/to/mc_probe/server121")
    p.add_argument("--mc-workers-dir", type=str, default="/path/to/mc_probe/openkage_workers")
    p.add_argument("--mc-workers", type=str, default="auto")
    p.add_argument("--mc-port-base", type=int, default=25600)
    p.add_argument("--mineflayer-port-base", type=int, default=3100)
    p.add_argument("--mc-reprovision", action="store_true")
    p.add_argument("--results-dir", type=str, default=None, help="覆盖落盘根（默认同 run.py 探测）")
    p.add_argument("--resume", type=str, default=None,
                   help="断点续跑：旧 run 的结果 JSON 或其所在目录")
    p.add_argument("--resume-window", type=int, default=None,
                   help="从指定 checkpointN 恢复（与 --resume 配对；缺省=最大窗口）")
    p.add_argument("--tag", type=str, default="voyager", help="run 标签（进目录名末段）")
    args = p.parse_args()

    global _RUN_TS  # noqa: PLW0603  续跑沿用旧 run_ts（目录名对上旧 run_dir），main 内重赋一次

    if args.resume and args.resume_window is None:
        p.error("--resume 必须配 --resume-window N（与 run.py 同款：显式回退到窗口 N）")
    if args.resume_window is not None and not args.resume:
        p.error("--resume-window N 必须与 --resume 一起用")

    # ---- 日志（与 run.py 同款双写 + 作用域列）----
    logs_dir = _PKG_ROOT / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / f"run_voyager_{_RUN_TS}_{_RUN_HOST}.log"
    fmt = ScopeFormatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers = [logging.StreamHandler(sys.stdout), logging.FileHandler(log_path, encoding="utf-8")]
    for h in root.handlers:
        h.setFormatter(fmt)

    def _term_to_keyboard(signum, frame):  # noqa: ARG001
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, _term_to_keyboard)  # 与 run.py 同款：SIGTERM 走 finally 落盘

    cfg = load_config(Path(args.config))
    ds = cfg.domain("minecraft")  # TrainConfig.domain（域私有规模；无则空 dict，逐键 .get 兜底）
    max_nights = args.max_nights if args.max_nights is not None else int(ds.get("max_nights", 30))
    tick_rate = args.tick_rate if args.tick_rate is not None else float(ds.get("tick_rate", 2.5))
    difficulty = args.difficulty or str(ds.get("difficulty", "hard"))
    world_seed = args.world_seed or str(ds.get("world_seed", "7"))
    spawn_seed = args.spawn_seed or str(ds.get("spawn_seed", "20260926"))
    aggregate_every = int(ds.get("aggregate_every", 2))  # 只作 checkpoint 窗口粒度（无聚合语义）
    n_agents = 1 if args.smoke else max(1, args.agents)
    if args.smoke:
        max_nights = 1
        aggregate_every = 1

    # ---- 续跑：读旧 JSON + rollback 到窗口 N ----
    resume_data: dict | None = None
    if args.resume:
        rp = Path(args.resume)
        if rp.is_dir():
            rp = rp / f"{rp.name}.json"
        if not rp.exists():
            p.error(f"--resume 指向的文件不存在：{rp}")
        resume_data = json.loads(rp.read_text(encoding="utf-8"))
        if resume_data.get("ablation") != "voyager":
            p.error(f"--resume 指向的旧 run ablation={resume_data.get('ablation')!r}，"
                    f"不是 voyager 基线 run，拒绝续跑。")
        old_model = (resume_data.get("config") or {}).get("model")
        if old_model and old_model != args.model:
            p.error(f"--model 与旧 run 不一致（旧={old_model} 新={args.model}）——"
                    f"续跑必须同骨干（曲线可比性）。")
        _RUN_TS = resume_data["run_ts"]
        # rollback：窗口 N 之后的删掉（备份）、扁平 JSON 回退到 N 边界夜——与 run.py 同语义。
        old_agents = (resume_data.get("config") or {}).get("agents") or list(DEFAULT_AGENT_KEYS[:n_agents])
        if args.resume_window is not None:
            rb = rollback_to_window(rp.parent, old_agents, args.resume_window, 1)
            logger.info("[resume] 已回退到窗口%d：删窗口 %s，回退臂 %s",
                        args.resume_window, rb["deleted_windows"] or "(无)",
                        rb["rolled_back_arms"])

    model_slug = args.model or "default"
    run_stem = (f"minecraft_{_RUN_TS}_{_RUN_HOST}_voyager_{model_slug}"
                + ("_smoke" if args.smoke else ""))
    results_root = Path(args.results_dir).expanduser() if args.results_dir else _RESULTS_ROOT
    run_dir = results_root / run_stem
    run_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_dir = run_dir
    skill_dir = run_dir / "skills"
    skill_dir.mkdir(parents=True, exist_ok=True)
    context_dir = run_dir / "contexts"
    out = run_dir / f"{run_stem}.json"
    logger.info("[voyager] adapted Voyager 基线：%s | agents=%d nights=%d agg(ckpt)=%d "
                "difficulty=%s seed=%s/%s tick=%.1f | %s", model_slug, n_agents, max_nights,
                aggregate_every, difficulty, world_seed, spawn_seed, tick_rate, run_dir)

    # ---- 世界工厂（MinecraftDomain 只用其 worker 池/_fresh_world/shutdown）----
    domain = build_domain(
        "minecraft",
        mc_port_base=args.mc_port_base,
        mineflayer_port_base=args.mineflayer_port_base,
        workers=n_agents,
        workers_dir=args.mc_workers_dir,
        reprovision=args.mc_reprovision,
        server_dir=_PKG_ROOT / "domains" / "minecraft" / "mc_server",
        max_nights=max_nights,
        aggregate_every=aggregate_every,
        smoke_dawn_nudge=args.smoke,
        mc_dir=args.mc_dir,
        world_seed=world_seed,
        spawn_seed=spawn_seed,
        difficulty=difficulty,
        tick_rate=tick_rate,
        # ★ random_spawn=False（2026-10-02 用户拍板「口径对齐 OpenKAGE 后重跑」）：
        #   OpenKAGE 主表 run.py 的 --fixed-spawn 默认 True → random_spawn=False →
        #   **世界自然出生点**（seed 7 → plains）。此前漏传落到域默认 True → ±1000 随机出生
        #   （voy1 首跑三孤岛落 forest/sunflower_plains/plains 三个不同群系），与主表
        #   「开局条件对齐」的 docstring 声明矛盾。对齐后 spawn_pick 返回 None（自然出生点），
        #   spawn_seed 参数仅作记录（不再进 RNG）。
        random_spawn=False,
        context_dir=context_dir,
        checkpoint_dir=checkpoint_dir,
        no_graph=True,   # 只用世界工厂，图谱/技能编排全不用（域侧跳过图谱快照等夜_cb 逻辑）
    )
    programs = domain._programs  # pylint: disable=protected-access  waitUntil（裸接口口径）

    agent_keys = list(DEFAULT_AGENT_KEYS[:n_agents])
    agents = []
    for k in agent_keys:
        a = VoyagerAgent(k, args.model, skill_store=None)
        # 续跑恢复：扁平 JSON 的 voyager 状态 + 世界从 checkpoint 恢复 + start_night
        a._resume = False  # pylint: disable=protected-access
        a._start_night = 0  # pylint: disable=protected-access
        if resume_data is not None:
            ckpt = read_progress(checkpoint_dir, k, 1)
            if ckpt is not None and not ckpt.get("complete"):
                a._resume = True  # pylint: disable=protected-access
                a._start_night = int(ckpt.get("n_nights_done", 0))  # pylint: disable=protected-access
                voy = (ckpt.get("metrics_snapshot") or {}).get("voyager") or {}
                a.load_state(voy)
                logger.info("[voyager:%s] 续跑：已过%d/%d夜，任务清单 完成%d/失败%d",
                            k, a._start_night, max_nights,
                            len(a.completed_tasks), len(a.failed_tasks))
        agents.append(a)

    async def _run_all() -> list[dict]:
        # 孤岛并行（各借一台 worker；与 OpenKAGE parallel_arms 同构）。返回逐 agent metrics。
        tasks = [
            run_one_voyager(domain, a, skill_dir, checkpoint_dir, programs,
                            context_dir, args.model, max_nights, aggregate_every,
                            smoke=args.smoke)
            for a in agents
        ]
        return await asyncio.gather(*tasks)

    metrics_per_agent: list[dict] = []
    try:
        metrics_per_agent = asyncio.run(_run_all())
    finally:
        # ★ 与 run.py 同纪律：SIGINT/异常也写 JSON（续跑有据）；先落盘再关世界。
        # finally 里写一次即够——正常路径也走这里，无需在 try 后再写一遍（幂等但冗余）。
        try:
            _write_results(out, args, agents, metrics_per_agent, run_dir, model_slug,
                           max_nights, aggregate_every, difficulty, world_seed, spawn_seed,
                           tick_rate, resume_data)
            logger.info("[finally] 已写入 %s", out)
        except Exception:  # pylint: disable=broad-except
            logger.exception("[finally] 写结果 JSON 失败（续跑需手工补）")
        domain.shutdown()

    logger.info("完成，写入 %s（%d 个孤岛）", out, len(metrics_per_agent))


def _write_results(out: Path, args, agents: list[VoyagerAgent], metrics_per_agent: list[dict],
                   run_dir: Path, model_slug: str, max_nights: int, aggregate_every: int,
                   difficulty: str, world_seed: str, spawn_seed: str, tick_rate: float,
                   resume_data: dict | None) -> None:
    """结果 JSON（schema 仿 run.py：ablation="voyager"，train_curve 单点 + per_arm_detail）。"""
    from domains.minecraft import MinecraftDomain
    from openkage.domain import Sample

    domain = MinecraftDomain.__new__(MinecraftDomain)  # 只借 aggregate_eval（纯函数，无状态）
    domain_name = "minecraft"
    detail = None
    per_arm_detail = {}
    if metrics_per_agent:
        try:
            samples = [Sample(text="", correct=None, metrics=m) for m in metrics_per_agent]
            detail = domain.aggregate_eval(samples).detail
            per_arm_detail = {a.name: domain.aggregate_eval(
                [Sample(text="", correct=None, metrics=m)]).detail
                for a, m in zip(agents, metrics_per_agent)}
        except Exception:  # pylint: disable=broad-except
            logger.exception("aggregate_eval 渲染失败（结果仍有原始 metrics）")
    # train_curve：单点（voyager 是一条轨迹，无 epoch 轴；score=各孤岛 xp_total 均值，
    # 与 OpenKAGE 的 train_curve 点同构——main_table 消费端按 per_arm_detail join）。
    scores = [m.get("xp_total", 0) for m in metrics_per_agent] or [0.0]
    payload = {
        "run_ts": _RUN_TS, "run_host": _RUN_HOST, "domain": domain_name,
        "ablation": "voyager",
        "config": {
            "agents": [a.name for a in agents],
            "model": args.model,
            "max_nights": max_nights, "aggregate_every_ckpt": aggregate_every,
            "difficulty": difficulty, "world_seed": world_seed, "spawn_seed": spawn_seed,
            "tick_rate": tick_rate, "smoke": bool(args.smoke),
            "n_agents": len(agents),
            **({"resumed_from": str(args.resume)} if args.resume else {}),
        },
        "train_curve": [{
            "epoch": 1,
            "score": sum(scores) / len(scores),
            "per_arm": {a.name: m.get("xp_total", 0)
                        for a, m in zip(agents, metrics_per_agent)},
            "n_arms": len(agents),
            "n_samples": len(metrics_per_agent),
            **({"detail": detail} if detail else {}),
            **({"per_arm_detail": per_arm_detail} if per_arm_detail else {}),
        }],
        "eval_curve": [],
        "consolidation_log": [],
        # 逐孤岛原始 metrics（含 daily_log 逐夜 discoveries——消费端读 checkpoint 或这里均可）
        "voyager_agents": metrics_per_agent,
        **({"resumed_from": str(args.resume)} if args.resume else {}),
    }
    if resume_data is not None:
        # 续跑沿用旧 run_ts（目录名一致），train_curve 仍单点（本次覆盖写最新值）——
        # voyager 无 epoch 轴，不存在「旧点+新点」拼接问题。
        old_curve = resume_data.get("train_curve") or []
        if old_curve and not metrics_per_agent:
            # 本段异常退出没拿到 metrics：保留旧点（finally 路径 metrics 空时的兜底），
            # 别用一个全零点把旧成果覆盖掉。
            payload["train_curve"] = old_curve
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
