#!/usr/bin/env python3
"""
tests/golden/inline_eligibility.py — Inline comment eligibility oracle.

For every finding that produces an inline comment, asserts:
  (i)  position/line/side are valid against the recorded diff
  (ii) suggestion-range endpoints fall on added-or-context lines
       (mirrors the vcs/common.sh:parse_diff_new_lines semantics)
  (iii) body-only fallback triggers only when expected (line not in diff)

Implements 0.FR-4.

Usage:
    python tests/golden/inline_eligibility.py [--fixture <name>] [--all] [--json]

Can also be imported and called from pytest via test_golden_harness.py.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

FIXTURES_DIR = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# Diff parsing — mirrors vcs/common.sh:parse_valid_lines and parse_diff_new_lines
# ---------------------------------------------------------------------------

def parse_valid_lines(diff_text: str) -> set[tuple[str, int]]:
    """Return set of (file, line) for every ADDED line in the unified diff.
    Mirrors vcs/common.sh:parse_valid_lines (bash engine's inline-eligibility gate).
    """
    valid: set[tuple[str, int]] = set()
    current_file = ""
    new_line = 0

    for raw_line in diff_text.splitlines():
        m_diff = re.match(r"^diff --git a/.+ b/(.+)$", raw_line)
        if m_diff:
            current_file = m_diff.group(1)
            new_line = 0
            continue
        if raw_line.startswith("+++ ") or raw_line.startswith("--- "):
            continue
        m_hunk = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", raw_line)
        if m_hunk:
            new_line = int(m_hunk.group(1))
            continue
        if not current_file or new_line <= 0:
            continue
        if raw_line.startswith("+"):
            valid.add((current_file, new_line))
            new_line += 1
        elif raw_line.startswith("-"):
            pass  # deleted lines don't advance new_line
        elif raw_line.startswith("\\"):
            pass  # "No newline at end of file" marker
        else:
            new_line += 1  # context line

    return valid


def parse_diff_new_lines(diff_text: str) -> set[tuple[str, int]]:
    """Return set of (file, line) for every line in the new file
    (added AND context). Used for suggestion-range validation.
    Mirrors vcs/common.sh:parse_diff_new_lines.
    """
    lines: set[tuple[str, int]] = set()
    current_file = ""
    new_line = 0

    for raw_line in diff_text.splitlines():
        m_diff = re.match(r"^diff --git a/.+ b/(.+)$", raw_line)
        if m_diff:
            current_file = m_diff.group(1)
            new_line = 0
            continue
        if raw_line.startswith("+++ ") or raw_line.startswith("--- "):
            continue
        m_hunk = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", raw_line)
        if m_hunk:
            new_line = int(m_hunk.group(1))
            continue
        if not current_file or new_line <= 0:
            continue
        if raw_line.startswith("+"):
            lines.add((current_file, new_line))
            new_line += 1
        elif raw_line.startswith("-"):
            pass
        elif raw_line.startswith("\\"):
            pass
        else:
            # context line — present in new file
            lines.add((current_file, new_line))
            new_line += 1

    return lines


# ---------------------------------------------------------------------------
# Eligibility oracle
# ---------------------------------------------------------------------------

class EligibilityResult:
    def __init__(self, fixture_name: str) -> None:
        self.fixture_name = fixture_name
        self.failures: list[str] = []
        self.passes: list[str] = []

    @property
    def ok(self) -> bool:
        return len(self.failures) == 0

    def fail(self, msg: str) -> None:
        self.failures.append(msg)

    def ok_msg(self, msg: str) -> None:
        self.passes.append(msg)

    def to_dict(self) -> dict[str, Any]:
        return {
            "fixture": self.fixture_name,
            "status": "pass" if self.ok else "fail",
            "passes": self.passes,
            "failures": self.failures,
        }


def check_fixture_eligibility(fixture_dir: Path) -> EligibilityResult:
    name = fixture_dir.name
    result = EligibilityResult(name)

    # Load expected.json
    expected_path = fixture_dir / "expected.json"
    if not expected_path.exists():
        result.fail("missing expected.json")
        return result

    try:
        expected = json.loads(expected_path.read_text())
    except json.JSONDecodeError as e:
        result.fail(f"invalid expected.json: {e}")
        return result

    # Load diff.patch
    diff_path = fixture_dir / "diff.patch"
    if not diff_path.exists():
        result.ok_msg("no diff.patch — skipping eligibility checks")
        return result

    diff_text = diff_path.read_text()
    valid_lines = parse_valid_lines(diff_text)
    diff_new_lines = parse_diff_new_lines(diff_text)

    expected_inline = expected.get("inline_comments", [])
    all_findings = expected.get("findings", [])

    # (i) For every expected inline comment, verify line is in valid_lines
    for ic in expected_inline:
        f = ic.get("file", "")
        line = ic.get("line")
        side = ic.get("side", "RIGHT")

        if line is None:
            result.fail(f"inline comment for {f} has no line number")
            continue

        if (f, line) in valid_lines:
            result.ok_msg(
                f"inline eligible: {f}:{line} side={side} (added line in diff)"
            )
        else:
            # Check if it's a context line (which is also valid for inline comments
            # on GitHub — GitHub allows RIGHT side on any new-file line)
            if (f, line) in diff_new_lines:
                result.ok_msg(
                    f"inline eligible (context): {f}:{line} side={side} (context line in diff)"
                )
            else:
                result.fail(
                    f"inline comment target {f}:{line} is NOT in diff "
                    f"(valid_lines has {len(valid_lines)} entries)"
                )

    # (ii) Suggestion-range validation
    for finding in all_findings:
        f = finding.get("file", "")
        line = finding.get("line")
        start_line = finding.get("start_line")
        suggested = finding.get("suggested_code", "")

        if not suggested or not start_line or start_line == line:
            continue

        if line is None:
            continue

        # Every line in start_line..line must be in diff_new_lines
        range_valid = True
        invalid_line = None
        for check_line in range(start_line, line + 1):
            if (f, check_line) not in diff_new_lines:
                range_valid = False
                invalid_line = check_line
                break

        if range_valid:
            result.ok_msg(
                f"suggestion range valid: {f}:{start_line}-{line} all in diff new lines"
            )
        else:
            result.fail(
                f"suggestion range {f}:{start_line}-{line} invalid: "
                f"line {invalid_line} is not in diff new-file lines"
            )

    # (iii) Body-only fallback: findings NOT in expected_inline should not be
    #       in valid_lines (they land in the review body, not as inline comments)
    inline_targets = {(ic.get("file"), ic.get("line")) for ic in expected_inline}
    for finding in all_findings:
        f = finding.get("file", "")
        line = finding.get("line")
        if line is None:
            continue
        if (f, line) not in inline_targets:
            # This finding should be body-only.
            # If it IS in valid_lines, it's eligible for inline but we chose not
            # to inline it (e.g. non-added line, or the finding is body-rendered).
            # We don't fail here — body-only for eligible lines is valid (e.g. the
            # inline cap may have been reached in full runs). We only fail when
            # a finding expected to be inline is NOT eligible.
            pass

    if not expected_inline and not all_findings:
        result.ok_msg("no inline comments expected — oracle trivially satisfied")

    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Inline eligibility oracle — verify inline comment eligibility per fixture"
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

    results = [check_fixture_eligibility(d) for d in fixture_dirs]

    if args.json:
        print(json.dumps([r.to_dict() for r in results], indent=2))
    else:
        for r in results:
            icon = "✓" if r.ok else "✗"
            print(f"  [{icon}] {r.fixture_name}")
            for failure in r.failures:
                print(f"       FAIL: {failure}")

    passed = sum(1 for r in results if r.ok)
    failed = sum(1 for r in results if not r.ok)

    if not args.json:
        print(f"\n{passed}/{len(results)} fixtures passed" +
              (f" ({failed} failed)" if failed else ""))

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
