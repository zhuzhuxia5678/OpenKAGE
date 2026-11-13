"""关键事件录制：训练中常驻一个「观战客户端」，死亡/tier 突破的瞬间抓帧。

## 为什么是这个架构

观战客户端用**独立账号**连服务器（不是冒用 bot 的身份）。这条路是实测选定的：

- 独立账号：服务器主动把 bot 的移动广播给它（`rel_entity_move` 等），画面天然正常。
- 曾试过让客户端用 bot 自己的名字登录以拿到**原生 HUD**（血条/快捷栏），代价是客户端
  与 bot 共用同一玩家实体、却没有位置权威 —— 必须每 300ms 强推 `position` 包纠正它，
  结果是**透视 + 画面持续抖动 + 卡顿**（人工验证确认不可用）。故放弃 HUD 换干净画面。

代价：spectator 模式下**没有** HUD（MC 全部键位里没有"显示被观战者血条"的绑定，已查证；
鼠标中键弹出的是 Spectator Menu 玩家列表，不是 bot 的物品栏）。要在画面上体现 Agent
状态，得后期用 `status` 观察（health/food/inventory 都有）叠字幕，不在本模块职责内。

## 三个必须遵守的约束（都是踩过的坑）

1. **观战账号必须是 OP**，否则 `/gamemode`、`/spectate` **静默失败**（不报错也不生效）。
   离线服 UUID 由名字 md5 决定，所以要连名字带 UUID 一起写进 ops.json（见 `_ensure_op`）。
2. **发命令不能用 `xdotool type`** —— 它打不出 `/`，服务器只收到 `gamemode spectator`
   当成聊天内容，回一句 `<--[HERE]` 语法错。必须走剪贴板 `xclip` + Ctrl+V。
3. **`regenerate_world()` 会重启 MC server**，观战客户端必被踢下线、且重连后旧的附身失效
   （新世界里 bot 是新实体）。**靠调用顺序规避**：一条轨迹一个录制器，起在 `_fresh_world()`
   **之后**（见 `MinecraftDomain.rollout`），生命周期内不再有世界重启 —— 天边界只 `/tick freeze`
   冻结，不动 server。改那个顺序就要把重连+重发 spectate 补回来。

## 失败策略

录制是观测手段，**绝不能成为训练的故障源**。所有对外方法都不抛异常，失败只记 warning
并把自己标记为不可用。训练侧不传 recorder 就是完全的原有行为。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("domain.minecraft.footage")

#: 客户端资源（原版 1.21.11 client.jar + libs + assets + natives）。
#: 放在 /path/to/mc_probe 下而非 /tmp —— /tmp 重启即丢，而这是 190M 的下载物。
DEFAULT_CLIENT_DIR = Path("/path/to/mc_probe/mc_client")
#: MC 1.21 需 Java 21（系统默认 java 是 8，起不来）。与 mc_server/start_servers.sh 同源。
JAVA_BIN = os.environ.get(
    "OPENKAGE_JAVA_BIN",
    "/usr/lib/jvm/java-21-openjdk/bin/java",
)
MC_VERSION = "1.21.11"
ASSET_INDEX = "29"

#: 完整版 ffmpeg（系统源没有 ffmpeg，Playwright 自带的那个是 --disable-everything 构建：
#: 没有 rawvideo/x264/x11grab，不能用）。见 /usr/local/bin/ffmpeg（7.0.2 静态版）。
FFMPEG_BIN = os.environ.get("OPENKAGE_FFMPEG", "/usr/local/bin/ffmpeg")
#: 录像帧率。实测代价（1280×720、crf30、真实游戏画面）：
#:   8fps  -> 7% CPU、11.8 MB/分钟     24fps -> 22% CPU、~25 MB/分钟
#:   15fps -> 14% CPU、~19 MB/分钟     30fps -> 26% CPU、~28 MB/分钟
#: 取 20：客户端 maxFps=60、软件渲染实测单帧 10~50ms，20fps 拿得到真实的新帧（不是复制帧）；
#: 而 3 臂 × 17 小时 ≈ 67GB —— 落在 44T 的挂载上不是问题，CPU 也只占 48 核里的约 1.2 核。
#: 再往 30 推收益递减：bot 大量时间在等待/挖同一个方块，帧间差异实测低到 0.0~0.08%。
REC_FPS = int(os.environ.get("OPENKAGE_REC_FPS", "20"))

#: x264 的 crf（0=无损、51=最差）。**22 是为了看清夜景**：本域最该看的画面（夜里被围攻、
#: 地下挖矿）亮度直方图极窄，crf30 会把承载形状的那几个灰阶并掉、糊成马赛克。
#: 详见 `_start_recording` 里的实测数据与取值理由。要换回省空间的老行为：
#:     OPENKAGE_REC_CRF=30
REC_CRF = int(os.environ.get("OPENKAGE_REC_CRF", "22"))

#: 客户端从启动到进世界的等待上限。软件渲染（llvmpipe，无 GPU）下很慢，实测 ~90s。
CLIENT_READY_TIMEOUT = 150

#: 「客户端已真正进世界」的判据（2026-09-13 二改：`/list` 主动探针 + 每 30s 重发，
#: 回执判据回到 `players online` 前缀）。
#:
#: ★ **为什么必须有主动探针**（0913 晚 21:04/21:22 两 run 5/5 臂全超时的教训）：启动期
#:   **没有任何被动 [CHAT] 来源** —— vanilla 服务器不会在 spec 进服时广播玩家列表；
#:   「自己进服」看不到自己的 joined 广播；bot（mineflayer）在录制器检查**之后**才起。
#:   前一版以为「spec 进服时服务器主动广播 players online」并删掉了 /list，结果冷启动
#:   期一条 [CHAT] 都不会有，5 臂全部白等 150s 超时、整 run 无录像。而 14:51 run 三正常臂
#:   的那条 `players online`，时序上正是旧版 `/list` 的**回执**（advancements @T+0s →
#:   发 /list → 回执 @T+4s），不是什么进服广播。
#: ★ **为什么每 30s 重发**（0913 下午 14:51 run erin/carol 翻车的教训）：旧版只在
#:   「看到 advancements」后发一次 —— 但 advancements 只是资源加载阶段产物，不代表
#:   player spawn 握手完成。客户端进世界慢时那一次 /list 打在加载屏上（聊天框还不存在，
#:   按键全落空），回执永远不来，白等超时。重发让「晚进的客户端」在后续探针轮命中；
#:   真卡死的客户端（两 run 的 erin/carol 日志停在 UBO resizing、无任何进展）重发
#:   也无回执，照旧 150s 超时 —— 重发不掩盖真故障，只救「慢而活」的。
#: ★ 回执文案查**客户端**的 en_us.json（client.jar 的 assets/minecraft/lang/en_us.json）：
#:   回执由客户端本地翻译渲染进聊天框、才落 client.log。1.21.11：
#:     commands.list.players = "There are %s of a max of %s players online: %s"
#:   判据取稳定前缀 "players online"，别拿全句比（人数会变）。
#: ★ `/list` 无副作用，对训练零干扰；spec 是 OP（`_ensure_op`），必然有权限。
#: ★ 仍与画面亮度无关（沿用 0912 核心改进）：暗场景（bot 在地下、spec 复活在井底）不影响，
#:   判的是命令回执，不是抓帧颜色数。
#: ★ 旧 colors 判据（连续 2 帧 colors > 3000 = mesh 建完）降级为**诊断**：0912 它在暗场景
#:   失真（合法暗帧 ~1700 < 3000 误判丢整段录像）。现仅超时 warning 里报最后 colors，区分
#:   「画面黑」和「卡死」，不参与就绪判定。
_LIST_RECEIPT_PREFIX = "players online"
#: `/list` 探针的发送间隔（秒）。`_send_cmd` 本身 ~4.5s（剪贴板+按键+等），150s 超时窗口
#: 内最多发 5 次 —— 首发命中即退（正常臂 ~30s 内），慢而活的臂在后续轮次命中。
_LIST_PROBE_INTERVAL = 30
#: 天边界重连连续失败多少次才真正放弃录制（2026-09-07）。重连失败旧版直接 enabled=False 永久
#: 放弃，一次偶发掉线就丢掉后面所有天录像。改为跳过当天、下天再试；连续失败到本上限才放弃
#: （避免客户端彻底坏了还每天白等 ~40s × MAX 天）。5 次 ≈ 一臂 5 天没录像才放弃，覆盖偶发抖动。
_MAX_RECONNECT_FAILS = 5
#: 抓帧超时。单帧 import 实测 ~20ms，给足余量；超时即放弃这一帧，不拖训练。
SNAP_TIMEOUT = 8
#: 等 bot 连进服务器并附身成功的上限。观战客户端**必然**比 bot 先就绪（见 `_enter_spectate`），
#: 所以这不是异常路径而是常态：bot 要等 `world.reset()` + 出生点预热（实测 12~60s，
#: `prewarm_and_spawn` 最坏重抽 12 次）。给 240s 余量 —— 超时说明 bot 真起不来，
#: 那种情况整条轨迹本来也跑不了。
SPECTATE_TIMEOUT = 240
#: 两次附身尝试之间的间隔。每次 `_send_cmd` 本身已花约 4.5s（剪贴板+按键+等回执）。
SPECTATE_RETRY_WAIT = 5
#: 附身成功的**正面回执**（服务器的权威回答）。判据必须是"它出现了"，不能是"失败文案没出现"——
#: 后者在回执尚未落进 client.log 时（空字符串）会假通过，那正是曾经的 bug。
_SPECTATE_OK = "Now spectating"


#: display 号的分配锁 + 已分配集合。
#: ★ 只看 /tmp/.X<n>-lock 存不存在会**竞态**：多个 arm 并发 start 时，谁都还没来得及创建
#:   lock 文件，于是全都挑中同一个号 —— 实测 3 个 recorder 全拿到 :70，两个 Xvfb 起不来，
#:   三个客户端渲染到同一块屏幕上，抓出来的帧张张都是别人的视角（数据静默错位，最坏的一种）。
_DISPLAY_LOCK = threading.Lock()
_TAKEN_DISPLAYS: set[int] = set()


def _free_display(start: int = 70) -> int:
    """占一个没人用的 X display 号（进程内互斥 + 文件锁双重判据）。"""
    with _DISPLAY_LOCK:
        for n in range(start, start + 60):
            if n in _TAKEN_DISPLAYS:
                continue
            if Path(f"/tmp/.X{n}-lock").exists():
                continue
            _TAKEN_DISPLAYS.add(n)
            return n
    raise RuntimeError("找不到空闲的 X display 号")


def _release_display(n: int | None) -> None:
    if n is None:
        return
    with _DISPLAY_LOCK:
        _TAKEN_DISPLAYS.discard(n)


def _port_alive(port: int, host: str = "127.0.0.1", timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


class FootageRecorder:
    """一个 worker 对应一个录制器：常驻观战客户端 + **按天分段录像** + 按 JS 步落时间索引。

    ## 为什么是全程录像而不是事件抓帧

    截图只能回答"做了什么"，**回答不了"怎么做的"** —— 而后者才是看 Agent 行为的重点。
    所以整条轨迹连续录，再落一份「JS 步 → 视频时间区间」的索引；要看某个 facet 是怎么得
    出的，就用它的来源步跳播那几秒。

    ## 为什么按**天**切文件

    切块的单位（跳播的粒度）是 JS 步，那是索引的事、不需要切文件。**切文件是为了抗损坏**：
    mp4 的 moov 索引写在**尾部**，ffmpeg 没能优雅退出（训练被 kill / 机器挂了）时整个文件
    打不开 —— 不是只丢最后几秒。一条 30 天的轨迹录成单文件，就是把 17 小时的素材押在
    "收尾一定能跑到"上，而 `experiments/run.py` 只有 `finally`、没有信号处理。

    按天切把最坏损失从「整轮」压到「最后一天」。代价接近零：天边界本就是 `/tick freeze`
    的安全点（反思在这里发生，游戏时间停着），重启 ffmpeg 的 ~1.5s 不花游戏时间；分段不重
    编码，总体积与单文件相同。

    ## 文件名必须带 slot（epoch/batch）

    `video/<arm>.mp4` 这种名字在 `--epochs 2` 下会被第 2 轮**覆盖**（ffmpeg 带 `-y`），而
    epoch 正是学习曲线的 X 轴 —— 那等于「跑得越多、留下的素材越少」。所以落盘路径里带
    `slot`（`e1b1` 这类，由 round 号解出，见 `MinecraftDomain._start_recorder`）：

        <out_dir>/<arm>/<slot>/day01.mp4          录像（每天一段）
        <out_dir>/<arm>/<slot>/steps.jsonl        JS 步 → 段内时间区间
        <out_dir>/<arm>/<slot>/facet_steps.jsonl  facet → 来源 JS 步（反思自报）
        <out_dir>/<arm>/<slot>/skill_steps.jsonl  skill → 来源 JS 步（反思自报，键是 slug）

    ## 用法（domain 侧）

        rec = FootageRecorder(out_dir, arm_key="alice", slot="e1b1",
                              mc_port=25600, bot_name="alice")
        rec.start()                       # 同步等客户端就绪（~25-40s，不含附身）
        ...  # 调用方起 bot（world.reset / prewarm_and_spawn）
        rec.attach_and_record()           # bot 上线后：附身 + 开第一天录制（首轮秒成）
        ...  # 每个 JS 步后：rec.mark_step(step=42, day=7, t0=.., t1=.., code=..)
        ...  # 天边界：世界冻结后 rec.end_day() → 反思 → rec.begin_day(day=8) → 解冻
        ...  # 关键事件（可选）：rec.snap("death", day=7, step=42)
        rec.stop()                        # 收尾（先停录像再关客户端，否则尾部损坏）

    **为什么是纯串行（2026-08-20 改）**：`start()` 只到客户端就绪（不碰 bot，同步等无死锁），
    附身+开录推迟到 bot 出生后由 `attach_and_record()` 做。这样 bot 一上线客户端早就绪、附身
    秒成，**录像只丢 ~4.5s 命令时间**。旧并行架构（客户端后台起、与 bot 出生并行）虽省墙钟，
    但 bot 先动、客户端 ~70s 后才就绪，开头 70s 必然录不到 —— 时差根因正是并行。

    **反思期间不录**：`end_day()` 在反思**之前**收段（世界已冻结，那几分钟是静止画面）；
    `begin_day()` 在解冻**之前**开新段。两者之间没有段在录，`elapsed()` 返回 None。

    `enabled=False`（或 start 失败）时所有方法都是 no-op，训练照常跑。
    """

    def __init__(
        self,
        out_dir: str | Path,
        *,
        arm_key: str,
        mc_port: int,
        slot: str = "",
        bot_name: str = "bot",
        mc_dir: str | Path | None = None,
        client_dir: str | Path | None = None,
        width: int = 1280,
        height: int = 720,
        spectator_name: Optional[str] = None,
        initial_day: int = 1,
    ) -> None:
        self.out_dir = Path(out_dir)
        self.arm_key = arm_key
        self.slot = slot or "e1b1"
        self.mc_port = int(mc_port)
        self.bot_name = bot_name
        self.mc_dir = Path(mc_dir) if mc_dir else None
        self.client_dir = Path(client_dir) if client_dir else DEFAULT_CLIENT_DIR
        self.width, self.height = int(width), int(height)
        # 每个 worker 一个独立观战账号：同名会互踢，多 worker 并发时必须错开。
        self.spectator_name = spectator_name or f"spec_{self.mc_port}"
        self.enabled = False
        self._display: int | None = None
        self._xvfb: subprocess.Popen | None = None
        self._client: subprocess.Popen | None = None
        self._gamedir: Path | None = None
        self._lock = threading.Lock()
        self._n_snaps = 0
        #: `(day, step, kind)` → 已抓过几帧。用来给同键的第 2 帧起加序号后缀 —— 一步内可以
        #: 死多次（跨步积压的死亡播报由 env.py 的 `_record_system` 一次性 drain），不带序号
        #: 就会**互相覆写**：events.jsonl 逐条追加、文件只剩最后一帧，前几次死亡的现场静默丢失
        #: （实测 2026-08-13 那轮 180 条事件里 45 帧被这样覆盖，占 25%，且不打任何告警）。
        self._snap_seq: dict[tuple[int, int, str], int] = {}
        self._spectating = False
        #: 是否已把视角切到第三人称。F5 是**三态循环**开关，而视角是客户端渲染设置、不随附身
        #: 丢失而重置 —— 所以整个客户端生命周期只该按一次（见 `_enter_spectate` ③）。
        self._third_person = False
        # ---- 按天分段录像 ----
        #: 本条轨迹的落盘目录：<out_dir>/<arm>/<slot>/。带 slot 才不会被下一个 epoch 覆盖。
        self._seg_dir = self.out_dir / self.arm_key / self.slot
        self._rec: subprocess.Popen | None = None   # 当前那一段的 ffmpeg x11grab 进程
        self._day = int(initial_day)                # 当前正在录的是第几天（续跑传 start_night+1）
        self._t_zero: float | None = None           # **当前段**的起点（段内 elapsed 的基准）
        self._n_steps = 0
        self._n_days = 0                            # 已完成的段数（收尾日志用）
        self._steps_fp = None                       # steps.jsonl 的句柄（追加写）
        self._boot: threading.Thread | None = None  # start_in_background 的启动线程
        # ★ 天边界重连连续失败计数（2026-09-07）：重连失败不再一次永久放弃（旧版 enabled=False
        #   后续天全跳过，alice day07 掉线→day08-21 录像全丢）。改为跳过当天、保持 enabled、
        #   下天再试；连续失败超 _MAX_RECONNECT_FAILS 才真正放弃。客户端偶发 Timed out 后
        #   往往下个天边界就能重起成功，不该因一次失败丢掉后面十几天录像。
        self._reconnect_fails = 0

    # ---------------------------------------------------------------- 生命周期

    def start(self) -> bool:
        """起 Xvfb + 客户端 + **等客户端进世界且 mesh 建完**。返回是否成功（失败不抛）。

        ★ **纯串行流程**（2026-08-20 改）：本方法只到「客户端就绪」就返回 —— **不附身、不开录**。
        附身要等 bot 上线，而 bot 由调用方在本方法返回**之后**才起（`run_exploration` 里
        `world.reset()`/`prewarm_and_spawn`）。所以这里同步等是安全的（不碰 bot，无死锁）；
        附身 + 开录推迟到 bot 出生后由 `attach_and_record()` 做。这样 bot 一上线客户端早就绪、
        附身秒成，录像只丢 ~4.5s 命令时间，而非旧的 ~70s（并行架构下 bot 先动、客户端后就绪）。

        阻塞约 25~40s（软件渲染；advancements + /list 命令回执判就绪，见 `_wait_client_ready`）。
        失败自调 `stop()` 清理已起的 Xvfb/客户端。`enabled` 保持 False —— 由 `attach_and_record` 设。
        """
        try:
            if not self._preflight():
                return False
            self._ensure_op()
            self._display = _free_display()
            self._start_xvfb()
            self._start_client()
            if not self._wait_client_ready():
                self.stop()
                return False
            logger.info("[footage] %s/%s 客户端就绪（MC :%d, display :%d, 观战账号 %s）—— 等 bot 上线后附身",
                        self.arm_key, self.slot, self.mc_port, self._display, self.spectator_name)
            return True
        except Exception:  # pylint: disable=broad-except
            logger.warning("[footage] %s 启动失败，本轮不录制（训练继续）",
                           self.arm_key, exc_info=True)
            self.stop()
            return False

    def attach_and_record(self) -> bool:
        """**bot 上线后**调：附身 bot + 开第一天录制。返回是否成功开录（失败不抛）。

        ★ 纯串行流程的第二阶段（2026-08-20）：`start()` 已让客户端就绪，本方法在 bot 出生
        之后调 —— 此时客户端早就绪、bot 已上线，`_enter_spectate` 首轮即附上（不再像旧并行
        架构那样要轮询等 bot 出现）。附身成功后开录**当前那一天**（`self._day`）那一段，
        再置 `enabled=True`。

        失败不抛（录制绝不成为训练故障源）：附身失败就不录（画面会停在自由视角拍不到 bot，
        那种素材是假的，宁可没有），只记 warning。`start()` 没成功就绪时本方法直接返回 False。
        """
        if self._display is None:
            return False
        try:
            with self._lock:
                # ★ 附身失败就**不录**：画面会停在自由视角、拍不到 bot —— 那种素材看起来完好
                #   （文件在、时长对），内容却是一整天的空地。宁可没有，也不要假的。
                if not self._enter_spectate():
                    logger.warning("[footage] %s/%s 附身 %s 失败，不录制（画面会是空地，宁可没有）",
                                   self.arm_key, self.slot, self.bot_name)
                    return False
                # ★ **不覆盖 `_day`**：非续跑时 `_day` 必为 1；续跑时由 `initial_day` 设成
                #   起点真实天号（start_night+1）。保留这个守卫是为 `begin_day` 万一被提前调过
                #   （它无条件记天号）的兜底。
                self._start_recording()      # 附身完成后才开录，避免开头几秒是主菜单/自由视角
                self.enabled = True
            logger.info("[footage] %s/%s 录制就绪（观战账号 %s，从第%d天起）-> %s",
                        self.arm_key, self.slot, self.spectator_name, self._day, self._seg_dir)
            return True
        except Exception:  # pylint: disable=broad-except
            logger.warning("[footage] %s/%s attach_and_record 异常，本轮不录制（训练继续）",
                           self.arm_key, self.slot, exc_info=True)
            return False

    def start_in_background(self) -> None:
        """在后台线程里跑 `start()`（仅客户端就绪），**立刻返回**。

        ⚠️ 2026-08-20 起 `start()` 不再含附身/开录（拆给 `attach_and_record`），所以"必须异步
        否则死锁"的旧前提已不成立 —— `start()` 现在不碰 bot，可直接同步调。本方法保留给仍想
        把客户端启动放后台的调用方（如 `__enter__` 上下文管理）。**附身仍须由调用方在 bot 上线
        后显式调 `attach_and_record()`**，本方法不替你做。

        起好时 `enabled` 仍为 False（等 `attach_and_record`）；就绪前所有方法 no-op。
        """
        if self._boot is not None:
            return
        self._boot = threading.Thread(
            target=self.start, name=f"footage-boot-{self.arm_key}", daemon=True)
        self._boot.start()

    def _preflight(self) -> bool:
        """依赖齐不齐。缺了就明确说缺什么 —— 别让人对着空目录猜。"""
        missing = [b for b in ("Xvfb", "xdotool", "xclip", "import") if not shutil.which(b)]
        if missing:
            logger.warning("[footage] 缺少命令 %s，不录制（dnf install xorg-x11-server-Xvfb "
                           "xdotool xclip ImageMagick）", ", ".join(missing))
            return False
        if not Path(JAVA_BIN).exists():
            logger.warning("[footage] Java 21 不在 %s，不录制", JAVA_BIN)
            return False
        # ffmpeg 要在这里查：等到 _start_recording 才发现缺，已经白付了 ~90s 的客户端启动。
        # 且必须是**完整版**（要 x11grab）—— Playwright 自带的阉割版没有这个 device。
        if not Path(FFMPEG_BIN).exists() and not shutil.which(FFMPEG_BIN):
            logger.warning("[footage] ffmpeg 不在 %s，不录制", FFMPEG_BIN)
            return False
        try:
            probe = subprocess.run([FFMPEG_BIN, "-hide_banner", "-devices"],
                                   capture_output=True, timeout=20)
            if b"x11grab" not in probe.stdout:
                logger.warning("[footage] %s 不支持 x11grab（可能是阉割构建），不录制", FFMPEG_BIN)
                return False
        except Exception:  # pylint: disable=broad-except
            logger.warning("[footage] ffmpeg 探测失败，不录制")
            return False
        jar = self.client_dir / "client.jar"
        if not jar.is_file():
            logger.warning("[footage] 客户端不在 %s，不录制", jar)
            return False
        if not _port_alive(self.mc_port):
            logger.warning("[footage] MC :%d 不通，不录制", self.mc_port)
            return False
        return True

    def _ensure_op(self) -> None:
        """把观战账号写进 ops.json（name + 由名字算出的离线 UUID）。

        ★ 没有 OP 的话 /gamemode 和 /spectate **静默失败**：不报错、也不生效，
        表现为"画面固定在出生点"，极难查。
        """
        if not self.mc_dir:
            return
        from .env import _offline_uuid  # 复用现成算法，别再实现一遍 md5+版本位

        ops = self.mc_dir / "ops.json"
        try:
            entries = json.loads(ops.read_text(encoding="utf-8")) if ops.is_file() else []
            if not isinstance(entries, list):
                entries = []
        except Exception:  # pylint: disable=broad-except
            logger.warning("[footage] ops.json 解析失败，跳过授权")
            return
        if any(isinstance(e, dict) and e.get("name") == self.spectator_name for e in entries):
            return
        entries.append({
            "uuid": _offline_uuid(self.spectator_name),
            "name": self.spectator_name,
            "level": 4,
            "bypassesPlayerLimit": True,
        })
        try:
            ops.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
            logger.info("[footage] 已把 %s 写入 ops.json", self.spectator_name)
        except Exception:  # pylint: disable=broad-except
            logger.warning("[footage] 写 ops.json 失败 —— /spectate 可能静默失效")

    def _start_xvfb(self) -> None:
        """起 Xvfb 并**确认它真的占住了这块屏**。

        不确认的话，Xvfb 起失败（号被抢/权限问题）时后续 import 会抓到别人的屏幕或空图 ——
        那种错误是静默的：帧照样产出、内容却是错的。宁可这里明确失败。
        """
        self._xvfb = subprocess.Popen(
            ["Xvfb", f":{self._display}", "-screen", "0", f"{self.width}x{self.height}x24"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        env = dict(os.environ, DISPLAY=f":{self._display}")
        for _ in range(20):
            time.sleep(0.5)
            if self._xvfb.poll() is not None:
                raise RuntimeError(f"Xvfb :{self._display} 启动即退出（号可能被抢）")
            # xdpyinfo 未必装；用 xdotool 探一下这块屏能否连上（它是我们本就依赖的命令）
            probe = subprocess.run(["xdotool", "getdisplaygeometry"], env=env,
                                   capture_output=True, timeout=10)
            if probe.returncode == 0:
                return
        raise RuntimeError(f"Xvfb :{self._display} 起了但连不上")

    def _start_client(self) -> None:
        from .env import _offline_uuid

        self._gamedir = self.out_dir / ".client" / self.spectator_name
        self._gamedir.mkdir(parents=True, exist_ok=True)
        # pauseOnLostFocus:false 是无头必须的（没有窗口焦点，否则游戏自动暂停）；
        # panoramaScrollSpeed:0 让主菜单背景静止（否则任何画面差异检测都被它盖掉）。
        #
        # ★ `gamma:1.0` = 亮度滑块推到 **Bright**（原版滑块上限；0.0 是 Moody 最暗）。
        #   **必须显式写**：不写时客户端按自己的默认落成 `gamma:0.5`，而本域主要的观测对象
        #   （夜间被围攻、地下挖矿）恰好全发生在暗处 —— 实测一帧夜景的亮度直方图是
        #   YMIN=17 / YAVG=20.5 / YMAX=84，即**整幅画面挤在 256 级里的 67 级**内，其中
        #   九成像素只占 19~22 这 4 级。人眼看不清，x264 也没有梯度可编，压出来就是一团糊
        #   （2026-08-14 一度被误判成「录像文件坏了」，实际 87 段文件全部完好、只是太黑）。
        #   拉亮等于把同样的信息铺到更宽的亮度区间上，**不花任何码率**。
        #   `darknessEffectScale:0.0` 关掉 1.19+ 幽匿尖啸体那个「视野强制变黑」的屏幕效果，
        #   同理：它只是渲染层的遮蔽，会把本就不多的暗部细节再抹掉一层。
        #   ⚠️ 两项都只影响观战客户端的**渲染**，与 bot 的 observe 无关（light/skyLight 读的是
        #      服务器数据）—— 不改变 Agent 看到的世界，只让录像能看清。
        #
        # ★ `maxFps:20` + `renderDistance:4`（2026-09-06 二次降，原 30/6，再原 60/8）：本机 GPU
        #   是某 C200 **推理卡**（zx-smi 可见、42G 显存），无 OpenGL 图形驱动、容器 /dev/dri
        #   也无 renderD128，MC 渲染只能走 llvmpipe 软件光栅化。5 臂 × `maxFps:60`×`renderDistance:8`
        #   的软件渲染各吃 350%~550% CPU，把 48 核打到 load avg 47，观战客户端因渲染/网络线程长期
        #   不被调度无法回应 server keepalive，被以 `Timed out` 踢下线（2026-09-06 这轮 bob/carol 中招）。
        #   降 CPU 两条刀都在软件渲染上（与帧率近似线性、与 renderDistance 平方相关）：
        #     · maxFps 30→20：对齐 REC_FPS（20）。客户端渲 30、ffmpeg 只取 20，多出的那一半纯浪费 CPU
        #       且拿不到新帧（重复帧）。降到 20 后渲染与采样同频，无白渲。
        #     · renderDistance 6→4：mesh 区块数 ~6²→4²（约 -55%）。代价是观战画面看不远 —— bot 在远处
        #       会出画。观战不需要竞技级视野，且 bot 多在近处作业，可接受。要看远场景靠录像跳播回看。
        #   配合断线重连（见 `_reconnect_client`）兜底。
        (self._gamedir / "options.txt").write_text(
            "pauseOnLostFocus:false\npanoramaScrollSpeed:0.0\n"
            "renderDistance:4\nsimulationDistance:8\ngraphicsMode:1\n"
            "gamma:1.0\ndarknessEffectScale:0.0\n"
            "guiScale:2\nmaxFps:20\nenableVsync:false\nnarrator:0\n"
            "soundCategory_master:0.0\nskipMultiplayerWarning:true\ntutorialStep:none\n"
            # ★ joinedFirstServer:true（2026-09-14 render_build3d 排查加）：quickPlayMultiplayer
            #   在「从未进过任何服务器」的新鲜 gamedir 上**可能静默不触发**——客户端停在
            #   主菜单 panorama（无报错，画面正常有内容，colors 判据看不出），150s 就绪超时。
            #   排查过程中的对照实验（D 例：全新 gamedir+true 也 10s 内 Connecting）说明
            #   它不是唯一的决定变量——当时真正的元凶另有其人（见 render_build3d_real.py
            #   的「用户名 ≤16 字符」注释：MC hello 包对超长用户名直接 EncoderException
            #   "String too big (was 17 characters, max 16)"，连接在协议层就断）。
            #   保留 true：footage 生产 gamedir 跨 run 复用早已是 true（写不写无差），
            #   而任何全新 spectator 目录的首启少一个已知变量，写上只赚不赔。
            "joinedFirstServer:true\n",
            encoding="utf-8",
        )
        libs = sorted((self.client_dir / "libs").glob("*.jar"))
        cp = ":".join([str(self.client_dir / "client.jar"), *map(str, libs)])
        env = dict(os.environ, DISPLAY=f":{self._display}")
        self._client = subprocess.Popen(
            [
                JAVA_BIN,
                f"-Djava.library.path={self.client_dir / 'natives'}",
                # ★ 4G（2026-08-22）：2 倍速长跑下与 MC server 同步调大，5 个 spec 客户端 × 4G
                #   合计 20G，系统 138G 富余。spec 只观战录像、非 OOM 源，不加诊断参数。
                "-Xmx4G", "-cp", cp, "net.minecraft.client.main.Main",
                "--username", self.spectator_name,
                "--uuid", _offline_uuid(self.spectator_name).replace("-", ""),
                "--accessToken", "0", "--version", MC_VERSION,
                "--gameDir", str(self._gamedir),
                "--assetsDir", str(self.client_dir / "assets"),
                "--assetIndex", ASSET_INDEX,
                "--userType", "legacy", "--versionType", "release",
                "--width", str(self.width), "--height", str(self.height),
                "--quickPlayMultiplayer", f"127.0.0.1:{self.mc_port}",
            ],
            cwd=str(self._gamedir), env=env,
            stdout=(self._gamedir / "client.log").open("w"),
            stderr=subprocess.STDOUT,
        )

    def _frame_color_count(self) -> int | None:
        """抓一帧 X 屏，返回画面唯一颜色数（`identify %k`）。

        **仅诊断用**（2026-09-12 起）：`_wait_client_ready` 超时 warning 里报「最后画面
        colors」用 —— 低/None 区分「画面太黑（合法暗场景）」与「客户端卡死/没渲」。
        旧版曾把它当就绪判据（colors > 3000 = mesh 建完），暗场景下误判整段录像被弃，
        见常量区 `_LIST_RECEIPT_PREFIX` 的来龙去脉。

        数值参考（0820 实测）：纯黑/主菜单 colors=1、加载中~200、明亮世界画面>15000、
        gamma 拉满的井底暗画面 ~1700。抓帧失败返回 None。

        ★ **不持 `self._lock`**（2026-09-06 修重连死锁）：本方法只在 `_wait_client_ready`
          里被调，而后者只在客户端**启动/重连**阶段跑——那时 `enabled=False`，`snap`/`mark_step`
          开头就 `if not self.enabled: return`，天然不与本方法并发。原版的 `with self._lock`
          是为防一个不会发生的并发，却让重连路径重入死锁（`begin_day` 持锁 → `_reconnect_client`
          → `_wait_client_ready` → 本方法二次获取不可重入 Lock，同线程永久阻塞；2026-09-06
          dave 天边界掉线触发，卡死 20 分钟、第 3 天起断录）。
          即便真并发：本方法写临时文件 `.ready_probe.png`（用完即删），`snap` 写 `d##_s####.jpg`，
          文件名不撞；`import -window root` 是进程级原子抓帧，无需进程内互斥。故去锁安全。
        """
        if self._display is None:
            return None
        env = dict(os.environ, DISPLAY=f":{self._display}")
        tmp = self._seg_dir / ".ready_probe.png"
        try:
            self._seg_dir.mkdir(parents=True, exist_ok=True)
            r = subprocess.run(
                ["import", "-window", "root", str(tmp)],
                env=env, capture_output=True, timeout=SNAP_TIMEOUT,
            )
            if r.returncode != 0 or not tmp.is_file():
                return None
            out = subprocess.run(
                ["identify", "-format", "%k", str(tmp)],
                capture_output=True, timeout=SNAP_TIMEOUT, text=True,
            )
            return int(out.stdout.strip()) if out.stdout.strip().isdigit() else None
        except Exception:  # pylint: disable=broad-except
            return None
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except Exception:  # pylint: disable=broad-except
                pass

    def _wait_client_ready(self) -> bool:
        """等客户端真正进世界 **且命令回路通**（2026-09-13 二改：探针 + 重发）。

        判据：client.log 里出现 `/list` 的回执（`players online` 前缀）。`/list` 探针
        首发 + 每 `_LIST_PROBE_INTERVAL` 秒重发 —— 启动期没有被动 [CHAT] 来源
        （服务器不广播玩家列表、看不到自己的 joined、bot 还没进服），必须主动探；
        重发救「进世界慢」的臂（首发打在加载屏上落空），真卡死的重发也无回执、
        照旧超时。见常量区 `_LIST_RECEIPT_PREFIX` 的来龙去脉。

        旧 colors 判据降级为诊断：超时 warning 里带上最后几次抓帧的 colors，区分
        「画面太黑」和「客户端卡死」两种失败，不参与就绪判定。
        """
        log = self._gamedir / "client.log" if self._gamedir else None
        deadline = time.time() + CLIENT_READY_TIMEOUT
        # 诊断用：是否已过加载屏（看到 advancements 行）。只进 warning，不影响判定。
        advanced = False
        # 诊断用：最后一次抓帧的 colors（None = 没抓到帧）。只进 warning，不影响判定。
        last_colors: int | None = None
        n_polls = 0
        next_probe_at = 0.0  # time.time() >= next_probe_at 时发下一发 /list
        while time.time() < deadline:
            if self._client and self._client.poll() is not None:
                return False  # 客户端自己退了
            text = ""
            try:
                if log and log.is_file():
                    text = log.read_text(encoding="utf-8", errors="ignore")
            except Exception:  # pylint: disable=broad-except
                pass
            if not advanced and "advancements" in text:
                advanced = True
            # ★ 就绪判据：/list 回执（含重连期 bot join 等任何先落盘的 CHAT 回执场景，
            #   前缀匹配天然覆盖）。到手即退，不再发探针。
            if _LIST_RECEIPT_PREFIX in text:
                logger.info("[footage] %s 客户端就绪（/list 回执到手，命令回路通；"
                            "画面 colors=%s 仅诊断）", self.arm_key, last_colors)
                return True
            # ★ 主动探针：首发立即发，之后每 _LIST_PROBE_INTERVAL 秒重发一发。
            #   进世界慢的臂（首发打在加载屏上）靠后续探针命中；真卡死的重发也
            #   无回执，150s 照旧超时 —— 重发不掩盖真故障，只救「慢而活」的。
            if time.time() >= next_probe_at:
                self._send_cmd("/list")
                next_probe_at = time.time() + _LIST_PROBE_INTERVAL
            # 诊断采样：顺手抓一帧看画面状态（不阻塞、失败即 None）。低频做
            # （每 4 次轮询 1 次），它要 fork import+identify，别在 150s 里跑几十次。
            n_polls += 1
            if n_polls % 4 == 0:
                last_colors = self._frame_color_count()
            time.sleep(2)
        logger.warning("[footage] %s 客户端未在 %ds 内就绪（已过加载屏 advanced=%s，"
                       "/list 探针已发（每 %ds 重发），回执始终未到 —— 多为客户端卡在"
                       "加载末段未真正进世界；最后画面 colors=%s —— 低/None 区分"
                       "「画面太黑」与「卡死」）",
                       self.arm_key, CLIENT_READY_TIMEOUT, advanced,
                       _LIST_PROBE_INTERVAL, last_colors)
        return False

    # ------------------------------------------------------------------ 观战

    def _xdo(self, *args: str) -> None:
        subprocess.run(["xdotool", *args], env=dict(os.environ, DISPLAY=f":{self._display}"),
                       check=False, timeout=15,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _send_cmd(self, cmd: str) -> None:
        """把命令送进聊天框。

        ★ 必须走剪贴板：`xdotool type "/cmd"` 的斜杠会被吞掉，服务器只收到 `cmd`
        当成聊天内容，回一句 `<--[HERE]` 语法错（实测两种 type 写法都失败）。
        """
        env = dict(os.environ, DISPLAY=f":{self._display}")
        subprocess.run(["xclip", "-selection", "clipboard"], input=cmd.encode("utf-8"),
                       env=env, check=False, timeout=10)
        time.sleep(0.4)
        self._xdo("key", "--clearmodifiers", "t")
        time.sleep(1.2)
        self._xdo("key", "--clearmodifiers", "ctrl+v")
        time.sleep(0.8)
        self._xdo("key", "--clearmodifiers", "Return")
        time.sleep(2.0)

    def _chat_log(self) -> str:
        """客户端日志里的 `[CHAT]` 部分 —— 服务器对我们发的命令的**唯一回执**。"""
        if not self._gamedir:
            return ""
        try:
            return (self._gamedir / "client.log").read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return ""

    def _client_disconnected(self) -> bool:
        """观战客户端当前是否已掉线/死亡（停用，天边界要重连）。

        三条判据，任一命中即 True：

        ① **没有 client 进程**（2026-09-12 二补）：`self._client is None`。正常录制生命周期内
          （`start()` 成功到 `stop()`）它恒非 None，为 None 只来自**重连失败后的清理**
          （`_reconnect_client` 的 finally 杀掉刚起的客户端置 None）。此刻确实没有客户端在
          跑 —— 判死，让下个天边界 `begin_day` 再进 `_reconnect_client`（"下天再试"的设计，
          `_skip_day_on_reconnect_fail`）。若不判死：判据③读日志——重连失败那次 client.log
          被 ``"w"`` 截断重写、只有 Connecting 没有 disconnected 行 —— 会误判「活着」，
          从此天天对黑屏开段、重试计数也不再涨（0912 修判据②时差点漏掉这条断链）。
          `stop()` 之后不误触发：它先置 `enabled=False`，`begin_day`/`resync_spectate`
          开头的 enabled 守卫先返回。

        ② **进程已退出**：`self._client.poll() is not None`。客户端会 native crash（glibc
          `corrupted double-linked list` 堆损坏，0912 spec_25601 在 day11 录制中途崩死）——
          崩溃时日志戛然而止、**没有**任何 ``disconnected with reason`` 行，判据③对它返回
          False → `begin_day` 误判「客户端活着」→ 天天对着黑屏开段 + `resync_spectate`
          天天白等 15s，到 run 结束全程黑屏录像。进程 poll 是最硬的判据。

        ③ **日志文本**：client.log 里存在 ``disconnected with reason:`` 行，且它的行号
          **晚于**最后一个 ``Connecting to`` 行 —— 即掉线后没有再重连成功过。覆盖「进程
          活着、停在 Connection Lost 屏」（被 server 踢 Timed out，进程不退出）。单看
          「有没有 disconnected 行」不够：客户端启动时也连过一次（有 Connecting），
          只有「最后一次 Connecting 之后又掉线了」才算当前掉线。

        ★ client.log 每次 `_start_client` 用 ``"w"`` 截断重写（见 `_start_client`），所以重连
          成功后日志是干净的、不含历史掉线行 —— 不会把上一条命周期的掉线误判成当前掉线。
        """
        # ① 没有 client 进程（重连失败被清理）= 死。
        if self._client is None:
            return True
        # ② 进程已退出 = 死。比日志更硬：崩溃/被 kill 都命中，且不需要读文件。
        if self._client.poll() is not None:
            return True
        if self._display is None or not self._gamedir:
            return False
        try:
            lines = (self._gamedir / "client.log").read_text(
                encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            return False
        last_connect = -1
        last_disconnect = -1
        for i, line in enumerate(lines):
            if "Connecting to" in line:
                last_connect = i
            elif "disconnected with reason" in line:
                last_disconnect = i
        return last_disconnect > last_connect

    def _reconnect_client(self) -> bool:
        """天边界用的断线重连：杀掉掉线的客户端进程 → 重起一个 → 等就绪 → 重新附身 bot。

        ## 为什么杀进程重启而不是 UI 点「重新连接」

        掉线后客户端进程**不退出**、停在 "Connection Lost" 屏幕。原版客户端掉线屏的「重新连接」按钮
        坐标随分辨率/guiScale 变，1.21 布局无法在此实测，xdotool 点坐标极易静默点错。而观战账号在
        游戏内**无任何进度**（库存/成就/位置都在 bot 那边），杀掉重起零损失，且复用已验证的
        `_start_client` → `_wait_client_ready` → `_enter_spectate` 干净路径。代价是 ~30~40s 墙钟，
        但只在天边界调（世界冻结，不花游戏时间）。

        ## 只杀客户端、不杀 Xvfb

        Xvfb 还活着、display 号 `self._display` 还占着 —— 复用它，不必重新抢号（抢号有竞态，见
        `_free_display`）。只杀 `self._client`。

        ## 必须重置 `_third_person` 守卫（关键坑）

        `_enter_spectate` 里 F5 受 ``_third_person`` 守卫（「整个客户端生命周期只按一次」），重启 =
        新的生命周期：不重置就不会再按 F5，新客户端视角停在第一人称、看不到 bot 本体。与死亡重附
        ``resync_spectate``（**不**重置、复用旧客户端的视角）恰好相反 —— 这里要新视角。

        失败**不抛**（录制绝不成为训练故障源）：任何步骤炸了都 `finally` 杀掉刚起的客户端防孤儿
        （每个 ~2G RSS），返回 False 让调用方 `enabled = False` 放弃本段。

        ⚠️ 调用方须已持有 `self._lock`（与 `attach_and_record` 同），本方法内含 ~40s 阻塞，但天边界
        世界冻结、无 `mark_step` 并发，持锁可接受。

        ⚠️ **本方法内部不得再调任何持 `self._lock` 的子方法**（`self._lock` 是不可重入
        `threading.Lock`，同线程二次获取会永久死锁）。下游 `_start_client`/`_wait_client_ready`/
        `_enter_spectate` 均不持锁——`_wait_client_ready` 里的 `_frame_color_count` 曾持锁致重连
        死锁（2026-09-06 已去锁，见其 docstring），新增子调用若要持锁须改用独立锁。
        """
        # ① 杀旧客户端（SIGTERM → wait(10s) → kill，与 stop() 同模式），不动 Xvfb。
        if self._client is not None:
            try:
                self._client.send_signal(signal.SIGTERM)
                try:
                    self._client.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self._client.kill()
            except Exception:  # pylint: disable=broad-except
                logger.debug("[footage] %s 重连时杀旧客户端异常", self.arm_key)
            self._client = None
        # ② 重置视角守卫：新客户端是新生命周期，F5 要重新按一次（见 docstring）。
        self._third_person = False
        self._spectating = False
        # ③④⑤ 重起 → 等就绪 → 重新附身。成功置 success=True；失败/异常都让 finally 杀掉已起的
        # 客户端防孤儿（每个 ~2G RSS）。
        success = False
        try:
            self._start_client()
            if not self._wait_client_ready():
                logger.warning("[footage] %s/%s 重连后客户端未就绪，放弃本段", self.arm_key, self.slot)
                return False
            if not self._enter_spectate():
                logger.warning("[footage] %s/%s 重连后重新附身 %s 失败，放弃本段",
                               self.arm_key, self.slot, self.bot_name)
                return False
            success = True
            return True
        except Exception:  # pylint: disable=broad-except
            logger.warning("[footage] %s/%s 重连异常，放弃本段（训练继续）",
                           self.arm_key, self.slot, exc_info=True)
            return False
        finally:
            if not success and self._client is not None:
                # 任何失败（_start_client 之后炸的）：杀掉刚起的客户端，别留孤儿。
                try:
                    self._client.send_signal(signal.SIGTERM)
                    try:
                        self._client.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        self._client.kill()
                except Exception:  # pylint: disable=broad-except
                    pass
                self._client = None

    def _enter_spectate(self, *, timeout: float = SPECTATE_TIMEOUT) -> bool:
        """进 spectator 并附身 bot，切到**第三人称**（bot 本体稳定居中）。返回是否**确认附身**。

        ★★ **必须重试到 bot 真的在线**（2026-08-13 修的一个静默失败）：
        `/spectate <bot>` 在 bot 还没连进服务器时回一句 `No entity was found` —— 命令失败，
        但客户端**照样停在自由视角**、ffmpeg 照样录得到画面。表现是「录了一整天，画面是世界
        原点的一片空地，bot 在 700 格外从未入镜」，而日志里一切正常。

        实测时序（旧并行架构 smoke run）：录制器 00:14:29 就绪 → 发 /spectate 失败 → bot
        00:14:35 才 `joined the game`。那时观战客户端起在 `_fresh_world()` 之后、bot 还没起，
        首次 /spectate 必然赶在 bot 之前。

        **2026-08-20 改纯串行后**：`attach_and_record()` 在 bot 出生**之后**才调，bot 已上线，
        首轮 /spectate 即附上 —— 上面那个"赶在 bot 之前"的时序不再出现。但本方法的**轮询 +
        正面判据仍保留**作为兜底：bot 出生到 `joined the game` 落进 client.log 之间仍有窗口，
        且 `resync_spectate`（死亡重附）复用本方法时 bot 可能刚重生尚未就位。轮询保证这两种
        情况也能等到 `Now spectating`，而非假通过。

        ## 判据：必须等到 `Now spectating`，不能用「没看到失败」代替

        曾经的判据是 `"No entity was found" not in reply` —— 那是把**没看到失败**当成了成功。
        `_send_cmd` 发完只 sleep 2s 就读日志，回执还没落盘时 `reply` 是**空字符串**，空串里
        当然不含失败文案，于是判定成功。改成正面判据（`Now spectating` 出现）+ 轮询等回执。

        ## 为什么必须先把观战者传送到 bot 附近（2026-08-13 实测）

        `/spectate` 在观战者**远离目标**时进的是 `camera follow mode`，而不是正常的第一人称
        附身（[wiki](https://minecraft.wiki/w/Commands/spectate)："If the spectator is away from
        the target, the command will result in a camera follow mode"）。那个模式**不把 bot 放在
        画面中心** —— 人工观看的表现正是「相机长时间不动、Agent 从不居中」。

        而我们**必然**踩中这个条件：观战账号首次登录出生在世界原点，bot 的随机出生点在
        ±128~700 格开外。

        实测（bot 原地跳，量「画面中心区变化 ÷ 边缘区变化」，>2 = bot 稳定居中）：

            远距离直接 spectate：F5×0 → 0.90 ❌     （中心无主体，即人眼看到的现象）
                                 F5×1 → 6.74 / 2.43 / 1.39  ❌ 不稳定，三次里一次失败
            先 tp 到近处再 spectate：F5×0 → 3.10 ✅
                                     F5×1 → 4.63 / 2.26 / 3.34 ✅ 三次全过（均值 3.41）

        所以顺序是 **tp → spectate → F5 一次**。F5 只切第一/三人称、**不会解除附身**
        （曾误判为脱附源头）；但按第 2 次会切进「第三人称正面」，实测 1.24 ❌，故只按一次。
        """
        self._xdo("mousemove", str(self.width // 2), str(self.height // 2), "click", "1")
        time.sleep(1.5)
        self._send_cmd("/gamemode spectator")
        deadline = time.time() + timeout
        attempt = 0
        while time.time() < deadline:
            attempt += 1
            # ① 先把自己传送到 bot 身边 —— 消除 `camera follow mode` 的触发条件（见 docstring）。
            #    按**实体名**传送，不需要知道 bot 在哪：省掉一条「查 bot 坐标」的链路，也就
            #    没有「坐标从哪来、准不准、过期没有」的问题。
            #    bot 不在线时这条同样会回 `No entity was found`，与 ② 的失败判据一致，无需分别处理。
            self._send_cmd(f"/tp {self.spectator_name} {self.bot_name}")
            # ② 附身。判据是**正面回执** `Now spectating`，不是「没看到失败」（见 docstring）。
            before = len(self._chat_log())
            self._send_cmd(f"/spectate {self.bot_name}")
            # 只看**本次命令之后**新增的那段回执：日志是累积的，拿全文判会一直看到上次的结果
            reply = self._chat_log()[before:]
            if _SPECTATE_OK in reply:
                # ③ 切第三人称。**整个客户端生命周期只按一次**（`_third_person` 守卫）：
                #    F5 是三态**循环**开关（第一人称 → 第三人称背后 → 第三人称正面 → 回第一人称），
                #    而视角是客户端的渲染设置、**不随附身丢失而重置**。所以重附身时再按就是把
                #    已经正确的视角切走。
                #    ⚠️ 2026-08-13 人工发现的真实回归：`resync_spectate` 复用本方法，于是每次
                #      死亡重附都多按一次 —— 第 2 次死亡后画面变成第一人称（看不到 bot 本体）。
                #      「只按一次」这条约束本就写在下面的注释里，却因为**复用了整段流程**而破掉。
                if not self._third_person:
                    self._xdo("key", "--clearmodifiers", "F5")
                    self._third_person = True
                    time.sleep(4)
                self._spectating = True
                logger.info("[footage] %s/%s 已附身 %s（第%d次尝试，第三人称跟随）",
                            self.arm_key, self.slot, self.bot_name, attempt)
                return True
            # 还没等到正面回执。两种可能，都靠重试兜住：
            #   · bot 还没连进服务器（`No entity was found`）—— 它正在 world.reset() 里起
            #   · 回执还没落进 client.log（`_send_cmd` 只等 2s）—— 下一轮就能读到
            logger.debug("[footage] %s 未等到「%s」（bot %s 可能还没上线），%ss 后重试",
                         self.arm_key, _SPECTATE_OK, self.bot_name, SPECTATE_RETRY_WAIT)
            time.sleep(SPECTATE_RETRY_WAIT)
        logger.warning(
            "[footage] %s/%s **附身 %s 失败**（%.0fs 内没等到「%s」）—— 画面会停在自由视角、"
            "拍不到 bot，故不录制（宁可没有素材，也不要一整天对着空地的假素材）",
            self.arm_key, self.slot, self.bot_name, timeout, _SPECTATE_OK)
        return False

    def resync_spectate(self, *, reason: str = "死亡后") -> bool:
        """**重新附身 bot**。死亡之后必须调 —— 否则整轮剩余录像都是死亡现场的空镜。

        reason：触发原因，只进日志文案（「死亡后已重新附身」vs「看门狗检测到脱附已重新附身」），
        方便事后从日志分辨是哪条防线起的作用。默认「死亡后」（历史语义），天边界/看门狗调用方
        各自带 reason。

        ## 为什么死亡会解除附身

        MC 原生行为：spectator 附身的实体被移除（**死亡**）时，客户端相机弹回观战者**自己**
        的身体，而那身体停在死亡现场。**没有任何日志或回执**说这件事发生了。

        2026-08-13 实测后果（一轮 3 个 arm 全中）：bot 死后重生回世界原点附近，而相机永远留在
        几百格外的死亡点。诊断时读服务器 `playerdata` 的 NBT `Pos` 发现三个观战账号分别精确
        停在自己那个 bot **首次死亡**的坐标上（误差 ≤0.6 格）。day3~day5 整段录像作废。

        ## 为什么不能靠「画面还在不在动」判断

        ❌ 那个判据会骗人：一个 arm 的相机卡在 y=121 高空，云和昼夜一直在动，freezedetect 算出
        静止率只有 38%、当天落盘 82MB —— 看起来「很有内容」，实际 bot 完全不在镜。唯一可靠的
        判据是坐标/回执。

        失败**不抛**（录制绝不能成为训练的故障源），只记 warning。返回是否重新附身成功。

        ⚠️ 本方法会阻塞约 10~15s（`_send_cmd` 每条约 4.5s，要发 tp + spectate）。调用方应
        放到 `asyncio.to_thread` 里，别压在训练主路径上。
        """
        if not self.enabled or self._display is None:
            return False
        # ★ 客户端已掉线（Timed out）**或已崩死**（native crash，2026-09-12 起判据含进程
        #   poll）时不就地重连：`_send_cmd` 对着断线屏/死进程发命令，聊天框打不开、
        #   必然等满 15s 超时白耗，且动作中途重启客户端会丢录像。跳过，留给下一个天边界的
        #   `begin_day` 做杀进程重连（那里世界冻结、是安全窗口）。
        if self._client_disconnected():
            logger.info("[footage] %s/%s 检测到客户端已掉线/崩死，跳过重附、留待天边界重连",
                        self.arm_key, self.slot)
            return False
        try:
            with self._lock:
                # 复用 _enter_spectate：tp 到 bot 身边 → spectate（**不再动 F5** —— 视角由
                # `_third_person` 守卫，只在客户端启动那次设，见那里的注释）。它内部有重试与正面判据。
                # timeout 给一次尝试的量：这里是热路径（刚死、马上要继续跑），不该长时间阻塞；
                # 万一这次没附上，下一个天边界的 begin_day 还会再兜一次。
                ok = self._enter_spectate(timeout=SPECTATE_RETRY_WAIT * 3)
            if ok:
                logger.info("[footage] %s/%s %s已重新附身 %s",
                            self.arm_key, self.slot, reason, self.bot_name)
            else:
                logger.warning("[footage] %s/%s %s**重新附身失败** —— 这一段画面会停在脱附"
                               "现场（下一个天边界会再试一次）",
                               self.arm_key, self.slot, reason)
            return ok
        except Exception:  # pylint: disable=broad-except
            logger.warning("[footage] %s/%s 重新附身异常（已忽略，训练继续）",
                           self.arm_key, self.slot, exc_info=True)
            return False

    # ⚠️ 这里原有一个 `resync()`（世界重启后重连+重发 spectate），**已删除（2026-08-12）**：
    #    全仓库无调用方。现在一条轨迹一个录制器、起在 `_fresh_world()` 之后，生命周期内不再有
    #    `regenerate_world()`，所以它永远走不到。要恢复的场合只有一种：录制器改成跨轨迹复用
    #    （省掉每轮那 ~90s 客户端启动）—— 那时**必须**重发 spectate，新世界里 bot 是新实体，
    #    不重发画面会停在自由飞行视角、拍不到 bot（见模块 docstring 约束 3）。
    #
    #    ★ 补记（2026-08-13）：删它时的判断「生命周期内没有世界重启」成立，但把**需要重发
    #      spectate**错等价于**世界重启**了 —— 漏掉了「bot 死亡」这条路径（远比世界重启频繁：
    #      实测一夜可死 7 次）。补回来的是上面那个 `resync_spectate()`。

    # ---------------------------------------------------------------- 分段录像

    def _video_path(self, day: int) -> Path:
        """第 `day` 天那一段的落盘路径。零填充到两位 —— 按名字排序就是按天排序。"""
        return self._seg_dir / f"day{int(day):02d}.mp4"

    def _start_recording(self) -> None:
        """开录**当前这一天**那一段（`self._day`）。

        为什么用 x11grab 而不是逐帧 import 再拼：实测 x11grab 只占 9% CPU / 296MB，
        且时长精确；逐帧抓拼要自己控节拍，帧间隔全靠 sleep，长时间录会累积漂移 ——
        而我们要拿时间戳去跳播，漂移直接让索引失准。
        """
        self._seg_dir.mkdir(parents=True, exist_ok=True)
        out = self._video_path(self._day)
        # -g 60 让关键帧密一点，前端 #t= 跳播才跳得准（关键帧太稀会跳到前面一帧）。
        #
        # ★ **crf 22 + aq-mode 3**（原为 crf 30 无 aq）。原注释说"软件渲染细节本就不多，
        #   再高码率只是浪费"—— 那个判断在**白天**成立，在夜里不成立，而夜里恰好是本域
        #   最该看清的时段（被围攻、逃跑、挖洞躲怪）。
        #   夜景的亮度直方图极窄（实测 YMIN=17 / YAVG=20.5 / YMAX=84，九成像素挤在 4 级内），
        #   crf30 在这么窄的带里量化，承载形状的那几级直接被并掉 —— 表现为一团马赛克。
        #   `aq-mode 3` 是**专门偏向暗部**的自适应量化（把码率从已经很亮、看不出差别的区域
        #   挪给暗部），正对这个症状；aq-strength 1.2 比默认 1.0 再偏一点。
        #   实测代价（把夜景归一化曝光后测 SSIM，模拟人眼在暗处的观感）：
        #     crf30 无 aq -> 0.962（当前）    crf23+aq3 -> 0.984
        #     crf26       -> 0.972            crf20+aq3 -> 0.990
        #   体积约 2 倍（本轮 3 臂 30 天 2.5GB -> 约 5GB），落在 27T 空闲的挂载上不是问题；
        #   CPU 几乎不变（实测 2 线程下 11.2x 实时 vs 11.7x，48 核上跑 3 个录制器绰绰有余）。
        #   仍留在 veryfast：慢的 preset 换来的压缩率对"看得清不清"帮助远小于 crf/aq。
        cmd = [
            FFMPEG_BIN, "-y", "-loglevel", "error",
            "-f", "x11grab", "-framerate", str(REC_FPS),
            "-video_size", f"{self.width}x{self.height}", "-i", f":{self._display}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", str(REC_CRF),
            "-aq-mode", "3", "-aq-strength", "1.2",
            "-g", str(REC_FPS * 6), "-pix_fmt", "yuv420p",
            str(out),
        ]
        self._rec = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stdin=subprocess.PIPE,
            # ffmpeg 日志按天追加进一个文件：每段一个 log 会在 30 天的目录里堆 30 个空文件。
            stderr=(self._seg_dir / "ffmpeg.log").open("a"),
        )
        time.sleep(1.5)
        if self._rec.poll() is not None:
            raise RuntimeError(f"ffmpeg x11grab 启动即退出（见 {self._seg_dir / 'ffmpeg.log'}）")
        # ★ **段内相对时间**：每段重置基准，于是 mark_step 落的 t0/t1 直接就是**这一段**
        #   视频里的秒数，前端 `#t=` 拿去就能用，不需要知道前面几天各录了多久。
        #   用 monotonic：墙钟被 NTP 调整时不会让索引错乱。
        self._t_zero = time.monotonic()
        if self._steps_fp is None:
            self._steps_fp = (self._seg_dir / "steps.jsonl").open("a", encoding="utf-8")

    def end_day(self) -> None:
        """收掉当天那一段。**在反思开始前调**（世界刚冻结）。

        为什么在这里收而不是等反思完：反思是几分钟的 LLM 墙钟，而世界是冻结的 —— 那几分钟
        录下来是**一动不动的静止画面**，白占体积、还让"这一天的录像"长度与游戏内容不成比例
        （实测反思一次数十秒到几分钟，而一天的有效动作可能才十几秒）。

        ★ **不动 `self._day`**：反思紧接着就要调 `mark_facet_source`，那里靠 `self._day`
        判定这批 facet 属于哪一天 —— 现在收的是"刚过去那天"的段，天号必须还是它。
        推进天号的是 `begin_day()`。

        失败**不抛**（录制绝不能成为训练的故障源），但会把自己标成不可用。
        """
        if not self.enabled:
            return
        try:
            with self._lock:
                # ★ 只有当天真开了段才计数 + 收段（2026-09-07）：begin_day 重连/开段失败会跳过
                #   当天（`_rec` 保持 None），那种天不该 `_n_days += 1`，否则收尾日志的天数比
                #   实际 mp4 多。`_stop_segment` 见 `_rec is None` 即 return，安全。
                had_segment = self._rec is not None
                self._stop_segment()
                if had_segment:
                    self._n_days += 1
            if had_segment:
                logger.info("[footage] %s/%s 第%d天录完（反思期间不录：世界冻结、画面静止）",
                            self.arm_key, self.slot, self._day)
            else:
                logger.info("[footage] %s/%s 第%d天未开段（重连/开段失败跳过），无段可收",
                            self.arm_key, self.slot, self._day)
        except Exception:  # pylint: disable=broad-except
            self.enabled = False
            logger.warning("[footage] %s/%s 收第%d天的段失败，停止录制（训练继续）",
                           self.arm_key, self.slot, self._day, exc_info=True)

    def begin_day(self, *, day: int) -> None:
        """开录第 `day` 天那一段。**在解冻前调**（反思已结束、天号已递增）。

        放在解冻**之前**：起 ffmpeg 要约 1.5s，冻结时游戏时间停着，这段空白不对应任何游戏
        内容；放到解冻之后就会真丢掉一秒多的动作。

        ★ **天号无条件记下**（即使还没就绪）：后台启动可能跨过一个天边界才完成，那时
        `enabled` 还是 False、这次调用不会真开段 —— 但 `_day` 必须已经是最新的，否则启动
        完成时会拿"第1天"去开段，而世界已经在第2天了（索引与画面错位，且只在
        `mark_step` 的天号不一致 warning 里露出蛛丝马迹）。

        ★★ **天边界也是断线重连的唯一窗口**（2026-09-06 补）：观战客户端被 server 以 `Timed out`
        踴下线后进程不退、停在 "Connection Lost" 屏幕，此刻世界冻结、bot 不动，是杀进程重启的
        安全窗口（动作中途重启会丢录像）。开新段**之前**先检测连接，掉了就 `_reconnect_client`
        重起 + 重新附身，再开段 —— 否则新段会全程录断线静止画面。

        ★★★ **重连失败不再永久放弃**（2026-09-07 改）：旧版重连/开段失败一次就 `enabled=False`，
        后续天全跳过——alice day07 掉线重连失败后 day08-21 录像全丢，但训练正常跑完。改为**跳过
        当天开段、保持 enabled、下天再试**（`_reconnect_fails` 计数），连续失败到 `_MAX_RECONNECT_FAILS`
        才真正放弃。理由：客户端偶发 Timed out 后往往下个天边界就能重起成功，`_client_disconnected`
        判据幂等（重连成功后 client.log 被 ``"w"`` 截断重写，不卡历史状态），没有东西阻止后续天重试。
        跳过的天 `_rec`/`_t_zero` 仍为 None → `end_day`/`mark_step`/`snap` 见 None 即 return，
        不计数、不写索引，安全。
        """
        self._day = int(day)          # ← 先于 enabled 判断：见上面那段
        if not self.enabled:
            return
        # ★ 断线重连（仅天边界）：开新段前先确认连接活着，掉了/进程崩死就重起客户端再开段。
        if self._client_disconnected():
            logger.warning("[footage] %s/%s 观战客户端已掉线/崩死，天边界重启客户端重连",
                           self.arm_key, self.slot)
            try:
                with self._lock:
                    reconnected = self._reconnect_client()
            except Exception:  # pylint: disable=broad-except
                logger.warning("[footage] %s/%s 重连异常，第%d天不录制（训练继续）",
                               self.arm_key, self.slot, self._day, exc_info=True)
                self._skip_day_on_reconnect_fail()
                return
            if not reconnected:
                self._skip_day_on_reconnect_fail()
                return
            # 重连成功：清零连续失败计数，继续开段。
            self._reconnect_fails = 0
            logger.info("[footage] %s/%s 重连成功，继续录第%d天", self.arm_key, self.slot, self._day)
        try:
            with self._lock:
                self._start_recording()
            # 开段成功也算从重连失败中恢复：清零计数（开段失败与重连失败共用同一上限）。
            self._reconnect_fails = 0
            logger.info("[footage] %s/%s 开始录第%d天", self.arm_key, self.slot, self._day)
        except Exception:  # pylint: disable=broad-except
            # 开段失败：同样跳过当天、下天再试，不一次永久放弃（与重连失败同策略）。
            self._skip_day_on_reconnect_fail()
            logger.warning("[footage] %s/%s 开录第%d天失败，跳过本天（训练继续）",
                           self.arm_key, self.slot, day, exc_info=True)

    def _skip_day_on_reconnect_fail(self) -> None:
        """重连/开段失败后的统一处理：跳过当天开段，连续失败到上限才真正放弃。

        跳过当天 = 不调 `_start_recording`，`_rec`/`_t_zero` 保持 None → `end_day` 的
        `_stop_segment`（`_rec is None` 即 return）不崩、`mark_step`/`snap`（`_t_zero is None`
        即 return）不写索引。连续失败计数 +1，达 `_MAX_RECONNECT_FAILS` 才 `enabled=False`。
        """
        self._reconnect_fails += 1
        if self._reconnect_fails >= _MAX_RECONNECT_FAILS:
            self.enabled = False
            logger.warning("[footage] %s/%s 连续 %d 天重连/开段失败，放弃录制（训练继续）",
                           self.arm_key, self.slot, self._reconnect_fails)
        else:
            logger.warning("[footage] %s/%s 第%d天不录制，跳过本天待下天重试"
                           "（连续失败 %d/%d）",
                           self.arm_key, self.slot, self._day,
                           self._reconnect_fails, _MAX_RECONNECT_FAILS)

    def elapsed(self) -> float | None:
        """从**当前段**起点到现在的秒数（= 这一天那段视频里的时间点）。没在录返回 None。

        反思期间（`end_day` 之后、`begin_day` 之前）也返回 None —— 那段时间没有段在录，
        调用方（env 的 `_t0`）据此跳过写索引。
        """
        if self._t_zero is None or not self.enabled:
            return None
        return round(time.monotonic() - self._t_zero, 2)

    def mark_facet_source(self, concept_slug: str, kind: str, steps: list[int],
                          *, op: str = "set", new_slug: str = "") -> None:
        """落一行 facet 来源步的**事件**（反思 Agent 自报的），供人回看那几步的录像。

        **有意不写进 facet 表**：视频是观测产物，不该混进 Agent 的记忆结构。
        `(concept_slug, kind)` 在 facet 表里本就唯一，足够当外部映射的键。

        ★ 这是一份**追加写的事件日志**，不是最终状态 —— 读的一侧按顺序回放得到当前映射
        （见 `viewer.server._replay_facet_steps`）。理由：反思可以改（update_facet）、删
        （del_facet / del_concept）和改名（update_concept / merge_concepts）自己写过的 facet，
        而一条 facet 被推翻之后，旧的来源步**指向的是被推翻的那个说法** —— 留着它会让人点开
        视频看到与正文矛盾的画面，还以为是记忆出了错。纠错自愈是本实验的主张之一，这条链路
        上不能有这种假证据。追加而不原地改：这里是训练热路径，重写整个文件既慢、又可能在
        训练被 kill 时留下半个文件。

        ★★ `day` 由**录制器**填（`self._day` = 刚录完/正在录的那一天），不由调用方给：
        反思发生在 `end_day()` **之后**、`begin_day()` **之前**，而 `end_day` 有意不推进天号 ——
        所以此刻 `self._day` 正是这批 facet 所属的那一天。让上游传等于把同一个事实存两份。

        `op`：`set` 覆盖、`drop` 失效（kind 为空 = 该概念全部）、`move_slug` 迁键。
        """
        if not self.enabled:
            return
        try:
            rec: dict[str, Any] = {"op": op, "concept_slug": concept_slug, "kind": kind}
            if op == "set":
                rec["day"], rec["steps"] = self._day, list(steps)
            elif op == "move_slug":
                rec["new_slug"] = new_slug
            with self._lock:
                with (self._seg_dir / "facet_steps.jsonl").open("a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception as e:  # pylint: disable=broad-except
            logger.warning("[footage] %s/%s 写 facet 映射失败（%s %s/%s）: %s",
                           self.arm_key, self.slot, op, concept_slug, kind, e)

    def mark_skill_source(self, slug: str, steps: list[int],
                          *, op: str = "set") -> None:
        """落一行**技能**来源步的事件（反思 Agent 自报），供人回看技能诞生那几步的录像。

        与 `mark_facet_source` 同构，差别仅在键：facet 是 `(concept_slug, kind)`（一条概念可有
        多种 kind 的 facet），skill 是单个 `slug`（技能无 kind、slug 不可变）。故：
        - `op` 只有 `set`（覆盖该 slug 的来源步）和 `drop`（delete_skill 时失效）；
        - 没有 `move_slug`（skill 的 slug 改不了，`update_skill` 不收 slug 参数）。

        ★ `update_skill` **不碰**来源步（用户定案）：来源步记录的是「这个技能最初产自哪几步」
          的历史溯源，改代码不清也不覆盖——录像入口始终指向技能诞生现场，而非当前版本。

        ★★ `day` 由**录制器**填（`self._day`），理由同 `mark_facet_source`：反思发生在
          `end_day()` 之后、`begin_day()` 之前，此刻 `self._day` 正是这批技能所属的那一天。

        追加写 `skill_steps.jsonl`（事件日志，非最终状态）——读的一侧按序回放得到当前映射
        （见 `viewer.server._replay_skill_steps`）。删除技能后旧的来源步指向的是已被抹除的
        技能，留着它点开录像会看到与技能库对不上的画面，故 drop 要落。
        """
        if not self.enabled:
            return
        try:
            rec: dict[str, Any] = {"op": op, "slug": slug}
            if op == "set":
                rec["day"], rec["steps"] = self._day, list(steps)
            with self._lock:
                with (self._seg_dir / "skill_steps.jsonl").open("a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception as e:  # pylint: disable=broad-except
            logger.warning("[footage] %s/%s 写 skill 映射失败（%s %s）: %s",
                           self.arm_key, self.slot, op, slug, e)

    def mark_step(self, *, step: int, day: int, t0: float, t1: float,
                  code: str = "", position: Any = None) -> None:
        """落一行「**JS 步** → 段内视频时间区间」的索引。

        step 必须是 JS 步号（一次 write_code），不是 ReAct 轮 —— 见类 docstring。

        ★ `day` 由调用方传（env 侧的 `day_idx`），但**落盘用的是 `self._day`**：两者本该
        相等，不等就说明 `begin_day` 漏调或差了一天，而那正是「点开第 3 天看到第 4 天画面」
        这类静默错位的来源。所以不信任参数、只信自己在录哪一段，并把不一致**报出来**。

        ★★ **没有段在录就不写**（`_t_zero is None`，即反思窗口内）：那时 t0/t1 没有对应的
        视频可言，写下去就是一行指向不存在画面的索引。调用方（env 的 `_t0`）本来也会拿
        `elapsed()` 挡一道，但这个判据该由录制器自己守 —— 索引与段的对应关系是它的不变式。
        """
        if not self.enabled or self._steps_fp is None or self._t_zero is None:
            return
        try:
            if int(day) != self._day:
                logger.warning(
                    "[footage] %s/%s step%d 报的是第%d天，但当前在录第%d天 —— 按在录的那天记；"
                    "多半是 begin_day 漏调（会让索引与画面错位，查天边界处理）",
                    self.arm_key, self.slot, step, day, self._day)
            with self._lock:
                self._steps_fp.write(json.dumps({
                    "day": self._day, "step": int(step),
                    "t0": round(float(t0), 2), "t1": round(float(t1), 2),
                    # 代码原文截断：索引文件要能被前端整体读进来，一步几 KB 的代码会把它撑爆
                    "code": (code or "")[:300],
                    "position": position,
                }, ensure_ascii=False) + "\n")
                self._steps_fp.flush()   # 训练可能被 kill，不 flush 就丢整段索引
                self._n_steps += 1
        except Exception as e:  # pylint: disable=broad-except
            logger.warning("[footage] %s/%s 写步索引失败（step%d）: %s",
                           self.arm_key, self.slot, step, e)

    def _stop_segment(self) -> None:
        """停当前那一段的 ffmpeg。**必须给它优雅退出的机会**，否则 mp4 尾部索引没写完、
        整个文件打不开（不是只丢最后几秒）—— 这也正是按天切段要防的那件事。"""
        if self._rec is None:
            return
        try:
            if self._rec.poll() is None:
                # 'q' 是 ffmpeg 的正常停止指令；SIGTERM 也行但 'q' 更干净
                try:
                    self._rec.stdin.write(b"q")
                    self._rec.stdin.flush()
                except Exception:  # pylint: disable=broad-except
                    self._rec.send_signal(signal.SIGINT)
                try:
                    self._rec.wait(timeout=25)
                except subprocess.TimeoutExpired:
                    logger.warning("[footage] %s/%s ffmpeg 未在 25s 内退出，强杀"
                                   "（第%d天那一段的尾部可能损坏）",
                                   self.arm_key, self.slot, self._day)
                    self._rec.kill()
        except Exception:  # pylint: disable=broad-except
            logger.debug("[footage] 停录像异常")
        self._rec = None
        self._t_zero = None

    def _stop_recording(self) -> None:
        """收尾：停最后一段（若还在录）+ 关索引句柄。

        `_rec is None` 是正常情况而非异常：最后一夜的段已由 `end_day()` 在反思前收掉，
        且那之后不再 `begin_day()`。这个判据同时防了 `_n_days` 重复计数。
        """
        if self._rec is not None:
            self._stop_segment()
            self._n_days += 1
        if self._steps_fp is not None:
            try:
                self._steps_fp.close()
            except Exception:  # pylint: disable=broad-except
                pass
            self._steps_fp = None

    # ------------------------------------------------------------------ 抓帧

    def snap(self, kind: str, *, day: int, step: int, meta: dict[str, Any] | None = None) -> None:
        """抓一帧并追加一条 events.jsonl。任何失败都只记 warning，不影响训练。

        死亡事件要**同步**抓（MC 死后立刻重生，晚一步现场就没了）。单帧 ~20ms。

        与录像同住 `<arm>/<slot>/`：一条轨迹的全部观测产物在一个目录里，删一轮素材就是删
        一个目录，也不会被下一个 epoch 覆盖（`events.jsonl` 此前在 out_dir 根上按 arm 字段
        区分，两轮的事件会混在同一个文件里）。

        ## 同一 `(day, step, kind)` 抓多帧

        文件名从第 2 帧起带序号（`..._death.jpg`、`..._death_2.jpg`）。**一步多帧是常态**：
        跨步积压的死亡播报由 env.py 的 `_record_system` 在步末一次性 drain，一个 600s 的
        `waitUntil` 步里死 4 次就会连抓 4 帧。不带序号时它们互相覆写，而 events.jsonl 照旧
        逐条追加 —— 前端于是拿 N 条不同死因的记录去指同一张图（还都用 `file` 当 v-for 的 key）。

        ⚠️ 这几帧画面**高度相似**（实测相邻帧 RMSE 只差 7~9%）：抓帧发生在 step 返回之后、
        `resync_spectate` 之前，相机还停在最后一次死亡点，早先那几次的现场其实已经没了。
        序号只保证「不静默丢数据」，要真看清每次死亡得靠录像跳播（见 `mark_step` 的索引）。
        """
        if not self.enabled:
            return
        try:
            with self._lock:
                key = (int(day), int(step), kind)
                n = self._snap_seq.get(key, 0) + 1
                self._snap_seq[key] = n
                suffix = "" if n == 1 else f"_{n}"
                rel = f"d{day:02d}_s{step:04d}_{kind}{suffix}.jpg"
                self._seg_dir.mkdir(parents=True, exist_ok=True)
                # quality 95（原 88）：死亡帧多半是**夜景**，实测均值只有 16/255，jpeg 在这么
                # 暗的画面上产生的块效应特别显眼（暗部本就只有几个灰阶，见 _start_recording）。
                # 单帧从约 90KB 涨到约 150KB，一轮总共几十张 —— 相对 GB 级的录像可以忽略。
                # 真正的亮度问题由客户端 `gamma:1.0` 解决（同一块 X 屏，录像/抓帧一起受益）。
                subprocess.run(
                    ["import", "-window", "root", "-quality", "95",
                     str(self._seg_dir / rel)],
                    env=dict(os.environ, DISPLAY=f":{self._display}"),
                    check=True, timeout=SNAP_TIMEOUT,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                rec = {"day": day, "step": step, "kind": kind,
                       "file": rel, "ts": time.time()}
                # 同键第 n 帧：显式落进 events.jsonl，前端/分析侧不用靠解析文件名反推。
                if n > 1:
                    rec["seq"] = n
                if meta:
                    rec.update({k: v for k, v in meta.items() if k not in rec})
                with (self._seg_dir / "events.jsonl").open("a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                self._n_snaps += 1
        except Exception as e:  # pylint: disable=broad-except
            logger.warning("[footage] %s 抓帧失败（%s d%d s%d）: %s",
                           self.arm_key, kind, day, step, e)

    # ------------------------------------------------------------------ 收尾

    def stop(self) -> None:
        # ★ 先等启动线程落定（`start_in_background` 可能还在起客户端）：不等就会**边起边关** ——
        #   我们关掉的是它当时还没创建的进程，随后它继续把 Xvfb/客户端/ffmpeg 起起来，
        #   谁都不再管它们（每个 MC 客户端约 2G RSS）。轨迹早早失败时就是这个情形。
        #   join 有上限：启动最坏 CLIENT_READY_TIMEOUT + SPECTATE_TIMEOUT，给点余量。
        #
        #   ⚠️ **要排除自己**：`start()` 的失败路径自己就会调 `stop()`，而它跑在这个线程里 ——
        #      join 自己会抛 `RuntimeError: cannot join current thread`（已实测），
        #      那会把「启动失败」变成「启动失败 + 收尾也炸」。
        _boot = self._boot
        if (_boot is not None and _boot.is_alive()
                and _boot is not threading.current_thread()):
            logger.info("[footage] %s 启动尚未完成，等它落定再收（避免留下孤儿进程）",
                        self.arm_key)
            _boot.join(timeout=CLIENT_READY_TIMEOUT + SPECTATE_TIMEOUT + 60)
            if _boot.is_alive():
                logger.warning("[footage] %s 启动线程未在预期内结束 —— 可能留下 Xvfb/客户端，"
                               "需人工查 `pgrep -af net.minecraft.client`", self.arm_key)
            self._boot = None
        # ★ 顺序关键：**先停录像，再关客户端/Xvfb**。反了的话 ffmpeg 的输入源（X 屏）
        #   先消失，它来不及写 mp4 的尾部索引 —— 整个文件会打不开（不是只丢最后几秒）。
        self._stop_recording()
        self.enabled = False
        for proc, name in ((self._client, "客户端"), (self._xvfb, "Xvfb")):
            if not proc:
                continue
            try:
                proc.send_signal(signal.SIGTERM)
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
            except Exception:  # pylint: disable=broad-except
                logger.debug("[footage] 停 %s 失败", name)
        self._client = self._xvfb = None
        _release_display(self._display)   # 归还号，供后续轮次/arm 复用
        self._display = None
        if self._n_snaps or self._n_steps:
            # glob 对不存在的目录返回空（不抛）—— 启动失败时 _seg_dir 可能压根没建过，
            # 但那种情况 _n_steps/_n_snaps 都是 0，走不到这里。
            mb = sum(p.stat().st_size for p in self._seg_dir.glob("day*.mp4")) / 1048576
            logger.info("[footage] %s/%s 收尾：%d 天分段（共 %.1f MB）、%d 个 JS 步索引、"
                        "%d 张抓帧 -> %s",
                        self.arm_key, self.slot, self._n_days, mb, self._n_steps,
                        self._n_snaps, self._seg_dir)

    def __enter__(self) -> "FootageRecorder":
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop()
