<script>
/**
 * 探索地图：一个副脑（arm）的 Minecraft 轨迹 + 记忆层地点，与右侧图谱双向联动。
 *
 * 数据来自 `GET /api/books/octopus_arm/{subject}/{book_slug}/scene`（后端 build_scene），
 * 解析逻辑全在 viewer/world_scene_render.py（后端 build_scene 调它）。**这里不重算任何
 * 坐标**：轨迹（lives）、覆盖范围（coverage，最小包围圆）、主聚居区（frame）都是后端给的。
 * 轨迹源是 reflect 反思上下文的逐步关键信号行（引擎 observe payload，~30 步/天全量采样，
 * 见 world_scene_render 模块 docstring）；死亡点来自 checkpoint 的服务器权威明细，不是从瞬移猜的。
 *
 * ★ 页面与后端**一份实现**（2026-09-30 定）：地图上画的三样东西 —— 地形底图（checkpoint
 *   世界渲染）、lives 轨迹、全量轨迹点的最小包围圆 —— 圆心半径由后端
 *   `world_scene_render.min_enclosing_circle` 算（此前前端另有一份 JS 版 Welzl，两份实现
 *   随时会静默漂移）。默认视野因此取**覆盖圆的外接框**：一屏同时装下全量轨迹与完整的圆。
 *   另两个预设：「聚居区」= 后端 frame（主聚居区，死后重生回世界原点会让副脑永久搬家、
 *   按全量点取景会把日常那片压成一个点，旧出图实现画错过一版）、「世界」= 整个世界地形
 *   （地形底图铺满）。
 *
 * 画法上沿用 SVG 版验证过的几条（都是画错后才定下来的，别改回去）：
 *   · 地点用**编号标在点上**，名字靠 hover 的 `<title>` 看 —— 据点型策略下十几个概念挤在
 *     同一坐标，直接标名字会叠成一坨，错开 y 也没用（它们本来就在一个位置）。
 *     （原先编号下方还有一份「编号→名字」图例，2026-08-10 按要求去掉了；要按名字找地点
 *     走右侧图谱的搜索框，选中后地图会自动平移过去并打脉冲环。）
 *   · 轨迹是**完整探索路径**（2026-09-30 定）：一条生命一条 polyline，跨天首尾相连
 *     （夜宿原地）；生命之间（死亡→重生瞬移）不连线 —— 那条几百格的直线不是它走过的路。
 *     数据来自后端 lives（world_scene_render._lives：跨天拼接 + 死亡唯一断点，checkpoint 权威）。
 *     画法为单色细实线（ACCENT），无发光底衬。
 *   · **不画记忆层关系边**（2026-09-30 用户定）：地图只承载轨迹/地点/死亡/覆盖圆，
 *     概念间关系走右侧图谱看。
 *   · 深度剖面横轴是**天**、不是累计步数 —— 各 arm 每天步数不同（实测 14~39）。
 * ★ **可拖拽 + 滚轮缩放，全部点都渲染**（2026-08-10 改）。原先是"只画取景框内的点、框外的
 *   数个数写一句『另有 N 个地点在取景框外』"，现在既然能拖能缩，那句提示就没必要了 ——
 *   点全在图上，拖出去看就行。这同时修掉了老做法的一个**潜在失真**：过滤是**逐点**做的，
 *   一段轨迹若"出框→框外走几步→回框"，过滤后剩下的点会被连成一条它根本没走过的直线
 *   （当前数据 33 段里恰好 0 例，所以一直没暴露；换个 run 就会画错）。
 *   实现上**不套 transform group**，而是把 pan/zoom 直接算进 sx()/sz()：这样圆点半径、
 *   线宽、字号天然不随缩放变形，不用到处写 `/zoom` 反向补偿。代价是平移/缩放时要重算
 *   折线字符串 —— 全量轨迹 ~1000 点/30 天，仍在毫秒级，可以忽略。
 *   超出地图矩形的部分靠 `clipPath` 裁掉，否则拖动时轨迹会画到坐标轴文字和剖面图上。
 */
import { computed, ref, watch } from "vue";

// 副脑配色：每个 arm 一套浅/深色（页面标注沿用）
const ARM_COLORS = {
  alice: ["#f7b2b2", "#a11d1d"],
  bob: ["#a9c9f5", "#12428f"],
  carol: ["#b5e0c0", "#186b34"],
};
const FALLBACK = ["#e0c3f7", "#5b1d8f"];
const SEA_LEVEL = 64;
const ACCENT = "#ff1f00"; // 统一高亮色：点、线、范围圈都用它，不再按角色区分

// 死亡 cause 是 MC 服务器的 translate key（death.attack.mob / death.fell.accident.generic…），
// 映射成看图时一眼懂的短语；没映射到的原样显示 key（宁可不翻译也不猜错）。
const DEATH_CAUSES = {
  "death.attack.mob": "被怪物击杀",
  "death.attack.player": "被玩家击杀",
  "death.attack.arrow": "被箭射死",
  "death.attack.explosion.player": "被爆炸炸死",
  "death.attack.explosion": "被爆炸炸死",
  "death.attack.drown": "溺水",
  "death.fell.accident.generic": "摔死",
  "death.attack.lava": "岩浆",
  "death.attack.inFire": "烧死",
  "death.attack.onFire": "烧死",
  "death.attack.cactus": "被仙人掌扎死",
  "death.attack.starve": "饿死",
  "death.attack.freeze": "冻死",
  "death.attack.sweetBerryBush": "被甜浆果丛刺死",
  "death.attack.anvil": "被铁砧砸死",
  "death.attack.fallingBlock": "被落方块砸死",
};
function deathLabel(d) {
  const cause = DEATH_CAUSES[d.cause] || d.cause;
  return d.killer && d.killer !== "unknown" ? `${cause}（${d.killer}）` : cause;
}

