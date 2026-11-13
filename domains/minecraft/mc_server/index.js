const fs = require("fs");
const express = require("express");
const bodyParser = require("body-parser");
const mineflayer = require("mineflayer");

const skills = require("./lib/skillLoader");
const { initCounter, getNextTime } = require("./lib/utils");
const obs = require("./lib/observation/base");
const OnChat = require("./lib/observation/onChat");
const OnError = require("./lib/observation/onError");
const OnLog = require("./lib/observation/onLog");
const OnSystem = require("./lib/observation/onSystem");
const OnSkill = require("./lib/observation/onSkill");
const { Voxels, NearbyGrid, Vision } = require("./lib/observation/voxels");
const Status = require("./lib/observation/status");
const Inventory = require("./lib/observation/inventory");
const Chests = require("./lib/observation/chests");
// 记分板采集器：把 MC 原生记分板权威值(deathCount/totalKillCount/xp/health/food/armor)
// 收进 observe，作 env 侧自统计指标的交叉校验旁证。不渲染给 LLM（env.py _IGNORED_FIELDS）。
const Scoreboard = require("./lib/observation/scoreboard");
// 进度协议包采集器：监听 packet_advancements 协议包维护的「已完成进度」状态，作 onSystem 被动
// 播报的佐证支路——能采到 announce_to_chat=false 的静默进度（如 story/root=[Minecraft]）。
// 不渲染给 LLM（env.py _IGNORED_FIELDS），dawn 时与 advancements_so_far 并排双值落盘。
const AdvancementPacket = require("./lib/observation/advancementPacket");
// 观战相机探针（2026-09-15）：读观战账号玩家实体与 bot 的距离——附身时恒 ≈0、脱附后冻在
// 脱附点。env 步末看门狗据此触发 resync_spectate（footage 录像防脱附空镜）。不渲染给 LLM。
const SpectatorCam = require("./lib/observation/spectatorCam");
const { plugin: tool } = require("mineflayer-tool");

let bot = null;

// 单步代码执行时限：超此值认定「任务超时」，中止本步、把超时作为 result 反馈给 bot
// （不掉线、不自愈）。500 刻（1x 下 25s）是**默认值**，bot 可在 /step 里用 timeoutTicks 按动作大小
// 自选（如挖 10 个木头要更久），服务端 clamp 到 [MIN, MAX] 刻。**单位是游戏刻**（详见下面常量块）。
//
// 为何敢放宽到 500 刻以上：早期掉线的根因**不是** await 太久，而是 pathfinder 同步 A* 阻塞事件循环
// 导致 keep_alive 回不去（见 /start 里 thinkTimeout/tickTimeout 的注释），那个已根除。长时间
// await 本身不阻塞事件循环，心跳照常。
//
// ★ 单位是**游戏刻**（game ticks），不是墙钟毫秒：让 Agent 生活在 MC 时间里——对它而言
//   世界以 20 刻/秒流转、一天 24000 刻，所有时限都按这个口径给和执行，没有任何「真实秒」概念
//   泄漏给它。执行上也按游戏刻计时（见 evaluateCode 的 timeoutPromise），于是**与倍速 N
//   无关**：3000 刻在 1x 是 150s 墙钟、在 2x 是 75s 墙钟，但对 Agent 与对执行都是「3000 刻」，
//   不再需要 `/(20*rate)` 那套折算。游戏刻取自服务器权威的 `bot.time.timeOfDay` 增量（处理午夜
//   回绕），不是 `globalTickCounter`（那个在本地物理 tick 上自增、服务器 freeze 时仍走，会与游戏
//   世界脱节）。
const STEP_CODE_TIMEOUT_TICKS = 500;
const STEP_CODE_TIMEOUT_MIN_TICKS = 100;
// 上限 3000 刻（与 Python 侧 _STEP_TIMEOUT_MAX_TICKS 同步）：覆盖 prompt 指导的最重合法动作
// （远距寻路/探索「上千刻」），并把单个失控步的代价封顶（2x 下 75s 墙钟）。不再用 10000 刻——
// 实测 Agent 从不用 bot.sleep/waitUntil(dawn) 真等一夜（prompt「天黑不是停手的理由，别整夜
// 原地空等天亮」），10000 是无人调用的安全网；且 Agent 倾向「能申多申」，长步申请 10000 刻却
// 17-21s 就完成、真实消耗 <1000 刻。降到 3000 既够用又逼 Agent 按动作大小估时限。
// ⚠️ **仍必须 < 夜窗 10000 刻**：一旦上限 ≥10000，一个步就能「白天起步→吞掉整夜→又在白天结束」，
//   那次 worldDay 滚动（24000/0）就可能不出现在步边界上 → 那天不结束、不反思、n_nights 不涨
//   （2026-08-13 实测 carol day04 step102 从 noon 连跑 568s 吞掉整夜，alice 31.4 游戏日只触发 26
//   次反思）。3000 远低于夜窗，天然躲过这个坑；步内另有 dayBreakPromise（见下方护栏 A，按 worldDay
//   滚动熔断）双保险。
const STEP_CODE_TIMEOUT_MAX_TICKS = 3000;

// ── heap 峰值告警（配合 env.py _launch_server 的 node --max-old-space-size=4096）─────────
//
// 0910 run 末段 25 次秒级 ConnectionError 自愈，全由自产技能 lightAreaWithTorches 的双层循环
// （radius=8,step=2 → 81 候选点，每点 goto+blockAt 纵扫+placeBlock）触发——进程 heap 峰值飙升
// → V8 heap OOM 退出 → 端口秒级 DOWN → Python 侧只看到 ConnectionError。崩前无痕（mineflayer.log
// 被 50M 截断+重启覆盖吃掉），无声 OOM。这里照搬 window_click 风暴熔断的「步内采样+步末注入 onError」
// 模式（见 /start 里 bot._client.write 的 wrap）：不主动中止（内存飙升不该粗暴打断合法慢操作，
// 且抛错未必能阻止已分配内存），只把峰值到危险区这一事实留痕+反馈给 Agent。
//
// ★ 阈值用 heapUsed 占老生代上限的**比例**（非 RSS）：RSS 含 C++ 层/原生缓冲/共享库，波动大易误报；
//   V8 heap OOM 看的是 heapUsed 对 --max-old-space-size。_OPENKAGE_HEAP_LIMIT_MB 必须与 env.py 的
//   --max-old-space-size 数值一致（都 4096），改一处须同步另一处。
// ★ 0.80 = 3.2G：给峰值留 0.8G 余量。lightAreaWithTorches 那类短时密集 goto 冲到这里就该警觉——
//   离 OOM 还有距离但已在危险区，让 Agent 知道「这步很吃内存、别再连环调」。
const _OPENKAGE_HEAP_WARN_RATIO = 0.80;
const _OPENKAGE_HEAP_LIMIT_MB = 4096;  // 必须与 env.py 的 --max-old-space-size 一致
const _HEAP_SAMPLE_INTERVAL_TICKS = 20; // 每 20 physicsTick 采一次（≈1s 游戏时间，与 stall 护栏同节拍）

/**
 * 测试钩子：允许通过环境变量临时压低 heap 告警阈值，触发告警路径而不必真把堆冲到 3.2G。
 *
 * 冒烟测试用法：
 *     OPENKAGE_HEAP_WARN_RATIO=0.00001 node test_heap_warning_smoke.js
 *
 * 仅对imulation/e2e 测试生效；生产运行不要设该变量（默认 0.80）。
 */
function _openkageHeapWarnRatio() {
    if (process.env.OPENKAGE_HEAP_WARN_RATIO) {
        const v = Number(process.env.OPENKAGE_HEAP_WARN_RATIO);
        if (Number.isFinite(v) && v > 0 && v <= 1) return v;
    }
    return _OPENKAGE_HEAP_WARN_RATIO;
}

// ── 步内昼夜跳变护栏（护栏 A，与步上限 < 夜窗互为双保险）───────────
//
// env.py 判「这一天结束」的唯一依据是 `is_night()` 在**步边界**上看到 夜→昼 跳变。
// 一个足够长的步能把整个白天窗口跨过去，那次黎明就永远不出现在任何步边界上 ——
// 那一天不结束、不反思、n_nights 不涨（见 STEP_CODE_TIMEOUT_MAX_TICKS 上面的实测记录）。
// 所以：**步内一旦观察到 夜→昼 跳变，就主动提前结束这一步**，把控制权交回 Python，
// 让它在紧接着的步边界上立刻看到 is_night=false → 正常走「冻结 → 反思 → 解冻」。
//
// ★ 阈值**必须与 lib/observation/status.js 的 getTime() 逐个数字对齐** —— env.py 的
//   NIGHT_PHASES({night,midnight}) 作用在那份标签上。按官方四阶段：白天 = day+noon = [0,12000)；
//   sunset[12000,13800)/night[13800,22200)/sunrise[22200,24000) 都不算白天（过渡段或夜晚）。
//   对不齐的后果是护栏在 env 认定天亮之前/之后才触发，等于没修。
const _DAY_END_TICK = 12000;    // >=12000 离开白天（进 sunset 过渡段）
const _DAY_START_TICK = 24000;  // 24000=0 回到白天（sunrise 段 22200-23999 不算白天）
/** 与 status.js getTime() 的 phase 划分等价的「是否白天」（仅 day+noon 段）。 */
function isDaytimeTick(t) {
    return t < _DAY_END_TICK;  // 24000 回绕到 0 自然落入 t<12000，无需显式判 >=_DAY_START_TICK
}

/**
 * ★ 进食/血量埋点的滤噪判定（2026-09-17，纯函数，可单测）。
 *
 * mineflayer `health` 事件每次 health/food/foodSaturation 任一变化都 fire（含饱和度自然衰减）。
 * 量化「生存」只关心进食/回血/受伤，故滤掉噪声：只在 food↑/health↑/health↓ 时记，trigger 单值。
 *   food↑  → "eat"（进食优先级最高；进食常同时带 health↑，原因归 eat）
 *   满食(f===20)且 sat↑ → "eat"（吃金苹果类 ALWAYS_CONSUMABLES，food 顶满不动、只有饱和度涨
 *     ——smoke 踩坑：满食吃金苹果 food 20→20 sat↑，若不补这条会漏记。饱和度只在进食时上升、
 *     平时只衰减，且真实 run 无 saturation 药水效果，满食+sat↑ 不可能是噪声）
 *   health↑ → "heal"
 *   health↓ → "damage"
 *   h===0 → null（死亡瞬间服务器把 food/sat 全重置到满——update_health(0,20,20)——
 *     food↑/sat↑ 全是重置假象；这次事件由紧随的 death 行权威记录）
 *   其余（仅饱和度衰减/全无变化）→ null（不记）
 *
 * @param {{h:number,f:number,s:number}} before  上一事件的 health/food/sat
 * @param {{h:number,f:number,s:number}} after   本事件的 health/food/sat
 * @returns {"eat"|"heal"|"damage"|null}
 */
function _classifyEatEvent(before, after) {
    // ★ h===0 一律 null：死亡瞬间服务器把 food/saturation 全重置到满（实测
    // update_health(0,20,20)），food↑/sat↑ 都是重置假象、不是进食。这次事件的信息
    // 由紧随的 death 行权威记录（health_before 取 _eatPrev、health_after=0）。
    if (after.h === 0) return null;
    if (after.f > before.f) return "eat";
    // 满食吃金苹果类：sat↑ food 不动 → eat（ALWAYS_CONSUMABLES 顶着满食也能吃）。
    // 饱和度只在进食时上升、平时只衰减，且真实 run 无 saturation 药水效果，满食+sat↑ 不可能是噪声。
    if (after.f === 20 && after.s > before.s) return "eat";
    if (after.h > before.h) return "heal";
    if (after.h < before.h) return "damage";
    return null;
}

// ── 等 tick，但**绝不永久挂起** ─────────────────────────────
//
// `bot.waitForTicks()` 依赖服务器的 tick 包到达。bot 一旦短暂断流（人类玩家登录后两个
// 客户端争抢新区块、bot 收不到 tick；也可能是网络抖动 / 服务器 GC / 区块生成），这个 await
// **永不 resolve**。而 /step 与 /start 都要在发响应**之前**等一次 tick 取最新观察 ——
// 于是 HTTP 响应发不出去，Python 侧只能干等满 STEP_TIMEOUT=300s 才判超时并自愈重连。
// 每次人进服围观都会撞上，表现为「莫名的 /step 失败 → 自愈」。
//
// 修法：给它套一个墙钟兜底。拿不到 tick 就带着**略旧一点的观察**返回 —— 少等几个 tick 完全
// 可接受，而挂死 300 秒不可接受。
// ★ 不能改成"吊销 waitForTicks 让它抛异常"：响应链路自己就在用它，抛异常会让 /step 直接 500。
//   兜底返回（而非抛错）才是这里正确的语义。
const TICK_WAIT_FALLBACK_MS = 3000;

async function waitTicksSafe(bot, ticks, fallbackMs = TICK_WAIT_FALLBACK_MS) {
    let timer = null;
    try {
        await Promise.race([
            bot.waitForTicks(ticks),
            new Promise((resolve) => {
                timer = setTimeout(() => {
                    console.log(
                        `[tick] 等 ${ticks} tick 超过 ${fallbackMs}ms（bot 可能短暂收不到 tick），` +
                            "带当前观察继续，避免 HTTP 响应挂死"
                    );
                    resolve();
                }, fallbackMs);
            }),
        ]);
    } finally {
        if (timer) clearTimeout(timer);
    }
}

// bot 自选时限的 clamp：非数/越界都收敛到合法区间，坏输入不至于让一步永不超时或秒超时。
// ★ 单位是**游戏刻**（见 STEP_CODE_TIMEOUT_TICKS 的说明）。HTTP 字段名 `timeoutTicks`，
//   与 Python 侧 env.step 的 body["timeoutTicks"] 对齐。
function clampTimeout(v) {
    const n = Number(v);
    if (!Number.isFinite(n) || n <= 0) return STEP_CODE_TIMEOUT_TICKS;
    return Math.min(STEP_CODE_TIMEOUT_MAX_TICKS, Math.max(STEP_CODE_TIMEOUT_MIN_TICKS, Math.round(n)));
}

// ★ 按游戏刻计时：累加 `bot.time.timeOfDay` 的增量（处理午夜 24000 回绕），到 budgetTicks 即超时。
//   与倍速无关、与墙钟无关——刻就是 Agent 体验到的「世界时间」本身。服务器权威 timeOfDay 在一步内
//   单调推进（freeze 只在反思期、不在步中），故可作计时基准；`globalTickCounter` 不行（本地自增、
//   freeze 时仍走）。与 deathPromise/dayBreakPromise 同构：轮询 + 挂 codePromise.finally 清理。
//
// ★★ **墙钟兜底**（2026-08-25 修，对应两次 run 的 ReadTimeout 事故）：
//   timeOfDay 只在收到服务器的 `update_time` 包时更新（见 lib/plugins/time.js）。bot 一旦**断流**
//   （收不到任何包：网络抖动 / 服务器 GC / 区块生成争抢 / 半连接），timeOfDay 冻结 → 上面的累加器
//   `acc` 永不增长 → `__TIMEOUT__` 永不触发 → /step handler 卡在 Promise.race → Python 干等满
//   STEP_TIMEOUT=300s → ReadTimeout → 自愈重连。这正是文件顶部 99-107 行用 waitTicksSafe 给
//   waitForTicks 套墙钟兜底的同一类缺陷，但本函数此前没有兜底。
//   修法：再加一条**墙钟上限** setTimeout —— `budgetTicks / (20*rate)` 秒后强制超时，不依赖
//   timeOfDay 是否在推进。事件循环还活时（断流、goto 永不 resolve、任意异步挂起）它能正常触发，
//   把那些原本要挂满 300s 的步在「本步申请的时限」内收掉、干净返回 __TIMEOUT__。
//
//   ⚠️ **本兜底救不了纯同步死循环**（如 LLM 手写 `while` 反复 `bot.dig` 脚下方块、bot 不掉落 →
//   循环第二轮起无 `await` → 锁死整个事件循环）：setTimeout 回调与 setInterval 一样排不上。
//   这是 JS 单线程的根本约束，非代码 bug。那条路径只能靠 Python 侧 300s HTTP 超时 + 重启进程兜底
//   （env.py 的 step() ReadTimeout → _heal_and_restart 重起进程），并在 onError 反馈里告诉 Agent
//   「你写死循环了，向下挖用 digDownTo」。已由 digDownTo 原语 + prompt 在源头消除主要诱因。
function makeTickTimeoutPromise(codePromise, budgetTicks, bot) {
    return new Promise((resolve) => {
        let settled = false;
        const finish = () => {
            if (settled) return;
            settled = true;
            clearInterval(iv);
            clearTimeout(wallTimer);
            resolve("__TIMEOUT__");
        };
        let acc = 0;
        // ★ bot 可能已被 onDisconnect 置 null（被踢/断连，见 otherError 的 null 兜底注释）：
        //   本 setInterval 是纯 JS 定时器，不随 bot.end() 停（不像 bot.on(physicsTick) 事件源死即停），
        //   断连后仍每 300ms 跑、读 bot.time 会抛 TypeError（虽被 otherError 兜住不崩进程，但刷屏）。
        //   判空即 finish() —— bot 都断了，这步本就该结束（与 deathPromise 的 null 兜底同口径）。
        let prev = (bot && bot.time && typeof bot.time.timeOfDay === "number")
            ? bot.time.timeOfDay : null;
        const iv = setInterval(() => {
            if (!bot) { finish(); return; }
            const t = bot.time && typeof bot.time.timeOfDay === "number"
                ? bot.time.timeOfDay : null;
            if (typeof t !== "number") return;
            if (typeof prev === "number") {
                // 增量，处理 24000 回绕：最多跳一天（24000），正常推进远小于它。
                let delta = (t - prev + 24000) % 24000;
                if (delta > 12000) delta = 0; // 回绕或异常大跳（如 /time set），不计入
                acc += delta;
            }
            prev = t;
            if (acc >= budgetTicks) {
                finish();
            }
        }, 300);
        // 墙钟兜底：timeOfDay 冻结（断流/goto 挂起）时，靠这条到点强制超时。rate 与
        // bot.openkageStepDeadlineMs 同口径（index.js /step 里设）；取不到时退回模块级 currentTickRate。
        const _rate = Math.max(1, Number(bot.openkageTickRate) || Number(currentTickRate) || 1);
        const wallMs = Math.max(1000, (budgetTicks / (20 * _rate)) * 1000);
        const wallTimer = setTimeout(finish, wallMs);
        // codePromise 落定（成功/死亡/天亮/真报错）后清掉两条 timer，别让它们空跑到点误触发超时。
        codePromise.finally(() => { clearInterval(iv); clearTimeout(wallTimer); }).catch(() => {});
    });
}

// ★ OPENKAGE 倍速改造（2026-08-21）：当前生效的游戏倍速 N（1=自然流速）。
// 由 /start 的 req.body.tick_rate 设置，两侧同步：server `/tick rate ${20*N}`（= 20N TPS = N 倍速；
// ⚠️ /tick rate 的值是 TPS 不是倍数，发裸 N 会减速）+ 客户端 bot.setTickRate(N)（本地物理 50/N ms）。
// 设为模块级是因为 resumeBot 要在反思解冻后**补发** /tick rate ${20*N}（freeze→unfreeze 不恢复 rate，
// 否则反思后世界悄悄降回 1 倍速）。bot 实例每次 /start 重建，但 rate 跨 bot 生命周期保持。
// 详见 OPENKAGE_PATCHES.md。
let currentTickRate = 1;

/** 把 server + 客户端两侧都设到倍速 N。只在 bot 已 spawn（能 bot.chat）时调用。
 *  返回实际生效的 N（clamp 到 ≥1）。 */
function applyTickRate(n) {
    const rate = Math.max(1, Number(n) || 1);
    currentTickRate = rate;
    // ★ **服务器 /tick rate 的语义是 TPS（每秒 tick 数），默认 20，不是「倍数」**（见
    //   https://minecraft.wiki/w/Commands/tick）。`rate` 是用户旋钮「N 倍速」，故服务器要发的
    //   是 `20*rate` TPS 才等于 N 倍速（N=2 → /tick rate 40 → 40 TPS = 2 倍快）。
    //   旧代码发裸 `rate`（/tick rate 2 = 2 TPS = 慢 10 倍）——这正是炉子 200 刻烧 52s、
    //   世界与 bot 动作反向失步的根因（2026-08-21 查实）。
    const serverTps = 20 * rate;
    if (bot && typeof bot.setTickRate === "function") {
        bot.setTickRate(rate);  // 客户端：本地物理 50/N ms = 20N tick/s = N 倍速（与 server 同频）
    }
    if (bot) {
        bot.openkageTickRate = rate;  // 供 digging.js 缩放 waitTime/swingInterval（见其 patch）
        bot.chat(`/tick rate ${serverTps}`);  // 服务器：20*N TPS = N 倍速（昼夜/怪物/红石/烧炼）
    }
    console.log(`[tickrate] 已设 ${rate}x 倍速（server /tick rate ${serverTps} TPS + 客户端 setTickRate(${rate})）`);
    return rate;
}

// ★ 循环体注入 yield：防 LLM 写 while(true){asyncApi()} 不 await 的纯同步死循环锁死事件循环 →
//   keepalive 饿死断线。详见 lib/injectYield.js 的注释。单独成模块以便单测。
const { injectYieldIntoLoops } = require("./lib/injectYield");

const app = express();

app.use(bodyParser.json({ limit: "50mb" }));
app.use(bodyParser.urlencoded({ limit: "50mb", extended: false }));

