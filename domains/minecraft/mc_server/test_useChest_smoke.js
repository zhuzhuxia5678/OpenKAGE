/**
 * useChest 行为契约测试 —— **不连 MC 服务器**，用假 bot 跑。
 *
 *     node test_useChest_smoke.js
 *
 * 测什么（useChest.js 的 getItemFromChest / depositItemIntoChest / checkItemInsideChest）：
 *   ① getItemFromChest：withdraw 静默部分取走（箱子里只有 2、要 5）→ 播报"实得 2/5"
 *      —— 这是关键回归：mineflayer withdraw 数量不足时不抛错只取走有的部分，旧实现完全
 *      不报，Agent 无从区分"取够了"与"只取到一半"（见 memory openkage_primitive_intent_feedback）。
 *   ② getItemFromChest：withdraw 抛 "Bot inventory is full" → 播报"背包满了"，**不**误报"Not enough"
 *      —— 旧 catch 一律说 "Not enough in chest"，把"背包满"误导成"箱子不够"。
 *   ③ getItemFromChest：withdraw 不抛错但库存没涨（窗口同步延迟）→ 播报"一件都没拿到"
 *   ④ depositItemIntoChest：deposit 部分存入 → 播报"实存 N/M"
 *   ⑤ depositItemIntoChest：deposit 抛 "destination full" → 播报"箱子满了"，**不**误报"Not enough"
 *   ⑥ checkItemInsideChest：本步就播报箱子内容（不等到下一轮 observe），且仍 emit closeChest 给观察系统
 *   ⑦ getItemFromChest 非 Vec3 → **抛错**（不是 chat+return，否则 Agent 误以为取物成功）
 *   ⑧ ★根因回归(deposit)：bot.inventory.count 跨窗读不到变化（恒旧值）、chest.count 读到真实存入
 *      → 必须播报"实存 N/M"，绝不"一件都没存进去"。这正是两次30夜长跑 deposit 全报 0 的真实失败模式，
 *      旧代码(读 bot.inventory.count)在此必红。2026-08-25 修：useChest 改读 chest.count。
 *   ⑨ ★根因回归(getItem)：取物对称场景，bot.inventory 读不到增加、chest.count 读到真实取走 → "实得 N/M"
 *   ⑩ ★倍速自适应：3x 下窗口包晚到(第26次轮询)，按 rate 换算的 _syncTicks=30 能覆盖 → "实存 8/8"；
 *      旧固定 25 tick 在 3x 下会误报（固定 tick 数随倍速漂移，必须按 rate 换算）。
 *
 * ⚠️ useChest.js 源码里有多处函数定义，vm 注入后全部可见；测试只调需要的那几个。
 *    mock 覆盖：mcData.itemsByName / bot.openContainer / chest.{findContainerItem,containerItems,
 *    withdraw,deposit,close} / bot.inventory.{count,findInventoryItem} / bot.blockAt / pathfinder.goto /
 *    bot.{emit,closeWindow,waitForTicks} / GoalLookAtBlock。
 */
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const Vec3 = require(path.join(__dirname, "node_modules", "vec3")).Vec3;

const SRC = fs.readFileSync(
    path.join(__dirname, "control_primitives", "useChest.js"),
    "utf-8"
);

// 物品 id 约定：dirt=3, cobblestone=4
const mcData = {
    blocksByName: { chest: { id: 54 }, crafting_table: { id: 58 } },
    itemsByName: {
        dirt: { id: 3, name: "dirt" },
        cobblestone: { id: 4, name: "cobblestone" },
    },
    items: { 3: { name: "dirt" }, 4: { name: "cobblestone" } },
};

