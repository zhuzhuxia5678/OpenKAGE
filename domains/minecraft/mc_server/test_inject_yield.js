/**
 * injectYieldIntoLoops 单元测试 —— 纯逻辑，不连 MC、不启动 express。
 *
 *     node test_inject_yield.js
 *
 * 钉住 2026-08-26 的修复：LLM 写 `while(true){bot.tossStack(x)}`（异步 API 不 await）= 纯同步
 * 死循环，锁死事件循环 → keepalive 饿死 → MC 服务器 Timed out 断线（dave 0826 跑 5 次的根因）。
 * 修法是 eval 前在每个循环体注入 `await bot.waitForTicks(1);`，每轮强制让出事件循环。
 *
 * 核心断言：
 *  ① 各循环形态（while/for/for-of/for-in/do-while）的 block 体都被注入
 *  ② 嵌套循环每层都注入、且内层不被重复注入（去重）
 *  ③ 空体/非 block 体不注入、不崩
 *  ④ 语法错误原样返回、不崩
 *  ⑤ **注入前后行数不变**（handleError 靠 programs.split("\n").length 反算行号，改行数会错位）
 *  ⑥ 无循环的代码原样返回
 *  ⑦ 注入后的代码语法仍合法（能被 acorn 再解析）
 */
const assert = require("assert");
const acorn = require("acorn");
const { injectYieldIntoLoops } = require("./lib/injectYield");

const YIELD = "await bot.waitForTicks(1);";
let passed = 0;
let failed = 0;

function check(name, input, predicate) {
    const out = injectYieldIntoLoops(input);
    try {
        predicate(out, input);
        console.log(`  ✓ ${name}`);
        passed++;
    } catch (e) {
        console.log(`  ✗ ${name}`);
        console.log(`      IN : ${input}`);
        console.log(`      OUT: ${out}`);
        console.log(`      ERR: ${e.message}`);
        failed++;
    }
}

function linesEq(out, input) {
    assert.strictEqual(
        out.split("\n").length, input.split("\n").length,
        `行数变了：${input.split("\n").length} → ${out.split("\n").length}（会破坏 handleError 行号映射）`
    );
}

function reparses(code) {
    try {
        // 注入的 await 在实际运行时被包在 (async()=>{...})() 里（见 index.js eval 处），合法。
        // 这里解析用 allowAwaitOutsideFunction 模拟那个上下文，验证注入本身不破坏语法结构。
        acorn.parse(code, { ecmaVersion: 2022, sourceType: "script",
            allowReturnOutsideFunction: true, allowAwaitOutsideFunction: true });
        return true;
    } catch { return false; }
}

console.log("== ① 各循环形态注入 ==");

