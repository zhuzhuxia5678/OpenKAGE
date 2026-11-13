/**
 * injectYieldIntoLoops 执行层 smoke —— 验证注入的 await 在执行时**真的让出事件循环**。
 *
 *     node test_inject_yield_exec_smoke.js
 *
 * 单元测试（test_inject_yield.js）只验注入的文本正确。这里更进一步：复刻 index.js 的 eval
 * 包裹方式（把 code 包进 (async()=>{...})() 执行），跑注入后的 `while(true){tossStack}` 死循环
 * 代码，**并行挂一个 setInterval 探针**——若事件循环被锁死（纯同步死循环），探针永不 fire；
 * 注入成功则每轮 await 让出，探针能 fire。
 *
 * 这直接钉住核心论点：注入把「锁死事件循环的纯同步死循环」变成「每轮让出的可中断循环」，
 * keepalive（也是事件循环 I/O）因此能被处理，不再饿死断线。
 *
 * 不连 MC：mock bot.waitForTicks(1) 立即 resolve（模拟 tick 到达），mock tossStack 计数。
 * 设一个步内上限（防万一注入失效跑成真死循环把测试卡死）。
 */
const assert = require("assert");
const { injectYieldIntoLoops } = require("./lib/injectYield");

// 复刻 index.js eval 处的包裹：(async () => { processShim+programs + await (async()=>{ code })() })()
// 这里只包 code（processShim/programs 与让出无关，省略），关键是 code 在 async 函数内 → await 合法。
function runCode(code, bot, stepBudgetMs) {
    return new Promise((resolve, reject) => {
        let resolved = false;
        const finish = (val) => { if (!resolved) { resolved = true; clearTimeout(hardStop); resolve(val); } };
        // 硬上限：注入失效时会真死循环，靠这个兜底让测试能退出（而非挂死）。
        const hardStop = setTimeout(() => finish("__HARDSTOP__"), stepBudgetMs);
        try {
            eval("(async () => { await (async () => { " + code + "\n})(); })()").then(() => finish("__DONE__"));
        } catch (e) {
            finish("__THROW__:" + e.message);
        }
    });
}

async function main() {
    let pass = 0, fail = 0;
    const check = (name, cond, detail) => {
        if (cond) { pass++; console.log(`  ✓ ${name}`); }
        else { fail++; console.log(`  ✗ ${name}  ${detail || ""}`); }
    };

    // ===== ① 注入前：大同步循环（不 await）期间事件循环被锁，探针不 fire =====
    // 用有限大循环（不是 while(true)）——纯同步死循环会连 setTimeout 兜底都锁死、卡死测试进程，
    // 那本身也是论点的一部分（外部救不了），但测试要能退出。故用 guard 有限循环：
    // 未注入时同步跑完 N 轮，期间事件循环被锁、探针不 fire；注入后每轮让出、探针能 fire。
    console.log("① 未注入大同步循环期间探针不 fire（事件循环被锁）");
    {
        let probeFires = 0;
        const probe = setInterval(() => { probeFires++; }, 1);
        let tossCount = 0;
        const bot = { tossStack: () => { tossCount++; } };
        // 50 万轮纯同步循环（不 await），足以让探针在期间一个都 fire 不了
        const rawCode = "for (let i = 0; i < 500000; i++) { bot.tossStack(); }";
        const result = await runCode(rawCode, bot, 5000);
        clearInterval(probe);
        check("未注入 → 同步循环期间探针不 fire", probeFires === 0, `probeFires=${probeFires}（应为0）`);
        check("未注入 → 循环正常跑完", result === "__DONE__", `result=${result}`);
        check("未注入 → tossStack 跑了 50 万次", tossCount === 500000, `tossCount=${tossCount}`);
        console.log(`     (probeFires=${probeFires} —— 事件循环被锁，keepalive 同理处理不了)`);
    }

    // ===== ② 注入后：每轮 await 让出，探针能 fire，循环可被中断 =====
    console.log("② 注入后每轮 await 让出事件循环（探针能 fire）");
    {
        let probeFires = 0;
        const probe = setInterval(() => { probeFires++; }, 5);
        let tossCount = 0;
        let tickCount = 0;
        // mock waitForTicks：用 setImmediate 让出事件循环（模拟真实 waitForTicks 等一个
        // physicsTick 事件 —— 那段等待期间事件循环转到 I/O 阶段处理 keepalive）。
        // ⚠️ 不能用 Promise.resolve()：它的 then 是微任务，当前宏任务结束前清空，不转到 I/O，
        // 验证不了「keepalive 这种 I/O 事件能被处理」的论点。setImmediate 才真让出到 check 阶段。
        let guard = 0;
        const bot = {
            waitForTicks: () => { tickCount++; return new Promise(r => setImmediate(r)); },
            tossStack: () => { tossCount++; },
            inventory: { items: () => [{ name: "cobblestone" }] },
        };
        // LLM 原始代码（带 break 出路，模拟真实：物品丢完退出）
        const rawCode = `let guard = 0;
while (true) {
  const item = bot.inventory.items().find(i => i.name === 'cobblestone');
  if (!item) break;
  bot.tossStack(item);
  if (++guard >= 50) break;  // 防真无限，丢 50 次退出
}`;
        const injected = injectYieldIntoLoops(rawCode);
        const result = await runCode(injected, bot, 2000);
        clearInterval(probe);
        check("注入后 → 探针能 fire（事件循环没锁死）", probeFires > 0, `probeFires=${probeFires}（应>0）`);
        check("注入后 → 循环正常完成（非硬上限）", result === "__DONE__", `result=${result}`);
        check("注入后 → waitForTicks 被调用（每轮让出）", tickCount >= 50, `tickCount=${tickCount}（应≥50）`);
        check("注入后 → tossStack 也跑了 50 次", tossCount >= 50, `tossCount=${tossCount}`);
        console.log(`     (probeFires=${probeFires}, tickCount=${tickCount}, tossCount=${tossCount})`);
    }

    // ===== ③ 注入对正常短 for 循环无副作用（仍正确完成、探针能 fire）=====
    console.log("③ 注入对正常短 for 循环无副作用（仍正确完成）");
    {
        let probeFires = 0;
        const probe = setInterval(() => { probeFires++; }, 1);
        let sum = 0;
        const bot = { waitForTicks: () => new Promise(r => setImmediate(r)) };
        const rawCode = "for (let i = 0; i < 100; i++) { sum += i; }";
        const injected = injectYieldIntoLoops(rawCode);
        // runCode 把 code 包进 async(){}，sum 是闭包变量 —— 用 globalThis 传出来
        globalThis.__sum = 0;
        const wrapped = "for (let i = 0; i < 100; i++) { globalThis.__sum += i; }";
        const injected2 = injectYieldIntoLoops(wrapped);
        const result = await runCode(injected2, bot, 5000);
        clearInterval(probe);
        check("短 for 循环注入后结果正确", globalThis.__sum === 4950, `sum=${globalThis.__sum}（应=4950）`);
        check("短 for 循环注入后正常完成", result === "__DONE__", `result=${result}`);
        // 探针 fire 与否取决于循环轮数是否跨过探针间隔；短循环可能太快不跨 —— 不作硬断言，
        // 核心是「结果正确 + 不锁死完成」，② 已证注入让出有效。
        delete globalThis.__sum;
    }

    console.log(`\n${pass} passed, ${fail} failed`);
    process.exit(fail === 0 ? 0 : 1);
}

main().catch(e => { console.error(e); process.exit(1); });
