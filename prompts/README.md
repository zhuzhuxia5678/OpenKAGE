# prompts/ — Agent prompts (one YAML file per Agent)

```
prompts/
├── default.yaml              ← tool-result text shared by all Agents (framework layer)
├── reflect/reflect.yaml      ← all prompts of the reflection Agent
├── skill_maintenance/        ← system / user_input of the skill-maintenance Agent
│   └── skill_maintenance.yaml   (its five skill-tool descriptions live in reflect.yaml)
└── aggregate/aggregate.yaml  ← all prompts of the aggregation Agent
```

The six skill tools (create / list / search / update / demote / delete) are one
set of classes used by two Agents, so their descriptions live in the `tools:`
section of `reflect.yaml` (the single source of truth). `_build_tools` redirects
the `skill_maintenance` vocabulary to the REFLECT prompts. Schemas (type /
required / enum) stay in the tool classes; YAML holds only model-visible text.

All files are loaded by `AgentPrompts` in `openkage/memory/prompt_store.py`
(REFLECT and AGGREGATE singletons, each validated once at module import).
Prompts describe the domain-independent mechanism of turning trajectories into
graph memory and merging them into shared experience. Domain-specific content
(tasks, observations, personas) is appended to the **user** message at runtime.

The same class also serves domain ReAct Agents: `vocab:` sections are optional,
and `base_dir` / `nested` allow YAML files in domain-private directories, e.g.
`domains/minecraft/prompts/react.yaml` loaded by `ReActPrompts(AgentPrompts)`.

## Placeholders

- `{{vocab.relations}}` — a reference into the same YAML file, resolved at load
  time and rendered by the last path segment (`*kinds` → markdown table,
  `*relations` → grouped list). Adding a vocabulary needs no code change.
- `{default_sec}` — a value supplied by the caller (a code constant or another
  Agent's vocabulary) through `render_system(**values)`. Placeholders are
  scanned from the text rather than listed separately.

## default.yaml — tool-result text

Lookup order is implicit:

```
result(key)  →  this Agent's tool_results  →  prompts/default.yaml  →  error
```

Agent YAML files therefore need no `include:` lists or cross-file references.
A same-named key shadows `default.yaml`; the loader logs the shadowed keys.
The top-level key is `tool_results` for historical reasons; the loader accepts
only that key.

Put text here when more than one Agent or any domain may use it (framework tool
results and a few filler strings). Keep domain-private text in domain YAML, JSON
key names in code (they are a contract), and do not add keys that no caller can
reach through `result()`.

## Core design: two layers, only the experience layer is aggregated

|  | **memory layer** | **experience layer** |
|---|---|---|
| concept name | proper name: `day 2`, `my first camp` | generic: `night movement risk` |
| content | first person, with source (day, coordinates) | decontextualized |
| criterion | unusable by someone who was not there | reusable as-is by others |
| kind | `where` / `what_happened` / `unfinished` / `open_question` | `working_definition` / `working_method` / `boundary` / `failed_attempt` |
| read by | tomorrow's own agent (`dump_memory_layer`) | full-text preload at reflection; **aggregated and shared** |
| lifetime | one trajectory (world regenerates, coordinates expire) | accumulates across epochs |

Preloaded inputs:

| Agent | Input | Form |
|---|---|---|
| aggregation | N arm experience layers | facet text (`dump_layer_full`) |
| | vector-similarity prescreen | hint line + similarity |
| reflection | both layers of its own graph | kind index (`dump_layer_index()`) |
| domain ReAct | arm memory layer | facet text (`dump_memory_layer`) |
| | count of shared-graph facets | one line |

Two dump forms, chosen by whether the reader can reach the text:
`dump_memory_layer` and `dump_layer_full` carry facet text (their readers cannot
query the source graph), while `dump_layer_index` carries only a kind index
(the reflection Agent can call `get_concept` on the same connection). All three
share `memory_graph.memory_dump_max_concepts` as a runaway guard.

Relations are always rendered with direction: outgoing `rel→peer`, incoming
`rel←peer`. Retrieval queries both layers; the query wording and hop count
express which layer is wanted.

Both layers are written by the reflection Agent (one ArmGraph, one call); the
aggregation Agent reads only the experience layer. Decontextualization is done
by the agent that had the experience, leaving aggregation to merge, weight and
reconcile across arms.

The layer is declared by the reflection Agent with `create_concept(layer=…)`:
*Can someone who was not there use this concept name as-is?* Yes →
`experience`, no → `memory`. `layer_of_kind` is used only for consistency
checks and migration of old databases without layer markers. A mismatch between
the declared layer and a facet kind is rejected by the tool.

Three code-level guarantees (not requests in a prompt):

| | Mechanism |
|---|---|
| read | `_dump_graph(g, layer=LAYER_EXPERIENCE)` — aggregation sees only experience |
| write | aggregation writes with `vocab="aggregate"`; the tool enum rejects memory kinds |
| warning | `_warn_layer_violations` warns if an arm's experience layer is empty |

## Structure of an Agent YAML file

| Key | Contents | Consumer |
|---|---|---|
| `system` | persona + rules + SOP (`{{KINDS}}` / `{{RELATIONS}}` from `vocab`) | `graph._reflect_system_prompt()` / `_AGGREGATE_SYSTEM_PROMPT` |
| `vocab.memory_kinds` / `experience_kinds` | two kind lists; single-layer Agents use `vocab.kinds` | tool enums + consistency checks |
| `vocab.relations` | relation edges and their retrieval `probes` | tool enums + `relation_semantics` |
| `tools` | descriptions and parameter notes of the ten narrow tools | `_TOOL_TEXT` |
| `user_inputs` | the text built per call | `reflect()` / `build_aggregate_input` |

YAML is used because prompt bodies contain markdown tables and code blocks:
`|` block scalars preserve them verbatim. Use `|+` (keep) when a trailing blank
line matters, since plain `|` clips it. `prompt_store.render_blocks` concatenates
blocks, substitutes `{name}` literally, and honors `when: <key>`; a missing value
raises instead of rendering an empty string.

## Hard constraints when editing vocabularies

1. **Adding a relation requires a probe.** Without one, the relation gets no
   semantic weight and is silently ignored during graph expansion. The loader
   raises on the missing probe.
2. **Never mix the two probe sets.** `score_relations(vocab=…)` must match the
   graph type, or none of its edges receive weights.
3. **Editing a prompt changes an experimental condition.** Runs before and after
   the change are not strictly comparable.

## Design principles

- **kinds are anchored in what was experienced, not in world truths.** The
  `working_` / `attempt` / `what_happened` wording signals that a record may be
  partial or wrong. There is deliberately no `definition` / `formalization`
  kind: it invites the model to promote a statistical inference into an absolute
  law, which can make experience a reason to stop acting.
- **A relation exists only if it supports an inference during retrieval.**
  Explanatory text belongs in facets, not on the edge.
