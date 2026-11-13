/**
 * waitUntil —— 「什么都不做，等某件事发生」。
 *
 * ── 为什么需要这个原语 ──
 * 没有这个原语时，Agent 夜里**被迫用 write_code 表达"我不想做动作"**，写一堆空转代码硬撑过
 * 等待。更糟的是当它宁愿宣布结束也不愿再写假动作时，ReAct 会判定 STOP 提前退出，外层把那当成
 * "天亮了"、清空计划开了"第 2 天"，而游戏里还是同一个夜晚。根因就是**缺少一个正当的
 * "等待"表达**。
 *
 * ── 设计：Agent 自己声明唤醒条件，系统不兜底 ──
 * 由 Agent 传 `conditions`（想等什么），而不是由代码写死一套策略 —— 它比我们更清楚此刻在等什么
 * （等天亮 / 等血回满 / 等怪走开 / 就是单纯歇一会）。
 * **系统不追加任何兜底事件**：原语只报事实、不替 Agent 决定监听什么。要防"夜里等到轨迹结束"就显式传 `dawn`；
 * 要防"等待中被怪打死不醒"就显式传 `hurt`。Agent 漏传 = 它的真实意图（也许它就想纯定时），
 * 原语不该替它补。
 * 纯计数器模式（不传 conditions）同理：铁定等够 ticks 刻，连 hurt 也不兜底。
 *
 * ── 只报事实，不给建议 ──
 * 醒来后如实说明**为什么醒的** + 新观察，不替 Agent 判断该怎么办
 * （与 env.py render_nearby_grid 同一原则：那里不给 feet 层标「可通行」，因为替模型下了
 *   它自己该下的结论）。
 *
 * ── 实现约束 ──
 * 所有条件都必须是**每 tick 可廉价求值**的：读 bot.health / bot.time.timeOfDay /
 * bot.entity.position / bot.inventory.items() / bot.nearestEntity 都是 O(1) 或轻遍历。
 * ⚠️ **绝不能**在这里用 bot.findBlock/findBlocks —— 那是同步扫描，每 tick 跑会把事件循环占死
 * → 服务器 30s 收不到 keep_alive 判定掉线（这个坑在 mineBlock 的注释里记着）。
 */

// 时刻阶段边界，**必须与 lib/observation/status.js 的 getTime 逐个数字对齐**（官方四阶段 time.md）：
//   day[0,12000) sunset[12000,13800) night[13800,22200) sunrise[22200,24000)。
//   24000=0 回到 day。dawn 唤醒阈值（_DAWN_TICK）只要比 env 认定天亮晚，Agent 就会在 env 已收尾后还傻等；
//   安全性不该由 dawn 阈值兜——那是 hurt/no_mobs_nearby 的职责。
const _DAY_END = 12000;     // 白天结束（进 sunset）
const _SUNSET_END = 13800;  // 日落段结束（进 night）
const _NIGHT_END = 22200;   // 夜晚段结束（进 sunrise）= dawn 唤醒点
const _TICKS_PER_DAY = 24000;

/**
 * 「天亮了」判据（dawn 条件用）。进日出段(22200)即算天亮——天将亮、黎明已至。
 * 注意这**不等于「白天」**：sunrise 段(22200-24000)天将亮但怪物 23460 才开始燃烧，
 * 仍是过渡期；严格白天是 [0,12000)。dawn 唤醒后 Agent 需自行判断是否安全出洞。
 */
function _isDawn(t) {
    return t < _DAY_END || t >= _NIGHT_END;  // 白天 或 日出段
}

/** 当前时刻 + 距下一个阶段的刻数，按官方四阶段播报（当前段→下一段起点）。
 *
 * ★ 单位是**游戏刻**（与 timeout_ticks / maxTicks 同口径）：Agent 生活在 MC 时间里，对它而言世界以
 *   20 刻/秒流逝、一天 24000 刻，原语回报的时间也只给刻、不给「真实秒」。距下一阶段的刻数与倍速 N
 *   无关 —— 1x 和 10x 下「距夜晚 1000 刻」是同一件事。
 *
 * ★ 各段播报的下一段：day/noon→距日落(12000)、sunset→距夜晚(13800)、night→距日出(22200)、
 *   sunrise→距白天(24000/0)。「距天亮」旧叫法已废——用距日出/距白天更准（天光含月光，夜晚未必全黑）。
 */
