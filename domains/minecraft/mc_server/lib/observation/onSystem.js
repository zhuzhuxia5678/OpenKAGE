const Observation = require("./base.js").Observation;

// 把**服务器权威的系统播报**（死亡 / 成就）收进观察。
//
// 为什么需要：服务器一直在把 `bot was slain by Zombie`、`bot has made the advancement [Stone Age]`
// 这类 systemChat 包推给 bot（mineflayer 在 plugins/chat.js 里 emit 成 messagestr，position="system"），
// 我们从来没监听 —— 于是这些「谁杀了我、我解锁了什么」的权威事实 100% 蒸发。
//
// 比现有埋点强在哪：
// ① **死亡不再漏计**。原先 n_deaths 靠 /step 执行期间轮询 health<=0（index.js evaluateCode），而
//    index.js 每步开头会 `bot.openkageDied = false`。bot 若在**两步之间**被打死（LLM 思考那几秒、
//    /tick freeze 反思期间），这次死亡就静默丢了。系统播报是服务器推的、跨步到达，本插件**不在步首
//    清零**（区别于 openkageLog.reset()），累积到被 observe 消费为止 → 漏不掉。
// ② **结构化，不用猜文案**。jsonMsg.translate 就是死因分类（death.attack.mob /
//    death.attack.explosion.player / death.fell.accident.generic…），可直接 group by 做「按死因聚合」；
//    with[0] 是主体、with[1] 是凶手/成就名。比正则匹配英文文案健壮得多。
// ③ **成就是权威的科技树里程碑**。比 env.py 那套 TECH_TIERS 从库存字符串模糊匹配（`"diamond" in name`）
//    准得多，可作对标 Voyager tech tree 的第二个独立指标。
//
// 过滤策略（position="system" 里混着三类，只有第一类该进上下文）：
//   ✅ 与 bot 相关的世界事件：death.* / chat.type.advancement.*
//   ❌ 我们自己发命令的回显：commands.damage.success、commands.kill.success.single、
//      commands.advancement.*、以及语法失败的 `Incorrect argument for command` +
//      `gamerule ...<--[HERE]`（这些**没有 translate 字段**，天然被白名单排除）
//   ❌ 别人的事件：同服其他真人玩家的死亡播报 —— 靠 with[0] === bot.username 滤掉
// 白名单按 translate 前缀来筛最干净：命令回显都是 commands.* 前缀，不会误收。
const DEATH_PREFIX = "death.";
const ADVANCEMENT_PREFIX = "chat.type.advancement.";

// 单步回传上限：死亡/成就都是低频事件，20 条足够；封顶只为防异常刷屏（如反复自杀）撑爆上下文。
const MAX_EVENTS = 20;

// `/kill @s` 的播报（death.attack.genericKill）。index.js 的 /start 硬重置里就有 `/kill @s`
// 清库存 —— 那不是「探索中被怪打死」，记成死亡会污染 n_deaths。故排除这一类。
// 真实致死（怪物/摔落/岩浆/爆炸）都有各自的 translate key，不会撞上它。
const IGNORED_DEATH_KEYS = new Set(["death.attack.genericKill", "death.attack.badRespawnPoint.link"]);

class onSystem extends Observation {
    constructor(bot) {
        super(bot);
        this.name = "onSystem";
        this.events = [];   // 累积的结构化事件，跨步存活直到被 flush/observe 消费
        this.dropped = 0;
        bot.openkageSystem = this;  // 供 index.js 在步末 flush（见 flush 注释）

        bot.on("messagestr", (message, position, jsonMsg) => {
            try {
                this._handle(message, position, jsonMsg);
            } catch (e) {
                // 解析失败绝不能影响 bot 主流程（messagestr 每条聊天都会来）
            }
        });
    }