check("while(true) block", "while (true) { bot.tossStack(item); }", (out) => {
    assert.ok(out.includes(YIELD), "未注入 yield");
    assert.ok(/while \(true\) \{await bot\.waitForTicks\(1\); bot\.tossStack/.test(out), out);
    linesEq(out, "while (true) { bot.tossStack(item); }");
});

check("while(cond) block", "while (guard < 30) { dig(); }", (out) => {
    assert.ok(out.includes(YIELD));
    linesEq(out, "while (guard < 30) { dig(); }");
});

check("for 经典 block", "for (let i=0;i<3;i++){ dig(i); }", (out) => {
    assert.ok(out.includes(YIELD));
    linesEq(out, "for (let i=0;i<3;i++){ dig(i); }");
});

check("for-of block", "for (const s of spots) { visit(s); }", (out) => {
    assert.ok(out.includes(YIELD));
    linesEq(out, "for (const s of spots) { visit(s); }");
});

check("for-in block", "for (const k in obj) { handle(k); }", (out) => {
    assert.ok(out.includes(YIELD));
    linesEq(out, "for (const k in obj) { handle(k); }");
});

check("do-while block", "do { step(); } while (go);", (out) => {
    assert.ok(out.includes(YIELD), "do-while 未注入");
    linesEq(out, "do { step(); } while (go);");
});

console.log("== ② 嵌套循环 ==");

check("嵌套 while/for 各层注入且去重", "while(a){ for(const x of arr){ doX(); } }", (out) => {
    const count = (out.match(/await bot\.waitForTicks\(1\);/g) || []).length;
    assert.strictEqual(count, 2, `嵌套两层应注入 2 次，实际 ${count}（去重失效？）: ${out}`);
    linesEq(out, "while(a){ for(const x of arr){ doX(); } }");
});

check("三层嵌套", "while(a){ while(b){ while(c){ f(); } } }", (out) => {
    const count = (out.match(/await bot\.waitForTicks\(1\);/g) || []).length;
    assert.strictEqual(count, 3, `三层应注入 3 次，实际 ${count}: ${out}`);
});

console.log("== ③ 空体/非 block 体不注入不崩 ==");

check("空体 while(cond);", "while (cond);", (out) => {
    assert.strictEqual(out, "while (cond);", "空体不应被改写");
});

check("非 block 体 for(...) f();", "for (const s of spots) console.log(s);", (out) => {
    assert.strictEqual(out, "for (const s of spots) console.log(s);", "非 block 体不应被改写");
});

check("单行 while body", "while (x) doThing();", (out) => {
    assert.strictEqual(out, "while (x) doThing();");
});

console.log("== ④ 语法错误原样返回 ==");

check("语法错误", "while (true) { ", (out) => {
    assert.strictEqual(out, "while (true) { ", "语法错误应原样返回");
});

check("乱码", "this is not js {{{", (out) => {
    assert.strictEqual(out, "this is not js {{{");
});

console.log("== ⑤ 行数不变（含多行代码）==");

const multi = `const a = 1;
while (true) {
  bot.tossStack(item);
}
console.log('done');`;
check("多行代码行数不变", multi, (out) => {
    linesEq(out, multi);
    assert.ok(out.includes(YIELD));
    // 注入位置是 { 之后（第 2 行末），tossStack 仍在第 3 行 —— 行号映射不变。
    // 报错若指到 tossStack 那行，handleError 用原 code 取行原文仍正确（注入只在循环体首，不改 tossStack 行）。
    const lines = out.split("\n");
    assert.ok(lines[1].includes(YIELD), `第2行(while行)应含注入: ${lines[1]}`);
    assert.strictEqual(lines[2].trim(), "bot.tossStack(item);", `第3行 tossStack 应原样: ${lines[2]}`);
});

console.log("== ⑥ 无循环原样返回 ==");

check("无循环", "const x = 1; console.log(x);", (out) => {
    assert.strictEqual(out, "const x = 1; console.log(x);");
});

check("只有 if", "if (a) { b(); }", (out) => {
    assert.strictEqual(out, "if (a) { b(); }", "if 不是循环，不应注入");
});

console.log("== ⑦ 注入后仍可被 acorn 解析 ==");

const realish = `// 清仓
const tossBulk = ['cobblestone','dirt'];
for (const name of tossBulk) {
  while (true) {
    const item = bot.inventory.items().find(i => i.name === name);
    if (!item) break;
    bot.tossStack(item);
  }
}
console.log('done');`;
check("真实 dave 清仓代码注入后合法", realish, (out) => {
    assert.ok(reparses(out), "注入后代码语法不合法");
    linesEq(out, realish);
    const count = (out.match(/await bot\.waitForTicks\(1\);/g) || []).length;
    assert.ok(count >= 2, `至少注入 for+while 两处，实际 ${count}`);
});

// 真实场景里 code 会被包进 async 函数，注入的 await 合法性最终由那里保证；
// 这里只验注入本身不破坏语法结构。

console.log("== ⑧ 顶层 await 不再让注入静默失效（bob step187 事故，2026-09-07） ==");
// ★ 钉住这次事故：LLM 代码在 eval 时被包进 `await (async () => { <code> })()`，
//   所以顶层 `await bot.xxx(...)` 是常态。injectYield 的 acorn.parse 必须开
//   allowAwaitOutsideFunction，否则顶层 await 触发 `Unexpected token` → catch 原样返回
//   → while 死循环注入 100% 失效 → keepalive 饿死 → ReadTimeout。改这个 option 前，
//   以下两个用例 changed 都是 false（注入没发生），是 09-07 bob 三次自愈的根因。

// 顶层 await + while 死循环（异步 API 不 await）—— 修复前会原样返回、不注入
check("顶层 await + while(true) 异步API不await", `await bot.pathfinder.goto(new GoalBlock(1,2,3));
while (true) { bot.tossStack(item); }`, (out, input) => {
    assert.ok(out.includes(YIELD), "顶层 await 导致注入静默跳过（应注入未注入）");
    linesEq(out, input);
    assert.ok(/while \(true\) \{await bot\.waitForTicks\(1\); bot\.tossStack/.test(out), out);
});

// bob step187 真实代码：顶层 await goto + while(cond) 含 continue 的 pillar 死循环
// （continue 在注入点之后，每轮先 await 让出，能拦住纯同步分支）
const step187 = `await bot.pathfinder.goto(new GoalBlock(base.x, base.y, base.z));
let y = Math.floor(bot.entity.position.y);
const tx = 204, tz = 555;
while (y < 68) {
  const below = bot.blockAt(new Vec3(tx, y-1, tz));
  if (below && below.name!=='air') { y = y-1; continue; }
  try {
    await bot.placeBlockSafe(below, new Vec3(0,1,0));
  } catch(e){}
}`;
check("bob step187 顶层await+while(cond)死循环", step187, (out, input) => {
    assert.ok(out.includes(YIELD), "顶层 await 导致注入静默跳过（应注入未注入）");
    linesEq(out, input);
    const count = (out.match(/await bot\.waitForTicks\(1\);/g) || []).length;
    assert.strictEqual(count, 1, `应只在 while 体注入 1 次，实际 ${count}: ${out}`);
    assert.ok(reparses(out), "注入后语法不合法");
});

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed === 0 ? 0 : 1);
