// 合成失败时的播报。**只被 craftItem 内部调用**，不是给 LLM 用的原语
// （但 load_control_primitives 把本目录所有 .js 无差别拼进注入串，故它在生成代码里也可见）。
//
// ─── 为什么用「游戏自带的 tag」来归纳，而不是枚举配方变体 ─────────────────────
// 原版游戏的配方材料位大量是**标签(tag)**，不是具体物品，而 minecraft-data / prismarine-recipe
// 不支持 tag，会把它笛卡尔展开成一条条具体配方，标签语义丢失：只能挑一条报（可能与实际持有的
// 变体不符）或全量枚举（啰嗦），且库数据版本滞后还会漏收新物品。见
// domains/minecraft/extract_tags.py 顶部的完整说明。
//
// 这里用 jar 抽出的原始配方（保留 tag 引用，落到 lib/mc_tags.json）播报，能直接表达
// 「任意 #logs」这类分组，且库滞后也不会漏。分组是**游戏自带的权威定义**，不是启发式猜的。
//
// 【降级】mc_tags.json 读不到时回退到「全量枚举 prismarine 变体」，保证原语永远有话可说 ——
// 但会 chat 一句提示，别让降级静默发生。
// 路径必须容忍两种加载方式：
//   · **注入执行（线上路径）**：本文件被 index.js 用 `eval("(async()=>{"+programs+code+"})()")`
//     执行，`__dirname` 是 **mc_server/**（index.js 所在目录），不是 control_primitives/。
//   · **直接 require（写测试时）**：`__dirname` 才是 control_primitives/。
// 故两个候选都留着，谁先命中用谁。
const _MC_TAGS_PATHS = [
    __dirname + "/lib/mc_tags.json", // eval 注入：__dirname = mc_server
    __dirname + "/../lib/mc_tags.json", // require 本文件：__dirname = control_primitives
];
let _mcTagData = null; // {tags: {name: [item...]}, recipes: {product: [{needs, requiresTable, makes}]}}
let _mcTagLoadFailed = false;

function _loadMcTags() {
    if (_mcTagData || _mcTagLoadFailed) return _mcTagData;
    for (const p of _MC_TAGS_PATHS) {
        try {
            _mcTagData = JSON.parse(require("fs").readFileSync(p, "utf8"));
            return _mcTagData;
        } catch (err) {
            /* 试下一个路径 */
        }
    }
    _mcTagLoadFailed = true;
    return null;
}

/**
 * 把一个 tag 的成员压成人类可读的「任意X」式描述，对齐 Minecraft wiki 的表达法。
 *
 * wiki 上 soul_campfire 的材料写作「任意原木或菌柄或任意去皮原木或…」共 8 类，而不是罗列 44 个
 * 物品名。这几类**可以纯从物品名推出来**，不需要手写语义表：按 `(有无 stripped_ 前缀, 末尾词元)`
 * 分组即可与 wiki 的分类对齐（如 #logs 按 log/wood/stem/hyphae × 是否去皮分组）。
 *
 * 组内 ≥2 个成员就说「any <form>」并给出木种数量；只有 1 个就直接报物品名（说「任意」反而费解）。
 * **库存里已有的成员优先展示**（把它所在的组排最前、并在组内点名），LLM 一眼能看到自己能用哪个 ——
 * 这是唯一的排序，纯按持有量，不猜语义。
 */
function _describeTagMembers(members, countOf) {
    const groups = new Map(); // form → {items:[], owned:[], total:0}
    for (const m of members) {
        const stripped = m.startsWith("stripped_");
        const form = (stripped ? "stripped " : "") + m.split("_").pop();
        if (!groups.has(form))
            groups.set(form, { items: [], owned: [], total: 0 });
        const g = groups.get(form);
        g.items.push(m);
        const c = countOf(m);
        if (c > 0) {
            g.owned.push(`${m}x${c}`);
            g.total += c;
        }
    }
    // 手里有的组排前面
    const ordered = [...groups.entries()].sort((a, b) => b[1].total - a[1].total);
    const parts = ordered.map(([form, g]) => {
        // 单成员组直接报名字 —— 说「any cobblestone (1 kinds)」是废话
        if (g.items.length === 1) {
            return g.owned.length ? g.owned[0] : g.items[0];
        }
        if (g.owned.length) {
            // 有存货：点名自己有的，同时说明这一类还有几种可选。
            // 不给 form 加复数 -s：末尾词元本身可能已是复数（planks → "plankss" 就闹笑话了）。
            return `${g.owned.join("/")} (or any other of ${g.items.length} ${form} types)`;
        }
        return `any ${form} (${g.items.length})`;
    });
    return `accepts ${parts.join(" OR ")}`;
}

