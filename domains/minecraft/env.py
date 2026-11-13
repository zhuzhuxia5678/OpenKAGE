"""Minecraft 环境封装（OpenKAGE开放世界主床，via Voyager Mineflayer 服务器）。

把 Voyager 的 Mineflayer Express 服务器（index.js: /start /step /stop）包装成 openkage
编排层好用的形状——沿用 Games + run_episode 抽象：

- MinecraftWorld：一个「MC server 端口 + mineflayer 服务器端口」的连接句柄。
  reset() → /start 起 bot 拿初始 observe；step(code) → /step 执行 LLM 生成的 JS 代码。
- run_exploration：给定一个 world + 一个「跑一整天 ReAct」的回调，跑一整条自由探索轨迹
  （天循环：每天一轮 ReAct，Agent 自己决定何时 write_code；天亮收尾并反思），
  埋点探索指标（独特物品数 / 科技树层级），返回完整轨迹 + 发现。

本域与传统 RL 任务域的差异：
- 动作 = LLM 生成的 JS 代码字符串（非 enum 合法动作）；
- 无 won/correct 二元判定 → 换成探索指标埋点（distinct items / tech tier）；
- 一轮 = N 次 LLM 迭代（对标 Voyager 的 prompting iteration），非游戏帧。

设计约束（同 solve/reflect）：不依赖 backend 业务模块。observe 返回是 JSON 字符串，
需再 json.loads（Voyager index.js 的行为）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import re
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

import requests

from openkage.orchestrate.window import WindowAggregationError

logger = logging.getLogger("minecraft_env")


def _heal_fuse_error_cls() -> type:
    """惰性取 HealFuseError（0930 自愈熔断，定义在包 __init__.py，与 SkillSyntaxError 同区）。

    为什么不在顶部 from . import：① env.py 被包 __init__.py 导入，顶部反向导入是环；
    ② tests 按**文件路径** spec_from_file_location 直载 env.py（脱离包上下文，见
    test_minecraft_idle_guard_no_retire.py 的 _load_env_module），顶层相对导入直接
    ImportError。函数体内在已就绪的 sys.modules 里取，两条路径都安全。
    """
    import domains.minecraft as _pkg
    return _pkg.HealFuseError


def _world_stall_error_cls() -> type:
    """惰性取 WorldStallError（1009 世界时钟停摆熔断，定义在包 __init__.py）。

    同 _heal_fuse_error_cls 的两个理由（环 + tests 文件路径直载），见其 docstring。
    """
    import domains.minecraft as _pkg
    return _pkg.WorldStallError

# ★ **MinecraftWorld 的方法全是同步阻塞的，async 侧一律经 asyncio.to_thread 调用**。
#
# 为什么不把 MinecraftWorld 改成 async：它是个干净的 HTTP 客户端 + 子进程管理器，同步版好读好
# 调试（可以在 REPL 里直接 world.step(...) 手工试一步），改成 async 会连带把 subprocess 等待、
# `time.sleep` 轮询那几段都染色。
#
# 为什么必须 to_thread：`parallel_arms=True` 之后各副脑走 asyncio.gather，而本文件的每一次
# `world.xxx()` 都是**同步阻塞**（requests.post 单步最长 STEP_TIMEOUT=300s、/start 最长
# START_TIMEOUT=90s、regenerate_world 里还有 time.sleep(1) 的轮询最坏几十秒）。裸调的话一个
# 副脑的一步就把事件循环占死、其余副脑全部冻住 —— **看起来在并发，实际是轮流阻塞**，加速为零。
# to_thread 内部走 contextvars.copy_context()，所以 logctx 的 `[e1b1 a1]` 行首标签在工作线程里
# 照样带上，日志不丢归属。

# 一轮探索封顶的夜晚数（昼夜节律，过 D 个夜结束本轮）。**轨迹长度的唯一闸门**：不与步数上限
# 并存 —— 每夜实际所需步数会随内容波动，若同时设步数上限，昼夜节律容易被静默截断成
# 「按步数跑满」。节律是昼夜，步数不是控制轴。
DEFAULT_MAX_NIGHTS = 3
# HTTP 超时：/step 里那段 JS 可能跑几十游戏 tick + 寻路 + 挖掘，给足（exploreUntil/
# mineBlock 等慢原语单步可达数分钟，超时过小会误判掉线触发无谓自愈）。
STEP_TIMEOUT = 300
# ★ **HTTP 超时必须严格大于本步的 Node 侧时限**：Node 到点会自己中止本步并
#   **正常返回**「超时」反馈给 LLM；而这边先超时的话，那次返回就收不到了 —— 走
#   ConnectionError/Timeout 分支去 _heal_and_restart（重启 bot、丢掉本步反馈）。谁先到决定了
#   「一步超时」是温和反馈还是一次假掉线自愈。
#   自选时限上限（540s）下固定 300s 已不够，但**不能一刀把 STEP_TIMEOUT 抬到 600** —— 那会让
#   `timeout_ticks=None` 的基建 step（/forceload 清理、/time set）在 bot 掉线时白等 10 分钟而非 5
#   分钟。故按本步实际申请的时限**逐步推导**：没申请的仍用 300s，申请了的给 +60s 余量
#   （observe 要等 tick、payload 要序列化回传）。这样不变式自动维持，不依赖两个常数手工同步。
STEP_TIMEOUT_SLACK = 60
START_TIMEOUT = 90
#: MC server JVM 参数：
#:   · `-Xmx6G`：高倍速下 tick 工作量翻倍，给 server 堆留缓冲（多臂并发时按臂数估内存）。
#:   · `-XX:+ExitOnOutOfMemoryError`：OOM 即退，让 `_heal_and_restart` 的 MC 探活快速拉起，
#:     而不是等 Watchdog 超时（期间 /step 干等、自愈迟迟不触发）。
#:   · `-XX:+HeapDumpOnOutOfMemoryError`：默认关，每份 dump ≈ 堆大小，并发 OOM 会撑爆磁盘；
#:     需诊断时临时加回此 flag + `-XX:HeapDumpPath=...`。
#:   · `-XX:NativeMemoryTracking=summary`：开销 ~5%，`jcmd <pid> VM.native_memory summary` 看堆外内存。
#:   · `-Xlog:gc*`：GC 日志落 `<mc_dir>/gc.log`（Popen 的 cwd=mc_dir），5 份轮转各 10M，
#:     用于判断卡顿是否 STW full GC。
_MC_JVM_FLAGS = [
    "-Xmx6G",
    "-XX:+ExitOnOutOfMemoryError",
    "-XX:NativeMemoryTracking=summary",
    "-Xlog:gc*:file=gc.log:time,uptime,level,tags:filecount=5,filesize=10M",
]
#: `/start` 的 reset 语义，两种，**别混用**（index.js 的 spawn 回调按这个值分支）：
#:   · "hard"（= `reset_mode` 默认值）→ `/clear @s` + `/kill @s` + 撤销全部成就。
#:     **只能在轮首用**：语义是「世界/库存 fresh，只有记忆跨轮携带」。
#:   · RESET_RECONNECT      → 什么都不清，纯粹把掉线的 bot 接回来。
#: 轮中的自愈重连必须用后者，理由与事故记录见 `_heal_and_restart` 的 docstring。
#: 取值只要不等于 "hard" 就会走 index.js 的 else（即不清），用具名字符串是为了让日志里
#: 能直接看出这次 /start 是哪种语义 —— 传 "" 或 None 在日志里读不出意图。
RESET_RECONNECT = "reconnect"
# 连续多少步世界时钟（timeTicks，服务器权威）不涨就判定停摆、中止本轮。步数上限移除后天亮是唯一
# 出口，时钟停了就永远等不到天亮。给 12 步余量：单步只跑几 tick 时两步同刻是可能的，但连续十几步
# 一刻不涨只能是 advance_time 失效 / 世界仍冻结。
# ⚠️ 判据**不能用 elapsedTime**（bot.globalTickCounter）：它会被自愈的 /start 归零，
#    护栏会把自愈副作用误判成停摆 —— 详见主循环里那段注释。
STALL_ABORT_STEPS = 12
# 连续自愈失败熔断阈值：_run_step_body 的 resume/step 两处 broad-except 把自愈失败
# 降级为「喂 agent 一条错误反馈」，mineflayer 自愈死循环在这条路径上可空转数小时。
# 连续失败达到本阈值抛 HealFuseError（registry 放行 → 停整 run，checkpoint 完整可续）。
# 取 10 让偶发抖动几轮内自愈、不误伤，真死循环也能较快熔断。
_MAX_HEAL_FAIL_STREAK = 10
# 重生世界（regenerate_world）用：停服等待 + 起服等待的上限。新世界首次生成一般要几十秒
# （其中 spawn 区块生成很快，多数时间花在 JVM + fabric mod 加载），给足余量。
MC_STOP_TIMEOUT = 60
MC_START_TIMEOUT = 180

# 随机出生点：传送前先 `/forceload` 预热目标点周围这么多格（见 prewarm_and_spawn）。
# 为什么要预热：MC server 启动只预热出生点那一圈（日志 `Preparing spawn area`），别处地形不存在；
# bot 传到未生成区块时地形虽会按需生成，但 mineflayer 等 tick 的时限很短 → /start 卡死
# （卡的是传送**之后**等 tick，传送本身是成功的）。
# 这个 ring 只影响预热耗时、**与随机半径无关**，所以半径可以放很大。别调大 ring：把整片随机区域
# 预生成会占满服务器主线程 → bot 收不到 keep_alive → 掉线 → mineflayer 进程崩。
SPAWN_PREWARM_RING = 48
# 落点校验容差（格）：/spreadplayers 会在目标点附近微调、bot 落地也会滑一两格，故不要求精确相等。
# "传送未生效"时偏差会有上千格，这个阈值足以区分两者。
SPAWN_ARRIVE_TOLERANCE = 24
# 出生点抽点上限：地表不可站（水面/岩浆等）就换一个点重抽，最多抽这么多次。
# 大陆世界里绝大多数点一次就中，这个数只是防"半径整片是海"时无限循环。
SPAWN_MAX_TRIES = 12

# timeOfDay（见 mc_server/lib/observation/status.js）的「夜晚」标签集合。
# is_night 用它判昼夜：仅 night+midnight（官方夜晚段 13800-22200），sunset/sunrise 是过渡段不算夜。
# 白天→夜晚的跳变（进 night）= 触发一次入夜检索 night_just_fell；日界由 worldDay 滚动判（不靠它）。
NIGHT_PHASES = frozenset({"night", "midnight"})

# control_primitives 目录：mc_server 与本文件同目录（domains/minecraft/mc_server/）。
_CONTROL_PRIMITIVES_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "mc_server", "control_primitives",
)

# ★ 合成反馈薄壳（2026-09-01 加）：包装 bot.recipesFor/recipesAll（返空时补一条客观事实诊断）
#   和 bot.craft（recipe 为 null 时把不可读的 AssertionError 换成可读错误）。只报客观事实
#   （配方存不存在/需不需要工作台/库存够不够/recipe 参数是什么），不处方（不教下一步做什么）。
#   详见 mc_server/lib/recipeWrap.js 顶部注释。移除高级原语后（self._programs=""）Agent 只剩
#   原生 API，缺了原语时代 craftItem 的客观反馈，0901 run 第一天 5 臂全卡在「recipesFor 返回0」
#   却无任何「需要工作台」的信号。此薄壳只补反馈、不恢复预封装能力，与去原语初衷不冲突。
#   开关：True=注入诊断薄壳（实验组），False=不注入（对照组，还原纯原生 API 行为）。
#   读环境变量 OPENKAGE_RECIPE_WRAP 控制，默认开（关掉即还原旧静默行为做对照）。
_RECIPE_WRAP_ENABLED = os.environ.get("OPENKAGE_RECIPE_WRAP", "1") not in ("0", "false", "False", "")
_RECIPE_WRAP_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "mc_server", "lib", "recipeWrap.js",
)

# ★ 时间栅栏（2026-09-01 加）：write_code 之间冻结游戏时间。**无条件开启**（不再有 on/off 对照，
#   栅栏是标准行为）。
#   根因：LLM 思考（API 调用、记忆检索、规划工具）期间 MC 世界照常 tick，思考慢的 arm 思考墙钟
#   被算进 24000 tick 的游戏日 → "少步"分不清是"图谱拖慢思考"还是"图谱带来更慢但更好的决策"
#   （0831 步数混淆）。栅栏让游戏时间只在 write_code 执行期间推进，思考期间 world.pause() 冻结，
#   把"游戏日"从"实时 24000（含思考空转）"重定义为"纯动作 24000"，干净消除步数混淆。
#   复用既有的双侧 world.pause()/resume()（见下方 pause/resume 定义），tick rate 跨 resume 已保
#   （resumeBot 每次 /tick rate 补发）。思考效率（墙钟/token/检索次数）另作补充轴单独报，不混进游戏日。


class _HealFuseCounter:
    """连续自愈失败计数器。

    为什么独立成类：计数的两个接入点（_run_step_body 的 resume 失败分支、step 失败分支）
    都是深嵌套闭包，行内变量难单测；提取成纯状态机（record_failure / record_success /
    trip），tests/test_mc_self_heal.py 直接驱动，与 stalled_steps（行内计数、只能靠
    _react_day_loop 间接测）互补。

    语义：
      · record_failure()：resume 或 step 自愈失败一次 +1，返回是否达到熔断阈值；
      · record_success()：任一路径成功（resume 成功 / world.step 真跑成功）→ 清零——
        偶发抖动（网关 502、ReadTimeout）几轮内自愈不该累计；
      · trip()：达阈值时抛 HealFuseError（定义在 domains/minecraft/__init__.py，
        registry._ABORT_EXCEPTION_NAMES 放行 → loop.py rollout-abort 停整 run）。
    """

    __slots__ = ("streak", "max_streak")

    def __init__(self, max_streak: int = _MAX_HEAL_FAIL_STREAK):
        self.streak = 0
        self.max_streak = max_streak

    def record_success(self) -> None:
        self.streak = 0

    def record_failure(self) -> bool:
        self.streak += 1
        return self.streak >= self.max_streak

    def trip(self, context: str = "") -> None:
        """抛熔断异常。context 附加现场（如 'resume 失败' / 'step 失败'）便于日志定位。"""
        raise _heal_fuse_error_cls()(
            f"连续 {self.streak} 次自愈失败（{context}）—— 疑似 mineflayer 自愈死循环"
            "已熔断停整 run（checkpoint 完整），请查 mineflayer.log 与 MC server 日志，"
            "人工处置后 --resume-window N 续跑。"
        )


def _load_recipe_wrap_source() -> str:
    """读 recipeWrap.js 自包含执行代码。文件缺失或开关关闭时返回空串（不注入）。"""
    if not _RECIPE_WRAP_ENABLED:
        return ""
    try:
        with open(_RECIPE_WRAP_PATH, encoding="utf-8") as fh:
            return fh.read()
    except OSError as e:
        logger.warning("recipe 诊断薄壳读取失败（不注入，退回原生静默行为）：%s", e)
        return ""


def _offline_uuid(name: str) -> str:
    """算 MC **离线模式**玩家的 UUID：`md5("OfflinePlayer:<name>")` 再打上 v3 版本位。

    ops.json 按「name + uuid」授权 OP，而离线服的 UUID 完全由名字决定 —— 所以换 bot 名字必须
    同步算出新 UUID 写进去（见 MinecraftWorld.set_bot_name 的注释）。
    """
    h = bytearray(hashlib.md5(f"OfflinePlayer:{name}".encode("utf-8")).digest())
    h[6] = (h[6] & 0x0F) | 0x30  # version 3
    h[8] = (h[8] & 0x3F) | 0x80  # RFC 4122 variant
    return str(uuid.UUID(bytes=bytes(h)))


def _default_java_bin() -> str:
    """找一个 Java 21（MC 1.21.11 起服硬要求；系统默认 java 常是 8，会起不来）。

    顺序：JAVA_BIN 环境变量（由 __init__ 处理）→ 常见 openjdk-21 安装路径 → 退回 "java"。
    与 mc_server/start_servers.sh 的 JAVA_BIN 默认值保持一致。
    """
    import glob
    for pat in ("/usr/lib/jvm/java-21-openjdk*/bin/java", "/usr/lib/jvm/*21*/bin/java"):
        hits = sorted(glob.glob(pat))
        if hits:
            return hits[0]
    return "java"


#: craftHelper.js 用来「按 tag 归纳配方」的数据（见 domains/minecraft/extract_tags.py）。
#: 缺了不致命（原语会降级成枚举 prismarine 变体并 chat 一句 warn），但播报会变啰嗦、且会漏掉
#: minecraft-data 尚未跟上的新物品（如 1.21 的 pale_oak_*），所以启动时主动生成一次。
_MC_TAGS_JSON = os.path.join(os.path.dirname(_CONTROL_PRIMITIVES_DIR), "lib", "mc_tags.json")


def ensure_mc_tags(jar_path: str | None = None) -> bool:
    """确保 mc_server/lib/mc_tags.json 存在；不存在就从服务端 jar 抽一份。

    返回是否可用。**故意不做「过期检查」**：换 MC 版本时请手动重跑
    `python3 -m domains.minecraft.extract_tags --jar <新jar>`，因为 jar 路径由 --mc-dir 决定，
    这里拿不到那个值（env 实例化时才有），静默用错版本的 tag 比没有更糟。
    """
    if os.path.isfile(_MC_TAGS_JSON):
        return True
    try:
        from .extract_tags import DEFAULT_JAR, extract_item_tags, extract_recipes
        jar = jar_path or DEFAULT_JAR
        if not os.path.isfile(jar):
            logger.warning("[minecraft] 找不到服务端 jar %s —— craftHelper 将降级为枚举配方变体"
                           "（播报更啰嗦，且可能漏掉新版物品）", jar)
            return False
        tags = extract_item_tags(jar)
        payload = {"__version__": tags.pop("__version__", ""),
                   "tags": tags, "recipes": extract_recipes(jar)}
        os.makedirs(os.path.dirname(_MC_TAGS_JSON), exist_ok=True)
        with open(_MC_TAGS_JSON, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=1)
        logger.info("[minecraft] 已生成 %s（MC %s，%d 个 item tag / %d 种产物配方）",
                    _MC_TAGS_JSON, payload["__version__"], len(tags), len(payload["recipes"]))
        return True
    except Exception:  # pylint: disable=broad-except
        logger.warning("[minecraft] 抽取 item tag 失败 —— craftHelper 将降级为枚举配方变体",
                       exc_info=True)
        return False


def load_control_primitives(cp_dir: str | None = None) -> str:
    """把 mc_server/control_primitives 下所有 .js 拼成一个 programs 字符串（Voyager 做法）。
    每次 /step 作为 programs 注入，生成代码里可直接 await mineBlock/craftItem/... """
    cp_dir = cp_dir or _CONTROL_PRIMITIVES_DIR
    if not os.path.isdir(cp_dir):
        logger.warning("control_primitives 目录不存在：%s（programs 将为空）", cp_dir)
        return ""
    ensure_mc_tags()
    parts = []
    for f in sorted(os.listdir(cp_dir)):
        if f.endswith(".js"):
            with open(os.path.join(cp_dir, f), encoding="utf-8") as fh:
                parts.append(fh.read())
    return "\n".join(parts)


# ★ 单独加载 waitUntil 这一个原语（2026-09-08）：
# 0827 去原语时 self._programs = ""（清空全部 16 个高级原语，拉开实验组与对照组差距），
# 但 waitUntil 不是「能力」原语（mineBlock/craftItem 那种替 Agent 完成采集合成、抹平差距），
# 而是「等待」原语——用服务器 timeOfDay 增量计时、dawn 兜底天亮自动醒（见
# control_primitives/waitUntil.js:260-272）。它解决的是「夜里如何合法等天亮」的表达问题，
# 不赋予任何能力优势，故单独保留不违背去原语实验设计。
# 不恢复其他 15 个原语，只读 waitUntil.js 单文件。waitUntil.js 无外部依赖（只用 bot API）。
# 没有它的后果：Agent 夜里被迫自制 for(N){waitForTicks(n)} 守夜循环——但 waitForTicks 等的是
# 客户端 physicsTick 事件、与服务器 timeOfDay 解耦（OPENKAGE_PATCHES.md:10 实锤），循环跑完
# 天也不会亮，每步只推进 ~40 刻，熬不到天亮 → 续跑 5 次触发 _MAX_RESUME_PER_DAY 被 retire
# 退场（bob day3 实测）。注入 waitUntil 后，续跑兜底（env.py _force_dawn_guard）调
# waitUntil(bot, 3000, '守夜', ['dawn']) 让时间流逝到天亮进反思；Agent 主动过夜改用 bot.sleep
# 跳夜（prompt 不再教 dawn，dawn 事件仅续跑兜底用）。
_WAIT_UNTIL_PRIMITIVE_FILE = "waitUntil.js"

def load_wait_until_primitive(cp_dir: str | None = None) -> str:
    """只加载 control_primitives/waitUntil.js 这一个原语（不加载其他 15 个）。

    详见模块级注释：0827 去原语后单独保留 waitUntil 这个「等待」原语，让 Agent 夜里能合法
    等天亮，而非自制无效的 waitForTicks 循环。返回的字符串拼进 programs，eval 作用域里
    `await waitUntil(bot, ...)` 全局可调（与原 mineBlock 同机制）。
    """
    cp_dir = cp_dir or _CONTROL_PRIMITIVES_DIR
    path = os.path.join(cp_dir, _WAIT_UNTIL_PRIMITIVE_FILE)
    if not os.path.isfile(path):
        logger.warning("waitUntil 原语文件不存在：%s（programs 将不含 waitUntil）", path)
        return ""
    with open(path, encoding="utf-8") as fh:
        return fh.read()




def is_night(obs: dict) -> bool:
    """从 observe 判当前是否夜晚阶段（timeOfDay ∈ NIGHT_PHASES）。"""
    tod = (obs.get("status", {}) or {}).get("timeOfDay", "")
    return tod in NIGHT_PHASES

# 科技树层级：**Voyager 语义** —— 只认「合成出该级工具成品」，不认原材料。
#
# 判定物是成品工具（wooden/stone/iron/diamond 四级），不是原材料：拿到锭必先于合成对应的镐，
# 若原材料就能满足判定，四级门槛就形同虚设。
#
# 同级认「任一工具」而非只认镐（依据 Voyager 自身计法）：难度在于拿到该级材料，不在于做哪一件，
# 故同级 pickaxe/sword/axe/shovel/hoe 任一即算解锁。**不计护甲**（走的是另一条配方链，且这四级
# 讲的是 tool）；**不计 golden_***（金工具材料易得但耐久极差，不在 wooden→diamond 这条主升级链上）。
#
# ★ 匹配方式为**精确相等**（不是子串）：库存 key 是 mineflayer 的确切物品名
#   （见 mc_server/lib/observation/inventory.js 直接取 item.name），无需模糊匹配，子串只会误吃。
_TIER_TOOL_KINDS = ("pickaxe", "sword", "axe", "shovel", "hoe")
_TIER_MATERIALS = ("diamond", "iron", "stone", "wooden")  # 由高到低
TECH_TIERS = [
    (mat, [f"{mat}_{kind}" for kind in _TIER_TOOL_KINDS])
    for mat in _TIER_MATERIALS
]
# rank：none=0 wooden=1 stone=2 iron=3 diamond=4 —— 与 Voyager Table 1 的四级里程碑**逐档对应**，
# 可直接对报。
_TIER_RANK = {name: len(TECH_TIERS) - i for i, (name, _) in enumerate(TECH_TIERS)}
_TIER_RANK["none"] = 0

# ── 9 级主线科技（按 Minecraft 进度树深度，2026-09-05 改）────────────────────
# tier 不再只看工具材质，而是按 agent 在 Minecraft 标签进度树（story.*）达成的**最深进度**算。
# 进度树最长路径 9 层（见 results/Advancements.txt）：
#   L1 工作台 → L2 圆石 → L3 石镐 → L4 铁锭 → L5 铁镐/熔岩桶/铁甲
#   → L6 钻石/黑曜石/盾 → L7 附魔/钻石甲/进下界 → L8 末影之眼/治僵尸村民 → L9 进末地
# 同层多进度（如 L5 有铁镐/熔岩桶/铁甲三个并行）取主分支名，旁支算同层达成。
# 显示名 = 游戏聊天播报的英文 advancement 名（带方括号，如 "[Diamonds!]"）。
_ADV_DEPTH: dict[str, int] = {
    "[Minecraft]": 1,            # story.root 工作台
    "[Stone Age]": 2,            # story.mine_stone 圆石
    "[Getting an Upgrade]": 3,   # story.upgrade_tools 石镐
    "[Acquire Hardware]": 4,     # story.smelt_iron 铁锭
    "[Isn't It Iron Pick]": 5,   # story.iron_tools 铁镐
    "[Hot Stuff]": 5,            # story.lava_bucket 熔岩桶（同层）
    "[Suit Up]": 5,              # story.obtain_armor 铁甲（同层）
    "[Diamonds!]": 6,            # story.mine_diamond 钻石
    "[Ice Bucket Challenge]": 6, # story.form_obsidian 黑曜石（同层）
    "[Not Today, Thank You]": 6, # story.deflect_arrow 盾（同层）
    "[Enchanter]": 7,            # story.enchant_item 附魔
    "[Cover Me with Diamonds]": 7,  # story.shiny_gear 钻石甲（同层）
    "[We Need to Go Deeper]": 7, # story.enter_the_nether 进下界（同层）
    "[Zombie Doctor]": 8,        # story.cure_zombie_villager 治僵尸村民
    "[Eye Spy]": 8,              # story.follow_ender_eye 末影之眼
    "[The End?]": 9,             # story.enter_the_end 进末地
}
# 深度 → tier 名（同层多进度共用一个 tier 名，取主分支里程碑）
_DEPTH_TIER: dict[int, str] = {
    1: "workbench", 2: "stone", 3: "stone_tools", 4: "iron",
    5: "iron_tools", 6: "diamond", 7: "enchant", 8: "ender_eye", 9: "end",
}
# 9 级 tier 的 rank（含旧 4 级名兼容：wooden=1 stone=2 iron=3 diamond=4 保留，
# 新 5-9 为增量；旧 run 的 tier_progress 存旧名，rank 仍可比、只是没有 5-9）
_TIER_RANK_9 = {
    "none": 0, "workbench": 1, "stone": 2, "stone_tools": 3, "iron": 4,
    "iron_tools": 5, "diamond": 6, "enchant": 7, "ender_eye": 8, "end": 9,
    # 旧名兼容（历史 run 的 tier_progress 用这些）
    "wooden": 1,
}

# ── 5 棵进度树的完整映射（2026-09-05）─────────────────────────────────────────
# 与上面 _ADV_DEPTH（只覆盖 base 一棵、且只取深度做 tier 名）互补：这里覆盖全部 5 棵树
# 的全部进度，给观察字段的「进度前沿」用——只报事实（各树当前走到哪），不压成单线、
# 不给路线。Minecraft 原版 5 个标签：base(=story,核心故事线)、冒险、下界、末地、农牧业。
# 对 Agent 隐去 "story" 字样、统一叫 base（story 是给人类玩家的叙事名）。
#
# 由 tools/gen_advancement_table.py 从 results/Advancements.txt（树结构/深度）+
# mcData 1.21.11 language.json（英文标题）生成，勿手改。重跑：MC 升级后 `python3
# tools/gen_advancement_table.py` 刷新。
# 已知缺口：husbandry.uh_oh（26.2 snapshot 新增，1.21.11 lang 未收录）——
#   _progress_frontier 遇未知显示名会跳过（不报错），lang 收录后重跑即补。
#: 进度显示名(游戏聊天播报的英文标题,带方括号) -> (树, 深度)。与 onSystem.js
#: 运行时采集的 advancement 字段同源同形（见 _collect_system_events）。
_ADV_INFO: dict[str, tuple[str, int]] = {
    # base (16 个, depth 1-9)
    '[Minecraft]': ('base', 1),
    '[Stone Age]': ('base', 2),
    '[Getting an Upgrade]': ('base', 3),
    '[Acquire Hardware]': ('base', 4),
    '[Hot Stuff]': ('base', 5),
    "[Isn't It Iron Pick]": ('base', 5),
    '[Suit Up]': ('base', 5),
    '[Diamonds!]': ('base', 6),
    '[Ice Bucket Challenge]': ('base', 6),
    '[Not Today, Thank You]': ('base', 6),
    '[Cover Me with Diamonds]': ('base', 7),
    '[Enchanter]': ('base', 7),
    '[We Need to Go Deeper]': ('base', 7),
    '[Eye Spy]': ('base', 8),
    '[Zombie Doctor]': ('base', 8),
    '[The End?]': ('base', 9),
    # nether (23 个, depth 1-6)
    '[Nether]': ('nether', 1),
    '[A Terrible Fortress]': ('nether', 2),
    '[Hidden in the Depths]': ('nether', 2),
    '[Oh Shiny]': ('nether', 2),
    '[Return to Sender]': ('nether', 2),
    '[Subspace Bubble]': ('nether', 2),
    '[This Boat Has Legs]': ('nether', 2),
    '[Those Were the Days]': ('nether', 2),
    '[Who is Cutting Onions?]': ('nether', 2),
    '[Cover Me in Debris]': ('nether', 3),
    '[Feels Like Home]': ('nether', 3),
    '[Hot Tourist Destinations]': ('nether', 3),
    '[Into Fire]': ('nether', 3),
    '[Not Quite "Nine" Lives]': ('nether', 3),
    '[Spooky Scary Skeleton]': ('nether', 3),
    '[Uneasy Alliance]': ('nether', 3),
    '[War Pigs]': ('nether', 3),
    '[Local Brewery]': ('nether', 4),
    '[Withering Heights]': ('nether', 4),
    '[A Furious Cocktail]': ('nether', 5),
    '[Bring Home the Beacon]': ('nether', 5),
    '[Beaconator]': ('nether', 6),
    '[How Did We Get Here?]': ('nether', 6),
    # end (9 个, depth 1-5)
    '[The End]': ('end', 1),
    '[Free the End]': ('end', 2),
    '[Remote Getaway]': ('end', 3),
    '[The End... Again...]': ('end', 3),
    '[The Next Generation]': ('end', 3),
    '[You Need a Mint]': ('end', 3),
    '[The City at the End of the Game]': ('end', 4),
    '[Great View From Up Here]': ('end', 5),
    "[Sky's the Limit]": ('end', 5),
    # adventure (47 个, depth 1-4)
    '[Adventure]': ('adventure', 1),
    '[Caves & Cliffs]': ('adventure', 2),
    '[Country Lode, Take Me Home]': ('adventure', 2),
    '[Crafters Crafting Crafters]': ('adventure', 2),
    '[Crafting a New Look]': ('adventure', 2),
    '[Heart Transplanter]': ('adventure', 2),
    '[Is It a Bird?]': ('adventure', 2),
    "[Isn't It Scute?]": ('adventure', 2),
    '[Minecraft: Trial(s) Edition]': ('adventure', 2),
    '[Monster Hunter]': ('adventure', 2),
    "[Ol' Betsy]": ('adventure', 2),
    '[Respecting the Remnants]': ('adventure', 2),
    '[Sneak 100]': ('adventure', 2),
    '[Sticky Situation]': ('adventure', 2),
    '[Surge Protector]': ('adventure', 2),
    '[Sweet Dreams]': ('adventure', 2),
    '[The Power of Books]': ('adventure', 2),
    '[Voluntary Exile]': ('adventure', 2),
    '[What a Deal!]': ('adventure', 2),
    '[A Throwaway Joke]': ('adventure', 3),
    '[Adventuring Time]': ('adventure', 3),
    '[Arbalistic]': ('adventure', 3),
    '[Blowback]': ('adventure', 3),
    '[Careful Restoration]': ('adventure', 3),
    '[Hero of the Village]': ('adventure', 3),
    '[Hired Help]': ('adventure', 3),
    '[Is It a Balloon?]': ('adventure', 3),
    '[It Spreads]': ('adventure', 3),
    '[Light as a Rabbit]': ('adventure', 3),
    '[Lighten Up]': ('adventure', 3),
    '[Mob Kabob]': ('adventure', 3),
    '[Monsters Hunted]': ('adventure', 3),
    '[Over-Overkill]': ('adventure', 3),
    '[Postmortal]': ('adventure', 3),
    '[Smithing with Style]': ('adventure', 3),
    '[Sound of Music]': ('adventure', 3),
    '[Star Trader]': ('adventure', 3),
    '[Take Aim]': ('adventure', 3),
    '[Two Birds, One Arrow]': ('adventure', 3),
    '[Under Lock and Key]': ('adventure', 3),
    '[Who Needs Rockets?]': ('adventure', 3),
    "[Who's the Pillager Now?]": ('adventure', 3),
    '[Bullseye]': ('adventure', 4),
    '[Is It a Plane?]': ('adventure', 4),
    '[Revaulting]': ('adventure', 4),
    '[Sniper Duel]': ('adventure', 4),
    '[Very Very Frightening]': ('adventure', 4),
    # husbandry (30 个, depth 1-5)
    '[Husbandry]': ('husbandry', 1),
    '[A Seedy Place]': ('husbandry', 2),
    '[Bee Our Guest]': ('husbandry', 2),
    '[Best Friends Forever]': ('husbandry', 2),
    '[Bukkit Bukkit]': ('husbandry', 2),
    '[Fishy Business]': ('husbandry', 2),
    '[Glow and Behold!]': ('husbandry', 2),
    '[Smells Interesting]': ('husbandry', 2),
    '[Stay Hydrated!]': ('husbandry', 2),
    '[The Parrots and the Bats]': ('husbandry', 2),
    '[Total Beelocation]': ('husbandry', 2),
    '[Whatever Floats Your Goat!]': ('husbandry', 2),
    "[You've Got a Friend in Me]": ('husbandry', 2),
    '[A Balanced Diet]': ('husbandry', 3),
    '[A Complete Catalogue]': ('husbandry', 3),
    '[Birthday Song]': ('husbandry', 3),
    '[Good as New]': ('husbandry', 3),
    '[Little Sniffs]': ('husbandry', 3),
    '[Serious Dedication]': ('husbandry', 3),
    '[Shear Brilliance]': ('husbandry', 3),
    '[Tactical Fishing]': ('husbandry', 3),
    '[The Whole Pack]': ('husbandry', 3),
    '[Two by Two]': ('husbandry', 3),
    '[Wax On]': ('husbandry', 3),
    '[When the Squad Hops into Town]': ('husbandry', 3),
    '[Planting the Past]': ('husbandry', 4),
    '[The Cutest Predator]': ('husbandry', 4),
    '[Wax Off]': ('husbandry', 4),
    '[With Our Powers Combined!]': ('husbandry', 4),
    '[The Healing Power of Friendship!]': ('husbandry', 5),
}

#: 5 棵树的展示顺序与中文名（观察字段渲染用）。base 译「主线」排首位（核心故事线）；
#: 其余按项目既定展示顺序（冒险/农牧业/下界/末地）。顺序固定便于 Agent 形成定式。
_ADV_TREE_ORDER: tuple[tuple[str, str], ...] = (
    ("base", "主线"), ("adventure", "冒险"), ("husbandry", "农牧业"),
    ("nether", "下界"), ("end", "末地"),
)

#: 「这件东西是工具/装备」的后缀判据，供物品无声消失诊断分级（见 run_exploration 里那处）。
#: 复用 _TIER_TOOL_KINDS（tier 判定的同一套后缀）再加护甲件，避免两份名单漂移；
#: 护甲要单列是因为它不参与 tier 但同样"丢了就是大事"（穿在身上、掉了会静默挨打）。
_TOOL_SUFFIXES = frozenset(_TIER_TOOL_KINDS) | frozenset(
    {"helmet", "chestplate", "leggings", "boots", "shield", "bow", "crossbow"}
)


#: 「这件东西是建造物」的判据 —— 用于每日建造能力指标（见 discoveries.buildings_*）。
#:
#: ★ **语义是「造过」，不是「世界里有几个」**：env 不采集已放置方块（JS observe 侧无此通道），
#:   故只能拿 `items_seen`（累积 set，只增不减）∩ 本名单 推断「bot 造出过哪些可放置方块」。
#:   这与 unique_items 同口径：把它造出来本身就是一次建造能力事件，至于造完又拆/又丢不计。
#:   缺点已知：造了又拆看不出来、当前世界存几个无从得知 —— 这块缺口留给事后（如确需再改 JS）。
#:
#: ★ **庇护所不算**：它是空间结构（四面墙+顶），不是单个物品，无法从物品名推断。
#:   若哪天要测庇护所，得另开一套「放置事件采集」，不能塞进这份名单。
#:
#: 命名同 `_TIER_TOOL_KINDS`：mineflayer 的确切物品名（snake_case），精确相等匹配；
#: 家族（door/trapdoor/bed/...）用后缀，单名单列。护甲/工具天然不在此列，不会误吃。
_BUILDABLE_EXACT: frozenset[str] = frozenset({
    # 工作类（科技/加工站）
    "crafting_table", "furnace", "blast_furnace", "smoker", "loom",
    "cartography_table", "fletching_table", "grindstone", "stonecutter",
    "smithing_table", "anvil", "chipped_anvil", "damaged_anvil", "brewing_stand",
    # 存储
    "chest", "trapped_chest", "ender_chest", "barrel", "hopper",
    "dispenser", "dropper",
    # 照明
    "torch", "soul_torch", "lantern", "soul_lantern", "glowstone",
    "sea_lantern", "end_rod", "redstone_lamp", "campfire", "soul_campfire",
    # 通行/结构
    "ladder", "scaffolding",
    # 农牧
    "composter", "bee_nest", "beehive", "cauldron",
    # 红石
    "redstone_torch", "lever", "repeater", "comparator", "observer",
    "piston", "sticky_piston",
    # 装饰/功能结构
    "glass", "glass_pane", "bookshelf", "enchanting_table", "conduit",
})
#: 家族后缀：oak_door / birch_door …、oak_trapdoor …、oak_bed …、shulker_box 及各色变体。
#: 用后缀匹配（`name.endswith(suf)`），覆盖全部木材/颜色变体，无需逐一列举。
_BUILDABLE_SUFFIXES: tuple[str, ...] = (
    "_door", "_trapdoor",          # 各种门/活板门
    "_bed",                        # 各种床
    "shulker_box",                 # 各色潜影盒（含 "shulker_box" 本身与 "<color>_shulker_box"）
    "_fence", "_fence_gate",       # 栅栏/栅栏门
    "_sign", "_banner",            # 牌子/旗帜（放置类）
)

#: ★ keep_inventory gamerule 的真值（2026-08-22 用户定 = true）。
#:: 与 mc_server/index.js 里 `bot.chat("/gamerule keep_inventory true")` 必须一致 ——
#:: Python 侧的死亡反馈文案、物品消失诊断分支都引用它，避免硬编码布尔散落多处漂移。
#:: true = 死后装备保留不掉落；false = 死亡掉落全部物品（1.21 前 的旧设计）。
_KEEP_INVENTORY: bool = True


def _is_buildable(name: str) -> bool:
    """判定一个物品名是否属于「建造物」（见 _BUILDABLE_EXACT 注释的语义）。

    匹配顺序：精确相等 → 后缀。与 `_TIER_TOOL_KINDS` 同为精确匹配风格，家族用后缀而非子串，
    避免误吃（"door" 后缀不会吃进非门的物品）。
    """
    if not name:
        return False
    if name in _BUILDABLE_EXACT:
        return True
    return any(name.endswith(suf) for suf in _BUILDABLE_SUFFIXES)


#: 从 LLM 生成的 JS 代码里抓「合成/放置的物品名」用的原语名 → 第几个参数是物品名。
#: ★ 只列**第一参数是物品名字符串**的原语：craftItem(bot, name, count)、placeItem(bot, name, pos)。
#:   垂直垫高放的是脚手架（dirt/cobblestone），不是「建造物」，且其参数是方块名而非典型建造物 → 不列。
#:   这些名字与 skillLoader 暴露给 LLM 的函数名一致（见 mc_server/lib/skillLoader.js）。
_BUILD_PRIMITIVE_ARG1: tuple[str, ...] = ("craftItem", "placeItem")
_BUILD_NAME_RE = None  # 延迟编译（见 _extract_built_from_code）


def _extract_built_from_code(code: str) -> set[str]:
    """从一段 LLM 生成的 JS 代码里提取 craftItem/placeItem 的**物品名参数**。

    抓的是 `craftItem(bot, 'crafting_table', 1)` / `placeItem(bot, 'torch', pos)` 里第一个字符串
    字面量。用正则而非 JS 解析器：原语调用形态固定（`name(任意, '...'`），正则够用且零依赖。

    ★ **为什么不靠 items_seen**：items_seen 在步末采样 inventory，而「合成后同一步放置」会让
      建造物从库存消失 → 漏采（smoke 实测）。代码文本是 Agent 意图的直接记录，造了就抓得到，
      不受库存瞬态影响。抓到的名字再由调用方 ∩ _is_buildable 过滤成建造物。
    ★ **只抓意图、不判成败**：placeItem 可能放置失败（被挡/超时），但仍算一次「建造尝试」。
      失败的放置不进 items_seen 也进不了这里的世界，但 Agent 的建造能力意图值得留痕；
      是否真建成由 items_seen ∩ placeItem 的交集佐证。这里宁可多抓（含失败尝试），事后再筛。
    ★ **先剥 JS 注释再匹配**：Agent 常在注释里写「// 我要 craftItem(bot,'furnace',1)」但没真造，
      不剥注释会把这种「计划但未执行」误记成建造过。剥 `//` 行注释与 `/* */` 块注释即可。
      字符串字面量里的同名词（console.log("placeItem...")）仍可能被误抓 —— 但那种情况罕见，
      且抓到的是 Agent 写进日志的物品名，多半本就是它关心的建造物，过度抓取代价可接受。
    """
    import re  # 局部 import，避免模块级副作用
    global _BUILD_NAME_RE
    if _BUILD_NAME_RE is None:
        # 匹配 `craftItem(` 或 `placeItem(` 后、到第一个单/双引号字符串字面量。
        # 允许中间有空白/换行/`bot,`。字符串用单引号或双引号。
        names = "|".join(_BUILD_PRIMITIVE_ARG1)
        _BUILD_NAME_RE = re.compile(
            r"\b(?:" + names + r")\s*\([^)]*?(['\"])([a-z0-9_]+)\1",
            re.IGNORECASE,
        )
    found: set[str] = set()
    for _m in _BUILD_NAME_RE.finditer(_strip_js_comments(code or "")):
        found.add(_m.group(2))
    return found


def _strip_js_comments(code: str) -> str:
    """剥掉 JS 的行注释（`// ...`）与块注释（`/* ... */`），保留字符串字面量内不被误删。

    只为 `_extract_built_from_code` 服务：让正则不去匹配注释里「计划但未执行」的原语调用。
    简单实现（不处理字符串内的 `//`，如 URL）—— 本域 LLM 代码极少含 URL/正则字面量，
    且即便误剥，后果只是少抓一个字符串内的物品名，不影响真调用。够用即可，不引完整 JS 词法。
    """
    import re
    # 块注释（非贪婪，跨行）
    code = re.sub(r"/\*.*?\*/", "", code, flags=re.DOTALL)
    # 行注释：`//` 到行尾。不匹配字符串内的 `//` —— 简单起见接受这个偏差（见 docstring）。
    code = re.sub(r"//[^\n]*", "", code)
    return code


#: Node 侧 **observe 事件**的 payload 里我们会渲染给 LLM 的字段，用于「产出/消费一致性」自检
#: （见 _audit_observe_fields）。
#: ★ 这里**只列非 on* 字段**：base.js 的 `bot.event(name)` 对 on* 插件做了过滤
#:   （`if (obs.name.startsWith("on") && obs.name !== event_name) return`），所以 event_name
#:   ="observe" 时所有 on* 都被跳过 —— onChat/onError/onLog/onSystem 出现在**各自的**事件项里
#:   （`[["onChat",{...}], ["observe",{...}]]`），由 _collect_events / _collect_system_events 消费。
#:   把它们写进这里会导致每轮都误报「payload 里没有」。
_RENDERED_FIELDS = frozenset({
    "status", "inventory", "voxels", "nearbyGrid", "vision", "nearbyChests",
})
#: 已知会出现但**有意不进 LLM 渲染**的字段（列在这里 = 明确表态，不再报 orphan warn）。
#: `world_healthy` 是 Node 侧每步注入的世界健康探针（columns/feet_block），由
#: `_guard_world_health_after_step` **系统自用**触发自愈，不给 LLM 看——它不是「观察事实」，
#: 进渲染反而会诱导 LLM 把「世界半连接」当成自己的环境信息去 react。详见 index.js 的注入逻辑。
#: `scoreboard` 是 MC 原生记分板权威值(deathCount/totalKillCount/xp/health/food/armor)，由
#: env **自用**做自统计指标的交叉校验旁证（落盘双值，见 discoveries["scoreboard"]）。同样不给
#: LLM 看——分数是评测元数据不是环境事实，渲染它会让 agent 去刷分而非自由探索。
#: `advancementPacket` 是 packet_advancements 协议包维护的「已完成进度」状态快照（佐证支路，
#: 2026-09-07），能采到 announce_to_chat=false 的静默进度（如 story/root=[Minecraft]）。
#: 与 onSystem 被动播报并排双值落盘（见 discoveries["advancement_packet"]），不渲染给 LLM。
#: `spectatorCam` 是观战相机探针（2026-09-15，见 spectatorCam.js）：读观战账号玩家实体与 bot
#: 的距离判「录像相机是否在跟着 bot」。env 步末看门狗自用（触发 resync_spectate 防脱附空镜），
#: 不渲染给 LLM——它是录像基建的健康信号，不是 bot 的环境事实。
_IGNORED_FIELDS: frozenset[str] = frozenset(
    {"world_healthy", "scoreboard", "advancementPacket", "spectatorCam"})

_audited_once = False


def _audit_observe_fields(payload: dict) -> None:
    """一次性自检：Node 产出的观察字段与 Python 实际消费的字段是否对得上。

    这类问题的特征是**全部静默** —— 没有任何报错，只是 LLM 少看到一类事实，然后它自己编因果。

    只在第一次拿到 payload 时跑（字段集合是固定的，每步查纯属浪费），发现问题打 WARNING
    而不抛异常 —— 多一个字段不影响跑，但必须让人看见。
    """
    global _audited_once  # pylint: disable=global-statement
    if _audited_once or not payload:
        return
    _audited_once = True
    produced = {k for k in payload if isinstance(k, str)}
    orphan = produced - _RENDERED_FIELDS - _IGNORED_FIELDS  # 采了但没人用 —— 可能又漏渲染了
    missing = _RENDERED_FIELDS - produced                    # 想渲染但 Node 没给
    if orphan:
        logger.warning(
            "[minecraft] 观察字段自检：Node 产出但 Python **从未消费** %s —— "
            "要么在 render_observation 里渲染它，要么从 index.js 的 obs.inject 里删掉，"
            "要么加进 _IGNORED_FIELDS 明确表态。静默不用 = LLM 少看到一类事实（本域已因此"
            "误判三次）", sorted(orphan),
        )
    if missing:
        logger.warning(
            "[minecraft] 观察字段自检：Python 期望消费但 payload 里**没有** %s —— "
            "渲染会静默退化成空/默认值，检查 index.js 的 obs.inject 是否漏注册插件",
            sorted(missing),
        )
    if not orphan and not missing:
        logger.info("[minecraft] 观察字段自检通过：产出与消费一致（%d 个字段）", len(produced))


def _parse_observe(raw) -> dict:
    """Voyager index.js 的 /start、/step 返回 JSON 字符串，形如
    [["observe",{...}], ["onChat",{...}], ...]。取最后一个 observe 的 payload。"""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return {}
    if isinstance(raw, dict):
        _audit_observe_fields(raw)
        return raw
    if isinstance(raw, list):
        for ev in reversed(raw):
            if isinstance(ev, (list, tuple)) and len(ev) == 2 and ev[0] == "observe":
                out = ev[1] or {}
                _audit_observe_fields(out)
                return out
    return {}


def _collect_events(raw) -> list[tuple[str, str]]:
    """抽出 chat / error / log 事件文本，供轨迹留痕 + 让 LLM 看到执行反馈（Voyager 错误反馈范式）。

    onLog = 生成代码里 console.log 的输出。LLM 天然爱用 console.log 汇报进度，
    以前这些输出写到 node stdout 被 DEVNULL 吞掉、100% 蒸发；现在 Node 侧劫持 console 收进
    onLog 插件回传，成为 LLM 自己设计的观测通道。

    注：onSystem（服务器死亡/成就播报）是**结构化 dict**，不走这里 —— 见 _collect_system_events。
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return []
    out: list[tuple[str, str]] = []
    if isinstance(raw, list):
        for ev in raw:
            if isinstance(ev, (list, tuple)) and len(ev) == 2 and ev[0] in ("onChat", "onError", "onLog"):
                payload = ev[1]
                text = payload.get(ev[0]) if isinstance(payload, dict) else str(payload)
                if text:
                    out.append((ev[0], str(text)))
    return out


