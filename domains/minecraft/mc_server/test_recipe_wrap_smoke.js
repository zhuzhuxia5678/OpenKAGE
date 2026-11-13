// recipeWrap.js 的单测：read_text 拿到自包含源码（与 env.py 注入同口径），eval 后验证各分支。
// 跑法：node domains/minecraft/mc_server/test_recipe_wrap_smoke.js
//
// 思路：recipeWrap.js 是纯执行代码（无 module.exports），env.py read_text 拼进 programs。
//   这里同样 read_text，在隔离作用域 eval，注入 mock 的 require / bot / mcData，验证包装后的
//   bot.recipesFor/recipesAll/craft 在各场景下行为正确。整个文件用 async main 包起来，场景
//   串行跑、统一结算。
const fs = require("fs");
const path = require("path");

const source = fs.readFileSync(
    path.join(__dirname, "lib", "recipeWrap.js"), "utf8"
);

let failures = 0;
function assert(cond, msg) {
    if (!cond) { console.error("✗ " + msg); failures++; }
    else console.log("✓ " + msg);
}

// mock Recipe：按 itemType 返回预设的 recipe 数组。
function makeMockRecipe(findMap) {
    return { find: (itemType) => findMap[itemType] || [] };
}
// mock require：只回应 prismarine-recipe 和 minecraft-data
function makeRequire(mockRecipe, mockMcData) {
    return (mod) => {
        if (mod === "prismarine-recipe") return () => ({ Recipe: mockRecipe });
        if (mod === "minecraft-data") return () => mockMcData;
        throw new Error("unexpected require: " + mod);
    };
}
// 构造假 bot：recipesFor/recipesAll 返回固定值（模拟 mineflayer 传 null 时返回空）
function makeFakeBot(origFor, origAll) {
    return {
        recipesFor: () => origFor,
        recipesAll: () => origAll,
        craft: async () => { throw new Error("原始 craft 不该被这样调到（测试应覆盖）"); },
        inventory: { count: () => 0 },
        registry: {},
        __openkageRecipeWrap: false,
    };
}
function applyWrap(src, bot, mockRecipe, mockMcData) {
    const require = makeRequire(mockRecipe, mockMcData);
    const mcData = mockMcData;
    // eslint-disable-next-line no-eval
    eval(src);
    return bot;
}
function withCapturedLog(fn) {
    const logs = [];
    const orig = console.log;
    console.log = (s) => logs.push(s);
    try { fn(); } finally { console.log = orig; }
    return logs;
}

// ★ 负向断言：诊断串只能含客观事实，不能含处方（教 Agent 下一步做什么）。
//   处方词出现 = 薄壳越界回了原语老路（替 Agent 推理"该做什么"）。
const _PRESCRIPTIVE = ["先做", "先找一个", "补齐", "再查", "再 findBlock", "传进第4参", "做工作台", "建议", "应该", "请"];
function assertPrescriptiveFree(msg, label) {
    for (const w of _PRESCRIPTIVE) {
        if (msg && msg.includes(w)) {
            assert(false, `${label} 诊断含处方词「${w}」（应只报事实）: ${msg}`);
            return;
        }
    }
    assert(true, `${label} 诊断只报事实不含处方`);
}

