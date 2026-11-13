# -*- coding: utf-8 -*-
"""Read-only web viewer for the released run results.

Cards are grouped by experiment x backbone over results/<cell>/runNN. Per-run views:
  - main-table metrics and per-agent item/advancement inventories (from checkpoints)
  - knowledge-graph canvases (from the shipped graph databases, when present)
  - per-agent exploration maps (from contexts/, when present)

Start with `python viewer/server.py` and open http://127.0.0.1:8090/.
The prebuilt frontend is in frontend/dist; rebuild with
`cd viewer/frontend && npm install && npm run build`.
Set OPENKAGE_RESULTS_DIR to point at a different results root.
"""
from __future__ import annotations

import asyncio
import copy
import gzip
import hashlib
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

# consolidation round 号的解码：**唯一真相在框架侧**（编码也在那儿），不在这里另写一份 divmod。
# 注：这会连带执行 `openkage/__init__.py`（把 brain/memory.graph 一起载入）。**无副作用**
# —— embedder/sqlite-vec 都是在 BrainGraph 构造时才初始化，不在 import 时；只是多载几个模块。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import openkage.roundcode as roundcode  # noqa: E402
# 主表指标口径的唯一固化出处（Deaths/Eq-max/Tool era/Obsidian/Nether…），/api/metrics/runs
# 逐臂指标直接调它，不在这里重算（见「量化指标」小节注释）。
import viewer._deps.main_table_metrics as main_table_metrics  # noqa: E402
# 层标记的字面量**从框架取**，不在这里写死 "experience"：副脑卡筛「被清掉的经验层」靠它
# （见 `_merge_cleared_experience`），字面量各存一份的话框架改名这里会静默筛出空集。
from openkage.memory.schema import LAYER_EXPERIENCE  # noqa: E402

# 探索地图（web 版）的数据层：轨迹/记忆层地点/记忆层边的解析全在 world_scene_render 里，
# 本文件只把它拼成 JSON。**解析逻辑一份都不在这里重写** —— 那个模块的坐标正则、"哪些概念
# 算地点"的判据、主聚居区取景都是画错好几版才定下来的（见其模块 docstring），另写一份必然漂移。
# 显式把本目录加进 sys.path：`python server.py` 时 cwd 恰好就是这儿所以不加也能 import，
# 但从别处以模块方式载入（uvicorn 指别的工作目录、或被 pytest 收集）就会 ModuleNotFoundError。
sys.path.insert(0, str(Path(__file__).resolve().parent))
import world_scene_render as wsr  # noqa: E402

FRONTEND_DIR = Path(__file__).resolve().parent / "frontend" / "dist"

# ── 扫描根 = 仓内 results/（匿名发布布局：results/<cell>/runNN）──────────────────
# 发布包的 results/ 不带机器层、也不带图谱 db：每个 cell 一个目录（ds_openkage /
# glm53_planner_only / abl_no_sharing …，全表见 results/manifest.tsv），里面每个 runNN
# 一个子目录，只放扁平 checkpoint_<agent>_r*.json。root_key 因此就是 cell 名，卡片
# slug 带 `<cell>/` 前缀（见 `_slug_with_root`）。
#
# ★ cell 目录**实时探测**、不写死清单：往 results/ 里加新 cell 不用改代码，下一轮
#   扫描自然出现。非 cell 文件靠 `_NON_CELL_ENTRIES` 跳过（manifest.tsv 等）。
_NON_CELL_ENTRIES = {"manifest.tsv", "logs", "footage", "old_results"}
#: results 落盘根可配（与 experiments/run.py 的 OPENKAGE_RESULTS_DIR 同名同义）。
_RESULTS_ROOT = Path(os.environ.get("OPENKAGE_RESULTS_DIR")
                     or Path(__file__).resolve().parent.parent / "results")


def _results_roots() -> list[tuple[str, Path]]:
    """[(root_key=cell 名, results/<cell>), ...]，按 cell 名排序（顺序稳定 ——
    无前缀 slug 兜底时要取第一个根，见 `_root_of_run`）。

    每次实时探测：新 cell 可能加进来，缓存会让「刚放进来的 run」迟迟不出现。
    """
    if not _scan_is_dir(_RESULTS_ROOT):
        return []
    out: list[tuple[str, Path]] = []
    for p in _scan_iterdir(_RESULTS_ROOT):
        if p.name.startswith("_") or p.name in _NON_CELL_ENTRIES:
            continue
        if _scan_is_dir(p):
            out.append((p.name, p))
    return sorted(out)


def _root_path(root_key: str | None) -> Path | None:
    """root_key（cell 名）→ 该 cell 的 results 子目录；None/未知 cell 返回 None。"""
    for k, p in _results_roots():
        if k == root_key:
            return p
    return None


def _root_label(root_key: str | None) -> str:
    """卡片上的来源标注 = cell 名（run 所属的实验 cell）。"""
    return root_key or "未知 cell"


def _slug_with_root(root_key: str | None, raw_slug: str) -> str:
    """给 book_slug 加 cell 名前缀，消除「同一 run 目录名出现在多个 cell」的歧义。

    反解见 `_split_slug_root`。
    """
    if not root_key:
        return raw_slug
    return f"{root_key}/{raw_slug}"


def _root_of_run(raw_slug: str) -> Path:
    """无前缀 slug（历史链接/收藏）→ 它所在的 cell 目录。

    逐个根探一次目录在不在，命中就用它；一个都没命中（run 已删）就退回第一个根，
    让调用方照常 404，不至于整页报错。
    """
    run_part = raw_slug.partition("__")[0]
    roots = _results_roots()
    for _k, root in roots:
        if _scan_is_dir(root / run_part):
            return root
    return roots[0][1] if roots else _RESULTS_ROOT


def _split_slug_root(book_slug: str) -> tuple[str, Path]:
    """book_slug → (纯 slug, 该卡所在的 cell 目录)。

    前缀不是已知 cell 名（含完全没有前缀的旧 slug）就按无前缀处理，
    走 `_root_of_run` 现找。
    """
    head, sep, tail = book_slug.partition("/")
    if sep and head in {k for k, _ in _results_roots()}:
        root = _root_path(head)
        if root is not None:
            return tail, root
    return book_slug, _root_of_run(book_slug)
# 主脑/对照组在 run_dir 下的固定文件名。alfworld 早期 run 用的是 primary.db（brain.db 的旧名），
# 一并认，免得历史 run 打不开。
_BRAIN_DB_NAMES = ("brain.db", "primary.db")
_FROZEN_DB_NAME = "frozen.db"
# 副脑图谱：arm_<key>_e<epoch>b<batch>.db。key 是副脑角色 md 的文件名 stem（见 Arm.from_file），
# 本身可能带下划线（a1_tech_climber），所以 key 段用非贪婪匹配、靠尾部 _e<数字>b<数字> 定界。
_ARM_FILE_RE = re.compile(r"^arm_(?P<key>.+?)_e(?P<epoch>\d+)b(?P<batch>\d+)\.db$")
# 题内聚合的**窗口快照**：arm_<key>_e<E>b<B>_w<W>.db。
#
# ★ 这是一个**独立**的正则，绝不能把 `_w<N>` 并进 `_ARM_FILE_RE`：后者决定「哪些文件出一张卡」，
#   快照要是也出卡，一个 3 arm × 6 窗口的 run 就会多出 18 张卡把卡片列表冲垮。快照的用途是
#   **并进它所属的那张副脑卡**（见 `_arm_snapshots` / `build_memory_graph` 的 snapshots 参数）。
_ARM_SNAP_RE = re.compile(
    r"^arm_(?P<key>.+?)_e(?P<epoch>\d+)b(?P<batch>\d+)_w(?P<window>\d+)\.db$")
# 题内聚合窗口的 checkpoint 目录：checkpoint{window}/（window.py 用 `f"checkpoint{window}"`
# 当目录名）。窗口号就在目录号里，与窗口序号同源 —— 见 `_arm_snapshots` 的注释。
_CHECKPOINT_DIR_RE = re.compile(r"^checkpoint(?P<window>\d+)$")
# 副脑图谱所在子目录：arm_graphs 是现命名，ephemeral 是 alfworld 早期 run 的旧命名。
_ARM_SUBDIRS = ("arm_graphs", "ephemeral")
# domain key → 展示名（跟 domains/ 对齐；找不到就用 key 本身）
_DOMAIN_DISPLAY = {"math": "数学", "alfworld": "ALFWorld（已删域）", "minecraft": "Minecraft"}


# 发布包的 run 目录名就是 `runNN`（launch order，见 results/manifest.tsv）——没有
# domain/ts/host/model/graph 段。run 的全部元数据（条件/模型/规模）都从**cell 名**推：
# cell = `<backbone>[_90d]_<method>`（ds_openkage / glm53_planner_only / abl_no_sharing /
# glm53flash_90d_openkage …）。
_RUN_DIR_RE = re.compile(r"^run\d+$")


def _fmt_run_slug(run_slug: str) -> str:
    """cell 内的 run 目录名直接展示（run03 → run03）。"""
    return run_slug


def _fmt_run_ts(run_slug: str) -> str:
    """发布布局没有起跑时间戳，run 目录名本身（launch order）就是定位键。"""
    return run_slug


def _domain_of_run(run_slug: str) -> str:
    """发布包只在 minecraft 域上跑过（README Layout 节）。"""
    return "minecraft"


# ── cell 名解析：`<backbone>[_90d]_<method>` ─────────────────────────────────
# backbone 段（与论文 Table config 行同词）：DeepSeek-V4.1-Flash（cell 名里缩写 ds）、
# GLM-5.3（glm53）、GLM-5.3-Flash（glm53flash）、hy4-preview（hy4）、Kimi-K3（kimi_k3）。
# method 段：openkage / planner_only（baseline B）/ voyager / echo_adapted /
# abl_no_sharing（−S）/ abl_flat_retrieval（−R）/ abl_no_memory_layer（−M）/
# abl_no_experience_layer（−E）。
# ★ 用词与论文五条件对齐（附录 ablation conditions）：−S 无共享 / −R 平面检索 /
#   −M 无记忆层 / −E 无经验层 / B 无图（bare model，即 planner-only）。
_CELL_MODEL_NAMES = {
    "ds": "DeepSeek-V4.1-Flash",
    "glm53": "GLM-5.3",
    "glm53flash": "GLM-5.3-Flash",
    "hy4": "hy4-preview",
    "kimi_k3": "Kimi-K3",
}
_METHOD_LABELS = {
    "openkage": "OpenKAGE",
    "planner_only": "baseline（B 无图）",
    "voyager": "Voyager 基线",
    "echo_adapted": "Echo 基线（适配版）",
    "no_sharing": "消融 −S 无共享",
    "flat_retrieval": "消融 −R 平面检索",
    "no_memory_layer": "消融 −M 无记忆层",
    "no_experience_layer": "消融 −E 无经验层",
    "raw_shared_pool": "消融 Raw 原样共享（不整合）",
    # transfer / 群体规模 / 时间尺度（附录 fig:transfer 与 fig:scaling）
    "transfer": "迁移（transfer）",
    "non_transfer": "迁移对照（non-transfer）",
    "openkage_seed_source": "迁移源（seed source）",
    "n5_openkage": "N=5 群体规模",
    "n8_openkage": "N=8 群体规模",
    "60d_openkage": "60 夜时间尺度",
}
_ABLATION_METHOD_KEYS = {"no_sharing", "flat_retrieval", "no_memory_layer",
                         "no_experience_layer", "raw_shared_pool"}


def _parse_cell(cell: str) -> dict:
    """cell 名 → {'model','model_raw','condition','condition_label','scaling'}。

    形状不认识时返回空 dict（调用方按「未知」处理，不要因此丢卡）。
    """
    # 规模限定段（_90d/_60d/_n5/_n8）可以在任意位置——整段剥掉再判，scaling 标记汇总
    quals = []
    body = cell
    for q in ("_90d_", "_60d_", "_n5_", "_n8_"):
        while q in body:
            body = body.replace(q, "_")
            quals.append(q.strip("_"))
    for q in ("_90d", "_60d", "_n5", "_n8"):
        if body.endswith(q):
            body = body[: -len(q)]
            quals.append(q[1:])
    parts = body.split("_", 1)
    if len(parts) < 2:
        return {}
    model_raw, method_raw = parts
    # kimi_k3_* 的 backbone 段本身带下划线：第一段拆出 "kimi" 时把 "k3_" 并回 backbone
    if model_raw == "kimi" and method_raw.startswith("k3_"):
        model_raw, method_raw = "kimi_k3", method_raw[3:]
    # 消融 cell（abl_*）都跑在默认 backbone DeepSeek-V4.1-Flash 上（论文 config 表），
    # cell 名里不再带 backbone 段
    if model_raw == "abl":
        model_raw = "ds"
    model = _CELL_MODEL_NAMES.get(model_raw, model_raw)
    method = (method_raw[4:] if method_raw.startswith("abl_") else method_raw)
    label = _METHOD_LABELS.get(method, method or "未知条件")
    return {"model": model, "model_raw": model_raw, "condition": method,
            "condition_label": label, "scaling": bool(quals), "scale": quals}


def _run_meta_fields(run_dir: Path, root_key: str | None) -> dict:
    """卡片要带的分级/筛选字段（实验分类、模型、规模…），供前端按
    实验→模型→run 三级分组 + 卡片为第四级。root_key = cell 名：发布布局里 run 的
    元数据（模型/条件）都在 cell 上，规模（臂数/夜数）从 checkpoint 读。"""
    run_slug = run_dir.name
    p = _parse_cell(root_key or "")
    scale = _run_scale_meta(run_dir)
    cat_key, cat_label = _exp_category(root_key or "", scale)
    return {
        "root": root_key or "",
        "root_label": _root_label(root_key),
        "run_slug": run_slug,
        "model": p.get("model") or "未知模型",
        "model_raw": p.get("model_raw") or "",
        "condition": p.get("condition") or "未知条件",
        "condition_label": p.get("condition_label") or "未知条件",
        "host": "",
        "run_ts": _fmt_run_ts(run_slug),
        "exp_category": cat_key,
        "exp_label": cat_label,
        "n_arms": scale["n_arms"],
        "max_nights": scale["max_nights"],
        "seed_graph": scale["seed_graph"],
    }


# ── 实验分类：四级树 实验→模型→run→卡片 的第一级 ──────────────────────────────
#   · 消融 —— cell 名以 abl_ 开头（−S/−R/−M/−E 四个条件 cell）
#   · scaling —— cell 名带 _90d 尾段（时间尺度轴；openkage 与 planner_only 配对腿都算）
#   · 其余 —— 主实验（openkage / planner_only / voyager / echo_adapted，条件在 run 行上区分）
# 优先级 消融 > scaling：现有 cell 两者互斥，真撞上时靠这个固定顺序兜住。
def _exp_category(cell: str, scale: dict) -> tuple[str, str]:
    """cell 名 + 规模信息 → (category_key, category_label)。
    前端按 exp_label 分第一级面板、按 exp_category 排序。"""
    p = _parse_cell(cell)
    if (p.get("condition") or "") in _ABLATION_METHOD_KEYS:
        return "ablation", "消融实验"
    if (p.get("condition") or "") in ("transfer", "non_transfer", "openkage_seed_source"):
        return "transfer", "迁移实验"
    if p.get("scaling") or scale.get("max_nights", 30) != 30 or scale.get("n_arms", 3) != 3:
        return "scaling", "Scaling"
    return "main", "主实验"

# run 规模/种子信息的进程内缓存：同一次 /api/books 扫描里主脑/冻结/各副脑卡会对同一个
# run 调 _run_meta_fields 好几次，而顶层结果 JSON 有几 MB，别每次都重读。TTL 与
# _BOOKS_TTL 对齐——列表本身 60s 刷新一次，这里没必要更勤。
_RUN_SCALE_CACHE: dict[str, tuple[float, dict]] = {}
_RUN_SCALE_TTL = 60.0


def _run_scale_meta(run_dir: Path) -> dict:
    """run 目录 → {'n_arms','max_nights','seed_graph'}。

    优先读顶层结果 JSON（config.arms / config.domain_scale.max_nights / seed_graph）。
    **正在跑的 run 还没落顶层 JSON**：臂名从扁平 checkpoint_<arm>_r*.json glob 推断
    （main_table_metrics._arms_of 同款）、夜数从任一 checkpoint 的 max_nights 字段读——
    90 夜在跑 run 也能当场正确归 scaling，不用等完赛。两样都没有（刚起跑、第一夜未过）
    时按默认 3 臂 30 夜兜底，此时归类暂为主实验，checkpoint 落盘后下一轮 TTL 自然修正。
    """
    key = str(run_dir)
    now = time.time()
    hit = _RUN_SCALE_CACHE.get(key)
    if hit and now - hit[0] < _RUN_SCALE_TTL:
        return hit[1]
    meta = {"n_arms": 3, "max_nights": 30, "seed_graph": False}
    top = run_dir / f"{run_dir.name}.json"
    if top.is_file():
        try:
            d = json.loads(top.read_text(encoding="utf-8"))
        except Exception:
            d = {}
        cfg = d.get("config") or {}
        if cfg.get("arms"):
            meta["n_arms"] = len(cfg["arms"])
        ds = cfg.get("domain_scale") or {}
        if ds.get("max_nights"):
            meta["max_nights"] = int(ds["max_nights"])
        if d.get("seed_graph"):
            meta["seed_graph"] = True
    else:
        ckpts = sorted(run_dir.glob("checkpoint_*_r*.json"))
        arms = {p.name[len("checkpoint_"):].split("_r")[0] for p in ckpts}
        if arms:
            meta["n_arms"] = len(arms)
        if ckpts:
            try:
                d = json.loads(ckpts[0].read_text(encoding="utf-8"))
                if d.get("max_nights"):
                    meta["max_nights"] = int(d["max_nights"])
            except Exception:
                pass
    _RUN_SCALE_CACHE[key] = (now, meta)
    return meta