// Minecraft 生物群系调色板（参考游戏地图/Voyager 鸟瞰图的配色）。
// 采样点的 biome 来自 react_day 观察面板（world_scene_render 按**最近邻**贴到轨迹点上，
// 一天只有约 3 个样本）；有 checkpoint 世界时地形底图走真实渲染，这套色只是降级方案。
const BIOME_COLORS = {
  plains: "#91bd59", sunflower_plains: "#a0c95f", meadow: "#88bb67",
  forest: "#59a83d", flower_forest: "#6cb748", birch_forest: "#87c05b",
  old_growth_birch_forest: "#8dc763", dark_forest: "#3d8a2e",
  taiga: "#68947b", snowy_taiga: "#b8d0cd", snowy_plains: "#eef5f2",
  jungle: "#43a438", bamboo_jungle: "#68b24f", savanna: "#bfb755",
  desert: "#f2df8b", beach: "#f7ebab", stony_shore: "#a8a48b",
  river: "#4b7ddb", frozen_river: "#a3c8e8", ocean: "#3b6bc7",
  deep_ocean: "#335ba8", swamp: "#6c7539", mangrove_swamp: "#7a7433",
  mountains: "#8ab689", windswept_hills: "#86b18a", grove: "#c9d8cc",
  snowy_slopes: "#dcebe8", jagged_peaks: "#e9f3f1", frozen_peaks: "#e5f0ef",
  dripstone_caves: "#735d49", lush_caves: "#59904b", badlands: "#c96f34",
  wooded_badlands: "#b96b35", cherry_grove: "#e5b3c3",
};

// ★ 最小包围圆（Voyager Fig.7 的探索覆盖范围）**不在前端算**（2026-09-30 改）。
//   此前这里有一份 JS 版 Welzl，与后端各存一份实现 —— 只要有一处分支写歪（三点退化、
//   洗牌顺序），页面上画的圈就和数据对不上，且不报错。现在由后端 build_scene 调
//   world_scene_render.min_enclosing_circle 算好（点集 = lives 的全量点，与轨迹同源），
//   前端只照画 `scene.coverage`；无轨迹时后端给 null，这里不画。

function lerpColor(c1, c2, t) {
  const a = [1, 3, 5].map(i => parseInt(c1.slice(i, i + 2), 16));
  const b = [1, 3, 5].map(i => parseInt(c2.slice(i, i + 2), 16));
  return "#" + a.map((p, i) => Math.round(p + (b[i] - p) * t).toString(16).padStart(2, "0")).join("");
}

