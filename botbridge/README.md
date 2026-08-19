# MAXR Python-Bot Bridge — skeleton

Thin C++ bridge between a MAXR server and an external Python bot. It connects as a
standalone client, goes through the lobby and mediates during the game between
MAXR's `cClient` (binary protocol) and the Python bot (JSON).

## Files

- `botbridge.h` / `botbridge.cpp` — core of the bridge.
- `main.cpp` — entry point, modelled after `dedicatedservermain.cpp`.

## Confirmed facts (checked against the MAXR source code)

- `cLobbyClient` (src/lib/game/startup/lobbyclient.h) offers `connectToServer()`,
  lobby control and the signal `onStartNewGame(std::shared_ptr<cClient>)`, which
  delivers the finished client at game start.
- `cClient` (src/lib/game/logic/client.h) offers `getModel()` for the state and
  ready-made high-level methods for every action (`endTurn`, `startMove`,
  `attack`, `startBuild`, `transfer`, ...).
- `cModel` can be serialized as JSON via `cJsonArchiveOut` — exactly the way
  `savegame.cpp` already does it in production (`archive << NVP(model)` +
  `json.dump(2)`).

## Status of the open points

1. **Client cadence (pump cadence)**: SOLVED for the base cadence. Tight pump loop
   following the pattern of MAXR's cApplication::execute() with SDL_Delay(1).
   cLobbyClient::run() is non-blocking and delegates internally to client->run()
   after game start. The only thing still open is whether
   client->handleNetMessages()/runClientJobs() is additionally needed in the game.
   NO 500ms polling: the game tick is 10ms, max lag 15 ticks.
2. **Lobby ready trigger**: SOLVED. The ready switch hangs on onOptionsChanged
   (where the map is loaded); tryToSwitchReadyState() only sets ready when a valid
   map is present.
3. **Landing selection**: IMPLEMENTED. enterLandingSelection + pickLandingPosition
   (terrain checked locally, RNG bounded by the map size, maxLandingAttempts) +
   warning confirmation by re-sending.
4. **"Bot ist am Zug" detection**: OPEN. Via cModel (activeTurnPlayer /
   turnEndState) or a signal — still to be determined.
5. **Action mapping**: OPEN. JSON action -> cClient method. The methods expect
   references to real unit objects from the model, not just IDs -> build a central
   ID lookup (findVehicle/findBuilding).
6. **Transport**: OPEN. stdin/stdout vs. a local socket (IBotTransport).
7. **State size**: possibly serialize only the data visible to the active player
   (fog of war / performance).

## License

When linking against MAXR (GPL v2) this bridge is also subject to GPL v2.
