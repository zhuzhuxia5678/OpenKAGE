async function killMob(bot, mobName, timeout = 6000) {
    // return if mobName is not string
    if (typeof mobName !== "string") {
        throw new Error(`mobName for killMob must be a string`);
    }
    // return if timeout is not number
    if (typeof timeout !== "number") {
        throw new Error(`timeout for killMob must be a number`);
    }
    // ★ timeout 单位是**游戏刻**（与 timeout_ticks / waitUntil.maxTicks 同口径）：Agent 生活在
    //   MC 时间里，世界以 20 刻/秒流逝，战斗时限也按刻给。6000 刻 = 300 秒（1x）= 旧默认值。
    //   timeout 必须是正数：传 0 或负数时换算成 0ms 的 `setTimeout` 会在**第一个事件循环 tick
    //   就触发** → `bot.pvp.stop()` 在 `bot.pvp.attack()` 真正开打之前就调用，会抛一个纯内部
    //   错误（LLM 完全看不出是自己的 timeout 参数写错了）。
    //   钳到 [20, 12000] 刻：下限保证攻击链跑得起来，上限防止一步等满整轮预算。
    if (!Number.isFinite(timeout) || timeout < 1) {
        throw new Error(
            `timeout for killMob must be at least 20 ticks (got ${timeout}) — ` +
                `it is how long to keep fighting before giving up.`
        );
    }
    timeout = Math.min(timeout, 12000);
    // 战斗预算换算成墙钟 ms 喂给 setTimeout（原语内部沿用墙钟）：`刻 / (20*rate) * 1000`，
    //   与倍速 N 无关地维持「6000 刻就是 6000 刻」的 Agent 体验。
    const _rate = Math.max(1, Number(bot.openkageTickRate) || 1);
    const timeoutMs = (timeout / (20 * _rate)) * 1000;

    const weaponsForShooting = [
        "bow",
        "crossbow",
        "snowball",
        "ender_pearl",
        "egg",
        "splash_potion",
        "trident",
    ];
    const mainHandItem = bot.inventory.slots[bot.getEquipmentDestSlot("hand")];

    const entity = bot.nearestEntity(
        (entity) =>
            entity.name === mobName &&
            // kill mob distance should be slightly bigger than explore distance
            entity.position.distanceTo(bot.entity.position) < 48
    );
    if (!entity) {
        bot.chat(`No ${mobName} nearby, please explore first`);
        _killMobFailCount++;
        if (_killMobFailCount > 10) {
            throw new Error(
                `killMob failed too many times, make sure you explore before calling killMob`
            );
        }
        return;
    }

    // ★ **打到一半没收手就返回**是 killMob 的核心契约（prompt 承诺「等到战果才返回，明说击杀了还是还活着」）。
    //   waitForMobRemoved / waitForMobShot 在「超时但没杀死」时 reject（message=干巴巴的 `Failed to kill X`）。
    //   旧实现不 catch，让它穿透成步级 `Your code:N\n Failed to kill zombie.` —— Agent 既看不到「还活着」，
    //   也无从判断是超时、打不过、还是怪跑出范围（这正是 mineBlock 那次「超时只报动作超时 → Agent 编错因果」
    //   的同构病根，见 memory openkage_primitive_intent_feedback）。shoot.js 同样调 waitForMobShot 却包了 catch
    //   报「还活着 + 原因」，本函数照抄它：catch 里如实说「还活着」+ 最可能的原因，不要裸抛。
    //   真的 entityGone 时 waitForMob* 已在内部 `bot.chat('Killed X!')` 并 resolve，不会走 catch。
    let droppedItem = null;
    try {
        if (mainHandItem && weaponsForShooting.includes(mainHandItem.name)) {
            bot.hawkEye.autoAttack(entity, mainHandItem.name);
            droppedItem = await waitForMobShot(bot, entity, timeoutMs);
        } else {
            await bot.pvp.attack(entity);
            droppedItem = await waitForMobRemoved(bot, entity, timeoutMs);
        }
    } catch (killErr) {
        // 还活着 / 没在 timeout 内打死。pvp/hawkEye 的攻击循环可能仍在后台驱动 bot，务必停掉。
        try { bot.pvp.stop(); } catch (e) { /* ignore */ }
        try { if (bot.hawkEye && bot.hawkEye.stop) bot.hawkEye.stop(); } catch (e) { /* ignore */ }
        // ★ 只报事实与准确意图，不替 Agent 决定下一步：说清「还活着」+ 这次攻击用什么、最可能的失败原因，
        //   不写「别用 killMob」这类禁止性指令（同 mineBlock 那条 memory 的原则）。
        const _wielding = mainHandItem ? mainHandItem.name : "空手";
        // 最常见三因：①空手/拿镐砍伤害极低 → 打不死；②怪在战斗中跑出 48 格或脱战回血；③timeout 给得太短。
        // 按手里武器给中性可达性建议，不是替它下决定。
        let _hint;
        if (!mainHandItem) {
            _hint = `你空手上阵，伤害很低、很难打死它 —— 先 bot.equip(剑,'hand') 再来，或先撤。`;
        } else if (mainHandItem.name.endsWith("_pickaxe") || mainHandItem.name.endsWith("_axe")
                   || mainHandItem.name.endsWith("_shovel") || mainHandItem.name.endsWith("_hoe")) {
            _hint = `你拿的是 ${_wielding}（非武器），伤害很低 —— 换成剑（bot.equip(剑,'hand')）再打更高效。`;
        } else if (weaponsForShooting.includes(mainHandItem.name)) {
            _hint = `可能在射程外/被遮挡/箭耗尽 —— 靠近、清出视线或确认弹药。`;
        } else {
            _hint = `可能它在战斗中跑远了或 timeout 给得不够 —— 靠近后重试，或为本步申请更长 timeout_ticks。`;
        }
        bot.chat(
            `打了 ${mobName} 但它**还活着**（${timeout} 刻内没打死，手里是 ${_wielding}）。` +
                _hint +
                `（这场没杀成，掉落物也没有。）`
        );
        return;
    }
    // 走到这里 = 真的杀了（waitForMob* 已在内部 chat 'Killed X!' / 'Shot X!' 并 resolve）。
    if (droppedItem) {
        // 拾取掉落物必须限时：`collect` 走寻路，掉落物可能落在够不着的地方（水里/悬崖下/
        //   实体包围），无限等会把整步预算烧光并以"动作超时"收场——而击杀其实已经成功，
        //   那份战果反馈会因为超时中止而白丢。战利品是可选收益，不该拖垮整步。
        // ★ LOOT_TICKS=200 刻（1x 下 10 秒）：以游戏刻计量，与 timeout_ticks 同口径。换算成墙钟 ms 喂
        //   setTimeout（同 timeoutMs 的折算）。
        const LOOT_TICKS = 200;
        const lootMs = (LOOT_TICKS / (20 * _rate)) * 1000;
        let lootTimer = null;
        try {
            await Promise.race([
                bot.collectBlock.collect(droppedItem, { ignoreNoPath: true }),
                new Promise((_, rej) => {
                    lootTimer = setTimeout(
                        () => rej(new Error(`loot pickup exceeded ${LOOT_TICKS} ticks`)),
                        lootMs
                    );
                }),
            ]);
        } catch (lootErr) {
            try {
                bot.pathfinder.setGoal(null);
            } catch (e) {
                /* ignore */
            }
            bot.chat(
                `Killed the ${mobName} but could not pick up its drop within ${LOOT_TICKS} ticks ` +
                    `— it may be unreachable. The kill still counts.`
            );
        } finally {
            if (lootTimer) clearTimeout(lootTimer);
        }
    }
    bot.save(`${mobName}_killed`);
}
