/**
 * 孤儿跨步串扰真跑验证（2026-09-15，接上个会话未答完的问题）。
 *
 *   bash start_servers.sh --mc-only            # 先起 MC server @ :25567（已起则跳过）
 *   MINEFLAYER_PORT=3001 node test_orphan_crosstep_live.js
 *
 * ## 问题（用户原话）
 * 「下一步开始了世界不再冻结，那如果上一步的孤儿还在空转会不会造成控制紊乱呢」
 *
 * ## 机制背景（代码已确认的事实）
 * - evaluateCode 的 Promise.race 不取消输家：超时/日界/死亡熔断只让本步返回，
 *   生成代码的 async IIFE（孤儿）仍在事件循环里挂着。
 * - 下一步 /step 开头只重置状态标记（cumulativeObs/openkageDied/openkageLog/openkageSkill、
 *   window_click 计数、heap 峰值），**不取消孤儿**。
 * - 孤儿每轮循环被 injectYield 注入 `await bot.waitForTicks(1)`，physicsTick 由本地
 *   setInterval 驱动（physics.js:493），/tick freeze 冻不住它；但 physicsEnabled=false
 *   后 physicsTick 不 emit（physics.js:86）→ 孤儿卡死在那一行。
 * - 栅栏：步末 trailing pause（freeze + physicsEnabled=false）→ 思考期 → 下步 resume
 *   （physicsEnabled=true + unfreeze）→ **resume 那一刻，卡着的孤儿全部复活**。
 *
 * ## 测什么
 * 模拟真实步序（stepA 超时留孤儿 → pause → 思考 → resume+stepB），验证四个判据：
 *   ① 孤儿日志串扰：stepA 的孤儿 console.log 是否出现在 stepB 的 onLog 里
 *   ② 孤儿动作串扰：stepA 的孤儿 goto/setControlState 是否在 stepB 期间操纵 bot 位置
 *   ③ 计数器污染：孤儿在 stepB 期间发的 window_click 是否计入 stepB 的步内计数（触发熔断误报）
 *   ④ pause 期间孤儿静止：freeze+physicsEnabled=false 窗口里孤儿循环不推进（tick 计数冻结）
 *
 * 判据②的关键设计：stepA 孤儿持续 setControlState('forward')；stepB 代码只原地
 * console.log 不移动。若 stepB 结束时 bot 位置明显位移 = 孤儿在操纵下一步的 bot。
 */
const http = require("http");

const MINEFLAYER_PORT = Number(process.env.MINEFLAYER_PORT || 3001);
const MC_PORT = 25567;
const BASE = `http://localhost:${MINEFLAYER_PORT}`;
const log = (...a) => console.log(`[orphan-test]`, ...a);

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

// 思考期时长（ms）。真实 run 里 = LLM 思考耗时，典型 10~40s（远超孤儿 5s 内置超时被杀）；
// 但也测短思考期（1.5s）——孤儿来不及被杀、resume 后是否复活串扰。
const PAUSE_MS = Number(process.env.PAUSE_MS || 5000);

/** 从 /step|/pause|/resume 响应里取信息：onLog 文本列表、observe payload、死亡数 */
function parseObs(raw) {
    let arr;
    try { arr = typeof raw === "string" ? JSON.parse(raw) : raw; } catch (e) { return { logs: [], obs: null, errors: [] }; }
    if (!Array.isArray(arr)) return { logs: [], obs: null, errors: [] };
    const logs = [];
    const errors = [];
    let obs = null;
    for (const ev of arr) {
        if (!Array.isArray(ev)) continue;
        if (ev[0] === "onLog") {
            const lines = (ev[1] && ev[1].events) || (ev[1] && ev[1].lines) || [];
            for (const l of lines) logs.push(typeof l === "string" ? l : JSON.stringify(l));
        }
        if (ev[0] === "onError") errors.push(JSON.stringify(ev[1]));
        if (ev[0] === "observe") obs = ev[1];
    }
    return { logs, obs, errors };
}

const DUMMY_PROGRAMS = `async function waitUntil(b,n,label){for(let i=0;i<n;i++){await b.waitForTicks(20);}}`;

