# memory_tencentdb (general plugin)

Integrates the memory-tencentdb four-layer backend into Hermes' **turn pipeline**
for every surface — CLI, TUI, gateway, ACP.

| Hook | What it does |
|---|---|
| `on_session_start` | Ensures the three daemons are up (Gateway 8420, embed 8421, llm-proxy 8422) |
| `pre_llm_call` | Queries L1 + L3 + L2 in parallel and returns context injected into the **current user message** |
| `post_llm_call` | Normalizes this turn's transcript to user/assistant rows and `POST /v3/conversation/add` (L0) in a background thread |

Only user/assistant text is sent: the Gateway schema rejects system/tool rows,
and tool output was never conversation. Turns are sent as a delta anchored on
the turn's original user message, so L0 never duplicates earlier turns.

## Runtime

Everything lives in `<process hermes home>/memory-tencentdb/` — the sidecars are
machine-wide daemons, not per-profile resources:

```bash
hermes memory-tencentdb status
hermes memory-tencentdb start
hermes memory-tencentdb stop
hermes memory-tencentdb restart
```

The sidecars are daemons: session teardown never stops them. `supervise.sh`
remains the only process owner; the plugin just checks health and invokes it
under an advisory lock.

When the systemd user unit `memory-tencentdb.service` is installed
(`ops/memory-tencentdb/memory-tencentdb.service`), start/stop/restart go
through `systemctl --user` instead. That matters: the gateway's stop path runs
a cgroup cleanup, so sidecars spawned as plain children of the gateway die on
every `hermes gateway restart`, and the first turn afterwards is lost to
`Connection refused` while they are restarted lazily. The unit gives them their
own cgroup. Without the unit the plugin falls back to `supervise.sh` directly.

## Enable

General plugins are opt-in:

```bash
hermes plugins enable memory_tencentdb
hermes config unset memory.provider   # the old provider integration is retired
```

## Tools

- `memory_tencentdb_memory_search`
- `memory_tencentdb_conversation_search`
- `memory_tencentdb_read_scene`

Tenancy: `team_id`/`agent_id` are `default`; `user_id` comes from the session's
platform sender (falling back to `default`) and is used for both writes and
recall, because the Gateway's isolation check is exact equality.

## Known limits

- `codex_app_server` turns bypass the turn finalizer (`pre/post_llm_call` never
  fire), so those turns are neither captured nor recalled. Chat-completions
  turns (CLI, TUI, gateway, ACP) are covered.
- Interrupted and empty-response turns do not reach `post_llm_call` (core gates
  the hook on a final response), so they are not written to L0.
- Tenancy has no profile dimension: the same platform `user_id` shares L0–L3
  across Hermes profiles.
- Sidecars are POSIX daemons (`bash` + `fcntl`); on Windows the plugin imports
  and its tools report the Gateway as unreachable instead of starting it.

## Provenance

`client.py` is adapted from `TencentDB-Agent-Memory/MemoryCore/hermes-plugin/memory/memory_tencentdb/client.py`.
That checkout is not modified; keep the v3 request shapes in sync if the
Gateway contract changes.
