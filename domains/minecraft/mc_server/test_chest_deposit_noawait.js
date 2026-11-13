/**
 * 实测「chest.deposit 漏 await + 立刻 close」是否会把 bot 搞掉线 / 产出 unhandledRejection。
 *
 *   bash start_servers.sh --mc-only        # 先起裸 MC server @ :25567（不起 mineflayer Express）
 *   node test_chest_deposit_noawait.js      # 再跑本脚本，mineflayer 直连
 *
 * ## 为什么有这个脚本
 *
 * alice step273（2026-09-08 run，logs/run_openkage_20260908_104408...）掉线根因疑似：LLM 手写
 *   chest.deposit(logId, null, n)   // ❌ 没 await
 *   chest.close();                   // 紧接着关窗
 * deposit 内部链路 transfer→clickWindow 要 `await` 服务器窗口确认包
 * （waitForWindowUpdate / confirmTransaction，超时 WINDOW_TIMEOUT=5000ms）。没 await 就 close()，
 * close 立刻把 bot.currentWindow=null + 发 close_window 包，孤儿 deposit 仍在对**已关窗口**等回包 /
 * 发 window_click → 抛错成 unhandledRejection（不在 /step await 链上，被顶层兜住只打 log 不退出），
 * 但状态已乱，keepalive 饿死 → 被踢 / RemoteDisconnected。
 *
 * 本脚本**轻量直连**（不走 index.js Express），直接复刻这条因果链：openContainer → deposit（不 await）
 * → close，然后观测 bot 是否被踢、是否产出 unhandledRejection、currentWindow 是否残留、能否继续动作。
 *
 * ## 对照
 *
 *   case A（复刻 bug）：chest.deposit(...) 不 await + 立刻 chest.close()
 *   case B（对照）：    await chest.deposit(...) + chest.close()
 *
 * 两组都重试 N 次，每次之后等一段墙钟让孤儿 deposit 5s 超时兑现，统计：
 *   - kicked 次数（被服务器踢）
 *   - end 次数（连接断）
 *   - unhandledRejection 条数（孤儿 deposit 抛错被顶层兜）
 *   - currentWindow 残窗情况
 *   - 后续动作（再 open 一次）是否还成
 *
 * ## 判据
 *
 *   case A 出现 kicked/end/unhandledRejection 之一，case B 全无 → 复刻成功，根因坐实
 *   两者都无 → 因果链不成立（可能 deposit 在 close 前已飞快完成、或服务器宽容没踢），需另找根因
 */
const mineflayer = require("mineflayer");
const { pathfinder, Movements, goals } = require("mineflayer-pathfinder");
const { Vec3 } = require("vec3");

const HOST = "localhost";
const PORT = 25567;
// 用户名必须是 ops.json 里的 op（level 4），否则 /give、/setblock、/time 会被拒。
const USER = "bot";
const log = (...a) => console.log(`[chestnoawait ${USER}]`, ...a);

const bot = mineflayer.createBot({
    host: HOST, port: PORT, username: USER, version: "1.21.11",
});
bot.loadPlugin(pathfinder);

// ---------- 全局观测台：抓 kicked / end / error / unhandledRejection ----------
const stats = {
    kicked: 0, end: 0, error: 0, unhandledRejection: 0, uncaughtException: 0,
    lastKick: null, lastEnd: null,
};
// 复刻 index.js:2167-2174 的兜底：不退出，只计数 + 打 log
process.on("unhandledRejection", (reason) => {
    stats.unhandledRejection++;
    log(`[unhandledRejection 已兜住，不退出]`,
        reason && (reason.message || reason));
});
process.on("uncaughtException", (err) => {
    stats.uncaughtException++;
    log(`[uncaughtException 已兜住，不退出]`, err && err.message);
});

bot.on("kicked", (r) => {
    stats.kicked++;
    stats.lastKick = typeof r === "string" ? r : JSON.stringify(r);
    log(`★ 被踢：${stats.lastKick}`);
});
bot.on("end", () => {
    stats.end++;
    log("★ 连接 end");
});
bot.on("error", (e) => {
    stats.error++;
    log(`★ bot error: ${e.message}`);
});

// 收集 /give /setblock /time 系统回显
bot.on("messagestr", (message, position) => {
    if (position !== "system") return;
    if (/give|setblock|time|chest|no|invalid|unknown|block/i.test(message)) {
        log(`[sys] ${message}`);
    }
});

const wait = (ms) => new Promise((r) => setTimeout(r, ms));

/** 发命令并等回显到齐（简单墙钟等） */
async function cmd(s, ms = 700) {
    bot.chat(s);
    await wait(ms);
}

