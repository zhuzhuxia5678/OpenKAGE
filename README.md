# OpenKAGE

**Open** **K**nowledge-sharing **A**gent-**G**roup **E**volution — a training-free framework in which a group of LLM agents accumulates, shares and reuses knowledge while exploring an open world.

[简体中文](README.zh-CN.md)

This is an anonymous public repository for the core OpenKAGE code associated with an ICLR 2027 submission. Author names, affiliations, and citation details are intentionally omitted during the review period. It contains the framework, re-implementations of two external baselines, prompts, configuration, metric and figure scripts, figure data, regenerated figures, and a read-only visualization frontend.

It does **not** include the experiment run records. Those are distributed separately as an anonymous archive for reproducibility. To use the metric CLI, the figure script, or the viewer, extract that archive so that its `results/` directory sits beside the code directories in this repository. Running a new experiment requires your own OpenAI-compatible LLM endpoint and a Minecraft Java server.

## Table of Contents

- [What is OpenKAGE](#what-is-openkage)
- [Highlights](#highlights)
- [Repository layout](#repository-layout)
- [Installation](#installation)
- [Quick start](#quick-start)
- [Running your own experiments](#running-your-own-experiments)
- [Run records](#run-records)
- [Recomputing the paper's metrics](#recomputing-the-papers-metrics)
- [Reproducing the paper's figures](#reproducing-the-papers-figures)
- [Configuration](#configuration)
- [Notes](#notes)
- [License and citation](#license-and-citation)

## What is OpenKAGE

OpenKAGE studies open-ended, long-horizon learning with LLM agents in Minecraft. Agents start with empty knowledge stores, propose and revise their own goals each game day, and write down what they learn. Two design choices make that knowledge useful to the group:

1. **A two-layer knowledge graph.** A *memory layer* keeps situated, per-agent memories; an *experience layer* holds transferable experience and executable skills together with the conditions under which they apply. Intent-aware subgraph retrieval reads from both the agent's local graph and the shared graph.
2. **Shared knowledge with two rhythms.** Skills are shared verbatim every night; every `K = 2` game days an aggregation step semantically consolidates the group's textual experience into a continuously evolving shared graph.

The associated paper evaluates groups of three agents over 30 game days on four LLM backbones, against a planner-only control, Voyager and an Echo baseline, plus four leave-one-out ablations and a Raw condition that shares experience records verbatim without consolidation. Readouts cover survival (deaths, repeated deaths, XP per life) and exploration (tool tier, obsidian, Nether entry). The separately distributed run records and the scripts in this repository support recomputing those metrics and regenerating the result figures.

## Highlights

- **Fully runnable framework**, not just logs: planner, reflector, aggregator, skill maintenance, two-layer graph and retrieval, with an OpenAI-compatible client.
- **Metric and figure scripts**: `paper_metrics.py` recomputes the paper's anchor metrics from run checkpoints; `plot_paper_progress.py` rebuilds the six result figures from the released per-day series and checkpoints. Neither needs an LLM endpoint or a Minecraft server once the run records are present.
- **Frontend visualization web page (read-only)**: view runs, metrics, inventories and achievements, knowledge-graph canvases and exploration maps in the browser, without modifying any experiment data. It requires the separately distributed `results/` directory.
- **Baseline code**: Voyager and Echo re-implemented on the same bare action API.

## Repository layout

| Path | Contents |
|---|---|
| `openkage/` | Core framework: `memory/` (two-layer graph), `orchestrate/` (aggregation, checkpointing), retrieval, `llm.py`, `embedder.py`, rollout loop. |
| `domains/minecraft/` | Minecraft server and Mineflayer bridge (`mc_server/`), atomic action API, observation collection, metrics. |
| `agents/`, `prompts/`, `domains/minecraft/prompts/` | Agent personas and every prompt used by the planner, reflector, aggregator and skill maintenance. |
| `configs/openkage_default.yml` | Single source of truth for run parameters; CLI flags override it. |
| `voyager/`, `echo/` | Re-implementations of the two external baselines. |
| `experiments/` | Four run entry points (`run.py`, `run_ablation.py`, `run_voyager.py`, `run_echo.py`), the metric CLI `paper_metrics.py`, and the figure script `plot_paper_progress.py`. |
| `results/` | **Not included.** Place the separately distributed run records here to use the metric CLI, figure script, and viewer. |
| `viewer/` | Read-only frontend visualization web page for browsing `results/` (Python server + prebuilt frontend). |
| `paper/iclr2027/tables/` | Per-day series and skill-usage figure data. |
| `paper/iclr2027/fig/` | The six regenerated result figures as SVG and PDF. |
| `LICENSE` | MIT License with an anonymous copyright holder. |

## Installation

You need Python 3 with `pip`, Node.js with `npm`, and — only for new runs — a Minecraft Java 1.21.11 (Fabric) server. Regenerating figures additionally needs [Inkscape](https://inkscape.org/) on `PATH`.

```bash
# Core run dependencies
pip install openai python-dotenv pyyaml json-repair

# Mineflayer bridge (only for new runs)
(cd domains/minecraft/mc_server && npm install)

# Figure regeneration
pip install matplotlib Pillow
# + system package: inkscape

# Frontend visualization web page (read-only)
pip install fastapi uvicorn pyyaml NBT Pillow
```

The Minecraft server and worker directories are **not** bundled. Wherever a CLI default contains `/path/to/...`, it is a placeholder; replace it with a real path as described in [Running your own experiments](#running-your-own-experiments).

## Quick start

First extract the separately distributed run records so that `results/` sits beside `openkage/`, `experiments/`, `viewer/` and `paper/`. Then:

```bash
# 1. Launch the visualization web page
python viewer/server.py                    # then open http://127.0.0.1:8090/ in a browser

# 2. Recompute the paper's anchor metrics for one run, or pool a whole cell
python experiments/paper_metrics.py results/ds_openkage/run01
python experiments/paper_metrics.py --cell ds_openkage --summary

# 3. Regenerate the six result figures (SVG + PDF)
python experiments/plot_paper_progress.py

# 4. Inspect the options for a new run
python experiments/run.py --help
```

Commands 1–3 need the run records; command 4 does not.

## Running your own experiments

```bash
cp .env.example .env                       # set OPENAI_API_KEY; optionally OPENAI_BASE_URL
python experiments/run.py --help
python experiments/run.py --domain minecraft --arms 3 --max-nights 30 \
    --model deepseek-flash --mc-dir /your/mc/server --mc-workers-dir /your/workers
```

- `run.py` — OpenKAGE; add `--no-graph` for the planner-only control, or `--seed-graph <completed-run-dir>` to start from an existing group's knowledge (the transfer experiment).
- `run_ablation.py` — leave-one-out ablations and the Raw condition, i.e. direct sharing without consolidation (`--ablation`; see `--help` for the choices).
- `run_voyager.py`, `run_echo.py` — the two external baselines.
- `--mc-dir`, `--mc-workers-dir`, `--mc-port-base` and `--mineflayer-port-base` point at a real Minecraft server, worker templates and free ports; see `domains/minecraft/mc_server/README.md`. Runs on the same machine must use disjoint worker directories and port ranges.

All scripts accept `--help`; the YAML config under `configs/` is the authoritative parameter source and CLI flags override it.

## Run records

The experiment run records are **not part of this repository**. They are distributed separately as one anonymous archive containing 161 runs: 15 main-table cells with ten runs each, plus the appendix figure cells. Each run ships flattened daily checkpoints, a whitelisted `run_config.json`, per-day context dumps, knowledge-graph databases, window snapshots, per-arm logs, key-event footage frames, and the icons used by the figures and viewer. World saves and vector-index sidecars are not included.

After extracting that archive, set `OPENKAGE_RESULTS_DIR` if you want the viewer or the run scripts to read a different results root.

## Recomputing the paper's metrics

`experiments/paper_metrics.py` is a single self-contained CLI (standard library only) that recomputes the metrics anchored in Tables 1–3 and in the appendix from the released checkpoints. It mirrors the formulas in the paper's `build_paper_tables.py`; it does not require scipy, the research repository, an LLM endpoint, or a Minecraft server.

```bash
# one run
python experiments/paper_metrics.py results/ds_openkage/run01

# several runs, or shell globs
python experiments/paper_metrics.py results/abl_no_sharing/run0{1,2,3}

# a whole cell, pooled exactly as the paper pools a cell
python experiments/paper_metrics.py --cell ds_openkage --summary

# LaTeX rows in the appendix per-run-table format
python experiments/paper_metrics.py --cell abl_raw_shared_pool --format latex

# machine-readable, with internal diagnostic columns
python experiments/paper_metrics.py --cell glm53_openkage --format json --all
```

The default table reports `Deaths`, `Repeat`, `XP/life`, `Tool era`, `Obsidian`, `Nether` (the six columns of Tables 1–3), plus `Weakest XP/life`, `Others' skills` and `All-3 dmd.` (reported in the text). `--format` accepts `table`, `tsv`, `json` and `latex`; `--summary` pools all input runs into one cell-level row; `--all` adds `Adv's`, `Eq-max`, `Starve` and `Drown`. Cell names are those in the separate results archive's `manifest.tsv`, e.g. `ds_openkage`, `abl_raw_shared_pool`.

## Reproducing the paper's figures

| Input | Contents |
|---|---|
| `paper/iclr2027/tables/daily_series.json` | Per-run, per-agent, per-day series for the 15 main-table cells: deaths and causes, cumulative XP, milestone first-reach days, skill calls, skill-library size, advancements. |
| `paper/iclr2027/tables/rb_e13_figdata.json` | Skill-call outcomes and code-revision windows used by Figure 11(b,c). |
| `results/<cell>/runNN/checkpoint_<agent>_r*.json` | Raw checkpoints for Figures 6 and 12, provided by the separate results archive. |

```bash
python experiments/plot_paper_progress.py
```

This writes six SVG/PDF pairs into `paper/iclr2027/fig/`:

| File | Figure | Panel |
|---|---|---|
| `fig_death_causes` | 5 | Deaths per agent by cause on four backbones |
| `fig_transfer` | 6 | Knowledge transfer to a fresh group |
| `fig_runs` | 9 | Per-run scatter of deaths, XP per life and milestones |
| `fig_death_rate` | 10 | Daily death rate |
| `fig_knowledge` | 11 | Skill-library growth, call outcomes, revisions |
| `fig_scaling` | 12 | Group size and 60-day horizon |

SVG text stays editable; PDFs are exported with Inkscape. Metric definitions are implemented in `experiments/paper_metrics.py`; `viewer/_deps/` holds the visualization web page's internal copies. Figures 1–4, 7 and 8 of the paper are schematic diagrams from the paper source, not outputs of this script.

## Configuration

- `.env.example` documents `OPENAI_API_KEY` / `OPENAI_BASE_URL` (any OpenAI-compatible Chat Completions endpoint with function calling), the optional `EMBEDDING_*` keys, and `OPENKAGE_RESULTS_DIR`. Copy it to `.env`; real environment variables take precedence.
- `configs/openkage_default.yml` holds all run parameters. Set model ids per role under `train.model` (`--model-task`, `--model-reflect`, `--model-consolidate` override them), and `llm.reasoning_effort` per role; set a role to `none` to omit the field for endpoints that do not accept it.
- **Embeddings are optional.** Without `EMBEDDING_API_KEY` the vector channel is off and retrieval uses graph structure only. The default `EMBEDDING_DIM` is 896 and the retrieval thresholds in the YAML were calibrated for it; public services may not offer that size, so set `EMBEDDING_DIM` to whatever your provider returns (for example 1024) and note that results across encoders are not directly comparable.

## Notes

- This is an anonymous public repository for a double-blind review: author names, affiliations, host names, account names, internal paths, and credentials were removed; `/path/to/...` values are placeholders. Some code and configuration comments are in Chinese.
- The `results/` directory is intentionally absent from this repository; the run records are distributed separately.
- The code talks to a single OpenAI-compatible endpoint, so model ids and sampling behaviour depend on the provider you configure.
- There is no `requirements.txt`; the dependency groups in [Installation](#installation) are the install commands. The frontend source of the visualization web page is in `viewer/frontend/src` (Vite + Vue; `npm install && npm run build` to rebuild the prebuilt `dist/`).

## License and citation

This repository is released under the [MIT License](LICENSE) with an anonymous copyright holder during the review period. Citation details will be provided after the review period.
