const Observation = require("./base.js").Observation;

// 单步回传上限：与 onLog 同口径（40 条 × 300 字符）。
//
// 为什么现在才加（2026-09-23 补）：此前这里是三个 on* 插件里**唯一没有封顶**的 ——
//   onLog  40 行 × 300 字符 + dropped 标注；onSystem MAX_EVENTS=20 + dropped；
//   onChat `this.obs += message`，条数与单条长度都无限，且**每条消息立刻 bot.event()**。
// 后者更贵：base.js 的 bot.event 每调用一次就往 cumulativeObs 塞一份**全量快照**
// （status/inventory/chests/voxels/scoreboard/…全部重算），于是「一步里循环调原语挖 8 个方块」
// 会换来 8 份全量快照 —— 这正是 onLog.js:26-27 特意规避的（它攒着步末 flush 一次）。
//
// 单条长度上限也不能省：chatEvent 是**内部自造事件**（mineflayer 的真聊天走 messagestr，
// 被 onSystem 按 position!=="system" 丢了），唯一发射点是 skillLoader 的 bot.chat 改写
// （原语反馈 + 「指令被拦」提示）。原语都是定长短句，但 LLM 生成代码里 `bot.chat(超长串)`
// 走的是同一条路，且**不受 MC 聊天 256 字节限制** —— 没有上限就是无上限。
const MAX_MESSAGES = 40;
const MAX_MSG_CHARS = 300;

// 把「给 LLM 看的执行反馈」收进观察。
//
// 来源见上：只有 bot.chat 的非斜杠分支 emit 的 chatEvent（原语反馈、指令拦截提示）。
// 斜杠开头的是真游戏指令（/gamerule、/give），已真发给服务器执行 —— 不回传 LLM
// （否则它看到的会是服务器回显，而非自己动作的反馈）。
class onChat extends Observation {
    constructor(bot) {
        super(bot);
        this.name = "onChat";
        this.messages = [];
        this.dropped = 0;
        bot.openkageChat = this; // 供 index.js 在步末 flush、步首 reset
        bot.on("chatEvent", (username, message) => {
            // Save entity status to local variable
            if (message.startsWith("/")) {
                return;
            }
            this._push(String(message));
            // ★ **不在这里 bot.event()**（与 onLog 同理）：每条都发一次事件 = 每条都产生一份
            //   全量快照，撑爆 cumulativeObs。攒着，步末由 index.js flush 一次。
        });
    }

    _push(text) {
        // 条数封顶：防循环刷屏把上下文冲掉（超出部分丢弃并在 observe 时标注）
        if (this.messages.length >= MAX_MESSAGES) {
            this.dropped += 1;
            return;
        }
        // 单条截断：超长时留个「…」尾巴，别让 LLM 以为它看到了完整的那句话
        this.messages.push(
            text.length > MAX_MSG_CHARS ? text.slice(0, MAX_MSG_CHARS) + "…" : text
        );
    }

    // 步末调一次（index.js）：有内容才发 onChat 帧。不显式发这一下，base.js 的
    // bot.event("observe") 会跳过名字以 "on" 开头的插件，onChat 就永远不出现在 observe 结果里。
    flush() {
        if (!this.messages.length) return;
        this.bot.event(this.name);
    }

    observe() {
        // ★ 返回**字符串**（不是 {messages, dropped}）：Python 侧 _collect_events 直接
        //   str(payload["onChat"]) 当文本用（渲染进反馈与轨迹），换结构会让它变成 dict repr。
        //   与 onLog 同形，故 dropped 也照它那样拼在末尾一行。
        let result = this.messages.join("\n");
        if (this.dropped) {
            result += `\n（另有 ${this.dropped} 条反馈被丢弃：单步最多回传 ${MAX_MESSAGES} 条）`;
        }
        this.messages = [];
        this.dropped = 0;
        return result;
    }

    // ★ **与 onSystem 相反，这里要清空**：onSystem 攒的是跨步到达的死亡/成就（清空=漏计），
    //   而 onChat 攒的是本步动作的反馈 —— 留到下一步就是「跨步噪音」（同类问题见
    //   domains/minecraft/__init__.py 里日界前不清 last_events 导致的跨天泄漏）。步首 index.js
    //   调 reset()，漏 await 的原语在 /step 返回后才 emit 的那几条就此丢弃，不污染下一步。
    reset() {
        this.messages = [];
        this.dropped = 0;
    }
}

module.exports = onChat;
