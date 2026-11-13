/**
 * 实测 sleep 跳夜后 ``bot.time.day`` 是否 +1（轻量直连版，照 test_advancement_*.js 骨架）。
 *
 *   bash start_servers.sh --mc-only        # 先起裸 MC server @ :25567（不起 mineflayer Express）
 *   node test_sleep_day.js                  # 再跑本脚本，mineflayer 直连
 *
 * ## 为什么有这个脚本
 *
 * erin 第10天 5 次续跑熬不过夜被 retire。根因是 sleep 跳夜把「夜」从步末采样里抹掉：
 * Python 侧判天亮靠 ``env.py:3448 if was_night and not now_night``，``was_night`` 取**上一步末**
 * 的 phase。erin 上一步末是 noon（昼）、本步内 sleep 跳夜、本步末 sunrise（昼），两步末都是
 * 白天 → 判据永不触发 → 不进反思 → 续跑死循环 5 次撞上限 retire。
 *
 * 拟用 ``bot.time.day``（mineflayer time.js:23 ``day = floor(time/24000)``，服务器权威累计刻）
 * 的相对增量判穿过午夜。前提是 sleep 跳夜后 day 真的 +1——本脚本验证这个前提。
 * **前提不成立则整个方案失效**，回退 daybreak 信号方案。
 *
 * ## 关键：sleep resolve 不等于跳夜完成
 *
 * bed.js waitUntilSleep 只等 'sleep' 事件（bot **躺下**瞬间 resolve），不是跳夜。
 * 跳夜是服务器随后发 ``update_time`` 包把 timeOfDay 归 0。故 sleep resolve 后必须监听
 * ``update_time`` 包、等 timeOfDay 落到白天段（<12000）再读 day——否则读到的是"躺下但夜
 * 还没跳"的旧 day。这正是直连版比 provision_workers 框架版强的地方：没有 index.js 的
 * dayBreakPromise 抢先中止本步，sleep 后能从容等 time 包。
 *
 * ## 输出判据
 *
 *   PROBE_BEFORE  day=D  timeOfDay=（设夜前）
 *   PROBE_SLEEP_START day=D  timeOfDay=14000（夜）
 *   PROBE_SLEEP_OK   → sleep（躺下）成功
 *   PROBE_DAY_JUMP   day=? timeOfDay=（应≈0 黎明）   ← 关键：对比 day 是否 +1
 *   → day = D+1：方案成立，进阶段2
 *   → day 不变（只 timeOfDay 归0）：看 age/time 是否涨，改用 age 跨24000判
 *   → age/time 也不涨：回退 daybreak 信号方案
 */
const mineflayer = require("mineflayer");
const { pathfinder, Movements, goals } = require("mineflayer-pathfinder");
const { Vec3 } = require("vec3");

const HOST = "localhost";
const PORT = 25567;
// 用户名必须是 ops.json 里的 op（level 4），否则 /time、/give 会被拒。
// server121/ops.json 里 "bot" 是 op；online-mode=false 按用户名匹配权限。
const USER = "bot";
const log = (...a) => console.log(`[sleepday ${USER}]`, ...a);

const bot = mineflayer.createBot({
    host: HOST, port: PORT, username: USER, version: "1.21.11",
});

bot.loadPlugin(pathfinder);

// 监听所有 update_time 包，实时打印 day 变化（抓跳夜那一刻）
let timePacketCount = 0;
let lastDaySeen = null;
bot._client.on("update_time", (packet) => {
    timePacketCount++;
    // 与 mineflayer time.js 同款换算（long→BigInt→Number）
    const t = Number(BigInt.asIntN(64, (BigInt(packet.time[0]) << 32n)) | BigInt(packet.time[1]));
    const a = Number(BigInt.asIntN(64, (BigInt(packet.age[0]) << 32n)) | BigInt(packet.age[1]));
    const day = Math.floor(t / 24000);
    const timeOfDay = t % 24000;
    const mark = (lastDaySeen !== null && day !== lastDaySeen) ? "  ★★★ day 变了！" : "";
    log(`[update_time #${timePacketCount}] day=${day} timeOfDay=${timeOfDay} age=${a} time=${t}${mark}`);
    lastDaySeen = day;
});

// 收集 grant/give/错误回显
bot.on("messagestr", (message, position, jsonMsg) => {
    if (position !== "system") return;
    const key = jsonMsg && jsonMsg.translate;
    if (!key && !/give|time|sleep|bed|no|invalid|unknown/i.test(message)) return;
    log(`[sys] translate=${key || "(none)"} text="${message}"`);
});

/** 等 ms 毫秒 */
const wait = (ms) => new Promise((r) => setTimeout(r, ms));

/** 等 timeOfDay 落到白天段（<12000）或超时 timeoutMs，返回是否等到跳夜 */
async function waitForDaybreak(timeoutMs = 8000) {
    const start = Date.now();
    while (Date.now() - start < timeoutMs) {
        if (bot.time.timeOfDay >= 0 && bot.time.timeOfDay < 12000) {
            // 二次确认 age/time 也刷新了
            return true;
        }
        await wait(100);
    }
    return false;
}