app.post("/start", (req, res) => {
    if (bot) onDisconnect("Restarting bot");
    bot = null;
    console.log(req.body);
    bot = mineflayer.createBot({
        host: "localhost", // minecraft server ip
        port: req.body.port, // minecraft server port
        // 用户名由 Python 侧按 arm key 传入（给每个副脑独立身份，为多 Agent 联机协作做准备）。
        // 缺省回落 "bot" —— 模板 ops.json 里本就有这条。
        // ★ 换名字必须同步 ops.json（离线 UUID 由名字算出），否则 bot 静默失去 OP、
        //   所有斜杠指令无声失败。Python 侧的 MinecraftWorld.set_bot_name 已负责写入。
        username: req.body.username || "bot",
        disableChatSigning: true,
        checkTimeoutInterval: 60 * 60 * 1000,
    });
    bot.once("error", onConnectionFailed);

    // Event subscriptions
    bot.waitTicks = req.body.waitTicks;
    bot.globalTickCounter = 0;
    // ★ **放置埋点（2026-09-14 加）**：Python 侧经 /start body.placeLog 传日志路径，
    //   监听 mineflayer 的 blockPlaced 事件（放置成功且类型已变才 emit，见
    //   place_block.js:25-28）——所有放置路径（placeBlockSafe/placeItem/
    //   bot.placeBlock）最终都汇到 placeBlockWithOptions 并触发此事件，收口全覆盖。
    //   记 (arm=bot.username, block=新方块名, x/y/z, t) 到 JSONL，供论文 3D 建造还原
    //   （settlement 图只能证明定居，3D 图直接看有没有造结构化的房子）。
    //   未传 placeLog 时零行为（不影响现有 run/smoke）；写失败不抛（埋点不阻断放置）。
    bot.placeLogPath = req.body.placeLog || null;
    if (bot.placeLogPath) {
        bot.on("blockPlaced", (oldBlock, newBlock) => {
            try {
                fs.appendFileSync(bot.placeLogPath,
                    JSON.stringify({
                        arm: bot.username,
                        block: newBlock ? newBlock.name : "?",
                        x: newBlock ? newBlock.position.x : null,
                        y: newBlock ? newBlock.position.y : null,
                        z: newBlock ? newBlock.position.z : null,
                        t: Date.now(),
                    }) + "\n");
            } catch (_e) { /* 埋点失败不影响放置主流程 */ }
        });
    }
    // ★ **进食/血量埋点（2026-09-17 加）**：Python 侧经 /start body.eatLog 传日志路径，
    //   监听 mineflayer 的 `health` 事件（health.js: update_health 包触发，emit 前
    //   bot.health/food/foodSaturation 三字段已全更新好——没有独立的 food/eat 事件，
    //   只此一个事件源），记进食/回血/受伤/死亡时间线，供量化「生存」长期目标。
    //
    //   ★ **滤噪**（只记关键事件，不记饱和度自然衰减）：每次 health 事件比较 before/after，
    //     food↑ → trigger="eat"（进食优先级最高，进食常同时带 health↑，原因归 eat）；
    //     否则 health↑ → "heal"；否则 health↓ → "damage"；三者都不满足（仅饱和度衰减/无变化）
    //     → 不记。death 事件单独记一条 trigger="death"、died=true、health_after=0。
    //   ★ **before 缓存**：bot.health 在首个 update_health 包到达前为 undefined，首次事件
    //     只初始化 bot._eatPrev 不记（无 before 可比）。死亡重生后 health 回 20、缓存顺带刷新。
    //   ★ **对齐字段**（比 place_log 多，要对到「第 N 天第 N 刻」）：elapsedTime=
    //     bot.globalTickCounter（游戏刻，status.js observe 同源）、worldDay=bot.time.day、
    //     died。三字段与 status.js observe 同源，量化脚本可跨日志对齐。
    //   未传 eatLog 时零行为（不影响现有 run/smoke）；写失败不抛（埋点不阻断主流程）。
    //   每臂一文件 eat_log_<arm>.jsonl（单写者，结构性消除 FUSE 多进程并发 append 丢行——
    //   见 __init__.py 注释与记忆 FUSE place_log 事故）。
    bot.eatLogPath = req.body.eatLog || null;
    if (bot.eatLogPath) {
        bot._eatPrev = null;  // {health, food, sat}，首事件初始化
        bot.on("health", () => {
            try {
                const h = bot.health, f = bot.food, s = bot.foodSaturation;
                if (bot._eatPrev === null) { bot._eatPrev = {h, f, s}; return; }
                const p = bot._eatPrev;
                const trigger = _classifyEatEvent(p, {h, f, s});  // 滤噪判定（纯函数，见上方）
                bot._eatPrev = {h, f, s};
                if (!trigger) return;  // 仅饱和度衰减/无变化，滤掉
                fs.appendFileSync(bot.eatLogPath,
                    JSON.stringify({
                        arm: bot.username,
                        t: Date.now(),
                        elapsedTime: bot.globalTickCounter,
                        worldDay: (bot.time && typeof bot.time.day === "number") ? bot.time.day : null,
                        trigger,
                        died: false,
                        health_before: p.h, health_after: h,
                        food_before: p.f, food_after: f,
                        sat_before: p.s, sat_after: s,
                    }) + "\n");
            } catch (_e) { /* 埋点失败不影响主流程 */ }
        });
    }
    // ★ 观战相机探针的观战账号名（2026-09-15）：Python 侧经 /start body.spectatorName 传
    //   （FootageRecorder.spectator_name，形如 "spec_<port>"）。spectatorCam 采集器据此发
    //   /data get entity <名> Pos 探录像相机位置（附身=跟着 bot；脱附=停在脱附点），env 步末
    //   看门狗防脱附空镜。未传时采集器仍注入（observe 返回 null 坐标，Python 判「不判定」）。
    bot.spectatorName = req.body.spectatorName || null;
    // ★ OPENKAGE 倍速：读 /start 传入的 tick_rate（缺省 1=自然流速）。
    //   实际两侧设置（server /tick rate + 客户端 setTickRate）推迟到 spawn 后 applyTickRate，
    //   因为 bot.chat 要等 bot 进世界才能发。这里先记下，spawn 回调里用。
    const startTickRate = Math.max(1, Number(req.body.tick_rate) || 1);
    // 原语的「中止钩子」登记表。带自有定时器/循环的原语（exploreUntil）把自己的
    // cleanup 注册进来，供 abortInFlightActions() 在超时/暂停时真正掐断（见 exploreUntil.js）。
    bot.openkageAborts = new Set();

    // ★ **window_click 步内限流熔断**（2026-09-08，防「chest.deposit/withdraw 漏 await + close」
    //   触发的 window_click 包风暴把进程灌到 OOM 崩溃）。
    //   事故：LLM 手写 `chest.deposit(id,null,n)`（不 await）紧接 `chest.close()`，close 把
    //   bot.currentWindow=null，但 deposit 内部 transfer→transferOne 递归闭包仍持有旧 chest 窗口
    //   引用；1.21 下 clickWindow 的 waitForWindowUpdate 对 chest 类型立即 resolve（不等服务器回包）
    //   → 递归飞转、每轮发一个 window_click 包，实测 ≈1665 包/秒，几十秒涨到 8G 堆 OOM，整条 run
    //   报废（alice step273 掉线即此）。修法见 react.yaml「await 铁律」治本；这里是兜底——
    //   即便 LLM 仍写错，也不让进程崩。
    //   做法：wrap bot._client.write，单步 window_click 包数超阈值就抛带标记的错。抛错会同步
    //   穿透 clickWindow（write 在 await waitForWindowUpdate 之前）→ reject transfer 递归链 →
    //   打断死递归（光丢包不抛错，clickWindow 仍立即 resolve、递归继续空转烧 CPU）。抛出的 reject
    //   成 unhandledRejection 被顶层兜住；同时置 _openkageWindowClickFused 标志，供 evaluateCode 在
    //   步末把它翻成一条点名的 onError 反馈（"window_click 风暴，检查 deposit/withdraw 是否漏
    //   await"），而不是无声 OOM。
    //   阈值 800：正常用法的 window_click 上限远低于此——实测 craft planks×10=57 包、×44（历史
    //   最大单步 craft）≈248 包；deposit/withdraw 即便 count=999 也按 stack 左 click 批搬（999 个
    //   ≈16 次 click，不是 999 次）。800 留 3 倍余量覆盖 craft×44 + 多次存取叠加（要破 800 得
    //   craft×143，历史从未出现且 timeout_ticks 早用满）。风暴是 1665 包/秒、几万包才 OOM，800 包在
    //   0.5 秒触发、远在内存爆之前。计数器在 /step 首重置（见 evaluateCode 开头）。
    //   ★ 判据用「单步累计量」而非「速率」：正常 craft 因 waitForWindowUpdate 对合成台/箱子立即
    //   resolve，速率本就 ~679/秒、峰值 50ms 窗甚至 2540/秒（实测 craft sticks×20），比风暴 1665/秒
    //   还高——速率判会误伤正常 craft，累计量才分得开（正常 craft 248 包就停、count 不会涨到 800）。
    bot.openkageWindowClickCount = 0;
    bot._openkageWindowClickFused = false;
    const _OPENKAGE_WINDOW_CLICK_LIMIT = 800;
    const _origClientWrite = bot._client.write.bind(bot._client);
    bot._client.write = (name, params) => {
        if (name === "window_click") {
            bot.openkageWindowClickCount++;
            if (bot.openkageWindowClickCount > _OPENKAGE_WINDOW_CLICK_LIMIT) {
                bot._openkageWindowClickFused = true;
                const err = new Error(
                    "window_click 包数超限（" + _OPENKAGE_WINDOW_CLICK_LIMIT +
                    "）——疑似 chest.deposit/withdraw 漏了 await 就 close()，触发发包风暴。" +
                    "存取容器物品必须 await 完成后再 close()。"
                );
                err.openkageWindowClickFused = true;
                throw err;
            }
        }
        return _origClientWrite(name, params);
    };

    // ★ **heap 峰值采样器**（2026-09-10，与 window_click wrap 同区注册）。常驻 physicsTick 监听器：
    //   每 _HEAP_SAMPLE_INTERVAL_TICKS 个 physicsTick 采一次 process.memoryUsage().heapUsed，维护本步
    //   峰值 bot.openkageHeapPeakMB；到 _OPENKAGE_HEAP_LIMIT_MB*_openkageHeapWarnRatio() 置告警标志、
    //   打 [heap告警] 行。步首在 evaluateCode 重置峰值/标志（见那里），步末据标志注入 onError 反馈。
    //   ★ **physicsTick 是 mineflayer 真实事件**（与 _startJumpStallMonitor 同源，见那里的注释），
    //     /tick freeze 期间不触发，但崩溃发生在非 freeze 的代码执行期，覆盖得到；按 tick 采样还对
    //     tick_rate 免疫（rate 只改两次触发间的墙钟间隔，不改事件本身）。
    //   ★ **process 在生成代码里被遮蔽**（见 evaluateCode 的 _procExitMsg），但那是 eval 字符串作用域
    //     内的局部遮蔽，不影响 index.js 自身基建代码里的全局 process——这里读 process.memoryUsage() 正常。
    //   开销：回调里 ++counter < 20 早 return，每秒一次 memoryUsage() 调用可忽略。
    bot.openkageHeapPeakMB = 0;
    bot._openkageHeapWarned = false;
    {
        let _heapTickCounter = 0;
        bot.on("physicsTick", () => {
            if (++_heapTickCounter < _HEAP_SAMPLE_INTERVAL_TICKS) return;
            _heapTickCounter = 0;
            const heapMB = process.memoryUsage().heapUsed / (1024 * 1024);
            if (heapMB > bot.openkageHeapPeakMB) bot.openkageHeapPeakMB = heapMB;
            if (!bot._openkageHeapWarned &&
                heapMB >= _OPENKAGE_HEAP_LIMIT_MB * _openkageHeapWarnRatio()) {
                bot._openkageHeapWarned = true;
                console.log(`[heap告警] heapUsed=${heapMB.toFixed(0)}MB 已达上限` +
                            `${_OPENKAGE_HEAP_LIMIT_MB}MB的` +
                            `${Math.round(_openkageHeapWarnRatio() * 100)}%` +
                            `（峰值采样，physicsTick）`);
            }
        });
    }

    // ★ 0930 修（carol 骑船事故）：kicked handler 必须闭包捕获**本实例**。此前 onDisconnect
    //   操作全局 bot——上一只 bot（僵尸）被服务器判 duplicate_login 踢掉时，它的 handler
    //   end() 的是全局变量里**刚登录成功的新 bot**（误杀继任者）→ 新 /start 永远无响应
    //   → Python 自愈重启进程 → 触发条件（playerdata 骑船）还在 → 死循环 65 轮。
    //   传参给 onDisconnect（见其第二参数），让它只处理「死的那只」、不动活着的新连接。
    {
        const _self = bot;
        bot.on("kicked", (msg) => onDisconnect(msg, _self));
        // ★ 1009 修（ds13 停摆事故）：end 事件同样挂 onDisconnect。此前只有 kicked 挂了——
        //   ds13 里 bot 被 server 以 keepalive Timed out 踢掉（垫柱技能 Vec3=null 报错风暴
        //   饿死 keepalive），但 disconnect 包在事件循环积压下未被处理成 kicked，socket
        //   半死（写报 EPIPE、读零包）而 `bot` 变量始终非 null → /step /pause /resume 全部
        //   照常 200、timeTicks 恒读断线前缓存 → Python 侧护栏只见「世界时间停摆」假象，
        //   自愈（依赖 /step 失败触发）永不启动，单臂空转 6 小时才 abort。
        //   mineflayer 断开有 kicked（服务器主动踢）与 end（socket 断/本地 end()）两条路，
        //   两条都收拾现场（onDisconnect 幂等：bot.end() 已断再调无害，dead===bot 才置 null）。
        //   仍是闭包捕获本实例 + 传 dead 参数，防僵尸串扰（同 kicked 的 0930 事故教训）。
        bot.on("end", (reason) => onDisconnect(
            `[end] 连接断开${reason ? "（" + reason + "）" : ""}`, _self));
    }

    // mounting will cause physicsTick to stop
    bot.on("mount", () => {
        // ★ 0930 修（carol 骑船事故）：登录重放 attach_entity 包时 vehicle 可能未就绪——
        //   无条件 dismount 会同步抛 `dismount: not mounted`（entities.js 的 else 分支），
        //   登录期该错误路由到 onConnectionFailed（只置 null 不 end socket）→ MC server 上
        //   留僵尸会话 → 下一轮 /start 新登录被判 duplicate_login → 自愈死循环 65 轮。
        //   守卫：没真骑上就忽略这个残缺的 mount 事件（真骑上时 vehicle 非 null，照常下）。
        if (bot.vehicle) bot.dismount();
    });

    bot.once("spawn", async () => {
        bot.removeListener("error", onConnectionFailed);
        let itemTicks = 1;
        if (req.body.reset === "hard") {
            bot.chat("/clear @s");
            bot.chat("/kill @s");
            // 成就随存档按玩家累积、**不随库存清空而重置**（已获得的成就再 grant 只回
            // `Couldn't grant ... as they already have it`，且**不再播报**）。成就统计现由
            // advancementPacket.js 监听协议包维护（2026-09-07 起，onSystem 成就侧已即弃）——
            // 不清零的话第 2 轮起 packet 里仍残留上一轮的 completed，bot 重新挖到石头也不会
            // 产生新成就 diff → 轮次间指标不可比。既然 reset=hard 的语义就是「世界/库存 fresh，
            // 只有记忆跨轮携带」，成就同属「上一轮的成果」，一并清掉才自洽。
            // （revoke 触发的 ClientboundAdvancementsPacket 会让 advancementPacket.js 清空 completed：
            // reset 包走 clear、progressMapping 走 else 分支 delete，两种都正确清零。）
            bot.chat("/advancement revoke @s everything");
            itemTicks += 1;
            const inventory = req.body.inventory ? req.body.inventory : {};
            const equipment = req.body.equipment
                ? req.body.equipment
                : [null, null, null, null, null, null];
            for (let key in inventory) {
                bot.chat(`/give @s minecraft:${key} ${inventory[key]}`);
                itemTicks += 1;
            }
            const equipmentNames = [
                "armor.head",
                "armor.chest",
                "armor.legs",
                "armor.feet",
                "weapon.mainhand",
                "weapon.offhand",
            ];
            for (let i = 0; i < 6; i++) {
                if (i === 4) continue;
                if (equipment[i]) {
                    bot.chat(
                        `/item replace entity @s ${equipmentNames[i]} with minecraft:${equipment[i]}`
                    );
                    itemTicks += 1;
                }
            }
        }

        if (req.body.position) {
            const p = req.body.position;
            // y 缺省（OpenKAGE的随机出生点协议只给 x/z）→ 用 `/spreadplayers x z 0 1 under 320 false @s`：
            // 它把目标放到该 (x,z) 柱体的**地表最高可站方块**上（`under <maxY>` 语义），
            // 不用自己扫地形高度、也不会把 bot 塞进石头或悬在半空。
            // spreadDistance=0 + maxRange=1 → 实际不散开，只做"落到地面"这件事。
            //
            // ⚠️ 两个前提，都由 Python 侧的 spawn 选点循环保证（见 env.py 的 try_spawn）：
            // ① 该区块**已生成** —— 否则等不到 tick，/start 会卡死（先 forceload 预热）；
            // ② 该柱体地表**可站人** —— 水面/岩浆上 spreadplayers 会**静默失败**
            //    （失败只回给命令发送者、不进服务器日志，表现为 bot 停在原地而 /start 仍返回成功），
            //    故 Python 侧会校验落点、不合格就换一个随机点重试。
            if (p.y === null || p.y === undefined) {
                bot.chat(`/spreadplayers ${p.x} ${p.z} 0 1 under 320 false @s`);
            } else {
                bot.chat(`/tp @s ${p.x} ${p.y} ${p.z}`);
            }
        }

        const { pathfinder } = require("mineflayer-pathfinder");
        const tool = require("mineflayer-tool").plugin;
        const collectBlock = require("mineflayer-collectblock").plugin;
        bot.loadPlugin(pathfinder);
        bot.loadPlugin(tool);
        bot.loadPlugin(collectBlock);
        // ★ **pvp / hawkEye 必须加载**：`control_primitives/` 里的 **killMob / shoot /
        //   waitForMobRemoved 都在调 `bot.pvp.*` 与 `bot.hawkEye.*`**，而 prompts/system.md
        //   还明确把它们列为"可以直接调用的辅助函数"，缺了这两个插件调用会直接抛错
        //   （`Cannot read properties of undefined (reading 'attack')`）。
        //   夜间被怪围是本域最常见的死亡原因，没有反击能力等于白送分。
        //   `minecrafthawkeye` 导出的是 `.default`（`.plugin` 为 undefined），所以这里显式取
        //   `.default`；mineflayer-pvp 则正常导出 `.plugin`。
        //   另：pvp 内部监听弃用事件 `physicTick`，会每次刷 console.warn 占 LLM 的 onLog 配额，
        //   已按 README 惯例给 node_modules/mineflayer-pvp/lib/PVP.js 打补丁改成 physicsTick。
        bot.loadPlugin(require("mineflayer-pvp").plugin);
        bot.loadPlugin(require("minecrafthawkeye").default);

        // ---- pathfinder A* 的两个时间旋钮（含义完全不同，别混）----
        //
        // · tickTimeout  = **单片最多阻塞事件循环多久**（astar.js:70 到点就 return 'partial'）。
        //   这才是掉线风险的直接旋钮：服务器 30s 收不到 keep_alive 会踢人，而
        //   keep_alive 是 minecraft-protocol/src/client/keepalive.js 里的**被动响应**
        //   （收到包才回、30s 没回才踢），跟 physicsTick 没有任何关系。
        //   40ms 的单片阻塞离那道 30s 门槛差三个数量级 → 恢复库默认值即可。
        //   ⚠️ 旧注释说「减半让 keep_alive 更有机会回」是**错的因果**（2026-08-16 核实后订正）：
        //      减半不影响 keep_alive，只是把 A* 的可用算力砍了一半。
        //
        // · thinkTimeout = **一次搜索的总墙钟**（astar.js:73，startTime 在 AStar 构造函数里设）。
        //   ★ 关键：它是墙钟、不是算力。原先 A* 只在 physicsTick(50ms 一次) 里续算一片
        //     20ms → **占空比仅 28%**，3000ms 墙钟里只有 ~850ms 真在算。
        //     下面 `__openkagePathfinderGuarded` 那段把续算改成 setImmediate 驱动后占空比 ≈100%，
        //     于是这个值才真正等于「算力预算」。
        //
        // 实测（200 格目标，按绕行规模；绕行才是代价来源，距离不是 —— 空旷地 512 格只要 50ms）：
        //   绕行   tick=20/physicsTick(旧)   tick=40/physicsTick   tick=40/setImmediate(现在)
        //   300    success                   success               success
        //   400    timeout                   success               success
        //   500    timeout                   timeout               success
        //   600    timeout                   timeout               success
        //   700    timeout                   timeout               timeout（降级走各 primitive 的 catch）
        bot.pathfinder.thinkTimeout = 5000;
        bot.pathfinder.tickTimeout = 40;

        // bot.collectBlock.movements.digCost = 0;
        // bot.collectBlock.movements.placeCost = 0;

        // 观战相机探针（2026-09-15）：bot 侧周期发 /data get entity <spec> Pos（服务器权威，
        // 附身时观战者坐标每刻被服务器写成 bot 位置、脱附后停在脱附点——真服三态实测），env
        // 步末看门狗据此触发 resync_spectate。观战名从 bot.spectatorName 读（/start body 传入，
        // base.js inject 对每项 new obs(bot) —— 采集器构造器里自己取）。
        obs.inject(bot, [
            OnChat,
            OnError,
            OnLog,
            OnSystem,
            OnSkill,
            Voxels,
            NearbyGrid,
            Vision,
            Status,
            Inventory,
            Chests,
            Scoreboard,
            AdvancementPacket,
            SpectatorCam,
            // 未纳入观察插件列表的两类，都是「采了、传了、从没被 Python 消费」：
            //   · OnSave       —— Voyager 的里程碑事件钩子；OpenKAGE用 tier 高水位 + 服务器成就播报
            //                     判进度（事实源更可靠）。bot.save() 已退化为 no-op，见 skillLoader.js。
            //   · BlockRecords —— 累积「见过但不在库存的方块种类」。**信息本就冗余**：白天的对话是
            //                     连续累积的（只在黎明 reset_day 清），前面每步观察都还在上下文里，
            //                     LLM 自己往回看就有；而且它的 reset() 从没被调用过，Set 会跨天
            //                     无限增长，真渲染出去反而是往上下文灌垃圾。
            // 若要恢复，记得**同时**在 env.py 的 render_observation 里渲染它 —— 否则又是死字段。
        ]);
        skills.inject(bot);

        if (req.body.spread) {
            bot.chat(`/spreadplayers ~ ~ 0 300 under 80 false @s`);
            // 同样走 waitTicksSafe：这里也在 /start 的响应路径上，挂住就是 /start 超时
            await waitTicksSafe(bot, bot.waitTicks);
        }

        // 发响应前等 tick：用 waitTicksSafe，bot 断流时也能返回（见其注释）
        await waitTicksSafe(bot, bot.waitTicks * itemTicks);

        // ★ **keep_inventory 必须在 res.json 之前设好并等生效**（2026-08-23 修，见 memory
        //   openkage_viewer_checkpoint_snapshots 那批 bug 经验的根因之一）：
        //   此前 gamerule 在 res.json **之后**用 `bot.chat` 发（fire-and-forget、无 await/无确认），
        //   于是存在窗口期：/start 响应已返回、Python 已开始发 /step 让 bot 动起来，但服务器还没
        //   处理完 keep_inventory 命令 —— 这期间任何死亡都按 keep_inventory=false（默认/旧值）掉物品。
        //   实测 carol step280 被 Creeper 炸死、iron_leggings 1→0 消失（_KEEP_INVENTORY=True 下本不该掉），
        //   agent 把这个 bug 当成"死亡掉落全部物品"记进经验层 death_drops_all_items。
        //   修法：把 gamerule 提前到 res.json 之前发，并等几 tick 让服务器处理完命令再返回。
        //   gamerule 命令是聊天指令、服务器 1 tick 内即处理，5 tick 足够覆盖往返；2 倍速下也只需
        //   ~125ms 墙钟。等不够比等过头危险（等不够=窗口期死亡掉物，等过头只是 /start 慢几 tick）。
        //   注：gamerule 持久化在存档里，但「本次 /start 首次设」与「存档被 regenerate 过回默认值」
        //   两种情形都依赖这一次显式设置及时生效，故不能省等待。
        // ★ MC 1.21.11 把 gamerule 全部改成 snake_case：doDaylightCycle → advance_time
        //   （原来的 `/gamerule doDaylightCycle true` 一直静默失败，服务器回
        //   `Incorrect argument for command`）。同批改名的还有 doTileDrops → block_drops、
        //   keepInventory → keep_inventory。改名前的写法在本服 100% 无效，勿回退。
        bot.chat("/gamerule advance_time true");
        // **显式设 keep_inventory true**（2026-08-22 用户定）：死亡后**不掉落物品**、带装备原地重生。
        //   此前是 false（死亡掉落全部物品），理由是「死亡有真实代价才撑得起记忆规避重复死亡的主张」。
        //   用户改为 true 的考量：减少「跑回死亡点捡装备」的 path_len 往返噪声 + 减少死亡后因丢光
        //   装备的二次死亡，让探索/建造指标更干净。代价：死亡代价降低，死亡率可能上升（不怕死），
        //   故「累计死亡次数」作为生存指标时这一点要在分析里注明（keep_inventory=true 下的死亡）。
        // 为何显式发而不是"什么都不发"：gamerule **持久化在世界存档里**，谁手动改过就一直生效，
        // 世界存档不在版本管理内 → 不发命令等于把实验前提交给一个不可见的外部状态。显式声明后
        // 每次 /start 都把它拉回已知值。
        bot.chat("/gamerule keep_inventory true");
        // ★ 建立 MC 原生记分板记分项（交叉校验用，2026-08-26）：6 个只读准则由服务器按游戏事件
        //   自动更新（死亡/杀怪/XP/血/饱食/护甲），是独立于 env 自统计的第二权威源。env.py 每步从
        //   observe 的 scoreboard 字段读快照、落盘与自统计值并排留存（见 discoveries["scoreboard"]）。
        //   ⚠️ **必须 setdisplay**：客户端默认只收到被显示到某 slot 的 objective 的 score 包；不设
        //   bot.scoreboards 里不会有该项分数（建了 objective 但收不到 score 更新）。用 list slot
        //   干扰最小（侧边栏 sidebar 会占屏），Python 侧从 bot.scoreboards 取值不依赖 slot。
        //   已存在同名 objective 时服务器回 `already exists`（无害，onSystem.js 过滤 commands.* 回显，
        //   不污染死亡/成就统计）；不判存在以省一次往返——重复 /start 续跑时这是常态。
        //   准则名取 MC 1.21 camelCase（与 keep_inventory 同批命名规则）。
        for (const [obj, crit] of [
            ["oct_death", "deathCount"],
            ["oct_kill", "totalKillCount"],
            ["oct_xp", "xp"],
            ["oct_health", "health"],
            ["oct_food", "food"],
            ["oct_armor", "armor"],
        ]) {
            bot.chat(`/scoreboard objectives add ${obj} ${crit}`);
            bot.chat(`/scoreboard objectives setdisplay ${obj} list`);
        }
        // ★ OPENKAGE 倍速：两侧同步设到 startTickRate（server /tick rate + 客户端 setTickRate）。
        //   必须在 advance_time true 之后：/tick rate 改的是 TPS，advance_time=true 才让昼夜按 TPS
        //   流转（二者配合才「一天从 20min 压到 20/N min」）。
        //   仅当 startTickRate>1 才发，省一次命令往返；rate=1 时仍记入 currentTickRate 供 resumeBot。
        if (startTickRate > 1) {
            applyTickRate(startTickRate);
        } else {
            currentTickRate = 1;
        }
        // 等 gamerule 命令在服务器处理完（见上 keep_inventory 注释）。放 res.json 前，确保返回时
        // keep_inventory 已生效、bot 此后任何死亡都不再掉物。waitTicksSafe：bot 断流时不无限挂。
        await waitTicksSafe(bot, 5);
        if (bot.openkageLog) console.log("[start] keep_inventory=true 已设（advance_time 同）");
        res.json(bot.observe());

        initCounter(bot);
    });

    // 死亡处理：mineflayer 默认 options.respawn=true，死后自动重生，无需手动 respawn。
    // 这里监听 death 只为置标记（供 /step 死亡熔断，兜底 health 轮询）——真实致死连续掉血时 death 会 fire。
    bot.on("death", () => {
        bot.openkageDied = true;
        console.log("[bot death] auto-respawn by mineflayer");
        // ★ 进食/血量埋点：死亡单独记一条 trigger="death"（独立权威记录，不依赖 health=0 推断，
        //   串进同一 eat_log_<arm>.jsonl）。health_after 取死亡瞬间 bot.health（health.js 先
        //   emit health(h=0) 再 emit death，此处 bot.health 应为 0；取不到则记 null）。
        //   before 用 _eatPrev 缓存（致死那串 damage 事件最后一次的 after），无缓存则同 after。
        if (bot.eatLogPath) {
            try {
                const h = (typeof bot.health === "number") ? bot.health : 0;
                const p = bot._eatPrev || {h, f: bot.food, s: bot.foodSaturation};
                fs.appendFileSync(bot.eatLogPath,
                    JSON.stringify({
                        arm: bot.username,
                        t: Date.now(),
                        elapsedTime: bot.globalTickCounter,
                        worldDay: (bot.time && typeof bot.time.day === "number") ? bot.time.day : null,
                        trigger: "death",
                        died: true,
                        health_before: p.h, health_after: h,
                        food_before: p.f, food_after: bot.food,
                        sat_before: p.s, sat_after: bot.foodSaturation,
                    }) + "\n");
            } catch (_e) { /* 埋点失败不影响死亡流程 */ }
        }
    });

    function onConnectionFailed(e) {
        console.log(e);
        // ★ 0930 修（carol 骑船事故）：登录期失败也必须 end() 关 socket，与 onDisconnect 对齐。
        //   此前这里是全文件唯一「置 null 但不关连接」的断开路径——socket 留在 MC server 上
        //   成僵尸会话（服务器视角 Carol 还在线），下一轮 /start 的新登录把它判
        //   duplicate_login 踢掉，而僵尸注册的 kicked handler 又误杀新连接 → 自愈死循环
        //   65 轮（~95s/轮 = START_TIMEOUT 90s + 开销），直到人工 TERM。
        //   end() 幂等（已断开时再调无害）；bot.end 自身可能抛（半初始化状态），吞掉。
        try {
            if (bot) bot.end();
        } catch (_e) { /* 已断开/未完成初始化，无需处理 */ }
        bot = null;
        res.status(400).json({ error: e });
    }
    // dead：本次断开的 bot 实例（★ 0930 加）。kicked 路径传捕获的 _self，/start 头部的
    //   「Restarting bot」调用不传（默认=当前全局，那只本来就占着全局）。绝不能无条件操作
    //   全局 bot：僵尸被踢时全局可能已换上健康新连接，误 end 它就是 carol 事故的反杀一环。
    function onDisconnect(message, dead = bot) {
        if (dead.viewer) {
            dead.viewer.close();
        }
        // ★ **断线前清掉 pathfinder 的残留 goal**（2026-08-21 修，对应 heal 死循环事故）：
        //   `bot.end()` 会断 socket 但**不**清 pathfinder 状态——而 monitorMovement 挂在
        //   physicsTick 定时器上、持有的 goal 对象在 end() 之后仍存活。当本断线由 `goto` 的
        //   `Took to long` reject 引发时，那个 goal 每 tick 还在重新问路、继续抛错；重连（/start
        //   重建 bot）后，若旧 bot 对象的定时器/事件没被 GC 干净，旧 goal 的异常会串进新连接的
        //   生命周期，形成「reject → 断线 → 重连 → 旧 goal 继续抛 → 又 reject」的死循环。
        //   在 end() 前 setGoal(null) 主动清掉，掐断这条串扰。best-effort：pathfinder 未就绪
        //   或已 stop 时静默放过，不影响断线本身。
        try {
            if (dead.pathfinder) dead.pathfinder.setGoal(null);
        } catch (e) {
            /* 清 goal 失败不影响断线流程 */
        }
        dead.end();
        console.log(message);
        // 只有「死的这只就是当前全局」才清全局——死的若是上一只（僵尸），全局里是刚接上的
        // 新连接，绝不能跟着置 null（那是另一半事故现场）。
        if (dead === bot) {
            bot = null;
        }
    }
});

