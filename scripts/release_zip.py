#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional
from urllib import error, parse, request


DEFAULT_REPO = "MrMixie/PersponifyCodex"


def _git_output(repo_root: Path, args: list[str]) -> str:
    cmd = ["git", "-C", str(repo_root), *args]
    return subprocess.check_output(cmd, text=True).strip()


def _latest_tag(repo_root: Path) -> Optional[str]:
    try:
        return _git_output(repo_root, ["describe", "--tags", "--abbrev=0"])
    except Exception:
        return None


def _build_zip(repo_root: Path, ref: str, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "git",
        "-C",
        str(repo_root),
        "archive",
        "--format=zip",
        "--output",
        str(out_path),
        ref,
    ]
    subprocess.check_call(cmd)


def _github_request(
    url: str,
    token: Optional[str],
    method: str = "GET",
    data: Optional[bytes] = None,
    extra_headers: Optional[dict] = None,
) -> dict:
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"token {token}"
    if extra_headers:
        headers.update(extra_headers)
    req = request.Request(url, data=data, headers=headers, method=method)
    try:
        with request.urlopen(req, timeout=15) as resp:
            body = resp.read()
            if not body:
                return {"ok": True}
            try:
                return json.loads(body.decode("utf-8"))
            except json.JSONDecodeError:
                return {"ok": True, "raw": body.decode("utf-8")}
    except error.HTTPError as exc:
        payload = exc.read().decode("utf-8") if exc.fp else str(exc)
        raise RuntimeError(f"GitHub API error ({exc.code}): {payload}") from exc


def _get_release(repo: str, tag: str, token: Optional[str]) -> dict:
    url = f"https://api.github.com/repos/{repo}/releases/tags/{tag}"
    return _github_request(url, token)


def _delete_asset(repo: str, asset_id: int, token: str) -> None:
    url = f"https://api.github.com/repos/{repo}/releases/assets/{asset_id}"
    _github_request(url, token, method="DELETE")


def _upload_asset(repo: str, release_id: int, zip_path: Path, token: str) -> dict:
    name = parse.quote(zip_path.name)
    url = (
        f"https://uploads.github.com/repos/{repo}/releases/{release_id}/assets"
        f"?name={name}"
    )
    data = zip_path.read_bytes()
    headers = {"Content-Type": "application/zip"}
    return _github_request(url, token, method="POST", data=data, extra_headers=headers)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build and optionally upload release ZIP.")
    parser.add_argument("--repo", default=DEFAULT_REPO, help="GitHub repo (owner/name)")
    parser.add_argument("--tag", default=None, help="Release tag (default: latest tag)")
    parser.add_argument("--ref", default="HEAD", help="Git ref to archive (default: HEAD)")
    parser.add_argument("--dist", default="dist", help="Output directory (default: dist)")
    parser.add_argument("--upload", action="store_true", help="Upload to GitHub release")
    parser.add_argument(
        "--token",
        default=None,
        help="GitHub token (or set GH_TOKEN/GITHUB_TOKEN)",
    )
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[1]
    tag = args.tag or _latest_tag(repo_root)
    if not tag:
        print("No git tag found; pass --tag explicitly.", file=sys.stderr)
        return 1

    zip_name = f"PersponifyCodex-{tag}.zip"
    zip_path = repo_root / args.dist / zip_name
    _build_zip(repo_root, args.ref, zip_path)
    print(f"Built: {zip_path}")

    if not args.upload:
        return 0

    token = args.token or os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        print("Missing GitHub token (use --token or GH_TOKEN/GITHUB_TOKEN).", file=sys.stderr)
        return 1

    release = _get_release(args.repo, tag, token)
    release_id = release.get("id")
    if not release_id:
        print(f"Release tag not found: {tag}", file=sys.stderr)
        return 1

    existing = None
    for asset in release.get("assets", []):
        if asset.get("name") == zip_name:
            existing = asset
            break
    if existing:
        _delete_asset(args.repo, existing.get("id"), token)
        print(f"Deleted existing asset: {zip_name}")

    result = _upload_asset(args.repo, release_id, zip_path, token)
    asset_id = result.get("id")
    print(f"Uploaded asset id: {asset_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
