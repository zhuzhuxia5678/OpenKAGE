const Observation = require("./base.js").Observation;

// 把**技能调用的成败计数**收进观察——结构化，不走 console.log / onLog 字符串通道。
//
// 为什么需要：技能埋点原本是 `console.log('[skill] <slug> ok')`，再由 Python 的
// _parse_skill_call_events 用 startswith("[skill] ") + endswith(" ok") 反向解析回来。
// 这等于让两段代码（JS 输出格式 / Python 解析逻辑）靠一个字符串契约维持同步——任一端漂移
// 就静默丢数且永不报警（同款病：world_healthy 字段死一个月）。且 onLog 是给 LLM 看的文本
// 通道，单步 40 行配额，被并发日志挤掉/截断就丢，"没产出 [skill] 行"和"Agent 没调技能"
// 无法区分。改成走 /step 已有的机读事件通道（与 onSystem 结构化事件同机制），彻底脱钩。
//
// 数据流（见 plans/structured_skill_count_channel.md）：
//   _wrap_skill_for_injection 产出的技能代理调 bot.openkageSkill.push({slug,ok,err})
//   → 步末 flush() → bot.event("onSkill") → 进 cumulativeObs
//   → /step 响应里出现 ["onSkill",{events:[...]}]
//   → Python _collect_skill_events 读结构化数组 → merge 进 state["skill_calls"]
//   → night_cb 透传 discoveries → _apply_skill_usage_counts 按来源分流回填（不变）
//
// 与 onLog 的关键差异：缓冲的是**结构化对象**而非字符串，observe() 返回 {events,dropped}
// （与 onSystem.observe 同构）。与 onSystem 的关键差异：技能调用是**步内**事件（代理在
// evaluateCode 的 eval 作用域里跑），步首 reset() 清空（同 onLog），不像死亡/成就需跨步累积。
class onSkill extends Observation {
    constructor(bot) {
        super(bot);
        this.name = "onSkill";
        this.events = [];
        this.dropped = 0;
        bot.openkageSkill = this;  // 供技能代理直接 push（见 _wrap_skill_for_injection）
    }

    // 由技能代理逐次调用（成功 ok:1 / 失败 ok:0 + err）。**不**在这里 bot.event()：
    // 攒着步末 flush 一次（同 onLog），避免每次调用都发全量快照撑爆 cumulativeObs。
    push(rec) {
        if (this.events.length >= MAX_EVENTS) {
            this.dropped += 1;
            return;
        }
        // 防御：rec 形状异常不炸（代理内已有 try/catch，这里再兜一层）
        this.events.push({
            slug: String(rec && rec.slug),
            ok: rec && rec.ok ? 1 : 0,
            err: rec && rec.err ? String(rec.err) : null,
        });
    }

    // 步末调一次（index.js）：有内容才发 onSkill 帧。不显式发这一下，base.js 的
    // bot.event("observe") 会跳过名字以 "on" 开头的插件，onSkill 永远不会出现在 observe 结果里。
    flush() {
        if (!this.events.length) return;
        this.bot.event(this.name);
    }

    observe() {
        const result = { events: this.events, dropped: this.dropped };
        this.events = [];
        this.dropped = 0;
        return result;
    }

    // 步首清空（同 onLog.reset）：技能调用计数是按步累加的，上一步的已 flush 走，不跨步留存。
    reset() {
        this.events = [];
        this.dropped = 0;
    }
}

// 单步回传上限：技能调用低频，50 条足够；封顶只为防异常刷屏（如代理在循环里误触发）撑爆上下文。
const MAX_EVENTS = 50;

module.exports = onSkill;
