"""Structured logging for the AI PR Review Python engine.

Provides setup_logging() which must be called once at process startup (cli.py).
Supports JSON (AI_LOG_FORMAT=json) and human-readable formats. Injects a
correlation ID into every log record via CorrelationFilter. Masks secrets
before emission via SecretMaskingFormatter.
"""

from __future__ import annotations

import contextlib
import contextvars
import copy
import json
import logging
import re
import sys
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
# Group 3 stops at common delimiters (,;'") to preserve surrounding structure.
_ENV_VAR_RE = re.compile(
    r"(?i)((?:[A-Z_]*(?:API_KEY|TOKEN|SECRET|PASSWORD|_KEY)\b)[^\s=]*)(\s*[=:]\s*)([^\s,;'\"]+)"
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
    # Layer 1 — preserve the original separator (= or :) so TOKEN: value → TOKEN: <redacted>
    text = _ENV_VAR_RE.sub(lambda m: m.group(1) + m.group(2) + "<redacted>", text)
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
        # Ensure correlation_id is present so %(correlation_id)s in the human
        # format string does not raise KeyError for records from loggers outside
        # the ai_pr_review hierarchy (e.g. propagated root-logger records).
        if not hasattr(record, "correlation_id"):
            record.correlation_id = ""
        if self._json_format:
            return self._format_json(record)
        rendered = super().format(record)
        return _mask_secrets(rendered, secret_literals=self._secret_literals)

    def _format_json(self, record: logging.LogRecord) -> str:
        # Work on a shallow copy so this formatter's mutations (exc_text clearing,
        # exc_info rendering, message assembly) do not affect the original LogRecord
        # seen by other handlers — the copy protects the original, not the reverse.
        record = copy.copy(record)
        # Render the message.  The entire block is wrapped in a broad except so
        # format() never raises regardless of LogRecord state — a crash in the
        # formatter would propagate to handleError() and could leak unmasked
        # content to stderr.  TypeError catches %-formatting arity mismatches;
        # the outer except catches anything unusual in record.args rendering.
        try:
            message = record.getMessage()
        except Exception as fmt_exc:
            try:
                args = record.args
                if isinstance(args, dict):
                    rendered_args = str(args)
                elif args:
                    rendered_args = " ".join(
                        str(a) for a in (args if isinstance(args, tuple) else (args,))
                    )
                else:
                    rendered_args = ""
                message = (
                    f"{record.msg!r} {rendered_args}"
                    f" [FORMATTING ERROR: {type(fmt_exc).__name__}: {fmt_exc}]"
                ).strip()
            except Exception as inner_exc:
                # Avoid record.msg!r — broken __repr__ raises again. Use safe stdlib attrs.
                message = (
                    f"[UNFORMATTABLE LOG RECORD logger={record.name} level={record.levelname}]"
                    f" {type(fmt_exc).__name__}: {fmt_exc}"
                    f" (secondary: {type(inner_exc).__name__}: {inner_exc})"
                )
        # Clear exc_text before formatting so a previously-cached value from a
        # non-masking formatter cannot bypass secret redaction here.
        record.exc_text = None
        if record.exc_info:
            try:
                record.exc_text = self.formatException(record.exc_info)
            except Exception:
                record.exc_text = "[exception formatting failed]"
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
    # Close managed handlers to release file descriptors before discarding them.
    for h in list(pkg_logger.handlers):
        if getattr(h, "_ai_pr_review_managed", False):
            with contextlib.suppress(Exception):
                h.close()
            pkg_logger.removeHandler(h)

    # Remove any CorrelationFilter instances previously added to pkg_logger so
    # they don't accumulate across repeated setup_logging() calls.
    pkg_logger.filters = [
        f for f in pkg_logger.filters if not isinstance(f, CorrelationFilter)
    ]

    handler = logging.StreamHandler()
    handler._ai_pr_review_managed = True  # type: ignore[attr-defined]
    handler.setFormatter(formatter)
    # CorrelationFilter on the handler so it fires for records propagated from
    # child loggers — Python's callHandlers skips parent-logger filters when
    # propagating, so a filter only on the logger would be a no-op for any
    # ai_pr_review.* child logger.  The filter is also added to pkg_logger so
    # that any external handler later attached directly to pkg_logger also
    # receives records with correlation_id set.
    # Note: secret masking only covers ai_pr_review.* log lines. Records from
    # third-party libraries (httpx, anyio, etc.) are not routed through
    # SecretMaskingFormatter and are therefore out of scope for masking here.
    # Callers must not log raw credential values through third-party loggers.
    corr_filter = CorrelationFilter()
    handler.addFilter(corr_filter)
    pkg_logger.addFilter(corr_filter)
    pkg_logger.addHandler(handler)
    try:
        pkg_logger.setLevel(log_level)
    except ValueError:
        pkg_logger.setLevel(logging.WARNING)
        # Write directly to stderr — the logger level is not yet set, so a
        # logger.warning() call here could be suppressed by the default level.
        print(
            f"[ai-pr-review] WARNING: unrecognised log level {log_level!r},"
            " falling back to WARNING",
            file=sys.stderr,
        )
    pkg_logger.propagate = False
