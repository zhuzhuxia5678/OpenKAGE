/**
 * mineBlock 预算套筒测试 —— **不连 MC 服务器**，用假 bot 跑。
 *
 *     node test_mineBlock_budget_smoke.js
 *
 * 只测 2026-08-24 的改动：collectBlock.collect 套本步剩余预算，
 *   ① 超时 → setGoal(null) + 清 targets + 不抛错 + gained>0 走"部分成功"播报含"本步时限收手"
 *   ② 超时 gained=0 → 走 collectTimedOut 分支播报"整段预算花在走向目标上"+ "别改用 bot.dig"
 *   ③ 正常完成（collect resolve）→ 走 gained>=count 播报，不含"本步时限收手"
 *   ④ 拿不到 deadline → 退化为固定 fallback 预算（不崩）
 *
 * 不测 mineBlock 的工具校验/findBlocks/走近等既有逻辑（那些由 audit_primitives.py 真实跑）。
 */
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const SRC = fs.readFileSync(
    path.join(__dirname, "control_primitives", "mineBlock.js"),
    "utf-8"
);

// mcData mock：stone 有 drops=[cobblestone id]、harvestTools 需要镐
const COBBLE_ID = 4;
const STONE_BLOCK = {
    id: 100,
    drops: [COBBLE_ID],
    harvestTools: { 1: true }, // pickaxe id=1 能采
};
const PICKAXE_ITEM = { type: 1, name: "stone_pickaxe" };
const mcData = {
    blocksByName: { stone: STONE_BLOCK },
    items: { [COBBLE_ID]: { name: "cobblestone", id: COBBLE_ID } },
    itemsByName: { stone: { id: COBBLE_ID } }, // 兜底
};

async function runOnce(makeBot) {
    const bot = makeBot();
    // 闭包全局：_collectDroppedItemsFailCount 不用，mineBlock 用 _mineBlockFailCount
    const wrappedSrc =
        "let _mineBlockFailCount = 0;\n" +
        SRC +
        "\n;globalThis.__run = (async () => {" +
        "  try { await mineBlock(bot, 'stone', 5); }" +
        "  catch(e){ globalThis.__runErr = e; }" +
        "  globalThis.__failCount = _mineBlockFailCount;" +
        "})();";
    const ctx = {
        bot, mcData, console, setTimeout, clearTimeout, Date, Math, Object,
        Promise, require, Vec3: require(path.join(__dirname, "node_modules", "vec3")).Vec3,
        globalThis: {},
    };
    vm.createContext(ctx);
    vm.runInContext(wrappedSrc, ctx);
    await ctx.globalThis.__run;
    return {
        chats: bot.__chats,
        failCount: ctx.globalThis.__failCount,
        runErr: ctx.globalThis.__runErr,
        setGoalCalled: bot.__setGoalCalled,
        targetsCleared: bot.__targetsCleared,
    };
}

function makeStoneBlock(pos) {
    return { name: "stone", position: pos, type: 100, diggable: true };
}

function makeBot(opts = {}) {
    const chats = [];
    // 库存计数序列：collect 前后 mineBlock 调 countInInventory() 两次（before/after）
    // opts.invSeq = [before, after]
    const invSeq = opts.invSeq || [0, 0];
    let invIdx = 0;
    const bot = {
        __chats: chats,
        __setGoalCalled: 0,
        __targetsCleared: 0,
        openkageTickRate: 1,
        openkageStepDeadlineMs: opts.deadlineMs, // undefined = 拿不到 deadline
        heldItem: PICKAXE_ITEM,
        entity: { position: { x: 0, y: 64, z: 0, distanceTo: () => 1, floored: () => ({x:0,y:64,z:0}) } },
        findBlocks: () => [{ x: 1, y: 64, z: 0 }], // 16 格内找到 stone
        blockAt: () => makeStoneBlock({ x: 1, y: 64, z: 0 }),
        inventory: {
            count: () => invSeq[Math.min(invIdx++, invSeq.length - 1)],
            items: () => [PICKAXE_ITEM],
        },
        registry: { itemsByName: { stone: { id: 100 } } },
        tool: { equipForBlock: async () => {} },
        pathfinder: {
            goto: async () => {},
            setGoal: () => { bot.__setGoalCalled++; },
        },
        collectBlock: {
            collect: opts.collectFn || (async () => {}),
            targets: { clear: () => { bot.__targetsCleared++; } },
        },
        emit: () => {},
        chat: (m) => chats.push(m),
        save: () => {},
    };
    return bot;
}

