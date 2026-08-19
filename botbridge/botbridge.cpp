/***************************************************************************
 *  MAXR Python-Bot Bridge — skeleton (implementation)
 *
 *  IMPORTANT: this is a scaffold. Method signatures marked with "TODO/PRUEFEN"
 *  have not yet been verified 1:1 against the MAXR code. Before the first build
 *  these places must be reconciled.
 ***************************************************************************/

#include "botbridge.h"

#include "game/data/gamesettings.h"
#include "game/data/map/map.h"
#include "game/data/model.h"
#include "game/data/player/player.h"
#include "game/data/savegameinfo.h"
#include "game/logic/client.h"
#include "game/logic/turntimeclock.h"
#include "utility/log.h"
#include "utility/serialization/jsonarchive.h"

#include <SDL.h>
#include <nlohmann/json.hpp>

#include <random>
#include <stdexcept>

namespace
{
	// pause per pump pass (ms). Close to MAXR's cApplication::execute() (SDL_Delay(1)
	// there). Small enough to stay in sync with the 10ms game tick and the 15-tick lag
	// limit, without busy-spinning.
	constexpr unsigned int kPumpDelayMs = 1;
} // namespace

//------------------------------------------------------------------------------
cBotBridge::cBotBridge (sBotConfig config, std::unique_ptr<IBotTransport> transport) :
	config (std::move (config)),
	transport (std::move (transport)),
	rng (this->config.hasRngSeed ? std::mt19937 (this->config.rngSeed) : std::mt19937 (std::random_device{}())),
	connectionManager (std::make_shared<cConnectionManager>())
{
	// build the bot player from the configuration.
	botPlayer.setName (std::string (this->config.playerName));
	botPlayer.setColor (this->config.playerColor);
	botPlayer.setReady (false);

	// resolve the clan once (0 -> RNG, 1..8 -> 0..7).
	resolvedClan = this->config.resolveClan (rng);
	Log.info ("BotBridge: aufgeloeste Clan-ID (intern): " + std::to_string (resolvedClan));

	lobbyClient = std::make_unique<cLobbyClient> (connectionManager, botPlayer);
}

//------------------------------------------------------------------------------
cBotBridge::~cBotBridge() = default;

//------------------------------------------------------------------------------
void cBotBridge::run()
{
	connectAndEnterLobby();

	// pump cadence following the pattern of MAXR's cApplication::execute(): a tight
	// loop that processes the messages each pass and pauses only briefly.
	// cLobbyClient::run() is non-blocking and drains the message queue; once the game
	// runs, it delegates internally to client->run().
	//
	// Important: NO long polling interval. The game tick is 10ms (GAME_TICK_TIME) and
	// the client may lag at most 15 ticks behind (MAX_CLIENT_LAG). A ~1ms delay keeps
	// the bot in sync and at the same time avoids busy-spinning.
	while (!finished)
	{
		lobbyClient->run();

		if (gameStarted && client)
		{
			gameLoop();
			break;
		}

		SDL_Delay (kPumpDelayMs);
	}
}

//------------------------------------------------------------------------------
void cBotBridge::connectAndEnterLobby()
{
	// wire the signals BEFORE we connect (analogous to dedicatedservergame.cpp).
	signalConnectionManager.connect (lobbyClient->onLocalPlayerConnected, [this]() {
		Log.info ("BotBridge: mit Server verbunden, in Lobby.");
	});

	signalConnectionManager.connect (lobbyClient->onConnectionFailed,
	                                 [this] (eDeclineConnectionReason) {
		Log.error ("BotBridge: Verbindung fehlgeschlagen.");
		finished = true;
	});

	signalConnectionManager.connect (lobbyClient->onConnectionClosed, [this]() {
		Log.info ("BotBridge: Verbindung geschlossen.");
		finished = true;
	});

	signalConnectionManager.connect (
		lobbyClient->onPlayersList,
		[this] (const cPlayerBasicData& localPlayer,
		        const std::vector<cPlayerBasicData>& players) {
			onLobbyPlayersList (localPlayer, players);
		});

	// ready switch on onOptionsChanged: this signal fires after the map has been
	// loaded (verified in handleNetMessage_MU_MSG_OPTIONS). Only then does
	// tryToSwitchReadyState() accept the ready state.
	signalConnectionManager.connect (
		lobbyClient->onOptionsChanged,
		[this] (std::shared_ptr<cGameSettings>, std::shared_ptr<cStaticMap> staticMap,
		        const cSaveGameInfo&) {
			if (staticMap && !botPlayer.isReady())
			{
				Log.info ("BotBridge: Map vorhanden, melde bereit.");
				lobbyClient->tryToSwitchReadyState();
			}
		});

	// preparation begins -> we join the landing selection.
	signalConnectionManager.connect (lobbyClient->onStartGamePreparation, [this]() {
		onStartGamePreparation();
	});

	// the host reports the landing state back.
	signalConnectionManager.connect (lobbyClient->onLandingDone,
	                                 [this] (eLandingPositionState state) {
		onLandingStateReceived (state);
	});

	// the central signal: delivers the finished cClient at game start.
	signalConnectionManager.connect (lobbyClient->onStartNewGame,
	                                 [this] (std::shared_ptr<cClient> c) {
		onStartNewGame (std::move (c));
	});

	// connect with the address from the configuration.
	sNetworkAddress address;
	address.ip = config.serverIp;
	address.port = config.serverPort;
	lobbyClient->connectToServer (address);
}

