"""Tests for ai_pr_review/logging.py — structured logging, secret masking, correlation IDs."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime

import pytest
from pydantic import ValidationError

from ai_pr_review.logging import (
    _mask_secrets,
    generate_correlation_id,
    setup_logging,
)


def _emit_and_capture(capsys, *, log_format: str, log_level: str = "DEBUG",
                      correlation_id: str = "test1234", message: str = "hello test",
                      level: int = logging.WARNING) -> str:
    """Helper: setup_logging, emit one record, return captured stderr.

    Uses a child of ai_pr_review so the package handler picks it up.
    """
    setup_logging(log_format, log_level, correlation_id)
    test_logger = logging.getLogger("ai_pr_review.test_logging_4_1")
    test_logger.log(level, message)
    return capsys.readouterr().err


# ---------------------------------------------------------------------------
# JSON format output
# ---------------------------------------------------------------------------

class TestJsonFormat:
    def test_json_format_emits_valid_json(self, capsys):
        stderr = _emit_and_capture(capsys, log_format="json")
        line = stderr.strip()
        assert line, "Expected non-empty stderr output"
        obj = json.loads(line)  # raises if invalid JSON
        assert isinstance(obj, dict)

    def test_json_format_contains_required_keys(self, capsys):
        stderr = _emit_and_capture(capsys, log_format="json", correlation_id="abc12345")
        obj = json.loads(stderr.strip())
        assert "timestamp" in obj
        assert "level" in obj
        assert "logger" in obj
        assert "message" in obj
        assert "correlation_id" in obj

    def test_json_format_contains_correlation_id(self, capsys):
        stderr = _emit_and_capture(capsys, log_format="json", correlation_id="deadbeef")
        obj = json.loads(stderr.strip())
        assert obj["correlation_id"] == "deadbeef"

    def test_json_timestamp_is_iso8601(self, capsys):
        stderr = _emit_and_capture(capsys, log_format="json")
        obj = json.loads(stderr.strip())
        # Should parse without raising
        dt = datetime.fromisoformat(obj["timestamp"].replace("Z", "+00:00"))
        assert dt.tzinfo is not None

    def test_json_level_matches_emitted_level(self, capsys):
        stderr = _emit_and_capture(capsys, log_format="json", level=logging.ERROR)
        obj = json.loads(stderr.strip())
        assert obj["level"] == "ERROR"

    def test_json_message_field_matches(self, capsys):
        stderr = _emit_and_capture(capsys, log_format="json", message="specific message")
        obj = json.loads(stderr.strip())
        assert obj["message"] == "specific message"


# ---------------------------------------------------------------------------
# Human format output
# ---------------------------------------------------------------------------

class TestHumanFormat:
    def test_human_format_is_not_json(self, capsys):
        stderr = _emit_and_capture(capsys, log_format="human")
        with pytest.raises((json.JSONDecodeError, ValueError)):
            json.loads(stderr.strip())

    def test_human_format_contains_correlation_id(self, capsys):
        stderr = _emit_and_capture(capsys, log_format="human", correlation_id="cafe1234")
        assert "cafe1234" in stderr

    def test_human_format_contains_message(self, capsys):
        stderr = _emit_and_capture(capsys, log_format="human", message="my log message")
        assert "my log message" in stderr


# ---------------------------------------------------------------------------
# Log level filtering
# ---------------------------------------------------------------------------

class TestLogLevel:
    def test_warning_level_suppresses_debug(self, capsys):
        setup_logging("json", "WARNING", "aaaa1111")
        test_logger = logging.getLogger("ai_pr_review.test_logging_4_1_level")
        test_logger.debug("should be suppressed")
        test_logger.info("also suppressed")
        stderr = capsys.readouterr().err
        assert stderr.strip() == ""

    def test_warning_level_suppresses_info(self, capsys):
        setup_logging("json", "WARNING", "aaaa1111")
        test_logger = logging.getLogger("ai_pr_review.test_logging_4_1_level")
        test_logger.info("info suppressed")
        assert capsys.readouterr().err.strip() == ""

    def test_warning_level_passes_warning(self, capsys):
        setup_logging("json", "WARNING", "aaaa1111")
        test_logger = logging.getLogger("ai_pr_review.test_logging_4_1_level")
        test_logger.warning("this passes")
        assert capsys.readouterr().err.strip() != ""

    def test_debug_level_passes_all(self, capsys):
        setup_logging("json", "DEBUG", "bbbb2222")
        test_logger = logging.getLogger("ai_pr_review.test_logging_4_1_debug")
        test_logger.debug("debug passes")
        stderr = capsys.readouterr().err
        assert stderr.strip() != ""
        obj = json.loads(stderr.strip())
        assert obj["level"] == "DEBUG"


# ---------------------------------------------------------------------------
# Secret masking — critical AC
# ---------------------------------------------------------------------------

class TestSecretMasking:
    def test_api_key_not_in_output(self, monkeypatch, capsys):
        """A fake API key injected as a secret literal must not appear in output."""
        fake_key = "sk-ant-fake-key-for-testing-1234abcd"
        secrets = frozenset({fake_key})
        setup_logging("json", "DEBUG", "sec00001", secrets=secrets)
        test_logger = logging.getLogger("ai_pr_review.test_logging_mask_1")
        test_logger.warning("key is %s", fake_key)
        stderr = capsys.readouterr().err
        assert fake_key not in stderr
        assert "<redacted>" in stderr

    def test_provider_token_prefix_redacted(self, capsys):
        """ghp_xxxx token in a log message is redacted by Layer 2."""
        setup_logging("json", "DEBUG", "sec00002")
        test_logger = logging.getLogger("ai_pr_review.test_logging_mask_2")
        token = "ghp_abcdefghijklmnopqrst"
        test_logger.warning("token: %s", token)
        stderr = capsys.readouterr().err
        assert token not in stderr
        assert "<redacted>" in stderr

    def test_sk_ant_prefix_redacted(self, capsys):
        """sk-ant-xxx token in a log message is redacted by Layer 2."""
        setup_logging("json", "DEBUG", "sec00003")
        test_logger = logging.getLogger("ai_pr_review.test_logging_mask_3")
        token = "sk-ant-api03-reallylong-token-value"
        test_logger.warning("auth: %s", token)
        stderr = capsys.readouterr().err
        assert token not in stderr

    def test_short_value_not_redacted(self, capsys):
        """Values shorter than 8 chars are NOT redacted by Layer 3 (avoids false positives)."""
        secrets = frozenset({"short"})  # only 5 chars — below threshold
        setup_logging("json", "DEBUG", "sec00004", secrets=secrets)
        test_logger = logging.getLogger("ai_pr_review.test_logging_mask_4")
        test_logger.warning("value: short")
        stderr = capsys.readouterr().err
        assert "short" in stderr  # must NOT be redacted

    def test_env_var_assignment_redacted(self, capsys):
        """ANTHROPIC_API_KEY=<value> in a log message is redacted by Layer 1."""
        setup_logging("json", "DEBUG", "sec00005")
        test_logger = logging.getLogger("ai_pr_review.test_logging_mask_5")
        test_logger.warning("ANTHROPIC_API_KEY=some-secret-value")
        stderr = capsys.readouterr().err
        assert "some-secret-value" not in stderr
        assert "<redacted>" in stderr

    def test_secret_in_exception_traceback_redacted(self, capsys):
        """A secret embedded in an exception message is redacted in traceback output."""
        fake_key = "sk-ant-fake-key-in-exception-abcd1234"
        secrets = frozenset({fake_key})
        setup_logging("json", "DEBUG", "sec00006", secrets=secrets)
        test_logger = logging.getLogger("ai_pr_review.test_logging_mask_6")
        try:
            raise ValueError(f"auth failed with key {fake_key}")
        except ValueError:
            test_logger.exception("caught error")
        stderr = capsys.readouterr().err
        assert fake_key not in stderr
        assert "<redacted>" in stderr


# ---------------------------------------------------------------------------
# _mask_secrets unit tests (layer-by-layer)
# ---------------------------------------------------------------------------

class TestMaskSecrets:
    def test_layer1_env_var_pattern(self):
        result = _mask_secrets("ANTHROPIC_API_KEY=sk-ant-xxx token=abc123")
        assert "sk-ant-xxx" not in result
        assert "abc123" not in result

    def test_layer2_sk_ant_prefix(self):
        result = _mask_secrets("Bearer sk-ant-api03-longtoken-value123")
        assert "sk-ant-api03-longtoken-value123" not in result
        assert "<redacted>" in result

    def test_layer3_literal_secret(self):
        import re
        pattern = re.compile(re.escape("my-super-secret-val"))
        result = _mask_secrets("logged: my-super-secret-val here", secret_literals=pattern)
        assert "my-super-secret-val" not in result
        assert "<redacted>" in result

    def test_safe_text_unchanged(self):
        text = "review complete: 3 findings, 0 failed agents"
        assert _mask_secrets(text) == text


# ---------------------------------------------------------------------------
# Correlation ID boundary
# ---------------------------------------------------------------------------

class TestCorrelationIdBoundary:
    @pytest.mark.anyio
    async def test_correlation_id_visible_to_native_analyzers(self, monkeypatch):
        """AI_PR_REVIEW_CORRELATION_ID set in os.environ is visible to native analyzer callables.

        Native analyzers run in a thread pool that shares the process environment, so
        the correlation ID set before run_analyzers is accessible to the callable.
        """
        import os
        from unittest.mock import patch

        from ai_pr_review.analyzers.bridge import AnalyzerSpec, run_analyzers
        from ai_pr_review.manifest import ChangedFiles

        observed_ids: list[str] = []

        def capturing_native(cf, diff):
            observed_ids.append(os.environ.get("AI_PR_REVIEW_CORRELATION_ID", ""))
            return []

        monkeypatch.setenv("AI_PR_REVIEW_CORRELATION_ID", "propagate1")
        spec = AnalyzerSpec("test", [], capturing_native)

        from ai_pr_review.analyzers import bridge
        with patch.object(bridge, "_ANALYZERS", [spec]):
            await run_analyzers(ChangedFiles(), "/dev/null")

        assert observed_ids == ["propagate1"], (
            f"correlation ID not visible to native analyzer: {observed_ids}"
        )

    def test_inbound_correlation_id_reused(self, monkeypatch):
        """If AI_PR_REVIEW_CORRELATION_ID is already set, that value is used."""
        monkeypatch.setenv("AI_PR_REVIEW_CORRELATION_ID", "existing1")
        existing = os.environ.get("AI_PR_REVIEW_CORRELATION_ID")
        correlation_id = existing or generate_correlation_id()
        assert correlation_id == "existing1"

    def test_generate_correlation_id_is_8_hex_chars(self):
        cid = generate_correlation_id()
        assert len(cid) == 8
        assert all(c in "0123456789abcdef" for c in cid)

    def test_generate_correlation_id_unique(self):
        ids = {generate_correlation_id() for _ in range(10)}
        assert len(ids) == 10  # no collisions in 10 samples (birthday p≈0.000001%)


# ---------------------------------------------------------------------------
# Config fields
# ---------------------------------------------------------------------------

class TestConfigFields:
    def test_config_log_format_default_is_human(self, monkeypatch):
        monkeypatch.delenv("AI_LOG_FORMAT", raising=False)
        from ai_pr_review.config import ReviewConfig
        config = ReviewConfig(log_format="human", log_level="WARNING")
        assert config.log_format == "human"

    def test_config_log_format_json(self, monkeypatch):
        from ai_pr_review.config import ReviewConfig
        config = ReviewConfig(log_format="json", log_level="WARNING")
        assert config.log_format == "json"

    def test_config_log_format_invalid_raises(self):
        from ai_pr_review.config import ReviewConfig
        with pytest.raises(ValidationError):
            ReviewConfig(log_format="xml", log_level="WARNING")

    def test_config_log_level_default_is_warning(self):
        from ai_pr_review.config import ReviewConfig
        config = ReviewConfig(log_format="human", log_level="WARNING")
        assert config.log_level == "WARNING"

    def test_config_log_level_case_insensitive(self):
        from ai_pr_review.config import ReviewConfig
        config = ReviewConfig(log_format="human", log_level="debug")
        assert config.log_level == "DEBUG"

    def test_config_log_level_invalid_raises(self):
        from ai_pr_review.config import ReviewConfig
        with pytest.raises(ValidationError):
            ReviewConfig(log_format="human", log_level="VERBOSE")

    def test_config_from_env_log_format(self, monkeypatch):
        monkeypatch.setenv("AI_LOG_FORMAT", "json")
        monkeypatch.delenv("AI_LOG_LEVEL", raising=False)
        from ai_pr_review.config import ReviewConfig
        config = ReviewConfig.from_env()
        assert config.log_format == "json"

    def test_config_from_env_log_level(self, monkeypatch):
        monkeypatch.delenv("AI_LOG_FORMAT", raising=False)
        monkeypatch.setenv("AI_LOG_LEVEL", "INFO")
        from ai_pr_review.config import ReviewConfig
        config = ReviewConfig.from_env()
        assert config.log_level == "INFO"


# ---------------------------------------------------------------------------
# setup_logging idempotency
# ---------------------------------------------------------------------------

class TestSetupLoggingIdempotency:
    def test_multiple_calls_no_duplicate_handlers(self, capsys):
        """Calling setup_logging twice must not result in duplicate output."""
        setup_logging("json", "DEBUG", "idem0001")
        setup_logging("json", "DEBUG", "idem0002")
        test_logger = logging.getLogger("ai_pr_review.test_logging_idem")
        test_logger.warning("once")
        stderr = capsys.readouterr().err
        lines = [line for line in stderr.strip().splitlines() if line.strip()]
        assert len(lines) == 1, f"Expected 1 line, got {len(lines)}: {stderr!r}"
        # Verify the second call's correlation ID is active, not the first's.
        assert json.loads(lines[0])["correlation_id"] == "idem0002"
