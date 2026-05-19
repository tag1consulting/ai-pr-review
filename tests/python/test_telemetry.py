"""Tests for ai_pr_review.telemetry."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ai_pr_review.telemetry import TelemetryEvent, emit_telemetry


def _sample_event(**overrides: object) -> TelemetryEvent:
    base = TelemetryEvent(
        correlation_id="test-id-123",
        timestamp="2026-05-19T12:00:00Z",
        repository="tag1consulting/ai-pr-review",
        pr_number="42",
        outcome="review_posted",
        findings_count=3,
        findings_by_severity={"High": 1, "Medium": 2},
        failed_agents=[],
        token_usage_by_agent={
            "code-reviewer": {
                "input": 1000,
                "output": 500,
                "cache_creation": 0,
                "cache_read": 0,
                "model": "claude-sonnet-4-6",
            }
        },
        agent_latency_ms={},
        sarif_elapsed_s=None,
        learning_store_entries_loaded=5,
        telemetry_schema_version="1",
    )
    for k, v in overrides.items():
        setattr(base, k, v)
    return base


def test_file_sink_writes_json(tmp_path: Path) -> None:
    sink = f"file://{tmp_path}/telemetry.jsonl"
    event = _sample_event()
    emit_telemetry(event, sink=sink)
    lines = (tmp_path / "telemetry.jsonl").read_text().splitlines()
    assert len(lines) == 1
    data = json.loads(lines[0])
    assert data["correlation_id"] == "test-id-123"
    assert data["repository"] == "tag1consulting/ai-pr-review"
    assert data["findings_count"] == 3


def test_file_sink_appends_on_multiple_calls(tmp_path: Path) -> None:
    sink = f"file://{tmp_path}/telemetry.jsonl"
    emit_telemetry(_sample_event(correlation_id="id-1"), sink=sink)
    emit_telemetry(_sample_event(correlation_id="id-2"), sink=sink)
    lines = (tmp_path / "telemetry.jsonl").read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["correlation_id"] == "id-1"
    assert json.loads(lines[1])["correlation_id"] == "id-2"


def test_http_sink_posts_json() -> None:
    event = _sample_event()
    with patch("ai_pr_review.telemetry.httpx.post") as mock_post:
        mock_post.return_value = MagicMock(status_code=200)
        emit_telemetry(event, sink="https://example.com/telemetry")
    mock_post.assert_called_once()
    call_kwargs = mock_post.call_args
    assert call_kwargs[0][0] == "https://example.com/telemetry"
    body = call_kwargs[1]["json"]
    assert body["correlation_id"] == "test-id-123"
    assert body["telemetry_schema_version"] == "1"


def test_http_sink_plain_http_scheme() -> None:
    event = _sample_event()
    with patch("ai_pr_review.telemetry.httpx.post") as mock_post:
        mock_post.return_value = MagicMock(status_code=200)
        emit_telemetry(event, sink="http://internal.example.com/telemetry")
    mock_post.assert_called_once()
    assert mock_post.call_args[0][0] == "http://internal.example.com/telemetry"


def test_http_4xx_response_logs_warning(caplog: pytest.LogCaptureFixture) -> None:
    event = _sample_event()
    with patch("ai_pr_review.telemetry.httpx.post") as mock_post:
        mock_post.return_value = MagicMock(status_code=401)
        with caplog.at_level(logging.WARNING):
            emit_telemetry(event, sink="https://example.com/telemetry")
    assert any("401" in r.message for r in caplog.records)


def test_http_5xx_response_logs_server_error(caplog: pytest.LogCaptureFixture) -> None:
    event = _sample_event()
    with patch("ai_pr_review.telemetry.httpx.post") as mock_post:
        mock_post.return_value = MagicMock(status_code=503)
        with caplog.at_level(logging.WARNING):
            emit_telemetry(event, sink="https://example.com/telemetry")
    assert any("503" in r.message and "server error" in r.message for r in caplog.records)


def test_file_sink_empty_path_logs_warning(caplog: pytest.LogCaptureFixture) -> None:
    """file:// with no path component logs a clear misconfiguration warning."""
    event = _sample_event()
    with caplog.at_level(logging.WARNING):
        emit_telemetry(event, sink="file://")
    assert any("empty path" in r.message for r in caplog.records)


def test_http_failure_swallowed() -> None:
    import httpx
    event = _sample_event()
    with patch("ai_pr_review.telemetry.httpx.post", side_effect=httpx.NetworkError("connection refused")):
        # must not raise
        emit_telemetry(event, sink="https://example.com/telemetry")


def test_http_timeout_swallowed() -> None:
    import httpx
    event = _sample_event()
    with patch("ai_pr_review.telemetry.httpx.post", side_effect=httpx.TimeoutException("timeout")):
        emit_telemetry(event, sink="https://example.com/telemetry")


