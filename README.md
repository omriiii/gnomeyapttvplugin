# Gnome Yapper

This is a Half-Life Alyx Gnome Mod that lets Twitch chat speak TTS audio lines through Gnome Chompsy.

## Requirements

- **Python 3.10+** -- [python.org/downloads](https://www.python.org/downloads/)
  - On the first install screen, check **"Add python.exe to PATH"**. If you skip this, `run.bat` won't be able to find Python.
- Half-Life: Alyx launched with:
  - **`-console -vconsole -tools`** in Launch Options (right-click the game in Steam -> Properties -> Launch Options)
  - Install [FrostEpex's AlyxLib HL:A Mod on Steam](https://steamcommunity.com/sharedfiles/filedetails/?id=3329679071) (Open that link and click the green +Subscribe button)
- (OPTIONAL) Run `streamber_bot_subaction.cs` in Streamer.bot as a subaction if you want to bind a Twitch Redeem to also run the TTS bot. 

## Quick start

1. Download this repo: green **Code** button on GitHub -> **Download ZIP**
   -> extract it somewhere.
2. Double-click **`run.bat`**.
   - First run only: it creates a local `venv` folder and installs a few
     Python packages. This can take a minute; you'll see pip output scroll
     by.
   - Every run after that starts in a couple seconds.
3. Your browser should open automatically to **http://127.0.0.1:8420/**
   (the status page). If it doesn't, just open that link yourself.
4. **On the status page, do these two things before anything else:**
   - Set **Twitch channel** to *your own* Twitch username (it defaults to
     someone else's channel).
   - Set the **audio output directory** to wherever you want generated
     `.wav` files saved (or click **Browse...** to pick one).
5. Hit **Test** to confirm you can hear a sample line -- this checks your
   TTS/audio path without needing HL:A or Twitch at all.
6. Launch Half-Life: Alyx with `-tools`. The status page's dot turns green
   once the bot finds it.

Leave the `run.bat` window open while you stream; closing it (or Ctrl+C)
stops the bot. Settings you change on the status page are saved to
`gnome_bot_state.json` next to the script, so they'll still be there next
time you run it.


## Troubleshooting

- **`run.bat` says Python wasn't found** -- install Python from the link
  above and make sure "Add to PATH" was checked, then run `run.bat` again.
- **Dependency install fails** -- re-run `run.bat`; if it keeps failing,
  delete the `venv` folder next to the script and try again from scratch.
- **Status page never turns green** -- make sure HL:A is running with
  `-tools`, and that nothing else (commonly SteamVR's `vrserver.exe`) is
  already using port 29000. If needed, add `-vconport <port>` to HL:A's
  launch options and change `HLA_PORT` near the top of
  `twitch_gnome_bot2.py` to match.
- **No sound from "Test"** -- check your OS's default playback device;
  `sounddevice` plays through whatever Windows currently has set as
  default output.
- **Something changed and it broke** -- delete the `venv` folder and
  re-run `run.bat` to get a clean set of dependencies.
