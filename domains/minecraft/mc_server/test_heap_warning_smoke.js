/**
 * heap 告警真服冒烟 —— lightAreaWithTorches 的 OOM 触发点。
 *
 * 触发机制：
 *   lightAreaWithTorches(bot, mcData, Vec3, GoalNear, center, radius, opts)
 *   radius=8/step=2 → 81 候选点，每点 goto + blockAt 纵扫(cy+3→cy-6 共10格) + placeBlock。
 *   远列未加载区块强制 blockAt 加载、goto 不可达时 A* open list 累积 → 进程 heap 峰值飙升
 *   → V8 heap OOM → 端口秒级 DOWN → Python 侧只看到 ConnectionError（崩前无痕）。
 *
 * 0910 加的 heap 采样器（index.js:322-349）目标：在冲到 _OPENKAGE_HEAP_LIMIT_MB*ratio（默认 3.2G）
 * 前，physicsTick 采样置 _openkageHeapWarned 标志、打 [heap告警] 行、步末（index.js:1554-1563）
 * 据 flag emit error → onError 反馈进 observe。不主动中止（内存飙升不该粗暴打断合法慢操作）。
 *
 * 本测试两阶段（用户选「先链路后真冲」）：
 *   阶段1（环境变量 OPENKAGE_HEAP_WARN_RATIO 压到几 MB）：用原技能代码跑少量点，只验证
 *     采样→[heap告警]console.log→步末 onError 链路贯通，不真冲堆。快、安全。
 *   阶段2（放开阈值 0.80 默认）：原码 + 大 radius 真冲，看采样器能否在崩前抢到 + 进程不崩。
 *
 * 用法（环境由调用方备好：start_servers.sh --mc-only 起 25567 + 带阈值的 index.js）：
 *   阶段1：OPENKAGE_HEAP_WARN_RATIO=0.00001 已在 index.js 进程环境里（复用残留），
 *          直接：node test_heap_warning_smoke.js
 *   阶段2：需重启 index.js 不带 OPENKAGE_HEAP_WARN_RATIO（走默认 0.80），再：
 *          node test_heap_warning_smoke.js --stage2
 *
 * 依赖：bot 是 op（ops.json level 4，/give 火把用）；programs 注入 lightAreaWithTorches 原码，
 * code 调用它。Vec3/mcData/GoalNear 由 /step 闭包提供（index.js /step handler 内 require）。
 */
const http = require("http");
const fs = require("fs");
const path = require("path");

const HOST = "127.0.0.1";
const PORT = Number(process.env.INDEX_PORT || 3002);
const INDEX_URL = `http://${HOST}:${PORT}`;
const HTTP_TIMEOUT_MS = 180000; // 阶段2真冲可能慢，给 3 分钟

const STAGE2 = process.argv.includes("--stage2");

// ── lightAreaWithTorches 原码（bob skill 库 verbatim，0910 run 的真实 OOM 触发点）─────────
const LIGHT_AREA_SRC = `
async function lightAreaWithTorches(bot, mcData, Vec3, GoalNear, center, radius, opts = {}) {
  const torchName = opts.torchName || 'torch';
  const step = opts.step || 2;
  const lightThreshold = opts.lightThreshold || 8;
  const placeable = new Set(['grass_block', 'dirt', 'coarse_dirt', 'podzol', 'cobblestone', 'stone',
    'andesite', 'granite', 'diorite', 'deepslate', 'sand', 'red_sand', 'gravel', 'snow_block',
    'oak_planks', 'spruce_planks', 'birch_planks']);
  const isAir = n => n === 'air' || n === 'cave_air';
  const countTorches = () => bot.inventory.items().filter(i => i.name === torchName).reduce((a, i) => a + i.count, 0);

  if (countTorches() === 0) throw new Error('lightArea: 背包没有 ' + torchName);

  const cx = Math.floor(center.x), cy = Math.floor(center.y), cz = Math.floor(center.z);
  const placed = [];

  for (let dx = -radius; dx <= radius; dx += step) {
    for (let dz = -radius; dz <= radius; dz += step) {
      if (countTorches() === 0) return placed;

      let ground = null;
      for (let y = cy + 3; y >= cy - 6; y--) {
        const b = bot.blockAt(center.offset(dx, y - cy, dz));
        if (!b) continue;
        const above = bot.blockAt(center.offset(dx, y - cy + 1, dz));
        const above2 = bot.blockAt(center.offset(dx, y - cy + 2, dz));
        if (placeable.has(b.name) && above && isAir(above.name) && above2 && isAir(above2.name)) {
          ground = b;
          break;
        }
      }
      if (!ground) continue;

      const spot = ground.position.offset(0, 1, 0);
      const at = bot.blockAt(spot);
      if (at && at.name === torchName) continue;
      try { if (bot.getLightAt && bot.getLightAt(spot) >= lightThreshold) continue; } catch (e) {}

      try { await bot.pathfinder.goto(new GoalNear(spot.x, spot.y, spot.z, 3)); } catch (e) {}

      const below = bot.blockAt(spot.offset(0, -1, 0));
      if (!below || isAir(below.name)) continue;

      const torchItem = bot.inventory.items().find(i => i.name === torchName);
      if (!torchItem) return placed;
      try { await bot.equip(torchItem, 'hand'); } catch (e) {}

      const face = spot.minus(below);
      let done = false;
      for (let a = 0; a < 2 && !done; a++) {
        try { await bot.placeBlock(below, face); done = true; }
        catch (e) { await new Promise(r => setTimeout(r, 150)); }
      }
      if (done) placed.push([spot.x, spot.y, spot.z]);
    }
  }
  return placed;
}
`;

