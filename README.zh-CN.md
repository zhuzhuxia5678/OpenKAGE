# OpenKAGE

<a href="https://arxiv.org/abs/2611.24302"><img src="https://github.com/user-attachments/assets/6e30acfc-24d1-4c12-9e48-a1327daffd2c" alt="arXiv" width="69" height="24"></a>
<a href="https://zhuzhuxia5678.github.io/OpenKAGE/"><img src="https://github.com/user-attachments/assets/d0d6b374-621b-4059-8eca-eefcf8b16c26" alt="Website" width="84" height="24"></a>
<a href="https://www.python.org/"><img src="https://github.com/user-attachments/assets/11fec6f4-f81c-4410-a58a-9d6ecb3dc0b6" alt="Python 3" width="88" height="24"></a>
<a href="https://www.minecraft.net/"><img src="https://github.com/user-attachments/assets/788ba52f-88fe-4ecd-af4c-e01fff4622dd" alt="Minecraft 1.21.11" width="130" height="24"></a>
<a href="LICENSE"><img src="https://github.com/user-attachments/assets/e63468b0-b8ee-4bf3-a52c-80e793bfe9ed" alt="MIT" width="61" height="24"></a>
<a href="https://github.com/zhuzhuxia5678/OpenKAGE_results/releases/tag/results-v1"><img src="https://github.com/user-attachments/assets/355b83a3-c7b6-4d6b-ac45-b095cb94d06b" alt="Results" width="81" height="24"></a>

[English](README.md)

**Open** **K**nowledge-sharing **A**gent-**G**roup **E**volution —— 一个驱动智能体终身学习和群体进化的框架。

OpenKAGE 让一组 LLM 智能体在开放世界探索中持续积累、共享并复用知识：情境记忆留在本地，可迁移经验与可执行技能进入共享层，由群体持续整合、检索并用于后续决策。

## 🎬 行为演示

OpenKAGE 智能体在 Minecraft 中积累情境记忆与可迁移经验，并通过意图感知检索复用群体共享的技能。下面六段展示代表性行为：建庇护所、钓鱼、制黑曜石、播种、捕猎与搭建下界传送门。

