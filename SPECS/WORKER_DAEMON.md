# Codex Worker Daemon (v1)

## Purpose
Watches the `codex_queue/jobs` folder, runs Codex, and writes responses to `codex_queue/responses`.

## Command
```
python3 codex_worker.py
```

Optional override:
```
python3 codex_worker.py --command "<codex command template>"
```

## Command Template
Supports placeholders:
- `{prompt_file}` path to job prompt file
- `{response_file}` path to write Codex output
- `{job_id}` job ID
- `{context_path}` path to cached context JSON

Default (auto-detected if Codex is on PATH):
```
codex exec --skip-git-repo-check --output-schema codex_response.schema.json --output-last-message {response_file} -C <repo> -
```

Example override:
```
PERSPONIFY_CODEX_CMD="codex exec --output-last-message {response_file} --output-schema codex_response.schema.json -"
```

## Output
Codex should write JSON with:
```
{"jobId": "...", "actions": [...], "summary": "..."}
```

Errors are written as a response with `ok: false` and empty actions.
