/**
 * 回放位置联动的纯映射部分：录像时间 → 地图坐标。
 *
 * 从 VideoPane 抽出（2026-09-30）：concept 详情面板里的三处录像（facet 片段 / 技能片段 /
 * 按天整段「相关录像」）也要位置联动，规则与录像面板完全同源 —— 两处各写一份必然漂移
 * （步末坐标怎么摊、间隙停哪、跨天出发点取哪，都是定案过的行为）。
 *
 * 数据模型（与后端 /api/video 对齐）：
 *   · steps.jsonl 每行的 `position` 是**步末**坐标（env 在 world.step 之后取 next_obs），
 *     `t0/t1` 是**段内**相对秒（footage 每天起一段录制器、重置基准）。
 *   · 一天 = `[{step, t0, t1, position:{x,y,z}}, ...]`（按 step 升序）。
 *
 * 映射规则（2026-09-30 定案）：
 *   · t 落在步 s 的 [t0,t1] 内 —— 从**出发位置**（上一步末；当天首步 = 前一天末步，
 *     跨天反思窗口 bot 不动、位置连续）线性走向 s.position：步首→步末的位移摊到这段
 *     录像时间上，录像里 bot 在走、标记同步在走。
 *   · t 落在间隙（s.t1 ≤ t < 下一步 t0）—— LLM 思考，bot 原地不动，停在 s.position，
 *     不预支下一步的位移。
 *   · 长位移/瞬移步**不做特判**：position 是步末快照，pathfinder 长步一步走上百格与
 *     死亡重生瞬移在索引里无法区分，一律线性滑过。
 *   · t 早于首步 t0 —— 停在出发位置；晚于末步 t1 —— 钳在末步位置。
 */

/**
 * 建「一天」的查询器。
 * @param steps    当天带 position 的步（可混入无 position 的步，会先滤掉）
 * @param prevEnd  前一天末步位置（当天出发点；null = 第 1 天 / 前一天没数据，
 *                 首步内插值退化成从首步末出发 —— 最保守行为，标记不乱跳）
 */
export function makePosQuery(steps, prevEnd) {
  const pos = (steps || []).filter(
    s => s && s.position && isFinite(s.t0) && isFinite(s.t1));
  const dayStart = prevEnd && Number.isFinite(prevEnd.x) ? prevEnd : null;
  return {
    /** 这天有没有可联动的数据（没带 position 的早期 run → false，调用方静默不联动） */
    usable: pos.length > 0,
    /**
     * t 秒 → {x, y, z, step}；这天不可用时恒为 null。
     * 返回对象每次都是新引用 —— 前端靠引用变化触发响应式更新，复用对象会漏更新。
     */
    at(t) {
      if (!pos.length) return null;
      const first = pos[0], last = pos[pos.length - 1];
      if (t <= first.t0) {
        const p = dayStart || first.position;
        return { x: p.x, y: p.y, z: p.z, step: first.step };
      }
      if (t >= last.t1) {
        return { x: last.position.x, y: last.position.y, z: last.position.z, step: last.step };
      }
      for (let i = 0; i < pos.length; i++) {
        const s = pos[i];
        if (t < s.t0) {
          // 间隙：上一步已收尾、这一步还没开始 —— 停在上一步末
          const p = pos[Math.max(0, i - 1)];
          return { x: p.position.x, y: p.position.y, z: p.position.z, step: p.step };
        }
        if (t < s.t1) {
          // 步内：从出发位置线性走向本步末（i=0 时 = 前一天末步，见 dayStart）
          const a = i > 0 ? pos[i - 1].position : (dayStart || s.position);
          const k = (t - s.t0) / Math.max(s.t1 - s.t0, 1e-6);
          const b = s.position;
          return { x: a.x + (b.x - a.x) * k, y: a.y + (b.y - a.y) * k,
                   z: a.z + (b.z - a.z) * k, step: s.step };
        }
      }
      return { x: last.position.x, y: last.position.y, z: last.position.z, step: last.step };
    },
  };
}

/**
 * 便捷封装：从 /api/video 的 `days` 里取「day 那天」的查询器（顺带找前一天末步当出发点）。
 * 返回的查询器 `usable === false` 表示那天没有可联动的数据。
 */
export function queryForDay(days, day) {
  const list = (days || []).filter(d => d.day === day);
  const cur = list[0];
  if (!cur) return makePosQuery([], null);
  // 前一天 = 按 day 排序比它小的最近一天（steps 里带 position 的才算 —— 中间某天全没
  // position 时跨过它找更早的，位置仍是连续的）
  const earlier = (days || []).filter(d => d.day < day && (d.steps || []).some(s => s.position));
  const prevSteps = earlier.length
    ? (earlier[earlier.length - 1].steps || []).filter(s => s.position) : [];
  return makePosQuery(cur.steps || [], prevSteps.length ? prevSteps[prevSteps.length - 1].position : null);
}
