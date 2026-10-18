from __future__ import annotations

from collections import deque
from pathlib import Path
import sys
import threading
import time

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, ToolMessage
from mcp_types import CallToolResult, TextContent, Tool

from eregion.runtime.context.snapshots import ContextSnapshotStatus
from eregion.runtime.errors import UnknownToolError
from eregion.runtime.mcp.config import McpRuntimeConfig, McpStdioServerConfig
from eregion.runtime.mcp.errors import McpStartupError
from eregion.api.main import create_app
from eregion.api.session_dependencies import (
    JOB_ASSISTANT_READONLY_TOOLS,
    create_session_runtime,
)
from session_paths import session_decision_path  # noqa: F401 (both decision paths)


ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "examples" / "mcp" / "local_test_server.py"


class CapturingModel:
    def __init__(self, responses=None):
        self.responses = deque(responses or [AIMessage(content="Done.")])
        self.tool_batches: list[list[dict]] = []
        self.message_batches: list[list] = []

    def bind_tools(self, tools):
        self.tool_batches.append(tools)
        return self

    def invoke(self, messages, config=None, **kwargs):
        self.message_batches.append(messages)
        return self.responses.popleft()


class FakeMcpClient:
    def __init__(self, *, is_error=False):
        self.is_error = is_error
        self.closed = False

    def connect(self):
        return None

    def list_tools(self):
        return [Tool(
            name="echo_text",
            description="Echo text",
            inputSchema={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
                "additionalProperties": False,
            },
        )]

    def call_tool(self, name, arguments, *, timeout_seconds=None):
        return CallToolResult(
            content=[TextContent(type="text", text="remote failed" if self.is_error else arguments["text"])],
            structuredContent={"text": arguments["text"]},
            isError=self.is_error,
        )

    def close(self):
        self.closed = True


class FailingMcpClient(FakeMcpClient):
    def connect(self):
        raise McpStartupError("private process details")


def mcp_config(*, read_only=True):
    return McpRuntimeConfig(servers=[McpStdioServerConfig(
        server_id="local-test",
        command=sys.executable,
        args=[str(SERVER)],
        cwd=ROOT,
        required=True,
        tool_allowlist={"echo_text", "add_numbers"},
        read_only_tools={"echo_text", "add_numbers"} if read_only else set(),
        startup_timeout_seconds=10,
        call_timeout_seconds=5,
    )])


def test_fastapi_lifecycle_health_effective_tools_and_snapshot(tmp_path):
    captured = {}
    model = CapturingModel()

    def factory():
        runtime = create_session_runtime(migrate_schema=True,
            database_url=f"sqlite:///{(tmp_path / 'mcp-app.sqlite').as_posix()}",
            model=model,
            mcp_config=mcp_config(),
        )
        captured["runtime"] = runtime
        return runtime

    app = create_app(session_runtime_factory=factory)
    with TestClient(app) as client:
        health = client.get("/health").json()
        assert health["status"] == "ok"
        assert {key: health["mcp"][key] for key in (
            "configured_servers", "ready_servers", "failed_optional_servers", "registered_tools"
        )} == {
                "configured_servers": 1,
                "ready_servers": 1,
                "failed_optional_servers": 0,
                "registered_tools": 2,
        }
        assert health["mcp"]["servers"][0]["server_id"] == "local-test"
        assert "command" not in str(health).lower()
        created = client.post("/sessions", json={
            "capability_profile": "job_assistant_readonly"
        }).json()["session"]
        runtime = captured["runtime"]
        state = runtime.sessions.require(created["session_id"])
        assert state.allowed_tools == JOB_ASSISTANT_READONLY_TOOLS | {
            "mcp__local-test__echo_text",
            "mcp__local-test__add_numbers",
        }
        response = client.post(
            f"/sessions/{state.session_id}/messages",
            json={
                "message_id": "mcp-visible-message",
                "content": "Use echo_text if needed.",
                "expected_version": state.version,
            },
        )
        assert response.status_code == 200
        tool_names = {
            item["function"]["name"] for item in model.tool_batches[-1]
        }
        final = runtime.sessions.require(state.session_id)
        snapshot = runtime.context_snapshots.require(final.last_context_snapshot_id)
        assert snapshot.status == ContextSnapshotStatus.USED
        assert snapshot.effective_tools == frozenset(tool_names)
        assert "mcp__local-test__echo_text" in tool_names
        manager = runtime.mcp_manager
    assert all(item.status.value == "stopped" for item in manager.diagnostics)
    with pytest.raises(UnknownToolError):
        captured["runtime"].registry.get("mcp__local-test__echo_text")


