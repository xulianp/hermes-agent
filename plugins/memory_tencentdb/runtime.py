"""Sidecar lifecycle for memory-tencentdb.

Three long-lived processes back the memory service:

* Gateway   — 127.0.0.1:8420 (Node, L0→L3 pipeline)
* local-embed — 127.0.0.1:8421 (bge-small-zh-v1.5, OpenAI-compatible embeddings)
* llm-proxy — 127.0.0.1:8422 (MiniMax thinking/status normalization)

They are **daemons**, not per-session resources: every Hermes surface (CLI, TUI,
gateway, ACP) ensures they are up, and nothing stops them on session teardown.
`supervise.sh` in the runtime directory remains the single owner of start/stop so
the plugin and manual operations cannot disagree.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Dict, Optional

try:
    import fcntl
except ImportError:  # Windows: the bash-based sidecars are unavailable anyway
    fcntl = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

GATEWAY_PORT = 8420
EMBED_PORT = 8421
LLM_PROXY_PORT = 8422

_PORTS = {"gateway": GATEWAY_PORT, "embed": EMBED_PORT, "llm-proxy": LLM_PROXY_PORT}

SYSTEMD_UNIT = "memory-tencentdb.service"


def runtime_dir() -> Path:
    """Runtime directory of the process's launch home (``<home>/memory-tencentdb``).

    The sidecars are machine-wide daemons with fixed ports and a data dir under
    ``~/.memory-tencentdb``, so this must be the process home — not the
    per-request profile override of a multiplexed gateway, which has no runtime
    copy. A named-profile process still uses the already-running daemons; only
    auto-start needs the launch home to be the one that owns them.
    """
    from hermes_constants import get_process_hermes_home

    return get_process_hermes_home() / "memory-tencentdb"


def _healthy(port: int, timeout: float = 1.0) -> bool:
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/health", timeout=timeout
        ) as resp:
            return 200 <= int(getattr(resp, "status", 200)) < 300
    except Exception:
        return False


def status() -> Dict[str, bool]:
    return {name: _healthy(port) for name, port in _PORTS.items()}


def all_healthy() -> bool:
    return all(status().values())


def _wait_healthy(timeout: float) -> bool:
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        if all_healthy():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.5)


def user_unit_path() -> Path:
    """Installed systemd user unit for the sidecars (respects ``XDG_CONFIG_HOME``)."""
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    return root / "systemd" / "user" / SYSTEMD_UNIT


def _systemd_available() -> bool:
    """True when the unit is installed and a working user manager is reachable.

    The unit gives the sidecars their own cgroup. Started as plain children of
    the gateway they would be reaped by its ExecStopPost cgroup cleanup on every
    ``hermes gateway restart``, and the next turn would race the lazy restart.
    """
    return (
        sys.platform.startswith("linux")
        and shutil.which("systemctl") is not None
        and user_unit_path().is_file()
    )


def _systemctl(action: str, timeout: float = 120.0) -> bool:
    try:
        done = subprocess.run(
            ["systemctl", "--user", action, SYSTEMD_UNIT],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("memory-tencentdb systemctl %s failed: %s", action, exc)
        return False
    if done.returncode != 0:
        output = (done.stdout or b"").decode("utf-8", "replace").strip()
        logger.warning("memory-tencentdb systemctl %s failed (%s): %s",
                       action, done.returncode, output)
        return False
    return True


class _StartLock:
    """Non-blocking advisory lock so concurrent sessions spawn one supervisor."""

    def __init__(self, path: Path):
        self._path = path
        self._fh = None

    def acquire(self) -> bool:
        if fcntl is None:
            return True  # no advisory lock on this platform; supervise.sh's port checks still apply
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = open(self._path, "a+")
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except (OSError, BlockingIOError):
            self.release()
            return False

    def release(self) -> None:
        if self._fh is not None:
            try:
                if fcntl is not None:
                    fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None


def ensure_started(wait: float = 0.0) -> bool:
    """Ensure all three sidecars are running.

    Returns True when they are healthy. ``wait=0`` means "spawn and return";
    callers that need the service immediately (CLI ``start``) pass a timeout.
    Fail-open: a missing runtime or spawn failure returns False without raising.
    """
    if all_healthy():
        return True

    root = runtime_dir()
    script = root / "supervise.sh"
    if not script.is_file():
        logger.warning("memory-tencentdb runtime missing: %s (expected supervise.sh)", root)
        return False

    lock = _StartLock(root / "pids" / "plugin-supervise.lock")
    if not lock.acquire():
        # Another process is already starting the stack.
        return _wait_healthy(wait) if wait > 0 else False

    try:
        if all_healthy():
            return True
        if _systemd_available():
            # Start through the unit so the daemons land in their own cgroup.
            logger.info("memory-tencentdb sidecars starting via %s", SYSTEMD_UNIT)
            if _systemctl("start"):
                return _wait_healthy(wait) if wait > 0 else True
            logger.warning("memory-tencentdb falling back to a direct supervise.sh start")
        log_path = root / "supervise.log"
        with open(log_path, "ab", buffering=0) as log:
            subprocess.Popen(
                ["bash", str(script), "start"],
                cwd=str(root),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
            )
        logger.info("memory-tencentdb sidecars starting (log: %s)", log_path)
        return _wait_healthy(wait) if wait > 0 else True
    except Exception as exc:
        logger.warning("memory-tencentdb sidecar start failed: %s", exc)
        return False
    finally:
        lock.release()


_ASYNC_INTERVAL = 60.0
_async_lock = threading.Lock()
_next_async_attempt = 0.0


def ensure_async() -> None:
    """Fire-and-forget start, coalesced across sessions.

    Without the gate, every session start while the stack is down would leave a
    worker waiting for health. One attempt per minute is enough for a daemon.
    """
    global _next_async_attempt
    now = time.monotonic()
    with _async_lock:
        if now < _next_async_attempt:
            return
        _next_async_attempt = now + _ASYNC_INTERVAL

    from agent.memory_provider import spawn_context_thread

    thread = spawn_context_thread(
        ensure_started, name="memory-tencentdb-sidecar", kwargs={"wait": 30.0}
    )
    thread.start()


def run_action(action: str, wait: float = 90.0) -> int:
    """Run ``start|stop|restart|status`` against the runtime supervise script."""
    if action == "start":
        return 0 if ensure_started(wait=wait) else 1
    if action not in {"stop", "restart", "status"}:
        raise ValueError(f"unknown action: {action}")
    if action in {"stop", "restart"} and _systemd_available():
        # Keep systemd's view of the unit in sync with the processes.
        if _systemctl(action, timeout=150.0):
            return 0
        logger.warning("memory-tencentdb systemctl %s failed; using supervise.sh", action)
    script = runtime_dir() / "supervise.sh"
    if not script.is_file():
        return 1
    try:
        return subprocess.call(["bash", str(script), action], cwd=str(script.parent))
    except Exception as exc:
        logger.warning("memory-tencentdb %s failed: %s", action, exc)
        return 1
