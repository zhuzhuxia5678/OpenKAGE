async function getItemFromChest(bot, chestPosition, itemsToGet) {
    // ★ **Vec3 校验必须抛、不能 chat+return**（同 placeItem 那条原则）：静默 return 会让 Agent 的
    //   `await getItemFromChest(...)` 拿到 undefined、误以为取物已发生，继续往下用根本没取到的东西。
    if (!(chestPosition instanceof Vec3)) {
        throw new Error("chestPosition for getItemFromChest must be a Vec3");
    }
    await moveToChest(bot, chestPosition);
    const chestBlock = bot.blockAt(chestPosition);
    const chest = await bot.openContainer(chestBlock);
    // ★ **轮询等待按倍速换算**（2026-08-25 修，对齐 smeltItem/waitUntil 范式）：窗口包（set_slot/
    //   window_items）的同步延迟是**墙钟**延迟，与倍速无关；但 waitForTicks 等的是游戏刻，倍速越高
    //   相同 tick 数等到的墙钟越短。固定 tick 数会"2x 够、3x 偏紧误报、1x 浪费步预算"。换算公式
    //   `墙钟秒 × 20 × rate`（见 index.js:72 / waitUntil.js:109），取 0.5s 余量 → `10 × rate` tick：
    //   1x=10、2x=20、2.5x=25、3x=30，恒等 0.5s 墙钟。`_rate` 取法同 smeltItem/killMob 等全项目惯用。
    const _rate = Math.max(1, Number(bot.openkageTickRate) || 1);
    const _syncTicks = 10 * _rate; // 0.5s 墙钟的窗口包同步余量
    // ★ **逐项如实报实得数量**（同 mineBlock 的 before/after 原则，见 memory openkage_primitive_intent_feedback）：
    //   mineflayer 的 `chest.withdraw(type, null, count)` 在箱子里**数量不足时不报错、而是只取走有的那部分**
    //   （transfer 走 `Math.min(movedItems, count)`，见 node_modules/.../inventory.js:328-334）——
    //   即「要 5 个、箱子里只有 2 个」会**静默成功取走 2 个**。旧实现 withdraw 不抛错就什么都不报，
    //   Agent 只看到步级"success"，无从区分「取够了」与「只取到一半」，于是据假足数往下做。
    //   用 chest.count 的前后差算实得数，部分取走也要说清。
    //   ★ **用 chest.count 而非 bot.inventory.count**（2026-08-25 修，同 smeltItem 根因，见
    //     memory openkage_usechest_deposit_zero_misreport）：容器窗口打开后 bot.inventory 是另一个
    //     独立 Window 对象，与 chest 的 slots 靠服务器包异步同步，轮询 10 tick 内读不到增减 →
    //     gained/deposited 恒 ≤0 → 100% 误报"一件都没拿到/存进去"（两次30夜长跑实测 deposit 全报 0，
    //     但物品真实到账，checkItem 读箱子数字与报 0 的数字精确对应）。withdraw/deposit 内部 transfer
    //     操作的就是 chest.slots（inventory.js:269），chest.count 数同一份 countRange(inventoryStart,
    //     inventoryEnd) 覆盖窗口视图背包区，立即可见，无需等跨窗同步。
    const results = [];
    for (const name in itemsToGet) {
        const itemByName = mcData.itemsByName[name];
        if (!itemByName) {
            bot.chat(`No item named ${name}`);
            results.push(`${name}: 不存在的物品名`);
            continue;
        }

        const item = chest.findContainerItem(itemByName.id);
        if (!item) {
            bot.chat(`I don't see ${name} in this chest`);
            results.push(`${name}: 箱子里没有`);
            continue;
        }
        const asked = itemsToGet[name];
        const before = chest.count(itemByName.id, null);
        try {
            await chest.withdraw(item.type, null, asked);
        } catch (err) {
            // ★ **catch 不能一律说 "Not enough"**：withdraw 抛错的真因只有两种 ——
            //   ① `Unable to withdraw, Bot inventory is full.`（背包满，根本放不下，inventory.js:354）
            //   ② `destination full`（同 inventory.js:323，箱→包时包满）
            //   「箱子里数量不够」**不在这条抛错路径上**（那是静默部分取走）。旧文案"Not enough in chest"
            //   把"背包满"误报成"箱子不够"，会把 Agent 推向"再去采/找箱子"的错误方向，白烧步数
            //   （同 craftHelper「料够却报缺料」那类误导病根）。
            const msg = (err && err.message) || String(err);
            let why;
            if (/inventory is full|destination full/i.test(msg)) {
                why = `背包满了放不下（${msg}）—— 先 bot.tossStack 腾位或存点东西进箱子再取`;
            } else {
                why = `取物异常：${msg}（多半是窗口同步问题，重试本步通常可成）`;
            }
            bot.chat(`取 ${name} 失败：${why}`);
            results.push(`${name}: 取物失败（${why}）`);
            continue;
        }
        // 容器窗口读数立即可见（chest.count 同 transfer 操作的 slots，见上文），但仍轮询几拍
        // 兜极端同步抖动（同 smeltItem/craftItem 的缓存坑），等一拍再数，避免少报。
        // ★ 轮询长度 = `_syncTicks`（按倍速换算的 0.5s 墙钟余量，见上文），非固定值——固定 tick 数
        //   会随倍速漂移（3x 下 25 tick 仅 0.4s 偏紧误报）。
        // ★ 轮询里读到的值直接用作 after，不另外再 count 一次：库存只会增，多读一次可能拿到
        //   更靠后的值（轮询在第 N 次确认 >before 后 break，若再 count 第 N+1 次算 gained，
        //   会把还没体现的增量算进来），与"这一刻实得"对不上。
        let after = before;
        for (let _t = 0; _t < _syncTicks; _t++) {
            after = chest.count(itemByName.id, null);
            if (after > before) break;
            await bot.waitForTicks(1);
        }
        const gained = after - before;
        if (gained <= 0) {
            // withdraw 没抛错但库存没涨 —— 窗口同步延迟或取了 0 个。如实说，别让 Agent 以为取到了。
            bot.chat(`取 ${name}: 一件都没拿到（withdraw 未报错但库存未增，窗口同步延迟，重试本步通常可成）`);
            results.push(`${name}: 取到 0/${asked}`);
        } else if (gained < asked) {
            bot.chat(`取 ${name}: 实得 ${gained}/${asked}（箱子里只有这么多）`);
            results.push(`${name}: 实得 ${gained}/${asked}`);
        } else {
            bot.chat(`取 ${name}: 实得 ${gained}/${asked}`);
            results.push(`${name}: 实得 ${gained}/${asked}`);
        }
    }
    await closeChest(bot, chestBlock);
    // 汇总一句：Agent 若在循环里取多种东西，一条总账比 N 条单行更好读
    bot.chat(`getItemFromChest 完成：${results.join("；")}`);
}

