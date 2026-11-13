#!/usr/bin/env python3
"""Regenerate the paper's result figures from the released run data.

All inputs and outputs live inside this package; no external paths are needed.

Inputs:
  - ``paper/iclr2027/tables/daily_series.json``  per-run, per-day series
  - ``paper/iclr2027/tables/skill_usage_figdata.json``  skill-usage data for Figure 11(b,c)
  - ``results/<cell>/runNN/checkpoint_<agent>_r*.json``  raw checkpoints for Figure 6 / 12
  - ``results/adv_icons/``  milestone icons drawn on the curves

Outputs: ``paper/iclr2027/fig/<name>.svg`` and ``<name>.pdf``. SVG text stays
editable; PDF conversion uses Inkscape.

Figures (exactly those included in the paper):
  fig_death_causes         Figure 5:  deaths per agent by cause, four backbones
  fig_transfer             Figure 6:  knowledge transfer (3 runs per group)
  fig_runs                 Figure 9:  per-run scatter
  fig_death_rate           Figure 10: daily death rate
  fig_knowledge            Figure 11: skill-library growth, call outcomes, revisions
  fig_scaling              Figure 12: group size and 60-day horizon

Usage::

    python3 experiments/plot_paper_progress.py

Requires ``matplotlib``, ``Pillow`` and ``inkscape``. Colors and marker shapes
are fixed per method/backbone so all figures share one visual encoding.
"""
from __future__ import annotations

import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.text import Text
from matplotlib.transforms import ScaledTranslation

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
DATA = ROOT / "paper" / "iclr2027" / "tables" / "daily_series.json"
FIG = ROOT / "paper" / "iclr2027" / "fig"

# ---------- 风格 ----------
INK, AXIS, GRID = "#000000", "#333333", "#e5e5e5"
# 方法配色：OpenKAGE 用朱红，其余方法用降饱和的冷灰色调
C_OK, C_PO, C_ECHO, C_VOY, C_REF = "#D55E00", "#4A6580", "#5E9C8C", "#A5779B", "#000000"
# 死因：Paul Tol muted（rose / cyan）+ Other 浅灰底灰斜线（Tol 的 pale grey）
C_COMBAT, C_DROWN, C_OTHER = "#CC6677", "#88CCEE", "#DDDDDD"
# 骨干（与方法正交）：Paul Tol muted 深色四色
C_BB = {"DS": "#332288", "GLM": "#117733", "hy4": "#882255", "kimi": "#999933"}
M_BB = {"DS": "o", "GLM": "s", "hy4": "^", "kimi": "D"}
# 群体规模（有序）：viridis 取样
C_N3, C_N5, C_N8 = "#440154", "#21918C", "#7AD151"
MARK = {C_OK: "o", C_PO: "s", C_ECHO: "^", C_VOY: "D", C_REF: "v",
        C_N3: "o", C_N5: "s", C_N8: "^"}


def _mk(color, every=5, marker=None):
    """冗余编码：每隔 every 个点一个标记；OpenKAGE 实心，其余空心。"""
    m = marker or MARK.get(color)
    if m is None:
        return {}
    return dict(marker=m, markevery=every, markersize=3.0, markeredgewidth=0.7,
                markeredgecolor=color, markerfacecolor=color if color in (C_OK, C_N3) else "white")

plt.rcParams.update({
    "font.family": "sans-serif",
    "font.sans-serif": ["Liberation Sans", "Nimbus Sans", "Arial", "Helvetica", "DejaVu Sans"],
    "font.size": 7.5, "axes.titlesize": 7.5, "axes.labelsize": 7.5,
    "xtick.labelsize": 6.8, "ytick.labelsize": 6.8, "legend.fontsize": 7,
    "text.color": INK, "axes.labelcolor": INK, "axes.edgecolor": AXIS,
    "xtick.color": AXIS, "ytick.color": AXIS,
    "axes.linewidth": 0.6, "xtick.major.width": 0.6, "ytick.major.width": 0.6,
    "xtick.major.size": 2.5, "ytick.major.size": 2.5,
    "axes.spines.top": False, "axes.spines.right": False,
    "axes.grid": True, "axes.grid.axis": "y", "grid.color": GRID, "grid.linewidth": 0.5,
    "axes.axisbelow": True, "legend.frameon": False, "legend.handlelength": 2.4,
    "hatch.linewidth": 0.5,
    "svg.fonttype": "none",          # SVG 里文字保持为 <text>，可在 Inkscape 中直接改
    "pdf.fonttype": 42,
})

