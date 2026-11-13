#!/usr/bin/env python3
"""Read per-day metrics from the flat per-agent checkpoints.

The authoritative source is
``results/<run>/checkpoint_<arm>_r*.json -> metrics_snapshot.night_reflections``,
a list of ``{day_idx, discoveries, reflection}`` entries. This module globs
the checkpoints, aggregates them per agent and flattens discoveries into rows
of the form ``{"arm": ..., "day_idx": ..., **discoveries}``.

Usage::

    from daily_log_loader import load_daily_rows, daily_by_arm
    rows = load_daily_rows(Path("results/<cell>/runNN"))   # flat, time-ordered
    by_arm = daily_by_arm(rows)                            # {arm: [ascending]}

Runs whose checkpoints have no night_reflections yield an empty list for that
agent; callers should treat it as missing data.
"""

from __future__ import annotations

import json
from pathlib import Path

ARMS = ["alice", "bob", "carol", "dave", "erin"]


def find_checkpoint_files(run_dir: Path) -> dict[str, Path]:
    """run_dir 下 checkpoint_<arm>_r*.json → {arm: 最新一份的 path}。

    取**字典序最大**（round 编号 Monotonic，最新轮 = 最全时序；续跑 run 的旧轮
    checkpoint 只含前段夜）。兼容顶层散放布局（2026-08-27 起唯一布局）。
    """
    run_dir = Path(run_dir)
    out: dict[str, Path] = {}
    for p in sorted(run_dir.glob("checkpoint_*_r*.json")):
        parts = p.stem.split("_")  # checkpoint_alice_r10101
        if len(parts) >= 3:
            arm = parts[1]
            out[arm] = p  # sorted() 后同 arm 后者覆盖 → 最新轮
    return out


def load_daily_rows(run_dir: Path, arms: list[str] | None = None) -> list[dict]:
    """逐夜 discoveries 扁平时序：[{arm, day_idx, **discoveries}, ...]。

    每臂取最新一份扁平 checkpoint 的 night_reflections， discoveries 展平成行（格式
    与已删除的 daily_metrics 行同构，消费端零改动）。arm 顺序按 ``arms``（缺省
    ARMS），行内按 day_idx 升序。某 arm 无 checkpoint → 该 arm 不出现在结果里。
    """
    run_dir = Path(run_dir)
    ckpts = find_checkpoint_files(run_dir)
    rows: list[dict] = []
    for arm in (arms or ARMS):
        p = ckpts.get(arm)
        if p is None:
            continue
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue  # checkpoint 损坏 → 该 arm 缺，不炸（画图脚本容忍部分臂缺失）
        ms = d.get("metrics_snapshot") or {}
        for nr in ms.get("night_reflections") or []:
            disc = nr.get("discoveries") or {}
            if "day_idx" not in nr:
                continue  # 老格式/空记录，跳过（与 brain.py 展平时的防御一致）
            rows.append({"arm": arm, "day_idx": nr["day_idx"], **disc})
    return rows


def daily_by_arm(rows: list[dict], arms: list[str] | None = None) -> dict[str, list[dict]]:
    """扁平时序 → {arm: [行按 day_idx 升序]}。arms 里的 arm 缺数据 → 空列表占位。"""
    out: dict[str, list[dict]] = {a: [] for a in (arms or ARMS)}
    for row in rows:
        arm = row.get("arm")
        if arm in out:
            out[arm].append(row)
    for a in out:
        out[a].sort(key=lambda x: x["day_idx"])
    return out


# 别名：plot 脚本里本地常已有同名 ``_daily_by_arm``，导入时用这个不易撞名
daily_by_arm_rows = daily_by_arm


def daily_by_arm_day(rows: list[dict], arms: list[str] | None = None) -> dict[str, dict[int, dict]]:
    """扁平时序 → {arm: {day_idx: 行}}（同 day 多行取后写者；正常一 arm 一天仅一行）。"""
    out: dict[str, dict[int, dict]] = {a: {} for a in (arms or ARMS)}
    for row in rows:
        arm = row.get("arm")
        if arm in out and row.get("day_idx") is not None:
            out[arm][row["day_idx"]] = row
    return out