export default {
  name: "SceneMap",
  props: {
    scene: { type: Object, default: null },
    loading: { type: Boolean, default: false },
    // 右侧图谱当前选中的概念 slug（不带 bookKey 前缀）——地图上对应的点要打脉冲环
    highlightSlug: { type: String, default: null },
    // 录像回放位置联动（VideoPane 发来）：{day, x, y, z, step} 或 null。
    // 坐标来自 steps.jsonl 的 position（步末坐标），与本图轨迹源逐点一致（实测对齐）。
    replayPos: { type: Object, default: null },
  },
  emits: ["pick-place"],
  setup(props, { emit }) {
    const W = 460, H = 460;          // 俯瞰图视口（SVG 用户单位，靠 viewBox 自适应）
    const PROF_H = 130;              // 深度剖面高度
    const hoverPlace = ref(null);

    const colors = computed(() => ARM_COLORS[props.scene?.arm] || FALLBACK);
    const maxDay = computed(() =>
      Math.max(1, ...(props.scene?.days || []).map(d => d.day)));

    // ── 视图状态：缩放倍数 + 平移（像素）。世界坐标 → 屏幕像素分两步：
    //    base px/pz 是「基准视野」的取景（默认 = 完整覆盖范围，见 baseView；不含用户交互），
    //    sx/sz 再叠上 zoom/pan。分两层是为了「回到默认视角」能一键复位，且缩放锚点数学上好写
    //    （见 onWheel）；预设视野「聚居区/世界」也只是调 zoom+pan，不动 base（见 setView）。
    const zoom = ref(1);
    const panX = ref(0), panY = ref(0);

    // ★ 默认基准视野 = **探索覆盖范围**（后端 coverage 那个最小包围圆的外接框；2026-09-30
    //   从「主聚居区」改）：这样默认一屏就同时装下**全量轨迹**和**完整的覆盖圆**。
    //   此前默认框的是主聚居区，覆盖圆比它大得多时整圈跑到画面外（实测 carol：主聚居区
    //   跨度 142 格 vs 覆盖半径 289 格 → 圆几乎全在视野外），看着就像「范围」跟轨迹对不上。
    //   代价：副脑若死亡重生迁过址，日常活动那片会相应缩小。要看细节用「聚居区」按钮放大，
    //   要看整个世界 + 地形底图的构图用「世界」按钮。
    //   圆外接框比轨迹包围盒略大（圆由三点定时会凸出去），故取 2r 而不是点的 min/max。
    const baseView = computed(() => {
      const c = props.scene?.coverage;
      const f = props.scene?.frame;
      if (c && c.r > 0) return { cx: c.cx, cz: c.cz, span: Math.max(2 * c.r * 1.06, 8) };
      // 退化：只有一个点/没有轨迹（r=0 或 coverage=null）→ 退回后端主聚居区取景
      return { cx: f?.cx || 0, cz: f?.cz || 0, span: f?.span || 8 };
    });
    // 「聚居区」：后端 frame（主聚居区 + 邻近地点，含 1.12 留白）—— 日常活动区的细节视角
    const clusterView = computed(() => {
      const f = props.scene?.frame;
      return f ? { cx: f.cx, cz: f.cz, span: f.span } : null;
    });
    // 「世界」：整个 checkpoint 世界的地形底图范围（地形铺满）
    const worldView = computed(() => {
      const t = props.scene?.terrain;
      if (!t) return null;
      return {
        cx: (t.min_x + t.max_x) / 2, cz: (t.min_z + t.max_z) / 2,
        span: Math.max(t.max_x - t.min_x, t.max_z - t.min_z, 8),
      };
    });
    // 切到某个预设视野：把它的中心放到画面中心、并把可见跨度调到 span（zoom 是相对 base 的）
    function setView(v) {
      if (!v) return;
      const z = Math.min(ZOOM_MAX, Math.max(zoomMin, baseView.value.span / v.span));
      zoom.value = z;
      panX.value = W / 2 - px(v.cx) * z;
      panY.value = H / 2 - pz(v.cz) * z;
    }
    const scale = computed(() => Math.min(W, H) / baseView.value.span);
    function px(x) { return W / 2 + (x - baseView.value.cx) * scale.value; }
    function pz(z) { return H / 2 + (z - baseView.value.cz) * scale.value; }
    // 叠加用户视图变换后的最终屏幕坐标。**所有绘制都用这两个**（px/pz 只是中间量）。
    // 不用 SVG transform group 的理由见组件头注释（圆点/线宽/字号不该随缩放变形）。
    function sx(x) { return px(x) * zoom.value + panX.value; }
    function sz(z) { return pz(z) * zoom.value + panY.value; }
    // 反向：屏幕像素 → 世界坐标（缩放锚点、把某个地点居中、算网格范围都要用）
    function worldOfSx(v) {
      return (v - panX.value) / zoom.value / scale.value - W / 2 / scale.value + baseView.value.cx;
    }
    function worldOfSz(v) {
      return (v - panY.value) / zoom.value / scale.value - H / 2 / scale.value + baseView.value.cz;
    }

    function dayColor(day) {
      const t = 0.18 + 0.82 * ((day - 1) / Math.max(1, maxDay.value - 1));
      return lerpColor(colors.value[0], colors.value[1], t);
    }

    // ── 轨迹折线：**按生命分段**画（2026-09-30 定）。数据直接吃后端 `lives` 字段
    //    （world_scene_render._lives：跨天首尾拼接、只在死亡断点切开），
    //    一条 polyline = 一次生命，段与段之间的
    //    空隙 = 一次死亡（断点来自 checkpoint 权威死亡明细，不是几何猜测）。
    //    days[].segments 按天嵌套（天界+死亡双重切分）会把轨迹碎成 N天+N死 段，
    //    不用；lives = 唯一断点数+1（Carol 19 天 6 死 → 7 条完整生命线）。
    //    旧后端（无 lives 字段）降级用 days 摊平 —— 兼容期，可删。
    // ── 轨迹折线：**完整探索轨迹**（2026-09-30 定）——一条生命一条 polyline，
    //    跨天首尾相连（夜宿原地，衔接位移实测 ≤10 格）；生命之间（死亡→重生瞬移）
    //    **不连线**：那条几百格的直线不是它走过的路，画出来是误导。
    //    数据直接吃后端 `lives`（world_scene_render._lives：跨天拼接 + 死亡唯一断点）。
    //    死亡位置由骷髅标记承载。
    const polylines = computed(() => {
      const parts = props.scene?.lives
        || (props.scene?.days || []).map(d => d.segments).flat();
      const out = [];
      parts.forEach((life, ci) => {
        if (!life.length) return;
        if (life.length === 1) {          // 孤立单点（生命只剩一步）：画小圆点，不静默丢
          out.push({ key: `dot-l${ci}`, dot: true,
                     x: sx(life[0].x), y: sz(life[0].z), color: ACCENT });
          return;
        }
        out.push({
          key: `life-${ci}`,
          dot: false,
          points: life.map(p => `${sx(p.x).toFixed(1)},${sz(p.z).toFixed(1)}`).join(" "),
          color: ACCENT,
        });
      });
      return out;
    });

    // ★ 回放联动 · 当天轨迹重描：正在回放第 N 天时，把**那一天**的折线单独加粗提亮画一遍，
    //   叠在全程轨迹上面 —— 否则 30 天的线挤在一起，根本看不出「它此刻走到哪一段」。
    //   回放位置可能落在地图当前视野外（重生瞬移到几百格外）——不裁剪，照画，跟镜交给
    //   下面的 watch(replayPos) 处理。
    //   与 polylines 同口径：按死亡分段（段间断开 = 死亡），不再摊平成一条。
    const replayDayLine = computed(() => {
      const rp = props.replayPos;
      if (!rp) return null;
      const d = (props.scene?.days || []).find(x => x.day === rp.day);
      if (!d) return null;
      const lines = d.segments
        .filter(seg => seg.length >= 2)
        .map(seg => seg.map(p => `${sx(p.x).toFixed(1)},${sz(p.z).toFixed(1)}`).join(" "));
      return lines.length ? { lines } : null;
    });

    // 回放位置标记（世界坐标 → 屏幕）：蓝色实心点 + 呼吸外圈，与地形/轨迹的红色系区分开。
    // day 对不上（地图数据比录像旧/新）时 rp.day 找不到当天 —— 标记照画（坐标本身仍有效），
    // 只是当天线重描不出。
    const replayMark = computed(() => {
      const rp = props.replayPos;
      if (!rp || !Number.isFinite(rp.x) || !Number.isFinite(rp.z)) return null;
      return { x: sx(rp.x), y: sz(rp.z), yw: rp.y, day: rp.day, step: rp.step };
    });

    // 每天第一个点 → 空心圈 + D<n> 标签
    const dayStarts = computed(() => {
      const out = [];
      for (const d of props.scene?.days || []) {
        const first = d.segments.flat()[0];
        if (first) out.push({ day: d.day, x: sx(first.x), y: sz(first.z), color: ACCENT });
      }
      return out;
    });

    const deaths = computed(() =>
      (props.scene?.days || []).flatMap(d =>
        d.deaths.map((p, i) => ({ key: `dth-${d.day}-${i}`, x: sx(p.x), y: sz(p.z),
                                  day: d.day, wx: p.x, wy: p.y, wz: p.z,
                                  cause: p.cause, killer: p.killer }))));

    // ── 地点：编号标在点上，名字靠 hover 的 <title>（见组件头注释） ──
    // 同一坐标的多个概念按索引散开一圈，否则编号完全重合（据点上常有 5-6 个概念）。
    // ⚠️ 散开的偏移量是**像素常量**、不随 zoom 变 —— 它是为了"让编号不叠字"，属于画法而非
    //    地理事实。放大时若跟着放大，同一坐标的几个点会飞散到几十像素外，看着像不同地方。
    const placeMarks = computed(() => {
      const used = Object.create(null);
      const out = [];
      for (const p of props.scene?.places || []) {
        let x = sx(p.x), y = sz(p.z);
        // 分组键用**基准像素**（px/pz，不含 zoom/pan）：既能把"挨得很近但不同坐标"的点归到
        // 一组（叠字的根源是像素距离、不是坐标是否相等），又不会随缩放漂移 —— 用屏幕像素
        // 会导致放大后分组变了、编号的散开方向跟着跳。
        const key = `${Math.round(px(p.x) / 9)},${Math.round(pz(p.z) / 9)}`;
        const k = used[key] || 0;
        used[key] = k + 1;
        if (k) {
          const ang = k * 1.9;
          x += Math.cos(ang) * (7 + k * 1.6);
          y += Math.sin(ang) * (7 + k * 1.6);
        }
        out.push({ ...p, idx: out.length + 1, x, y, wx: p.x, wy: p.y, wz: p.z });
      }
      return out;
    });
    const placeByslug = computed(() =>
      Object.fromEntries(placeMarks.value.map(p => [p.slug, p])));

    // 记忆层关系边已按用户要求移除（2026-09-30）：地图上只画轨迹与地点，不再画
    // 概念间的关系连线。关系信息仍走右侧图谱查看；scene.edges 数据照常从后端来、
    // hidden_edges 计数不变，只是不再投影到地图上。

    // Voyager Fig.7 的核心可视化：轨迹的最小包围圆 = 探索覆盖范围。
    // ★ 圆心/半径**直接吃后端 scene.coverage** —— 那是后端 min_enclosing_circle 算的
    //   （点集 = lives 全量点），与轨迹必然同源（见文件头注释）。
    //   画在世界坐标上（cx/cz/r），再走同一套 sx/sz/scale*zoom，跟随拖拽缩放。
    const coverage = computed(() => {
      const c = props.scene?.coverage;
      return c && Number.isFinite(c.r) ? c : null;
    });

    // ★ 地形图底图：Voyager Fig.7 不是羊皮纸网格，而是 Minecraft 世界俯瞰渲染。
    // 我们没有存世界方块数据，只有轨迹采样时带回来的 biome 标签；用最近邻 + 双最近邻混合
    // 生成地形场，再加一点确定性纹理噪声，尽量还原「森林/河流/沙漠/雪地色块」的观感。
    // 未知 '?' 的轨迹点不参与种子，由最近的已知 biome 外推。
    const biomeTerrain = computed(() => {
      const pts = (props.scene?.days || []).flatMap(d => d.segments.flat())
        .filter(p => p.biome && p.biome !== "?");
      if (!pts.length) return [];
      const all = (props.scene?.days || []).flatMap(d => d.segments.flat());
      const xs = all.map(p => p.x), zs = all.map(p => p.z);
      const min = { x: Math.min(...xs), z: Math.min(...zs) };
      const max = { x: Math.max(...xs), z: Math.max(...zs) };
      const span = Math.max(max.x - min.x, max.z - min.z, 8);
      const size = Math.max(1.5, span / 90);
      const out = [];
      for (let x = min.x - size; x <= max.x + size; x += size) {
        for (let z = min.z - size; z <= max.z + size; z += size) {
          let a = null, b = null;
          for (const p of pts) {
            const d = (p.x - x) ** 2 + (p.z - z) ** 2;
            if (!a || d < a.d) { b = a; a = { d, biome: p.biome }; }
            else if (!b || d < b.d) b = { d, biome: p.biome };
          }
          const k = b ? a.d / (a.d + b.d) : 0; // 0=贴着 a，1=正中（更倾向 b）
          const ca = BIOME_COLORS[a.biome] || "#8ab689";
          const cb = b ? (BIOME_COLORS[b.biome] || "#8ab689") : ca;
          // 混合只给 35% 权重，保留地形边界；块状纹理保留 Minecraft 地图的质感。
          const mix = Math.min(0.35, k);
          const rgb = [0, 1, 2].map(i =>
            Math.round(parseInt(ca.slice(1 + i * 2, 3 + i * 2), 16) * (1 - mix)
                      + parseInt(cb.slice(1 + i * 2, 3 + i * 2), 16) * mix));
          const h = Math.sin(x * 12.9898 + z * 78.233) * 43758.5453;
          const shade = 0.88 + ((h - Math.floor(h)) * 0.24);
          const [r, g, bch] = rgb.map(v => Math.max(0, Math.min(255, Math.round(v * shade))));
          out.push({ x, z, size, color: `rgb(${r},${g},${bch})` });
        }
      }
      return out;
    });

    // ── 网格：步长取一个"整齐"的量级，让跨度可读。
    //    缩放后步长要跟着换档，否则放大到 8× 时网格线密成一片、缩小时又只剩一条。 ──
    const grid = computed(() => {
      // 当前视野实际覆盖多少格 = **基准视野跨度 / zoom**（基准视野已不再是 frame，见 baseView）
      const visSpan = baseView.value.span / zoom.value;
      let step = 10 ** Math.max(0, Math.round(Math.log10(Math.max(visSpan / 4, 1))));
      if (visSpan / step > 8) step *= 2;
      const xs = [], zs = [];
      // 按**当前视口**反推要画哪些网格线（原先按 frame 中心±half 算，缩放后会算到视口外去）
      const x0 = worldOfSx(0), x1 = worldOfSx(W);
      const z0 = worldOfSz(0), z1 = worldOfSz(H);
      for (let g = Math.floor(x0 / step) * step; g <= x1; g += step) {
        const x = sx(g);
        if (x >= 1 && x <= W - 1) xs.push({ g, x });
      }
      for (let g = Math.floor(z0 / step) * step; g <= z1; g += step) {
        const y = sz(g);
        if (y >= 1 && y <= H - 1) zs.push({ g, y });
      }
      return { step, xs, zs, bar: step * scale.value * zoom.value };
    });

    // ── 拖拽 / 滚轮缩放 ──────────────────────────────────────────────
    // 坐标换算的关键：鼠标事件给的是 **CSS 像素**，而 sx/sz 输出的是 **SVG 用户单位**。
    // SVG 靠 viewBox 自适应宽度（见 .scene-svg { width:100% }），两者比例 = viewBox宽/元素宽，
    // 不换算的话窄面板下拖动会"跟不上手"（实测 1/3 宽时差 ~1.1 倍）。
    const svgEl = ref(null);
    function toUser(e) {
      const r = svgEl.value.getBoundingClientRect();
      const k = (W + 52) / r.width;               // viewBox 宽 / 实际宽
      return { x: (e.clientX - r.left) * k - 34, y: (e.clientY - r.top) * k - 8 };  // -34/-8 = viewBox 原点偏移
    }

    let dragging = false, last = null, moved = 0;
    function onDown(e) {
      if (e.button !== 0) return;
      dragging = true; moved = 0; last = toUser(e);
    }
    function onMove(e) {
      if (!dragging) return;
      const p = toUser(e);
      panX.value += p.x - last.x;
      panY.value += p.y - last.y;
      moved += Math.abs(p.x - last.x) + Math.abs(p.y - last.y);
      last = p;
    }
    function onUp() { dragging = false; }
    // 拖动过就别把 mouseup 当成"点了这个地点"（否则每次拖完都会误选中一个点并弹出详情）
    const DRAG_SLOP = 4;
    function onPlaceClick(p) { if (moved <= DRAG_SLOP) emit("pick-place", p.slug); }

    // 放大上限 12×（够看清同坐标散开的那一圈编号）；缩小不设数据推导的下限，可以任意缩小。
    const ZOOM_MAX = 12;
    // 1× = 完整覆盖范围；「聚居区」/「世界」两个预设分别会 <1×（框更小→放大）和 >1×（框更大
    // →缩小），故下限放开到 0 —— 旧版「有地形就 ≥1×」的理由（1×=整个世界）已随基准视野改掉。
    const zoomMin = 0;

    function onWheel(e) {
      const p = toUser(e);
      // 缩放量跟 deltaY **大小**成比例，不是每个事件固定一档：迁址簇要缩到 0.08× 才看得见，
      // 固定 0.88 一档得滚 20 次才到底（实测一次大幅滚动只算一档、以为"缩不动了"）。
      // 归一化到 100（鼠标滚轮一格的典型 deltaY），触控板的小增量就自然变成细调。
      const k = Math.min(4, Math.abs(e.deltaY) / 100);
      const factor = e.deltaY > 0 ? 0.88 ** k : (1 / 0.88) ** k;
      const next = Math.min(ZOOM_MAX, Math.max(zoomMin, zoom.value * factor));
      if (next === zoom.value) return;
      // 锚定光标：让光标下的那个世界坐标缩放前后落在同一屏幕位置
      panX.value = p.x - (p.x - panX.value) * (next / zoom.value);
      panY.value = p.y - (p.y - panY.value) * (next / zoom.value);
      zoom.value = next;
    }
    // 「复位」= 回到 1× 的完整覆盖范围（全量轨迹 + 完整覆盖圆）
    function resetView() { zoom.value = 1; panX.value = 0; panY.value = 0; }
    // 换 arm 时必须复位：上一个 arm 拖到的位置对新 arm 毫无意义（各 arm 取景中心差数百格）
    watch(() => props.scene?.arm, resetView);

    function zoomBy(f) {
      const next = Math.min(ZOOM_MAX, Math.max(zoomMin, zoom.value * f));
      if (next === zoom.value) return;
      // 以地图中心为锚点（按钮缩放没有光标位置）
      panX.value = W / 2 - (W / 2 - panX.value) * (next / zoom.value);
      panY.value = H / 2 - (H / 2 - panY.value) * (next / zoom.value);
      zoom.value = next;
    }

    // ── 深度剖面：横轴按**天**等宽（不是累计步数，见组件头注释）。
    //    ★ 剖面**不受地图的 pan/zoom 影响** —— 它的横轴是"天"、纵轴是绝对高度 y，
    //      跟 x-z 平面的取景毫无关系；跟着地图缩放只会让人误以为两图同一坐标系。 ──
    const profile = computed(() => {
      const days = props.scene?.days || [];
      const all = days.flatMap(d => d.segments.flat());
      if (!all.length) return null;
      const ys = all.map(p => p.y);
      const ymin = Math.min(...ys), ymax = Math.max(...ys);
      const yspan = Math.max(ymax - ymin, 1);
      const dayW = W / maxDay.value;
      const qy = v => PROF_H - ((v - ymin) / yspan) * (PROF_H - 16) - 8;
      const lines = days.map(d => {
        const pts = d.segments.flat();
        const n = pts.length;
        return {
          day: d.day, color: dayColor(d.day),
          points: pts.map((p, k) =>
            `${((d.day - 1 + (n > 1 ? k / (n - 1) : 0.5)) * dayW).toFixed(1)},${qy(p.y).toFixed(1)}`
          ).join(" "),
        };
      });
      return {
        lines, ymin, ymax, dayW,
        sea: ymin <= SEA_LEVEL && SEA_LEVEL <= ymax ? qy(SEA_LEVEL) : null,
        diamond: ymin < 16 ? { y: qy(16), h: Math.max(0, qy(ymin) - qy(16)) } : null,
        ticks: [ymin, (ymin + ymax) / 2, ymax].map(v => ({ v, y: qy(v) })),
      };
    });

    // 联动：右侧图谱选中的概念若在地图上 → 打脉冲环；不在 → 上方给一句说明，
    // 不能什么都不发生（那样用户以为联动坏了）。
    const highlighted = computed(() =>
      props.highlightSlug ? placeByslug.value[props.highlightSlug] || null : null);
    const highlightMiss = computed(() => {
      if (!props.highlightSlug || highlighted.value) return null;
      const s = props.scene;
      if (!s) return null;
      // 分清几种"不在图上"，别拿一句话糊过去 —— 用户点了个节点却没反应时，他要知道的是
      // 「为什么」，而这几种原因的处置完全不同（数据缺坐标 / 本来就不是地方 / 层设计如此）。
      // 注：原先还有一种「在取景框外」—— 现在全部地点都渲染 + 可拖可缩，那种情况不存在了，
      // 且选中框外的点会自动平移过去（见下面的 watch）。
      if (s.no_coord.some(c => c.slug === props.highlightSlug)) return "该记忆概念的正文里没有可解析的坐标";
      if (s.non_places?.some(c => c.slug === props.highlightSlug))
        return "该概念是当天总结（不是一个地方），未标在地图上";
      // 走到这儿说明这个 slug 根本不在记忆层里 —— 私有图谱同时装着经验层（去情境化的通名
      // 结论，如"夜间地表危险"），它们本就没有地理位置，这是分层设计的应有结果、不是缺数据。
      return "该概念在经验层，没有地理位置";
    });

    // 选中的地点若落在可视矩形外，自动平移把它带到中间 —— 否则"打了脉冲环"却在屏幕外，
    // 表现和联动失效一样。只调 pan、不动 zoom（不擅自改变用户设定的缩放级别）。
    watch(highlighted, (p) => {
      if (!p) return;
      const M = 24;   // 边距：贴着边框也算"看不见"，留一点余量
      if (p.x >= M && p.x <= W - M && p.y >= M && p.y <= H - M) return;
      panX.value += W / 2 - p.x;
      panY.value += H / 2 - p.y;
    });

    // ★ 回放跟镜：标记走出视野时平移跟过去（同上，不动 zoom）。**阈值放宽到 1/4 画布**——
    //   跟图谱高亮那种「一次性跳转」不同，回放是连续移动的，标记贴边就追会导致画面一路
    //   抖个不停；让它走到快出界才平移一次，跳变少而稳。用户手动拖动与跟镜不冲突：
    //   拖完若标记仍在新视野内，下一次 timeupdate 前不会再动。
    watch(replayMark, (p) => {
      if (!p) return;
      const MX = W / 4, MY = H / 4;
      if (p.x >= MX && p.x <= W - MX && p.y >= MY && p.y <= H - MY) return;
      panX.value += W / 2 - p.x;
      panY.value += H / 2 - p.y;
    });

    return {
      W, H, PROF_H, ACCENT, colors, maxDay, baseView, scale, sx, sz, dayColor, deathLabel,
      polylines, dayStarts, deaths, placeMarks, grid, profile,
      hoverPlace,
      coverage, biomeTerrain,
      highlighted, highlightMiss,
      replayDayLine, replayMark,
      svgEl, zoom, onDown, onMove, onUp, onWheel, resetView, zoomBy, setView,
      clusterView, worldView,
      isDefaultView: computed(() => zoom.value === 1 && panX.value === 0 && panY.value === 0),
      onPick: onPlaceClick,
    };
  },
};
</script>

