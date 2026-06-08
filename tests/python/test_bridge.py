"""Tests for ai_pr_review.analyzers.bridge."""

from __future__ import annotations

import json
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from ai_pr_review.analyzers.bridge import (
    _SARIF_EQUIVALENT_ANALYZERS,
    AnalyzerSpec,
    _file_list,
    _is_eligible,
    _normalise_output,
    _run_analyzer,
    _sarif_covered_names,
    run_analyzers,
)
from ai_pr_review.manifest import ChangedFiles

# ---------------------------------------------------------------------------
# _is_eligible
# ---------------------------------------------------------------------------


class TestIsEligible:
    def test_no_required_types_always_eligible(self) -> None:
        spec = AnalyzerSpec("trufflehog", "run-trufflehog.sh", [])
        cf = ChangedFiles()
        assert _is_eligible(spec, cf) is True

    def test_required_type_present(self) -> None:
        spec = AnalyzerSpec("shellcheck", "run-shellcheck.sh", ["shell"])
        cf = ChangedFiles(shell=["review.sh"])
        assert _is_eligible(spec, cf) is True

    def test_required_type_absent(self) -> None:
        spec = AnalyzerSpec("shellcheck", "run-shellcheck.sh", ["shell"])
        cf = ChangedFiles()
        assert _is_eligible(spec, cf) is False

    def test_multi_type_any_match(self) -> None:
        spec = AnalyzerSpec("checkov", "run-checkov.sh", ["terraform", "iac", "dockerfile"])
        cf = ChangedFiles(dockerfile=["Dockerfile"])
        assert _is_eligible(spec, cf) is True

    def test_multi_type_none_match(self) -> None:
        spec = AnalyzerSpec("checkov", "run-checkov.sh", ["terraform", "iac", "dockerfile"])
        cf = ChangedFiles(python=["main.py"])
        assert _is_eligible(spec, cf) is False


# ---------------------------------------------------------------------------
# _normalise_output
# ---------------------------------------------------------------------------


class TestNormaliseOutput:
    def test_valid_findings(self) -> None:
        data = [{"severity": "High", "confidence": 80, "finding": "SQL injection risk"}]
        result = _normalise_output(json.dumps(data), "test-analyzer")
        assert len(result) == 1
        assert result[0].severity == "High"
        assert result[0].source == "test-analyzer"

    def test_source_from_data_takes_precedence(self) -> None:
        data = [{"severity": "Low", "confidence": 60, "finding": "X", "source": "custom"}]
        result = _normalise_output(json.dumps(data), "fallback")
        assert result[0].source == "custom"

    def test_non_json_returns_empty(self, capsys: pytest.CaptureFixture[str]) -> None:
        result = _normalise_output("not json", "test-analyzer")
        assert result == []
        captured = capsys.readouterr()
        assert "non-JSON" in captured.err

    def test_non_list_returns_empty(self) -> None:
        result = _normalise_output(json.dumps({"key": "val"}), "test-analyzer")
        assert result == []

    def test_malformed_finding_skipped(self, capsys: pytest.CaptureFixture[str]) -> None:
        # Missing required 'finding' field
        data = [{"severity": "High", "confidence": 80}]
        result = _normalise_output(json.dumps(data), "test-analyzer")
        assert result == []
        captured = capsys.readouterr()
        assert "malformed" in captured.err

    def test_non_dict_items_skipped(self) -> None:
        data = ["not a dict", {"severity": "Low", "confidence": 50, "finding": "ok"}]
        result = _normalise_output(json.dumps(data), "test-analyzer")
        assert len(result) == 1

    def test_empty_array(self) -> None:
        result = _normalise_output("[]", "test-analyzer")
        assert result == []


# ---------------------------------------------------------------------------
# run_analyzers
# ---------------------------------------------------------------------------


