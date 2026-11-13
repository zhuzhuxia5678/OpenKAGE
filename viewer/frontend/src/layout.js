/** 图谱布局的纯数学部分：力导向 + 数值稳定护栏。
 *
 * 从 App.vue 抽出来的**唯一动机是可测**：这些函数没有任何 Vue 依赖，抽出后能被 node 直接
 * 跑（`node layout.test.mjs`），不用为了测布局引入 vitest 整套前端测试栈。
 *
 * ★ 抽出前我是"另写一份模拟"来量化布局的 —— 那种做法必然与实现漂移（同一套公式两处维护）。
 *   现在测试与页面 import 的是同一份代码。
 *
 * 初始摆位 + 四道护栏的来历见各函数注释；一句话概括：斥力 `f = repulsion/d²` 在 d→0 时会
 * 炸到几十万，而圆周等分的初始摆位在节点多时开局即重叠，两者叠加把画布撑到 49666×15343
 * （视口 1600）。修完是 2078×1785。
 */

// 力/速度的上限：**数值稳定性护栏**（2026-08-13 加）。
//
// 斥力是 `f = repulsion / d²`，d 很小时会炸掉：d=1 → f≈13861，乘 damping 后**单步位移就上万**；
// d=0.1 → 单步 54 万。而初始摆位是圆周等分，节点一多就**开局必然重叠**（179 个节点摆在
// r=883 的圆上，相邻间距 2π×883/179 ≈ 31，而节点直径 56）—— 于是第一次迭代就有节点被弹飞。
//
// 实测（Alice 218 节点）：迭代过程中 maxR 峰值达 **10 万**，而中位数一直稳在 ~400；
// Carol 更极端，峰值 46 万。最终 `boundR = max(hypot)` 被这一两个离群点撑爆 → 画布宽到
// 49666（视口只有 1600，主体缩成一小团，就是「图谱直径非常大」的直接原因）。
//
// 上限取值：MAX_FORCE=260 约等于 d≈7.3 时的斥力（节点半径量级），比这更近就不再加速；
// MAX_SPEED=60 让单步位移不超过约两个节点半径，既压住爆炸又不影响正常收敛速度。
const MAX_FORCE = 260;
const MAX_SPEED = 60;

/** 把向量按模长夹到 lim 以内，返回缩放系数（模长本就小于 lim 时返回 1）。 */
function clampScale(x, y, lim) {
  const m = Math.hypot(x, y);
  return m > lim ? lim / m : 1;
}

/** 收敛后把「甩得太远」的粒子拉回来（护栏④）。
 *
 * 力/速度夹断把爆炸压住了，但 260 次迭代未必收敛干净：连通分量里度为 1 的叶子节点常被
 * 邻居推到很远（实测 Alice 最远 R=1962 而中位数 429）。判据用**中位数的倍数**而不是绝对值 ——
 * 图有大有小，绝对阈值对小图太松、对大图太紧。
 *
 * 只按半径方向缩放、不动角度：保住布局的相对方位，人看上去还是同一张图。
 */
function pullInOutliers(particles, factor = 3, minKeep = 200) {
  if (particles.length < 4) return;
  const ds = particles.map(p => Math.hypot(p.x, p.y));
  const med = [...ds].sort((a, b) => a - b)[ds.length >> 1] || 1;
  const cap = Math.max(med * factor, minKeep);
  particles.forEach((p, i) => {
    if (ds[i] > cap) {
      const k = cap / ds[i];
      p.x *= k; p.y *= k;
    }
  });
}

/** 簇的外接半径：用**分位数**而不是 max（护栏③）。
 *
 * `boundR` 决定簇在簇间布局里占多大地方，进而决定整块画布尺寸。取 max 时**一个**离群点
 * 就能把它撑爆（实测 Carol：maxBoundR=4557，而绝大多数节点都在几百以内），
 * 于是画布被拉到几万宽、主体缩成一小团。
 *
 * 取 95 分位再加一个节点直径的余量：既反映簇的真实大小，又不被极端值绑架。
 * 少数真的落在 boundR 之外的节点只会让相邻簇稍微靠近一点，远好过画布爆炸。
 */
function boundRadius(particles, q = 0.95) {
  if (!particles.length) return 80;
  const ds = particles.map(p => Math.hypot(p.x, p.y) + p.r).sort((a, b) => a - b);
  const idx = Math.min(ds.length - 1, Math.floor(ds.length * q));
  return Math.max(80, ds[idx] + 40);
}