<template>
  <div class="scene-map">
    <div v-if="loading" class="scene-state"><div class="kg-spinner" /><span>加载地图中...</span></div>
    <div v-else-if="!scene" class="scene-state">没有地图数据</div>
    <div v-else-if="!scene.days.length" class="scene-state">
      <div class="ss-title">这个 run 没有可解析的轨迹</div>
    </div>

    <template v-else>
      <div class="scene-head">
        <span class="scene-title">{{ scene.arm_display }} 的探索地图</span>
        <span class="scene-stat">
          <!-- planner-only（后端 kind 字段）：点是稀疏重建的采样点（面板/onLog/place 三源），
               不是逐步轨迹 ——「步」字样会让人误读成它只走了这么多步 -->
          {{ scene.stats.days }} 天 · {{ scene.stats.steps }}
          {{ scene.kind === 'planner_only' ? '采样点' : '步' }} ·
          死亡 {{ scene.stats.deaths }} · 图上地点 {{ placeMarks.length }}
        </span>
        <span class="scene-span">
          {{ Math.round(baseView.span / zoom) }} 格
          <button v-if="clusterView" class="scene-reset" @click="setView(clusterView)"
                  title="放大到主聚居区：日常活动的那一片（死亡重生迁址后它只占覆盖范围的一角）">聚居区</button>
          <button v-if="worldView" class="scene-reset" @click="setView(worldView)"
                  title="缩到整个 checkpoint 世界（地形底图铺满）—— 整个世界范围的构图">世界</button>
          <button v-if="!isDefaultView" class="scene-reset"
                  title="回到默认视角：完整覆盖范围（全量轨迹 + 完整覆盖圆）"
                  @click="resetView">复位</button>
        </span>
      </div>

      <div v-if="highlightMiss" class="scene-note miss">{{ highlightMiss }}</div>

      <svg ref="svgEl" class="scene-svg"
           :viewBox="`-34 -8 ${W + 52} ${H + 30}`"
           @mousedown.prevent="onDown" @mousemove="onMove" @mouseup="onUp" @mouseleave="onUp"
           @wheel.prevent="onWheel">
        <!-- 裁剪：拖动/放大后超出地图矩形的部分必须裁掉，否则会画到坐标轴文字和剖面图上 -->
        <defs>
          <clipPath :id="'mapclip-' + scene.arm">
            <rect x="0" y="0" :width="W" :height="H" />
          </clipPath>
          <radialGradient :id="'paper-' + scene.arm" cx="50%" cy="45%" r="75%">
            <stop offset="0%" stop-color="#fdfaf1" />
            <stop offset="62%" stop-color="#f6efdd" />
            <stop offset="100%" stop-color="#eadfc6" />
          </radialGradient>
          <radialGradient :id="'vignette-' + scene.arm" cx="50%" cy="50%" r="72%">
            <stop offset="72%" stop-color="rgba(91,63,32,0)" />
            <stop offset="100%" stop-color="rgba(91,63,32,.18)" />
          </radialGradient>
        </defs>
        <rect x="0" y="0" :width="W" :height="H" fill="#f4efdf"
              stroke="#c8b795" stroke-width="1.2" />
        <g :clip-path="`url(#mapclip-${scene.arm})`">
        <!-- 真实地形底图：checkpoint 里的 Minecraft 世界方块渲染（Voyager Fig.7 同思路） -->
        <g v-if="scene.terrain" style="pointer-events:none">
          <image :key="scene.terrain.fingerprint" :href="scene.terrain.url"
                 :xlink:href="scene.terrain.url"
                 :x="sx(scene.terrain.min_x)" :y="sz(scene.terrain.min_z)"
                 :width="scene.terrain.width * scale * zoom"
                 :height="scene.terrain.height * scale * zoom"
                 preserveAspectRatio="none" image-rendering="pixelated" />
        </g>
        <!-- 没有 checkpoint 世界时的降级：按轨迹 biome 标签近似地形 -->
        <g v-else style="pointer-events:none">
          <rect v-for="(t, i) in biomeTerrain" :key="'ter' + i" :x="sx(t.x)" :y="sz(t.z)"
                :width="Math.ceil(t.size * scale * zoom) + .6"
                :height="Math.ceil(t.size * scale * zoom) + .6" :fill="t.color" />
        </g>

        <!-- 网格 -->
        <g>
          <line v-for="g in grid.xs" :key="'gx' + g.g" :x1="g.x" y1="0" :x2="g.x" :y2="H"
                stroke="rgba(255,255,255,.26)" />
          <line v-for="g in grid.zs" :key="'gz' + g.g" x1="0" :y1="g.y" :x2="W" :y2="g.y"
                stroke="rgba(255,255,255,.26)" />
        </g>

        <!-- Voyager Fig.7 式探索覆盖范围：轨迹的最小包围圆 -->
        <circle v-if="coverage && coverage.r > 0" :cx="sx(coverage.cx)" :cy="sz(coverage.cz)"
                :r="coverage.r * scale * zoom" fill="none"
                :stroke="ACCENT" stroke-width="3.4" opacity=".98" />

        <!-- 轨迹：一条完整折线（全部天、全部点按序相连），单色细实线无底衬 -->
        <g>
          <template v-for="p in polylines" :key="p.key">
            <circle v-if="p.dot" :cx="p.x" :cy="p.y" r="2" :fill="p.color" opacity=".95" />
            <polyline v-else :points="p.points" fill="none" :stroke="p.color" stroke-width="2"
                      stroke-linejoin="round" stroke-linecap="round" opacity=".9" />
          </template>
          <!-- 回放联动：正在回放那一天的轨迹单独加粗提亮一遍（画在全程轨迹之上） -->
          <polyline v-for="(ln, i) in (replayDayLine?.lines || [])" :key="'replay-ln-' + i"
                    :points="ln" fill="none"
                    stroke="#2563eb" stroke-width="2" stroke-linejoin="round"
                    stroke-linecap="round" opacity=".55" />
        </g>

        <!-- 每天起点 -->
        <g v-for="d in dayStarts" :key="'ds' + d.day">
          <circle :cx="d.x" :cy="d.y" r="2.8" :fill="ACCENT" stroke="#fff" stroke-width="1.2" />
        </g>

        <!-- 死亡点：小骷髅（圆颅+两眼+下颌），比叉号在密集图里更容易一眼认出。
             cause/killer 是服务器权威明细（checkpoint），title 给出死因而非只有坐标。 -->
        <g v-for="d in deaths" :key="d.key" opacity="0.85">
          <title>第{{ d.day }}天{{ d.cause ? '·' + deathLabel(d) : '死亡' }} ({{ d.wx }}, {{ d.wy }}, {{ d.wz }})</title>
          <circle :cx="d.x" :cy="d.y" r="4.2" fill="#3a3a3a" />
          <circle :cx="d.x - 1.5" :cy="d.y - 0.6" r="1.15" fill="#fff" />
          <circle :cx="d.x + 1.5" :cy="d.y - 0.6" r="1.15" fill="#fff" />
          <rect :x="d.x - 1.7" :y="d.y + 2.6" width="3.4" height="2" rx="0.7" fill="#3a3a3a" />
        </g>

        <!-- 地点：编号在点上，名字靠 hover 的 <title> -->
        <g v-for="p in placeMarks" :key="p.slug" class="place-mark" @click="onPick(p)"
           @mouseenter="hoverPlace = p.slug" @mouseleave="hoverPlace = null">
          <title>{{ p.name }} ({{ p.wx }}, {{ p.wy }}, {{ p.wz }})</title>
          <circle v-if="highlighted && highlighted.slug === p.slug" :cx="p.x" :cy="p.y" r="8"
                  fill="none" :stroke="ACCENT" stroke-width="2">
            <!-- 用 SMIL 而不是 CSS 动画：CSS 里 `r` 是 SVG 几何属性，只有较新的浏览器
                 才认它可动画（Safari 至今不认），动不了就是个静止的圈、看不出高亮。 -->
            <animate attributeName="r" values="8;18" dur="1.4s" repeatCount="indefinite" />
            <animate attributeName="opacity" values="0.95;0" dur="1.4s" repeatCount="indefinite" />
          </circle>
          <circle :cx="p.x" :cy="p.y" :r="hoverPlace === p.slug ? 8 : 6.6" :fill="ACCENT"
                  stroke="var(--bg-panel)" stroke-width="1.2" opacity="0.94" />
          <text :x="p.x" :y="p.y + 3.3" font-size="8.6" fill="#fff" text-anchor="middle"
                font-weight="bold" style="pointer-events:none">{{ p.idx }}</text>
        </g>

        <!-- 回放位置标记：蓝点 + 呼吸圈（SMIL，理由同上 Safari 那条），画在地点之上。
             title 带天/步/y，跟死亡点同款 hover 可读。 -->
        <g v-if="replayMark" style="pointer-events:none">
          <title>回放位置 · 第{{ replayMark.day }}天 第{{ replayMark.step }}步 · y={{ Math.round(replayMark.yw) }}</title>
          <circle :cx="replayMark.x" :cy="replayMark.y" r="9" fill="none"
                  stroke="#2563eb" stroke-width="2">
            <animate attributeName="r" values="6;16" dur="1.2s" repeatCount="indefinite" />
            <animate attributeName="opacity" values="0.9;0" dur="1.2s" repeatCount="indefinite" />
          </circle>
          <circle :cx="replayMark.x" :cy="replayMark.y" r="5.2" fill="#2563eb"
                  stroke="#fff" stroke-width="1.6" />
        </g>

        <!-- 地图装饰：指北针 + 轻微暗角（论文图风格） -->
        <g transform="translate(24, 24)" style="pointer-events:none">
          <circle r="13" fill="rgba(255,252,244,.82)" stroke="#b9a578" />
          <path d="M0,-8 L3.5,3 L0,1 L-3.5,3 Z" fill="#8b5e34" />
          <text y="7" font-size="5.5" fill="#6b552f" text-anchor="middle" font-weight="700">N</text>
        </g>
        <rect x="0" y="0" :width="W" :height="H" :fill="`url(#vignette-${scene.arm})`"
              style="pointer-events:none" />

        </g>
        <!-- 坐标轴刻度/箭头：画在 clip 之外（它们在地图矩形外侧，被裁掉就没了） -->
        <g>
          <text v-for="g in grid.xs" :key="'tx' + g.g" :x="g.x" :y="H + 12" font-size="9"
                fill="#7c6b4a" text-anchor="middle">{{ g.g }}</text>
          <text v-for="g in grid.zs" :key="'tz' + g.g" x="-4" :y="g.y + 3" font-size="9"
                fill="#7c6b4a" text-anchor="end">{{ g.g }}</text>
          <text x="2" :y="H + 12" font-size="9" fill="#7c6b4a">x→</text>
          <text x="-4" y="-1" font-size="9" fill="#7c6b4a" text-anchor="end"
                dominant-baseline="hanging">z↓</text>
        </g>

        <!-- 比例尺 -->
        <line x1="12" :y1="H - 12" :x2="12 + grid.bar" :y2="H - 12" stroke="#7c6b4a" stroke-width="2.2" />
        <text :x="16 + grid.bar" :y="H - 8.5" font-size="9" fill="#7c6b4a">{{ grid.step }} 格</text>
      </svg>

      <!-- 深度剖面：横轴按天等宽、三 arm 天界对齐（这里只画当前 arm） -->
      <svg v-if="profile" class="scene-prof" :viewBox="`-34 -14 ${W + 52} ${PROF_H + 30}`">
        <text x="-30" y="-4" font-size="9.5" fill="var(--text-secondary)">深度剖面（y 随天变化）</text>
        <rect x="0" y="0" :width="W" :height="PROF_H" fill="var(--bg-panel)" stroke="var(--border-hover)" />
        <rect v-if="profile.diamond" x="0" :y="profile.diamond.y" :width="W" :height="profile.diamond.h"
              fill="#3b82f6" opacity="0.13" />
        <text v-if="profile.diamond" x="4" :y="profile.diamond.y + 10" font-size="8.5" fill="#7aa0c4">钻石层 y&lt;16</text>
        <line v-if="profile.sea !== null" x1="0" :y1="profile.sea" :x2="W" :y2="profile.sea"
              stroke="#7f9fb5" stroke-dasharray="5,4" />
        <text v-if="profile.sea !== null" x="4" :y="profile.sea - 4" font-size="8.5" fill="#7f9fb5">地表 y=64</text>
        <text v-for="t in profile.ticks" :key="'yt' + t.v" x="-4" :y="t.y + 3" font-size="8.5"
              fill="var(--text-dim)" text-anchor="end">y={{ Math.round(t.v) }}</text>
        <template v-for="d in profile.lines" :key="'pl' + d.day">
          <line :x1="d.day * profile.dayW" y1="0" :x2="d.day * profile.dayW" :y2="PROF_H"
                stroke="var(--border)" />
          <polyline :points="d.points" fill="none" :stroke="d.color" stroke-width="1.6" opacity="0.9" />
          <text :x="(d.day - 0.5) * profile.dayW" :y="PROF_H - 3" font-size="8.5"
                fill="var(--text-dim)" text-anchor="middle">D{{ d.day }}</text>
        </template>
      </svg>
    </template>
  </div>
