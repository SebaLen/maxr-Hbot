# MAXR Python Bot

An external AI opponent for [M.A.X. Reloaded (MAXR)](https://github.com/maxr-dot-org/maxr),
an open-source remake of the 1996 strategy game *M.A.X.* (Interplay). MAXR supports
multiplayer over TCP/IP but ships no built-in AI opponent — this project fills that gap.

The bot plays as a standalone player. A thin C++ **bridge** connects to a MAXR server
as a real client, uses MAXR's own serialization, and exchanges only JSON with the
**Python bot**, which contains all of the game logic.

## Repository layout

```
bot/          Python bot — all the game intelligence (reads JSON state, returns JSON actions)
botbridge/    Thin C++ bridge between the MAXR server (binary protocol) and the Python bot (JSON)
```

- **`bot/`** — pure Python. The bot reads the JSON game state, decides, and returns JSON
  actions. See the module files for the heuristics (expansion, defense, unit modes, upgrades, …).
- **`botbridge/`** — the C++ glue. Connects as a client, walks through the lobby, and
  translates between MAXR's binary `cClient` protocol and JSON. See
  [`botbridge/README.md`](botbridge/README.md) and the design document
  `botbridge/MAXR_Bot_Documentation.docx` for details.

## How it fits together

```
MAXR server  <-- binary TCP -->  C++ bridge  <-- JSON -->  Python bot  --> one JSON log per turn
 (unchanged)                    (links MAXR)              (all the logic)
```

## Status

Work in progress. The bridge is a working scaffold; some hookup points are marked `TODO`
in the source and in `botbridge/MAXR_Bot_Documentation.docx`.

## A note on language

Code comments and documentation are in English. Some runtime **log messages** printed by
the bot are still in German — these are program output, not documentation, and were left
unchanged on purpose.

## License

This project links against MAXR, which is licensed under the **GNU General Public License,
version 2 (GPL-2.0)**. Because the C++ bridge links against MAXR, this repository is
distributed under **GPL-2.0** as well. See the [`LICENSE`](LICENSE) file for the full text.