def _collect_system_events(raw) -> list[dict]:
    """抽出 onSystem 的结构化服务器播报（死亡 / 成就）。见 mc_server/lib/observation/onSystem.js。

    与 _collect_events 分开的原因：这些不是给 LLM 直接读的文本，而是要**按 cause 聚合统计**的
    结构化事实（death: cause/killer/position/timeOfDay/nearby；advancement: advancement/cause）。

    去重：Node 侧每收到一条播报就 bot.event("onSystem") 发一次快照，而 base.js 的 bot.event 会把
    「当前 events 数组」整个塞进 cumulativeObs —— 但 observe() 会清空数组，所以同一步内多条播报
    表现为「前几个 onSystem 帧各带增量、最后由 observe 消费」。同一事件通常不会跨帧重复，这里仍按
    (kind, cause, text, position) 去重兜底，避免任何重复计数污染 n_deaths。
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return []
    out: list[dict] = []
    seen: set[tuple] = set()
    dropped = 0
    if isinstance(raw, list):
        for ev in raw:
            if not (isinstance(ev, (list, tuple)) and len(ev) == 2):
                continue
            payload = ev[1]
            if not isinstance(payload, dict):
                continue
            # onSystem 的 observe 结果会出现在 onSystem 帧里，也会随后续 observe 帧一起回传
            sysobs = payload.get("onSystem")
            if not isinstance(sysobs, dict):
                continue
            dropped = max(dropped, int(sysobs.get("dropped") or 0))
            for e in sysobs.get("events") or []:
                if not isinstance(e, dict):
                    continue
                pos = e.get("position") or {}
                key = (e.get("kind"), e.get("cause"), e.get("text"),
                       pos.get("x"), pos.get("y"), pos.get("z"))
                if key in seen:
                    continue
                seen.add(key)
                out.append(e)
    if dropped:
        # 不静默截断：Node 侧单步封顶 MAX_EVENTS 条，超出会丢。真到这一步说明异常（如反复自杀），
        # 必须显式说出来，否则统计会读成「就死了这么多次」。
        logger.warning("[minecraft] onSystem 单步事件超上限，丢弃 %d 条（死亡/成就统计可能偏低）",
                       dropped)
    return out


# 同一技能保留的失败报错样本上限（去重后）：防某个技能炸几百次把反思诊断块撑爆 token。
# ★ 2026-08-29 两阶段反思：阶段2（技能维护）每技能最多看 10 条失败样本（用户定调）。
_SKILL_ERR_SAMPLES = 10


def _collect_skill_events(raw) -> dict:
    """抽出 onSkill 的结构化技能调用埋点，返回**增量**计数 `{slug: {"ok": n, "fail": n, "errs": [...]}}`。

    见 mc_server/lib/observation/onSkill.js：技能代理 `bot.openkageSkill.push({slug,ok,err})`
    攒进缓冲，步末 flush 成 `["onSkill",{events:[...],dropped:N}]` 事件。本函数读这个结构化
    数组——**不走 console.log / onLog 字符串通道**（旧法 _parse_skill_call_events 靠
    `startswith("[skill] ")` + `endswith(" ok")` 反向解析日志行，两段代码靠字符串契约维持同步、
    任一端漂移就静默丢数；2026-08-26 改走机读事件通道，与 onSystem 同机制）。

    返回增量（只含本步新出现的计数，不含历史累计），调用方（_run_step_body）merge 进
    _state["skill_calls"]（累加语义），与旧 _parse_skill_call_events 同签名便于替换。

    与 _collect_system_events 同构：raw 可能是 str/list；onSkill 帧的 payload 是
    `{events:[{slug,ok,err}], dropped:N}`。

    ★ **errs 是失败样本**（2026-08-29）：失败时把 err（技能代理 push 的 `((e&&e.message)||e)`，
      可读单行消息）攥进 errs 列表，供反思 Agent 看到「哪个技能为什么失败」自己维护（见
      _reflect_exploration 的 skill_diagnosis）。同一报错文本去重、每技能最多留
      `_SKILL_ERR_SAMPLES` 条，防某个技能炸几百次把诊断块撑爆。成功调用不产生 errs。
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return {}
    inc: dict[str, dict] = {}
    if isinstance(raw, list):
        for ev in raw:
            if not (isinstance(ev, (list, tuple)) and len(ev) == 2):
                continue
            payload = ev[1]
            if not isinstance(payload, dict):
                continue
            skobs = payload.get("onSkill")
            if not isinstance(skobs, dict):
                continue
            for e in skobs.get("events") or []:
                if not isinstance(e, dict):
                    continue
                _slug = str(e.get("slug") or "")
                if not _slug:
                    continue
                _slot = inc.setdefault(_slug, {"ok": 0, "fail": 0, "errs": []})
                if e.get("ok"):
                    _slot["ok"] += 1
                else:
                    _slot["fail"] += 1
                    err_msg = str(e.get("err") or "")[:200].strip()
                    if err_msg and err_msg not in _slot["errs"] \
                            and len(_slot["errs"]) < _SKILL_ERR_SAMPLES:
                        _slot["errs"].append(err_msg)
    return inc



# 死因 translate key → 中文短标签（供「按死因聚合」的可读输出）。
# key 来自服务器权威播报（death.attack.mob / death.fell.accident.generic ...），见 onSystem.js。
# 未收录的 key 直接退化显示原 key（不猜、不丢）。
_DEATH_CAUSE_LABELS = {
    "death.attack.mob": "被怪物击杀",
    "death.attack.player": "被玩家击杀",
    "death.attack.arrow": "被箭射杀",
    "death.attack.explosion": "被爆炸炸死",
    "death.attack.explosion.player": "被爆炸炸死",
    "death.attack.fireball": "被火球击杀",
    "death.attack.lava": "掉进岩浆",
    "death.attack.inFire": "烧死",
    "death.attack.onFire": "被火烧死",
    "death.attack.drown": "溺水",
    "death.attack.starve": "饿死",
    "death.attack.cactus": "被仙人掌刺死",
    "death.attack.fall": "摔死",
    "death.fell.accident.generic": "摔死",
    "death.fell.accident.ladder": "从梯子上摔死",
    "death.attack.outOfWorld": "掉出世界",
    "death.attack.magic": "被魔法伤害击杀",
    "death.attack.indirectMagic": "被间接魔法击杀",
    "death.attack.sweetBerryBush": "被甜浆果丛刺死",
    "death.attack.sting": "被蜇死",
    "death.attack.thorns": "被荆棘反伤致死",
    "death.attack.anvil": "被铁砧砸死",
    "death.attack.fallingBlock": "被坠落方块砸死",
}


def _death_label(cause: str) -> str:
    """死因 key → 可读标签（未收录则原样返回 key，便于发现新死因）。"""
    return _DEATH_CAUSE_LABELS.get(cause, cause or "未知死因")


def _death_line(d: dict) -> str:
    """一次死亡渲染成一行：死因 + 凶手 + 死在哪 + 当时旁边有什么 + 持有量对比。

    位置/附近生物不在服务器播报里（播报只有「谁被谁杀了」），是 onSystem.js 死亡时额外快照的
    —— 「在 y=12 挖矿被苦力怕炸死、旁边有 creeper」才能沉淀成可复用的 pitfall，光「死了一次」不能。

    ★ 持有量对比（死前→重生后，2026-08-24 加）：反思 Agent 看不到执行期的死亡 onError
    反馈（反思轨迹用的是当步 world.step 原始 events，不含 `_system_feedback` 后拼的 onError），
    而 reflect.yaml 旧 prompt 又写死「死亡会掉落全部物品」——两头叠加让 erin 把「放置方块用光
    原木 + 当步死亡」误记成「死亡掉光物品」并污染公共脑。把死前→重生后 diff 渲进行里，反思
    Agent 就有事实可看、不必瞎猜。diff 口径见 `_holdings_diff`（库存+装备槽）；跨步积压的死亡
    （freeze/resume 期间被打死）拿不到准确死前库存，holdings_before 为 None 时不显示 diff。
    """
    parts = [_death_label(d.get("cause"))]
    killer = d.get("killer")
    if killer and killer != "unknown":
        parts.append(f"凶手 {killer}")
    pos = d.get("position") or {}
    if pos:
        parts.append(f"位置 x={pos.get('x')} y={pos.get('y')} z={pos.get('z')}")
    nearby = "、".join(d.get("nearby") or []) or "无"
    parts = [p for p in parts if p]
    line = "，".join(parts) + f"，附近：{nearby}"
    hb, ha = d.get("holdings_before"), d.get("holdings_after")
    if hb is not None and ha is not None:
        line += f"，持有量（死前→重生后）：{_holdings_diff(hb, ha)}"
    return line


def _summarize_deaths(deaths: list[dict]) -> str:
    """按死因聚合成一行，如「被怪物击杀×3(zombie×2, skeleton×1)、摔死×1」。
    死因分类直接用服务器 translate key（不猜文案），凶手用播报里的 with[1]。"""
    if not deaths:
        return ""
    from collections import Counter, defaultdict
    by_cause: dict[str, list[str]] = defaultdict(list)
    for d in deaths:
        killer = (d.get("killer") or "").strip()
        by_cause[d.get("cause") or ""].append(killer)
    parts = []
    for cause, killers in sorted(by_cause.items(), key=lambda kv: -len(kv[1])):
        label = f"{_death_label(cause)}×{len(killers)}"
        named = Counter(k for k in killers if k and k != "unknown")
        if named:
            label += "(" + ", ".join(f"{k}×{n}" if n > 1 else k for k, n in named.most_common()) + ")"
        parts.append(label)
    return "、".join(parts)


def _accumulate_xp(total: int, last: int | None, now: int | float | None) -> tuple[int, int | None]:
    """累计 XP 的纯函数记账规则（score 数据源，2026-08-25）。

    MC 死亡界面的"得分"= 自上次死亡以来挣的 XP；重生后服务器压低
    `bot.experience.points`。我们要的是**跨命累计总 XP**（死亡不扣减），故只把
    **正增量**加进 total —— 死亡产生的负增量（points 被压低）自动跳过，total 天然
    单调递增、跨命累计，无需在死亡点做重置。

    - last=None（首步）：仅记录基线，不产生增量。
    - now 读不到(None)/非数：跳过，原样返回（不污染累计，老存档/异常时退化）。
    - now 为浮点：转 int（XP 点数取整，与 MC 口径一致）。

    抽成纯函数是为了可测：run_exploration 里的 _track 闭包调它，测试直接验规则
    而不必起一个 MC 跑。返回 (新total, 新last)。
    """
    if not isinstance(now, (int, float)):
        return total, last
    now_i = int(now)
    if last is not None:
        delta = now_i - last
        if delta > 0:
            total += delta
    return total, now_i


#: 记分板 objective 名 → 准则的映射（与 index.js /start 里建的 6 项一一对应）。
#: Python 侧用这个固定映射从 observe.scoreboard 提取权威值，避免依赖准则名（observe 里不带准则，
#: 只带 objective 名 + scores）。改名要同步改 index.js 的 add 循环。
_SCOREBOARD_OBJ_TO_KEY = {
    "oct_death": "deathCount",
    "oct_kill": "totalKillCount",
    "oct_xp": "xp",
    "oct_health": "health",
    "oct_food": "food",
    "oct_armor": "armor",
}


def _extract_scoreboard(sb: dict | None, bot_name: str | None) -> dict:
    """从 observe 的 scoreboard 字段提取 {准则: 值} 扁平 dict（交叉校验旁证，2026-08-26）。

    sb 形如 {"objectives": {<objName>: {"name","title","scores": {<holder>: value}}}}（见
    mc_server/lib/observation/scoreboard.js）。我们要的是**bot 自己**在每个准则下的分。本实验每服
    单 bot，scores 里通常只有一个 holder（就是 bot 名），故取法：先匹配 bot_name，否则取唯一 holder，
    否则取最大值（多 holder 罕见，取最大是「bot 至少达到这个分」的保守下界，不抛）。

    - sb 为空/缺 objectives：返回全 None 的 dict（6 个准则键都在，值 None）—— 缺记分板不污染落盘，
      事后看全 None 即知「这步没读到记分板」（spawn 早期/续跑首步 score 包未到）。
    - value 非 number（含 null）：记 None，不抛。
    - bot_name 为 None：退化成「取唯一 holder / 取最大」。

    ★ **口径差（预期，非 bug，别在测试里断言相等）**：记分板 `xp` = 当前持有经验值（重生被服务器
      压低）；env 的 `xp_total` = 跨命累计正增量（死亡不扣减）。两者只在「本命内」相等，死过必分叉。
      deathCount 与 n_deaths、health/food 与 dawn_status 同源应相等；totalKillCount/armor 是 env
      现无自统计的新信号（记分板独有）。本函数只如实提取，不做任何对齐/比对——比对留事后人工。
    """
    out = {k: None for k in _SCOREBOARD_OBJ_TO_KEY.values()}
    if not isinstance(sb, dict):
        return out
    objs = sb.get("objectives") or {}
    for obj_name, key in _SCOREBOARD_OBJ_TO_KEY.items():
        obj = objs.get(obj_name)
        if not isinstance(obj, dict):
            continue
        scores = obj.get("scores") or {}
        if not isinstance(scores, dict) or not scores:
            continue
        if bot_name and bot_name in scores and isinstance(scores[bot_name], (int, float)):
            out[key] = scores[bot_name]
            continue
        # 非 number 的 value 过滤掉再选（bot_name 不匹配/其值非数时走这里）
        nums = {h: v for h, v in scores.items() if isinstance(v, (int, float))}
        if not nums:
            continue
        if len(nums) == 1:
            out[key] = next(iter(nums.values()))
        else:
            out[key] = max(nums.values())
    return out


def _extract_advancement_packet(pkt: dict | None) -> dict:
    """从 observe 的 advancementPacket 字段提取「已完成进度」佐证快照（2026-09-07）。

    pkt 形如 {"completed":["[Minecraft]",...], "completedKeys":["minecraft:story/root",...],
    "storyTreeSeen":bool}（见 mc_server/lib/observation/advancementPacket.js）。`completed` 是
    packet_advancements 协议包维护的「全部 criterion 达成」的进度显示名集合，与 onSystem 被动播报
    (advancements_so_far) 并排双值落盘。

    ★ 与 onSystem 的口径差（预期非 bug，不比对不修正，同 _extract_scoreboard）：
      ① announce_to_chat 维度：协议包能采到 announce_to_chat=false 的静默进度（story/root=
         [Minecraft]），onSystem 采不到——这正是开本支路的动机。
      ② 懒推送维度：story 树（含 root）要等 bot 达成至少一个 story criterion 才推送；bot 整天
         没碰 story 进度时 storyTreeSeen=false、completed 里无 root，是「没收到包」非「没达成」。
      ③ 只落盘双值、不比对、不修正；差异分析事后人工/脚本做。

    - pkt 缺/非 dict：返回空形态 {"completed":[], "storyTreeSeen":False}，不污染落盘。
    """
    if not isinstance(pkt, dict):
        return {"completed": [], "storyTreeSeen": False}
    completed = pkt.get("completed")
    if not isinstance(completed, list):
        completed = []
    story_tree_seen = bool(pkt.get("storyTreeSeen"))
    return {"completed": list(completed), "storyTreeSeen": story_tree_seen}


def union_advancement_sources(o: dict | None, sys_events: list[dict] | None,
                              seen: set[str]) -> tuple[list[str], set[str], set[str]]:
    """成就两路源头并集的纯函数（2026-09-14，可单测，tests/test_advancement_feedback_dedupe.py）。

    两路采集在此并集再对 seen diff：
      ① packet 协议包：o["advancementPacket"].completed 步末快照全集（采 announce=false 的
         静默 root）；
      ② onSystem 聊天播报：sys_events 里 kind=="advancement" 的事件（采 announce=true 的子进度
         如 fishy/plant_seed；packet 失效期是它们的唯一来源）。
    同一成就两路都报时并集后只出现一次（不双计）。

    - o: 步末观察 dict（含 advancementPacket 字段）；None/缺字段按空 packet 处理。
    - sys_events: _collect_system_events 抽出的 onSystem 事件（含 death+advancement，函数内部
      只挑 kind=="advancement"）。
    - seen: 总成就变量 _seen_advancements 的当前值（diff 基线，跨批累积）。
    返回 (new_advs, pkt_set, onsys_set)：
      new_advs = sorted((pkt|onsys) - seen)  本批新增（记账/反馈用）；
      pkt_set / onsys_set = 两路各自的集合（来源标注用，调用方 `seen |= pkt|onsys` 推进）。
    """
    pkt = set(_extract_advancement_packet(
        (o or {}).get("advancementPacket"))["completed"])
    onsys = {e.get("advancement") for e in (sys_events or [])
             if isinstance(e, dict) and e.get("kind") == "advancement"
             and e.get("advancement")}
    new_advs = sorted((pkt | onsys) - seen)
    return new_advs, pkt, onsys


#: 观战相机脱附判定阈值（格）：附身时观战者坐标恒 ≈bot（服务器逐刻写），阈值放宽到 32 容纳
#: 探针回执与步末观察之间的时间差里 bot 的正常移动（2.5 速下 5s 可走 ~20 格）；脱附后相机
#: 冻在脱附点、bot 继续活动，距离很快超阈。宁松勿紧：误触发一次白付 10~15s xdotool 命令链。
_SPECTATOR_DETACH_DISTANCE = 32


def _spectator_needs_reattach(payload: dict | None) -> bool:
    """观战相机看门狗的判据（2026-09-15，纯函数，可单测）。

    payload 是**本步 world.step 返回**的观察（不是闭包里的旧 obs）——探针读数要与「此刻相机
    位置」同步，用步初的旧观察会把已修好的脱附误判成仍脱附。形如
    {"spectatorCam": {"pos": {x,y,z}|None, "distance": number|None, "probeStale": bool,
    "botPos": {x,y,z}|None}}，见 mc_server/lib/observation/spectatorCam.js（bot 侧周期发
    /data get entity <spec> Pos，服务器权威）。

    - distance > 阈值（观战者离 bot 远——附身时服务器每刻把观战者坐标写成 bot 位置，距离恒
      ≈0；脱附后观战者冻在脱附点、bot 走开，距离拉开）→ True，该重附。
      覆盖三条既有防线漏掉的路径：bot 断连自愈重连（实体移除→脱附，**无死亡播报**）、
      栅栏失效的步间死亡、观战客户端卡死前的画面漂移。
    - probeStale=True（探针刚发了查询还没收到回执）或 pos/distance 缺失 → False：
      「不判定」不等于「脱附」，误触发一次要白付 10~15s 的 xdotool 命令链，宁缺勿滥。
      未配观战名（对照组）恒落这里。探针回执有 5s 节流窗口，步长 <5s 时读到的是上一次
      回执的缓存——缓存最多旧一步长+5s，脱附检测延迟可接受。
    """
    cam = (payload or {}).get("spectatorCam") if isinstance(payload, dict) else None
    if not isinstance(cam, dict):
        return False
    if cam.get("probeStale") or not isinstance(cam.get("pos"), dict):
        return False
    d = cam.get("distance")
    return isinstance(d, (int, float)) and d > _SPECTATOR_DETACH_DISTANCE


#: 单步水平位移超过这个格数就判定为**重生/传送瞬移**，不计入 path_len（见 _summarize_path）。
#: 取值理由：单步真实移动量受 step 超时约束（默认 25s，跑步约 5.6 格/秒 ≈ 140 格），但超时
#: 可放宽到 500s 且寻路可能绕远，故留足余量取 400 —— 这个方向上宁可漏剔一次长途跋涉
#: （少量高估路程），也不要把一次重生瞬移（几百到上千格）算成"走出来的距离"。
#: ⚠️ 改这个值 = 改 path_len 的指标定义，跨 run 的曲线不再可比。
_TELEPORT_JUMP_BLOCKS = 400.0


def _summarize_path(trajectory: list[dict], spawn: dict | None,
                    biomes: set[str]) -> dict:
    """从逐步坐标算 **map coverage** 三件套（对标 Voyager「distance traveled 2.3×」那一项）。

    产出：
      - `path_len`：相邻步位移的**水平**折线长度（格）
      - `max_dist_from_spawn`：**靠走**最远离开过立足点多远（格）。分段口径见下方 ⚠️ ——
        名字里的 spawn 只对第一段成立，重生后按各自落点为锚（改名会破坏已落盘曲线的可比性，
        故保留原名并在此说明）
      - `n_biomes` / `biomes`：走过的生物群系种类

    ★ **为什么必须报两个距离而不是只报 path_len**：Voyager 报的是「走了多远」，但那个数
      **奖励原地绕圈**。本域一条轨迹会死几十次（实测 30 夜死 68 次），死一次就重生一次，
      「跑回死亡点捡装备」的往返会把 path_len 刷得很高，而 bot 其实一直在同一小片地方。
      `max_dist_from_spawn` 不受往返影响 —— 两者并列才分得清「真的走远了」和「来回跑」。
      两个数一起看还能当死亡代价的旁证：path_len 高而 max_dist 低 = 大量步数花在往返上。

    ★ **只算水平距离（忽略 y）**：竖直位移的语义完全不同 —— 挖竖井下矿 60 格不该被记成
      「探索了 60 格地图」，那是深度不是广度，已经由 tier/items 表达了。Voyager 的 map
      coverage 也是俯视图上的覆盖。

    ★ **死亡重生的瞬移必须剔除**（否则这个指标是垃圾）：重生把 bot 拉回出生点，相邻两步的
      坐标差可能是几百格，直接累加会凭空多出几百格「路程」——死得越多、数字越漂亮，
      指标方向完全反了。判据用**位移阈值**而不是「这一步有没有死亡记录」：死亡播报是跨步
      积压的（见 onSystem.js），死亡与瞬移不一定落在同一步。单步能真实移动的上限受 25s
      默认超时约束（`_STEP_TIMEOUT_DEFAULT_TICKS`，1x 下 25s、跑步约 5.6 格/秒 ≈ 140 格），但超时
      可放宽到 3000 刻（2x 下 75s ≈ 420 格）、寻路也可能走很远，故阈值取 `_TELEPORT_JUMP_BLOCKS`（400 格）
      —— 宁可漏剔一次
      长途跋涉，也不要把重生瞬移算成路程。被剔除的段数一并返回，落进 metrics：
      **静默丢弃是不可接受的**，跳变次数本身就是「这条轨迹死了多少次/传送了多少次」的旁证。

    `spawn` 为 None（拿不到出生坐标）时 `max_dist_from_spawn` 报 None 而不是 0 ——
    0 会被误读成「一步没走」，而真相是「不知道」。
    """
    pts = [p for p in (rec.get("pos") for rec in trajectory)
           if isinstance(p, dict) and isinstance(p.get("x"), (int, float))
           and isinstance(p.get("z"), (int, float))]
    # ★ 按**瞬移**把轨迹切成若干「连续行走段」：一次重生/传送开一段新的。path_len 只累加段内
    #   位移，max_dist 只在段内相对**该段起点**取最大（见下面 max_dist 那处的 ⚠️）。
    segments: list[list[dict]] = [[]] if pts else []
    n_jumps = 0
    for i, p in enumerate(pts):
        if i and math.dist((pts[i - 1]["x"], pts[i - 1]["z"]), (p["x"], p["z"])) > _TELEPORT_JUMP_BLOCKS:
            n_jumps += 1        # 重生/传送，不是走出来的 → 从这里开新段
            segments.append([])
        segments[-1].append(p)
    path_len = sum(
        math.dist((a["x"], a["z"]), (b["x"], b["z"]))
        for seg in segments for a, b in zip(seg, seg[1:])
    )
    # ⚠️ **锚点按段各自算，不能一律用出生点**（2026-08-17 smoke 实测抓到的真 bug）：
    #    frozen 基线那条轨迹 —— bot 在 (433,-754) 出生、游进海里溺死、被重生到世界原点
    #    (-1.3, 7.5)。若一律拿出生点做锚，`max(dist(spawn, p))` = **877 格**，而它实际只走了
    #    9 格（path_len=9.1、n_pos_jumps=1 都对）。于是这条"一步没走成、淹死在原地"的轨迹
    #    会被报成本轮探索范围最大的一条 —— 而这正是本指标要防的那种误读，只是换了个入口。
    #    改成「每段相对**自己的起点**取最大，再对段求最大」：语义是「**靠走**最远离开过某个
    #    立足点多远」，与 path_len 的分段口径一致，也不受重生落点位置影响。
    #    第一段的起点就是出生点（若 spawn 可用则用它，更精确——首个采样点已经是 step0 之后了）。
    # ★ 有段但**拿不到出生坐标**时仍报 None（不是 0）：0 的字面意思是「一步没离开立足点」，
    #   而真相是「不知道锚在哪」。两者读起来一样、含义完全相反（见本函数末段说明）。
    #   注意这与「重生后各段自带锚点」不冲突：第一段没有可信锚点，整条轨迹的"离开多远"
    #   就无从谈起（后续段的位移相对的是重生点，不是探索起点）。
    max_dist = None
    if segments and spawn and isinstance(spawn.get("x"), (int, float)):
        max_dist = 0.0
        for k, seg in enumerate(segments):
            anchor = seg[0]
            # 第一段用出生点做锚更精确（首个采样点已经是 step0 执行之后了，bot 可能已走开）。
            # ⚠️ 但**出生点到首个采样点本身也可能是一次瞬移**：死亡发生在第 1 步时，
            #    `spawn` 是死前的出生坐标、`seg[0]` 已是重生落点，两者相距几百格，而它们之间
            #    没有中间采样点 —— 上面那个 i>=1 的循环看不到这一跳（smoke 实测就栽在这里：
            #    修了分段仍报 878 格）。所以这里对 spawn 锚**同样过一遍阈值**，
            #    超了就退回用 seg[0]（那才是这段行走真正的立足点）。
            if k == 0:
                if math.dist((spawn["x"], spawn["z"]), (seg[0]["x"], seg[0]["z"])) \
                        <= _TELEPORT_JUMP_BLOCKS:
                    anchor = spawn
                else:
                    n_jumps += 1   # 出生点→首采样点这一跳也如实计数，别静默吞掉
            max_dist = max(
                [max_dist] + [math.dist((anchor["x"], anchor["z"]), (p["x"], p["z"])) for p in seg]
            )
    return {
        "path_len": round(path_len, 1),
        "max_dist_from_spawn": round(max_dist, 1) if max_dist is not None else None,
        "n_pos_jumps": n_jumps,
        "n_biomes": len(biomes),
        "biomes": sorted(biomes),
        # 有坐标的步数 / 总步数：明显小于 1 就说明 observe 的 position 有缺失，
        # 上面两个距离会被低估 —— 不报这个，低估是静默的。
        "n_pos_samples": len(pts),
    }