</template>

<style scoped>
.scene-map { display: flex; flex-direction: column; height: 100%; overflow-y: auto; padding: 10px 12px; gap: 6px; }
.scene-state {
  flex: 1; display: flex; flex-direction: column; align-items: center; justify-content: center;
  gap: 10px; color: var(--text-secondary); font-size: 13px; text-align: center;
}
.ss-title { font-size: 14px; font-weight: 600; }
.ss-desc { font-size: 11px; color: var(--text-dim); max-width: 320px; line-height: 1.5; }
.scene-head { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.scene-title { font-size: 13px; font-weight: 600; }
.scene-stat { font-size: 11px; color: var(--text-secondary); }
.scene-span { font-size: 11px; color: var(--text-dim); margin-left: auto; display: flex; align-items: center; gap: 6px; }
.scene-reset {
  font-size: 10px; padding: 1px 6px; border-radius: 4px; cursor: pointer;
  background: var(--bg-hover); border: 1px solid var(--border); color: var(--text-secondary);
}
.scene-reset:hover { border-color: var(--brand); color: var(--text-primary); }
.scene-note { font-size: 10.5px; color: var(--yellow); line-height: 1.45; }
.scene-note.miss { color: var(--text-dim); }
.scene-svg, .scene-prof { width: 100%; display: block; overflow: visible; }
/* 地图可拖：光标提示 + 禁掉拖拽时的文字选中（否则拖过标签会选中文字，手感很差） */
.scene-svg { cursor: grab; user-select: none; }
.scene-svg:active { cursor: grabbing; }
.scene-prof { margin-top: 8px; }
.place-mark { cursor: pointer; }
.kg-spinner {
  width: 26px; height: 26px; border: 3px solid var(--border); border-top-color: var(--brand);
  border-radius: 50%; animation: kg-spin .8s linear infinite;
}
@keyframes kg-spin { to { transform: rotate(360deg); } }
</style>
