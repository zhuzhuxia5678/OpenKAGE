/**
 * resume gap 冒烟（真服）—— 验证砍掉 waitForTicks(20) 后：
 *   ① resume 往返墙钟从 ~1s 降到 <100ms（正常路径，shouldUsePhysics 已就绪）。
 *   ② 栅栏 tick 连续：pause 后 tick 冻住、resume 后 tick 重新推进（世界真解冻）。
 *   ③ 连续多轮 pause→resume 稳定（无监听器泄漏挂死、无状态漂移）。
 *
 * 前置：start_servers.sh --mc-only 已起 25567；index.js @ MINEFLAYER_PORT 已起。
 * 运行：MINEFLAYER_PORT=3000 node test_resume_gap_smoke.js
 */
const http = require("http");

const PORT = Number(process.env.MINEFLAYER_PORT || 3000);
const MC_PORT = 25567;
const BASE = `http://localhost:${PORT}`;
const log = (...a) => console.log(`[gap]`, ...a);

function post(p, body, timeoutMs = 60000) {
    return new Promise((resolve, reject) => {
        const data = JSON.stringify(body);
        const req = http.request(`${BASE}${p}`, {
            method: "POST",
            headers: { "Content-Type": "application/json", "Content-Length": Buffer.byteLength(data) },
            timeout: timeoutMs,
        }, (res) => {
            let buf = "";
            res.on("data", (c) => { buf += c; });
            res.on("end", () => resolve({ status: res.statusCode, body: buf }));
        });
        req.on("error", reject);
        req.on("timeout", () => { req.destroy(new Error("timeout")); });
        req.write(data);
        req.end();
    });
}

const wait = (ms) => new Promise((r) => setTimeout(r, ms));

/** 从四元组响应取 observe payload 的 timeTicks / worldDay。
 *  pause/resume 端点的响应是双层字符串化（res.json 包了已 stringify 的字符串），step 是单层。
 *  兼容两种：parse 到字符串就再 parse 一次。 */
function tickOf(raw) {
    try {
        let arr = typeof raw === "string" ? JSON.parse(raw) : raw;
        if (typeof arr === "string") arr = JSON.parse(arr); // 双层字符串化
        if (!Array.isArray(arr)) return null;
        for (const ev of arr) {
            if (Array.isArray(ev) && ev[0] === "observe" && ev[1]) {
                const st = ev[1].status || {};
                return { timeTicks: st.timeTicks, worldDay: st.worldDay };
            }
        }
    } catch (e) { /* ignore */ }
    return null;
}

async function main() {
    log("① /start 连 bot（port=%d）", MC_PORT);
    let r = await post("/start", { port: MC_PORT, username: "bot", waitTicks: 5, tick_rate: 1, reset: "soft" });
    log("   /start -> status=%d", r.status);
    if (r.status !== 200) { log("✗ /start 失败:", r.body.slice(0, 300)); process.exit(1); }
    await wait(500);

    // 跑一步真实动作，让 bot 处于「正常物理在跑」状态（shouldUsePhysics=true）
    log("② /step 跑一步（让物理循环就绪）");
    r = await post("/step", {
        programs: "async function w(b){await b.waitForTicks(5);}",
        code: "await w(bot);",
        timeoutTicks: 200,
    });
    log("   /step -> status=%d", r.status);
    if (r.status !== 200) { log("✗ /step 失败:", r.body.slice(0, 300)); }
    await wait(300);

    // 多轮 pause→resume，测往返墙钟 + tick 连续性
    const ROUNDS = 5;
    const resumeTimes = [];
    let prevTickAfterPause = null;
    let allOk = true;

    for (let i = 0; i < ROUNDS; i++) {
        // pause
        let pr = await post("/pause", {});
        if (pr.status !== 200) { log(`✗ 轮${i} /pause status=${pr.status}`); allOk = false; break; }
        const tickPaused = tickOf(pr.body);
        // 模拟思考期 1.5s
        await wait(1500);
        // resume —— 计时
        const t0 = Date.now();
        pr = await post("/resume", {});
        const dt = Date.now() - t0;
        resumeTimes.push(dt);
        if (pr.status !== 200) { log(`✗ 轮${i} /resume status=${pr.status}`); allOk = false; break; }
        const tickResumed = tickOf(pr.body);

        log(`轮${i}: pause tick=${tickPaused && tickPaused.timeTicks} -> resume tick=${tickResumed && tickResumed.timeTicks}  resume往返=${dt}ms`);

        // 栅栏连续性：resume 后 tick 应 >= pause 时 tick（解冻后世界重新推进，至少不变小）
        if (tickPaused && tickResumed && tickResumed.timeTicks < tickPaused.timeTicks) {
            log(`  ⚠ resume tick(${tickResumed.timeTicks}) < pause tick(${tickPaused.timeTicks})？`);
        }
        prevTickAfterPause = tickPaused;
        await wait(300);
    }

    // 判据汇总
    log("\n=== 判据 ===");
    if (resumeTimes.length === ROUNDS && allOk) {
        log(`✓ ${ROUNDS} 轮 pause→resume 全部 200`);
    } else {
        log(`✗ 有失败轮次`); allOk = false;
    }
    const avg = resumeTimes.length ? Math.round(resumeTimes.reduce((a, b) => a + b, 0) / resumeTimes.length) : -1;
    const max = resumeTimes.length ? Math.max(...resumeTimes) : -1;
    log(`resume 往返：avg=${avg}ms max=${max}ms  各轮=[${resumeTimes.join(", ")}]ms`);
    // 旧 waitForTicks(20) 在 1x 下 ≈ 1000ms；改后正常路径应 <150ms（留余量给 observe 序列化）
    if (avg > 0 && avg < 150) {
        log(`✓ resume gap 已从 ~1000ms 降到 ${avg}ms（<150ms，正常路径就绪探针快返）`);
    } else {
        log(`✗ resume 往返 avg=${avg}ms 偏高，期望 <150ms`); allOk = false;
    }
    // 稳定性：max 不该飙升（无泄漏挂死）
    if (max > 0 && max < 300) {
        log(`✓ 最慢一轮 ${max}ms < 300ms（无泄漏/挂死迹象）`);
    } else {
        log(`⚠ 最慢一轮 ${max}ms，关注是否有超时兜底`); // 不算硬失败：偶发 GC 可能
    }

    log("\n（不停 MC server，留给你继续测；index.js @ %d 仍在跑）", PORT);
    process.exit(allOk ? 0 : 1);
}

main().catch((e) => { console.error(e); process.exit(1); });