/** 给 bot 指定物品若干，清空同名牌先防混 */
async function giveItem(name, count) {
    // inventory.clear 已能清整包；但只清这一种更安全。直接 clear 全包（测试场景无所谓）
    bot.inventory.items().forEach((it) => {
        try { bot.tossStack(it); } catch (e) {}
    });
    await wait(400);
    await cmd(`/give ${USER} minecraft:${name} ${count}`, 700);
}

/** 在 bot 脚前方一格放一个箱子，返回该箱子 block（找不到返回 null） */
async function placeChestAhead() {
    const pos = bot.entity.position.floored();
    const chestPos = pos.offset(0, 0, 1);
    // 先清掉那格（可能是上轮放的箱子/方块）
    await cmd(`/setblock ${chestPos.x} ${chestPos.y} ${chestPos.z} minecraft:air`, 500);
    await cmd(`/setblock ${chestPos.x} ${chestPos.y} ${chestPos.z} minecraft:chest`, 700);
    await wait(300);
    const b = bot.blockAt(chestPos);
    return b && b.name === "chest" ? b : null;
}

/** 打开箱子并返回 window（带超时，openContainer 等 windowOpen） */
async function openChestTimed(chestBlock, timeoutMs = 5000) {
    return new Promise((resolve, reject) => {
        const t = setTimeout(() => reject(new Error("openContainer 超时")), timeoutMs);
        bot.openContainer(chestBlock).then((w) => { clearTimeout(t); resolve(w); })
            .catch((e) => { clearTimeout(t); reject(e); });
    });
}

/** case A（复刻 bug）：deposit 不 await + 立刻 close */
async function caseA(chestBlock, label) {
    log(`\n========== ${label} case A（不 await deposit + 立刻 close）==========`);
    const before = {
        rej: stats.unhandledRejection, kick: stats.kicked, end: stats.end,
    };
    let chest;
    try {
        chest = await openChestTimed(chestBlock);
    } catch (e) {
        log(`  open 失败: ${e.message}`); return { ok: false, reason: "open fail" };
    }
    log(`  opened: currentWindow=${bot.currentWindow ? bot.currentWindow.id : "null"}`);
    const cob = mcData().itemsByName["cobblestone"];
    // ★ 关键：不 await。fire-and-forget。
    try {
        chest.deposit(cob.id, null, 32);   // ❌ 没 await
        log("  deposit 已发出（未 await）");
    } catch (e) {
        log(`  deposit 同步抛错（罕见）: ${e.message}`);
    }
    // 立刻关窗（不等 deposit）
    try { chest.close(); } catch (e) { log(`  close 抛错: ${e.message}`); }
    log(`  closed: currentWindow=${bot.currentWindow ? bot.currentWindow.id : "null"}`);
    // 等孤儿 deposit 5s 超时兑现（WINDOW_TIMEOUT=5000）+ 余量
    log("  等 8s 看孤儿 deposit 是否抛 unhandledRejection / 是否被踢…");
    await wait(8000);
    const after = {
        rej: stats.unhandledRejection, kick: stats.kicked, end: stats.end,
    };
    const rejDelta = after.rej - before.rej;
    const kickDelta = after.kick - before.kick;
    const endDelta = after.end - before.end;
    log(`  Δ unhandledRejection=+${rejDelta}  Δ kicked=+${kickDelta}  Δ end=+${endDelta}`);
    log(`  currentWindow 残留=${bot.currentWindow ? bot.currentWindow.id : "null"}`);
    return {
        ok: rejDelta === 0 && kickDelta === 0 && endDelta === 0,
        rejDelta, kickDelta, endDelta,
    };
}

/** case B（对照）：await deposit + close */
async function caseB(chestBlock, label) {
    log(`\n========== ${label} case B（await deposit + close，对照）==========`);
    const before = {
        rej: stats.unhandledRejection, kick: stats.kicked, end: stats.end,
    };
    let chest;
    try {
        chest = await openChestTimed(chestBlock);
    } catch (e) {
        log(`  open 失败: ${e.message}`); return { ok: false, reason: "open fail" };
    }
    log(`  opened: currentWindow=${bot.currentWindow ? bot.currentWindow.id : "null"}`);
    const cob = mcData().itemsByName["cobblestone"];
    try {
        await chest.deposit(cob.id, null, 32);   // ✅ await
        log("  deposit 完成（已 await）");
    } catch (e) {
        log(`  deposit 抛错: ${e.message}`);
    }
    try { chest.close(); } catch (e) { log(`  close 抛错: ${e.message}`); }
    log(`  closed: currentWindow=${bot.currentWindow ? bot.currentWindow.id : "null"}`);
    log("  等 8s 看是否无事发生…");
    await wait(8000);
    const after = {
        rej: stats.unhandledRejection, kick: stats.kicked, end: stats.end,
    };
    const rejDelta = after.rej - before.rej;
    const kickDelta = after.kick - before.kick;
    const endDelta = after.end - before.end;
    log(`  Δ unhandledRejection=+${rejDelta}  Δ kicked=+${kickDelta}  Δ end=+${endDelta}`);
    log(`  currentWindow 残留=${bot.currentWindow ? bot.currentWindow.id : "null"}`);
    return {
        ok: rejDelta === 0 && kickDelta === 0 && endDelta === 0,
        rejDelta, kickDelta, endDelta,
    };
}

