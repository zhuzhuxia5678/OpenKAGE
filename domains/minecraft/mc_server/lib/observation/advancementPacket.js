const Observation = require("./base.js").Observation;

// 把 **packet_advancements 协议包**维护的「已完成进度」状态收进观察，作 onSystem 被动播报的
// 独立佐证支路（2026-09-07）。
//
// 为什么需要：onSystem 靠监听 `chat.type.advancement.*` 聊天播报统计进度里程碑，但 Minecraft
// 原版里部分进度的 advancement 定义 `announce_to_chat=false`（最典型的是 story/root 工作台根
// 进度 `[Minecraft]`）——服务端**不发聊天播报**，onSystem 永远采不到它，导致 env 侧 tier 从 0
// 直接跳到 2（depth=1 这一档在数据里不存在）。已实跑确认 1.21.11 的 /advancement 命令只有
// grant/revoke、没有 test 查询子命令，故唯一能读到这类静默进度的途径是监听底层协议包
// `bot._client.on("advancements", ...)`——这是 bot 进度界面的数据源，root 也走这条。
//
// 数据来源：服务端推送的 ClientboundAdvancementsPacket（minecraft-protocol emit 成事件名
// "advancements"）。结构：
//   { reset:bool, advancementMapping:[{key, value:{parentId, displayData, requirements}}],
//     identifiers:[], progressMapping:[{key, value:[{criterionIdentifier, criterionProgress}]}] }
// 一个进度「已完成」= 它在 progressMapping 里，且每个 requirements 组各至少有一个 criterion 的
// `criterionProgress` 非 null（null=未达成；有值=[tick,时间戳]=已达成）。story/root 的 criterion
// 是 `crafting_table`。
//
// ★ 与 onSystem 的口径差（预期非 bug，不比对不修正）：
//   ① announce_to_chat 维度：onSystem 只能采 announce_to_chat=true 的进度（带聊天播报）；
//      本采集器靠协议包，能采到 announce_to_chat=false 的静默进度（root）。两路数据并排留痕，
//      差异正是佐证信号（见 env.py discoveries["advancement_packet"] 与 advancements_so_far）。
//   ② 懒推送维度：bot spawn 时服务端只推 recipes 树（reset=true）；story 树（含 root）要等
//      bot **达成至少一个 story criterion** 才推送。bot 若整天没碰 story 进度，协议包里压根没
//      root（storyTreeSeen 标志暴露这一点）。故本采集器未必比 onSystem 更全——两路互补而非替代。
//   ③ 维度层级：onSystem 采集的 advancement 帧带 cause（task/goal/challenge 难度层级），协议包
//      只有「已完成」布尔态、无层级。故 onSystem 的结构化信息不可由本采集器替代。
//
// 形态仿 scoreboard（状态快照采集器，非事件累积器）：名字不以 "on" 开头 → base.js 的
// bot.event("observe") 每次都调它（见 base.js:30 的 on* 跳过规则），无需 flush、无需步首 reset。
// 进度状态跨步持续累积，observe 时取当前快照。
class AdvancementPacket extends Observation {
    constructor(bot) {
        super(bot);
        this.name = "advancementPacket";

        // 已达成进度的显示名集合（如 "[Minecraft]"）—— 与 onSystem 的 advancement 字段、
        // env.py _ADV_INFO 的 key 同源同形（带方括号英文标题）。
        this.completed = new Set();
        // 原始进度 key（如 "minecraft:story/root"）—— 与显示名去重对齐，供诊断/跨树进度核对。
        this.completedKeys = new Set();
        // 是否曾收到过 story 树的推送（健康标志）：懒推送机制下，bot 没碰 story 进度时为 false，
        // 此时 completed 里没有 root 是「没收到包」而非「收到了但没达成」——区分两者靠本标志。
        this.storyTreeSeen = false;

        // lang.json 的进度显示名映射（key -> "[title]"），用 bot.version 加载，无版本硬编码。
        // 例：minecraft:story/root -> "[Minecraft]"。与 env.py _ADV_INFO 同源（都查 advancements.*.title）。
        this._titleByKey = AdvancementPacket._loadTitleMap(bot.version);
        // requirements 缓存（key -> OR 组数组 [[c1,c2],[c3]]）：advancementMapping 带的 requirements
        // 是判定「完成」的权威语义（每组至少一个 criterion 达成），但增量包常只带 progressMapping
        // 不带 mapping，故需跨包缓存。reset 时不清空——requirements 是静态定义，不随进度状态变。
        this._requirementsCache = new Map();

        // 注册协议包监听器。整段 try/catch 包裹：单包解析异常绝不炸 bot 事件循环
        // （协议包处理在 minecraft-protocol 的解析路径上，抛出是灾难性的，仿 onSystem.js:46-52）。
        this.bot._client.on("advancements", (data) => {
            try {
                this._handle(data);
            } catch (e) {
                // 静默吞掉：一个畸形包不该影响后续包，也不该影响 bot 主流程
            }
        });
    }

