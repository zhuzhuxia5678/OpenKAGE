#!/usr/bin/env python3
"""Compute core per-run metrics for the release viewer.

The paper's Tables 1--3 report six columns: Deaths, Repeat, XP/life, Tool era,
Obsidian and Nether. This module supplies the checkpoint-level quantities used
by the viewer (Deaths, XP/life, Tool era, Obsidian, Nether, plus Adv's,
Eq-max and Starve as diagnostics). The full paper column set, Repeat, the
cell-level aggregation and the LaTeX rows are implemented in
``experiments/paper_metrics.py`` -- use that CLI to check numbers against the
manuscript. Per-run values are means over the agents of that run, with counts
of attaining agents; see the paper's appendix for the full definitions.

Usage::

    python3 viewer/_deps/main_table_metrics.py <run_dir> [<run_dir> ...]
    python3 viewer/_deps/main_table_metrics.py --tsv <run_dir>

Conventions:

- Deaths: mean count of ``metrics_snapshot.deaths`` per agent.
- XP/life: final cumulative XP divided by (deaths + 1), per agent first.
- Eq-max: highest four-slot equipment score reached on any night, with the
  number of agents reaching that peak.
- Tool era: highest tool tier (iron / dmd.), mean first-attainment day among
  attaining agents, and their count. Obsidian is reported separately; it is
  not a tool tier.
- Obsidian / Nether: mean first-attainment day with the number of attaining
  agents; an advancement marks Nether entry.
- Unfinished runs are computed too (checkpoints are written every window) but
  are flagged and are not comparable with completed 30-night runs.
"""
from __future__ import annotations

import argparse
import glob
import json
import statistics
import sys
from pathlib import Path

# 自引导：viewer 从 viewer/_deps 载入本模块；把本目录加进 sys.path 供同级 import。
sys.path.insert(0, str(Path(__file__).resolve().parent))

# 装备材质分与 inspect_metrics / item_table_html 同口径（不另立第二出处）
from inspect_metrics import equip_score  # noqa: E402

# ★ 臂名不再硬编码 5 个：§3.5 (b) 有 8 臂 run（alice..helen），写死前 5 个会让 8 臂 run
#   只统计 alice..erin → Deaths/XP/life 等「按人数归一」的列全是 5 臂口径，数字看着对其实是错的。
#   改从 run 目录 glob checkpoint_<arm>_r*.json 实测臂名；这里只保留**展示顺序**（未知名排最后）。
_ARM_ORDER = ["alice", "bob", "carol", "dave", "erin", "frank", "greg", "helen"]
_ARMS = _ARM_ORDER[:5]  # 兼容旧引用（无 checkpoint 时的回退）


def _arms_of(run_dir: Path) -> list[str]:
    """实测该 run 有几臂：glob 扁平 checkpoint_<arm>_r*.json，按 _ARM_ORDER 排序。"""
    names = set()
    for p in glob.glob(str(Path(run_dir) / "checkpoint_*_r*.json")):
        stem = Path(p).name[len("checkpoint_"):]
        names.add(stem.split("_r")[0])
    if not names:
        return list(_ARMS)
    known = [a for a in _ARM_ORDER if a in names]
    extra = sorted(n for n in names if n not in _ARM_ORDER)
    return known + extra
#: 科技档 → 可比整数（report 里取各臂最高档用）。2026-09-27 起不再产出 obsid. 档。
_ERA_ORDER = {"none": 0, "wooden": 1, "stone": 2, "iron": 3, "diamond": 4}


def _load_arm(run_dir: Path, arm: str):
    """取该臂最新一份 checkpoint 的 metrics_snapshot（扁平 checkpoint_<arm>_r*.json）。"""
    cps = sorted(glob.glob(str(run_dir / f"checkpoint_{arm}_r*.json")))
    if not cps:
        return None
    return json.loads(Path(cps[-1]).read_text(encoding="utf-8"))


def _era_of(items: list[str], tool_tier: str | None) -> str:
    """工具材质档：dmd. > iron > stone > wooden。

    ★ 2026-09-25 论文口径：`obsid.` 不再作为一级（"拿到黑曜石"归 Obsidian 列单独报告），
      2026-09-27 起本函数不再产出 obsid. —— 拿到 obsid. 的臂必然已有钻石，判为 dmd.。
    """
    if any(i.startswith("diamond_") for i in items):
        return "dmd."
    if any(i.startswith("iron_") for i in items):
        return "iron"
    if tool_tier in ("diamond",):
        return "dmd."
    if tool_tier in ("iron",):
        return "iron"
    if any(i.startswith("stone_") for i in items):
        return "stone"
    return "wooden"


