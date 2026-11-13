/**
 * 成就播报真MC验证（2026-09-14，用户指定 ground truth = world/advancements/<uuid>.json）
 *
 * 验证目标：当 bot 达成 Advancements.txt 里的成就（主线/冒险/农牧业 三棵树），系统是否
 *   正确播报。三方对证：
 *   ① GROUND TRUTH（服务端权威）：world/advancements/<uuid>.json 里对应 key 的 done:true
 *   ② 协议包采集器（advancementPacket.js，系统实际记账/反馈源）：completed/completedKeys
 *   ③ 聊天播报（chat.type.advancement.*）：onSystem 已即弃，但记录它诊断 announce_to_chat
 *
 * 方法：op 账号 'bot'（ops.json level4）连入 → revoke everything 干净起点 → 逐个 grant only
 *   → 等服务端推协议包 + 落盘 ground truth → 三方比对。
 *
 * ★ 懒推送要点（记忆）：story 树在 bot 达成至少一个 story criterion 前不推；grant only 单节点
 *   会触发该节点 progressMapping 推送。每个 grant 后给足时间收包。
 *
 *     node test_advancement_broadcast_gt.js
 */
const mineflayer = require("mineflayer");
const fs = require("fs");
const path = require("path");

const HOST = "localhost";
const PORT = 25567;
const VERSION = "1.21.11";
const USERNAME = "bot";  // op 账号，能跑 /advancement grant

// ground-truth 落盘目录（server.properties level-name=world）
const ADV_DIR = "/path/to/mc_probe/server121/world/advancements";
// op 账号 'bot' 的 uuid（ops.json level4），其存档文件名
const BOT_UUID = "67128b5b-2e6b-3ad1-baa0-1b937b03e5c5";
const GT_FILE = path.join(ADV_DIR, `${BOT_UUID}.json`);

const log = (...a) => console.log(...a);

// 待测成就（主线/冒险/农牧业 代表，含静默 root 与带层级的子节点）
// label: 人类可读 / key: 协议包 key / tree: 树 / expectedName: 采集器应得的显示名 / announce: 是否预期有聊天播报
const TARGETS = [
    { label: "主线-root",  key: "minecraft:story/root",           tree: "story",     expected: "[Minecraft]",            announce: false, note: "announce_to_chat=false 静默根" },
    { label: "主线-L2",    key: "minecraft:story/mine_stone",      tree: "story",     expected: "[Stone Age]",            announce: true,  note: "普通 task" },
    { label: "主线-L4",    key: "minecraft:story/smelt_iron",      tree: "story",     expected: "[Acquire Hardware]",     announce: true,  note: "普通 task" },
    { label: "冒险-root",  key: "minecraft:adventure/root",        tree: "adventure", expected: "[Adventure]",            announce: false, note: "root" },
    { label: "冒险-L2",    key: "minecraft:adventure/kill_a_mob",  tree: "adventure", expected: "[Monster Hunter]",       announce: true,  note: "普通 task" },
    { label: "冒险-L2床",  key: "minecraft:adventure/sleep_in_bed",tree: "adventure", expected: "[Sweet Dreams]",         announce: true,  note: "key 非 story.sleep_in_bed" },
    { label: "农牧-root",  key: "minecraft:husbandry/root",        tree: "husbandry", expected: "[Husbandry]",            announce: false, note: "root" },
    { label: "农牧-L2种",  key: "minecraft:husbandry/plant_seed",  tree: "husbandry", expected: "[A Seedy Place]",        announce: true,  note: "普通 task" },
    { label: "农牧-L2鱼",  key: "minecraft:husbandry/fishy_business",tree: "husbandry",expected: "[Fishy Business]",       announce: true,  note: "fishing_rod_hooked trigger，run0912漏报的那条" },
];

// ── 协议包采集器：复用系统真实实现（advancementPacket.js），保证验的是生产代码路径 ──
// 构造时需要 _client.on（采集器在构造里注册监听）；给个空函数让它注册到一个无人触发的 handler，
// 真实数据由下方 bot._client 监听后手动 ap._handle(data) 喂入（与 test_advancement_packet_smoke.js 同法）。
const AdvancementPacket = require("./lib/observation/advancementPacket.js");
const ap = new AdvancementPacket({ version: VERSION, _client: { on: () => {} } });

// 直接监听底层协议包，喂给真实采集器
const pktLog = [];
let pktCount = 0;

