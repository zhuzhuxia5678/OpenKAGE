<script>
import { renderMd, renderInline } from "./composables/useMarkdown.js";
import SceneMap from "./components/SceneMap.vue";
import VideoPane from "./components/VideoPane.vue";
import MetricsPane from "./components/MetricsPane.vue";
import InventoryPane from "./components/InventoryPane.vue";
import { queryForDay } from "./composables/replayPos.js";
import { ref, reactive, computed, watch, onMounted, onUnmounted, nextTick } from "vue";


function getNodeRadius(node) {
  // 技能节点是孤岛（facet_count/degree 全 0），按 concept 公式会恒为 16。给固定半径
  // 让它在图上和普通概念一样显眼，且与六边形形状匹配。
  if (node.kind === 'skill') return 18;
  const w = (node.facet_count || 0) * 0.8 + Math.log2(1 + (node.in_degree + node.out_degree)) * 1.6;
  return Math.min(32, 16 + w);
}
function getDegreeColor(node) {
  const deg = node.in_degree + node.out_degree;
  if (deg >= 6) return '#10b981';
  if (deg >= 3) return '#f59e0b';
  if (deg >= 1) return '#94a3b8';
  return '#cbd5e1';
}
function getDegreeArc(node) {
  const deg = node.in_degree + node.out_degree;
  const pct = Math.min(1, deg / 6);
  const r = getNodeRadius(node) + 4;
  return (pct * 2 * Math.PI * r).toFixed(1);
}
function getNodeIcon(node) {
  // 技能节点用齿轮符号（concept 用域首字母）——形状 + 图标双重区分，不靠颜色
  // （颜色已被 bookKey 占用区分图谱来源）。
  if (node.kind === 'skill') return '⚙';
  return (node.subject || '?').charAt(0).toUpperCase();
}
// 技能节点的六边形顶点（以节点中心为原点、半径 r）。concept 用 circle，技能用六边形
// 做形状区分；flat-top 朝向，和齿轮图标视觉协调。
function hexPoints(node) {
  const r = getNodeRadius(node);
  const pts = [];
  for (let i = 0; i < 6; i++) {
    const a = Math.PI / 3 * i;
    pts.push(`${(r * Math.cos(a)).toFixed(1)},${(r * Math.sin(a)).toFixed(1)}`);
  }
  return pts.join(' ');
}
function truncate(s, n) { s = String(s || ''); return s.length <= n ? s : s.slice(0, n) + '…'; }

// 章鱼主脑图谱（octopus_brain）本次训练新增/修改的节点标记：change_status 由后端按
// created_round/updated_round 算出（0=种子、>0=本次训练某个 batch 碰过），只有主脑卡的
// 节点才会带这个字段，其它图谱（real/staged/octopus_frozen/octopus_arm）没有这字段，不显示徽标。
const CHANGE_RING_COLOR = { new: '#22c55e', modified: '#f59e0b' };
function getChangeRingColor(node) { return CHANGE_RING_COLOR[node.change_status] || null; }

// 「已被聚合清掉的经验层」节点（node.cleared，只有副脑卡会有）：题内聚合每到窗口边界就把
// 经验层硬删，这些节点只存在于窗口快照里 —— 图上画出来是为了能看**经验的出处**（它们与
// 记忆层之间的边就是"哪条具体经历支撑了这条经验"）。
// 视觉上要和 draft 区分开（draft 是"还没落盘的候选"，cleared 是"已经毕业、离开副脑"）：
// 用更长的虚线节奏 + 更低的不透明度，看起来像"淡出"。
function getNodeDash(node) {
  if (node.cleared) return '5,3';
  return node.stage === 'draft' ? '3,2' : 'none';
}
function getNodeOpacity(node) {
  if (node.cleared) return 0.45;
  return node.stage === 'draft' ? 0.55 : 0.95;
}

// 布局的纯数学部分（力导向 + 四道数值稳定护栏）全在 layout.js —— 抽出去**只为可测**：
// 那边没有 Vue 依赖，能被 node 直接跑（见 layout.test.mjs）。放在这里就只能靠"另写一份模拟"
// 来验证，而那必然与实现漂移。
import { pullInOutliers, boundRadius, runForceLayout, runSimpleRepulsion,
         spiralPlacement } from "./layout.js";

