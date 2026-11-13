<script>
/**
 * 录像面板：一条轨迹（一个副脑卡）的按天选集 + 正在跑时的直播，与探索地图同款右半边布局。
 *
 * **只有一排按钮 + 一块画面**，没有单独的直播入口（2026-09-30 用户定案）：
 *   · 选集（days）—— GET /api/books/.../video 给的按天分段。
 *   · 直播（live）—— GET /api/live/<run>/<arm>/stream 的 MJPEG 流，只在**本机**正在录的
 *     臂上出现。**就长在"最新那一天"那个按钮上**：正在录的那天带 🔴 直播，点它看实时画面，
 *     点别的天看回放 —— 画面始终是下面那一个框（直播时框里放 <img>，回放时放 <video>）。
 * ★ 正在录那天的 mp4 **打不开**（moov 索引写在文件尾、end_day 才落盘，见 footage.py），
 *   但文件已经在盘上、后端会把它算进 days —— 所以不能**另加**一个同号的"直播天"按钮
 *   （会变成两个"第 7 天"），只能在 days 里把那一天就地标 live。
 *
 * 播放定位沿用 facet 跳播那套：steps.jsonl 的 t0/t1 是**段内**相对秒（footage.py 起每段
 * 录制器时重置基准），所以 `video.currentTime = t0` 不需要知道前面几天各录了多久。
 *
 * ★ 回放位置联动（2026-09-30）：steps.jsonl 每行带 `position`（**步末**坐标，env 落索引时
 *   从 world.step 之后的 next_obs 取，见 env.py mark_step 调用处），且 /api/video 原样传
 *   上来 —— 于是「视频播到第几秒」可以就地换算成「它在地图上的哪一格」emit 给 App 转给
 *   SceneMap 打标记。映射规则（步内线性铺开 / 间隙停上一步末 / 长步不特判）与 concept
 *   详情面板那三处录像**共用同一份实现**（composables/replayPos.js，别处再要联动也从
 *   那儿取）：两处各写一份必然漂移。整天选集之外（直播 / 没选天 / 该 run 的 steps 没带
 *   position，如早期 run）emit null，地图收起标记。坐标与地图轨迹源逐点一致已实测
 *   （同一天 36 步 ↔ 36 点、首末点距离 0），无需后端改动。
 */
import { ref, watch, computed } from "vue";
import { makePosQuery } from "../composables/replayPos.js";

