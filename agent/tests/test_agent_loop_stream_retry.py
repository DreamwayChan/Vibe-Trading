"""AgentLoop single stream retry on ProviderStreamError.

Mirrors the swarm worker policy: a transient mid-stream failure (connection
reset, relay hiccup, 5xx) is retried exactly once; a deterministic 4xx fails
the run immediately with error_code=provider_stream_error and no wasted
request. Deltas from the failed attempt are dropped so the trace does not
contain duplicated thinking text.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

import pytest

import src.agent.loop as loop_mod
from src.providers.chat import LLMResponse, ProviderStreamError


class _FlakyLoopLLM:
    """LLM stub raising queued errors from stream_chat before succeeding."""

    def __init__(self, errors: list[Exception], final_content: str) -> None:
        """Initialize the flaky stub.

        Args:
            errors: Exceptions raised by successive ``stream_chat`` calls,
                consumed in order before any success.
            final_content: Content of the response returned once the error
                queue is drained.
        """
        self._errors = list(errors)
        self._final_content = final_content
        self.calls = 0

    def stream_chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[Any] | None = None,
        on_text_chunk: Callable[[str], None] | None = None,
        on_reasoning_chunk: Callable[[str], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> LLMResponse:
        """Raise the next queued error or return the final response.

        Args:
            messages: Conversation messages (ignored).
            tools: Tool definitions (ignored).
            on_text_chunk: Text streaming callback; fires before a queued
                error so the dropped-delta behavior is observable.
            on_reasoning_chunk: Reasoning callback (ignored).

        Returns:
            The scripted final ``LLMResponse``.

        Raises:
            Exception: The next queued error, if any remain.
        """
        self.calls += 1
        if self._errors:
            if on_text_chunk:
                on_text_chunk("partial-from-failed-attempt ")
            raise self._errors.pop(0)
        if on_text_chunk:
            on_text_chunk(self._final_content)
        return LLMResponse(content=self._final_content)

    def chat(self, messages: list[dict[str, Any]], **_: Any) -> LLMResponse:
        """Return an empty non-streaming response (unused).

        Args:
            messages: Conversation messages (ignored).

        Returns:
            Empty ``LLMResponse``.
        """
        return LLMResponse(content="")


def _transient_error() -> ProviderStreamError:
    """Build a ProviderStreamError mimicking a transient mid-stream reset.

    Returns:
        ProviderStreamError wrapping a ``ConnectionResetError`` (no status).
    """
    return ProviderStreamError(
        provider="deepseek",
        model="deepseek-v4-pro",
        original=ConnectionResetError("connection reset by peer"),
    )


def _bad_request_error() -> ProviderStreamError:
    """Build a ProviderStreamError mimicking a deterministic 400 rejection.

    Returns:
        ProviderStreamError whose original exception carries status_code=400.
    """
    original = Exception("invalid temperature: only 1 is allowed for this model")
    original.status_code = 400  # type: ignore[attr-defined]
    return ProviderStreamError(
        provider="moonshot", model="kimi-k2.6", original=original
    )


def _run(
    monkeypatch,
    tmp_path: Path,
    llm: _FlakyLoopLLM,
    events: list[tuple[str, dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Run an AgentLoop turn against the given scripted LLM.

    Args:
        monkeypatch: pytest monkeypatch fixture (zeroes the retry sleep).
        tmp_path: Scratch run directory.
        llm: The scripted LLM stub.
        events: Optional event sink collecting ``(event_type, data)`` tuples.

    Returns:
        The AgentLoop result dict.
    """
    from src.agent.loop import AgentLoop
    from src.memory.persistent import PersistentMemory
    from src.tools import build_registry

    monkeypatch.setattr(loop_mod, "STREAM_RETRY_DELAY_S", 0.0)
    pm = PersistentMemory()
    agent = AgentLoop(
        registry=build_registry(persistent_memory=pm, include_shell_tools=False),
        llm=llm,
        event_callback=(
            (lambda event_type, data: events.append((event_type, data)))
            if events is not None
            else None
        ),
        max_iterations=3,
        persistent_memory=pm,
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    agent.memory.run_dir = str(run_dir)
    return agent.run(user_message="hello")


def test_transient_stream_failure_is_retried_and_run_succeeds(
    monkeypatch, tmp_path: Path
) -> None:
    """One transient ProviderStreamError then success → run completes (2 calls)."""
    llm = _FlakyLoopLLM([_transient_error()], "Final answer.")
    events: list[tuple[str, dict[str, Any]]] = []

    result = _run(monkeypatch, tmp_path, llm, events)

    assert result["status"] == "success"
    assert result["content"] == "Final answer."
    assert llm.calls == 2

    event_types = [event_type for event_type, _ in events]
    assert "stream_reset" in event_types
    text_positions = [
        index for index, event_type in enumerate(event_types) if event_type == "text_delta"
    ]
    reset_position = event_types.index("stream_reset")
    assert text_positions[0] < reset_position < text_positions[-1]
    reset = next(data for event_type, data in events if event_type == "stream_reset")
    assert reset["reason"] == "provider_stream_retry"
    assert reset["iter"] == 1
    assert reset["provider"] == "deepseek"
    assert reset["model"] == "deepseek-v4-pro"


def test_double_stream_failure_fails_run(monkeypatch, tmp_path: Path) -> None:
    """Two consecutive transient failures → failed run, no third attempt."""
    llm = _FlakyLoopLLM([_transient_error(), _transient_error()], "Final answer.")

    result = _run(monkeypatch, tmp_path, llm)

    assert result["status"] == "failed"
    assert result["error_code"] == "provider_stream_error"
    assert llm.calls == 2


def test_non_retryable_4xx_fails_without_retry(monkeypatch, tmp_path: Path) -> None:
    """A deterministic 4xx ProviderStreamError fails immediately (1 call)."""
    llm = _FlakyLoopLLM([_bad_request_error()], "Final answer.")

    result = _run(monkeypatch, tmp_path, llm)

    assert result["status"] == "failed"
    assert result["error_code"] == "provider_stream_error"
    assert llm.calls == 1


def test_loop_source_wires_heartbeat_around_llm_streaming() -> None:
    """stream_chat must sit inside HeartbeatTimer so Agent SSE idle timeout
    cannot false-trip during a slow first-token / provider retry wait."""
    import inspect
    import re

    source = inspect.getsource(loop_mod.AgentLoop.run)
    timer_match = re.search(
        r'with HeartbeatTimer\(\s*\n\s*tool_name=f"llm:', source
    )
    timer_idx = timer_match.start() if timer_match else -1
    stream_idx = source.find("self.llm.stream_chat(", timer_idx)
    assert 0 < timer_idx < stream_idx, (
        "AgentLoop stream_chat must be wrapped in HeartbeatTimer emitting "
        "llm_heartbeat so the Agent page idle timer stays in sync."
    )


def test_slow_stream_emits_llm_heartbeat(monkeypatch, tmp_path: Path) -> None:
    """A multi-second stream_chat must emit llm_heartbeat ticks."""

    class _SlowLLM:
        """LLM stub that blocks inside stream_chat long enough for a tick."""

        model_name = "slow-test-model"
        calls = 0

        def stream_chat(self, *args: Any, **kwargs: Any) -> LLMResponse:
            self.calls += 1
            _time = __import__("time")
            _time.sleep(0.7)
            return LLMResponse(content="done")

        def chat(self, *args: Any, **kwargs: Any) -> LLMResponse:
            return LLMResponse(content="")

    monkeypatch.setattr(loop_mod, "HEARTBEAT_INTERVAL_S", 0.2)
    events: list[tuple[str, dict[str, Any]]] = []
    result = _run(monkeypatch, tmp_path, _SlowLLM(), events)

    assert result["status"] == "success"
    heartbeats = [data for event_type, data in events if event_type == "llm_heartbeat"]
    assert heartbeats, "expected at least one llm_heartbeat during slow stream"
    assert heartbeats[0]["phase"] == "llm"
    assert heartbeats[0]["tool"] == "llm:slow-test-model"
