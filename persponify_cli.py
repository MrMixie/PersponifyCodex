#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import uuid
from pathlib import Path
from typing import Any, Optional
from urllib import request, error

DEFAULT_SERVER = os.environ.get("PERSPONIFY_SERVER_URL", "http://127.0.0.1:3030")
PROTOCOL_VERSION = 1
PROMPT_FILE = Path(__file__).resolve().parent / "STUDIO_GRADE_PROMPT.md"


def _http_get_json(url: str, timeout: float = 3.0) -> Optional[dict]:
    try:
        with request.urlopen(url, timeout=timeout) as resp:
            data = resp.read().decode("utf-8")
        return json.loads(data)
    except Exception:
        return None


def _http_post_json(url: str, payload: dict, timeout: float = 6.0) -> Optional[dict]:
    data = json.dumps(payload).encode("utf-8")
    req = request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
        return json.loads(body) if body else {"ok": True}
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8") if exc.fp else ""
        return {"ok": False, "error": body or str(exc)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _detect_codex_cmd(repo_root: Path) -> Optional[list[str]]:
    override = os.environ.get("PERSPONIFY_CODEX_CMD", "").strip()
    if override:
        return override.split()
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
        return None
    return [codex_bin, "exec", "--skip-git-repo-check", "-C", str(repo_root)]


def _load_prompt_file() -> str:
    if PROMPT_FILE.exists():
        return PROMPT_FILE.read_text(encoding="utf-8").strip()
    return ""


def _build_prompt(summary: Optional[dict], user_prompt: str) -> str:
    parts = ["System guidelines:", _load_prompt_file()]
    parts.append(
        "\n".join(
            [
                "Action schema rules:",
                "Use only these action types: createInstance, setProperty, setProperties, setAttribute,",
                "setAttributes, deleteInstance, rename, move, editScript.",
                'Paths must be absolute like "game/ReplicatedStorage/Folder/Script".',
                "createInstance requires: type, parentPath, className, name.",
                "editScript requires: type, path, mode, and source (or chunks).",
            ]
        )
    )
    if summary:
        parts.append("Context summary:")
        parts.append(json.dumps(summary, indent=2, sort_keys=True))
    parts.append("User request:")
    parts.append(user_prompt)
    parts.append("Assistant:")
    return "\n\n".join(p for p in parts if p).strip()


def _extract_actions_block(text: str) -> tuple[str, Optional[dict]]:
    if not text:
        return "", None
    start_tag = "<actions_json>"
    end_tag = "</actions_json>"
    start = text.find(start_tag)
    end = text.find(end_tag)
    if start >= 0 and end > start:
        payload = text[start + len(start_tag) : end].strip()
        cleaned = (text[:start] + text[end + len(end_tag) :]).strip()
        try:
            return cleaned, json.loads(payload)
        except Exception:
            return cleaned, None
    return text.strip(), None


def _should_apply(summary_text: str) -> bool:
    return "apply status: ready" in (summary_text or "").lower()


def _run_codex(cmd: list[str], prompt: str) -> str:
    with tempfile.TemporaryDirectory() as tmpdir:
        response_path = Path(tmpdir) / f"response_{uuid.uuid4().hex}.txt"
        args = cmd + ["--output-last-message", str(response_path), prompt]
        proc = subprocess.run(args, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or f"Codex failed ({proc.returncode})")
        if response_path.exists():
            return response_path.read_text(encoding="utf-8").strip()
        return proc.stdout.strip()


def _enqueue_actions(base_url: str, actions: list[dict]) -> Optional[dict]:
    tx = {
        "protocolVersion": PROTOCOL_VERSION,
        "transactionId": f"TX_CLI_{uuid.uuid4()}",
        "actions": actions,
    }
    return _http_post_json(f"{base_url}/enqueue", {"tx": tx})


def _print_status(base_url: str) -> None:
    status = _http_get_json(f"{base_url}/status")
    if not status or not status.get("ok"):
        print("Server: offline")
        return
    primary = status.get("primary") or {}
    alive = primary.get("alive") is True
    queue = status.get("queuePending")
    print(f"Server: online | Plugin: {'connected' if alive else 'disconnected'} | Queue: {queue}")


def _prompt_loop(base_url: str, repo_root: Path) -> int:
    cmd = _detect_codex_cmd(repo_root)
    if not cmd:
        print("Codex CLI not found. Install Codex and run `codex login`.")
        return 1

    _print_status(base_url)
    while True:
        try:
            user_text = input("You> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye.")
            return 0
        if not user_text:
            continue
        if user_text.lower() in ("exit", "quit", "/exit", "/quit"):
            print("Bye.")
            return 0

        summary = _http_get_json(f"{base_url}/context/summary")
        prompt = _build_prompt(summary, user_text)
        try:
            output = _run_codex(cmd, prompt)
        except Exception as exc:
            print(f"Codex error: {exc}")
            continue

        cleaned, payload = _extract_actions_block(output)
        if cleaned:
            print(f"Codex> {cleaned}\n")

        if not isinstance(payload, dict):
            continue
        summary_text = payload.get("summary") or cleaned
        plan_steps = payload.get("steps") if isinstance(payload.get("steps"), list) else None
        actions = payload.get("actions") if isinstance(payload.get("actions"), list) else None

        if plan_steps:
            for idx, step in enumerate(plan_steps, start=1):
                step_actions = step.get("actions") if isinstance(step.get("actions"), list) else None
                title = step.get("title") or f"Step {idx}"
                if not step_actions:
                    print(f"Step {idx}: {title} (no actions)")
                    continue
                if not _should_apply(summary_text):
                    confirm = input(f"Apply step {idx}/{len(plan_steps)} ({title})? [y/N] ").strip().lower()
                    if confirm not in ("y", "yes"):
                        print("Skipped.")
                        continue
                res = _enqueue_actions(base_url, step_actions)
                print(f"Queued step {idx}/{len(plan_steps)} ({title}): {res}")
            continue

        if actions:
            if not _should_apply(summary_text):
                confirm = input("Apply these changes now? [y/N] ").strip().lower()
                if confirm not in ("y", "yes"):
                    print("Skipped.")
                    continue
            res = _enqueue_actions(base_url, actions)
            print(f"Queued: {res}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Persponify Codex CLI (Studio-grade, CLI-first workflow)."
    )
    parser.add_argument("--server", default=DEFAULT_SERVER, help="Server base URL")
    parser.add_argument(
        "--repo",
        default=str(Path(__file__).resolve().parent),
        help="Repo root for Codex context",
    )
    args = parser.parse_args()
    base_url = args.server.rstrip("/")
    repo_root = Path(args.repo).expanduser().resolve()
    return _prompt_loop(base_url, repo_root)


if __name__ == "__main__":
    raise SystemExit(main())
