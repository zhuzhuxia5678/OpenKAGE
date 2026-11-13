/**
 * digDownTo —— 「向下挖一条竖井并跟着掉下去」。
 *
 * ── 为什么需要这个原语 ──
 * 向下挖此前没有专门的原语（已有原语管向上/垫高，唯独没有"往下"），于是 LLM 反复
 * 手写这样的循环：
 *
 *   while (currentY > 30) {
 *     const below = bot.blockAt(...脚下...);
 *     if (below && !['air','water','lava'].includes(below.name)) await bot.dig(below);
 *     currentY = Math.floor(bot.entity.position.y);   // ← bot 没掉下去，y 不变
 *   }
 *
 * 这里有**两个**坑，任何一个都会让这一步挂满 300s ReadTimeout（已两次 run 复现）：
 *
 *   (a) **挖了不掉**：`bot.dig` 挖掉脚下方块后，bot **不一定**自动掉进那个 1 格洞 —— 取决于
 *       它站在洞口的哪个边沿、有没有被邻格卡住。于是 `currentY` 永不变，循环里的 `if` 第二轮起
 *       就因为 `below` 已是 air 而跳过 `dig`，**整轮没有任何 await** → 纯同步 while 死循环 →
 *       锁死 Node 事件循环 → makeTickTimeoutPromise 的 setInterval 排不上 → tick 超时永不触发
 *       → /step 卡满 STEP_TIMEOUT=300s。纯同步死循环从 Node 内部无法打断（JS 单线程），只能靠
 *       Python 侧 HTTP 超时兜底，代价是 5 分钟空转 × 自愈重试次数。
 *   (b) **`bot.dig(air)` 不报错**：air 的 diggable=false 但 `dig()` 不检查它，会默默发 block_dig
 *       包、走 setTimeout(0) resolve —— 既不抛错也不前进，让 (a) 的空转更隐蔽。
 *
 * 本函数做的是**一格一格的循环**：挖脚下 → **等 bot 真的落进去**（位置 y 减小）→ 落不进去就主动
 * 走进洞口 → 重新定位再挖下一格。每一格都重新判断、重新等落稳，是 LLM 手写时漏掉的关键步骤。
 *
 * ── 与 digUpTo 的对称性 ──
 * digUpTo 挖头顶第 2 格 + 垫脚上升；本函数挖脚下方块 + 等下落。两者各自独立，不互相复用
 * （上下半身的物理不一样：向上要主动垫，向下靠重力 + 兜底走进去）。
 *
 * ── 只报事实 ──
 * 播报实际到了哪个 y、挖了几块、为什么停。**不判断**"你现在安全了"（那要看天光/怪物/岩浆，是它
 * 的活）。原语只报事实与准确意图，不写禁止性决定。
 */

//: 脚下遇到这些**先停下来问 Agent**，不自动挖开。
//  · lava/water —— 挖开会涌上来（岩浆掉血极快，是本域最快的死法之一，见 system.md §6）
//  · sand/gravel —— 受重力，挖开后上方的沙砾会连续塌落、把刚挖的竖井重新填满
//  · bedrock —— 挖不动（diggable=false），继续循环只是白烧时限
//  （与 digUpTo 的 _DIG_UP_HAZARD 同口径，只是方向反过来）
const _DIG_DOWN_HAZARD = new Set([
    "lava", "flowing_lava", "water", "flowing_water", "bubble_column",
    "sand", "red_sand", "gravel", "suspicious_sand", "suspicious_gravel",
    "powder_snow", "pointed_dripstone",
]);

//: 等 bot 落进刚挖的洞里最多多少 tick。正常 2-3 tick 重力就把它拉下去；等不到多半是它没站在洞口
//  正上方（边沿踩着），需要主动走进去而不是干等。
const _FALL_WAIT_TICKS = 8;

/**
 * @param {object} bot
 * @param {number} targetY 想挖到哪个 y 高度（绝对坐标，取整）
 * @param {string} [_blockName] 占位参数，与 digUpTo 签名对称；向下挖不垫脚，故不使用
 * @returns {number} 实际到达的 y
 */
