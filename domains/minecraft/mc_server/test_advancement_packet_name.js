/**
 * 精简探测：连服务端，dump 所有名字含 advancement 的 packet 事件，并直接监听几个候选名。
 *   node test_advancement_packet_name.js
 */
const mineflayer = require("mineflayer");
const USER = "pkt_" + Math.floor(Math.random() * 100000);
const log = (...a) => console.log(`[pkt ${USER}]`, ...a);

const bot = mineflayer.createBot({
    host: "localhost", port: 25567, username: USER, version: "1.21.11",
});

// 1. 直接监听候选事件名
["packet_advancements", "advancements", "clientbound_packet_advancements"].forEach((name) => {
    bot._client.on(name, (data) => {
        log(`★ 命中事件 "${name}"：reset=${data && data.reset} ` +
            `mappings=${(data && data.advancementMapping && data.advancementMapping.length) || 0} ` +
            `progress=${(data && data.progressMapping && data.progressMapping.length) || 0}`);
        // 找 root
        const pm = data && data.progressMapping;
        if (pm) {
            for (const e of pm) {
                if (/story\/path/to/.test(e.key)) log(`   ROOT: ${JSON.stringify(e.value)}`);
            }
        }
    });
});

// 2. 在所有 packet 上加一个透传监听，看 advancements 包到底叫什么名
//    minecraft-protocol 的 deserialize 把包名 emit 成 packet_<name>
bot._client.on("packet", (data, packetMeta) => {
    const n = packetMeta && packetMeta.name;
    if (n && /advancement/.test(n)) log(`◎ packet 透传：name="${n}"`);
});

bot.once("spawn", async () => {
    log("spawn，version", bot.version);
    await new Promise(r => setTimeout(r, 2000));
    log("--- 给工作台，看是否触发 advancements 包 ---");
    bot.chat("/advancement revoke @s everything");
    await new Promise(r => setTimeout(r, 500));
    bot.chat("/give @s minecraft:crafting_table 1");
    await new Promise(r => setTimeout(r, 2500));
    log("--- 再直接 grant root ---");
    bot.chat("/advancement grant @s only minecraft:story/root");
    await new Promise(r => setTimeout(r, 2000));
    log("=== 结束 ===");
    process.exit(0);
});
bot.on("kicked", r => log("被踢", r));
bot.on("error", e => { log("错误", e.message); process.exit(1); });
