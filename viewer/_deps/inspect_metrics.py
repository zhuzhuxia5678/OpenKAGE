#!/usr/bin/env python3
"""Diagnostic per-run metrics, aggregated over the agents of a run.

All values read the latest night_reflections entry of each agent's checkpoint
(cumulative end-of-night snapshot); the eat/heal counts come from the per-arm
``eat_log_<Arm>.jsonl``. Definitions:

  1. Deaths        length of metrics_snapshot.deaths
  2. XP            discoveries.xp_total / xp_level
  3. Equipment     four-slot material score (leather 1 .. netherite 5, max 16)
  4. Tech score    max(tier_progress) (base advancement-tree depth 0-9)
  5. Tree depth    same as tech score; per-level rates from tier_progress_steps
  6. Meals         true eat events in eat_log (respawn resets filtered)
  7. Hunger restored  sum of food + saturation increments
  8. Health restored  sum of health increments from heal events

Unfinished runs work (checkpoints are written every window); when reflections
are missing, XP falls back to metrics_snapshot.xp_total and equipment/tech
report N/A. Missing eat logs count as zero.

Usage::

    python3 viewer/_deps/inspect_metrics.py <run_dir> [<run_dir> ...]
"""
from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

_ARMS = ["alice", "bob", "carol", "dave", "erin"]

# 装备材质分（与 item_table_html._MAT_SCORE 同口径）
_MAT_SCORE = {"leather": 1, "golden": 2, "gold": 2, "chainmail": 2, "copper": 2,
              "iron": 3, "turtle": 3, "diamond": 4, "netherite": 5}


def equip_score(eq) -> float:
    """4槽(头/胸/腿/脚)材质分之和，满分16(全钻石)。None/空=0。同画图工具口径。"""
    if not eq:
        return 0.0
    sc = 0.0
    for slot in ("head", "torso", "legs", "feet"):
        v = eq.get(slot)
        if v and isinstance(v, dict) and v.get("name"):
            for mat, val in _MAT_SCORE.items():
                if v["name"].startswith(mat):
                    sc += val
                    break
    return sc


def _tier_order(t):
    """工具/材质档位转可比整数（跨臂取最高档用）。"""
    order = {"none": 0, "wooden": 1, "wood": 1, "stone": 2, "iron": 3,
             "gold": 2, "golden": 2, "diamond": 4, "netherite": 5}
    return order.get(t, 0)


# ★ 进食/血量指标（2026-09-17）：从 eat_log_<Arm>.jsonl 逐事件聚合，量化「生存」。
#   eat_log 文件名是大写 bot_name（eat_log_Alice.jsonl），arm 字段同样大写，.lower() 映射
#   到 _ARMS 小写 key（同 plot_combined_ts._load_eat_log 口径）。容错照搬 load_place_log：
#   strip+lstrip("\\x00")+try/except+arm 白名单。
def _eat_log_metrics(run_dir: Path) -> dict[str, dict]:
    """读 eat_log_<Arm>.jsonl → {arm: {n_eats, food_restored, sat_restored,
    hunger_restored, health_healed}}。

    ★ 滤重生重置假事件（记忆 eat-log-collector 坑）：health_before<0.5 的事件是死亡后
    服务器重生重置（h:0→20、food/sat 顶满），非真进食/回血，一律不计。具体：
    death 行紧跟的 "eat" 行 h:0→20 f:15→20 sat:0→5 是重生重置被分类器误标 eat。
    口径：
      - n_eats        = 真 eat 事件数（health_before≥0.5）
      - food_restored = Σ max(0, food_after-food_before)  真 eat 事件
      - sat_restored  = Σ max(0, sat_after-sat_before)    真 eat 事件
      - hunger_restored = food_restored + sat_restored（用户定 food+sat 合计为「饥饿值」）
      - health_healed = Σ max(0, health_after-health_before)  heal 事件（饱和度驱动自然回血）
    MC 机制：吃食物只涨 food/sat、health 不变；回血全靠 heal 事件（因果两步，不混算）。
    """
    out: dict[str, dict] = {}
    for p in sorted(run_dir.glob("eat_log_*.jsonl")):
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip().lstrip("\x00").strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            arm = (r.get("arm") or "").lower()
            if arm not in _ARMS:
                continue
            if r.get("health_before") is None:
                continue
            # 滤重生重置：health_before<0.5 是死亡瞬间的服务器重置事件
            if r["health_before"] < 0.5:
                continue
            m = out.setdefault(arm, {"n_eats": 0, "food_restored": 0.0,
                                     "sat_restored": 0.0, "health_healed": 0.0})
            trg = r.get("trigger")
            if trg == "eat":
                m["n_eats"] += 1
                m["food_restored"] += max(0.0, (r.get("food_after") or 0) - (r.get("food_before") or 0))
                m["sat_restored"] += max(0.0, (r.get("sat_after") or 0) - (r.get("sat_before") or 0))
            elif trg == "heal":
                m["health_healed"] += max(0.0, (r.get("health_after") or 0) - (r.get("health_before") or 0))
    for m in out.values():
        m["hunger_restored"] = m["food_restored"] + m["sat_restored"]
    return out


