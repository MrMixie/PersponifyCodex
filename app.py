# app.py
# Persponify Studio AI — Local Test Companion Server (FastAPI)
# v1 test server + Context Export API (Read Mode)
#
# Endpoints:
#   GET  /health
#   GET  /discover          (alias of /health; for future clients)
#   GET  /status            (summary: primary + queue + last receipt)
#   POST /register
#   GET  /sync
#   POST /wait            (long-poll; 204 when no tx)
#   POST /enqueue         (test helper; scoped by primary)
#   POST /enqueue_mock    (test helper; AUTO-scope if omitted)
#   POST /receipt
#   POST /heartbeat
#   POST /mcp             (MCP over HTTP for Codex)
#   GET  /mcp             (MCP info/health)
#
# Debug:
#   GET  /debug/state         (AUTO-scope if omitted)
#   GET  /debug/last_wait     (AUTO-scope if omitted)
#   GET  /debug/last_receipt  (AUTO-scope if omitted)
#   POST /debug/reset
#   POST /debug/fault
#
# Scope helpers:
#   GET  /scope/current
#
# Context (READ MODE tests):
#   POST /context/export   (AUTO-scope if omitted; still requires primary)
#   GET  /context/latest   (AUTO-scope if omitted)
#   GET  /context/summary  (AUTO-scope if omitted)
#   GET  /context/semantic (AUTO-scope if omitted; semantic tags/deps)
#   GET  /context/script   (AUTO-scope if omitted; returns one script if present)
#   GET  /context/missing  (AUTO-scope if omitted; scripts missing source)
#   POST /context/reset    (AUTO-scope if omitted; still requires primary)
#
# Helpers:
#   POST /util/chunk_source  (split large script source into chunks)
#   POST /util/edit_script_tx (build editScript tx with chunks)
# Asset search:
#   GET  /assets/search
#   GET  /assets/info
# Note: keep the endpoint list above in sync with any new routes.

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
import threading
import traceback
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import FastAPI, HTTPException, Query, Body, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from companion.service import HeadlessService
from mcp_common import handle_request as mcp_handle_request

APP_VERSION = "0.1.1"
PROTOCOL_VERSION = 1
DEFAULT_CHUNK_SIZE = 60000
DEFAULT_COMPANION_CONFIG = "companion/config.example.json"

# ----------------------------
# Models
# ----------------------------

class RegisterIn(BaseModel):
    clientId: str
    studioSessionId: str
    placeId: int
    clientTag: Optional[str] = None
    takeover: Optional[bool] = None

class RegisterOut(BaseModel):
    leaseToken: str
    fence: int
    serverSeq: int

class SyncOut(BaseModel):
    rulesVersion: str = APP_VERSION
    recommendedSince: int = 0
    meta: Dict[str, Any] = Field(default_factory=dict)

class WaitIn(BaseModel):
    leaseToken: str
    fence: int
    since: int
    placeId: int
    studioSessionId: str
    clientTag: Optional[str] = None
    timeoutSec: Optional[float] = None  # optional override

class ReleaseIn(BaseModel):
    leaseToken: str
    fence: int
    clientId: Optional[str] = None
    studioSessionId: Optional[str] = None
    reason: Optional[str] = None

class TxEnvelope(BaseModel):
    protocolVersion: int = PROTOCOL_VERSION
    transactionId: str
    actions: List[Dict[str, Any]]

class WaitOut(BaseModel):
    seq: int
    fence: int
    claimToken: str
    tx: TxEnvelope

class EnqueueIn(BaseModel):
    tx: TxEnvelope

class ReceiptIn(BaseModel):
    leaseToken: str
    fence: Optional[int] = None
    claimToken: str
    transactionId: str

    applied: List[Any] = Field(default_factory=list)
    errors: List[Any] = Field(default_factory=list)
    notes: List[Any] = Field(default_factory=list)

    meta: Optional[Dict[str, Any]] = None
    clientTag: Optional[str] = None

class HeartbeatIn(BaseModel):
    leaseToken: str
    fence: int
    studioSessionId: str
    placeId: int
    clientTag: Optional[str] = None

# Context export (READ MODE)
class ContextTreeItem(BaseModel):
    path: str
    className: str
    children: Optional[int] = None

class ContextScriptItem(BaseModel):
    path: str
    className: str
    sha1: Optional[str] = None
    bytes: Optional[int] = None
    source: Optional[str] = None  # optionally omitted for large scripts
    sourceTruncated: Optional[bool] = None
    sourceOmittedReason: Optional[str] = None
    attributes: Optional[Dict[str, Any]] = None
    tags: Optional[List[str]] = None

class ContextExportIn(BaseModel):
    projectKey: str = "default"
    meta: Dict[str, Any] = Field(default_factory=dict)
    tree: List[ContextTreeItem] = Field(default_factory=list)
    scripts: List[ContextScriptItem] = Field(default_factory=list)

class ContextRequestIn(BaseModel):
    projectKey: Optional[str] = None
    roots: Optional[List[str]] = None
    paths: Optional[List[str]] = None
    includeSources: Optional[bool] = None
    mode: Optional[str] = None  # "full" | "diff"

class TelemetryReportIn(BaseModel):
    projectKey: str = "default"
    meta: Dict[str, Any] = Field(default_factory=dict)
    camera: Optional[Dict[str, Any]] = None
    selection: Optional[List[Any]] = None
    nodes: Optional[List[Any]] = None
    ui: Optional[List[Any]] = None
    lighting: Optional[Dict[str, Any]] = None
    services: Optional[List[str]] = None
    logs: Optional[List[Any]] = None
    diffs: Optional[List[Any]] = None
    assets: Optional[Dict[str, Any]] = None
    tagIndex: Optional[Dict[str, Any]] = None
    uiQa: Optional[Dict[str, Any]] = None

    class Config:
        extra = "allow"

class TelemetryRequestIn(BaseModel):
    projectKey: Optional[str] = None
    roots: Optional[List[str]] = None
    paths: Optional[List[str]] = None
    includeScene: Optional[bool] = None
    includeGui: Optional[bool] = None
    includeLighting: Optional[bool] = None
    includeSelection: Optional[bool] = None
    includeCamera: Optional[bool] = None
    includeLogs: Optional[bool] = None
    includeDiffs: Optional[bool] = None
    includeAssets: Optional[bool] = None
    includeTagIndex: Optional[bool] = None
    includeUiQa: Optional[bool] = None

class CatalogSearchRequestIn(BaseModel):
    projectKey: Optional[str] = None
    query: Optional[str] = None
    assetTypes: Optional[List[Any]] = None
    bundleTypes: Optional[List[Any]] = None
    category: Optional[str] = None
    sortType: Optional[str] = None
    sortAggregation: Optional[str] = None
    salesType: Optional[str] = None
    minPrice: Optional[int] = None
    maxPrice: Optional[int] = None
    maxResults: Optional[int] = None

class CatalogSearchReportIn(BaseModel):
    projectKey: str = "default"
    query: Optional[str] = None
    requestedAt: Optional[float] = None
    requestId: Optional[str] = None
    results: Optional[List[Dict[str, Any]]] = None
    error: Optional[str] = None
    meta: Dict[str, Any] = Field(default_factory=dict)

class ContextMemoryIn(BaseModel):
    projectKey: str = "default"
    memory: str

# AI (Companion)
class AiCompleteIn(BaseModel):
    prompt: str
    system: Optional[str] = None
    adapter: Optional[str] = None

class AiSecretsIn(BaseModel):
    byAdapter: Optional[Dict[str, Dict[str, str]]] = None
    byType: Optional[Dict[str, Dict[str, str]]] = None
    replace: Optional[bool] = False

class AiAdapterModelsIn(BaseModel):
    adapter: str

class AiMoeIn(BaseModel):
    prompt: str
    system: Optional[str] = None
    mergeAdapter: Optional[str] = None
    masterAdapter: Optional[str] = None
    maxExperts: Optional[int] = None
    includeAdapters: Optional[List[str]] = None
    timeoutSec: Optional[float] = None
    autoSelect: Optional[bool] = True
    learn: Optional[bool] = True

class AiMoeFeedbackIn(BaseModel):
    adapter: str
    score: float = Field(..., ge=-1.0, le=1.0)
    note: Optional[str] = None

class AiMemoryIn(BaseModel):
    transcript: str
    summary: Optional[str] = None
    adapter: Optional[str] = None
    maxChars: Optional[int] = None

# Codex bridge
class CodexJobIn(BaseModel):
    prompt: str
    system: Optional[str] = None
    intent: Optional[str] = None
    autoApply: Optional[bool] = True
    placeId: Optional[int] = None
    studioSessionId: Optional[str] = None
    projectKey: Optional[str] = None

# ----------------------------
# In-memory state
# ----------------------------

# Local-only server; keep it off public interfaces.
app = FastAPI(title="Persponify Studio AI Test Server", version=APP_VERSION)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

_lock = threading.RLock()
_cv = threading.Condition(_lock)
_svc_lock = threading.RLock()
_service: Optional[HeadlessService] = None

# Primary lease state
_primary_lease_token: Optional[str] = None
_primary_session_id: Optional[str] = None
_primary_client_id: Optional[str] = None
_primary_place_id: Optional[int] = None
_fence: int = 0
_last_heartbeat: float = 0.0

# Queue + sequencing
_seq: int = 0
_queue: List[Dict[str, Any]] = []  # items: {"seq": int, "tx": dict, "claimToken": str, "claimed": bool, "scope": (placeId, sessionId)}
_claims: Dict[str, Dict[str, Any]] = {}  # claimToken -> {"expiresAt": float, "seq": int, "txId": str, "scope": (placeId, sessionId)}

# Debug traces
_last_wait: Dict[Tuple[int, str], Dict[str, Any]] = {}
_last_receipt: Dict[Tuple[int, str], Dict[str, Any]] = {}
_fault: Dict[str, Any] = {"mode": None}

# Context store
_context_latest: Dict[Tuple[int, str, str], Dict[str, Any]] = {}  # (placeId, studioSessionId, projectKey) -> payload
_context_versions: Dict[Tuple[int, str, str], int] = {}
_context_deltas: Dict[Tuple[int, str, str], Dict[str, Any]] = {}
_context_memory: Dict[Tuple[int, str, str], str] = {}
_context_last_export_at: Dict[Tuple[int, str, str], float] = {}
_context_semantic: Dict[Tuple[int, str, str], Dict[str, Any]] = {}
_context_memory_mtime: Dict[Tuple[int, str, str], float] = {}
_context_export_requests: Dict[Tuple[int, str], Dict[str, Any]] = {}
_context_fingerprints: Dict[Tuple[int, str, str], str] = {}

# Telemetry store
_telemetry_latest: Dict[Tuple[int, str, str], Dict[str, Any]] = {}
_telemetry_versions: Dict[Tuple[int, str, str], int] = {}
_telemetry_last_export_at: Dict[Tuple[int, str, str], float] = {}
_telemetry_fingerprints: Dict[Tuple[int, str, str], str] = {}
_telemetry_export_requests: Dict[Tuple[int, str], Dict[str, Any]] = {}
_telemetry_history: Dict[Tuple[int, str, str], List[Dict[str, Any]]] = {}
_catalog_search_requests: Dict[Tuple[int, str], Dict[str, Any]] = {}
_catalog_latest: Dict[Tuple[int, str, str], Dict[str, Any]] = {}
_catalog_versions: Dict[Tuple[int, str, str], int] = {}

# Asset cache
_asset_info_cache: Dict[int, Dict[str, Any]] = {}
_asset_info_cache_exp: Dict[int, float] = {}
_asset_search_cache: Dict[str, Dict[str, Any]] = {}
_asset_search_cache_exp: Dict[str, float] = {}

# Codex bridge state
_codex_lock = threading.RLock()
_codex_state: Dict[str, Any] = {
    "lastJob": None,
    "lastResponse": None,
    "lastError": None,
}
_codex_jobs_index: Dict[str, Dict[str, Any]] = {}
_codex_tx_job_map: Dict[str, str] = {}
_codex_repair_attempts: Dict[str, int] = {}
_codex_repair_last: Dict[str, float] = {}
_action_stats: Dict[str, Any] = {"total": 0, "byType": {}}
_audit_lock = threading.RLock()

def _codex_queue_root() -> Path:
    return Path(os.environ.get("PERSPONIFY_CODEX_QUEUE", "codex_queue")).resolve()

CODEX_QUEUE_DIR = _codex_queue_root()
CODEX_JOBS_DIR = CODEX_QUEUE_DIR / "jobs"
CODEX_RESP_DIR = CODEX_QUEUE_DIR / "responses"
CODEX_ACKS_DIR = CODEX_QUEUE_DIR / "acks"
CODEX_ERRORS_DIR = CODEX_QUEUE_DIR / "errors"
CODEX_CONTEXT_DIR = CODEX_QUEUE_DIR / "context"

for _p in (CODEX_QUEUE_DIR, CODEX_JOBS_DIR, CODEX_RESP_DIR, CODEX_ACKS_DIR, CODEX_ERRORS_DIR, CODEX_CONTEXT_DIR):
    _p.mkdir(parents=True, exist_ok=True)

# Small parsing helpers (used by constants)
def _parse_csv_set(value: Optional[str], default: Optional[List[str]] = None) -> set:
    if not value:
        return set(default or [])
    return {v.strip() for v in value.split(",") if v.strip()}

# Tuning
HEARTBEAT_TTL_SEC = 15.0
CLAIM_TTL_SEC = 30.0
DEFAULT_WAIT_TIMEOUT_SEC = 25.0
CODEX_JOB_TTL_SEC = float(os.environ.get("PERSPONIFY_CODEX_JOB_TTL_SEC", "600"))
CODEX_MAX_ACTIONS = int(os.environ.get("PERSPONIFY_CODEX_MAX_ACTIONS", "400"))
CODEX_MAX_SOURCE_BYTES = int(os.environ.get("PERSPONIFY_CODEX_MAX_SOURCE_BYTES", "400000"))
CONTEXT_DELTA_MAX_ITEMS = int(os.environ.get("PERSPONIFY_CONTEXT_DELTA_MAX_ITEMS", "200"))
TELEMETRY_HISTORY_LIMIT = int(os.environ.get("PERSPONIFY_TELEMETRY_HISTORY_LIMIT", "20"))
ASSET_INFO_CACHE_TTL_SEC = float(os.environ.get("PERSPONIFY_ASSET_INFO_CACHE_TTL_SEC", "600"))
ASSET_SEARCH_CACHE_TTL_SEC = float(os.environ.get("PERSPONIFY_ASSET_SEARCH_CACHE_TTL_SEC", "60"))
CODEX_ALLOWED_EDIT_MODES = {
    "replace",
    "append",
    "prepend",
    "replaceRange",
    "insertBefore",
    "insertAfter",
}
CODEX_POLICY_PROFILE = os.environ.get("PERSPONIFY_CODEX_POLICY_PROFILE", "power")
CODEX_PROTECTED_ROOTS = _parse_csv_set(os.environ.get("PERSPONIFY_CODEX_PROTECTED_ROOTS"))
CODEX_AUTO_REPAIR = os.environ.get("PERSPONIFY_CODEX_AUTO_REPAIR", "0") == "1"
CODEX_REPAIR_MAX_ATTEMPTS = int(os.environ.get("PERSPONIFY_CODEX_REPAIR_MAX_ATTEMPTS", "2"))
CODEX_REPAIR_COOLDOWN_SEC = float(os.environ.get("PERSPONIFY_CODEX_REPAIR_COOLDOWN_SEC", "8"))
CODEX_FOCUS_MAX_SCRIPTS = int(os.environ.get("PERSPONIFY_CODEX_FOCUS_MAX_SCRIPTS", "8"))
CODEX_FOCUS_MAX_BYTES = int(os.environ.get("PERSPONIFY_CODEX_FOCUS_MAX_BYTES", "20000"))
CONTEXT_EXPORT_MIN_INTERVAL_SEC = float(os.environ.get("PERSPONIFY_CONTEXT_MIN_INTERVAL_SEC", "0"))
CODEX_MAX_RISK = float(os.environ.get("PERSPONIFY_CODEX_MAX_RISK", "0.7"))
MAX_QUEUE_SIZE = int(os.environ.get("PERSPONIFY_MAX_QUEUE", "1000"))
AUDIT_LEDGER_LIMIT = int(os.environ.get("PERSPONIFY_AUDIT_LEDGER_LIMIT", "200"))
CODEX_PACKS_ENABLED = os.environ.get("PERSPONIFY_CODEX_PACKS_ENABLED", "1") != "0"
CODEX_PACK_MAX_ITEMS = int(os.environ.get("PERSPONIFY_CODEX_PACK_MAX_ITEMS", "40"))
CODEX_PACK_MAX_EDGES = int(os.environ.get("PERSPONIFY_CODEX_PACK_MAX_EDGES", "200"))
CODEX_PACK_MAX_REQUIRES = int(os.environ.get("PERSPONIFY_CODEX_PACK_MAX_REQUIRES", "8"))
CODEX_PACK_MAX_SNAPSHOTS = int(os.environ.get("PERSPONIFY_CODEX_PACK_MAX_SNAPSHOTS", "12"))
CODEX_SCRIPT_INDEX_MAX = int(os.environ.get("PERSPONIFY_CODEX_SCRIPT_INDEX_MAX", "200"))
SEMANTIC_ENABLED = os.environ.get("PERSPONIFY_SEMANTIC_ENABLED", "1") != "0"
SEMANTIC_KEYWORD_LIMIT = int(os.environ.get("PERSPONIFY_SEMANTIC_KEYWORD_LIMIT", "20"))
SEMANTIC_SYMBOL_LIMIT = int(os.environ.get("PERSPONIFY_SEMANTIC_SYMBOL_LIMIT", "40"))
SEMANTIC_MAX_REQUIRES = int(os.environ.get("PERSPONIFY_SEMANTIC_MAX_REQUIRES", "30"))
SEMANTIC_MAX_SERVICES = int(os.environ.get("PERSPONIFY_SEMANTIC_MAX_SERVICES", "30"))
SEMANTIC_MAX_SOURCE_BYTES = int(os.environ.get("PERSPONIFY_SEMANTIC_MAX_SOURCE_BYTES", "350000"))
RECONCILE_INTERVAL_SEC = float(os.environ.get("PERSPONIFY_RECONCILE_INTERVAL_SEC", "15"))
CONTEXT_EVENTS_PATH = Path(
    os.environ.get("PERSPONIFY_CONTEXT_EVENTS_LOG", str(CODEX_CONTEXT_DIR / "context_events.log"))
).resolve()
AUDIT_LOG_PATH = Path(
    os.environ.get("PERSPONIFY_AUDIT_LOG", str(CODEX_QUEUE_DIR / "audit.log"))
).resolve()
QUEUE_STATE_PATH = Path(
    os.environ.get("PERSPONIFY_QUEUE_STATE", str(CODEX_QUEUE_DIR / "queue_state.json"))
).resolve()
SQLITE_ENABLED = os.environ.get("PERSPONIFY_SQLITE_ENABLED", "1") != "0"
SQLITE_PATH = Path(
    os.environ.get("PERSPONIFY_SQLITE_PATH", str(CODEX_QUEUE_DIR / "codex_state.db"))
).resolve()
SQLITE_TIMEOUT_SEC = float(os.environ.get("PERSPONIFY_SQLITE_TIMEOUT_SEC", "3"))
ASSET_SEARCH_TIMEOUT_SEC = float(os.environ.get("PERSPONIFY_ASSET_SEARCH_TIMEOUT_SEC", "6"))
ASSET_SEARCH_MAX_RESULTS = int(os.environ.get("PERSPONIFY_ASSET_SEARCH_MAX_RESULTS", "30"))
ASSET_SEARCH_ALLOWED_LIMITS = {10, 28, 30, 50, 60, 100, 120}
ASSET_SEARCH_USER_AGENT = os.environ.get("PERSPONIFY_ASSET_SEARCH_USER_AGENT", "PersponifyCodex/0.1")

# Action catalog for Codex jobs (plugin supports these)
SUPPORTED_ACTIONS = [
    "createInstance",
    "insertAsset",
    "setProperty",
    "setProperties",
    "cloneInstance",
    "clearChildren",
    "setTags",
    "deleteInstance",
    "rename",
    "move",
    "setAttribute",
    "setAttributes",
    "editScript",
    "tween",
    "emitParticles",
    "playSound",
    "animationCreate",
    "animationAddKeyframe",
    "animationPreview",
    "animationStop",
]

# Companion config
def _companion_config_path() -> str:
    return os.environ.get("PERSPONIFY_COMPANION_CONFIG", DEFAULT_COMPANION_CONFIG)

def _get_service() -> HeadlessService:
    global _service
    with _svc_lock:
        if _service is None:
            _service = HeadlessService.from_path(_companion_config_path())
        return _service

def _reload_service() -> HeadlessService:
    global _service
    with _svc_lock:
        _service = HeadlessService.from_path(_companion_config_path())
        return _service

# ----------------------------
# Helpers
# ----------------------------

def _now() -> float:
    return time.time()

def _pick_asset_search_limit(requested: Optional[int]) -> int:
    if requested is None:
        return 10
    try:
        value = int(requested)
    except (TypeError, ValueError):
        return 10
    if value in ASSET_SEARCH_ALLOWED_LIMITS:
        return value
    for candidate in sorted(ASSET_SEARCH_ALLOWED_LIMITS):
        if value <= candidate:
            return candidate
    return max(ASSET_SEARCH_ALLOWED_LIMITS)

