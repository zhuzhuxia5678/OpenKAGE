<script>
/**
 * 量化指标面板：**当前选中 run** 的论文主表指标（Table~\ref{tab:main} 那套口径），
 * 与探索地图同一块右侧分屏位（两者互斥：指标是表格不是地图，同屏开两个右侧栏
 * 会把图谱挤没，见 App.vue 的 rightPane 状态机注释）。
 *
 * 数据来自 `GET /api/books/.../metrics`（后端逐 agent 调 experiments/main_table_metrics
 * .arm_metrics，口径与论文/CLI 完全同源，不另算）。指标看的是整个 run（一份
 * checkpoint 聚合出 3 agent 均值），所以面板跟着「选中卡所属的 run」走，不挑卡种：
 * 选中主脑/冻结/副脑/planner-only 卡都行，取的都是同一个 run 的数。
 *
 * 布局：run 级 8 列（论文列 + XP/life 诊断列）一排横放 + 逐 agent 明细常展（一个
 * run 就 3~8 个 agent，没有再收进折叠的必要）。单 run 接口 ~1s，无 building 轮询。
 *
 * 用词对齐论文（App.vue scaleLabel 同款约定）：论文统一称 agent / game day，
 * 「臂」「夜」是内部黑话（后端字段仍叫 arm/nights，展示层一律翻译）。
 */
import { computed } from "vue";

// run 级指标卡：key = 行字段，dir 影响箭头朝向
const COLS = [
  { key: "deaths", label: "Deaths", dir: "down", fmt: v => v == null ? "—" : v.toFixed(1),
    title: "每 agent 累计死亡数，均值" },
  { key: "starve", label: "Starve", dir: "down", fmt: v => v == null ? "—" : String(v),
    title: "全 run 饿死总数" },
  { key: "eq_max", label: "Eq-max", dir: "up", fmt: (v, r) => v == null ? "—" : `${Math.round(v)} (${r.eq_max_n}/${r.n_arms_metrics})`,
    title: "任意一天的最高四槽装备分（满分 16），括号 = 达到该峰值的 agent 数" },
  { key: "era", label: "Tool era", dir: "up", fmt: (v, r) => v == null ? "—" :
      r.era_day == null ? `${v} (—, ${r.era_n}/${r.n_arms_metrics})`
                        : `${v} (${r.era_day} day, ${r.era_n}/${r.n_arms_metrics})`,
    title: "最高工具材质档（iron/dmd.）+ 首达天均值（对达到的 agent）+ 人数" },
  { key: "advs", label: "Adv's", dir: "up", fmt: v => v == null ? "—" : v.toFixed(1),
    title: "五树成就去重计数，均值" },
  { key: "obsidian_day", label: "Obsidian", dir: "down", fmt: (v, r) => v == null ? "—" : `${v} day (${r.obsidian_n}/${r.n_arms_metrics})`,
    title: "首次拿到黑曜石的天（对拿到的 agent 取均值）+ 人数；— = 无人拿到" },
  { key: "nether_day", label: "Nether", dir: "down", fmt: (v, r) => v == null ? "—" : `${v} day (${r.nether_n}/${r.n_arms_metrics})`,
    title: "首次进入下界的天（对进入的 agent 取均值）+ 人数；— = 无人进入" },
  { key: "xp_per_life", label: "XP/life", dir: "up", fmt: v => v == null ? "—" : v.toFixed(1),
    title: "末天累计 XP ÷ (死亡+1)，先每人算再平均（诊断列，论文不报）" },
];

const ARM_ORDER = ["alice", "bob", "carol", "dave", "erin", "frank", "greg", "helen"];

// 逐 agent 明细的列定义：name 左对齐，数据列等宽居中（表头与数据行同 grid，天然对齐）
const DETAIL_COLS = [
  { label: "Agent", cls: "mp-arm-name" },
  { label: "死亡" }, { label: "XP/life" }, { label: "Eq-max" },
  { label: "档位（首达天）" },
  { label: "黑曜" }, { label: "下界" }, { label: "Adv's" },
];

export default {
  name: "MetricsPane",
  props: {
    // /api/books/.../metrics 的返回：{has_metrics, ...指标字段}
    data: { type: Object, default: null },
    loading: { type: Boolean, default: false },
  },
  setup(props) {
    const row = computed(() => (props.data?.has_metrics ? props.data : null));

    // 逐 agent 明细：按 alice..helen 固定顺序（与后端 _ARM_ORDER 同序），未知名排最后
    const armRows = computed(() => {
      const arms = row.value?.arms || {};
      return Object.keys(arms).sort(
        (a, b) => (ARM_ORDER.indexOf(a) + 1 || 9) - (ARM_ORDER.indexOf(b) + 1 || 9)
                  || a.localeCompare(b))
        .map(k => ({ arm: k, ...arms[k] }));
    });

    function night(v) { return v == null ? "—" : String(v); }

    return { COLS, DETAIL_COLS, row, armRows, night };
  },
};
</script>