def _domain_of_db(db_path: Path) -> str:
    """图谱 db 路径 → 域 key：往上找 **cell 目录的直接子目录**（= run 目录）再判域。

    发布包只在 minecraft 域上跑过；路径形状不认识（不在 results/ 下）时退回
    "memory"——即改这版之前的固定值。"""
    roots = [p for _, p in _results_roots()]
    for parent in db_path.parents:
        if parent.parent in roots:
            return _domain_of_run(parent.name)
    return "memory"


def _arm_display(run_slug: str, arm_key: str) -> str:
    """arm key → 展示名：读 agents/<domain>/<key>.md 的 frontmatter name。

    2026-08-03 起 name 是 Agent 的**英文人名**（`alice.md` → `Alice`），不再是中文角色名
    （原「科技攀登者」）—— 因为 minecraft 域拿 name 当游戏内用户名，MC 只接受
    3-16 位 [A-Za-z0-9_]。角色定位改看 md 里的 stage/style 字段。

    key 就是角色 md 的文件名 stem（见 Arm.from_file），但历史 run 的 key 是改名前的旧文件名
    （角色 md 后来加了 a1_/a2_ 排序前缀，如 tech_climber → a1_tech_climber.md），所以精确命中
    失败时再按"去掉 a<数字>_ 前缀相等"兜一次。仍找不到（文件已删、目录已清空）就退回 key 本身。"""
    arms_dir = Path(__file__).resolve().parent.parent / "agents" / "minecraft"
    candidates = [arms_dir / f"{arm_key}.md"]
    if not candidates[0].exists() and arms_dir.exists():
        candidates = [p for p in sorted(arms_dir.glob("*.md"))
                      if re.sub(r"^a\d+_", "", p.stem) == arm_key]
    for md in candidates:
        try:
            fm, _ = parse_frontmatter(md.read_text(encoding="utf-8"))
        except Exception:
            continue
        if fm.get("name"):
            return fm["name"]
    return arm_key


# 概念数缓存：db 路径 → (mtime, size, count, 读取时刻)。见 `_concept_count`。
_CONCEPT_CACHE: dict[str, tuple[float, int, int | None, float]] = {}
_CONCEPT_TTL = 30.0


def _concept_count(db_path: Path) -> int | None:
    """数 concept 条数；文件正被写/刚被删/不是 MemoryGraph schema 就返回 None（调用方跳过这张卡，
    下次刷新自然收敛）。

    ★ 2026-09-27 加缓存：多根扫描后一次要开近 200 个库，而库在网络盘上（每次 open
    都有往返）⇒ 无缓存时 /api/books 实测 **50 秒**，而前端是 10s 轮询，接口会被压垮。
    双判据：①`mtime+size` 没变 → 直接用缓存；②变了但**距上次读取不足 30s** 也用缓存
    ——正在跑的 run 每个窗口都在改写这些 db，只按 mtime 判就等于每次都重开一遍网络盘上的库。
    列表页的概念数差几十秒无所谓（点开卡片走的 graph/concept 接口不缓存，始终实时）。
    """
    try:
        key = str(db_path)
        hit = _CONCEPT_CACHE.get(key)
        now = time.time()
        pair = _scan_stat_pair(db_path)
        if pair is None:
            return None
        st_mtime, st_size = pair
        if hit and (hit[0] == st_mtime and hit[1] == st_size):
            return hit[2]
        if hit and now - hit[3] < _CONCEPT_TTL:
            return hit[2]
        with sqlite3.connect(db_path) as conn:
            (count,) = conn.execute("SELECT COUNT(*) FROM concept").fetchone()
        _CONCEPT_CACHE[key] = (st_mtime, st_size, count, now)
        return count
    except Exception:
        return None


def _resolve_brain_db(run_slug: str, scan: bool = False) -> Path | None:
    """主脑卡的 book_slug = run 目录名（如 minecraft_20260728_213315）→ brain.db 路径。

    slug 可带 cell 名前缀（`<cell>/<run>`），先剥再在该 cell 目录下找。"""
    raw, root = _split_slug_root(run_slug)
    run_dir = root / raw
    for name in _BRAIN_DB_NAMES:
        p = run_dir / name
        # 书架扫描走 TTL 缓存；graph/concept 点开卡片时始终真查
        if (_scan_is_file(p) if scan else p.is_file()):
            return p
    return None


def _resolve_frozen_db(run_slug: str, scan: bool = False) -> Path | None:
    raw, root = _split_slug_root(run_slug)
    p = root / raw / _FROZEN_DB_NAME
    return p if (_scan_is_file(p) if scan else p.is_file()) else None


def _resolve_skill_db(run_slug: str, arm_key: str) -> Path | None:
    """某臂的常驻技能库路径：`run_dir/skills/skill_{arm_key}.db`（存在才返回）。

    ★ **技能归属重构（2026-08-27）**：技能不再存公有脑 brain.db / 副脑 arm_*.db，改存各臂常驻
      SkillStore（见 openkage/memory/skill_store.py + brain.py:367-375 的 `skill_dir = workdir/"skills"`）。
      跨 epoch/window 存活、聚合不再清 —— 所以副脑卡上的技能要**另开这个库**读，arm_db 的 skill 表在新版
      run 里是空的。
    老版 run 没有 `skills/` 目录（技能当时还在 brain.db）→ 返回 None，调用方据此走旧路径。
    """
    raw, root = _split_slug_root(run_slug)
    p = root / raw / "skills" / f"skill_{arm_key}.db"
    return p if p.exists() else None


def list_octopus_brain_books() -> list[dict]:
    """主脑（evolving）卡：每个存在 brain.db 的 run 目录各出一张。按目录名倒序（目录名带
    时间戳，最新的 run 排最前）。"""
    books = []
    for root_key, run_dir in _iter_run_dirs():
        slug = _slug_with_root(root_key, run_dir.name)
        db_path = _resolve_brain_db(slug, scan=True)
        if db_path is None:
            continue
        count = _concept_count(db_path)
        if count is None:
            continue
        books.append({
            "origin": "octopus_brain", "subject": _domain_of_run(run_dir.name),
            "book": _fmt_run_slug(run_dir.name),
            "book_slug": slug,
            "chapters_done": None, "chapters_total": None,
            "concept_count": count, "collision_count": 0,
            **_run_meta_fields(run_dir, root_key),
        })
    return books


def list_octopus_frozen_books() -> list[dict]:
    """对照组（frozen）卡：只有落了 frozen.db 的 run 才出卡——无种子的探索域（minecraft）
    的 frozen 是 NullGraph、根本不落盘，那些 run 就没有这张卡。"""
    books = []
    for root_key, run_dir in _iter_run_dirs():
        slug = _slug_with_root(root_key, run_dir.name)
        db_path = _resolve_frozen_db(slug, scan=True)
        if db_path is None:
            continue
        count = _concept_count(db_path)
        if count is None:
            continue
        books.append({
            "origin": "octopus_frozen", "subject": _domain_of_run(run_dir.name),
            "book": f"初始共享图谱（对照） · {_fmt_run_slug(run_dir.name)}",
            "book_slug": slug,
            "chapters_done": None, "chapters_total": None,
            "concept_count": count, "collision_count": 0,
            **_run_meta_fields(run_dir, root_key),
        })
    return books


FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", flags=re.DOTALL)
H1_RE = re.compile(r"^#\s+(.+)$", flags=re.MULTILINE)

def parse_frontmatter(text: str) -> tuple[dict, str]:
    """跟 backend/ingest_okf.py 的 parse_frontmatter 同款逻辑（无 frontmatter → 空 dict + 原文）。"""
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    meta = yaml.safe_load(m.group(1)) or {}
    return meta, text[m.end():]


def split_h1(body: str) -> list[tuple[str, str]]:
    """跟 backend/ingest_okf.py 的 split_h1 同款逻辑：按 `# H1` 切段。"""
    parts = H1_RE.split(body)
    titles, contents = parts[1::2], parts[2::2]
    return [(t.strip(), c.strip()) for t, c in zip(titles, contents) if t.strip() and c.strip()]


# ── 副脑卡：扫描 run_dir/arm_graphs 动态生成多卡 ────────────────────────────────
# 副脑图谱跟主脑/对照组一样是 MemoryGraph 同构 schema（ArmGraph 继承同一套，物理上是独立
# sqlite），build_memory_graph()/memory_concept_detail() 直接复用，接收对应 db 路径（见
# _resolve_arm_db）。主脚本每批落 run_dir/arm_graphs/arm_<key>_e<epoch>b<batch>.db，batch
# 聚合进主脑后就 unlink（openkage/brain.py 的 _consolidate 之后）——所以这里每次请求都实时扫描
# （Path.glob 不缓存、天然跳过已删文件），有残留文件才出卡，正常跑完的 run 就是空列表。


def _iter_run_dirs() -> list[tuple[str, Path]]:
    """遍历**所有 cell**下的 run 目录，产出 `[(cell 名, run_dir)]`，按 cell 名 + run 名排序。
    只认 runNN 命名（launch order，见 results/manifest.tsv）。

    扫描根是 results/<cell>/（见 `_results_roots`）：每个 cell 一个根，
    root_key 就是 cell 名。
    """
    out: list[tuple[str, Path]] = []
    for root_key, root in _results_roots():
        if not _scan_is_dir(root):
            continue
        for p in _scan_iterdir(root):
            if _scan_is_dir(p) and _RUN_DIR_RE.match(p.name):
                out.append((root_key, p))
    def _run_num(name: str):
        m = re.search(r"\d+$", name)
        return int(m.group()) if m else float("inf")
    out.sort(key=lambda t: (t[0], _run_num(t[1].name), t[1].name))
    return out


def _iter_arm_dirs():
    """遍历所有 run 的副脑图谱目录，产出 `(root_key, run_dir, arm_dir)`。"""
    for root_key, run_dir in _iter_run_dirs():
        for sub in _ARM_SUBDIRS:
            d = run_dir / sub
            if _scan_exists(d):
                yield root_key, run_dir, d


def _resolve_arm_db(book_slug: str) -> Path | None:
    """副脑卡的 book_slug 形如 "<run 目录名>__arm_<key>_e<epoch>b<batch>"（带 run 前缀是因为
    同一个副脑 key 在不同 run 里会重名，只用文件名当 slug 会串卡）→ 实际 db 路径。
    找不到（已被主脚本 unlink / run 目录被删）返回 None。"""
    raw_slug, root = _split_slug_root(book_slug)
    run_slug, _, db_stem = raw_slug.partition("__")
    if not db_stem:
        return None
    for sub in _ARM_SUBDIRS:
        p = root / run_slug / sub / f"{db_stem}.db"
        if p.exists():
            return p
    return None


_ARM_SNAPSHOTS_CACHE: dict[str, tuple[float, list[tuple[int, Path]]]] = {}
_ARM_SNAPSHOTS_TTL = 60.0   # 与 _MEMORY_GRAPH_TTL 对齐，快照只在窗口边界新增


def _arm_snapshots(arm_db: Path) -> list[tuple[int, Path]]:
    """窗口快照列表按 60s TTL 缓存：原来每次 /graph 都要 glob 15 个 checkpoint 目录
    并逐个 is_file()（网络盘上约 0.8s）。快照只在窗口边界新增，晚一分钟看到不影响看图。
    """
    key = str(arm_db)
    hit = _ARM_SNAPSHOTS_CACHE.get(key)
    if hit and time.time() - hit[0] < _ARM_SNAPSHOTS_TTL:
        return list(hit[1])
    result = _arm_snapshots_uncached(arm_db)
    _ARM_SNAPSHOTS_CACHE[key] = (time.time(), result)
    return list(result)


def _arm_snapshots_uncached(arm_db: Path) -> list[tuple[int, Path]]:
    """该副脑图谱对应的**窗口快照**列表 `[(窗口号, 路径), ...]`，按窗口号升序。

    ## 这些快照里装着什么

    题内聚合每到一个窗口边界就 `snapshot_to()` 一份、**紧接着硬删经验层**
    （`openkage/orchestrate/window.py:130-131` → `graph.clear_layer`，那是真的 `DELETE FROM`）。
    所以「被聚合清掉的经验」在活图谱里查不到，却**完整留在这些快照里** —— 实测
    `minecraft_20260810_140001` 的 alice：6 个窗口共 95 个经验概念、95 条 facet、
    67 条「记忆层↔经验层」的跨层边，而活图谱的经验层是 0。

    ## 两种快照布局

    快照落在哪由编排层是否传了 `checkpoint_dir` 决定（见 `brain.py` 构造 WindowAggregator
    那处），两套都得认 —— 只认一套会让另一种布局的真实 run 经验层在前端整片不可见：

    ① **legacy 布局**（无 checkpoint_dir）：`arm_graphs/arm_<key>_e<E>b<B>_w<W>.db`，
       窗口号在文件名里。早期 run 用。
    ② **checkpoint 布局**（传了 checkpoint_dir=self.workdir，现行生产路径）：写到
       `checkpoint{window}/arm_<key>/snap.db`（见 `window.py:_run_window_sync` 的 snap 路径）。
       文件名固定叫 `snap.db`、不带窗口号 —— **窗口号取自它所在的 checkpoint 目录号**。

    ## checkpoint 布局的窗口号为什么能直接用目录号

    `window.py` 用 `f"checkpoint{window}"` 当目录名（window 是题内聚合窗口序号，1 起），
    目录号与窗口号同源、不是两份约定。经验概念的 `created_at` 是 day_idx（reflect 的 `at`，
    见 `graph.py:reflect`），而 `clear_layer` 每窗口清一次经验层，故 `checkpoint{N}/snap.db`
    的经验层只含窗口 N 内新建的（created_at 落在窗口 N 的天区间）。实测一条 30 夜 /
    aggregate_every=3 的 run：5 arm × 10 窗口共 264 行经验，`ceil(created_at/3) == N` 零违例。
    viewer 侧因此**不需要知道 aggregate_every** —— 目录号就是窗口号。

    ## 为什么不查库定窗口

    `created_at` 是 day_idx（副脑刻度），不是 roundcode 编码的 round —— 从它反解窗口要先知道
    aggregate_every，那是框架侧的运行参数、viewer 拿不到也不该再存一份。目录号同源、无漂移风险。
    """
    m = _ARM_FILE_RE.match(arm_db.name)
    if not m:
        return []
    arm_key, epoch, batch = m.group("key"), m.group("epoch"), m.group("batch")
    run_dir = arm_db.parent.parent  # arm_graphs/ 的上一层 = run 目录

    by_window: dict[int, Path] = {}

    # ① legacy 布局：arm_<key>_e<E>b<B>_w<W>.db（窗口号在文件名里）
    prefix = f"arm_{arm_key}_e{epoch}b{batch}_w"
    for p in arm_db.parent.glob(f"{prefix}*.db"):
        sm = _ARM_SNAP_RE.match(p.name)
        # key 用非贪婪匹配，`arm_a_e1b1_w1.db` 这类名字理论上能被别的 key 前缀撞上，
        # 所以除了 glob 还要用正则复核 key/epoch/batch 三者全等 —— 否则会把别的 arm 的快照并进来。
        if not sm or (sm.group("key"), sm.group("epoch"), sm.group("batch")) != (
                arm_key, epoch, batch):
            continue
        by_window[int(sm.group("window"))] = p

    # ② checkpoint 布局：checkpoint{window}/arm_<key>/snap.db（窗口号 = checkpoint 目录号）
    #    run_dir 不存在或非 run 目录时 glob 自然返回空，无需特判。
    for ckpt_dir in run_dir.glob("checkpoint*"):
        cm = _CHECKPOINT_DIR_RE.match(ckpt_dir.name)
        if not cm:
            continue
        snap = ckpt_dir / f"arm_{arm_key}" / "snap.db"
        if snap.is_file():
            # 同一窗口理论上不会两路都有（一个 run 只用一种布局）；去重以窗口号为键，
            # legacy 先到先得 —— 保留这个稳定顺序比纠结"哪个对"更有意义（两者内容等价）。
            by_window.setdefault(int(cm.group("window")), snap)

    return sorted(by_window.items())


