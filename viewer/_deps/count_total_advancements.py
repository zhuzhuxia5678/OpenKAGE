#!/usr/bin/env python3
"""tab:main 补列脚本：输出各 run 的「总成就数」(全部5棵树不重复成就数，3arm均值)。

口径对齐 paper-metrics-recording-methodology（0919）：
  - metrics_snapshot.advancements = list[dict]，取条目['advancement']（显示名，带方括号）
  - 总成就数 = 显示名去重后的数量；Mainl. = 其中 tree=='base' 的条数
本脚本两数都打，便于与主表现有 Mainl. 列交叉校验。

用法：
  python experiments/count_total_advancements.py <run_dir> [<run_dir> ...]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_ARMS = ["alice", "bob", "carol", "dave", "erin"]

# 装备/工具依赖已剥：只 import _ADV_INFO 会连带 domains.minecraft 顶层——
# env.py 顶层无重依赖（仅常量+类定义），直接 import 是安全的。
try:
    from domains.minecraft.env import _ADV_INFO
except Exception as e:  # pragma: no cover
    sys.exit(f"import _ADV_INFO 失败: {e}")

_BASE_NAMES = {name for name, (tree, _) in _ADV_INFO.items() if tree == "base"}


def arm_adv(run_dir: Path, arm: str) -> tuple[int, int] | None:
    """返回 (总成就数, base树成就数)；无 checkpoint 返回 None。"""
    cps = sorted(run_dir.glob(f"checkpoint_{arm}_r*.json"))
    if not cps:
        return None
    d = json.loads(cps[-1].read_text(encoding="utf-8"))
    ms = d.get("metrics_snapshot", {})
    advs = ms.get("advancements") or []
    # 条目是 dict，取 ['advancement'] 显示名；收集器理论上已去重，仍防御性 set 一下
    names = {a.get("advancement") for a in advs if isinstance(a, dict)}
    names.discard(None)
    total = len(names)
    mainl = sum(1 for n in names if n in _BASE_NAMES)
    return total, mainl


def report(run_dir: Path) -> None:
    name = run_dir.name
    per_arm = {a: arm_adv(run_dir, a) for a in _ARMS}
    arms_ok = [a for a in _ARMS if per_arm[a] is not None]
    if not arms_ok:
        print(f"=== {name[:70]}: 无 checkpoint ===")
        return
    totals = [per_arm[a][0] for a in arms_ok]
    mainls = [per_arm[a][1] for a in arms_ok]
    # 完赛判据（入表铁律）：三臂 n_nights_done==30 且 complete==True
    flags = []
    for a in arms_ok:
        cps = sorted(run_dir.glob(f"checkpoint_{a}_r*.json"))
        d = json.loads(cps[-1].read_text(encoding="utf-8"))
        ms = d.get("metrics_snapshot", {})
        nrs = ms.get("night_reflections") or []
        n_done = nrs[-1].get("day_idx") if nrs else None
        flags.append(f"{a}:N{n_done}/{d.get('complete')}")
    mean_t = sum(totals) / len(totals)
    mean_m = sum(mainls) / len(mainls)
    print(f"=== {name[:70]}")
    print(f"    总成就均值={mean_t:.1f}  各臂={totals}")
    print(f"    Mainl.均值={mean_m:.1f}  各臂={mainls}   [{'; '.join(flags)}]")


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit("用法: python experiments/count_total_advancements.py <run_dir> [...]")
    for p in sys.argv[1:]:
        report(Path(p))


if __name__ == "__main__":
    main()