function _timeLine(t, bot) {
    let remain, what;
    if (t < _DAY_END) {
        remain = _DAY_END - t;
        what = "距日落";
    } else if (t < _SUNSET_END) {
        remain = _SUNSET_END - t;
        what = "距夜晚";
    } else if (t < _NIGHT_END) {
        remain = _NIGHT_END - t;
        what = "距日出";
    } else {
        remain = _TICKS_PER_DAY - t;
        what = "距白天";
    }
    return (
        `timeOfDay=${t}（${_isDawn(t) ? "天亮" : "夜间"}，` +
        `${what}约 ${remain} 刻）`
    );
}

/**
 * @param {object} bot
 * @param {number} ticks       等多少游戏刻。纯计数器模式（不传 conditions）：等够 ticks 刻醒，
 *                             reason=condition_met。事件模式（传 conditions）：ticks 是兜底上限，
 *                             事件命中 reason=condition_met、超时 reason=timeout。clamp 到 [1, 3000]，
 *                             并与本步 timeout_ticks 剩余取小。
 * @param {string} reason      为什么要等（一句话，进日志与轨迹留痕，供夜反思读），可选。
 * @param {string[]} conditions 想等的条件（见 _CONDITION_DOC），可选。**系统不兜底任何事件**——
 *                             传什么就只监听什么（防睡死要显式传 ['hurt']、等天亮要显式传 ['dawn']，
 *                             原语不替 Agent 决定监听什么）。不传是纯计数器，铁定等够 ticks 刻。
 */
