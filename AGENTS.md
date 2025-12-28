# Repository Guidelines

## Project Structure & Module Organization
- `app.py` is the local FastAPI companion server that the Roblox plugin connects to.
- `context_scripts/` holds the exported Roblox modules and UI scripts (e.g., `Core/`, `Protocol/`, `UI/`). Treat these as the source of truth for Studio-side behavior.
- `tx_update_*.json` are queued transactions used to apply updates back into Roblox Studio.
- `context_latest.json` captures the most recent context export for quick inspection.
- `UPGRADES.md` tracks the roadmap and feature backlog.
- `persponify_mcp_server.py` exposes MCP tools for the real Codex CLI to enqueue actions.

## Codex CLI (MCP) Rules
- Prefer the real Codex CLI (interactive) with MCP tools over custom wrappers.
- Follow `STUDIO_GRADE_PROMPT.md` as the operating baseline.
- Use the MCP tool `enqueue_actions` to apply changes into Studio.
- Use `get_status` to verify plugin connectivity before applying.
- Use `get_context_summary` when you need fresh Studio context.
- At the start of each new Codex chat, call `get_status` + `get_context_summary`
  and summarize the current Studio state before planning changes.

## Build, Test, and Development Commands
- `python -m uvicorn app:app --host 127.0.0.1 --port 3030 --reload` runs the local server with auto-reload.
- `python app.py --host 127.0.0.1 --port 3030 --reload` runs the same server without invoking uvicorn as a module.
- `python3 codex_launcher.py` starts a small local UI that launches the server and Codex worker, shows status, and stops them on close.
- `PersponifyCodexLauncher.app` runs the launcher UI via a double-click (safe to move anywhere).
- `python3 codex_worker.py` runs the Codex job worker (file queue) using the local `codex` CLI.
- `python3 codex_worker.py --command "<codex command>"` overrides the Codex command template.
- In Roblox Studio, load the plugin and click Connect to verify server discovery and primary lease.
- Optional quick health check: `curl http://127.0.0.1:3030/health` (expect HTTP 200).

## Agent Shortcuts (Speed)
- `python3 scripts/quick_sync.py --commit --push` syncs `context_scripts/` to Studio, then commits/pushes.
- `python3 scripts/release_zip.py --upload --tag v0.1.0` builds and updates the GitHub release ZIP (needs `GH_TOKEN` or `GITHUB_TOKEN`).
- Prefer these helpers over manual enqueue/commit steps when iterating on plugin scripts.

## Coding Style & Naming Conventions
- Python: 4-space indentation, type hints where practical, keep endpoints and models explicit.
- Lua: tabs for indentation (align with existing files), module paths mirror Roblox structure (e.g., `ReplicatedStorage/PersponifyStudioAI/Core/...`).
- Transactions: keep the `TX_UPDATE_YYYY_MM_DD_NNN` naming pattern for traceability.
- Prefer clear, defensive logging over clever shortcuts.

## Testing Guidelines
- No automated tests yet. Use manual smoke checks:
  - Start the server, connect the plugin, click Resync, and confirm context export succeeds.
  - Apply a small tx update and verify `Apply start/end` logs in Output.
- If adding new features, include at least one repeatable manual test step in your PR description.

## Commit & Pull Request Guidelines
- There is no git history here; use conventional-style messages (e.g., `feat: add audit log`) when creating commits.
- PRs should include: summary, verification steps, and screenshots for any UI changes.

## Security & Configuration Notes
- Keep the server bound to `127.0.0.1` unless explicitly adding a secure remote bridge.
- Avoid storing keys or secrets in the repo; use environment variables instead.
- When modifying connection logic, ensure localhost-only defaults remain intact.

## Agent Workflow (Roblox Plugin)
- Default to applying edits via tx updates and keep `context_scripts/` in sync.
- Avoid pasting full scripts unless a manual review is needed; summarize changes with file paths instead.

