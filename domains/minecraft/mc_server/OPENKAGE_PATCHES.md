# node_modules patches

This file records direct modifications to third-party libraries under
`domains/minecraft/mc_server/node_modules/`. They are not in upstream
repositories: `npm install` / `npm ci` overwrites them, so reapply the patches
after an upgrade. Every changed line carries a `★ OPENKAGE PATCH` comment; grep
for that string to find all patch sites.

## Background

Minecraft's `/tick rate` command takes TPS (ticks per second, default 20), not a
speed multiplier. For an N-times faster world the server must receive
`/tick rate ${20*N}`. The bot's actions are driven by a local 50 ms physics tick,
independent of the server rate, so the client tick interval must be changed in
lockstep; otherwise the bot and the world run at different speeds.

## Patch 1: mineflayer/lib/plugins/physics.js

Make the local physics tick interval variable and expose `bot.setTickRate(N)`.

- `PHYSICS_INTERVAL_MS` / `PHYSICS_TIMESTEP`: `const` → `let`
- add `bot.setTickRate(rate)`: set `PHYSICS_INTERVAL_MS = 50/N`, then rebuild the
  timer with `clearInterval` + `setInterval` (inside the closure that owns
  `doPhysicsTimer` / `doPhysics`)
- `waitForTicks` timeout: `ticks * 50` → `ticks * PHYSICS_INTERVAL_MS`

The source is patched rather than monkey-patched because `doPhysicsTimer`,
`doPhysics` and `PHYSICS_TIMESTEP` are private to the `inject` closure and the
timer cannot be rebuilt from outside the bot instance.

## Patch 2: mineflayer/lib/plugins/digging.js

Dig duration must shrink with the speed multiplier, otherwise the server
cancels digs that take longer than it expects.

- `const waitTime = bot.digTime(block)` → `bot.digTime(block) / _octRate`
  (`_octRate = bot.openkageTickRate`, default 1)
- `swingInterval`: `350` → `350 / _octRate`

`bot.openkageTickRate` is set by `applyTickRate` in `mc_server/index.js`.

## Patch 3: mineflayer-pvp/lib/PVP.js

Rename the deprecated `physicTick` event to `physicsTick` so the console warning
does not consume the log budget on every tick.

## Companion changes (tracked in this repository; no reapplication needed)

- `mc_server/index.js`: `applyTickRate(N)`, module-level `currentTickRate`; `/start`
  reads `req.body.tick_rate` and synchronizes both sides after spawn;
  `resumeBot` / `/unfreeze` resend `/tick rate ${20*N}`
- `mc_server/control_primitives/waitUntil.js`: timeline and time-to-dawn scaled
  by `20*rate`
- `domains/minecraft/env.py`: `tick_rate` passed to `MinecraftWorld`, included in
  the `_do_start` payload, and used when rendering time
- `domains/minecraft/worker_pool.py`, `__init__.py`: forward `tick_rate` to workers
- `experiments/run.py`, `configs/openkage_default.yml`: `--tick-rate` CLI flag and
  `domain_scale.minecraft.tick_rate`

## Reapplying after an npm upgrade

```bash
cd domains/minecraft/mc_server
# 1. physics.js: const -> let (PHYSICS_INTERVAL_MS / PHYSICS_TIMESTEP)
# 2. physics.js: waitForTicks timeout ticks*50 -> ticks*PHYSICS_INTERVAL_MS
# 3. physics.js: add bot.setTickRate before bot.on('end')
# 4. digging.js: waitTime /= openkageTickRate, swingInterval 350 -> 350/_octRate
# 5. PVP.js: physicTick -> physicsTick (if the upgrade touched this file)
grep -n "★ OPENKAGE PATCH" node_modules/mineflayer/lib/plugins/physics.js node_modules/mineflayer/lib/plugins/digging.js
```
