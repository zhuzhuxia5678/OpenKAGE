const Observation = require("./base.js").Observation;

// 单步回传上限：40 行 × 300 字符，够 LLM 汇报进度，又不至于被刷屏日志冲掉上下文。
const MAX_LINES = 40;
const MAX_LINE_CHARS = 300;

// 把生成代码里的 console.log 捞回观察。
//
// 为什么需要：LLM 天然爱用 console.log 汇报进度，但 eval 出来的 console.log 写的是 node
// 进程 stdout，而 Python 侧用 subprocess.DEVNULL 起的 mineflayer server —— 这些自述
// 100% 蒸发，LLM 在对 /dev/null 说话。
//
// 为什么不改 prompt 让它用 bot.chat 代替：bot.chat 会真发到游戏聊天框（256 字符上限、服务器
// rate-limit、污染 MC 日志）；console.log 语义上就是「给自己看的调试输出」，正对得上。
// 捕获后它就成了 LLM 主动设计的观测通道（能力增长点，值得留在轨迹里）。
//
// 劫持 console 在 /step 里按步开关（见 index.js captureConsole），本类只负责缓冲 + flush。
class onLog extends Observation {
    constructor(bot) {
        super(bot);
        this.name = "onLog";
        this.lines = [];
        bot.openkageLog = this; // 供 index.js 的 console 劫持直接 push
    }

    // 由 console 劫持逐行调用。**不**在这里 bot.event()：console.log 频率远高于 chat，
    // 每行都发一次事件会产生一堆全量快照撑爆 cumulativeObs。攒着，步末 flush 一次。
    push(line) {
        // 单行截断 + 条数封顶：防 LLM 写循环刷屏把上下文冲掉（超出部分丢弃并在 flush 时标注）
        if (this.lines.length >= MAX_LINES) {
            this.dropped = (this.dropped || 0) + 1;
            return;
        }
        this.lines.push(String(line).slice(0, MAX_LINE_CHARS));
    }

    // 步末调一次：有内容才发 onLog 事件（base.js 的 bot.event 会跳过非当前事件的 on* 插件，
    // 所以不显式发这一下，onLog 永远不会出现在 observe 结果里）。
    flush() {
        if (!this.lines.length) return;
        this.bot.event(this.name);
    }

    observe() {
        let result = this.lines.join("\n");
        if (this.dropped) {
            result += `\n（另有 ${this.dropped} 行日志被丢弃：单步最多回传 ${MAX_LINES} 行）`;
        }
        this.lines = [];
        this.dropped = 0;
        return result;
    }

    reset() {
        this.lines = [];
        this.dropped = 0;
    }
}

module.exports = onLog;
