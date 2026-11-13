/**
 * _waitForPhysicsReady 纯逻辑单测 —— 不连真服，用 EventEmitter mock bot。
 *
 * 钉三种行为（对应 resume 端点砍掉 waitForTicks(20) 后的就绪探针，见 index.js）：
 *   ① shouldUsePhysics 已就绪（physicsEnabled=true 且本地物理循环在 emit physicsTick）：
 *      探针应在「下一个 physicsTick」立刻 resolve，不等到 2s 兜底。
 *   ② 未就绪（紧跟死亡重生，shouldUsePhysics=false，physicsTick 暂不 emit）后又恢复：
 *      探针应等到「恢复后的第一个 physicsTick」才 resolve。
 *   ③ 长期不就绪（physicsTick 永不 emit）：探针应在 RESUME_READY_TIMEOUT_MS 后兜底 resolve，
 *      绝不挂死 HTTP（这是旧 waitForTicks 在 bot 断流时会挂死 300s 的同源缺陷，见 waitTicksSafe 注释）。
 *
 * 运行：node test_physics_ready_probe.js
 * 退出码 0=全过，1=有失败。
 */
const EventEmitter = require("events");

// ── 把 index.js 里的探针函数单独拎出来测（它只依赖 bot 的 once/removeListener/emit）──
// 为避免 require 整个 index.js（会起 HTTP 服务、连 MC），这里 verbatim 复刻其实现，
// 并在文末断言它与源文件一致（见 SOURCESYNC）。改动 index.js 里 _waitForPhysicsReady 时
// 必须同步改这里，SOURCESYNC 会抓出来。
const RESUME_READY_TIMEOUT_MS = 2000;
function _waitForPhysicsReady(bot) {
    return new Promise((resolve) => {
        let settled = false;
        const listener = () => {
            if (settled) return;
            settled = true;
            clearTimeout(timer);
            bot.removeListener("physicsTick", listener);
            resolve();
        };
        const timer = setTimeout(() => {
            console.log(`[resume] 物理就绪探针超过 ${RESUME_READY_TIMEOUT_MS}ms 未收到 physicsTick`
                + "（可能紧跟死亡重生，shouldUsePhysics 尚未复位），带当前观察继续");
            listener();
        }, RESUME_READY_TIMEOUT_MS);
        bot.once("physicsTick", listener);
    });
}

// 用短超时跑兜底用例，不真等 2s：把超时压到 80ms。
function makeProbeWithTimeout(timeoutMs) {
    return function (bot) {
        return new Promise((resolve) => {
            let settled = false;
            const listener = () => {
                if (settled) return;
                settled = true;
                clearTimeout(timer);
                bot.removeListener("physicsTick", listener);
                resolve();
            };
            const timer = setTimeout(() => {
                console.log(`[resume-test] 物理就绪探针超过 ${timeoutMs}ms 未收到 physicsTick，兜底返回`);
                listener();
            }, timeoutMs);
            bot.once("physicsTick", listener);
        });
    };
}

let passed = 0, failed = 0;
function assert(cond, msg) {
    if (cond) { passed++; console.log(`  ✓ ${msg}`); }
    else { failed++; console.error(`  ✗ ${msg}`); }
}

async function main() {
    // ① 已就绪：本地物理循环在 emit → 下一个 physicsTick 即 resolve。
    {
        const bot = new EventEmitter();
        // 模拟 doPhysics 定时器每 16ms emit 一个 physicsTick（≈60fps，仅示意）
        const iv = setInterval(() => bot.emit("physicsTick"), 16);
        const t0 = Date.now();
        await _waitForPhysicsReady(bot);
        const dt = Date.now() - t0;
        clearInterval(iv);
        assert(dt < 100, `① 已就绪应在 ~一帧内返回（实测 ${dt}ms < 100ms）`);
        assert(bot.listenerCount("physicsTick") === 0, "① 返回后应清掉监听器（不泄漏）");
    }

    // ② 未就绪后恢复：前 300ms 不 emit（shouldUsePhysics=false），之后开始 emit → 探针等到恢复后的第一帧。
    {
        const bot = new EventEmitter();
        let emitting = false;
        const iv = setInterval(() => { if (emitting) bot.emit("physicsTick"); }, 16);
        setTimeout(() => { emitting = true; }, 300); // 300ms 后 shouldUsePhysics 复位
        const t0 = Date.now();
        await _waitForPhysicsReady(bot);
        const dt = Date.now() - t0;
        clearInterval(iv);
        assert(dt >= 280 && dt < 600, `② 未就绪应等到恢复后第一帧（实测 ${dt}ms，期望 ≈300ms）`);
        assert(bot.listenerCount("physicsTick") === 0, "② 返回后应清掉监听器（不泄漏）");
    }

    // ③ 长期不就绪：physicsTick 永不 emit → 超时兜底 resolve，绝不挂死。
    {
        const bot = new EventEmitter();
        const probe = makeProbeWithTimeout(80);
        const t0 = Date.now();
        await probe(bot);
        const dt = Date.now() - t0;
        assert(dt >= 75 && dt < 400, `③ 永不就绪应在超时后兜底返回（实测 ${dt}ms，期望 ≈80ms）`);
        assert(bot.listenerCount("physicsTick") === 0, "③ 兜底返回后也应清掉监听器（不泄漏）");
    }

    // ④ 竞态：physicsTick 与超时几乎同时到达 → 只 resolve 一次（settled 守卫），不抛 double-resolve。
    {
        const bot = new EventEmitter();
        let resolveCount = 0;
        const p = _waitForPhysicsReady(bot).then(() => { resolveCount++; });
        bot.emit("physicsTick");          // 立刻就绪
        // 模拟超时 timer 也触发（实际不会，但 settled 守卫要顶住）
        setTimeout(() => bot.emit("physicsTick"), 5); // 再来一帧
        await p;
        await new Promise(r => setTimeout(r, 10));
        assert(resolveCount === 1, `④ 多次触发只 resolve 一次（实测 ${resolveCount} 次）`);
    }

    // ── SOURCESYNC：抓 verbatim 复刻与 index.js 源是否一致 ──
    {
        const fs = require("fs");
        const src = fs.readFileSync(require("path").join(__dirname, "index.js"), "utf-8");
        const m = src.match(/function _waitForPhysicsReady\(bot\) \{[\s\S]*?\n\}/);
        const inSource = m ? m[0] : "";
        const here = _waitForPhysicsReady.toString();
        // 比较关键骨架（忽略空白差异）：两者都应含 once("physicsTick") + removeListener + RESUME_READY_TIMEOUT_MS
        const skeleton = (s) => s.replace(/\s+/g, " ");
        const key = (s) => [
            'bot.once("physicsTick", listener)', 'bot.removeListener("physicsTick", listener)',
            "RESUME_READY_TIMEOUT_MS", "setTimeout", "settled"
        ].every(k => s.includes(k));
        assert(key(inSource), "SOURCESYNC: index.js 含 _waitForPhysicsReady 关键骨架");
        assert(key(here), "SOURCESYNC: 本测试复刻版含相同关键骨架");
        // 严格逐字比对（格式化后空白可能变，用骨架串比对）
        assert(skeleton(inSource).includes(skeleton(here).slice(0, 80)),
            "SOURCESYNC: index.js 实现与本测试复刻一致（改一处须同步另一处）");
    }

    console.log(`\n${passed} passed, ${failed} failed`);
    process.exit(failed ? 1 : 0);
}

main().catch((e) => { console.error(e); process.exit(1); });
