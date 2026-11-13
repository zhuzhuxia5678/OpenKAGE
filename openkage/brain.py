"""OpenKAGE 高层门面：一行配置OpenKAGE系统、跑副脑任务、读能力增长曲线。

    from openkage import OpenKAGE, Arm
    from domains import build_domain

    domain = build_domain("math")            # 任务域/环境（math / minecraft / 自定义）
    octo = OpenKAGE(
        brain_config="configs/openkage_default.yml",
        arms=[Arm.from_file("arms/math/p1.md"), Arm.from_file("arms/math/p2.md")],
        domain=domain,                        # 框架 ↔ 任务域唯一契约（TaskDomain 协议）
        llm=None,                             # None = 用 yml 的 train.model（三角色各一个）
        seed="data/brain_seed.db",          # 可选：主脑初始知识（不给则空图起步）
        workdir="runs/demo",                 # 一次运行的落盘根目录（图谱/日志/结果）
    )
    train, eval_tasks = domain.build_tasks(cfg)
    octo.run(train, epochs=3, eval_tasks=eval_tasks)   # 跑 3 个 epoch（每 batch 一次聚合）
    curve = octo.eval_curve                  # evolving vs frozen 学习曲线（能力增长主张）
    # 终身学习域（如 minecraft，domain.train_curve=True）读 octo.train_curve

这是对底层引擎（BrainGraph / ArmGraph + orchestrate.loop 泛型编排 + TaskDomain）的门面封装：
OpenKAGE 只负责"配置 + 编排循环 + 落盘"，全程对 domain 多态调用，不出现任何具体任务域关键词；
真正的做任务(rollout)/反思/打分由 domain 提供，聚合/衰减/检索在记忆引擎里，不重复实现算法。

术语（对照真OpenKAGE神经解剖）：主脑 Brain 统筹一张持续演化的 BrainGraph；每条副脑 Arm 半自治地
做任务、各维护一张短生命周期 ArmGraph；每批(batch)结束主脑 consolidate 融合各副脑经验
（一致性加权 + 冲突消解 + 衰减）。详见 README 与论文。

术语约定（本仓最容易读错的一处，务必遵守）：

    epoch（中文写「轮 / 训练轮次」）
        = 全部训练题跑一遍。**记忆只能靠 epoch 跨轮累积**（arm 图谱按 (epoch,batch) 新建，
          副脑经验只有 consolidate 进主脑才跨轮**被后续轮次读到**）→ 它是学习曲线的 X 轴。
          ⚠️ 「跨轮存活」指的是**能被下一轮检索到**，不是文件还在不在：arm 图谱现在
          **跑完保留存档**（2026-08-10 起不再 unlink），但下一轮会新建自己那份、
          不会去读上一轮的 —— 所以对学习曲线而言语义没变。
        暴露为：yml `train.epochs` / CLI `--epochs` / `OpenKAGE.run(epochs=)`。

    batch（中文写「批」，**不要写「轮」**）
        = 一个 epoch 内训练题的一个分片。**每个 batch 结束做一次 consolidate**
          → 有几个 batch 就做几次**题间**聚合。暴露为：yml `train.batches` / CLI `--batches`。

    window（中文写「窗口」，2026-08-09 加）
        = **一条轨迹内部**的一个聚合窗口 → **题内**聚合。为什么需要它：`batch` 切的是**题目
          列表**，而 minecraft 一个训练题就是一整条 `max_nights` 天的轨迹 —— 「一条 365 夜的
          轨迹每 7 夜聚合一次」在 `batches` 上物理表达不出来（且 MC 的 n_train 被钉为 1，
          batches 恒等于 1）。它同时钉住**副脑经验层的规模上限**（K 夜的产出，而非整条轨迹）。
          暴露为：yml `domain_scale.<域>.aggregate_every` / CLI `--aggregate-every`。
          关掉（0）时一条轨迹只有末尾那一个窗口 = **与本维度存在之前的行为等价**。
          编排在 `orchestrate/window.py`，域侧靠 `on_window` 回调驱动。

    round（仅存在于记忆图谱层，中文写「consolidation round」或直接写 round，不写「轮」）
        = **一次 consolidation = 一个 (epoch, batch, window)**，编码走
        `openkage.roundcode.encode()`（`E*10000+B*100+W`，肉眼可读）。
        **编码/解码的唯一真相在 `openkage/roundcode.py`，别在别处自己写乘法** —— 此前编码散在
        brain/consolidate、解码在 viewer，加第三维时两份必然漂移（而漂移是静默的：viewer 把
        round 显示成怪值却不报错）。
        `BrainGraph.set_current_round()` 与**主脑侧**的 consolidate 都是这个语义。
        ⚠️ **DB 列不叫 round 了**（2026-08-08）：`concept/facet/edge.created_at`/`updated_at`
        —— 因为**副脑那边装的是"第几天"**（一条轨迹几天、每天反思一次），而不是 round。
        列名统一成中性的 `_at`、刻度由写入方定，见 `BrainGraph.reflect` 的 `at` 参数。

写中文注释/日志时：涉及 epoch 写「轮」，涉及 batch 写「批」，涉及 window 写「窗口」，
涉及图谱 round 写「round」。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from openkage.domain import EvalScore, Sample, Task, TaskDomain
from openkage.logctx import arm_log_ids, log_banner, log_scope
from openkage.memory.graph import BrainGraph, ArmGraph, NullGraph
from openkage.memory.schema import LAYER_MEMORY
from openkage.memory.skill_store import SkillStore, SkillRegistry, NullSkillStore
from openkage.orchestrate.loop import run_arm_batch, eval_readonly, eval_baseline_only
from openkage.orchestrate.consolidate import consolidate as _consolidate
from openkage.orchestrate.window import WindowAggregator, NightBarrier, SpawnBarrier
from openkage.orchestrate.checkpoint import resume_sources
from openkage.roundcode import encode as _encode_round
from openkage.config import ModelConfig, SkillConfig, TrainConfig, load_config

logger = logging.getLogger("brain")


@dataclass
class Arm:
    """一个副脑 Agent：一个独立的任务视角（人设/风格/易错倾向），跑任务并沉淀经验。

    对外只需 `Arm.from_file(md)`（读 arms/<domain>/*.md：frontmatter 元数据 + 正文 role）或
    `Arm.from_dict(...)`。key 是机器稳定 id（做落盘/日志的稳定标识）；role 是逐字节传给
    rollout/反思的"视角透镜"文本，不改写任务本身。

    **name 是 Agent 的名字**：一个**正常的英文人名**（Alice / Bob 这种），不是职能代号。
    - 语义上：人设（role）描述它的性格与取向，名字才是它的身份。
    - 技术上：minecraft 域拿 `name` 当**游戏内用户名**，MC 只接受 3-16 位 `[A-Za-z0-9_]`，
      中文会被服务器直接拒；多 Agent 联机协作时，这就是彼此称呼的名字。
    人读的角色描述由 `stage` / `style` / `role` 承载，不再单独存一个中文名。
    """

    key: str
    name: str = ""
    role: str = ""
    stage: str = ""
    style: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "Arm":
        return cls(
            key=d["key"], name=d.get("name", d["key"]), role=d.get("role", ""),
            stage=d.get("stage", ""), style=d.get("style", ""),
        )

    @classmethod
    def from_file(cls, path: str | Path) -> "Arm":
        """读一个副脑角色 md（frontmatter: name/stage/style；正文去掉一级标题即 role）。"""
        import re
        import yaml

        p = Path(path)
        text = p.read_text(encoding="utf-8")
        if not text.startswith("---"):
            raise ValueError(f"{p} 缺少 frontmatter，无法作为 Arm 角色文件")
        _, front, body = text.split("---", 2)
        meta = yaml.safe_load(front) or {}
        role = re.sub(r"\A\s*#[^\n]*\n+", "", body).strip()
        return cls(
            key=p.stem, name=meta.get("name", p.stem), role=role,
            stage=meta.get("stage", ""), style=meta.get("style", ""),
        )

    def as_dict(self) -> dict:
        """转成编排原语（run_arm_batch 等）消费的 dict 形态。"""
        return {"key": self.key, "name": self.name, "role": self.role,
                "stage": self.stage, "style": self.style}


class OpenKAGE:
    """OpenKAGE系统门面：一个主脑 + 若干副脑 + 一次运行的落盘目录。

    参数
    ----
    brain_config : str | Path | TrainConfig
        主脑图谱 + 检索/衰减/采样参数（yml 路径或已加载的 TrainConfig）。
    arms : list[Arm]
        副脑列表（至少 1 个）。
    llm : ModelConfig | None
        三个角色各自的底座模型（见 openkage.config.ModelConfig）；None 用 yml 的 train.model。
        ★ **不接受裸字符串**：那会让"一个模型跑三个角色"重新变成一条隐式路径，而调用方
          无法从 `llm="xxx"` 看出三个 Agent 是不是同一个模型（理由详见 ModelConfig）。
          要三个一样就 `ModelConfig(task=m, reflect=m, consolidate=m)`。
    seed : str | Path | None
        主脑初始知识图谱（sqlite）。给了就 copy 一份作为演化起点 + 一份 frozen 对照；
        不给则从空图起步（此时无 frozen 对照，gap 恒为 evolving 自身分）。
    seed_graph : str | Path | None
        ★ **跨 run 图谱迁移种子**（2026-09-20，论文 §3.4 迁移实验）：给一个**已完赛 run 的
        results 子目录**，本次 run 从它炼出来的知识资产起步 —— 三处文件级 copy：
        公共图谱 ``brain.db``（+ ``brain_vec.db``）、每臂技能库 ``skills/skill_{arm}.db``
        （+ ``_vec.db``）、私有图谱 ``arm_graphs/arm_{arm}_e{epoch}b{batch}.db``（+ ``_vec.db``）。
        随后**私有图谱清掉记忆层**（``ArmGraph.clear_layer(LAYER_MEMORY)``）：记忆层是
        「我在哪、我做过什么」的专名坐标与跨天日程，换一组 agent 就是语义错位，不能跟着搬。
        ⚠ 实测（DS 30 夜完赛 run）：私有图谱在 run 结束时**只有记忆层**——经验层每窗口聚合完
        就清了，15 个 checkpoint 快照里也一样。故「清掉记忆层」后私有图谱为空，新组私有图
        天然从空起步，实际迁过去的载荷 = **公共经验图谱 + 技能库**（正是 §3.4 要测的那份
        可分发资产）。这条注释是给后来人别再去找「私有经验层」了。
        与 ``seed`` 正交：``seed`` 是域 ``prepare()`` 产出的教材先验（math 域），两者不共用路径。
        与续跑的关系：**checkpoint 恢复优先于本种子**（同窗口同源），同传时以 checkpoint 为准。
    workdir : str | Path
        本次运行的落盘根目录（主脑/副脑图谱、临时文件都在这里，跟任何生产库物理隔离）。
    no_graph : bool
        **对照组 ablation**：True 时本次 run 整个作为
        **无图谱对照组** —— 主脑与各副脑图全部换成 NullGraph（不建/不读/不写任何记忆），
        聚合走 no-op、不跑 frozen 基线（对照组本身就是基线）。唯一变量是图谱体系（私有记忆/
        公共经验/技能/聚合），其余（arms 数、max_nights、aggregate_every、角色分配）与实验组
        完全一致，故两组 train_curve 的 per_arm 可逐角色 join（实验组 dave vs 对照组 dave）。
        ★ 2026-09-24 起【昨日目标】/【昨日经历】等跨天上下文管理**不随对照组跑**（domain 层
        context_mgmt=记忆层连带：NullGraph 无记忆层 → 整套消失；0911-0923 的「归还对照组」
        口径作废——那期间对照组由独立 flag 控制，0915 起默认关，故主表 nograph 行实况即「无
        昨日上下文」，与今日连带语义一致）。
        实验组是另起一次 ``no_graph=False`` 的 run。
        rollout 层无需改：NullGraph 作 primary 时 retrieve/dump 恒空，bot 即无记忆裸模型。
        续跑完全复用：checkpoint 以 (arm_key, round_num) 为键、与图谱无关，对照组天然能续。
    """

    def __init__(
        self,
        brain_config: "str | Path | TrainConfig",
        arms: list[Arm],
        domain: TaskDomain,
        llm: "ModelConfig | None" = None,
        seed: "str | Path | None" = None,
        workdir: "str | Path" = "openkage_run",
        n_samples: int | None = None,
        no_graph: bool = False,
        no_aggregate: bool = False,
        ablation: "str | None" = None,
        seed_graph: "str | Path | None" = None,
    ) -> None:
        if not arms:
            raise ValueError("至少需要一个副脑 Arm")
        self.cfg: TrainConfig = (
            brain_config if isinstance(brain_config, TrainConfig)
            else load_config(Path(brain_config))
        )
        self.arms = arms
        self.domain = domain
        # 三个角色各自的底座（唯一真相在 cfg.models / yml train.model）。
        # ★ **没有 `self.model_name` 了**（2026-08-15）：原先那一个属性同时喂 rollout /
        #   reflect / consolidate 三个 Agent，是"换模型只能三个一起换"的物理原因。
        #   留一个同名属性当"默认模型"更糟 —— 那就是漏传时静默落回某一个角色的入口，
        #   正是 RetrievalConfig docstring 里那次分叉的形状。要模型就明确说要哪个角色的。
        if llm is not None and not isinstance(llm, ModelConfig):
            raise TypeError(
                f"llm 必须是 ModelConfig（三角色各一个模型），收到 {type(llm).__name__}: "
                f"{llm!r}。裸字符串已不支持 —— 三个角色要用同一个模型请写 "
                f"ModelConfig(task=m, reflect=m, consolidate=m)（理由见 ModelConfig 的 docstring）"
            )
        self.models: ModelConfig = llm if llm is not None else self.cfg.models
        # 每题 rollout 次数：显式传入优先（runner 已把命令行/smoke/yml 三层解析完），否则取 yml。
        self.n_samples = int(n_samples) if n_samples is not None else self.cfg.n_samples
        self.seed = Path(seed) if seed is not None else None
        # ★ **跨 run 图谱迁移种子**（2026-09-20 §3.4，见类 docstring 的 seed_graph）：已完赛 run 的
        #   results 目录，本次从它炼出的知识资产起步。图谱组才有意义 —— 对照组全程 NullGraph，
        #   搬图进去没处装，直接报错而不是静默忽略（跑了 10 小时才发现没生效最贵）。
        self.seed_graph = Path(seed_graph) if seed_graph is not None else None
        if self.seed_graph is not None:
            if no_graph:
                raise ValueError(
                    f"seed_graph 与 no_graph 不可同开：对照组全程 NullGraph，种子图谱无处装载。"
                    f"（seed_graph={self.seed_graph}）")
            if not self.seed_graph.is_dir():
                raise ValueError(f"seed_graph 不是目录（要给已完赛 run 的 results 子目录）：{self.seed_graph}")
        self.workdir = Path(workdir)
        self.workdir.mkdir(parents=True, exist_ok=True)
        # ★ **对照组 ablation 标志**（见类 docstring 的 no_graph 参数）：True 时主脑/副脑图全用
        #   NullGraph、聚合 no-op、不跑 frozen 基线。由 _run_async 各处读它做短路。
        self.no_graph = bool(no_graph)
        # ★ **消融 A 无共享标志**（--no-aggregate，2026-09-15）：私有双层图照常（反思写入、
        #   day_start 记忆层注入、私有图检索），但两条共享通道全关——永不聚合（主脑 NullGraph、
        #   共享图恒空、检索只见自己的图）+ 技能只读本臂（跨臂 SkillRegistry 关）。与 no_graph 的
        #   区别：no_graph 副脑也 NullGraph（全程裸模型）；A 副脑是真 ArmGraph（反思照常写、
        #   经验层满 30 夜不清）。由 _run_async 各处读它做短路。与 no_graph 互斥（见 run.py 校验）。
        # ★ **leave-one-out 消融字符串**（2026-09-18，run_ablation.py 专用）：五 rung 之一
        #   （no_shared_graph/flat_retrieval/no_memory_layer/no_exp_text/no_exp_skills）。
        #   no_shared_graph 在 brain 层的语义 = 旧 A 无共享（主脑 NullGraph+聚合 no-op+技能
        #   arm_only_view），故在此派生 no_aggregate 布尔、既有守卫全部复用；其余四 rung 的
        #   拆除点在 domain（工具装配/prompt 条件块）与 graph.py（flat 检索/层拒绝写入），
        #   brain 层只把字符串透传给 ArmGraph/主脑构造。None=非消融 run（run.py 的 Full/对照，
        #   由 no_graph 布尔表达，两者互斥）。
        self.ablation = ablation
        if ablation == "no_shared_graph":
            no_aggregate = True  # −S：brain 层语义与旧 A 完全一致，派生布尔走既有守卫
        if ablation is not None and ablation != "no_shared_graph" and no_aggregate:
            raise ValueError(
                f"ablation={ablation!r} 与 no_aggregate=True 不可同开：leave-one-out 一次只拆"
                f"一个模块，no_shared_graph 才是「共享」rung（由 ablation 派生，勿手动传）。")
        self.no_aggregate = bool(no_aggregate)

        if self.no_graph:
            # 对照组：主脑/副脑全程 NullGraph（不建/不读/不写记忆）。不调 _make_brain —— 它会
            # 真造 brain.db 并 copy 种子，对照组不要这些。NullGraph 与 BrainGraph 同接口、retrieve
            # 恒空、reflect 是 no-op，rollout 层照常跑、bot 即无记忆裸模型。
            self.brain: BrainGraph | NullGraph = NullGraph()
            # 对照组本身就是基线（无记忆水平线），再 frozen 一次是重复且语义错位 → 也 NullGraph
            # （且第 4 处改动会让基线采样整个跳过，这个 NullGraph 只是占位不被真用）。
            self._frozen: BrainGraph | NullGraph = NullGraph()
        elif self.no_aggregate:
            # ★ 消融 A 无共享：主脑 NullGraph（公有检索恒空、共享图恒空）——与 no_graph 同处理；
            #   frozen 也 NullGraph（主脑空图，frozen=「冻结在初始状态的记忆」无意义）。但副脑
            #   走真 ArmGraph 分支（见 _run_async 里 `if self.no_graph` 的对照——A 不进那个分支）。
            #   不调 _make_brain（主脑恒空无需造 brain.db / copy 种子）；不调 _restore_brain_from_checkpoint
            #   （主脑恒空无需恢复 public.db）。
            self.brain = NullGraph()
            self._frozen = NullGraph()
        else:
            # ★ **主脑续跑从 checkpoint{W}/public.db 恢复**（2026-09-07 三者同源改造）：
            #   主脑此前是异类——续跑时 sqlite3.connect 直接打开残留 brain.db，带着上次 run 后期
            #   污染记忆跑，靠记忆操作法手工 cp public.db。现在与活图谱 private.db / 技能 skill.db
            #   同源：同一个 latest_window_with_world 取窗口 W、同一个 checkpoint{W}/ 目录。
            #   在 _make_brain **之前**拷文件（与活图谱「先 copy2 拷文件、再 connect」同模式）——
            #   _make_brain 无种子分支直接 BrainGraph(brain.db) 打开刚拷好的文件；有种子分支会
            #   覆盖（math 域语义，主脑恢复对无种子域才有意义）。非续跑/无快照 → 不动，原行为。
            # ★ 迁移种子：checkpoint 恢复优先（续跑语义），没恢复上才铺种子公共图。
            #   两者都走「先 copy2 文件、再 connect」的同一模式（见 _make_brain 上方的注释）。
            _restored = self._restore_brain_from_checkpoint()
            if not _restored:
                self._seed_public_graph()
            self.brain = self._make_brain(self.workdir / "brain.db",
                                          self.workdir / "brain_vec.db")
            # frozen 对照组 = 「冻结在初始状态的记忆」，与 evolving(self.brain) 对跑出 gap。
            # 两种初始状态：
            # ① 有种子（math：KG 教材种子）→ 复制一份种子库当 frozen，它冻结在"只有先验、不吸收经验"。
            # ② 无种子（探索域，如 minecraft）→ 初始状态**就是空图**，
            #    故 frozen = NullGraph（永远检索不到东西）= **无记忆裸模型基线**。这正是"记忆到底有没有用"
            #    要的对照，不是"没种子就不需要对照组"。
            if self.seed is not None:
                self._frozen = self._make_brain(self.workdir / "frozen.db",
                                                self.workdir / "frozen_vec.db")
            else:
                self._frozen = NullGraph()
        self.eval_curve: list[dict] = []
        #: 训练曲线（domain.train_curve=True 时填充）：每轮一个点，Y = 本轮各 arm 训练得分均值。
        #: 终身学习域（Minecraft）用它替代逐轮 eval —— 训练轮次本身就是学习曲线的 X 轴。
        self.train_curve: list[dict] = []
        self.consolidation_log: list[dict] = []

    # ---- 构造主脑图谱（可选从 seed copy 起步）----
    def _make_brain(self, db: Path, vec: Path) -> BrainGraph:
        if self.seed is not None:
            seed_vec = self.seed.with_name(self.seed.stem + "_vec.db")
            shutil.copy2(self.seed, db)
            if seed_vec.exists():
                shutil.copy2(seed_vec, vec)
            else:
                # 概念/facet 结构库拷过去了，但向量库没有——BrainGraph 会在 vec 路径上
                # 静默建一张空的 vec0 虚表，效果等同 NullGraph：检索恒 0 命中，且日志上
                # 跟"正常有种子向量"完全无法区分。必须在这里就喊出来，否则只能靠事后
                # 排查检索命中率才能发现。
                logger.warning(
                    "种子向量库不存在：%s（种子 db 是 %s）——将以空向量库启动，"
                    "检索会因此恒 0 命中，直到重新导入/生成种子向量",
                    seed_vec, self.seed,
                )
        mg = self.cfg.memory_graph
        # 检索参数**整个 RetrievalConfig 传进去**，不再逐字段拆开传 —— 逐字段传就是
        # 「漏传一个就静默落回另一份默认值」的入口，副脑那次分叉正是这么发生的
        # （见 RetrievalConfig 的 docstring）。副脑构造点也传同一个实例。
        # 聚合步数不在这里算：yml 的 memory_graph.max_consolidate_steps 就是它的值
        # （原先这里按 arm 数算一个自动值传进去，与 graph.py 的兜底不一致且从没跑过，已删）。
        #
        # `context_dir` 显式给 `<workdir>/contexts`（**三个 Agent 的留痕都进这一个目录**：
        # 任务域 ReAct 由 runner 传同一个路径）。不靠 graph.py 的兜底（db 旁边）——
        # 那会让主脑留痕落在 `<workdir>/contexts`、副脑落在 `<workdir>/arm_graphs/contexts`，
        # 同一次 run 的上下文被劈成两处，复盘时得记住去哪儿找。
        # `model_name` 给**聚合**那个角色：BrainGraph 上唯一跑 LLM 的入口是 `consolidate()`
        # （主脑不 reflect —— 反思写的是副脑图谱，见 ArmGraph 那个构造点）。
        # ★ 消融主脑侧接线（2026-09-22，修 −R 公共图漏压平的实装 bug）：主脑此前**不收**
        #   ablation —— flat_retrieval 的 flat 短路只在副脑 ArmGraph 生效，公共图检索
        #   （RetrieveMemoryTool 的 public scope → self.brain.retrieve_subgraph）全程走真子图
        #   （实证：flatret run 的留痕里公共命中块是「相关子图 N 概念 M 内部关系」，flat 短路
        #   不可能给出内部边）。与副脑构造点同口径：**只传图谱层认识的 rung**
        #   （flat_retrieval / no_memory_layer / no_exp_text），
        #   no_shared_graph / no_exp_skills 主脑无动作（−S 主脑是 NullGraph 压根不进本分支）。
        return BrainGraph(db, vec_db_path=vec, model_name=self.models.consolidate, retrieval=mg,
                          context_dir=self.workdir / "contexts",
                          ablation=(self.ablation
                                    if self.ablation in ("flat_retrieval",
                                                         "no_memory_layer",
                                                         "no_exp_text", "no_exp_layer",
                                                         "raw_shared_pool")
                                    else None))

    def _restore_brain_from_checkpoint(self) -> bool:
        """续跑时把主脑 brain.db 从 ``checkpoint{W}/public.db`` 恢复（与活图谱/技能同窗口 W）。

        ★ **三者同源**（2026-09-07）：主脑此前是续跑恢复的异类——直接打开残留 brain.db 带着
          上次 run 后期污染跑，靠记忆操作法手工 cp public.db。现与活图谱 private.db / 技能
          skill.db 同源：同一个 ``latest_window_with_world`` 取窗口 W、同一个 ``checkpoint{W}/``
          目录。在 ``_make_brain`` 之前拷文件，让 ``_make_brain`` 无种子分支直接打开恢复好的库。

        判定（不依赖 start_epoch——本方法在 ``__init__`` 调，那时还没 start_epoch）：
          1. glob 扫第一个臂的扁平 JSON ``checkpoint_{arm}_r*.json``，取最新一个。无 → 非续跑。
          2. 读它的 ``complete``：True（上轮完成）→ 不恢复，原行为。False → 续跑。
          3. ``W = latest_window_with_world(workdir, arms[0])``。None（第一窗口前停）→ 不恢复。
          4. ``checkpoint{W}/public.db`` 不存在（早期 run 无 public 快照）→ 不恢复，原行为。
          5. 拷 ``public.db → brain.db`` + ``public_vec.db → brain_vec.db``（存在则拷）。

        ★ W 用第一臂的 ``latest_window_with_world``（与活图谱/技能同源），**不单独扫 public.db**：
          三者 W 一致是第一优先级。边缘情况（第一臂异常退场早、其 W 落后于 public.db 的 W）
          → ``checkpoint{W}/public.db`` 不存在 → 不恢复（安全退路：宁可不恢复，不取错窗口）。
        ★ round_num 不依赖 start_epoch：glob 取扁平 JSON 后从文件内容读 ``round_num`` 字段，
          调 ``resume_sources`` 复用同一判定（保证与活图谱/技能的 is_resume 语义一致）。
          minecraft 单轨迹只有一个 round，glob = 精确匹配；多 epoch 亦一致（见计划）。
        ★ 失败 broad-except 退回原行为（打开残留），不拖构造——主脑恢复是 IO 旁路。

        返回是否实际恢复（True=已拷 public.db，False=非续跑/无快照/失败 → _make_brain 原行为）。
        """
        if not self.arms:
            return False
        first_arm = self.arms[0].key
        try:
            import glob as _glob
            import json as _json
            # glob 扫该臂所有 round 的扁平 JSON，取最新修改的一个（续跑语义=最近一次未完成 run）
            flats = _glob.glob(str(self.workdir / f"checkpoint_{first_arm}_r*.json"))
            if not flats:
                return False  # 全新 run，无任何 checkpoint
            latest = max(flats, key=lambda p: Path(p).stat().st_mtime)
            data = _json.loads(Path(latest).read_text(encoding="utf-8"))
            if data.get("complete"):
                return False  # 上轮已完成，正常新跑
            round_num = int(data.get("round_num", 0))
            is_resume, w, paths = resume_sources(self.workdir, first_arm, round_num)
            if not is_resume or w is None or paths["public"] is None:
                return False  # 第一窗口前停 / 早期 run 无 public 快照
            public = paths["public"]
            public_vec = public.with_name(public.stem + "_vec.db")
            shutil.copy2(public, self.workdir / "brain.db")
            if public_vec.is_file():
                shutil.copy2(public_vec, self.workdir / "brain_vec.db")
            logger.info("[brain] 续跑主脑从 %s 恢复（窗口%d，与活图谱/技能同源）", public, w)
            return True
        except Exception:  # pylint: disable=broad-except
            logger.exception("[brain] 续跑主脑恢复失败，退回原行为（打开残留 brain.db）")
            return False

    # ═══════════════════════════════════════════════════════════════════════════
    # 跨 run 图谱迁移种子（2026-09-20，论文 §3.4「知识是可插拔资产」实验）
    # ═══════════════════════════════════════════════════════════════════════════
    # 三处文件级 copy（公共图谱 / 技能库 / 私有图谱），一律在**对应对象 connect 之前**做
    # （与续跑恢复同模式，见 _restore_brain_from_checkpoint 上方的理由：连上了再覆盖文件，
    # 活连接拿到的是不一致状态）。私有图谱额外清一层记忆层 —— 那是「我在哪、我做过什么」
    # 的专名坐标与日程，换一组 agent 就是语义错位。
    #
    # ⚠ 三处都是**尽力而为**：失败只记日志 + 退回「从空起步」，不抛异常拖垮训练
    # （IO 旁路，与续跑恢复同样的处置）。但每一处都会在日志里喊出来 —— 静默退成空图
    # 等于这次迁移实验白跑 10 小时，必须让巡检第一眼看到。

    def _seed_public_graph(self) -> bool:
        """把种子 run 的公共图谱 ``brain.db``（+ 向量）拷进 workdir，作为本次演化起点。

        公共图谱装的是**经验层**（已调和的通名结论），是迁移实验要测的那份资产本体。
        返回是否真的铺上了种子。
        """
        src = self.seed_graph
        if src is None:
            return False
        sdb, svec = src / "brain.db", src / "brain_vec.db"
        if not sdb.is_file():
            logger.warning("[seed-graph] 种子目录里没有 brain.db：%s —— 公共图谱退回从空图起步", sdb)
            return False
        try:
            shutil.copy2(sdb, self.workdir / "brain.db")
            if svec.is_file():
                shutil.copy2(svec, self.workdir / "brain_vec.db")
            else:
                # 与 _make_brain 里那条同因：结构库拷过去、向量库没有 → 检索恒 0 命中，
                # 且日志上跟「正常有种子的图」无法区分。必须喊。
                logger.warning("[seed-graph] 种子公共图谱缺向量库 %s —— 公共检索将恒 0 命中", svec)
        except Exception:  # pylint: disable=broad-except
            logger.exception("[seed-graph] 公共图谱种子拷贝失败，退回从空图起步")
            return False
        try:
            conn = sqlite3.connect(self.workdir / "brain.db")
            try:
                layers = dict(conn.execute("SELECT layer, COUNT(*) FROM concept GROUP BY layer"))
                n_facet = conn.execute("SELECT COUNT(*) FROM facet").fetchone()[0]
                n_edge = conn.execute("SELECT COUNT(*) FROM edge").fetchone()[0]
            finally:
                conn.close()
            logger.info("[seed-graph] 公共图谱种子 ← %s：%s（facet=%d edge=%d）",
                        src.name,
                        "、".join(f"{k}={v}" for k, v in sorted(layers.items())) or "空图",
                        n_facet, n_edge)
        except Exception:  # pylint: disable=broad-except
            logger.exception("[seed-graph] 公共图谱种子统计失败（不影响起跑，仅丢留痕）")
        return True

    def _seed_skill_store(self, arm_key: str, skill_dir: Path) -> bool:
        """把种子 run 该臂的技能库 ``skill_{arm_key}.db``（+ 向量）拷进 workdir/skills/。

        ⚠ **按臂名对号入座**：源 run 与新组都是 alice/bob/carol（`--arms 3` 取前三个角色
        md），同名即同库。臂名对不上时**跳过并告警** —— 把 alice 的技能塞给 carol
        是另一种错配（技能里的 intent/签名是那个 agent 的写法）。
        """
        src = self.seed_graph
        if src is None:
            return False
        sdb = src / "skills" / f"skill_{arm_key}.db"
        if not sdb.is_file():
            logger.warning("[seed-graph] 种子目录里没有该臂技能库：%s —— %s 的技能从空起步",
                           sdb, arm_key)
            return False
        try:
            shutil.copy2(sdb, skill_dir / f"skill_{arm_key}.db")
            svec = sdb.with_name(f"skill_{arm_key}_vec.db")
            if svec.is_file():
                shutil.copy2(svec, skill_dir / f"skill_{arm_key}_vec.db")
            else:
                logger.warning("[seed-graph] 技能向量库缺失 %s —— 该臂技能语义检索恒 0 命中", svec)
        except Exception:  # pylint: disable=broad-except
            logger.exception("[seed-graph] %s 技能库种子拷贝失败，退回从空起步", arm_key)
            return False
        try:
            conn = sqlite3.connect(skill_dir / f"skill_{arm_key}.db")
            try:
                n = conn.execute("SELECT COUNT(*) FROM skill").fetchone()[0]
                nv = conn.execute("SELECT COUNT(*) FROM skill WHERE verified=1").fetchone()[0]
            finally:
                conn.close()
            logger.info("[seed-graph] %s 技能库种子 ← %s：%d 条（verified=%d）",
                        arm_key, sdb.name, n, nv)
        except Exception:  # pylint: disable=broad-except
            logger.exception("[seed-graph] %s 技能库种子统计失败（不影响起跑）", arm_key)
        return True

    def _seed_private_graph(self, arm_key: str, epoch: int, batch_id: int, dst: Path) -> bool:
        """把种子 run 该臂的私有图谱拷到 ``dst``（ArmGraph 构造**之前**），返回是否落了种子。

        只做文件 copy —— **清记忆层留给调用方**在 ArmGraph 构造之后用
        ``clear_layer(LAYER_MEMORY)`` 做：那个方法同时清向量条目（``_vec_store.delete``），
        自己写 SQL 删 concept 会留下孤儿向量（检索锚到已删 slug，静默掉命中率）。
        """
        src = self.seed_graph
        if src is None:
            return False
        name = f"arm_{arm_key}_e{epoch}b{batch_id}.db"
        sdb = src / "arm_graphs" / name
        if not sdb.is_file():
            have = sorted(p.name for p in (src / "arm_graphs").glob(f"arm_{arm_key}_*.db")
                          if not p.name.endswith("_vec.db")) if (src / "arm_graphs").is_dir() else []
            logger.warning("[seed-graph] 种子里没有 %s（该臂有：%s）—— %s 私有图谱从空起步",
                           name, have or "无", arm_key)
            return False
        try:
            shutil.copy2(sdb, dst)
            svec = sdb.with_name(sdb.stem + "_vec.db")
            if svec.is_file():
                shutil.copy2(svec, dst.with_name(dst.stem + "_vec.db"))
        except Exception:  # pylint: disable=broad-except
            logger.exception("[seed-graph] %s 私有图谱种子拷贝失败，退回从空起步", arm_key)
            return False
        return True


    def run(self, tasks: list[Task], epochs: int = 1,
            eval_tasks: list[Task] | None = None, batches: int = 1,
            start_epoch: int = 1,
            on_epoch_end: "Callable[[int], None] | None" = None) -> "OpenKAGE":
        """同步入口（内部起 asyncio 事件循环）。tasks/eval_tasks 是 domain.build_tasks 产出的
        Task 列表。

        参数与 yml/CLI 同名同义（见 configs/openkage_default.yml）：
        - `epochs`：训练轮数。**记忆只能靠 epoch 跨轮累积**（arm 图谱按 batch 新建，
          下一轮不读上一轮那份；文件本身保留存档），故它就是学习曲线的 X 轴。
        - `batches`：把 train 切几个 batch = **聚合(consolidate)几次**。
        - `start_epoch`：本次循环第一个 epoch 的**绝对编号**（默认 1）。断点续训时由调用方
          （experiments/run.py 的 `--resume`）传入「旧 run 已完成的最大 epoch + 1」，
          使 `_encode_round`/日志/train_curve 里的 epoch 字段是跨进程连续的绝对轮次，
          而不是每次重启进程都从 1 数起——否则同一份 brain.db 上会有两个 epoch=1，
          曲线在图上会重叠而不是接续。
        - `on_epoch_end`：每个 epoch 的曲线点 append 完之后调用一次，传入刚完成的**绝对**
          epoch 号。用途是让调用方在每轮结束后立即把 train_curve/eval_curve 落盘一次
          （见 run.py 的增量 checkpoint），使进程中途被杀时已完成轮次的曲线数据不丢失。
          回调异常会被吞掉并记日志——checkpoint 写失败不该拖垮训练本身。
        返回 self 以便链式读结果。
        """
        asyncio.run(self._run_async(tasks, epochs, eval_tasks, batches, start_epoch,
                                    on_epoch_end))
        return self

    @staticmethod
    def _split(seq: list, n: int) -> list[list]:
        if n <= 1 or not seq:
            return [seq]
        size = (len(seq) + n - 1) // n
        return [seq[i:i + size] for i in range(0, len(seq), size)] or [seq]

    async def _run_async(self, tasks: list[Task], epochs: int,
                         eval_tasks: list[Task] | None, batches: int = 1,
                         start_epoch: int = 1,
                         on_epoch_end: "Callable[[int], None] | None" = None) -> None:
        arm_dicts = [a.as_dict() for a in self.arms]
        display = {a.key: a.name for a in self.arms}
        # 日志作用域用的短 arm 名（minecraft 的 key 太长，见 logctx.arm_log_ids）
        log_ids = arm_log_ids([a.key for a in self.arms])
        ephem_dir = self.workdir / "arm_graphs"
        ephem_dir.mkdir(parents=True, exist_ok=True)
        batch_lists = self._split(tasks, batches)
        # 曲线画法由域声明（见 domain.TaskDomain.train_curve）：
        # - False（math）：每轮训练后跑 evolving eval，Y = held-out eval 分（测泛化）。
        # - True（minecraft 这类终身学习域）：Y = **本轮各 arm 训练得分均值**，不跑逐轮 eval。
        #   理由：该域 eval 与 train 是同一个中性探索目标（内容全同）、每轮又 reset 世界，
        #   逐轮 eval 只是同条件下多采一条样，不提供 train 之外的新信息，却要付一条完整轨迹的
        #   墙钟（单 bot 串行）。训练轮次本身就是 X 轴。
        train_curve_mode = bool(getattr(self.domain, "train_curve", False))

        # ---- 训练前基线（对照组，全程只测一次）----
        # 评测预算（n_eval=K、epochs=N）：
        #   对照组 frozen  = K × 1     —— 训练前测一次，之后各 epoch 复用
        #   实验组 evolving = K × N    —— 每轮训练后测一次
        #   合计            = K × (1 + N)
        # frozen 只测一次的依据：它在整个 run 里**不可变** —— eval 恒传 secondary=None（不写任何
        # 图），无种子域的 frozen 更是 NullGraph（reflect 是 no-op）。每 epoch 重跑只是在同一固定
        # 条件下重复采样，白烧 epochs 倍墙钟（MC 单 bot 串行、一条轨迹几分钟）。
        # **不分有无种子，一律如此**：训练前主脑是空图（无种子域）还是带教材先验（math 有种子），
        # 都不再单独测 epoch0 的 evolving —— 曲线的 X 轴从 epoch1 起，Y 轴对照线由这一次基线拉平。
        #
        # ★ train_curve 模式下这一次基线**仍然要跑**：它是「空记忆水平线」，否则训练曲线上升无法
        #   与「世界随机性/运气」区分 —— 只花 1 条轨迹换来曲线的可解释性。
        #
        # ★ **与训练并发**：基线只是一条水平参照线，**训练不依赖它** —— frozen 组恒传
        #   `secondary=None`（不写任何图谱），无种子域的 frozen 更是 NullGraph，而训练读写的是
        #   evolving 主脑，两条路径无任何数据依赖。用 create_task 让它与第 1 轮训练同时跑，真正
        #   用到分数的地方（epoch 末算曲线点）再 await —— 那时它通常早已跑完，await 立即返回。
        #   两个前提，改这段前先确认还成立：
        #   ① 出生点 RNG 按 arm 派生独立流（基线走 `arm_key=None` → "_eval" 键，见
        #      MinecraftDomain._make_spawn_pick），并发不改变任何一条轨迹的出生点序列 → **可复现性不变**；
        #   ② 基线要独占一台 worker（MC 域），故 runner 需多备一台，否则某个副脑会在
        #      `pool.acquire()` 上排队、把省下的墙钟又还回去（见 experiments/run.py 的 worker 数计算）。
        #   日志上两边会交错，但行首 `[base frozen]` / `[e1b1 a1 #1]` 的作用域列足以区分
        #   （contextvar 在 create_task 时 copy 一份，互不串味，见 openkage.logctx）。
        baseline_task = None
        # ★ no_graph（对照组）不跑 frozen 基线：对照组本身就是「无记忆水平线」基线，再 frozen
        #   一次是重复且语义错位（它的 train_curve 点本就不该有 frozen_baseline 对照列）。
        #   minecraft 域默认 n_eval=0 → eval_tasks 为空本就不创建；这里是防御：用户若给对照组
        #   设 --n-eval >0 也不浪费那条轨迹。
        # ★ 消融 A 无共享也不跑 frozen：主脑 NullGraph，frozen=「冻结在初始状态的记忆」对空图无意义。
        if eval_tasks and not self.no_graph and not self.no_aggregate:
            baseline_task = asyncio.create_task(
                eval_baseline_only(self.domain, self._frozen, eval_tasks, self.models.task))

        # ★ **常驻 SkillStore**（技能归属重构 2026-08-27）：每臂一个 skill_{key}.db，
        #   跨 epoch/window 存活，整个 run 复用一份（续跑时打开已有文件追加，不重建）。
        #   no_graph 对照组用 NullSkillStore（不建文件，接口 no-op）。
        skill_dir = self.workdir / "skills"
        skill_dir.mkdir(parents=True, exist_ok=True)
        # ★ **续跑 skill.db 从 checkpoint 恢复**（2026-09-07 修「checkpoint 不存 skill 快照
        #   致续跑用污染活库 / 回退重跑丢全部技能」）：与活图谱 private.db / 主脑 public.db 恢复
        #   **三者同源**——同一个 `resume_sources(checkpoint_dir, arm_key, round_num)` 取同窗口 W
        #   的 paths["skill"]，窗口号 W 唯一来源 `latest_window_with_world`，保证记忆 / 技能 / 主脑
        #   取自同一窗口、不错配。在 SkillStore 构造**之前**做文件级覆盖（与活图谱「先 copy2 拷文件、
        #   再 connect」同模式），否则 SkillStore 已 connect 活库、再覆盖文件会拿到不一致状态
        #   （活连接 shutil.copy2 的老坑，见 graph.snapshot_to 用 backup 而非 copy2 的理由）。
        #   round_num = 本轮第一个 batch（续跑从 start_epoch 的 batch 1 起，与 epoch 循环内活图谱
        #   第一次恢复 `_encode_round(epoch, batch_id=1)` 逐字同窗口）。
        #   早期 run 的 checkpoint{W}/ 无 skill.db（本改动前不快照）→ paths["skill"] 为 None 跳过，
        #   退化原行为（打开活 skill.db）。no_graph 对照组无技能库、跳过。is_resume=False 不动。
        if not self.no_graph:
            _resume_round = _encode_round(start_epoch, 1)
            for a in self.arms:
                is_resume, _w, paths = resume_sources(
                    self.workdir, a.key, _resume_round)
                src_skill = paths["skill"] if is_resume else None
                if src_skill is None:
                    continue
                try:
                    dst_skill = skill_dir / f"skill_{a.key}.db"
                    shutil.copy2(src_skill, dst_skill)
                    src_skill_vec = src_skill.with_name("skill_vec.db")
                    if src_skill_vec.is_file():
                        shutil.copy2(src_skill_vec,
                                     skill_dir / f"skill_{a.key}_vec.db")
                    logger.info("[skill_store:%s] 续跑技能库从 %s 恢复（窗口同源 private.db/public.db）",
                                a.key, src_skill)
                except Exception:  # pylint: disable=broad-except
                    # IO 旁路：恢复失败退回活 skill.db（与修复前行为一致），不拖训练。
                    logger.exception("[skill_store:%s] 续跑技能库恢复失败，退回活库", a.key)
            # ★ 迁移种子（§3.4）：无 checkpoint 可恢复时，把种子 run 的同名臂技能库铺进来。
            #   在 SkillStore 构造**之前**（下面 arm_skill_stores 循环）做文件级覆盖。
            if self.seed_graph is not None:
                for a in self.arms:
                    self._seed_skill_store(a.key, skill_dir)
        arm_skill_stores: dict = {}
        for a in self.arms:
            if self.no_graph:
                arm_skill_stores[a.key] = NullSkillStore(a.key)
            elif self.ablation in ("no_exp_layer", "no_exp_skills"):
                # ★ −K 拆技能（2026-09-18）：全臂 NullSkillStore——create_skill 写库 no-op、
                #   list/retrieve 恒空、签名块注入无技能可注、阶段2 维护空库跳过、计数回填
                #   add_usage_counts no-op。技能拆除的全部语义由 NullSkillStore 接口承载，
                #   brain/domain 层无需再分支（domain 侧工具仍装配，但对空库操作全 no-op；
                #   retrieve_skills 工具由 domain 按 ablation 下架，见 __init__.py）。
                arm_skill_stores[a.key] = NullSkillStore(a.key)
            else:
                arm_skill_stores[a.key] = SkillStore(
                    skill_dir / f"skill_{a.key}.db",
                    vec_db_path=skill_dir / f"skill_{a.key}_vec.db",
                    arm_key=a.key,
                )
        # ★ 技能查重阈值（yml `skill.similar_warn_threshold`）：CreateSkillTool 建技能后按 intent
        #   跨臂语义检索，超阈值的已有技能塞进返回 warning（只警告不拦）。SkillConfig.load() 读
        #   同一份 yml（带进程级缓存），与 retrieval=self.cfg.memory_graph 同一个取值口径。
        skill_registry = SkillRegistry(
            arm_skill_stores, skill_config=SkillConfig.load())

        async def _baseline() -> "EvalScore | None":
            """取基线分（await 后台任务）。重复调用安全：Task 的结果会缓存，第二次 await 直接返回。

            基线失败**不拖垮整个 run**：它只是参照线，而训练轨迹已经烧掉了真金白银的墙钟。
            拿不到就退化成 None —— 下游 `_train_curve_point` / `eval_readonly` 本就接受 None
            （曲线点少一条对照线，不影响主结果落盘）。
            """
            if baseline_task is None:
                return None
            try:
                return await baseline_task
            except Exception:  # pylint: disable=broad-except
                logger.exception("frozen 基线采样失败 —— 曲线将缺少空记忆参照线，训练结果不受影响")
                return None

        for epoch in range(start_epoch, start_epoch + epochs):
            log_banner(logger, f"第 {epoch} 轮训练（本次运行第 {epoch - start_epoch + 1}/{epochs} 轮；"
                                "各副脑采样 + 反思 → 聚合进主脑）")
            epoch_records: list[dict] = []  # 本 epoch 所有 arm×task 的留痕，供训练曲线取分
            for batch_id, batch_tasks in enumerate(batch_lists, 1):
                # round = 一次 consolidation = 一个 (epoch,batch,window)（见类 docstring 的术语约定）。
                # 这里设的是本 batch 的**末窗口**号（题内聚合的各窗口在 consolidate 内部各自
                # encode）——rollout 期间副脑写自己的图谱用的是"第几天"刻度，与此无关。
                # ★ no_graph 时 self.brain 是 NullGraph，无 set_current_round —— 跳过（对照组
                #   不建主脑、round 号无处可写，跳过即正确）。
                # ★ 消融 A 无共享同理：主脑也是 NullGraph。
                if not self.no_graph and not self.no_aggregate:
                    self.brain.set_current_round(_encode_round(epoch, batch_id))

                arm_graphs: dict[str, ArmGraph] = {}
                for a in self.arms:
                    if self.no_graph:
                        # 对照组副脑图也用 NullGraph（非 None → night_cb 的 `if secondary is not
                        # None` 守卫照常放行，触发 on_window 拿窗口级 checkpoint；reflect 是
                        # no-op 不调 LLM、dump_* 返回「（无）」不炸）。不造 arm_*.db 文件。
                        arm_graphs[a.key] = NullGraph()
                        continue
                    eg = ephem_dir / f"arm_{a.key}_e{epoch}b{batch_id}.db"
                    egv = ephem_dir / f"arm_{a.key}_e{epoch}b{batch_id}_vec.db"
                    _seeded_private = False  # 本臂是否铺了迁移种子（决定构造后要不要清记忆层）
                    # ★ **续跑活图谱恢复**（2026-08-31 修「续跑活图谱未从 checkpoint 恢复」bug；
                    #   2026-09-07 三者同源改造）：ArmGraph 构造时 sqlite3.connect 直接打开已有
                    #   eg、不覆盖（见 graph.py），上次 run 残留的 eg 带着中断时的中间态（含已聚合
                    #   但未清的经验层），续跑直接打开会把污染带进新轨迹。从 checkpoint 窗口 W 的定格
                    #   快照 private.db 恢复——与 _fresh_world 的 world 恢复、技能 skill.db、主脑
                    #   public.db **同窗口同源**（同一 latest_window_with_world、同一 checkpoint{W}/）。
                    #   is_resume=True 但无 src（第一窗口前停 / 早期 run 无 private.db）→ 清残留
                    #   eg 从空记忆续跑，不带着上次 run 的污染继续。
                    #   is_resume=False（全新轨迹 / 上轮 complete）→ 不动，原行为（打开残留或新建）。
                    is_resume, _w, paths = resume_sources(
                        self.workdir, a.key, _encode_round(epoch, batch_id))
                    src_db = paths["private"] if is_resume else None
                    if src_db is not None:
                        try:
                            shutil.copy2(src_db, eg)
                            # vec 命名与 snapshot_with_vectors 一致（path.stem + "_vec.db"）
                            src_vec = src_db.with_name(src_db.stem + "_vec.db")
                            if src_vec.is_file():
                                shutil.copy2(src_vec, egv)
                            logger.info("[e%db%d] %s 续跑活图谱从 %s 恢复",
                                        epoch, batch_id, a.key, src_db)
                        except Exception:  # pylint: disable=broad-except
                            # IO 旁路，不拖训练：恢复失败则退回残留/新建（与修复前行为一致）
                            logger.exception("[e%db%d] %s 续跑活图谱恢复失败，退回残留/新建",
                                             epoch, batch_id, a.key)
                    elif is_resume:
                        # 续跑但无窗口 checkpoint 可精确恢复：清残留，从空记忆续跑
                        # （等价回退到轨迹起点，防第一窗口前停时残留 eg 污染）
                        for p in (eg, egv):
                            if p.exists():
                                try:
                                    p.unlink()
                                except Exception:  # pylint: disable=broad-except
                                    pass
                        logger.info("[e%db%d] %s 续跑无窗口 checkpoint，从空记忆续跑（已清残留）",
                                    epoch, batch_id, a.key)
                    elif self.seed_graph is not None:
                        # ★ 迁移种子（§3.4）：非续跑 → 铺种子私有图谱，随后清记忆层（见下方）。
                        _seeded_private = self._seed_private_graph(a.key, epoch, batch_id, eg)
                    # ★ **必须显式传 retrieval**：此前这里一个检索参数都没传，副脑于是落到
                    #   schema.py 的模块常量（min_score=0.45），而主脑用的是 yml 的 0.0 ——
                    #   两个图谱用不同检索口径跑了两天没人发现（2026-08-08 修）。
                    #   现在不传也会读同一份 yml（BrainGraph.__init__ 的兜底），显式传是为了
                    #   让"主副脑同口径"这件事在代码里看得见。
                    arm_graphs[a.key] = ArmGraph(
                        # `model_name` 给**反思**那个角色：副脑图谱上跑 LLM 的入口只有
                        # `reflect()`（聚合读它但那次调用发生在**主脑**的 consolidate 里，
                        # 用的是主脑那份 model_name —— 见 _make_brain 与 window.py 的快照）。
                        eg, vec_db_path=egv, model_name=self.models.reflect,
                        retrieval=self.cfg.memory_graph,
                        # 与主脑同一个留痕目录（不是 arm_graphs/contexts）——
                        # 同一次 run 的上下文集中一处，见 _make_brain 那处的理由。
                        context_dir=self.workdir / "contexts",
                        # ★ leave-one-out 消融（2026-09-18）：**只传图谱层认识的 rung**
                        #   （flat_retrieval / no_memory_layer / no_exp_text）——
                        #   graph.py 按值分支：flat_retrieval→retrieve_subgraph 锚定即止；
                        #   no_memory_layer→create_concept 拒 memory 层+dump_memory_layer
                        #   返 None；no_exp_text→拒 experience 层（经验层恒空）。
                        #   `no_shared_graph` 不传：图谱层无动作（拆除点在本文件 no_aggregate
                        #   派生 / skill_store 装配层），传过去会被 graph.py 的值域校验拒
                        #   （smoke 0918 实抓：单测没覆盖该装配路径）。
                        #   ★ `no_exp_skills` **要传**（2026-09-26 修）：它在图谱层的检索/写入
                        #   同样无分支，但**反思 prompt 的 append_line 由 ArmGraph 渲染**——
                        #   不传就渲染不出来，−K run 会照 §4 白撞 create_skill。已加进
                        #   GRAPH_ABLATION_VALUES，值域校验放行。
                        ablation=(self.ablation
                                  if self.ablation in ("flat_retrieval",
                                                       "no_memory_layer",
                                                       "no_exp_text", "no_exp_layer",
                                                       "no_exp_skills")
                                  else None))
                    if _seeded_private:
                        # ★ 迁移种子清记忆层（§3.4）：换一组 agent，源 agent 的「我的1号营地在
                        #   (184,64,557)」「第 N 天没做完的竖井」是纯噪声（且世界虽同 seed、
                        #   新组的实际轨迹不同，坐标指到的地方可能压根没有箱子）。经验层才是
                        #   通名结论 —— 但实测源 run 结束时私有图经验层为 0（每窗口聚合完即清），
                        #   故这里清完通常是空图，新组私有图本就从空起步。走 clear_layer 而不是
                        #   自己写 SQL：它连向量条目一起删（漏了会留孤儿向量、静默掉命中率）。
                        _st = arm_graphs[a.key].clear_layer(LAYER_MEMORY)
                        logger.info("[seed-graph] %s 私有图谱种子已清记忆层：%s", a.key, _st)
                    # ★ 注入本臂 SkillStore + 共享 SkillRegistry（技能归属重构 2026-08-27）：
                    #   ArmGraph 的反思期工具绑 SkillStore（create/update/list/search_skills），
                    #   create_skill 前通过 registry 跨臂查重（防同名注入冲突）。
                    # ★ 消融 A 无共享（--no-aggregate）：技能只读本臂——注入 arm_only_view 视图，
                    #   list_all_verified/retrieve_all 只返本臂技能、跨臂查重退化本臂（A 里无别的臂）。
                    #   Full 条件下注入全局 skill_registry（跨臂共享）。视图见 skill_store.arm_only_view。
                    arm_graphs[a.key]._skill_store = arm_skill_stores[a.key]  # noqa: SLF001
                    arm_graphs[a.key]._skill_registry = (  # noqa: SLF001
                        skill_registry.arm_only_view(a.key) if self.no_aggregate
                        else skill_registry)

                # 题内聚合窗口收集器（见 orchestrate/window.py）：域在自己的聚合边界上调
                # `on_window`，它就地快照+清该副脑经验层、攒齐一窗后**后台**跑聚合。
                # 域不做题内聚合（math，或 aggregate_every=0）时，`on_window` 一次都不会被调，
                # 收集器全程空转、`drain()` 返回空列表 —— 于是**行为与本机制存在之前完全一致**。
                # ★ no_graph：仍构造收集器（要靠它走 on_window → 写窗口 world checkpoint 续跑），
                #   但内部聚合走 no-op 短路（见 window.py 的 _run_window_sync）。
                # ★ 消融 A 无共享同理：副脑真图、要写 world checkpoint 续跑，聚合走 no-op 短路
                #   （不清层——经验层满 30 夜不清是 A 的特性）。
                windows = WindowAggregator(
                    self.brain, arm_graphs, display,
                    ephem_dir, epoch, batch_id,
                    checkpoint_dir=self.workdir,
                    round_num=_encode_round(epoch, batch_id),
                    no_graph=self.no_graph,
                    no_aggregate=self.no_aggregate,
                )
                # ★ 每晚三栅栏（2026-08-29）：各臂同一天同阶段反思跑完才一起进下一阶段/下一天，
                #   防"一臂已改技能、另一臂还在用旧"。_active 含全臂（含 no_graph 对照组——它们
                #   rollout 仍在跑轨迹、每晚到 night_cb，不参与会致别的臂在栅栏永久等它）。
                #   key 与 run_arm_batch 的 arm["key"] 同源。no_graph 臂照样到 night_cb（secondary
                #   是 NullGraph 非 None），故必须纳入 _active，否则实验臂会等它等到天荒地老。
                night_barrier = NightBarrier(arm_graphs.keys())
                # ★ 开局出生栅栏（2026-09-12）：所有臂 bot 出生后才一起迈第一步训练，防「4 臂已
                #   开训、录像卡死的臂晚 150s 才出生」。与 night_barrier 同样含全臂（含 no_graph 对照组）。
                #   纯等齐无 retire（出生是一次性事件，详见 SpawnBarrier 注释）。
                spawn_barrier = SpawnBarrier(arm_graphs.keys())

                arm_calls = [
                    # 这个 model_name 是**任务域 rollout** 的（run_arm_batch → domain.rollout）。
                    # 反思用的模型不从这里走 —— 它在 arm_graphs[...] 那份 ArmGraph 上。
                    # ★ 消融 A 无共享：run_arm_batch 透传给 rollout→_make_react_day 的 skill_registry
                    #   也要是 per-arm 视图（执行期 _inject_all_skills/_render_skill_delta 经它检索技能）。
                    #   与上面 arm_graphs[...]._skill_registry 注入同口径（A 用视图、Full 用全局）。
                    run_arm_batch(self.domain, ad, self.brain, arm_graphs[ad["key"]],
                                  batch_tasks, self.models.task, epoch, batch_id, self.n_samples,
                                  log_id=log_ids.get(ad["key"]),
                                  window_agg=windows,
                                  night_barrier=night_barrier,
                                  spawn_barrier=spawn_barrier,
                                  skill_registry=(
                                      skill_registry.arm_only_view(ad["key"]) if self.no_aggregate
                                      else skill_registry))
                    for ad in arm_dicts
                ]
                # run_arm_batch 返回逐 task records（含 sample_metrics）—— 收下它们，训练曲线要用。
                if getattr(self.domain, "parallel_arms", True):
                    results = await asyncio.gather(*arm_calls)
                else:  # 单 env 域（如 Minecraft 单 bot）：各 arm 串行
                    results = [await call for call in arm_calls]
                for recs in results:
                    epoch_records.extend(recs or [])

                # ① 先收题内聚合（窗口）的留痕：各副脑 rollout 期间已经在后台陆续跑了，
                #    这里只是等最后几个落地。**必须在批末聚合之前** —— 两者都写主脑，
                #    而聚合 Agent 是有状态的一串工具调用，让它们重叠等于两个 Agent 并发改同一张图。
                self.consolidation_log.extend(await windows.drain())

                # ② 批末聚合（**题间**）：把各副脑图谱里**剩下的**经验层归并进主脑。
                # ★ 两处细节都是端到端实测踩出来的：
                #   · window 号取 `windows.next_window` 而不是恒为 1 —— 否则批末这次与题内第 1 个
                #     窗口写出同一个 round 号，两次聚合的行在图谱里叠成一次（没开题内聚合时它
                #     恰好返回 1，编码与本机制存在之前完全一致）。
                #   · 经验层**全空就跳过** —— 开了题内聚合时末窗口刚清完，这里常常无事可做，
                #     跑一次是纯浪费的 ReActAgent 调用，还会让"经验层为 0"这条**用来抓真故障的**
                #     告警变成每批必现的噪音。
                # ★ no_graph：NullGraph 无经验层，批末聚合无意义（any_experience_left 本就返
                #   False，这里再 AND 一道 no_graph 是双保险 + 让日志走「跳过」分支而非「聚合」）。
                # ★ 消融 A 无共享：副脑经验层非空（真图、满 30 夜不清），但 A 不该聚合（共享图恒空、
                #   主脑 NullGraph 无 consolidate）—— AND 一道 no_aggregate 跳过批末聚合。
                if (not self.no_graph and not self.no_aggregate
                        and windows.any_experience_left()):
                    # 聚合日志同样带作用域：它跑的是**主脑**写入，与上面各副脑的日志要能一眼区分。
                    # 标签用 ASCII（aggr 而非"聚合"）—— 中文在终端占两列，会让定宽对齐失效。
                    batch_window = windows.next_window
                    with log_scope(f"e{epoch}b{batch_id} aggr"):
                        agg = await _consolidate(self.brain, arm_graphs, display,
                                                 epoch, batch_id, batch_window,
                                                 )
                    self.consolidation_log.append(agg)
                    # 主脑历史快照（与题内窗口聚合后那次同一目的：持续学习指标要能回看每个
                    # 时点的公共图谱）。**两处都要**：题内窗口聚合在 window.py 里快照，批末
                    # 这次在这里 —— 漏掉任何一处，那次聚合的贡献就只存在于终态里，
                    # 曲线上少一个点。文件名与 window.py 那处同构（同一套 e/b/w 编码）。
                    # 路径：写进 checkpoint{batch_window}/public.db，与题内窗口那次保持一致
                    # （不再落到 arm_graphs/ —— arm_graphs 只持有活跃私有图谱）。
                    snap = self.workdir / f"checkpoint{batch_window}" / "public.db"
                    snap.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        _, vec = self.brain.snapshot_with_vectors(snap)
                        logger.info("[e%db%d] 主脑快照 → %s%s", epoch, batch_id, snap.name,
                                    "" if vec else "（⚠️ 无向量库，事后无法检索）")
                    except Exception:  # pylint: disable=broad-except
                        logger.exception("[e%db%d] 主脑快照失败（聚合已成功，训练继续）",
                                         epoch, batch_id)
                else:
                    logger.info(
                        "[e%db%d] 批末聚合跳过：各副脑经验层已全空（题内聚合的 %d 个窗口已把它们"
                        "取走）—— 跑一次只会是空转", epoch, batch_id, windows.n_windows_fired)

                for g in arm_graphs.values():
                    g.close()
                # ★★ **副脑图谱与窗口快照一律保留，不再删除**（2026-08-10 用户定案）★★
                #
                # 此前这里会 unlink 掉 `arm_*.db` / `_vec.db` / 窗口快照 `_w*`，理由是
                # 「漏删几十 KB 的快照会在长 run 里堆成几 GB」。但那个权衡是错的：
                #   · 副脑私有图谱是**这条轨迹唯一的完整记忆**（每天的经历、营地坐标、
                #     它自己归纳出的经验 —— 主脑只拿到经验层被归并后的版本）。删了就再也
                #     没有「聚合前后对比」「某个副脑到底记了什么」这两类复盘的可能；
                #   · 词表反推（experiments/vocab_report.py）**主要就靠副脑侧** ——
                #     反思 Agent 的自发命名只存在于这些库里；
                #   · 体积根本不成问题：实测一条 26 天 3 副脑的轨迹，全部 concept 库合计
                #     ~540KB（vec 库大些，各 4MB 量级，仍是几十 MB 级别）。
                #
                # 于是「省磁盘」换掉的是「实验数据可复盘」，这笔交易不划算。要清理旧 run
                # 就整目录删（**注意别删正在跑的那个** —— 已删 inode 仍被进程持有时，
                # SQLite 写不了新 -wal 会降级成 `attempt to write a readonly database`，
                # 表现为反思静默失败；2026-08-10 实测踩过一次，靠 /proc/<pid>/fd 抢救回来）。
                logger.info(
                    "[e%db%d] 副脑图谱与窗口快照**保留存档**于 %s（不再删除：它们是这条轨迹"
                    "唯一的完整私有记忆，也是词表反推的主要数据源）", epoch, batch_id, ephem_dir)

            # epoch 边界不做衰减：facet 强度只挂在 concept 上、只增不减、且只由任务域 Agent 在
            # 检索时触发（见 schema.py 的强度机制说明）。遗忘由「相对排名」表达：被用到的升上去、
            # 没用到的原地不动，不需要在 epoch 边界主动衰减谁。
            if train_curve_mode:
                # 终身学习域：本轮曲线点直接由训练轨迹算出，不再跑一次 eval。
                # 这里是基线分**第一个真正被用到**的地方 —— await 后台任务（通常早已跑完）。
                # 逐日指标不再在 brain 层展开成 daily_metrics（2026-09-12 删除）：discoveries
                # 逐夜已随 night_reflections 落在各 arm checkpoint（metrics_snapshot），主 JSON
                # 的扁平时序是纯冗余副本，且只在 epoch 边界才落盘——异常终止时反而丢。
                # 消费端（quick_metrics/画图脚本）一律读 checkpoint 的 night_reflections。
                self.train_curve.append(
                    self._train_curve_point(epoch, epoch_records, await _baseline()))
            elif eval_tasks:
                point = await eval_readonly(self.domain, self.brain, self._frozen, eval_tasks,
                                            self.models.task, epoch=epoch,
                                            frozen_cache=await _baseline())
                point.pop("_frozen_score_obj", None)
                self.eval_curve.append(point)

            if on_epoch_end is not None:
                try:
                    on_epoch_end(epoch)
                except Exception:  # pylint: disable=broad-except
                    logger.exception(
                        "[e%d] on_epoch_end 回调失败（多半是增量 checkpoint 落盘出错）——"
                        "训练继续，但本轮曲线点可能没有及时写盘", epoch)

        # 没有任何 epoch 消费过基线时（epochs=0，或 train_curve/eval 两条分支都没进）后台任务
        # 仍悬着。不收尾会在事件循环关闭时报 "Task was destroyed but it is pending"，更糟的是
        # MC 域那条轨迹已借走一台 worker，domain.shutdown() 会把它连人带 server 一起停掉。
        if baseline_task is not None and not baseline_task.done():
            logger.info("[基线frozen] 无 epoch 消费基线分，取消后台采样")
            baseline_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await baseline_task

        # ★ 关闭所有常驻 SkillStore（刷盘 + 关连接）。
        for store in arm_skill_stores.values():
            try:
                store.close()
            except Exception:  # pylint: disable=broad-except
                pass

    def _train_curve_point(self, epoch: int, records: list[dict],
                           frozen_baseline=None) -> dict:
        """把本轮所有 arm 的训练采样折成一个曲线点：Y = **各 arm 得分均值**。

        取分口径：对每条 record 的 sample_metrics 逐样本调 domain.score，先在 arm 内求均值
        （一个 arm 本轮可能跑多 task/多采样），再对 arms 求均值 —— 这样**每个 arm 权重相等**，
        不会因某个 arm 恰好多跑了几条而被它主导。
        per_arm 一并留在点里，便于看清「是所有 arm 一起涨，还是只有某一个在涨」（后者往往说明
        涨的是运气/某人设特性，不是共享记忆在起作用）。
        """
        per_arm: dict[str, list[float]] = {}
        #: 逐 arm 的 Sample 列表：给 domain.aggregate_eval 用（见下方 `detail`）。
        #: 与 per_arm 分开攒是因为前者只要一个 float，而聚合要看全套 metrics。
        per_arm_samples: dict[str, list[Sample]] = {}
        for r in records:
            for m in r.get("sample_metrics") or []:
                try:
                    s = self.domain.score(Sample(text="", correct=None, metrics=m))
                except Exception:  # pylint: disable=broad-except
                    continue
                per_arm.setdefault(r.get("arm", "?"), []).append(float(s))
                per_arm_samples.setdefault(r.get("arm", "?"), []).append(
                    Sample(text="", correct=None, metrics=m))
        arm_means = {k: (sum(v) / len(v)) for k, v in per_arm.items() if v}
        score = (sum(arm_means.values()) / len(arm_means)) if arm_means else 0.0
        point = {
            "epoch": epoch,
            "score": score,                       # Y 轴：各 arm 均分的均值
            "per_arm": arm_means,                 # 分 arm 看，判断是否只有个别 arm 在涨
            "n_arms": len(arm_means),
            "n_samples": sum(len(v) for v in per_arm.values()),
        }
        # 空记忆水平基线（训练前实测一次）：训练曲线要能与它对比才叫"证据"
        if frozen_baseline is not None:
            point["frozen_baseline"] = frozen_baseline.score
            point["gap_vs_frozen"] = score - frozen_baseline.score
            # 基线的**分列**指标同样要留：主表的每一列都需要一个 frozen 对照值
            # （"我们 X 步解锁 stone" 只有和 "空记忆 Y 步" 并列才是证据）。此前只留了合成分。
            point["frozen_detail"] = frozen_baseline.detail
        # ★ **分列指标必须落进曲线点**（2026-08-17 补）：`score` 是个合成标量
        # （minecraft = unique_items + W·tier_rank − P·deaths），而论文主表要报的是**分列**的量
        # —— tier 各级达成率/首达步序、unique items、map coverage、代价分母。它们全都已经在
        # `sample_metrics` 里，但此前这个函数只调 `domain.score()` 取一个 float，
        # 于是**算完即弃、一个都没进 results JSON**（那份 JSON 里只有 score/per_arm）。
        # 走 `domain.aggregate_eval` 而不是在这里手写取平均：口径必须与 eval 分支**同一把尺**
        # ——两份实现必然漂移，而漂移的后果是"训练曲线和 eval 曲线的同名指标对不上"。
        # 失败不影响曲线点（合成分已经算好了），只是少一份分列留痕。
        try:
            point["detail"] = self.domain.aggregate_eval(
                [s for v in per_arm_samples.values() for s in v]).detail
            point["per_arm_detail"] = {
                k: self.domain.aggregate_eval(v).detail for k, v in per_arm_samples.items()
            }
        except Exception:  # pylint: disable=broad-except
            logger.exception(
                "训练曲线点的分列指标聚合失败 —— 曲线点仍有合成分，但 tier/coverage 等分列量"
                "这一轮不会落盘（论文主表要用它们，别忽略这条）")
        logger.info(
            "[训练曲线 e%d] 均分=%.1f（%d arms / %d 采样）%s | per_arm=%s",
            epoch, score, len(arm_means), point["n_samples"],
            f" vs frozen基线 {frozen_baseline.score:.1f} (gap={point['gap_vs_frozen']:+.1f})"
            if frozen_baseline is not None else "",
            {k: round(v, 1) for k, v in arm_means.items()},
        )
        return point

    def close(self) -> None:
        self.brain.close()
        if self._frozen is not None:
            self._frozen.close()
