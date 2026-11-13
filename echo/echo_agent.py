"""Adapted Echo agent：CSD 经验记忆 + 五维检索 + ICAL 任务迁移。

官方 Echo 发布件缺少 Mindcraft runtime（见 external-baseline-survey-20261002.md 后续
代码审计），无法原生复现。本模块把 Echo 的机制核心移植到本仓统一执行器：

  · 成功任务 → Contextual State Descriptor（五维）；
  · 新任务前按五维相似度检索 top-k CSD；
  · In-Context Analogical Learning 诱导下一任务与 action guidance。

复用 VoyagerAgent 的迭代代码生成与自验证（同骨干/同动作接口/同反馈回灌），但不使用
Voyager 课程与技能库；任务来源被 CSD→ICAL 闭环替换。所有差异在 run_echo.py 清单列明。
"""

from __future__ import annotations

import json
import logging
import math
import re
from collections import Counter
from pathlib import Path
from typing import Any

from voyager.voyager_agent import VoyagerAgent
from voyager.prompts import PROTOCOL_MISSION_ZH  # ★ 长期目标单一出处（= react.yaml system 节原文）

logger = logging.getLogger("echo")

TEMP_CSD = 0.0
TEMP_ICAL = 0.1

CSD_DIMENSIONS = ("attr", "struct", "func", "proc", "inter")
DEFAULT_WEIGHTS = {d: 1.0 for d in CSD_DIMENSIONS}

SEED_TASKS = (
    ("Get one oak log", "Punch or cut an oak tree until one oak log enters inventory."),
    ("Craft 4 sticks", "Craft planks from logs, then craft at least four sticks."),
    ("Craft a wooden pickaxe", "Craft a crafting table, planks, sticks, then a wooden pickaxe."),
    ("Craft a stone pickaxe", "Mine stone with a wooden pickaxe and craft a stone pickaxe."),
)

#: ★ seed 连续失败上限（2026-10-04 修死循环）：某 seed 反复失败时 n_done 不涨 →
#:   SEED_TASKS[n_done] 永远指向同一任务 → 30 夜死循环。达到上限后跳过该 seed
#:   （n_done 前进一位），全部 seed 用尽仍无成功经验 → 按 ICAL 语义交 LLM 从零提议
#:   （检索不了就类比不了，这是 adapted 版在统一骨干下的必要护栏，记进适配清单）。
#:   ★ 上限取 3（用户 2026-10-04 拍板「抬高重试次数」）：seed 是我们自造的冷启动设计
#:   （官方无此机制），3 次 × 4 轮代码生成 ≈ 12 步/任务——比 Voyager 首任务（5 次）
#:   低，因为 seed 有 4 个、全部卡死最多 4×12=48 步就全部放行交 LLM，不会再绕回。
MAX_SAME_TIER_RETRY = 1   # 同层级替换最多驳回重提 1 次（防死循环、防烧调用）

MAX_SEED_FAILS = 3

#: ★★ 2026-10-25 同义反复护栏（adapted 侧护栏，官方无此机制，须写进适配清单）：
#:   官方 ICAL 只要求「goal 与 anchor 不同」，未要求「更进一步」。实测两条 lane 的 alice
#:   双双陷进零 XP 任务环——anchor 一旦是低价值的石头/木材工艺，后续类比只在**同一材料层级
#:   内部**做 materials_map 替换（andesite→diorite、cobblestone walls→granite walls…），
#:   任务价值永不上升，表现为 XP 长期冻结 + 死亡数被推高。护栏 = 提示词规则 + 确定性驳回。
SAME_TIER_GROUPS = (
    {  # 石头/泥土层级（stone tier 及以下）
        "andesite", "diorite", "granite", "cobblestone", "stone", "gravel", "dirt",
        "polished_andesite", "polished_diorite", "polished_granite", "stone_bricks",
        "cracked_stone_bricks", "mossy_cobblestone", "smooth_stone", "slab", "stairs",
        "wall", "walls", "bricks", "brick",
    },
    {  # 木材层级（wood tier）
        "oak", "birch", "spruce", "acacia", "jungle", "dark_oak", "oak_log", "birch_log",
        "spruce_log", "acacia_log", "jungle_log", "dark_oak_log", "planks", "log", "logs",
    },
)

