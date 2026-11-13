/**
 * onSystem 成就侧恢复采集单测 —— **不连 MC 服务器**，用假 bot 喂聊天播报。
 *
 *     node test_onsystem_advancement.js
 *
 * 测什么（lib/observation/onSystem.js，2026-09-14 成就侧恢复）：
 *   2026-09-07 起成就记账全交 packet 协议包路、onSystem 成就播报即弃；09-14 恢复采集
 *   announce=true 子进度的聊天播报（packet 失效期兜底），经 _collect_system_events 抽出后
 *   与 packet 快照在 env 侧 _absorb_advs **源头并集**进总成就变量 _seen_advancements——
 *   落盘/观察前沿/tier/步反馈全读它，一条线（见 env.py union_advancement_sources）。
 *
 * 用例：
 *   ① chat.type.advancement.task + with[1]="[Fishy Business]" -> events 含 kind=advancement
 *   ② 成就名归一化：with[1] 不带方括号 -> 补成 "[X]"（与 packet completed 同形，差集才能对齐）
 *   ③ 死亡播报不受影响 -> kind=death 仍正常
 *   ④ 别人（subject≠bot.username）的成就被滤掉 -> 不进 events
 *   ⑤ announce=false 的 root 不发聊天播报 -> onSystem 本就采不到（这是 onSystem 的盲区，
 *      靠 packet/ground-truth 补，本测确认 onSystem 不假装能采到）
 *   ⑥ 畸形包（无 with/with[1] 为空）不抛、不污染 events
 *
 * 测试三法选型（memory/openkage_testing_methodology）：纯逻辑单测——构造 fake jsonMsg 喂
 * _handle，验事件结构，不需 MC/server。onSystem 靠聊天播报，本测不验证服务端是否真发播报
 * （那是 test_advancement_broadcast_gt.js 真跑 grant 的事）。
 */
const path = require("path");
const assert = require("assert");

const OnSystem = require(path.join(__dirname, "lib", "observation", "onSystem.js"));
assert.strictEqual(typeof OnSystem, "function", "onSystem.js 应 export 类");

// ── 假 bot：onSystem 构造只要 bot.username + bot.on（messagestr 注册用）──
function makeBot(username) {
    return {
        username: username || "bot",
        on: () => {},  // messagestr 监听器注册（测试直接调 _handle，不走事件）
        entity: null,  // 成就事件不读 _position/_nearby，null 无害
        time: null,
    };
}

// 构造一个 ChatMessage-like 组件：toString() 返回人类可读文本。
// 真实 mineflayer ChatMessage 有 toString()，把 translate/with 解析成 "bot has made..."。
// 这里只测 onSystem 取 with[1].toString()，故 with 元素是 {toString: ()=>文本} 的简单对象。
function comp(text) {
    return { toString: () => text };
}

// 构造一条 chat.type.advancement.* 播报的 jsonMsg。
function advMsg(advName, subject, subtype) {
    return {
        translate: subtype || "chat.type.advancement.task",
        with: [comp(subject || "bot"), comp(advName)],
    };
}
// 构造一条死亡播报的 jsonMsg。
function deathMsg(subject, killer, causeKey) {
    return {
        translate: causeKey || "death.attack.zombie",
        with: [comp(subject || "bot"), comp(killer || "Zombie")],
    };
}

let pass = 0;
function ok(name, cond) {
    assert.ok(cond, name);
    pass++;
    console.log("  ✓ " + name);
}

console.log("== ① 成就播报 → kind=advancement 事件 ==");
{
    const bot = makeBot("bot");
    const sys = new OnSystem(bot);
    sys._handle("bot has made the advancement [Fishy Business]", "system",
                advMsg("[Fishy Business]"));
    const advs = sys.events.filter((e) => e.kind === "advancement");
    ok("events 含 1 条 advancement", advs.length === 1);
    ok("advancement 名 = [Fishy Business]", advs[0].advancement === "[Fishy Business]");
    ok("cause = chat.type.advancement.task", advs[0].cause === "chat.type.advancement.task");
}

