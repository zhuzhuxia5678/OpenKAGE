async function smeltItem(bot, itemName, fuelName, count = 1, furnacePos) {
    // return if itemName or fuelName is not string
    if (typeof itemName !== "string" || typeof fuelName !== "string") {
        throw new Error("itemName or fuelName for smeltItem must be a string");
    }
    // return if count is not a number
    if (typeof count !== "number") {
        throw new Error("count for smeltItem must be a number");
    }
    const item = mcData.itemsByName[itemName];
    const fuel = mcData.itemsByName[fuelName];
    if (!item) {
        throw new Error(`No item named ${itemName}`);
    }
    if (!fuel) {
        throw new Error(`No item named ${fuelName}`);
    }
    // ★ **优先用已知坐标找炉子，退而求其次才 32 格半径盲搜**（2026-08-25 修，根因见 memory
    //   openkage_braindb_systembug_audit_20260825 的 container_vanish_across_days —— 该经验是**误诊**：
    //   炉子没真消失，是 findBlock(maxDistance:32) 只搜 bot 当前位置 32 格内，bot 走远了/记混坐标
    //   就搜不到，Agent 把"搜不到"误记成"世界状态损坏"。治本：smeltItem 接受可选 furnacePos
    //   （Vec3），给了就 blockAt 确认它还在、并直接走过去开炉；没给才退回旧的 32 格盲搜。
    //   这样 Agent 拿记忆里的炉子坐标（day_N 存的 (1,1,-10) 之类）直查，不再因走远而"丢炉子"，
    //   也从根本上消除"炉子跨天消失"这条误记的诱因。
    let furnaceBlock = null;
    if (furnacePos) {
        // 坐标可能是 Vec3 或 {x,y,z}。blockAt 在 chunk 没加载时返 null（mineflayer: null block
        //   means chunk not loaded）—— 那就先走过去触发加载、再读一次。
        const _pos = furnacePos;
        const _readAt = () => {
            const b = bot.blockAt(_pos);
            return b && b.name === "furnace" ? b : null;
        };
        furnaceBlock = _readAt();
        if (!furnaceBlock) {
            // 走到坐标附近让 chunk 加载，再读一次。best-effort：goto 失败不致命，落回盲搜。
            try {
                await bot.pathfinder.goto(
                    new GoalNear(_pos.x, _pos.y, _pos.z, 2)
                );
            } catch (_e) { /* 走不过去就落回下面盲搜 */ }
            furnaceBlock = _readAt();
        }
    }
    if (!furnaceBlock) {
        furnaceBlock = bot.findBlock({
            matching: mcData.blocksByName.furnace.id,
            maxDistance: 32,
        });
    }
    if (!furnaceBlock) {
        throw new Error(
            "No furnace nearby" +
            (furnacePos
                ? `（已按你给的坐标 (${furnacePos.x},${furnacePos.y},${furnacePos.z}) 找过：那里不是炉子/` +
                  `chunk 没加载；32 格内也搜不到。炉子可能真没放或被挖走了，可 placeItem 重新放一个）`
                : "（没给坐标、32 格内也没炉子。若你记得炉子坐标，传给 smeltItem 第 5 个参数 `furnacePos` 直查）")
        );
    } else {
        // ★ **用 GoalNear 站到炉子 2 格内，不用 GoalLookAtBlock**（2026-08-21 e2e 暴露）：
        //   GoalLookAtBlock 只保证"看着"炉子，不保证站得够近能右键交互。bot 可能站在 5 格外看着炉子，
        //   activateBlock 够不着 → 服务器不开窗 → `Event windowOpen did not fire within 20000ms`
        //   （smeltItem 因此 21s 超时失败，炉子根本没打开）。GoalNear(range=2) 保证 bot 头部在炉子
        //   2 格内，activateBlock 必在交互距离。与 placeItem「GoalPlaceBlock 要 5 格内」同源教训。
        await bot.pathfinder.goto(
            new GoalNear(furnaceBlock.position.x, furnaceBlock.position.y, furnaceBlock.position.z, 2)
        );
    }
    // openFurnace 走 activateBlock → 等 windowOpen 事件，偶发超时（服务器开窗包延迟）。
    // 重试 2 次：窗口包晚到时换个朝位/再 activate 通常能开。
    let furnace = null;
    for (let _ot = 0; _ot < 2; _ot++) {
        try {
            furnace = await bot.openFurnace(furnaceBlock);
            break;
        } catch (e) {
            if (_ot === 1) throw e;
            await bot.waitForTicks(10);
        }
    }
    let success_count = 0;

    // ★ **库存读取容错**（见 memory openkage_arm_graph_bug_audit_20260820 的 #3）：
    //   mineflayer 的 `bot.inventory.findInventoryItem` 读的是客户端缓存的 slots，而
    //   putInput/putFuel/takeOutput 走 clickWindow→服务器往返，**slots 刷新有 1~几 tick 延迟**。
    //   旧实现一次性 `findInventoryItem` 查不到就 break/报「No X to smelt」—— 但实际库存里还有
    //   （carol day29「报 No raw_iron 但有 raw_iron×3」、erin day10-15「烧不成」都根因于此）。
    //   先等几 tick 让窗口/库存同步再判，仍没有才算真没有。
    const _hasItem = async (itemId) => {
        if (bot.inventory.findInventoryItem(itemId, null)) return true;
        for (let _t = 0; _t < 10; _t++) {
            await bot.waitForTicks(1);
            if (bot.inventory.findInventoryItem(itemId, null)) return true;
        }
        return false;
    };

    // ★ **try/finally 保证 furnace.close() 必执行**（2026-08-24 修）：
    //   for 循环内有 3 条 throw 路径（putFuel 失败/putInput 失败/假成功）会直接退出函数，
    //   绕过 furnace.close()，导致炉子窗口未归还。mineflayer 下次 openFurnace 前会 auto-close
    //   （openBlock 检查 bot.currentWindow），功能上无害，但结构上有漏。
    try {
    for (let i = 0; i < count; i++) {
        if (!(await _hasItem(item.id))) {
            bot.chat(`No ${itemName} to smelt in inventory`);
            break;
        }
        // ★ **燃料块判据与 fuelSeconds 解耦**（2026-08-24 修，根因见下）：
        //   旧判据 `furnace.fuelSeconds < 15 && fuelItem()?.name !== fuelName` 几乎恒 false：
        //   ① 开窗未收进度包时 fuelSeconds=null，`null < 15` 虽为 true（JS 把 null 转 0），但
        //      ② oak_log 燃烧恰好 15 秒，刚投进炉子时 fuelSeconds≈15，`15 < 15`===false → **整段清
        //      异类燃料 + 投燃料被跳过**。150621 真实长跑里 carol step27 炉子燃料槽卡着 oak_log、
        //      要用 coal 烧，putFuel(coal) 撞 `destination full` 反复失败（日志 line3569/3748 实证）。
        //   正确判据是「燃料槽里不是目标燃料就得清+投」，与 fuelSeconds 无关。fuelSeconds 只在「已有
        //   足量同种燃料在烧」时可省一次投料，不是门控条件。判 type 用 fuelItem().type（数字 id），
        //   与 putInput/inputItem 一致（item 来自 mcData 只有 .id，prismarine-item 的 .type===id）。
        const _curFuel = furnace.fuelItem();
        const _needFuel = !_curFuel || _curFuel.type !== fuel.id;
        if (_needFuel) {
            // ★ **先清燃料槽里的异类物品**（2026-08-24 修）：
            //   若 slots[1] 已有物品但不是目标燃料（如 carol step9 把 oak_log 投进燃料槽后
            //   卡死炉子），putFuel 的 destEnd=destStart+1=2，window 里 slots[1] 满且物品 type
            //   不匹配 → findItemRange 和 firstEmptySlotRange 都返回 null → `destination full`。
            //   shiftClick/dumpItem 对炉子槽无效（carol step31/32 实测），只有 takeFuel()
            //   （mineflayer furnace.js:85，走 bot.putAway）能可靠地把燃料槽物品取回背包。
            //   takeFuel 走 putAway 往返有竞态，**重试 3 次**而非只试一次（旧版只试一次、catch 吞，
            //   takeFuel 回执谎报后 putFuel 必撞 destination full，见场景11 测试）。
            if (_curFuel && _curFuel.type !== fuel.id) {
                for (let _tt = 0; _tt < 3; _tt++) {
                    try {
                        await furnace.takeFuel();
                        break;
                    } catch (_e) {
                        if (_tt === 2) {
                            // 3 次都没清掉 —— 燃料槽被异类物品焊死，本步救不回，抛让 LLM 知道
                            throw new Error(
                                `takeFuel 清理燃料槽失败：3 次重试后 slots[1] 仍卡着异类物品` +
                                `（type=${_curFuel.type}，目标燃料 ${fuelName}）。炉子被上次的料卡住，` +
                                `可能需手动破坏炉子重放，或换一个炉子`
                            );
                        }
                        await bot.waitForTicks(2);
                    }
                }
                // 等 putAway 往返刷新 slots[1]，并轮询确认真清空（防 takeFuel 回执谎报）
                for (let _t = 0; _t < 10; _t++) {
                    const _f = furnace.fuelItem();
                    if (!_f || _f.type === fuel.id) break;
                    await bot.waitForTicks(1);
                }
            }
            if (!(await _hasItem(fuel.id))) {
                bot.chat(`No ${fuelName} as fuel in inventory`);
                break;
            }
            // ★ **putFuel 包在 try/catch 里重试**（2026-08-24 修）：
            //   `bot.transfer` 可能抛 "Can't find X" / "destination full" 等同步异常，
            //   旧写法裸 await 让异常穿透 for 循环直达 finally.close()，跳过投燃料整步。
            //   这里最多重试 3 次：catch 后等 2 tick 再重试；3 次仍失败才 throw。
            let _putFuelOk = false;
            for (let _pft = 0; _pft < 3; _pft++) {
                try {
                    await furnace.putFuel(fuel.id, null, 1);
                    _putFuelOk = true;
                    break;
                } catch (_e) {
                    await bot.waitForTicks(2);
                }
            }
            if (!_putFuelOk) {
                throw new Error(
                    `putFuel(${fuelName}) 投燃料失败：3 次重试后仍异常` +
                    `（库存剩 ${bot.inventory.count(fuel.id)}）。窗口同步问题，重试本步通常可成`
                );
            }
            // ★ **燃料合法性不靠 `furnace.fuel`**（2026-08-23 修，见 memory
            //   openkage_viewer_checkpoint_snapshots 那批 bug 经验的根因之一）：
            //   `furnace.fuel` 只在服务器下发 `craft_progress_bar`（property 0）包后才从 null 变数值
            //   （mineflayer furnace.js:48）。putFuel 后固定等 20 tick 再读 `!furnace.fuel`，progress
            //   包没到就仍是 null → **对任何合法燃料（木板/木棍/煤…）都会误抛 "is not a valid fuel"**。
            //   修法：靠「燃料槽里有没有该物」这一确定信号（slots[1]，set_slot/window_items 包刷新，
            //   比 progress 包更早到），轮询几 tick 确认 fuelItem 命中。
            let _fuelOk = false;
            for (let _t = 0; _t < 10; _t++) {
                if (furnace.fuelItem() && furnace.fuelItem().type === fuel.id) {
                    _fuelOk = true;
                    break;
                }
                await bot.waitForTicks(1);
            }
            if (!_fuelOk) {
                throw new Error(
                    `putFuel(${fuelName}) 投燃料失败：10 次轮询后炉子 slots[1] 仍无该物` +
                    `（库存剩 ${bot.inventory.count(fuel.id)}）。窗口同步问题，重试本步通常可成`
                );
            }
        }

        // ★ **先清 input 槽里的异类物品**（2026-08-24 修）：同燃料槽的问题——若 slots[0]
        //   已有不同类物品（上一次 putInput 进去但还没烧完的其他矿石），putInput 会
        //   `destination full`。先 takeInput() 把旧料取回背包。takeInput 同样走 putAway 往返、
        //   有竞态，**重试 3 次**（与 takeFuel 对称，旧版只试一次）+ 轮询确认真清空。
        const _existingInput = furnace.inputItem();
        if (_existingInput && _existingInput.type !== item.id) {
            for (let _tt = 0; _tt < 3; _tt++) {
                try {
                    await furnace.takeInput();
                    break;
                } catch (_e) {
                    if (_tt === 2) break; // 清不掉也无害——后续 putInput 若仍 destination full 会再抛
                    await bot.waitForTicks(2);
                }
            }
            for (let _t = 0; _t < 10; _t++) {
                const _i = furnace.inputItem();
                if (!_i || _i.type === item.id) break;
                await bot.waitForTicks(1);
            }
        }

        // ★ **putInput 投料验证 + try/catch 重试**（2026-08-21 暴露投料竞态；2026-08-24 加 catch）：
        //   `furnace.putInput` 走 `bot.transfer`（clickWindow 服务器往返），有竞态——
        //   返回了但物品没真落 slots[0]，或抛 "Can't find X" / "destination full"。
        //   旧写法裸 await 让异常穿透 _INPUT_TRIES 循环；现在 catch 后等 2 tick 重试。
        const _INPUT_TRIES = 4;
        let _inputOk = false;
        for (let _it = 0; _it < _INPUT_TRIES; _it++) {
            try {
                await furnace.putInput(item.id, null, 1);
            } catch (_e) {
                await bot.waitForTicks(2);
                continue; // putInput 抛了，直接重试（验证步骤也跳过）
            }
            // 等窗口 slots 刷新（set_slot 包），再验证 slots[0] 是投的物品。
            // ★ item 来自 mcData.itemsByName（只有 .id，无 .type）；furnace.inputItem() 返回
            //   prismarine-item（.type === 数字 id）。故比较用 item.id。
            for (let _t = 0; _t < 10; _t++) {
                const inp = furnace.inputItem();
                if (inp && inp.type === item.id) { _inputOk = true; break; }
                await bot.waitForTicks(1);
            }
            if (_inputOk) break;
            await bot.waitForTicks(2);
        }
        if (!_inputOk) {
            // 投了 4 次都没进炉子槽位 —— 窗口/库存同步严重延迟或物品不可投
            throw new Error(
                `putInput(${itemName}) 投料失败：${_INPUT_TRIES} 次重试后炉子 slots[0] 仍无该物 ` +
                `（库存剩 ${bot.inventory.count(item.id)}）。窗口同步问题，重试本步通常可成`
            );
        }
        // ★ 记投料前 slots[2]（产物槽）旧值：第②段判「产物入槽」要防残留误判。
        //   谓词不能写 newItem.type === item.id —— **产物 id ≠ 原料 id**（raw_iron.id=810 烧成
        //   iron_ingot.id=811），那样恒 false → updateSlot:2 永不 resolve → 100% 烧炼超时（2026-08-21
        //   实锤的真 bug，新版从未真实 run 过故日志无痕）。正确判法：slot 2 只装烧炼产物，产物入槽
        //   = newItem 非空 且 与投料前旧值不同（防上轮残留/燃料误刷新触发 updateSlot:2）。产物真伪
        //   由第③段「takeOutput 后库存 count 涨了」兜底。
        const _slot2Before = furnace.outputItem(); // 投料验证刚过，此时 slots[2] 是上轮残留或 null
        let _producedType = null; // 第②段谓词匹配时记下产物 type，供第③段数产物库存（非原料）用
        // ★ **产物等待改为事件驱动**（2026-08-21 治本，见 memory openkage_arm_graph_bug_revisit_20260821 #3）：
        //   旧实现投料后盲等 240 tick 再轮询 furnace.outputItem()，但 mineflayer 把「炉子在烧」和
        //   「产物入槽」绑在两套互不通知的事件上 —— craft_progress_bar 包驱动 progressSeconds 并
        //   emit('update')，而产物入 slots[2] 走 set_slot/window_items → Window.updateSlot →
        //   emit('updateSlot:2')，不触发 furnace 的 'update'。轮询 outputItem() 只能每 tick 碰运气
        //   读一次，烧完进度包到了、产物包晚到时恰读空 → 0821 实测约 50% 误报 invalid input。
        //   治本：mineflayer 自己的 inventory.js craft 路径就用 once(window,'updateSlot:2') 等产物槽
        //   （inventory.js:463/475），set_slot 与 window_items 两条路径都走 window.updateSlot 必触发
        //   updateSlot:2 —— 这是产物入槽的确定信号，不再碰运气。分两段：① 等 update(炉子开始烧)
        //   确认 input 合法；② 等 updateSlot:2(产物入槽) 取走。两段都带超时兜底，按 progressSeconds
        //   状态区分「从未烧过」(真 invalid) vs「烧过但产物包没到」(窗口包延迟，重试可成)。
        const _everProgressed = { v: false }; // 闭包内可变，记曾否读到 progressSeconds>0
        // ★ 面向 Agent 的超时报错只说**游戏刻**（墙钟 ms 是内部 setTimeout 用的、不外泄）：
        //   `timeoutMs` 按当前倍速换算回刻 `ms * rate / 1000`。
        const _rate = Math.max(1, Number(bot.openkageTickRate) || 1);
        const _waitForEvent = (eventName, predicate, timeoutMs, progressLabel, failIfNoProgressMsg) =>
            new Promise((resolve, reject) => {
                let _timer = null;
                let _settled = false;
                const _cleanup = () => {
                    if (_settled) return;
                    _settled = true;
                    furnace.removeListener(eventName, _handler);
                    if (_timer) clearTimeout(_timer);
                    if (furnace.progressSeconds && furnace.progressSeconds > 0) _everProgressed.v = true;
                };
                const _handler = (...args) => {
                    if (predicate(...args)) { _cleanup(); resolve(); }
                    // 谓词不满足（如 updateSlot:2 刷的是空槽/燃料残留）→ 继续等，不 settle
                };
                furnace.on(eventName, _handler);
                _timer = setTimeout(() => {
                    _cleanup();
                    const progressed = _everProgressed.v || (furnace.progressSeconds && furnace.progressSeconds > 0);
                    reject(new Error(
                        progressed
                            ? `${itemName} 烧炼超时（${progressLabel}）：炉子烧过但 ${Math.round(timeoutMs * _rate * 20 / 1000)} 刻内 ${eventName} 未触发（mineflayer 窗口包延迟，重试本步通常可成）`
                            : failIfNoProgressMsg
                    ));
                }, timeoutMs);
            });

        // ① 等「炉子开始烧」：投料合法则服务器下发 craft_progress_bar → furnace.emit('update')。
        //    从未烧过（燃料没点 / input 不可烧）→ 15s 超时 → 判 invalid input。
        try {
            await _waitForEvent(
                "update",
                () => { _everProgressed.v = true; return true; },
                15000,
                "等开始烧(update)",
                `${itemName} is not a valid input —— 投料后炉子 300 刻内从未进入烧炼状态` +
                `（everProgressed=false）。多半是 input 不可烧，或燃料没点上`
            );
        } finally {
            // update listener 持续整个炉子生命周期（furnace.js:36），这里只等首次，务必 off 掉
            furnace.removeAllListeners("update");
        }

        // ② 等「产物入槽」：烧成后 set_slot 刷新 slots[2] → emit('updateSlot:2', oldItem, newItem)。
        //    谓词判「newItem 非空 且 与投料前 _slot2Before 不同」—— 产物入槽的确定信号。**不要**写
        //    newItem.type === item.id：产物 id ≠ 原料 id（raw_iron 810→iron_ingot 811），恒 false。
        //    硬上限 25s 覆盖最慢烧炼(~10s)+启动延迟+产物包延迟。
        await _waitForEvent(
            "updateSlot:2",
            (_oldItem, newItem) => {
                if (!newItem) return false; // 空槽刷新（产物被取走/槽被清），不算
                const before = _slot2Before;
                // 与投料前旧值不同即视为新产物入槽（旧值 null → 任何非空都算；旧值有残留 → type 或 count 变才算）
                if (!before || newItem.type !== before.type || newItem.count !== before.count) {
                    _producedType = newItem.type; // 记产物 type 给第③段数库存（产物 ≠ 原料，别用 item.id）
                    return true;
                }
                return false;
            },
            25000,
            "等产物入槽(updateSlot:2)",
            `${itemName} is not a valid input —— 投料后炉子从未进入烧炼状态` +
            `（everProgressed=false）。多半是 input 不可烧，或燃料没点上`
        );

        // ③ 取走产物并验证真入账（沿用 craftItem _verifyCrafted 思路，防 takeOutput 回执谎报）：
        //    takeOutput 走 bot.putAway(item.slot)（furnace.js:77），有同款窗口往返竞态。
        //    **数产物的库存，不是原料**：takeOutput 取走的是产物（_producedType），入账让产物 count 涨；
        //    旧实现误用 item.id（原料），原料投进炉子后只减不增 → count 永不涨 → 100% 误报假成功
        //    （2026-08-21 实锤，与第②段谓词同源的「产物/原料搞混」第二处）。
        //    ★ **用 furnace.count() 而非 bot.inventory.count()**（2026-08-24 修）：
        //    takeOutput → putAway 把产物写进 bot.currentWindow.slots（炉子窗口背包区 slot 3-38），
        //    而 bot.inventory.slots 是另一个 Window 对象，跨窗同步有延迟，15 tick 内读不到 →
        //    误报假成功（实测 copper_ingot/iron_ingot 累计 24 次假成功，产物其实都到背包了）。
        //    furnace 就是 bot.currentWindow，其 slots 由 putAway 直接写入，countRange(3,39) 覆盖
        //    炉子窗口视图里的全部 36 格背包区（主背包 + 快捷栏），立即可见，无需等跨窗同步。
        const _producedId = _producedType; // 第②段记下的产物 type（数字 id）
        // ★ **takeOutput 包 try/catch + 重试**（2026-08-24 修）：putAway 走 clickWindow→confirmTransaction
        //   往返，1.21.11 requireConfirmation=true 下服务器可能 reject 事务（"Server rejected transaction"）
        //   或超时（"didn't respond to transaction"）。旧版裸 await 让异常穿透，agent 看到原始 reject 报错
        //   不知可重试。重试 3 次，每次先确认 outputItem() 还在（产物可能已被服务器移走）。
        const _beforeOut = furnace.count(_producedId, null);
        let _tookOk = false;
        for (let _ot = 0; _ot < 3; _ot++) {
            // 产物已被取走/移走就没得取了，break 让 _tookOk 判定走 count 涨了没
            const _out = furnace.outputItem();
            if (!_out) break;
            try {
                await furnace.takeOutput();
            } catch (_e) {
                await bot.waitForTicks(3);
                continue; // reject/超时 → 重试
            }
            // ★ **轮询窗口 15→25 tick**（2026-08-24 修）：高倍速下游戏刻跑得快、但窗口包（set_slot/
            //   window_items）的墙钟延迟与倍速无关，15 tick(@2.5x≈0.3s 墙钟)偏紧易误报假成功。
            //   25 tick(@2.5x≈0.5s 墙钟)给同步更多余量，仍不拖累步预算（远小于烧炼本身的几十 tick）。
            for (let _t = 0; _t < 25; _t++) {
                if (furnace.count(_producedId, null) > _beforeOut) { _tookOk = true; break; }
                await bot.waitForTicks(1);
            }
            if (_tookOk) break;
            await bot.waitForTicks(3); // count 没涨，可能 takeOutput 回执谎报，重试
        }
        // ★ **_producedId 为 null 的退化判据**（2026-08-24 加）：极端情况下 ② 谓词没记到 type
        //   （理论上不会，但防御），furnace.count(null)=countRange(...,NaN) 恒 0 → 必误报假成功。
        //   退化到「takeOutput 后 outputItem() 变 null 即产物被取走」判据。
        if (!_tookOk && _producedId == null) {
            _tookOk = !furnace.outputItem(); // 产物槽空了 = 被取走
        }
        if (!_tookOk) {
            throw new Error(
                `smeltItem(${itemName}) 假成功：takeOutput 回执正常但产物未入账` +
                `（产物 ${_producedId} 取前=${_beforeOut} 取后=${furnace.count(_producedId, null)}，窗口同步延迟，重试本步通常可成）`
            );
        }
        // 防御：每轮 Promise 内 _cleanup 已 off 本轮 listener，再兜底清一次 updateSlot:2
        furnace.removeAllListeners("updateSlot:2");
        success_count++;

    }
    } finally {
        furnace.close();
    }

    // ★ **成败以「产物是否真取回」为准**：旧实现只在 success_count=0 时算失败，但中途抛过
    //   "not a valid input"（实际烧成了）的情况会直接跳出循环、连 success_count 都不累加 ——
    //   撒谎说全失败。现在产物判定已容错，这里 success_count 反映的是真取回的产物数。
    if (success_count > 0) {
        bot.chat(
            `Smelted ${success_count} ${itemName}（申请 ${count}）。` +
            (success_count < count ? `少烧 ${count - success_count} 个：中途物料不足或炉子问题。` : "")
        );
    } else {
        bot.chat(
            `Failed to smelt ${itemName}, please check the fuel and input.`
        );
        _smeltItemFailCount++;
        if (_smeltItemFailCount > 10) {
            throw new Error(
                `smeltItem failed too many times, please check the fuel and input.`
            );
        }
    }
}