def list_octopus_arm_books() -> list[dict]:
    """扫所有 run 的 arm_graphs 目录，每个 arm_*.db 出一张卡。卡片命名「副脑角色名 · 第N轮第M批」，
    book_slug="<run 目录名>__<文件名去 .db>"（跨 run 唯一）。按 book_slug 倒序，最新 run 排最前。

    planner-only（nograph，论文条件 B）run 没有 arm_*.db（无私有图），但也有轨迹卡——
    planner-only 卡也有探索地图可看，那条路线的轨迹来自 wsr.load_trajectories_nograph
    的稀疏重建（见其 docstring）。卡带 kind="planner_only"：概念数无意义（无图谱），
    前端按它把「概念」计数位换成轨迹步数标识。"""
    books = []
    for root_key, run_dir, d in _iter_arm_dirs():
        has_ctx = (run_dir / "contexts").is_dir()
        for db_path in _scan_iterdir(d):
            if not (db_path.name.startswith("arm_") and db_path.name.endswith(".db")):
                continue
            if db_path.name.endswith("_vec.db"):
                continue  # 向量库副本不是图谱本体，不出卡
            m = _ARM_FILE_RE.match(db_path.name)
            if not m:
                continue
            count = _concept_count(db_path)
            if count is None:
                continue
            display = _arm_display(run_dir.name, m.group("key"))
            # 有没有录像可看（前端据此决定要不要亮「录像」开关）。跟 has_scene 一样只探
            # 目录在不在、不解析内容：steps.jsonl 几千行，/api/books 是 10s 轮询的接口，
            # 每轮为几十张卡各解析一遍纯属浪费。真点开时 /video 接口自会给出空态。
            fdir, _ = _footage_dir_of(
                _slug_with_root(root_key, f"{run_dir.name}__{db_path.stem}"), scan=True)
            # 发布包的录像走轻量口径（jpg 关键帧 + jsonl 索引，不带 mp4）：按天分段视频要
            # mp4 真在场才亮「录像」开关，免得点开全是空段。
            has_video = any(_scan_is_file(fdir / f"day{d:02d}.mp4") for d in range(1, 91))
            books.append({
                "origin": "octopus_arm",
                "subject": _domain_of_run(run_dir.name),
                # 书名第二行是所属 run 的时间戳：同一个 agent（Alice）在每次 run 里都会
                # 出一张同名卡，不带时间戳时多次 run 的卡片肉眼分不出来。前端 .book-name 用
                # white-space: pre-line 渲染这个换行。
                "book": f"{display} · 私有图谱 第{m.group('epoch')}轮第{m.group('batch')}批\n{_fmt_run_ts(run_dir.name)}",
                "book_slug": _slug_with_root(root_key, f"{run_dir.name}__{db_path.stem}"),
                "chapters_done": None, "chapters_total": None,
                "concept_count": count, "collision_count": 0,
                # 分组用：臂名（前端 run 层下按臂展示）+ 实验/模型/条件/主机/来源根（见 _run_meta_fields）
                "arm": display,
                **_run_meta_fields(run_dir, root_key),
                # 有没有探索地图可看（前端据此决定要不要显示「地图」开关）。只看 contexts/ 目录
                # 在不在 —— **不在这里真去解析轨迹**：那要读几 MB JSON，而 /api/books 是 10s
                # 轮询的接口，每次为几十张卡解析一遍轨迹纯属浪费。目录在但解析不出轨迹的极端
                # 情况（空目录/格式变了），前端点开会拿到 days=[] 的空地图并显示空态。
                # 上下文留痕是 2026-08-10 起默认开的，更早的 run 没有这份数据 → 不显示开关。
                "has_scene": has_ctx,
                # 有没有录像（同款理由只探目录：steps.jsonl 在 = 这条轨迹开过 --mc-footage）
                "has_video": has_video,
            })
    books.extend(_list_planner_only_books())
    def _run_num(name: str):
        m = re.search(r"\d+$", name)
        return int(m.group()) if m else float("inf")
    books.sort(key=lambda b: (_run_num(b.get("run_slug") or ""), b["book_slug"]))
    return books


#: 出「轨迹卡」的无图谱 cell：planner-only（baseline B）与两个外部基线（voyager/echo
#: 无共享图无私有图，contexts 文件名与 planner-only 不同构——轨迹解析给空态，卡照出）。
_TRAJECTORY_CELLS_SUFFIXES = ("_planner_only", "_voyager", "_echo_adapted")


def _list_planner_only_books() -> list[dict]:
    """无图谱 cell（planner-only / voyager / echo）的轨迹卡：一个 run 按臂出卡。

    has_scene 只探 contexts/ 目录在不在（解析不出轨迹的极端情况由 scene 接口给空态），
    concept_count 给 None——无图谱，前端按 kind 字段把计数位换成「轨迹」标识。"""
    books = []
    for root_key, run_dir in _iter_run_dirs():
        if not any((root_key or "").endswith(s) for s in _TRAJECTORY_CELLS_SUFFIXES):
            continue
        has_ctx = _scan_is_dir(run_dir / "contexts")
        cond = _parse_cell(root_key or "").get("condition_label") or "无图谱"
        for arm in _planner_only_arms(run_dir):
            display = _arm_display(run_dir.name, arm)
            books.append({
                "origin": "octopus_arm",
                "subject": _domain_of_run(run_dir.name),
                "book": f"{display} · {cond} 轨迹\n{run_dir.name}",
                # slug 与副脑卡同构（`<run>__arm_<key>_e1b1`），scene/graph 接口靠尾部
                # _e<数字>b<数字> 定界解析（_parse_arm_book_slug）——nograph 卡复用同一套解析。
                "book_slug": _slug_with_root(root_key, f"{run_dir.name}__arm_{arm}_e1b1"),
                "chapters_done": None, "chapters_total": None,
                "concept_count": None, "collision_count": 0,
                "arm": display,
                "kind": "planner_only",
                **_run_meta_fields(run_dir, root_key),
                "has_scene": has_ctx,
                "has_video": False,     # 外部基线/planner 组按口径不开录像
            })
    return books


def _planner_only_arms(run_dir: Path) -> list[str]:
    """run 的 arm key 列表（小写）：从扁平 checkpoint_<arm>_r*.json 收，不写死三臂
    （与 main_table_metrics._arms_of 同款 glob）。"""
    arms: set[str] = set()
    for p in _scan_iterdir(run_dir):
        name = p.name
        if not (name.startswith("checkpoint_") and name.endswith(".json")):
            continue
        arms.add(name[len("checkpoint_"):].split("_r")[0])
    return sorted(arms)


# ── 探索地图（web 版）────────────────────────────────────────────────────────
# 数据全部来自 world_scene_render 模块，这里只做三件事：
#   ① 从 book_slug 反解出 run 目录 + arm key（副脑卡的 slug 已经带了这两样，不用新参数）
#   ② 调 wsr 的三个 loader，取出**这一个 arm** 的那份
#   ③ 把「取景框」算好一起返回 —— 见下面 _scene_frame 的注释，这个必须后端算


def _parse_arm_book_slug(book_slug: str) -> tuple[str, str] | None:
    """副脑卡 book_slug → (run 目录名, arm key)。形状不认识返回 None。

    arm key 是角色 md 的文件名 stem、可能带下划线（a1_tech_climber），所以靠 `_ARM_FILE_RE`
    的尾部 `_e<数字>b<数字>` 定界，不能按下划线切。
    """
    # 先剥 cell 名前缀（`<cell>/<run>__arm_...`，见 _slug_with_root）——
    # 此前直接 partition("__")，根前缀会被并进 run 目录名，下游按 run 目录定位的路径全部解析错。
    raw_slug, _root = _split_slug_root(book_slug)
    run_slug, _, db_stem = raw_slug.partition("__")
    if not db_stem:
        return None
    m = _ARM_FILE_RE.match(f"{db_stem}.db")
    if not m:
        return None
    return run_slug, m.group("key")


def _is_planner_only_cell(book_slug: str) -> bool:
    """book_slug（可带 cell 前缀）→ 它的 run 是不是 planner-only cell。
    发布布局里 planner-only 的判据就是 cell 名以 _planner_only 结尾。"""
    head = book_slug.partition("/")[0]
    return head.endswith("_planner_only")


def _scene_frame(points: list[tuple], places: list[dict]) -> dict:
    """算地图取景框：**只按主聚居区取景**，其它聚居区逐个报出来。

    为什么不能让前端按全量点取 min/max：死后重生回的是世界原生出生点（x,z≈0）而不是那个随机
    出生点，所以死过的副脑会永久搬家 —— 实测 alice 第 1 天在 x≈712、第 2 天起 7 天全在 x≈0，
    两簇相距 716 格。按全量取景会把真正天天活动的那片压成一个点（world_scene_render 模块 docstring
    的「主聚居区」那条，画错过一版）。

    `others` 是**逐簇**的（`_other_clusters`），不是把非主簇的点取一个总平均 —— 那个平均值
    常常落在一个谁都没去过的地方（实测 alice 平均出 (110,6)，那坐标 60 格内只有 3 步）。

    返回的 span 已含 1.12 的留白系数（与合并前的出图实现同一个算法），前端直接拿去定 scale。
    """
    main, _rest = wsr._main_cluster(points)
    xs = [p[1] for p in main]
    zs = [p[3] for p in main]
    if not xs:  # 没有任何轨迹点（新 run 第一天还没跑完），退化成用地点定框
        xs = [a["x"] for a in places] or [0]
        zs = [a["z"] for a in places] or [0]
    lo_x, hi_x, lo_z, hi_z = min(xs), max(xs), min(zs), max(zs)
    # 地点只有落在主簇附近的才参与定框，否则老簇的地点又把框拉回去（同上，与合并前的出图实现一致）
    gap = wsr._CLUSTER_GAP
    for a in places:
        if lo_x - gap <= a["x"] <= hi_x + gap and lo_z - gap <= a["z"] <= hi_z + gap:
            xs.append(a["x"])
            zs.append(a["z"])
    span = max(max(xs) - min(xs), max(zs) - min(zs), 8.0) * 1.12
    cx, cz = (max(xs) + min(xs)) / 2, (max(zs) + min(zs)) / 2
    # 前端画的是正方形画布，取景半边长就是 span/2 —— 用同一个判据算"谁在框外"，
    # 图注才不会说"另有 41 步在 15 格外"而那 41 步就画在图中央（合并前的出图实现踩过，见 world_scene_render 模块 docstring）。
    half = span / 2

    def visible(p: tuple) -> bool:
        return cx - half <= p[1] <= cx + half and cz - half <= p[3] <= cz + half

    return {
        "cx": cx, "cz": cz, "span": span,
        "lo_x": min(xs), "hi_x": max(xs), "lo_z": min(zs), "hi_z": max(zs),
        "others": wsr._other_clusters(points, visible=visible),
    }


# build_scene 的进程内缓存：{book_slug: (scene_json, 过期时刻)}。stale-while-revalidate
# 语义（同 /api/books 那套）：手里有就先返回旧的，过期重建在锁内只跑一份。
# ★ 2026-11-11 改：失效改看 run 目录指纹（见 _run_data_fingerprint），不再按 TTL 硬过期；
#   _SCENE_TTL 只作兜底。同一 run 的 alice/bob/carol 共用一份解析结果，别各扫一遍 contexts。
_SCENE_CACHE: dict[str, tuple[dict, float]] = {}
_SCENE_TTL = 300.0
_SCENE_REFRESHING: dict[str, threading.Lock] = {}

# run 级解析结果缓存：{(root, run_slug, is_planner_only): (指纹, payload, 构建时刻)}。
# payload = {"traj", "memory", "edges"}，三个 arm 从里面各取自己那份。
_RUN_DATA_CACHE: dict[tuple, tuple[str, dict, float]] = {}
_RUN_DATA_REFRESHING: dict[tuple, threading.Lock] = {}


def _run_data_fingerprint(run_dir: Path, is_planner_only: bool = False) -> str:
    """run 侧输入是否变化的**廉价**指纹：只做十几次 stat，不读文件内容。

    网络盘上逐文件 stat 570 份要 18s（实测），所以只取三个目录的
    (mtime_ns, 条目数) + 少量会被追加写的文件内容指纹。contexts/ 的文件写完才可见、
    之后不改，新增/删除会改目录 mtime；db 与 jsonl 会被持续追加，单独 stat。
    同名文件被改写不覆盖 —— 那是 _SCENE_TTL(300s) 兜底的情况，不是正常路径。
    """
    parts: list[tuple] = []
    for d in (run_dir, run_dir / "contexts", run_dir / "arm_graphs"):
        try:
            st = d.stat()
            parts.append((d.name, st.st_mtime_ns, len(_scan_iterdir(d))))
        except OSError:
            parts.append((d.name, None, None))

    def _pair(p: Path) -> tuple:
        try:
            st = p.stat()
            return (p.name, st.st_mtime_ns, st.st_size)
        except OSError:
            return (p.name, None, None)

    for p in _scan_iterdir(run_dir / "arm_graphs"):
        if wsr._ARM_DB_RE.match(p.name):
            parts.append(("db", _pair(p)))
    for p in _scan_iterdir(run_dir):
        if wsr._CKPT_FILE_RE.match(p.name) or (is_planner_only and p.suffix == ".jsonl"):
            parts.append(("log", _pair(p)))
    return hashlib.sha256(repr(parts).encode()).hexdigest()[:20]


def _run_payload(scene_root: Path, run_slug: str, run_dir: Path,
                 is_planner_only: bool) -> tuple[str, dict]:
    """同一个 run 的所有 arm 共享一份解析结果；指纹没变就直接复用，变了才重建一次。

    锁保证三个 arm 同时首次打开时只有一份在真扫，其余等锁后命中缓存。
    """
    key = (str(scene_root), run_slug, is_planner_only)
    fp = _run_data_fingerprint(run_dir, is_planner_only)
    hit = _RUN_DATA_CACHE.get(key)
    if hit and hit[0] == fp:
        return fp, hit[1]
    lock = _RUN_DATA_REFRESHING.setdefault(key, threading.Lock())
    with lock:
        hit = _RUN_DATA_CACHE.get(key)
        if hit and hit[0] == fp:
            return fp, hit[1]
        try:
            traj = (wsr.load_trajectories_nograph(run_dir) if is_planner_only
                    else wsr.load_trajectories(run_dir, None))
        except FileNotFoundError:
            traj = {}   # 老 run 没有 contexts/：返回空地图而不是 404（保持原语义）
        payload = {
            "traj": traj,
            "memory": wsr.load_memory_layer(run_dir),
            "edges": wsr.load_memory_edges(run_dir),
        }
        _RUN_DATA_CACHE[key] = (fp, payload, time.time())
        return fp, payload


def build_scene(book_slug: str) -> dict:
    """一张副脑卡 → 它那一个 arm 的探索地图数据（轨迹 / 记忆层地点 / 记忆层边 / 取景框）。

    `places` 里带 `slug`，与图谱接口的节点 slug 是**同一个键** —— 前端靠它做地图↔图谱联动。
    没解析出坐标的记忆层概念放 `no_coord`（前端列成灰条标"无坐标"），不静默丢：看图的人
    否则会以为地图漏画了。经验层概念本就没有地理属性，压根不在这个响应里。

    ★ 2026-11-11 改：缓存失效改看 **run 目录指纹**，不再按 30s TTL 硬过期。
      前端 10s 轮询 scene，而一次重建要读 570 份 contexts（并发预读后仍 ~11s）；
      已跑完的 run 数据不动、指纹常年不变 ⇒ 直接续期返回，不重建。指纹变了才
      stale-while-revalidate（先回旧值，后台重建一次，所有 arm 共享结果）。
      _SCENE_TTL(300s) 只作兜底：目录 mtime 不覆盖「同名文件被改写」这种非常规情况。
      首次（无缓存）仍同步建 —— 那次没有旧值可回。
    """
    now = time.time()
    hit = _SCENE_CACHE.get(book_slug)
    # 地形是后台线程渲染后异步补的：还等着就不能短路，否则 scene 永远停在
    # terrain=null 那版（前端画降级色块、「世界」按钮不出现）。600s 后不再重试，
    # 避免世界数据损坏时每轮轮询都重建。旧格式缓存（无 run_fp）也强制重建一次。
    waiting = bool(hit and hit[0].get("terrain_pending")
                   and now - (hit[0].get("terrain_pending_since") or now) < 600)
    if hit and "run_fp" not in hit[0]:
        waiting = True
    if hit and now < hit[1] and not waiting:
        return hit[0]
    if hit:
        # ★ 数据没变就别重建：已跑完的 run 指纹常年不变，直接续期回旧值。
        try:
            parsed = _parse_arm_book_slug(book_slug)
            if parsed:
                run_slug, _arm = parsed
                _raw, scene_root = _split_slug_root(book_slug)
                is_planner_only = _is_planner_only_cell(book_slug)
                run_fp = _run_data_fingerprint(scene_root / run_slug, is_planner_only)
                if hit[0].get("run_fp") == run_fp and not waiting:
                    _SCENE_CACHE[book_slug] = (hit[0], now + _SCENE_TTL)
                    return hit[0]
        except Exception:
            pass   # 指纹算不出来就退回原来的后台重建路径
        # 有旧值：stale 先回，重建放后台（锁保证同一 slug 同时只有一个重建在跑）
        lock = _SCENE_REFRESHING.setdefault(book_slug, threading.Lock())
        if not lock.locked():
            threading.Thread(target=_refresh_scene, args=(book_slug,), daemon=True,
                             name=f"scene-{book_slug[-16:]}").start()
        return hit[0]
    return _refresh_scene(book_slug, blocking=True)