async function digDownTo(bot, targetY, _blockName = null) {
    if (typeof targetY !== "number" || !Number.isFinite(targetY)) {
        // String() 而非 JSON.stringify()：理由同其他数值校验（NaN 会被序列化成 null，误导排查方向）
        throw new Error(
            `targetY for digDownTo must be a finite number (got ${String(targetY)})。` +
                `它是**绝对 y 坐标**（观察里「位置」那行的 y），不是"往下几格"。` +
                `NaN 多半是上一行算错了。`
        );
    }
    targetY = Math.floor(targetY);
    const startY = Math.floor(bot.entity.position.y);
    if (targetY >= startY) {
        bot.chat(
            `digDownTo: targetY=${targetY} 不低于你现在的 y=${startY}，什么都没做。` +
                `（本函数只向下；要上去请用底层放置/挖掘逻辑）`
        );
        return startY;
    }
    //: 单次上限，理由同 digUpTo 的 _MAX_CLIMB：一步别咬太大。地表约 y=64-80，从地表一口气挖到
    //  铁矿层(y=15)也在这个量级内。
    const _MAX_DESCEND = 96;
    if (startY - targetY > _MAX_DESCEND) {
        bot.chat(
            `digDownTo: 从 y=${startY} 到 ${targetY} 有 ${startY - targetY} 格，超过单次上限 ` +
                `${_MAX_DESCEND}，本次只挖到 y=${startY - _MAX_DESCEND}（可再调一次继续）。`
        );
        targetY = startY - _MAX_DESCEND;
    }

    let dug = 0;
    let stopReason = "";

    while (Math.floor(bot.entity.position.y) > targetY) {
        // 本步预算护栏（同类垂直移动原语）：宁可少挖几格并如实说，也不要整步超时白花
        // （超时那步的进度不进观察，等于白挖）。留 2s 余量给收尾播报 + observe 回传。
        if (
            typeof bot.openkageStepDeadlineMs === "number" &&
            Date.now() > bot.openkageStepDeadlineMs - 2000
        ) {
            stopReason = "本步时限快到了，提前收手（要一次挖到底请申请更长 timeout_ticks）";
            break;
        }

        const feet = bot.entity.position.floored();
        // 挖**正脚下方块**（feet-1）：挖开后 bot 应当掉进这个 1 格洞、y 减 1。
        const below = bot.blockAt(feet.offset(0, -1, 0));
        if (!below || below.name === "air" || below.name === "cave_air" || below.name === "void_air") {
            // 脚下已是空气：理论上重力早该把 bot 拉下去。若仍没掉，说明它被邻格卡住或站在洞口边沿。
            // 主动朝洞口挪一步触发下落，而不是空转（这正是手写 while 死循环的源头）。
            const descended = await _nudgeIntoGap(bot);
            if (!descended) {
                stopReason =
                    `脚下(y=${feet.y - 1})已是空气但 bot 没掉下去（可能被邻格卡住/站在洞口边沿）` +
                    `—— 我挪了一步仍没下去。换个位置或自己判断要不要继续`;
                break;
            }
            continue;
        }
        if (_DIG_DOWN_HAZARD.has(below.name)) {
            stopReason =
                `脚下方块是 ${below.name}（挖开有危险：岩浆/水会涌上来，沙砾会塌下来填满竖井）` +
                `—— 我没有挖它。换个位置横向移开几格再往下，或自己决定要不要处理它`;
            break;
        }
        if (below.diggable === false) {
            stopReason = `脚下方块是 ${below.name}，挖不动（bedrock 之类）—— 横向移开几格再往下`;
            break;
        }
        // 挑对工具再挖：bot.dig **不会**自动换工具（system.md §3 已写明），空手挖 stone 慢且会走进
        // equipForBlock 那条会让进程递归到 OOM 的路（见 mineBlock 顶部注释）。
        try {
            if (bot.tool && typeof bot.tool.equipForBlock === "function") {
                await bot.tool.equipForBlock(below, {});
            }
        } catch (_e) {
            // 换工具失败不致命（可能本来就裸手可采），继续尝试挖
        }
        const blkName = below.name;
        const yBefore = Math.floor(bot.entity.position.y);
        try {
            await bot.dig(below);
            dug++;
        } catch (err) {
            stopReason = `挖 ${blkName}(y=${below.position.y}) 失败：${(err && err.message) || err}`;
            break;
        }

        // ★ **核心正确性**：挖开后等 bot 真的掉进去（y 减小），而不是假设它会自动掉。
        //   这一等是手写 while 死循环里漏掉的步骤 —— 没有它，下一轮 blockAt 读到 air、跳过 dig、
        //   进入无 await 的纯同步空转、锁死事件循环。
        const descended = await _waitForDescent(bot, yBefore);
        if (!descended) {
            // 挖开了但没掉下去：bot 站在洞口边沿。主动朝脚下洞口挪一步触发下落。
            const nudged = await _nudgeIntoGap(bot);
            if (!nudged || Math.floor(bot.entity.position.y) >= yBefore) {
                stopReason =
                    `挖开了 ${blkName}(y=${below.position.y}) 但 bot 没掉下去（站在洞口边沿/被卡住）` +
                    `—— 我挪了一步仍没下去，停在这里免得空转。横向挪几格再挖，或自己判断`;
                break;
            }
        }
    }

    const endY = Math.floor(bot.entity.position.y);
    const parts = [
        `digDownTo：到达 y=${endY}（起点 ${startY}，目标 ${targetY}），挖掉 ${dug} 块`,
    ];
    if (endY > targetY && !stopReason) stopReason = "循环结束但未达目标";
    if (stopReason) parts.push(`提前停止：${stopReason}`);
    bot.chat(parts.join("；"));
    return endY;
}

/**
 * 等 bot 落进刚挖的洞：y 减小即算成功。
 * @param {object} bot
 * @param {number} yBefore 挖之前 floor 的 y
 * @returns {Promise<boolean>} 是否成功下落
 */
async function _waitForDescent(bot, yBefore) {
    for (let t = 0; t < _FALL_WAIT_TICKS; t++) {
        await bot.waitForTicks(1);
        if (Math.floor(bot.entity.position.y) < yBefore) return true;
    }
    return false;
}

/**
 * bot 脚下已是洞却没掉（站边沿/被卡）时，朝当前朝向走一步 + 松开，触发重力下落。
 * 不强求方向正确 —— 任何水平移动都可能让 bot 从洞口边沿滑进去。走完等几 tick 看 y 有没有减。
 * @returns {Promise<boolean>} 是否成功下落
 */
async function _nudgeIntoGap(bot) {
    const yBefore = Math.floor(bot.entity.position.y);
    try {
        bot.setControlState("forward", true);
        await bot.waitForTicks(3);
        bot.setControlState("forward", false);
    } catch (_e) {
        bot.setControlState("forward", false);
    }
    for (let t = 0; t < _FALL_WAIT_TICKS; t++) {
        await bot.waitForTicks(1);
        if (Math.floor(bot.entity.position.y) < yBefore) return true;
    }
    return false;
}
