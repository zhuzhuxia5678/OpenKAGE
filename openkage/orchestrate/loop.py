"""泛型编排：编排层对 TaskDomain 多态调用——因域而异的「怎么跑一次采样 / 怎么反思 / 怎么判分 /
怎么聚合 eval」全在 domain 里，编排层只管 epoch/batch/arm/采样并发/留痕这套骨架。

- run_arm_batch：一个副脑在一个 batch 的每个 task 上跑 N 次采样（domain.parallel_samples
  决定并行还是串行）→ domain.reflect 把这批采样写进该副脑图谱。
- eval_readonly：evolving(主脑) vs frozen 各跑一遍只读采样，用 domain.aggregate_eval
  聚合成分数，gap = 两者之差。

设计约束：不依赖 config/backend；n_samples/副脑角色由调用方传入。consolidate 仍在
orchestrate/consolidate.py（已域无关），本模块不重复。
"""

from __future__ import annotations

import asyncio
import collections
import logging
import time

from openkage.domain import EvalScore, Sample, Task, TaskDomain
from openkage.logctx import log_banner, log_scope
from openkage.orchestrate.window import WindowAggregationError
from openkage.roundcode import encode as _encode_round

logger = logging.getLogger("loop")


async def _rollout_samples(
    domain: TaskDomain, task: Task, *, primary, secondary, arm_role, arm_key, model_name,
    arm_name=None,
    round_num: int, n_samples: int, on_window=None, on_night=None,
    on_spawn_ready=None,
    skill_registry=None,
) -> list[Sample]:
    """跑本 task 的 N 次采样。domain.parallel_samples=True 则并行（无共享写状态），否则串行
    （如 Minecraft：同一副脑的多次采样共用它借到的那一台 MC server）。

    每次采样套一层 `#i` 日志作用域：并行时 N 条采样的底层日志（rollout 检索、httpx 请求）会按
    完成顺序交错，没有这个序号就分不清「同一 task 的 4 条检索日志」是 4 次采样各一条、还是某次
    采样重试了 4 遍 —— 这两种情况的排查方向完全不同。

    `on_window` 透传给域（题内聚合回调，见下方 run_arm_batch 的说明）。
    `on_night` 透传给域（每晚三栅栏回调，见 run_arm_batch 说明）。
    """
    async def call(i: int):
        with log_scope(f"#{i}"):
            return await domain.rollout(
                task, primary=primary, secondary=secondary, arm_role=arm_role,
                arm_name=arm_name,
                arm_key=arm_key, model_name=model_name, round_num=round_num,
                on_window=on_window,
                on_night=on_night,
                on_spawn_ready=on_spawn_ready,
                skill_registry=skill_registry,
            )

    if getattr(domain, "parallel_samples", True):
        return list(await asyncio.gather(*[call(i) for i in range(1, n_samples + 1)]))
    return [await call(i) for i in range(1, n_samples + 1)]


