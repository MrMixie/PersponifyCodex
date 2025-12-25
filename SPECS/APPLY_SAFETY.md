# Apply Safety Spec (v1)

## Goals
- Prevent destructive or invalid edits.
- Provide clear errors and auto-repair loop.
- Support auto-apply with risk controls.

## Preflight Checks
- Validate action shapes (type, path, required fields).
- Enforce allow/deny lists.
- Enforce protected roots and internal root protection.
- Check source size caps and action count caps.

## Risk Tiers
- Safe: setProperty, setAttribute, editScript in small scope.
- Normal: createInstance, rename/move, editScript large changes.
- High: deleteInstance, reparent across services, bulk changes.

## Two-Phase Apply
1) Preflight and hash checks (server-side).
2) Enqueue tx if safe, otherwise require approval or Codex rework.

## Hash Checks
- Actions can include `expectedHash`.
- If mismatch, reject and send error to Codex for rebase.

## Rollback
- If apply fails, record receipt with errors.
- Optionally enqueue rollback tx if safe to do so.

## Auto-Repair Loop
- On failure, server packages error details + context delta and sends back to Codex.

## Telemetry
- Track error rates by action type and path.
- Use telemetry to adjust risk thresholds.
