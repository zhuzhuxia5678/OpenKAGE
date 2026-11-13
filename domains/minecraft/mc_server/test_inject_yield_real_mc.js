/**
 * injectYield 真实 MC smoke —— 验证「顶层 await 不再让注入静默失效」在真服务器上成立。
 *
 *   bash start_servers.sh --mc-only        # 先起裸 MC server @ :25567
 *   node test_inject_yield_real_mc.js      # 再跑本脚本，mineflayer 直连
 *
 * ## 验什么
 *
 * 修复前（injectYield acorn.parse 漏 allowAwaitOutsideFunction）：LLM 代码顶层 `await` 是
 * 常态（eval 包进 async 函数），script 模式下顶层 await 非法 → parse 抛错 → catch 原样返回
 * → while 死循环注入 100% 失效 → 锁死事件循环 → keepalive 饿死 → MC server Timed out 踢人。
 * （bob step187 三次自愈 ReadTimeout 的根因。）
 *
 * 修复后：注入生效，while 体每轮 `await bot.waitForTicks(1)` 让出事件循环 → keepalive 持续
 * 被处理 → bot 不被踢、循环正常退出。
 *
 * ## 对照设计
 *
 * 同一段「顶层 await + while 死循环」代码，分别用修复前/后的注入器处理，再 eval 执行：
 *
 *  ① 对照组（修复前注入器）：顶层 await → parse 抛错 → 原样返回 → while 体无注入 → 纯同步
 *     死循环锁死事件循环。期间 server 发来的 update_time 包（事件循环 I/O 阶段处理）堆积不
 *     处理 → 探针停 fire。纯同步死循环会连 setTimeout 兜底都锁死，故循环体用「同步计算 + 守卫
 *     退出」（有限但足够长，~2s），复刻「事件循环被锁期间 I/O 停滞」的本质。
 *
 *  ② 实验组（修复后注入器）：while 体注入 `await bot.waitForTicks(1)` → 每轮让出 → 事件循环
 *     持续转 → update_time 探针持续 fire、bot 不被踢、循环守卫退出正常完成。
 *
 * ## 判据
 *
 *  ① 对照组：注入器原样返回（未注入）；循环期间探针停 fire（事件循环被锁）
 *  ② 实验组：注入器注入了 while 体；循环期间探针持续 fire；bot 未被踢；循环正常完成
 *  被踢信号：bot 'kicked'/'end' + server 日志 "Timed out" = keepalive 饿死铁证
 */
const mineflayer = require("mineflayer");
const acorn = require("acorn");
const MagicString = require("magic-string");

const HOST = "localhost";
const PORT = 25567;
const USER = "bot"; // ops.json op 名，online-mode=false 按名匹配
const log = (...a) => console.log(`[yield-real ${USER}]`, ...a);

// 注入器工厂：allowAwait 控制是否复刻修复前（漏选项）的行为
function makeInjector(allowAwait) {
    return function (code) {
        let ast;
        try {
            ast = acorn.parse(code, {
                ecmaVersion: 2022, sourceType: "script",
                allowReturnOutsideFunction: true,
                ...(allowAwait ? { allowAwaitOutsideFunction: true } : {}),
            });
        } catch (e) { return code; } // parse 抛错 → 原样返回（修复前对顶层 await 代码走这条）
        const ms = new MagicString(code);
        const yieldStmt = "await bot.waitForTicks(1);";
        const injected = new Set();
        let changed = false;
        function walk(node) {
            if (!node || typeof node.type !== "string") return;
            const isLoop = node.type === "WhileStatement" || node.type === "DoWhileStatement" ||
                node.type === "ForStatement" || node.type === "ForInStatement" || node.type === "ForOfStatement";
            if (isLoop && node.body && node.body.type === "BlockStatement") {
                const pos = node.body.start + 1;
                if (!injected.has(pos)) { ms.appendLeft(pos, yieldStmt); injected.add(pos); changed = true; }
            }
            for (const key in node) {
                if (key === "type" || key === "start" || key === "end") continue;
                const child = node[key];
                if (Array.isArray(child)) for (const c of child) walk(c);
                else if (child && typeof child.type === "string") walk(child);
            }
        }
        walk(ast);
        return changed ? ms.toString() : code;
    };
}
const injectFixed = makeInjector(true);   // 修复后
const injectBroken = makeInjector(false); // 修复前（漏 allowAwaitOutsideFunction）

// keepalive 探针：server 每秒发若干 update_time 包，mineflayer 在事件循环 I/O 阶段处理。
// 事件循环被纯同步循环锁死时，这些包堆积不处理 → 探针停 fire。
let timePkts = 0;
const wait = (ms) => new Promise((r) => setTimeout(r, ms));

let kicked = false;
const bot = mineflayer.createBot({ host: HOST, port: PORT, username: USER, version: "1.21.11" });
bot.loadPlugin(require("mineflayer-pathfinder").pathfinder);
bot._client.on("update_time", () => { timePkts++; });
bot.on("kicked", (r) => { kicked = true; log("★★★ 被 server 踢（keepalive 饿死铁证）：", String(r).slice(0, 200)); });
bot.on("end", () => log("连接结束"));
bot.on("error", (e) => { log("错误：", e.message); });

// 复刻 index.js evaluateCode 的 eval 包裹：code 在 async 函数内 → 顶层 await 合法
function runCode(code) {
    return eval("(async () => { await (async () => { " + code + "\n})(); })()");
}

