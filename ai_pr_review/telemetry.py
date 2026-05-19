"""Structured telemetry emission — E4.S2.

Emits one JSON object per review run to a local file or HTTP endpoint.
Off by default; enabled via AI_TELEMETRY_ENABLED=true.
Supported sinks: file:///path/to/file.jsonl, https://..., http://...

Telemetry must never abort a review. All I/O errors are logged as WARNING
and silently swallowed. The caller in cli.py wraps the call in try/except.
"""

from __future__ import annotations

import dataclasses
import json
import logging

import httpx

logger = logging.getLogger(__name__)


@dataclasses.dataclass
class TelemetryEvent:
    """Structured event emitted after each review run (schema version 1)."""

    correlation_id: str
    timestamp: str
    repository: str
    pr_number: str
    outcome: str
    findings_count: int
    findings_by_severity: dict[str, int]
    failed_agents: list[str]
    token_usage_by_agent: dict[str, dict[str, object]]
    agent_latency_ms: dict[str, int]
    sarif_elapsed_s: float | None
    learning_store_entries_loaded: int
    telemetry_schema_version: str


def emit_telemetry(event: TelemetryEvent, *, sink: str) -> None:
    """Emit *event* to *sink*.

    Routes to ``_emit_file`` for ``file://`` sinks or ``_emit_http`` for
    ``http://``/``https://`` sinks. Logs a WARNING and returns silently for
    empty or unrecognised sink schemes — telemetry must never abort a review.
    """
    if not sink:
        logger.warning(
            "telemetry: AI_TELEMETRY_SINK is empty; skipping emission. "
            "Set AI_TELEMETRY_SINK to a file:// path or http(s):// URL to receive events."
        )
        return
    if sink.startswith("file://"):
        _emit_file(event, sink[len("file://"):])
    elif sink.startswith("http://") or sink.startswith("https://"):
        _emit_http(event, sink)
    else:
        logger.warning(
            "telemetry: unrecognised sink scheme %r; supported schemes: file://, http://, https://. "
            "Telemetry will not be emitted.",
            sink,
        )


def _emit_file(event: TelemetryEvent, path: str) -> None:
    """Append one JSON line to *path* (created if absent, appended if exists)."""
    if not path:
        logger.warning(
            "telemetry: file:// sink has empty path; check AI_TELEMETRY_SINK value "
            "(expected format: file:///absolute/path/to/file.jsonl)"
        )
        return
    if not path.startswith("/"):
        logger.warning(
            "telemetry: file:// sink path %r is relative; use an absolute path "
            "(expected format: file:///absolute/path/to/file.jsonl)",
            path,
        )
        return
    try:
        payload = json.dumps(dataclasses.asdict(event))
    except (TypeError, ValueError) as exc:
        logger.warning("telemetry: could not serialise event to JSON: %s", exc, exc_info=True)
        return
    try:
        with open(path, "a") as fh:
            fh.write(payload + "\n")
    except OSError as exc:
        logger.warning("telemetry: could not write to file %r (%s): %s",
                       path, type(exc).__name__, exc, exc_info=True)


def _emit_http(event: TelemetryEvent, url: str) -> None:
    """POST *event* as JSON to *url*. Swallows network and HTTP errors."""
    try:
        response = httpx.post(url, json=dataclasses.asdict(event), timeout=5.0)
    except (httpx.TimeoutException, httpx.NetworkError) as exc:
        logger.warning("telemetry: HTTP POST to %r failed (transient): %s", url, exc)
        return
    except httpx.HTTPError as exc:
        logger.warning("telemetry: HTTP POST to %r failed: %s", url, exc)
        return
    except Exception as exc:
        logger.warning("telemetry: HTTP POST to %r failed (unexpected: %s): %s",
                       url, type(exc).__name__, exc, exc_info=True)
        return
    if response.status_code >= 500:
        logger.warning(
            "telemetry: HTTP POST to %r returned %d; telemetry receiver returned a server error",
            url, response.status_code,
        )
    elif response.status_code >= 400:
        logger.warning(
            "telemetry: HTTP POST to %r returned %d; check endpoint configuration",
            url, response.status_code,
        )
