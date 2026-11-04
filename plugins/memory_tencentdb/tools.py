"""Agent-facing memory tools (same names as the retired provider).

Session-scoped tenancy is resolved from the ``session_id`` dispatch kwarg and
the identity cache filled by ``pre_llm_call``.
"""

from __future__ import annotations

import json
from typing import Any, Dict

from . import runtime, state
from .client import get_client


def _coerce_limit(value: Any) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError):
        return 5
    return max(1, min(20, limit))


def _ids(session_id: str) -> Dict[str, str]:
    return {
        "team_id": "default",
        "agent_id": "default",
        "user_id": state.user_id_for(session_id),
    }


def _unavailable() -> str:
    return json.dumps({
        "error": "memory-tencentdb Gateway is not reachable.",
        "hint": "Run `hermes memory-tencentdb start` and retry.",
    })


MEMORY_SEARCH_SCHEMA = {
    "name": "memory_tencentdb_memory_search",
    "description": (
        "Search through the user's long-term memories. Use this when you need to "
        "recall specific information about the user's preferences, past events, "
        "instructions, or context from previous conversations. Returns relevant "
        "memory records ranked by relevance."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query describing what you want to recall about the user.",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum number of results to return (default: 5, max: 20).",
            },
            "type": {
                "type": "string",
                "enum": ["persona", "episodic", "instruction"],
                "description": "Optional filter by memory type.",
            },
        },
        "required": ["query"],
    },
}

CONVERSATION_SEARCH_SCHEMA = {
    "name": "memory_tencentdb_conversation_search",
    "description": (
        "Search through past conversation history (raw dialogue records). "
        "Use when memory_tencentdb_memory_search doesn't have the information "
        "you need, or when you want to find specific past conversations or "
        "exact words the user said before."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query describing what conversation content you want to find.",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum number of messages to return (default: 5, max: 20).",
            },
        },
        "required": ["query"],
    },
}

READ_SCENE_SCHEMA = {
    "name": "memory_tencentdb_read_scene",
    "description": (
        "Read a scene block's full content by its name. "
        "Use when you see a scene listed in the available scenes and want to "
        "retrieve detailed information from that scene."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "scene_id": {
                "type": "string",
                "description": "Scene file name (e.g. 'travel-plan.md' or 'travel-plan').",
            },
        },
        "required": ["scene_id"],
    },
}


def handle_memory_search(args: Dict[str, Any], *, session_id: str = "", **_kw) -> str:
    query = (args.get("query") or "").strip()
    if not query:
        return json.dumps({"error": "Missing required parameter: query"})
    client = get_client()
    if not client.health(timeout=0.5) and not runtime.ensure_started(wait=5):
        return _unavailable()
    try:
        result = client.atomic_search(
            query=query,
            limit=_coerce_limit(args.get("limit")),
            type_filter=args.get("type", ""),
            **_ids(session_id),
        )
    except Exception as exc:
        return json.dumps({"error": f"Memory search failed: {exc}"})
    items = client._data(result).get("items") or []
    if not items:
        return "No memories found for this query."
    return "\n".join(
        f"- [{item.get('type', '?')}] {item.get('content', '')}"
        for item in items if isinstance(item, dict)
    )


def handle_conversation_search(args: Dict[str, Any], *, session_id: str = "", **_kw) -> str:
    query = (args.get("query") or "").strip()
    if not query:
        return json.dumps({"error": "Missing required parameter: query"})
    client = get_client()
    if not client.health(timeout=0.5) and not runtime.ensure_started(wait=5):
        return _unavailable()
    try:
        # No session_id: this tool is for cross-session recall; passing it would
        # narrow the Gateway search to the current session.
        result = client.conversation_search(
            query=query,
            limit=_coerce_limit(args.get("limit")),
            **_ids(session_id),
        )
    except Exception as exc:
        return json.dumps({"error": f"Conversation search failed: {exc}"})
    items = client._data(result).get("messages") or []
    if not items:
        return "No conversations found for this query."
    return "\n".join(
        f"[{item.get('role', '?')}] {item.get('content', '')}"
        for item in items if isinstance(item, dict)
    )


def handle_read_scene(args: Dict[str, Any], *, session_id: str = "", **_kw) -> str:
    scene_id = (args.get("scene_id") or "").strip()
    if not scene_id:
        return "Error: scene_id is required"
    client = get_client()
    if not client.health(timeout=0.5) and not runtime.ensure_started(wait=5):
        return _unavailable()
    path = scene_id if scene_id.endswith(".md") else f"{scene_id}.md"
    try:
        result = client.scenario_read(path=path, **_ids(session_id))
    except Exception as exc:
        return json.dumps({"error": f"Scene read failed: {exc}"})
    content = client._data(result).get("content") or ""
    return content or f"Scene '{scene_id}' is empty or not found."