async function main() {
    assert(source.includes("bot.recipesFor = function"), "源码包含 recipesFor 包装");
    assert(source.includes("__openkageRecipeWrap"), "源码含幂等标记");
    assert(source.includes("bot.craft = async"), "源码包含 craft 包装");
    assert(!/^\s*module\.exports\s*=/.test(source) && !/\n\s*module\.exports\s*=/.test(source),
        "源码无 module.exports 赋值语句（纯执行代码）");

    // === 场景1：木镐 requiresTable=true，调用者传 null → 报「需要工作台」纯事实 ===
    {
        const pickId = 913;
        const mockRecipe = makeMockRecipe({
            [pickId]: [{ requiresTable: true, delta: [{ id: 5, metadata: null, count: -3 }, { id: 280, metadata: null, count: -2 }], result: { id: pickId, count: 1 } }],
        });
        const mockMcData = { items: { 913: { name: "wooden_pickaxe" }, 5: { name: "oak_planks" }, 280: { name: "stick" } } };
        const bot = makeFakeBot([], []);
        applyWrap(source, bot, mockRecipe, mockMcData);
        const logs = withCapturedLog(() => bot.recipesFor(pickId, null, 1, null));
        assert(logs.length === 1, "场景1 报了1条诊断");
        assert(logs[0] && logs[0].includes("需要工作台") && logs[0].includes("null"), "场景1 诊断含事实「需要工作台」+ null");
        assertPrescriptiveFree(logs[0], "场景1");
    }
    // === 场景2：配方存在、不缺工作台，但缺料 → 报缺料点名（纯事实）===
    {
        const plankId = 5;
        const mockRecipe = makeMockRecipe({
            [plankId]: [{ requiresTable: false, delta: [{ id: 17, metadata: null, count: -1 }], result: { id: plankId, count: 4 } }],
        });
        const mockMcData = { items: { 5: { name: "oak_planks" }, 17: { name: "oak_log" } } };
        const bot = makeFakeBot([], []);
        bot.inventory.count = () => 0;
        applyWrap(source, bot, mockRecipe, mockMcData);
        const logs = withCapturedLog(() => bot.recipesFor(plankId, null, 1, null));
        assert(logs.length === 1, "场景2 报了1条诊断");
        assert(logs[0] && logs[0].includes("缺料") && logs[0].includes("oak_log"), "场景2 诊断点名缺 oak_log");
        assertPrescriptiveFree(logs[0], "场景2");
    }
    // === 场景3：配方客观不存在 → 报「无任何配方」（纯事实）===
    {
        const mockRecipe = makeMockRecipe({});
        const mockMcData = { items: {} };
        const bot = makeFakeBot([], []);
        applyWrap(source, bot, mockRecipe, mockMcData);
        const logs = withCapturedLog(() => bot.recipesFor(99999, null, 1, null));
        assert(logs.length === 1, "场景3 报了1条诊断");
        assert(logs[0] && logs[0].includes("无任何配方"), "场景3 诊断含「无任何配方」");
        assertPrescriptiveFree(logs[0], "场景3");
    }
    // === 场景4：返回非空 → 不报 ===
    {
        const pickId = 913;
        const mockRecipe = makeMockRecipe({
            [pickId]: [{ requiresTable: true, delta: [{ id: 5, metadata: null, count: -3 }], result: { id: pickId, count: 1 } }],
        });
        const mockMcData = { items: {} };
        const bot = makeFakeBot([{ requiresTable: true, delta: [] }], [{ requiresTable: true, delta: [] }]);
        applyWrap(source, bot, mockRecipe, mockMcData);
        const logs = withCapturedLog(() => bot.recipesFor(pickId, null, 1, "fakeTable"));
        assert(logs.length === 0, "场景4 返回非空时不报诊断");
    }
    // === 场景5：幂等 ===
    {
        const mockRecipe = makeMockRecipe({ 913: [{ requiresTable: true, delta: [], result: { id: 913, count: 1 } }] });
        const mockMcData = { items: {} };
        const bot = makeFakeBot([], []);
        applyWrap(source, bot, mockRecipe, mockMcData);
        const firstFn = bot.recipesFor;
        applyWrap(source, bot, mockRecipe, mockMcData);
        assert(bot.recipesFor === firstFn, "场景5 重复注入不二次包装");
    }
    // === 场景6：bot.craft(null) → 抛可读错误（非不可读的 AssertionError），且只报事实 ===
    {
        const mockRecipe = makeMockRecipe({});
        const mockMcData = { items: {} };
        const bot = makeFakeBot([], []);
        applyWrap(source, bot, mockRecipe, mockMcData);
        let threw = false, errMsg = "";
        try { await bot.craft(null, 1, null); } catch (e) { threw = true; errMsg = e.message; }
        assert(threw, "场景6 craft(null) 抛错");
        assert(errMsg.includes("recipe 参数为 null"), "场景6 抛可读错误点明 recipe 为 null: " + errMsg);
        assert(!errMsg.includes("ERR_ASSERTION") && !errMsg.includes("falsy value"), "场景6 不是不可读的 AssertionError");
        assertPrescriptiveFree(errMsg, "场景6");
    }
    // === 场景7：craft 合法 recipe → 透传原始 craft，不干预 ===
    {
        const mockRecipe = makeMockRecipe({});
        const mockMcData = { items: {} };
        const bot = makeFakeBot([], []);
        let origCalled = false;
        bot.craft = async () => { origCalled = true; return "ok"; };
        applyWrap(source, bot, mockRecipe, mockMcData);
        const r = await bot.craft({ result: { id: 1, count: 1 } }, 1, null);
        assert(origCalled, "场景7 合法 recipe 透传到原始 craft");
        assert(r === "ok", "场景7 透传返回值不变");
    }
}

main().then(() => {
    if (failures) { console.error(`\n${failures} 项失败`); process.exit(1); }
    else { console.log("\n全部通过"); process.exit(0); }
}).catch((e) => {
    console.error("测试运行异常:", e); process.exit(1);
});
