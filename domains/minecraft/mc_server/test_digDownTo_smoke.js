/**
 * digDownTo 冒烟测试 —— **不连 MC 服务器**，用假 bot 跑。
 *
 *     node test_digDownTo_smoke.js
 *
 * 钉住 2026-08-25 的修复：LLM 手写 `while` 反复 `bot.dig` 脚下方块会死循环锁死进程
 * （挖开后 bot 不会自动掉进 1 格洞 → y 不变 → 第二轮起无 await → 纯同步空转）。
 * digDownTo 的核心正确性是「挖脚下 → 等 bot 真的掉进去 → 没掉就主动走进洞口」，
 * 这里用 mock 验证这条链路及其护栏。
 *
 * 测：
 *  ① 正常逐格下挖到 targetY（每挖一块 y 减 1，模拟重力下落）
 *  ② 脚下是 lava → 停下报原因，不挖
 *  ③ 脚下是 bedrock（diggable=false）→ 停下报原因
 *  ④ deadline 到 → 提前收手报「本步时限快到了」
 *  ⑤ targetY 非法（NaN）→ 抛错
 *  ⑥ targetY 不低于当前 y → 什么都没做（边界）
 *  ⑦ 核心正确性：挖开后 bot 没掉下去（y 不变）→ 主动挪一步触发下落；挪了仍没掉 → 停下报原因
 *    （这是手写 while 死循环的源头，必须有兜底）
 */
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const SRC = fs.readFileSync(
    path.join(__dirname, "control_primitives", "digDownTo.js"),
    "utf-8"
);

async function runOnce(makeBot, targetY) {
    const bot = makeBot();
    const wrappedSrc =
        SRC +
        "\n;globalThis.__run = (async () => {" +
        "  let ret;" +
        "  try { ret = await digDownTo(bot, " + JSON.stringify(targetY) + "); }" +
        "  catch(e){ globalThis.__runErr = e; }" +
        "  globalThis.__ret = ret;" +
        "})();";
    const ctx = {
        bot, console, setTimeout, clearTimeout, Date, Math, Object,
        Promise, require,
        Vec3: require(path.join(__dirname, "node_modules", "vec3")).Vec3,
        globalThis: {},
    };
    vm.createContext(ctx);
    vm.runInContext(wrappedSrc, ctx);
    await ctx.globalThis.__run;
    return {
        chats: bot.__chats,
        ret: ctx.globalThis.__ret,
        runErr: ctx.globalThis.__runErr,
        dug: bot.__dugCount,
    };
}

/** 造一个方块对象。name="air" 时 diggable 可省。 */
function block(name, pos) {
    return {
        name,
        position: pos,
        diggable: name !== "bedrock" && name !== "air",
    };
}

/**
 * 造 bot：脚下(y-1)方块由 belowSeq 依次给出；每次 dig 成功后若 markFall=true 则 y 减 1。
 * nudgeFail=true 时，即使挖开/脚下是 air，bot 也不掉（模拟站边沿/被卡），用于测兜底。
 */
function makeBot(opts = {}) {
    const chats = [];
    const belowSeq = opts.belowSeq || ["stone", "stone", "stone"]; // 依次脚下方块
    let belowIdx = 0;
    let dugCount = 0;
    let y = opts.startY != null ? opts.startY : 64;
    const markFall = opts.markFall !== false; // 默认挖完就掉
    const nudgeFail = !!opts.nudgeFail;
    const entity = {
        position: {
            get x() { return 0; },
            get y() { return y; },
            get z() { return 0; },
            floored: () => ({ x: 0, y: Math.floor(y), z: 0, offset(dx, dy, dz) { return { x: 0, y: Math.floor(y) + dy, z: 0 }; } }),
        },
    };
    const bot = {
        __chats: chats,
        __dugCount: 0,
        openkageTickRate: 1,
        openkageStepDeadlineMs: opts.deadlineMs,
        entity,
        blockAt: (p) => {
            // 脚下方块（feet-1）：按序列给；序列耗尽给 air（挖穿了）
            if (p.y === Math.floor(y) - 1) {
                if (belowIdx < belowSeq.length) return block(belowSeq[belowIdx], p);
                return block("air", p);
            }
            return block("stone", p);
        },
        dig: async (b) => {
            dugCount++;
            bot.__dugCount = dugCount;
            belowIdx++; // 挖掉后，下次这个位置变 air
            if (markFall && !nudgeFail) {
                await bot.waitForTicks(1);
                y -= 1; // 重力下落
            }
        },
        waitForTicks: async () => {},
        setControlState: () => {},
        tool: { equipForBlock: async () => {} },
        chat: (m) => chats.push(m),
    };
    return bot;
}