def latest_snapshot(run_dir: Path) -> dict:
    """取一组 run 各 arm 末态快照，返回 {arm: {指标}} + meta(n_nights/complete)。"""
    per_arm = {}
    n_nights_list = []
    for arm in _ARMS:
        cps = sorted(run_dir.glob(f"checkpoint_{arm}_r*.json"))
        if not cps:
            per_arm[arm] = None
            continue
        d = json.loads(cps[-1].read_text(encoding="utf-8"))
        ms = d.get("metrics_snapshot", {})
        nrs = ms.get("night_reflections") or []
        # 末条 discoveries = 最新完成夜的累计快照
        disc = nrs[-1].get("discoveries", {}) if nrs else {}
        deaths = ms.get("deaths") or []
        tier_progress = ms.get("tier_progress") or []
        tier_rank = max(tier_progress) if tier_progress else 0
        # n_nights_done: 末条 night_reflections 的 day_idx
        n_done = nrs[-1].get("day_idx") if nrs else None
        if n_done is not None:
            n_nights_list.append(n_done)
        per_arm[arm] = {
            "n_deaths_cum": len(deaths),
            "xp_total": disc.get("xp_total") if disc else ms.get("xp_total"),
            "xp_level": disc.get("xp_level"),
            "tool_tier": disc.get("tool_tier"),
            "equip_score": equip_score(disc.get("equipment")) if disc else None,
            "tech_tier": tier_rank,            # 科技分 = base 进度树深度 0-9
            "unique_items": disc.get("unique_items_so_far"),
            "n_nights_done": n_done,
        }
    # ★ 进食/血量指标并表（2026-09-17）：eat_log 是事件级 jsonl，不在 checkpoint 里，
    #   单独聚合后按 arm 并进 per_arm。无 eat_log 的旧 run（2026-09-17 前）这些列为 0/缺。
    eat_m = _eat_log_metrics(run_dir)
    for arm in _ARMS:
        if per_arm.get(arm):
            per_arm[arm].update(eat_m.get(arm, {"n_eats": 0, "food_restored": 0.0,
                "sat_restored": 0.0, "hunger_restored": 0.0, "health_healed": 0.0}))
    n_nights = max(n_nights_list) if n_nights_list else None
    return {"per_arm": per_arm, "n_nights": n_nights}


def _fmt_mean(vals, fmt="{:.1f}"):
    v = [x for x in vals if x is not None]
    if not v:
        return "N/A"
    if all(isinstance(x, str) for x in v):
        # 字符串枚举（tool_tier）：跨臂取最高档
        return max(v, key=_tier_order)
    return fmt.format(statistics.mean(v))


def report(run_dir: Path) -> None:
    name = run_dir.name
    snap = latest_snapshot(run_dir)
    per_arm = snap["per_arm"]
    n_nights = snap["n_nights"]
    label = f"@N{n_nights}(未完成)" if n_nights else "@早期"

    print(f"=== {name[:60]} {label} (5arm均值) ===")
    # 各臂明细一行
    arms_ok = [a for a in _ARMS if per_arm[a]]
    if not arms_ok:
        print("  (无 checkpoint，第1夜未落盘)"); print(); return

    deaths = [per_arm[a]["n_deaths_cum"] for a in arms_ok]
    xp = [per_arm[a]["xp_total"] for a in arms_ok]
    xplvl = [per_arm[a]["xp_level"] for a in arms_ok]
    equip = [per_arm[a]["equip_score"] for a in arms_ok]
    tech = [per_arm[a]["tech_tier"] for a in arms_ok]
    uniq = [per_arm[a]["unique_items"] for a in arms_ok]
    tools = [per_arm[a]["tool_tier"] for a in arms_ok]
    # 进食/血量（2026-09-17，从 eat_log 聚合，滤重生重置）
    n_eats = [per_arm[a].get("n_eats", 0) for a in arms_ok]
    hunger = [per_arm[a].get("hunger_restored", 0.0) for a in arms_ok]
    healed = [per_arm[a].get("health_healed", 0.0) for a in arms_ok]

    print(f"  死亡(累计):  {_fmt_mean(deaths,'{:.1f}')}  各臂={deaths}")
    print(f"  XP(累计):    {_fmt_mean(xp,'{:.1f}')}  各臂={xp}")
    print(f"  XP等级:      {_fmt_mean(xplvl,'{:.1f}')}")
    print(f"  装备分(/16): {_fmt_mean(equip,'{:.1f}')}  各臂={equip}")
    print(f"  科技分(/9):  {_fmt_mean(tech,'{:.1f}')}  (base进度树深度)")
    print(f"  工具档(最高):{_fmt_mean(tools)}  各臂={tools}")
    print(f"  独特物品:    {_fmt_mean(uniq,'{:.1f}')}")
    print(f"  ── 生存(进食/血量，eat_log 聚合，滤重生重置) ──")
    print(f"  进食次数:    {_fmt_mean(n_eats,'{:.1f}')}  各臂={n_eats}")
    print(f"  恢复饥饿值:  {_fmt_mean(hunger,'{:.1f}')}  各臂={[round(x,1) for x in hunger]}  (food+sat合计)")
    print(f"  恢复血量:    {_fmt_mean(healed,'{:.1f}')}  各臂={[round(x,1) for x in healed]}  (heal自然回血)")
    print()


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit("用法: python experiments/inspect_metrics.py <run_dir> [<run_dir> ...]")
    for p in sys.argv[1:]:
        d = Path(p)
        if not d.is_dir():
            print(f"=== {p}: 目录不存在 ==="); print(); continue
        report(d)


if __name__ == "__main__":
    main()