def _full_iron_night(nrs: list[dict]) -> int | None:
    """首套满铁（四槽材质分 ≥12）的夜。没有则 None。"""
    for n in nrs:
        if equip_score(n.get("discoveries", {}).get("equipment")) >= 12:
            return n.get("day_idx")
    return None


def _cum_steps_by_night(nrs: list[dict]) -> dict[int, int]:
    """每夜**夜末**累计步数（Σ n_steps）——附录 per-agent 的 Iron pk (steps) 用夜末口径。"""
    out, cum = {}, 0
    for n in nrs:
        cum += (n.get("discoveries") or {}).get("n_steps") or 0
        out[n.get("day_idx")] = cum
    return out


def arm_metrics(run_dir: Path, arm: str) -> dict | None:
    d = _load_arm(run_dir, arm)
    if d is None:
        return None
    ms = d.get("metrics_snapshot") or {}
    nrs = ms.get("night_reflections") or []
    deaths = ms.get("deaths") or []
    disc = (nrs[-1].get("discoveries") or {}) if nrs else {}
    items = disc.get("items_so_far") or []

    eq_max, eq_night = 0.0, None
    obsidian_night = None
    for n in nrs:
        dd = n.get("discoveries") or {}
        sc = equip_score(dd.get("equipment"))
        if sc > eq_max:
            eq_max, eq_night = sc, n.get("day_idx")
        if obsidian_night is None and "obsidian" in (dd.get("items_so_far") or []):
            obsidian_night = n.get("day_idx")

    xp = disc.get("xp_total")
    advs = {a.get("advancement") for a in (ms.get("advancements") or [])}
    # ★ 下界首达夜（2026-09-29 补）：advancement 记录自带 day_idx，与铁镐夜同源口径。
    nether_adv = next((a for a in (ms.get("advancements") or [])
                       if a.get("advancement") == "[We Need to Go Deeper]"), None)
    # ★ 工具档首达夜（2026-09-29 补）：论文主表口径 = 每人"首次达到该档"的夜，
    #   对达到的人取均值 + 人数（附录 A.5 对 Tool era / Obsidian / Nether 三列同一口径）。
    era_final = _era_of(items, disc.get("tool_tier"))
    era_night = None
    for n in nrs:
        dd = n.get("discoveries") or {}
        if _era_of(dd.get("items_so_far") or [], dd.get("tool_tier")) == era_final:
            era_night = n.get("day_idx")
            break
    # 铁镐：成就那条记录自带 day_idx；步数取该夜**夜末**累计（校准：09-18 12:54 行 3/3 对上）
    iron_pk = next((a for a in (ms.get("advancements") or [])
                    if a.get("advancement") == "[Isn't It Iron Pick]"), None)
    cum_by_night = _cum_steps_by_night(nrs)
    diamond_night = obsidian_night_d = None
    for n in nrs:
        it = (n.get("discoveries") or {}).get("items_so_far") or []
        day = n.get("day_idx")
        if diamond_night is None and any(i == "diamond" or i.startswith("diamond_") for i in it):
            diamond_night = day
        if obsidian_night_d is None and "obsidian" in it:
            obsidian_night_d = day
    diamond_pick_night = None
    for n in nrs:
        it = (n.get("discoveries") or {}).get("items_so_far") or []
        if "diamond_pickaxe" in it:
            diamond_pick_night = n.get("day_idx")
            break
    return {
        "arm": arm,
        "n_nights": d.get("n_nights_done"),
        "complete": d.get("complete"),
        "deaths": len(deaths),
        "drown": sum(1 for x in deaths if isinstance(x, dict)
                     and x.get("cause") == "death.attack.drown"),
        "starve": sum(1 for x in deaths if isinstance(x, dict)
                      and x.get("cause") == "death.attack.starve"),
        "xp_total": xp,
        "xp_per_life": (xp / (len(deaths) + 1)) if xp is not None else None,
        "eq_max": eq_max,
        "eq_max_night": eq_night,
        "era": era_final,
        "era_night": era_night,
        "obsidian_night": obsidian_night,
        "nether": "[We Need to Go Deeper]" in advs,
        "nether_night": (nether_adv.get("day_idx") if nether_adv else None),
        "full_iron_night": _full_iron_night(nrs),
        "iron_pk_steps": (cum_by_night.get(iron_pk.get("day_idx")) if iron_pk else None),
        "iron_pk_night": (iron_pk.get("day_idx") if iron_pk else None),
        "diamond_night": diamond_night,
        "diamond_pick_night": diamond_pick_night,
    }


def _fmt(v, fmt="{:.1f}"):
    return "N/A" if v is None else fmt.format(v)