    // 模块级缓存：同一 version 的 lang.json 只加载一次（多个 bot 共享只读映射）。
    static _titleMapCache = new Map();
    static _loadTitleMap(version) {
        if (AdvancementPacket._titleMapCache.has(version)) {
            return AdvancementPacket._titleMapCache.get(version);
        }
        const out = new Map();
        try {
            const lang = require("minecraft-data")(version).language;
            for (const k in lang) {
                // k 形如 "advancements.story.root.title"（点分隔）-> 进度 id "story/root"（斜杠分隔）。
                // 协议包的 progressMapping/advancementMapping key 用斜杠（"minecraft:story/root"），
                // lang.json 用点，故这里把点转斜杠对齐协议包形态。
                if (k.startsWith("advancements.") && k.endsWith(".title")) {
                    const sub = k.slice("advancements.".length, -".title".length);
                    out.set("minecraft:" + sub.replace(/\./g, "/"), "[" + lang[k] + "]");
                }
            }
        } catch (e) {
            // mcData 缺 language 或版本未知：返回空 map，_lookup 全返 null（completed 恒空，不抛）
        }
        AdvancementPacket._titleMapCache.set(version, out);
        return out;
    }

    _handle(data) {
        if (!data || typeof data !== "object") return;

        // ① reset=true：服务端重发整棵树，清空「已完成」状态重建（否则 revoke/重连后的旧状态会残留）。
        //    spawn 时 recipes 树的 reset=true 会先清一次（此时 completed 本就空，无害）；story 树
        //    首次推送时的 reset=true 把 recipes 阶段的已完成项清掉——但 recipes 不进 _titleByKey
        //    （title map 只收录 advancements.*.title，recipes 是 advancements 之外的命名空间），
        //    故实际无影响。
        if (data.reset) {
            this.completed.clear();
            this.completedKeys.clear();
        }

        // ② 遍历 advancementMapping：记录 story 树推送标志 + 缓存 requirements（判定完成的权威语义）。
        const mappings = Array.isArray(data.advancementMapping) ? data.advancementMapping : [];
        for (const m of mappings) {
            const key = m && m.key;
            if (typeof key !== "string") continue;
            if (key.indexOf("minecraft:story/") === 0) {
                this.storyTreeSeen = true;
            }
            // requirements: OR 组数组 [[c1,c2],[c3]] = (c1 OR c2) AND c3。增量包常不带它，故跨包缓存。
            const reqs = m.value && m.value.requirements;
            if (Array.isArray(reqs)) {
                this._requirementsCache.set(key, reqs);
            }
        }

        // ③ progressMapping 是「已完成」事实源：逐条判定是否全部 criterion 达成。
        const pm = Array.isArray(data.progressMapping) ? data.progressMapping : [];
        for (const entry of pm) {
            const key = entry && entry.key;
            if (typeof key !== "string") continue;
            const disp = this._lookup(key);
            if (!disp) continue;  // 非进度命名空间（如 recipes）或 lang.json 未收录：跳过
            const criteria = Array.isArray(entry.value) ? entry.value : [];
            const done = this._isComplete(key, criteria);
            if (done) {
                this.completed.add(disp);
                this.completedKeys.add(key);
            } else {
                // 部分达成 -> 未完成。从已完成集合移除（防 revoke 后残留）。
                this.completed.delete(disp);
                this.completedKeys.delete(key);
            }
        }
    }

    // 进度「已完成」= 所有 requirements 组各至少一个 criterion 达成（requirements 是 OR 组数组：
    // [[c1,c2],[c3]] = (c1 OR c2) AND c3）。无 requirements 时回退到「所有 criterionProgress 非 null」。
    //
    // ★ criterionProgress 类型是 option<i64>，minecraft-protocol 解码成 Long 对象。Long(0) 仍是真值，
    //   故必须用 `!= null` 判「已达成」（存在性），不能用真值判——这是本采集器最易错的点。
    _isComplete(key, criteria) {
        const done = new Set();
        for (const c of criteria) {
            if (c && c.criterionIdentifier != null && c.criterionProgress != null) {
                done.add(c.criterionIdentifier);
            }
        }
        // requirements 从本包的 advancementMapping 取（若有同 key 的 mapping 带 requirements）。
        // 增量包可能只带 progressMapping 不带 mapping，此时回退到「所有非 null」。
        const reqs = this._requirementsCache && this._requirementsCache.get(key);
        if (reqs && Array.isArray(reqs) && reqs.length) {
            return reqs.every((group) => Array.isArray(group) && group.some((c) => done.has(c)));
        }
        return criteria.length > 0 && criteria.every((c) => c && c.criterionProgress != null);
    }

    _lookup(key) {
        return this._titleByKey.get(key) || null;
    }

    observe() {
        return {
            completed: Array.from(this.completed).sort(),
            completedKeys: Array.from(this.completedKeys).sort(),
            storyTreeSeen: this.storyTreeSeen,
        };
    }

    // 状态持续累积，步首不清空（同 scoreboard）。
    reset() {}
}

module.exports = AdvancementPacket;
