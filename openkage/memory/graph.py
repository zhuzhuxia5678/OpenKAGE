from __future__ import annotations

import asyncio
import logging
import math
import random
import sqlite3
import sys
import threading
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent.parent.parent / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from openkage.memory._common import (  # noqa: E402
    _concept_relations,
    _concept_row_to_dict,
    _format_concepts_rich,  # 本模块 _dump_layer 用；不再有外部 import
    _slugify,  # noqa: F401  re-export：domains/_kg_seed.py 从 graph 导入
    fuse_part_hits,
)
from openkage.config import RetrievalConfig  # noqa: E402  检索参数唯一来源（yml），见其 docstring
from openkage.agent_io import ContextDumper, dump_reasonings  # noqa: E402  反思/聚合的上下文留痕（默认开）+ reasoning 观测落盘
from openkage.memory.schema import (  # noqa: E402
    STRENGTH_INIT,
    LAYER_EXPERIENCE,
    LAYER_MEMORY,
    _SCHEMA,
    _migrate_category,
    _migrate_strength,
    _migrate_table_names,
    _migrate_time_columns,
    _migrate_topic_tag_to_layer,
)
from openkage.memory.prompt_store import AGGREGATE, REFLECT  # noqa: E402
from openkage.memory.subgraph import MemorySubgraph  # noqa: E402
from openkage.memory.tools import _build_tools  # noqa: E402

logger = logging.getLogger(__name__)


#: `_expand` 里给 `math.log(strength)` 兜的下限。强度**设计上**恒 >= STRENGTH_INIT(1.0)
#: （只增不减），但老库/手改过的库里可能出现 0 甚至负值，那会让 log 抛 ValueError/返回 -inf、
#: 把整条检索链带崩。夹到一个极小正数：这类脏数据表现为"最不值得走"，而不是崩溃。
_MIN_STRENGTH = 1e-6


#: 全景**记忆层**撞护栏时拼给 Agent 看的那行（`_dump_layer` 用；没撞顶时不出现）。
#: 护栏原先只打 warning，模型侧完全无感 —— 它拿到一份被砍掉最老概念的清单，而 user_input
#: 紧接着断言「全景是完整的」。本次 30 夜 run 里 `dump_layer_index` 撞顶 15 次（记忆层 152→186）。
#: 这里只陈述事实（列了几个 / 实际几个）；「那就该改用 search_concepts」写在 reflect.yaml，
#: 不在这儿重说一遍。
#: ★ **必须点明「记忆层」并说清经验层是完整的**（2026-08-15 护栏按层解耦后）：护栏现在只管
#:   记忆层（见 `_dump_layer`），而这行出现在**两层全景**的开头 —— 不说是哪一层，Agent 只能
#:   假设经验概念也可能被省略，于是对一份**完整**的清单也去做多余的 `search_concepts`；
#:   而"经验层没列出来的就是真没有、可以直接建"这个判断本来是确定的，不该被这行搞含糊。
#: ⚠️ **只服务索引形态**：正文形态（`dump_layer_full`）唯一的消费者是聚合读副脑经验层，
#:   那层每窗口清空、峰值 30 个概念、6 份 run 日志里撞顶 0 次 —— 不给不可达分支写文案。
#:   护栏解耦后它更是**结构上**撞不到了（经验层不设上限），见 `_dump_layer`。
_TRUNCATED_PANORAMA_NOTE = (
    "⚠️ 下面的**记忆层**只列了最近被活动过的 {n_shown} 个，实际有 {n_total} 个 —— "
    "较久未被碰过的没列出来（按 updated_at 倒序取前 N）。（经验层是完整的。）\n\n"
)


def _count_steps(result) -> int:
    """一次 ReAct 运行实际跑了几步（= 几轮 LLM 调用）。

    ★ **不能用 `len(result.history)`**：history 装的是消息，一步会往里塞
    1 条 assistant + N 条 tool result（N = 这步的 tool_calls 数），外加开头 1 条 user ——
    这会让计出来的步数明显大于真实值，跟 `max_reflect_steps` 对不上。
    步数 = assistant 消息条数，这才是「循环转了几圈」。
    """
    return sum(1 for m in (result.history or []) if m.get("role") == "assistant")

# 聚合（consolidate）用的**完整独立** system prompt，来源 `prompts/aggregate/aggregate.yaml`，
# 装载走 AgentPrompts 类。跟反思 prompt 同一范式（同一套 10 个窄工具），差别在于：
# 输入是 N 份副脑**已经归纳好的经验层**（看不到记忆层），任务是跨副脑归并 + 一致性加权 + 冲突消解。
_AGGREGATE_SYSTEM_PROMPT = AGGREGATE.system

# ★ 图谱层认识的口径值（2026-09-24 提为模块级常量：装配层与测试要能直接查，不必真构造图谱）。
#   `no_shared_graph` 的拆除点在 brain.py 装配层（主脑 NullGraph + 聚合 no-op），图谱层不收。
#   ★ `no_exp_skills` **要收**（2026-09-26 修）：拆除点确实在 skill_store.py（NullSkillStore），
#     图谱层的检索/写入对它无分支——但**反思 prompt 的 append_line 住在 ArmGraph 里**
#     （`_reflect_system_prompt` 读 `self._ablation`），不收这个值，yaml 里那行
#     「本运行不收集技能经验」就是死代码：−K run 的反思 Agent 照着 §4 去撞 create_skill，
#     拿到 NullSkillStore 的「对照组无技能库」、白烧反思步数。
GRAPH_ABLATION_VALUES = {None, "flat_retrieval",
                         "no_memory_layer", "no_exp_text", "no_exp_layer",
                         "no_exp_skills", "raw_shared_pool"}

# ★ 走「整节替换 §1」的 rung（2026-09-26 起**为空**——用户定消融提示词一律只在末尾追加一行，
#   改整节就动了实验前提）。保留这个常量是为了让替换通道不被删死：需要时把 rung 名加回来即可，
#   yaml 里对应写 `section_1_replacement`。
_SECTION1_REPLACEMENT_RUNGS: tuple = ()


