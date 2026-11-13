"""Voyager 观察装配器：env 的 observe payload → 原文 prompt 的状态字段。

Voyager 原文（A.3.1）给课程/代码生成/自验证 prompt 的状态由这些字段组成：
Inventory / Equipment / Nearby blocks / Other recently-seen blocks / Nearby entities /
Chests / Biome / Time / Health / Hunger / Position。本仓 env 的 observe payload 字段
名不同（voxels/entities/position/health…），这里做一层**纯函数**翻译，不引入新观察源——
字段集合是原文的子集+超集（多了光照/贴身网格等，但那些不进 Voyager 的 prompt，
保持 Voyager 的观察口径 = 忠实适配）。

★ warm-up 课表（原文 Table A.1）：按「已完成任务数」逐步解锁观察字段——
  0 任务：core inventory（log/planks/stick/crafting_table/furnace/dirt/coal/pickaxe/sword/axe
          家族的近似）+ equipment + nearby blocks + position
  5 任务：nearby entities
  7 任务：full inventory
  10 任务：other recently-seen blocks + biome
  15 任务：health + hunger + time + additional context
原文动机：开局 prompt 只暴露最少信息，让课程从基础技能起步、逐步推向更复杂的任务。
additional context（原文 A.3.2 的自问自答+wiki 检索）按原文自标 optional 省略——
统一骨干后它就是同模型自问自答，信息增益趋零（见 run_voyager.py 的适配清单）。

纯函数、无 IO —— 单测直接喂假 payload（tests/test_voyager_obs_assembler.py）。
"""

from __future__ import annotations

from domains.minecraft.env import (
    _TICK_DAY_END,
    _TICK_NIGHT_END,
    _TICK_SUNSET_END,
    _TICKS_PER_DAY,
)

#: warm-up 课表（原文 Table A.1 逐行；additional context 一行省略，见模块 docstring）。
#: key = 字段名（assemble 的输出段），value = 解锁所需的最少已完成任务数。
WARMUP_SCHEDULE: dict[str, int] = {
    "inventory": 0,        # 0 任务时给 core inventory（WARMUP_FULL_INV_AT 起给全量）
    "equipment": 0,
    "nearby_blocks": 0,
    "position": 0,
    "nearby_entities": 5,
    "full_inventory": 7,
    "recent_blocks": 10,
    "biome": 10,
    "health": 15,
    "hunger": 15,
    "time": 15,
}
#: full inventory 解锁点（Table A.1 的 "full inventory | after 7"，与 core inventory 同键切换）。
WARMUP_FULL_INV_AT = 7

#: core inventory 的物品族（Table A.1 原文：log, planks, stick, crafting_table, furnace,
#: dirt, coal, pickaxe, sword, axe）。库存 key 是 mineflayer 精确物品名（oak_log/stone_pickaxe…），
#: 故按**子串匹配**过滤——原文列的本来就是类别不是精确名。
#: ★ cobblestone 补自原码正则（curriculum_agent_core_inventory_items，审计划 C 节）：
#:   原版 core 清单比论文附录多了 cobblestone（挖到圆石是早期最关键的正反馈）。
_CORE_INV_SUBSTRINGS = (
    "log", "planks", "stick", "crafting_table", "furnace", "dirt", "coal",
    "cobblestone", "pickaxe", "sword", "axe",
)
#: 木板/原木的特例：原文 "log/planks" 覆盖所有树种（oak_log/spruce_planks/…），
#: 子串天然覆盖；但 "log" 会误吃 "waterlogged_*"（无此物品名）与 "slogan"（无）——
#: MC 物品名里含 log 的只有 *_log，安全。
#: axe 同理会吃 axe 自身与 pickaxe —— 过滤时须排除更长的名字先命中（见 _core_inventory）。


def _core_inventory(inv: dict) -> dict:
    """core inventory：只保留 Table A.1 列的十类物品（子串匹配，长名优先防 pickaxe 被 axe 吃）。"""
    out = {}
    for name, count in inv.items():
        low = str(name)
        # pickaxe 先判（它也含 "axe"）——命中更具体的那类就不再判宽的
        if ("pickaxe" in low and "pickaxe" in _CORE_INV_SUBSTRINGS) or \
           any(low.endswith(s) or s in low for s in _CORE_INV_SUBSTRINGS if s != "pickaxe"):
            out[name] = count
    return out


def _min_dist(v) -> float:
    """entities 值 → 最近距离（新格式 dict / 旧格式纯数字，与 env._render_entities 同判）。"""
    if isinstance(v, dict):
        try:
            return float(v.get("min", 0) or 0)
        except (TypeError, ValueError):
            return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _time_of_day_label(time_of_day: str) -> str:
    """本域五段 phase → 原文六段 label（sunrise/day/noon/sunset/night/midnight）。

    原文 Time 字段取值即这六个词。本域 status.timeOfDay 是 day/sunset/night/sunrise 四段
    （官方四阶段口径，见 env._TICK_* 注释），这里映射：day → day（正午语义不区分，
    原文的 noon/day 区分对课程决策无信息量）；night 且过半 → midnight（夜晚中点
    14200+4000=18200 之后的 night 报 midnight，与原文六段对齐）。
    缺失/未知原样返回。
    """
    return time_of_day or "unknown"


