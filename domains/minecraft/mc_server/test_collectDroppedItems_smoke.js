/**
 * collectDroppedItems 行为契约测试 —— **不连 MC 服务器**，用假 bot 跑。
 *
 *     node test_collectDroppedItems_smoke.js
 *
 * 测什么（collectDroppedItems.js）：
 *   ① radius 内无掉落物 → bot.chat 提示 + failCount 累加，不抛错
 *   ② 有掉落物 → 逐个 collect，按距离升序，播报捡了 N 件
 *   ③ 单件 collect 抛错（够不着）→ 跳过、setGoal(null)、继续下一件，不抛错
 *   ④ 整体 maxTicks 到点 → 剩余件记 skipped 停手
 *   ⑤ 捡取是可选收益：单件失败不抛，整批全失败才 failCount 累加
 *
 * ⚠️ 原语依赖 eval 闭包里的全局 `_collectDroppedItemsFailCount`，这里在 mock 作用域里
 *    声明同名变量喂给它（与 index.js:912 的 let 同构）。
 */
const fs = require("fs");
const path = require("path");
const Vec3 = require(path.join(__dirname, "node_modules", "vec3")).Vec3;

// 读原语源码
const SRC = fs.readFileSync(
    path.join(__dirname, "control_primitives", "collectDroppedItems.js"),
    "utf-8"
);

// 跑一次：给定 mock bot 配置，返回 {chats, failCount, runErr}
// 用 vm 把原语源码包进一个闭包，让 _collectDroppedItemsFailCount 成为真正的 let 闭包变量
// （原语对它做 ++，必须可变闭包，不能是值拷贝参数）。
function runOnce(makeBot) {
    const bot = makeBot();
    return (async () => {
        const vm = require("vm");
        const wrappedSrc =
            "let _collectDroppedItemsFailCount = 0;\n" +
            SRC +
            "\n;globalThis.__run = (async () => {" +
            "  try { await collectDroppedItems(bot, radius, maxTicks); }" +
            "  catch(e){ globalThis.__runErr = e; }" +
            "  globalThis.__failCount = _collectDroppedItemsFailCount;" +
            "})();";
        const ctx = {
            bot, console, setTimeout, clearTimeout, Date, Math, Object, Promise,
            require, Vec3, globalThis: {},
            // 测试参数：collectDroppedItems 的 radius/maxTicks 由测试用例控制
            radius: bot.__radius, maxTicks: bot.__maxTicks,
        };
        vm.createContext(ctx);
        vm.runInContext(wrappedSrc, ctx);
        await ctx.globalThis.__run;
        return {
            chats: bot.__chats,
            failCount: ctx.globalThis.__failCount,
            runErr: ctx.globalThis.__runErr,
        };
    })();
}

// ---- mock bot 工厂 ----
function makeBot(opts = {}) {
    const myPos = opts.pos || new Vec3(0, 64, 0);
    const entities = opts.entities || {};
    const collectResults = opts.collectResults || {}; // entity.id -> 'ok' | throw msg
    const inventoryCounts = opts.inventoryCounts || [10]; // 每次 items() 返回的 count 序列
    let invIdx = 0;
    const chats = [];
    const bot = {
        __chats: chats,
        __radius: opts.radius != null ? opts.radius : 8, // 传给 collectDroppedItems 的 radius
        __maxTicks: opts.maxTicks != null ? opts.maxTicks : 200, // 传给 collectDroppedItems 的 maxTicks
        openkageTickRate: 1,
        entity: { position: myPos },
        entities,
        inventory: {
            items: () => [{ count: inventoryCounts[Math.min(invIdx++, inventoryCounts.length - 1)] }],
        },
        pathfinder: { setGoal: () => {} },
        collectBlock: {
            collect: async (entity) => {
                const r = collectResults[entity.id];
                if (r && r.startsWith("throw:")) throw new Error(r.slice(6));
                // 成功：实体消失（caller 不管，这里模拟）
            },
            targets: { clear: () => {} },
        },
        chat: (m) => chats.push(m),
        emit: () => {},
    };
    return bot;
}

function mkItemEntity(id, pos) {
    return {
        id,
        name: "item",
        position: pos,
        isValid: true,
        distanceTo: function (p) {
            return Math.sqrt(
                (this.position.x - p.x) ** 2 +
                (this.position.y - p.y) ** 2 +
                (this.position.z - p.z) ** 2
            );
        },
    };
}

