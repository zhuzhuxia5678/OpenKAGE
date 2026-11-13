async function craftItem(bot, name, count = 1) {
    // return if name is not string
    if (typeof name !== "string") {
        throw new Error("name for craftItem must be a string");
    }
    // return if count is not number
    if (typeof count !== "number") {
        throw new Error("count for craftItem must be a number");
    }
    const itemByName = mcData.itemsByName[name];
    if (!itemByName) {
        throw new Error(`No item named ${name}`);
    }
    const craftingTable = bot.findBlock({
        matching: mcData.blocksByName.crafting_table.id,
        maxDistance: 32,
    });
    if (!craftingTable) {
        bot.chat("Craft without a crafting table");
    } else {
        await bot.pathfinder.goto(
            new GoalLookAtBlock(craftingTable.position, bot.world)
        );
    }

    // ★ **库存读数容错**（见 memory openkage_arm_graph_bug_revisit_20260821 的 #10）：
    //   `bot.recipesFor` 不是纯静态查询 —— 它内部 `requirementsMetForRecipe`（craft.js:224）
    //   会读 `bot.inventory.count()` 判库存够不够（craft.js:233）。而 mineflayer 的库存是
    //   客户端缓存、靠服务器的 set_slot/window_items 包刷新，**有 1~几 tick 延迟**。
    //   dave 第2天反复「recipesFor 返回 0 / craft 假成功但库存无入账」根因就在此：刚拾取/死亡/
    //   开过窗后库存缓存还没同步，`count` 返 0 → `requirementsMetForRecipe` false → recipesFor 返空
    //   → 走 else 报「做不了」，可实际配方存在、料也在；第3天库存稳定即成功（同代码）。
    //   这与 #2 equip 竞态、#3 smeltItem「报找不到但实际烧成了」是**同一根因**。
    //
    //   修法（与 smeltItem 的 `_hasItem` 同构）：查配方前先等库存同步——若第一次 recipesFor 返空，
    //   别急着判「无配方」，等几 tick 让缓存刷新再查，重试几次。仍空才算真没有。
    //   注意区分两种「空」：recipesFor 返空可能是「库存没同步」也可能是「真无配方」。
    //   recipesAll（craft.js:214）**不读库存**（只判 requiresTable），拿它当「配方是否客观存在」
    //   的权威——recipesAll 有而 recipesFor 无，就是库存没同步的铁证，重试；两者都无才是真无配方。
    const _RECIPE_WAIT_TICKS = 10;
    let baseRecipe = null;
    for (let _t = 0; _t < _RECIPE_WAIT_TICKS; _t++) {
        baseRecipe = bot.recipesFor(itemByName.id, null, 1, craftingTable)[0];
        if (baseRecipe) break;
        // recipesFor 空：先看配方是否客观存在（recipesAll 不读库存）。存在却查不到 = 库存没同步，等。
        if (bot.recipesAll(itemByName.id, null, craftingTable).length) {
            await bot.waitForTicks(1);
            continue;
        }
        break; // 配方客观上就不存在，等再久也没用，直接进 else 报真缺料
    }
    if (baseRecipe) {
        // 按配方 delta 算「材料最多支持做几次」，用于料不够时给出可执行的具体数字，
        // 而不是只说一句「做不了」让 LLM 瞎猜。
        // ★ 这里的 bot.inventory.count 同样受缓存延迟影响（同上 #10）：刚拾取完可能读到旧值。
        //   算 maxCrafts 前等一拍让库存稳定，避免「料其实够却报 maxCrafts=0」的假缺料。
        let maxCrafts = Infinity;
        for (let _retry = 0; _retry < _RECIPE_WAIT_TICKS; _retry++) {
            maxCrafts = Infinity;
            for (const d of baseRecipe.delta) {
                if (d.count < 0) {
                    maxCrafts = Math.min(
                        maxCrafts,
                        Math.floor(bot.inventory.count(d.id, d.metadata) / -d.count)
                    );
                }
            }
            if (maxCrafts > 0) break; // 至少能做 1 次，库存已同步
            await bot.waitForTicks(1);
        }
        if (maxCrafts < count) {
            // 料不够做满 count 次：直说差多少、能做几次，别播报「I can make」后再翻脸。
            bot.chat(
                `I can only do the recipe for ${name} ${maxCrafts} times (you asked ${count}); ` +
                    `each craft makes ${baseRecipe.result.count} ${name}. ` +
                    `Gather more ingredients or retry with count=${Math.max(maxCrafts, 1)}.`
            );
            if (maxCrafts <= 0) {
                failedCraftFeedback(bot, name, itemByName, craftingTable);
                _craftItemFailCount++;
                if (_craftItemFailCount > 10) {
                    throw new Error(
                        "craftItem failed too many times, check chat log to see what happened"
                    );
                }
                return;
            }
            count = maxCrafts; // 尽力而为：做满能做的份数，比整步空转有用
        }
        bot.chat(`I can make ${name}`);
        // ★ **库存空格预检**（2026-08-25 修，根因见 memory
        //   openkage_braindb_systembug_audit_20260825 的 slot_full_craft_fake_success）：
        //   mineflayer 的 bot.craft（craft.js）流程是「先把材料点进合成格 → putMaterialsAway 把
        //   材料挪回 → grabResult 把产物 putAway 到库存」。putAway 走 putSelectedItemRange
        //   （inventory.js:157）：**找不到空格就 tossLeftover（clickWindow(-999)）把物品扔到地上**。
        //   于是背包满 36/36 时：材料可能被扔、产物也被扔 —— bot.craft 不抛错，但料和产物都丢了。
        //   下面 _realGain 检查虽能检出"产物没到账"并抛错，但**料已经被吞、且错误里不提丢了多少料**
        //   （长跑实例：合钻石胸甲被扣 8 钻、0 产物，Agent 据错误以为只是没合成、不知 8 钻已没）。
        //   治本：合成前先确认至少 1 个空格。emptySlotCount()=0 就直接抛、根本不进 craft ——
        //   料不会被动。要求 1 格即可（产物堆叠进已有格也行，但留 1 空格最稳，防多产物分散占格）。
        //   best-effort：旧版 mineflayer 无 emptySlotCount 时跳过预检（退回靠 _realGain 兜底）。
        const _hasEmptySlotCount = typeof bot.inventory.emptySlotCount === "function";
        if (_hasEmptySlotCount && bot.inventory.emptySlotCount() <= 0) {
            throw new Error(
                `craft ${name} 前库存已满（36/36 无空格）：mineflayer 的 bot.craft 在背包满时会 ` +
                    `把产物/材料 tossLeftover 扔到地上（不报错），等于吞料。先 toss/drop 或用掉 ` +
                    `一些物品腾出至少 1 格空格，再 craft ${name}。`
            );
        }
        // 记录 craft 前的产物数量，用于 craft 后验证产物**真入账**（见下面 _verifyCrafted）。
        const _beforeCount = bot.inventory.count(itemByName.id, null);
        // ★ **同时记录每种材料的 craft 前数量**（2026-08-25 修，吞料上报）：万一 craft 假成功
        //   （_realGain<=0）或预检漏网（旧 mineflayer 无 emptySlotCount），要能告诉 Agent
        //   「这次不仅没出产物，还丢了 N 个 X 料」，而不是只说"没合成"。delta 里 count<0 的是材料。
        const _matSnapshot = []; // [{name, id, metadata, before}]
        for (const d of baseRecipe.delta) {
            if (d.count < 0) {
                // id->name 走 mcData.items[id]（与 mineBlock.js:37 / craftHelper.js:212 同口径）。
                const _mi = mcData.items[d.id];
                _matSnapshot.push({
                    name: (_mi && _mi.name) ? _mi.name : `id#${d.id}`,
                    id: d.id, metadata: d.metadata,
                    before: bot.inventory.count(d.id, d.metadata),
                });
            }
        }
        try {
            await bot.craft(baseRecipe, count, craftingTable);
        } catch (err) {
            // 真实 err.message 必须播报：吞掉它的话，LLM 只看到「做不了」而无从判断是缺料、
            // 缺工作台、还是库存满，只能瞎猜。
            bot.chat(
                `I cannot do the recipe for ${name} ${count} times: ${err.message}`
            );
            return;
        }

        // ★ **靠世界状态判成败，不靠 craft 的回执**（同 placeItem 的思路，见 #10）：
        //   bot.craft 内部 grabResult（craft.js:132）把产物 `putAway(0)` 到库存，但这个动作
        //   靠 `windowUpdate` 事件确认；事件晚到 / 库存缓存未刷新时，bot.craft 可能不抛错、
        //   服务器侧却没真合成成功，或成功了但 `bot.inventory.items()` 还读不到产物。
        //   dave 第2天「回执显示 got 4 但库存无 acacia_planks 入账」就是这种「假成功」。
        //   修法：等库存同步后**数产物有没有真涨**。涨了才算成、如实报产出数；没涨就抛，
        //   让 LLM 知道这次没做成、别据「I did the recipe」自欺（与 placeItem 失败必抛同理）。
        const _expectedGain = count * baseRecipe.result.count;
        let _afterCount = _beforeCount;
        for (let _t = 0; _t < _RECIPE_WAIT_TICKS; _t++) {
            _afterCount = bot.inventory.count(itemByName.id, null);
            if (_afterCount >= _beforeCount + _expectedGain) break;
            await bot.waitForTicks(1);
        }
        const _realGain = _afterCount - _beforeCount;
        if (_realGain <= 0) {
            // 产物一个都没多出来 —— 这次 craft 没真正生效。抛错而非播报成功，避免 LLM 据假成功
            // 的回执继续往下走（如以为做出了 iron_sword 就去打怪，结果主手空）。
            // ★ **同时上报材料损失**（2026-08-25 修）：清点每种材料 craft 后比 craft 前少了多少。
            //   长跑里"合钻石胸甲被扣 8 钻 0 产物"正是此场景——光说"没合成"会让 Agent 以为料还在，
            //   继续拿不存在的钻石做别的。这里点名丢了什么、丢了几个，Agent 才知道要去捡回/重新备料。
            let _matLossMsg = "";
            for (const m of _matSnapshot) {
                const _after = bot.inventory.count(m.id, m.metadata);
                const _lost = m.before - _after;
                if (_lost > 0) {
                    _matLossMsg += `、丢失 ${_lost} 个 ${m.name}（${m.before}→${_after}）`;
                }
            }
            throw new Error(
                `craft ${name} 假成功：bot.craft 未报错但库存产物未入账（before=${_beforeCount} ` +
                    `after=${_afterCount}，期望 +${_expectedGain}）。可能是窗口/库存同步延迟，` +
                    `重试本步通常可成。` +
                    (_matLossMsg
                        ? `⚠️ 但材料已被动${_matLossMsg} —— 若背包当时已满，这些料可能被 ` +
                          `tossLeftover 扔到了脚下，捡回或重新备料。`
                        : "")
            );
        }
        bot.chat(
            `I did the recipe for ${name} ${count} times ` +
                `(got ${_realGain} ${name})`
        );
    } else {
        failedCraftFeedback(bot, name, itemByName, craftingTable);
        _craftItemFailCount++;
        if (_craftItemFailCount > 10) {
            throw new Error(
                "craftItem failed too many times, check chat log to see what happened"
            );
        }
    }
}
