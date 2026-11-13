/**
 * smeltItem 炉子查找契约测试 —— **不连 MC 服务器**，用假 bot 跑。
 *
 *     node test_smeltItem_furnace_find_smoke.js
 *
 * 测什么（smeltItem.js 的 furnacePos 优先查找，2026-08-25 修，根因见 memory
 * openkage_braindb_systembug_audit_20260825 的 container_vanish_across_days —— 该经验是**误诊**：
 * 炉子没真消失，是 findBlock(maxDistance:32) 只搜 32 格内、bot 走远了搜不到）：
 *   ① 传 furnacePos 且该坐标真是 furnace → blockAt 命中、直接开炉（不走 32 格盲搜）
 *   ② 传 furnacePos 但该坐标是 air（炉子不在那）→ 走过去触发加载仍非 furnace → 退回 32 格盲搜
 *   ③ 不传 furnacePos、32 格内有炉 → 旧路径盲搜命中
 *   ④ 不传 furnacePos、32 格内无炉 → 抛错提示"传 furnacePos 直查"
 *   ⑤ 传 furnacePos、两路都搜不到 → 抛错提示"已按坐标找过…重新 placeItem 放一个"
 *
 * ★ 关键回归：旧实现只 32 格盲搜，bot 走远就报 "No furnace nearby"，Agent 误记成"炉子跨天消失"。
 *   新实现优先按已知坐标 blockAt，治本。
 */
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const assert = require("assert");

const SRC = fs.readFileSync(
    path.join(__dirname, "control_primitives", "smeltItem.js"),
    "utf-8"
);

// 假 mcData
const mcData = {
    itemsByName: {
        raw_iron: { id: 871, name: "raw_iron" },
        coal: { id: 303, name: "coal" },
        iron_ingot: { id: 316, name: "iron_ingot" },
    },
    blocksByName: { furnace: { id: 61 } },
};

/**
 * 假 bot。关键驱动：
 *   blockAtMap: { "x,y,z": blockName } —— blockAt 按坐标查；缺省返 {name:'air'}（chunk 已加载但非炉）
 *   findBlockResult: 32 格盲搜返回的 block（或 null）
 *   openFurnaceResult: 假 furnace 窗口（或抛错）
 */
function makeBot({ blockAtMap = {}, findBlockResult = null, invItems = [] } = {}) {
    const state = {
        openFurnaceCalled: false,
        blockAtReads: [],
        findBlockCalled: false,
        gotoPositions: [],
    };
    const bot = {
        blockAt(pos) {
            const key = `${pos.x},${pos.y},${pos.z}`;
            state.blockAtReads.push(key);
            const name = blockAtMap[key] || "air";
            return { name, position: { x: pos.x, y: pos.y, z: pos.z } };
        },
        findBlock(opts) {
            state.findBlockCalled = true;
            return findBlockResult;
        },
        pathfinder: {
            goto: async (goal) => {
                // GoalNear 构造时前 3 个参数是 x,y,z；记录下来
                state.gotoPositions.push([goal.x, goal.y, goal.z]);
            },
        },
        async openFurnace(block) {
            state.openFurnaceCalled = true;
            return {
                // 假 furnace 窗口：smeltItem 后续要读 inputItem/fuelItem/progressSeconds 等
                inputItem: () => null,
                fuelItem: () => null,
                outputItem: () => null,
                fuelSeconds: null,
                progressSeconds: 0,
                async putInput() {}, async putFuel() {}, async takeOutput() {},
                async close() {},
            };
        },
        inventory: {
            findInventoryItem: (id) => invItems.find((i) => i.type === id) || null,
            count: (id) => invItems.filter((i) => i.type === id).reduce((s, i) => s + i.count, 0),
        },
        async waitForTicks() { /* no-op */ },
        chat: () => {},
    };
    return { bot, state };
}

// 把 smeltItem.js 装进闭包：它依赖 mcData, bot, GoalNear, Vec3, _smeltItemFailCount（index.js 闭包提供）。
function loadSmeltItem(bot) {
    const ctx = {
        mcData, bot, console, Math, Infinity,
        _smeltItemFailCount: 0, // index.js 闭包里的失败计数器，测试给个初值
        Vec3: class { constructor(x, y, z) { this.x = x; this.y = y; this.z = z; } },
        GoalNear: class { constructor(x, y, z, r) { this.x = x; this.y = y; this.z = z; this.range = r; } },
    };
    vm.createContext(ctx);
    vm.runInContext(SRC + "\nthis.smeltItem = smeltItem;", ctx);
    return ctx.smeltItem;
}