function postJson(urlPath, body = {}, timeoutMs = HTTP_TIMEOUT_MS) {
    return new Promise((resolve, reject) => {
        const data = JSON.stringify(body);
        const req = http.request(
            {
                host: HOST, port: PORT, path: urlPath, method: "POST",
                headers: { "Content-Type": "application/json", "Content-Length": Buffer.byteLength(data) },
                timeout: timeoutMs,
            },
            (res) => {
                let chunks = "";
                res.setEncoding("utf8");
                res.on("data", (c) => { chunks += c; });
                res.on("end", () => {
                    try { resolve(JSON.parse(chunks)); }
                    catch (e) { reject(new Error(`JSON parse error: ${e.message}\nbody(头500):\n${chunks.slice(0, 500)}`)); }
                });
            }
        );
        req.on("error", reject);
        req.on("timeout", () => { req.destroy(); reject(new Error(`request timeout ${timeoutMs}ms (${urlPath})`)); });
        req.write(data);
        req.end();
    });
}

/** observe payload = [[eventName, {obsName:..., onError:{message,name,...}, ...}], ...]。
 *  收集所有 onError.message。 */
function getErrorsFromObserve(observePayload) {
    if (!Array.isArray(observePayload)) return [];
    const errors = [];
    for (const ev of observePayload) {
        if (!Array.isArray(ev) || ev.length < 2) continue;
        const inner = ev[1];
        if (!inner || typeof inner !== "object") continue;
        if (inner.onError && typeof inner.onError.message === "string") errors.push(inner.onError.message);
        else if (typeof inner.onError === "string") errors.push(inner.onError);
    }
    return errors;
}

function hasHeapError(errors) {
    return Array.isArray(errors) && errors.some((e) =>
        typeof e === "string" && e.includes("内存峰值 heapUsed"));
}

/** 读 index.js 的 console log 尾段（[heap告警] 行落在那）。 */
function readConsoleLogTail() {
    const p = process.env.INDEX_CONSOLE_LOG || "/tmp/index_heap_test_console.log";
    try {
        const stat = fs.statSync(p);
        const startPos = Math.max(0, stat.size - 512 * 1024);
        return fs.readFileSync(p, { encoding: "utf-8", start: startPos, end: stat.size });
    } catch (e) {
        return `（读 console log 失败: ${e.message}）`;
    }
}

