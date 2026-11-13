/**
 * collectBlock.collect 的 Vec3 误传拦截 —— 行为契约测试，**不连 MC 服务器**。
 *
 *     node test_collectblock_vec3_guard_smoke.js
 *
 * 测什么（index.js 里 `if (!bot.__openkageCollectGuarded)` 那段 wrapper）：
 *   LLM 把 `bot.findBlocks()` 返回的 Vec3 数组（{x,y,z}，无 .position）直接喂给
 *   `bot.collectBlock.collect(targets)`，库内部 getClosest 取 `target.position.distanceTo(...)`
 *   会崩成干巴的 "Cannot read properties of undefined (reading 'distanceTo')"。wrapper 在
 *   collect 入口拦下，抛带「原因+正确写法」的干净错误（_openkageClean=true）。
 *
 *   A：Vec3 数组 → 抛 _openkageClean 错，消息含 blockAt+distanceTo，orig 未被调
 *   B：单个 Vec3（非数组）→ 抛错，消息含 targets[0]
 *   C：Block 数组（{position,name}）→ 透传，orig 被调、参数是原始 target
 *   D：Entity（{position,isValid}）→ 透传
 *   E：混合数组（Block + Vec3）→ 抛错，消息含 targets[1]
 *   F：callback 模式（传 cb）→ cb 被调、不抛（验证 callbackify 再回调不吞参）
 *   G：装两次 → 只包一层（幂等）
 *
 * wrapper 是 index.js 里嵌在 /step 函数体里的一小段，这里内联安装（同 test_killMob_smoke
 * 的思路：不抽源码，直接复制挂载逻辑到测试里），测的是判据/透传/幂等本身。
 */

// ── 内联：与 index.js 的 wrapper 完全一致 ──────────────────────────────
function installGuard(bot) {
    if (!bot.__openkageCollectGuarded) {
        if (bot.collectBlock && typeof bot.collectBlock.collect === "function") {
            bot.__openkageCollectGuarded = true;
            const _origCollect = bot.collectBlock.collect.bind(bot.collectBlock);
            bot.collectBlock.collect = function (target, options, cb) {
                const arr = Array.isArray(target) ? target : [target];
                for (let i = 0; i < arr.length; i++) {
                    const t = arr[i];
                    if (t === null || t === undefined) continue;
                    if (typeof t.x === "number" && t.position === undefined) {
                        const _e = new Error(
                            `collectBlock.collect: targets[${i}] 是 Vec3 坐标（有 x/y/z 但没有 .position），` +
                            `不能直接传给 collect —— 内部 getClosest 要 \`target.position.distanceTo(...)\`，` +
                            `Vec3 没有 .position 会崩。findBlocks() 返回 Vec3 数组，正确写法：先转成 Block —— ` +
                            `\`targets = vec3s.map(v => bot.blockAt(v)); await bot.collectBlock.collect(targets)\``
                        );
                        _e._openkageClean = true;
                        throw _e;
                    }
                }
                return _origCollect(target, options, cb);
            };
        }
    }
}

// 假 bot 工厂：collect 记录被调情况
//   opts.origImpl: 原生 collect 的实现（默认异步 noop）；opts.origThrows: 原生直接抛
function makeBot(opts = {}) {
    let origCalled = false;
    let origArgs = null;
    const bot = {
        entity: { position: { x: 0, y: 64, z: 0, distanceTo: () => 0 } },
        collectBlock: {
            // 真实 collect 签名 collect(target, options={}, cb)，支持 callback
            collect: function (target, options, cb) {
                origCalled = true;
                origArgs = { target, options, cb };
                if (opts.origThrows) throw opts.origThrows;
                const p = opts.origImpl ? opts.origImpl(target, options) : Promise.resolve();
                if (typeof cb === "function") {
                    Promise.resolve(p).then(() => cb(null)).catch((e) => cb(e));
                }
                return p;
            },
            targets: { clear: () => {} },
        },
    };
    bot.__origCalled = () => origCalled;
    bot.__origArgs = () => origArgs;
    return bot;
}