def _http_get_json(url: str, timeout_sec: float) -> Tuple[bool, Any, str]:
    req = urllib.request.Request(url, headers={"User-Agent": ASSET_SEARCH_USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            raw = resp.read().decode("utf-8")
        return True, json.loads(raw), ""
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8") if exc.fp else ""
        return False, body or f"HTTP {exc.code}", f"HTTP {exc.code}"
    except Exception as exc:  # pragma: no cover - network exceptions vary
        return False, str(exc), "error"

def _catalog_search_ids(query: str, limit: int, cursor: Optional[str]) -> Tuple[bool, Dict[str, Any], str]:
    cache_key = f"{query}|{limit}|{cursor or ''}"
    if ASSET_SEARCH_CACHE_TTL_SEC > 0:
        exp = _asset_search_cache_exp.get(cache_key)
        if exp and exp > _now():
            cached = _asset_search_cache.get(cache_key)
            if isinstance(cached, dict):
                return True, cached, ""
    params = {"keyword": query, "limit": str(limit)}
    if cursor:
        params["cursor"] = cursor
    url = "https://catalog.roblox.com/v1/search/items?" + urllib.parse.urlencode(params)
    ok, data, err = _http_get_json(url, ASSET_SEARCH_TIMEOUT_SEC)
    if not ok:
        return False, {"error": data}, err
    if ASSET_SEARCH_CACHE_TTL_SEC > 0 and isinstance(data, dict):
        _asset_search_cache[cache_key] = data
        _asset_search_cache_exp[cache_key] = _now() + ASSET_SEARCH_CACHE_TTL_SEC
    return True, data, ""

def _marketplace_info(asset_id: int) -> Tuple[bool, Dict[str, Any], str]:
    if ASSET_INFO_CACHE_TTL_SEC > 0:
        exp = _asset_info_cache_exp.get(asset_id)
        if exp and exp > _now():
            cached = _asset_info_cache.get(asset_id)
            if isinstance(cached, dict):
                return True, cached, ""
    url = "https://api.roblox.com/marketplace/productinfo?assetId=" + str(asset_id)
    ok, data, err = _http_get_json(url, ASSET_SEARCH_TIMEOUT_SEC)
    if not ok:
        return False, {"error": data}, err
    if isinstance(data, dict) and data.get("AssetId") is None:
        data["AssetId"] = asset_id
    if ASSET_INFO_CACHE_TTL_SEC > 0 and isinstance(data, dict):
        _asset_info_cache[asset_id] = data
        _asset_info_cache_exp[asset_id] = _now() + ASSET_INFO_CACHE_TTL_SEC
    return True, data, ""

CODEX_DENY_ACTIONS = _parse_csv_set(
    os.environ.get("PERSPONIFY_CODEX_DENY_ACTIONS"),
    default=[],
)

def _scope_key(place_id: int, session_id: str) -> Tuple[int, str]:
    return (int(place_id), str(session_id))

def _is_primary_alive() -> bool:
    if not _primary_lease_token:
        return False
    return (_now() - _last_heartbeat) <= HEARTBEAT_TTL_SEC

def _reset_primary_unlocked() -> None:
    global _primary_lease_token, _primary_session_id, _primary_client_id, _primary_place_id
    _primary_lease_token = None
    _primary_session_id = None
    _primary_client_id = None
    _primary_place_id = None

def _cleanup_claims_unlocked() -> None:
    now = _now()
    expired = [k for k, v in _claims.items() if v.get("expiresAt", 0) <= now]
    for k in expired:
        del _claims[k]

def _next_seq_unlocked() -> int:
    global _seq
    _seq += 1
    return _seq

def _require_primary_scope(placeId: int, studioSessionId: str) -> None:
    # Strict scope for core RPCs
    if placeId != _primary_place_id or studioSessionId != _primary_session_id:
        raise HTTPException(status_code=409, detail="ScopeMismatch")

def _maybe_fault_delay(name: str) -> None:
    # Fault injection: {"mode":"delay_wait","sec":2}
    if _fault.get("mode") == name and _fault.get("sec"):
        time.sleep(float(_fault["sec"]))

def _resolve_scope_auto(placeId: Optional[int], studioSessionId: Optional[str]) -> Tuple[int, str]:
    """
    AUTO scope (for test/debug/context helpers):
      - If both are provided: use them
      - Else, if primary exists: use primary scope
      - Else: raise 400 (no scope available)
    """
    if placeId is not None and studioSessionId is not None:
        return _scope_key(placeId, studioSessionId)

    if _primary_place_id is not None and _primary_session_id is not None:
        return _scope_key(_primary_place_id, _primary_session_id)

    raise HTTPException(status_code=400, detail="No scope provided and no primary is connected.")

def _resolve_project_key(pid: int, sid: str, project_key: Optional[str]) -> str:
    key = str(project_key or "default")
    if key not in ("", "default"):
        return key

    candidates: List[Tuple[float, str]] = []
    for (p, s, k), ts in _context_last_export_at.items():
        if int(p) == int(pid) and str(s) == str(sid):
            candidates.append((float(ts), str(k)))
    if not candidates:
        for (p, s, k) in _context_latest.keys():
            if int(p) == int(pid) and str(s) == str(sid):
                candidates.append((_context_last_export_at.get((p, s, k), 0.0), str(k)))
    if not candidates:
        return key

    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]

def _resolve_scope_for_debug(placeId: Optional[int], studioSessionId: Optional[str]) -> Tuple[int, str]:
    # For convenience: allow missing and default to primary, but keep a clear error otherwise.
    try:
        return _resolve_scope_auto(placeId, studioSessionId)
    except HTTPException as e:
        # keep existing semantics somewhat similar to your previous version
        raise HTTPException(status_code=422, detail="placeId and studioSessionId required (or connect primary first)") from e

def _context_id(place_id: int, session_id: str, project_key: str) -> str:
    safe_key = str(project_key or "default").replace("/", "_")
    return f"p_{int(place_id)}__s_{str(session_id)}__k_{safe_key}"

def _telemetry_id(place_id: int, session_id: str, project_key: str) -> str:
    safe_key = str(project_key or "default").replace("/", "_")
    return f"p_{int(place_id)}__s_{str(session_id)}__k_{safe_key}"

def _telemetry_history_entry(payload: Dict[str, Any], version: int, fingerprint: Optional[str]) -> Dict[str, Any]:
    meta = payload.get("meta") if isinstance(payload, dict) else None
    if not isinstance(meta, dict):
        meta = {}
    counts = meta.get("counts") if isinstance(meta.get("counts"), dict) else {}
    ui_qa = payload.get("uiQa") if isinstance(payload, dict) else None
    ui_counts = ui_qa.get("counts") if isinstance(ui_qa, dict) else {}
    return {
        "version": version,
        "receivedAt": _now(),
        "fingerprint": fingerprint,
        "nodeCount": counts.get("nodes"),
        "uiCount": counts.get("ui"),
        "logCount": counts.get("logs"),
        "diffCount": counts.get("diffs"),
        "assetCount": counts.get("assets"),
        "uiIssueCount": ui_counts.get("issues"),
        "meta": {
            "include": meta.get("include"),
            "truncated": meta.get("truncated"),
            "scope": meta.get("scope"),
        },
    }

def _context_file_path(context_id: str) -> Path:
    return CODEX_CONTEXT_DIR / f"context_{context_id}.json"

def _context_memory_path(context_id: str) -> Path:
    return CODEX_CONTEXT_DIR / f"context_{context_id}.memory.txt"

def _write_atomic_json(path: Path, data: Dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    tmp.replace(path)

def _db_connect() -> Optional[sqlite3.Connection]:
    if not SQLITE_ENABLED:
        return None
    try:
        conn = sqlite3.connect(SQLITE_PATH, timeout=SQLITE_TIMEOUT_SEC)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn
    except Exception:
        return None

def _init_db() -> None:
    if not SQLITE_ENABLED:
        return
    conn = _db_connect()
    if conn is None:
        return
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL,
                event TEXT,
                payload_json TEXT
            );
            CREATE TABLE IF NOT EXISTS context_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL,
                event TEXT,
                payload_json TEXT
            );
            CREATE TABLE IF NOT EXISTS context_snapshots (
                context_id TEXT,
                version INTEGER,
                place_id INTEGER,
                session_id TEXT,
                project_key TEXT,
                created_at REAL,
                payload_json TEXT,
                PRIMARY KEY (context_id, version)
            );
            CREATE INDEX IF NOT EXISTS idx_context_snapshots_id ON context_snapshots(context_id);
            CREATE TABLE IF NOT EXISTS context_memory (
                context_id TEXT PRIMARY KEY,
                updated_at REAL,
                memory TEXT
            );
            CREATE TABLE IF NOT EXISTS context_semantic (
                context_id TEXT,
                version INTEGER,
                updated_at REAL,
                payload_json TEXT,
                PRIMARY KEY (context_id, version)
            );
            CREATE TABLE IF NOT EXISTS queue_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                seq INTEGER,
                payload_json TEXT,
                updated_at REAL
            );
            """
        )
        conn.commit()
    finally:
        conn.close()

def _db_exec(sql: str, params: Tuple[Any, ...]) -> None:
    conn = _db_connect()
    if conn is None:
        return
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()

def _db_fetch_one(sql: str, params: Tuple[Any, ...]) -> Optional[Tuple[Any, ...]]:
    conn = _db_connect()
    if conn is None:
        return None
    try:
        cur = conn.execute(sql, params)
        return cur.fetchone()
    finally:
        conn.close()

def _db_write_audit(record: Dict[str, Any]) -> None:
    payload = json.dumps(record, ensure_ascii=True)
    _db_exec(
        "INSERT INTO audit_log (ts, event, payload_json) VALUES (?, ?, ?)",
        (record.get("ts"), record.get("event"), payload),
    )

def _db_write_context_event(record: Dict[str, Any]) -> None:
    payload = json.dumps(record, ensure_ascii=True)
    _db_exec(
        "INSERT INTO context_events (ts, event, payload_json) VALUES (?, ?, ?)",
        (record.get("ts"), record.get("event"), payload),
    )

def _db_record_context_snapshot(
    context_id: str,
    version: int,
    place_id: int,
    session_id: str,
    project_key: str,
    payload: Dict[str, Any],
) -> None:
    payload_json = json.dumps(payload, ensure_ascii=True)
    _db_exec(
        "INSERT OR REPLACE INTO context_snapshots "
        "(context_id, version, place_id, session_id, project_key, created_at, payload_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (context_id, int(version), int(place_id), str(session_id), str(project_key), _now(), payload_json),
    )

def _db_load_latest_context(context_id: str) -> Optional[Dict[str, Any]]:
    row = _db_fetch_one(
        "SELECT payload_json FROM context_snapshots WHERE context_id = ? "
        "ORDER BY version DESC LIMIT 1",
        (context_id,),
    )
    if not row:
        return None
    try:
        return json.loads(row[0])
    except Exception:
        return None

def _db_record_context_memory(context_id: str, memory: str) -> None:
    _db_exec(
        "INSERT OR REPLACE INTO context_memory (context_id, updated_at, memory) VALUES (?, ?, ?)",
        (context_id, _now(), memory),
    )

def _db_load_context_memory(context_id: str) -> Optional[Tuple[float, str]]:
    row = _db_fetch_one(
        "SELECT updated_at, memory FROM context_memory WHERE context_id = ?",
        (context_id,),
    )
    if not row:
        return None
    try:
        return float(row[0]), str(row[1])
    except Exception:
        return None

def _record_semantic_snapshot(semantic: Dict[str, Any]) -> None:
    if not SQLITE_ENABLED:
        return
    context_id = semantic.get("contextId")
    version = semantic.get("contextVersion")
    if not context_id or version is None:
        return
    payload_json = json.dumps(semantic, ensure_ascii=True)
    _db_exec(
        "INSERT OR REPLACE INTO context_semantic (context_id, version, updated_at, payload_json) "
        "VALUES (?, ?, ?, ?)",
        (str(context_id), int(version), _now(), payload_json),
    )

def _db_load_latest_semantic(context_id: str) -> Optional[Dict[str, Any]]:
    row = _db_fetch_one(
        "SELECT payload_json FROM context_semantic WHERE context_id = ? "
        "ORDER BY version DESC LIMIT 1",
        (context_id,),
    )
    if not row:
        return None
    try:
        return json.loads(row[0])
    except Exception:
        return None

def _db_save_queue_state(data: Dict[str, Any]) -> None:
    _db_exec(
        "INSERT OR REPLACE INTO queue_state (id, seq, payload_json, updated_at) VALUES (1, ?, ?, ?)",
        (int(data.get("seq") or 0), json.dumps(data, ensure_ascii=True), _now()),
    )

def _db_load_queue_state() -> Optional[Dict[str, Any]]:
    row = _db_fetch_one("SELECT payload_json FROM queue_state WHERE id = 1", ())
    if not row:
        return None
    try:
        return json.loads(row[0])
    except Exception:
        return None

def _db_clear_context(context_id: str) -> None:
    if not SQLITE_ENABLED:
        return
    _db_exec("DELETE FROM context_snapshots WHERE context_id = ?", (context_id,))
    _db_exec("DELETE FROM context_memory WHERE context_id = ?", (context_id,))
    _db_exec("DELETE FROM context_semantic WHERE context_id = ?", (context_id,))

_init_db()

def _resolve_project_key_for_scope(place_id: int, session_id: str, requested: Optional[str]) -> str:
    if requested:
        return str(requested)
    keys = [k for (pid, sid, k) in _context_latest.keys() if pid == int(place_id) and sid == str(session_id)]
    if len(keys) == 1:
        return keys[0]
    if "default" in keys:
        return "default"
    if keys:
        return keys[0]
    return "default"

def _build_context_summary(context: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not context:
        return {"treeCount": 0, "scriptCount": 0, "totalScriptBytes": 0}
    scripts = context.get("scripts", []) or []
    tree = context.get("tree", []) or []
    total_bytes = 0
    for s in scripts:
        b = s.get("bytes")
        if isinstance(b, int):
            total_bytes += b
    summary = {
        "treeCount": len(tree),
        "scriptCount": len(scripts),
        "totalScriptBytes": total_bytes,
    }
    meta = context.get("meta") if isinstance(context, dict) else None
    if isinstance(meta, dict):
        summary["gameId"] = meta.get("gameId")
        summary["placeId"] = meta.get("placeId")
        summary["pluginVersion"] = meta.get("pluginVersion")
        summary["buildId"] = meta.get("buildId")
        summary["totalScriptChars"] = meta.get("totalScriptChars")
        summary["exportedScriptChars"] = meta.get("exportedScriptChars")
        summary["omittedSourceCount"] = meta.get("omittedSourceCount")
        summary["omittedByDiff"] = meta.get("omittedByDiff")
        summary["omittedBySize"] = meta.get("omittedBySize")
        summary["omittedByTotal"] = meta.get("omittedByTotal")
        summary["totalCapHit"] = meta.get("totalCapHit")
        summary["truncatedBySize"] = meta.get("truncatedBySize")
        summary["attributesIncluded"] = meta.get("attributesIncluded")
        summary["tagsIncluded"] = meta.get("tagsIncluded")
    return summary

def _truncate_list(items: List[Any], limit: int) -> List[Any]:
    if limit <= 0:
        return []
    if len(items) <= limit:
        return items
    return items[:limit]

def _script_fingerprint(item: Dict[str, Any]) -> str:
    sha1 = item.get("sha1")
    if isinstance(sha1, str) and sha1:
        return sha1
    src = item.get("source")
    if isinstance(src, str) and src:
        return "sha1:" + hashlib.sha1(src.encode("utf-8")).hexdigest()
    size = item.get("bytes")
    if isinstance(size, int):
        return f"bytes:{size}"
    return "unknown"

def _has_full_source(script: Dict[str, Any]) -> bool:
    if script.get("source") is None:
        return False
    return not bool(script.get("sourceTruncated"))

def _is_missing_source(script: Dict[str, Any]) -> bool:
    if _has_full_source(script):
        return False
    if script.get("sourceOmittedReason") == "diff":
        return False
    return True

SEMANTIC_STOPWORDS = {
    "and",
    "or",
    "the",
    "a",
    "an",
    "to",
    "for",
    "of",
    "in",
    "on",
    "with",
    "is",
    "are",
    "was",
    "were",
    "be",
    "this",
    "that",
    "then",
    "else",
    "do",
    "does",
    "did",
    "if",
    "elseif",
    "end",
    "local",
    "function",
    "return",
    "true",
    "false",
    "nil",
    "game",
    "script",
    "self",
}

RE_REQUIRE = re.compile(r"\brequire\s*\(\s*([^\)]+?)\s*\)", re.IGNORECASE)
RE_SERVICE = re.compile(r"GetService\s*\(\s*['\"]([^'\"]+)['\"]\s*\)")
RE_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
RE_FUNCTION_DEF = re.compile(r"\bfunction\s+([A-Za-z_][A-Za-z0-9_]*(?:[.:][A-Za-z_][A-Za-z0-9_]*)*)")

def _extract_services(source: Optional[str]) -> List[str]:
    if not source:
        return []
    found = []
    for match in RE_SERVICE.finditer(source):
        name = match.group(1)
        if name and name not in found:
            found.append(name)
        if len(found) >= SEMANTIC_MAX_SERVICES:
            break
    return found

def _extract_requires(source: Optional[str]) -> List[str]:
    if not source:
        return []
    found = []
    for match in RE_REQUIRE.finditer(source):
        arg = match.group(1).strip()
        if not arg:
            continue
        if arg not in found:
            found.append(arg)
        if len(found) >= SEMANTIC_MAX_REQUIRES:
            break
    return found

def _extract_keywords(source: Optional[str]) -> List[str]:
    if not source:
        return []
    tokens = RE_IDENTIFIER.findall(source)
    counts: Dict[str, int] = {}
    for token in tokens:
        lowered = token.lower()
        if lowered in SEMANTIC_STOPWORDS:
            continue
        if len(lowered) < 3:
            continue
        counts[lowered] = counts.get(lowered, 0) + 1
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return [token for token, _ in ranked[:SEMANTIC_KEYWORD_LIMIT]]

def _extract_symbol_lines(source: Optional[str]) -> List[Dict[str, Any]]:
    if not source:
        return []
    seen = set()
    symbols: List[Dict[str, Any]] = []
    for idx, line in enumerate(source.splitlines(), start=1):
        for match in RE_FUNCTION_DEF.finditer(line):
            name = match.group(1)
            if not name or name in seen:
                continue
            symbols.append({"name": name, "line": idx})
            seen.add(name)
            if len(symbols) >= SEMANTIC_SYMBOL_LIMIT:
                return symbols
    return symbols

def _semantic_tags_for_script(path: str, class_name: Optional[str], source: Optional[str], services: List[str]) -> List[str]:
    tags: set = set()
    lower_path = (path or "").lower()
    lower_source = (source or "").lower()

    if "serverscriptservice" in lower_path or "/server/" in lower_path:
        tags.add("server")
    if "starterplayerscripts" in lower_path or "startercharacterscripts" in lower_path:
        tags.add("client")
    if "startergui" in lower_path or "/ui" in lower_path or "/gui" in lower_path:
        tags.add("ui")
    if "replicatedstorage" in lower_path:
        tags.add("shared")
    if "serverstorage" in lower_path:
        tags.add("server_storage")

    if class_name in {"ScreenGui", "SurfaceGui", "BillboardGui", "TextLabel", "TextButton"}:
        tags.add("ui")

    if "datastoreservice" in lower_source or "datastore" in lower_source:
        tags.add("datastore")
    if "remotefunction" in lower_source or "remoteevent" in lower_source:
        tags.add("networking")
    if "httpservice" in lower_source:
        tags.add("http")
    if "tweenservice" in lower_source:
        tags.add("ui")
    if "pathfindingservice" in lower_source:
        tags.add("pathfinding")
    if "marketplaceservice" in lower_source:
        tags.add("commerce")
    if "messagingservice" in lower_source:
        tags.add("messaging")
    if "teleportservice" in lower_source:
        tags.add("teleport")
    if "physicsservice" in lower_source:
        tags.add("physics")
    if "runservice" in lower_source:
        tags.add("runtime")
    if "userinputservice" in lower_source:
        tags.add("input")

    for service in services:
        if service == "DataStoreService":
            tags.add("datastore")
        elif service == "HttpService":
            tags.add("http")
        elif service == "TweenService":
            tags.add("ui")
        elif service == "MarketplaceService":
            tags.add("commerce")
        elif service == "MessagingService":
            tags.add("messaging")
        elif service == "TeleportService":
            tags.add("teleport")
        elif service == "PhysicsService":
            tags.add("physics")
        elif service == "RunService":
            tags.add("runtime")
        elif service == "UserInputService":
            tags.add("input")
        elif service == "PathfindingService":
            tags.add("pathfinding")

    return sorted(tags)

def _build_semantic_entry(script: Dict[str, Any]) -> Dict[str, Any]:
    path = script.get("path") or ""
    class_name = script.get("className")
    source = script.get("source")
    if script.get("sourceTruncated"):
        source = None
    if isinstance(source, str) and len(source.encode("utf-8")) > SEMANTIC_MAX_SOURCE_BYTES:
        source = None

    services = _extract_services(source)
    requires = _extract_requires(source)
    keywords = _extract_keywords(source)
    tags = _semantic_tags_for_script(path, class_name, source, services)
    symbol_lines = _extract_symbol_lines(source)
    symbols = [item["name"] for item in symbol_lines]
    fingerprint = _script_fingerprint(script)
    line_count = None
    if isinstance(source, str) and source != "":
        line_count = source.count("\n") + 1

    return {
        "path": path,
        "className": class_name,
        "sha1": script.get("sha1"),
        "bytes": script.get("bytes"),
        "fingerprint": fingerprint,
        "tags": tags,
        "services": services,
        "requires": requires,
        "keywords": keywords,
        "symbols": symbols,
        "symbolLines": symbol_lines,
        "lineCount": line_count,
    }

def _summarize_semantic(entries: List[Dict[str, Any]]) -> Dict[str, Any]:
    tag_counts: Dict[str, int] = {}
    service_counts: Dict[str, int] = {}
    requires_count = 0
    symbol_count = 0
    for entry in entries:
        for tag in entry.get("tags") or []:
            tag_counts[tag] = tag_counts.get(tag, 0) + 1
        for service in entry.get("services") or []:
            service_counts[service] = service_counts.get(service, 0) + 1
        requires_count += len(entry.get("requires") or [])
        symbol_count += len(entry.get("symbols") or [])
    return {
        "scriptCount": len(entries),
        "tagCounts": tag_counts,
        "serviceCounts": service_counts,
        "requiresCount": requires_count,
        "symbolCount": symbol_count,
    }

def _build_context_semantic(context: Optional[Dict[str, Any]], context_id: str, version: int) -> Dict[str, Any]:
    entries: List[Dict[str, Any]] = []
    if context:
        scripts = context.get("scripts", []) or []
        for script in scripts:
            if not isinstance(script, dict):
                continue
            entries.append(_build_semantic_entry(script))
    summary = _summarize_semantic(entries)
    return {
        "contextId": context_id,
        "contextVersion": version,
        "updatedAt": _now(),
        "summary": summary,
        "scripts": {entry["path"]: entry for entry in entries if entry.get("path")},
    }

def _normalize_text(text: Optional[str]) -> str:
    if not text:
        return ""
    lowered = str(text).lower()
    for ch in ("\n", "\r", "\t"):
        lowered = lowered.replace(ch, " ")
    return lowered

def _classify_prompt(prompt: Optional[str], script_count: int) -> str:
    text = _normalize_text(prompt)
    if text:
        rollback_kw = ("rollback", "revert", "restore", "old version", "previous version", "start over", "restart")
        refactor_kw = ("refactor", "rework", "rewrite", "overhaul", "architecture", "breaking change")
        review_kw = ("review", "audit", "analyze", "assessment", "check", "feedback", "thoughts")
        continue_kw = ("continue", "finish", "next", "direction", "roadmap", "ideas")
        if any(k in text for k in rollback_kw):
            return "rollback"
        if any(k in text for k in refactor_kw):
            return "refactor"
        if any(k in text for k in review_kw):
            return "review"
        if any(k in text for k in continue_kw):
            return "continue"
    if script_count <= 0:
        return "greenfield"
    return "general"

def _build_script_index(
    context: Optional[Dict[str, Any]],
    semantic: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    if not context:
        return {"scripts": [], "truncated": False}
    scripts = context.get("scripts", []) or []
    semantic_map = semantic.get("scripts") if isinstance(semantic, dict) else {}
    items: List[Dict[str, Any]] = []
    for script in scripts:
        if not isinstance(script, dict):
            continue
        path = script.get("path")
        if not path:
            continue
        entry = semantic_map.get(path) if isinstance(semantic_map, dict) else {}
        symbols = entry.get("symbols") if isinstance(entry, dict) else None
        items.append({
            "path": path,
            "className": script.get("className"),
            "bytes": script.get("bytes"),
            "lineCount": entry.get("lineCount") if isinstance(entry, dict) else None,
            "tags": entry.get("tags") if isinstance(entry, dict) else None,
            "symbolCount": len(symbols) if isinstance(symbols, list) else 0,
            "fingerprint": entry.get("fingerprint") if isinstance(entry, dict) else _script_fingerprint(script),
            "hasSource": _has_full_source(script),
        })
    items.sort(key=lambda item: item.get("path") or "")
    truncated = False
    if len(items) > CODEX_SCRIPT_INDEX_MAX:
        items = items[:CODEX_SCRIPT_INDEX_MAX]
        truncated = True
    return {"scripts": items, "truncated": truncated}

def _build_dependency_index(semantic: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not semantic or not isinstance(semantic, dict):
        return {"nodes": [], "truncated": False}
    entries = semantic.get("scripts") or {}
    if not isinstance(entries, dict):
        return {"nodes": [], "truncated": False}
    nodes: List[Dict[str, Any]] = []
    edge_count = 0
    truncated = False
    for path in sorted(entries.keys()):
        entry = entries.get(path)
        if not isinstance(entry, dict):
            continue
        requires = entry.get("requires") or []
        if not isinstance(requires, list) or not requires:
            continue
        reqs = [str(item) for item in requires if item]  # preserve raw form
        total = len(reqs)
        if total > CODEX_PACK_MAX_REQUIRES:
            reqs = reqs[:CODEX_PACK_MAX_REQUIRES]
        nodes.append({
            "path": path,
            "requires": reqs,
            "requiresCount": total,
        })
        edge_count += len(reqs)
        if len(nodes) >= CODEX_PACK_MAX_ITEMS or edge_count >= CODEX_PACK_MAX_EDGES:
            truncated = True
            break
    return {"nodes": nodes, "truncated": truncated}

def _build_hotspots(script_index: Dict[str, Any]) -> Dict[str, Any]:
    scripts = script_index.get("scripts") if isinstance(script_index, dict) else None
    if not isinstance(scripts, list) or not scripts:
        return {}
    def _metric(item: Dict[str, Any], key: str) -> int:
        value = item.get(key)
        return int(value) if isinstance(value, int) else 0
    by_bytes = sorted(scripts, key=lambda item: _metric(item, "bytes"), reverse=True)[:10]
    by_symbols = sorted(scripts, key=lambda item: _metric(item, "symbolCount"), reverse=True)[:10]
    def _trim(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        trimmed = []
        for item in items:
            trimmed.append({
                "path": item.get("path"),
                "bytes": item.get("bytes"),
                "lineCount": item.get("lineCount"),
                "symbolCount": item.get("symbolCount"),
            })
        return trimmed
    return {
        "largestScripts": _trim(by_bytes),
        "mostSymbols": _trim(by_symbols),
    }

def _build_delta_summary(delta: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(delta, dict):
        return {}
    summary: Dict[str, Any] = {}
    for key in ("scriptsChanged", "scriptsAdded", "scriptsRemoved"):
        items = delta.get(key)
        if isinstance(items, list) and items:
            summary[key] = items[:CODEX_PACK_MAX_ITEMS]
            summary[f"{key}Truncated"] = len(items) > CODEX_PACK_MAX_ITEMS
    return summary

def _build_analysis_pack(
    context: Optional[Dict[str, Any]],
    semantic: Optional[Dict[str, Any]],
    delta: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    if not context:
        return {}
    script_index = _build_script_index(context, semantic)
    dependencies = _build_dependency_index(semantic)
    hotspots = _build_hotspots(script_index)
    missing_sources = []
    scripts = context.get("scripts", []) or []
    for script in scripts:
        if not isinstance(script, dict):
            continue
        if _is_missing_source(script) and script.get("path"):
            missing_sources.append(script.get("path"))
            if len(missing_sources) >= CODEX_PACK_MAX_ITEMS:
                break
    return {
        "scriptIndex": script_index,
        "dependencies": dependencies,
        "hotspots": hotspots,
        "delta": _build_delta_summary(delta),
        "missingSources": missing_sources,
    }

def _build_blueprint_pack(script_count: int) -> Dict[str, Any]:
    return {
        "scriptCount": script_count,
        "hasScripts": script_count > 0,
        "starterChecklist": [
            "Define the core loop and player goals.",
            "Map the main systems and data flow.",
            "Draft the folder/module layout.",
            "Plan UI/UX surfaces and feedback.",
            "Decide on persistence and safety constraints.",
        ],
    }

def _build_rollback_pack(pid: int, sid: str, project_key: str) -> Dict[str, Any]:
    context_id = _context_id(pid, sid, project_key)
    records = _read_tail_jsonl(CONTEXT_EVENTS_PATH, CODEX_PACK_MAX_SNAPSHOTS * 5)
    snapshots: List[Dict[str, Any]] = []
    for record in reversed(records):
        if not isinstance(record, dict):
            continue
        if record.get("event") != "export":
            continue
        if record.get("contextId") != context_id:
            continue
        snapshots.append({
            "contextVersion": record.get("contextVersion"),
            "ts": record.get("ts"),
            "treeCount": record.get("treeCount"),
            "scriptCount": record.get("scriptCount"),
            "delta": record.get("delta"),
        })
        if len(snapshots) >= CODEX_PACK_MAX_SNAPSHOTS:
            break
    return {
        "contextId": context_id,
        "snapshots": snapshots,
    }

def _build_refactor_pack() -> Dict[str, Any]:
    return {
        "guidance": [
            "Map entry points and dependencies before changing behavior.",
            "Plan a migration path (compat layer or staged rollout).",
            "Apply edits in small steps with expectedHash checks.",
        ],
    }

def _ensure_semantic_cache(pid: int, sid: str, project_key: str, context: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not SEMANTIC_ENABLED:
        return None
    key = (int(pid), str(sid), str(project_key))
    version = _context_versions.get(key, 0)
    existing = _context_semantic.get(key)
    if existing and existing.get("contextVersion") == version:
        return existing
    context_id = _context_id(pid, sid, project_key)
    if SQLITE_ENABLED:
        cached = _db_load_latest_semantic(context_id)
        if cached and cached.get("contextVersion") == version:
            _context_semantic[key] = cached
            return cached
    if not context:
        return None
    semantic = _build_context_semantic(context, context_id, version)
    _context_semantic[key] = semantic
    _record_semantic_snapshot(semantic)
    return semantic

def _compute_context_delta(prev: Optional[Dict[str, Any]], curr: Dict[str, Any]) -> Dict[str, Any]:
    prev_tree = prev.get("tree", []) if prev else []
    prev_scripts = prev.get("scripts", []) if prev else []
    curr_tree = curr.get("tree", []) or []
    curr_scripts = curr.get("scripts", []) or []

    prev_tree_paths = {t.get("path") for t in prev_tree if isinstance(t, dict)}
    curr_tree_paths = {t.get("path") for t in curr_tree if isinstance(t, dict)}

    prev_script_map = {
        s.get("path"): _script_fingerprint(s)
        for s in prev_scripts
        if isinstance(s, dict) and s.get("path")
    }
    curr_script_map = {
        s.get("path"): _script_fingerprint(s)
        for s in curr_scripts
        if isinstance(s, dict) and s.get("path")
    }

    added_tree = sorted(p for p in curr_tree_paths if p and p not in prev_tree_paths)
    removed_tree = sorted(p for p in prev_tree_paths if p and p not in curr_tree_paths)

    added_scripts = sorted(p for p in curr_script_map.keys() if p not in prev_script_map)
    removed_scripts = sorted(p for p in prev_script_map.keys() if p not in curr_script_map)
    changed_scripts = sorted(
        p for p, fp in curr_script_map.items()
        if p in prev_script_map and prev_script_map[p] != fp
    )

    return {
        "treeAddedCount": len(added_tree),
        "treeRemovedCount": len(removed_tree),
        "scriptsAddedCount": len(added_scripts),
        "scriptsRemovedCount": len(removed_scripts),
        "scriptsChangedCount": len(changed_scripts),
        "treeAdded": _truncate_list(added_tree, CONTEXT_DELTA_MAX_ITEMS),
        "treeRemoved": _truncate_list(removed_tree, CONTEXT_DELTA_MAX_ITEMS),
        "scriptsAdded": _truncate_list(added_scripts, CONTEXT_DELTA_MAX_ITEMS),
        "scriptsRemoved": _truncate_list(removed_scripts, CONTEXT_DELTA_MAX_ITEMS),
        "scriptsChanged": _truncate_list(changed_scripts, CONTEXT_DELTA_MAX_ITEMS),
    }

def _merge_context_sources(prev: Optional[Dict[str, Any]], curr: Dict[str, Any]) -> Dict[str, Any]:
    if not prev or not isinstance(curr, dict):
        return curr
    meta = curr.get("meta")
    scope = meta.get("scope") if isinstance(meta, dict) else {}
    if scope.get("mode") != "diff":
        return curr

    prev_scripts = {
        s.get("path"): s
        for s in (prev.get("scripts", []) or [])
        if isinstance(s, dict) and s.get("path")
    }
    merged = 0
    for script in curr.get("scripts", []) or []:
        if not isinstance(script, dict):
            continue
        if script.get("source") is not None:
            continue
        path = script.get("path")
        if not path:
            continue
        prev_script = prev_scripts.get(path)
        if not prev_script:
            continue
        prev_source = prev_script.get("source")
        if prev_source is None:
            continue
        curr_hash = script.get("sha1") or script.get("hash")
        prev_hash = prev_script.get("sha1") or prev_script.get("hash")
        if curr_hash and prev_hash and curr_hash != prev_hash:
            continue
        if not curr_hash and script.get("bytes") and prev_script.get("bytes") and script.get("bytes") != prev_script.get("bytes"):
            continue
        script["source"] = prev_source
        script["sourceTruncated"] = False
        if script.get("sourceOmittedReason") == "diff":
            script.pop("sourceOmittedReason", None)
        merged += 1

    if merged and isinstance(meta, dict):
        meta["mergedSources"] = merged
    return curr

def _get_context_delta(pid: int, sid: str, project_key: str) -> Optional[Dict[str, Any]]:
    return _context_deltas.get((int(pid), str(sid), str(project_key)))

def _get_context_memory(pid: int, sid: str, project_key: str) -> Optional[str]:
    key = (int(pid), str(sid), str(project_key))
    memory = _context_memory.get(key)
    if memory:
        return memory
    path = _context_memory_path(_context_id(pid, sid, project_key))
    if path.exists():
        try:
            memory = path.read_text()
            _context_memory[key] = memory
            try:
                _context_memory_mtime[key] = path.stat().st_mtime
            except Exception:
                pass
            return memory
        except Exception:
            pass
    if SQLITE_ENABLED:
        record = _db_load_context_memory(_context_id(pid, sid, project_key))
        if record:
            updated_at, mem = record
            _context_memory[key] = mem
            _context_memory_mtime[key] = updated_at
            return mem
    return None

def _lookup_script_hash(context: Optional[Dict[str, Any]], path: str) -> Optional[str]:
    if not context or not path:
        return None
    for s in context.get("scripts", []) or []:
        if not isinstance(s, dict):
            continue
        if s.get("path") == path:
            return _script_fingerprint(s)
    return None

def _should_request_resync(errors: Optional[List[Any]]) -> bool:
    if not errors:
        return False
    for err in errors:
        if not isinstance(err, str):
            continue
        lowered = err.lower()
        if "expectedhash mismatch" in lowered or "expectedhash provided but no cached hash" in lowered:
            return True
    return False

def _queue_context_export_request(
    pid: int,
    sid: str,
    project_key: str,
    reason: Optional[str] = None,
) -> bool:
    with _cv:
        if _primary_place_id is None or _primary_session_id is None:
            return False
        try:
            _require_primary_scope(int(pid), str(sid))
        except HTTPException:
            return False
        request_payload: Dict[str, Any] = {
            "requestedAt": _now(),
            "projectKey": str(project_key),
            "includeSources": True,
            "mode": "full",
        }
        if reason:
            request_payload["reason"] = str(reason)
        _context_export_requests[(int(pid), str(sid))] = request_payload
    _audit_event(
        "context_request",
        {
            "placeId": int(pid),
            "studioSessionId": str(sid),
            "projectKey": str(project_key),
            "reason": reason or "",
        },
    )
    return True

def _truncate_source_bytes(text: str, limit: int) -> Tuple[str, bool]:
    if limit <= 0:
        return "", True
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text, False
    trimmed = data[:limit].decode("utf-8", errors="ignore")
    return trimmed, True

def _build_focus_pack(context: Optional[Dict[str, Any]], delta: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not context:
        return {"scripts": [], "truncated": False}
    scripts = context.get("scripts", []) or []
    focus_paths: List[str] = []
    if isinstance(delta, dict):
        for key in ("scriptsChanged", "scriptsAdded"):
            items = delta.get(key)
            if isinstance(items, list):
                focus_paths.extend([p for p in items if isinstance(p, str)])
    if not focus_paths:
        focus_paths = [s.get("path") for s in scripts if isinstance(s, dict) and s.get("path")]

    picked = []
    truncated = False
    for path in focus_paths:
        if len(picked) >= CODEX_FOCUS_MAX_SCRIPTS:
            truncated = True
            break
        for s in scripts:
            if not isinstance(s, dict):
                continue
            if s.get("path") != path:
                continue
            source = s.get("source")
            source_truncated = bool(s.get("sourceTruncated"))
            preview = None
            trimmed = False
            line_count = None
            if isinstance(source, str):
                preview, trimmed = _truncate_source_bytes(source, CODEX_FOCUS_MAX_BYTES)
                if source != "":
                    line_count = source.count("\n") + 1
            picked.append({
                "path": s.get("path"),
                "className": s.get("className"),
                "bytes": s.get("bytes"),
                "sha1": s.get("sha1"),
                "fingerprint": _script_fingerprint(s),
                "sourcePreview": preview,
                "previewTruncated": trimmed or source_truncated,
                "sourceIsFull": (preview is not None and trimmed is False and source_truncated is False),
                "sourceTruncated": source_truncated,
                "sourceOmittedReason": s.get("sourceOmittedReason"),
                "lineCount": line_count,
            })
            break

    return {"scripts": picked, "truncated": truncated}

def _audit_event(event: str, payload: Dict[str, Any]) -> None:
    with _audit_lock:
        record = {
            "ts": _now(),
            "event": event,
            **payload,
        }
        try:
            AUDIT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with AUDIT_LOG_PATH.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=True) + "\n")
        except Exception:
            pass
        try:
            _db_write_audit(record)
        except Exception:
            pass

def _save_queue_state_unlocked() -> None:
    try:
        data = {
            "seq": _seq,
            "queue": [
                {
                    "seq": item.get("seq"),
                    "tx": item.get("tx"),
                    "scope": item.get("scope"),
                }
                for item in _queue
            ],
        }
        _write_atomic_json(QUEUE_STATE_PATH, data)
        _db_save_queue_state(data)
    except Exception:
        pass

def _load_queue_state() -> None:
    global _seq, _queue
    data = None
    if QUEUE_STATE_PATH.exists():
        try:
            data = json.loads(QUEUE_STATE_PATH.read_text())
        except Exception:
            data = None
    if data is None and SQLITE_ENABLED:
        data = _db_load_queue_state()
    if data is None:
        return
    seq = data.get("seq")
    items = data.get("queue")
    if not isinstance(items, list):
        return
    rebuilt = []
    max_seq = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        tx = item.get("tx")
        scope = item.get("scope")
        if not tx or not scope:
            continue
        seq_val = item.get("seq")
        if not isinstance(seq_val, int):
            continue
        max_seq = max(max_seq, seq_val)
        rebuilt.append({
            "seq": seq_val,
            "tx": tx,
            "claimToken": "CLAIM_" + str(uuid.uuid4()),
            "claimed": False,
            "scope": tuple(scope),
        })
    _queue = rebuilt
    if isinstance(seq, int):
        _seq = max(_seq, seq, max_seq)
    else:
        _seq = max(_seq, max_seq)

_load_queue_state()

def _append_context_event(event: str, payload: Dict[str, Any]) -> None:
    record = {"ts": _now(), "event": event, **payload}
    try:
        CONTEXT_EVENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with CONTEXT_EVENTS_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=True) + "\n")
    except Exception:
        pass
    try:
        _db_write_context_event(record)
    except Exception:
        pass

def _read_tail_jsonl(path: Path, limit: int) -> List[Dict[str, Any]]:
    if limit <= 0:
        return []
    try:
        lines = path.read_text().splitlines()
    except Exception:
        return []
    tail = lines[-limit:]
    out = []
    for line in tail:
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out

def _record_action_stats(actions: List[Dict[str, Any]]) -> None:
    with _codex_lock:
        _action_stats["total"] = int(_action_stats.get("total") or 0) + len(actions)
        by_type = _action_stats.setdefault("byType", {})
        for action in actions:
            if not isinstance(action, dict):
                continue
            t = action.get("type")
            if not isinstance(t, str):
                continue
            by_type[t] = int(by_type.get(t) or 0) + 1

def _write_codex_job(job: Dict[str, Any]) -> Dict[str, Any]:
    job_id = job.get("jobId") or str(uuid.uuid4())
    job["jobId"] = job_id
    job_path = CODEX_JOBS_DIR / f"job_{job_id}.json"
    _write_atomic_json(job_path, job)
    _codex_jobs_index[job_id] = job
    _record_codex_job(job)
    return {
        "ok": True,
        "jobId": job_id,
        "contextId": job.get("contextId"),
        "contextVersion": job.get("contextVersion"),
    }

def _should_auto_repair(tx_id: str) -> bool:
    if not CODEX_AUTO_REPAIR:
        return False
    if not tx_id:
        return False
    attempts = _codex_repair_attempts.get(tx_id, 0)
    if attempts >= CODEX_REPAIR_MAX_ATTEMPTS:
        return False
    last_at = _codex_repair_last.get(tx_id, 0.0)
    if (_now() - last_at) < CODEX_REPAIR_COOLDOWN_SEC:
        return False
    return True

def _normalize_action(action: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(action, dict):
        return action
    out = dict(action)
    raw_type = out.get("type") or out.get("action") or out.get("actionType") or ""
    raw_type = str(raw_type).strip()
    lower = raw_type.lower()

    if lower in {"createfolder", "create_folder"}:
        out["type"] = "createInstance"
        out.setdefault("className", out.get("class") or "Folder")
    elif lower in {"createscript", "create_script"}:
        out["type"] = "createInstance"
        out.setdefault("className", out.get("class") or "Script")
    elif lower in {"createlocalscript", "create_localscript"}:
        out["type"] = "createInstance"
        out.setdefault("className", out.get("class") or "LocalScript")
    elif lower in {"createmodulescript", "create_modulescript"}:
        out["type"] = "createInstance"
        out.setdefault("className", out.get("class") or "ModuleScript")
    elif lower in {"setscript", "setsource", "setscriptsource"}:
        out["type"] = "editScript"
        out.setdefault("mode", "replace")
        if "source" not in out:
            out["source"] = (
                out.get("scriptSource")
                or out.get("content")
                or out.get("text")
                or out.get("value")
            )
    elif lower in {"setparent", "moveinstance"}:
        out["type"] = "move"
        out.setdefault("newParentPath", out.get("parentPath") or out.get("parent"))
    elif lower in {"renameinstance", "setname"}:
        out["type"] = "rename"
        out.setdefault("newName", out.get("name"))
    elif lower in {"delete", "remove", "destroy", "destroyinstance"}:
        out["type"] = "deleteInstance"
    elif lower in {"clone", "cloneinstance"}:
        out["type"] = "cloneInstance"
    elif lower in {"clearchildren", "removechildren"}:
        out["type"] = "clearChildren"
    elif lower in {"settags", "addtags", "removetags"}:
        out["type"] = "setTags"
        if lower == "addtags":
            out.setdefault("mode", "add")
        if lower == "removetags":
            out.setdefault("mode", "remove")
    elif lower in {"insertasset", "loadasset", "insert"}:
        out["type"] = "insertAsset"
    elif lower in {"tween", "tweeninstance"}:
        out["type"] = "tween"
    elif lower in {"emitparticles", "emit"}:
        out["type"] = "emitParticles"
    elif lower in {"playsound", "playaudio"}:
        out["type"] = "playSound"
    elif lower in {"createanimation", "animationcreate"}:
        out["type"] = "animationCreate"
    elif lower in {"addkeyframe", "animationaddkeyframe"}:
        out["type"] = "animationAddKeyframe"
    elif lower in {"previewanimation", "animationpreview"}:
        out["type"] = "animationPreview"
    elif lower in {"stopanimation", "animationstop"}:
        out["type"] = "animationStop"
    elif lower in {"setproperties"}:
        out["type"] = "setProperties"
    elif lower in {"setproperty"}:
        out["type"] = "setProperty"
    elif lower in {"setattribute"}:
        out["type"] = "setAttribute"
    elif lower in {"setattributes"}:
        out["type"] = "setAttributes"
    elif lower in {"edit"}:
        out["type"] = "editScript"
    elif raw_type:
        out["type"] = raw_type

    action_type = out.get("type")

    if action_type in {
        "setProperty",
        "setProperties",
        "deleteInstance",
        "rename",
        "move",
        "setAttribute",
        "setAttributes",
        "editScript",
        "cloneInstance",
        "clearChildren",
        "setTags",
        "tween",
        "emitParticles",
    }:
        out.setdefault("path", out.get("targetPath") or out.get("target"))

    if action_type == "createInstance":
        out.setdefault("parentPath", out.get("parent") or out.get("parent_path"))
        out.setdefault("className", out.get("class") or out.get("class_name"))
        if "source" not in out:
            out["source"] = out.get("content") or out.get("text") or out.get("value")

    if action_type == "insertAsset":
        out.setdefault("parentPath", out.get("parent") or out.get("parent_path"))
        out.setdefault("assetId", out.get("id") or out.get("asset") or out.get("assetID"))

    if action_type == "setProperty":
        out.setdefault("property", out.get("key"))

    if action_type == "setProperties":
        out.setdefault("properties", out.get("props") or out.get("values"))

    if action_type == "setAttribute":
        out.setdefault("attribute", out.get("key"))

    if action_type == "setAttributes":
        out.setdefault("attributes", out.get("attrs") or out.get("values"))

    if action_type == "move":
        out.setdefault("newParentPath", out.get("parentPath") or out.get("parent"))

    if action_type == "rename":
        out.setdefault("newName", out.get("name"))

    if action_type == "cloneInstance":
        out.setdefault("sourcePath", out.get("source") or out.get("path"))
        if "path" not in out and out.get("sourcePath"):
            out["path"] = out["sourcePath"]
        out.setdefault("parentPath", out.get("parent") or out.get("parentPath"))

    if action_type == "editScript":
        out.setdefault("mode", "replace")
        if "source" not in out and "chunks" not in out:
            out["source"] = out.get("content") or out.get("text") or out.get("value")

    if action_type == "playSound":
        out.setdefault("path", out.get("targetPath") or out.get("target"))
        out.setdefault("soundId", out.get("id") or out.get("sound") or out.get("assetId"))

    if action_type == "animationCreate":
        out.setdefault("parentPath", out.get("parent") or out.get("parent_path"))
        out.setdefault("name", out.get("animationName") or out.get("sequenceName"))

    if action_type == "animationAddKeyframe":
        out.setdefault("path", out.get("sequencePath") or out.get("targetPath") or out.get("target"))

    if action_type == "animationPreview":
        out.setdefault("rigPath", out.get("rig") or out.get("targetPath"))
        out.setdefault("sequencePath", out.get("path") or out.get("sequence"))

    return out

def _normalize_actions(actions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [_normalize_action(action) for action in actions]

def _validate_codex_actions(actions: List[Dict[str, Any]], context: Optional[Dict[str, Any]]) -> List[str]:
    errors: List[str] = []
    if len(actions) > CODEX_MAX_ACTIONS:
        errors.append(f"too many actions: {len(actions)} > {CODEX_MAX_ACTIONS}")

    allowed_roots = _parse_csv_set(os.environ.get("PERSPONIFY_CODEX_ALLOWED_ROOTS"))
    if not allowed_roots:
        allowed_roots = set()

    def path_under(path: Optional[str], roots: set) -> bool:
        if not roots:
            return True
        if not path:
            return False
        for root in roots:
            root = root.strip()
            if not root:
                continue
            if path.startswith(root):
                return True
        return False

    safe_edit_bytes = int(os.environ.get("PERSPONIFY_CODEX_SAFE_EDIT_BYTES", "8000"))

    for idx, action in enumerate(actions, start=1):
        if not isinstance(action, dict):
            errors.append(f"action {idx}: not an object")
            continue
        raw_type = action.get("type")
        if not isinstance(raw_type, str) or not raw_type:
            errors.append(f"action {idx}: missing type")
            continue
        action_type = raw_type
        if action_type not in SUPPORTED_ACTIONS:
            errors.append(f"action {idx}: unsupported type {action_type}")
        if action_type in CODEX_DENY_ACTIONS:
            errors.append(f"action {idx}: blocked type {action_type}")

        path = action.get("path")
        if action_type in {
            "setProperty",
            "setProperties",
            "deleteInstance",
            "rename",
            "move",
            "setAttribute",
            "setAttributes",
            "editScript",
            "cloneInstance",
            "clearChildren",
            "setTags",
            "tween",
            "emitParticles",
            "animationAddKeyframe",
        }:
            if not isinstance(path, str) or not path:
                errors.append(f"action {idx}: missing path")
            elif not path.startswith("game/"):
                errors.append(f"action {idx}: invalid path {path}")
            if CODEX_PROTECTED_ROOTS:
                for root in CODEX_PROTECTED_ROOTS:
                    if root and path.startswith(root):
                        errors.append(f"action {idx}: protected path {path}")
            if not path_under(path, allowed_roots):
                errors.append(f"action {idx}: path outside allowed roots")

        if action_type == "createInstance":
            parent_path = action.get("parentPath")
            class_name = action.get("className")
            if not isinstance(parent_path, str) or not parent_path:
                errors.append(f"action {idx}: missing parentPath")
            if not isinstance(class_name, str) or not class_name:
                errors.append(f"action {idx}: missing className")
            if CODEX_PROTECTED_ROOTS:
                for root in CODEX_PROTECTED_ROOTS:
                    if root and parent_path.startswith(root):
                        errors.append(f"action {idx}: protected parentPath {parent_path}")
            if not path_under(parent_path, allowed_roots):
                errors.append(f"action {idx}: parentPath outside allowed roots")
            source = action.get("source")
            if isinstance(source, str) and len(source.encode("utf-8")) > CODEX_MAX_SOURCE_BYTES:
                errors.append(f"action {idx}: source too large")

        if action_type == "insertAsset":
            parent_path = action.get("parentPath")
            asset_id = action.get("assetId")
            if not isinstance(parent_path, str) or not parent_path:
                errors.append(f"action {idx}: missing parentPath")
            if not asset_id:
                errors.append(f"action {idx}: missing assetId")
            if isinstance(parent_path, str) and not parent_path.startswith("game/"):
                errors.append(f"action {idx}: invalid parentPath {parent_path}")
            if isinstance(parent_path, str) and not path_under(parent_path, allowed_roots):
                errors.append(f"action {idx}: parentPath outside allowed roots")

        if action_type == "cloneInstance":
            parent_path = action.get("parentPath")
            if parent_path is not None and not isinstance(parent_path, str):
                errors.append(f"action {idx}: invalid parentPath")
            if parent_path and not path_under(parent_path, allowed_roots):
                errors.append(f"action {idx}: parentPath outside allowed roots")

        if action_type == "clearChildren":
            if not isinstance(path, str) or not path:
                errors.append(f"action {idx}: missing path")

        if action_type == "setTags":
            tags = action.get("tags")
            add = action.get("add")
            remove = action.get("remove")
            if tags is None and add is None and remove is None:
                errors.append(f"action {idx}: missing tags")

        if action_type == "tween":
            props = action.get("properties")
            if not isinstance(props, dict):
                errors.append(f"action {idx}: missing properties")

        if action_type == "emitParticles":
            count = action.get("count") or action.get("emit")
            if count is not None:
                try:
                    int(count)
                except (TypeError, ValueError):
                    errors.append(f"action {idx}: invalid emit count")

        if action_type == "playSound":
            sound_id = action.get("soundId") or action.get("assetId")
            if not action.get("path"):
                parent_path = action.get("parentPath")
                if not sound_id:
                    errors.append(f"action {idx}: missing soundId")
                if not isinstance(parent_path, str) or not parent_path:
                    errors.append(f"action {idx}: missing parentPath")
                elif not parent_path.startswith("game/"):
                    errors.append(f"action {idx}: invalid parentPath {parent_path}")
                elif not path_under(parent_path, allowed_roots):
                    errors.append(f"action {idx}: parentPath outside allowed roots")

        if action_type == "animationCreate":
            parent_path = action.get("parentPath")
            if not isinstance(parent_path, str) or not parent_path:
                errors.append(f"action {idx}: missing parentPath")
            elif not parent_path.startswith("game/"):
                errors.append(f"action {idx}: invalid parentPath {parent_path}")
            elif not path_under(parent_path, allowed_roots):
                errors.append(f"action {idx}: parentPath outside allowed roots")

        if action_type == "animationPreview":
            rig_path = action.get("rigPath")
            sequence_path = action.get("sequencePath")
            sequence_data = action.get("sequence")
            if not isinstance(rig_path, str) or not rig_path:
                errors.append(f"action {idx}: missing rigPath")
            elif not rig_path.startswith("game/"):
                errors.append(f"action {idx}: invalid rigPath {rig_path}")
            elif not path_under(rig_path, allowed_roots):
                errors.append(f"action {idx}: rigPath outside allowed roots")
            if sequence_path:
                if not isinstance(sequence_path, str) or not sequence_path.startswith("game/"):
                    errors.append(f"action {idx}: invalid sequencePath {sequence_path}")
                elif not path_under(sequence_path, allowed_roots):
                    errors.append(f"action {idx}: sequencePath outside allowed roots")
            elif not isinstance(sequence_data, dict):
                errors.append(f"action {idx}: missing sequencePath/sequence")

        if action_type == "animationStop":
            rig_path = action.get("rigPath")
            if not isinstance(rig_path, str) or not rig_path:
                errors.append(f"action {idx}: missing rigPath")
            elif not rig_path.startswith("game/"):
                errors.append(f"action {idx}: invalid rigPath {rig_path}")
            elif not path_under(rig_path, allowed_roots):
                errors.append(f"action {idx}: rigPath outside allowed roots")

        if action_type == "setProperty":
            prop = action.get("property")
            if not isinstance(prop, str) or not prop:
                errors.append(f"action {idx}: missing property")

        if action_type == "setProperties":
            props = action.get("properties")
            if not isinstance(props, dict):
                errors.append(f"action {idx}: missing properties")

        if action_type == "setAttribute":
            attr = action.get("attribute")
            if not isinstance(attr, str) or not attr:
                errors.append(f"action {idx}: missing attribute")

        if action_type == "setAttributes":
            attrs = action.get("attributes")
            if not isinstance(attrs, dict):
                errors.append(f"action {idx}: missing attributes")

        if action_type == "editScript":
            mode = action.get("mode") or "replace"
            if mode not in CODEX_ALLOWED_EDIT_MODES:
                errors.append(f"action {idx}: unsupported editScript mode {mode}")
            source = action.get("source")
            chunks = action.get("chunks")
            size = 0
            if isinstance(source, str):
                size += len(source.encode("utf-8"))
            if chunks is not None:
                if not isinstance(chunks, list):
                    errors.append(f"action {idx}: editScript chunks not a list")
                else:
                    for c in chunks:
                        if not isinstance(c, str):
                            errors.append(f"action {idx}: editScript chunk not a string")
                            break
                        size += len(c.encode("utf-8"))
            if source is None and not chunks:
                errors.append(f"action {idx}: editScript missing source/chunks")
            if size > CODEX_MAX_SOURCE_BYTES:
                errors.append(f"action {idx}: editScript payload too large")

            expected = action.get("expectedHash") or action.get("expectedSha1")
            if expected:
                actual = _lookup_script_hash(context, path)
                if not actual:
                    errors.append(f"action {idx}: expectedHash provided but no cached hash")
                elif str(actual) != str(expected):
                    errors.append(f"action {idx}: expectedHash mismatch")

            if CODEX_POLICY_PROFILE == "safe" and size > safe_edit_bytes:
                errors.append(f"action {idx}: editScript exceeds safe size")

        if CODEX_POLICY_PROFILE == "safe" and action_type in {"createInstance", "rename", "move"}:
            errors.append(f"action {idx}: blocked by safe policy")
        if CODEX_POLICY_PROFILE != "power" and action_type == "deleteInstance":
            errors.append(f"action {idx}: blocked by policy")

    return errors

def _preview_errors(errors: Optional[List[Any]], limit: int = 5) -> List[Any]:
    if not errors or not isinstance(errors, list):
        return []
    return _truncate_list(errors, limit)

def _codex_pending_count() -> int:
    try:
        jobs = {p.stem for p in CODEX_JOBS_DIR.glob("job_*.json")}
        acks = {p.stem for p in CODEX_ACKS_DIR.glob("job_*.json")}
        return len(jobs - acks)
    except Exception:
        return 0

def _get_cached_context(pid: int, sid: str, project_key: str) -> Optional[Dict[str, Any]]:
    key = (int(pid), str(sid), str(project_key))
    data = _context_latest.get(key)
    if data:
        return data
    path = _context_file_path(_context_id(pid, sid, project_key))
    if path.exists():
        try:
            data = json.loads(path.read_text())
        except Exception:
            data = None
    if data is None and SQLITE_ENABLED:
        data = _db_load_latest_context(_context_id(pid, sid, project_key))
    if not data:
        return None
    _context_latest[key] = data
    if isinstance(data, dict):
        v = data.get("contextVersion")
        if isinstance(v, int):
            _context_versions[key] = v
        _ensure_semantic_cache(pid, sid, project_key, data)
    return data

def _record_codex_error(job_id: str, message: str, detail: Optional[Dict[str, Any]] = None) -> None:
    with _codex_lock:
        _codex_state["lastError"] = {
            "jobId": job_id,
            "message": message,
            "detail": detail or {},
            "time": _now(),
        }
    _audit_event("codex_error", {"jobId": job_id, "message": message, "detail": detail or {}})

def _record_codex_job(job: Dict[str, Any]) -> None:
    with _codex_lock:
        _codex_state["lastJob"] = {
            "jobId": job.get("jobId"),
            "intent": job.get("intent"),
            "contextId": job.get("contextId"),
            "contextVersion": job.get("contextVersion"),
            "time": job.get("createdAt"),
        }
    _audit_event("codex_job", {"jobId": job.get("jobId"), "contextId": job.get("contextId")})

def _record_codex_response(job_id: str, summary: str) -> None:
    with _codex_lock:
        _codex_state["lastResponse"] = {
            "jobId": job_id,
            "summary": summary,
            "time": _now(),
        }
    _audit_event("codex_response", {"jobId": job_id, "summary": summary})

def _enqueue_tx_unlocked(scope: Tuple[int, str], tx: Dict[str, Any]) -> Dict[str, Any]:
    s = _next_seq_unlocked()
    claim = "CLAIM_" + str(uuid.uuid4())
    item = {
        "seq": s,
        "tx": tx,
        "claimToken": claim,
        "claimed": False,
        "scope": scope,
    }
    _queue.append(item)
    _save_queue_state_unlocked()
    _cv.notify_all()
    return {"seq": s, "claimToken": claim}

def _load_job_from_disk(job_id: str) -> Optional[Dict[str, Any]]:
    path = CODEX_JOBS_DIR / f"job_{job_id}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None

def _write_ack(job_id: str, payload: Dict[str, Any]) -> None:
    ack_path = CODEX_ACKS_DIR / f"job_{job_id}.json"
    _write_atomic_json(ack_path, payload)

def _write_error(job_id: str, payload: Dict[str, Any]) -> None:
    err_path = CODEX_ERRORS_DIR / f"job_{job_id}.json"
    _write_atomic_json(err_path, payload)

def _process_codex_response(job_id: str, data: Dict[str, Any], resp_path: Optional[Path] = None) -> None:
    job = _codex_jobs_index.get(job_id) or _load_job_from_disk(job_id)
    if not job:
        msg = "Job not found for response"
        _record_codex_error(job_id, msg)
        _write_error(job_id, {"ok": False, "error": msg})
        _write_ack(job_id, {"ok": False, "error": msg})
        return

    if _primary_place_id is None or _primary_session_id is None:
        created_at = job.get("createdAt")
        if created_at is None:
            msg = "NoPrimary"
            _record_codex_error(job_id, msg)
            _write_error(job_id, {"ok": False, "error": msg})
            _write_ack(job_id, {"ok": False, "error": msg})
            return
        if (_now() - float(created_at)) > CODEX_JOB_TTL_SEC:
            msg = "NoPrimary"
            _record_codex_error(job_id, msg)
            _write_error(job_id, {"ok": False, "error": msg})
            _write_ack(job_id, {"ok": False, "error": msg})
        return

    scope = job.get("scope") or {}
    pid = scope.get("placeId")
    sid = scope.get("studioSessionId")
    if pid is None or sid is None:
        msg = "Response missing scope"
        _record_codex_error(job_id, msg)
        _write_error(job_id, {"ok": False, "error": msg})
        _write_ack(job_id, {"ok": False, "error": msg})
        return

    actions = data.get("actions")
    if actions is None and isinstance(data.get("tx"), dict):
        actions = data["tx"].get("actions")
    if actions is None and isinstance(data.get("plan"), dict):
        actions = data["plan"].get("actions")
    if actions is None and isinstance(data.get("plan"), list):
        if all(isinstance(item, dict) for item in data["plan"]):
            actions = data["plan"]
    if actions is None and isinstance(data.get("dsl"), dict):
        actions = data["dsl"].get("actions")
    if not isinstance(actions, list):
        msg = "Invalid actions list"
        _record_codex_error(job_id, msg, {"payload": data})
        _write_error(job_id, {"ok": False, "error": msg})
        _write_ack(job_id, {"ok": False, "error": msg})
        return
    actions = _normalize_actions(actions)
    if data.get("ok") is False or len(actions) == 0:
        msg = "No actions to apply"
        err_detail = {"errors": data.get("errors") or data.get("notes")}
        _record_codex_error(job_id, msg, err_detail)
        _write_error(job_id, {"ok": False, "error": msg, "detail": err_detail})
        _write_ack(job_id, {"ok": False, "error": msg})
        if resp_path:
            try:
                resp_path.unlink()
            except Exception:
                pass
        return

    risk = data.get("riskScore")
    if risk is None:
        risk = data.get("risk")
    if risk is None:
        risk = data.get("risk_score")
    try:
        risk_val = float(risk) if risk is not None else None
    except Exception:
        risk_val = None
    if risk_val is not None and risk_val > CODEX_MAX_RISK and CODEX_POLICY_PROFILE != "power":
        msg = "Codex risk score too high"
        detail = {"risk": risk_val, "maxRisk": CODEX_MAX_RISK}
        _record_codex_error(job_id, msg, detail)
        _write_error(job_id, {"ok": False, "error": msg, "detail": detail})
        _write_ack(job_id, {"ok": False, "error": msg})
        if resp_path:
            try:
                resp_path.unlink()
            except Exception:
                pass
        return

    project_key = scope.get("projectKey") or "default"
    context = _get_cached_context(int(pid), str(sid), str(project_key))
    validation_errors = _validate_codex_actions(actions, context)
    if validation_errors:
        if _should_request_resync(validation_errors):
            _queue_context_export_request(int(pid), str(sid), str(project_key), reason="expectedHash mismatch")
        msg = "Codex action validation failed"
        detail = {"errors": validation_errors}
        _record_codex_error(job_id, msg, detail)
        _write_error(job_id, {"ok": False, "error": msg, "detail": detail})
        _write_ack(job_id, {"ok": False, "error": msg})
        if resp_path:
            try:
                resp_path.unlink()
            except Exception:
                pass
        return

    tx = {
        "protocolVersion": PROTOCOL_VERSION,
        "transactionId": data.get("transactionId") or f"TX_CODEX_{job_id}",
        "actions": actions,
    }

    with _cv:
        if _primary_place_id is None or _primary_session_id is None:
            msg = "NoPrimary"
            _record_codex_error(job_id, msg)
            _write_error(job_id, {"ok": False, "error": msg})
            _write_ack(job_id, {"ok": False, "error": msg})
            return

        scope_key = _scope_key(int(pid), str(sid))
        try:
            _require_primary_scope(int(pid), str(sid))
        except HTTPException:
            msg = "ScopeMismatch"
            _record_codex_error(job_id, msg, {"scope": scope})
            _write_error(job_id, {"ok": False, "error": msg})
            _write_ack(job_id, {"ok": False, "error": msg})
            return

        if len(_queue) >= MAX_QUEUE_SIZE:
            msg = "QueueFull"
            _record_codex_error(job_id, msg)
            _write_error(job_id, {"ok": False, "error": msg})
            _write_ack(job_id, {"ok": False, "error": msg})
            return

        meta = _enqueue_tx_unlocked(scope_key, tx)
        _codex_tx_job_map[tx["transactionId"]] = job_id

    _record_action_stats(actions)
    _audit_event(
        "codex_tx_enqueued",
        {
            "jobId": job_id,
            "txId": tx.get("transactionId"),
            "seq": meta.get("seq"),
            "scope": {"placeId": int(pid), "studioSessionId": str(sid)},
        },
    )

    _record_codex_response(job_id, str(data.get("summary") or ""))
    _write_ack(job_id, {"ok": True, "seq": meta["seq"], "txId": tx["transactionId"]})
    if resp_path:
        try:
            resp_path.unlink()
        except Exception:
            pass

def _sweep_codex_jobs() -> None:
    now = _now()
    for job_path in CODEX_JOBS_DIR.glob("job_*.json"):
        job_id = job_path.stem.replace("job_", "", 1)
        ack_path = CODEX_ACKS_DIR / f"job_{job_id}.json"
        resp_path = CODEX_RESP_DIR / f"job_{job_id}.json"
        if ack_path.exists() or resp_path.exists():
            continue
        job = _load_job_from_disk(job_id)
        created_at = None
        if isinstance(job, dict):
            created_at = job.get("createdAt")
        if created_at is None:
            try:
                created_at = job_path.stat().st_mtime
            except Exception:
                created_at = None
        if created_at is None:
            continue
        if (now - float(created_at)) <= CODEX_JOB_TTL_SEC:
            continue
        msg = "Codex job expired"
        _record_codex_error(job_id, msg, {"ageSec": now - float(created_at)})
        _write_error(job_id, {"ok": False, "error": msg})
        _write_ack(job_id, {"ok": False, "error": msg})
        try:
            job_path.unlink()
        except Exception:
            pass

def _codex_watch_loop() -> None:
    while True:
        try:
            _sweep_codex_jobs()
            for resp_path in CODEX_RESP_DIR.glob("job_*.json"):
                job_id = resp_path.stem.replace("job_", "", 1)
                ack_path = CODEX_ACKS_DIR / f"job_{job_id}.json"
                if ack_path.exists():
                    continue
                try:
                    data = json.loads(resp_path.read_text())
                except Exception as exc:
                    _record_codex_error(job_id, f"Response parse failed: {exc}")
                    _write_error(job_id, {"ok": False, "error": "Response parse failed"})
                    _write_ack(job_id, {"ok": False, "error": "Response parse failed"})
                    continue
                if data.get("jobId") and str(data.get("jobId")) != job_id:
                    _record_codex_error(job_id, "jobId mismatch")
                    _write_error(job_id, {"ok": False, "error": "jobId mismatch"})
                    _write_ack(job_id, {"ok": False, "error": "jobId mismatch"})
                    continue
                _process_codex_response(job_id, data, resp_path)
        except Exception:
            # Swallow errors so the watcher stays alive.
            pass
        time.sleep(1.0)

_codex_thread = threading.Thread(target=_codex_watch_loop, daemon=True)
_codex_thread.start()

def _load_context_from_disk(context_id: str) -> Optional[Dict[str, Any]]:
    path = _context_file_path(context_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None

def _reconcile_contexts() -> None:
    keys = list(_context_latest.keys())
    for key in keys:
        pid, sid, project_key = key
        context_id = _context_id(pid, sid, project_key)
        current_version = _context_versions.get(key, 0)

        disk_context = _load_context_from_disk(context_id)
        db_context = None
        if disk_context is None and SQLITE_ENABLED:
            db_context = _db_load_latest_context(context_id)

        candidate = disk_context or db_context
        if isinstance(candidate, dict):
            cand_version = candidate.get("contextVersion")
            if isinstance(cand_version, int) and cand_version > current_version:
                prev = _context_latest.get(key)
                _context_latest[key] = candidate
                _context_versions[key] = cand_version
                _context_deltas[key] = _compute_context_delta(prev, candidate)
                _ensure_semantic_cache(pid, sid, project_key, candidate)
                _append_context_event(
                    "reconcile",
                    {
                        "contextId": context_id,
                        "contextVersion": cand_version,
                        "projectKey": project_key,
                    },
                )

        mem_path = _context_memory_path(context_id)
        if mem_path.exists():
            try:
                mtime = mem_path.stat().st_mtime
            except Exception:
                mtime = 0.0
            if mtime and mtime > _context_memory_mtime.get(key, 0.0):
                try:
                    memory = mem_path.read_text()
                except Exception:
                    memory = None
                if memory is not None:
                    _context_memory[key] = memory
                    _context_memory_mtime[key] = mtime
                    _append_context_event(
                        "memory_reload",
                        {
                            "contextId": context_id,
                            "projectKey": project_key,
                            "bytes": len(memory.encode("utf-8")),
                        },
                    )
        elif SQLITE_ENABLED:
            record = _db_load_context_memory(context_id)
            if record:
                updated_at, memory = record
                if updated_at > _context_memory_mtime.get(key, 0.0):
                    _context_memory[key] = memory
                    _context_memory_mtime[key] = updated_at

def _reconcile_loop() -> None:
    while True:
        try:
            _reconcile_contexts()
        except Exception:
            pass
        time.sleep(RECONCILE_INTERVAL_SEC)

_reconcile_thread = threading.Thread(target=_reconcile_loop, daemon=True)
_reconcile_thread.start()

# ----------------------------
# Routes
# ----------------------------

@app.get("/health")
def health():
    return {
        "ok": True,
        "serverName": "PersponifyCodex",
        "serverTime": _now(),
        "rulesVersion": APP_VERSION,
        "protocolVersion": PROTOCOL_VERSION,
        "endpoints": {
            "health": "/health",
            "discover": "/discover",
            "status": "/status",
            "register": "/register",
            "release": "/release",
            "sync": "/sync",
            "wait": "/wait",
            "receipt": "/receipt",
            "heartbeat": "/heartbeat",
            "enqueue": "/enqueue",
            "enqueue_mock": "/enqueue_mock",
            "enqueue_template": "/enqueue_template",
            "debug_state": "/debug/state",
            "debug_last_wait": "/debug/last_wait",
            "debug_last_receipt": "/debug/last_receipt",
            "debug_reset": "/debug/reset",
            "debug_fault": "/debug/fault",
            "scope_current": "/scope/current",
            "context_export": "/context/export",
            "context_latest": "/context/latest",
            "context_summary": "/context/summary",
            "context_semantic": "/context/semantic",
            "context_reset": "/context/reset",
            "context_script": "/context/script",
            "context_missing": "/context/missing",
            "context_memory": "/context/memory",
            "context_events": "/context/events",
            "chunk_source": "/util/chunk_source",
            "telemetry_report": "/telemetry/report",
            "telemetry_request": "/telemetry/request",
            "telemetry_latest": "/telemetry/latest",
            "telemetry_summary": "/telemetry/summary",
            "telemetry_history": "/telemetry/history",
            "telemetry_ui_qa_report": "/telemetry/ui_qa_report",
            "telemetry_reset": "/telemetry/reset",
            "catalog_request": "/catalog/request",
            "catalog_report": "/catalog/report",
            "catalog_latest": "/catalog/latest",
            "assets_search": "/assets/search",
            "assets_info": "/assets/info",
            "codex_job": "/codex/job",
            "codex_status": "/codex/status",
            "codex_response": "/codex/response",
            "codex_compile": "/codex/compile",
            "diagnostics": "/diagnostics",
            "audit_ledger": "/audit/ledger",
            "ai_complete": "/ai/complete",
            "ai_stream": "/ai/stream",
            "ai_moe_complete": "/ai/moe/complete",
            "ai_moe_stream": "/ai/moe/stream",
            "ai_models": "/ai/models",
            "ai_health": "/ai/health",
            "ai_reload": "/ai/reload",
            "ai_secrets": "/ai/secrets",
            "ai_adapter_models": "/ai/adapter_models",
        },
        "meta": {
            "supportsWaitTimeout": True,
            "defaultWaitTimeoutSec": DEFAULT_WAIT_TIMEOUT_SEC,
            "heartbeatTtlSec": HEARTBEAT_TTL_SEC,
            "claimTtlSec": CLAIM_TTL_SEC,
            "supportsTakeover": True,
            "companionConfig": _companion_config_path(),
            "codexQueueDir": str(CODEX_QUEUE_DIR),
            "sqlite": {
                "enabled": SQLITE_ENABLED,
                "path": str(SQLITE_PATH),
            },
        },
    }

@app.get("/discover")
def discover():
    # Backward/forward compatibility: some clients may expect /discover.
    return health()

@app.get("/status")
def status():
    with _cv:
        context_request: Any = False
        telemetry_request: Any = False
        catalog_request: Any = False
        if _primary_place_id is not None and _primary_session_id is not None:
            key = (int(_primary_place_id), str(_primary_session_id))
            req = _context_export_requests.get(key)
            context_request = req if isinstance(req, dict) else bool(req)
            t_req = _telemetry_export_requests.get(key)
            telemetry_request = t_req if isinstance(t_req, dict) else bool(t_req)
            c_req = _catalog_search_requests.get(key)
            catalog_request = c_req if isinstance(c_req, dict) else bool(c_req)
        return {
            "ok": True,
            "serverTime": _now(),
            "primary": {
                "leaseToken": _primary_lease_token,
                "fence": _fence,
                "placeId": _primary_place_id,
                "studioSessionId": _primary_session_id,
                "clientId": _primary_client_id,
                "alive": _is_primary_alive(),
                "lastHeartbeatAgeSec": round(_now() - _last_heartbeat, 3) if _primary_lease_token else None,
            },
            "queuePending": len(_queue),
            "claims": len(_claims),
            "lastReceipt": _last_receipt.get(_scope_key(_primary_place_id, _primary_session_id))
            if _primary_place_id is not None and _primary_session_id is not None
            else None,
            "codex": {
                "pending": _codex_pending_count(),
                "lastJob": _codex_state.get("lastJob"),
                "lastResponse": _codex_state.get("lastResponse"),
                "lastError": _codex_state.get("lastError"),
                "actionStats": _action_stats,
            },
            "contextRequest": context_request,
            "telemetryRequest": telemetry_request,
            "catalogRequest": catalog_request,
            "queueLimit": MAX_QUEUE_SIZE,
        }

@app.get("/diagnostics")
def diagnostics():
    with _cv:
        scope_key = None
        if _primary_place_id is not None and _primary_session_id is not None:
            scope_key = _scope_key(_primary_place_id, _primary_session_id)

        context_scopes = len(_context_latest)
        last_receipt = _last_receipt.get(scope_key) if scope_key else None
        codex_files = {
            "jobs": len(list(CODEX_JOBS_DIR.glob("job_*.json"))),
            "responses": len(list(CODEX_RESP_DIR.glob("job_*.json"))),
            "acks": len(list(CODEX_ACKS_DIR.glob("job_*.json"))),
            "errors": len(list(CODEX_ERRORS_DIR.glob("job_*.json"))),
        }

        return {
            "ok": True,
            "version": APP_VERSION,
            "serverTime": _now(),
            "primary": {
                "placeId": _primary_place_id,
                "studioSessionId": _primary_session_id,
                "clientId": _primary_client_id,
                "alive": _is_primary_alive(),
                "lastHeartbeatAgeSec": round(_now() - _last_heartbeat, 3) if _primary_lease_token else None,
            },
            "queue": {
                "pending": len(_queue),
                "claims": len(_claims),
                "lastReceipt": last_receipt,
                "limit": MAX_QUEUE_SIZE,
            },
            "codex": {
                "pending": _codex_pending_count(),
                "lastJob": _codex_state.get("lastJob"),
                "lastResponse": _codex_state.get("lastResponse"),
                "lastError": _codex_state.get("lastError"),
                "files": codex_files,
                "jobTtlSec": CODEX_JOB_TTL_SEC,
                "actionStats": _action_stats,
            },
            "context": {
                "scopes": context_scopes,
            },
            "auditLog": str(AUDIT_LOG_PATH),
            "contextEventsLog": str(CONTEXT_EVENTS_PATH),
            "queueState": str(QUEUE_STATE_PATH),
            "sqlite": {
                "enabled": SQLITE_ENABLED,
                "path": str(SQLITE_PATH),
            },
        }

@app.get("/audit/ledger")
def audit_ledger(limit: int = Query(AUDIT_LEDGER_LIMIT, ge=1, le=1000)):
    return {"ok": True, "events": _read_tail_jsonl(AUDIT_LOG_PATH, limit)}

@app.get("/scope/current")
def scope_current():
    with _cv:
        return {
            "ok": True,
            "primary": {
                "leaseToken": _primary_lease_token,
                "fence": _fence,
                "placeId": _primary_place_id,
                "studioSessionId": _primary_session_id,
                "clientId": _primary_client_id,
                "alive": _is_primary_alive(),
                "lastHeartbeatAgeSec": round(_now() - _last_heartbeat, 3) if _primary_lease_token else None,
            },
            "serverSeq": _seq,
        }

@app.post("/register", response_model=RegisterOut)
def register(inp: RegisterIn):
    global _primary_lease_token, _primary_session_id, _primary_client_id, _primary_place_id, _fence, _last_heartbeat

    with _cv:
        if _primary_lease_token and not _is_primary_alive():
            _reset_primary_unlocked()
            _queue.clear()
            _claims.clear()
            _last_wait.clear()
            _last_receipt.clear()
            _save_queue_state_unlocked()

        if _primary_lease_token is None:
            _queue.clear()
            _claims.clear()
            _last_wait.clear()
            _last_receipt.clear()
            _fence += 1
            _primary_lease_token = str(uuid.uuid4())
            _primary_session_id = inp.studioSessionId
            _primary_client_id = inp.clientId
            _primary_place_id = inp.placeId
            _last_heartbeat = _now()
            _save_queue_state_unlocked()
            return RegisterOut(leaseToken=_primary_lease_token, fence=_fence, serverSeq=_seq)

        # Allow reconnect for same session+client
        if inp.studioSessionId == _primary_session_id and inp.clientId == _primary_client_id:
            _last_heartbeat = _now()
            return RegisterOut(leaseToken=_primary_lease_token, fence=_fence, serverSeq=_seq)

        if inp.takeover:
            # Force takeover: reset primary and clear scoped queue/claims.
            _reset_primary_unlocked()
            _queue.clear()
            _claims.clear()
            _last_wait.clear()
            _last_receipt.clear()
            _fence += 1
            _primary_lease_token = str(uuid.uuid4())
            _primary_session_id = inp.studioSessionId
            _primary_client_id = inp.clientId
            _primary_place_id = inp.placeId
            _last_heartbeat = _now()
            _save_queue_state_unlocked()
            return RegisterOut(leaseToken=_primary_lease_token, fence=_fence, serverSeq=_seq)

        raise HTTPException(status_code=409, detail="Primary already registered")

@app.post("/release")
def release(inp: ReleaseIn):
    global _primary_lease_token, _primary_session_id, _primary_client_id, _primary_place_id, _last_heartbeat
    with _cv:
        fence = inp.fence if inp.fence is not None else _fence
        if inp.leaseToken != _primary_lease_token or fence != _fence:
            raise HTTPException(status_code=409, detail="FenceMismatch")
        _reset_primary_unlocked()
        _last_heartbeat = 0.0
        _save_queue_state_unlocked()
        _cv.notify_all()
    return {"ok": True}

@app.get("/sync", response_model=SyncOut)
def sync(
    leaseToken: str = Query(...),
    fence: int = Query(...),
    placeId: int = Query(...),
    studioSessionId: str = Query(...),
):
    with _cv:
        if leaseToken != _primary_lease_token or fence != _fence:
            raise HTTPException(status_code=409, detail="FenceMismatch")
        _require_primary_scope(placeId, studioSessionId)

        return SyncOut(
            rulesVersion=APP_VERSION,
            recommendedSince=0,
            meta={"serverSeq": _seq},
        )

@app.post("/heartbeat")
def heartbeat(inp: HeartbeatIn):
    global _last_heartbeat
    with _cv:
        if inp.leaseToken != _primary_lease_token or inp.fence != _fence:
            raise HTTPException(status_code=409, detail="FenceMismatch")
        _require_primary_scope(inp.placeId, inp.studioSessionId)
        _last_heartbeat = _now()
        return {"ok": True, "serverSeq": _seq}

@app.post("/enqueue")
def enqueue(inp: EnqueueIn):
    # Test helper: enqueue to current primary scope
    if inp.tx.protocolVersion != PROTOCOL_VERSION:
        raise HTTPException(status_code=400, detail="ProtocolVersionMismatch")

    with _cv:
        if _primary_place_id is None or _primary_session_id is None:
            raise HTTPException(status_code=409, detail="NoPrimary")
        if len(_queue) >= MAX_QUEUE_SIZE:
            raise HTTPException(status_code=429, detail="QueueFull")

        actions = _normalize_actions(list(inp.tx.actions or []))
        s = _next_seq_unlocked()
        claim = "CLAIM_" + str(uuid.uuid4())
        item = {
            "seq": s,
            "tx": {
                **inp.tx.model_dump(),
                "actions": actions,
            },
            "claimToken": claim,
            "claimed": False,
            "scope": _scope_key(_primary_place_id, _primary_session_id),
        }
        _queue.append(item)
        _audit_event(
            "tx_enqueue",
            {
                "transactionId": inp.tx.transactionId,
                "seq": s,
                "scope": {"placeId": _primary_place_id, "studioSessionId": _primary_session_id},
            },
        )
        _save_queue_state_unlocked()
        _cv.notify_all()
        return {"ok": True, "seq": s, "pending": len(_queue)}

@app.post("/enqueue_mock")
def enqueue_mock(
    placeId: Optional[int] = Query(None),
    studioSessionId: Optional[str] = Query(None),
):
    with _cv:
        if _primary_place_id is None or _primary_session_id is None:
            raise HTTPException(status_code=409, detail="NoPrimary")
        if len(_queue) >= MAX_QUEUE_SIZE:
            raise HTTPException(status_code=429, detail="QueueFull")

        pid, sid = _resolve_scope_auto(placeId, studioSessionId)

        # Keep multi-studio safe: only allow mock into the active primary scope
        _require_primary_scope(pid, sid)

        s = _next_seq_unlocked()
        claim = "CLAIM_" + str(uuid.uuid4())
        tx = {
            "protocolVersion": PROTOCOL_VERSION,
            "transactionId": f"TX_MOCK_{s}",
            "actions": [
                {"type": "CreateFolder", "parent": "Workspace", "name": "AITest"},
                {
                    "type": "CreateModuleScript",
                    "parent": "Workspace/AITest",
                    "name": "HelloModule",
                    "source": "return function() print(\"hello from Persponify Studio AI\") end",
                },
            ],
        }
        item = {"seq": s, "tx": tx, "claimToken": claim, "claimed": False, "scope": _scope_key(pid, sid)}
        _queue.append(item)
        _audit_event(
            "tx_enqueue",
            {
                "transactionId": tx.get("transactionId"),
                "seq": s,
                "scope": {"placeId": pid, "studioSessionId": sid},
            },
        )
        _save_queue_state_unlocked()
        _cv.notify_all()
        return {"ok": True, "seq": s, "pending": len(_queue), "scope": {"placeId": pid, "studioSessionId": sid}}

@app.post("/wait")
def wait_for_tx(inp: WaitIn):
    deadline = _now() + float(inp.timeoutSec or DEFAULT_WAIT_TIMEOUT_SEC)

    with _cv:
        _maybe_fault_delay("delay_wait")

        if inp.leaseToken != _primary_lease_token or inp.fence != _fence:
            raise HTTPException(status_code=409, detail="FenceMismatch")
        _require_primary_scope(inp.placeId, inp.studioSessionId)

        scope = _scope_key(inp.placeId, inp.studioSessionId)

        def find_next():
            for item in _queue:
                if item.get("scope") == scope and item["seq"] >= inp.since and not item.get("claimed", False):
                    return item
            return None

        while True:
            _cleanup_claims_unlocked()
            item = find_next()
            if item:
                item["claimed"] = True
                claim = item["claimToken"]
                _claims[claim] = {
                    "expiresAt": _now() + CLAIM_TTL_SEC,
                    "seq": item["seq"],
                    "txId": item["tx"].get("transactionId", ""),
                    "scope": scope,
                }
                _last_wait[scope] = {
                    "since": inp.since,
                    "returned": {"seq": item["seq"], "txId": item["tx"].get("transactionId")},
                    "queuePending": len([q for q in _queue if q.get("scope") == scope and not q.get("claimed")]),
                }
                out = WaitOut(seq=item["seq"], fence=_fence, claimToken=claim, tx=item["tx"])
                return JSONResponse(out.model_dump())

            remaining = deadline - _now()
            if remaining <= 0:
                _last_wait[scope] = {
                    "since": inp.since,
                    "returned": None,
                    "queuePending": len([q for q in _queue if q.get("scope") == scope and not q.get("claimed")]),
                }
                return Response(status_code=204)

            _cv.wait(timeout=remaining)

@app.post("/receipt")
def receipt(
    inp: ReceiptIn,
    placeId: int = Query(...),
    studioSessionId: str = Query(...),
):
    with _cv:
        if inp.leaseToken != _primary_lease_token or inp.fence != _fence:
            raise HTTPException(status_code=409, detail="FenceMismatch")
        _require_primary_scope(placeId, studioSessionId)

        _cleanup_claims_unlocked()
        scope = _scope_key(placeId, studioSessionId)

        claim = _claims.get(inp.claimToken)
        if not claim or claim.get("scope") != scope:
            raise HTTPException(status_code=409, detail="ClaimInvalidOrExpired")

        seq_to_remove = claim.get("seq")
        if seq_to_remove is not None:
            for i, item in enumerate(list(_queue)):
                if item.get("scope") == scope and item.get("seq") == seq_to_remove:
                    _queue.pop(i)
                    break

        del _claims[inp.claimToken]
        _save_queue_state_unlocked()
        _cv.notify_all()

        _last_receipt[scope] = {
            "transactionId": inp.transactionId,
            "removedSeq": seq_to_remove,
            "remaining": sum(1 for q in _queue if q.get("scope") == scope),
            "appliedCount": len(inp.applied or []),
            "errorsCount": len(inp.errors or []),
            "notesCount": len(inp.notes or []),
            "errorsPreview": _preview_errors(inp.errors, 5),
        }

        _audit_event(
            "receipt",
            {
                "transactionId": inp.transactionId,
                "errorsCount": len(inp.errors or []),
                "appliedCount": len(inp.applied or []),
                "scope": {"placeId": placeId, "studioSessionId": studioSessionId},
            },
        )

        tx_id = inp.transactionId
        if (inp.errors or []) and tx_id and _should_auto_repair(tx_id):
            job_id = _codex_tx_job_map.get(tx_id)
            project_key = _resolve_project_key_for_scope(placeId, studioSessionId, None)
            context = _get_cached_context(placeId, studioSessionId, project_key)
            context_version = _context_versions.get((int(placeId), str(studioSessionId), str(project_key)), 0)
            context_id = _context_id(placeId, studioSessionId, project_key)
            delta = _get_context_delta(placeId, studioSessionId, project_key)
            prompt_lines = [
                f"Auto-repair failed tx {tx_id}.",
                "Errors:",
                json.dumps(inp.errors or [], indent=2),
            ]
            if job_id:
                prompt_lines.append(f"Original job: {job_id}")
            repair_job = {
                "jobId": str(uuid.uuid4()),
                "createdAt": _now(),
                "contextId": context_id,
                "contextVersion": context_version,
                "mode": "auto",
                "intent": "repair",
                "prompt": "\n".join(prompt_lines),
                "system": None,
                "scope": {
                    "placeId": int(placeId),
                    "studioSessionId": str(studioSessionId),
                    "projectKey": project_key,
                },
                "context": {
                    "summary": _build_context_summary(context),
                    "meta": (context.get("meta") if context else {}),
                    "missing": [],
                    "delta": delta,
                    "lastReceipt": _last_receipt.get(scope),
                },
                "contextRef": {
                    "path": str(_context_file_path(context_id)),
                    "contextId": context_id,
                    "contextVersion": context_version,
                },
                "policy": {
                    "riskProfile": CODEX_POLICY_PROFILE,
                    "allowAutoApply": True,
                    "protectedRoots": list(CODEX_PROTECTED_ROOTS),
                },
                "capabilities": {
                    "actions": SUPPORTED_ACTIONS,
                    "maxSourceBytes": CODEX_MAX_SOURCE_BYTES,
                },
                "repairOf": {
                    "transactionId": tx_id,
                    "jobId": job_id,
                },
            }
            _codex_repair_attempts[tx_id] = _codex_repair_attempts.get(tx_id, 0) + 1
            _codex_repair_last[tx_id] = _now()
            _write_codex_job(repair_job)

        return {"ok": True, **_last_receipt[scope]}

# ----------------------------
# Debug endpoints (AUTO-scope)
# ----------------------------

@app.get("/debug/state")
def debug_state(placeId: Optional[int] = None, studioSessionId: Optional[str] = None):
    with _cv:
        scope = _resolve_scope_for_debug(placeId, studioSessionId)
        q = [it for it in _queue if it.get("scope") == scope]
        primary = {
            "leaseToken": _primary_lease_token,
            "fence": _fence,
            "placeId": _primary_place_id,
            "studioSessionId": _primary_session_id,
            "clientId": _primary_client_id,
            "alive": _is_primary_alive(),
            "lastHeartbeatAgeSec": round(_now() - _last_heartbeat, 3) if _primary_lease_token else None,
        }
        return {
            "primary": primary,
            "serverSeq": _seq,
            "queuePending": len(q),
            "claims": sum(1 for c in _claims.values() if c.get("scope") == scope),
            "queue": q,
        }

@app.get("/debug/last_wait")
def debug_last_wait(placeId: Optional[int] = None, studioSessionId: Optional[str] = None):
    with _cv:
        scope = _resolve_scope_for_debug(placeId, studioSessionId)
        return {"lastWait": _last_wait.get(scope)}

@app.get("/debug/last_receipt")
def debug_last_receipt(placeId: Optional[int] = None, studioSessionId: Optional[str] = None):
    with _cv:
        scope = _resolve_scope_for_debug(placeId, studioSessionId)
        return {"lastReceipt": _last_receipt.get(scope)}

@app.post("/debug/reset")
def debug_reset():
    global _seq, _queue, _claims, _last_wait, _last_receipt, _fault
    with _cv:
        _seq = 0
        _queue = []
        _claims = {}
        _last_wait = {}
        _last_receipt = {}
        _catalog_search_requests.clear()
        _catalog_latest.clear()
        _catalog_versions.clear()
        _fault = {"mode": None}
        _save_queue_state_unlocked()
        _cv.notify_all()
        return {"ok": True}

class FaultIn(BaseModel):
    mode: Optional[str] = None  # e.g. "delay_wait"
    sec: Optional[float] = None

@app.post("/debug/fault")
def debug_fault(inp: FaultIn):
    with _cv:
        _fault["mode"] = inp.mode
        _fault["sec"] = inp.sec
        return {"ok": True, "fault": _fault}


class ShutdownIn(BaseModel):
    reason: Optional[str] = None


@app.post("/shutdown")
def shutdown_server(inp: ShutdownIn):
    def _do_exit():
        time.sleep(0.2)
        os._exit(0)

    threading.Thread(target=_do_exit, daemon=True).start()
    return {"ok": True, "message": "shutting down"}

# ----------------------------
# Context (READ MODE) — AUTO-scope helpers
# ----------------------------

@app.post("/context/export")
def context_export(
    inp: ContextExportIn,
    placeId: Optional[int] = Query(None),
    studioSessionId: Optional[str] = Query(None),
):
    try:
        with _cv:
            # Still requires a primary (safety)
            if _primary_place_id is None or _primary_session_id is None:
                raise HTTPException(status_code=409, detail="NoPrimary")

            pid, sid = _resolve_scope_auto(placeId, studioSessionId)
            _require_primary_scope(pid, sid)

            key = (int(pid), str(sid), str(inp.projectKey or "default"))
            _context_export_requests.pop((int(pid), str(sid)), None)

            if CONTEXT_EXPORT_MIN_INTERVAL_SEC > 0:
                last_at = _context_last_export_at.get(key, 0.0)
                if (_now() - last_at) < CONTEXT_EXPORT_MIN_INTERVAL_SEC:
                    return {
                        "ok": True,
                        "stored": False,
                        "throttled": True,
                        "projectKey": key[2],
                        "contextId": _context_id(pid, sid, key[2]),
                        "contextVersion": _context_versions.get(key, 0),
                    }

            payload = inp.model_dump()
            payload["serverReceivedAt"] = _now()
            prev = _context_latest.get(key)
            meta = payload.get("meta") if isinstance(payload, dict) else None
            incoming_fp = meta.get("fingerprint") if isinstance(meta, dict) else None
            last_fp = _context_fingerprints.get(key)
            changed = True
            if incoming_fp and last_fp == incoming_fp:
                changed = False

            if not changed:
                _context_last_export_at[key] = _now()
                return {
                    "ok": True,
                    "stored": False,
                    "unchanged": True,
                    "projectKey": key[2],
                    "contextId": _context_id(pid, sid, key[2]),
                    "contextVersion": _context_versions.get(key, 0),
                }

            version = _context_versions.get(key, 0) + 1
            _context_versions[key] = version
            if incoming_fp:
                _context_fingerprints[key] = incoming_fp
            payload["contextVersion"] = version
            context_id = _context_id(pid, sid, key[2])
            payload["contextId"] = context_id
            payload = _merge_context_sources(prev, payload)
            _context_deltas[key] = _compute_context_delta(prev, payload)
            _context_latest[key] = payload
            _context_last_export_at[key] = _now()
            try:
                _write_atomic_json(_context_file_path(context_id), payload)
            except Exception:
                pass
            try:
                _db_record_context_snapshot(context_id, version, pid, sid, key[2], payload)
            except Exception:
                pass

            semantic = None
            try:
                semantic = _ensure_semantic_cache(pid, sid, key[2], payload)
            except Exception as exc:
                _audit_event("context_semantic_error", {"error": str(exc)})

            _append_context_event(
                "export",
                {
                    "contextId": context_id,
                    "contextVersion": version,
                    "projectKey": key[2],
                    "treeCount": len(inp.tree),
                    "scriptCount": len(inp.scripts),
                    "delta": _context_deltas.get(key),
                },
            )

            _audit_event(
                "context_export",
                {
                    "contextId": context_id,
                    "contextVersion": version,
                    "projectKey": key[2],
                    "treeCount": len(inp.tree),
                    "scriptCount": len(inp.scripts),
                    "delta": _context_deltas.get(key),
                },
            )

            return {
                "ok": True,
                "stored": True,
                "changed": True,
                "projectKey": key[2],
                "contextId": context_id,
                "contextVersion": version,
                "treeCount": len(inp.tree),
                "scriptCount": len(inp.scripts),
                "delta": _context_deltas.get(key),
                "semanticSummary": (semantic.get("summary") if semantic else None),
            }
    except HTTPException:
        raise
    except Exception as exc:
        detail = traceback.format_exc(limit=4)
        _audit_event("context_export_error", {"error": str(exc), "trace": detail})
        return JSONResponse(status_code=500, content={"ok": False, "error": str(exc)})


@app.post("/context/request")
def context_request(
    inp: ContextRequestIn = Body(default_factory=ContextRequestIn),
    projectKey: str = Query("default"),
    placeId: Optional[int] = Query(None),
    studioSessionId: Optional[str] = Query(None),
):
    with _cv:
        if _primary_place_id is None or _primary_session_id is None:
            raise HTTPException(status_code=409, detail="NoPrimary")
        pid, sid = _resolve_scope_auto(placeId, studioSessionId)
        _require_primary_scope(pid, sid)
        requested_key = None
        if inp.projectKey is not None:
            requested_key = str(inp.projectKey)
        elif projectKey and projectKey != "default":
            requested_key = str(projectKey)
        req_key = _resolve_project_key_for_scope(pid, sid, requested_key)
        request_payload: Dict[str, Any] = {
            "requestedAt": _now(),
        }
        if requested_key:
            request_payload["projectKey"] = requested_key
        if isinstance(inp.roots, list):
            request_payload["roots"] = [str(item) for item in inp.roots if str(item).strip() != ""]
        if isinstance(inp.paths, list):
            request_payload["paths"] = [str(item) for item in inp.paths if str(item).strip() != ""]
        if inp.includeSources is not None:
            request_payload["includeSources"] = bool(inp.includeSources)
        if isinstance(inp.mode, str) and inp.mode in ("full", "diff"):
            request_payload["mode"] = inp.mode
        _context_export_requests[(int(pid), str(sid))] = request_payload
        return {
            "ok": True,
            "requested": True,
            "projectKey": req_key,
            "placeId": pid,
            "studioSessionId": sid,
            "request": request_payload,
        }

@app.get("/context/latest")
def context_latest(
    projectKey: str = Query("default"),
    placeId: Optional[int] = Query(None),
    studioSessionId: Optional[str] = Query(None),
):
    with _cv:
        pid, sid = _resolve_scope_auto(placeId, studioSessionId)
        projectKey = _resolve_project_key(pid, sid, projectKey)
        key = (int(pid), str(sid), str(projectKey))
        data = _get_cached_context(pid, sid, projectKey)
        if not data:
            raise HTTPException(status_code=404, detail="NoContext")
        return {
            "ok": True,
            "projectKey": projectKey,
            "contextId": _context_id(pid, sid, projectKey),
            "contextVersion": _context_versions.get(key, 0),
            "context": data,
        }

@app.get("/context/summary")
def context_summary(
    projectKey: str = Query("default"),
    placeId: Optional[int] = Query(None),
    studioSessionId: Optional[str] = Query(None),
):
    with _cv:
        pid, sid = _resolve_scope_auto(placeId, studioSessionId)
        projectKey = _resolve_project_key(pid, sid, projectKey)
        key = (int(pid), str(sid), str(projectKey))
        data = _get_cached_context(pid, sid, projectKey)
        if not data:
            raise HTTPException(status_code=404, detail="NoContext")
        semantic = _ensure_semantic_cache(pid, sid, projectKey, data)

        scripts = data.get("scripts", []) or []
        tree = data.get("tree", []) or []
        total_bytes = 0
        for s in scripts:
            b = s.get("bytes")
            if isinstance(b, int):
                total_bytes += b

        meta = data.get("meta") if isinstance(data, dict) else None
        if not isinstance(meta, dict):
            meta = {}

        return {
            "ok": True,
            "projectKey": projectKey,
            "contextId": _context_id(pid, sid, projectKey),
            "contextVersion": _context_versions.get(key, 0),
            "placeId": int(pid),
            "studioSessionId": str(sid),
            "gameId": meta.get("gameId"),
            "meta": meta,
            "treeCount": len(tree),
            "scriptCount": len(scripts),
            "totalScriptBytes": total_bytes,
            "serverReceivedAt": data.get("serverReceivedAt"),
            "memory": _get_context_memory(pid, sid, projectKey),
            "semanticSummary": (semantic.get("summary") if semantic else None),
        }

@app.post("/telemetry/report")
def telemetry_report(
    inp: TelemetryReportIn,
    projectKey: str = Query("default"),
    placeId: Optional[int] = Query(None),
    studioSessionId: Optional[str] = Query(None),
):
    with _cv:
        pid, sid = _resolve_scope_auto(placeId, studioSessionId)
        requested = None
        if inp.projectKey and inp.projectKey != "default":
            requested = inp.projectKey
        elif projectKey and projectKey != "default":
            requested = projectKey
        projectKey = _resolve_project_key_for_scope(pid, sid, requested)
        key = (int(pid), str(sid), str(projectKey))
        payload = inp.dict()
        payload["projectKey"] = projectKey
        incoming_fp = None
        meta = payload.get("meta")
        if isinstance(meta, dict):
            incoming_fp = meta.get("fingerprint")
        last_fp = _telemetry_fingerprints.get(key)
        version = _telemetry_versions.get(key, 0)
        has_logs = bool(payload.get("logs"))
        has_diffs = bool(payload.get("diffs"))
        changed = True
        if incoming_fp and last_fp == incoming_fp and not (has_logs or has_diffs):
            changed = False
        if changed:
            version += 1
            _telemetry_versions[key] = version
            if incoming_fp:
                _telemetry_fingerprints[key] = incoming_fp
            history = _telemetry_history.setdefault(key, [])
            history.append(_telemetry_history_entry(payload, version, incoming_fp))
            if TELEMETRY_HISTORY_LIMIT > 0 and len(history) > TELEMETRY_HISTORY_LIMIT:
                _telemetry_history[key] = history[-TELEMETRY_HISTORY_LIMIT:]
        _telemetry_latest[key] = payload
        _telemetry_last_export_at[key] = _now()
        _telemetry_export_requests.pop((int(pid), str(sid)), None)
        return {
            "ok": True,
            "stored": True,
            "changed": changed,
            "projectKey": projectKey,
            "telemetryId": _telemetry_id(pid, sid, projectKey),
            "telemetryVersion": version,
            "nodeCount": len(payload.get("nodes") or []),
            "uiCount": len(payload.get("ui") or []),
        }

@app.post("/telemetry/request")
def telemetry_request(
    inp: TelemetryRequestIn = Body(default_factory=TelemetryRequestIn),
    projectKey: str = Query("default"),
    placeId: Optional[int] = Query(None),
    studioSessionId: Optional[str] = Query(None),
):
    with _cv:
        if _primary_place_id is None or _primary_session_id is None:
            raise HTTPException(status_code=409, detail="NoPrimary")
        pid, sid = _resolve_scope_auto(placeId, studioSessionId)
        _require_primary_scope(pid, sid)
        requested_key = None
        if inp.projectKey is not None:
            requested_key = str(inp.projectKey)
        elif projectKey and projectKey != "default":
            requested_key = str(projectKey)
        req_key = _resolve_project_key_for_scope(pid, sid, requested_key)
        request_payload: Dict[str, Any] = {"requestedAt": _now()}
        if requested_key:
            request_payload["projectKey"] = requested_key
        if isinstance(inp.roots, list):
            request_payload["roots"] = [str(item) for item in inp.roots if str(item).strip() != ""]
        if isinstance(inp.paths, list):
            request_payload["paths"] = [str(item) for item in inp.paths if str(item).strip() != ""]
        if inp.includeScene is not None:
            request_payload["includeScene"] = bool(inp.includeScene)
        if inp.includeGui is not None:
            request_payload["includeGui"] = bool(inp.includeGui)
        if inp.includeLighting is not None:
            request_payload["includeLighting"] = bool(inp.includeLighting)
        if inp.includeSelection is not None:
            request_payload["includeSelection"] = bool(inp.includeSelection)
        if inp.includeCamera is not None:
            request_payload["includeCamera"] = bool(inp.includeCamera)
        if inp.includeLogs is not None:
            request_payload["includeLogs"] = bool(inp.includeLogs)
        if inp.includeDiffs is not None:
            request_payload["includeDiffs"] = bool(inp.includeDiffs)
        if inp.includeAssets is not None:
            request_payload["includeAssets"] = bool(inp.includeAssets)
        if inp.includeTagIndex is not None:
            request_payload["includeTagIndex"] = bool(inp.includeTagIndex)
        if inp.includeUiQa is not None:
            request_payload["includeUiQa"] = bool(inp.includeUiQa)
        _telemetry_export_requests[(int(pid), str(sid))] = request_payload
        return {
            "ok": True,
            "requested": True,
            "projectKey": req_key,
            "placeId": pid,
            "studioSessionId": sid,
            "request": request_payload,
        }

@app.get("/telemetry/latest")
def telemetry_latest(
    projectKey: str = Query("default"),
    placeId: Optional[int] = Query(None),
    studioSessionId: Optional[str] = Query(None),
):
    with _cv:
        pid, sid = _resolve_scope_auto(placeId, studioSessionId)
        projectKey = _resolve_project_key_for_scope(pid, sid, projectKey if projectKey != "default" else None)
        key = (int(pid), str(sid), str(projectKey))
        data = _telemetry_latest.get(key)
        if not data:
            raise HTTPException(status_code=404, detail="NoTelemetry")
        return {
            "ok": True,
            "projectKey": projectKey,
            "telemetryId": _telemetry_id(pid, sid, projectKey),
            "telemetryVersion": _telemetry_versions.get(key, 0),
            "telemetry": data,
        }

@app.get("/telemetry/summary")
def telemetry_summary(
    projectKey: str = Query("default"),
    placeId: Optional[int] = Query(None),
    studioSessionId: Optional[str] = Query(None),
):
    with _cv:
        pid, sid = _resolve_scope_auto(placeId, studioSessionId)
        projectKey = _resolve_project_key_for_scope(pid, sid, projectKey if projectKey != "default" else None)
        key = (int(pid), str(sid), str(projectKey))
        data = _telemetry_latest.get(key)
        if not data:
            raise HTTPException(status_code=404, detail="NoTelemetry")
        meta = data.get("meta") if isinstance(data, dict) else None
        if not isinstance(meta, dict):
            meta = {}
        last_at = _telemetry_last_export_at.get(key)
        age = (_now() - last_at) if last_at else None
        return {
            "ok": True,
            "projectKey": projectKey,
            "telemetryId": _telemetry_id(pid, sid, projectKey),
            "telemetryVersion": _telemetry_versions.get(key, 0),
            "placeId": int(pid),
            "studioSessionId": str(sid),
            "telemetryLastExportAt": last_at,
            "telemetryAgeSec": age,
            "nodeCount": len(data.get("nodes") or []),
            "uiCount": len(data.get("ui") or []),
            "serviceCount": len(data.get("services") or []),
            "logCount": len(data.get("logs") or []),
            "diffCount": len(data.get("diffs") or []),
            "assetCount": (data.get("assets") or {}).get("summary", {}).get("assets", 0)
            if isinstance(data.get("assets"), dict)
            else 0,
            "tagCount": (data.get("tagIndex") or {}).get("summary", {}).get("tags", 0)
            if isinstance(data.get("tagIndex"), dict)
            else 0,
            "attributeCount": (data.get("tagIndex") or {}).get("summary", {}).get("attributes", 0)
            if isinstance(data.get("tagIndex"), dict)
            else 0,
            "uiIssueCount": (data.get("uiQa") or {}).get("counts", {}).get("issues", 0)
            if isinstance(data.get("uiQa"), dict)
            else 0,
            "meta": meta,
        }

@app.get("/telemetry/history")
def telemetry_history(
    projectKey: str = Query("default"),
    placeId: Optional[int] = Query(None),
    studioSessionId: Optional[str] = Query(None),
    limit: int = Query(20, ge=1, le=200),
):
    with _cv:
        pid, sid = _resolve_scope_auto(placeId, studioSessionId)
        projectKey = _resolve_project_key_for_scope(pid, sid, projectKey if projectKey != "default" else None)
        key = (int(pid), str(sid), str(projectKey))
        history = _telemetry_history.get(key)
        if history is None:
            raise HTTPException(status_code=404, detail="NoTelemetry")
        if TELEMETRY_HISTORY_LIMIT > 0:
            limit = min(limit, TELEMETRY_HISTORY_LIMIT)
        return {
            "ok": True,
            "projectKey": projectKey,
            "telemetryId": _telemetry_id(pid, sid, projectKey),
            "telemetryVersion": _telemetry_versions.get(key, 0),
            "history": history[-limit:],
        }

@app.get("/telemetry/ui_qa_report")
def telemetry_ui_qa_report(
    projectKey: str = Query("default"),
    placeId: Optional[int] = Query(None),
    studioSessionId: Optional[str] = Query(None),
    maxIssues: int = Query(50, ge=1, le=500),
):
    with _cv:
        pid, sid = _resolve_scope_auto(placeId, studioSessionId)
        projectKey = _resolve_project_key_for_scope(pid, sid, projectKey if projectKey != "default" else None)
        key = (int(pid), str(sid), str(projectKey))
        data = _telemetry_latest.get(key)
        if not data:
            raise HTTPException(status_code=404, detail="NoTelemetry")
        ui_qa = data.get("uiQa")
        if not isinstance(ui_qa, dict):
            raise HTTPException(status_code=404, detail="NoUiQa")
        issues = ui_qa.get("issues") or []
        if isinstance(issues, list):
            issues = sorted(
                issues,
                key=lambda item: float(item.get("severity") or 0),
                reverse=True,
            )
            issues = issues[:maxIssues]
        else:
            issues = []
        return {
            "ok": True,
            "projectKey": projectKey,
            "telemetryId": _telemetry_id(pid, sid, projectKey),
            "telemetryVersion": _telemetry_versions.get(key, 0),
            "counts": ui_qa.get("counts"),
            "screen": ui_qa.get("screen"),
            "issues": issues,
        }

@app.post("/telemetry/reset")
def telemetry_reset(
    projectKey: str = Query("default"),
    placeId: Optional[int] = Query(None),
    studioSessionId: Optional[str] = Query(None),
):
    with _cv:
        pid, sid = _resolve_scope_auto(placeId, studioSessionId)
        projectKey = _resolve_project_key_for_scope(pid, sid, projectKey if projectKey != "default" else None)
        key = (int(pid), str(sid), str(projectKey))
        existed = key in _telemetry_latest
        if existed:
            del _telemetry_latest[key]
            _telemetry_versions.pop(key, None)
            _telemetry_last_export_at.pop(key, None)
            _telemetry_fingerprints.pop(key, None)
            _telemetry_history.pop(key, None)
        return {
            "ok": True,
            "cleared": existed,
            "projectKey": projectKey,
        }

@app.post("/catalog/request")
def catalog_request(
    inp: CatalogSearchRequestIn = Body(default_factory=CatalogSearchRequestIn),
    projectKey: str = Query("default"),
    placeId: Optional[int] = Query(None),
    studioSessionId: Optional[str] = Query(None),
):
    with _cv:
        if _primary_place_id is None or _primary_session_id is None:
            raise HTTPException(status_code=409, detail="NoPrimary")
        pid, sid = _resolve_scope_auto(placeId, studioSessionId)
        _require_primary_scope(pid, sid)
        requested_key = None
        if inp.projectKey is not None:
            requested_key = str(inp.projectKey)
        elif projectKey and projectKey != "default":
            requested_key = str(projectKey)
        req_key = _resolve_project_key_for_scope(pid, sid, requested_key)
        request_payload: Dict[str, Any] = {
            "requestedAt": _now(),
            "requestId": str(uuid.uuid4()),
        }
        if requested_key:
            request_payload["projectKey"] = requested_key
        if inp.query is not None:
            request_payload["query"] = str(inp.query)
        if isinstance(inp.assetTypes, list):
            request_payload["assetTypes"] = inp.assetTypes
        if isinstance(inp.bundleTypes, list):
            request_payload["bundleTypes"] = inp.bundleTypes
        if inp.category is not None:
            request_payload["category"] = str(inp.category)
        if inp.sortType is not None:
            request_payload["sortType"] = str(inp.sortType)
        if inp.sortAggregation is not None:
            request_payload["sortAggregation"] = str(inp.sortAggregation)
        if inp.salesType is not None:
            request_payload["salesType"] = str(inp.salesType)
        if inp.minPrice is not None:
            request_payload["minPrice"] = int(inp.minPrice)
        if inp.maxPrice is not None:
            request_payload["maxPrice"] = int(inp.maxPrice)
        if inp.maxResults is not None:
            request_payload["maxResults"] = int(inp.maxResults)
        _catalog_search_requests[(int(pid), str(sid))] = request_payload
        return {
            "ok": True,
            "requested": True,
            "projectKey": req_key,
            "placeId": pid,
            "studioSessionId": sid,
            "request": request_payload,
        }

@app.post("/catalog/report")
def catalog_report(
    inp: CatalogSearchReportIn,
    projectKey: str = Query("default"),
    placeId: Optional[int] = Query(None),
    studioSessionId: Optional[str] = Query(None),
):
    with _cv:
        pid, sid = _resolve_scope_auto(placeId, studioSessionId)
        requested = None
        if inp.projectKey and inp.projectKey != "default":
            requested = inp.projectKey
        elif projectKey and projectKey != "default":
            requested = projectKey
        projectKey = _resolve_project_key_for_scope(pid, sid, requested)
        key = (int(pid), str(sid), str(projectKey))
        payload = inp.dict()
        payload["projectKey"] = projectKey
        version = _catalog_versions.get(key, 0) + 1
        _catalog_versions[key] = version
        _catalog_latest[key] = payload
        _catalog_search_requests.pop((int(pid), str(sid)), None)
        return {
            "ok": True,
            "stored": True,
            "projectKey": projectKey,
            "catalogVersion": version,
            "resultCount": len(payload.get("results") or []),
        }

@app.get("/catalog/latest")
def catalog_latest(
    projectKey: str = Query("default"),
    placeId: Optional[int] = Query(None),
    studioSessionId: Optional[str] = Query(None),
):
    with _cv:
        pid, sid = _resolve_scope_auto(placeId, studioSessionId)
        projectKey = _resolve_project_key_for_scope(pid, sid, projectKey if projectKey != "default" else None)
        key = (int(pid), str(sid), str(projectKey))
        data = _catalog_latest.get(key)
        if not data:
            raise HTTPException(status_code=404, detail="NoCatalog")
        return {
            "ok": True,
            "projectKey": projectKey,
            "catalogVersion": _catalog_versions.get(key, 0),
            "catalog": data,
        }

@app.get("/assets/search")
def assets_search(
    query: str = Query(..., min_length=1),
    limit: int = Query(10),
    cursor: Optional[str] = Query(None),
):
    limit = _pick_asset_search_limit(limit)
    ok, data, err = _catalog_search_ids(query, limit, cursor)
    if not ok:
        raise HTTPException(status_code=502, detail=f"catalog search failed: {err}")
    ids: List[int] = []
    for item in data.get("data", []) or []:
        if item.get("itemType") == "Asset" and "id" in item:
            try:
                ids.append(int(item["id"]))
            except (TypeError, ValueError):
                continue
    results = []
    max_results = min(len(ids), ASSET_SEARCH_MAX_RESULTS)
    for asset_id in ids[:max_results]:
        ok_info, info, _ = _marketplace_info(asset_id)
        if not ok_info:
            results.append({"assetId": asset_id})
            continue
        creator = info.get("Creator") or {}
        results.append({
            "assetId": info.get("AssetId", asset_id),
            "name": info.get("Name"),
            "description": info.get("Description"),
            "assetTypeId": info.get("AssetTypeId"),
            "creator": {
                "id": creator.get("Id"),
                "name": creator.get("Name"),
                "type": creator.get("CreatorType"),
            },
            "price": info.get("PriceInRobux"),
            "isForSale": info.get("IsForSale"),
            "isPublicDomain": info.get("IsPublicDomain"),
            "iconImageId": info.get("IconImageAssetId"),
        })
    return {
        "ok": True,
        "query": query,
        "limit": limit,
        "previousCursor": data.get("previousPageCursor"),
        "nextCursor": data.get("nextPageCursor"),
        "results": results,
        "resultCount": len(results),
    }

@app.get("/assets/info")
def assets_info(assetId: int = Query(..., ge=1)):
    ok, info, err = _marketplace_info(assetId)
    if not ok:
        raise HTTPException(status_code=502, detail=f"asset info failed: {err}")
    return {"ok": True, "asset": info}

@app.get("/context/semantic")
def context_semantic(
    projectKey: str = Query("default"),
    placeId: Optional[int] = Query(None),
    studioSessionId: Optional[str] = Query(None),
    includeScripts: bool = Query(False),
    limit: int = Query(200, ge=1, le=1000),
):
    with _cv:
        pid, sid = _resolve_scope_auto(placeId, studioSessionId)
        projectKey = _resolve_project_key(pid, sid, projectKey)
        key = (int(pid), str(sid), str(projectKey))
        data = _get_cached_context(pid, sid, projectKey)
        if not data:
            raise HTTPException(status_code=404, detail="NoContext")
        semantic = _ensure_semantic_cache(pid, sid, projectKey, data)
        if not semantic:
            raise HTTPException(status_code=404, detail="NoSemantic")

        scripts: List[Dict[str, Any]] = []
        if includeScripts:
            entries = semantic.get("scripts") or {}
            for path in sorted(entries.keys())[:limit]:
                entry = entries.get(path)
                if entry:
                    scripts.append(entry)

        return {
            "ok": True,
            "projectKey": projectKey,
            "contextId": _context_id(pid, sid, projectKey),
            "contextVersion": _context_versions.get(key, 0),
            "summary": semantic.get("summary"),
            "scripts": scripts,
        }

@app.get("/context/script")
def context_script(
    path: str = Query(...),
    projectKey: str = Query("default"),
    placeId: Optional[int] = Query(None),
    studioSessionId: Optional[str] = Query(None),
):
    with _cv:
        pid, sid = _resolve_scope_auto(placeId, studioSessionId)
        projectKey = _resolve_project_key(pid, sid, projectKey)
        key = (int(pid), str(sid), str(projectKey))
        data = _get_cached_context(pid, sid, projectKey)
        if not data:
            raise HTTPException(status_code=404, detail="NoContext")

        scripts = data.get("scripts", []) or []
        for s in scripts:
            if s.get("path") == path:
                if not _has_full_source(s):
                    reason = s.get("sourceOmittedReason")
                    if reason == "diff":
                        raise HTTPException(status_code=404, detail="SourceOmitted")
                    if s.get("sourceTruncated"):
                        raise HTTPException(status_code=404, detail="SourceTruncated")
                    raise HTTPException(status_code=404, detail="SourceMissing")
                return {"ok": True, "projectKey": projectKey, "script": s}

        raise HTTPException(status_code=404, detail="ScriptNotFound")

@app.get("/context/missing")
def context_missing(
    projectKey: str = Query("default"),
    placeId: Optional[int] = Query(None),
    studioSessionId: Optional[str] = Query(None),
):
    with _cv:
        pid, sid = _resolve_scope_auto(placeId, studioSessionId)
        projectKey = _resolve_project_key(pid, sid, projectKey)
        key = (int(pid), str(sid), str(projectKey))
        data = _get_cached_context(pid, sid, projectKey)
        if not data:
            raise HTTPException(status_code=404, detail="NoContext")

        scripts = data.get("scripts", []) or []
        missing = [s.get("path") for s in scripts if _is_missing_source(s)]
        return {"ok": True, "projectKey": projectKey, "missing": missing, "count": len(missing)}

@app.get("/context/events")
def context_events(limit: int = Query(50, ge=1, le=500)):
    return {"ok": True, "events": _read_tail_jsonl(CONTEXT_EVENTS_PATH, limit)}

@app.get("/context/memory")
def context_memory_get(
    projectKey: str = Query("default"),
    placeId: Optional[int] = Query(None),
    studioSessionId: Optional[str] = Query(None),
):
    with _cv:
        pid, sid = _resolve_scope_auto(placeId, studioSessionId)
        projectKey = _resolve_project_key(pid, sid, projectKey)
        memory = _get_context_memory(pid, sid, projectKey)
        if memory is None:
            raise HTTPException(status_code=404, detail="NoMemory")
        return {"ok": True, "projectKey": projectKey, "memory": memory}

@app.post("/context/memory")
def context_memory_set(
    inp: ContextMemoryIn,
    placeId: Optional[int] = Query(None),
    studioSessionId: Optional[str] = Query(None),
):
    with _cv:
        if _primary_place_id is None or _primary_session_id is None:
            raise HTTPException(status_code=409, detail="NoPrimary")
        pid, sid = _resolve_scope_auto(placeId, studioSessionId)
        _require_primary_scope(pid, sid)
        key = (int(pid), str(sid), str(inp.projectKey))
        memory = (inp.memory or "").strip()
        if memory == "":
            raise HTTPException(status_code=400, detail="EmptyMemory")
        _context_memory[key] = memory
        path = _context_memory_path(_context_id(pid, sid, inp.projectKey))
        try:
            path.write_text(memory)
            try:
                _context_memory_mtime[key] = path.stat().st_mtime
            except Exception:
                pass
        except Exception:
            pass
        try:
            _db_record_context_memory(_context_id(pid, sid, inp.projectKey), memory)
        except Exception:
            pass
        _append_context_event(
            "memory_set",
            {
                "contextId": _context_id(pid, sid, inp.projectKey),
                "projectKey": inp.projectKey,
                "bytes": len(memory.encode("utf-8")),
            },
        )
        return {"ok": True, "projectKey": inp.projectKey, "bytes": len(memory.encode("utf-8"))}

@app.post("/context/reset")
def context_reset(
    projectKey: str = Query("default"),
    placeId: Optional[int] = Query(None),
    studioSessionId: Optional[str] = Query(None),
):
    with _cv:
        if _primary_place_id is None or _primary_session_id is None:
            raise HTTPException(status_code=409, detail="NoPrimary")

        pid, sid = _resolve_scope_auto(placeId, studioSessionId)
        _require_primary_scope(pid, sid)

        projectKey = _resolve_project_key(pid, sid, projectKey)
        key = (int(pid), str(sid), str(projectKey))
        existed = key in _context_latest
        if existed:
            del _context_latest[key]
            _context_versions.pop(key, None)
            _context_deltas.pop(key, None)
            _context_memory.pop(key, None)
            _context_last_export_at.pop(key, None)
            _context_semantic.pop(key, None)
            _context_memory_mtime.pop(key, None)
            _context_fingerprints.pop(key, None)
            try:
                _context_file_path(_context_id(pid, sid, projectKey)).unlink()
            except Exception:
                pass
            try:
                _context_memory_path(_context_id(pid, sid, projectKey)).unlink()
            except Exception:
                pass
            try:
                _db_clear_context(_context_id(pid, sid, projectKey))
            except Exception:
                pass
        _append_context_event(
            "reset",
            {
                "contextId": _context_id(pid, sid, projectKey),
                "projectKey": projectKey,
                "deleted": existed,
            },
        )
        return {"ok": True, "deleted": existed, "projectKey": projectKey}

# ----------------------------
# Codex bridge
# ----------------------------

@app.post("/codex/compile")
def codex_compile(payload: Dict[str, Any]):
    actions = payload.get("actions")
    if actions is None and isinstance(payload.get("plan"), dict):
        actions = payload["plan"].get("actions")
    if actions is None and isinstance(payload.get("plan"), list):
        if all(isinstance(item, dict) for item in payload["plan"]):
            actions = payload["plan"]
    if actions is None and isinstance(payload.get("dsl"), dict):
        actions = payload["dsl"].get("actions")

    if not isinstance(actions, list):
        raise HTTPException(status_code=400, detail="Invalid actions list")

    actions = _normalize_actions(actions)
    errors = _validate_codex_actions(actions, None)
    risk = payload.get("riskScore")
    if risk is None:
        risk = payload.get("risk")
    if risk is None:
        risk = payload.get("risk_score")
    try:
        risk_val = float(risk) if risk is not None else None
    except Exception:
        risk_val = None
    if risk_val is not None and risk_val > CODEX_MAX_RISK and CODEX_POLICY_PROFILE != "power":
        errors = list(errors or [])
        errors.append(f"riskScore {risk_val} exceeds {CODEX_MAX_RISK}")
    if errors:
        return {"ok": False, "errors": errors}
    return {"ok": True, "actions": actions, "count": len(actions)}

@app.post("/codex/job")
def codex_job(inp: CodexJobIn):
    with _cv:
        if _primary_place_id is None or _primary_session_id is None:
            raise HTTPException(status_code=409, detail="NoPrimary")

        pid, sid = _resolve_scope_auto(inp.placeId, inp.studioSessionId)
        _require_primary_scope(pid, sid)

        project_key = _resolve_project_key_for_scope(pid, sid, inp.projectKey)
        key = (int(pid), str(sid), str(project_key))

        context = _get_cached_context(pid, sid, project_key)
        context_version = _context_versions.get(key, 0)
        context_id = _context_id(pid, sid, project_key)
        delta = _get_context_delta(pid, sid, project_key)
        last_receipt = _last_receipt.get(_scope_key(pid, sid))
        memory = _get_context_memory(pid, sid, project_key)
        focus_pack = _build_focus_pack(context, delta)
        semantic = _ensure_semantic_cache(pid, sid, project_key, context)
        focus_semantic: Dict[str, Any] = {}
        if semantic:
            entries = semantic.get("scripts") or {}
            for item in focus_pack.get("scripts", []):
                path = item.get("path")
                if path and path in entries:
                    focus_semantic[path] = entries.get(path)

        missing = []
        if context:
            scripts = context.get("scripts", []) or []
            missing = [s.get("path") for s in scripts if _is_missing_source(s)]

        script_count = len(context.get("scripts", []) or []) if context else 0
        scenario = _classify_prompt(inp.prompt, script_count)
        packs: Dict[str, Any] = {}
        if CODEX_PACKS_ENABLED:
            if scenario in {"review", "continue", "refactor", "general"}:
                packs["analysis"] = _build_analysis_pack(context, semantic, delta)
            if scenario == "rollback":
                packs["rollback"] = _build_rollback_pack(int(pid), str(sid), str(project_key))
            if scenario == "greenfield":
                packs["blueprint"] = _build_blueprint_pack(script_count)
            if scenario == "refactor":
                packs["refactor"] = _build_refactor_pack()

        created_at = _now()
        job = {
            "jobId": str(uuid.uuid4()),
            "createdAt": created_at,
            "contextId": context_id,
            "contextVersion": context_version,
            "mode": "auto" if inp.autoApply else "manual",
            "intent": inp.intent or "edit",
            "prompt": inp.prompt,
            "system": inp.system,
            "scope": {
                "placeId": int(pid),
                "studioSessionId": str(sid),
                "projectKey": project_key,
            },
            "context": {
                "summary": _build_context_summary(context),
                "meta": (context.get("meta") if context else {}),
                "missing": missing,
                "delta": delta,
                "lastReceipt": last_receipt,
                "memory": memory,
                "focus": focus_pack,
                "semantic": (semantic.get("summary") if semantic else None),
                "focusSemantic": (focus_semantic or None),
                "scenario": scenario,
                "packs": packs or None,
            },
            "contextRef": {
                "path": str(_context_file_path(context_id)),
                "contextId": context_id,
                "contextVersion": context_version,
            },
            "policy": {
                "riskProfile": CODEX_POLICY_PROFILE,
                "allowAutoApply": bool(inp.autoApply),
                "protectedRoots": list(CODEX_PROTECTED_ROOTS),
            },
            "capabilities": {
                "actions": SUPPORTED_ACTIONS,
                "maxSourceBytes": 250000,
            },
        }
        return _write_codex_job(job)

@app.get("/codex/status")
def codex_status():
    with _codex_lock:
        return {
            "ok": True,
            "pending": _codex_pending_count(),
            "lastJob": _codex_state.get("lastJob"),
            "lastResponse": _codex_state.get("lastResponse"),
            "lastError": _codex_state.get("lastError"),
        }

@app.post("/codex/response")
def codex_response(payload: Dict[str, Any]):
    job_id = str(payload.get("jobId") or "")
    if not job_id:
        raise HTTPException(status_code=400, detail="Missing jobId")
    _process_codex_response(job_id, payload, None)
    return {"ok": True, "jobId": job_id}

# ----------------------------
# MCP (HTTP transport)
# ----------------------------

def _mcp_project_key() -> str:
    data = status()
    primary = data.get("primary") if isinstance(data, dict) else None
    if isinstance(primary, dict):
        place = primary.get("placeId")
        session = primary.get("studioSessionId")
        if place and session:
            return f"p_{place}__s_{session}"
    return "default"


def _mcp_tool_call(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    if name == "enqueue_actions":
        actions = args.get("actions")
        if not isinstance(actions, list):
            return {"isError": True, "content": [{"type": "text", "text": "actions must be a list"}]}
        actions = _normalize_actions(actions)
        tx_id = args.get("transactionId") or f"TX_MCP_{uuid.uuid4()}"
        tx = TxEnvelope(protocolVersion=PROTOCOL_VERSION, transactionId=tx_id, actions=actions)
        try:
            res = enqueue(EnqueueIn(tx=tx))
        except HTTPException as exc:
            return {
                "isError": True,
                "content": [{"type": "text", "text": f"enqueue failed: {exc.detail}"}],
            }
        return {
            "content": [
                {"type": "text", "text": json.dumps({"transactionId": tx_id, "result": res}, indent=2)}
            ]
        }

    if name == "get_status":
        res = status()
        return {"content": [{"type": "text", "text": json.dumps(res or {}, indent=2)}]}

    if name == "get_context_summary":
        project_key = _mcp_project_key()
        try:
            res = context_summary(projectKey=project_key, placeId=None, studioSessionId=None)
        except HTTPException as exc:
            if exc.detail == "NoContext" and project_key != "default":
                try:
                    res = context_summary(projectKey="default", placeId=None, studioSessionId=None)
                except HTTPException as exc2:
                    return {
                        "isError": True,
                        "content": [{"type": "text", "text": f"context summary failed: {exc2.detail}"}],
                    }
            else:
                return {
                    "isError": True,
                    "content": [{"type": "text", "text": f"context summary failed: {exc.detail}"}],
                }
        return {"content": [{"type": "text", "text": json.dumps(res or {}, indent=2)}]}

    if name == "request_context_export":
        payload: Dict[str, Any] = {}
        for key in ("projectKey", "roots", "paths", "includeSources", "mode"):
            if key in args:
                payload[key] = args[key]
        try:
            req = ContextRequestIn(**payload)
            project_key = str(args.get("projectKey") or "default")
            res = context_request(req, projectKey=project_key, placeId=None, studioSessionId=None)
        except HTTPException as exc:
            return {
                "isError": True,
                "content": [{"type": "text", "text": f"context export request failed: {exc.detail}"}],
            }
        return {"content": [{"type": "text", "text": json.dumps(res or {}, indent=2)}]}

    return {"isError": True, "content": [{"type": "text", "text": f"Unknown tool: {name}"}]}


def _mcp_stream(payload: Dict[str, Any]):
    data = json.dumps(payload, ensure_ascii=True)
    yield f"event: message\ndata: {data}\n\n"


@app.post("/mcp")
def mcp_http(payload: Dict[str, Any], request: Request):
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid MCP payload")
    response = mcp_handle_request(payload, _mcp_tool_call)
    if response is None:
        return Response(status_code=204)
    accept = request.headers.get("accept") or ""
    if "text/event-stream" in accept.lower():
        return StreamingResponse(_mcp_stream(response), media_type="text/event-stream")
    return JSONResponse(content=response)


@app.get("/mcp")
def mcp_info():
    return {"ok": True, "transport": "http"}

# ----------------------------
# AI (local companion)
# ----------------------------

MOE_MAX_EXPERT_CHARS = 8000
DEFAULT_MOE_TIMEOUT_SEC = 18.0
MEMORY_MAX_CHARS = 1200
DEFAULT_MOE_STATS_PATH = "companion/moe_stats.json"
MOE_LEARN_RATE = 0.2
MOE_IDLE_BONUS_SEC = 600.0
MOE_LATENCY_CAP_MS = 15000.0
MOE_EXPERT_GUIDE = (
    "You are an expert advisor. Provide analysis and suggestions only. "
    "Do not apply edits or claim to have changed files. Focus on risks, edge cases, "
    "and recommended changes so a master model can apply them."
)
MOE_MASTER_GUIDE = (
    "You are the master editor. You are the only model allowed to propose final edits "
    "or changes. If script context is missing, request it (use /context/export or "
    "/context/script). When edits are needed, describe them as discrete actions suitable "
    "for a tx update. Use expert input to produce a single cohesive plan or response."
)
MEMORY_SYSTEM = (
    "You update a compact, factual memory for future responses. "
    "Only keep durable preferences, project details, constraints, and decisions. "
    "Exclude transient chit-chat."
)

_moe_stats_lock = threading.RLock()
_moe_stats_cache: Optional[Dict[str, Any]] = None

def _truncate_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n[truncated]"

def _moe_stats_path() -> str:
    return os.environ.get("PERSPONIFY_MOE_STATS", DEFAULT_MOE_STATS_PATH)

def _load_moe_stats() -> Dict[str, Any]:
    global _moe_stats_cache
    with _moe_stats_lock:
        if _moe_stats_cache is not None:
            return _moe_stats_cache
        path = Path(_moe_stats_path())
        data: Dict[str, Any] = {}
        try:
            if path.exists():
                raw = json.loads(path.read_text())
                if isinstance(raw, dict):
                    data = raw
        except Exception:
            data = {}
        _moe_stats_cache = data
        return data

def _save_moe_stats(stats: Dict[str, Any]) -> None:
    with _moe_stats_lock:
        path = Path(_moe_stats_path())
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(stats, indent=2, sort_keys=True))

def _update_moe_stats(results: List[Dict[str, Any]]) -> None:
    with _moe_stats_lock:
        stats = _load_moe_stats()
        now = time.time()
        for res in results:
            name = str(res.get("adapter") or "")
            if not name:
                continue
            entry = stats.setdefault(name, {})
            ok = bool(res.get("ok"))
            duration = int(res.get("durationMs") or 0)
            success = int(entry.get("success") or 0)
            fail = int(entry.get("fail") or 0)
            avg_ms = entry.get("avg_ms")
            if avg_ms is None:
                avg_ms = duration
            else:
                avg_ms = (1.0 - MOE_LEARN_RATE) * float(avg_ms) + MOE_LEARN_RATE * float(duration)
            entry["avg_ms"] = int(avg_ms)
            entry["last_ms"] = duration
            entry["last_ok"] = ok
            entry["last_used"] = now
            entry["success"] = success + (1 if ok else 0)
            entry["fail"] = fail + (0 if ok else 1)
        _save_moe_stats(stats)

def _score_adapter(adapter: Any, stats: Dict[str, Any], now: float) -> float:
    entry = stats.get(adapter.name, {}) if stats else {}
    success = float(entry.get("success") or 0)
    fail = float(entry.get("fail") or 0)
    total = success + fail
    success_rate = (success / total) if total > 0 else 0.55
    avg_ms = float(entry.get("avg_ms") or 7000.0)
    latency_penalty = min(1.0, avg_ms / MOE_LATENCY_CAP_MS)
    feedback = float(entry.get("feedback_ema") or 0.0)
    last_used = entry.get("last_used")
    idle_bonus = 0.0
    if not last_used:
        idle_bonus = 0.15
    else:
        idle_sec = max(0.0, now - float(last_used))
        idle_bonus = min(0.2, (idle_sec / MOE_IDLE_BONUS_SEC) * 0.2)
    return success_rate + feedback + idle_bonus - (0.25 * latency_penalty)

def _build_moe_system(user_system: Optional[str]) -> str:
    base = (
        "You are a synthesis model. Combine the expert responses into the best single answer. "
        "Resolve conflicts, keep it concise, and mention uncertainty if needed. "
        f"{MOE_MASTER_GUIDE}"
    )
    if user_system:
        return f"{user_system}\n\n{base}"
    return base

def _build_moe_expert_system(user_system: Optional[str]) -> str:
    if user_system:
        return f"{user_system}\n\n{MOE_EXPERT_GUIDE}"
    return MOE_EXPERT_GUIDE

def _build_moe_prompt(prompt: str, results: List[Dict[str, Any]]) -> str:
    lines = ["User prompt:", prompt, "", "Expert responses:"]
    for res in results:
        if not res.get("ok"):
            continue
        text = _truncate_text(str(res.get("text") or ""), MOE_MAX_EXPERT_CHARS)
        lines.append(f"[{res.get('adapter')}]")
        lines.append(text)
        lines.append("")
    return "\n".join(lines).strip()

def _build_memory_prompt(summary: str, transcript: str, max_chars: int) -> str:
    current = summary.strip() if summary else ""
    return (
        "Current memory:\n"
        f"{current}\n\n"
        "Recent conversation:\n"
        f"{transcript.strip()}\n\n"
        f"Update the memory in <= {max_chars} characters. "
        "Use short bullet points. Keep only useful facts."
    )

def _truncate_memory(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip()

def _auto_moe_cap(adapters: List[Any]) -> int:
    cpu = os.cpu_count() or 4
    local = []
    remote = []
    for adapter in adapters:
        try:
            hint = adapter.resource_hint()
        except Exception:
            hint = "remote"
        if hint == "local":
            local.append(adapter)
        else:
            remote.append(adapter)

    remote_cap = min(len(remote), max(1, cpu))
    local_cap = 1 if local else 0
    return min(len(adapters), remote_cap + local_cap)

def _select_moe_adapters(
    svc: HeadlessService,
    include: Optional[List[str]] = None,
    max_experts: Optional[int] = None,
    adaptive: bool = True,
) -> List[Any]:
    adapters = []
    for cfg in svc.config.adapters:
        if not cfg.enabled:
            continue
        if include and cfg.name not in include:
            continue
        try:
            adapter = svc.resolve_adapter(cfg.name)
        except Exception:
            continue
        try:
            if not adapter.is_available():
                continue
        except Exception:
            continue
        adapters.append(adapter)

    if not adapters:
        return []

    adapters = [a for a in adapters if getattr(a, "type", "") != "echo"]
    if not adapters:
        return []

    if adaptive and not include:
        stats = _load_moe_stats()
        now = time.time()
        adapters.sort(key=lambda a: _score_adapter(a, stats, now), reverse=True)

    auto_cap = _auto_moe_cap(adapters)
    if max_experts:
        cap = max(1, int(max_experts))
        cap = min(cap, auto_cap)
    else:
        cap = auto_cap
    adapters = adapters[:cap]
    return adapters

def _run_moe_experts(
    adapters: List[Any],
    prompt: str,
    system: Optional[str],
    timeout_sec: Optional[float],
) -> List[Dict[str, Any]]:
    timeout = float(timeout_sec or DEFAULT_MOE_TIMEOUT_SEC)
    timeout = max(5.0, min(timeout, 120.0))
    order = {adapter.name: idx for idx, adapter in enumerate(adapters)}
    results: List[Dict[str, Any]] = []

    def _call(adapter: Any) -> Dict[str, Any]:
        start = time.time()
        try:
            expert_system = _build_moe_expert_system(system)
            text = adapter.complete(prompt, system=expert_system)
            return {
                "adapter": adapter.name,
                "adapterType": adapter.type,
                "ok": True,
                "text": text,
                "error": None,
                "durationMs": int((time.time() - start) * 1000),
            }
        except Exception as exc:
            return {
                "adapter": adapter.name,
                "adapterType": adapter.type,
                "ok": False,
                "text": "",
                "error": str(exc),
                "durationMs": int((time.time() - start) * 1000),
            }

    worker_cap = min(len(adapters), max(1, _auto_moe_cap(adapters)))
    with ThreadPoolExecutor(max_workers=worker_cap) as pool:
        future_map = {pool.submit(_call, adapter): adapter for adapter in adapters}
        done, not_done = wait(future_map, timeout=timeout)
        for future in done:
            try:
                results.append(future.result())
            except Exception as exc:
                adapter = future_map[future]
                results.append({
                    "adapter": adapter.name,
                    "adapterType": adapter.type,
                    "ok": False,
                    "text": "",
                    "error": str(exc),
                    "durationMs": int(timeout * 1000),
                })
        for future in not_done:
            adapter = future_map[future]
            future.cancel()
            results.append({
                "adapter": adapter.name,
                "adapterType": adapter.type,
                "ok": False,
                "text": "",
                "error": "timeout",
                "durationMs": int(timeout * 1000),
            })

    results.sort(key=lambda item: order.get(item.get("adapter", ""), 0))
    return results

def _select_merge_adapter(
    svc: HeadlessService,
    merge_name: Optional[str],
    results: List[Dict[str, Any]],
    adapters: List[Any],
) -> Any:
    candidate_names = []
    if merge_name:
        candidate_names.append(merge_name)
    if svc.config.default_adapter:
        candidate_names.append(svc.config.default_adapter)
    for res in results:
        if res.get("ok"):
            candidate_names.append(res.get("adapter"))
    for adapter in adapters:
        candidate_names.append(adapter.name)

    for name in candidate_names:
        if not name:
            continue
        try:
            adapter = svc.resolve_adapter(name)
        except Exception:
            continue
        try:
            if not adapter.is_available():
                continue
        except Exception:
            continue
        return adapter
    raise RuntimeError("No available merge adapter")

@app.post("/ai/complete")
def ai_complete(inp: AiCompleteIn):
    try:
        svc = _get_service()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"CompanionInitFailed: {exc}") from exc

    try:
        adapter = svc.resolve_adapter(inp.adapter)
        text = adapter.complete(inp.prompt, system=inp.system)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"CompanionCompleteFailed: {exc}") from exc

    return {
        "ok": True,
        "text": text,
        "adapter": adapter.name,
        "adapterType": adapter.type,
    }

@app.post("/ai/stream")
def ai_stream(inp: AiCompleteIn):
    try:
        svc = _get_service()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"CompanionInitFailed: {exc}") from exc

    try:
        adapter = svc.resolve_adapter(inp.adapter)
        iterator = adapter.stream(inp.prompt, system=inp.system)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"CompanionStreamFailed: {exc}") from exc

    return StreamingResponse(iterator, media_type="text/plain")

@app.post("/ai/moe/complete")
def ai_moe_complete(inp: AiMoeIn):
    try:
        svc = _get_service()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"CompanionInitFailed: {exc}") from exc

    adaptive = True if inp.autoSelect is None else bool(inp.autoSelect)
    adapters = _select_moe_adapters(svc, inp.includeAdapters, inp.maxExperts, adaptive=adaptive)
    if not adapters:
        raise HTTPException(status_code=400, detail="NoAdaptersAvailable")

    results = _run_moe_experts(adapters, inp.prompt, inp.system, inp.timeoutSec)
    if inp.learn is not False:
        _update_moe_stats(results)
    ok_results = [res for res in results if res.get("ok")]
    if not ok_results:
        raise HTTPException(status_code=500, detail="MoEAllExpertsFailed")

    try:
        merge_name = inp.mergeAdapter or inp.masterAdapter
        merge_adapter = _select_merge_adapter(svc, merge_name, results, adapters)
        merged = merge_adapter.complete(
            _build_moe_prompt(inp.prompt, ok_results),
            system=_build_moe_system(inp.system),
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"MoEMergeFailed: {exc}") from exc

    return {
        "ok": True,
        "mergeAdapter": merge_adapter.name,
        "mergeAdapterType": merge_adapter.type,
        "merged": merged,
        "experts": results,
    }

@app.post("/ai/moe/stream")
def ai_moe_stream(inp: AiMoeIn):
    try:
        svc = _get_service()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"CompanionInitFailed: {exc}") from exc

    adaptive = True if inp.autoSelect is None else bool(inp.autoSelect)
    adapters = _select_moe_adapters(svc, inp.includeAdapters, inp.maxExperts, adaptive=adaptive)
    if not adapters:
        raise HTTPException(status_code=400, detail="NoAdaptersAvailable")

    results = _run_moe_experts(adapters, inp.prompt, inp.system, inp.timeoutSec)
    if inp.learn is not False:
        _update_moe_stats(results)
    ok_results = [res for res in results if res.get("ok")]
    if not ok_results:
        raise HTTPException(status_code=500, detail="MoEAllExpertsFailed")

    try:
        merge_name = inp.mergeAdapter or inp.masterAdapter
        merge_adapter = _select_merge_adapter(svc, merge_name, results, adapters)
        iterator = merge_adapter.stream(
            _build_moe_prompt(inp.prompt, ok_results),
            system=_build_moe_system(inp.system),
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"MoEMergeFailed: {exc}") from exc

    return StreamingResponse(iterator, media_type="text/plain")

@app.get("/ai/moe/stats")
def ai_moe_stats():
    stats = _load_moe_stats()
    return {"ok": True, "stats": stats}

@app.post("/ai/moe/feedback")
def ai_moe_feedback(inp: AiMoeFeedbackIn):
    stats = _load_moe_stats()
    entry = stats.setdefault(inp.adapter, {})
    feedback = float(entry.get("feedback_ema") or 0.0)
    entry["feedback_ema"] = (1.0 - MOE_LEARN_RATE) * feedback + MOE_LEARN_RATE * float(inp.score)
    entry["feedback_count"] = int(entry.get("feedback_count") or 0) + 1
    entry["last_feedback"] = float(inp.score)
    entry["last_feedback_note"] = inp.note or ""
    entry["last_feedback_at"] = time.time()
    _save_moe_stats(stats)
    return {"ok": True, "adapter": inp.adapter, "feedback": entry["feedback_ema"]}

@app.get("/ai/models")
def ai_models():
    try:
        svc = _get_service()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"CompanionInitFailed: {exc}") from exc

    out = []
    for cfg in svc.config.adapters:
        entry = {
            "name": cfg.name,
            "type": cfg.type,
            "enabled": cfg.enabled,
            "available": False,
            "capabilities": {},
        }
        if cfg.enabled:
            try:
                adapter = svc.adapters.get(cfg.name) or svc.registry.create(cfg)
                entry["available"] = adapter.is_available()
                entry["capabilities"] = adapter.capabilities()
            except Exception as exc:
                entry["error"] = str(exc)
        out.append(entry)

    return {
        "ok": True,
        "defaultAdapter": svc.config.default_adapter,
        "adapters": out,
    }

@app.post("/ai/memory/summarize")
def ai_memory_summarize(inp: AiMemoryIn):
    try:
        svc = _get_service()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"CompanionInitFailed: {exc}") from exc

    if not inp.transcript.strip():
        return {"ok": True, "summary": inp.summary or ""}

    max_chars = int(inp.maxChars or MEMORY_MAX_CHARS)
    max_chars = max(200, min(max_chars, 4000))

    try:
        adapter = svc.resolve_adapter(inp.adapter)
        if getattr(adapter, "type", "") == "echo":
            raise RuntimeError("Echo adapter cannot update memory")
        text = adapter.complete(
            _build_memory_prompt(inp.summary or "", inp.transcript, max_chars),
            system=MEMORY_SYSTEM,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"MemoryUpdateFailed: {exc}") from exc

    return {
        "ok": True,
        "summary": _truncate_memory(str(text or ""), max_chars),
        "adapter": adapter.name,
        "adapterType": adapter.type,
    }

@app.get("/ai/health")
def ai_health():
    try:
        svc = _get_service()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"CompanionInitFailed: {exc}") from exc

    return {
        "ok": True,
        "configPath": _companion_config_path(),
        "defaultAdapter": svc.config.default_adapter,
        "adapterCount": len(svc.config.adapters),
    }

@app.post("/ai/reload")
def ai_reload():
    try:
        svc = _reload_service()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"CompanionReloadFailed: {exc}") from exc

    return {
        "ok": True,
        "configPath": _companion_config_path(),
        "defaultAdapter": svc.config.default_adapter,
        "adapterCount": len(svc.config.adapters),
    }

@app.post("/ai/secrets")
def ai_secrets(inp: AiSecretsIn):
    try:
        svc = _get_service()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"CompanionInitFailed: {exc}") from exc

    try:
        svc.apply_secrets(
            by_adapter=inp.byAdapter or {},
            by_type=inp.byType or {},
            replace=bool(inp.replace),
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"CompanionSecretsFailed: {exc}") from exc

    return {
        "ok": True,
        "replace": bool(inp.replace),
        "adapterSecrets": len(inp.byAdapter or {}),
        "typeSecrets": len(inp.byType or {}),
    }

@app.get("/ai/adapter_models")
def ai_adapter_models(adapter: str = Query(...)):
    try:
        svc = _get_service()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"CompanionInitFailed: {exc}") from exc

    try:
        adapter_obj = svc.resolve_adapter(adapter)
        models = adapter_obj.list_models()
    except NotImplementedError:
        return {"ok": True, "adapter": adapter, "supported": False, "models": []}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"CompanionModelsFailed: {exc}") from exc

    return {"ok": True, "adapter": adapter_obj.name, "supported": True, "models": models}

class ChunkSourceIn(BaseModel):
    source: str
    chunkSize: Optional[int] = None
    overlap: Optional[int] = None

@app.post("/util/chunk_source")
def chunk_source(inp: ChunkSourceIn):
    src = inp.source or ""
    size = int(inp.chunkSize or DEFAULT_CHUNK_SIZE)
    size = max(1000, min(size, 200000))
    overlap = int(inp.overlap or 0)
    overlap = max(0, min(overlap, max(0, size - 1)))

    chunks = []
    i = 0
    while i < len(src):
        end = min(len(src), i + size)
        chunks.append(src[i:end])
        if end >= len(src):
            break
        i = max(0, end - overlap)

    return {"ok": True, "chunkSize": size, "overlap": overlap, "chunks": chunks, "count": len(chunks)}

class EditScriptTxIn(BaseModel):
    path: str
    source: str
    mode: Optional[str] = "replace"
    chunkSize: Optional[int] = None

@app.post("/util/edit_script_tx")
def edit_script_tx(inp: EditScriptTxIn):
    size = int(inp.chunkSize or DEFAULT_CHUNK_SIZE)
    size = max(1000, min(size, 200000))
    src = inp.source or ""
    chunks = [src[i:i + size] for i in range(0, len(src), size)]
    return {
        "ok": True,
        "chunkSize": size,
        "tx": {
            "protocolVersion": PROTOCOL_VERSION,
            "transactionId": f"TX_EDIT_{uuid.uuid4()}",
            "actions": [
                {
                    "type": "editScript",
                    "path": inp.path,
                    "mode": inp.mode or "replace",
                    "chunks": chunks,
                }
            ],
        },
    }

if __name__ == "__main__":
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="Run the local Persponify Studio AI server.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=3030)
    parser.add_argument("--reload", action="store_true", help="Enable auto-reload (dev only)")
    args = parser.parse_args()

    try:
        import uvicorn
    except Exception as exc:
        print("Missing dependency: uvicorn. Install with `python -m pip install uvicorn`.", file=sys.stderr)
        raise

    uvicorn.run("app:app", host=args.host, port=args.port, reload=args.reload)
