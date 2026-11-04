"""Behaviour tests for the memory-tencentdb pipeline plugin."""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

import plugins.memory_tencentdb as plugin
from plugins.memory_tencentdb import capture, recall, runtime, tools
from plugins.memory_tencentdb import state


@pytest.fixture(autouse=True)
def _clear_recall_cache():
    recall.clear_cache()
    yield
    recall.clear_cache()


class _FakeClient:
    def __init__(self, *, items=None, core="", entries=None):
        self._items = items or []
        self._core = core
        self._entries = entries or []
        self.added: List[Dict[str, Any]] = []
        self.searches: List[str] = []

    def atomic_search(self, query: str, **kwargs):
        self.searches.append(query)
        return {"code": 0, "data": {"items": self._items}}

    def core_read(self, **kwargs):
        return {"code": 0, "data": {"content": self._core}}

    def scenario_ls(self, **kwargs):
        return {"code": 0, "data": {"entries": self._entries}}

    def conversation_add(self, messages, **kwargs):
        self.added.append({"messages": messages, **kwargs})
        return {"code": 0, "data": {}}

    def health(self, timeout=1.0):
        return True

    @staticmethod
    def _data(raw):
        return raw.get("data", {}) if isinstance(raw, dict) else {}


def test_build_turn_messages_drops_non_conversation_and_injection():
    messages = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "previous ask"},
        {"role": "assistant", "content": "previous answer"},
        {"role": "user", "content": "new ask\n\n<relevant-memories>noise</relevant-memories>"},
        {"role": "assistant", "content": "let me check"},
        {"role": "tool", "content": "tool output"},
        {"role": "assistant", "content": "final answer"},
    ]

    rows = capture.build_turn_messages(messages, "new ask")

    assert [row["role"] for row in rows] == ["user", "assistant", "assistant"]
    assert rows[0]["content"] == "new ask"
    assert all("relevant-memories" not in row["content"] for row in rows)
    assert all("tool output" not in row["content"] for row in rows)


def test_build_turn_messages_falls_back_when_user_message_is_empty():
    messages = [
        {"role": "user", "content": "earlier"},
        {"role": "assistant", "content": "earlier answer"},
    ]

    rows = capture.build_turn_messages(messages, "", "the answer")

    assert rows == [{"role": "assistant", "content": "the answer"}]


def test_final_response_fills_an_interrupted_transcript():
    rows = capture.build_turn_messages(
        [{"role": "user", "content": "ask"}], "ask", "final answer",
    )

    assert rows == [
        {"role": "user", "content": "ask"},
        {"role": "assistant", "content": "final answer"},
    ]


def test_long_content_is_split_under_gateway_limit():
    messages = [{"role": "user", "content": "x" * 20_000}]

    rows = capture.build_turn_messages(messages, "x" * 20_000)

    assert len(rows) >= 3
    assert all(len(row["content"]) <= capture.MAX_CONTENT for row in rows)
    assert "".join(row["content"] for row in rows) == "x" * 20_000


def test_row_cap_keeps_the_user_anchor():
    messages = [{"role": "user", "content": "ask"}]
    messages += [{"role": "assistant", "content": f"step {i}"} for i in range(150)]

    rows = capture.build_turn_messages(messages, "ask")

    assert len(rows) == capture.MAX_MESSAGES
    assert rows[0] == {"role": "user", "content": "ask"}


def test_capture_turn_posts_normalized_rows_with_tenancy():
    client = _FakeClient()
    messages = [{"role": "user", "content": "ask"}, {"role": "assistant", "content": "answer"}]

    sent = capture.capture_turn(
        client,
        messages=messages,
        user_message="ask",
        assistant_response="answer",
        session_id="s1",
        user_id="u1",
    )

    assert sent == 2
    assert client.added == [{
        "messages": [{"role": "user", "content": "ask"},
                     {"role": "assistant", "content": "answer"}],
        "session_id": "s1",
        "team_id": "default",
        "agent_id": "default",
        "user_id": "u1",
    }]


def test_timestamps_are_utc_with_z_suffix():
    # z.iso.datetime() rejects numeric offsets; the Gateway would 400 every row.
    assert capture._iso_timestamp(0.0) == "1970-01-01T00:00:00Z"
    assert capture._iso_timestamp("2026-11-04T20:45:00+08:00") == "2026-11-04T12:45:00Z"
    assert capture._iso_timestamp("2026-11-04T12:45:00Z") == "2026-11-04T12:45:00Z"
    assert capture._iso_timestamp("not a date") is None

    rows = capture.build_turn_messages(
        [{"role": "user", "content": "x", "timestamp": 0.0}], "x",
    )

    assert rows[0]["timestamp"] == "1970-01-01T00:00:00Z"


def test_redirect_turn_keeps_the_correction_and_final_answer():
    messages = [
        {"role": "user", "content": "original ask"},
        {"role": "assistant", "content": "working"},
        {"role": "user", "content": "no, do this instead"},
        {"role": "assistant", "content": "final answer"},
    ]

    rows = capture.build_turn_messages(messages, "original ask", "final answer")

    assert [row["content"] for row in rows] == [
        "original ask", "working", "no, do this instead", "final answer",
    ]


def test_non_exact_anchor_strips_recall_but_keeps_the_row_text():
    messages = [
        {"role": "user", "content": "voice: original ask\n\n"
                                    "<relevant-memories>noise</relevant-memories>"},
        {"role": "assistant", "content": "answer"},
    ]

    rows = capture.build_turn_messages(messages, "original ask", "answer")

    assert rows[0] == {"role": "user", "content": "voice: original ask"}
    assert rows[-1] == {"role": "assistant", "content": "answer"}


def test_final_answer_is_not_duplicated_when_the_transcript_has_it():
    messages = [
        {"role": "user", "content": "ask"},
        {"role": "assistant", "content": "answer"},
    ]

    rows = capture.build_turn_messages(messages, "ask", "answer")

    assert [row["content"] for row in rows] == ["ask", "answer"]


def test_conversation_add_omits_an_empty_session_id(monkeypatch):
    from plugins.memory_tencentdb.client import MemoryTencentdbClient

    client = MemoryTencentdbClient(base_url="http://127.0.0.1:1")
    seen = {}

    def fake_post(path, body, timeout=None):
        seen["path"] = path
        seen["body"] = body
        return {"code": 0, "data": {}}

    monkeypatch.setattr(client, "_post", fake_post)

    client.conversation_add([{"role": "user", "content": "x"}], session_id="")

    assert seen["path"] == "/v3/conversation/add"
    assert "session_id" not in seen["body"]


def test_conversation_search_reads_messages_and_stays_cross_session(monkeypatch):
    client = _FakeClient()
    calls = {}

    def fake_search(query, **kwargs):
        calls.update(kwargs)
        return {"code": 0, "data": {"messages": [{"role": "user", "content": "hello"}]}}

    client.conversation_search = fake_search
    monkeypatch.setattr(tools, "get_client", lambda: client)
    state.remember_identity("tool-session", "tool-user")

    out = tools.handle_conversation_search({"query": "hello"}, session_id="tool-session")

    assert "[user] hello" in out
    assert calls["user_id"] == "tool-user"
    assert "session_id" not in calls


def test_recall_renders_all_three_layers():
    client = _FakeClient(
        items=[{"type": "work_method", "content": "answer in Chinese first"}],
        core="persona text",
        entries=[{"path": "backend.md"}],
    )

    context = recall.build_context(client, "what did we decide about the backend?")

    assert "<relevant-memories>" in context
    assert "answer in Chinese first" in context
    assert "<user-core>\npersona text\n</user-core>" in context
    assert "- Scene: backend" in context


def test_slow_layers_are_cached_between_turns():
    class Client(_FakeClient):
        def __init__(self):
            super().__init__(core="persona", entries=[{"path": "s.md"}])
            self.core_calls = 0
            self.scene_calls = 0

        def core_read(self, **kwargs):
            self.core_calls += 1
            return super().core_read(**kwargs)

        def scenario_ls(self, **kwargs):
            self.scene_calls += 1
            return super().scenario_ls(**kwargs)

    client = Client()
    recall.build_context(client, "first question about the backend")
    recall.build_context(client, "second question about the backend")

    assert client.core_calls == 1
    assert client.scene_calls == 1
    assert len(client.searches) == 2


def test_recall_skips_trivial_prompts():
    client = _FakeClient()

    assert recall.build_context(client, "thanks") == ""
    assert client.searches == []


def test_runtime_status_reports_each_port(monkeypatch):
    seen = []

    def fake_healthy(port, timeout=1.0):
        seen.append(port)
        return port == runtime.GATEWAY_PORT

    monkeypatch.setattr(runtime, "_healthy", fake_healthy)

    assert runtime.status() == {"gateway": True, "embed": False, "llm-proxy": False}
    assert seen == [runtime.GATEWAY_PORT, runtime.EMBED_PORT, runtime.LLM_PROXY_PORT]


@pytest.fixture
def sidecar_runtime(tmp_path, monkeypatch):
    """A runtime dir with a supervisor script and every port reported down."""
    (tmp_path / "supervise.sh").write_text("#!/bin/bash\n", encoding="utf-8")
    (tmp_path / "pids").mkdir()
    monkeypatch.setattr(runtime, "runtime_dir", lambda: tmp_path)
    monkeypatch.setattr(runtime, "all_healthy", lambda: False)
    return tmp_path


def test_start_goes_through_the_systemd_unit(sidecar_runtime, monkeypatch):
    """The unit gives the daemons a cgroup the gateway's stop cleanup cannot reach."""
    actions = []
    monkeypatch.setattr(runtime, "_systemd_available", lambda: True)
    monkeypatch.setattr(runtime, "_systemctl",
                        lambda action, timeout=120.0: actions.append(action) or True)
    monkeypatch.setattr(runtime.subprocess, "Popen",
                        lambda *a, **k: pytest.fail("spawned outside the unit"))

    assert runtime.ensure_started(wait=0) is True
    assert actions == ["start"]


