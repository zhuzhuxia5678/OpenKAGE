const Observation = require("./base.js").Observation;

// 把 MC 原生**记分板(Scoreboard)**的权威值收进观察，作 env 侧自统计指标的交叉校验旁证。
//
// 为什么需要：env.py 的 n_deaths(从 onSystem 播报数)/xp_total(从 experience.points 累加)/
// health/food(从 dawn_status 读)全是**自己统计或单点采样**，缺一个独立权威源做对照。记分板由
// MC 服务器侧维护，准则固定的几项(deathCount/totalKillCount/xp/health/food/armor)按游戏事件
// **自动**更新，是独立于 env 采样的第二数据源。env 自统计若与记分板系统性偏离即可定位统计 bug
// —— 同 env.py「物理不变式自检」思路，补上「外部权威值对照」这一环。
//
// 数据来源：mineflayer scoreboard 插件(4.8.1 默认加载)暴露 `bot.scoreboards`（所有 objective 的
// 字典）。**关键约束**：客户端默认只收到被 `setdisplay` 到某 slot 的 objective 的 score 包，故
// index.js 的 /start spawn 回调里每建一个记分项都紧跟 `/scoreboard objectives setdisplay <name> list`
// —— 不 setdisplay 的话 bot.scoreboards 里不会有该项的分数（建了 objective 但客户端收不到 score 更新）。
//
// ⚠️ 口径差（预期，非 bug）：记分板 `xp` 准则 = **当前持有**经验值(重生会被服务器压低)；
// env 的 xp_total = 跨命累计正增量(死亡不扣减)。两者只在「本命内」相等，死过必然分叉。这层差异
// 由 Python 侧注释说明，本采集器只如实上报记分板当前值，不做任何对齐。
//
// 只读、无事件监听：记分板是状态对象，每步取快照即可（与 status 同模式），score 更新包由
// mineflayer 插件异步写进 bot.scoreboards，observe 时直接读最新态。
class Scoreboard extends Observation {
    constructor(bot) {
        super(bot);
        this.name = "scoreboard";
    }

    observe() {
        // bot.scoreboards: { <objectiveName>: ScoreBoard { name, title, itemsMap } }
        // ScoreBoard.itemsMap: { <holderName>: { name, value, displayName(getter) } }
        // 取 value 而非整对象：displayName 是 getter(会 new ChatMessage)，序列化它既慢又可能抛。
        const sbs = this.bot.scoreboards;
        if (!sbs || typeof sbs !== "object") {
            return { objectives: {} };
        }
        const objectives = {};
        for (const name of Object.keys(sbs)) {
            const sb = sbs[name];
            if (!sb) continue;
            const scores = {};
            const itemsMap = sb.itemsMap || {};
            for (const holder of Object.keys(itemsMap)) {
                const item = itemsMap[holder];
                // value 是数值，读不到/非数记 null（不污染、不抛）
                if (item && typeof item.value === "number") {
                    scores[holder] = item.value;
                } else {
                    scores[holder] = null;
                }
            }
            objectives[name] = {
                name: sb.name || name,
                title: sb.title || null,
                scores: scores,
            };
        }
        return { objectives: objectives };
    }
}

module.exports = Scoreboard;
