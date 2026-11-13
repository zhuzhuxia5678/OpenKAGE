/**
 * craftItem 行为契约测试 —— **不连 MC 服务器**，用假 bot 跑。
 *
 *     node test_craftItem_smoke.js
 *
 * 测什么（craftItem.js 的库存满预检 + 吞料上报，2026-08-25 修，根因见 memory
 * openkage_braindb_systembug_audit_20260825 的 slot_full_craft_fake_success）：
 *   ① 库存满（emptySlotCount=0）且配方/料都够 → **进 craft 前就抛错**，绝不调 bot.craft
 *      —— 旧代码会进 craft，mineflayer 把产物/材料 tossLeftover 扔地上、不报错 = 吞料。
 *   ② 库存有空格、craft 假成功（产物 0 入账）且材料真被扣 → 抛错信息里**点名丢了多少料**
 *      （"丢失 8 个 diamond"），而非只说"没合成"。长跑里"合钻石胸甲被扣 8 钻 0 产物"即此。
 *   ③ 库存有空格、craft 真成功（产物入账）→ 如实报 got N，不误报吞料。
 *   ④ 库存满但 emptySlotCount 不是函数（旧 mineflayer）→ 跳过预检，退回靠 _realGain 兜底
 *      （不因预检逻辑误伤旧版 bot）。
 *
 * ★ 关键回归：mineflayer craft.js 在背包满时 putAway→tossLeftover 把物品扔地上、bot.craft 不抛错。
 *   旧 craftItem 只在事后用 _realGain<=0 检出"没产物"并抛错，但料已丢、错误里不提丢料。
 */
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const assert = require("assert");

const CRAFT_SRC = fs.readFileSync(
    path.join(__dirname, "control_primitives", "craftItem.js"),
    "utf-8"
);

// 假 mcData：diamond=264, stick=280, diamond_chestplate=311
const mcData = {
    itemsByName: {
        diamond: { id: 264, name: "diamond" },
        stick: { id: 280, name: "stick" },
        diamond_chestplate: { id: 311, name: "diamond_chestplate" },
        crafting_table: { id: 58, name: "crafting_table" },
    },
    blocksByName: { crafting_table: { id: 58 } },
    // mcData.items[id] 数组式（与 mineBlock.js:37 / craftHelper.js:212 同口径）
    items: {
        264: { id: 264, name: "diamond" },
        280: { id: 280, name: "stick" },
        311: { id: 311, name: "diamond_chestplate" },
    },
};

/**
 * 构造假 bot + 把 craftItem.js 装进一个闭包执行，返回 { craftItem, bot, state }。
 * craftItem.js 依赖：mcData, bot, failedCraftFeedback, _craftItemFailCount（后两者由本闭包提供）。
 */
function makeHarness({ emptySlotCount, recipeDelta, recipeResult, craftConsumesMaterials, craftProduces, invCounts } = {}) {
    // invCounts: { itemId: number } 初始库存数；craftConsumesMaterials/craftProduces 在 bot.craft 时改它。
    const inv = Object.assign({}, invCounts || {});
    const state = {
        craftCalled: false,
        chatLog: [],
    };
    const bot = {
        // findBlock：返回一个工作台（craftItem 要 goto 它；测试里 pathfinder.goto 是 no-op）
        findBlock: () => ({ position: { x: 0, y: 0, z: 0 } }),
        pathfinder: { goto: async () => {} },
        // recipesFor：返回一个 baseRecipe（测试驱动 delta/result）
        recipesFor: (id) => {
            if (id !== 311) return [];
            return [{ delta: recipeDelta, result: { id: 311, count: recipeResult } }];
        },
        // recipesAll：返回非空表示配方客观存在（让 craftItem 进 baseRecipe 分支）
        recipesAll: () => [{ delta: recipeDelta, result: { id: 311, count: recipeResult } }],
        inventory: {
            count: (id) => inv[id] || 0,
            emptySlotCount: typeof emptySlotCount === "number"
                ? () => emptySlotCount
                : (emptySlotCount === "absent" ? undefined : () => 1),
        },
        // bot.craft：模拟 mineflayer 行为——按 craftConsumesMaterials 扣料、按 craftProduces 加产物。
        // craftConsumesMaterials=true 时扣 delta 材料（模拟"料被点进合成格后 tossLeftover 丢了"）；
        // craftProduces=true 时加产物（模拟正常入账）。两者可独立，构造"假成功吞料"场景。
        async craft(recipe, count, table) {
            state.craftCalled = true;
            if (craftConsumesMaterials) {
                for (const d of recipe.delta) {
                    if (d.count < 0) inv[d.id] = Math.max(0, (inv[d.id] || 0) + d.count * count);
                }
            }
            if (craftProduces) {
                inv[recipe.result.id] = (inv[recipe.result.id] || 0) + recipe.result.count * count;
            }
        },
        async waitForTicks() { /* no-op */ },
        chat: (m) => { state.chatLog.push(m); },
    };

    // 闭包：提供 craftItem.js 期望的 failedCraftFeedback / _craftItemFailCount / mcData / GoalLookAtBlock。
    const failedCraftFeedback = () => { /* 测试不走这里（recipesAll 非空）*/ };
    let _craftItemFailCount = 0;
    const GoalLookAtBlock = class {};
    const ctx = {
        mcData, bot, failedCraftFeedback, _craftItemFailCount, GoalLookAtBlock,
        console, Math, Infinity,
    };
    vm.createContext(ctx);
    // 把 craftItem.js 里的 async function craftItem 暴露出来
    vm.runInContext(CRAFT_SRC + "\nthis.craftItem = craftItem;", ctx);
    return { craftItem: ctx.craftItem, bot, state, inv };
}

