#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
import time
import uuid
from typing import Any, Dict, Optional
from urllib import error, request

from mcp_common import handle_request as mcp_handle_request, tool_list as mcp_tool_list
SERVER_URL = os.environ.get("PERSPONIFY_SERVER_URL", "http://127.0.0.1:3030").rstrip("/")
PROTOCOL_VERSION = 1
MIN_PYTHON = (3, 9)
DEBUG = os.environ.get("PERSPONIFY_MCP_DEBUG", "").strip().lower() in {"1", "true", "yes", "on"}
LOG_PATH = os.environ.get(
    "PERSPONIFY_MCP_LOG_PATH", os.path.expanduser("~/.codex/log/persponify_mcp_server.log")
)
_LOG_FILE = None


def _log(message: str) -> None:
    if not DEBUG:
        return
    global _LOG_FILE
    try:
        if _LOG_FILE is None:
            os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
            _LOG_FILE = open(LOG_PATH, "a", encoding="utf-8")
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        _LOG_FILE.write(f"[{timestamp}] {message}\n")
        _LOG_FILE.flush()
    except Exception:
        try:
            sys.stderr.write(f"Persponify MCP log failure: {message}\n")
            sys.stderr.flush()
        except Exception:
            pass


class _NoMessage(Exception):
    pass


def _read_message() -> Optional[dict]:
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        if line in (b"\r\n", b"\n"):
            break
        try:
            key, value = line.decode("utf-8").split(":", 1)
        except ValueError:
            continue
        headers[key.strip().lower()] = value.strip()

    length = int(headers.get("content-length", "0"))
    if length <= 0:
        raise _NoMessage()
    body = sys.stdin.buffer.read(length)
    if not body:
        raise _NoMessage()
    try:
        return json.loads(body.decode("utf-8"))
    except Exception as exc:
        _log(f"Failed to decode JSON message: {exc}")
        raise _NoMessage() from exc


def _send_message(payload: dict) -> None:
    data = json.dumps(payload).encode("utf-8")
    sys.stdout.buffer.write(b"Content-Length: " + str(len(data)).encode("utf-8") + b"\r\n\r\n")
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


def _http_get(path: str) -> Optional[dict]:
    url = SERVER_URL + path
    try:
        with request.urlopen(url, timeout=4.0) as resp:
            body = resp.read().decode("utf-8")
        return json.loads(body) if body else {"ok": True}
    except Exception:
        return None


def _resolve_project_key() -> str:
    status = _http_get("/status")
    primary = status.get("primary") if isinstance(status, dict) else None
    if isinstance(primary, dict):
        place = primary.get("placeId")
        session = primary.get("studioSessionId")
        if place and session:
            return f"p_{place}__s_{session}"
    return "default"


def _http_post(path: str, payload: dict) -> dict:
    url = SERVER_URL + path
    data = json.dumps(payload).encode("utf-8")
    req = request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with request.urlopen(req, timeout=6.0) as resp:
            body = resp.read().decode("utf-8")
        return json.loads(body) if body else {"ok": True}
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8") if exc.fp else ""
        return {"ok": False, "error": body or str(exc)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _tool_list() -> dict:
    return mcp_tool_list()


def _tool_call(name: str, args: Dict[str, Any]) -> dict:
    if name == "enqueue_actions":
        actions = args.get("actions")
        if not isinstance(actions, list):
            return {"isError": True, "content": [{"type": "text", "text": "actions must be a list"}]}
        tx_id = args.get("transactionId") or f"TX_MCP_{uuid.uuid4()}"
        tx = {"protocolVersion": PROTOCOL_VERSION, "transactionId": tx_id, "actions": actions}
        res = _http_post("/enqueue", {"tx": tx})
        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps({"transactionId": tx_id, "result": res}, indent=2),
                }
            ]
        }

    if name == "get_status":
        res = _http_get("/status")
        return {"content": [{"type": "text", "text": json.dumps(res or {}, indent=2)}]}

    if name == "get_context_summary":
        project_key = _resolve_project_key()
        res = _http_get(f"/context/summary?projectKey={project_key}")
        if isinstance(res, dict) and res.get("detail") == "NoContext" and project_key != "default":
            res = _http_get("/context/summary")
        return {"content": [{"type": "text", "text": json.dumps(res or {}, indent=2)}]}

    if name == "request_context_export":
        payload = {}
        for key in ("projectKey", "roots", "paths", "includeSources", "mode"):
            if key in args:
                payload[key] = args[key]
        res = _http_post("/context/request", payload)
        return {"content": [{"type": "text", "text": json.dumps(res or {}, indent=2)}]}

    return {"isError": True, "content": [{"type": "text", "text": f"Unknown tool: {name}"}]}


def _handle_request(req: dict) -> Optional[dict]:
    return mcp_handle_request(req, _tool_call)


def main() -> int:
    if sys.version_info < MIN_PYTHON:
        _log(
            f"Python too old: {sys.version_info.major}.{sys.version_info.minor} "
            f"(min {MIN_PYTHON[0]}.{MIN_PYTHON[1]})"
        )
        sys.stderr.write(
            f"Persponify MCP requires Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ "
            f"(found {sys.version_info.major}.{sys.version_info.minor}).\n"
        )
        sys.stderr.flush()
        return 1
    _log(f"Startup ok. Python {sys.version_info.major}.{sys.version_info.minor}")
    _log(f"Server URL: {SERVER_URL}")
    while True:
        try:
            req = _read_message()
        except _NoMessage:
            continue
        if req is None:
            _log("Stdin closed; exiting.")
            break
        _log(f"Received method={req.get('method')} id={req.get('id')}")
        res = _handle_request(req)
        if res:
            _send_message(res)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