    _handle(message, position, jsonMsg) {
        // position: "chat"=玩家聊天（onChat 管）、"system"=系统播报、"game_info"=action bar
        if (position !== "system") return;
        const key = jsonMsg && jsonMsg.translate;
        if (!key) return;  // 无 translate 的都是命令回显/报错，丢

        const isDeath = key.startsWith(DEATH_PREFIX);
        const isAdvancement = key.startsWith(ADVANCEMENT_PREFIX);
        // ★ 成就侧恢复采集（2026-09-14）：2026-09-07 起成就记账全交 packet 协议包路、onSystem
        //   成就播报即弃；但 packet 路在 run 中途会状态失效（completedKeys 恒空、storyTreeSeen 恒
        //   False，run0912 alice day10 后达成 fishy_business/plant_seed 漏报）。真跑 grant 测试证明
        //   announce=true 的子进度（fishy/plant_seed/kill_a_mob 等）grant 后服务端**确实发**
        //   chat.type.advancement.* 聊天播报——onSystem 能采到它们，作 packet 失效期的独立兜底。
        //   ★ 三个 root（story/adventure/husbandry 的 root）announce_to_chat=false 不发播报，onSystem
        //   仍采不到（只能靠 packet/ground-truth）。故 onSystem 不是 packet 的替代，是**子进度的
        //   第二条采集路**。
        //   ★ 落盘口径不变：onSystem 成就**不进** advancements 记账列表（_record_system 仍只收
        //   death），仅经 _collect_system_events 抽出后，在 _system_feedback 调用点与 packet diff 做
        //   **差集**——packet 没采到的子进度进步反馈（让 agent 当场知道达成成就），packet 正常时
        //   差集为空不双计。落盘准确性靠 ground-truth 回补脚本兜底，不靠 onSystem。
        if (isAdvancement) {
            return this._handleAdvancement(message, key, jsonMsg);
        }
        if (!isDeath) return;
        if (IGNORED_DEATH_KEYS.has(key)) return;

        // 事件主体必须是自己：同服可能有真人玩家，其死亡播报不该算进本 bot 的 n_deaths
        const parts = (jsonMsg && jsonMsg.with) || [];
        const subject = this._text(parts[0]);
        if (subject && this.bot.username && subject !== this.bot.username) return;

        if (this.events.length >= MAX_EVENTS) {
            this.dropped += 1;
            return;
        }

        // with[1]：死亡时是凶手、成就时是成就名。mob 击杀时 with[1].text 可能是 **null**
        // （凶手名藏在 with[1].translate="entity.minecraft.zombie"），而具名实体/玩家才有 .text
        // —— 所以统一用 ChatMessage.toString() 取，两种都能拿到人类可读名（"Zombie" / "TestZed"）。
        const detail = this._text(parts[1]);

        // 到这里 key 必是 death.*（成就已在上方 return，非 death/advancement 也已 return）。
        // 死亡额外快照「在哪死的、当时旁边有什么」：播报本身不含这些，但正是它们才能
        // 沉淀成有用的 pitfall（「在 y=12 挖矿被苦力怕炸死」远比「死了一次」可教）。
        this.events.push({
            kind: "death",
            cause: key,                       // 死因分类，供按死因聚合
            killer: detail || "unknown",      // /damage 不带 by <entity> 时凶手为空，容忍缺项
            text: String(message),
            position: this._position(),
            timeOfDay: this._timeOfDay(),
            nearby: this._nearby(),
        });
        // **不在这里 bot.event()**（与 onLog 同理，但原因更关键）：播报随时到达，包括**两步之间**
        // （LLM 思考、/tick freeze 反思期间）。而 index.js 每步开头会 `bot.cumulativeObs = []`
        // —— 此刻 push 的帧会在下一步开始时被整个丢掉，正是本插件要修的「跨步死亡漏计」。
        // 故只攒进 this.events（跨步存活），由 index.js 在**步末** flush 一次，保证被回传。
    }