SAME_TIER_RULE = (
    "\n\nHARD RULE — the new task must ADVANCE beyond the anchor, not just reskin it:\n"
    "REJECTED (do not propose): swapping materials **within the same tier** — e.g."
    " andesite->diorite, cobblestone walls->granite walls, stone slabs->stone stairs,"
    " oak planks->birch planks. These add no new capability and yield no progress.\n"
    "REQUIRED: the new task must do at least ONE of:\n"
    "  1) move up a TIER (wood->stone->iron->diamond; hand->furnace->enchanting->nether),\n"
    "  2) obtain an item / structure / machine the agent does NOT yet have,\n"
    "  3) enter a NEW place (new biome, cave depth, village, nether portal),\n"
    "  4) satisfy a PREREQUISITE the agent currently lacks (fuel, ore, table, armor, food).\n"
    "If you cannot advance, propose the closest task that DOES advance."
)


def _is_same_tier_swap(anchor_task: str, new_task: str) -> bool:
    """确定性驳回：新任务与 anchor 只差「同层级材料词」⇒ 判为同义反复。

    做法：把两个任务文本里的同层级材料 token 全部替换成占位符，若归一化后**完全相同**
    且双方都确实含该层级材料词 ⇒ 只是在同一层级内换皮。只在该精确情形下命中，
    避免误伤正常任务。
    """
    _STOP = {"a", "an", "the", "of", "from", "with", "into", "to", "and", "or", "then",
             "it", "that", "this", "in", "on", "at", "by", "for", "some", "more", "new",
             "existing", "nearby", "flat", "ground", "block", "blocks", "item", "items",
             "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
             "ten", "craft", "mine", "place", "get", "make", "obtain", "using", "use"}

    def norm(t: str, group: set[str]) -> "tuple[str, bool]":
        toks = re.findall(r"[a-z_]+", (t or "").lower())
        hit = False
        out = []
        for w in toks:
            base = w.split("_")[0]
            if w in group or base in group:
                hit = True
                out.append("<M>")
            elif w not in _STOP:      # 冠词/数词/通用动词不参与比较（否则 a vs an 就判不出换皮）
                out.append(w)
        return " ".join(out), hit

    for group in SAME_TIER_GROUPS:
        a_norm, a_hit = norm(anchor_task, group)
        n_norm, n_hit = norm(new_task, group)
        if a_hit and n_hit and a_norm == n_norm:
            return True
    return False


def _bow(text: Any) -> Counter:
    words = []
    for token in str(text or "").lower().split():
        token = "".join(ch for ch in token if ch.isalnum() or ch in ("_", "-"))
        if token:
            words.append(token)
    return Counter(words)


def _cosine(a: Counter, b: Counter) -> float:
    if not a or not b:
        return 0.0
    common = set(a) & set(b)
    num = sum(a[k] * b[k] for k in common)
    da = math.sqrt(sum(v * v for v in a.values()))
    db = math.sqrt(sum(v * v for v in b.values()))
    return num / (da * db) if da and db else 0.0


def _dim_text(csd: dict, dim: str) -> str:
    """把一个 CSD 维度压成检索文本（兼容官方 csd_data.json 的宽松 schema）。"""
    v = csd.get(dim, "")
    if not isinstance(v, dict):
        return str(v or "")
    if dim == "attr":
        return "\n".join(
            f"{k}: color={m.get('color', '')}; shape={m.get('shape', '')}; "
            f"texture={m.get('texture', '')}"
            for k, m in v.items() if isinstance(m, dict))
    return "\n".join(f"{k}: {m}" for k, m in v.items())


def five_dim_similarity(query: dict, csd: dict) -> dict[str, float]:
    """官方 run_icl_flow.py 的五维 BoW cosine（默认等权），逐维返回分数。"""
    return {d: _cosine(_bow(_dim_text(query, d)), _bow(_dim_text(csd, d)))
            for d in CSD_DIMENSIONS}