METHODS = [  # (格, 名称, 颜色, 线型, 线宽, 图层)
    ("DS/Voyager", "Voyager", C_VOY, "-", 1.1, 2),
    ("DS/Echo", "Echo (adapted)", C_ECHO, "-", 1.1, 3),
    ("DS/PO", "Planner-only", C_PO, "-", 1.2, 4),
    ("DS/OK", "OpenKAGE", C_OK, "-", 1.8, 5),
]
BACKBONES = [("DS", "DS-V4.1F"), ("GLM", "GLM-5.3"), ("hy4", "hy4-preview"), ("kimi", "Kimi-K3")]


def _panel_label(ax, letter, title):
    # 不用 mathtext（会混入 DejaVu 字体）：粗体 (a) 与标题分成两个文本对象，
    # 但共用同一个 transform 与 va=baseline，基线严格对齐、间距固定。
    # f 让大画布（Figure 6 / 12）里的字号与偏移跟着 rcParams 一起放大。
    f = plt.rcParams["font.size"] / 7.5
    trans = ax.transAxes + ScaledTranslation(0, 4 * f / 72, ax.figure.dpi_scale_trans)
    ax.text(0, 1.0, f"({letter})", transform=trans, fontweight="bold",
            fontsize=8 * f, ha="left", va="baseline")
    ax.text(0.15, 1.0, title, transform=trans,
            fontsize=plt.rcParams["axes.titlesize"], ha="left", va="baseline")


def _check_bbox(fig, name):
    """出图前自证：所有文字（含 figure 级 legend）都在画布内。"""
    fig.canvas.draw()
    r = fig.canvas.get_renderer()
    W, H = fig.canvas.get_width_height()
    texts = [t for ax in fig.axes for t in ax.findobj(Text) if t.get_visible() and t.get_text()]
    texts += [t for t in fig.texts if t.get_text()]
    bad = [(t.get_text(), t.get_window_extent(r)) for t in texts]
    bad = [(s, b) for s, b in bad if b.x0 < -1 or b.y0 < -1 or b.x1 > W + 1 or b.y1 > H + 1]
    bad += [("<legend>", l.get_window_extent(r)) for l in fig.legends
            if (lambda b: b.x0 < -1 or b.y0 < -1 or b.x1 > W + 1 or b.y1 > H + 1)(l.get_window_extent(r))]
    if bad:
        raise SystemExit(f"{name}: 文字越界 {bad}")


def _save(fig, name):
    _check_bbox(fig, name)
    svg, pdf = FIG / f"{name}.svg", FIG / f"{name}.pdf"
    fig.savefig(svg)
    plt.close(fig)
    subprocess.run(["inkscape", str(svg), "--export-type=pdf", f"--export-filename={pdf}"],
                   check=True, capture_output=True)
    return svg, pdf


def _top_legend(fig, handles, labels, ncol, y=1.0):
    fig.legend(handles, labels, loc="upper center", ncol=ncol, bbox_to_anchor=(0.5, y),
               columnspacing=1.6, handletextpad=0.6)


def _big_canvas(scale):
    """Figure 6 / 12 使用较宽画布，等比放大字体与线宽；嵌入栏宽后缩放回去，
    使其余结果图的显示字号保持一致。"""
    keys = ("font.size", "axes.titlesize", "axes.labelsize", "xtick.labelsize",
            "ytick.labelsize", "legend.fontsize", "axes.linewidth",
            "xtick.major.width", "ytick.major.width", "xtick.major.size",
            "ytick.major.size", "grid.linewidth", "hatch.linewidth")
    return {k: plt.rcParams[k] * scale for k in keys}


# ---------- 正文图 ----------

def _cat(c):
    c = (c or "").replace("death.", "")
    if any(k in c for k in ("mob", "arrow", "explosion", "spear", "player", "indirectMagic",
                            "thorns", "trident", "fireball", "sting")):
        return "Combat"
    if "drown" in c:
        return "Drowning"
    return "Other"