def tech_tier(inventory: dict, equipped: set[str] | None = None,
              advancements: set[str] | None = None) -> str:
    """判当前科技树层级名。

    ★ 2026-09-05 改：优先按 **Minecraft 进度树深度**（9 级）算——从 advancements 取已达成的
    story.* 进度，查 ``_ADV_DEPTH`` 得最深深度，映射到 9 级 tier 名（workbench..end）。
    这比旧"查库存工具"更能反映"跨入新科技阶段"（进下界、附魔等不体现为工具材质）。

    advancements: agent 已达成的进度显示名集合（如 ``{"[Diamonds!]", "[Stone Age]"}``），
    由调用方从 env 的 advancements 状态列表提取。**传了就按深度算**。

    回退：advancements 为空（第 1 天还没达成任何进度）时，回退旧"查库存工具材质"逻辑
    （wooden/stone/iron/diamond），保证第 1 天早期不报 none 之外的状态能被识别。

    inventory/equipped: 回退逻辑用，同旧语义。
    """
    if advancements:
        depths = [_ADV_DEPTH[a] for a in advancements if a in _ADV_DEPTH]
        if depths:
            return _DEPTH_TIER[max(depths)]
    # 回退：按库存工具材质（旧逻辑，兼容无 advancement 的早期）
    names = set(inventory or {}) | set(equipped or ())
    for tier_name, markers in TECH_TIERS:
        if names.intersection(markers):
            return tier_name
    return "none"


def tool_tier_name(inventory: dict, equipped: set[str] | None = None) -> str:
    """按「已造出的最高级成品工具」结算的 4 级层级名（wooden/stone/iron/diamond/none）。

    ★ 这是**旧口径**（2026-09-05 前 tech_tier 的唯一逻辑），现在单独保留给观察字段用：
    与 9 级进度树口径（progress_frontier / base_depth，单调）**并列展示、互不替代**——
    它按库存+装备里此刻有没有该级工具成品判定，工具用完断裂后会**回退**（非单调），
    能让 Agent 看到自己「此刻手里最高级的工具是什么」（石镐断了就回到木级）。

    判定物与 tech_tier 回退分支完全一致：五种工具（pickaxe/sword/axe/shovel/hoe）×四级
    材质，精确匹配物品名，不计护甲/金工具（见 _TIER_MATERIALS / TECH_TIERS 注释）。
    **不传 advancements**，故绝不走 9 级深度分支——这是它与 tech_tier 的关键区别。
    只报事实（你当前最高级工具），不给路线（不报「该造什么」），符合开放式原则。
    """
    names = set(inventory or {}) | set(equipped or ())
    for tier_name, markers in TECH_TIERS:
        if names.intersection(markers):
            return tier_name
    return "none"


def progress_frontier(advancements: set[str] | None) -> str:
    """渲染 5 棵进度树各自的「当前前沿」给 Agent 看（只报事实，不给路线）。

    前沿 = 该树已达成进度里 **depth 最高的那些**（同 depth 并列，不取唯一）。
    每棵树一行片段：``主线 L4·[Acquire Hardware]``；同层多个空格并列：
    ``主线 L6·[Diamonds!] [Ice Bucket Challenge] [Not Today, Thank You]``；
    该树一个进度都没达成则 ``主线 None``。

    ★ **只报已拥有、不报未拥有/缺口/下一步**——这是开放式探索的硬约束：给出「还差什么」
    就等于塞攻略。Agent 看到的是自己已走过的脚印，看不到终点与路径，方向由它自己定。
    对照组/实验组都看得到自己的前沿（本地状态，公平）；跨臂共享靠图谱 milestone facet
    （实验组独有变量），不在本函数。

    advancements: agent 已达成的进度显示名集合（带方括号英文标题，与 onSystem.js 采集同源）。
    未知显示名（如 lang 未收录的 husbandry.uh_oh）静默跳过——不报错，也不计入前沿。

    返回拼好的单行字符串（不含前缀标签，由 render_observation 加「进度：」头）。
    空集合返回全 None 的五段（第 1 天早期常态）。
    """
    # 按树归集 (depth, 显示名)
    by_tree: dict[str, list[tuple[int, str]]] = {t: [] for t, _ in _ADV_TREE_ORDER}
    for disp in (advancements or ()):
        info = _ADV_INFO.get(disp)
        if info is None:
            continue  # 未知显示名跳过（lang 缺口或翻译变动）
        tree, depth = info
        by_tree.setdefault(tree, []).append((depth, disp))
    parts = []
    for tree, zh in _ADV_TREE_ORDER:
        items = by_tree.get(tree) or []
        if not items:
            parts.append(f"{zh} None")
            continue
        top = max(d for d, _ in items)
        names = [disp for d, disp in sorted(items) if d == top]
        parts.append(f"{zh} L{top}·" + " ".join(names))
    return "  ".join(parts)


def base_depth(advancements: set[str] | None) -> int:
    """打分用：base（story）进度树的当前深度，0-9。

    = base 树已达成进度里最高的 depth（root=1，进末地=9）；一个都没达成返回 0。
    取代旧 tech_tier 在打分链的角色：tier_progress 现存这个整数（不再存 9 级 tier 名），
    tier_rank = 沿轨迹高水位 = max(tier_progress)，无需 _TIER_RANK_9 查表。

    ★ 与 progress_frontier 同源同口径（都查 _ADV_INFO 的 depth），只是本函数只取 base 一棵
    树、且返回整数供打分比大小；前沿渲染给 Agent 看，本函数给打分用。Agent 看不到本函数。

    advancements: agent 已达成的进度显示名集合（带方括号英文标题）。未知显示名跳过。
    """
    depths = [info[1] for disp in (advancements or ())
              if (info := _ADV_INFO.get(disp)) and info[0] == "base"]
    return max(depths) if depths else 0


#: 3×3×3 邻域渲染时的方位顺序（固定，保证每步观察文本布局稳定、便于模型形成定式）。
#: 先四正向再四斜角 —— 正向是能直接走/挖的方向，斜角在 MC 里过不去（要拐两下），优先级低。
_GRID_DIRS = ("-x", "+x", "-z", "+z", "-x-z", "-x+z", "+x-z", "+x+z")


def render_nearby_grid(grid: dict) -> list[str]:
    """把 NearbyGrid 的 3 层 × 25 格渲染成给 LLM 看的文本行。

    为什么需要它：只回答「附近有哪些种类」的扁平集合不回答「**哪个方向**是什么」——
    bot 因此不知道哪边能走、脚下踩着什么、头顶有没有封顶，只能靠 console.log 逐格试探。

    三层的语义（bot 高约 1.8 格，占 feet 与 head 两层的中心格）：
    - `head`  y+1：头部所在层
    - `feet`  y+0：脚所在层（水平移动发生在这一层）
    - `below` y-1：支撑层（`center` 即脚下踩着的方块）

    格数是 **27-2=25** 而非直觉的 27-1=26：bot 高约 1.8 格，**占 feet 与 head 两层的
    中心格**（不是一格）。below 的中心格是脚下支撑方块，保留。

    ★ **只报事实，不加判断**：不给 feet 层标 `(可通行)`、below 层标 `(⚠悬空)` 这类标注——
    那类标注是在替模型下它自己该下的结论，而依据并不充分：「下一格是空气」不等于「会摔死」，
    可能只低一格（台阶，走过去毫无风险），也可能是深渊，而本网格只到 y-1、**看不到 y-2 及
    更下方**，凭 y-1 一格标「悬空」是拿没有的信息下断言。同理「是空气」也不必然「可通行」
    （半砖、栅栏、水、岩浆各有各的规则）。方块名本身已是完整事实，怎么解读交给模型
    （要确认深度它可以自己 `bot.blockAt` 往下探）。
    """
    if not grid:
        return []
    lines = ["【周围 3×3×3（25格，绝对方位；y+1头顶层 / y+0身体层 / y-1脚下层）】"]
    for layer, label in (("head", "y+1 头顶层"), ("feet", "y+0 身体层"), ("below", "y-1 脚下层")):
        cells = grid.get(layer) or {}
        if not cells:
            continue
        parts = []
        # 中心格只在 below 层有意义（脚下支撑方块）——feet/head 的中心格是 bot 自己。
        if layer == "below" and cells.get("center"):
            parts.append(f"正下方={cells['center']}")
        for d in _GRID_DIRS:
            name = cells.get(d)
            if name:
                parts.append(f"{d}={name}")
        lines.append(f"  {label}：{'  '.join(parts)}")
    return lines


#: 视野扇区的渲染顺序（固定，保证每步文本布局稳定）。上/平/下 × 左/中/右。
#: 「平」层排最前 —— 那是 bot 实际会走过去的那一层，信息价值最高。
#: 键是**绝对轴向 + 垂直层**（与 nearbyGrid / exploreUntil 的 Vec3 同一坐标系；
#: 视野只 ±60°，一次通常只覆盖其中 2-3 个轴向，其余是盲区）。
_VIEW_SECTORS = tuple(f"{a}{v}" for v in ("平", "上", "下") for a in ("-x", "+x", "-z", "+z"))
_VIEW_TOP_K = 3


def render_vision(vision: dict) -> list[str]:
    """把 Vision 的 9 扇区射线命中聚合成文本行。

    与 `render_nearby_grid` 互补：贴身 25 格回答「脚边能不能走」，
    这里回答「**朝向那边远处有什么、值不值得走过去**」。原有观察缺这一环 —— bot 只能靠
    exploreUntil 盲目走一段再看。

    **是真视觉、不是全知**：Node 侧用 `bot.world.raycast` 从眼睛投射，命中第一个实心方块即停，
    所以被山挡住的树看不到。这与 nearbyGrid 的穿墙全知是两种语义，别混。

    压缩方式（533 条射线 → 几行）：按**每条射线朝的绝对轴向** × 垂直层(上/平/下)归类，
    每区按命中占比取前 %d 种方块，报「名字 ×占比(最近-最远格)」。开阔（射线一路没撞到东西）
    单独报 —— 「那个方向是空的」本身是重要信息。

    ★ **扇区用绝对方位而非左/中/右**：网格用绝对轴向、视野用相对朝向时，模型要自己拿 yaw
    换算才能对上两套观察，而 bot 的 yaw 常是斜的（它从不主动 bot.look，朝向只是寻路/挖掘的
    副作用）→ 换算必错，"视野里偏左那棵树"与推算坐标对不上、走过去就歪。
    统一到绝对方位后，`-x平` 这个键在任何朝向下都指同一个世界方向，可直接写成 Vec3(-1,0,0)。

    ★ 与本文件其他渲染一致：**只报事实，不下判断**（不标「可去/危险」之类）。
    """ % _VIEW_TOP_K
    if not vision:
        return []
    sectors = vision.get("sectors") or {}
    if not sectors:
        return []
    rng = vision.get("range", "?")
    # 朝向要**连真实角度一起报**：只报最近的轴向名会掩盖最多 44° 的偏差，而 bot 从不主动
    # bot.look、yaw 全是寻路/挖掘的副作用，几乎总是斜的。模型据"朝向 -x"去算坐标就会算歪 ——
    # 必须让它知道自己歪了多少。
    axis = vision.get("axis") or vision.get("facing", "?")   # facing 是旧字段名，兼容一下
    yaw_deg, off = vision.get("yawDeg"), vision.get("offAxisDeg")
    if yaw_deg is None:
        facing_txt = f"朝向 {axis}"
    elif not off:
        facing_txt = f"朝向 {axis}（yaw={yaw_deg}°，正对该轴向）"
    else:
        facing_txt = (f"朝向 yaw={yaw_deg}°，**偏离 {axis} 轴 {off}°**"
                      f"（不是正对轴向；要正对可 bot.look 对齐）")
    lines = [f"【视野（{facing_txt}；水平±60° 垂直±30°，最远 {rng} 格；被遮挡的看不到）】"]
    for key in _VIEW_SECTORS:
        s = sectors.get(key)
        if not s:
            continue
        total = s.get("total") or 1
        blocks = s.get("blocks") or {}
        sky = s.get("sky", 0)
        parts = []
        if sky:
            parts.append(f"开阔{round(100 * sky / total)}%")
        # 按命中射线数降序 —— 占得多的就是那个方向的主体（山体/树林/地面）
        for name, rec in sorted(blocks.items(), key=lambda kv: -kv[1].get("n", 0))[:_VIEW_TOP_K]:
            pct = round(100 * rec.get("n", 0) / total)
            lo, hi = rec.get("min"), rec.get("max")
            span = f"{lo:g}格" if lo == hi else f"{lo:g}-{hi:g}格"
            parts.append(f"{name}{pct}%({span})")
        if not parts:
            continue
        # 最近的那个方块给出坐标：LLM 要走过去/挖它时可直接用，省一次 findBlock
        nearest = min((r for r in blocks.values() if r.get("pos")),
                      key=lambda r: r.get("min", 1e9), default=None)
        coord = ""
        if nearest and nearest.get("pos"):
            p = nearest["pos"]
            coord = f"  最近点({p['x']},{p['y']},{p['z']})"
        lines.append(f"  {key}：{'  '.join(parts)}{coord}")
    return lines


def _render_chests(chests: dict) -> str:
    """渲染 nearbyChests（16 格内的箱子）。

    数据形态（chests.js）：`{"(x, y, z)": "Unknown" | "Invalid" | {物品名: 数量}}`
    —— `Unknown` = 看到了但没开过，`Invalid` = 已被破坏。key 是 Vec3 的字符串形式。
    """
    if not chests:
        return "(无)"
    out = []
    for pos, items in chests.items():
        if items == "Invalid":
            continue  # 已被破坏的箱子不必再报
        if items == "Unknown" or not isinstance(items, dict):
            out.append(f"{pos}未查看")
        elif not items:
            out.append(f"{pos}空")
        else:
            brief = ", ".join(f"{k}x{v}" for k, v in list(items.items())[:4])
            more = "…" if len(items) > 4 else ""
            out.append(f"{pos}[{brief}{more}]")
    return "  ".join(out) if out else "(无)"


#: 装备槽的渲染顺序与中文名。键与 mineflayer `bot.equip(item, destination)` 的 destination
#: **同名**（status.js getEquipment 有意对齐），故渲染时把键一并写出来 —— LLM 照抄即可换装，
#: 不必再猜「胸甲那个槽在 equip 里叫什么」（正确答案 `torso` 并不好猜）。
#: 主手槽的键名。单独拎出来是因为它在**语义上和其他五个槽不同一档**：主手是快捷栏
#: （槽 36-44），**落在 `bot.inventory.items()` 的 9-44 区间内**，而护甲(5-8)/副手(45)在区间外。
#: 于是「集合去重」的用法（unique_items / tech_tier）并入主手无害，而「计数」的用法
#: （_holdings）并入主手就是重复计数 —— 见 equipped_item_names 的 exclude_main_hand。
_MAIN_HAND_SLOT = "hand"

_EQUIP_SLOTS = (
    ("head", "头"), ("torso", "胸"), ("legs", "腿"), ("feet", "脚"),
    (_MAIN_HAND_SLOT, "主手"), ("off-hand", "副手"),
)


def equipped_item_names(status: dict, *, exclude_main_hand: bool = False) -> set[str]:
    """装备栏里六个槽的物品名集合（空槽跳过）。

    ★ 存在的理由是**打分正确性**，不只是渲染：`unique_items` 与 `tech_tier`
    都只看 `inventory`，而 `bot.inventory.items()` 只覆盖槽 **9-44** —— 六个装备槽里，
    主手是快捷栏（36-44，**在**区间内，没问题），但**护甲 5-8 与副手 45 在区间外**。于是：

    - `unique_items`：穿上身的每一件护甲都不进 `items_seen` —— 造出并穿上 `iron_chestplate`
      这次发现**白干**。这是实打实在少算，且方向单一（越会用装备的 arm 被扣得越多）。
    - `tech_tier`：判定物是五种工具（见 TECH_TIERS），护甲不参与，主手又在区间内，
      所以只有「把 tier 工具塞进**副手**」这一种情形会漏判。不常见但确实可能
      （`bot.equip(sword, 'off-hand')` 是合法调用）。

    并入后两处都以「观察里能看到的物品都被计入」为准 —— 这也让打分与 bot 看到的事实一致：
    否则它看着装备行里的铁甲，而分数当它不存在。

    `exclude_main_hand`：**只有按「件数」累加的调用方才该传 True**（目前唯一一个是
    `_holdings`）。上面两个调用方是往 set 里并、天然去重，主手重复并入不影响结果；而
    `_holdings` 是 `out[name] += 1`，主手那件东西已经计在 inventory 里了，再加一遍就凭空
    多出一件，进而伪造出 `stone_pickaxe 2→0` 这种假的"整叠归零"。默认 False 保持前两个
    调用方的行为不变（它们依赖主手也在集合里 —— 副手漏判那条注释讲的就是这个）。
    """
    out = set()
    for slot, _ in _EQUIP_SLOTS:
        if exclude_main_hand and slot == _MAIN_HAND_SLOT:
            continue
        it = (status.get("equipment") or {}).get(slot)
        if isinstance(it, dict) and it.get("name"):
            out.add(it["name"])
    return out


# ── 工具/装备消失诊断的分类（2026-08-23 抽成纯函数便于单测）──────────────────────
# `_run_step_body` 里逐步 diff「库存+装备」持有量，减少的工具/装备按可疑度分流报日志。
# 抽出来的是**分类逻辑**（返回类别字符串），日志文案仍在调用方拼（文案改了不影响分类判定）。
# 守的是 keep_inventory=true 下的一个误导：此前「死了且 keep_inventory=true 且少工具」被混进
# 「未死」分支（因 `not _KEEP_INVENTORY` 为 False 走 else），文案却写「本步未死亡」—— bot 明明
# 死了却报没死，agent 困惑之下归因为「死亡掉落全部物品」记进经验层（见 death_drops_all_items）。
#: 死亡掉物属正常规则（仅 keep_inventory=false）
_TOOL_LOSS_DEATH_RULE = "death_rule"
#: 死亡且 keep_inventory=true 却少工具 = 真异常（keep_inventory 没生效或物品凭空消失）
_TOOL_LOSS_DEATH_KEEPINV_ANOMALY = "death_keepinv_anomaly"
#: 未死、主动丢弃（bot tossStack）
_TOOL_LOSS_TOSSED = "tossed"
#: 未死、挖矿耐久耗尽断裂
_TOOL_LOSS_DUG_BREAK = "dug_break"
#: 未死、无法解释的无声丢失
_TOOL_LOSS_SILENT = "silent"


def _classify_tool_disappearance(new_deaths: list, keep_inventory: bool,
                                 code: str, events: list) -> str:
    """把「本步工具/装备整叠归零」按可疑度分到一类，供 `_run_step_body` 的诊断日志选用。

    三组判据（与 `_run_step_body` 里的分支一一对应，文案在那边、分类在这里）：
      · 死了：keep_inventory=false → 正常规则；keep_inventory=true → 真异常。
      · 未死 + 主动丢弃（代码含 tossStack/bot.toss 或反馈含「丢弃」）→ 正常。
      · 未死 + 挖矿耐久断裂（挖掘原语 + 「挖掉/挖了」播报）→ 正常。
      · 未死 + 无法解释 → 无声丢失（真异常，留 WARNING）。

    `new_deaths` 非空即视为本步死过（跨步积压的死亡也算，见 `_record_system`）。
    """
    if new_deaths:
        return _TOOL_LOSS_DEATH_RULE if not keep_inventory else _TOOL_LOSS_DEATH_KEEPINV_ANOMALY
    _ev_join = " ".join(t for _, t in events)
    _code_lc = (code or "").lower()
    if "tossstack" in _code_lc or "bot.toss" in _code_lc or "丢弃" in _ev_join:
        return _TOOL_LOSS_TOSSED
    if ("digupto" in _code_lc or "mineblock" in _code_lc or "bot.dig" in _code_lc) \
            and ("挖掉" in _ev_join or "挖了" in _ev_join):
        return _TOOL_LOSS_DUG_BREAK
    return _TOOL_LOSS_SILENT


def _holdings_diff(before: dict[str, int], after: dict[str, int]) -> str:
    """把「死前 vs 重生后」的持有量对比渲染成一句话：多了什么 / 少了什么 / 不变。

    持有量口径见 `run_exploration` 里的闭包 `_holdings`：库存 + 穿在身上的装备（不含主手，
    避免双算）。只报**有变化**的项；完全一致时返回「不变」。

    ★ 不替 agent 下「掉没掉」的结论 —— 它自己看 diff 就知道装备还在还是没了。这是
    2026-08-24 改死亡反馈的配套（见 `_system_feedback` docstring）：此前写死「装备保留
    未掉落（keep_inventory=true）」，agent 重生后看到空背包（实为本步放置方块把仅有的
    原木用光了、非死亡掉落）会无视这句断言、反按「看到空包」误记成「死亡掉光物品」并
    污染公共脑。给原始 diff 让它自己判断，不会被一句与眼见矛盾的主观断言带偏。
    """
    all_keys = set(before) | set(after)
    gained, lost = [], []
    for k in sorted(all_keys):
        b, a = before.get(k, 0), after.get(k, 0)
        if a > b:
            gained.append(f"{k} {b}→{a}" if b else f"{k} +{a - b}")
        elif a < b:
            lost.append(f"{k} {b}→{a}")
    parts = []
    if lost:
        parts.append("少了：" + "、".join(lost))
    if gained:
        parts.append("多了：" + "、".join(gained))
    if not parts:
        return "没变"
    return "；".join(parts)


#: 一天的关键节点（游戏刻），边界严格对齐官方四阶段（time.md）：
#:   day[0,12000) sunset[12000,13800) night[13800,22200) sunrise[22200,24000)。
#:   24000=0 是 day 起点（一天开始 / 日界 worldDay 滚动 / 反思收尾）。
_TICK_DAY_END = 12000    # 白天结束 = 日落段开始
_TICK_SUNSET_END = 13800 # 日落段结束 = 夜晚段开始（官方 night 起点）
_TICK_NIGHT_END = 22200  # 夜晚段结束 = 日出段开始（官方 sunrise 起点）
_TICKS_PER_DAY = 24000
# 兼容旧引用（已不用于判据，仅渲染/注释引用时退化到新常量）
_TICK_SUNSET = _TICK_DAY_END
_TICK_DAWN = _TICK_NIGHT_END


def _render_time(status: dict, tick_rate: float = 1) -> str:
    """渲染时间：`night (14200/24000，距日出 8000 刻)`。

    为什么要给原始刻与倒计时：只给 `night`/`sunset` 这类**阶段标签**不够——
    「还有多久天黑 / 还有多久日出」是本域最关键的时间决策，环境明明有 `bot.time.timeOfDay`
    却不告诉它的话，就逼它靠"天色变化"猜。属于「环境知道但不说」的一类。

    ★ **倒计时直接给游戏刻，不换算成真实分钟**：Agent 生活在 MC 时间里——对它而言世界以
      20 刻/秒流逝、一天 24000 刻，时间是刻不是分钟。换算成分钟会泄漏「真实世界」概念、还会
      随倍速 N 失真。给刻后与倍速无关、与 timeout_ticks（也是刻）同口径，Agent 拿倒计时跟
      自己的时限直接比。tick_rate 参数保留只为向后兼容签名、已不参与计算。

    ★ **播「下一个阶段」的距离**（2026-09-16，按官方四阶段）：当前在哪个段，就报距下一个段
      起点还剩多少刻。各段终点（=下一段起点）：
        day/noon  → 距日落  （终点 12000，日落段开始）
        sunset    → 距夜晚  （终点 13800，夜晚段开始＝天黑刷怪）
        night/midnight → 距日出（终点 22200，日出段开始＝天将亮）
        sunrise   → 距白天  （终点 24000/0，正式日出＝白天开始/日界）
      「距天亮」旧叫法已废——天光含月光，夜晚露天未必全黑；用「距日出/距白天」更准。
      关键决策点：sunset 段报「距夜晚」(刷怪将至)，night 段报「距日出」(还要熬多久)。
    """
    label = status.get("timeOfDay", "?")
    t = status.get("timeTicks")
    if not isinstance(t, (int, float)):
        return str(label)  # 老 payload 没有 timeTicks，退化成只报标签
    t = int(t) % _TICKS_PER_DAY
    if t < _TICK_DAY_END:
        remain, what = _TICK_DAY_END - t, "距日落"
    elif t < _TICK_SUNSET_END:
        remain, what = _TICK_SUNSET_END - t, "距夜晚"
    elif t < _TICK_NIGHT_END:
        remain, what = _TICK_NIGHT_END - t, "距日出"
    else:
        remain, what = _TICKS_PER_DAY - t, "距白天"
    return f"{label} ({t}/{_TICKS_PER_DAY}，{what} {remain} 刻)"


def _sky_darken(t: int) -> int:
    """当前时刻天光要**减掉**多少（0-11）。原版 `Level.getSkyDarken` 的曲线，逐点核对过。

    为什么需要它：刷怪规则是「方块光==0 且 天光<=7」，但观察给的 `skyLight` 是**静态暴露度**、
    不随昼夜变化（露天空地午夜仍读 15）。于是那条规则在夜里**根本套不上去** —— Agent 没有
    任何数据能算出「此刻此地有多暗」，只能退回到 `sunset`/`night` 这种阶段标签，而标签把
    黄昏和深夜混为一谈：环境**知道但没说**（同 timeTicks / light / nearbyChests 那批）。

    曲线（本函数实算，抽样）：
        t=11000 → 减 0（正午到黄昏前一直是满亮）
        t=12500 → 减 4（日落中段，露天亮度 11，仍不刷怪）
        t=13000 → 减 7（露天亮度 8，**刚好还差一点**）
        t=13500 → 减 10（露天亮度 5 ≤ 7，地表开始刷怪）
        t=14000..22000 → 减 11（露天亮度 4，**触底且不再变暗**）
        t=23000 → 减 7（黎明回升）
    两个可教的事实就在这条曲线里：① 日落(12000)到真正变暗(≈13500)之间有约 75 秒真实时间，
    黄昏并不危险；② 午夜并不比入夜更暗 —— "深夜更危险"来自怪物累积，不是亮度。
    """
    f = t / 24000.0 - 0.25
    if f < 0:
        f += 1.0
    g = 0.5 - math.cos(f * math.pi) / 2.0
    h = (f * 2 + g) / 3.0
    d = 1.0 - (math.cos(h * 2 * math.pi) * 2.0 + 0.5)
    return round(min(max(d, 0.0), 1.0) * 11)


def _render_light(status: dict) -> str:
    """渲染脚下这一格的光照：`方块光 0/15  天光 15/15`。

    为什么要给：1.18 起敌对生物的刷新条件恰好就是这两个数
    （`方块光==0 且 天光<=7`，规则写在 react.yaml 的 system「这个世界怎么运转」一节），所以「我现在待的
    地方会不会刷怪」在环境里是**可精确读出的事实**，值得直接暴露给它，而不是让 bot 挖完密闭
    掩体、封好口却不知道自己正坐在 light=0 的刷怪点上。

    **两个数都报、不合成一个"暗不暗"**：方块光是它能主动改变的那项（插火把就能压掉刷怪），
    天光是"这格有多见天"的暴露度、**不随昼夜衰减**（午夜地表空地仍是 15）。
    合并或只报其一都会让规则套不上去。

    ★ 只报事实、不下判断（与本文件其他 render_* 一致）：不标「⚠会刷怪」——
    刷怪还受难度/群系/方块类型/上方空间影响，凭两个数下断言是拿没有的信息说话。
    带 `/15` 上限，理由同生命的 `/20`：不知道满值就判断不了 10 算亮还是暗。

    **第三个数「此刻实际」**：把静态天光按当前时刻的 `_sky_darken` 折算，
    即游戏真正用来判刷怪的那个值 `max(方块光, 天光 - 折减)`。加它的理由见 _sky_darken 的
    docstring —— 没有它，刷怪规则在夜里无从计算，黄昏与深夜在 Agent 眼里一样黑。
    折减量一并列出（`天光15-11`），这样它能自己看出"变暗"来自时刻而非位置。
    """
    def _num(v):
        return v if isinstance(v, (int, float)) else None

    bl, sl = _num(status.get("light")), _num(status.get("skyLight"))
    if bl is None and sl is None:
        return ""  # 区块未加载 / 老 payload 没这两个字段，整行省掉而不是印 "?"
    parts = []
    if bl is not None:
        parts.append(f"方块光 {bl:g}/15")
    if sl is not None:
        parts.append(f"天光 {sl:g}/15")
    # 此刻实际亮度：需要 timeTicks（老 payload 没有就跳过这一项，不猜）
    t = status.get("timeTicks")
    if sl is not None and isinstance(t, (int, float)):
        sub = _sky_darken(int(t) % _TICKS_PER_DAY)
        eff = max(bl or 0, max(0, sl - sub))
        # 括号里的算式**只在天光真的被折减时才写**：天光已是 0（地下/密闭）时写
        # 「天光0-11」会读成负数、且暗示"变暗来自时刻"，而那里的黑暗其实来自不见天
        # —— 折减对它毫无作用。这种情况直接说明亮度由谁决定，别贴一个误导的算式。
        if sl > 0 and sub > 0:
            parts.append(f"→ 此刻实际 {eff:g}/15（天光{sl:g} 按当前时刻折减 {sub}）")
        else:
            parts.append(f"→ 此刻实际 {eff:g}/15")
    return "  ".join(parts)


def _render_entities(entities: dict) -> str:
    """渲染附近生物一行，带**只数与方位**：`zombie×3(最近5格, +x/+x-z/-z)  chicken(11格, -x)`。

    为什么要只数与方位：夜间围攻是本域主要死因之一（Zombie/Spider/Skeleton 常见），若只报
    「最近一只的距离」会把「被几只围住」这个决定性事实抹平；没有方位则不知道往哪躲。
    改法见 status.js 的 getEntities 注释。

    **兼容旧格式**：老 payload 的 entities 是 `{名字: 距离}`（纯数字），新格式是
    `{名字: {n, min, dirs}}`。两种都渲染 —— 这样 Node/Python 谁先部署都不会崩。
    """
    if not entities:
        return "(无)"

    def _min_dist(v) -> float:
        if isinstance(v, dict):
            try:
                return float(v.get("min", 0) or 0)
            except (TypeError, ValueError):
                return 0.0
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    parts = []
    for name, v in sorted(entities.items(), key=lambda kv: _min_dist(kv[1])):
        if isinstance(v, dict):
            n = int(v.get("n", 1) or 1)
            dirs = [d for d in (v.get("dirs") or []) if d]
            head = f"{name}×{n}" if n > 1 else name
            tail = f"最近{_min_dist(v):.0f}格" if n > 1 else f"{_min_dist(v):.0f}格"
            if dirs:
                tail += f", {'/'.join(dirs)}"
            parts.append(f"{head}({tail})")
        else:  # 旧格式：只有距离
            parts.append(f"{name}({_min_dist(v):.0f}格)")
    return "  ".join(parts)


def _render_equipment(status: dict) -> str:
    """渲染装备栏一行：`头=无  胸=iron_chestplate(耐久224/240)  …  主手=stone_pickaxe(耐久118/131)`。

    装备槽不在「库存」那行的槽位区间里，所以 bot 穿上护甲后，若不单独渲染这一行，那件护甲
    在观察里会**完全消失**（既不在库存、也没有装备行）——从它的视角看等于「刚造的甲丢了」，
    可能重复再造；主手拿的是什么同样不可见，「现在是石镐还是空手」只能靠 console.log 自己查。

    空槽显示 `无` 而不是省略：**「没穿甲」本身是要让它看见的事实**（省略则无从区分
    「没穿」与「这一项没报」），也是「该去造甲了」的触发信号。
    """
    eq = status.get("equipment") or {}
    if not isinstance(eq, dict):
        return ""
    parts = []
    for slot, label in _EQUIP_SLOTS:
        it = eq.get(slot)
        if not (isinstance(it, dict) and it.get("name")):
            parts.append(f"{label}=无")
            continue
        dur = it.get("durability")
        txt = it["name"]
        if isinstance(dur, (list, tuple)) and len(dur) == 2:
            txt += f"(耐久{dur[0]}/{dur[1]})"
        parts.append(f"{label}={txt}")
    return "  ".join(parts)


def _render_vitals(status: dict) -> str:
    """生命行：`生命：11.5/20  饥饿：18/20(饱和2.4)  氧气：7/20  ⚠在岩浆中`。

    三项状态：

    - **氧气**（`oxygen`，0-20）：水下窒息是一条独立的掉血通道。只报「生命 8」而不报氧气，
      bot 看到掉血却不知道原因是「在水下」，也就不知道该往上游 —— 只在**非满值时**报，
      平时不占版面（陆地上恒为 20，每步都印就是噪音）。
    - **在水中 / 在岩浆中**（`isInWater` / `isInLava`）：岩浆是本域最快的死法之一，
      且掉进去时**没有任何其他字段会提示**（3×3×3 网格报的是身体层方块名，
      而 bot 站在岩浆里那一格的方块名恰好就是 `lava` —— 但那要它自己去关联）。
      这两项属于「不报就可能直接死」，故只要为真就显式标出来。
    - **饱和度**（`saturation`）：饥饿值下降的缓冲池，饱和归零后饥饿才开始掉。
      附在饥饿后面的括号里，同样只在非零时报。

    生命/饥饿都补上 `/20` 上限：光看「生命 11.5」需要先知道满值是 20 才能判断危险程度。
    """
    def _num(v):
        return v if isinstance(v, (int, float)) else None

    hp, food = _num(status.get("health")), _num(status.get("food"))
    hp_txt = f"{hp:g}/20" if hp is not None else str(status.get("health", "?"))
    food_txt = f"{food:g}/20" if food is not None else str(status.get("food", "?"))
    sat = _num(status.get("saturation"))
    if sat:
        food_txt += f"(饱和{sat:g})"
    parts = [f"生命：{hp_txt}", f"饥饿：{food_txt}"]
    # XP（经验）：等级 + 当前点数。与生命/饥饿同类（状态/进度感知，非异常项），故紧跟其后、
    # 排在只在异常时出现的氧气/岩浆之前。points 是 mineflayer 的当前持有值，死亡后会被服务器
    # 压低（见 status.js experience 注释）——如实报，与生命归零同性质，不替它美化。
    xp = _num((status.get("experience") or {}).get("points"))
    xp_lvl = _num((status.get("experience") or {}).get("level"))
    if xp_lvl is not None or xp is not None:
        parts.append(f"XP：Lv.{xp_lvl if xp_lvl is not None else '?'}"
                     + (f"({xp:g}pts)" if xp is not None else ""))
    oxy = _num(status.get("oxygen"))
    if oxy is not None and oxy < 20:
        parts.append(f"氧气：{oxy:g}/20")
    if status.get("isInLava"):
        parts.append("⚠在岩浆中")
    elif status.get("isInWater"):
        parts.append("在水中")
    return "  ".join(parts)


