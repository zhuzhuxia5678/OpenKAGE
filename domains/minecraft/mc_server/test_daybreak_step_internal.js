/**
 * 验证 index.js dayBreakPromise 步内熔断判据：worldDay 滚动（严格 +1）那一刻被
 * 1s 轮询抓到、提前结束"这一步"。复刻 index.js:1749-1762 的轮询循环，不依赖 index.js。
 *
 *   bash start_servers.sh --mc-only        # 起裸 MC server @ :25567
 *   node test_daybreak_step_internal.js    # mineflayer 直连
 *
 * ## 为什么有这个脚本
 *
 * 0910 日界纠正 smoke run 里，worldDay 滚动都落在**栅栏间隙**（步间 pause/resume 之间），
 * env.py 步边界判据 `cur_day > last_day` 直接接住，**dayBreakPromise 步内熔断分支一次没触发**。
 * 这不是 bug（栅栏间隙 codePromise 已 resolve、interval 已清，本就不该触发），但留下覆盖盲区：
 * "一步内跨过 24000/0"这个边角形态没被真实验证过。等价体单测（test_minecraft_day_boundary.py
 * 的 _daybreak_poll 等价体）查不出 mineflayer 真实 bot.time.day 读取 + setInterval 时序。
 *
 * 本脚本复刻 dayBreakPromise 的核心：setInterval(1000ms) 读 bot.time.day，prevDay!=null 且
 * d>prevDay 时 resolve("__DAYBREAK__")。把时间设到接近 24000（23000，day=0），让游戏自然流速
 * 推过 24000/0（day 0→1），验证轮询能在滚动那一刻抓到、且不误判（设夜后 prevDay=0 不会立刻自杀）。
 *
 * ## 输出判据
 *
 *   PROBE_SET   day=0 timeOfDay=23000（设夜后基准）
 *   PROBE_POLL  每秒轮询：day=? timeOfDay=?（应看到 day 从 0 涨到 1 的那一拍）
 *   PROBE_DAYBREAK  worldDay 滚动被抓到，__DAYBREAK__ 触发  ← 关键
 *   → 触发且触发时 day 严格 +1：步内熔断判据正确
 *   → 设夜后立刻触发（day 没涨）：误判 bug（白天起步自杀），判据错了
 *   → 超时没触发：轮询没抓到 worldDay 滚动，时序有问题
 */
const mineflayer = require("mineflayer");

const HOST = "localhost";
const PORT = 25567;
// 用户名必须是 ops.json 里的 op（level 4），否则 /time 被拒。server121/ops.json 里 "bot" 是 op。
const USER = "bot";
const log = (...a) => console.log(`[daybreak ${USER}]`, ...a);

const bot = mineflayer.createBot({
    host: HOST, port: PORT, username: USER, version: "1.21.11",
});

const wait = (ms) => new Promise((r) => setTimeout(r, ms));

// 监听 update_time 包，实时打印 day 变化（抓 worldDay 滚动那一刻，独立于轮询）
let timePacketCount = 0;
let lastDaySeen = null;
bot._client.on("update_time", (packet) => {
    timePacketCount++;
    const t = Number(BigInt.asIntN(64, (BigInt(packet.time[0]) << 32n)) | BigInt(packet.time[1]));
    const day = Math.floor(t / 24000);
    const timeOfDay = t % 24000;
    const mark = (lastDaySeen !== null && day !== lastDaySeen) ? "  ★★★ worldDay 滚动！" : "";
    log(`[update_time #${timePacketCount}] day=${day} timeOfDay=${timeOfDay}${mark}`);
    lastDaySeen = day;
});

bot.on("messagestr", (message, position, jsonMsg) => {
    if (position !== "system") return;
    if (!/time|set|unknown|invalid/i.test(message)) return;
    log(`[sys] "${message}"`);
});

/**
 * 复刻 index.js:1749-1762 的 dayBreakPromise 轮询循环。
 * 1s 一次读 bot.time.day，prevDay!=null 且 d>prevDay → resolve("__DAYBREAK__")。
 * 返回 {triggered, dayAtTrigger, prevDayAtTrigger, polls} 便于断言。
 */
