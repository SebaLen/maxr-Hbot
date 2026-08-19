/***************************************************************************
 *  MAXR Python-Bot Bridge — configuration (implementation)
 ***************************************************************************/

#include "botconfig.h"

#include <nlohmann/json.hpp>

#include <fstream>
#include <stdexcept>

//------------------------------------------------------------------------------
int sBotConfig::resolveClan (std::mt19937& rng) const
{
	if (clanConfigValue == 0)
	{
		// RNG: random internal index 0..7
		std::uniform_int_distribution<int> dist (0, cBotConfigLoader::kNumClans - 1);
		return dist (rng);
	}
	// 1..8 -> internal 0..7
	return clanConfigValue - 1;
}

//------------------------------------------------------------------------------
sBotConfig cBotConfigLoader::loadFromFile (const std::string& path)
{
	std::ifstream file (path);
	if (!file.is_open())
	{
		throw std::runtime_error ("BotConfig: Datei nicht gefunden: " + path);
	}

	nlohmann::json json;
	try
	{
		file >> json;
	}
	catch (const nlohmann::json::parse_error& e)
	{
		throw std::runtime_error (std::string ("BotConfig: JSON-Parse-Fehler: ") + e.what());
	}

	sBotConfig cfg;

	// --- connection ---
	cfg.serverIp = json.value ("server_ip", cfg.serverIp);
	cfg.serverPort = static_cast<std::uint16_t> (json.value ("server_port", 0));

	// --- player ---
	cfg.playerName = json.value ("player_name", cfg.playerName);

	if (json.contains ("player_color"))
	{
		const auto& c = json.at ("player_color");
		if (!c.is_array() || c.size() < 3)
		{
			throw std::runtime_error ("BotConfig: player_color muss [r, g, b] sein.");
		}
		const int r = c.at (0).get<int>();
		const int g = c.at (1).get<int>();
		const int b = c.at (2).get<int>();
		for (int v : {r, g, b})
		{
			if (v < 0 || v > 255)
				throw std::runtime_error ("BotConfig: player_color-Werte muessen 0..255 sein.");
		}
		cfg.playerColor = cRgbColor (static_cast<unsigned char> (r),
		                             static_cast<unsigned char> (g),
		                             static_cast<unsigned char> (b));
	}

	// --- clan ---
	cfg.clanConfigValue = json.value ("clan", cfg.clanConfigValue);
	if (cfg.clanConfigValue < 0 || cfg.clanConfigValue > kNumClans)
	{
		throw std::runtime_error (
			"BotConfig: clan muss 0 (random) oder 1.." + std::to_string (kNumClans) + " sein.");
	}

	// --- landing site ---
	cfg.startingLocationX = json.value ("starting_location_x", cfg.startingLocationX);
	cfg.startingLocationY = json.value ("starting_location_y", cfg.startingLocationY);
	cfg.ignoreStartingLocationWarning =
		json.value ("ignore_starting_location_warning", cfg.ignoreStartingLocationWarning);

	cfg.maxLandingAttempts = json.value ("max_landing_attempts", cfg.maxLandingAttempts);
	if (cfg.maxLandingAttempts < 1)
	{
		throw std::runtime_error ("BotConfig: max_landing_attempts muss mindestens 1 sein.");
	}

	if (cfg.startingLocationX < 0 || cfg.startingLocationY < 0)
	{
		throw std::runtime_error ("BotConfig: starting_location darf nicht negativ sein.");
	}

	// --- misc ---
	if (json.contains ("rng_seed") && !json.at ("rng_seed").is_null())
	{
		cfg.hasRngSeed = true;
		cfg.rngSeed = json.at ("rng_seed").get<std::uint32_t>();
	}
	cfg.logDir = json.value ("log_dir", cfg.logDir);

	return cfg;
}