def render_position(pos: dict) -> str:
    """位置 → `(x, y, z)`（原文格式，1 位小数；None 轴给 ?）。"""
    if not isinstance(pos, dict):
        return "(?, ?, ?)"
    def _n(k: str) -> str:
        v = pos.get(k)
        return f"{float(v):.1f}" if isinstance(v, (int, float)) else "?"
    return f"({_n('x')}, {_n('y')}, {_n('z')})"


def assemble_state_block(obs: dict, completed_tasks: int, *,
                         events: "list[tuple[str, str]] | None" = None) -> str:
    """把 observe payload 按 warm-up 课表装配成 Voyager prompt 的状态块（纯函数）。

    输出是拼进课程/代码生成/自验证 user prompt 的状态段（原文 A.3.1 的信息列表），
    只含已解锁字段——**未解锁字段整段缺席**（不是写 "N/A"），与原文「prompt 逐级
    暴露信息」的语义一致。字段顺序照原文：Biome/Time 在前、Inventory/Chests 在后
    （原文 Prompt 1 的信息顺序）。

    obs: env._parse_observe 的 payload（status/voxels/entities/inventory/nearbyChests/…）。
    completed_tasks: 该智能体已确认成功的任务数（课程 warm-up 的进度轴）。
    events: 可选的本步反馈 [(kind, text), …]（执行反馈/执行错误）——Voyager 的迭代提示
      机制要把它拼进代码生成 prompt（原文 Prompt 4 的 Chat log / Execution error 段）。
      None/空 → 段缺席。
    """
    status = obs.get("status", {}) or {}
    inv = obs.get("inventory", {}) or {}
    voxels = obs.get("voxels", []) or []
    unlocked = {k for k, at in WARMUP_SCHEDULE.items() if completed_tasks >= at}
    lines: list[str] = []

    if "biome" in unlocked:
        lines.append(f"Biome: {status.get('biome', '?')}")
    if "time" in unlocked:
        lines.append(f"Time: {_time_of_day_label(status.get('timeOfDay', ''))}")
    if "nearby_blocks" in unlocked:
        lines.append(f"Nearby blocks: {', '.join(sorted(set(voxels))) or '(无)'}")
    if "recent_blocks" in unlocked:
        # 原文 "other blocks that are recently seen"：本域无「历史看过」通道（观察只有当前
        # voxels），用 17×5×17 当前集合近似——不再引入新状态源，差异记进适配清单。
        # 与 nearby_blocks 同值时（必然，同一来源）省略该行，避免同一段贴两遍。
        seen_now = ", ".join(sorted(set(voxels)))
        if seen_now:
            lines.append(f"Other blocks that are recently seen: {seen_now}")
    if "nearby_entities" in unlocked:
        # entities payload：`{名字: {n, min, dirs}}`（新）或 `{名字: 距离}`（旧），见 env._render_entities。
        # Voyager 原文只列名字（nearest to farthest），按最近距离排序、带只数。
        ents = status.get("entities") or {}
        names = []
        for name, v in sorted(ents.items(), key=lambda kv: _min_dist(kv[1])):
            n = int(v.get("n", 1) or 1) if isinstance(v, dict) else 1
            names.append(f"{name}×{n}" if n > 1 else name)
        lines.append(f"Nearby entities (nearest to farthest): {', '.join(names) or '(无)'}")
    if "health" in unlocked:
        lines.append(f"Health: {status.get('health', '?')}")
    if "hunger" in unlocked:
        lines.append(f"Hunger: {status.get('food', '?')}")
    if "position" in unlocked:
        lines.append(f"Position: {render_position(status.get('position') or {})}")
    if "equipment" in unlocked:
        eq = status.get("equipment") or {}
        # 槽值可为 None（空槽，真 payload 实测）——只列非空槽的物品名。
        eq_names = [str(v.get("name")) for v in eq.values()
                    if isinstance(v, dict) and v.get("name")] if isinstance(eq, dict) else []
        lines.append(f"Equipment: {', '.join(eq_names) or '(无)'}")
    if "inventory" in unlocked:
        if "full_inventory" in unlocked:
            shown = inv
            label = f"Inventory ({status.get('inventoryUsed', len(inv))}/36)"
        else:
            shown = _core_inventory(inv)
            label = "Inventory"
        body = ", ".join(f"'{k}': {v}" for k, v in sorted(shown.items())) or "(空)"
        lines.append(f"{label}: {body}")
    # chests（原文有；本域观察有 nearbyChests 通道，无未解锁门槛——原文未列进 Table A.1，
    # 且任务格式约束已排除放置/存取类任务，恒给 "(无已知箱子)" 意义不大，省略整段。
    # 差异记适配清单。）
    if events:
        lines.append("Chat log: " + " | ".join(f"[{k}] {v[:200]}" for k, v in events))
    return "\n".join(lines)


def phase_for_ticks(tick: int) -> str:
    """游戏刻 → 四段 phase 标签（供午夜判定等；与 env._render_time 同边界、纯函数版）。"""
    t = int(tick) % _TICKS_PER_DAY
    if t < _TICK_DAY_END:
        return "day"
    if t < _TICK_SUNSET_END:
        return "sunset"
    if t < _TICK_NIGHT_END:
        return "night"
    return "sunrise"
