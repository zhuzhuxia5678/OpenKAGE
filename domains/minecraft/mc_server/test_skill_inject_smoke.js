/**
 * 技能注入埋点包装代码 —— 跨语言执行端契约测试。**不连 MC 服务器**。
 *
 *     node test_skill_inject_smoke.js
 *
 * 测什么：Python 侧 `_wrap_skill_for_injection`（domains/minecraft/__init__.py）把反思产出的
 * JS 技能代码包一层埋点代理后注入 programs。这段包装代码是 Python **字符串拼接**产出的，
 * 最终在 Node 里 eval 执行。本 smoke 验证拼接产物在 Node 里**真能跑、真能产出正确的结构化
 * 埋点事件**——这是 Python 拼代码 → Node 执行 → bot.openkageSkill.push → onSkill 事件 →
 * Python _collect_skill_events 这个跨语言闭环的执行端。
 *
 * ★ 之前这段逻辑埋在 react_day 闭包深处（2026-08-26 抽成模块级 _wrap_skill_for_injection），
 *   现有 22 个 Python 单测碰不到「Node 真能执行」这层。一旦拼接出错（函数名没替换、slug 含非标识符
 *   字符、try/catch 拼坏），整条计数降级回路会静默失效——包装代码在 Node 里 SyntaxError 或
 *   ReferenceError，push 永不产出，_collect_skill_events 永远收到空增量，技能永远不会被
 *   demote。这个 smoke 就是来钉死这层契约的。
 *
 * ★ 包装代码样本取自 Python _wrap_skill_for_injection 的真实输出（见文末 SAMPLE 注释），与
 *   Python e2e smoke（test_skill_pipeline_e2e.py 的 ④⑤ 段）对同一产物的字符串断言交叉印证。
 *
 * ★ 2026-08-26 改走结构化通道：代理不再 `console.log('[skill] ...')`，改为
 *   `bot.openkageSkill.push({slug, ok:1/0, err})`（onSkill 观察插件，见 lib/observation/onSkill.js）。
 *   旧法靠 JS 输出格式与 Python 解析逻辑的字符串契约维持同步、任一端漂移就静默丢数；
 *   结构化事件与 onSystem 同机制，丢数可检测。本 smoke 随之改断言「push 进了结构化对象」。
 *
 * 用例：
 *   ① 成功路径：raw 函数正常返回 → push {slug:'mine_iron',ok:1}，返回值透传
 *   ② 失败路径：raw 函数抛错 → push {slug:'mine_iron',ok:0,err:<msg>}，错误照常 throw
 *      （让 Agent 看到错误反馈，见 _wrap_skill_for_injection 设计意图：不吞错）
 *   ③ 原函数体未被改写：_skill_<slug>_raw 保留原始逻辑（只是改名，函数体原样）
 *   ④ 多次调用累计：每次成功/失败都独立 push 一条结构化事件（onSkill 回传后由 Python 累加）
 *   ⑤ bot.openkageSkill 缺失时不炸（守卫 `bot.openkageSkill &&`）：旧版 Node/未注册插件时退化
 */
const assert = require("assert");

// ── Python _wrap_skill_for_injection 的真实输出样本 ──────────────────────────
// 来源：python -c "from domains.minecraft import _wrap_skill_for_injection; ..."
// 原始技能：async function mineIron(bot, n){ await mineBlock(bot,'iron_ore',n); return n; }
// 包装后（Python 拼接产物，原样照搬，任何改动都会让本测试与真实注入脱钩）：
const WRAPPED_SKILL_CODE = `
// 技能 mine_iron：挖矿冶铁
async function _skill_mine_iron_raw(bot, n){ await mineBlock(bot,'iron_ore',n); return n; }
async function mineIron(...args){try{const r=await _skill_mine_iron_raw(...args);try{bot.openkageSkill&&bot.openkageSkill.push({slug:'mine_iron',ok:1});}catch(_m){}return r;}catch(e){try{bot.openkageSkill&&bot.openkageSkill.push({slug:'mine_iron',ok:0,err:((e&&e.message)||e)});}catch(_m){}throw e;}}
`;

