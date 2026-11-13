/** 图谱布局的数值稳定性回归测试。
 *
 *     node viewer/frontend/src/layout.test.mjs
 *
 * 前端没有测试框架（package.json 只有 vite），为一个纯函数模块引入 vitest 不值得 ——
 * 这些用例只需要 node 和 assert。
 *
 * ## 守的是什么
 *
 * 2026-08-13 的 bug：斥力 `f = repulsion/d²` 没有上限，d→0 时单步位移可达数十万；而初始摆位
 * 是圆周等分，节点一多开局就重叠 → 第一次迭代即弹飞。后果是 `boundR = max(hypot)` 被离群点
 * 撑爆，画布拉到 **49666×15343**（视口 1600），主体缩成一小团 —— 用户报的「图谱直径非常大」。
 *
 * 修完实测（真实数据、真实浏览器）：Alice 218 节点 2078×1785、重叠 0 对。
 *
 * 用例覆盖四道护栏，每条都构造**会触发原 bug 的输入**：
 *   ① 力/速度夹断      —— 两点几乎重合
 *   ② 初始摆位不重叠    —— 200 个节点
 *   ③ boundR 用分位数   —— 一个离群点
 *   ④ 收敛后拉回离群点  —— 中位数的十几倍远
 */
import assert from "node:assert";
import {
  MAX_SPEED, clampScale, pullInOutliers, boundRadius,
  runForceLayout, runSimpleRepulsion, spiralPlacement,
} from "./layout.js";

const tests = [];
const test = (name, fn) => tests.push([name, fn]);
const radiusOf = () => 20;

test("clampScale：超限按模长缩放，未超限返回 1", () => {
  assert.strictEqual(clampScale(3, 4, 10), 1);          // 模长 5 < 10
  assert.ok(Math.abs(clampScale(30, 40, 10) - 0.2) < 1e-9);  // 模长 50 → 10/50
});

test("① 密集重叠时不会被弹飞（力/速度夹断）", () => {
  // ⚠️ 判据要有区分度：只放 **2** 个几乎重合的点测不出来 —— 向心引力两步就把它们拉回来了
  //    （实测去掉夹断后仍稳定在 R≈91）。必须让**多个**点同时挤在一起，才能复现"互相弹射、
  //    一步几万"的真实情形。
  const N = 30;
  const ps = Array.from({ length: N }, (_, i) => ({
    id: `n${i}`, x: i * 0.001, y: 0, r: 20,   // 全挤在 0.03 单位的线段上
  }));
  const adj = Object.create(null);
  for (const p of ps) adj[p.id] = new Set();
  runForceLayout(ps, adj, { iterations: 120, repulsion: 14000, damping: 0.78, gravity: 0.025 });
  assert.ok(ps.every(p => Number.isFinite(p.x) && Number.isFinite(p.y)), "出现 NaN/Infinity");
  const maxR = Math.max(...ps.map(p => Math.hypot(p.x, p.y)));
  // 去掉夹断后实测能到数万；30 个 r=20 的点铺开只需几百的半径
  assert.ok(maxR < 3000, `被弹到 ${Math.round(maxR)}，说明夹断没生效`);
  // 也要真的推开了（不是卡在原地）
  assert.ok(maxR > 50, "节点没有被斥力铺开");
});

test("① 簇间斥力同样有夹断", () => {
  const ps = [
    { x: 0, y: 0, r: 900 },
    { x: 0.01, y: 0, r: 900 },   // 两个大簇几乎重合：重叠力 + 斥力都极大
  ];
  runSimpleRepulsion(ps, { iterations: 60, repulsion: 12000, gravity: 0.05, gcx: 0, gcy: 0 });
  for (const p of ps) {
    assert.ok(Number.isFinite(p.x), "坐标必须有限");
    assert.ok(Math.hypot(p.x, p.y) < MAX_SPEED * 60, "簇被弹飞了");
  }
});

test("② 初始摆位无重叠（200 节点）", () => {
  const comp = Array.from({ length: 200 }, (_, i) => ({ id: `n${i}` }));
  const ps = spiralPlacement(comp, radiusOf);
  let overlaps = 0;
  for (let i = 0; i < ps.length; i++) {
    for (let j = i + 1; j < ps.length; j++) {
      // 用真实节点半径（20）判重叠，不含摆位时预留的 +12 余量
      if (Math.hypot(ps[i].x - ps[j].x, ps[i].y - ps[j].y) < 40) overlaps++;
    }
  }
  assert.strictEqual(overlaps, 0, `初始就有 ${overlaps} 对重叠 —— 会立刻触发大斥力`);
});

