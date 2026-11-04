"""Turn capture: Hermes transcript → memory-tencentdb L0.

The Node Gateway accepts only ``user``/``assistant`` rows (1..8192 chars,
at most 100 per request). Hermes transcripts also carry ``system``, ``tool``
rows, multimodal content parts and reasoning; those are not L0 conversation and
are dropped here.

Only the **current turn** is sent, anchored on the turn's original user message.
Earlier turns were captured by their own ``post_llm_call``; replaying the whole
history would duplicate L0 rows (and re-trigger L1 extraction).
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

MAX_MESSAGES = 100
MAX_CONTENT = 8000  # Gateway rejects >8192; leave headroom for the continuation marker


def flatten_text(content: Any) -> str:
    """Best-effort text of an OpenAI content value (str, parts list, or dict)."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, (list, tuple)):
        parts: List[str] = []
        for part in content:
            if isinstance(part, str):
                text = part
            elif isinstance(part, dict):
                text = part.get("text")
                if text is None:
                    text = part.get("content")
                if not isinstance(text, str):
                    text = ""
            else:
                text = ""
            if text.strip():
                parts.append(text.strip())
        return "\n".join(parts).strip()
    if isinstance(content, dict):
        return flatten_text(content.get("text") or content.get("content") or "")
    return ""


def _iso_timestamp(value: Any) -> Optional[str]:
    """Gateway-safe ISO-8601: ``z.iso.datetime()`` rejects ``+00:00`` offsets."""
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str) and value.strip():
        raw = value.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return None


_INJECTION_RE = re.compile(
    r"<(relevant-memories|user-core|scene-navigation)>.*?</\1>\s*",
    re.DOTALL | re.IGNORECASE,
)


def _strip_injections(text: str) -> str:
    """Remove the recall blocks ``pre_llm_call`` appended to the live user row."""
    return _INJECTION_RE.sub("", text).strip()


def _chunks(text: str, limit: int = MAX_CONTENT) -> Iterable[str]:
    """Split *text* on paragraph boundaries, then hard-split oversized blocks."""
    remaining = text
    while len(remaining) > limit:
        cut = remaining.rfind("\n\n", 0, limit)
        if cut < limit // 2:
            cut = remaining.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        yield remaining[:cut].strip()
        remaining = remaining[cut:].lstrip()
    if remaining.strip():
        yield remaining.strip()


def _anchor_index(messages: List[Dict[str, Any]], user_message: str) -> Tuple[Optional[int], bool]:
    """Index of this turn's user row and whether it is an exact text match.

    With no user text there is no turn to anchor on — returning the previous
    turn's user row would replay history into L0. A non-exact anchor (voice
    prefix, mid-turn redirect) is kept verbatim instead of overwritten, because
    the row's own text is real conversation.
    """
    if not user_message:
        return None, False
    for idx in range(len(messages) - 1, -1, -1):
        msg = messages[idx]
        if msg.get("role") == "user" and flatten_text(msg.get("content")) == user_message:
            return idx, True
    for idx in range(len(messages) - 1, -1, -1):
        if messages[idx].get("role") == "user":
            return idx, False
    return None, False


def build_turn_messages(
    messages: Optional[List[Dict[str, Any]]],
    user_message: Any,
    assistant_response: Any = "",
) -> List[Dict[str, Any]]:
    """Normalize the final transcript of one turn into Gateway-safe rows."""
    messages = messages or []
    clean_user = flatten_text(user_message)
    anchor, exact = _anchor_index(messages, clean_user)
    if anchor is None:
        # No transcript to anchor on (e.g. an empty/interrupted turn): still
        # record the user ask and the final answer when they exist.
        rows: List[Dict[str, Any]] = []
        if clean_user:
            rows.append({"role": "user", "content": clean_user})
        answer = flatten_text(assistant_response)
        if answer:
            rows.append({"role": "assistant", "content": answer})
        return _finalize(rows)

    entries = []
    for pos, msg in enumerate(messages[anchor:]):
        role = msg.get("role")
        if role not in ("user", "assistant"):
            continue
        text = flatten_text(msg.get("content"))
        if pos == 0 and role == "user":
            # Exact anchor: store the ask as the user wrote it, without the
            # recall block. Fallback anchor: keep the row's own text (voice
            # prefix / redirect) and only strip our injected blocks.
            text = clean_user if exact else _strip_injections(text)
        if not text:
            continue
        if entries and entries[-1][0] == role and entries[-1][1] == text:
            continue  # identical consecutive rows add nothing to L0
        raw_ts = msg.get("timestamp")
        if raw_ts is None:
            raw_ts = msg.get("ts")
        entries.append((role, text, _iso_timestamp(raw_ts)))

    # Abnormal exits / redirects can finalize before the final answer reaches
    # the transcript; only append when the transcript does not already end on it.
    answer = flatten_text(assistant_response)
    if answer and (not entries or entries[-1][0] != "assistant" or entries[-1][1] != answer):
        entries.append(("assistant", answer, None))

    rows: List[Dict[str, Any]] = []
    for role, text, timestamp in entries:
        for chunk in _chunks(text):
            row: Dict[str, Any] = {"role": role, "content": chunk}
            if timestamp:
                row["timestamp"] = timestamp
            rows.append(row)
    return _finalize(rows)


def _finalize(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if len(rows) > MAX_MESSAGES:
        head = [rows[0]] if rows[0]["role"] == "user" else []
        rows = head + rows[-(MAX_MESSAGES - len(head)):]
    return rows


def transcript_signature(rows: List[Dict[str, Any]]) -> str:
    """Stable hash for diagnostics/tests (not used for dedup across turns)."""
    digest = hashlib.sha256()
    for row in rows:
        digest.update(row["role"].encode("utf-8"))
        digest.update(b"\0")
        digest.update(row["content"].encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()[:16]


def capture_turn(
    client: Any,
    *,
    messages: Optional[List[Dict[str, Any]]],
    user_message: Any,
    assistant_response: Any,
    session_id: str,
    user_id: str = "default",
    team_id: str = "default",
    agent_id: str = "default",
) -> int:
    """Build and POST one turn to L0. Returns the number of rows sent."""
    rows = build_turn_messages(messages, user_message, assistant_response)
    if not rows:
        return 0
    try:
        response = client.conversation_add(
            messages=rows,
            session_id=session_id or "",
            team_id=team_id,
            agent_id=agent_id,
            user_id=user_id or "default",
        )
        code = response.get("code") if isinstance(response, dict) else None
        if code not in (None, 0):
            logger.warning("memory-tencentdb L0 write rejected (code=%s): %s",
                           code, response.get("message") if isinstance(response, dict) else "")
    except Exception as exc:
        logger.warning("memory-tencentdb L0 write failed for session %s: %s", session_id, exc)
        return 0
    return len(rows)