function failedCraftFeedback(bot, name, item, craftingTable) {
    // 第一关：连「假设有工作台」都查不到任何配方 → 这东西在当前条件下根本做不出来。
    // recipesAll 的过滤是 `!recipe.requiresTable || craftingTable`，所以传入 craftingTable
    // 为空、而该物品的所有配方都要 3×3 时，这里为空 —— 就是「附近没有工作台」。
    if (!bot.recipesAll(item.id, null, craftingTable).length) {
        throw new Error(`No crafting table nearby`);
    }

    const data = _loadMcTags();
    if (data && data.recipes && data.recipes[name]) {
        _feedbackFromTags(bot, name, data, craftingTable);
        return;
    }
    _feedbackFromExpandedRecipes(bot, name, item, !data);
}

/** 用 jar 原始配方（保留 tag）播报。每个材料位一行，标明「任一即可」与已有数量。 */
function _feedbackFromTags(bot, name, data, craftingTable) {
    const countOf = (itemName) => {
        const it = mcData.itemsByName[itemName];
        return it ? bot.inventory.count(it.id, null) : 0;
    };
    // 一个材料位可能是 {item} / {tag} / {anyOf:[...]}（如 torch 的 coal|charcoal）。
    // 统一成「可接受的具体物品名列表」+ 一个人类可读的标签描述。
    const slotOptions = (need) => {
        if (need.item) return { label: need.item, options: [need.item] };
        if (need.anyOf) {
            // 候选列表少时直接 "A or B"（如 coal or charcoal，最清楚）；多了就走与 tag 相同的
            // 归纳（如 orange_bed 接受 15 种床，平铺成一行 200+ 字符纯是噪音）。
            return need.anyOf.length <= 3
                ? { label: need.anyOf.join(" or "), options: need.anyOf }
                : { label: `any of ${need.anyOf.length}`, options: need.anyOf,
                    tagMembers: need.anyOf };
        }
        const members = (data.tags && data.tags[need.tag]) || [];
        return {
            label: `any #${need.tag}`,
            options: members,
            tagMembers: members,
        };
    };

    const recipes = data.recipes[name];
    // 各条配方是否同一档（全要台 / 全不要台）。同档就把这句话提到末尾说一次，混档才逐条标。
    const mixedTableReq =
        recipes.some((r) => r.requiresTable) && recipes.some((r) => !r.requiresTable);
    const allRequireTable = recipes.every((r) => r.requiresTable);
    const lines = [];
    let anyReady = false;
    for (const r of recipes) {
        const parts = [];
        let short = 0;
        for (const need of r.needs) {
            const { label, options, tagMembers } = slotOptions(need);
            // 同一材料位可用多种物品时，取**库存里该位可用物品的总量**（它们可混用）
            let have = 0;
            const owned = [];
            for (const o of options) {
                const c = countOf(o);
                if (c > 0) {
                    have += c;
                    owned.push(`${o}x${c}`);
                }
            }
            const missing = Math.max(need.count - have, 0);
            short += missing;
            let desc = `${need.count}x ${label}`;
            if (tagMembers && tagMembers.length > 1) {
                desc += ` — ${_describeTagMembers(tagMembers, countOf)}`;
            }
            parts.push(
                `${desc} — you have ${have}` +
                    (owned.length ? ` [${owned.join(", ")}]` : "") +
                    (missing > 0 ? `, need ${missing} more` : " ✓")
            );
        }
        if (short === 0) anyReady = true;
        const head =
            recipes.length > 1 ? `Recipe (makes ${r.makes}):` : `Needs (makes ${r.makes}):`;
        // 「要不要工作台」这行**不在这里逐条重复**：同一产物的各条配方几乎总是同一档，重复 N 遍
        // 纯是噪音。统一提到末尾说一次；万一真有混档（部分变体 2×2、部分 3×3），下面会逐条标
        // 出来，不会因合并而说错。
        lines.push(
            head + "\n" + parts.map((p) => "    - " + p).join("\n") +
                (mixedTableReq ? `\n    * ${r.requiresTable ? "3x3 — needs a crafting table" : "2x2 — bare-handed OK"}` : "")
        );
    }

    // 料够却仍失败 → 缺工作台。报缺料会让 LLM 去采根本不缺的东西，必须说清真实原因。
    if (anyReady && !craftingTable) {
        bot.chat(
            `I cannot make ${name}: ingredients are sufficient, but this is a 3x3 recipe and ` +
                `there is no crafting table within 32 blocks. Place one first — ` +
                `crafting_table takes 4 planks and works bare-handed.`
        );
        return;
    }
    // 同档时把「要不要工作台」在末尾统一说一次（混档已逐条标过，这里就不再重复）
    let tail = "";
    if (!mixedTableReq) {
        tail = allRequireTable
            ? "\n  * 3x3 recipe — needs a crafting table within 32 blocks (craftItem walks to it automatically)"
            : "\n  * 2x2 recipe — works bare-handed, no crafting table needed";
    }
    bot.chat(`I cannot make ${name} yet.\n  ` + lines.join("\n  ") + tail);
}

