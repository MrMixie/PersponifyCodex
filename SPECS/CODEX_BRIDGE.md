# Codex Bridge Spec (v1)

## Goals
- Local-only bridge between Studio plugin and Codex.
- Deterministic job format with strict schema validation.
- No network dependency; file-based queue with atomic writes.

## Directory Layout
- `codex_queue/`
  - `jobs/`           (server writes jobs)
  - `responses/`      (Codex writes responses)
  - `acks/`           (server writes ack after consuming)
  - `errors/`         (optional: server writes parse/validation failures)

## Job Lifecycle
1) Server writes a job to `codex_queue/jobs/job_<uuid>.json`.
2) Codex daemon picks up the job, processes it, writes
   `codex_queue/responses/job_<uuid>.json`.
3) Server consumes the response, enqueues tx actions, writes
   `codex_queue/acks/job_<uuid>.json`.
4) Optional: server moves failed jobs to `errors/` with reason.

## Job Schema (server -> Codex)
```json
{
  "jobId": "uuid",
  "createdAt": 1730000000,
  "contextId": "p_123__s_abc",
  "contextVersion": 42,
  "mode": "auto" ,
  "intent": "edit|create|debug|refactor",
  "prompt": "user request",
  "system": "system prompt",
  "scope": {
    "placeId": 123,
    "studioSessionId": "abc",
    "projectKey": "p_123__s_abc"
  },
  "context": {
    "summary": "short project summary",
    "changes": {"scripts": [], "tree": []},
    "missing": ["game/ReplicatedStorage/Foo"]
  },
  "policy": {
    "riskProfile": "safe|normal|power",
    "allowAutoApply": true,
    "protectedRoots": ["ReplicatedStorage/PersponifyStudioAI"]
  },
  "capabilities": {
    "actions": ["editScript","setProperty","createInstance"],
    "maxSourceBytes": 250000
  }
}
```

## Response Schema (Codex -> server)
```json
{
  "jobId": "uuid",
  "ok": true,
  "summary": "what changed",
  "plan": ["step1", "step2"],
  "actions": [
    {"type":"editScript", "path":"game/...", "mode":"replace", "source":"..."}
  ],
  "notes": [],
  "errors": []
}
```

## Validation Rules
- Reject if `jobId` does not match a pending job.
- Reject if `actions` are not a list or are missing `type`.
- Reject if `contextVersion` is stale unless `allowStale` is true.

## Auto Apply
- Server enqueues actions as tx. Plugin auto mode applies immediately.
- Manual mode: server still enqueues, plugin uses Preview/Apply.

## Retry/Timeout
- Jobs have TTL (e.g. 5 minutes). Expired jobs are marked failed.
- Codex daemon can safely retry by re-reading jobs without ack.

## Security
- Local filesystem only. No remote endpoints required.
- Job directory location configurable via env var.
