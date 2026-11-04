"""HTTP client for the memory-tencentdb Gateway sidecar.

Adapted from the upstream provider's ``client.py`` (TencentDB-Agent-Memory,
MemoryCore/hermes-plugin/memory/memory_tencentdb). Only the v3 data-plane
methods the pipeline integration uses are kept; the Gateway contract is shared,
so both copies must move together.

Thread-safe — one instance can serve recall, capture and tool dispatch.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8420
DEFAULT_TIMEOUT = 5  # seconds


def resolve_port() -> int:
    """Resolve ``MEMORY_TENCENTDB_GATEWAY_PORT`` with validation."""
    raw = os.environ.get("MEMORY_TENCENTDB_GATEWAY_PORT")
    if not raw:
        return DEFAULT_PORT
    try:
        port = int(raw)
    except ValueError:
        logger.warning("Invalid MEMORY_TENCENTDB_GATEWAY_PORT=%r (not an integer); using %d",
                       raw, DEFAULT_PORT)
        return DEFAULT_PORT
    if not 1 <= port <= 65535:
        logger.warning("MEMORY_TENCENTDB_GATEWAY_PORT=%d is out of range; using %d",
                       port, DEFAULT_PORT)
        return DEFAULT_PORT
    return port


class MemoryTencentdbClient:
    """Thin v3 client for the local Node Gateway."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        timeout: int = DEFAULT_TIMEOUT,
        api_key: Optional[str] = None,
        service_id: str = "default",
    ):
        host = os.environ.get("MEMORY_TENCENTDB_GATEWAY_HOST", DEFAULT_HOST)
        self._base_url = (base_url or f"http://{host}:{resolve_port()}").rstrip("/")
        self._timeout = timeout
        self._api_key = (api_key or os.environ.get("TDAI_API_KEY", "")).strip() or None
        self._service_id = (service_id or "default").strip()

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def timeout(self) -> int:
        return self._timeout

    def _headers(self, *, content_type: bool) -> Dict[str, str]:
        headers: Dict[str, str] = {}
        if content_type:
            headers["Content-Type"] = "application/json"
        # A local Gateway with auth disabled ignores the value but still parses it.
        headers["Authorization"] = f"Bearer {self._api_key or 'local'}"
        headers["x-tdai-service-id"] = self._service_id
        return headers

    def _post(self, path: str, body: Dict[str, Any], timeout: Optional[float] = None) -> Dict[str, Any]:
        req = urllib.request.Request(
            f"{self._base_url}{path}",
            data=json.dumps(body).encode("utf-8"),
            headers=self._headers(content_type=True),
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout or self._timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            logger.warning("memory-tencentdb %s returned %d: %s", path, exc.code, detail[:400])
            raise
        except Exception as exc:
            logger.debug("memory-tencentdb %s failed: %s", path, exc)
            raise

    def _get(self, path: str, timeout: Optional[float] = None) -> Dict[str, Any]:
        req = urllib.request.Request(
            f"{self._base_url}{path}",
            headers=self._headers(content_type=False),
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout or self._timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            logger.debug("memory-tencentdb GET %s failed: %s", path, exc)
            raise

    @staticmethod
    def _data(raw: Dict[str, Any]) -> Dict[str, Any]:
        """Return the ``data`` object of a v3 envelope (``{}`` on anything else)."""
        if isinstance(raw, dict) and raw.get("code", 0) in (0, None):
            data = raw.get("data")
            return data if isinstance(data, dict) else {}
        if isinstance(raw, dict) and isinstance(raw.get("data"), dict):
            return raw["data"]
        return {}

    # -- health ---------------------------------------------------------------

    def health(self, timeout: float = 1.0) -> bool:
        try:
            self._get("/health", timeout=timeout)
            return True
        except Exception:
            return False

    # -- L0 -------------------------------------------------------------------

    def conversation_add(
        self,
        messages: List[Dict[str, Any]],
        *,
        session_id: str = "",
        team_id: str = "default",
        agent_id: str = "default",
        user_id: str = "default",
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "team_id": team_id,
            "agent_id": agent_id,
            "user_id": user_id,
            "messages": messages,
        }
        if session_id:
            # The schema requires min(1); an omitted field gets the platform's
            # default bucket, so never send "".
            body["session_id"] = session_id
        return self._post("/v3/conversation/add", body)

    def conversation_search(
        self,
        query: str,
        *,
        limit: int = 5,
        session_id: str = "",
        team_id: str = "default",
        agent_id: str = "default",
        user_id: str = "default",
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "team_id": team_id,
            "agent_id": agent_id,
            "user_id": user_id,
            "query": query,
            "limit": limit,
        }
        if session_id:
            body["session_id"] = session_id
        return self._post("/v3/conversation/search", body)

    # -- L1 / L2 / L3 ---------------------------------------------------------

    def atomic_search(
        self,
        query: str,
        *,
        limit: int = 5,
        type_filter: str = "",
        team_id: str = "default",
        agent_id: str = "default",
        user_id: str = "default",
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "team_id": team_id,
            "agent_id": agent_id,
            "user_id": user_id,
            "query": query,
            "limit": limit,
        }
        if type_filter:
            body["type"] = type_filter
        return self._post("/v3/atomic/search", body, timeout=timeout)

    def scenario_ls(
        self,
        *,
        team_id: str = "default",
        agent_id: str = "default",
        user_id: str = "default",
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        return self._post("/v3/scenario/ls", {
            "team_id": team_id, "agent_id": agent_id, "user_id": user_id,
        }, timeout=timeout)

    def scenario_read(
        self,
        path: str,
        *,
        team_id: str = "default",
        agent_id: str = "default",
        user_id: str = "default",
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        return self._post("/v3/scenario/read", {
            "team_id": team_id, "agent_id": agent_id, "user_id": user_id, "path": path,
        }, timeout=timeout)

    def core_read(
        self,
        *,
        team_id: str = "default",
        agent_id: str = "default",
        user_id: str = "default",
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        return self._post("/v3/core/read", {
            "team_id": team_id, "agent_id": agent_id, "user_id": user_id,
        }, timeout=timeout)


_CLIENT: Optional[MemoryTencentdbClient] = None
_CLIENT_LOCK = threading.Lock()


def get_client() -> MemoryTencentdbClient:
    """Process-wide client shared by hooks and tool handlers."""
    global _CLIENT
    with _CLIENT_LOCK:
        if _CLIENT is None:
            _CLIENT = MemoryTencentdbClient()
        return _CLIENT
