"""题内聚合窗口的收集器（任务域无关）。

**要解决的问题**：`batches` 切的是**题目列表**，切不进一条轨迹的内部。而 minecraft 一个训练题
就是一整条 `max_nights` 天的轨迹 —— 「一条 365 夜的轨迹每 7 夜聚合一次」在 `batches` 上物理
表达不出来。本模块提供那个缺失的维度：**window**。

**编排形态（2026-08-19 改：栅栏同步）**::

    arm1 ──d5,d6,d7─┐
    arm2 ──d5,d6,d7─┼─▶ 3/3 齐 → leader 快照+聚合+清层 → 一起进 d8,d9…
    arm3 ──d5,d6,d7─┘

副脑到达窗口边界后**栅栏等齐**（不清层），最后一个到达者当 leader 独占「快照 → 聚合 → 清层」
（各 ArmGraph 自带连接+锁，leader 可清所有副脑的层，见 `_WindowGate`），followers 在 gate 上等
结果：成功才一起放行进第 K+1 天；失败则 abort 整个 batch（见 `WindowAggregationError`）。

★ **为什么从「不阻塞」改成栅栏同步**（用户 2026-08-19 定，墙钟换可维护性）：
  ① 消掉主脑公共图谱「一个窗口 + 一次聚合时长」的检索滞后 —— 栅栏同步下，所有副脑进第 K+1
     天时主脑已有窗口 K 的结论，记忆反馈回路无滞后；
  ② 聚合失败不再是「尽力继续」（旧版经验层已清、丢失、训练带污染继续），而是 **abort batch**：
     失败的窗口继续跑会把「主脑缺这一窗口」的污染带进后续训练。abort 时经验层**未清**（清层在
     聚合成功之后），数据完整可复盘。

★ **为什么快照而不是直接把活的副脑图谱交给聚合**：聚合读快照、清层改活库，两者分离 —— 聚合
读的是一份一致的、不受随后清层影响的快照（已实测）。

★ **清层在聚合成功之后**（与旧版「快照后立刻清」相反）：失败时经验层保留、可事后手工聚合。
这是 abort 语义能成立的前提 —— 否则 abort 了也丢了数据。

**Checkpoint 整合（2026-08-19）**：若构造时传入 checkpoint_dir，leader 在聚合成功后写聚合快照
（含经验层的 snap.db）到 checkpoint{W}/arm_{key}/，主脑快照写 checkpoint{W}/public.db。
清层后的私有图谱 checkpoint（private.db + vec + progress.json）仍由 domain 的 night_cb 在
_make_night_reflection 之后写——届时 last_reflection 已生成，progress.json 一次完整。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from openkage.memory.graph import ArmGraph, BrainGraph
from openkage.memory.schema import LAYER_EXPERIENCE
from openkage.logctx import isolated_scope
from openkage.orchestrate.consolidate import consolidate as _consolidate
from openkage.roundcode import FIRST_WINDOW

logger = logging.getLogger("window")


class WindowAggregationError(RuntimeError):
    """题内窗口聚合失败。

    ★ **聚合失败 = abort 整个 batch**（2026-08-19 用户定）：失败的窗口继续跑会把
    「主脑缺这一窗口贡献」的污染带进后续训练。这是个比「尽力继续」更强的语义 ——
    用专用异常类型，让编排层（loop.py 的 rollout except）能把它与「世界卡死」这类
    rollout 自身故障区分开：聚合失败要 abort，rollout 自身故障仍「记0样本继续下一个」。

    栅栏同步下，本异常由 leader 在 `_WindowGate` 里抛出、经 Event 唤醒所有 follower
    一并抛出 → 穿透各副脑的 submit → run_arm_batch → brain.py 的 gather abort 整个 batch。
    此时**各副脑经验层尚未清**（清层在聚合成功之后），故失败可复盘、经验不丢。
    """


class _WindowGate:
    """一个窗口的**栅栏同步**闸门（2026-08-19：后台异步 → 栅栏同步；2026-08-29：拆 arrive/collect）。

    三栅栏反思后，"等齐"和"等聚合结果"拆成两步（见 NightBarrier 三栅栏设计）：
    - `done`（Event）= **全员到齐**（不再是"聚合完"）。arrive 在此等齐，齐了就放行进阶段2，
      **不等聚合**——聚合与阶段2 并行。
    - `result`（Future）= 聚合结果（leader 起 `_run_window_sync` 后台任务填入）。collect 在栅栏3
      await 它取 cleared；失败 future 带异常 → collect 抛 `WindowAggregationError`（abort batch）。

    leader 在 arrive 里（最后一个到达者）起 `result` future 并 `done.set()` 唤醒 followers 进阶段2。
    followers 在 collect 里 `await result`。retire 后台 leader 路径（`_leader_complete`）同。

    为什么 leader 能清所有副脑的层：每个 ArmGraph 有自己的 `_conn`+`_lock`
    （graph.py:144,153），`snapshot_to`/`clear_layer` 操作的是该图的连接，任意协程
    都能对任意 ArmGraph 对象调用 —— 故 leader 单程完成所有副脑的快照+清层。**清层在聚合成功之后**：
    失败时经验层保留、可复盘。

    retire（副脑退场）会缩小「还需等谁」的集合；若退场后剩下的副脑都已到达，
    最后一个到达者即为 leader，gate 当场发车（不卡死）。
    """

    def __init__(self) -> None:
        #: = 全员到齐（arrive 在此等齐；齐了 leader 起 result 并 set 它，放行 followers 进阶段2）
        self.done = asyncio.Event()
        #: leader 起、followers 在 collect await：`_run_window_sync` 的 future（结果=cleared dict）
        self.result: asyncio.Future | None = None
        #: 旧 submit 兼容用（arrive/collect 拆分后 cleared 经 result 传，本字段保留给 submit 兼容路径）
        self.cleared: dict[str, dict] | None = None
        #: 旧 submit 兼容用：聚合失败原因（arrive/collect 路径走 result 异常）
        self.error: BaseException | None = None


class WindowAggregator:
    """收集各副脑的题内聚合窗口快照，攒齐一个窗口就**后台**跑一次聚合。

    一个实例服务一个 `(epoch, batch)`：`window` 在其内部从 1 开始递增。
    调用序列（由域经 `on_window` 回调驱动）::

        agg = WindowAggregator(primary, arm_graphs, display, snap_dir, epoch, batch, n_arms)
        await agg.submit("alice", window=1, at_day=7)   # 各副脑在自己第 7 天黎明各调一次
        ...
        agg.retire("alice")                            # 该副脑 rollout 结束/异常 → 不再供货
        await agg.drain()                              # batch 末：等所有后台聚合落地
    """

    def __init__(
        self,
        primary: BrainGraph,
        arm_graphs: dict[str, ArmGraph],
        arm_display: dict[str, str],
        snapshot_dir: Path,
        epoch: int,
        batch_id: int,
        checkpoint_dir: "Path | None" = None,
        round_num: int = 0,
        no_graph: bool = False,
        no_aggregate: bool = False,
    ) -> None:
        self._primary = primary
        self._arm_graphs = arm_graphs
        self._display = arm_display
        self._snap_dir = snapshot_dir
        self._epoch = epoch
        self._batch = batch_id
        self._checkpoint_dir = Path(checkpoint_dir) if checkpoint_dir else None
        self._round_num = round_num
        # ★ 对照组 ablation：True 时 _run_window_sync 走 no-op 短路（不聚合、不开副脑快照
        #   ArmGraph、不写主脑快照），world checkpoint + progress.json 由域侧 night_cb 写
        #   （见 domains/minecraft 夜间回调里对 no_graph 的跳过）。续跑靠后者，与本机制一致。
        self._no_graph = bool(no_graph)
        # ★ 消融 A 无共享（--no-aggregate，2026-09-15）：与 no_graph 共用 _run_window_sync 的
        #   no-op 短路（不聚合、不清层——A 要的正是经验层满 30 夜不清），但副脑是真 ArmGraph
        #   （反思照常写）。与 no_graph 的区别：no_graph 副脑也 NullGraph（any_experience_left
        #   短路返 False）；A 副脑真图、经验层非空，但批末聚合由 brain.py 的 `not no_aggregate`
        #   守卫跳过，故 any_experience_left 在 A 下不被用于「决定聚合」，保持原逻辑无害。
        self._no_aggregate = bool(no_aggregate)
        #: {window: {arm_key: 快照路径}} —— 攒够就发车
        self._pending: dict[int, dict[str, Path]] = {}
        #: {window: {arm_key: 快照时它在第几天}}，进留痕算滞后
        self._lag: dict[int, dict[str, int]] = {}
        #: 还在供货的副脑。rollout 结束/异常的从这里移除，否则它欠的那一份会让窗口永远等不齐。
        self._active: set[str] = set(arm_graphs)
        #: 后台聚合任务，batch 末统一 await
        self._tasks: list[asyncio.Task] = []
        #: {window: _WindowGate} —— 栅栏同步下各窗口的闸门。攒齐还在供货的副脑 → leader 发车。
        #: 取代了旧版「攒齐 → create_task 后台跑」：现在 submit **阻塞**到聚合跑完才返回。
        self._gates: dict[int, _WindowGate] = {}
        #: 已落地窗口的留痕 rec（leader 在 `_run_window_sync` 末尾 append）。drain() 返回它，
        #: brain.py 拿去 extend 进 `consolidation_log`。栅栏同步下 submit 返回即聚合完，
        #: drain 主要处理「有副脑到达但未成窗」的末窗口。
        self._records: list[dict] = []
        #: 只为日志：本 batch 一共发了几次窗口聚合
        self.n_windows_fired = 0
        #: 已经用掉的最大 window 号 —— **批末聚合要接在它后面，不能重用 1**。
        #: 否则批末那次与题内第 1 个窗口写出同一个 round 号，两次聚合的行在图谱里叠成一次、
        #: viewer 上也分不开。（实测踩到过：端到端跑出 (1,1,1) 出现两次。）
        self.max_window = 0

    @property
    def next_window(self) -> int:
        """下一个可用的 window 号，给**批末**聚合用（避免与题内各窗口撞号）。

        没开题内聚合时 `max_window=0` → 返回 `FIRST_WINDOW`（1），即批末那次就是 window 1
        —— 与本机制存在之前的 round 编码**完全一致**（严格泛化的要求之一）。
        """
        return max(self.max_window + 1, FIRST_WINDOW)

    def _ablation_of(self, arm_key: str) -> "str | None":
        """该臂**活图**的 _ablation（快照 ArmGraph 打开前从活图读，None=非消融 run）。

        为什么从活图读而不是构造参数：WindowAggregator 的调用方（brain.py）已把 ablation
        传给各臂 ArmGraph，这里再收一个参数就是同一值的第二个来源（静默分叉的形状——
        08-08 retrieval 双口径事故同款）。活图是唯一真相。
        """
        return getattr(self._arm_graphs.get(arm_key), "_ablation", None)

    def any_experience_left(self) -> bool:
        """还有没有哪个副脑的经验层非空 —— 用来决定批末聚合是否值得跑。

        开了题内聚合时，末窗口过后各副脑的经验层常常**全空**（刚清完、又没到下一个边界）。
        那时批末聚合是一次纯浪费的 ReActAgent 调用（可能一路跑到 max_consolidate_steps），
        而且 `_warn_layer_violations` 会对每个副脑打一条"经验层为 0"的 warning —— 那条告警
        本是用来抓「反思只写记忆层」这个**真故障**的，在这里却成了预期行为，
        白白训练读日志的人忽略它。

        ★ no_graph（对照组）：副脑图是 NullGraph、无 _conn，直接返 False（本就不该批末聚合）。
          不走下面的 try —— 它对 NullGraph 会 `g._conn.execute(...)` 抛 AttributeError，虽被
          except 吞成 return True（误判有经验），但那是把对照组带进一次必炸的批末聚合。短路掉。
        """
        if self._no_graph:
            return False
        for g in self._arm_graphs.values():
            try:
                n = g._conn.execute(  # noqa: SLF001
                    "SELECT COUNT(*) FROM concept WHERE layer = ?", (LAYER_EXPERIENCE,)
                ).fetchone()[0]
            except Exception:  # pylint: disable=broad-except
                return True  # 数不出来就别跳过：宁可多跑一次，不要静默漏掉真有的经验
            if n:
                return True
        return False

    # ---- 供货侧（域在自己的聚合边界上调）----

    async def submit(self, arm_key: str, window: int, at_day: int) -> dict:
        """某副脑到达窗口边界：**栅栏等齐 → leader 快照+聚合+清层 → 一起放行**。

        ★ 2026-08-19 改：从「先到先清层 + 后台聚合不阻塞」改为**栅栏同步**。
          - 到达边界**不清层**，记 `at_day` 进 `_pending`，在 `_WindowGate.done` 上阻塞等齐；
          - 最后一个到达者 = leader，独占跑「快照 → 聚合 → 清层」（见 `_WindowGate` docstring：
            各 ArmGraph 自带连接+锁，leader 可清所有副脑的层）；
          - **聚合成功才清层**（旧版是快照后立刻清）——失败时经验层保留、可复盘，且 leader 置
            `error` 唤醒 followers 一并抛 `WindowAggregationError` → abort 整个 batch。
          - followers 被唤醒后取回各自 arm 的清层计数返回。

        代价：submit 不再「立刻返回」，最慢的副脑卡住所有人在窗口边界等（用户 2026-08-19 定：
        墙钟换可维护性）。换来：主脑公共图谱无「一个窗口 + 一次聚合时长」的检索滞后，所有
        副脑进入第 K+1 天时主脑已有窗口 K 的结论。

        checkpoint 快照（snap.db，含经验层）仍由 leader 写到 checkpoint{window}/arm_{key}/；
        清层后的私有图谱 checkpoint（private.db + vec + progress.json）由 domain 的 night_cb
        在 _make_night_reflection 之后写——届时 last_reflection 已生成，progress.json 一次完整。
        """
        # 旧 submit = arrive + collect 串行（三栅栏拆分后，night_cb 分别调 arrive/collect，
        # 但 submit 保留给旧测试/未拆分调用点）。done 语义已是「全员到齐」，arrive 起后台聚合。
        await self.arrive(arm_key, window, at_day)
        return await self.collect(arm_key, window)

    async def arrive(self, arm_key: str, window: int, at_day: int) -> None:
        """栅栏1：到达窗口边界、**等齐**（最后一个到者起后台聚合），放行进阶段2（不等聚合）。

        ★ 2026-08-29 三栅栏拆分：arrive 只等齐 + leader 起聚合 future，**不等聚合结果**——
          聚合与阶段2 并行。聚合结果在 collect（栅栏3）取。arrive 不抛业务异常（聚合在后台，
          还没/正在跑）。
        """
        gate = self._gates.get(window)
        if gate is None:
            gate = _WindowGate()
            self._gates[window] = gate
        self._pending.setdefault(window, {})[arm_key] = None  # 占位：快照路径由 leader 填
        self._lag.setdefault(window, {})[arm_key] = at_day
        self.max_window = max(self.max_window, int(window))

        waiting = self._active - set(self._pending[window])
        if waiting:
            # 还没攒齐还在供货的副脑 → 阻塞等齐（done=全员到齐），不等聚合
            logger.info("[窗口%d] %s 第%d天到达，已收 %d/%d 份，等：%s",
                        window, self._display.get(arm_key, arm_key), at_day,
                        len(self._pending[window]),
                        len(self._active) or len(self._pending[window]),
                        [self._display.get(a, a) for a in sorted(waiting)])
            await gate.done.wait()
            return

        # 攒齐了 → 本副脑是 leader，起后台聚合（不 await，存进 gate.result），set done 放行 followers
        snaps = self._pending.pop(window)
        lag = self._lag.pop(window, {})
        absent = [a for a in self._arm_graphs if a not in snaps]
        logger.info("[窗口%d] %s 第%d天最后到达，起后台聚合（%d 份），放行进阶段2",
                    window, self._display.get(arm_key, arm_key), at_day, len(snaps))
        gate.result = asyncio.ensure_future(
            self._run_window_sync(window, snaps, lag, absent))
        self.n_windows_fired += 1
        gate.done.set()
        # gate 留着：collect 要 await gate.result；collect 后才 pop（见 collect）

    async def collect(self, arm_key: str, window: int) -> dict:
        """栅栏3：等聚合结果（栅栏1 起的后台 future），返回 cleared。失败抛 WindowAggregationError。

        ★ 必须在 arrive 之后调。聚合 future 由 leader 在 arrive 起好，这里 await 它。
          失败（future 带异常）→ 抛 WindowAggregationError（abort batch，此处抛、不被 night_cb 吞）。
        ★ **不 pop gate**：leader 先 collect 完会 pop 掉 gate，follower 后 collect 时 gate 不在
          就取不到 future → 误返回空计数（实测 test_multi_arm_failure_aborts_all_arms 踩到）。
          future 完成后所有 await 它的协程都拿到同一结果/异常，gate 留着无副作用（每窗口一个，
          极小）；drain 末尾 _tasks.clear 不涉及 _gates，_gates 在下个 batch 自然重建。
        """
        gate = self._gates.get(window)
        if gate is None or gate.result is None:
            # gate 未建/未起 future（arrive 未被调到 leader 分支，如非聚合天数）→ 无聚合，空计数
            return {"concepts": 0, "facets": 0, "edges": 0, "vectors": 0}
        try:
            cleared = await gate.result
        except WindowAggregationError:
            # _run_window_sync 失败已包成 WindowAggregationError，直接抛（abort batch）
            raise
        except BaseException as exc:  # pylint: disable=broad-except
            raise WindowAggregationError(
                f"窗口{window}聚合失败，abort batch") from exc
        return cleared.get(arm_key, {
            "concepts": 0, "facets": 0, "edges": 0, "vectors": 0})


    def retire(self, arm_key: str) -> None:
        """某副脑不再供货（rollout 正常结束或异常退出）。

        ★ **必须调**：栅栏靠「攒齐还在供货的副脑」才发车，一个已经退场的副脑若还留在名单里，
        它欠的那一份永远补不上 → 后续窗口**永久卡住**，而且完全静默。退场后立即重查所有挂着的
        窗口 —— 它们可能因为分母变小而当场就齐了（leader 在 submit 里自然当选）。
        """
        if arm_key not in self._active:
            return
        self._active.discard(arm_key)
        logger.info("[窗口] %s 退场（不再供货），剩 %d 个副脑在跑",
                    self._display.get(arm_key, arm_key), len(self._active))
        # 重查挂着的窗口：分母变小可能当场就齐。但 leader 选举发生在 submit 协程里——
        # 退场的副脑不会再调 submit，故只需让「剩下的副脑都已到达」的窗口被发车。
        # 这里没有协程可调度（retire 是同步的），发车由下一个到达的 submit 或 drain 触发。
        for w in sorted(self._pending):
            self._maybe_complete(w)

    def _maybe_complete(self, window: int) -> None:
        """retire 后若本窗口还在供货的副脑都已到达，**立刻**起 leader 任务发车（不等 drain）。

        为什么不等 drain：副脑中途退场时，别的副脑还**在跑**、正阻塞在这个窗口的栅栏上等这批
        公共经验。拖到 batch 末（drain）才聚合 = 这一窗口的公共经验这一整段都查不到，等于白开。
        故 retire 检测到「齐了」（分母因退场变小）时，立刻 `create_task` 一个 leader 协程跑
        `_run_window_sync` 并 set gate.done 唤醒等待者 —— 与「最后一个到达者当 leader」同一条路径。
        """
        got = self._pending.get(window) or {}
        waiting = self._active - set(got)
        if waiting or not got:
            return
        # 齐了且没有活跃 submit 协程会自然当选 leader → 起后台聚合 future 并 set done 放行 followers。
        snaps = self._pending.pop(window)
        lag = self._lag.pop(window, {})
        absent = [a for a in self._arm_graphs if a not in snaps]
        gate = self._gates.get(window)
        logger.info("[窗口%d] retire 后已攒齐 %d 份，起后台聚合、放行 followers",
                    window, len(snaps))
        self.n_windows_fired += 1
        if gate is not None:
            # 与 arrive 的 leader 分支同：起 future、set done 放行进阶段2，collect 取 result。
            gate.result = asyncio.ensure_future(
                self._run_window_sync(window, snaps, lag, absent))
            gate.done.set()
        else:
            # 无 gate（不应发生，retire 前 arrive 应已建 gate）→ 后台跑掉，结果丢弃
            self._tasks.append(asyncio.create_task(
                self._run_window_sync(window, snaps, lag, absent)))

    # ---- 消费侧 ----

    async def _run_window_sync(self, window: int, arms_present: dict[str, None],
                               lag: dict[str, int], absent: list[str]) -> dict[str, dict]:
        """leader 跑一个窗口的聚合：**快照 → 聚合 → 成功才清层**，返回逐 arm 清层计数。

        ★ 与旧 `_run_window`（后台、失败返 None）的三点不同（2026-08-19 栅栏同步）：
          ① **清层在聚合成功之后**：旧版快照后立刻清、失败就丢经验；新版失败时经验层保留、
             可复盘（abort batch 也不丢数据）；
          ② **失败抛 `WindowAggregationError`**（不再 return None）：由 submit 的 leader
             分支捕获、经 gate 唤醒 followers 一并抛 → abort 整个 batch；
          ③ **快照对活库就地做**（leader 持有各 ArmGraph 引用），不再依赖 submit 预先填路径。

        `arms_present` 的 key = 已到达本窗口的副脑（value 占位 None，仅为复用旧签名形状）。
        """
        # ★ **对照组 no-op 短路**（no_graph）：primary/arm_graphs 都是 NullGraph，没有 _conn /
        #   consolidate / snapshot_to / snapshot_with_vectors —— 走真聚合会 AttributeError。
        #   对照组本就不该聚合（无图谱可归并），这里记一条 no-op 留痕、清层计数全 0 后直接返回。
        #   world checkpoint + progress.json 由域侧 night_cb 写（续跑靠它们），不在这里管。
        #   标 ablation=no_graph 进留痕，读日志的人不会把这条误读成"实验组聚合失败"。
        # ★ **消融 A 无共享 no-op 短路**（no_aggregate，2026-09-15）：副脑是真 ArmGraph、主脑是
        #   NullGraph。聚合要读主脑 consolidate（NullGraph 无此方法）且 A 本就不该聚合（共享图恒空、
        #   检索只见自己的图）。复用同一条 no-op 短路：不快照、不清层、不写主脑快照——经验层满
        #   30 夜不清正是 A 的特性。ablation 标 no_aggregate 与 no_graph 区分。
        if self._no_graph or self._no_aggregate:
            abl = "no_graph" if self._no_graph else "no_aggregate"
            self._records.append({
                "epoch": self._epoch, "batch": self._batch, "window": window,
                "ablation": abl, "consolidated": False,
                "note": "对照组无图谱聚合（no-op）" if self._no_graph
                        else "消融A无共享：聚合 no-op（经验层保留不清）",
                "arms": list(arms_present), "lag_days": dict(lag), "absent": list(absent),
            })
            logger.info("[窗口%d] %s no-op 聚合（%d 份到达）—— 不归并、不快照图谱%s",
                        window, "对照组" if self._no_graph else "消融A无共享",
                        len(arms_present),
                        "（经验层保留不清）" if self._no_aggregate else "")
            return {a: {"concepts": 0, "facets": 0, "edges": 0} for a in arms_present}

        snaps: dict[str, Path] = {}
        graphs: dict[str, BrainGraph] = {}
        try:
            # ① 快照：对各活库就地 `snapshot_to`（各 ArmGraph 自带连接+锁，leader 可直接调）。
            for arm_key in arms_present:
                g = self._arm_graphs[arm_key]
                if self._checkpoint_dir is not None:
                    snap = (self._checkpoint_dir / f"checkpoint{window}"
                            / f"arm_{arm_key}" / "snap.db")
                    snap.parent.mkdir(parents=True, exist_ok=True)
                else:
                    snap = (self._snap_dir
                            / f"arm_{arm_key}_e{self._epoch}b{self._batch}_w{window}.db")
                g.snapshot_to(snap)
                snaps[arm_key] = snap

            # ② 打开快照供聚合读（只读用途，仍用 ArmGraph 保持层判定口径一致，见旧注释）。
            # ★ 消融 ablation 透传（2026-09-18）：快照 ArmGraph 继承活图的 _ablation——
            #   consolidate._warn_layer_violations 读它抑制 no_exp_text 下「经验层为 0」的
            #   每窗必现噪音（该 rung 经验层恒空是设计，非反思失败）。
            for k, p in snaps.items():
                graphs[k] = ArmGraph(p,
                                     vec_db_path=p.with_name(p.stem + "_vec.db"),
                                     model_name=None,
                                     retrieval=self._primary._retrieval,  # noqa: SLF001
                                     context_dir=self._primary._context_dir,  # noqa: SLF001
                                     ablation=self._ablation_of(k))
            # ★ isolated_scope（非 log_scope）：聚合被 leader 副脑的 submit 直接 `await`，
            #   调用栈外层是 leader 的 `log_scope("e1b1 bob #1")`。用 log_scope 会 append 出
            #   `[e1b1 bob #1 e1b1w1 aggr]` —— 归属错挂到 leader 头上，且谁最后到齐就挂谁。
            #   聚合不属于任何副脑，从干净起点设 `e1b1w1 aggr`。retire 后台 leader 路径同理。
            #   详见 openkage.logctx.isolated_scope 的 docstring。
            with isolated_scope(f"e{self._epoch}b{self._batch}w{window} aggr"):
                rec = await _consolidate(
                    self._primary, graphs, self._display,
                    self._epoch, self._batch, window,
                    lag_days=lag, absent_arms=absent,
                )

            # ③ **聚合成功才清层**（失败的话这里到不了，异常已在下面 except 抛出）。
            cleared: dict[str, dict] = {}
            for arm_key in arms_present:
                g = self._arm_graphs[arm_key]
                c = g.clear_layer(LAYER_EXPERIENCE)
                # ★ 技能归属重构（2026-08-27）：技能存各臂常驻 SkillStore，跨窗口存活，
                #   聚合不再清技能（谁写的谁维护，不再上交公有脑）。
                cleared[arm_key] = {**c}
                logger.info(
                    "[窗口%d] %s 第%d天：聚合成功 → 清经验层（%d概念/%d facet/%d边）"
                    "（记忆层保留；技能跨窗口常驻 SkillStore）",
                    window, self._display.get(arm_key, arm_key), lag.get(arm_key, 0),
                    c["concepts"], c["facets"], c["edges"],
                )

            # ④ 主脑历史快照（持续学习指标要回看各窗口主脑状态）。
            if self._checkpoint_dir is not None:
                snap_path = self._checkpoint_dir / f"checkpoint{window}" / "public.db"
                snap_path.parent.mkdir(parents=True, exist_ok=True)
            else:
                snap_path = (self._snap_dir
                             / f"brain_e{self._epoch}b{self._batch}_w{window}.db")
            try:
                _, vec = self._primary.snapshot_with_vectors(snap_path)
                logger.info("[窗口%d] 主脑快照 → %s%s", window,
                            snap_path.name if self._checkpoint_dir is None
                            else snap_path.relative_to(self._checkpoint_dir),
                            "" if vec else "（⚠️ 无向量库，事后只能看结构、无法检索）")
            except Exception:  # pylint: disable=broad-except
                logger.exception("[窗口%d] 主脑快照失败（聚合本身已成功，训练继续）", window)

            self._records.append(rec)
            return cleared
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pylint: disable=broad-except
            # ★ 聚合失败 → 抛 WindowAggregationError（abort batch）。此时清层未执行，
            #   各副脑经验层**完整保留**（与旧版"已清、无法重放"相反）—— 可事后手工聚合或复盘。
            logger.exception(
                "[窗口%d] 聚合失败 —— **abort 整个 batch**。各副脑经验层未清、完整保留"
                "（快照已在 %s），可事后手工聚合",
                window,
                (self._checkpoint_dir / f"checkpoint{window}") if self._checkpoint_dir
                else self._snap_dir,
            )
            raise WindowAggregationError(
                f"窗口{window}聚合失败：{exc}") from exc
        finally:
            for g in graphs.values():
                try:
                    g.close()
                except Exception:  # pylint: disable=broad-except
                    pass

    async def drain(self) -> list[dict]:
        """收尾：把「有副脑到达但未成窗」的末窗口按现有份数发车，返回本 batch 全部留痕。

        ★ 2026-08-19 栅栏同步后语义变了：submit 返回即聚合完，故正常窗口的留痕已在
        `_records` 里；drain 只剩一种情况要处理 —— **末窗口部分供货**：各副脑天数不同步，
        batch 末某些副脑已到达第 K 个窗口边界、另一些没到就退场了，这个窗口从未攒齐、
        没 leader 跑过。这里**以现有份数**当 leader 跑一次聚合（与旧版 drain 同语义）。

        失败处理同样反转：旧版 `return_exceptions=True` 把异常压成 None、训练继续；新版
        聚合失败抛 `WindowAggregationError` —— 但 batch 末各副脑 rollout 都已结束，abort
        已无意义（没有在跑的轨迹可中止），故 drain 这里**catch 住、loud 告警、不抛**，
        让 brain.py 能正常写曲线点。这与题内 submit 的 abort 语义不冲突：题内失败 abort
        的是「还在跑的 batch」，batch 末已无在跑的轨迹。
        """
        for w in sorted(self._pending):
            got = self._pending.get(w) or {}
            if not got:
                continue
            logger.info("[窗口%d] batch 末仍有 %d 份未成窗，按现有份数发车", w, len(got))
            snaps = self._pending.pop(w)
            lag = self._lag.pop(w, {})
            absent = [a for a in self._arm_graphs if a not in snaps]
            gate = self._gates.pop(w, None)
            try:
                await self._run_window_sync(w, snaps, lag, absent)
            except WindowAggregationError:
                # batch 末聚合失败：无在跑轨迹可 abort，loud 告警后放行（留痕不进列表）。
                logger.error("[窗口%d] batch 末聚合失败 —— 经验层未清、保留可复盘；"
                             "本窗口留痕不进曲线", w)
            else:
                # 唤醒可能仍阻塞在这个 gate 上的 followers（若有）。
                if gate is not None:
                    gate.done.set()
        self._tasks.clear()  # 栅栏同步下应为空，保留清空动作防残留
        out = self._records
        self._records = []
        return out


class NightBarrier:
    """每晚三栅栏：各臂同一天的同阶段反思都跑完，才一起进下一阶段/下一天（2026-08-29）。

    解决跨臂技能库竞态：`_inject_all_skills` 每天 createplan 查 `SkillRegistry.list_all_verified()`
    读各臂 SkillStore 当前状态；各臂 rollout 并行、天数进度独立 → 一臂改技能时另一臂可能还在用旧。
    三栅栏（key = `(stage, day_idx)`，stage=1/2/3）精确同步：
    - **栅栏1**（阶段1 后）：所有臂经验/记忆反思完 → 一起进阶段2（保证阶段2 看到当天回填的计数）。
    - **栅栏2**（阶段2 后）：所有臂技能维护完 → 技能库稳定（下一天 createplan 读到一致的库）。
    - **栅栏3**（仅聚合天数，在 collect 之后）：由 WindowAggregator.collect 担任，不归本类。
      本类只管栅栏1/2（纯等齐）。

    与 WindowAggregator 的区别：**只等齐，不聚合**。无 LLM/快照/清层 → submit 纯 Event 等待，
    不抛业务异常（被 env.py 的 except 吞也不影响——它本来就无异常可抛）。

    ★ `_active` 含**所有臂**（含 no_graph 对照组）：对照组 NullSkillStore 无技能，但其 rollout
      仍在跑轨迹、每晚到 night_cb。不参与会致别的臂在栅栏永久等它 → 死锁。
    ★ key 用 `(stage, day_idx)` 元组：三阶段共用一个 barrier 实例，不同阶段/不同天各自独立 gate。
    """
    def __init__(self, arm_keys) -> None:
        self._active: set[str] = set(arm_keys)            # 含 no_graph 对照组
        self._arrived: dict[tuple[int, int], set[str]] = {}   # {(stage,day): 已到达臂}
        self._gates: dict[tuple[int, int], asyncio.Event] = {}  # {(stage,day): 闸门}

    async def submit(self, arm_key: str, stage: int, day_idx: int) -> None:
        """某臂到达 (stage, day_idx) 栅栏：等齐所有还在供货的臂，最后一个 set done 放行。

        纯 Event 等待，不抛业务异常。arm 退场（retire）缩小分母，挂着的 gate 可能当场放行。
        """
        key = (stage, day_idx)
        gate = self._gates.get(key)
        if gate is None:
            gate = asyncio.Event()
            self._gates[key] = gate
        arrived = self._arrived.setdefault(key, set())
        arrived.add(arm_key)
        waiting = self._active - arrived
        if waiting:
            logger.info("[每晚栅栏%d] %s 第%d天到达，已收 %d/%d，等：%s",
                        stage, arm_key, day_idx, len(arrived), len(self._active),
                        sorted(waiting))
            await gate.wait()
            return
        logger.info("[每晚栅栏%d] 第%d天全员到齐（%d臂），放行", stage, day_idx, len(arrived))
        gate.set()
        self._arrived.pop(key, None)
        self._gates.pop(key, None)

    def retire(self, arm_key: str) -> None:
        """某臂不再供货（rollout 结束/异常）。★ 必须 finally 调，否则它欠的份额让栅栏永久卡死。

        退场后重检所有挂着的 gate：分母变小可能当场就齐 → set done 放行等待者。
        无聚合故直接同步 set done（不像 WindowAggregator 要 create_task 后台 leader）。
        """
        if arm_key not in self._active:
            return
        self._active.discard(arm_key)
        logger.info("[每晚栅栏] %s 退场，剩 %d 臂在跑", arm_key, len(self._active))
        for key in sorted(self._arrived):
            self._maybe_release(key)

    def _maybe_release(self, key: tuple[int, int]) -> None:
        """retire 后若本 gate 还在供货的臂都已到达，立刻 set done 放行。"""
        arrived = self._arrived.get(key)
        if not arrived or (self._active - arrived):
            return
        gate = self._gates.get(key)
        if gate is not None and not gate.is_set():
            logger.info("[每晚栅栏%d] 第%d天 retire 后已攒齐（%d臂），放行",
                        key[0], key[1], len(arrived))
            gate.set()
        self._arrived.pop(key, None)
        self._gates.pop(key, None)


class SpawnBarrier:
    """开局出生栅栏：所有臂 bot 都出生（+附身尝试）后才一起迈第一步训练（2026-09-12）。

    解决「各臂不等齐就开训」：5 臂经 brain.py 的 gather 并发跑 rollout，谁先录像就绪谁先进
    run_exploration 出生开训——录像客户端卡死的臂（如 bob 渲染线程挂起）经 recorder.start 的
    150s 超时降级为「不录制继续」后，晚 150s 才出生，出现「4 臂已开训、1 臂没出生」。
    本栅栏在「出生后、第一步前」同步：先到的等，最后一个到齐放行。**录像降级继续训练**这一
    行为不变——卡死的臂晚点无录制出生，照样到栅栏、照样跑，只是全员等它降级完一起开始。

    与 NightBarrier 的区别（故不复用它）：
    - **无 (stage, day_idx) 维度**：出生是开局一次性事件，单 gate 而非按 (stage,day) 分的字典。
    - **无 retire**：NightBarrier 必须 retire（每晚重复栅栏，臂退场后它欠的份额让别的臂永久等）；
      本栅栏是开局一次性，能到 submit() 的臂都已出生。**不 abort 录像卡死**（用户定：降级继续），
      故录像卡死的臂降级后也会到栅栏——只是晚最多 150s。唯一「到不了栅栏」的是 rollout 真正
      崩溃（非录像卡死），那种情况触发 brain.py gather cancel，等其他臂的 gate.wait() 被
      CancelledError 唤醒退出，不会永久卡死。故 retire 在这里是死代码——run_arm_batch 的
      finally 刻意不调。
    - **纯 Event 等齐无超时**：不 abort 就没有「超时该干啥」——超时只能放行（破坏「人齐才开始」）
      或继续等（=无超时）。用户选纯等齐：录像卡死时全员最多白等 150s，换「人齐才开始」的保证。

    ★ `_active` 含**所有臂**（含 no_graph 对照组，同 NightBarrier）：对照组 rollout 仍出生、
      到栅栏，不纳入会让实验臂在栅栏永久等它。
    """
    def __init__(self, arm_keys) -> None:
        self._active: set[str] = set(arm_keys)   # 含 no_graph 对照组
        self._arrived: set[str] = set()
        self._gate = asyncio.Event()

    async def submit(self, arm_key: str) -> None:
        """某臂出生完成：等齐所有臂，最后一个 set done 放行。

        纯 Event 等齐，不抛业务异常（被 env.py 的 except 吞也不影响——本就无异常可抛）。
        on_spawn_ready=None（eval/单臂）时根本不会调本方法（run_exploration 内 None 守卫）。
        单臂时 waiting 为空，立即 set done 放行，无阻塞。
        """
        self._arrived.add(arm_key)
        waiting = self._active - self._arrived
        if waiting:
            logger.info("[出生栅栏] %s 出生完成，已收 %d/%d，等：%s",
                        arm_key, len(self._arrived), len(self._active), sorted(waiting))
            await self._gate.wait()
            return
        logger.info("[出生栅栏] 全员到齐（%d臂），放行迈第一步", len(self._arrived))
        self._gate.set()


