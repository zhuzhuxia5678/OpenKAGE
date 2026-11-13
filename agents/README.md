# agents/ — Arm personas, grouped by task domain

Each Arm is a "perspective lens" (a persona) prepended verbatim to the acting and
reflection Agents. The set of personas is domain-specific; `run.py` loads
`agents/<domain>/*.md` according to `--domain`.

- `agents/minecraft/` — Minecraft: 16 MBTI-based personas (see the table below).

## File format

```
---
name: <human-readable name>
stage: <stage / domain>
style: <style>
---

# Role

<persona text, prepended verbatim to the acting / reflection Agent>
```

- The filename stem is the key: it is stored in the database and logs, and the
  `agents[:n]` slice order is the filename sort order.
- Frontmatter is metadata only; the body (without the `# Role` heading) is the
  persona text.
- A persona does not change the task: hard constraints come from the domain's
  system prompt and the reflection prompts; the persona is injected as data.

## Design principles

- **Orthogonality**: personas differ along independent decision axes so the group
  produces diverse trajectories for aggregation.
- **No domain know-how**: personas describe how to explore and use experience,
  never game-specific recipes (e.g. "smelt iron in a furnace"). That knowledge
  must be learned into memory.
- **ReAct style**: every persona asks the agent to reason before acting.
- **Personalities, not strategies**: personas state motives and preferences, not
  algorithms such as "depth-first search". Exploration strategy is left to the
  agent and its accumulated knowledge.

The persona bodies use the MBTI cognitive-function stack (dominant → auxiliary →
tertiary → inferior) as a standardized description. The 16 types are ordered in
pairs by dominant function.

## Minecraft personas

| # | key | MBTI | Stack | One-line focus | Orthogonal axis | Expected distinctive metric |
|---|-----|------|-------|----------------|-----------------|-----------------------------|
| 0 | `alice` (Alice) | ESTP | Se→Ti→Fe→Ni | Acts first, dislikes waiting, takes tactical risks | drive to get stronger × tactical aggression | high `tier_rank` / `n_advancements`, low `unique_items` |
| 1 | `bob` (Bob) | INFP | Fi→Ne→Si→Te | Excited by novelty itself; keeps options open | curiosity vs risk tolerance | high `unique_items`, low `tier_rank` |
| 2 | `carol` (Carol) | ISTJ | Si→Te→Fi→Ne | Checks existing conclusions first, dislikes duplicate work | trust in and scrutiny of existing conclusions | main amplifier of the evolving-vs-frozen gap |
| 3 | `dave` (Dave) | ISTP | Ti→Se→Ni→Fe | Small hands-on checks, wants certainty, records key state | tolerance of uncertainty | many small steps, frequent `onLog`, small `timeout_ticks` |
| 4 | `erin` (Erin) | ISFJ | Si→Fe→Ti→Ne | Secures a safe retreat first, fears irreversible loss | fear of irreversible loss | markedly fewer `n_deaths`, slower exploration |
| 5 | `frank` (Frank) | ENTJ | Te→Ni→Se→Fi | Sets a big goal then works backwards, intolerant of inefficiency | strategic aggression | planned technology progression, high `tier_rank` |
| 6 | `greg` (Greg) | ENTP | Ne→Ti→Fe→Si | Asks whether there is another way | trying new methods | multiple solutions per goal, varied single-step strategies |
| 7 | `helen` (Helen) | INTP | Ti→Ne→Si→Fe | Understands the mechanism before acting | theoretical reasoning | few heavy steps, long up-front reasoning |
| 8 | `ivan` (Ivan) | INTJ | Ni→Te→Fi→Se | Forms a global picture before acting | long-range foresight × coherence | internally consistent paths, few random branches |
| 9 | `julia` (Julia) | INFJ | Ni→Fe→Ti→Se | Long-range meaning, turns insight into situated action | long-range insight × meaning | goal-directed behaviour, slower on-the-spot adaptation |
| 10 | `kevin` (Kevin) | ENFJ | Fe→Ni→Se→Ti | Reads the room, aligns the group | coordination | maintains harmony, few direct conflicts |
| 11 | `laura` (Laura) | ESFJ | Fe→Si→Ne→Ti | Cares for others, keeps order | order × care | stable supply, risk-averse |
| 12 | `mike` (Mike) | ENFP | Ne→Fi→Te→Si | Sees links between possibilities, follows meaning | divergent connections × values | high `unique_items` in many directions |
| 13 | `nina` (Nina) | ESTJ | Te→Si→Ne→Fi | Turns chaos into order, sets rules | order × efficiency | methodical execution, little improvisation |
| 14 | `oscar` (Oscar) | ESFP | Se→Fi→Te→Ni | Lives in the moment, learns by doing | present experience × authenticity | fast reactions, little long-range planning |
| 15 | `paula` (Paula) | ISFP | Fi→Se→Ni→Te | Follows inner values, expresses through action | authenticity × experience | deep engagement, slower systematic progress |

The "expected distinctive metric" column is an observation hypothesis, not an
instruction in any persona: no persona text tells the agent to maximize a metric.

**Filename order is the priority order.** `--smoke` takes the first two personas,
`--arms 3` (the default) the first three (`alice`, `bob`, `carol`); the rest are
used when scaling group size. Do not rename files: the key is stored in databases
and logs, so a rename breaks comparability with recorded runs.