async def run_arm_batch(
    domain: TaskDomain,
    arm: dict,
    primary,
    ephemeral,
    batch_tasks: list[Task],
    model_name: str | None,
    epoch: int,
    batch_id: int,
    n_samples: int,
    log_id: str | None = None,
    window_agg=None,
    night_barrier=None,
    spawn_barrier=None,
    skill_registry=None,
) -> list[dict]:
    """一个副脑在这个 batch 的每个 task：跑 N 次采样（读主脑 primary + 自己的副脑图 ephemeral），
    再交给 domain.reflect 写进副脑图（ephemeral）。读侧只读主脑，写侧只写自己的副脑图
    （「只读主脑 + 只写副脑」）。返回逐 task 留痕 records。

    整个函数体套在 `log_scope("e<epoch>b<batch> <arm>")` 里 —— math 的各 arm 走
    asyncio.gather 并行，日志按完成顺序交错，只有作用域标签能认出每一行属于哪个副脑。
    正因为标签已在行首，本函数内部的日志不再重复打 `[e%d b%d][%s]` 前缀。

    log_id：作用域里用的**短** arm 名（默认退回 arm["key"]）。若某域的 key 本身较长，
    调用方可用 logctx.arm_log_ids 统一算短名后传进来。

    window_agg：题内聚合的窗口收集器（`orchestrate.window.WindowAggregator`，可为 None）。
    本函数把它包装成一个 `on_window(window, at)` 回调交给域 —— **域只需知道"我到了一个聚合
    边界"**，不需要知道 arm_key、快照路径、攒齐几份才发车这些编排细节。
    ★ 本函数**保证** `retire()` 一定被调（finally）：漏调会让后续窗口永久等一个已经退场的
    副脑、且完全静默（聚合就是不发生而已）—— 所以这个责任放在编排层，不交给域。
    """
    with log_scope(f"e{epoch}b{batch_id} {log_id or arm['key']}"):
        try:
            return await _run_arm_batch_inner(
                domain, arm, primary, ephemeral, batch_tasks, model_name,
                epoch, batch_id, n_samples, window_agg=window_agg,
                night_barrier=night_barrier,
                spawn_barrier=spawn_barrier,
                skill_registry=skill_registry,
            )
        finally:
            if window_agg is not None:
                # 本副脑不再供货。**必须在 finally**：rollout 抛异常时（MC 的世界卡死/自愈无效
                # 会 raise）同样要退场，否则它欠的那一份永远补不上。
                window_agg.retire(arm["key"])
            if night_barrier is not None:
                # ★ 同上：每晚栅栏也靠「攒齐还在供货的副脑」发车，漏 retire 会让栅栏永久卡死。
                night_barrier.retire(arm["key"])