/** mcData 懒加载（spawn 后 registry 才可用） */
function mcData() {
    return require("minecraft-data")(bot.version);
}

bot.once("spawn", async () => {
    log("spawn，version", bot.version, "位置", bot.entity.position.floored());
    await wait(1500);

    // 设白天、关怪（减少干扰）
    await cmd("/time set 1000", 600);
    await cmd("/gamerule doMobSpawning false", 600);

    const resultsA = [];
    const resultsB = [];
    const ROUNDS = 3;

    for (let i = 0; i < ROUNDS; i++) {
        if (stats.end > 0 || stats.kicked > 0) {
            log(`\n!! 已掉线，提前结束（round ${i}）`); break;
        }
        // 每轮重新给 64 cobblestone（case 会存 32 进箱子，剩余 32 在包里）
        await giveItem("cobblestone", 64);
        const chestBlock = await placeChestAhead();
        if (!chestBlock) { log("箱子没放出来，跳过本轮"); continue; }
        // 走到箱子旁（openContainer 有距离要求）
        try {
            bot.pathfinder.setMovements(new Movements(bot, mcData()));
            await bot.pathfinder.goto(new goals.GoalNear(chestBlock.position.x, chestBlock.position.y, chestBlock.position.z, 1));
        } catch (e) { log(`  goto err（继续）: ${e.message}`); }
        await wait(300);

        // case A
        const ra = await caseA(chestBlock, `round${i}`);
        resultsA.push(ra);
        if (stats.end > 0 || stats.kicked > 0) { log("!! case A 后掉线，停"); break; }

        // 间隔
        await wait(1500);
        if (stats.end > 0 || stats.kicked > 0) break;

        // case B（同一个箱子，先补满 cobblestone）
        await giveItem("cobblestone", 64);
        const cb = await placeChestAhead(); // 重新放一个干净箱子
        if (!cb) { log("case B 箱子没放出来，跳过"); continue; }
        try {
            bot.pathfinder.setMovements(new Movements(bot, mcData()));
            await bot.pathfinder.goto(new goals.GoalNear(cb.position.x, cb.position.y, cb.position.z, 1));
        } catch (e) { log(`  goto err（继续）: ${e.message}`); }
        await wait(300);
        const rb = await caseB(cb, `round${i}`);
        resultsB.push(rb);
        if (stats.end > 0 || stats.kicked > 0) { log("!! case B 后掉线，停"); break; }
        await wait(1500);
    }

    // ---------- 汇总 ----------
    log("\n================ 实测结论 ================");
    log(`case A（不 await deposit + close）× ${resultsA.length} 轮：`);
    log(`  掉线(kicked/end)轮数: ${resultsA.filter(r => r.kickDelta > 0 || r.endDelta > 0).length}`);
    log(`  产出 unhandledRejection 轮数: ${resultsA.filter(r => r.rejDelta > 0).length}`);
    log(`  全程无恙轮数: ${resultsA.filter(r => r.ok).length}`);
    log(`case B（await deposit + close，对照）× ${resultsB.length} 轮：`);
    log(`  掉线轮数: ${resultsB.filter(r => r.kickDelta > 0 || r.endDelta > 0).length}`);
    log(`  产出 unhandledRejection 轮数: ${resultsB.filter(r => r.rejDelta > 0).length}`);
    log(`  全程无恙轮数: ${resultsB.filter(r => r.ok).length}`);
    log(`全局累计: kicked=${stats.kicked} end=${stats.end} unhandledRejection=${stats.unhandledRejection} uncaughtException=${stats.uncaughtException}`);
    if (stats.lastKick) log(`最后被踢原因: ${stats.lastKick}`);

    const aBad = resultsA.some(r => r.kickDelta > 0 || r.endDelta > 0 || r.rejDelta > 0);
    const bGood = resultsB.every(r => r.ok);
    if (aBad && bGood) {
        log("\n★ 复刻成功：不 await deposit + close 会引发掉线/unhandledRejection，对照 await 全程无恙 → 根因坐实");
    } else if (aBad) {
        log("\n★ case A 出问题（但 case B 也可能出，看上面分项）→ 不 await 是诱因之一，但可能非唯一");
    } else {
        log("\n⚠ case A 也没出问题 → 因果链未复现。可能 deposit 在 close 前已飞快完成、或服务器宽容。需增大 transfer 压力（更多物品/更慢服务器）再试");
    }
    finish(0);
});

function finish(code) {
    setTimeout(() => process.exit(code), 400);
}
