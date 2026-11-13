"""MC worker 池：每个副脑一台独立 MC server + 一个独立 mineflayer。

每 arm 每轮都要重生世界，而重生要停整个 MC server —— 多副脑共用一台 server 时，一个 arm 的
重生会把其他 arm 的 bot 一起掀翻，且存档互相覆盖。给每个副脑配一台自己的 server + 存档目录，
让「重生」的影响面收缩到单个 worker 内部，`parallel_arms` 才能安全打开，墙钟降到
`epochs × 一条轨迹`。

## worker 目录怎么造

从 `--mc-dir`（**模板**，默认 `/path/to/mc_probe/server121`）**整目录复制**到
`<workers_dir>/w<i>/`，跳过 `world*` / `logs` / `*.log`：
- 存档（`world/`）不拷 —— 每条轨迹开头都 `regenerate_world()` 删存档按固定 seed 重生。
- 其余全拷（`libraries` / `.fabric` / `versions` / jar/properties/**ops.json**）。
- **`ops.json` 必须拷**：bot 是 OP（level 4），`/forceload`、`/spreadplayers`、`/gamerule`
  全靠这个身份，少了它这些命令会**静默失败**（不报错、只是没效果）。

**模板目录全程只读**：用户手工起的那台（:25567）不受影响，worker 端口段刻意错开。
`mc_server/`（含 `node_modules`）**不复制** —— index.js 只读文件系统，N 个 node 进程
共用同一个 cwd，只是 `node index.js <各自端口>`。

## 为什么是池，而不是 `arm[i] → worker[i]` 硬绑

`--mc-workers` 可以小于 arms（起 N 台 JVM 每台 2G，机器不一定给得起）。硬绑的话多余的 arm
没有 worker 可用；池化后它们排队等前面的归还，`--mc-workers 1` 就自动退化成旧的串行行为。

**「一个 world 绝不被两个 rollout 同时用」由队列结构性保证** —— worker 被取走时不在队列里，
`acquire()` 是唯一入口。这条不变量比任何断言可靠，因为它不依赖调用方守规矩。

跨 arm 复用 worker 不破坏隔离：每次 rollout 开头都重生世界，上一个 arm 在这台机器上
砍的树/挖的洞一定被抹掉。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import socket
from dataclasses import dataclass
from pathlib import Path

from .env import MinecraftWorld

logger = logging.getLogger("domain.minecraft.pool")

#: 复制模板目录时跳过的顶层条目。存档不拷（每轮重生）、日志不拷（各 worker 自己写）。
_SKIP_PREFIXES = ("world", "logs")
_SKIP_SUFFIXES = (".log",)


@dataclass
class WorkerSpec:
    """一个 worker 的静态配置：目录 + 两个端口。"""

    index: int
    mc_dir: Path
    mc_port: int
    mineflayer_port: int

    @property
    def mineflayer_url(self) -> str:
        return f"http://127.0.0.1:{self.mineflayer_port}"


def _port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _skip(name: str) -> bool:
    return name.startswith(_SKIP_PREFIXES) or name.endswith(_SKIP_SUFFIXES)


def _set_properties(path: Path, updates: dict[str, str]) -> None:
    """改 server.properties 里的若干 key（其余行原样保留）。

    逐行处理、整文件重写：不引入 properties 解析依赖，也不碰模板里手调过的其他项。
    注意 MC 的 `level-type` 值形如 `minecraft\\:normal`（冒号被转义），逐行不碰它才安全。
    """
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    remaining = dict(updates)
    out = []
    for ln in lines:
        key = ln.split("=", 1)[0] if "=" in ln else None
        if key in remaining:
            out.append(f"{key}={remaining.pop(key)}\n")
        else:
            out.append(ln)
    for key, val in remaining.items():  # 模板里没有这一项 → 追加
        out.append(f"{key}={val}\n")
    path.write_text("".join(out), encoding="utf-8")


def provision_workers(
    base_mc_dir: str | Path,
    workers_dir: str | Path,
    n: int,
    *,
    mc_port_base: int,
    mineflayer_port_base: int,
    world_seed: str,
    difficulty: str = "normal",
    force: bool = False,
) -> list[WorkerSpec]:
    """造 n 份 worker 目录并返回它们的 spec。幂等：目录已存在则不重拷，但**总是**重写
    `server.properties`（端口 + seed）并清空 `openkage_server.log`。

    properties 每次都重写而不是"已存在就信它"：端口/seed 来自命令行，用户换了
    `--mc-port-base` 或 `--world-seed` 之后，复用旧目录会拿着上次的端口起服。

    `openkage_server.log` 每次清空：`_launch_mc_server` 靠「本次启动之后新写入的 `Done (`」
    判就绪（见 env.py 的 log_start_offset）。文件不清也能工作，但跨 run 累积会越来越大。

    端口占用**直接抛**，不静默换端口：换了之后日志里的端口表就是错的，排查时会往错误方向找。
    """
    base = Path(base_mc_dir).resolve()
    root = Path(workers_dir).resolve()
    if not (base / "server.properties").is_file():
        raise FileNotFoundError(
            f"模板 MC 目录不像一个 server：{base} 下没有 server.properties。"
            "--mc-dir 要指向含 server.properties + fabric-server-launch.jar 的目录。"
        )
    if root == base or base in root.parents:
        raise ValueError(
            f"--mc-workers-dir({root}) 不能是模板目录本身或它的子目录 —— "
            "worker 目录会被反复重写，放在模板里会污染模板。"
        )

    specs = [
        WorkerSpec(index=i, mc_dir=root / f"w{i}",
                   mc_port=mc_port_base + i, mineflayer_port=mineflayer_port_base + i)
        for i in range(n)
    ]

    # 端口预检放在复制**之前**：撞端口就没必要花几十秒拷 690M 了。
    busy = [(s.index, p, kind) for s in specs
            for p, kind in ((s.mc_port, "MC"), (s.mineflayer_port, "mineflayer"))
            if _port_in_use(p)]
    if busy:
        detail = "、".join(f"w{i} 的 {kind} 端口 {p}" for i, p, kind in busy)
        raise RuntimeError(
            f"端口已被占用：{detail}。可能是上一次 run 没退干净（`pgrep -f fabric-server-launch`"
            f" / `pgrep -f 'node index.js'` 看一下），或与模板服/别的服务撞段。"
            "换 --mc-port-base / --mineflayer-port-base，或先清掉占用进程。"
        )

    root.mkdir(parents=True, exist_ok=True)
    for spec in specs:
        if force and spec.mc_dir.exists():
            logger.info("[pool] w%d --mc-reprovision：删掉旧目录 %s", spec.index, spec.mc_dir)
            shutil.rmtree(spec.mc_dir, ignore_errors=True)
        if not spec.mc_dir.exists():
            logger.info("[pool] w%d 复制模板 %s → %s（跳过存档/日志）",
                        spec.index, base, spec.mc_dir)
            shutil.copytree(
                base, spec.mc_dir,
                ignore=lambda _d, names: [x for x in names if _skip(x)],
                symlinks=True,
            )
        else:
            logger.info("[pool] w%d 复用已有目录 %s（重写端口/seed）", spec.index, spec.mc_dir)
        # `provision_workers` 每次都重写 server.properties（端口 + seed），**总是**强制
        # allow-flight=true —— 不依赖模板里手调的值（vanilla 默认 false）。
        #
        # 为什么必须 true（dave flying 踢人事故，2026-08-20）：pauseBot() 黎明反思期把
        # bot.physicsEnabled=false 让 bot 悬空（防反思期摔死、防孤儿代码乱动）——这是**我们的
        # 设计意图**，不是作弊。而 allow-flight=false（vanilla 反飞行默认）会把「bot 在我们控制下
        # 短暂悬停」判成飞行外挂，下发 multiplayer.disconnect.flying 把 bot 踢掉 → 重连不恢复
        # chunk → 半连接空转 → 整轮退场。bot 是我们自己进程里的 mineflayer 物理模拟实体，不是需要
        # 防作弊的人类玩家；开启 allow-flight=true 就是承认「这个 server 上只有受控 bot、没有需要
        # 反飞行的人」，让 pauseBot 的悬空副作用合法化。每 arm 独立 MC server，不影响别的 arm。
        # （治本；半连接的兜底校验见 env.py _validate_world_after_heal。）
        _set_properties(spec.mc_dir / "server.properties", {
            "server-port": str(spec.mc_port),
            "level-seed": world_seed,
            "allow-flight": "true",
            # ★ 世界难度挡位（2026-09-09 用户定加）：与 allow-flight 同纪律——每轮强制重写、
            #   不依赖模板里手调的值，使 difficulty 由 yml/命令行唯一真相接管。取值
            #   peaceful/easy/normal/hard（MC 原生）；yml 默认 normal。
            "difficulty": difficulty,
        })
        (spec.mc_dir / "openkage_server.log").write_text("", encoding="utf-8")
        if not (spec.mc_dir / "ops.json").is_file():
            # bot 不是 OP 时 /forceload、/spreadplayers、/gamerule 会**静默失败**
            # （不报错、只是没效果），表现和权限问题看起来毫不相干。
            logger.warning("[pool] w%d 缺 ops.json —— bot 不是 OP，/forceload 等命令会静默失败",
                           spec.index)

    # 这张表是硬杀（SIGKILL）之后手工清进程的唯一线索，必须打。
    logger.info("[pool] %d 个 worker 就绪：", n)
    for s in specs:
        logger.info("[pool]   w%d  MC :%d  mineflayer :%d  %s",
                    s.index, s.mc_port, s.mineflayer_port, s.mc_dir)
    return specs


class MinecraftWorldPool:
    """N 个 `MinecraftWorld` 的独占租借池。

        async with pool.acquire() as world:
            ...   # 这段时间内 world 只属于我

    `asyncio.Queue` 而非信号量+索引：队列把「谁空闲」和「独占」合成一件事 —— 拿走就不在队列
    里，归还才回去。不需要额外的 in-use 集合，也就不存在两者不一致的可能。
    """

    def __init__(self, specs: list[WorkerSpec], *, server_dir: str | Path,
                 java_bin: str | None = None, tick_rate: float = 1) -> None:
        if not specs:
            raise ValueError("worker 池至少要有一个 worker")
        self._specs = list(specs)
        self._worlds = [
            MinecraftWorld(
                s.mineflayer_url, s.mc_port,
                server_dir=str(server_dir), mineflayer_port=s.mineflayer_port,
                mc_dir=str(s.mc_dir), java_bin=java_bin,
                tick_rate=tick_rate,  # ★ OPENKAGE 倍速：透传到每个 worker
            )
            for s in self._specs
        ]
        # Queue 在事件循环外构造是安全的（3.10+ 不再绑定构造时的 loop）。
        self._free: asyncio.Queue = asyncio.Queue()
        for w in self._worlds:
            self._free.put_nowait(w)

    @property
    def size(self) -> int:
        return len(self._worlds)

    @contextlib.asynccontextmanager
    async def acquire(self, label: str = "?"):
        """独占借一个 world，退出时归还（异常路径也归还，否则一次失败会永久少一个 worker）。"""
        if self._free.empty():
            logger.info("[pool] %s 等待空闲 worker（共 %d 个，全部在用）", label, self.size)
        world = await self._free.get()
        idx = self._worlds.index(world)
        logger.info("[pool] %s 占用 w%d（MC :%d）", label, idx, world.mc_port)
        try:
            yield world
        finally:
            self._free.put_nowait(world)
            logger.info("[pool] %s 释放 w%d", label, idx)

    def shutdown_all(self) -> None:
        """停所有 worker：bot + mineflayer 进程 + **MC server**。

        与模板那台不同，worker 的 MC server **必须停**：它是本次 run 的临时资产，不停就是
        纯泄漏。`MinecraftWorld.shutdown` 默认不停 MC server 的那套理由（跨 run 复用的重资产、
        用户开着客户端围观）只对模板那台成立。
        """
        for idx, w in enumerate(self._worlds):
            try:
                w.shutdown(stop_mc=True)
                logger.info("[pool] w%d 已停（bot + mineflayer + MC server）", idx)
            except Exception:  # pylint: disable=broad-except
                # 一个 worker 停不掉不该拖累其他 worker —— 剩下的更要停，否则泄漏更多。
                logger.exception("[pool] w%d 停服失败，可能残留进程（MC :%d）", idx, w.mc_port)
