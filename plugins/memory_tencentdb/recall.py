"""Per-turn recall from L1/L3/L2, injected into the current user message.

`pre_llm_call` runs once at turn start, before the first API call. The returned
text is appended to the *current* user message by core, never to the system
prompt, so the cached prefix stays byte-stable.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from typing import Any, Dict, List, Tuple

logger = logging.getLogger(__name__)

MAX_CONTEXT_CHARS = 4000
DEFAULT_TIMEOUT = 3.0
# Bound each HTTP call below the overall budget so a slow layer's worker thread
# cannot outlive the turn (cancel_futures does not interrupt an in-flight urlopen).
CALL_TIMEOUT = 2.5
# L3 persona / L2 scene index change on the pipeline's wall clock, not per turn.
# Caching them keeps recall to one HTTP round-trip in the steady state.
_SLOW_LAYER_TTL = 300.0
_SLOW_LAYER_MAX = 64
_slow_cache: Dict[Tuple[str, str, str], Tuple[float, Dict[str, Any]]] = {}
_cache_lock = threading.Lock()


def _profile_key() -> str:
    try:
        from hermes_constants import get_hermes_home

        return str(get_hermes_home())
    except Exception:
        return ""


def clear_cache() -> None:
    with _cache_lock:
        _slow_cache.clear()


def _cached(layer: str, user_id: str, fetch) -> Dict[str, Any]:
    key = (_profile_key(), user_id or "default", layer)
    now = time.monotonic()
    with _cache_lock:
        hit = _slow_cache.get(key)
        if hit and now - hit[0] < _SLOW_LAYER_TTL:
            return hit[1]
    try:
        value = fetch()
    except Exception:
        raise
    with _cache_lock:
        _slow_cache[key] = (now, value)
        while len(_slow_cache) > _SLOW_LAYER_MAX:
            _slow_cache.pop(next(iter(_slow_cache)))
    return value


def is_trivial(query: str) -> bool:
    try:
        from agent.memory_provider import is_trivial_prompt

        return is_trivial_prompt(query)
    except Exception:
        return False


def _l1_block(data: Dict[str, Any]) -> str:
    items = data.get("items") or []
    lines: List[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        content = str(item.get("content") or "").strip()
        if not content:
            continue
        lines.append(f"- [{item.get('type', 'unknown')}] {content}")
    if not lines:
        return ""
    return (
        "<relevant-memories>\n"
        "以下是当前对话召回的相关记忆，仅作为参考：\n\n"
        + "\n".join(lines)
        + "\n</relevant-memories>"
    )


def _l3_block(data: Dict[str, Any]) -> str:
    core = str(data.get("content") or "").strip()
    return f"<user-core>\n{core}\n</user-core>" if core else ""


def _l2_block(data: Dict[str, Any]) -> str:
    entries = data.get("entries") or []
    lines = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        path = str(entry.get("path") or "").strip()
        if path:
            lines.append(f"- Scene: {path.removesuffix('.md')}")
    if not lines:
        return ""
    return (
        "<scene-navigation>\n"
        "Available scenes:\n"
        + "\n".join(lines)
        + "\n</scene-navigation>"
    )


def build_context(
    client: Any,
    query: str,
    *,
    user_id: str = "default",
    team_id: str = "default",
    agent_id: str = "default",
    timeout: float = DEFAULT_TIMEOUT,
) -> str:
    """Fetch the three layers in parallel and render the injected block."""
    query = (query or "").strip()
    if not query or is_trivial(query):
        return ""

    calls = {
        "l1": lambda: client.atomic_search(
            query=query, limit=5,
            team_id=team_id, agent_id=agent_id, user_id=user_id, timeout=CALL_TIMEOUT,
        ),
        "l3": lambda: _cached("l3", user_id, lambda: client.core_read(
            team_id=team_id, agent_id=agent_id, user_id=user_id, timeout=CALL_TIMEOUT,
        )),
        "l2": lambda: _cached("l2", user_id, lambda: client.scenario_ls(
            team_id=team_id, agent_id=agent_id, user_id=user_id, timeout=CALL_TIMEOUT,
        )),
    }
    results: Dict[str, Dict[str, Any]] = {}
    executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="memory-tencentdb-recall")
    futures = {}
    try:
        futures = {name: executor.submit(fn) for name, fn in calls.items()}
        done, _pending = wait(futures.values(), timeout=timeout)
        for name, future in futures.items():
            if future not in done:
                logger.debug("memory-tencentdb recall %s timed out", name)
                continue
            try:
                results[name] = future.result()
            except Exception as exc:
                logger.debug("memory-tencentdb recall %s failed: %s", name, exc)
    finally:
        for future in futures.values():
            future.cancel()
        executor.shutdown(wait=False, cancel_futures=True)

    blocks = [
        _l1_block(client._data(results.get("l1", {}))),
        _l3_block(client._data(results.get("l3", {}))),
        _l2_block(client._data(results.get("l2", {}))),
    ]
    context = "\n\n".join(block for block in blocks if block)
    if len(context) > MAX_CONTEXT_CHARS:
        context = context[:MAX_CONTEXT_CHARS].rstrip() + "\n…"
    return context
