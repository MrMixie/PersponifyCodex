#!/usr/bin/env python3
"""Codex worker daemon for Persponify Codex jobs."""
# Note: queue layout here must match the server's enqueue/receipt flow.

from __future__ import annotations

import argparse
import atexit
import json
import os
import shlex
import shutil
import subprocess
import time
from pathlib import Path
from typing import Dict, Optional, Tuple, TextIO


ROOT = Path(__file__).resolve().parent
_LOCK_FILE: Optional[TextIO] = None


def _queue_root() -> Path:
    return Path(os.environ.get("PERSPONIFY_CODEX_QUEUE", "codex_queue")).resolve()


def _ensure_dirs(root: Path) -> Dict[str, Path]:
    dirs = {
        "root": root,
        "jobs": root / "jobs",
        "responses": root / "responses",
        "acks": root / "acks",
        "errors": root / "errors",
        "tmp": root / "tmp",
    }
    for path in dirs.values():
        path.mkdir(parents=True, exist_ok=True)
    return dirs


def _pid_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False
    return True


def _acquire_lock_fallback(lock_path: Path) -> Optional[TextIO]:
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            existing = lock_path.read_text().strip()
            pid = int(existing) if existing.isdigit() else 0
        except Exception:
            pid = 0
        if pid and _pid_running(pid):
            print(f"Another codex_worker is already running (pid {pid}).")
            return None
        try:
            lock_path.unlink()
        except Exception:
            pass
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except Exception:
            print("Another codex_worker is already running.")
            return None
    lock_file = os.fdopen(fd, "w")
    lock_file.write(str(os.getpid()))
    lock_file.flush()
    return lock_file


def _acquire_lock(root: Path) -> Optional[TextIO]:
    lock_path = root / "worker.lock"
    try:
        import fcntl  # type: ignore
    except Exception:
        return _acquire_lock_fallback(lock_path)

    lock_file = open(lock_path, "a+", encoding="utf-8")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock_file.seek(0)
        existing = lock_file.read().strip() or "unknown"
        print(f"Another codex_worker is already running (pid {existing}).")
        lock_file.close()
        return None

    lock_file.seek(0)
    lock_file.truncate()
    lock_file.write(str(os.getpid()))
    lock_file.flush()
    return lock_file


def _register_lock_cleanup(lock_path: Path, lock_file: Optional[TextIO]) -> None:
    def _cleanup() -> None:
        try:
            if lock_file and not lock_file.closed:
                lock_file.close()
        except Exception:
            pass
        try:
            if lock_path.exists():
                lock_path.unlink()
        except Exception:
            pass

    atexit.register(_cleanup)


