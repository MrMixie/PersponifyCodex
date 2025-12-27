#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Iterable, Optional
from urllib import error, request


DEFAULT_SERVER = os.environ.get("PERSPONIFY_SERVER_URL", "http://127.0.0.1:3030")


def _http_post_json(url: str, payload: dict, timeout: float = 8.0) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
        if not body:
            return {"ok": True}
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return {"ok": False, "error": body}
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8") if exc.fp else ""
        return {"ok": False, "error": body or str(exc)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _parse_source_path(source: str) -> Optional[str]:
    for line in source.splitlines()[:6]:
        stripped = line.strip()
        if stripped.lower().startswith("-- source path:"):
            path = stripped.split(":", 1)[1].strip()
            if path:
                return path
    return None


def _map_file_to_path(path: Path, source: str) -> Optional[str]:
    src = _parse_source_path(source)
    if src:
        return src
    name = path.stem
    if not name:
        return None
    parts = name.split("__")
    if not parts:
        return None
    return "game/" + "/".join(parts)


def _collect_all_lua(context_dir: Path) -> list[Path]:
    files: list[Path] = []
    for root, _, names in os.walk(context_dir):
        for name in names:
            if name.endswith(".lua"):
                files.append(Path(root) / name)
    return sorted(files)


def _git_changed_paths(repo_root: Path) -> set[Path]:
    cmd = ["git", "-C", str(repo_root), "status", "--porcelain", "--", "context_scripts"]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        return set()
    changed: set[Path] = set()
    for line in proc.stdout.splitlines():
        if not line:
            continue
        entry = line[3:].strip()
        if " -> " in entry:
            entry = entry.split(" -> ", 1)[1].strip()
        if entry:
            changed.add(repo_root / entry)
    return changed


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _build_actions(files: Iterable[Path]) -> tuple[list[dict], list[str]]:
    actions: list[dict] = []
    skipped: list[str] = []
    for path in files:
        try:
            source = _read_text(path)
        except Exception:
            skipped.append(str(path))
            continue
        target_path = _map_file_to_path(path, source)
        if not target_path:
            skipped.append(str(path))
            continue
        actions.append(
            {
                "type": "editScript",
                "path": target_path,
                "mode": "replace",
                "source": source,
            }
        )
    return actions, skipped


def _enqueue_actions(base_url: str, actions: list[dict], batch_size: int) -> list[dict]:
    results: list[dict] = []
    if batch_size <= 0:
        batch_size = len(actions)
    for i in range(0, len(actions), batch_size):
        batch = actions[i : i + batch_size]
        tx = {
            "protocolVersion": 1,
            "transactionId": f"TX_QUICK_{uuid.uuid4()}",
            "actions": batch,
        }
        results.append(_http_post_json(f"{base_url}/enqueue", {"tx": tx}))
    return results


def _git_has_staged(repo_root: Path) -> bool:
    proc = subprocess.run(
        ["git", "-C", str(repo_root), "diff", "--cached", "--quiet"],
        capture_output=True,
        text=True,
    )
    return proc.returncode == 1


def _git_has_changes(repo_root: Path) -> bool:
    proc = subprocess.run(
        ["git", "-C", str(repo_root), "diff", "--quiet"],
        capture_output=True,
        text=True,
    )
    return proc.returncode == 1


def _git_add(repo_root: Path, files: list[Path]) -> None:
    if not files:
        return
    rels = [str(path.relative_to(repo_root)) for path in files]
    subprocess.check_call(["git", "-C", str(repo_root), "add", "--", *rels])


def _git_commit(repo_root: Path, message: str) -> bool:
    if not _git_has_staged(repo_root):
        return False
    subprocess.check_call(["git", "-C", str(repo_root), "commit", "-m", message])
    return True


def _git_push(repo_root: Path) -> None:
    subprocess.check_call(["git", "-C", str(repo_root), "push"])


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync context_scripts to Studio quickly.")
    parser.add_argument("--server", default=DEFAULT_SERVER, help="Server base URL")
    parser.add_argument("--all", action="store_true", help="Sync all Lua files")
    parser.add_argument(
        "--file",
        action="append",
        dest="files",
        default=[],
        help="Specific context script file (repeatable)",
    )
    parser.add_argument("--batch", type=int, default=0, help="Actions per enqueue (0=all)")
    parser.add_argument("--dry-run", action="store_true", help="Show what would happen")
    parser.add_argument("--commit", action="store_true", help="Commit context scripts")
    parser.add_argument("--push", action="store_true", help="Push after commit")
    parser.add_argument(
        "--message",
        default="chore: sync context scripts",
        help="Commit message",
    )
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[1]
    context_dir = repo_root / "context_scripts"
    if not context_dir.exists():
        print("Missing context_scripts/ directory.", file=sys.stderr)
        return 1

    selected_files: list[Path] = []
    if args.files:
        for raw in args.files:
            path = (repo_root / raw).resolve()
            if path.exists():
                selected_files.append(path)
            else:
                print(f"Missing file: {raw}", file=sys.stderr)
                return 1
    elif not args.all:
        changed = _git_changed_paths(repo_root)
        if changed:
            selected_files = sorted(changed)
        else:
            selected_files = _collect_all_lua(context_dir)
    else:
        selected_files = _collect_all_lua(context_dir)

    if not selected_files:
        print("No context scripts found.")
        return 0

    actions, skipped = _build_actions(selected_files)
    if skipped:
        print("Skipped (missing path header or unreadable):")
        for path in skipped:
            print(f" - {path}")
    if not actions:
        print("No actions to enqueue.")
        return 1

    if args.dry_run:
        print(f"Would enqueue {len(actions)} actions.")
    else:
        results = _enqueue_actions(args.server.rstrip("/"), actions, args.batch)
        print(f"Enqueued {len(actions)} actions in {len(results)} batch(es).")
        for idx, res in enumerate(results, start=1):
            if not res or not res.get("ok", True):
                print(f"Batch {idx} error: {res}")

    if args.commit or args.push:
        _git_add(repo_root, selected_files)
        if _git_has_changes(repo_root) and not _git_has_staged(repo_root):
            print("No staged changes to commit.")
        else:
            if args.commit or args.push:
                committed = _git_commit(repo_root, args.message)
                if committed:
                    print("Committed.")
        if args.push:
            _git_push(repo_root)
            print("Pushed.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
