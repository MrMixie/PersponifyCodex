# Persponify Codex (CLI‑First)

Persponify Codex is a local‑only bridge between Roblox Studio and the Codex CLI.
The Studio plugin applies changes from the server, while **all chat + planning
happens in the CLI**.

GitHub: https://github.com/MrMixie/PersponifyCodex

## What You Install
1) **Roblox Studio plugin** (Persponify Codex)  
2) **Local companion repo** (this folder, from GitHub)  
3) **Codex CLI** (official Codex app/CLI; sign in to your account)

## Recommended Location (Repo)
Put the `PersponifyCodex` folder anywhere you choose. Examples:
- `~/Developer/PersponifyCodex`
- `C:\\Dev\\PersponifyCodex`
- `D:\\Projects\\PersponifyCodex`

No hard‑coded paths are used; the launcher stores its own config in your OS
config directory:
- macOS: `~/Library/Application Support/PersponifyCodex/launcher.json`
- Windows: `%APPDATA%\\PersponifyCodex\\launcher.json`
- Linux: `${XDG_CONFIG_HOME:-~/.config}/PersponifyCodex/launcher.json`

## Setup Matrix (TL;DR)
| You already have | Do this next | Then | Finish |
| --- | --- | --- | --- |
| Plugin installed | Download repo from GitHub | Start launcher | Connect plugin, register MCP, run Codex |
| Repo downloaded | Start launcher | Install/enable plugin | Connect plugin, register MCP, run Codex |
| Codex CLI ready | Download repo + install plugin | Start launcher | Connect plugin, register MCP, run Codex |

## Quick Start (Plugin‑First)
1) Buy/install the **Persponify Codex** plugin in Roblox Studio.
2) Download this repo from GitHub to a folder you choose.
3) Start the launcher:
   - macOS: open `PersponifyCodexLauncher.app`
   - Or: `python3 codex_launcher.py`
4) In Studio, open the plugin and click **Connect**.
5) Register the MCP server (one‑time), then run Codex (see below).
6) Ask Codex to build or change something. The plugin applies it automatically.

## Quick Start (GitHub‑First)
1) Download this repo.
2) Start the launcher (above).
3) Install/enable the Roblox plugin in Studio.
4) Connect the plugin.
5) Register the MCP server (one‑time), then run Codex.

## Quick Start (Codex‑First)
1) Install Codex and run `codex login`.
2) Download this repo and the Roblox plugin.
3) Start the launcher + connect the plugin.
4) Register the MCP server (one‑time), then run Codex.

## How to Connect the Codex CLI (Recommended)
1) Start the launcher (server on `127.0.0.1:3030`).
2) In Studio, open the plugin and click **Connect**.
3) Register the MCP server (one‑time):
   `codex mcp add persponify --url http://127.0.0.1:3030/mcp`
4) Run Codex in the repo (so it reads `AGENTS.md` + `STUDIO_GRADE_PROMPT.md`):
   - Option A: `cd /path/to/PersponifyCodex` then `codex`
   - Option B: `codex -C /path/to/PersponifyCodex`
     (Windows: `codex -C C:\Path\To\PersponifyCodex`)
5) First message: type `sync context`.
6) Chat as normal — Codex uses the MCP tool to enqueue actions and the plugin applies them.

Python requirement:
- **Python 3.9+ required** (3.11 recommended) to run the local server/launcher.
- macOS: Homebrew `brew install python@3.11`
- Windows: use `py -3` (auto‑selects newest 3.x)
- Linux: install `python3.11` (apt/dnf/pacman) or use your distro’s latest 3.x

Notes:
- If you move the repo, re‑run `codex mcp remove persponify` and add it again.
- Verify registration with `codex mcp list`.
- Codex CLI is global (installed via the official Codex app). It does not need
  to be copied into this repo.
- Optional legacy wrapper (guided flow): `python3 persponify_cli.py`
  (still works, but the recommended path is the real Codex CLI + MCP).
- Automatic option: click "Register Codex" in the launcher to auto-register MCP.
- If Codex says “NoContext,” type `sync context` again; it will request a fresh export.
 - Headless fallback: set `PERSPONIFY_LAUNCHER_NOGUI=1` to force no-GUI mode.
 - Optional overrides: set `PERSPONIFY_CODEX_REPO` or edit `launcher.json`.
 - Legacy stdio MCP: `codex mcp add persponify -- python3 -u /path/to/PersponifyCodex/persponify_mcp_server.py`

## Codex Model + Permissions
Codex is separate from this repo. Use Codex’s own settings to pick a model and
permission scope. Recommended:
- Use the **smallest permissions** that still let Codex read this repo.
- Avoid global filesystem access unless you explicitly need it.

## Studio‑Grade Behavior
- Run `codex` from inside the repo so it can read `AGENTS.md` and
  `STUDIO_GRADE_PROMPT.md`.
- If Codex ever ignores the tool, remind it: “Use the MCP tool `enqueue_actions`.”
- New chat tip: start with “sync context” — Codex should call `get_status` and
  `get_context_summary` automatically and summarize the Studio state.

## Release ZIP (Dev Shortcut)
- `python3 scripts/release_zip.py --upload --tag v0.3.1` (build + upload ZIP, creates the release if missing)

## Troubleshooting
- Plugin says **offline**: launcher is not running, or port `3030` is blocked.
- Plugin says **connected** but no changes: check Studio Output and plugin logs.
- MCP timeout on startup: switch to HTTP MCP registration:
  `codex mcp remove persponify` then
  `codex mcp add persponify --url http://127.0.0.1:3030/mcp`.
- CLI says **Codex not found**: install Codex and run `codex login`.

## Files You’ll Use
- `codex_launcher.py` → starts local server + status UI
- `persponify_mcp_server.py` → MCP server for Codex tool calls (legacy stdio fallback)
- `persponify_cli.py` → optional legacy wrapper
- `STUDIO_GRADE_PROMPT.md` → studio‑grade operating guide

## Support & Issues
Open issues on GitHub: https://github.com/MrMixie/PersponifyCodex/issues

## Versioning & Updates
- Repo releases use semantic tags (example: `v0.1.0`).
- Plugin builds use a date-based build id; publish the plugin and tag the repo together.
- For download-only users, attach a ZIP to each GitHub release.

## License
MIT. See `LICENSE`.
