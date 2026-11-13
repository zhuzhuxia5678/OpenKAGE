#!/usr/bin/env python3
"""Recompute the paper's anchor metrics from one or more released run directories.

This is a *verification* tool for the anonymous release, not a second source of
truth for the paper: every formula here mirrors ``experiments/build_paper_tables.py``
(``arm_metrics`` / ``summarize`` / ``run_level`` / ``tex_row``), which remains the
single source for the numbers in the manuscript. It is deliberately self-contained
(standard library only, no scipy, no ``experiments`` package) so that it runs from
an unpacked release without installing the research repository.

Inputs are run directories in the release layout, i.e. directories containing
``checkpoint_<arm>_r*.json``. Pass explicit paths, shell globs, or ``--cell NAME``
to read ``results/<cell>/run*``.

Examples::

    # one run
    python3 experiments/paper_metrics.py results/ds_openkage/run01

    # a whole paper cell, aggregated exactly as Table 1 pools a cell
    python3 experiments/paper_metrics.py --cell ds_openkage --summary

    # every run of a cell, one row each, LaTeX rows comparable to the appendix
    python3 experiments/paper_metrics.py --cell ds_openkage --format latex

    # machine-readable, including internal diagnostic columns
    python3 experiments/paper_metrics.py --cell abl_raw_shared_pool --format json --all
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import statistics as st
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RESULTS = ROOT / "results"

_ARM_ORDER = ["alice", "bob", "carol", "dave", "erin", "frank", "greg", "helen"]
_AGENTS = set(_ARM_ORDER)
_MAT_SCORE = {"leather": 1, "golden": 2, "gold": 2, "chainmail": 2, "copper": 2,
              "iron": 3, "turtle": 3, "diamond": 4, "netherite": 5}


def _arms_of(run_dir: Path) -> list[str]:
    names = set()
    for p in glob.glob(str(run_dir / "checkpoint_*_r*.json")):
        names.add(Path(p).name[len("checkpoint_"):].split("_r")[0])
    if not names:
        return []
    return [a for a in _ARM_ORDER if a in names] + sorted(n for n in names if n not in _ARM_ORDER)


def _load_arm(run_dir: Path, arm: str):
    cps = sorted(glob.glob(str(run_dir / f"checkpoint_{arm}_r*.json")))
    return json.loads(Path(cps[-1]).read_text(encoding="utf-8")) if cps else None


def _equip_score(eq) -> float:
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


def _era_of(items: list[str], tool_tier: str | None) -> str:
    if any(i.startswith("diamond_") for i in items) or tool_tier == "diamond":
        return "dmd."
    if any(i.startswith("iron_") for i in items) or tool_tier == "iron":
        return "iron"
    if any(i.startswith("stone_") for i in items):
        return "stone"
    return "wooden"


def _dist(a, b):
    try:
        return math.dist((a["x"], a["y"], a["z"]), (b["x"], b["y"], b["z"]))
    except (TypeError, KeyError):
        return math.inf


def arm_metrics(run_dir: Path, arm: str) -> dict | None:
    """Per-agent quantities; mirrors experiments.main_table_metrics.arm_metrics
    plus the Repeat / skill-usage fields from explore_paper_metrics.arm_features."""
    d = _load_arm(run_dir, arm)
    if d is None:
        return None
    ms = d.get("metrics_snapshot") or {}
    nrs = ms.get("night_reflections") or []
    deaths = ms.get("deaths") or []
    disc = (nrs[-1].get("discoveries") or {}) if nrs else {}
    items = disc.get("items_so_far") or []

    eq_max = 0.0
    obsidian_night = diamond_pick_night = None
    for n in nrs:
        dd = n.get("discoveries") or {}
        eq_max = max(eq_max, _equip_score(dd.get("equipment")))
        it = dd.get("items_so_far") or []
        if obsidian_night is None and "obsidian" in it:
            obsidian_night = n.get("day_idx")
        if diamond_pick_night is None and "diamond_pickaxe" in it:
            diamond_pick_night = n.get("day_idx")

    era = _era_of(items, disc.get("tool_tier"))
    era_night = None
    for n in nrs:
        dd = n.get("discoveries") or {}
        if _era_of(dd.get("items_so_far") or [], dd.get("tool_tier")) == era:
            era_night = n.get("day_idx")
            break

    nether_adv = next((a for a in (ms.get("advancements") or [])
                       if isinstance(a, dict) and a.get("advancement") == "[We Need to Go Deeper]"), None)
    adv_names = {a.get("advancement") for a in (ms.get("advancements") or []) if isinstance(a, dict)}
    adv_names.discard(None)

    # Repeat: same cause within 16 blocks of any earlier death of this agent.
    repeat = 0
    for i, dd in enumerate(deaths):
        p = dd.get("position") if isinstance(dd, dict) else None
        if not p:
            continue
        for e in deaths[:i]:
            if not isinstance(e, dict):
                continue
            q = e.get("position")
            if q and e.get("cause") == dd.get("cause") and _dist(p, q) <= 16:
                repeat += 1
                break

    # Skill usage: skill_calls per day, skill_sources (cumulative) for authorship.
    src = {}
    for n in nrs:
        ss = (n.get("discoveries") or {}).get("skill_sources")
        if isinstance(ss, dict):
            src.update(ss)
    calls = foreign = foreign_ok = 0
    for n in nrs:
        sc = (n.get("discoveries") or {}).get("skill_calls")
        if not isinstance(sc, dict):
            continue
        for name, r in sc.items():
            if not isinstance(r, dict):
                continue
            c = (r.get("ok") or 0) + (r.get("fail") or 0)
            calls += c
            if src.get(name) in _AGENTS and src[name] != arm:
                foreign += c
                foreign_ok += r.get("ok") or 0

    xp_total = disc.get("xp_total")
    return {
        "arm": arm,
        "n_nights": d.get("n_nights_done"),
        "complete": d.get("complete"),
        "deaths": len(deaths),
        "starve": sum(1 for x in deaths if isinstance(x, dict) and x.get("cause") == "death.attack.starve"),
        "drown": sum(1 for x in deaths if isinstance(x, dict) and x.get("cause") == "death.attack.drown"),
        "xp_total": xp_total,
        "xp_per_life": (xp_total / (len(deaths) + 1)) if xp_total is not None else None,
        "eq_max": eq_max,
        "era": era,
        "era_night": era_night,
        "obsidian_night": obsidian_night,
        "nether": "[We Need to Go Deeper]" in adv_names,
        "nether_night": nether_adv.get("day_idx") if nether_adv else None,
        "diamond_pick_night": diamond_pick_night,
        "adv": len(adv_names),
        "repeat": repeat,
        "skill_calls": calls,
        "skill_foreign": foreign,
        "foreign_ok": foreign_ok,
    }


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return st.mean(xs) if xs else None


def summarize(arms: list[dict]) -> dict:
    """Cell-level aggregation over agents; identical to build_paper_tables.summarize."""
    n = len(arms)
    eqs = [a["eq_max"] for a in arms]
    eq_peak = max(eqs)
    best_era = "dmd." if any(a["era"] == "dmd." for a in arms) else max(
        {a["era"] for a in arms}, key=lambda e: {"wooden": 0, "stone": 1, "iron": 2}[e])
    era_arms = [a for a in arms if a["era"] == best_era]
    obs = [a["obsidian_night"] for a in arms if a["obsidian_night"] is not None]
    neth = [a["nether_night"] for a in arms if a["nether"]]
    return {
        "n_agents": n,
        "deaths": st.mean(a["deaths"] for a in arms),
        "starve": sum(a["starve"] for a in arms),
        "drown": sum(a["drown"] for a in arms),
        "xpl": _mean([a["xp_per_life"] for a in arms]),
        "eq_peak": eq_peak, "eq_k": sum(1 for e in eqs if e >= eq_peak),
        "era": best_era, "era_k": len(era_arms),
        "era_day": _mean([a["era_night"] for a in era_arms]),
        "obs_k": len(obs), "obs_day": _mean(obs),
        "neth_k": len(neth), "neth_day": _mean(neth),
        "adv": _mean([a["adv"] for a in arms]),
        "repeat": st.mean(a["repeat"] for a in arms),
    }


def run_level(runs: list[list[dict]]) -> dict:
    """Quantities that need the run structure; identical to build_paper_tables.run_level."""
    calls = sum(a["skill_calls"] for r in runs for a in r)
    foreign = sum(a["skill_foreign"] for r in runs for a in r)
    fok = sum(a["foreign_ok"] for r in runs for a in r)
    all3 = sum(1 for r in runs if all(a["diamond_pick_night"] is not None for a in r))
    return {
        "worst_xpl": st.mean(min(a["xp_per_life"] for a in r) for r in runs),
        "foreign_share": foreign / calls if calls else 0.0,
        "foreign_ok": fok / foreign if foreign else 0.0,
        "all3_dpick": all3,
        "n_runs": len(runs),
    }


# ---------- formatting ----------

def _day(day, k, n) -> str:
    return "---" if k == 0 else f"{day:.1f} day ({k}/{n})"


def _era(s) -> str:
    return f"{s['era']} ({s['era_day']:.1f} day, {s['era_k']}/{s['n_agents']})"


def latex_row(label: str, s: dict) -> str:
    """Byte-compatible with build_paper_tables.tex_row (appendix per-run tables)."""
    n = s["n_agents"]
    def lday(day, k):
        return "---" if k == 0 else f"${day:.1f}\\,\\mathrm{{day}}\\,({k}/{n})$"
    era = (f"${s['era']}\\,({s['era_day']:.1f}\\,\\mathrm{{day}},\\,"
           f"{s['era_k']}/{n})$")
    return (f"{label} & ${s['deaths']:.1f}$ & ${s['xpl']:.1f}$ & {era} & "
            f"{lday(s['obs_day'], s['obs_k'])} & {lday(s['neth_day'], s['neth_k'])} \\\\")


BASE_COLS = ["run", "agents", "deaths", "repeat", "xp_life", "tool_era",
             "obsidian", "nether", "worst_xp_life", "others_skills", "all3_dpick"]
EXTRA_COLS = ["adv", "eq_max", "starve", "drown"]
HEADERS = {
    "run": "Run", "agents": "Agents", "deaths": "Deaths", "repeat": "Repeat",
    "xp_life": "XP/life", "tool_era": "Tool era", "obsidian": "Obsidian",
    "nether": "Nether", "worst_xp_life": "Weakest XP/l",
    "others_skills": "Others' skills", "all3_dpick": "All-3 dmd.",
    "adv": "Adv's", "eq_max": "Eq-max", "starve": "Starve", "drown": "Drown",
}


def row_values(label: str, s: dict, rl: dict, extra: bool) -> dict:
    v = {
        "run": label,
        "agents": str(s["n_agents"]),
        "deaths": f"{s['deaths']:.1f}",
        "repeat": f"{s['repeat']:.1f}",
        "xp_life": f"{s['xpl']:.1f}",
        "tool_era": _era(s),
        "obsidian": _day(s["obs_day"], s["obs_k"], s["n_agents"]),
        "nether": _day(s["neth_day"], s["neth_k"], s["n_agents"]),
        "worst_xp_life": f"{rl['worst_xpl']:.1f}",
        "others_skills": f"{100 * rl['foreign_share']:.0f}% ({100 * rl['foreign_ok']:.0f}% ok)",
        "all3_dpick": f"{rl['all3_dpick']}/{rl['n_runs']}",
    }
    if extra:
        v.update({
            "adv": f"{s['adv']:.1f}",
            "eq_max": f"{s['eq_peak']:.0f} ({s['eq_k']}/{s['n_agents']})",
            "starve": str(s["starve"]),
            "drown": str(s["drown"]),
        })
    return v


# ---------- CLI ----------

def expand_inputs(args) -> list[tuple[str, Path]]:
    out: list[tuple[str, Path]] = []
    for pat in args.runs:
        hits = sorted(glob.glob(pat))
        for h in hits:
            p = Path(h)
            if p.is_dir():
                out.append((p.name, p))
    for cell in args.cell:
        root = args.results_root / cell
        for d in sorted(root.glob("run*")):
            if d.is_dir():
                out.append((f"{cell}/{d.name}", d))
    # de-duplicate by resolved path, keep first label
    seen, uniq = set(), []
    for label, p in out:
        k = str(p.resolve())
        if k not in seen:
            seen.add(k)
            uniq.append((label, p))
    return uniq


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("runs", nargs="*", help="run directories or globs")
    ap.add_argument("--cell", action="append", default=[], metavar="NAME",
                    help="read results/<cell>/run* (repeatable)")
    ap.add_argument("--results-root", type=Path, default=DEFAULT_RESULTS,
                    help=f"results root (default: {DEFAULT_RESULTS})")
    ap.add_argument("--summary", action="store_true",
                    help="pool all input runs into one cell-level row")
    ap.add_argument("--label", default="Cell", help="label for --summary / --format latex")
    ap.add_argument("--format", choices=["table", "tsv", "json", "latex"], default="table")
    ap.add_argument("--all", action="store_true", help="add internal diagnostic columns")
    ap.add_argument("--nights", type=int, default=30, help="expected completed nights (default: 30)")
    args = ap.parse_args()

    inputs = expand_inputs(args)
    if not inputs:
        ap.error("no run directories found (pass paths, globs, or --cell NAME)")

    runs, incomplete = [], []
    for label, p in inputs:
        arms = [m for m in (arm_metrics(p, a) for a in _arms_of(p)) if m]
        if not arms:
            print(f"warning: {p} has no checkpoint_<arm>_r*.json", file=sys.stderr)
            continue
        bad = [a["arm"] for a in arms if not (a["complete"] and (a["n_nights"] or 0) >= args.nights)]
        if bad:
            incomplete.append((label, bad))
        runs.append((label, p, arms))

    cols = BASE_COLS + (EXTRA_COLS if args.all else [])
    rows = []
    if args.summary:
        all_arms = [a for _, _, arms in runs for a in arms]
        s = summarize(all_arms)
        rl = run_level([arms for _, _, arms in runs])
        rows.append((args.label, s, rl))
    else:
        for label, _, arms in runs:
            rows.append((label, summarize(arms), run_level([arms])))

    if args.format == "latex":
        for label, s, _ in rows:
            print(latex_row(label, s))
    elif args.format == "json":
        payload = {
            "n_runs": len(runs),
            "summary": args.summary,
            "rows": [row_values(l, s, rl, args.all) for l, s, rl in rows],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        vals = [row_values(l, s, rl, args.all) for l, s, rl in rows]
        widths = {c: max(len(HEADERS[c]), *(len(v[c]) for v in vals)) for c in cols}
        if args.format == "tsv":
            print("\t".join(HEADERS[c] for c in cols))
            for v in vals:
                print("\t".join(v[c] for c in cols))
        else:
            print("| " + " | ".join(HEADERS[c].ljust(widths[c]) for c in cols) + " |")
            print("|" + "|".join("-" * (widths[c] + 2) for c in cols) + "|")
            for v in vals:
                print("| " + " | ".join(v[c].ljust(widths[c]) for c in cols) + " |")

    for label, bad in incomplete:
        print(f"warning: {label} has incomplete arms {bad} (@N<{args.nights})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
