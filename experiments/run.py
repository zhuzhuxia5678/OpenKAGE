"""Run one OpenKAGE experiment (or the planner-only control).

    python experiments/run.py --domain minecraft --smoke
    python experiments/run.py --domain minecraft --group control

The training loop (epoch -> batch -> arm -> consolidate -> decay -> eval) is in
openkage.OpenKAGE; this entry point assembles the TaskDomain, loads the agent
personas from agents/<domain>/, and writes one run directory under results/.
Domain-specific prompts and scoring live in domains/<domain>/.

Each new run writes results/<domain>_<ts>_<host>/ with a result JSON, graph
databases and daily checkpoints; logs go to logs/. The released results use the
separate results/<cell>/runNN layout described in README.md. See README.md and
configs/openkage_default.yml for setup and the run configuration.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shlex
import signal
import socket
import sys
from datetime import datetime
from pathlib import Path

_EXPERIMENTS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _EXPERIMENTS_DIR.parent
_PKG_ROOT = _EXPERIMENTS_DIR.parent  # OpenKAGE —— openkage 包根 + configs/data/results 所在
# results 落盘根可配：环境变量 OPENKAGE_RESULTS_DIR 覆盖，不设则退回仓内 results/。
_RESULTS_ROOT = (Path(os.environ["OPENKAGE_RESULTS_DIR"]).expanduser()
                 if os.environ.get("OPENKAGE_RESULTS_DIR") else _PKG_ROOT / "results")
# 本入口自包含：.env 就地加载，不依赖任何宿主项目的 backend 包。
for _p in (str(_PKG_ROOT), str(_EXPERIMENTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# ── .env: repository-root .env (see .env.example); real environment variables win ──
try:
    from dotenv import load_dotenv  # noqa: E402

    if (_PKG_ROOT / ".env").exists():
        load_dotenv(_PKG_ROOT / ".env", override=False)
except ImportError:  # python-dotenv is optional
    pass

from openkage import Arm, OpenKAGE  # noqa: E402  框架核心
from openkage.config import (  # noqa: E402
    MODEL_ROLES,
    REASONING_EFFORTS,
    LLMConfig,
    ModelConfig,
    load_config,
    resolve_n,
)
from openkage.logctx import ScopeFormatter  # noqa: E402  行首作用域列（frozen/evolving/各副脑）
from domains import build_domain  # noqa: E402  任务域插件包

logger = logging.getLogger("run_openkage")

# 每次训练日志开头的门面：终端和 logs/*.log 里都能一眼认出这是哪个项目的 run。
BANNER = r"""
  ___   ____   _____  _   _  _  __     _     ____   _____
 / _ \ |  _ \ | ____|| \ | || |/ /    / \   / ___| | ____|
| | | || |_) ||  _|  |  \| || ' /   / _ \ | |  _  |  _|
| |_| ||  __/ | |___ | |\  || . \  / ___ \| |_| | | |___
 \___/ |_|    |_____||_| \_||_|\_\ /_/   \_\\____| |_____|
