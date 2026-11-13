/**
 * killMob 行为契约测试 —— **不连 MC 服务器**，用假 bot + 真实 waitForMobRemoved 跑。
 *
 *     node test_killMob_smoke.js
 *
 * 测什么（killMob.js）：
 *   ① 近战 timeout 没打死 → reject 被 catch → 播报"还活着"+ 原因，**不**裸抛 `Failed to kill`
 *      —— 这是关键回归：旧实现不 catch waitForMobRemoved 的 reject，穿透成步级 `Failed to kill X`，
 *      Agent 看不到"还活着"也无从判断原因（同 mineBlock 那次"超时只报动作超时"病根，见 memory
 *      openkage_primitive_feedback）。prompt 承诺"明说击杀了还是还活着"。
 *   ② 近战手里是镐（非武器）→ "还活着"播报里给"换成剑"的中性建议（不替 Agent 下禁止性决定）
 *   ③ 真的杀了（entityGone 触发）→ 不走 catch，正常返回（waitForMobRemoved 内部已 chat 'Killed'）
 *   ④ 杀了且有掉落物但捡不到 → 播报"Killed ... but could not pick up"，杀仍算成
 *   ⑤ 找不到怪 → 播报"No X nearby"，不抛
 *
 * ⚠️ killMob 依赖同目录 waitForMobRemoved.js 的 waitForMobRemoved/waitForMobShot。
 *    这里把两份源码拼进同一 vm 上下文，走**真实**的 reject 路径（setTimeout 触发 bot.pvp.stop()
 *    → emit stoppedAttacking → onStoppedAttacking reject），而不是 mock 掉它 —— 这样测的是
 *    killMob 的 catch 逻辑对真实 reject 的反应。
 */
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const KILL_SRC = fs.readFileSync(path.join(__dirname, "control_primitives", "killMob.js"), "utf-8");
const WAIT_SRC = fs.readFileSync(path.join(__dirname, "control_primitives", "waitForMobRemoved.js"), "utf-8");

// entity 工厂：带 name/position；position 上有 distanceTo（killMob 的 nearestEntity filter
// 调 entity.position.distanceTo(bot.entity.position)）
function mkEntity(name, pos) {
    const position = {
        x: pos.x, y: pos.y, z: pos.z,
        distanceTo: function (p) {
            return Math.sqrt((this.x - p.x) ** 2 + (this.y - p.y) ** 2 + (this.z - p.z) ** 2);
        },
    };
    return { name, position, isValid: true };
}

// runOnce：调 killMob(bot, mobName, timeout)
async function runOnce(makeBot, mobName, timeout) {
    const bot = makeBot();
    const wrappedSrc =
        "let _killMobFailCount = 0;\n" +
        WAIT_SRC + "\n" +
        KILL_SRC +
        `\n;globalThis.__run = (async () => {` +
        `  try { await killMob(bot, ${JSON.stringify(mobName)}, ${timeout}); }` +
        `  catch(e){ globalThis.__runErr = e; }` +
        `  globalThis.__failCount = _killMobFailCount;` +
        `})();`;
    const ctx = {
        bot, console, setTimeout, clearTimeout, Date, Math, Object, Promise,
        require, globalThis: {},
    };
    vm.createContext(ctx);
    vm.runInContext(wrappedSrc, ctx);
    await ctx.globalThis.__run;
    return { chats: bot.__chats, runErr: ctx.globalThis.__runErr, failCount: ctx.globalThis.__failCount };
}

// mock bot 工厂
//   opts.hand: {name} 主手物品（决定走近战 vs 远程 + 建议文案）
//   opts.entity: 找到的怪实体（null = 找不到）
//   opts.killOutcome: 'never'（超时没打死）/ 'killed'（entityGone 触发）/ 'killedWithDrop'（杀+掉落但捡不到）
function makeBot(opts = {}) {
    const chats = [];
    const listeners = {}; // event -> [fns]
    const bot = {
        __chats: chats,
        openkageTickRate: 1,
        health: 20,
        entity: { position: { x: 0, y: 64, z: 0, distanceTo: () => 0 } },
        inventory: {
            slots: opts.hand ? [opts.hand] : [],
        },
        getEquipmentDestSlot: () => 0,
        nearestEntity: (filter) => (opts.entity && filter(opts.entity) ? opts.entity : null),
        // pvp：attack 是 noop；stop() 触发 stoppedAttacking（waitForMobRemoved 超时后调它）
        pvp: {
            attack: async () => {},
            stop: async () => { bot.__emit("stoppedAttacking"); },
        },
        // hawkEye：远程分支才用，给 noop
        hawkEye: {
            autoAttack: () => {},
            stop: () => {},
        },
        // collectBlock：捡掉落物
        collectBlock: {
            collect: async () => {
                if (opts.collectThrows) throw new Error("unreachable");
            },
            targets: { clear: () => {} },
        },
        pathfinder: { setGoal: () => {} },
        chat: (m) => chats.push(m),
        save: () => {},
        // 事件系统：waitForMobRemoved 监听 entityGone/stoppedAttacking/itemDrop
        on: (evt, fn) => { (listeners[evt] = listeners[evt] || []).push(fn); },
        once: (evt, fn) => { (listeners[evt] = listeners[evt] || []).push(fn); },
        removeListener: (evt, fn) => {
            if (listeners[evt]) listeners[evt] = listeners[evt].filter((f) => f !== fn);
        },
        removeAllListeners: (evt) => { delete listeners[evt]; },
        __emit: (evt, ...args) => {
            (listeners[evt] || []).slice().forEach((fn) => fn(...args));
        },
    };
    bot.__listeners = listeners;

    // 杀局驱动：'killed'/'killedWithDrop' 时，等一小段让 waitForMobRemoved 注册好监听，再 emit entityGone
    if (opts.killOutcome === "killed" || opts.killOutcome === "killedWithDrop") {
        setTimeout(() => {
            if (opts.killOutcome === "killedWithDrop") {
                bot.__emit("itemDrop", { position: opts.entity.position });
            }
            bot.__emit("entityGone", opts.entity);
        }, 50);
    }
    // 'never'：什么都不做，waitForMobRemoved 的 setTimeout 超时后自己调 bot.pvp.stop() → stoppedAttacking → reject
    return bot;
}