export default {
  name: "VideoPane",
  props: {
    // /api/books/.../video 的返回：{has_video, days:[{day,url,steps,range,size_mb}], ...}
    video: { type: Object, default: null },
    loading: { type: Boolean, default: false },
    // /api/live 里这一臂的记录；null/undefined = 没在录（停跑/远端/反思间隙）
    liveRec: { type: Object, default: null },
    // 这条轨迹所属的 run 目录名 + 臂名（直播流的 URL 用）
    runSlug: { type: String, default: "" },
    armKey: { type: String, default: "" },
    // 臂的展示名（面板标题用，如 "Alice · 第1轮第1批"）
    armLabel: { type: String, default: "" },
  },
  emits: ["replay-pos"],
  setup(props, { emit }) {
    const curDay = ref(null);      // 当前选的天（null = 没选/没有）
    const lastLiveDay = ref(null); // 短暂扫不到（反思/天边界）时记住直播天，别把画面切走
    const videoEl = ref(null);
    // liveRec 里的 pid 当 nonce：录制器重启（新的一天）pid 变 → src 变 → <img> 自动重连
    const liveUrl = computed(() => {
      if (!props.liveRec || !props.runSlug) return null;
      return `/api/live/${encodeURIComponent(props.runSlug)}/${encodeURIComponent(props.armKey)}`
             + `/stream?w=640&fps=3&_=${props.liveRec.pid}`;
    });

    // 正在录的那天也作为一个可点的“天”：直播是判据不是独立画面
    const liveDayNum = computed(() => {
      if (props.liveRec?.day != null) { lastLiveDay.value = props.liveRec.day; return props.liveRec.day; }
      return lastLiveDay.value;
    });

    // 选集 = 后端按天分段，正在录的那天就地标 live（不在 days 里才补一个）。
    const dayEntries = computed(() => {
      const out = (props.video?.days || []).map(d => ({ ...d }));
      const live = liveDayNum.value;
      if (live == null) return out;
      const hit = out.find(d => d.day === live);
      if (hit) hit.live = true;   // 已在 days 里：那天 mp4 还没 moov、放不了，画面走直播流
      else out.push({ day: live, live: true, url: null, steps: [], range: null, size_mb: null });
      return out.sort((a, b) => a.day - b.day);
    });

    // days 变了（换卡/轮询刷新）要对齐 curDay：跟不住的（被取消选中的臂）落回直播天/最后一天
    watch([() => props.video, liveDayNum], ([, liveDay]) => {
      const days = dayEntries.value;
      if (!days.length) { curDay.value = null; return; }
      if (curDay.value != null && days.some(d => d.day === curDay.value)) return;
      // 默认选正在录的那天（= 直播）；没有直播再选**最后一段完整落盘的天**
      curDay.value = liveDay != null ? liveDay : days[days.length - 1].day;
    }, { immediate: true });

    const curDayRec = computed(() =>
      dayEntries.value.find(d => d.day === curDay.value) || null);

    // ── 回放位置联动：视频时间 → 地图坐标 ─────────────────────────────────
    // 当前选集那一天的「带 position 的步」（早期 run 的 steps.jsonl 可能不带 position ——
    // 空 = 这天联动不了，不报错）。查询器由共用模块构造：当天步 + 前一天末步当出发点
    // （跨天反思窗口 bot 不动、位置连续；第 1 天没有前一天 → 首步内从首步末出发）。
    const posSteps = computed(() => {
      const steps = curDayRec.value?.steps || [];
      return steps.filter(s => s.position && isFinite(s.t0) && isFinite(s.t1));
    });
    const dayStartPos = computed(() => {
      const cur = curDayRec.value;
      if (!cur) return null;
      const days = props.video?.days || [];
      const prevDay = [...days].reverse().find(d => d.day < cur.day
        && d.steps?.some(s => s.position));
      const prevSteps = (prevDay?.steps || []).filter(s => s.position);
      return prevSteps.length ? prevSteps[prevSteps.length - 1].position : null;
    });
    const posQuery = computed(() => makePosQuery(posSteps.value, dayStartPos.value));

    function fmtDur(s) {
      if (!isFinite(s)) return "—";
      const m = Math.floor(s / 60), sec = Math.round(s % 60);
      return m ? `${m}m${String(sec).padStart(2, "0")}s` : `${sec}s`;
    }

    // 播放头走到 t 秒 → emit {day, x, z, step}（映射规则见 replayPos.js 的模块 docstring：
    // 步内从出发位置线性铺开 / 间隙停上一步末 / 长步不特判）。换天/切直播/这步数据没
    // position → emit null，地图收起标记。
    function onTimeUpdate() {
      const el = videoEl.value;
      if (!el || curDay.value == null || curDay.value === liveDayNum.value) {
        emit("replay-pos", null); return;
      }
      const q = posQuery.value;
      if (!q.usable) { emit("replay-pos", null); return; }
      const p = q.at(el.currentTime);
      emit("replay-pos", p ? { day: curDay.value, ...p } : null);
    }
    // 暂停/换天时也要报一次：暂停在半路时标记应停在当前位置（timeupdate 只在播放中走）
    function onPause() { onTimeUpdate(); }
    // 换天 / 离开选集（切直播）→ 先发 null 清掉地图上的旧标记，避免「看第 3 天直播、地图上还
    // 停着第 2 天回放的点」这种错位。seek 到新位置后 timeupdate 自然会补发新值。
    watch([curDay, () => props.video], () => emit("replay-pos", null));

    return { curDay, curDayRec, dayEntries, liveDayNum, liveUrl, videoEl, fmtDur, onTimeUpdate, onPause };
  },
};
</script>