    // 成就播报（chat.type.advancement.task/goal/challenge）→ kind:"advancement" 事件。
    // 复用 _handle 的死亡采集机制（主体校验 + events 攒缓冲 + 步末 flush），仅事件结构不同。
    // ★ 成就名归一化为带方括号显示名（如 "[Fishy Business]"）：与 advancementPacket.js 的
    //   completed 集合（"[" + title + "]"）、env.py _ADV_INFO 的 key 同源同形，保证 env 侧
    //   packet diff 与 onSystem 成就能做差集去重。with[1] 经 ChatMessage.toString() 取，MC 进度
    //   显示名标准格式本就带方括号，但 toString 行为依实现版本，故取后统一：去首尾空白，无方括号
    //   则补上（兜底防格式漂移致差集失效）。
    _handleAdvancement(message, key, jsonMsg) {
        const parts = (jsonMsg && jsonMsg.with) || [];
        // 主体必须是自己（同服真人玩家达成成就不该算进本 bot）
        const subject = this._text(parts[0]);
        if (subject && this.bot.username && subject !== this.bot.username) return;

        if (this.events.length >= MAX_EVENTS) {
            this.dropped += 1;
            return;
        }

        let name = this._text(parts[1]);
        if (!name) return;  // 无成就名（畸形包）丢弃，不污染统计
        name = name.trim();
        if (!(name.startsWith("[") && name.endsWith("]"))) {
            name = "[" + name + "]";
        }

        this.events.push({
            kind: "advancement",
            advancement: name,              // 带方括号显示名，与 packet completed 同形
            cause: key,                     // chat.type.advancement.task/goal/challenge 难度层级
            text: String(message),
        });
        // 同 death：不在此 bot.event()，攒进 events 由步末 flush 回传（跨步存活）。
    }

    // 步末调一次（index.js）：有累积事件才发 onSystem 帧。不显式发这一下，base.js 的
    // bot.event("observe") 会跳过名字以 "on" 开头的插件，onSystem 就永远不出现在 observe 结果里。
    flush() {
        if (!this.events.length) return;
        this.bot.event(this.name);
    }

    // ChatMessage 组件 → 人类可读文本。mineflayer 的 ChatMessage 有 toString()，
    // 能把嵌套的 translate（entity.minecraft.zombie）解析成 "Zombie"。
    _text(component) {
        if (!component) return null;
        try {
            const s = component.toString();
            return s ? s.trim() : null;
        } catch (e) {
            return null;
        }
    }

    _position() {
        try {
            const p = this.bot.entity && this.bot.entity.position;
            if (!p) return null;
            return { x: Math.round(p.x), y: Math.round(p.y), z: Math.round(p.z) };
        } catch (e) {
            return null;
        }
    }

    // 复用 status 插件的时段判定语义，但这里只需粗粒度（是否夜晚由 Python 侧判）
    _timeOfDay() {
        try {
            return this.bot.time ? this.bot.time.timeOfDay : null;
        } catch (e) {
            return null;
        }
    }

    // 死时附近 16 格内的生物名（去重）：比 status 的 32 格更近，聚焦「真正威胁到我的东西」
    _nearby() {
        try {
            const entities = this.bot.entities;
            if (!entities || !this.bot.entity) return [];
            const names = new Set();
            for (const id in entities) {
                const e = entities[id];
                if (!e || !e.displayName || !e.position) continue;
                if (e.name === "player" || e.name === "item") continue;
                if (e.position.distanceTo(this.bot.entity.position) < 16) {
                    names.add(e.name);
                }
            }
            return Array.from(names);
        } catch (e) {
            return [];
        }
    }

    observe() {
        const result = { events: this.events, dropped: this.dropped };
        this.events = [];
        this.dropped = 0;
        return result;
    }

    // **故意不清空 events**：reset() 由 base.js 的框架语义在重置时调用，但死亡/成就必须
    // 跨步累积（这正是它比 health 轮询可靠的原因）。唯一的消费点是 observe()。
    reset() {}
}

module.exports = onSystem;
