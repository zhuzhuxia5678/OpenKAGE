// Explore downward for 1200 ticks: exploreUntil(bot, new Vec3(0, -1, 0), 1200);
// ★ maxTime 单位是**游戏刻**（与 timeout_ticks / waitUntil.maxTicks 同口径）：Agent 生活在 MC 时间里，
//   世界以 20 刻/秒流逝，探索时限也按刻给与计。按 game tick 计时（timeOfDay 增量）与倍速无关。
async function exploreUntil(
    bot,
    direction,
    maxTime = 1200,
    callback = () => {
        return false;
    }
) {
    if (typeof maxTime !== "number") {
        throw new Error("maxTime must be a number");
    }
    if (typeof callback !== "function") {
        throw new Error("callback must be a function");
    }
    const test = callback();
    if (test) {
        bot.chat("Explore success.");
        return Promise.resolve(test);
    }
    if (direction.x === 0 && direction.y === 0 && direction.z === 0) {
        throw new Error("direction cannot be 0, 0, 0");
    }
    if (
        !(
            (direction.x === 0 || direction.x === 1 || direction.x === -1) &&
            (direction.y === 0 || direction.y === 1 || direction.y === -1) &&
            (direction.z === 0 || direction.z === 1 || direction.z === -1)
        )
    ) {
        throw new Error(
            "direction must be a Vec3 only with value of -1, 0 or 1"
        );
    }
    maxTime = Math.min(maxTime, 24000);
    return new Promise((resolve, reject) => {
        const dx = direction.x;
        const dy = direction.y;
        const dz = direction.z;

        let explorationInterval;
        let budgetInterval;

        const cleanUp = () => {
            clearInterval(explorationInterval);
            clearInterval(budgetInterval);
            bot.pathfinder.setGoal(null);
            // 从中止登记表里摘掉自己（幂等，重复摘无副作用）
            if (bot.openkageAborts) bot.openkageAborts.delete(abortSelf);
        };

        // **把自己登记进中止表**：本函数持有一个每 2 秒重设 pathfinder goal 的 setInterval ——
        // /step 超时时外部只 setGoal(null) 一次是**没用的**，下一个 interval tick 又把 goal
        // 设回去，bot 会继续走远。故登记一个中止钩子，让 index.js 的 abortInFlightActions()
        // 能真正掐掉这个循环。
        const abortSelf = () => {
            cleanUp();
            resolve(null);  // 让 await 它的孤儿代码立刻继续/结束，而不是悬挂到 maxTime
        };
        if (bot.openkageAborts) bot.openkageAborts.add(abortSelf);

        const explore = () => {
            const x =
                bot.entity.position.x +
                Math.floor(Math.random() * 20 + 10) * dx;
            const y =
                bot.entity.position.y +
                Math.floor(Math.random() * 20 + 10) * dy;
            const z =
                bot.entity.position.z +
                Math.floor(Math.random() * 20 + 10) * dz;
            // ★ range 参数**必须传**：GoalNear/GoalNearXZ 拿 range 算 `rangeSq = range * range`，
            //   不传 → `rangeSq = NaN` → 判「到达」的 `dx*dx+dy*dy+dz*dz <= NaN` **恒为 false**，
            //   bot 站在目标格上也不承认到了。
            //
            //   后果不是"bot 不动"（A* 超时会返回 partial 路径，bot 照样朝那边走），而是
            //   **每次寻路都必然烧满 thinkTimeout**：无 range 时 A* 会一路跑到超时才放弃
            //   （访问节点数远超有 range 时的数量级），而本函数每 2 秒就 setGoal 一次，
            //   等于让 A* 长期满负荷跑。这与 mineBlock 里「同步 A* 阻塞事件循环 → 收不到
            //   keep_alive → 被踢下线」是同一类病根，只是这里是持续的钝性开销，不是一次性尖峰。
            //
            //   取 2：本函数的语义是「朝某方向漫游」，目标点是随机撒的（当前位置 +10~29 格），
            //   精确落点毫无意义；range 越大 A* 越省。但太大会让"到达"过早成立、每轮实际位移
            //   变小，2 是省开销与仍在推进之间的折中。
            const ARRIVE_R = 2;
            let goal = new GoalNear(x, y, z, ARRIVE_R);
            if (dy === 0) {
                goal = new GoalNearXZ(x, z, ARRIVE_R);
            }
            bot.pathfinder.setGoal(goal);

            try {
                const result = callback();
                if (result) {
                    cleanUp();
                    bot.chat("Explore success.");
                    resolve(result);
                }
            } catch (err) {
                cleanUp();
                reject(err);
            }
        };

        explorationInterval = setInterval(explore, 2000);

        // ★ 按游戏刻计预算：累加 bot.time.timeOfDay 增量（处理 24000 回绕），到 maxTime 刻即到顶。
        //   与倍速无关、不暴露墙钟给 Agent。轮询同 timeout_ticks 的 makeTickTimeoutPromise。
        let acc = 0;
        let prev =
            bot.time && typeof bot.time.timeOfDay === "number"
                ? bot.time.timeOfDay
                : null;
        budgetInterval = setInterval(() => {
            const t = (bot.time && bot.time.timeOfDay) || 0;
            if (typeof prev === "number") {
                let delta = (t - prev + 24000) % 24000;
                if (delta > 12000) delta = 0; // 回绕或 /time set 异常跳，不计入
                acc += delta;
            }
            prev = t;
            if (acc >= maxTime) {
                cleanUp();
                bot.chat("Max exploration time reached");
                resolve(null);
            }
        }, 300);
    });
}
