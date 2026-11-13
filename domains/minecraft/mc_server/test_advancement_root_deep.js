/**
 * 深入：打印 spawn 时首个 advancements 包完整内容（看 root criterion 结构），
 * 并确认 grant root 后服务端到底推不推包。同时收所有 systemChat 看 grant 回显。
 *   node test_advancement_root_deep.js
 */
const mineflayer = require("mineflayer");
const USER = "deep_" + Math.floor(Math.random() * 100000);
const log = (...a) => console.log(`[deep ${USER}]`, ...a);

const bot = mineflayer.createBot({
    host: "localhost", port: 25567, username: USER, version: "1.21.11",
});

let pktNo = 0;
bot._client.on("advancements", (data) => {
    pktNo++;
    log(`\n========== advancements 包 #${pktNo} ==========`);
    log(`reset=${data.reset} mappings=${(data.advancementMapping||[]).length} progress=${(data.progressMapping||[]).length}`);
    // 打印所有 mapping 的 key（看 root 在不在）
    if (data.advancementMapping) {
        for (const m of data.advancementMapping) {
            log(`  mapping key="${m.key}" parentId=${JSON.stringify(m.value && m.value.parentId)} hasDisplay=${!!(m.value && m.value.displayData)}`);
        }
    }
    // 打印所有 progress（criterion 达成状态）
    if (data.progressMapping) {
        for (const p of data.progressMapping) {
            log(`  progress key="${p.key}" criteria=${JSON.stringify(p.value)}`);
        }
    }
});

bot.on("messagestr", (message, position, jsonMsg) => {
    if (position !== "system") return;
    const key = jsonMsg && jsonMsg.translate;
    if (!key && !/advancement|grant|revoke/i.test(message)) return;
    log(`[systemChat] translate=${key||"(none)"} text="${message}"`);
});

bot.once("spawn", async () => {
    log("spawn，等 2s 收首批包...");
    await new Promise(r => setTimeout(r, 2000));
    log(`\n--- 已收到 ${pktNo} 个包。现在 grant root ---`);
    bot.chat("/advancement grant @s only minecraft:story/root");
    await new Promise(r => setTimeout(r, 2500));
    log(`\n--- grant 后共 ${pktNo} 个包。现在 give 工作台（看 criterion 自然达成）---`);
    bot.chat("/advancement revoke @s everything");
    await new Promise(r => setTimeout(r, 500));
    bot.chat("/give @s minecraft:crafting_table 1");
    await new Promise(r => setTimeout(r, 3000));
    log(`\n=== 结束：总共 ${pktNo} 个 advancements 包 ===`);
    process.exit(0);
});
bot.on("kicked", r => log("被踢", r));
bot.on("error", e => { log("错误", e.message); process.exit(1); });