async function main() {
    let pass = 0, fail = 0;
    const check = (name, cond, detail) => {
        if (cond) { pass++; console.log(`  ✓ ${name}`); }
        else { fail++; console.log(`  ✗ ${name}  ${detail || ""}`); }
    };

    // ① 正常逐格下挖 64→61（3 格 stone）
    console.log("① 正常下挖到 targetY");
    {
        const bot = makeBot({ belowSeq: ["stone", "stone", "stone"], startY: 64 });
        const r = await runOnce(() => bot, 61);
        check("未抛错", !r.runErr, r.runErr && r.runErr.message);
        check("返回 y=61", r.ret === 61, `ret=${r.ret}`);
        check("挖了 3 块", r.dug === 3, `dug=${r.dug}`);
        check("播报含'挖掉 3 块'", r.chats.join(" ").includes("挖掉 3 块"), r.chats.join(" "));
    }

    // ② 脚下 lava → 停下不挖
    console.log("② 脚下 lava 停下");
    {
        const bot = makeBot({ belowSeq: ["lava"], startY: 64 });
        const r = await runOnce(() => bot, 60);
        check("未抛错", !r.runErr, r.runErr && r.runErr.message);
        check("没挖", r.dug === 0, `dug=${r.dug}`);
        check("播报含 lava 危险", r.chats.join(" ").includes("lava"), r.chats.join(" "));
    }

    // ③ 脚下 bedrock → 停下
    console.log("③ 脚下 bedrock 停下");
    {
        const bot = makeBot({ belowSeq: ["bedrock"], startY: 64 });
        const r = await runOnce(() => bot, 60);
        check("未抛错", !r.runErr, r.runErr && r.runErr.message);
        check("没挖", r.dug === 0, `dug=${r.dug}`);
        check("播报含'挖不动'", r.chats.join(" ").includes("挖不动"), r.chats.join(" "));
    }

    // ④ deadline 到 → 提前收手
    console.log("④ deadline 到提前收手");
    {
        const bot = makeBot({
            belowSeq: ["stone", "stone", "stone", "stone", "stone"],
            startY: 64,
            deadlineMs: Date.now() - 1, // 已过期
        });
        const r = await runOnce(() => bot, 50);
        check("未抛错", !r.runErr, r.runErr && r.runErr.message);
        check("播报含'本步时限快到了'", r.chats.join(" ").includes("本步时限快到了"), r.chats.join(" "));
    }

    // ⑤ targetY 非法 → 抛错
    console.log("⑤ targetY=NaN 抛错");
    {
        const bot = makeBot({ startY: 64 });
        const r = await runOnce(() => bot, NaN);
        check("抛错", !!r.runErr, "未抛错");
        check("错误含 targetY", String(r.runErr && r.runErr.message).includes("targetY"), r.runErr && r.runErr.message);
    }

    // ⑥ targetY 不低于当前 y → 什么都没做
    console.log("⑥ targetY>=当前y 边界");
    {
        const bot = makeBot({ startY: 64 });
        const r = await runOnce(() => bot, 70);
        check("未抛错", !r.runErr, r.runErr && r.runErr.message);
        check("返回原 y=64", r.ret === 64, `ret=${r.ret}`);
        check("没挖", r.dug === 0, `dug=${r.dug}`);
    }

    // ⑦ 核心正确性：挖开后 bot 没掉（nudgeFail）→ 挪一步仍没掉 → 停下报原因，不空转
    console.log("⑦ 挖开后没掉 → 挪步仍没掉 → 停下报原因（不死循环）");
    {
        const bot = makeBot({ belowSeq: ["stone"], startY: 64, nudgeFail: true });
        const r = await runOnce(() => bot, 60);
        check("未抛错", !r.runErr, r.runErr && r.runErr.message);
        check("只挖了 1 块就停（没死循环）", r.dug === 1, `dug=${r.dug}`);
        check("播报含'没掉下去'", r.chats.join(" ").includes("没掉下去"), r.chats.join(" "));
        // 关键：没有挂死（runOnce 能返回本身就证明没死循环锁死 vm）
        check("未挂死（返回了）", typeof r.ret === "number", `ret=${r.ret}`);
    }

    console.log(`\n${pass} passed, ${fail} failed`);
    process.exit(fail === 0 ? 0 : 1);
}

main().catch((e) => { console.error("smoke 崩了:", e); process.exit(2); });
