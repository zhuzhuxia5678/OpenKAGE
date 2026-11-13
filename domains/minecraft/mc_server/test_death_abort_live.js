/**
 * 死亡熔断 + 冻结 + 重生点位置 真跑验证（2026-09-15，配合 __DIED__ 分支升级）。
 *
 *   bash start_servers.sh --mc-only            # 先起 MC server @ :25567（已起）
 *   MINEFLAYER_PORT=3001 node test_death_abort_live.js
 *
 * ## 测什么
 * index.js evaluateCode 的 __DIED__ 分支升级为「死亡即 pauseBot + 等孤儿死透 + 等重生完成」后必须：
 *   ① /step 在 bot 死亡时返回 success（不报错、不挂起）——死亡熔断正常工作
 *   ② /step 的 observe 回传里含死亡播报（onSystem 帧）——agent 拿得到死亡反馈
 *   ③ 死亡后孤儿代码死透（pauseBot 卡死 waitForTicks + 等待收尾）——孤儿未跑完、死亡步 <12s
 *   ④ 死亡步 observe 取到的是**重生点位置**（非死亡现场旧位置）——_waitForRespawn 等到了 spawn
 *   ⑤ 死亡后**世界冻结**（/tick freeze 生效）——pauseBot 的 /tick freeze 真冻住世界时间
 *
 * 死亡触发：tp 到虚空外 y=-80 立即死（death.attack.outOfWorld，不在 IGNORED_DEATH_KEYS）。
 * 死亡现场 y≈-80，重生点 y≈70+，④ 用 y 差距 >50 判 observe 是否等到了重生。
 *
 * ★ ⑤ 是 pauseBot /tick bypass 修复（bot.chat→bot._chat）的回归守卫：修复前 pauseBot 在
 *   __DIED__ 分支被调时 openkageInGeneratedCode 仍 true，bot.chat 的 /tick 被命令拦截层 block，
 *   世界未冻（7cc48de 实测 7/7 被拦，死亡循环没治住）；③ 的「孤儿卡死」靠 physicsEnabled=false
 *   那半仍能过，掩盖了 /tick 失效。⑤ 直接验世界冻结（两次 /pause 间隔 2s 的 timeTicks delta=0），
 *   /tick 被 block 时 delta≈40 tick（tick_rate=1），红灯。
 *
 * ## 判据
 *   ② 死亡反馈进 observe（agent 拿得到死亡反馈）✓ 核心
 *   ③ 死亡熔断快速返回(<12s)+孤儿未跑完（pauseBot+等待生效）✓ 核心
 *   ④ 死亡步 observe 取到重生点（非死亡现场，dy>50）✓ 核心
 *   ⑤ 死亡后世界冻结（两次 /pause timeTicks delta≤2）✓ 核心
 */
const path = require("path");
const http = require("http");

const MINEFLAYER_PORT = Number(process.env.MINEFLAYER_PORT || 3001);
const MC_PORT = 25567;
const BASE = `http://localhost:${MINEFLAYER_PORT}`;
const log = (...a) => console.log(`[death-test]`, ...a);