<template>
  <div class="mp-pane">
    <div class="mp-head">
      <span class="mp-title">量化指标 · 主表口径</span>
      <span v-if="row" class="mp-run">{{ row.condition_label }} · {{ row.run_ts }}
        <template v-if="!row.complete"> · <span class="mp-nights" :title="'未完赛：只跑了 ' + row.n_nights + ' 天'">@N{{ row.n_nights }}</span></template>
      </span>
    </div>

    <div v-if="loading && !row" class="mp-loading">加载指标中…</div>
    <div v-else-if="data && !data.has_metrics" class="mp-loading">
      这个 run 还没有可统计的 checkpoint（第一夜还没跑完）
    </div>
    <div v-else-if="!row" class="mp-loading">选择一张图谱卡后显示该 run 的指标</div>

    <div v-else class="mp-body">
      <!-- run 级 8 列：卡式网格（面板窄，表格横向挤不下） -->
      <div class="mp-cards">
        <div v-for="c in COLS" :key="c.key" class="mp-card" :title="c.title">
          <div class="mp-card-label">{{ c.label }}<span class="mp-dir">{{ c.dir === 'down' ? '↓' : '↑' }}</span></div>
          <div class="mp-card-val">{{ c.fmt(row[c.key], row) }}</div>
        </div>
      </div>

      <!-- 逐 agent 明细：一个 run 3~8 个 agent，常展不折叠。表头/数据行同一 grid 列定义，列列对齐 -->
      <div class="mp-arm-head">分 agent 明细</div>
      <div class="mp-arm-row mp-arm-row-head">
        <span v-for="c in DETAIL_COLS" :key="c.label" :class="c.cls">{{ c.label }}</span>
      </div>
      <div v-for="a in armRows" :key="a.arm" class="mp-arm-row">
        <span class="mp-arm-name">{{ a.arm }}</span>
        <span>{{ a.deaths }}<template v-if="a.starve"> · 饿{{ a.starve }}</template></span>
        <span>{{ a.xp_per_life == null ? '—' : a.xp_per_life.toFixed(1) }}</span>
        <span>{{ a.eq_max == null ? '—' : Math.round(a.eq_max) }}</span>
        <span>{{ a.era }}<template v-if="a.era_night != null"> · 第{{ a.era_night }}天</template></span>
        <span :class="{ 'mp-hit': a.obsidian_night != null }">{{ night(a.obsidian_night) }}</span>
        <span :class="{ 'mp-hit': a.nether }">{{ night(a.nether_night) }}</span>
        <span>{{ a.advs == null ? '—' : a.advs }}</span>
      </div>
    </div>
  </div>
</template>

<style scoped>
.mp-pane { display: flex; flex-direction: column; height: 100%; overflow: hidden; }
.mp-head { display: flex; flex-direction: column; gap: 2px; padding: 8px 12px 6px; flex-shrink: 0; }
.mp-title { font-size: 12px; font-weight: 600; color: var(--text-primary); }
.mp-run { font-size: 10px; color: var(--text-secondary); font-variant-numeric: tabular-nums; }
.mp-nights { color: var(--yellow); border: 1px dashed var(--yellow); border-radius: 6px; padding: 0 3px; }
.mp-loading { padding: 24px 16px; font-size: 12px; color: var(--text-dim); text-align: center; line-height: 1.6; }
.mp-body { flex: 1; min-height: 0; overflow-y: auto; padding: 0 12px 12px; }

/* run 级指标卡：两列网格（面板宽度 ~1/3 屏，8 张卡 2×4 排布） */
.mp-cards { display: grid; grid-template-columns: 1fr 1fr; gap: 6px; margin-bottom: 12px; }
.mp-card {
  background: var(--bg-base); border: 1px solid var(--border); border-radius: 6px;
  padding: 6px 8px; min-width: 0;
}
.mp-card-label { font-size: 10px; color: var(--text-dim); margin-bottom: 2px; white-space: nowrap; }
.mp-dir { font-size: 9px; margin-left: 2px; }
.mp-card-val { font-size: 13px; font-weight: 600; color: var(--text-primary); font-variant-numeric: tabular-nums; }

/* 逐 agent 明细：表头/数据行同一 grid（8 列：Agent 名 + 7 数据列），列列严格对齐；
   数据列居中（数字表），name 列左对齐。tabular-nums 保证同列数字位对位。 */
.mp-arm-head { font-size: 10px; color: var(--text-dim); text-transform: uppercase; letter-spacing: .5px; margin-bottom: 4px; }
.mp-arm-row {
  display: grid;
  grid-template-columns: minmax(52px, auto) repeat(7, 1fr);
  gap: 6px; font-size: 10.5px; color: var(--text-secondary); padding: 3px 6px;
  font-variant-numeric: tabular-nums; text-align: center;
}
.mp-arm-row-head { color: var(--text-dim); border-bottom: 1px solid var(--border-hover); }
.mp-arm-row:not(.mp-arm-row-head):hover { background: var(--bg-hover); border-radius: 4px; }
.mp-arm-name { color: var(--text-primary); font-weight: 600; text-align: left; }
/* 达成里程碑的 agent 数字亮色：一眼看出是谁拿的黑曜/进了下界 */
.mp-hit { color: var(--green); font-weight: 600; }
</style>