class BrainGraph:
    """主脑图谱：CRUD 全部生效，检索是独立实现的（`retrieve_subgraph`），
    反思/聚合由一个小 ReActAgent 完成。"""

    #: 本图谱用**哪个 Agent** 的受控词表（见 tools._VOCABS / relation_semantics._PROBE_TABLES）。
    #: 基类 BrainGraph = 公共图谱 → "aggregate"（只有经验侧 kind）；
    #: 子类 ArmGraph = 私有图谱 → "reflect"（**两层 8 种 kind 都能用**）。
    #: 它决定三件事：①工具的 kind/relation enum ②图扩展用哪套 probe 打分
    #: ③反思/聚合 prompt 里注入哪套词表说明。
    #:
    #: ⚠️ **它不决定 layer**：层由 Agent 在 `create_concept` 时显式声明 ——
    #: 私有图谱一个 Agent 同时写两层，"一份图谱一个层"不成立。
    #:
    #: ★ **必须是类属性、不能只在 __init__ 里赋值**：ArmGraph 在
    #: `super().__init__()` **之后**才设它，而基类 __init__ 里的层迁移就要用到默认层 ——
    #: 写成实例属性会让私有老库按公共图谱的默认层迁移，层标记全错。
    _vocab: str = "aggregate"

    #: 老库迁移时，kind 不在词表里的历史概念（种子库的"严格定义"等）落到哪一层。
    #: 公共图谱的种子是教材先验 = 经验层；私有图谱由 ArmGraph 覆写为记忆层。
    _default_layer: str = LAYER_EXPERIENCE

    #: 本图谱上跑的 Agent 属于哪个**模型角色**（`openkage.config.MODEL_ROLES` 之一）——
    #: 决定它取哪个 `sdk.timeout` 与 `reasoning_effort`（两者都按角色分，见 yml `llm:`）。
    #: 主脑图谱上跑的是**聚合** Agent；ArmGraph 覆写成 `reflect`（那上面跑的是反思）。
    #: ★ 与 `_vocab` 同为类属性、同一个理由：两者描述的是同一件事的两面 ——
    #:   "这张图谱上跑的是哪个 Agent"，所以放在一起、别让它们分叉。
    _model_role: str = "consolidate"

    def __init__(
        self,
        db_path: Path,
        vec_db_path: Path | None = None,
        model_name: str | None = None,
        *,
        retrieval: RetrievalConfig | None = None,
        context_dir: "Path | None" = None,
        ablation: "str | None" = None,
    ):
        """`retrieval` 不传则从 `configs/openkage_default.yml` 加载（`RetrievalConfig.load()`）。

        ★ **检索参数没有 python 侧默认值**（2026-08-08 用户定案）：全部真相在 yml。
        此前是「schema.py 模块常量当默认值 + yml 显式传收紧值」的双轨制，而那**造成过一个
        静默分叉**：`brain.py` 只给主脑逐字段传了 yml 的值，`ArmGraph(eg, vec_db_path=…,
        model_name=…)` 一个检索参数都没传 —— 于是 08-06 那次「min_score 0.60→0」的定案
        只作用于主脑，**副脑一直在用 schema.py 里的 0.45**。两个图谱用不同检索口径跑了两天，
        没有任何地方报错。现在没有第二个值可分叉：不传 = 读同一份 yml。

        `context_dir`：反思/聚合 Agent 的**上下文留痕目录**（不传 → 落在 db 旁边的
        `contexts/`，见 `self._context_dir`）。每次 reflect/consolidate 会把真正发给 LLM 的
        完整 messages 写一份进去 —— 那是复现与诊断所需的全部信息（详见 `openkage.agent_io`）。

        `ablation`（2026-09-18 leave-one-out 消融，run_ablation.py 专用；None=非消融 run）：
          · flat_retrieval —— `retrieve_subgraph` 降级平面 top-k：向量锚定即止、不走 `_expand`
            边扩展、不给 inner/outer edges（结构信息整段拿掉），渲染走 `render_context` 平文本。
            知识照写照聚合（写入路径完全不变），只有读取降级 —— 与 RAG baseline 的差异
            收敛为「检索算法」单一变量。
            检索照常子图。主脑侧专用（私有 ArmGraph 不收此值的行为分支）。
          · no_memory_layer —— `create_concept` 拒绝 memory 层（反思写不进）+ `dump_memory_layer`
            返 None（晨间注入消失）+ `dump_layer_index/full` 的 memory 段为空。
            经验层/聚合/技能全照常。
          · no_exp_text —— `create_concept` 拒绝 experience 层（经验文本写不进，经验层恒空）。
            记忆层/聚合（自然空转）/技能全照常。
          · no_exp_skills —— 检索/写入**无分支**（拆除点在 skill_store.py 的 NullSkillStore）；
            收它只为 ArmGraph 的反思 prompt append_line 能渲染出来。
          · no_shared_graph —— 图谱层不收（拆除点在 brain.py 装配层：主脑 NullGraph + 聚合 no-op）。
        """
        db_path.parent.mkdir(parents=True, exist_ok=True)
        #: 上下文留痕目录。**默认就落盘、不需要任何开关**：此前 minecraft ReAct 的留痕藏在
        #: 一个默认关闭的 CLI flag 后面，结果 2026-08-10 那轮 30 夜实验跑到第 7 天才发现
        #: 一个字都没存；而反思/聚合从来就没有过留痕（各自几十步、几十 KB 上下文，
        #: 跑完只在日志里留一句话总结）。
        #: 不传时**落在 db 旁边**（`<db 所在目录>/contexts/`）而不是某个全局路径：
        #: 图谱库已经在 run 目录下了，留痕跟着它走就自动进同一次 run 的产物里 ——
        #: 不需要把 run 目录再从调用链上传一遍（那是第二个可以分叉的来源）。
        self._context_dir = Path(context_dir) if context_dir else db_path.parent / "contexts"
        #: 留痕文件名的前缀 = db 的 stem（`brain` / `arm_alice_e1b1`）——
        #: 于是「哪个图谱的哪次反思」在文件名上就分得开，不用另起一套命名。
        self._context_tag = db_path.stem
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        # ★ **必须显式开 FK**：SQLite 的 `PRAGMA foreign_keys` 默认 **OFF**，而且是**连接级**
        # 设置 —— 不开的话 schema 里三处 `ON DELETE CASCADE` 只是装饰：`del_concept` 删掉概念行
        # 后 facet 与边会变成孤儿，孤儿边仍会渲染进 prompt，让 Agent 沿一条死边去 get_concept。
        # 必须在 executescript 之前设（pragma 不受随后的隐式 commit 影响）。
        # ⚠️ 开了它之后，**改 slug 的路径要用 `PRAGMA defer_foreign_keys`**：先 UPDATE 父行
        # 会瞬时孤立子行、当场 IntegrityError，见 tools.UpdateConceptTool。
        self._conn.execute("PRAGMA foreign_keys=ON")
        #: 序列化对 `self._conn` 的访问。**读也必须持它**，不只是写 —— 见 `_query` 的 docstring。
        self._lock = threading.Lock()
        # ★ **表名迁移必须在 executescript 之前**：`CREATE TABLE IF NOT EXISTS concept` 在老库
        #   （表名还叫 memory_concept）上会**新建一张空表**，之后所有查询都打在空表上 ——
        #   568 个概念变成 0 个且不报任何错。顺序错了是静默的，别调换这两行。
        _rn = _migrate_table_names(self._conn)
        if _rn:
            logger.info("表名迁移：%s 的 %s（去掉 memory_ 前缀）", db_path.name, "、".join(_rn))
        # 列名迁移紧跟其后、同样在 executescript 之前（它依赖表名已是新的）：
        # created_round/updated_round → created_at/updated_at。`round` 只对主脑成立，
        # 副脑那边装的是「第几天」—— 见 _migrate_time_columns 的 docstring。
        _tc = _migrate_time_columns(self._conn)
        if _tc:
            logger.info("时间列迁移：%s 的 %s", db_path.name, "、".join(_tc))
        self._conn.executescript(_SCHEMA)
        # 强度迁移：concept 加 strength，删掉 facet 的 reinforcement_count/
        # last_touched_round/archived 与 edge 的 support_count。幂等，详见那边的 docstring。
        _m = _migrate_strength(self._conn)
        if _m:
            logger.info("强度迁移：%s 的字段调整 %s（强度一律从 %.1f 重新长起）",
                        db_path.name, _m, STRENGTH_INIT)
        self._conn.commit()
        # 老库的 topic_tag 历史值（game_mechanics / math / …）改写成层标记。幂等、已是层标记则不动。
        # 逐概念按它自己的 facet kind 判层（一份图谱可能同时装两层）；kind 不在词表里的历史概念
        # 落到 self._default_layer（类属性，ArmGraph 覆写 —— 见那里的注释）。
        _n_migrated = _migrate_topic_tag_to_layer(self._conn, self._default_layer)
        if _n_migrated:
            logger.info("层标记迁移：%s 的 %d 个概念已写入 layer（未知 kind 落 %r）",
                        db_path.name, _n_migrated, self._default_layer)
        # 类别列迁移（2026-09-12）：concept 加可空 category，记忆层概念的四分类。
        # 旧行不回填（见 _migrate_category 的 docstring），新 run 从第一天起由反思 Agent 显式声明。
        if _migrate_category(self._conn):
            logger.info("类别列迁移：%s 的 concept 已加可空列 category（记忆层四分类，旧行为 NULL）",
                        db_path.name)
        # ★ 检索参数**唯一来源是 yml**（见 __init__ docstring 里那次静默分叉）。
        #   `retrieval=None` → 读 configs/openkage_default.yml（带进程级缓存，不会每建一个
        #   ArmGraph 就重读一次文件）。想让某个实例用别的口径，显式构造 RetrievalConfig 传进来。
        cfg = retrieval if retrieval is not None else RetrievalConfig.load()
        self._retrieval = cfg
        self._anchor_topk = cfg.anchor_topk
        self._anchor_min_score = cfg.anchor_min_score
        self._anchor_gap_ratio = cfg.anchor_gap_ratio
        #: 分块索引的过取倍数：一个概念最多占 `1 + facet_max_per_concept` 条分片行，所以想稳拿
        #: top_k 个**不同概念**，最多得取这么多行。见 `search_concept_hits`。
        self._overfetch_mult = 1 + cfg.facet_max_per_concept
        self._expand_max_hops = cfg.expand_max_hops
        self._expand_top_p = cfg.expand_top_p
        self._expand_temperature = cfg.expand_temperature
        # 强度增益也走同一份 yml（此前是 schema.py 的模块常量，与 yml 无关联）。
        self._strength_gain_expand = cfg.strength_gain_expand
        self._strength_gain_retrieve = cfg.strength_gain_retrieve
        # 三个 dump 共用的失控护栏（不是常规截断）。**存成实例属性、不做方法入口参数**：
        # 它是"这份图谱一次最多注入多少个概念"这个属性本身，不是每次调用要商量的事 ——
        # 给每个 dump 都加一个 max_concepts 形参，等于把同一个值在三处签名里各暴露一遍，
        # 而没有任何调用点需要逐次覆盖它（2026-08-08 用户定案）。要改就改 yml。
        self._memory_dump_max_concepts = cfg.memory_dump_max_concepts
        # 记忆层**正文注入**（dump_memory_layer）的桶预算（2026-09-12）：按概念类别分桶，
        # 各桶独立 updated_at 倒序。只管正文注入 —— 上面的单值护栏仍管 _dump_layer
        # （反思全景是索引形态，便宜且带防重建告知行，没必要分桶）。
        self._memory_dump_budgets = dict(cfg.memory_dump_budgets)
        # 步数预算也走同一份 yml（此前 max_reflect_steps 在签名里硬编码 30，而 yml 另有一份）。
        # ★ 两个预算都**没有 __init__ 形参**：原先各有一个 `max_*_steps=None` 的覆盖口，
        #   没有任何调用点用得上（brain.py 那处传的是它自己算的自动值，已随自动公式一起删），
        #   留着就是"第二个值可以从哪进来"的入口 —— 正是本文件 docstring 里那次静默分叉的形状。
        self._max_reflect_steps = cfg.max_reflect_steps
        # 聚合**单独一份步数预算**：它与反思的工作量根本不同 —— 反思消化 1 条轨迹，聚合要融合
        # N 份副脑图谱（N=arm 数），步数需求比反思大，共用一份预算容易被打满而没能建边
        # （只顾着建 concept/facet，manage_edge 一次没调）。没有 edge，检索的「关系语义驱动图
        # 扩展」整段失效，只剩向量锚定 —— 所以这不是省几个 token 的事。
        # 它就是 yml 里那个数，**没有"填 0 则按 arm 数自动算"的兜底**（那条路曾存在、还分叉成
        # 两份不一致的公式，而 yml 一直显式给值，两条都没被执行过；2026-08-09 删）。
        self._max_consolidate_steps = cfg.max_consolidate_steps
        self._model_name = model_name
        # ★ leave-one-out 消融（2026-09-18）：图谱层拆除点。取值与语义见 __init__ docstring；
        #   非法值在此报错（写错 rung 名静默跑成 Full 是最贵的失败模式）。
        if ablation not in GRAPH_ABLATION_VALUES:
            raise ValueError(
                f"ablation={ablation!r} 不是图谱层认识的 rung（合法：{sorted(v for v in GRAPH_ABLATION_VALUES if v)}）；"
                f"no_shared_graph 的拆除点在 brain.py 装配层（主脑 NullGraph + 聚合 no-op），"
                f"图谱层不收这个值。")
        self._ablation = ablation
        # self._vocab 是**类属性**（见类定义处）—— 这里不再赋值，否则 ArmGraph 的覆写会被冲掉。
        # 当前 round：新建/更新的行据此打 created_at / updated_at。训练循环每 batch 前调
        # set_current_round(roundcode.encode(epoch,batch[,window])) 设置；不设置则恒为 0。
        self._current_round = 0

        # ★ 自包含 embedder（openkage/embedder.py，不再 from kg.embedder 依赖 backend/，
        #   见 openkage/embedder.py 顶部「为什么复制」）。
        from openkage.embedder import get_embedder

        from .vector_store import MemoryVectorStore

        self._vec_store = MemoryVectorStore(
            get_embedder(), vec_db_path or (db_path.parent / "memory_vec.db")
        )
        # ★ **技能归属重构（2026-08-27）**：技能不再存本图谱，改存各臂常驻 SkillStore。
        #   ArmGraph 由 brain.py 构造时注入本臂 SkillStore + 共享 SkillRegistry；BrainGraph
        #   （主脑）不持有技能，这两个恒 None（聚合不再有技能工具，见 tools._build_tools）。
        #   reflect() 把它们传给 _build_tools，反思期技能工具绑 SkillStore（非本图谱 conn）。
        self._skill_store = None
        self._skill_registry = None

    # ------------------------------------------------------------------
    # 检索：向量锚定入口 → 沿边扩展成子图 → 带上各节点的 facet
    # 任务域在 rollout 前拿它取记忆上下文。与 backend/kg_facade.retrieve 同源但已分叉
    # （边权多了「入口」一元、逐边概率展开取代 size 预算等，各处 docstring 就地说明）
    # ------------------------------------------------------------------

    def _relations_of(self, slug: str) -> list[dict]:
        """`_concept_relations` 的持锁封装（它内部自己 execute，故整段圈进临界区）。

        与 `_query` 同一个理由，见那边的 docstring。`tools.py` 里的两个调用点不用改：
        它们本来就在 `with self._lock` 块内 —— 那也是为什么这里**不能**复用它们的写法，
        `_lock` 不可重入。
        """
        with self._lock:
            return _concept_relations(self._conn, slug)

    def _query(self, sql: str, params: "tuple | list" = ()) -> list:
        """**持锁**跑一条只读查询并把行取完，返回 list。

        ★★ 为什么读也要持锁（2026-08-11 修，起因是 7 次 `sqlite3.InterfaceError:
        bad parameter or other API misuse`）：`_BoundTool` 的 docstring 早就写明了前提 ——
        ReAct 一步里的多个 tool_calls 会被 `asyncio.gather` 并发跑在**不同线程**
        （react.py 的 `_exec` fan-out），而整个图谱共用**一个** `sqlite3.Connection`
        （`check_same_thread=False`）。10 个 CRUD 写工具都老实持了 `_lock`，
        但**检索路径没有** —— `retrieve_subgraph` / `expand_concept` 里的裸
        `self._conn.execute(...)` 就是那 7 次崩溃的现场。

        一个连接上并发 execute 会互相踩游标状态，症状是 InterfaceError 而**不是**
        "SQLite is locked"，所以看着不像并发问题；而且它落在 `tools.py` 那个
        `except Exception: continue` 里，只丢掉半个 scope 的结果、日志照打「命中」——
        **静默降级**，Agent 拿到不完整的记忆却不知道。

        ⚠️ **只包 SQLite，不包向量检索**：`_anchor` 里的 `vec_store.search` 走 HTTP
        （embedding 服务），三个副脑共用主脑图谱这一把锁 —— 把网络往返也圈进临界区
        会把并发检索强行串成串行，白等在别人的 HTTP 上。

        ⚠️ **不可重入**：`_lock` 是 `threading.Lock`。所以已持锁的路径（`reinforce_concepts`
        等自带 `with self._lock` 的方法）**不要**再调本方法，否则自锁死。
        """
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def _fetch_concepts(self, slugs: list[str]) -> dict[str, dict]:
        if not slugs:
            return {}
        placeholders = ",".join("?" * len(slugs))
        rows = self._query(
            f"SELECT slug, name, summary, layer FROM concept WHERE slug IN ({placeholders})",
            tuple(slugs),
        )
        return {r[0]: _concept_row_to_dict(r) for r in rows}

    def search_concept_hits(self, query: str, top_k: int) -> list[tuple[str, float]]:
        """概念级向量检索：过取分片行 → 按概念融合 → 返回前 `top_k` 个 `(slug, score)`，降序。

        ★ 分块索引（2026-09-24）之后，**所有**「拿向量结果当 slug 用」的地方都必须走这里，
        不能直接读 `vec_store.search()` 的 `biz_id` —— 那个现在是**分片 key**
        （`{slug}::{kind}`），拿它去 `concept` 表查会一条都查不到，而且不报错。
        已知的两处消费者：`_anchor`（本类）与 `consolidate._find_dedup_hints`（去重预筛）。

        `score` 是**归一化**加权平均（见 `_common.fuse_part_hits`），仍在余弦量程内。
        """
        hits = self._vec_store.search(query, top_k=max(1, top_k * self._overfetch_mult))
        if not hits:
            return []
        return fuse_part_hits(
            hits, w_identity=self._retrieval.index_w_identity,
            w_facet=self._retrieval.index_w_facet,
            facet_agg=self._retrieval.index_facet_agg,
        )[:top_k]

    def _anchor(self, query: str) -> list[dict]:
        """向量锚定入口：取 top_k → 分数门（绝对 min_score / 相对 gap_ratio 取严者）。

        ★ **入口数量不再硬截断**（2026-08-08 用户定案，原 `anchor_max_entries` 已删）：
        该留几个入口是 query 与图谱的**语义关系**决定的，不该由一个跟内容无关的数字拍死。

        ⚠️ **实测入口数确实变多了**（真图谱 120 concept，`anchor_topk=8`）：
            query「天黑了要不要继续赶路」top=0.469 floor=0.399 → 过门 **7** 条（原先截到 3）
            query「怎么才能挖到铁矿石」  top=0.508 floor=0.432 → 过门 **5** 条
        原因是这个图谱的分数分布很平（8 条候选挤在 0.40~0.47 的窄带里），`gap_ratio=0.85`
        的相对门在这种分布下几乎放行所有 top_k —— 也就是说**入口数现在实质由 `anchor_topk`
        决定**。这是有意接受的口径变更（入口多 → 图扩展起点多 → 召回更宽），但要知道
        真正的旋钮已经从 max_entries 挪到了 topk：想收窄就调 `anchor.topk` 或抬 `gap_ratio`。
        ⚠️ **上面这几条实测数字是两代旧口径下的**（① 只嵌 name+summary ② 2026-09-21 起
        嵌 name+summary+全部 facet 拼接），**都已作废**。2026-09-24 起是**分块索引**：
        一个概念 1+N 条向量（identity + 每条 facet 各一条），检索时过取分片行再按概念融合成
        一个归一化分数。分布会再次整体位移，`gap_ratio` / `min_score` 需按新分布重新标定；
        标定方法见 `experiments/calibrate_retrieval.py`。
        """
        fused = self.search_concept_hits(query, self._anchor_topk)
        if not fused:
            return []
        top_score = float(fused[0][1])
        # 绝对地板默认 0（见 config yml 那段推导：余弦绝对值不能跨 embedding 模型/语料比较，
        # 可移植的判据只有相对的 gap_ratio）；想启用绝对门把 min_score 调回非 0 即可。
        score_floor = max(self._anchor_min_score, top_score * self._anchor_gap_ratio)
        entry_slugs = [s for s, sc in fused if sc >= score_floor]
        concepts = self._fetch_concepts(entry_slugs)
        return [concepts[s] for s in entry_slugs if s in concepts]

    def _expand(
        self, entries: list[dict], query: str, hops: int = 1,
        layer: str | None = None,
    ) -> list[dict]:
        """从入口出发沿边扩展：逐入口给候选边打分并按概率决定走不走。
        `边得分 = 关系语义权重 + ln(落点概念强度)` → softmax → 逐边独立按概率展开。

        ★ **关系语义权重是 `f(query, 入口概念, 关系类型)` 三元函数**
        （`relation_semantics.score_relations_from_entry`）：同一个 query 站在不同概念上会朝
        不同的关系方向走。原版口径少"入口"这一元，同一种关系在整张图上处处同分。

        每一跳，对当前 frontier（第一跳就是向量锚定的入口）：

        1. **权重** = 入口内 z-score( cos( embed(query), mean( embed(入口概念名 + 权重文本) ) ) )
           —— 入口名拼在**每条权重文本**上、按关系取均值，query 那一侧保持干净。
           ★ **权重文本 = probe 若有、否则关系名本身**（2026-08-09，随关系词表放开）：
           关系现在由 Agent 自由命名、不在 yaml 里，所以绝大多数关系走的是「关系名」这一条。
           **候选关系集合就是下面 ① 查出来的那些**（图里本跳实际存在的边），不再有静态词表；
           这也让 z-score 的参照组变成"本入口本跳的关系"，详见
           `relation_semantics.score_relations_from_entry` 的 ⚠️ 段。
           原始余弦分**必须再做 z-score**：共同的入口名前缀会把各条拼接文本压到几乎同向，
           带宽只剩 0.05、softmax 后概率差 1.1 倍（等于均匀分布）。详见那边的完整推导与实测数字。
           ⚠️ z-score 后**权重可以是负数**（它是"离本入口均值几个标准差"）。
        2. **得分** = 权重 + **ln(落点概念强度)**（不是入口的强度）—— 强度表达"这个概念被实际
           用到过几次"（只由任务域 Agent 触发，见 schema 的强度机制说明）。于是"沿这条边走"
           的倾向同时受语义相关性与历史有用性驱动。
           ★ **为什么是加 ln 而不是乘**（2026-08-08，随 z-score 一起改）：权重带符号了，
           `权重 × 强度` 会在权重为负时**反转强度的作用**（-1.6 × 3.0 = -4.8 比
           -1.6 × 1.0 = -1.6 更低 → 越常用的概念越不可能被走到）。而 softmax 里
           `exp(z + ln s) = e^z · s` —— 强度仍然是**概率的乘性因子**，语义与原先完全一致，
           只是搬到了指数上，且不受权重符号影响。实测量级也可比：z ∈ [-1.6, +1.3]，
           真实图谱 strength ∈ [1.0, 3.6] → ln ∈ [0, 1.28]，两项谁也不压死谁。
        3. **概率** = 对**所有入口的所有候选边**一起做 softmax（不是每个入口各自归一化 ——
           那样会让只有一条边的入口拿到概率 1.0，凭空压过一个有十条好边的入口）。
        4. **展开**：每条候选边**各自按自己的概率独立**决定走不走（不是抽固定条数），
           被选中的落点进入下一跳的 frontier。概率再乘**本跳入口数 k**，于是每跳新增节点的
           期望是「**每个入口** 1 个」（2026-08-10 用户定案，取代原先的「整跳合计 1 个」——
           那让入口越多、每个入口越探不动，见 `_sample_edges`）。

        **采样是真随机、不追求可复现**：同一个 query 两次检索可以带回不同的邻居，弱边也有
        机会被走到（这不影响 world seed 那套可复现性——那管的是环境/出生点，不是检索）。

        `hops` 由**任务域 Agent 自己指定**（上限 `MAX_HOPS`）—— 它才知道这次是想就近看看，
        还是要顺着依赖链往深处挖。

        已访问过的节点不再进 frontier（防环、也防重复渲染）。
        """
        if not entries or hops <= 0 or not query:
            return entries
        from openkage.embedder import get_embedder

        from .relation_semantics import score_relations_from_entry

        hops = max(1, min(int(hops), self._expand_max_hops))
        # query 只嵌入**一次**（它在整轮扩展里不变）——入口那侧的向量由 relation_semantics
        # 按 (入口名, vocab) 缓存，所以多跳/多次检索里同一个入口不会重复嵌入。
        # query 侧加指令前缀（KaLM 等非对称模型需要），与 vector_store.search 同口径。
        try:
            q_emb = get_embedder().embed_one(query, is_query=True)
        except Exception:  # pylint: disable=broad-except
            logger.warning("图扩展：query 嵌入失败，跳过扩展", exc_info=True)
            return entries
        if not q_emb:
            return entries

        visited = {e["slug"] for e in entries}
        out = list(entries)
        frontier = list(entries)

        for _hop in range(hops):
            # 只看 frontier 空不空：规模由「逐边独立概率 × 本跳入口数」决定（每个入口期望 +1 个
            # 节点），不再有全局条数刹车（原 neighbor_budget，2026-08-08 删）。
            # ⚠️ 扇出**不再自限**（乘 k 之前 Σp=1 才是自限的）—— 见 `_sample_edges` 里
            #    「没有数量封顶」那段记下的代价与新耦合。
            if not frontier:
                break
            # ① 取 frontier 的所有边（两个方向都算邻居 —— requires 正反都有信息价值）
            f_slugs = [e["slug"] for e in frontier]
            name_by_slug = {e["slug"]: (e.get("name") or e["slug"]) for e in frontier}
            ph = ",".join("?" * len(f_slugs))
            rows = self._query(
                f"SELECT from_slug, to_slug, relation FROM edge "
                f"WHERE from_slug IN ({ph}) OR to_slug IN ({ph})",
                (*f_slugs, *f_slugs),
            )
            if not rows:
                break
            # 本跳**实际存在的关系名**（排序去重）—— 这就是要打分的候选关系集合。
            # ★ 关系词表放开后**没有静态名单可用**，图里有什么边就给什么边打分。
            #   排序去重是 `_entry_relation_vecs` 的 lru_cache 要求（key 含这个元组）：
            #   同一组关系的不同顺序会各占一个缓存槽、各打一次 embedding。
            hop_relations = tuple(sorted({r[2] for r in rows if r[2]}))

            # layer 过滤：本跳候选落点必须是目标层，否则跨层落点会占采样预算又进不了 frontier。
            # 批量查候选落点 layer（layer=None 时跳过 = 不过滤，旧行为）。
            layer_ok: set[str] | None = None
            if layer is not None:
                cand_nbrs = set()
                for f, t, _rel in rows:
                    if f in name_by_slug and t not in visited:
                        cand_nbrs.add(t)
                    elif t in name_by_slug and f not in visited:
                        cand_nbrs.add(f)
                layer_ok = set()
                if cand_nbrs:
                    cph = ",".join("?" * len(cand_nbrs))
                    layer_ok = {
                        r[0] for r in self._query(
                            f"SELECT slug FROM concept WHERE slug IN ({cph}) AND layer = ?",
                            (*cand_nbrs, layer),
                        )
                    }

            # ② 逐条边算得分：权重(入口条件的关系语义) + ln(落点强度)
            rel_cache: dict[str, dict[str, float]] = {}
            cands: list[tuple[str, str, str, float]] = []  # (entry, relation, neighbor, score)
            for from_slug, to_slug, relation in rows:
                # 边的两端谁是入口、谁是落点（落点已访问过就不再扩展）
                if from_slug in name_by_slug and to_slug not in visited:
                    entry_slug, nbr_slug = from_slug, to_slug
                elif to_slug in name_by_slug and from_slug not in visited:
                    entry_slug, nbr_slug = to_slug, from_slug
                else:
                    continue
                # layer 过滤：落点不在目标层则跳过（不进打分/采样，不占预算）
                if layer_ok is not None and nbr_slug not in layer_ok:
                    continue
                if entry_slug not in rel_cache:
                    rel_cache[entry_slug] = score_relations_from_entry(
                        q_emb, name_by_slug[entry_slug], vocab=self._vocab,
                        relations=hop_relations)
                # 权重是**入口内 z-score**（带符号，"离本入口均值几个标准差"）。
                # 缺省 0.0 = "就是本入口均值"，是中性值 —— 而不是像原先绝对余弦口径下那样
                # 等价于"最差"。⚠️ 正常路径**不该**落到缺省：`hop_relations` 就是从这批 rows
                # 里取的，所以每个 relation 都被打过分。真落到缺省只有一种情形 ——
                # embedder 不可用（`_entry_relation_vecs` 返回空），那时全部为 0、
                # softmax 退回均匀分布，即"没有语义信号就别装作有"。
                weight = rel_cache[entry_slug].get(relation, 0.0)
                # ★ 这里**不再有绝对权重门**（原 `rel_weight_floor: 0.30`，2026-08-08 用户定案删）：
                #   原始余弦分挤在极窄的带里（实测 0.372~0.416），拿一个绝对值去截只有两种
                #   结果 —— 要么全放过（0.30 实测 21/21 全过，等于没有这道门），要么全挡死
                #   （同 min_score=0.60 那次事故：检索被整个关掉）。绝对余弦值还不可跨
                #   embedding 模型/语料比较。低概率边的剪枝改到 softmax **之后**用相对判据做
                #   （top_p，见 `_sample_edges`）。
                # ★ 得分 = z + ln(强度)，**不是乘法**：权重带符号后乘法会反转强度的作用
                #   （见 _expand docstring 第 2 条）。softmax 里 exp(z+ln s) = e^z · s，
                #   强度仍是概率的乘性因子。强度恒 >= STRENGTH_INIT(1.0) > 0，ln 不会炸。
                cands.append((entry_slug, relation, nbr_slug,
                              weight + math.log(max(self._strength_of(nbr_slug), _MIN_STRENGTH))))
            if not cands:
                break

            # ③ 全局 softmax → 逐边按概率独立展开
            picked = self._sample_edges(cands)
            if not picked:
                break
            new_slugs = list(dict.fromkeys(nbr for _, _, nbr, _ in picked))
            fetched = self._fetch_concepts(new_slugs)
            # 兜底：layer 过滤下，落点应已是目标层（cands 已过 layer_ok）；仍按 fetched 的 layer
            # 字段再筛一次，防万一（layer_ok 是单独查的，与 _fetch_concepts 不同源）。
            frontier = [fetched[s] for s in new_slugs if s in fetched
                        and (layer is None or fetched[s].get("layer") == layer)]
            for c in frontier:
                if c["slug"] not in visited:
                    visited.add(c["slug"])
                    out.append(c)
        return out

    def _strength_of(self, slug: str) -> float:
        row = self._query(
            "SELECT strength FROM concept WHERE slug = ?", (slug,)
        )
        return float(row[0][0]) if row and row[0][0] is not None else STRENGTH_INIT

    def _sample_edges(
        self, cands: list[tuple[str, str, str, float]]
    ) -> list[tuple[str, str, str, float]]:
        """softmax(得分/温度) → top-p 剪枝 → 重新归一化 → **× 本跳入口数 k** → 每条候选边
        各自按自己的概率独立决定走不走。（这就是完整的顺序；`× k` 是**最后**一步，吃的是
        剪枝并重归一化之后的概率。）

        ★ **2026-08-08 用户定案：逐边独立展开，不再取固定条数**（原 `hop_sample_size`
        与 `neighbor_budget` 一并删除）。softmax 出来的相对分配就是**每条边被展开的倾向**，
        各自掷一次骰子 —— 不是"按这个分布抽 k 条"。

        ★ **2026-08-10 用户定案：概率整体乘以本跳入口数 k**（`k = len({入口})`，就是本跳
        frontier 有多少个节点），于是期望变成「**每个入口** 新增 1 个节点」而不是「整跳合计 1 个」。

        病灶是 `Σp=1` 回答错了问题：它是**互斥选择题**的形式（"该走哪一条"），而"这条边值不值得
        走"本该是**逐边独立的判断题**。归一化的直接后果 ——

        - **两条都极好的边必须互相分掉概率**。实测「我的营地在哪里」19 条候选边、得分
          `-1.64 ~ +2.30`，最高分那条概率只有 **0.270**，跟第 2/3/4 名（0.08 级别）几乎没差别：
          排名信息还在，"值不值得走"的信息被归一化抹掉了。
        - **候选边越多，每条边越不可能被走**。20 条好边的枢纽与 2 条烂边的孤点，扩展预算完全相同。
        - 更反的是激励方向：`Σp=1` 是**整跳合计**，frontier 有 k 个节点时每个节点只分到 `1/k`。
          于是**能锚定出 8 个入口（= query 命中面广）恰恰导致每个入口最探不动**。

        ★ **乘 k 之后的期望，闭式**（2026-08-10 —— 别写成"期望 = k"，那是忽略截断的近似）：
        每条边是独立伯努利，成功率 `q_i = min(k·p_i, 1)`，故

            E[本跳新增**边**数] = Σ_i q_i = Σ_i min(k·p_i, 1) = k − Σ_i (k·p_i − 1)⁺
            Var[边数]          = Σ_i q_i(1 − q_i)          （独立，故方差可加）

        `Σp=1` 只保证 `Σ k·p_i = k`；`min(·,1)` 是**非线性**的，凡有 `p_i > 1/k` 的边就会
        亏掉 `(k·p_i − 1)` 那部分质量。所以 **E ≤ k，等号仅当所有 p_i ≤ 1/k**（即概率完全均匀
        时才取到）。而语义打分的目的正是让分布不均匀 —— 所以实践中恒有 E < k。
        真实图谱实测（bob，561 跳，把每跳的 p 向量代入上式**精确计算**、非蒙特卡洛）：
        平均 k=3.451，`E[边数]=3.197`，截断亏损 0.254，**E/k = 0.9497**；46.7% 的跳发生了截断。

        ⚠️ 还要再降一次：上式算的是**边**数，而"新增节点"要去重（见下）。
        `E[新增节点数] = Σ_t (1 − Π_{i→t}(1 − q_i))`，t 遍历不同落点。同批实测 **2.957**。
        所以从 k 到真实节点数有两道折损：截断（k→3.197）与去重（3.197→2.957），
        合计 `E[节点]/k ≈ 0.857`。

        ★ **多条边指向同一落点 → 不会产生重复节点**（Q2）：`_expand` 里
        `dict.fromkeys(...)` 先按 slug 去重、随后 `visited` 又拦一道，所以 `frontier` 与 `out`
        都不含重复。但**概率上并不等价于一条边**：schema 是 `UNIQUE(from_slug, to_slug, relation)`
        —— 同一对概念间允许多条**不同关系**的边，两个入口也可能都指向同一个邻居。
        于是同一落点被 m 条边各自独立命中，实际到达概率是 `1 − Π(1 − q_i)`（比单条高、但低于
        求和），这正是上面去重那一项的来源。实测 42.6% 的跳存在重复落点、平均 0.845 条重复边。
        ⚠️ 这意味着**度数高且被多种关系指向的枢纽概念，天然更容易被走到** —— 这个偏置是
        机制的自然结果（多条独立证据都指向它），不是缺陷，但解释召回结果时要知道它在。
        乘 k 之后 `Σ(k·p)=k`：**跨入口的相对分配仍由全局 softmax 决定**（好入口多拿、弱入口
        少拿，这是 2026-08-08 定的口径，见 `_expand` docstring 第 3 条，不变），只有总量被放大到
        与入口数同阶。端到端实测（真实副脑图谱 bob，76 概念/104 边，改动前 → 改动后）——

            query                入口   hops=1 节点        hops=3 节点
            夜晚遇到怪物怎么保命    1    1.99 → 2.04       3.02 → 3.03     ← 单入口时恒等
            如何采集铁矿并冶炼      3    3.79 → 5.64       5.08 → 8.47
            我的营地在哪里         5    5.98 → 9.78       7.40 → 16.33
            饥饿度低了去哪找食物    8    8.93 → 14.40      9.73 → 21.56

        ★ **k 取「本跳 frontier 大小」而不是「锚定入口数」**：第一跳两者定义上相同，分歧在多跳 ——
        frontier 会随扩展枯竭而变小，k 自动跟着缩（实测 hops=3 收敛在 21.56）；而整趟固定
        k=8 到第 3 跳还在按 8 倍放大，容易发散（同条件 24.64 且尾巴更长）。"本跳入口数"也正是
        这个乘数在语义上该有的含义。

        ★ **k 不是配置项**：它从 `cands` 里数出来（`{c[0] for c in cands}`）。候选边自带入口，
        再加一个 yml 旋钮去声明"这次有几个入口"就是给同一个事实存第二份。

        ★ **没有数量封顶**（2026-08-10 用户定案，即不恢复 `neighbor_budget`）：该带回多少是
        query 与图谱的语义关系决定的，不该由一个跟内容无关的数字拍死；`hops` 又由任务域 Agent
        自己指定 —— 它要深挖就给它。已知代价，如实记在这里：宽 query 在 `hops=3` 下会带回
        **全图的 28%**（上表 21.56/76，p95 达 34%），检索的选择性随之下降；成本侧尚有余量
        （该图谱全部 facet 共 20009 字，实测记忆块只占 ReAct 上下文的 6.24%、最大 27.9%）。
        ⚠️ 扇出**不再自限**，所以这个规模现在实质挂在 `anchor.topk` 上（见 `_anchor` 的 ⚠️ 段：
        gap_ratio 在平分布图上几乎不挡人，入口数实质由 topk 决定）—— 调 topk 会连带把扩展预算
        按同倍数放大，这是本改动引入的新耦合。

        `Σp<1` 那一侧的性质仍然要紧：top_p 剪枝后**必须重新归一化**，否则每跳期望会跌破 k、
        多跳迅速枯竭 —— 那是把"概率给谁"的旋钮偷偷变成了"给多少"。

        温度在这里仍然有意义（它决定概率怎么分配）：温度小 → 概率集中在最优边，几乎总是
        沿最相关那条走；温度大 → 摊平，弱边也有机会。删掉采样条数不影响它。

        ★ **`expand.top_p`：概率剪枝放在 softmax 之后**（2026-08-08 用户定案，取代原先在
        softmax **之前**用绝对余弦值截断的 `rel_weight_floor`）。判据与 LLM 采样的 top-p 同构：
        按概率降序累加，只保留累积到 `top_p` 之内的边，尾部低概率边整个丢掉。

        为什么必须放在 softmax 之后：关系权重是余弦量级的数，**没有可解释的绝对刻度** ——
        实测同一入口内 12 种关系全挤在 0.372~0.416 这样的窄带里，绝对门 0.30 实测 21/21
        全过（等于不存在），稍微调高就会全挡死（同 min_score=0.60 那次把检索整个关掉的事故）。
        而 softmax 之后是**归一化的概率**：`p=0.001` 就是"这条边只有千分之一该走"，
        这个判断跟 embedding 模型、语料、语言都无关，是可移植的。

        `top_p=1.0` = 不剪枝（保留全部）。剪枝后**重新归一化**，于是 Σp 仍为 1（再乘 k），
        「每个入口期望新增 1 个节点」这个性质不变 —— top_p 只改变概率**分配给谁**，
        不改变总量。

        用全局 `random` 模块、**不接受种子**：同一个 query 两次检索可以带回不同的邻居、
        弱边也有机会被走到。不要为了"可复现"再给它派生固定种子 —— 那会把探索变成确定性查表；
        world seed 那套可复现性管的是环境/出生点，与检索无关。

        减最大值再 exp 是标准的数值稳定写法（不减的话大得分会 overflow）。
        """
        scores = [c[3] for c in cands]
        mx = max(scores)
        temp = self._expand_temperature or 1.0
        weights = [math.exp((s - mx) / temp) for s in scores]
        total = sum(weights)
        if total <= 0:
            return []
        probs = [(c, w / total) for c, w in zip(cands, weights)]
        # ── top-p 剪枝（核采样）：按概率降序累加，保留累积 ≤ top_p 的那批 ──
        # 用 `>=` 判断**在加完当前项之后**才停，所以恰好跨过阈值的那条**被保留** ——
        # 与 LLM 的 nucleus sampling 一致（否则 top_p 略小于最高概率时会剪成空集：
        # 单条边概率为 1.0 而 top_p=0.9 的情形，一条不留就等于图扩展彻底停摆）。
        top_p = self._expand_top_p
        if top_p < 1.0:
            probs.sort(key=lambda cp: -cp[1])
            kept, acc = [], 0.0
            for cand, p in probs:
                kept.append((cand, p))
                acc += p
                if acc >= top_p:
                    break
            # 重新归一化：剪掉尾部后 Σp<1，不归一化会让每跳期望新增数被一并压低
            # （那是 top_p 的副作用，不是它该管的事 —— 它只该决定"概率给谁"）。
            kept_total = sum(p for _, p in kept)
            probs = [(cand, p / kept_total) for cand, p in kept] if kept_total > 0 else []
        # 逐边独立伯努利试验：p_i = 剪枝归一化后的概率 **× 本跳入口数 k**，截到 1.0。
        # k 从候选边自带的入口数出来（不是配置项，见 docstring）。截断只在 p_i·k > 1 时生效，
        # 那本就是"这条边必走"，不是隐式的条数刹车。
        # ⚠️ `min(·, 1.0)` 是**表意**而非行为：`random() < 3.99` 与 `random() < 1.0` 同样恒真，
        #    删掉它任何测试都抓不到（2026-08-10 实测确认，别指望夹具守这一行）。留着是因为
        #    上面闭式 `Σ min(k·p_i, 1)` 要读得出来 —— 概率超过 1 这件事得在代码里显形。
        # ★ **k 数的是 top-p 剪枝*前*的入口数（`cands`），不是剪枝后还有边在场的入口数**
        #   —— 2026-08-10 用户定案，这是有意的，别"顺手修正"成 `probs` 那一侧：
        #   预算是「这一跳有几个入口」赋予的，top_p 只管**钱给谁**、不管**给多少**
        #   （与上面重新归一化同一套逻辑：剪枝不该偷偷改变总量）。于是某个入口的边被整个剪光时，
        #   它那份预算**转移**给边更好的入口，而不是凭空消失。
        #   ⚠️ 代价是「每个入口期望 1 个」在这些跳上只在**整跳合计**的意义上成立：实测 833 跳里
        #   有 200 跳（24.0%，top_p=0.95 平均剪掉 19.2% 的边）出现 k_cands > k_kept，
        #   典型如 5 个入口进来、剪完只剩 3 个入口在场，仍按 ×5 放大 → 那 3 个存活入口
        #   实际各领到 1.67 个。要让字面口径成立就得把 k 改成剪枝后的数，但那等于让 top_p
        #   兼职决定总量 —— 已明确否掉。
        # 全 0 命中是允许的结果（这一跳就停在这儿），由调用方的 `if not picked: break` 处理
        # —— 不做"至少保一条"的兜底：那等于偷偷把概率改成"最优边恒定走"，探索的随机性就被抹掉了。
        k = len({c[0] for c in cands})
        return [cand for cand, p in probs if random.random() < min(p * k, 1.0)]

    def retrieve(self, query: str, hops: int = 1) -> list[dict]:
        """`retrieve_subgraph` 的扁平 facet 视图（**真正的入口是那个**，见它的 docstring）。
        hops = 走几跳（上限 expand.max_hops），由调用方/Agent 决定。

        ⚠️ 返回**扁平 facet 列表**，供既有调用点消费（rollout.retrieve_memory_block /
        build_context_messages 都按列表消费，且 `summarize_mem` 数的是 len()）——
        代价是把子图结构丢掉了。要图结构（节点+边）用 `retrieve_subgraph`。
        """
        return self.retrieve_subgraph(query, hops=hops).facets

    def retrieve_subgraph(self, query: str, hops: int = 1,
                          layer: str | None = None) -> MemorySubgraph:
        """**召回一片子图**（节点 + 内部边 + 可展开的外部边 + 入口标记 + 各节点的 facet）。

        向量锚定出入口概念 → 从入口沿边扩展（`_expand`，逐边按概率、走 hops 跳）→
        把这片节点的边和 facet 一起带上。**召回单位是子图、不是一条条 facet**：
        节点之间怎么连、谁是枢纽、还能往哪走，正是图记忆相对于平铺文档检索的价值所在，
        而扁平列表会把这些全丢掉（`retrieve` 就是那个有损视图）。
        入口靠 query 的向量相关性选，不是分桶平铺。

        ★ **layer 过滤**（2026-09-05 恢复，曾 2026-08-08 撤掉）：私有图谱分 memory/experience
        两层，检索默认只查经验层（memory 层有 day_start 滑动窗口全量常驻注入，不必再检索）。
        ``layer="experience"``（默认，由工具层传入）只召回经验层节点；``layer="memory"`` 只召回
        记忆层；``layer=None`` 不过滤（旧行为，兼容）。公有图谱只有 experience 层，过滤对它无害。

        四处过滤点（入口/扩展落点/外部边/expand 邻居）都在本方法及 `_expand`/`_subgraph_edges`
        内闭合：入口锚定后按 layer 过滤、扩展落点 SQL JOIN concept.layer 过滤、外部边按 layer
        过滤子图外端——漏一处就会跨层爬回去、或给 Agent 列出展开不了的 expand 方向白花一步。
        """
        entries = self._anchor(query)
        if layer is not None:
            entries = [e for e in entries if e.get("layer") == layer]
        # ★ 消融 flat_retrieval（2026-09-18）：锚定即止——不走 _expand 边扩展、不给
        #   inner/outer edges（子图结构整段拿掉）。返回的 MemorySubgraph 无边，
        #   render_subgraph 退化成平列表（入口标记也无意义，entry_slugs 仍填锚点——
        #   渲染层对无边子图自然按平列表出，无需特判）。知识写入路径完全不变（写图谱/
        #   聚合/共享照常），只有读取降级为 RAG 式 top-k——与 RAG baseline 的差异收敛为
        #   「检索算法」单一变量。reinforce 照给（强度是图谱内部状态，读取降级不改记账）。
        if self._ablation == "flat_retrieval":
            if not entries:
                return MemorySubgraph()
            self.reinforce_concepts([e["slug"] for e in entries],
                                    self._strength_gain_retrieve)
            facets = self._collect_facets(entries)
            return MemorySubgraph(
                concepts={c["slug"]: c for c in entries},
                facets=facets,
                inner_edges=[],
                outer_edges=[],
                entry_slugs=[e["slug"] for e in entries],
            )
        nodes = self._expand(entries, query, hops=hops, layer=layer)
        if not nodes:
            return MemorySubgraph()
        # 命中即算「被用到」——但只给 `strength.gain_retrieve`（yml，默认 0.1）：检索无阈值、
        # 无关的概念也会进来，信号精度远低于 Agent 主动 expand。
        self.reinforce_concepts([c["slug"] for c in nodes], self._strength_gain_retrieve)
        facets = self._collect_facets(nodes)
        inner, outer = self._subgraph_edges([c["slug"] for c in nodes], layer=layer)
        return MemorySubgraph(
            concepts={c["slug"]: c for c in nodes},
            facets=facets,
            inner_edges=inner,
            outer_edges=outer,
            entry_slugs=[e["slug"] for e in entries],
        )

    def _subgraph_edges(
        self, slugs: list[str], layer: str | None = None,
    ) -> tuple[list[tuple[str, str, str]], list[tuple[str, str, str]]]:
        """取与 `slugs` 相关的边，拆成「两端都在子图内」与「一端在子图外」两组。

        内部边 = 这片子图的结构（谁依赖谁、谁是枢纽）；
        外部边 = 还能往哪走（Agent 用 expand 的候选方向）。
        两者对 Agent 的用途不同，故分开返回、分开渲染。

        ``layer`` 非 None 时（retrieve_subgraph 传）：外部边的子图外端按 layer 过滤——
        跨层的外部端点会列出一个 Agent 沿同 layer 展开不了的方向、白花一步，故剔除。
        内部边两端都在子图内（已按 layer 过滤），无需再过。
        """
        if not slugs:
            return [], []
        inside = set(slugs)
        ph = ",".join("?" * len(slugs))
        rows = self._query(
            f"SELECT from_slug, relation, to_slug FROM edge "
            f"WHERE from_slug IN ({ph}) OR to_slug IN ({ph})",
            (*slugs, *slugs),
        )
        # layer 过滤：外部边的子图外端必须是目标层（否则给 Agent 一个展开不了的跨层方向）。
        outer_slugs = set()
        if layer is not None:
            for f, _rel, t in rows:
                if f not in inside:
                    outer_slugs.add(f)
                if t not in inside:
                    outer_slugs.add(t)
            outer_layer_ok = set()
            if outer_slugs:
                oph = ",".join("?" * len(outer_slugs))
                outer_layer_ok = {
                    r[0] for r in self._query(
                        f"SELECT slug FROM concept WHERE slug IN ({oph}) AND layer = ?",
                        (*outer_slugs, layer),
                    )
                }
        inner, outer = [], []
        for f, rel, t in rows:
            if f in inside and t in inside:
                inner.append((f, rel, t))
            else:
                # 外部边：layer 过滤时，子图外端必须在目标层
                if layer is not None:
                    outer_end = t if t not in inside else f
                    if outer_end not in outer_layer_ok:
                        continue
                outer.append((f, rel, t))
        return inner, outer

    def _collect_facets(self, concepts: list[dict]) -> list[dict]:
        """把一批 concept 上的 facet 召回成扁平列表 —— **一条不截，全带上**。

        抽成公共方法：`retrieve`（向量锚定+图扩展的子图）与 `expand_concept`（Agent 指定的
        邻居）都要做**同一件事**，写两份必然漂移。

        ★ **2026-08-08 用户定案：不再限条数**（原 `facets_per_concept` / `top_k` 已删）。
        理由：`(concept_slug, kind)` 是联合主键，所以一个 concept 的 facet 条数 = 它用到的
        kind 数 ≤ 词表大小（反思侧 8 / 聚合侧 4）—— **本来就有天然上限**，再加一道数字截断
        是在为一个不存在的爆炸做防御。而它错的时候是静默的：一个概念真挂满 8 种 kind 时，
        `facets_per_concept=4` 会默默吃掉一半内容，且吃掉哪一半取决于 `created_at, kind`
        这个跟重要性无关的排序。实测真实图谱（120 concept）最多的一个概念只有 3 条 facet。

        「哪个概念值得进上下文」这件事已经在上一层（`_expand` 的逐边概率展开）决定完了；
        既然一个概念被选中了，就把它记的东西完整给出去 —— 半截的知识比不给更容易误导。

        排序按 `created_at, kind` 保留：**确定性**即可（同一次检索两次渲染顺序一致）。
        """
        if not concepts:
            return []
        slugs = [c["slug"] for c in concepts]
        placeholders = ",".join("?" * len(slugs))
        facet_rows = self._query(
            f"SELECT concept_slug, kind, content FROM facet "
            f"WHERE concept_slug IN ({placeholders}) "
            f"ORDER BY created_at, kind",
            tuple(slugs),
        )

        concept_by_slug = {c["slug"]: c for c in concepts}
        return [
            {"concept_slug": concept_slug, "concept_name": concept_by_slug[concept_slug]["name"],
             "kind": kind, "content": content}
            for concept_slug, kind, content in facet_rows
        ]

    def expand_concept(self, slug: str, relation: str | None = None) -> MemorySubgraph:
        """沿边走一跳：返回 `slug` 这个节点的邻居 concept 及其 facet。

        把图**显式交给 Agent**：它自己决定从哪个节点、沿哪种关系再走一跳，而不是只能通过
        换 query 被动等待 `_expand` 内部自动选边。

        relation=None 表示不限关系（拿全部邻居）；给了就只走那一种。
        方向不限（in/out 都算邻居）：`requires` 这类边正反两个方向都有信息价值
        （A requires B：从 A 问"我需要什么前提"、从 B 问"什么依赖我"）。

        返回 `MemorySubgraph`（与 `retrieve_subgraph` **同一形状**）——
        中心节点也放进 concepts、并标为 entry（它是这次展开的出发点），
        中心自己的 facet 不重复给（Agent 刚才就是从它那里拿到 slug 的），只给邻居的。
        节点不存在 / 该方向没有邻居时返回空子图（`.empty` 为 True）。

        ★ **不按层过滤邻居**（与 `retrieve_subgraph` 同一决定，理由见那边）：
        跨层边（`夜间移动风险 located_at 第2天`）是有意义的展开方向 —— 从一条通用教训走到
        "我哪次踩到它"，那正是图记忆该有的样子。
        """
        center = self._fetch_concepts([slug]).get(slug)
        if center is None:
            return MemorySubgraph()
        rels = self._relations_of(slug)
        if relation:
            rels = [r for r in rels if r["relation"] == relation]
        peer_slugs = list(dict.fromkeys(r["peer_slug"] for r in rels))  # 去重保序
        peers = self._fetch_concepts(peer_slugs)
        facets = self._collect_facets([peers[s] for s in peer_slugs if s in peers])
        nodes = {slug: center, **{s: peers[s] for s in peer_slugs if s in peers}}
        # ★ **强度的主要来源**：Agent 花一步刻意从 slug 沿边深挖，这是
        # 「这个概念有用」最高精度的信号 —— 中心和被带出来的邻居都加 `strength.gain_expand`。
        # 它与 gain_retrieve 的**比例**（yml 默认 1.0 : 0.1）才是设计立场：让"主动选择"
        # 压过"语义碰巧相近"。
        self.reinforce_concepts(list(nodes), self._strength_gain_expand)
        inner, outer = self._subgraph_edges(list(nodes))
        return MemorySubgraph(
            concepts=nodes,
            facets=facets,
            inner_edges=inner,
            outer_edges=outer,
            entry_slugs=[slug],
        )

    def render_context(self, facets: list[dict]) -> str | None:
        """把检索到的 facet 渲染成喂给模型的记忆块文本。

        ⚠️ **措辞必须域中性**：这里是**框架层**，minecraft / math 都走它，不能预设任务形态
        （比如写"过去做同类题的经验"）。各域在 retrieve_memory_block 时已经给了自己的前缀
        （minecraft 传的是「过往探索经验（自己）：」），框架层只说"过去积累的经验"，
        kind 标签本身已说明每条是什么类型。

        实际渲染委托给 `MemorySubgraph.render`，只有一处实现。本方法保留是因为既有调用点
        （rollout 的两个函数、各域的记忆块）都按「retrieve 拿列表 → render_context 拿文本」
        这个形状在用；传进来的扁平列表缺少边信息，这里按 facet 里的 concept_slug 反查补齐，
        输出与 `render_subgraph` 一致。新代码建议直接走 `retrieve_subgraph` + `render_subgraph`
        （少一次反查）。
        """
        if not facets:
            return None
        slugs = list(dict.fromkeys(f["concept_slug"] for f in facets))
        inner, outer = self._subgraph_edges(slugs)
        sg = MemorySubgraph(
            concepts=self._fetch_concepts(slugs),
            facets=facets,
            inner_edges=inner,
            outer_edges=outer,
            # 扁平列表分不出哪些是向量入口（信息在 retrieve 里就丢了）→ 不标 ★，
            # 而不是瞎标一个：标错比不标更误导（Agent 会据此判断相关度）。
            entry_slugs=[],
        )
        return self.render_subgraph(sg)

    def render_subgraph(self, sg: "MemorySubgraph") -> str | None:
        """把子图渲染成记忆块文本。**所有喂给模型的记忆文本都从这里出**。

        ⚠️ **措辞必须域中性**：本层是框架层，minecraft / math 都走它，不能预设任务形态。

        本方法只输出「这次查到了什么」。**「怎么读这段结果」由各域自己的检索工具描述承载**：
        那些说明每次检索一字不变，放进工具描述模型只读一次，比每次检索结果里都重复一遍省 token。
        框架层不放一份公共常量的理由：读图说明里必然要写「想深挖就把 slug 传给 **expand**」——
        而 `expand` 是**某个域的工具名**（如 minecraft），框架层不认识它，math 域也没有这个
        工具。放这里就等于让框架层的文案依赖某个域的工具集，正是本方法 docstring 第一句在
        禁止的事。
        """
        return sg.render()

    def dump_memory_layer(self, *, header: str = "") -> str | None:
        """把**记忆层**（`layer='memory'`）渲染成一块文本（不检索、不打分、不截 facet）。

        ★ **记忆层 = 跨天的上下文压缩机制**，这是本方法存在的全部理由。私有图谱分两层
        （见 schema.LAYER_*）：`experience` 是归纳出的、可共享的、**要被聚合进主脑**的结论，
        走正常检索；`memory` 是「我在哪、我做过什么、我还差什么」，只给明天的自己。
        一天结束时上下文整个清空，这份记忆是唯一的跨天通道 —— 一整天几万 token 的轨迹被
        反思压成几个专名概念（`day_N`、营地、矿洞），第二天原样注入回去。

        为什么它必须常驻、不能全走 retrieve：「我的营地在哪」「昨天有什么没做完」
        **没有人会想到去查它**。向量检索天生答不了这类问题 —— query 是"我现在想干什么"，
        而记忆是"我在哪、我做过什么"，两者语义不匹配（这也是检索恒 0 命中的成因之一）。

        ⚠️ **只出记忆层**：经验层不在这里出。它是通名的、要走聚合进主脑再由 retrieve 按
        当下意图取回；跟着常驻注入会重复占上下文，而且把"该聚合的"和"永不聚合的"混成一块，
        读的人分不清哪些明天还算数。这与聚合侧只读经验层（`consolidate._dump_arm_graph`
        传 `LAYER_EXPERIENCE`）正好互补：两层各有各的出口，谁也不串到对方的通道里。

        ★ **窗口按类别分桶**（2026-09-12，取代单值滑动窗口）：预算在 yml 的
        `memory_graph.memory_dump_budgets`（episodic/spatial/lesson/status 各一个），
        各桶独立按 `updated_at` 倒序取前 N。**桶上限是绝对的、不跨桶回填** —— spatial 不足
        预算时空位就空着，不挪给 episodic（让一类记忆超额挤占另一类的失效曲线，正是分桶要
        避免的；窗口是上限不是目标）。
        为什么必须分桶 —— 旧口径（单值 updated_at 倒序）下反思每天固定写 4~5 个
        `day_N` 系新概念（updated_at=now）**天然霸榜**：0912 批 30 夜末态记忆层 81~150 个，
        注入的 20 个几乎全是最近 4~5 天流水账，营地/箱子这类「没人会想到去 retrieve、
        必须常驻」的 spatial 概念被系统性挤出窗外（实测 carol 三只箱子含坐标全在
        窗口外，day_17 箱子读 air → day_18 被迫重建）。分桶后每类的失效曲线不同
        （episodic 几天翻篇 / spatial 挖空才失效 / lesson 不随时间衰减 / status 每天覆盖），
        各自的预算就是各自的窗口，互不挤占。

        ★ **排序仍按 `updated_at` 倒序**（桶内与回填全局都是）：撞顶时丢的是**最近未被
        活动过的**记忆（最可能已经失效的），符合"越近越有用"。
        ⚠️ `updated_at` 能当判据，前提是**写图谱的工具在碰概念内容时回写了它**
        （`add_facet` / `update_facet` / `del_facet` / `manage_edge` / `merge` 都推进父
        concept.updated_at 到当前 round，见 tools.py；retrieve/expand 命中经
        `reinforce_concepts` 一并推进，见那边 2026-09-12 的改动）。否则它建后不动、
        退化成 created_at。`created_at` 作同 `updated_at` 下的稳定兜底（与
        `search_concepts` 一致）。

        ⚠️ **category 为 NULL 的概念跳过本窗口**（老库/旧 run 的行没有类别）：没有类别
        就没有预算桶，进窗口等于绕过分桶回到旧口径的霸榜问题。retrieve/expand 照常能
        取到它们 —— 那条路不看 category，记忆没有丢，只是不常驻注入。
        """
        # ★ 消融 no_memory_layer（2026-09-18）：记忆层常驻注入消失（create_concept 已在
        #   工具层拒写 memory，此处防御性返 None——续跑旧库/手工写库也不会把记忆层
        #   注入回来）。晨间 day_start 的 own_memory_block 为空 → 模板【记忆】块只剩
        #   公共经验数量行。经验层/聚合/技能不受影响。
        if self._ablation == "no_memory_layer":
            return None
        rows = self._conn.execute(
            "SELECT slug, name, summary, layer, category FROM concept "
            "WHERE layer = ? AND category IS NOT NULL "
            "ORDER BY updated_at DESC, created_at DESC, slug",
            (LAYER_MEMORY,),
        ).fetchall()
        if not rows:
            return None
        n_with_cat = len(rows)
        # ── 分桶选人：每桶独立 updated_at 倒序取预算，**桶上限是绝对的**（不回填）──
        budgets = self._memory_dump_budgets
        picked: list[str] = []
        for cat, budget in budgets.items():
            bucket = [r[0] for r in rows if r[4] == cat]
            picked.extend(bucket[:budget])
        # ★ **不做跨桶回填**（2026-09-12）：回填会让"已撞上限的桶"继续从别的桶捡人，使桶上限
        #   形同虚设（40 个全 episodic 时 episodic=18 的上限被回填顶回 34）。桶上限是该类记忆
        #   的注入预算本身 —— spatial 只有 7 个而预算 10，那 3 个空位就空着，不挪给 episodic
        #   （episodic 有自己的 18；让一类记忆超额挤占另一类的失效曲线，正是分桶要避免的）。
        #   生产 5 角色×30 夜：episodic 30~112、spatial 7~60、lesson 0~18、status 1~2，
        #   各桶大多撞顶，空位本就少；窗口是上限不是目标，某桶不足时窗口略小是可接受的。
        # 渲染顺序 = 全局 updated_at 倒序（rows 本身已按它排好，直接过滤保序）：
        # 桶只是选人逻辑，不是展示分组 —— 最新概念仍统一在最前，与旧口径的呈现习惯一致。
        picked_set = set(picked)
        rows = [r for r in rows if r[0] in picked_set]
        if len(rows) < n_with_cat:
            # ★ **info 而非 warning**（与 `_dump_layer` 同一条理由）：窗口收窄是分桶预算
            #   **正常工作**，不是异常。⚠️ 这里**不发**给 Agent 的告知行
            #   （`_TRUNCATED_PANORAMA_NOTE`）：本方法的消费者是白天的 ReAct，它不建概念 ——
            #   那行是给**反思**防重建用的（见 `_dump_layer`）。
            logger.info(
                "dump_memory_layer: 记忆层(有类别)共 %d 个概念，按桶预算注入 %d 个"
                "（%s）—— 各桶独立 updated_at 倒序，桶上限绝对、不跨桶回填",
                n_with_cat, len(rows),
                ", ".join(f"{c}={n}" for c, n in budgets.items()),
            )
        slugs = [r[0] for r in rows]
        rows_by_slug = {r[0]: r for r in rows}
        placeholders = ",".join("?" * len(slugs))
        raw_facets = self._conn.execute(
            f"SELECT f.concept_slug, c.name, f.kind, f.content "
            f"FROM facet f JOIN concept c ON c.slug = f.concept_slug "
            f"WHERE f.concept_slug IN ({placeholders}) "
            f"ORDER BY f.kind",
            tuple(slugs),
        ).fetchall()
        # ⚠️ **按 `slugs` 的顺序重排**：`MemorySubgraph.render` 是按 facet 的遍历顺序分组
        # 输出的，所以"概念以什么顺序出现在 prompt 里"由这个列表的顺序决定，而不是上面
        # SQL 里的 `ORDER BY f.kind`（那只管同一概念内 facet 的排列，不管概念间顺序）——
        # 概念间的顺序要按 `slugs`（已经是 updated_at 倒序）来定。
        by_slug: dict[str, list[dict]] = {}
        for cs, cname, kind, content in raw_facets:
            by_slug.setdefault(cs, []).append(
                {"concept_slug": cs, "concept_name": cname, "kind": kind, "content": content})
        facets = [f for s in slugs if s in rows_by_slug for f in by_slug.get(s, [])]
        if not facets:
            return None
        # `slugs` 只有记忆层，故 inner_edges（两端都在 slugs 内）天然只有层内边 ——
        # 指向经验层的边落进 outer 被丢掉，经验层专名不会从边清单泄进来。
        inner, _outer = self._subgraph_edges(slugs)
        sg = MemorySubgraph(
            concepts=self._fetch_concepts(slugs),
            facets=facets,
            inner_edges=inner,
            # 不出"可展开的外部边"：记忆层是**常驻注入**、不是一次检索结果，没有"再往外走一步"
            # 这个动作可接（Agent 手里的 expand 是给检索用的）。而这里的外部边指向经验层，
            # 摆出来只会诱导 Agent 去 expand 一个它本该用 retrieve 按意图取的东西。
            outer_edges=[],
            entry_slugs=[],
        )
        return sg.render(header=header) if header else sg.render()

    def dump_layer_index(self, layer: str | None = None) -> str:
        """把某一层（**不传 = 全部层**）的概念渲染成**索引形态**：`[层] slug 「名」summary`
        + 已有 facet kind 列表 + 关系，**不含 facet 正文**。空图返回「（无）」。

        ⚠️ **只给能 `get_concept` 补上正文的调用方**。判据不是"省 token"，而是
        **正文可不可达**：拿不到正文的调用方必须走 `dump_layer_full`（见那边）。
        - 聚合读**主脑**、反思读**自己那份图谱** → 工具就绑在同一个连接上，可以 drill → 用本方法。
        - 聚合读**副脑** → `get_concept` 恒返回"不存在" → **必须** `dump_layer_full`。

        ★ **与 `dump_memory_layer` 是两种形态、服务两种需求**：那个出正文，因为 ReAct 要的是
        `where` 里的坐标数字本身、而它手里**没有** `get_concept`；本方法出索引，因为反思要的是
        "这个概念我记过了吗、slug 叫什么"，而它改 facet 前**必须**先 `get_concept` 读原文
        （`update_facet` 整条覆盖）—— 给它正文反而会诱导它跳过那一步。

        消费者（层口径写在调用处，本方法不预设）：
        - 反思：`_reflect_exploration` **不传 layer**（两层全景）。记忆层也要给 ——
          "已经记过的地方若有变化就补充进去"这条要求此前只能靠 `search_concepts`，而它
          不带 query 时靠 `updated_at` 排序，而在 2026-08-08 修好副脑刻度之前那个值在一条
          轨迹内是常量（见 `_dump_layer`）—— 全景把"我记过什么"这件事变成确定的，
          不依赖排序好不好。
          两层比单层只多约 35 token（实测第 3 天的图谱：922 vs 391 字符）。
        - 聚合：**已不再前置主脑全景**（2026-08-08 删，见 `build_aggregate_input`）。

        `layer` 一并作为 `peer_layer` 传下去（**只渲染层内边**）：不然过滤了概念行、
        关系列表却仍会把另一层的 slug 带出来 —— 见 `_concept_relations` 的 docstring。
        不传 layer 时不过滤，跨层边正常渲染（那是有信息量的：一条经验挂到了哪一天）。

        护栏见 `_dump_layer` —— 与 `dump_memory_layer` 共用同一个值，但**只作用于记忆层**
        （经验层不设上限，2026-08-15 解耦）。所以两层全景撞顶时被省略的只会是最老的记忆概念，
        经验层那半始终是完整的 —— 告知行必须把这点说清（见 `_TRUNCATED_PANORAMA_NOTE`）。
        """
        return self._dump_layer(layer, with_content=False)

    def dump_layer_full(self, layer: str | None = None) -> str:
        """同 `dump_layer_index`，但**每条 facet 出全文**（`[kind] 正文`）。空图返回「（无）」。

        ★ **唯一消费者是聚合读副脑图谱**，而这不是偏好、是硬约束：聚合 Agent 的 10 个工具
        全绑在**主脑**连接上（`consolidate` 里的 `_build_tools(self._conn, …)`），副脑是
        独立的 SQLite 文件、只被 `build_aggregate_input` 读来拼文本、**从不交给 Agent**。
        所以副脑的 slug 对它 `get_concept` 恒返回「不存在」——
        索引形态下它**没有任何办法**知道那条 facet 写了什么。

        2026-08-08 之前这里就是索引形态，后果是聚合的四项职责全部落空且**完全静默**：
        「取表述最清楚、参数最全的那个版本」没有版本可比、「把各自的措辞与原文一起综合」
        没有措辞、「判断是不是适用条件不同」没有条件可读、「语气比证据更绝对就调回去」
        看不到语气。它只能照着 name+summary 编一条正文写进主脑，无报错、Δfacet 照涨。

        代价是上下文变大（一个 batch N 个 arm × 十几个概念 × 几条 facet），这是**知情的**
        取舍：聚合本就是逐条比对正文的活，那是它的主要工作而不是例外。若哪天撞上下文上限，
        该动的是 arm 数 / 每 arm 概念数，而不是把正文换回索引。

        ★ **这条路上没有截断**（2026-08-15 护栏按层解耦）：`_dump_layer` 的护栏只管记忆层，
        而唯一调用方传的是 `LAYER_EXPERIENCE` —— 结构上进不了截断分支。经验层靠"每窗口聚合后
        `clear_layer`"收敛（实测峰值 30 个概念），真涨到几百说明它没被清空，那是 bug，
        该显出来而不是被静默砍掉再当成完整的聚合输入。理由详见 `_dump_layer`。
        """
        return self._dump_layer(layer, with_content=True)

    def _dump_layer(self, layer: str | None, *, with_content: bool) -> str:
        """`dump_layer_index` / `dump_layer_full` 的共同实现（差别只有 `with_content`）。

        护栏是 `self._memory_dump_max_concepts`（yml 的 `memory_graph.memory_dump_max_concepts`），
        与 `dump_memory_layer` **同一个值** —— 三个 dump 是同一件事的三种形态，阈值没理由各设一个。

        ★ **护栏只管记忆层，经验层不设任何上限**（2026-08-15 用户定案）。此前是两层混在一起
        排完再截前 N，于是那个名字在两层全景这条路上是假的：它实际是「两层之和」的上限，
        **经验概念在跟记忆概念抢同一个额度**。两件事被这个耦合混成了一件，而它们性质相反：
          · 记忆层撞顶是**设计内**行为 —— 每天至少一个 `day_N` 加地点概念，30 天必然超过，
            滑动窗口（丢最老的）正是它存在的理由，所以只 `logger.info` 陈述事实；
          · 经验概念被挤掉是**真丢失** —— 那正是该聚合进主脑的那部分，而窗口聚合后
            `clear_layer(LAYER_EXPERIENCE)` 一清（见 orchestrate/window.py），再也找不回来。
        为什么经验层连一个兜底上限都不给：它靠"每窗口聚合后清空"天然收敛（实测峰值 30 个概念、
        6 份 run 日志撞顶 0 次）。真涨到几百说明**它没被清空**，那是 bug —— 该让它显出来，
        而不是截断掉再假装全景是完整的。`dump_layer_full`（聚合读副脑经验层）因此结构上
        不再可能截断。

        ⚠️ **实测这个耦合的代价不在"经验层被丢"，而在"记忆窗口被悄悄缩小"**
        （2026-08-13 那次 30 夜 run 的 20 份副脑图谱，逐字节比对改前/改后）：
          · 经验概念一条都没被丢掉 —— 它们的 `created_at` 恰好都落在最近 N 内；
          · 但它们**占掉了记忆层的额度**：记忆层实际能注入的从 150 掉到 129~135
            （bob_w6 记忆 179+经验 21 → 记忆只出 129 条，凭空少 21 天的最老记忆）；
          · 更坏的一种：`carol_w4` 记忆 138 **还没到 150**、经验 21，合计 159 撞顶 →
            滑动窗口尚未该启动就被启动，砍掉了 9 条真记忆（含 `day_1`、`spawn_point_day1`、
            `todo_day3`）。全景说"更老的没列出来"，而那本是它够得着的。
        所以这不是"理论上不对但实际没事"，是一直在损失，只是损失落在记忆层而非经验层。

        撞顶时它仍只是**失控护栏**、不是常规截断：打 info 之外，**在返回文本开头拼一行给
        Agent 的告知**（`_TRUNCATED_PANORAMA_NOTE`，2026-08-15 加）。后者是重点：此前只有日志，
        模型侧看不到自己拿的是残缺清单，而 user_input 还断言「全景是完整的」。

        ★ **渲染顺序仍是 slug 字典序，只有"撞顶时丢谁"按 `updated_at` 倒序挑**：
        - 保持 slug 序是为了输出稳定（聚合侧的视图不因护栏而变，已按字节校验）；
        - 但撞顶时按字典序丢等于随机丢，所以先按 `updated_at DESC` 取前 N、再排回 slug。
          没撞顶时两种取法结果完全相同。

        `updated_at` 的刻度**由写入方定**（见 `reflect` 的 `at`，与 `created_at` 同源）：
        副脑写第几天、主脑写 round（见 openkage.roundcode）。**它由工具回写维护**
        （碰概念内容就推进，见 tools.py），不是建后不动 —— 这是把它当护栏判据的前提。
        `created_at` 作同 `updated_at` 下的稳定兜底。2026-08-08 之前副脑也写主脑的 round、
        且没有回写，一条轨迹内两个值全相同 → 排序退化成 slug 兜底序。现在副脑传 `day_idx`
        且工具回写，这个排序真的成立。
        """
        sql = "SELECT slug, name, summary, layer FROM concept"
        params: tuple = ()
        if layer is not None:
            sql += " WHERE layer = ?"
            params = (layer,)
        rows = [_concept_row_to_dict(r) for r in self._conn.execute(
            sql + " ORDER BY updated_at DESC, created_at DESC, slug", params).fetchall()]
        # ★ 护栏**只作用于记忆层**（见 docstring）：判据是 `len(mem)`，经验层不参与计数、
        #   也不被截断。`layer=LAYER_EXPERIENCE` 时 `mem` 恒空 → 这条路永不截断。
        mem = [r for r in rows if r["layer"] == LAYER_MEMORY]
        rest = [r for r in rows if r["layer"] != LAYER_MEMORY]
        n_memory = len(mem)
        truncated_note = ""  # 没撞顶就是空串 —— 拼进 user_input 不留任何痕迹
        if n_memory > self._memory_dump_max_concepts:
            # ★ **info 而非 warning**（2026-08-15 用户定案）：记忆层撞顶是滑动窗口**正常工作**，
            #   不是"规模失控"。旧文案说"通常意味着通名概念被写进了记忆层、检查反思 prompt"，
            #   而 30 夜 run 里它必然撞 —— 每天一条 `day_N` 加地点概念就够了，那是设计如此。
            logger.info(
                "%s: 记忆层有 %d 个概念，超过 %d（yml 的 "
                "memory_graph.memory_dump_max_concepts）—— 按滑动窗口只注入**最近被活动过的** %d 个"
                "（按 updated_at 倒序，丢掉的是最近未被碰过的、最可能失效的）。经验层不受此限、全量输出",
                "dump_layer_full" if with_content else "dump_layer_index",
                n_memory, self._memory_dump_max_concepts, self._memory_dump_max_concepts,
            )
            mem = mem[:self._memory_dump_max_concepts]
            # ★ **撞顶必须对 Agent 可见**，不能只留一行日志（2026-08-15）：
            #   实测本次 30 夜 run 里 `dump_layer_index` 撞顶 15 次（记忆层 152→186）。
            #   日志里有记录，但**模型什么也看不到** —— 它拿到的是一份被砍掉最老概念的
            #   清单，而 user_input 里紧接着写着「全景是**完整**的，没列出来的就是真的还没有，
            #   可以直接建」。这两句叠起来正是最坏的组合：清单不完整，却明确告诉它可以当成
            #   完整的用 —— 被丢掉的老概念于是被重建一遍，静默且不可察。
            #   ⚠️ **只有索引形态发这行**：正文形态（`dump_layer_full`）走的是聚合读副脑经验层，
            #   而护栏已按层解耦 —— 那条路上 `mem` 恒空、结构上进不来这个分支。留着这个条件
            #   是因为 `dump_layer_full()` 不传 layer 时理论上仍会带记忆层（当前无此调用方）。
            #   这里返回的这行由调用方原样拼进 user_input（它就是全景的第一行）。
            if not with_content:
                truncated_note = _TRUNCATED_PANORAMA_NOTE.format(
                    n_total=n_memory, n_shown=self._memory_dump_max_concepts,
                )
        rows = mem + rest
        rows.sort(key=lambda r: r["slug"])  # 渲染回字典序（见 docstring）
        # ★ **跨层边的去向由 `with_content` 决定**（2026-08-10 用户定案）：
        #   · `dump_layer_full`（正文形态，唯一消费者是**聚合读副脑**）→ `mark`：
        #     跨层边保留并标成 `rel→day_1_death[memory]`。不这么做的话聚合看到的关系
        #     **全军覆没** —— 实测反思建的边几乎全是跨层的（记忆→经验），经验层内部一条都没有，
        #     于是每条经验都写着「已有关系：无」，「跨副脑归并」少了一整维输入。
        #     代价：记忆层的 slug 名会进聚合上下文（facet 正文与 summary 仍不会），
        #     由 aggregate.yaml 讲清「那只是连接锚点，查不到、也不许写进公共图谱」。
        #   · `dump_layer_index`（索引形态，反思读**自己那份**图谱）→ `drop`，保持原样：
        #     它本来两层都看得见（不传 layer），没有"泄漏"这回事；单层调用时跨层边是噪音。
        return truncated_note + _format_concepts_rich(
            self._conn, rows, peer_layer=layer,
            with_content=with_content,
            cross_layer="mark" if with_content else "drop")

    def dump_used_vocab(self, layer: str | None = None) -> str:
        """这份图谱**已经用过哪些 facet kind 与关系名**（各带次数），渲染成两行。

        ★ **存在的唯一理由：kind/relation 已放开为自由命名**（2026-08-09）。此前两者是 yaml 里
        的受控词表、每次都随 system prompt 发给 Agent，"该用哪个词"是确定的。现在 Agent 自己
        起名，而**每天的反思 / 每批的聚合是互不知情的独立会话** —— 不把已用的名字告诉它，
        同一个范畴会在第 1 天叫 `where`、第 7 天叫 `location`、第 20 天叫 `coords`。
        后果有两层，都很实：
          ① 「同一概念下每种 kind 只有一条」这条归并压力**失效** —— 两个名字各存一条，
             谁也不逼谁概括；
          ② 关系名现在**直接参与检索打分**（见 relation_semantics 的 fallback），
             同义异名把一类边的证据摊薄成好几份。
        再加上本次实验的目的本身（跑完反推词表）：碎成上百个一次性标签就什么也反推不出来。

        ⚠️ **与 `dump_layer_index` 不重复**，两者回答不同的问题：那个是「有哪些概念」
        （按概念逐条列、随图谱无界增长、记忆层撞护栏会截断，而**截断时连名字一起丢**）；
        本方法是「这个图谱管这类内容叫什么」（**行数由词表规模决定、不由概念数决定**，
        天然收敛在几十行）。所以本方法**不设护栏**：它涨不上去，真涨上去了那本身就是要看的现象
        （说明命名在发散，正是这次实验要观察的东西）。

        `layer` 只过滤 **kind**（facet 挂在概念上、概念有层），不传 = 两层都算。
        **关系不按层过滤**：边可以跨层（`第3天 --导致--> 夜间风险`），按层筛会把跨层边算给谁
        变成一个没有正确答案的问题；而这份清单的用途是"这个名字用过没有"，跨层边同样算用过。

        返回形如（空图返回「（无）」，与其它 dump 一致）::

            facet kind：what_happened(12)、working_method(8)、where(5)
            关系：precedes(11)、located_at(6)、requires(4)
        """
        params: tuple = ()
        sql = ("SELECT f.kind, COUNT(*) FROM facet f "
               "JOIN concept c ON c.slug = f.concept_slug ")
        if layer is not None:
            sql += "WHERE c.layer = ? "
            params = (layer,)
        # 次数降序、同次数按名字 —— 名字兜底让输出稳定（否则同频的顺序随插入序漂）
        kinds = self._conn.execute(
            sql + "GROUP BY f.kind ORDER BY COUNT(*) DESC, f.kind", params).fetchall()
        rels = self._conn.execute(
            "SELECT relation, COUNT(*) FROM edge "
            "GROUP BY relation ORDER BY COUNT(*) DESC, relation").fetchall()
        if not kinds and not rels:
            return "（无）"
        fmt = lambda rows: "、".join(f"{n}({c})" for n, c in rows) or "（还没有）"  # noqa: E731
        return f"facet kind：{fmt(kinds)}\n关系：{fmt(rels)}"

    def set_current_round(self, round_num: int) -> None:
        """训练循环每 batch 前调用，设定当前 round（新建/更新的行据此打 created_at /
        updated_at，viewer 靠它区分"种子带来的"与"本次训练建的"）。

        ★ 术语：本层的 **round = 一次 consolidation = 一个 batch**，编码为
        `roundcode.encode(epoch, batch, window)`，**不等于 epoch**（后者是「全部训练题跑一遍」，
        中文写「轮」）。编码/解码的唯一真相在 `openkage/roundcode.py`，别自己写乘法。
        全套约定见 openkage/brain.py 顶部；此处不写「轮」以免歧义。
        """
        self._current_round = round_num

    # ------------------------------------------------------------------
    # 强度：只由**任务域 Agent** 在检索时触发（见 schema.py 的强度机制说明）
    # ------------------------------------------------------------------

    def reinforce_concepts(self, slugs: "list[str]", gain: float) -> int:
        """给一批概念的强度加 `gain`，返回实际改了几个。

        ★ **这是全库唯一的强度写入口**。合法调用者只有任务域 Agent 的检索工具
        （两个增益值来自 yml 的 `memory_graph.strength.*`，默认 1.0 : 0.1）：
          · `expand(slug)` 命中 → `gain=strength.gain_expand`（Agent 主动深挖，高精度信号）
          · `retrieve` 命中     → `gain=strength.gain_retrieve`（只说明向量语义相近）

        ⚠️ **反思/聚合 Agent 不得调用**：它们在**写**知识，不是在用知识 —— 让写的人给自己
        打分，强度就退化成"谁写得多"。那 10 个窄 CRUD 工具里已经不存在任何强度字段的写操作。

        衰减/归档不需要了：强度**只增不减**也能表达遗忘 —— 被用到的升上去、没用到的原地
        不动，**相对排名**就是遗忘。

        ★ **检索命中同时推进 `updated_at`**（2026-09-12）："被用到"就是"被活动过"——
        这正是 `dump_memory_layer` 窗口排序的判据（见那边 docstring）。此前只有反思的
        写路径推进它，导致白天 Agent 检索命中的概念在注入窗口里照样沉底。写入用
        `self._current_round`（与反思写路径同一刻度），不取墙钟。
        """
        if not slugs or not gain:
            return 0
        uniq = list(dict.fromkeys(slugs))
        ph = ",".join("?" * len(uniq))
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE concept SET strength = strength + ?, updated_at = ? "
                f"WHERE slug IN ({ph})",
                (float(gain), self._current_round, *uniq),
            )
            n = cur.rowcount or 0
            self._conn.commit()
        if n:
            logger.debug("[强度] %d 个概念 +%.2f：%s", n, gain, uniq[:5])
        return n

    # ------------------------------------------------------------------
    # 反思：ReActAgent 维护 concept+facet 图（自己调 search_concepts 查已有内容）
    # ------------------------------------------------------------------

    def _reflect_system_prompt(self) -> str:
        """反思用的 system prompt（一个场景一个完整独立 prompt，不共享底座、不拼接）。
        基类 BrainGraph（主脑）**不提供**：主脑只被 consolidate 聚合写入（走 _AGGREGATE_SYSTEM_PROMPT），
        反思只发生在副脑 ArmGraph（覆写返回 free-relations 版）。"""
        raise NotImplementedError(
            "主脑 BrainGraph 不做反思（只做聚合）；反思请用副脑 ArmGraph"
        )

    async def reflect(
        self,
        at: int,
        user_input: str,
        *,
        log_tag: str = "reflect",
        log_extra: str = "",
        system_prompt: str | None = None,
        vocab: str | None = None,
    ) -> None:
        """通用反思核心（任务域无关）：把一段已由**任务域**组织好的 user_input 交给反思 ReActAgent
        自主写图谱。反思引擎本体——system prompt(_reflect_system_prompt)、10 个窄工具(_build_tools)、
        受控词表(按 self._vocab 选)——全部与任务域无关，各类任务通吃。

        ★ `system_prompt` / `vocab` 可选覆盖（2026-08-29，两阶段反思）：默认用本图的
          `_reflect_system_prompt()` + `self._vocab`（阶段1 经验/记忆反思）。阶段2（技能维护）传
          `system_prompt=SKILL_MAINTENANCE.system` + `vocab="skill_maintenance"`：换一套聚焦技能
          维护的 prompt + 仅 5 个技能工具，与阶段1 完全独立的 LLM 会话。log_tag 区分落盘文件。

        ★ `at` 就是写进 `created_at` / `updated_at` 的值，**刻度由调用方定**（2026-08-08）：
        反思发生在 ArmGraph，那里的刻度是**第几天**（一条轨迹几天、每天反思一次）；
        主脑侧的 `consolidate` 传的是 round（见 openkage.roundcode）。列名统一、语义分开 ——
        参数因此叫 `at` 而不是 `round_num`，免得副脑那边名不副实。

        词表由 `self._vocab` 决定：反思只发生在 ArmGraph → vocab="reflect" →
        工具接受两层的 kind，而层由 Agent 在 `create_concept` 时**显式声明**
        （`schema.layer_of_kind` 只校验「声明的层与 kind 是否矛盾」）。

        user_input 怎么组织（成败分布/参考答案/探索发现那套是**举例**）属于任务域，各域自己拼，
        走这个通用核心即可，记忆引擎不认识域概念。「图谱里已经有什么」也归任务域决定要不要前置：
        minecraft 前置**两层全景**（`dump_layer_index()` 不传 layer，索引形态），
        facet 正文仍靠 Agent 自己调 `get_concept`（想按语义找则 `search_concepts`）。
        框架层**不代任何域做这个决定** ——「前置多少」取决于该域图谱的规模与形状。
        ⚠️ 前置**全景**与曾被删除的**前置候选块**（带 query 的 topk 召回）不是一回事，
        删除理由见 tests/test_memory_tools.py::test_reflect_has_no_framework_side_candidate_recall。

        log_tag/log_extra 仅用于日志区分来源。"""
        from openkage.llm import get_llm  # 顺带确保 backend 已在 sys.path

        from openkage.agent_core.agent.events import AgentFinishReason
        from openkage.agent_core.agent.react import ReActAgent
        from openkage.agent_core.context.simple import SimpleContextBuilder
        from openkage.agent_core.tool.registry import ToolRegistry

        tools = _build_tools(
            self._conn, self._vec_store, self._lock, at,
            vocab=vocab or self._vocab, retrieval=self._retrieval,
            skill_store=self._skill_store, skill_registry=self._skill_registry,
            # ★ leave-one-out 消融（2026-09-18）：no_memory_layer/no_exp_text 时
            #   create_concept 拒绝被拆的那层（工具层校验，报错文案指路另一层）。
            ablation=self._ablation,
        )
        # 上下文留痕：把真正发给 LLM 的完整 messages 落盘（默认开，见 openkage.agent_io）。
        # ★ tag 带上 `at`：反思是**每天一次**、每次一段独立对话，共用一个文件名会让后一天
        #   覆盖前一天 —— 而"第几天的反思写了什么"正是要看的东西。
        # ★ log_tag 区分阶段：阶段1=reflect_exploration，阶段2=reflect_skill_maintenance（独立落盘）。
        dumper = ContextDumper(
            self._context_dir, f"{self._context_tag}_{log_tag}_at{at:03d}",
            kind="reflect", model_name=self._model_name,
            tool_defs_provider=lambda: ToolRegistry(tools).get_definitions(),
            extra={"at": at, "vocab": vocab or self._vocab},
        )
        agent = ReActAgent(
            llm=get_llm(self._model_name, role=self._model_role),
            context_builder=SimpleContextBuilder(system_prompt or self._reflect_system_prompt(),
                                                 on_build=dumper),
            tool_registry=ToolRegistry(tools),
            max_steps=self._max_reflect_steps,
        )
        try:
            # asyncio.shield 保护反思不被外部 task 取消打断（CancelledError 是 BaseException，
            # 普通 except Exception 漏过 → 图谱空写。shield 让内部协程跑完；外部取消仍会在
            # shield 处抛 CancelledError，由下面的 except BaseException 接住并记录。
            result = await asyncio.shield(agent.async_run(user_input))
        except asyncio.CancelledError:
            logger.warning("%s[at %d]: 反思任务被外部取消（asyncio.CancelledError），"
                           "图谱可能写到一半 —— 这一天的记忆不完整", log_tag, at)
            raise  # 向上传播，让调用方感知取消
        except Exception:  # pylint: disable=broad-except
            logger.exception("%s: Agent 运行异常，跳过本次记忆维护", log_tag)
            return
        # ★ reasoning 观测落盘（2026-09-16）：跑完整轮一次性写 <tag>_reasoning.json，
        #   只读不回灌上下文（见 agent_io.dump_reasonings）。tag 与上面 ContextDumper 同前缀。
        _reasonings = (result.metadata or {}).get("reasonings") or []
        if self._context_dir is not None:
            dump_reasonings(
                self._context_dir, f"{self._context_tag}_{log_tag}_at{at:03d}",
                _reasonings, model_name=self._model_name,
                extra={"at": at, "vocab": vocab or self._vocab, "log_tag": log_tag},
            )
        steps = _count_steps(result)
        logger.info(
            "%s[at %d]%s: %s (%d/%d 步)",
            log_tag, at, log_extra, (result.output or "")[:120],
            steps, self._max_reflect_steps,
        )
        # 撞顶要**显式喊出来**：反思撞顶被强制结束时 final_response 为空，若不单独打一条
        # warning，这个截断很容易淹没在几百行 httpx 日志里看不出来。撞顶意味着当天经历
        # 没写完（edge 和经验层往往是最后才写的），属于结果可信度问题。
        if result.finish_reason is AgentFinishReason.MAX_STEPS:
            logger.warning(
                "[%s] 用满 %d 步被强制结束 —— 这一天的记忆可能没写完（经验层/边常是最后才写的）。"
                "如果发现经验层为空或缺边，调大 configs/*.yml 的 memory_graph.max_reflect_steps",
                log_tag, self._max_reflect_steps,
            )
        # 截断与撞顶是**两种**没写完，修法完全不同（一个调 max_tokens、一个调 max_steps），
        # 所以分开报。截断更隐蔽：撞顶至少步数是满的，截断可能只跑了几步就"完成"了。
        elif result.finish_reason is AgentFinishReason.LENGTH_TRUNCATED:
            logger.warning(
                "[%s] 只跑了 %d 步就被 max_tokens 截断 —— 这一天的记忆几乎肯定没写完。"
                "thinking 模型的思考 token 与输出共用预算，请调大 configs/*.yml 的 "
                "llm.max_tokens.reflect_step（answer 要同步 >它）", log_tag, steps,
            )

    def ingest_raw_pool(self, round_num: int, arm_graphs: "dict[str, BrainGraph]") -> dict:
        """消融 raw_shared_pool 的"聚合"：把各副脑**经验层**的概念/facet/边原样追加进主脑，
        不归并、不去重、不做冲突消解（无聚合 Agent）。检索、技能共享、清层都与 Full 同。

        只读经验层；边只拷两端都在经验层的（跨到记忆层的边丢弃，守住"记忆私有"）。
        slug 加 `w{round}_{arm}_` 前缀避免跨窗口/跨臂撞名。"""
        from openkage.memory.tools import _index_concept  # 函数内 import：tools 顶部 import 本模块

        n_c = n_f = n_e = 0
        new_slugs: list[str] = []
        with self._lock:
            for arm_key, g in arm_graphs.items():
                prefix = f"w{round_num}_{arm_key}_"
                rows = g._conn.execute(  # noqa: SLF001
                    "SELECT slug, name, summary FROM concept WHERE layer = ?",
                    (LAYER_EXPERIENCE,)).fetchall()
                src_slugs = {r[0] for r in rows}
                for slug, name, summary in rows:
                    self._conn.execute(
                        "INSERT INTO concept (slug, name, summary, layer, category, "
                        "created_at, updated_at) VALUES (?, ?, ?, ?, NULL, ?, ?)",
                        (prefix + slug, name, summary, LAYER_EXPERIENCE, round_num, round_num))
                    new_slugs.append(prefix + slug)
                    n_c += 1
                    for kind, content in g._conn.execute(  # noqa: SLF001
                            "SELECT kind, content FROM facet WHERE concept_slug = ?", (slug,)):
                        self._conn.execute(
                            "INSERT INTO facet (concept_slug, kind, content, created_at, "
                            "updated_at) VALUES (?, ?, ?, ?, ?)",
                            (prefix + slug, kind, content, round_num, round_num))
                        n_f += 1
                for fr, to, rel in g._conn.execute(  # noqa: SLF001
                        "SELECT from_slug, to_slug, relation FROM edge"):
                    if fr in src_slugs and to in src_slugs:
                        self._conn.execute(
                            "INSERT OR IGNORE INTO edge (id, from_slug, to_slug, relation, "
                            "created_at) VALUES (lower(hex(randomblob(16))), ?, ?, ?, ?)",
                            (prefix + fr, prefix + to, rel, round_num))
                        n_e += 1
            self._conn.commit()
        for s in new_slugs:
            _index_concept(self._conn, self._vec_store, s,
                           part_max_chars=self._retrieval.index_part_max_chars)
        return {"concepts": n_c, "facets": n_f, "edges": n_e}

    async def consolidate(self, round_num: int, user_input: str) -> str:
        """聚合：把 N 份副脑图谱融合进主脑图谱，用跟反思 Agent 同构的 ReActAgent 自主写库。

        跟副脑反思完全同一范式——同一套 _build_tools 10 个窄工具、同一个 ReActAgent 循环、
        self._conn/self._vec_store 直接读写主脑图谱——差别只有两处：①system prompt 用
        _AGGREGATE_SYSTEM_PROMPT（多副脑融合规则）而非反思 prompt；②user_input 是"主脑图谱
        现状 + N 份副脑图谱全量"（由 orchestrate.consolidate.build_aggregate_input 拼好后传入）。

        主脑图谱执行侧受控**经验侧**词表（vocab="aggregate"，见类属性 _vocab）——
        聚合的输入是各私有图谱里**已经是经验层**的内容（记忆层根本不会被端上来，
        见 orchestrate.consolidate），所以它的职责是**跨副脑归并/加权**，而不是"从个例做抽象"。round_num 见 openkage.roundcode（三段编码），新建/强化的 facet 据此打
        created_at / updated_at。返回 Agent 的一句话总结（供日志/留痕）。

        ★ **聚合不再碰技能**（2026-08-27）：技能归各臂常驻 SkillStore 维护（谁写的谁维护），
        聚合只归并 concept/facet/edge 经验。原 skill_candidates 参数已删。"""
        from openkage.llm import get_llm  # 顺带确保 backend 已在 sys.path

        from openkage.agent_core.agent.events import AgentFinishReason
        from openkage.agent_core.agent.react import ReActAgent
        from openkage.agent_core.context.simple import SimpleContextBuilder
        from openkage.agent_core.tool.registry import ToolRegistry

        tools = _build_tools(
            self._conn, self._vec_store, self._lock, round_num,
            vocab=self._vocab, retrieval=self._retrieval,
        )
        # 上下文留痕（默认开，见 openkage.agent_io）。★ tag 带上 round_num：一次 run 有多次聚合
        # （epoch × batch × window，见 openkage.roundcode），共用文件名会让后一次覆盖前一次 ——
        # 而"每个窗口各归并了什么、公共图谱是怎么一步步长起来的"正是要看的东西。
        # 这份尤其值得留：聚合的 user_input 是 N 份副脑经验层的**全文**（一次几十 KB），
        # 跑完只在日志里留一句话总结，不落盘就再也拿不到它到底读了什么。
        dumper = ContextDumper(
            self._context_dir, f"{self._context_tag}_consolidate_r{round_num}",
            kind="consolidate", model_name=self._model_name,
            tool_defs_provider=lambda: ToolRegistry(tools).get_definitions(),
            extra={"round": round_num, "vocab": self._vocab},
        )
        agent = ReActAgent(
            llm=get_llm(self._model_name, role=self._model_role),
            context_builder=SimpleContextBuilder(_AGGREGATE_SYSTEM_PROMPT, on_build=dumper),
            tool_registry=ToolRegistry(tools),
            max_steps=self._max_consolidate_steps,  # 独立预算，见 __init__ 注释
        )
        try:
            result = await agent.async_run(user_input)
        except Exception:  # pylint: disable=broad-except
            logger.exception("consolidate: Agent 运行异常，跳过本次聚合")
            return ""
        # ★ reasoning 观测落盘（2026-09-16）：tag 与上面 ContextDumper 同前缀，只读不回灌。
        _reasonings = (result.metadata or {}).get("reasonings") or []
        if self._context_dir is not None:
            dump_reasonings(
                self._context_dir, f"{self._context_tag}_consolidate_r{round_num}",
                _reasonings, model_name=self._model_name,
                extra={"round": round_num, "vocab": self._vocab},
            )
        summary = (result.output or "")[:200]
        steps = _count_steps(result)  # 不是 len(history)，见 _count_steps
        logger.info(
            "consolidate[round %d]: %s (%d/%d 步)",
            round_num, summary, steps, self._max_consolidate_steps,
        )
        # 撞顶要**显式喊出来**：ReActAgent 自己只打一行 WARNING，混在几百行 httpx 日志里极易漏看。
        # 撞顶意味着聚合是被截断的，图谱很可能缺边、缺后半段 concept，属于结果可信度问题，
        # 不是性能问题。
        # 判据用 finish_reason 而非 `steps >= 上限`：后者依赖步数口径正确，一旦步数统计有
        # 误差这个比较就会失真。
        if result.finish_reason is AgentFinishReason.MAX_STEPS:
            logger.warning(
                "[聚合] 用满 %d 步被强制结束 —— 图谱可能缺边/缺概念（edge 常是最后才连的）。"
                "如果 Δedge=+0，优先调大 configs/*.yml 的 memory_graph.max_consolidate_steps",
                self._max_consolidate_steps,
            )
        # ★ 这一条是 `Δconcept=+0 Δfacet=+0 Δedge=+0` 最常见的**真实**原因（2026-08-14 实测）：
        # 聚合 Agent 查完一轮概念、正要写库时被 max_tokens 截断，而旧代码把"没有 tool_calls"
        # 当成正常收工 → 全程零告警，读日志的人会以为"它判断无需新增"。
        # 所以这里必须与"步数用满"分开报：两者修法不同，且截断往往只跑了个位数步。
        elif result.finish_reason is AgentFinishReason.LENGTH_TRUNCATED:
            logger.warning(
                "[聚合] 被 max_tokens 截断（未写完就结束）—— 若看到 Δconcept/Δfacet/Δedge 全为 +0，"
                "**这就是原因，不是「无需新增」**。thinking 模型的思考 token 与输出共用预算，"
                "调大 configs/*.yml 的 llm.max_tokens.reflect_step（answer 需同步 >它）",
            )
        return summary

    # ------------------------------------------------------------------
    # 题内聚合窗口的两件原语：快照（给聚合读）+ 清层（腾空经验层继续跑）
    # ------------------------------------------------------------------

    def snapshot_to(self, path: "Path") -> "Path":
        """把本图谱**原子**复制到 `path`，返回该路径。用于题内聚合窗口的快照。

        ★ 为什么是文件快照而不是「把经验层提取成一段文本」：`orchestrate.consolidate` 的整套
        入口（`_dump_arm_graph` / `_find_dedup_hints` / `_warn_layer_violations`）消费的是
        **图谱对象**（要 `_conn` 和 `dump_layer_full`）。快照成同 schema 的库，那边签名一字不改
        就能复用；提取成文本则要为窗口聚合另写一条并行的拼装路径 —— 两条必然漂移，而漂移的后果
        是「窗口聚合与批末聚合读到的经验不一样」，极难察觉。

        用 `sqlite3.Connection.backup()` 而非 `shutil.copy2`：copy 一个**活连接**的 db 文件可能
        拷到写了一半的页（这些库虽小，但反思刚写完就快照，正是最容易撞上的时刻）。backup 走
        SQLite 自己的备份 API，拿到的是一致快照。

        **不快照向量库**：唯一用到 vec 的是聚合的去重预筛，而它查的是 **primary** 的 vec store
        （`_find_dedup_hints` 里 `primary.search_concept_hits`），副脑自己的那份从不被读。
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            dest = sqlite3.connect(path)
            try:
                self._conn.backup(dest)
            finally:
                dest.close()
        return path

    def snapshot_with_vectors(self, path: "Path") -> "tuple[Path, Path | None]":
        """快照本图谱**连带向量库**，返回 `(图谱路径, 向量库路径或 None)`。

        与 `snapshot_to` 的分工（**别把两者合并**）：
          · `snapshot_to`     —— 给**聚合**读的副脑快照。聚合只读 `_conn`/`dump_layer_full`，
                                 去重预筛查的是 primary 的 vec store，所以副脑的 vec 不需要，
                                 快照它纯属浪费（vec 库比图谱大一个量级：实测 4.2MB vs 0.4MB）。
          · `snapshot_with_vectors` —— 给**主脑**留的历史快照。它存在的理由是事后要能
                                 `retrieve()` 它（持续学习曲线：拿 window K 的公共图谱去跑
                                 只读评测 / 新人迁移）。而 `retrieve` 的第一步就是向量锚定
                                 （`_anchor`）—— **没有 vec 库，检索直接退化成空召回**，
                                 快照就成了个打不开的罐头。

        向量库用 `shutil.copy2` 而非 `Connection.backup()`：sqlite-vec 的虚表
        （`vec0`）在 backup API 下**不保证**能重建（虚表的影子表是实现细节），而这里的调用
        时机是安全的 —— 聚合刚落地、下一个窗口还没开始写。为稳妥先 `wal_checkpoint(FULL)`
        把 WAL 里的页压回主库，否则 copy 出来的库会缺最后一批写入。

        向量通道未启用（没装 sqlite-vec / 没 embedder）时第二个返回值是 None，**不报错**：
        那种环境下检索本来就没有向量锚定，快照的可用性没有额外损失。
        """
        import shutil

        graph_path = self.snapshot_to(path)
        vec_src = getattr(getattr(self, "_vec_store", None), "db_path", None)
        if not vec_src or not getattr(self._vec_store, "enabled", False):
            return graph_path, None
        vec_src = Path(vec_src)
        if not vec_src.exists():
            return graph_path, None
        vec_dst = path.with_name(path.stem + "_vec.db")
        try:
            with self._lock:
                try:
                    self._vec_store._conn.execute(  # noqa: SLF001
                        "PRAGMA wal_checkpoint(FULL)")
                except Exception:  # pylint: disable=broad-except
                    pass  # 没有 WAL 或已 checkpoint —— copy 仍然是完整的
                shutil.copy2(vec_src, vec_dst)
        except Exception:  # pylint: disable=broad-except
            # 向量库没拷成 = 这份快照事后**检索不了**（只能看结构统计）。不静默：
            # 持续学习曲线正是靠检索这些快照画出来的，缺一个点就是曲线上一个洞。
            logger.exception("主脑快照的向量库复制失败：%s → %s（该快照事后无法检索）",
                             vec_src, vec_dst)
            return graph_path, None
        return graph_path, vec_dst

    def clear_layer(self, layer: str) -> dict:
        """删掉 `layer` 这一层的全部概念（连带其 facet、**任一端落在该层**的边、向量条目），
        返回 `{"concepts": n, "facets": n, "edges": n, "vectors": n}` 计数供留痕。

        ★ **题内聚合后只清经验层、留记忆层**，这是本方法存在的唯一理由。两层的寿命本来就不同：
          · 经验层是通名的、**已经聚合进主脑**了 —— 留在副脑只会让下个窗口重复归并同一批结论，
            也是「副脑经验层随轨迹无界增长」的成因；
          · 记忆层是「我在哪、我做过什么、我还差什么」，**从不参与聚合**，是跨天唯一的通道。
            清掉它，第 K+1 天的 Agent 就成了个新人（营地坐标、没做完的事全丢），
            而那正是记忆层存在的唯一理由 —— 详见 `dump_memory_layer` 的 docstring。

        ⚠️ **跨层边一并删**：`A(记忆) --caused_by--> B(经验)`，B 删了这条边就成了半悬空的边。
        FK 的 `ON DELETE CASCADE` 本会处理，但这里**显式删**，与 `tools.DelConceptTool` 同一
        个理由：`PRAGMA foreign_keys` 是**连接级**的，哪天有个新连接忘了开，cascade 就静默失效
        留下孤儿边，而孤儿边会渲染进 prompt、让 Agent 沿一条死边去 `get_concept`。
        记忆端的概念**不受影响**（只掉这条边），这是有意的：它的 `where` 坐标仍然有效。

        向量条目逐个删（`_vec_store.delete`）：漏了它，检索会锚定到一个**已经不存在**的 slug 上
        —— 后续 `_fetch_concepts` 查不到、静默返回空子图，表现为「检索命中率突然掉下去」而
        没有任何报错。
        """
        with self._lock:
            slugs = [r[0] for r in self._conn.execute(
                "SELECT slug FROM concept WHERE layer = ?", (layer,)).fetchall()]
            if not slugs:
                return {"concepts": 0, "facets": 0, "edges": 0, "vectors": 0}
            ph = ",".join("?" * len(slugs))
            n_facets = self._conn.execute(
                f"SELECT COUNT(*) FROM facet WHERE concept_slug IN ({ph})", tuple(slugs)
            ).fetchone()[0]
            # 任一端在本层即删（跨层边也删，见 docstring）
            n_edges = self._conn.execute(
                f"SELECT COUNT(*) FROM edge WHERE from_slug IN ({ph}) OR to_slug IN ({ph})",
                (*slugs, *slugs),
            ).fetchone()[0]
            self._conn.execute(f"DELETE FROM facet WHERE concept_slug IN ({ph})", tuple(slugs))
            self._conn.execute(
                f"DELETE FROM edge WHERE from_slug IN ({ph}) OR to_slug IN ({ph})",
                (*slugs, *slugs),
            )
            self._conn.execute(f"DELETE FROM concept WHERE slug IN ({ph})", tuple(slugs))
            self._conn.commit()
        # 向量删除在锁外：`MemoryVectorStore` 自己有锁（见 vector_store.py），
        # 在这里嵌套持有两把锁没有必要，且它是另一个连接、与上面的事务无关。
        from .tools import _unindex_concept  # 函数内 import：tools 顶部 import 本模块，循环
        n_vec = 0
        for s in slugs:
            # ★ 删**全部**分片（identity + 每条 facet）。只删 identity 会留下 N 条孤儿 facet 分片
            # —— `search` 仍会命中它们，而对应概念已删 → 命中一个查不到的 slug，正是本函数
            # docstring 里那段「检索命中率突然掉下去」的另一种成因，且更难查。
            _unindex_concept(self._vec_store, s)
            n_vec += 1
        logger.info("clear_layer(%s)：删掉 %d 概念 / %d facet / %d 边 / %d 向量（分片已连带删）",
                    layer, len(slugs), n_facets, n_edges, n_vec)
        return {"concepts": len(slugs), "facets": n_facets, "edges": n_edges, "vectors": n_vec}

    def close(self) -> None:
        self._vec_store.close()
        self._conn.close()


# =====================================================================
# 副脑图谱 ArmGraph —— 每条副脑半自治维护的**私有记忆**图谱
# =====================================================================
# 副脑反思用的**完整独立** system prompt。它维护的是**私有记忆**（专名概念 + 时空指称边），
# 与聚合 prompt 维护的公共经验是两个范畴。一个场景一个完整 prompt，不共享底座。
#
# 来源是 `prompts/reflect/reflect.yaml`：那一份文件同时装着 system prompt、记忆侧词表+probe、
# 10 个工具的文案、以及各域的 user_input 模板。此处拿到的是**已把词表渲染进去**的成品
# （见 prompt_store）。
_EPHEMERAL_REFLECT_SYSTEM_PROMPT = REFLECT.system


class ArmGraph(BrainGraph):
    """副脑图谱 = 一条副脑的**私有记忆**：跟主脑 BrainGraph 同 schema、同检索/CRUD 逻辑，
    区别是它用**反思侧词表**（`vocab="reflect"`）。

    ★ **私有记忆 vs 公共经验是两个范畴**：

    - 私有记忆的概念是**专名**（`我的1号营地` / `第2天` / `东边那片矿洞`），facet 带出处
      （第几天、什么坐标），关系是时空指称（located_at / happens_during / precedes …）。
      它是**跨天的上下文压缩机制**：今天结束时留下"明天的我需要知道的"。
    - 公共经验的概念是**通名**（`夜间防卫`），去情境化，关系是推理（requires / causes …）。
    - **聚合只读经验层**：记忆层根本不会进聚合的上下文（见 orchestrate.consolidate 与
      prompts/aggregate/aggregate.yaml）。归纳由反思 Agent 自己做，聚合负责跨副脑归并。

    因为概念是专名，`(concept_slug, kind)` 唯一约束不挡"记多天的事"——
    第 2 天和第 3 天是两个概念，各有一条 what_happened。

    生命周期：minecraft 域 batches=n_train=n_samples=1，故一份 ArmGraph = 一个 epoch =
    **一条完整轨迹 = max_nights 天**，聚合后**保留存档**（2026-08-10 起不删，见 brain.py）。「跨天不跨轮」正好吻合 ——
    世界每轮重生，坐标类记忆跨轮就是假的。
    ⚠️ 这条前提依赖域声明：若 batches>1，一个 epoch 内会建多份 ArmGraph、私有记忆会在 batch
    边界断掉，届时需把宿主上移。

    物理上是独立 sqlite 文件（跟主脑图谱、跟任何生产库都物理隔离）。"""

    #: 用**反思 Agent** 的词表：两层 8 种 kind 都能用 —— 它要同时写
    #: memory 层（只给明天的自己）与 experience 层（**唯一会被聚合的**）。
    #: **类属性**（不在 __init__ 里赋值）—— 基类 __init__ 的层迁移就要用到，晚一步就错。
    _vocab = "reflect"
    #: 老库里 kind 不在词表的历史概念，在私有图谱里按记忆层算（那是它的主体）。
    _default_layer = LAYER_MEMORY
    #: 私有图谱上跑的是**反思** Agent（基类那个 `consolidate` 是给主脑聚合用的）。
    _model_role = "reflect"

    def _reflect_system_prompt(self) -> str:
        # 完整独立 prompt（不再是"基类受控 prompt + 追加 free 说明"的拼接）。
        # ★ leave-one-out 消融（2026-09-18）：no_memory_layer / no_exp_text 时把 system 的
        #   「## 1. 两层」节整段替换成 reflect.yaml `ablation_overrides.<rung>.
        #   section_1_replacement`（只改层指令——被拆层在工具层已拒写，指令把 Agent 的
        #   归纳冲动导向未拆层，防它反复撞拒绝浪费反思步数）。锚点替换失配即 raise：
        #   yaml 改版后锚点漂移必须当场炸，静默跑成带两层指令的 Full 口径是最贵的失败。
        # ★★ 2026-09-26 用户定：**所有 rung 的提示词改动一律走 append_line（末尾追加一行）**，
        #    原版 prompt 一字不动 —— 改整节指令就不是「Full 减一个模块」，实验前提会变。
        _line = REFLECT.raw_ablation_append_line(self._ablation)
        if _line:
            return _EPHEMERAL_REFLECT_SYSTEM_PROMPT + "\n\n" + _line + "\n"
        # 下面这条整节替换通道**保留但当前无 rung 再走**（_SECTION1_REPLACEMENT_RUNGS 为空）：
        # −M/−E 早年用过，若将来要恢复，把 rung 加进那个元组即可。
        if self._ablation not in _SECTION1_REPLACEMENT_RUNGS:
            return _EPHEMERAL_REFLECT_SYSTEM_PROMPT
        import re as _re

        override = REFLECT.raw_ablation_override(self._ablation)
        if not override:
            raise ValueError(
                f"reflect.yaml 缺 ablation_overrides.{self._ablation}.section_1_replacement"
                f"（消融 {self._ablation!r} 需要层指令覆盖）")
        # 锚点 = 从「## 1.」标题行到「## 2.」标题行之前（含两者之间的全部内容）。
        m = _re.search(r"^## 1\..*?(?=^## 2\. )", _EPHEMERAL_REFLECT_SYSTEM_PROMPT,
                       flags=_re.S | _re.M)
        if m is None:
            raise ValueError(
                "reflect.yaml 的 system prompt 里找不到「## 1. … ## 2.」锚点段——"
                "yaml 改版后请同步更新 ArmGraph._reflect_system_prompt 的替换锚点。")
        return _EPHEMERAL_REFLECT_SYSTEM_PROMPT[:m.start()] + override.strip() + "\n\n" \
            + _EPHEMERAL_REFLECT_SYSTEM_PROMPT[m.end():]


class NullGraph:
    """空记忆对照：跟 BrainGraph 同接口，但不产生任何存储/LLM 调用，永远表现为"空记忆"（消融/基线用）。"""

    def retrieve(self, query: str, hops: int = 1) -> list[dict]:  # noqa: ARG002
        return []

    # ★ retrieve_subgraph 必须与 BrainGraph **同签名同返回形状**：frozen 对照组走的就是
    #   本类，少一个 Agent 一调就 AttributeError、整条轨迹作废。空子图的 .empty 为 True，
    #   调用方的 `if not sg.empty` 分支天然把它当"没查到"处理。
    #   ⚠️ 参数也要跟着同步：工具按关键字调（`hops=…`、`layer=…`），签名对不上就是 TypeError，
    #   而 frozen 组走的正是这里 —— evolving 组一切正常，极易漏测。
    #   ★ 2026-09-06 实中此坑：tools.py 用 `layer=layer_arg` 关键字调，本签名漏了 layer →
    #     对照组每步检索 TypeError（虽被 except 降级、对无图谱组实际行为零影响，但日志噪音 +
    #     "结果不完整"标记不规范）。补齐 layer 参数。
    #   ★ 技能检索（retrieve_skills/list_verified_skills/clear_skills）不在本类：技能归属
    #     重构（2026-08-27）后技能只存在各臂 skill.db，NullGraph/BrainGraph 都不碰技能。
    #     frozen/no_graph 路径 skill_registry=None，RetrieveSkillsTool/_inject_all_skills 直接 miss。
    def retrieve_subgraph(self, query: str, hops: int = 1,
                          layer: str | None = None) -> MemorySubgraph:  # noqa: ARG002
        return MemorySubgraph()

    def expand_concept(  # noqa: ARG002
        self, slug: str, relation: str | None = None
    ) -> MemorySubgraph:
        return MemorySubgraph()

    def render_context(self, facets: list[dict]) -> str | None:  # noqa: ARG002
        return None

    def render_subgraph(self, sg) -> str | None:  # noqa: ARG002
        return None

    def dump_memory_layer(self, *, header: str = "") -> str | None:  # noqa: ARG002
        return None

    def dump_layer_index(self, layer: str | None = None) -> str:  # noqa: ARG002
        # 返回「（无）」而不是 ""：与 _format_concepts_rich 的空图返回值一致 ——
        # 调用方（反思 user_input）直接把它拼进文案，空串会拼出一个悬空的小标题。
        return "（无）"

    def dump_layer_full(self, layer: str | None = None) -> str:  # noqa: ARG002
        return "（无）"


    def dump_used_vocab(self, layer: str | None = None) -> str:  # noqa: ARG002
        # 同上：返回「（无）」而不是 ""。frozen 组的 rollout 也会走反思的 user_input 拼装路径
        # （reflect 本身是 no-op，但 user_input 在调用前就已经拼好了），少这个方法就是
        # AttributeError —— 且只在 frozen 组炸、evolving 组一切正常，极易漏测。
        return "（无）"

    async def reflect(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003, ARG002
        return

    def close(self) -> None:
        return