async function main() {
    let pass = 0, fail = 0;
    const check = (name, cond, detail) => {
        if (cond) { pass++; console.log(`  ✓ ${name}`); }
        else { fail++; console.log(`  ✗ ${name}  ${detail || ""}`); }
    };
    const collect = (bot, target, options, cb) =>
        bot.collectBlock.collect(target, options, cb);

    // A：Vec3 数组 → 抛 _openkageClean 错，消息含 blockAt+distanceTo，orig 未被调
    console.log("A：Vec3 数组 → 抛 _openkageClean 干净错，orig 未被调");
    {
        const bot = makeBot({ origThrows: new Error("should not reach") });
        installGuard(bot);
        let err = null;
        try { await collect(bot, [{ x: 1, y: 64, z: 2 }, { x: 3, y: 64, z: 4 }]); }
        catch (e) { err = e; }
        check("抛了错", err instanceof Error, err);
        check("_openkageClean=true", err && err._openkageClean === true, "标记缺失");
        check("消息含 blockAt 正确写法", err && err.message.includes("blockAt"), err && err.message);
        check("消息含 distanceTo 崩因", err && err.message.includes("distanceTo"), err && err.message);
        check("orig 未被调", bot.__origCalled() === false, "应拦在入口");
    }

    // B：单个 Vec3（非数组）→ 抛错，消息含 targets[0]
    console.log("B：单个 Vec3（非数组）→ 抛错，消息含 targets[0]");
    {
        const bot = makeBot({ origThrows: new Error("should not reach") });
        installGuard(bot);
        let err = null;
        try { await collect(bot, { x: 1, y: 64, z: 2 }); }
        catch (e) { err = e; }
        check("抛了错", err instanceof Error, err);
        check("_openkageClean=true", err && err._openkageClean === true, "标记缺失");
        check("消息含 targets[0]", err && err.message.includes("targets[0]"), err && err.message);
        check("orig 未被调", bot.__origCalled() === false, "应拦在入口");
    }

    // C：Block 数组（{position,name}）→ 透传，orig 被调、参数是原始 target
    console.log("C：Block 数组 → 透传，orig 被调、参数是原始 target");
    {
        const bot = makeBot();
        installGuard(bot);
        const blocks = [
            { position: { x: 1, y: 64, z: 2 }, name: "stone" },
            { position: { x: 3, y: 64, z: 4 }, name: "dirt" },
        ];
        await collect(bot, blocks, { maxDistance: 8 });
        check("orig 被调", bot.__origCalled() === true, "Block 应透传");
        check("传的是原始 target 数组", bot.__origArgs() && bot.__origArgs().target === blocks, "应原样透传");
        check("options 透传", bot.__origArgs() && bot.__origArgs().options.maxDistance === 8, "options 丢失");
    }

    // D：Entity（{position,isValid}）→ 透传
    console.log("D：单个 Entity → 透传");
    {
        const bot = makeBot();
        installGuard(bot);
        const ent = { position: { x: 1, y: 64, z: 2 }, isValid: true };
        await collect(bot, ent);
        check("orig 被调", bot.__origCalled() === true, "Entity 应透传");
        check("传的是原始 Entity", bot.__origArgs() && bot.__origArgs().target === ent, "应原样透传");
    }

    // E：混合数组（Block + Vec3）→ 抛错，消息含 targets[1]
    console.log("E：混合数组（Block + Vec3）→ 抛错，消息含 targets[1]");
    {
        const bot = makeBot({ origThrows: new Error("should not reach") });
        installGuard(bot);
        const mixed = [
            { position: { x: 1, y: 64, z: 2 }, name: "stone" },  // 合法 Block
            { x: 5, y: 64, z: 6 },                                 // Vec3
        ];
        let err = null;
        try { await collect(bot, mixed); }
        catch (e) { err = e; }
        check("抛了错", err instanceof Error, err);
        check("_openkageClean=true", err && err._openkageClean === true, "标记缺失");
        check("消息含 targets[1]", err && err.message.includes("targets[1]"), err && err.message);
        check("orig 未被调", bot.__origCalled() === false, "应整批拦下");
    }

    // F：callback 模式（传 cb）→ cb 被调、不抛
    console.log("F：callback 模式 → cb 被调、不抛（callbackify 再回调不吞参）");
    {
        const bot = makeBot();
        installGuard(bot);
        const blocks = [{ position: { x: 1, y: 64, z: 2 }, name: "stone" }];
        let cbErr = "not called";
        await new Promise((resolve) => {
            bot.collectBlock.collect(blocks, {}, (e) => { cbErr = e || null; resolve(); });
        });
        check("cb 被调且无错", cbErr === null, `cbErr=${cbErr}`);
        check("orig 被调", bot.__origCalled() === true, "callback 模式也应透传");
        check("cb 原样透传到 orig", bot.__origArgs() && typeof bot.__origArgs().cb === "function", "cb 丢失");
    }

    // G：装两次 → 只包一层（幂等）
    console.log("G：装两次 → 只包一层（幂等）");
    {
        const bot = makeBot();
        installGuard(bot);
        installGuard(bot);   // 第二次应被标志短路
        // Vec3 输入：若被包两层，校验逻辑会跑两遍但只抛一次（throw 后不会到第二次），
        // 关键是 orig 不被调 + 标志阻止重包装。这里验证抛错行为与单次一致。
        let err = null;
        try { await collect(bot, [{ x: 1, y: 64, z: 2 }]); }
        catch (e) { err = e; }
        check("装两次仍正确拦截", err instanceof Error && err._openkageClean === true, "幂等失效");
        check("orig 未被调", bot.__origCalled() === false, "应拦在入口");
        check("标志已设（阻止重包装）", bot.__openkageCollectGuarded === true, "标志缺失");
    }

    console.log(`\n${pass} passed, ${fail} failed`);
    process.exit(fail === 0 ? 0 : 1);
}

main().catch((e) => {
    console.error("smoke 崩了:", e);
    process.exit(2);
});