async function main() {
    let pass = 0,
        fail = 0;
    const check = (name, cond, detail) => {
        if (cond) {
            pass++;
            console.log(`  ✓ ${name}`);
        } else {
            fail++;
            console.log(`  ✗ ${name}  ${detail || ""}`);
        }
    };

    // ① radius 内无掉落物
    console.log("① 无掉落物 → 提示 + failCount+1，不抛错");
    {
        const bot = makeBot({ entities: {}, inventoryCounts: [10] });
        const r = await runOnce(() => bot);
        check("有提示", r.chats.length === 1 && r.chats[0].includes("没有掉落物"), JSON.stringify(r.chats));
        check("failCount=1", r.failCount === 1, `got ${r.failCount}`);
        check("未抛错", !r.runErr, r.runErr && r.runErr.message);
    }

    // ② 有 3 个掉落物，都成功 → 播报捡了 3 件
    console.log("② 3 件都成功 → 捡了 3 件");
    {
        const e1 = mkItemEntity("a", new Vec3(1, 64, 0)); // dist 1
        const e2 = mkItemEntity("b", new Vec3(2, 64, 0)); // dist 2
        const e3 = mkItemEntity("c", new Vec3(3, 64, 0)); // dist 3
        const bot = makeBot({
            entities: { a: e1, b: e2, c: e3 },
            collectResults: { a: "ok", b: "ok", c: "ok" },
            inventoryCounts: [10, 11, 13, 16], // before=10, after each +1,+2,+3 → 16
        });
        const r = await runOnce(() => bot);
        check("播报含'捡了 3 件'", r.chats.some((c) => c.includes("捡了 3 件")), JSON.stringify(r.chats));
        check("未抛错", !r.runErr, r.runErr && r.runErr.message);
    }

    // ③ 中间一件够不着（collect 抛错）→ 跳过、继续后面的
    console.log("③ 中间件够不着 → 跳过、捡其余");
    {
        const e1 = mkItemEntity("a", new Vec3(1, 64, 0));
        const e2 = mkItemEntity("b", new Vec3(2, 64, 0));
        const e3 = mkItemEntity("c", new Vec3(3, 64, 0));
        const bot = makeBot({
            entities: { a: e1, b: e2, c: e3 },
            collectResults: { a: "ok", b: "throw:unreachable", c: "ok" },
            inventoryCounts: [10, 11, 11, 12],
        });
        const r = await runOnce(() => bot);
        check("捡了 2 件", r.chats.some((c) => c.includes("捡了 2 件")), JSON.stringify(r.chats));
        check("跳过 1 件", r.chats.some((c) => c.includes("跳过 1 件")), JSON.stringify(r.chats));
        check("未抛错", !r.runErr, r.runErr && r.runErr.message);
    }

    // ④ radius 外的掉落物不扫
    console.log("④ radius 外不扫");
    {
        const e1 = mkItemEntity("a", new Vec3(1, 64, 0)); // dist 1, in
        const e2 = mkItemEntity("b", new Vec3(20, 64, 0)); // dist 20, out (radius=8)
        const bot = makeBot({
            entities: { a: e1, b: e2 },
            collectResults: { a: "ok" },
            inventoryCounts: [10, 11],
        });
        const r = await runOnce(() => bot);
        check("只捡 1 件（radius 外的 b 没扫）", r.chats.some((c) => c.includes("捡了 1 件")), JSON.stringify(r.chats));
    }

    // ⑤ 全部够不着 → failCount+1，不抛（<10）
    console.log("⑤ 全部够不着 → failCount+1，不抛错");
    {
        const e1 = mkItemEntity("a", new Vec3(1, 64, 0));
        const bot = makeBot({
            entities: { a: e1 },
            collectResults: { a: "throw:unreachable" },
            inventoryCounts: [10, 10],
        });
        const r = await runOnce(() => bot);
        check("failCount=1", r.failCount === 1, `got ${r.failCount}`);
        check("未抛错", !r.runErr, r.runErr && r.runErr.message);
    }

    console.log(`\n${pass} passed, ${fail} failed`);
    process.exit(fail === 0 ? 0 : 1);
}

main().catch((e) => {
    console.error("smoke 崩了:", e);
    process.exit(2);
});