async function waitUntil(bot, ticks = 1200, reason = "", conditions = []) {
    const startHealth = typeof bot.health === "number" ? bot.health : 20;
    const startFood = typeof bot.food === "number" ? bot.food : 20;
    const startPos = bot.entity && bot.entity.position;
    const rate = Math.max(1, Number(bot && bot.openkageTickRate) || 1);

    // 归一化 Agent 传来的 conditions。
    // 系统不兜底任何事件：原语只报事实、不替 Agent 决定
    //   监听什么）：传什么就只监听什么。要防睡死该由 Agent 显式传 ['hurt']、等天亮显式传 ['dawn']。
    //   纯计数器模式（conditions 为空/非数组/全无效）= 铁定等够 ticks 刻、雷打不动。
    //   Agent 漏传 = 它的真实意图（也许就想纯定时），原语不替它补。
    const asked = (Array.isArray(conditions) ? conditions : [conditions])
        .filter((c) => typeof c === "string" && c.trim())
        .map((c) => c.trim().toLowerCase());
    const isTimerMode = asked.length === 0;  // 纯计数器：没传任何有效条件
    // ★ active 是 asked 的**拷贝**而非引用：下方 preSatisfied 探测会原地 active.length=0/push
    //   复用 evaluate()，若 active===asked 会把 asked 也清空 → askedAllMet 判据失效（asked 变空）。
    const active = [...asked];
    // ★ **进门就成立的条件要摘掉**：白天调 waitUntil(['dawn']) 时它一开始就为真
    //   → 0 刻返回，等于这次等待白调。若同时传了 ['night']（想等入夜）却被 ['dawn'] 抢先命中，
    //   就会出现「等待结束：天亮了（等了 0 刻）」这类误导播报。
    //   语义上也说得通：唤醒条件描述的是**状态变化**，进门已满足的条件不构成"等待的理由"。
    //   全部条件都已满足时不摘（否则没有出口），退化为纯计时等待。
    const preSatisfied = new Set();

    // 3000 = index.js 的 STEP_CODE_TIMEOUT_MAX_TICKS（write_code 步上限）：waitUntil 在步内执行，
    // ticks 不该超过步上限——超过也会被步 timeout_ticks 截断。与步上限对齐，clamp 到 [1, 3000]。
    // ⚠️ 等天亮（sunset12000→dawn22200 最多10200刻）是跨步行为（续跑轮多次 waitUntil），不是单次。
    const _MAX_WAIT_TICKS = 3000;
    const askedTicks = Number(ticks) || 1200;
    let budgetTicks = Math.max(1, Math.min(askedTicks, _MAX_WAIT_TICKS));
    // 与本步 deadline 取小：原语不该把整步预算烧光（同 mineBlock 走近那处的教训）。
    // openkageStepDeadlineMs 是墙钟（原语内部沿用 Date.now），换算回刻：`剩余墙钟秒 / (20*rate)`。
    if (typeof bot.openkageStepDeadlineMs === "number") {
        const stepRemainTicks = Math.max(
            0,
            ((bot.openkageStepDeadlineMs - 1500 - Date.now()) / 1000) * (20 * rate)
        );
        if (stepRemainTicks < budgetTicks) {
            budgetTicks = stepRemainTicks;
        }
    }
    // ★ **实际能等多久必须如实告知**：ticks 会被两个天花板悄悄削掉
    //   —— 本条的 `_MAX_WAIT_TICKS` 硬顶，以及**本步 timeout_ticks**（更常见）。若播报只说
    //   「到时（条件未满足）」，Agent 会把原因猜到 ticks 上、反复去加它 —— 但那个旋钮
    //   早被 min(…, _MAX_WAIT_TICKS) 封顶，全无效果，而真正该动的 timeout_ticks 顶死在上限时
    //   却没人告诉它。（数值别写死在注释里：它跟着 index.js 的步上限走。）
    const truncated =
        askedTicks - budgetTicks > 20 // 20 刻容差：别为舍入误差报警
            ? { askedTicks, grantedTicks: budgetTicks, byStep: budgetTicks < Math.min(askedTicks, _MAX_WAIT_TICKS) }
            : null;

    // 条件可带参数，写成 `名字:数值`（如 `no_mobs_nearby:20`、`hurt:3`）。这两个小工具负责拆参数：
    // 一个取名字（用于 switch 匹配），一个取数值（取不到就用缺省）。
    // 注意：「等够 N 刻」由主参数 ticks 承担，conditions 只放事件，不放 ticks:N（会落 default
    //   被当未知条件忽略）。
    const nameOf = (c) => c.split(":")[0];
    /** 取条件的数值参数。max 因条件而异（距离封 64 格、掉血封 20 点），取不到就用 dflt。 */
    const argOf = (c, dflt, max) => {
        const v = Number(c.split(":")[1]);
        return Number.isFinite(v) && v > 0 ? Math.min(v, max) : dflt;
    };

    // waitedTicks：累计已等游戏刻（timeOfDay 增量）。提前声明在 evaluate 之前，便于将来有条件
    // 需要读它（preSatisfied 探测阶段会调 evaluate，若 waitedTicks 还在 TDZ 会抛 ReferenceError）。
    let waitedTicks = 0;

    /** 求值所有条件，返回第一个满足的 {name, detail}；都不满足返回 null。 */
    function evaluate() {
        const t = (bot.time && bot.time.timeOfDay) || 0;
        const hp = typeof bot.health === "number" ? bot.health : 20;
        const food = typeof bot.food === "number" ? bot.food : 20;
        for (const c of active) {
            switch (nameOf(c)) {
                case "dawn":
                    if (_isDawn(t)) return { name: "dawn", detail: `天亮了（timeOfDay=${t}）` };
                    break;
                case "hurt": {
                    // 掉多少才叫醒也由 Agent 定（`hurt:3` = 掉 3 点以上才醒）：躲在掩体里被
                    // 蜘蛛隔墙擦一下掉 1 点，未必值得中断等待；而开阔地掉 3 点就该反应了。
                    // 缺省 0.5（任何掉血都醒）—— 这是**兜底**条件，宁可灵敏。
                    const tol = argOf(c, 0.5, 20);
                    if (startHealth - hp >= tol)
                        return {
                            name: "hurt",
                            detail: `掉血 ${(startHealth - hp).toFixed(1)} 点：生命 ${startHealth} → ${hp}`,
                        };
                    break;
                }
                case "night":
                    if (!_isDawn(t)) return { name: "night", detail: `入夜（timeOfDay=${t}）` };
                    break;
                case "healed":
                case "health_full":
                    if (hp >= 20) return { name: "healed", detail: `生命已回满（${hp}/20）` };
                    break;
                case "fed":
                case "food_full":
                    if (food >= 20) return { name: "fed", detail: `饱食度已满（${food}/20）` };
                    break;
                case "no_mobs_nearby": {
                    // ★ 距离由 **Agent 自己给**（`no_mobs_nearby:20`），不写死一个数：
                    //   多远算"安全"取决于它此刻的处境 —— 在密闭掩体里 5 格就够，在开阔地被
                    //   skeleton 射箭时 20 格也不安全，而它比代码更清楚自己在哪、怕什么。
                    //   缺省 16 只是没给时的兜底。
                    // ⚠️ 这是**球半径**：distanceTo 是三维欧氏距离，头顶 12 格的怪也算在内。
                    //   垂直方向常隔着天花板，所以同一个数字在"地面开阔"与"地下洞穴"里的
                    //   含义并不一样。
                    const r = argOf(c, 16, 64);
                    const hostile = bot.nearestEntity(
                        (e) =>
                            e.type === "hostile" &&
                            e.position.distanceTo(bot.entity.position) < r
                    );
                    if (!hostile)
                        return {
                            name: "no_mobs_nearby",
                            detail: `${r} 格（球半径）内没有敌对生物了`,
                        };
                    break;
                }
                // 「等够 N 刻」由主参数 ticks 承担（新签名 waitUntil(bot, ticks, ...)），
                // conditions 只放事件（dawn/night/hurt/healed/fed/no_mobs_nearby）。
                // ticks 既是纯计数器模式的达成目标，也是事件模式的兜底上限。
                default:
                    // 未知条件：不静默忽略（那会让 Agent 以为在等某件事，其实没人在看）。
                    // 记下来，醒来时一并告知。
                    if (!evaluate._unknown) evaluate._unknown = new Set();
                    evaluate._unknown.add(c);
                    break;
            }
        }
        return null;
    }

    // 逐个条件单独求值一次，把「进门已成立」的挑出来（见上面 preSatisfied 的说明）。
    // 做法：临时把 active 换成单条来复用 evaluate()，避免另写一份判定逻辑（两份必然漂移）。
    {
        const all = [...active];
        for (const c of all) {
            active.length = 0;
            active.push(c);
            if (evaluate()) preSatisfied.add(c);
        }
        active.length = 0;
        active.push(...all.filter((c) => !preSatisfied.has(c)));
        evaluate._unknown = undefined;  // 上面的探测会误记 unknown，清掉重新收集

        // ★ **Agent 要等的条件全都已经成立 → 立刻返回，不等**。
        //   典型是「白天却 wait dawn」「夜里却 wait night」「满血却 wait healed」：说明它判断错了现状。
        //   这时正确反应是**马上告诉它现状**，让它重新判断 —— 而不是替它找个别的出口继续等
        //   （若改成"白天补 night、夜里补 dawn"，那是替 Agent 决定"那你就等到入夜吧"，
        //    既拖时间又掩盖了它算错这件事）。也不该退化成纯计时干等满 maxTicks。
        //   判据是「Agent 显式给的**全部**条件都进门成立」：传 ['dawn','night']（等任一昼夜切换）
        //   时只有一个成立、不算全成立，会正常等下一次切换；只有传单一已成立条件（或多个全成立）
        //   才触发。
        const askedAllMet =
            asked.length > 0 && asked.every((c) => preSatisfied.has(c));
        if (askedAllMet) {
            const t = (bot.time && bot.time.timeOfDay) || 0;
            // ★ 这条播报要**足够重**：它是全函数唯一用来「纠正 Agent 时刻误判」的分支。
            //   仅报一个裸 timeOfDay 是不够的：Agent 不是没看见播报，而是①播报没说
            //   "你要等的事此刻已成立、所以一刻没等"，②更没说**游戏时间没有推进**（这才是
            //   容易误判的那一点：调过 waitUntil 就以为时间流过去了）。
            //   同 truncated / unknown 两处的教训：Agent 会拿播报去证实自己调用前就写好的乐观
            //   断言，播报不明说"没发生"，它就当成"发生了"。
            bot.chat(
                `无需等待：你要等的条件（${asked.join(", ")}）**在调用时就已经成立**，` +
                    `因此一刻没等，当前 ${_timeLine(t, bot)}，请按当前时刻重新判断现在该做什么`
            );
            return {
                waited: 0,
                reason: "already_satisfied",
                woke: null,
                timeOfDay: t,
                isDay: _isDawn(t),
                preSatisfied: [...preSatisfied],
            };
        }
    }

    let hit = null;
    let died = false;
    const onDeath = () => {
        died = true;
    };
    bot.once("death", onDeath);
    // ★ 按游戏刻计时（与 timeout_ticks 同口径）：累加 bot.time.timeOfDay 增量、处理午夜回绕，
    //   到 budgetTicks 即到时。与倍速无关 —— Agent 体验到的「世界时间」就是 game tick 本身。
    //   waitedTicks 已在 evaluate 之前声明（见上方，为 preSatisfied 探测阶段可读）。
    let prevT =
        bot.time && typeof bot.time.timeOfDay === "number"
            ? bot.time.timeOfDay
            : null;
    try {
        // 轮询而不是纯事件驱动：条件里有「时刻」这类没有对应事件的量，统一按 tick 求值最简单。
        // 每 5 tick 评一次 —— 足够灵敏，又不至于每 tick 都跑一遍全部条件。
        while (waitedTicks < budgetTicks) {
            if (died) break;
            hit = evaluate();
            if (hit) break;
            await bot.waitForTicks(5);
            const t = (bot.time && bot.time.timeOfDay) || 0;
            if (typeof prevT === "number") {
                let delta = (t - prevT + _TICKS_PER_DAY) % _TICKS_PER_DAY;
                if (delta > 12000) delta = 0; // 回绕或异常大跳（/time set），不计入
                waitedTicks += delta;
            }
            prevT = t;
        }
    } finally {
        bot.removeListener("death", onDeath);
    }

    const waited = Math.round(waitedTicks);
    const hp = typeof bot.health === "number" ? bot.health : 20;
    const moved =
        startPos && bot.entity && bot.entity.position
            ? bot.entity.position.distanceTo(startPos)
            : 0;

    const parts = [];
    if (died) {
        // 死亡是强信号，首句即足；不带「监听条件/生命位移」后缀——死亡时这些无决策价值。
        parts.push(`等待中**死亡**（等了 ${waited} 刻）`);
    } else if (hit) {
        // 事件命中：detail 已自带具体信息（如「天亮了(timeOfDay=X)」「掉血N点:生命20→17」），
        // hurt 命中 detail 已含血量变化，不再追加「生命位移」避免重复。
        parts.push(`等到了 ${hit.detail}（等了 ${waited} 刻）`);
    } else if (isTimerMode) {
        // 纯计数器模式：等够 ticks 刻是正常达成（reason=condition_met），不是超时。
        if (truncated && truncated.byStep) {
            // 被步 timeout_ticks 削短了（没等够申请的 ticks）—— 首句直接说没等够，
            // 不说「等够了」（否则 Agent 以为真等够了，与实际语义矛盾）。
            parts.push(
                `⚠️ 没等够：申请 ${truncated.askedTicks} 刻，本步 timeout_ticks 只够 ` +
                `${Math.round(truncated.grantedTicks)} 刻 —— 要等更久请在 timeout_ticks 申请更长时限`
            );
        } else {
            parts.push(`等够了 ${waited} 刻（定时器到时）`);
        }
    } else {
        parts.push(`等待到时：等了 ${waited} 刻，条件未满足`);
        // 到时而条件没满足时，说清「是谁截断的、下次该动哪个旋钮」——
        // 不这么说 Agent 只能猜，而它猜错过（见上面 truncated 的注释）。
        if (truncated) {
            parts.push(
                truncated.byStep
                    ? `⚠️ 没等够：申请 ${truncated.askedTicks} 刻，本步 timeout_ticks 只够 ` +
                      `${Math.round(truncated.grantedTicks)} 刻 —— 要等更久请在 timeout_ticks 申请更长时限`
                    : `⚠️ 已等满单次上限 ${_MAX_WAIT_TICKS} 刻（你申请 ${truncated.askedTicks} 刻）`
            );
        }
        // 还差多久到天亮：等 dawn 却到时了，这是它下一步唯一需要的数（刻，与倍速无关）
        if (active.some((c) => nameOf(c) === "dawn")) {
            const t = (bot.time && bot.time.timeOfDay) || 0;
            const togo = t < _NIGHT_END ? _NIGHT_END - t : 0;
            if (togo > 0) {
                parts.push(`距天亮还有约 ${togo} 刻`);
            }
        }
    }
    // 「监听的条件」只在事件模式报：纯定时器（被截断/死亡）报「无」无决策价值——
    // 截断首句已说清问题、死亡首句已足。事件模式才需要 Agent 确认在等什么。
    if (!isTimerMode) {
        parts.push(`监听的条件：${active.join(", ")}`);
        // 摘掉的条件要说出来，否则 Agent 以为在等它、实际没人看（同"参数被静默忽略"那类坑）
        if (preSatisfied.size) {
            parts.push(
                `（调用时已成立、故未作为唤醒条件：${[...preSatisfied].join(", ")}）`
            );
        }
    }
    if (evaluate._unknown && evaluate._unknown.size) {
        parts.push(
            `⚠️ 无法识别的条件被忽略：${[...evaluate._unknown].join(", ")}` +
                `（可用：dawn / night / hurt / healed / fed / no_mobs_nearby）`
        );
    }
    // 生命/位移只在**有变化**时报：血量变了 或 位移>0.5格。纯计时无变化时不报（无决策价值）。
    // hurt 命中 detail 已含血量，但这里仍报位移（hurt 等待期间可能也移动了）。
    const hpChanged = Math.abs(hp - startHealth) >= 0.5;
    if (hpChanged || moved > 0.5) {
        parts.push(`生命 ${startHealth} → ${hp}，位移 ${moved.toFixed(1)} 格`);
    }

    // ★ **纯定时器正常达成全静默**（2026-09-10 短定时器静默；2026-09-16 放宽为不限刻数）：
    //   纯计数器模式只要**正常达成**（没死、没事件命中、没被步 timeout_ticks 截断）就**不打 chat 播报**，
    //   不管等了多少刻。动机：Agent 把 waitUntil 当 sleep 塞进手写循环（典型 `while(...掉落)
    //   await waitUntil(bot,2,'fall')` 等重力下落、`waitUntil(bot,40,...)` 等动画、
    //   `waitUntil(bot,200,...)` 等过夜），一步循环几十上百次，每次都 bot.chat 一条几乎雷同的
    //   「等够了 N 刻；监听的条件：无；生命 20→20，位移 0.0 格」—— 2026-09-10 alice step724
    //   实测单步刷出 501 条，把真正有价值的 onLog（下挖结果）淹掉。
    //   纯定时器正常达成时，整条播报对 Agent 决策都无价值：「等够了 N 刻」是它自己申请的 N、
    //   「监听的条件：无」是它自己没传条件、「生命位移」纯计时等待通常无变化。Agent 当 sleep 用时
    //   根本不读返回值。故静默，返回值照给（result.waited 仍是信号，Agent 若接返回值不丢信息）。
    //   **不静默**的情况都要保留播报，因为它们承载 Agent 下一步该用的信息：
    //     · died —— 等待中死亡，关键事件
    //     · hit —— 事件命中（等到了 dawn/hurt/…），「等到了」是有意义信号（事件模式永远不静默）
    //     · truncated.byStep —— 被步 timeout_ticks 截断（没等够），Agent 该去调 timeout_ticks
    //   事件模式（传了 conditions）永远不静默 —— 那不是「纯计时」。
    const shortTimerSilent =
        isTimerMode && !died && !hit && !(truncated && truncated.byStep);
    if (shortTimerSilent) {
        // 不 bot.chat：短定时器等完无异常、无事件命中、未被步时限截断 —— 没有需要 Agent 响应的信号。
        // 信息仍在返回值里（waited/reason/timeOfDay/health），只是不进 onChat 反馈流。
    } else {
        bot.chat(parts.join("；"));
    }

    // ★ **必须有返回值**：若所有出口都是裸 `return`，Agent 写的
    //   `const result = await waitUntil(...)` 会永远拿到 undefined，从而拿不到任何可分支的
    //   信号，只能靠**调用前就写死的乐观文案**下结论。
    //   这与同目录约定也不一致（不同原语返回各自的结构化结果）。
    //   返对象而非布尔：`reason` 让 Agent 能区分"等到了 / 到时了 / 被打断 / 压根没等"这四种，
    //   它们的正确后续动作完全不同（布尔会把后三种混成一个 false）。
    const t = (bot.time && bot.time.timeOfDay) || 0;
    return {
        waited: Number(waited),
        reason: died ? "died" : hit ? "condition_met" : (isTimerMode ? "condition_met" : "timeout"),
        woke: hit ? hit.name : (isTimerMode && !died ? "ticks" : null),
        timeOfDay: t,
        isDay: _isDawn(t),
        health: hp,
        preSatisfied: [...preSatisfied],
    };
}
