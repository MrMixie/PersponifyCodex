#!/usr/bin/env python3
"""Simple local launcher UI for the Persponify Codex server."""
# Note: keep this file UI-focused; server logic stays in app.py.

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import uuid
import shlex
import signal
from pathlib import Path
import shutil
from typing import Optional

try:
    from urllib.request import urlopen, Request
except ImportError:  # pragma: no cover - very old Python
    urlopen = None
    Request = None

HOST = "127.0.0.1"
PORT = 3030
POLL_SEC = 2.0
LAUNCHER_BUILD = "2025-12-23-CODEX-02"
CODEX_TIMEOUT_SEC = float(os.environ.get("PERSPONIFY_CODEX_TIMEOUT_SEC", "240"))
CHAT_ENABLED = os.environ.get("PERSPONIFY_LAUNCHER_CHAT", "0") == "1"

MEMORY_MODES = ["Off", "Brief", "Full"]
SCOPE_MODES = ["Game", "Place", "Session", "Manual"]

THEME = {
    "bg": "#040605",
    "panel": "#080c0a",
    "text": "#d2f8de",
    "muted": "#7fb59c",
    "green": "#2df2a3",
    "green_dim": "#0e4a2d",
    "red": "#ff6b6b",
    "yellow": "#ffd166",
    "border": "#123524",
}

ROOT = Path(__file__).resolve().parent
SERVER_PATH = ROOT / "app.py"
WORKER_PATH = ROOT / "codex_worker.py"
EXTRA_PATHS = [
    "/usr/local/bin",
    "/opt/homebrew/bin",
    str(Path.home() / ".local" / "bin"),
    str(Path.home() / ".codex" / "bin"),
]


def _support_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "PersponifyCodex"
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA")
        if base:
            return Path(base) / "PersponifyCodex"
        return Path.home() / "AppData" / "Roaming" / "PersponifyCodex"
    base = os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
    return Path(base) / "PersponifyCodex"


# Keep state outside the repo so updates don't wipe chats.
SUPPORT_DIR = _support_dir()
CONFIG_PATH = SUPPORT_DIR / "launcher.json"
STATE_PATH = SUPPORT_DIR / "launcher_state.json"
LOG_PATH = SUPPORT_DIR / "codex_server.log"
WORKER_LOG_PATH = SUPPORT_DIR / "codex_worker.log"


def _looks_like_repo(path: Path) -> bool:
    return (path / "codex_launcher.py").exists() and (path / "app.py").exists()


def _read_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(CONFIG_PATH.read_text())
    except Exception:
        return {}


def _write_config(repo_path: Path) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    config = _read_config()
    config["repoPath"] = str(repo_path)
    CONFIG_PATH.write_text(json.dumps(config, indent=2))


def _read_state() -> dict:
    if not STATE_PATH.exists():
        return {
            "chats": {},
            "lastChatId": None,
            "lastChatByScope": {},
            "scopeMode": "Place",
            "chatCounters": {},
        }
    try:
        data = json.loads(STATE_PATH.read_text())
    except Exception:
        return {
            "chats": {},
            "lastChatId": None,
            "lastChatByScope": {},
            "scopeMode": "Place",
            "chatCounters": {},
        }
    if not isinstance(data, dict):
        return {
            "chats": {},
            "lastChatId": None,
            "lastChatByScope": {},
            "scopeMode": "Place",
            "chatCounters": {},
        }
    data.setdefault("chats", {})
    data.setdefault("lastChatId", None)
    data.setdefault("lastChatByScope", {})
    data.setdefault("scopeMode", "Place")
    data.setdefault("chatCounters", {})
    if not isinstance(data["chats"], dict):
        data["chats"] = {}
    if not isinstance(data["lastChatByScope"], dict):
        data["lastChatByScope"] = {}
    if not isinstance(data["chatCounters"], dict):
        data["chatCounters"] = {}
    return data


def _write_state(data: dict) -> None:
    SUPPORT_DIR.mkdir(parents=True, exist_ok=True)
    try:
        STATE_PATH.write_text(json.dumps(data, indent=2))
    except Exception:
        pass


def _select_restart_python(config: Optional[dict]) -> str:
    if config:
        override = config.get("pythonPath")
        if isinstance(override, str) and override and Path(override).exists():
            return override
    return sys.executable


def _launcher_env() -> dict:
    env = dict(os.environ)
    parts = env.get("PATH", "").split(os.pathsep) if env.get("PATH") else []
    for extra in EXTRA_PATHS:
        if extra and extra not in parts:
            parts.insert(0, extra)
    env["PATH"] = os.pathsep.join([p for p in parts if p])
    return env


def _popen_kwargs() -> dict:
    kwargs = {"env": _launcher_env()}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    return kwargs


def _terminate_process(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    if os.name == "nt":
        try:
            proc.send_signal(signal.CTRL_BREAK_EVENT)
            proc.wait(timeout=5)
            return
        except Exception:
            pass
    else:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=5)
            return
        except Exception:
            pass
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


class ServerController:
    def __init__(self) -> None:
        self.proc: Optional[subprocess.Popen] = None
        self.started_by_launcher = False
        self.log_file = None

    def is_port_open(self) -> bool:
        try:
            with socket.create_connection((HOST, PORT), timeout=0.5):
                return True
        except OSError:
            return False

    def start(self) -> None:
        if self.is_port_open():
            cleaned = _cleanup_stale_server(ROOT)
            if not cleaned and self.is_port_open():
                raise RuntimeError("Port 3030 already in use")
        if not SERVER_PATH.exists():
            raise FileNotFoundError(f"Server not found: {SERVER_PATH}")
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.log_file = open(LOG_PATH, "a", encoding="utf-8")
        cmd = [sys.executable, str(SERVER_PATH), "--host", HOST, "--port", str(PORT)]
        self.proc = subprocess.Popen(cmd, stdout=self.log_file, stderr=self.log_file, **_popen_kwargs())
        self.started_by_launcher = True

    def stop(self) -> None:
        if self.proc:
            _terminate_process(self.proc)
            self.proc = None
        elif self.is_port_open():
            _post_json("/shutdown", {"reason": "launcher_exit"}, timeout=2.0)
            for _ in range(10):
                if not self.is_port_open():
                    break
                time.sleep(0.2)
        if self.log_file:
            try:
                self.log_file.close()
            except Exception:
                pass
            self.log_file = None


class WorkerController:
    def __init__(self) -> None:
        self.proc: Optional[subprocess.Popen] = None
        self.started_by_launcher = False
        self.log_file = None

    def start(self) -> None:
        _cleanup_stale_worker(ROOT)
        if not WORKER_PATH.exists():
            raise FileNotFoundError(f"Worker not found: {WORKER_PATH}")
        WORKER_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.log_file = open(WORKER_LOG_PATH, "a", encoding="utf-8")
        cmd = [sys.executable, str(WORKER_PATH)]
        self.proc = subprocess.Popen(cmd, stdout=self.log_file, stderr=self.log_file, **_popen_kwargs())
        self.started_by_launcher = True

    def stop(self) -> None:
        if self.proc:
            _terminate_process(self.proc)
            self.proc = None
        if self.log_file:
            try:
                self.log_file.close()
            except Exception:
                pass
            self.log_file = None


class StatusState:
    def __init__(self) -> None:
        self.server = "Server: —"
        self.plugin = "Plugin: —"
        self.worker = "Worker: —"
        self.codex = "Codex: —"
        self.last_error = ""


def _fetch_json(path: str) -> Optional[dict]:
    if urlopen is None:
        return None
    url = f"http://{HOST}:{PORT}{path}"
    try:
        with urlopen(url, timeout=1.5) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def _post_json(path: str, payload: dict, timeout: float = 6.0) -> Optional[dict]:
    if urlopen is None or Request is None:
        return None
    url = f"http://{HOST}:{PORT}{path}"
    body = json.dumps(payload).encode("utf-8")
    req = Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def _looks_like_persponify_health(data: Optional[dict]) -> bool:
    if not data or not isinstance(data, dict):
        return False
    if data.get("serverName") == "PersponifyCodex":
        return True
    if not data.get("ok"):
        return False
    endpoints = data.get("endpoints") or {}
    meta = data.get("meta") or {}
    return "codex_job" in endpoints and "context_export" in endpoints and "codexQueueDir" in meta


def _port_open() -> bool:
    try:
        with socket.create_connection((HOST, PORT), timeout=0.5):
            return True
    except OSError:
        return False