export default {
  components: { SceneMap, VideoPane, MetricsPane, InventoryPane },
  setup() {
    const books = ref([]);
    const booksLoading = ref(true);
    const selectedKeys = ref(new Set());

    const canvasWrap = ref(null);
    const svgW = ref(1200);
    const svgH = ref(800);
    const zoom = ref(1);
    const pan = ref({ x: 0, y: 0 });

    const loadingGraph = ref(false);
    const nodes = ref([]);
    const edges = ref([]);
    const clusters = ref([]);  // Blocking阶段"疑似重复"候选簇，LLM还没裁决，纯展示用
    const stats = ref(null);

    const searchTerm = ref('');
    const searchFocused = ref(false);
    const selectedNode = ref(null);
    const selectedDetail = ref(null);

    function getNode(id) { return nodes.value.find(n => n.id === id); }
    function isOnEdge(edge, nodeId) { return edge.src === nodeId || edge.dst === nodeId; }
    function isNeighbor(nodeId, selectedId) {
      if (nodeId === selectedId) return true;
      return edges.value.some(e => (e.src === selectedId && e.dst === nodeId) || (e.dst === selectedId && e.src === nodeId));
    }
    const displayedEdges = computed(() => {
      if (selectedNode.value) {
        const sid = selectedNode.value.id;
        return edges.value.filter(e => e.src === sid || e.dst === sid);
      }
      return edges.value;
    });
    // 变化节点只留右上角的形状徽标做标记，不加 halo/呼吸环/跑马灯边——这些动效在密集图谱里
    // 反而是噪声（用户明确要求去掉），静态徽标够识别新增/修改了。
    function nodeGroupOpacity(node) {
      if (selectedNode.value && !isNeighbor(node.id, selectedNode.value.id)) return 0.2;
      return 1;
    }
    function edgeOpacity(edge) {
      if (selectedNode.value) return isOnEdge(edge, selectedNode.value.id) ? 1 : 0.12;
      // cleared = 连着「已聚合清掉的经验层」的边（出处连线）。压暗到 0.4 让它退到背景，
      // 但选中某个节点时上面那行仍会把它提到 1 —— 点开一条经验就能看清它的出处。
      if (edge.cleared) return 0.4;
      return edge.status === 'candidate' ? 0.55 : 0.85;
    }
    function edgeStrokeWidth(edge) {
      if (selectedNode.value && isOnEdge(edge, selectedNode.value.id)) return 3;
      return 2;
    }

    // 图例用的「已聚合清掉」计数。**从 nodes 现算**、不读 stats.cleared_count：多卡叠加时
    // stats 是前端按 merged 节点重算的（见 loadGraph 末尾），后端那个字段只对单卡成立 ——
    // 读它会在多选图谱时显示错的数。
    const clearedCount = computed(() => nodes.value.filter(n => n.cleared).length);

    const activeRelationTypes = computed(() => {
      const seen = new Set();
      const out = [];
      for (const e of edges.value) {
        if (!seen.has(e.relation)) { seen.add(e.relation); out.push(e.relation); }
      }
      return out;
    });

    const relationColorMap = computed(() => {
      const types = activeRelationTypes.value;
      const n = types.length;
      const map = {};
      types.forEach((rel, i) => {
        const hue = n <= 1 ? 0 : i * 270 / (n - 1);
        map[rel] = `hsl(${Math.round(hue)}, 70%, 55%)`;
      });
      return map;
    });

    // Blocking阶段"疑似重复"候选簇画成虚线圈——按簇内当前节点位置算一个包围椭圆，
    // 不重新摆放节点位置（只是把已有位置圈出来），节点位置变了圈会跟着重算
    const clusterShapes = computed(() => {
      const PAD = 28;
      return clusters.value.map(cl => {
        const members = cl.members.map(id => getNode(id)).filter(Boolean);
        if (members.length < 2) return null;
        const xs = members.map(n => n.x), ys = members.map(n => n.y);
        const minX = Math.min(...xs), maxX = Math.max(...xs);
        const minY = Math.min(...ys), maxY = Math.max(...ys);
        return {
          id: cl.id,
          cx: (minX + maxX) / 2,
          cy: (minY + maxY) / 2,
          rx: (maxX - minX) / 2 + PAD,
          ry: (maxY - minY) / 2 + PAD,
        };
      }).filter(Boolean);
    });

    // 力导向布局：簇内 + 簇间双层
    function layoutNodes(rawNodes) {
      const N = rawNodes.length;
      if (N === 0) return;
      const W = svgW.value || 1200, H = svgH.value || 800;
      const cx = W / 2, cy = H / 2;

      const adj = Object.create(null);
      for (const n of rawNodes) adj[n.id] = new Set();
      for (const e of edges.value) {
        if (adj[e.src] && adj[e.dst]) { adj[e.src].add(e.dst); adj[e.dst].add(e.src); }
      }

      const visited = new Set();
      const components = [];
      for (const n of rawNodes) {
        if (visited.has(n.id)) continue;
        const stack = [n.id];
        const comp = [];
        while (stack.length) {
          const id = stack.pop();
          if (visited.has(id)) continue;
          visited.add(id);
          const node = rawNodes.find(x => x.id === id);
          if (node) comp.push(node);
          for (const nb of adj[id] || []) if (!visited.has(nb)) stack.push(nb);
        }
        components.push(comp);
      }
      components.sort((a, b) => b.length - a.length);

      const clusters = components.map(comp => {
        // 初始摆位用不重叠的螺旋（②）、收敛后拉回离群点（④）、外接半径取分位数（③），
        // 三者都在 layout.js 里，理由见各函数注释。
        const particles = spiralPlacement(comp, getNodeRadius);
        runForceLayout(particles, adj, { iterations: 260, repulsion: 14000, linkDist: 140, damping: 0.78, gravity: 0.025 });
        pullInOutliers(particles);
        return { comp, particles, boundR: boundRadius(particles) };
      });

      const numC = clusters.length;
      const totalCirc = clusters.reduce((s, c) => s + c.boundR * 2 + 60, 0);
      const initR = numC > 1 ? totalCirc / (2 * Math.PI) : 0;
      const supers = clusters.map((cl, i) => ({
        x: cx + initR * Math.cos((i / numC) * 2 * Math.PI - Math.PI / 2),
        y: cy + initR * Math.sin((i / numC) * 2 * Math.PI - Math.PI / 2),
        r: cl.boundR + 30,
      }));
      if (numC > 1) {
        runSimpleRepulsion(supers, { iterations: 180, repulsion: 12000, gravity: 0.05, gcx: cx, gcy: cy });
        // 节点多、几乎没有连边（比如概念识别完还没跑关系抽取）时，每个概念都是孤立单例，
        // 超级节点初始摆位半径按数量线性放大，纯斥力+弱向心力180次迭代收不完，
        // 总有一两个"掉队"的甩得很远——外接包围盒被这几个离群点拖成巨大画布，
        // 真正聚在一起的主体反而显得很小。用中位数半径夹一下离群值，兜底防止画布被撑爆。
        //
        // ⚠️ **原来的 `cap = median*2 + 150` 是个空护栏**（2026-08-13 查出）：所有超级节点的
        //   初始距离**都等于 initR**，若斥力没把它们拉开多少，中位数就≈initR 本身，于是
        //   cap≈2×initR —— 比谁都大，一个都夹不到。实测 21 个簇时只夹到 1 个，形同虚设。
        //   改成以「簇自身尺寸」为基准：所有簇的外接半径之和就是它们**紧挨着排一圈**所需的
        //   半径量级，超过它两倍就是真的甩飞了。这个基准与迭代结果无关，不会自我塌缩。
        const dists = supers.map(s => Math.hypot(s.x - cx, s.y - cy));
        const ringR = clusters.reduce((s, c) => s + c.boundR, 0) / Math.PI;  // 周长≈2Σr → R=Σr/π
        const cap = Math.max(ringR * 2, 400);
        supers.forEach((s, i) => {
          if (dists[i] > cap) {
            const scale = cap / dists[i];
            s.x = cx + (s.x - cx) * scale;
            s.y = cy + (s.y - cy) * scale;
          }
        });
      } else if (supers[0]) {
        supers[0].x = cx; supers[0].y = cy;
      }

      clusters.forEach((cl, ci) => {
        const sx = supers[ci].x, sy = supers[ci].y;
        cl.comp.forEach((n, ni) => {
          const p = cl.particles[ni];
          n.x = sx + p.x; n.y = sy + p.y;
        });
      });

      if (rawNodes.length > 0) {
        const PAD = 60;
        const xs = rawNodes.map(n => n.x), ys = rawNodes.map(n => n.y);
        const minX = Math.min(...xs) - PAD, minY = Math.min(...ys) - PAD;
        const maxX = Math.max(...xs) + PAD, maxY = Math.max(...ys) + PAD;
        rawNodes.forEach(n => { n.x += -minX; n.y += -minY; });
        svgW.value = Math.max(svgW.value, maxX - minX);
        svgH.value = Math.max(svgH.value, maxY - minY);
      }
    }

    function relayoutWithRealSize() {
      if (!canvasWrap.value || canvasWrap.value.clientWidth === 0) return;
      svgW.value = Math.max(canvasWrap.value.clientWidth, svgW.value);
      svgH.value = Math.max(canvasWrap.value.clientHeight, svgH.value);
    }

    let _resizeObserver = null;
    function setupResizeObserver() {
      if (_resizeObserver || !canvasWrap.value) return;
      _resizeObserver = new ResizeObserver(() => relayoutWithRealSize());
      _resizeObserver.observe(canvasWrap.value);
    }
    onUnmounted(() => { _resizeObserver?.disconnect(); _resizeObserver = null; });

    let booksInitialized = false;
    async function loadBooks() {
      booksLoading.value = true;
      try {
        const res = await fetch('/api/books');
        const data = await res.json();
        books.value = data.books;
      } finally {
        booksLoading.value = false;
      }
      if (!booksInitialized) {
        booksInitialized = true;
      }
      // 初始不默认选任何图谱：进入时图谱区就是空选择，用户点卡后再拉图。
      if (selectedKeys.value.size) loadGraph();
    }

    // 书本卡片列表定时轮询：训练/上传期间卡片会自动出现/消失（章鱼主脑/副脑图谱、
    // 识别中占位卡等），只刷新卡片列表本身，不联带 loadGraph()——
    // 否则每次轮询都会打断用户正在查看的图谱（缩放/选中状态被重置）。
    let booksPollTimer = null;
    async function pollBooksList() {
      const res = await fetch('/api/books');
      const data = await res.json();
      books.value = data.books;
      // 地图数据跟着刷新：训练在跑时 contexts/ 每天新增一份、记忆层也在长。
      // 不重置视口/选中（沿用 mergeLiveGraph 的原则），只换数据。
      if (sceneVisible.value) loadScene();
    }
    function startBooksPoll() {
      if (booksPollTimer) return;
      booksPollTimer = setInterval(pollBooksList, 10000);
    }
    function stopBooksPoll() {
      if (booksPollTimer) { clearInterval(booksPollTimer); booksPollTimer = null; }
    }

    // 章鱼 Agent 记忆图谱的三种卡（主脑 evolving / 冻结对照组 / 副脑），都是 octopus/results
    // 下的实验产物、走后端 build_memory_graph 那套（区别于 real/staged 的 markdown 图谱）。
    const OCTOPUS_ORIGINS = ['octopus_brain', 'octopus_frozen', 'octopus_arm'];
    function isOctopusBook(b) { return OCTOPUS_ORIGINS.includes(b.origin); }
    // 卡片右上角来源徽标文案：三种记忆图谱要一眼区分开——共享图谱是边跑边长的那份
    // （论文 shared graph）、初始共享图谱是迁移/对照的起点、私有图谱是聚合完就销毁的
    // 临时产物（论文 local graph，记忆层+经验层都在 agent 自己手里）。用词对齐论文。
    const OCTOPUS_PILL = {
      octopus_brain: '共享图谱',
      octopus_frozen: '初始共享图谱',
      octopus_arm: '私有图谱',
    };
    // planner-only（nograph，论文条件 B）的轨迹卡：同为 octopus_arm origin（复用 scene 接口
    // 的 slug 解析），但没有图谱 db ——徽标与计数位都换成轨迹语义，别让人以为是空图谱坏了。
    function octopusPillLabel(b) {
      if (b.kind === 'planner_only') return b.condition_label || 'planner-only 轨迹';
      return OCTOPUS_PILL[b.origin] || '';
    }

    // key 必须带 cell（root）：不同 cell 的 run01 卡片 origin/subject/book 全同，
    // 不带 cell 会互相覆盖选中态、metrics 也找错 run
    function bookKey(b) { return `${b.root || ''}/${b.origin}/${b.subject}/${b.book}`; }
    // 私有图谱卡的 book 里带 \n（书名第二行是 run 时间戳，见 server.py list_octopus_arm_books）。
    // 卡片上靠 .book-name 的 pre-line 正常换行，但图例/确认框这类单行位置得压平成一行。
    function oneLineBookName(b) { return String(b.book || '').replace(/\n/g, ' · '); }
    function bookApiBase(b) {
      // real/staged 的 {book} 段是 knowledge/kg_staging 下的真实目录名（后端用它定位目录）；
      // 章鱼三种图谱的后端 dispatch 以 book_slug 为准（主脑/对照组按 run 目录名、副脑按
      // "<run 目录名>__<db 文件名>"），且 book 字段是带空格/中点的展示名不适合当路径段，
      // 所以这几类改用 book_slug 当 {book} 段。
      const seg = isOctopusBook(b) ? b.book_slug : b.book;
      return `/api/books/${encodeURIComponent(b.origin)}/${encodeURIComponent(b.subject)}/${encodeURIComponent(seg)}`;
    }
    function graphUrl(b) {
      return `${bookApiBase(b)}/graph`;
    }
    function isBookSelected(b) { return selectedKeys.value.has(bookKey(b)); }
    function conceptCountLabel(b) {
      return '概念';
    }

    // ── 书架浏览：文件管理器式三层下钻（2026-09-28 重做）─────────────────────────
    // 数据仍是后端 `_run_meta_fields` 给的那套字段（exp_label / model / run_slug / n_arms /
    // max_nights …），但展示从「一棵全展开的四级树」改成**逐层下钻**，像文件管理器：
    //   第一层：实验 × 模型 组合卡（实验 = 主实验/消融/迁移/Scaling，判据在后端 _exp_category，
    //           与 RESULTS_TABLE.md 口径一致；模型做副标题）
    //   第二层：该组合下的 run 卡（起跑时间/条件/规模/主机/来源根）
    //   第三层：该 run 的图谱卡（共享图谱 / 初始共享图谱 / 各 agent 私有图谱），点选进画布
    // 面包屑可随时回退。
    // 模型排序（横比口径 2026-09-27）：GLM-5.3 已含官方池小写 glm-5.3 那批（后端 2026-10-09
    // 大小写归一，viewer/server.py _MODEL_DISPLAY_NAMES），故不再单列 'glm-5.3' 项。
    const MODEL_ORDER = ['DeepSeek-V4.1-Flash', 'GLM-5.3', 'hy4-preview', 'Kimi-K3', 'GLM-5.3-Flash'];
    const EXP_ORDER = ['主实验', '消融实验', '迁移实验', 'Scaling'];
    function modelRank(m) { const i = MODEL_ORDER.indexOf(m); return i < 0 ? MODEL_ORDER.length : i; }
    function expRank(e) { const i = EXP_ORDER.indexOf(e); return i < 0 ? EXP_ORDER.length : i; }
    // run 规模徽标：与默认 3×30 不同才标（scaling run 一眼可辨），同默认就不占地方。
    // 用词对齐论文：N agents × M game days（原「N臂×M夜」是内部黑话）。
    function scaleLabel(b) {
      const n = b.n_arms ?? 3, m = b.max_nights ?? 30;
      if (n === 3 && m === 30) return '';
      return `${n} agents × ${m} 天`;
    }

    // 下钻状态：null = 第一层（实验×模型组合）；{ exp, model } = 第二层（该组合下的 run 列表）；
    // { exp, model, run } = 第三层（该 run 的图谱卡）。模型维存在组合卡副标题上。
    const drill = ref(null);

    // ── 第一层数据：按实验聚合、组内按模型切卡 ──
    // groupTree 把 run 卡按 实验→模型→run 三级组织好；shelfGroups 摊平成
    // 组合卡列表（实验做 major、模型做 minor）。
    const groupTree = computed(() => {
      const byExp = new Map();
      for (const b of books.value) {
        const e = b.exp_label || '主实验';
        const m = b.model || '未知模型';
        // key = cell/run：不同 cell 的 run01..NN 同名（发布布局按 cell 内编号），不带 cell 会撞键合并
        const r = (b.root ? b.root + '/' : '') + (b.run_slug || b.book_slug);
        if (!byExp.has(e)) byExp.set(e, new Map());
        const models = byExp.get(e);
        if (!models.has(m)) models.set(m, new Map());
        const runs = models.get(m);
        if (!runs.has(r)) runs.set(r, { key: `${e}/${m}/${r}`, run_slug: r, first: b, books: [] });
        runs.get(r).books.push(b);
      }
      const exps = [];
      for (const [e, models] of byExp) {
        const modelList = [];
        for (const [m, runs] of models) {
          const num = s => { const m = /\d+$/.exec(s); return m ? +m[0] : Infinity; };
          const runList = [...runs.values()].sort((x, y) =>
            (num(x.run_slug) - num(y.run_slug)) || (x.run_slug < y.run_slug ? -1 : 1)); // launch order
          modelList.push({ name: m, runs: runList, count: runList.reduce((s, r) => s + r.books.length, 0) });
        }
        modelList.sort((a, b) => modelRank(a.name) - modelRank(b.name) || a.name.localeCompare(b.name));
        exps.push({ name: e, models: modelList, count: modelList.reduce((s, m) => s + m.count, 0) });
      }
      exps.sort((a, b) => expRank(a.name) - expRank(b.name) || a.name.localeCompare(b.name));
      return { exps };
    });

    // 第一层组合卡：{ major: '主实验', minor: 'DeepSeek-V4.1-Flash', runs, count }，按实验序排
    const shelfGroups = computed(() => {
      const cards = [];
      for (const e of groupTree.value.exps)
        for (const m of e.models)
          cards.push({ major: e.name, minor: m.name, runs: m.runs, count: m.count });
      return cards;
    });
    function enterGroup(g) { drill.value = { major: g.major, minor: g.minor, runs: g.runs }; }

    // ── 第二层：当前组合下的 run 卡 ──
    // 轮询刷新 books 后 runs 里的 first/books 引用会换成新对象，drill 只存名字、
    // 每次从 shelfGroups 里现查 —— 下钻状态天然跟数据刷新同步，不会指到过期引用。
    const currentRuns = computed(() => {
      const d = drill.value;
      if (!d) return [];
      const g = shelfGroups.value.find(x => x.major === d.major && x.minor === d.minor);
      return g ? g.runs : [];
    });

    // runTs 一并存进 drill：面包屑这一级要显示可读时间戳（工具行不再重复路径，见模板注释）
    function enterRun(r) { drill.value = { ...drill.value, run: r.key, runSlug: r.run_slug, runTs: r.first.run_ts }; }

    // ── 直播：正在跑的 run 直接抓观战客户端的 X 屏（后端 /api/live）──
    // 只在**本机**在跑的 run 上出现（远端 run 的 X 屏不在这台机器上，后端扫不到 ffmpeg）。
    // 画面是后端常驻 ffmpeg 推的 MJPEG 流（3fps），<img> 直接吃；关掉面板/离开这层 → <img>
    // 卸载 → 连接断 → 后端杀掉那路 ffmpeg，不留常驻进程。
    const liveRuns = ref({});        // run 目录名 → {root, day, arms:[{arm,display,slot,size,day,pid}]}
    const liveSeenArms = ref({});    // run 目录名 → [arm…]：出现过就留着（VideoPane 判直播天用）
    const liveLastSeen = ref({});    // run 目录名 → 最后一次扫到它在录的时间戳

    async function pollLive() {
      try {
        const data = await (await fetch('/api/live')).json();
        const m = {};
        const seen = { ...liveSeenArms.value };
        const last = { ...liveLastSeen.value };
        for (const r of data.runs || []) {
          m[r.run] = r;
          last[r.run] = Date.now();
          const set = new Set(seen[r.run] || []);
          for (const a of r.arms) set.add(a.arm);
          seen[r.run] = [...set].sort();
        }
        liveRuns.value = m;
        liveSeenArms.value = seen;
        liveLastSeen.value = last;
      } catch (err) {
        // 直播是附加功能：后端没这个接口 / 挂了都静默，别把整个书架轮询带崩
      }
    }
    let livePollTimer = null;
    onMounted(() => {
      pollLive();
      // 10s：天边界重启录制器约 1.5s、反思几分钟，跟 /api/books 同频就够，不用更勤
      livePollTimer = setInterval(pollLive, 10000);
    });
    onUnmounted(() => { if (livePollTimer) clearInterval(livePollTimer); });

    const liveInfo = computed(() => (drill.value?.runSlug ? liveRuns.value[drill.value.runSlug] : null));

    // ── 第三层：该 run 的图谱卡 ──
    const currentRunCards = computed(() => {
      const d = drill.value;
      if (!d || !d.run) return [];
      const r = currentRuns.value.find(x => x.key === d.run);
      return r ? r.books : [];
    });

    // 面包屑：实验/模型（点击回第一层）· run 时间戳（点击回第二层）
    const crumbs = computed(() => {
      const d = drill.value;
      if (!d) return [];
      const c = [{ label: `${d.major} / ${d.minor}`, back: () => { drill.value = null; } }];
      // 第三层那段显示 run_ts 而不是 run_slug（目录名）：路径已经表达了「哪个实验/模型」，
      // 这一级只补「哪一次 run」，用可读的时间戳；目录名带 host/模型段，摆进面包屑太长。
      if (d.run) c.push({ label: d.runTs || d.run, back: () => { drill.value = { major: d.major, minor: d.minor, runs: d.runs }; } });
      return c;
    });

    // ── 选中 ──
    // 选图谱一律点单卡 toggleBook；全选按钮已整层撤掉（一组/一 run 动辄上百张图谱，
    // 一次全选等于把半个库塞进画布）。

    // 书籍 → 颜色：不管选中没选中，每本书都有颜色，按书籍列表里的全部书（不是"选中的书"）在
    // 七彩色谱（红→紫）上均分取点算出hue，算完缓存固定下来（bookHueCache 是个持久的reactive
    // 对象，不会清空）。触发重算的是"书籍总数变了"（新增/删除了一本书），跟选中状态完全无关——
    // 选中/取消选中书籍不会导致任何书的颜色变化，颜色是这本书自己的固定属性，不是"当前这批
    // 选中的书要互相区分"的临时安排。
    const RAINBOW_HUE_RANGE = [0, 270]; // 红(0)→紫(270)，留一段不用满360°，避免终点和起点撞回同一个红
    const bookHueCache = reactive({});
    const allBookCount = computed(() => books.value.length);
    watch(allBookCount, (n) => {
      books.value.forEach((b, i) => {
        const hue = n <= 1 ? RAINBOW_HUE_RANGE[0]
          : RAINBOW_HUE_RANGE[0] + i * (RAINBOW_HUE_RANGE[1] - RAINBOW_HUE_RANGE[0]) / (n - 1);
        bookHueCache[bookKey(b)] = hue;
      });
    }, { immediate: true });
    function getBookHue(key) { return bookHueCache[key] ?? 0; }
    function getBookColor(key) { return key ? `hsl(${getBookHue(key)}, 70%, 55%)` : '#94a3b8'; }
    function getNodeColor(node) { return getBookColor(node.bookKey); }
    function getNodeBg(node) { return `hsla(${getBookHue(node.bookKey)}, 70%, 55%, 0.12)`; }

    // resetView/resetSelection=false 用于流水线自动进度触发的重新布局（见 fetchAllPipelineStatuses
    // 里的 pendingRelayoutKeys 分支）——跟用户主动切书/翻步骤时的 loadGraph() 是同一套拉数据+布局
    // 逻辑，只是不打断用户当前正看着的视角和详情面板。
    // 网络抖动 / 线程池被轮询占满时 fetch 可能长期不返回；给个上限，
    // 超时抛 AbortError → loadGraph 的 catch/finally 会收尾，不会永远停在「加载图谱中...」。
    function fetchWithTimeout(url, ms = 30000) {
      const ac = new AbortController();
      const timer = setTimeout(() => ac.abort(), ms);
      return fetch(url, { signal: ac.signal }).finally(() => clearTimeout(timer));
    }

    async function loadGraph({ resetView = true, resetSelection = true } = {}) {
      const selected = books.value.filter(isBookSelected);
      loadingGraph.value = true;
      if (resetSelection) { selectedNode.value = null; selectedDetail.value = null; }
      if (resetView) { svgW.value = 1200; svgH.value = 800; zoom.value = 1; pan.value = { x: 0, y: 0 }; }
      try {
        const results = await Promise.all(selected.map(async b => {
          const graphRes = await fetchWithTimeout(graphUrl(b));
          const data = await graphRes.json();
          const key = bookKey(b);
          const prefix = key + '::';
          const bookNodes = (data.nodes || []).map(n => ({ ...n, id: prefix + n.slug, bookKey: key, origin: b.origin, subject: b.subject, book: b.book, book_slug: b.book_slug }));
          const bookEdges = (data.edges || []).map(e => ({ ...e, id: prefix + e.id, src: prefix + e.src, dst: prefix + e.dst }));
          const bookClusters = (data.clusters || []).map(cl => ({ ...cl, id: prefix + cl.id, members: cl.members.map(m => prefix + m) }));
          return { nodes: bookNodes, edges: bookEdges, clusters: bookClusters };
        }));

        const rawNodes = results.flatMap(r => r.nodes);
        edges.value = results.flatMap(r => r.edges);
        clusters.value = results.flatMap(r => r.clusters);
        const _skillN = rawNodes.filter(n => n.kind === 'skill').length;
        stats.value = {
          concept_count: rawNodes.length - _skillN,
          skill_count: _skillN,
          edge_count: edges.value.length,
          subjects: [...new Set(rawNodes.map(n => n.subject))],
        };
        layoutNodes(rawNodes);
        packClustersInstant(rawNodes, clusters.value);
        nodes.value = rawNodes;
        await nextTick();
        relayoutWithRealSize();
        setupResizeObserver();
      } catch (err) {
        console.error('加载图谱失败', err);
      } finally {
        loadingGraph.value = false;
      }
    }

    function toggleBook(b) {
      const key = bookKey(b);
      const next = new Set(selectedKeys.value);
      if (next.has(key)) next.delete(key); else next.add(key);
      selectedKeys.value = next;
      loadGraph();
    }

    // ── 疑似重复簇的节点自动往簇内质心吸拢，不再是"东边一个西边一个画个大圈"——
    // packClustersInstant 一次性瞬间归位（loadGraph 力导向布局刚摆完位置时用，不用再跟着动画
    // 从头收敛一遍）；stepClusterPacking 是持续跑的懒惰插值，管实时轮询过程中的细微调整。
    // 正在被用户拖拽的节点两边都会跳过，不跟手抢节点。
    function clusterPackTargets(clusterList, lookup) {
      const out = [];
      for (const cl of clusterList) {
        const members = cl.members.map(lookup).filter(Boolean);
        if (members.length < 2) continue;
        const cx = members.reduce((s, p) => s + p.x, 0) / members.length;
        const cy = members.reduce((s, p) => s + p.y, 0) / members.length;
        const r = 20 + 6 * members.length;
        members.forEach((n, i) => {
          const angle = (2 * Math.PI * i) / members.length;
          out.push([n, cx + Math.cos(angle) * r, cy + Math.sin(angle) * r]);
        });
      }
      return out;
    }
    function packClustersInstant(nodeList, clusterList) {
      const byId = new Map(nodeList.map(n => [n.id, n]));
      for (const [n, tx, ty] of clusterPackTargets(clusterList, id => byId.get(id))) {
        n.x = tx; n.y = ty;
      }
    }
    let clusterAnimHandle = null;
    function stepClusterPacking() {
      for (const [n, tx, ty] of clusterPackTargets(clusters.value, getNode)) {
        if (n === draggingNode) continue;
        n.x += (tx - n.x) * 0.08;
        n.y += (ty - n.y) * 0.08;
      }
      clusterAnimHandle = clusters.value.length ? requestAnimationFrame(stepClusterPacking) : null;
    }
    watch(clusters, () => {
      if (clusters.value.length && clusterAnimHandle === null) {
        clusterAnimHandle = requestAnimationFrame(stepClusterPacking);
      }
    });
    onUnmounted(() => { if (clusterAnimHandle !== null) cancelAnimationFrame(clusterAnimHandle); });

    // ── 实时图谱：跟 loadGraph 的区别是不重置缩放/平移，也不重新跑力导向布局——
    // 已有节点原地保留坐标，只有新出现的节点才要决定放哪，这样才不会每次轮询都全图跳动 ──
    function mergeLiveGraph(freshNodes, freshEdges, freshClusters) {
      const oldById = new Map(nodes.value.map(n => [n.id, n]));
      const cx = (svgW.value || 1200) / 2, cy = (svgH.value || 800) / 2;

      const brandNew = [];
      const merged = freshNodes.map(fresh => {
        const old = oldById.get(fresh.id);
        if (old) return { ...fresh, x: old.x, y: old.y };
        const parentIds = (fresh.merged_from_slugs || []).map(s => fresh.bookKey + '::' + s);
        const parents = parentIds.map(id => oldById.get(id)).filter(Boolean);
        if (parents.length) {
          return {
            ...fresh,
            x: parents.reduce((s, p) => s + p.x, 0) / parents.length,
            y: parents.reduce((s, p) => s + p.y, 0) / parents.length,
          };
        }
        const node = { ...fresh, x: cx + (Math.random() - 0.5) * 140, y: cy + (Math.random() - 0.5) * 140 };
        brandNew.push(node);
        return node;
      });

      // 全新节点（没有旧位置也没有父节点）会都落在中心同一小块里，跑一轮轻量斥力让它们互相推开
      // ——只动这批新节点自己，不碰已有节点坐标，不会打断用户正在看的图谱。
      if (brandNew.length > 1) {
        const particles = brandNew.map(n => ({ x: n.x, y: n.y, r: getNodeRadius(n) + 12 }));
        runSimpleRepulsion(particles, { iterations: 120, repulsion: 6000, damping: 0.75, gravity: 0.03, gcx: cx, gcy: cy });
        brandNew.forEach((n, i) => { n.x = particles[i].x; n.y = particles[i].y; });
      }

      nodes.value = merged;
      edges.value = freshEdges;
      clusters.value = freshClusters || [];
      const _skillN = merged.filter(n => n.kind === 'skill').length;
      stats.value = {
        concept_count: merged.length - _skillN,
        skill_count: _skillN,
        edge_count: freshEdges.length,
        subjects: [...new Set(merged.map(n => n.subject))],
      };
    }

    async function refreshLiveGraph() {
      if (loadingGraph.value) return;  // loadGraph()正在跑完整布局，别跟它抢着写nodes.value
      try {
        const results = await Promise.all(selected.map(async b => {
          const res = await fetchWithTimeout(graphUrl(b));
          const data = await res.json();
          const key = bookKey(b);
          const prefix = key + '::';
          const bookNodes = (data.nodes || []).map(n => ({ ...n, id: prefix + n.slug, bookKey: key, origin: b.origin, subject: b.subject, book: b.book, book_slug: b.book_slug }));
          const bookEdges = (data.edges || []).map(e => ({ ...e, id: prefix + e.id, src: prefix + e.src, dst: prefix + e.dst }));
          const bookClusters = (data.clusters || []).map(cl => ({ ...cl, id: prefix + cl.id, members: cl.members.map(m => prefix + m) }));
          return { nodes: bookNodes, edges: bookEdges, clusters: bookClusters };
        }));
        mergeLiveGraph(results.flatMap(r => r.nodes), results.flatMap(r => r.edges), results.flatMap(r => r.clusters));
      } catch (err) {
        console.error('刷新实时图谱失败', err);
      }
    }

    // ── 交互 ──
    let isPanning = false, panStart = { x: 0, y: 0 };
    let draggingNode = null, dragStart = { x: 0, y: 0 };
    function onSvgMousedown(e) {
      if (e.button !== 0) return;
      isPanning = true;
      panStart = { x: e.clientX - pan.value.x, y: e.clientY - pan.value.y };
    }
    function onNodeMousedown(e, node) {
      draggingNode = node;
      dragStart = { x: (e.clientX - pan.value.x) / zoom.value - node.x, y: (e.clientY - pan.value.y) / zoom.value - node.y };
      e.stopPropagation();
    }
    function onSvgMousemove(e) {
      if (draggingNode) {
        draggingNode.x = (e.clientX - pan.value.x) / zoom.value - dragStart.x;
        draggingNode.y = (e.clientY - pan.value.y) / zoom.value - dragStart.y;
        return;
      }
      if (isPanning) pan.value = { x: e.clientX - panStart.x, y: e.clientY - panStart.y };
    }
    function onSvgMouseup() { isPanning = false; draggingNode = null; }
    function onWheel(e) {
      const delta = e.deltaY > 0 ? -0.1 : 0.1;
      const newZoom = Math.max(0.4, Math.min(8, zoom.value + delta));
      const rect = canvasWrap.value?.getBoundingClientRect();
      if (rect) {
        const mx = e.clientX - rect.left, my = e.clientY - rect.top;
        pan.value = { x: mx - (mx - pan.value.x) * (newZoom / zoom.value), y: my - (my - pan.value.y) * (newZoom / zoom.value) };
      }
      zoom.value = newZoom;
    }
    function resetView() { zoom.value = 1; pan.value = { x: 0, y: 0 }; }

    async function selectNode(node) {
      selectedNode.value = node;
      footage.value = [];
      try {
        // 技能节点走独立的 /skill 接口（skill 不是 concept，查 concept 表查不到），
        // 但技能同样有「产自哪几步」的录像映射（落 skill_steps.jsonl，与 facet 同构），
        // 所以也拉录像数据、在详情面板挂跳播入口。
        if (node.kind === 'skill') {
          const res = await fetch(`${bookApiBase(node)}/skill/${encodeURIComponent(node.slug)}`);
          if (!res.ok) throw new Error(`技能详情请求失败（${res.status}）`);
          selectedDetail.value = await res.json();
          loadVideo(node);
          return;
        }
        const res = await fetch(`${bookApiBase(node)}/concept/${encodeURIComponent(node.slug)}`);
        selectedDetail.value = await res.json();
        loadFootage(node, selectedDetail.value);
        loadVideo(node);
      } catch (err) {
        console.error('加载节点详情失败', err);
        selectedDetail.value = null;
      }
    }

    // ---- 关键事件画面（训练时录的死亡/tier 突破/成就各一帧，见 domains/minecraft/footage.py）
    // 只有副脑卡的**记忆层**概念有画面可看：day 取自 concept.created_at（第几天记下的），
    // 经验层是去情境化的通名（"夜间地表危险"），没有"哪一天"的含义。
    // ---- 录像：**按天分段**，每条 facet 各挂自己那一段的那几秒
    // 切块单位是 JS 步（一次 write_code），不是 ReAct 轮 —— createplan/retrieve 那些
    // 只在 LLM 侧发生、画面一动不动。步 → 段内时间区间的索引由训练时落盘（steps.jsonl）。
    //
    // ★ **facet 不与"概念哪天建的"绑定，只与视频绑定**（2026-08-12 用户定案）：每条 facet
    //   自带 day + url + range（后端已按它自报的那天核对过步号），所以第 5 天往第 2 天的概念
    //   上补的 facet 也能挂上第 5 天的画面 —— 此前整个面板按概念的 day 拉视频，那条永远看不到。
    //   也因此**不看层**：经验层概念只要有 facet 报了来源步，一样能看。
    const video = ref(null);          // {has_video, days:[{day,url,steps,range}], facet_steps:{}}
    const openClip = ref(null);       // 当前展开的那条 facet 的片段（null = 都收起）
    async function loadVideo(node) {
      video.value = null; openClip.value = null;
      if (!node || node.origin !== 'octopus_arm') return;
      try {
        const res = await fetch(`${bookApiBase(node)}/video`);
        const data = await res.json();
        if (selectedNode.value && selectedNode.value.slug === node.slug && data.has_video) {
          video.value = data;
        }
      } catch (err) {
        console.error('加载录像失败', err);
      }
    }
    //: 这条 facet 有没有录像片段（反思 Agent 自报来源步 → 后端核对过步号真实存在）。
    //  键与后端一致：concept_slug + \t + kind（facet 表里这一对本就唯一）。
    function facetClip(f) {
      const m = video.value && video.value.facet_steps;
      if (!m || !selectedDetail.value) return null;
      return m[`${selectedDetail.value.slug}\t${f.kind}`] || null;
    }
    //: 这个技能有没有「产自哪几步」的录像片段（后端 video.skill_steps[slug]，与 facet 同构）。
    //  键就是 slug（skill 无 kind、slug 不可变）。来源步只在 create_skill 时落一次（用户定案：
    //  update_skill 不碰、指向诞生现场），delete_skill 后查不到（drop 已落）。
    function skillClip(slug) {
      const m = video.value && video.value.skill_steps;
      if (!m) return null;
      return m[slug] || null;
    }
    //: 技能的录像展开/收起。openClip 的 key 用 `skill:{slug}` 前缀，避免与 facet 的
    //  `slug\tkind` 键撞——同一个 selectedDetail 切换时两套互不干扰。
    function toggleSkillClip(slug) {
      const clip = skillClip(slug);
      if (!clip || clip.unresolved) return;
      const key = `skill:${slug}`;
      openClip.value = openClip.value === key ? null : key;
    }
    function skillClipOpen(slug) {
      return openClip.value === `skill:${slug}`;
    }
    //: **只挂与这个概念有关的那几天**的完整录像，而不是整条轨迹的每一天。
    //  为什么：跑 30 天时 `video.days` 有 30 段，每段几十 MB —— 挂在每个概念下面等于
    //  「点开任意一个概念都看到 30 个完整视频」，既找不到重点、又让人以为它们都与该概念有关。
    //  相关的定义（取并集）：
    //    ① 该概念名下**任一 facet 自报的来源天**（facetClip 的 day，后端已核对过步号）；
    //    ② 概念自己的 `day`（= created_at 那天）—— 一条 facet 都没报来源步时的兜底，
    //       至少还能看「它是哪天被写下来的」。
    //  实测那轮 carol：162 个概念里 161 个只关联 1 天、1 个关联 2 天（而不是 30 天）。
    //  ★ `unresolved` 的 clip 不算「相关天」：它的步号在那一天的索引里根本不存在，
    //    拿它去挂整天录像等于凭一个幻觉步号把一整天的视频摆出来。
    const relevantDays = computed(() => {
      if (!video.value || !selectedDetail.value) return [];
      const want = new Set();
      for (const f of selectedDetail.value.facets || []) {
        const clip = facetClip(f);
        if (clip && !clip.unresolved) want.add(clip.day);
      }
      if (!want.size && selectedDetail.value.day) want.add(selectedDetail.value.day);
      return video.value.days.filter(d => want.has(d.day));
    });
    //: 展开/收起这条 facet 的视频。**点开才加载** —— 一条轨迹有几十条 facet，
    //  全部渲染成 <video> 会让浏览器同时开几十个连接去拉 preload=metadata。
    function toggleClip(f) {
      const clip = facetClip(f);
      if (!clip || clip.unresolved) return;   // 对不上索引的没有可播片段
      const key = `${selectedDetail.value.slug}\t${f.kind}`;
      openClip.value = openClip.value === key ? null : key;
    }
    function clipOpen(f) {
      return selectedDetail.value
        && openClip.value === `${selectedDetail.value.slug}\t${f.kind}`;
    }
    //: 拼出带时间片段的视频 URL。#t=起,止 是浏览器原生的媒体片段语法，
    //  配合后端的 HTTP Range（206）就能直接跳到第 N 秒，不用先下完前面。
    function clipSrc(clip) {
      return clip ? `${clip.url}#t=${clip.range[0]},${clip.range[1]}` : '';
    }
    function fmtDur(t) {
      const m = Math.floor(t / 60), s = Math.floor(t % 60);
      return `${m}:${String(s).padStart(2, '0')}`;
    }

    // ── 全局单播放器（2026-09-30 用户定案）─────────────────────────────────
    // 同一时刻只允许一个 <video> 在播：录像面板和 concept 详情面板的视频都往 replayPos
    // 里打位置（地图标记），两个同时播、标记就会两边来回跳。任何 video 起播 → 暂停其余
    // 所有 video，并记下「位置标记归谁」（replayOwner，起播后粘住 —— 暂停时标记停在
    // 原处不换主）。DOM 级实现而不是组件间传 ref：视频散在两个组件的四处模板里，
    // 谁在播由浏览器说了算；play 不冒泡，capture 监听 document 才收得到。
    const replayOwner = ref(null);      // 'pane' | 'detail'：最近一次起播的视频在哪个面板
    const autoPaused = new WeakSet();   // 被单播放器暂停的 video：它的 pause 回调闭嘴一拍
                                        //（否则会把刚接管的标记打回旧视频的位置）
    function onAnyVideoPlay(ev) {
      const el = ev.target;
      if (!(el instanceof HTMLVideoElement)) return;
      replayOwner.value = el.closest('.video-pane') ? 'pane' : 'detail';
      document.querySelectorAll('video').forEach(v => {
        if (v !== el && !v.paused) { autoPaused.add(v); v.pause(); }
      });
    }
    onMounted(() => document.addEventListener('play', onAnyVideoPlay, true));
    onUnmounted(() => document.removeEventListener('play', onAnyVideoPlay, true));

    // ── concept 详情面板的录像 → 位置联动 ─────────────────────────────────
    // 面板里三处 <video>（facet 片段 / 技能片段 / 按天整段「相关录像」）与右侧录像面板
    // 共用同一个 replayPos，规则同源（composables/replayPos.js）：播放中按视频时间算出
    // 当时位置，交给 onReplayPos 走同一套「地图打标记 + 自动切臂/开地图」。
    // 只取**当前选中概念所属的那条轨迹**（video 就是 loadVideo(selectedNode) 拉的），
    // 所以 day 一定落在同一 run/arm 里，不会把标记打到别的世界去。
    // ★ 联动前提是这条轨迹的卡**正在被选中**（isBookSelected）：selectedNode 的 bookKey
    //   一定在 selectedKeys 里（点节点=选卡），防御的是「选中集合被批量改」的极端时序。
    function onDetailVideoTime(ev, day) {
      if (replayOwner.value === 'pane') return;   // 标记归录像面板：详情视频的事件不接管
      const el = ev?.target;
      if (autoPaused.delete(el)) return;          // 被单播放器暂停的（详情内互切）：不报位置
      if (!el || day == null || !video.value?.days?.length) {
        onReplayPos(null); return;
      }
      const q = queryForDay(video.value.days, day);
      if (!q.usable) { onReplayPos(null); return; }
      const p = q.at(el.currentTime);
      onReplayPos(p ? { day, ...p } : null);
    }
    // 收起片段 / 关详情面板 / 换选中概念 → 清标记（否则地图上停着上一个片段的最后位置）。
    // 标记归录像面板时不清：面板那边正播着，下个 timeupdate 自己会定位。
    watch(openClip, (v) => { if (!v && replayOwner.value !== 'pane') onReplayPos(null); });
    watch(selectedDetail, (d) => {
      if (replayOwner.value === 'detail') {
        replayOwner.value = null;         // 详情关了/换概念：所有权放回，录像面板可接管
        if (!d) onReplayPos(null);        // 换概念不用清 —— 下一个片段/暂停事件自己会重定位
      } else if (!d) onReplayPos(null);
    });

    const footage = ref([]);
    const footageLoading = ref(false);
    const footageZoom = ref(null);        // 点开的大图（null = 不显示灯箱）
    async function loadFootage(node, detail) {
      if (!node || node.origin !== 'octopus_arm' || !detail || !detail.day) return;
      footageLoading.value = true;
      try {
        const res = await fetch(`${bookApiBase(node)}/footage?day=${encodeURIComponent(detail.day)}`);
        const data = await res.json();
        // 竞态：连点两个节点时，晚到的响应不能盖掉当前选中的那个
        if (selectedNode.value && selectedNode.value.slug === node.slug) {
          footage.value = data.events || [];
        }
      } catch (err) {
        console.error('加载画面失败', err);
        footage.value = [];
      } finally {
        footageLoading.value = false;
      }
    }
    // Esc 关灯箱：灯箱是全屏遮罩，没有键盘出路的话只能靠点击，不符合直觉
    function onFootageKey(e) { if (e.key === 'Escape' && footageZoom.value) footageZoom.value = null; }
    onMounted(() => window.addEventListener('keydown', onFootageKey));
    onUnmounted(() => window.removeEventListener('keydown', onFootageKey));

    const FOOTAGE_LABEL = { death: '死亡', advancement: '成就' };
    function footageLabel(ev) {
      // tier 事件的 kind 形如 tier_stone（见 env.py 的 _snap(f"tier_{tier}")）
      if (ev.kind && ev.kind.startsWith('tier_')) return `突破 ${ev.kind.slice(5)}`;
      return FOOTAGE_LABEL[ev.kind] || ev.kind;
    }
    function footageDesc(ev) {
      if (ev.kind === 'death') {
        const who = ev.killer && ev.killer !== 'unknown' ? ev.killer : '';
        return [who, ev.cause].filter(Boolean).join(' · ');
      }
      if (ev.kind === 'advancement') return ev.advancement || '';
      return ev.tier || '';
    }
    function selectByPeer(peerId) {
      if (!selectedNode.value) return;
      const n = getNode(selectedNode.value.bookKey + '::' + peerId);
      if (n) selectNode(n);
    }
    function closeDetail() { selectedNode.value = null; selectedDetail.value = null; }

    // 技能 JS 代码复制：navigator.clipboard 不可用时（非 https / 旧浏览器）退到 textarea 兜底。
    const skillCopied = ref(false);
    async function copySkillCode() {
      const code = selectedDetail.value?.js_code || '';
      try {
        if (navigator.clipboard?.writeText) {
          await navigator.clipboard.writeText(code);
        } else {
          const ta = document.createElement('textarea');
          ta.value = code; document.body.appendChild(ta); ta.select();
          document.execCommand('copy'); document.body.removeChild(ta);
        }
        skillCopied.value = true;
        setTimeout(() => { skillCopied.value = false; }, 1500);
      } catch (err) {
        console.error('复制技能代码失败', err);
      }
    }
    function formatPageRanges(pages) {
      if (!pages || !pages.length) return '';
      const ranges = [];
      let start = pages[0], prev = pages[0];
      for (let i = 1; i <= pages.length; i++) {
        const p = pages[i];
        if (p === prev + 1) { prev = p; continue; }
        ranges.push(start === prev ? `${start}` : `${start}-${prev}`);
        start = prev = p;
      }
      return ranges.join('、');
    }
    function centerOnNode(node) {
      const rect = canvasWrap.value?.getBoundingClientRect();
      if (rect) pan.value = { x: rect.width / 2 - node.x * zoom.value, y: rect.height / 2 - node.y * zoom.value };
    }
    function searchAndSelect() {
      const term = searchTerm.value.trim().toLowerCase();
      if (!term) return;
      const hit = nodes.value.find(n => (n.title || '').toLowerCase().includes(term) || (n.slug || '').toLowerCase().includes(term));
      if (hit) {
        selectNode(hit);
        centerOnNode(hit);
      } else {
        alert(`没找到 "${term}"`);
      }
    }

    // ── 探索地图（私有图谱专属）─────────────────────────────────────────────
    // 地理坐标只存在于**副脑私有图谱的记忆层**（主脑全是去情境化的经验概念、一个坐标都没有），
    // 所以只有 octopus_arm 卡有地图。一次只画一个 arm：三个副脑跑在三台独立的 MC server 上
    // （同 seed 同地形但互不相通），叠一张图会暗示它们同处一地、可能相遇甚至抢资源。
    const showScene = ref(false);
    const sceneArmKey = ref(null);     // 当前画哪张 arm 卡（bookKey）
    const sceneData = ref(null);
    const sceneLoading = ref(false);
    const splitPct = ref(100 / 3);     // 右侧地图占比（默认 1/3，可拖）

    const sceneBooks = computed(() =>
      books.value.filter(b => b.origin === 'octopus_arm' && b.has_scene && isBookSelected(b)));
    const sceneBook = computed(() =>
      sceneBooks.value.find(b => bookKey(b) === sceneArmKey.value) || sceneBooks.value[0] || null);
    const sceneVisible = computed(() => showScene.value && !!sceneBook.value);
    // 选中的卡里有没有 planner-only 轨迹卡（无图谱）：左侧画布要给专门的空态文案，
    // 而不是「图谱里还没有概念」（那会让人以为数据丢了）。
    const selectedPlannerOnly = computed(() =>
      books.value.some(b => b.kind === 'planner_only' && isBookSelected(b)));

    async function loadScene() {
      const b = sceneBook.value;
      if (!b) { sceneData.value = null; return; }
      sceneLoading.value = !sceneData.value;   // 只有首次/换 arm 才转圈，轮询刷新不闪
      try {
        const res = await fetch(`${bookApiBase(b)}/scene`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        // 每轮轮询直接换数据：地形底图是后台异步补的，跳过赋值会让「世界」按钮一直不出现。
        sceneData.value = await res.json();
      } catch (err) {
        console.error('加载探索地图失败', err);
        sceneData.value = null;
      } finally {
        sceneLoading.value = false;
      }
    }
    function pickSceneArm(b) {
      if (bookKey(b) === sceneArmKey.value) return;
      sceneArmKey.value = bookKey(b);
      sceneData.value = null;      // 换 arm 要清掉旧数据，否则会短暂显示上一个 arm 的地图
      loadScene();
    }
    // 打开地图、或选中的 arm 卡集合变了（换卡/取消选中）时重新拉数据
    watch([sceneVisible, () => sceneBooks.value.map(bookKey).join('|')], () => {
      if (!sceneVisible.value) return;
      if (!sceneBooks.value.some(b => bookKey(b) === sceneArmKey.value)) {
        sceneArmKey.value = sceneBooks.value[0] ? bookKey(sceneBooks.value[0]) : null;
        sceneData.value = null;
      }
      loadScene();
    });

    // ── 录像面板（浮在图谱区右上角的浮层）─────────────────────────────────────
    // 结构仿探索地图那套：一次只看一条轨迹（一个副脑卡）、arm chip 切换、独立开关。
    // 已跑完的 run 看按天选集（/video 的分段 mp4）；本机正在跑的那条轨迹，**最新那一天**
    // 就是直播（/api/live 的 MJPEG，数据源就是 pollLive 拉回来的那份）—— 复用同一个按钮
    // 和同一个播放器框，没有单独的直播入口（2026-09-30 用户定案）。
    // ★ 浮层而不是分屏（2026-09-30 用户定案）：选了带录像的卡、打开开关就盖在图谱上方 ——
    //   图谱不用为它让位缩窄，关掉立刻回到全宽。地图/指标面板照旧走分屏（split 由
    //   rightPane 触发），三者互不挤占。
    const showVideo = ref(false);
    const videoArmKey = ref(null);     // 当前看哪张 arm 卡（bookKey）
    const videoData = ref(null);       // {has_video, days, ...}（/video 的返回）
    const videoLoading = ref(false);

    const videoBooks = computed(() =>
      books.value.filter(b => b.origin === 'octopus_arm' && b.has_video && isBookSelected(b)));
    const videoBook = computed(() =>
      videoBooks.value.find(b => bookKey(b) === videoArmKey.value) || videoBooks.value[0] || null);
    const videoVisible = computed(() => showVideo.value && !!videoBook.value);

    async function loadVideoIndex() {
      const b = videoBook.value;
      if (!b) { videoData.value = null; return; }
      videoLoading.value = !videoData.value;
      try {
        const res = await fetch(`${bookApiBase(b)}/video`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        videoData.value = await res.json();
      } catch (err) {
        console.error('加载录像索引失败', err);
        videoData.value = null;
      } finally {
        videoLoading.value = false;
      }
    }
    function pickVideoArm(b) {
      if (bookKey(b) === videoArmKey.value) return;
      videoArmKey.value = bookKey(b);
      videoData.value = null;
      loadVideoIndex();
    }
    watch([videoVisible, () => videoBooks.value.map(bookKey).join('|')], () => {
      if (!videoVisible.value) return;
      if (!videoBooks.value.some(b => bookKey(b) === videoArmKey.value)) {
        videoArmKey.value = videoBooks.value[0] ? bookKey(videoBooks.value[0]) : null;
        videoData.value = null;
      }
      loadVideoIndex();
    });

    // 直播记录：当前这张 arm 卡对应的 run/臂有没有正在录（liveRuns 由 pollLive 10s 维护）。
    // book 卡有 run_slug（目录名）和 arm（角色名，即 footage 目录里的 <arm> 段）。
    const videoLiveRec = computed(() => {
      const b = videoBook.value;
      if (!b) return null;
      const rec = liveRuns.value[b.run_slug];
      if (!rec) return null;
      // arm 字段在卡上是展示名（"Alice"），footage 目录是角色名（"alice"）—— 小写对齐
      return rec.arms.find(a => a.arm.toLowerCase() === (b.arm || '').toLowerCase()) || null;
    });

    // ── 量化指标面板（论文主表口径，2026-10-09 加）─────────────────────────────
    // 与地图共用右侧分屏位：地图是 SceneMap 的世界图、指标是 run × 列的表格，两者都从
    // 工具栏开关、都吃掉右半边 —— 同时开两个右侧栏会把图谱挤成一条缝，故互斥（用户定案）。
    // ★ 只统计**当前选中的 run**（2026-10-09 用户定案）：指标跟「选中卡所属的 run」走，
    //   任一选中卡的 run 变了就重拉（接口单 run ~1s，无全量构建）。选中多张卡属同一 run
    //   时取第一张的；选中卡横跨多个 run 时也只显示第一张的（一次只看一条，要看别的换卡）。
    const showMetrics = ref(false);
    const metricsData = ref(null);    // /metrics 的返回（含 has_metrics）
    const metricsLoading = ref(false);
    let metricsReqSeq = 0;            // 竞态序号：晚到的旧响应不许盖新 run 的数

    // 当前该看哪个 run 的指标：选中卡里第一张带 run_slug 的（主脑/副脑/planner-only 都带）
    const metricsBook = computed(() =>
      books.value.find(b => isBookSelected(b) && b.run_slug) || null);

    async function fetchMetrics(b) {
      const seq = ++metricsReqSeq;
      metricsLoading.value = true;
      try {
        const res = await fetch(`${bookApiBase(b)}/metrics`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        if (seq !== metricsReqSeq) return;   // 过期响应丢弃（run 已换）
        metricsData.value = data;
      } catch (err) {
        console.error('加载量化指标失败', err);
        if (seq === metricsReqSeq) metricsData.value = null;
      } finally {
        if (seq === metricsReqSeq) metricsLoading.value = false;
      }
    }
    // 面板开着 + 选中 run 变了 → 重拉；关着不动（下次打开时 watch 立即触发同值拉取）
    watch([showMetrics, () => metricsBook.value && bookKey(metricsBook.value)], ([on], [prevOn, prevKey]) => {
      if (!on) return;
      const b = metricsBook.value;
      if (!b) { metricsData.value = null; invData.value = null; return; }
      // 刚打开、或换 run 了才拉（选中同 run 的别的卡不重复拉）
      if (!prevOn || prevKey !== bookKey(b)) { fetchMetrics(b); fetchInventory(b); }
    }, { immediate: true });

    // ── 物品/成就清单（2026-10-10 加）：同一右侧分屏位内的 sub-tab ──────────────
    // 主表（MetricsPane）/ 物品 / 成就（InventoryPane 的两个视图）共用一个面板，
    // tab 状态放这里而不是组件内 —— 面板关掉再开要保持上次看的视图。
    const metricsTab = ref('main');   // 'main' | 'items' | 'advs'
    const invData = ref(null);        // /inventory 的返回（含 has_inventory）
    const invLoading = ref(false);
    let invReqSeq = 0;                // 竞态序号：与 metricsReqSeq 同款

    async function fetchInventory(b) {
      const seq = ++invReqSeq;
      invLoading.value = true;
      try {
        const res = await fetch(`${bookApiBase(b)}/inventory`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        if (seq !== invReqSeq) return;   // 过期响应丢弃（run 已换）
        invData.value = data;
      } catch (err) {
        console.error('加载物品/成就清单失败', err);
        if (seq === invReqSeq) invData.value = null;
      } finally {
        if (seq === invReqSeq) invLoading.value = false;
      }
    }

    // 右侧栏当前显示什么：地图和指标互斥，谁后开谁顶掉对方（split 由这个统一状态触发）。
    // 录像照旧是浮层不参与。sceneVisible 保持原语义（地图在显示），供回放联动等旧逻辑用。
    const rightPane = computed(() => sceneVisible.value ? 'scene' : (showMetrics.value ? 'metrics' : null));
    function toggleScene() {
      showScene.value = !showScene.value;
      if (showScene.value) showMetrics.value = false;
    }
    function toggleMetrics() {
      showMetrics.value = !showMetrics.value;
      if (showMetrics.value) showScene.value = false;
    }

    // ── 回放位置联动（VideoPane / concept 详情面板 → SceneMap）──────────────
    // 两个来源共用这一份状态：右侧录像面板按天选集（VideoPane emit），和 concept 详情
    // 面板里的 facet/技能片段、按天整段录像（onDetailVideoTime）。谁在播，标记就跟谁。
    // ★ 看录像的轨迹 ≠ 地图正在画的轨迹时**自动把地图切到同一条**：位置标记只有画在
    //   同一个 arm 的地图上才有意义（三个副脑跑在三个互不相通的世界里）。
    //   切卡的前提是那张卡也有地图（has_scene）—— 有录像没地图的 run（早期没开上下文
    //   留痕）不硬切，也不开地图。
    // ★ 地图没开时随第一次回放位置**自动打开**：用户要的就是「视频↔地图对照」，不开
    //   等于功能隐形。用户主动关掉地图后，同一轮回放里 timeupdate 还会再来位置——
    //   仍会重开；要看视频不看地图就先暂停回放（暂停后标记停在原处，不再推新位置）。
    // 两个来源各自的「别在播了」收尾：VideoPane 关面板（下方 watch videoVisible）、
    // 详情面板收片段/换概念（上方两个 watch）。
    const replayPos = ref(null);
    function onReplayPos(p, from) {
      // 单播放器仲裁（见 replayOwner 定义处）：标记归详情面板时，录像面板的
      // replay-pos 全部丢弃 —— 不丢就两边来回跳。from='pane' 只来自 VideoPane。
      if (from === 'pane' && replayOwner.value === 'detail') return;
      replayPos.value = p;
      if (!p) return;
      // 目标轨迹：详情面板优先（video 是 loadVideo(selectedNode) 拉的、与选中概念同源），
      // 否则右侧录像面板正在看的那张卡。都没有 → 位置无处可画，只更新标记数据。
      const node = selectedDetail.value && video.value ? selectedNode.value : null;
      const targetKey = node ? node.bookKey : (videoVisible.value ? bookKey(videoBook.value) : null);
      if (!targetKey) return;
      const target = sceneBooks.value.find(b => bookKey(b) === targetKey);
      if (!target) return;
      const sb = sceneBook.value;
      if (!sb || bookKey(sb) !== bookKey(target)) {
        sceneArmKey.value = bookKey(target);
        sceneData.value = null;      // 换 arm 清旧图；watch(sceneVisible…) 会触发 loadScene
      }
      // 自动开地图（回放联动）：同样要走互斥 —— 指标面板正占着右侧栏时，直接置
      // showScene 会让两个右侧栏同时亮。统一走 toggleScene 的互斥语义。
      if (!showScene.value) toggleScene();
    }
    // 关掉录像面板 → 清标记 + 释放标记所有权（详情面板的下一个 timeupdate 可重新接管）。
    // 详情面板那边：openClip/selectedDetail 的 watch 已各自清，但「面板还开着、片段还展着、
    // 只是暂停了」时标记停在最后位置 —— 这是有意的（暂停 ≠ 不看了）；真不想要就收起片段
    // 或关面板。
    watch(videoVisible, () => {
      if (!videoVisible.value) {
        replayPos.value = null;
        if (replayOwner.value === 'pane') replayOwner.value = null;
      }
    });

    // 地图 → 图谱：点地图上的地点，选中图谱里的同一个概念并移到视口中心。
    // 高亮不用新写逻辑 —— nodeGroupOpacity/edgeOpacity 已经会把非邻居压暗、只显示相连边。
    function onPickPlace(slug) {
      const b = sceneBook.value;
      if (!b) return;
      const node = getNode(bookKey(b) + '::' + slug);
      if (!node) return;   // 图谱侧还没加载到这个节点（正在轮询刷新），下次点就有了
      selectNode(node);
      centerOnNode(node);
    }
    // 图谱 → 地图：选中的节点属于别的 arm 卡时自动切过去，地图侧据 highlightSlug 打脉冲环。
    // 不在地图上的（经验层/无坐标/框外）由 SceneMap 显示一行说明，不静默无反应。
    const sceneHighlightSlug = computed(() => {
      const n = selectedNode.value;
      if (!n || !sceneBook.value) return null;
      return n.bookKey === bookKey(sceneBook.value) ? n.slug : null;
    });
    watch(selectedNode, (n) => {
      if (!n || !sceneVisible.value) return;
      if (n.origin === 'octopus_arm' && n.bookKey !== sceneArmKey.value
          && sceneBooks.value.some(b => bookKey(b) === n.bookKey)) {
        sceneArmKey.value = n.bookKey;
        sceneData.value = null;
        loadScene();
      }
    });

    // 分隔条拖拽：只改百分比，不动两侧内部的缩放/平移状态。
    // 地图在**右**边，所以宽度是"从鼠标到右边缘"的距离 —— 往右拖 = 地图变窄。
    let splitDragging = false;
    function onSplitDown() { splitDragging = true; }
    function onSplitMove(e) {
      if (!splitDragging) return;
      const host = document.querySelector('.kg-body');
      if (!host) return;
      const rect = host.getBoundingClientRect();
      splitPct.value = Math.min(72, Math.max(24, ((rect.right - e.clientX) / rect.width) * 100));
    }
    function onSplitUp() { splitDragging = false; }
    onMounted(() => {
      window.addEventListener('mousemove', onSplitMove);
      window.addEventListener('mouseup', onSplitUp);
    });
    onUnmounted(() => {
      window.removeEventListener('mousemove', onSplitMove);
      window.removeEventListener('mouseup', onSplitUp);
    });

    // 主题切换：localStorage 持久化
    const isDark = ref(localStorage.getItem('openkage-viewer-theme') !== 'light');
    function toggleTheme() { isDark.value = !isDark.value; }
    watch(isDark, (dark) => {
      document.documentElement.setAttribute('data-theme', dark ? 'dark' : 'light');
      localStorage.setItem('openkage-viewer-theme', dark ? 'dark' : 'light');
    }, { immediate: true });

    onMounted(loadBooks);
    onMounted(startBooksPoll);
    onUnmounted(stopBooksPoll);

    return {
      books, booksLoading, selectedKeys, canvasWrap, svgW, svgH, zoom, pan,
      loadingGraph, nodes, edges, clusters, clusterShapes, stats, searchTerm, searchFocused, selectedNode, selectedDetail,
      toggleBook, isBookSelected, resetView, loadGraph, getNode, isOnEdge, isNeighbor, displayedEdges,
      nodeGroupOpacity, edgeOpacity, edgeStrokeWidth,
      onSvgMousedown, onNodeMousedown, onSvgMousemove, onSvgMouseup, onWheel,
      selectNode, selectByPeer, closeDetail, formatPageRanges, searchAndSelect, copySkillCode, skillCopied,
      footage, footageLoading, footageZoom, footageLabel, footageDesc,
      video, fmtDur, facetClip, toggleClip, clipOpen, clipSrc, relevantDays,
      skillClip, toggleSkillClip, skillClipOpen,
      getNodeColor, getNodeBg, getNodeRadius, getDegreeColor, getDegreeArc, getNodeIcon, hexPoints, truncate,
      getChangeRingColor, getNodeDash, getNodeOpacity, clearedCount,
      relationColorMap, activeRelationTypes, isDark, toggleTheme,
      bookKey, oneLineBookName, getBookColor,
      renderMd,
      renderInline,
      conceptCountLabel,
      isOctopusBook, octopusPillLabel,
      drill, shelfGroups,
      enterGroup, currentRuns, currentRunCards, enterRun, crumbs,
      liveRuns, liveInfo,
      scaleLabel,
      showScene, sceneVisible, sceneBooks, sceneBook, sceneData, sceneLoading,
      selectedPlannerOnly,
      sceneArmKey, sceneHighlightSlug, splitPct, pickSceneArm, onPickPlace, onSplitDown,
      showVideo, videoVisible, videoBooks, videoBook, videoData, videoLoading,
      replayPos, onReplayPos, onDetailVideoTime,
      videoLiveRec, pickVideoArm,
      showMetrics, metricsData, metricsLoading, rightPane, toggleScene, toggleMetrics,
      metricsTab, invData, invLoading,
    };
  },
};
</script>

<template>
  <div class="app">
    <header class="topbar">
      <div class="topbar-left">
        <div class="logo">🕸️ OpenKAGE 知识图谱</div>
        <div class="subtitle">可视化</div>
      </div>
      <div class="topbar-right">
        <button class="btn-icon" :title="isDark ? '切换到浅色' : '切换到深色'" @click="toggleTheme">
          <svg v-if="isDark" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <circle cx="12" cy="12" r="5" />
            <line x1="12" y1="1" x2="12" y2="3" /><line x1="12" y1="21" x2="12" y2="23" />
            <line x1="4.22" y1="4.22" x2="5.64" y2="5.64" /><line x1="18.36" y1="18.36" x2="19.78" y2="19.78" />
            <line x1="1" y1="12" x2="3" y2="12" /><line x1="21" y1="12" x2="23" y2="12" />
            <line x1="4.22" y1="19.78" x2="5.64" y2="18.36" /><line x1="18.36" y1="5.64" x2="19.78" y2="4.22" />
          </svg>
          <svg v-else width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M21 12.79A9 9 0 1111.21 3 7 7 0 0021 12.79z" />
          </svg>
        </button>
      </div>
    </header>

    <div class="shelf">
      <div v-if="booksLoading" class="empty-state">正在扫描图谱…（多根扫描较慢，首次约需几十秒）</div>
      <div v-else-if="!books.length" class="empty-state">还没有任何 run 卡（results/ 下没有 cell/runNN 目录）</div>
      <template v-else>
        <div class="shelf-main">
          <!-- 面包屑：根「全部图谱」常驻 -->
          <div class="shelf-crumbs">
            <button class="crumb" :class="{root: !drill}" @click="drill = null">全部图谱</button>
            <button v-for="(c, i) in crumbs" :key="i" class="crumb" @click="c.back()">
              <span class="crumb-sep">›</span>{{ c.label }}
            </button>
          </div>

          <!-- 第一层：实验 × 模型 组合卡（文件夹） -->
          <div v-if="!drill" class="shelf-grid">
            <div v-for="g in shelfGroups" :key="g.major + '/' + g.minor" class="group-card" @click="enterGroup(g)">
              <div class="group-card-top">
                <span class="gc-folder">📁</span>
                <span class="gc-count">{{ g.runs.length }} run</span>
              </div>
              <div class="gc-major">{{ g.major }}</div>
              <div class="gc-minor">{{ g.minor }}</div>
            </div>
          </div>

          <!-- 第二层：run 卡（文件夹，点开第三层）。
               当前路径由上面的面包屑表达，这里不再重复一遍。全选按钮已整层撤掉。 -->
          <template v-else-if="!drill.run">
            <div class="shelf-grid">
              <div v-for="r in currentRuns" :key="r.run_slug" class="group-card run-card" @click="enterRun(r)">
                <div class="group-card-top">
                  <span class="gc-folder">📁</span>
                  <span class="gc-count">{{ r.books.length }} 图</span>
                </div>
                <div class="gc-major">{{ r.first.run_ts }}</div>
                <div class="gc-minor-row">
                  <span class="gh-cond">{{ r.first.condition_label }}</span>
                  <span v-if="scaleLabel(r.first)" class="gh-scale">{{ scaleLabel(r.first) }}</span>
                  <span v-if="liveRuns[r.run_slug]" class="gh-live">🔴 直播 第{{ liveRuns[r.run_slug].day }}天</span>
                </div>
                <div class="gc-meta">
                  <span class="gc-host">{{ r.first.host }}</span>
                  <!-- 来源根 = 机器目录，通常与 run 目录名里的 host 段一致；不一致（人工搬过目录）才另标一个 -->
                  <span v-if="r.first.root_label && r.first.root_label !== r.first.host" class="gh-root">{{ r.first.root_label }}</span>
                </div>
              </div>
            </div>
          </template>

          <!-- 第三层：该 run 的图谱卡（共享图谱 / 私有图谱…，点选进画布）。
               全选按钮已去掉（第二、三层都撤了），选图谱一律点单卡 toggleBook。 -->
          <template v-else>
            <div class="shelf-grid">
              <div v-for="b in currentRunCards" :key="b.book_slug"
                   class="book-card" :class="{active: isBookSelected(b)}"
                   @click="toggleBook(b)">
                <div class="book-card-main">
                  <div class="book-card-top-row">
                    <div class="book-card-pills">
                      <span class="subject-pill">{{ b.subject }}</span>
                      <span class="origin-pill" :class="'origin-' + b.origin">
                        {{ octopusPillLabel(b) }}
                      </span>
                    </div>
                  </div>
                  <div class="book-name">{{ b.book }}</div>
                  <div class="book-stats">
                    <!-- planner-only 卡无图谱（concept_count 为 null）：计数位换成轨迹语义，
                         别显示一个空的「概念 0」让人以为是数据丢了 -->
                    <span v-if="b.kind === 'planner_only'">无图谱</span>
                    <span v-else>{{ conceptCountLabel(b) }} <b>{{ b.concept_count }}</b></span>
                    <span v-if="b.has_scene">有探索地图</span>
                  </div>
                </div>
              </div>
            </div>
          </template>
        </div>
      </template>
    </div>
    <template v-if="selectedKeys.size > 0">
      <div class="graph-toolbar">
        <div class="graph-toolbar-left">
          <span class="graph-toolbar-title">图谱 · 已选 {{ selectedKeys.size }} 张</span>
          <span v-if="stats" class="graph-toolbar-count">{{ stats.concept_count }} 个概念 · {{ stats.edge_count }} 条关系<span v-if="stats.skill_count"> · {{ stats.skill_count }} 个技能</span></span>
        </div>
        <div class="graph-toolbar-right">
          <!-- 地图开关只在选中了带轨迹的私有图谱卡时出现：地理坐标只存在于副脑记忆层 -->
          <button v-if="sceneBooks.length" class="ctrl-btn btn-scene" :class="{on: showScene}"
                  :title="showScene ? '关闭探索地图' : '打开探索地图'"
                  @click="toggleScene">
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <polygon points="1 6 8 3 16 6 23 3 23 18 16 21 8 18 1 21" />
              <line x1="8" y1="3" x2="8" y2="18" /><line x1="16" y1="6" x2="16" y2="21" />
            </svg>
          </button>
          <!-- 量化指标开关（论文主表口径，全 run 一张表）。与地图互斥：谁后开谁顶掉对方 ——
               两者都是右侧分屏，同屏开两个会把图谱挤没。 -->
          <button class="ctrl-btn btn-metrics" :class="{on: showMetrics}"
                  :title="showMetrics ? '关闭量化指标' : '打开量化指标'"
                  @click="toggleMetrics">
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <line x1="4" y1="20" x2="20" y2="20" /><line x1="7" y1="16" x2="7" y2="12" />
              <line x1="12" y1="16" x2="12" y2="6" /><line x1="17" y1="16" x2="17" y2="9" />
            </svg>
          </button>
          <!-- 录像开关：同一批选中卡里有带录像的（steps.jsonl 在）才亮。按天选集回放；
               正在跑的那条轨迹，最新一天那个按钮就是直播（VideoPane 里按 liveRec 自己判）。 -->
          <button v-if="videoBooks.length" class="ctrl-btn btn-video" :class="{on: showVideo}"
                  :title="showVideo ? '关闭录像' : '打开录像'"
                  @click="showVideo = !showVideo">
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <polygon points="23 7 16 12 23 17 23 7" /><rect x="1" y="5" width="15" height="14" rx="2" ry="2" />
            </svg>
          </button>
          <div class="search-bar" :class="{focused: searchFocused}">
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <circle cx="11" cy="11" r="8" /><line x1="21" y1="21" x2="16.65" y2="16.65" />
            </svg>
            <input v-model="searchTerm" placeholder="搜索概念名/slug..." @focus="searchFocused=true" @blur="searchFocused=false" @keydown.enter="searchAndSelect" />
          </div>
          <button class="ctrl-btn" title="重新加载" @click="loadGraph">
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <path d="M3 12a9 9 0 109-9 9.75 9.75 0 00-6.74 2.74L3 8" /><path d="M3 3v5h5" />
            </svg>
          </button>
          <button class="ctrl-btn" title="重置视图" @click="resetView">
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <path d="M21 12a9 9 0 01-9 9c-2.52 0-4.93-1-6.74-2.74L3 16" /><path d="M21 21v-5h-5" />
            </svg>
          </button>
          <span class="zoom-val">{{ Math.round(zoom * 100) }}%</span>
        </div>
      </div>

      <!-- split 由右侧栏触发（地图或指标面板，两者互斥二选一）；录像仍是浮层，不参与分屏 -->
      <div class="kg-body" :class="{split: rightPane}"
           :style="rightPane ? {'--split-pct': splitPct + '%'} : null">
        <div v-if="loadingGraph" class="kg-loading">
          <div class="kg-spinner" /><span>加载图谱中...</span>
        </div>
        <div v-else-if="nodes.length === 0" class="kg-empty">
          <template v-if="selectedPlannerOnly">
            <div class="ke-icon">🗺️</div>
            <div class="ke-title">planner-only（B 无图）没有知识图谱</div>
            <div class="ke-desc">这条轨迹只有探索地图可看——点上方工具栏的「地图」开关</div>
          </template>
          <template v-else>
            <div class="ke-icon">🕸️</div>
            <div class="ke-title">所选图谱里还没有概念</div>
            <div class="ke-desc">换一张有内容的图谱</div>
          </template>
        </div>

        <div v-else ref="canvasWrap" class="graph-canvas-wrap"
             @mousedown="onSvgMousedown" @mousemove="onSvgMousemove" @mouseup="onSvgMouseup" @mouseleave="onSvgMouseup" @wheel.prevent="onWheel">
          <svg class="graph-svg" :viewBox="'0 0 ' + svgW + ' ' + svgH">
            <g :transform="'translate(' + pan.x + ',' + pan.y + ') scale(' + zoom + ')'">
              <transition-group tag="g" name="cluster-anim" class="clusters-layer">
                <ellipse v-for="shape in clusterShapes" :key="shape.id"
                         :cx="shape.cx" :cy="shape.cy" :rx="shape.rx" :ry="shape.ry"
                         fill="rgba(250, 204, 21, 0.06)" stroke="#facc15" stroke-width="1.5"
                         stroke-dasharray="6,4" />
              </transition-group>
              <g class="edges-layer">
                <transition-group tag="g" name="edge-anim">
                  <line v-for="edge in displayedEdges" :key="edge.id"
                        :x1="getNode(edge.src)?.x" :y1="getNode(edge.src)?.y"
                        :x2="getNode(edge.dst)?.x" :y2="getNode(edge.dst)?.y"
                        :stroke="relationColorMap[edge.relation] || '#888'"
                        :stroke-width="edgeStrokeWidth(edge)"
                        :stroke-dasharray="edge.cleared ? '5,3' : (edge.relation === 'related' || edge.status === 'candidate' ? '4,3' : 'none')"
                        :opacity="edgeOpacity(edge)" />
                </transition-group>
              </g>

              <transition-group tag="g" name="node-anim">
                <g v-for="node in nodes" :key="node.id" :transform="'translate(' + node.x + ',' + node.y + ')'"
                   class="node-group" :class="{selected: selectedNode?.id===node.id, 'node-draft': node.stage === 'draft', 'node-skill': node.kind === 'skill'}"
                   :style="{opacity: nodeGroupOpacity(node)}"
                   @mousedown.stop="onNodeMousedown($event, node)" @click.stop="selectNode(node)">
                  <circle v-if="selectedNode?.id===node.id" :r="getNodeRadius(node)+12" :fill="getNodeColor(node)" opacity="0.12" />
                  <circle :r="getNodeRadius(node)+4" fill="none" stroke="var(--bg-hover)" stroke-width="3" />
                  <!-- degree 弧只对 concept 有意义（技能是 0 度孤岛），技能节点跳过 -->
                  <circle v-if="node.kind !== 'skill'" :r="getNodeRadius(node)+4" fill="none" :stroke="getDegreeColor(node)" stroke-width="3" stroke-linecap="round"
                          :stroke-dasharray="getDegreeArc(node) + ' 999'" transform="rotate(-90)" opacity="0.85" />
                  <!-- 主体：concept=圆，skill=六边形（形状区分，不靠颜色） -->
                  <circle v-if="node.kind !== 'skill'" :r="getNodeRadius(node)" :fill="getNodeBg(node)" :stroke="getNodeColor(node)"
                          stroke-width="1.5" :stroke-dasharray="getNodeDash(node)"
                          :opacity="getNodeOpacity(node)" />
                  <polygon v-else :points="hexPoints(node)" :fill="getNodeBg(node)" :stroke="getNodeColor(node)"
                           stroke-width="1.5" :stroke-dasharray="getNodeDash(node)"
                           :opacity="getNodeOpacity(node)" />
                  <text text-anchor="middle" dominant-baseline="central" :font-size="getNodeRadius(node)*0.85" :fill="getNodeColor(node)"
                        font-weight="700" style="user-select:none;pointer-events:none">{{ getNodeIcon(node) }}</text>
                  <!-- 变化态角标：纯矢量线条画图形（不用文字字形）——字体的"+"/"~"天生不是视觉居中的，
                       换成几何图形从坐标原点对称画，居中是坐标算出来的，不是字体量出来的。
                       新增=十字加号（4 臂），修改=六角小星（3 条线 6 臂），形状本身就能区分，不用色相。 -->
                  <g v-if="node.change_status" :transform="'translate(' + (getNodeRadius(node)*0.75) + ',' + (-getNodeRadius(node)*0.75) + ')'"
                     style="pointer-events:none">
                    <circle r="9" :fill="getChangeRingColor(node)" stroke="var(--bg-hover)" stroke-width="2" />
                    <g v-if="node.change_status === 'new'" stroke="#fff" stroke-width="2" stroke-linecap="round">
                      <line x1="-3.5" y1="0" x2="3.5" y2="0" />
                      <line x1="0" y1="-3.5" x2="0" y2="3.5" />
                    </g>
                    <g v-else stroke="#fff" stroke-width="1.6" stroke-linecap="round">
                      <line x1="-3.5" y1="0" x2="3.5" y2="0" />
                      <line x1="-3.5" y1="0" x2="3.5" y2="0" transform="rotate(60)" />
                      <line x1="-3.5" y1="0" x2="3.5" y2="0" transform="rotate(120)" />
                    </g>
                  </g>
                  <text text-anchor="middle" :y="getNodeRadius(node)+16" font-size="11" fill="var(--text-secondary)"
                        style="user-select:none;pointer-events:none" :font-weight="selectedNode?.id===node.id ? '600' : '400'">{{ truncate(node.title, 12) }}</text>
                </g>
              </transition-group>
            </g>
          </svg>

          <div class="legend legend-books" v-if="books.filter(isBookSelected).length > 1">
            <div class="legend-title">图谱</div>
            <div class="legend-scroll">
              <div v-for="b in books.filter(isBookSelected)" :key="bookKey(b)" class="legend-item">
                <span class="legend-dot" :style="{background: getBookColor(bookKey(b))}"></span><span>{{ oneLineBookName(b) }}</span>
              </div>
            </div>
          </div>
          <div class="legend" v-if="activeRelationTypes.length">
            <div class="legend-title">关系类型</div>
            <div class="legend-scroll">
              <div v-for="rel in activeRelationTypes" :key="rel" class="legend-item">
                <span class="legend-line" :style="{background: relationColorMap[rel] || '#888'}"></span><span>{{ rel }}</span>
              </div>
            </div>
          </div>
          <!-- 已被聚合进共享图谱的经验层：只有私有图谱卡会有（后端 stats.cleared_count） -->
          <div class="legend legend-cleared" v-if="clearedCount">
            <div class="legend-title">经验层</div>
            <div class="legend-item">
              <svg class="legend-dot" width="16" height="16" viewBox="-8 -8 16 16">
                <circle r="7" fill="none" stroke="#94a3b8" stroke-width="1.5"
                        stroke-dasharray="5,3" opacity="0.6" />
              </svg>
              <span>已聚合进共享图谱 {{ clearedCount }} 个</span>
            </div>
          </div>
          <div class="legend legend-changes" v-if="nodes.some(n => n.change_status)">
            <div class="legend-title">本次训练变化</div>
            <div class="legend-item">
              <svg class="legend-dot" width="16" height="16" viewBox="-8 -8 16 16">
                <circle r="8" fill="#22c55e" />
                <g stroke="#fff" stroke-width="1.8" stroke-linecap="round">
                  <line x1="-3.5" y1="0" x2="3.5" y2="0" /><line x1="0" y1="-3.5" x2="0" y2="3.5" />
                </g>
              </svg>
              <span>新增节点</span>
            </div>
            <div class="legend-item">
              <svg class="legend-dot" width="16" height="16" viewBox="-8 -8 16 16">
                <circle r="8" fill="#f59e0b" />
                <g stroke="#fff" stroke-width="1.5" stroke-linecap="round">
                  <line x1="-3.5" y1="0" x2="3.5" y2="0" />
                  <line x1="-3.5" y1="0" x2="3.5" y2="0" transform="rotate(60)" />
                  <line x1="-3.5" y1="0" x2="3.5" y2="0" transform="rotate(120)" />
                </g>
              </svg>
              <span>修改节点</span>
            </div>
          </div>
        </div>

        <!-- 右侧分屏（地图与指标互斥，二选一）：放在图谱之后 = 视觉上在右边（.kg-body.split 是 flex 行） -->
        <template v-if="rightPane === 'scene'">
          <div class="split-handle" @mousedown.prevent="onSplitDown"
               title="拖动调整图谱与地图的比例"></div>
          <aside class="scene-pane" :style="{width: splitPct + '%'}">
            <div v-if="sceneBooks.length > 1" class="scene-arms">
              <button v-for="b in sceneBooks" :key="bookKey(b)" class="scene-arm-chip"
                      :class="{on: sceneBook && bookKey(b) === bookKey(sceneBook)}"
                      :style="{borderColor: sceneBook && bookKey(b) === bookKey(sceneBook) ? getBookColor(bookKey(b)) : ''}"
                      @click="pickSceneArm(b)">
                <span class="legend-dot" :style="{background: getBookColor(bookKey(b))}"></span>
                {{ oneLineBookName(b) }}
              </button>
            </div>
            <SceneMap :scene="sceneData" :loading="sceneLoading"
                      :highlight-slug="sceneHighlightSlug" :replay-pos="replayPos"
                      @pick-place="onPickPlace" />
          </aside>
        </template>
        <template v-else-if="rightPane === 'metrics'">
          <div class="split-handle" @mousedown.prevent="onSplitDown"
               title="拖动调整图谱与指标面板的比例"></div>
          <aside class="scene-pane metrics-pane" :style="{width: splitPct + '%'}">
            <!-- sub-tab：主表 / 物品 / 成就 三个视图共用这个右侧分屏位（互斥同 rightPane 理由） -->
            <div class="mp-tabs">
              <button class="mp-tab" :class="{on: metricsTab === 'main'}" @click="metricsTab = 'main'">主表指标</button>
              <button class="mp-tab" :class="{on: metricsTab === 'items'}" @click="metricsTab = 'items'">物品清单</button>
              <button class="mp-tab" :class="{on: metricsTab === 'advs'}" @click="metricsTab = 'advs'">成就清单</button>
            </div>
            <MetricsPane v-show="metricsTab === 'main'" :data="metricsData" :loading="metricsLoading" />
            <InventoryPane v-if="metricsTab !== 'main'" :data="invData" :loading="invLoading" :tab="metricsTab" />
          </aside>
        </template>

        <!-- 录像：浮在图谱上方（不与地图分屏抢行），打开就盖在图谱区右上，关掉即回到图谱
             全宽/原图。z-index 在详情面板之下（详情是全局浮层，应该压过一切）。 -->
        <transition name="slide">
          <aside v-if="videoVisible" class="video-pane">
            <div v-if="videoBooks.length > 1" class="scene-arms">
              <button v-for="b in videoBooks" :key="bookKey(b)" class="scene-arm-chip"
                      :class="{on: videoBook && bookKey(b) === bookKey(videoBook)}"
                      :style="{borderColor: videoBook && bookKey(b) === bookKey(videoBook) ? getBookColor(bookKey(b)) : ''}"
                      @click="pickVideoArm(b)">
                <span class="legend-dot" :style="{background: getBookColor(bookKey(b))}"></span>
                {{ oneLineBookName(b) }}
              </button>
            </div>
            <VideoPane :video="videoData" :loading="videoLoading" :live-rec="videoLiveRec"
                       :run-slug="videoBook ? videoBook.run_slug : ''"
                       :arm-key="videoLiveRec ? videoLiveRec.arm : ''"
                       :arm-label="videoBook ? videoBook.book : ''"
                       @replay-pos="p => onReplayPos(p, 'pane')" />
          </aside>
        </transition>

        <transition name="slide">
          <aside v-if="selectedDetail" class="detail-panel">
            <header class="detail-head">
              <div>
                <div class="detail-title">{{ selectedDetail.title }}</div>
                <div class="detail-meta">
                  <span class="badge subject-badge">{{ selectedDetail.subject || '—' }}</span>
                  <template v-if="selectedDetail.kind === 'skill'">
                    <span class="badge badge-skill">skill</span>
                    <span v-if="selectedDetail.verified" class="badge" style="color:#22c55e">✓ 已验证</span>
                    <span v-else class="badge" style="color:#94a3b8">未验证</span>
                    <!-- 与经验层同机制：聚合把这条技能并进共享图谱后从私有图谱清掉，详情来自窗口快照 -->
                    <span v-if="selectedDetail.cleared" class="badge badge-cleared"
                          title="聚合把这条技能并进共享图谱后，就把它从私有图谱清掉了。这里显示的内容来自清理前的窗口快照。">
                      已聚合 · 第{{ selectedDetail.cleared_window }}个窗口被清
                    </span>
                  </template>
                  <template v-else>
                    <span class="badge">{{ selectedDetail.chapter }}</span>
                    <!-- 这条经验已聚合进共享图谱、并从私有图谱硬删；正文来自那个窗口的快照 -->
                    <span v-if="selectedDetail.cleared" class="badge badge-cleared"
                          title="聚合把这条经验并进共享图谱后，就把它从私有图谱的经验层删掉了。这里显示的正文来自清理前的窗口快照。">
                      已聚合 · 第{{ selectedDetail.cleared_window }}个窗口被清
                    </span>
                  </template>
                </div>
                <div class="detail-slug">{{ selectedDetail.id }}</div>
                <div v-if="selectedDetail.source_sections?.length" class="detail-source">
                  {{ selectedDetail.source_sections.join('\n') }}
                </div>
              </div>
              <button class="ctrl-btn" @click="closeDetail">
                <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                  <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
                </svg>
              </button>
            </header>

            <!-- 技能详情：意图 / 签名 / 前置 / 实战计数 / 产自哪几步的录像 / JS 代码。
                 skill 是独立表（与 concept 无边），整套另起；录像入口照搬 facet 那套
                 （键换成 slug，见 skillClip / toggleSkillClip）。 -->
            <template v-if="selectedDetail.kind === 'skill'">
              <div v-if="selectedDetail.intent" class="section">
                <div class="section-title">何时该用</div>
                <div class="section-body md-body">{{ selectedDetail.intent }}</div>
              </div>
              <div class="section">
                <div class="section-title">调用签名</div>
                <div class="section-body"><code class="skill-sig">{{ selectedDetail.signature }}</code></div>
              </div>
              <div v-if="selectedDetail.preconditions" class="section">
                <div class="section-title">前置条件</div>
                <div class="section-body md-body">{{ selectedDetail.preconditions }}</div>
              </div>
              <div class="section">
                <div class="section-title">实战计数</div>
                <div class="section-body skill-counts">
                  <!-- 两行四数：第一行=当前代码版本窗口（update_skill 改 js_code 时归零），
                       第二行=全程累计永不清零。后者来自后端 total_success/total_fail_count。
                       老数据/老 run 没这俩字段 → ?? 0 兜底，不会显示成 undefined。 -->
                  <div class="skill-counts-row">
                    <span class="skill-counts-label">当前版本</span>
                    <span class="skill-ok">✓ 成功 {{ selectedDetail.success_count ?? 0 }}</span>
                    <span class="skill-fail">✗ 失败 {{ selectedDetail.fail_count ?? 0 }}</span>
                  </div>
                  <div class="skill-counts-row">
                    <span class="skill-counts-label">全程历史</span>
                    <span class="skill-ok">✓ 成功 {{ selectedDetail.total_success_count ?? 0 }}</span>
                    <span class="skill-fail">✗ 失败 {{ selectedDetail.total_fail_count ?? 0 }}</span>
                  </div>
                </div>
              </div>
              <!-- 技能产自哪几步的录像（反思自报 source_steps，落 skill_steps.jsonl，与 facet 同构）。
                   只在 create_skill 时落一次；点开才插 <video>，同 facet。 -->
              <div v-if="skillClip(selectedDetail.slug)" class="section">
                <div class="section-title">产自哪几步
                  <span v-if="!skillClip(selectedDetail.slug).unresolved" class="facet-clip"
                        @click="toggleSkillClip(selectedDetail.slug)"
                        :title="'第' + skillClip(selectedDetail.slug).day + '天第 ' + skillClip(selectedDetail.slug).steps.join('、') + ' 步的录像'">
                    {{ skillClipOpen(selectedDetail.slug) ? '▾ 收起录像' : '▶ 看这' + skillClip(selectedDetail.slug).steps.length + '步' }}
                  </span>
                  <span v-else class="facet-clip-bad"
                        :title="'反思自报第' + skillClip(selectedDetail.slug).day + '天第 ' + (skillClip(selectedDetail.slug).claimed_steps || []).join('、') + ' 步，但那一天的索引里没有这些步 —— 无法定位画面'">
                    ⚠ 来源步对不上
                  </span>
                </div>
                <div v-if="skillClipOpen(selectedDetail.slug)" class="facet-video">
                  <video class="octopus-video" :src="clipSrc(skillClip(selectedDetail.slug))"
                         controls autoplay preload="metadata"
                         @timeupdate="onDetailVideoTime($event, skillClip(selectedDetail.slug).day)"
                         @pause="onDetailVideoTime($event, skillClip(selectedDetail.slug).day)"></video>
                  <div class="facet-video-cap">
                    第{{ skillClip(selectedDetail.slug).day }}天 ·
                    第 {{ skillClip(selectedDetail.slug).steps.join('、') }} 步 ·
                    {{ fmtDur(skillClip(selectedDetail.slug).range[0]) }} → {{ fmtDur(skillClip(selectedDetail.slug).range[1]) }}
                    <span v-if="skillClip(selectedDetail.slug).dropped_steps?.length" class="clip-hint">
                      · 另有第 {{ skillClip(selectedDetail.slug).dropped_steps.join('、') }} 步对不上索引，已略去
                    </span>
                  </div>
                </div>
              </div>
              <div v-if="selectedDetail.js_code" class="section">
                <div class="section-title">
                  JS 代码
                  <button class="skill-copy" @click="copySkillCode" :title="skillCopied ? '已复制' : '复制代码'">
                    {{ skillCopied ? '✓ 已复制' : '复制' }}
                  </button>
                </div>
                <pre class="skill-code"><code>{{ selectedDetail.js_code }}</code></pre>
              </div>
            </template>

            <template v-else>
            <div v-if="selectedDetail.summary" class="section">
              <div class="section-title">概述</div>
              <div class="section-body md-body" v-html="renderMd(selectedDetail.summary)"></div>
            </div>

            <div v-if="selectedDetail.aliases?.length" class="section">
              <div class="section-title">别名</div>
              <div class="section-body"><span v-for="a in selectedDetail.aliases" :key="a" class="alias-pill" v-html="renderInline(a)"></span></div>
            </div>

            <div v-if="selectedDetail.facets?.length" class="section">
              <div class="section-title">Facets（{{ selectedDetail.facets.length }}）</div>
              <div class="section-body">
                <div v-for="(f, idx) in selectedDetail.facets" :key="idx" class="facet-card">
                  <div class="facet-head">
                    <span class="facet-kind">{{ f.kind }}</span>
                    <span v-if="f.change_status" class="badge" :style="{color: f.change_status === 'new' ? '#22c55e' : '#f59e0b'}">
                      {{ f.change_status === 'new' ? '本次新增' : '本次强化' }}
                    </span>
                    <span v-if="f.pages?.length" class="detail-source">第{{ formatPageRanges(f.pages) }}页</span>
                    <!-- 反思 Agent 自报的来源步 → 那几秒录像。没报来源步就没这个按钮。
                         ★ 点开才插 <video>：一条轨迹几十条 facet，全渲染会让浏览器同时开
                         几十个连接拉 preload=metadata。 -->
                    <span v-if="facetClip(f) && !facetClip(f).unresolved" class="facet-clip" @click="toggleClip(f)"
                          :title="'第' + facetClip(f).day + '天第 ' + facetClip(f).steps.join('、') + ' 步的录像'">
                      {{ clipOpen(f) ? '▾ 收起录像' : '▶ 看这' + facetClip(f).steps.length + '步' }}
                    </span>
                    <!-- 来源步**一个都对不上**：如实说出来，而不是什么都不显示。
                         静默会让「这条没画面」与「模型报的步号是幻觉」不可区分，
                         而后者是该被看见的模型质量信号（见 server.py 的 unresolved 注释）。 -->
                    <span v-else-if="facetClip(f) && facetClip(f).unresolved" class="facet-clip-bad"
                          :title="'反思自报第' + facetClip(f).day + '天第 ' + (facetClip(f).claimed_steps || []).join('、') + ' 步，但那一天的索引里没有这些步 —— 无法定位画面'">
                      ⚠ 来源步对不上
                    </span>
                  </div>
                  <div class="facet-content md-body" v-html="renderMd(f.content)"></div>
                  <!-- 录像**挂在这条 facet 的正文下面**：它是这句话的证据，不是"这一天"的附录 -->
                  <div v-if="clipOpen(f)" class="facet-video">
                    <video class="octopus-video" :src="clipSrc(facetClip(f))"
                           controls autoplay preload="metadata"
                           @timeupdate="onDetailVideoTime($event, facetClip(f).day)"
                           @pause="onDetailVideoTime($event, facetClip(f).day)"></video>
                    <div class="facet-video-cap">
                      第{{ facetClip(f).day }}天 ·
                      第 {{ facetClip(f).steps.join('、') }} 步 ·
                      {{ fmtDur(facetClip(f).range[0]) }} → {{ fmtDur(facetClip(f).range[1]) }}
                      <!-- 部分步号无效：能放，但要让人知道有几个被丢了 -->
                      <span v-if="facetClip(f).dropped_steps?.length" class="clip-hint">
                        · 另有第 {{ facetClip(f).dropped_steps.join('、') }} 步对不上索引，已略去
                      </span>
                    </div>
                  </div>
                </div>
              </div>
            </div>

            <!-- **只挂与本概念相关的那几天**的完整录像（见 relevantDays 的注释：整条轨迹
                 30 天全挂上去，等于每个概念下面都是 30 个几十 MB 的视频）。
                 与上面每条 facet 的片段是两个用途：这里是"完整地看那一天"，那里是"这句话的证据"。 -->
            <div v-if="relevantDays.length" class="section">
              <div class="section-title">
                <span>相关录像（{{ relevantDays.length }} 天<span v-if="video.days.length > relevantDays.length"
                  class="clip-hint"> / 全程 {{ video.days.length }} 天</span>）</span>
              </div>
              <div class="section-body">
                <details v-for="d in relevantDays" :key="d.day" class="day-seg">
                  <summary>
                    第{{ d.day }}天 · {{ d.steps.length }} 个动作 ·
                    {{ fmtDur(d.range[1]) }} · {{ d.size_mb }} MB
                  </summary>
                  <video class="octopus-video" :src="d.url" controls preload="none"
                         @timeupdate="onDetailVideoTime($event, d.day)"
                         @pause="onDetailVideoTime($event, d.day)"></video>
                  <div class="step-list">
                    <div v-for="s in d.steps" :key="s.step" class="step-row" :title="s.code">
                      <span class="step-no">#{{ s.step }}</span>
                      <span class="step-time">{{ fmtDur(s.t0) }}</span>
                      <code class="step-code">{{ s.code }}</code>
                    </div>
                  </div>
                </details>
              </div>
            </div>

            <!-- 当天的关键事件画面。没开 --mc-footage 的 run 拿到空数组 -> 整块不渲染 -->
            <div v-if="footage.length" class="section">
              <div class="section-title">当时的画面（第{{ selectedDetail.day }}天 · {{ footage.length }}）</div>
              <div class="section-body footage-grid">
                <div v-for="(ev, i) in footage" :key="ev.file + '#' + i" class="footage-card" @click="footageZoom = ev">
                  <img :src="ev.url" :alt="footageLabel(ev)" loading="lazy" />
                  <div class="footage-cap">
                    <span class="footage-kind" :class="'fk-' + (ev.kind || '').split('_')[0]">{{ footageLabel(ev) }}</span>
                    <span class="footage-step">第{{ ev.step }}步</span>
                  </div>
                  <div v-if="footageDesc(ev)" class="footage-desc">{{ footageDesc(ev) }}</div>
                </div>
              </div>
            </div>

            <div v-if="selectedDetail.neighbors?.length" class="section">
              <div class="section-title">关系（{{ selectedDetail.neighbors.length }}）</div>
              <div class="section-body">
                <div v-for="(n, idx) in selectedDetail.neighbors" :key="idx" class="neighbor-row" @click="selectByPeer(n.peer_id)">
                  <span class="dir" :style="{color: relationColorMap[n.relation] || '#888'}">{{ n.direction === 'out' ? '→' : '←' }}</span>
                  <span class="rel-pill" :style="{color: relationColorMap[n.relation] || '#888'}">{{ n.relation }}</span>
                  <span class="peer-name">{{ n.peer_id }}</span>
                </div>
              </div>
            </div>
            </template>
          </aside>
        </transition>

        <!-- 画面灯箱：点缩略图看大图，点背景/按 Esc 关掉 -->
        <div v-if="footageZoom" class="footage-lightbox" @click="footageZoom = null">
          <img :src="footageZoom.url" :alt="footageLabel(footageZoom)" />
          <div class="footage-lightbox-cap">
            {{ footageLabel(footageZoom) }} · 第{{ footageZoom.day }}天第{{ footageZoom.step }}步
            <template v-if="footageDesc(footageZoom)"> · {{ footageDesc(footageZoom) }}</template>
          </div>
        </div>
      </div>
    </template>
    <div v-else class="graph-empty-full">
      <div class="ke-icon">🕸️</div>
      <div class="ke-title">未选择图谱</div>
      <div class="ke-desc">在左侧点选一张或多张图谱卡开始查看</div>
    </div>
  </div>
</template>

<style>
/* 深色主题变量：从 frontend/src/style.css 手动拷贝同步（viewer 独立于主线 frontend，暂不共享文件；合并主线时删掉这段改成 import） */
:root {
  --bg-base: #07080c;
  --bg-panel: #0e0f14;
  --bg-card: #13141a;
  --bg-hover: #1a1b22;
  --bg-active: #21222b;
  --brand: #6366f1;
  --brand-light: #818cf8;
  --brand-dim: rgb(99 102 241 / 12%);
  --gradient-brand: linear-gradient(135deg, #6366f1 0%, #8b5cf6 50%, #a855f7 100%);
  --text-primary: #eeeef0;
  --text-secondary: #9394a5;
  --text-muted: #5f6070;
  --text-dim: #3e3f50;
  --border: rgb(255 255 255 / 5.5%);
  --border-hover: rgb(255 255 255 / 10%);
  --green: #10b981;
  --green-dim: rgb(16 185 129 / 12%);
  --yellow: #f59e0b;
  --yellow-dim: rgb(245 158 11 / 12%);
  --red: #ef4444;
  --red-dim: rgb(239 68 68 / 12%);
  --blue: #3b82f6;
  --blue-dim: rgb(59 130 246 / 12%);
  --purple: #a855f7;
  --purple-dim: rgb(168 85 247 / 12%);
  --radius-sm: 6px;
  --radius-md: 10px;
  --radius-lg: 14px;
}
/* 浅色主题：跟 frontend/src/style.css 的 [data-theme='light'] 同步 */
[data-theme='light'] {
  --bg-base: #f0f0f5;
  --bg-panel: #fff;
  --bg-card: #fff;
  --bg-hover: #f4f4f8;
  --bg-active: #eaeaf0;
  --brand: #5b5bd6;
  --brand-light: #5b5bd6;
  --brand-dim: rgb(91 91 214 / 8%);
  --gradient-brand: linear-gradient(135deg, #5b5bd6 0%, #7c3aed 50%, #9333ea 100%);
  --text-primary: #111118;
  --text-secondary: #52525b;
  --text-muted: #71717a;
  --text-dim: #a1a1aa;
  --border: rgb(0 0 0 / 7%);
  --border-hover: rgb(0 0 0 / 13%);
}
* { box-sizing: border-box; margin: 0; padding: 0; }
html, body, #app { height: 100%; }
body {
  background: var(--bg-base);
  color: var(--text-primary);
  font-family: Inter, -apple-system, sans-serif;
  font-size: 13px;
}
.app { height: 100vh; display: flex; flex-direction: column; }

.topbar {
  height: 52px; flex-shrink: 0; display: flex; align-items: center; justify-content: space-between;
  padding: 0 20px; background: var(--bg-panel); border-bottom: 1px solid var(--border);
}
.topbar-left { display: flex; align-items: baseline; gap: 12px; }
.logo { font-weight: 700; font-size: 14px; }
.subtitle { font-size: 11px; color: var(--text-dim); }
.badge-branch {
  font-size: 10px; padding: 2px 8px; border-radius: 999px;
  background: var(--yellow-dim); color: var(--yellow); font-weight: 600;
}
.topbar-right { display: flex; align-items: center; gap: 10px; }
.btn-icon, .ctrl-btn {
  width: 28px; height: 26px; display: inline-flex; align-items: center; justify-content: center;
  background: transparent; border: 1px solid var(--border); color: var(--text-secondary);
  border-radius: 6px; cursor: pointer; transition: all .12s;
}
.btn-icon:hover, .ctrl-btn:hover { background: var(--bg-hover); color: var(--text-primary); border-color: var(--brand); }

/* ── 书架（2026-09-28 重做）：文件管理器式三层下钻 ─────────────────────────────
   按实验分组（实验×模型组合 → run → 图谱卡），面包屑回退，纵向可滚。
   第一层=实验×模型组合文件夹卡、第二层=run 文件夹卡、
   第三层=图谱卡（点选进画布）。高度上限 42vh 与旧版一致。 */
.shelf {
  flex-shrink: 0; display: flex; align-items: stretch; gap: 0;
  border-bottom: 1px solid var(--border); max-height: 42vh; overflow: hidden;
}
.shelf-main { flex: 1; min-width: 0; overflow-y: auto; padding: 12px 20px; }
/* 面包屑：退回上一层/顶层的通道，文件管理器惯例。根「全部图谱」常驻（当前在根时高亮）。 */
.shelf-crumbs { display: flex; align-items: center; gap: 2px; margin-bottom: 10px; flex-wrap: wrap; }
.crumb {
  display: inline-flex; align-items: center; gap: 2px; font-size: 12px;
  color: var(--text-secondary); background: transparent; border: none;
  border-radius: 4px; padding: 2px 6px; cursor: pointer;
}
.crumb:hover { background: var(--bg-hover); color: var(--text-primary); }
.crumb.root { color: var(--text-primary); font-weight: 600; }
.crumb-sep { color: var(--text-dim); margin: 0 2px; }
/* 卡片网格：自适应列宽（组合卡/run 卡/图谱卡统一一套），文件夹感 */
.shelf-grid {
  display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 10px;
}
.group-card {
  background: var(--bg-panel); border: 1px solid var(--border); border-radius: var(--radius-md);
  padding: 10px 12px; cursor: pointer; transition: all .15s;
  display: flex; flex-direction: column; gap: 3px; min-height: 88px;
}
.group-card:hover { background: var(--bg-hover); border-color: var(--border-hover); }
.group-card-top { display: flex; align-items: center; justify-content: space-between; margin-bottom: 2px; }
.gc-folder { font-size: 15px; }
.gc-count { font-size: 10px; color: var(--text-dim); border: 1px solid var(--border); border-radius: 8px; padding: 0 5px; }
.gc-major { font-size: 13px; font-weight: 700; color: var(--text-primary); }
.gc-minor { font-size: 11px; color: var(--text-secondary); }
.gc-minor-row { display: flex; align-items: center; gap: 5px; flex-wrap: wrap; }
.gc-meta { display: flex; align-items: center; gap: 5px; margin-top: auto; flex-wrap: wrap; }
.gc-host { font-size: 10px; color: var(--text-dim); }
/* run 卡大标题是时间戳，等宽数字对齐更好读 */
.run-card .gc-major { font-variant-numeric: tabular-nums; }
/* run 卡的条件徽标：主实验里区分 graph/nograph，消融里写明是哪个 rung */
.gh-cond {
  font-size: 10px; color: var(--text-secondary); background: var(--bg-active);
  border-radius: 999px; padding: 0 7px; white-space: nowrap;
}
/* 规模徽标（5臂×30夜 / 3臂×60夜…）：scaling run 的身份标识 */
.gh-scale {
  font-size: 10px; color: var(--brand-light, var(--brand)); font-weight: 600;
  border: 1px solid var(--brand); border-radius: 8px; padding: 0 5px; white-space: nowrap;
}
.gh-root { font-size: 10px; color: var(--text-dim); border: 1px solid var(--border); border-radius: 8px; padding: 0 5px; }
/* ── 直播面板（正在跑的 run，本机 X 屏实抓）── */
.gh-live {
  font-size: 10px; color: #ef4444; font-weight: 600; white-space: nowrap;
  border: 1px solid #ef4444; border-radius: 8px; padding: 0 5px;
}
.book-card {
  min-width: 0; background: var(--bg-panel); border: 1px solid var(--border); position: relative;
  border-radius: var(--radius-md); padding: 12px 14px; cursor: pointer; transition: all .15s;
}
.book-card-main { min-height: 108px; }
.book-card-top-row { display: flex; align-items: center; justify-content: space-between; gap: 6px; margin-bottom: 6px; }
.book-card-pills { display: flex; align-items: center; gap: 4px; flex-wrap: wrap; }
.book-card-actions { display: flex; align-items: center; gap: 2px; flex-shrink: 0; }
.book-card:hover { background: var(--bg-hover); }
.book-card.active { border-color: var(--brand); background: var(--brand-dim); }
.book-card.pending { cursor: default; opacity: .75; }
.book-card.pending:hover { background: var(--bg-panel); }
.book-card .subject-pill {
  display: inline-block; font-size: 10px; padding: 1px 7px; border-radius: 999px;
  background: var(--bg-active); color: var(--text-secondary);
}
.book-card .subject-pill.placeholder-pill { opacity: .75; }
.origin-pill { display: inline-block; font-size: 10px; padding: 1px 7px; border-radius: 999px; font-weight: 600; }
.origin-pill.origin-real { background: var(--green-dim); color: var(--green); }
.origin-pill.origin-staged { background: var(--yellow-dim); color: var(--yellow); }
/* 章鱼记忆图谱三种卡：主脑用品牌色（主角），冻结对照组用蓝色（跟主脑区分但同样"正式"，
   它是评测基线不是次要产物），副脑用灰调（临时产物，聚合完就销毁） */
.origin-pill.origin-octopus_brain { background: var(--brand-dim); color: var(--brand); }
.origin-pill.origin-octopus_frozen { background: var(--blue-dim); color: var(--blue); }
.origin-pill.origin-octopus_arm { background: var(--bg-active); color: var(--text-secondary); }
/* pre-line：私有图谱卡的书名是两行（角色·轮批 \n run 时间戳，见 server.py 的
   list_octopus_arm_books），HTML 默认会把那个 \n 压成空格 */
.book-card .book-name { font-size: 13px; font-weight: 600; margin-bottom: 8px; white-space: pre-line; }
.book-stats { display: flex; gap: 14px; font-size: 11px; color: var(--text-secondary); }
.book-stats b { color: var(--text-primary); }

.btn-delete-book {
  flex-shrink: 0; width: 20px; height: 20px; display: inline-flex; align-items: center; justify-content: center;
  background: transparent; color: var(--text-dim); border: none; border-radius: 6px; cursor: pointer;
}
.btn-delete-book:hover { background: var(--red-dim); color: var(--red); }


/* ── 图谱工具条 ── */
.graph-toolbar {
  height: 44px; flex-shrink: 0; display: flex; align-items: center; justify-content: space-between;
  padding: 0 20px; background: var(--bg-panel); border-bottom: 1px solid var(--border); gap: 12px;
}
.graph-toolbar-left { display: flex; align-items: center; gap: 10px; min-width: 0; }
.graph-toolbar-title { font-size: 13px; font-weight: 600; }
.graph-toolbar-count { font-size: 11px; color: var(--text-secondary); white-space: nowrap; }
.graph-toolbar-right { display: flex; align-items: center; gap: 8px; }
.search-bar {
  display: flex; align-items: center; gap: 6px; background: var(--bg-hover);
  border: 1px solid var(--border); border-radius: 6px; padding: 4px 10px; width: 200px;
  transition: border-color .15s;
}
.search-bar.focused { border-color: var(--brand); }
.search-bar svg { color: var(--text-secondary); flex-shrink: 0; }
.search-bar input {
  background: transparent; border: none; outline: none; flex: 1; font-size: 12px; color: var(--text-primary); min-width: 0;
}
.search-bar input::placeholder { color: var(--text-dim); }
.zoom-val { font-size: 11px; color: var(--text-secondary); min-width: 32px; text-align: center; }

/* ── 图谱主体 ── */
.kg-body { flex: 1; position: relative; overflow: hidden; background: var(--bg-base); }
/* 分屏：左图谱右地图。用 flex 而不是 grid —— 左半边到底是 canvas 还是空态/加载态是运行时
   才定的（三选一），flex:1 让"当时在场的那个"占住左半边，不用为每种情况写列宽。 */
.kg-body.split { display: flex; }
.kg-body.split .graph-canvas-wrap { flex: 1; min-width: 0; }
/* 加载/空态在分屏下**参与 flex**，而不是继续当 absolute 浮层。两个原因，缺一个都会出 bug：
   ① 它们和 .graph-canvas-wrap 是 v-if/v-else-if/v-else 的三选一 —— 空态出现时 canvas
      根本不在 DOM 里，flex 行只剩分隔条+地图，两者会滑到 left:0（实测地图挪到 x=5）；
   ② 浮层宽度写死百分比对不上可拖的 --split-pct，会横向压住地图。
   改成 flex:1 后左半边由它自己占住，地图归位，宽度也不用再写死。 */
.kg-body.split .kg-loading, .kg-body.split .kg-empty {
  position: relative; inset: auto; flex: 1; min-width: 0;
}
/* 详情面板贴的是 .kg-body 的右边 —— 分屏后那儿是地图，会把地图盖掉。改贴地图左侧。
   用 CSS 变量而不是写死百分比：分隔条可拖，宽度是运行时才知道的（--split-pct 由内联 style 给）。 */
.kg-body.split .detail-panel { right: calc(var(--split-pct, 46%) + 22px); }
/* 录像面板：浮层（不参与分屏），盖在图谱区上方。z-index 压过图谱各层（图例 z5），但
   在详情面板（z30）之下 —— 详情是全局浮层，永远该看得见。 */
.video-pane {
  position: absolute; top: 12px; right: 12px; bottom: 12px; z-index: 20;
  width: 46%; min-width: 300px; max-width: 720px;
  display: flex; flex-direction: column; overflow: hidden;
  border: 1px solid var(--border); border-radius: 10px; background: var(--bg-base);
  box-shadow: 0 8px 32px rgba(0, 0, 0, .25);
}
/* 地图开着（分屏）时：右半边是地图，录像只能盖在**图谱**那半边 —— 右边界贴到地图左沿
   （与 detail-panel 同一套 --split-pct 让位）。宽度改 left/right 双锚自适应：地图拖得宽时
   图谱半边可能比 46% 还窄，写死宽度会伸出图谱左沿。回放↔地图联动要看着地图打标记，
   盖住地图等于废掉联动。 */
.kg-body.split .video-pane { left: 12px; right: calc(var(--split-pct, 46%) + 12px); width: auto; min-width: 0; }
.btn-video.on { background: var(--brand-dim); color: var(--brand); border-color: var(--brand); }
.scene-pane {
  flex-shrink: 0; height: 100%; overflow: hidden; display: flex; flex-direction: column;
  border-left: 1px solid var(--border); background: var(--bg-base);
}
.scene-arms { display: flex; gap: 6px; padding: 8px 12px 0; flex-wrap: wrap; flex-shrink: 0; }
.scene-arm-chip {
  display: inline-flex; align-items: center; gap: 5px; font-size: 11px; padding: 3px 9px;
  border-radius: 999px; cursor: pointer; background: var(--bg-panel); color: var(--text-secondary);
  border: 1px solid var(--border); white-space: nowrap;
}
.scene-arm-chip:hover { background: var(--bg-hover); }
.scene-arm-chip.on { color: var(--text-primary); background: var(--bg-active); font-weight: 600; }
.split-handle {
  flex-shrink: 0; width: 5px; cursor: col-resize; background: var(--border);
  transition: background .12s;
}
.split-handle:hover { background: var(--brand); }
.btn-scene.on { background: var(--brand-dim); color: var(--brand); border-color: var(--brand); }
.btn-metrics.on { background: var(--brand-dim); color: var(--brand); border-color: var(--brand); }
/* 指标面板：与地图同一分屏位（scene-pane 的布局直接复用），底色用面板色把表格衬出来 */
.metrics-pane { background: var(--bg-panel); }
/* 面板内 sub-tab（主表/物品/成就）：tab 行占固定高，下面组件 flex:1 撑满 */
.metrics-pane { display: flex; flex-direction: column; }
.mp-tabs { display: flex; gap: 4px; padding: 6px 12px 4px; flex-shrink: 0;
           border-bottom: 1px solid var(--border); }
.mp-tab { padding: 3px 10px; font-size: 11px; border: 1px solid transparent;
          border-radius: 6px; background: none; color: var(--text-secondary); cursor: pointer; }
.mp-tab:hover { background: var(--bg-hover); }
.mp-tab.on { background: var(--brand-dim); color: var(--brand); border-color: var(--brand); }
.graph-canvas-wrap {
  width: 100%; height: 100%; cursor: grab; user-select: none; background: var(--bg-base);
  /* 图例（.legend*）是 absolute 定位在这个容器里的 —— 少了 relative 它们就会拿
     .kg-body 当包含块，分屏后 left:16px 落在**地图那半边**上，实测把图例画到了地图里
     （地图在左时重叠 220px）。图例属于图谱，必须跟着图谱那半边走。 */
  position: relative;
}
.graph-canvas-wrap:active { cursor: grabbing; }
.graph-svg { width: 100%; height: 100%; display: block; background: var(--bg-base); }

.node-group { cursor: pointer; transition: opacity .2s; }
.node-group:hover circle:nth-of-type(3),
.node-group.selected circle:nth-of-type(3) { filter: drop-shadow(0 0 8px currentColor); }

.node-anim-enter-active, .node-anim-leave-active { transition: opacity .45s ease; }
.node-anim-enter-from, .node-anim-leave-to { opacity: 0; }
.edge-anim-enter-active, .edge-anim-leave-active { transition: opacity .45s ease; }
.edge-anim-enter-from, .edge-anim-leave-to { opacity: 0; }
.cluster-anim-enter-active, .cluster-anim-leave-active { transition: opacity .45s ease; }
.cluster-anim-enter-from, .cluster-anim-leave-to { opacity: 0; }
.clusters-layer ellipse { pointer-events: none; }

.kg-loading, .kg-empty {
  position: absolute; inset: 0; display: flex; flex-direction: column; align-items: center; justify-content: center;
  gap: 14px; color: var(--text-secondary); font-size: 14px;
}
.kg-spinner {
  width: 32px; height: 32px; border: 3px solid var(--border); border-top-color: var(--brand);
  border-radius: 50%; animation: kg-spin .8s linear infinite;
}
@keyframes kg-spin { to { transform: rotate(360deg); } }
.ke-icon { font-size: 48px; }
.ke-title { font-size: 16px; font-weight: 600; }
.ke-desc { font-size: 13px; color: var(--text-secondary); }

.legend {
  position: absolute; left: 16px; bottom: 16px; background: var(--bg-panel); backdrop-filter: blur(8px);
  border: 1px solid var(--border); border-radius: 8px; padding: 10px 12px; font-size: 12px;
  color: var(--text-secondary); box-shadow: 0 4px 16px rgb(0 0 0 / 30%); z-index: 5;
}
.legend-books { bottom: auto; top: 16px; max-width: 220px; }
.legend-changes { left: auto; right: 16px; }
/* 经验层图例：贴右下，避免和右上的「本次训练变化」图例重叠（两者不会同时出现在同一张卡上
   —— cleared 只在副脑卡、change_status 只在主脑卡 —— 但多选叠加时可能同屏） */
.legend-cleared { left: auto; right: 16px; bottom: 16px; top: auto; }
.legend-title { font-size: 10px; color: var(--text-dim); text-transform: uppercase; letter-spacing: .5px; margin-bottom: 6px; }
/* 图谱/关系类型两个图例的条数由数据决定（选中多张卡、关系类型多）会顶穿画布，
   所以只让条目区滚动、标题固定在顶上。overscroll-behavior 防止滚到底后把滚动
   事件传给画布触发缩放。 */
.legend-scroll { max-height: 180px; overflow-y: auto; overscroll-behavior: contain; }
.legend-scroll::-webkit-scrollbar { width: 6px; }
.legend-scroll::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }
.legend-scroll::-webkit-scrollbar-track { background: transparent; }
.legend-item { display: flex; align-items: center; gap: 6px; padding: 2px 0; }
.legend-line { display: inline-block; width: 18px; height: 2px; border-radius: 1px; }
.legend-dot { display: inline-flex; align-items: center; justify-content: center; width: 16px; height: 16px; line-height: 1; border-radius: 50%; font-size: 10px; font-weight: 800; color: #fff; }
/* ── 详情侧栏（浮层而非常驻栏） ── */
.detail-panel {
  position: absolute; top: 16px; right: 16px; bottom: 16px; width: 360px;
  background: var(--bg-panel); border: 1px solid var(--border); border-radius: 10px;
  padding: 14px 16px; overflow-y: auto; z-index: 30; box-shadow: 0 8px 32px rgb(0 0 0 / 40%);
}
.detail-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 10px; margin-bottom: 10px; }
.detail-title { font-size: 16px; font-weight: 600; line-height: 1.3; margin-bottom: 6px; }
.detail-meta { display: flex; gap: 5px; flex-wrap: wrap; margin-bottom: 4px; }
.badge {
  background: var(--bg-hover); border: 1px solid var(--border); color: var(--text-secondary);
  padding: 2px 8px; border-radius: 10px; font-size: 11px;
}
.subject-badge { background: var(--brand-dim); color: var(--brand-light); border-color: var(--brand); }
/* 「已聚合清掉」的 badge：虚线边框呼应图上那些虚线节点，让两处是同一个视觉语言 */
.badge-cleared { border-style: dashed; cursor: help; }
/* 技能 badge：与 concept 的层标记同位，用品牌色实心区分 */
.badge-skill { background: var(--brand); color: #fff; border-color: var(--brand); }
/* 技能详情专用 */
.skill-sig { font-family: "SF Mono", Menlo, monospace; font-size: 12px; background: var(--bg-hover); padding: 4px 8px; border-radius: 4px; border: 1px solid var(--border); display: inline-block; }
.skill-counts { display: flex; flex-direction: column; gap: 6px; font-size: 13px; }
.skill-counts-row { display: flex; gap: 16px; align-items: baseline; }
.skill-counts-label { font-size: 11px; color: var(--text-secondary); min-width: 56px; }
.skill-ok { color: #22c55e; }
.skill-fail { color: #ef4444; }
.skill-copy { font-size: 11px; color: var(--text-secondary); background: var(--bg-hover); border: 1px solid var(--border); border-radius: 4px; padding: 2px 8px; cursor: pointer; text-transform: none; letter-spacing: 0; }
.skill-copy:hover { color: var(--brand-light); border-color: var(--brand); }
.skill-code { margin: 0; background: var(--bg-base); border: 1px solid var(--border); border-radius: 6px; padding: 10px 12px; overflow-x: auto; font-family: "SF Mono", Menlo, monospace; font-size: 12px; line-height: 1.5; color: var(--text-primary); white-space: pre; }
.skill-code code { font-family: inherit; }
.detail-slug { font-size: 11px; color: var(--text-dim); font-family: "SF Mono", Menlo, monospace; word-break: break-all; }
.detail-source { font-size: 11px; color: var(--text-secondary); margin-top: 4px; white-space: pre-line; }
.section { margin-top: 14px; }
.section-title { font-size: 11px; color: var(--text-dim); text-transform: uppercase; letter-spacing: .5px; margin-bottom: 6px; display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.section-body { font-size: 13px; color: var(--text-primary); line-height: 1.55; white-space: pre-wrap; }
.alias-pill {
  display: inline-block; background: var(--bg-hover); border: 1px solid var(--border); color: var(--text-secondary);
  padding: 2px 8px; border-radius: 10px; font-size: 11px; margin: 2px 4px 2px 0;
}
.facet-card { background: var(--bg-base); border: 1px solid var(--border); border-radius: 6px; padding: 8px 10px; margin-bottom: 6px; }
.facet-head { display: flex; justify-content: space-between; margin-bottom: 4px; }
.facet-kind { font-size: 11px; color: var(--brand-light); font-weight: 600; }
.facet-content { font-size: 12px; color: var(--text-primary); line-height: 1.5; white-space: pre-wrap; }

/* ---- 录像（每条 facet 一段片段 + 按天的完整录像） + JS 步清单 ---- */
.octopus-video { width: 100%; border-radius: 6px; background: #000; display: block; }
.step-list { margin-top: 6px; max-height: 220px; overflow-y: auto;
             border: 1px solid var(--border); border-radius: 6px; }
.step-row { display: flex; align-items: center; gap: 8px; padding: 4px 8px;
            font-size: 11px; border-bottom: 1px solid var(--border); }
.step-row:last-child { border-bottom: none; }
.step-no { color: var(--text-muted); min-width: 32px; }
.step-time { color: var(--brand-light); min-width: 38px; font-variant-numeric: tabular-nums; }
.step-code { color: var(--text-primary); overflow: hidden; text-overflow: ellipsis;
             white-space: nowrap; font-family: ui-monospace, monospace; }
.facet-clip { font-size: 10px; color: var(--brand); cursor: pointer; padding: 1px 5px;
              border: 1px solid var(--brand); border-radius: 3px; white-space: nowrap;
              transition: background .12s; }
.facet-clip:hover { background: var(--brand); color: #fff; }
/* 来源步对不上：不可点，用暗淡的警示色 —— 它是「模型报了个定位不到的步号」的提示，
   不是一个可交互的入口。default 光标（非 pointer）就是在说「这里没东西可点」。 */
.facet-clip-bad { font-size: 10px; color: var(--text-muted); padding: 1px 5px;
                  border: 1px dashed var(--border); border-radius: 3px; cursor: default; }
/* facet 正文下面那段录像：留一点上边距，别贴着文字 */
.facet-video { margin-top: 6px; }
.facet-video-cap { font-size: 10px; color: var(--text-muted); margin-top: 3px;
                   font-variant-numeric: tabular-nums; }
/* 「/ 全程 N 天」那半句：弱化成灰色，提示这里是筛过的子集、不是全部 */
.clip-hint { color: var(--text-muted); font-weight: normal; }
/* 按天的完整录像：<details> 折叠，收起时不加载视频（preload=none） */
.day-seg { border: 1px solid var(--border); border-radius: 6px; padding: 6px 8px;
           margin-bottom: 6px; }
.day-seg > summary { font-size: 11px; color: var(--text-primary); cursor: pointer;
                     font-variant-numeric: tabular-nums; }
.day-seg[open] > summary { margin-bottom: 6px; }

/* ---- 关键事件画面（footage）：两列缩略图 + 点开灯箱 ---- */
.footage-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 6px; }
.footage-card { background: var(--bg-base); border: 1px solid var(--border); border-radius: 6px;
                overflow: hidden; cursor: zoom-in; transition: border-color .15s; }
.footage-card:hover { border-color: var(--brand); }
.footage-card img { width: 100%; display: block; aspect-ratio: 16 / 9; object-fit: cover; background: #000; }
.footage-cap { display: flex; justify-content: space-between; align-items: center; padding: 4px 6px 2px; }
.footage-kind { font-size: 11px; font-weight: 600; color: var(--brand-light); }
/* 事件类型配色：死亡红、tier 突破绿、成就黄 —— 与图谱里的语义色一致 */
.footage-kind.fk-death { color: #ef4444; }
.footage-kind.fk-tier { color: #22c55e; }
.footage-kind.fk-advancement { color: #f59e0b; }
.footage-step { font-size: 10px; color: var(--text-muted); }
.footage-desc { font-size: 10px; color: var(--text-muted); padding: 0 6px 5px;
                overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.footage-lightbox { position: fixed; inset: 0; z-index: 200; background: rgba(0,0,0,.85);
                    display: flex; flex-direction: column; align-items: center; justify-content: center;
                    gap: 10px; cursor: zoom-out; }
.footage-lightbox img { max-width: 92vw; max-height: 84vh; border-radius: 6px; }
.footage-lightbox-cap { font-size: 13px; color: #e5e7eb; }
.neighbor-row { display: flex; align-items: center; gap: 8px; padding: 6px 8px; border-radius: 5px; cursor: pointer; transition: background .15s; font-size: 12px; }
.neighbor-row:hover { background: var(--bg-hover); }
.dir { font-size: 14px; font-weight: 700; width: 16px; text-align: center; }
.dir-out { color: var(--brand); }
.dir-in { color: var(--yellow); }
.rel-pill { font-size: 11px; font-weight: 600; }
.peer-name { color: var(--text-primary); flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

.slide-enter-active, .slide-leave-active { transition: transform .25s ease, opacity .25s ease; }
.slide-enter-from, .slide-leave-to { transform: translateX(20px); opacity: 0; }


@keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: .4; } }

.empty-state { color: var(--text-dim); font-size: 12px; padding: 30px; text-align: center; }
.graph-empty-full {
  flex: 1; min-width: 0; display: flex; flex-direction: column;
  align-items: center; justify-content: center; gap: 6px;
}

/* ---------- Markdown / KaTeX 渲染过的内容在详情面板里的样式收敛 ---------- */
.md-body p { margin: 4px 0; }
.md-body p:first-child { margin-top: 0; }
.md-body p:last-child { margin-bottom: 0; }
.md-body ul,
.md-body ol { margin: 4px 0; padding-left: 20px; }
.md-body li { margin: 2px 0; }
.md-body h1,
.md-body h2,
.md-body h3,
.md-body h4 { margin: 8px 0 4px; font-size: 13px; font-weight: 600; color: var(--text-primary); }
.md-body code {
  background: var(--bg-hover); padding: 1px 4px; border-radius: 3px; font-size: 11px;
  font-family: "SF Mono", Menlo, Consolas, monospace;
}
.md-body pre { background: var(--bg-hover); padding: 8px 10px; border-radius: 4px; overflow-x: auto; font-size: 11px; margin: 6px 0; }
.md-body pre code { background: transparent; padding: 0; }
.md-body blockquote { border-left: 3px solid var(--border-hover); padding-left: 8px; margin: 4px 0; color: var(--text-secondary); }
.md-body a { color: var(--brand-light); text-decoration: none; }
.md-body a:hover { text-decoration: underline; }
.md-body table { border-collapse: collapse; font-size: 11px; margin: 6px 0; }
.md-body th,
.md-body td { border: 1px solid var(--border); padding: 3px 6px; }
/* KaTeX 块级公式在窄面板里防溢出 */
.md-body .katex-display { margin: 6px 0; overflow-x: auto; overflow-y: hidden; }
.md-body .katex { font-size: 1em; }
</style>
