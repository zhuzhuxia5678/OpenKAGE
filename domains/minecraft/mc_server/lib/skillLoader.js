function inject(bot) {
    bot._sleep = bot.sleep;
    bot.sleep = async (bedBlock) => {
        await bot.waitForTicks(20);
        // ★ 记 sleep 前的 worldDay —— bot._sleep resolve 仅代表"躺下"，跳夜是服务器随后异步发
        //   update_time 包（test_sleep_day.js:134 实测）。不等跳夜完成就返回，Agent 拿到的仍是黑夜、
        //   继续动作会被怪打。故 poll bot.time.day 滚动（+1=跳夜完成），超时兜底 8s。
        const dayBefore = bot.time && typeof bot.time.day === "number" ? bot.time.day : null;
        await bot._sleep(bedBlock);
        await bot.waitForTicks(135);
        if (dayBefore !== null) {
            const deadline = Date.now() + 8000;
            while (Date.now() < deadline) {
                const dayNow = bot.time && typeof bot.time.day === "number" ? bot.time.day : null;
                if (dayNow !== null && dayNow > dayBefore) break;  // 跳夜完成
                await bot.waitForTicks(5);
            }
        }
    };

    bot._fish = bot.fish;
    bot.fish = async () => {
        if (bot.heldItem?.name !== "fishing_rod") {
            bot.chat("I'm not holding a fishing rod!");
            return;
        }
        let timeout = null;
        await Promise.race([
            bot._fish(),
            new Promise(
                (resolve, reject) =>
                    (timeout = setTimeout(() => {
                        bot.activateItem();
                        reject(
                            new Error(
                                "Finishing timeout, make sure you get to and look at a water block!"
                            )
                        );
                    }, 60000))
            ),
        ]);
        clearTimeout(timeout);
        await bot.waitForTicks(20);
    };

    bot._consume = bot.consume;
    // ★ **consume 必须先 equip 食物到主手、并停住动作**（2026-08-25 修，根因见
    //   memory openkage_braindb_systembug_audit_20260825 的 pause_before_consume / health_regen）：
    //   mineflayer 的 `bot.consume()`（inventory.js:99）**只吃主手当前握着的物品**：
    //     · 主手空 → 第 104 行 `bot.heldItem.name` 抛 TypeError，或发了空 use_item 包、
    //       服务器永不回 entity_status=9 → 挂满 CONSUME_TIMEOUT(~2.5s) 返回 "Promise timed out"，
    //       生命饥饿都不变（Carol 连试 melon_slice/rotten_flesh 全超时的真因）；
    //     · 主手握非食物（镐/方块）→ 同样永不触发进食确认 → 同样的静默超时。
    //   旧包装直接 `await bot._consume()`，既不 equip 也不停寻路——consume 与正在跑的 pathfinder
    //   goal 抢同一执行通道也会挂起超时。Agent 只能靠自己「先 equip 再 consume + 静止半秒」绕过，
    //   并把这条绕过误记成"游戏规律"污染公共脑。
    //   修法把绕过收进框架：① 主手非食物就自动从背包找食物 equip 上；② equip/吃前停 pathfinder，
    //      避免与移动抢通道；③ 无食物时抛**明确**错（而非 mineflayer 的静默超时），让 Agent 知道
    //      是"没吃的"而不是"动作卡了"。对照同文件 fish 包装（第 11 行校验 heldItem）——consume 此前
    //      漏了这层，是明显的不一致。
    bot.consume = async (item) => {
        // 允许显式传 `bot.consume(foodItem)` 指定吃哪个；不传就吃主手或背包里任一食物。
        // ★ isFood 用 bot.registry.foodsByName（官方 API，由 minecraft-data 的 foods.json 索引），
        //   回退到 ALWAYS_CONSUMABLES（potion/milk_bucket/*_golden_apple，这些不在 foods.json 里但可吃）。
        const _ALWAYS = ["potion", "milk_bucket", "enchanted_golden_apple", "golden_apple"];
        const _foods = (bot.registry && bot.registry.foodsByName) || {};
        const _isFood = (it) => !!it && (Boolean(_foods[it.name]) || _ALWAYS.includes(it.name));

        // ① 主手没握食物（空 / 非食物）→ 从背包找食物 equip 上。Agent 调 consume 前常忘 equip，
        //   这是"反复进食全超时"的主要入口。
        if (!_isFood(bot.heldItem)) {
            const food = item || bot.inventory.items().find((i) => _isFood(i));
            if (!food) {
                // 明确报错，而非让 mineflayer 在空主手上抛 TypeError 或静默挂超时。
                throw new Error(
                    "没有可吃的食物：主手不是食物且背包里也没有食物。" +
                    "先采集/烹饪食物（apple/melon_slice/cooked_porkchop 等）再来 consume。"
                );
            }
            await bot.equip(food, "hand");
        }
        // ② 停掉正在跑的寻路/挖掘 goal，避免与 consume 抢执行通道挂起超时（Carol 的对照实验：
        //    连续移动/挖块中 consume 全超时，静止半秒后吃成功）。setGoal(null) 是停止寻路的
        //    正式 API（见 shoot.js:81），best-effort：旧 bot 或无 pathfinder 时静默跳过。
        try { bot.pathfinder && bot.pathfinder.setGoal && bot.pathfinder.setGoal(null); } catch (_e) { /* ignore */ }
        // ③ 主手已确认为食物，安全调用真实 consume。bot.food===20 时 mineflayer 自抛 'Food is full'，
        //    那是有意义的信号（提示吃饱了），原样透传。
        await bot._consume();
        await bot.waitForTicks(20);
    };

    // ★ **equip 装护甲/副手后校验槽位真装上**（2026-08-25 修，根因见 memory
    //   openkage_braindb_systembug_audit_20260825 的 armor_slot_occupied_by_block）：
    //   placeItem 里的 equip 竞态修复只覆盖 `equip(item,'hand')`（验 bot.heldItem），**护甲四件
    //   与副手没覆盖**。mineflayer 的 `bot.equip(item, dest)`（simple_inventory.js:88）对非 hand 目标
    //   走 `bot.moveSlotItem(sourceSlot, destSlot)` —— 一连串 clickWindow，靠 windowUpdate 包确认。
    //   窗口/库存同步延迟下，equip「返回无异常」但护甲槽位仍是旧物（被方块顶占、或上次没装上），
    //   Agent 据此以为穿了护甲其实没穿。长跑里 erin/bob 反复记到"装备槽被方块顶占、护甲没真穿上"。
    //   修法与 placeItem 的 hand 校验同构：equip 后验目标槽位确实握上了目标物品类型，没握上就等几
    //   tick + 重试 equip（最多 3 次），仍没握上就抛明确错（让 Agent 知道是 equip 竞态、重试本步即可，
    //   而非据"equip 正常返回"误以为穿好了）。仅校验非 hand 目标——hand 的竞态 placeItem 已兜底，
    //   且 heldItem 的读法与护甲槽不同。
    bot._equip = bot.equip;
    bot.equip = async (item, dest) => {
        if (dest === "hand" || !dest) {
            // hand 走原路径（placeItem 等会自行校验 heldItem）；不在此重复兜底。
            return bot._equip(item, dest);
        }
        // 护甲/副手槽位索引（与 status.js:124-129 / simple_inventory.js:12-19 同口径）。
        const _ARMOR_SLOT = { head: 5, torso: 6, legs: 7, feet: 8, "off-hand": 45 };
        const _slot = _ARMOR_SLOT[dest];
        if (_slot === undefined) {
            // 未知目标名：交给 mineflayer 原 equip 报"invalid destination"，别在此吞。
            return bot._equip(item, dest);
        }
        const _targetType = item && item.type;
        const _WAIT = 10;
        let _ok = false;
        for (let _try = 0; _try < 3; _try++) {
            await bot._equip(item, dest);
            // 等服务器的 windowUpdate 包刷新护甲槽（通常 1~2 tick）。
            for (let _t = 0; _t < _WAIT; _t++) {
                const _cur = bot.inventory.slots[_slot];
                if (_cur && _cur.type === _targetType) { _ok = true; break; }
                await bot.waitForTicks(1);
            }
            if (_ok) break;
            // 没装上：item 对象可能在 clickWindow 过程中换了 slot，重取一份权威的再试（同 placeItem 思路）。
            if (_targetType != null) {
                const _fresh = bot.inventory.findInventoryItem
                    ? bot.inventory.findInventoryItem(_targetType)
                    : null;
                if (_fresh) item = _fresh;
            }
        }
        if (!_ok) {
            const _cur = bot.inventory.slots[_slot];
            throw new Error(
                `equip(${dest}) 竞态：equip 返回正常但 ${dest} 槽位仍是 ` +
                `${_cur ? _cur.name : "空"}（期望 ${item && item.name ? item.name : "目标物品"}）。` +
                `可能被方块顶占或窗口同步延迟，重试本步通常可成。穿完务必核对观察里的「装备」行。`
            );
        }
    };

    bot._useOn = bot.useOn;
    bot.useOn = async (entity) => {
        if (entity.position.distanceTo(bot.entity.position) > 6) {
            bot.chat("Please goto a place near the entity first!");
            return;
        }
        await bot._useOn(entity);
        await bot.waitForTicks(20);
    };

    bot._activateBlock = bot.activateBlock;
    bot.activateBlock = async (block) => {
        if (block.position.distanceTo(bot.entity.position) > 6) {
            bot.chat("Please goto a place near the block first!");
            return;
        }
        // action_count.activateBlock++;
        await bot._activateBlock(block);
    };

    bot._chat = bot.chat;
    // 按「斜杠开头 = 游戏指令」二分，两条去处不再重叠。
    //   · "/..."  → 真发给服务器执行（onChat.js 那侧本就过滤掉 "/" 开头的，不回传 LLM）
    //   · 其余    → 纯粹是给 LLM 的执行反馈（原语的 "I did the recipe for..."、超时/死亡提示），
    //               只 emit chatEvent 走 observe 回传，**不再发到游戏聊天框**。
    // 为何这样分：这些反馈已回传 LLM + 打进 Python 侧日志（[minecraft反馈]），
    // 再刷一遍聊天框纯属噪音——人在客户端围观时聊天框全是 "Placed dirt"/"I can make stick"，
    // 盖掉真正想看的东西。**那个被腾空的位置现在给了 bot 的思考**（index.js 的 say，走 _chat）。
    bot.chat = (message) => {
        // action_count.chat++;
        const text = String(message);
        if (text.startsWith("/")) {
            bot._chat(text);                     // 指令：必须真发，否则不生效
        } else {
            bot.emit("chatEvent", "bot", text);  // 反馈：只给 LLM，不进聊天框
        }
    };
    // ★ **LLM 生成的代码不许直接发游戏指令**。
    // system.md 原本列了 `bot.chat('/...')` 作为可用手段，已删除 —— 一旦用了 /give、/tp、
    // /gamerule 就等于作弊，直接污染实验效度：我们测的是「探索能力」，不是「会不会用管理员指令」。
    // 从 prompt 删只是"不建议"，这里做执行层拦截才是"保证"。
    // 拦下来转成一条反馈告知 LLM，让它知道这条路不通、换用原语（而不是静默失败后困惑）。
    //
    // ★★ **原语必须豁免**：`givePlacedItemBack` 内部要发 /gamerule + /give、`useChest` 的
    //    moveToChest 要发 /tp，而它们是**写在 system.md 里、LLM 可以合法调用的原语**。
    //    只按「是否在生成代码里」拦，会把「LLM 调原语 → 原语发指令」也拦掉 —— 那等于静默破坏
    //    一个已文档化的能力，比不拦更糟。
    //
    //    豁免方式按**调用来源**（谁在发这条指令），不按指令文本：指令前缀白名单是漏的 ——
    //    `/give bot` 与 `/tp ` 一旦放行，LLM 只要写 `bot.chat('/give bot diamond_pickaxe')`
    //    就白送钻石镐、`/tp 100 12 200` 就瞬移到钻石层，而这两件恰恰是我们要测量的能力本身。
    //    前缀无法区分"原语在用它"和"LLM 在用它"。「原语入口 +1 / 出口 -1 的深度计数」也行不通：
    //    原语是拼进 `programs` 字符串、在 eval 闭包里声明的**局部函数**，外部拿不到引用去包装。
    //    最终用 **Error().stack 认调用者**：原语的函数名会出现在栈里（跨 await 也保留），
    //    而 LLM 直接 bot.chat 时栈里只有匿名的 eval 帧。这是纯运行时事实，不靠约定。
    const _CMD_PRIMITIVES = [
        "givePlacedItemBack",       // /gamerule block_drops + /give bot（把放置物拆回库存）
        "givePlacedItemBackSingle", // 上者的内部函数，实际发指令的地方
        "moveToChest",              // /tp（>32 格时靠近箱子），useChest 内部
        "getItemFromChest", "depositItemIntoChest", "checkItemInsideChest",
    ];
    bot.openkageInGeneratedCode = false;
    const _chatSplit = bot.chat;
    bot.chat = (message) => {
        const text = String(message);
        if (bot.openkageInGeneratedCode && text.startsWith("/")) {
            // 栈帧格式为 `    at givePlacedItemBackSingle (eval at <anonymous> (...))`，
            // 故按 `at <fn> ` 匹配（比裸函数名精确：不会被字符串里的同名词误命中）。
            const stack = new Error().stack || "";
            const fromPrimitive = _CMD_PRIMITIVES.some((fn) =>
                stack.includes("at " + fn + " ")
            );
            if (!fromPrimitive) {
                const cmd = text.split(/\s+/)[0];
                bot.emit(
                    "chatEvent", "bot",
                    `Game commands are disabled in your code (blocked: ${cmd}). ` +
                        `Use the helper functions (mineBlock / craftItem / placeItem / ` +
                        `givePlacedItemBack / ...) or plain bot APIs instead.`
                );
                return;
            }
        }
        _chatSplit(text);
    };

    bot.inventoryUsed = () => {
        return bot.inventory.slots.slice(9, 45).filter((item) => item !== null)
            .length;
    };

    // `bot.save()` 退化为 **no-op**，但**签名保留**。
    //
    // 它原是 Voyager 的「里程碑事件」钩子：原语成功时调 `bot.save('oak_log_mined')`，onSave 插件
    // 收下字符串进 payload，Voyager 的 critic/curriculum 据此判断 task 是否完成。
    // OpenKAGE**不需要**它：进度判定走 tier 高水位 + 服务器权威成就播报（onSystem 的 translate key），
    // 那是事实源，比原语自报可靠。Python 侧对 `onSave` 字段从未消费 —— 采了、传了，没人用过。
    //
    // ★ 为什么必须真正断掉、而不只是不渲染：`bot.emit("save", ...)` 会走 base.js 的 `bot.event()`，
    //   而那函数会把**所有非 on* 插件全部重跑一遍 observe** —— 包括 Voxels（17×5×17 = 1445 格）、
    //   Vision（48 格射线）、Chests（findBlocks 16 格）。每次 save 调用都等于一次整套环境快照，
    //   算完立刻被丢掉。这是纯浪费的主线程开销，而主线程一卡就掉线
    //   （见 mineBlock 里那段「A* 阻塞事件循环 >30s → 服务器踢下线」的记录）。
    //
    // 保留空函数而不删签名：4 个原语（mineBlock/placeItem×2/killMob）还在调它，删掉会 ReferenceError；
    // 且日后若真要恢复里程碑事件，改这一处即可。
    bot.save = function (_eventName) {
        /* no-op —— 理由见上 */
    };
}

// export all control_primitives
module.exports = { inject };
