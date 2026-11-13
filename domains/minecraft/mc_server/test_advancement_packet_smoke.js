/**
 * advancementPacket 采集器 smoke 测试 —— **不连 MC 服务器**，用假 bot 喂协议包。
 *
 *     node test_advancement_packet_smoke.js
 *
 * 测什么（lib/observation/advancementPacket.js，2026-09-07 建，onSystem 被动播报的佐证支路）：
 *   监听 packet_advancements 协议包，维护「已完成进度显示名」状态快照。能采到
 *   announce_to_chat=false 的静默进度（如 story/root=[Minecraft]），与 onSystem 互补。
 *
 * 用例：
 *   ① reset=true + story/root 全 criterion 达成 -> completed 含 "[Minecraft]"（★ 核心：root 能采到）
 *   ② 部分 criterion（一个 null）-> 不在 completed
 *   ③ reset=true 清空先前状态
 *   ④ 增量（reset=false）累加新达成进度，旧达成保留
 *   ⑤ requirements OR 组语义：[[c1,c2],[c3]] = (c1 OR c2) AND c3，部分组未满足 -> 未完成
 *   ⑥ requirements 全组满足 -> 完成
 *   ⑦ 未知 key（非 advancements 命名空间/lang.json 未收录）-> 跳过不抛
 *   ⑧ 畸形包（缺字段/null/非对象）-> 不抛、状态保留
 *   ⑨ 空/缺包 -> observe() 返回 {completed:[],completedKeys:[],storyTreeSeen:false}
 *   ⑩ storyTreeSeen 标志：收到 minecraft:story/* mapping 后置 true
 *
 * ★ 关键防御：_handle 失败会炸 bot 事件循环（协议包在 minecraft-protocol 解析路径上）。
 *   任何形态都绝不能让 _handle 抛。同 scoreboard/onSystem 的韧性要求。
 */
const path = require("path");
const assert = require("assert");

const AdvancementPacket = require(path.join(__dirname, "lib", "observation", "advancementPacket.js"));
assert.strictEqual(typeof AdvancementPacket, "function", "advancementPacket.js 应 export 类");

// ── 假 bot：提供 _client.on / _fire，version 用真 "1.21.11" 让 lang.json 加载真实 title ──
function makeBot(version) {
    const handlers = {};
    return {
        version: version || "1.21.11",
        _client: {
            on: (name, h) => { handlers[name] = h; },
        },
        _fire: (name, data) => { if (handlers[name]) handlers[name](data); },
    };
}

// 构造一个 progressMapping entry：criterionProgress 有值=达成，null=未达成。
function prog(key, crits) {
    // crits: [{id, done}]
    return {
        key,
        value: crits.map((c) => ({
            criterionIdentifier: c.id,
            criterionProgress: c.done ? [100, 1000] : null,
        })),
    };
}
// 构造一个 advancementMapping entry：带 requirements（OR 组数组）。
function mapWithReq(key, requirements) {
    return { key, value: { parentId: null, displayData: null, requirements } };
}
// 不带 requirements 的 mapping（用于纯 story 树标志探测）。
function mapBare(key) {
    return { key, value: { parentId: null, displayData: null, requirements: [] } };
}

let passed = 0;
function check(name, fn) {
    fn();
    passed++;
    console.log(`  ✓ ${name}`);
}

// ════════════════════════════════════════════════════ ① root 全达成 -> completed 含 [Minecraft]

check("reset=true + story/root 全 criterion 达成 -> completed 含 [Minecraft]", () => {
    const bot = makeBot();
    const ap = new AdvancementPacket(bot);
    bot._fire("advancements", {
        reset: true,
        advancementMapping: [mapBare("minecraft:story/root")],
        progressMapping: [prog("minecraft:story/root", [{ id: "crafting_table", done: true }])],
    });
    const out = ap.observe();
    assert.ok(out.completed.includes("[Minecraft]"), `应有 [Minecraft]，实际 ${JSON.stringify(out.completed)}`);
    assert.ok(out.completedKeys.includes("minecraft:story/root"));
    assert.strictEqual(out.storyTreeSeen, true);
});

// ════════════════════════════════════════════════════ ② 部分 criterion -> 未完成

