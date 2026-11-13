async function givePlacedItemBack(bot, name, position) {
    // ★ 参数校验：本函数内部真的发 `/setblock ... air destroy` 删方块。坏输入有两种后果，
    //   都极难查：
    //   ① 传字符串而非数组（`givePlacedItemBack(bot,'crafting_table',pos)`，签名看起来就像单个）
    //      → `name.length` 变成字符串长度，循环多次、`position[i]` 全 undefined，
    //      在 `Math.floor(position.x)` 抛 `Cannot read properties of undefined` ——
    //      报错完全指不出真正的问题（"这两个参数要传数组"）。
    //   ② **两数组长度不匹配** → 前几个正常拆回，轮到 undefined 时抛错，
    //      而 `/gamerule block_drops false` **已经打开、恢复语句却被异常跳过** ——
    //      世界被留在「挖方块不掉落」状态，之后所有采集静默不掉东西。
    //      这是能**污染整条轨迹**的脏状态，且症状（采集恒 gained 0）离病因极远。
    //   故：先校验再动 gamerule；并把恢复放进 finally，任何路径都不留脏状态。
    if (!Array.isArray(name) || !Array.isArray(position)) {
        throw new Error(
            "givePlacedItemBack(bot, names, positions): both names and positions must be ARRAYS, " +
                "corresponding one-to-one — e.g. givePlacedItemBack(bot, ['crafting_table'], [pos])"
        );
    }
    if (name.length !== position.length) {
        throw new Error(
            `givePlacedItemBack: names has ${name.length} entries but positions has ` +
                `${position.length} — they must correspond one-to-one.`
        );
    }
    if (name.length === 0) {
        // 空列表不是错误，但要**明说什么都没做**（原来静默返回，LLM 无法区分"拆完了"与"没拆"）
        bot.chat("givePlacedItemBack: the list was empty, so nothing was taken back.");
        return;
    }
    for (let i = 0; i < name.length; i++) {
        if (typeof name[i] !== "string") {
            throw new Error(
                `givePlacedItemBack: names[${i}] must be a block-name string, got ${typeof name[i]}`
            );
        }
        const q = position[i];
        if (!q || !Number.isFinite(q.x) || !Number.isFinite(q.y) || !Number.isFinite(q.z)) {
            throw new Error(
                `givePlacedItemBack: positions[${i}] must be a Vec3 with finite x/y/z ` +
                    `(got ${JSON.stringify(q)})`
            );
        }
    }
    // MC 1.21.11 起 gamerule 全为 snake_case：doTileDrops → block_drops
    // （旧名被服务器拒为 `Incorrect argument for command`，即这层防掉落保护此前形同虚设）。
    await bot.chat("/gamerule block_drops false");
    // iterate name and position
    const history = [];
    try {
        for (let i = 0; i < name.length; i++) {
            await givePlacedItemBackSingle(bot, name[i], position[i]);
        }
    } finally {
        // 必须无条件恢复：见上面 ② 的说明。
        await bot.chat("/gamerule block_drops true");
    }

    async function givePlacedItemBackSingle(bot, name, position) {
        // ★ 用 `@s`（指令发送者自己）而不是写死的用户名 "bot"：每个副脑现在有自己的名字
        //   （arm key，见 index.js 的 username），写死 "bot" 会让这条指令**静默指向不存在的
        //   玩家** —— 方块被 /setblock 删掉了、但物品没还回库存，净效果是「拆家拆没了」。
        bot.chat(`/give @s ${name} 1`);
        const x = Math.floor(position.x);
        const y = Math.floor(position.y);
        const z = Math.floor(position.z);
        // loop through 125 blocks around the block
        const size = 3;
        for (let dx = -size; dx <= size; dx++) {
            for (let dy = -size; dy <= size; dy++) {
                for (let dz = -size; dz <= size; dz++) {
                    const block = bot.blockAt(new Vec3(x + dx, y + dy, z + dz));
                    if (
                        block?.name === name &&
                        !history.includes(block.position)
                    ) {
                        await bot.chat(
                            `/setblock ${x + dx} ${y + dy} ${
                                z + dz
                            } air destroy`
                        );
                        history.push(block.position);
                        await bot.waitForTicks(20);
                        return;
                    }
                }
            }
        }
    }
}