def _load_json(path: Path) -> Optional[Dict]:
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def _write_atomic_json(path: Path, data: Dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    tmp.replace(path)


def _extract_json(text: str) -> Optional[Dict]:
    # Codex sometimes wraps JSON with extra text; peel off the first object.
    text = text.strip()
    if not text:
        return None
    if text.startswith("{") and text.endswith("}"):
        try:
            return json.loads(text)
        except Exception:
            return None
    # Attempt to extract the first JSON object from a mixed response.
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except Exception:
            return None
    return None


def _build_prompt(job: Dict) -> str:
    lines = []
    system = job.get("system")
    if system:
        lines.append(str(system))

    lines.append(
        "Return JSON only. Format: {\"jobId\":..., \"actions\":[...], \"summary\":...}."
    )
    lines.append(
        "Actions must be an array of objects. Each action MUST include a string field \"type\"."
    )
    lines.append("Use only these action types:")
    lines.append(
        "createInstance, setProperty, setProperties, setAttribute, setAttributes, "
        "deleteInstance, rename, move, editScript"
    )
    lines.append(
        "Paths must be absolute Roblox paths like \"game/ReplicatedStorage/Folder/Script\" "
        "or \"game/Workspace/Part\"."
    )
    lines.append(
        "createInstance requires: type, parentPath, className, name."
    )
    lines.append(
        "editScript requires: type, path, mode (replace|append|prepend|replaceRange|insertBefore|insertAfter), "
        "and source (or chunks)."
    )
    lines.append(
        "setProperty requires: type, path, property, value. "
        "setProperties requires: type, path, properties."
    )
    lines.append(
        "setAttribute requires: type, path, attribute, value. "
        "setAttributes requires: type, path, attributes."
    )
    lines.append(
        "rename requires: type, path, newName. move requires: type, path, newParentPath."
    )

    context = job.get("context") or {}
    summary = context.get("summary") or {}
    lines.append("Context summary:")
    lines.append(json.dumps(summary, indent=2, sort_keys=True))

    missing = context.get("missing") or []
    if missing:
        lines.append("Missing sources: " + ", ".join(str(m) for m in missing[:20]))

    context_ref = job.get("contextRef") or {}
    context_path = context_ref.get("path")
    if context_path:
        lines.append(f"Context file path: {context_path}")

    lines.append("User request:")
    lines.append(str(job.get("prompt") or ""))

    return "\n\n".join(lines).strip()


def _run_command(
    cmd_template: str,
    prompt: str,
    tmp_dir: Path,
    job_id: str,
    timeout_sec: float,
    context_path: Optional[str],
    schema_path: Optional[str],
    repo_root: Optional[str],
) -> Tuple[str, str]:
    prompt_path = tmp_dir / f"prompt_{job_id}.txt"
    response_path = tmp_dir / f"response_{job_id}.txt"
    prompt_path.write_text(prompt)

    expanded = cmd_template.format(
        prompt_file=str(prompt_path),
        response_file=str(response_path),
        job_id=job_id,
        context_path=str(context_path or ""),
        schema_path=str(schema_path or ""),
        repo_root=str(repo_root or ""),
    )
    args = shlex.split(expanded)

    proc = subprocess.Popen(
        args,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    out, err = proc.communicate(prompt, timeout=timeout_sec)

    if response_path.exists():
        out = response_path.read_text().strip()

    if proc.returncode != 0:
        raise RuntimeError(err.strip() or f"Codex command failed: {proc.returncode}")

    return out, err.strip()


def _process_job(
    job: Dict,
    dirs: Dict[str, Path],
    cmd_template: str,
    timeout_sec: float,
    schema_path: Optional[str],
    repo_root: Optional[str],
) -> Dict:
    job_id = str(job.get("jobId") or "")
    context_ref = job.get("contextRef") or {}
    context_path = context_ref.get("path")

    prompt = _build_prompt(job)
    output, stderr = _run_command(
        cmd_template,
        prompt,
        dirs["tmp"],
        job_id,
        timeout_sec,
        context_path,
        schema_path,
        repo_root,
    )
    data = _extract_json(output)
    if not data:
        raise RuntimeError("Codex output was not valid JSON")
    data["jobId"] = job_id
    return data


def main() -> int:
    parser = argparse.ArgumentParser(description="Persponify Codex worker daemon")
    parser.add_argument("--queue", default=str(_queue_root()), help="Queue root directory")
    parser.add_argument("--command", default=os.environ.get("PERSPONIFY_CODEX_CMD", ""))
    parser.add_argument("--schema", default=str(ROOT / "codex_response.schema.json"))
    parser.add_argument(
        "--use-schema",
        action="store_true",
        default=os.environ.get("PERSPONIFY_CODEX_USE_SCHEMA", "0") == "1",
    )
    parser.add_argument("--poll", type=float, default=1.0)
    parser.add_argument("--timeout", type=float, default=240.0)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    root = Path(args.queue).resolve()
    dirs = _ensure_dirs(root)
    lock_path = dirs["root"] / "worker.lock"
    lock_file = _acquire_lock(dirs["root"])
    if not lock_file:
        return 1
    global _LOCK_FILE
    _LOCK_FILE = lock_file
    _register_lock_cleanup(lock_path, lock_file)

    schema_path = str(Path(args.schema).resolve()) if args.use_schema else ""
    repo_root = str(ROOT)

    cmd_template = args.command.strip()
    if not cmd_template:
        codex_bin = shutil.which("codex")
        if not codex_bin:
            candidates = [
                "/usr/local/bin/codex",
                "/opt/homebrew/bin/codex",
                str(Path.home() / ".local" / "bin" / "codex"),
                str(Path.home() / ".codex" / "bin" / "codex"),
            ]
            if os.name == "nt":
                base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
                if base:
                    candidates.append(str(Path(base) / "Programs" / "Codex" / "codex.exe"))
                prog = os.environ.get("PROGRAMFILES")
                if prog:
                    candidates.append(str(Path(prog) / "Codex" / "codex.exe"))
                prog_x86 = os.environ.get("PROGRAMFILES(X86)")
                if prog_x86:
                    candidates.append(str(Path(prog_x86) / "Codex" / "codex.exe"))
            for candidate in candidates:
                if Path(candidate).exists():
                    codex_bin = candidate
                    break
        if not codex_bin:
            print("Missing Codex command. Set --command or PERSPONIFY_CODEX_CMD.")
            return 1
        if args.use_schema and schema_path:
            cmd_template = (
                f"{codex_bin} exec --skip-git-repo-check --output-schema {schema_path} "
                f"--output-last-message {{response_file}} -C {repo_root} -"
            )
        else:
            cmd_template = (
                f"{codex_bin} exec --skip-git-repo-check "
                f"--output-last-message {{response_file}} -C {repo_root} -"
            )

    while True:
        jobs = sorted(dirs["jobs"].glob("job_*.json"))
        for job_path in jobs:
            job_id = job_path.stem.replace("job_", "", 1)
            ack_path = dirs["acks"] / f"job_{job_id}.json"
            resp_path = dirs["responses"] / f"job_{job_id}.json"
            if ack_path.exists() or resp_path.exists():
                continue

            job = _load_json(job_path)
            if not job:
                _write_atomic_json(
                    dirs["errors"] / f"job_{job_id}.json",
                    {"ok": False, "error": "Failed to parse job"},
                )
                _write_atomic_json(ack_path, {"ok": False, "error": "Failed to parse job"})
                continue

            try:
                response = _process_job(job, dirs, cmd_template, args.timeout, schema_path, repo_root)
                _write_atomic_json(resp_path, response)
            except Exception as exc:
                err_msg = str(exc)
                _write_atomic_json(
                    resp_path,
                    {
                        "jobId": job_id,
                        "ok": False,
                        "summary": "Codex error",
                        "actions": [],
                        "errors": [err_msg],
                    },
                )

        if args.once:
            break
        time.sleep(args.poll)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