//------------------------------------------------------------------------------
void cBotBridge::onLobbyPlayersList (const cPlayerBasicData& /*localPlayer*/,
                                     const std::vector<cPlayerBasicData>& /*players*/)
{
	// the player list has changed. The ready switch deliberately does NOT happen
	// here, but in onOptionsChanged, since tryToSwitchReadyState() requires a loaded
	// map. This method is kept as a hook for later logic (e.g. reacting to
	// co-players).
}

//------------------------------------------------------------------------------
void cBotBridge::onStartGamePreparation()
{
	Log.info ("BotBridge: Spielvorbereitung gestartet, betrete Landeauswahl.");
	lobbyClient->enterLandingSelection();

	// determine and send the first landing position.
	// TODO/PRUEFEN: staticMap access. Presumably via
	// lobbyClient->getLobbyPreparationData().staticMap.
	const auto& prep = lobbyClient->getLobbyPreparationData();
	if (!prep.staticMap)
	{
		Log.error ("BotBridge: keine Map fuer Landeauswahl verfuegbar.");
		finished = true;
		return;
	}

	try
	{
		const cPosition pos = pickLandingPosition (*prep.staticMap);
		lastLandingPosition = pos;
		hasLastLandingPosition = true;
		lobbyClient->selectLandingPosition (pos);
		Log.info ("BotBridge: Landeposition gesendet: ("
		          + std::to_string (pos.x()) + "," + std::to_string (pos.y()) + ")");
	}
	catch (const std::exception& e)
	{
		Log.error (std::string ("BotBridge: ") + e.what());
		finished = true;
	}
}

//------------------------------------------------------------------------------
cPosition cBotBridge::pickLandingPosition (const cStaticMap& map)
{
	// fixed position from the config, if set.
	if (!config.useRandomLanding())
	{
		cPosition pos (config.startingLocationX, config.startingLocationY);
		if (!map.isValidPosition (pos))
		{
			throw std::runtime_error ("Feste Landeposition liegt ausserhalb der Karte.");
		}
		return pos;
	}

	// RNG mode: roll within the valid range [0, size-1] until land is found.
	const int size = map.getSize().x(); // maps are square
	std::uniform_int_distribution<int> dist (0, size - 1);

	for (int attempt = 0; attempt < config.maxLandingAttempts; ++attempt)
	{
		const cPosition pos (dist (rng), dist (rng));
		if (map.isBlocked (pos)) continue;
		if (map.isWater (pos)) continue;
		// note: isCoast deliberately allowed; make it configurable if needed.
		return pos;
	}

	throw std::runtime_error (
		"No valid landing position found after " + std::to_string (config.maxLandingAttempts)
		+ " attempts.");
}

//------------------------------------------------------------------------------
void cBotBridge::onLandingStateReceived (eLandingPositionState state)
{
	switch (state)
	{
		case eLandingPositionState::Clear:
		case eLandingPositionState::Confirmed:
			Log.info ("BotBridge: Landeposition akzeptiert.");
			// continue in the flow; the game starts afterwards (MU_MSG_START_GAME).
			break;

		case eLandingPositionState::Warning:
			if (config.ignoreStartingLocationWarning && hasLastLandingPosition)
			{
				// confirmation by re-sending THE SAME position -> Confirmed.
				Log.info ("BotBridge: Warning ignoriert, bestaetige Position erneut.");
				lobbyClient->selectLandingPosition (lastLandingPosition);
			}
			else
			{
				Log.info ("BotBridge: Warning -> neue Position waehlen.");
				const auto& prep = lobbyClient->getLobbyPreparationData();
				if (prep.staticMap)
				{
					try
					{
						const cPosition pos = pickLandingPosition (*prep.staticMap);
						lastLandingPosition = pos;
						hasLastLandingPosition = true;
						lobbyClient->selectLandingPosition (pos);
					}
					catch (const std::exception& e)
					{
						Log.error (std::string ("BotBridge: ") + e.what());
						finished = true;
					}
				}
			}
			break;

		case eLandingPositionState::TooClose:
		{
			Log.info ("BotBridge: TooClose -> neue Position waehlen.");
			const auto& prep = lobbyClient->getLobbyPreparationData();
			if (prep.staticMap)
			{
				try
				{
					const cPosition pos = pickLandingPosition (*prep.staticMap);
					lastLandingPosition = pos;
					hasLastLandingPosition = true;
					lobbyClient->selectLandingPosition (pos);
				}
				catch (const std::exception& e)
				{
					Log.error (std::string ("BotBridge: ") + e.what());
					finished = true;
				}
			}
			break;
		}

		case eLandingPositionState::Unknown:
		default:
			// no decision yet; wait.
			break;
	}
}

