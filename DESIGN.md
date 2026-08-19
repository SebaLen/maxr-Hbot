# MAXR Python Bot — Design Documentation

*External AI opponent for M.A.X. Reloaded via a JSON bridge*

> **Scope of this document.** This is the findings and design record from the
> analysis phase (6 June 2026). It documents how MAXR works internally and why the
> bridge architecture was chosen — the reasoning is still current and is what the
> implementation is built on. For the **current state of the project**, see the
> [README](../README.md); the "next steps" in section 10 below are from June 2026
> and have since been implemented.

---

## 1. Project goal

The goal is an AI opponent for M.A.X. Reloaded (MAXR) that acts as a standalone bot.
MAXR is an open-source remake of the strategy game *M.A.X.* (Interplay, 1996) and
supports multiplayer matches over TCP/IP, but offers no built-in AI opponent. The bot
is meant to fill this gap.

Core requirements:

- The bot is written in Python.
- The bot logic works entirely with JSON.
- Each turn of the bot is stored as its own JSON file, so decisions are transparent
  and traceable.
- The MAXR game itself should stay as unchanged as possible.

## 2. Starting point: MAXR

| Property | Value |
|----------|-------|
| Project | M.A.X.R. (Mechanized Assault & eXploration Reloaded) |
| Repository | github.com/maxr-dot-org/maxr (branch: develop) |
| Language | C++ (~96%), C, some Lua |
| License | GPL v2 (code), CC BY-SA 3.0 (parts of the assets) |
| Platforms | Linux, macOS, Windows (SDL2 + CMake/premake5) |
| JSON library | nlohmann/json |
| AI status | No built-in AI opponent; topic open in the forum |

The forum discussion on the AI originally dates from 2013. It is technically outdated
today; the chosen approach goes its own way.

## 3. Architecture decision: bridge variant

Two ways for an external bot were examined.

### 3.1 Pure Python (rejected)

A pure Python bot would have to rebuild MAXR's binary network format itself. That is
fragile: every change to a data structure in the C++ code would silently break the
Python mapping, and the bot would be strongly version-dependent.

### 3.2 Bridge variant (chosen)

A thin C++ bridge connects as a real client to the game, uses MAXR's own serialization
code and exchanges only JSON with the Python bot. Advantages:

- The binary wire format does not have to be rebuilt by hand — MAXR's own code
  handles that.
- Version-safe: the bridge uses the same structures as the game.
- The Python bot stays fully decoupled, in pure Python and JSON.
- Clean separation: if the bot crashes, the game does not crash.

### 3.3 Component overview

![Architecture of the MAXR bot bridge](media/architecture.png)

*Figure 1: The MAXR server stays unchanged and speaks binary over TCP. The C++ bridge
translates between binary and JSON. The Python bot contains the entire logic and
writes one JSON file per turn.*

| Component | Role |
|-----------|------|
| MAXR server | Unchanged game. Speaks binary over TCP. |
| C++ bridge | Thin glue. Uses `cClient` + `cJsonArchive`. Translates between binary (game side) and JSON (bot side). |
| Python bot | Entire game intelligence. Reads the JSON state, makes decisions, returns JSON actions. |
| Turn logs | One JSON file per turn, written by the Python bot. |

## 4. Findings from the source code

### 4.1 Network protocol

The network transport is binary (`cBinaryArchive`), not JSON. The message types are
defined in `src/lib/game/protocol/netmessage.h` as `enum eNetMessageType`.
Particularly relevant is the type `ACTION`, which the code explicitly describes as the
set of actions a client (AI or player) can trigger. AI and human thus share the same
mechanism.

Important message types:

- `TCP_HELLO` / `TCP_WANT_CONNECT` / `TCP_CONNECTED` — the three-stage connection
  handshake.
- `ACTION` — all game actions of a participant.
- `RESYNC_MODEL` / `REQUEST_RESYNC_MODEL` — complete copy of the game model to clients.
- `MULTIPLAYER_LOBBY` — lobby and game preparation.

### 4.2 The class `cClient` — the docking point

`src/lib/game/logic/client.h` is the central interface for the bridge. It is
considerably more convenient than expected:

- `getModel()` returns the complete, already-deserialized game state (`cModel`).
- `getActivePlayer()` returns the own player.
- For every game action there is a ready-made high-level method: `endTurn()`,
  `attack()`, `startMove()`, `startBuild()`, `transfer()`, `upgradeVehicle()`,
  `changeResearch()` and more.

These methods internally build the matching `cAction`, serialize it correctly and send
it to the server. All the binary detail is thereby already encapsulated by the game
itself — the bridge does not have to touch any byte format.

