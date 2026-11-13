/**
 * 确认两件事：
 *  ① /advancement grant 的正确语法（only 是否报错？换个写法）
 *  ② 让 bot 真正持有工作台（root criterion = 物品栏有 crafting_table），
 *     看 story 树是否被解锁、advancements 包是否推送 story/root 的 criterion 达成。
 *   node test_advancement_natural.js
 */
const mineflayer = require("mineflayer");
const USER = "nat_" + Math.floor(Math.random() * 100000);
const log = (...a) => console.log(`[nat ${USER}]`, ...a);

const bot = mineflayer.createBot({
    host: "localhost", port: 25567, username: "bot", version: "1.21.11",
});

let pktNo = 0;
const storyKeys = [];
bot._client.on("advancements", (data) => {
    pktNo++;
    log(`\n== 包 #${pktNo} reset=${data.reset} mappings=${(data.advancementMapping||[]).length} progress=${(data.progressMapping||[]).length} ==`);
    if (data.advancementMapping) {
        for (const m of data.advancementMapping) {
            if (/^minecraft:story\//.test(m.key)) {
                storyKeys.push(m.key);
                log(`  STORY mapping: ${m.key} hasDisplay=${!!(m.value&&m.value.displayData)}`);
            }
        }
    }
    if (data.progressMapping) {
        for (const p of data.progressMapping) {
            if (/^minecraft:story\//.test(p.key)) {
                log(`  STORY progress: ${p.key} = ${JSON.stringify(p.value)}`);
            }
        }
    }
});

bot.on("messagestr", (message, position, jsonMsg) => {
    if (position !== "system") return;
    const key = jsonMsg && jsonMsg.translate;
    if (!key && !/advancement|grant|recipe|made/i.test(message)) return;
    log(`[sys] translate=${key||"(none)"} text="${message}"`);
});

bot.once("spawn", async () => {
    log("spawn");
    await new Promise(r => setTimeout(r, 1500));
    log("\n--- ① 测试 grant 语法（不带 only，用 from）---");
    bot.chat("/advancement grant @s from minecraft:story/root");
    await new Promise(r => setTimeout(r, 1500));

    log("\n--- ② revoke everything，干净起点 ---");
    bot.chat("/advancement revoke @s everything");
    await new Promise(r => setTimeout(r, 1000));

    log("\n--- ③ give 工作台，让 root criterion 自然达成 ---");
    bot.chat("/give @s minecraft:crafting_table 1");
    await new Promise(r => setTimeout(r, 3000));

    log("\n--- ④ 打开进度界面触发服务端推 story 树（用 /advancement grant 一个 story 子节点）---");
    bot.chat("/advancement grant @s only minecraft:story/mine_stone");
    await new Promise(r => setTimeout(r, 2000));

    log(`\n=== 结束：共 ${pktNo} 包，story mapping 累计 ${storyKeys.length} 个 ===`);
    log(`story keys: ${JSON.stringify([...new Set(storyKeys)])}`);
    process.exit(0);
});
bot.on("kicked", r => log("被踢", r));
bot.on("error", e => { log("错误", e.message); process.exit(1); });
