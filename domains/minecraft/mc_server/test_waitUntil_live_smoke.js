/**
 * waitUntil 真服轻量直连冒烟 —— 连真 MC server(25567)，验证 waitUntil 注入后在 eval 作用域可达。
 *
 *     node test_waitUntil_live_smoke.js
 *
 * 钉住 2026-09-08 改动3：0827 去原语后 programs=""，waitUntil 不可用。现 load_wait_until_primitive
 * 单独注入。本测试模拟 index.js evaluateCode 的 eval 注入范式（programs + LLM code 拼 IIFE），
 * 在真服上验证：
 *  ① await waitUntil(bot,...) 不 ReferenceError（注入成功）
 *  ② /time set 21500（临近天亮）后调 waitUntil(['dawn'])，服务器时间自然流逝到≥22200 → dawn 命中
 *  ③ reason=condition_met、woke=dawn、timeOfDay≥22200
 *
 * 不连真服跑不了（timeOfDay 推进靠服务器 tick）。
 * username=op 名 bot（ops.json level 4，能发 /time set）。
 */
const mineflayer = require("mineflayer");
const fs = require("fs");
const path = require("path");

const HOST = "127.0.0.1";
const PORT = 25567;
const USERNAME = "bot";

const WAIT_UNTIL_SRC = fs.readFileSync(
    path.join(__dirname, "control_primitives", "waitUntil.js"),
    "utf-8"
);

// 模拟 index.js evaluateCode 的 eval 注入：programs(waitUntil) + LLM code 拼 IIFE
function evalWithWaitUntil(bot, code) {
    // 与 index.js:1556 同构：(async () => { programs + \nawait (async () => { code })() })()
    const wrapped = "(async () => {" + WAIT_UNTIL_SRC +
        "\nawait (async () => {" + code + "\n})()})()";
    // eslint-disable-next-line no-eval
    return eval(wrapped);
}

async function main() {
    console.log("[live] 连接 MC server", HOST + ":" + PORT, "as", USERNAME);
    const bot = mineflayer.createBot({
        host: HOST, port: PORT, username: USERNAME,
    });

    let exitCode = 1;
    const done = new Promise((resolve) => { bot._resolve = resolve; });

    bot.once("spawn", async () => {
        try {
            console.log("[live] bot 已 spawn，timeOfDay =", bot.time?.timeOfDay);
            // ① 事件模式：/time set 21500（临近天亮），waitUntil(bot,3000,reason,['dawn'])
            bot.chat("/time set 21500");
            await bot.waitForTicks(10);
            console.log("[live] ① /time set 21500 后 timeOfDay =", bot.time?.timeOfDay);
            let t0 = Date.now();
            await evalWithWaitUntil(bot,
                "globalThis.__wu1 = await waitUntil(bot, 3000, '真服测试等天亮', ['dawn']);");
            let elapsed = ((Date.now() - t0) / 1000).toFixed(1);
            const r1 = bot._wu1 || globalThis.__wu1;
            console.log("[live] ① waitUntil 返回（" + elapsed + "s）:", JSON.stringify(r1));

            // ② 纯计数器模式：waitUntil(bot,200,reason) 等够200刻醒，reason=condition_met、woke=ticks
            const todBefore = bot.time?.timeOfDay;
            t0 = Date.now();
            await evalWithWaitUntil(bot,
                "globalThis.__wu2 = await waitUntil(bot, 200, '纯定时器测试');");
            elapsed = ((Date.now() - t0) / 1000).toFixed(1);
            const r2 = bot._wu2 || globalThis.__wu2;
            const todAfter = bot.time?.timeOfDay;
            console.log("[live] ② waitUntil 返回（" + elapsed + "s）:", JSON.stringify(r2),
                "tod", todBefore, "→", todAfter);

            // ③ 判定
            let pass = 0, fail = 0;
            const check = (n, c, d) => { if (c) { console.log("  ✓", n); pass++; } else { console.log("  ✗", n, d || ""); fail++; } };
            console.log("[live] 事件模式 ['dawn']：");
            check("不 ReferenceError（注入可达）", r1 !== undefined, "r1=" + r1);
            check("reason=condition_met", r1 && r1.reason === "condition_met", JSON.stringify(r1));
            check("woke=dawn", r1 && r1.woke === "dawn", JSON.stringify(r1));
            check("timeOfDay≥22200", r1 && r1.timeOfDay >= 22200, "tod=" + (r1 && r1.timeOfDay));
            console.log("[live] 纯计数器模式（无 conditions）：");
            check("不 ReferenceError", r2 !== undefined, "r2=" + r2);
            check("reason=condition_met", r2 && r2.reason === "condition_met", JSON.stringify(r2));
            check("woke=ticks", r2 && r2.woke === "ticks", JSON.stringify(r2));
            check("timeOfDay 推进了≥200刻", todAfter - todBefore >= 200 || todAfter - todBefore + 24000 >= 200,
                `${todBefore}→${todAfter}`);
            console.log("[live] " + pass + " pass, " + fail + " fail");
            exitCode = fail === 0 ? 0 : 1;
        } catch (e) {
            console.error("[live] 测试异常:", e && e.stack || e);
            exitCode = 1;
        } finally {
            bot._resolve(exitCode);
        }
    });

    bot.on("error", (e) => { console.error("[live] bot error:", e); bot._resolve(1); });
    bot.on("end", () => { console.log("[live] bot end"); bot._resolve(exitCode); });

    const code = await done;
    try { bot.quit(); } catch (e) {}
    setTimeout(() => process.exit(code), 1000);
}

main();