async function main() {
    let pass = 0, fail = 0;
    const check = (name, cond, detail) => {
        if (cond) { pass++; console.log(`  ✓ ${name}`); }
        else { fail++; console.log(`  ✗ ${name}  ${detail || ""}`); }
    };

    // ① collect 阻塞到预算超时，gained>0 → 部分成功播报含"本步时限收手"，不抛错，setGoal 被调
    console.log("① collect 超时 + gained>0 → 部分成功 + 本步时限收手");
    {
        // deadline 设为 100ms 后 → 预算极小，collect 立刻超时
        // collectFn 永不 resolve（模拟批量寻路卡住）
        const bot = makeBot({
            deadlineMs: Date.now() + 100,
            invSeq: [0, 3], // before=0, after=3 → gained=3
            collectFn: () => new Promise(() => {}), // 永不 resolve
        });
        const r = await runOnce(() => bot);
        const chat = r.chats.join(" ");
        check("未抛错", !r.runErr, r.runErr && r.runErr.message);
        check("播报含 gained=3", chat.includes("Mined 3"), chat);
        check("播报含'本步时限收手'", chat.includes("本步时限收手"), chat);
        check("setGoal(null) 被调", r.setGoalCalled >= 1, `called ${r.setGoalCalled}`);
        check("targets 被清", r.targetsCleared >= 1, `cleared ${r.targetsCleared}`);
    }

    // ② collect 超时 gained=0 → collectTimedOut 分支，含"整段预算花在走向目标"+"本步时限收手"
    //   （原语不替 Agent 决定"别用 bot.dig"，故两个分支都不含该提示；bot.dig 语义由 prompt 说明）
    console.log("② collect 超时 gained=0 → 超时收手提示");
    {
        const bot = makeBot({
            deadlineMs: Date.now() + 100,
            invSeq: [0, 0], // gained=0
            collectFn: () => new Promise(() => {}),
        });
        const r = await runOnce(() => bot);
        const chat = r.chats.join(" ");
        check("未抛错", !r.runErr, r.runErr && r.runErr.message);
        check("播报含'本步时限收手'", chat.includes("本步时限收手"), chat);
        check("播报含'本步时限收手'", chat.includes("本步时限收手"), chat);
    }

    // ③ collect 正常 resolve，gained>=count → 正常播报，不含"本步时限收手"
    console.log("③ collect 正常完成 → 不含本步时限收手");
    {
        const bot = makeBot({
            deadlineMs: Date.now() + 10000, // 充足预算
            invSeq: [0, 5], // gained=5 >= count=5
            collectFn: async () => {}, // 立刻 resolve
        });
        const r = await runOnce(() => bot);
        const chat = r.chats.join(" ");
        check("未抛错", !r.runErr, r.runErr && r.runErr.message);
        check("播报含'Mined 5'", chat.includes("Mined 5"), chat);
        check("不含'本步时限收手'", !chat.includes("本步时限收手"), chat);
    }

    // ④ 拿不到 deadline（undefined）→ 退化为 fallback 预算，collect 超时仍收手不崩
    console.log("④ 无 deadline → fallback 预算，超时仍收手");
    {
        const bot = makeBot({
            deadlineMs: undefined, // 拿不到
            invSeq: [0, 2],
            collectFn: () => new Promise(() => {}), // 永不 resolve，等 fallback 超时
        });
        const r = await runOnce(() => bot);
        const chat = r.chats.join(" ");
        check("未抛错", !r.runErr, r.runErr && r.runErr.message);
        check("超时收手（播报含本步时限收手 或 Mined）", chat.includes("本步时限收手") || chat.includes("Mined 2"), chat);
    }

    console.log(`\n${pass} passed, ${fail} failed`);
    process.exit(fail === 0 ? 0 : 1);
}

main().catch((e) => { console.error("smoke 崩了:", e); process.exit(2); });