function makeDayBreakPromise() {
    let prevDay = typeof bot.time?.day === "number" ? bot.time.day : null;
    let polls = 0;
    const promise = new Promise((resolve) => {
        const iv = setInterval(() => {
            const d = bot.time?.day;
            polls++;
            if (typeof d !== "number") return;  // 掉线瞬间不触发也不更新 prev（同 index.js）
            log(`PROBE_POLL #${polls} day=${d} timeOfDay=${bot.time.timeOfDay} (prev=${prevDay})`);
            if (prevDay !== null && d > prevDay) {  // worldDay 滚动：严格 +1
                clearInterval(iv);
                resolve({ triggered: true, dayAtTrigger: d, prevDayAtTrigger: prevDay, polls });
                return;
            }
            prevDay = d;
        }, 1000);
        // 外层超时兜底（index.js 里靠 timeoutPromise/codePromise 限步长，这里自己限）
        setTimeout(() => { clearInterval(iv); resolve({ triggered: false, polls }); }, 60000);
    });
    return promise;
}

bot.once("spawn", async () => {
    log("spawn，version", bot.version, "位置", bot.entity.position.floored());
    await wait(1500); // 让首批 update_time 包到达

    // ① 设到接近 24000（23000，day=0）。游戏自然流速会推过 24000/0 → day 0→1。
    //    ★ /time set 会把累计 time 重置成 23000 → day=floor(23000/24000)=0。
    //    设夜后 prevDay 基准=0，若判据用绝对值会立刻自杀；用严格 +1 不会。
    log("--- ① /time set 23000（接近 24000，day=0）---");
    bot.chat("/time set 23000");
    await wait(1000);
    const dayBefore = bot.time.day;
    const todBefore = bot.time.timeOfDay;
    log(`\n========== PROBE_SET（设夜后）day=${dayBefore} timeOfDay=${todBefore} ==========`);

    // ② 挂 dayBreakPromise 轮询，等 worldDay 滚动（自然流速，约 1000 刻 / 50s @ 默认 20TPS）
    //    裸服默认 20TPS，23000→24000 需 1000 刻 = 50s。给 60s 超时。
    log("--- ② 挂 dayBreakPromise 轮询（1s/次），等 worldDay 0→1 ---");
    const result = await makeDayBreakPromise();

    log(`\n========== PROBE_DAYBREAK ==========`);
    log(`触发=${result.triggered} polls=${result.polls}`);
    if (result.triggered) {
        log(`触发时 day=${result.dayAtTrigger} prev=${result.prevDayAtTrigger} → 严格+1=${result.dayAtTrigger === result.prevDayAtTrigger + 1}`);
        log(`设夜后基准 day=${dayBefore}（未立刻自杀=${dayBefore === result.prevDayAtTrigger}）`);
        if (result.dayAtTrigger === result.prevDayAtTrigger + 1 && dayBefore === result.prevDayAtTrigger) {
            log("★ 判据正确：worldDay 严格 +1 那一刻触发，设夜后未误判自杀");
        } else {
            log("⚠ 触发但判据异常，人工研判");
        }
    } else {
        log(`⚠ 60s 内未触发 worldDay 滚动（timeOfDay 现在=${bot.time.timeOfDay}）。可能裸服流速异常或时间没推进，检查 update_time 日志`);
    }
    log(`\n========== 实测结论 ==========`);
    log(`daybreak 步内熔断判据：${result.triggered && result.dayAtTrigger === result.prevDayAtTrigger + 1 ? "正确 ✓" : "需排查 ⚠"}`);
    return finish(0);
});

function finish(code) {
    log(`=== 结束：共收到 ${timePacketCount} 个 update_time 包 ===`);
    setTimeout(() => process.exit(code), 300);
}

bot.on("kicked", (r) => { log("被踢：", r); process.exit(1); });
bot.on("error", (e) => { log("错误：", e.message); process.exit(1); });
bot.on("end", () => log("连接结束"));
