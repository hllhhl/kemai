"""Retry behaviour of ModelClient: transient failures are repeated, real ones are not.

Relay gateways (the `ly.77625.xyz` style ones) answer 500 on the first call and
200 on the next, so these tests pin down exactly when a second attempt happens.
"""
from __future__ import annotations

import pytest

from app import llm
from app.config import Settings
from app.llm import ModelClient, ModelRuntimeError
from app.model_registry import ModelConfig


class FakeStore:
    """Stands in for ModelConfigStore: only `snapshot()` is reached by `_request`."""

    def __init__(self, configured=True):
        self.config = ModelConfig(
            base_url="https://relay.example/v1",
            model="demo-flash",
            api_key="k" if configured else "",
            timeout_seconds=5,
        )

    def snapshot(self):
        return self.config


class FlakyClient(ModelClient):
    """`_events` fails a set number of times, then yields one complete answer."""

    def __init__(self, failures, error_factory=None, emit_before_failing=None):
        super().__init__(Settings(), FakeStore())
        self.config_store = FakeStore()
        self.failures = list(failures)
        self.error_factory = error_factory or (lambda: ModelRuntimeError("boom", 500))
        self.emit_before_failing = emit_before_failing
        self.calls = 0

    def _events(self, request, timeout):
        self.calls += 1
        if self.failures:
            self.failures.pop(0)
            if self.emit_before_failing:
                yield self.emit_before_failing
            raise self.error_factory()
        yield {"type": "done", "text": "答案", "stop_reason": "stop"}


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """Record the waits instead of paying them."""
    waits = []
    monkeypatch.setattr(llm, "sleep", lambda seconds: waits.append(seconds))
    return waits


def test_retries_until_the_gateway_answers(no_sleep):
    client = FlakyClient([1, 1, 1, 1])  # four failures, success on the fifth attempt
    assert client.complete([{"role": "user", "content": "hi"}]) == "答案"
    assert client.calls == 5
    assert no_sleep == [0.8, 1.6, 3.2, 6.4]


def test_gives_up_after_five_attempts(no_sleep):
    client = FlakyClient([1] * 10)
    with pytest.raises(ModelRuntimeError):
        client.complete([{"role": "user", "content": "hi"}])
    assert client.calls == 5


def test_recovers_on_the_second_call(no_sleep):
    client = FlakyClient([1])
    assert client.complete([{"role": "user", "content": "hi"}]) == "答案"
    assert client.calls == 2
    assert no_sleep == [0.8]


@pytest.mark.parametrize("status", [401, 402, 403, 404, 400])
def test_does_not_retry_credential_or_request_errors(status, no_sleep):
    client = FlakyClient([1, 1], error_factory=lambda: ModelRuntimeError("no", status))
    with pytest.raises(ModelRuntimeError):
        client.complete([{"role": "user", "content": "hi"}])
    assert client.calls == 1


@pytest.mark.parametrize("status", [429, 502, 503, 504])
def test_retries_throttling_and_gateway_errors(status, no_sleep):
    client = FlakyClient([1], error_factory=lambda: ModelRuntimeError("slow", status))
    assert client.complete([{"role": "user", "content": "hi"}]) == "答案"
    assert client.calls == 2


def test_does_not_retry_after_text_was_already_streamed(no_sleep):
    """Repeating would replay half an answer to the user."""
    client = FlakyClient([1], emit_before_failing={"type": "delta", "text": "半句"})
    with pytest.raises(ModelRuntimeError):
        list(client.stream([{"role": "user", "content": "hi"}]))
    assert client.calls == 1
    assert no_sleep == []


def test_timeout_without_status_is_retried(no_sleep):
    client = FlakyClient([1], error_factory=lambda: ModelRuntimeError("timeout", timed_out=True))
    assert client.complete([{"role": "user", "content": "hi"}]) == "答案"
    assert client.calls == 2


def test_failure_message_admits_the_retries():
    message = llm.model_failure_message(ModelRuntimeError("gateway", 502))
    assert "502" in message and "重试 5 次" in message

    limited = llm.model_failure_message(ModelRuntimeError("slow", 429))
    assert "重试 5 次" in limited


def test_node_runtime_prefers_the_desktop_shell(monkeypatch, tmp_path):
    """打包版不能要求用户另装 Node.js：desktop 传进来的 exe 优先于 PATH。"""
    fake = tmp_path / "electron.exe"
    fake.write_bytes(b"")
    monkeypatch.setenv("KEMAI_NODE", str(fake))
    monkeypatch.setenv("KEMAI_NODE_ELECTRON", "1")

    node, extra = llm.node_runtime()

    assert node == str(fake)
    assert extra == {"ELECTRON_RUN_AS_NODE": "1"}


def test_node_runtime_falls_back_to_path_in_development(monkeypatch):
    monkeypatch.delenv("KEMAI_NODE", raising=False)
    monkeypatch.setattr(llm.shutil, "which", lambda name: r"C:\nodejs\node.exe")

    node, extra = llm.node_runtime()

    assert node.endswith("node.exe")
    assert extra == {}


def test_missing_node_explains_what_to_do(monkeypatch):
    monkeypatch.delenv("KEMAI_NODE", raising=False)
    monkeypatch.setattr(llm.shutil, "which", lambda name: None)

    with pytest.raises(ModelRuntimeError, match="未找到 Node.js"):
        llm.node_runtime()


def test_missing_bridge_names_the_missing_component(monkeypatch, tmp_path):
    """报错要说清是打包缺文件，而不是含糊的「进程提前结束」。"""
    monkeypatch.setattr(llm, "ROOT", tmp_path)

    with pytest.raises(ModelRuntimeError, match="缺少模型运行组件"):
        llm.bridge_script()
