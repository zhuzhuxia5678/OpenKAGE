# OpenKAGE

**Open** **K**nowledge-sharing **A**gent-**G**roup **E**volution — a framework that drives lifelong learning and collective evolution of agents.

MIT · Python 3 · Minecraft 1.21.11 · [Website](https://zhuzhuxia5678.github.io/OpenKAGE/) · [Paper](https://arxiv.org/abs/2611.24302)

[简体中文](README.zh-CN.md)

OpenKAGE lets a group of LLM agents accumulate, share, and reuse knowledge while exploring an open world. Situated memories stay local; transferable experience and executable skills become a shared resource that the group consolidates, retrieves, and builds on over time.

## Behavior demos

OpenKAGE agents acquire situated memories and transferable experience in Minecraft, then reuse shared skills through intent-aware retrieval. The six clips below show representative behaviors: shelter construction, fishing, obsidian crafting, crop planting, hunting, and Nether portal entry.

| Shelter | Fishing | Obsidian crafting |
|---|---|---|
| ![Shelter](https://github.com/user-attachments/assets/9fdc0254-cc45-4cd4-9115-394f174ef11d) | ![Fishing](https://github.com/user-attachments/assets/0c8474a2-5d60-4996-bd8a-50cc211a28bb) | ![Obsidian crafting](https://github.com/user-attachments/assets/cf93f505-e8b6-4aea-a57f-acdcec1a0144) |
| Farming | Hunting | Nether portal |
| ![Farming](https://github.com/user-attachments/assets/2d520038-338b-49b2-9b97-01e28329cbcc) | ![Hunting](https://github.com/user-attachments/assets/b768e390-fafb-4cd5-aed3-632206df3c02) | ![Nether portal](https://github.com/user-attachments/assets/f2d02d76-f4f2-455e-8c5a-38f2f0f28839) |

## Quickstart: launch a Minecraft run

OpenKAGE runs a group of LLM agents inside Minecraft. Configure an OpenAI-compatible endpoint, prepare a Minecraft 1.21.11 server template and a worker directory, then start the rollout loop:

```bash
cp .env.example .env
pip install openai python-dotenv pyyaml json-repair
pip install matplotlib Pillow fastapi uvicorn NBT
(cd domains/minecraft/mc_server && npm install)

python experiments/run.py --domain minecraft --arms 3 --max-nights 30 \
    --mc-dir /your/mc/server --mc-workers-dir /your/workers
```

`run.py` provisions one Minecraft server per agent, connects the Mineflayer bridge, and writes checkpoints, knowledge graphs, daily contexts, and skill databases under `results/`. Model ids, personas, aggregation cadence, world seed, and difficulty live in `configs/openkage_default.yml`; CLI flags override them. Override model roles with `--model-task`, `--model-reflect`, and `--model-consolidate`. Add `--mc-footage` for per-day video. Parallel runs on one machine must use disjoint worker directories and port ranges with `--mc-port-base` and `--mineflayer-port-base`.

`--no-graph` starts the planner-only control; `--seed-graph <completed-run-dir>` starts from a completed run's public graph and skills. `run_ablation.py`, `run_voyager.py`, and `run_echo.py` start the ablations and baselines.

## Key ideas

- **Two-layer knowledge graph.** A memory layer keeps situated, per-agent memories; an experience layer holds transferable experience and executable skills with their applicability conditions.
- **Shared knowledge with two rhythms.** Skills are shared verbatim every night; every `K = 2` game days, textual experience is consolidated into a continuously evolving shared graph.
- **Intent-aware retrieval.** Planning reads subgraphs from both local and shared graphs, so accumulated knowledge returns to decisions.
- **Runnable research stack.** The framework, two baselines, metric CLI, figure pipeline, and read-only viewer are all included.

## Viewer

The read-only viewer makes a run's knowledge state inspectable: shared and private graphs, exploration trajectories, achievements, and the facets and skills attached to a concept.

| Knowledge graph | Exploration map |
|---|---|
| [![Knowledge graph](https://github.com/user-attachments/assets/651a8be7-d655-45bb-bf34-dd67e4893798)](https://github.com/user-attachments/assets/651a8be7-d655-45bb-bf34-dd67e4893798) | [![Exploration map](https://github.com/user-attachments/assets/e8bef6dd-fdec-4b60-bb4c-9e020e6d3564)](https://github.com/user-attachments/assets/e8bef6dd-fdec-4b60-bb4c-9e020e6d3564) |
| Achievements | Node detail |
| [![Achievements](https://github.com/user-attachments/assets/f959d506-3ba7-46ce-9d32-640c2dcebc68)](https://github.com/user-attachments/assets/f959d506-3ba7-46ce-9d32-640c2dcebc68) | [![Node detail](https://github.com/user-attachments/assets/42ce1c91-c4cd-46a5-b43a-f8c52748393e)](https://github.com/user-attachments/assets/42ce1c91-c4cd-46a5-b43a-f8c52748393e) |

## What's in this repository

| Path | What it contains |
|---|---|
| `openkage/` | Planner, reflector, aggregator, two-layer graph, retrieval, skill maintenance, and rollout loop. |
| `domains/minecraft/` | Minecraft action API, Mineflayer bridge, observation collection, and metrics. |
| `agents/`, `prompts/`, `configs/` | Personas, prompts, and the authoritative run configuration. |
| `voyager/`, `echo/` | Re-implementations of the two external baselines. |
| `experiments/` | Run entry points, the metric CLI, and the figure pipeline. |
| `viewer/` | Read-only visualization web page. |
| `paper/iclr2027/` | Figure data and regenerated result figures. |
| `media/` | Behavior clips and viewer screenshots used in this README; see [media/README.md](media/README.md). |
| `results/` | Run records produced by `run.py`; the released archive is distributed separately. |

## Reproducibility

The released run records are distributed separately. Extract the archive so that `results/` sits beside `openkage/`, `experiments/`, `viewer/`, and `paper/`.

```bash
python experiments/paper_metrics.py --cell ds_openkage --summary
python experiments/plot_paper_progress.py
python viewer/server.py                    # http://127.0.0.1:8090/
```

These commands read released checkpoints; no LLM endpoint or Minecraft server is needed. Figure regeneration also needs [Inkscape](https://inkscape.org/) on `PATH`.

| Goal | Command | Main input |
|---|---|---|
| Recompute anchor metrics | `python experiments/paper_metrics.py --cell ds_openkage --summary` | released checkpoints |
| Regenerate result figures | `python experiments/plot_paper_progress.py` | daily series, skill-usage data, checkpoints |
| Browse run state | `python viewer/server.py` | `results/` and per-day contexts |

<details>
<summary>Metric CLI options</summary>

```bash
python experiments/paper_metrics.py results/ds_openkage/run01
python experiments/paper_metrics.py results/abl_no_sharing/run0{1,2,3}
python experiments/paper_metrics.py --cell abl_raw_shared_pool --format latex
python experiments/paper_metrics.py --cell glm53_openkage --format json --all
```

The default table reports `Deaths`, `Repeat`, `XP/life`, `Tool era`, `Obsidian`, and `Nether`, plus `Weakest XP/life`, `Others' skills`, and `All-3 dmd.`. `--format` accepts `table`, `tsv`, `json`, and `latex`; `--summary` pools all input runs; `--all` adds diagnostic columns. Cell names follow the separate archive's `results/manifest.tsv`.

</details>

<details>
<summary>Figure inputs and outputs</summary>

| Input | Contents |
|---|---|
| `paper/iclr2027/tables/daily_series.json` | Per-run, per-agent, per-day series for the main-table cells. |
| `paper/iclr2027/tables/skill_usage_figdata.json` | Skill-call outcomes and code-revision windows for Figure 11(b,c). |
| `results/<cell>/runNN/checkpoint_<agent>_r*.json` | Raw checkpoints for Figures 6 and 12. |

`plot_paper_progress.py` writes six SVG/PDF pairs into [paper/iclr2027/fig/](paper/iclr2027/fig/): `fig_death_causes`, `fig_transfer`, `fig_runs`, `fig_death_rate`, `fig_knowledge`, and `fig_scaling`. Figures 1–4, 7, and 8 are schematic diagrams from the paper source, not outputs of this script.

</details>

<details>
<summary>Configuration</summary>

- [.env.example](.env.example) documents `OPENAI_API_KEY`, `OPENAI_BASE_URL`, optional `EMBEDDING_*` keys, and `OPENKAGE_RESULTS_DIR`.
- [configs/openkage_default.yml](configs/openkage_default.yml) is the authoritative parameter source; CLI flags override it.
- Embeddings are optional. Without `EMBEDDING_API_KEY`, retrieval uses graph structure only. The default `EMBEDDING_DIM` is 896; set it to the provider's actual dimension when using a public service.

</details>

## Notes

- Run records are distributed separately; world saves and vector-index sidecars are excluded.
- The code talks to a single OpenAI-compatible endpoint, so model ids and sampling behaviour depend on the configured provider.
- The viewer frontend source is in [viewer/frontend/src](viewer/frontend/src).

## Paper and license

[OpenKAGE: Open-Ended Lifelong Learning in the Open World via Knowledge-Sharing Agent-Group Evolution](https://arxiv.org/abs/2611.24302). Released under the [MIT License](LICENSE).
