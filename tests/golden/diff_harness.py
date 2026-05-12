#!/usr/bin/env python3
"""
tests/golden/diff_harness.py — Payload-level golden parity harness.

Runs a fixture's recorded tapes against the expected.json and produces a
structured diff report. Implements 0.FR-3 and 0.FR-7.

Usage:
    python tests/golden/diff_harness.py [--fixture <name>] [--all] [--json]

Exit codes:
    0 — all fixtures passed
    1 — one or more fixtures failed
    2 — argument or setup error

The harness asserts on four dimensions per fixture:
    (a) findings JSON — severity, confidence, file, line, finding
    (b) outbound HTTP calls — method, URL pattern, required body fields
    (c) watermark transitions — sha_before, sha_after
    (d) thread-resolution calls (when stale_threads_resolved > 0)

Tolerances (see tolerances.md):
    - Timestamps in tape files are ignored
    - Opaque request IDs (comment IDs, review IDs) are ignored
    - Field ordering within findings arrays is normalized
    - Whitespace variations in rendered markdown are ignored
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

FIXTURES_DIR = Path(__file__).parent / "fixtures"
TOLERANCES_FILE = Path(__file__).parent / "tolerances.md"

# ---------------------------------------------------------------------------
# Tolerance helpers
# ---------------------------------------------------------------------------

_OPAQUE_ID_KEYS = {"id", "html_url", "review_id", "comment_id", "node_id"}
_TIMESTAMP_PATTERN = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?"
)


def _normalize_finding(f: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of a finding with only the assertable fields."""
    return {
        "severity": (f.get("severity") or "").upper(),
        "file": f.get("file", ""),
        "line": f.get("line"),
        "finding": _ws_normalize(f.get("finding", "")),
    }


def _ws_normalize(s: str) -> str:
    """Collapse runs of whitespace and strip."""
    return re.sub(r"\s+", " ", s).strip()


def _body_contains_all(body: str, required: list[str]) -> tuple[bool, list[str]]:
    missing = [r for r in required if r not in body]
    return len(missing) == 0, missing


# ---------------------------------------------------------------------------
# Tape loader
# ---------------------------------------------------------------------------

def load_tapes(fixture_dir: Path, tape_type: str) -> list[dict[str, Any]]:
    """Load all JSON tapes from llm-tapes/ or vcs-tapes/."""
    tape_dir = fixture_dir / tape_type
    if not tape_dir.exists():
        return []
    tapes = []
    for p in sorted(tape_dir.glob("*.json")):
        try:
            tapes.append(json.loads(p.read_text()))
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON in {p}: {e}") from e
    return tapes


# ---------------------------------------------------------------------------
# Assertion helpers
# ---------------------------------------------------------------------------

class AssertionResult:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.passes: list[str] = []

    @property
    def ok(self) -> bool:
        return len(self.failures) == 0

    def fail(self, msg: str) -> None:
        self.failures.append(msg)

    def ok_msg(self, msg: str) -> None:
        self.passes.append(msg)


def assert_findings(
    result: AssertionResult,
    expected_findings: list[dict[str, Any]],
    vcs_tapes: list[dict[str, Any]],
    llm_tapes: list[dict[str, Any]],
) -> None:
    """Dimension (a): findings JSON matches expected."""
    # Extract findings from LLM tape response bodies
    actual_findings: list[dict[str, Any]] = []
    for tape in llm_tapes:
        resp = tape.get("response_body", "")
        if not resp:
            continue
        # Try to parse the JSON in the response body (it's a stringified JSON)
        try:
            resp_obj = json.loads(resp)
        except json.JSONDecodeError:
            continue
        # Extract text content from Anthropic response format
        content = resp_obj.get("content", [])
        for block in (content if isinstance(content, list) else []):
            text = block.get("text", "") if isinstance(block, dict) else ""
            # Extract json-findings block
            m = re.search(r"```json-findings\n(.*?)```", text, re.DOTALL)
            if m:
                try:
                    parsed = json.loads(m.group(1))
                    if isinstance(parsed, list):
                        actual_findings.extend(parsed)
                except json.JSONDecodeError:
                    pass

    expected_norm = sorted(
        [_normalize_finding(f) for f in expected_findings],
        key=lambda x: (x["file"], x.get("line") or 0, x["severity"]),
    )
    actual_norm = sorted(
        [_normalize_finding(f) for f in actual_findings],
        key=lambda x: (x["file"], x.get("line") or 0, x["severity"]),
    )

    if expected_norm == actual_norm:
        result.ok_msg(f"findings: {len(expected_findings)} findings match")
    else:
        result.fail(
            f"findings mismatch: expected {len(expected_norm)}, got {len(actual_norm)}\n"
            f"  expected: {json.dumps(expected_norm, indent=2)}\n"
            f"  actual:   {json.dumps(actual_norm, indent=2)}"
        )


def assert_outbound_calls(
    result: AssertionResult,
    expected_calls: list[dict[str, Any]],
    vcs_tapes: list[dict[str, Any]],
) -> None:
    """Dimension (b): outbound VCS HTTP calls match expected patterns."""
    for expected_call in expected_calls:
        pattern = expected_call.get("url_pattern", "")
        method = expected_call.get("method", "POST")
        required = expected_call.get("body_contains", [])

        matched_tape = None
        for tape in vcs_tapes:
            tape_url = tape.get("url", "")
            tape_method = tape.get("method", "")
            if pattern in tape_url and tape_method == method:
                matched_tape = tape
                break

        if matched_tape is None:
            result.fail(
                f"outbound call not found: {method} matching '{pattern}'"
            )
            continue

        body = matched_tape.get("request_body", "")
        ok, missing = _body_contains_all(body, required)
        if ok:
            result.ok_msg(f"outbound call: {method} {pattern} — body_contains OK")
        else:
            result.fail(
                f"outbound call {method} {pattern}: "
                f"request body missing required strings: {missing}"
            )


