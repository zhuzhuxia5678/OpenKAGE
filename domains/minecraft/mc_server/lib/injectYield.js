// 在 LLM 生成代码的每个循环体开头注入 `await bot.waitForTicks(1);` —— 防 while(true){asyncApi()}
// 不 await 的纯同步死循环锁死事件循环 → keepalive 饿死断线。详见 index.js 里的调用处注释。
//
// 单独成模块以便单测（index.js require 它即生效；测试直接 require 本模块，不启动 express）。
const acorn = require("acorn");
const MagicString = require("magic-string");

// 在 LLM 生成代码的每个循环体开头注入 `await bot.waitForTicks(1);`（2026-08-26，治 dave 断线）。
//
// 根因：LLM 会写 `while (true) { bot.tossStack(item); }` —— tossStack 是异步 API 却没 await，
//   while(true) 不让出事件循环 = 纯同步死循环。Node 单线程下事件循环被锁死 → mineflayer 处理
//   不了 MC 服务器的 keepalive 心跳包 → 服务器 30s 超时 "Timed out" 踢人 → bot.end() → 正在
//   执行的 /step 响应发不出 → Python 侧 RemoteDisconnected。（与 while(dig) 同类，见 memory
//   openkage_readtimeout_while_dig_loop。dig 那例靠 digDownTo 原语治理，但 tossStack/craftItem/
//   placeBlock ... 逐个加原语是打地鼠，故用此通用机制一刀切。）
//
// 为什么「外层套 await」/「keepalive 看门狗」都不行：纯同步死循环从不返回控制权，外层 await
//   等不到、setInterval 抢救回调排不上事件循环。**只能从循环内部打断** —— 每轮强制 await 让出
//   一次事件循环，keepalive 即可被处理（minecraft-protocol 的 keep_alive 包走 socket I/O，
//   事件循环转一轮就处理），顺带让异步原语的 Promise 有机会真正完成。
//
// 用 magic-string 行内插入（appendLeft 到 block 起始 { 内侧），**不增行数** —— handleError
//   （见 /step handler 内）靠 programs.split("\n").length 把 V8 栈 <anonymous>:行号 换算回 LLM
//   代码行号，改了行数会让报错定位全错位。acorn 只用来定位循环体节点的位置，不做整树重生成
//   （escodegen 重排会破坏行号）。
//
// 边界：只注入 body 是 BlockStatement 的循环；空体/非 block 体（while(cond); / for(...) f()）
//   跳过；解析失败原样返回（让原有语法错误路径接管）；programs（原语定义）不注入，只改 code。
function injectYieldIntoLoops(code) {
    let ast;
    try {
        ast = acorn.parse(code, {
            ecmaVersion: 2022,
            sourceType: "script",
            allowReturnOutsideFunction: true,
            // ★ 必须开 allowAwaitOutsideFunction（2026-09-07 修，bob step187 事故）：LLM 代码在
            //   eval 时被包进 `await (async () => { <code> })()`（见 index.js evaluateCode），
            //   所以顶层 `await bot.xxx(...)` 是常态、是预期写法。script 模式下顶层 await 非法，
            //   acorn.parse 会抛 `Unexpected token` → 落进下面 catch 原样返回 → **注入静默跳过**。
            //   结果：只要代码含顶层 await（几乎所有真实 step），整个防死循环注入 100% 失效，
            //   while 死循环一路畅通锁死事件循环 → ReadTimeout。加这个选项让顶层 await 能解析。
            allowAwaitOutsideFunction: true,
        });
    } catch (e) {
        return code;  // 语法错误：原样返回，让原报错路径接管（不引入新故障）
    }
    const ms = new MagicString(code);
    const yieldStmt = "await bot.waitForTicks(1);";
    const injected = new Set();  // 防 walk 递归时同一 block 被注入多次（嵌套循环场景）
    let changed = false;

    function walk(node) {
        if (!node || typeof node.type !== "string") return;
        const isLoop =
            node.type === "WhileStatement" || node.type === "DoWhileStatement" ||
            node.type === "ForStatement" || node.type === "ForInStatement" ||
            node.type === "ForOfStatement";
        if (isLoop && node.body && node.body.type === "BlockStatement") {
            const pos = node.body.start + 1;  // { 内侧
            if (!injected.has(pos)) {
                ms.appendLeft(pos, yieldStmt);
                injected.add(pos);
                changed = true;
            }
        }
        // 递归子节点（含循环体本身，处理嵌套）
        for (const key in node) {
            if (key === "type" || key === "start" || key === "end") continue;
            const child = node[key];
            if (Array.isArray(child)) {
                for (const c of child) walk(c);
            } else if (child && typeof child.type === "string") {
                walk(child);
            }
        }
    }
    walk(ast);
    return changed ? ms.toString() : code;
}

module.exports = { injectYieldIntoLoops };