def render_observation(obs: dict, tick_rate: float = 1,
                       advancements: set[str] | None = None) -> str:
    """把 observe payload 渲染成给 LLM 看的文本（对标 Voyager action agent 的 human message）。

    advancements: agent 已达成的进度显示名集合（如 ``{"[Diamonds!]"}``）。用于渲染「进度」行
    （5 棵树各自前沿，见 progress_frontier）。打分用的 tier 仍由 tech_tier 在轨迹追踪侧算，
    不在此渲染。

    ★ 不传时（None）默认从 obs 的 advancementPacket 字段取（2026-09-07，成就源切 packet）——
    这样所有调用点（含主 tool result / resume 错误返回 / 快照 lambda）无需各自算 adv_names
    也能渲染出进度行，避免「主返回漏传参数→进度行恒 None」的老 bug。packet 能采到 onSystem
    漏掉的 root=[Minecraft]，5 棵树前沿完整。
    """
    if advancements is None:
        advancements = set(_extract_advancement_packet(
            obs.get("advancementPacket") if obs else None)["completed"])
    status = obs.get("status", {}) or {}
    inv = obs.get("inventory", {}) or {}
    pos = status.get("position", {}) or {}
    voxels = obs.get("voxels", []) or []
    ent_str = _render_entities(status.get("entities") or {})
    # 光照跟时间同一行：两者一起才构成「此刻此地会不会刷怪」的判据（夜晚 + 方块光 0），
    # 而且都是**环境属性**、不是 bot 的生命体征 —— 放进 _render_vitals 会和生命/饥饿混为一类。
    light_str = _render_light(status)
    lines = [
        f"生物群系：{status.get('biome', '?')}  时间：{_render_time(status, tick_rate)}"
        + (f"  光照：{light_str}" if light_str else ""),
        # 保留 1 位小数（不用 `:.0f` 取整）：bot 的坐标是浮点，而**方块操作要整数方块坐标** ——
        # 取整后 LLM 看到的 `x=450` 无法判断自己是站在 450 号方块中间还是紧贴 449/451 的边界，
        # 而 placeItem / blockAt / GoalNear 都对这一格之差敏感。
        # y 同样保留：站在半砖/台阶上时 y=64.5，取整成 64 会让"脚下是什么"推断错位。
        f"位置：x={pos.get('x', '?'):.1f} y={pos.get('y', '?'):.1f} z={pos.get('z', '?'):.1f}"
        if isinstance(pos.get("x"), (int, float)) else f"位置：{pos}",
        _render_vitals(status),
        # 半径必须标出来：本域各观察源的范围**互不相同**（生物 32 / 箱子 16 / voxels 17×5×17 /
        # 视野 48 / 贴身网格 3×3×3），不标就得靠 LLM 脑补对齐——否则会出现 mineBlock 只搜 16 格
        # 而视野给到 48、LLM 看见远处的树却被告知「附近没有」这类播报与观察矛盾的情况
        # （已在原语侧点明搜索半径，这里把观察侧也补齐）。
        f"附近生物（32格内，近→远）：{ent_str}",
        f"附近方块（周围17×5×17内出现过的种类，无方位）：{', '.join(sorted(set(voxels))) or '(无)'}",
        f"库存({status.get('inventoryUsed', 0)}/36)：{', '.join(f'{k}x{v}' for k, v in inv.items()) or '(空)'}",
        # 装备行紧跟库存：两者是同一件事的两半（背着的 / 穿着的），而且装备槽**不在库存那行里**
        # （见 _render_equipment）—— 挨着放才能让「这件甲在身上、不在包里」一眼可辨。
        f"装备：{_render_equipment(status)}",
        # 进度行（替代旧「当前科技层级」，2026-09-05）：报 5 棵进度树各自的前沿（已达成
        # 进度里 depth 最高的那个），只报已拥有、不报缺口/路线——开放式探索不给攻略。
        # 打分链改用 base_depth()（tier_progress 存 depth 整数），旧 tech_tier() 保留但不再被调。
        f"进度：{progress_frontier(advancements)}",
        # 科技行（2026-09-09 加回）：按「已造出的最高级成品工具」结算的 4 级
        # （wooden/stone/iron/diamond/none），与上面进度树口径并列。这是旧口径，非单调——
        # 工具用完断裂会回退，让 Agent 看到此刻手里最高级的工具是什么。只报事实不给路线。
        # 根因修复：0908 把 Agent 推向钻地下冲进度树后，XP/步效率崩到对照的 1/3.4——
        # 加回科技让 Agent 对「自己手上的工具档位」有即时感知，配合目标措辞调整把
        # 精力从「竖井爬升脱困」拉回「采资源涨 XP/造装备」。
        f"科技：{tool_tier_name(inv, equipped_item_names(status))}",
        f"附近箱子（16格内）：{_render_chests(obs.get('nearbyChests') or {})}",
    ]
    # 空间感知分两级，都放在最后、紧挨着本步决策：
    # ① 贴身 3×3×3（穿墙全知，25 格）—— 回答「脚边能不能走、脚下是什么」；
    # ② 视野扇区（真遮挡，48 格）—— 回答「朝向那边远处有什么、值不值得走过去」。
    # 与上面那行「无方位的种类集合」一起构成 近身 → 视野 → 全域概览 三档。
    lines.extend(render_nearby_grid(obs.get("nearbyGrid") or {}))
    lines.extend(render_vision(obs.get("vision") or {}))
    return "\n".join(lines)


class MinecraftWorld:
    """一个 MC 世界的连接句柄：对应一个 mineflayer 服务器（驱动一个 bot）连一个 MC server。

    mineflayer_url: Voyager Express 服务器地址（如 http://127.0.0.1:3000）
    mc_port: 该 mineflayer 服务器要连的 MC server 端口（如 25567）

    崩溃自愈：慢原语可能让 bot 超时掉线、进而拖垮 mineflayer 进程。传 server_dir +
    mineflayer_port 时，MinecraftWorld 用 subprocess 托管 mineflayer server，连接被拒时自动
    重起进程 + 重发 /start 续跑，而不是整轮报废。position 记住上次 /start 的出生点，重连后传送回去。

    **世界重置**：传 mc_dir（MC server 目录）时，regenerate_world() 能停服 → 删存档 → 换
    level-seed → 重起服，做到**每轮探索都是全新地形**。不传 mc_dir 则退化为旧行为（只清库存，
    世界跨轮持久）并在每轮 reset 时告警 —— 因为那会让「evolving 上升」混淆进「世界被前几轮
    开发过」，是**不能拿去写论文的**。
    """

    def __init__(
        self,
        mineflayer_url: str,
        mc_port: int,
        *,
        reset_mode: str = "hard",
        server_dir: str | None = None,
        mineflayer_port: int | None = None,
        max_heal_retries: int = 2,
        mc_dir: str | None = None,
        java_bin: str | None = None,
        tick_rate: float = 1,
    ):
        self.mineflayer_url = mineflayer_url.rstrip("/")
        self.mc_port = mc_port
        self.reset_mode = reset_mode
        # 自愈用：托管 mineflayer 进程所需
        self.server_dir = server_dir
        self.mineflayer_port = mineflayer_port or self._infer_port(self.mineflayer_url)
        self.max_heal_retries = max_heal_retries
        self._proc = None            # subprocess.Popen（仅当 server_dir 给定时托管）
        self._last_position = None   # 上次 /start 的出生点，重连后传送回去
        # 世界重置用：MC server 目录（含 server.properties + world/）与 Java 21 可执行文件
        self.mc_dir = str(mc_dir) if mc_dir else None
        self.java_bin = java_bin or os.environ.get("JAVA_BIN") or _default_java_bin()
        self._mc_proc = None         # 我们自己起的 MC server 进程（regenerate_world 之后）
        self._world_generation = 0   # 已重生世界的次数，仅用于日志/留痕
        #: bot 的游戏内用户名。默认 "bot"（与模板 ops.json 里已有的条目一致）；
        #: 由 domain 侧按 arm key 设置（见 set_bot_name），为多人联机协作做准备。
        self.bot_name = "bot"
        #: ★ OPENKAGE 倍速（2026-08-21）：游戏倍速 N（1=自然流速）。透传到 index.js /start 的
        #: tick_rate，由 Node 侧两侧同步（server /tick rate + 客户端 setTickRate）。见 memory
        #: >1 时一夜干等与昼夜流速均按 N 倍压缩。
        #: 保留浮点：2.5 等小数倍速合法（server /tick rate ${20*N} 接受 50 TPS），int() 会静默截成 2。
        self.tick_rate = max(1.0, float(tick_rate))
        #: ★ 放置埋点日志路径（2026-09-14 加，None=不埋）：由 domain 侧在 _fresh_world 设定
        #: （复用 run 目录），经 /start body.placeLog 传给 Node 侧，blockPlaced 事件逐条
        #: append（arm/block/x/y/z/t）。供论文 3D 建造还原——差集法被结构随机性污染（0914
        #: 验证），事件埋点是放置的唯一权威记录。
        self.place_log_path: str | None = None
        #: ★ 进食/血量埋点日志路径（2026-09-17 加，None=不埋）：由 domain 侧在 _fresh_world 设定
        #: （本地盘缓存 /path/to/logs/<run>/<arm>.jsonl，2026-09-18 FUSE 二次事故后改），
        #: 经 /start body.eatLog 传给 Node 侧，监听 mineflayer
        #: `health` 事件记进食/回血/受伤/死亡时间线（trigger=eat/heal/damage/death，滤掉饱和度
        #: 自然衰减噪声），带 elapsedTime/worldDay/died 对齐字段。供量化「生存」长期目标——
        #: 黎明单点快照（discoveries.health/food）统计不到吃了多少/回了多少/挨了几下打。
        self.eat_log_path: str | None = None
        #: ★ 埋点日志拷回映射（2026-09-18 加）：{本地盘路径: 共享盘目标}，_fresh_world 设定，
        #: 窗口边界 + shutdown 由 _sync_local_logs 整文件 copy2 拷回共享盘 run 目录。
        #: place/eat 两日志 Node 侧只写本地盘（FUSE 高频 append 两次实锤丢头部，0914/0918），
        #: 共享盘上只出现整文件拷贝。读侧（plot_build3d/inspect_metrics）读共享盘路径不变。
        self._log_sync_targets: dict[str, str] = {}
        #: ★ 观战账号名（2026-09-15 加，None=无录像/对照组）：由 domain 侧在起录像器后设定
        #: （FootageRecorder.spectator_name，形如 "spec_<mc_port>"），经 /start body.spectatorName
        #: 传给 Node 侧——spectatorCam 采集器据此发 /data get entity <名> Pos 探「录像相机是否
        #: 跟着 bot」，env 步末看门狗防脱附空镜（见 _spectator_needs_reattach）。
        self.spectator_name: str | None = None

    def set_bot_name(self, name: str) -> None:
        """设置 bot 的游戏内用户名，并**确保它在 ops.json 里是 OP**。

        为什么要给每个 arm 起名：现在所有副脑都叫 "bot"，游戏内围观时分不出谁是谁；日后要做
        **多 Agent 联机协作**，名字是最基本的身份。用 arm key 当名字，日志作用域
        （`[e1b1 a1 #1]`）与游戏内 `<Alice>` 能直接对上。

        ★★ **必须同步 ops.json，否则会踩一个查起来极痛的坑**：MC 离线模式的玩家 UUID 是
        **由用户名 md5 算出来的**（`OfflinePlayer:<name>`），而 ops.json 按「name + uuid」授权。
        换了名字却不更新 ops.json，bot 就**静默失去 OP** —— `/forceload`、`/spreadplayers`、
        `/gamerule`、`/tick freeze` 全部无声失败，症状表现为「出生点抽 12 次全败」「世界时间
        不流动」这类跟权限毫不相干的样子。所以这里在改名的同时把新名字的 UUID 追加进
        ops.json，level=4。
        """
        name = (name or "").strip() or "bot"
        self.bot_name = name
        if not self.mc_dir:
            return  # 没有 mc_dir（如连外部服）时只改名，OP 由使用者自己保证
        ops_path = Path(self.mc_dir) / "ops.json"
        try:
            entries = json.loads(ops_path.read_text(encoding="utf-8")) if ops_path.is_file() else []
            if not isinstance(entries, list):
                entries = []
        except Exception:  # pylint: disable=broad-except
            logger.warning("[minecraft] ops.json 解析失败，将重建（原内容备份为 .bak）")
            try:
                ops_path.replace(ops_path.with_suffix(".json.bak"))
            except Exception:  # pylint: disable=broad-except
                pass
            entries = []
        if any(isinstance(e, dict) and e.get("name") == name for e in entries):
            return  # 已是 OP，无需重复写
        entries.append({
            "uuid": _offline_uuid(name),
            "name": name,
            "level": 4,
            "bypassesPlayerLimit": True,
        })
        try:
            ops_path.write_text(json.dumps(entries, ensure_ascii=False, indent=2),
                                encoding="utf-8")
            logger.info("[minecraft] 已把 %s 写入 ops.json（OP level 4）", name)
        except Exception:  # pylint: disable=broad-except
            logger.warning("[minecraft] 写 ops.json 失败 —— bot %s 可能不是 OP，"
                           "斜杠指令会静默失败", name, exc_info=False)

    @staticmethod
    def _infer_port(url: str) -> int:
        try:
            return int(url.rsplit(":", 1)[1].split("/")[0])
        except Exception:  # pylint: disable=broad-except
            return 3000

    def _server_alive(self) -> bool:
        try:
            requests.get(self.mineflayer_url, timeout=3)
            return True
        except requests.exceptions.RequestException as e:
            # 404 等 HTTP 响应说明进程活着（它只有 /start /step /stop）
            return isinstance(e, requests.exceptions.HTTPError)
        except Exception:  # pylint: disable=broad-except
            return False

    def _launch_server(self) -> bool:
        """（重）起托管的 mineflayer server 进程，等它监听端口。仅当 server_dir 给定时可用。
        杀旧进程后**等端口真正释放**再起新进程——否则旧进程 TIME_WAIT/未退干净会让新进程 EADDRINUSE
        立刻死掉。"""
        if not self.server_dir:
            return False
        import subprocess
        import time
        if self._proc is not None:
            try:
                self._proc.kill()
                self._proc.wait(timeout=5)  # 等旧进程真退出，释放端口
            except Exception:  # pylint: disable=broad-except
                pass
            self._proc = None
        # 等端口释放（最多 10s）：确认没人再监听 3000，避免新进程 EADDRINUSE
        for _ in range(10):
            if not self._port_in_use():
                break
            time.sleep(1)
        logger.warning("[minecraft] (重)起 mineflayer server @ %s port %d",
                       self.server_dir, self.mineflayer_port)
        # ★ **mineflayer 的 stdout/stderr 必须留档**（不能是 DEVNULL）：Node 侧的
        #   `[abort]`/`[tick]` 这些诊断行、pathfinder 的报错、未捕获异常的栈都要能查。
        #   落到 worker 目录下的 mineflayer.log（与 MC 的 openkage_server.log 并排，便于对时间线）。
        #   用 "ab" 追加而非覆盖：自愈会反复重起进程，覆盖会丢掉崩溃前的最后几行。
        _mf_log = os.path.join(self.mc_dir or self.server_dir, "mineflayer.log")
        # ★ **防失控涨大**（2026-08-22，长跑前置）：node 侧每步都打 `[pathfinder预算]`/`[tick]`
        #   诊断行，ab 追加永不轮转 → 实测 w2 涨到 4G。30 天长跑会到几十 G 爆盘。启动前若已超
        #   _MF_LOG_CAP，截断保留**尾部** _MF_LOG_TAIL（崩溃前最后几行最该留，与上面"ab 不覆盖"
        #   同一理由），再追加。不引入轮转库：subprocess stdout 接的是文件句柄，中途换句柄复杂；
        #   每次重起截一次已足够（重起频率远低于日志产生频率）。
        _MF_LOG_CAP = 50 * 1024 * 1024   # 50M：超此即截断
        _MF_LOG_TAIL = 1 * 1024 * 1024   # 保留尾部 1M
        try:
            if os.path.exists(_mf_log) and os.path.getsize(_mf_log) > _MF_LOG_CAP:
                with open(_mf_log, "rb") as _f:
                    _f.seek(-_MF_LOG_TAIL, 2)
                    _tail = _f.read()
                with open(_mf_log, "wb") as _f:
                    _f.write(_tail)
                logger.info("[minecraft] mineflayer.log 超 %dM，已截断保留尾部 1M",
                            _MF_LOG_CAP // (1024 * 1024))
        except OSError:
            pass  # 截断失败不影响起进程
        try:
            _mf_logf = open(_mf_log, "ab")  # pylint: disable=consider-using-with
        except OSError:
            _mf_logf = subprocess.DEVNULL   # 写不了就退回丢弃，绝不因留档失败而起不来
        self._proc = subprocess.Popen(
            # ★ 不带 --heapsnapshot-near-heap-limit（2026-08-24 长跑前改）。mineflayer JS heap OOM
            # 时自愈重连（ConnectionError/RemoteDisconnected → _heal_and_restart）已能恢复，不需要
            # dump 留痕；而每份快照 ≈ 堆大小（GB 级），5 臂并发 OOM 会连环 dump 撑爆磁盘
            # （0824 实测 mc_server/ 下 7 份 .heapsnapshot 占满 42G，把系统盘顶到 98% → 全队冻死）。
            # 需诊断时临时加回 `--heapsnapshot-near-heap-limit=1`。
            # ★ 带 --max-old-space-size=4096（2026-09-10 加）。把原本模糊的 V8 老生代上限明确化——
            #   node 不设此值时按物理内存动态算、容器环境下不可预测，OOM 边界模糊（0910 run 末段
            #   25 次秒级 ConnectionError 自愈即 mineflayer 进程 heap OOM 退出，崩前无痕、崩后
            #   mineflayer.log 被 50M 截断+重启覆盖吃掉，事后查不到 FATAL ERROR 堆栈）。设 4096：
            #   ① 上限可控——峰值（如 lightAreaWithTorches 81 次 goto+placeBlock）有 4G 余量不至于
            #      一冲就崩；② 配合 index.js 的 heap 告警阈值（_OPENKAGE_HEAP_LIMIT_MB 同标 4096，
            #      峰值到 80%=3.2G 即主动留痕+注入 onError 反馈），把「无声 OOM」变「留痕告警」。
            #   它只抬天花板、不修内存泄漏，治本仍靠改自产技能代码（由反思阶段维护）。
            ["node", "--max-old-space-size=4096", "index.js", str(self.mineflayer_port)],
            cwd=self.server_dir,
            stdout=_mf_logf, stderr=subprocess.STDOUT,
        )
        # ★ ready 窗口 30s→120s（2026-09-03，共享盘 FUSE 冷启）：
        # 代码放在网络盘上时，mineflayer 首次从网络盘 require 5 万个
        # node_modules 文件有冷启竞争——单臂实测 ~40s 才起监听，5 臂并发更慢，原 30s 窗口必
        # 全部判未就绪退场（0903 续跑 5 臂全崩于此）。FUSE 缓存预热后秒起（~5s），故 120s 对
        # 热路径只是多 poll 几次立刻返回，无副作用；冷路径才真正用到这段余量。
        for _ in range(120):
            # 新进程可能因 EADDRINUSE 等立刻死 → 先检测它是否还活着
            if self._proc.poll() is not None:
                logger.error("[minecraft] mineflayer server 新进程启动即退出（code=%s）", self._proc.returncode)
                return False
            try:
                requests.get(self.mineflayer_url, timeout=2)
                return True
            except requests.exceptions.HTTPError:
                return True  # 有 HTTP 响应即已监听
            except Exception:  # pylint: disable=broad-except
                time.sleep(1)
        logger.error("[minecraft] mineflayer server 重起后 120s 内未就绪")
        return False

    def _port_in_use(self) -> bool:
        import socket
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1)
            return s.connect_ex(("127.0.0.1", self.mineflayer_port)) == 0

    def _heal_and_restart(self) -> bool:
        """自愈：区分两种故障，对症下药（混为一谈是自愈失败的根因）。
        ① **bot 掉线、mineflayer 进程还活**（被怪咬死→Timed out 掉线，最常见）：进程 HTTP 仍响应，
           **只需重发 /start 让它重连 bot**，绝不杀进程（杀了反而端口冲突起不来）。
        ② **进程真崩/端口没响应**：才杀进程重起 + /start。

        ★ **轻量分支（①）重连后要校验世界是否真活**（2026-08-20 修，dave flying 退场事故）：
        重连后 bot 可能「半连接」——连接活但 chunk 没恢复（getColumns 返 0、blockAt 返 null）。
        `_validate_world_after_heal` 检出半连接时，① **降级走 ②**（重启进程，像 carol ReadTimeout
        那样从干净进程恢复世界），别停在半连接空转到退场。只许 ① 升级为 ②，绝不反向降级。

        ★★ **自愈一律走 RESET_RECONNECT，绝不能用 self.reset_mode**（2026-08-11 修）：
        `reset_mode` 默认 `"hard"`，而 index.js 的 hard 分支是 `/clear @s` + `/kill @s` +
        撤销全部成就 —— 那是**轮首**该干的事。自愈发生在**轮中**，用 hard 等于「bot 掉线一下，
        身家和成就一起清零」。2026-08-10 那轮 bob 就是这么没的：16:32 一次 ReadTimeout 触发
        重连，同一步 iron 全套 + 248 cobblestone + **3 颗 diamond** 归零、tier iron→none，
        而 `/kill @s` 的播报被 onSystem 按 death.attack.genericKill 有意忽略（那本是为了不把
        轮首清库存记成死亡），于是这次归零**连死亡计数都没进**，事后只剩一条"未死亡却丢装备"
        的诊断警报。指标被削、事故还查不出来 —— 两头都坏。
        """
        # ★ 自愈时不传 position，让 MC 服务端从 playerdata 恢复玩家断线前位置。
        #   旧实现传 self._last_position（轮首出生点），导致 carol 掉线后 heal 被送回轮首、
        #   一步跳动 800+ 格（#4）。reset=RESET_RECONNECT 已保证不清库存/成就，服务端 playerdata
        #   是玩家位置的唯一权威来源，与 resume 续跑路径一致。_validate_world_after_heal 兜底
        #   chunk 半连接。
        # ★ **首步:探 MC server 端口**(2026-08-22,w4 OOM 事故后补)。旧逻辑只探 mineflayer
        #   (`_server_alive`),从不探 MC。MC 崩了(OOM/Watchdog 强杀)而 mineflayer 还活时,
        #   下面重发 /start 会永久 ECONNREFUSED(bot 连不上已死的 MC)→ 走到重起 mineflayer →
        #   /start 仍 ECONNREFUSED → **死循环空转**,直到 run 的 gather 被这个 arm 拖死。
        #   MC 挂了必须先拉起 MC:走 `_launch_mc_server`(读档重起,**绝不** regenerate_world——
        #   regenerate 删存档清库存,等于轮中归零,正是上面 docstring 警告的 bob 事故同源)。
        #   MC 起来后 mineflayer 必也已掉线(它连的 MC 没了),交给下面既有重连/重起流程接管 bot。
        #   ExitOnOutOfMemoryError 已保证 OOM 即退、端口秒级 DOWN,这里能快速检出。
        if self.mc_dir and not self._mc_port_in_use():
            logger.warning("[minecraft] 自愈:MC server 端口 %d 无监听(MC 进程已崩,如 OOM/"
                           "Watchdog),读档重起 MC(不重生,保留库存/成就/位置)", self.mc_port)
            if not self._launch_mc_server():
                logger.error("[minecraft] 自愈:读档重起 MC 失败,放弃(交上层 /step 重试兜)")
                return False
        reconnect_position = None
        server_ok = self._server_alive()
        if server_ok:
            # 进程活着，只是 bot 掉了 → 直接重连，先试一次
            try:
                self._do_start(reconnect_position, reset=RESET_RECONNECT)
            except Exception:  # pylint: disable=broad-except
                logger.warning("[minecraft] 自愈：进程存活但 /start 重连失败，降级为重起进程")
                server_ok = False  # 落到下面重起
            else:
                logger.info("[minecraft] 自愈：mineflayer 进程存活，已重发 /start 重连 bot"
                            "（reset=%s，由 playerdata 恢复位置，不清库存/成就）", RESET_RECONNECT)
                self._unfreeze_after_heal()
                # ★ 重连后校验世界：半连接（chunk 空）就降级走重启进程分支，别空转到退场。
                #   校验独立成块：它 best-effort，失败（返回 True）不该被当成 /start 重连失败
                #   而误触发重启；只有「明确检出半连接」(返 False) 才降级。
                if not self._validate_world_after_heal():
                    logger.warning("[minecraft] 自愈：轻量重连后世界为空（半连接），降级重启进程"
                                   "（像 carol ReadTimeout 那样从干净进程恢复世界）")
                    server_ok = False  # 落到下面重启分支
                else:
                    return True
        # 进程崩了（或重连失败/半连接降级）：重起进程再 /start
        if not self._launch_server():
            return False
        try:
            self._do_start(reconnect_position, reset=RESET_RECONNECT)
        except Exception:  # pylint: disable=broad-except
            logger.exception("[minecraft] 自愈 /start 失败")
            return False
        logger.info("[minecraft] 自愈：已重起 mineflayer 进程并 /start（reset=%s，"
                    "由 playerdata 恢复位置，不清库存/成就）", RESET_RECONNECT)
        self._unfreeze_after_heal()
        # 重启已是最后手段：再校验一次只记日志，不改变 return（仍半连接就交上层 /step 重试兜）。
        if not self._validate_world_after_heal():
            logger.error("[minecraft] 自愈：重启进程后世界仍为空 —— 交上层 /step 重试兜")
        return True

    def _unfreeze_after_heal(self) -> None:
        """自愈重连 bot 后**兜底补一次 /tick unfreeze**（2026-08-19 修，对应 alice 第7天冻死事故）。

        为什么需要：`/tick freeze` 是 MC 服务器侧持久状态，不随 bot 重连复位。而自愈只发 /start
        重连 bot —— `/start` 的 spawn handler 从不发 /tick unfreeze。于是「resume 时 bot 已掉线
        → /resume 返 400 → 自愈只接回 bot → 世界仍冻着 → timeTicks 恒定 → stall 护栏 abort」
        这条链需要一个解冻兜底。

        何时调用：`_heal_and_restart` 的两条成功路径末尾。此时 bot 刚 `/start` 成功、已 spawn
        （非空），Node 侧 /unfreeze 端点能经 bot.chat 真正发出 /tick unfreeze。bot 为空时该端点
        回 unfrozen:false —— 那意味着连 bot 都没接上，是更大的故障，由上层 /step 重试兜，本方法
        只 warning 不抛。

        best-effort：unfreeze 失败不影响 heal 的成功判定（bot 已重连才是 heal 的本职），故吞异常
        只记日志。每 arm 独立 MC server，给本 arm 解冻不会误伤别的 arm。
        """
        try:
            r = requests.post(f"{self.mineflayer_url}/unfreeze", timeout=10)
            r.raise_for_status()
            data = r.json() if r.content else {}
            if not data.get("unfrozen"):
                logger.warning("[minecraft] 自愈后补 /unfreeze：bot 仍未在线，世界可能仍冻结"
                               "（更大的故障，交由上层 /step 重试兜）")
            else:
                logger.info("[minecraft] 自愈后已补 /tick unfreeze（防 /tick freeze 持久化卡死世界）")
        except Exception:  # pylint: disable=broad-except
            logger.warning("[minecraft] 自愈后补 /unfreeze 失败（已忽略，不影响 bot 重连）",
                           exc_info=False)

    def _guard_world_health_after_step(self, obs: dict | None) -> None:
        """每步返回后检查世界健康。发现半连接/僵尸连接时触发自愈，避免 agent 在虚空/无 chunk
        状态下继续白给动作（dave 虚空、carol 位移、erin 回填疑似同源）。best-effort。
        """
        wh = (obs or {}).get("world_healthy")
        if not wh or wh.get("healthy"):
            return
        logger.warning(
            "[minecraft] /step 返回时世界状态异常：%s，将触发自愈", wh
        )
        try:
            # health 检查本身失败不抛，让调用方至少拿到本步 observe
            if not self._heal_and_restart():
                logger.error("[minecraft] 世界健康检查触发自愈失败")
        except Exception:  # pylint: disable=broad-except
            logger.exception("[minecraft] 世界健康检查触发自愈时异常")

    def _validate_world_after_heal(self) -> bool:
        """自愈重连后**校验世界是否真活**（2026-08-20 修，对应 dave flying 退场事故）。

        为什么需要：`_heal_and_restart` 的**轻量分支**（mineflayer 进程存活）只重发 /start 重连 bot，
        不重启进程。重连后 bot 可能处于「半连接」——连接层活（能读 position/timeTicks）但**没收到
        任何 chunk 数据**（world.getColumns() 返 0、blockAt 返 null）。dave flying 踢人后正是卡在这：
        bot 被接回来了，但世界是空的 → 动作全 unknown/超时 → timeTicks 恒定 → 空转护栏 abort 整轮。
        对比 carol ReadTimeout 自愈走「重启进程」分支，重连后世界正常恢复、未退场。

        两条自愈分支对 chunk 丢失的恢复能力不同：本方法把「轻量分支重连后世界仍空」检出，让
        `_heal_and_restart` 据此**降级走重启进程分支**（像 carol 那样），别停在半连接状态空转到退场。

        健康判据（保守，任一不满足即判死——宁可多重启一次也别停在半连接）：
          · bot_alive=True（连 bot 都没接上 → heal 本身失败，交上层 /step 重试兜）；
          · columns>0（没收到任何 chunk → 半连接铁证）；
          · feet_block 非 null（脚下格读不到 → 局部 chunk 缺失，与 dave 症状一致）。

        best-effort：探测本身失败（网络/超时/JSON 异常）返回 **True**（不阻塞 heal）——校验是兜底、
        不是阻塞，真半连接会在下一步 /step 暴露、由既有 stall/ReadTimeout 机制兜。每 arm 独立 MC
        server，给本 arm 探测不干扰别的 arm。返回 True=世界健康 / False=半连接需降级。
        """
        try:
            r = requests.get(f"{self.mineflayer_url}/world_health", timeout=10)
            r.raise_for_status()
            data = r.json() if r.content else {}
        except Exception:  # pylint: disable=broad-except
            logger.warning("[minecraft] 自愈后校验世界健康失败（已忽略，不影响 bot 重连）",
                           exc_info=False)
            return True
        if not data.get("bot_alive"):
            logger.warning("[minecraft] 自愈后 /world_health：bot 仍未在线（更大的故障，"
                           "交上层 /step 重试兜）")
            return False
        columns = data.get("columns", 0)
        feet = data.get("feet_block")
        if not columns or feet is None:
            logger.warning("[minecraft] 自愈后世界为空（半连接：columns=%s feet_block=%s）—— "
                           "bot 连着但 chunk 没恢复，应降级重启进程",
                           columns, feet)
            return False
        logger.info("[minecraft] 自愈后世界健康（columns=%s feet_block=%s）", columns, feet)
        return True

    def _do_start(self, position: dict | None, *, reset: str | None = None) -> dict:
        """POST /start。`reset=None` 用实例的 `reset_mode`（轮首重置）；自愈重连要显式传
        `RESET_RECONNECT` —— 见 `_heal_and_restart` 里那段。"""
        body = {"port": self.mc_port, "waitTicks": 20,
                "reset": self.reset_mode if reset is None else reset,
                "username": self.bot_name}
        # ★ OPENKAGE 倍速：透传 tick_rate，Node 侧 spawn 后两侧同步设速。
        if self.tick_rate and self.tick_rate > 1:
            body["tick_rate"] = self.tick_rate
        # ★ 放置埋点日志（2026-09-14）：Node 侧 blockPlaced 事件逐条 append（见 __init__ 注释）。
        if self.place_log_path:
            body["placeLog"] = self.place_log_path
        # ★ 进食/血量埋点日志（2026-09-17）：Node 侧 health 事件逐条 append（见 __init__ 注释）。
        if self.eat_log_path:
            body["eatLog"] = self.eat_log_path
        # ★ 观战相机探针（2026-09-15）：Node 侧 spectatorCam 采集器据此发 /data get 探相机位置。
        if self.spectator_name:
            body["spectatorName"] = self.spectator_name
        if position:
            body["position"] = position
        r = requests.post(f"{self.mineflayer_url}/start", json=body, timeout=START_TIMEOUT)
        r.raise_for_status()
        return _parse_observe(r.json())

    # ──────────────── 世界重置 ────────────────
    #
    # 为什么必须有这个：`/start reset:hard` 只发 /clear + /kill + /advancement revoke ——
    # **库存和成就清了，世界地形没清**。没有本方法时，第 2 轮的 bot 站在第 1 轮砍光树、
    # 挖开洞、放好工作台的世界里 → 学习曲线上升**无法区分「记忆变好」与「世界被前几轮开发过」**。
    #
    # 成本：全新世界首次生成约几十秒（spawn 区块生成很快，其余是 JVM + fabric mod 加载），
    # 存档几 MB。相对一轮探索的几分钟墙钟可忽略。

    def _mc_port_in_use(self) -> bool:
        import socket
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1)
            return s.connect_ex(("127.0.0.1", self.mc_port)) == 0

    def _stop_mc_server(self) -> None:
        """停掉占用 mc_port 的 MC server，等端口真正释放。

        两种情况都要处理：① 我们自己起的（self._mc_proc）→ terminate 优雅停（MC 收到 SIGTERM
        会存档退出）；② **别人起的**（start_servers.sh 的 nohup，首轮几乎总是这种）→ 按端口
        找 PID 杀。不释放端口就重起会 "Perhaps a server is already running"。
        """
        import signal
        import subprocess
        import time

        if self._mc_proc is not None:
            try:
                self._mc_proc.terminate()
                self._mc_proc.wait(timeout=MC_STOP_TIMEOUT)
            except Exception:  # pylint: disable=broad-except
                try:
                    self._mc_proc.kill()
                except Exception:  # pylint: disable=broad-except
                    pass
            self._mc_proc = None

        # 端口仍被占（别人起的 server）→ 按端口找 PID，先 TERM 再 KILL
        if self._mc_port_in_use():
            try:
                out = subprocess.run(["fuser", "-n", "tcp", str(self.mc_port)],
                                     capture_output=True, text=True, timeout=10).stdout
                pids = [int(p) for p in out.split() if p.strip().isdigit()]
            except Exception:  # pylint: disable=broad-except
                pids = []
            for pid in pids:
                try:
                    os.kill(pid, signal.SIGTERM)
                except Exception:  # pylint: disable=broad-except
                    pass
            for _ in range(MC_STOP_TIMEOUT):
                if not self._mc_port_in_use():
                    break
                time.sleep(1)
            for pid in pids:  # 还没退就硬杀
                if self._mc_port_in_use():
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except Exception:  # pylint: disable=broad-except
                        pass
        for _ in range(MC_STOP_TIMEOUT):
            if not self._mc_port_in_use():
                return
            time.sleep(1)
        logger.error("[minecraft] MC server 端口 %d 在 %ds 内未释放，重生世界可能失败",
                     self.mc_port, MC_STOP_TIMEOUT)

    def _set_level_seed(self, seed: str) -> None:
        """改 server.properties 的 level-seed（其余行保持原样，含 level-type/gamemode/difficulty）。

        只改这一行、整文件重写：避免引入 yaml/properties 解析依赖，也不动用户手调过的其他项。
        注意 MC 的 properties 里 level-type 值形如 `minecraft\\:normal`（冒号被转义），
        逐行处理不碰它才安全。
        """
        path = os.path.join(self.mc_dir, "server.properties")
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        out, found = [], False
        for ln in lines:
            if ln.startswith("level-seed="):
                out.append(f"level-seed={seed}\n")
                found = True
            else:
                out.append(ln)
        if not found:
            out.append(f"level-seed={seed}\n")
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(out)

    def _delete_world_save(self) -> None:
        """删掉 level-name 指定的存档目录（含 world_nether / world_the_end）。

        只删 server.properties 里 level-name 声明的那个目录名及其维度兄弟目录，不做通配删除
        ——避免误伤 mc_dir 下的 mods/libraries/versions 等。
        """
        import shutil
        level_name = "world"
        path = os.path.join(self.mc_dir, "server.properties")
        try:
            with open(path, "r", encoding="utf-8") as f:
                for ln in f:
                    if ln.startswith("level-name="):
                        level_name = ln.split("=", 1)[1].strip() or "world"
                        break
        except OSError:
            pass
        for suffix in ("", "_nether", "_the_end"):
            d = os.path.join(self.mc_dir, level_name + suffix)
            if os.path.isdir(d):
                shutil.rmtree(d, ignore_errors=True)

    def _launch_mc_server(self) -> bool:
        """起 MC server，**等服务器自己宣布 `Done (…)` 才算就绪**（新世界要先生成地形，
        故用 MC_START_TIMEOUT）。

        ⚠️ **判据不能用「端口能连」**：MC 的启动顺序是**先绑端口、后加载世界**——期间端口
        已监听但拒绝/挂起玩家登录。若一探到端口就返回 True，紧接着的第一次 `/start` 会撞上
        「服务器还没准备好接客」，mineflayer 那侧 `await bot.waitForTicks()` 等不到 tick，
        干等满 START_TIMEOUT 才失败换点。因为每次世界重生都要走一遍，这段耗时是
        `arms × epochs` 次的固定损耗，不是偶发抖动。

        `Done (` 是 MC 自己打的「可以接客了」信号，用它才是语义正确的判据。
        """
        import subprocess
        import time
        log_path = os.path.join(self.mc_dir, "openkage_server.log")
        # ★ 只认**本次启动**写进来的内容：日志以 "ab" 追加，文件里存着历次启动的 `Done (`，
        # 从头读会立刻匹配到上一次的、等于没等。故先记下当前文件尾偏移，之后只读它之后的字节。
        try:
            log_start_offset = os.path.getsize(log_path)
        except OSError:
            log_start_offset = 0
        with open(log_path, "ab") as logf:
            # ★ JVM 参数走 _MC_JVM_FLAGS（4G 堆 + OOM 诊断，见模块常量注释）。HeapDumpPath
            #   落 worker 目录（%p=PID 占位符），与 openkage_server.log 并排，崩溃后就近可查。
            heapdump_path = os.path.join(self.mc_dir, "java_pid%p.hprof")
            self._mc_proc = subprocess.Popen(
                [self.java_bin, *_MC_JVM_FLAGS,
                 f"-XX:HeapDumpPath={heapdump_path}",
                 "-jar", "fabric-server-launch.jar", "nogui"],
                cwd=self.mc_dir, stdout=logf, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
            )
        for _ in range(MC_START_TIMEOUT):
            if self._mc_proc.poll() is not None:
                logger.error("[minecraft] MC server 启动即退出（code=%s），见 %s",
                             self._mc_proc.returncode, log_path)
                return False
            try:
                with open(log_path, "rb") as f:
                    f.seek(log_start_offset)
                    fresh = f.read().decode("utf-8", errors="replace")
            except OSError:
                fresh = ""
            if 'Done (' in fresh:
                # 端口应当早已就绪（Done 在绑端口之后），这里再确认一次，两个条件都满足才放行。
                if self._mc_port_in_use():
                    return True
            time.sleep(1)
        logger.error("[minecraft] MC server %ds 内未就绪（没等到 `Done (`），见 %s",
                     MC_START_TIMEOUT, log_path)
        return False

    def regenerate_world(self, seed: str | None = None) -> bool:
        """**把世界恢复到原始状态**：停 MC server → 删存档 → 写 level-seed → 重起 server。

        实验协议用的是**固定 seed**：每次传同一个 seed → 地形逐块相同，
        每轮只是把上一条轨迹砍的树/挖的洞/放的工作台全部抹掉，回到初始地形。
        seed=None 时沿用 server.properties 现值（等价效果）。

        返回 True/False。**bot 侧不用管**：mineflayer 进程与 MC server 是两个进程，
        server 重起后下一次 /start 会重连（连不上会走既有自愈逻辑）。
        """
        if not self.mc_dir:
            logger.error("[minecraft] 未配置 mc_dir，无法重生世界（世界将跨轮持久）")
            return False
        import time
        t0 = time.time()
        # 先把 bot 摘下来：server 要停了，留着连接只会让它报一堆掉线错误
        self.stop()
        self._stop_mc_server()
        self._delete_world_save()
        if seed is not None:
            self._set_level_seed(seed)
        ok = self._launch_mc_server()
        if ok:
            self._world_generation += 1
            logger.info("[minecraft] 世界已重生（第%d次，seed=%s，耗时%.1fs）—— 地形回到初始状态，"
                        "只有记忆跨轮携带", self._world_generation, seed, time.time() - t0)
        return ok

    def save_world(self, *, settle_ticks: int = 40) -> bool:
        """显式存档：发 `/save-all` 并等落盘。供**透明分段的段末存档点**用。

        为什么不能只靠 SIGTERM 存档：`_stop_mc_server` 的 SIGTERM 确实会让 MC 存档退出
        （见其注释），但分段场景下我们要在**反思完成、/tick unfreeze 之后**这个安全点
        主动存档——这样段间即使发生异常/网络中断，最近的存档点已是干净状态。
        /tick freeze 状态持久化进存档（见 env.py 冻结相关注释），**必须在 unfreeze 后
        再 save_world**，否则存档里世界是冻的，续跑读档后 bot 动不了。

        settle_ticks：发完 /save-all 后等的 tick 数，给 MC 把存档写盘的时间。
        返回 True/False（失败只告警不抛，段末存档失败不该拖垮已跑完的轨迹）。
        """
        if not self._server_alive() and self.server_dir:
            self._launch_server()
        try:
            self.step(
                f"bot.chat('/save-all'); await bot.waitForTicks({settle_ticks});",
                "", None, "", True)
            logger.info("[minecraft] 世界已显式存档（/save-all，分段存档点）")
            return True
        except Exception:  # pylint: disable=broad-except
            logger.exception("[minecraft] /save-all 存档失败 —— 段末存档点未落盘，"
                             "续跑可能回到更早状态（不影响本次轨迹已跑完的部分）")
            return False

    def resume_world(self) -> bool:
        """**续跑读档**：不删存档、不换 seed，直接（重）起 MC server 读已有存档。

        与 `regenerate_world` 对立：regenerate 是「每轮 fresh 世界」（跨 epoch 协议），
        resume 是「同一条轨迹内透明分段续跑」——段间世界/物品/位置/时间全部从存档恢复，
        等价于一次性跑完。地基已实测（experiments/verify_save_resume.py）：SIGTERM 停服
        后 resume_world 续起，bot 位置+库存完全恢复。

        前提：存档是上段 `save_world()` 在 unfreeze 后存的干净状态。若上段在 /tick freeze
        中被杀，存档里世界是冻的 → 续跑首步要先 /tick unfreeze（由调用方在 reset 后处理）。
        """
        if not self.mc_dir:
            logger.error("[minecraft] 未配置 mc_dir，无法 resume（存档路径无从定位）")
            return False
        import time
        t0 = time.time()
        # bot 先摘下来（若还连着），再停 MC server。resume 不删存档、不动 seed。
        self.stop()
        # 若 server 还在跑，停掉再起以读档（MC 起服时从 level-name 目录读存档）；
        # 若已停（分段间断后），直接起。
        if self._mc_port_in_use():
            self._stop_mc_server()
        ok = self._launch_mc_server()
        if ok:
            logger.info("[minecraft] 世界已续跑读档（不重生，保留上段地形/物品/位置，"
                        "耗时%.1fs）—— 透明分段：等价于一次性连续跑", time.time() - t0)
        return ok

    def prewarm_and_spawn(self, pick: Callable[[], tuple[int, int]], *,
                          ring: int = SPAWN_PREWARM_RING,
                          max_tries: int = SPAWN_MAX_TRIES) -> dict:
        """随机出生点：**预热 → 传送 → 校验落点；没落下去就换一个点重抽**，直到成功。

        `pick()` 每次调用返回一个候选 (x, z)（由域层的确定性 RNG 提供，见 MinecraftDomain._make_spawn_pick）。

        ── 为什么要预热 ──
        MC server 启动只预热出生点那一圈（日志 `Preparing spawn area`），别处地形还不存在。
        bot 被传到未生成区块时地形虽会按需现场生成，但 mineflayer 的 `await bot.waitForTicks()`
        等待时间很短，超时会让整个 `/start` 卡死。**卡的不是传送本身** —— 服务器日志显示传送
        已成功，卡的是传送之后等 tick。故先 `/forceload add` 圈住候选点周围 ±ring 格让服务器
        主动生成，再传送 → 落地即已加载。预热量只与 ring² 有关、**与随机半径无关**，所以半径
        可以随便放大。

        ── 怎么判断"这个点能不能站" ──
        出生用 `/spreadplayers x z 0 1 under 320 false @s`（放到该柱体地表最高可站方块，
        省掉自己扫地形高度）。它在**水面/岩浆**等不可站立的柱体上会**静默失败**：
        失败信息只回给命令发送者、不进服务器日志，表现为 **bot 停在原地而 `/start` 仍返回成功**。

        所以判据就是**落点本身**：传送后比对实际坐标与目标，差太多 = 没落下去 = 该点不可站 → 换点。
        ⚠️ **不要试图"先读地表方块预检"**：`bot.blockAt()` 读不到远处方块（forceload 只让
        *服务器*生成区块，**客户端仍只收到自己附近的区块**，远处探测恒返回 null）。
        ⚠️ 也**不要"先 tp 到高空再 spreadplayers 兜底"**：spreadplayers 失败时 bot 会从 200 格
        自由落体**摔死**（除非正好落水）。换点才是对的做法。

        返回最终 observe payload（已落地并校验）。max_tries 次都没落成则抛 RuntimeError。
        """
        import time
        tried: list[tuple[int, int]] = []
        for attempt in range(1, max_tries + 1):
            x, z = pick()
            tried.append((x, z))
            t0 = time.time()
            phase = "起 mineflayer server"   # 失败措辞用；每进一步就更新，见下
            if not self._server_alive():
                self._launch_server()
            forceload_sent = False   # 只有真发过 forceload 才需要在 finally 里清理，见下
            try:
                # ① 锚在自然出生点（那里区块必然已加载），借这个连接发 forceload 预热候选点
                phase = "锚定自然出生点(/start)"
                self.reset(None)
                phase = f"预热候选点周围 ±{ring}(/forceload)"
                forceload_sent = True
                self.step(
                    f"bot.chat('/forceload add {x - ring} {z - ring} {x + ring} {z + ring}');"
                    f"await bot.waitForTicks(40);", "", trusted=True,
                )
                # ② spreadplayers 落到地表最高可站方块
                phase = "传送到候选点(/spreadplayers)"
                obs = self.reset({"x": x, "y": None, "z": z})
            except (requests.exceptions.Timeout,
                    requests.exceptions.ConnectionError) as e:
                # **瞬时故障，不打调用栈**：mineflayer 那侧 /start 等 tick 超时、或 bot 掉线，
                # 都是本方法用「换点重试」兜的**预期内**情况（max_tries=12 就是为它设的）。
                # 用 logger.exception 会吐出一长串 urllib3/requests 包装栈，把一次正常重试
                # 渲染得像崩溃，真正的故障反而被淹掉。栈里也没有额外信息：超时的位置永远是
                # requests.post，有用的「哪一步、第几次」已经在这行里了。
                # ⚠️ 常见诱因：MC server 的 pause-when-empty-seconds 空闲暂停 —— 世界重生后有空窗期，
                # bot 去连暂停中的服务器，握手要等 unpause，就会撞上 START_TIMEOUT。
                logger.warning("[minecraft] 出生点重试（第%d/%d 次）：%s 阶段 %s；换点 —— "
                               "瞬时故障，非坐标问题",
                               attempt, max_tries, phase, type(e).__name__)
                continue
            except Exception:  # pylint: disable=broad-except
                # 非超时的意外异常（解析错、服务器 500、代码 bug…）：**保留完整栈**，那是真需要看的。
                logger.exception("[minecraft] 出生点 (%d,%d) 在 %s 阶段失败 → 换点（第%d/%d 次）",
                                 x, z, phase, attempt, max_tries)
                continue
            finally:
                # ⚠️ **只在真发过 forceload 时才清理**：若无条件执行，「① /start 就超时」的情况下
                # （bot 根本没连上、forceload 压根没发出去）还要再对一个没有 bot 的 mineflayer 发
                # 一次 /step，等满 STEP_TIMEOUT —— 清理动作反而比失败本身贵得多。
                # 没预热过就没有常驻区块要清，直接跳过。
                if forceload_sent:
                    try:
                        self.step("bot.chat('/forceload remove all'); await bot.waitForTicks(10);", "",
                                  trusted=True)
                    except Exception:  # pylint: disable=broad-except
                        logger.warning("[minecraft] /forceload remove all 失败，可能留下常驻区块")
            # ④ 校验真到了（spreadplayers 静默失败的兜底，见 docstring）
            pos = (obs.get("status", {}) or {}).get("position", {}) or {}
            px, pz = pos.get("x"), pos.get("z")
            if px is not None and pz is not None \
                    and abs(px - x) <= SPAWN_ARRIVE_TOLERANCE \
                    and abs(pz - z) <= SPAWN_ARRIVE_TOLERANCE:
                logger.info("[minecraft] 出生点就位 (%d,%d) → 实际(%.0f,%.0f,%.0f) %s 耗时%.1fs"
                            "（第%d/%d 次抽点）",
                            x, z, px, pos.get("y", 0), pz,
                            (obs.get("status", {}) or {}).get("biome"), time.time() - t0,
                            attempt, max_tries)
                return obs
            # 没到位 = spreadplayers 静默失败 = 该柱体地表不可站（水面/岩浆）→ 换点
            logger.info("[minecraft] 出生点候选(%d,%d) 落不下去（多半是水面/岩浆，bot 仍在 %s,%s）"
                        " → 换点（第%d/%d 次）", x, z, px, pz, attempt, max_tries)
        raise RuntimeError(
            f"抽了 {max_tries} 个随机出生点都无法落地（试过 {tried[:8]}{'...' if len(tried) > 8 else ''}）。"
            "可能是随机半径落在大片海洋/岩浆区，或 forceload 被拒（bot 必须是 OP 名 'bot'）。"
            "可调小 --spawn-radius 或换 --world-seed。"
        )

    def reset(self, position: dict | None = None, *, resume: bool = False) -> dict:
        """POST /start：起（或重起）bot，返回初始 observe。

        - resume=False（默认，轮首语义）：**清空库存 + 成就**（reset=hard），传送到 position。
          与 `regenerate_world` 配合用：每轮先 regenerate_world(seed) 换地形，再 reset() 清库存起 bot。
        - resume=True（透明分段续跑语义）：**不清库存/不杀bot/不撤成就**（reset="none"），
          bot 连进来后位置/库存由存档 `playerdata/<uuid>.dat` 自动恢复。与 `resume_world` 配合用：
          段间续跑读档后，reset(resume=True) 让 bot 带着上段结束状态回来。**bot 名字（→UUID）
          必须与上段一致**，否则读不回同一个 playerdata（见 set_bot_name）。

        ⚠️ **本方法不碰世界地形** —— reset:hard 只发 /clear + /kill + /advancement revoke
        （见 mc_server/index.js）。resume 时连这些都不发。
        position=None 用世界自然出生点；给 {x,y,z} 则传送。若托管进程且服务器没起，先起它。
        """
        self._last_position = position
        if self.server_dir and not self._server_alive():
            self._launch_server()
        if resume:
            # 续跑：不传 hard。index.js:153 只判 reset==="hard"，传 "none" 不清库存，
            # bot 连进来位置/库存由存档恢复（地基已实测）。
            return self._do_start(position, reset="none")
        return self._do_start(position)

    def step(self, code: str, programs: str = "", timeout_ticks: int | None = None,
             say: str = "", trusted: bool = False) -> tuple[dict, list[tuple[str, str]], list[dict], dict]:
        """POST /step：执行一段 JS（programs 是注入的 primitive 辅助函数）。
        timeout_ticks：bot 自选的单步代码时限，**单位是游戏刻**（Node 侧 clamp 到
            [100,3000] 刻，None=用默认 500 刻）。
        say：执行前**发到游戏聊天框**的一句话（bot 本步在想什么），供人在客户端围观时看懂它的意图。
        trusted：**这段代码是 env 自己的基建脚本，不是 LLM 写的** —— Node 侧据此放行游戏指令。

        ★ trusted 为什么必须有：Node 侧会拦掉「LLM 生成代码里的斜杠指令」
        （防作弊，见 skillLoader.js）。而 env 自己的基建脚本走的是**同一个 /step 端点** ——
        `prewarm_and_spawn` 的 `/forceload add`、smoke 的 `/time set` 都在里面。若不显式声明
        来源，Node 侧会把这些基建脚本一起当成 LLM 代码拦掉：forceload 发不出去 → 候选点区块
        没预热 → spreadplayers 传送过去 bot 等不到 tick → /step 干等满 STEP_TIMEOUT。
        表现为"出生点莫名慢"。**来源必须由调用方显式声明，不能靠 Node 侧猜调用栈。**
        返回 (observe payload, events, system_events, skill_events)。

        system_events = 服务器权威播报（死亡/成就），见 onSystem.js。**每个 step 的返回都要收**
        —— 包括 /tick freeze、/time set 这类工具性 step：onSystem 的事件由 observe() 消费清空，
        任何一次 /step 都会触发 observe，若调用方丢掉这些 step 的 system_events，那期间到达的
        死亡播报就永久丢了（正是「跨步死亡漏计」要修的问题）。

        skill_events = 本步技能调用埋点增量 `{slug:{ok,fail}}`（见 onSkill.js）。只有注入了
        技能且 Agent 真调了才有内容；工具性 step（/time set 等）恒为 {}。
        """
        last_err = None
        body = {"code": code, "programs": programs}
        if timeout_ticks is not None:
            body["timeoutTicks"] = int(timeout_ticks)
        if say:
            body["say"] = say
        if trusted:
            body["trusted"] = True
        # HTTP 超时跟着本步申请的时限走（见 STEP_TIMEOUT_SLACK 的说明）：Node 侧到点会自己
        # 中止并正常返回超时反馈，这边必须比它晚，否则那次返回收不到、退化成假掉线自愈。
        # ★ timeout_ticks 的值是**游戏刻**：换算成墙钟秒 `刻 / (20 * tick_rate)` —— 2x 下 3000 刻
        #   = 75s（上限，需走 max(..., +slack) 抬高 HTTP 上限），倍速 N 下随 N 收缩、自动小于
        #   STEP_TIMEOUT=300s。这样「Node 刻超时先到、Python HTTP 后到」的不变式在任意倍速下都成立。
        http_timeout = STEP_TIMEOUT
        if timeout_ticks is not None:
            rate = max(1.0, float(getattr(self, "tick_rate", 1) or 1))
            wall_sec = int(timeout_ticks) / (20 * rate)
            http_timeout = max(STEP_TIMEOUT, wall_sec + STEP_TIMEOUT_SLACK)
        for attempt in range(self.max_heal_retries + 1):
            try:
                r = requests.post(
                    f"{self.mineflayer_url}/step",
                    json=body,
                    timeout=http_timeout,
                )
                r.raise_for_status()
                raw = r.json()
                obs = _parse_observe(raw)
                self._guard_world_health_after_step(obs)
                return obs, _collect_events(raw), _collect_system_events(raw), _collect_skill_events(raw)
            except (requests.exceptions.ConnectionError,
                    requests.exceptions.Timeout,
                    requests.exceptions.HTTPError) as e:
                # ★ HTTPError 必须在此列（2026-08-19 修）：raise_for_status() 抛的 400/500
                #   不是 ConnectionError 也不是 Timeout，不 catch 就穿透 for 循环、穿透本函数，
                #   被上层 except Exception 静默吞掉 —— _heal_and_restart 永不触发。
                #   alice 第7天冻死正是此路径：resume 时 bot 掉线 → /resume 返 400 → HTTPError 穿透。
                last_err = e
                logger.warning("[minecraft] /step 失败（第%d次）：%s；尝试自愈",
                               attempt + 1, type(e).__name__)
                if not self.server_dir or attempt >= self.max_heal_retries:
                    break
                if not self._heal_and_restart():
                    break
        # 自愈无门（或未托管进程）：抛给 run_exploration 记成一次失败步、继续下一步。
        # ★ ReadTimeout 单独打标（2026-08-25 修）：/step 挂满 http_timeout 才超时，几乎只有两种
        #   成因——① LLM 写了纯同步死循环（while 反复 dig 脚下之类）锁死了 Node 事件循环，tick
        #   超时与墙钟兜底都救不了（JS 单线程约束）；② bot 断流 timeOfDay 冻结（墙钟兜底已修，但若
        #   连事件循环都被锁死则同样失效）。两种都指向「代码把进程卡死了」而非「网络掉线」，故在
        #   消息里带 READTIMEOUT 标记，让 _run_step_body 给 Agent 一条能自救的反馈（改用 digDownTo），
        #   而不是只看到一句「连接失败」继续重写同样的死循环。
        is_read_timeout = isinstance(last_err, requests.exceptions.ReadTimeout)
        marker = "[READTIMEOUT 疑似死循环] " if is_read_timeout else ""
        raise RuntimeError(f"{marker}/step 连接失败且自愈无效：{last_err}")

    def pause(self) -> tuple[dict, list[tuple[str, str]], list[dict], dict]:
        """POST /pause：**真暂停** bot（黎明反思期间用），返回与 step 同形的四元组。

        两侧一起停（见 mc_server/index.js 的 pauseBot 注释）：
        ① 服务器侧 `/tick freeze` —— 怪/红石/时间/天气停；
        ② 客户端侧 `bot.physicsEnabled = false` + 掐断寻路/挖掘/控制输入 —— **这半才拦得住 bot 自己**。
        为什么必须有②：Minecraft 的 `/tick freeze` 明确豁免玩家（bot 就是玩家），且玩家移动是
        客户端权威的 —— 没有任何服务器命令能暂停 bot。而 /step 的超时靠 Promise.race 实现、JS 无
        promise 取消机制，超时只是「不等了」，那段代码仍在后台操纵 bot（孤儿会在反思的几十秒里
        继续挖矿、位置漂移，污染反思读到的轨迹与 tier 首达步序）。

        返回值同 step：**system_events 必须收**（/pause 那侧也 observe，死亡播报在此被消费）。
        第四项 skill_events 恒为 {}（技能调用只发生在 /step，pause/resume 不执行 Agent 代码）。
        """
        return self._post_observe("pause")

    def resume(self) -> tuple[dict, list[tuple[str, str]], list[dict], dict]:
        """POST /resume：恢复物理 + `/tick unfreeze`，返回与 step 同形的四元组。"""
        return self._post_observe("resume")

    def _post_observe(self, endpoint: str) -> tuple[dict, list[tuple[str, str]], list[dict], dict]:
        """POST 一个「无 body、返回 observe」的端点（/pause、/resume），解析同 step。
        自愈策略与 step 一致：连接被拒/超时先尝试自愈重起，无门则抛给调用方。"""
        last_err = None
        for attempt in range(self.max_heal_retries + 1):
            try:
                r = requests.post(f"{self.mineflayer_url}/{endpoint}", timeout=STEP_TIMEOUT)
                r.raise_for_status()
                raw = r.json()
                # skill_events 恒为 {}：pause/resume 不执行 Agent 代码，无技能调用埋点。
                return _parse_observe(raw), _collect_events(raw), _collect_system_events(raw), {}
            except (requests.exceptions.ConnectionError,
                    requests.exceptions.Timeout,
                    requests.exceptions.HTTPError) as e:
                # ★ HTTPError 必须在此列（2026-08-19 修，同 /step 那处）：/pause /resume 返
                #   400/500 时 raise_for_status() 抛的 HTTPError 不属 ConnectionError/Timeout，
                #   不 catch 就穿透到 run_exploration 的 except Exception 被吞、heal 永不触发。
                #   alice 第7天：resume 时 bot 掉线 → /resume 返 400（"Bot not spawned"）→
                #   HTTPError 4ms 内穿透 → world.resume() 静默失败 → 世界永久 /tick freeze。
                last_err = e
                logger.warning("[minecraft] /%s 失败（第%d次）：%s；尝试自愈",
                               endpoint, attempt + 1, type(e).__name__)
                if not self.server_dir or attempt >= self.max_heal_retries:
                    break
                if not self._heal_and_restart():
                    break
        raise RuntimeError(f"/{endpoint} 连接失败且自愈无效：{last_err}")

    def stop(self) -> None:
        try:
            requests.post(f"{self.mineflayer_url}/stop", timeout=10)
        except Exception:  # pylint: disable=broad-except
            pass

    def shutdown(self, *, stop_mc: bool = False) -> None:
        """彻底关掉：停 bot + 杀掉托管的 mineflayer 进程（若有）；`stop_mc=True` 时连 MC server 一起停。

        **默认不停 MC server**（哪怕是我们 regenerate_world 起的）：手工起的那台
        （`start_servers.sh`，端口 25567）是跨 run 复用的重资产，起一次要 45s，且用户常开着
        Mac 客户端围观。

        `stop_mc=True` 给 **worker 池**用：worker 的 server 是本次 run 的临时
        资产（每台并行跑，N 台同时占资源），跑完不停就是纯泄漏，上面那两条理由对它都不成立。
        见 worker_pool.MinecraftWorldPool.shutdown_all。
        """
        self.stop()
        if self._proc is not None:
            try:
                self._proc.kill()
            except Exception:  # pylint: disable=broad-except
                pass
            self._proc = None
        if stop_mc:
            self._stop_mc_server()