"""


def _launch_command() -> str:
    """还原本次启动的命令行，形如 `python experiments/run.py --domain minecraft --smoke`。

    用 sys.executable 而非裸 "python"（虚拟环境/多版本下 "python" 未必是同一个解释器）；
    每段都 shlex.quote，含空格或引号的参数（如 --mc-workers-dir、带空格的路径）复制回终端仍等价。
    注意：外层的 nohup / 重定向 / 环境变量前缀不在 argv 里，还原不出来，只能记到这一层。
    """
    return " ".join(shlex.quote(a) for a in [sys.executable, *sys.argv])


def setup_logging(script_name: str, log_dir: Path | None = None) -> Path:
    """配置 root logger：StreamHandler + FileHandler 双写到 logs/（终端 + 文件都能事后翻）。
    log_dir 不传用默认 logs/；返回本次日志文件路径。日志配置是 runner/CLI 关切，不放进可复用框架。

    用 ScopeFormatter 而非普通 Formatter：它在每行**行首**插一个定宽的 `[作用域]` 列（如
    `[base frozen]` / `[e1b1 p2 #3]`），归属由 openkage.logctx 的 contextvar 提供。挂在
    **handler** 上，所以 httpx / openai 这些第三方 logger 的输出也一并带上标签 —— 它们的代码
    我们改不了，但它们跑在我们的 context 里。
    """
    logs_dir = log_dir if log_dir is not None else _PKG_ROOT / "results" / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    # ★ **文件名带 hostname 防撞名**（2026-09-04）：三机并行跑时 logs/ 共享，秒级 ts 可能撞
    #   （三机同时启动到同秒），同名校验会导致 FileHandler 互相覆盖、日志串流。加 hostname
    #   短名后缀（host-N/host-N/本机）每机独立文件。resume 时日志文件名也会带新 hostname
    #   （旧日志保留不删，新续跑日志另起一个带本机名的文件，互不干扰）。
    host = socket.gethostname().split(".")[0]
    log_path = logs_dir / f"{script_name}_{ts}_{host}.log"

    fmt = ScopeFormatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(fmt)
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers = [stream_handler, file_handler]
    # 门面走 logger 而非 print，才能同时落到 logs/*.log；用裸 %s 避免每行被时间戳前缀切碎。
    logging.getLogger("openkage").info("%s", BANNER)
    # 记下本次是怎么起来的：日志躺在 logs/ 里几周后，光看规模行也猜不出当时给了哪些 flag。
    # shlex.quote 保证这一行可以直接复制回终端重跑；cwd 一并记，因为 argv[0] 可能是相对路径。
    logger.info("启动命令：%s", _launch_command())
    logger.info("工作目录：%s", Path.cwd())
    logger.info("日志同时写入：%s", log_path)
    # ★ results 落盘根（2026-09-25 起各机写各机本地盘，不再写 FUSE 共享盘）：起跑确认链要核这一行，
    #   免得 run 跑了一半才发现产物落在共享盘（FUSE 抖动/整批消失已三度发生）。
    logger.info("results 落盘根：%s", _RESULTS_ROOT)
    return log_path

_RUN_TS = datetime.now().strftime("%Y%m%d_%H%M%S")
# ★ **与 logs 同构的 host 后缀**（run.py:131 的 socket.gethostname 短名同源）：results 目录名
#   带 host 防三机并行撞名覆盖 —— 秒级 ts 在三机同时启动时可能撞，共享盘上同目录会互相覆盖
#   丢掉 brain.db/arm_graphs/结果 JSON。resume 时从旧 JSON 的 run_host 恢复（见 main 的
#   --resume 分支），使续跑拼出的目录名对上旧 run_dir。旧 JSON 无此字段 → 空串，目录名退化到
#   `<domain>_<ts>`，兼容迁移前不带 host 的旧目录。
_RUN_HOST = socket.gethostname().split(".")[0]
# ★ **目录名置尾的模型/图谱标识段**（2026-09-09）：与 _RUN_TS/_RUN_HOST 同源管理 ——
#   新 run 时分别从 scale["model"]["task"] / args.no_graph 取；resume 时从旧 JSON 的
#   config.model.task / ablation 恢复（见 main 的 --resume 分支），使续跑拼出的目录名对上
#   旧 run_dir、读得到旧 brain.db/arm_graphs。空串 → _run_stem 不追加该段，退化到
#   不带该段的命名（极旧 run 无 config.model / 无 ablation 时如此）。
_RUN_MODEL_SLUG = ""   # 模型段，如 "deepseek-flash"；空串=不追加
_RUN_GRAPH_TAG = ""    # 图谱段，实验组 "graph" / 对照组 "nograph"；空串=不追加
# ★ 消融段（2026-09-15）：消融 A 无共享 "noagg" / B 无私有图 "nopriv"(拟) / C 平面检索 "flatret"(拟)。
#   与图谱段分开——消融 run 的图谱段仍是 graph（A 有私有图）/nograph（B 逻辑上无私有图），
#   消融段独立追加在最后，plot 脚本分别识别 graph|nograph 与消融段。空串=非消融 run（Full/对照）。
_RUN_ABLATION_TAG = ""
_CONFIG_DIR = _PKG_ROOT / "configs"
_DATA_DIR = _PKG_ROOT / "data"
_ARMS_DIR = _PKG_ROOT / "agents"


def _run_stem(domain: str) -> str:
    """results 子目录/JSON 的名根：`<domain>_<ts>[_<host>][_<model>][_<graph>][_<ablation>]`。

    各段空串时不追加，向旧命名退化：
      · host 空串（resume 旧 JSON 无 run_host 字段）→ 退化到不带 host；
      · model/graph/ablation 空串（极旧 run 无 config.model/ablation）→ 退化到不带该段。
    故新 run 形如 `minecraft_<ts>_<host>_<model>_graph`，消融 A 形如 `..._graph_noagg`，
    迁移前旧 run 形如 `minecraft_<ts>`，resume 各类旧 run 都能拼回与旧目录同名的路径。
    run_dir / out / context_dir / footage_dir / checkpoint_dir 同根，保证目录名与内部 JSON 同名
    （下游 report_metrics / quick_metrics 的「目录名 = JSON 名」假设由此成立）。

    ★ **段顺序固定为 ts → host → model → graph → ablation**：模型/图谱/消融段置尾，保持
      `minecraft_<ts>` 前缀，使 `sorted()[-1]` 跨不同模型/图谱取最新 run 时仍按时间戳排序。
      目录名不被任何代码反解析（split），故模型名里的 `_`/`.` 不做清洗。
    """
    parts = [domain, _RUN_TS]
    if _RUN_HOST:
        parts.append(_RUN_HOST)
    if _RUN_MODEL_SLUG:
        parts.append(_RUN_MODEL_SLUG)
    if _RUN_GRAPH_TAG:
        parts.append(_RUN_GRAPH_TAG)
    if _RUN_ABLATION_TAG:
        parts.append(_RUN_ABLATION_TAG)
    return "_".join(parts)


def _override_llm_effort(args) -> None:
    """把 `--reasoning-effort-<角色>` 覆盖进 `LLMConfig` 的进程级缓存（没给则不动）。

    为什么不像规模参数那样塞进 `scale`：thinking 档位住在 yml 的 `llm.reasoning_effort`，
    而 `llm` 那节不经 runner 透传 —— `openkage/llm.py` 是在**热路径上**自己
    `LLMConfig.load()` 取值的。所以覆盖必须落在那个 lru_cache 上。

    ★ 必须**在任何 LLM 被构造之前**调用（`get_llm` 一构造就把 timeout/effort 定死在
      实例上，见其缓存键）。手法与 `reaggregate_window._override_sdk_timeout` 同一个 ——
      lru_cache 没有公开的"预置"API，直接把内核换成返回定值的函数。
    """
    from dataclasses import replace  # noqa: PLC0415

    from openkage import config as _cfg  # noqa: PLC0415

    picked = {r: v for r in MODEL_ROLES
              if (v := getattr(args, f"reasoning_effort_{r}", None)) is not None}
    if not picked:
        return
    base = _cfg.LLMConfig.load()
    patched = replace(base, reasoning_effort={**base.reasoning_effort, **picked})
    _cfg._load_llm_cached.cache_clear()  # noqa: SLF001
    _cfg._load_llm_cached = lambda _path: patched  # type: ignore[assignment]  # noqa: SLF001
    logger.info("[thinking] 命令行覆盖 reasoning_effort：%s（最终 %s）",
                picked, patched.reasoning_effort)


def _resolve_domain_scale(cfg, args, domain: str) -> dict:
    """域私有规模的三层解析：**命令行 > smoke（若开） > yml `domain_scale.<域>`**。

    与 main() 里 train/eval 那段同一个顺序即同一个优先级 —— 这是本文件的既有纪律
    「yml 是唯一真相，命令行只做覆盖，default=None 才分得清『没给』与『给了』」。

    ⚠️ smoke 的覆盖是**逐键 merge**、不是整节替换：smoke 只该动规模，不该顺手把机制参数
    带回默认值（如把用户显式配的 aggregate_every 冲掉）。
    """
    scale = dict(cfg.domain(domain))
    smoke_ds = ((cfg.smoke.get("domain_scale") or {}).get(domain) or {}) if args.smoke else {}
    scale.update(smoke_ds)
    # 命令行最后覆盖（flag 名 → yml 键名）
    for key, flag in (("max_nights", "max_nights"), ("aggregate_every", "aggregate_every"),
                      ("tick_rate", "tick_rate"),
                      ("world_seed", "world_seed"), ("spawn_seed", "spawn_seed"),
                      ("difficulty", "difficulty")):
        v = getattr(args, flag, None)
        if v is not None:
            scale[key] = v
    return scale


def select_arms(arms_dir: Path, only_arm: str | None, n_arms: int) -> list:
    """读 arms/<domain>/*.md 装配副脑角色列表。

    - ``only_arm`` 为 None：取前 ``n_arms`` 个（原 ``sorted(glob)[:N]`` 逻辑，向后兼容）。
    - ``only_arm`` 给定：先读全部可用角色 md，再按 ``key == only_arm`` 筛出那一个
      （key = md 文件名，如 alice/bob/carol）。筛不到 → ``ValueError`` 列出可用 key。
      ★ 不能用 ``[:1]``——那只会取排第一的 alice，无法单独续跑 bob/carol；本筛选取代
      固定顺序，使 ``--only-arm bob`` 真的只跑 bob。调用方据此把 ``scale["arms"]`` 钉为 1。

    纯文件 IO + 筛选逻辑，不依赖 MC/LLM，可单测（见 tests/test_only_arm.py）。
    """
    all_arms = [Arm.from_file(m) for m in sorted(arms_dir.glob("*.md"))
                if m.read_text(encoding="utf-8").startswith("---")]
    if only_arm:
        arms = [a for a in all_arms if a.key == only_arm]
        if not arms:
            avail = ", ".join(a.key for a in all_arms) or "(无)"
            raise ValueError(
                f"--only-arm {only_arm!r} 在 {arms_dir} 下找不到匹配角色"
                f"（key=md 文件名）。可用 key：{avail}")
        logger.info("[规模] --only-arm %s：只跑该角色单臂（arms 钉为 1）", only_arm)
        return arms
    return all_arms[:n_arms]


def _build_domain(args, scale: dict, n_arms: int, domain_scale: dict):

    """按 --domain 装配 TaskDomain，域相关 CLI 参数在此收口（框架层不认识这些）。
    返回 (domain, build_cfg)：build_cfg 传给 domain.build_tasks。备份/种子交给 domain.prepare()。

    n_train / n_eval 原样透传（可能是 "all"）—— 各域 build_tasks 拿到多少题只有它自己知道，
    故 "all" 的解析统一在 main() 里、build_tasks 返回之后做（见 _apply_n_limits）。

    n_arms 是**实际**副脑数（arms/<domain>/*.md 数量与 scale["arms"] 取小，见 main），
    minecraft 用它决定默认起几台 MC worker —— 所以 main 里 arms 的读取必须在本函数之前。

    domain_scale 是**域私有**规模（已由 `_resolve_domain_scale` 做完三层解析），
    如 minecraft 的 max_nights / aggregate_every。框架层不认识它们，故在这里落地。
    """
    if args.domain == "minecraft":
        # worker 数：auto = 每个副脑一台 + **基线一台**（全并发）。显式给数字可上限截断
        # （起 N 台 JVM 各 2G，机器不一定给得起）；小于需求数时多余的副脑在 worker 池上排队，
        # 1 即退化成旧的串行行为。
        #
        # ★ 为什么 auto 要 +1（2026-07-31 用户定）：frozen 基线现在与第 1 轮训练**并发**跑
        # （见 openkage/brain.py 的 baseline_task），而它同样要从池里独占借一台 world 整轮探索。
        # 只备 n_arms 台的话，基线会抢走其中一台 → 有个副脑在 pool.acquire() 上干等一条完整
        # 轨迹（MC 约 7min/夜），并发省下的墙钟又还回去一半。n_eval=0（不跑基线）时不加。
        # n_eval 可以是 "all"（本域无固定题库 → MinecraftDomain._as_count 把它当 1），也可以是
        # 数字字符串或 int。这里只关心「是不是 0」，故宽松解析：解析不出来一律按「要跑基线」，
        # 宁可多起一台 JVM 也不要让基线去抢副脑的 worker。
        n_eval_raw = scale["n_eval"]
        if isinstance(n_eval_raw, str) and n_eval_raw.strip().lower() == "all":
            need_baseline_worker = True
        else:
            try:
                need_baseline_worker = int(str(n_eval_raw).strip()) > 0
            except ValueError:
                need_baseline_worker = True
        # ★ no_graph（对照组）不跑 frozen 基线（brain.py 跳过 baseline_task）→ 不多备那台 worker。
        #   否则白起一台 JVM（2G）永远闲着。
        # ★ 消融 A 无共享同理：主脑 NullGraph，frozen 基线无意义（brain.py 也跳过 baseline_task）。
        if args.no_graph or getattr(args, "no_aggregate", False):
            need_baseline_worker = False
        want = n_arms + (1 if need_baseline_worker else 0)
        workers = want if args.mc_workers == "auto" else max(1, int(args.mc_workers))
        if workers > want:
            logger.info("[minecraft] --mc-workers %d 超过需求数 %d（%d 副脑%s），"
                        "多出来的 worker 永远闲着 → 收到 %d", workers, want, n_arms,
                        " + 1 基线" if need_baseline_worker else "", want)
            workers = want
        if need_baseline_worker and workers <= n_arms:
            # 明确说出后果：这不是错误配置，但「基线与训练并发」的收益会被排队吃掉一部分。
            logger.info("[minecraft] worker 数 %d ≤ 副脑数 %d，而基线也要独占一台 → "
                        "会有副脑在池上排队（想全并发给 %d 台）", workers, n_arms, n_arms + 1)
        domain = build_domain(
            "minecraft",
            mc_port_base=args.mc_port_base,
            mineflayer_port_base=args.mineflayer_port_base,
            workers=workers,
            workers_dir=args.mc_workers_dir,
            reprovision=args.mc_reprovision,
            server_dir=_PKG_ROOT / "domains" / "minecraft" / "mc_server",
            max_nights=domain_scale["max_nights"],
            aggregate_every=domain_scale["aggregate_every"],
            smoke_dawn_nudge=args.smoke,  # smoke 开局设到将黎明，加速验证反思链路
            mc_dir=args.mc_dir,
            # ★ 世界重生协议种子（world_seed/spawn_seed）：与 max_nights 同属域私有参数，
            #   现住 yml 的 domain_scale.minecraft（对齐「yml 是运行期参数唯一真相」纪律）。
            #   yml 缺键时（如自定义 config 没抄这两行）→ 落到域构造函数签名默认值
            #   （"2026"/"20260703"），故用 None-条件展开（同 spawn_radius 的写法），
            #   绝不显式传 None 给 provision_workers（后者会把 level-seed=None 写进
            #   server.properties → 种子损坏）。
            #   两者都是 str：Random 走 sha512，前导零保留（spawn_seed="20260703" 不丢零）。
            **({"world_seed": domain_scale["world_seed"]}
               if domain_scale.get("world_seed") is not None else {}),
            # None → 用域里的默认半径（1000），不在这里重复硬编码那个数
            **({"spawn_radius": args.spawn_radius} if args.spawn_radius is not None else {}),
            random_spawn=not args.fixed_spawn,
            **({"spawn_seed": domain_scale["spawn_seed"]}
               if domain_scale.get("spawn_seed") is not None else {}),
            # ★ 世界难度挡位（difficulty）：与 world_seed/spawn_seed 同属域私有协议参数，
            #   现住 yml 的 domain_scale.minecraft。None-条件展开（同 spawn_seed 写法）：
            #   yml 缺键时落到域构造函数签名默认 "normal"，绝不显式传 None 给
            #   provision_workers（后者会把 None 写成字面 "difficulty=None" 损坏 properties）。
            **({"difficulty": domain_scale["difficulty"]}
               if domain_scale.get("difficulty") is not None else {}),
            # ★ **上下文留痕默认开**（2026-08-10）：此前它藏在 `--dump-step-inputs` 后面、
            #   默认关闭 —— 结果那轮 30 夜实验跑到第 7 天才发现一个字都没存下来，而这份内容
            #   是复现与诊断所需的全部信息（记忆块喂了什么、哪一步开始跑偏）。
            #   代价很小：一天一个文件、覆盖写，几十 KB 量级；而漏存的代价是整轮无法复盘。
            #   路径与 main() 里的 run_dir 同源（都由模块级 _RUN_TS/_RUN_HOST 决定），所以不必改
            #   domain/run_dir 的构造顺序。
            context_dir=_RESULTS_ROOT / _run_stem(args.domain) / "contexts",
            # 关键事件画面 + 全程录像。**默认关**，靠 --mc-footage 开：每个 worker 要常驻一个
            # MC 客户端（-Xmx4G，约 4G 内存）+ 一个 ffmpeg（约 9% CPU），会和训练抢资源 ——
            # 该不该付这个代价取决于这轮是否要出素材，故不默认开。
            # ★ 落盘在 --mc-footage-dir（默认 = /path/to/footage
            #   本机 ceph 盘，2026-09-18 迁移）：一条 17 小时轨迹录下来是 GB 量级；
            #   只有本机 48 核开得了录像（16G/31G 远端机器观战客户端会 OOM），且共享盘
            #   results/footage 受 spaceclient 抖动影响（0918 paper 三次消失同源），不再作默认。
            footage_dir=((Path(args.mc_footage_dir) / _run_stem(args.domain))
                         if getattr(args, "mc_footage", False) else None),
            # ★ 透明分段 checkpoint（2026-08-18）：每个聚合窗口边界存档 + 写进度，
            # 中断后 --resume --resume-window N 显式回退到窗口 N 续跑同一条轨迹（容灾）。
            # 与 run_dir 同源路径。
            # resume 时 _RUN_TS/_RUN_HOST 沿用旧 run 的（见 main 的 --resume 分支），故 checkpoint
            # 自然落在原 run 目录下、续跑能读到。
            checkpoint_dir=_RESULTS_ROOT / _run_stem(args.domain),
            no_graph=args.no_graph,
            # ★ 消融 A 无共享（--no-aggregate）**不传给 domain**：domain 侧 self._no_graph 的
            #   语义是「全程 NullGraph」（跳过技能计数回填/技能维护/图谱快照）。A 副脑是真
            #   ArmGraph，这些全要保留（技能限本臂但仍计数/维护、private.db checkpoint 续跑要
            #   恢复），故 A 的 no_graph=False 让 domain 天然走「真图」分支即可，无需 no_aggregate
            #   标志。A 的隔离全在 brain.py（主脑 NullGraph + 聚合 no-op + 技能限本臂视图）。
            #   no_aggregate 只透传给 OpenKAGE(BrainGraph)构造（见下方 octo = OpenKAGE(...)）。
            # ★ 上下文管理（2026-09-24 起为记忆层连带，独立 flag 已删）：不再从 CLI 透传，
            #   domain 构造函数内部按 no_graph/ablation 派生（no_graph=no_memory_layer=None →
            #   True 照跑；True/有记忆层的 rung 不传也照跑）。旧 --no-context-mgmt 已拒。
            # ★ OPENKAGE 倍速：domain_scale.minecraft.tick_rate（默认 1），可被 --tick-rate 覆盖。
            # >1 时 server /tick rate + 客户端 setTickRate 两侧同步，昼夜/动作按 N 倍速压缩。
            # 浮点 N（如 2.5）合法：Node 侧 Number()/20*rate 全程支持，server 收到 /tick rate 50（=50TPS）。
            tick_rate=float(domain_scale.get("tick_rate") or 1),
            # ★ 任务域 ReActAgent 每天的最大步数（防死循环兜底，见 yml domain_scale.minecraft.react_max_steps 注释）。
            #   与 tick_rate 同款：yml 写了就覆盖，缺键走域构造函数默认（999）。不给 CLI flag ——
            #   它是兜底不是调参维度，暴露 flag 会诱导误调小值把 Agent 在天亮前掐死。
            **({"react_max_steps": int(domain_scale["react_max_steps"])}
               if domain_scale.get("react_max_steps") is not None else {}),
        )
        build_cfg = {"n_train": scale["n_train"], "n_eval": scale["n_eval"]}
        return domain, build_cfg

    raise ValueError(f"未知 --domain {args.domain!r}")


def _apply_n_limits(train_tasks, eval_tasks, scale: dict):
    """把 n_train / n_eval（含 "all"）落到实际题目列表上，返回截断后的 (train, eval)。

    为什么在 build_tasks **之后**统一做，而不是只靠各域自己截：
    - 各域对 cfg["n_train"] 的处理并不一致 —— minecraft 会用它切片，而 **math 完全忽略
      它、直接返回整个题库**（历史行为）。统一语义就得有一处统一兜底。
    - "all" 需要知道题库总量，那只有 build_tasks 返回后才知道。
    此处按 resolve_n 解析（"all" → 全部，整数 → clamp 到题库大小），再切片；已经被域截过的
    列表再截一次是幂等的。
    """
    n_tr = resolve_n(scale["n_train"], len(train_tasks), what="n_train")
    n_ev = resolve_n(scale["n_eval"], len(eval_tasks), what="n_eval")
    if n_tr != len(train_tasks) or n_ev != len(eval_tasks):
        logger.info("[规模] 题数落定：train %d→%d，eval %d→%d",
                    len(train_tasks), n_tr, len(eval_tasks), n_ev)
    return train_tasks[:n_tr], eval_tasks[:n_ev]


def main() -> None:
    # 规模参数的 default 一律 None ——「未显式给出」才好与「显式给了」区分开，从而做到
    # **yml 是唯一真相、命令行只做覆盖**（2026-07-28 统一）。默认值全在
    # configs/openkage_default.yml 的 train/eval 节，不在这里硬编码。
    p = argparse.ArgumentParser(
        description="OpenKAGE训练统一入口（--domain 选任务域；规模默认值见 configs/openkage_default.yml）")
    p.add_argument("--domain", required=True, choices=["minecraft"])
    p.add_argument("--config", type=str, default=str(_CONFIG_DIR / "openkage_default.yml"))
    p.add_argument("--resume", type=str, default=None,
                   help="断点续训：指向之前一次运行落盘的 results/<domain>_<ts>/<domain>_<ts>.json。"
                        "复用该文件同名的 run_dir（brain.db/arm_graphs 原地续写，"
                        "不新起一份），epoch 编号从「旧文件里 train_curve/eval_curve 的"
                        "最大 epoch + 1」开始接续（不是从 1 重数）。--epochs 仍表示"
                        "**本次**要跑几轮，不是目标总轮数。仅按 epoch 边界存档：若上次"
                        "进程是在某一轮进行中被杀，那半轮的曲线点不会出现，但已写进"
                        "图谱的记忆不会丢——下次会从上一个**完整**epoch 之后重新跑。"
                        "★ 必须配 --resume-window N：续跑一律走 rollback 显式回退到窗口 N "
                        "（备份并删 N+1..末 + 扁平JSON 回退到 N 边界夜），保证活数据"
                        "（扁平 checkpoint 的 last_reflection/last_goal/metrics_snapshot、"
                        "arm_graphs、skills、brain.db）全部从 checkpoint{N}/ 同源覆盖，"
                        "不被活数据中未跑完的半截数据污染。不带 --resume-window 的普通续跑"
                        "已移除（原自动取最大窗口路径不回退扁平 JSON 的 reflection/goal，"
                        "会读入未跑完的撕裂态活数据）。")
    p.add_argument("--resume-window", type=int, default=None, metavar="N",
                   help="从指定 checkpointN 恢复（★ 必须与 --resume 一起用，2026-09-11 起强制）："
                        "备份并删 N+1..末 窗口 + 回退各臂扁平JSON到N边界夜，让 latest_window_with_world "
                        "取到 N、三者（private/skill/public）从 checkpoint{N}/ 同源恢复。"
                        "N 必须带 world 存档（通常 N>=1）。")
    # ---- 训练侧（覆盖 yml 的 train.*）----
    p.add_argument("--arms", type=int, default=None, help="副脑个数（默认取 train.arms）")
    # ★ --only-arm <name>（2026-09-15）：只跑指定角色一个臂，覆盖 --arms。续跑单臂用——
    #   arm 装配是固定顺序 sorted(glob)[:N]（见下方），--arms 1 只会取 alice（永远排第一），
    #   无法单独续跑 bob/carol。本 flag 从读出的 arms 里筛 key==name 的那个、强制 arms=1，
    #   使 rollback 的 arm_keys 列表只含单角色 → 只动该臂 checkpoint、不碰同目录别的臂
    #   （checkpoint 落盘本就按 arm_key 隔离，见 checkpoint.checkpoint_path）。不给则行为不变。
    p.add_argument("--only-arm", type=str, default=None, metavar="NAME",
                   help="只跑指定角色（key=文件名，如 alice/bob/carol），覆盖 --arms 强制单臂。"
                        "续跑单臂用：从读出的 arms 里筛出该角色，rollback 只动该臂 checkpoint。")
    p.add_argument("--epochs", type=int, default=None, help="训练轮数（默认取 train.epochs）")
    p.add_argument("--n-train", type=str, default=None,
                   help='训练题数，"all"=全部（默认取 train.n_train）')
    p.add_argument("--batches", type=int, default=None,
                   help="分成几个 batch = 聚合几次（默认取 train.batches）")
    p.add_argument("--n-samples", type=int, default=None,
                   help="一个题目 rollout 几次（默认取 train.n_samples）")
    # ★ **按角色分的三个 flag**（2026-08-15，取代原先单个 `--model`）：yml 的 train.model 现在
    #   是 `{task, reflect, consolidate}` 三个键，一个 `--model xxx` 无法表达该覆盖哪个角色。
    #   三个 flag 各自独立覆盖，没给的沿用 yml —— 于是「只把任务域换成便宜模型试一把」
    #   这种最常见的操作不用动 yml。`--model` 本身**保留但直接报错**（见 main 里的迁移检查）：
    #   静默忽略一个曾经生效的 flag 会让人以为跑的是自己指定的模型。
    p.add_argument("--model-task", type=str, default=None,
                   help="任务域 rollout 的模型（默认取 train.model.task，yml 当前 DeepSeek-V4-Flash-0731）")
    p.add_argument("--model-reflect", type=str, default=None,
                   help="反思 Agent 的模型（默认取 train.model.reflect，yml 当前 DeepSeek-V4-Flash-0731）")
    p.add_argument("--model-consolidate", type=str, default=None,
                   help="聚合 Agent 的模型（默认取 train.model.consolidate，yml 当前 DeepSeek-V4-Flash-0731）")
    p.add_argument("--model", type=str, default=None,
                   help="[已移除] 请改用 --model-task / --model-reflect / --model-consolidate")
    # ★ thinking 档位也按角色分（2026-08-16）：与 --model-* 同构、同理由。
    #   它覆盖的是 yml 的 `llm.reasoning_effort.<角色>`（**不在 train 节**，所以不走
    #   上面那张规模覆盖表，而是经 _override_llm_effort 塞进 LLMConfig 的进程级缓存）。
    for _role in MODEL_ROLES:
        p.add_argument(f"--reasoning-effort-{_role}", type=str, default=None,
                       choices=list(REASONING_EFFORTS),
                       help=f"{_role} 角色的 thinking 档位"
                            f"（默认取 llm.reasoning_effort.{_role}）")
    # ---- 评测侧（覆盖 yml 的 eval.*）----
    p.add_argument("--n-eval", type=str, default=None,
                   help='评估题数，"all"=全部（默认取 eval.n_eval）')
    p.add_argument("--eval-k", type=int, default=None,
                   help="一次评估里每题重复跑几次，取均值压噪（默认取 eval.k）")
    # ---- Minecraft 专属 ----
    # ★ 并发架构（2026-07-31）：**每个副脑一台独立 MC server + 一个独立 mineflayer**（worker 池，
    # 见 domains/minecraft/worker_pool.py）。端口按 worker 序号递增：w0 用 base+0、w1 用 base+1…
    # 端口段刻意错开手工调试用的那套（MC 25567 / mineflayer 3000，见 mc_server/start_servers.sh），
    # 所以正式跑与手工调试可以并存、互不抢端口。
    p.add_argument("--mc-workers", type=str, default="auto",
                   help="起几台独立 MC server（= 能并行跑几个副脑）。auto（默认）= 副脑数，"
                        "即全并发；给数字可上限截断（每台 JVM 约 2G）；1 = 退回串行")
    p.add_argument("--mc-port-base", type=int, default=25600,
                   help="worker 的 MC 端口起点（w<i> 用 base+i）。默认 25600，与手工起的 25567 错开")
    p.add_argument("--mineflayer-port-base", type=int, default=3100,
                   help="worker 的 mineflayer 端口起点（w<i> 用 base+i）。默认 3100，"
                        "与手工调试用的 3000 错开")
    p.add_argument("--mc-workers-dir", type=str, default="/path/to/mc_probe/openkage_workers",
                   help="worker 目录的父目录；每个 worker 一份 w<i>/（从 --mc-dir 复制，约 138M/份）")
    p.add_argument("--mc-reprovision", action="store_true",
                   help="删掉已有 worker 目录重新复制（模板改了 mods/properties 之后用）；"
                        "平时复用已有目录，只重写端口/seed")
    p.add_argument("--mc-footage", action="store_true",
                   help="录关键事件的游戏画面（死亡/tier 突破/成就各抓一帧）+ 全程按天分段录像。"
                        "落盘结构：<mc-footage-dir>/<domain>_<run_ts>_<host>/<arm>/<slot>/day01.mp4 + steps.jsonl。"
                        "每个 worker 常驻一个观战 MC 客户端：约 4G 内存（-Xmx4G）+ 软件渲染持续占 CPU，"
                        "会和训练抢资源，故默认关。需要 Xvfb/xdotool/xclip/ImageMagick 与 /path/to/mc_probe/mc_client")
    p.add_argument("--mc-footage-dir", type=str,
                   default="/path/to/footage",
                   help="录像/截图的落盘根目录（每个 run 一个 <domain>_<run_ts>_<host> 子目录）。"
                        "默认 = /path/to/footage（本机挂载的 ceph 盘）——"
                        "只有本机 48 核开得了录像（远端 16G/31G 观战客户端会 OOM），"
                        "而共享盘 results/footage 受 spaceclient 抖动影响（0918 paper 三次消失同源风险），"
                        "故默认落到这块不会掉东西的 ceph 盘。一条 17 小时轨迹的录像是 GB 量级")
    # ---- 域私有规模（覆盖 yml 的 domain_scale.<域>.*）；default=None 才分得清「没给」与「给了」----
    p.add_argument("--max-nights", type=int, default=None,
                   help="一条轨迹过几个夜晚（**轨迹长度的唯一闸门**，步数不设上限）。"
                        "默认取 domain_scale.minecraft.max_nights。"
                        "实测每夜约 15-20 步、每夜墙钟约 20min（第 1 夜约 7min）")
    p.add_argument("--aggregate-every", type=int, default=None,
                   help="**题内聚合周期**：每过 K 夜做一次「副脑经验层→主脑」聚合，聚合后只清"
                        "经验层、**保留记忆层**（营地坐标等跨天信息不丢）。0=关闭（只在轨迹末尾"
                        "聚合一次)。默认取 domain_scale.minecraft.aggregate_every。"
                        "它同时是副脑经验层的规模上限（K 夜的产出）—— 跑长轨迹（如 365 夜）"
                        "必须开，否则经验层要独自涨满整条轨迹")
    # ★ OPENKAGE 倍速（2026-08-21）：游戏倍速 N。server /tick rate ${20*N}（= 20N TPS = N 倍速，
    #   ⚠️ /tick rate 的值是 TPS 不是倍数，发裸 N 会减速）+ 客户端 setTickRate(N)（本地物理 50/N ms）
    #   两侧同步，昼夜流速/夜间干等/动作节拍按 N 压缩（5天预算→约 5/N 天的时钟部分；LLM 往返与 504
    #   不受影响）。默认 1=自然流速。
    p.add_argument("--tick-rate", type=float, default=None,
                   help="MC 游戏倍速 N（1=自然流速，一夜≈20min）。>1 时昼夜/动作按 N 倍速压缩，"
                        "压长轨迹墙钟。默认取 domain_scale.minecraft.tick_rate（yml 默认 2.5）。"
                        "支持小数（如 2.5 → server /tick rate 50 TPS）。"
                        "⚠️ 加速会等比放大怪物速度，死亡率会上升；建议 N=2 起步")
    p.add_argument("--difficulty", type=str, default=None,
                   help="世界难度挡位 peaceful/easy/normal/hard（MC 原生取值）。"
                        "每 worker 重生时强制写入 server.properties（不依赖模板手调值）。"
                        "默认取 domain_scale.minecraft.difficulty（yml 默认 hard，0910 起）。"
                        "⚠️ 0908 及此前对照实验在 easy 下跑，改挡位后不可与其直接横比")

    p.add_argument("--mc-dir", type=str, default="/path/to/mc_probe/server121",
                   help="**模板** MC server 目录（含 server.properties + fabric-server-launch.jar"
                        " + ops.json）。每个 worker 从它整目录复制一份（跳过存档/日志），"
                        "在自己那份里删存档按固定 seed 重生世界。模板本身全程只读")
    # 旧 flag：架构变了（单 server → worker 池），它们的语义已不存在。**显式报错而不是静默忽略** ——
    # 静默忽略会让人以为「我指定了 25567 所以跑的是那台」，而实际跑的是 25600+，端口对不上时
    # 排查方向全错。
    p.add_argument("--mineflayer-url", type=str, default=None, help=argparse.SUPPRESS)
    p.add_argument("--mc-port", type=int, default=None, help=argparse.SUPPRESS)
    p.add_argument("--world-seed", type=str, default=None,
                   help="世界 seed（**固定不变**）：地形逐块恒定，别人拿同 seed 即可复现同一片世界。"
                        "默认取 domain_scale.minecraft.world_seed（yml 默认 \"7\"）")
    p.add_argument("--spawn-radius", type=int, default=None,
                   help="随机出生点半径（格），默认 1000。出生前会先预热目标点周围一小圈再传送"
                        "（约 4s，与半径无关），所以半径放大不额外花钱；半径越大落点生物群系越多样")
    p.add_argument("--fixed-spawn", action=argparse.BooleanOptionalAction, default=True,
                   help="出生点是否固定到世界自然出生点（**默认 True**）：所有轨迹同一出生地，"
                        "横向（不同 agent）纵向（不同夜）都能对比。--no-fixed-spawn 退回在 "
                        "--spawn-radius 内随机取出生点（可复现，由 --spawn-seed 决定），"
                        "用于需要开局地形多样性的实验")
    p.add_argument("--spawn-seed", type=str, default=None,
                   help="出生点 RNG 种子：同值复跑得到同一串出生点，实验完全可复现。"
                        "默认取 domain_scale.minecraft.spawn_seed（yml 默认 \"20260703\"）。"
                        "⚠️ 用 str 类型：前导零会保留（Random 内部走 sha512，不求数值）。")
    p.add_argument("--smoke", action="store_true", help="极小规模验证链路")
    # **对照组 ablation**：开启后本次 run 整个作为
    #   **无图谱对照组** —— 主脑/副脑图全用 NullGraph、聚合 no-op、不跑 frozen 基线。唯一变量是
    #   图谱，其余（arms/max_nights/aggregate_every/角色）与实验组一致，故两组 train_curve 的
    #   per_arm 可逐角色 join。实验组是另起一次不带本 flag 的 run。续跑完全复用（checkpoint 以
    #   arm_key+round 为键、与图谱无关）。math 域亦可开（NullGraph 作主脑即空先验基线）。
    p.add_argument("--no-graph", action="store_true",
                   help="对照组 ablation：本次 run 整个作为无图谱对照组（主脑/副脑图全 NullGraph、"
                        "聚合 no-op、不跑 frozen 基线）。实验组另起一次普通 run，两份 JSON 事后"
                        "逐角色 join。")
    # ★ 上下文管理独立 flag 已删（2026-09-24，用户定：昨日目标/复盘是记忆层的连带机制）：
    #   domain 层 context_mgmt 由 no_graph/ablation 派生（无记忆层=−M/no_graph → 整套消失，
    #   其余照跑），CLI 不再有独立开关。旧命令行传 --no-context-mgmt 会被 argparse 拒——
    #   那正是想要的（0913-0923 的独立开关口径已并入记忆层语义）。横比口径以 JSON 的
    #   ablation 字段为准（0924 前 run 另看 context_mgmt 字段的历史值）。
    # ★ 消融 A 无共享（2026-09-15，见 paper/iclr2027_experiments.md §3.1）：私有双层图照常
    #   （反思写入、day_start 记忆层注入、私有图检索），但两条共享通道全关——永不聚合（主脑
    #   NullGraph、共享图恒空、检索只见自己的图）+ 技能只读本臂（跨臂 SkillRegistry 关）。等价
    #   "5 个互不相识的 Voyager 式个体：各自技能库 + 各自记忆，零共享"。隔离"共享"的价值（贡献 2）。
    #   ⚠ 与 --aggregate-every 0 不同（后者语义=轨迹末尾聚合一次，configs 注释明写）；必须用本 flag。
    #   ⚠ 与 --no-graph 互斥（no_graph 是更彻底的全程空，A 是真副脑+空主脑的混合装配）。
    p.add_argument("--no-aggregate", action="store_true",
                   help="消融 A 无共享：聚合+跨臂技能两条通道全关，私有图照常。与 --no-graph 互斥。"
                        "⚠ 与 --aggregate-every 0 不同（后者=末尾聚合一次）。")
    # ★ 跨 run 图谱迁移种子（2026-09-20，论文 §3.4「知识是可插拔资产」）：给一个**已完赛 run 的
    #   results 子目录**，本次 run 从它炼出的知识资产起步——公共图谱 brain.db、每臂技能库
    #   skills/skill_{arm}.db、私有图谱 arm_graphs/arm_{arm}_e1b1.db 三处文件级 copy。
    #   私有图谱随后**清掉记忆层**（专名坐标/日程跨组是错配；实测源 run 结束时私有图只剩记忆层，
    #   清完即空 → 新组私有图从空起步），实际迁过去的载荷 = 公共经验图谱 + 技能库。
    #   ⚠ 与 --no-graph 互斥（对照组全程 NullGraph，图没处装）；与 --resume 同传时 checkpoint 优先。
    p.add_argument("--seed-graph", type=str, default=None, metavar="RUN_DIR",
                   help="图谱迁移种子：已完赛 run 的 results 子目录（如 results/minecraft_..._graph）。"
                        "本次 run 从它的公共图谱 + 各臂技能库起步（私有图记忆层已清）。"
                        "与 --no-graph 互斥。")
    # ⚠️ `--dump-step-inputs` 已删除（2026-08-10）：上下文留痕改成**默认开**，
    #    没有"要不要存"这个选择了（理由见 _build_domain 里 context_dir 那处）。
    #    **不保留成 no-op flag**：一个还能传、但什么也不改的开关，比报错难查得多。
    #    旧命令行传它会被 argparse 当未知参数直接拒 —— 那正是想要的（提醒它已经没有了）。
    args = p.parse_args()

    # 旧 flag 直接拒绝（架构从「单 server」变成「worker 池」，它们指的东西已不存在）。
    # 报错而不是忽略：默认端口段也变了（MC 25567→25600、mineflayer 3000→3100），静默吞掉
    # `--mc-port 25567` 会让人以为跑的是那台手工起的服，端口对不上时排查方向全错。
    for old, new in (("mineflayer_url", "--mineflayer-port-base"),
                     ("mc_port", "--mc-port-base")):
        if getattr(args, old, None) is not None:
            p.error(
                f"--{old.replace('_', '-')} 已移除：现在每个副脑一台独立 MC server + 一个独立 "
                f"mineflayer（端口按 worker 序号递增），单个地址/端口不再有意义。请改用 {new}"
                f"（配合 --mc-workers）。"
            )

    # ★ 消融 flag 互斥校验（2026-09-15）：--no-aggregate（A 无共享：真副脑+空主脑）与
    #   --no-graph（全程 NullGraph）不可同开——no_graph 是更彻底的空，叠加 A 无额外信息且
    #   语义冲突（no_graph 副脑也空，A 要副脑真图）。报错而非忽略，防静默跑成 no_graph。
    if getattr(args, "no_aggregate", False) and getattr(args, "no_graph", False):
        p.error(
            "--no-aggregate 与 --no-graph 互斥：no_graph 是全程 NullGraph（更彻底的空），"
            "A 无共享要副脑真图+主脑空图的混合装配，两者不可同开。消融 A 用 --no-aggregate，"
            "对照组用 --no-graph。"
        )
    # ★ 迁移种子校验（2026-09-20 §3.4）：起跑前就把「种子目录不存在 / 与对照组同开」挡掉 ——
    #   跑 10 小时才发现种子没铺上是最贵的失败模式（日志里退化成"从空图起步"，事后极难分辨）。
    if getattr(args, "seed_graph", None):
        _sg = Path(args.seed_graph)
        if not _sg.is_dir():
            p.error(f"--seed-graph 不是目录（要给已完赛 run 的 results 子目录）：{_sg}")
        if not (_sg / "brain.db").is_file():
            p.error(f"--seed-graph 目录里没有 brain.db（不是一条带图谱的完赛 run）：{_sg}")
        if getattr(args, "no_graph", False):
            p.error("--seed-graph 与 --no-graph 互斥：对照组全程 NullGraph，种子图谱无处装载。")
        if not (_sg / "skills").is_dir():
            logger.warning("--seed-graph：%s 里没有 skills/ 子目录 —— 技能库将从空起步"
                           "（迁移载荷只剩公共图谱）", _sg)
        if args.resume:
            # 不报错：checkpoint 恢复优先于种子（同窗口同源），同传是合法的「种子 run 续跑」场景。
            logger.warning("--seed-graph 与 --resume 同传：checkpoint 恢复优先，"
                           "本次的种子文件只在无 checkpoint 可恢复时才生效。")

    setup_logging("run_openkage")

    # ★ **SIGTERM 转 KeyboardInterrupt**（2026-08-19，修续跑缺陷③）：
    # Python 默认 SIGTERM 直接终止进程、**不跑 finally**——上一次 `kill -TERM` 停 run 时
    # results json 没落盘（--resume 报"文件不存在"，要手工从日志重建）。SIGINT 走 KeyboardInterrupt
    # 路径会跑 finally 写 json（8/19 233207 run 证实）。这里把 SIGTERM 也接到同一条路径，
    # 让 `kill -TERM` 与 Ctrl-C 行为一致：finally 写 json → 续跑有据。
    # 不能在 handler 里直接写 json：octo/domain 实例在 main 局部，handler 拿不到；走 KeyboardInterrupt
    # 让正常异常处理 + finally 接管最干净（asyncio 也会把 CancelledError 撒给所有任务）。
    def _term_to_keyboard(signum, frame):  # noqa: ARG001
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, _term_to_keyboard)

    cfg = load_config(Path(args.config))

    # `--model` 曾经生效、现在语义已不成立（yml 的 train.model 是三个角色的 dict）。
    # 报错而不是忽略：静默吞掉它会让人以为整个 run 跑的是自己指定的那个模型。
    if getattr(args, "model", None) is not None:
        p.error(
            "--model 已移除：train.model 现在按角色分成 task/reflect/consolidate 三个键"
            "（一个模型名无法表达该覆盖哪个角色）。请改用 --model-task / --model-reflect / "
            "--model-consolidate；三个角色都要换就三个都给。"
        )

    # ---- 断点续训：--resume 指向旧 results json ----
    # （math 域已于 2026-10-04 移除；现在 --domain 只接受 minecraft，无需再按域名拦截）
    # ★ --resume 与 --resume-window 强制配对（2026-09-11）：二者必须同时出现。
    #   - --resume-window 不带 --resume：window 是续跑子选项，脱离续跑语义无意义。
    #   - --resume 不带 --resume-window：原「自动取最大窗口」路径不调 rollback_to_window，
    #     扁平 JSON 的 last_reflection/last_goal/metrics_snapshot 不被回退覆盖，会读入
    #     活数据中「未跑完的撕裂态」（env 层 n_nights 在 night_cb 反思前就递增，中断在
    #     二者之间时扁平出现 nights=N 但反思停第 N-1 天；更糟的形态是 _checkpoint_cb 写了
    #     nights=N+第N天反思但 world save 未完整）。续跑一律走 rollback 显式回退到窗口 N，
    #     四类活数据（扁平 JSON / arm_graphs / skills / brain.db）全部从 checkpoint{N}/ 同源覆盖。
    if args.resume_window is not None and not args.resume:
        p.error("--resume-window N 必须与 --resume 一起用：它指定从哪个 checkpointN 续跑恢复。")
    if args.resume and args.resume_window is None:
        p.error(
            "--resume 必须配 --resume-window N：续跑一律走 rollback 显式回退到窗口 N，"
            "保证活数据（扁平 checkpoint 的 last_reflection/last_goal/metrics_snapshot、"
            "arm_graphs、skills、brain.db）从 checkpoint{N}/ 同源覆盖，不被活数据中未跑完的"
            "半截数据污染。不带 --resume-window 的普通续跑已移除。"
        )
    resume_data: dict | None = None
    start_epoch = 1
    if args.resume:
        resume_path = Path(args.resume)
        if not resume_path.exists():
            p.error(f"--resume 指向的文件不存在：{resume_path}")
        resume_data = json.loads(resume_path.read_text(encoding="utf-8"))
        if resume_data.get("domain") != args.domain:
            p.error(
                f"--resume 指向的旧结果 domain={resume_data.get('domain')!r}，"
                f"与本次 --domain {args.domain!r} 不一致，图谱/任务定义不兼容，拒绝续训。"
            )
        # ★ 对照组/实验组不可跨组续训：曲线可比性的前提是两组各自连续。对照组（no_graph）resume
        #   到实验组、或反之，会把「无图谱基线」与「有图谱曲线」混进同一条 train_curve，per_arm
        #   逐角色 join 时对不上。读旧 run 的 ablation 标记（顶层或 config 任一），与本次 flag 比。
        # ★ 消融 A 无共享（no_aggregate）同款硬拒：A 与 Full / no_graph 曲线均不可比（A 副脑真图但
        #   永不聚合、技能限本臂，与两者结构不同），不可互相续。三者 ablation 标记任一不一致即拒。
        old_ablation = resume_data.get("ablation") or (resume_data.get("config") or {}).get("ablation")
        new_ablation = ("no_aggregate" if getattr(args, "no_aggregate", False)
                        else ("no_graph" if args.no_graph else None))
        if old_ablation != new_ablation:
            p.error(
                f"--resume 跨 ablation 口径续训被拒：旧 run ablation={old_ablation!r}，"
                f"本次 ablation={new_ablation!r}（--no-graph={'开' if args.no_graph else '关'}"
                f" / --no-aggregate={'开' if getattr(args, 'no_aggregate', False) else '关'}）。"
                f"Full / no_graph / no_aggregate 三者曲线不可比，不能互相续。请用同口径的 JSON 续。"
            )
        # ★ 跨天上下文口径（2026-09-24 定，用户）：context_mgmt=记忆层连带（对照组 --no-graph
        #   → False，其余 → True），不落独立字段、无独立守卫——历史 run 的实际口径与今日
        #   派生一致（实验组一直 True、对照组一直 False），resume 由上面的 ablation 硬拒
        #   承载即可。唯一跨口径行=nomen（−M 旧口径 cm=True），处理方式=全新重跑（不续跑）。
        # 复用旧 run_ts + run_host：run_dir/context_dir/footage_dir/结果文件名全部落回同一批路径，
        # 而不是新起一份带新时间戳的目录（那样 brain.db/arm_graphs 就读不到旧数据了）。host 必须一起
        # 沿用 —— 否则拼出的 `<domain>_<ts>_<当前host>` 对不上旧 run 实际的 `<domain>_<ts>_<旧host>`
        # 目录，读不到旧 brain.db/arm_graphs。旧 JSON 无 run_host 字段（迁移前的 run）→ 空串，
        # 目录名退化到 `<domain>_<ts>` 对上当时不带 host 的旧目录。
        global _RUN_TS, _RUN_HOST  # noqa: PLW0603  模块级常量，本函数内按需重赋一次
        global _RUN_MODEL_SLUG, _RUN_GRAPH_TAG, _RUN_ABLATION_TAG  # noqa: PLW0603  目录名模型/图谱/消融段同样沿用旧 run
        _RUN_TS = resume_data["run_ts"]
        _RUN_HOST = resume_data.get("run_host") or ""
        # ★ 从旧 JSON 恢复模型/图谱段，使续跑拼出的目录名对上旧 run_dir。host 之上的同款纪律：
        #   不沿用就会用当前 yml/CLI 的 model 把目录名拼错、读不到旧 brain.db/arm_graphs。
        #   旧 JSON 无 config.model → 模型段空串；ablation 缺失/None 推不出图谱段 → 空串。
        # ★ 消融 A（no_aggregate）图谱段=graph（有私有图）+ 消融段=noagg；与 no_graph（nograph）
        #   和 Full（graph）三者区分。消融段见 _RUN_ABLATION_TAG 注释。
        _old_cfg = resume_data.get("config") or {}
        _RUN_MODEL_SLUG = str((_old_cfg.get("model") or {}).get("task") or "")
        _old_abl = resume_data.get("ablation") or _old_cfg.get("ablation")
        if _old_abl == "no_graph":
            _RUN_GRAPH_TAG = "nograph"
            _RUN_ABLATION_TAG = ""
        elif _old_abl == "no_aggregate":
            _RUN_GRAPH_TAG = "graph"
            _RUN_ABLATION_TAG = "noagg"
        elif _old_abl is None and _RUN_MODEL_SLUG:
            _RUN_GRAPH_TAG = "graph"
            _RUN_ABLATION_TAG = ""
        else:
            _RUN_GRAPH_TAG = ""
            _RUN_ABLATION_TAG = ""
        # ★ **目录名一致性校验**（2026-09-09）：新命名给目录名加了模型/图谱段，但 0908 前的旧 run
        #   目录名不含这两段（如 minecraft_<ts>）。从 JSON 恢复出的段拼出的目录若不存在，
        #   先尝试退掉模型+图谱段（对上迁移前不带 host 的旧目录），仍不存在 → 报错：静默新建空目录
        #   会让续跑读不到旧 brain.db/arm_graphs，必须显式拦下。
        _results_root = _RESULTS_ROOT
        if not (_results_root / _run_stem(args.domain)).is_dir():
            _RUN_MODEL_SLUG, _RUN_GRAPH_TAG, _RUN_ABLATION_TAG = "", "", ""
            if not (_results_root / _run_stem(args.domain)).is_dir():
                p.error(
                    f"--resume 拼出的 run 目录不存在：{_results_root / _run_stem(args.domain)}。\n"
                    f"旧 run 目录名不含模型/图谱段、与新版命名不兼容，无法用新代码续跑。"
                    f"请用目录名对应的旧二进制续跑，或把旧目录改名为新格式后再续。"
                )
        train_curve_prev = resume_data.get("train_curve") or []
        eval_curve_prev = resume_data.get("eval_curve") or []
        prev_epoch = max(
            [pt.get("epoch", 0) for pt in train_curve_prev]
            + [pt.get("epoch", 0) for pt in eval_curve_prev] + [0]
        )
        start_epoch = prev_epoch + 1
        logger.info(
            "[resume] 从 %s 续训：run_ts=%s run_host=%s（沿用旧 run_dir），已完成 %d 轮，本次从第 %d 轮起跑",
            resume_path, _RUN_TS, _RUN_HOST or "(空,兼容迁移前)", prev_epoch, start_epoch,
        )

    # ---- 规模参数三层解析：命令行 > smoke（若开） > yml train/eval ----
    # 顺序即优先级。smoke 只覆盖 yml，**不覆盖显式命令行** —— 这样 `--smoke --epochs 6`
    # 能按预期跑 6 轮（改前 smoke 硬钉 epochs=1，会把显式 --epochs 静默吃掉）。
    scale = {**cfg.train, **cfg.eval}
    if args.smoke:
        scale.update(cfg.smoke)
    for key, flag in (("arms", "arms"), ("epochs", "epochs"), ("n_train", "n_train"),
                      ("batches", "batches"), ("n_samples", "n_samples"),
                      ("n_eval", "n_eval"), ("k", "eval_k")):
        v = getattr(args, flag, None)
        if v is not None:
            scale[key] = v
    # `model` 不在上面那个循环里：它是**三个角色的 dict**，逐角色覆盖而不是整节替换 ——
    # 整节替换的话 `--model-task x` 会把 reflect/consolidate 两个键直接抹掉（然后
    # ModelConfig.from_dict 报"缺角色"，而报错原因跟用户做的事对不上）。
    scale["model"] = {**scale["model"],
                      **{r: v for r in MODEL_ROLES
                         if (v := getattr(args, f"model_{r}", None)) is not None}}
    # ★ **新 run 设目录名模型/图谱段**（2026-09-09）：仅在非 resume 时执行 —— resume 路径在
    #   上方 --resume 分支已从旧 JSON 恢复 _RUN_MODEL_SLUG/_RUN_GRAPH_TAG（对上旧 run_dir），
    #   这里再覆盖会用当前 yml/CLI 的 model 把目录名拼错、读不到旧 brain.db/arm_graphs。
    #   scale["model"]["task"] 必存在（_role_dict 强制三角色齐全，见 config.py:223）。模型名
    #   原样取，不做 slug 清洗（目录名不被反解析，见 _run_stem docstring）。
    #   （global 已在上方 --resume 分支声明，函数级生效，此处不重复声明。）
    if not args.resume:
        _RUN_MODEL_SLUG = str(scale["model"].get("task") or "")
        _RUN_GRAPH_TAG = "nograph" if args.no_graph else "graph"
        # ★ 消融段（2026-09-15）：A 无共享 → "noagg"。A 的图谱段仍是 graph（有私有图），
        #   消融段独立追加。B/C 待实现时在此加 "nopriv"/"flatret"。
        _RUN_ABLATION_TAG = ("noagg" if getattr(args, "no_aggregate", False) else "")
    _override_llm_effort(args)
    logger.info("[规模] arms=%s epochs=%s n_train=%s batches=%s n_samples=%s | n_eval=%s k=%s%s%s",
                scale["arms"], scale["epochs"], scale["n_train"], scale["batches"],
                scale["n_samples"], scale["n_eval"], scale["k"],
                "（smoke）" if args.smoke else "",
                "（对照组 no_graph：无图谱、聚合 no-op、不跑基线）" if args.no_graph else "")

    # ---- 域私有规模三层解析（同一优先级顺序）：命令行 > smoke > yml domain_scale.<域> ----
    # smoke 的 max_nights=1 等值现在住在 yml 的 `smoke.domain_scale`，不再在这里改 args
    # —— 改 args 会让「命令行 > smoke」这条优先级反过来（显式 --max-nights 被 smoke 静默吃掉）。
    domain_scale = _resolve_domain_scale(cfg, args, args.domain)
    if domain_scale:
        logger.info("[规模][%s] 域私有：%s%s", args.domain, domain_scale,
                    "（smoke）" if args.smoke else "")

    if args.domain == "minecraft":
        # ★ MC 的训练侧自由度是 epochs + max_nights + aggregate_every（理由见 MinecraftDomain
        # docstring）：框架侧的 batches/n_train/n_samples 全钉为 1 ——
        # task 内容全同 + 每次 rollout 前重生世界 → 训练侧同一目标重复采样不增记忆维度的信息；
        # 而 arm 图谱按 (epoch,batch) 新建（下一轮不读上一轮那份），**只有 epoch 能让记忆跨轮累积**，正是学习曲线的 X 轴。
        # - n_train=1 时 _split(tasks,batches) 恒返回 1 个 batch → batches 自动坍缩，设别的值无效。
        #   「一条轨迹内多次聚合」由 --aggregate-every（题内 window）表达，不由 batches。
        # - **eval 侧**：本域走 train_curve（不跑逐轮 eval），n_eval/k 只决定**训练前那一次
        #   frozen 空记忆基线**采几条轨迹。基线是训练曲线的水平参照线，越稳越好，但每条都要付
        #   一条完整轨迹的墙钟 → 保持小值（yml smoke 里是 1）。
        #   ⚠️ **n_eval=0 时基线整个不跑**（brain.py 的 `if eval_tasks`），于是曲线点里
        #   没有 frozen_baseline / frozen_detail —— 「得分上升」就无法与「世界随机性/运气」
        #   区分，那条曲线也就进不了论文。默认已改为 1（2026-08-17），别再调回 0。
        forced = {"batches": 1, "n_train": 1}
        overridden = {k: scale[k] for k, v in forced.items() if scale.get(k) != v}
        scale.update(forced)
        if overridden:
            logger.info(
                "[minecraft] batches/n_train 已钉为 1（原值 %s）：训练轮数用 --epochs，"
                "轨迹内聚合次数用 --aggregate-every，基线轨迹数用 --n-eval/--eval-k", overridden)

        # ★ 题内聚合窗口的前提：**一个 batch 内每个副脑恰好跑一次 rollout**。
        # 否则第 2 次 rollout 的 window 1 会跟第 1 次的 window 1 撞成同一个 key（收集器按
        # window 号攒份数），两条轨迹的经验被当成"同一窗口的两个副脑"混起来聚合 —— 跑得通、
        # 但数据静默错。**报错而不是静默降级**，让哪天有人放开这两个值时立刻看见冲突。
        #
        # ⚠️ 用**生效值**算，不是 scale 里的原值：`n_samples` 会被
        # `MinecraftDomain.n_samples=1` 在 loop.py 里覆盖（域声明的编排约束），而 yml 默认
        # 给的是 4 —— 拿原值算会让**默认配置**都触发这个报错（实测踩到）。
        # 域声明优先级更高，所以这里问域要它的最终值。
        from domains.minecraft import MinecraftDomain
        eff_samples = MinecraftDomain.n_samples or int(scale["n_samples"])
        n_roll = int(scale["n_train"]) * int(eff_samples)
        if int(domain_scale.get("aggregate_every") or 0) > 0 and n_roll > 1:
            p.error(
                f"--aggregate-every > 0（题内聚合）要求一个 batch 内每个副脑只跑一次 rollout，"
                f"但当前 n_train={scale['n_train']} × n_samples={eff_samples}（域生效值）"
                f" = {n_roll} 次。多次 rollout 的窗口号会互相覆盖、把不同轨迹的经验混进同一次"
                f"聚合（静默错数据）。要更多样本请调 --epochs。"
            )

    # ★ 不再 init_db()/init_kg_db()（2026-08-27）：那两个建的是 宿主项目生产库的表，
    #   OpenKAGE 跑训练全程不碰它们（math 域只读生产 data/kg.db，minecraft 从空图起步）。
    #   删掉后 backend 不再进 sys.path，openkage 自包含。

    # ★ 副脑**先读**，再装配 domain（2026-07-31 调序）：minecraft 的 worker 数默认取副脑数，
    # 而实际副脑数可能小于 scale["arms"]（arms/<domain>/ 下的 md 不够多时被截断）。
    # 拿 scale["arms"] 去起 server 会多起几台永远闲着的 JVM（每台 2G）。
    # 副脑角色按 domain 分子目录：arms/<domain>/*.md（math 学生人设 / minecraft 探索策略）
    arms_dir = _ARMS_DIR / args.domain
    arms = select_arms(arms_dir, args.only_arm, int(scale["arms"]))
    if args.only_arm:
        scale["arms"] = 1  # select_arms 已筛成单臂，同步 scale 供下游 worker/日志推算
    if not arms:
        raise FileNotFoundError(f"{arms_dir} 下没有可用的副脑角色 md（需带 frontmatter）")
    if len(arms) < int(scale["arms"]):
        logger.info("[规模] 副脑数落定：请求 %s，%s 下只有 %d 个可用 md → 按 %d 跑",
                    scale["arms"], arms_dir, len(arms), len(arms))

    # ★ --resume-window N：从指定 checkpointN 恢复（2026-09-07）。在构造 OpenKAGE 之前把 N+1..末
    #   窗口备份+删掉、扁平JSON 回退到 N 边界夜，让 latest_window_with_world 取到 N、三者
    #   （private/skill/public）从 checkpoint{N}/ 同源恢复。清掉 N 之后是为了不影响续跑后落盘。
    #   仅 --resume 时生效（上面已校验 --resume-window 必须配 --resume）。resume_round 与续跑
    #   read_progress 用的 round 同源（_encode_round(start_epoch,1)），保证回退后的扁平JSON能被命中。
    if args.resume and args.resume_window is not None:
        from openkage.roundcode import encode as _encode_round
        from openkage.orchestrate.checkpoint import rollback_to_window
        _resume_ckpt_dir = _RESULTS_ROOT / _run_stem(args.domain)
        _resume_round = _encode_round(start_epoch, 1)
        try:
            _rb = rollback_to_window(_resume_ckpt_dir, [a.key for a in arms],
                                     args.resume_window, _resume_round)
            logger.info("[resume-window] 已回退到窗口%d：删除窗口 %s，扁平JSON 回退臂 %s，备份→%s",
                        args.resume_window, _rb["deleted_windows"] or "(无)",
                        _rb["rolled_back_arms"], _rb["backed_up_to"] or "(无删除)")
            # ★ 曲线历史同边界截断（2026-09-12）：rollback_to_window 只动 checkpoint 文件，
            #   顶层 run.json 的 consolidation_log 原样残留 → 续跑后 window 撞号。此处按窗口
            #   N 边界截断（与 checkpoint 回退同口径），train_curve 不动（epoch 绝对编号不随
            #   窗口回退）。daily_metrics 字段已删除（逐夜指标只存 checkpoint 的
            #   night_reflections，本身随扁平 JSON 回退、无需截断）。同步更新内存
            #   resume_data：已读入，截断改的是磁盘，不同步则铺回 octo.* 的仍是未截断旧
            #   list → 撞号污染复活。
            from openkage.orchestrate.checkpoint import truncate_curve_history
            _tc = truncate_curve_history(resume_path, args.resume_window,
                                         domain_scale["aggregate_every"])
            if _tc is not None:
                resume_data["consolidation_log"] = _tc["consolidation_log"]
                logger.info("[resume-window] 曲线历史截断到窗口%d：consolidation_log 留%d条、"
                            "train_curve 不动(%d点)",
                            args.resume_window, _tc["consolidation_log_kept"],
                            _tc["train_curve_untouched"])
        except ValueError as _e:
            p.error(f"--resume-window {args.resume_window} 无效：{_e}")

    domain, build_cfg = _build_domain(args, scale, len(arms), domain_scale)
    models = ModelConfig.from_dict(scale["model"])
    # 域私有前置（math：备份生产 KG + 导入教材种子 → seed 路径；minecraft：空图 → None）。
    # 给 `consolidate` 那个角色：种子导入建的是一张 **BrainGraph**（主脑），而 BrainGraph 上
    # 唯一跑 LLM 的入口就是 consolidate。⚠️ 实际上这条路径**一次 LLM 都不调**（只做结构转换
    # + embedding，见 _kg_seed._import_seed_from_kg），所以这里传谁都不改变行为；显式传
    # consolidate 是为了别在"哪个角色"这个问题上留一个含义不明的 None。
    seed = domain.prepare(model_name=models.consolidate)

    if resume_data is not None:
        # 一致性提醒而非硬挡：brain.db 打开已有文件就能续用，即使这些值变了也跑得通，
        # 但曲线的可比性会被打断（"轮次涨了"混进"这轮换了模型/世界更长"两种解释）。
        # 响亮地说出来，不代替用户决定要不要继续。
        old_cfg = resume_data.get("config") or {}
        mismatches = []
        old_arms = old_cfg.get("arms")
        if old_arms is not None and sorted(old_arms) != sorted(a.key for a in arms):
            mismatches.append(f"arms: 旧={old_arms} 新={[a.key for a in arms]}")
        old_ds = (old_cfg.get("domain_scale") or {})
        for k in ("max_nights", "aggregate_every"):
            if k in old_ds and old_ds[k] != domain_scale.get(k):
                mismatches.append(f"{k}: 旧={old_ds[k]} 新={domain_scale.get(k)}")
        old_model = old_cfg.get("model") or {}
        new_model = models.as_dict()
        if old_model and old_model != new_model:
            mismatches.append(f"model: 旧={old_model} 新={new_model}")
        if mismatches:
            logger.warning(
                "[resume] 本次配置与旧 run 不完全一致（不阻断，但曲线可比性会受影响）：%s",
                "；".join(mismatches),
            )

    train_tasks, eval_tasks = domain.build_tasks(build_cfg)
    train_tasks, eval_tasks = _apply_n_limits(train_tasks, eval_tasks, scale)
    # eval.k：一次评估里每题重复跑几次 —— 用「把每个 eval task 复制 k 份」实现，因为
    # aggregate_eval 本就对一组 samples 求分（各域自定求和/均值口径），复制后天然是重复采样取均。
    k = int(scale["k"])
    if k > 1 and eval_tasks:
        eval_tasks = [t for t in eval_tasks for _ in range(k)]
        logger.info("[eval] k=%d → 每题重复评 %d 次（eval 采样数 %d）", k, k, len(eval_tasks))
    logger.info(
        "[%s] OpenKAGE训练：arms=%d E=%d model=%s | train=%d eval=%d",
        domain.name, len(arms), int(scale["epochs"]), models.summary(),
        len(train_tasks), len(eval_tasks),
    )

    run_dir = _RESULTS_ROOT / _run_stem(args.domain)
    octo = OpenKAGE(brain_config=cfg, arms=arms, domain=domain,
                   llm=models, seed=seed, workdir=run_dir,
                   n_samples=int(scale["n_samples"]), no_graph=args.no_graph,
                   no_aggregate=getattr(args, "no_aggregate", False),
                   # ★ 跨 run 图谱迁移种子（§3.4）：已完赛 run 的 results 目录 → 公共图 + 技能库
                   #   起步，私有图清记忆层。None=常规从空图起步的 run。
                   seed_graph=getattr(args, "seed_graph", None))
    if resume_data is not None:
        # 曲线是纯内存 list（OpenKAGE.__init__ 里新建成空列表），断点续训要接着画同一条线，
        # 就把旧 run 的历史点原样铺进去，本次新点在其后 append —— 而不是另起一条只有
        # 本次几轮的新曲线。
        octo.train_curve = list(resume_data.get("train_curve") or [])
        octo.eval_curve = list(resume_data.get("eval_curve") or [])
        octo.consolidation_log = list(resume_data.get("consolidation_log") or [])

    # ★ 结果 JSON 直接落进各自的 run 子目录（results/<domain>_<ts>_<host>/<domain>_<ts>_<host>.json），
    #   不再散在 results/ 顶层 —— 顶层 *.json 与 results/*/ 并存既乱，也让 report_metrics 的
    #   `glob("*_*.json")` 误中 run_dir 里别的 json。run_dir 上面已建（与 run.py 同源 _RUN_TS/_RUN_HOST），
    #   exist_ok 再兜一次：resume 沿用旧 run_ts/run_host 时目录本就存在，不重建。JSON 名取 run_dir.name，
    #   使「目录名 = JSON 名」对齐下游 report_metrics / quick_metrics 的定位假设。
    run_dir.mkdir(parents=True, exist_ok=True)
    out = run_dir / f"{run_dir.name}.json"

    def _snapshot_results() -> dict:
        # 现取一次 LLMConfig（命令行的 effort 覆盖已经落进它的缓存）——
        # config 节要记 reasoning_effort / sdk_timeout 的**实际生效值**。
        _llm_cfg = LLMConfig.load()
        return {
            "run_ts": _RUN_TS, "run_host": _RUN_HOST, "domain": domain.name,        # 记落定后的实际规模（不是命令行原值）——复盘时要知道真跑了多少，而非请求了多少。
            # ★ 对照组标记：事后 join 实验组/对照组两份 JSON 的唯一标识（None=实验组）。
            #   resume 跨组校验也读它（见 main 的 --resume 分支）。
            "ablation": ("no_aggregate" if getattr(args, "no_aggregate", False)
                         else ("no_graph" if args.no_graph else None)),
            # ★ 跨天上下文口径（2026-09-24 定，用户）：不落独立字段——context_mgmt=记忆层
            #   连带，由 ablation/no_graph 唯一派生（对照组无记忆层 → 无【昨日目标】/
            #   【昨日经历】；其余照跑），读端看 ablation 即可。0913-0923 旧 run 落有
            #   context_mgmt 布尔（独立 flag 时代的值，仅 −M/nomen 一条与今日派生不符，
            #   已在 RESULTS_TABLE 说明 0 注记）。resume 的旧字段守卫仍读它（拦旧 −M 续跑）。
            # ★ 迁移种子 run（§3.4）：非空=本次 run 的公共图谱/技能库是从该 run 的成果起步的
            #   （私有图谱的记忆层已清）。入表与横比时必看——种子化 run 与从空图起步的 run
            #   不是同一个条件，混进同一组统计会污染均值。
            **({"seed_graph": str(args.seed_graph)} if getattr(args, "seed_graph", None) else {}),
            "config": {
                "arms": [a.key for a in arms],
                "epochs": int(scale["epochs"]),
                "batches": int(scale["batches"]),
                "n_samples": int(scale["n_samples"]),
                "n_train": len(train_tasks),
                "n_eval": len(eval_tasks),
                "eval_k": int(scale["k"]),
                # 三个角色各自的模型（不是一个字符串）—— 复盘时"任务域用的什么、聚合用的什么"
                # 是解释曲线与墙钟的第一位上下文，混成一个名字就再也分不出来。
                "model": models.as_dict(),
                # thinking 档位与超时**也按角色记**：它们直接决定单步能不能出 tool_call
                # （不配 = 最高档 = 吃满预算 → 0 个 tool_call），是解释"某个窗口 Δ 为 0"
                # 的第一位线索。从 LLMConfig 现取，所以命令行覆盖也会落进来。
                "reasoning_effort": dict(_llm_cfg.reasoning_effort),
                "sdk_timeout": dict(_llm_cfg.sdk_timeout),
                # 域私有规模（落定后的值）：minecraft 的 max_nights/aggregate_every 直接决定
                # 「轨迹多长 + 一条轨迹内聚合了几次」，是复盘曲线时的第一位上下文。
                **({"domain_scale": domain_scale} if domain_scale else {}),
                # 并行度也是「实际跑了什么」的一部分（minecraft）：worker 数 < arms 时副脑是排队跑的，
                # 墙钟与全并发的 run 不可直接比。取 domain 报的真实池大小，不是命令行原值。
                **({"mc_workers": domain.n_workers} if hasattr(domain, "n_workers") else {}),
                # 复盘用：出生点/世界种子决定这条轨迹的可复现性（此前只在日志里出现，没落进
                # 结果 JSON，事后复盘要靠翻 run 日志才能找到）。★ 2026-08-29 迁到 yml 后，
                # 真值在 domain_scale.minecraft（见上），这里镜像一份到顶层兼容旧 reader
                # （report_metrics 仍读顶层 cfg['world_seed']）；取落定值（yml+CLI 解析后）而非 args。
                **({"world_seed": domain_scale.get("world_seed"),
                    "spawn_seed": domain_scale.get("spawn_seed")}
                   if args.domain == "minecraft" else {}),
                **({"resumed_from": str(Path(args.resume))} if args.resume else {}),
                # 对照组标记（与顶层 ablation 同值，放 config 里方便 resume 校验统一从 config 读）。
                "ablation": ("no_aggregate" if getattr(args, "no_aggregate", False)
                             else ("no_graph" if args.no_graph else None)),
            },
            "eval_curve": octo.eval_curve,
            "train_curve": octo.train_curve,
            "consolidation_log": octo.consolidation_log,
            # 逐日量化指标不落主 JSON（2026-09-12 删除 daily_metrics 字段）：discoveries 逐夜
            # 已随 night_reflections 存进各 arm checkpoint（每窗口覆盖写、异常终止不丢），
            # 主 JSON 的扁平时序是纯冗余副本且只在 epoch 边界落盘。消费端读 checkpoint。
        }

    def _checkpoint(epoch: int) -> None:
        # 每个 epoch 边界立即覆写 results json（不等训练全跑完）：进程中途被杀时，
        # 已完成轮次的 train_curve/eval_curve/consolidation_log 不会随内存一起丢失
        # ——它们本来只在这一份 JSON 里有落盘（图谱记忆走 brain.db，两者独立）。
        out.write_text(json.dumps(_snapshot_results(), ensure_ascii=False, indent=2),
                       encoding="utf-8")
        logger.info("[checkpoint e%d] 已写入 %s", epoch, out)

    try:
        octo.run(train_tasks, epochs=int(scale["epochs"]), eval_tasks=eval_tasks,
                 batches=int(scale["batches"]), start_epoch=start_epoch,
                 on_epoch_end=_checkpoint)
    finally:
        # ★ SIGINT/异常都写 JSON（2026-08-19）：原先 write_text 在 finally 外，进程被杀时
        # train_curve 不落盘，--resume 报"文件不存在"。移进来后哪怕轨迹还没跑完也能续跑。
        # write_text 在 close/shutdown 之前：_snapshot_results 访问 octo/domain 的字段，
        # 此时实例仍完整；close/shutdown 失败也不影响 JSON 已落盘。
        try:
            out.write_text(json.dumps(_snapshot_results(), ensure_ascii=False, indent=2),
                           encoding="utf-8")
            logger.info("[finally] 已写入 %s", out)
        except BaseException as _e:
            logger.warning("[finally] 写 results JSON 失败（%s: %s），续跑需手工补最小 JSON",
                           type(_e).__name__, _e)
        octo.close()
        if hasattr(domain, "shutdown"):
            # Minecraft：停掉所有 worker（bot + mineflayer + MC server）。这些是本次 run 的临时
            # 资产，每台 worker 一个 ~2.5G RSS 的 JVM，不停就是泄漏。
            domain.shutdown()

    out.write_text(json.dumps(_snapshot_results(), ensure_ascii=False, indent=2),
                   encoding="utf-8")
    logger.info("完成，写入 %s", out)
    if octo.train_curve:
        # 终身学习域（domain.train_curve=True）：X=训练轮次，Y=本轮各 arm 得分均值。
        # 只要呈增长趋势即说明记忆在起作用；frozen 基线是"空记忆水平线"，供判断增长是否真实。
        base = octo.train_curve[0].get("frozen_baseline")
        logger.info(
            "train_curve (轮次 → 各arm均分%s) = %s",
            f"，frozen空记忆基线={base:.1f}" if base is not None else "",
            [(p["epoch"], round(p["score"], 1)) for p in octo.train_curve],
        )
        scores = [p["score"] for p in octo.train_curve]
        if len(scores) >= 2:
            logger.info("  首轮 %.1f → 末轮 %.1f（净变化 %+.1f）",
                        scores[0], scores[-1], scores[-1] - scores[0])
    if octo.eval_curve:
        logger.info(
            "eval_curve (evolving vs frozen, gap) = %s",
            [(e["epoch"], f"{e['evolving_score']:.1f}/{e['frozen_score']:.1f}", f"{e['gap']:+.1f}")
             for e in octo.eval_curve],
        )


if __name__ == "__main__":
    main()