## Product Goal
- Build a local-first companion experience that delivers what "Lemonade" offers, but better: dynamic connections between the Roblox plugin and multiple AI backends (local + OpenAI/ChatGPT + others), without requiring user-managed servers or subscriptions. Users buy the plugin once, and the companion app runs free/local.
- Concrete milestones (based on current repo state):
  - Current baseline: plugin <-> local FastAPI server (app.py) for sync, apply, and context export; headless companion scaffold in companion/ with adapter registry and CLI.
  - Milestone 1: unify "app" and app.py by exposing a stable local API surface for AI calls (chat/complete/stream) and wiring plugin UI to it.
  - Milestone 2: implement adapter contract in companion/ for local models + OpenAI/ChatGPT + others, with config-driven selection and health reporting.
  - Milestone 3: local-only UX flow in plugin (Connect, model select, status, prompts, receipts) with no external hosting or accounts.
  - Milestone 4: packaging and onboarding (one-click local app start, auto-discovery, zero-config defaults, offline-first).
  - Milestone 5: multi‑AI workflows (Full MLEs) — support running multiple models side‑by‑side with unified UX, if provider policies allow.

## Current Plugin Capabilities (Studio)
- Connection + discovery: local-only health check, primary lease, sync, heartbeat, status polling, auto-reconnect, manual URL override.
- Transactions: long-poll wait, fetch/preview/apply, receipts, rollback safety, undo (ChangeHistoryService).
- Actions supported: createInstance, setProperty/setProperties, deleteInstance (guarded), rename/move, setAttribute/setAttributes, editScript (replace/append/prepend/replaceRange/insertBefore/insertAfter), chunked edits.
- Safety: allow/deny action lists, protected paths, delete guard + one-time approval.
- Context export: scoped by placeId + studioSessionId with projectKey strategy; diff exports, full export on demand, diff cache clear, fetch missing sources helper.
- UI/UX: auto vs manual mode, terminal/chat layout modes, logs panel + stress test, audit log preview, preview panel, URL select/copy.
- Multi‑Studio scope: sessions are scoped by placeId + studioSessionId; safe across multiple Studio windows and multiple places within the same game universe, but not intended for cross-experience sharing.