def _iter_processes() -> list[tuple[int, str]]:
    results: list[tuple[int, str]] = []
    if os.name == "nt":
        try:
            out = subprocess.check_output(
                ["wmic", "process", "get", "ProcessId,CommandLine", "/FORMAT:LIST"],
                text=True,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            return results
        cmd = ""
        pid = None
        for line in out.splitlines():
            line = line.strip()
            if not line:
                if pid is not None:
                    results.append((pid, cmd))
                cmd = ""
                pid = None
                continue
            if line.startswith("CommandLine="):
                cmd = line.split("=", 1)[1]
            elif line.startswith("ProcessId="):
                try:
                    pid = int(line.split("=", 1)[1])
                except ValueError:
                    pid = None
        if pid is not None:
            results.append((pid, cmd))
        return results

    try:
        out = subprocess.check_output(["ps", "-ax", "-o", "pid=,command="], text=True)
    except Exception:
        return results
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if not parts:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        cmd = parts[1] if len(parts) > 1 else ""
        results.append((pid, cmd))
    return results


def _kill_processes_by_match(keyword: str, repo_root: Optional[Path]) -> bool:
    killed_any = False
    root_str = str(repo_root) if repo_root else None
    for pid, cmd in _iter_processes():
        if pid == os.getpid():
            continue
        if keyword not in cmd:
            continue
        if root_str and root_str not in cmd:
            continue
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            else:
                os.kill(pid, signal.SIGTERM)
            killed_any = True
        except Exception:
            continue
    return killed_any


def _cleanup_stale_server(repo_root: Path) -> bool:
    if not _port_open():
        return False
    health = _fetch_json("/health")
    if not _looks_like_persponify_health(health):
        return False
    _post_json("/shutdown", {"reason": "launcher_cleanup"}, timeout=2.0)
    for _ in range(10):
        if not _port_open():
            return True
        time.sleep(0.2)
    _kill_processes_by_match("app.py", repo_root)
    for _ in range(10):
        if not _port_open():
            return True
        time.sleep(0.2)
    return False


def _cleanup_stale_worker(repo_root: Path) -> bool:
    return _kill_processes_by_match("codex_worker.py", repo_root)


def _detect_codex_cmd(repo_root: Path) -> Optional[str]:
    override = os.environ.get("PERSPONIFY_CODEX_CMD", "").strip()
    if override:
        return override
    codex_bin = _find_codex_bin()
    if not codex_bin:
        return None
    return (
        f"{codex_bin} exec --skip-git-repo-check "
        f"--output-last-message {{response_file_q}} -C {{repo_root_q}}"
    )


def _find_codex_bin() -> Optional[str]:
    codex_bin = shutil.which("codex")
    if codex_bin:
        return codex_bin
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
            return candidate
    return None


def _extract_json(text: str) -> Optional[dict]:
    text = (text or "").strip()
    if not text:
        return None
    if text.startswith("{") and text.endswith("}"):
        try:
            return json.loads(text)
        except Exception:
            return None
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except Exception:
            return None
    return None


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
        return cleaned, _extract_json(payload)
    return text.strip(), None


def _normalize_status(raw: Optional[str]) -> str:
    value = (raw or "").strip().lower()
    if value in ("done", "complete", "completed", "success", "ok"):
        return "done"
    if value in ("doing", "running", "active", "in_progress", "progress"):
        return "running"
    if value in ("blocked", "error", "failed"):
        return "blocked"
    return "pending"


def _strip_control_lines(text: str) -> str:
    if not text:
        return ""
    out = []
    for line in text.splitlines():
        lower = line.strip().lower()
        if lower.startswith(("apply status:", "checklist:", "memory:", "scope:")):
            continue
        out.append(line)
    cleaned = "\n".join(out).strip()
    return cleaned


def _parse_plan_lines(text: str) -> list[dict]:
    if not text:
        return []
    items = []
    header_seen = False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        lower = stripped.lower()
        if re.match(r"^(plan|checklist|steps)\s*:", lower):
            header_seen = True
            continue
        bullet = re.match(r"^(?:-|\*|\d+\.)\s*(\[[ xX~!]\])?\s*(.+)$", stripped)
        if not bullet:
            if header_seen:
                break
            continue
        if not header_seen and not stripped.startswith("- ["):
            continue
        status_token = bullet.group(1) or ""
        title = bullet.group(2).strip()
        status = "pending"
        if status_token:
            token = status_token.strip("[]").lower()
            if token == "x":
                status = "done"
            elif token in ("~", "!"):
                status = "running"
        items.append({"title": title, "status": status})
    return items


def _plan_items_from_payload(payload: dict) -> list[dict]:
    raw_plan = payload.get("plan")
    if not isinstance(raw_plan, list):
        return []
    items = []
    for idx, entry in enumerate(raw_plan, start=1):
        if isinstance(entry, dict):
            title = entry.get("title") or entry.get("step") or entry.get("name") or f"Step {idx}"
            status = _normalize_status(entry.get("status"))
        else:
            title = str(entry)
            status = "pending"
        items.append({"title": str(title), "status": status, "id": str(entry.get("id") or idx) if isinstance(entry, dict) else str(idx)})
    return items


def _steps_from_payload(payload: dict) -> list[dict]:
    raw_steps = payload.get("steps")
    if not isinstance(raw_steps, list):
        return []
    steps = []
    for idx, entry in enumerate(raw_steps, start=1):
        if not isinstance(entry, dict):
            continue
        actions = entry.get("actions")
        if not isinstance(actions, list):
            continue
        step_id = str(entry.get("id") or idx)
        title = str(entry.get("title") or entry.get("step") or f"Step {idx}")
        steps.append({"id": step_id, "title": title, "actions": actions})
    return steps


def _is_confirmation(text: str) -> bool:
    if not text:
        return False
    cleaned = text.strip().lower()
    if cleaned in ("yes", "y", "ok", "okay", "sure", "go ahead", "proceed", "do it", "confirm"):
        return True
    if cleaned.startswith(("yes,", "ok,", "okay,")):
        return True
    return False


def _is_cancel(text: str) -> bool:
    if not text:
        return False
    cleaned = text.strip().lower()
    if cleaned in ("cancel", "never mind", "nevermind", "stop", "abort", "drop it"):
        return True
    return False


def _is_tentative(text: str) -> bool:
    if not text:
        return False
    lowered = text.lower()
    phrases = [
        "i think",
        "maybe",
        "what if",
        "should we",
        "could we",
        "consider",
        "idea",
        "brainstorm",
        "not sure",
        "perhaps",
    ]
    return any(p in lowered for p in phrases)


def _is_memory_query(text: str) -> bool:
    if not text:
        return False
    lowered = text.lower()
    return any(
        phrase in lowered
        for phrase in (
            "what do you remember",
            "what do you know about me",
            "memory",
            "remember me",
            "do you remember",
        )
    )


def _is_history_query(text: str) -> bool:
    if not text:
        return False
    lowered = text.lower().strip()
    return lowered in ("/history", "/recap")


def _is_test_request(text: str) -> bool:
    if not text:
        return False
    lowered = text.lower()
    return any(
        phrase in lowered
        for phrase in (
            "run a test",
            "smoke test",
            "test the plugin",
            "test the server",
            "make sure the plugin works",
            "make sure the server works",
            "test connectivity",
        )
    )


def _scope_from_summary(summary: Optional[dict], mode: str) -> tuple[Optional[str], str]:
    if not summary:
        return None, "Unscoped"
    meta = summary.get("meta") if isinstance(summary, dict) else None
    meta = meta if isinstance(meta, dict) else {}
    game_id = summary.get("gameId") or meta.get("gameId")
    place_id = summary.get("placeId") or meta.get("placeId")
    session_id = summary.get("studioSessionId") or summary.get("sessionId")
    project_key = summary.get("projectKey")

    if mode == "Game" and game_id:
        return f"game:{game_id}", f"Game {game_id}"
    if mode == "Place" and place_id:
        label = f"Place {place_id}"
        if game_id:
            return f"place:{game_id}:{place_id}", label
        return f"place:{place_id}", label
    if mode == "Session" and session_id:
        label = f"Session {session_id[:8]}" if isinstance(session_id, str) else "Session"
        return f"session:{session_id}", label
    if project_key:
        return f"project:{project_key}", f"Project {project_key}"
    return None, "Unscoped"


def _run_codex(
    cmd_template: str,
    prompt: str,
    timeout_sec: float,
    tmp_dir: Path,
    job_id: str,
    on_proc: Optional[callable] = None,
    on_done: Optional[callable] = None,
    cancel_check: Optional[callable] = None,
) -> str:
    tmp_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = tmp_dir / f"prompt_{job_id}.txt"
    response_path = tmp_dir / f"response_{job_id}.txt"
    prompt_path.write_text(prompt)

    format_args = {
        "prompt_file": str(prompt_path),
        "response_file": str(response_path),
        "job_id": job_id,
        "repo_root": str(ROOT),
        "prompt_file_q": shlex.quote(str(prompt_path)),
        "response_file_q": shlex.quote(str(response_path)),
        "repo_root_q": shlex.quote(str(ROOT)),
    }
    expanded = cmd_template.format(**format_args)
    args = shlex.split(expanded)
    proc = subprocess.Popen(
        args,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if on_proc:
        on_proc(proc)
    try:
        out, err = proc.communicate(prompt, timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise RuntimeError(f"Codex command timed out after {timeout_sec} seconds")
    finally:
        if on_done:
            on_done()

    if response_path.exists():
        if cancel_check and cancel_check():
            raise RuntimeError("Cancelled")
        return response_path.read_text().strip()

    if cancel_check and cancel_check():
        raise RuntimeError("Cancelled")
    if proc.returncode != 0:
        raise RuntimeError(err.strip() or f"Codex command failed: {proc.returncode}")

    return out.strip()


def _build_system_guidelines() -> list:
    return [
        "You are Persponify Codex, a Lemonade-style planning + build assistant for Roblox Studio.",
        "This is a developer tool, not player chat. Keep responses structured and pragmatic.",
        "You are operating inside the Persponify Codex launcher with access to the local server + Roblox plugin.",
        "Do not mention sandboxes, permissions, or external execution limits.",
        "For greetings or small talk, reply briefly and ask what they want to do next without assuming a task.",
        "Policy: follow the user's intent first; do not invent requirements.",
        "Policy: preserve existing code style and patterns unless asked to refactor.",
        "Policy: prefer minimal, targeted edits over large rewrites.",
        "Policy: read and respect any project-specific rules or constraints in the prompt or context.",
        "Policy: never fabricate file contents, script paths, or API behavior; rely on context or ask.",
        "Assume each prompt is a request to change Studio unless the user is clearly just chatting.",
        "Assume the launcher has started the local server; only mention connectivity if status says otherwise.",
        "Default mode is Guided: implement only what was asked, then propose upgrades separately and wait for approval.",
        "Never add features, refactors, or 'nice to have' changes unless the user explicitly requests them.",
        "Decide whether to apply based on confidence and scope: small, clear requests can auto-apply; large or ambiguous ones must ask first.",
        "Use the checklist only for multi-step work (2+ steps or a plan with 3+ items).",
        "When a checklist is used, keep steps small, ordered, and verify each step before moving on.",
        "If the request is ambiguous, ask clarifying questions and do not include <actions_json>.",
        "Before including <actions_json>, perform a quick self-check (paths/services/syntax/side effects) and mention any risk in summary.",
        "When ready to apply, include 'Apply status: ready' in summary; otherwise use 'Apply status: needs-confirmation'.",
        "If you set needs-confirmation, ask for confirmation and wait.",
        "When the user confirms (e.g., 'go ahead'), respond with <actions_json> for the last approved plan.",
        "Policy: never delete or move critical objects unless explicitly requested.",
        "Policy: if context is trimmed or missing, call it out and request scope/clarification before applying.",
        "Policy: if apply fails or receipt shows errors, stop, summarize the error, and ask before retrying.",
        "Policy: avoid secrets or personal data in logs/responses; use placeholders if needed.",
        "Implementation: inspect existing scripts before editing; keep diffs small and localized.",
        "Implementation: avoid breaking changes; preserve public interfaces unless asked to change them.",
        "Testing: if a change is risky or multi-step, propose a quick manual smoke test; if you cannot test, say so.",
        "Testing: do not claim test results you did not run.",
        "Communication: keep summaries concise, list assumptions explicitly, and ask direct questions when blocked.",
        "Communication: never reveal chain-of-thought; provide conclusions and next actions only.",
        "When asked to test or validate, propose a short smoke test and offer to enqueue the actions directly.",
        "Do not say you cannot run Studio; enqueue a small test transaction immediately and ask the user to confirm results in Studio.",
        "For test requests, always include <actions_json> for a minimal create/delete marker.",
        "Always use the provided chat history as your source of context if memory is empty.",
        "Use /status data (server + plugin connection) when deciding whether to enqueue actions or ask the user to connect the plugin.",
        "If core context is missing, ask to switch to Full memory or request a full chat replay before applying.",
        "You can explicitly control checklist visibility with 'Checklist: on|off|auto'.",
        "If you want to apply changes, include JSON inside <actions_json>...</actions_json>.",
        "Only include <actions_json> when Apply status is ready.",
        "The JSON must include: {\"actions\":[...], \"summary\":\"...\"}.",
        "For multi-step work, include {\"plan\":[...], \"steps\":[{\"id\":\"1\",\"title\":\"...\",\"actions\":[...]}]} so the checklist can advance.",
        "Outside <actions_json>, respond with normal assistant text.",
        "You may set memory/scope by adding lines: 'Memory: Off|Brief|Full' and 'Scope: Game|Place|Session|Manual'.",
        "Commands available: /status, /models, /feedback, /chat, /rollback.",
    ]


def _action_schema_lines() -> list:
    return [
        "Action schema rules:",
        "Use only these action types: createInstance, setProperty, setProperties, setAttribute, setAttributes, "
        "deleteInstance, rename, move, editScript.",
        "Paths must be absolute Roblox paths like \"game/ReplicatedStorage/Folder/Script\" or \"game/Workspace/Part\".",
        "createInstance requires: type, parentPath, className, name.",
        "editScript requires: type, path, mode (replace|append|prepend|replaceRange|insertBefore|insertAfter), "
        "and source (or chunks).",
        "setProperty requires: type, path, property, value.",
        "setProperties requires: type, path, properties.",
        "setAttribute requires: type, path, attribute, value.",
        "setAttributes requires: type, path, attributes.",
    ]


def _build_chat_prompt(
    summary: Optional[dict],
    memory_summary: Optional[str],
    history: list,
    user_prompt: str,
) -> str:
    lines = []
    lines.append("System guidelines:")
    lines.extend(_build_system_guidelines())
    lines.extend(_action_schema_lines())
    if summary:
        lines.append("Context summary (Roblox Studio):")
        lines.append(json.dumps(summary, indent=2, sort_keys=True))
    if memory_summary:
        lines.append("Memory summary:")
        lines.append(str(memory_summary))
    lines.append("Conversation:")
    for role, content in history:
        prefix = "User" if role == "user" else "Assistant"
        lines.append(f"{prefix}: {content}")
    lines.append(f"User: {user_prompt}")
    lines.append("Assistant:")
    return "\n\n".join(lines).strip()


def _build_apply_prompt(summary: Optional[dict], memory_summary: Optional[str], user_prompt: str) -> str:
    lines = []
    lines.append("System guidelines:")
    lines.extend(_build_system_guidelines())
    lines.append("You are a Lemonade-style planning assistant for Roblox Studio.")
    lines.append("If you are ready to apply, include <actions_json>{...}</actions_json>.")
    lines.extend(_action_schema_lines())
    lines.append("rename requires: type, path, newName.")
    lines.append("move requires: type, path, newParentPath.")
    lines.append(
        "summary must be detailed and include: plan steps, any assumptions, "
        "questions if needed, and an explicit 'Apply status: ready' or "
        "'Apply status: needs-confirmation'."
    )
    lines.append("If you are unsure or need clarification, ask questions and do not include <actions_json>.")
    if summary:
        lines.append("Context summary:")
        lines.append(json.dumps(summary, indent=2, sort_keys=True))
    if memory_summary:
        lines.append("Memory summary:")
        lines.append(str(memory_summary))
    lines.append("User request:")
    lines.append(str(user_prompt))
    return "\n\n".join(lines).strip()


def poll_loop(
    state: StatusState,
    stop_event: threading.Event,
    on_status: Optional[callable] = None,
) -> None:
    while not stop_event.is_set():
        health = _fetch_json("/health")
        if health and health.get("ok"):
            state.server = f"Server: OK (port {PORT})"
        else:
            state.server = "Server: offline"
            state.plugin = "Plugin: —"
            state.codex = "Codex: —"
            time.sleep(POLL_SEC)
            continue

        status = _fetch_json("/status")
        if status and isinstance(status, dict):
            primary = status.get("primary") or {}
            alive = primary.get("alive") is True
            state.plugin = "Plugin: connected" if alive else "Plugin: disconnected"
            codex = status.get("codex") or {}
            pending = codex.get("pending")
            last_job = (codex.get("lastJob") or {}).get("jobId")
            last_error = (codex.get("lastError") or {}).get("message")
            pending_text = f"pending={pending}" if pending is not None else "pending=?"
            job_text = f"lastJob={last_job}" if last_job else "lastJob=—"
            err_text = f"err={last_error}" if last_error else "err=—"
            state.codex = f"Codex: {pending_text} {job_text} {err_text}"
            if on_status:
                try:
                    on_status(status)
                except Exception:
                    pass
        else:
            state.plugin = "Plugin: —"
            state.codex = "Codex: —"

        time.sleep(POLL_SEC)


def _run_headless() -> int:
    controller = ServerController()
    worker = WorkerController()
    state = StatusState()
    stop_event = threading.Event()

    try:
        controller.start()
    except Exception as exc:
        state.server = f"Server: failed to start ({exc})"
    try:
        worker.start()
    except Exception as exc:
        state.worker = f"Worker: failed to start ({exc})"

    def _shutdown() -> None:
        stop_event.set()
        controller.stop()
        worker.stop()

    def _handle_signal(_sig, _frame):
        _shutdown()
        raise SystemExit(0)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handle_signal)
        except Exception:
            pass

    def on_status_update(status: dict) -> None:
        try:
            root.after(0, lambda: _handle_status_update(status))
        except Exception:
            pass

    t = threading.Thread(target=poll_loop, args=(state, stop_event, on_status_update), daemon=True)
    t.start()

    last_line = ""
    try:
        while True:
            line = f"{state.server} | {state.plugin} | {state.worker} | {state.codex}"
            if line != last_line:
                print(line, flush=True)
                last_line = line
            time.sleep(POLL_SEC)
    except KeyboardInterrupt:
        pass
    finally:
        _shutdown()
    return 0


def _run_gui() -> int:
    try:
        import tkinter as tk
        import tkinter.font as tkfont
        import tkinter.filedialog as filedialog
        import tkinter.messagebox as messagebox
        import tkinter.simpledialog as simpledialog
    except Exception as exc:
        print(f"GUI unavailable: {exc}", file=sys.stderr)
        return _run_headless()

    controller = ServerController()
    worker = WorkerController()
    state = StatusState()
    stop_event = threading.Event()

    try:
        controller.start()
    except Exception as exc:
        state.server = f"Server: failed to start ({exc})"
    try:
        worker.start()
    except Exception as exc:
        state.worker = f"Worker: failed to start ({exc})"

    root = tk.Tk()
    root.title("Persponify Codex")
    if CHAT_ENABLED:
        root.geometry("800x560")
        base_min_w, base_min_h = 560, 360
        root.minsize(base_min_w, base_min_h)
    else:
        root.geometry("660x360")
        base_min_w, base_min_h = 460, 280
        root.minsize(base_min_w, base_min_h)

    compact_min_w = 360
    compact_min_h = 220
    compact_state = {"enabled": False, "ready": False}
    last_full_size = {"w": None, "h": None}
    root.resizable(True, True)
    root.configure(bg=THEME["bg"])

    if sys.platform == "darwin":
        mono_family = "Menlo"
    elif os.name == "nt":
        mono_family = "Consolas"
    else:
        mono_family = "DejaVu Sans Mono"

    heading_font = tkfont.Font(family=mono_family, size=16, weight="bold")
    label_font = tkfont.Font(family=mono_family, size=12)
    muted_font = tkfont.Font(family=mono_family, size=10)

    scroll_container = tk.Frame(root, bg=THEME["bg"])
    scroll_container.pack(fill="both", expand=True)

    canvas = tk.Canvas(scroll_container, bg=THEME["bg"], highlightthickness=0, bd=0)
    vscroll = tk.Scrollbar(scroll_container, orient="vertical", command=canvas.yview)
    canvas.configure(yscrollcommand=vscroll.set)
    vscroll.pack(side="right", fill="y")
    canvas.pack(side="left", fill="both", expand=True)

    content = tk.Frame(canvas, bg=THEME["bg"])
    content_id = canvas.create_window((0, 0), window=content, anchor="nw")

    def _on_frame_configure(_event=None) -> None:
        canvas.configure(scrollregion=canvas.bbox("all"))

    def _on_canvas_configure(event) -> None:
        canvas.itemconfigure(content_id, width=event.width)

    content.bind("<Configure>", _on_frame_configure)
    canvas.bind("<Configure>", _on_canvas_configure)

    def _apply_min_size() -> None:
        if compact_state.get("enabled"):
            root.minsize(compact_min_w, compact_min_h)
            return
        root.minsize(base_min_w, base_min_h)

    def _bind_scroll_wheel(widget, target) -> None:
        def _on_wheel(event):
            delta = event.delta
            if delta == 0:
                return "break"
            target.yview_scroll(int(-1 * (delta / 120)), "units")
            return "break"

        def _on_button4(_event):
            target.yview_scroll(-1, "units")
            return "break"

        def _on_button5(_event):
            target.yview_scroll(1, "units")
            return "break"

        widget.bind("<MouseWheel>", _on_wheel)
        widget.bind("<Button-4>", _on_button4)
        widget.bind("<Button-5>", _on_button5)
        widget.bind("<Button-2>", lambda e: target.scan_mark(e.x, e.y))
        widget.bind("<ButtonPress-2>", lambda e: target.scan_mark(e.x, e.y))
        widget.bind("<B2-Motion>", lambda e: target.scan_dragto(e.x, e.y, gain=1))

    def _unbind_scroll_wheel(widget) -> None:
        for event_name in ("<MouseWheel>", "<Button-4>", "<Button-5>", "<Button-2>", "<ButtonPress-2>", "<B2-Motion>"):
            widget.unbind(event_name)

    _bind_scroll_wheel(canvas, canvas)
    _bind_scroll_wheel(scroll_container, canvas)
    _bind_scroll_wheel(content, canvas)
    _bind_scroll_wheel(root, canvas)

    header = tk.Frame(content, bg=THEME["bg"])
    header.pack(fill="x", padx=16, pady=(14, 6))

    title = tk.Label(
        header,
        text="Persponify Codex",
        anchor="w",
        font=heading_font,
        fg=THEME["green"],
        bg=THEME["bg"],
    )
    title.pack(side="left")

    compact_btn = tk.Label(
        header,
        text="Compact",
        bg=THEME["panel"],
        fg=THEME["text"],
        padx=10,
        pady=4,
        cursor="hand2",
        relief="flat",
        font=muted_font,
        highlightthickness=1,
        highlightbackground=THEME["border"],
    )
    compact_btn.pack(side="right", padx=(6, 0))

    panel = tk.Frame(content, bg=THEME["panel"], highlightbackground=THEME["border"], highlightthickness=1)
    panel.pack(fill="x", padx=16, pady=(6, 10))

    def make_row(parent, text: str):
        row = tk.Label(
            parent,
            text=text,
            anchor="w",
            font=label_font,
            fg=THEME["text"],
            bg=THEME["panel"],
            justify="left",
        )
        row.pack(fill="x", padx=12, pady=6)
        return row

    config = _read_config()
    repo_display = ROOT
    repo_path = config.get("repoPath") if isinstance(config, dict) else None
    if isinstance(repo_path, str) and repo_path:
        candidate = Path(repo_path).expanduser().resolve()
        if _looks_like_repo(candidate):
            repo_display = candidate
    codex_cmd = _detect_codex_cmd(repo_display)

    state_data = _read_state()
    chat_state = state_data.get("chats", {})
    current_chat_id = state_data.get("lastChatId")
    scope_mode_value = state_data.get("scopeMode")
    if scope_mode_value not in SCOPE_MODES:
        scope_mode_value = "Place"

    last_warned_context_version = None
    plan_state = {
        "items": [],
        "steps": [],
        "active_index": 0,
        "inflight_tx": None,
        "tx_by_step": {},
        "step_by_tx": {},
        "last_receipt_tx": None,
        "receipts": [],
        "auto_advance": True,
        "apply_ready": False,
        "visible": False,
        "checklist_mode": "auto",
        "awaiting_actions": False,
        "awaiting_confirm": False,
    }

    active_request = {"proc": None, "token": None, "cancelled": False}
    active_lock = threading.Lock()

    server_label = make_row(panel, state.server)
    plugin_label = make_row(panel, state.plugin)
    worker_label = make_row(panel, state.worker)
    codex_label = make_row(panel, state.codex)
    repo_label = make_row(panel, f"Repo: {repo_display}")

    log_hint = tk.Label(
        panel,
        text=f"Log: {LOG_PATH}",
        anchor="w",
        font=muted_font,
        fg=THEME["muted"],
        bg=THEME["panel"],
        justify="left",
    )
    log_hint.pack(fill="x", padx=12, pady=(4, 8))

    panel_rows = [server_label, plugin_label, worker_label, codex_label, repo_label]
    wrap_targets = [server_label, plugin_label, worker_label, codex_label, repo_label, log_hint]

    def _update_wrap(_event=None) -> None:
        width = panel.winfo_width()
        if width <= 0:
            return
        wrap = max(260, width - 24)
        for target in wrap_targets:
            target.configure(wraplength=wrap)

    panel.bind("<Configure>", _update_wrap)
    _update_wrap()

    cli_panel = tk.Frame(content, bg=THEME["bg"], highlightbackground=THEME["border"], highlightthickness=1)
    cli_label = tk.Label(
        cli_panel,
        text=(
            "How to connect Codex (launcher is already running):\n"
            "1) In Studio, open the plugin and click Connect.\n"
            "2) Register the MCP server (one-time):\n"
            "   codex mcp add persponify --url http://127.0.0.1:3030/mcp\n"
            "3) Run Codex in the repo:\n"
            "   - cd /path/to/PersponifyCodex then codex\n"
            "   - or: codex -C /path/to/PersponifyCodex\n"
            "4) First message: sync context\n"
            "5) Chat there; Codex enqueues actions and Studio applies them automatically."
        ),
        anchor="w",
        justify="left",
        font=muted_font,
        fg=THEME["muted"],
        bg=THEME["bg"],
        padx=16,
        pady=12,
    )
    cli_label.pack(fill="both", expand=True)
    if not CHAT_ENABLED:
        cli_panel.pack(fill="both", expand=True, padx=16, pady=(0, 10))

    chat_panel = tk.Frame(content, bg=THEME["bg"], highlightbackground=THEME["border"], highlightthickness=1)
    chat_panel.pack(fill="both", expand=True, padx=16, pady=(0, 10))

    chat_header = tk.Frame(chat_panel, bg=THEME["bg"])
    chat_header.pack(fill="x", padx=12, pady=(10, 6))
    chat_header_top = tk.Frame(chat_header, bg=THEME["bg"])
    chat_header_top.pack(fill="x")

    chat_label = tk.Label(
        chat_header_top,
        text="Chat",
        font=muted_font,
        fg=THEME["muted"],
        bg=THEME["bg"],
    )
    chat_label.pack(side="left", padx=(18, 6))

    chat_var = tk.StringVar(value="Loading…")
    chat_menu = tk.OptionMenu(chat_header_top, chat_var, "Loading…")
    chat_menu.configure(bg=THEME["border"], fg=THEME["text"], activebackground=THEME["green"], font=muted_font)
    chat_menu["menu"].configure(bg=THEME["panel"], fg=THEME["text"], font=muted_font)
    chat_menu.configure(width=14)
    chat_menu.pack(side="left")

    new_chat_btn = tk.Label(
        chat_header_top,
        text="New",
        bg=THEME["green_dim"],
        fg=THEME["text"],
        padx=8,
        pady=3,
        cursor="hand2",
        relief="flat",
        font=muted_font,
        highlightthickness=1,
        highlightbackground=THEME["border"],
    )
    new_chat_btn.pack(side="left", padx=(8, 0))

    rename_chat_btn = tk.Label(
        chat_header_top,
        text="Rename",
        bg=THEME["panel"],
        fg=THEME["text"],
        padx=8,
        pady=3,
        cursor="hand2",
        relief="flat",
        font=muted_font,
        highlightthickness=1,
        highlightbackground=THEME["border"],
    )
    rename_chat_btn.pack(side="left", padx=(6, 0))

    delete_chat_btn = tk.Label(
        chat_header_top,
        text="Delete",
        bg=THEME["panel"],
        fg=THEME["text"],
        padx=8,
        pady=3,
        cursor="hand2",
        relief="flat",
        font=muted_font,
        highlightthickness=1,
        highlightbackground=THEME["border"],
    )
    delete_chat_btn.pack(side="left", padx=(6, 0))

    def set_manage_visibility(visible: bool) -> None:
        if visible:
            if not rename_chat_btn.winfo_ismapped():
                rename_chat_btn.pack(side="left", padx=(6, 0))
            if not delete_chat_btn.winfo_ismapped():
                delete_chat_btn.pack(side="left", padx=(6, 0))
        else:
            if rename_chat_btn.winfo_ismapped():
                rename_chat_btn.pack_forget()
            if delete_chat_btn.winfo_ismapped():
                delete_chat_btn.pack_forget()

    memory_var = tk.StringVar(value="Brief")
    scope_var = tk.StringVar(value=scope_mode_value)
    context_status = tk.Label(
        chat_header_top,
        text="Memory: Brief | Scope: Game",
        font=muted_font,
        fg=THEME["muted"],
        bg=THEME["bg"],
    )
    context_status.pack(side="left", padx=(16, 0))

    def _update_header_layout(_event=None) -> None:
        width = root.winfo_width()
        if width <= 0:
            return
        set_manage_visibility(width >= 780)
        if width < 900:
            context_status.configure(text="Mem: " + memory_var.get() + " | Scope: " + scope_var.get())
        else:
            context_status.configure(text="Memory: " + memory_var.get() + " | Scope: " + scope_var.get())

    root.bind("<Configure>", _update_header_layout, add="+")
    _update_header_layout()

    body = tk.Frame(chat_panel, bg=THEME["bg"])
    body.pack(fill="both", expand=True, padx=12, pady=(0, 8))

    chat_column = tk.Frame(body, bg=THEME["bg"])
    chat_column.pack(side="left", fill="both", expand=True)

    side_column = tk.Frame(body, bg=THEME["panel"], highlightbackground=THEME["border"], highlightthickness=1)
    side_column.configure(width=280)
    side_column.pack_propagate(False)

    chat_body = tk.Frame(chat_column, bg=THEME["bg"])
    chat_body.pack(fill="both", expand=True, pady=(0, 8))

    chat_scroll = tk.Scrollbar(chat_body)
    chat_scroll.pack(side="right", fill="y")

    chat_text = tk.Text(
        chat_body,
        wrap="word",
        bg=THEME["bg"],
        fg=THEME["text"],
        insertbackground=THEME["text"],
        font=label_font,
        bd=0,
        height=12,
    )
    chat_text.pack(fill="both", expand=True)
    chat_text.tag_configure("label_user", foreground=THEME["green"])
    chat_text.tag_configure("label_codex", foreground=THEME["text"])
    chat_text.tag_configure("label_system", foreground=THEME["muted"])
    chat_text.tag_configure("content", foreground=THEME["text"])
    chat_text.tag_configure("thinking", foreground=THEME["yellow"])
    chat_text.configure(yscrollcommand=chat_scroll.set, state="disabled")
    chat_scroll.configure(command=chat_text.yview)
    _bind_scroll_wheel(chat_text, chat_text)
    _bind_scroll_wheel(chat_scroll, chat_text)

    input_frame = tk.Frame(chat_column, bg=THEME["bg"])
    input_frame.pack(fill="x", pady=(0, 8))

    input_box = tk.Text(
        input_frame,
        height=3,
        wrap="word",
        bg=THEME["panel"],
        fg=THEME["text"],
        insertbackground=THEME["text"],
        font=label_font,
        bd=0,
    )
    input_box.pack(side="left", fill="both", expand=True)

    send_btn = tk.Label(
        input_frame,
        text="Send",
        bg=THEME["green_dim"],
        fg=THEME["text"],
        padx=10,
        pady=6,
        cursor="hand2",
        relief="flat",
        font=muted_font,
        highlightthickness=1,
        highlightbackground=THEME["border"],
    )
    send_btn.pack(side="left", padx=(8, 0))

    stop_btn = tk.Label(
        input_frame,
        text="Stop",
        bg=THEME["panel"],
        fg=THEME["muted"],
        padx=10,
        pady=6,
        cursor="hand2",
        relief="flat",
        font=muted_font,
        highlightthickness=1,
        highlightbackground=THEME["border"],
    )
    stop_btn.pack(side="left", padx=(6, 0))

    plan_frame = tk.Frame(side_column, bg=THEME["panel"])
    plan_frame.pack(fill="both", expand=True, padx=8, pady=(8, 6))

    plan_label = tk.Label(
        plan_frame,
        text="Checklist",
        anchor="w",
        font=muted_font,
        fg=THEME["muted"],
        bg=THEME["panel"],
    )
    plan_label.pack(fill="x")

    plan_scroll = tk.Scrollbar(plan_frame)
    plan_scroll.pack(side="right", fill="y")

    plan_text = tk.Text(
        plan_frame,
        wrap="word",
        bg=THEME["panel"],
        fg=THEME["text"],
        insertbackground=THEME["text"],
        font=muted_font,
        bd=0,
        height=10,
    )
    plan_text.pack(fill="both", expand=True, pady=(6, 0))
    plan_text.tag_configure("pending", foreground=THEME["muted"])
    plan_text.tag_configure("running", foreground=THEME["yellow"])
    plan_text.tag_configure("done", foreground=THEME["green"])
    plan_text.tag_configure("blocked", foreground=THEME["red"])
    plan_text.configure(yscrollcommand=plan_scroll.set, state="disabled")
    plan_scroll.configure(command=plan_text.yview)
    _bind_scroll_wheel(plan_text, plan_text)
    _bind_scroll_wheel(plan_scroll, plan_text)

    receipt_frame = tk.Frame(side_column, bg=THEME["panel"])
    receipt_frame.pack(fill="x", padx=8, pady=(0, 8))

    receipt_label = tk.Label(
        receipt_frame,
        text="Receipts",
        anchor="w",
        font=muted_font,
        fg=THEME["muted"],
        bg=THEME["panel"],
    )
    receipt_label.pack(fill="x")

    receipt_text = tk.Text(
        receipt_frame,
        wrap="word",
        bg=THEME["panel"],
        fg=THEME["text"],
        insertbackground=THEME["text"],
        font=muted_font,
        bd=0,
        height=6,
    )
    receipt_text.pack(fill="x", pady=(6, 0))
    receipt_text.tag_configure("receipt_ok", foreground=THEME["green"])
    receipt_text.tag_configure("receipt_err", foreground=THEME["red"])
    receipt_text.configure(state="disabled")
    _bind_scroll_wheel(receipt_text, receipt_text)

    diag_label = tk.Label(
        side_column,
        text="Diagnostics: idle",
        anchor="w",
        font=muted_font,
        fg=THEME["muted"],
        bg=THEME["panel"],
    )
    diag_label.pack(fill="x", padx=8, pady=(0, 8))

    def _update_side_wrap(_event=None) -> None:
        width = side_column.winfo_width()
        if width <= 0:
            return
        wrap = max(180, width - 16)
        for target in (plan_label, receipt_label, diag_label):
            target.configure(wraplength=wrap)

    side_column.bind("<Configure>", _update_side_wrap)
    _update_side_wrap()

    if not CHAT_ENABLED:
        chat_panel.pack_forget()
        _apply_min_size()

    footer = tk.Frame(content, bg=THEME["bg"])
    footer.pack(fill="x", padx=16, pady=(0, 12))
    build_label = tk.Label(
        footer,
        text=f"Build: {LAUNCHER_BUILD}",
        anchor="w",
        font=muted_font,
        fg=THEME["muted"],
        bg=THEME["bg"],
    )
    build_label.pack(fill="x", pady=(0, 6))
    config_hint = tk.Label(
        footer,
        text="Models/permissions are controlled by your Codex CLI config "
        "(see ~/.codex/config.toml). Changes there apply to this app.",
        anchor="w",
        font=muted_font,
        fg=THEME["muted"],
        bg=THEME["bg"],
        justify="left",
    )
    config_hint.pack(fill="x", pady=(0, 8))
    button_row = tk.Frame(footer, bg=THEME["bg"])
    button_row.pack(fill="x")

    def action_button(parent, text: str, command, bg: str, fg: str, hover_bg: str, hover_fg: str):
        btn = tk.Label(
            parent,
            text=text,
            bg=bg,
            fg=fg,
            padx=10,
            pady=6,
            cursor="hand2",
            relief="flat",
            font=muted_font,
            highlightthickness=1,
            highlightbackground=THEME["border"],
        )

        def on_enter(_):
            btn.configure(bg=hover_bg, fg=hover_fg)

        def on_leave(_):
            btn.configure(bg=bg, fg=fg)

        def on_click(_):
            command()

        btn.bind("<Enter>", on_enter)
        btn.bind("<Leave>", on_leave)
        btn.bind("<Button-1>", on_click)
        return btn

    tmp_dir = SUPPORT_DIR / "tmp"

    def _format_history(chat: dict, limit: int, exclude_user_prompt: Optional[str] = None) -> list:
        out = []
        messages = chat.get("messages") or []
        sliced = messages[-limit:] if limit > 0 else []
        if exclude_user_prompt and sliced:
            last = sliced[-1]
            if last.get("role") == "user" and last.get("content") == exclude_user_prompt:
                sliced = sliced[:-1]
        for msg in sliced:
            role = msg.get("role")
            if role not in ("user", "assistant"):
                continue
            out.append((role, msg.get("content", "")))
        return out

    def _chat_label(chat: dict) -> str:
        name = chat.get("name") or "Chat"
        scope_label = chat.get("scopeLabel") or "Unscoped"
        return f"{name} • {scope_label}"

    def _save_state() -> None:
        state_data["lastChatId"] = current_chat_id
        state_data["scopeMode"] = scope_var.get()
        _write_state(state_data)

    def _set_checklist_visible(visible: bool) -> None:
        if visible and not side_column.winfo_ismapped():
            side_column.pack(side="right", fill="y", padx=(12, 0))
        elif not visible and side_column.winfo_ismapped():
            side_column.pack_forget()
        plan_state["visible"] = visible
        _render_diagnostics()
        _apply_min_size()

    def _set_compact_mode(enabled: bool) -> None:
        was_enabled = compact_state["enabled"]
        if was_enabled == enabled:
            return
        compact_state["enabled"] = enabled
        compact_btn.configure(text="Expand" if enabled else "Compact")
        if enabled:
            if not was_enabled:
                last_full_size["w"] = root.winfo_width()
                last_full_size["h"] = root.winfo_height()
            if cli_panel.winfo_ismapped():
                cli_panel.pack_forget()
            if chat_panel.winfo_ismapped():
                chat_panel.pack_forget()
            if footer.winfo_ismapped():
                footer.pack_forget()
            if codex_label.winfo_ismapped():
                codex_label.pack_forget()
            if repo_label.winfo_ismapped():
                repo_label.pack_forget()
            if log_hint.winfo_ismapped():
                log_hint.pack_forget()
            _unbind_scroll_wheel(canvas)
            _unbind_scroll_wheel(scroll_container)
            _unbind_scroll_wheel(content)
            _unbind_scroll_wheel(root)
            root.resizable(False, False)
            root.minsize(compact_min_w, compact_min_h)
            root.geometry(f"{compact_min_w}x{compact_min_h}")
        else:
            header.pack(fill="x", padx=16, pady=(14, 6))
            panel.pack(fill="x", padx=16, pady=(6, 10))
            for row in panel_rows:
                if row.winfo_ismapped():
                    row.pack_forget()
            for row in panel_rows:
                row.pack(fill="x", padx=12, pady=6)
            log_hint.pack(fill="x", padx=12, pady=(4, 8))
            if CHAT_ENABLED:
                chat_panel.pack(fill="both", expand=True, padx=16, pady=(0, 10))
            else:
                cli_panel.pack(fill="both", expand=True, padx=16, pady=(0, 10))
            footer.pack(fill="x", padx=16, pady=(0, 12))
            root.resizable(True, True)
            _bind_scroll_wheel(canvas, canvas)
            _bind_scroll_wheel(scroll_container, canvas)
            _bind_scroll_wheel(content, canvas)
            _bind_scroll_wheel(root, canvas)
            if last_full_size["w"] and last_full_size["h"]:
                root.geometry(f"{last_full_size['w']}x{last_full_size['h']}")
        _apply_min_size()

    def _toggle_compact(_event=None) -> None:
        _set_compact_mode(not compact_state["enabled"])

    compact_btn.bind("<Button-1>", _toggle_compact)

    def _update_checklist_visibility(
        plan_items: list,
        steps: list,
        summary_text: str,
    ) -> None:
        if plan_state["checklist_mode"] == "off":
            _set_checklist_visible(False)
            return
        has_plan = bool(plan_items) or bool(steps)
        if plan_state["checklist_mode"] == "on":
            _set_checklist_visible(has_plan)
            return
        # Auto: only show for multi-step work.
        long_plan = len(steps) >= 2 or len(plan_items) >= 3
        if "checklist: on" in (summary_text or "").lower():
            long_plan = True
        _set_checklist_visible(has_plan and long_plan)

    def _render_plan() -> None:
        plan_text.configure(state="normal")
        plan_text.delete("1.0", "end")
        items = plan_state["items"]
        if not items:
            plan_text.insert("end", "No checklist yet.\n", "pending")
        else:
            for idx, item in enumerate(items, start=1):
                status = item.get("status", "pending")
                title = item.get("title", "")
                prefix = {"pending": "[ ]", "running": "[~]", "done": "[x]", "blocked": "[!]"}.get(status, "[ ]")
                plan_text.insert("end", f"{idx}. {prefix} {title}\n", status)
        plan_text.configure(state="disabled")

    def _set_progress(text: str) -> None:
        tag = "progress_line"

        def _do():
            if not chat_text.winfo_exists():
                return
            chat_text.configure(state="normal")
            ranges = chat_text.tag_ranges(tag)
            if ranges:
                chat_text.delete(ranges[0], ranges[-1])
            chat_text.insert("end", "Codex: ", ("label_codex", tag))
            chat_text.insert("end", f"{text}\n\n", ("content", tag))
            chat_text.configure(state="disabled")
            chat_text.see("end")

        root.after(0, _do)

    input_lock = {"locked": False}

    def _set_input_enabled(enabled: bool) -> None:
        input_lock["locked"] = not enabled
        if enabled:
            send_btn.configure(bg=THEME["green_dim"], fg=THEME["text"])
        else:
            send_btn.configure(bg=THEME["panel"], fg=THEME["muted"])

    def _render_receipts() -> None:
        receipt_text.configure(state="normal")
        receipt_text.delete("1.0", "end")
        for entry in plan_state["receipts"]:
            tx_id = entry.get("transactionId") or "-"
            applied = entry.get("appliedCount", 0)
            errors = entry.get("errorsCount", 0)
            stamp = entry.get("time", "")
            tag = "receipt_err" if errors else "receipt_ok"
            receipt_text.insert("end", f"{stamp} {tx_id} applied={applied} errors={errors}\n", tag)
            preview = entry.get("errorsPreview")
            if preview:
                receipt_text.insert("end", f"  {preview}\n", tag)
        receipt_text.configure(state="disabled")

    def _render_diagnostics(status: Optional[dict] = None) -> None:
        parts = []
        steps = plan_state["steps"]
        if steps:
            done = sum(1 for s in steps if s.get("status") == "done")
            total = len(steps)
            parts.append(f"Steps {done}/{total}")
            parts.append("Apply ready" if plan_state.get("apply_ready") else "Apply hold")
        if plan_state["inflight_tx"]:
            short = plan_state["inflight_tx"][-8:]
            parts.append(f"In-flight …{short}")
        if status:
            pending = status.get("queuePending")
            if pending is not None:
                parts.append(f"Queue {pending}")
        diag = "Diagnostics: " + (" | ".join(parts) if parts else "idle")
        diag_label.configure(text=diag)

    def _sync_plan_items() -> None:
        if plan_state["steps"]:
            plan_state["items"] = [
                {"id": s.get("id"), "title": s.get("title"), "status": s.get("status", "pending")}
                for s in plan_state["steps"]
            ]
        _render_plan()
        _render_diagnostics()

    def _set_plan_items(items: list) -> None:
        plan_state["items"] = items
        _render_plan()
        _render_diagnostics()

    def _set_steps(steps: list, plan_items: list) -> None:
        plan_state["steps"] = []
        plan_state["active_index"] = 0
        plan_state["inflight_tx"] = None
        plan_state["tx_by_step"] = {}
        plan_state["step_by_tx"] = {}
        plan_state["receipts"] = []
        plan_state["last_receipt_tx"] = None
        plan_state["awaiting_actions"] = False
        plan_state["awaiting_confirm"] = False
        for idx, step in enumerate(steps, start=1):
            title = step.get("title") or f"Step {idx}"
            if plan_items and idx - 1 < len(plan_items):
                title = plan_items[idx - 1].get("title") or title
            plan_state["steps"].append(
                {
                    "id": step.get("id") or str(idx),
                    "title": title,
                    "actions": step.get("actions") or [],
                    "status": "pending",
                }
            )
        _render_receipts()
        _sync_plan_items()

    def _record_receipt(receipt: dict) -> None:
        entry = {
            "transactionId": receipt.get("transactionId"),
            "appliedCount": receipt.get("appliedCount", 0),
            "errorsCount": receipt.get("errorsCount", 0),
            "errorsPreview": None,
            "time": time.strftime("%H:%M:%S"),
        }
        errors_preview = receipt.get("errorsPreview")
        if isinstance(errors_preview, list) and errors_preview:
            entry["errorsPreview"] = errors_preview[0]
        plan_state["receipts"].append(entry)
        plan_state["receipts"] = plan_state["receipts"][-12:]
        _render_receipts()
        _render_diagnostics()

    def _apply_receipt_to_plan(receipt: dict) -> None:
        tx_id = receipt.get("transactionId")
        if not tx_id:
            return
        step_id = plan_state["step_by_tx"].get(tx_id)
        if not step_id:
            return
        steps = plan_state["steps"]
        total_steps = len(steps)
        for idx, step in enumerate(steps):
            if step.get("id") == step_id:
                if (receipt.get("errorsCount") or 0) > 0:
                    step["status"] = "blocked"
                    preview = None
                    errors_preview = receipt.get("errorsPreview")
                    if isinstance(errors_preview, list) and errors_preview:
                        preview = errors_preview[0]
                    if preview:
                        _set_progress(
                            f"Error on step {idx + 1}/{total_steps}: {step.get('title')} — {preview}"
                        )
                    else:
                        _set_progress(
                            f"Error on step {idx + 1}/{total_steps}: {step.get('title')}. Want me to fix it?"
                        )
                else:
                    step["status"] = "done"
                    plan_state["active_index"] = max(plan_state["active_index"], idx + 1)
                    _set_progress(f"Applied step {idx + 1}/{total_steps}: {step.get('title')}.")
                break
        plan_state["inflight_tx"] = None
        _sync_plan_items()
        if plan_state["steps"] and all(s.get("status") == "done" for s in plan_state["steps"]):
            _set_progress("All checklist steps applied.")
        if plan_state["auto_advance"] and plan_state.get("apply_ready"):
            _enqueue_next_step()

    def _enqueue_next_step() -> None:
        steps = plan_state["steps"]
        if plan_state["inflight_tx"] or not steps:
            return
        idx = plan_state["active_index"]
        if idx >= len(steps):
            return
        step = steps[idx]
        if not step.get("actions"):
            step["status"] = "blocked"
            _sync_plan_items()
            return
        total_steps = len(steps)
        _set_progress(f"Applying step {idx + 1}/{total_steps}: {step.get('title')}")
        step["status"] = "running"
        ok, res = enqueue_actions(step["actions"])
        if ok:
            tx_id = res.get("transactionId")
            plan_state["inflight_tx"] = tx_id
            if tx_id:
                plan_state["tx_by_step"][step.get("id")] = tx_id
                plan_state["step_by_tx"][tx_id] = step.get("id")
            _set_progress(f"Queued step {idx + 1}/{total_steps}: {step.get('title')}")
        else:
            step["status"] = "blocked"
            _record_receipt(
                {
                    "transactionId": "enqueue_failed",
                    "appliedCount": 0,
                    "errorsCount": 1,
                    "errorsPreview": [str(res)],
                }
            )
        _sync_plan_items()

    def _render_chat(chat: Optional[dict]) -> None:
        chat_text.configure(state="normal")
        chat_text.delete("1.0", "end")
        if chat:
            for msg in chat.get("messages", []):
                role = msg.get("role")
                content = msg.get("content", "")
                if role == "user":
                    label = "You"
                    tag = "label_user"
                elif role == "assistant":
                    label = "Codex"
                    tag = "label_codex"
                else:
                    label = "System"
                    tag = "label_system"
                chat_text.insert("end", f"{label}: ", tag)
                chat_text.insert("end", f"{content}\n\n", "content")
        chat_text.configure(state="disabled")
        chat_text.see("end")

    def _refresh_chat_menu(select_id: Optional[str] = None) -> None:
        menu = chat_menu["menu"]
        menu.delete(0, "end")
        items = []
        for chat_id, chat in chat_state.items():
            label = _chat_label(chat)
            items.append((label, chat_id))
        items.sort(key=lambda item: item[0].lower())
        if not items:
            chat_var.set("No chats")
            _render_chat(None)
            return
        for label, chat_id in items:
            menu.add_command(
                label=label,
                command=lambda cid=chat_id, lbl=label: _select_chat(cid, lbl),
            )
        label_by_id = {cid: lbl for lbl, cid in items}
        target_id = select_id or current_chat_id
        if target_id in label_by_id:
            _select_chat(target_id, label_by_id[target_id])
            return
        _select_chat(items[0][1], items[0][0])

    def _create_chat(scope_key: Optional[str], scope_label: str) -> str:
        chat_id = str(uuid.uuid4())
        counter_key = scope_key or "global"
        current_count = state_data["chatCounters"].get(counter_key, 0)
        next_count = current_count + 1
        state_data["chatCounters"][counter_key] = next_count
        default_name = f"Chat {next_count}"
        chat_state[chat_id] = {
            "id": chat_id,
            "name": default_name,
            "scopeKey": scope_key,
            "scopeLabel": scope_label,
            "messages": [],
            "memorySummary": "",
            "memoryMode": "Brief",
            "autoRenamed": False,
            "updatedAt": time.time(),
        }
        state_data["lastChatByScope"][scope_key or "global"] = chat_id
        return chat_id

    def _select_chat(chat_id: str, label: Optional[str] = None) -> None:
        nonlocal current_chat_id
        if chat_id not in chat_state:
            return
        current_chat_id = chat_id
        chat = chat_state[chat_id]
        scope_key = chat.get("scopeKey") or "global"
        state_data["lastChatByScope"][scope_key] = chat_id
        if label:
            chat_var.set(label)
        else:
            chat_var.set(_chat_label(chat))
        memory_mode = chat.get("memoryMode") if chat.get("memoryMode") in MEMORY_MODES else "Brief"
        memory_var.set(memory_mode)
        _render_chat(chat)
        _save_state()

    def _get_current_chat() -> dict:
        nonlocal current_chat_id
        if current_chat_id and current_chat_id in chat_state:
            return chat_state[current_chat_id]
        chat_id = _create_chat(None, "Unscoped")
        current_chat_id = chat_id
        return chat_state[chat_id]

    def _ensure_chat_for_scope(scope_key: Optional[str], scope_label: str) -> None:
        if scope_var.get() == "Manual":
            return
        target = scope_key or "global"
        current = _get_current_chat()
        if current.get("scopeKey") == scope_key:
            return
        last_by_scope = state_data.get("lastChatByScope", {})
        chat_id = last_by_scope.get(target)
        if chat_id in chat_state:
            _select_chat(chat_id)
            return
        chat_id = _create_chat(scope_key, scope_label)
        _select_chat(chat_id)

    def append_chat(role: str, content: str) -> None:
        chat = _get_current_chat()
        role_key = role if role in ("user", "assistant", "system") else "system"
        label = "You" if role_key == "user" else "Codex" if role_key == "assistant" else "System"
        chat["messages"].append({"role": role_key, "content": content, "ts": time.time()})
        chat["updatedAt"] = time.time()
        rerendered = False
        if role_key == "user" and not chat.get("autoRenamed"):
            trimmed = " ".join(content.split())
            if trimmed:
                new_name = trimmed[:48].rstrip()
                chat["name"] = new_name
                chat["autoRenamed"] = True
                _refresh_chat_menu(chat.get("id"))
                rerendered = True
        _save_state()

        tag = "label_user" if role_key == "user" else "label_codex" if role_key == "assistant" else "label_system"

        def _do():
            if rerendered:
                return
            chat_text.configure(state="normal")
            chat_text.insert("end", f"{label}: ", tag)
            chat_text.insert("end", f"{content}\n\n", "content")
            chat_text.configure(state="disabled")
            chat_text.see("end")
        root.after(0, _do)

    def _maybe_warn_context(summary: dict) -> None:
        nonlocal last_warned_context_version
        version = summary.get("contextVersion")
        if version is not None and version == last_warned_context_version:
            return
        meta = summary.get("meta") if isinstance(summary, dict) else None
        meta = meta if isinstance(meta, dict) else {}
        omitted = summary.get("omittedSourceCount")
        if omitted is None:
            omitted = meta.get("omittedSourceCount")
        cap_hit = summary.get("totalCapHit")
        if cap_hit is None:
            cap_hit = meta.get("totalCapHit")
        max_total = meta.get("maxTotalChars")
        exported = meta.get("exportedScriptChars")
        if (omitted and int(omitted) > 0) or cap_hit:
            append_chat("system", "Warning: context export trimmed (some script sources omitted).")
            last_warned_context_version = version
            return
        if isinstance(max_total, int) and isinstance(exported, int) and max_total > 0:
            ratio = exported / max_total
            if ratio >= 0.85:
                append_chat("system", "Warning: context export near size limit.")
                last_warned_context_version = version

    def _handle_status_update(status: dict) -> None:
        if not isinstance(status, dict):
            return
        receipt = status.get("lastReceipt") or {}
        tx_id = receipt.get("transactionId")
        if tx_id and tx_id != plan_state["last_receipt_tx"]:
            plan_state["last_receipt_tx"] = tx_id
            _record_receipt(receipt)
            _apply_receipt_to_plan(receipt)
        _render_diagnostics(status)

    def show_thinking(token: str) -> None:
        tag = f"thinking_{token}"

        def _do():
            if not chat_text.winfo_exists():
                return
            chat_text.configure(state="normal")
            chat_text.insert("end", "Codex: ", ("label_codex", tag))
            chat_text.insert("end", "thinking...\n\n", ("thinking", tag))
            chat_text.configure(state="disabled")
            chat_text.see("end")

        root.after(0, _do)

    def clear_thinking(token: str) -> None:
        tag = f"thinking_{token}"

        def _do():
            if not chat_text.winfo_exists():
                return
            ranges = chat_text.tag_ranges(tag)
            if ranges:
                chat_text.configure(state="normal")
                chat_text.delete(ranges[0], ranges[-1])
                chat_text.configure(state="disabled")

        root.after(0, _do)

    def get_context_summary() -> Optional[dict]:
        summary = _fetch_json("/context/summary")
        if summary and summary.get("ok"):
            summary = dict(summary)
            summary.pop("ok", None)
            scope_key, scope_label = _scope_from_summary(summary, scope_var.get())
            if scope_var.get() != "Manual":
                root.after(0, lambda: _ensure_chat_for_scope(scope_key, scope_label))
            _maybe_warn_context(summary)
            return summary
        return None

    def _build_transcript(chat: dict, limit: int = 10) -> str:
        lines = []
        for msg in (chat.get("messages") or [])[-limit:]:
            role = msg.get("role")
            if role not in ("user", "assistant"):
                continue
            prefix = "User" if role == "user" else "Assistant"
            lines.append(f"{prefix}: {msg.get('content', '')}")
        return "\n".join(lines)

    def _should_apply(summary_text: str) -> bool:
        text = (summary_text or "").lower()
        return "apply status: ready" in text

    def _apply_directives(summary_text: str, summary: Optional[dict]) -> None:
        if not summary_text:
            return
        memory = None
        scope = None
        checklist_mode = None
        for line in summary_text.splitlines():
            stripped = line.strip()
            lower = stripped.lower()
            if lower.startswith("memory:"):
                memory = stripped.split(":", 1)[1].strip().capitalize()
            elif lower.startswith("scope:"):
                scope = stripped.split(":", 1)[1].strip().capitalize()
            elif lower.startswith("checklist:"):
                checklist_mode = stripped.split(":", 1)[1].strip().lower()

        if memory in MEMORY_MODES:
            memory_var.set(memory)
            chat = _get_current_chat()
            chat["memoryMode"] = memory
            _save_state()

        if scope in SCOPE_MODES:
            scope_var.set(scope)
            state_data["scopeMode"] = scope
            _save_state()
            if scope != "Manual" and summary:
                scope_key, scope_label = _scope_from_summary(summary, scope)
                _ensure_chat_for_scope(scope_key, scope_label)

        if checklist_mode in ("on", "off", "auto"):
            plan_state["checklist_mode"] = checklist_mode

        _update_header_layout()

    def _cancel_active_request() -> None:
        with active_lock:
            proc = active_request.get("proc")
            token = active_request.get("token")
            active_request["cancelled"] = True
        if proc and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        if token:
            clear_thinking(token)
        _set_input_enabled(True)
        _set_progress("Thinking stopped.")

    def _update_memory_summary(chat: dict, summary: Optional[dict]) -> None:
        if chat.get("memoryMode") != "Brief":
            return
        transcript = _build_transcript(chat, limit=12)
        if not transcript.strip():
            return
        payload = {
            "summary": chat.get("memorySummary") or "",
            "transcript": transcript,
            "maxChars": 1200,
        }
        res = _post_json("/ai/memory/summarize", payload, timeout=15.0)
        if res and res.get("ok") and res.get("summary"):
            chat["memorySummary"] = res.get("summary")
            _save_state()

    def enqueue_actions(actions: list) -> tuple:
        compile_res = _post_json("/codex/compile", {"actions": actions})
        if not compile_res or not compile_res.get("ok"):
            return False, compile_res or {"error": "compile failed"}
        tx_id = f"TX_CLI_{uuid.uuid4()}"
        payload = {
            "tx": {
                "protocolVersion": 1,
                "transactionId": tx_id,
                "actions": actions,
            }
        }
        res = _post_json("/enqueue", payload)
        if res and res.get("ok"):
            return True, {"transactionId": tx_id, "pending": res.get("pending")}
        return False, res or {"error": "enqueue failed"}

    def show_models() -> None:
        res = _fetch_json("/ai/models")
        if not res or not res.get("ok"):
            append_chat("system", "Models unavailable. Start the companion service first.")
            return
        lines = []
        default = res.get("defaultAdapter")
        adapters = res.get("adapters") or []
        for entry in adapters:
            name = entry.get("name")
            enabled = entry.get("enabled")
            available = entry.get("available")
            suffix = []
            if name == default:
                suffix.append("default")
            if enabled:
                suffix.append("enabled")
            if available:
                suffix.append("available")
            label = f"- {name} ({', '.join(suffix)})" if suffix else f"- {name}"
            lines.append(label)
        output = "Models:\n" + "\n".join(lines) if lines else "Models: none"
        append_chat("system", output)

    def show_status() -> None:
        lines = []
        status = _fetch_json("/status")
        if status and isinstance(status, dict):
            primary = status.get("primary") or {}
            alive = primary.get("alive") is True
            pending = (status.get("codex") or {}).get("pending")
            last_job = (status.get("codex") or {}).get("lastJob") or {}
            last_error = (status.get("codex") or {}).get("lastError") or {}
            lines.append(f"Server: online (port {PORT})")
            lines.append(f"Plugin: {'connected' if alive else 'disconnected'}")
            lines.append(f"Codex pending: {pending if pending is not None else '?'}")
            if last_job.get("jobId"):
                lines.append(f"Last job: {last_job.get('jobId')}")
            if last_error.get("message"):
                lines.append(f"Last error: {last_error.get('message')}")
        else:
            lines.append("Server: offline")

        summary = _fetch_json("/context/summary")
        if summary and summary.get("ok"):
            lines.append(
                f"Context v{summary.get('contextVersion')} tree={summary.get('treeCount')} scripts={summary.get('scriptCount')}"
            )
            total_chars = summary.get("totalScriptChars")
            exported_chars = summary.get("exportedScriptChars")
            if isinstance(total_chars, int) and isinstance(exported_chars, int):
                lines.append(f"Context chars: {exported_chars}/{total_chars}")
            omitted = summary.get("omittedSourceCount")
            if isinstance(omitted, int) and omitted > 0:
                lines.append(f"Context omitted sources: {omitted}")

        lines.append(f"Memory: {memory_var.get()} | Scope: {scope_var.get()}")
        append_chat("system", "\n".join(lines))

    def request_resync() -> None:
        def _notify(message: str, ok: bool = True) -> None:
            if CHAT_ENABLED:
                append_chat("system", message)
            elif ok:
                messagebox.showinfo("Persponify Codex", message)
            else:
                messagebox.showerror("Persponify Codex", message)

        status = _fetch_json("/status")
        primary = status.get("primary") if isinstance(status, dict) else None
        alive = primary.get("alive") is True if isinstance(primary, dict) else False
        if not alive:
            _notify("Connect the plugin first, then retry Resync.", ok=False)
            return
        summary = _fetch_json("/context/summary")
        payload = {"mode": "full", "includeSources": True}
        if isinstance(summary, dict) and summary.get("projectKey"):
            payload["projectKey"] = summary["projectKey"]
        res = _post_json("/context/request", payload, timeout=4.0)
        if res and res.get("ok"):
            _notify("Resync requested. Watch Studio logs for the export.")
        else:
            _notify(f"Resync failed: {res or 'server unavailable'}", ok=False)

    def send_feedback(args: list) -> None:
        if not args:
            append_chat("system", "Usage: /feedback <score> [note] OR /feedback <adapter> <score> [note]")
            return
        adapter = None
        score = None
        note = ""
        if len(args) >= 2:
            try:
                score = float(args[1])
                adapter = args[0]
                note = " ".join(args[2:]).strip()
            except ValueError:
                try:
                    score = float(args[0])
                    note = " ".join(args[1:]).strip()
                except ValueError:
                    score = None
        else:
            try:
                score = float(args[0])
            except ValueError:
                score = None
        if score is None:
            append_chat("system", "Usage: /feedback <score> [note] OR /feedback <adapter> <score> [note]")
            return
        if not adapter:
            models = _fetch_json("/ai/models")
            adapter = models.get("defaultAdapter") if models else None
        if not adapter:
            append_chat("system", "No adapter available for feedback.")
            return
        payload = {"adapter": adapter, "score": score, "note": note}
        res = _post_json("/ai/moe/feedback", payload)
        if res and res.get("ok"):
            append_chat("system", f"Feedback sent to {adapter} (score={score}).")
        else:
            append_chat("system", f"Feedback failed: {res or 'unknown error'}")

    def handle_command(text: str) -> bool:
        cmdline = text.strip()
        if not cmdline.startswith("/"):
            return False
        parts = cmdline.split()
        cmd = parts[0].lower()
        args = parts[1:]
        if cmd in ("/apply", "/discard"):
            append_chat("system", "Apply is AI-driven now. Just respond to the AI’s questions.")
            return True
        if cmd == "/models":
            show_models()
            return True
        if cmd == "/status":
            show_status()
            return True
        if cmd == "/resync":
            request_resync()
            return True
        if cmd == "/feedback":
            send_feedback(args)
            return True
        if cmd == "/memory":
            append_chat("system", "Memory and scope are AI-driven now.")
            return True
        if cmd in ("/history", "/recap"):
            chat = _get_current_chat()
            messages = chat.get("messages") or []
            if not messages:
                append_chat("assistant", "No prior messages in this chat yet.")
                return True
            user_lines = [m.get("content", "") for m in messages if m.get("role") == "user"]
            assistant_lines = [m.get("content", "") for m in messages if m.get("role") == "assistant"]
            user_recent = [line for line in user_lines[-5:] if line]
            assistant_recent = [line for line in assistant_lines[-3:] if line]
            parts = []
            if user_recent:
                parts.append("Your recent messages:")
                for line in user_recent:
                    parts.append(f"- {line}")
            if assistant_recent:
                parts.append("My recent replies:")
                for line in assistant_recent:
                    parts.append(f"- {line}")
            append_chat("assistant", "\n".join(parts) if parts else "No prior messages in this chat yet.")
            return True
        if cmd == "/rollback":
            if not plan_state["steps"]:
                append_chat("system", "No checklist steps available to roll back.")
                return True
            append_chat("user", "Rollback the last applied step.")
            token = str(uuid.uuid4())
            show_thinking(token)
            threading.Thread(target=run_request, args=("Rollback the last applied step.", token), daemon=True).start()
            return True
        if cmd == "/help":
            append_chat(
                "system",
                "Commands: /status, /resync, /models, /feedback, /chat, /rollback, /history, /help",
            )
            return True
        return False

    def run_request(user_text: str, token: str, confirmed: bool = False) -> None:
        if not codex_cmd:
            append_chat("system", "Codex CLI not found. Install Codex and run `codex login`.")
            clear_thinking(token)
            return
        summary = get_context_summary()
        status_snapshot = _fetch_json("/status")
        alive = False
        if status_snapshot and isinstance(status_snapshot, dict):
            primary = status_snapshot.get("primary") or {}
            alive = primary.get("alive") is True
        try:
            with active_lock:
                active_request["cancelled"] = False
                active_request["token"] = token
            root.after(0, lambda: _set_input_enabled(False))
            force_no_apply = _is_tentative(user_text) and not confirmed
            memory_mode = memory_var.get()
            if memory_mode not in MEMORY_MODES:
                memory_mode = "Brief"
            chat = _get_current_chat()
            chat["memoryMode"] = memory_mode
            messages = chat.get("messages") or []
            history_limit = 6 if memory_mode == "Brief" else 14 if memory_mode == "Full" else 4
            memory_summary = chat.get("memorySummary") if memory_mode != "Off" else None
            if memory_mode == "Full" and not memory_summary and messages:
                history_limit = len(messages)
            elif memory_mode != "Off" and not memory_summary and messages:
                history_limit = min(max(len(messages), history_limit), 20)
            history = _format_history(chat, history_limit, exclude_user_prompt=user_text)

            if _is_test_request(user_text):
                test_name = "PersponifyCodex_Test"
                actions = [
                    {
                        "type": "createInstance",
                        "parentPath": "game/Workspace",
                        "className": "Folder",
                        "name": test_name,
                        "ifExists": "replace",
                    },
                    {"type": "deleteInstance", "path": f"game/Workspace/{test_name}"},
                ]
                _set_steps([{"id": "1", "title": "Smoke test", "actions": actions}], [])
                plan_state["apply_ready"] = True
                plan_state["awaiting_confirm"] = False
                plan_state["awaiting_actions"] = False
                _update_checklist_visibility([], plan_state["steps"], "Smoke test")
                append_chat(
                    "assistant",
                    "Running a quick smoke test: create + delete a temp folder in Workspace. "
                    "Check Studio output and the plugin log for the apply receipt.",
                )
                if alive is False:
                    append_chat("assistant", "Connect the plugin first, then tell me to apply.")
                    plan_state["apply_ready"] = False
                    plan_state["awaiting_confirm"] = True
                else:
                    _enqueue_next_step()
                return

            prompt = _build_chat_prompt(summary, memory_summary, history, user_text)
            extra_blocks = []
            if status_snapshot and isinstance(status_snapshot, dict):
                extra_blocks.append(
                    "Server status: " + ("online; plugin connected" if alive else "online; plugin disconnected")
                )
            if messages:
                extra_blocks.append(
                    f"Chat history count: {len(messages)} messages (included: {len(history)})."
                )
            if plan_state["items"]:
                extra_blocks.append("Checklist state:")
                extra_blocks.append(json.dumps(plan_state["items"], indent=2))
            if plan_state["receipts"]:
                extra_blocks.append("Recent receipts:")
                extra_blocks.append(json.dumps(plan_state["receipts"][-3:], indent=2))
            if confirmed and plan_state["awaiting_actions"]:
                extra_blocks.append(
                    "User confirmed. Provide <actions_json> for the previously proposed plan. "
                    "Do not ask more questions unless you are blocked."
                )
                if plan_state["items"]:
                    extra_blocks.append("Proposed plan:")
                    extra_blocks.append(json.dumps(plan_state["items"], indent=2))
            if extra_blocks:
                prompt = prompt + "\n\n" + "\n".join(extra_blocks)
            def _store_proc(proc: Optional[subprocess.Popen]) -> None:
                with active_lock:
                    active_request["proc"] = proc

            def _clear_proc() -> None:
                with active_lock:
                    active_request["proc"] = None

            def _is_cancelled() -> bool:
                with active_lock:
                    return bool(active_request.get("cancelled"))

            output = _run_codex(
                codex_cmd,
                prompt,
                CODEX_TIMEOUT_SEC,
                tmp_dir,
                str(uuid.uuid4()),
                on_proc=_store_proc,
                on_done=_clear_proc,
                cancel_check=_is_cancelled,
            )
            if not output:
                output = "(no response)"

            cleaned_raw, actions_payload = _extract_actions_block(output)
            cleaned_display = _strip_control_lines(cleaned_raw)
            if cleaned_display:
                append_chat("assistant", cleaned_display)
                _apply_directives(cleaned_raw, summary)
            else:
                append_chat("assistant", "(no response)")

            plan_items = []
            steps = []
            summary_text = ""
            actions = None
            if isinstance(actions_payload, dict):
                plan_items = _plan_items_from_payload(actions_payload)
                steps = _steps_from_payload(actions_payload)
                actions = actions_payload.get("actions")
                summary_text = actions_payload.get("summary") or ""
                if summary_text:
                    _apply_directives(summary_text, summary)

            if _is_test_request(user_text) and actions_payload is None and not steps and actions is None:
                test_prompt = _build_apply_prompt(
                    summary,
                    memory_summary,
                    "Create a minimal smoke test: create Folder PersponifyCodex_Test under game/Workspace "
                    "then delete it. Return <actions_json> only.",
                )
                test_output = _run_codex(
                    codex_cmd,
                    test_prompt,
                    CODEX_TIMEOUT_SEC,
                    tmp_dir,
                    str(uuid.uuid4()),
                    on_proc=_store_proc,
                    on_done=_clear_proc,
                    cancel_check=_is_cancelled,
                )
                test_cleaned, test_payload = _extract_actions_block(test_output)
                if isinstance(test_payload, dict):
                    actions = test_payload.get("actions")
                    summary_text = test_payload.get("summary") or "Smoke test ready."
                    _apply_directives(summary_text, summary)
                    if isinstance(actions, list):
                        _set_steps([{"id": "1", "title": "Smoke test", "actions": actions}], [])

            if not plan_items:
                plan_items = _parse_plan_lines(cleaned_raw)
            if steps:
                _set_steps(steps, plan_items)
            elif isinstance(actions, list):
                title = plan_items[0].get("title") if plan_items else "Apply changes"
                _set_steps([{"id": "1", "title": title, "actions": actions}], plan_items)
            elif plan_items:
                _set_plan_items(plan_items)
                plan_state["awaiting_actions"] = True
            else:
                plan_state["awaiting_actions"] = False
                plan_state["awaiting_confirm"] = False

            apply_ready = _should_apply(summary_text or cleaned_raw)
            if _is_test_request(user_text) and plan_state["steps"]:
                apply_ready = True
            if force_no_apply and apply_ready:
                apply_ready = False
                plan_state["awaiting_confirm"] = True
                if "confirm" not in (cleaned_display or "").lower():
                    append_chat("assistant", "Want me to apply this?")
            plan_state["apply_ready"] = apply_ready
            if not apply_ready and plan_state["steps"]:
                plan_state["awaiting_confirm"] = True
            if apply_ready and not plan_state["steps"] and not isinstance(actions, list):
                plan_state["awaiting_actions"] = True
            _update_checklist_visibility(plan_items, steps, summary_text or cleaned_raw)
            if apply_ready and plan_state["steps"]:
                if alive is False:
                    append_chat("assistant", "Connect the plugin first, then tell me to apply.")
                    plan_state["apply_ready"] = False
                    plan_state["awaiting_confirm"] = True
                else:
                    _enqueue_next_step()
            elif apply_ready and isinstance(actions, list):
                if alive is False:
                    append_chat("assistant", "Connect the plugin first, then tell me to apply.")
                    plan_state["apply_ready"] = False
                    plan_state["awaiting_confirm"] = True

            if memory_mode == "Brief":
                threading.Thread(
                    target=_update_memory_summary,
                    args=(chat, summary),
                    daemon=True,
                ).start()
        except Exception as exc:
            if str(exc) == "Cancelled":
                append_chat("assistant", "Thinking stopped.")
            else:
                append_chat("system", f"Codex error: {exc}")
        finally:
            clear_thinking(token)
            root.after(0, lambda: _set_input_enabled(True))

    def on_send() -> None:
        if input_lock["locked"]:
            return
        text = input_box.get("1.0", "end").strip()
        if not text:
            return
        input_box.delete("1.0", "end")
        if _is_memory_query(text):
            append_chat("user", text)
            chat = _get_current_chat()
            mode = chat.get("memoryMode") if chat.get("memoryMode") in MEMORY_MODES else memory_var.get()
            summary = chat.get("memorySummary") or ""
            if mode == "Off":
                append_chat("assistant", "Memory is off for this chat.")
            elif summary:
                append_chat("assistant", f"Memory is {mode}. Current summary: {summary}")
            else:
                append_chat("assistant", f"Memory is {mode}, but nothing is saved yet.")
            return
        if _is_history_query(text):
            handle_command(text)
            return
        if text.startswith("/chat"):
            chat_text_value = text[len("/chat") :].strip()
            if not chat_text_value:
                append_chat("system", "Usage: /chat <message>")
                return
            append_chat("user", chat_text_value)
            token = str(uuid.uuid4())
            show_thinking(token)
            threading.Thread(target=run_request, args=(chat_text_value, token), daemon=True).start()
            return
        if handle_command(text):
            return
        if _is_cancel(text):
            plan_state["items"] = []
            plan_state["steps"] = []
            plan_state["active_index"] = 0
            plan_state["inflight_tx"] = None
            plan_state["tx_by_step"] = {}
            plan_state["step_by_tx"] = {}
            plan_state["awaiting_actions"] = False
            plan_state["awaiting_confirm"] = False
            plan_state["apply_ready"] = False
            _set_checklist_visible(False)
            _render_plan()
            append_chat("assistant", "Okay — canceled.")
            return
        append_chat("user", text)
        token = str(uuid.uuid4())
        show_thinking(token)
        confirmed = _is_confirmation(text)
        if confirmed and plan_state["awaiting_confirm"] and plan_state["steps"]:
            status_snapshot = _fetch_json("/status")
            primary = status_snapshot.get("primary") if isinstance(status_snapshot, dict) else None
            alive = primary.get("alive") is True if isinstance(primary, dict) else False
            if not alive:
                append_chat("assistant", "Connect the plugin first, then tell me to apply.")
                plan_state["apply_ready"] = False
                plan_state["awaiting_confirm"] = True
                return
            plan_state["apply_ready"] = True
            plan_state["awaiting_confirm"] = False
            append_chat("assistant", "Got it — applying now.")
            _enqueue_next_step()
            return
        threading.Thread(target=run_request, args=(text, token, confirmed), daemon=True).start()

    def on_shift_return(_event) -> str:
        input_box.insert("insert", "\n")
        return "break"

    send_btn.bind("<Button-1>", lambda _e: on_send())
    stop_btn.bind("<Button-1>", lambda _e: _cancel_active_request())
    input_box.bind("<Return>", lambda _e: (on_send(), "break"))
    input_box.bind("<Shift-Return>", on_shift_return)
    root.bind("<Escape>", lambda _e: _cancel_active_request())

    def on_new_chat() -> None:
        chat = _get_current_chat()
        scope_key = chat.get("scopeKey")
        scope_label = chat.get("scopeLabel") or "Unscoped"
        chat_id = _create_chat(scope_key, scope_label)
        _refresh_chat_menu(chat_id)

    def on_rename_chat() -> None:
        chat = _get_current_chat()
        current_name = chat.get("name") or "Chat"
        new_name = simpledialog.askstring("Rename Chat", "New name:", initialvalue=current_name)
        if not new_name:
            return
        chat["name"] = new_name.strip()
        chat["updatedAt"] = time.time()
        _save_state()
        _refresh_chat_menu(chat.get("id"))

    def on_delete_chat() -> None:
        nonlocal current_chat_id
        chat = _get_current_chat()
        name = chat.get("name") or "Chat"
        confirm = messagebox.askyesno("Delete Chat", f"Delete '{name}'?")
        if not confirm:
            return
        chat_id = chat.get("id")
        if chat_id in chat_state:
            del chat_state[chat_id]
        for scope_key, stored_id in list(state_data.get("lastChatByScope", {}).items()):
            if stored_id == chat_id:
                state_data["lastChatByScope"].pop(scope_key, None)
        if state_data.get("lastChatId") == chat_id:
            state_data["lastChatId"] = None
        _save_state()
        current_chat_id = None
        if chat_state:
            new_id = next(iter(chat_state.keys()))
            _refresh_chat_menu(new_id)
        else:
            new_id = _create_chat(None, "Unscoped")
            _refresh_chat_menu(new_id)

    def on_memory_change(*_args) -> None:
        mode = memory_var.get()
        if mode not in MEMORY_MODES:
            return
        chat = _get_current_chat()
        chat["memoryMode"] = mode
        _save_state()
        _update_header_layout()

    def on_scope_change(*_args) -> None:
        scope = scope_var.get()
        if scope not in SCOPE_MODES:
            return
        state_data["scopeMode"] = scope
        _save_state()
        summary = _fetch_json("/context/summary")
        if summary and summary.get("ok"):
            summary = dict(summary)
            summary.pop("ok", None)
            scope_key, scope_label = _scope_from_summary(summary, scope)
            _ensure_chat_for_scope(scope_key, scope_label)
        _update_header_layout()

    new_chat_btn.bind("<Button-1>", lambda _e: on_new_chat())
    rename_chat_btn.bind("<Button-1>", lambda _e: on_rename_chat())
    delete_chat_btn.bind("<Button-1>", lambda _e: on_delete_chat())
    memory_var.trace_add("write", on_memory_change)
    scope_var.trace_add("write", on_scope_change)

    def on_set_repo() -> None:
        nonlocal codex_cmd
        selection = filedialog.askdirectory(title="Select PersponifyCodex folder")
        if not selection:
            return
        candidate = Path(selection).expanduser().resolve()
        if not _looks_like_repo(candidate):
            messagebox.showerror("Persponify Codex", "That folder doesn't look like a PersponifyCodex repo.")
            return
        _write_config(candidate)
        repo_label.configure(text=f"Repo: {candidate}")
        codex_cmd = _detect_codex_cmd(candidate)
        messagebox.showinfo(
            "Persponify Codex",
            "Repo path saved. Use Restart to switch to the new repo.",
        )

    def on_restart() -> None:
        controller.stop()
        worker.stop()
        cfg = _read_config()
        repo_override = None
        if isinstance(cfg, dict):
            repo_override = cfg.get("repoPath")
        if isinstance(repo_override, str) and repo_override:
            candidate = Path(repo_override).expanduser().resolve()
            if _looks_like_repo(candidate) and candidate != ROOT:
                python_path = _select_restart_python(cfg)
                launcher_path = candidate / "codex_launcher.py"
                os.chdir(str(candidate))
                os.execv(python_path, [python_path, str(launcher_path)])
                return
        try:
            controller.start()
            worker.start()
        except Exception as exc:
            messagebox.showerror("Persponify Codex", f"Restart failed: {exc}")
        refresh_labels()

    def on_restart_server() -> None:
        controller.stop()
        try:
            controller.start()
        except Exception as exc:
            messagebox.showerror("Persponify Codex", f"Server restart failed: {exc}")
        refresh_labels()

    def on_resync() -> None:
        request_resync()

    set_repo = action_button(
        button_row,
        "Set Repo…",
        on_set_repo,
        THEME["green_dim"],
        THEME["text"],
        THEME["green"],
        THEME["panel"],
    )
    set_repo.pack(side="left")

    restart_btn = action_button(
        button_row,
        "Restart",
        on_restart,
        THEME["border"],
        THEME["text"],
        THEME["green"],
        THEME["panel"],
    )
    restart_btn.pack(side="left", padx=(8, 0))

    restart_server_btn = action_button(
        button_row,
        "Restart Server",
        on_restart_server,
        THEME["panel"],
        THEME["text"],
        THEME["green"],
        THEME["panel"],
    )
    restart_server_btn.pack(side="left", padx=(8, 0))

    resync_btn = action_button(
        button_row,
        "Resync",
        on_resync,
        THEME["panel"],
        THEME["text"],
        THEME["green"],
        THEME["panel"],
    )
    resync_btn.pack(side="left", padx=(8, 0))

    def on_register_mcp() -> None:
        _register_mcp(repo_display, show_dialog=True)

    register_mcp_btn = action_button(
        button_row,
        "Register Codex",
        on_register_mcp,
        THEME["panel"],
        THEME["text"],
        THEME["green"],
        THEME["panel"],
    )
    register_mcp_btn.pack(side="left", padx=(8, 0))

    def refresh_labels() -> None:
        if worker.proc is None:
            state.worker = "Worker: —"
        elif worker.proc.poll() is None:
            state.worker = "Worker: running"
        else:
            state.worker = "Worker: stopped"

        server_label.config(text=state.server, fg=THEME["green"] if "OK" in state.server else THEME["red"])
        plugin_label.config(
            text=state.plugin,
            fg=THEME["green"] if "connected" in state.plugin else THEME["yellow"],
        )
        worker_label.config(
            text=state.worker,
            fg=THEME["green"] if "running" in state.worker else THEME["yellow"],
        )
        codex_label.config(
            text=state.codex,
            fg=THEME["text"] if "err=—" in state.codex else THEME["yellow"],
        )
        root.after(int(POLL_SEC * 1000), refresh_labels)

    def _auto_register_mcp() -> None:
        try:
            _register_mcp(repo_display, show_dialog=False)
        except Exception:
            pass

    threading.Thread(target=_auto_register_mcp, daemon=True).start()

    def _mcp_expected_url() -> str:
        base = os.environ.get("PERSPONIFY_SERVER_URL", "http://127.0.0.1:3030").rstrip("/")
        return f"{base}/mcp"

    def _mcp_is_registered(codex_bin: str, expected_url: str) -> bool:
        try:
            proc = subprocess.run(
                [codex_bin, "mcp", "get", "persponify", "--json"],
                capture_output=True,
                text=True,
                check=False,
                env=_launcher_env(),
            )
        except Exception:
            return False
        if proc.returncode != 0:
            return False
        try:
            data = json.loads(proc.stdout.strip())
        except Exception:
            data = None
        if isinstance(data, dict):
            transport = data.get("transport")
            if isinstance(transport, dict):
                if transport.get("type") == "http" and transport.get("url") == expected_url:
                    return True
        return expected_url in proc.stdout

    def _register_mcp(_repo_path: Path, show_dialog: bool = True) -> None:
        codex_bin = _find_codex_bin()
        if not codex_bin:
            if show_dialog:
                messagebox.showerror("Persponify Codex", "Codex CLI not found. Install Codex and run `codex login`.")
            return
        expected_url = _mcp_expected_url()
        if _mcp_is_registered(codex_bin, expected_url):
            if show_dialog:
                messagebox.showinfo("Persponify Codex", "MCP server already registered.")
            return
        try:
            subprocess.run(
                [codex_bin, "mcp", "remove", "persponify"],
                capture_output=True,
                text=True,
                check=False,
                env=_launcher_env(),
            )
            cmd = [codex_bin, "mcp", "add", "persponify", "--url", expected_url]
            proc = subprocess.run(cmd, capture_output=True, text=True, check=False, env=_launcher_env())
        except Exception as exc:
            if show_dialog:
                messagebox.showerror("Persponify Codex", f"MCP registration failed: {exc}")
            return
        if proc.returncode != 0:
            if show_dialog:
                messagebox.showerror("Persponify Codex", f"MCP registration failed: {proc.stderr.strip()}")
            return
        if show_dialog:
            messagebox.showinfo("Persponify Codex", "MCP server registered.")

    def on_close() -> None:
        stop_event.set()
        controller.stop()
        worker.stop()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)

    def _handle_signal(_sig, _frame):
        try:
            root.after(0, on_close)
        except Exception:
            on_close()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handle_signal)
        except Exception:
            pass

    t = threading.Thread(target=poll_loop, args=(state, stop_event, None), daemon=True)
    t.start()

    if not chat_state:
        current_chat_id = _create_chat(None, "Unscoped")
    _refresh_chat_menu(current_chat_id)
    on_scope_change()

    _render_plan()
    _render_receipts()
    _render_diagnostics()
    _set_checklist_visible(plan_state["visible"])
    refresh_labels()
    compact_state["ready"] = True
    _set_compact_mode(False)
    root.after(0, _apply_min_size)
    root.mainloop()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--nogui", action="store_true")
    args, _ = parser.parse_known_args()

    if os.environ.get("PERSPONIFY_LAUNCHER_NOGUI") == "1":
        args.nogui = True

    if args.nogui:
        return _run_headless()
    return _run_gui()


if __name__ == "__main__":
    raise SystemExit(main())