def retrieve_top_k(csds: list[dict], query: dict, *, top_k: int = 3,
                   weights: dict[str, float] | None = None,
                   exclude_index: int | None = None) -> list[dict]:
    """五维加权检索，按总分排序；空库返回 []。

    ★ `exclude_index`（2026-10-24 改回官方口径）：官方 `run_icl_flow.py` 的
      `retrieve_top_k` 里有 `if tid == anchor_id: continue` —— anchor 自己是 query，
      必须从候选里排除，否则自相似恒满分、top-k 被"刚做完的那条"占满。
      官方靠 task_id 排除，本协议 CSD 以列表存储，按下标排除同一语义。
    """
    weights = {**DEFAULT_WEIGHTS, **(weights or {})}
    scored = []
    for i, c in enumerate(csds):
        if exclude_index is not None and i == exclude_index:
            continue
        sims = five_dim_similarity(query, c)
        score = sum(float(weights.get(d, 0.0)) * s for d, s in sims.items())
        scored.append({"csd": c, "score": score, "dim_scores": sims})
    scored.sort(key=lambda x: (-x["score"], str(x["csd"].get("task", ""))))
    return scored[: max(0, int(top_k))]


def _extract_json(text: str) -> dict | None:
    m = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _csd_query_from_obs(obs: dict) -> dict:
    """当前观察 → 五维检索 query（官方 run_icl_flow.py 的「新任务 CSD」角色，纯函数）。

    官方流程里 query 是**待执行新任务**的五维 CSD（提前由 LLM 描述），本协议任务是
    动态提议的（先检索后提议），拿不到"新任务 CSD"——用**当前处境的五维描述**做
    query：要迁移的是「在什么处境下、哪条成功经验可类比」，处境锚定与官方语义一致。
    数据源只用观察通道已有字段（inventory/voxels/entities），不引入新观察：
      attr   持有物品（颜色/形状未观察，记 present——BoW 检索只吃词形）
      struct 视野方块（空间布局的观察等价物）
      func   实体（环境中的能动对象）
      proc   空（尚未执行，无过程可言——官方 query 此维也为空/由任务给出）
      inter  空（同上）
    """
    inv = obs.get("inventory", {}) or {}
    voxels = obs.get("voxels", []) or []
    status = obs.get("status", {}) or {}
    ents = status.get("entities") or {}
    return {
        "attr": {str(k): {"present": True} for k in list(inv)[:40]},
        "struct": "Observed blocks: " + ", ".join(map(str, list(voxels)[:40])),
        "func": {str(k): {"present": True} for k in list(ents)[:20]},
        "proc": "",
        "inter": "",
    }


def _normalize_csd(obj: dict, task: str) -> dict:
    out: dict[str, Any] = {"task": task}
    for d in CSD_DIMENSIONS:
        v = obj.get(d)
        out[d] = v if isinstance(v, (str, dict, list)) else ""
    return out


def _fallback_csd(task: str, obs: dict, *, code: str | None,
                  events: "list[tuple[str, str]] | None") -> dict:
    """CSD LLM 输出缺失/不合法时的保守降级：不丢成功经验，也不写全空记忆。"""
    inv = obs.get("inventory", {}) or {}
    voxels = obs.get("voxels", []) or []
    event_text = " | ".join(t for _, t in (events or []) if t)
    code_text = (code or "").strip()
    return {
        "task": task,
        "attr": {str(k): {"present": True} for k in list(inv)[:40]},
        "struct": "Observed blocks: " + ", ".join(map(str, list(voxels)[:40])),
        "func": {},
        "proc": f"Successfully completed task: {task}",
        "inter": (f"Execution feedback: {event_text[:2000]}. "
                  f"Successful action summary: {code_text[:4000]}").strip(),
    }


