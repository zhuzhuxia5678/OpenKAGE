/**
 * consume 行为契约测试 —— **不连 MC 服务器**，用假 bot 跑 skillLoader.inject。
 *
 *     node test_consume_smoke.js
 *
 * 测什么（skillLoader.js 的 consume 包装，2026-08-25 修，根因见 memory
 * openkage_braindb_systembug_audit_20260825 的 pause_before_consume / health_regen）：
 *   ① 主手空、背包有食物 → 自动 equip 食物到主手再 consume（旧代码直接 consume 必静默超时）
 *   ② 主手握非食物（镐）、背包有食物 → 自动换上食物再 consume
 *   ③ 主手已是食物 → 不再 equip、直接 consume（避免无谓换手）
 *   ④ 主手非食物、背包也无食物 → **抛明确错**「没有可吃的食物」，而非 mineflayer 的静默超时/TypeError
 *   ⑤ consume 前停 pathfinder（setGoal(null)），避免与移动抢执行通道超时
 *   ⑥ bot.food===20 时透传 mineflayer 的 'Food is full'（这是有意义的"吃饱了"信号，不是 bug）
 *   ⑦ 显式传 bot.consume(foodItem) → 用指定食物 equip
 *   ⑧ 无 pathfinder 的旧 bot 不崩（best-effort setGoal）
 *
 * ★ 关键回归：旧包装（`await bot._consume()`）在主手空/非食物时会让 mineflayer 的 inventory.js:104
 *   `bot.heldItem.name` 抛 TypeError 或发空包挂满 CONSUME_TIMEOUT → "Promise timed out"，生命饥饿不变。
 *   长跑里 Carol 连试 melon_slice/rotten_flesh 全超时即此；Agent 被迫"先 equip 再静止半秒"绕过。
 */
const path = require("path");
const assert = require("assert");

// 直接 require skillLoader.js 拿到 inject 函数（module.exports = { inject }）。
const { inject } = require(path.join(__dirname, "lib", "skillLoader.js"));
assert.strictEqual(typeof inject, "function", "skillLoader 应 export inject 函数");

// ---- 假对象 ----
function makeItem(name, type) {
    return { name, type, count: 1 };
}

/**
 * 构造假 bot。关键：skillLoader 的 `bot._consume = bot.consume` 会先把"真实 consume"存进 _consume，
 * 再用包装覆盖 bot.consume。所以这里把真实实现挂在 bot.consume 上，inject 后 bot._consume 即是真实现、
 * bot.consume 即是包装。state 记录 equip / setGoal / consume 调用以断言。
 */
function makeBot({ heldItem = null, inventoryItems = [], consumeThrows = null, withPathfinder = true } = {}) {
    const state = { heldItem, equipped: [], goalCleared: false, consumeCalled: false };
    const bot = {
        get heldItem() { return state.heldItem; },
        set heldItem(v) { state.heldItem = v; },
        registry: {
            // mineflayer registry.foodsByName：键为食物名，由 minecraft-data foods.json 索引。
            foodsByName: Object.fromEntries(
                ["melon_slice", "cooked_porkchop", "apple", "rotten_flesh", "porkchop", "cooked_beef"]
                    .map((n) => [n, { name: n }])
            ),
        },
        inventory: { items: () => inventoryItems.slice() },
        async consume() {
            state.consumeCalled = true;
            if (consumeThrows) throw consumeThrows;
        },
        async equip(item, dest) {
            assert.strictEqual(dest, "hand", "consume 只往主手 equip");
            state.equipped.push(item.name);
            state.heldItem = item; // equip 后主手更新
        },
        async waitForTicks() { /* no-op */ },
    };
    if (withPathfinder) {
        bot.pathfinder = { setGoal: (g) => { state.goalCleared = (g === null); } };
    }
    inject(bot); // bot._consume = bot.consume（真实）；bot.consume = 包装
    return { bot, state };
}

