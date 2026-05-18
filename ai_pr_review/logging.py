"""Structured logging for the AI PR Review Python engine.

Provides setup_logging() which must be called once at process startup (cli.py).
Supports JSON (AI_LOG_FORMAT=json) and human-readable formats. Injects a
correlation ID into every log record via CorrelationFilter. Masks secrets
before emission via SecretMaskingFormatter.
"""

from __future__ import annotations

import contextvars
import json
import logging
import re
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

# Module-level ContextVar holding the current correlation ID.
# Set once in setup_logging(); readable by CorrelationFilter from any coroutine.
_correlation_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "correlation_id", default=""
)

# Layer 2: provider token prefix patterns (applied globally, not just at setup time).
_TOKEN_PREFIX_RE = re.compile(
    r"\b("
    r"sk-ant-[A-Za-z0-9\-]{10,}"
    r"|sk-[A-Za-z0-9]{10,}"
    r"|ghp_[A-Za-z0-9]{10,}"
    r"|ghs_[A-Za-z0-9]{10,}"
    r"|github_pat_[A-Za-z0-9_]{10,}"
    r"|glpat-[A-Za-z0-9_\-]{10,}"
    r"|glcbt-[A-Za-z0-9_\-]{10,}"
    r")"
)

# Layer 1: env-var name=value patterns.
_ENV_VAR_RE = re.compile(
    r"(?i)((?:[A-Z_]*(?:API_KEY|TOKEN|SECRET|PASSWORD|_KEY)\b)[^\s=]*)\s*[=:]\s*(\S+)"
)


def generate_correlation_id() -> str:
    """Return a new short correlation ID (8 hex chars from uuid4)."""
    return uuid.uuid4().hex[:8]


def _mask_secrets(text: str, *, secret_literals: re.Pattern[str] | None = None) -> str:
    """Redact known secret patterns from a string.

    Three layers applied in order:
    1. Env-var name=value patterns (ANTHROPIC_API_KEY=abc → ANTHROPIC_API_KEY=<redacted>)
    2. Provider token prefixes (sk-ant-xxx → <redacted>)
    3. Literal secret values compiled at setup time (Layer 3 pattern passed in)
    """
    # Layer 1
    text = _ENV_VAR_RE.sub(lambda m: m.group(1) + "=<redacted>", text)
    # Layer 2
    text = _TOKEN_PREFIX_RE.sub("<redacted>", text)
    # Layer 3
    if secret_literals is not None:
        text = secret_literals.sub("<redacted>", text)
    return text


class CorrelationFilter(logging.Filter):
    """Injects correlation_id from the ContextVar into every LogRecord."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = _correlation_id_var.get("")
        return True


class SecretMaskingFormatter(logging.Formatter):
    """Formatter that redacts secrets from the fully-rendered log line."""

    def __init__(
        self,
        fmt: str | None = None,
        datefmt: str | None = None,
        *,
        json_format: bool = False,
        secret_literals: re.Pattern[str] | None = None,
    ) -> None:
        super().__init__(fmt=fmt, datefmt=datefmt)
        self._json_format = json_format
        self._secret_literals = secret_literals

    def format(self, record: logging.LogRecord) -> str:
        if self._json_format:
            return self._format_json(record)
        rendered = super().format(record)
        return _mask_secrets(rendered, secret_literals=self._secret_literals)

    def _format_json(self, record: logging.LogRecord) -> str:
        # Render the message so that exception tracebacks are captured in the
        # message field.  Narrow to TypeError (%-formatting arity mismatch) and
        # include the exception type so malformed log calls are visible in output
        # rather than silently emitting a raw template string.  Attempt to render
        # record.args into the fallback so masking can still apply to arg values.
        try:
            message = record.getMessage()
        except TypeError as fmt_exc:
            try:
                args = record.args
                rendered_args = " ".join(
                    str(a)
                    for a in (args if isinstance(args, tuple) else (args,))
                ) if args else ""
                message = (
                    f"{record.msg!r} {rendered_args} [FORMATTING ERROR: {type(fmt_exc).__name__}]"
                ).strip()
            except Exception:
                message = f"{record.msg!r} [FORMATTING ERROR: {type(fmt_exc).__name__}]"
        # Clear exc_text before formatting so a previously-cached value from a
        # non-masking formatter cannot bypass secret redaction here.
        record.exc_text = None
        if record.exc_info:
            record.exc_text = self.formatException(record.exc_info)
        if record.exc_text:
            message = message + "\n" + record.exc_text
        if record.stack_info:
            message = message + "\n" + self.formatStack(record.stack_info)

        message = _mask_secrets(message, secret_literals=self._secret_literals)

        obj = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=UTC
            ).isoformat(timespec="microseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": message,
            "correlation_id": getattr(record, "correlation_id", ""),
        }
        return json.dumps(obj)


def setup_logging(
    log_format: str,
    log_level: str,
    correlation_id: str,
    *,
    secrets: frozenset[str] | None = None,
) -> None:
    """Configure the ai_pr_review package logger for this process.

    Must be called once at process startup (cli.py review/compute/slash commands).
    Idempotent: removes only handlers previously installed by this function
    (identified by the ``_ai_pr_review_managed`` sentinel attribute) so external
    handlers (Sentry, file handlers added by deployment infrastructure) are not
    disturbed on reconfiguration.

    Sets ``propagate=False`` on the ``ai_pr_review`` package logger so records
    are not doubled by root-logger handlers in production.  In tests, records
    logged to ``ai_pr_review.*`` loggers are captured by ``caplog`` only when
    ``caplog.at_level(..., logger='ai_pr_review')`` is used (or via the
    ``_reset_pkg_logger`` fixture in conftest.py which restores propagate after
    each test).

    Args:
        log_format: "json" or "human".
        log_level: stdlib level name ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL").
        correlation_id: ID to inject into every log record.
        secrets: Optional frozenset of literal secret values to redact (Layer 3).
                 Values shorter than 8 characters are ignored to avoid false positives.
    """
    # Store correlation ID in ContextVar so CorrelationFilter can read it.
    _correlation_id_var.set(correlation_id)

    # Build Layer 3 pattern from caller-supplied secret values.
    secret_literals: re.Pattern[str] | None = None
    if secrets:
        long_secrets = [s for s in secrets if len(s) >= 8]
        if long_secrets:
            pattern = "|".join(re.escape(s) for s in sorted(long_secrets, key=len, reverse=True))
            secret_literals = re.compile(pattern)

    # Build formatter.
    if log_format == "json":
        formatter = SecretMaskingFormatter(
            json_format=True,
            secret_literals=secret_literals,
        )
    else:
        formatter = SecretMaskingFormatter(
            fmt="%(asctime)s %(levelname)-8s [%(correlation_id)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
            json_format=False,
            secret_literals=secret_literals,
        )

    pkg_logger = logging.getLogger("ai_pr_review")

    # Remove only handlers we previously installed; leave external handlers intact.
    pkg_logger.handlers = [
        h for h in pkg_logger.handlers
        if not getattr(h, "_ai_pr_review_managed", False)
    ]

    handler = logging.StreamHandler()
    handler._ai_pr_review_managed = True  # type: ignore[attr-defined]
    handler.setFormatter(formatter)
    # Add CorrelationFilter to both the handler and the logger so any handler
    # later attached to the logger also receives records with correlation_id set.
    handler.addFilter(CorrelationFilter())
    pkg_logger.addHandler(handler)
    pkg_logger.addFilter(CorrelationFilter())
    pkg_logger.setLevel(log_level)
    pkg_logger.propagate = False