class EchoAgent(VoyagerAgent):
    """一个 adapted Echo 孤岛：成功经验 CSD 化，ICAL 驱动下一任务。"""

    def __init__(self, name: str, model_name: str | None, *,
                 csd_path: "Path | None" = None, top_k: int = 3):
        super().__init__(name, model_name, skill_store=None)
        self.csd_path = Path(csd_path) if csd_path else None
        self.top_k = top_k
        self.csds: list[dict] = []
        self.attempted_tasks: list[str] = []
        self.n_llm_calls.update({"csd": 0, "ical": 0})
        if self.csd_path and self.csd_path.exists():
            try:
                data = json.loads(self.csd_path.read_text(encoding="utf-8"))
                raw = data.get("csds") if isinstance(data, dict) else data
                self.csds = [c for c in (raw or []) if isinstance(c, dict)]
            except Exception:  # pylint: disable=broad-except
                logger.exception("[echo:%s] CSD 文件损坏，从空库启动", name)

    # ---- state / persistence ----
    def state_dict(self) -> dict:
        return {**super().state_dict(), "csds": self.csds,
                "attempted_tasks": list(self.attempted_tasks)}

    def load_state(self, d: dict | None) -> None:
        d = d or {}
        super().load_state(d)
        self.csds = [c for c in (d.get("csds") or []) if isinstance(c, dict)]
        self.attempted_tasks = list(d.get("attempted_tasks") or [])
        self._save_csd_file()

    def _save_csd_file(self) -> None:
        if not self.csd_path:
            return
        self.csd_path.parent.mkdir(parents=True, exist_ok=True)
        self.csd_path.write_text(
            json.dumps({"csds": self.csds}, ensure_ascii=False, indent=2),
            encoding="utf-8")

    # ---- Echo memory-then-transfer loop ----
    async def propose_next_task(self, obs: dict) -> "tuple[str, str] | None":
        """任务来源 = seed 队列（冷启动）→ CSD 检索 + ICAL（有记忆后）。

        ★ 检索 query（★2026-10-24 改回官方口径，覆盖 10-04 版）：
        官方 §3.2 (1) Task Selection = "choose a representative task (e.g. most
        successful or most recently learned)"，代码 `run_icl_flow.py` 默认
        `--anchor_policy latest_success` ⇒ **query 就是「最近一次成功任务的 CSD」**，
        检索时**排除 anchor 自身**（官方 `if tid == anchor_id: continue`），再由 ICAL
        从「anchor + top-K」类比诱导出一个与 anchor 不同的新任务。
        10-04 版改用「当前观察处境」做 query，是担心 anchor 自相似占满 top-k；但官方
        本来就用 anchor 且靠"排除"解决，改处境反而丢掉了类比源——论文报的
        1.3–1.7× 提速与 chain-unlocking 正来自「从最近成功经验做材料/结构类比」
        （官方 prompt 例：copper tools → iron tools）。现按官方实现：anchor =
        csds[-1]，query = anchor，排除 anchor，ICAL 上下文显式带
        "REFERENCE TASK (ANCHOR)" 段并强制 materials_map 类比替换。
        ★ seed 死循环护栏（2026-10-04）：某 seed 失败时 n_done 不涨、永远重出同一任务。
        失败 MAX_SEED_FAILS 次即跳过该 seed；全部 seed 用尽仍无成功经验 → 交 LLM 按
        ICAL 长期目标从零提议（检索段明示库为空，不伪造类比源）。
        """
        n_done = len(self.completed_tasks)
        # ── seed 队列（冷启动）──
        idx = n_done
        while idx < len(SEED_TASKS):
            task, desc = SEED_TASKS[idx]
            if self.failed_tasks.count(task) < MAX_SEED_FAILS:
                return task, desc
            logger.warning("[echo:%s] seed %r 已失败%d 次——跳过该 seed", self.name,
                           task, self.failed_tasks.count(task))
            idx += 1
        if not self.csds:
            return await self._ical_from_scratch(obs)

        try:
            from voyager.state_adapter import assemble_state_block
            state_block = assemble_state_block(obs, n_done)
        except Exception:  # pylint: disable=broad-except
            state_block = json.dumps(obs, ensure_ascii=False, default=str)[:6000]
        # ── 官方 anchor = 最近一次成功经验（latest_success）──
        anchor_idx = len(self.csds) - 1
        anchor = self.csds[anchor_idx]
        hits = retrieve_top_k(self.csds, anchor, top_k=self.top_k,
                              exclude_index=anchor_idx)
        logger.info("[echo:%s] ICAL anchor=%r  检索到 %d 条类比记忆", self.name,
                    str(anchor.get("task", ""))[:60], len(hits))
        history = "\n".join(
            f"- {'DONE' if t in self.completed_tasks else 'FAILED'}: {t}"
            for t in self.attempted_tasks[-30:])
        user = "\n".join(filter(None, [
            "Current state:\n" + (state_block or "(unavailable)"),
            "Recent task history (do not repeat these exact tasks):\n" + history,
            "REFERENCE TASK (ANCHOR) — the most recent success; your new task MUST"
            " differ from it:\n" + json.dumps(anchor, ensure_ascii=False, indent=2),
            "ANALOGOUS EXAMPLES (top-K by five-axis similarity, anchor excluded):\n"
            + json.dumps([{"score": round(h["score"], 3), "dim_scores":
                           {d: round(s, 3) for d, s in h["dim_scores"].items()},
                           "csd": h["csd"]} for h in hits],
                          ensure_ascii=False, indent=2),
            "Propose the next Minecraft task by analogy from the anchor and the"
            " examples: substitute materials / reuse the same procedure on a new"
            " object (e.g. copper tools -> iron tools, oak_log -> birch_log)."
            + SAME_TIER_RULE,
        ]))
        anchor_task = str(anchor.get("task", "") or "")
        async def _ask(extra: str = "") -> "tuple[str, str]":
            self.n_llm_calls["ical"] += 1
            resp = await self._think(
                [{"role": "system", "content": ICAL_SYSTEM},
                 {"role": "user", "content": user + extra}], TEMP_ICAL, kind="ical")
            o = _extract_json(resp) or {}
            return (str(o.get("task") or "").strip(),
                    str(o.get("guidance") or "").strip())

        task, guidance = await _ask()
        # ── 同义反复护栏：最多驳回重提 1 次 ──
        for _ in range(MAX_SAME_TIER_RETRY):
            if not task or not _is_same_tier_swap(anchor_task, task):
                break
            logger.warning("[echo:%s] ICAL 同层级替换驳回 %r（anchor=%r）——要求更进一步后重提",
                           self.name, task[:60], anchor_task[:60])
            task, guidance = await _ask(
                "\n\nYour previous proposal was REJECTED: it only swaps materials within"
                " the same tier (no advance). Propose a DIFFERENT task that advances a"
                " tier, obtains a new item/structure, enters a new place, or fills a"
                " missing prerequisite.")
        if task and task not in self.attempted_tasks:
            return task, guidance
        if task and self.completed_tasks:  # 重复任务：保守重试上一成功任务
            logger.warning("[echo:%s] ICAL 重复任务 %r，重试上一成功任务", self.name, task)
            return self.completed_tasks[-1], f"Repeat successfully: {task}"
        return None

    async def _ical_from_scratch(self, obs: dict) -> "tuple[str, str] | None":
        """无 CSD 记忆时的 ICAL 退化形态：从当前状态 + 长期目标直接提议任务。

        seed 队列用尽且库为空时的唯一出口——返回 None 会触发 run_echo 的固定兜底
        （"Obtain a wood log"），兜底任务再失败时 seed 逻辑又重走一遍，形成外层死循环；
        让 LLM 带长期目标重新提议才有恢复可能（ICAL_SYSTEM 本就携带长期目标协议）。
        """
        try:
            from voyager.state_adapter import assemble_state_block
            state_block = assemble_state_block(obs, len(self.completed_tasks))
        except Exception:  # pylint: disable=broad-except
            state_block = json.dumps(obs, ensure_ascii=False, default=str)[:6000]
        history = "\n".join(
            f"- {'DONE' if t in self.completed_tasks else 'FAILED'}: {t}"
            for t in self.attempted_tasks[-30:])
        user = "\n".join(filter(None, [
            "Current state:\n" + (state_block or "(unavailable)"),
            "Recent task history (do not repeat these exact tasks):\n" + history,
            "No retrieved CSD memories (memory store is empty).",
            "Propose the next Minecraft task that serves the long-term goals.",
        ]))
        self.n_llm_calls["ical"] += 1
        resp = await self._think(
            [{"role": "system", "content": ICAL_SYSTEM},
             {"role": "user", "content": user}], TEMP_ICAL, kind="ical")
        obj = _extract_json(resp)
        task = str((obj or {}).get("task") or "").strip()
        guidance = str((obj or {}).get("guidance") or "").strip()
        if task and task not in self.attempted_tasks:
            return task, guidance
        return None

    async def record_success(self, task: str, obs: dict, *, code: str | None,
                             events: "list[tuple[str, str]] | None") -> dict:
        """成功任务 → 五维 CSD；失败不阻断主循环（不写失败经验，官方语义）。"""
        self.n_llm_calls["csd"] += 1
        user = json.dumps({
            "task": task,
            "successful_action_code": code or "",
            "execution_events": [{"kind": k, "text": t} for k, t in (events or [])][-40:],
            "observation": obs,
        }, ensure_ascii=False, default=str)
        resp = await self._think(
            [{"role": "system", "content": CSD_SYSTEM},
             {"role": "user", "content": user}], TEMP_CSD, kind="csd")
        csd = _normalize_csd(_extract_json(resp) or {}, task)
        if not any(str(csd.get(d) or "").strip() for d in CSD_DIMENSIONS):
            csd = _fallback_csd(task, obs, code=code, events=events)
            logger.warning("[echo:%s] CSD 输出缺失/不合法，已写保守降级 CSD：%s",
                           self.name, task)
        self.csds.append(csd)
        self._save_csd_file()
        logger.info("[echo:%s] CSD 入库（%d 条）：%s", self.name, len(self.csds), task)
        return csd

    def mark_task(self, task: str, success: bool) -> None:
        super().mark_task(task, success)
        if task and task not in self.attempted_tasks:
            self.attempted_tasks.append(task)