app.post("/step", async (req, res) => {
    // import useful package
    let response_sent = false;
    let _consoleReleased = false;
    // console 劫持的解除函数（由 captureConsole 赋值）；两条返回路径都要解，否则 console 永久被劫持。
    let releaseConsole = () => {};
    function releaseConsoleOnce() {
        if (_consoleReleased) return;
        _consoleReleased = true;
        releaseConsole();
        if (bot && bot.openkageLog) bot.openkageLog.flush();
        // onChat（原语反馈）同样步末 flush：它不再每条消息发一次事件（那等于每条一份全量快照），
        // 攒在缓冲里，必须在 bot.observe() 之前发帧，否则本步反馈全丢。见 onChat.js。
        if (bot && bot.openkageChat) bot.openkageChat.flush();
        // onSystem（服务器死亡/成就播报）也在步末 flush：它跨步累积（含两步之间到达的死亡），
        // 必须在 bot.observe() 之前发帧，否则本步回传里看不到。见 onSystem.js 的 flush 注释。
        if (bot && bot.openkageSystem) bot.openkageSystem.flush();
        // onSkill（技能调用成败埋点）步末 flush：代理 push 进缓冲的结构化事件，步首已 reset。
        // 必须在 bot.observe() 之前发帧，否则本步回传里看不到 onSkill 帧。见 onSkill.js。
        if (bot && bot.openkageSkill) bot.openkageSkill.flush();
    }
    function otherError(err) {
        // 坏 pathfinder goal（每 physics tick 重抛，见文件末尾 clearBadGoal 注释）：清掉 goal
        // 即止，**不能**当成本步失败提前返回 —— 否则一次坏 goal 就把这一步的响应打断，且后续
        // 每个 tick 都再来一遍。清完让生成代码继续跑到它自己的结局（成功/超时/真报错）。
        if (clearBadGoal(err)) return;
        // ★ **bot 已为 null 时走兜底**（2026-09-10 修，由 lightAreaWithTorches 复现坐实）：
        //   bot 被 server 踢/断连 → onDisconnect 执行 bot.end() 后置 `bot = null`（index.js:597）。
        //   从此刻到 Python 侧自愈 /start 重建 bot 之间，bot 就是 null。而这期间 pathfinder 的
        //   monitorMovement（挂 physicsTick 定时器、持有的 goal 在 end() 后仍存活，见 onDisconnect
        //   注释）每 tick 仍可能抛 Took-to-long/bad goal → 进本 uncaughtException 处理器。
        //   旧代码直接 `bot.emit(...)` → `Cannot read properties of null (reading 'emit')` →
        //   **处理器内部抛的 TypeError 走 _fatalException 直接杀进程**（Node 不会二次调用
        //   uncaughtException 处理器）。于是「本可自愈的断连」升级成「index.js 进程硬崩」，
        //   Python 侧只能走更重的 _heal_and_restart 整个重启 index.js（0910 run 末段 25 次秒级
        //   ConnectionError 自愈里，这条路径让若干次断连变成进程崩）。onConnectionFailed/onDisconnect
        //   都会置 bot=null，故这里必须与它们一致地判空，绝不碰已 null 的 bot。
        if (!bot) {
            console.log("Uncaught Error (bot 已断开/null，无法 emit，走兜底)");
            console.log(err && err.stack || err);
            releaseConsoleOnce();
            if (!response_sent) {
                response_sent = true;
                res.status(400).json({ error: "bot disconnected during step: " + (err && err.message || err) });
            }
            return;
        }
        console.log("Uncaught Error");
        releaseConsoleOnce();  // 异常路径也要解劫持 + flush，否则 log 丢且 console 一直被占
        bot.emit("error", handleError(err));
        // 走 waitTicksSafe：这是**异常路径的唯一出口**，bot 断流时若用裸 waitForTicks
        // 就永远发不出响应（挂到 /step 超时），而这条路径本身已经是在处理错误了。
        waitTicksSafe(bot, bot.waitTicks).then(() => {
            if (!response_sent) {
                response_sent = true;
                res.json(bot.observe());
            }
        });
    }

    process.on("uncaughtException", otherError);

    const mcData = require("minecraft-data")(bot.version);
    mcData.itemsByName["leather_cap"] = mcData.itemsByName["leather_helmet"];
    mcData.itemsByName["leather_tunic"] =
        mcData.itemsByName["leather_chestplate"];
    mcData.itemsByName["leather_pants"] =
        mcData.itemsByName["leather_leggings"];
    mcData.itemsByName["leather_boots"] = mcData.itemsByName["leather_boots"];
    mcData.itemsByName["lapis_lazuli_ore"] = mcData.itemsByName["lapis_ore"];
    mcData.blocksByName["lapis_lazuli_ore"] = mcData.blocksByName["lapis_ore"];
    const {
        Movements,
        goals: {
            Goal,
            GoalBlock,
            GoalNear,
            GoalXZ,
            GoalNearXZ,
            GoalY,
            GoalGetToBlock,
            GoalLookAtBlock,
            GoalBreakBlock,
            GoalCompositeAny,
            GoalCompositeAll,
            GoalInvert,
            GoalFollow,
            GoalPlaceBlock,
        },
        pathfinder,
        Move,
        ComputedPath,
        PartiallyComputedPath,
        XZCoordinates,
        XYZCoordinates,
        SafeBlock,
        GoalPlaceBlockOptions,
    } = require("mineflayer-pathfinder");
    const { Vec3 } = require("vec3");

    // Set up pathfinder
    const movements = new Movements(bot, mcData);
    // ★ 脚手架方块：从上游硬编码的 2 种放宽到「库存里任何完整实心方块」。
    //   上游 lib/movements.js:75-77 只 push 了 dirt 与 cobblestone，于是 bot 身上只有
    //   stone / deepslate / oak_planks 时**连 1×1 塔都垫不起来**（getScaffoldingItem() 返回
    //   null → resetPath('no_scaffolding_blocks')），表现为"寻路说走不到"而不是"缺料"，
    //   诊断时极易归因到地形。而这些方块垫脚在游戏里完全合法。
    //   过滤口径按完整碰撞箱判定（bb==="block"
    //   且非重力/交互方块）—— **两处必须同口径**，否则"手动垫得上去、寻路却说没料"会更难查。
    //   排除易塌/有副作用的方块：sand/gravel 会塌，tnt/anvil 等有副作用。
    {
        const _EXCLUDE = new Set([
            "sand", "red_sand", "gravel", "anvil", "chipped_anvil", "damaged_anvil",
            "dragon_egg", "pointed_dripstone", "scaffolding", "tnt", "slime_block",
            "honey_block", "powder_snow", "magma_block", "ice", "frosted_ice",
            "soul_sand", "mud", "bamboo_block",
        ]);
        const ids = [];
        for (const blk of mcData.blocksArray) {
            if (blk.boundingBox !== "block") continue;
            if (_EXCLUDE.has(blk.name)) continue;
            const it = mcData.itemsByName[blk.name];   // 必须有对应**物品**才能放置
            if (it) ids.push(it.id);
        }
        // 保序：dirt / cobblestone 仍排最前（上游默认，也是最该先消耗的廉价料），
        // 其余按 mcData 顺序追加。getScaffoldingItem 按本数组顺序找第一个持有的物品。
        const head = ["dirt", "cobblestone"]
            .map((n) => mcData.itemsByName[n] && mcData.itemsByName[n].id)
            .filter((v) => v !== undefined);
        movements.scafoldingBlocks = [
            ...head,
            ...ids.filter((id) => !head.includes(id)),
        ];

        // ── 顺带把这两个方法改成**库存驱动**（否则放宽名单会拖慢热路径）────────────────
        // 上游两个实现都是 `for (id of scafoldingBlocks) for (item of inventory)`，即
        // **O(名单 × 36)**。名单从 2 变成几百后单次耗时仍在毫秒级 —— 而 countScaffoldingItems
        // 每次寻路都调（lib 的 index.js:85）、getScaffoldingItem 在垫塔时每 tick 都调。
        // 单次耗时谈不上致命，但本仓已经栽过两次「每 tick 同步扫描 → 事件循环被占死 →
        // 30s 收不到 keep_alive 判掉线」（见 mineBlock 顶部注释），没必要留这个隐患。
        // 改成遍历**库存的 36 格**、用 Set/Map 查名单 → O(36)，与名单大小无关。
        // 这两个方法是 scafoldingBlocks 的**唯一消费者**（已 grep 确认：movements.js:143/154），
        // 所以覆盖它们不会漏掉别的调用点；名单本身仍留着，作为口径的单一来源。
        const _scafSet = new Set(movements.scafoldingBlocks);
        const _scafRank = new Map(
            movements.scafoldingBlocks.map((id, i) => [id, i])
        );
        movements.countScaffoldingItems = function () {
            let count = 0;
            for (const item of this.bot.inventory.items()) {
                if (_scafSet.has(item.type)) count += item.count;
            }
            return count;
        };
        movements.getScaffoldingItem = function () {
            // 保持"名单靠前的优先用"这个语义（dirt/cobblestone 先消耗），只是换了遍历方向
            let best = null;
            let bestRank = Infinity;
            for (const item of this.bot.inventory.items()) {
                const r = _scafRank.get(item.type);
                if (r !== undefined && r < bestRank) {
                    bestRank = r;
                    best = item;
                }
            }
            return best;
        };
    }
    bot.pathfinder.setMovements(movements);

    // ── ★ 目标坐标校验：非有限数的 goal 会让 A* 永不终止 → 进程 OOM 崩溃 ────────────────
    //
    // 触发方式：LLM 把 `bot.findBlocks()` 的返回当成 Block 对象用（它返回的是 **Vec3 数组**），
    // 取 `.position.x` 得到 undefined → 坐标变 NaN，然后在循环里反复
    // `goto(new GoalNear(NaN,NaN,NaN,3))`。
    // GoalNear 拿到 NaN 后：`isEnd()` **永远 false**、`heuristic()` 返回 NaN —— A* 既判不出
    // 「到达」、也无法用 f 值剪枝，open list 无限膨胀 → `FATAL ERROR: JavaScript heap out
    // of memory`，node 进程直接死。
    //
    // 后果在 Python 侧极难定位：只表现为 `ConnectionError` / `RemoteDisconnected` + 自愈重连
    // （进程死了而非网络问题）。更隐蔽的是**自愈成功时 Python 侧完全无痕**：崩溃发生在 Node
    // 侧、被自愈悄悄接住，ConnectionError 计数看不出崩溃发生过。此前多轮"莫名 ConnectionError"
    // 很可能都是它。
    //
    // 故在**入口**校验：坐标非有限数就直接抛错告诉 LLM 它算错了，而不是让 A* 把进程拖死。
    // 包 goto 与 setGoal 两个入口 —— LLM 两个都会用，只包一个必然漏。
    //
    // ★ **只装一次**：这段代码在 `/step` 处理体内，每步都会跑一遍。
    //   `_origGoto = bot.pathfinder.goto.bind(...)` 捕获的是**上一步已经包装过的那个函数**，
    //   于是每步多套一层，走 N 步就 N 层嵌套校验（不致命，但按步数线性增长的无用开销＋闭包链）。
    //   标志挂在 bot 上而非模块级：/start 会整个重建 bot，新 bot 自然重新装一次，语义正好。
    if (!bot.__openkagePathfinderGuarded) {
        bot.__openkagePathfinderGuarded = true;
        const _origGoto = bot.pathfinder.goto.bind(bot.pathfinder);
        const _origSetGoal = bot.pathfinder.setGoal.bind(bot.pathfinder);
        // ── ★ 非阻塞 A*：把「续算」从 physicsTick 改成 setImmediate 驱动 ──────────────
        //
        // 问题（2026-08-16 实测）：`thinkTimeout` 是**总墙钟**（astar.js:73，startTime 在
        // AStar 构造函数里设），而库只在 `monitorMovement` 里、**每个 physicsTick（50ms）
        // 续算一片 tickTimeout**（pathfinder/index.js:442）→ 占空比只有 tickTimeout/50。
        // 于是「5s 预算」实际只算了 ~2s，绕行稍大的目标必然 timeout。
        //
        // 解法：`goto` 先用 `getPathFromTo`（**generator，纯计算、对 bot 无副作用**）把整条
        // 路径算出来，两片之间用 `setImmediate` 让出事件循环 —— 单片阻塞仍只有 tickTimeout
        // （40ms），但两片之间不再白等 50ms，占空比 ≈100%。算完再交给原 goto 去走，
        // 而 `getPathTo` 的包裹负责把算好的结果喂给 `monitorMovement`，让它不必重算。
        //
        // ⚠️ **不能靠多调几次 `monitorMovement` 来提速**：它同时驱动挖掘（bot.dig）、
        //    放方块（bot.placeBlock）、setControlState('jump'/'sneak') —— 多调一次就会
        //    重复触发这些副作用。所以只碰纯计算的那条路（getPathFromTo / getPathTo）。
        const _origGetPathTo = bot.pathfinder.getPathTo.bind(bot.pathfinder);
        //: 单槽缓存 `{ goal, result }`。**命中即消费**（置 null）—— 只允许用一次，
        //: 这样「预算期间世界变了」最坏只导致一次陈旧尝试，之后 resetPath 触发的重算会
        //: 走 _origGetPathTo（缓存已空）= 退化成改动前的行为，是正确的兜底。
        let _pre = null;
        //: 递增令牌：goal 变更 / 中止时 ++，在飞的预算发现 token 不是自己的就丢弃结果。
        let _preToken = 0;
        //: 诊断计数。★ **五个数分属两个不同的函数，别互相做分母**（2026-08-16 我自己算错过一次）：
        //:
        //:   goto()      每次调用记 `hit`（预算算出完整路）或 `timeout`（没算出来）
        //:               → **预算成功率 = hit / (hit + timeout)**，这个除法才有意义（实测 76%）
        //:   getPathTo() 每次被 monitorMovement 调用时记 `hit` / `miss` / `drift`
        //:               → miss 多是**正常的**：缓存是单次消费，而一次行走途中被打断
        //:                 （挖方块 / 被怪推走 / chunk 加载 / goal 重置）会让 monitorMovement
        //:                 反复重新问路，第二次起必然 miss。所以 `hit/(hit+miss)` **不是**
        //:                 任何有意义的比率（我拿它算出 29% 并据此误判"收益为零"）。
        //:
        //: `timeout` 也**不等于失败**：预算没算出完整路径时会退化成交给 _origGoto 走 partial
        //: 路径（见 goto 包裹里那段），bot 照样挪近 —— 所以最终 `Took to long` 的次数
        //: 远少于 timeout 计数（实测 25 vs 109）。
        //:
        //: 最要紧的用途：identity 链一旦断裂表现为 **hit 恒 0 而 miss 在涨**
        //: （白算一遍再走老路：性能倒退但不报错），不看这行发现不了。
        const _preStat = { hit: 0, miss: 0, drift: 0, abort: 0, timeout: 0 };
        bot.__openkagePreStat = _preStat;
        //: 首节点漂移上限（格²）：预算期间 bot 可能被怪物击退/落水/掉落，
        //: 那时路径首节点在身后，硬走会先往回退。超过 3 格就弃用缓存、让库重算。
        const _PRE_MAX_DRIFT_SQ = 9;

        function _cancelPrecompute() {
            _preToken++;
            _pre = null;
        }

        /** 异步算完整条路径；返回 result 或 null（被作废/无 movements/无 goal）。 */
        async function _precompute(goal, budgetMs) {
            const myToken = ++_preToken;
            const movements = bot.pathfinder.movements;
            // setGoal(null) 是全局的取消手段，movements 也可能还没装 —— 都直接放弃预算。
            if (!movements || !goal) return null;
            let gen;
            try {
                gen = bot.pathfinder.getPathFromTo(movements, bot.entity.position, goal, {
                    timeout: budgetMs,
                    tickTimeout: bot.pathfinder.tickTimeout,
                });
            } catch (e) {
                return null;   // 构造失败（畸形 goal 等）→ 交给原 goto 去照常报错
            }
            let r = gen.next().value?.result;
            while (r && r.status === "partial") {
                await new Promise((res) => setImmediate(res));
                if (myToken !== _preToken) {
                    _preStat.abort++;
                    return null;   // 已被 setGoal / abort 作废
                }
                try {
                    r = gen.next().value?.result;
                } catch (e) {
                    return null;
                }
            }
            // ★ 必须摘掉 context：它是整个 AStar 实例（closedDataSet + openHeap，
            //   六位数节点量级）。留着会被 _pre 一直 pin 住，等于把一次搜索的全部内存挂起来。
            if (r) delete r.context;
            return r;
        }

        // 递归下钻找坐标：pathfinder 的 Goal 类把坐标放在**三种不同位置**
        // （见 node_modules/mineflayer-pathfinder/lib/goals.js）：
        //   · 自有 x/y/z   —— GoalBlock / GoalNear / GoalXZ / GoalNearXZ / GoalY /
        //                     GoalGetToBlock / GoalFollow（后者由 entity.position 算出）
        //   · `.pos`（Vec3）—— GoalLookAtBlock / GoalPlaceBlock（**没有**自有 x/y/z）
        //   · 包着别的 Goal —— GoalBreakBlock/.goal、GoalInvert/.goal、GoalComposite{Any,All}/.goals[]
        //
        // ★ 只看自有 x/y/z 是不够的：`.pos` 系与包装系**同样会漏过**，而它们同样致命：
        //   `new GoalLookAtBlock(new Vec3(NaN,NaN,NaN), world)` 的字段是 ['pos','world',…]，
        //   `'x' in goal === false` → 那种校验会直接放行；而它 heuristic 返回 NaN、isEnd 恒 false，
        //   与 GoalNear 的 NaN 是一模一样的病（A* 既判不出到达、又无法靠 f 值剪枝 → open list 爆）。
        //   这两个类在本作用域的解构里对 LLM **直接可见**（见上面 require 的那一大坨），
        //   「LLM 绕过原语直接调 pathfinder」这条路径恰好从这个缺口穿过去。
        //   故改成递归下钻，日后新增 Goal 类型也能兜住。
        const _GOAL_MAX_DEPTH = 6;   // 仅防畸形/自引用结构把校验本身拖死；正常 Goal 最多套 2 层
        function _badGoalCoords(goal, depth, seen) {
            if (!goal || typeof goal !== "object") return null;
            if (depth > _GOAL_MAX_DEPTH) return null;
            if (seen.has(goal)) return null;          // 环形引用保护
            seen.add(goal);
            const bad = [];
            for (const k of ["x", "y", "z"]) {
                if (k in goal && !Number.isFinite(goal[k])) bad.push(`${k}=${goal[k]}`);
            }
            if (goal.pos && typeof goal.pos === "object") {
                for (const k of ["x", "y", "z"]) {
                    if (k in goal.pos && !Number.isFinite(goal.pos[k])) {
                        bad.push(`pos.${k}=${goal.pos[k]}`);
                    }
                }
            }
            // `rangeSq` 同样必须是有限数：GoalNear/GoalNearXZ/GoalFollow 少传
            // 最后那个 range 参数就会得到 `rangeSq = NaN`，于是判「到达」的
            // `dx*dx+dy*dy+dz*dz <= NaN` 恒 false —— bot 站在目标上也不承认到了，每次寻路都
            // 必然烧满 thinkTimeout。
            // 而主线程一卡就掉线（见 mineBlock 里那段 keep_alive 的记录）。Voyager 原版的
            // exploreUntil 就漏了这个参数（已修），LLM 手写 goal 时同样容易漏，故一并挡在入口。
            //
            // ★ 坐标坏与 range 坏**同时发生**时必须优先报坐标：
            //   `new GoalNear(NaN, NaN, NaN)` 两者都坏，若把 rangeSq 混进同一个 bad 列表，
            //   下游 `bad.includes("rangeSq")` 就会把它判成 range 类错、让 LLM 去补 range ——
            //   而真正的病是坐标算错了，补 range 治不了。坐标是更根本的错，先报它。
            if (bad.length) return bad.join(", ");
            if ("rangeSq" in goal && !Number.isFinite(goal.rangeSq)) {
                return `rangeSq=${goal.rangeSq}（GoalNear/GoalNearXZ 少传了最后的 range 参数）`;
            }
            const inner = _badGoalCoords(goal.goal, depth + 1, seen);
            if (inner) return `goal{${inner}}`;
            if (Array.isArray(goal.goals)) {
                for (let i = 0; i < goal.goals.length; i++) {
                    const e = _badGoalCoords(goal.goals[i], depth + 1, seen);
                    if (e) return `goals[${i}]{${e}}`;
                }
            }
            return null;
        }
        function _goalErr(goal) {
            const bad = _badGoalCoords(goal, 0, new Set());
            if (!bad) return null;
            // 两类问题的修法完全不同，报错必须分开说 —— 否则「少传 range」会被这条消息
            // 误导去查坐标（坐标其实没错），白烧步数。
            const isRange = bad.includes("rangeSq");
            const msg = isRange
                ? `寻路目标非法（${bad}）。已拒绝这次寻路：` +
                  `rangeSq 为 NaN 会让「是否到达」的判断恒为 false —— bot 走到目标上也不算到，` +
                  `每次寻路都必然耗尽思考时限。` +
                  `补上最后一个参数即可，例如 new GoalNear(x, y, z, 1)、new GoalNearXZ(x, z, 1)。`
                : `寻路目标坐标非法（${bad}）—— 多半是坐标算错了。` +
                  `注意 bot.findBlocks() 返回的是 **Vec3 数组**（直接用 v.x/v.y/v.z），` +
                  `而 bot.findBlock() 返回的是 **Block 对象**（要用 b.position.x）。` +
                  `已拒绝这次寻路：NaN 目标会让寻路算法无法终止并耗尽内存。`;
            if (bot.openkageLog) bot.openkageLog.push("[warn] " + msg);
            return new Error(msg);
        }
        // ★ _goalXYZ：从 goal 递归取出**首个有限坐标** {x,y,z}（任一轴可 undefined），供「上方无可达路径」
        //   检测算垂直缺口用。复用 _badGoalCoords 的递归形状（自有 x/y/z、.pos、.goal、.goals[]）。
        //   关键：用 Number.isFinite 判定——GoalXZ/GoalNearXZ 无 y 字段，g.y 为 undefined，
        //   Number.isFinite(undefined)===false，故无 y 的 goal 永不触发「头顶」判据（水平探索安全）。
        //   复合 goal（GoalCompositeAny「到达任一即可」）优先返回**带有限 y 的子目标**：只要任一子目标
        //   可达就不该误报，而带 y 的子目标才是「上方够不着」判断的对象。
        function _goalXYZ(goal, depth, seen) {
            if (!goal || typeof goal !== "object") return null;
            if (depth > _GOAL_MAX_DEPTH) return null;
            if (seen.has(goal)) return null;          // 环形引用保护
            seen.add(goal);
            // 自有坐标 / .pos 坐标：取有限值，undefined 轴留空
            const pick = (o) => {
                if (!o || typeof o !== "object") return null;
                const has = (k) => k in o && Number.isFinite(o[k]);
                if (has("x") || has("y") || has("z")) {
                    return { x: has("x") ? o.x : undefined,
                             y: has("y") ? o.y : undefined,
                             z: has("z") ? o.z : undefined };
                }
                return null;
            };
            const own = pick(goal) || (goal.pos ? pick(goal.pos) : null);
            // 递归子目标，优先返回带有限 y 的（复合 goal 的可达性判断更准）
            const inner = _goalXYZ(goal.goal, depth + 1, seen);
            if (inner) return inner;
            if (Array.isArray(goal.goals)) {
                let withY = null, first = null;
                for (const sg of goal.goals) {
                    const r = _goalXYZ(sg, depth + 1, seen);
                    if (r) {
                        if (!first) first = r;
                        if (r.y !== undefined && !withY) withY = r;
                    }
                }
                if (withY) return withY;
                if (first) return first;
            }
            return own;
        }
        // ★ _goalWantsStandOn：判断 goal 是否「要求 bot 站到指定坐标」（vs「靠近/看着即可」）。
        //   只对「要站到指定点」的 goal 判「无可达路径」——GoalBlock/GoalNear/GoalGetToBlock 要 bot 站到
        //   目标点/旁；而 GoalLookAtBlock（collectBlock 挖方块用，reach=4.5 站旁边看着即可）、GoalFollow
        //   （跟随实体，靠近即可）不该拦——bot 在低处够得着挖高处方块是合法的，拦了就误报。
        //   判别靠字段特征（goals.js）：GoalLookAtBlock 有 reach+world+pos；GoalFollow 有 entity。
        //   复合 goal（GoalCompositeAny「任一即可」）只要有任一子 goal 要 standOn 就算（保守：任一可达
        //   子目标的判据会在 T3/T4 卡住而不误 throw）。
        function _goalWantsStandOn(goal, depth, seen) {
            if (!goal || typeof goal !== "object") return false;
            if (depth > _GOAL_MAX_DEPTH) return false;
            if (seen.has(goal)) return false;
            seen.add(goal);
            // 靠近/看着即可的 goal —— 排除
            if ("reach" in goal && "world" in goal) return false;   // GoalLookAtBlock
            if ("entity" in goal) return false;                      // GoalFollow
            // 要 standOn 的 goal：有 rangeSq（GoalNear/GoalNearXZ）或自有 x/y/z 无 reach（GoalBlock/GoalGetToBlock）
            if ("rangeSq" in goal) return true;                      // GoalNear/GoalNearXZ
            if ("x" in goal && "y" in goal && "z" in goal) return true; // GoalBlock/GoalGetToBlock
            // GoalY：只有 y 无 x/z（到达某高度，要爬到/下到该 y）。0902 超时里 GoalY(cy+1) 是徒劳跳常见写法。
            if ("y" in goal && !("x" in goal) && !("z" in goal) && !("rangeSq" in goal)) return true;
            // 递归子 goal
            if (_goalWantsStandOn(goal.goal, depth + 1, seen)) return true;
            if (Array.isArray(goal.goals)) {
                for (const sg of goal.goals) if (_goalWantsStandOn(sg, depth + 1, seen)) return true;
            }
            return false;
        }
        // ★ _goalDeprecatedErr：拦截已废弃/会崩的 Goal 类型。GoalBreakBlock 被 mineflayer-pathfinder
        //   废弃（history.md:69 "deprecate GoalBreakBlock"），且库实现把 bot 当 world 传给内部
        //   GoalLookAtBlock（goals.js:212），导致 isEnd 的 this.world.raycast 崩 → "distanceTo undefined"
        //   报错（0902 run alice/carol step1 砍树开局就崩 16 次）。改用 GoalLookAtBlock(pos, bot.world)。
        //   用 instanceof 递归检测（含复合 goal 的 .goal/.goals[]）。
        function _goalDeprecatedErr(goal, depth, seen) {
            if (!goal || typeof goal !== "object") return null;
            if (depth > _GOAL_MAX_DEPTH || seen.has(goal)) return null;
            seen.add(goal);
            if (typeof GoalBreakBlock !== "undefined" && goal instanceof GoalBreakBlock) {
                const p = goal.goal && goal.goal.pos;
                const coord = p ? `(${p.x}, ${p.y}, ${p.z})` : "";
                const _e = new Error(
                    `GoalBreakBlock${coord} 已废弃且会崩溃（库内部把 bot 当 world 传错）。` +
                    `改用 GoalLookAtBlock：await bot.pathfinder.goto(new GoalLookAtBlock(new Vec3(x,y,z), bot.world))。` +
                    `两者语义相同（走到能看着/挖到目标方块的位置）。`
                );
                _e._openkageClean = true;   // handleError 不加 (raised by environment) 后缀
                return _e;
            }
            const inner = _goalDeprecatedErr(goal.goal, depth + 1, seen);
            if (inner) return inner;
            if (Array.isArray(goal.goals)) {
                for (const sg of goal.goals) {
                    const e = _goalDeprecatedErr(sg, depth + 1, seen);
                    if (e) return e;
                }
            }
            return null;
        }
        // ★ _goalVerticalRange：goal 在垂直方向能容忍的 dy（bot 站低/高处仍算「到达」的最大 y 差）。
        //   GoalNear: sqrt(rangeSq)（isEnd 是 dx²+dy²+dz²<=rangeSq，垂直单维最多 sqrt(rangeSq)）。
        //   GoalBlock: 0（要精确站到 x,y,z）。
        //   用于 T2：goalY-botY > verticalRange 才说明 bot 站不到达标的 y（够不着那个点）。
        function _goalVerticalRange(goal, depth, seen) {
            if (!goal || typeof goal !== "object") return 0;
            if (depth > _GOAL_MAX_DEPTH || seen.has(goal)) return 0;
            seen.add(goal);
            if ("rangeSq" in goal && Number.isFinite(goal.rangeSq)) return Math.sqrt(goal.rangeSq);
            // GoalBlock 无 rangeSq → 精确点，range 0
            const inner = _goalVerticalRange(goal.goal, depth + 1, seen);
            if (inner > 0) return inner;
            if (Array.isArray(goal.goals)) {
                let mx = 0;
                for (const sg of goal.goals) mx = Math.max(mx, _goalVerticalRange(sg, depth + 1, seen));
                if (mx > 0) return mx;
            }
            return 0;
        }
        // ★ _startJumpStallMonitor：goto 期间监测 bot 是否「原地徒劳跳」（y 振荡 + x/z 不动），
        //   命中则 setGoal(null) 中止 goto（库 reject PathStopped），调用方据此抛「无可达路径」。
        //   症状检测不依赖 A* 结果（v1-v3 失败的根因：precompute 不可靠/无便宜充分条件）。
        //   判据：最近 _NSAMP 个采样（每 physicsTick 一个）里 x/z 位移 <_STALL_XZ（原地）且
        //   y 振幅 >_OSCILL_Y（反复跳起回落）。正常寻路要么 y 不振（卡住算路）、要么 x/z 在动（真在走）；
        //   唯独「想往上爬但脚下无路」才是 y 振 + x/z 不动同时成立。
        //
        //   ★ **采样必须按游戏刻（physicsTick 事件）走，不能用墙钟 setInterval**：
        //     bot.setTickRate(N) 把客户端 physicsTick 定时器改成 50/N ms —— rate 一变，相同墙钟间隔
        //     （旧版 400ms）覆盖的 tick 数/物理时长就变，判据随 rate 漂移。改订 physicsTick 后，
        //     「每 _INTERVAL_TICKS 个 tick 判一次」恒等于固定游戏时长，无论 rate 怎么变判据对倍速免疫。
        //     physicsTick 是 mineflayer 真实事件：/tick rate 不让事件"跳过"，只改它两次触发间的墙钟间隔。
        //   _INTERVAL_TICKS=20 tick ≈ 1 秒游戏时间（20 TPS），与旧版 setInterval(400ms) 在 2.5× 下
        //     落在 50 tick/s ≈ 每 20 tick 一次的节拍相当（仅墙钟→tick 的等价替换，窗口大小不变）。
        function _startJumpStallMonitor(bot, gCoord, botYStart) {
            const _INTERVAL_TICKS = 20; // 判的节拍 = 每 20 个 physicsTick 判一次（rate 无关）
            const _NSAMP = 5;          // 窗口 = 最近 5 次判的采样（与旧版一致）
            const _STALL_XZ = 0.3;     // x/z 位移 <此值算「原地」
            const _OSCILL_Y = 0.3;     // y 振幅 >此值算「在跳」
            const samples = [];        // {x,y,z} 采样历史
            let triggered = false;
            let ticksSinceJudge = 0;
            const onPhysicsTick = () => {
                if (triggered) return;
                if (!bot || !bot.entity || !bot.entity.position) return;
                // 每 _INTERVAL_TICKS 个 tick 判一次（tick 0、20、40…），其余 tick 只计数不判。
                if (++ticksSinceJudge < _INTERVAL_TICKS) return;
                ticksSinceJudge = 0;
                const p = bot.entity.position;
                samples.push({ x: p.x, y: p.y, z: p.z });
                if (samples.length > _NSAMP) samples.shift();
                if (samples.length < _NSAMP) return;
                // x/z 原地？首末采样水平位移
                const dx = samples[samples.length - 1].x - samples[0].x;
                const dz = samples[samples.length - 1].z - samples[0].z;
                if (Math.abs(dx) > _STALL_XZ || Math.abs(dz) > _STALL_XZ) return;  // 水平在动 → 非徒劳
                // y 振荡？窗口内 max-min
                let yMin = Infinity, yMax = -Infinity;
                for (const s of samples) { if (s.y < yMin) yMin = s.y; if (s.y > yMax) yMax = s.y; }
                if (yMax - yMin < _OSCILL_Y) return;  // y 没振（站着不动/平稳走）→ 非徒劳
                // 命中徒劳跳：中止 goto。setGoal(null) 让库 reject PathStopped（goto.js:39）。
                triggered = true;
                const _coord = `(${gCoord.x}, ${gCoord.y}, ${gCoord.z})`;
                if (bot.openkageLog) bot.openkageLog.push(
                    `[warn] goto: 检测到徒劳跳 目标${_coord} ` +
                    `(bot y≈${yMin.toFixed(1)}-${yMax.toFixed(1)} 振荡, x/z 原地, 起始y=${botYStart})`);
                try {
                    if (bot.pathfinder) bot.pathfinder.setGoal(null);
                } catch (_e) { /* ignore */ }
            };
            bot.on("physicsTick", onPhysicsTick);
            return {
                triggered: () => triggered,
                stop() { bot.removeListener("physicsTick", onPhysicsTick); },
            };
        }
        // `monitorMovement` 要算新路径时唯一的入口（pathfinder/index.js:466 是
        // `bot.pathfinder.getPathTo(...)` —— 走的是**这个属性**，所以能从外面接管）。
        // 命中缓存就把预算好的结果直接给它，省掉那次「每 tick 只算一片」的慢重算。
        bot.pathfinder.getPathTo = (movements, goal, timeout) => {
            // identity 匹配：goto.js:63 把**同一个 goal 对象**传进 setGoal，
            // 而 monitorMovement 用的 stateGoal 就是它 —— 所以用 === 而不是比坐标。
            if (_pre && _pre.goal === goal) {
                const cached = _pre.result;
                _pre = null;                       // 单次消费
                const first = cached.path && cached.path[0];
                if (!first) {
                    _preStat.hit++;
                    return cached;                 // 空路径（已在目标上）：直接给
                }
                const dx = bot.entity.position.x - first.x;
                const dy = bot.entity.position.y - first.y;
                const dz = bot.entity.position.z - first.z;
                if (dx * dx + dy * dy + dz * dz <= _PRE_MAX_DRIFT_SQ) {
                    _preStat.hit++;
                    return cached;
                }
                _preStat.drift++;                  // 漂移太远 → 弃用，让库按当前位置重算
            } else {
                _preStat.miss++;
            }
            return _origGetPathTo(movements, goal, timeout);
        };
        bot.pathfinder.goto = async (goal, ...rest) => {
            const e = _goalErr(goal);
            // 保持「不同步抛」这条纪律：本函数是 async，抛出即 reject，
            // 调用方的 `await ... catch` 照常接住（原注释那个担心由 async 天然满足）。
            if (e) throw e;
            const _dep = _goalDeprecatedErr(goal, 0, new Set());
            if (_dep) throw _dep;
            // 先异步把整条路算出来（占空比 ≈100%）。
            // ★ 预算只取本步剩余时间的 **30%**，不是一半 —— 因为「算路」之后还得**走**：
            //   mineBlock 用 `Promise.race(goto, walkBudget=剩余*50%)` 掐这次 goto，
            //   若预算也吃到 50%，路刚算完就被 race 判超时，等于白算（而改动前它是走着走着
            //   被掐、至少挪近了一截）。另外四个 primitive（useChest/smeltItem/placeItem/
            //   craftItem）没有自己的时间预算、只靠步级超时，这里的夹取同样保护它们。
            //   拿不到 deadline（非 /step 上下文）时才退回 thinkTimeout。
            //
            // ⚠️ **预算失败是有成本的**：这段时间 bot 站着不动（不像库那样边走边算）。
            //   所以预算窗口要足够小，失败了也只损失一小截；剩下的时间交给 _origGoto
            //   走 partial 路径。这就是取 30%（而不是全部 thinkTimeout）的第二个理由。
            const remainMs =
                typeof bot.openkageStepDeadlineMs === "number"
                    ? bot.openkageStepDeadlineMs - Date.now()
                    : bot.pathfinder.thinkTimeout;
            const budget = Math.max(
                800,
                Math.min(bot.pathfinder.thinkTimeout, Math.floor(remainMs * 0.3))
            );
            const pre = await _precompute(goal, budget);
            // ★ **预算失败绝不能直接抛错**（2026-08-16 实测踩过）：
            //   改动前的行为是「A* 超时 → 返回 **partial 路径** → bot 边走边算」，所以哪怕
            //   算不出完整路线，bot 也会**朝目标挪近一截**，下一步 LLM 再调就落在更近的地方
            //   （mineBlock 的那句 "often already closer, so just call mineBlock again" 正是
            //   建立在这个行为上的）。
            //   我第一版在这里 `throw` → bot **一步没动**就报错，于是同一个目标反复失败、
            //   步数白烧。实测 26 次/75 step（34.7%）远高于基线 40/318（12.6%）—— 报错率
            //   反而**升了**，就是这个原因。
            //   正确做法：预算没算出完整路径时**照旧交给 _origGoto**，让库自己走 partial 路径
            //   （它会在 monitorMovement 里继续续算）。预算这条路只在**成功时**抄近道，
            //   失败时完全退化成改动前的行为 —— 只赚不赔。
            if (pre && pre.status !== "success") _preStat.timeout++;
            else if (pre) _pre = { goal, result: pre };   // 交给上面的 getPathTo 消费
            // ★ 「无可达路径」客观反馈（2026-09-02，v4 症状检测，对齐 placeBlockSafe「够不着」范式）：
            //   agent 调 goto(高处目标) 想往上爬但脚下无路/无块时，bot 在 _origGoto 走 partial 期间
            //   **原地反复跳起又回落**（y 振荡、x/z 不动），跳到步超时只反馈「动作超时」—— agent 不知
            //   根因是上方够不着，反复重试烧步数（0902 dave step16/36/78 等）。
            //
            //   ★ 为什么不靠 A* 结果判（v1-v3 都失败）：判断「路径不可达」需完整跑完 A*，但完整 A* 慢
            //     （正是超时原因）；precompute 的 bestNode.y 不可靠——partial 可能只是「没算完」非「无路」，
            //     且 precompute 常被 setGoal 作废返回 null（collectBlock 等场景），判据 0 次执行（v3 实测）。
            //     没有便宜的「不可达」充分条件（placeBlockSafe 能判「够不着」靠瞬时几何，goto 可达性是搜索问题无此条件）。
            //
            //   v4 改为**症状检测**：_origGoto 期间监测 bot 位置，检测到「原地 y 振荡」（持续跳起回落但
            //   x/z 不动）= 徒劳跳的硬症状，早停 + 抛客观事实。不依赖 A* 结果，直接观测 bot 真实行为。
            //   只在「可能是爬高」时启动监测（goalY>botY 且 wantsStandOn），水平 goto 不监测。
            const _gFence = _goalXYZ(goal, 0, new Set());
            const _botYFence = Math.floor(bot.entity.position.y);
            let _fenceMon = null;   // 振荡监测器
            // 只在「bot 真站不到达标 y」时监测：vertGap > verticalRange（垂直差超 goal 的 y 容差）。
            //   GoalNear(67, range=2) dy=1 ≤ 2 → bot 站低处就达标，不该监测（v4 初版漏了这个过滤，
            //   把正常跳跃寻路误判徒劳跳：erin step20 GoalNear(225,67,545,2) 被误报）。
            //   GoalNear(y+1, 0.5) dy=1 > 0.5 → bot 站不到 → 监测（真徒劳跳场景）。
            if (_gFence && Number.isFinite(_gFence.y) && _gFence.y > _botYFence &&
                _goalWantsStandOn(goal, 0, new Set()) &&
                (_gFence.y - _botYFence) > _goalVerticalRange(goal, 0, new Set())) {
                _fenceMon = _startJumpStallMonitor(bot, _gFence, _botYFence);
            }
            try {
                return await _origGoto(goal, ...rest);
            } catch (_gotoErr) {
                // 监测器检测到徒劳跳（setGoal(null) 触发库 reject PathStopped）→ 转成「无可达路径」客观反馈。
                // 非监测器触发的 reject（真 NoPath/GoalChanged 等）原样抛出，不干预。
                if (_fenceMon && _fenceMon.triggered()) {
                    const _coord = `(${_gFence.x}, ${_gFence.y}, ${_gFence.z})`;
                    const _e = new Error(
                        `goto: 目标【${_coord}】已无可达路径，需要破坏/放置方块以搭建可达路径。`
                    );
                    _e._openkageClean = true;   // handleError 不加 (raised by environment) 后缀
                    throw _e;
                }
                throw _gotoErr;
            } finally {
                if (_fenceMon) _fenceMon.stop();   // goto 结束（成功/失败/超时）必清监测器
            }
        };
        bot.pathfinder.setGoal = (goal, ...rest) => {
            const e = _goalErr(goal);
            if (e) throw e;   // setGoal 是同步 API，同步抛才符合调用方预期
            const _dep = _goalDeprecatedErr(goal, 0, new Set());
            if (_dep) throw _dep;
            // goal 换了（或被置 null 取消）→ 在飞的预算已无意义，作废它。
            // ⚠️ 这条也覆盖 exploreUntil 那个每 2s 调一次 setGoal 的循环：
            //    它走**裸 setGoal、不预算**，所以只会不断作废（不会堆积在飞任务）。
            //    动态 goal（pvp 的 setGoal(goal, true)）同理不预算 —— 打架时阻塞几秒算路
            //    比走一条部分路径更糟，这是有意的。
            //
            // ★ **但不能作废刚为这个 goal 预算好的结果**（2026-08-16 smoke 实测 hit=0/miss=23
            //   就是踩了这个）：`goto` 的调用链是
            //       我们的 goto → _precompute → 存 _pre → _origGoto
            //       → lib/goto.js:63 `bot.pathfinder.setGoal(goal)`  ← **又回到这里**
            //   传的正是同一个 goal 对象。无条件 _cancelPrecompute() 会把缓存当场清掉，
            //   于是 monitorMovement 再问 getPathTo 时必然 miss → 白算一遍再走老路
            //   （功能正常、但优化完全失效）。所以「goal 与在飞/已备的预算是同一个」时放行。
            if (!_pre || _pre.goal !== goal) _cancelPrecompute();
            return _origSetGoal(goal, ...rest);
        };
        // 让 `abortInFlightActions()` 能掐掉在飞的预算搜索。
        // ★ 必须与 goto 包裹**同时**上线：有在飞搜索却没有中止钩子 = 孤儿循环
        //   （正是 exploreUntil.js:56-60 那段注释警告的形状）。
        // 挂在 bot 上供 abortInFlightActions 调用（它在 ⓪ 阶段跑 openkageAborts，
        // 但那个集合每步会被清空 —— 预算取消是**跨步常驻**的能力，故用独立字段）。
        bot.__openkageCancelPrecompute = _cancelPrecompute;
    }   // ← if (!bot.__openkagePathfinderGuarded)

    // ★ collectBlock.collect Vec3 误传护栏（挂载一次）：
    //   findBlocks() 返回 Vec3（无 .position），直接喂 collect 会让 getClosest 取
    //   target.position.distanceTo 崩成干巴的 "reading 'distanceTo'"。入口拦下，
    //   抛带正确写法（先 bot.blockAt 转 Block）的干净错误（_openkageClean=true）。
    //   判据：x 数字 && .position===undefined（Block/Entity 的 .position 是 Vec3 对象，不误伤）。
    //   只换 collect 方法：cancelTask/targets 是 collectBlock 实例字段，abort 直接调，不能动。
    //   .bind + 三参透传：callback 模式下库会 callbackify(this.collect) 再回调本 wrapper。
    if (!bot.__openkageCollectGuarded) {
        if (bot.collectBlock && typeof bot.collectBlock.collect === "function") {
            bot.__openkageCollectGuarded = true;
            const _origCollect = bot.collectBlock.collect.bind(bot.collectBlock);
            bot.collectBlock.collect = function (target, options, cb) {
                const arr = Array.isArray(target) ? target : [target];
                for (let i = 0; i < arr.length; i++) {
                    const t = arr[i];
                    if (t == null) continue;
                    if (typeof t.x === "number" && t.position === undefined) {
                        const _e = new Error(
                            `collectBlock.collect: targets[${i}] 是 Vec3 坐标（有 x/y/z 但没有 .position），` +
                            `不能直接传给 collect —— 内部 getClosest 要 \`target.position.distanceTo(...)\`，` +
                            `Vec3 没有 .position 会崩。findBlocks() 返回 Vec3 数组，正确写法：先转成 Block —— ` +
                            `\`targets = vec3s.map(v => bot.blockAt(v)); await bot.collectBlock.collect(targets)\``
                        );
                        _e._openkageClean = true;   // handleError 不加 (raised by environment) 后缀
                        throw _e;
                    }
                }
                return _origCollect(target, options, cb);
            };
        }
    }

    // ★ **bot.placeBlockSafe —— bot.placeBlock 的修复版本（挂载一次）**
    //
    // 问题根因（与 placeItem.js:119 注释完全同构）：
    //   bot.placeBlock 内部 `_genericPlace` 会 `await bot.lookAt(target, forceLook=undefined)`，
    //   lookAt 非 force 模式需跨多个 physics tick 等视线插值追上目标，把放置时间窗口吃掉 →
    //   服务器等不到 blockUpdate 确认包 → "Event blockUpdate did not fire within timeout of 5000ms"。
    //   这是 carol/erin 困在地下竖井、无法垫脚爬回地面的直接原因。
    //
    // 修法（两步，与 placeItem.js 完全同构）：
    //   (a) 放置前 `bot.lookAt(target, true)` force 预对准 —— 瞬时设 lastSentYaw/Pitch，不 await。
    //       target 与 _genericPlace 内部算法逐字节一致（dx=0.5+face.x*0.5 同理），
    //       否则服务器按视线方向拒绝放置，blockUpdate 仍不触发。
    //   (b) 放置用 `bot._placeBlockWithOptions({forceLook:'ignore'})` —— 跳过内部 lookAt，
    //       退化为"发包 + 等确认"，窗口内不再有转向等待。
    //   (c) 重试 3 次兜底残余抖动（与 placeItem.js 的 _PLACE_TRIES 同值）。
    //
    // 用法与 bot.placeBlock 完全相同，直接替换即可：
    //   await bot.placeBlockSafe(refBlock, new Vec3(0, 1, 0))
    if (!bot.placeBlockSafe) {
        bot.placeBlockSafe = async function (referenceBlock, faceVector) {
            // ── ① 类型校验：referenceBlock 必须是 Block（有 .position），不是 Vec3 ──
            if (referenceBlock === null || referenceBlock === undefined) {
                throw new Error(
                    "placeBlockSafe: referenceBlock 为 null —— " +
                    "chunk 未加载或坐标算错，先 bot.blockAt() 确认非 null 再放置"
                );
            }
            if (typeof referenceBlock !== "object" ||
                typeof referenceBlock.position !== "object" ||
                referenceBlock.position === null) {
                const _hint = typeof referenceBlock.x === "number"
                    ? "（传的是 Vec3 坐标，应先 bot.blockAt(vec3) 取到 Block 再传进来）"
                    : "（必须是 bot.blockAt() 返回的 Block，有 .position/.name 字段）";
                throw new Error("placeBlockSafe: referenceBlock 不是有效的 Block " + _hint);
            }
            // ── ② 类型校验：faceVector 必须是 Vec3（有数值 .x/.y/.z）──
            if (!faceVector ||
                typeof faceVector.x !== "number" ||
                typeof faceVector.y !== "number" ||
                typeof faceVector.z !== "number") {
                throw new Error(
                    "placeBlockSafe: faceVector 不是有效的 Vec3 —— " +
                    "如 new Vec3(0,1,0) = 顶面、new Vec3(0,0,1) = +z 侧面"
                );
            }
            // ── ③ 可达性：眼睛到目标面心距离 >5 格立刻报错，省去 3×5s 空等 ──
            //    面心 = block 中心 + face × 0.5（与 _genericPlace 内部算法一致）
            const _fcX = referenceBlock.position.x + 0.5 + faceVector.x * 0.5;
            const _fcY = referenceBlock.position.y + 0.5 + faceVector.y * 0.5;
            const _fcZ = referenceBlock.position.z + 0.5 + faceVector.z * 0.5;
            const _dist = Math.sqrt(
                (bot.entity.position.x - _fcX) ** 2 +
                (bot.entity.position.y + 1.62 - _fcY) ** 2 +  // 眼睛高度约 1.62 格
                (bot.entity.position.z - _fcZ) ** 2
            );
            if (_dist > 5) {
                const _p = referenceBlock.position;
                throw new Error(
                    "placeBlockSafe: 够不着 —— 眼睛到目标面距离 " + _dist.toFixed(1) + " 格（上限 5），" +
                    "先 `await bot.pathfinder.goto(new GoalNear(" +
                    _p.x + "," + _p.y + "," + _p.z + ", 3))` 走近"
                );
            }
            // ── ④ 持物校验：手持为空立刻抛，而不是发包后默默失败 ──
            if (!bot.heldItem) {
                throw new Error(
                    "placeBlockSafe: 手持为空 —— 放前先 await bot.equip(item, 'hand')"
                );
            }

            // ── 放置主逻辑（forceLook:'ignore' 修复 blockUpdate 超时，见上方注释）──
            // ★ 目标格 = referenceBlock 沿 faceVector 方向相邻那一格（方块将出现在这里）
            const _targetPos = referenceBlock.position.offset(
                faceVector.x, faceVector.y, faceVector.z);
            const _dx = 0.5 + faceVector.x * 0.5;
            const _dy = 0.5 + faceVector.y * 0.5;
            const _dz = 0.5 + faceVector.z * 0.5;
            const _lookTarget = referenceBlock.position.offset(_dx, _dy, _dz);
            const _PLACE_TRIES = 3;
            let _lastErr = "";
            for (let _attempt = 0; _attempt < _PLACE_TRIES; _attempt++) {
                // 每次重试前重新 force 对准视线（2026-08-24 修，对应 dave step70/71 火把反复超时：
                // 第一次 force lookAt 发出后服务器侧视线插值要几 tick 才追上；
                // 把 lookAt 放进循环，第二次重试时服务器视线已稳定，放置命中）
                await bot.lookAt(_lookTarget, true);
                try {
                    await bot._placeBlockWithOptions(referenceBlock, faceVector, {
                        swingArm: "right",
                        forceLook: "ignore",
                    });
                    // ★ 放置后验证：发包成功 ≠ 方块真在（服务器可能因碰撞/视线静默拒放）。
                    //   等 2 tick 让服务器状态同步，再 blockAt 确认目标格现在是手持方块。
                    //   学 placeItem.js:13「放置成功当且仅当目标格现在真的是那个方块」。
                    await bot.waitForTicks(2);
                    const _now = bot.blockAt(_targetPos);
                    // 方块放上后目标格应非 air。仍空气 = 没放上，查原因。
                    if (!_now || _now.name === "air" || _now.name === "cave_air") {
                        // ★ 查目标格内有没有实体（含 bot 自己）—— MC 不能在实体占据的格子放方块，
                        //   服务器静默拒放。扫 bot.entities 找碰撞箱与目标格重叠的实体并报出名字。
                        const _occupiers = [];
                        for (const _eid in bot.entities) {
                            const _e = bot.entities[_eid];
                            if (!_e || !_e.position) continue;
                            // 实体碰撞箱底角 ~ 顶角，Y 占两格（脚 + 头）
                            const _ef = _e.position.floored();
                            if (_ef.x === _targetPos.x && _ef.z === _targetPos.z &&
                                (_ef.y === _targetPos.y || _ef.y + 1 === _targetPos.y)) {
                                const _name = _e.name || _e.username ||
                                    (_e === bot.entity ? "你自己" : "未知实体");
                                _occupiers.push(_name);
                            }
                        }
                        if (_occupiers.length > 0) {
                            const _who = _occupiers.join("、");
                            throw new Error(
                                "placeBlockSafe: 方块没放上 —— 目标格(" +
                                _targetPos.x + "," + _targetPos.y + "," + _targetPos.z +
                                ")被实体占据：" + _who +
                                "（不能在实体所在的格子放方块）"
                            );
                        }
                        throw new Error(
                            "placeBlockSafe: 方块没放上 —— 目标格(" +
                            _targetPos.x + "," + _targetPos.y + "," + _targetPos.z +
                            ")仍是空气（视线没对准 / 邻格无支撑 / 放置被拒）"
                        );
                    }
                    return _targetPos; // 成功：返回方块实际落点 Vec3，供 caller 核对
                } catch (err) {
                    _lastErr = (err && err.message) || String(err);
                    // 「被实体占据」是确定性失败，重试无用，立刻抛
                    if (_lastErr.includes("被实体占据")) throw new Error(_lastErr);
                    if (_attempt < _PLACE_TRIES - 1) await bot.waitForTicks(4);
                }
            }
            throw new Error(_lastErr || "placeBlockSafe: 3 次重试均失败");
        };
    }

    // ★ **不在这里重置 globalTickCounter**：它是观察里的 `elapsedTime`，而
    //   env.py 的「世界时钟停摆」护栏是**跨步比较**它（`_tick == last_tick` 连续 12 步就中止）。
    //   每步归零会让这个比较失去意义 —— 读到的永远是"本步开头累积的那几十个 tick"，
    //   数值随本步耗时波动、甚至恒定，护栏因此误判"时间停摆"中止整轮探索。
    //   计数器在 /start 里初始化一次即可，之后单调递增，正好符合"游戏刻在走它就涨"的语义。

    function onTick() {
        // globalTickCounter 必须留：它是观察里的 elapsedTime（游戏刻），Python 侧的时钟停摆
        // 护栏靠它判断世界时间是否还在流动（见 env.py 的 STALL_ABORT_STEPS）。
        bot.globalTickCounter++;
        // ★ 卡住自动传送已删除，原为 Voyager 的 onStuck/teleportBot：
        // pathfinder 移动中每 100 tick 检查一次，若 5 个采样点位移 < 1.5 格就把 bot 随机
        // 传送到附近 1 格内的空气块。
        //
        // 为什么删（不是清理，是修实验缺陷）：
        // ① **OpenKAGE没有 task 边界**。Voyager 逐 task 执行，卡死会让整个 task 挂住，故需要兜底；
        //    OpenKAGE是连续自由探索（一个世界跑满 max_nights），"卡住"本身就是 bot 该学会解决的
        //    问题（挖开、跳出去、换方向）—— 自动传送把这个学习机会直接拿掉了。
        // ② **它静默改变 bot 位置，且观察里没有任何字段告知**。曾观察到的后果：bot 挖矿到
        //    逼仄矿坑，被连续传送几次，位置凭空变动叠加拾取延迟后，它误判「库存系统出现严重
        //    问题」，进而写 `bot.inventory.slots[i]=null` 想"重置库存"，把真有的
        //    wooden_pickaxe 也弄没了 → tier 从 wooden 掉回 none。一次善意的兜底，
        //    导致 bot 自毁 + 十几步无效挣扎。
        // ③ 传送使轨迹不可复现、也不可解释（位移不来自 bot 的任何决策）。
    }

    // 事件名从 `physicTick` 改为 `physicsTick`。mineflayer 已弃用前者
    // （lib/plugins/physics.js 两个都 emit，行为等价），但它装了个 newListener 钩子：一旦有人监听
    // 弃用名就 console.warn 一条弃用警告 —— 而 /step 期间 console.warn 正被劫持进 onLog，
    // 等于白占一行 LLM 的 40 行观测配额。
    bot.on("physicsTick", onTick);

    // initialize fail count
    let _craftItemFailCount = 0;
    let _killMobFailCount = 0;
    let _mineBlockFailCount = 0;
    let _placeItemFailCount = 0;
    let _smeltItemFailCount = 0;
    let _collectDroppedItemsFailCount = 0;

    // Retrieve array form post bod
    const code = req.body.code;
    const programs = req.body.programs;
    // bot 自选的单步时限：LLM 按本步动作大小申请，服务端 clamp 兜底。**单位是游戏刻**（见
    // STEP_CODE_TIMEOUT_TICKS）：对 Agent 来说世界以刻流逝，时限也按刻给。HTTP 字段名 `timeoutTicks`
    // 与 Python 侧 env.step 的 body["timeoutTicks"] 对齐。
    const stepTimeoutTicks = clampTimeout(req.body.timeoutTicks);
    // 本步的绝对截止时刻，暴露给原语。
    // 为什么需要：原语内部若自己做耗时动作（如 mineBlock 发现目标在 16 格外、先寻路走近），
    // 不知道本步还剩多少时间就会把整步预算烧光 —— 远处的树可能让 goto 死磕到
    // "Took to long to decide path"，把 LLM 只申请短时限的一步拖到被强制中止，
    // 连原语自己的 catch 都没机会跑。有了它，原语可以只花剩余时间的一部分、留出采集时间。
    //
    // ★ 原语内部用 `Date.now()` 读这个截止时刻（wall-clock），故这里把刻换算成墙钟毫秒：
    //   `刻 / (20 * rate) * 1000`。1x 下与旧实现逐秒等价；倍速下随 rate 收缩（10x 下 500 刻=2.5s 墙钟，
    //   原语据此自限、与刻超时同节拍）。这是原语的内部实现细节、不暴露给 Agent——
    //   Agent 看到的永远是刻。
    const _rate = Math.max(1, Number(bot.openkageTickRate) || 1);
    bot.openkageStepDeadlineMs = Date.now() + (stepTimeoutTicks / (20 * _rate)) * 1000;
    bot.cumulativeObs = [];
    bot.openkageDied = false;  // 本步开始清死亡标记；死亡熔断见 evaluateCode
    // console.log 捕获：本步内劫持 console，生成代码里的 log 攒进 onLog 插件，
    // 步末随 observe 回传给 LLM。原语自己不用 console.log（用 bot.chat），故捕获到的都是 LLM 写的。
    if (bot.openkageLog) bot.openkageLog.reset();
    // onChat 攒的是**本步**动作反馈（原语 bot.chat），必须步首清空 —— 它不像 onSystem 那样
    // 需要跨步存活，留着就是上一步的噪音。见 onChat.js reset() 的注释。
    if (bot.openkageChat) bot.openkageChat.reset();
    // 技能调用埋点缓冲：步首清空（计数按步累加，见 onSkill.js）。代理 push 进来，步末 flush。
    if (bot.openkageSkill) bot.openkageSkill.reset();
    releaseConsole = captureConsole();
    // bot 本步「在想什么」发到**游戏聊天框**：人在客户端围观时能直接
    // 看懂它的意图，不必去翻日志。
    // ★ 走 `bot._chat` 而不是 `bot.chat` —— 后者被 skillLoader 改造过：非斜杠开头的一律只
    //   emit chatEvent 给 LLM、**不进聊天框**（那次改动是为了不让 "Placed dirt" 之类原语反馈
    //   刷屏、盖住真正想看的东西）。而这句恰恰是"真正想看的东西"，所以绕过那层二分直接发。
    // ★ 分段发：MC 聊天单条上限是 **256 字节**（不是字符）。中文 UTF-8 占 3 字节，按字符切会超 ——
    //   180 个中文字就有 225 字节，再加前缀就顶穿，服务端会断开连接（Chat message too long）。
    //   故按**字节**切，留足余量（bot 名字等前缀也算在服务端那 256 里）。
    //   只发前两段，避免一步的思考把聊天框刷满 —— 想看全文去 dump 或日志。
    if (req.body.say) {
        const say = String(req.body.say).replace(/\s+/g, " ").trim();
        const segs = [];
        let cur = "";
        for (const ch of say) {                       // 按码点遍历，不切碎多字节字符/emoji
            if (Buffer.byteLength(cur + ch, "utf8") > 150) {
                segs.push(cur);
                cur = ch;
                if (segs.length >= 2) break;          // 只要前两段
            } else {
                cur += ch;
            }
        }
        if (cur && segs.length < 2) segs.push(cur);
        for (const seg of segs) {
            try {
                bot._chat(seg);
            } catch (e) {
                /* 聊天发失败绝不该影响这一步的执行 */
            }
        }
    }
    await waitTicksSafe(bot, bot.waitTicks);
    const r = await evaluateCode(code, programs, stepTimeoutTicks);
    releaseConsoleOnce();
    process.off("uncaughtException", otherError);
    if (r !== "success") {
        bot.emit("error", handleError(r));
    }
    // wait for last message —— **必须用 waitTicksSafe**：这是发响应前的最后一次等 tick，
    // bot 断流时用 bot.waitForTicks 会永久挂起、把整个 /step 拖到 300s 超时。
    await waitTicksSafe(bot, bot.waitTicks);
    // ★ window_click 风暴熔断的反馈注入（2026-09-08）。
    //   孤儿 deposit/withdraw（漏 await）的 reject 不在 /step 的 await 链上，熔断抛错走的是
    //   unhandledRejection 兜底——只在 index.js stdout 留痕，Python 侧和 Agent 看不到。
    //
    //   ★★ 时序坑（实测）：孤儿风暴在 close() 之后才异步起飞，从 count=1 涨到熔断阈值 500 约需
    //   2 秒（1665 包/秒）。上面 waitTicksSafe 只等 ~1s（20 tick），步内若没有额外 await，孤儿
    //   可能还没涨到 500、步就返回了——但孤儿会在步返回后继续疯转、最终 OOM 崩进程（熔断没拦住、
    //   onError 也没注入）。故这里在 waitTicksSafe 后**再补一段墙钟等待**：本步若有 window_click
    //   活动（count>0），就轮询最多 _FUSE_GRACE_MS（4s），等孤儿要么触发熔断、要么 count 停止增长
    //   （正常 transfer 完成）。这样把孤儿爆发窗口框在步内，熔断必能触发、onError 必能注入。
    //   正常步（无容器操作 count=0）零开销直接跳过。
    const _FUSE_GRACE_MS = 4000;
    if (bot.openkageWindowClickCount > 0 && !bot._openkageWindowClickFused) {
        let lastCount = bot.openkageWindowClickCount;
        let stableTicks = 0;
        const graceStart = Date.now();
        while (Date.now() - graceStart < _FUSE_GRACE_MS) {
            await new Promise((resolve) => setTimeout(resolve, 100));
            if (bot._openkageWindowClickFused) break;  // 孤儿触发熔断，立即出
            if (bot.openkageWindowClickCount === lastCount) {
                stableTicks++;
                // count 连续 1s 不变 = 正常 transfer 已完成、无孤儿在飞 → 不必再等
                if (stableTicks >= 10) break;
            } else {
                stableTicks = 0;
                lastCount = bot.openkageWindowClickCount;
            }
        }
    }
    if (bot._openkageWindowClickFused) {
        bot.emit("error",
            "本步触发 window_click 包风暴熔断（单步超 800 个 window_click 包）——" +
            "疑似 chest.deposit/withdraw 漏了 await 就 close()，或对已关窗口继续存取。" +
            "容器存取必须 await 完成后再 close()。");
    }
    if (bot._openkageHeapWarned) {
        // ★ 步末兜底再采一次（见 /start 里 physicsTick 采样器注释）：峰值可能落在两个采样点
        //   之间（1980ms→2010ms 这个窗），且 GC 后末值可能回落——兜底补采防漏。
        const endHeapMB = process.memoryUsage().heapUsed / (1024 * 1024);
        if (endHeapMB > bot.openkageHeapPeakMB) bot.openkageHeapPeakMB = endHeapMB;
        bot.emit("error",
            `本步内存峰值 heapUsed≈${bot.openkageHeapPeakMB.toFixed(0)}MB，已达进程上限` +
            `${_OPENKAGE_HEAP_LIMIT_MB}MB的${Math.round(_openkageHeapWarnRatio() * 100)}%——` +
            `进程接近 V8 heap OOM（继续下去会秒级崩溃、触发自愈重启、丢本步反馈）。`);
    }
    if (!response_sent) {
        response_sent = true;
        // ★ `bot.observe()` 返回的是 **JSON 字符串**（见 lib/observation/base.js：它
        //   `return JSON.stringify(bot.cumulativeObs)`），形如
        //   '[["observe",{...}],["onChat",{...}],...]'。要往里注入 world_healthy，必须先
        //   parse 成结构、改完再发——直接在字符串上 `payload.world_healthy = ...` 会**静默失败**
        //   （原始值设属性在非 strict 下不抛、也不生效），导致 875 行 `payload.world_healthy.healthy`
        //   读到 undefined、抛 TypeError，整个 /step handler 的 promise reject 成
        //   unhandledRejection，Python 侧拿不到响应 → ReadTimeout → 自愈重连 → 下一步又抛 →
        //   死循环（2026-08-21 heal 死循环事故的真正燃料，不是 Took-to-long）。
        //
        //   parse 后注入 world_healthy 到**最后一个 observe 事件的 payload** 里（Python 侧
        //   _parse_observe 正是取最后一个 observe 事件的 ev[1]），与 status/voxels 等字段并列。
        //   再 res.json(结构) 发出 JSON 数组——Express 不再二次编码，Python 侧 requests 直接
        //   解析成 list，走 _parse_observe 的 list 分支，与原先「字符串再 json.loads」等价。
        // onChat 再 flush 一次：releaseConsoleOnce 之后还有一次 waitTicksSafe（等最后的消息），
        // 漏 await 的原语可能在这一窗里才 emit 反馈。flush 对空缓冲是 no-op，重复调无害。
        if (bot && bot.openkageChat) bot.openkageChat.flush();
        const payloadStr = bot.observe();
        let payload;
        try {
            payload = JSON.parse(payloadStr);
        } catch (e) {
            // observe() 的契约是合法 JSON；万一畸形，退化成空 observe 事件，绝不让步末构造崩溃。
            console.error("[step] bot.observe() 返回非 JSON，退化空结构：", e && e.message);
            payload = [["observe", {}]];
        }
        // 每步返回前把世界健康状态挂到 observe 上（dave 虚空/carol 位移/erin 回填同源防御）。
        //   僵尸连接时 /step 仍正常返回、timeTicks 还在走，stall 护栏不会触发；靠这个字段让
        //   Python 侧在本步末尾立即发现异常并走 _heal_and_restart，避免 agent 在虚空里继续白给。
        const wh = computeWorldHealth(bot);
        // 找最后一个 observe 事件注入；没有就追加一个（理论上 observe() 一定含一条 observe 事件）。
        let injected = false;
        if (Array.isArray(payload)) {
            for (let i = payload.length - 1; i >= 0; i--) {
                const ev = payload[i];
                if (Array.isArray(ev) && ev.length === 2 && ev[0] === "observe" && ev[1]) {
                    ev[1].world_healthy = wh;
                    injected = true;
                    break;
                }
            }
        }
        if (!injected) {
            payload = Array.isArray(payload) ? payload : [];
            payload.push(["observe", { world_healthy: wh }]);
        }
        if (!wh.healthy && bot.openkageLog) {
            bot.openkageLog.push(
                `[warn] 世界状态异常：${wh.reason}。` +
                "本步动作可能不可靠，系统将尝试自愈重连。"
            );
        }
        res.json(payload);
    }
    // 路径预算的命中情况：只在**有过预算活动**的步里打一行，避免刷屏。
    // ★ 为什么值得打：`hit` 恒 0 + `miss` 在涨 = getPathTo 的 identity 匹配断了
    //   （goal 对象不是同一个），表现为"白算一遍再走老路" —— 性能倒退但功能正常，
    //   不打这行就完全看不出来。drift 高说明预算期间 bot 老被推走（击退/落水）。
    // ★ **行里直接把成功率算出来**：五个数分属 goto / getPathTo 两个函数，
    //   并排打很容易被拿去互相做分母（我自己就用 hit/(hit+miss) 算出 29% 并误判"收益为零"，
    //   正确的是 hit/(hit+timeout)=76%）。把唯一有意义的那个比率印出来，省掉这一步误算。
    if (bot.__openkagePreStat) {
        const s = bot.__openkagePreStat;
        if (s.hit || s.miss || s.drift || s.abort || s.timeout) {
            const tries = s.hit + s.timeout;
            const rate = tries ? ((s.hit / tries) * 100).toFixed(0) + "%" : "n/a";
            console.log(
                `[pathfinder预算] 预算成功率=${rate}（hit=${s.hit}/(hit+timeout=${tries})）` +
                ` | getPathTo 命中 hit=${s.hit} miss=${s.miss} drift=${s.drift}` +
                ` | abort=${s.abort}（累计，跨步不清零；miss 多属正常——缓存单次消费，` +
                `一次行走被打断后会重新问路）`
            );
        }
    }
    bot.removeListener("physicsTick", onTick);  // 名字必须与上面 bot.on 一致，否则每步漏一个监听器

    async function evaluateCode(code, programs, timeoutTicks) {
        // Echo the code produced for players to see it. Don't echo when the bot code is already producing dialog or it will double echo
        // ★ 标记「现在执行的是 LLM 生成的代码」，供 skillLoader 拦掉其中的游戏指令（见那里的注释）。
        //   置位覆盖**整个异步生命周期**（不只是 eval 那一行）—— 生成代码里的 await 会在 eval 返回
        //   之后才继续跑，只包住 eval 等于没拦。finally 里清位，超时/报错路径也不会漏。
        // trusted=true 表示这段代码是 **env 自己的基建脚本**（forceload 预热、/time set 等），
        // 不是 LLM 写的 —— 不置这个标志，斜杠指令就不会被拦（见 skillLoader.js 的说明）。
        // 由调用方显式声明，而不是在这里猜调用栈：两者走的是同一个 /step 端点，Node 侧分不出来。
        bot.openkageInGeneratedCode = !req.body.trusted;
        // ★ 重置 window_click 步内计数器 + 清熔断标志（见 /start 里的限流熔断注释）。
        //   每步独立计数：上一步即便触发过熔断，本步重新开始（自愈重连后 bot 重建，计数器也随新 bot 归零）。
        bot.openkageWindowClickCount = 0;
        bot._openkageWindowClickFused = false;
        // ★ 重置 heap 峰值/告警标志（见 /start 里 physicsTick 采样器注释）。每步独立：上一步的峰值
        //   不带入本步，告警也是每步最多注一条。
        bot.openkageHeapPeakMB = 0;
        bot._openkageHeapWarned = false;
        try {
            // 死亡熔断：把生成代码与「bot 死亡信号」赛跑。bot 若在代码执行途中被怪打死，
            // 那段 mineBlock/寻路 await 会永不 resolve → 整个 /step 卡到服务器超时踢下线。这里死亡一旦
            // 触发就中止等待、让本步干净返回（重生由 mineflayer options.respawn 默认自动做）。
            // 死亡信号用 **bot.health<=0 轮询**（/kill 瞬杀客户端收不到 health 包、death 事件也不 fire；
            // 但真实怪物连续掉血 update_health 会观测到 health 递减到 0，轮询对此一定有效）。
            // 也兼收 death 事件置的 openkageDied 标记（若 fire）。
            // ★ **`process` 在生成代码里必须被挡住**：
            //   LLM 可能写出 `if (!pick) { console.log('✗ 木镐不在库存！'); process.exit(1); }` ——
            //   `process.exit` 是**同步立即终止**，Node 侧连一行日志都来不及留，Python 侧只看到
            //   ConnectionError + 自愈重连，与 OOM 完全无法区分。
            //   它的本意只是"这一步没法继续了"，而那本该是 `throw` —— throw 会被下面的 catch 收成
            //   一条 onError 反馈交给 LLM，进程照常活着。
            //   做法：在 eval 的作用域里用一个同名局部变量**遮蔽**全局 `process`，把 exit/abort/kill
            //   换成抛错，其余字段（platform/version/memoryUsage…）透传，不影响正常读取。
            //   遮蔽而不是删：`typeof process` 仍为 object，LLM 那些 `process.platform` 之类的探测
            //   不会因此报奇怪的错。
            //   注意**必须放在 eval 的字符串作用域内**（下面拼进 `(async () => {…})`），
            //   放外面改不了 eval 里看到的绑定。原语不引用 process（已 grep 确认），故无副作用。
            // ★ 提示语用 `JSON.stringify` 注入，**不要手写引号转义**：手写 `\\'` / `\\"` 拼出来的
            //   容易是语法错误（`missing ) after argument list`），而它在**每一步**都会拼进 eval
            //   —— 等于整个 /step 全线报错。JSON.stringify 负责所有转义，消息里带中文/引号/反引号
            //   都不会出事。
            const _procExitMsg =
                "() is disabled here: it would kill the whole mineflayer server process, " +
                "losing the bot and this run's connection (the Python side would only see a " +
                "ConnectionError). To stop this step early, throw instead: " +
                'throw new Error("why") — that returns cleanly and tells you what happened.';
            // ★ 整个 shim **必须压在一行里、且不带结尾换行**：下面
            //   `handleError` 靠 `programs.split("\n").length` 把 V8 栈里的
            //   `<anonymous>:行号` 换算回「LLM 代码的第几行」，并据此摘出那一行原文给它看。
            //   shim 拼在 `programs` **之前**，多一行就把所有行号推移一行 —— 报错会指到隔壁语句，
            //   比不报更误导（那是 LLM 唯一的调试线索）。压成一行则偏移为 0，映射逻辑无需改动。
            const processShim =
                "const process = new Proxy(globalThis.process, { get(t, k) {" +
                " if (k === 'exit' || k === 'abort' || k === 'kill' || k === 'reallyExit') {" +
                " return () => { throw new Error('process.' + String(k) + " +
                JSON.stringify(_procExitMsg) +
                "); }; }" +
                " const v = t[k];" +
                " return typeof v === 'function' ? v.bind(t) : v;" +
                " } });";
            // ★ **LLM 代码必须套在自己的嵌套作用域里**：若 `programs` 与 `code` 平铺在
            //   **同一个** eval 作用域，LLM 只要声明一个与原语依赖同名的变量（如
            //   `const Vec3 = require("vec3")` —— `require("vec3")` 返回的是**工厂函数**、
            //   `require("vec3").Vec3` 才是类），就会**遮蔽掉整个 programs 看到的那个绑定**，
            //   于是像 placeItem 的 `position instanceof Vec3` 会变成「instanceof 工厂函数」
            //   → 恒 false → LLM 传了一个完全合法的 Vec3 却被告知 "must be a Vec3"。
            //   `Cannot access 'Vec3' before initialization` 是同一根因的另一副面孔（TDZ：
            //   原语在它那句 const 之前被调用）。
            //   受影响的不止 placeItem：Vec3 被多个原语依赖、mcData 同样被多个依赖，
            //   LLM 一次同名声明就能让它们**集体中毒** —— 这是个由生成代码触发的潜伏全域故障。
            //   修法选嵌套 IIFE 而不是逐个原语改名：一行覆盖全部原语、且对**未来新增的**任何
            //   同名依赖都免疫（改名只能挡住今天已知的 Vec3/mcData 两个）。
            //   ⚠️ 包裹串**不能在 code 之前引入换行**：handleError 靠
            //   `programs.split("\n").length` 反算 LLM 代码行号，多一行整张映射就错位
            //   （同 processShim 那处的教训）。
            // ★ 注入循环体 yield（防 while(true){asyncApi()} 纯同步死循环锁死事件循环 → keepalive
            //   饿死断线，见 injectYieldIntoLoops 注释）。行内插入不增行数，handleError 行号映射不受影响。
            //   注：注入只作用在 LLM 的 code 上，programs（原语定义）是可信基建代码不注入。
            const codeYielded = injectYieldIntoLoops(code);
            const codePromise = eval(
                "(async () => {" + processShim + programs +
                "\nawait (async () => {" + codeYielded + "\n})()})()"
            );
            const deathPromise = new Promise((resolve) => {
                const iv = setInterval(() => {
                    // ★ bot 已断开（被踢/onDisconnect 置 null，见 otherError 的 null 兜底注释）时，
                    //   这轮询本就无意义——不清掉会每 300ms 访问 bot.health 抛 TypeError，虽被
                    //   otherError 兜住不崩进程，但刷屏 + 浪费。判空即当「死亡」处理：清 interval、
                    //   resolve("__DIED__") 让 evaluateCode 的 Promise.race 走 步末清理 返回，别空转。
                    if (!bot) {
                        clearInterval(iv);
                        resolve("__DIED__");
                        return;
                    }
                    if (bot.openkageDied || (typeof bot.health === "number" && bot.health <= 0)) {
                        bot.openkageDied = true;
                        clearInterval(iv);
                        resolve("__DIED__");
                    }
                }, 300);
                codePromise.finally(() => clearInterval(iv)).catch(() => {});
            });
            // ★ **步内日界熔断**（护栏 A，2026-09-10 改为 worldDay 滚动口径）：
            //   一旦本步内观察到 worldDay **滚动**（= 真正穿过 24000/0），立刻结束这一步，让
            //   Python 侧在紧接着的步边界上看到 cur_day > last_day，正常触发「冻结 → 反思 → 解冻」。
            //
            //   ★ 日界口径与 env.py 对齐：日界 = 24000/0（worldDay 滚动），**不是** phase 的夜→昼
            //     跳变。sunrise 段现对齐官方 = [22200,24000)（历史上阈值在 22000/23000 间反复），但无论
            //     定哪都只是「首次天亮」不是下一天临界点 —— sunrise 段(22200→24000)这段清晨属于当天，
            //     bot 夜结束后可继续 ReAct 直到 worldDay 真正滚动。旧的「夜→昼跳变」判据会把日界提前，已废弃。
            //
            //   ⚠️ 仍必须是「跳变」(worldDay 严格 +1)，不能是「现在 worldDay 是多少」：
            //     白天起步时 prev 已是当天 worldDay，若用绝对值判会立刻自杀；跨轮询记住 prev、
            //     只在 worldDay 增大那一刻触发，才能抓住「白天起步、跨过午夜、又在白天结束」这种
            //     真正吞日的形态（step 从 noon 跑过整个 24000/0 到次日 noon）。
            //
            //   · 轮询而非监听 `time` 事件：mineflayer 每 tick emit time，挂监听器等于每 tick 跑一次；
            //     1s 一次足够（跨午夜窗口宽裕，慢一秒不影响判定）。
            //   · bot.time.day 读取异常（掉线瞬间）时 typeof !== "number" → 不触发也不更新 prev
            //     （同旧 isDaytimeTick 缺采样的处理），不误判。
            //   · 与 deathPromise/timeoutPromise 同构：清理挂在 codePromise.finally 上。
            const dayBreakPromise = new Promise((resolve) => {
                // ★ bot 断开（onDisconnect 置 null）时本 setInterval 仍跑、读 bot.time 抛 TypeError
                //   （同 makeTickTimeoutPromise / deathPromise 的 null 兜底，见 otherError 注释）。
                //   判空即当「天亮」处理：bot 都断了这步本就该结束。初始化 prevDay 也要判空。
                let prevDay = (bot && bot.time && typeof bot.time.day === "number") ? bot.time.day : null;
                const iv = setInterval(() => {
                    if (!bot) { clearInterval(iv); resolve("__DAYBREAK__"); return; }
                    const d = bot.time?.day;
                    if (typeof d !== "number") return;
                    if (prevDay !== null && d > prevDay) {   // worldDay 滚动：穿过 24000/0
                        clearInterval(iv);
                        resolve("__DAYBREAK__");
                        return;
                    }
                    prevDay = d;
                }, 1000);
                codePromise.finally(() => clearInterval(iv)).catch(() => {});
            });
            // 任务超时：按**游戏刻**计时（bot 自选，默认 500 刻，clamp 到 [100,3000] 刻），
            // 累加 timeOfDay 增量到 timeoutTicks 即认定「这一步动作超时」，中止等待、正常返回
            // —— bot **不掉线、不自愈重来**，只是把「超时」作为这一步的 result 反馈给它，让它下步
            // 换更小/更快的动作，或为这类慢动作申请更长的 timeoutTicks。与倍速无关（见
            // makeTickTimeoutPromise）。
            const timeoutPromise = makeTickTimeoutPromise(codePromise, timeoutTicks, bot);
            const outcome = await Promise.race([
                codePromise.then(() => "success").catch((e) => e),
                deathPromise,
                timeoutPromise,
                dayBreakPromise,
            ]);
            // timeoutPromise 的 interval 在 makeTickTimeoutPromise 内部自管、挂在 codePromise.finally
            // 上清理（同 death/dayBreak），这里不再有外层 timer 要清。
            if (outcome === "__DIED__") {
                // ★ 死亡即冻结 + 等孤儿收尾 + 等重生完成（2026-09-15，治死亡循环 + 位置不可靠）。
                //   ① abortInFlightActions()：清 pathfinder goal / collectBlock / 控制态。goto 型孤儿
                //     即被 GoalChanged reject 杀死（goto.js 的 goalChangedListener），不跨步。
                //   ② pauseBot()：/tick freeze 冻怪 AI + physicsEnabled=false。**死亡循环的根因**是
                //     熔断后世界仍解冻——僵尸继续攻击 + 死亡到 Python 侧 pause 的往返窗口里世界在动，
                //     形成"重生即被杀"（dstlow3 alice step25 一次死 6 次）。死亡瞬间就冻，省掉往返窗口。
                //     pauseBot 幂等：Python 侧死亡 pause(env.py:3632) 会再冻一次，无害；下步 resume
                //     照常解冻 + 补发倍速（resumeBot 已处理 rate 恢复）。
                //   ③ 有界等待收敛死亡后的两个异步状态再返回（Promise.race 套 8s 墙钟兜底）：
                //      a) 孤儿死透：physicsEnabled=false 后，循环型孤儿卡在 injectYield 注入的
                //         `await bot.waitForTicks(1)`（physicsTick 不再 emit，physics.js:86），
                //         waitForTicks 内置超时 ticks×间隔+5s 后 reject（physics.js:464）→ codePromise
                //         落定。这是**协作式取消**——JS 无强制 promise 取消（已 web 搜索确认），
                //         项目用 physicsEnabled+waitForTicks 实现等效 AbortSignal 检查点。
                //      b) 重生完成：bot.respawn() 只发 client_command 包（health.js:26），服务器回包
                //         后才更新 entity.position 到重生点、emit 'spawn'。**等它保证 observe 取到
                //         重生点位置**（非死亡现场旧位置——alice "重生位置 位置未知"日志即没等重生）。
                //      8s 兜底：孤儿若是纯墙钟 Promise（无 waitForTicks 检查点）不会 reject，重生包
                //      极端情况也可能慢——不为它们拖死 HTTP（STEP_TIMEOUT=300s 足够余量），到点照样返回。
                abortInFlightActions();
                pauseBot();
                await Promise.race([
                    Promise.all([codePromise.catch(() => {}), _waitForRespawn(bot)]),
                    new Promise((r) => setTimeout(r, 8000)),
                ]).catch(() => {});
                bot.chat("我被击杀了，已重生。");
                return "success";  // observe 此时拿重生点位置 + 冻结静止现场，孤儿已死透不复活串扰下步
            }
            if (outcome === "__DAYBREAK__") {
                // 跨过午夜（worldDay 滚动 = 24000/0）：中止本步、干净返回，让 Python 侧在步边界上
                // 看到 cur_day > last_day 并进入反思。
                // **同 __TIMEOUT__ 一样必须真中止**：Promise.race 不取消输家，那段还在等的
                // waitUntil 会继续操纵 bot（孤儿 log 串到后面几步、位置还在漂）。
                abortInFlightActions();
                // 明确告诉 bot 这一步是**因为跨过午夜**被收掉的，不是它的代码错了、也不是超时。
                // 不说清楚它会把这次提前返回误读成「waitUntil 又失灵了」，然后去调 maxSeconds
                // （2026-08-13 那轮它就反复在错误的方向上折腾 timeout_ticks）。
                bot.chat(
                    "跨过午夜了 —— 这一步已提前收尾（不是超时、也不是报错）。" +
                    "一天满 24000 刻，本轮探索会在这里结束并进入复盘。"
                );
                return "success";
            }
            if (outcome === "__TIMEOUT__") {
                // **真的中止它**：Promise.race 不取消输家，JS 也没有 promise 取消机制
                // —— 若只是「不等了」，那段代码仍会在后台操纵 bot（孤儿的 console.log 可能串到
                // 后面几步的观测里，位置也在漂）。所以主动掐断 bot 的进行中动作，让孤儿失去执行能力、
                // 下次 await 醒来时自行退出。**不关 physicsEnabled**：本步之后 bot 要继续正常行动，
                // 这里只清「进行中的动作」，与黎明反思的 pauseBot()（连物理一起停）不同。
                abortInFlightActions();
                // 把实际生效的时限告诉 bot（它可能申请了 X 但被 clamp 成 Y），并提示可加大 timeoutTicks。
                // ★ 只说刻、不说秒——Agent 生活在 MC 时间里，对它而言世界以刻流逝，没有「真实秒」。
                //
                // ★★ **分档提示**（2026-09-07，injectYield 修复后）：修复前超时几乎只剩「纯同步死循环
                //   锁死事件循环」一种成因（撞 Python 300s ReadTimeout，那边才点名死循环）；injectYield
                //   修复后 while 体每轮让出，死循环不再锁死事件循环，而是**像一个跑太久的动作**在申请刻数
                //   内被刻超时正常拦住 → 超时变成常态化的「动作太慢」（goto 太远、挖一大片、waitForTicks 久）。
                //   故不再无差别贴「疑似死循环」——那会让正常的慢动作超时被误判。但仍有一类超时指向死循环：
                //   **申请了很长的 timeoutTicks 仍超时**。正常慢动作 Agent 会逐步加大申请直到够用；死循环
                //   每轮让出却永不退出，申请再长也不够。判据：申请 ≥ 2000 刻（1x=100s、2x=50s，已是「很久」）
                //   仍超时 → 追加「检查循环退出条件」。阈值远低于 MAX(3000)，给正常慢动作留够余量、降误报。
                const _TIMEOUT_DEADLOOP_HINT_TICKS = 2000;
                const _deadloopHint = timeoutTicks >= _TIMEOUT_DEADLOOP_HINT_TICKS
                    ? " 你申请了很长的时限仍超时——代码可能在 while/for 里没有可靠的退出条件"
                      + "（break/return/计数器到顶），检查循环是否真能结束，别再原地空转。"
                    : "";
                bot.chat(
                    `上一步动作超时了（执行超过 ${timeoutTicks} 刻，已中止并停下当前动作）。` +
                    `下一步请做更小、更快的单个动作，或在 timeout_ticks 里申请更长时限（上限 ${
                        STEP_CODE_TIMEOUT_MAX_TICKS} 刻）。` +
                    _deadloopHint
                );
                return "success";  // 超时不算执行错误：作为本步 result 反馈，bot 在原地继续下一步
            }
            // ★ **reject 路径也要 abort**（2026-08-21 修，对应 heal 死循环事故）：
            //   上面三条字符串哨兵分支里，__TIMEOUT__/__DAYBREAK__ 都调了 abortInFlightActions()
            //   清掉残留 goal；唯独「生成代码 reject」（goto 抛 `Took to long to decide path` 等）
            //   走这条 `return outcome` —— outcome 是个 Error 对象，**之前没 abort**。
            //
            //   后果：goto reject 时 pathfinder 的 monitorMovement 仍挂在 physicsTick 定时器上、
            //   拿着**同一个没被清掉的 goal** 每 tick 重新问路 → 再次抛 `Took to long`。这第二次
            //   抛已不在 /step 的 await 链上（在 tick 定时器回调里），evaluateCode 的 try/catch 和
            //   Promise.race 都抓不到 → 被顶层 unhandledRejection handler 兜住，且伴随
            //   `Cannot read properties of undefined (reading 'healthy')`（computeWorldHealth 在
            //   bot 已被置 null 时读 .healthy）。Python 侧每 60s ReadTimeout → _heal_and_restart
            //   重连 → 重连后旧 bot 的残留 goal 没清 → 新 bot 一上线旧 goal 继续每 tick 抛 →
            //   立刻又一轮，无限循环。
            //
            //   修法：reject（outcome 是 Error）时与超时分支对称地 abort 一次，把残留 goal 清掉，
            //   掐断「reject 后每 tick 重抛」的源头。outcome 是字符串（"success"）时 bot 已正常停下，
            //   不该再 abort——故只在 Error 时做。abort 自身 best-effort（bot 为空直接返回）。
            if (outcome instanceof Error) {
                abortInFlightActions();
            }
            return outcome;
        } catch (err) {
            // ★ 同理：evaluateCode 自身抛错（outcome race 之外的异常）也要 abort 清残留 goal，
            //   与上面 reject 路径对称，避免同类死循环从这条路径复现。
            abortInFlightActions();
            return err;
        } finally {
            // 清位：超时/异常路径也要走到，否则后续原语发指令会被误拦
            bot.openkageInGeneratedCode = false;
        }
    }

    // console 劫持：本步内把 console.log/info/warn 转存进 onLog 插件，
    // 步末随 observe 回传给 LLM。返回解除函数（务必调用，否则 console 永久被占）。
    // 仍然透传给原始 console：node 侧 stdout 保留一份，便于人工排查（Python 侧丢弃它无妨）。
    //
    // **故意不劫持 console.error**：顶层 uncaughtException/unhandledRejection 兜底用的正是
    // console.error，而 pathfinder 之类的框架异常会每 physics tick 重抛（一次坏 goal 能刷出
    // 数千条）。劫持它会让这些框架噪音瞬间填满 onLog 的 40 行配额，把 LLM 自己写的 log 挤掉。
    // LLM 用 console.error 汇报的场景少见，不值这个代价。
    function captureConsole() {
        const original = {
            log: console.log,
            info: console.info,
            warn: console.warn,
        };
        const sink = (level, orig) => (...parts) => {
            try {
                if (bot && bot.openkageLog) {
                    const text = parts
                        .map((p) => {
                            if (typeof p === "string") return p;
                            try {
                                return JSON.stringify(p);
                            } catch (e) {
                                return String(p);  // 循环引用等：退回 toString
                            }
                        })
                        .join(" ");
                    bot.openkageLog.push(level === "log" ? text : `[${level}] ${text}`);
                }
            } catch (e) {
                // 捕获失败绝不能影响生成代码执行
            }
            orig.apply(console, parts);
        };
        console.log = sink("log", original.log);
        console.info = sink("info", original.info);
        console.warn = sink("warn", original.warn);
        return function release() {
            console.log = original.log;
            console.info = original.info;
            console.warn = original.warn;
        };
    }

    // onStuck / teleportBot（Voyager 的卡住自动传送）已整体删除。
    // 删除理由见上方 onTick 内的注释块（简言之：OpenKAGE无 task 边界不需要防死锁兜底，而它静默挪动
    // bot 位置会诱发误判）。**不要照 Voyager 原版加回来。**

    // Voyager 的 `returnItems`（task 间隔离逻辑）已**整体删除**。
    //
    // 它原本每步拆掉附近 128 格内的工作台/熔炉再 `/give` 回库存，为的是「上一个 task 放的方块
    // 不影响下一个 task」。OpenKAGE不需要，而且有害：
    // ① OpenKAGE是**连续自由探索**（一个世界跑满 max_nights，无 task 边界），bot 自己放的工作台本该
    //    留在世界里成为它的据点 —— 每步拆掉反而破坏空间记忆的沉淀、让它反复重放工作台白费步数；
    // ② 它原先就**从未真正生效**：头尾两行发的是 `/gamerule doTileDrops`，而 MC 1.21.11 已改名为
    //    `block_drops`（服务器回 `Incorrect argument for command`），即「防刷物品」的保护一直是空的，
    //    而拆台 `/give` 却在真执行；
    // ③ **它是作弊面**：函数体里有 6 处 `/give @s`，包括直接白送 `iron_pickaxe` / `crafting_table`。
    //    留着一个「会给 bot 白送铁镐」的函数是实验效度上的隐患 —— 万一日后被误接回去，分数就废了
    //    且极不容易发现（我们测的是探索能力，不是"被喂了多少装备"）。
    // 真要对齐 Voyager 行为，去 Voyager 原仓取，别在这里留一把上了膛的枪。

    function handleError(err) {
        let stack = err.stack;
        if (!stack) {
            return err;
        }
        console.log(stack);
        const final_line = stack.split("\n")[1];
        const regex = /<anonymous>:(\d+):\d+\)/;

        const programs_length = programs.split("\n").length;
        let match_line = null;
        for (const line of stack.split("\n")) {
            const match = regex.exec(line);
            if (match) {
                const line_num = parseInt(match[1]);
                if (line_num >= programs_length) {
                    match_line = line_num - programs_length;
                    break;
                }
            }
        }
        if (!match_line) {
            return err.message;
        }
        let f_line = final_line.match(
            /\((?<file>.*):(?<line>\d+):(?<pos>\d+)\)/
        );
        if (f_line && f_line.groups && fs.existsSync(f_line.groups.file)) {
            // ★ **不要把服务器的绝对路径和内部源码行丢给 LLM**：
            //   若这里输出 `/path/to/index.js:461` + 那一行源码（护栏自己的实现细节），
            //   对 LLM 毫无可操作性：它改不了 index.js，却要为这两行付 onLog 配额，
            //   还容易被误导去"修"一个不属于它的文件。
            //   真正有用的是**它自己那一行代码** + 错误消息本身（护栏的消息已写得很清楚）。
            //   只保留一个简短来源标记，便于人排查时知道错误来自环境层而非生成代码。
            const { file, line } = f_line.groups;
            const where = require("path").basename(file);
            const lines = code.split("\n");
            const idx = Math.min(Math.max(match_line, 1), lines.length);
            // ★ 环境层主动抛的「干净」错误（goto 无可达路径、GoalBreakBlock 废弃等，err._openkageClean=true）
            //   消息本身已完整客观，不加 `(raised by the environment in index.js:N)` 后缀——那行泄露内部
            //   文件名+行号，对 agent 无可操作性还占 onLog 配额（违背上方注释初衷）。
            if (err._openkageClean) {
                return (
                    `Your code:${idx}\n${(lines[idx - 1] || "").trim()}\n ` +
                    err.message
                );
            }
            return (
                `Your code:${idx}\n${(lines[idx - 1] || "").trim()}\n ` +
                err.message
            );
        } else if (
            f_line &&
            f_line.groups &&
            f_line.groups.file.includes("<anonymous>")
        ) {
            // ★ **报错定位必须只用 match_line 一个坐标系**：混用两个来源不同、含义不同的
            //   行号会产出错的提示：
            //   · `match_line` 来自**第一个 `>= programs_length` 的栈帧**，已减去偏移，
            //     是「LLM 代码的第几行」——正确的那个；
            //   · `line` 来自 `final_line`（**栈顶那一帧**），是未减偏移的 eval 内绝对行号，
            //     而栈顶通常在**原语内部**（LLM 调 mineBlock 时栈顶是 mineBlock 的某行）。
            //   若用 `line < programs_length` 判「错在 programs 里」再拿 `line` 去索引 programs，
            //   等于用「栈顶绝对行号」去查一张「programs 相对行号」的表 —— 必然指到无关的行
            //   （如把错在 code 第 1 行的 bug 报成 craftHelper 里的一句注释）。
            //   现在统一只报 `match_line` 对应的 LLM 代码行；原语内部栈帧对 LLM 无意义，不再展示。
            const lines = code.split("\n");
            const idx = Math.min(Math.max(match_line, 1), lines.length);
            return (
                `Your code:${idx}\n${(lines[idx - 1] || "").trim()}\n ` +
                err.message
            );
        }
        return err.message;
    }
});

