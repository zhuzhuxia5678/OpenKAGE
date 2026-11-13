/**
 * makeTickTimeoutPromise 墙钟兜底测试 —— **不连 MC 服务器**。
 *
 *     node test_makeTickTimeoutPromise_smoke.js
 *
 * 2026-08-25 修：makeTickTimeoutPromise 原本只靠累加 bot.time.timeOfDay 判超时。bot 一断流，
 * timeOfDay 冻结 → 累加器永不增长 → __TIMEOUT__ 永不触发 → /step 挂满 300s。修法是加一条墙钟
 * setTimeout 兜底。这里从 index.js 里**抽出真正的函数源码**在 vm 里跑（不是镜像副本），钉住：
 *
 *  ① timeOfDay 冻结（断流）+ codePromise 永不落定 → 墙钟到点仍 resolve("__TIMEOUT__")
 *     （这是修复的核心：旧行为会永不 resolve）
 *  ② timeOfDay 正常推进到 budgetTicks → 刻超时先触发（与墙钟并存，谁先到谁算）
 *  ③ codePromise 先 resolve（成功）→ 不触发 __TIMEOUT__（两条 timer 都被 finally 清掉）
 *  ④ 墙钟时长随 rate 收缩（2 倍速下 500 刻 = 12.5s 墙钟，不是 25s）
 */
const fs = require("fs");
const path = require("path");
const vm = require("vm");

// 从 index.js 抽出 makeTickTimeoutPromise 的真正源码（含其上方注释块到函数闭合大括号）
const SRC = fs.readFileSync(path.join(__dirname, "index.js"), "utf-8");
const startIdx = SRC.indexOf("function makeTickTimeoutPromise(");
if (startIdx < 0) throw new Error("找不到 makeTickTimeoutPromise");
// 找函数闭合：从 startIdx 起匹配大括号
let depth = 0, endIdx = -1, inStr = false, strCh = "";
for (let i = SRC.indexOf("{", startIdx); i < SRC.length; i++) {
    const c = SRC[i];
    if (inStr) {
        if (c === "\\") { i++; continue; }
        if (c === strCh) inStr = false;
        continue;
    }
    if (c === '"' || c === "'" || c === "`") { inStr = true; strCh = c; continue; }
    if (c === "{") depth++;
    else if (c === "}") { depth--; if (depth === 0) { endIdx = i; break; } }
}
if (endIdx < 0) throw new Error("找不到 makeTickTimeoutPromise 闭合");
const FN_SRC = SRC.slice(startIdx, endIdx + 1);