CSD_SYSTEM = """You convert one successful Minecraft task execution into a Contextual State Descriptor (CSD).
Return ONLY one JSON object with exactly these keys:
{"attr": {...}, "struct": "...", "func": {...}, "proc": "...", "inter": "..."}
- attr: relevant entities/objects with color, shape, texture, and other physical properties.
- struct: spatial layout, object relations, and scene arrangement.
- func: object roles, affordances, and utility.
- proc: dependencies, state transitions, and operation order.
- inter: executed actions, tool use, and agent-environment interaction routines.
Be concise, factual, and reusable for a different but related task."""

#: ★★ 2026-10-25 协议对齐（与 Voyager 同款，**用户定**）：长期目标**不在此硬编码**，
#:   直接取 `voyager.prompts.PROTOCOL_MISSION_ZH`（其本身是 react.yaml system 节的中文原文）。
#:   理由：本实验比较的是**机制框架**（Echo 的 CSD+ICAL 类比 vs Voyager 的自动课程+技能库），
#:   而"场景/长期目标"属于本实验协议，不是任何外部方法的机制 ⇒ 两条外部基线必须与
#:   OpenKAGE 主实验完全同语义、且**单一出处**，否则会翻译漂移（改版前 Echo 那份就已在
#:   标点和项目符号上与 Voyager 不一致）。保留的仍是 Echo 自己的机制指令（anchor/类比/护栏）。
ICAL_SYSTEM = (
    "你在 Minecraft 中执行 In-Context Analogical Learning，为下一个任务做类比迁移。\n\n"
    + PROTOCOL_MISSION_ZH
    + """

我会给你：当前状态、近期任务历史、**REFERENCE TASK (ANCHOR)**（最近一次成功任务，
你的新任务必须与它不同）、以及按五维相似度检索到的 top-K 类比 CSD 记忆。
请按官方 ICAL 语义做类比迁移：从 anchor 出发，替换材料 / 把同一套流程用到新对象上
（例：copper tools → iron tools，oak_log → birch_log，smelt iron ore → smelt gold ore）。

只返回 JSON：{"task": "...", "guidance": "...", "materials_map": {...}, "analogy_from": "..."}
- task：一个具体、可验证的 Minecraft 任务，**不能与 anchor 或近期历史重复**；用简短英文短语表示。
- guidance：简洁说明步骤/前提，并显式说明它如何由 anchor/类比记忆推得。
- materials_map：本次类比的材料替换（如 {"copper": "iron"}）；无替换时给 {}。
- analogy_from：一句话说明类比自哪条记忆、替换了什么。
任务必须服务上述长期目标；除非确实需要补充资源，不要重复旧工作。
★★ 硬约束：新任务必须**比 anchor 更进一步**，不能只在同一材料层级内换皮
（andesite→diorite、cobblestone walls→granite walls、oak planks→birch planks 均属驳回项）。
推进判定：升一个层级（wood→stone→iron→diamond）／拿到还没有的物品或结构／去新地方／补上缺失前提。
task 中不要提到 Echo、JSON 或本提示词。"""
)