// 一个假的 chest 窗口对象
// ★ **chest.count 必须独立于 bot.inventory.count**（2026-08-25 修，钉 useChest 根因）：
//   真实 mineflayer 里 bot.inventory 是另一个 Window 对象，与 chest 的 slots 异步同步，容器打开
//   期间 bot.inventory.count 读不到变化（这正是长跑里 deposit 全报 0 的根因）。mock 里两者独立，
//   才能构造"bot.inventory 恒旧值、chest.count 反映真实存入"的根因场景（见 ⑧⑨）。
function makeChest(opts = {}) {
    return {
        // 容器里的物品（containerItems）
        _items: opts.containerItems || [],
        containerItems() {
            return this._items.slice();
        },
        // findContainerItem：按 id 找容器里的物品
        findContainerItem(id) {
            return this._items.find((it) => it.type === id) || null;
        },
        // ★ chest.count：数容器窗口视图背包区该物品数。真实环境里 transfer(deposit/withdraw) 直接
        //   改这份 slots、立即可见。mock 用 _chestInvCount 序列驱动（deposit 减、withdraw 增），
        //   与 bot.inventory.count 的 invCountSeq 解耦。
        _chestInvCountSeq: opts.chestInvCountSeq || [0],
        _chestInvIdx: 0,
        count(type, metadata) {
            const v = this._chestInvCountSeq[
                Math.min(this._chestInvIdx, this._chestInvCountSeq.length - 1)
            ];
            this._chestInvIdx++;
            return v;
        },
        // withdraw：opts.withdraw 行为（'silent:2' = 不抛错、模拟取走 2 个；'throw:msg' = 抛错）
        _withdrawBehavior: opts.withdraw,
        async withdraw(type, metadata, count) {
            const b = this._withdrawBehavior;
            if (typeof b === "string" && b.startsWith("throw:")) {
                throw new Error(b.slice(6));
            }
            // silent: 模拟 mineflayer 静默部分取走 —— 不抛错，由测试通过 chestInvCountSeq 控制库存变化
            return;
        },
        _depositBehavior: opts.deposit,
        async deposit(type, metadata, count) {
            const b = this._depositBehavior;
            if (typeof b === "string" && b.startsWith("throw:")) {
                throw new Error(b.slice(6));
            }
            return;
        },
        async close() {
            this._closed = true;
        },
    };
}

// runOnce：调 getItemFromChest / depositItemIntoChest / checkItemInsideChest 其一
// fnName 决定调哪个；args 是除 bot 外的参数（已构造好）
async function runOnce(fnName, makeBot, args) {
    const bot = makeBot();
    const wrappedSrc =
        SRC +
        `\n;globalThis.__run = (async () => {` +
        `  try { await ${fnName}(bot, ...args); }` +
        `  catch(e){ globalThis.__runErr = e; }` +
        `})();`;
    const ctx = {
        bot, mcData, console, setTimeout, clearTimeout, Date, Math, Object,
        Promise, require, Vec3, globalThis: {},
        // GoalLookAtBlock：moveToChest 里 goto 用到，给个空构造即可（goto 是 mock）
        GoalLookAtBlock: function () {},
        args,
    };
    vm.createContext(ctx);
    vm.runInContext(wrappedSrc, ctx);
    await ctx.globalThis.__run;
    return { chats: bot.__chats, runErr: ctx.globalThis.__runErr, emitted: bot.__emitted };
}

function makeBot(opts = {}) {
    const chats = [];
    const emitted = [];
    // ★ bot.inventory.count 序列：2026-08-25 修后 useChest 不再读 bot.inventory.count（改读 chest.count），
    //   这个 mock 保留是为了在 ⑧⑨ 里模拟"bot.inventory 跨窗读不到变化"的真实根因（恒返回旧值）。
    //   其它场景给 [0] 占位即可（不会被 useChest 调用）。
    const invCountSeq = opts.invCountSeq || [0];
    let invIdx = 0;
    const chest = opts.chest || makeChest({});
    const bot = {
        __chats: chats,
        __emitted: emitted,
        chat: (m) => chats.push(m),
        openkageTickRate: opts.openkageTickRate != null ? opts.openkageTickRate : 1,
        entity: { position: new Vec3(0, 64, 0) },
        blockAt: () => ({ name: "chest", position: new Vec3(0, 64, 0) }),
        openContainer: async () => chest,
        closeWindow: async () => {},
        waitForTicks: async () => {},
        emit: (evt, ...rest) => emitted.push({ evt, rest }),
        pathfinder: { goto: async () => {} },
        inventory: {
            count: () => {
                const v = invCountSeq[Math.min(invIdx, invCountSeq.length - 1)];
                invIdx++;
                return v;
            },
            findInventoryItem: opts.findInventoryItem || (() => ({ type: 3, count: 10 })),
        },
    };
    return bot;
}

