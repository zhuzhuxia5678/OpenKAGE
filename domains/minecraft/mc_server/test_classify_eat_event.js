/**
 * _classifyEatEvent 滤噪判定单测 —— **不连 MC 服务器**。
 *
 *     node test_classify_eat_event.js
 *
 * 从 index.js 抽出 _classifyEatEvent 的真正源码在 vm 里跑（不是镜像副本，与
 * test_makeTickTimeoutPromise_smoke.js 同款），钉住滤噪规则：
 *   ① food↑ → "eat"（即便同时 health↑，原因归 eat）
 *   ② health↑（food 不变）→ "heal"
 *   ③ health↓（food 不变）→ "damage"
 *   ④ 仅饱和度衰减（h/f 不变 s 变）→ null；非满食 sat↑（food 不动）→ null
 *   ⑤ 全无变化 → null
 *   ⑥ food↓（饥饿消耗，非进食）+ health 不变 → null（food 只在上升时算 eat）
 *   ⑦ 同时 food↑ + health↓（边吃边挨打）→ "eat"（food↑ 优先级最高）
 *   ⑧ 满食(f=20)吃金苹果类：sat↑ food 不动 → "eat"（smoke 踩坑补的分支）
 */
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const SRC = fs.readFileSync(path.join(__dirname, "index.js"), "utf-8");
const startIdx = SRC.indexOf("function _classifyEatEvent(");
if (startIdx < 0) throw new Error("找不到 _classifyEatEvent");
// 找函数闭合大括号（与 test_makeTickTimeoutPromise_smoke 同款括号匹配）
let depth = 0, endIdx = -1, inStr = false, strCh = "";
for (let i = SRC.indexOf("{", startIdx); i < SRC.length; i++) {
    const c = SRC[i];
    if (inStr) {
        if (c === "\\") { i++; continue; }
        if (c === strCh) inStr = false;
        continue;
    }
    if (c === '"' || c === "'" || c === "`") { inStr = true; strCh = c; continue; }
    if (c === "{") depth++;
    else if (c === "}") { depth--; if (depth === 0) { endIdx = i; break; } }
}
if (endIdx < 0) throw new Error("找不到 _classifyEatEvent 闭合");
const FN_SRC = SRC.slice(startIdx, endIdx + 1);

const ctx = { console, Object, globalThis: {} };
vm.createContext(ctx);
vm.runInContext(FN_SRC + "\nglobalThis.__fn = _classifyEatEvent;", ctx);
const fn = ctx.globalThis.__fn;

let pass = 0, fail = 0;
const check = (name, got, want) => {
    if (got === want) { pass++; console.log(`  ✓ ${name}`); }
    else { fail++; console.log(`  ✗ ${name}  got=${got} want=${want}`); }
};

const B = (h, f, s) => ({ h, f, s });  // before/after 构造简写

console.log("① food↑ → eat（即便同时 health↑）");
check("food↑ + health↑ → eat", fn(B(10, 14, 5), B(12, 20, 7)), "eat");
check("food↑ + health 不变 → eat", fn(B(20, 10, 5), B(20, 15, 6)), "eat");

console.log("② health↑（food 不变）→ heal");
check("health↑ food 不变 → heal", fn(B(15, 18, 5), B(18, 18, 5)), "heal");
check("health↑ sat 也变 → heal", fn(B(15, 18, 3), B(18, 18, 5)), "heal");

console.log("③ health↓（food 不变）→ damage");
check("health↓ food 不变 → damage", fn(B(18, 18, 5), B(12, 18, 5)), "damage");
check("health↓ sat 也变 → damage", fn(B(20, 20, 5), B(10, 20, 2)), "damage");

console.log("④ 仅饱和度衰减（h/f 不变 s 变）→ null");
check("sat 衰减 → null", fn(B(20, 20, 5), B(20, 20, 3)), null);
check("非满食 sat 上升 f 不变 → null", fn(B(20, 18, 1), B(20, 18, 4)), null);

console.log("⑤ 全无变化 → null");
check("全不变 → null", fn(B(20, 20, 5), B(20, 20, 5)), null);

console.log("⑥ food↓（饥饿消耗，非进食）+ health 不变 → null");
check("food↓ health 不变 → null", fn(B(20, 18, 2), B(20, 15, 0)), null);

console.log("⑦ food↑ + health↓（边吃边挨打）→ eat（food↑ 优先级最高）");
check("food↑ + health↓ → eat", fn(B(15, 14, 5), B(12, 20, 7)), "eat");

console.log("⑧ 满食(f=20)吃金苹果类：sat↑ food 不动 → eat（smoke 踩坑补的分支）");
check("f=20 sat↑ → eat", fn(B(20, 20, 3), B(20, 20, 5)), "eat");
check("f=20 sat↑ + health↑ → eat", fn(B(18, 20, 3), B(20, 20, 5)), "eat");
check("f=19 sat↑（非满食，sat 波动不算进食）→ null", fn(B(20, 19, 1), B(20, 19, 3)), null);

console.log("⑨ 死亡瞬间服务器重置 food/sat 到满（h→0）→ null（由紧随的 death 行权威记录）");
check("h→0 + f=20 sat 5→20 → null", fn(B(20, 20, 5), B(0, 20, 20)), null);
check("h→0 + f 17→20 sat 涨 → null", fn(B(15, 17, 2), B(0, 20, 20)), null);

console.log(`\n${'='.repeat(40)}\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