def fig_death_causes(D):
    cats = [("Combat", C_COMBAT), ("Drowning", C_DROWN), ("Other", C_OTHER)]
    fig, axes = plt.subplots(1, 4, figsize=(5.5, 1.66), sharey=True)
    for ax, (b, name) in zip(axes, BACKBONES):
        for x, (m, mname) in enumerate((("PO", "Planner-\nonly"), ("OK", "OpenKAGE"))):
            ags = [a for r in D[f"{b}/{m}"] for a in r["agents"]]
            cnt = Counter(_cat(x[1]) for a in ags for x in a["causes"])
            bottom = 0.0
            for cat, color in cats:
                v = cnt.get(cat, 0) / len(ags)
                # 不描边：各段等宽、上下无缝；Other 靠浅灰底 + 斜线显形，不用外框
                ax.bar(x, v, bottom=bottom, width=0.74, color=color, linewidth=0,
                       edgecolor="none", hatchcolor="#777777",
                       hatch="//////" if cat == "Other" else None,
                       label=cat if (b == "DS" and m == "PO") else None)
                bottom += v
            ax.text(x, bottom + 1.2, f"{bottom:.1f}", ha="center", va="bottom", fontsize=6.5,
                    color=INK, fontweight="bold" if m == "OK" else "normal")
        ax.set_xticks([0, 1])
        ax.set_xticklabels(["Planner-only", "OpenKAGE"], fontsize=6.3)
        ax.set_xlim(-0.5, 1.5)
        ax.tick_params(axis="x", length=0)
        ax.set_title(name, pad=4)
    axes[0].set_ylim(0, 75)
    axes[0].set_yticks([0, 20, 40, 60])
    axes[0].set_ylabel("Deaths per agent")
    h, l = axes[0].get_legend_handles_labels()
    _top_legend(fig, h, l, ncol=3)
    fig.tight_layout(rect=[0, 0, 1, 0.89], w_pad=0.8)
    return _save(fig, "fig_death_causes")


# ---------- 附录图 ----------

def fig_death_rate(D):
    """每人每天死亡数：先在每个 run 内对三个智能体取均值并做 3 天滑动平均，
    再对 10 个 run 取均值，阴影为跨 run 的 ±1 标准误。各面板 y 轴独立。"""
    import statistics as st
    fig, axes = plt.subplots(1, 4, figsize=(5.5, 1.7))
    for ax, (b, name) in zip(axes, BACKBONES):
        for m, label, color, ls, lw, z in (("PO", "Planner-only", C_PO, "-", 1.3, 3),
                                           ("OK", "OpenKAGE", C_OK, "-", 1.7, 4)):
            per_run = []
            for r in D[f"{b}/{m}"]:
                daily = [sum(a["deaths"][t] for a in r["agents"]) / len(r["agents"]) for t in range(30)]
                per_run.append([st.mean(daily[max(0, t - 1):t + 2]) for t in range(30)])
            mu = [st.mean(x[t] for x in per_run) for t in range(30)]
            se = [st.stdev(x[t] for x in per_run) / len(per_run) ** 0.5 for t in range(30)]
            days = range(1, 31)
            ax.fill_between(days, [u - e for u, e in zip(mu, se)], [u + e for u, e in zip(mu, se)],
                            color=color, alpha=0.18, lw=0, zorder=z - 1)
            ax.plot(days, mu, color=color, ls=ls, lw=lw, zorder=z, label=label, **_mk(color))
        ax.set_title(name, pad=4)
        ax.set_xlim(1, 30)
        ax.set_xticks([1, 10, 20, 30])
        ax.set_xlabel("Day")
        ax.set_ylim(0, None)
    axes[0].set_ylabel("Deaths / agent / day")
    h, l = axes[0].get_legend_handles_labels()
    _top_legend(fig, h[::-1], l[::-1], ncol=2)
    fig.tight_layout(rect=[0, 0, 1, 0.89], w_pad=0.6)
    return _save(fig, "fig_death_rate")


# ---------- 知识积累 / 重复死亡 / run 级分布 ----------

import math
import statistics as st



