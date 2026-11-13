"""consolidation round 号的编码/解码 —— **唯一真相**，编码与解码不许各写一份。

`concept/facet/edge.created_at` / `updated_at` 在**主脑**侧装的是 consolidation round 号
（副脑侧装的是「第几天」，刻度由写入方定，见 `BrainGraph.reflect` 的 `at` 参数）。
这个 round 号是把 `(epoch, batch, window)` 三个维度压进一个整数的复合编码。

为什么需要本模块：编码此前散在 `brain.py` / `consolidate.py`（`epoch*100+batch`），
解码在 `viewer/server.py`（`divmod(r, 100)`）—— 同一个约定存了两份、还隔着两个包。
加第三个维度时两份必然漂移，而漂移的后果是**viewer 把 round 显示成怪值却不报错**。
故编码一律走 `encode()`、解码一律走 `decode()`/`fmt()`，谁都不要自己写乘法。

本模块**自身零依赖**（连 logging 都不 import）：viewer 要 import 它，不该为了解一个整数而把
框架实现拖进去。⚠️ 但 `import openkage.roundcode` 仍会执行 `openkage/__init__.py`，连带载入
brain/memory.graph —— **无副作用**（embedder/sqlite-vec 都是 BrainGraph 构造时才初始化，
不在 import 时），只是多载几个模块。真要完全隔离得按文件路径直接 load，不值得。

术语（全套约定见 `openkage/brain.py` 顶部）：
    epoch（轮）    全部训练题跑一遍 —— 学习曲线的 X 轴
    batch（批）    一个 epoch 内题目的分片 —— **题间**聚合
    window（窗口） **一条轨迹内**的一个聚合窗口 —— **题内**聚合（MC = 每 K 夜一次）

不变式：**1 次 consolidation = 1 个 (epoch, batch, window)**。
"""

from __future__ import annotations

#: 每个维度的进位基数。三段各留两位十进制 → `E*10000 + B*100 + W`，
#: 于是 round 号肉眼可读（10203 = 第1轮第2批第3窗口），这是选 100 而非位移的唯一理由。
BASE = 100

#: window 的起始值。**从 1 起**：`aggregate_every=0`（不做题内聚合）时轨迹只有末尾那一次
#: 聚合，它就是 window 1 —— 于是"没开题内聚合"不需要一个特殊的哨兵值（0 或 None），
#: 少一个分支就少一处会写错的地方。
FIRST_WINDOW = 1


def encode(epoch: int, batch: int, window: int = FIRST_WINDOW) -> int:
    """把 `(epoch, batch, window)` 压成 round 号。三段都必须在 [0, BASE)，越界直接报错。

    ⚠️ 越界**报错而不是取模**：静默取模会让第 100 批和第 0 批写出同一个 round 号，
    于是 viewer 上两次完全不同的聚合叠在一起，而没有任何地方报错。
    真撞上（>99 个窗口）该改的是 BASE，不是让它绕回去。
    """
    for name, v in (("epoch", epoch), ("batch", batch), ("window", window)):
        if not 0 <= int(v) < BASE:
            raise ValueError(
                f"round 编码的 {name}={v} 超出 [0,{BASE}) —— 三段各占两位十进制。"
                f"若确实需要更多（如 >99 个题内聚合窗口），请改 openkage.roundcode.BASE，"
                f"不要让它静默绕回去（那会让两次不同的聚合写出同一个 round 号）。"
            )
    return int(epoch) * BASE * BASE + int(batch) * BASE + int(window)


def decode(round_num: int) -> tuple[int, int, int]:
    """round 号 → `(epoch, batch, window)`。`encode` 的逆。

    **兼容两段编码的老库**：2026-08-09 之前 round = `epoch*100+batch`（101 = 第1轮第1批）。
    那些值 < 10000，解出来的 epoch 会是 0/1 这种小数 —— 见 `fmt` 对它的处理。
    """
    epoch, rest = divmod(int(round_num), BASE * BASE)
    batch, window = divmod(rest, BASE)
    return epoch, batch, window


def fmt(round_num: int) -> str:
    """round 号 → 人话，如 `第1轮第2批第3窗口`。给日志与 viewer 用。

    直接显示 `第10203轮` 会让人以为跑了上万轮，所以一律反解。

    **老库（两段编码）**：`round_num < BASE*BASE` 时它是 `epoch*100+batch`，
    此时 `decode` 的 epoch 段恒为 0。按老格式渲染成 `第N轮第M批`（不带窗口）——
    不加窗口段是因为那些行确实没有窗口维度，凭空补一个 `第0窗口` 是在显示一个不存在的事实。
    """
    if int(round_num) < BASE * BASE:
        epoch, batch = divmod(int(round_num), BASE)
        return f"第{epoch}轮第{batch}批"
    epoch, batch, window = decode(round_num)
    return f"第{epoch}轮第{batch}批第{window}窗口"