def _refresh_scene(book_slug: str, blocking: bool = False) -> dict:
    """重建一个 slug 的 scene（build_scene 的工人函数；blocking=True 用于首建）。"""
    lock = _SCENE_REFRESHING.setdefault(book_slug, threading.Lock())
    if blocking:
        lock.acquire()
    elif not lock.acquire(blocking=False):
        return _SCENE_CACHE[book_slug][0]  # 已有人在重建，回现有值
    try:
        parsed = _parse_arm_book_slug(book_slug)
        if parsed is None:
            raise HTTPException(400, f"不是合法的私有图谱卡 book_slug: {book_slug}")
        run_slug, arm = parsed
        # 2026-09-27：slug 可能带根前缀（2026-10-04 起是机器名），run 目录要在该卡**所属的根**下面找。
        _raw, scene_root = _split_slug_root(book_slug)
        run_dir = scene_root / run_slug
        if not run_dir.is_dir():
            raise HTTPException(404, f"run 目录不存在: {run_slug}")

            # ★ planner-only（nograph）走**稀疏重建**那条路（2026-10-08）：nograph 无夜反思 ⇒ 无
        #   reflect 留痕，轨迹从天末面板+onLog+place_log 三源重建（见
        #   wsr.load_trajectories_nograph 的 docstring）。判据与出卡同源：条件段 nograph。
        #   voyager/echo 基线的 contexts 文件名不同构，两路 loader 都解析不出点 → 空态（预期）。
        is_planner_only = _is_planner_only_cell(book_slug)
        # ★ 同一 run 的所有 arm 共用一份解析结果：指纹没变就直接复用，变了才重建一次。
        run_fp, payload = _run_payload(scene_root, run_slug, run_dir, is_planner_only)
        traj = payload["traj"]
        memory = payload["memory"].get(arm, [])
        edges = payload["edges"].get(arm, [])

        # ★ Voyager Fig.7 式真实地形底图：checkpoint 里每个 arm 都保存了完整 Minecraft 世界
        # （world/region/*.mca）。渲染失败只影响底图，轨迹/地点仍可用。
        # 冷渲染一次 ~10s（批量 IO 优化后；改前 50s+）—— scene 接口被前端 10s 轮询打着，
        # 不能同步等它：未命中缓存时丢后台线程渲染、本次先返回 terrain=null，下一轮轮询
        # 自然补上；PNG 端点（api_scene_terrain）拿同一把指纹锁等渲染完成，保证 url
        # 一出现在 scene JSON 里、PNG 必然已经就绪。
        terrain_info = None
        terrain_pending = False   # 冷渲染已丢后台、本轮还没拿到底图
        try:
            latest_world = wsr._latest_world(run_dir, arm)
            if latest_world is not None:
                fp = wsr._region_fingerprint(latest_world)
                cached = (wsr._CACHE_DIR / f"terrain-{fp}.json").is_file()
                if cached:
                    meta = wsr.render_world_topdown(latest_world.parent)
                else:
                    threading.Thread(
                        target=wsr.render_world_topdown,
                        args=(latest_world.parent,), daemon=True,
                        name=f"terrain-{fp[:8]}").start()
                    meta = None
                    terrain_pending = True
                if meta is not None:
                    terrain_subject = _domain_of_run(run_slug)
                    terrain_info = {
                        **{k: meta[k] for k in ("min_x", "min_z", "max_x", "max_z", "chunks", "fingerprint")},
                        "width": meta["max_x"] - meta["min_x"],
                        "height": meta["max_z"] - meta["min_z"],
                        "url": f"/api/books/octopus_arm/{quote(terrain_subject)}/{quote(book_slug, safe='')}"
                               f"/scene/terrain?v={meta['fingerprint']}",
                    }
        except Exception as exc:
            print(f"[viewer] checkpoint 世界地形渲染失败，跳过: {exc}")

        days_raw = traj.get(arm, {})
        days = [
            {
                "day": day,
                # 轨迹按**死亡断点**切成多段（唯一判据，2026-09-30 定；此前按 30 格位移切，
                # 会把 pathfinder 一步 30~140 格的正常行走误判成瞬移）。断点来自 checkpoint
                # 结构化死亡明细 (day, step)，见 wsr._segments/_merge_checkpoint_deaths。
                "segments": [
                    [{"step": s, "x": x, "y": y, "z": z, "biome": b} for (s, x, y, z, b) in seg]
                    for seg in wsr._segments(data["points"], data.get("death_breaks"))
                ],
                # 死亡点带服务器权威的 cause/killer（checkpoint 明细，见
                # wsr._merge_checkpoint_deaths）—— 前端 hover title 显示「被什么杀的」，
                # 老格式 run 无 checkpoint 时后两位为 null。
                "deaths": [{"x": x, "y": y, "z": z, "cause": cause, "killer": killer}
                           for (x, y, z, cause, killer) in data["deaths"]],
            }
            for day, data in sorted(days_raw.items())
        ]
        # ★ 生命段（前端轨迹线的主数据源）：跨天首尾拼接、只在死亡断点切开 ——
        # days[].segments 按天嵌套（天界+死亡双重切分）会把轨迹碎成 N天+N死 段，
        # 而生命段 = 唯一断点数+1（与合并前的出图实现同一逻辑，Carol 19 天
        # 6 死 → 7 条完整生命线）。同一步内双亡只产生一个断点位置，段数=唯一断点+1。
        lives_raw = wsr._lives(days_raw)
        lives = [
            [{"step": s, "x": x, "y": y, "z": z, "biome": b} for (s, x, y, z, b) in seg]
            for seg in lives_raw
        ]
        # ★ 探索覆盖范围（Voyager Fig.7 式最小包围圆）：唯一实现是 wsr.min_enclosing_circle，
        #   点集就是上面 lives 的全量点（跨天拼接、死亡处断开的那一份）——轨迹与范围因此必然同源。
        #   ⚠️ 用**全量**轨迹点，不用主聚居区：覆盖范围的语义就是「它到过的全部地方」，含死亡重生
        #   迁址后那个几百格外的簇；只框主聚居区会把圆算小（实测 carol：主聚居区跨度 142 格 vs
        #   全量覆盖半径 289 格，按主簇画出来的圈罩不住一半轨迹）。
        coverage = wsr.min_enclosing_circle(
            [(p[1], p[3]) for life in lives_raw for p in life])
        places = [c for c in memory if c["x"] is not None and c["is_place"]]
        all_points = [p for d in days_raw.values() for p in d["points"]]
        place_slugs = {p["slug"] for p in places}
        # 只留两端都是**图上地点**的边。被滤掉的大多是连到「第N天」/「第N天结束状态」这类当天
        # 总结概念的边 —— 它们的 summary 里也常带坐标，但那只是正文里**碰巧第一个**出现的数字
        # （实测「第2天」被解析成 (136,-15)，而它整天都在 x≈6 活动），不是一个地方。
        # 把 `第2天工作台 建立于 第2天` 画成一条地理连线，等于凭空画一条指向不存在地点的线。
        # 数目仍报出来（`hidden_edges`），不静默丢。
        drawable = [e for e in edges if e["src"] in place_slugs and e["dst"] in place_slugs]
        # rev 必须带上地形状态：底图异步补上后 rev 要变，否则前端按「数据没变」
        # 跳过重绘，会一直显示 terrain=null 那版的降级色块。
        terrain_tag = (terrain_info["fingerprint"] if terrain_info else
                       "pending" if terrain_pending else "none")
        prev = _SCENE_CACHE.get(book_slug)
        pending_since = (prev[0].get("terrain_pending_since") if prev else None) or time.time()
        scene = {
            "arm": arm,
            "arm_display": _arm_display(run_slug, arm),
            "run": run_slug,
            # 数据版本：前端据此跳过没变化的轮询重绘；run_fp 供 build_scene 判数据是否变化。
            "rev": f"{run_fp}:{terrain_tag}",
            "run_fp": run_fp,
            "terrain_pending": terrain_pending,
            "terrain_pending_since": pending_since if terrain_pending else None,
            "days": days,
            # 生命段：前端轨迹线主数据源（跨天拼接 + 死亡唯一断点，见上方 lives 注释）
            "lives": lives,
            # 探索覆盖范围（最小包围圆，wsr.min_enclosing_circle 算的，与轨迹同源）。
            # 前端**直接照画**、不再自己算一份（理由见 import 处的注释）。无轨迹时为 None。
            "coverage": ({"cx": coverage[0], "cz": coverage[1], "r": coverage[2]}
                         if coverage else None),
            "places": places,
            "edges": drawable,
            "hidden_edges": len(edges) - len(drawable),
            "no_coord": [{"slug": c["slug"], "name": c["name"]}
                         for c in memory if c["x"] is None],
            # 有坐标但不是地点（当天总结类）—— 不画在地图上。仍要**逐条**给出来（不只给个数）：
            # 前端在"点了图谱节点但地图上没高亮"时要据此说清是哪一种原因（总结概念 / 无坐标 /
            # 在框外 / 根本是经验层），一句"不在地图上"糊过去等于让人以为联动坏了。
            "non_places": [{"slug": c["slug"], "name": c["name"]}
                           for c in memory if c["x"] is not None and not c["is_place"]],
            "terrain": terrain_info,
            "frame": _scene_frame(all_points, places),
            # planner-only 卡的辨识标记：前端把「步」字样换成「采样点」（稀疏重建的点不是步，
            # 密度约为 reflect 路线的 1/3，不标清楚会误读成「它只走了这么多步」）。
            "kind": "planner_only" if is_planner_only else None,
            "stats": {
                "days": len(days),
                "steps": len(all_points),
                "deaths": sum(len(d["deaths"]) for d in days_raw.values()),
                "places": len(places),
                "memory_concepts": len(memory),
            },
        }
        _SCENE_CACHE[book_slug] = (scene, time.time() + _SCENE_TTL)
        return scene
    finally:
        lock.release()


def _resolve_octopus_db(origin: str, book_slug: str) -> Path | None:
    """三种卡统一按 origin 分派到各自的 book_slug→db 路径解析。未知 origin 返回 None。"""
    if origin == "octopus_brain":
        return _resolve_brain_db(book_slug)
    if origin == "octopus_frozen":
        return _resolve_frozen_db(book_slug)
    if origin == "octopus_arm":
        return _resolve_arm_db(book_slug)
    return None


def _concept_row_to_node(row: tuple, facet_count: int, origin: str = "octopus_arm",
                         subject: str = "memory") -> dict:
    # 第 4 列（concept.layer）是**层标记** `memory`/`experience`
    # （2026-08-06，见 openkage/memory/schema.py 的 LAYER_*）。前端那个字段叫 chapter（"章节"），
    # 是 markdown 教材图谱那套的形状，这里沿用它承载层标记 —— 显示"memory"/"experience"
    # 比原先显示模型随手填的 `game_mechanics` 更有意义。
    # subject 是卡片/节点上的分组标签，取 run 所属域（minecraft/math），由调用方从 db 路径反查。
    slug, name, summary, layer = row
    return {
        "id": slug, "slug": slug, "title": name, "subject": subject,
        "summary": summary, "difficulty": 3, "facet_count": facet_count, "chapter": layer,
        "origin": origin, "in_degree": 0, "out_degree": 0,
    }


def _read_arm_snapshot(window: int, snap_path: Path) -> dict | None:
    """读一个窗口快照库里「并入副脑卡需要的三样数据」。独立文件、无共享状态 ⇒ 可并行。"""
    try:
        with sqlite3.connect(f"file:{snap_path}?mode=ro", uri=True) as conn:
            rows = conn.execute(
                "SELECT slug, name, summary, layer FROM concept WHERE layer = ?",
                (LAYER_EXPERIENCE,)).fetchall()
            exp_slugs = {r[0] for r in rows}
            facet_counts = dict(conn.execute(
                "SELECT concept_slug, COUNT(*) FROM facet GROUP BY concept_slug").fetchall())
            edge_rows = conn.execute(
                "SELECT id, from_slug, to_slug, relation FROM edge").fetchall()
            try:
                skill_snap_rows = conn.execute(
                    "SELECT slug, name, intent, signature, verified, "
                    "success_count, fail_count FROM skill"
                ).fetchall()
            except sqlite3.OperationalError:
                skill_snap_rows = []
        return {"rows": rows, "exp_slugs": exp_slugs, "facet_counts": facet_counts,
                "edge_rows": edge_rows, "skill_snap_rows": skill_snap_rows}
    except sqlite3.Error as exc:
        # 快照读不出来不该让整张图 500 —— 少一个窗口的经验，比整张卡打不开好。
        # 但**必须出声**：静默跳过就会表现成"这个窗口没产出经验"，与真的没产出无法区分。
        print(f"[viewer] 窗口快照读取失败，跳过 {snap_path.name}: {exc}")
        return None


def _merge_cleared_experience(node_by_slug: dict, edges: list, snapshots, origin: str,
                              subject: str) -> int:
    """把窗口快照里**已被聚合清掉的经验层**并进图里，节点/边打 `cleared` 标记。返回并入的节点数。

    只取经验层：记忆层在活图谱里已经有了（而且是更新的版本），重复并入会用旧快照覆盖新状态。

    边只取**至少一端落在经验层**的：纯记忆层之间的边活图谱里已有。跨层边正是「哪条具体经历
    支撑了这条经验」的出处连线 —— 实测 alice 有 67 条，且 67/67 的记忆层那一端都还在活图谱里，
    所以并进来不会画出断头线（`edges` 那段的 `not in node_by_slug` 判断仍然兜着，
    防的是将来记忆层也被清理时出现的悬空边）。

    ★ **同 slug 不覆盖活图谱**：本 run 实测 95 个经验概念跨 6 窗口零冲突，但同名一旦出现，
      覆盖会静默改掉活节点的标题/摘要。宁可丢掉那条快照记录也不污染当前状态。

    ★ **技能与经验层同机制**（2026-08-26）：题内聚合成功后 `clear_skills()` 与 `clear_layer`
      同时清（window.py:376-380），故被聚合走的技能也只在窗口快照里。这里照经验层同一套
      从快照 skill 表捞回来、打 `cleared` 标记 —— 技能是独立表（无边），不参与下面的边合并。
    """
    # 快照库都在网络盘上，一个 open+查询约 1s，45 个串行 ≈ 46s（副脑卡打开慢的根因）。
    # 每个快照是独立 sqlite 文件，先全部并行读回来，再在主线程按窗口顺序合并（保持确定性）。
    snap_paths = [(w, p) for w, p in (snapshots or ()) if p.exists()]
    if snap_paths:
        with ThreadPoolExecutor(max_workers=min(8, len(snap_paths))) as pool:
            snap_data = list(pool.map(lambda t: _read_arm_snapshot(*t), snap_paths))
    else:
        snap_data = []
    n_cleared = 0
    for (window, snap_path), data in zip(snap_paths, snap_data):
        if data is None:
            continue
        rows = data["rows"]; exp_slugs = data["exp_slugs"]
        facet_counts = data["facet_counts"]; edge_rows = data["edge_rows"]
        skill_snap_rows = data["skill_snap_rows"]

        for row in rows:
            slug = row[0]
            if slug in node_by_slug:
                continue          # 见上：绝不覆盖活图谱
            node = _concept_row_to_node(row[:4], facet_counts.get(slug, 0), origin, subject)
            node["cleared"] = True
            node["cleared_window"] = window
            node_by_slug[slug] = node
            n_cleared += 1

        # 被聚合清掉的技能：同 slug 不覆盖（与经验层同原则），打 cleared 标记 + 窗口号。
        # kind="skill" 让前端画六边形 + 走 /skill 详情；详情接口会回查快照取 js_code。
        for s_slug, s_name, intent, signature, verified, sc, fc in skill_snap_rows:
            if s_slug in node_by_slug:
                continue
            node_by_slug[s_slug] = {
                "id": s_slug, "slug": s_slug, "title": s_name, "subject": subject,
                "summary": intent, "difficulty": 3, "facet_count": 0, "chapter": None,
                "origin": origin, "in_degree": 0, "out_degree": 0,
                "kind": "skill", "verified": bool(verified),
                "success_count": sc, "fail_count": fc,
                "cleared": True, "cleared_window": window,
            }
            n_cleared += 1

        for edge_id, from_slug, to_slug, relation in edge_rows:
            if from_slug not in exp_slugs and to_slug not in exp_slugs:
                continue          # 纯记忆层的边：活图谱里已有
            if from_slug not in node_by_slug or to_slug not in node_by_slug:
                continue          # 悬空边（实测为 0，留着防将来）
            edges.append({
                "id": f"cleared:{window}:{edge_id}",   # 加前缀防与活图谱的边 id 撞
                "src": from_slug, "dst": to_slug, "relation": relation,
                "status": "verified", "cleared": True,
            })
            node_by_slug[from_slug]["out_degree"] += 1
            node_by_slug[to_slug]["in_degree"] += 1
    return n_cleared


# ★ 图谱结果缓存（2026-09-29 加）：副脑卡要并行读几十个网络盘上的快照库，即使并行后仍要数秒。
#   指纹 = 活库/技能库/全部快照的 (mtime,size) + 参数：
#   · 指纹没变 → 直接回缓存（跑完的 run 基本永久命中）
#   · 指纹变了但距上次构建不足 TTL → 先回旧图，后台不重建；正在跑的 run 每个窗口都在改 db，
#     只按指纹判等于每次点开都重付快照 IO 的钱。图晚几十秒可接受，卡打不开不可接受。
_MEMORY_GRAPH_CACHE: dict[str, tuple[tuple, float, dict]] = {}
_MEMORY_GRAPH_TTL = 60.0


