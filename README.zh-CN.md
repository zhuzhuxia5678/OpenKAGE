# OpenKAGE

**Open** **K**nowledge-sharing **A**gent-**G**roup **E**volution —— 一个免训练框架：一组 LLM 智能体在开放世界探索中持续积累、共享并复用知识。

[English](README.md)

这是 ICLR 2027 投稿期间核心 OpenKAGE 代码的**匿名公开仓库**。作者姓名、单位与引用信息在评审期有意省略。仓库包含框架本体、两个外部基线的复现实现、提示词、配置、指标与绘图脚本、图数据、重绘结果图，以及只读可视化前端。

**本仓库不包含实验运行记录。** 运行记录作为单独的匿名归档发布，用于复现；要使用指标 CLI、绘图脚本或可视化网页，请先解包该归档，使 `results/` 目录与仓库内的代码目录同级。跑新实验需要自备 OpenAI 兼容的 LLM 端点和 Minecraft Java 服务端。

## 目录

- [什么是 OpenKAGE](#什么是-openkage)
- [特性](#特性)
- [演示动图](#演示动图)
- [可视化界面截图](#可视化界面截图)
- [仓库结构](#仓库结构)
- [安装](#安装)
- [快速开始](#快速开始)
- [自己跑实验](#自己跑实验)
- [运行记录](#运行记录)
- [复算论文指标](#复算论文指标)
- [复现论文图](#复现论文图)
- [配置](#配置)
- [说明](#说明)
- [许可证与引用](#许可证与引用)

## 什么是 OpenKAGE

OpenKAGE 研究 LLM 智能体在 Minecraft 中的开放式长程学习。智能体从空知识库起步，每个游戏日自行提出并修订目标，并把学到的东西写进知识库。使这些知识对整个群体都有用的，是两点设计：

1. **双层知识图。** *记忆层*保存与情境绑定的个体记忆；*经验层*保存可迁移的经验和可执行技能，以及它们的适用条件。意图感知子图检索同时查询智能体的本地图与共享图。
2. **两种节奏的共享。** 技能每晚原样共享；每 `K = 2` 个游戏日，聚合步骤把群体的文本经验语义整合进持续演进的共享图。

相关论文在四个 LLM 骨干上，以三人群体 × 30 个游戏日评估该方法，对照 planner-only 控制组、Voyager 与 Echo 基线，并做四组留一消融，外加一个把经验记录原样共享、不做整合的 Raw 条件；指标覆盖生存（死亡数、重复死亡、XP/life）与探索（工具档位、黑曜石、进入下界）。单独发布的运行记录与本仓库中的脚本一起，可复算这些指标并重绘结果图。

## 特性

- **完整可运行的框架**，不只是日志：planner、reflector、aggregator、技能维护、双层图与检索、OpenAI 兼容客户端一应俱全。
- **指标与绘图脚本**：`paper_metrics.py` 从 run checkpoint 复算论文锚点指标；`plot_paper_progress.py` 由逐日序列与 checkpoint 重绘六张结果图。运行记录就位后，两者都无需 LLM 端点或 Minecraft 服务器。
- **前端可视化网页（只读）**：在浏览器中查看 run、各项指标、物品与成就清单、知识图画布和探索地图，不会改动任何实验数据；需要单独发布的 `results/` 目录。
- **基线代码**：Voyager 与 Echo 在同一套底层动作 API 上复现。

## 演示动图

取自 `演示动图.pptx` 最后一页的六段 Minecraft 短片。六个源 GIF 共 88.68 MiB，已调色板优化为 480×270、约 8 fps 的循环 GIF（合计 50.94 MiB），下方直接内联播放。`videos/manifest.json` 记录标签、帧数与时长。

| 建庇护所 | 钓鱼 | 制黑曜石 |
|---|---|---|
| ![建庇护所](media/videos/shelter.gif) | ![钓鱼](media/videos/fishing.gif) | ![制黑曜石](media/videos/obsidian.gif) |
| 播种 | 捕猎 | 下界传送门 |
| ![播种](media/videos/farming.gif) | ![捕猎](media/videos/hunting.gif) | ![下界传送门](media/videos/nether_portal.gif) |

## 可视化界面截图

以下截图来自只读可视化网页，由无头 Chrome 在 1600×1000 分辨率下、浅色主题采集；数据卡片栏已隐藏。素材来源见 [media/README.md](media/README.md)。

| 知识图谱 | 探索地图 |
|---|---|
| ![知识图谱](media/screenshots/knowledge_graph.jpg) | ![探索地图](media/screenshots/exploration_map.jpg) |
| 节点详情 | 成就清单 |
| ![节点详情](media/screenshots/node_detail.jpg) | ![成就清单](media/screenshots/achievements.jpg) |

## 仓库结构

| 路径 | 内容 |
|---|---|
| `openkage/` | 核心框架：`memory/`（双层图）、`orchestrate/`（聚合与 checkpoint）、检索、`llm.py`、`embedder.py`、rollout 主循环。 |
| `domains/minecraft/` | Minecraft 服务端与 Mineflayer 桥接（`mc_server/`）、原子动作 API、观测采集、指标。 |
| `agents/`、`prompts/`、`domains/minecraft/prompts/` | 智能体人设，以及 planner、reflector、aggregator、技能维护用到的全部提示词。 |
| `configs/openkage_default.yml` | 运行参数的唯一真相；命令行 flag 只做覆盖。 |
| `voyager/`、`echo/` | 两个外部基线的复现实现。 |
| `experiments/` | 四个运行入口（`run.py`、`run_ablation.py`、`run_voyager.py`、`run_echo.py`）、指标 CLI `paper_metrics.py`，以及绘图脚本 `plot_paper_progress.py`。 |
| `results/` | **不在本仓库内。** 将单独发布的运行记录解包到这里，供指标 CLI、绘图脚本和可视化网页使用。 |
| `viewer/` | 只读的前端可视化网页，用于浏览 `results/`（Python 服务端 + 预构建前端）。 |
| `paper/iclr2027/tables/` | 逐日序列与技能使用图数据。 |
| `paper/iclr2027/fig/` | 六张重绘结果图的 SVG 与 PDF。 |
| `media/` | 优化后的演示 GIF 与可视化界面截图；见 [media/README.md](media/README.md)。 |
| `LICENSE` | MIT 许可证，版权人使用匿名占位。 |

## 安装

需要 Python 3（`pip`）、Node.js（`npm`）；只有跑新实验才需要 Minecraft Java 1.21.11（Fabric）服务端。重绘结果图还需要系统 `PATH` 上有 [Inkscape](https://inkscape.org/)。

```bash
# 运行核心依赖
pip install openai python-dotenv pyyaml json-repair

# Mineflayer 桥接（仅跑新实验需要）
(cd domains/minecraft/mc_server && npm install)

# 重绘结果图
pip install matplotlib Pillow
# 另需系统包：inkscape

# 前端可视化网页（只读）
pip install fastapi uvicorn pyyaml NBT Pillow
```

Minecraft 服务端与 worker 目录**不在仓库内**。命令行默认值里凡是带 `/path/to/...` 的都是占位符，请按下文[自己跑实验](#自己跑实验)一节换成真实路径。

## 快速开始

先解包单独发布的运行记录，使 `results/` 与 `openkage/`、`experiments/`、`viewer/`、`paper/` 同级，然后：

```bash
# 1. 启动可视化网页
python viewer/server.py                    # 然后在浏览器打开 http://127.0.0.1:8090/

# 2. 复算论文锚点指标：单条 run，或整格汇总
python experiments/paper_metrics.py results/ds_openkage/run01
python experiments/paper_metrics.py --cell ds_openkage --summary

# 3. 重绘六张结果图（SVG + PDF）
python experiments/plot_paper_progress.py

# 4. 查看新实验的可选参数
python experiments/run.py --help
```

命令 1–3 需要运行记录；命令 4 不需要。

## 自己跑实验

```bash
cp .env.example .env                       # 填 OPENAI_API_KEY；非 OpenAI 服务再设 OPENAI_BASE_URL
python experiments/run.py --help
python experiments/run.py --domain minecraft --arms 3 --max-nights 30 \
    --model deepseek-flash --mc-dir /your/mc/server --mc-workers-dir /your/workers
```

- `run.py` —— OpenKAGE；加 `--no-graph` 跑 planner-only 控制组，加 `--seed-graph <已完赛run目录>` 从已有群体的知识起步（迁移实验）。
- `run_ablation.py` —— 留一消融，以及 Raw 条件（直接共享、不做整合）（`--ablation`，取值见 `--help`）。
- `run_voyager.py`、`run_echo.py` —— 两个外部基线。
- `--mc-dir`、`--mc-workers-dir`、`--mc-port-base`、`--mineflayer-port-base` 分别指向真实的服务端、worker 模板与空闲端口，详见 `domains/minecraft/mc_server/README.md`。同一台机器上并行跑多条 run 时，worker 目录与端口段必须互相错开。

四个脚本都支持 `--help`；参数以 `configs/` 下的 YAML 为准，命令行 flag 仅用于覆盖。

## 运行记录

实验运行记录**不在本仓库内**。它们作为单独的匿名归档发布，共 161 条 run：15 个主表实验格各 10 条，另含附录图对应的实验格。每条 run 含扁平逐日 checkpoint、白名单 `run_config.json`、逐日 context 转储、知识图数据库、窗口快照、分臂日志、关键事件帧，以及图与网页使用的图标；世界存档与向量索引附属文件不在其中。

解包后，如需让可视化网页或运行脚本读取其他 results 根目录，可设置 `OPENKAGE_RESULTS_DIR`。

## 复算论文指标

`experiments/paper_metrics.py` 是一个自包含的命令行脚本（只用标准库），从已发布的 checkpoint 复算论文 Table 1–3 与附录的锚点指标。它逐项对应论文 `build_paper_tables.py` 的公式；不需要 scipy、不需要研究仓库、不需要 LLM 端点或 Minecraft 服务器。

```bash
# 单条 run
python experiments/paper_metrics.py results/ds_openkage/run01

# 多条 run 或 shell glob
python experiments/paper_metrics.py results/abl_no_sharing/run0{1,2,3}

# 整个实验格，按论文口径汇总
python experiments/paper_metrics.py --cell ds_openkage --summary

# 附录逐 run 表同款 LaTeX 行
python experiments/paper_metrics.py --cell abl_raw_shared_pool --format latex

# 机器可读，附带内部诊断列
python experiments/paper_metrics.py --cell glm53_openkage --format json --all
```

默认表输出 `Deaths`、`Repeat`、`XP/life`、`Tool era`、`Obsidian`、`Nether`（Table 1–3 的六列），外加正文报的 `Weakest XP/life`、`Others' skills`、`All-3 dmd.`。`--format` 可取 `table` / `tsv` / `json` / `latex`；`--summary` 把输入的所有 run 汇总成一行；`--all` 追加 `Adv's`、`Eq-max`、`Starve`、`Drown`。实验格名见单独运行记录归档中的 `manifest.tsv`，例如 `ds_openkage`、`abl_raw_shared_pool`。

## 复现论文图

| 输入 | 内容 |
|---|---|
| `paper/iclr2027/tables/daily_series.json` | 15 个主表格逐 run、逐智能体、逐天的序列：死亡与死因、累计 XP、里程碑首达夜、技能调用、技能库规模、成就。 |
| `paper/iclr2027/tables/rb_e13_figdata.json` | Figure 11(b,c) 用的技能调用结果与改码前后窗口数据。 |
| `results/<cell>/runNN/checkpoint_<agent>_r*.json` | Figure 6 / 12 使用的原始 checkpoint，由单独的运行记录归档提供。 |

```bash
python experiments/plot_paper_progress.py
```

脚本向 `paper/iclr2027/fig/` 写出六对 SVG/PDF：

| 文件 | 图号 | 内容 |
|---|---|---|
| `fig_death_causes` | 5 | 四骨干上按死因拆分的每人死亡数 |
| `fig_transfer` | 6 | 知识迁移到全新群体 |
| `fig_runs` | 9 | 死亡数、XP/life、里程碑的 run 级散点 |
| `fig_death_rate` | 10 | 逐天死亡率 |
| `fig_knowledge` | 11 | 技能库增长、调用结果、改码修订 |
| `fig_scaling` | 12 | 群体规模与 60 夜时间轴 |

SVG 中的文字保持可编辑，PDF 由 Inkscape 导出。指标定义实现于 `experiments/paper_metrics.py`；`viewer/_deps/` 是可视化网页的内部副本。论文 Figure 1–4、7、8 是论文源码里的示意图，不由本脚本生成。

## 配置

- `.env.example` 说明 `OPENAI_API_KEY` / `OPENAI_BASE_URL`（任意支持 function calling 的 OpenAI 兼容 Chat Completions 端点）、可选的 `EMBEDDING_*`，以及 `OPENKAGE_RESULTS_DIR`。复制为 `.env` 即可；真实环境变量优先级更高。
- `configs/openkage_default.yml` 保存全部运行参数。三个角色的模型 id 在 `train.model` 下设置（`--model-task`、`--model-reflect`、`--model-consolidate` 可分别覆盖）；`llm.reasoning_effort` 按角色设置，端点不接受该字段时把对应角色设为 `none` 即可省略。
- **Embedding 可选。** 不设 `EMBEDDING_API_KEY` 时向量通道关闭，检索只走图结构。默认 `EMBEDDING_DIM` 为 896，YAML 里的检索阈值按它标定；公共服务未必提供 896 维，请把 `EMBEDDING_DIM` 改成服务商实际返回的维度（例如 1024），并注意不同编码器之间的结果不可直接横比。

## 说明

- 这是双盲评审期间的匿名公开仓库：作者姓名、单位、主机名、账号名、内部路径与凭据均已移除，`/path/to/...` 均为占位符；部分代码与配置注释为中文。
- `results/` 目录有意不放入本仓库；运行记录单独发布。
- 代码只对接一个 OpenAI 兼容端点，因此模型 id 与采样行为取决于你配置的服务商。
- 仓库内没有 `requirements.txt`，[安装](#安装)一节已按用途分组给出安装命令。可视化网页的前端源码位于 `viewer/frontend/src`（Vite + Vue），运行 `npm install && npm run build` 即可重建预构建的 `dist/`。

## 许可证与引用

本仓库在评审期以匿名版权人按 [MIT 许可证](LICENSE) 发布。引用信息将在评审期结束后提供。
