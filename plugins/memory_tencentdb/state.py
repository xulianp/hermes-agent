"""Per-session identity shared by hooks and tool handlers.

`pre_llm_call` carries ``sender_id`` (the platform user); `post_llm_call` and
tool dispatch carry only ``session_id``. Cache the mapping for the
session so reads and writes use the same tenancy key (``rowMatchesIsolation``
compares ``user_id`` for exact equality).
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Optional

_LOCK = threading.Lock()
_MAX_SESSIONS = 2048
_SENDERS: "OrderedDict[str, str]" = OrderedDict()
_SUBAGENT_SESSIONS: "OrderedDict[str, None]" = OrderedDict()


def _remember(mapping: "OrderedDict", key: str, value=None) -> None:
    with _LOCK:
        mapping[key] = value
        mapping.move_to_end(key)
        while len(mapping) > _MAX_SESSIONS:
            mapping.popitem(last=False)


def remember_identity(session_id: str, user_id: Optional[str]) -> None:
    if session_id and user_id:
        _remember(_SENDERS, session_id, str(user_id))
    elif session_id:
        with _LOCK:
            _SENDERS.setdefault(session_id, "default")


def user_id_for(session_id: str) -> str:
    with _LOCK:
        return _SENDERS.get(session_id or "", "default") or "default"


def mark_subagent(session_id: str) -> None:
    if session_id:
        _remember(_SUBAGENT_SESSIONS, session_id)


def is_subagent(session_id: str) -> bool:
    with _LOCK:
        return (session_id or "") in _SUBAGENT_SESSIONS