bot.once("spawn", async () => {
    log("spawn，version", bot.version, "位置", bot.entity.position.floored());
    await wait(1500); // 让首批 update_time 包到达

    const pos = bot.entity.position.floored();
    // 床放在 bot 脚前方一格地面上：foot@(x,y,z+1)、head@(x,y,z+2)，facing=south（head 在 +z）
    // bed.js parseBedMetadata：facing=south → headOffset=(0,0,1)，head=foot+(0,0,1) ✓ 对齐
    const footPos = pos.offset(0, 0, 1);
    const headPos = pos.offset(0, 0, 2);

    // ① 设到夜里（14000 = night 段，bed.js 要求 timeOfDay∈[12541,23458] 才合法）
    log("--- ① /time set 14000（设夜）---");
    bot.chat("/time set 14000");
    await wait(1000);
    // ★ /time set 会把累计 time 重置成 14000 → day=floor(14000/24000)=0。
    //   故 sleep 前基准 day 必须在**设夜后**记，而不是设夜前（设夜前 day 是历史值无意义）。
    const dayBefore = bot.time.day;
    const todBefore = bot.time.timeOfDay;
    log(`\n========== PROBE_BEFORE（设夜后/sleep 前）day=${dayBefore} timeOfDay=${todBefore} age=${bot.time.age} time=${bot.time.time} ==========`);

    // ② /setblock 放完整双格床（绕开 placeBlock 超时 + half bed）
    log(`--- ② /setblock 放床 foot@${footPos} head@${headPos} ---`);
    bot.chat(`/setblock ${footPos.x} ${footPos.y} ${footPos.z} minecraft:white_bed[facing=south,part=foot]`);
    await wait(600);
    bot.chat(`/setblock ${headPos.x} ${headPos.y} ${headPos.z} minecraft:white_bed[facing=south,part=head]`);
    await wait(800);

    let bedBlock = bot.findBlock({ matching: bot.registry.blocksByName["white_bed"].id, maxDistance: 6 });
    log(`    放床后 findBlock=${bedBlock ? bedBlock.position : "null"}`);
    if (!bedBlock) { log("PROBE_ERR 床未生成，放弃"); return finish(1); }

    // ③ 走到床边（保证在 sleep clickRange 内，距离≤2）
    log("--- ③ goto 床边 ---");
    try {
        const { GoalNear } = goals;
        bot.pathfinder.setMovements(new Movements(bot, require("minecraft-data")(bot.version)));
        await bot.pathfinder.goto(new GoalNear(bedBlock.position.x, bedBlock.position.y, bedBlock.position.z, 1));
    } catch (e) {
        log(`    goto err（继续试 sleep）: ${e.message}`);
    }
    await wait(300);

    // ④ sleep —— resolve 仅代表"躺下"，不是"跳夜"
    log(`--- ④ PROBE_SLEEP_START day=${bot.time.day} timeOfDay=${bot.time.timeOfDay} → bot.sleep ---`);
    try {
        await bot.sleep(bedBlock);
        log("    PROBE_SLEEP_OK（已躺下）");
    } catch (e) {
        log(`    PROBE_SLEEP_ERR ${e.message}`);
        log(`    PROBE_SLEEP_FAIL day=${bot.time.day} timeOfDay=${bot.time.timeOfDay}`);
        return finish(1);
    }

    // ⑤ sleep resolve 后等 update_time 包跳夜
    log("--- ⑤ 等 update_time 跳夜（最多 8s）---");
    const jumped = await waitForDaybreak(8000);
    const dayAfter = bot.time.day;
    const todAfter = bot.time.timeOfDay;
    log(`\n========== PROBE_DAY_JUMP day=${dayAfter} timeOfDay=${todAfter} age=${bot.time.age} time=${bot.time.time} 跳夜=${jumped} ==========`);
    log(`\n========== 实测结论 ==========`);
    log(`sleep 前 day=${dayBefore}  →  sleep 后 day=${dayAfter}  →  day +1 = ${dayAfter === dayBefore + 1}`);
    log(`timeOfDay: ${todBefore} → ${todAfter}`);
    log(`age: sleep 前未记 / 后=${bot.time.age}, time(累计刻)=${bot.time.time}`);
    if (dayAfter === dayBefore + 1) {
        log("★ 方案成立：sleep 跳夜后 bot.time.day +1，可用 day 相对增量判穿过午夜 → 进阶段2");
    } else if (dayAfter === dayBefore) {
        log("⚠ day 不变（只 timeOfDay 归0）。检查 age/time 是否因跳夜递增——若涨则改用 age 跨24000判；若都不涨回退 daybreak 信号方案");
    } else {
        log(`⚠ day 变化异常（${dayBefore}→${dayAfter}），人工研判`);
    }
    return finish(0);
});

function finish(code) {
    log(`=== 结束：共收到 ${timePacketCount} 个 update_time 包 ===`);
    setTimeout(() => process.exit(code), 300);
}

bot.on("kicked", (r) => { log("被踢：", r); process.exit(1); });
bot.on("error", (e) => { log("错误：", e.message); process.exit(1); });
bot.on("end", () => log("连接结束"));