// 簇内力导向：斥力 + 弹簧 + 向心引力（照抄 AdminKgView.vue 的 runForceLayout）
function runForceLayout(particles, adj, opts) {
  const { iterations = 260, repulsion = 14000, linkDist = 140, damping = 0.78, gravity = 0.025 } = opts;
  const n = particles.length;
  if (n <= 1) return;
  const idIdx = Object.create(null);
  particles.forEach((p, i) => (idIdx[p.id] = i));
  const vx = new Array(n).fill(0), vy = new Array(n).fill(0);
  const fx = new Array(n), fy = new Array(n);

  for (let it = 0; it < iterations; it++) {
    for (let i = 0; i < n; i++) { fx[i] = 0; fy[i] = 0; }
    for (let i = 0; i < n; i++) {
      for (let j = i + 1; j < n; j++) {
        const dx = particles[i].x - particles[j].x, dy = particles[i].y - particles[j].y;
        const d2 = dx * dx + dy * dy + 0.01;
        const f = repulsion / d2;
        const dist = Math.sqrt(d2);
        const ux = dx / dist, uy = dy / dist;
        fx[i] += ux * f; fy[i] += uy * f;
        fx[j] -= ux * f; fy[j] -= uy * f;
        const minDist = (particles[i].r || 20) + (particles[j].r || 20);
        if (dist < minDist) {
          const overlap = minDist - dist;
          fx[i] += ux * overlap * 0.6; fy[i] += uy * overlap * 0.6;
          fx[j] -= ux * overlap * 0.6; fy[j] -= uy * overlap * 0.6;
        }
      }
    }
    for (let i = 0; i < n; i++) {
      const id = particles[i].id;
      for (const nb of adj[id] || []) {
        const j = idIdx[nb];
        if (j === undefined || j <= i) continue;
        const dx = particles[j].x - particles[i].x, dy = particles[j].y - particles[i].y;
        const dist = Math.hypot(dx, dy) + 0.01;
        const f = (dist - linkDist) * 0.08;
        const ux = dx / dist, uy = dy / dist;
        fx[i] += ux * f; fy[i] += uy * f;
        fx[j] -= ux * f; fy[j] -= uy * f;
      }
    }
    for (let i = 0; i < n; i++) { fx[i] += -particles[i].x * gravity; fy[i] += -particles[i].y * gravity; }
    for (let i = 0; i < n; i++) {
      // ★ 双重夹断（见 MAX_FORCE 那段注释）：先夹合力、再夹速度。
      //   只夹速度不够 —— 合力本身能到几十万，夹速度会让节点每步都以最大速度直线飞出去。
      const kf = clampScale(fx[i], fy[i], MAX_FORCE);
      vx[i] = (vx[i] + fx[i] * kf) * damping; vy[i] = (vy[i] + fy[i] * kf) * damping;
      const kv = clampScale(vx[i], vy[i], MAX_SPEED);
      vx[i] *= kv; vy[i] *= kv;
      particles[i].x += vx[i]; particles[i].y += vy[i];
    }
  }
}

// 簇间：超级节点纯斥力 + 向心引力（照抄 AdminKgView.vue 的 runSimpleRepulsion）
function runSimpleRepulsion(particles, opts) {
  const { iterations = 150, repulsion = 8000, damping = 0.7, gravity = 0.05, gcx = 0, gcy = 0 } = opts;
  const n = particles.length;
  if (n <= 1) return;
  const vx = new Array(n).fill(0), vy = new Array(n).fill(0);
  const fx = new Array(n), fy = new Array(n);
  for (let it = 0; it < iterations; it++) {
    for (let i = 0; i < n; i++) { fx[i] = 0; fy[i] = 0; }
    for (let i = 0; i < n; i++) {
      for (let j = i + 1; j < n; j++) {
        const dx = particles[i].x - particles[j].x, dy = particles[i].y - particles[j].y;
        const dist = Math.hypot(dx, dy) + 0.01;
        const minDist = particles[i].r + particles[j].r + 10;
        const ux = dx / dist, uy = dy / dist;
        if (dist < minDist) {
          const overlap = minDist - dist;
          fx[i] += ux * overlap * 0.5; fy[i] += uy * overlap * 0.5;
          fx[j] -= ux * overlap * 0.5; fy[j] -= uy * overlap * 0.5;
        }
        const f = repulsion / (dist * dist);
        fx[i] += ux * f; fy[i] += uy * f;
        fx[j] -= ux * f; fy[j] -= uy * f;
      }
    }
    for (let i = 0; i < n; i++) { fx[i] += (gcx - particles[i].x) * gravity; fy[i] += (gcy - particles[i].y) * gravity; }
    for (let i = 0; i < n; i++) {
      // 与簇内同一套夹断：这里的 r 是整簇的外接半径（可达上千），重叠力 = overlap×0.5 也能很大
      const kf = clampScale(fx[i], fy[i], MAX_FORCE);
      vx[i] = (vx[i] + fx[i] * kf) * damping; vy[i] = (vy[i] + fy[i] * kf) * damping;
      const kv = clampScale(vx[i], vy[i], MAX_SPEED);
      vx[i] *= kv; vy[i] *= kv;
      particles[i].x += vx[i]; particles[i].y += vy[i];
    }
  }
}

/** 簇的**初始摆位**：向日葵螺旋，保证起始状态几乎无重叠（护栏②）。
 *
 * 原先是圆周等分 `a = i/m·2π` 放在 `baseR = 80 + √m×60` 上 —— 节点一多，相邻弧长 2πR/m
 * 就小于节点直径（实测 179 个节点：间距 31 < 直径 56），**开局即重叠** → 第一次迭代就触发
 * 巨大斥力把节点弹飞（配合 MAX_FORCE 那段注释看）。
 *
 * 螺旋用黄金角：点在圆盘上面积均匀、间距处处相近；半径按「总面积 ≥ 各节点占地之和」反算，
 * 填充率 0.55（圆盘随机铺放的经验值，留出走位余量）。
 */
function spiralPlacement(comp, radiusOf) {
  const m = comp.length;
  const rs = comp.map(radiusOf);
  const areaNeeded = rs.reduce((s, r) => s + Math.PI * (r + 14) * (r + 14), 0);
  const spiralR = Math.max(80, Math.sqrt(areaNeeded / (0.55 * Math.PI)));
  const GOLDEN = Math.PI * (3 - Math.sqrt(5));   // ≈2.39996 rad
  return comp.map((n, i) => {
    const rr = spiralR * Math.sqrt(m === 1 ? 0 : i / (m - 1));  // √ 让面积均匀，不向心堆积
    const a = i * GOLDEN;
    return { id: n.id, x: Math.cos(a) * rr, y: Math.sin(a) * rr, r: rs[i] + 12 };
  });
}

export { MAX_FORCE, MAX_SPEED, clampScale, pullInOutliers, boundRadius,
         runForceLayout, runSimpleRepulsion, spiralPlacement };