## Work Log (Recent)
- Created a Codex-focused v1 repo and drafted specs in `SPECS/`.
- Added Codex bridge endpoints, file-queue watcher, and plugin UI hooks for "Send to Codex" in the v1 repo.
- Added a local launcher UI (`codex_launcher.py`) that starts the server + Codex worker, shows status, and stops them on exit.
- Added a macOS app wrapper (`PersponifyCodexLauncher.app`) that launches the UI via double-click without AppleScript.
- Added Codex worker daemon (`codex_worker.py`) and spec (`SPECS/WORKER_DAEMON.md`).
- Added `python app.py --host 127.0.0.1 --port 3030 --reload` entrypoint to start the local server without `python -m uvicorn`.
- Added Context tools: Clear Diff Cache + Fetch Missing Sources buttons and handlers.
- Added context endpoints in plugin transport: /context/missing and /context/script.
- Executor upgrades: diff-aware apply (skip no-op writes), auto-chunk large editScript payloads, and toggleable features in Config.
- UI stability: improved wrapping, row sizing, hover color stability, and reduced settings-write spam.
- New AI endpoints in app.py: /ai/complete, /ai/stream, /ai/models, /ai/health, /ai/reload with local companion integration.
- Local Electron app created in the workspace (`PersponifyStudioAIApp`) with chat UI, adapter compare, health panel, and local fonts.
- App bundles a local server copy (app.py + companion/), auto-starts it on app launch, and binds to 127.0.0.1 only.
- Launchers: macOS .app + start.command; Windows/Linux launchers pending (see current tasks).
- Added companion adapters (OpenAI, Anthropic, xAI, Ollama) with config-driven settings and basic availability checks.
- Added /ai/secrets endpoint + secret injection in companion service (in-memory overrides by adapter/type).
- App server bundle + config defaults updated to include new adapters and settings.
- Settings drawer added to the Electron UI with vault (safeStorage-backed) and adapter config controls (enable/disable, defaults, model/base URL).
- Streaming now respects adapter capabilities in the chat UI.
- Added multi-adapter management in the app (add/remove adapters, per-adapter vault overrides, disabled adapters are non-selectable).
- Updated the app UI copy to avoid local filesystem paths, added clearer server status messaging, and tightened text wrapping in chat and settings.
- Launch scripts updated to avoid hard-coded user paths; added Electron build configuration for Mac/Win/Linux packaging.
- Installed electron-builder dependencies and produced macOS DMG build at `PersponifyStudioAIApp/dist`.
- Eliminated deprecated inflight/glob@7 in build tooling via overrides; added single-instance lock and cache cleanup on quit.
- App now quits fully on window close (macOS included) to avoid background processes.
- Added adapter model discovery endpoint (/ai/adapter_models) and Settings UI to fetch available models per adapter (dynamic per key).
- Added MoE orchestration endpoints (/ai/moe/complete, /ai/moe/stream) with multi-expert merge and MoE toggle in the app UI; rebuilt macOS DMG.
- Added MoE settings (max experts, timeout, merge adapter), auto model refresh on key sync, and a plugin connection indicator in the app sidebar.
- Added adaptive MoE caps based on local vs remote adapters and CPU, plus OpenAI-compatible adapters (OpenRouter/Groq/Mistral/Together) with extra headers support.
- Added build icons (.icns/.ico/.png) and a simple OS-detect download page scaffold at `PersponifyStudioAIApp/downloads/index.html`; rebuilt macOS DMG with custom icon.
- Deferred auto-updater integration to avoid deprecated dependencies; will revisit once update hosting and signing are in place.
- Added keyless local OpenAI-compatible support (LM Studio/llama.cpp/vLLM/textgen) by allowing local base URLs without API keys and marking them as local resources.
- Added MoE learning: persistent per-adapter stats, adaptive routing in auto mode, feedback endpoint, and master/expert role guidance for MoE responses.
- Mitigated Electron GPU crashes by disabling hardware acceleration and added start.command fallback to open the app when npm/node is unavailable.
- Added renderer-to-main logging bridge and model error surfacing; app now logs renderer errors in `~/Library/Application Support/studio-ai-app/app.log` and shows model fetch errors in the model dropdown.
- Disabled echo adapters for MoE and added UI gating/error messaging so MoE only runs with real models.
- Added per-chat memory (summary) with toggle/UI and a new `/ai/memory/summarize` endpoint so MoE and single-model chats can persist a compact memory locally.
- Added Quick setup key auto-detection and advanced-settings toggle to reduce settings clutter; local adapters are auto-enabled by default and selection now prefers available adapters.
- Added Reset chat + Clear all chats controls, and auto-refresh model list on startup for smoother onboarding.
- Added automatic vault sync on startup so saved keys persist without re-entry.
- Added availability probes for local OpenAI-compatible and Ollama adapters so unavailable local servers are not selected.
- Added auto-managed Ollama runtime controls (auto-start, install helper, status) to the Studio AI app.
- Quick setup is always visible in Settings so keys can be added without enabling advanced sections.
- Issues encountered: Settings only showed Adapters because Quick setup was gated behind advanced; desktop app copy was stale, causing updates to appear missing. Resolved by rebuilding and replacing the Desktop app with the latest build.
- Fixed Ollama auto-start on macOS by launching the app bundle (Ollama.app) when the CLI is missing; added clearer start/stop error messaging.
- Added local runtime discovery for OpenAI-compatible servers (LM Studio/llama.cpp/vLLM/textgen) and UI status list in the app settings.
- Prevented Echo from being selected when advanced is off; prompt now warns if only Echo is available.
- Issue tracked: keys saved via provider defaults did not surface a default adapter; now auto-enables a default adapter for that provider.
- Multi-adapter selection now only applies to MoE (disabled outside MoE) and MoE requests pass selected experts to the server.
- Latest: local runtime status list shows LM Studio/llama.cpp/vLLM/textgen endpoints; Ollama auto-start uses the app bundle on macOS; Desktop and Applications app copies are kept in sync after each build.
- Ops note: performed a safe quit of user apps (keeping Roblox Player + Terminal) to free system resources during troubleshooting.

