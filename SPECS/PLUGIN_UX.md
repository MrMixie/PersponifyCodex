# Plugin UX Spec (v1)

## Goals
- Codex-first workflow without breaking current Studio flow.
- Minimal friction for auto-apply users.

## New UI Elements
- "Send to Codex" button (manual trigger).
- Codex status line (last job, last error, context version).
- Optional "Auto-send" toggle.

## Auto vs Manual
- Auto mode: Codex jobs enqueue tx; plugin auto-applies.
- Manual mode: tx stays in preview until Apply.

## Safety UX
- Risk badge if actions include delete or large edits.
- "Safe mode" toggle to block high-risk actions.

## Context UX
- Display contextId + version.
- Button to refresh context and rebuild cache.

## Error UX
- Show last Codex error with a retry button.
- Link to local diagnostics endpoint.