def _day_trajectory_text(day_idx: int, steps: list[dict]) -> str:
    """把「一个白天」的若干步渲染成文本，供夜晚反思 Agent 读。

    ★ **代码与反馈都不截断**（2026-08-12 修）：此前代码截 400 字符、每条反馈截 120，
    实测（2026-08-10 那轮，3 副脑 × 30 天 × 2150 步）**砍掉了全部代码的 58.8%**
    —— 代码长度 median 832 字符，84% 的步被截。而被砍掉的正是反思最需要的部分：
    截断点落在开头的注释与变量声明之后，`bot.dig` / `bot.craft` / `placeBlock` 这些
    **真正动手的调用**、以及 `catch` 里的失败分支，全在 400 字符之外。
    于是反思看到的是「它打算干什么」（注释+声明），看不到「它实际怎么干的、错在哪」——
    而后者才是经验层能归纳出「这类事该怎么做/会怎么错」的唯一依据。

    为什么不截断是安全的（三条，改回去前先重新核实）：
    ① **反思是按天的**，输入天然被「一天的步数」封顶，不随轨迹长度累积
       —— 实测解除截断后单天 prompt median 3.6万 token、最大 7.3万（涨 21.5%），
       对 200k 上下文毫无压力。
    ② **上游已经有硬上限**：onLog 单步封顶 40 行 × 300 字符（见
       `mc_server/lib/observation/onLog.js`）；onChat **2026-09-23 才补上** 40 条 × 300
       字符（此前它是 `obs += message` 无上限、且每条都发一次全量快照，见
       `mc_server/lib/observation/onChat.js`）；onError 是单条覆盖、不累积。
       ⚠️ 写「onChat 来自服务器播报、不存在无上限入口」是**错的**：chatEvent 是内部自造
       事件，唯一发射点是 skillLoader 的 bot.chat 改写，不受 MC 聊天 256 字节限制。
       这里再截一刀只是重复设限。
    ③ 代码本身也有上限：它是 LLM 单次 `write_code` 的产出，实测 max 3600 字符。

    ⚠️ 同批（2026-08-12）把另外两处截断也去掉了 —— `_run_step_body` 给白天 ReAct 的
    tool result（原 `v[:400]`）、整轮 `trajectory_text`（原 400/120）。各自的理由写在那两处，
    **不同于本函数**：前者在一天内累积、后者纯留痕。现在全链路不再有任何截断，
    「截断」这件事只发生在上游 onLog 的行数/行长封顶。
    """
    lines = [f"第 {day_idx} 天的探索轨迹："]
    for t in steps:
        lines.append(f"[第{t['step']}步 观察] {t['obs']}")
        lines.append(f"> 生成代码：{t['code'].strip()}")
        if t["events"]:
            ev_txt = "; ".join(f"{k}:{v}" for k, v in t["events"])
            lines.append(f"  [反馈] {ev_txt}")
        # 死亡/成就单独成行（服务器权威）：反思要能看到「哪一步死的、怎么死的」才能沉淀 pitfall
        for d in t.get("deaths") or []:
            lines.append("  [死亡] " + _death_line(d))
        for a in t.get("advancements") or []:
            lines.append(f"  [成就] {a}")  # a 是成就显示名（packet 源，list[str]）
    return "\n".join(lines)


def _format_events_full(events: list[tuple[str, str]]) -> str:
    """把一步的全部 onChat/onError/onLog 原文分块渲染，供日志排查（不截断）。

    这是日志里「bot 本步干了什么」的唯一事实源（[minecraft动作] 只报 tier/物品数）：
    onLog 可能几十行、onError 带完整 stack，都按类型分组原样打印，不做优先级取舍与截断。
    """
    label = {"onChat": "onChat（系统自报）", "onError": "onError（执行报错）",
             "onLog": "onLog（bot 自己的 console.log）"}
    lines = []
    for kind in ("onChat", "onError", "onLog"):
        msgs = [v for k, v in events if k == kind]
        if not msgs:
            continue
        lines.append(f"  ── {label[kind]} ──")
        for m in msgs:
            # 多行内容缩进对齐，视觉上归属到所属分块
            body = str(m).rstrip()
            lines.extend(f"    {ln}" for ln in body.splitlines() or [""])
    # 兜底：出现未预期的事件类型也别静默丢掉
    others = [(k, v) for k, v in events if k not in ("onChat", "onError", "onLog")]
    if others:
        lines.append("  ── 其他事件 ──")
        lines.extend(f"    [{k}] {v}" for k, v in others)
    return "\n".join(lines)


#: 同一天最多「续跑」几次 ReAct（LLM 提前停手但天没亮时重开）。给足余量：一夜 7 分钟、
#: 一轮 ReAct 少则十几步，正常最多续 1~2 次；超过这个数说明它在反复立刻停手，该报错而非空转。
_MAX_RESUME_PER_DAY = 5

#: ★ 同一天最多容忍几轮「一次 write_code 都没调」（2026-09-22 用户定：2 → 5 → 8）。
#:   2 太紧，会把**探索到高原、主动 stop 的最强臂误杀**——实测三次退场（dsexp3 两次、
#:   nomen 一次）全是当时 XP 最高、装备已齐的那臂：模型认为当天没事可做而返回 stop，
#:   write_code=0 连续 2 轮即被判空转 → 退场，整轮采样作废。给到 8 轮余量后仍不动，
#:   才走「强制守夜等天亮」，不再退场（与 _MAX_RESUME_PER_DAY 超限同一处理）。
#: ⚠ 实际生效的容忍轮数还受 _MAX_RESUME_PER_DAY 约束：只要当天天没亮，resume_streak
#:   也在累计，它先到阈值就先触发守夜（两者行为一致，只是触发点不同）。
_MAX_IDLE_ROUNDS_PER_DAY = 8


async def _react_day_loop(react_day_cb, state: dict, do_step, render_obs, get_events) -> None:
    """ReAct 模式的外层「天循环」。

    结构::

        while 整轮没结束:
            state["day_over"] = False
            await react_day_cb(...)   # 跑**一整天**的 ReAct，天亮时靠 should_stop 收尾
            # 天亮的暂停/反思已在 do_step 内部按原顺序做完了

    要点：
    - **天亮立刻结束这一天的 ReAct**：do_step 检测到夜→昼跳变时把 `state["day_over"]` 置 True，
      而 react_day_cb 传给 ReActAgent 的 should_stop 读的正是这个标志。ReActAgent 在**下一次
      think 之前**检查它 → 立刻返回 EXTERNAL_STOP，中间不会再插入任何工具调用。
    - **反思不受上下文压缩影响**：反思读的是 env 侧独立累积的 `day_steps`（见 _run_step_body 里
      的 day_steps.append），不是 LLM 的 history。这两套本来就是分离的。
    - **STOP ≠ 天亮**：ReActAgent 有两个出口 —— `EXTERNAL_STOP`（天亮，我们要的）与
      `STOP`（LLM 不再调工具）。LLM 可能在计划全标 success 后就 STOP，它以为「活到天亮」这步
      做完了，而游戏里天还没亮。若把 STOP 也当成一天结束（day_idx+1、计划清空、dump 换文件），
      "第 2 天"其实会是同一个夜晚的后半段。做法是：**天没亮就 resume=True 续跑同一天**
      （保留计划与 day_idx，只重开一轮 ReAct）。根因是 Agent 缺少「我要等」的正当表达 ——
      已单独注入 `waitUntil` 原语（0827 去原语后只保留这一个「等待」原语，见
      load_wait_until_primitive）；Agent 主动过夜用 bot.sleep 跳夜（夜注入 header 轻引导「夜里也有
      能做的事」、不强推方向），续跑兜底则用 waitUntil(['dawn']) 让时间流逝到天亮。
      不要自制 waitForTicks 循环、不要停止调用工具。
      续跑超限（_MAX_RESUME_PER_DAY）时不再退场，改由 env 侧 do_step 跑 waitUntil
      让时间自然流逝到天亮（见下方续跑超限分支）。
    - 防呆：若某天 ReAct 一次 write_code 都没调（例如 LLM 直接给最终答复），世界不会前进、
      day_over 永远不会变 True → 死循环。故这里检查步数是否推进，没推进就累计 guard；
      ★ 2026-09-22 起 guard 达 _MAX_IDLE_ROUNDS_PER_DAY(5) **不再中止/退场**，改走「waitUntil
      守夜等天亮」（与续跑超限同一处理），避免把探索到高原、主动 stop 的最强臂误杀。
    """
    guard = 0
    resume = False        # 本次调用是否「续跑同一天」
    resume_streak = 0     # 连续续跑次数：LLM 每轮都立刻停时不能无限重开
    while not state["run_over"]:
        state["day_over"] = False
        # 入夜强制检索标记的跨天兜底：正常情况下入夜后当夜内就被 react 层 on_step 消费清成 False；
        # 极端情况（入夜那步后 ReAct 立刻 STOP 续跑、首轮首步才消费）也由 on_step 兜住。这里再 pop
        # 一次确保开新天干净——防止上一夜的残留标记在新一天的 ReAct 首步被误当「刚入夜」触发一次。
        state.pop("night_just_fell", None)
        steps_before = state["step_idx"]
        reason = await react_day_cb(state, do_step, render_obs, get_events, resume=resume)
        if state["run_over"]:
            break
        advanced = state["step_idx"] != steps_before
        # 天亮了（day_over）→ 正常进下一天；否则说明 LLM 自己停了 → 续跑同一天
        if state["day_over"]:
            resume, resume_streak = False, 0
        else:
            resume_streak += 1
            if resume_streak > _MAX_RESUME_PER_DAY:
                # ★ 不退场（2026-09-08，原 break → retire 丢臂）：LLM 反复 STOP 续跑熬不到天亮，
                #   多半是自制了无效的 waitForTicks 守夜循环（waitForTicks 等客户端 physicsTick、
                #   不推进游戏时间）。改由 env 侧循环 do_step 跑 waitUntil(['dawn'])：waitUntil 用
                #   服务器 timeOfDay 计时，世界按默认倍速自然流逝。ticks 上限 3000（与步上限对齐），
                #   一次等不到天亮就再等一轮，直到天亮判据触发反思。详见 load_wait_until_primitive。
                logger.warning(
                    "[minecraft] 同一天已续跑 %d 次仍未到天亮（reason=%s）—— 不退场，"
                    "改由 env 侧 waitUntil 守夜，让时间按默认倍速自然流逝到天亮进反思",
                    resume_streak - 1, reason)
                # 循环等天亮：每轮 waitUntil 最多等 3000 刻（步上限），天亮判据在 do_step 内
                # _run_step_body 触发 day_over=True 时退出。安全阀防异常死循环。
                _force_dawn_guard = 0
                while not state["day_over"] and not state["run_over"] and _force_dawn_guard < 20:
                    _force_dawn_guard += 1
                    await do_step(
                        "await waitUntil(bot, 3000, '续跑超限强制守夜等天亮', ['dawn']);",
                        3000,  # ticks 上限 3000（与 STEP_CODE_TIMEOUT_MAX_TICKS 对齐）
                        "续跑超限强制守夜")
                # ★ 必须手动重置：continue 跳过第 2770 行的 `resume, resume_streak = False, 0`
                #   （那条在 react_day_cb 之后的 day_over 分支里，本路径走不到）。不重置则下一天
                #   STOP 会立刻再触发超限。反思已在 do_step 内的 _run_step_body 完成（天亮判据
                #   env.py:3561 触发 pause+night_cb+day_over=True），continue 回顶部 day_over=False
                #   重置是为下一天准备——正确。
                resume, resume_streak = False, 0
                continue
            logger.info(
                "[minecraft] ReAct 提前结束（reason=%s）但天还没亮 → 续跑同一天（第 %d 次）",
                reason, resume_streak)
            resume = True
        if not advanced:
            # 这一轮没执行任何代码 —— 世界没动，再来一轮也是同样结果
            guard += 1
            logger.warning(
                "[minecraft] ReAct 本轮未执行任何 write_code（世界未前进，第 %d 次）", guard)
            if guard >= _MAX_IDLE_ROUNDS_PER_DAY:
                # ★ 2026-09-22 改：原来直接 break（→ 该臂退场、整轮作废）。现在与「续跑超限」
                #   同一处理：不退场，改由 env 侧循环 do_step 跑 waitUntil(['dawn'])，让世界按
                #   tick_rate（默认 2.5 倍速，Node 侧 spawn 时已同步设速）自然流逝到天亮进反思。
                #   这样「探索到高原主动 stop」的臂只是白等一夜，不会丢臂。
                logger.error(
                    "[minecraft] 连续 %d 轮没有任何动作 —— 不退场，改由 env 侧 waitUntil 守夜，"
                    "让世界按 tick_rate 倍速自然流逝到天亮进反思", guard)
                _force_dawn_guard = 0
                while not state["day_over"] and not state["run_over"] and _force_dawn_guard < 20:
                    _force_dawn_guard += 1
                    await do_step(
                        "await waitUntil(bot, 3000, '空转保护强制守夜等天亮', ['dawn']);",
                        3000,  # ticks 上限 3000（与 STEP_CODE_TIMEOUT_MAX_TICKS 对齐）
                        "空转保护强制守夜")
                # ★ 手动重置（continue 跳过了「续跑」分支里的重置）：不重置则下一天一停手
                #   就会立刻再触发本分支。
                guard = 0
                resume, resume_streak = False, 0
                continue
        else:
            guard = 0