async function depositItemIntoChest(bot, chestPosition, itemsToDeposit) {
    // return if chestPosition is not Vec3
    if (!(chestPosition instanceof Vec3)) {
        throw new Error(
            "chestPosition for depositItemIntoChest must be a Vec3"
        );
    }
    await moveToChest(bot, chestPosition);
    const chestBlock = bot.blockAt(chestPosition);
    const chest = await bot.openContainer(chestBlock);
    // ★ **轮询等待按倍速换算**（同 getItemFromChest，见上文）：`_syncTicks = 10 * rate`（0.5s 墙钟余量），
    //   非固定值，避免高倍速下固定 tick 数偏紧误报"存入 0"。`_rate` 取法同全项目惯用。
    const _rate = Math.max(1, Number(bot.openkageTickRate) || 1);
    const _syncTicks = 10 * _rate; // 0.5s 墙钟的窗口包同步余量
    // ★ **逐项如实报实存数量**（同 getItemFromChest，根因见上）：mineflayer 的 `chest.deposit`
    //   在箱子满 / 库存不够时行为不对称 —— 库存不够会**静默只存有的那部分**（transfer 取 min），
    //   箱子满则抛 `destination full`。旧实现 catch 一律说 "Not enough in inventory" 既漏报"部分存入"
    //   又误报"箱子满"为"库存不够"。用 chest.count 的前后差算实存数（同 getItemFromChest，
    //   ★ bot.inventory.count 跨窗延迟会 100% 误报"存入 0"，见上文 2026-08-25 修），部分存入也要说清。
    const results = [];
    for (const name in itemsToDeposit) {
        const itemByName = mcData.itemsByName[name];
        if (!itemByName) {
            bot.chat(`No item named ${name}`);
            results.push(`${name}: 不存在的物品名`);
            continue;
        }
        const item = bot.inventory.findInventoryItem(itemByName.id);
        if (!item) {
            bot.chat(`No ${name} in inventory`);
            results.push(`${name}: 库存里没有`);
            continue;
        }
        const asked = itemsToDeposit[name];
        const before = chest.count(itemByName.id, null);
        try {
            await chest.deposit(item.type, null, asked);
        } catch (err) {
            // ★ withdraw 那条同款区分：deposit 抛错的真因是 `destination full`（箱子满），
            //   不是"库存不够"（库存不够走静默部分存入）。旧文案会误导 Agent 去采根本不缺的东西。
            const msg = (err && err.message) || String(err);
            let why;
            if (/destination full/i.test(msg)) {
                why = `箱子满了放不下 —— 换个箱子或先取走些箱子里的东西再存`;
            } else {
                why = `存物异常：${msg}（多半是窗口同步问题，重试本步通常可成）`;
            }
            bot.chat(`存 ${name} 失败：${why}`);
            results.push(`${name}: 存物失败（${why}）`);
            continue;
        }
        // ★ 同 getItemFromChest：轮询里读到的值直接用作 after，不再多 count 一次（见上）。
        //    轮询长度 = `_syncTicks`（按倍速换算的 0.5s 墙钟余量，见上文），非固定值。
        let after = before;
        for (let _t = 0; _t < _syncTicks; _t++) {
            after = chest.count(itemByName.id, null);
            if (after < before) break;
            await bot.waitForTicks(1);
        }
        const deposited = before - after;
        if (deposited <= 0) {
            bot.chat(`存 ${name}: 一件都没存进去（deposit 未报错但库存未减，窗口同步延迟，重试本步通常可成）`);
            results.push(`${name}: 存入 0/${asked}`);
        } else if (deposited < asked) {
            bot.chat(`存 ${name}: 实存 ${deposited}/${asked}（库存里只有这么多 / 箱子空间不够）`);
            results.push(`${name}: 实存 ${deposited}/${asked}`);
        } else {
            bot.chat(`存 ${name}: 实存 ${deposited}/${asked}`);
            results.push(`${name}: 实存 ${deposited}/${asked}`);
        }
    }
    await closeChest(bot, chestBlock);
    bot.chat(`depositItemIntoChest 完成：${results.join("；")}`);
}