app.post("/stop", (req, res) => {
    // bot 可能已为 null（连接失败走 onConnectionFailed、或前一次 /start 重启途中）——
    // 直接 bot.end() 会抛 TypeError，被 Python 侧 shutdown() 的 except 吞掉，表现为「自愈失败后
    // 关不干净」。判空后幂等返回。
    if (!bot) {
        res.json({ message: "Bot already stopped" });
        return;
    }
    bot.end();
    bot = null;
    res.json({
        message: "Bot stopped",
    });
});

// 掐断 bot「进行中的动作」，不动物理开关、不碰服务器 tick。超时（/step）与暂停（黎明反思）共用。
// 返回做了哪几件事，便于日志/排查。
//
// ★ 顺序照 mineflayer-pathfinder 源码定：setGoal(null) 必须先于任何「停 tick / 关物理」的动作。
//   setGoal(null) → resetPath → fullStop() 是**同步**的（清 control states + 水平速度归零）；
//   而 pathfinder.stop() 只置 stopPathing 标志、要等下一个 physicsTick 的 monitorMovement 才处理
//   —— 若先关物理/冻结，那个 tick 永远不来，goal 就卡住了。故用 setGoal(null)。
function abortInFlightActions() {
    const done = [];
    if (!bot) return done;
    try {
        // ⓪ 先跑原语自己登记的中止钩子（目前只有 exploreUntil）。**必须最先**：它持有一个每 2 秒
        //    重设 goal 的 setInterval，光在下面 setGoal(null) 一次是没用的 —— 下个 interval tick
        //    又把 goal 设回去。钩子会 clearInterval + resolve，让等它的孤儿代码立刻往下走完。
        if (bot.openkageAborts && bot.openkageAborts.size) {
            const hooks = Array.from(bot.openkageAborts);
            bot.openkageAborts.clear();
            for (const h of hooks) {
                try {
                    h();
                } catch (e) {
                    console.error("[abort] 原语中止钩子出错：", e && e.message);
                }
            }
            done.push(`primitive_hooks(${hooks.length})`);
        }
        // ①-pre 掐掉在飞的**异步路径预算**（setImmediate 驱动的 A*，见守卫层
        //    `_precompute`）。放在 setGoal(null) **之前**：那条 setGoal 本身也会作废预算
        //    （包裹里调了 _cancelPrecompute），但显式先做一次更稳 —— setGoal 万一抛错
        //    （如 pathfinder 未就绪）就轮不到它了，而在飞的搜索必须停。
        if (typeof bot.__openkageCancelPrecompute === "function") {
            try {
                bot.__openkageCancelPrecompute();
                done.push("precompute_cancelled");
            } catch (e) {
                /* ignore */
            }
        }
        // ① 掐断寻路意图（同步 fullStop）
        if (bot.pathfinder) {
            bot.pathfinder.setGoal(null);
            done.push("pathfinder_goal_cleared");
        }
        // ② 停 collectBlock 的批量任务（mineBlock 走的就是它）。**不 await**：cancelTask 等的是
        //    collectBlock_finished 事件，而该事件依赖 tick 推进；暂停场景下已 freeze，await 会挂死。
        //    置标志即可，孤儿醒来自行退出。
        if (bot.collectBlock && typeof bot.collectBlock.cancelTask === "function") {
            bot.collectBlock.cancelTask().catch(() => {});
            done.push("collectblock_cancelled");
        }
        // ②-bis ★ **强制清空 targets**：上面 cancelTask 只是 pathfinder.stop() + 等
        //   `collectBlock_finished`，而**那个事件由 collect() 的 finally 发出**。超时时孤儿正卡在
        //   collect() 内部的 await 上，永远走不到 finally → 事件永不 emit → `targets` 永不 clear。
        //   于是**下一步**再调 mineBlock 时，collect() 第一行 `await this.cancelTask()` 看到
        //   targets 非空，就 `await once(bot,'collectBlock_finished')` —— 等一个永远不会来的事件，
        //   /step 彻底无响应，Python 侧只能干等满 STEP_TIMEOUT=300s 再自愈重连。
        //   清空 targets 让下一步的 cancelTask 走 `targets.empty` 那条快速返回分支。
        //   直接动私有字段是有意的：CollectBlock 没有对外的"强制重置"API，而留着这个悬挂状态
        //   会让**后续每一步**都卡死，代价远大于碰一次私有实现。
        try {
            const cb = bot.collectBlock;
            if (cb && cb.targets && typeof cb.targets.clear === "function") {
                cb.targets.clear();
                done.push("collectblock_targets_cleared");
            }
            // 再补发一次 `collectBlock_finished`：万一已经有人卡在 `await once(...)` 上
            // （上一步的孤儿、或恰好并发进来的调用），清 targets 并不能唤醒它们 ——
            // once 监听的是事件本身，不会因为 targets 空了就 resolve。补发一次把它们放出来。
            bot.emit("collectBlock_finished");
        } catch (e) {
            console.error("[abort] 清 collectBlock.targets 失败：", e && e.message);
        }
        // ③ 停挖掘：bot.dig 有自己的计时器，不完全跟 physicsTick 走 → 必须显式停。
        //    stopDigging 会让进行中的 dig promise reject（'Digging aborted'），那是预期行为。
        if (bot.targetDigBlock && typeof bot.stopDigging === "function") {
            try {
                bot.stopDigging();
                done.push("digging_stopped");
            } catch (e) {
                /* 预期：孤儿的 catch 会吃掉这个 reject */
            }
        }
        // ④ 清残留控制输入（即使没在寻路，LLM 也可能自己 setControlState('forward', true)）
        //
        if (typeof bot.clearControlStates === "function") {
            bot.clearControlStates();
            done.push("control_states_cleared");
        }
        // ⑤ 关残留容器窗口（2026-09-08）：LLM 步内 `bot.openContainer/openFurnace` 后没关、或
        //    异常中断留下 `bot.currentWindow` 非空，下一步再开窗会撞 "windowOpen did not fire"
        //    （bob step352 即此）。这里兜底关掉。**不关正在 transfer 的窗口**——那靠 ⑤-bis 的
        //    window_click 熔断拦在风暴起头；这里只处理「开了没关」的静默残留，关窗本身同步安全。
        if (bot.currentWindow && typeof bot.closeWindow === "function") {
            try {
                bot.closeWindow(bot.currentWindow);
                done.push("leftover_window_closed");
            } catch (e) {
                /* 残窗关失败不致命，吞掉 */
            }
        }
    } catch (e) {
        console.error("[abort] 掐断进行中动作时出错：", e && e.message);
    }
    return done;
}

