/**
 * 实测：1.21.11 服务端下，能否读到 story/root（[Minecraft] 工作台根进度）。
 *
 *   node test_advancement_root_probe.js
 *
 * 三件事：
 *  ① 发 /advancement test @s minecraft:story/root —— 看 test 子命令在 1.21.11 是否存在、回显什么。
 *     （lang 文件 + 多份命令文档都说只有 grant/revoke，这里实跑确认）
 *  ② 监听底层协议包 packet_advancements —— 看 progressMapping 里 story/root 的 criterion 状态。
 *     这是 bot 进度界面的数据源，root 应在内（announce_to_chat=false 不影响协议包）。
 *  ③ /give @s minecraft:crafting_table 后再查 —— 看达成 root 时协议包是否推送更新。
 *
 * 连真实服务端（localhost:25567），不依赖 index.js 的 HTTP 层。
 */
const mineflayer = require("mineflayer");

const HOST = "localhost";
const PORT = 25567;
const USER = "adv_probe_" + Math.floor(Math.random() * 100000);

const log = (...a) => console.log(`[probe ${USER}]`, ...a);

const bot = mineflayer.createBot({
    host: HOST,
    port: PORT,
    username: USER,
    version: "1.21.11",
});

// 收集所有 messagestr 系统消息（命令回显在这里），打印 translate + 原文
bot.on("messagestr", (message, position, jsonMsg) => {
    if (position !== "system") return;
    const key = jsonMsg && jsonMsg.translate;
    // 只打 commands.* 和 advancement 相关，过滤噪音
    if (!key && !/advancement/i.test(message)) return;
    log(`[systemChat] pos=${position} translate=${key || "(none)"} text="${message}"`);
});

// ① 监听底层协议包 advancements（服务端→客户端）
let advPacketCount = 0;
let rootSeenInPacket = null;
bot.on("clientbound_packet_advancements" in bot._client._events ? "clientbound_packet_advancements" : "advancements", (data) => {
    advPacketCount++;
    const pm = data && data.progressMapping;
    log(`[packet_advancements #${advPacketCount}] reset=${data && data.reset} ` +
        `mappings=${(data && data.advancementMapping && data.advancementMapping.length) || 0} ` +
        `progress=${(pm && pm.length) || 0}`);
    if (pm) {
        for (const entry of pm) {
            // story/root 的完整 id 是 minecraft:story/root
            if (/story\/path/to/.test(entry.key)) {
                rootSeenInPacket = entry;
                log(`  >> ROOT progress: key=${entry.key} criteria=${JSON.stringify(entry.value)}`);
            }
        }
    }
});

bot.once("spawn", async () => {
    log("已 spawn，版本", bot.version);
    // 给点时间让首批 advancements 包到达（服务端 spawn 时会发一次 reset=true 的全量包）
    await wait(1500);
    log(`spawn 后 1.5s：共收到 ${advPacketCount} 个 advancements 包，root 在包内=${!!rootSeenInPacket}`);

    // ② 发 /advancement test —— 验证子命令是否存在
    log("--- 步骤①：发 /advancement test @s minecraft:story/root ---");
    bot.chat("/advancement test @s minecraft:story/root");
    await wait(1000);

    // ③ 先 revoke 再 give 工作台，看 root 是否通过协议包达成
    log("--- 步骤②：revoke everything + give crafting_table ---");
    bot.chat("/advancement revoke @s everything");
    await wait(500);
    bot.chat("/give @s minecraft:crafting_table 1");
    await wait(2000);
    log(`give 后：advancements 包累计=${advPacketCount}，root 在包内=${!!rootSeenInPacket}`);
    if (rootSeenInPacket) {
        log(`  ROOT 最新状态：${JSON.stringify(rootSeenInPacket.value)}`);
    }

    // ④ 直接 grant root，对比协议包（grant 一定能让 root 达成，验证我们 parse 对了字段）
    log("--- 步骤③：/advancement grant @s only minecraft:story/root ---");
    bot.chat("/advancement grant @s only minecraft:story/root");
    await wait(1500);
    log(`grant 后：advancements 包累计=${advPacketCount}，root 在包内=${!!rootSeenInPacket}`);
    if (rootSeenInPacket) {
        log(`  ROOT grant 后状态：${JSON.stringify(rootSeenInPacket.value)}`);
    }

    log("=== 测试结束 ===");
    log(`总结：test子命令回显见上方 systemChat；协议包共${advPacketCount}个；root曾被协议包提及=${!!rootSeenInPacket}`);
    process.exit(0);
});

bot.on("kicked", (r) => log("被踢：", r));
bot.on("error", (e) => { log("错误：", e.message); process.exit(1); });
bot.on("end", () => log("连接结束"));

function wait(ms) { return new Promise((r) => setTimeout(r, ms)); }
