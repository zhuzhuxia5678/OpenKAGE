// shoot 1 pig with a bow: shoot(bot, "bow", "pig");
async function shoot(bot, weapon, target) {
    const validWeapons = [
        "bow",
        "crossbow",
        "snowball",
        "ender_pearl",
        "egg",
        "splash_potion",
        "trident",
    ];
    if (!validWeapons.includes(weapon)) {
        bot.chat(`${weapon} is not a valid weapon for shooting`);
        return;
    }

    const weaponItem = mcData.itemsByName[weapon];
    if (!bot.inventory.findInventoryItem(weaponItem.id, null)) {
        bot.chat(`No ${weapon} in inventory for shooting`);
        return;
    }

    // ★ **必须限制搜索距离**：`nearestEntity` 不带距离条件会把几十甚至上百格外的目标也算
    //   "找到"，而弓的有效射程远不到那么远——函数会照常返回、一支箭都没射出去，LLM 收到的是
    //   "没报错"，只能理解成"我射了"，于是继续等一个永远不会到的战果。
    //   与 killMob 对齐用 48 格（=观察视野），超出就明说要先靠近。
    const SHOOT_R = 48;
    const targetEntity = bot.nearestEntity(
        (entity) =>
            entity.name === target &&
            entity.position.distanceTo(bot.entity.position) < SHOOT_R
    );
    if (!targetEntity) {
        // 区分"附近根本没有这种生物"与"有但太远"：两者的下一步动作完全不同。
        const anyFar = bot.nearestEntity((e) => e.name === target);
        if (anyFar) {
            const d = Math.round(anyFar.position.distanceTo(bot.entity.position));
            bot.chat(
                `${target} is ${d} blocks away — too far to shoot (I only engage within ` +
                    `${SHOOT_R} blocks). Walk closer first, then call shoot again.`
            );
        } else {
            bot.chat(`No ${target} nearby`);
        }
        return;
    }
    // 用**真实的 weapon**，别硬编码 "bow"：传 crossbow/snowball/trident 若被静默当成 bow
    // 处理，且没有任何提示，LLM 无从知道自己的参数被忽略了。对比 killMob.js 传的是
    // mainHandItem.name（那边是对的）。
    bot.hawkEye.autoAttack(targetEntity, weapon);
    // ★ **必须等射击结束再返回**：`autoAttack` 是即发即返的，若函数就此返回、不等结果，
    //   LLM 无法区分"射死了""没射中""压根没射"——这与 mineBlock 那次「成功时不播报 →
    //   LLM 编出库存不同步幻觉」是同一个病根。
    //   用与 killMob 相同的 waitForMobShot 等 `entityGone` / `auto_shot_stopped`，
    //   并把战果如实说出来。射击预算 600 刻（1x 下 30 秒）：够射几轮，又不至于把整步预算烧光。
    //   ★ 以游戏刻计量、按当前倍速换算成墙钟 ms 喂给 waitForMobShot（其内部是 setTimeout 墙钟）。
    const _rate = Math.max(1, Number(bot.openkageTickRate) || 1);
    const SHOT_TICKS = 600;
    try {
        const dropped = await waitForMobShot(bot, targetEntity, (SHOT_TICKS / (20 * _rate)) * 1000);
        bot.chat(`Shot down the ${target} with the ${weapon}.`);
        if (dropped) {
            // ★ **拾取掉落物必须限时**：`collect` 走的是寻路，掉落物可能落在够不着的地方
            //   （水里/悬崖下/被实心包围），无限等会把整步预算烧光、最后以"动作超时"收场——
            //   战果播报虽然发出去了，但后面的代码全没跑。
            //   战利品是可选收益，不该让它拖垮整步：给 200 刻（1x 下 10 秒）上限，超时就放弃拾取并说明。
            const LOOT_TICKS = 200;
            let lootTimer = null;
            try {
                await Promise.race([
                    bot.collectBlock.collect(dropped, { ignoreNoPath: true }),
                    new Promise((_, rej) => {
                        lootTimer = setTimeout(
                            () => rej(new Error(`loot pickup exceeded ${LOOT_TICKS} ticks`)),
                            (LOOT_TICKS / (20 * _rate)) * 1000
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
                    `Killed the ${target} but could not pick up its drop within ` +
                        `${LOOT_TICKS} ticks — it may be unreachable. The kill still counts.`
                );
            } finally {
                if (lootTimer) clearTimeout(lootTimer);
            }
        }
    } catch (err) {
        // waitForMobShot 在"停了但没杀死"时 reject —— 那是真实结果，如实转达而不是装成成功。
        bot.hawkEye.stop();
        bot.chat(
            `Shot at the ${target} with the ${weapon} but it is still alive ` +
                `(${String(err && err.message).slice(0, 60)}). It may be out of range, behind ` +
                `cover, or you ran out of ammo — check your arrows and line of sight.`
        );
    }
}
