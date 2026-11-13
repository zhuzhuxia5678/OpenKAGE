"""框架层断点续跑 checkpoint 工具（任务域无关）。

提供两类操作：
· **进度 JSON**（扁平，run_dir 根目录）：每夜由 domain._checkpoint_cb 更新 n_nights_done；
  `complete=True` 在最终夜由 domain 写入。rollout 开头读它决定「续跑 or 新跑」。
· **ARM 图谱 checkpoint**（窗口粒度，checkpoint{W}/arm_{key}/）：在 submit() 里于
  清经验层后立即写私有图谱快照 + 向量库；搭配 domain 写入 world/ 成自包含目录。
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import time
from pathlib import Path

logger = logging.getLogger("checkpoint")


# ─────────── 扁平进度 JSON ───────────

def checkpoint_path(run_dir, arm_key, round_num) -> Path:
    """进度文件路径：{run_dir}/checkpoint_{arm}_r{round}.json。"""
    arm = arm_key or "eval"
    return Path(run_dir) / f"checkpoint_{arm}_r{round_num}.json"


def write_progress(
    run_dir, arm_key, round_num, n_nights_done, max_nights=None,
    last_reflection: str = "",
    metrics_snapshot: dict | None = None,
    last_goal: str = "",
) -> None:
    """写/更新扁平进度 JSON。

    max_nights=None 时不写 complete 字段（读端 ``get("complete")`` 返回 None，
    等价于 False——会触发续跑，符合「中途窗口」语义）。
    max_nights 传入时按 n_nights_done >= max_nights 计算 complete。
    last_reflection 非空时写入，供续跑时注入「昨夜复盘」上下文。
    last_goal 非空时写入，供续跑时恢复次日 day_start 的【昨日目标】块（有记忆层的 rung 同写；
    2026-09-24 起 context_mgmt=记忆层连带，−M/no_graph 写空串=字段缺席；0909-0910 期间的对照
    组 checkpoint 亦无此字段）。
    metrics_snapshot 非空时写入，供续跑恢复指标累积状态（step_idx/items_seen/
    tier_progress/…，修续跑缺陷①：否则续跑段这些从 0、tier_first_step/n_steps/unique_items
    只算续跑段）。每夜覆盖写——保留最近一次快照即可，续跑从该夜继续。
    """
    p = checkpoint_path(run_dir, arm_key, round_num)
    arm = arm_key or "eval"
    data: dict = {
        "arm_key": arm,
        "round_num": round_num,
        "n_nights_done": int(n_nights_done),
    }
    if max_nights is not None:
        data["max_nights"] = int(max_nights)
        data["complete"] = int(n_nights_done) >= int(max_nights)
    if last_reflection:
        data["last_reflection"] = last_reflection
    if last_goal:
        data["last_goal"] = last_goal
    if metrics_snapshot:
        data["metrics_snapshot"] = metrics_snapshot
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        logger.info("[checkpoint] 进度：arm=%s round=%s 已过%d夜%s → %s",
                    arm, round_num, n_nights_done,
                    f"/{max_nights}" if max_nights is not None else "",
                    p.name)
    except Exception:  # pylint: disable=broad-except
        logger.exception("[checkpoint] 写进度失败（arm=%s round=%s）——"
                         "最坏是该窗口没有续跑点，不影响当前轨迹", arm, round_num)


# ★ FUSE 写后读半同步重试参数（0913 实测校准）：rollback 刚写完的扁平 JSON，屏障读回
#   成功后 0.6~3.2s 内 brain/rollout 层再读仍可能拿到半同步态（json.loads 抛异常），且
#   可见性**非单调**（先可见后不可见）。0912 观察的 332ms / 1.5s 屏障窗口被证明不充分
#   ——重试下沉到 read_progress 本身（所有读者受益：brain 技能库/活图谱恢复 + rollout），
#   窗口放宽到 retries×delay=10s。本地盘 write_text 立刻可见，健康路径第一次读即中、
#   零等待；仅损坏路径才等待。
_FUSE_READ_RETRIES = 20
_FUSE_READ_DELAY = 0.5


def read_progress(run_dir, arm_key, round_num,
                  *, retries: int = _FUSE_READ_RETRIES) -> dict | None:
    """读进度 JSON。无文件返回 None（= 这条轨迹没跑过，从头开始）。

    ★ 损坏（json.loads 抛异常）≠ 没跑过（0912/0913 两次实踩）：FUSE 共享盘写后读有
    延迟窗口，rollback 刚 write_text 的扁平 JSON 可能读到半同步态（空/截断）。故损坏
    先短间隔重试，重试用尽仍损坏才判真损坏返回 None，由调用方降级（rollout 退「全新
    轨迹」——单臂静默退回第1天的污染路径，重试窗口宁大勿小）。文件不存在不重试：
    不存在是「没跑过」的正常语义，非竞态。
    """
    p = checkpoint_path(run_dir, arm_key, round_num)
    if not p.is_file():
        return None
    n_tries = max(1, int(retries))
    for attempt in range(n_tries):
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # pylint: disable=broad-except
            if attempt + 1 < n_tries:
                time.sleep(_FUSE_READ_DELAY)
    logger.warning("[checkpoint] 进度文件损坏（重试 %d 次×%.1fs 后仍坏），忽略"
                   "（arm=%s round=%s）：%s",
                   n_tries, _FUSE_READ_DELAY, arm_key, round_num, p)
    return None


def window_progress_path(checkpoint_dir, arm_key, window) -> Path:
    """窗口目录下的进度 JSON 路径：{checkpoint_dir}/checkpoint{window}/arm_{arm}/progress.json。

    与扁平 ``checkpoint_path`` 的分工：
    - 扁平 JSON（每夜覆盖、只留最新夜）→ ``read_progress`` 续跑检测用。
    - 窗口目录 JSON（窗口边界夜定格、不随后续夜覆盖）→ 回退续跑恢复 metrics_snapshot 用。
    """
    arm = arm_key or "eval"
    return Path(checkpoint_dir) / f"checkpoint{window}" / f"arm_{arm}" / "progress.json"


def read_window_progress(checkpoint_dir, arm_key, window) -> dict | None:
    """读窗口目录的进度 JSON。无文件/损坏/无 snapshot 返回 None。

    用于续跑回退路径：扁平 JSON 的 metrics_snapshot 是最新夜的（含窗口间已重跑的几天），
    不能直接用于回退到窗口边界；窗口目录这份是窗口边界夜定格的 snapshot，正好对得上
    回退后的 start_night（=W*K）。早期 run 的窗口目录 progress.json 无 metrics_snapshot
    字段（write_arm_graph_checkpoint 不写它）→ ``get("metrics_snapshot")`` 为 None，
    退回原有「前段指标从0重算」行为，向后兼容。
    """
    p = window_progress_path(checkpoint_dir, arm_key, window)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # pylint: disable=broad-except
        logger.warning("[checkpoint] 窗口进度文件损坏，忽略（arm=%s window=%s）：%s",
                       arm_key, window, p)
        return None


# ─────────── ARM 图谱窗口 checkpoint ───────────

def write_arm_graph_checkpoint(
    checkpoint_dir,
    arm_graph,
    arm_key: str,
    window: int,
    round_num: int,
    day_idx: int,
    last_reflection: str = "",
) -> Path | None:
    """窗口边界写私有图谱 checkpoint（框架层，任务域无关）。

    在 night_cb 里 _make_night_reflection 之后调用——此刻活图谱经验层已清（on_window 做的）、
    last_reflection 已生成，一次性写入完整的 checkpoint。

    产物目录：
        <checkpoint_dir>/checkpoint{window}/arm_{key}/
            private.db       私有图谱（记忆层快照）
            private_vec.db   私有向量库（同步清经验层后）
            skill.db         技能库快照（跨窗口常驻，续跑恢复用；2026-09-07 加）
            skill_vec.db     技能向量库（与 skill.db 配对，可能缺）
            progress.json    进度（含 last_reflection）
            progress.json    扁平进度副本（同目录自包含）

    同时更新 <checkpoint_dir>/checkpoint_{arm}_r{round}.json（供 rollout 续跑检测）。
    失败只 logger.exception 返回 None——checkpoint 是 IO 旁路，不拖当前轨迹。
    """
    checkpoint_dir = Path(checkpoint_dir)
    arm = arm_key or "eval"
    arm_dir = checkpoint_dir / f"checkpoint{window}" / f"arm_{arm}"
    arm_dir.mkdir(parents=True, exist_ok=True)

    # ① 私有图谱 + 向量：清经验层后的活图谱与活向量库已同步，snapshot_with_vectors 一次拿配对快照。
    private_db = arm_dir / "private.db"
    try:
        _, vec_path = arm_graph.snapshot_with_vectors(private_db)
    except Exception:  # pylint: disable=broad-except
        logger.exception("[checkpoint] 窗口%d 私有图谱快照失败（arm=%s）", window, arm)
        return None

    # ② 技能库快照（2026-09-07 修「续跑 skill.db 不进 checkpoint 致污染/丢失」）：
    #   skill.db 跨窗口常驻、不随聚合清层，但续跑/回退重跑必须能精确恢复到窗口 W 的态——
    #   否则续跑直接打开中断时的活 skill.db（含窗口 W 之后产出的"未来技能"），与从 private.db
    #   回退的记忆态错配；回退重跑清 skills/ 后更从空开始、窗口 W 之前技能全丢。窗口边界快照一份，
    #   续跑时由 brain.py 与 private.db 同窗口同源拷回（见 resume 分支）。NullGraph（no_graph 对照组）
    #   走 write_progress 分支不进本函数；防御性 getattr 兜底早期未挂 _skill_store 的 ArmGraph。
    skill_store = getattr(arm_graph, "_skill_store", None)
    if skill_store is not None:
        try:
            skill_db = arm_dir / "skill.db"
            _, skill_vec = skill_store.snapshot_with_vectors(skill_db)
            logger.info("[checkpoint] 窗口%d 技能库快照 → %s%s", window,
                        skill_db.relative_to(checkpoint_dir),
                        "" if skill_vec else "（⚠️ 无向量库）")
        except Exception:  # pylint: disable=broad-except
            # 与图谱快照同口径：IO 旁路，失败不拖当前轨迹，只缺该窗口的技能续跑点。
            logger.exception("[checkpoint] 窗口%d 技能库快照失败（arm=%s）", window, arm)

    # ③ 扁平进度 JSON + 副本进 arm_dir（传 last_reflection，窗口 progress.json 一次写完整）
    write_progress(checkpoint_dir, arm, round_num, day_idx, last_reflection=last_reflection)
    prog_src = checkpoint_path(checkpoint_dir, arm, round_num)
    if prog_src.is_file():
        try:
            shutil.copy2(prog_src, arm_dir / "progress.json")
        except Exception:  # pylint: disable=broad-except
            logger.debug("[checkpoint] 窗口%d 拷 progress.json 失败（arm=%s）", window, arm)

    logger.info("[checkpoint] 窗口%d 私有 checkpoint 写入 → %s（day=%d%s）",
                window, arm_dir.relative_to(checkpoint_dir), day_idx,
                "" if vec_path else "（⚠️ 无向量库）")
    return arm_dir


# ─────────── 续跑恢复源判定 ───────────

def latest_window_with_world(checkpoint_dir, arm_key: str) -> int | None:
    """扫 checkpoint 目录，返回**带 world/ 存档的最近窗口号** W（无则 None）。

    续跑缺陷②（2026-08-19）用：SIGTERM/SIGINT 中途停时活存档会被一次"死亡重生时刻的
    save_world"覆盖、库存丢失。续跑不读活存档，而是从最近窗口边界的
    `checkpoint{W}/arm_{key}/world/`（窗口边界 unfreeze 后 _snapshot_world 拷的干净存档）恢复。
    续跑起点回退到 W*aggregate_every（重跑窗口之间的几天，最多 K-1 天，换库存不丢）。

    ★ 2026-08-31 从 domains/minecraft 提升为框架层公共函数：活图谱恢复（见
    ``resume_graph_source``）与 world 恢复都要"找带 world/ 的最近窗口"，窗口号 W 必须只有
    一个来源，否则记忆与世界状态会取到不同窗口、错配。两个调用点都调本函数。
    """
    arm = arm_key or "eval"
    base = Path(checkpoint_dir)
    best = None
    for d in base.glob("checkpoint[0-9]*"):
        # 目录名形如 checkpoint1/checkpoint2 …，取尾部数字
        m = re.search(r"(\d+)$", d.name)
        if not m:
            continue
        w = int(m.group(1))
        if (d / f"arm_{arm}" / "world").is_dir():
            if best is None or w > best:
                best = w
    return best


def resume_sources(checkpoint_dir, arm_key: str, round_num: int):
    """续跑恢复源统一判定（框架层，任务域无关）。

    返回 ``(is_resume, W, paths)``：
      - ``is_resume`` (bool)：本 ``(arm_key, round_num)`` 是否有**未完成**的 checkpoint
        （= 续跑轮）。False 表示全新轨迹或上轮已完成。
      - ``W`` (int | None)：恢复源窗口号，唯一来源 ``latest_window_with_world``。
        is_resume=True 但无窗口快照可恢复时为 None（第一窗口前停 / 早期 run 无 world 快照）。
      - ``paths`` (dict)：同窗口 ``checkpoint{W}/`` 下三者的源路径，各自 ``.is_file()`` 判
        存在、不存在置 None。W 为 None 时全 None。
          ``{"private": checkpoint{W}/arm_{key}/private.db,
             "skill":   checkpoint{W}/arm_{key}/skill.db,
             "public":  checkpoint{W}/public.db}``

    ★ **三者同源**（2026-09-07）：活图谱 private.db / 技能 skill.db / 主脑 public.db 复用
      同一个窗口 W、同一个 ``checkpoint{W}/`` 目录——保证记忆 / 技能 / 主脑取自同一窗口、
      不错配。public.db 在窗口根（所有臂聚合的产物），private.db/skill.db 在 ``arm_{key}/``
      子目录，但三者窗口号 W 同源（同一次窗口边界聚合时一起写）。W 唯一来源
      ``latest_window_with_world``，与 ``_fresh_world`` 的 world 恢复逐字同源。

    判定（与原 ``resume_graph_source`` 一致，仅多返回 W + 全三路径）：
      1. ``read_progress`` 返回 None（扁平 JSON 不存在/损坏）→ ``(False, None, {全None})``：全新轨迹。
      2. ``ckpt.complete`` 为 True → ``(False, None, {全None})``：上轮已完成，正常新跑（不清旧档，
         与 rollout 的 complete 分支一致）。
      3. 否则 ``is_resume=True``。``W = latest_window_with_world(...)``：
         - W 为 None（第一窗口前停 / 早期 run 无 world 快照）→ ``(True, None, {全None})``：
           调用方应清残留活库从空记忆续跑，不带着上次 run 的污染继续。
         - 否则填三路径（各自判存在；早期 run 的窗口目录可能只有 snap.db 无 private.db /
           本改动前无 skill.db → 对应键 None）。

    ★ 边界：停在窗口边界（``n_nights_done == W*K``）时 rollout 的 ``_resume_window`` 保持
      None（不进回退分支），但本函数**仍返回** ``(True, W, {...})``——第 W*K 夜就是窗口 W
      边界，快照正是那夜聚合清经验层后的定格，活库必须从它恢复。
    """
    ckpt = read_progress(checkpoint_dir, arm_key, round_num)
    empty = {"private": None, "skill": None, "public": None}
    if ckpt is None or ckpt.get("complete"):
        return False, None, empty
    w = latest_window_with_world(checkpoint_dir, arm_key)
    if w is None:
        return True, None, empty
    arm = arm_key or "eval"
    win_dir = Path(checkpoint_dir) / f"checkpoint{w}"
    arm_dir = win_dir / f"arm_{arm}"
    private = arm_dir / "private.db"
    skill = arm_dir / "skill.db"
    public = win_dir / "public.db"
    return True, w, {
        "private": private if private.is_file() else None,
        "skill": skill if skill.is_file() else None,
        "public": public if public.is_file() else None,
    }


def rollback_to_window(checkpoint_dir, arm_keys, window: int, resume_round: int,
                       *, backup_dir=None) -> dict:
    """从指定窗口 N 恢复：备份并删 N+1..末 窗口目录 + 回退各臂扁平 JSON 到 N 边界夜。

    ★ ``--resume-window N`` 的落地（2026-09-07）：续跑恢复源不必是最后一个窗口——用户可指定
      任意 checkpointN 恢复。本函数在 run.py 启动时、构造 OpenKAGE 之前调用，把 N 之后的窗口
      清掉 + 扁平 JSON 回退到 N 边界夜，让 ``latest_window_with_world`` 自然取到 N、三者
      （private/skill/public）从 ``checkpoint{N}/`` 同源恢复。清掉 N 之后是为了不影响续跑后
      的落盘（否则新跑的 N+1.. 会和旧的同号窗口混在一起）。

    步骤：
      1. 校验 N：``checkpoint{N}/arm_{arm}/world/`` 必须存在（否则 latest_window_with_world
         取不到 N、会取 <N 的窗口错配）→ 缺则 ValueError。N >= 当前最大窗口（没东西可清）
         → 跳过删除，仅回退扁平 JSON（等于正常续跑，warn 由调用方记）。
      2. 备份：``checkpoint{N+1}..末`` 整个 copytree 到 ``backup_dir``（默认
         ``<checkpoint_dir>/_rollback_backup_<N>/``）。备份失败 → ValueError，不删（破坏性
         操作必须先备份成功）。
      3. 删除：``rmtree(checkpoint{N+1}..末)``。
      4. 回退扁平 JSON：对每个 arm，读 ``checkpoint{N}/arm_{arm}/progress.json``（窗口边界夜
         定格的 snapshot），改写 ``round_num=resume_round`` + ``arm_key=arm``，写到
         ``checkpoint_{arm}_r{resume_round}.json``（覆盖原扁平 JSON）。
         ★ round 对齐：文件名与 round_num 字段都用 resume_round（= ``_encode_round(start_epoch,1)``），
           保证续跑 ``read_progress(workdir, arm, resume_round)`` 命中。单 epoch 天然一致，
           多 epoch 强制对齐。

    返回 ``{"deleted_windows": [N+1,...], "rolled_back_arms": [...], "backed_up_to": path}``。
    """
    import shutil as _shutil

    base = Path(checkpoint_dir)
    arm_keys = list(arm_keys)
    n = int(window)

    # ① 校验 N：每个臂都要有 checkpoint{N}/arm_{arm}/world/ + progress.json
    for arm in arm_keys:
        arm_dir = base / f"checkpoint{n}" / f"arm_{arm}"
        if not (arm_dir / "world").is_dir():
            raise ValueError(
                f"窗口{n} 的 arm_{arm}/world/ 不存在——latest_window_with_world 取不到 N，"
                f"会取到 <N 的窗口错配。请选一个带 world 存档的窗口（通常 N>=1）。")
        if not (arm_dir / "progress.json").is_file():
            raise ValueError(
                f"窗口{n} 的 arm_{arm}/progress.json 不存在——无法回退扁平 JSON 到 N 边界夜。")

    # ② 找 > N 的窗口目录
    later_windows = []
    for d in base.glob("checkpoint[0-9]*"):
        m = re.search(r"(\d+)$", d.name)
        if m and int(m.group(1)) > n:
            later_windows.append(int(m.group(1)))
    later_windows.sort()

    # ③ 备份 + 删除 > N 的窗口
    bk = Path(backup_dir) if backup_dir else (base / f"_rollback_backup_{n}")
    if later_windows:
        bk.mkdir(parents=True, exist_ok=True)
        for w in later_windows:
            src = base / f"checkpoint{w}"
            dst = bk / f"checkpoint{w}"
            try:
                _shutil.copytree(src, dst)
            except Exception as exc:  # pylint: disable=broad-except
                raise ValueError(
                    f"备份窗口{w} 失败（{exc}）——未删除任何窗口，中止。请检查磁盘/权限。") from exc
        for w in later_windows:
            _shutil.rmtree(base / f"checkpoint{w}", ignore_errors=False)
        logger.info("[checkpoint] rollback_to_window(%d)：备份 %d 个窗口到 %s 并删除",
                    n, len(later_windows), bk.name)

    # ④ 回退各臂扁平 JSON 到 N 边界夜
    rolled = []
    rolled_payloads = []  # (arm → 刚写的 data)，供第⑤步屏障做内容比对
    for arm in arm_keys:
        win_prog_path = base / f"checkpoint{n}" / f"arm_{arm}" / "progress.json"
        try:
            data = json.loads(win_prog_path.read_text(encoding="utf-8"))
        except Exception as exc:  # pylint: disable=broad-except
            raise ValueError(
                f"读窗口{n}/arm_{arm}/progress.json 失败（{exc}）——无法回退扁平 JSON。") from exc
        data["round_num"] = int(resume_round)
        data["arm_key"] = arm
        flat_path = checkpoint_path(base, arm, resume_round)
        flat_path.parent.mkdir(parents=True, exist_ok=True)
        flat_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        rolled.append(arm)
        rolled_payloads.append(data)
        logger.info("[checkpoint] rollback_to_window(%d)：arm=%s 扁平JSON 回退到第%d夜（round=%d）",
                    n, arm, data.get("n_nights_done"), resume_round)

    # ⑤ FUSE 写后读可见性屏障（0912 实踩修复，0913 升级）：
    #   write_text 返回 ≠ 对后续读者可见——FUSE 共享盘写后读有延迟窗口，rollback 串行
    #   写完 5 臂扁平 JSON 后，紧接的 brain 层（resume_sources）与 rollout 并行读，某臂
    #   可能读到半同步态 → json.loads 抛异常 → 判「进度文件损坏」→ 误走「全新轨迹」
    #   分支（世界重生/自然出生/"今天是第1天"），单臂静默退回第1天、5 臂夜数撕裂。
    #   ★ 0913 复发教训：原屏障只做「read_progress 非 None」瞬时校验——它通过后
    #   0.6~3.2s，brain/rollout 再读仍半同步（可见性非单调，延迟窗口跨屏障存活）。
    #   故：①重试已下沉 read_progress 本身（见其 docstring）；②屏障升级为「内容比对」
    #   ——读回的 JSON 必须与刚写入的 data 逐字段相等（防半同步态里碰巧可解析的
    #   旧内容/半截内容骗过瞬时校验），比对失败同样短间隔重试，用尽 raise（不静默
    #   降级，宁可起跑失败也不要单臂污染整批）。
    for arm, expect in zip(rolled, rolled_payloads):
        flat = checkpoint_path(base, arm, resume_round)
        for attempt in range(_FUSE_READ_RETRIES):
            got = read_progress(base, arm, resume_round, retries=1)
            if got == expect:
                break
            time.sleep(_FUSE_READ_DELAY)
        else:
            # 重试用尽仍读不回刚写的内容——文件落盘内容本身正确（write_text 已返回），
            # 这只是 FUSE 可见性；持续不可见大概率是共享盘异常，应人工介入而非猜测。
            raise RuntimeError(
                f"rollback_to_window({n})：arm={arm} 扁平 JSON 写后读回校验失败（FUSE 可见性）——"
                f"文件已写但读回内容与刚写的不一致，疑似共享盘异常，请人工检查 {flat}")

    return {"deleted_windows": later_windows, "rolled_back_arms": rolled,
            "backed_up_to": str(bk) if later_windows else None}


def resume_graph_source(checkpoint_dir, arm_key: str, round_num: int):
    """续跑活图谱恢复源判定（薄封装 ``resume_sources`` 取 ``private`` 键）。

    返回 ``(is_resume, src_private_db)``——保持原签名向后兼容现有调用点/测试。
    ★ 2026-09-07 三者同源改造后，判定逻辑统一在 ``resume_sources``（返回同窗口 W 的
      private/skill/public 三路径）。新代码请直接用 ``resume_sources`` 取所需键。
    语义见 ``resume_sources`` 的 docstring（``src_private_db`` = 其 ``paths["private"]``）。
    """
    is_resume, _w, paths = resume_sources(checkpoint_dir, arm_key, round_num)
    return is_resume, paths["private"]


def truncate_curve_history(run_json_path, window: int, aggregate_every: int = None) -> dict | None:
    """rollback 后截断 run.json 的曲线历史到窗口 N 边界（与 checkpoint 回退同边界）。

    ★ 2026-09-12：``rollback_to_window`` 只动 checkpoint 文件（窗口目录 + 扁平
    ``checkpoint_<arm>_r*.json``），顶层 run.json 的曲线 list 不在它职责内、原样残留。
    续跑后 ``consolidation_log`` 的 ``window > N`` 条目会与新跑数据撞号（新窗口 N+1）。
    本函数按窗口 N 边界截断，让 run.json 曲线历史与 checkpoint 回退同口径。

    - ``consolidation_log``：删 ``window > N`` 的条目（``window`` 是独立字段）。
    - ``train_curve``：**不动**。epoch 是跨进程连续的绝对编号（见 brain.py ``run`` 的
      ``start_epoch`` 语义），rollback 回退窗口不回退 epoch，曲线点原样保留。
    - ``daily_metrics`` 已删除（2026-09-12）：逐夜指标只存各 arm checkpoint 的
      ``night_reflections``（本身随扁平 JSON 回退、无需另截），主 JSON 不再带该字段。
      旧 run 的 JSON 里若残留此字段，原样保留不动（不影响续跑）。

    健壮性：run.json 不存在 / 损坏 → 返回 None，不炸（向后兼容）。

    返回 ``{"consolidation_log": list, "train_curve": list, "consolidation_log_kept": int,
    "train_curve_untouched": int}``，供调用方记日志 + 同步内存 ``resume_data``（截断改的是
    磁盘，调用方已读入的内存副本需同步，否则铺回 ``octo.*`` 的仍是未截断旧 list）。
    """
    p = Path(run_json_path)
    if not p.is_file():
        logger.warning("[checkpoint] truncate_curve_history：run.json 不存在，跳过：%s", p)
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("[checkpoint] truncate_curve_history：run.json 损坏，跳过（%s）：%s", exc, p)
        return None

    n = int(window)

    # consolidation_log：按 window 截
    cl = list(data.get("consolidation_log") or [])
    cl_new = [c for c in cl if int(c.get("window", 0)) <= n]
    data["consolidation_log"] = cl_new

    # train_curve：不动（epoch 绝对编号）
    tc = list(data.get("train_curve") or [])

    try:
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:  # pylint: disable=broad-except
        logger.exception("[checkpoint] truncate_curve_history：写回 run.json 失败（%s）：%s",
                         exc, p)
        return None

    logger.info(
        "[checkpoint] truncate_curve_history(窗口%d)：consolidation_log %d→%d、"
        "train_curve 不动(%d点)",
        n, len(cl), len(cl_new), len(tc))
    return {
        "consolidation_log": cl_new,
        "train_curve": tc,
        "consolidation_log_kept": len(cl_new),
        "train_curve_untouched": len(tc),
    }