/**
 * 在受控作用域里 eval 包装代码，注入假的 mineBlock + 假 bot（带 openkageSkill 缓冲）。
 * 返回 { mineIron, skillEvents, bot } —— mineIron 是 eval 出来的代理函数，
 * skillEvents 是捕获的结构化埋点事件（模拟 mineflayer onSkill 插件回传 Python 的产物）。
 *
 * @param {object} opts
 * @param {boolean} opts.withSkillSink — false 时不挂 bot.openkageSkill（测守卫分支，默认 true）
 */
function makeEnv({ mineBlockImpl = null, withSkillSink = true } = {}) {
    const skillEvents = [];
    const bot = {};
    if (withSkillSink) {
        // 模拟 onSkill 插件的 push（见 lib/observation/onSkill.js：真实插件里 push 攒进缓冲、
        // 步末 flush 成 onSkill 事件随 /step 响应回传）
        bot.openkageSkill = { push: (rec) => skillEvents.push(rec) };
    }
    // 假 mineBlock：默认成功（返回 undefined），可被 mineBlockImpl 覆盖成抛错
    const mineBlock = mineBlockImpl || (async (bot, name, n) => { bot._dug = { name, n }; });

    // 用 new Function 在独立作用域 eval，把 bot/mineBlock 作为参数注入
    // （与 index.js 的 eval 闭包同构：programs 里的原语就是这么对 LLM 代码可见的；
    //   代理里的 `bot.openkageSkill` 取的是这个注入的 bot，与真实运行时一致）
    const factory = new Function("bot", "mineBlock", WRAPPED_SKILL_CODE + "\nreturn { mineIron };");
    const { mineIron } = factory(bot, mineBlock);
    assert.strictEqual(typeof mineIron, "function", "包装后 mineIron 应是可调用函数");
    return { mineIron, skillEvents, bot };
}