// ── 真暂停 / 恢复 ────────────────────────────────────────
//
// **为什么需要「两侧」暂停**：`/tick freeze` 冻结服务器 tick loop，但 Minecraft 官方语义是
// "All gameplay elements are not ticked, **except players** and any entity a player is riding"
// （minecraft.wiki/w/Commands/tick）—— bot 就是 player。玩家移动是**客户端权威**的（客户端算好
// 位置发包给服务器），服务器无论怎么冻结自己都拦不住一个客户端继续发 position 包。这不是漏洞，
// 是架构必然：**没有任何服务器命令能暂停 bot**（`/pause` 不存在，Voyager 原版发的那句依赖它自带的
// fabric mod；1.21.2+ 的 `pause-when-empty-seconds` 只在**无人连接**时触发，bot 全程连着 → 永不
// 生效；`/tick rate 1` 同样对玩家无效）。
//
// 而 bot 的「移动意志」根本不在服务器，在**我们自己进程里的 mineflayer 物理模拟**
// （node_modules/mineflayer/lib/plugins/physics.js:81 `if (bot.physicsEnabled && shouldUsePhysics)`
// → simulatePlayer().apply(bot)）。所以客户端侧掐断 `bot.physicsEnabled = false` 才是缺失的那一半。
//
// **为什么需要它**：/step 的超时用 Promise.race 实现，而 JS **没有 promise 取消机制** —— 超时只是
// 让 /step 提前返回，那段生成代码仍在后台跑（超时步的孤儿代码会继续执行，位置和日志都会在后续
// 步之间漂移）。这些孤儿会在黎明反思的几十秒里继续挖矿/寻路，污染反思读到的轨迹与 tier 首达步序。
function pauseBot() {
    if (!bot) return { paused: false, reason: "no bot" };
    const done = [];
    try {
        // ① 服务器侧：怪/红石/时间/天气/掉落方块 停（bot 不挨打、世界时间不流逝）
        //   ★ 走 bot._chat（skillLoader 包装前的原始 chat，见 skillLoader.js:154）：pauseBot 在
        //   __DIED__ 分支被调时仍处 evaluateCode 函数体内（finally 未到），openkageInGeneratedCode
        //   还是 true，bot.chat 的 /tick 会被命令拦截层 block（7cc48de 实测 7/7 被拦，死亡循环没
        //   治住——世界未冻、僵尸继续攻击重生点）。_chat 是基建专用绕过通道，同 say（index.js:1565）
        //   /spectatorCam 探针（spectatorCam.js:88）/test_death_abort 的 /kill。裸 bot（无 skillLoader，
        //   如 live 测试）退回 bot.chat。resumeBot 的 /tick unfreeze 不用改——它在 /resume 端点
        //   （/step 已返回、标志已清 false），不被拦。
        const _send = bot._chat || bot.chat;
        _send.call(bot, "/tick freeze");
        done.push("tick_freeze");
        // ② 掐断一切进行中的动作（必须在关物理之前，见 abortInFlightActions 的顺序注释）
        done.push(...abortInFlightActions());
        // ③ **客户端侧：停 bot 自己的物理模拟** —— 这才是「暂停玩家」真正生效的那一下。
        //    副作用：期间 bot 不受重力（停在半空不会掉，恢复后才继续掉）。对反思期反而是好事
        //    （不会摔死），但语义是「bot 被抽离物理」，不是「世界静止」。
        bot.physicsEnabled = false;
        done.push("physics_disabled");
    } catch (e) {
        console.error("[pause] 暂停时出错：", e && e.message);
    }
    return { paused: true, done };
}