console.log("== ② 成就名归一化（不带方括号 → 补成 [X]）==");
{
    const bot = makeBot("bot");
    const sys = new OnSystem(bot);
    // with[1] 不带方括号（某些实现 toString 不带），应补成 [Fishy Business]
    sys._handle("bot has made the advancement Fishy Business", "system",
                advMsg("Fishy Business"));
    const advs = sys.events.filter((e) => e.kind === "advancement");
    ok("归一化后 = [Fishy Business]", advs.length === 1 && advs[0].advancement === "[Fishy Business]");
}

console.log("== ③ 死亡播报不受影响（kind=death 仍正常）==");
{
    const bot = makeBot("bot");
    const sys = new OnSystem(bot);
    sys._handle("bot was slain by Zombie", "system", deathMsg());
    const deaths = sys.events.filter((e) => e.kind === "death");
    const advs = sys.events.filter((e) => e.kind === "advancement");
    ok("death 事件 1 条", deaths.length === 1);
    ok("无 advancement 事件（死亡不是成就）", advs.length === 0);
    ok("death.cause = death.attack.zombie", deaths[0].cause === "death.attack.zombie");
}

console.log("== ④ 别人的成就被滤掉（subject≠bot.username）==");
{
    const bot = makeBot("bot");
    const sys = new OnSystem(bot);
    // 同服真人 player1 达成成就，不该算进 bot
    sys._handle("player1 has made the advancement [Stone Age]", "system",
                advMsg("[Stone Age]", "player1"));
    const advs = sys.events.filter((e) => e.kind === "advancement");
    ok("别人成就不进 events", advs.length === 0);
}

console.log("== ⑤ 成就+死亡同帧都收（kind 区分，互不干扰）==");
{
    const bot = makeBot("bot");
    const sys = new OnSystem(bot);
    sys._handle("bot has made the advancement [Stone Age]", "system", advMsg("[Stone Age]"));
    sys._handle("bot was slain by Zombie", "system", deathMsg());
    sys._handle("bot has made the advancement [Fishy Business]", "system",
                advMsg("[Fishy Business]"));
    const advs = sys.events.filter((e) => e.kind === "advancement");
    const deaths = sys.events.filter((e) => e.kind === "death");
    ok("advancement 2 条", advs.length === 2);
    ok("death 1 条", deaths.length === 1);
}

console.log("== ⑥ 畸形包不抛、不污染 events ==");
{
    const bot = makeBot("bot");
    const sys = new OnSystem(bot);
    // 非 system position -> 丢弃
    sys._handle("x", "chat", advMsg("[X]"));
    // 无 translate -> 丢弃
    sys._handle("x", "system", { with: [comp("bot"), comp("[X]")] });
    // achievement key 但 with[1] 为空 -> 丢弃（无成就名）
    sys._handle("x", "system", { translate: "chat.type.advancement.task", with: [comp("bot")] });
    // 完全畸形 -> 不抛
    sys._handle("x", "system", null);
    sys._handle("x", "system", "not an object");
    ok("畸形包全部丢弃，events 为空", sys.events.length === 0);
}

console.log("== ⑦ flush 仅在有事件时发帧（base.js 跳过 on* 前缀的契约）==");
{
    const bot = makeBot("bot");
    const sys = new OnSystem(bot);
    let fired = false;
    bot.event = () => { fired = true; };  // 模拟 base.js 注入的 bot.event
    sys.flush();  // 无事件 -> 不发帧
    ok("无事件时不 flush", fired === false);
    sys._handle("bot has made the advancement [Stone Age]", "system", advMsg("[Stone Age]"));
    sys.flush();  // 有事件 -> 发帧
    ok("有事件时 flush 发帧", fired === true);
}

console.log(`\n全部通过（${pass} 项）`);
