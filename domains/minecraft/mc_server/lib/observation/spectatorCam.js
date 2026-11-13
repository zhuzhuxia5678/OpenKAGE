const Observation = require("./base.js").Observation;

// 把**观战账号的坐标**收进观察，作 footage 录像附身状态的探针（2026-09-15）。
//
// 为什么需要：spectator 脱附（bot 死亡/断连重连后实体移除）是**完全静默**的——没有任何日志或
// 回执，相机弹回观战者自己的身体停在脱附点，此后画面全是空镜（2026-08-13 实踩：三个观战账号
// 精确停在各自 bot 首次死亡的坐标，day3~day5 录像作废）。env.py 侧已有死亡重附/天边界兜底，但
// 「bot 断连自愈重连」（实体移除→脱附，无死亡播报）与「栅栏失效的步间死亡」两条路径此前只能等
// 天边界才修正。本采集器让 env 在每步的 observe payload 里读到「相机现在是否跟着 bot」，
// 脱附时下一步立刻触发 resync_spectate，把画面错位窗口从「最多一天」压到「一步」。
//
// ★ 数据源为什么是 /data get entity <spec> Pos（真服实测三态验证，见 test_spectator_cam_live.js）：
//   · spectate 附身时，服务器**每刻把观战者实体的 Pos 写成 bot 位置**（spectate 机制的实现）——
//     /data get 返回 bot 当前坐标（bot 位移后仍相同）；
//   · 脱附后（bot 死亡/断连），服务器侧观战者实体**停在脱附点**——/data get 返回旧坐标，
//     bot 重生/走动后与 bot 当前坐标拉开距离；
//   · 服务器权威，不依赖任何客户端的缓存/广播。
//   ✗ 两条**不可行**的路（同样真服实测，别再走）：
//   · bot.entities / bot.players[spec].entity —— spectator 实体**不进其他玩家的实体同步**，
//     entity 恒 null（误判永远 attached=false 或探针读不到）；
//   · 观战客户端侧的 camera 包（附身=cameraId 指向 bot 实体）——信号完美但只有观战客户端
//     收得到，而生产环境观战侧是真 MC 客户端（client.jar），该包不落 client.log。
//
// ⚠️ 前提（与 footage._ensure_op 同源）：bot 必须是 OP（ops.json level 4），否则 /data get
// 静默失败（无回执）→ probeStale=true，Python 侧按「不判定」处理，不算脱附。
//
// 查询节流：observe() 距上次发查询 ≥ PROBE_INTERVAL_MS 才重发一条（回执异步到达、下一次
// observe 消费）；间隔内的 observe 返回上次回执的缓存。回执解析 [xd, yd, zd] 形如
// "[25.5d, 68.0d, 17.5d]"（NBT 双精度后缀 d）。
//
// 判据（Python 侧 _spectator_needs_reattach）：attached = 观战者坐标与 bot 坐标距离 ≤ 阈值
// （阈值由 Python 侧定，这里只报原始坐标）。形态仿 scoreboard（状态快照采集器，非事件累积器）。
class SpectatorCam extends Observation {
    constructor(bot) {
        super(bot);
        this.name = "spectatorCam";
        // 观战账号名。index.js /start 时把 Python 侧传的 body.spectatorName 挂在 bot 上
        // （FootageRecorder.spectator_name，形如 "spec_<port>"）；null（未传，如对照组/老调用方）
        // 时本探针停用——observe 恒返回 null 坐标，Python 侧按「不判定」处理。
        this.spectatorName = bot.spectatorName || null;
        // 最近一次回执解析出的观战者坐标 {x,y,z}；null = 还没收到过回执。
        this.lastPos = null;
        // 最近一次发出查询的时刻（ms）；0 = 从未发过。
        this.lastProbeAt = 0;
        // 是否仍持有一条未被新回执覆盖的过期查询（发了没回 = 命令通道异常/无 OP），供 Python 判「不判定」。
        this.probeStale = false;
        this._bound = false;
        this._bind();
    }

    _bind() {
        if (this._bound || !this.spectatorName) return;
        this._bound = true;
        // 系统消息回执（/data get 的返回是 system chat，非普通聊天）。整段 try/catch：
        // 单条消息解析异常绝不炸 bot 事件循环（同 onSystem.js 的韧性要求）。
        this.bot.on("message", (msg) => {
            try {
                this._handle(msg);
            } catch (e) {
                // 静默吞掉：一条畸形消息不该影响探针，更不该影响 bot 主流程
            }
        });
    }

    _handle(msg) {
        // 回执形如 "spec_25610 has the following entity data: [25.5d, 68.0d, 17.5d]"
        // （先名字后 data，双重匹配避免别的 /data get 回执串台）。
        const s = msg && msg.toString ? msg.toString() : String(msg);
        if (!s || s.indexOf(this.spectatorName) === -1) return;
        const m = s.match(/\[(-?[\d.]+)d,\s*(-?[\d.]+)d,\s*(-?[\d.]+)d\]/);
        if (!m) return;
        this.lastPos = { x: parseFloat(m[1]), y: parseFloat(m[2]), z: parseFloat(m[3]) };
        this.probeStale = false;
    }

    observe() {
        // 未配观战名（对照组/未传 body.spectatorName）：探针停用，恒报 null（Python 判「不判定」）。
        if (!this.spectatorName) {
            return { pos: null, distance: null, probeStale: false, botPos: null };
        }
        const now = Date.now();
        if (now - this.lastProbeAt >= SpectatorCam.PROBE_INTERVAL_MS) {
            this.lastProbeAt = now;
            this.probeStale = true; // 发出即置过期，收到回执才清（覆盖窗口内的 observe）
            // ★ 走 bot._chat（skillLoader 保存的原始 chat）：包装版对非斜杠消息只 emit 不发服，
            //   这里发的是斜杠命令、两条路都会真发，但用原始通道语义更明确（基建探针，与 LLM
            //   反馈通道无关）。裸 bot（无 skillLoader，如 live 测试）退回 bot.chat。
            const send = this.bot._chat || this.bot.chat;
            send.call(this.bot, `/data get entity ${this.spectatorName} Pos`);
        }
        const self = this.bot.entity && this.bot.entity.position;
        let distance = null;
        if (this.lastPos && self) {
            distance = this.lastPos.x !== null ? Math.sqrt(
                (this.lastPos.x - self.x) ** 2 +
                (this.lastPos.y - self.y) ** 2 +
                (this.lastPos.z - self.z) ** 2) : null;
        }
        return {
            // 观战者坐标（服务器权威，null=尚无回执）
            pos: this.lastPos,
            // 与 bot 当前坐标的直线距离（null=无法计算）
            distance: distance !== null ? Math.round(distance * 10) / 10 : null,
            // 最新一次查询是否有回执覆盖（false=回执正常；true=发了没回，通道异常/无 OP）
            probeStale: this.probeStale,
            // bot 自己的坐标快照（Python 侧算距离/留痕用，避免跨消息读 bot.entity 有竞态）
            botPos: self ? { x: self.x, y: self.y, z: self.z } : null,
        };
    }

    // 状态跨步持续（lastPos/lastProbeAt），步首不清空。
    reset() {}
}

// 查询间隔：探针一条 /data get ≈ 服务器 200ms 内回执，步与步之间间隔几秒到几十秒——
// 5s 足够新鲜（脱附后最多 5s+一步长 的探测延迟），又不会刷屏聊天通道。
SpectatorCam.PROBE_INTERVAL_MS = 5000;

module.exports = SpectatorCam;
