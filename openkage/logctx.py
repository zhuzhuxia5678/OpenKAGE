"""日志作用域：让每一条日志都自带「这是谁在跑」的标签。

用 contextvar 存当前作用域标签，一个自定义 Formatter 在输出时读出来拼到行首，于是调用栈里
任意深度、任意模块（含第三方库）的日志都自动带上归属，零侵入：

    with log_scope("base frozen"):   # loop._eval_one_group
        ...                          # 这之下所有日志都带 [base frozen]

contextvar 的两条传播语义：
- `asyncio.gather` 把每个协程包成 Task → 各自 copy 一份 context，并行的 arm/采样天然互不串味；
- 裸 `await`（单 bot 串行的 arm 路径）共享调用方 context → 靠 `log_scope` 的
  `finally: reset` 收尾，不会泄漏给下一个 arm。

选 Formatter 而不是 logging.Filter：Filter 方案要把 `scope` 写进 record、formatter 里引用
`%(scope)s`，一旦有 handler 没挂上这个 filter，格式化就会因缺字段整条报错；Formatter 自己算
自己用，不会缺字段。

约定：
- 作用域可嵌套，内层自动追加在外层后面：`e1b1 p2` + `#3` → `[e1b1 p2 #3]`。
- 标签求短且不含中文：出现在每一行行首，长了会把正文挤到右边；中文在终端占两列，会让
  ljust 对齐失效（所以是 `reflect` 而不是 `反思`）。
- 本模块只产出/渲染作用域；把 Formatter 装到 handler 上是 runner 的关切
  （见 experiments/run.py 的 setup_logging），日志配置不进可复用框架。
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
import unicodedata

#: 当前日志作用域（形如 "e1b1 p2 #3"）。空串 = 无归属（框架启动/收尾阶段）。
_scope: contextvars.ContextVar[str] = contextvars.ContextVar("openkage_log_scope", default="")

#: scope 列的显示宽度：定宽才能让 message 正文对齐成列，扫日志时眼睛不用左右跳。
#: 15 覆盖实际最长的标签（`e1b1 a1 reflect`）。**超宽不截断** —— 宁可某几行错位，也不要把归属
#: 信息截没了；但无论如何都保证列末至少留一个空格，否则超宽标签会跟后面的时间戳粘成一团。
SCOPE_WIDTH = 15


@contextlib.contextmanager
def log_scope(tag: str):
    """把 `tag` 追加进当前日志作用域，退出时恢复（异常路径也恢复，故用 try/finally）。

    嵌套即拼接：外层 `log_scope("e1b1 p2")` 内再 `log_scope("#3")` → `[e1b1 p2 #3]`。
    空 tag 直接放行（调用方不必自己判空）。
    """
    if not tag:
        yield
        return
    current = _scope.get()
    token = _scope.set(f"{current} {tag}" if current else tag)
    try:
        yield
    finally:
        _scope.reset(token)


@contextlib.contextmanager
def isolated_scope(tag: str):
    """把作用域**重置成** `tag`（不 append 到外层），退出时恢复原值。

    与 `log_scope` 的区别：`log_scope` 在当前 scope 后**追加**（嵌套拼接，归属逐层累积）；
    `isolated_scope` 从**干净起点**（`""`）设 `tag`，不管外层 scope 是什么 —— 给「本段逻辑
    不属于当前调用栈归属」的场景用。

    典型场景：窗口聚合的 `_run_window_sync` 被 leader 副脑的 `submit` 直接 `await`（栅栏
    同步，2026-08-19），调用栈外层是 leader 副脑的 `log_scope("e1b1 bob #1")`。若用
    `log_scope("e1b1w1 aggr")`，append 出来是 `[e1b1 bob #1 e1b1w1 aggr]` —— 归属错挂到
    leader 副脑头上，且谁最后到齐就挂谁，不确定。聚合不属于任何副脑，它的日志头就该干净地
    只有 `[e1b1w1 aggr]`。retire 后台 leader 路径同理（`create_task` 继承退场触发者的 scope）。

    比「在每个调用点用 `neutral_context()` 包 `create_task`」好在哪：① submit 的 leader
    是裸 `await`（共享调用方 context，`create_task` 的 `context=` 参数够不着它），只能从
    被调方内部隔离；② 一处覆盖所有调用路径（submit leader / retire 后台 leader / 未来新增），
    新调用点不会再漏。
    """
    token = _scope.set(tag)
    try:
        yield
    finally:
        _scope.reset(token)


def current_scope() -> str:
    """当前作用域字符串（无则空串）。给需要把归属写进结果 JSON / 异常消息的地方用。"""
    return _scope.get()


def neutral_context() -> contextvars.Context:
    """返回一份**作用域已清空**的 context，给「不该带任何副脑归属」的后台任务用。

    用法：``asyncio.create_task(coro, context=neutral_context())``。

    为什么需要它：``create_task`` 会复制当前 contextvars 快照进新任务。若发起处正处在某个
    副脑的 ``log_scope`` 内（如窗口聚合在最后一个交快照的副脑调用栈里触发），后台任务就会
    继承那个副脑的归属标签，再嵌套自己的 ``log_scope`` 就拼成 ``[e1b1 bob #1 e1b1w1 aggr]``
    —— 归属错挂且不确定（谁最后交快照就挂谁）。

    fork 出来再 ``_scope.set("")`` 到中性态（default），后台任务出生即无归属，内部再
    ``log_scope`` 从干净起点拼接。与 ``env.py`` 里 ``copy_context()`` **保留**副脑归属的
    用法对偶——那里要带（工作线程里的日志得认得出是谁）、这里要丢（聚合不属于任何副脑），
    两种都是「fork 后再调一个操作」。

    用公开函数而非让调用方自己 ``_scope.set``：``_scope`` 是本模块私有存储，存储方式将来若
    变（换成栈、换成 dataclass），调用方不该连带改。
    """
    ctx = contextvars.copy_context()
    ctx.run(_scope.set, "")
    return ctx


def arm_log_ids(keys: list[str]) -> dict[str, str]:
    """给一组副脑 key 算出**短显示 id**，用于日志作用域：{key: short}。

    取每个 key 按 `_` 分割后的第一段当短名，避免长 key 把每行行首占用过多列。

    安全阀：只有**取完前缀仍然两两不同**时才用短名；否则（如 `scout_a`/`scout_b` 都坍缩成
    `scout`）整组退回完整 key —— 日志宽一点无所谓，认错人是不能接受的。
    """
    shorts = [k.split("_")[0] for k in keys]
    if len(set(shorts)) == len(shorts) and all(shorts):
        return dict(zip(keys, shorts))
    return {k: k for k in keys}


class ScopeFormatter(logging.Formatter):
    """在**行首**插入定宽的 `[scope]` 列，然后才是常规的时间戳/级别/模块名/正文。

    为什么放行首而不是插在模块名后面：`%(name)s` 的宽度差异很大（`httpx` vs
    `openkage.memory.vector_store`），插在它后面每行位置都不同，就失去了「一眼扫一列」的意义。
    放行首才能对齐成列，而这正是要解决的问题 —— 看一眼行首就知道这条是谁输出的。

    多行 message（如启动门面的 ASCII 图案、异常 traceback）只有第一行带前缀 —— 这是 logging 的
    既有行为，图案因此不会被逐行插入标签打散。
    """

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        scope = _scope.get()
        if not scope:
            return f"{' ' * (SCOPE_WIDTH + 2)} {base}"
        # 末尾那个 " " 不能省：标签超过 SCOPE_WIDTH 时 ljust 不再补位，会跟时间戳粘在一起。
        return f"{f'[{scope}]'.ljust(SCOPE_WIDTH + 2)} {base}"


def _display_width(s: str) -> int:
    """字符串在终端占的**列数**（不是字符数）：CJK/全角算 2 列。

    `len()` 会把中文当 1 列，导致含中文的分隔线右侧 ═ 补少了一半 —— 几条线长短不一反而更乱。
    """
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in s)


def log_banner(logger: logging.Logger, title: str, *, width: int = 72) -> None:
    """打一条阶段分隔线，把 frozen 基线 / 各训练轮 / evolving 评测在视觉上切开。"""
    head = f"{'═' * 4} {title} "
    logger.info("%s", head + "═" * max(0, width - _display_width(head)))
