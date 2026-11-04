"""memory-tencentdb integrated into the Hermes message pipeline.

General plugin (not a memory provider): it rides the turn hooks that already
carry the real transcript, so every surface — CLI, TUI, gateway, ACP — captures
L0 and recalls L1/L2/L3 without a per-provider opt-in.

* ``on_session_start``  → ensure the three sidecars are running (daemon lifetime)
* ``pre_llm_call``      → recall, injected into the current user message
* ``post_llm_call``     → capture this turn's transcript into L0
* tools + ``hermes memory-tencentdb ...``

The upstream TencentDB-Agent-Memory checkout stays read-only; this plugin owns
the Hermes-side integration.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, Optional

from . import capture, recall, runtime, state, tools

logger = logging.getLogger(__name__)

_SKIP_PLATFORMS = {"cron", "subagent"}

_health_lock = threading.Lock()
_healthy = False
_next_probe = 0.0
_next_ensure = 0.0


def _gateway_ready() -> bool:
    """Cached health probe: a dead sidecar must not add latency to every turn."""
    global _healthy, _next_probe, _next_ensure
    now = time.monotonic()
    with _health_lock:
        if now < _next_probe:
            return _healthy

    from .client import get_client

    ok = get_client().health(timeout=0.5)
    with _health_lock:
        _healthy = ok
        _next_probe = now + (15.0 if ok else 30.0)
        if not ok and now >= _next_ensure:
            _next_ensure = now + 60.0
            start = True
        else:
            start = False
    if start:
        runtime.ensure_async()
    return ok


def _on_session_start(session_id: str = "", platform: str = "", **_kwargs: Any) -> None:
    if platform in _SKIP_PLATFORMS:
        return
    runtime.ensure_async()


def _on_pre_llm_call(
    user_message: Any = "",
    session_id: str = "",
    parent_session_id: str = "",
    sender_id: str = "",
    platform: str = "",
    **_kwargs: Any,
) -> Optional[Dict[str, str]]:
    if platform in _SKIP_PLATFORMS or parent_session_id:
        if session_id:
            state.mark_subagent(session_id)
        return None

    state.remember_identity(session_id, sender_id)
    if not _gateway_ready():
        return None

    query = capture.flatten_text(user_message)
    if not query:
        return None

    from .client import get_client

    context = recall.build_context(
        get_client(), query, user_id=state.user_id_for(session_id),
    )
    return {"context": context} if context else None


def _on_post_llm_call(
    session_id: str = "",
    user_message: Any = "",
    assistant_response: Any = "",
    conversation_history: Any = None,
    platform: str = "",
    **_kwargs: Any,
) -> None:
    if platform in _SKIP_PLATFORMS or state.is_subagent(session_id):
        return
    if not session_id:
        return

    from agent.memory_provider import spawn_context_thread
    from .client import get_client

    thread = spawn_context_thread(
        capture.capture_turn,
        name="memory-tencentdb-capture",
        kwargs={
            "client": get_client(),
            "messages": conversation_history,
            "user_message": user_message,
            "assistant_response": assistant_response,
            "session_id": session_id,
            "user_id": state.user_id_for(session_id),
        },
    )
    thread.start()


def register(ctx) -> None:
    ctx.register_hook("on_session_start", _on_session_start)
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
    ctx.register_hook("post_llm_call", _on_post_llm_call)

    for name, schema, handler, emoji in (
        (tools.MEMORY_SEARCH_SCHEMA["name"], tools.MEMORY_SEARCH_SCHEMA,
         tools.handle_memory_search, "🧠"),
        (tools.CONVERSATION_SEARCH_SCHEMA["name"], tools.CONVERSATION_SEARCH_SCHEMA,
         tools.handle_conversation_search, "💬"),
        (tools.READ_SCENE_SCHEMA["name"], tools.READ_SCENE_SCHEMA,
         tools.handle_read_scene, "📖"),
    ):
        ctx.register_tool(name=name, toolset="memory", schema=schema, handler=handler, emoji=emoji)

    from .cli import memory_tencentdb_command, register_cli

    ctx.register_cli_command(
        name="memory-tencentdb",
        help="Manage the memory-tencentdb sidecars (start/stop/restart/status)",
        setup_fn=register_cli,
        handler_fn=memory_tencentdb_command,
        description=(
            "Four-layer memory backend (L0 conversation → L1 memories → L2 scenes → "
            "L3 persona) used automatically by every Hermes session. This command "
            "manages the local Gateway, embedding service and LLM proxy."
        ),
    )