// ---- 用例 ----
async function main() {
    // ① 主手空、背包有 melon_slice → equip melon_slice 再 consume + 停 pathfinder
    {
        const { bot, state } = makeBot({
            heldItem: null,
            inventoryItems: [makeItem("melon_slice", 1061)],
        });
        await bot.consume();
        assert.deepStrictEqual(state.equipped, ["melon_slice"], "① 应自动 equip melon_slice");
        assert.strictEqual(state.consumeCalled, true, "① 应调到真实 consume");
        assert.strictEqual(state.goalCleared, true, "① consume 前应停 pathfinder");
        console.log("✓ ① 主手空+背包有食物 → 自动 equip 再 consume + 停 pathfinder");
    }

    // ② 主手握非食物（pickaxe）、背包有 cooked_porkchop → 换上食物再 consume
    {
        const { bot, state } = makeBot({
            heldItem: makeItem("stone_pickaxe", 250),
            inventoryItems: [makeItem("stone_pickaxe", 250), makeItem("cooked_porkchop", 939)],
        });
        await bot.consume();
        assert.deepStrictEqual(state.equipped, ["cooked_porkchop"], "② 应 equip 食物而非保留镐");
        assert.strictEqual(state.consumeCalled, true, "② 应调到真实 consume");
        console.log("✓ ② 主手非食物+背包有食物 → 换上食物再 consume");
    }

    // ③ 主手已是食物 → 不再 equip、直接 consume
    {
        const { bot, state } = makeBot({
            heldItem: makeItem("apple", 857),
            inventoryItems: [makeItem("apple", 857)],
        });
        await bot.consume();
        assert.deepStrictEqual(state.equipped, [], "③ 主手已是食物就不该再 equip");
        assert.strictEqual(state.consumeCalled, true, "③ 应直接调真实 consume");
        assert.strictEqual(state.goalCleared, true, "③ 仍应停 pathfinder");
        console.log("✓ ③ 主手已是食物 → 不换手、直接 consume");
    }

    // ④ 主手非食物、背包也无食物 → 抛明确错（而非静默超时）
    {
        const { bot } = makeBot({
            heldItem: makeItem("stone_pickaxe", 250),
            inventoryItems: [makeItem("stone_pickaxe", 250), makeItem("dirt", 3)],
        });
        await assert.rejects(
            () => bot.consume(),
            /没有可吃的食物/,
            "④ 无食物时应抛「没有可吃的食物」明确错"
        );
        console.log("✓ ④ 无食物 → 抛明确错（不静默超时/不抛 TypeError）");
    }

    // ⑤ bot.food===20 时透传 mineflayer 的 'Food is full'
    {
        const { bot } = makeBot({
            heldItem: makeItem("cooked_porkchop", 939),
            inventoryItems: [makeItem("cooked_porkchop", 939)],
            consumeThrows: new Error("Food is full"),
        });
        await assert.rejects(
            () => bot.consume(),
            /Food is full/,
            "⑤ 应透传 'Food is full'（吃饱信号，不是 bug）"
        );
        console.log("✓ ⑤ food 满 → 透传 'Food is full'");
    }

    // ⑥ 显式传 bot.consume(foodItem) → 用指定食物 equip（背包里同时有多种食物）
    {
        const { bot, state } = makeBot({
            heldItem: null,
            inventoryItems: [makeItem("melon_slice", 1061), makeItem("cooked_beef", 847)],
        });
        await bot.consume(makeItem("cooked_beef", 847));
        assert.deepStrictEqual(state.equipped, ["cooked_beef"], "⑥ 显式指定应 equip 该食物");
        assert.strictEqual(state.consumeCalled, true, "⑥ 应调到真实 consume");
        console.log("✓ ⑥ 显式 bot.consume(foodItem) → equip 指定食物");
    }

    // ⑦ 无 pathfinder 的旧 bot 不崩（best-effort setGoal）
    {
        const { bot } = makeBot({
            heldItem: makeItem("apple", 857),
            inventoryItems: [makeItem("apple", 857)],
            withPathfinder: false,
        });
        await bot.consume(); // 无 pathfinder，不应抛
        console.log("✓ ⑦ 无 pathfinder 的 bot 不崩（best-effort）");
    }

    console.log("\n全部 consume 契约测试通过 ✓");
}

main().catch((e) => { console.error(e); process.exit(1); });