// ── 聊天播报记录 ──
const chatLog = [];

const bot = mineflayer.createBot({ host: HOST, port: PORT, username: USERNAME, version: VERSION });

// ── onSystem 采集器（2026-09-14 成就侧恢复后）：复用真实实现，挂到 bot 上让它自注册
//   messagestr 监听。grant 后检查 bot.openkageSystem.events 是否含对应 advancement 事件，
//   验证「announce=true 子进度 onSystem 能采到、announce=false root 采不到」。
const OnSystem = require("./lib/observation/onSystem.js");
let ons = null;

bot._client.on("advancements", (data) => {
    pktCount++;
    pktLog.push({ n: pktCount, reset: !!data.reset,
        mappings: (data.advancementMapping || []).map(m => m.key).filter(k => /story|adventure|husbandry/.test(k)),
        progress: (data.progressMapping || []).map(p => p.key).filter(k => /story|adventure|husbandry/.test(k)),
    });
    ap._handle(data);
});

bot.on("messagestr", (message, position, jsonMsg) => {
    if (position !== "system") return;
    const key = jsonMsg && jsonMsg.translate;
    // 记录所有 system 聊天（含成就播报与命令回显），诊断用
    chatLog.push({ key: key || null, text: String(message) });
    if (key && key.startsWith("chat.type.advancement")) {
        log(`  [聊天播报] translate=${key} text="${message}"`);
    }
});

const sleep = (ms) => new Promise(r => setTimeout(r, ms));

// 读 ground truth 文件，返回 {key -> done} 映射（只含 story/adventure/husbandry）
function readGroundTruth() {
    try {
        const raw = JSON.parse(fs.readFileSync(GT_FILE, "utf8"));
        const out = {};
        for (const k of Object.keys(raw)) {
            if (/minecraft:(story|adventure|husbandry)\//.test(k)) {
                out[k] = { done: !!raw[k].done, criteriaCount: Object.keys(raw[k].criteria || {}).length };
            }
        }
        return out;
    } catch (e) {
        return { _err: e.message };
    }
}

async function chat(cmd) {
    bot.chat(cmd);
    await sleep(800);  // 命令往返 + 服务端处理
}