async function checkItemInsideChest(bot, chestPosition) {
    // return if chestPosition is not Vec3
    if (!(chestPosition instanceof Vec3)) {
        throw new Error(
            "chestPosition for checkItemInsideChest must be a Vec3"
        );
    }
    await moveToChest(bot, chestPosition);
    const chestBlock = bot.blockAt(chestPosition);
    const chest = await bot.openContainer(chestBlock);
    // ★ **本步就要把箱子里有什么报给 Agent**（同 mineBlock「成功也必须播报实得」的原则，
    //   见 memory openkage_primitive_intent_feedback）：旧实现 openContainer 后直接 closeChest，
    //   箱子内容只通过 `emit("closeChest", itemNames, position)` 进了 chests.js 观察系统 ——
    //   而那份观察是**下一步**才生成的。本步 Agent 拿到的是步级"success"，看不到箱子里任何东西，
    //   要等下一轮 observe 的「附近箱子」字段更新才知道，白白多烧一步、还容易忘了自己查过哪个箱子。
    //   这里直接读 containerItems() 当场播报：进可立即决定取什么，退也留了观察侧的记录。
    const items = chest.containerItems();
    // 同名叠堆求和：agg 既用于播报，也原样 emit 给 chests.js 观察系统（与 listItemsInChest 的
    // emit 形态逐字一致，chests.js 据此记「附近箱子」内容）—— 一份读数两用，不重新打开箱子。
    const agg = {};
    for (const it of items) {
        agg[it.name] = (agg[it.name] || 0) + it.count;
    }
    bot.emit("closeChest", agg, chestBlock.position);
    try {
        await chest.close();
    } catch (err) {
        // 关窗失败兜底（同 closeChest 的容错）：确保窗口不残留
        try { await bot.closeWindow(chestBlock); } catch (e) { /* ignore */ }
    }
    const summary = items.length === 0
        ? "空的"
        : Object.entries(agg).map(([n, c]) => `${n}×${c}`).join(", ");
    bot.chat(
        `箱子 (${Math.round(chestPosition.x)}, ${Math.round(chestPosition.y)}, ` +
            `${Math.round(chestPosition.z)}) 里：${summary}。` +
            (items.length === 0
                ? ""
                : ` 要取用 getItemFromChest(同坐标, {名:数})`)
    );
}

async function moveToChest(bot, chestPosition) {
    if (!(chestPosition instanceof Vec3)) {
        throw new Error(
            "chestPosition for depositItemIntoChest must be a Vec3"
        );
    }
    if (chestPosition.distanceTo(bot.entity.position) > 32) {
        bot.chat(
            `/tp ${chestPosition.x} ${chestPosition.y} ${chestPosition.z}`
        );
        await bot.waitForTicks(20);
    }
    const chestBlock = bot.blockAt(chestPosition);
    if (chestBlock.name !== "chest") {
        bot.emit("removeChest", chestPosition);
        throw new Error(
            `No chest at ${chestPosition}, it is ${chestBlock.name}`
        );
    }
    await bot.pathfinder.goto(
        new GoalLookAtBlock(chestBlock.position, bot.world, {})
    );
    return chestBlock;
}

async function listItemsInChest(bot, chestBlock) {
    const chest = await bot.openContainer(chestBlock);
    const items = chest.containerItems();
    if (items.length > 0) {
        const itemNames = items.reduce((acc, obj) => {
            if (acc[obj.name]) {
                acc[obj.name] += obj.count;
            } else {
                acc[obj.name] = obj.count;
            }
            return acc;
        }, {});
        bot.emit("closeChest", itemNames, chestBlock.position);
    } else {
        bot.emit("closeChest", {}, chestBlock.position);
    }
    return chest;
}

async function closeChest(bot, chestBlock) {
    try {
        const chest = await listItemsInChest(bot, chestBlock);
        await chest.close();
    } catch (err) {
        await bot.closeWindow(chestBlock);
    }
}

function itemByName(items, name) {
    for (let i = 0; i < items.length; ++i) {
        const item = items[i];
        if (item && item.name === name) return item;
    }
    return null;
}