async function main() {
    let pass = 0, fail = 0;
    const check = (name, cond, detail) => {
        if (cond) { pass++; console.log(`  ✓ ${name}`); }
        else { fail++; console.log(`  ✗ ${name}  ${detail || ""}`); }
    };

    // ① getItemFromChest：withdraw 静默部分取走（要 5、实得 2）→ 播报"实得 2/5"
    console.log("① 取物部分成功 → 播报实得 2/5（非静默）");
    {
        const chest = makeChest({
            containerItems: [{ type: 3, name: "dirt", count: 2 }],
            withdraw: "silent", // 不抛错，mineflayer 静默取走有的部分
        });
        // chestInvCountSeq: before=0(#0), 轮询 #1=0(不>0), #2=2(>0 break), after=2 → gained=2 → 实得 2/5
        chest._chestInvCountSeq = [0, 0, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2];
        const bot = makeBot({ chest });
        const r = await runOnce("getItemFromChest", () => bot, [new Vec3(0, 64, 0), { dirt: 5 }]);
        const joined = r.chats.join(" | ");
        check("播报含'实得 2/5'", joined.includes("实得 2/5"), JSON.stringify(r.chats));
        check("未抛错", !r.runErr, r.runErr && r.runErr.message);
    }

    // ② getItemFromChest：withdraw 抛 "Bot inventory is full" → 播报"背包满了"，不误报 Not enough
    console.log("② 取物撞背包满 → 播报'背包满了'（不误报 Not enough）");
    {
        const chest = makeChest({
            containerItems: [{ type: 3, name: "dirt", count: 5 }],
            withdraw: "throw:Unable to withdraw, Bot inventory is full.",
        });
        chest._chestInvCountSeq = [3, 3]; // before 读一次（withdraw 抛错，不走轮询）
        const bot = makeBot({ chest });
        const r = await runOnce("getItemFromChest", () => bot, [new Vec3(0, 64, 0), { dirt: 5 }]);
        const joined = r.chats.join(" | ");
        check("播报含'背包满了'", joined.includes("背包满了"), JSON.stringify(r.chats));
        check("不误报 Not enough / 箱子不够", !joined.includes("Not enough"), JSON.stringify(r.chats));
        check("未抛错", !r.runErr, r.runErr && r.runErr.message);
    }

    // ③ getItemFromChest：withdraw 不抛错但库存没涨 → 播报"一件都没拿到"
    console.log("③ 取物窗口同步延迟（库存未增）→ 播报'一件都没拿到'");
    {
        const chest = makeChest({
            containerItems: [{ type: 3, name: "dirt", count: 5 }],
            withdraw: "silent",
        });
        // chestInvCountSeq: before=3, after 轮询一直 3（chest.count 也读不到增 → 真没取到）
        chest._chestInvCountSeq = [3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3];
        const bot = makeBot({ chest });
        const r = await runOnce("getItemFromChest", () => bot, [new Vec3(0, 64, 0), { dirt: 5 }]);
        const joined = r.chats.join(" | ");
        check("播报含'一件都没拿到'", joined.includes("一件都没拿到"), JSON.stringify(r.chats));
        check("未抛错", !r.runErr, r.runErr && r.runErr.message);
    }

    // ④ depositItemIntoChest：deposit 部分存入 → 播报"实存 N/M"
    console.log("④ 存物部分成功 → 播报实存 3/8");
    {
        const chest = makeChest({
            containerItems: [],
            deposit: "silent",
        });
        // deposit 用 chest.count 前后差：before=10, 轮询首次 <10 即 7（实存 3）
        // 序列：第1次=before(10)，之后轮询读到 7 → break，deposited=10-7=3
        chest._chestInvCountSeq = [10, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7];
        const bot = makeBot({
            chest,
            findInventoryItem: () => ({ type: 3, count: 10 }),
        });
        const r = await runOnce("depositItemIntoChest", () => bot, [new Vec3(0, 64, 0), { dirt: 8 }]);
        const joined = r.chats.join(" | ");
        check("播报含'实存 3/8'", joined.includes("实存 3/8"), JSON.stringify(r.chats));
        check("未抛错", !r.runErr, r.runErr && r.runErr.message);
    }

    // ⑤ depositItemIntoChest：deposit 抛 "destination full" → 播报"箱子满了"，不误报 Not enough
    console.log("⑤ 存物撞箱子满 → 播报'箱子满了'（不误报 Not enough）");
    {
        const chest = makeChest({
            containerItems: [],
            deposit: "throw:destination full",
        });
        chest._chestInvCountSeq = [10, 10]; // before 读一次（deposit 抛错，不走轮询）
        const bot = makeBot({
            chest,
            findInventoryItem: () => ({ type: 3, count: 10 }),
        });
        const r = await runOnce("depositItemIntoChest", () => bot, [new Vec3(0, 64, 0), { dirt: 8 }]);
        const joined = r.chats.join(" | ");
        check("播报含'箱子满了'", joined.includes("箱子满了"), JSON.stringify(r.chats));
        check("不误报 Not enough / 库存不够", !joined.includes("Not enough"), JSON.stringify(r.chats));
        check("未抛错", !r.runErr, r.runErr && r.runErr.message);
    }

    // ⑥ checkItemInsideChest：本步就播报箱子内容，且 emit closeChest
    console.log("⑥ 查箱 → 本步播报内容 + emit closeChest 给观察系统");
    {
        const chest = makeChest({
            containerItems: [
                { type: 3, name: "dirt", count: 5 },
                { type: 4, name: "cobblestone", count: 12 },
            ],
        });
        const bot = makeBot({ chest, invCountSeq: [0] });
        const r = await runOnce("checkItemInsideChest", () => bot, [new Vec3(0, 64, 0)]);
        const joined = r.chats.join(" | ");
        check("播报含 dirt×5", joined.includes("dirt×5"), JSON.stringify(r.chats));
        check("播报含 cobblestone×12", joined.includes("cobblestone×12"), JSON.stringify(r.chats));
        const closeEvt = r.emitted.find((e) => e.evt === "closeChest");
        check("emit 了 closeChest（观察系统依赖）", !!closeEvt, JSON.stringify(r.emitted));
        check("未抛错", !r.runErr, r.runErr && r.runErr.message);
    }

    // ⑦ getItemFromChest 非 Vec3 → 抛错（不是 chat+return）
    console.log("⑦ 非 Vec3 → 抛错（不静默 return）");
    {
        const bot = makeBot({ invCountSeq: [0] });
        const r = await runOnce("getItemFromChest", () => bot, [[1, 2, 3], { dirt: 5 }]);
        check("抛了错", !!r.runErr && /Vec3/.test(r.runErr.message), r.runErr && r.runErr.message);
        check("没有 chat（静默 return 才会无 chat 但也无错）", r.chats.length === 0, JSON.stringify(r.chats));
    }

    // ⑧ ★★ 钉根因：deposit 真实成功，但 bot.inventory.count 跨窗读不到变化（恒旧值），
    //      只有 chest.count 反映真实存入 → 必须播报"实存 8/8"，绝不能"一件都没存进去"。
    //      这正是两次30夜长跑里 deposit 全报 0 的真实失败模式（物品真存进箱子了，bot.inventory
    //      读不到减少）。旧代码（读 bot.inventory.count）在此场景必红 —— 这就是回归价值。
    console.log("⑧ ★根因：bot.inventory跨窗读不到，chest.count读到真实存入 → 播报实存 8/8");
    {
        const chest = makeChest({
            containerItems: [],
            deposit: "silent",
        });
        // chest.count 反映真实存入：before=10, 轮询读到 2（实存 8）→ deposited=8 → 实存 8/8
        chest._chestInvCountSeq = [10, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2];
        const bot = makeBot({
            chest,
            // ★ bot.inventory.count 跨窗恒返回旧值 10 —— 模拟容器打开期间读不到变化（真实环境根因）
            invCountSeq: [10, 10, 10, 10, 10, 10, 10, 10, 10, 10, 10, 10],
            findInventoryItem: () => ({ type: 3, count: 10 }),
        });
        const r = await runOnce("depositItemIntoChest", () => bot, [new Vec3(0, 64, 0), { dirt: 8 }]);
        const joined = r.chats.join(" | ");
        check("播报含'实存 8/8'（chest.count 读到真实存入）", joined.includes("实存 8/8"), JSON.stringify(r.chats));
        check("绝不误报'一件都没存进去'", !joined.includes("一件都没存进去"), JSON.stringify(r.chats));
        check("绝不误报'存入 0/8'", !joined.includes("存入 0/8"), JSON.stringify(r.chats));
        check("未抛错", !r.runErr, r.runErr && r.runErr.message);
    }

    // ⑨ ★★ 钉根因（取物对称）：getItem 真实成功，bot.inventory.count 跨窗读不到增加（恒旧值），
    //      只有 chest.count 反映真实取走 → 必须播报"实得 5/5"，绝不能"一件都没拿到"。
    console.log("⑨ ★根因：bot.inventory跨窗读不到，chest.count读到真实取走 → 播报实得 5/5");
    {
        const chest = makeChest({
            containerItems: [{ type: 3, name: "dirt", count: 5 }],
            withdraw: "silent",
        });
        // chest.count 反映真实取走：before=0, 轮询读到 5（实得 5）→ gained=5 → 实得 5/5
        chest._chestInvCountSeq = [0, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5];
        const bot = makeBot({
            chest,
            // ★ bot.inventory.count 跨窗恒返回旧值 0 —— 模拟读不到增加
            invCountSeq: [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
        });
        const r = await runOnce("getItemFromChest", () => bot, [new Vec3(0, 64, 0), { dirt: 5 }]);
        const joined = r.chats.join(" | ");
        check("播报含'实得 5/5'（chest.count 读到真实取走）", joined.includes("实得 5/5"), JSON.stringify(r.chats));
        check("绝不误报'一件都没拿到'", !joined.includes("一件都没拿到"), JSON.stringify(r.chats));
        check("绝不误报'取到 0/5'", !joined.includes("取到 0/5"), JSON.stringify(r.chats));
        check("未抛错", !r.runErr, r.runErr && r.runErr.message);
    }

    // ⑩ ★★ 倍速自适应：3 倍速下窗口包延迟到第 26 次轮询才反映真实存入。按倍速换算 _syncTicks=30
    //      能覆盖 → 播报"实存 8/8"；旧固定 25 tick 在 3x 下会误报"一件都没存进去"（这正是用户问
    //      "2倍速/3倍速怎么办"的场景：固定 tick 数随倍速漂移，必须按 rate 换算）。
    console.log("⑩ ★倍速自适应：3x下窗口包晚到(第26次)，_syncTicks=30 覆盖 → 播报实存 8/8");
    {
        const chest = makeChest({
            containerItems: [],
            deposit: "silent",
        });
        // chest.count 序列：before=10(#0)，轮询 #1~#25 仍读 10（窗口包还没到），#26 才读 2（实存 8）。
        // 即前 26 个值=10，第 27 个值起=2。3x 下 _syncTicks=30，轮询到 #26 命中 break → deposited=8。
        // 旧固定 25 tick 只轮询到 #25（仍是 10）→ deposited=0 → 误报"一件都没存进去"。
        const seq = [10]; // #0 before
        for (let i = 0; i < 25; i++) seq.push(10); // #1~#25 窗口包未到
        for (let i = 0; i < 10; i++) seq.push(2);  // #26+ 真实存入
        chest._chestInvCountSeq = seq;
        const bot = makeBot({
            chest,
            openkageTickRate: 3, // ★ 3 倍速 → _syncTicks = 10*3 = 30
            findInventoryItem: () => ({ type: 3, count: 10 }),
        });
        const r = await runOnce("depositItemIntoChest", () => bot, [new Vec3(0, 64, 0), { dirt: 8 }]);
        const joined = r.chats.join(" | ");
        check("3x 下播报'实存 8/8'（_syncTicks=30 覆盖晚到的窗口包）", joined.includes("实存 8/8"), JSON.stringify(r.chats));
        check("3x 下绝不误报'一件都没存进去'", !joined.includes("一件都没存进去"), JSON.stringify(r.chats));
        check("未抛错", !r.runErr, r.runErr && r.runErr.message);
    }

    console.log(`\n${pass} passed, ${fail} failed`);
    process.exit(fail === 0 ? 0 : 1);
}

main().catch((e) => {
    console.error("smoke 崩了:", e);
    process.exit(2);
});