def _stat_fingerprint(path: Path) -> tuple[str, int, int] | None:
    try:
        st = path.stat()
        return (str(path), st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _memory_graph_fingerprint(db_path: Path, origin: str, annotate_changes: bool, snapshots) -> tuple:
    deps = [_stat_fingerprint(db_path)]
    if origin == "octopus_arm":
        m = _ARM_FILE_RE.match(db_path.name)
        if m:
            skill_db = _resolve_skill_db(db_path.parent.parent.name, m.group("key"))
            if skill_db is not None:
                deps.append(_stat_fingerprint(skill_db))
    deps.extend(_stat_fingerprint(p) for _, p in (snapshots or ()))
    return (str(db_path), origin, annotate_changes, tuple(deps))


def build_memory_graph(db_path: Path, origin: str = "octopus_arm", annotate_changes: bool = False,
                       snapshots: "list[tuple[int, Path]] | None" = None) -> dict:
    if not db_path.exists():
        return {"nodes": [], "edges": [], "stats": {"concept_count": 0, "skill_count": 0,
                "edge_count": 0, "subjects": [], "relation_types": {}}}
    key = str(db_path)
    now = time.time()
    hit = _MEMORY_GRAPH_CACHE.get(key)
    if hit:
        old_fp, ts, result = hit
        # ★ 2026-11-11：TTL 内**直接**回，别先算指纹 —— 算一次要对几十个窗口快照
        #   逐个 stat，网络盘上就是 1~2s（实测 /graph 热请求全花在这）。这正是
        #   本函数注释「指纹变了但距上次构建不足 TTL → 先回旧图」的本意，旧实现
        #   把判断顺序写反了。deepcopy 防止调用方/序列化共享可变结构。
        if now - ts < _MEMORY_GRAPH_TTL:
            return copy.deepcopy(result)
        fp = _memory_graph_fingerprint(db_path, origin, annotate_changes, snapshots)
        if old_fp == fp:
            _MEMORY_GRAPH_CACHE[key] = (fp, time.time(), result)  # 续期，不重建
            return copy.deepcopy(result)
    else:
        fp = _memory_graph_fingerprint(db_path, origin, annotate_changes, snapshots)
    built = _build_memory_graph_uncached(db_path, origin, annotate_changes, snapshots)
    _MEMORY_GRAPH_CACHE[key] = (fp, time.time(), built)
    return copy.deepcopy(built)


def _build_memory_graph_uncached(db_path: Path, origin: str = "octopus_arm", annotate_changes: bool = False,
                       snapshots: "list[tuple[int, Path]] | None" = None) -> dict:
    """跟 build_graph() 平行的一套实现：读 memory_graph.db 同构 schema 的 sqlite（而不是
    markdown），映射成前端通用的 {nodes, edges, stats} 结构——node = concept（不是 facet），跟
    openkage/memory/graph.py 的 Concept/Facet/Edge 三层结构对应。

    slug 本身就是 concept 的主键（没有单独的 UUID id 了），edge 也直接存
    from_slug/to_slug——不用像改之前那版一样再维护一份 id→slug 映射做转换。

    主脑/对照组/副脑三种卡都传自己那份 db 复用这套逻辑（三者同构 schema）。origin 用于节点上色
    标签区分来源。

    annotate_changes=True 时（只有主脑卡 octopus_brain 传 True）额外打 change_status：种子
    （brain_seed.db，有种子的域才有）拷进来的 concept/facet/edge 全部 created_at=0，本次
    训练真正碰过的才 >0（round 见 openkage/roundcode.py，编码 = epoch/batch/window 三段）。
    节点本身是本次新建的（concept.created_at>0）标 new；节点来自种子但挂的某个 facet 被本次
    训练新增/强化过（facet.created_at>0 或 updated_at>0）标 modified；否则不标（前端据此
    判断要不要画高亮环，另两种卡不传这个参数，省一次查询）。
    注意无种子的探索域（minecraft）主脑是从空图起步，所有节点都是本次训练建的，
    于是整张图全标 new——这不是 bug，"全新"确实是事实。"""
    if not db_path.exists():
        return {"nodes": [], "edges": [], "stats": {"concept_count": 0, "skill_count": 0, "edge_count": 0, "subjects": [], "relation_types": {}}}

    with sqlite3.connect(db_path) as conn:
        if annotate_changes:
            concept_rows = conn.execute(
                "SELECT slug, name, summary, layer, created_at FROM concept"
            ).fetchall()
            facet_stat_rows = conn.execute(
                "SELECT concept_slug, COUNT(*), "
                "MAX(CASE WHEN created_at > 0 OR updated_at > 0 THEN 1 ELSE 0 END) "
                "FROM facet GROUP BY concept_slug"
            ).fetchall()
            facet_counts = {slug: count for slug, count, _ in facet_stat_rows}
            facet_touched = {slug for slug, _, touched in facet_stat_rows if touched}
            edge_rows = conn.execute(
                "SELECT id, from_slug, to_slug, relation, created_at FROM edge"
            ).fetchall()
        else:
            concept_rows = conn.execute("SELECT slug, name, summary, layer FROM concept").fetchall()
            facet_counts = dict(
                conn.execute("SELECT concept_slug, COUNT(*) FROM facet GROUP BY concept_slug").fetchall()
            )
            edge_rows = conn.execute("SELECT id, from_slug, to_slug, relation FROM edge").fetchall()
        # skill 是独立表（无 from_slug/to_slug，与 concept 无边关系，靠 intent 向量检索）——
        # 作为孤岛节点画进图，不连边。表在所有 MemoryGraph schema 库里都存在（octopus 三种
        # origin 都是同构 schema），但用 try 兜底：个别 checkpoint 快照可能是更早的库、没这张表。
        try:
            skill_rows = conn.execute(
                "SELECT slug, name, intent, signature, verified, success_count, fail_count, "
                "created_at, updated_at FROM skill"
            ).fetchall()
        except sqlite3.OperationalError:
            skill_rows = []

    # ★ **技能归属重构（2026-08-27）**：技能不再存 arm_db / brain.db，改存各臂常驻
    #   `skills/skill_{arm_key}.db`（跨 epoch/window 存活、聚合不再清）。副脑卡（octopus_arm）
    #   要从那个库再读一份技能 —— arm_db 的 skill 表在新版 run 里是空的，不补就读出 0 技能。
    #   brain/frozen 卡不进这里（origin 守卫挡住）：主脑本就不存技能、新版 brain.db 技能表为空。
    #   arm_key 与 run 目录从 db_path 文件名反解（与 _arm_snapshots 同款，见 server.py:520-524）。
    skill_db_rows: list = []
    if origin == "octopus_arm":
        m = _ARM_FILE_RE.match(db_path.name)
        if m:
            skill_db = _resolve_skill_db(db_path.parent.parent.name, m.group("key"))
            if skill_db is not None:
                # 只读打开：run 进行中框架持续写这个库，mode=ro 永不阻塞写方、不撞锁，只看最后提交态。
                try:
                    with sqlite3.connect(f"file:{skill_db}?mode=ro", uri=True) as sconn:
                        skill_db_rows = sconn.execute(
                            "SELECT slug, name, intent, signature, verified, success_count, "
                            "fail_count, created_at, updated_at FROM skill"
                        ).fetchall()
                except sqlite3.OperationalError:
                    skill_db_rows = []

    node_by_slug = {}
    subject = _domain_of_db(db_path)
    for row in concept_rows:
        slug = row[0]
        node = _concept_row_to_node(row[:4], facet_counts.get(slug, 0), origin, subject)
        if annotate_changes:
            created_at = row[4]
            if created_at and created_at > 0:
                node["change_status"] = "new"
            elif slug in facet_touched:
                node["change_status"] = "modified"
        node_by_slug[slug] = node

    # 技能节点：kind="skill" 让前端切换形状/点击分支。facet_count/degree 全 0（孤岛），
    # summary 放 intent 供悬停速览，完整字段（js_code/preconditions/计数）点开走 /skill 接口。
    # change_status 与 concept 同口径（仅主脑卡 annotate_changes=True 时算）：created_at>0
    # = 本次新建标 new；否则 updated_at>0 = 种子技能被改过标 modified。这样「本次训练变化」
    # 角标也覆盖技能节点，不再只标 concept。
    # ★ arm_db 行在先、skill_db 行在后：first-seen-wins（同 slug 不覆盖）。新版 run arm_db 技能表
    #   为空，技能全来自 skill_db；老 run 无 skills/ 目录（skill_db_rows 恒空），仍只读 arm_db。这份
    #   合并必须在 _merge_cleared_experience 之前，让后者「同 slug 不覆盖」守卫正确跳过快照重复。
    for row in list(skill_rows) + list(skill_db_rows):
        s_slug, s_name, intent, signature, verified, sc, fc, s_created, s_updated = row
        node = {
            "id": s_slug, "slug": s_slug, "title": s_name, "subject": subject,
            "summary": intent, "difficulty": 3, "facet_count": 0, "chapter": None,
            "origin": origin, "in_degree": 0, "out_degree": 0,
            "kind": "skill", "verified": bool(verified),
            "success_count": sc, "fail_count": fc,
        }
        if annotate_changes:
            if s_created and s_created > 0:
                node["change_status"] = "new"
            elif s_updated and s_updated > 0:
                node["change_status"] = "modified"
        node_by_slug[s_slug] = node

    edges = []
    for edge_row in edge_rows:
        edge_id, from_slug, to_slug, relation = edge_row[:4]
        if from_slug not in node_by_slug or to_slug not in node_by_slug:
            continue
        edge = {"id": edge_id, "src": from_slug, "dst": to_slug, "relation": relation, "status": "verified"}
        if annotate_changes and edge_row[4] and edge_row[4] > 0:
            edge["change_status"] = "new"
        edges.append(edge)
        node_by_slug[from_slug]["out_degree"] += 1
        node_by_slug[to_slug]["in_degree"] += 1

    n_cleared = _merge_cleared_experience(node_by_slug, edges, snapshots, origin, subject)

    relation_types: dict[str, int] = {}
    for e in edges:
        relation_types[e["relation"]] = relation_types.get(e["relation"], 0) + 1

    # 按节点 kind 统计：cleared 技能也带 kind="skill"，故 skill_count 含被聚合清掉的；
    # concept_count 只数 concept（经验层 cleared 节点也是 concept），不含 skill 孤岛。
    n_skill = sum(1 for n in node_by_slug.values() if n.get("kind") == "skill")
    return {
        "nodes": list(node_by_slug.values()),
        "edges": edges,
        "stats": {
            "concept_count": len(node_by_slug) - n_skill,
            "skill_count": n_skill,
            "edge_count": len(edges),
            "subjects": [subject] if node_by_slug else [],
            "relation_types": relation_types,
            # 前端图例用：有多少个节点是「已聚合进主脑、已从副脑清掉」的
            "cleared_count": n_cleared,
        },
    }


def _fmt_batch_round(r: int) -> str:
    """二级图谱的 round 号是 (epoch,batch,window) 的内部编码，直接显示「第10101轮」会让人
    误以为跑了上万轮 —— 反解成「第N轮第M批第K窗口」的人话。

    ★ **解码逻辑不在这里**：它在 `openkage/roundcode.py`，与编码同一个文件。此前这里自己写
    `divmod(r, 100)`，于是同一个约定在 viewer 和框架各存一份 —— 2026-08-09 加 window 维时
    正是这种形状会静默漂移（框架写三段、viewer 按两段解，round 显示成怪值却不报错）。
    该模块故意零依赖，import 它不会把框架拖进来。
    """
    return roundcode.fmt(r)


def memory_concept_detail(slug: str, db_path: Path) -> dict:
    if not db_path.exists():
        raise HTTPException(404, f"memory db 不存在: {db_path}")
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT slug, name, summary, layer, created_at FROM concept WHERE slug = ?", (slug,)
        ).fetchone()
        if row is None:
            raise HTTPException(404, f"memory concept not found: {slug}")
        facet_rows = conn.execute(
            # 2026-08-07：facet 的 reinforcement_count 已删（强度只挂 concept，见
            # openkage/memory/schema.py）——这里跟着去掉，否则 no such column。
            "SELECT kind, content, created_at, updated_at "
            "FROM facet WHERE concept_slug = ? ORDER BY kind",
            (slug,),
        ).fetchall()
        edge_rows = conn.execute(
            "SELECT relation, from_slug, to_slug FROM edge WHERE from_slug = ? OR to_slug = ?",
            (slug, slug),
        ).fetchall()

    # 第 4 列是层标记（见 _concept_row_to_node 的注释）
    concept_slug, name, summary, layer, created_at = row
    neighbors = [
        {"relation": relation, "peer_id": (to_slug if from_slug == concept_slug else from_slug),
         "direction": "out" if from_slug == concept_slug else "in"}
        for relation, from_slug, to_slug in edge_rows
    ]
    return {
        "id": concept_slug, "slug": concept_slug, "title": name,
        "summary": summary, "subject": _domain_of_db(db_path), "difficulty": 3,
        "aliases": [], "tags": [], "chapter": layer,
        # memory 层的 created_at 就是 day_idx（第几天记下的）——minecraft 域反思时按天写入，
        # 见 domains/minecraft/__init__.py 的 reflect(day_idx, ...)。前端用它去拉当天的
        # 游戏画面（footage 接口按 day 过滤）。experience 层是通名、没有天的含义。
        "day": created_at if layer == "memory" else None,
        "source_sections": [],
        "facets": [
            {
                "kind": kind,
                # ⚠️ 这里原本拼了一句「（强化 rc 次）」—— 而 `rc` 早已不存在：
                # 2026-08-07 那次「强度只挂 concept」的改造删掉了 facet.reinforcement_count，
                # 上面的 SELECT 跟着去掉了那一列，**但这行 f-string 漏改**，于是
                # `NameError: name 'rc' is not defined` —— 点开任何一个图谱节点都是
                # 500，前端表现为「点了没反应、卡片不弹出」（2026-08-10 修）。
                # facet 级已经没有强度可言（强度是 concept 级的 `concept.strength`，
                # 本响应目前不返回它 —— 前端详情面板也没有用它的位置），
                # 所以正文就是正文，不再追加任何计数。
                "content": content,
                "pages": [],
                # 跟 build_memory_graph(annotate_changes=True) 同一套判断：facet 是本次训练新建的
                # （created_at>0）标 new，种子 facet 但被本次强化过（updated_at>0）标
                # modified，否则不标（前端没这个字段就不画角标）。二级图谱（全部单 batch 内新建）
                # 传进来的 db 也会算出这个字段，只是意义不大，前端目前只在一级图谱详情面板用它。
                "change_status": ("new" if cr and cr > 0 else ("modified" if ur and ur > 0 else None)),
            }
            for kind, content, cr, ur in facet_rows
        ],
        "neighbors": neighbors,
    }



def list_books() -> list[dict]:
    """书架 = run 卡的并集（"book" 在本工具里读作"一张卡"，见文件头命名说明）：
    记忆图谱 3 种（主脑/冻结对照组/副脑）+ planner-only 轨迹卡。任一来源为空
    就是少几张卡，不报错。"""
    return (list_octopus_brain_books() + list_octopus_frozen_books()
            + list_octopus_arm_books())


def build_graph(origin: str, subject: str, book: str, as_of_step: str | None = None) -> dict:
    if origin in ("octopus_brain", "octopus_frozen", "octopus_arm"):
        # book 参数在 dispatch 里其实传的是 book_slug（前端点章鱼图谱卡时用 book_slug 当 book 段：
        # 主脑/对照组是 run 目录名，副脑是 "<run 目录名>__<db 文件名>"）
        db_path = _resolve_octopus_db(origin, book)
        if db_path is None:
            # 发布包不带图谱 db：返回空图而非 404，前端展示空态
            # （run 的看点在指标/清单面板，不在图谱画布）
            return {"nodes": [], "edges": [],
                    "stats": {"concept_count": 0, "edge_count": 0, "subjects": [], "relation_types": {}}}
        # 只有主脑标"本次训练变化"高亮：对照组恒等于初始状态、副脑本身整张都是本批新产出的，
        # 标了没有信息量
        #
        # 窗口快照**只对副脑卡**并入：那些快照是「这个 arm 在第 N 个窗口边界被清掉的经验层」，
        # 天然属于它自己那张卡。主脑卡不需要（经验已经聚合成主脑节点了，那才是它的正文）；
        # 对照组是冻结的、压根不跑聚合。
        return build_memory_graph(
            db_path, origin=origin,
            annotate_changes=(origin == "octopus_brain"),
            snapshots=_arm_snapshots(db_path) if origin == "octopus_arm" else None)
    raise HTTPException(400, f"未知 origin: {origin}")


def concept_detail(origin: str, subject: str, book: str, slug: str, as_of_step: str | None = None) -> dict:
    if origin in ("octopus_brain", "octopus_frozen", "octopus_arm"):
        db_path = _resolve_octopus_db(origin, book)  # book 段是 book_slug（见 bookApiBase）
        if db_path is None:
            raise HTTPException(404, f"知识图谱已销毁或不存在（{origin}）: {book}")
        try:
            return memory_concept_detail(slug, db_path)
        except HTTPException:
            # 活图谱里没有 → 可能是**已被聚合清掉的经验层**节点（图上画着它，点开却 404 就是
            # 「看得见点不开」）。依次翻窗口快照，找到就带上 cleared 标记返回。
            if origin != "octopus_arm":
                raise
            for window, snap in _arm_snapshots(db_path):
                try:
                    detail = memory_concept_detail(slug, snap)
                except HTTPException:
                    continue
                detail["cleared"] = True
                detail["cleared_window"] = window
                return detail
            raise
    raise HTTPException(400, f"未知 origin: {origin}")


app = FastAPI(title="知识图谱可视化器（OpenKAGE）")


class _JsonGzipMiddleware:
    """只对 application/json 响应压缩，避开 MJPEG / PNG / MP4 等流式与文件响应。

    Starlette 自带的 GZipMiddleware 只排除 text/event-stream，会把 /api/live 的
    multipart/x-mixed-replace 直播流和 FileResponse 一起压（流式 gzip 要 flush 才出帧，
    直播会卡）；而 /api/books 这类 JSON 实测 756KB → gzip 25KB，收益全在 JSON 上。
    只处理带 content-length 的一次性响应 —— FastAPI 的 JSONResponse 都是这种。
    """

    def __init__(self, app, minimum_size: int = 1024, compresslevel: int = 6):
        self.app = app
        self.minimum_size = minimum_size
        self.compresslevel = compresslevel

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        accept = ""
        for k, v in scope.get("headers", []):
            if k.lower() == b"accept-encoding":
                accept = v.decode("latin-1")
                break
        if "gzip" not in accept.lower():
            await self.app(scope, receive, send)
            return

        state = {"start": None, "skip": False}

        async def send_wrapper(message):
            mtype = message["type"]
            if mtype == "http.response.start":
                raw = message["headers"]
                ct = next((v.decode("latin-1") for k, v in raw
                           if k.lower() == b"content-type"), "")
                has_len = any(k.lower() == b"content-length" for k, v in raw)
                if ct.startswith("application/json") and has_len:
                    state["start"] = message   # 扣住 start，等 body 收齐后改 headers
                    return
                state["skip"] = True
                await send(message)
                return
            if mtype == "http.response.body" and not state["skip"]:
                if message.get("more_body"):   # 保守：真出现流式 JSON 就原样放行
                    await send(state["start"])
                    state["skip"] = True
                    await send(message)
                    return
                body = message.get("body", b"")
                if len(body) >= self.minimum_size:
                    packed = gzip.compress(body, self.compresslevel)
                    headers = [(k, v) for k, v in state["start"]["headers"]
                               if k.lower() not in (b"content-length", b"content-encoding", b"vary")]
                    headers += [(b"content-encoding", b"gzip"),
                                (b"content-length", str(len(packed)).encode()),
                                (b"vary", b"Accept-Encoding")]
                    await send({**state["start"], "headers": headers})
                    await send({"type": "http.response.body", "body": packed})
                else:
                    await send(state["start"])
                    await send(message)
                return
            await send(message)

        await self.app(scope, receive, send_wrapper)


# ★ 2026-11-11 撤下自制 _JsonGzipMiddleware：curl 正常但浏览器里所有 application/json
#   请求（/scene、/api/books）都挂住不返回（PNG 等未压缩响应正常），导致地图一直
#   "加载中"、<image> 不渲染。传输压缩收益远小于这个故障，先停用；类保留备查。
# app.add_middleware(_JsonGzipMiddleware)