def fig_knowledge(D):
    """a 可用技能库规模（Full 总量 / Full 中本人所写 / −S）；b 四骨干上本人 vs 他人技能的调用失败率；
    c DS 上失败后改码的前后失败率（数据：tables/skill_usage_figdata.json，由技能使用分析生成）。"""
    fig, axes = plt.subplots(1, 3, figsize=(5.5, 1.75))
    days = range(1, 31)
    full = [a for r in D["DS/OK"] for a in r["agents"]]
    nos = [a for r in D["abl/S"] for a in r["agents"]]
    ax = axes[0]
    ys = {}
    for ags, key, label, color, ls, lw in (
            (full, "n_skills_lib", "Full: group library", C_OK, "-", 1.8),
            (full, "n_skills_own", "Full: self-written", C_OK, "-", 1.2),
            (nos, "n_skills_lib", "−S: own only", C_REF, "-", 1.2)):
        y = ys[(id(ags), key)] = [st.mean(a[key][t] for a in ags) for t in range(30)]
        mk = _mk(color)
        if key == "n_skills_own":        # 与 −S 曲线数值接近：空心标记且错位放置
            mk.update(markerfacecolor="white", markevery=(2, 5))
        ax.plot(days, y, color=color, ls=ls, lw=lw, label=label, **mk)
    # 总量与自写之间 = 来自同伴的技能，直接在图上标出来
    lib, own = ys[(id(full), "n_skills_lib")], ys[(id(full), "n_skills_own")]
    ax.fill_between(days, own, lib, color=C_OK, alpha=0.12, lw=0, zorder=1)
    ax.text(23, (own[22] + lib[22]) / 2, "from\nteammates", color=C_OK, fontsize=6.2,
            ha="center", va="center", linespacing=1.0)
    ax.legend(loc="upper left", fontsize=6.0, handlelength=1.8, handletextpad=0.4,
              borderaxespad=0.1)
    _panel_label(ax, "a", "Skills available per agent")
    ax.set_xlim(1, 30)
    ax.set_ylim(0, 185)                  # 顶部留白放图例，不压曲线
    ax.set_yticks([0, 50, 100, 150])
    ax.set_xticks([1, 10, 20, 30])
    ax.set_xlabel("Day")
    ax.set_ylabel("Skills")
    fd = json.loads((ROOT / "paper" / "iclr2027" / "tables" / "skill_usage_figdata.json").read_text(encoding="utf-8"))
    # b：四骨干上本人技能 vs 他人技能的调用失败率（柱=合并后的比率，点=单个 run）
    ax = axes[1]
    for i, (b_, name) in enumerate(BACKBONES):
        rows = fd["backbones"][b_]
        for j, (ck, fk, filled) in enumerate((("own_calls", "own_fail", False),
                                              ("foreign_calls", "foreign_fail", True))):
            x = i + (j - 0.5) * 0.38
            rate = 100 * sum(r[fk] for r in rows) / sum(r[ck] for r in rows)
            ax.bar(x, rate, width=0.34, facecolor=C_OK if filled else "white", edgecolor=C_OK,
                   linewidth=0.9, zorder=2, label=("Teammates' skills" if filled else "Own skills") if i == 0 else None)
            ys_ = [100 * r[fk] / r[ck] for r in rows if r[ck] > 0]
            xs_ = [x + 0.12 * ((k * 0.618) % 1 - 0.5) for k in range(len(ys_))]
            ax.scatter(xs_, ys_, s=3, color="#555555", alpha=0.6, linewidths=0, zorder=3)
    ax.set_xticks(range(4))
    ax.set_xticklabels(["DS", "GLM", "hy4", "Kimi"])
    ax.set_xlim(-0.6, 3.6)
    ax.set_ylim(0, 46)
    ax.set_ylabel("Failed calls (%)")
    ax.legend(loc="upper right", fontsize=6.0, handlelength=1.2, handletextpad=0.4, borderaxespad=0.1)
    _panel_label(ax, "b", "Skill calls that failed")
    # c：DS 上每次"失败后改码"，该技能改前/改后窗口的失败率（细线=单次改码，粗线=合并）
    ax = axes[2]
    revs = fd["revisions_ds"]
    for r in revs:
        pb = 100 * r["fail_before"] / (r["ok_before"] + r["fail_before"])
        pa = 100 * r["fail_after"] / (r["ok_after"] + r["fail_after"])
        ax.plot([0, 1], [pb, pa], color="#8a8a8a", lw=0.5, alpha=0.12, zorder=2)
    fb = 100 * sum(r["fail_before"] for r in revs) / sum(r["ok_before"] + r["fail_before"] for r in revs)
    fa = 100 * sum(r["fail_after"] for r in revs) / sum(r["ok_after"] + r["fail_after"] for r in revs)
    ax.plot([0, 1], [fb, fa], color=C_OK, lw=1.8, zorder=4, **dict(marker="o", markersize=3.4, markeredgecolor=C_OK, markerfacecolor=C_OK))
    ax.text(0.04, fb + 4, f"{fb:.1f}%", color=C_OK, fontsize=6.4, ha="left", va="bottom")
    ax.text(0.96, fa + 4, f"{fa:.1f}%", color=C_OK, fontsize=6.4, ha="right", va="bottom")
    ax.set_xlim(-0.15, 1.15)
    ax.set_xticks([0, 1])
    ax.set_xticklabels(["Before", "After"])
    ax.set_ylim(0, 100)
    ax.set_yticks([0, 25, 50, 75, 100])
    ax.set_ylabel("Failed calls (%)")
    _panel_label(ax, "c", f"After a revision (n={len(revs)})")
    fig.tight_layout(w_pad=0.9)
    return _save(fig, "fig_knowledge")