bot.once("spawn", async () => {
    log(`== bot spawn（uuid=${BOT_UUID}）==`);
    // spawn 后实例化 onSystem（bot.username 此时确定），它自注册 messagestr 监听。
    ons = new OnSystem(bot);
    await sleep(2000);  // 等初始 recipes 树推送完

    log("\n== ① revoke everything（干净起点）==");
    await chat("/advancement revoke @s everything");
    await sleep(1500);
    // revoke 触发 reset 包，清空采集器状态
    log(`  revoke 后采集器 completed=${JSON.stringify(ap.observe().completed)}`);
    log(`  revoke 后采集器 completedKeys=${JSON.stringify(ap.observe().completedKeys)}`);

    // 强制落盘 ground truth（revoke 后存档可能未及时写盘）
    await chat("/save-all");
    await sleep(2500);

    const results = [];
    for (const t of TARGETS) {
        log(`\n== 测 [${t.label}] ${t.key}（${t.note}）==`);
        const beforeGT = readGroundTruth();
        const beforePkt = ap.observe();
        // 清空 onSystem events：每轮独立测「grant 这一个成就 onSystem 是否收到」
        ons.events = [];
        ons.dropped = 0;

        // grant only：只授这一个节点，触发其 progressMapping 推送
        await chat(`/advancement grant @s only ${t.key}`);
        await sleep(2000);  // 等协议包到达
        await chat("/save-all");
        await sleep(2500);  // 等落盘

        const afterPkt = ap.observe();
        const afterGT = readGroundTruth();

        // 三方判定
        const gtDone = afterGT[t.key] && afterGT[t.key].done === true;
        const pktSeen = afterPkt.completedKeys.includes(t.key);
        const pktNameMatch = afterPkt.completed.includes(t.expected);
        // 聊天播报：本测期间是否收到 chat.type.advancement.* 且文本含 expected
        const chatHit = chatLog.some(c => c.key && c.key.startsWith("chat.type.advancement")
                            && c.text.includes(t.expected.replace(/^\[|\]$/g, "")));
        // onSystem 采集器是否收到该成就的 advancement 事件（2026-09-14 成就侧恢复后）
        const onsHit = ons.events.some(e => e.kind === "advancement" && e.advancement === t.expected);

        const row = {
            ...t,
            gtDone, pktSeen, pktNameMatch, chatHit, onsHit,
            pktNew: afterPkt.completed.filter(n => !beforePkt.completed.includes(n)),
            gtBefore: beforeGT[t.key] || null,
            gtAfter: afterGT[t.key] || null,
        };
        results.push(row);
        log(`  GROUND TRUTH done=${gtDone} (${JSON.stringify(row.gtAfter)})`);
        log(`  协议包采集  completedKeys含=${pktSeen} completed含"${t.expected}"=${pktNameMatch}`);
        log(`  聊天播报    命中=${chatHit}（预期 announce=${t.announce}）`);
        log(`  onSystem    收到=${onsHit}（预期 announce=${t.announce}）`);
        log(`  本步采集器新增=${JSON.stringify(row.pktNew)}`);

        // 收完一个，revoke 回干净，保证下一个独立
        await chat("/advancement revoke @s everything");
        await sleep(1200);
        await chat("/save-all");
        await sleep(2000);
        // revoke 后采集器应清空（reset 包）
        const cleared = ap.observe();
        log(`  revoke回滚后 采集器 completed=${JSON.stringify(cleared.completed)}`);
    }

    log("\n\n========================================");
    log("三方对证汇总（✓=符合预期 ✗=不符）");
    log("========================================");
    log("成就 | GT done | 协议包见Key | 协议包名匹配 | 聊天播报 | onSystem | 预期播报");
    for (const r of results) {
        const gtMark = r.gtDone ? "✓" : "✗";
        const pktKeyMark = r.pktSeen ? "✓" : "✗";
        const pktNameMark = r.pktNameMatch ? "✓" : "✗";
        // 聊天播报符合预期 = (命中 == announce)
        const chatMark = (r.chatHit === r.announce) ? "✓" : `✗(实${r.chatHit})`;
        // onSystem 符合预期 = (收到 == announce)：announce=true 子进度 onSystem 应收到，
        // announce=false root onSystem 应收不到（靠 packet/ground-truth 补）
        const onsMark = (r.onsHit === r.announce) ? "✓" : `✗(实${r.onsHit})`;
        log(`${r.label.padEnd(10)} |   ${gtMark}     |     ${pktKeyMark}       |     ${pktNameMark}       |   ${chatMark}    |   ${onsMark}    | ${r.announce}`);
    }

    // 最终断言（2026-09-14 onSystem 成就侧恢复）：announce=true 的成就 onSystem 必收到，
    // announce=false 的 root onSystem 必收不到。GT/协议包仍全 done（grant 成功的前提）。
    const announceTrue = results.filter(r => r.announce);
    const announceFalse = results.filter(r => !r.announce);
    const onsAllCorrect = announceTrue.every(r => r.onsHit === true)
                       && announceFalse.every(r => r.onsHit === false)
                       && results.every(r => r.gtDone);
    log(`\n★ onSystem 成就侧恢复断言：announce=true(${announceTrue.length}条)全收到=${announceTrue.every(r => r.onsHit)} `
      + `announce=false(${announceFalse.length}条)全不收=${announceFalse.every(r => !r.onsHit)} `
      + `GT全done=${results.every(r => r.gtDone)} → ${onsAllCorrect ? "✓ PASS" : "✗ FAIL"}`);
    if (!onsAllCorrect) process.exit(1);

    log("\n\n明细：");
    for (const r of results) {
        log(`[${r.label}] ${r.key}`);
        log(`  GT: before=${JSON.stringify(r.gtBefore)} after=${JSON.stringify(r.gtAfter)} -> done=${r.gtDone}`);
        log(`  协议包: key在=${r.pktSeen} 名"${r.expected}"在=${r.pktNameMatch} 本步新增=${JSON.stringify(r.pktNew)}`);
        log(`  聊天: 命中=${r.chatHit} 预期=${r.announce}`);
        log(`  onSystem: 收到=${r.onsHit}（events=${JSON.stringify(ons.events.filter(e=>e.kind==="advancement").map(e=>e.advancement))}）`);
    }

    log(`\n总计收到 ${pktCount} 个协议包，${chatLog.length} 条 system 聊天。`);
    process.exit(0);
});

bot.on("kicked", r => { log("被踢:", r); process.exit(1); });
bot.on("error", e => { log("错误:", e.message); process.exit(1); });
bot.on("end", () => { log("连接结束"); process.exit(0); });
