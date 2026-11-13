/**
 * waitUntil 新签名冒烟测试 —— **不连 MC 服务器**，用 mock bot 跑。
 *
 *     node test_waitUntil_inject_smoke.js
 *
 * 新签名（2026-09-08）：waitUntil(bot, ticks, reason, conditions)
 *   - ticks：等多少游戏刻（上限 3000）。纯计数器模式等够 ticks 醒 reason=condition_met；
 *     事件模式 ticks 是兜底上限，事件命中 reason=condition_met、超时 reason=timeout。
 *   - conditions：可选事件数组。不传=纯计数器；传了=只监听传的事件。**系统不兜底任何事件**
 *     （要防睡死显式传 ['hurt']、等天亮显式传 ['dawn']）。
 *
 * 验证：
 *  ① 纯计数器（无 conditions）：等够 ticks 醒 reason=condition_met、woke=ticks
 *  ② 事件模式 ['dawn']：夜里等天亮命中 reason=condition_met、woke=dawn
 *  ③ 白天 ['dawn'] 进门已成立 → already_satisfied、一刻没等
 *  ④ 事件模式超时（ticks 先到）→ reason=timeout、woke=null
 *  ⑤ ticks 上限 3000 clamp（传 99999 实际等 3000）
 *  ⑥ 纯计数器不兜底 hurt（掉血不醒，铁定等够 ticks）
 *  ⑦ 事件模式不兜底 hurt（传 ['healed']，掉血不醒，超时 timeout）
 *  ⑧ 短定时器静默：纯计数器 ≤30 刻不发 bot.chat（防微 sleep 刷屏），返回值仍正确
 *  ⑨ 短定时器静默边界：≤30 静默、>30 仍发 chat（阈值线两侧对照）
 */
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const SRC = fs.readFileSync(
    path.join(__dirname, "control_primitives", "waitUntil.js"),
    "utf-8"
);

/** mock bot：timeOfDay 可控、waitForTicks 立即 resolve（模拟 tick 到达 + 时间流逝）。
 *  可选 onTick(n) 钩子：每次 waitForTicks 推进时间后调用，用例⑥用它模拟掉血。
 *  chat 记录到 __chatLog（用例⑧⑨断言纯定时器正常达成静默：纯计数器无异常不该发 chat）。 */
function makeBot(startTod, tickStep, onTick) {
    let tod = startTod;
    const listeners = {};
    const chatLog = [];
    return {
        time: { get timeOfDay() { return tod; }, day: 1 },
        health: 20,
        food: 20,
        entity: { position: { x: 0, y: 64, z: 0, distanceTo: () => 0 } },
        inventory: { items: () => [] },
        nearestEntity: () => null,
        entities: {},
        chat: (m) => chatLog.push(m),
        once: (ev, fn) => { (listeners[ev] = listeners[ev] || []).push(fn); },
        removeListener: (ev, fn) => {
            if (listeners[ev]) listeners[ev] = listeners[ev].filter(f => f !== fn);
        },
        on: (ev, fn) => { (listeners[ev] = listeners[ev] || []).push(fn); },
        waitForTicks: async (n) => { tod += tickStep * n; if (onTick) onTick(n); },
        openkageTickRate: 2,
        // deadline 设很远，让 ticks 撑到 _MAX_WAIT_TICKS=3000（不被步 deadline 削）
        openkageStepDeadlineMs: Date.now() + 1000000000,
        __tod: () => tod,
        __chatLog: chatLog,
    };
}

/** 跑一次：startTod 起始时刻，tickStep 每 waitForTicks(5) 推进的刻数，ticks/reason/conditions 透传。 */
async function runOnce(startTod, tickStep, ticks, conditions, reason) {
    const bot = makeBot(startTod, tickStep);
    const condStr = JSON.stringify(conditions);
    const reasonStr = JSON.stringify(reason || "测试");
    const wrappedSrc =
        SRC +
        "\n;globalThis.__run = (async () => {" +
        "  let ret;" +
        "  try { ret = await waitUntil(bot, " + ticks + ", " + reasonStr + ", " + condStr + "); }" +
        "  catch(e){ globalThis.__runErr = e; }" +
        "  globalThis.__ret = ret;" +
        "})();";
    const ctx = {
        bot, console, setTimeout, clearTimeout, Date, Math, Object,
        Promise, require, globalThis: {},
    };
    vm.createContext(ctx);
    vm.runInContext(wrappedSrc, ctx);
    await ctx.globalThis.__run;
    if (ctx.globalThis.__runErr) throw ctx.globalThis.__runErr;
    return { ret: ctx.globalThis.__ret, tod: bot.__tod(), chatLog: bot.__chatLog };
}