def fig_runs(D):
    """附录：四骨干 × {Planner-only, OpenKAGE} 的 run 级分布（每点一个 run，横线为均值）。"""
    mets = [("Deaths", lambda r: st.mean(sum(a["deaths"]) for a in r["agents"])),
            ("XP per life", lambda r: st.mean(a["xp"][-1] / (sum(a["deaths"]) + 1) for a in r["agents"])),
            ("Advancements", lambda r: st.mean(a["adv"] for a in r["agents"]))]
    fig, axes = plt.subplots(1, 3, figsize=(5.5, 1.85))
    for ax, (ylabel, f), letter in zip(axes, mets, "abc"):
        for bi, (b, name) in enumerate(BACKBONES):
            for k, (m, color) in enumerate((("OK", C_OK), ("PO", C_PO))):
                vals = [f(r) for r in D[f"{b}/{m}"]]
                x0 = bi + (k - 0.5) * 0.36
                xs = [x0 + 0.1 * (((j * 7) % 10) / 10 - 0.45) for j in range(len(vals))]
                ax.scatter(xs, vals, s=9, marker="s" if m == "OK" else "o", zorder=3, linewidths=0.7,
                           facecolors="white" if m == "OK" else color, edgecolors=color,
                           label=("OpenKAGE" if m == "OK" else "Planner-only") if bi == 0 else None)
                mu = st.mean(vals)
                ax.plot([x0 - 0.13, x0 + 0.13], [mu, mu], color=INK, lw=1.1, zorder=4)
        ax.set_xticks(range(4))
        ax.set_xticklabels([n for _, n in BACKBONES], fontsize=6, rotation=20)
        ax.tick_params(axis="x", length=0)
        ax.set_ylim(0, None)
        _panel_label(ax, letter, ylabel)
    h, l = axes[0].get_legend_handles_labels()
    _top_legend(fig, h, l, ncol=2)
    fig.tight_layout(rect=[0, 0, 1, 0.88], w_pad=1.0)
    return _save(fig, "fig_runs")


# ---------- Figure 6（迁移）与 Figure 12（scaling）：直接读发布包 run 目录 ----------

RESULTS = ROOT / "results"          # 发布包布局：results/<cell>/runNN
# N=3 用 Figure 6 也复用的那条 30 夜 run（glm53flash_non_transfer/run02）。
GROUP_SIZE = (("N = 3", "glm53flash_non_transfer/run02", C_N3, "-"),
              ("N = 5", "glm53flash_n5_openkage/run01", C_N5, "-"),
              ("N = 8", "glm53flash_n8_openkage/run01", C_N8, "-"))

# Figure 6 / 12 使用的红蓝配色（与其余结果图的统一配色不同）
C_OLD_RED, C_OLD_BLUE, C_OLD_GREEN = "#d62728", "#1f77b4", "#2ca02c"

# Figure 6：transfer / non-transfer 各 3 条 run（每条 3 agents），按发布包 cell 顺序。
TRANSFER_GROUPS = (
    ("OpenKAGE (transfer)", C_OLD_RED, (
        "glm53flash_transfer/run01",
        "glm53flash_transfer/run02",
        "glm53flash_transfer/run03")),
    ("OpenKAGE (non-transfer)", C_OLD_BLUE, (
        "glm53flash_non_transfer/run01",
        "glm53flash_non_transfer/run02",
        "glm53flash_non_transfer/run03")),
)
# Figure 12 (b)：B.6 的两条 60 夜 run（各 3 agents，60 game days）
SEPT60 = (("OpenKAGE", "glm53flash_60d_openkage/run01", C_OLD_RED, "-"),
          ("Baseline", "glm53flash_60d_planner_only/run01", C_OLD_BLUE, "--"))

# 成就显示名 → results/adv_icons 下图标 stem
TRANSFER_ICONS = (("[Isn't It Iron Pick]", "story.iron_tools"),
                  ("[Suit Up]", "story.obtain_armor"),
                  ("[Diamonds!]", "story.mine_diamond"),
                  ("[Ice Bucket Challenge]", "story.form_obsidian"))


def _agents_daily(rd: Path, T: int):
    """逐智能体逐天：累计死亡 / 累计 XP / 累计不重复成就数（Adv's 同口径）。"""
    out = []
    for cp in sorted({p.name.split("_r")[0] for p in rd.glob("checkpoint_*_r*.json")}):
        ms = json.loads(sorted(rd.glob(f"{cp}_r*.json"))[-1].read_text())["metrics_snapshot"]
        nrs = ms.get("night_reflections") or []
        dd = [0] * T
        for d in ms.get("deaths") or []:
            if isinstance(d, dict) and d.get("day_idx") and d["day_idx"] <= T:
                dd[d["day_idx"] - 1] += 1
        first = {}
        for a in ms.get("advancements") or []:
            if isinstance(a, dict) and a.get("advancement") and a.get("day_idx"):
                first[a["advancement"]] = min(first.get(a["advancement"], 99), a["day_idx"])
        xp, last = [], 0
        for t in range(T):
            if t < len(nrs):
                last = (nrs[t].get("discoveries") or {}).get("xp_total") or last
            xp.append(last)
        out.append({"deaths": [sum(dd[:t + 1]) for t in range(T)], "xp": xp,
                    "adv": [sum(1 for v in first.values() if v <= t + 1) for t in range(T)]})
    return out


