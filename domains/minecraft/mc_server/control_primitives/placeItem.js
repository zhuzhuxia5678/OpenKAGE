async function placeItem(bot, name, position) {
    // return if name is not string
    if (typeof name !== "string") {
        throw new Error(`name for placeItem must be a string`);
    }
    // return if position is not Vec3
    if (!(position instanceof Vec3)) {
        throw new Error(`position for placeItem must be a Vec3`);
    }
    // ★ instanceof Vec3 **不代表坐标有效**：`new Vec3(NaN, NaN, NaN)` 是完全合法的 Vec3。
    //   而本函数会把 position 原样喂给 `new GoalPlaceBlock(position, …)`（见下面 goto 那行），
    //   NaN 目标会让 A* 永不终止、open list 无限膨胀直到进程 OOM
    //   （见 index.js 里 pathfinder.goto 包装处的完整说明）。
    //   这与 mineBlock 走近逻辑那处校验是同一个坑的对称面 —— 那边过滤 findBlocks 结果，
    //   这边过滤 LLM 直接给的坐标。入口便宜地挡一次，比事后救进程划算。
    if (
        !Number.isFinite(position.x) ||
        !Number.isFinite(position.y) ||
        !Number.isFinite(position.z)
    ) {
        throw new Error(
            `position for placeItem has invalid coordinates (${position.x}, ${position.y}, ` +
                `${position.z}) —— 多半是坐标算错了。注意 bot.findBlocks() 返回 **Vec3 数组**` +
                `（直接用 v.x/v.y/v.z），bot.findBlock() 返回 **Block 对象**（要用 b.position.x）。`
        );
    }
    const itemByName = mcData.itemsByName[name];
    if (!itemByName) {
        throw new Error(`No item named ${name}`);
    }
    const item = bot.inventory.findInventoryItem(itemByName.id);
    if (!item) {
        // ★ **必须抛，不能 return**：LLM 的建造循环常写成
        //   `for (pos of 一堆坐标) { try { await placeItem(...); placed++ } catch {} }`，
        //   静默 return 会让每一次「没放成」都照样 `placed++`，于是它**自己的日志在骗它**——
        //   即便 bot.chat 已经报了没有物品，它也可能被自己 console.log 的「✓」直接盖过。
        //   抛错才能让 catch 真的捕获到、让计数器停在真实值。
        throw new Error(
            `No ${name} in inventory —— 放置**没有发生**。` +
                `先看观察里的「库存」确认还剩多少，或用 craftHelper/mineBlock 补料再建造；` +
                `如果你在循环里建造，注意本次迭代没有放下任何方块。`
        );
    }
    const item_count = item.count;
    // find a reference block
    const faceVectors = [
        new Vec3(0, 1, 0),
        new Vec3(0, -1, 0),
        new Vec3(1, 0, 0),
        new Vec3(-1, 0, 0),
        new Vec3(0, 0, 1),
        new Vec3(0, 0, -1),
    ];
    let referenceBlock = null;
    let faceVector = null;
    for (const vector of faceVectors) {
        const block = bot.blockAt(position.minus(vector));
        if (block?.name !== "air") {
            referenceBlock = block;
            faceVector = vector;
            bot.chat(`Placing ${name} on ${block.name} at ${block.position}`);
            break;
        }
    }
    if (!referenceBlock) {
        // 「空中放方块」在建掩体时**成片出现**（先摆屋顶再摆墙，屋顶那一圈全是悬空的），
        //   静默返回会让 LLM 以为屋顶盖好了。它需要知道的是「换个顺序：先有邻接方块再往外长」，
        //   所以这里抛错而不是静默返回。
        _placeItemFailCount++;
        const msg =
            `No block to place ${name} on at (${position.x}, ${position.y}, ${position.z}) —— ` +
            `放置**没有发生**。这一格六个面都紧邻空气，而放方块必须**贴着已有方块**。` +
            `建墙/盖顶时改成从地面或已放好的方块**逐格往外长**，别先摆悬空的那一圈。`;
        bot.chat(msg);
        throw new Error(msg);
    }

    // You must use try catch to placeBlock
    try {
        // You must first go to the block position you want to place
        await bot.pathfinder.goto(new GoalPlaceBlock(position, bot.world, {}));

        // ★ **equip 竞态修复（见 memory openkage_arm_graph_bug_audit_20260820 的 #2）**：
        //   mineflayer 的 `bot.equip(item,'hand')`（simple_inventory.js:88）在物品位于快捷栏时
        //   只发一个 `held_item_slot` 包后立刻 `bot.updateHeldItem()` 就返回 —— 它读的是本地缓存
        //   `bot.inventory.slots[8]`，**不等服务器回 `held_item_slot` 确认**。
        //   于是 equip「返回无异常」但 `bot.heldItem` 可能仍是旧值（erin 第10–15天反复记到
        //   `equip(furnace,'hand')` 返回正常、item 却还留在 slot13、主手空，placeItem 紧接着报
        //   `must be holding an item to place`）。函数不抛错的竞态，靠重试无法在调用方无感知地自愈。
        //
        //   修法：equip 后**验证主手确实拿到了目标物品**，没拿到就等几 tick + 重试 equip（LRU
        //   抢快捷栏位的情形下，第二次 equip 命中的就是刚腾出的格）。验证用 `bot.heldItem?.type`：
        //   prismarine-item 把数字 id 放在 `.type`（同 mineBlock.js harvestTools 校验那处的教训，
        //   不是 `.id`）。仍拿不到才抛 —— 让既有的「靠世界状态判成败」catch 收口，而不是静默往下放空手。
        const _EQUIP_TRIES = 3;
        let _heldOk = false;
        for (let _e = 0; _e < _EQUIP_TRIES; _e++) {
            await bot.equip(item, "hand");
            // 等服务器确认主手栏更新（heldItemChanged 通常 1~2 tick 内 fire）
            for (let _t = 0; _t < 10; _t++) {
                if (bot.heldItem && bot.heldItem.type === item.type) {
                    _heldOk = true;
                    break;
                }
                await bot.waitForTicks(1);
            }
            if (_heldOk) break;
        }
        if (!_heldOk) {
            throw new Error(
                `must be holding an item to place —— equip(${name}) 后主手仍是 ` +
                `${bot.heldItem ? bot.heldItem.name : "空"}，` +
                `库存里 ${name} 的数量=${bot.inventory.count(item.type)}（equip 竞态，3 次重试后仍没握上）`
            );
        }
        // item 对象可能在 equip 的 clickWindow 过程中换了 slot，放完前重新取一份权威的
        const _held = bot.heldItem;

        // ★ **放置窗口修复（放置窗口的对称面，见历史放置超时问题）**：
        //   旧实现直接 `await bot.placeBlock(ref, face)` —— 它内部 `_genericPlace` 会
        //   `await bot.lookAt(ref.offset(dx,dy,dz), forceLook)`（forceLook 缺省 undefined=非'ignore'），
        //   这一眼 lookAt 跨好几个 physics tick（physics.js 要等 lastSentYaw 追上目标才放行），
        //   把放置窗口吃掉大半 → 服务端 block_place 等不到 blockUpdate 确认包 →
        //   `Event blockUpdate:(x,y,z) did not fire within timeout of 5000ms`。
        //   这正是 alice/bob/carol/erin 跑下来 placeItem 反复超时、erin 因此 6 天零进展的根因
        //   （见 memory openkage_arm_graph_bug_audit_20260820 的 #1）。
        //
        //   两步消除它（与底层安全放置逻辑完全同构）：
        //   (a) **force 预对准**：lookAt(target, true) 走 force 分支，瞬时设 lastSentYaw/Pitch 后返回、
        //       不 await lookingTask —— 把转向移到放置窗口之外。
        //       target 必须与 _genericPlace 对该 face 算出的点逐字节一致（generic_place.js:24-26：
        //       dx=0.5+face.x*0.5，dy/dz 同理），否则 forceLook:'ignore' 关掉 lookAt 后视线偏、
        //       服务器按视线方向拒绝放置 → 又超时。
        //   (b) **放置走 _placeBlockWithOptions({forceLook:'ignore'})**：跳过 _genericPlace 里的
        //       await lookAt，放置退化为「发包 + 等确认」，窗口内不再有转向等待。
        //       不能用 bot.placeBlock：它固定传 forceLook=undefined，会把 lookAt 等待带回窗口。
        //   (c) 残余极端抖动仍可能错过窗口 → 重试兜底（同本文件的放置重试上限）。
        const _dx = 0.5 + faceVector.x * 0.5;
        const _dy = 0.5 + faceVector.y * 0.5;
        const _dz = 0.5 + faceVector.z * 0.5;
        const _lookTarget = referenceBlock.position.offset(_dx, _dy, _dz);

        const _PLACE_TRIES = 3;
        let _placed = false;
        let _placeErr = "";
        for (let _attempt = 0; _attempt < _PLACE_TRIES; _attempt++) {
            // ★ **每次重试都重新 force 对准视线**（2026-08-24 修，对应 dave step70/71 火把反复超时）：
            //   旧实现把 lookAt 放在循环**外**只调一次，然后 3 次 _placeBlockWithOptions 都走
            //   `forceLook:'ignore'`（跳过 generic_place 内部的 lookAt）。问题是 force lookAt 虽是
            //   瞬时设 lastSentYaw/Pitch，但**服务器侧的视线插值要几个 tick 才追上**——第一次放置
            //   发出时服务器看到的视线还是旧的，对**贴附型方块（torch/告示牌）尤其致命**：服务器
            //   严格按视线方向判定火把贴哪面，视线没到位就静默拒绝（blockUpdate 不 fire →
            //   `Event blockUpdate did not fire within timeout of 5000ms`），而重试时视线仍没重新对准
            //   （forceLook:'ignore' 跳过了），于是 3 次全败在同一个错位的视线上。
            //   移进循环、每次重试前重新 force lookAt：第一次即使没追上，第二次时视线已稳定，
            //   放置命中。对普通方块（石头/泥土，放置点宽容）无副作用，对贴附方块是关键修复。
            await bot.lookAt(_lookTarget, true);
            try {
                await bot._placeBlockWithOptions(referenceBlock, faceVector, {
                    swingArm: "right",
                    forceLook: "ignore",
                });
                _placed = true;
                break;
            } catch (err) {
                _placeErr = (err && err.message) || String(err);
                // 确认包晚到的抖动：重试前等几 tick 让世界状态稳定，避免连发
                if (_attempt < _PLACE_TRIES - 1) {
                    await bot.waitForTicks(4);
                }
            }
        }
        if (!_placed) {
            // 把重试用尽的错误抛给下面 catch，按既有的「靠世界状态判成败」逻辑收口
            throw new Error(_placeErr || "placeBlock did not confirm");
        }
        bot.chat(`Placed ${name}`);
        bot.save(`${name}_placed`);
    } catch (err) {
        // ★ **靠世界状态判成败，不靠库存计数**（这是"造的和代码不一致"的一个隐蔽来源）。
        //   若判据是 `库存数量没变 → 真失败；变了 → 其实放成了`，会在**放最后一块**时判错：
        //   客户端库存有更新延迟，函数顶部的 findInventoryItem 可能读到过期的 count=1 而放行，
        //   服务器却因为实际没有物品而拒绝放置；进到这里时 findInventoryItem 已返回 null，
        //   于是 `undefined === 1` 为 false → 落进 else → **谎报 "Placed"**。
        //   世界状态是权威的（同「用 systemChat 死亡播报取代 chat 文案匹配」那次的思路）：
        //   放置成功当且仅当**目标格现在真的是那个方块**。
        const nowBlock = bot.blockAt(position);
        const nowName = nowBlock && nowBlock.name;
        // 少数物品放下后的方块名与物品名不同（torch 贴墙变 wall_torch 等），故放宽到前缀匹配
        const reallyPlaced =
            !!nowName &&
            (nowName === name ||
                nowName === `wall_${name}` ||
                nowName.startsWith(name));
        if (!reallyPlaced) {
            // **别把中止/寻路失败一律说成"位置不好"**。
            // `The goal was changed before it could be completed!` / `Took to long to decide path`
            // 是 /step 超时中止或寻路算不出路径，换个坐标毫无帮助；`must be holding an item to
            // place` 是 equip 竞态，重试即可。照着"换个位置"的错建议白试新坐标
            // 会白烧步数，而步数就是本域的评测预算。
            const msg = err.message || "";
            let hint;
            if (
                msg.includes("goal was changed") ||
                msg.includes("Took to long to decide path") ||
                msg.includes("Took too long to decide path")
            ) {
                hint =
                    "this was an abort or pathfinding failure, NOT a bad position — " +
                    "the spot may be fine; move closer yourself and retry, or raise timeout_ticks";
            } else if (msg.includes("No path to the goal")) {
                // ★ `No path to the goal!` **不是**"位置不好"，而是**你离得太远、根本够不着**。
                //   placeItem 内部走 `GoalPlaceBlock(position, …)`，它的 isEnd 要求「**头部 5 格
                //   内 + 有视线**」（见 mineflayer-pathfinder/lib/goals.js 的 getFaceAndRef：
                //   range 缺省 5、LOS 缺省 true）—— 也就是**遥控建造在物理上就不成立**。
                //   隔着地形/在地下往地表摆方块时最容易撞上，而旧提示"请换个位置"是最没用的
                //   建议（换哪个坐标都一样远），照着提示会白烧一整步。
                hint =
                    "你**离目标太远**：placeItem 要求你的头部在目标 5 格内且有视线（遥控建造不成立）。" +
                    "先 `bot.pathfinder.goto(new goals.GoalNear(x, y, z, 2))` 走到跟前，再逐格放置；" +
                    "隔着地形/在地下往地表摆方块**永远不会成功**";
            } else if (msg.includes("must be holding an item")) {
                hint = "equip race condition — just retry this step";
            } else if (msg.includes("did not fire within timeout")) {
                // ★ blockUpdate 超时（dave step70/71 火把反复栽在这）：放置发了包但服务器没确认。
                //   对贴附型方块（torch/告示牌）最常见——它们放置后变 wall_torch/wall_sign，服务器严格
                //   按视线方向 + 支撑面判定，视线没到位或支撑面不合法就静默拒绝。重试已重新对准视线
                //   （见上面 lookAt 进循环的修复），仍超时多半是支撑面问题：火把只能放**实心完整方块**
                //   的顶面或侧面，别往台阶/栅栏/玻璃这类非完整或透明方块上插。
                const _attachable = ["torch", "soul_torch", "redstone_torch"];
                const _isAttachable = _attachable.some(
                    (a) => name === a || name.endsWith("_sign")
                );
                if (_isAttachable) {
                    hint =
                        `放置贴附型方块（${name}）超时——多半是支撑面不合法：${name} 只能插在**实心完整方块**` +
                        `（石头/泥土/圆石/熔炉这类）的**顶面或侧面**，别往台阶/栅栏/玻璃/树叶这类非完整或透明方块上插。` +
                        `换个实心方块作支撑面、走到跟前重试；若支撑面没问题，可能是本步时限不够，申请更长 timeout_ticks。`;
                } else {
                    hint =
                        "放置确认包超时——走到目标跟前、确认目标格与邻接方块都如你预期，重试；" +
                        "仍不行就换个位置或为本步申请更长 timeout_ticks。";
                }
            } else {
                hint = "please find another position to place";
            }
            bot.chat(`Error placing ${name}: ${err.message}. ${hint}`);
            _placeItemFailCount++;
            if (_placeItemFailCount > 10) {
                throw new Error(
                    `placeItem failed too many times. 最后一次：${msg}。${hint}`
                );
            }
            // ★ **失败必须抛**（与上面「库存没有该物」同一个病）：若这里只 bot.chat 一句就
            //   正常返回，LLM 的 `try { await placeItem(...); placed++ }` 会**把失败也计成
            //   成功**——即便 console.log 打出了「✓ 放置」，坐标对应的其实是刚刚报错的那些格子。
            //   之后它会据此认为自己已经建好、放心过夜。
            //   bot.chat 仍然保留（它是即使 LLM 空 catch 也留得下的证据），但**必须同时抛**，
            //   否则「造的和代码说的不一致」这件事在它自己的观测里根本不可见。
            throw new Error(`Error placing ${name} at (${position.x}, ${position.y}, ${position.z}): ${msg}。${hint}`);
        } else {
            // 目标格现在确实是该方块 —— 抛错只是过程中的噪音（如 placeBlock 等确认包超时，
            // 但服务器其实放成了）。这才是可以正当报成功的唯一情形。
            bot.chat(`Placed ${name}`);
            bot.save(`${name}_placed`);
        }
    }
}