check("部分 criterion（一个 null）-> 不在 completed", () => {
    const bot = makeBot();
    const ap = new AdvancementPacket(bot);
    bot._fire("advancements", {
        reset: true,
        advancementMapping: [mapBare("minecraft:story/obtain_armor")],
        progressMapping: [prog("minecraft:story/obtain_armor", [
            { id: "iron_helmet", done: true },
            { id: "iron_chestplate", done: false },  // 未达成
        ])],
    });
    const out = ap.observe();
    assert.ok(!out.completed.includes("[Suit Up]"), `不应有 [Suit Up]，实际 ${JSON.stringify(out.completed)}`);
});

// ════════════════════════════════════════════════════ ③ reset=true 清空

check("reset=true 清空先前已完成状态", () => {
    const bot = makeBot();
    const ap = new AdvancementPacket(bot);
    bot._fire("advancements", {
        reset: true,
        advancementMapping: [mapBare("minecraft:story/root")],
        progressMapping: [prog("minecraft:story/root", [{ id: "crafting_table", done: true }])],
    });
    assert.ok(ap.observe().completed.includes("[Minecraft]"));
    // 再来一个 reset=true 的空包
    bot._fire("advancements", { reset: true, advancementMapping: [], progressMapping: [] });
    const out = ap.observe();
    assert.deepStrictEqual(out.completed, []);
    // storyTreeSeen 不被空包重置（只升不降的健康标志）
    assert.strictEqual(out.storyTreeSeen, true);
});

// ════════════════════════════════════════════════════ ④ 增量累加，旧达成保留

check("增量（reset=false）累加新达成，旧达成保留", () => {
    const bot = makeBot();
    const ap = new AdvancementPacket(bot);
    // 第一包：root 达成
    bot._fire("advancements", {
        reset: true,
        advancementMapping: [mapBare("minecraft:story/root"), mapBare("minecraft:story/mine_stone")],
        progressMapping: [prog("minecraft:story/root", [{ id: "crafting_table", done: true }])],
    });
    // 第二包（增量）：mine_stone 达成
    bot._fire("advancements", {
        reset: false,
        progressMapping: [prog("minecraft:story/mine_stone", [{ id: "get_stone", done: true }])],
    });
    const out = ap.observe();
    assert.ok(out.completed.includes("[Minecraft]"), "旧达成 [Minecraft] 应保留");
    assert.ok(out.completed.includes("[Stone Age]"), "新达成 [Stone Age] 应在");
});

// ════════════════════════════════════════════════════ ⑤ requirements OR 组：部分组未满足 -> 未完成

check("requirements [[c1,c2],[c3]]：组2未满足 -> 未完成", () => {
    const bot = makeBot();
    const ap = new AdvancementPacket(bot);
    // obtain_armor 的 requirements 实际是 [[iron_helmet],[iron_chestplate],[iron_leggings],[iron_boots]]
    // 这里造一个 OR 组：[[a,b],[c]]，a 达成（满足组1）、c 未达成（组2未满足）
    bot._fire("advancements", {
        reset: true,
        advancementMapping: [mapWithReq("minecraft:story/obtain_armor", [["a", "b"], ["c"]])],
        progressMapping: [prog("minecraft:story/obtain_armor", [
            { id: "a", done: true },
            { id: "b", done: false },
            { id: "c", done: false },
        ])],
    });
    const out = ap.observe();
    assert.ok(!out.completed.includes("[Suit Up]"), "组2未满足，不应完成");
});

// ════════════════════════════════════════════════════ ⑥ requirements 全组满足 -> 完成

check("requirements [[a,b],[c]]：组1(b)、组2(c) 各满足 -> 完成", () => {
    const bot = makeBot();
    const ap = new AdvancementPacket(bot);
    bot._fire("advancements", {
        reset: true,
        advancementMapping: [mapWithReq("minecraft:story/obtain_armor", [["a", "b"], ["c"]])],
        progressMapping: [prog("minecraft:story/obtain_armor", [
            { id: "a", done: false },
            { id: "b", done: true },   // 组1 满足（OR）
            { id: "c", done: true },   // 组2 满足
        ])],
    });
    const out = ap.observe();
    assert.ok(out.completed.includes("[Suit Up]"), "全组满足应完成");
});

// ════════════════════════════════════════════════════ ⑦ 未知 key -> 跳过不抛

