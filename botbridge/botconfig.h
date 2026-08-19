/***************************************************************************
 *  MAXR Python-Bot Bridge — configuration (header)
 *
 *  Reads the join parameters from a JSON file and provides them as a prepared
 *  C++ struct (incl. clan RNG and default handling).
 *
 *  Verified against the MAXR code:
 *    - cRgbColor (r,g,b,alpha=0xFF)          utility/color.h
 *    - cPosition (x,y)                       utility/position.h
 *    - Clan-Indizes 0..7 (8 Clans)           data/clans.json, unitdata.cpp
 ***************************************************************************/

#ifndef botbridge_botconfigH
#define botbridge_botconfigH

#include "game/networkaddress.h"
#include "utility/color.h"
#include "utility/position.h"

#include <cstdint>
#include <random>
#include <string>

struct sBotConfig
{
	// --- connection ---
	std::string serverIp = "127.0.0.1";
	std::uint16_t serverPort = 0; // 0 -> default (main sets DEFAULTPORT)

	// --- player ---
	std::string playerName = "ClaudeBot";
	cRgbColor playerColor = cRgbColor (255, 0, 0);

	// --- clan ---
	// raw value from the config (0 = random, 1..8 = clan).
	int clanConfigValue = 0;

	// --- landing site ---
	// (0,0) -> RNG mode. Otherwise a fixed position.
	int startingLocationX = 0;
	int startingLocationY = 0;
	bool ignoreStartingLocationWarning = true;

	// maximum number of RNG attempts to find a valid landing position before
	// aborting (prevents an endless loop on water-rich maps).
	int maxLandingAttempts = 1000;

	// --- misc ---
	// optional seed for reproducible RNG (clan + landing site).
	// if not set: a random seed.
	bool hasRngSeed = false;
	std::uint32_t rngSeed = 0;

	std::string logDir = "./bot_logs";

	// --- derived helpers ---

	// true if no fixed landing position is set (RNG mode).
	bool useRandomLanding() const { return startingLocationX == 0 && startingLocationY == 0; }

	// returns the internal 0-based clan index (0..7).
	// with clanConfigValue == 0 it is rolled randomly via rng.
	int resolveClan (std::mt19937& rng) const;
};

class cBotConfigLoader
{
public:
	// loads the configuration from a JSON file.
	// throws std::runtime_error on file/parse errors or invalid values.
	static sBotConfig loadFromFile (const std::string& path);

	// number of available clans (verified: 8).
	static constexpr int kNumClans = 8;
};

#endif