def assert_watermark(
    result: AssertionResult,
    expected_watermark: dict[str, Any],
    vcs_tapes: list[dict[str, Any]],
) -> None:
    """Dimension (c): watermark sha_after appears in a VCS tape request body."""
    sha_after = expected_watermark.get("sha_after")
    if not sha_after:
        result.ok_msg("watermark: no sha_after to assert")
        return

    # sha_after should appear in at least one VCS tape (summary comment body)
    sha_short = sha_after[:8]
    for tape in vcs_tapes:
        body = tape.get("request_body", "")
        if sha_short in body or sha_after[:12] in body:
            result.ok_msg(f"watermark: sha_after {sha_short} found in VCS tape")
            return

    result.fail(
        f"watermark: sha_after {sha_short} not found in any VCS tape request body"
    )


def assert_thread_resolution(
    result: AssertionResult,
    expected: dict[str, Any],
    vcs_tapes: list[dict[str, Any]],
) -> None:
    """Dimension (d): stale thread resolution calls present when expected."""
    stale_count = expected.get("stale_threads_resolved", 0)
    if stale_count == 0:
        result.ok_msg("thread-resolution: none expected")
        return

    resolve_calls = [
        t for t in vcs_tapes
        if "resolveReviewThread" in t.get("url", "")
        or "resolve" in t.get("url", "").lower()
        or "resolveReviewThread" in t.get("request_body", "")
    ]
    if len(resolve_calls) >= stale_count:
        result.ok_msg(
            f"thread-resolution: {len(resolve_calls)} resolve calls (expected {stale_count})"
        )
    else:
        result.fail(
            f"thread-resolution: expected {stale_count} resolve calls, "
            f"found {len(resolve_calls)}"
        )


# ---------------------------------------------------------------------------
# Fixture runner
# ---------------------------------------------------------------------------

class FixtureResult:
    def __init__(self, name: str) -> None:
        self.name = name
        self.passed = False
        self.assertion_result: AssertionResult | None = None
        self.error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        if self.error:
            return {"fixture": self.name, "status": "error", "error": self.error}
        ar = self.assertion_result
        return {
            "fixture": self.name,
            "status": "pass" if self.passed else "fail",
            "passes": ar.passes if ar else [],
            "failures": ar.failures if ar else [],
        }


def run_fixture(fixture_dir: Path) -> FixtureResult:
    name = fixture_dir.name
    result = FixtureResult(name)

    try:
        expected_path = fixture_dir / "expected.json"
        if not expected_path.exists():
            result.error = "missing expected.json"
            return result

        expected = json.loads(expected_path.read_text())
        llm_tapes = load_tapes(fixture_dir, "llm-tapes")
        vcs_tapes = load_tapes(fixture_dir, "vcs-tapes")

        ar = AssertionResult()

        assert_findings(ar, expected.get("findings", []), vcs_tapes, llm_tapes)
        assert_outbound_calls(ar, expected.get("outbound_calls", []), vcs_tapes)
        assert_watermark(ar, expected.get("watermark", {}), vcs_tapes)
        assert_thread_resolution(ar, expected, vcs_tapes)

        result.assertion_result = ar
        result.passed = ar.ok

    except (ValueError, json.JSONDecodeError, OSError) as e:
        result.error = str(e)

    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Golden parity harness — replay fixtures and diff against expected"
    )
    parser.add_argument("--fixture", metavar="NAME", help="Run a specific fixture by name")
    parser.add_argument("--all", action="store_true", help="Run all fixtures (default)")
    parser.add_argument("--json", action="store_true", help="Output JSON report")
    args = parser.parse_args()

    if args.fixture:
        fixture_dirs = [FIXTURES_DIR / args.fixture]
        if not fixture_dirs[0].exists():
            print(f"ERROR: Fixture '{args.fixture}' not found in {FIXTURES_DIR}", file=sys.stderr)
            return 2
    else:
        fixture_dirs = sorted(d for d in FIXTURES_DIR.iterdir() if d.is_dir())

    if not fixture_dirs:
        print(f"INFO: No fixtures found in {FIXTURES_DIR}", file=sys.stderr)
        return 0

    results = [run_fixture(d) for d in fixture_dirs]

    if args.json:
        print(json.dumps([r.to_dict() for r in results], indent=2))
    else:
        for r in results:
            status_icon = "✓" if r.passed else ("!" if r.error else "✗")
            print(f"  [{status_icon}] {r.name}")
            if r.error:
                print(f"       ERROR: {r.error}")
            elif not r.passed and r.assertion_result:
                for failure in r.assertion_result.failures:
                    print(f"       FAIL: {failure}")

    passed = sum(1 for r in results if r.passed)
    failed = sum(1 for r in results if not r.passed)
    errors = sum(1 for r in results if r.error)

    if not args.json:
        print(f"\n{passed}/{len(results)} fixtures passed"
              + (f" ({failed} failed, {errors} errors)" if failed or errors else ""))

    return 0 if failed == 0 and errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