def report(run_dir: Path, tsv: bool = False) -> None:
    run_dir = Path(run_dir)
    rows = [m for m in (arm_metrics(run_dir, a) for a in _arms_of(run_dir)) if m]
    if not rows:
        print(f"=== {run_dir.name}: 无 checkpoint ===")
        return
    nights = max((r["n_nights"] or 0) for r in rows)
    complete = all(r["complete"] for r in rows)
    flag = "" if complete else f"  @N{nights}(未完成)"
    print(f"=== {run_dir.name}{flag} ===")

    deaths = [r["deaths"] for r in rows]
    drown = sum(r["drown"] for r in rows) / len(rows)
    starve = sum(r["starve"] for r in rows) / len(rows)
    xpl = [r["xp_per_life"] for r in rows if r["xp_per_life"] is not None]
    eqs = [r["eq_max"] for r in rows]
    eq_peak = max(eqs)
    era_best = max((r["era"] for r in rows),
                   key=lambda e: {"wooden": 0, "stone": 1, "iron": 2, "dmd.": 3, "obsid.": 4}[e])
    obs = [r["obsidian_night"] for r in rows if r["obsidian_night"] is not None]
    nether = sum(1 for r in rows if r["nether"])
    # ★ 工具档首达夜 / 下界首达夜（2026-09-29 补）：口径同 Obsidian —— 每人"首次达到"的夜，
    #   对达到的人取均值 + 人数（论文附录 A.5：Tool era / Obsidian / Nether 三列同一口径）。
    era_nights = [r["era_night"] for r in rows
                  if r["era"] == era_best and r["era_night"] is not None]
    neth_nights = [r["nether_night"] for r in rows if r["nether_night"] is not None]

    print(f"  Deaths ↓      {statistics.mean(deaths):.1f}   各臂={deaths}")
    print(f"  D/St ↓        {drown:.0f}/{starve:.0f}   (溺水/饿死，按人数归一；"
          f"原始合计 {sum(r['drown'] for r in rows)}/{sum(r['starve'] for r in rows)})")
    print(f"  XP/life ↑     {statistics.mean(xpl):.1f}   各臂={[round(x, 1) for x in xpl]}")
    print(f"  Eq-max ↑      {eq_peak:.0f} ({sum(1 for e in eqs if e >= eq_peak)}/{len(rows)})"
          f"   各臂峰值={[round(e) for e in eqs]}")
    # ★ 输出格式照抄论文主表 tab:main（2026-09-29 对齐，别自创格式）：
    #   Tool era `$dmd.\,(10.6\,\mathrm{day},\,9/9)$` → `dmd. (10.6 day, 3/3)`
    #   Obsidian `$22.2\,\mathrm{day}\,(6/9)$`       → `22.2 day (6/9)`
    #   无人达成一律 `---`（论文 baseline 行就是这样写的）。
    print(f"  Tool era ↑    {era_best} ({statistics.mean(era_nights):.1f} day, "
          f"{sum(1 for r in rows if r['era'] == era_best)}/{len(rows)})"
          f"   各臂={[r['era'] for r in rows]} 各臂夜={[r['era_night'] for r in rows]}"
          if era_nights else
          f"  Tool era ↑    {era_best} (--- day, "
          f"{sum(1 for r in rows if r['era'] == era_best)}/{len(rows)})")
    print(f"  Obsidian ↓    {statistics.mean(obs):.1f} day ({len(obs)}/{len(rows)})" if obs
          else f"  Obsidian ↓    ---")
    print(f"  Nether ↑      {statistics.mean(neth_nights):.1f} day ({nether}/{len(rows)})"
          f"   各臂夜={[r['nether_night'] for r in rows]}" if neth_nights
          else f"  Nether ↑      ---")
    print("  ── 分臂（对应附录 per-agent 表）──")
    for r in rows:
        print(f"    {r['arm']:6s} 死亡{str(r['deaths']):>3s} "
              f"溺水{str(r['drown']):>2s} 饿死{str(r['starve']):>2s} "
              f"XP/life {_fmt(r['xp_per_life']):>5s} "
              f"满铁夜 {str(r['full_iron_night']):>4s} "
              f"铁镐夜 {str(r['iron_pk_night']):>4s} "
              f"铁镐步 {str(r['iron_pk_steps']):>5s} "
              f"钻夜 {str(r['diamond_night']):>4s} "
              f"钻镐夜 {str(r['diamond_pick_night']):>4s} "
              f"黑曜夜 {str(r['obsidian_night']):>4s} "
              f"档夜 {str(r['era_night']):>4s} "
              f"下界夜 {str(r['nether_night']):>4s}")
    print()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("runs", nargs="+")
    ap.add_argument("--tsv", action="store_true")
    args = ap.parse_args()
    for p in args.runs:
        d = Path(p)
        if not d.is_dir():
            print(f"=== {p}: 目录不存在 ===")
            continue
        report(d, args.tsv)


if __name__ == "__main__":
    main()