bot.once("spawn", async () => {
    log("spawn，version", bot.version, "位置", bot.entity.position.floored());
    await wait(2500); // 让首批 update_time 包到达，建立基线

    let pass = 0, fail = 0;
    const check = (name, cond, detail) => {
        if (cond) { pass++; log(`  ✓ ${name}`); }
        else { fail++; log(`  ✗ ${name}  ${detail || ""}`); }
    };

    // ============ ① 对照组：修复前注入器，顶层 await 致 parse 抛错、while 不注入 ============
    log("\n========== ① 对照组（修复前注入器）：顶层 await 让注入静默失效，while 锁死事件循环 ==========");
    {
        // 顶层 await（常态）+ while 同步死循环。修复前注入器 parse 抛错 → 原样返回 → while 无注入。
        // 循环体用「同步计算 + 守卫退出」：有限但足够长（~2s）复刻锁死期，又不至于卡死进程。
        const rawCode = `await bot.waitForTicks(1); // 顶层 await（step187 第一行 await goto 形态）
let n = 0;
let guard = 0;
while (guard < 4000000) {
  n += guard * 3;
  guard++;
}
console.log('sync loop done', n);`;
        const broken = injectBroken(rawCode);
        const injectedOk = broken.includes("await bot.waitForTicks(1);") &&
                           /while \(guard < 4000000\) \{await bot\.waitForTicks/.test(broken);
        log(`  修复前注入器是否注入了 while 体？ ${injectedOk ? "是(异常)" : "否(预期：顶层await致parse抛错原样返回)"}`);

        const pktsBefore = timePkts;
        const t0 = Date.now();
        await runCode(broken); // 同步循环霸占事件循环期间，update_time 包堆积不处理
        const elapsed = Date.now() - t0;
        const pktsDuring = timePkts - pktsBefore;
        await wait(1500); // 等积压包 flush

        log(`  同步循环耗时 ${elapsed}ms，期间 update_time 包被处理 ${pktsDuring} 个`);
        check("对照组：修复前注入器对顶层await代码未注入（静默失效）", !injectedOk,
              `injectedOk=${injectedOk}（应false）`);
        check("对照组：事件循环被锁期间 keepalive 探针停 fire", pktsDuring <= 1,
              `pktsDuring=${pktsDuring}（应≤1，I/O 被锁包堆积）`);
    }

    await wait(2000);
    log(`  恢复期后 update_time 总包数=${timePkts}（应已恢复增长）`);

    // ============ ② 实验组：修复后注入器，while 体注入、每轮让出 ============
    log("\n========== ② 实验组（修复后注入器）：while 死循环每轮让出，keepalive 持续 ==========");
    {
        // 顶层 await + while 死循环（守卫退出）。修复后注入器 → while 体注入 waitForTicks(1)。
        const rawCode = `await bot.waitForTicks(2); // 顶层 await（step187 第一行 await goto 形态）
let guard = 0;
let n = 0;
while (guard < 300) {
  n += guard;
  guard++; // 每轮递增到 300 退出（不靠 continue 跳过，确保能完成）
}`;
        const fixed = injectFixed(rawCode);
        const injectedCount = (fixed.match(/await bot\.waitForTicks\(1\);/g) || []).length;
        const whileLine = fixed.slice(fixed.indexOf("while (guard < 300)"), fixed.indexOf("while (guard < 300)") + 80).split("\n")[0];
        log(`  修复后注入器注入次数=${injectedCount}（应=1，仅 while 体）`);
        log(`  注入后 while 体首行：${whileLine}`);

        const pktsBefore = timePkts;
        const t0 = Date.now();
        let completed = false;
        try { await runCode(fixed); completed = true; }
        catch (e) { log(`  runCode 抛错：${e.message}`); }
        const elapsed = Date.now() - t0;
        const pktsDuring = timePkts - pktsBefore;

        log(`  while 死循环 ${elapsed}ms 完成=${completed}，期间 update_time 包=${pktsDuring}`);
        check("实验组：修复后注入器注入了 while 体（注入次数=1）", injectedCount === 1,
              `injectedCount=${injectedCount}（应1）`);
        check("实验组：注入点在循环体首（while{await waitForTicks）",
              /while \(guard < 300\) \{await bot\.waitForTicks\(1\);/.test(fixed),
              "注入点不在循环体首");
        check("实验组：循环正常完成（非锁死）", completed, "未完成");
        check("实验组：bot 未被踢（keepalive 持续处理）", !kicked && bot.entity,
              kicked ? "bot 被踢（keepalive 饿死）" : "bot 已掉线");
        check("实验组：事件循环持续转（探针持续 fire，不中断）", pktsDuring >= 3,
              `pktsDuring=${pktsDuring}（应≥3，每轮让出→I/O 持续处理）`);
    }

    log(`\n========== 结论 ==========`);
    log(`总 update_time 包数=${timePkts}，被踢=${kicked}`);
    log(`pass=${pass} fail=${fail}`);
    log(fail === 0 ? "★ 真实 MC 验证通过：顶层 await 注入修复生效，while 死循环不再锁死事件循环、keepalive 不饿死"
                   : "✗ 有用例失败，见上");
    finish(fail === 0 ? 0 : 1);
});

function finish(code) {
    setTimeout(() => { try { bot.quit(); } catch (e) {} process.exit(code); }, 500);
}
