#!/usr/bin/env python3
"""Bootstrap for the Persponify Codex launcher app.

Reads a user config to find the repo and launches codex_launcher.py from there.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional


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


SUPPORT_DIR = _support_dir()
CONFIG_PATH = SUPPORT_DIR / "launcher.json"
# Common repo names so the bootstrap can auto-detect without prompting.
DEFAULT_REPO_NAMES = [
    "PersponifyCodex",
    "PersponifyCodexRepo",
    "PersponifyCodexServer",
]


def _alert(message: str) -> None:
    if sys.platform == "darwin":
        try:
            subprocess.run(
                [
                    "osascript",
                    "-e",
                    f'display alert "Persponify Codex Launcher" message "{message}" as critical',
                ],
                check=False,
            )
            return
        except Exception:
            pass
    print(message, file=sys.stderr)


def _prompt_for_repo() -> Optional[Path]:
    if sys.platform == "darwin":
        try:
            result = subprocess.run(
                [
                    "osascript",
                    "-e",
                    'POSIX path of (choose folder with prompt "Select your PersponifyCodex folder")',
                ],
                check=False,
                capture_output=True,
                text=True,
            )
        except Exception:
            return None
        if result.returncode != 0:
            return None
        selected = (result.stdout or "").strip()
        if not selected:
            return None
        return Path(selected).expanduser().resolve()
    try:
        selected = input("Select your PersponifyCodex folder: ").strip()
    except Exception:
        return None
    if not selected:
        return None
    return Path(selected).expanduser().resolve()


def _load_config() -> Optional[dict]:
    if not CONFIG_PATH.exists():
        return None
    try:
        return json.loads(CONFIG_PATH.read_text())
    except Exception:
        return None


def _looks_like_repo(path: Path) -> bool:
    return (path / "codex_launcher.py").exists() and (path / "app.py").exists()


def _auto_detect_repo() -> Optional[Path]:
    override = os.environ.get("PERSPONIFY_CODEX_REPO")
    if override:
        candidate = Path(override).expanduser().resolve()
        if _looks_like_repo(candidate):
            return candidate

    script_path = Path(__file__).resolve()
    app_root = None
    for parent in script_path.parents:
        if parent.name.endswith(".app"):
            app_root = parent
            break
    if app_root:
        for parent in [app_root.parent] + list(app_root.parent.parents)[:3]:
            if _looks_like_repo(parent):
                return parent

    for base in [Path.cwd(), Path.home()]:
        if not base.exists():
            continue
        for name in DEFAULT_REPO_NAMES:
            candidate = (base / name).resolve()
            if _looks_like_repo(candidate):
                return candidate
    return None


def _write_config(repo_path: Path, python_path: str) -> None:
    SUPPORT_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(
        json.dumps({"repoPath": str(repo_path), "pythonPath": python_path or ""}, indent=2)
    )


def _probe_python(path: str) -> bool:
    try:
        result = subprocess.run(
            [path, "-c", "import tkinter as tk; r=tk.Tk(); r.update_idletasks(); r.destroy(); print('ok')"],
            check=False,
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONNOUSERSITE": "1"},
            timeout=3,
        )
    except Exception:
        return False
    if result.returncode != 0:
        return False
    return "ok" in (result.stdout or "")


def _select_python(config: Optional[dict], require_tk: bool) -> Optional[str]:
    if config:
        override = config.get("pythonPath")
        if isinstance(override, str) and override and Path(override).exists():
            if not require_tk:
                return override
            if _probe_python(override):
                return override

    candidates = [
        "/usr/local/bin/python3.11",
        "/opt/homebrew/bin/python3.11",
        "/usr/local/bin/python3.12",
        "/opt/homebrew/bin/python3.12",
        "/usr/local/bin/python3",
        "/opt/homebrew/bin/python3",
        "/usr/bin/python3",
    ]
    for path in candidates:
        if not Path(path).exists():
            continue
        if not require_tk:
            return path
        if _probe_python(path):
            return path
    return None


def main() -> int:
    config = _load_config()
    python_path = _select_python(config, require_tk=True)
    nogui = False
    if not python_path:
        python_path = _select_python(config, require_tk=False)
        nogui = True
    if not python_path:
        _alert("No python3 found. Install python3 and try again.")
        return 1

    repo_path = None
    if config:
        repo_path = config.get("repoPath")
    if not isinstance(repo_path, str) or not repo_path:
        detected = _auto_detect_repo()
        if detected:
            _write_config(detected, python_path)
            repo_path = str(detected)
        else:
            picked = _prompt_for_repo()
            if picked and _looks_like_repo(picked):
                _write_config(picked, python_path)
                repo_path = str(picked)
            else:
                SUPPORT_DIR.mkdir(parents=True, exist_ok=True)
                if not CONFIG_PATH.exists():
                    CONFIG_PATH.write_text(json.dumps({"repoPath": "", "pythonPath": ""}, indent=2))
                _alert(
                    "Missing launcher config. Edit "
                    f"{CONFIG_PATH} and set repoPath to your PersponifyCodex folder."
                )
                return 1

    repo = Path(repo_path).expanduser().resolve()
    launcher = repo / "codex_launcher.py"
    if not launcher.exists():
        _alert(f"codex_launcher.py not found in {repo}.")
        return 1

    os.chdir(str(repo))
    args = [python_path, str(launcher)]
    if nogui:
        args.append("--nogui")
    os.execv(python_path, args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