function resumeBot() {
    if (!bot) return { resumed: false, reason: "no bot" };
    try {
        bot.physicsEnabled = true;   // 先恢复客户端物理
        bot.chat("/tick unfreeze");  // 再解冻服务器
        // ★ OPENKAGE 倍速：反思期 /tick freeze 不保证保留 rate（不同版本行为不一，且 freeze 是
        //   持久状态），unfreeze 后世界可能降回 1 倍速 → 反思后昼夜变慢、与客户端 setTickRate
        //   不同频 → 失步。故每次 resume 都按 currentTickRate 重发 server 侧 /tick rate。
        //   客户端 setTickRate 在 pause 时未动定时器（只 physicsEnabled=false），无需重设；
        //   这里若 bot.setTickRate 仍在则幂等重设一次也无害（会重建 setInterval，确保节拍正确）。
        if (currentTickRate > 1) {
            // ★ 服务器发 20*rate TPS（= N 倍速），不是裸 rate（那是 TPS、会减速）。见 applyTickRate。
            bot.chat(`/tick rate ${20 * currentTickRate}`);
            if (typeof bot.setTickRate === "function") bot.setTickRate(currentTickRate);
            console.log(`[tickrate] resume 后补发 ${currentTickRate}x 倍速（server /tick rate ${20 * currentTickRate}）`);
        }
    } catch (e) {
        console.error("[resume] 恢复时出错：", e && e.message);
    }
    return { resumed: true };
}