def test_start_falls_back_to_the_supervisor_when_systemctl_fails(sidecar_runtime, monkeypatch):
    monkeypatch.setattr(runtime, "_systemd_available", lambda: True)
    monkeypatch.setattr(runtime, "_systemctl", lambda action, timeout=120.0: False)
    spawned = []

    class _Popen:
        def __init__(self, cmd, **kwargs):
            spawned.append(cmd)

    monkeypatch.setattr(runtime.subprocess, "Popen", _Popen)

    assert runtime.ensure_started(wait=0) is True
    assert spawned == [["bash", str(sidecar_runtime / "supervise.sh"), "start"]]


def test_stop_and_restart_keep_systemd_in_sync(sidecar_runtime, monkeypatch):
    actions = []
    monkeypatch.setattr(runtime, "_systemd_available", lambda: True)
    monkeypatch.setattr(runtime, "_systemctl",
                        lambda action, timeout=120.0: actions.append(action) or True)
    monkeypatch.setattr(runtime.subprocess, "call",
                        lambda *a, **k: pytest.fail("supervise.sh bypassed the unit"))

    assert runtime.run_action("stop") == 0
    assert runtime.run_action("restart") == 0
    assert actions == ["stop", "restart"]


def test_register_wires_hooks_tools_and_cli():
    class Ctx:
        def __init__(self):
            self.hooks = {}
            self.tools = {}
            self.cli = {}

        def register_hook(self, name, callback):
            self.hooks.setdefault(name, []).append(callback)

        def register_tool(self, **kwargs):
            self.tools[kwargs["name"]] = kwargs

        def register_cli_command(self, **kwargs):
            self.cli[kwargs["name"]] = kwargs

    ctx = Ctx()
    plugin.register(ctx)

    assert set(ctx.hooks) == {"on_session_start", "pre_llm_call", "post_llm_call"}
    assert set(ctx.tools) == {
        "memory_tencentdb_memory_search",
        "memory_tencentdb_conversation_search",
        "memory_tencentdb_read_scene",
    }
    assert all(tool["toolset"] == "memory" for tool in ctx.tools.values())
    assert "memory-tencentdb" in ctx.cli


def test_pre_hook_skips_subagent_sessions(monkeypatch):
    calls = []
    monkeypatch.setattr(plugin, "_gateway_ready", lambda: calls.append("ready") or True)

    result = plugin._on_pre_llm_call(
        user_message="do work", session_id="child", parent_session_id="parent", sender_id="u1",
    )

    assert result is None
    assert calls == []
    assert plugin.state.is_subagent("child")


def test_post_hook_spawns_capture_and_keeps_identity(monkeypatch):
    started = []

    class Thread:
        def start(self):
            started.append(True)

    def fake_spawn(target, *, name, kwargs):
        started.append((target, kwargs))
        return Thread()

    monkeypatch.setattr(plugin, "_gateway_ready", lambda: True)
    monkeypatch.setattr(
        "agent.memory_provider.spawn_context_thread", fake_spawn, raising=False
    )
    plugin.state.remember_identity("s1", "u1")

    plugin._on_post_llm_call(
        session_id="s1", user_message="ask", assistant_response="answer",
        conversation_history=[{"role": "user", "content": "ask"},
                              {"role": "assistant", "content": "answer"}],
    )

    assert len(started) == 2
    target, kwargs = started[0]
    assert target is capture.capture_turn
    assert kwargs["user_id"] == "u1"
    assert kwargs["session_id"] == "s1"


def test_post_hook_ignores_subagents(monkeypatch):
    called = []
    monkeypatch.setattr("agent.memory_provider.spawn_context_thread",
                        lambda *a, **k: called.append(True), raising=False)
    plugin.state.mark_subagent("child")

    plugin._on_post_llm_call(session_id="child", user_message="x",
                             assistant_response="y", conversation_history=[])

    assert called == []


def test_post_hook_sends_the_turn_over_http(monkeypatch):
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from plugins.memory_tencentdb.client import MemoryTencentdbClient

    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            received.append((self.path, body))
            payload = b'{"code":0,"data":{}}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = MemoryTencentdbClient(base_url=f"http://127.0.0.1:{server.server_port}")
        monkeypatch.setattr("plugins.memory_tencentdb.client.get_client", lambda: client)

        class SyncThread:
            def __init__(self, fn):
                self._fn = fn

            def start(self):
                self._fn()

        def fake_spawn(target, *, name, kwargs):
            return SyncThread(lambda: target(**kwargs))

        monkeypatch.setattr("agent.memory_provider.spawn_context_thread", fake_spawn, raising=False)
        plugin.state.remember_identity("http-session", "http-user")

        plugin._on_post_llm_call(
            session_id="http-session",
            user_message="ping",
            assistant_response="pong",
            conversation_history=[
                {"role": "user", "content": "ping"},
                {"role": "assistant", "content": "pong"},
            ],
            platform="cli",
        )
    finally:
        server.shutdown()
        server.server_close()

    assert received
    path, body = received[0]
    assert path == "/v3/conversation/add"
    assert body["user_id"] == "http-user"
    assert body["session_id"] == "http-session"
    assert body["messages"] == [
        {"role": "user", "content": "ping"},
        {"role": "assistant", "content": "pong"},
    ]