async def _run_arm_batch_inner(
    domain: TaskDomain,
    arm: dict,
    primary,
    ephemeral,
    batch_tasks: list[Task],
    model_name: str | None,
    epoch: int,
    batch_id: int,
    n_samples: int,
    window_agg=None,
    night_barrier=None,
    spawn_barrier=None,
    skill_registry=None,
) -> list[dict]:
    """run_arm_batch 的函数体（拆出来只为让作用域包装那层保持一目了然）。"""
    records: list[dict] = []
    arm_role = arm.get("role")
    # Agent 的名字（英文人名，如 Alice）。minecraft 域拿它当**游戏内用户名** ——
    # key 是文件名/机器 id（alice），name 才是身份（Alice）；MC 用户名大小写敏感，
    # 且日后多 Agent 联机时彼此称呼用的是这个。
    arm_name = arm.get("name") or arm["key"]
    round_num = _encode_round(epoch, batch_id)
    # 题内聚合回调：把编排细节（arm_key / 快照 / 攒齐几份）挡在域外面，域只报「我到了第 N 个
    # 聚合边界、此刻是第几天」。域不做题内聚合时它一次都不会被调。
    # ★ 2026-08-29 三栅栏拆分：on_window 暴露 arrive（栅栏1 后起后台聚合）+ collect（栅栏3
    #   等聚合结果），不再单一 __call__。仍保留 __call__=arrive+collect 兼容旧调用/测试。
    on_window = None
    if window_agg is not None:
        _arm_key = arm["key"]
        class _WindowHook:
            async def arrive(self, window: int, at: int) -> None:
                await window_agg.arrive(_arm_key, window=window, at_day=at)
            async def collect(self, window: int) -> dict:
                return await window_agg.collect(_arm_key, window=window)
            async def __call__(self, window: int, at: int) -> dict:  # 旧 submit 兼容
                await window_agg.arrive(_arm_key, window=window, at_day=at)
                return await window_agg.collect(_arm_key, window=window)
        on_window = _WindowHook()

    # 每晚三栅栏回调（栅栏1/2，纯等齐；栅栏3 由 on_window.collect 担任）。
    on_night = None
    if night_barrier is not None:
        _arm_key2 = arm["key"]
        async def on_night(stage: int, day_idx: int) -> None:
            await night_barrier.submit(_arm_key2, stage=stage, day_idx=day_idx)

    # 开局出生栅栏回调（2026-09-12）：bot 出生后、第一步训练前调一次，等齐所有臂到齐放行。
    # 与 on_night 同模式：spawn_barrier=None（eval/单臂）时 on_spawn_ready=None，域内 None 守卫跳过。
    # ★ SpawnBarrier 无 retire——run_arm_batch 的 finally 刻意不调（出生是一次性事件，详见类注释）。
    on_spawn_ready = None
    if spawn_barrier is not None:
        _arm_key3 = arm["key"]
        async def on_spawn_ready() -> None:
            await spawn_barrier.submit(_arm_key3)

    # 域可强制覆盖采样次数（domain.n_samples，见 domain.py 契约）：多采样对某些域是**冗余维度**
    # —— Minecraft 的 task 内容全同、且每轮 rollout 都 reset 世界，「同 task 采 N 次」与
    # 「N 个 task 各采 1 次」产出等价，白跑 N-1 倍墙钟（单 bot 还只能串行）。域自己声明 1 即关掉，
    # 不必靠调用方记得在 config/命令行里改。与 parallel_arms/parallel_samples 同属域声明的编排约束。
    domain_n = getattr(domain, "n_samples", None)
    if domain_n and domain_n != n_samples:
        logger.info("域 %s 声明 n_samples=%d，覆盖传入值 %d",
                    getattr(domain, "name", "?"), domain_n, n_samples)
        n_samples = domain_n
    for i, task in enumerate(batch_tasks, 1):
        _t = time.perf_counter()
        try:
            samples = await _rollout_samples(
                domain, task, primary=primary, secondary=ephemeral, arm_role=arm_role,
                arm_key=arm["key"], arm_name=arm_name, model_name=model_name, round_num=round_num,
                n_samples=n_samples, on_window=on_window, on_night=on_night,
                on_spawn_ready=on_spawn_ready,
                skill_registry=skill_registry,
            )
        except WindowAggregationError:
            # ★ 题内聚合失败 = abort 整个 batch（2026-08-19，栅栏同步配套语义）：
            #   失败的窗口继续跑会污染后续训练。此处**放行**它（不吞），让它穿到 run_arm_batch
            #   外 → brain.py 的 gather 无 return_exceptions → abort 整个 batch。
            #   与下面的「世界卡死/自愈无效」这类 rollout 自身故障区分：那些仍「记0样本继续」，
            #   因为它们是单 arm 单 task 的偶发故障，不是记忆系统的污染。
            logger.exception("[训练][%d/%d] %s 题内聚合失败 —— abort 整个 batch",
                              i, len(batch_tasks), task.id)
            raise
        except Exception:  # pylint: disable=broad-except
            # ★ rollout 失败 = abort 整个 run（2026-09-12，覆盖旧「记0样本继续」设计）：
            #   旧设计吞掉异常记 0 样本继续下一个 task，意图是「防世界卡死/自愈无效这类偶发
            #   故障拖垮整条训练」。但实测后果是：一臂 rollout 崩（如 MC mineflayer 起不来 /
            #   FUSE 读 node_modules ENOENT）后，其他 4 臂继续跑几十秒~几分钟**干烧 token**，
            #   且产出 5 臂撕裂的不可比曲线。用户定案：rollout 失败就立刻停整 run，不干烧。
            #   放行异常（不吞）→ brain.py:602 的 asyncio.gather（无 return_exceptions）立即抛出
            #   并 cancel 其他还在跑的臂（asyncio 语义，已 3.13 实测）→ 穿到 run.py finally 写
            #   results JSON（续跑有据）→ 进程退出。与上方 WindowAggregationError 同款「abort」语义。
            logger.exception("[训练][%d/%d] %s rollout 失败 —— abort 整个 run（不干烧其他臂 token）",
                              i, len(batch_tasks), task.id)
            raise
        solve_sec = time.perf_counter() - _t

        letters = [(s.metrics.get("answer_letter") or "?") for s in samples]
        distribution = dict(collections.Counter(letters))
        n_correct = sum(1 for s in samples if s.correct)
        records.append({
            "epoch": epoch, "batch_id": batch_id, "arm": arm["key"],
            "task_id": task.id, "topic_tag": task.topic_tag,
            "n_samples": n_samples,
            "answer_distribution": distribution,
            "n_correct": n_correct,
            "sample_metrics": [s.metrics for s in samples],
            "sample_answers": [s.text for s in samples],
            "primary_context": samples[0].primary_context if samples else None,
            "secondary_context": samples[0].secondary_context if samples else None,
            "solve_sec": round(solve_sec, 1),
        })
        # 无对错的域（探索类，correct 恒 None、无 answer_letter）：打「分布={'?':N} (对0/N)」
        # 会读成"全答错了"，是误导。改用 domain.score 的均分汇报，那才是这类域的进展信号。
        graded = any(s.correct is not None for s in samples)
        if graded:
            logger.info(
                "[训练][%d/%d] %s 采样%d次 分布=%s (对%d/%d) solve=%.1fs",
                i, len(batch_tasks), task.id,
                n_samples, distribution, n_correct, n_samples, solve_sec,
            )
        else:
            try:
                avg = sum(domain.score(s) for s in samples) / len(samples) if samples else 0.0
            except Exception:  # pylint: disable=broad-except
                avg = float("nan")
            logger.info(
                "[训练][%d/%d] %s 采样%d次 均分=%.1f solve=%.1fs",
                i, len(batch_tasks), task.id, n_samples, avg, solve_sec,
            )
        # 反思写进该副脑图谱（某些域此调用为 no-op：如 Minecraft 反思已在 rollout 的 night_cb 里逐夜做）
        try:
            _t = time.perf_counter()
            with log_scope("reflect"):  # ASCII 标签：中文占两列会破坏行首定宽对齐
                await domain.reflect(ephemeral, task, samples,
                                     arm_role=arm_role, round_num=round_num)
            records[-1]["reflect_sec"] = round(time.perf_counter() - _t, 1)
        except Exception:  # pylint: disable=broad-except
            logger.exception("reflect 失败，跳过本 task 记忆写入")
    return records


