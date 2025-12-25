# Observability Spec (v1)

## Goals
- Make every action traceable.
- Enable replay and debugging.
- Provide a minimal local dashboard view.

## Logs
- Structured JSON logs with timestamps and scopes.
- Separate channels: server, codex-bridge, apply, context.

## Audit Ledger
- Persist every tx, receipt, and error in a local ledger.
- Include action counts, paths touched, and duration.

## Metrics
- Queue depth, apply latency, error rate, context export cadence.
- MoE stats for adapter performance (if enabled).

## Replay
- Allow re-running a past tx or restoring a prior snapshot.

## Diagnostics Endpoint
- `/diagnostics` returns health, queue stats, last error summary.