There are 28 action types (`cAction::eActiontype`), among others `InitNewGame`,
`StartMove`, `Attack`, `StartBuild`, `EndTurn`, `Transfer`, `ChangeResearch`,
`UpgradeVehicle`, `UpgradeBuilding`.

### 4.3 `cModel` as JSON — confirmed

The central open question was whether the game state can be serialized completely as
JSON. The check shows: yes, unambiguously.

- `cModel` has a template-generic `save()` method (`template <ArchiveOut Archive>`).
  The same code works for binary and JSON archives.
- MAXR already stores the complete game model as JSON in its savegames today
  (`src/lib/game/data/savegame.cpp`).
- The savegame code uses `archive << NVP(model)` followed by `json.dump(2)` — i.e.
  indented, human-readable JSON.

Key code location from `savegame.cpp`:

```cpp
nlohmann::json json;
cJsonArchiveOut archive (json);
archive << NVP (model);
file << json.dump (2);   // 2 = indentation, readable JSON
```

The data package contained in the model comprises, among others: `gameId`, `gameTime`,
random generator, `gameSettings`, `map`, `unitsData`, `players` (player list),
`moveJobs`, `attackJobs`, neutral buildings and vehicles, `turnCounter` and
`casualtiesTracker`.

### 4.4 Template: `dedicatedserver`

Under `src/dedicatedserver` MAXR already contains a complete, GUI-less program
(~820 lines) that connects via the connectionmanager and processes network messages.
It is the ideal blueprint for the bridge — we merely replace the server role with a
client (`cClient`) that logs in as a player.

## 5. Open points and risks

- **Size of the JSON state:** a complete model can become large. Performance and
  possibly filtering to the data visible to the bot are to be checked.
- **Lobby phase:** the bridge must first navigate through player setup, map selection
  and `initNewGame` before the actual game begins. The dedicatedserver code shows the
  flow.
- **Transport bridge ⇄ Python:** still to be chosen (stdin/stdout or a local socket).
- **Synchronization:** gametime sync and freeze modes must be served correctly so that
  the connection stays stable.

## 6. Join and start flow

The flow from connecting to game start was verified in the source code
(`lobbyclient.cpp`) and is divided into four phases. A large part runs automatically;
the bot only has to intervene actively at a few points.

### 6.1 Phase 1: handshake (automatic)

After `connectToServer(address)` the 3-way handshake runs by itself: the server sends
`TCP_HELLO` (with a version check), the client automatically answers with
`TCP_WANT_CONNECT` (name, color, ready), the server confirms with `TCP_CONNECTED` and
assigns a player number. Then the signal `onLocalPlayerConnected` fires. On an error
`TCP_CONNECT_FAILED` comes (signal `onConnectionFailed`).

### 6.2 Phase 2: lobby

The server sends `PLAYERLIST` (signal `onPlayersList`) and `OPTIONS` including the map
(signal `onOptionsChanged`). The bot sets its properties via
`changeLocalPlayerProperties(name, color, ready)` and reports ready via
`tryToSwitchReadyState()`.

> **Pitfall:** `tryToSwitchReadyState` does **not** set to ready as long as no valid
> map is present. The bot must therefore wait until `onOptionsChanged` has delivered
> the map.

### 6.3 Phase 3: preparation and landing

`START_GAME_PREPARATIONS` delivers `unitsData` and `clanData` (signal
`onStartGamePreparation`). Then the landing-site choice follows:
`enterLandingSelection()`, then `selectLandingPosition(cPosition)`. The server confirms
via `LANDING_STATE` (signal `onLandingDone`).

### 6.4 Phase 4: game start

`MU_MSG_START_GAME` is the decisive message. In the handler the `cClient` is created,
wired with `setPlayers` and `setLocalClient`, `setPreparationData` is called, and it is
handed to the bridge via the signal `onStartNewGame(client)`. From here the bridge
holds the `cClient` and is in the game.

Note: `client->initNewGame(...)` is commented out in the handler; the sending of the
init data (incl. clan) happens via the preparation phase.

## 7. Clan selection

The clan sits in `sInitPlayerData.clan` (an int) and is sent to the server via
`client->initNewGame(initPlayerData)` as a `cActionInitNewGame`. There are 8 clans,
indexed internally 0-based (0 to 7). The code validates: clan less than 0 or clan
greater/equal to the number of clans is invalid. The default `-1` means "no clan".

Clan indices: 0 The Chosen, 1 Crimson Path, 2 Von Griffin, 3 Ayer's Hand, 4 Musashi,
5 Sacred Eights, 6 7 Knights, 7 Axis Inc.

Configuration schema (1-based, user-friendly):

- `clan = 0` — RNG, random choice internally from 0 to 7.
- `clan = 1 to 8` — the respective clan, mapped internally to 0 to 7
  (internal value = `config.clan - 1`).

## 8. Landing-site choice

There are two separate validations, at different places.

