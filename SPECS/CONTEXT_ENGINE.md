# Context Engine Spec (v1)

## Goals
- Avoid resending full hierarchy each time.
- Provide stable context IDs and versioned deltas.
- Support on-demand fetch of missing sources.

## Core Concepts
- `contextId`: stable ID per project scope (placeId + studioSessionId).
- `contextVersion`: monotonic version for each snapshot.
- `hashGraph`: per-node and per-script hashes for diffing.

## Server Cache Model
- Store a canonical in-memory model:
  - `tree`: nodes keyed by path
  - `scripts`: source + hash
  - `meta`: last export time, counts, scope
- Persist to disk (JSON or SQLite) for crash recovery.

## Export Flow
1) Plugin exports diffs only (changed scripts and tree deltas).
2) Server updates cache and bumps `contextVersion`.
3) Codex jobs include only changed elements + summary.

## Delta Format (plugin -> server)
```json
{
  "contextId": "p_123__s_abc",
  "contextVersion": 42,
  "treeDelta": {
    "added": ["path1"],
    "removed": ["path2"],
    "updated": ["path3"]
  },
  "scriptDelta": {
    "changed": ["pathA"],
    "removed": ["pathB"]
  }
}
```

## On-Demand Fetch
- Codex can request missing script sources by path.
- Server returns source from cache or asks plugin via `/context/script`.

## Hashing
- Use cheap fingerprint for quick diffs.
- Optionally use SHA-1 for cross-check in high-risk actions.

## Conflict Detection
- Actions include `expectedHash` for target script.
- If hash mismatch, server asks Codex to rebase or aborts apply.

## Context Summary
- Maintain a short, updated summary per project.
- Inject into Codex job instead of full history.
