"""Tests for ai_pr_review.vcs.http: retry, tape recording, redaction."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from ai_pr_review.vcs.http import (
    RecordingClient,
    RetryExhaustedError,
    RetryPolicy,
    TapeRecorder,
    redact_secrets,
    retry_transient,
)

# ---------------------------------------------------------------------------
# retry_transient
# ---------------------------------------------------------------------------


def _response(status: int, body: str = "") -> httpx.Response:
    return httpx.Response(status_code=status, text=body, request=httpx.Request("GET", "http://x"))


def test_retry_returns_first_success() -> None:
    calls = {"n": 0}

    def call() -> httpx.Response:
        calls["n"] += 1
        return _response(200, "ok")

    resp = retry_transient(call, policy=RetryPolicy(attempts=3, sleep=lambda _s: None))
    assert resp.status_code == 200
    assert calls["n"] == 1


def test_retry_recovers_after_transient_failures() -> None:
    sequence = [_response(502), _response(503), _response(200, "ok")]

    def call() -> httpx.Response:
        return sequence.pop(0)

    resp = retry_transient(
        call, policy=RetryPolicy(attempts=3, base_backoff=0, jitter=False, sleep=lambda _s: None)
    )
    assert resp.status_code == 200
    assert sequence == []


def test_retry_exhausted_raises() -> None:
    def call() -> httpx.Response:
        return _response(503)

    with pytest.raises(RetryExhaustedError, match="Retry exhausted"):
        retry_transient(
            call,
            policy=RetryPolicy(attempts=3, base_backoff=0, jitter=False, sleep=lambda _s: None),
        )


def test_retry_non_transient_4xx_returns_immediately() -> None:
    calls = {"n": 0}

    def call() -> httpx.Response:
        calls["n"] += 1
        return _response(404)

    resp = retry_transient(call, policy=RetryPolicy(attempts=3, sleep=lambda _s: None))
    assert resp.status_code == 404
    assert calls["n"] == 1


def test_retry_transient_network_error_then_success() -> None:
    called = [0]

    def call() -> httpx.Response:
        called[0] += 1
        if called[0] == 1:
            raise httpx.ConnectError("boom", request=httpx.Request("GET", "http://x"))
        return _response(200, "ok")

    resp = retry_transient(
        call,
        policy=RetryPolicy(attempts=3, base_backoff=0, jitter=False, sleep=lambda _s: None),
    )
    assert resp.status_code == 200
    assert called[0] == 2


def test_retry_non_transient_exception_reraises() -> None:
    def call() -> httpx.Response:
        raise ValueError("programmer error")

    with pytest.raises(ValueError, match="programmer error"):
        retry_transient(
            call,
            policy=RetryPolicy(attempts=3, base_backoff=0, jitter=False, sleep=lambda _s: None),
        )


# ---------------------------------------------------------------------------
# redact_secrets
# ---------------------------------------------------------------------------


def test_redact_authorization_header() -> None:
    redacted = redact_secrets("Authorization: Bearer abc123")
    assert "abc123" not in redacted
    assert "Authorization: <redacted>" in redacted


def test_redact_github_token_value() -> None:
    redacted = redact_secrets("token ghp_XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX abc")
    assert "ghp_" not in redacted
    assert "<redacted>" in redacted


def test_redact_gitlab_pat() -> None:
    redacted = redact_secrets("Private-Token: glpat-deadbeefdeadbeef1234")
    assert "glpat-" not in redacted
    assert "<redacted>" in redacted


def test_redact_no_op_for_clean_text() -> None:
    assert redact_secrets("hello world") == "hello world"


def test_redact_github_actions_gho_token() -> None:
    redacted = redact_secrets("gho_XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX")
    assert "gho_" not in redacted
    assert "<redacted>" in redacted


def test_redact_github_pat_token() -> None:
    redacted = redact_secrets("github_pat_XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX")
    assert "github_pat_" not in redacted
    assert "<redacted>" in redacted


def test_redact_gitlab_ci_token() -> None:
    redacted = redact_secrets("glcbt-XXXXXXXXXXXXXXXXXXXXXXXXXX")
    assert "glcbt-" not in redacted
    assert "<redacted>" in redacted


def test_redact_job_token_header() -> None:
    redacted = redact_secrets("JOB-TOKEN: supersecretjobtoken123")
    assert "supersecretjobtoken123" not in redacted
    assert "<redacted>" in redacted


# ---------------------------------------------------------------------------
# TapeRecorder
# ---------------------------------------------------------------------------


def test_tape_recorder_no_op_when_dir_none() -> None:
    rec = TapeRecorder(record_dir=None)
    rec.record("GET", "http://x/y", None, _response(200, "ok"))
    # no exception, no side effect to verify


def test_tape_recorder_writes_files(tmp_path: Path) -> None:
    rec = TapeRecorder(record_dir=tmp_path, provider="github")
    rec.record("GET", "https://api.github.com/repos/o/r/pulls/1", None, _response(200, '{"x":1}'))
    rec.record(
        "POST",
        "https://api.github.com/repos/o/r/pulls/1/reviews",
        '{"body":"hi"}',
        _response(201, '{"id":42}'),
    )
    files = sorted(tmp_path.iterdir())
    assert len(files) == 2
    first = json.loads(files[0].read_text())
    assert first["seq"] == 1
    assert first["method"] == "GET"
    assert first["status"] == 200
    assert first["url"].endswith("/pulls/1")
    second = json.loads(files[1].read_text())
    assert second["seq"] == 2
    assert second["method"] == "POST"
    assert second["request_body"] == '{"body":"hi"}'


def test_tape_recorder_redacts_request_body(tmp_path: Path) -> None:
    rec = TapeRecorder(record_dir=tmp_path)
    rec.record(
        "POST",
        "https://api.github.com/x",
        'Authorization: Bearer ghp_SECRETTOKEN123456789012',
        _response(200, "ok"),
    )
    payload = json.loads(next(tmp_path.iterdir()).read_text())
    assert "ghp_" not in payload["request_body"]
    assert "SECRETTOKEN" not in payload["request_body"]


def test_tape_recorder_from_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "tapes"
    monkeypatch.setenv("AI_PR_REVIEW_RECORD_DIR", str(target))
    rec = TapeRecorder.from_env()
    assert rec.record_dir == target
    assert target.exists()


def test_tape_recorder_from_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AI_PR_REVIEW_RECORD_DIR", raising=False)
    rec = TapeRecorder.from_env()
    assert rec.record_dir is None


# ---------------------------------------------------------------------------
# RecordingClient
# ---------------------------------------------------------------------------


def test_recording_client_records_requests(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport, base_url="https://api.github.com")
    rec = TapeRecorder(record_dir=tmp_path)
    rc = RecordingClient(
        http=client,
        recorder=rec,
        retry_policy=RetryPolicy(attempts=2, base_backoff=0, jitter=False, sleep=lambda _s: None),
    )
    resp = rc.request("GET", "/repos/o/r")
    assert resp.status_code == 200
    files = list(tmp_path.iterdir())
    assert len(files) == 1
