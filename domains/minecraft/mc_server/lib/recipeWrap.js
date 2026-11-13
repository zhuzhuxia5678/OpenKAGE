// 包装 bot.recipesFor / bot.recipesAll / bot.craft：返回空数组或 recipe 为 null 时补一条
// **客观事实** console.log / 可读错误，把 mineflayer 的静默陷阱翻译成 Agent 能读懂的事实。
// ★ 只报事实不处方：诊断只陈述「配方存不存在 / 需不需要工作台 / 库存够不够 / recipe 参数是什么」，
//   不教 Agent 下一步做什么（「先做工作台」「补齐材料」这类是 Agent 该自己推理的，薄壳替它想
//   就回了原语的老路）。
//
// ★ 这是一段**自包含执行代码**（无 module.exports）：由 env.py 用 read_text 读成字符串，
//   拼进每步 /step 的 programs 头部注入 eval 作用域。和 control_primitives/*.js 同机制，
//   但独立于已移除的高级原语（2026-08-27 用户定 self._programs=""）—— 只补「反馈」半边、
//   不恢复原语的「预封装能力」。开关在 env.py 侧（_RECIPE_WRAP_ENABLED），要对照实验关掉即可。
//
// 背景（见 memory openkage_skill_vs_primitive_experiment）：移除 16 个高级原语后 Agent 只剩
//   mineflayer 原生 API。原语时代 craftItem.js 内部本有客观诊断（requiresTable 判断、缺料点名），
//   移除后这道信号没了。实测 0901 run 第一天 5 臂全 tier=none、无一造出木镐，根因是 Agent 反复
//   bot.recipesFor(id, null, 1, null) 查木镐 —— 木镐配方 requiresTable=true，传 null 的 craftingTable
//   → recipesFor 走 requirementsMetForRecipe 第一行 `recipe.requiresTable && !craftingTable → false`
//   直接过滤 → 返回 []；recipesAll 走 `!recipe.requiresTable || craftingTable` → false → 同样 []。
//   两个空数组同时意味着「①需要工作台但你传了null ②配方客观不存在 ③缺料」三种本质不同的状态，
//   mineflayer 一字不提，Agent 自己 console.log("配方不可用") 后误判放弃。
//
// 设计要点：
//   · 不改控制流：包装函数仍返回原始结果（[]），只额外 console.log 一条诊断。不抛错、不改返回值，
//     Agent 原有代码逻辑不受影响，只是多了一条它该看到的信息。
//   · 诊断走 Recipe.find（独立查询，不调被包装的 bot.recipesAll，避免递归）：拿 recipe.requiresTable
//     和 recipe.delta 判「需要工作台 / 缺什么料 / 真无配方」三态。
//   · 幂等防重包：__openkageRecipeWrap 标记位，重复注入（续跑/多次拼 programs）不二次包装。
//   · 反馈进 onLog 通道（与 Agent 自己的 console.log 同路），不占额外配额类型。
//   · 行号映射安全：本段作为 programs 一部分注入，其行数计入 programs.split("\n").length 偏移，
//     LLM 代码行号映射自动正确（见 index.js handleError 的 programs_length 逻辑）。
//
// 单测见 test_recipe_wrap_smoke.js：read_text 本文件后 eval，注入 mock bot/Recipe 验证三态分支。
if (typeof bot !== 'undefined' && bot && typeof bot.recipesFor === 'function' && !bot.__openkageRecipeWrap) {
  bot.__openkageRecipeWrap = true;
  const _Recipe = (typeof require === 'function') ? require('prismarine-recipe')(bot.registry).Recipe : null;
  const _mcData = (typeof mcData !== 'undefined') ? mcData : ((typeof require === 'function') ? require('minecraft-data')(bot.version) : null);
  const _origRecipesFor = bot.recipesFor.bind(bot);
  const _origRecipesAll = bot.recipesAll.bind(bot);
  // id→name：点名缺料/产物用。优先 mcData.items[id].name，拿不到回退 'id#N'。
  function _idName(id) {
    try {
      if (_mcData && _mcData.items && _mcData.items[id] && _mcData.items[id].name) return _mcData.items[id].name;
    } catch (e) {}
    return 'id#' + id;
  }
  // 核心诊断：给定 itemType/craftingTable，返回该不该报、报什么。返回 null=不报，否则返回诊断串。
  // ★ 只报客观事实（配方存不存在 / 需不需要台 / 库存够不够），不处方（不教下一步做什么）——
  //   「先做工作台」「补齐材料再查」这类是 Agent 该自己推理的，薄壳替它想就回了原语的老路。
  function _diagnose(itemType, metadata, craftingTable) {
    if (!_Recipe) return null; // 拿不到 Recipe 模块就没法诊断，退回原始静默
    let recipes = [];
    try { recipes = _Recipe.find(itemType, metadata) || []; } catch (e) { return null; }
    if (!recipes.length) {
      return `[配方诊断] 物品 ${_idName(itemType)}(id=${itemType}) 客观上无任何配方。`;
    }
    // 配方客观存在。看是否需要工作台而调用者没给。
    const needsTable = recipes.some(r => r.requiresTable);
    const hasTable = !!craftingTable;
    if (needsTable && !hasTable) {
      return `[配方诊断] ${_idName(itemType)} 的配方存在且需要工作台，但本次查询的 craftingTable ` +
        `参数为 ${(craftingTable === null ? 'null' : (craftingTable === undefined ? 'undefined' : '假值'))}。`;
    }
    // 配方存在、不缺工作台（或本就不需要台）→ 剩下只剩「缺料」。逐 delta 点名缺什么。
    // delta 里 count<0 的是材料；按「做 1 次」算每种料够不够。
    const missing = [];
    for (const r of recipes) {
      for (const d of (r.delta || [])) {
        if (d.count < 0) {
          let have = 0;
          try { have = bot.inventory.count(d.id, d.metadata); } catch (e) { have = 0; }
          const need = -d.count; // 做 1 次所需
          if (have < need) missing.push(`${_idName(d.id)}(have ${have}/need ${need})`);
        }
      }
      if (missing.length) break; // 一个配方报清就够了
    }
    if (missing.length) {
      return `[配方诊断] ${_idName(itemType)} 配方存在${needsTable ? '且工作台已提供' : ''}，库存缺料：` +
        missing.join('、') + `。`;
    }
    return null; // 配方在、料够、台也给了 → 不报（这种情况 recipesFor 本应返回非空，不该走到这）
  }
  bot.recipesFor = function(itemType, metadata, minResultCount, craftingTable) {
    const res = _origRecipesFor(itemType, metadata, minResultCount, craftingTable);
    if (res && res.length === 0) {
      const msg = _diagnose(itemType, metadata, craftingTable);
      if (msg) console.log(msg);
    }
    return res;
  };
  bot.recipesAll = function(itemType, metadata, craftingTable) {
    const res = _origRecipesAll(itemType, metadata, craftingTable);
    if (res && res.length === 0) {
      const msg = _diagnose(itemType, metadata, craftingTable);
      if (msg) console.log(msg);
    }
    return res;
  };
  // ★ bot.craft：recipe 为 null/undefined 时，mineflayer 内部 `assert.ok(recipe)` 抛的是
  //   AssertionError「The expression evaluated to a falsy value」——对 Agent 完全不可读。
  //   这里把不可读的 AssertionError 换成点明事实的可读错误（仍抛错，不改控制流：craft 本来就会抛）。
  //   只补这一处客观反馈缺口，recipe 合法时透传原始 craft，不干预合成逻辑。
  const _origCraft = bot.craft.bind(bot);
  bot.craft = async function(recipe, count, craftingTable) {
    if (recipe === null || recipe === undefined ||
        typeof recipe !== 'object' || typeof recipe.result === 'undefined') {
      throw new Error(
        `craft 的 recipe 参数为 ${recipe === null ? 'null' : (recipe === undefined ? 'undefined' : '非配方对象')}，` +
        `类型应为 recipesFor/recipesAll 返回的配方对象。`
      );
    }
    return _origCraft(recipe, count, craftingTable);
  };
}
