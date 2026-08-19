/***************************************************************************
 *  MAXR Python-Bot Bridge — entry point (skeleton)
 *
 *  Modelled after src/dedicatedserver/dedicatedservermain.cpp:
 *  initialise settings, SDL components and LoadData(), then start the bridge.
 ***************************************************************************/

// IMPORTANT: on Windows SDL by default redefines `main` to `SDL_main` (via
// SDL_main.h, which is pulled in by SDL.h). Since our include chain
// (botbridge.h -> client.h -> gametimer.h -> SDL.h) brings in SDL.h, our main()
// function would otherwise be renamed to SDL_main and the linker would find no
// entry point. SDL_MAIN_HANDLED forbids this redefinition. Must come BEFORE every
// include.
#define SDL_MAIN_HANDLED

#include "botbridge.h"
#include "botconfig.h"

#include "SDLutility/sdlcomponent.h"
#include "SDLutility/sdlnetcomponent.h"
#include "crashreporter/debug.h"
#include "defines.h"
#include "resources/loaddata.h"
#include "settings.h"
#include "utility/log.h"

#include <memory>
#include <string>

// TODO: concrete transport implementation (stdin/stdout or a local socket).

int main (int argc, char** argv)
{
	try
	{
		if (!cSettings::getInstance().isInitialized())
		{
			return -1;
		}
		CR_INIT_CRASHREPORTING();

		auto sdlComponent = std::make_shared<SDLComponent> (false);
		auto sdlNetComponent = std::make_shared<SDLNetComponent> (sdlComponent);

		if (LoadData (false) == eLoadingState::Error)
		{
			Log.error ("BotBridge: Fehler beim Laden der Daten!");
			return -1;
		}

		// load the configuration (path from argv[1], otherwise default).
		const std::string configPath = argc > 1 ? argv[1] : "bot_config.json";
		sBotConfig config = cBotConfigLoader::loadFromFile (configPath);

		// default port if 0 in the config (not set).
		if (config.serverPort == 0)
		{
			config.serverPort = DEFAULTPORT;
		}

		// TODO: transport = std::make_unique<cStdioTransport>();
		std::unique_ptr<IBotTransport> transport; // nullptr -> still to be set

		cBotBridge bridge (std::move (config), std::move (transport));
		bridge.run();

		Log.info ("BotBridge: EOF");
		return 0;
	}
	catch (const std::exception& ex)
	{
		Log.error (ex.what());
		return -1;
	}
}
