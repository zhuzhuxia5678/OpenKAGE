#!/usr/bin/env python3
"""Data layer for the viewer's exploration maps.

Parses per-agent trajectories, memory-layer places and edges, the exploration
coverage circle and top-down terrain images from a run directory. The viewer
backend (server.py) turns this into JSON for the frontend SceneMap.vue; both
sides share these definitions so the map and the graph stay in sync.

Inputs (all read-only, under results/<run>/):
  - contexts/  per-day trajectory key signals; planner-only runs are rebuilt
    from day-end panels, onLog and place_log instead
  - arm_graphs/arm_<arm>_e1b1.db  memory-layer concepts with (x, y, z) coords
  - checkpoint_*  structured death details (day, step, cause, killer)
  - checkpoint*/world/region/*.mca  world saves for the terrain underlay

Notable conventions: deaths are the only segment boundaries (a distance jump
can be normal pathfinding); framing uses the main settlement cluster while the
coverage circle uses all visited points; the minimum enclosing circle is an
iterative Welzl with a fixed-seed generator for reproducibility; terrain PNGs
are cached by region-file fingerprint and rendered in a background thread.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import re
import sqlite3
import threading
import zlib
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from nbt import nbt
from PIL import Image

# ── §A 数据层：轨迹 / 记忆层地点与边 / 生命段 / 主聚居区（原 scene_map.py）─────────

# 记忆锚点坐标的合法范围：出生点随机半径默认 1000，再留几倍余量。
# 过滤掉 summary 里被误当坐标的东西（如 "(1, 64, -6)" 是坐标，但 "y=54 到 y=42" 里的裸数字不是）。
_COORD_LIMIT = 100_000


# ── 轨迹解析（reflect 上下文 → 逐步坐标 + 权威死亡点）──────────────────────────
# 关键信号行（_extract_key_signals 渲染，2026-09 起反思轨迹的固定形态）：
#   [时间:day (259/24000，距日落 11741 刻)  位置:x=213.5 y=69.0 z=538.5  生命:20/20 …]
# 位置来自引擎 observe payload 的 status.position（render_observation 渲染），不是散文碰运气。
_SIG_POS_RE = re.compile(r"位置:x=(-?[\d.]+)\s+y=(-?[\d.]+)\s+z=(-?[\d.]+)")
# 权威死亡行（_death_line 渲染）：`  [死亡] 被怪物击杀，凶手 Spider，位置 x=211 y=69 z=540，附近：…`
# 位置是 onSystem.js 死亡时快照的，比旧版「>30 格跳变猜死亡点」准（长寻路步会误报那个启发式）。
_DEATH_POS_RE = re.compile(
    r"\[死亡\].*?位置 x=(-?[\d.]+) y=(-?[\d.]+) z=(-?[\d.]+)")
# react_day 观察面板（biome 来源，一天约 3 份）：
#   生物群系：plains  时间：…   /   位置：x=200.5 y=62.0 z=528.5
_BIOME_RE = re.compile(r"生物群系：(\S+)")
_DAY_PANEL_POS_RE = re.compile(r"位置：x=(-?[\d.]+)\s+y=(-?[\d.]+)\s+z=(-?[\d.]+)")
# reflect 文件名：arm_<arm>_e1b1_reflect_exploration_at<NNN>.json，NNN=天号（night_cb 的 day_idx）。
_CTX_RE = re.compile(r"arm_(?P<arm>[a-z0-9]+)_e\d+b\d+_reflect_exploration_at(?P<at>\d+)\.json$")
# react_day 文件名（biome 辅助源）：e1b1_<arm>_<run…>_dayNN.json。
_CTX_DAY_RE = re.compile(r"e\d+b\d+_(?P<arm>[A-Za-z0-9]+)_.+_day(?P<day>\d+)\.json$")
# 扁平 checkpoint 文件（死亡权威补充源）：checkpoint_<arm>_r<轮号>.json。
_CKPT_FILE_RE = re.compile(r"checkpoint_(?P<arm>[a-z0-9]+)_r\d+\.json$")
# ── planner-only（nograph）组的稀疏轨迹重建源 ────────────────────────────────
#   onLog 自报坐标行（模型 console.log 打的，格式松散但都是 `…at {"x":…,"y":…,"z":…}`）：
#     [onLog] now at {"x":235.5,"y":49,"z":520.45} / furnace at {...} / digging iron at {...}
_ONLOG_AT_RE = re.compile(
    r'\[onLog\][^\n]*?at\s*\{"x":(-?[\d.]+),"y":(-?[\d.]+),"z":(-?[\d.]+)\}')
# 记忆层坐标：形如 (6,64,-6) / (678, 112, 323) / 坐标 (2, 65, 12)。
# 要求三段都在一对括号里，避免把 "y=54 到 y=42" 这类散落数字凑成一个假坐标。
_MEM_COORD_RE = re.compile(r"\((-?\d{1,6})\s*,\s*(-?\d{1,4})\s*,\s*(-?\d{1,6})\)")
_ARM_DB_RE = re.compile(r"arm_(?P<arm>[a-z0-9]+)_e(?P<epoch>\d+)b(?P<batch>\d+)\.db$")

# 「不是地点」的记忆概念：当天总结/状态盘点/待办。它们常在 summary 里捎带坐标，
# 但标到地图上纯属噪声（"第3天结束状态"不是一个地方）。宁可漏标几个真地点，
# 也不要让图上飘着一堆不是地名的字 —— 地图的可读性优先。
_NON_PLACE_RE = re.compile(r"^第\d+天$|结束状态|待办|计划|之谜|失败|未完成|需要|推进")


# ── 网络盘批量预读 ────────────────────────────────────────────────────────────
# 网络盘上一次 open+read 是几十毫秒的往返，一个 run 的 contexts/ 有 500+ 份 JSON，
# 逐份串行读实测 139s（8 并发 19s）。解析本身是 CPU 活，仍按 sorted(paths) 串行做，
# 保证轨迹点顺序、biome 最近邻、死亡断点换算与旧版逐字段一致。
_CTX_READ_WORKERS = 8


def _read_one(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None   # 跑中被 kill 留的半截文件 / 读失败：调用方沿用跳过语义


def _read_json_texts(paths: list[Path], workers: int = _CTX_READ_WORKERS) -> dict[Path, str | None]:
    """并发预读一批 JSON/JSONL 文本；ex.map 保序，返回 {path: text|None}。"""
    if workers <= 1:
        return {p: _read_one(p) for p in paths}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return dict(zip(paths, ex.map(_read_one, paths)))


def _is_place(name: str) -> bool:
    """这个记忆概念是不是一个**地点**（值得往地图上标）。

    靠名字判，不靠"有没有坐标" —— 总结类概念也有坐标（见模块 docstring）。
    """
    return not _NON_PLACE_RE.search(name)


def _read_only(db: Path) -> sqlite3.Connection:
    """以 immutable 打开：训练**正在写**这个库时也不会拿到锁、更不会写坏它。

    immutable=1 而非 mode=ro 的理由：mode=ro 仍会去读 -wal 并可能要求写 -shm（拿共享锁），
    而这些 db 正被运行中的 run 持有。地图数据读取容忍"看不到最后几秒的写入"，
    但绝不容忍干扰训练。
    """
    conn = sqlite3.connect(f"file:{db}?immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _parse_reflect_day(body: str) -> dict:
    """解析一份反思上下文的「完整经历」段 → {"points": [(step, x, y, z, biome)],
    "deaths": [(x, y, z, None, None)]}。

    轨迹点：逐条关键信号行 `位置:x=… y=… z=…`（引擎 observe payload 渲染，见模块 docstring）。
    step 按出现顺序编号 —— 只用于同天内的时间顺序与 polyline 连线，绝对值无关紧要。
    biome 传 None 由调用方按最近邻并（本函数不读 react_day 文件，保持单一职责）。

    死亡点：`[死亡] …位置 x=… y=… z=…` 行，服务器权威播报快照，逐条收。reflect 行不带
    结构化 cause/killer（要再从中文文案反解，易碎），故后两位给 None —— 有 checkpoint 时
    会被 _merge_checkpoint_deaths 整体替换成带 cause/killer 的版本。
    """
    points: list[tuple[int, float, float, float, str]] = []
    step = 0
    for m in _SIG_POS_RE.finditer(body):
        step += 1
        points.append((step, float(m.group(1)), float(m.group(2)), float(m.group(3)), "?"))
    deaths: list[tuple] = [
        (float(m.group(1)), float(m.group(2)), float(m.group(3)), None, None)
        for m in _DEATH_POS_RE.finditer(body)
    ]
    return {"points": points, "deaths": deaths}


def _load_day_biomes(ctx_dir: Path, arm: str, day: int,
                     texts: dict | None = None) -> list[tuple[float, float, str]]:
    """从 react_day 文件（e1b1_<arm>_…_dayNN.json）抽出当天观察面板的 (x, z, biome) 列表。

    reflect 的关键信号行不含 biome；这里一天能拿到约 3 份带 biome 的观察面板，
    供调用方按最近邻贴到轨迹点上。文件不存在/解析不到 → 空列表（biome 全 "?"，
    前端地形底图有最近邻外推兜底）。
    """
    out: list[tuple[float, float, str]] = []
    for path in sorted(ctx_dir.glob(f"e*b{1}_{arm}_*_day{day:02d}.json")):
        if path.name.endswith("_reasoning.json"):
            continue
        raw = texts.get(path) if texts is not None else _read_one(path)
        if raw is None:
            continue
        doc = json.loads(raw)
        for msg in doc.get("messages", []):
            content = msg.get("content")
            if msg.get("role") != "tool" or not isinstance(content, str):
                continue
            biome = _BIOME_RE.search(content)
            pos = _DAY_PANEL_POS_RE.search(content)
            if biome and pos:
                out.append((float(pos.group(1)), float(pos.group(3)), biome.group(1)))
    return out


def _attach_biomes(points: list[tuple], samples: list[tuple[float, float, str]]) -> None:
    """把 react_day 的 (x, z, biome) 采样按最近邻贴到轨迹点上（原地改第 5 位）。

    样本一天只有约 3 个、覆盖不了全图 —— 这只是地形底图的上色种子，不是精确字段；
    贴错最近邻最多让底图色块边界偏一点，无碍轨迹几何。
    """
    if not samples:
        return
    for i, p in enumerate(points):
        _, x, _, z, _ = p
        best = min(samples, key=lambda s: (s[0] - x) ** 2 + (s[1] - z) ** 2)
        points[i] = (*p[:4], best[2])


def _merge_checkpoint_deaths(run_dir: Path, out: dict, texts: dict | None = None) -> None:
    """用 checkpoint_<arm>_r*.json 的结构化死亡明细**替换**各天的 reflect 死亡行。

    reflect 死亡行覆盖 ~87%（实测本 run 47/65）：「跨步积压死亡」（LLM 思考/反思窗口里
    被打死、播报被天边界 pause/resume 的 _record_system 收走）不挂 day_steps 的任何 rec，
    _build_reflection_trajectory 永远看不到 —— 这是反思轨迹文本的既有盲区，不是解析丢的。
    checkpoint 的 metrics_snapshot.deaths 是 env 侧全量记账，实测对三个 arm 都完全包含
    reflect 死亡行（坐标逐条一致），故直接整体替换而非并集 —— 并集的去重键躲不开
    「同一天同一坐标死两次」（实测 bob day12 两次摔死在同一坑，(day,x,z) 相同），
    而 checkpoint 本来就是权威，没必要拿子集去修它。
    文件不存在/无死亡 → 保持 reflect 行不动（老 run 仍有死亡可画）。
    """
    for path in sorted(run_dir.glob("checkpoint_*_r*.json")):
        m = _CKPT_FILE_RE.match(path.name)
        if not m:
            continue
        arm = m.group("arm")
        days = out.get(arm)
        if not days:
            continue
        raw = texts.get(path) if texts is not None else _read_one(path)
        if raw is None:
            continue
        try:
            doc = json.loads(raw)
            deaths = doc.get("metrics_snapshot", {}).get("deaths") or []
        except (OSError, ValueError):
            continue
        # (x, y, z, cause, killer)：后两样给 web 端 hover title 用（死因+凶手，服务器权威）；
        # cause 是 translate key（如 death.attack.mob），原样给、不猜中文。
        by_day: dict[int, list[tuple]] = defaultdict(list)
        for rec in deaths:
            pos = rec.get("position") or {}
            if pos.get("x") is None or pos.get("z") is None:
                continue
            day = rec.get("day_idx")
            if day is None:
                continue
            by_day[int(day)].append(
                (pos["x"], pos.get("y"), pos["z"], rec.get("cause"), rec.get("killer")))
        for day, pts in by_day.items():
            if day in days:
                days[day]["deaths"] = pts
        # ★ 死亡断点：把 checkpoint 的全局 env 步号换算成**天内点索引**，供 _segments 切线。
        #   前提（已实测验证）：reflect 每天点数 == 当天 env write_code 步数，从头逐天累计
        #   后全局索引对齐（Carol 6 处死亡断点坐标 vs 死亡点 x-z 距离全部 ≤5.5 格，
        #   且每条 env step 都落在对应天的 reflect 点数区间内）。对不上的 run（点数缺天/
        #   reflect 版本混杂）→ 索引越界或坐标对不上，此时丢弃该断点（宁少断不错断）。
        for rec in deaths:
            day = rec.get("day_idx")
            step = rec.get("step")
            if day is None or step is None or day not in days:
                continue
            day_points = days[day]["points"]
            # 天内 0 基索引 = env 全局步号 − 该天之前所有天的点数
            base = sum(len(days[d]["points"]) for d in days if d < day)
            idx = int(step) - base
            if 0 <= idx < len(day_points):
                days[day].setdefault("death_breaks", set()).add(idx)


def _lives(days: dict[int, dict]) -> list[list[tuple]]:
    """把一个 arm 的全部轨迹点拼成**生命段**列表：跨天首尾相连，只在死亡断点切开。

    web 端 polylines 的数据源（与合并前的出图实现同一逻辑）：
    - 跨天**连线**——每天末点与次日首点是夜宿原地（衔接位移实测 ≤10 格），天界不是
      轨迹断点；按天嵌套输出（days[].segments）会把 19 天 6 死的轨迹碎成 26 段，
      看起来与 7 条完整生命线完全不是一个量级。
    - **死亡是唯一断线判据**：`death_breaks`（checkpoint 权威 (day, step) 换算，见
      _merge_checkpoint_deaths）在全局序列上的位置切一刀。无 checkpoint 的 run
      → 无断点 → 全程一条。

    返回 [生命段, …]，每段是 (step, x, y, z, biome) 点列（step 是天内序号，
    拼接后不重排——绘制只需要 x/z 顺序）。
    """
    seq: list[tuple] = []
    # 全局死亡断点索引：各天 death_breaks 的天内索引 + 该天在全局序列中的起点偏移
    global_breaks: set[int] = set()
    for day in sorted(days):
        data = days[day]
        base = len(seq)
        seq.extend(data["points"])
        for idx in data.get("death_breaks") or ():
            if 0 <= idx < len(data["points"]):
                global_breaks.add(base + idx)
    if not seq:
        return []
    out: list[list[tuple]] = [[]]
    for i, p in enumerate(seq):
        out[-1].append(p)
        if i in global_breaks:
            out.append([])
    return [seg for seg in out if seg]


def load_trajectories(run_dir: Path, day_range: tuple[int, int] | None) -> dict:
    """读 contexts/ 的 reflect 文件出 {arm: {day: {"points": [...], "deaths": [...]}}}。

    只认 arm_*_reflect_exploration_at*.json —— 那是**唯一**逐步轨迹源（见模块 docstring：
    密度 ~30 步/天，与 env 的 n_pos_samples 全等）。旧的 react_day（e1b1_…_dayNN.json）
    观察面板一天只有约 3 份、onLog 自报坐标命中率为零，当轨迹源会把轨迹退化成三段折线 ——
    现在它只作 biome 辅助源（_load_day_biomes）。nograph 对照组没有 reflect 文件 →
    返回空 dict（预期，viewer 显示空态）。死亡点补 checkpoint 结构化明细（见
    _merge_checkpoint_deaths）。
    """
    ctx_dir = run_dir / "contexts"
    if not ctx_dir.is_dir():
        raise FileNotFoundError(
            f"{ctx_dir} 不存在 —— 上下文留痕是 2026-08-10 起默认开的，更早的 run 没有这份数据")
    # 一次性并发预读：reflect 轨迹 + react_day 的 biome 面板 + checkpoint 死亡明细。
    # 旧的逐份串行 open 在网络盘上是 57s 的大头；解析顺序下面仍按 sorted(paths)。
    paths = sorted(p for p in ctx_dir.glob("*.json")
                   if not p.name.endswith("_reasoning.json"))
    paths += sorted(run_dir.glob("checkpoint_*_r*.json"))
    texts = _read_json_texts(paths)
    out: dict[str, dict[int, dict]] = defaultdict(dict)
    for path in sorted(ctx_dir.glob("arm_*_reflect_exploration_at*.json")):
        if path.name.endswith("_reasoning.json"):
            continue
        m = _CTX_RE.search(path.name)
        if not m:
            continue
        arm, day = m.group("arm"), int(m.group("at"))
        if day_range and not (day_range[0] <= day <= day_range[1]):
            continue
        raw = texts.get(path)
        if raw is None:
            continue
        doc = json.loads(raw)
        # 当天轨迹嵌在喂给反思 Agent 的 user 消息「完整经历」段里。
        body = next(
            (c for msg in doc.get("messages", [])
             if isinstance(c := msg.get("content"), str) and "完整经历" in c),
            None,
        )
        if body is None:
            continue
        parsed = _parse_reflect_day(body)
        if not parsed["points"]:
            continue
        _attach_biomes(parsed["points"], _load_day_biomes(ctx_dir, arm, day, texts=texts))
        out[arm][day] = parsed
    _merge_checkpoint_deaths(run_dir, out, texts=texts)
    return dict(out)


# ── planner-only（nograph）组的稀疏轨迹重建（2026-10-08 加）──────────────────
# 为什么需要单独一条路：nograph（论文条件 B，planner-only）**没有夜反思**——反思是实验设计
# 变量，被拿掉的正是它。而 load_trajectories 的轨迹源 arm_*_reflect_exploration_at*.json
# 是夜反思的输入留痕 ⇒ nograph run 一份都没有，web 地图此前显示空态。
# 但探索地图恰恰是论文要的图（B 组 vs OpenKAGE 组的探索范围对比），轨迹必须能画。
#
# 数据从哪来（三源合并，密度约为 reflect 路线的 1/3，实测 90 天 alice 1341 点 vs 2677）：
#   ① 天末观察面板 —— e1b1_<arm>_*_dayNN.json 里 tool 消息尾部的「当前观察」块
#      （KEEP_FULL_OBSERVATIONS=3，恰好每天剩 3 份：等待到时×2 + 跨午夜收尾），带精确坐标。
#      位置在消息序列里的顺序 = 真实时序。
#   ② onLog 自报坐标 —— 执行反馈里模型代码 console.log 打的 `… at {"x":..,"y":..,"z":..}`。
#      零散（90 天 alice 仅 33 条），聊胜于无。
#   ③ place_log_<Arm>.jsonl —— 放置方块埋点（blockPlaced 事件），每行带精确 x/y/z + 墙钟 t。
#      最密的一源（90 天 alice 1076 条），但**不带天号**（worldDay 是 eat_log 专属对齐字段，
#      place_log 落盘时没有——埋点代码见 mc_server/index.js 的 blockPlaced 分支）。
#      天号用 eat_log_<Arm>.jsonl 的 (t, worldDay) 锚点分段线性插值：eat_log 由 health 事件
#      驱动、覆盖全程（进食/受伤/回血每天都发生），墙钟与 place_log 同源（Date.now()）。
#      实测 90 天 alice：1076 条 place 全部落进锚点窗口、按天聚合后 88/90 天有点。
#
# 时序口径：天内顺序 = place 按 t 升序 + 面板按消息序 + onLog 按出现序，**不跨源精确插排**
# （place 的 t 与 day 文件的消息序没有共同时间轴）。代价可控：一天内的活动跨度
# 实测 median 7 格（p90 55 格），顺序错拍只会在折线上留下几格的小毛刺，不影响「去了哪」。
# 跨天衔接实测 median 17 格（天末面板→次日首 place），与 reflect 路线的「夜宿原地 ≤10 格」
# 同量级，跨天连线是安全的。
#
# 死亡断点：checkpoint 的 (day, step) step 是 env 全局步号，而这里的点是稀疏采样、
# 与步号无对齐关系（load_trajectories 那套「每天点数==当天步数」的前提不成立）。
# _merge_checkpoint_deaths 的换算偶尔会「碰巧」落在索引范围内（实测 alice 90 天 41 死
# 只命中 1 个假断点：day4 第 3 点离死亡点 24 格，纯属巧合）——假断点比不断线更糟
# （凭空断开一条没死的线），故 nograph 路线**点数对不上就直接清掉全部断点**：
# 只保留 days[].deaths 的死亡位置（前端画骷髅），线不在死点断开。
# 折中理由：nograph 组本来就以死亡频繁著称（bob 死 44 次），把线切成 45 段对稀疏折线
# 是二次伤害；骷髅标记已把死亡位置完整表达。
#
# biome：面板自带（同 _load_day_biomes 的格式），place/onLog 点按最近邻贴。


def _eatlog_day_anchors(run_dir: Path, arm: str, texts: dict | None = None) -> list[tuple[int, int]]:
    """eat_log_<Arm>.jsonl → 按墙钟升序的 [(t_ms, worldDay)] 锚点列。

    eat_log 行的 worldDay 单调不减（health 事件实时落盘）；重复天只留首个（插值锚点
    只需要「天边界在哪」）。文件缺失/空 → 空列表（调用方退化为不并 place_log）。
    """
    path = run_dir / f"eat_log_{arm.capitalize()}.jsonl"
    raw = texts.get(path) if texts is not None else (_read_one(path) if path.is_file() else None)
    if raw is None:
        return []
    anchors: list[tuple[int, int]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue  # 被 kill 的 run 可能留半行
        t, day = rec.get("t"), rec.get("worldDay")
        if isinstance(t, (int, float)) and isinstance(day, int):
            if not anchors or day > anchors[-1][1]:
                anchors.append((int(t), day))
    return anchors


def _day_of_t(t: float, anchors: list[tuple[int, int]]) -> int | None:
    """墙钟 ms → 天号（锚点分段线性插值，四舍五入）。锚点外/空 → None。"""
    if not anchors:
        return None
    if t <= anchors[0][0]:
        return anchors[0][1]
    if t >= anchors[-1][0]:
        return anchors[-1][1]
    lo, hi = 0, len(anchors) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if anchors[mid][0] <= t:
            lo = mid
        else:
            hi = mid
    (ta, da), (tb, db) = anchors[lo], anchors[hi]
    if db == da:
        return da
    return int(round(da + (db - da) * (t - ta) / (tb - ta)))


def load_trajectories_nograph(run_dir: Path) -> dict:
    """planner-only（nograph）run 的稀疏轨迹重建：三源合并成 load_trajectories 同构的
    {arm: {day: {"points": [...], "deaths": [...]}}}，供同一套 build_scene 消费。

    arm key 统一小写（eat_log/place_log 文件名是首字母大写的 Agent 名，day 文件里也是
    大写；与 reflect 路线的 arm key（小写，文件名 arm_alice_*）对齐——scene 接口按小写取）。
    一个 run 只有 e1b1 一个 slot（run.py 的 nograph 都是单 epoch 单 batch），不扫多 slot。
    解析不到任何点 → 空 dict（前端显示空态，与旧版行为一致）。
    """
    ctx_dir = run_dir / "contexts"
    if not ctx_dir.is_dir():
        return {}
    # 三源 + 死亡明细一次性并发预读（同 load_trajectories 的理由）。
    paths = sorted(p for p in ctx_dir.glob("*.json")
                   if not p.name.endswith("_reasoning.json"))
    paths += sorted(run_dir.glob("*.jsonl"))
    paths += sorted(run_dir.glob("checkpoint_*_r*.json"))
    texts = _read_json_texts(paths)
    # arm 名从 day 文件名收（e1b1_<Arm>_<run…>_dayNN.json），不写死 alice/bob/carol ——
    # scaling run 有 5/8 臂。
    arms: set[str] = set()
    for path in ctx_dir.glob("e*b*_*.json"):
        if path.name.endswith("_reasoning.json"):
            continue
        m = _CTX_DAY_RE.match(path.name)
        if m:
            arms.add(m.group("arm").lower())

    out: dict[str, dict[int, dict]] = defaultdict(dict)
    # ①+② 天末面板 + onLog 坐标（都在 day 文件的消息正文里，一趟扫完）
    panel_biomes: dict[str, dict[int, list[tuple[float, float, str]]]] = defaultdict(dict)
    for path in sorted(ctx_dir.glob("e*b*_*_day*.json")):
        if path.name.endswith("_reasoning.json"):
            continue
        m = _CTX_DAY_RE.match(path.name)
        if not m:
            continue
        arm, day = m.group("arm").lower(), int(m.group("day"))
        raw = texts.get(path)
        if raw is None:
            continue  # 半截文件（跑中被 kill）/ 读失败，跳过不炸
        try:
            doc = json.loads(raw)
        except (OSError, ValueError):
            continue
        points: list[tuple] = []
        biomes: list[tuple[float, float, str]] = []
        for msg in doc.get("messages", []):
            content = msg.get("content")
            if not (isinstance(content, str) and content.startswith("执行反馈")):
                continue
            pm = _DAY_PANEL_POS_RE.search(content)
            if pm:
                x, y, z = (float(pm.group(i)) for i in (1, 2, 3))
                points.append((len(points), x, y, z, "?"))
                bm = _BIOME_RE.search(content)
                if bm:
                    biomes.append((x, z, bm.group(1)))
            for om in _ONLOG_AT_RE.finditer(content):
                ox, oy, oz = (float(om.group(i)) for i in (1, 2, 3))
                points.append((len(points), ox, oy, oz, "?"))
        if points:
            out[arm][day] = {"points": points, "deaths": []}
            panel_biomes[arm][day] = biomes

    # ③ place_log（最密一源），天号经 eat_log 锚点插值
    for arm in arms:
        place_path = run_dir / f"place_log_{arm.capitalize()}.jsonl"
        raw = texts.get(place_path)
        if raw is None:
            continue
        anchors = _eatlog_day_anchors(run_dir, arm, texts=texts)
        if not anchors:
            continue  # 没锚点映射不了天，宁缺毋滥（①② 的点还在）
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            x, y, z, t = rec.get("x"), rec.get("y"), rec.get("z"), rec.get("t")
            if not (isinstance(x, (int, float)) and isinstance(z, (int, float))
                    and isinstance(t, (int, float))):
                continue
            day = _day_of_t(t, anchors)
            if day is None:
                continue
            day_data = out[arm].setdefault(day, {"points": [], "deaths": []})
            # 追加进当天点列；place 内部保持 t 升序（逐行读本身有序），panel 点已在前面。
            day_data["points"].append((len(day_data["points"]), float(x),
                                       float(y) if isinstance(y, (int, float)) else 0.0,
                                       float(z), "?"))

    # biome 最近邻贴（与 reflect 路线同一策略：面板有 biome，place/onLog 没有）
    for arm, days in out.items():
        for day, biomes in panel_biomes.get(arm, {}).items():
            if biomes and day in days:
                _attach_biomes(days[day]["points"], biomes)

    if out:
        _merge_checkpoint_deaths(run_dir, out, texts=texts)  # 死亡点+cause/killer 并进 days[].deaths
        # 断点清零（见本函数 docstring 上方的「死亡断点」段）：稀疏点与 env 步号无对齐，
        # 偶然命中的索引是假断点，统一丢弃；死亡位置由 deaths 字段承载。
        for days in out.values():
            for day_data in days.values():
                day_data.pop("death_breaks", None)
    return dict({a: dict(d) for a, d in out.items()})


def load_memory_layer(run_dir: Path) -> dict:
    """读各副脑图谱的**记忆层**全部概念 {arm: [{slug,name,x,y,z,is_place}]}，**含无坐标的**。

    只取 layer='memory'：经验层是"夜间地表危险"这类通名结论，本就没有坐标（也不该有）。
    名字取概念 name（"第2天工作台"/"我的主据点"），坐标在 summary 或 facet 正文里；解析不出
    坐标的（如"闪长岩之谜"）`x/y/z` 为 None —— 调用方按 x 是否为 None 过滤，
    web 侧要把它们列出来标"无坐标"，不然看图的人会以为地图漏画了。
    `is_place` 由 `_is_place` 判定 —— 地图只标地点，非地点（当天总结）只进统计。

    带 `slug` 是为了**跟图谱节点对齐**（web 端点地图上的点要能选中图谱里同一个概念）；
    name 是模型起的展示名、会重名，不能当 key。
    """
    graphs = run_dir / "arm_graphs"
    out: dict[str, list[dict]] = defaultdict(list)
    if not graphs.is_dir():
        return dict(out)
    for db in sorted(graphs.glob("arm_*.db")):
        m = _ARM_DB_RE.search(db.name)
        if not m:  # 跳过 *_vec.db 与窗口快照 *_w1.db（快照是清层前的副本，会与主库重复）
            continue
        conn = _read_only(db)
        try:
            rows = conn.execute(
                "SELECT slug, name, summary FROM concept WHERE layer='memory'").fetchall()
            for row in rows:
                facets = " ".join(
                    r["content"] for r in conn.execute(
                        "SELECT content FROM facet WHERE concept_slug=?", (row["slug"],)))
                hit = _MEM_COORD_RE.search(f"{row['summary']} {facets}")
                x = y = z = None
                if hit:
                    cx, cy, cz = (int(g) for g in hit.groups())
                    if max(abs(cx), abs(cz)) <= _COORD_LIMIT:
                        x, y, z = cx, cy, cz
                out[m.group("arm")].append({
                    "slug": row["slug"], "name": row["name"], "x": x, "y": y, "z": z,
                    "is_place": _is_place(row["name"]),
                })
        finally:
            conn.close()
    return dict(out)


def load_memory_edges(run_dir: Path) -> dict:
    """记忆层概念之间的边 {arm: [{src, relation, dst}]}。

    关系名是反思 Agent **自由命名的中文**（`位于`/`发现于`/`可从此到达`…，2026-08-09 起放开为
    自由词表），原样返回、不做归一化映射 —— 没有受控词表可映射。
    两端都在记忆层内的才要（记忆层→经验层的边落不到地图上）。web 地图用；地图本身不画边（关系走右侧图谱）。
    """
    graphs = run_dir / "arm_graphs"
    out: dict[str, list[dict]] = defaultdict(list)
    if not graphs.is_dir():
        return dict(out)
    for db in sorted(graphs.glob("arm_*.db")):
        m = _ARM_DB_RE.search(db.name)
        if not m:
            continue
        conn = _read_only(db)
        try:
            in_layer = {r["slug"] for r in conn.execute(
                "SELECT slug FROM concept WHERE layer='memory'")}
            for row in conn.execute("SELECT from_slug, relation, to_slug FROM edge"):
                if row["from_slug"] in in_layer and row["to_slug"] in in_layer:
                    out[m.group("arm")].append({
                        "src": row["from_slug"], "relation": row["relation"],
                        "dst": row["to_slug"],
                    })
        finally:
            conn.close()
    return dict(out)


def _segments(points: list[tuple], breaks: set[int] | None = None) -> list[list[tuple]]:
    """把点切成多段（每段内部才连线）。**死亡是唯一断线判据**（2026-09-30 用户定）。

    `breaks` 是"死亡发生的那一步"在 points 里的 0 基索引集合（由
    `_merge_checkpoint_deaths` 从 checkpoint 的 (day, step) 换算，见其 docstring）。
    死亡那一步的**步末点**收进当前段作段尾，下一步（重生后）开新段。

    为什么不用位移阈值判瞬移（旧 `_JUMP_GAP=30` 版已删）：pathfinder 长时限一步
    可走 30~140 格是**正常行走**，30 格阈值会把它们误切成"断开"——实测 Carol
    19 处断开里 0 处是真瞬移（最大单步 108 格，env 侧 `_TELEPORT_JUMP_BLOCKS=400`
    口径下 n_pos_jumps=0），旧图注还写着「轨迹断开 = 死亡重生瞬移」，等于
    图注在撒谎。死亡是 checkpoint 权威事实，不靠几何猜测。

    `breaks=None`（老 run 无 checkpoint / 解析不到）→ 返回单段：全天连一条，
    宁可少断也不错断。
    """
    if not points:
        return []
    if not breaks:
        return [list(points)]
    segs: list[list[tuple]] = [[]]
    for i, p in enumerate(points):
        segs[-1].append(p)
        if i in breaks:
            segs.append([])
    return [s for s in segs if s]


# 两个点相距超过这么多格就算不同「聚居区」。60 格：一个据点的日常活动半径实测在几十格内，
# 而迁址造成的簇间距是数百格 —— 中间那段很空，阈值取在两者之间即可。
# ⚠️ 不要放大到 200：实测 alice 除了 658 格外的老簇，还有个 130 格外的小簇（去村庄那趟），
# 200 会把它算进主簇，取景跨度从 60 格拉到 196 格，日常活动区又被压成一团。
_CLUSTER_GAP = 60.0


def _cluster_points(points: list[tuple]) -> list[list[tuple]]:
    """把点按 x-z 距离粗聚类，返回各簇（步数多的在前）。

    一维贪心（按 x,z 排序后按间距切）就够了：簇间距 700+ 格、簇内 < 100 格，
    差一个数量级，不值得引入 sklearn 做真聚类。
    """
    if not points:
        return []
    ordered = sorted(points, key=lambda p: (p[1], p[3]))
    clusters: list[list[tuple]] = [[ordered[0]]]
    for p in ordered[1:]:
        if math.dist((clusters[-1][-1][1], clusters[-1][-1][3]), (p[1], p[3])) > _CLUSTER_GAP:
            clusters.append([])
        clusters[-1].append(p)
    clusters.sort(key=len, reverse=True)
    return clusters


def _main_cluster(points: list[tuple]) -> tuple[list[tuple], list[tuple]]:
    """把点粗聚类，返回（步数最多的簇, 其余点）。

    为什么需要它见模块 docstring 的「主聚居区」那条：死后重生回世界原点会让副脑永久搬家，
    两簇相距 700+ 格，按全量取景会把真正的活动区压成一个点。

    ⚠️ `rest` 是**其余所有簇混在一起**的点，别对它取平均当"另一个地方的位置" ——
    实测 alice 的 rest 含 4 个互相隔着几十到几百格的簇，平均出来是 (110, 6)，
    而那个坐标 60 格内只有 3 步：它是个**没人去过的地方**。要报"另外去过哪"就得
    逐簇报，见 `_other_clusters`。
    """
    clusters = _cluster_points(points)
    if not clusters:
        return [], []
    main = clusters[0]
    rest = [p for c in clusters[1:] for p in c]
    return main, rest


def _other_clusters(points: list[tuple], visible=None, limit: int = 3) -> list[dict]:
    """除主聚居区外的其它聚居区，**逐簇**报 {steps, cx, cz, dist}（按步数降序，最多 limit 个）。

    逐簇而非取总平均：见 `_main_cluster` 的警告。超出 limit 的并进最后一项的 `more` 计数，
    不静默丢 —— 「另有 N 步在别处」这句话必须说出来，否则图上看不出它还去过 700 格外。

    `visible(p)` 给了的话，**框内的点不算"另有"** —— 取景框比主簇包围盒大（加了地点又乘了
    1.12 留白），紧邻主簇的小簇其实是画得出来的。不排掉它们会出现「图注说另有 41 步在 15 格外，
    而那 41 步就明明画在图中央」的自相矛盾。
    """
    clusters = _cluster_points(points)
    if len(clusters) <= 1:
        return []
    main = clusters[0]
    mx = sum(p[1] for p in main) / len(main)
    mz = sum(p[3] for p in main) / len(main)
    others = []
    for c in clusters[1:]:
        offframe = [p for p in c if not visible(p)] if visible else c
        if offframe:
            others.append(offframe)
    out = []
    for c in others[:limit]:
        cx = sum(p[1] for p in c) / len(c)
        cz = sum(p[3] for p in c) / len(c)
        out.append({"steps": len(c), "cx": round(cx, 1), "cz": round(cz, 1),
                    "dist": round(math.dist((cx, cz), (mx, mz)), 1)})
    tail = others[limit:]
    if tail:
        out.append({"steps": sum(len(c) for c in tail), "cx": None, "cz": None,
                    "dist": None, "more": len(tail)})
    return out

# ── §B 探索覆盖范围：最小包围圆（原 render_arm_map.py）──────────────────────────

# ── 最小包围圆：Welzl 随机增量法，期望 O(n) ──────────────────────────────────
# 暴力版是 O(n³)（两两直径圆 × 全点包含检查），全量轨迹 ~800 点要几亿次距离计算。
# 洗牌用固定种子 LCG（不用 random）：同一份数据画出的圆可复现。
def _circle2(a, b):
    return ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2, math.hypot(a[0] - b[0], a[1] - b[1]) / 2)


def _circle3(a, b, c):
    d = 2 * (a[0] * (b[1] - c[1]) + b[0] * (c[1] - a[1]) + c[0] * (a[1] - b[1]))
    if abs(d) < 1e-9:
        return None
    a2, b2, c2 = a[0] ** 2 + a[1] ** 2, b[0] ** 2 + b[1] ** 2, c[0] ** 2 + c[1] ** 2
    cx = (a2 * (b[1] - c[1]) + b2 * (c[1] - a[1]) + c2 * (a[1] - b[1])) / d
    cy = (a2 * (c[0] - b[0]) + b2 * (a[0] - c[0]) + c2 * (b[0] - a[0])) / d
    return (cx, cy, math.hypot(a[0] - cx, a[1] - cy))


def _in_circle(c, p, eps=1e-6):
    return math.hypot(p[0] - c[0], p[1] - c[1]) <= c[2] + eps


def _circle_through(a, b, c):
    """过 a,b,c 三点的最小圆：先试外接圆（锐角三角形时它最小）；退化（共线）或外接圆
    罩不住三个点时，最小圆必是某对点的直径圆 —— 枚举三点对取能罩住的最小者。
    """
    c3 = _circle3(a, b, c)
    if c3 and all(_in_circle(c3, p) for p in (a, b, c)):
        return c3
    best = None
    for p, q in ((a, b), (a, c), (b, c)):
        cand = _circle2(p, q)
        if all(_in_circle(cand, x) for x in (a, b, c)) and (best is None or cand[2] < best[2]):
            best = cand
    return best


def min_enclosing_circle(points):
    """points: [(x, z), …] → (cx, cz, r)，世界坐标。空输入返回 None。

    ★ Welzl 写成**迭代**，不写 `_mec(rest, bd)` 递归：递归版深度 = 点数，而 reflect 全量
      轨迹实测 1001~1232 点 —— 撞 Python 默认递归上限 1000 直接 RecursionError 崩掉覆盖圆计算
      （不是数据问题，算不出来时别去改数据）。迭代版是同一算法的展开：逐个点加入，
      新点落在当前圆外就「以它为边界点」重算（内层同理再退一层），期望 O(n)。
      最小圆唯一，故与递归版逐点同结果。
    """
    pts = list(points)
    if not pts:
        return None
    if len(pts) == 1:
        return (pts[0][0], pts[0][1], 0)
    seed = 0x2F6E2B1
    for i in range(len(pts) - 1, 0, -1):
        seed = (seed * 1103515245 + 12345) & 0x7FFFFFFF
        j = seed % (i + 1)
        pts[i], pts[j] = pts[j], pts[i]

    circle = None
    for i, p in enumerate(pts):
        if circle is not None and _in_circle(circle, p):
            continue
        circle = (p[0], p[1], 0.0)                  # 边界点 = {p}
        for j in range(i):
            q = pts[j]
            if _in_circle(circle, q):
                continue
            circle = _circle2(p, q)                 # 边界点 = {p, q}
            for k in range(j):
                r = pts[k]
                if _in_circle(circle, r):
                    continue
                circle = _circle_through(p, q, r)   # 边界点 = {p, q, r}
    return circle

# ── §C 地形底图：checkpoint 世界俯瞰渲染（原 world_render.py）────────────────────

_CKPT_RE = re.compile(r"checkpoint(\d+)$", re.I)
_CACHE_DIR = Path("/tmp/openkage_viewer_terrain")
_CACHE_TTL_SECONDS = 3600

# 同一世界指纹只允许一个渲染在跑（FastAPI 线程池里 scene 接口和 PNG 接口可能并发
# 触发同一冷渲染，无锁时会双倍烧 CPU 各画 50 秒）。跨进程不共享 —— 单 uvicorn 进程内够用。
_RENDER_LOCKS: dict[str, threading.Lock] = {}
_RENDER_LOCKS_GUARD = threading.Lock()


def render_lock(fingerprint: str) -> threading.Lock:
    """指纹 → 渲染锁。viewer/server.py 的 PNG 端点拿同一把锁等冷渲染完成。"""
    with _RENDER_LOCKS_GUARD:
        return _RENDER_LOCKS.setdefault(fingerprint, threading.Lock())

# 常见地形/建造方块的地图色。这里追求论文图可读性，不是严格贴图颜色。
_BLOCK_COLORS = {
    "grass_block": (112, 180, 74), "dirt": (134, 96, 67), "coarse_dirt": (119, 85, 59),
    "stone": (126, 126, 126), "deepslate": (80, 80, 86), "cobblestone": (128, 128, 128),
    "sand": (231, 215, 150), "gravel": (134, 129, 126), "sandstone": (216, 203, 147),
    "water": (58, 101, 212), "ice": (160, 201, 241), "lava": (226, 87, 30),
    "snow": (240, 246, 247), "clay": (161, 165, 171),
    "oak_leaves": (58, 138, 49), "birch_leaves": (106, 166, 70), "spruce_leaves": (50, 101, 62),
    "oak_log": (102, 81, 49), "birch_log": (215, 201, 157), "spruce_log": (58, 37, 16),
    "oak_planks": (176, 140, 85), "birch_planks": (198, 179, 123), "spruce_planks": (114, 84, 48),
    "coal_ore": (80, 80, 80), "iron_ore": (150, 124, 102), "copper_ore": (174, 110, 76),
    "gold_ore": (164, 138, 73), "redstone_ore": (140, 66, 58), "lapis_ore": (64, 86, 149),
    "diamond_ore": (94, 136, 156), "deepslate_iron_ore": (100, 89, 90),
    "tuff": (104, 108, 103), "andesite": (136, 136, 136), "granite": (149, 103, 85),
    "diorite": (187, 187, 189), "calcite": (224, 225, 224), "amethyst_block": (139, 107, 199),
    "smooth_basalt": (73, 70, 76), "magma_block": (137, 60, 39), "obsidian": (21, 18, 31),
    "oak_fence": (150, 119, 71), "oak_stairs": (167, 132, 80), "crafting_table": (137, 99, 58),
    "furnace": (116, 116, 116), "chest": (160, 120, 55), "barrel": (134, 98, 56),
    "short_grass": (114, 168, 72), "tall_grass": (114, 168, 72), "bush": (103, 150, 68),
    "seagrass": (51, 125, 104), "tall_seagrass": (51, 125, 104), "kelp": (65, 127, 106),
    "dandelion": (201, 181, 58), "poppy": (218, 44, 40), "oxeye_daisy": (226, 226, 214),
    "leaf_litter": (143, 115, 59), "glow_lichen": (88, 132, 92), "cobweb": (226, 226, 226),
    "vine": (74, 126, 54), "sugar_cane": (185, 207, 145), "lily_pad": (83, 148, 69),
    "copper_block": (191, 116, 77), "raw_copper_block": (165, 101, 69),
    "waxed_copper_block": (187, 109, 72), "raw_iron_block": (193, 164, 149),
    "bedrock": (85, 85, 85),
}
_FALLBACK_RULES = (
    ("water", (58, 101, 212)), ("leaves", (58, 138, 49)), ("log", (102, 81, 49)),
    ("planks", (176, 140, 85)), ("ore", (120, 120, 120)), ("copper", (188, 112, 72)),
    ("deepslate", (80, 80, 86)), ("tuff", (104, 108, 103)), ("candle", (222, 188, 163)),
    ("bed", (190, 68, 64)), ("glass", (207, 231, 232)), ("terracotta", (161, 91, 61)),
    ("amethyst", (139, 107, 199)), ("torch", (235, 178, 85)), ("rail", (150, 150, 150)),
)


def _color(name: str) -> tuple[int, int, int]:
    key = name.removeprefix("minecraft:")
    if key in _BLOCK_COLORS:
        return _BLOCK_COLORS[key]
    return next((c for token, c in _FALLBACK_RULES if token in key), (128, 128, 128))


def _unpack_indices(block_states) -> list[int]:
    palette = block_states.get("palette")
    data = block_states.get("data")
    if data is None:
        return [0] * 4096
    bits = max(4, (max(1, len(palette)) - 1).bit_length())
    mask, per_long = (1 << bits) - 1, 64 // bits
    out: list[int] = []
    for raw in data:
        value = int(raw) & ((1 << 64) - 1)  # TAG_Long 是有符号；块索引按无符号位域解释
        for _ in range(per_long):
            out.append(value & mask)
            value >>= bits
            if len(out) == 4096:
                return out
    return out


def _unpack_biomes(biomes) -> list[str]:
    palette = [str(x) for x in biomes["palette"]]
    data = biomes.get("data")
    if data is None:
        return [palette[0]] * 64
    bits = max(1, (max(1, len(palette)) - 1).bit_length())
    mask, per_long = (1 << bits) - 1, 64 // bits
    values: list[int] = []
    for raw in data:
        value = int(raw) & ((1 << 64) - 1)
        for _ in range(per_long):
            values.append(value & mask)
            value >>= bits
            if len(values) == 64:
                break
        if len(values) == 64:
            break
    return [palette[v] if v < len(palette) else palette[0] for v in values]


# 把生物群系转成比“逐方块贴图色”更强的地图分类色。否则平原/森林视角下整张图确实
# 几乎全是同一种绿；地形图需要让人一眼看出河流、沙滩、雪坡、洞穴和植被差异。
_BIOME_COLORS = {
    "ocean": (47, 90, 197), "deep_ocean": (35, 73, 166), "warm_ocean": (48, 113, 208),
    "cold_ocean": (58, 113, 191), "frozen_ocean": (147, 189, 228), "river": (63, 120, 226),
    "frozen_river": (162, 207, 238), "beach": (233, 216, 149), "stony_shore": (162, 157, 136),
    "desert": (235, 199, 106), "savanna": (191, 178, 89), "plains": (151, 194, 91),
    "sunflower_plains": (164, 202, 93), "meadow": (136, 186, 94),
    "forest": (73, 140, 62), "flower_forest": (89, 154, 72),
    "birch_forest": (126, 174, 89), "old_growth_birch_forest": (132, 180, 92),
    "dark_forest": (49, 107, 49), "taiga": (86, 126, 95), "snowy_taiga": (176, 202, 194),
    "jungle": (58, 135, 58), "bamboo_jungle": (89, 152, 69),
    "swamp": (93, 110, 66), "mangrove_swamp": (100, 112, 61),
    "snowy_plains": (232, 240, 238), "grove": (206, 220, 208),
    "snowy_slopes": (220, 235, 230), "jagged_peaks": (233, 242, 239), "frozen_peaks": (229, 240, 238),
    "windswept_hills": (132, 153, 116), "windswept_gravelly_hills": (145, 146, 134),
    "windswept_forest": (98, 136, 90), "badlands": (191, 107, 52),
    "wooded_badlands": (172, 105, 55), "cherry_grove": (226, 174, 189),
    "dripstone_caves": (126, 95, 73), "lush_caves": (85, 145, 74),
    "deep_dark": (57, 60, 71), "mountains": (143, 160, 138),
}


def _surface_color(block: str, biome: str | None = None) -> tuple[int, int, int]:
    """官方 filled_map 的 MaterialColor 基色（1.19.4 反编译值）。

    官方地图不是按纹理/生物群系画渐变，而是把方块映射到少数 MapColor，再用三档亮度
    （180/220/255）表现高度与水深。这里复刻常见地表方块的映射。
    """
    key = block.removeprefix("minecraft:")
    if key in ("grass_block", "short_grass", "tall_grass", "bush", "moss_block",
               "moss_carpet", "slime_block", "honey_block"):
        return (0x7f, 0xb2, 0x38)                 # GRASS
    if key in ("oak_leaves", "birch_leaves", "spruce_leaves", "dark_oak_leaves",
               "azalea_leaves", "mangrove_roots", "vine", "lily_pad", "sugar_cane"):
        return (0x00, 0x7c, 0x00)                 # PLANT
    if key in ("sand", "red_sand", "gravel", "sandstone", "red_sandstone", "clay",
               "suspicious_sand", "suspicious_gravel", "raw_gold_block"):
        return (0xf7, 0xe9, 0xa3)                 # SAND
    if key in ("dirt", "coarse_dirt", "rooted_dirt", "podzol", "dirt_path", "farmland"):
        return (0x97, 0x6d, 0x4d)                 # DIRT
    if key in ("stone", "deepslate", "cobblestone", "andesite", "diorite", "granite",
               "tuff", "calcite", "cobbled_deepslate", "smooth_basalt", "obsidian",
               "bedrock", "pointed_dripstone", "amethyst_block", "rail", "spawner",
               "trial_spawner", "vault") or key.endswith("_ore"):
        return (0x70, 0x70, 0x70)                 # STONE
    if key in ("water", "seagrass", "tall_seagrass", "kelp", "kelp_plant",
               "bubble_column", "lava"):
        return (0x40, 0x40, 0xff)                 # WATER
    if key in ("snow", "snow_block", "powder_snow"):
        return (0xff, 0xff, 0xff)                 # SNOW
    if key == "ice" or key.startswith("packed_ice") or key.startswith("blue_ice"):
        return (0xa0, 0xa0, 0xff)                 # ICE
    if key in ("oak_log", "birch_log", "spruce_log", "dark_oak_log", "oak_planks",
               "birch_planks", "spruce_planks", "dark_oak_planks", "oak_fence",
               "birch_fence", "oak_stairs", "crafting_table", "chest", "barrel",
               "furnace", "bee_nest"):
        return (0x8f, 0x77, 0x48)                 # WOOD
    if key in ("iron_block", "raw_iron_block", "copper_block", "waxed_copper_block",
               "waxed_exposed_copper", "waxed_weathered_copper",
               "waxed_oxidized_copper", "hopper", "dispenser"):
        return (0xa7, 0xa7, 0xa7)                 # METAL
    if key in ("snowy", "frozen"):
        return (0xa0, 0xa0, 0xff)
    # 未列出的方块用先前的纹理近似色兜底，仍会经过官方三档亮度处理。
    return _color(block)


def _read_chunk(path: Path, offset: int) -> nbt.NBTFile | None:
    try:
        with path.open("rb") as f:
            f.seek(offset)
            return _read_chunk_from(f)
    except (OSError, zlib.error, EOFError):
        return None


def _read_chunk_from(f) -> nbt.NBTFile | None:
    """从已定位到 chunk 头的文件对象解一个 chunk（批量读时复用同一 fd）。"""
    try:
        length = int.from_bytes(f.read(4), "big")
        f.read(1)  # compression type；当前世界实测为 zlib
        return nbt.NBTFile(buffer=io.BytesIO(zlib.decompress(f.read(length - 1))))
    except (OSError, zlib.error, EOFError):
        return None


def _chunk_surface(chunk: nbt.NBTFile):
    top = [[None] * 16 for _ in range(16)]
    height = [[-320] * 16 for _ in range(16)]
    biome = [["minecraft:plains"] * 16 for _ in range(16)]
    water_depth = [[0] * 16 for _ in range(16)]
    counting_water = [[False] * 16 for _ in range(16)]
    sections = [s for s in chunk["sections"] if "block_states" in s]
    sections.sort(key=lambda s: int(s["Y"].value), reverse=True)
    found_all = False
    for sec in sections:
        if found_all:
            break
        states = sec["block_states"]
        palette = [str(e["Name"]) for e in states["palette"]]
        if len(palette) == 1 and palette[0] == "minecraft:air":
            continue
        indices = _unpack_indices(states)
        biome_names = _unpack_biomes(sec["biomes"]) if "biomes" in sec else ["minecraft:plains"] * 64
        base_y = int(sec["Y"].value) * 16
        for y in range(15, -1, -1):
            for z in range(16):
                row = top[z]
                for x in range(16):
                    if row[x] is not None:
                        continue
                    name = palette[indices[(y << 8) | (z << 4) | x]]
                    if name not in ("minecraft:air", "minecraft:cave_air"):
                        is_water = (name == "minecraft:water" or name in
                                    ("minecraft:seagrass", "minecraft:tall_seagrass",
                                     "minecraft:kelp", "minecraft:kelp_plant",
                                     "minecraft:bubble_column"))
                        if row[x] is None:
                            row[x] = name
                            height[z][x] = base_y + y
                            biome[z][x] = biome_names[((y // 4) << 4) | ((z // 4) << 2) | (x // 4)]
                            water_depth[z][x] = 1 if is_water else 0
                            counting_water[z][x] = is_water
                        elif counting_water[z][x]:
                            if is_water:
                                water_depth[z][x] += 1
                            else:
                                counting_water[z][x] = False
        found_all = all(v is not None for row in top for v in row)
    return top, height, biome, water_depth


def _latest_world(run_dir: Path, arm: str) -> Path | None:
    candidates = []
    for p in run_dir.iterdir():
        if not p.is_dir() or not _CKPT_RE.fullmatch(p.name):
            continue
        world = p / f"arm_{arm}" / "world" / "region"
        if world.is_dir() and any(world.glob("*.mca")):
            candidates.append((int(_CKPT_RE.fullmatch(p.name).group(1)), world))
    return max(candidates, default=(0, None))[1]


def _region_fingerprint(region_dir: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(region_dir.glob("*.mca")):
        try:
            st = p.stat()
            h.update(f"{p.name}:{st.st_mtime_ns}:{st.st_size};".encode())
        except OSError:
            pass
    return h.hexdigest()[:20]


def render_world_topdown(world_dir: Path, output_dir: Path | None = None) -> dict:
    region_dir = world_dir / "region"
    if not region_dir.is_dir():
        raise FileNotFoundError(f"world/region 不存在: {world_dir}")
    fp = _region_fingerprint(region_dir)
    cache_dir = output_dir or _CACHE_DIR
    cache_dir.mkdir(parents=True, exist_ok=True)
    png_path = cache_dir / f"terrain-{fp}.png"
    meta_path = cache_dir / f"terrain-{fp}.json"
    if png_path.is_file() and meta_path.is_file():
        try:
            return json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass

    # 冷渲染一次几十秒（几百 chunk 的 NBT 解码），不能让 scene/PNG 两个端点并发
    # 各画一份 —— 拿指纹级锁串行化；后到者在锁上等，醒来重查缓存即秒回。
    with render_lock(fp):
        if png_path.is_file() and meta_path.is_file():
            try:
                return json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass

        # 每个 region 文件只 open 一次、批量读它名下所有 chunk。逐 chunk open
        # 在网络盘上一次 ~34ms，2 千 chunk 就是 70+ 秒纯 open（2026-10-09
        # 实测 93s 里 75s 是 _io.open）；改成每文件一次 open 后冷渲染 52s → ~10s。
        chunks: dict[tuple[int, int], tuple] = {}
        for path in sorted(region_dir.glob("*.mca")):
            try:
                with path.open("rb") as f:
                    header = f.read(4096)
                    for i in range(1024):
                        b = header[i * 4:i * 4 + 4]
                        offset = int.from_bytes(b[:3], "big") * 4096
                        if not offset:
                            continue
                        f.seek(offset)
                        chunk = _read_chunk_from(f)
                        if chunk is None or str(chunk.get("Status", "")) != "minecraft:full":
                            continue
                        chunks[(int(chunk["xPos"].value), int(chunk["zPos"].value))] = \
                            _chunk_surface(chunk)
            except OSError:
                continue

    if not chunks:
        raise RuntimeError("checkpoint 世界里没有可渲染的 full chunk")

    xs, zs = [c[0] for c in chunks], [c[1] for c in chunks]
    min_x, min_z = min(xs) * 16, min(zs) * 16
    max_x, max_z = (max(xs) + 1) * 16, (max(zs) + 1) * 16
    # 没生成/没保存 chunk 的区域直接填官方地图的“未探索羊皮纸底色”。
    # 不用透明：部分查看器/浏览器主题会把透明渲染成黑底，误以为地形坏了。
    img = Image.new("RGBA", (max_x - min_x, max_z - min_z), (244, 239, 223, 255))
    pix = img.load()
    # 官方 filled_map 的三档亮度（MaterialColor.Brightness LOW/NORMAL/HIGH）。
    # 非水：与同一列上一个像素的地表高度比较，高 -> 255，低 -> 180，平 -> 220；
    # 水：按水深 + 棋盘抖动分档，浅水更亮、深水更暗。这个“阶梯式亮度”是官方地图
    # 看起来有一格格地形阴影的关键，不能用连续坡面渐变替代。
    low, normal, high = 180 / 255, 220 / 255, 255 / 255
    for wx in range(min_x, max_x):
        prev_height = None
        cx, lx = divmod(wx, 16)
        for wz in range(min_z, max_z):
            cz, lz = divmod(wz, 16)
            surface = chunks.get((cx, cz))
            if surface is None:
                prev_height = None
                continue
            top, height, _biomes, water_depth = surface
            name = top[lz][lx]
            if not name:
                prev_height = None
                continue
            y = height[lz][lx]
            bkey = name.removeprefix("minecraft:")
            is_water = bkey in ("water", "seagrass", "tall_seagrass", "kelp",
                                "kelp_plant", "bubble_column")
            checker = (wx + wz) & 1
            if is_water:
                score = water_depth[lz][lx] * 0.1 + checker * 0.2
                brightness = high if score < 0.5 else (low if score > 0.9 else normal)
            else:
                delta = 0.0 if prev_height is None else float(y - prev_height)
                score = delta * 4.0 / 4.0 + (checker - 0.5) * 0.4
                brightness = high if score > 0.6 else (low if score < -0.6 else normal)
            prev_height = y
            rgb = _surface_color(name)
            pix[wx - min_x, wz - min_z] = tuple(
                [max(0, min(255, round(v * brightness))) for v in rgb] + [255])

    img.save(png_path, optimize=True)
    meta = {"path": str(png_path), "min_x": min_x, "min_z": min_z,
            "max_x": max_x, "max_z": max_z, "chunks": len(chunks), "fingerprint": fp}
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    return meta