async function main() {
    let pass = 0, fail = 0;
    const check = (name, cond, detail) => {
        if (cond) { pass++; console.log(`  ✓ ${name}`); }
        else { fail++; console.log(`  ✗ ${name}  ${detail || ""}`); }
    };

    // ① 近战（空手）超时没打死 → 播报"还活着"，不裸抛 Failed to kill
    console.log("① 近战超时没打死 → 播报'还活着'（不裸抛 Failed to kill）");
    {
        const ent = mkEntity("zombie", { x: 3, y: 64, z: 0 });
        const bot = makeBot({ hand: null, entity: ent, killOutcome: "never" });
        // timeout=60 刻：waitForMobRemoved 拿 60刻→3000ms(1x) 的 setTimeout，超时后 stop→reject
        const r = await runOnce(() => bot, "zombie", 30);
        const joined = r.chats.join(" | ");
        check("播报含'还活着'", joined.includes("还活着"), JSON.stringify(r.chats));
        check("不裸抛 Failed to kill", !r.runErr || !/Failed to kill/.test(r.runErr.message), r.runErr && r.runErr.message);
        check("提到空手原因", joined.includes("空手"), JSON.stringify(r.chats));
    }

    // ② 近战手里是镐 → "还活着"播报给"换成剑"建议（中性，非禁止）
    console.log("② 拿镐砍没打死 → 播报'还活着'+'换成剑'建议");
    {
        const ent = mkEntity("zombie", { x: 3, y: 64, z: 0 });
        const bot = makeBot({ hand: { name: "stone_pickaxe" }, entity: ent, killOutcome: "never" });
        const r = await runOnce(() => bot, "zombie", 30);
        const joined = r.chats.join(" | ");
        check("播报含'还活着'", joined.includes("还活着"), JSON.stringify(r.chats));
        check("提到换成剑", joined.includes("剑"), JSON.stringify(r.chats));
        check("不写禁止性'别用 killMob'", !joined.includes("别用 killMob"), JSON.stringify(r.chats));
        check("不裸抛", !r.runErr || !/Failed to kill/.test(r.runErr.message), r.runErr && r.runErr.message);
    }

    // ③ 真的杀了（entityGone）→ 不走 catch，正常返回；waitForMobRemoved 内部 chat 'Killed'
    console.log("③ 真杀了 → 正常返回，chat 含 'Killed'");
    {
        const ent = mkEntity("zombie", { x: 3, y: 64, z: 0 });
        const bot = makeBot({ hand: { name: "iron_sword" }, entity: ent, killOutcome: "killed" });
        const r = await runOnce(() => bot, "zombie", 3000);
        const joined = r.chats.join(" | ");
        check("chat 含 Killed", /Killed/i.test(joined), JSON.stringify(r.chats));
        check("不说'还活着'", !joined.includes("还活着"), JSON.stringify(r.chats));
        check("未抛错", !r.runErr, r.runErr && r.runErr.message);
    }

    // ④ 杀了有掉落物但捡不到 → 播报"Killed ... but could not pick up"，杀仍算成
    console.log("④ 杀了但掉落物捡不到 → 播报'could not pick up'，未抛错");
    {
        const ent = mkEntity("zombie", { x: 3, y: 64, z: 0 });
        const bot = makeBot({
            hand: { name: "iron_sword" },
            entity: ent,
            killOutcome: "killedWithDrop",
            collectThrows: true,
        });
        const r = await runOnce(() => bot, "zombie", 3000);
        const joined = r.chats.join(" | ");
        check("chat 含 'could not pick up'", joined.includes("could not pick up"), JSON.stringify(r.chats));
        check("未抛错", !r.runErr, r.runErr && r.runErr.message);
    }

    // ⑤ 找不到怪 → 播报 No X nearby，不抛（<10 次）
    console.log("⑤ 找不到怪 → 播报'No X nearby'，不抛错");
    {
        const bot = makeBot({ hand: { name: "iron_sword" }, entity: null, killOutcome: "never" });
        const r = await runOnce(() => bot, "zombie", 30);
        const joined = r.chats.join(" | ");
        check("播报含'No zombie nearby'", joined.includes("No zombie nearby"), JSON.stringify(r.chats));
        check("failCount=1", r.failCount === 1, `got ${r.failCount}`);
        check("未抛错", !r.runErr, r.runErr && r.runErr.message);
    }

    console.log(`\n${pass} passed, ${fail} failed`);
    process.exit(fail === 0 ? 0 : 1);
}

main().catch((e) => {
    console.error("smoke 崩了:", e);
    process.exit(2);
});