# ★ 书架缓存：/api/books 是前端 10s 轮询的接口，而多根扫描实测要几十秒 ⇒ 加 TTL 缓存，
#   让轮询打到缓存上。新跑出来的卡最迟 TTL 后出现，列表页差几十秒不影响使用（点开卡片走的
#   是 graph/concept 接口，那些不缓存，始终是实时的）。
_BOOKS_CACHE: dict[str, Any] = {"ts": 0.0, "books": None}
_BOOKS_TTL = 600.0   # 10 分钟：一次全量扫盘要几分钟，60s 一刷会让后台线程常驻扫盘
_BOOKS_REFRESHING = threading.Lock()


# ★ 书架扫描专用的文件系统增量缓存（2026-09-29 加）：
#   网络盘上一次 Path.stat()/is_dir()/iterdir() 就是几毫秒的往返，一次 list_books()
#   要打约 3000 下 ⇒ 即使 concept 数已缓存，整体仍要 16s。列表页并不要求「刚落盘的目录
#   秒出」（/api/books 本身还有 60s TTL），所以这里按 TTL 缓存存在性/目录清单/文件 stat。
#   ★ 只给 list_books() 扫描路径用：graph/video 等「点开卡片」的实时接口不走这套，始终真查。
_SCAN_TTL = 300.0  # 要明显大于 _BOOKS_TTL(60s)：否则每轮过期重建都重新付一遍全量 stat 的钱
_SCAN_KIND: dict[str, tuple[str, float]] = {}    # path → ("dir"/"file"/"missing", 到期时刻)
_SCAN_DIRS: dict[str, tuple[tuple[str, ...], float]] = {}  # path → (子项名, 到期时刻)
_SCAN_STATS: dict[str, tuple[tuple[int, int], float]] = {}  # path → ((mtime_ns,size), 到期时刻)


def _scan_kind(path: Path) -> str:
    key = str(path)
    now = time.time()
    hit = _SCAN_KIND.get(key)
    if hit and now < hit[1]:
        return hit[0]
    kind = "missing"
    try:
        if path.is_dir():
            kind = "dir"
        elif path.is_file():
            kind = "file"
    except OSError:
        pass
    _SCAN_KIND[key] = (kind, now + _SCAN_TTL)
    return kind


def _scan_is_dir(path: Path) -> bool:
    return _scan_kind(path) == "dir"


def _scan_is_file(path: Path) -> bool:
    return _scan_kind(path) == "file"


def _scan_exists(path: Path) -> bool:
    return _scan_kind(path) != "missing"


def _scan_iterdir(path: Path) -> list[Path]:
    key = str(path)
    now = time.time()
    hit = _SCAN_DIRS.get(key)
    if hit and now < hit[1]:
        return [path / n for n in hit[0]]
    try:
        names = tuple(sorted(p.name for p in path.iterdir()))
    except OSError:
        names = ()
    _SCAN_DIRS[key] = (names, now + _SCAN_TTL)
    return [path / n for n in names]


def _scan_stat_pair(path: Path) -> tuple[int, int] | None:
    key = str(path)
    now = time.time()
    hit = _SCAN_STATS.get(key)
    if hit and now < hit[1]:
        return hit[0]
    try:
        st = path.stat()
        pair = (st.st_mtime, st.st_size)
    except OSError:
        pair = None
    if pair is not None:
        _SCAN_STATS[key] = (pair, now + _SCAN_TTL)
    return pair


def _refresh_books_cache(force: bool = False) -> list[dict]:
    """同步重建书架缓存。force=True 用于后台预热/过期重建；否则 TTL 未到直接复用。"""
    if not force:
        if _BOOKS_CACHE["books"] is not None and time.time() - _BOOKS_CACHE["ts"] < _BOOKS_TTL:
            return _BOOKS_CACHE["books"]
    # 请求并发打进来时只让一个线程扫多根，其余等它把缓存填好（几十秒的扫描跑 N 份纯浪费）
    with _BOOKS_REFRESHING:
        if not force and _BOOKS_CACHE["books"] is not None \
                and time.time() - _BOOKS_CACHE["ts"] < _BOOKS_TTL:
            return _BOOKS_CACHE["books"]
        # ★ 一次性换缓存（2026-11-11 二改）：曾试过"每扫完一类就更新缓存"的分批可见，
        #   但扫描中途页面会出现「166 张主脑卡、0 张副脑卡」的半截书架，看着像数据丢了。
        #   api_books 已不再等这把锁（见 _start_books_refresh），冷扫期间书架维持上一份
        #   完整列表、首启返回空列表+前端转圈，都不会出现半截状态。
        books = list_books()
        _BOOKS_CACHE["ts"] = time.time()
        _BOOKS_CACHE["books"] = books
        return books


def list_books_cached() -> list[dict]:
    now = time.time()
    if _BOOKS_CACHE["books"] is not None and now - _BOOKS_CACHE["ts"] < _BOOKS_TTL:
        return _BOOKS_CACHE["books"]
    books = list_books()
    _BOOKS_CACHE["ts"] = now
    _BOOKS_CACHE["books"] = books
    return books


_BOOKS_REBUILDING = False
_BOOKS_REBUILD_GUARD = threading.Lock()


def _start_books_refresh() -> None:
    """只起一个后台重建线程，且**不占用请求线程**。

    ★ 2026-11-11：此前 api_books 首建会同步排在 _BOOKS_REFRESHING 锁后等冷扫（几分钟），
      前端 10s 一轮询 → 浏览器到该域名的 6 个 HTTP/1.1 连接全被 pending 请求占满，
      连 /scene 的 fetch 都排不上队（服务端日志 200，浏览器却一直收不到），地图永远
      停在「加载中」。这里改成：请求线程立即返回，重建只在唯一的后台线程里跑。
    """
    global _BOOKS_REBUILDING
    with _BOOKS_REBUILD_GUARD:
        if _BOOKS_REBUILDING:
            return
        _BOOKS_REBUILDING = True

    def run():
        global _BOOKS_REBUILDING
        try:
            _refresh_books_cache(force=True)
        finally:
            with _BOOKS_REBUILD_GUARD:
                _BOOKS_REBUILDING = False
    threading.Thread(target=run, daemon=True, name="books-refresh").start()


@app.get("/api/books")
def api_books():
    books = _BOOKS_CACHE["books"]
    if books is None:
        _start_books_refresh()
        return {"books": [], "rev": 0.0, "warming": True}
    if time.time() - _BOOKS_CACHE["ts"] >= _BOOKS_TTL:
        _start_books_refresh()
    return {"books": books, "rev": _BOOKS_CACHE["ts"]}


@app.on_event("startup")
def warm_books_cache():
    # 启动即后台预热：服务起来时就开始扫，扫描过程分批写入缓存（用户先看到已有卡片）。
    _start_books_refresh()


@app.get("/api/books/{origin}/{subject}/{book:path}/graph")
def api_graph(origin: str, subject: str, book: str, as_of_step: str | None = None):
    return build_graph(origin, subject, book, as_of_step)


@app.get("/api/books/{origin}/{subject}/{book:path}/concept/{slug}")
def api_concept(origin: str, subject: str, book: str, slug: str, as_of_step: str | None = None):
    return concept_detail(origin, subject, book, slug, as_of_step)


def _skill_detail_from_db(slug: str, db_path: Path) -> dict | None:
    """从单个 db（活图谱或窗口快照）取一条技能详情；没有就返回 None。"""
    with sqlite3.connect(db_path) as conn:
        try:
            row = conn.execute(
                "SELECT slug, name, intent, signature, preconditions, js_code, "
                "verified, success_count, fail_count, "
                "total_success_count, total_fail_count FROM skill WHERE slug = ?",
                (slug,),
            ).fetchone()
        except sqlite3.OperationalError:
            return None  # 老库无 skill 表
    if row is None:
        return None
    (s_slug, name, intent, signature, preconditions, js_code, verified,
     sc, fc, tsc, tfc) = row
    return {
        "kind": "skill", "id": s_slug, "slug": s_slug, "title": name,
        "subject": _domain_of_db(db_path),
        "intent": intent, "signature": signature,
        "preconditions": preconditions, "js_code": js_code,
        "verified": bool(verified), "success_count": sc, "fail_count": fc,
        # 全程累计计数（success/fail_count 是「当前代码版本窗口」，update_skill 改 js_code
        # 时归零；total_* 是所有版本成败全程累计、永不清零，见 skill 表 schema 注释）。
        "total_success_count": tsc, "total_fail_count": tfc,
    }


@app.get("/api/books/{origin}/{subject}/{book:path}/skill/{slug}")
def api_skill_detail(origin: str, subject: str, book: str, slug: str):
    """技能详情。skill 是独立表（与 concept 无边），只有 octopus 三种 origin 的 MemoryGraph
    schema 库才有；real/staged 是 markdown，根本走不到这里（前端不会对那两种卡发这个请求）。

    查找顺序（层层兜底，找不到才 404）：
    ① 副脑卡优先查本臂常驻 `skills/skill_{arm_key}.db`（技能归属重构 2026-08-27 后技能都在这里）；
    ② 活图谱（brain.db / arm_db）—— 老版 run 技能就在 brain.db；
    ③ 副脑卡再翻窗口快照：被聚合清掉的技能只在快照里（与经验层 cleared 同机制），带 cleared 标记返回。"""
    db_path = _resolve_octopus_db(origin, book)
    if db_path is None:
        raise HTTPException(404, f"知识图谱已销毁或不存在（{origin}）: {book}")
    # ① 副脑卡的本臂常驻技能库（新版 run 技能的家）
    if origin == "octopus_arm":
        parsed = _parse_arm_book_slug(book)
        if parsed:
            skill_db = _resolve_skill_db(parsed[0], parsed[1])
            if skill_db is not None:
                detail = _skill_detail_from_db(slug, skill_db)
                if detail is not None:
                    return detail
    # ② 活图谱（老版 run 技能在 brain.db / 罕见地直接在 arm_db）
    detail = _skill_detail_from_db(slug, db_path)
    if detail is not None:
        return detail
    # ③ 副脑卡翻窗口快照（老 cleared-skill 兜底，新版 run 快照无技能 → 自然落空）
    if origin != "octopus_arm":
        raise HTTPException(404, f"skill not found: {slug}")
    for window, snap in _arm_snapshots(db_path):
        detail = _skill_detail_from_db(slug, snap)
        if detail is not None:
            detail["cleared"] = True
            detail["cleared_window"] = window
            return detail
    raise HTTPException(404, f"skill not found: {slug}")


@app.get("/api/books/{origin}/{subject}/{book:path}/scene")
def api_scene(origin: str, subject: str, book: str):
    """副脑卡的探索地图（轨迹 + 记忆层地点/边）。`book` 段是 book_slug（见前端 bookApiBase）。

    只有 octopus_arm 有地图：地理坐标只存在于**私有图谱的记忆层**。主脑（brain.db）实测
    26 个概念全在经验层、一个坐标都没有 —— 经验是去情境化的通名结论，"夜间地表危险"没有
    坐标，这是分层设计的应有结果（见 openkage/memory 的 layer 约定），不是数据缺失。
    """
    if origin != "octopus_arm":
        raise HTTPException(400, "只有私有图谱（origin=octopus_arm）有探索地图")
    return build_scene(book)


@app.get("/api/books/{origin}/{subject}/{book:path}/scene/terrain")
def api_scene_terrain(origin: str, subject: str, book: str):
    """探索地图的俯瞰地形底图（PNG，从 checkpoint 的 Minecraft 世界渲染并缓存）。

    冷渲染 ~10s。scene 接口会先一步把渲染丢进后台线程再返回 terrain=null（见 build_scene），
    所以浏览器真正请求这个 url 时多半已在渲染/已渲染完 —— 拿同一把指纹锁等它，
    醒来命中缓存秒回。等不到（scene 那次线程没起来等边角情况）就自己渲染。
    """
    if origin != "octopus_arm":
        raise HTTPException(400, "只有私有图谱（origin=octopus_arm）有探索地图")
    parsed = _parse_arm_book_slug(book)
    if parsed is None:
        raise HTTPException(400, f"不是合法的私有图谱卡 book_slug: {book}")
    run_slug, arm = parsed
    _, root = _split_slug_root(book)
    latest_world = wsr._latest_world(root / run_slug, arm)
    if latest_world is None:
        raise HTTPException(404, f"checkpoint 里没有 {arm} 的世界数据")
    with wsr.render_lock(wsr._region_fingerprint(latest_world)):
        meta = wsr.render_world_topdown(latest_world.parent)
    return FileResponse(meta["path"], media_type="image/png",
                        headers={"Cache-Control": f"max-age={wsr._CACHE_TTL_SECONDS}"})


#: 录像/截图的落盘根目录（与 experiments/run.py 的 --mc-footage-dir 同义）。
#: 发布包不带录像（has_video 恒 False），路由保留是为了与前端契约一致，落空即空态。
_PKG_ROOT_FOR_FOOTAGE = Path(__file__).resolve().parent.parent
_FOOTAGE_ROOT = Path(os.environ.get(
    "OPENKAGE_FOOTAGE_DIR", "/path/to/footage"))


def _footage_dir_of(book_slug: str, scan: bool = False) -> tuple[Path, str]:
    """副脑卡 book_slug → (该条轨迹的 footage 目录, arm key)。复用地图那套 slug 解析。

    目录形状是 `<run>/<arm>/<slot>/`（slot = `e1b1`，见 domains/minecraft/footage.py）——
    **带 slot 才分得开多个 epoch**：录像文件名此前不含 epoch，第 2 轮会覆盖第 1 轮的素材，
    而 epoch 正是学习曲线的 X 轴。slot 从 book_slug 尾部那段 `_e<数字>b<数字>` 取，
    与图谱 db 的命名同源（同一条轨迹的图谱和录像因此必然配对）。

    run 目录兼容两个位置：`_FOOTAGE_ROOT/<run>/`（--mc-footage-dir 落盘处）与
    `results/footage/<run>/` —— 依次哪个存在用哪个。
    """
    parsed = _parse_arm_book_slug(book_slug)
    if parsed is None:
        raise HTTPException(400, f"不是合法的私有图谱卡 book_slug: {book_slug}")
    run_slug, arm = parsed
    m = _ARM_FILE_RE.match(f"{book_slug.partition('__')[2]}.db")
    slot = f"e{m.group('epoch')}b{m.group('batch')}" if m else "e1b1"
    ext = _FOOTAGE_ROOT / run_slug
    shared = _PKG_ROOT_FOR_FOOTAGE / "results" / "footage" / run_slug
    # 两处都没有时返回 ext（不存在的路径）——调用方统一按「没有录像」空态处理，
    # 这里不抛异常（上游 events.jsonl/steps.jsonl 的 is_file 检查自会兜住）。
    is_dir = _scan_is_dir if scan else (lambda p: p.is_dir())
    root = next((p for p in (ext, shared) if is_dir(p)), ext)
    return (root / arm / slot), arm


@app.get("/api/books/{origin}/{subject}/{book:path}/footage")
def api_footage(origin: str, subject: str, book: str, day: int | None = None):
    """关键事件画面清单（死亡 / tier 突破 / 成就各一帧）。

    数据来自训练时录的 `<arm>/<slot>/events.jsonl`（见 domains/minecraft/footage.py）。
    `day` 给了就只返回那一天的 —— 前端点开某个记忆概念时用它的 `day`（= created_at）过滤，
    把"那天的画面"贴在 facet 下面。

    没开 `--mc-footage` 的 run 没有这个目录 → 返回空列表而不是 404，前端展示空态
    （与"图谱已销毁""没有轨迹"同一待遇）。
    """
    if origin != "octopus_arm":
        return {"events": []}
    fdir, _arm = _footage_dir_of(book)
    jsonl = fdir / "events.jsonl"
    if not jsonl.is_file():
        return {"events": []}
    events = []
    try:
        for line in jsonl.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue  # 录制中途被 kill 可能留下半行，跳过而不是整个接口 500
            # ⚠️ 不再按 `rec["arm"]` 过滤：events.jsonl 现在**每条轨迹一份**（在
            #    <arm>/<slot>/ 下），文件本身就已经限定了 arm 与 epoch/batch。
            #    此前是全 run 一份、靠 arm 字段区分，那样两个 epoch 的事件会混在一个文件里。
            if day is not None and int(rec.get("day", -1)) != int(day):
                continue
            # 图片不在静态目录里，给前端一个能直接放进 <img src> 的取图 URL
            rec["url"] = (f"/api/books/{origin}/{subject}/{book}"
                          f"/footage/file/{rec.get('file', '')}")
            events.append(rec)
    except OSError:
        return {"events": []}
    events.sort(key=lambda r: (int(r.get("day", 0)), int(r.get("step", 0))))
    return {"events": events}


@app.get("/api/books/{origin}/{subject}/{book:path}/footage/file/{path:path}")
def api_footage_file(origin: str, subject: str, book: str, path: str):
    """取一帧画面。`path` 是 events.jsonl 里的文件名（如 `d07_s0042_death.jpg`）。"""
    fdir, _ = _footage_dir_of(book)
    target = (fdir / path).resolve()
    # ★ 路径必须落在本轨迹的 footage 目录内：`path` 直接来自 URL，不校验就是目录穿越
    #   （`../../` 能读到任意文件）。用 resolve() 后比对前缀，符号链接也一并挡掉。
    try:
        target.relative_to(fdir.resolve())
    except ValueError:
        raise HTTPException(403, "非法路径")
    if not target.is_file():
        raise HTTPException(404, f"画面不存在: {path}")
    return FileResponse(target, media_type="image/jpeg")