test("② 单节点簇摆在原点、不产生 NaN", () => {
  const ps = spiralPlacement([{ id: "only" }], radiusOf);
  assert.deepStrictEqual([ps[0].x, ps[0].y], [0, 0]);
});

test("③ boundRadius 不被单个离群点撑爆", () => {
  const ps = [
    ...Array.from({ length: 40 }, (_, i) => ({ x: i % 7 * 30, y: (i / 7 | 0) * 30, r: 20 })),
    { x: 50000, y: 0, r: 20 },     // 离群点
  ];
  const r = boundRadius(ps);
  assert.ok(r < 5000, `boundR=${Math.round(r)}，被离群点绑架了`);
  // 但也不能小到反映不了簇的真实尺寸
  assert.ok(r > 100, `boundR=${Math.round(r)} 太小，簇间会重叠`);
});

test("④ pullInOutliers 把远点拉回、近点不动", () => {
  const ps = [
    ...Array.from({ length: 20 }, (_, i) => ({ x: Math.cos(i) * 100, y: Math.sin(i) * 100, r: 20 })),
    { x: 20000, y: 0, r: 20 },
  ];
  const near = { ...ps[0] };
  pullInOutliers(ps);
  const far = ps[ps.length - 1];
  assert.ok(Math.hypot(far.x, far.y) < 1000, `离群点还在 ${Math.round(Math.hypot(far.x, far.y))}`);
  assert.ok(Math.abs(ps[0].x - near.x) < 1e-9, "近处的点不该被动");
});

test("④ 拉回只改半径、不改方向", () => {
  const ps = [
    ...Array.from({ length: 10 }, () => ({ x: 10, y: 0, r: 20 })),
    { x: 3000, y: 4000, r: 20 },   // 方向 (0.6, 0.8)
  ];
  pullInOutliers(ps);
  const p = ps[ps.length - 1];
  assert.ok(Math.abs(p.x / p.y - 0.75) < 1e-6, "方向被改变了");
});

test("大图整体：无 NaN、跨度可控、无重叠（端到端）", () => {
  // 180 个节点、链式连边 —— 接近真实副脑图谱的形状
  const comp = Array.from({ length: 180 }, (_, i) => ({ id: `n${i}` }));
  const adj = Object.create(null);
  for (const n of comp) adj[n.id] = new Set();
  for (let i = 1; i < comp.length; i++) {
    adj[`n${i - 1}`].add(`n${i}`);
    adj[`n${i}`].add(`n${i - 1}`);
  }
  const ps = spiralPlacement(comp, radiusOf);
  runForceLayout(ps, adj, { iterations: 260, repulsion: 14000, linkDist: 140, damping: 0.78, gravity: 0.025 });
  pullInOutliers(ps);
  assert.ok(ps.every(p => Number.isFinite(p.x) && Number.isFinite(p.y)), "出现 NaN/Infinity");
  const xs = ps.map(p => p.x), ys = ps.map(p => p.y);
  const span = Math.max(Math.max(...xs) - Math.min(...xs), Math.max(...ys) - Math.min(...ys));
  // 改动前同规模的簇 boundR 能到 4557（跨度近万）；给 6000 的宽松上限，够抓回归
  assert.ok(span < 6000, `跨度 ${Math.round(span)} 过大，护栏可能失效`);
  let overlaps = 0;
  for (let i = 0; i < ps.length; i++) {
    for (let j = i + 1; j < ps.length; j++) {
      if (Math.hypot(ps[i].x - ps[j].x, ps[i].y - ps[j].y) < 40) overlaps++;
    }
  }
  assert.ok(overlaps <= 2, `${overlaps} 对节点画在一起`);
});

let failed = 0;
for (const [name, fn] of tests) {
  try {
    fn();
    console.log(`  ok    ${name}`);
  } catch (e) {
    failed++;
    console.log(`  FAIL  ${name}: ${e.message}`);
  }
}
console.log(`\n${tests.length - failed}/${tests.length} passed`);
process.exit(failed ? 1 : 0);
