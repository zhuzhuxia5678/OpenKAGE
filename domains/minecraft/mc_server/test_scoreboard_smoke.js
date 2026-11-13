/**
 * scoreboard 采集器 smoke 测试 —— **不连 MC 服务器**，用假 bot.scoreboards 跑 observe()。
 *
 *     node test_scoreboard_smoke.js
 *
 * 测什么（lib/observation/scoreboard.js，2026-08-26 建，记分板交叉校验旁证的数据源）：
 *   ① 6 个 objective 都在、bot 单 holder 时，observe 正确扁平成 {objectives:{name:{name,title,scores}}}
 *   ② value 非 number（null/字符串）时记 null，不抛（采集器侧异常不该炸整步 observe 回传）
 *   ③ 空 bot.scoreboards / undefined 时返回 {objectives:{}}，不抛
 *   ④ itemsMap 里有 displayName getter 的对象（真实 mineflayer 形态）→ 只取 value，不触发 getter
 *      （getter 会 new ChatMessage，序列化它既慢又可能抛）
 *   ⑤ 多 holder 全取（采集器不挑 holder，Python 侧 _extract_scoreboard 负责选 bot 自己的）
 *
 * ★ 关键防御：observe() 失败会炸整个 step 回传（base.js 遍历调每个采集器，一个抛全完）。
 *   记分板是可选旁证，任何形态都绝不能让 observe() 抛。
 */
const path = require("path");
const assert = require("assert");

const Scoreboard = require(path.join(__dirname, "lib", "observation", "scoreboard.js"));
assert.strictEqual(typeof Scoreboard, "function", "scoreboard.js 应 export 类");

// ── 构造 mineflayer 真实形态的 itemsMap item：{name, value, displayName(getter)} ──
// getter 是故意的——真实 mineflayer 的 item 对象有 displayName getter（见 lib/scoreboard.js:30），
// 直接 JSON.stringify 整个 item 会触发它（new ChatMessage），既慢又可能抛。采集器必须只取 value。
function realItem(name, value) {
    return {
        name,
        value,
        get displayName() {
            throw new Error("displayName getter 不该被采集器触发");
        },
    };
}

function makeBot(scoreboards) {
    return { scoreboards };
}

let passed = 0;
function check(name, fn) {
    fn();
    passed++;
    console.log(`  ✓ ${name}`);
}

// ════════════════════════════════════════════════════ ① 全量提取

check("6 objective 全在、单 holder → 扁平成 objectives.scores", () => {
    const bot = makeBot({
        oct_death: { name: "oct_death", title: "oct_death", itemsMap: { alice: realItem("alice", 3) } },
        oct_kill: { name: "oct_kill", title: "oct_kill", itemsMap: { alice: realItem("alice", 17) } },
        oct_xp: { name: "oct_xp", title: "oct_xp", itemsMap: { alice: realItem("alice", 42) } },
        oct_health: { name: "oct_health", title: "oct_health", itemsMap: { alice: realItem("alice", 18.5) } },
        oct_food: { name: "oct_food", title: "oct_food", itemsMap: { alice: realItem("alice", 20) } },
        oct_armor: { name: "oct_armor", title: "oct_armor", itemsMap: { alice: realItem("alice", 12) } },
    });
    const out = new Scoreboard(bot).observe();
    assert.deepStrictEqual(out.objectives.oct_death.scores, { alice: 3 });
    assert.deepStrictEqual(out.objectives.oct_xp.scores, { alice: 42 });
    assert.strictEqual(out.objectives.oct_health.scores.alice, 18.5);
    assert.strictEqual(out.objectives.oct_armor.scores.alice, 12);
});

// ════════════════════════════════════════════════════ ② 非 number value → null，不抛

check("value=null → scores 里记 null", () => {
    const bot = makeBot({
        oct_death: { name: "oct_death", title: "oct_death", itemsMap: { alice: realItem("alice", null) } },
    });
    const out = new Scoreboard(bot).observe();
    assert.strictEqual(out.objectives.oct_death.scores.alice, null);
});

check("value=字符串 → scores 里记 null", () => {
    const bot = makeBot({
        oct_xp: { name: "oct_xp", title: "oct_xp", itemsMap: { alice: { name: "alice", value: "N/A" } } },
    });
    const out = new Scoreboard(bot).observe();
    assert.strictEqual(out.objectives.oct_xp.scores.alice, null);
});

// ════════════════════════════════════════════════════ ③ 空形态不抛

check("bot.scoreboards=undefined → {objectives:{}}", () => {
    const out = new Scoreboard({}).observe();
    assert.deepStrictEqual(out, { objectives: {} });
});

check("bot.scoreboards=null → {objectives:{}}", () => {
    const out = new Scoreboard({ scoreboards: null }).observe();
    assert.deepStrictEqual(out, { objectives: {} });
});

check("空 objective dict → {objectives:{}}", () => {
    const out = new Scoreboard(makeBot({})).observe();
    assert.deepStrictEqual(out, { objectives: {} });
});

// ════════════════════════════════════════════════════ ④ displayName getter 不被触发

check("itemsMap item 有 displayName getter → observe 只取 value、不触发 getter", () => {
    // realItem 的 getter 一旦被访问就抛；若采集器误 stringify 整个 item，这里会抛错
    const bot = makeBot({
        oct_death: { name: "oct_death", title: "oct_death", itemsMap: { alice: realItem("alice", 5) } },
    });
    const out = new Scoreboard(bot).observe();
    assert.strictEqual(out.objectives.oct_death.scores.alice, 5);
    // 且能被 JSON.stringify（observe 回传走 JSON）而不触发 getter
    assert.doesNotThrow(() => JSON.stringify(out));
});

// ════════════════════════════════════════════════════ ⑤ 多 holder 全取

check("多 holder → scores 全带，采集器不挑（Python 侧负责选 bot 自己的）", () => {
    const bot = makeBot({
        oct_death: {
            name: "oct_death", title: "oct_death",
            itemsMap: { alice: realItem("alice", 3), bob: realItem("bob", 9) },
        },
    });
    const out = new Scoreboard(bot).observe();
    assert.deepStrictEqual(out.objectives.oct_death.scores, { alice: 3, bob: 9 });
});

// ════════════════════════════════════════════════════ ⑥ itemsMap 缺失/objective 异常

check("objective 无 itemsMap 字段 → scores 为空 dict，不抛", () => {
    const bot = makeBot({ oct_death: { name: "oct_death", title: "oct_death" } }); // 无 itemsMap
    const out = new Scoreboard(bot).observe();
    assert.deepStrictEqual(out.objectives.oct_death.scores, {});
});

check("objective 本身为 null → 跳过，不进 objectives", () => {
    const bot = makeBot({ oct_death: null, oct_xp: { name: "oct_xp", title: "oct_xp", itemsMap: { alice: realItem("alice", 7) } } });
    const out = new Scoreboard(bot).observe();
    assert(!("oct_death" in out.objectives), "null objective 应被跳过");
    assert.strictEqual(out.objectives.oct_xp.scores.alice, 7);
});

console.log(`\n${passed} passed`);