async function main() {
    // ① 成功路径：raw 正常返回 → push {slug:'mine_iron',ok:1}，返回值透传
    {
        const { mineIron, skillEvents, bot } = makeEnv();
        const ret = await mineIron(bot, 3);
        assert.strictEqual(ret, 3, "① 成功时应透传 raw 的返回值");
        assert.deepStrictEqual(
            skillEvents, [{ slug: "mine_iron", ok: 1 }],
            `① 成功应 push 一条 ok 事件，实际：${JSON.stringify(skillEvents)}`
        );
        // raw 确实被调用（原函数体执行了 mineBlock）
        assert.deepStrictEqual(bot._dug, { name: "iron_ore", n: 3 }, "① raw 原函数体应被执行");
        console.log("✓ ① 成功路径 → push {slug:'mine_iron',ok:1} + 透传返回值");
    }

    // ② 失败路径：raw 抛错 → push {slug:'mine_iron',ok:0,err:<msg>}，错误照常 throw
    {
        const boom = new Error("furnace out of fuel");
        const { mineIron, skillEvents, bot } = makeEnv({
            mineBlockImpl: async () => { throw boom; },
        });
        await assert.rejects(
            () => mineIron(bot, 1),
            /furnace out of fuel/,
            "② 失败时应照常 throw 原错误（不吞错，让 Agent 看到反馈）"
        );
        assert.deepStrictEqual(
            skillEvents, [{ slug: "mine_iron", ok: 0, err: "furnace out of fuel" }],
            `② 失败应 push 一条 ok:0+err 事件，实际：${JSON.stringify(skillEvents)}`
        );
        console.log("✓ ② 失败路径 → push {slug:'mine_iron',ok:0,err:<msg>} + 错误透传");
    }

    // ③ 原函数体未被改写：_skill_<slug>_raw 保留原始逻辑（改名不改体）
    {
        // raw 里 return n，证明函数体原样保留（若被改写/截断，返回值会变）
        const { mineIron, bot } = makeEnv();
        const r0 = await mineIron(bot, 0);
        const r7 = await mineIron(bot, 7);
        assert.strictEqual(r0, 0, "③ raw 函数体原样保留（return n 透传 0）");
        assert.strictEqual(r7, 7, "③ raw 函数体原样保留（return n 透传 7）");
        console.log("✓ ③ 原函数体未改写：_skill_<slug>_raw 保留原始逻辑");
    }

    // ④ 多次调用累计：每次成功/失败都独立 push 一条结构化事件（onSkill 回传后由 Python 累加）
    {
        const { mineIron, skillEvents, bot } = makeEnv({
            mineBlockImpl: async (b, name, n) => {
                if (n < 0) throw new Error("bad n");
            },
        });
        await mineIron(bot, 1);              // ok
        await mineIron(bot, 2);              // ok
        await assert.rejects(() => mineIron(bot, -1));  // fail
        assert.deepStrictEqual(
            skillEvents,
            [
                { slug: "mine_iron", ok: 1 },
                { slug: "mine_iron", ok: 1 },
                { slug: "mine_iron", ok: 0, err: "bad n" },
            ],
            `④ 多次调用应各 push 独立事件，实际：${JSON.stringify(skillEvents)}`
        );
        console.log("✓ ④ 多次调用 → 各 push 独立结构化事件（供 Python 累加）");
    }

    // ⑤ 非 Error 对象被 throw 时，err 用原值（((e&&e.message)||e) 兜底）
    {
        const { mineIron, skillEvents, bot } = makeEnv({
            mineBlockImpl: async () => { throw "字符串错误"; },  // 非 Error 对象
        });
        await assert.rejects(() => mineIron(bot, 1));
        assert.deepStrictEqual(
            skillEvents, [{ slug: "mine_iron", ok: 0, err: "字符串错误" }],
            `⑤ 非 Error throw 时 err 应用原值，实际：${JSON.stringify(skillEvents)}`
        );
        console.log("✓ ⑤ 非 Error 对象 throw → err 用原值（兜底 ((e&&e.message)||e)）");
    }

    // ⑥ 守卫：bot.openkageSkill 缺失时不炸（withSkillSink=false → bot 上无 openkageSkill）
    //    代理内 `bot.openkageSkill && ...` 短路跳过 push，技能照常执行、错误照常 throw。
    {
        const { mineIron, skillEvents, bot } = makeEnv({ withSkillSink: false });
        const ret = await mineIron(bot, 5);
        assert.strictEqual(ret, 5, "⑥ 无 openkageSkill 时技能仍应正常执行并返回");
        assert.deepStrictEqual(skillEvents, [], "⑥ 无 openkageSkill 时不应 push 任何事件");
        console.log("✓ ⑥ bot.openkageSkill 缺失 → 守卫短路，技能照常执行不炸");
    }

    // ⑦ 埋点 push 自身抛错不影响技能执行（内层 try/catch 吞埋点错误）
    //    模拟 openkageSkill.push 抛错（如缓冲已满/插件异常）→ 技能返回值仍透传、不抛
    {
        const bot = {
            openkageSkill: { push: () => { throw new Error("sink broken"); } },
        };
        const mineBlock = async (b, name, n) => { b._dug = { name, n }; };
        const factory = new Function("bot", "mineBlock", WRAPPED_SKILL_CODE + "\nreturn { mineIron };");
        const { mineIron } = factory(bot, mineBlock);
        const ret = await mineIron(bot, 9);
        assert.strictEqual(ret, 9, "⑦ 埋点 push 抛错时技能返回值仍应透传");
        assert.deepStrictEqual(bot._dug, { name: "iron_ore", n: 9 }, "⑦ raw 仍被执行");
        console.log("✓ ⑦ 埋点 push 抛错 → 内层 try/catch 吞掉，技能执行不受影响");
    }

    console.log("\n全部技能注入埋点包装契约测试通过 ✓");
}

main().catch((e) => { console.error(e); process.exit(1); });
