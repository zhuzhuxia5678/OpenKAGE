#!/usr/bin/env python3
"""探测候选 world_seed 的**自然出生点生物群系**，挑出生在平原（plains）的 seed。

走的是生产默认出生路径：MinecraftWorld.regenerate_world(seed) → reset(None)。
reset(None) = /start 不传 position = bot 落在世界自然出生点（由 level-seed 决定）。
读 observe payload 里 status.biome 即真实出生生物群系。与 run.py --fixed-spawn（默认 True）完全一致。

用法：
  python3 domains/minecraft/probe_spawn_biome.py --seeds 2026 1 2 3
  python3 domains/minecraft/probe_spawn_biome.py --target plains
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

# 走仓库根（脚本在 domains/minecraft/ 下，sys.path[0] 是它），加项目根让 import domains... 可用
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _REPO_ROOT)

from domains.minecraft.worker_pool import provision_workers  # noqa: E402
from domains.minecraft.env import MinecraftWorld  # noqa: E402

# Java 21（env._default_java_bin 也能找，显式钉死避免环境差异）
os.environ.setdefault("JAVA_BIN", "/usr/lib/jvm/java-21-openjdk/bin/java")

TEMPLATE_MC_DIR = "/path/to/mc_probe/server121"


def probe_one(seed: str, world: MinecraftWorld) -> dict:
    """重生世界（按 seed）→ 自然出生点起 bot → 读 biome。返回 {seed, biome, position}。"""
    t0 = time.time()
    if not world.regenerate_world(seed):
        return {"seed": seed, "ok": False, "err": "regenerate_world 失败"}
    obs = world.reset(None)  # 自然出生点
    status = obs.get("status", {}) or {}
    pos = status.get("position", {}) or {}
    biome = status.get("biome", "?")
    result = {
        "seed": seed,
        "ok": True,
        "biome": biome,
        "position": {k: (round(pos[k], 1) if pos.get(k) is not None else None)
                     for k in ("x", "y", "z")} if pos else None,
        "secs": round(time.time() - t0, 1),
    }
    # 收尾：摘掉 bot，准备下一个 seed（regenerate_world 会再 stop，这里先停 node 侧 bot）
    try:
        world.stop()
    except Exception:  # noqa: BLE001
        pass
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seeds", nargs="*", default=["2026"],
                    help="候选 world_seed 列表（str，前导零会保留）")
    ap.add_argument("--target", default="plains",
                    help="目标生物群系名（默认 plains）。命中的 seed 会单独列出")
    ap.add_argument("--mc-port-base", type=int, default=25610,
                    help="worker MC 端口基（避开正式 run 的 25600 段）")
    ap.add_argument("--mineflayer-port-base", type=int, default=3110)
    ap.add_argument("--workers-dir", default="/tmp/openkage_seed_probe")
    ap.add_argument("--mc-dir", default=TEMPLATE_MC_DIR)
    args = ap.parse_args()

    print(f"[probe] 模板={args.mc_dir} worker={args.workers_dir} "
          f"port_base={args.mc_port_base} 目标群系={args.target}", flush=True)

    specs = provision_workers(
        args.mc_dir, args.workers_dir, 1,
        mc_port_base=args.mc_port_base, mineflayer_port_base=args.mineflayer_port_base,
        world_seed=args.seeds[0], force=True,
    )
    spec = specs[0]
    world = MinecraftWorld(
        mineflayer_url=f"http://127.0.0.1:{spec.mineflayer_port}",
        mc_port=spec.mc_port,
        server_dir=os.path.join(_REPO_ROOT, "domains", "minecraft", "mc_server"),
        mineflayer_port=spec.mineflayer_port,
        mc_dir=spec.mc_dir,
    )

    results = []
    try:
        for seed in args.seeds:
            print(f"[probe] === seed={seed!r} 重生世界中（~40s）... ===", flush=True)
            r = probe_one(seed, world)
            results.append(r)
            if r.get("ok"):
                print(f"[probe] seed={seed!r} → biome={r['biome']!r} "
                      f"pos={r['position']} ({r['secs']}s)", flush=True)
            else:
                print(f"[probe] seed={seed!r} 失败：{r.get('err')}", flush=True)
    finally:
        # shutdown(stop_mc=True)：停 bot + 杀托管 mineflayer 进程 + 停 MC server，三件全清。
        # 不能只用 stop()（只发 /stop 给 bot、不杀 node 进程）—— 否则 node index.js 留在端口上，
        # 下次 provision_workers 撞端口报错（第一次跑就因为这样留了孤儿进程）。
        try:
            world.shutdown(stop_mc=True)
        except Exception:  # noqa: BLE001
            pass

    print("\n========== 汇总 ==========")
    for r in results:
        if r.get("ok"):
            mark = "  <-- 命中目标" if r["biome"] == args.target else ""
            print(f"  seed={r['seed']!r:>12}  biome={r['biome']!r}{mark}")
        else:
            print(f"  seed={r['seed']!r:>12}  FAIL {r.get('err')}")
    hits = [r["seed"] for r in results if r.get("ok") and r["biome"] == args.target]
    print(f"\n命中 {args.target!r} 的 seed：{hits if hits else '（无）'}")
    print("\n全部结果 JSON：")
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
