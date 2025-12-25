from __future__ import annotations

from typing import Any, Callable, Dict, Optional


MCP_SERVER_INFO = {"name": "Persponify MCP", "version": "0.1.0"}


def tool_list() -> Dict[str, Any]:
    return {
        "tools": [
            {
                "name": "enqueue_actions",
                "description": "Enqueue Roblox Studio actions to the local Persponify server.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "actions": {
                            "type": "array",
                            "items": {"type": "object"},
                            "description": "Action list for the Studio plugin.",
                        },
                        "transactionId": {"type": "string"},
                    },
                    "required": ["actions"],
                },
            },
            {
                "name": "get_status",
                "description": "Fetch server/plugin status from /status.",
                "inputSchema": {"type": "object", "properties": {}},
            },
            {
                "name": "get_context_summary",
                "description": "Fetch the latest context summary from /context/summary.",
                "inputSchema": {"type": "object", "properties": {}},
            },
            {
                "name": "request_context_export",
                "description": "Ask the plugin to export context immediately, optionally scoped.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "projectKey": {"type": "string"},
                        "roots": {"type": "array", "items": {"type": "string"}},
                        "paths": {"type": "array", "items": {"type": "string"}},
                        "includeSources": {"type": "boolean"},
                        "mode": {"type": "string", "enum": ["full", "diff"]},
                    },
                },
            },
        ]
    }


def handle_request(
    req: Dict[str, Any],
    tool_call: Callable[[str, Dict[str, Any]], Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    req_id = req.get("id")
    method = req.get("method")
    params = req.get("params") or {}

    if method == "initialize":
        protocol = params.get("protocolVersion") or "2024-11-05"
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": protocol,
                "serverInfo": MCP_SERVER_INFO,
                "capabilities": {"tools": {"listChanged": False}},
            },
        }

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": tool_list()}

    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        result = tool_call(name, args)
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    if method == "resources/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"resources": []}}

    if method == "prompts/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"prompts": []}}

    if method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"ok": True}}

    if req_id is None:
        return None
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }
