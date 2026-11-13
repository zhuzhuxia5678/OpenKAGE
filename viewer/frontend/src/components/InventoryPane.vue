<script>
/**
 * 物品/成就清单面板：**当前选中 run** 逐 agent 的「拿到过哪些物品 / 达成过哪些成就」，
 * 参照 results/item_table.html（experiments/item_table_html.py 生成的离线对照表）的
 * 展示口径 —— 物品按 7 类分组 + 图标 + 中文名，成就按 5 个 tab 分组 + 图标 + 首达天。
 *
 * 数据来自 `GET /api/books/.../inventory`（后端读各臂最新扁平 checkpoint 的
 * metrics_snapshot.items_seen / advancements，口径与 item_table_html 同源；分类/译名/
 * tab 归属的表头元数据由后端从 experiments.item_table_html 常量直接带出，一处改两边变）。
 * 图标走 /api/icons/item/<物品名>.png 与 /api/icons/adv/<key>.png（results/item_icons
 * 与 results/adv_icons 的静态贴图）。图标请求不看表头元数据：run 里出现过就试，
 * 这样后来新增、还没进 _CAT_ITEMS 的物品只要有贴图也能显示；404 时 @error 记进
 * failedIcons，降级为占位色块（不能直接 display:none，那样格子会整块空白）。
 *
 * 布局与 MetricsPane 同一右侧分屏位（在它内部以 sub-tab 切换主表 / 物品 / 成就，
 * 切换状态在 App.vue 持有）。与离线 HTML 的差异：① 一次只看一个 run（无实验/对照组
 * 并排着色）；② 面板窄，物品表不做冻结表头大矩阵 —— 逐 agent 卡片式：每 agent 一块，
 * 物品按类别分组铺图标网格，鼠标悬停看中文名；成就同理但带首达天。
 */
import { computed, ref } from "vue";

// agent 展示顺序（与后端 main_table_metrics._ARM_ORDER 同序）
const ARM_ORDER = ["alice", "bob", "carol", "dave", "erin", "frank", "greg", "helen"];
// 物品类别展示顺序（后端 _inv_table_meta.items 的 cat 值，与 _CAT_ITEMS 同源）
const CAT_ORDER = ["原木与木材", "矿石与原料", "工具", "武器与防具", "食物",
                   "红石与铁轨", "羊毛与掉落物"];
// 成就 tab 展示顺序 + 覆盖比分母（后端 tab_total，与 _ADV_TAB_TOTAL 同源）
const TAB_ORDER = ["Minecraft", "Nether", "The End", "Adventure", "Husbandry"];

export default {
  name: "InventoryPane",
  props: {
    // /api/books/.../inventory 的返回：{has_inventory, items_by_arm, advs_by_arm, items, advs, tab_total, ...run 元数据}
    data: { type: Object, default: null },
    loading: { type: Boolean, default: false },
    // 'items' | 'advs'：App.vue 持有的 sub-tab（与 MetricsPane 的主表共用一排 tab）
    tab: { type: String, default: "items" },
  },
  emits: ["update:tab"],
  setup(props) {
    const row = computed(() => (props.data?.has_inventory ? props.data : null));

    // 缺贴图时降级成占位色块。图标是静态资源、不随 run 变，失败一次整页记住即可，
    // 免得每个 agent 卡片都重复发一次必然 404 的请求。
    const failedIcons = ref(new Set());
    function markIconFailed(key) {
      failedIcons.value = new Set(failedIcons.value).add(key);
    }

    // agent 顺序：alice..helen 固定序，未知名排最后
    const armNames = computed(() => {
      const arms = Object.keys(row.value?.items_by_arm || {});
      return arms.sort((a, b) => (ARM_ORDER.indexOf(a) + 1 || 9) - (ARM_ORDER.indexOf(b) + 1 || 9)
                                || a.localeCompare(b));
    });

    // ── 物品视图 ──
    // 该 run 出现过的全部物品（各臂并集）按类别分桶；只在已知类别集合里的按 CAT_ORDER
    // 排，未知类别（新物品没进 _CAT_ITEMS）归「其他」排最后。
    const itemGroups = computed(() => {
      const meta = row.value?.items || {};
      const seen = new Set();
      for (const a of armNames.value)
        for (const it of row.value.items_by_arm[a] || []) seen.add(it);
      const buckets = new Map(CAT_ORDER.map(c => [c, []]));
      buckets.set("其他", []);
      for (const it of seen) {
        const cat = meta[it]?.cat || "其他";
        if (!buckets.has(cat)) buckets.set(cat, []);
        buckets.get(cat).push(it);
      }
      // 组内排序：有中文名的按中文、其余按原名，稳定输出
      for (const [c, arr] of buckets) arr.sort((x, y) => (meta[x]?.cn || x).localeCompare(meta[y]?.cn || y));
      return [...buckets.entries()].filter(([, arr]) => arr.length);
    });

    // ── 成就视图 ──
    // 本 run 达成过的全部成就（各臂并集，带方括号显示名）按 tab 分桶 + 覆盖比
    const advGroups = computed(() => {
      const meta = row.value?.advs || {};
      const totals = row.value?.tab_total || {};
      const seen = new Map();   // 显示名 → {cn, tab, icon, days: {arm: 首达天}}
      for (const a of armNames.value) {
        for (const [disp, day] of Object.entries(row.value?.advs_by_arm?.[a] || {})) {
          if (!seen.has(disp)) seen.set(disp, { ...(meta[disp] || { cn: disp, tab: "其他", icon: null }), days: {} });
          seen.get(disp).days[a] = day;
        }
      }
      const buckets = new Map(TAB_ORDER.map(t => [t, []]));
      buckets.set("其他", []);
      for (const e of seen.values()) {
        const t = e.tab || "其他";
        if (!buckets.has(t)) buckets.set(t, []);
        buckets.get(t).push(e);
      }
      for (const arr of buckets.values()) arr.sort((x, y) => (x.cn || "").localeCompare(y.cn || ""));
      return [...buckets.entries()].filter(([, arr]) => arr.length)
        .map(([t, arr]) => ({ tab: t, advs: arr, total: totals[t] || arr.length }));
    });

    // 每个 agent 的计数行（物品数 / 成就数），卡片头部显示
    function itemCount(a) { return (row.value?.items_by_arm?.[a] || []).length; }
    function advCount(a) { return Object.keys(row.value?.advs_by_arm?.[a] || {}).length; }

    // 成就徽标文案：达成过 → 首达天（第N天），未达成不渲染
    function advDay(e, a) { return e.days[a] != null ? `第${e.days[a]}天` : null; }

    return { row, armNames, itemGroups, advGroups, itemCount, advCount, advDay,
             failedIcons, markIconFailed, CAT_ORDER, TAB_ORDER };
  },
};
</script>

<template>
  <div class="inv-pane">
    <div class="inv-head">
      <span class="inv-title">物品与成就清单</span>
      <span v-if="row" class="inv-run">{{ row.condition_label }} · {{ row.run_ts }}</span>
    </div>

    <div v-if="loading && !row" class="inv-loading">加载清单中…</div>
    <div v-else-if="data && !data.has_inventory" class="inv-loading">
      这个 run 还没有可统计的 checkpoint（第一夜还没跑完）
    </div>
    <div v-else-if="!row" class="inv-loading">选择一张图谱卡后显示该 run 的清单</div>

    <div v-else class="inv-body">
      <!-- 物品视图：逐 agent 卡片，每卡按类别分组的图标网格 -->
      <template v-if="tab === 'items'">
        <div class="inv-arm" v-for="a in armNames" :key="'i-' + a">
          <div class="inv-arm-head">
            <span class="inv-arm-name">{{ a }}</span>
            <span class="inv-arm-count">{{ itemCount(a) }} 种物品</span>
          </div>
          <div class="inv-cat" v-for="[cat, items] in itemGroups" :key="a + cat">
            <span class="inv-cat-label">{{ cat }}</span>
            <div class="inv-grid">
              <div v-for="it in items" :key="it" class="inv-cell"
                   :class="{ got: row.items_by_arm[a]?.includes(it) }"
                   :title="`${row.items[it]?.cn || it} (${it})`">
                <img v-if="!failedIcons.has(it)" :src="`/api/icons/item/${it}.png`" :alt="it"
                     loading="lazy" @error="markIconFailed(it)" />
                <span v-else class="inv-noicon">{{ (row.items[it]?.cn || it)[0] }}</span>
                <span class="inv-cn">{{ row.items[it]?.cn || it }}</span>
              </div>
            </div>
          </div>
        </div>
      </template>

      <!-- 成就视图：逐 agent 卡片，按 tab 分组的徽标（图标 + 中文名 + 首达天） -->
      <template v-else-if="tab === 'advs'">
        <div class="inv-arm" v-for="a in armNames" :key="'a-' + a">
          <div class="inv-arm-head">
            <span class="inv-arm-name">{{ a }}</span>
            <span class="inv-arm-count">{{ advCount(a) }} 项成就</span>
          </div>
          <div class="inv-cat" v-for="g in advGroups" :key="a + g.tab">
            <span class="inv-cat-label">{{ g.tab }} <span class="inv-cat-ratio">{{ g.advs.length }}/{{ g.total }}</span></span>
            <div class="inv-grid inv-grid-adv">
              <div v-for="e in g.advs" :key="e + a" class="inv-cell inv-adv"
                   :class="{ got: e.days[a] != null }"
                   :title="e.days[a] != null ? `${e.cn} · 第${e.days[a]}天` : e.cn">
                <img v-if="e.icon && !failedIcons.has('adv:' + e.icon)"
                     :src="`/api/icons/adv/${e.icon}.png`" :alt="e.cn"
                     loading="lazy" @error="markIconFailed('adv:' + e.icon)" />
                <span v-else class="inv-noicon">{{ (e.cn || '?')[0] }}</span>
                <span class="inv-cn">{{ e.cn }}</span>
                <span v-if="advDay(e, a)" class="inv-day">{{ advDay(e, a) }}</span>
              </div>
            </div>
          </div>
        </div>
      </template>
    </div>
  </div>