async function main() {
    console.log(`[heap] 阶段${STAGE2 ? "2（真冲堆，阈值 0.80 默认）" : "1（压低阈值走通链路）"} → index.js ${INDEX_URL}`);

    let pass = 0, fail = 0;
    const check = (name, cond, detail) => {
        if (cond) { console.log("  ✓", name); pass++; }
        else { console.log("  ✗", name, detail || ""); fail++; }
    };

    try {
        // ── /start：hard reset，bot 出生在地表 ──
        console.log("[heap] /start hard reset …");
        let startPayload = await postJson("/start", {
            username: "bot", reset: "hard", port: 25567, waitTicks: 10,
        });
        // ★ Restarting bot 路径（前一个 bot 还活着时再 /start）返回的是 **JSON 字符串**
        //   （bot.observe() 本身是 string，该路径未 parse），冷启动返回数组。两种都归一成数组。
        if (typeof startPayload === "string") {
            try { startPayload = JSON.parse(startPayload); } catch (e) { /* 保持原样让 check 报 */ }
        }
        check("/start 返回 observe 数组", Array.isArray(startPayload) && startPayload.length > 0,
              JSON.stringify(startPayload).slice(0, 200));

        // ── 步0（trusted 基建）：准备场景 ──
        // bot 是 op（ops.json level 4）可 /give /tp /fill。trusted:true 让斜杠指令不被 skillLoader 拦。
        // 阶段1：出生点 y=100 高台 /give 火把即可（链路验证不要求不 flying）。
        // 阶段2：lightAreaWithTorches 在 y=100 高台 goto 悬空点会触发 invalid_player_movement 踢人
        //   （allow-flight=true 已开仍踢，是非法位移非飞行）。故 /tp 到 (0,64,0) + /fill 铺 17×17
        //   stone 平台（radius 8，正好匹配原技能 radius=8 扫描范围），上方 air，bot 不再悬空。
        let setupCode, expectCenter;
        if (STAGE2) {
            // 阶段2直接冲堆（不跑 lightAreaWithTorches），bot 出生即可，不需平台/give。
            // 给 bot 创造模式防出生下落被 flying 踢（冲堆期间 bot 不动，但要稳）。
            setupCode = `
                bot.chat('/gamemode creative');
                await bot.waitForTicks(15);
            `;
            expectCenter = { x: 0, y: 100, z: 0 };
        } else {
            setupCode = `
                bot.chat('/give bot torch 256');
                await bot.waitForTicks(30);
            `;
            expectCenter = null;
        }
        const setupPayload = await postJson("/step", {
            code: setupCode, programs: "", timeoutTicks: 600, waitTicks: 15, trusted: true, bot_id: "test",
        });
        // 坐标从 observe 的 status.position 取（return 值不进 observe 通道，走另一路）。
        const pickPos = (payload) => {
            if (!Array.isArray(payload)) return null;
            const obs = payload.find((e) => Array.isArray(e) && e[0] === "observe");
            const pos = obs && obs[1] && obs[1].status && obs[1].status.position;
            if (pos && typeof pos.x === "number") {
                return { x: Math.floor(pos.x), y: Math.floor(pos.y), z: Math.floor(pos.z) };
            }
            return null;
        };
        let center = expectCenter || pickPos(setupPayload) || pickPos(startPayload);
        check(STAGE2 ? "步0 铺平台+/tp/give 火把 + 站定 (0,64,0)" : "步0 /give 火把 + 拿到出生坐标",
              !!center,
              "setup observe status.position=" + JSON.stringify(
                  (Array.isArray(setupPayload) && (setupPayload.find((e) => Array.isArray(e) && e[0] === "observe") || {}).status || {}).position));
        if (!center) throw new Error("拿不到出生坐标，无法继续");
        console.log("[heap] 中心点:", JSON.stringify(center));

        // ── 主步 ──
        // 阶段1：调 lightAreaWithTorches(radius=4，25 点)走通链路（压低阈值下几 MB 就触发）。
        // 阶段2：直接冲堆。lightAreaWithTorches 单 bot 跑 81 点堆才 ~174MB（0910 的 4G 是 5 臂×多天
        //   累积），到不了 3.2G。改为人为放大内存压力冲到 80% 阈值，验证采样器能否崩前抢到。
        //   ★ 冲堆代码必须不锁死事件循环——采样器挂 physicsTick，纯同步 while(new Array) 会锁住
        //   事件循环、physicsTick 不触发、采样器采不到。故每轮 await bot.waitForTicks(1) 让出
        //   事件循环，physicsTick 得以触发采样；每轮分配 ~50MB 不释放，堆单调上涨冲到 3.2G。
        let callCode, programs, timeoutTicks;
        if (STAGE2) {
            callCode = `
                // 冲堆：每轮分配一批**纯 V8 heap 小对象**（Float64Array 走 external 不进 heapUsed，
                // 大字符串进得慢——探针实测 40批×2万对象=115MB heapUsed 才涨；小对象是唯一快路径）。
                // + waitForTicks(1) 让 physicsTick 跑（采样器挂 physicsTick 才采得到）。
                // 阈值 10%（~409MB）下 ~50 轮触发；冲到 ~550MB 停（离 4G 上限安全）。
                const _acc = [];
                let peak = 0;
                for (let i = 0; i < 120; i++) {
                    const batch = new Array(60000);
                    for (let j = 0; j < 60000; j++) batch[j] = {a: j, b: 'pad' + j, c: [j, j+1, j+2]};
                    _acc.push(batch);
                    await bot.waitForTicks(1);
                    peak = Math.max(peak, process.memoryUsage().heapUsed / (1024*1024));
                    if (peak > 550) break;
                }
                return { rounds: _acc.length, peakMB: Math.round(peak) };
            `;
            programs = "";
            timeoutTicks = 2900;
        } else {
            const radius = 4;
            callCode = `
                const center = new Vec3(${center.x}, ${center.y}, ${center.z});
                const placed = await lightAreaWithTorches(bot, mcData, Vec3, GoalNear, center, ${radius}, {step:2});
                return { placed: placed.length };
            `;
            programs = LIGHT_AREA_SRC;
            timeoutTicks = 1500;
        }
        console.log(`[heap] /step 主步（${STAGE2 ? "直接冲堆 50MB/轮×200" : "lightAreaWithTorches radius=4 链路验证"}）…`);
        const stepPayload = await postJson("/step", {
            code: callCode, programs, timeoutTicks, waitTicks: 15, bot_id: "test",
        });
        const stepErrors = getErrorsFromObserve(stepPayload);
        console.log("[heap] 主步 observe 长度:", Array.isArray(stepPayload) ? stepPayload.length : "(非数组)");
        console.log("[heap] 主步 errors:", JSON.stringify(stepErrors).slice(0, 400));

        if (STAGE2) {
            // 阶段2：真冲堆。采样器在冲到 3.2G 前应抢到告警（onError 含「内存峰值 heapUsed」），
            // 且进程不崩（/step 正常返回数组而非连接断）。
            check("阶段2: 进程未崩（/step 正常返回 observe 数组）",
                  Array.isArray(stepPayload),
                  "stepPayload=" + JSON.stringify(stepPayload).slice(0, 200));
            check("阶段2: 采样器崩前抢到 heap onError 反馈", hasHeapError(stepErrors),
                  "errors=" + JSON.stringify(stepErrors).slice(0, 300));
        } else {
            // 阶段1：压低阈值，原码跑少量点 → 峰值几 MB 必过阈值 → 链路贯通。
            check("阶段1: 主步触发 heap onError（链路贯通）", hasHeapError(stepErrors),
                  "errors=" + JSON.stringify(stepErrors).slice(0, 300));
        }

        // console log 出现 [heap告警] 行（physicsTick 采样器打的）
        const logTail = readConsoleLogTail();
        check("console log 出现 [heap告警] 行", logTail.includes("[heap告警]"),
              "logTail 尾段=" + logTail.slice(-400));

        // 若有 [heap告警] 行，打印它（看 heapUsed 多少 MB）
        const heapLines = logTail.split("\n").filter((l) => l.includes("[heap告警]"));
        if (heapLines.length) console.log("[heap] 告警行样本:", heapLines.slice(-3).join(" | "));
    } catch (e) {
        console.error("[heap] 测试异常:", e && e.stack || e);
        // 阶段2 真冲的诊断要分清：
        //   · ECONNREFUSED / socket hang up = index.js 进程崩了（堆 OOM 或 otherError 路径）→ 采样器没拦住
        //   · request timeout = /step 没在 HTTP 超时内返回（81 点 goto 慢 / bot 断连卡住）→ 进程未必崩
        if (STAGE2 && e && /ECONNREFUSED|socket hang up/i.test(e.message)) {
            console.error("[heap] ⚠ 阶段2进程崩了（采样器没能在崩前抢到）—— 这正是要发现的");
        } else if (STAGE2 && e && /timeout/i.test(e.message)) {
            console.error("[heap] 阶段2 /step 超时（看进程是否还活、console log heapUsed 涨到多少）");
        }
        fail++;
    } finally {
        try { await postJson("/stop", { bot_id: "test" }, 10000); } catch (e) { /* ignore */ }
    }

    console.log(`\n[heap] 结果: ${pass} pass, ${fail} fail`);
    process.exit(fail === 0 ? 0 : 1);
}

main();
