"""adapted Voyager 基线（外部基线，非消融档）。

Voyager（Wang et al. 2023, arXiv:2305.16291）四模块——自动课程 / 代码生成（迭代提示）/
自验证 / 技能库——在本仓 MC 环境上的**受控适配**实现。设计定案见
`introduction-voyager-discussion-log.md` §5 与 plans/baseline_voyager_20261001.md（若有）。

定位：**外部方法基线**，不拆除 OpenKAGE 的任何机制，而是把另一套完整的
「个体自主探索 + 个体技能库」系统放进同一张床：
  - 同一 MC server / 难度 / 种子 / 出生点流 / 昼夜节律 / 指标采集器（env.run_exploration 全复用）；
  - 同一 LLM 网关与骨干（get_llm，per-call temperature）；
  - 同一 KaLM 嵌入器（技能检索，openkage.memory.skill_store.SkillStore 只读复用）。

「孤岛群」：N 个 Voyager 智能体并行、各技能库各课程进度、**零共享**——对应讨论日志 §5.3
的比较单位（N 孤岛 vs N 臂群体，成员数相同，支持「任一成员达成」口径）。

与 OpenKAGE 各臂的差异只在**智能体层**：无夜反思/无记忆图谱/无聚合/无跨臂技能——
Voyager 的跨天记忆 = 技能库 + 完成/失败任务清单（原文语义）。

零 openkage 写依赖：本包只 import openkage 的 LLM/技能库/工具函数，不写任何图谱。
入口见 experiments/run_voyager.py。
"""
