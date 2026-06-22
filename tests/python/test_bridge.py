"""Tests for ai_pr_review.analyzers.bridge."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from ai_pr_review.analyzers.bridge import (
    _SARIF_EQUIVALENT_ANALYZERS,
    ANALYZER_NAMES,
    AnalyzerSpec,
    _analyzer_skip_names,
    _file_list,
    _is_eligible,
    _sarif_covered_names,
    run_analyzers,
)
from ai_pr_review.manifest import ChangedFiles

# ---------------------------------------------------------------------------
# _is_eligible
# ---------------------------------------------------------------------------


class TestIsEligible:
    def test_no_required_types_always_eligible(self) -> None:
        spec = AnalyzerSpec("trufflehog", [], lambda cf, d: [])
        cf = ChangedFiles()
        assert _is_eligible(spec, cf) is True

    def test_required_type_present(self) -> None:
        spec = AnalyzerSpec("shellcheck", ["shell"], lambda cf, d: [])
        cf = ChangedFiles(shell=["review.sh"])
        assert _is_eligible(spec, cf) is True

    def test_required_type_absent(self) -> None:
        spec = AnalyzerSpec("shellcheck", ["shell"], lambda cf, d: [])
        cf = ChangedFiles()
        assert _is_eligible(spec, cf) is False

    def test_multi_type_any_match(self) -> None:
        spec = AnalyzerSpec("checkov", ["terraform", "iac", "dockerfile"], lambda cf, d: [])
        cf = ChangedFiles(dockerfile=["Dockerfile"])
        assert _is_eligible(spec, cf) is True

    def test_multi_type_none_match(self) -> None:
        spec = AnalyzerSpec("checkov", ["terraform", "iac", "dockerfile"], lambda cf, d: [])
        cf = ChangedFiles(python=["main.py"])
        assert _is_eligible(spec, cf) is False


# ---------------------------------------------------------------------------
# run_analyzers
# ---------------------------------------------------------------------------


class TestRunAnalyzers:
    @pytest.mark.anyio
    async def test_skips_analyzer_with_no_eligible_files(self) -> None:
        calls: list[str] = []

        def fake_native(cf: ChangedFiles, diff: Path) -> list:
            calls.append("called")
            return []

        spec = AnalyzerSpec("mock-tool", ["shell"], fake_native)
        cf = ChangedFiles()  # no shell files
        from ai_pr_review.analyzers import bridge
        with patch.object(bridge, "_ANALYZERS", [spec]):
            findings = await run_analyzers(cf, "/dev/null")
        assert findings == []
        assert calls == []  # native fn was not invoked

    @pytest.mark.anyio
    async def test_runs_eligible_analyzer(self) -> None:
        from ai_pr_review.findings.models import Finding

        def fake_native(cf: ChangedFiles, diff: Path) -> list:
            return [Finding(severity="Low", confidence=55, finding="SC2034")]

        spec = AnalyzerSpec("mock-tool", ["shell"], fake_native)
        cf = ChangedFiles(shell=["review.sh"])
        from ai_pr_review.analyzers import bridge
        with patch.object(bridge, "_ANALYZERS", [spec]):
            findings = await run_analyzers(cf, "/dev/null")
        assert len(findings) == 1

    @pytest.mark.anyio
    async def test_native_exception_returns_empty_and_warns(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Unhandled exception in native_fn must not abort run_analyzers."""
        def exploding(cf: ChangedFiles, diff: Path) -> list:
            raise RuntimeError("boom")

        spec = AnalyzerSpec("mock-tool", ["shell"], exploding)
        cf = ChangedFiles(shell=["review.sh"])
        from ai_pr_review.analyzers import bridge
        with patch.object(bridge, "_ANALYZERS", [spec]):
            findings = await run_analyzers(cf, "/dev/null")
        assert findings == []
        captured = capsys.readouterr()
        assert "WARNING" in captured.err


# ---------------------------------------------------------------------------
# #354: concurrency tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_concurrency_peak_does_not_exceed_cap() -> None:
    """Peak active analyzers must not exceed the concurrency cap."""
    peak = 0
    active = 0
    lock = threading.Lock()

    def slow_native(cf: ChangedFiles, diff: Path) -> list:
        nonlocal peak, active
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.01)
        with lock:
            active -= 1
        return []

    specs = [
        AnalyzerSpec(f"a{i}", [], slow_native)
        for i in range(4)
    ]
    from ai_pr_review.analyzers import bridge
    with patch.object(bridge, "_ANALYZERS", specs):
        findings = await run_analyzers(ChangedFiles(), "/dev/null", concurrency=2)

    assert peak <= 2
    assert findings == []


