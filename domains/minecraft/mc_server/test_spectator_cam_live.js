/**
 * spectatorCam 采集器 live 验证 —— 连真 MC server(25567)，真 bot + 真观战客户端双实体，
 * 驱动附身→死亡脱附→重附全周期，验证采集器经 /data get 探针读出的三态读数（2026-09-15 建）。
 *
 *     node test_spectator_cam_live.js
 *
 * ★ 本测试是探针数据源的**定案验证**（此前两条路被真服证伪，别再走回头路，见 spectatorCam.js）：
 *   ✗ bot.entities / bot.players[spec].entity —— spectator 实体不进其他玩家的实体同步，恒 null；
 *   ✗ 观战客户端侧 camera 包 —— 信号完美但生产环境观战侧是真 MC 客户端，包不落日志；
 *   ✓ bot（OP）发 /data get entity <spec> Pos —— 服务器权威，回执走 bot 聊天通道。
 *
 * 验证（与 env._spectator_needs_reattach 判据对齐，阈值 32 格）：
 *   ① 附身态：distance ≈0（服务器每刻把观战者坐标写成 bot 位置）
 *   ② bot 位移后仍附身：distance 仍 <32（探针跟随，缓存与 bot 新位置同步）
 *   ③ bot 死亡（被动脱附，death 事件为铁证）：distance >32（观战者冻在死亡点，bot 重生在远处）
 *   ④ 重附后：distance 回落 <32（看门狗→resync 闭环）
 *   ⑤ probeStale 语义：发查询后、回执前为 true；回执到达后为 false
 *   ⑥ 未配 spectatorName（对照组形态）：恒 {pos:null,...} 不发命令
 *
 * 观战账号 spec_wd 需在 ops.json（服务器不热加载 ops.json——已在 server121 配好；换服务器时
 * 先 /op）。bot 用户名在 ops.json（模板自带）。
 *
 * 断言失败 exit 1；全过 exit 0。总时长 ~75s（③④ 前各留 5.2s 探针节流窗口）。
 */
const assert = require("assert");
const path = require("path");
const mineflayer = require("mineflayer");

const SpectatorCam = require(path.join(__dirname, "lib", "observation", "spectatorCam.js"));

const HOST = "127.0.0.1";
const PORT = 25567;
const VERSION = "1.21.11";
const SPEC = "spec_wd";
const DETACH = 32; // 与 env._SPECTATOR_DETACH_DISTANCE 一致

const log = (...a) => console.log("[live]", ...a);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function wait(check, ms, label) {
    const t0 = Date.now();
    while (Date.now() - t0 < ms) { if (check()) return true; await sleep(400); }
    log(`  ⚠ 等待超时: ${label}`);
    return false;
}

function connect(username) {
    return mineflayer.createBot({ host: HOST, port: PORT, username, version: VERSION });
}