async function main() {
    // ① 传 furnacePos 且该坐标真是 furnace → blockAt 命中、直接开炉（不走 32 格盲搜）
    {
        const { bot, state } = makeBot({
            blockAtMap: { "1,1,-10": "furnace" },
            findBlockResult: null, // 即使盲搜也是 null，证明没走盲搜
            invItems: [{ name: "raw_iron", type: 871, count: 8, slot: 1 }],
        });
        const smeltItem = loadSmeltItem(bot);
        await smeltItem(bot, "raw_iron", "coal", 8, { x: 1, y: 1, z: -10 });
        assert.ok(state.blockAtReads.includes("1,1,-10"), "① 应 blockAt 查给定坐标");
        assert.strictEqual(state.findBlockCalled, false, "① blockAt 命中后不应再 32 格盲搜");
        assert.strictEqual(state.openFurnaceCalled, true, "① 应开炉");
        console.log("✓ ① furnacePos 命中 → blockAt 直查、不走盲搜、直接开炉");
    }

    // ② 传 furnacePos 但坐标是 air → 走过去加载仍非 furnace → 退回 32 格盲搜
    {
        const fakeFurnace = { name: "furnace", position: { x: 5, y: 0, z: -10 } };
        const { bot, state } = makeBot({
            blockAtMap: {}, // (1,1,-10) 是 air
            findBlockResult: fakeFurnace, // 盲搜能找到别处的炉
            invItems: [{ name: "raw_iron", type: 871, count: 8, slot: 1 }],
        });
        const smeltItem = loadSmeltItem(bot);
        await smeltItem(bot, "raw_iron", "coal", 8, { x: 1, y: 1, z: -10 });
        assert.strictEqual(state.findBlockCalled, true, "② 坐标非炉子应退回 32 格盲搜");
        assert.strictEqual(state.openFurnaceCalled, true, "② 盲搜命中后应开炉");
        console.log("✓ ② furnacePos 非炉 → 退回盲搜、命中别处炉子");
    }

    // ③ 不传 furnacePos、32 格内有炉 → 旧路径盲搜命中
    {
        const fakeFurnace = { name: "furnace", position: { x: 5, y: 0, z: -10 } };
        const { bot, state } = makeBot({
            findBlockResult: fakeFurnace,
            invItems: [{ name: "raw_iron", type: 871, count: 8, slot: 1 }],
        });
        const smeltItem = loadSmeltItem(bot);
        await smeltItem(bot, "raw_iron", "coal", 8); // 不传第 5 参
        assert.strictEqual(state.findBlockCalled, true, "③ 不传坐标应走 32 格盲搜");
        assert.strictEqual(state.openFurnaceCalled, true, "③ 盲搜命中应开炉");
        console.log("✓ ③ 不传 furnacePos → 32 格盲搜命中（向后兼容）");
    }

    // ④ 不传 furnacePos、32 格内无炉 → 抛错提示"传 furnacePos 直查"
    {
        const { bot } = makeBot({ findBlockResult: null });
        const smeltItem = loadSmeltItem(bot);
        await assert.rejects(
            () => smeltItem(bot, "raw_iron", "coal", 8),
            /No furnace nearby.*没给坐标.*传给 smeltItem 第 5 个参数/,
            "④ 应提示传 furnacePos"
        );
        console.log("✓ ④ 不传坐标+无炉 → 抛错提示「传 furnacePos 直查」");
    }

    // ⑤ 传 furnacePos、两路都搜不到 → 抛错提示"已按坐标找过…重新 placeItem"
    {
        const { bot, state } = makeBot({
            blockAtMap: {}, // 坐标非炉
            findBlockResult: null, // 盲搜也无
        });
        const smeltItem = loadSmeltItem(bot);
        await assert.rejects(
            () => smeltItem(bot, "raw_iron", "coal", 8, { x: 1, y: 1, z: -10 }),
            /No furnace nearby.*已按你给的坐标.*placeItem 重新放一个/,
            "⑤ 应提示已按坐标找过、重新放炉"
        );
        assert.strictEqual(state.findBlockCalled, true, "⑤ 坐标无炉应再走盲搜兜底");
        console.log("✓ ⑤ 传坐标+两路无炉 → 抛错提示「重新 placeItem 放一个」");
    }

    console.log("\n全部 smeltItem 炉子查找契约测试通过 ✓");
}

main().catch((e) => { console.error(e); process.exit(1); });