// ── resume 后的「物理就绪探针」 ─────────────────────────────
//
// 取代旧 resume 端点里 `bot.waitForTicks(bot.waitTicks)`（= 等 20 个 physicsTick）的那一刀。
// 旧写法按注释说是「让世界重新动起来再观察」，但实测是误导：waitForTicks 监听的是本地
// physicsTick 事件（physics.js:459-480），而 physicsTick 由本地 doPhysics 定时器自走
// （physics.js:68，与 server /tick rate 解耦），跟服务器 unfreeze 与否无关。所以正常路径下
// 它只是在白白等 20 个本地物理帧（2.5 倍速 ≈ 0.8s、1 倍速 ≈ 1s），冻结/恢复的 gap 就叠在这里。
//
// 但它兜着一个真实的窗口，不能整段砍掉：physicsTick 只在 `bot.physicsEnabled && shouldUsePhysics`
// 都为 true 时才 emit（physics.js:86）。`physicsEnabled` 由 resumeBot() 置 true；`shouldUsePhysics`
// 正常路径恒为 true，但**死亡分支后会短暂为 false**——respawn 事件置 false（physics.js:488），
// 且 death 时置位的 respawnTimer 让重生后第一个 position_look 包延迟 1.5s 才把 shouldUsePhysics
// 拉回 true（physics.js:432-445）。死亡分支紧接着就是下步初的 resume（env.py:3505），那一刻
// shouldUsePhysics 可能还没复位 → physicsTick 暂不 emit → 旧 waitForTicks(20) 实际在等这 1.5s。
//
// 探针的正确语义就是「等 physicsTick 真的在 emit 这件事本身」——即本地物理循环已就绪——
// 而非固定凑 20 帧。正常路径第一个 physicsTick 在下一帧就到（≈ PHYSICS_INTERVAL_MS，几十 ms），
// 死亡路径才等到 shouldUsePhysics 复位。带 2s 墙钟兜底覆盖死亡那 1.5s 延迟；超时带略旧观察返回
// （与 waitTicksSafe 同理：挂死 HTTP 不可接受，少一两帧无妨，下个 /step 会再 observe）。
const RESUME_READY_TIMEOUT_MS = 2000;
function _waitForPhysicsReady(bot) {
    return new Promise((resolve) => {
        let settled = false;
        // listener 必须是命名引用——finish 里 removeListener 删的就是它。若像首版那样
        // `bot.once("physicsTick", () => {...})` 注册匿名箭头，超时兜底路径的 removeListener
        // 因引用不匹配删不掉它 → 每次超时泄漏一个 once 监听器（test_physics_ready_probe 用例③抓出）。
        const listener = () => {
            if (settled) return;
            settled = true;
            clearTimeout(timer);
            bot.removeListener("physicsTick", listener);
            resolve();
        };
        const timer = setTimeout(() => {
            console.log(`[resume] 物理就绪探针超过 ${RESUME_READY_TIMEOUT_MS}ms 未收到 physicsTick`
                + "（可能紧跟死亡重生，shouldUsePhysics 尚未复位），带当前观察继续");
            listener();
        }, RESUME_READY_TIMEOUT_MS);
        bot.once("physicsTick", listener);
    });
}