async def _eval_one_group(
    domain: TaskDomain, graph, eval_tasks: list[Task], model_name: str | None,
    epoch: int, label: str, scope: str,
):
    """一组（evolving/frozen）跑一遍只读采样：只读主脑图谱，不写任何图谱（secondary=None）。

    `scope` 是 frozen/evolving 分界的关键 —— 这一组内部所有日志（rollout 的记忆检索、图谱查询、
    httpx 请求）都会自动带上 `[base frozen]` / `[ev3 evolving]`，否则底层日志一律裸奔，
    两组的检索日志会混在一起分不清谁是谁。
    label 仍单独传：它进正文（人读的组名），scope 进行首（对齐成列的短标签）。
    """
    samples: list[Sample] = []
    n = len(eval_tasks)
    with log_scope(scope):
        for i, task in enumerate(eval_tasks, 1):
            try:
                s = await domain.rollout(
                    task, primary=graph, secondary=None, arm_role=None, arm_key=None,
                    # eval 只读、不写任何图谱（secondary=None），所以这个 round 号其实没有
                    # 任何一行会带上它 —— 仍走 encode 是为了不留下第二种编码写法（batch=0
                    # 表示"不属于任何 batch"）。**不传 on_window**：eval 不做题内聚合。
                    model_name=model_name, round_num=_encode_round(epoch, 0),
                )
            except Exception:  # pylint: disable=broad-except
                logger.exception("[%s][%d/%d] %s 采样异常，跳过", label, i, n, task.id)
                continue
            samples.append(s)
            logger.info("[%s][%d/%d] %s -> 分=%.1f", label, i, n, task.id, domain.score(s))
        return domain.aggregate_eval(samples)