async function main() {
    let pass = 0, fail = 0;
    const check = (name, cond, detail) => {
        if (cond) { pass++; console.log(`  ✓ ${name}`); }
        else { fail++; console.log(`  ✗ ${name}  ${detail || ""}`); }
    };

    // ① timeOfDay 冻结 + codePromise 永不落定 → 墙钟到点 resolve __TIMEOUT__
    console.log("① timeOfDay 冻结（断流）→ 墙钟兜底触发 __TIMEOUT__");
    {
        const ctx = {
            console, setTimeout, clearTimeout, setInterval, clearInterval,
            Date, Math, Number, Promise, Object,
            globalThis: {},
            currentTickRate: 1,
            bot: {
                time: { timeOfDay: 1000 }, // 冻结：永不更新
                openkageTickRate: 1,
            },
        };
        vm.createContext(ctx);
        vm.runInContext(FN_SRC + "\nglobalThis.__makeTickTimeoutPromise = makeTickTimeoutPromise;", ctx);
        const codePromise = new Promise(() => {}); // 永不落定
        const t0 = Date.now();
        const result = await ctx.globalThis.__makeTickTimeoutPromise(codePromise, 200, ctx.bot);
        const elapsed = Date.now() - t0;
        check("resolve __TIMEOUT__", result === "__TIMEOUT__", `result=${result}`);
        // 200 刻 / (20*1) = 10s 墙钟。给 2s 容差（timer 精度 + floor 下限 1000ms 不影响 10s 量级）
        check(`墙钟约 10s（实际 ${elapsed}ms）`, elapsed >= 8000 && elapsed < 13000, `elapsed=${elapsed}`);
    }

    // ② timeOfDay 正常推进到 budgetTicks → 刻超时先触发
    console.log("② timeOfDay 推进到 budgetTicks → 刻超时触发");
    {
        let tod = 1000;
        const ctx = {
            console, setTimeout, clearTimeout, setInterval, clearInterval,
            Date, Math, Number, Promise, Object,
            globalThis: {},
            currentTickRate: 1,
            bot: {
                get time() { return { timeOfDay: tod }; },
                openkageTickRate: 1,
            },
        };
        vm.createContext(ctx);
        vm.runInContext(FN_SRC + "\nglobalThis.__makeTickTimeoutPromise = makeTickTimeoutPromise;", ctx);
        // 模拟服务器持续推 timeOfDay：每 300ms（与 setInterval 同频）推进 200 刻
        const advancer = setInterval(() => { tod += 250; }, 50);
        const codePromise = new Promise(() => {});
        const t0 = Date.now();
        const result = await ctx.globalThis.__makeTickTimeoutPromise(codePromise, 200, ctx.bot);
        clearInterval(advancer);
        const elapsed = Date.now() - t0;
        check("resolve __TIMEOUT__", result === "__TIMEOUT__", `result=${result}`);
        // 刻超时应在 timeOfDay 累加到 200 后很快触发（advancer 50ms 一次，~几百 ms），远早于 10s 墙钟
        check(`刻超时先于墙钟（实际 ${elapsed}ms < 3000ms）`, elapsed < 3000, `elapsed=${elapsed}`);
    }

    // ③ codePromise 先 resolve → 不触发 __TIMEOUT__
    console.log("③ codePromise 先成功 → 不触发超时");
    {
        const ctx = {
            console, setTimeout, clearTimeout, setInterval, clearInterval,
            Date, Math, Number, Promise, Object,
            globalThis: {},
            currentTickRate: 1,
            bot: { time: { timeOfDay: 1000 }, openkageTickRate: 1 },
        };
        vm.createContext(ctx);
        vm.runInContext(FN_SRC + "\nglobalThis.__makeTickTimeoutPromise = makeTickTimeoutPromise;", ctx);
        // codePromise 立刻 resolve
        const codePromise = Promise.resolve();
        // 用 Promise.race：若 timeoutPromise 在 codePromise 之前 resolve 成 __TIMEOUT__，会赢
        const raced = await Promise.race([
            codePromise.then(() => "success"),
            ctx.globalThis.__makeTickTimeoutPromise(codePromise, 10000, ctx.bot),
        ]);
        check("codePromise 先赢（success）", raced === "success", `raced=${raced}`);
    }

    // ④ 墙钟随 rate 收缩：2 倍速下 500 刻 = 12.5s，不是 25s
    console.log("④ 2 倍速下墙钟随 rate 收缩");
    {
        const ctx = {
            console, setTimeout, clearTimeout, setInterval, clearInterval,
            Date, Math, Number, Promise, Object,
            globalThis: {},
            currentTickRate: 1,
            bot: { time: { timeOfDay: 1000 }, openkageTickRate: 2 },
        };
        vm.createContext(ctx);
        vm.runInContext(FN_SRC + "\nglobalThis.__makeTickTimeoutPromise = makeTickTimeoutPromise;", ctx);
        const codePromise = new Promise(() => {});
        const t0 = Date.now();
        const result = await ctx.globalThis.__makeTickTimeoutPromise(codePromise, 500, ctx.bot);
        const elapsed = Date.now() - t0;
        check("resolve __TIMEOUT__", result === "__TIMEOUT__", `result=${result}`);
        // 500 刻 / (20*2) = 12.5s 墙钟。若是旧行为（不随 rate）会是 25s
        check(`2x 下 500 刻≈12.5s（实际 ${elapsed}ms）`, elapsed >= 10000 && elapsed < 16000, `elapsed=${elapsed}`);
    }

    console.log(`\n${pass} passed, ${fail} failed`);
    process.exit(fail === 0 ? 0 : 1);
}

main().catch((e) => { console.error("smoke 崩了:", e); process.exit(2); });