// 等 bot 重生完成（emit 'spawn'）。供 __DIED__ 分支用：死亡是异步的——bot.respawn() 只发
// client_command 包，服务器回包后才更新 entity.position 到重生点、emit 'spawn'（health.js:32）。
// 死亡熔断那一刻 bot.entity.position 还停在死亡现场，不等重生完成就 observe 会拍到旧位置。
// 带 3s 墙钟兜底：spawn 事件在 update_health 回调里 emit，正常几百毫秒；兜底防极端网络下不挂死
// 死亡步（外层 __DIED__ 还套了 8s race，这里超时只是早一步 resolve、不致命）。
function _waitForRespawn(bot) {
    return new Promise((resolve) => {
        if (bot && bot.isAlive) return resolve();  // 已重生（health 轮询命中时 respawn 可能已完成）
        let done = false;
        const onSpawn = () => { if (!done) { done = true; resolve(); } };
        const t = setTimeout(onSpawn, 3000);
        if (bot) bot.once("spawn", () => { clearTimeout(t); onSpawn(); });
        else { clearTimeout(t); resolve(); }
    });
}

// /pause 与 /resume **都返回 bot.observe()**（与 /step 同形），不是随手为之：
// onSystem 累积的死亡/成就播报唯一的消费点就是 observe()。若这两个端点不 observe，
// 「bot 恰在天亮前后被打死」那条播报要等到下一次 /step 才被取走 —— 而那已经在黎明反思**之后**，
// 于是这次死亡永远进不了当夜反思（正是 env.py 那边刚修好的漏洞，别在这里又漏回去）。
// flush 也要显式调：base.js 的 bot.event("observe") 会跳过名字以 "on" 开头的插件。
function observeWithFlush() {
    if (bot.openkageSystem) bot.openkageSystem.flush();
    if (bot.openkageLog) bot.openkageLog.flush();
    // onChat 同上：攒着的原语反馈在这一刻取走，否则要等下一次 /step（且会被步首 reset 丢掉）。
    if (bot.openkageChat) bot.openkageChat.flush();
    return bot.observe();
}

app.post("/pause", (req, res) => {
    if (!bot) {
        res.status(400).json({ error: "Bot not spawned" });
        return;
    }
    const r = pauseBot();
    console.log("[pause]", JSON.stringify(r));
    // **不 waitForTicks**：已经 freeze 了，tick 不再推进，等它会挂死
    // （Voyager 原版那句 `bot.waitForTicks(...).then(...)` 放在 freeze 之后就是个死等）。
    res.json(observeWithFlush());
});

app.post("/resume", (req, res) => {
    if (!bot) {
        res.status(400).json({ error: "Bot not spawned" });
        return;
    }
    const r = resumeBot();
    console.log("[resume]", JSON.stringify(r));
    // ★ 不再 waitForTicks(20)：旧写法按注释是「让世界动起来再观察」，实为误导——它等的是本地
    //   physicsTick（与 server unfreeze 解耦），正常路径白白等 20 帧叠出冻结/恢复 gap。改用
    //   就绪探针等「physicsTick 真的在 emit」本身：正常路径下一帧即返（几十 ms），死亡分支后才
    //   等到 shouldUsePhysics 复位（physics.js 1.5s 延迟），2s 兜底绝不挂死 HTTP。与 /pause 端点
    //   对称（pause 不 wait，见其注释）。详见 _waitForPhysicsReady。
    _waitForPhysicsReady(bot).then(() => res.json(observeWithFlush()));
});

// **/unfreeze：无条件补发 /tick unfreeze，不受 `if (!bot)` 拦截**（与 /resume 的关键区别）。
//
// 为什么需要它（2026-08-19 alice 第7天冻死事故）：
//   `/tick freeze` 是 **MC 服务器侧持久状态**，不随 mineflayer bot 重连复位。而
//   `_heal_and_restart`（Python 侧自愈）只发 /start 重连 bot —— `/start` 的 spawn handler
//   从不发 /tick unfreeze。于是当 resume 时 bot 已掉线 → /resume 返 400（被 if(!bot) 挡）→
//   自愈只把 bot 接回来、世界仍冻着 → timeTicks 恒定 → stall 护栏 abort 整轮。
//
//   这条链路需要一个「不依赖 bot 是否在线」的解冻入口做兜底。但 mineflayer 侧的 /tick 命令
//   只能由在线的 bot 代发（bot.chat），所以本端点在 **bot 在线** 时才真正生效。
//   时序保证它被调用时 bot 一定在线：`_heal_and_restart` 是先 `_do_start` 重连 bot 成功、
//   bot 已 spawn（非空）**之后**才调本端点 —— 那时 bot.chat("/tick unfreeze") 一定能发出。
//   bot 为空时回 200 但 unfrozen:false（说明连 bot 都没接上，是更大的故障，由上层 /step 重试
//   与 ReadTimeout 机制兜，本端点不再强行尝试）。
//
// per-arm 安全：每条轨迹独占一台 MC server（worker_pool per-arm mc_port），给本 arm 解冻
// 不会误伤别的 arm 的世界。
app.post("/unfreeze", (req, res) => {
    if (bot) {
        try {
            bot.chat("/tick unfreeze");
            // ★ OPENKAGE 倍速：同 resumeBot，解冻后补发 rate，防世界降回 1 倍速与客户端不同频。
            //   自愈重连的 /start 虽也会 applyTickRate，但若自愈路径没带 tick_rate（或本端点
            //   被独立调用），这里兜底保证 rate 不丢。
            if (currentTickRate > 1) {
                // ★ 服务器发 20*rate TPS（= N 倍速），不是裸 rate。见 applyTickRate。
                bot.chat(`/tick rate ${20 * currentTickRate}`);
                if (typeof bot.setTickRate === "function") bot.setTickRate(currentTickRate);
            }
        } catch (e) {
            console.error("[unfreeze] 发 /tick unfreeze 出错：", e && e.message);
        }
        console.log("[unfreeze] 已发 /tick unfreeze（bot 在线）");
        res.json({ unfrozen: true });
    } else {
        console.warn("[unfreeze] bot 为空，无法代发 /tick unfreeze —— 说明 bot 尚未重连成功");
        res.json({ unfrozen: false });
    }
});

// /world_health（GET）：返回 bot 当前世界是否「连着且 chunk 在」的自检快照。
//
// 用途（dave flying 踢人事故，2026-08-20）：自愈 _heal_and_restart 的**轻量分支**只重发 /start
// 重连 bot、不重启进程。重连后 bot 可能处于「半连接」——连接层活（能读 position/timeTicks）
// 但**没收到任何 chunk 数据**（world.getColumns() 返 0、blockAt 返 null、_client.ended 后续变 true）。
// 这时动作全 unknown/超时、timeTicks 恒定 → 空转护栏 abort 整轮退场。对比 carol ReadTimeout
// 自愈走「重启进程」分支，重连后世界正常恢复。Python 侧据此判定：chunk 空就降级走重启进程分支
// （见 env.py _validate_world_after_heal）。
//
// 只报事实、不下「健康/不健康」判断（与 status.js 惯例一致）：判定阈值留给 Python 侧。
//   · columns：已加载列数（prismarine-world world.js getColumns() 返数组，空数组=半连接铁证）。
//   · feet_block：bot 脚下一格的方块名；blockAt 在 chunk 没加载时返 null（mineflayer blocks.js
//     「null block means chunk not loaded」）。null=局部 chunk 缺失。
function computeWorldHealth(bot) {
    if (!bot) {
        return { healthy: false, bot_alive: false, columns: 0, feet_block: null, reason: "bot is null" };
    }
    let columns = 0;
    let feet_block = null;
    try {
        columns = (bot.world && bot.world.getColumns)
            ? bot.world.getColumns().length : 0;
    } catch (e) {
        console.error("[world_health] 读 getColumns 出错：", e && e.message);
    }
    try {
        const p = bot.entity.position.floored().offset(0, -1, 0);
        const b = bot.blockAt(p, false);
        feet_block = b ? b.name : null;
    } catch (e) {
        console.error("[world_health] 读脚下方块出错：", e && e.message);
    }
    const healthy = columns > 0 && feet_block !== null;
    const result = { healthy, bot_alive: true, columns, feet_block };
    if (!healthy) {
        result.reason = `columns=${columns} feet_block=${feet_block}`;
    }
    return result;
}

app.get("/world_health", (req, res) => {
    res.json(computeWorldHealth(bot));
});

// 顶层错误兜底：Voyager 原版无此处理，任何未处理的 rejection/exception
// （典型：远距离传送落到未生成区块时 waitForTicks 超时 reject）都会让整个进程崩掉，进而拖垮
// 整轮探索。这里兜住，只打日志不退出——让进程存活，Python 侧 /step 拿到后续错误再决定重试/自愈。
//
// **坏 pathfinder goal 自清**：LLM 若给 bot.pathfinder.setGoal() 传了不合规
// 对象（如裸 Vec3 / {x,y,z}，而非 new GoalNear(...)），pathfinder 的 monitorMovement 会在**每个
// physics tick** 调 stateGoal.isValid() 抛 TypeError。该异常不在 /step 的 await 链上（在 tick
// 定时器回调里），evaluateCode 的 try/catch 和 Promise.race 都抓不到，于是被本 handler 兜住 →
// 坏 goal 永远留在 pathfinder 里，每 tick 重抛，直到进程重起，可以短时间内刷出大量日志。
// 所以这里识别出这类异常就主动清掉 goal，掐断刷屏源头。
function isBadGoalError(err) {
    const msg = (err && err.message) || "";
    return /stateGoal\.\w+ is not a function/.test(msg) ||
           /Cannot read propert.* of (null|undefined).*stateGoal/.test(msg);
}

function clearBadGoal(err) {
    if (!isBadGoalError(err)) return false;
    try {
        if (bot && bot.pathfinder) {
            bot.pathfinder.setGoal(null);
            // 同一个异常会被派发给所有 uncaughtException 监听者（/step 的 otherError + 顶层兜底），
            // 故按异常对象去重，只清一次、只提示 LLM 一次，别浪费 onLog 配额。
            if (err && err.__openkageGoalHandled) return true;
            if (err && typeof err === "object") err.__openkageGoalHandled = true;
            console.error("[pathfinder] 检测到不合规 goal（", err.message,
                          "），已 setGoal(null) 清除，掐断每-tick 重抛");
            if (bot.openkageLog) {
                // 让 LLM 知道自己传错了 goal——否则它只看到「没走过去」，无从修正
                bot.openkageLog.push(
                    "[warn] 你给 bot.pathfinder.setGoal() 传了不合规的 goal 对象，已被清除。" +
                    "goal 必须是 mineflayer-pathfinder 的 Goal 实例（如 new GoalNear(x,y,z,range)），" +
                    "不能直接传 Vec3 或 {x,y,z}。建议改用 bot.pathfinder.goto(new GoalNear(...))。"
                );
            }
        }
    } catch (e) {
        console.error("[pathfinder] 清除坏 goal 失败：", e && e.message);
    }
    return true;
}

process.on("uncaughtException", (err) => {
    if (clearBadGoal(err)) return;  // 已处理，不再刷「已兜住」那行
    console.error("[uncaughtException 已兜住，不退出]", err && err.message);
});
process.on("unhandledRejection", (reason) => {
    if (clearBadGoal(reason)) return;
    console.error("[unhandledRejection 已兜住，不退出]", reason && (reason.message || reason));
});

// Server listening to PORT 3000

const DEFAULT_PORT = 3000;
const PORT = process.argv[2] || DEFAULT_PORT;
app.listen(PORT, () => {
    console.log(`Server started on port ${PORT}`);
});