async def run_exploration(
    world: MinecraftWorld,
    react_day_cb: Callable[..., Awaitable[Any]],
    *,
    programs: str = "",
    max_nights: int = DEFAULT_MAX_NIGHTS,
    night_cb: Optional[Callable[[str, int, dict, list], Awaitable[str]]] = None,
    smoke_dawn_nudge: bool = False,
    spawn_pick: Optional[Callable[[], tuple[int, int]]] = None,
    recorder: Any = None,
    start_night: int = 0,
    resume_spawn: bool = False,
    checkpoint_cb: Optional[Callable[[int, dict], Awaitable[None]]] = None,
    initial_last_events: "list[tuple[str, str]] | None" = None,
    resume_metrics: dict | None = None,
    on_spawn_ready: Optional[Callable[[], Awaitable[None]]] = None,
) -> dict:
    """跑一整轮自由探索：reset → bot 顺**自然昼夜**探索（白天+夜晚都真实经历，
    夜晚有怪、要自保）→ 检测到**夜→昼**跳变（熬过一夜、天亮）即算过完一天：**冻结游戏(/tick freeze)
    → 反思（安全，bot 不挨打、SQLite 无并发）→ 解冻(/tick unfreeze) 进入下一天** → 数满 max_nights
    个夜晚结束。

    **max_nights 是轨迹长度的唯一闸门**：一条轨迹跑多少步完全由「过完 D 个夜要多少步」决定，
    步数不设上限——若与步数上限并存，每夜实际所需步数的波动会让步数闸门先到，max_nights
    就成了名义值。

    为何在夜→昼冻结反思：① 天亮是安全时刻（怪被烧/消失），符合"活过一晚复盘昨天"直觉；
    ② 冻结游戏刻 → 反思期间世界时间停止，bot 不会在"思考"时被怪打死，也不用异步/加锁
    （反思写副脑图谱与主循环下步检索天然串行，无 SQLite 并发）。

    昼夜自然流速（不 /time set 强制）：bot 真经历夜晚危险，"建庇护所/自保"靠模型+记忆自涌现
    （prompt 只给"世界有危险"提示，不给步骤）。一轮墙钟因此变大（要等真实昼夜循环），max_nights 封顶。

    night_cb(day_traj_text, day_idx, discoveries) -> reflection_text：写副脑图谱 + 返回次日指导文本，
    文本拼进后续步 last_events。night_cb=None 时退化为「只过夜、不反思」。

    死亡埋点：bot 血量归零会死亡（掉物品、重生）。检测 health<=0 记 death，死亡本身是探索表现的一部分。

    返回 dict：n_steps / n_nights / n_deaths / unique_items / items_seen / peak_tier / tier_rank /
      final_tier / final_tier_rank / tier_first_step / tier_progress / trajectory / trajectory_text /
      final_inventory / final_equipped / night_reflections /
      path_len / max_dist_from_spawn / n_pos_jumps / n_biomes / biomes / n_pos_samples
      （后一行是 map coverage，见 _summarize_path）
    """
    # ★ recipe 诊断薄壳：每轮读一次缓存（见 _load_recipe_wrap_source），拼进每步 LLM 代码步的
    #   programs 头部。只包 LLM 代码步（_run_step_body），不碰 trusted 基建步（smoke_dawn_nudge
    #   那种 /time set）。开关 _RECIPE_WRAP_ENABLED 关掉即还原纯原生 API 静默行为做对照。
    _recipe_wrap = _load_recipe_wrap_source()
    if _recipe_wrap:
        logger.info("[minecraft] recipe 诊断薄壳已注入（OPENKAGE_RECIPE_WRAP=1）")
    # 出生点：spawn_pick=None 用世界自然出生点；给了取点函数则走「预热→检查能否站→不能就换点」
    # 的随机出生流程（见 MinecraftWorld.prewarm_and_spawn），返回时 bot 已落地并校验过。
    if resume_spawn:
        # ★ 透明分段续跑：不重抽出生点、不清库存。bot 带着上段存档的位置/库存回来
        # （与上层 resume_world 配合，地基已实测）。spawn_pick 在续跑段应传 None。
        obs = await asyncio.to_thread(world.reset, None, resume=True)
    elif spawn_pick is not None:
        obs = await asyncio.to_thread(world.prewarm_and_spawn, spawn_pick)
    else:
        # 自然出生点：区块由服务器启动时预热，直接起
        obs = await asyncio.to_thread(world.reset, None)
    # 真实出生点（env 层"换点重抽"后才定下来，故由这里回报给调用方做留痕/复现）
    _sp = (obs.get("status", {}) or {}).get("position", {}) or {}
    spawn_actual = {k: (round(_sp[k], 1) if _sp.get(k) is not None else None)
                    for k in ("x", "y", "z")} if _sp else None
    spawn_biome = (obs.get("status", {}) or {}).get("biome")

    def _fmt_pos(p: dict | None) -> dict | None:
        """把位置格式化成 `{x,y,z}`（每轴 round 1 位、逐轴 None 守卫），无位置返回 None。

        `p` 可以是**裸位置 dict**（如 `spawn_actual`、`status.position`）—— 出生/复活点日志共用，
        保证两处格式与 metrics 里 `spawn_actual` 的 round 口径一致，不重复写 round 逻辑。
        定义须在出生点日志（下方 _spawn_pos_txt）之前：那行调用早于复活点日志处也用它。
        """
        if not p:
            return None
        out = {}
        for k in ("x", "y", "z"):
            v = p.get(k)
            out[k] = round(float(v), 1) if v is not None else None
        if all(v is None for v in out.values()):
            return None
        return out

    # ★ 出生点位置进日志（2026-09-01 加）：此前 spawn_actual/spawn_biome 只写进 metrics、不进日志，
    #   换 seed 排查「开局落点对不对」只能事后读 JSON。这里打一行实时可见。
    #   kind 标明出生方式：续跑段不重抽（存档恢复位）、随机出生（--no-fixed-spawn）、自然出生（默认）。
    _spawn_kind = ("续跑" if resume_spawn
                   else "随机出生" if spawn_pick is not None else "自然出生")
    _spawn_pos_txt = _fmt_pos(spawn_actual) or "位置未知"
    logger.info("[minecraft] 出生点（%s）：位置 %s，生物群系 %s",
                _spawn_kind, _spawn_pos_txt, spawn_biome or "未知")
    if smoke_dawn_nudge:
        # 仅 smoke：一次性把时间设到 midnight 附近（~18000），bot 探索几步就自然流到黎明，
        # 快速验证「夜→昼冻结反思」链路。正式跑不传此参数 → 纯自然昼夜。
        try:
            obs, _, _, _ = await asyncio.to_thread(
                world.step, "bot.chat('/time set 18000'); await bot.waitForTicks(5);", "",
                None, "", True)
        except Exception:  # pylint: disable=broad-except
            logger.warning("[minecraft] smoke dawn nudge /time set 失败，退回自然昼夜")
    # ★★ **纯串行附身**（2026-08-20）：客户端已在 rollout 里同步等到就绪（footage.start），
    #   bot 刚出生上线，此刻附身 + 开第一天录制。`_enter_spectate` 内部仍轮询等 `Now spectating`
    #   正面回执（bot joined 落进 client.log 有窗口，且死亡重附也复用本路径），常态首轮即成。
    #   附身要发 /tp + /spectate（约 4.5s 墙钟，剪贴板+按键 sleep），走 to_thread 不阻塞 event loop。
    #   失败不中断轨迹（录制是观测手段，绝不成为训练故障源）—— recorder 内部已吞异常。
    if recorder is not None:
        try:
            await asyncio.to_thread(recorder.attach_and_record)
        except Exception:  # pylint: disable=broad-except
            logger.warning("[minecraft] 录制器 attach_and_record 异常，继续无录制探索",
                           exc_info=False)
    # ★ 开局出生栅栏（2026-09-12）：所有臂 bot 都出生（+附身尝试）后才一起迈第一步训练，
    #   防「4 臂已开训、录像卡死的臂晚 150s 才出生」。放在 attach_and_record 之后：bot 出生
    #   （world.reset/prewarm_and_spawn）是事实，附身失败只影响录像不影响出生，故无论附身成功
    #   与否都调。录像卡死的臂经 recorder.start 150s 降级后也会到本栅栏（只是晚点无录制）。
    #   on_spawn_ready=None（eval/单臂）跳过。纯等齐无超时——录像卡死时全员最多白等 150s。
    if on_spawn_ready is not None:
        await on_spawn_ready()
    # ★ 续跑缺陷①（2026-08-19）：items_seen/biomes_seen/tier_progress/tier_progress_steps 从
    #   resume_metrics 恢复（前段累积），让 unique_items/tier_first_step 含前段。trajectory 不恢复
    #   （太大，metrics 不需要完整 trajectory）——故 path_len/max_dist 仍只算续跑段（已知局限）。
    _rm = resume_metrics or {}
    items_seen: set[str] = set(_rm.get("items_seen", []))
    #: ★ **主动建造过的物品名**（2026-08-22，每日建造指标的来源）。
    #:   与 items_seen 不同：items_seen 只在步末采样 `inventory`，而「合成工作台后**同一步**立即
    #:   placeItem 放下」是 Agent 极常见模式 —— 放置后工作台从库存消失，items_seen 漏采
    #:   （smoke 实测 alice 造了工作台却报 buildings=[]）。故 buildings 不用 items_seen 代理，
    #:   改用本集合：从每步 code 文本里解析 craftItem/placeItem 的物品名参数累积
    #:   （见 _extract_built_from_code），「主动合成/放置过」即一次建造能力事件，不受放置后消失影响。
    #:   再 ∩ _is_buildable 过滤掉非建造物（craftItem 也合成 wooden_pickaxe 等工具，非建造物）。
    built_items: set[str] = set(_rm.get("built_items", []))
    # ★ **累计 XP 记账**（2026-08-25，score 改用 MC 累计 XP 的数据源）：
    #   MC 死亡界面的"得分"= 自上次死亡以来挣的 XP，重生后服务器压低 bot.experience.points。
    #   我们要的是**跨命累计总 XP**（死亡不扣减），故不能直接累加 points（死后下降会把累计拉低）。
    #   做法：维护单调递增的 _xp_total + 上一步读数 _xp_last，每步只把**正增量**加进 _xp_total ——
    #   死亡产生的负增量自动被跳过，_xp_total 天然跨命单调累计，无需在死亡点做任何重置。
    #   _xp_last=None 表示尚未读到过首步 points（首步不产生增量，仅记录基线）。
    _xp_total: int = int(_rm.get("xp_total", 0))
    _xp_last: int | None = _rm.get("xp_last")  # 上一步的 bot.experience.points，算增量用
    #: 走过的生物群系集合（map coverage 的一部分）。与 items_seen 同样是只增不减的累积集合，
    #: 由 _track 每步更新。
    biomes_seen: set[str] = set(_rm.get("biomes_seen", []))
    trajectory: list[dict] = []
    tier_progress: list[int] = list(_rm.get("tier_progress", []))  # 每步的 base 进度深度(0-9)，画「第几步达到某深度」
    #: 与 tier_progress 同下标：该条目对应的**真实 step 数**（=len(trajectory) 快照）。
    #: _track 除了每个真实 step 调一次，黎明 world.resume() 后还会额外调一次补记账（无对应的
    #: trajectory 条目）——若直接拿 tier_progress 的下标当步数（如 tier_first_step 曾经那样），
    #: 每过一夜后面所有下标都会虚高 1。这份影子列表让「下标」与「真实步数」解耦，谁要报步数
    #: 查这份，不要查 enumerate 出来的 i。
    tier_progress_steps: list[int] = list(_rm.get("tier_progress_steps", []))
    # ★ 续跑缺口②残余（2026-08-21）：tier_progress_steps 续跑段要接在前段累计步序上（见 _track
    #   里 _step_seq_baseline + len(trajectory)）。baseline = 前段 step_idx（从 resume_metrics 恢复，
    #   含前段全部步数）；新跑段 _rm 为空 → baseline=0，与原 len(trajectory) 逐字节一致（严格泛化）。
    _step_seq_baseline: int = int(_rm.get("step_idx", 0))
    #: 本步的「库存+装备」计数快照（_track 每步刷新），与 _prev_holdings 相比得出无声丢失。
    #: 用单键 dict 而不是普通变量：_track 是闭包，赋值裸变量需要 nonlocal，这样最省事且不易漏。
    _last_holdings: dict[str, dict[str, int]] = {}
    _prev_holdings: dict[str, dict[str, int]] = {}

    def _holdings(o: dict) -> dict[str, int]:
        """当前**总持有量** = 库存计数 + **库存区间外**的装备槽各 1。

        为什么要把装备槽并进来（否则会误报）：护甲/副手不在 `bot.inventory.items()` 的 9-44
        区间内，穿上即从库存消失（见 equipped_item_names）。只比库存的话，「穿上铁头盔」会被
        当成「头盔凭空丢了」——那是假警报，比不报更浪费排查时间。

        ★★ **主手必须排除**（2026-08-11 修）：主手是快捷栏槽 36-44，**在** 9-44 区间内，
        所以它本来就已经计在 `inventory` 里了 —— 再 +1 就是同一把镐算两遍。
        `equipped_item_names` 自己的 docstring 早写明了这件事（「主手是快捷栏，**在**区间内，
        没问题」），是这里并槽时没照着做。

        后果不是"数字略大"，而是**制造假的丢失事件**：手持一把 stone_pickaxe 时持有量记成 2，
        它耐久断裂后变 0，diff 出来就是刺眼的 `stone_pickaxe 2→0` —— 看着像"两把镐同时凭空
        消失"，实际是一把镐正常用坏。2026-08-10 那轮 23 条无声丢失警报里有 11 条是这么来的，
        排查时会把人往"孤儿代码偷走物品"的错方向带。

        `_MAIN_HAND_SLOT` 之外的五个槽（四件护甲 + 副手）才真在区间外、才该 +1。
        实测那轮 238 份观察：主手物品 237 次同时出现在库存行里，护甲/副手 0 次 —— 与此一致。
        """
        inv = o.get("inventory", {}) or {}
        out = {k: int(v) for k, v in inv.items()}
        for name in equipped_item_names(o.get("status", {}) or {}, exclude_main_hand=True):
            out[name] = out.get(name, 0) + 1
        return out

    def _snap(kind: str, step_idx: int, meta: dict | None = None) -> None:
        """关键事件抓一帧游戏画面（recorder=None 时什么都不做）。

        录制是**观测手段，不能成为训练的故障源**：recorder 内部已吞掉所有异常，
        这里再兜一层，确保任何录制问题都不会打断轨迹。

        `day_idx` 取自 `_state`，不是闭包自由变量 —— 首次 `_track(obs)` 发生在
        `day_idx = 1` 赋值**之前**，直接引用会 NameError 打断整条轨迹。
        """
        if recorder is None:
            return
        try:
            recorder.snap(kind, day=int(_snap_state.get("day", 1)),
                          step=step_idx, meta=meta or {})
        except Exception:  # pylint: disable=broad-except
            logger.debug("[minecraft] 抓帧失败（已忽略）: %s", kind, exc_info=False)

    #: 抓帧要用的当天序号。与主循环的 day_idx 同步推进（见下方 `_snap_state["day"] = day_idx`）。
    _snap_state = {"day": 1}

    # ★ 续跑缺陷①：deaths/advancements 从 resume_metrics 恢复（前段累积），必须在 _track 定义
    #   **之前**赋值——_track 引用 advancements（算 base_depth），且 _track(obs) 在下方首次调用
    #   早于这些变量原赋值点会导致 NameError（free variable 未绑定，2026-09-05 起跑即崩已踩）。
    deaths: list[dict] = list(_rm.get("deaths", []))         # 服务器权威死亡记录
    advancements: list[dict] = list(_rm.get("advancements", []))   # 服务器权威进度里程碑
    # ★ packet 成就 diff 基线（2026-09-07，成就消费源从 onSystem 切 packet）：历史已见成就显示名。
    # _track 每次拿步末 packet completed 全集与此对比，得本步新增成就 → 记账 + 步反馈 + 日志。
    # 续跑从恢复后的 advancements 列表初始化（前段已见的不重复计）；_track 改读 packet 全集算
    # tier（root=[Minecraft] 进计数，修 tier 0→2 跳跃根因）。详见 plan lexical-booping-mango。
    _seen_advancements: set[str] = {a.get("advancement") for a in advancements
                                    if a.get("advancement")}

    # ★ _state 必须在 _track 定义**之前**初始化（2026-09-07 修）：_track 内部记账/抓帧读
    #   _state["step_idx"]（见 _new_advs 循环），而首次 _track(obs)（见下方 _track(obs) 首调）
    #   早于原 _state 赋值点 → 若首步 packet 恰好含成就（_new_advs 非空）会 NameError 起跑即崩。
    #   此前靠「spawn 时只推 recipes 树、不进 title map → 首步 _new_advs 恒空」兜底，是脆弱的
    #   隐式时序保证（py_compile/单测查不出）。这里把初始化上移消除该依赖。
    _state = {"obs": obs,
              "step_idx": int(resume_metrics.get("step_idx", 0)) if resume_metrics else 0,
              "day_over": False, "run_over": False,
              "extra_programs": "",
              # ★ 技能调用计数（见 plans/structured_skill_count_channel.md）：每步从 /step 响应的
              #   onSkill 结构化事件累加（step() 内 _collect_skill_events 读成增量）。night_cb 反思
              #   时回填副脑 skill 表的 success_count/fail_count，聚合据此降级高失败率技能。
              #   {slug: {"ok": n, "fail": n}}
              "skill_calls": {},
              # ★ 技能来源映射（{slug: "primary"|"secondary"}）：注入时定归属，随 skill_calls 同
              #   管道透传 night_cb，回填时按来源分流（secondary 技能→副脑、primary 技能→主脑，
              #   主脑写入持 _lock 防多臂并发踩踏）。生命周期与 skill_calls 完全对齐。
              "skill_sources": {},
              # ★ 技能代码全量注入（createplan 时 _inject_all_skills 把两图谱全部 verified 技能的
              #   js_code 拼进来）：每步 write_code 的 programs 带上，Agent `await 技能名(bot,…)`
              #   直接可调（与原语同通道、不进 LLM）。黎明清零（次日 createplan 重新注入）。
              #   签名块不进 state——已拼进 createplan toolresult（用 SKILL_BLOCK_MARKER 标边界）。
              "extra_programs": ""}

    def _absorb_advs(o: dict | None, sys_events: list[dict] | None) -> list[str]:
        """成就两路并集喂总变量 _seen_advancements + 记账，返回本批新增成就名。

        ★ 唯一总成就变量的**唯一写入点**（2026-09-14 源头并集）：两条采集支线在此汇入——
            ① packet 协议包（o["advancementPacket"].completed 步末快照全集，能采 announce=false
              的静默 root）；
            ② onSystem 聊天播报（sys_events 里 kind=="advancement"，能采 announce=true 的子进度
              如 fishy/plant_seed；packet 失效期是它们的唯一来源）。
          两路先并集再 diff，同一成就两路都报只记一次。下游（落盘 advancements 列表/观察前沿
          progress_frontier/tier base_depth/步反馈）全部只读 _seen_advancements，一条线。

        独立于 _track 的原因：_track 每步一次（带 tier_progress 节奏副作用），而 pause/resume
        栅栏边界（步初 resume / 步末 pause / 日界 pause / 黎明 resume / resume 异常补 pause）各自
        收到**独立的一批** onSystem 事件（observe 消费即清空、批次不重复），这些边界也要喂总变量
        ——但不能再动 tier_progress（每步一点的节奏），故抽出本函数供 _track 与各边界共用。

        并集/diff 核心抽成模块级纯函数 union_advancement_sources（可单测，见
        tests/test_advancement_feedback_dedupe.py）。
        """
        nonlocal _seen_advancements
        _new_advs, pkt, _onsys = union_advancement_sources(o, sys_events, _seen_advancements)
        _seen_advancements |= (pkt | _onsys)
        # 新增成就记账 + 抓帧 + 日志。step/day 取 _state/_snap_state（非闭包自由变量——首次
        # _track(obs) 发生在 day_idx/_state 赋值之前，直接引用会 NameError；_new_advs 此时为空
        # 不进本循环，故安全。同 _snap 的 day 取法见 _snap docstring）。
        for _name in _new_advs:
            advancements.append({"step": _state["step_idx"],
                                 "day_idx": _snap_state.get("day", 1),
                                 "advancement": _name, "cause": None, "text": ""})
            _snap("advancement", _state["step_idx"], {"advancement": _name})
            # 来源标注：两路都报的成就标 packet+onSystem，单路的标实际来源
            _src = ("[packet+onSystem]" if (_name in pkt and _name in _onsys)
                    else "[onSystem]" if _name in _onsys else "[packet]")
            logger.info("[minecraft] 成就达成（step%d）：%s %s",
                        _state["step_idx"], _name, _src)
        return _new_advs

    def _track(o: dict, sys_events: list[dict] | None = None):
        """步末记账：items/生物群系/成就/tier/XP。返回本步新增成就名 list[str]。

        成就经 _absorb_advs 源头并集（见其 docstring）；本函数补 items/biome/tier/XP 的
        每步记账。tier 读 _seen_advancements（env 闭包态）而非 packet 进程态——自愈 ② 重启
        mineflayer 后 pkt 清零、懒推送不补 story 全量，_seen_advancements 跨重连只增不减。
        """
        # ★ _seen_advancements 用 |= 推进（赋值语句）→ 必须声明 nonlocal，否则 Python 把它当
        #   _track 局部变量，首次读 pkt - _seen_advancements 时未绑定→UnboundLocalError
        #   （记忆铁律：嵌套函数给外层变量赋值漏 nonlocal，单测/py_compile 查不出必真 run）。
        #   advancements.append/items_seen.update 是方法调用非赋值，不需 nonlocal。
        nonlocal _xp_total, _xp_last, _seen_advancements
        # 装备槽（护甲/副手）不在 inventory 里，两处指标都要并进来 —— 见 equipped_item_names。
        inv = o.get("inventory", {}) or {}
        eq = equipped_item_names(o.get("status", {}) or {})
        items_seen.update(inv.keys())
        items_seen.update(eq)
        # 走过的生物群系（map coverage 的第三个分量，见 _summarize_path）。在 _track 里收而不是
        # 在步循环里收：黎明 resume 也会调它，那时 bot 可能已经被重生到别的群系了。
        _biome = (o.get("status", {}) or {}).get("biome")
        if _biome:
            biomes_seen.add(_biome)
        # 成就两路并集（packet 快照 + onSystem 本步事件）——见 _absorb_advs
        _new_advs = _absorb_advs(o, sys_events)
        # ★ tier 读 _seen_advancements（env 闭包态）而非 pkt（mineflayer 进程态）：pkt 是
        #   advancementPacket.js 维护的「已完成」集合，构造时为空、靠增量协议包累积；自愈走 ②
        #   重启进程分支（_launch_server 重起 mineflayer）时这个集合被清空，而 MC server 按懒推送
        #   只补 recipes 树不补 story 全量，重连后 pkt 恒空 → tier 永久归零（2026-09-07 bob 事故：
        #   step187 tier=5 → 19:24 ReadTimeout 重连 → step188 tier=0 卡到结尾）。
        #   _seen_advancements 是 run_exploration 闭包变量、_heal_and_restart 不碰它 → 轮中重连存活；
        #   它每步 `_seen_advancements |= pkt` 只增不减，重连前达成的进度留在集合里。故 tier 改读它，
        #   与 timeTicks 之于 elapsedTime 同构（服务器态 vs bot 进程态，见下方时钟停摆护栏注释）。
        #   pkt 仍用于上面的 diff 基线（_new_advs）与留痕（daily_log.advancement_packet）。
        tier = base_depth(_seen_advancements)
        # depth 突破（比历史高水位更高）时抓一帧：这是「能力增长」最直观的画面证据。
        # 用高水位而非「与上一步比」—— 死亡掉装备会让 depth 回落，回落后再拿到同级进度
        # 不算新突破，否则同一个里程碑会被反复拍。tier_progress 现存 base depth 整数(0-9)，
        # 直接比大小即可（旧口径存 9 级 tier 名、靠 _TIER_RANK_9 查表，2026-09-05 改）。
        if tier_progress and tier > max(tier_progress):
            _snap(f"tier_{tier}", len(trajectory), {"tier": tier})
        tier_progress.append(tier)
        # ★ 续跑缺口②残余（2026-08-21）：tier_progress_steps 要含前段累计步序，否则续跑段
        #   tier_first_step 拿到本段小步序 → 对报 Voyager Table 1「解锁该级用了几步」在分段跑下失真。
        #   但 trajectory 不恢复（续跑段从空起），直接 len(trajectory) 只算本段。用 baseline 偏移：
        #   新跑段 baseline=0（与原 len(trajectory) 逐字节一致，严格泛化），续跑段 baseline=前段
        #   step_idx（从 resume_metrics 恢复）。补记账点不 append trajectory，故其值与上一真实步相同
        #   （单调性保持，tier_first_step 取首次达成的点不受影响）。
        tier_progress_steps.append(_step_seq_baseline + len(trajectory))
        _last_holdings["cur"] = _holdings(o)
        # ★ 累计 XP（score 数据源，2026-08-25）：只累加正增量，跨命单调递增。
        #   规则见 _accumulate_xp（抽成纯函数便于测试）。points 在 status.experience.points。
        _xp_now = (o.get("status", {}) or {}).get("experience", {}).get("points")
        _xp_total, _xp_last = _accumulate_xp(_xp_total, _xp_last, _xp_now)
        return _new_advs  # 本步新增成就名，供主循环做步反馈「你达成了成就 X」

    _track(obs)
    last_events: list[tuple[str, str]] = list(initial_last_events) if initial_last_events else []

    # ★ 透明分段续跑偏移：续跑段从第 start_night 夜之后续起。day_idx/n_nights 从 start_night
    # 起，使 night_cb 的 day_idx、聚合窗口号（day_idx // aggregate_every）与一次性跑 30 夜
    # 完全对齐——这是"透明"的关键：分段跑出的窗口号、反思编号和一次跑不可区分。
    # deaths/advancements/tier_progress 等仍是**本段内**累积，由上层（brain/run）按段拼接成完整轨迹。
    day_idx = start_night + 1
    n_nights = start_night
    # ★ 续跑缺陷①：deaths/advancements/night_reflections 从 resume_metrics 恢复（前段累积），
    # 让 n_deaths/n_advancements 含前段。day_items_before 用恢复后的 items_seen。
    # （deaths/advancements 已在 _track 定义前赋值，见上方；night_reflections 在下方。）
    day_steps: list[dict] = []      # 当天（自上个黎明以来）的步，反思后清空
    day_items_before: set[str] = set(items_seen)  # 当天开始时的物品集，算当日 discoveries
    #: 当天开始时的 built_items 快照，算「当日新建造」（与 day_items_before 同模式）。
    day_built_before: set[str] = set(built_items)
    #: 当天开始时的 _xp_total 快照，算「当日新增 XP」（与 day_items_before 同模式）。
    _xp_at_day_start: int = _xp_total
    was_night = is_night(obs)       # 出生即夜：was_night 初值 True，影响入夜检测（不再等昼→夜跳变）。
                                   # ★ 日界由 worldDay 滚动判定（见主循环 _day_rolled），与 was_night 无关。
    night_reflections: list[dict] = list(_rm.get("night_reflections", []))
    # 时钟停摆护栏的状态（见主循环内的检测）：last_tick=上一步的 timeTicks（服务器权威游戏刻）。
    # 取字段的优先级要与那边**保持一致**（timeTicks 优先、退化到 elapsedTime）：不一致会让第一步
    # 拿 None 跟数字比，白记一次 stalled。
    last_tick = (obs.get("status", {}) or {}).get("timeTicks")
    if last_tick is None:
        last_tick = (obs.get("status", {}) or {}).get("elapsedTime")
    # ★ 日界判据基准（2026-09-10 纠正）：last_day=上一步末的 worldDay（服务器权威累计天数，
    #   status.js 暴露，mineflayer time.js day=floor(time/24000)）。日界 = worldDay 滚动 = 真正穿过
    #   24000/0（**不是** 22000 的夜→昼跳变 —— 那是首次天亮不是下一天临界点）。熬夜（timeOfDay 自然
    #   推进过 24000）与 sleep 跳夜（服务器把累计 time 推进到下一个 24000 倍数，实测 14005→24010）
    #   都让 worldDay +1，都走主判据 `cur_day > last_day`。worldDay 缺失时退化到 timeTicks 回绕兜底
    #   （见主循环 _ticks_wrapped），同为 24000/0 口径。**用相对增量不用绝对值** —— bot 出生时
    #   worldDay 不一定是 0（世界可能已跑很久），与框架「第 N 天」有偏移，对齐反而错。
    last_day = (obs.get("status", {}) or {}).get("worldDay")
    stalled_steps = 0
    # ★ 0930 自愈熔断计数（carol 骑船事故）：resume/step 两处 broad-except 共享，
    #   连续失败达 _MAX_HEAL_FAIL_STREAK(10) 抛 HealFuseError（registry 放行 → rollout-abort）。
    #   与 stalled_steps 同为闭包计数器，但状态机在 _HealFuseCounter（可单测）。
    _heal_fuse = _HealFuseCounter()

    def _record_system(sys_events: list[dict], step_idx: int,
                       holdings_before: dict[str, int] | None = None,
                       holdings_after: dict[str, int] | None = None,
                       respawn_pos: dict | None = None) -> tuple[list[dict], list[dict]]:
        """把 onSystem 的结构化播报记进 deaths，返回本次新增的 (死亡, 成就)。

        每个 world.step 之后都要调（含 freeze/unfreeze 这类工具 step）——onSystem 的事件由 Node 侧
        observe() 消费清空，漏调一次就永久丢那批事件。这正是「LLM 思考/反思期间被打死」不再漏计的关键。

        ★ **成就记账仍只走 packet（落盘），onSystem 成就仅做运行时反馈（2026-09-14 更新）**：
          成就的**落盘**记账/diff/日志在 `_track`（读 packet 协议包全集，能采到 onSystem 漏掉的
          root=[Minecraft]）。本函数 now **只管死亡**——返回的 new_advs 恒为空 list（保留是为了维持
          6 个调用点的签名兼容），kind==advancement 事件在此处 continue 跳过、不进 advancements 列表。
          onSystem.js 成就侧已于 2026-09-14 恢复采集（不再即弃）：announce=true 子进度的聊天播报重新
          被收进 onSystem 事件，经 _collect_system_events 抽出后，在 _system_feedback 调用点与 packet
          diff 做**差集**进步反馈（packet 失效期兜底子进度，packet 正常时差集为空不双计）。落盘准确性
          靠 ground-truth 回补脚本兜底，不靠 onSystem。

        ★ **记日志也在这里做**（2026-08-14 移进来）：原先「第N次死亡」的 logger 与喂给 LLM 的
        onError 文案都写在 `_run_step_body` 里、在它自己那次调用**之后** —— 于是另外两个调用点
        （天边界的 `world.pause()` / `world.resume()`）拿到的死亡**只进 deaths 和抓帧、永不上日志**。
        实测 2026-08-13 那轮 carol step495：events.jsonl 有 3 条死亡而日志只有 2 条，第 3 条
        （Zombie Villager）恰在步末→冻结之间到达。`n_deaths` 与反思都不受影响（deaths 列表是全的），
        坏的是**日志与落盘数据对不上**，而排查时人正是拿这两者互校的。
        放这里就与调用点无关了：任何路径收到的播报都会被同样地记账、抓帧、上报。

        序号 `_first_ord` 必须**逐条算**，不能在循环里读 `len(deaths)`：本函数已把这批死亡
        append 完，循环里那个长度是**恒定的终值** —— 一步内死 4 次会连报 4 次「第 59 次」，
        且总数跳号（实测那轮：48→50、55→59）。跨步积压的死亡（LLM 思考/反思期间被打死，
        见 onSystem.js）让「一步多死」成为常态而非边界情况；这个序号会随 onError 喂给 LLM，
        报错等于让它拿错误的自身状态做决策。
        """
        new_deaths: list[dict] = []
        for e in sys_events or []:
            if e.get("kind") != "death":
                continue  # 成就已切 packet（_track 管），onSystem 成就侧即弃，此处只收死亡
            rec = {"step": step_idx, "day_idx": day_idx,
                   "cause": e.get("cause"), "killer": e.get("killer"),
                   "position": e.get("position"), "timeOfDay": e.get("timeOfDay"),
                   "nearby": e.get("nearby") or [], "text": e.get("text"),
                   "holdings_before": holdings_before, "holdings_after": holdings_after,
                   # ★ 重生落点（2026-09-01 加）：区别于 position（死亡地点，onSystem 死亡瞬间
                   #   快照的 bot.entity.position）。respawn_position 是本步/本边界返回后 bot 的实际
                   #   位置 = 重生落点（keep_inventory + mineflayer 自动重生，落回世界出生点/床）。
                   #   由调用方按各自场景传最新 obs 的 status.position（见三处调用点）。
                   "respawn_position": respawn_pos}
            deaths.append(rec)
            new_deaths.append(rec)
            # ★ 死亡帧必须**同步**抓：MC 死后立刻重生，晚一步现场（尸体/凶手/地形）就没了。
            #   单帧约 20ms；recorder=None 或录制失败时是 no-op，不影响轨迹。
            _snap("death", step_idx, {"cause": rec["cause"], "killer": rec["killer"],
                                      "position": rec["position"]})
        _first_ord = len(deaths) - len(new_deaths) + 1
        for i, d in enumerate(new_deaths):
            logger.info("[minecraft] bot 死亡第%d次（step%d）：%s", _first_ord + i, step_idx,
                        _death_line(d))
            # ★ 重生落点单独打一行（2026-09-01 加）：_death_line 的「位置」是死亡地点，
            #   这里是死后重生落点，标签用「重生位置」区分、序号与上方死亡行对齐。
            #   打在 _record_system 内（不是 _run_step_body），天边界 pause/resume 两调用点
            #   收到的跨步死亡才不会漏报（同「死亡日志必须在 _record_system 内」那条守卫）。
            _rp_txt = _fmt_pos(d.get("respawn_position")) or "位置未知"
            logger.info("[minecraft] 重生位置（第%d次）：位置 %s", _first_ord + i, _rp_txt)
        return new_deaths, []  # 成就恒空（已切 packet/_track）；保留二元组维持调用点签名

    def _system_feedback(new_deaths: list[dict], new_advs: list[str],
                         holdings_before: dict[str, int] | None,
                         holdings_after: dict[str, int] | None) -> list[tuple[str, str]]:
        """把刚记账的死亡/成就转成**给 LLM 看的**反馈条目（onError / onChat）。

        与 `_record_system` 分开：记账在**每个** step 之后都要做，而这些文案只在有
        `last_events` 可挂的地方才有意义（天边界的 freeze/resume 步不回传给 LLM ——
        那两下的死亡会通过当日 `deaths_today` / `deaths_summary` 进入夜反思）。

        序号口径与 `_record_system` 的日志**保持一致**：都按「这批在 deaths 里的位置」算。

        ★ 死亡反馈**只报事实对比，不下「掉没掉」的结论**（2026-08-24 改，见 memory
        openkage_system_bug_experiences_fixed / erin「死亡掉落全部物品」误判）：
        此前写死「装备与物品保留未掉落（keep_inventory=true）」，但 agent 重生后看到
        空背包（实为本步放置方块把仅有的原木用光了、非死亡掉落）会无视这句断言、
        反按「看到空包」误记成「死亡掉光物品」并污染公共脑。改为直接给死前→重生后的
        持有量 diff，agent 自己看多了/少了/不变，不会被一句与眼见矛盾的主观断言带偏。
        holdings_before/after 取自本步开始与结束的 `_holdings(obs)`（库存+装备槽口径）。
        """
        out: list[tuple[str, str]] = []
        first_ord = len(deaths) - len(new_deaths) + 1
        for i, d in enumerate(new_deaths):
            diff_line = ""
            if holdings_before is not None and holdings_after is not None:
                diff_line = f"持有量（本步开始→重生后）：{_holdings_diff(holdings_before, holdings_after)}。"
            out.append((
                "onError",
                f"你死了（第{first_ord + i}次）：{_death_line(d)}。已重生。"
                f"{diff_line}"
            ))
        for name in new_advs:
            # name 是 packet diff 得出的成就显示名（含 onSystem 漏掉的 root=[Minecraft]）。
            out.append(("onChat", f"你达成了成就 {name}。"))
        return out

    # ★ 续跑缺陷①：_state["step_idx"] 从 resume_metrics 恢复（前段累计步数），让
    #   n_steps/tier_first_step 含前段。_state 的初始化已上移到 _track 定义之前（见上方）——
    #   _track 内部读 _state["step_idx"]，首调早于原赋值点会 NameError。
    # ★ `extra_programs`：react_day 每天可往这里塞「当天全量注入的技能 JS 代码」（见
    #   __init__._inject_all_skills：两图谱全部 verified 技能，不按 goal 筛选、不需声明），
    #   _run_step_body 会把它拼在原语 programs 之后注入 /step —— Agent 的 write_code 代码里就能
    #   `await skillX(bot,...)` 直接调（与 mineBlock 原语同机制，代码不进 LLM 上下文）。空=不追加。

    async def _do_one_step(code: str, step_timeout_ticks: int | None, explain: str = "") -> str:
        """执行一步：跑代码 → 记账 → 处理昼夜/死亡/成就 → 返回**给 LLM 看的反馈+新观察**。

        返回值就是 write_code 的 tool result。
        """
        nonlocal last_events, was_night, last_tick, stalled_steps
        nonlocal day_idx, day_steps, day_items_before, day_built_before, n_nights
        step_idx = _state["step_idx"]
        obs_local = _state["obs"]
        # 进度行按已达成进度算（5 树前沿）。★ 读 _seen_advancements（env 闭包态）而非步末 packet：
        #   packet 是 mineflayer 进程态，自愈 ② 重启进程后清零、懒推送不补 story 全量 → 重连后进度
        #   前沿全空，Agent 误以为没进度而重复造轮子。_seen_advancements 跨重连存活、只增不减，
        #   含历史已达成的全部进度（含 packet 采到的 root=[Minecraft]，base L1 不再缺）。
        adv_names = set(_seen_advancements)
        obs_text_local = render_observation(obs_local, world.tick_rate, adv_names)
        if explain:
            logger.info("[minecraft动作] step%d 计划：%s（自选时限 %s）", step_idx,
                        explain.replace("\n", " ")[:80],
                        f"{step_timeout_ticks} 刻" if step_timeout_ticks else "默认")
        # 自选时限始终打在 [minecraft代码] 这行（[minecraft动作] 计划行只在有 explain 时才出），
        # 便于不依赖 plan 也能逐步看到 Agent 申请了几刻：None=默认时限，数字=该步自选的游戏刻。
        logger.info("[minecraft代码] step%d code（自选时限 %s）：\n%s", step_idx,
                    f"{step_timeout_ticks} 刻" if step_timeout_ticks else "默认", code)
        # ★ 累积「主动建造过的物品」：从 code 文本抓 craftItem/placeItem 的物品名参数。
        #   放在执行前/后都行（只解析文本、不依赖执行结果）；放这里（执行前）即使步超时也能抓到意图。
        #   built_items 是闭包外层 set，.update 不需 nonlocal。见 _extract_built_from_code 的理由。
        built_items.update(_extract_built_from_code(code))
        # explain 同时用于三处：日志、聊天框（say）、以及不进 tool result（那里已有反馈+观察）
        feedback_text = await _run_step_body(obs_text_local, code, step_timeout_ticks, step_idx,
                                             say=explain)
        _state["step_idx"] = step_idx + 1
        return feedback_text

    async def _run_step_body(obs_text: str, code: str, step_timeout_ticks: int | None,
                             step_idx: int, say: str = "") -> str:
        """单步的全部副作用与记账。返回「执行反馈 + 新观察」文本（ReAct 的 tool result 用）。

        say：本步发到**游戏聊天框**的一句话（bot 在想什么），供人在客户端围观时看懂它的意图。
        """
        nonlocal last_events, was_night, last_tick, last_day, stalled_steps
        nonlocal day_idx, day_steps, day_items_before, day_built_before, n_nights
        nonlocal _xp_total, _xp_last, _xp_at_day_start
        # ★ 时间栅栏：进 world.step 前先 resume 解冻（上一步末 trailing pause 冻着了世界，思考期间
        #   游戏时间被冻结正是栅栏的目的）。resume 早于 holdings_before 拍快照——resume 的 obs 不赋
        #   给 _state["obs"]（holdings_before 要的是步前=上一步末快照，resume 返的是解冻后新快照，
        #   两者时间上几乎一致但语义不同，不能混淆）。system_events 照常 _record_system 收（/resume
        #   那侧也 observe，跨步死亡播报在此消费，与 dawn 路径同模式见 env.py pause docstring）。
        _step_ran = False  # world.step 是否真跑成功（stall 护栏 fence-aware 用，见下方护栏处）
        # ★ 时间栅栏：进 world.step 前先 resume 解冻（上一步末 trailing pause 冻着了世界，思考期间
        #   游戏时间被冻结正是栅栏的目的）。resume 早于 holdings_before 拍快照——resume 的 obs 不赋
        #   给 _state["obs"]（holdings_before 要的是步前=上一步末快照，resume 返的是解冻后新快照，
        #   两者时间上几乎一致但语义不同，不能混淆）。system_events 照常 _record_system 收（/resume
        #   那侧也 observe，跨步死亡播报在此消费，与 dawn 路径同模式）。
        try:
            _ro, _, _rsys, _ = await asyncio.to_thread(world.resume)
            _record_system(_rsys, step_idx,
                           respawn_pos=(_ro.get("status", {}) or {}).get("position"))
            # ★ 思考期晚到的 onSystem 成就播报在这批 _rsys 里（observe 消费即清空、不重复），
            #   喂总变量防漏；packet 快照此时无新内容（世界刚解冻），_absorb_advs 传 _ro 亦无妨。
            _absorb_advs(_ro, _rsys)
            # ★【栅栏验证插桩】步初 tick：resume 解冻后立刻读 timeTicks。与步末 tick 对比：
            #   stepN末==stepN+1初 ⟹ 思考期冻住（栅栏生效）；stepN末<stepN+1初 ⟹ 思考期世界在动（栅栏漏）。
            #   临时验证用，确认后可删。
            _t0_tick = ((_ro.get("status", {}) or {}).get("timeTicks"))
            logger.info("[栅栏验证] step%d 初 tick=%s（resume 后，应等于上步末 tick）", step_idx, _t0_tick)
            _heal_fuse.record_success()  # resume 成功 → 自愈失败计数清零（0930 熔断，与 step 侧共享）
        except Exception:  # pylint: disable=broad-except
            # resume 失败 = 世界仍冻着 → world.step 里 waitForTicks 会挂到步超时。栅栏下 resume 每步
            # 都调（原先只在 dawn 调），后果更重。失败则跳过本步 world.step（_step_ran 保持 False），
            # 喂一条错误反馈让 agent 自救一轮，stall 护栏因 _step_ran=False 不计 stall。
            # ★ 0930 加熔断计数：这条「降级自救」路径正是 carol 死循环空转 1h42m 的通道——
            #   mineflayer 自愈死循环时 resume 每步都失败，agent 拿着同一句错误反馈原地 probe，
            #   进程全活、无退场，守护链/巡检全失明。连续失败达 _MAX_HEAL_FAIL_STREAK 就熔断。
            #   HealFuseError 不能被本 except 重新吞掉：抛出后直接穿出（它不进 pause 兜底）。
            if _heal_fuse.record_failure():
                _heal_fuse.trip("时间栅栏 resume")
            logger.warning("[minecraft] 时间栅栏 resume 失败，跳过本步 world.step 让 agent 自救",
                           exc_info=True)
            last_events = [("onError",
                            "本步恢复世界时间失败（resume 异常），世界仍冻结，未执行你的代码。"
                            "请重试。")]
            # 直接走 trailing pause（重新冻结，本就冻着也无害）再 return，跳过 world.step 全流程。
            if not _state["run_over"]:
                try:
                    _pobs, _, _psys, _ = await asyncio.to_thread(world.pause)
                    _record_system(_psys, step_idx)
                    _absorb_advs(_pobs, _psys)  # 异常路径的成就播报也喂总变量
                except Exception:  # pylint: disable=broad-except
                    logger.warning("[minecraft] 时间栅栏 resume 失败后补 pause 也失败", exc_info=False)
            from .react_tools import OBS_SEPARATOR
            _fb_resume_err = "执行反馈：\n  [onError] " + last_events[0][1]
            return f"{_fb_resume_err}\n{OBS_SEPARATOR}\n当前观察：\n{render_observation(_state['obs'], world.tick_rate, set(_seen_advancements))}"
        obs = _state["obs"]
        sys_events: list[dict] = []
        # ★ 死前持有量快照：在 world.step 动世界**之前**拍，= 本步开始时（库存+装备槽口径）。
        #   给 _system_feedback 算「死前→重生后」diff 用（见其 docstring）。obs 此刻还是步开始值，
        #   下面 world.step 返回后 obs 会被 next_obs 覆盖，这里不先存就拿不到死前了。
        holdings_before = _holdings(obs)
        # ★★ 录制的时间锚点：**只包住 world.step 这一段**。
        #   `step_idx` 是 **JS 步**（一次 write_code = 一段真正在游戏里发生的动作），
        #   **不是 ReAct 轮**：ReAct 还会调 createplan/updateplan/retrieve/expand，那些只在
        #   LLM 侧发生、游戏世界一动不动、也就没有对应的画面。实测第1天 alice 有 48 个 ReAct
        #   轮、其中只有 34 次 write_code —— 若按 ReAct 轮切视频，会凭空多出 14 个"空块"，
        #   而且后面每一块的时间都错位。
        #   t0/t1 取相对**当天那一段**录像起点的秒数，直接就是 dayNN.mp4 里的 [起, 止]
        #   （录像按天切段，见 footage.end_day / begin_day）。
        _t0 = recorder.elapsed() if recorder is not None else None
        # ★ 当天命中的技能代码拼在原语 programs 之后（见 _state["extra_programs"]）：
        #   反思产出的技能在这里被注入，Agent 的 write_code 代码里 `await skillX(bot,...)` 直接可用。
        _extra = _state.get("extra_programs") or ""
        # ★ recipe 诊断薄壳拼在最前（运行时基建，先于原语/技能代码生效，包装 bot.recipesFor）。
        #   幂等（bot.__openkageRecipeWrap 标记）—— 每步都拼进去 eval 也只真正包装一次。
        #   行号映射安全：薄壳行数计入 programs.split("\n").length 偏移，LLM 代码行号自动正确。
        _parts = [_p for _p in (_recipe_wrap, programs, _extra) if _p]
        _step_programs = "\n".join(_parts)
        try:
            next_obs, events, sys_events, skill_inc = await asyncio.to_thread(
                world.step, code, _step_programs, step_timeout_ticks, say)
            _step_ran = True  # world.step 真跑成功（stall 护栏 fence-aware 用）
            _heal_fuse.record_success()  # 真跑成功 → 自愈失败计数清零（0930 熔断，与 resume 侧共享）
        except Exception as e:  # pylint: disable=broad-except
            # /step 网络/执行层异常：记为一次失败步，喂错误反馈给下一轮，不中断整条轨迹。
            # ★ ReadTimeout 单独点明根因（2026-08-25 修）：挂满 300s 才超时几乎只意味着「代码把
            #   执行进程锁死了」——纯同步死循环（while 反复 dig 脚下之类）。光说「连接失败」Agent
            #   会重写同样的死循环，点名根因让它自己换写法。具体怎么挖交给原语表/prompt，这里不啰嗦。
            # ★ 0930 加熔断计数：与 resume 失败侧共享 _heal_fuse——carol 死循环里 /step 的
            #   ReadTimeout 与 /resume 的 400 交替出现，两侧任一连续失败 10 次都该熔断。
            if isinstance(e, _heal_fuse_error_cls()):
                raise  # 熔断信号不降级（self-heal 内部抛的，见 _post_observe/step 的 trip 调用链）
            if _heal_fuse.record_failure():
                _heal_fuse.trip("world.step")
            msg = str(e)
            if "READTIMEOUT" in msg or "Read timed out" in msg:
                events = [("onError",
                           "这一步把执行进程锁死了整整 300s 才被超时杀掉（ReadTimeout），"
                           "你的代码很可能跑进了死循环。")]
            else:
                events = [("onError", f"step 调用异常：{e}")]
            next_obs = obs
            skill_inc = {}  # /step 没成功返回，无 onSkill 埋点
        # ★ 技能调用埋点计数（见 plans/structured_skill_count_channel.md）：本步 onSkill 结构化
        #   事件（技能代理 push 进 bot.openkageSkill，步末随 /step 响应回传，由 step() 内
        #   _collect_skill_events 读成增量），累加进 skill_calls。night_cb 反思时回填副脑 skill 表
        #   success_count/fail_count，聚合据此降级高失败率技能。
        #   ★ 走结构化通道而非扫 onLog `[skill]` 日志行（2026-08-26 改）：旧法 _parse_skill_call_events
        #     靠字符串契约维持 JS/Python 同步，任一端漂移就静默丢数；onSkill 是机读事件，与 onSystem
        #     同机制，丢数可检测。skill_inc 是 step() 已解析好的增量，这里只 merge。
        if skill_inc:
            _skill_calls = _state.get("skill_calls") or {}
            for _slug, _inc in skill_inc.items():
                _slot = _skill_calls.setdefault(_slug, {"ok": 0, "fail": 0, "errs": []})
                _slot["ok"] += _inc.get("ok", 0)
                _slot["fail"] += _inc.get("fail", 0)
                # 失败样本去重后累加，封顶 _SKILL_ERR_SAMPLES（见 _collect_skill_events）。
                for _err in _inc.get("errs") or []:
                    if _err not in _slot["errs"] and len(_slot["errs"]) < _SKILL_ERR_SAMPLES:
                        _slot["errs"].append(_err)
            _state["skill_calls"] = _skill_calls
        rec = {"step": step_idx, "obs": obs_text, "code": code, "events": events,
               "timeout_ticks": step_timeout_ticks,
               # 本步**结束后**的坐标：算「走了多远」（map coverage，见 _summarize_path）的原料，
               # 也是画俯视轨迹图所需的全部数据。取 next_obs 而不是 obs —— 要的是这段代码把 bot
               # 带到了哪，不是它出发前在哪。/step 异常时 next_obs 退化成 obs，那一段位移=0，
               # 正确（世界确实没动）。
               "pos": (next_obs.get("status") or {}).get("position")}
        trajectory.append(rec)
        day_steps.append(rec)
        if recorder is not None and _t0 is not None:
            # 落一行「JS 步 → 视频时间区间」的索引。失败不影响轨迹（内部已吞异常）。
            recorder.mark_step(step=step_idx, day=day_idx, t0=_t0, t1=recorder.elapsed(),
                               code=code, position=(next_obs.get("status") or {}).get("position"))
        # ★ 世界状态写回 _state：ReAct 分支的 write_code 工具在闭包里读它，赋值给局部 obs
        #   不会被外面看到（Python 闭包语义），那样新分支永远看不到世界更新。
        obs = next_obs
        _state["obs"] = obs
        last_events = events
        # ★ _track 返回本步新增成就名（packet ∪ onSystem 源头并集 diff，见 _track docstring）；
        #   步反馈/trajectory 都用它——与落盘同源。成就记账已全在 _track 内完成，
        #   _record_system 只管死亡（kind==advancement 在那里 continue 跳过）。
        _track_new_advs = _track(obs, sys_events)
        new_deaths, _ = _record_system(sys_events, step_idx,
                                       holdings_before, _holdings(obs),
                                       respawn_pos=(obs.get("status", {}) or {}).get("position"))
        rec["deaths"] = new_deaths
        rec["advancements"] = _track_new_advs  # list[str] 名字（trajectory 文本化直接打名字）
        # ★ **死亡会静默解除观战附身** —— 相机弹回观战者自己的身体（停在死亡现场），此后
        #   bot 重生到别处、整段录像变成空镜，而日志里没有任何异常。所以死后必须重新附身。
        #   位置讲究两点：
        #     ① 在 `_record_system` **之后** —— 它内部的 `_snap("death", ...)` 要拍死亡现场，
        #        重新附身会把相机挪走，顺序反了就永远拍不到"死在哪、被谁杀"的那一帧。
        #     ② 走 `to_thread` —— 重附身要发 /tp + /spectate 两条命令（约 10~15s 墙钟，
        #        全是 xdotool/剪贴板的 sleep），不能阻塞事件循环。
        # ★ **死亡后冻结世界**（2026-09-15，消死亡循环）：
        #   死亡熔断只结束"这一步"，世界仍解冻——僵尸 AI 继续攻击 → "重生即被杀"循环
        #   （dstlow3 alice step25 一次死 6 次即此）。带录像时 recorder.resync_spectate 还要 ~15s，
        #   这窗口更致命。故死亡时先 pause（/tick freeze 冻怪 AI + physicsEnabled=false 停 bot），
        #   再从容重新附身，下步初的 resume 解冻恢复。recorder=None 的对照组同样受益。
        #   ★ JS 侧 __DIED__ 分支已升级为「死亡即 pauseBot + 等孤儿在 waitForTicks 检查点死透 +
        #     等重生完成」后再返回（index.js __DIED__ 分支），故此处 world.pause() 是**幂等重冻**：
        #     世界已在 JS 侧冻住、孤儿已死、observe 已是重生点位置。这里再冻一次是防御 JS 侧
        #     pause 失败的兜底（网络抖动 / pause 抛错），并按 dawn 路径口径收 _record_system（跨步
        #     积压的死亡播报）。dawn 步既死又跨午夜时先走这刀、再到 3802 dawn pause 幂等重冻，无害。
        #   pause 的 obs 记 _record_system；paused 标志不在此置位（那是 dawn 反思路径专属，见 3808）。
        if new_deaths and not _state.get("run_over"):
            try:
                _dobs, _, _dsys, _ = await asyncio.to_thread(world.pause)
                _record_system(_dsys, step_idx,
                               respawn_pos=(_dobs.get("status", {}) or {}).get("position"))
                _absorb_advs(_dobs, _dsys)  # 暂停瞬间晚到的成就播报喂总变量防漏
            except Exception:  # pylint: disable=broad-except
                logger.warning("[minecraft] 死亡后冻结失败，世界可能仍解冻（死亡循环风险）",
                               exc_info=False)
        if new_deaths and recorder is not None:
            try:
                await asyncio.to_thread(recorder.resync_spectate)
            except Exception:  # pylint: disable=broad-except
                logger.debug("[minecraft] 死亡后重新附身失败（已忽略）", exc_info=False)

        # 本步进度：只报 tier / 物品数（「干成没干成」看下面 [minecraft反馈] 的原文，
        # 摘要行按优先级只取一种又截断，和完整反馈并存反而是噪音）。
        # tier 直接读 tier_progress[-1]（_track 刚为本步算好的），不重算 —— 重算要记得带
        # equipped 才不漏判副手（见 tech_tier），少传一次就和打分口径不一致了。
        # ★ tier 读 _seen_advancements（env 闭包态，跨 ② 重连存活），非 packet 进程态（2026-09-07
        #   修自愈重连 tier 归零回归）：root=[Minecraft] 进计数，不再从 0 跳 2。重连后 packet 清零
        #   不影响 tier（_seen_advancements 只增不减），但 packet 通道本身仍值得留痕——
        # storyTreeSeen=false 时 packet 里没 story 树（懒推送，bot 没碰 story 进度，或 ② 重连后未补），
        # 给个标记区分「packet 通道此刻空」vs「tier 真为 0」（tier 由 _seen_advancements 算，与此标记独立）。
        _pkt_seen = _extract_advancement_packet(
            obs.get("advancementPacket") if obs else None)["storyTreeSeen"]
        logger.info("[minecraft动作] step%d 完成 | tier=%s 物品%d种%s",
                    step_idx, tier_progress[-1], len(items_seen),
                    " (无story包)" if not _pkt_seen else "")
        # ★ **物品无声消失诊断**：逐步 diff「库存+装备」总持有量，把**减少的**项单独打一行。
        #   动机是原语执行结果与库存变化有时会不一致（如播报"放置失败"却仍然少了工具），事后
        #   单步复现往往因为缺了触发时刻的上下文残留（超时步的孤儿代码、pathfinder abort，见
        #   MinecraftWorld.pause 的 docstring 里那条「JS 无 promise 取消 → 超时步的孤儿代码仍在
        #   后台操纵 bot」）而无法重现——靠事后读日志猜不出来，所以把证据采集**前移到发生的
        #   那一刻**。
        #
        #   ⚠️ **分两级、不要一律报警**：几乎每步都在放方块/合成/丢垃圾，
        #   "持有量减少"本身极其普通 —— 拿它当警报，一轮会刷十几条 WARNING，真正的异常
        #   反而被埋掉（同「no_mobs_nearby 播报噪音」那类教训）。故按**可疑度**分流：
        #     · 整叠归零且是**工具/装备类**（工具丢了才影响 tier，也最不该无声消失）→ WARNING
        #     · 其余减少（材料被消耗、丢垃圾）→ DEBUG。run.py 把 root 级别钉在 INFO
        #       （见 _setup_logging），所以这一档**平时完全不出现**；要看就临时把那里改成 DEBUG
        #   判据里带上本步是否**声称成功**：播报里出现「没放下/失败/0 块」却仍然少东西，
        #   才是真正的无声丢失。这个判断留给读日志的人，诊断只负责把两边摆在一起。
        _prev_hold, _cur_hold = _prev_holdings.get("v"), _last_holdings.get("cur", {})
        if _prev_hold is not None:
            _lost = {k: (v, _cur_hold.get(k, 0)) for k, v in _prev_hold.items()
                     if _cur_hold.get(k, 0) < v}
            if _lost:
                _detail = "，".join(f"{k} {a}→{b}" for k, (a, b) in sorted(_lost.items()))
                # 整叠归零的工具/装备 —— tier 靠它们算，无声消失的代价最大
                _gone_tools = sorted(
                    k for k, (_a, b) in _lost.items()
                    if b == 0 and _TOOL_SUFFIXES.intersection({k.rsplit("_", 1)[-1]})
                )
                _tier_note = ""
                if len(tier_progress) >= 2 and tier_progress[-1] < tier_progress[-2]:
                    _tier_note = (f"；进度深度掉级 {tier_progress[-2]} → {tier_progress[-1]}"
                                  f"（打分用 peak 高水位不受影响，但 bot 实际能力被削）")
                if _gone_tools or _tier_note:
                    # ★ **三路分流**（2026-08-23 改，keep_inventory 下文案与事实对齐）：
                    #   ① new_deaths 且 keep_inventory=false：死亡掉物 = 正常规则 → INFO。
                    #   ② new_deaths 且 keep_inventory=true：死了却少工具 = **真异常**（keep_inventory
                    #      没生效或物品凭空消失）→ WARNING，文案明确写"本步死亡且 keep_inventory=true
                    #      下仍消失"。此前这路被混进 else（因 `not _KEEP_INVENTORY` 为 False 走 else），
                    #      而 else 文案写的是"本步未死亡"——step280 实测 bot 死了第25次、却报"本步未
                    #      死亡"，agent 看到"死了+少装备+诊断说没死"困惑之下归因为"死亡掉落全部物品"
                    #      记进经验层 death_drops_all_items（与 keep_inventory=true 的事实矛盾）。
                    #   ③ 未死：现有 tossStack/挖矿耐久/无声丢失分流（不变）。
                    # 分类逻辑抽到 `_classify_tool_disappearance`（纯函数，便于单测），文案在这里拼。
                    _loss_cls = _classify_tool_disappearance(new_deaths, _KEEP_INVENTORY, code, events)
                    if _loss_cls == _TOOL_LOSS_DEATH_RULE:
                        # 一步内可能死多次（跨步积压，见下方死亡序号那段）：报区间而非单个序号，
                        # 否则「第 59 次死」会让人以为这批掉落只对应一次死亡。
                        _n_new = len(new_deaths)
                        _last_ord = len(deaths)
                        _ord_note = (f"第{_last_ord}次死" if _n_new == 1 else
                                     f"第{_last_ord - _n_new + 1}~{_last_ord}次死，本步共{_n_new}次")
                        logger.info(
                            "[minecraft诊断] step%d 工具/装备随**死亡**掉落（%s，属正常规则）"
                            "：%s%s",
                            step_idx, _ord_note, _detail, _tier_note)
                    elif _loss_cls == _TOOL_LOSS_DEATH_KEEPINV_ANOMALY:
                        # keep_inventory=true 下死亡不该掉任何物 —— 死了却少工具就是真异常。
                        _n_new = len(new_deaths)
                        _last_ord = len(deaths)
                        _ord_note = (f"第{_last_ord}次死" if _n_new == 1 else
                                     f"第{_last_ord - _n_new + 1}~{_last_ord}次死，本步共{_n_new}次")
                        logger.warning(
                            "[minecraft诊断] step%d **本步死亡（%s）且 keep_inventory=true 下工具/装备"
                            "仍消失**：%s%s —— keep_inventory 下死亡不该掉物，这是真异常（查 keep_inventory"
                            "是否及时生效、超时步孤儿代码、掉落物未拾取、耐久耗尽断裂）。完整变化：%s",
                            step_idx, _ord_note, "、".join(_gone_tools) or "(无整叠归零的工具)",
                            _tier_note, _detail)
                    elif _loss_cls == _TOOL_LOSS_TOSSED:
                        # ★ 主动丢弃（tossStack）与耐久断裂是「可解释的归零」降级 INFO，只把无法
                        #   解释的留 WARNING（2026-08-19 修，避免真异常被噪音淹没，见原注释）。
                        logger.info(
                            "[minecraft诊断] step%d 工具/装备**主动丢弃**（bot tossStack，"
                            "非丢失）：%s%s。完整变化：%s",
                            step_idx, "、".join(_gone_tools) or "(无整叠归零的工具)",
                            _tier_note, _detail)
                    elif _loss_cls == _TOOL_LOSS_DUG_BREAK:
                        logger.info(
                            "[minecraft诊断] step%d 工具/装备**耐久耗尽断裂**（本步在挖矿，"
                            "非丢失）：%s%s。完整变化：%s",
                            step_idx, "、".join(_gone_tools) or "(无整叠归零的工具)",
                            _tier_note, _detail)
                    else:  # _TOOL_LOSS_SILENT
                        # 真正的"无声丢失"应是「本步声称失败/没挖、工具却整叠没了」—— 那才留 WARNING。
                        logger.warning(
                            "[minecraft诊断] step%d **工具/装备消失且本步未死亡**：%s%s —— 若本步"
                            "播报说「没放下 / 失败 / 0 块」却仍然少了东西，那就是无声丢失"
                            "（查超时步的孤儿代码、掉落物未拾取、耐久耗尽断裂）。完整变化：%s",
                            step_idx, "、".join(_gone_tools) or "(无整叠归零的工具)",
                            _tier_note, _detail)
                else:
                    logger.debug("[minecraft诊断] step%d 持有量减少：%s", step_idx, _detail)
        _prev_holdings["v"] = dict(_cur_hold)
        # 完整反馈（不截断）：三类原文分块原样打印，尤其 onLog（LLM 自己的 console.log，
        # 可能几十行）和 onError 的完整 stack。与 [minecraft代码] 一样多行原样打印，便于直接读。
        if events:
            logger.info("[minecraft反馈] step%d 完整反馈（%d 条）：\n%s", step_idx, len(events),
                        _format_events_full(events))
        else:
            logger.info("[minecraft反馈] step%d （无反馈）", step_idx)

        # 死亡/成就的**给 LLM 的反馈**（记账与日志已在 `_record_system` 里做完）：
        # ① 跨步不漏：Node 侧的死亡熔断只在 /step 执行期间轮询 health，bot 在两步之间（LLM 思考、
        #    /tick freeze 反思期间）被打死会静默丢失；服务器播报由 onSystem 累积，漏不掉。
        # ② 带死因/凶手：可按 cause 聚合（zombie×3、creeper×1），喂给反思远比「死了 N 次」可教。
        # 序号口径见 `_record_system` 的 docstring（逐条算，不在循环里读 len(deaths)）。
        # 成就反馈直接用 _track_new_advs（packet ∪ onSystem 已在 _track 源头并集，无需差集）。
        last_events = list(last_events) + _system_feedback(
            new_deaths, _track_new_advs, holdings_before, _holdings(obs))

        # 日界检测：worldDay 滚动（= 真正穿过 24000/0）即一天结束、进栅栏反思。
        # （在日界反思：跨过午夜是天然的「一天」分界，且暂停让反思期间 bot 不挨打、不乱动、SQLite 无并发。）
        now_night = is_night(obs)
        # ★ 日界口径（2026-09-10 纠正）：日界 = 24000/0（worldDay 滚动），**不是** phase 的夜→昼跳变。
        #   旧判据看「夜→昼跳变」（历史上 sunrise 阈值在 22000/23000 间反复，2026-09-16 统一对齐官方
        #   四阶段：sunrise 段 = [22200,24000)）。无论 sunrise 起点定哪，phase 跳变都只是「首次天亮」、
        #   不是「下一天临界点」—— sunrise 段(22200→24000)这段清晨本属于当天，旧判据把它错算进
        #   新一天、反思早了。现在 bot 夜结束后可继续 ReAct，直到 worldDay 真正滚动（24000→0）才进
        #   反思。worldDay = floor(time/24000)（服务器权威累计天数，status.js 暴露），它的滚动点恰好
        #   就是真正的 24000/0。
        cur_day = (obs.get("status", {}) or {}).get("worldDay")
        # 时钟停摆护栏：步数上限去掉后，**唯一的出口是天亮**，于是「世界时间根本不流动」会从
        # 原先的"轨迹变短"升级成"永不退出的 LLM 死循环"。这不是假想故障：`advance_time`
        # 可能因 gamerule 改名等原因静默失败，/tick freeze 也持久化在存档里。
        #
        # ★ 判据用 `timeTicks`（服务器权威的 `bot.time.timeOfDay`，0-23999）**而不是**
        #   `elapsedTime`。后者是 `bot.globalTickCounter`，两个性质让它不适合当停摆判据：
        #     ① **它会被自愈重置**：`/start` 里 `bot.globalTickCounter = 0`（index.js:106），
        #        而 `onTick` 监听器是 /step 期间注册、步末 removeListener 摘掉的 —— ReadTimeout
        #        触发 `_heal_and_restart()` 重连后，它就停在 0 再也不涨。
        #     ② 于是 `_tick == last_tick` 分不清「世界时钟真停了」和「计数器刚被自己的自愈归零」。
        #   2026-08-13 那轮 alice 正是这样：day26 正常结束 → 10:47/10:52 两次 ReadTimeout 自愈
        #   → elapsedTime 恒为 0 → 护栏把**自愈的副作用**误判成时间停摆，day27 中止，白扔 3 天
        #   轨迹（还缺席了 window6 聚合）。更糟的是 Agent 采信了错误的报错文案，把「advance_time
        #   gamerule 彻底失效」当成经验写进图谱，差点污染公共脑。
        #   `timeTicks` 由服务器每刻推进、不随 bot 重连复位，真冻结（/tick freeze、advance_time
        #   失效）时它才不动 —— 这才是「世界时间是否流动」的正确判据。
        #
        # ★ 时间栅栏下的语义变化（2026-09-01，fence-aware）：栅栏开时世界本就大部分时间冻着（思考期
        #   间），只有 world.step 执行期间推进 tick。故连续两步 post-obs 的 timeTicks 差 = 本步
        #   world.step 消耗的 tick。一个消耗 0 tick 的步（no-op / 纯观察 / world.step 抛异常未动世界）
        #   现在看起来和真 stall 一模一样——必须区分：
        #     ① world.step 抛异常的步（_step_ran=False）：世界没动是预期，**不计 stall**，把 last_tick
        #        刷新成当前值（让后续成功步比较基准正确），喂错误反馈让 agent 自救。
        #     ② world.step 跑成功但消耗 0 tick 的步（_step_ran=True, _tick==last_tick）：这才是真
        #        "agent 代码不推进时钟"，计 stall，连达 STALL_ABORT_STEPS 仍中止（兜底真正卡死的 agent）。
        #   护栏语义从「世界时钟停了」改为「agent 代码连续多步不推进时钟」。栅栏关时 _step_ran 恒 True
        #   （世界空转期间也在走，0-tick 步罕见），行为与旧版一致。
        _tick = (obs.get("status", {}) or {}).get("timeTicks")
        if _tick is None:  # 老 payload 没有 timeTicks：退化用 elapsedTime，比完全不设护栏好
            _tick = (obs.get("status", {}) or {}).get("elapsedTime")
        if _tick is not None and _tick == last_tick:
            if _step_ran:
                # world.step 跑成功但没推进 tick —— 真 stall（agent 代码不推进时钟）。
                stalled_steps += 1
                if stalled_steps >= STALL_ABORT_STEPS:
                    # ★ 1009 改抛 WorldStallError（ds13 两次 NOTOK 事故）：裸 RuntimeError 不在
                    #   registry 的 _ABORT_EXCEPTION_NAMES 放行名单里，被工具层 broad-except 吞成
                    #   错误反馈喂回 LLM ⇒ 护栏每步重抛每步被吞（ds13 两段各 ~1500 次、单臂空转
                    #   6 小时）。放行名单按名字匹配，沿 rollout → loop.py abort 整 run 留人工。
                    raise _world_stall_error_cls()(
                        f"世界时间停摆：连续 {stalled_steps} 步 timeTicks 恒为 {_tick}，"
                        f"已过 {n_nights}/{max_nights} 夜。多半是 `advance_time` gamerule 静默失败或世界"
                        "仍处 /tick freeze（步数上限已移除，跨过午夜(24000/0)是唯一出口，不中止就会无限跑下去）。"
                        "ds13 教训：也可能是 bot 半死断线（桥侧 timeTicks 恒读缓存、/step 仍 200）——"
                        "查 mineflayer.log 的 EPIPE/报错风暴与 MC server 日志的 Timed out 掉线。"
                    )
            else:
                # world.step 抛异常 → 世界没动是预期，不计 stall，刷新比较基准。
                last_tick = _tick
        else:
            stalled_steps = 0
            last_tick = _tick
        # 日界判据（2026-09-10 纠正，全部 24000/0 口径，不出现 22000 phase 判据）：
        #   主判据：cur_day > last_day —— worldDay 滚动 = 真正穿过 24000/0。熬夜（timeOfDay 自然推进
        #     过 24000）与 sleep 跳夜（服务器把累计 time 推进到下一个 24000 倍数，实测 14005→24010）
        #     都让 worldDay +1，都走这条。
        #   兜底判据：worldDay 缺失（异常/老 payload，status.js 0907 起稳定暴露 worldDay，缺失极罕见）
        #     时用 timeTicks 回绕 —— timeTicks 是 0-23999 的回绕时刻，从接近 24000 跳回接近 0 即穿过
        #     24000/0，与 worldDay 滚动同义（sleep 跳夜 ~14000→~10 也命中）。这保证兜底也是 24000/0
        #     口径，不退化到 22000。`_tick` 已在上方时钟停摆护栏取好（timeTicks 优先、退化 elapsedTime）。
        #   ★ 旧「was_night and not now_night」(夜→昼，22000) 不再作日界判据 —— 那是首次天亮不是日界。
        #     was_night/now_night 仍保留：驱动下方入夜检索标记（night_just_fell，进 night 段=13800 时置位）。
        _day_rolled = cur_day is not None and last_day is not None and cur_day > last_day
        # worldDay 缺失才走 timeTicks 回绕兜底（同为 24000/0 口径）。
        _ticks_wrapped = (
            (cur_day is None or last_day is None)
            and _tick is not None and last_tick is not None
            and _tick < last_tick  # timeTicks 从接近 24000 回绕到接近 0
        )
        if _day_rolled or _ticks_wrapped:
            if _ticks_wrapped and not _day_rolled:
                logger.warning("[minecraft] worldDay 缺失，用 timeTicks 回绕判日界（%d→%d）—— "
                               "status.js 可能未返回 worldDay，请排查", last_tick, _tick)
            n_nights += 1
            reflection = ""
            # ① **先暂停，再算 discoveries**（顺序很关键）：暂停这一下可能带回死亡播报（bot
            # 恰在天亮前后被打死）。若先算好 discoveries 再冻结，那次死亡会进全局 deaths
            # （总数不丢），却不在 deaths_today/deaths_summary 里 —— **时间上离反思最近、最该
            # 被复盘的那次死亡恰好错过反思**。暂停在前，它才能进当夜 discoveries。
            #
            # world.pause() 是**真暂停**（两侧）：服务器 /tick freeze + 客户端 physicsEnabled=false
            # 并掐断寻路/挖掘/控制输入。只发 /tick freeze 拦不住 bot 自己 —— MC 明确豁免玩家，
            # 且超时步留下的「孤儿代码」仍在后台操纵 bot（详见 MinecraftWorld.pause 的 docstring）。
            paused = False
            if night_cb is not None:
                try:
                    # pause() 返回同 step 形四元组（含 fresh obs）—— 捕获它做复活点留痕，
                    # 不能用闭包 obs（是暂停前的过期位置）。跨步死亡（反思期被打死）就在这批 _sys 里。
                    _pause_obs, _, _sys, _ = await asyncio.to_thread(world.pause)
                    _record_system(_sys, step_idx,
                                   respawn_pos=(_pause_obs.get("status", {}) or {}).get("position"))
                    # ★ 日界 pause 时刻晚到的 onSystem 成就播报喂总变量（反思期晚到的由黎明
                    #   resume 的 _track(obs, _sys) 收）——防栅栏边界漏。
                    _absorb_advs(_pause_obs, _sys)
                    paused = True
                except Exception:  # pylint: disable=broad-except
                    logger.warning("[minecraft] 暂停失败，反思期间 bot 可能受威胁或继续乱动")
            # ①' **世界一冻结就收掉当天的录像段**（反思之前）：反思是几分钟的 LLM 墙钟，而
            #    世界此刻是冻结的 —— 那几分钟录下来是一动不动的静止画面，白占体积、还让
            #    「这一天的录像」长度与游戏内容不成比例。
            #    ★ `end_day` **有意不推进天号**：紧接着的反思要靠 recorder 的当前天号给
            #      facet 归属（见 footage.mark_facet_source），必须还是刚过去那一天。
            if recorder is not None:
                try:
                    await asyncio.to_thread(recorder.end_day)
                except Exception:  # pylint: disable=broad-except
                    logger.debug("[minecraft] 收录像段失败（已忽略）", exc_info=False)
            # ② 汇总当日发现（此时已含 freeze 步到达的死亡/成就）
            day_new_items = sorted(set(items_seen) - day_items_before)
            # ★ 读 _seen_advancements（env 闭包态）而非 tier_progress[-1]：后者取末值，自愈 ②
            #   重启进程后会骤降（packet 清零），daily tier 不该跟着采集断层掉。_seen_advancements
            #   跨重连存活、只增不减，base_depth 取它才是当日真实能力（与 _track/final_tier 同口径）。
            cur_tier = base_depth(_seen_advancements)
            day_deaths = [d for d in deaths if d["day_idx"] == day_idx]
            day_advs = [a for a in advancements if a["day_idx"] == day_idx]
            # ★ **每日生存/装备/建造快照**（2026-08-22，对应用户「每日量化指标」需求）：
            #   此刻 `_state["obs"]` 是当天最后一步的观察（pause 之后未更新，resume 才更新），
            #   其 status 含黎明（熬过夜后）的 health/food/equipment —— 正是「这一天结束时 bot 的
            #   生存状态」。这些信号此前完全不落盘（night_reflections 只带上面那几个 discoveries key，
            #   跑完即丢），新增字段后随 discoveries 进 night_reflections → ep → Sample.metrics.daily_log。
            _dawn_status = (_state["obs"].get("status") or {}) if _state.get("obs") else {}
            _dawn_eq = _dawn_status.get("equipment") or {}
            # 建造物：当日新造的种类 + 累计造过的种类。来源是 built_items（从 code 解析的 craftItem/
            # placeItem 物品名，见 _extract_built_from_code），**不是 items_seen** —— items_seen 在步末
            # 采样库存，「合成后同一步放置」会让建造物从库存消失而漏采（smoke 实测 alice 造了工作台
            # 却报空）。built_items 抓的是 Agent 的建造意图，不受库存瞬态影响。再 ∩ _is_buildable
            # 过滤掉非建造物（craftItem 也合成 wooden_pickaxe 等工具）。
            _bld_so_far = sorted(n for n in built_items if _is_buildable(n))
            _bld_today = sorted(n for n in (built_items - day_built_before) if _is_buildable(n))
            discoveries = {
                "new_items": day_new_items,
                "n_new_items": len(day_new_items),
                "tier": cur_tier,
                # ★ 工具材质 4 级（2026-09-09 加，与 tier 9级depth 并列落盘）：tier 是进度树深度
                #   （单调），tool_tier 是「已造出的最高级成品工具」wooden/stone/iron/diamond/none
                #   （非单调、工具断裂回退）。两者口径不同，画图各画各的——旧图
                #   plot_combined_ts_0908/0909.py 的 tier rank 口径在新 run 里靠本字段还原。与观察里渲染的「科技」行同源同口径
                #   （都调 tool_tier_name，按库存+装备的工具材质判定，不计护甲/金工具）。
                "tool_tier": tool_tier_name(
                    (_state.get("obs") or {}).get("inventory") or {},
                    equipped_item_names(_dawn_status),
                ),
                "n_steps": len(day_steps),
                "n_deaths_so_far": len(deaths),
                # 当日死亡按死因聚合 + 成就里程碑：喂给夜反思，让「怎么死的」能沉淀成 pitfall
                "deaths_today": day_deaths,
                "deaths_summary": _summarize_deaths(day_deaths),
                "advancements_today": [a.get("advancement") for a in day_advs],
                # 累计成就全量清单（直接落盘留痕，不必事后从 advancements_today 并集重建）。
                # advancements 是 [{advancement, day_idx, ...}, ...] 累积列表，去重取名。
                "advancements_so_far": sorted({a.get("advancement") for a in advancements
                                               if a.get("advancement")}),
                # ── 每日量化指标（生存/探索/建造，按天时序）──────────────────────
                # 生存：黎明时的生命/饥饿（熬过夜后的残值，趋势↑=生存能力↑）。
                "health": _dawn_status.get("health"),
                "food": _dawn_status.get("food"),
                "n_deaths_today": len(day_deaths),
                # 装备：四槽+主副手的原始物品名（用户定：记原始物品名，事后自由聚合材质层级）。
                # status.equipment 形如 {head:{name,durability}, torso:null, ...}，原样带出。
                "equipment": _dawn_eq,
                # 探索（累计，看趋势↑）：独特物品总数 + 当日新增 + **累计全量清单**。
                # unique_items_so_far 只是个数；items_so_far 是累计造过/捡过的物品名全集，
                # 直接落盘留痕（不必事后从 new_items 并集重建）。与 buildings_so_far 同口径。
                "unique_items_so_far": len(items_seen),
                "items_so_far": sorted(items_seen),
                # 建造：当日新造种类 + 累计造过种类（趋势↑=建造能力↑）。庇护所不计（空间结构，见注释）。
                "buildings_today": _bld_today,
                "buildings_so_far": _bld_so_far,
                # ── MC 累计 XP（score 数据源，2026-08-25）──────────────────────────
                # xp_total = 跨命累计总 XP（死亡不扣减，单调↑）；xp_today = 当日新增。
                # xp_level = 黎明时的 XP 等级（MC 原生口径，死后会被压低，仅作旁证）。
                # 与 unique_items_so_far 同为累计量，按天看趋势。
                "xp_total": _xp_total,
                "xp_today": _xp_total - _xp_at_day_start,
                "xp_level": (_dawn_status.get("experience") or {}).get("level"),
                # ── 技能调用计数（见 plans/skill_aggregation_maintenance.md）──────────
                # 当天注入技能被调用的成功/失败次数（{slug:{ok,fail}}），由 _run_step_body 解析
                # onLog 埋点累加。night_cb 反思时回填 skill 表，聚合据此降级高失败率技能。
                "skill_calls": dict(_state.get("skill_calls") or {}),
                # 技能来源映射：按注入来源把计数分流回副脑/主脑（见 __init__.py:_apply_skill_usage_counts）
                "skill_sources": dict(_state.get("skill_sources") or {}),
                # ── MC 记分板权威值（交叉校验旁证，2026-08-26）──────────────────────
                # 与上面的自统计值并排留存：deathCount↔n_deaths_so_far、health/food↔health/food、
                # xp↔xp_total（★口径不同见 _extract_scoreboard：记分板 xp 是当前持有会随重生压低，
                #   xp_total 是跨命累计不扣减，死过必分叉，这是预期非 bug）。
                # totalKillCount/armor 是记分板独有、env 现无自统计的新信号。
                # ⚠️ 只落盘双值、不比对、不修正（用户定）：差异分析事后人工/脚本做。
                # 缺记分板（spawn 早期/续跑首步 score 包未到）时全 None，不污染。
                "scoreboard": _extract_scoreboard(
                    _state["obs"].get("scoreboard") if _state.get("obs") else None,
                    _dawn_status.get("name"),
                ),
                # ── 进度协议包佐证支路（2026-09-07，root 等静默进度采集）──────────────
                # 与 advancements_so_far（onSystem 被动播报，announce_to_chat=true 的进度）
                # 并排留存双值：advancement_packet 是 packet_advancements 协议包维护的
                # "已完成进度"状态快照，能采到 announce_to_chat=false 的静默进度（如
                # story/root=[Minecraft]，onSystem 永远采不到它，致 tier 从 0 跳到 2）。
                # ⚠️ 口径差（预期非 bug，见 _extract_advancement_packet）：懒推送——story 树
                #   （含 root）要等 bot 达成至少一个 story criterion 才推送；bot 整天没碰
                #   story 进度则协议包里没 root（看 storyTreeSeen 标志区分「没收到包」vs
                #   「收到了没达成」）。只落盘双值、不比对、不修正（同 scoreboard 口径）。
                "advancement_packet": _extract_advancement_packet(
                    _state["obs"].get("advancementPacket") if _state.get("obs") else None,
                ),
            }
            if night_cb is not None:
                # ③ 反思（安全，可从容做完写副脑图谱 + 生成次日指导）
                day_text = _day_trajectory_text(day_idx, day_steps)
                try:
                    # ★ day_steps 透传给 night_cb：rollout 侧用它 + agent.history 重建
                    #   「思考链+关键信号观察」版 trajectory_text（_build_reflection_trajectory），
                    #   替代这里的 day_text（完整观察版，网格视野占大头淹没反思）。day_text 仍生成，
                    #   作 fallback / 日志留痕用。day_steps 此刻是当天完整步（清空在 night_cb 之后）。
                    reflection = await night_cb(day_text, day_idx, discoveries, day_steps)
                except asyncio.CancelledError:
                    # graph.py 的 asyncio.shield 会让反思内部跑完后把 CancelledError 重新抛出
                    # —— 这里接住并记录，不让它继续向上污染 gather，然后用空反思文本继续本夜流程。
                    logger.warning("[minecraft] 第%d天 night_cb 被外部取消（CancelledError），"
                                   "反思可能未完整写入", day_idx)
                    reflection = ""
                except WindowAggregationError:
                    # ★ 聚合失败 = abort 整个 batch（栅栏3 的 on_window.collect 抛出，2026-08-29）。
                    #   此前被下面的 `except Exception` 吞成 reflection=""，导致「聚合失败 abort batch」
                    #   语义实际未生效（注释声称 raise→abort，实现被吞）。现在显式放行，让它穿透到
                    #   loop.py 的 rollout except（按类型放行）→ run_arm_batch → brain.py gather →
                    #   abort 整个 batch。各副脑经验层未清（清层在聚合成功之后），数据完整可复盘。
                    raise
                except Exception:  # pylint: disable=broad-except
                    logger.exception("[minecraft] 第%d天 night_cb 失败，跳过本次反思", day_idx)
                    reflection = ""
                # ★ 逐夜 discoveries 无条件落盘（2026-09-08 解除反思门控）：
                #   discoveries 每夜都算出来了（见上方 env.py:3671），不该被「反思是否有文本」
                #   门控。此前 append 在 `if reflection:` 里，对照组（no_graph）在 night_cb
                #   末尾强制 reflection=""（取消夜反思是实验设计变量）→ 一条都不 append →
                #   逐夜 tier/deaths/xp/equipment/成就/记分板全丢，只剩 checkpoint 的累积末值。
                #   现在无条件 append，reflection 空就存空串。实验组行为不变（reflection 有文本，
                #   照旧带出来）。dawn_reflection 注入下方仍保持 if reflection: 门控——那是
                #   给 bot 看次日目标，与「记录逐夜指标」是两件事，不该绑在一起。
                night_reflections.append(
                    {"day_idx": day_idx, "discoveries": discoveries, "reflection": reflection}
                )
                if reflection:
                    # ★ 用专属 key "dawn_reflection"（2026-08-29）：不再用 "onChat" + 【第N天黎明复盘】
                    #   前缀。旧版前缀是注入/取回的配对锚点（取回靠 "黎明复盘" in v 匹配），但前缀会
                    #   原样泄露进 bot 看到的天亮步 tool result（[onChat] 【第N天黎明复盘】...）和次日
                    #   day_start 的【昨日复盘与今日目标】块（day_goal 取回又带一遍）。现在改用专属 key：
                    #   注入存裸文本（无标签），取回按 k=="dawn_reflection" 精确匹配（见 __init__.py react_day）。
                    #   bot 在天亮步反馈里看到的是 [dawn_reflection] <复盘+目标正文>，次日 day_start 看到的是
                    #   干净的【昨日复盘与今日目标】<复盘+目标正文>——不再有「黎明复盘」旧标签。
                    last_events = list(last_events) + [
                        ("dawn_reflection", reflection)
                    ]
                # ★ 反思已读走当天 skill_calls（回填 skill 表）→ 清零，下一天重新计。
                #   skill_sources / extra_programs 同步清零（次日 createplan 时 _inject_all_skills
                #   重新全量注入代码 + 重建来源；mem/skill 签名块已拼进 toolresult、不再进 state）。
                _state["skill_calls"] = {}
                _state["skill_sources"] = {}
                _state["extra_programs"] = ""
            logger.info(
                "[minecraft] 第%d天黎明（step%d，过%d夜死%d次）：当日新物品%d种 depth=%s%s%s，%s",
                day_idx, step_idx, n_nights, len(deaths), len(day_new_items), cur_tier,
                f"，当日死亡：{discoveries['deaths_summary']}" if discoveries["deaths_summary"] else "",
                f"，当日成就：{', '.join(discoveries['advancements_today'])}" if discoveries["advancements_today"] else "",
                "已反思" if night_cb else "无反思",
            )
            # ④ **先进入下一天，再恢复**（同上，顺序关键）：恢复这一下到达的死亡属于新的一天 ——
            # 当天的反思此刻已经跑完，若仍记成当天，那次死亡不会进任何一次反思。递增 day_idx
            # 后再恢复，它就会被下一个黎明的 deaths_today 收走。
            day_idx += 1
            _snap_state["day"] = day_idx   # 抓帧的天号跟着走（见 _snap）
            # ★ **开录新一天那一段**（当天的段已在反思之前由 `end_day` 收掉）。
            #   ① 在 `resume` **之前**：起 ffmpeg 要约 1.5s，冻结时游戏时间停着，这段空白
            #      不对应任何游戏内容；放到解冻之后就会真丢掉一秒多的动作。
            #   ② 在 `day_idx` 递增**之后**：段的文件名与索引里的天号都取它。
            #   ③ **最后一夜不开**：`run_over` 时紧接着就 stop()，开了只会多留一个 0 字节的
            #      day{N+1}.mp4，让前端多出一个点开是空的入口。
            if recorder is not None and n_nights < max_nights:
                try:
                    await asyncio.to_thread(recorder.begin_day, day=day_idx)
                except Exception:  # pylint: disable=broad-except
                    # begin_day 内部已吞异常并自我禁用；这里再兜一层，确保不打断轨迹
                    logger.debug("[minecraft] 开录新一天失败（已忽略）", exc_info=False)
                # ★ 天边界**兜一道重新附身**：主循环那处只在「本步检测到死亡」时触发，而 bot 也会
                #   死在两步之间（LLM 思考那几秒、反思期间）—— 那种死亡由 onSystem 跨步累积上报，
                #   可能落在别的分支里。这里无条件重附一次，代价是几秒（世界正冻结，不花游戏时间），
                #   换的是"任何漏掉的脱附最多只污染一天"。已附身时重发也是幂等的。
                try:
                    await asyncio.to_thread(recorder.resync_spectate)
                except Exception:  # pylint: disable=broad-except
                    logger.debug("[minecraft] 天边界重新附身失败（已忽略）", exc_info=False)
            day_steps = []
            day_items_before = set(items_seen)
            day_built_before = set(built_items)  # 与 day_items_before 同模式：次日「当日新建造」的基线
            _xp_at_day_start = _xp_total  # 与 day_items_before 同模式：次日「当日新增 XP」的基线
            # ★ **「黎明清空当天对话」不需要显式动作**：每天新建一个 ReActAgent，history 不跨天，
            # 昨天只通过「私有记忆图谱 + 黎明复盘文本」带过来——这正是「把一天压缩成几句带进
            # 第二天」的信息瓶颈该有的样子。
            if paused:
                # ⑤ 恢复：物理 + 服务器 tick 一起放开，进入下一天。**必须在 break 之前** ——
                # /tick freeze 持久化在世界存档里，带着冻结状态退出会让下一轮探索的世界时间是停的
                # （physicsEnabled 是进程内状态，随 bot 重建自然复位，但 freeze 不是）。
                # 用 paused 而非 night_cb 判断：暂停失败时不该再发一次恢复。
                try:
                    obs, _, _sys, _ = await asyncio.to_thread(world.resume)
                    _state["obs"] = obs
                    _record_system(_sys, step_idx,
                                   respawn_pos=(obs.get("status", {}) or {}).get("position"))
                    # _track 在此调是为采 tier/biome/XP 并算 tier 突破帧（freeze 期间世界冻结，
                    # resume 解冻瞬间无新 packet 成就）。返回的 _new_advs 有意丢弃——此处是黎明
                    # 反思边界、非「一步」，无步反馈通道；即便非空，记账（advancements.append）已在
                    # _track 内完成，不丢记账。与主循环 _track_new_advs 喂 _system_feedback 的口径
                    # 差异在此。★ _sys 照传：反思/冻结期间晚到的 onSystem 成就播报在这里被收走
                    # （packet 快照不含它们），源头并集靠这一处兜住，否则跨步积压的成就漏。
                    _track(obs, _sys)
                    now_night = is_night(obs)
                    # 日界判据基准刷新：resume 后 obs 已换，worldDay 也跟着重读（与 now_night 同步），
                    # 下方 last_day = cur_day 会用新一天的 worldDay 作基准，下一步不再误判日界。
                    cur_day = (obs.get("status", {}) or {}).get("worldDay")
                except Exception:  # pylint: disable=broad-except
                    logger.warning("[minecraft] 恢复失败，世界可能仍处于冻结状态")
            # ★ 透明分段存档点：当夜反思已完成、世界已 unfreeze、未进下一天——存档时机。
            # checkpoint_cb 由上层（domain.rollout）注入：save_world（per-arm 世界存档）+
            # 写进度文件（已到第 N 夜）。
            #
            # ★★ 不等聚合（关键）：题内聚合是后台跨 arm 异步的（WindowAggregator.submit 攒齐
            # N 个 arm 才发车，run_exploration 不等它，brain 层 batch 末 windows.drain() 才 await）。
            # 但这不妨碍 checkpoint——主脑 brain.db 是持久化文件，聚合结果直接写盘，不需要
            # checkpoint 保护。checkpoint 只保 per-arm 世界状态（位置/物品/地形）+ 进度记录，
            # 这些在反思后 unfreeze 后就齐了。续跑时同一 run_dir 的 brain.db 自然带上已落地的
            # 聚合结果；未落地的窗口聚合会在续跑段 batch 末 drain 时补（经验层快照已交）。
            # 故"checkpoint 在聚合后"的语义由 brain.db 持久化保证，不需要 run_exploration 等 drain。
            #
            # 必须在 world.resume() 之后：freeze 状态持久化进存档，冻着存档会让续跑读档后世界冻死。
            if checkpoint_cb is not None and not _state["run_over"]:
                # ★ 续跑缺陷①：把指标累积状态随 checkpoint 持久化，供续跑恢复（见 run_exploration
                #   开头 resume_metrics 的恢复）。每夜覆盖写——续跑从最近夜恢复即可。
                _snap = {
                    "step_idx": _state["step_idx"],
                    "items_seen": sorted(items_seen),
                    "biomes_seen": sorted(biomes_seen),
                    "tier_progress": list(tier_progress),
                    "tier_progress_steps": list(tier_progress_steps),
                    "deaths": list(deaths),
                    "advancements": list(advancements),
                    "night_reflections": list(night_reflections),
                    # built_items 随 checkpoint 持久化，续跑段恢复后接着累积（见 built_items 声明处）。
                    "built_items": sorted(built_items),
                    # XP 累计状态随 checkpoint 持久化（score 数据源，2026-08-25）：续跑段恢复
                    # _xp_total 接着累加，_xp_last 恢复后才能正确算首步增量（否则首步重置基线）。
                    "xp_total": _xp_total,
                    "xp_last": _xp_last,
                }
                try:
                    await checkpoint_cb(n_nights, _snap)
                except Exception:  # pylint: disable=broad-except
                    logger.exception("[minecraft] 第%d夜 checkpoint 失败 —— 不影响轨迹，"
                                     "但该夜存档点未落盘（续跑会回到更早存档）", n_nights)
            # 跨过午夜（worldDay 滚动 = 24000/0）—— 通知 ReAct 分支「本天结束」（should_stop 读这个
            # 标志立刻收尾）。旧分支忽略它，靠下面的 run_over 决定是否 break。
            _state["day_over"] = True
            if n_nights >= max_nights:
                logger.info("[minecraft] 已过 %d 个夜晚，结束本轮探索（step%d）", n_nights, step_idx)
                _state["run_over"] = True
        elif not was_night and now_night:
            # 昼→夜跳变：入夜。框架强制检索过夜经验（不依赖 Agent 主动 retrieve）。
            # ★ 只打标记，不在此检索 —— retr/primary/secondary 在 react 层闭包里，env 层拿不到
            #   （env 不认识检索栈）。react 层在 ReAct 每步工具后钩子（ReActAgent.on_step）读这个
            #   标记、用闭包里的检索（_unified_inject，增量去重）、把结果拼进本步 write_code
            #   toolresult 末尾（EXP_BLOCK_MARKER 标边界，与 createplan 同款）→ 下次 think 看得到。
            #   一天只触发一次：标记在 react 层消费后置 False（见 domains/minecraft 的 _night_memory_inject）。
            #   出生即夜（was_night 初值已是 True，env.py:2622）不触发本分支 —— 等不到昼→夜跳变，
            #   但那天本就是从夜开始的，Agent 开场 day_start 自带经验检索（createplan 时），不缺。
            _state["night_just_fell"] = True
        was_night = now_night
        # 日界判据基准刷新（与 was_night 同步）：cur_day 可能为 None（异常/老 payload 无 worldDay）
        # —— 此时保持 last_day 不变，下一步日界判据退化到 timeTicks 回绕兜底（同为 24000/0 口径，不崩）。
        if cur_day is not None:
            last_day = cur_day
        # 返回给 LLM 的 tool result：执行反馈 + **完整的新观察**。
        # 为什么要带完整观察：world.step 之后整个世界都变了（时间流逝、怪物移动、可能死亡重生），
        # 观察不是这段代码的「返回值」而是一次环境快照 —— 只给执行结果不足以指导下一步动作。
        #
        # ★ **反馈不截断**（2026-08-12，与 `_day_trajectory_text` 同批）：这是 bot 自己设计的
        #   观测通道（onLog 是它写的 console.log、onError 是真实报错），截掉尾巴等于让它看不到
        #   自己那句汇报的结论。上限由**上游**保证：onLog 单步封顶 40 行 × 300 字符
        #   （见 `mc_server/lib/observation/onLog.js`）；onChat 同口径 40 条 × 300 字符
        #   （2026-09-23 补，见 onChat.js）；onError 单条覆盖、不累积。
        #   ⚠️ 旧注释写的「onChat 来自服务器播报」不成立：chatEvent 是内部自造事件。
        #   ⚠️ 与反思那处不同，这一段在**一天内累积**且不被压缩（CompactingContextBuilder
        #   只砍观察块、保留反馈，见 react_tools.py）—— 实测截 400 时天末累积 median 6.5K /
        #   max 21.8K 字符，而只有 3.2% 的条目贴着 400 上限，故放开的增量很小；
        #   真出现「某天反馈把上下文顶掉」，该治的是压缩策略（让旧反馈也参与压缩），
        #   不是回来把单条截短 —— 后者丢的是最新那条的结论，正好是最该看的。
        if last_events:
            fb = "执行反馈：\n" + "\n".join(f"  [{k}] {v}" for k, v in last_events)
        else:
            fb = "执行反馈：（无输出）"
        # 分隔标记必须与 react_tools.OBS_SEPARATOR 一致 —— 那边靠它切掉旧观察做上下文压缩，
        # 两处不一致会让压缩**静默失效**（观察一直堆积而没人报错）。故从那里 import，不再手写。
        #
        # ★ **不写步号**：这里的 step_idx 是 write_code 的调用计数，而
        #   Agent 上下文里另有一套编号 —— plan 的步号（createplan 的 steps 下标）。两套数字混在
        #   一起会让它把「当前观察（第7步之后）」错认成「计划第 7 步」。而且 tool result 里本来
        #   就不需要时序标记：消息在对话中的位置已经表达了先后。
        from .react_tools import OBS_SEPARATOR
        # ★ 时间栅栏 trailing pause：本步 world.step 已跑完（含 dawn 分支的 pause→反思→resume→
        #   save_world→day_over 全流程），此刻世界处于「可冻」状态。重新冻结，让下一段 LLM 思考
        #   期间游戏时间不流逝（栅栏的目的）。
        #   ★ 排序关键：必须在 checkpoint_cb（save_world）**之后**——save_world 要 unfrozen 落盘
        #     （env.py:1970/3303 注释：冻着存档→续跑读档世界冻死）。dawn 步顺序为 resume@解冻 →
        #     save_world → day_over → 此处 trailing pause（为次日 LLM 规划重新冻结），save 时世界仍
        #     unfrozen，正确。dawn 内部 pause/resume 不动（职责不同：它冻为反思，此处冻为思考）。
        #   ★ gate `not run_over`：最后一夜 run_over=True 时 checkpoint 已被 not run_over 跳过，
        #     此处也跳过→世界保持 unfrozen。否则下一轮 reset/prewarm_and_spawn 的 /forceload+
        #     waitForTicks 会卡死（env.py:2150 注释：forceload 不落地等 tick→/step 挂满超时）。
        #     /start 不 unfreeze（index.js:241-490），不会救你。
        #   pause 的 obs 不赋给 _state["obs"]——上面 return 用的是本步 world.step 后的 obs（步末快照），
        #   pause 返的是冻结后快照，两者时间一致但语义不同，且下步 resume 会再取一次，无需覆盖。
        if not _state["run_over"]:
            try:
                _po, _, _psys, _ = await asyncio.to_thread(world.pause)
                _record_system(_psys, step_idx)
                # ★ 步末 pause 间隙晚到的 onSystem 成就播报（执行完→冻结前到达）喂总变量防漏。
                _absorb_advs(_po, _psys)
                # ★【栅栏验证插桩】步末 tick：trailing pause 冻结后读 timeTicks。与下步初 tick 对比。
                #   临时验证用，确认后可删。
                _t1_tick = ((_po.get("status", {}) or {}).get("timeTicks"))
                logger.info("[栅栏验证] step%d 末 tick=%s（pause 后，下步初应等于此值）", step_idx, _t1_tick)
            except Exception:  # pylint: disable=broad-except
                # trailing pause 失败=思考期间世界可能继续 tick（栅栏漏了），但不致命：下步 resume 仍会
                # 重新解冻+补发 tick rate。记 WARNING 不中断轨迹。
                logger.warning("[minecraft] 时间栅栏 trailing pause 失败，本步间思考期世界可能未冻结",
                               exc_info=False)
        # ★ 观战相机看门狗（2026-09-15，用户提议的「每步检测是否在观战 bot」）：读本步 payload 里
        #   spectatorCam 探针（bot 侧周期发 /data get entity <spec> Pos 取观战者坐标——服务器权威：
        #   附身时观战者坐标每刻被服务器写成 bot 位置，脱附后停在脱附点，与 bot 距离拉开），
        #   判定脱附就重附。放在步末 trailing pause 之后（世界已冻结）——重附的 10~15s 墙钟全是
        #   xdotool 命令链的 sleep，不花游戏时间；这是死亡重附、天边界重附都选冻结窗口的原因。
        #   不需要死亡/日界特判：死亡步的重附在前（resync 成功→本步探针距离≈0→看门狗 no-op；
        #   失败→看门狗补试）；dawn 步的天边界重附在反思前，成败同样反映在本步探针读数里。
        #   它多兜住的两条路径：bot 断连自愈重连（实体移除→脱附，无死亡播报）与栅栏失效的步间
        #   死亡——此前只能等天边界，画面错位最长一整天。判据见 _spectator_needs_reattach。
        if recorder is not None and not _state.get("run_over") and _spectator_needs_reattach(next_obs):
            logger.info("[minecraft] step%d 看门狗：观战相机未跟着 bot（spectatorCam=%s），重新附身",
                        step_idx, (next_obs or {}).get("spectatorCam"))
            try:
                await asyncio.to_thread(recorder.resync_spectate, reason="看门狗检测到脱附，")
            except Exception:  # pylint: disable=broad-except
                logger.debug("[minecraft] 看门狗重附失败（已忽略，天边界还会兜底）", exc_info=False)
        return f"{fb}\n{OBS_SEPARATOR}\n当前观察：\n{render_observation(_state['obs'], world.tick_rate, set(_seen_advancements))}"

    # ★ 时间栅栏初始冻结：进 ReAct 循环前先冻一次。首个 _do_one_step 的 resume 会重新解冻（幂等，
    #   resume 重发 tick rate 无害）再 step。这样「spawn 后到首次 write_code 之间」的 LLM 思考
    #   （新天 createplan / 记忆检索 / 规划）也被栅栏挡掉，游戏时间不空转。
    #   不在 spawn（env.py:2493）后立刻冻——smoke_dawn_nudge（需 waitForTicks）/recorder.attach 等
    #   trusted 基建假定世界在 tick，冻结要尽量晚，贴着循环入口。第一夜若已入夜、首步前就有思考，
    #   正好被这刀挡住。
    try:
        _iobs, _, _isys, _ = await asyncio.to_thread(world.pause)
        _record_system(_isys, _state["step_idx"])
        # spawn 后、首步前晚到的成就播报（概率≈0 但兜住）：喂总变量。
        _absorb_advs(_iobs, _isys)
    except Exception:  # pylint: disable=broad-except
        logger.warning("[minecraft] 时间栅栏初始冻结失败，首步前思考期世界可能未冻结", exc_info=False)

    await _react_day_loop(react_day_cb, _state, _do_one_step,
                          lambda: render_observation(_state["obs"], world.tick_rate, set(_seen_advancements)),
                          lambda: last_events)

    obs = _state["obs"]
    final_inv = obs.get("inventory", {}) or {}
    final_equipped = equipped_item_names(obs.get("status", {}) or {})
    # ★ final_tier 读 _seen_advancements（env 闭包态）而非 tier_progress[-1]：后者取末值，自愈
    #   ② 重启进程后 tier_progress 末值会骤降（packet 清零 → _track 那步 base_depth=0 append 进 list），
    #   但那是采集断层非真实退步。_seen_advancements 跨重连存活、只增不减，base_depth 取它才是
    #   bot 真实能力。与 _track 的 tier 口径逐字一致（_track 末步 tier_progress[-1] 已等于
    #   base_depth(_seen_advancements) 当步值，重连后该集合未变则 tier 不掉）。
    final_tier = base_depth(_seen_advancements)  # base depth 整数（0-9）
    # 最终记分板权威值（与 daily_log 里每夜的 scoreboard 同源，这里是末夜快照，供 aggregate_eval
    # 直接取用、不必从 night_reflections 末元素捞）。口径见 _extract_scoreboard。
    final_scoreboard = _extract_scoreboard(
        obs.get("scoreboard"), (obs.get("status", {}) or {}).get("name"))
    # 最终进度协议包佐证快照（与 daily_log 里每夜的 advancement_packet 同源，末夜快照）。口径见
    # _extract_advancement_packet：能采到 onSystem 漏掉的 announce_to_chat=false 静默进度。
    final_advancement_packet = _extract_advancement_packet(obs.get("advancementPacket"))
    # ★ tier_rank 取**沿轨迹的高水位**（对齐 Voyager Table 1 的「首次解锁某级」语义）：
    # 里程碑一旦达成就不该被收回。若只取 final 快照，而本域 keep_inventory=false、
    # 进度达成后死亡不会取消进度（服务器侧进度是永久的），但 base_depth 看的是「已达成
    # 进度集合」—— 集合只增不减，故高水位=末值，仍保留 peak/final 双值口径以兼容历史
    # 信号字段（peak>final 不再会发生于进度口径，但字段保留不破坏 schema）。
    # tier_progress 现存 base depth 整数(0-9)，直接 max 即得高水位，无需 _TIER_RANK_9 查表。
    peak_tier = max(tier_progress, default=0)
    peak_rank = peak_tier
    # 首次达到各 depth 的步序（对标 Voyager Table 1「解锁该级所需 prompting iterations」）。
    # tier_progress[i] 是「第 i 个 _track 记账点」的 base depth，**不等于**第 i 个真实 step ——
    # 黎明 resume 会插入无对应 trajectory 条目的记账点（见 tier_progress_steps 声明处）。
    # 报步数一律查 tier_progress_steps[i]，不要直接用 enumerate 的 i。
    # ★ 2026-09-05 改：tier_progress 存 depth 整数，tier_first_step 的 key 改成 depth(1-9)
    #   整数（不再是 9 级 tier 名）。一步跨多级时按 depth 区间补齐。
    tier_first_step = {}
    seen_depth = 0
    for i, d in enumerate(tier_progress):
        if d > seen_depth:
            for level in range(seen_depth + 1, d + 1):  # 补齐一步跨多级
                if level not in tier_first_step:
                    tier_first_step[level] = tier_progress_steps[i]
            seen_depth = d
    deaths_summary = _summarize_deaths(deaths)
    # map coverage（对标 Voyager 的 distance traveled）：路程 + 离出生点最远距离 + 群系覆盖。
    # 必须在 trajectory 收齐之后算 —— 它逐步读 rec["pos"]。
    path_stats = _summarize_path(trajectory, spawn_actual, biomes_seen)
    adv_names = [a.get("advancement") for a in advancements if a.get("advancement")]

    # 整轮轨迹文本化（供 record 留痕；反思是按天喂的，不复用这个）
    # ★ **同样不截断**（2026-08-12）：它没有 LLM 消费者（只进 `Sample.text` → record 的
    #   `sample_answers`，给人事后翻），所以这里的取舍不是"上下文预算"而是"留痕要不要完整"
    #   —— 而一份**故意截半**的留痕，读的人无从分辨"代码就这么短"还是"被砍了"，
    #   反而比没有更误导。落盘体积不是问题（实测一条 30 天轨迹全部代码约 66 万字符）。
    lines = ["自由探索轨迹（Minecraft，昼夜节律）："]
    for t in trajectory:
        lines.append(f"[第{t['step']}步 观察] {t['obs']}")
        lines.append(f"> 生成代码：{t['code'].strip()}")
        if t["events"]:
            ev_txt = "; ".join(f"{k}:{v}" for k, v in t["events"])
            lines.append(f"  [反馈] {ev_txt}")
        # 服务器权威事件单独成行（死亡/成就是轨迹里最该被看见的两类事实）
        for d in t.get("deaths") or []:
            lines.append("  [死亡] " + _death_line(d))
        for a in t.get("advancements") or []:
            lines.append(f"  [成就] {a}")  # a 是成就显示名（packet 源，list[str]）
    result_line = (
        f"探索结果：过 {n_nights} 夜、死 {len(deaths)} 次"
        + (f"（{deaths_summary}）" if deaths_summary else "")
        + f"，发现 {len(items_seen)} 种独特物品，最高进度深度={peak_tier}"
        + (f"（最终={final_tier}）" if final_tier != peak_tier else "")
        # 走了多远也进这一行：它和"发现了多少种物品"是同一层的探索广度事实，而这行文本会进
        # trajectory_text → 喂给反思 Agent，让它能复盘"我今天是走远了还是在原地打转"。
        + f"，行进 {path_stats['path_len']:.0f} 格"
        + (f"、最远离出生点 {path_stats['max_dist_from_spawn']:.0f} 格"
           if path_stats["max_dist_from_spawn"] is not None else "")
        + f"、走过 {path_stats['n_biomes']} 种生物群系"
        + (f"，服务器成就 {len(adv_names)} 项：{'、'.join(adv_names)}" if adv_names else "")
        + f"（{len(trajectory)}步）"
    )
    lines.append(result_line)
    trajectory_text = "\n".join(lines)

    return {
        # ★ 续跑缺口②残余（2026-08-21）：trajectory 有意不恢复（太大，见 _rm 恢复处注释），
        #   故 len(trajectory) 只算本段；但 step_idx 已从 resume_metrics 恢复前段累计（见 _state
        #   初始化），每步 +1，return 时 = 前段 + 本段真实步数。用 step_idx 让 n_steps 与
        #   tier_first_step 的步序同口径（都含前段），单段内与 len(trajectory) 等价。
        #   path_stats 仍只算本段（依赖 trajectory，已知局限，价值/成本比低不动）。
        "n_steps": _state["step_idx"],
        "n_nights": n_nights,
        "n_deaths": len(deaths),
        "deaths": deaths,                    # 结构化死亡明细（step/day/cause/killer/position/nearby）
        "deaths_summary": deaths_summary,    # 按死因聚合的一行摘要
        "n_advancements": len(advancements),
        "advancements": advancements,        # 服务器权威进度里程碑（对标 Voyager tech tree 的独立指标）
        "unique_items": len(items_seen),
        "items_seen": sorted(items_seen),
        # 主动建造过的物品名（从 code 解析 craftItem/placeItem，见 _extract_built_from_code）。
        # 留痕用：daily_log 里的 buildings_so_far 已是它 ∩ buildable 的结果，这里带全集供事后复盘。
        "built_items": sorted(built_items),
        # map coverage 四件套（path_len / max_dist_from_spawn / n_pos_jumps / n_biomes…），
        # 对标 Voyager 的 distance traveled；口径与取舍见 _summarize_path 的 docstring。
        **path_stats,
        # tier 三件套：peak=高水位（打分用，对齐 Voyager「首次解锁」）、final=最终快照（peak>final
        # 即「达成过但丢了」）、first_step=各级首达步序（直接对报 Voyager Table 1 的 iteration 数）。
        "peak_tier": peak_tier,
        "tier_rank": peak_rank,
        "final_tier": final_tier,
        "final_tier_rank": final_tier,
        "tier_first_step": tier_first_step,
        "tier_progress": tier_progress,
        "trajectory": trajectory,
        "trajectory_text": trajectory_text,
        "final_inventory": final_inv,
        # 最终穿在身上的（护甲/副手）—— 与 final_inventory 并列而非合并：留痕时要能分清
        # 「背着的」和「穿着的」（护甲穿在身上才起作用），且 final_inventory 是 {名:数量}、
        # 这里只是名字集合，形态本就不同。
        "final_equipped": sorted(final_equipped),
        "night_reflections": night_reflections,
        # MC 累计 XP（score 数据源，2026-08-25）：跨命累计总 XP，死亡不扣减，单调递增。
        # 见 _xp_total 记账逻辑（只累加正增量）。续跑从 metrics_snapshot.xp_total 恢复。
        "xp_total": _xp_total,
        # XP 等级（MC 原生口径，2026-09-10）：末态 obs.status.experience.level。与 daily_log 里
        # 每夜的 xp_level 同源同口径——死后会被服务器压低（仅旁证，非跨命累计）。补落盘让
        # quick_metrics 末态口径也能取到，与按夜口径对称。
        "xp_level": (obs.get("status", {}).get("experience") or {}).get("level"),
        # 工具材质 4 级（2026-09-10）：与 daily_log 里的 tool_tier 同源同口径（都调 tool_tier_name，
        # 按库存+装备的最高级成品工具材质判定 wooden/stone/iron/diamond/none，非单调、工具断裂回退）。
        # 与 9 级 tier_rank 并列展示、互不替代。末态用 final_inv+final_equipped 结算。
        "tool_tier": tool_tier_name(final_inv, final_equipped),
        # MC 记分板权威值末夜快照（交叉校验旁证，2026-08-26）：与自统计的 n_deaths/xp_total 等
        # 并排留存。每夜时序在 daily_log.scoreboard，这里是末夜值供 aggregate_eval.detail 直接取。
        # 只落盘双值、不比对、不修正；口径差见 _extract_scoreboard。
        "scoreboard": final_scoreboard,
        # 进度协议包佐证末夜快照（2026-09-07）：与 advancements（onSystem 被动播报）并排留存，
        # 能采到 announce_to_chat=false 的静默进度（story/root 等）。每夜时序在
        # daily_log.advancement_packet，这里是末夜值。口径差见 _extract_advancement_packet。
        "advancement_packet": final_advancement_packet,
        # 真实出生点 + 生物群系：随机出生时"抽到哪、落在什么地形"是解释这条轨迹的关键条件
        # （落在森林 vs 落在雪原，得分天差地别），也是复现单条轨迹所需的信息。
        "spawn_actual": spawn_actual,
        "spawn_biome": spawn_biome,
    }