//------------------------------------------------------------------------------
void cBotBridge::onStartNewGame (std::shared_ptr<cClient> c)
{
	Log.info ("BotBridge: Spiel startet, cClient erhalten.");
	client = std::move (c);
	gameStarted = true;
}

//------------------------------------------------------------------------------
void cBotBridge::gameLoop()
{
	// same pump cadence as in run(). After game start cLobbyClient::run() delegates
	// internally to client->run(), which processes network messages and client jobs.
	// We therefore keep calling lobbyClient->run() instead of manually rebuilding the
	// client cadence.
	//
	// TODO/PRUEFEN: whether client->handleNetMessages()/runClientJobs() is
	// additionally needed or whether client->run() (via lobbyClient->run()) already
	// does everything. In the GUI the gameTimer takes over the driving; headless the
	// exact obligation still has to be verified.
	while (!finished)
	{
		lobbyClient->run(); // delegates to client->run() as long as client is set

		// TODO/PRUEFEN: how do we detect "it is the bot's turn"?
		// candidates: turnEndState in the model, activeTurnPlayer == own player, or a
		// signal. Must be checked against cModel/cTurnCounter.
		const bool myTurn = false; // TODO

		if (myTurn)
		{
			takeTurn();
		}

		// TODO: detect game end (victory/defeat report) -> finished = true.

		SDL_Delay (kPumpDelayMs);
	}
}

//------------------------------------------------------------------------------
void cBotBridge::takeTurn()
{
	// protection for the lobby test: without a transport (nullptr) we cannot delegate
	// a turn to the bot. We then simply end the turn so that the bot does not block the
	// game and does not crash.
	if (!transport)
	{
		Log.warn ("BotBridge: kein Transport gesetzt, beende Zug ohne Aktion.");
		client->endTurn();
		return;
	}

	// 1) state as JSON to the bot.
	const std::string jsonState = serializeStateToJson();
	transport->sendState (jsonState);

	// 2) fetch the bot's reply (blocking).
	const std::string jsonActions = transport->receiveActions();

	// 3) apply the actions.
	applyBotActions (jsonActions);

	// 4) end the turn.
	client->endTurn();
}

//------------------------------------------------------------------------------
std::string cBotBridge::serializeStateToJson() const
{
	// directly analogous to savegame.cpp (confirmed working):
	//   nlohmann::json json;
	//   cJsonArchiveOut archive (json);
	//   archive << NVP (model);
	//   return json.dump (2);
	//
	// note: getModel() returns a const reference, but the serialize() method of the
	// archive pattern is non-const (shared for reading/writing). We therefore remove
	// the const-ness; when writing, the model is not modified. This is the usual
	// approach with this serialization pattern.
	nlohmann::json json;
	cModel& model = const_cast<cModel&> (client->getModel());

	cJsonArchiveOut archive (json);
	archive << NVP (model);

	// TODO/OPTIMIZATION: instead of the complete model, possibly output only the data
	// visible to the active player (fog of war, size). For the first run the full model
	// is enough.

	return json.dump (2);
}

//------------------------------------------------------------------------------
void cBotBridge::applyBotActions (const std::string& jsonActions)
{
	// expected format (proposal), e.g.:
	// { "actions": [
	//     { "type": "startMove", "unitId": 42, "path": [[5,6],[5,7]] },
	//     { "type": "attack",    "unitId": 7,  "target": [9,3] },
	//     { "type": "startBuild","unitId": 11, "building": "mine", ... }
	// ] }
	const auto parsed = nlohmann::json::parse (jsonActions, nullptr, false);
	if (parsed.is_discarded())
	{
		Log.error ("BotBridge: ungueltiges JSON vom Bot.");
		return;
	}

	for (const auto& action : parsed.value ("actions", nlohmann::json::array()))
	{
		const std::string type = action.value ("type", "");

		// TODO: mapping type -> cClient method. Examples (check the signatures!):
		//   "endTurn"     -> client->endTurn();
		//   "startMove"   -> fetch unit by ID from the model, build the path,
		//                    client->startMove (vehicle, path, ...);
		//   "attack"      -> client->attack (aggressor, targetPos, targetUnit);
		//   "startBuild"  -> client->startBuild (vehicle, buildingId, speed, pos);
		//   "transfer"    -> client->transfer (src, dst, value, resourceType);
		//
		// the cClient methods expect references to real unit objects from the cModel,
		// not just IDs. So we have to look them up by ID in the model. Encapsulate this
		// lookup centrally (e.g. helper findVehicle(id), findBuilding(id)).

		if (type == "endTurn")
		{
			// client->endTurn();  // is called in takeTurn() anyway
		}
		else
		{
			Log.warn ("BotBridge: unbekannter Aktionstyp: " + type);
		}
	}
}
