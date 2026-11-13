/**
 * 探针版：定位「不 await deposit + close」导致 OOM 的精确循环点。
 *
 *   bash start_servers.sh --mc-only
 *   node test_chest_deposit_noawait_probe.js
 *
 * 思路：openContainer → chest.deposit(...)（不 await）→ 立刻 chest.close()，
 * 然后插桩统计 transferOne / clickWindow 被调了多少次、最后状态，并在 5000 次硬熔断退出
 * （不等到 OOM）。同时打 setInterval 每秒报告堆增长。
 */
const mineflayer = require("mineflayer");
const { pathfinder, Movements, goals } = require("mineflayer-pathfinder");
const { Vec3 } = require("vec3");

const HOST = "localhost";
const PORT = 25567;
const USER = "bot";
const log = (...a) => console.log(`[probe ${USER}]`, ...a);

const bot = mineflayer.createBot({ host: HOST, port: PORT, username: USER, version: "1.21.11" });
bot.loadPlugin(pathfinder);

const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const cmd = async (s, ms = 700) => { bot.chat(s); await wait(ms); };
const mcData = () => require("minecraft-data")(bot.version);

// 每秒堆报告
const heapTimer = setInterval(() => {
    const m = process.memoryUsage();
    log(`[heap] rss=${(m.rss/1048576).toFixed(0)}MB heapUsed=${(m.heapUsed/1048576).toFixed(0)}MB heapTotal=${(m.heapTotal/1048576).toFixed(0)}MB`);
}, 1000);

// 插桩：包一层 clickWindow 计数（mineflayer 把 clickWindow 装在 inventory 插件闭包里，
// 没挂到 bot 上，无法直接 wrap。改用更上层观测：监听 _client 写包，统计 window_click 数）
let windowClickPackets = 0;
let lastClickTimes = [];
const origWrite = bot._client.write.bind(bot._client);
bot._client.write = (name, params) => {
    if (name === "window_click") {
        windowClickPackets++;
        lastClickTimes.push(Date.now());
        if (windowClickPackets % 200 === 0) {
            log(`  >>> window_click 包已发 ${windowClickPackets} 个`);
        }
        if (windowClickPackets > 5000) {
            log(`  !!! window_click 超 5000，硬熔断（防 OOM）`);
            dumpAndExit("LOOP_WINDOW_CLICK");
            return; // 不再发
        }
    }
    return origWrite(name, params);
};

let exited = false;
function dumpAndExit(reason) {
    if (exited) return; exited = true;
    clearInterval(heapTimer);
    log("\n================ 探针结论 ================");
    log(`触发熔断原因: ${reason}`);
    log(`window_click 包总数: ${windowClickPackets}`);
    log(`currentWindow: ${bot.currentWindow ? bot.currentWindow.id : "null"}`);
    if (lastClickTimes.length >= 2) {
        const span = lastClickTimes[lastClickTimes.length-1] - lastClickTimes[0];
        log(`最后 ${lastClickTimes.length} 个 click 跨 ${span}ms → 速率 ≈ ${(lastClickTimes.length*1000/Math.max(1,span)).toFixed(0)} 包/秒`);
    }
    setTimeout(() => process.exit(2), 200);
}

process.on("unhandledRejection", (r) => log("[unhandledRejection]", r && (r.message||r)));
process.on("uncaughtException", (e) => { log("[uncaughtException]", e && e.message); });

bot.on("kicked", (r) => log("★ 被踢:", typeof r==="string"?r:JSON.stringify(r)));
bot.on("end", () => log("★ end"));
bot.on("error", (e) => log("★ error:", e.message));

bot.once("spawn", async () => {
    log("spawn", bot.version, bot.entity.position.floored());
    await wait(1500);
    await cmd("/time set 1000", 600);
    await cmd("/gamerule doMobSpawning false", 600);

    // 给 64 cobblestone
    bot.inventory.items().forEach((it) => { try{bot.tossStack(it);}catch(e){} });
    await wait(400);
    await cmd(`/give ${USER} minecraft:cobblestone 64`, 700);

    // 放箱子在脚前
    const pos = bot.entity.position.floored();
    const cp = pos.offset(0,0,1);
    await cmd(`/setblock ${cp.x} ${cp.y} ${cp.z} minecraft:air`, 400);
    await cmd(`/setblock ${cp.x} ${cp.y} ${cp.z} minecraft:chest`, 700);
    await wait(300);
    const cb = bot.blockAt(cp);
    if (!cb || cb.name !== "chest") { log("箱子没放出来"); return process.exit(1); }

    try {
        bot.pathfinder.setMovements(new Movements(bot, mcData()));
        await bot.pathfinder.goto(new goals.GoalNear(cp.x, cp.y, cp.z, 1));
    } catch(e){ log("goto err", e.message); }
    await wait(300);

    log("\n========== openContainer → deposit(不await) → close ==========");
    const chest = await bot.openContainer(cb);
    log(`opened currentWindow=${bot.currentWindow?bot.currentWindow.id:"null"}`);
    const cob = mcData().itemsByName["cobblestone"];
    // ★ 不 await
    chest.deposit(cob.id, null, 32);
    log("deposit 已发出（不 await）");
    chest.close();
    log(`closed currentWindow=${bot.currentWindow?bot.currentWindow.id:"null"}`);

    // 等 12s 看 click 包数是否疯涨
    log("等 12s 观测…");
    await wait(12000);
    log(`\n12s 后: window_click 包=${windowClickPackets}`);
    log(`currentWindow=${bot.currentWindow?bot.currentWindow.id:"null"}`);
    if (windowClickPackets > 100) {
        log("★ 确认：不 await deposit + close 触发 window_click 包风暴 → 进程 OOM 崩 / bot 被踢");
    } else if (windowClickPackets > 0 && windowClickPackets <= 100) {
        log("○ 有少量 click（可能正常 transfer 的尾巴），未风暴");
    } else {
        log("○ 无 click 风暴，OOM 另有他因");
    }
    clearInterval(heapTimer);
    setTimeout(()=>process.exit(0), 300);
});
