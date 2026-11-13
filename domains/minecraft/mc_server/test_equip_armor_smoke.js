/**
 * equip 装护甲/副手校验契约测试 —— **不连 MC 服务器**，用假 bot 跑 skillLoader.inject。
 *
 *     node test_equip_armor_smoke.js
 *
 * 测什么（skillLoader.js 的 equip 包装，2026-08-25 修，根因见 memory
 * openkage_braindb_systembug_audit_20260825 的 armor_slot_occupied_by_block）：
 *   ① equip(diamond_chestplate,'torso') 槽位 6 真装上 → 不抛错、slots[6] 是胸甲
 *   ② equip 后槽位仍是旧物（被方块顶占）→ 重试 3 次仍失败 → **抛明确错**点名"槽位仍是 X"
 *      （旧代码 equip 正常返回、Agent 误以为穿好了 = armor_slot_occupied_by_block 真因）
 *   ③ equip('hand') → 走原路径，不进护甲校验（hand 的竞态由 placeItem 兜底）
 *   ④ 第二次重试时槽位才装上 → 校验成功（模拟窗口同步延迟，重试后到账）
 *   ⑤ 未知目标名（如 'pocket'）→ 交给 mineflayer 原 equip，不在此吞错
 *
 * ★ 关键回归：mineflayer equip 对非 hand 目标走 moveSlotItem(clickWindow 串)，窗口同步延迟下
 *   equip 返回正常但护甲槽仍是旧物。长跑 erin/bob 反复记"护甲没真穿上、装备槽被方块顶占"。
 */
const path = require("path");
const assert = require("assert");

const { inject } = require(path.join(__dirname, "lib", "skillLoader.js"));
assert.strictEqual(typeof inject, "function", "skillLoader 应 export inject 函数");

// 护甲槽索引（与 status.js:124-129 / simple_inventory.js:12-19 同）：head=5 torso=6 legs=7 feet=8 off-hand=45
const SLOT = { head: 5, torso: 6, legs: 7, feet: 8, "off-hand": 45 };

function makeItem(name, type) {
    return { name, type, count: 1, slot: 10 };
}

/**
 * 假 bot。slotsSim 是槽位号->item 的状态机，由真实 _equip 写入。
 * failUntil: 前 N 次 _equip 不让槽位生效（模拟窗口同步延迟/被顶占），第 N+1 次生效。
 */
function makeBot({ failUntil = -1, slots = {} } = {}) {
    const state = { equipCalls: 0, slots: Object.assign({}, slots) };
    const bot = {
        inventory: {
            get slots() { return state.slots; }, // mineflayer slots 是数组；这里用对象 + getter 模拟读 slots[N]
            findInventoryItem(type) {
                // 重试时 equip 会重取一份权威 item；返回一个 type 匹配的假 item
                return makeItem("diamond_chestplate", type);
            },
        },
        // 真实 equip：skillLoader 会 `bot._equip = bot.equip` 存起来再覆盖 bot.equip。
        // 所以把真实实现挂在 bot.equip 上，inject 后 bot._equip 即真实现、bot.equip 即包装。
        async equip(item, dest) {
            state.equipCalls++;
            // 真实 mineflayer 会把 item 移进护甲槽；这里按 failUntil 模拟"装没装上"
            if (state.equipCalls > failUntil) {
                state.slots[SLOT[dest]] = item;
            }
        },
        async waitForTicks() { /* no-op */ },
    };
    inject(bot); // bot._equip = bot.equip（真实）；bot.equip = 包装
    return { bot, state };
}

// 注意：makeBot 里 bot.inventory.slots 用对象模拟，但包装读 bot.inventory.slots[_slot]
// 会触发 getter 返回对象、再 [N] 取。需保证读法一致。下面用例直接断言 state.slots。

async function main() {
    // ① equip 胸甲到 torso，槽位 6 真装上 → 不抛错
    {
        const { bot, state } = makeBot();
        const chest = makeItem("diamond_chestplate", 311);
        await bot.equip(chest, "torso");
        assert.strictEqual(state.slots[SLOT.torso], chest, "① torso 槽位应装上胸甲");
        assert.strictEqual(state.equipCalls, 1, "① 一次成功、无需重试");
        console.log("✓ ① equip 胸甲到 torso → 槽位真装上、不抛错");
    }

    // ② 槽位被方块顶占、3 次重试仍装不上 → 抛明确错点名"槽位仍是 X"
    {
        const block = makeItem("dirt", 3); // 槽位被方块占着
        const { bot, state } = makeBot({ failUntil: 999, slots: { 6: block } });
        const chest = makeItem("diamond_chestplate", 311);
        await assert.rejects(
            () => bot.equip(chest, "torso"),
            /equip\(torso\) 竞态.*槽位仍是 dirt.*重试本步通常可成/,
            "② 3 次失败应抛竞态错点名旧物"
        );
        assert.strictEqual(state.equipCalls, 3, "② 应重试满 3 次");
        console.log("✓ ② 槽位被占+重试失败 → 抛错点名「槽位仍是 dirt」（不静默成功）");
    }

    // ③ equip('hand') → 走原路径，不进护甲校验（不应抛、equipCalls 只 +1 走真实 _equip）
    {
        const { bot, state } = makeBot();
        const sword = makeItem("diamond_sword", 276);
        // hand 路径不校验护甲槽；但 makeBot 的 _equip 对未知 SLOT[undefined] 会写 NaN 槽——
        // 这里只验证不抛错、且走了真实 _equip。
        await bot.equip(sword, "hand");
        assert.strictEqual(state.equipCalls, 1, "③ hand 走原 _equip 一次");
        console.log("✓ ③ equip('hand') → 走原路径、不进护甲校验");
    }

    // ④ 第 2 次重试时槽位才装上 → 校验成功（模拟窗口同步延迟，重试到账）
    {
        const { bot, state } = makeBot({ failUntil: 1 }); // 第 1 次失败、第 2 次起生效
        const chest = makeItem("diamond_chestplate", 311);
        await bot.equip(chest, "torso");
        assert.ok(state.slots[SLOT.torso], "④ 重试后槽位应装上");
        assert.strictEqual(state.slots[SLOT.torso].type, 311, "④ 装上的是胸甲");
        assert.strictEqual(state.equipCalls, 2, "④ 第 1 次失败、第 2 次成功");
        console.log("✓ ④ 窗口延迟 → 重试后到账、校验成功");
    }

    // ⑤ 未知目标名 → 交给 mineflayer 原 equip（不在此吞错）
    {
        const { bot, state } = makeBot();
        const x = makeItem("diamond_chestplate", 311);
        // dest='pocket' 不在 _ARMOR_SLOT，包装应直接调 bot._equip，让 mineflayer 报 invalid destination
        await bot.equip(x, "pocket"); // makeBot 的 _equip 会写 slots[undefined]（无害）
        assert.strictEqual(state.equipCalls, 1, "⑤ 未知 dest 应走原 _equip 一次");
        console.log("✓ ⑤ 未知目标名 → 交给原 equip、不在此吞错");
    }

    console.log("\n全部 equip 护甲校验契约测试通过 ✓");
}

main().catch((e) => { console.error(e); process.exit(1); });
