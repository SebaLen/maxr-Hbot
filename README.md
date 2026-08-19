# MAXR Python Bot

An external AI opponent for [M.A.X. Reloaded (MAXR)](https://github.com/maxr-dot-org/maxr),
an open-source remake of the 1996 strategy game *M.A.X.* (Interplay). MAXR supports
multiplayer over TCP/IP but ships no built-in AI opponent — this project fills that gap.

The bot joins as a standalone player. A thin C++ **bridge** connects to a MAXR server
as a real client, uses MAXR's own serialization, and exchanges only JSON with the
**Python bot**, which holds all game logic.

```
MAXR server  <-- binary TCP -->  C++ bridge  <-- JSON -->  Python bot  --> one JSON log per turn
 (unchanged)                    (links MAXR)              (all the logic)
```

## Status

The heuristic bot plays complete games autonomously against a human or against a
second instance of itself. It is built as a **reproducible sparring partner** —
a fair, deterministic opponent rather than a strong one. Both sides obey the same
rules, which is what matters for the intended use.

- **Python bot** — playing. Expansion, defense, surveying, unit modes and upgrades
  are implemented.
- **C++ bridge** — working. Connects, walks the lobby, executes and validates
  actions through MAXR's own rules. Some hookup points are marked `TODO` in the source.
- **Headless bot-vs-bot** — working, including a server-side turbo mode for
  accelerated runs (see *Throughput* below).
- **Learning agent** — not implemented. The environment and the reward model are
  prepared; see *Roadmap*.

Known limitation: the bot can retry an impossible action across several turns
instead of abandoning it. Left in deliberately — it costs turns but keeps the
opponent's behaviour consistent.

## Repository layout

```
bot/          Python bot — all game intelligence (reads JSON state, returns JSON actions)
bot/tests/    Unit tests for the Python bot
botbridge/    Thin C++ bridge between the MAXR server (binary protocol) and the bot (JSON)
```

Python modules:

| Module | Role |
|--------|------|
| `bot_run.py` | Entry point and main turn loop |
| `maxr_bot_lib.py` | Core: socket/protocol layer and `GameState` |
| `build_plan.py` | Build planning |
| `strategy.py` | Builder requirements and overall strategy |
| `unit_modes.py` | Per-unit behaviour modes |
| `surveyor_planner.py` | Surveyor routing |
| `heat_map_calc.py` | Threat and heat map |
| `upgrade_logic.py` | Upgrade decisions |

## Design

Three invariants shape the codebase:

**The bridge is a dumb client.** It executes, validates against MAXR's own rules and
returns state. No game logic, no hardcoded coordinates — everything is computed from
state in the Python bot. This keeps the C++ side small and makes the strategy layer
replaceable, which is what allows a learning agent to be dropped in later.

**MAXR is sequential.** Units act one after another and every action produces a fresh
state. The bot re-reads state after each action and never computes on stale unit data.

**MAXR is lockstep.** The bridge ticks like a real client — one `client->run()` plus a
1 ms delay per loop, then a non-blocking check for a bot message. Actions are
fire-and-forget; the bridge does not wait for the server echo. Sync busy-loops and
catch-up sends were tried and removed, because they break the tick.

## Wire protocol

Line-based JSON over TCP (default port 5001), one message per line, one action per
message. `endTurn` is always sent alone. The full action table — `move`, `attack`,
`startBuild`, `finishBuild`, `transfer`, `startWork`, `stopWork`, `sentry`,
`setAutoMove` — with required fields and rejection reasons is documented in the
design document under `botbridge/`.

## Throughput

Headless bot-vs-bot runs were profiled to find out what actually limits game speed.

The bottleneck is the **lockstep wait between turns**, not per-frame delays:
roughly 800,000 bridge frames elapse while game time advances only ~400 ticks between
two bot turns. The bridge spends almost all of that idle-ticking, waiting for the
server's sync round-trip.

The lever is therefore server-side. A `turbo_mode` flag in the dedicated server's
configuration skips the fixed 10 ms server delay and injects tick events directly.
It is throttled by `MAX_CLIENT_LAG`, so the server never outruns the slowest bot and
no out-of-sync occurs. Bridge-side frame delay was also tested at 0 ms: stable, but
no speedup — it only busy-spins the CPU, confirming where the time goes.

Turbo and live spectating are mutually exclusive: a real client joining becomes the
slowest lockstep participant and drags the game back to real time. Turbo games are
inspected afterwards via the per-turn autosaves.

## Running

Two bots against each other requires: the dedicated server, two bridge instances,
two bot processes, and the `startGame` command.

```
python bot_run.py 127.0.0.1 5001 Bot1
python bot_run.py 127.0.0.1 5002 Bot2
```

A PowerShell starter automates the sequence. It launches the server as a child
process with redirected stdin, watches its output for the lobby-ready line and
writes `startGame` at exactly that point — no timing guesswork, no window automation.

<!-- PRÜFEN: Pfad/Name des Startskripts und der Aufruf ergänzen -->

## Tests

Unit tests for the Python bot live in `bot/tests`. They run the decision logic
against mocked game states, so no server or bridge is required.

## Roadmap

The next step is a learning agent trained against this heuristic bot.

Prepared so far: a headless, accelerated environment for reproducible runs, and a
reward model for the economy phase — reward rules for mines, factories, a production
ratchet and energy efficiency, calibrated against real game-state dumps rather than
estimates. The design deliberately rewards outcomes indirectly to avoid reward
hacking, so the agent cannot farm an isolated event instead of building an economy.

Not yet implemented: the reward module itself and the environment binding.

## A note on language

Code comments and documentation are in English. Some runtime **log messages** printed
by the bot are still in German — these are program output, not documentation, and
were left unchanged on purpose.

## License

This project links against MAXR, which is licensed under the **GNU General Public
License, version 2 (GPL-2.0)**. Because the C++ bridge links against MAXR, this
repository is distributed under **GPL-2.0** as well. See [`LICENSE`](LICENSE) for
the full text.
