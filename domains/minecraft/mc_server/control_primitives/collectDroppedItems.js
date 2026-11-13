/**
 * collectDroppedItems —— 「把附近地上的掉落物捡回库存」。
 *
 * ── 为什么需要这个原语 ──
 * 系统里**没有任何"捡地上东西"的入口**：mineBlock 捡它自己挖出的掉落物（collectBlock 内部
 * 监听 itemDrop 追加为 target 再走过去捡，见 node_modules/.../CollectBlock.js 的 mineBlock），
 * killMob/shoot 捂它们击杀的战利品。可一旦 LLM 退化为裸 `bot.dig`（只破坏方块、不拾取），
 * 或东西是被炸/被烧/被怪打掉落的，那些掉落物就永远躺在地上没人捡——bot 明明挖了却"圆石 0"
 * （alice step44/46/48 的真因：mineBlock 连续两步超时 → 改用 bot.dig → 圆石全掉地上）。
 *
 * 本原语补上这个缺口：扫 radius 内已存在的掉落实体，逐个走过去捡。它也是 mineBlock 超时收手
 * 后的补救手段——mineBlock 中途停手时，**在它自己循环里已挖落的那几块**会被它的拾取分支捡走，
 * 但更早被 bot.dig / 爆炸 / 旧战斗留下的掉落物它不管，那时就轮到本原语。
 *
 * ── 行为约定 ──
 * · radius 内没掉落物 → bot.chat 一句 + _failCount 累加（与 mineBlock 一致），不抛错
 * · 逐个捡，每件套自己的 LOOT_TICKS 预算（够不着就跳过、不抛——捡取是可选收益）
 * · 整体也套 maxTicks，到点停手
 * · 播报"捡了 N 件，跳过 M 件够不着"，库存变化用前后差给（比 collect 返回值可靠）
 *
 * ── 与 killMob 的拾取同构 ──
 * killMob 击杀后捡战利品那段（killMob.js 的 LOOT_TICKS 块）就是单件 collect + 超时 setGoal(null)，
 * 这里把它复用到"扫一堆"上：每件一个 Promise.race(collect, 超时)，超时跳过下一件。
 */