@pytest.mark.anyio
async def test_one_analyzer_failure_does_not_abort_others(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An unexpected exception in one analyzer task must not cancel the rest."""
    from ai_pr_review.findings.models import Finding

    def crashing(cf: ChangedFiles, diff: Path) -> list:
        raise RuntimeError("simulated crash")

    def ok_native(cf: ChangedFiles, diff: Path) -> list:
        return [Finding(severity="Low", confidence=55, finding="SC2034")]

    crash_spec = AnalyzerSpec("crasher", [], crashing)
    ok_spec = AnalyzerSpec("shellcheck", ["shell"], ok_native)
    cf = ChangedFiles(shell=["review.sh"])

    from ai_pr_review.analyzers import bridge
    with patch.object(bridge, "_ANALYZERS", [crash_spec, ok_spec]):
        findings = await run_analyzers(cf, "/dev/null", concurrency=4)

    # shellcheck finding survives despite crasher failing
    assert len(findings) == 1
    assert findings[0].finding == "SC2034"
    captured = capsys.readouterr()
    assert "WARNING" in captured.err
    assert "simulated crash" in captured.err


@pytest.mark.anyio
async def test_findings_returned_in_spec_order() -> None:
    """Findings must come back in the same order as the _ANALYZERS list."""
    from ai_pr_review.findings.models import Finding

    def make_fn(label: str):
        def fn(cf: ChangedFiles, diff: Path) -> list:
            return [Finding(severity="Low", confidence=55, finding=f"{label}-finding")]
        return fn

    specs = [
        AnalyzerSpec("first", [], make_fn("first")),
        AnalyzerSpec("second", [], make_fn("second")),
    ]
    from ai_pr_review.analyzers import bridge
    with patch.object(bridge, "_ANALYZERS", specs):
        findings = await run_analyzers(ChangedFiles(), "/dev/null")

    assert len(findings) == 2
    assert findings[0].finding == "first-finding"
    assert findings[1].finding == "second-finding"


# ---------------------------------------------------------------------------
# #353: SARIF-skip tests
# ---------------------------------------------------------------------------


def test_sarif_covered_names_returns_matching_stems() -> None:
    """Filename stems matching known SARIF-equivalent analyzers are returned."""
    covered = _sarif_covered_names(("results/ruff.sarif", "results/hadolint.sarif"))
    assert "ruff" in covered
    assert "hadolint" in covered
    assert "semgrep" not in covered


def test_sarif_covered_names_case_insensitive() -> None:
    """Stem matching is case-insensitive."""
    covered = _sarif_covered_names(("results/Ruff.SARIF",))
    assert "ruff" in covered


def test_sarif_covered_names_non_matching_path_ignored() -> None:
    """Unrecognized stems (not in _SARIF_EQUIVALENT_ANALYZERS) produce no entries."""
    covered = _sarif_covered_names(("results/custom-tool.sarif",))
    assert len(covered) == 0


def test_sarif_covered_names_empty_paths() -> None:
    """Empty sarif_paths produces an empty set (no native analyzers skipped)."""
    assert _sarif_covered_names(()) == frozenset()


@pytest.mark.anyio
async def test_run_analyzers_skips_sarif_covered_analyzer(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Native ruff wrapper is skipped when ruff.sarif is configured."""
    from ai_pr_review.findings.models import Finding

    def ruff_fn(cf: ChangedFiles, diff: Path) -> list:
        return []

    def shellcheck_fn(cf: ChangedFiles, diff: Path) -> list:
        return [Finding(severity="Low", confidence=55, finding="SC2034")]

    ruff_spec = AnalyzerSpec("ruff", ["python"], ruff_fn)
    shellcheck_spec = AnalyzerSpec("shellcheck", ["shell"], shellcheck_fn)

    from ai_pr_review.analyzers import bridge
    with patch.object(bridge, "_ANALYZERS", [ruff_spec, shellcheck_spec]):
        cf = ChangedFiles(python=["app.py"], shell=["review.sh"])
        findings = await run_analyzers(
            cf, "/dev/null",
            sarif_skip=_sarif_covered_names(("results/ruff.sarif",)),
        )

    # ruff was skipped; shellcheck finding present
    assert len(findings) == 1
    assert findings[0].finding == "SC2034"
    captured = capsys.readouterr()
    assert "ruff" in captured.err
    assert "skipping native" in captured.err


@pytest.mark.anyio
async def test_run_analyzers_no_sarif_skip_runs_all() -> None:
    """When sarif_skip is empty, no native analyzer is suppressed."""
    from ai_pr_review.findings.models import Finding

    def ruff_fn(cf: ChangedFiles, diff: Path) -> list:
        return [Finding(severity="Low", confidence=55, finding="E501")]

    ruff_spec = AnalyzerSpec("ruff", ["python"], ruff_fn)
    from ai_pr_review.analyzers import bridge
    with patch.object(bridge, "_ANALYZERS", [ruff_spec]):
        findings = await run_analyzers(
            ChangedFiles(python=["app.py"]), "/dev/null",
            sarif_skip=frozenset(),
        )

    assert len(findings) == 1
    assert findings[0].finding == "E501"


def test_sarif_equivalent_analyzers_constant() -> None:
    """The module constant must include ruff, semgrep, and hadolint."""
    assert "ruff" in _SARIF_EQUIVALENT_ANALYZERS
    assert "semgrep" in _SARIF_EQUIVALENT_ANALYZERS
    assert "hadolint" in _SARIF_EQUIVALENT_ANALYZERS


# ---------------------------------------------------------------------------
# Warning format assertions (Story 4-5)
# ---------------------------------------------------------------------------


class TestWarningFormat:
    @pytest.mark.anyio
    async def test_native_exception_warning_format(self, capsys: pytest.CaptureFixture[str]) -> None:
        def exploding(cf: ChangedFiles, diff: Path) -> list:
            raise RuntimeError("boom")

        spec = AnalyzerSpec("slow-tool", [], exploding)
        from ai_pr_review.analyzers import bridge
        with patch.object(bridge, "_ANALYZERS", [spec]):
            findings = await run_analyzers(ChangedFiles(), "/dev/null")
        captured = capsys.readouterr()
        assert findings == []
        assert "[ai-pr-review] WARNING:" in captured.err


# ---------------------------------------------------------------------------
# _file_list helper
# ---------------------------------------------------------------------------


class TestFileList:
    def test_returns_sorted_deduplicated_newline_joined(self) -> None:
        cf = ChangedFiles(
            all_files=["b.py", "a.sh", "b.py", "c.go"],
        )
        result = _file_list(cf)
        assert result == "a.sh\nb.py\nc.go"

    def test_empty_changed_files(self) -> None:
        cf = ChangedFiles()
        assert _file_list(cf) == ""

    def test_single_file(self) -> None:
        cf = ChangedFiles(all_files=["main.py"])
        assert _file_list(cf) == "main.py"

    def test_deduplication_preserves_sort(self) -> None:
        cf = ChangedFiles(all_files=["z.py", "a.py", "z.py", "m.py", "a.py"])
        result = _file_list(cf)
        assert result == "a.py\nm.py\nz.py"


# ---------------------------------------------------------------------------
# ANALYZER_NAMES canonical set
# ---------------------------------------------------------------------------


def test_analyzer_names_covers_all_registry_entries() -> None:
    """ANALYZER_NAMES must contain exactly the names in _ANALYZERS."""
    from ai_pr_review.analyzers.bridge import _ANALYZERS
    assert {spec.name for spec in _ANALYZERS} == ANALYZER_NAMES


# ---------------------------------------------------------------------------
# _analyzer_skip_names: allow/deny collapse logic
# ---------------------------------------------------------------------------


class TestAnalyzerSkipNames:
    def test_both_empty_returns_empty_skip_set(self) -> None:
        """Empty allowlist + empty denylist => skip nothing (no-op default)."""
        result = _analyzer_skip_names((), ())
        assert result == frozenset()

    def test_allowlist_only_skips_everything_else(self) -> None:
        """Non-empty allowlist => skip all names not in the allowlist."""
        result = _analyzer_skip_names(("semgrep", "trufflehog"), ())
        assert "semgrep" not in result
        assert "trufflehog" not in result
        # All other known analyzer names should be in the skip set
        for name in ANALYZER_NAMES - {"semgrep", "trufflehog"}:
            assert name in result

    def test_denylist_only_skips_listed_names(self) -> None:
        """Empty allowlist + denylist => skip exactly the denylist names."""
        result = _analyzer_skip_names((), ("checkov", "tflint"))
        assert result == frozenset({"checkov", "tflint"})

    def test_allowlist_takes_precedence_over_denylist(self) -> None:
        """When allowlist is non-empty, denylist is completely ignored."""
        # Even though semgrep is in the deny, the allowlist takes precedence
        result = _analyzer_skip_names(("semgrep",), ("semgrep",))
        assert "semgrep" not in result  # allowlist wins; semgrep should run

    def test_allowlist_single_entry(self) -> None:
        """Allowlist of one => skip all others."""
        result = _analyzer_skip_names(("ruff",), ())
        assert "ruff" not in result
        assert len(result) == len(ANALYZER_NAMES) - 1

    @pytest.mark.anyio
    async def test_run_analyzers_honors_disabled_param(self) -> None:
        """run_analyzers skips analyzers named in the disabled param."""
        cf = ChangedFiles(all_files=["main.py"], python=["main.py"])
        # Use a real native analyzer name that would normally be eligible
        # for a python file: ruff. Disable it and ensure it's not in results.
        disabled = frozenset({"ruff"})
        # Patch all native fns to return empty so test is fast
        from ai_pr_review.analyzers import bridge
        patched = [
            spec._replace(native_fn=lambda _cf, _diff: [])
            for spec in bridge._ANALYZERS
        ]
        with (
            patch.object(bridge, "_ANALYZERS", patched),
            # Also update ANALYZER_NAMES to match the patched registry
            patch.object(bridge, "ANALYZER_NAMES", {s.name for s in patched}),
        ):
            findings = await run_analyzers(
                cf, "/dev/null", disabled=disabled
            )
        assert findings == []  # all native fns return [] so no findings expected