def test_mcp_is_error_is_returned_to_model_and_session_continues(tmp_path):
    first = AIMessage(
        content="",
        tool_calls=[{
            "name": "mcp__local-test__echo_text",
            "args": {"text": "test"},
            "id": "mcp-call-1",
            "type": "tool_call",
        }],
    )
    model = CapturingModel([first, AIMessage(content="The remote tool failed safely.")])
    fake = FakeMcpClient(is_error=True)
    config = McpRuntimeConfig(servers=[McpStdioServerConfig(
        server_id="local-test",
        command=sys.executable,
        tool_allowlist={"echo_text"},
        read_only_tools={"echo_text"},
    )])
    runtime = create_session_runtime(migrate_schema=True,
        database_url=f"sqlite:///{(tmp_path / 'mcp-error.sqlite').as_posix()}",
        model=model,
        mcp_config=config,
        mcp_client_factory=lambda _: fake,
    )
    app = create_app(run_service=object(), session_runtime=runtime)
    try:
        with TestClient(app) as client:
            session = client.post("/sessions", json={}).json()["session"]
            response = client.post(
                f"/sessions/{session['session_id']}/messages",
                json={
                    "message_id": "mcp-error-message",
                    "content": "Echo this text.",
                    "expected_version": session["version"],
                },
            )
            assert response.status_code == 200
            assert response.json()["session"]["status"] == "active"
            assert response.json()["response"] == "The remote tool failed safely."
            call = response.json()["tool_calls"][0]
            assert call["provider"] == "MCP"
            assert call["mcp_server_id"] == "local-test"
            assert call["remote_tool_name"] == "echo_text"
            assert call["status"] == "failed"
            assert call["error_code"] == "mcp_tool_reported_error"
            assert call["duration_ms"] >= 0
            assert "arguments" not in call
            assert "result" not in call
            tool_messages = [
                message
                for batch in model.message_batches
                for message in batch
                if isinstance(message, ToolMessage)
            ]
            assert any("mcp_tool_reported_error" in str(item.content) for item in tool_messages)
    finally:
        runtime.close()
    assert fake.closed is True


def test_api_cannot_supply_mcp_configuration(tmp_path):
    runtime = create_session_runtime(migrate_schema=True,
        database_url=f"sqlite:///{(tmp_path / 'no-client-config.sqlite').as_posix()}",
        model=CapturingModel(),
        mcp_config=McpRuntimeConfig(),
    )
    try:
        with TestClient(create_app(run_service=object(), session_runtime=runtime)) as client:
            response = client.post("/sessions", json={
                "capability_profile": "job_assistant_readonly",
                "mcp_servers": [{"command": "unsafe"}],
            })
            assert response.status_code == 422
    finally:
        runtime.close()


def test_optional_failure_produces_sanitized_degraded_health(tmp_path):
    failed = FailingMcpClient()
    config = McpRuntimeConfig(servers=[McpStdioServerConfig(
        server_id="optional-private-server",
        command="private-command --with-secret",
        required=False,
    )])
    runtime = create_session_runtime(migrate_schema=True,
        database_url=f"sqlite:///{(tmp_path / 'degraded.sqlite').as_posix()}",
        model=CapturingModel(),
        mcp_config=config,
        mcp_client_factory=lambda _: failed,
    )
    try:
        with TestClient(create_app(run_service=object(), session_runtime=runtime)) as client:
            response = client.get("/health")
            body = response.json()
            assert body["status"] == "degraded"
            assert {key: body["mcp"][key] for key in (
                "configured_servers", "ready_servers", "failed_optional_servers", "registered_tools"
            )} == {
                    "configured_servers": 1,
                    "ready_servers": 0,
                    "failed_optional_servers": 1,
                    "registered_tools": 0,
            }
            server = body["mcp"]["servers"][0]
            assert server["status"] == "degraded"
            assert server["last_error_code"] == "mcp_startup_failed"
            assert "private process details" not in response.text
            assert "private-command" not in response.text
            assert "command" not in server
            assert "args" not in server
            assert "cwd" not in server
            assert "env" not in server
    finally:
        runtime.close()
    assert failed.closed is True