async function main() {
    // ① 库存满（emptySlotCount=0）+ 配方料都够 → 进 craft 前就抛错，绝不调 bot.craft
    {
        const h = makeHarness({
            emptySlotCount: 0,
            recipeDelta: [{ id: 264, count: -8 }], // 钻石胸甲用 8 钻
            recipeResult: 1,
            invCounts: { 264: 10 }, // 料够
        });
        await assert.rejects(
            () => h.craftItem(h.bot, "diamond_chestplate", 1),
            /库存已满.*tossLeftover 扔到地上.*吞料/,
            "① 库存满应抛吞料预警错"
        );
        assert.strictEqual(h.state.craftCalled, false, "① 库存满时绝不该调到 bot.craft（料会被吞）");
        console.log("✓ ① 库存满 → 预检拦截、不进 craft、抛吞料预警");
    }

    // ② 库存有空格、craft 假成功（0 产物）+ 材料真被扣 → 抛错点名"丢失 8 个 diamond"
    {
        const h = makeHarness({
            emptySlotCount: 1,
            recipeDelta: [{ id: 264, count: -8 }],
            recipeResult: 1,
            craftConsumesMaterials: true, // 模拟材料被点进合成格后丢了
            craftProduces: false,         // 产物没入账 = 假成功
            invCounts: { 264: 8 },
        });
        await assert.rejects(
            () => h.craftItem(h.bot, "diamond_chestplate", 1),
            /假成功.*丢失 8 个 diamond.*8→0/,
            "② 应点名丢失 8 个 diamond"
        );
        assert.strictEqual(h.state.craftCalled, true, "② 有空格时该进 craft");
        console.log("✓ ② 假成功吞料 → 抛错点名「丢失 8 个 diamond」（不只说没合成）");
    }

    // ③ 库存有空格、craft 真成功 → 如实报 got N，不误报吞料
    {
        const h = makeHarness({
            emptySlotCount: 1,
            recipeDelta: [{ id: 264, count: -8 }],
            recipeResult: 1,
            craftConsumesMaterials: true,
            craftProduces: true,
            invCounts: { 264: 8 },
        });
        await h.craftItem(h.bot, "diamond_chestplate", 1);
        const did = h.state.chatLog.some((m) => /I did the recipe.*got 1 diamond_chestplate/.test(m));
        assert.strictEqual(did, true, "③ 真成功应报 got 1 diamond_chestplate");
        console.log("✓ ③ 真成功 → 如实报 got 1，不误报吞料");
    }

    // ④ 旧 mineflayer 无 emptySlotCount → 跳过预检，靠 _realGain 兜底（不误伤旧版）
    {
        const h = makeHarness({
            emptySlotCount: "absent", // 旧版没有这个方法
            recipeDelta: [{ id: 264, count: -8 }],
            recipeResult: 1,
            craftConsumesMaterials: false,
            craftProduces: true, // 真成功
            invCounts: { 264: 8 },
        });
        await h.craftItem(h.bot, "diamond_chestplate", 1); // 不应因预检逻辑抛错
        const did = h.state.chatLog.some((m) => /got 1 diamond_chestplate/.test(m));
        assert.strictEqual(did, true, "④ 无 emptySlotCount 时正常路径应放行");
        console.log("✓ ④ 旧 mineflayer（无 emptySlotCount）→ 跳过预检、不误伤");
    }

    console.log("\n全部 craftItem 契约测试通过 ✓");
}

main().catch((e) => { console.error(e); process.exit(1); });