### 8.1 Terrain check (client-side)

Whether a position lies on water or on blocked terrain is checked by
`cStaticMap::isWater()` and `isBlocked()` in `map.cpp`. In the real game this happens
client-side in the selection window before a position is sent. The server therefore
possibly does not validate the terrain again at landing. The bot must thus perform this
check itself and send only positions on valid land. The `staticMap` is available via
`lobbyPreparationData`.

### 8.2 Distance check (server-side)

The `cLandingPositionManager` checks the distances to other players on the server and
answers with `MU_MSG_LANDING_STATE` (signal `onLandingDone`).

Distances: `tooCloseDistance` = 10 fields, `warningDistance` = 28 fields.
States: `Unknown` (no choice yet), `Clear` (free), `Warning` (within the warning
distance), `TooClose` (too close, rejected), `Confirmed` (warning deliberately
confirmed).

### 8.3 Warning confirmation by repetition

Important mechanic: a warning is **not** confirmed by a flag, but by re-sending *the
same position*. If the player is in the `Warning` state and chooses the same position
again (distance to the last position ≤ `tooCloseDistance`), the state switches to
`Confirmed` and further warnings are ignored.

### 8.4 Configuration

- `starting_location_x` / `starting_location_y` — fixed position. If both are 0, the
  bot rolls via RNG.
- `ignore_starting_location_warning = true` — on `Warning` the bot re-sends the same
  position (leads to `Confirmed`).
- `ignore_starting_location_warning = false` — on `Warning` the bot chooses a new
  position.

Flow logic: determine the position (config or RNG), check `isWater`/`isBlocked` locally
(re-roll on water), send to the host, wait for `LANDING_STATE`. On `TooClose` a new
position; on `Warning` according to the flag; on `Clear` or `Confirmed` accepted.

### 8.5 Coordinates and map size

Coordinates are `cPosition` (`cFixedVector` with data type int, access via `x()` and
`y()`). Maps are always square (otherwise the loader aborts with *"Map must be
quadratic!"*) with a minimum size of 16. A position is valid according to
`isValidPosition` exactly when `0 <= x < size` and `0 <= y < size`. The valid interval
is thus `[0, size-1]` in both axes.

Example sizes of shipped maps: Mushroom 60, Lava 64, Three Isles 64, Donuts 70,
Delta 112. On the wire the size is stored as `uint16` (theoretically up to 65535), in
practice maps are small. The RNG logic fetches the actual size at runtime via
`staticMap getSize().x()` and rolls in the range `[0, size-1]`
(`uniform_int_distribution(0, size-1)`). The upper end is `size-1`, not `size` — mind
the off-by-one.

Attempt limit: `max_landing_attempts` (default 1000) bounds the RNG attempts to find a
valid landing position, and prevents endless loops on water-rich maps. On exceeding it
the bot aborts with an error.

## 9. Pump cadence (main loop)

The message cadence in the MAXR code is realized not as an interval timer, but as a
tight main loop. `cLobbyClient::run()` is non-blocking: it drains the message queue
(`cConcurrentQueue`) in a while loop and processes each message. Once the game runs,
`run()` delegates internally to `client->run()`. In the GUI, `cApplication::execute()`
calls this `run()` in a loop that pauses only `SDL_Delay(1)`, i.e. about one
millisecond, per pass. The real join client
(`menucontrollermultiplayerclient.cpp`) registers exactly as such a runnable.

> **Important:** do not use a long polling interval. The game tick is 10 ms
> (`GAME_TICK_TIME`) and the client may lag at most 15 ticks behind
> (`MAX_CLIENT_LAG`). A 500 ms interval would immediately produce 50 ticks of lag, far
> above the limit, which the sync mechanism would interpret as a connection problem.
> In the lobby phase (no game tick) a slow interval would be uncritical, but not in the
> game phase.

Implementation in the bridge: the `run()` loop follows the MAXR pattern. Per pass
`lobbyClient->run()` is called and at the end `SDL_Delay(kPumpDelayMs)` with
`kPumpDelayMs = 1`. That keeps the bot in sync and at the same time avoids
busy-spinning. It remains open whether `client->handleNetMessages()` /
`runClientJobs()` is additionally needed in the game or whether `client->run()` (via
`lobbyClient->run()`) already does everything.

## 10. Next steps *(as recorded in June 2026)*

**Completed at the time of writing:**

- Architecture decision (bridge variant).
- Check of the `cModel` JSON serialization (confirmed).

**Planned at the time of writing** — all of the following have since been implemented;
see the [README](../README.md) for the current state:

- Design the skeleton of the C++ bridge (based on `dedicatedserver` + `cClient`).
- Set up the Python bot skeleton with JSON turn logging.
- Define the transport channel between the bridge and Python.
