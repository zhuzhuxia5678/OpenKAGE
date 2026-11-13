"""记忆图谱的 sqlite schema + 幂等迁移 + 检索调参常量。

Concept（稳定概念身份）+ Facet（多个内容切面）+ Edge（概念间关系）三层结构。
调参常量跟 backend/kg/config.py 的默认值对齐但独立维护（两套图谱分开调参，互不牵连）。

★ `concept.layer` = 层标记（`memory` / `experience`），见 LAYER_MEMORY / LAYER_EXPERIENCE。
老库若还没有这一列，`_migrate_topic_tag_to_layer` 负责幂等迁移。

⚠️ **别跟 `Task.topic_tag` 混**（`openkage/domain.py`）：那是**任务的知识点标签**
（math 的章节 / minecraft 的探索阶段），跟本表这一列毫无关系。

★ `facet` 的 `PRIMARY KEY (concept_slug, kind)` = **同一 Concept 下每种 kind 只有一条**。
它与「facet kind 词表放开、允许建议词表之外的 kind」是两件独立的事，容易混：放开的是
**取名范围**，不是"一个概念的某类知识可以记很多条"——同类多条意味着没概括干净，
该用 `update_facet` 合并。
"""

from __future__ import annotations

import logging
import sqlite3

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS concept (
    slug TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    summary TEXT NOT NULL,
    -- 层标记：'memory'（我的具体经历，永不聚合）/ 'experience'（可共享的结论，唯一被聚合的）。
    -- 由反思 Agent 在 create_concept 时显式声明（见 tools.CreateConceptTool）。
    layer TEXT NOT NULL,
    -- ★ **强度**：这个概念被**实际用到**的累计量，全库唯一的强度字段。
    -- 由**任务域 Agent 在检索时触发**，反思/聚合 Agent 完全碰不到它（详见下方设计说明）。
    -- REAL 而非 INTEGER：retrieve 命中只加 0.1，整数会把每次增量截断成 0。
    -- 初始 1.0 而非 0：`边得分 = 关系权重 × 落点强度`，若初始 0 则空图起步时**所有边得分恒为 0**
    -- （强相关与无关的边并列），softmax 退化成均匀分布 —— 而 minecraft 正是从空图起步，
    -- 那是常态不是边界情况。起点 1.0 让关系权重的排序在没人 expand 过时依然有效。
    strength REAL NOT NULL DEFAULT 1.0,
    -- ★ **记忆类别**（2026-09-12）：记忆层概念的四分类，供 `dump_memory_layer` 按桶配额注入
    --   （窗口不再按 updated_at 一把抓 —— 日程类以 4~5 个/天的写入速度天然霸榜，把营地/箱子
    --   这类"没人会想到去 retrieve、必须常驻"的持久概念挤出窗外，见 dump_memory_layer 的 docstring）。
    --   由反思 Agent 在 create_concept 时显式声明（见 tools.CreateConceptTool 与 CAT_* 常量）。
    --   **可空**：experience 层不填（它不进窗口，类别无意义）；老库旧行也空着 ——
    --   本字段没有降级启发式，没声明就是没有（2026-09-12 用户定案：全盘接受显式声明，不走前缀兜底）。
    category TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS facet (
    concept_slug TEXT NOT NULL REFERENCES concept(slug) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    -- ★ 联合主键 = **同一 Concept 下每种 kind 只能有一条**。
    -- 它与「kind 词表放开」是两件独立的事，别混：放开的是**取名不限于建议词表**，
    -- 唯一性约束照旧 —— 一个概念的某一类知识就该被概括成一条，多条同类是没概括干净。
    PRIMARY KEY (concept_slug, kind)
);
CREATE TABLE IF NOT EXISTS edge (
    id TEXT PRIMARY KEY,
    from_slug TEXT NOT NULL REFERENCES concept(slug) ON DELETE CASCADE,
    to_slug TEXT NOT NULL REFERENCES concept(slug) ON DELETE CASCADE,
    relation TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    UNIQUE(from_slug, to_slug, relation)
);
-- ★ **技能（可直接复用的 JS 代码）**：反思 Agent 把当天验证成功的 JS 代码段存成技能，
-- 执行期 retrieve 命中后把 js_code 拼进 programs 注入，Agent 的 write_code 代码里
-- `await skillX(bot, ...)` 直接可调（与 mineBlock 原语同机制）。区别于 facet.content 的
-- 文本经验（间接引导），技能是**直接可用**的代码。详见 plans/reflection_js_skills.md。
-- 产出门槛（prompt 端把关）：只存当天执行成功且达成意图的，verified 默认 1。
-- 聚合广播：consolidate 只汇总 verified=1 的技能到主脑公共池，未验证留私有。
CREATE TABLE IF NOT EXISTS skill (
    slug TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    -- 何时该用（自然语言）——检索靠它做向量匹配，写得越具体越易被命中
    intent TEXT NOT NULL,
    -- 可注入 programs 的 JS（含 async function 定义），执行期原样拼进 programs
    js_code TEXT NOT NULL,
    -- 调用签名 "mineIron(bot, count=5)"，注入 prompt 给 Agent 看怎么调
    signature TEXT NOT NULL,
    -- 前置条件文本（如"需要石镐及以上"）
    preconditions TEXT,
    -- 是否验证成功（门槛在产出端：反思只存成功的，默认 1）。聚合只广播 verified=1。
    verified INTEGER NOT NULL DEFAULT 1,
    success_count INTEGER NOT NULL DEFAULT 0,
    fail_count INTEGER NOT NULL DEFAULT 0,
    -- ★ 技能修订归档（2026-08-29 两阶段反思；2026-08-30 修订 total_* 语义）：
    --   success_count/fail_count = 「当前代码版本评价窗口」，update_skill 改 js_code 时归零。
    --   total_success_count/total_fail_count = 「所有版本成败全程累计，永不清零」，由
    --   add_usage_counts 在执行期每次回填都累加（不是 revise 时才归档）。阶段2 据此看
    --   「改进后窗口效果」+「历史全程可靠性」。revise_count = 改过几次代码。
    total_success_count INTEGER NOT NULL DEFAULT 0,
    total_fail_count INTEGER NOT NULL DEFAULT 0,
    revise_count INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
"""

# ═══════════════════════════════════════════════════════════════════════════════
# 强度机制
# ═══════════════════════════════════════════════════════════════════════════════
#
# 只有 concept 有强度，facet 与 edge 都没有。强度表达"这个概念被实际用到过"，
# 只由任务域 Agent 触发，反思/聚合 Agent 的工具一律不写它（写的人给自己打分没有意义）：
#   · `expand(slug)` 命中 → + gain_expand    Agent 花一步刻意深挖，高精度信号
#   · `retrieve` 命中     → + gain_retrieve  仅说明向量语义相近；检索无阈值、总给 top_k，
#                                            完全无关的也会返回，故只给十分之一
#
# ★ **两个增益值在 yml**（`memory_graph.strength.*`，经 RetrievalConfig 进 BrainGraph）——
# 它们是运行期旋钮，与 anchor/expand 那些检索参数同类，python 侧不留第二份默认值。
# 而 `STRENGTH_INIT` 留在这里：它是**建表 DDL 的一部分**（下面 _SCHEMA 的 DEFAULT 1.0 与
# _migrate_strength 的 ADD COLUMN 都用它），且算法要求它 > 0 —— 若为 0 则空图起步时所有边
# 得分恒为 0、softmax 退化成均匀分布（minecraft 正是从空图起步）。那是结构约束不是旋钮。
#
# 强度只增不减：被用到的升上去、没用到的原地不动，相对排名就是遗忘，不需要衰减/归档机制。
#
# 强度怎么参与检索：见 `graph.BrainGraph._expand`——
# 边得分 = 关系语义权重 × 落点概念强度 → softmax → 按概率采样决定走哪些边；跳数由 Agent 自己定
# （有上限）。
STRENGTH_INIT = 1.0


#: 层标记的两个合法值，存在 `concept.layer` 列里。
#: **由反思 Agent 在 create_concept 时显式声明** —— 见 tools.CreateConceptTool 与 layer_of_kind。
LAYER_MEMORY = "memory"
LAYER_EXPERIENCE = "experience"

#: 记忆层概念的**类别**（`concept.category` 列，2026-09-12）。四分类映射到记忆的衰减性质，
#: 各桶在 `dump_memory_layer` 的注入窗口里有独立预算（yml 的 `memory_graph.memory_dump_budgets`）：
#:   · `episodic`（情景）—— 时间锚：`day_N` 及当天子节点（blocker/unfinished/发现线索）。
#:     衰减快：未完成线程实测拖 1~4 天就解决或失效。
#:   · `spatial`（空间）—— 持久资产：营地、箱子、矿井、工作台。衰减慢（挖空才失效），
#:     且**没人会想到去 retrieve 它** —— 「我的箱子在哪」与当下意图语义不匹配，必须常驻。
#:   · `lesson`（教训）—— 事件/遭遇：`death_*` 及同类。价值不随天衰减，近期最相关。
#:   · `status`（状态）—— 跨天滚动的自我模型：`current_state` / `*_progress` / `*_backlog`。
#:     每个 agent 实测仅 1~2 个，天天覆盖更新，本质是"我今天是谁、做到哪了"。
#: **只对 memory 层有意义**：experience 层走聚合-清空循环、不进注入窗口，create_concept 不收。
#: 由反思 Agent 显式声明（enum 校验），**没有前缀启发式兜底**（2026-09-12 用户定案）——
#: 漏填/乱填会被工具拒掉重来，而不是被代码猜一个。
CAT_EPISODIC = "episodic"
CAT_SPATIAL = "spatial"
CAT_LESSON = "lesson"
CAT_STATUS = "status"
CATEGORIES = (CAT_EPISODIC, CAT_SPATIAL, CAT_LESSON, CAT_STATUS)


def layer_of_kind(kind: str, default: str = LAYER_MEMORY) -> str:
    """这个 facet kind **典型属于**哪一层。

    ⚠️⚠️ **2026-08-09 起本函数恒返回 `default`** —— kind 受控词表已删除（放开为自由命名，
    见 `prompts/reflect/reflect.yaml` 顶部），映射表 `REFLECT.kind_layers` 因此是空的。
    两个用途一个消失、一个仍然正确：
      · **一致性校验已随之删除**（原 `tools._layer_kind_conflict`，见那处注释）——
        `my_note` 属于哪一层，没有任何静态信息能回答。这是知情的能力回退。
      · **老库迁移仍然正确**：那条路径本来就允许"词表外的 kind 落到 `default`"，
        现在只是所有 kind 都走那条兜底 —— 而 `default` 由调用方按图谱类型给
        （私有=记忆层 / 公共=经验层），对整库一次性判定来说这仍是唯一可用的依据。
    词表填回 yaml 后本函数自动恢复原行为，无需改代码。

    ⚠️ **这从来不是"层由 kind 决定"** —— 层是 concept 级属性，由**反思 Agent 在
    create_concept 时显式声明**（合法值取 `prompts.layers`，那一项**仍然受控**）。
    不能反过来"从 kind 推出层"：**层是 concept 级的，kind 是 facet 级的** —— 一个概念可以有
    多条不同 kind 的 facet，用其中某一条去决定整个概念的层，只在"一个概念只有一个 facet"时
    成立，否则后续再挂的不同 kind facet 会被错误归层、静默漏聚合。
    """
    from openkage.memory.prompt_store import REFLECT

    return REFLECT.kind_layers.get(kind, default)


def _migrate_table_names(conn: sqlite3.Connection) -> list[str]:
    """老库迁移：表名去前缀 `memory_concept/facet/edge` → `concept/facet/edge`。

    ★ **必须在 `executescript(_SCHEMA)` 之前调** —— 这是本函数唯一容易出错的地方：
    `CREATE TABLE IF NOT EXISTS concept` 在老库上会**新建一张空表**，而老数据还躺在
    `memory_concept` 里。之后所有查询都打在空表上：568 个概念变成 0 个，**不报任何错**
    （读起来就是"这个图谱是空的"）。实测确认过这个行为，所以调用顺序不是风格问题。

    幂等：新名已存在就跳过（同时存在两套时**不动老表**、只告警 —— 那种库是上面那个
    顺序错误的产物，合并数据需要人来决定，不该由迁移代码猜）。
    返回改了哪几张表，供日志。

    ⚠️ **向量库不在这里**：`vector_store` 的 `COLLECTION_CONCEPT` / `vec_concept` 是数据值与
    它自己的虚表名，与这三张表无关。它们**2026-08-10 单独改过一次**（也去掉了 `memory_`
    前缀），走的是另一条路 —— vec0 有 4 张影子表要一起改名、collection 值还参与 rowid 的
    sha1，且**旧库不自动迁移**（要显式跑 `scripts/migrate_vec_db.py`）。详见那边的注释。
    """
    have = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    done = []
    for old, new_name in (("memory_concept", "concept"), ("memory_facet", "facet"),
                          ("memory_edge", "edge")):
        if old not in have:
            continue
        if new_name in have:
            logger.warning(
                "表名迁移：%s 与 %s **同时存在** —— 不动老表。这通常意味着某次打开库时 "
                "_SCHEMA 先于本迁移执行（那会建出空的新表），老数据还在 %s 里。"
                "怎么合并要人来定，迁移代码不猜", old, new_name, old)
            continue
        conn.execute(f"ALTER TABLE {old} RENAME TO {new_name}")
        done.append(f"{old}→{new_name}")
    if done:
        conn.commit()
    return done


def _migrate_time_columns(conn: sqlite3.Connection) -> list[str]:
    """老库迁移：`created_round`/`updated_round` → `created_at`/`updated_at`（三张表）。

    ★ **为什么改名**（2026-08-08 用户定案）：`round` 这个名字只对**主脑**成立
    （round = 一次 consolidation = 一个 (epoch,batch,window)，见 openkage.roundcode）。
    副脑图谱里它承载的是"第几天"——
    一条轨迹几天、每天反思一次。同一个列名指两种刻度，读代码的人只能靠猜。
    改成中性的 `_at`：**列名统一、语义按图谱类型分开**（主脑写 round，副脑写天数），
    这比"两套列名 + 共用 SQL 到处拼列名"便宜得多（那要动 50+ 处 SQL）。

    幂等：已是新名就跳过。走 `ALTER TABLE ... RENAME COLUMN`（SQLite ≥3.25）。
    ⚠️ 依赖表名已经是新的 —— 调用顺序：`_migrate_table_names` → 本函数 → `executescript`。
    """
    done = []
    for table in ("concept", "facet", "edge"):
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if not cols:
            continue  # 表还不存在（全新库），_SCHEMA 会建好
        for old, new_name in (("created_round", "created_at"), ("updated_round", "updated_at")):
            if old in cols and new_name not in cols:
                conn.execute(f"ALTER TABLE {table} RENAME COLUMN {old} TO {new_name}")
                done.append(f"{table}.{old}→{new_name}")
    if done:
        conn.commit()
    return done


def _migrate_topic_tag_to_layer(conn: sqlite3.Connection, default_layer: str) -> int:
    """老库迁移两件事：**① 列改名** `topic_tag` → `layer`；**② 值改写**成层标记。

    幂等（两步各自幂等）：列已叫 `layer` 就跳过改名；值已是 LAYER_* 的行不动。
    返回改写了几行值（供日志）。

    ① **改名**：走 `ALTER TABLE ... RENAME COLUMN`（SQLite ≥3.25）。
       ⚠️ 与 `Task.topic_tag`（任务的知识点标签）**无关**。

    ② **值改写**：**逐概念按它自己的 facet kind 判层** —— 一份图谱可能同时装两层
       （私有图谱的反思 Agent 一个人写记忆 + 经验），不能整库刷成同一个值。这里用
       `layer_of_kind` 反推是**合理的**：历史库根本没有层标记，只有 kind 可依据，且这是整库
       一次性判定，不存在"概念后来又挂了别层 facet"那个粒度问题（见 layer_of_kind 的
       docstring）。词表外的 kind 落到 `default_layer`，由调用方按图谱类型给。
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(concept)").fetchall()}
    if not cols:
        return 0  # 表还不存在（全新库），_SCHEMA 会建好
    # ── ① 列改名（幂等）──
    if "layer" not in cols:
        if "topic_tag" not in cols:
            raise RuntimeError(
                "concept 既没有 `layer` 也没有 `topic_tag` 列 —— 这个库的结构不认识，"
                "拒绝继续（避免把层标记写进不存在的列）"
            )
        conn.execute("ALTER TABLE concept RENAME COLUMN topic_tag TO layer")
        conn.commit()
        logger.info("concept: 列 topic_tag 已改名为 layer")
    # ── ② 值改写（幂等）──
    rows = conn.execute(
        "SELECT c.slug, (SELECT f.kind FROM facet f WHERE f.concept_slug = c.slug LIMIT 1) "
        "FROM concept c WHERE c.layer NOT IN (?, ?)",
        (LAYER_MEMORY, LAYER_EXPERIENCE),
    ).fetchall()
    n = 0
    for slug, kind in rows:
        layer = layer_of_kind(kind, default=default_layer) if kind else default_layer
        conn.execute("UPDATE concept SET layer = ? WHERE slug = ?", (layer, slug))
        n += 1
    conn.commit()
    return n


def _migrate_strength(conn: sqlite3.Connection) -> dict:
    """迁移到「只有 concept 有强度」：给 concept 加 `strength`，删掉 facet 的
    `reinforcement_count`/`last_touched_round`/`archived` 与 edge 的 `support_count`。
    幂等（每步各自查 PRAGMA 再决定动不动），返回做过哪些动作供日志。

    老库的强度信息会丢，这是有意的：旧字段承载的语义是"反思/聚合 Agent 自己声称印证了几次"，
    新语义是"任务域 Agent 实际用到过几次"——两者不是同一个量，数值换算只会把旧口径的偏差带进
    新机制。所有概念一律从 `STRENGTH_INIT` 起步，强度由训练过程重新长出来。

    ⚠️ `ALTER TABLE ... DROP COLUMN` 需要 SQLite >= 3.35，且该列不能被索引/约束引用。
    低于该版本的 SQLite 上会抛错，需改走重建表迁移。
    """
    done: dict = {}
    ccols = {r[1] for r in conn.execute("PRAGMA table_info(concept)").fetchall()}
    if ccols and "strength" not in ccols:
        conn.execute(
            f"ALTER TABLE concept ADD COLUMN strength REAL NOT NULL DEFAULT {STRENGTH_INIT}")
        done["concept.strength"] = "added"
    for table, col in (("facet", "reinforcement_count"),
                       ("facet", "last_touched_round"),
                       ("facet", "archived"),
                       ("edge", "support_count")):
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if cols and col in cols:
            conn.execute(f"ALTER TABLE {table} DROP COLUMN {col}")
            done[f"{table}.{col}"] = "dropped"
    if done:
        conn.commit()
    return done


def _migrate_skill_revision_counts(conn: sqlite3.Connection) -> dict:
    """给 skill 表加修订归档三列：`total_success_count` / `total_fail_count` / `revise_count`。

    幂等（PRAGMA 查列再决定加不加），返回做过哪些动作供日志。照 `_migrate_strength` 模板。

    ★ **SkillStore 专用**（2026-08-29）：SkillStore 只用 skill 表、此前不跑任何迁移；这是首个
      skill 专用迁移。BrainGraph 复用同一份 _SCHEMA、它的 __init__ 跑的是 concept/facet/edge
      迁移，跑本函数也无害（skill 表存在才加列，存在与否都安全）。

    老库的 skill 行新列默认 0。语义（2026-08-30 修订）：total_* 由 add_usage_counts 在执行期
    全程累加（所有版本成败之和，永不清零），success_count/fail_count 是当前版本评价窗口。
    老库此前累计的计数只存在 success_count/fail_count 里（视为当前窗口计数），不回填进 total_*——
    修复上线后新发生的回填会正常累加进 total_*，历史窗口计数在下次 revise 归零时不再搬进 total_*
    （避免双计），故 total_* 从 0 起步、只计后续累计。
    """
    done: dict = {}
    cols = {r[1] for r in conn.execute("PRAGMA table_info(skill)").fetchall()}
    if cols:  # skill 表已建（全新库由 _SCHEMA 建好，无需迁移）
        for col in ("total_success_count", "total_fail_count", "revise_count"):
            if col not in cols:
                conn.execute(
                    f"ALTER TABLE skill ADD COLUMN {col} INTEGER NOT NULL DEFAULT 0")
                done[f"skill.{col}"] = "added"
    if done:
        conn.commit()
    return done


def _migrate_category(conn: sqlite3.Connection) -> bool:
    """给 concept 表加 `category` 列（记忆类别的四分类，见 CAT_* 常量）。

    幂等（PRAGMA 查列再决定加不加），返回是否加了列（供日志）。照 `_migrate_strength` 模板。

    ★ **旧行不回填**（2026-09-12 用户定案"全盘接受、不走降级链"）：category 只由反思 Agent
      显式声明，没有"按 slug 前缀猜类别"的启发式 —— 老库的概念要么是已完成 run 的分析数据
      （不进新注入窗口），要么被 `create_concept` 撞名即拒挡住（tools.py 不给已有概念改层/类别的口）。
      旧行 category 恒 NULL，新 run 全量新库从第一天起就带值。

    ⚠️ **只影响注入窗口的选人**，不影响检索：`dump_memory_layer` 对 NULL 类别的概念直接跳过
      （无类别的记忆不进常驻窗口，retrieve/expand 照常能取到 —— 那条路不看 category）。
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(concept)").fetchall()}
    if not cols or "category" in cols:
        return False
    conn.execute("ALTER TABLE concept ADD COLUMN category TEXT")
    conn.commit()
    return True