(async () => {
    let pass = 0, fail = 0;
    function check(name, cond, detail) {
        if (cond) { console.log("  ✓", name); pass++; }
        else { console.log("  ✗", name, detail || ""); fail++; }
    }

    console.log("① 纯计数器（无 conditions）：等够 ticks 醒 reason=condition_met");
    {
        // tickStep=100，每 waitForTicks(5) 推进 500 刻。ticks=1500 → 3 轮后 waited≈1500 命中
        // 用 12000（sunset 起步，纯计数器不监听 dawn/night，不会被抢命中）
        const { ret, tod } = await runOnce(12000, 100, 1500, undefined, "纯定时器");
        check("reason=condition_met", ret && ret.reason === "condition_met", JSON.stringify(ret));
        check("woke=ticks", ret && ret.woke === "ticks", JSON.stringify(ret));
        check("waited≥1500", ret && ret.waited >= 1500, `waited=${ret && ret.waited}`);
    }

    console.log("② 事件模式 ['dawn']：夜里等天亮命中");
    {
        // 21500(夜) 起，tickStep=100，每轮500刻。dawn 在 timeOfDay≥22200 命中（进日出段=天亮）
        // ticks=3000 兜底，dawn 在 waited≈700 命中（22200-21500）—— 在 3000 兜底内，reason=condition_met
        const { ret, tod } = await runOnce(21500, 100, 3000, ["dawn"], "等天亮");
        check("reason=condition_met", ret && ret.reason === "condition_met", JSON.stringify(ret));
        check("woke=dawn", ret && ret.woke === "dawn", JSON.stringify(ret));
        check("timeOfDay≥22200", tod >= 22200, `tod=${tod}`);
    }

    console.log("③ 白天 ['dawn'] 进门已成立 → already_satisfied");
    {
        const { ret, tod } = await runOnce(8000, 100, 3000, ["dawn"], "白天等dawn");
        check("reason=already_satisfied", ret && ret.reason === "already_satisfied", JSON.stringify(ret));
        check("一刻没等", ret && ret.waited === 0, `waited=${ret && ret.waited}`);
    }

    console.log("④ 事件模式超时（ticks 兜底先到）→ reason=timeout");
    {
        // 18000(夜) 起，dawn 差4200（→22200），但 ticks=3000 先到 → timeout
        const { ret } = await runOnce(18000, 100, 3000, ["dawn"], "等不到天亮");
        check("reason=timeout", ret && ret.reason === "timeout", JSON.stringify(ret));
        check("woke=null", ret && ret.woke === null, JSON.stringify(ret));
    }

    console.log("⑤ ticks 上限 3000 clamp（传 99999 实际等 3000）");
    {
        // 纯计数器，传 99999，应 clamp 到 3000。waited 到3000就醒。
        const { ret } = await runOnce(12000, 100, 99999, undefined, "超限clamp");
        check("reason=condition_met", ret && ret.reason === "condition_met", JSON.stringify(ret));
        check("waited≤3000（被clamp）", ret && ret.waited <= 3000 + 50, `waited=${ret && ret.waited}`);
    }

    console.log("⑥ 纯计数器不兜底 hurt（掉血不醒，铁定等够 ticks）");
    {
        // 纯计数器无 conditions，掉血也不应醒——原语不替 Agent 决定监听什么，要防睡死
        // 该由 Agent 显式传 ['hurt']。用 onTick 在第2次 tick 掉血，但纯计数器应无视、等够 ticks 醒。
        let round = 0;
        const bot = makeBot(12000, 100, () => { round++; if (round >= 2) bot.health = 17; });
        // ticks=1500, 无 conditions。第2轮掉血3点 → 纯计数器无视，等够 1500 刻醒
        const wrappedSrc = SRC +
            "\n;globalThis.__run=(async()=>{globalThis.__ret=await waitUntil(bot,1500,'掉血不醒测试');})();";
        const ctx = { bot, console, setTimeout, clearTimeout, Date, Math, Object, Promise, require, globalThis: {} };
        vm.createContext(ctx);
        vm.runInContext(wrappedSrc, ctx);
        await ctx.globalThis.__run;
        const ret = ctx.globalThis.__ret;
        check("reason=condition_met（等够 ticks，非 hurt）", ret && ret.reason === "condition_met", JSON.stringify(ret));
        check("woke=ticks（掉血没打断）", ret && ret.woke === "ticks", JSON.stringify(ret));
    }

    console.log("⑦ 事件模式不兜底 hurt（传 ['healed']，掉血不醒，超时 timeout）");
    {
        // 传 ['healed']（等血满）但不满血 → healed 进门不成立，正常等。中途掉血不应醒
        // （旧版会兜底 hurt 偷偷唤醒，违背"只监听传的条件"）。ticks=1500 先到 → timeout、woke=null。
        let round = 0;
        const bot = makeBot(12000, 100, () => { round++; if (round >= 2) bot.health = 17; });
        bot.health = 18;  // 不满血，healed 进门不成立
        const wrappedSrc = SRC +
            "\n;globalThis.__run=(async()=>{globalThis.__ret=await waitUntil(bot,1500,'等血满',['healed']);})();";
        const ctx = { bot, console, setTimeout, clearTimeout, Date, Math, Object, Promise, require, globalThis: {} };
        vm.createContext(ctx);
        vm.runInContext(wrappedSrc, ctx);
        await ctx.globalThis.__run;
        const ret = ctx.globalThis.__ret;
        check("reason=timeout（掉血没兜底唤醒）", ret && ret.reason === "timeout", JSON.stringify(ret));
        check("woke=null（hurt 未被兜底加入）", ret && ret.woke === null, JSON.stringify(ret));
    }

    console.log("⑧ 纯定时器正常达成静默：纯计数器不发 bot.chat（防 sleep 刷屏）");
    {
        // 2026-09-10 短定时器静默；2026-09-16 放宽为不限刻数：Agent 把 waitUntil 当 sleep 塞进
        // 手写循环（如 `while(...掉落) await waitUntil(bot,2,'fall')`），一步循环几十上百次、
        // 每次 bot.chat 一条雷同播报（alice step724 实测单步 501 条）。纯计数器正常达成（无异常）
        // 时静默，返回值照给。这里 ticks=20、tickStep=1（每轮5刻），4 轮后 waited≈20 命中静默。
        const { ret, chatLog } = await runOnce(12000, 1, 20, undefined, "微sleep");
        check("reason=condition_met", ret && ret.reason === "condition_met", JSON.stringify(ret));
        check("woke=ticks", ret && ret.woke === "ticks", JSON.stringify(ret));
        check("waited≈20", ret && ret.waited <= 30, `waited=${ret && ret.waited}`);
        check("未发 bot.chat（静默）", chatLog.length === 0, `chatLog=${JSON.stringify(chatLog)}`);
    }

    console.log("⑨ 纯定时器长等待也静默：ticks=40/200 正常达成不发 chat");
    {
        // 2026-09-16 放宽：去掉 ≤30 阈值，纯定时器正常达成全静默。ticks=40（旧口径会发 chat）
        // 和 ticks=200（长等待过夜）都应静默 —— 纯计时无事件，播报对 Agent 决策无价值。
        // tickStep=2（每轮 10 刻）：ticks=40 → 4 轮 waited≈40 静默。
        const r40 = await runOnce(12000, 2, 40, undefined, "边界40");
        check("ticks=40 静默", r40.chatLog.length === 0, `chatLog.len=${r40.chatLog.length}`);
        const r200 = await runOnce(12000, 10, 200, undefined, "长等待200");
        check("ticks=200 静默", r200.chatLog.length === 0, `chatLog.len=${r200.chatLog.length}`);
    }

    console.log(`\n${pass} pass, ${fail} fail`);
    process.exit(fail === 0 ? 0 : 1);
})().catch(e => { console.error("测试异常:", e && e.stack || e); process.exit(1); });
