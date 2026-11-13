/**
 * 端到端 live 验证：用真实 mineflayer bot 连 1.21.11 服务端，实例化 AdvancementPacket 采集器，
 * 验证它能采到 onSystem 采不到的 story/root=[Minecraft]（announce_to_chat=false 的静默进度）。
 *
 *   node test_advancement_packet_live.js
 *
 * 验证逻辑：
 *  ① spawn 时只推 recipes 树 -> observe() completed 空、storyTreeSeen=false（懒推送，预期）
 *  ② grant story 树触发推送 -> observe() completed 应含 [Minecraft]（root，协议包能采到）
 *  ③ 对比：此时 onSystem 被动播报**不**会为 root 单独发 chat.type.advancement 播报
 *     （root 的 announce_to_chat=false）——这正是佐证支路的价值。
 *
 * 用 op 用户名 "bot"（ops.json level 4），否则 /advancement grant 被拒。
 */
const mineflayer = require("mineflayer");
const path = require("path");
const AdvancementPacket = require(path.join(__dirname, "lib", "observation", "advancementPacket.js"));

const log = (...a) => console.log(`[live]`, ...a);

const bot = mineflayer.createBot({
    host: "localhost", port: 25567, username: "bot", version: "1.21.11",
});

// 实例化采集器（它会自己注册 bot._client.on("advancements")）
const ap = new AdvancementPacket(bot);

// 监听 onSystem 同源的聊天播报，对比 root 是否被播报
const chatAdvs = [];
bot.on("messagestr", (message, position, jsonMsg) => {
    if (position !== "system") return;
    const key = jsonMsg && jsonMsg.translate;
    if (key && key.startsWith("chat.type.advancement.")) {
        chatAdvs.push(message);
    }
});

bot.once("spawn", async () => {
    log("spawn，version", bot.version);
    await new Promise((r) => setTimeout(r, 1500));
    let snap = ap.observe();
    log(`① spawn 后：completed=${JSON.stringify(snap.completed)} storyTreeSeen=${snap.storyTreeSeen}`);
    console.assert(snap.storyTreeSeen === false, "spawn 时不应见过 story 树（懒推送）");

    log("② grant story 树（from root，触发服务端推送 story 树含 root）...");
    bot.chat("/advancement grant @s from minecraft:story/root");
    await new Promise((r) => setTimeout(r, 2500));

    snap = ap.observe();
    log(`② grant 后：completed=${JSON.stringify(snap.completed)} storyTreeSeen=${snap.storyTreeSeen}`);
    console.assert(snap.storyTreeSeen === true, "grant story 后应见过 story 树");
    console.assert(snap.completed.includes("[Minecraft]"),
        `★ 应采到 [Minecraft]（root），实际 ${JSON.stringify(snap.completed)}`);

    log(`③ 同期 onSystem 聊天播报的成就数=${chatAdvs.length}`);
    // root 不在该列表里（announce_to_chat=false）——但 grant from root 会播报 root 的所有子进度，
    // 故 chatAdvs 非空。关键是 [Minecraft] 不在其中。
    const rootInChat = chatAdvs.some((m) => /\[Minecraft\]/.test(m));
    log(`   root [Minecraft] 是否在聊天播报里=${rootInChat}（预期 false：announce_to_chat=false）`);

    log("\n=== 结论 ===");
    log(`协议包 completed 含 [Minecraft]: ${snap.completed.includes("[Minecraft]") ? "是 ✓" : "否 ✗"}`);
    log(`onSystem 聊天播报含 [Minecraft]: ${rootInChat ? "是" : "否"}（佐证支路补采成功）`);
    process.exit(snap.completed.includes("[Minecraft]") ? 0 : 1);
});
bot.on("kicked", (r) => { log("被踢", r); process.exit(1); });
bot.on("error", (e) => { log("错误", e.message); process.exit(1); });