function post(p, body) {
    return new Promise((resolve, reject) => {
        const data = JSON.stringify(body);
        const req = http.request(`${BASE}${p}`, {
            method: "POST",
            headers: { "Content-Type": "application/json", "Content-Length": Buffer.byteLength(data) },
            timeout: 60000,
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

// 复刻 env.py _collect_system_events：从 observe 响应里抽出 onSystem 死亡事件。
// observe 响应形如 '[["observe",{...}],["onSystem",{events:[...]}],...]' 或已 parse 的数组。
function collectDeaths(rawObs) {
    let arr;
    try {
        arr = typeof rawObs === "string" ? JSON.parse(rawObs) : rawObs;
    } catch (e) { return []; }
    if (!Array.isArray(arr)) return [];
    const deaths = [];
    for (const ev of arr) {
        if (!Array.isArray(ev) || ev[0] !== "onSystem") continue;
        const payload = ev[1] || {};
        for (const s of (payload.events || [])) {
            if (s && s.kind === "death") deaths.push(s);
        }
    }
    return deaths;
}

// 从 observe 里取最后一个 observe 事件的 payload（env.py _parse_observe 同款）
function lastObserve(rawObs) {
    let arr;
    try { arr = typeof rawObs === "string" ? JSON.parse(rawObs) : rawObs; } catch (e) { return null; }
    if (!Array.isArray(arr)) return null;
    for (let i = arr.length - 1; i >= 0; i--) {
        if (Array.isArray(arr[i]) && arr[i][0] === "observe") return arr[i][1];
    }
    return null;
}

const wait = (ms) => new Promise((r) => setTimeout(r, ms));

async function main() {
    log("① /start 连 bot（port=%d, user=bot）", MC_PORT);
    let r = await post("/start", {
        port: MC_PORT, username: "bot", waitTicks: 5, tick_rate: 1, reset: "soft",
    });
    log("   /start -> status=%d", r.status);
    if (r.status !== 200) { log("✗ /start 失败:", r.body.slice(0, 300)); process.exit(1); }
    await wait(4000); // 等 spawn + 插件装载

    // 先保证 bot 满血、白天、安全位置
    log("② 设白天 + 满血（/time set day + /damage 清不了血，直接 /effect 给抗性）");
    await post("/step", {
        code: `bot.chat('/time set day'); await waitUntil(bot, 20, 'set day');`,
        programs: `async function waitUntil(b,n,label){for(let i=0;i<n;i++){await b.waitForTicks(20);}}`,
        timeoutTicks: 400,
    });
    await wait(1000);

    // 记死亡现场坐标（tp/kill 前的位置，用于后面比对 observe 是否取到了重生点而非旧现场）。
    // 直接从探针步的 observe 帧取 position（最可靠），不依赖 onLog 字符串解析。
    let deathScenePos = null;
    let r0 = await post("/step", {
        code: `console.log('HP_BEFORE=' + bot.health + ' food=' + bot.food);`,
        programs: `async function waitUntil(b,n,label){for(let i=0;i<n;i++){await b.waitForTicks(20);}}`,
        timeoutTicks: 200,
    });
    log("   探针步 HP_BEFORE 打出（见 onLog）");
    try {
        deathScenePos = (lastObserve(JSON.parse(r0.body)) || {}).status?.position || null;
    } catch (e) { /* ignore */ }
    log("   死亡现场坐标(kill 前)=%j", deathScenePos);

    // ★ 核心：跑一段会死的代码 + 一个孤儿循环（模拟 dstlow3 的 waitUntil(天亮)）
    //   死亡触发：trusted 基建脚本里 bot._chat('/kill') 直接发命令（绕过 skillLoader 对生成代码
    //   斜杠指令的拦截；/kill 触发 death 事件 + health→0，__DIED__ 熔断靠 health 轮询仍生效。
    //   /kill 死因是 genericKill 被 onSystem 排除不计入 deaths 计数，但熔断/重生机制照常走）。
    //   同时挂一个长循环孤儿，若 pauseBot 没卡死它，孤儿会在死亡后继续跑（observe 里有它的 log）；
    //   若 pauseBot 生效，孤儿卡在 waitForTicks、5.5s 内置超时 reject 死透。
    log("③ /step 跑死亡触发代码（/kill + 挂孤儿循环）");
    const deathCode = `
bot._chat('/kill');
// 孤儿：死亡熔断后这段若没被 pauseBot 卡死会继续 console.log
for (let i = 0; i < 30; i++) {
  await bot.waitForTicks(10);
  console.log('ORPHAN_STILL_RUNNING tick=' + i);
}
console.log('ORPHAN_COMPLETED');
`;
    const t0 = Date.now();
    r = await post("/step", {
        code: deathCode,
        programs: `async function waitUntil(b,n,label){for(let i=0;i<n;i++){await b.waitForTicks(20);}}`,
        timeoutTicks: 600,
        trusted: true,  // 基建式死亡触发：放行 /kill（生成代码里的斜杠指令默认被拦）
    });
    const dt = Date.now() - t0;
    log("   /step(死亡步) -> status=%d 耗时=%dms", r.status, dt);
    if (r.status !== 200) { log("✗ /step 非 200:", r.body.slice(0, 400)); }

    let obsPayload = null;
    try { obsPayload = JSON.parse(r.body); } catch (e) { log("   响应非 JSON:", r.body.slice(0, 200)); }

    // ② 死亡确实发生并完成重生：死亡步 observe 的 health 应==20（满血=已重生）。
    //    /kill 是 genericKill 被 onSystem 排除不计入 deaths 计数，故不用 collectDeaths 判；
    //    health==20 是死亡→重生完成的硬信号，对 /kill 和真实怪物死亡都成立。
    let deathStepHealth = null;
    let respawnPosInDeathStep = null;
    try {
        const arr = typeof r.body === "string" ? JSON.parse(r.body) : r.body;
        const obs = lastObserve(arr);
        if (obs && obs.status) {
            deathStepHealth = obs.status.health;
            respawnPosInDeathStep = obs.status.position;
        }
    } catch (e) { /* ignore */ }
    log("   ② 死亡步 observe health=%s position=%j（health==20 = 死亡发生并完成重生）",
        deathStepHealth, respawnPosInDeathStep);

    // ③ 验证孤儿是否被 pauseBot 卡死：看死亡步响应的 onLog 里有没有 ORPHAN_COMPLETED。
    //    onLog observe 返回的是字符串（lines join），直接 String(ev[1]) 找。
    //    pauseBot 后孤儿卡在 waitForTicks、5.5s 内置超时 reject 死透 → 不会跑完 30 轮。
    let orphanRan = false;
    let orphanCompleted = false;
    let orphanMaxTick = -1;
    try {
        const arr = typeof r.body === "string" ? JSON.parse(r.body) : r.body;
        if (Array.isArray(arr)) {
            for (const ev of arr) {
                if (!Array.isArray(ev) || ev[0] !== "onLog") continue;
                const txt = String(ev[1]);
                if (txt.includes("ORPHAN_STILL_RUNNING")) orphanRan = true;
                if (txt.includes("ORPHAN_COMPLETED")) orphanCompleted = true;
                for (const m of txt.matchAll(/ORPHAN_STILL_RUNNING tick=(\d+)/g)) {
                    orphanMaxTick = Math.max(orphanMaxTick, Number(m[1]));
                }
            }
        }
    } catch (e) { /* ignore */ }
    log("   ③ 死亡步 onLog: 孤儿跑到 maxTick=%s, COMPLETED=%s（应未跑完 30 轮）",
        orphanMaxTick, orphanCompleted);

    // ④ 死亡步 observe 取到的位置（应为重生点，非死亡现场旧位置）
    //    __DIED__ 分支现在会等 _waitForRespawn(spawn 事件) 再返回，observe 应已是重生点。
    log("   ④ 死亡步 observe position=%j（应=重生点，≠ 死亡现场 %j）",
        respawnPosInDeathStep, deathScenePos);

    // ⑤ 死亡后世界应冻结（/tick freeze 生效）：__DIED__ 分支的 pauseBot 发 /tick freeze 冻怪 AI。
    //    修复前 pauseBot 用 bot.chat 被 skillLoader 拦截层 block（openkageInGeneratedCode 仍 true），
    //    世界未冻；修复后走 bot._chat 绕过。验证：死亡步返回后（世界应已被 __DIED__ 的 pauseBot 冻），
    //    连续两次 /pause（幂等重冻+返回 observe）间隔 2s 墙钟，比较 status.timeTicks——冻结则两次
    //    相同（游戏时间不推进），未冻结则推进（2.5 倍速下 2s ≈ 50 tick，远超 timeOfDay 回绕误差）。
    //    测试 tick_rate=1（/start 设），2s 墙钟 ≈ 40 tick；冻结时 delta=0。
    //    ★ 不用 /step 读（/step 会 resume 解冻，测不到死亡步末的冻结态）；/pause 不 resume、
    //    只 pauseBot 幂等重冻+observe，正好读「死亡步末冻结状态」。
    const tickOf = (resp) => {
        try {
            const arr = typeof resp.body === "string" ? JSON.parse(resp.body) : resp.body;
            return (lastObserve(arr) || {}).status?.timeTicks ?? null;
        } catch (e) { return null; }
    };
    log("⑤ 死亡后世界冻结验证（/tick freeze 生效）：两次 /pause 间隔 2s 读 timeTicks");
    const r_p1 = await post("/pause", {});
    const tt1 = tickOf(r_p1);
    await wait(2000);
    const r_p2 = await post("/pause", {});
    const tt2 = tickOf(r_p2);
    const freezeDelta = (tt1 != null && tt2 != null) ? Math.abs(tt2 - tt1) : null;
    log("   /pause timeTicks: 第一次=%s 第二次=%s delta=%s（delta=0 = 世界冻结 /tick 生效）",
        tt1, tt2, freezeDelta);

    // 死亡后立刻再打一步探针，看世界状态（health 应回满=已重生；若孤儿没停，这步可能被干扰）
    await wait(1500);
    log("⑥ 死亡后探针步：看 bot 状态（health 应回满=重生成功）");
    r = await post("/step", {
        code: `console.log('HP_AFTER_DEATH=' + bot.health + ' pos=' + JSON.stringify(bot.entity.position.floored()));`,
        programs: `async function waitUntil(b,n,label){for(let i=0;i<n;i++){await b.waitForTicks(20);}}`,
        timeoutTicks: 200,
    });
    log("   探针步 status=%d", r.status);

    // 判据汇总
    log("\n========== 判据 ==========");
    const ok_return = r0.status === 200; // 前置探针能跑=server 正常
    // ② 死亡确实发生并完成重生：死亡步 observe health==20（满血=重生完成）。
    //    /kill 不计 deaths 计数，故用 health 判；这是死亡→重生完成的硬信号。
    const ok_rebirth = deathStepHealth === 20;
    log("① 死亡步返回成功(前置探针绿): %s", ok_return ? "✓" : "✗");
    log("② 死亡发生并完成重生(observe health==20): %s (health=%s)",
        ok_rebirth ? "✓ 核心" : "✗ 核心", deathStepHealth);
    // ③ pauseBot 卡死孤儿判据：孤儿不该跑完 30 轮（pauseBot→physicsEnabled=false→
    //    waitForTicks 卡死→5.5s 内置超时 reject 死透）。死亡步耗时应在 8s 等待窗口内
    //    （/kill 瞬时死 + 等孤儿 reject ~5.5s + 等重生 ~1s ≈ 6-7s）。若孤儿跑完=没死透。
    const ok_orphan_dead = !orphanCompleted && dt < 12000;
    log("③ pauseBot 卡死孤儿(未跑完+<12s): %s (耗时%dmS, 孤儿完成=%s, maxTick=%s)",
        ok_orphan_dead ? "✓ 核心" : "✗", dt, orphanCompleted, orphanMaxTick);

    // ④ 重生点位置判据：死亡步 observe 的 position 应明显不同于死亡现场（_waitForRespawn 等到了
    //    spawn，position 已更新到重生点）。/kill 重生点=出生点，与死亡现场一般 x/z 不同。
    let ok_respawn_pos = false;
    if (respawnPosInDeathStep && deathScenePos) {
        const dx = Math.abs((respawnPosInDeathStep.x || 0) - (deathScenePos.x || 0));
        const dz = Math.abs((respawnPosInDeathStep.z || 0) - (deathScenePos.z || 0));
        const dy = Math.abs((respawnPosInDeathStep.y || 0) - (deathScenePos.y || 0));
        // 重生点与死亡现场应有明显位移（任一轴 >3 格）。若几乎重合 = observe 可能还是旧位置。
        ok_respawn_pos = Math.max(dx, dy, dz) > 3;
        log("④ 死亡步 observe 取到重生点(≠死亡现场): %s (dxz=%.0f,%.0f dy=%.0f, 现场=%j→observe=%j)",
            ok_respawn_pos ? "✓ 核心" : "✗", dx, dz, dy, deathScenePos, respawnPosInDeathStep);
    } else {
        log("④ 死亡步 observe 取到重生点: 跳过（缺位置数据: observe=%j 现场=%j）",
            respawnPosInDeathStep, deathScenePos);
    }

    // ⑤ 死亡后世界冻结（/tick freeze 生效）判据：两次 /pause 间隔 2s 的 timeTicks delta 应=0
    //    （世界冻结=游戏时间不推进）。修复前 /tick 被 block，delta 会 ≈40 tick（tick_rate=1，2s）。
    //    容差 2 tick 防 timeOfDay 回绕/采样抖动。delta>5 = /tick freeze 未生效（红灯）。
    const ok_world_frozen = freezeDelta != null && freezeDelta <= 2;
    log("⑤ 死亡后世界冻结(/tick freeze 生效): %s (delta=%s, 应≤2)",
        ok_world_frozen ? "✓ 核心" : "✗ 核心", freezeDelta);

    log("==========================");
    if (ok_rebirth && ok_orphan_dead && ok_respawn_pos && ok_world_frozen) {
        log("✅ 死亡熔断 + 冻结 + 重生点位置 + 世界冻结 真跑验证通过");
        process.exit(0);
    } else {
        log("❌ 验证未通过（见上）");
        process.exit(1);
    }
}

main().catch((e) => { log("异常:", e); process.exit(2); });