async function collectDroppedItems(bot, radius = 8, maxTicks = 200) {
    // return if radius is not number
    if (typeof radius !== "number" || !Number.isFinite(radius)) {
        throw new Error(
            `radius for collectDroppedItems must be a finite number (got ${String(radius)})`
        );
    }
    if (typeof maxTicks !== "number" || !Number.isFinite(maxTicks) || maxTicks < 1) {
        throw new Error(
            `maxTicks for collectDroppedItems must be a positive number of ticks (got ${String(maxTicks)})`
        );
    }
    // ★ 与 killMob/mineBlock 同口径：Agent 生活在 MC 时间里，刻是它的计时单位。
    //   墙钟 ms 只用于内部 setTimeout（库走墙钟），刻只用于给 Agent 的播报。
    const _rate = Math.max(1, Number(bot.openkageTickRate) || 1);
    const maxMs = (maxTicks / (20 * _rate)) * 1000;

    // 扫 radius 内的掉落实体。mineflayer 里掉落物的 entity.name === 'item'
    // （日志里 alice step63 已实测 `Object.values(bot.entities).filter(e => e.name==='item')` 能扫到）。
    // 按距 bot 升序，近的先捡——远的更可能超出本步时限，先捡近的保证收益。
    const drops = Object.values(bot.entities)
        .filter(
            (e) =>
                e &&
                e.name === "item" &&
                e.position &&
                e.position.distanceTo(bot.entity.position) <= radius
        )
        .sort(
            (a, b) =>
                a.position.distanceTo(bot.entity.position) -
                b.position.distanceTo(bot.entity.position)
        );

    if (drops.length === 0) {
        bot.chat(
            `附近 ${radius} 格内没有掉落物可捡 —— 如果你确实看到地上有东西，可能它不叫 'item' ` +
                `（如经验球 xp_orb 是另一回事、本原语不捡），或它已超过 ${radius} 格（调大 radius）。`
        );
        _collectDroppedItemsFailCount++;
        if (_collectDroppedItemsFailCount > 10) {
            throw new Error(
                `collectDroppedItems found nothing too many times — make sure there really are ` +
                    `dropped items on the ground nearby before calling it.`
            );
        }
        return;
    }

    // 单件捡取预算：够不着一件不该拖垮整批。killMob 用 200 刻捡单件战利品，这里件数更多，
    // 单件预算收窄到 60 刻（1x 下 3 秒，够走到身边几格的掉落物），整批由 maxTicks 兜底。
    const LOOT_TICKS_PER_ITEM = 60;
    const lootMsPerItem = (LOOT_TICKS_PER_ITEM / (20 * _rate)) * 1000;

    // 库存计数：捡取是异步的，用前后差判实得比 collect 返回值可靠（同 mineBlock 的 before/after）。
    // 这里只数"总件数变化"作粗略校验——掉落物可能叠成不同 id，精确计数无意义，捡到没捡到
    // 以 entityGone（collect 内部捡到后实体消失）为准。
    const inventoryCountBefore = bot.inventory.items().reduce((s, i) => s + i.count, 0);

    let picked = 0;
    let skipped = 0;
    const overallStart = Date.now();
    let overallTimer = null;

    try {
        for (const drop of drops) {
            // 整体预算到点：剩下的件数记入 skipped，停手。用 Date.now() 而非刻累加——墙钟是
            // setTimeout 同一套时间，与 bot.openkageStepDeadlineMs 同口径。
            if (Date.now() - overallStart >= maxMs) {
                skipped += drops.length - (picked + skipped);
                break;
            }
            // 实体可能在排队等待期间已被别人捡走 / 过期消失，跳过无效的（同 collectBlock.js:42）
            if (!drop.isValid) {
                skipped++;
                continue;
            }

            let itemTimer = null;
            try {
                await Promise.race([
                    bot.collectBlock.collect(drop, { ignoreNoPath: true }),
                    new Promise((_, rej) => {
                        itemTimer = setTimeout(
                            () =>
                                rej(
                                    new Error(
                                        `pickup exceeded ${LOOT_TICKS_PER_ITEM} ticks`
                                    )
                                ),
                            lootMsPerItem
                        );
                    }),
                ]);
                picked++;
            } catch (err) {
                // 单件够不着：停寻路、跳到下一件。掉落物可能落在够不着的地方（水里/悬崖下/
                // 实体包围），无限等会把整批预算烧光——与 killMob 的 loot 超时同一理由。
                try {
                    bot.pathfinder.setGoal(null);
                } catch (e) {
                    /* ignore */
                }
                skipped++;
            } finally {
                if (itemTimer) clearTimeout(itemTimer);
            }
        }
    } finally {
        if (overallTimer) clearTimeout(overallTimer);
    }

    const inventoryCountAfter = bot.inventory.items().reduce((s, i) => s + i.count, 0);
    const gained = inventoryCountAfter - inventoryCountBefore;

    // 播报：捡到几件 + 跳过几件 + 库存净增。picked 是"collect 没抛错"的件数，gained 是库存实增，
    // 两者可能不等（叠堆、或 collect 成功但库存满没真正进包）——都以事实报，让 LLM 自己对账。
    if (picked > 0) {
        bot.chat(
            `捡了 ${picked} 件掉落物` +
                (skipped > 0
                    ? `，跳过 ${skipped} 件（够不着或本步时限到）`
                    : "") +
                `，库存净增 ${gained}。`
        );
    } else {
        bot.chat(
            `${drops.length} 件掉落物一件都没捡到（都够不着或本步时限到）—— 它们可能在水里/悬崖下，` +
                `走近后重试，或本步申请更长 timeout_ticks。`
        );
        _collectDroppedItemsFailCount++;
        if (_collectDroppedItemsFailCount > 10) {
            throw new Error(
                `collectDroppedItems picked up nothing too many times — the drops may be ` +
                    `permanently unreachable (water/cliff). Move closer or give up on them.`
            );
        }
    }
}
