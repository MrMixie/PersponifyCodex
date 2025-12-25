Persponify Codex Launcher

This app starts the local Codex server + worker. It can live anywhere
(Desktop, Applications, Downloads).

Where to put the repo:
- Keep the PersponifyCodex folder anywhere (recommended: a folder you choose).
- The launcher will auto-detect it and save the path in:
  macOS:   ~/Library/Application Support/PersponifyCodex/launcher.json
  Windows: %APPDATA%\\PersponifyCodex\\launcher.json
  Linux:   ${XDG_CONFIG_HOME:-~/.config}/PersponifyCodex/launcher.json

If auto-detect fails:
- Double-click the app and pick your PersponifyCodex folder when prompted,
  or edit launcher.json manually.

You can also use the "Set Repo…" button in the launcher UI to update it,
then click "Restart" to switch without closing the app.
Use "Restart Server" to only restart the server (leave worker running).

Headless fallback:
- On older macOS builds where Tk crashes, the launcher runs in headless mode.
- You can force headless with PERSPONIFY_LAUNCHER_NOGUI=1.

Optional overrides:
- Set env var PERSPONIFY_CODEX_REPO to force the repo location.
- Set "pythonPath" in launcher.json if you want a specific python3.