def _band(ax, days, ags, key, color, ls, lw, label, z=3, end_label=True, mk=None,
         end_inside=False, end_below=False, below_dy=-12, end_dx=-2):
    mu = [st.mean(a[key][t] for a in ags) for t in range(len(days))]
    sd = [st.stdev(a[key][t] for a in ags) for t in range(len(days))]
    ax.fill_between(days, [m - s for m, s in zip(mu, sd)], [m + s for m, s in zip(mu, sd)],
                    color=color, alpha=0.14, lw=0, zorder=z - 1)
    ax.plot(days, mu, color=color, ls=ls, lw=lw, label=label, zorder=z,
            **(mk if mk is not None else _mk(color, every=max(1, len(days) // 6))))
    if end_label:
        f = plt.rcParams["font.size"] / 7.5
        if end_inside:
            # 所有终值标注右端统一贴在 x 轴右端（右对齐成一列）；
            # 垂直方向各自紧贴自己那条线：高线在线上、低线在线下。
            # 谁高谁低由调用处按数据决定（fig_transfer 的 end_below）。
            dy = (below_dy if end_below else 4) * f
            va = "top" if end_below else "bottom"
            ax.annotate(f"{mu[-1]:.1f}", (days[-1] + 1, mu[-1]), xytext=(end_dx * f, dy),
                        textcoords="offset points", ha="right", va=va,
                        fontsize=6.3 * f, color=color)
        else:
            ax.annotate(f"{mu[-1]:.1f}", (days[-1], mu[-1]), xytext=(3 * f, 0),
                        textcoords="offset points", va="center",
                        fontsize=6.3 * f, color=color)
    return mu


SCALING_ICONS = (("[Isn't It Iron Pick]", "story.iron_tools"),
                 ("[Diamonds!]", "story.mine_diamond"),
                 ("[Ice Bucket Challenge]", "story.form_obsidian"))


def _adv_icon(ax, x, y0, stem, sign, S, dy_pt=None):
    """Figure 6 / 12 共用的 Voyager 式成就图标：锚在曲线台阶上，
    sign=+1 上挂、-1 下挂；统一 16px、中性灰框 + 浅灰底。"""
    from matplotlib.offsetbox import AnnotationBbox, OffsetImage
    from PIL import Image
    p = ROOT / "results" / "adv_icons" / f"{stem}.png"
    if not p.is_file():
        return
    img = Image.open(p).convert("RGBA").resize((16, 16), Image.LANCZOS)
    off = (11 if dy_pt is None else dy_pt) * S * sign
    ax.add_artist(AnnotationBbox(OffsetImage(img, zoom=0.65 * S), (x, y0),
                                 xybox=(0, off), boxcoords="offset points",
                                 frameon=True, pad=0.1,
                                 bboxprops=dict(edgecolor="#666666",
                                                linewidth=0.8 * S,
                                                facecolor="#ebebeb"),
                                 zorder=6))


def _sept_first_days(dirs):
    """逐夜 advancements_so_far 里每个成就首次出现的夜（每臂一次）。"""
    out = {}
    for d in dirs:
        for cp in sorted({q.name.split("_r")[0] for q in d.glob("checkpoint_*_r*.json")}):
            j = json.loads(sorted(d.glob(f"{cp}_r*.json"))[-1].read_text())
            seen = set()
            for ni, nr in enumerate(j["metrics_snapshot"].get("night_reflections") or [], start=1):
                for a in (nr.get("discoveries") or {}).get("advancements_so_far") or []:
                    if a not in seen:
                        seen.add(a)
                        out.setdefault(a, []).append(ni)
    return out


def _icon_row(ax, day_by_stem, curves, S, T):
    """一排图标：x=各成就首达夜中位数，同层水平错开，y 锚在窗口内曲线最大值上方。"""
    jobs = sorted((day, stem) for stem, day in day_by_stem.items())
    placed = []
    for day, stem in jobs:
        x = max(day, placed[-1] + 2.2) if placed else day
        placed.append(x)
        win = [c[t] for c in curves for t in range(max(0, day - 2), min(T, day + 2))]
        y0 = max(win) if win else curves[0][min(day - 1, T - 1)]
        _adv_icon(ax, x, y0, stem, 1, S)


def _transfer_dirs(names):
    """cell/runNN → results/ 下的目录。"""
    return [RESULTS / n for n in names]


def _adv_first_days(dirs):
    """{成就显示名: [各智能体首次达成夜]}，口径同 _agents_daily 的 adv。"""
    out = {}
    for d in dirs:
        for cp in sorted({p.name.split("_r")[0] for p in d.glob("checkpoint_*_r*.json")}):
            ms = json.loads(sorted(d.glob(f"{cp}_r*.json"))[-1].read_text())["metrics_snapshot"]
            for a in ms.get("advancements") or []:
                if isinstance(a, dict) and a.get("advancement") and a.get("day_idx"):
                    out.setdefault(a["advancement"], []).append(a["day_idx"])
    return out


def _hollow_mk(color, days):
    """Figure 6 / 12 的空心圆点标记，线型由 ls 另行指定。"""
    f = plt.rcParams["font.size"] / 7.5
    return dict(marker="o", markevery=max(1, len(days) // 8), markersize=3.2 * f,
                markeredgewidth=0.8 * f, markeredgecolor=color, markerfacecolor="white")


def fig_transfer(D):
    """正文 Figure 6：知识迁移，两组各 3 run × 3 agents 的逐夜均值 ±1 sd。
    画布为 12.9in 宽（每面板约 4.3in），底部共用图例。"""
    S = 12.9 / 5.5
    with plt.rc_context(_big_canvas(S)):
        fig, axes = plt.subplots(1, 3, figsize=(12.9, 4.3))
        days = list(range(1, 31))
        packs = [(label, color, [a for d in _transfer_dirs(names) for a in _agents_daily(d, 30)])
                 for label, color, names in TRANSFER_GROUPS]
        # 每个面板里较高的那条曲线标注放线上方、较低的放线下方：
        # (a) 蓝(PO-non-transfer)高红低、(b)(c) 红(transfer)高蓝低
        for ax, (key, ylabel, letter) in zip(axes, (("deaths", "Deaths", "a"),
                                                    ("xp", "Cumulative XP", "b"),
                                                    ("adv", "Advancements", "c"))):
            ends = [st.mean(a[key][29] for a in ags) for _, _, ags in packs]
            for i, (label, color, ags) in enumerate(packs):
                ls, lw = ("-", 1.8 * S) if i == 0 else ("--", 1.4 * S)
                _band(ax, days, ags, key, color, ls, lw, label,
                      mk=_hollow_mk(color, days), end_inside=True,
                      end_below=(ends[i] < ends[1 - i]),
                      below_dy=(-5 if key == "adv" else -12))
            _panel_label(ax, letter, ylabel)
            ax.set_xlim(1, 31)            # 终值标注收在内侧，右侧几乎不留白
            ax.set_xticks([1, 10, 20, 30])
            ax.set_xlabel("Day")
            if key != "adv":            # (c) 不强制从 0 起，否则 1.4 的差距被压扁
                ax.set_ylim(0, None)
        # (c) 里程碑图标：
        # x = 该组各智能体首达夜的中位数，y 锚在该夜曲线「开始抬升」的台阶附近，
        # transfer 上挂、non-transfer 下挂；同组同层按夜水平错开。
        ax = axes[2]
        curves = [[st.mean(a["adv"][t] for a in ags) for t in range(30)]
                  for _, _, ags in packs]
        for row, (label, color, names) in enumerate(TRANSFER_GROUPS):
            first = _adv_first_days(_transfer_dirs(names))
            jobs = []
            for adv, stem in TRANSFER_ICONS:
                ds = sorted(first.get(adv, []))
                if ds:
                    jobs.append((ds[len(ds) // 2], stem))
            jobs.sort()
            sign = 1 if row == 0 else -1
            placed_x = []
            for day, stem in jobs:
                x = max(day, placed_x[-1] + 2.2) if placed_x else day
                placed_x.append(x)
                win = [c[t] for c in curves for t in range(max(0, day - 2), min(30, day + 2))]
                y0 = max(win) if sign > 0 else curves[row][day - 1]
                # 铁镐（transfer 组台阶最矮的一档）往下压一点更贴曲线
                _adv_icon(ax, x, y0, stem, sign, S,
                          dy_pt=7 if (row == 0 and stem == "story.iron_tools") else None)
        ax.set_ylim(1.5, 14.5)
        h, l = axes[0].get_legend_handles_labels()
        _top_legend(fig, h, l, ncol=2)
        fig.subplots_adjust(left=0.045, right=0.995, bottom=0.15, top=0.80, wspace=0.10)
        return _save(fig, "fig_transfer")


def fig_scaling(D):
    """附录 Figure 12：群体规模（30 夜）与时间轴（60 夜）。"""
    S = 8.6 / 5.5
    with plt.rc_context(_big_canvas(S)):
        fig, axes = plt.subplots(1, 2, figsize=(8.6, 4.5))
        days = list(range(1, 31))
        # (a) 群体规模：N=3/5/8 各一条 run，30 夜
        a_ends, a_curves = [], []
        for (label, d, _, _), color, name in zip(
                GROUP_SIZE, (C_OLD_RED, C_OLD_BLUE, C_OLD_GREEN),
                ("3 agents", "5 agents", "8 agents")):
            ags = _agents_daily(RESULTS / d, 30)
            a_ends.append(st.mean(a["adv"][29] for a in ags))
            a_curves.append([st.mean(a["adv"][t] for a in ags) for t in range(30)])
            _band(axes[0], days, ags, "adv", color, "-",
                  1.6 * S, name, end_label=False, mk=_hollow_mk(color, days))
        # (b) 时间轴：两条 60 夜 run（各 3 agents）
        d60, b_curves = list(range(1, 61)), []
        for i, (name, d, color, ls) in enumerate(SEPT60):
            ags = _agents_daily(RESULTS / d, 60)
            b_curves.append([st.mean(a["adv"][t] for a in ags) for t in range(60)])
            _band(axes[1], d60, ags, "adv", color, ls, (1.8 if i == 0 else 1.4) * S,
                  name, end_inside=True, end_below=(i == 1), below_dy=-5,
                  end_dx=2, mk=_hollow_mk(color, d60))
        _panel_label(axes[0], "a", "Group size (30 days)")
        _panel_label(axes[1], "b", "Time horizon (60 days)")
        axes[0].set_xlim(1, 31)
        axes[0].set_xticks([1, 10, 20, 30])
        axes[1].set_xlim(1, 61)
        axes[1].set_xticks([1, 20, 40, 60])
        for ax in axes:
            ax.set_xlabel("Day")
            ax.set_ylim(0, 14)
        axes[0].set_ylabel("Advancements / agent")
        # (a) 终值标注（与附录 B.6 一致：9.8 / 10.0 / 9.8）：最高那条（蓝）标线上方，
        # 较低的绿、红两条标各自线下方，依次往下错开避免叠。
        order = sorted(range(3), key=lambda i: -a_ends[i])
        for rank, i in enumerate(order):
            color = (C_OLD_RED, C_OLD_BLUE, C_OLD_GREEN)[i]
            txt = ("9.8", "10.0", "9.8")[i]
            dy, va = ((4 * S, "bottom") if rank == 0
                      else (-(4 + (rank - 1) * 10) * S, "top"))
            axes[0].annotate(txt, (31, a_ends[i]), xytext=(-2 * S, dy),
                             textcoords="offset points", ha="right", va=va,
                             fontsize=6.3 * S, color=color)
        # 成就图标（铁镐 → 钻石 → 黑曜石）：(a) 取三条群体规模 run
        # 合并各臂首达夜，(b) 取 OpenKAGE 那条 60 夜 run 的首达夜。
        a_first = _sept_first_days([RESULTS / d for _, d, _, _ in GROUP_SIZE])
        b_first = _adv_first_days([RESULTS / SEPT60[0][1]])
        for ax, first, cs, T in ((axes[0], a_first, a_curves, 30),
                                  (axes[1], b_first, b_curves, 60)):
            day_by_stem = {}
            for adv, stem in SCALING_ICONS:
                ds = sorted(first.get(adv, []))
                if ds:
                    day_by_stem[stem] = ds[len(ds) // 2]
            _icon_row(ax, day_by_stem, cs, S, T)
        # 图例置于子图内部竖向排列
        axes[0].legend(loc="lower right", ncol=1, handlelength=2.0)
        axes[1].legend(loc="upper left", ncol=1, handlelength=2.0)
        fig.subplots_adjust(left=0.09, right=0.975, bottom=0.11, top=0.80, wspace=0.10)
        return _save(fig, "fig_scaling")


def main():
    """只生成论文里实际放的六张结果图。"""
    D = json.loads(DATA.read_text())
    for f in (fig_death_causes, fig_death_rate, fig_knowledge, fig_runs,
              fig_transfer, fig_scaling):
        svg, pdf = f(D)
        print("wrote", svg.name, "→", pdf.name)


if __name__ == "__main__":
    main()
