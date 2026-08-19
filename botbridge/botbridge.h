/***************************************************************************
 *  MAXR Python-Bot Bridge — skeleton (header)
 *
 *  This class connects as a standalone client to a MAXR server, goes through the
 *  lobby, and mediates during the game between MAXR's cClient (binary protocol)
 *  and an external Python bot (JSON over stdin/stdout or a local socket).
 *
 *  Status: SCAFFOLD. Places marked with TODO are not yet verified and must be
 *  checked/filled in against the real MAXR code.
 *
 *  Note the license: when linking against MAXR (GPL v2) this bridge is also
 *  subject to GPL v2.
 ***************************************************************************/

#ifndef botbridge_botbridgeH
#define botbridge_botbridgeH

#include "botconfig.h"
#include "game/connectionmanager.h"
#include "game/data/player/playerbasicdata.h"
#include "game/networkaddress.h"
#include "game/startup/lobbyclient.h"
#include "utility/position.h"
#include "utility/signal/signalconnectionmanager.h"

#include <atomic>
#include <memory>
#include <random>
#include <string>

class cClient;
class cStaticMap;

// The concrete transport interface to the Python bot.
// Deliberately abstracted so that we can later choose between stdin/stdout and a
// local TCP socket without changing the bridge logic.
class IBotTransport
{
public:
	virtual ~IBotTransport() = default;

	// sends the (JSON) game state to the bot.
	virtual void sendState (const std::string& jsonState) = 0;

	// blocks until the bot delivers a (JSON) reply with its actions.
	// returns the raw JSON string.
	virtual std::string receiveActions() = 0;
};

class cBotBridge
{
public:
	cBotBridge (sBotConfig config, std::unique_ptr<IBotTransport> transport);
	~cBotBridge();

	// connects, goes through the lobby and enters the game.
	// returns only when the game ends or the connection breaks.
	void run();

private:
	// --- lobby phase -------------------------------------------------------
	void connectAndEnterLobby();
	void onLobbyPlayersList (const cPlayerBasicData& localPlayer,
	                         const std::vector<cPlayerBasicData>& players);
	void onStartGamePreparation();
	void onStartNewGame (std::shared_ptr<cClient> client);

	// --- landing-site choice ----------------------------------------------------
	// determines the next landing position to send:
	//  - fixed position from the config, if set
	//  - otherwise RNG until a position on valid land is found
	//    (terrain checked locally via staticMap; limited by maxLandingAttempts).
	// throws std::runtime_error if nothing is found after maxLandingAttempts.
	cPosition pickLandingPosition (const cStaticMap& map);

	// reaction to the landing state reported by the host.
	void onLandingStateReceived (eLandingPositionState state);

	// --- game phase -------------------------------------------------------
	void gameLoop();
	void takeTurn();
	std::string serializeStateToJson() const;
	void applyBotActions (const std::string& jsonActions);

private:
	sBotConfig config;
	cPlayerBasicData botPlayer;
	std::unique_ptr<IBotTransport> transport;

	std::mt19937 rng;
	int resolvedClan = -1; // internal 0..7, resolved once from config

	// last landing position sent to the host (for warning confirmation by
	// re-sending the same position).
	cPosition lastLandingPosition;
	bool hasLastLandingPosition = false;

	std::shared_ptr<cConnectionManager> connectionManager;
	std::unique_ptr<cLobbyClient> lobbyClient;
	std::shared_ptr<cClient> client;

	cSignalConnectionManager signalConnectionManager;
	std::atomic<bool> gameStarted{false};
	std::atomic<bool> finished{false};
};

#endif
