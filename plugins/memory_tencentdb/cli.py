"""``hermes memory-tencentdb {start|stop|restart|status}``.

Replaces the per-skill operational instructions: the same commands now live in
the CLI and act on the profile-scoped runtime directory.
"""

from __future__ import annotations

import argparse
import sys

from . import runtime


def register_cli(subparser: argparse.ArgumentParser) -> None:
    subparser.add_argument(
        "action",
        nargs="?",
        default="status",
        choices=["start", "stop", "restart", "status"],
        help="sidecar action (default: status)",
    )
    subparser.add_argument(
        "--wait", type=float, default=90.0,
        help="seconds to wait for health after start (default: 90)",
    )


def memory_tencentdb_command(args: argparse.Namespace) -> int:
    action = getattr(args, "action", "status") or "status"
    wait = float(getattr(args, "wait", 90.0) or 0.0)

    if action == "status":
        return _print_status()

    rc = runtime.run_action(action, wait=wait)
    if rc != 0:
        print(f"  ❌ `{action}` failed (see {runtime.runtime_dir()}/supervise.log)",
              file=sys.stderr)
        return rc
    # systemctl swallows the supervisor's per-component lines; show them here so
    # `start`/`restart` still prove all three ports came back.
    if action in {"start", "restart"}:
        return _print_status()
    print(f"  ✅ memory-tencentdb {action} done")
    return rc


def _print_status() -> int:
    ports = {"gateway": runtime.GATEWAY_PORT,
             "embed": runtime.EMBED_PORT,
             "llm-proxy": runtime.LLM_PROXY_PORT}
    healthy = runtime.status()
    for name, port in ports.items():
        mark = "✅" if healthy.get(name) else "❌"
        print(f"  {mark} {name:<10} 127.0.0.1:{port}")
    print(f"  runtime: {runtime.runtime_dir()}")
    return 0 if all(healthy.values()) else 1