async def eval_baseline_only(
    domain: TaskDomain, frozen, eval_tasks: list[Task], model_name: str | None,
) -> EvalScore:
    """只测 frozen 对照组一次，作为整条曲线复用的基线（训练开始前调用）。

    评测预算（n_eval=K、epochs=N）：对照组 K×1（此函数一次）+ 实验组 K×N（每轮训练后）
    = K×(1+N)。frozen 只测一次的依据：它在整个 run 里**不可变** —— eval 恒传 secondary=None
    （不写任何图），无种子域的 frozen 更是 NullGraph（reflect 也是 no-op）。后续各 epoch 经
    eval_readonly 的 frozen_cache 复用此分。

    不区分有无种子：无论训练前主脑是空图还是带教材先验，都不再单独测 epoch0 的 evolving，
    曲线 X 轴从 epoch1 起。
    """
    log_banner(logger, "frozen 基线（冻结对照组 · 空记忆水平线 · 全程只测这一次）")
    fr = await _eval_one_group(domain, frozen, eval_tasks, model_name, 0, "基线frozen",
                               scope="base frozen")
    logger.info("[基线frozen] 得分 %.1f/%d —— 整条曲线复用此基线", fr.score, fr.total)
    return fr


async def eval_readonly(
    domain: TaskDomain, primary, frozen, eval_tasks: list[Task],
    model_name: str | None, epoch: int,
    frozen_cache: EvalScore | None = None,
) -> dict:
    """evolving(主脑) 跑一遍只读评测，与 frozen 对照组比出 gap = evolving - frozen。

    **frozen 只在训练前实测一次、之后全程复用**：frozen 图谱在整个 run
    里**不可变** —— eval 恒传 `secondary=None`（不写任何图），且空图域的 frozen 就是 NullGraph
    （reflect 是 no-op）。每个 epoch 重跑它只是在同一固定条件下重复采样，白烧 epochs 倍墙钟
    （MC 单 bot 串行、一条轨迹几分钟，代价很实）。基线由 eval_baseline_only 测得后经
    `frozen_cache` 传入；未传（独立调用场景）才现场测一次。

    返回值里 `frozen_reused` 标记该点的 frozen 分是实测还是复用，便于结果 JSON 复盘。

    gap 的量纲由各域的 aggregate_eval 定：math 的 eval 是**一批不同的题**，求和
    （对 N 题中的几题）天然可比；Minecraft 的 eval 是**同一目标重复 K 次**，其 aggregate_eval
    返回「每轮探索得分均值」，故 n_eval 变化时 gap 仍可比。跨 n_eval / 跨种子比较前，先确认
    该域 aggregate_eval 的口径。
    """
    log_banner(logger, f"第 {epoch} 轮 · evolving 评测（实验组 · 读训练后的主脑记忆）")
    ev = await _eval_one_group(domain, primary, eval_tasks, model_name, epoch, "evolving",
                               scope=f"ev{epoch} evolving")
    reused = frozen_cache is not None
    fr = frozen_cache if reused else await _eval_one_group(
        domain, frozen, eval_tasks, model_name, epoch, "frozen", scope=f"ev{epoch} frozen")
    gap = ev.score - fr.score
    logger.info(
        "[第%d轮对照] evolving %.1f/%d vs frozen %.1f/%d%s → gap=%+.1f",
        epoch, ev.score, ev.total, fr.score, fr.total,
        "(复用训练前基线)" if reused else "", gap,
    )
    return {
        "epoch": epoch,
        "evolving_score": ev.score,
        "frozen_score": fr.score,
        "gap": gap,
        "total": ev.total,
        "frozen_total": fr.total,
        "frozen_reused": reused,
        "evolving_detail": ev.detail,
        "frozen_detail": fr.detail,
        "_frozen_score_obj": fr,  # 供调用方缓存（独立调用 eval_readonly 时可留存复用）
    }