def test_mcp_write_like_tool_still_requires_persisted_approval(tmp_path):
    tool_call = AIMessage(content="", tool_calls=[{
        "name": "mcp__local-test__echo_text",
        "args": {"text": "approval"},
        "id": "approval-call",
        "type": "tool_call",
    }])
    model = CapturingModel([tool_call])
    fake = FakeMcpClient()
    config = McpRuntimeConfig(servers=[McpStdioServerConfig(
        server_id="local-test",
        command=sys.executable,
        tool_allowlist={"echo_text"},
    )])
    runtime = create_session_runtime(migrate_schema=True,
        database_url=f"sqlite:///{(tmp_path / 'approval.sqlite').as_posix()}",
        model=model,
        mcp_config=config,
        mcp_client_factory=lambda _: fake,
    )
    try:
        with TestClient(create_app(run_service=object(), session_runtime=runtime)) as client:
            created = client.post("/sessions", json={}).json()["session"]
            response = client.post(
                f"/sessions/{created['session_id']}/messages",
                json={
                    "message_id": "approval-message",
                    "content": "Call the configured tool.",
                    "expected_version": created["version"],
                },
            )
            assert response.status_code == 200
            body = response.json()
            assert body["session"]["status"] == "awaiting_tool_approval"
            assert body["pending_tool_approvals"][0]["tool_name"] == (
                "mcp__local-test__echo_text"
            )
    finally:
        runtime.close()


def test_restarted_session_handles_unavailable_mcp_tool_safely(tmp_path):
    url = f"sqlite:///{(tmp_path / 'restart.sqlite').as_posix()}"
    first_client = FakeMcpClient()
    config = McpRuntimeConfig(servers=[McpStdioServerConfig(
        server_id="local-test",
        command=sys.executable,
        tool_allowlist={"echo_text"},
        read_only_tools={"echo_text"},
    )])
    first = create_session_runtime(migrate_schema=True,
        database_url=url,
        model=CapturingModel(),
        mcp_config=config,
        mcp_client_factory=lambda _: first_client,
    )
    state = first.coordinator.create_session(
        allowed_tools=first.capability_tools("job_assistant_readonly")
    )
    first.close()

    unavailable_call = AIMessage(content="", tool_calls=[{
        "name": "mcp__local-test__echo_text",
        "args": {"text": "missing"},
        "id": "missing-call",
        "type": "tool_call",
    }])
    model = CapturingModel([
        unavailable_call,
        AIMessage(content="That configured MCP tool is unavailable."),
    ])
    restarted = create_session_runtime(migrate_schema=True,
        database_url=url,
        model=model,
        mcp_config=McpRuntimeConfig(),
    )
    try:
        with TestClient(create_app(run_service=object(), session_runtime=restarted)) as client:
            restored = client.get(f"/sessions/{state.session_id}")
            assert restored.status_code == 200
            response = client.post(
                f"/sessions/{state.session_id}/messages",
                json={
                    "message_id": "after-restart",
                    "content": "Try the old MCP tool.",
                    "expected_version": restored.json()["session"]["version"],
                },
            )
            assert response.status_code == 200
            assert response.json()["session"]["status"] == "active"
            assert response.json()["response"] == (
                "That configured MCP tool is unavailable."
            )
            tool_messages = [
                message
                for batch in model.message_batches
                for message in batch
                if isinstance(message, ToolMessage)
            ]
            assert any("tool_not_found" in str(item.content) for item in tool_messages)
    finally:
        restarted.close()


def test_sync_session_execution_does_not_block_fastapi_event_loop(tmp_path):
    runtime = create_session_runtime(migrate_schema=True,
        database_url=f"sqlite:///{(tmp_path / 'event-loop.sqlite').as_posix()}",
        model=CapturingModel(),
        mcp_config=McpRuntimeConfig(),
    )
    entered = threading.Event()
    release = threading.Event()
    original = runtime.coordinator.submit_user_message

    def blocked_submit(*args, **kwargs):
        entered.set()
        release.wait(timeout=2)
        return original(*args, **kwargs)

    runtime.coordinator.submit_user_message = blocked_submit
    try:
        with TestClient(create_app(run_service=object(), session_runtime=runtime)) as client:
            created = client.post("/sessions", json={}).json()["session"]
            responses = []

            def send_message():
                responses.append(client.post(
                    f"/sessions/{created['session_id']}/messages",
                    json={
                        "message_id": "blocking-turn",
                        "content": "Wait while health remains responsive.",
                        "expected_version": created["version"],
                    },
                ))

            worker = threading.Thread(target=send_message)
            worker.start()
            assert entered.wait(timeout=1)
            started = time.monotonic()
            health = client.get("/health")
            elapsed = time.monotonic() - started
            assert health.status_code == 200
            assert elapsed < 0.5
            release.set()
            worker.join(timeout=3)
            assert responses[0].status_code == 200
    finally:
        release.set()
        runtime.close()