</template>

<style scoped>
.inv-pane { display: flex; flex-direction: column; height: 100%; overflow: hidden; }
.inv-head { display: flex; flex-direction: column; gap: 2px; padding: 8px 12px 6px; flex-shrink: 0; }
.inv-title { font-size: 12px; font-weight: 600; color: var(--text-primary); }
.inv-run { font-size: 10px; color: var(--text-secondary); font-variant-numeric: tabular-nums; }
.inv-loading { padding: 24px 16px; font-size: 12px; color: var(--text-dim); text-align: center; line-height: 1.6; }
.inv-body { flex: 1; min-height: 0; overflow-y: auto; padding: 0 12px 12px; }

/* 逐 agent 卡片 */
.inv-arm { margin-bottom: 14px; border: 1px solid var(--border); border-radius: 8px;
           background: var(--bg-base); overflow: hidden; }
.inv-arm-head { display: flex; align-items: baseline; gap: 8px; padding: 6px 10px;
                border-bottom: 1px solid var(--border); }
.inv-arm-name { font-size: 12px; font-weight: 600; color: var(--text-primary); }
.inv-arm-count { font-size: 10px; color: var(--text-dim); font-variant-numeric: tabular-nums; }

/* 类别分组 */
.inv-cat { padding: 6px 10px 2px; }
.inv-cat + .inv-cat { border-top: 1px dashed var(--border); }
.inv-cat-label { font-size: 10px; color: var(--text-dim); text-transform: uppercase;
                 letter-spacing: .5px; }
.inv-cat-ratio { color: var(--text-dim); opacity: .8; margin-left: 2px; }

/* 图标网格：小格子（图标 + 截断中文名），拿到过的亮、没拿到的暗 */
.inv-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(64px, 1fr));
            gap: 4px; padding: 4px 0 6px; }
.inv-grid-adv { grid-template-columns: repeat(auto-fill, minmax(108px, 1fr)); }
.inv-cell { display: flex; flex-direction: column; align-items: center; gap: 2px;
            border-radius: 5px; padding: 4px 2px; opacity: .22;
            border: 1px solid transparent; }
.inv-cell.got { opacity: 1; background: var(--brand-dim); border-color: var(--brand-dim); }
.inv-cell img { width: 22px; height: 22px; image-rendering: pixelated; }
.inv-cell .inv-noicon { width: 22px; height: 22px; line-height: 22px; text-align: center;
                        font-size: 10px; color: var(--text-dim); background: var(--bg-hover);
                        border-radius: 4px; }
.inv-cn { font-size: 9px; color: var(--text-secondary); max-width: 100%;
          overflow: hidden; text-overflow: ellipsis; white-space: nowrap; text-align: center; }
.inv-day { font-size: 9px; color: var(--green); font-variant-numeric: tabular-nums; }
/* 成就格横排（图标左、文字右），比物品格宽 */
.inv-adv { flex-direction: row; justify-content: flex-start; gap: 5px; padding: 3px 6px; }
.inv-adv .inv-cn { text-align: left; flex: 1; min-width: 0; }
</style>