async function main() {
    const bot = connect("bot");
    const spec = connect(SPEC);
    let specChat = null, botDied = 0;
    spec.on("message", (m) => { const s = m.toString(); if (s.includes("Now spectating")) specChat = s; });
    bot.on("death", () => { botDied++; });

    // 采集器实例化（与 index.js obs.inject 同位：类经 new obs(bot)，名字挂 bot.spectatorName）
    bot.spectatorName = SPEC;
    const cam = new SpectatorCam(bot);

    if (!await wait(() => bot.entity && spec.entity, 20000, "双 spawn")) { process.exit(1); }
    log("双 spawn OK");

    // ⑥ 对照组形态：未配名的采集器恒 null、不发包
    const anonBot = { spectatorName: null, _chat: () => { throw new Error("不该发命令"); } };
    const anonCam = new SpectatorCam(anonBot);
    assert.deepStrictEqual(anonCam.observe(), { pos: null, distance: null, probeStale: false, botPos: null },
        "未配观战名应恒 null 且不发命令");
    log("⑥ 未配名形态 OK（不发命令、恒 null）");

    // 观战客户端就位：spectator + 近距 + 附身
    spec.chat("/gamemode spectator");
    await wait(() => spec.game && spec.game.gameMode === "spectator", 8000, "gamemode");
    spec.chat(`/tp ${SPEC} bot`); await sleep(1000);
    specChat = null; spec.chat(`/spectate bot`);
    if (!await wait(() => specChat, 10000, "Now spectating 回执")) { process.exit(1); }
    log("附身回执:", specChat);

    // ⑤ probeStale：首查在途=true → 回执到=false
    let snap = cam.observe();          // 首次 observe 触发查询
    assert.strictEqual(snap.probeStale, true, "发出查询后（回执前）probeStale 应为 true");
    assert.strictEqual(snap.pos, null, "首查前 pos 应为 null");
    if (!await wait(() => !cam.observe().probeStale, 8000, "首查回执")) { process.exit(1); }
    snap = cam.observe();
    assert.strictEqual(snap.probeStale, false, "回执到达后 probeStale 应回 false");
    log(`⑤ probeStale 语义 OK（回执 pos=${JSON.stringify(snap.pos)}）`);

    // ① 附身态：distance ≈0
    snap = cam.observe();
    log(`① 附身态: distance=${snap.distance}`);
    assert.ok(snap.distance !== null && snap.distance < DETACH, `附身态 distance 应 <${DETACH}，实测 ${snap.distance}`);

    // ② bot 位移后仍附身（探针跟随）
    // ★ sleep 必须 > PROBE_INTERVAL_MS(5s)：探针 5s 节流——tp 后立刻 observe 不会发新查询，
    //   读到的是 ⑤ 时（tp 前）的旧回执 → 假性 distance 巨大（踩过：290.7 的假失败）。
    bot.chat("/tp bot -200 100 -200");
    await sleep(5200);
    cam.observe();                      // 节流窗口已过，真正发出新查询
    await sleep(1800);                  // 等回执
    snap = cam.observe();
    log(`② bot 位移后(仍附身): distance=${snap.distance} botPos=${JSON.stringify(snap.botPos)}`);
    assert.ok(snap.distance !== null && snap.distance < DETACH,
        `附身下 bot 位移后 distance 仍应 <${DETACH}（服务器逐刻同步），实测 ${snap.distance}`);

    // ③ bot 死亡 → 被动脱附（death 事件铁证，观战者冻在死亡点）
    bot.chat("/kill bot");
    if (!await wait(() => botDied > 0, 8000, "death 事件")) { process.exit(1); }
    await sleep(2700);
    cam.observe(); await sleep(1800);   // 节流窗口（5s）已过：新查询 + 等回执
    snap = cam.observe();
    log(`③ 死亡脱附: distance=${snap.distance}（观战者冻在死亡点，bot 重生在 spawn）`);
    assert.ok(snap.distance !== null && snap.distance > DETACH,
        `死亡脱附后 distance 应 >${DETACH}，实测 ${snap.distance}`);

    // ④ 重附（看门狗→resync 闭环）
    spec.chat(`/tp ${SPEC} bot`); await sleep(1000);
    specChat = null; spec.chat("/spectate bot");
    if (!await wait(() => specChat, 10000, "重附回执")) { process.exit(1); }
    await sleep(5200);                  // 节流窗口（同 ②）
    cam.observe(); await sleep(1800);
    snap = cam.observe();
    log(`④ 重附: distance=${snap.distance}`);
    assert.ok(snap.distance !== null && snap.distance < DETACH,
        `重附后 distance 应回落 <${DETACH}，实测 ${snap.distance}`);

    log("全部通过 ✓（附身≈0 / 位移仍贴 / 死亡脱附>32 / 重附归位 / probeStale 语义 / 对照组恒 null）");
    bot.quit(); spec.quit();
    process.exit(0);
}

process.on("unhandledRejection", (e) => { console.error("[live] 未处理异常:", e); process.exit(1); });
main().catch((e) => { console.error("[live] 失败:", e && e.message ? e.message : e); process.exit(1); });
setTimeout(() => { console.error("[live] 总超时 120s，视为失败"); process.exit(1); }, 120000).unref();