def test_http_non_httpx_exception_swallowed(caplog: pytest.LogCaptureFixture) -> None:
    """Non-httpx transport exceptions (ssl.SSLError, socket OSError) are swallowed."""
    import ssl
    event = _sample_event()
    with (
        patch("ai_pr_review.telemetry.httpx.post", side_effect=ssl.SSLError("certificate verify failed")),
        caplog.at_level(logging.WARNING),
    ):
        emit_telemetry(event, sink="https://example.com/telemetry")
    assert any("SSLError" in r.message or "unexpected" in r.message.lower() for r in caplog.records)


def test_empty_sink_skipped(caplog: pytest.LogCaptureFixture) -> None:
    event = _sample_event()
    with caplog.at_level(logging.WARNING):
        emit_telemetry(event, sink="")
    assert any("AI_TELEMETRY_SINK" in r.message or "sink" in r.message.lower() for r in caplog.records)


def test_unknown_scheme_skipped(caplog: pytest.LogCaptureFixture) -> None:
    event = _sample_event()
    with caplog.at_level(logging.WARNING):
        emit_telemetry(event, sink="ftp://somewhere/file.jsonl")
    assert any("sink" in r.message.lower() or "scheme" in r.message.lower() or "unrecognised" in r.message.lower() for r in caplog.records)


def test_schema_version_field(tmp_path: Path) -> None:
    sink = f"file://{tmp_path}/t.jsonl"
    emit_telemetry(_sample_event(), sink=sink)
    data = json.loads((tmp_path / "t.jsonl").read_text())
    assert data["telemetry_schema_version"] == "1"


def test_event_has_all_required_fields() -> None:
    event = _sample_event()
    d = asdict(event)
    required = {
        "correlation_id", "timestamp", "repository", "pr_number", "outcome",
        "findings_count", "findings_by_severity", "failed_agents",
        "token_usage_by_agent", "agent_latency_ms", "sarif_elapsed_s",
        "learning_store_entries_loaded", "telemetry_schema_version",
    }
    assert required <= d.keys()


def test_config_fields_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AI_TELEMETRY_ENABLED", "true")
    monkeypatch.setenv("AI_TELEMETRY_SINK", "file:///tmp/t.jsonl")
    # Clear any other AI_ vars that would cause ConfigError
    for k in list(os.environ):
        if k.startswith("AI_") and k not in ("AI_TELEMETRY_ENABLED", "AI_TELEMETRY_SINK"):
            monkeypatch.delenv(k, raising=False)
    from ai_pr_review.config import ReviewConfig
    config = ReviewConfig.from_env()
    assert config.telemetry_enabled is True
    assert config.telemetry_sink == "file:///tmp/t.jsonl"


def test_file_sink_oserror_swallowed(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """An OSError writing the file is logged as WARNING and not re-raised."""
    sink = f"file://{tmp_path}/subdir/that/does/not/exist/t.jsonl"
    with caplog.at_level(logging.WARNING):
        emit_telemetry(_sample_event(), sink=sink)
    # Should not raise; warning should appear
    assert any("telemetry" in r.message.lower() or "file" in r.message.lower() for r in caplog.records)


def test_file_sink_serialization_error_swallowed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A TypeError from json.dumps (non-serialisable field) is logged and not re-raised."""
    import json as _json
    from unittest.mock import patch as _patch
    sink = f"file://{tmp_path}/t.jsonl"
    with (
        caplog.at_level(logging.WARNING),
        _patch.object(_json, "dumps", side_effect=TypeError("not serialisable")),
    ):
        emit_telemetry(_sample_event(), sink=sink)
    assert not (tmp_path / "t.jsonl").exists()
    assert any("serialis" in r.message.lower() or "json" in r.message.lower() for r in caplog.records)


def test_config_telemetry_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """telemetry_enabled defaults to False — no AI_TELEMETRY_ENABLED set."""
    for k in list(os.environ):
        if k.startswith("AI_"):
            monkeypatch.delenv(k, raising=False)
    from ai_pr_review.config import ReviewConfig
    config = ReviewConfig.from_env()
    assert config.telemetry_enabled is False
    assert config.telemetry_sink == ""


def test_event_agent_latency_ms_populated(tmp_path: Path) -> None:
    """TelemetryEvent with agent_latency_ms dict is serialised and round-trips."""
    sink = f"file://{tmp_path}/t.jsonl"
    event = _sample_event(agent_latency_ms={"code-reviewer": 1250, "security-reviewer": 3400})
    emit_telemetry(event, sink=sink)
    data = json.loads((tmp_path / "t.jsonl").read_text())
    assert data["agent_latency_ms"] == {"code-reviewer": 1250, "security-reviewer": 3400}