<template>
  <div class="vp-pane">
    <div class="vp-head">
      <span class="vp-title">{{ armLabel || '录像' }}</span>
      <span v-if="liveRec" class="vp-live-pill">🔴 直播 第{{ liveRec.day }}天</span>
    </div>

    <!-- 正在录的那天被选中 = 直播；选别的天看回放。离开直播天 → <img> 卸载 → 后端 ffmpeg 被杀。 -->
    <div v-if="loading && !video && !dayEntries.length" class="vp-loading">加载录像索引…</div>
    <div v-else-if="!dayEntries.length" class="vp-empty">
      这条轨迹没有按天分段录像（这条 run 没开 --mc-footage；本发布包仅附关键事件画面，不含按天视频）
    </div>
    <template v-else>
      <div class="vp-days">
        <button v-for="d in dayEntries" :key="d.day" class="vp-day"
                :class="{on: d.day === curDay, live: d.live}" @click="curDay = d.day">
          第{{ d.day }}天<span class="vp-day-size">{{ d.live ? '🔴 直播' : d.size_mb + 'MB' }}</span>
        </button>
      </div>
      <div v-if="curDayRec" class="vp-player">
        <template v-if="curDayRec.live">
          <img v-if="liveUrl" class="vp-stream" :src="liveUrl" :alt="`直播 ${armKey}`">
          <div v-else class="vp-stream vp-wait">反思 / 天边界 · 暂无画面</div>
        </template>
        <video v-else ref="videoEl" :key="curDayRec.day" controls preload="metadata"
               :src="curDayRec.url"
               @timeupdate="onTimeUpdate" @pause="onPause"></video>
        <div class="vp-dayinfo">
          <template v-if="curDayRec.live">第 {{ curDayRec.day }} 天 · 🔴 直播（本机 X 屏实抓）</template>
          <template v-else>第 {{ curDayRec.day }} 天 · {{ curDayRec.steps.length }} 个动作步
            · 全长 {{ fmtDur(curDayRec.range[1]) }}
          </template>
        </div>
      </div>
    </template>
  </div>
</template>

<style scoped>
.vp-pane { display: flex; flex-direction: column; height: 100%; overflow: hidden; }
.vp-head { display: flex; align-items: center; gap: 8px; padding: 8px 12px 6px; flex-shrink: 0; }
.vp-title { font-size: 12px; font-weight: 600; color: var(--text-primary); white-space: pre-line; }
.vp-live-pill {
  font-size: 10px; color: #ef4444; font-weight: 600; white-space: nowrap;
  border: 1px solid #ef4444; border-radius: 8px; padding: 0 5px;
  animation: vp-blink 1.4s ease-in-out infinite;
}
@keyframes vp-blink { 0%, 100% { opacity: 1; } 50% { opacity: .35; } }
.vp-loading, .vp-empty { padding: 24px 16px; font-size: 12px; color: var(--text-dim); text-align: center; }
.vp-days { display: flex; gap: 5px; padding: 0 12px 8px; flex-wrap: wrap; flex-shrink: 0; }
.vp-day {
  font-size: 11px; padding: 2px 8px; border-radius: 6px; cursor: pointer;
  background: var(--bg-panel); color: var(--text-secondary);
  border: 1px solid var(--border); white-space: nowrap;
}
.vp-day:hover { background: var(--bg-hover); }
.vp-day.on { color: var(--brand-light, var(--brand)); border-color: var(--brand); background: var(--brand-dim); font-weight: 600; }
.vp-day.live.on, .vp-day.live:hover { color: #ef4444; border-color: #ef4444; background: rgba(239,68,68,.08); }
.vp-day-size { font-size: 9px; color: var(--text-dim); margin-left: 3px; }
.vp-player { flex: 1; min-height: 0; display: flex; flex-direction: column; padding: 0 12px 10px; }
/* 回放的 <video> 与直播的 MJPEG <img> 占同一块（用户定案：直播不另开显示框） */
.vp-player video, .vp-player .vp-stream {
  width: 100%; flex: 1; min-height: 0; background: #000; border-radius: 6px; object-fit: contain;
}
.vp-wait {
  display: flex; align-items: center; justify-content: center;
  font-size: 11px; color: var(--text-dim); text-align: center; line-height: 1.6;
}
.vp-dayinfo { font-size: 10px; color: var(--text-dim); padding-top: 5px; text-align: center; }
</style>