#: facet 映射的键分隔符。用制表符是因为 slug（`[a-z0-9_]`）和 kind（中文短词）都不含它，
#: 拼出来的键不会歧义。**前端按同一个约定拆**（App.vue 的 facetClip），改这里要一起改。
_FACET_KEY_SEP = "\t"


def _replay_facet_steps(path: Path) -> dict[str, dict]:
    """回放 `facet_steps.jsonl` 的事件日志 → 当前的「facet → 来源步」映射。

    落盘的是**事件**而不是最终状态（理由见 domains/minecraft/footage.py 的
    `mark_facet_source`：被推翻的 facet 不能留着旧片段当假证据）。这里按行序回放：

      set        覆盖 `(slug, kind)` 的来源步与所属天
      drop       删掉该键（`kind` 为空 = 该概念下**全部** facet，del_concept 用）
      move_slug  概念改名/被合并 → 名下所有键搬到新 slug；**不覆盖目标已有的键**
                 （merge 同 kind 撞车时保留的是 primary 那条内容，映射也该保留 primary 的）

    键是 `"<concept_slug><TAB><kind>"` —— facet 表里 `(concept_slug, kind)` 本就唯一。
    """
    out: dict[str, dict] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue  # 训练被 kill 可能留半行，跳过而不是整个接口 500
        slug, kind = rec.get("concept_slug", ""), rec.get("kind", "")
        prefix = f"{slug}{_FACET_KEY_SEP}"
        op = rec.get("op", "set")
        if op == "set":
            out[f"{prefix}{kind}"] = {"day": int(rec.get("day", 0)),
                                      "steps": list(rec.get("steps") or [])}
        elif op == "drop":
            if kind:
                out.pop(f"{prefix}{kind}", None)
            else:
                for k in [k for k in out if k.startswith(prefix)]:
                    out.pop(k, None)
        elif op == "move_slug":
            new_slug = rec.get("new_slug", "")
            if not new_slug:
                continue
            for k in [k for k in out if k.startswith(prefix)]:
                moved = f"{new_slug}{_FACET_KEY_SEP}{k[len(prefix):]}"
                if moved not in out:      # 目标已有的不覆盖（见 docstring）
                    out[moved] = out[k]
                out.pop(k, None)
    return out


def _replay_skill_steps(path: Path) -> dict[str, dict]:
    """回放 `skill_steps.jsonl` 的事件日志 → 当前的「skill slug → 来源步」映射。

    与 `_replay_facet_steps` 同构，差别仅在键：facet 是 `"<concept_slug><TAB><kind>"`（一个概念
    可有多种 kind 的 facet），skill 是单个 slug（无 kind、slug 不可变）。故 op 只有：
      set   覆盖该 slug 的来源步与所属天
      drop  删掉该键（delete_skill 后失效——技能已抹除，旧片段是假证据）

    没有 `move_slug`：skill 的 slug 改不了（`update_skill` 不收 slug 参数），且 `update_skill`
    不碰来源步（用户定案：来源固定指向技能诞生现场）。
    """
    out: dict[str, dict] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue  # 训练被 kill 可能留半行，跳过而不是整个接口 500
        slug = rec.get("slug", "")
        op = rec.get("op", "set")
        if op == "set":
            out[slug] = {"day": int(rec.get("day", 0)),
                         "steps": list(rec.get("steps") or [])}
        elif op == "drop":
            out.pop(slug, None)
    return out


@app.get("/api/books/{origin}/{subject}/{book:path}/video")
def api_video_index(origin: str, subject: str, book: str):
    """这条轨迹的**按天分段**录像 + 「JS 步 → 段内时间区间」索引 + 每条 facet 的录像片段。

    切块单位是 JS 步（一次 write_code = 一段真在游戏里发生的动作），不是 ReAct 轮：
    createplan/updateplan/retrieve 只在 LLM 侧发生、画面一动不动。实测第1天 alice
    48 个 ReAct 轮里只有 34 次 write_code。

    ★ **录像按天切文件**（`day07.mp4`，见 footage.py 的类 docstring：单文件在训练被 kill
    时会整个损坏）。`t0/t1` 是**段内**相对秒数，所以前端拿 `(day, t0)` 就能直接定位。

    ★★ **facet 片段不按天筛**（2026-08-12 用户定案：「facet 不与天绑定，只与视频绑定」）。
    此前这个接口按 `?day=` 过滤，而前端传的是**概念**的 created_at —— 于是第 5 天往第 2 天
    建的概念上补的那条 facet（来源步属于第 5 天）会被静默丢掉、永远看不到画面。
    现在每条 facet 自带 `day`，各挂各的那一段，与概念哪天建的、属于哪一层都无关。
    """
    if origin != "octopus_arm":
        return {"has_video": False, "days": [], "facet_steps": {}}
    fdir, arm = _footage_dir_of(book)
    index = fdir / "steps.jsonl"
    if not index.is_file():
        return {"has_video": False, "days": [], "facet_steps": {}}
    # day → 该天的步清单。分段录像的每一段自成一个时间轴，所以索引也按天分组。
    by_day: dict[int, list[dict]] = {}
    try:
        for line in index.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue  # 训练被 kill 可能留半行，跳过而不是整个接口 500
            by_day.setdefault(int(rec.get("day", 0)), []).append(rec)
    except OSError:
        return {"has_video": False, "days": [], "facet_steps": {}}

    base = f"/api/books/{origin}/{subject}/{book}/video/file"
    days = []
    for d in sorted(by_day):
        seg = fdir / f"day{d:02d}.mp4"
        if not seg.is_file():
            # 有索引没视频：ffmpeg 那一段起不来/被强杀。**跳过而不是给个坏入口** ——
            # 点开一个打不开的 <video> 比没有入口更让人以为是前端坏了。
            continue
        steps = sorted(by_day[d], key=lambda r: int(r.get("step", 0)))
        days.append({
            "day": d,
            "url": f"{base}/{d}",
            "steps": steps,
            "size_mb": round(seg.stat().st_size / 1048576, 1),
            # 整天区间：反思没报来源步时的兜底（放这一整天）
            "range": [min(s["t0"] for s in steps), max(s["t1"] for s in steps)],
        })
    if not days:
        return {"has_video": False, "days": [], "facet_steps": {}}

    # facet → 那几步的**段内**时间区间 + 在哪一天那一段里。
    # 步号由模型填，可能是幻觉/别的天 —— 只留在**它自报那一天**的索引里真实存在的步。
    # 整条都对不上**不给播放入口**（留着会让人点了跳到错误的位置，比没有入口更糟），
    # 但**要如实说出来**：返回 `unresolved` 标记 + 它自报的步号，前端据此显示一句
    # 「来源步对不上」而不是什么都不显示。
    #
    # ★ 为什么不能静默跳过（2026-08-14 修）：静默 = 「这条 facet 没有画面」与「它自报的
    #   步号是幻觉」不可区分，而后者是**模型质量信号**，恰恰是该被看见的。实测那轮
    #   27 处步号对不上（21 处是别的天的真实步号、6 处全程不存在），其中 alice 3 条、
    #   carol 3 条 facet 的步号**全部**对不上 —— 那 6 条在前端曾完全没有任何痕迹。
    #   `partial` 则是另一种情况：部分步号有效，画面能放，但要让人知道有几个没对上。
    facets: dict[str, Any] = {}
    skills: dict[str, Any] = {}
    have = {d["day"]: {int(s["step"]): s for s in d["steps"]} for d in days}

    def _resolve(day: int, claimed: list) -> dict:
        """把一条来源步记录解析成可播片段（facet/skill 共用）。

        - 全对不上 → {day, unresolved, claimed_steps}（不给 url/range，前端不渲染 <video>）
        - 全/部分对上 → {day, url, steps, range}（部分对上额外带 dropped_steps）
        """
        hit = [have[day][n] for n in claimed if day in have and n in have[day]]
        if not hit:
            return {"day": day, "unresolved": True, "claimed_steps": claimed}
        entry = {
            "day": day,
            "url": f"{base}/{day}",
            "steps": [h["step"] for h in hit],
            "range": [min(h["t0"] for h in hit), max(h["t1"] for h in hit)],
        }
        if len(hit) < len(claimed):
            entry["dropped_steps"] = [n for n in claimed if n not in {h["step"] for h in hit}]
        return entry

    for key, rec in _replay_facet_steps(fdir / "facet_steps.jsonl").items():
        # 见上方 _resolve：步号由模型填，可能是幻觉/别的天——只留它自报那天真实存在的步。
        # 全对不上不给入口但要如实说 unresolved（模型质量信号，见 api_video_index docstring）。
        facets[key] = _resolve(rec["day"], list(rec["steps"]))
    for slug, rec in _replay_skill_steps(fdir / "skill_steps.jsonl").items():
        # 技能来源步同理：delete_skill 后这里查不到（drop 已落），create 时落的那条照此解析。
        skills[slug] = _resolve(rec["day"], list(rec["steps"]))
    return {"has_video": True, "arm": arm, "days": days,
            "facet_steps": facets, "skill_steps": skills}


@app.get("/api/books/{origin}/{subject}/{book:path}/video/file/{day}")
def api_video_file(origin: str, subject: str, book: str, day: int):
    """取第 `day` 天那一段录像。

    录像是几百 MB ~ GB 量级，浏览器 `<video>` 靠 HTTP Range（206 分段）才能「点开就跳到
    第 N 秒」而不是先把前面全下完。Starlette 的 FileResponse **自带** Range 支持（源码里有
    http_range/206/content-range 的处理，直接从 ASGI scope 读 headers），所以这里
    不需要显式接 Request —— 已实测确认。
    """
    if origin != "octopus_arm":
        raise HTTPException(400, "只有私有图谱卡有录像")
    fdir, _arm = _footage_dir_of(book)
    # `day` 已由 FastAPI 强制成 int，格式化进文件名不存在穿越风险；仍照旧做前缀校验
    # （fdir 本身来自 book_slug 解析，多一道不亏）。
    video = (fdir / f"day{int(day):02d}.mp4").resolve()
    try:
        video.relative_to(fdir.resolve())
    except ValueError:
        raise HTTPException(403, "非法路径")
    if not video.is_file():
        raise HTTPException(404, f"第 {day} 天的录像不存在")
    return FileResponse(video, media_type="video/mp4")


# ── 直播：直接抓观战客户端的 X 屏（2026-09-28 加）────────────────────────────
# 为什么绕开录像文件：正在写的 `day<NN>.mp4` 还没有 moov（索引写在文件尾，end_day 才落），
# 当天收尾前整个文件打不开 —— 这正是 footage.py「为什么按天切文件」里记的抗损坏取舍。
# Xvfb 屏（:70/:71/:72）是现成的画面源，从它**再**开一路抓取是只读的，不影响训练那一路录制；
# 实测 3fps / 640px ≈ 2.3% 核/臂，本机 48 核上 3 臂同时看也不到 10% 核。
#
# 「正在跑」的判据用 /proc 扫 ffmpeg 而不是看文件 mtime：只有**本机**在跑的 run 才有 X 屏可抓，
# 而同步过来的远端目录 mtime 也在变 —— 按 mtime 判会把远端 run 误判成可直播
# （它的 X 屏根本不在这台机器上）。
_LIVE_FFMPEG = os.environ.get("OPENKAGE_FFMPEG", "/usr/local/bin/ffmpeg")
#: 直播帧率/宽度。录制那一路是 20fps/1280x720；直播只是看个动向，不跟录像同规格。
_LIVE_FPS = 3
_LIVE_WIDTH = 640
#: 单路最长无帧时间。天边界录制器会重启（约 1.5s），训练被 kill 则再也不会有帧 —— 那条
#: 连接不能一直挂着占一个 ffmpeg。
_LIVE_IDLE_TIMEOUT = 30.0
#: 并发上限：每路一个 ffmpeg，不封顶就是「多开几个标签页把 CPU 吃光」。
_LIVE_MAX_STREAMS = 6
_live_stream_count = 0
_live_stream_lock = threading.Lock()

_LIVE_CMD_DISP_RE = re.compile(r"-i\s+:(\d+)")
_LIVE_CMD_SIZE_RE = re.compile(r"-video_size\s+(\d+x\d+)")
#: 输出路径形如 <root>/<run>/<arm>/<slot>/day<NN>.mp4（见 footage.py 的目录约定）
_LIVE_CMD_PATH_RE = re.compile(r"(\S+?/([^/\s]+)/(e\d+b\d+)/day(\d+)\.mp4)")


def _live_recordings() -> list[dict]:
    """扫 /proc 找出**本机**正在录的每一路 ffmpeg →「哪个 run 的哪个臂、在哪个 X 屏上」。

    只认 cmdline 带 `x11grab` 且输出 `day<NN>.mp4` 的进程（就是 footage.py 起的录制器）。
    扫不到就是没有 —— 不抛异常、不猜。
    """
    out: list[dict] = []
    for pid_dir in Path("/proc").iterdir():
        if not pid_dir.name.isdigit():
            continue
        try:
            cmd = (pid_dir / "cmdline").read_bytes().decode("utf-8", "replace").replace("\x00", " ")
        except OSError:
            continue  # 进程在我们扫的这一瞬退了，正常
        if "x11grab" not in cmd or ".mp4" not in cmd:
            continue
        m_disp = _LIVE_CMD_DISP_RE.search(cmd)
        m_path = _LIVE_CMD_PATH_RE.search(cmd)
        if not (m_disp and m_path):
            continue
        m_size = _LIVE_CMD_SIZE_RE.search(cmd)
        video = Path(m_path.group(1))
        out.append({
            "pid": int(pid_dir.name),
            "display": int(m_disp.group(1)),
            "size": m_size.group(1) if m_size else "1280x720",
            "run": video.parent.parent.parent.name,  # <root>[/footage]/<run>/<arm>/<slot>/dayNN.mp4
            "arm": m_path.group(2),
            "slot": m_path.group(3),
            "day": int(m_path.group(4)),
            "video": str(video),
        })
    return out


def _root_key_of_live_video(video: Path) -> str | None:
    """这段录像落在哪个扫描根（= 哪台机器）下—— 前端按它对回 run 卡。"""
    try:
        v = video.resolve()
    except OSError:
        return None
    for key, root in _results_roots():
        try:
            v.relative_to(root.resolve())
            return key
        except ValueError:
            continue
    return None


def _live_runs() -> dict[str, dict]:
    """run 目录名 → {'root', 'day', 'arms': [{arm, display, slot, size, day, pid}]}。"""
    runs: dict[str, dict] = {}
    for rec in _live_recordings():
        r = runs.setdefault(rec["run"], {"root": None, "day": rec["day"], "arms": []})
        r["day"] = max(r["day"], rec["day"])
        if r["root"] is None:
            r["root"] = _root_key_of_live_video(Path(rec["video"]))
        # pid 顺带当"这一路录制器"的身份：前端拿它做 src 的 nonce —— 录制器重启（新的一天）
        # pid 就变了 → src 变了 → <img> 自动重连，前端不用自己记时间戳
        r["arms"].append({k: rec[k] for k in ("arm", "display", "slot", "size", "day", "pid")})
    for r in runs.values():
        r["arms"].sort(key=lambda a: a["arm"])
    return runs


@app.get("/api/live")
def api_live_runs():
    """本机正在跑（= 有 X 屏可抓）的 run 清单。远端 run 的 X 屏不在这台机器上，不出现在里面。"""
    return {"runs": [dict(run=name, **info) for name, info in sorted(_live_runs().items())]}


@app.get("/api/live/{run}/{arm}/stream")
async def api_live_stream(run: str, arm: str, w: int = _LIVE_WIDTH, fps: int = _LIVE_FPS):
    """一路 MJPEG 直播流（`multipart/x-mixed-replace`），`<img src>` 直接吃。

    从录制器正在抓的那个 Xvfb 屏再抓一路：只读、不动训练产物、也不碰正在写的 mp4。
    客户端断开（关面板/关页面）时 Starlette 会取消这个生成器 → finally 里杀掉 ffmpeg，
    不会留下常驻进程。
    """
    rec = next((r for r in _live_recordings()
                if r["run"] == run and r["arm"] == arm), None)
    if rec is None:
        # 天边界录制器重启的 1.5s 里、或 run 已结束，都会走到这。前端按"这一路没了"处理。
        raise HTTPException(409, f"{run}/{arm} 当前没有在录（天边界重启中或已停跑）")
    width = max(160, min(int(w), 1920))
    fps = max(1, min(int(fps), 10))
    cmd = [
        _LIVE_FFMPEG, "-y", "-loglevel", "error",
        "-f", "x11grab", "-framerate", str(fps), "-video_size", rec["size"],
        "-i", f":{rec['display']}",
        "-vf", f"scale={width}:-1", "-q:v", "5",
        "-f", "mjpeg", "-",
    ]
    boundary = b"octopusliveframe"

    async def frames():
        global _live_stream_count
        with _live_stream_lock:
            if _live_stream_count >= _LIVE_MAX_STREAMS:
                return
            _live_stream_count += 1
        # start_new_session：ffmpeg 自成一个进程组，杀的时候连它自己 fork 的编码线程一起收掉
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                stdin=subprocess.DEVNULL, start_new_session=True)
        loop = asyncio.get_running_loop()
        buf = b""
        last_frame = time.monotonic()
        try:
            while True:
                if time.monotonic() - last_frame > _LIVE_IDLE_TIMEOUT:
                    break
                # read1 而不是 read：来一帧就发一帧，别等缓冲区凑满（直播要的是低延迟）
                chunk = await loop.run_in_executor(None, proc.stdout.read1, 65536)
                if not chunk:
                    break
                buf += chunk
                while True:
                    start = buf.find(b"\xff\xd8\xff")
                    if start < 0:
                        buf = b""
                        break
                    end = buf.find(b"\xff\xd9", start)
                    if end < 0:
                        buf = buf[start:]
                        break
                    frame = buf[start:end + 2]
                    buf = buf[end + 2:]
                    last_frame = time.monotonic()
                    yield (b"--" + boundary + b"\r\n"
                           b"Content-Type: image/jpeg\r\n"
                           b"Content-Length: " + str(len(frame)).encode() + b"\r\n\r\n"
                           + frame + b"\r\n")
        finally:
            # 客户端断开 → 生成器被取消 → 走这里。SIGKILL 给进程组（ffmpeg 输出是管道，
            # 不存在"没写完的录像文件"，不需要给它优雅退出的机会）。
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                proc.kill()
            try:
                proc.stdout.close()
            except OSError:
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            with _live_stream_lock:
                _live_stream_count -= 1

    return StreamingResponse(
        frames(),
        media_type=f"multipart/x-mixed-replace; boundary={boundary.decode()}",
        headers={"Cache-Control": "no-store", "X-Live-Day": str(rec["day"])},
    )