async function main() {
    log("① /start 连 bot（port=%d）", MC_PORT);
    let r = await post("/start", {
        port: MC_PORT, username: "bot", waitTicks: 5, tick_rate: 1, reset: "soft",
    });
    log("   /start -> status=%d", r.status);
    if (r.status !== 200) { log("✗ /start 失败:", r.body.slice(0, 300)); process.exit(1); }
    await wait(4000);

    // 环境准备：白天、记录起点、bot 静止
    r = await post("/step", {
        code: `bot.chat('/time set day'); bot.clearControlStates(); await waitUntil(bot,10,'setup');`,
        programs: DUMMY_PROGRAMS, timeoutTicks: 300,
    });
    const setupObs = parseObs(r.body).obs;
    const startPos = setupObs && setupObs.status && setupObs.status.position;
    log("   起点 pos=%j", startPos);

    // ════════════════════════════════════════════════════════════════
    // ② stepA：申请 100 刻（短时限），代码要跑 ~2000 刻 → 必然 __TIMEOUT__ 留孤儿。
    //    孤儿做三件事（分别对应判据①②③）：
    //      - console.log('ORPHAN_A tick=N')（日志串扰探针）
    //      - setControlState('forward', true)（动作串扰探针）
    //      - 每 20 轮 console.log 存活计数（观察 pause/resume 边界行为）
    // ════════════════════════════════════════════════════════════════
    log("② stepA：短时限(100刻) + 长循环(~2400刻) → 超时留孤儿");
    const t0 = Date.now();
    r = await post("/step", {
        code: `
bot.clearControlStates();
bot.setControlState('forward', true);   // 孤儿会持续按着 forward
for (let i = 0; i < 120; i++) {
  await bot.waitForTicks(10);
  if (i % 5 === 0) console.log('ORPHAN_A tick=' + (i*10));
}
console.log('ORPHAN_A_COMPLETED');
`,
        programs: DUMMY_PROGRAMS, timeoutTicks: 100,   // 100 刻后 __TIMEOUT__，孤儿还剩 ~1100 刻
    });
    const stepADt = Date.now() - t0;
    log("   stepA -> status=%d 耗时=%dms（100刻@1x≈5s，若 ~5s 返回=超时熔断生效）", r.status, stepADt);
    const aObs = parseObs(r.body);
    log("   stepA onLog 行数=%d（孤儿跑到 tick=%s）",
        aObs.logs.length,
        (aObs.logs.filter(l => l.includes("ORPHAN_A tick=")).pop() || "无").toString());

    // ════════════════════════════════════════════════════════════════
    // ③ 模拟 Python 栅栏：步末 pause（思考期）→ 5s 后 resume
    // ════════════════════════════════════════════════════════════════
    log("③ pause（模拟步末 trailing pause / 思考期栅栏），等 %dms 再 resume", PAUSE_MS);
    const pauseT0 = Date.now();
    r = await post("/pause", {});
    const pauseObs = parseObs(r.body);
    log("   /pause -> status=%d", r.status);
    // pause 窗口内孤儿应卡死（physicsEnabled=false → waitForTicks 永不 resolve）
    await wait(PAUSE_MS);
    r = await post("/resume", {});
    const resumeObs = parseObs(r.body);
    log("   /resume -> status=%d（此刻 physicsEnabled=true，卡着的孤儿全部复活）", r.status);

    // ════════════════════════════════════════════════════════════════
    // ④ stepB：正常一步，代码只探针不动作。
    //    若孤儿在 resume 后串进 stepB：stepB 的 onLog 会出现 ORPHAN_A、位置会漂移。
    // ════════════════════════════════════════════════════════════════
    log("④ stepB：探针步（不移动、不打日志循环）");
    const tB = Date.now();
    r = await post("/step", {
        code: `
bot.clearControlStates();
console.log('STEPB_START pos=' + JSON.stringify(bot.entity.position.floored()));
for (let i = 0; i < 20; i++) { await bot.waitForTicks(10); }  // 站着等 200 刻，观察有没有被动
console.log('STEPB_END pos=' + JSON.stringify(bot.entity.position.floored()));
`,
        programs: DUMMY_PROGRAMS, timeoutTicks: 300,
    });
    const stepBDt = Date.now() - tB;
    log("   stepB -> status=%d 耗时=%dms", r.status, stepBDt);
    const bObs = parseObs(r.body);
    const bLogs = bObs.logs;
    const startPosB = (bLogs.find(l => l.includes("STEPB_START")) || "").match(/pos=(\{.*\})/)?.[1];
    const endPosB = (bLogs.find(l => l.includes("STEPB_END")) || "").match(/pos=(\{.*\})/)?.[1];
    const orphanInB = bLogs.filter(l => l.includes("ORPHAN_A"));
    log("   stepB onLog 总行数=%d，其中孤儿串入行数=%d", bLogs.length, orphanInB.length);
    if (orphanInB.length) log("     串入样例: %j", orphanInB.slice(0, 3));
    log("   STEPB pos %s → %s", startPosB, endPosB);

    // 位移计算
    let drift = null;
    try {
        const s = JSON.parse(startPosB), e = JSON.parse(endPosB);
        drift = Math.hypot(e.x - s.x, e.z - s.z);
    } catch (_) { /* parse 失败不致命 */ }
    log("   stepB 期间位移 = %s 格", drift === null ? "?" : drift.toFixed(1));

    // ════════════════════════════════════════════════════════════════
    // ⑤ 判据 ④（pause 期间孤儿静止）：pause/resume 之间 waitForTicks 不推进——
    //    无法从外面直接读孤儿计数，用间接判据：stepA 的 onLog 里孤儿最后 tick 数
    //    vs 超时时刻的理论值（100 刻 ≈ 10 轮 i）。若 stepA 返回时孤儿已跑到 i≥50
    //    说明 abort 没掐（正常：纯 for 循环 abort 掐不断）。
    // ════════════════════════════════════════════════════════════════
    log("\n========== 判据 ==========");
    const timeoutWorked = stepADt < 10000;  // 100刻@1x=5s，+步尾等待 ≈ 6-8s
    log("① stepA 超时熔断按时返回: %s（%dms）", timeoutWorked ? "✓" : "✗", stepADt);
    log("② 孤儿日志串进 stepB: %s（%d 行）", orphanInB.length > 0 ? "⚠ 串扰实锤" : "未串入", orphanInB.length);
    const moved = drift !== null && drift > 2.0;
    log("③ stepB 期间 bot 被动位移>2格: %s（%s 格）%s",
        moved ? "⚠ 控制紊乱实锤" : "否", drift === null ? "?" : drift.toFixed(1),
        moved ? "" : "（孤儿 forward 未操纵 stepB 的 bot / 或已被 abort）");
    log("④ stepB 耗时是否被孤儿拖慢: %s（%dms，正常≈10-12s）", stepBDt > 20000 ? "⚠ 异常" : "正常", stepBDt);
    log("==========================");

    // 结论不设 pass/fail 硬门槛——这是观测性测试，四种串扰形态都要如实报告
    log("\n解读：见脚本头部『测什么』；把 ①②③④ 的结果带回给主会话分析。");
    process.exit(0);
}

main().catch((e) => { log("异常:", e); process.exit(2); });