class TestRunAnalyzers:
    def _make_script(self, tmpdir: str, name: str, output: str, exit_code: int = 0) -> Path:
        """Create a mock bash script in tmpdir/analyzers/ that prints output."""
        analyzers = Path(tmpdir) / "analyzers"
        analyzers.mkdir(exist_ok=True)
        script = analyzers / name
        script.write_text(f"#!/bin/bash\necho '{output}'\nexit {exit_code}\n")
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        return script

    @pytest.mark.anyio
    async def test_skips_analyzer_with_no_eligible_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            # shellcheck requires shell files
            self._make_script(tmpdir, "run-shellcheck.sh", "[]")
            cf = ChangedFiles()  # no shell files
            findings = await run_analyzers(cf, "/dev/null", tmpdir)
            assert findings == []

    @pytest.mark.anyio
    async def test_runs_eligible_analyzer(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            payload = json.dumps([{"severity": "Low", "confidence": 55, "finding": "SC2034"}])
            self._make_script(tmpdir, "run-shellcheck.sh", payload)
            cf = ChangedFiles(shell=["review.sh"])
            findings = await run_analyzers(cf, "/dev/null", tmpdir)
            assert len(findings) == 1

    @pytest.mark.anyio
    async def test_missing_script_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            # Script file does not exist
            cf = ChangedFiles(shell=["review.sh"])
            findings = await run_analyzers(cf, "/dev/null", tmpdir)
            assert findings == []

    @pytest.mark.anyio
    async def test_non_zero_exit_code_other_than_1_skipped(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            self._make_script(tmpdir, "run-shellcheck.sh", "[]", exit_code=2)
            cf = ChangedFiles(shell=["review.sh"])
            findings = await run_analyzers(cf, "/dev/null", tmpdir)
            assert findings == []
            captured = capsys.readouterr()
            assert "exited 2" in captured.err

    @pytest.mark.anyio
    async def test_exit_code_1_accepted(self) -> None:
        """Exit code 1 is valid (grep returns 1 for no-match paths)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            self._make_script(tmpdir, "run-shellcheck.sh", "[]", exit_code=1)
            cf = ChangedFiles(shell=["review.sh"])
            findings = await run_analyzers(cf, "/dev/null", tmpdir)
            assert findings == []  # empty but not an error

    @pytest.mark.anyio
    async def test_timeout_returns_empty(self, capsys: pytest.CaptureFixture[str]) -> None:
        spec = AnalyzerSpec("slow", "run-slow.sh", [])
        with (
            patch("subprocess.run", side_effect=subprocess.TimeoutExpired("bash", 120)),
            tempfile.TemporaryDirectory() as tmpdir,
        ):
            Path(tmpdir, "analyzers").mkdir()
            script = Path(tmpdir, "analyzers", "run-slow.sh")
            script.write_text("#!/bin/bash\nsleep 999\n")
            script.chmod(script.stat().st_mode | stat.S_IEXEC)
            cf = ChangedFiles()
            from ai_pr_review.analyzers import bridge
            with patch.object(bridge, "_ANALYZERS", [spec]):
                findings = await run_analyzers(cf, "/dev/null", tmpdir)
        assert findings == []
        captured = capsys.readouterr()
        assert "timed out" in captured.err

    @pytest.mark.anyio
    async def test_oserror_returns_empty(self, capsys: pytest.CaptureFixture[str]) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, "analyzers").mkdir()
            script = Path(tmpdir, "analyzers", "run-shellcheck.sh")
            script.write_text("#!/bin/bash\n")
            # Remove execute permission to trigger OSError on some systems
            script.chmod(0o644)
            cf = ChangedFiles(shell=["review.sh"])
            with patch("subprocess.run", side_effect=OSError("permission denied")):
                findings = await run_analyzers(cf, "/dev/null", tmpdir)
        assert findings == []
        captured = capsys.readouterr()
        assert "failed to start" in captured.err

    @pytest.mark.anyio
    async def test_empty_stdout_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            # Script produces no output
            self._make_script(tmpdir, "run-shellcheck.sh", "")
            cf = ChangedFiles(shell=["review.sh"])
            findings = await run_analyzers(cf, "/dev/null", tmpdir)
            assert findings == []

    @pytest.mark.anyio
    async def test_env_vars_passed_to_subprocess(self) -> None:
        """DIFF_FILE must be set in the subprocess environment."""
        with tempfile.TemporaryDirectory() as tmpdir:
            analyzers = Path(tmpdir) / "analyzers"
            analyzers.mkdir()
            script = analyzers / "run-shellcheck.sh"
            # Use printf to avoid shell quoting issues with echo and double quotes
            script.write_text(
                "#!/bin/bash\n"
                'printf \'[{"severity":"Low","confidence":50,"finding":"%s"}]\\n\' "$DIFF_FILE"\n'
            )
            script.chmod(script.stat().st_mode | stat.S_IEXEC)
            cf = ChangedFiles(shell=["review.sh"])
            findings = await run_analyzers(cf, "/tmp/test.diff", tmpdir)
            assert len(findings) == 1
            assert "/tmp/test.diff" in findings[0].finding


# ---------------------------------------------------------------------------
# #354: concurrency tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_concurrency_peak_does_not_exceed_cap() -> None:
    """Peak active analyzers must not exceed the concurrency cap.

    We replace _run_analyzer with a slow synchronous version and measure how
    many tasks are executing at once from within the async coroutine layer. The
    CapacityLimiter is acquired *before* `to_thread.run_sync` is called, so we
    measure the slot acquisition, not the subprocess itself.
    """
    import threading
    specs = [
        AnalyzerSpec("a1", "run-a1.sh", []),
        AnalyzerSpec("a2", "run-a2.sh", []),
        AnalyzerSpec("a3", "run-a3.sh", []),
        AnalyzerSpec("a4", "run-a4.sh", []),
    ]
    peak = 0
    active = 0
    lock = threading.Lock()

    def slow_analyzer(spec: AnalyzerSpec, *args: Any, **kwargs: Any) -> list:
        nonlocal peak, active
        with lock:
            active += 1
            peak = max(peak, active)
        import time
        time.sleep(0.01)
        with lock:
            active -= 1
        return []

    with tempfile.TemporaryDirectory() as tmpdir:
        analyzers = Path(tmpdir) / "analyzers"
        analyzers.mkdir()
        # Script files must exist for the eligible check to pass
        for spec in specs:
            s = analyzers / spec.script
            s.write_text("#!/bin/bash\necho '[]'\n")
            s.chmod(s.stat().st_mode | stat.S_IEXEC)

        from ai_pr_review.analyzers import bridge
        with (
            patch.object(bridge, "_ANALYZERS", specs),
            patch.object(bridge, "_run_analyzer", side_effect=slow_analyzer),
        ):
            findings = await run_analyzers(ChangedFiles(), "/dev/null", tmpdir, concurrency=2)

    assert peak <= 2
    assert findings == []


@pytest.mark.anyio
async def test_one_analyzer_failure_does_not_abort_others(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An unexpected exception in one analyzer task must not cancel the rest."""
    crash_spec = AnalyzerSpec("crasher", "run-crasher.sh", [])
    ok_spec = AnalyzerSpec("shellcheck", "run-shellcheck.sh", ["shell"])

    with tempfile.TemporaryDirectory() as tmpdir:
        analyzers = Path(tmpdir) / "analyzers"
        analyzers.mkdir()
        # shellcheck returns a valid finding
        payload = json.dumps([{"severity": "Low", "confidence": 55, "finding": "SC2034"}])
        ok_script = analyzers / "run-shellcheck.sh"
        ok_script.write_text(f"#!/bin/bash\necho '{payload}'\n")
        ok_script.chmod(ok_script.stat().st_mode | stat.S_IEXEC)
        # crasher exists but raises in _run_analyzer via subprocess
        crash_script = analyzers / "run-crasher.sh"
        crash_script.write_text("#!/bin/bash\necho '[]'\n")
        crash_script.chmod(crash_script.stat().st_mode | stat.S_IEXEC)

        from ai_pr_review.analyzers import bridge
        with patch.object(bridge, "_ANALYZERS", [crash_spec, ok_spec]):
            original_run_analyzer = bridge._run_analyzer

            def patched(spec: AnalyzerSpec, *args: Any, **kwargs: Any) -> Any:
                if spec.name == "crasher":
                    raise RuntimeError("simulated crash")
                return original_run_analyzer(spec, *args, **kwargs)

            with patch.object(bridge, "_run_analyzer", side_effect=patched):
                cf = ChangedFiles(shell=["review.sh"])
                findings = await run_analyzers(cf, "/dev/null", tmpdir, concurrency=4)

    # shellcheck finding survives despite crasher failing
    assert len(findings) == 1
    assert findings[0].finding == "SC2034"
    captured = capsys.readouterr()
    assert "WARNING" in captured.err
    assert "simulated crash" in captured.err


@pytest.mark.anyio
async def test_findings_returned_in_spec_order() -> None:
    """Findings must come back in the same order as the _ANALYZERS list."""
    specs = [
        AnalyzerSpec("first", "run-first.sh", []),
        AnalyzerSpec("second", "run-second.sh", []),
    ]
    payload_first = json.dumps([{"severity": "Low", "confidence": 55, "finding": "first-finding"}])
    payload_second = json.dumps([{"severity": "Low", "confidence": 55, "finding": "second-finding"}])

    with tempfile.TemporaryDirectory() as tmpdir:
        analyzers = Path(tmpdir) / "analyzers"
        analyzers.mkdir()
        for spec, payload in [(specs[0], payload_first), (specs[1], payload_second)]:
            s = analyzers / spec.script
            s.write_text(f"#!/bin/bash\necho '{payload}'\n")
            s.chmod(s.stat().st_mode | stat.S_IEXEC)

        from ai_pr_review.analyzers import bridge
        with patch.object(bridge, "_ANALYZERS", specs):
            findings = await run_analyzers(ChangedFiles(), "/dev/null", tmpdir)

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
    ruff_spec = AnalyzerSpec("ruff", "run-ruff.sh", ["python"])
    shellcheck_spec = AnalyzerSpec("shellcheck", "run-shellcheck.sh", ["shell"])
    payload = json.dumps([{"severity": "Low", "confidence": 55, "finding": "SC2034"}])

    with tempfile.TemporaryDirectory() as tmpdir:
        analyzers = Path(tmpdir) / "analyzers"
        analyzers.mkdir()
        # Both scripts exist and would produce findings if run.
        for spec, data in [(ruff_spec, "[]"), (shellcheck_spec, payload)]:
            s = analyzers / spec.script
            s.write_text(f"#!/bin/bash\necho '{data}'\n")
            s.chmod(s.stat().st_mode | stat.S_IEXEC)

        from ai_pr_review.analyzers import bridge
        with patch.object(bridge, "_ANALYZERS", [ruff_spec, shellcheck_spec]):
            cf = ChangedFiles(python=["app.py"], shell=["review.sh"])
            findings = await run_analyzers(
                cf, "/dev/null", tmpdir,
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
    ruff_spec = AnalyzerSpec("ruff", "run-ruff.sh", ["python"])
    payload = json.dumps([{"severity": "Low", "confidence": 55, "finding": "E501"}])

    with tempfile.TemporaryDirectory() as tmpdir:
        analyzers = Path(tmpdir) / "analyzers"
        analyzers.mkdir()
        s = analyzers / "run-ruff.sh"
        s.write_text(f"#!/bin/bash\necho '{payload}'\n")
        s.chmod(s.stat().st_mode | stat.S_IEXEC)

        from ai_pr_review.analyzers import bridge
        with patch.object(bridge, "_ANALYZERS", [ruff_spec]):
            findings = await run_analyzers(
                ChangedFiles(python=["app.py"]), "/dev/null", tmpdir,
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
    async def test_timeout_warning_format(self, capsys: pytest.CaptureFixture[str]) -> None:
        spec = AnalyzerSpec("slow-tool", "run-slow.sh", [])
        with (
            patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="bash", timeout=120)),
            tempfile.TemporaryDirectory() as tmpdir,
        ):
            Path(tmpdir, "analyzers").mkdir()
            script = Path(tmpdir, "analyzers", "run-slow.sh")
            script.write_text("#!/bin/bash\nsleep 9999\n")
            script.chmod(script.stat().st_mode | stat.S_IEXEC)
            from ai_pr_review.analyzers import bridge
            with patch.object(bridge, "_ANALYZERS", [spec]):
                findings = await run_analyzers(ChangedFiles(), "/dev/null", tmpdir)
        captured = capsys.readouterr()
        assert findings == []
        assert "[ai-pr-review] WARNING:" in captured.err

    @pytest.mark.anyio
    async def test_oserror_warning_format(self, capsys: pytest.CaptureFixture[str]) -> None:
        with (
            patch("subprocess.run", side_effect=OSError("permission denied")),
            tempfile.TemporaryDirectory() as tmpdir,
        ):
            Path(tmpdir, "analyzers").mkdir()
            script = Path(tmpdir, "analyzers", "run-shellcheck.sh")
            script.write_text("#!/bin/bash\n")
            script.chmod(0o644)
            findings = await run_analyzers(ChangedFiles(shell=["review.sh"]), "/dev/null", tmpdir)
        captured = capsys.readouterr()
        assert findings == []
        assert "[ai-pr-review] WARNING:" in captured.err

    def test_non_json_warning_format(self, capsys: pytest.CaptureFixture[str]) -> None:
        result = _normalise_output("not json at all", "my-analyzer")
        captured = capsys.readouterr()
        assert result == []
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
# stdin passthrough via subprocess.run input= kwarg
# ---------------------------------------------------------------------------


class TestStdinPassthrough:
    def test_subprocess_called_with_file_list_as_input(self) -> None:
        """_run_analyzer must pass the file list to subprocess.run via input=."""
        spec = AnalyzerSpec("shellcheck", "run-shellcheck.sh", ["shell"])
        cf = ChangedFiles(all_files=["foo.sh", "bar.sh"])
        expected_input = _file_list(cf)

        captured_kwargs: dict = {}

        def mock_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess:
            captured_kwargs.update(kwargs)
            return subprocess.CompletedProcess(args=[], returncode=0, stdout="[]", stderr="")

        with patch("subprocess.run", side_effect=mock_run):
            _run_analyzer(spec, "/fake/run-shellcheck.sh", "/dev/null", {}, expected_input)

        assert "input" in captured_kwargs
        assert captured_kwargs["input"] == "bar.sh\nfoo.sh"

    @pytest.mark.anyio
    async def test_run_analyzers_passes_all_files_via_stdin(self) -> None:
        """Integration check: file_list computed from all_files reaches subprocess."""
        with tempfile.TemporaryDirectory() as tmpdir:
            analyzers = Path(tmpdir) / "analyzers"
            analyzers.mkdir()
            script = analyzers / "run-shellcheck.sh"
            # Echo stdin back as a JSON finding so we can assert it arrived.
            script.write_text(
                "#!/bin/bash\n"
                "STDIN=$(cat)\n"
                'printf \'[{"severity":"Low","confidence":50,"finding":"%s"}]\\n\' "$STDIN"\n'
            )
            script.chmod(script.stat().st_mode | stat.S_IEXEC)
            cf = ChangedFiles(shell=["review.sh"], all_files=["review.sh"])
            findings = await run_analyzers(cf, "/dev/null", tmpdir)
        assert len(findings) == 1
        assert "review.sh" in findings[0].finding