# ── 量化指标（论文主表口径，2026-10-09 加）────────────────────────────────────
# 一个 run 的主表指标（Deaths / Starve / Eq-max / Tool era / Obsidian / Nether /
# Adv's …，即 paper/iclr2027 Table~\ref{tab:main} 那套），前端放在右侧栏的
# 「指标」面板里展示**当前打开的 run**（2026-10-09 用户定案：只统计选中的 run，
# 不做全库表格——全量 80 run 构建要 ~100s，且看的时候一次只关心一条）。
#
# ★ 口径不在这里另写：逐臂指标直接调 experiments/main_table_metrics.arm_metrics（Deaths/
#   Eq-max/Tool era/Obsidian/Nether/XP·life 的唯一固化出处），Adv's 用
#   count_total_advancements 的口径（advancements 显示名去重计数，3 臂均值）——
#   在这里重算一份等于「同一列两种算法」的开端（main_table_metrics docstring 明言要防的坑）。
#   arm_metrics 只吃 run_dir，checkpoint 定位（glob 最新一份）它自己管，与 CLI 完全同源。
#
# ★ 路由与 /scene 同构：book 段是 book_slug，反解出 run 目录（跨机前缀照 _split_slug_root
#   处理）。一次只读一个 run 的 checkpoint（~1s），配 mtime+size 指纹缓存（跑完的 run
#   永久命中；正在跑的 run 指纹变了就重算），不需要全量那套 building/后台构建。
#   建造方块（settlement 表）不进这个接口：那要 NBT 扫末态世界存档（build_metrics_12runs
#   的 strict_counts，分钟级 + 依赖 nbt 库），是离线脚本的事。
_METRICS_ARM_CACHE: dict[str, tuple[tuple, dict]] = {}  # run_dir → (指纹, 逐臂指标)


def _metrics_run_fingerprint(run_dir: Path) -> tuple:
    """run 目录的指纹 = 各臂最新 checkpoint 的 (mtime_ns, size)。checkpoint 是逐窗口
    覆写落盘（文件名带 r<轮>，同轮重跑会原地更新），所以最新那份的 stat 变了 = 指标可能
    变了。没 checkpoint 的 run（还没跑完第一夜）指纹为空元组，每次都会重算——它便宜。"""
    fps = []
    for p in sorted(run_dir.glob("checkpoint_*_r*.json")):
        fp = _stat_fingerprint(p)
        if fp is not None:
            fps.append(fp)
    # 只看最新一份：老的 checkpoint 文件不再变，全部收进指纹只多付 stat 的钱
    by_arm: dict[str, tuple] = {}
    for fp in fps:
        arm = Path(fp[0]).name[len("checkpoint_"):].split("_r")[0]
        by_arm[arm] = fp
    return tuple(sorted(by_arm.values()))


def _adv_count_of(ms: dict) -> int:
    """Adv's 口径（count_total_advancements.arm_adv 同款）：advancements 显示名去重计数。
    采集器已去重，set 一下是防御。base 树计数（Mainl.）论文已删列，不算了。"""
    names = {a.get("advancement") for a in (ms.get("advancements") or [])
             if isinstance(a, dict)}
    names.discard(None)
    return len(names)


def _metrics_row(run_dir: Path, root_key: str | None) -> dict | None:
    """一个 run → 一行指标（逐臂 arm_metrics + Adv's + run 元数据）。没有 checkpoint
    （还没过第一夜/纯 voyager 早期 run）返回 None —— 出现在表格里只会是一排 N/A。"""
    fps = _metrics_run_fingerprint(run_dir)
    hit = _METRICS_ARM_CACHE.get(str(run_dir))
    if hit and hit[0] == fps:
        arms = hit[1]
    else:
        arms = {}
        for arm in main_table_metrics._arms_of(run_dir):
            m = main_table_metrics.arm_metrics(run_dir, arm)
            if m is None:
                continue
            # Adv's 补进来：arm_metrics 不含它（main_table_metrics 的 report 里没这列，
            # 论文数字来自 count_total_advancements）。
            ck = sorted(run_dir.glob(f"checkpoint_{arm}_r*.json"))
            if ck:
                try:
                    d = json.loads(ck[-1].read_text(encoding="utf-8"))
                    m["advs"] = _adv_count_of(d.get("metrics_snapshot") or {})
                except (OSError, json.JSONDecodeError):
                    m["advs"] = None
            else:
                m["advs"] = None
            arms[arm] = m
        _METRICS_ARM_CACHE[str(run_dir)] = (fps, arms)
    if not arms:
        return None
    # —— run 级聚合（口径 = main_table_metrics.report 的打印逻辑，逐列照抄） ——
    rows = list(arms.values())
    n = len(rows)
    deaths = [r["deaths"] for r in rows]
    starve = sum(r["starve"] for r in rows)               # Starve 是 run 级总数（论文口径）
    eqs = [r["eq_max"] for r in rows]
    eq_peak = max(eqs)
    era_rank = {"wooden": 0, "stone": 1, "iron": 2, "dmd.": 3}
    era_best = max((r["era"] for r in rows), key=lambda e: era_rank.get(e, -1))
    obs_nights = [r["obsidian_night"] for r in rows if r["obsidian_night"] is not None]
    neth_nights = [r["nether_night"] for r in rows if r["nether_night"] is not None]
    era_nights = [r["era_night"] for r in rows
                  if r["era"] == era_best and r["era_night"] is not None]
    advs = [r["advs"] for r in rows if r["advs"] is not None]
    xpl = [r["xp_per_life"] for r in rows if r["xp_per_life"] is not None]

    def _mean(xs):
        return round(sum(xs) / len(xs), 1) if xs else None

    return {
        **_run_meta_fields(run_dir, root_key),
        "run_dir": run_dir.name,
        "complete": all(r["complete"] for r in rows),
        "n_nights": max((r["n_nights"] or 0) for r in rows),
        "n_arms_metrics": n,
        # —— 论文 tab:main 的列（↓越小越好 / ↑越大越好，前端照此标箭头） ——
        "deaths": _mean(deaths),
        "starve": starve,
        "eq_max": eq_peak,
        "eq_max_n": sum(1 for e in eqs if e >= eq_peak),
        "era": era_best,
        "era_day": _mean(era_nights),
        "era_n": sum(1 for r in rows if r["era"] == era_best),
        "obsidian_day": _mean(obs_nights),
        "obsidian_n": len(obs_nights),
        "nether_day": _mean(neth_nights),
        "nether_n": sum(1 for r in rows if r["nether"]),
        "advs": _mean(advs),
        # XP/life 论文正文不报（附录 A.5：run 间离散 >> 组间差），但巡检口径一直看它，
        # 放进面板做诊断列不占论文列位。
        "xp_per_life": _mean(xpl),
        "arms": {a: {k: r[k] for k in (
            "deaths", "starve", "xp_per_life", "eq_max", "era", "era_night",
            "obsidian_night", "nether", "nether_night", "advs")}
            for a, r in arms.items()},
    }


@app.get("/api/books/{origin}/{subject}/{book:path}/metrics")
def api_metrics_of_book(origin: str, subject: str, book: str):
    """一条图谱卡（副脑/主脑/planner-only 皆可）所属 run 的主表指标。

    book 段是 book_slug：主脑/冻结卡是 run 目录名，副脑卡是 `<run>__arm_...`（取 __ 前
    的 run 段）；跨机前缀照 _split_slug_root 处理。没有 checkpoint 的 run（还没过第一夜）
    返回 has_metrics=false，前端显示空态而不是一排 N/A。"""
    raw, root = _split_slug_root(book)
    run_slug = raw.partition("__")[0]
    run_dir = root / run_slug
    if not run_dir.is_dir():
        raise HTTPException(404, f"run 目录不存在: {run_slug}")
    # root_key 给 _run_meta_fields 填「来源机器」字段（卡片那套同款；无前缀的旧 slug 为 None）
    root_key = book.partition("/")[0] if "/" in book and book.partition("/")[0] != raw else None
    row = _metrics_row(run_dir, root_key)
    if row is None:
        return {"has_metrics": False}
    return {"has_metrics": True, **row}


# ── 物品/成就清单（2026-10-10 加）────────────────────────────────────────────
# 主表指标回答「这个 run 跑到什么档」，清单回答「每个 agent 具体拿到过哪些物品 / 达成过
# 哪些成就」。展示口径与 results/item_table.html（experiments/item_table_html.py 生成）
# 同一套：物品按 7 类分组 + 中文名，成就按 5 个 tab 分组 + 中文名 + 图标。
#
# ★ 数据口径与 item_table_html._items_by_arm/_advs_by_arm 同源：各臂最新扁平 checkpoint
#   （checkpoint_<arm>_r*.json）metrics_snapshot 里的 items_seen（步末库存累积并集，
#   env.py:3332 更新）与 advancements（显示名带方括号，如 "[Stone Age]"）。
#   成就首达天从 advancement 事件行的 day_idx 取（事件按步记录、day_idx 已带），不需要
#   再像旧 HTML 那样按夜聚合。此处只在 server 侧「读」，不复制 item_table_html 的分组/
#   译名逻辑 —— 那两张表（cats/中文名/tab 归属）从 experiments.item_table_html 直接
#   import 常量（它没 matplotlib 顶层 import，载入无副作用），分类改一处两边一起变。
#   ⚠ 注意 item_table_html._ADV_INFO 是「内部五树名」（Minecraft/Nether/…）与 adv_icons
#   的 <树.名> 命名是两套 key：图标的 key 链 = language.json 标题 → advancements.<key>.title
#   （plot_seed_transfer._adv_key_map 同款），这里照抄这条链，不引入第三种 key。
_INVENTORY_CACHE: dict[str, tuple[tuple, dict]] = {}  # run_dir → (指纹, 清单数据)


def _inventory_row(run_dir: Path, root_key: str | None) -> dict | None:
    """一个 run → 物品/成就清单（逐臂 sets + 表头元数据）。没有 checkpoint 返回 None。"""
    fps = _metrics_run_fingerprint(run_dir)
    hit = _INVENTORY_CACHE.get(str(run_dir))
    if hit and hit[0] == fps:
        return hit[1]
    items_by_arm: dict[str, list[str]] = {}
    advs_by_arm: dict[str, dict[str, int]] = {}   # arm → {显示名: 首达 day_idx}
    for arm in main_table_metrics._arms_of(run_dir):
        cps = sorted(run_dir.glob(f"checkpoint_{arm}_r*.json"))
        if not cps:
            continue
        try:
            ms = json.loads(cps[-1].read_text(encoding="utf-8")).get("metrics_snapshot") or {}
        except (OSError, json.JSONDecodeError):
            continue
        items_by_arm[arm] = sorted(ms.get("items_seen") or [])
        advs: dict[str, int] = {}
        for a in ms.get("advancements") or []:
            if not isinstance(a, dict):
                continue
            name = a.get("advancement")
            if name is None:
                continue
            # 同名多次事件取最早那天（防御：采集器已去重，这里兜底）
            d = a.get("day_idx")
            if name not in advs or (d is not None and (advs[name] is None or d < advs[name])):
                advs[name] = d
        advs_by_arm[arm] = advs
    if not items_by_arm:
        return None
    row = {**_run_meta_fields(run_dir, root_key), "items_by_arm": items_by_arm,
           "advs_by_arm": advs_by_arm}
    _INVENTORY_CACHE[str(run_dir)] = (fps, row)
    return row


def _inv_table_meta() -> dict:
    """清单的表头元数据（前端渲染用）：物品分类表 + 成就信息表。

    进程内只算一次：全是从 item_table_html 常量 + language.json 静态推导，与具体 run 无关。
    物品表：item → {cat, cn}；成就表：显示名 → {cn, tab, icon}（icon = /api/icons/adv/<key>
    的 key，取不到映射的成就 icon 为 None，前端画占位色块）。
    """
    if getattr(_inv_table_meta, "_cache", None) is not None:
        return _inv_table_meta._cache
    # 顶部 `import experiments.main_table_metrics`（sys.path 已插仓根）顺带把 experiments
    # 注册成命名空间包，这里按模块再 import 同一个文件，载入的只有常量定义。
    import viewer._deps.item_table_html as item_table_html

    items = {it: {"cat": cat, "cn": item_table_html._cn_name(it)}
             for cat, its in item_table_html._CAT_ITEMS for it in its}
    # 成就显示名 → icon key：language.json 的 advancements.<key>.title 反查（125/125 都能
    # 映射到 key）。mc_server 装好 node_modules 后该文件在 minecraft-data 包里。
    title2key: dict[str, str] = {}
    lang_p = Path(__file__).resolve().parent.parent / (
        "domains/minecraft/mc_server/node_modules/minecraft-data/minecraft-data"
        "/data/pc/1.21.11/language.json")
    try:
        lang = json.loads(lang_p.read_text(encoding="utf-8"))
        title2key = {v: k[len("advancements."):-len(".title")]
                     for k, v in lang.items()
                     if k.startswith("advancements.") and k.endswith(".title")}
    except (OSError, json.JSONDecodeError):
        pass
    advs = {}
    for disp, (cn, _icon_item, tab) in item_table_html._ADV_INFO.items():
        advs[disp] = {"cn": cn, "tab": tab, "icon": title2key.get(disp.strip("[]"))}
    _inv_table_meta._cache = {"items": items, "advs": advs,
                              "tab_total": item_table_html._ADV_TAB_TOTAL}
    return _inv_table_meta._cache


@app.get("/api/books/{origin}/{subject}/{book:path}/inventory")
def api_inventory_of_book(origin: str, subject: str, book: str):
    """一条图谱卡所属 run 的物品获取 + 成就达成清单（逐臂）。

    路由与 /metrics 同构（book 段是 book_slug、跨机前缀照 _split_slug_root 处理、
    没有 checkpoint 返回 has_inventory=false）。表头元数据只随代码变不随 run 变，
    每次都带回去（前端免再拉一次）。"""
    raw, root = _split_slug_root(book)
    run_slug = raw.partition("__")[0]
    run_dir = root / run_slug
    if not run_dir.is_dir():
        raise HTTPException(404, f"run 目录不存在: {run_slug}")
    root_key = book.partition("/")[0] if "/" in book and book.partition("/")[0] != raw else None
    row = _inventory_row(run_dir, root_key)
    if row is None:
        return {"has_inventory": False}
    return {"has_inventory": True, **row, **_inv_table_meta()}


# 清单图标：results/item_icons（物品，<物品名>.png）与 results/adv_icons（成就，
# <树.名>.png）。目录随仓走、内容静态（从 Minecraft 客户端资源渲染），浏览器长缓存一天即可。
# ⚠ 挂在 /api/icons 而不是根下 StaticFiles：根 "/" 已被前端 dist 占住（html=True 挂载在
#   FRONTEND_DIR.exists() 之后，且先注册的 app.mount("/") 会吞掉后加的路由 —— 见文末
#   mount 顺序），API 前缀不与它抢。
_ITEM_ICONS_DIR = _RESULTS_ROOT / "item_icons"
_ADV_ICONS_DIR = _RESULTS_ROOT / "adv_icons"


@app.get("/api/icons/item/{name}.png")
def api_item_icon(name: str):
    f = _ITEM_ICONS_DIR / f"{name}.png"
    if not f.is_file():
        raise HTTPException(404, f"无物品图标: {name}")
    return FileResponse(f, media_type="image/png", headers={"Cache-Control": "max-age=86400"})


@app.get("/api/icons/adv/{key:path}.png")
def api_adv_icon(key: str):
    f = _ADV_ICONS_DIR / f"{key}.png"
    if not f.is_file():
        raise HTTPException(404, f"无成就图标: {key}")
    return FileResponse(f, media_type="image/png", headers={"Cache-Control": "max-age=86400"})


if FRONTEND_DIR.exists():
    # 入口 HTML 不缓存：StaticFiles 的 etag 只看 (mtime, size)，vite 重建 index.html 时
    # 两者可能都不变 → 浏览器带 If-None-Match 拿 304，继续用旧 HTML 引用旧 hash 的 JS，
    # 新前端永远加载不到（2026-11-11 物品清单图标「改完不生效」就是这个）。带 hash 的
    # JS/CSS 仍走 StaticFiles 的常规缓存，不受影响。
    @app.get("/", include_in_schema=False)
    def api_frontend_index():
        f = FRONTEND_DIR / "index.html"
        if not f.is_file():
            raise HTTPException(404, "前端未构建")
        return FileResponse(f, media_type="text/html", headers={"Cache-Control": "no-store"})

    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")


if __name__ == "__main__":
    import uvicorn
    # reload=True 时必须传导入字符串（"server:app"）而不是 app 对象——reload 靠子进程重新
    # import 模块实现，拿不到内存里的 app。
    #
    # ⚠ reload 默认关：watch 整个目录树（含 node_modules 的数万文件）在网络盘上代价很高。
    # 仅开发改代码时才用 OPENKAGE_VIEWER_RELOAD=1 python server.py 开启。
    reload = os.environ.get("OPENKAGE_VIEWER_RELOAD") == "1"
    uvicorn.run(
        "server:app",
        host="0.0.0.0",
        port=8090,
        reload=reload,
        reload_dirs=[str(Path(__file__).resolve().parent)] if reload else None,
    )