## Current State (Codex Server + Launcher)
- Server upgrades in `app.py`: Codex bridge validation + policy (risk score, deny lists, protected roots, max actions), queue limits, job TTL sweep, auto-repair loop, audit ledger, diagnostics, context deltas, focus pack, memory storage, context events, semantic tagging/dependency extraction, `/context/semantic` endpoint, SQLite persistence for context/audit/events/semantic, and a reconcile loop that refreshes cache from disk/DB.
- New config/env toggles in `app.py`: `PERSPONIFY_SQLITE_ENABLED`, `PERSPONIFY_SQLITE_PATH`, `PERSPONIFY_SEMANTIC_ENABLED`, `PERSPONIFY_SEMANTIC_*`, `PERSPONIFY_RECONCILE_INTERVAL_SEC`, plus Codex policy/risk/size limits already in place.
- Queue state persistence: `codex_queue/queue_state.json` plus SQLite fallback to survive restarts.
- Launcher UI (`codex_launcher.py`): green/black theme; status lines; repo display; footer with build label `Build: 2025-12-23-CODEX-02`; buttons moved to footer; custom label-based buttons to force dark theme (Tk buttons were white); Set Repo / Restart / Restart Server; restart can re-exec into a new repo; restart-server keeps worker running; PATH injection for `/usr/local/bin` and `/opt/homebrew/bin`; optional headless mode (`--nogui` or `PERSPONIFY_LAUNCHER_NOGUI=1`); headless prints status lines to stdout.
- Relocatable launcher app: `PersponifyCodexLauncher.app` uses `launcher_bootstrap.py` and `launcher.json` in the OS support/config dir (`~/Library/Application Support/PersponifyCodex` on macOS; `%APPDATA%\\PersponifyCodex` on Windows; `${XDG_CONFIG_HOME:-~/.config}/PersponifyCodex` on Linux) to auto-detect repo and python; includes `LAUNCHER_README.txt`; auto-detect checks `PERSPONIFY_CODEX_REPO`, app parent folders, and common user dirs; prompts user to pick repo if not found; writes config automatically.
- Python/Tk: system `/usr/bin/python3` crashes Tk (Tcl/Tk 8.5) on this Mac; launcher probes Tk by creating a root window before selecting Python; falls back to headless if no Tk-capable Python found; config explicitly set to `/usr/local/bin/python3.11` (confirmed Tk OK).
- Launcher config: `launcher.json` in the OS support/config dir; stores `repoPath` and `pythonPath` without hard-coding user-specific paths in repo files.
- Worker: `codex_worker.py` auto-detects `codex` CLI via PATH; fallback to `/usr/local/bin/codex` or `/opt/homebrew/bin/codex`.
- Tests run (local): `/health`, `/register`, `/sync`, `/context/export`, `/context/summary`, `/context/semantic`, `/codex/job`, SQLite snapshot checks on a temp port; regex fix for `GetService`/`require` confirmed in semantic output.
- Logs: `codex_worker.log` and `codex_server.log` exist in repo root; cleared on 2025-12-23 after earlier worker errors; old launcher backup removed (`PersponifyCodexLauncher.app.bak` deleted).
- Plugin status: not started yet; server is on port 3030 and ready for plugin integration; current `/status` shows `primary.alive=false` until Studio connects.

## Pending / In Progress (Roblox Plugin)
- User requested a brand new, separate Codex-focused plugin (not a rename of the old plugin). Work was paused mid-way because we started copying modules from the old plugin for convenience.
- Changes already made in the local `PersponifyAITest` repo before the stop:
  - Copied core files from `context_scripts/ReplicatedStorage__PersponifyStudioAI__*` to new `context_scripts/ReplicatedStorage__PersponifyCodex__*` for a fresh plugin scaffold.
  - Updated `ReplicatedStorage__PersponifyCodex__Core__Config.lua` with Codex branding, base URL default to 3030, status poll interval, Codex prompt/config table, and protected root rename.
  - Updated `ReplicatedStorage__PersponifyCodex__Core__Version.lua` branding/internal root/build id.
  - Updated `ReplicatedStorage__PersponifyCodex__Core__Transport.lua` endpoints to include Codex endpoints (`/codex/job`, `/codex/status`, `/codex/compile`, `/diagnostics`, `/audit/ledger`, `/context/semantic`, `/context/memory`), plus helper methods to call them.
- User clarified they want a fundamentally different plugin (not continuing the copied scaffold), so these changes should be reconsidered or discarded when resuming.

## Potential Future Updates
- Mobile companion app (iOS/Android) would require a separate client (likely Flutter/React Native) and a local pairing flow; cannot run the Python server on-device like desktop.