| 建庇护所 | 钓鱼 | 制黑曜石 |
|---|---|---|
| ![建庇护所](https://github.com/user-attachments/assets/9fdc0254-cc45-4cd4-9115-394f174ef11d) | ![钓鱼](https://github.com/user-attachments/assets/0c8474a2-5d60-4996-bd8a-50cc211a28bb) | ![制黑曜石](https://github.com/user-attachments/assets/cf93f505-e8b6-4aea-a57f-acdcec1a0144) |
| 播种 | 捕猎 | 下界传送门 |
| ![播种](https://github.com/user-attachments/assets/2d520038-338b-49b2-9b97-01e28329cbcc) | ![捕猎](https://github.com/user-attachments/assets/b768e390-fafb-4cd5-aed3-632206df3c02) | ![下界传送门](https://github.com/user-attachments/assets/f2d02d76-f4f2-455e-8c5a-38f2f0f28839) |

## 🚀 快速开始：起跑 Minecraft

OpenKAGE 在 Minecraft 中驱动一组 LLM 智能体。配置一个 OpenAI 兼容端点，准备 Minecraft 1.21.11 服务端模板和 worker 目录，然后启动 rollout：

```bash
cp .env.example .env
pip install openai python-dotenv pyyaml json-repair
pip install matplotlib Pillow fastapi uvicorn NBT
(cd domains/minecraft/mc_server && npm install)

python experiments/run.py --domain minecraft --arms 3 --max-nights 30 \
    --mc-dir /your/mc/server --mc-workers-dir /your/workers
```

`run.py` 为每个智能体准备独立 Minecraft 服务端，连接 Mineflayer 桥接，并把 checkpoint、知识图谱、逐日 context 和技能库写入 `results/`。模型 id、智能体人设、聚合周期、世界种子与难度由 `configs/openkage_default.yml` 决定，命令行 flag 覆盖配置。三个模型角色分别用 `--model-task`、`--model-reflect`、`--model-consolidate` 覆盖。加 `--mc-footage` 会按天录像。同机并行 run 必须用 `--mc-workers-dir`、`--mc-port-base` 和 `--mineflayer-port-base` 错开目录与端口段。

`--no-graph` 起 planner-only 控制组；`--seed-graph <completed-run-dir>` 从已完赛 run 的公共图谱和技能库起步。`run_ablation.py`、`run_voyager.py`、`run_echo.py` 分别起消融与基线。

## 🧠 核心思路

- **双层知识图。** 记忆层保存与情境绑定的个体记忆；经验层保存可迁移经验和可执行技能，并保留适用条件。
- **两种节奏的共享。** 技能每晚原样共享；每 `K = 2` 个游戏日，文本经验被整合进持续演进的共享图。
- **意图感知检索。** 规划时同时读取本地图与共享图的子图，让积累的知识重新进入决策。
- **完整可运行的研究栈。** 框架本体、两个基线、指标 CLI、绘图管线与只读可视化网页都在仓库内。

## 🗺️ 可视化界面

只读可视化界面用于查看一次运行中的知识状态：共享/私有图谱、探索轨迹、成就进展，以及概念或技能节点上的条件、经验与技能细节。

| 知识图谱 | 探索地图 |
|---|---|
| [![知识图谱](https://github.com/user-attachments/assets/651a8be7-d655-45bb-bf34-dd67e4893798)](https://github.com/user-attachments/assets/651a8be7-d655-45bb-bf34-dd67e4893798) | [![探索地图](https://github.com/user-attachments/assets/e8bef6dd-fdec-4b60-bb4c-9e020e6d3564)](https://github.com/user-attachments/assets/e8bef6dd-fdec-4b60-bb4c-9e020e6d3564) |
| 成就清单 | 节点详情 |
| [![成就清单](https://github.com/user-attachments/assets/f959d506-3ba7-46ce-9d32-640c2dcebc68)](https://github.com/user-attachments/assets/f959d506-3ba7-46ce-9d32-640c2dcebc68) | [![节点详情](https://github.com/user-attachments/assets/42ce1c91-c4cd-46a5-b43a-f8c52748393e)](https://github.com/user-attachments/assets/42ce1c91-c4cd-46a5-b43a-f8c52748393e) |

## 📦 仓库内容

| 路径 | 内容 |
|---|---|
| `openkage/` | planner、reflector、aggregator、双层图、检索、技能维护与 rollout 主循环。 |
| `domains/minecraft/` | Minecraft 动作 API、Mineflayer 桥接、观测采集与指标。 |
| `agents/`、`prompts/`、`configs/` | 智能体人设、提示词与权威运行配置。 |
| `voyager/`、`echo/` | 两个外部基线的复现实现。 |
| `experiments/` | 运行入口、指标 CLI 与绘图管线。 |
| `viewer/` | 只读可视化网页。 |
| `paper/iclr2027/` | 图数据与重绘结果图。 |
| `media/` | README 中使用的行为演示与界面截图；见 [media/README.md](media/README.md)。 |
| `results/` | `run.py` 产出的运行记录；已发布归档单独提供。 |

## 📊 复现

完整运行记录单独发布。解包归档，使 `results/` 与 `openkage/`、`experiments/`、`viewer/`、`paper/` 同级。

```bash
python experiments/paper_metrics.py --cell ds_openkage --summary
python experiments/plot_paper_progress.py
python viewer/server.py                    # http://127.0.0.1:8090/
```

这些命令读取已发布的 checkpoint，不需要在线模型或 Minecraft 服务器。重绘结果图还需要系统 `PATH` 上有 [Inkscape](https://inkscape.org/)。

| 目标 | 命令 | 主要输入 |
|---|---|---|
| 复算锚点指标 | `python experiments/paper_metrics.py --cell ds_openkage --summary` | 已发布 checkpoint |
| 重绘结果图 | `python experiments/plot_paper_progress.py` | 逐日序列、技能使用数据、checkpoint |
| 浏览运行状态 | `python viewer/server.py` | `results/` 与逐日 context |

<details>
<summary>指标 CLI 选项</summary>

```bash
python experiments/paper_metrics.py results/ds_openkage/run01
python experiments/paper_metrics.py results/abl_no_sharing/run0{1,2,3}
python experiments/paper_metrics.py --cell abl_raw_shared_pool --format latex
python experiments/paper_metrics.py --cell glm53_openkage --format json --all
```

默认表输出 `Deaths`、`Repeat`、`XP/life`、`Tool era`、`Obsidian`、`Nether`，外加 `Weakest XP/life`、`Others' skills`、`All-3 dmd.`。`--format` 可取 `table` / `tsv` / `json` / `latex`；`--summary` 汇总所有输入 run；`--all` 追加诊断列。实验格名见单独归档中的 `results/manifest.tsv`。

</details>

<details>
<summary>图输入与输出</summary>

| 输入 | 内容 |
|---|---|
| `paper/iclr2027/tables/daily_series.json` | 主表实验格逐 run、逐智能体、逐天的序列。 |
| `paper/iclr2027/tables/skill_usage_figdata.json` | Figure 11(b,c) 用的技能调用结果与改码前后窗口数据。 |
| `results/<cell>/runNN/checkpoint_<agent>_r*.json` | Figure 6 / 12 使用的原始 checkpoint。 |

`plot_paper_progress.py` 向 [paper/iclr2027/fig/](paper/iclr2027/fig/) 写出六对 SVG/PDF：`fig_death_causes`、`fig_transfer`、`fig_runs`、`fig_death_rate`、`fig_knowledge`、`fig_scaling`。论文 Figure 1–4、7、8 是论文源码里的示意图，不由本脚本生成。

</details>

<details>
<summary>配置</summary>

- [.env.example](.env.example) 说明 `OPENAI_API_KEY`、`OPENAI_BASE_URL`、可选的 `EMBEDDING_*` 与 `OPENKAGE_RESULTS_DIR`。
- [configs/openkage_default.yml](configs/openkage_default.yml) 是权威参数源，命令行 flag 仅做覆盖。
- Embedding 可选。不设 `EMBEDDING_API_KEY` 时只走图结构检索。默认 `EMBEDDING_DIM` 为 896；使用公共服务时请改成实际维度。

</details>

## 📝 说明

- 运行记录单独发布；世界存档与向量索引附属文件不在其中。
- 代码只对接一个 OpenAI 兼容端点，因此模型 id 与采样行为取决于配置的服务商。
- 可视化网页前端源码位于 [viewer/frontend/src](viewer/frontend/src)。

## 📄 论文与许可证

[OpenKAGE: Open-Ended Lifelong Learning in the Open World via Knowledge-Sharing Agent-Group Evolution](https://arxiv.org/abs/2611.24302)。按 [MIT 许可证](LICENSE) 发布。