/** 降级：mc_tags.json 缺失或该产物不在其中时，回退到枚举 prismarine 展开的变体。 */
function _feedbackFromExpandedRecipes(bot, name, item, tagsMissing) {
    if (tagsMissing) {
        // 不静默降级：数据缺了要说，否则以后看日志会以为播报本来就这么啰嗦
        bot.chat(
            `[warn] mc_tags.json not loaded — falling back to enumerating recipe variants ` +
                `(run: python3 -m domains.minecraft.extract_tags)`
        );
    }
    const allRecipes = bot.recipesAll(
        item.id,
        null,
        mcData.blocksByName.crafting_table.id
    );
    const seen = new Set();
    const variants = [];
    for (const recipe of allRecipes) {
        const parts = [];
        let shortTotal = 0;
        for (const d of recipe.delta) {
            if (d.count >= 0) continue; // >=0 是产物
            const ingName = mcData.items[d.id].name;
            const need = -d.count;
            // 库存计数用 bot.inventory.count（**按 id 跨槽求和**）：findInventoryItem 只返回
            // 第一个匹配的格子，bot 有两摞 oak_planks（64 + 10）时会漏算。
            const have = bot.inventory.count(d.id, d.metadata);
            shortTotal += Math.max(need - have, 0);
            parts.push({ ingName, need, have, short: Math.max(need - have, 0) });
        }
        if (!parts.length) continue;
        const sig = parts.map((p) => `${p.need}x ${p.ingName}`).join(" + ");
        if (seen.has(sig)) continue;
        seen.add(sig);
        variants.push({ sig, parts, shortTotal });
    }
    if (!variants.length) {
        bot.chat(`I cannot make ${name}: no usable recipe found.`);
        return;
    }
    variants.sort((a, b) => a.shortTotal - b.shortTotal);
    const ready = variants.find((v) => v.shortTotal === 0);
    if (ready) {
        bot.chat(
            `I cannot make ${name} right now: ingredients are sufficient ` +
                `(${ready.sig}), but this recipe needs a crafting table within 32 blocks. ` +
                `Place one first — crafting_table takes 4 planks and works bare-handed.`
        );
        return;
    }
    const lines = variants.map((v) => {
        const missing = v.parts
            .filter((p) => p.short > 0)
            .map((p) => `${p.short} more ${p.ingName}`)
            .join(", ");
        const haveTxt = v.parts
            .map((p) => `${p.ingName}: ${p.have}/${p.need}`)
            .join(", ");
        return `  - ${v.sig}  → need ${missing}  (have ${haveTxt})`;
    });
    bot.chat(
        `I cannot make ${name} yet. ANY ONE of these ${variants.length} recipes works — ` +
            `pick whichever matches what you actually have:\n` +
            lines.join("\n")
    );
}
