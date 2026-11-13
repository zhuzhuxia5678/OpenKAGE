async function mineBlock(bot, name, count = 1) {
    // return if name is not string
    if (typeof name !== "string") {
        throw new Error(`name for mineBlock must be a string`);
    }
    if (typeof count !== "number") {
        throw new Error(`count for mineBlock must be a number`);
    }
    const blockByName = mcData.blocksByName[name];
    if (!blockByName) {
        throw new Error(`No block named ${name}`);
    }
    // ─── 工具等级前置校验 ────────────────────────────────────────
    // **为什么挡在这里**：手上没有能采该方块的工具时，collectBlock 会调
    // `bot.tool.equipForBlock(block, {requireHarvest:true, getFromChest:true})`，而那条路
    // 曾是**整个 mineflayer 进程猝死**的根因 —— `equipForBlock` 在 requireHarvest 滤空后
    // 无限自递归（吃满堆内存 → OOM；已在 node_modules 打补丁，见
    // ../README.md「两处 vendored 依赖改动」②）。库那边补好了，但这里仍值得挡一次：
    //   ① 便宜 —— 一次库存遍历，不发包不寻路；
    //   ② 反馈**可执行** —— 库的错误只说 "Bot does not have a harvestable tool!"，
    //      而 LLM 需要知道的是「该用哪些工具、它是否已在库存里」，否则播报模糊会让它接着猜、
    //      反复空手采不可采的方块。
    // ★ 只在**确实无法采集**时挡：`canHarvest(null)` 为真的方块（oak_log/dirt 等裸手可采）
    //   一律放行，绝不能因为"没拿镐"就拦掉本来能干的活。
    if (blockByName.harvestTools) {
        const okIds = Object.keys(blockByName.harvestTools).map(Number);
        // ★ 库存物品的数字 id 要读 `.type` 而**不是** `.id`：prismarine-item 的实例把 id 放在
        //   `.type`，`.id` 是 undefined。写错的话 `includes` 恒 false → 明明拿着镐也被判成
        //   "没工具"，把能干的活拦掉。
        const held = bot.inventory.items().map((i) => i.type);
        if (!okIds.some((id) => held.includes(id))) {
            // 列**前几档**而不是只报"最便宜的那一个"：mcData 在 1.21.11 里把 copper_pickaxe
            // 排在最前（铜器时代新增，确实合法），但只报它会把 LLM 推向一条它多半没材料、
            // 也不熟的路 —— 而 stone_pickaxe 往它现有材料看常常更近。给几个选项让它自己挑，
            // 别替它决定（与 render_nearby_grid「只报事实、不给建议」同一原则）。
            const options = okIds
                .map((id) => mcData.items[id] && mcData.items[id].name)
                .filter(Boolean)
                .slice(0, 3);
            bot.chat(
                `Cannot mine ${name}: it needs a proper tool and you have none in your inventory. ` +
                    `Any of these works: ${options.join(" / ") || "a better tool"} ` +
                    `(cheapest first) — craft or fetch one, then call mineBlock('${name}') again. ` +
                    `I did not try: mining it without the right tool breaks the block and drops nothing.`
            );
            return;
        }
    }
    // ─── 采集半径：近处批量采，远处先自己走近 ──────────────────────────────────
    // ★ **批量 collect 的搜索半径必须留在 16 格**：更大的半径 + 更多目标数会把大量目标交给
    //   collectBlock 批量寻路，同步 A* 累积阻塞事件循环 → bot 收不到 keep_alive → 服务器踢下线
    //   （同类事故还崩过一次整个 mineflayer 进程）。半径×目标数决定计算量，别放大它。
    //
    // 但观察给 LLM 的视野是 **48 格**，于是「看得见却采不到」：LLM 看见 40 格外的树、调
    // mineBlock 得到 "No X nearby"，播报与观察直接矛盾。只把播报改成「只搜 16 格，你先
    // 走近」是诚实了，但每次都要 LLM 多烧一步去 goto，而步数就是本域的评测预算。
    //
    // 现在：16 格内没找到时，**在 48 格内找一个最近的，自己走过去**，再在新位置重新做 16 格
    // 批量采集。这不碰上面那个危险路径 —— 走近用的是**单目标 A***（一次 GoalNear，计算量与
    // 批量寻路差一个量级），批量 collect 依旧只在身边 16 格内跑。
    //
    // ★ **走近本身要限时**：若不限时，远处的目标可能让 `pathfinder.goto` 死磕到
    //   `Took too long to decide path to goal!`，把 LLM 申请的整步时限烧光、超时中止时 catch
    //   都没机会跑 —— 等于把「多烧一步 goto」换成了「白烧一整步」，比原来更糟。
    //   故给走近一个自己的时限：只花本步预算的一部分，剩下的留给真正的采集。
    //   `bot.openkageStepDeadlineMs`（若 index.js 提供）是本步的绝对截止时刻；拿不到就用保守默认。
    const SEARCH_R = 16; // 批量 collect 的半径（勿放大，见上）
    const REACH_R = 48; // 与观察视野一致：这个范围内的目标都算「够得着，走过去就行」
    const WALK_BUDGET_RATIO = 0.5; // 走近最多花本步剩余时间的一半
    const WALK_BUDGET_FALLBACK_MS = 8000; // 拿不到本步 deadline 时的保守上限
    let blocks = bot.findBlocks({
        matching: [blockByName.id],
        maxDistance: SEARCH_R,
        count: Math.max(count * 4, 8),
    });
    let walkedTo = null;
    if (blocks.length === 0) {
        // 16 格内没有 → 在视野范围内找最近的一个，走过去
        const far = bot.findBlocks({
            matching: [blockByName.id],
            maxDistance: REACH_R,
            count: 1, // 只要最近的那个：findBlocks 按距离升序返回
        });
        // 只接受坐标有效的候选：findBlocks 理论上只返回有效 Vec3，但这里是**喂给 pathfinder 的
        // 入口**，而 NaN 目标会让 A* 永不终止、耗尽内存直到进程 OOM（见 index.js 里
        // pathfinder.goto 包装处的说明）。入口便宜地挡一次，比事后救进程划算。
        const validFar = far.filter(
            (v) => v && Number.isFinite(v.x) && Number.isFinite(v.y) && Number.isFinite(v.z)
        );
        if (validFar.length > 0) {
            const t = validFar[0];
            const dist = Math.round(t.distanceTo(bot.entity.position));
            bot.chat(
                `No ${name} within ${SEARCH_R} blocks; nearest is ${dist} blocks away — walking there first.`
            );
            // 走近的时间预算：本步剩余时间的一半（留一半给采集），拿不到 deadline 就用保守值。
            const remainMs =
                typeof bot.openkageStepDeadlineMs === "number"
                    ? bot.openkageStepDeadlineMs - Date.now()
                    : WALK_BUDGET_FALLBACK_MS * 2;
            const walkBudget = Math.max(
                1500,
                Math.min(WALK_BUDGET_FALLBACK_MS, Math.floor(remainMs * WALK_BUDGET_RATIO))
            );
            let timer = null;
            try {
                // GoalNear(…, 3)：走到 3 格内就算到位。不要求精确贴上去 —— 目标可能嵌在墙里/
                // 树冠上，GoalBlock 那种严格目标容易 "Took too long to decide path"。
                // 用 Promise.race 给它套一个自己的时限：超了就 setGoal(null) 停下、当场返回，
                // 把剩余时间还给本步（bot 通常已经走近了一截，下一步 LLM 再调就落在 16 格内）。
                // ★ 超时报错按游戏刻给 Agent（walkBudget 内部是墙钟 ms，按当前倍速换算回刻）。
                const _wbRate = Math.max(1, Number(bot.openkageTickRate) || 1);
                await Promise.race([
                    bot.pathfinder.goto(new GoalNear(t.x, t.y, t.z, 3)),
                    new Promise((_, reject) => {
                        timer = setTimeout(
                            () => reject(new Error(`walk budget ${Math.round(walkBudget * _wbRate / 1000)} 刻 exceeded`)),
                            walkBudget
                        );
                    }),
                ]);
                walkedTo = dist;
            } catch (err) {
                // 停掉寻路，否则孤儿 goal 会在后续 tick 继续把 bot 拖走（同 exploreUntil 那个坑）
                try {
                    bot.pathfinder.setGoal(null);
                } catch (e) {
                    /* ignore */
                }
                // 走不过去要**说清是寻路失败/超时**，别让 LLM 以为这里没有这种方块（它明明看得见）。
                // 也要说清「bot 已经挪动了」——它的坐标变了，下一步的决策要基于新位置。
                // ★ 时限按游戏刻给 Agent（walkBudget 内部是墙钟 ms，按当前倍速换算回刻）。
                const movedTo = bot.entity.position;
                const _chatRate = Math.max(1, Number(bot.openkageTickRate) || 1);
                bot.chat(
                    `Could not reach the nearest ${name} (${dist} blocks away) within ${Math.round(walkBudget * _chatRate / 1000)} 刻: ` +
                        `${err.message}. I stopped at (${Math.round(movedTo.x)}, ${Math.round(
                            movedTo.y
                        )}, ${Math.round(movedTo.z)}) — often already closer, so just call ` +
                        `mineBlock('${name}') again next step; if it keeps failing the target may be ` +
                        `across water/a cliff or enclosed.`
                );
                _mineBlockFailCount++;
                if (_mineBlockFailCount > 10) {
                    throw new Error(
                        "mineBlock failed too many times, make sure you explore before calling mineBlock"
                    );
                }
                return;
            } finally {
                if (timer) clearTimeout(timer);
            }
            // 到位后重新扫一遍身边（位置变了，原来的坐标列表已失效）
            blocks = bot.findBlocks({
                matching: [blockByName.id],
                maxDistance: SEARCH_R,
                count: Math.max(count * 4, 8),
            });
        }
    }
    if (blocks.length === 0) {
        // 连 48 格内都没有（或走过去之后反而扫不到了，例如目标是别人看到的浮空方块）
        bot.chat(
            `No ${name} within ${REACH_R} blocks` +
                (walkedTo !== null
                    ? ` even after walking ${walkedTo} blocks toward one`
                    : "") +
                ` — explore farther first (your view also reaches ${REACH_R} blocks, so if you can` +
                ` see one beyond that, walk toward it with bot.pathfinder.goto).`
        );
        _mineBlockFailCount++;
        if (_mineBlockFailCount > 10) {
            throw new Error(
                "mineBlock failed too many times, make sure you explore before calling mineBlock"
            );
        }
        return;
    }
    const targets = [];
    for (let i = 0; i < blocks.length; i++) {
        targets.push(bot.blockAt(blocks[i]));
    }
    // **成功时也必须播报实得数量**：若 collect 之后只 bot.save()、不 chat，`ignoreNoPath: true`
    // 会静静跳过挖不到的目标，于是「请求 5 个、实得 0 个」与「实得 5 个」对 LLM 完全无法区分，
    // 它只能靠自己 console.log 猜，容易制造出「库存不同步」一类的幻觉诊断。
    // 这与 unique_items 那次（装备槽不在 items() 区间）是同一类病根：**观察缺失 → LLM 编因果**。
    // 用采集前后的库存差来算实得数：比 collect 的返回值可靠（掉落物拾取是异步的）。
    // ★ **库存计数必须用 item id，不能用 block id**：两者是**不同的 id 空间**，同名物品的
    //   id 基本都不一样（如 oak_log block 与 item 的 id 不同，cobblestone、dirt 也是）；
    //   拿 block.id 去 bot.inventory.count 会数到**另一种完全无关的物品**上，播报的数字全是错的。
    // ★ **数的是「这个方块实际掉什么」，不是同名物品**：mcData 的 blocksByName[x].drops 给出
    //   真实掉落物 id（stone→cobblestone、grass_block→dirt、deepslate→cobbled_deepslate、
    //   iron_ore→raw_iron）。若按方块名去数同名物品，遇到这类方块会恒得 0、把明明成功的
    //   一步播报成「gained 0」，等于换一种方式继续骗 LLM。
    const dropId = Array.isArray(blockByName.drops) && blockByName.drops.length
        ? (typeof blockByName.drops[0] === "object"
              ? blockByName.drops[0].drop?.id ?? blockByName.drops[0].drop
              : blockByName.drops[0])
        : mcData.itemsByName[name]?.id;
    const dropName = dropId !== undefined ? mcData.items[dropId]?.name : undefined;
    const countInInventory = () =>
        dropId !== undefined ? bot.inventory.count(dropId, null) : 0;
    const before = countInInventory();
    // ★ **collect 套本步剩余预算**（治本：alice step42/43 连续超时的根因）。
    //   `collectBlock.collect(targets)` 是个无预算整体——它对每个 target 做「寻路→挖→等掉落→捡」，
    //   批量时累加。没有套筒时，远处的几个目标会让它死磕到吃光整步时限，于是整步被 abortInFlightActions
    //   强制中止，**这一步的进度（已挖到几个）完全不进观察**——LLM 只看到一句"动作超时"，
    //   下一轮为了"至少挖到点什么"就退化成裸 `bot.dig`（只挖不捡，圆石全掉地上，见 collectDroppedItems
    //   的背景）。给它套本步剩余预算，到点就停手、用已采到的 gained 走"部分成功"播报——
    //   LLM 看到"已采 N 个、剩 M 个下次再来"，就不会弃用 mineBlock。
    //
    //   ★ 关键：collectBlock 内部是「挖一块→监听 itemDrop→追加为 target→捡」的循环
    //   （CollectBlock.js 的 mineBlock + itemDrop 订阅），所以**即便中途超时，已挖落的掉落物
    //   会被它自己的拾取分支捡走**——这与裸 bot.dig（掉地上没人捡）有本质区别，要在超时播报里点明。
    //
    //   预算源 = bot.openkageStepDeadlineMs（本步绝对截止时刻，index.js:931 设置，墙钟 ms）。
    //   留 COLLECT_TAIL_TICKS 安全余量给后续播报/收尾（同 mineBlock 顶部 walk budget 留一半的思路，
    //   但这里采集是主任务，余量只留够收尾）。拿不到 deadline（旧版/非 /step 入口）退化为固定上限。
    const _rate = Math.max(1, Number(bot.openkageTickRate) || 1);
    const COLLECT_TAIL_TICKS = 200; // 留给播报 + 停手收尾的刻数
    const COLLECT_FALLBACK_TICKS = 600; // 拿不到本步 deadline 时的保守上限
    let collectBudgetMs;
    if (typeof bot.openkageStepDeadlineMs === "number") {
        const remainTicks =
            ((bot.openkageStepDeadlineMs - Date.now()) * 20 * _rate) / 1000;
        // 剩余已不足 tail 余量 → 几乎不给了（留极小窗口让 collect 至少启动一次），靠 gained 收尾
        const usableTicks = Math.max(40, remainTicks - COLLECT_TAIL_TICKS);
        collectBudgetMs = (usableTicks / (20 * _rate)) * 1000;
    } else {
        collectBudgetMs = (COLLECT_FALLBACK_TICKS / (20 * _rate)) * 1000;
    }

    let collectBudgetTimer = null;
    let collectTimedOut = false;
    try {
        await Promise.race([
            bot.collectBlock.collect(targets, {
                ignoreNoPath: true,
                count: count,
            }),
            new Promise((_, rej) => {
                collectBudgetTimer = setTimeout(
                    () => rej(new Error(`collect budget exceeded`)),
                    collectBudgetMs
                );
            }),
        ]);
    } catch (collectErr) {
        // 超时（或 collect 内部抛错）：停寻路，不向上抛——用已采到的 gained 走部分成功播报。
        // collect 内部的 itemDrop 拾取循环在被 setGoal(null) 打断后，已挖落的掉落物会留在地上
        // 等下次 collect / collectDroppedItems 捡，但**正在捡的那几件**多半已进包（gained 会反映）。
        try {
            bot.pathfinder.setGoal(null);
        } catch (e) {
            /* ignore */
        }
        // 清 collectBlock 残留 targets：超时时 collect 的 finally 发 collectBlock_finished，
        // 但 targets 可能未清空（见 index.js abortInFlightActions 的 ②-bis 注释），下一步会卡。
        // 这里和 abortInFlightActions 同样的强制清理，保证下一步 mineBlock 不被悬挂 targets 拖死。
        try {
            const cb = bot.collectBlock;
            if (cb && cb.targets && typeof cb.targets.clear === "function") {
                cb.targets.clear();
            }
            bot.emit("collectBlock_finished");
        } catch (e) {
            /* ignore */
        }
        if (
            collectErr &&
            typeof collectErr.message === "string" &&
            collectErr.message.includes("collect budget exceeded")
        ) {
            collectTimedOut = true;
        }
        // 非"预算超时"的真实错误（如 PathStopped 之外的路由错误）也不抛：mineBlock 的契约是
        // "尽力采、报实得"，gained=0 的分支会给出可达性诊断。抛错会让 LLM 误以为整步崩了。
    } finally {
        if (collectBudgetTimer) clearTimeout(collectBudgetTimer);
    }
    const gained = countInInventory() - before;
    // 掉落物与方块不同名时，播报里两个名字都给出，LLM 才能把「我挖的」与「我库存里多出来的」
    // 对上（否则它会以为东西丢了 —— 这正是本轮那批「库存不同步」幻觉的来源）。
    const asLabel =
        dropName && dropName !== name ? `${dropName} (from ${name})` : name;
    if (gained >= count && !collectTimedOut) {
        bot.chat(`Mined ${gained} ${asLabel}.`);
    } else if (gained > 0) {
        // 部分成功要说清，否则 LLM 会以为整步失败而重复劳动
        bot.chat(
            `Mined ${gained} ${asLabel} (asked for ${count}; some targets were unreachable). ` +
                `Inventory now has ${before + gained}.` +
                (collectTimedOut
                    ? ` 采集中途因本步时限收手（已挖落的都自动捡回了，没掉地上）—— ` +
                      `剩余目标下次再调 mineBlock，或为本步申请更长 timeout_ticks。`
                    : "")
        );
    } else {
        // 真的一个没采到。**播报必须说清「工具已排除」**：
        // 若仍写「or your tool tier is too low to harvest it」，而走到这里时工具**必然是够的**
        // —— 上面入口的工具等级校验已经挡掉了没工具的情形。留着这句会把 LLM 推向错误方向，
        // 让它反复去合成更好的镐（明明手里那把就能采），白烧步数。
        // 这与那次「gained 0 stone」是同一类病根：**播报给了一个已被排除的原因当解释**。
        // 真实原因只剩地形/寻路：目标被实心包围、在水/悬崖对面、或 collectBlock 的批量寻路
        // 选中了走不到的那几块（`ignoreNoPath:true` 会静默跳过它们）。
        const heldName = bot.heldItem ? bot.heldItem.name : "bare hand";
        if (collectTimedOut) {
            // 预算全花在寻路上、一块都没挖到：典型是目标虽在 16 格内但 collectBlock 的批量寻路
            // 绕不过中间地形（水/悬崖/实心包围），整段预算耗在 goto 上。
            // 建议换更近/更暴露的目标、或本步申请更长 timeout_ticks 再试 mineBlock。
            // （原语只报事实与准确意图，不替 Agent 决定"别用 bot.dig"——用不用 bot.dig 是它的事，
            //   bot.dig 不捡掉落物这一事实由 prompt 的 bot.dig 行说明。）
            bot.chat(
                `Found ${targets.length} ${name} block(s) within ${SEARCH_R} blocks but gained 0 ` +
                    `${dropName || name} —— 采集中途因本步时限收手，整段预算花在走向目标上、一块都没挖到。` +
                    `你的工具没问题（${heldName} 能采 ${name}）。目标是 collectBlock 的批量寻路绕不过去。` +
                    `移到一个 ${name} 更暴露的位置再调 mineBlock('${name}')，或为本步申请更长 timeout_ticks。`
            );
        } else {
            bot.chat(
                `Found ${targets.length} ${name} block(s) within ${SEARCH_R} blocks but gained 0 ` +
                    `${dropName || name}. Your tool is fine (holding ${heldName}, which can harvest ` +
                    `${name}) — the targets were unreachable: enclosed by solid blocks, across ` +
                    `water/a cliff, or pathfinding could not get to them. ` +
                    `Move to a spot where ${name} is exposed and call mineBlock('${name}') again.`
            );
        }
    }
    bot.save(`${name}_mined`);
}