check("未知 key（非 advancements 命名空间）-> 跳过不抛", () => {
    const bot = makeBot();
    const ap = new AdvancementPacket(bot);
    assert.doesNotThrow(() => {
        bot._fire("advancements", {
            reset: true,
            advancementMapping: [mapBare("minecraft:recipes/root")],
            progressMapping: [prog("minecraft:recipes/root", [{ id: "impossible", done: true }])],
        });
    });
    const out = ap.observe();
    assert.deepStrictEqual(out.completed, [], "recipes 命名空间不该进 completed");
    assert.strictEqual(out.storyTreeSeen, false, "recipes 树不该触发 storyTreeSeen");
});

// ════════════════════════════════════════════════════ ⑧ 畸形包 -> 不抛、状态保留

check("畸形包（null/缺字段/非对象）-> 不抛、状态保留", () => {
    const bot = makeBot();
    const ap = new AdvancementPacket(bot);
    // 先放一个有效达成
    bot._fire("advancements", {
        reset: true,
        advancementMapping: [mapBare("minecraft:story/root")],
        progressMapping: [prog("minecraft:story/root", [{ id: "crafting_table", done: true }])],
    });
    // 一系列畸形包（不含 reset:true —— reset:true 是合法清空信号，不是畸形）
    assert.doesNotThrow(() => bot._fire("advancements", null));
    assert.doesNotThrow(() => bot._fire("advancements", {}));
    assert.doesNotThrow(() => bot._fire("advancements", { progressMapping: [{}] }));
    assert.doesNotThrow(() => bot._fire("advancements", { progressMapping: [{ key: "x" }] }));
    assert.doesNotThrow(() => bot._fire("advancements", { advancementMapping: "notarray" }));
    const out = ap.observe();
    // 上述畸形包都不该清掉有效达成（{} 不是 reset:true，null 被早返）
    assert.ok(out.completed.includes("[Minecraft]"), "畸形包不该清掉有效状态");
});

// ════════════════════════════════════════════════════ ⑨ 空包 -> observe 默认形态

check("从未收到包 -> observe() 返回默认空形态", () => {
    const bot = makeBot();
    const ap = new AdvancementPacket(bot);
    const out = ap.observe();
    assert.deepStrictEqual(out, { completed: [], completedKeys: [], storyTreeSeen: false });
});

// ════════════════════════════════════════════════════ ⑩ storyTreeSeen 只升不降

check("storyTreeSeen：收到 story mapping 后 true，之后 recipes 包不降回 false", () => {
    const bot = makeBot();
    const ap = new AdvancementPacket(bot);
    bot._fire("advancements", {
        reset: true,
        advancementMapping: [mapBare("minecraft:story/root")],
        progressMapping: [],
    });
    assert.strictEqual(ap.observe().storyTreeSeen, true);
    // 之后来个只有 recipes 的包（reset=true 会清 completed，但 storyTreeSeen 是只升标志）
    bot._fire("advancements", {
        reset: true,
        advancementMapping: [mapBare("minecraft:recipes/root")],
        progressMapping: [],
    });
    assert.strictEqual(ap.observe().storyTreeSeen, true, "storyTreeSeen 不该被 recipes 包重置");
});

// ════════════════════════════════════════════════════ ⑪ revoke 场景：已完成变部分 -> 移出 completed

check("revoke 后进度变部分达成 -> 从 completed 移除", () => {
    const bot = makeBot();
    const ap = new AdvancementPacket(bot);
    bot._fire("advancements", {
        reset: true,
        advancementMapping: [mapBare("minecraft:story/root")],
        progressMapping: [prog("minecraft:story/root", [{ id: "crafting_table", done: true }])],
    });
    assert.ok(ap.observe().completed.includes("[Minecraft]"));
    // revoke：criterionProgress 变 null（增量包，reset=false）
    bot._fire("advancements", {
        reset: false,
        progressMapping: [prog("minecraft:story/root", [{ id: "crafting_table", done: false }])],
    });
    const out = ap.observe();
    assert.ok(!out.completed.includes("[Minecraft]"), "revoke 后应移出 completed");
    assert.ok(!out.completedKeys.includes("minecraft:story/root"));
});

console.log(`\n${passed} passed`);
