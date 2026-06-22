"""Tests for the native trufflehog analyzer."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ai_pr_review.analyzers.native.trufflehog import _load_allowlist, _run_trufflehog
from ai_pr_review.manifest import ChangedFiles

_FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "trufflehog"


def _load_fixture(name: str) -> str:
    return (_FIXTURE_DIR / name).read_text()


def _make_cf(files: list[str]) -> ChangedFiles:
    return ChangedFiles(all_files=files)


class TestLoadAllowlist:
    def test_no_config_file_returns_empty(self, tmp_path: Path) -> None:
        result = _load_allowlist(tmp_path)
        assert result == set()

    def test_config_with_paths_returns_set(self, tmp_path: Path) -> None:
        (tmp_path / ".trufflehog.yml").write_text(
            "allowlist:\n  paths:\n    - ai_pr_review/config.py\n    - scripts/deploy.sh\n"
        )
        result = _load_allowlist(tmp_path)
        assert "ai_pr_review/config.py" in result
        assert "scripts/deploy.sh" in result

    def test_empty_paths_returns_empty(self, tmp_path: Path) -> None:
        (tmp_path / ".trufflehog.yml").write_text("allowlist:\n  paths: []\n")
        result = _load_allowlist(tmp_path)
        assert result == set()

    def test_malformed_yaml_returns_empty(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        (tmp_path / ".trufflehog.yml").write_text(":\ninvalid: [yaml")
        with caplog.at_level("WARNING"):
            result = _load_allowlist(tmp_path)
        assert result == set()

    def test_non_dict_yaml_returns_empty(self, tmp_path: Path) -> None:
        (tmp_path / ".trufflehog.yml").write_text("- a\n- b\n")
        result = _load_allowlist(tmp_path)
        assert result == set()


class TestRunTrufflehogGuards:
    def test_no_files_returns_empty(self) -> None:
        cf = ChangedFiles()
        result = _run_trufflehog(cf, Path("/dev/null"))
        assert result == []

    def test_binary_absent_returns_empty(self, tmp_path: Path) -> None:
        f = tmp_path / "settings.py"
        f.write_text("secret = 'abc'\n")
        cf = _make_cf([str(f)])
        with patch("ai_pr_review.analyzers.native.trufflehog.shutil.which", return_value=None):
            result = _run_trufflehog(cf, Path("/dev/null"))
        assert result == []

    def test_binary_absent_logs_warning(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        f = tmp_path / "settings.py"
        f.write_text("secret = 'abc'\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.trufflehog.shutil.which", return_value=None),
            caplog.at_level("WARNING"),
        ):
            _run_trufflehog(cf, Path("/dev/null"))
        assert "trufflehog not found" in caplog.text

    def test_nonexistent_files_skipped(self, tmp_path: Path) -> None:
        cf = _make_cf([str(tmp_path / "nonexistent.py")])
        with patch("ai_pr_review.analyzers.native.trufflehog.shutil.which", return_value="/usr/bin/trufflehog"):
            result = _run_trufflehog(cf, Path("/dev/null"))
        assert result == []


class TestRunTrufflehogFindings:
    def _run_with_fixture(self, fixture_name: str, tmp_path: Path, allowlist_yaml: str | None = None) -> list:
        fixture = _load_fixture(fixture_name)
        f = tmp_path / "settings.py"
        f.write_text("secret = 'abc'\n")
        cf = _make_cf([str(f)])

        # Write allowlist if requested
        if allowlist_yaml:
            (tmp_path / ".trufflehog.yml").write_text(allowlist_yaml)

        with (
            patch("ai_pr_review.analyzers.native.trufflehog.shutil.which", return_value="/usr/bin/trufflehog"),
            patch("ai_pr_review.analyzers.native.trufflehog.subprocess.run") as mock_run,
            patch("ai_pr_review.analyzers.native.trufflehog.Path") as mock_path_cls,
        ):
            # Make Path(".").is_file() return False for .trufflehog.yml check
            # but allow Path(f).is_file() to return True for target files
            real_path = Path
            def path_side_effect(arg: object) -> Path:
                return real_path(arg)
            mock_path_cls.side_effect = path_side_effect
            mock_run.return_value = MagicMock(returncode=0, stdout=fixture, stderr="")
            # Use the actual _load_allowlist with tmp_path
            with patch("ai_pr_review.analyzers.native.trufflehog._load_allowlist", return_value=set()):
                return _run_trufflehog(cf, Path("/dev/null"))

    def test_double_dash_precedes_target_files(self, tmp_path: Path) -> None:
        # Argument-injection guard: target files must follow a literal "--".
        f = tmp_path / "settings.py"
        f.write_text("x = 1\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.trufflehog.shutil.which", return_value="/usr/bin/trufflehog"),
            patch("ai_pr_review.analyzers.native.trufflehog.subprocess.run") as mock_run,
            patch("ai_pr_review.analyzers.native.trufflehog._load_allowlist", return_value=set()),
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            _run_trufflehog(cf, Path("/dev/null"))
        call_args = mock_run.call_args[0][0]
        assert "--" in call_args
        assert call_args.index("--") < call_args.index(str(f))

    def _run_with_fixture_simple(self, fixture_name: str, tmp_path: Path) -> list:
        """Simpler helper that patches _load_allowlist directly."""
        fixture = _load_fixture(fixture_name)
        f = tmp_path / "settings.py"
        f.write_text("secret = 'abc'\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.trufflehog.shutil.which", return_value="/usr/bin/trufflehog"),
            patch("ai_pr_review.analyzers.native.trufflehog.subprocess.run") as mock_run,
            patch("ai_pr_review.analyzers.native.trufflehog._load_allowlist", return_value=set()),
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout=fixture, stderr="")
            return _run_trufflehog(cf, Path("/dev/null"))

    def test_empty_output_returns_empty(self, tmp_path: Path) -> None:
        findings = self._run_with_fixture_simple("trufflehog-empty.json", tmp_path)
        assert findings == []

    def test_verified_secret_maps_to_critical_95(self, tmp_path: Path) -> None:
        findings = self._run_with_fixture_simple("trufflehog-verified.json", tmp_path)
        assert len(findings) == 1
        assert findings[0].severity == "Critical"
        assert findings[0].confidence == 95
        assert findings[0].source == "trufflehog"

    def test_verified_finding_mentions_detector_name(self, tmp_path: Path) -> None:
        findings = self._run_with_fixture_simple("trufflehog-verified.json", tmp_path)
        assert "AWS" in findings[0].finding

    def test_verified_remediation_mentions_rotate(self, tmp_path: Path) -> None:
        findings = self._run_with_fixture_simple("trufflehog-verified.json", tmp_path)
        assert "rotate" in findings[0].remediation.lower() or "Rotate" in findings[0].remediation

    def test_unverified_secret_maps_to_high_85(self, tmp_path: Path) -> None:
        findings = self._run_with_fixture_simple("trufflehog-unverified.json", tmp_path)
        assert len(findings) == 1
        assert findings[0].severity == "High"
        assert findings[0].confidence == 85

    def test_unverified_non_test_finding_not_tagged_as_mock(self, tmp_path: Path) -> None:
        findings = self._run_with_fixture_simple("trufflehog-unverified.json", tmp_path)
        assert "test file" not in findings[0].finding

    def test_unverified_test_file_maps_to_low_40(self, tmp_path: Path) -> None:
        findings = self._run_with_fixture_simple("trufflehog-unverified-test-file.json", tmp_path)
        assert len(findings) == 1
        assert findings[0].severity == "Low"
        assert findings[0].confidence == 40

    def test_unverified_test_file_tagged_with_mock_notice(self, tmp_path: Path) -> None:
        findings = self._run_with_fixture_simple("trufflehog-unverified-test-file.json", tmp_path)
        assert "test file" in findings[0].finding

    def test_unverified_test_file_remediation_suggests_verify(self, tmp_path: Path) -> None:
        findings = self._run_with_fixture_simple("trufflehog-unverified-test-file.json", tmp_path)
        assert "Verify" in findings[0].remediation or "verify" in findings[0].remediation

    def test_verified_test_file_stays_critical_never_demoted(self, tmp_path: Path) -> None:
        findings = self._run_with_fixture_simple("trufflehog-verified-test-file.json", tmp_path)
        assert len(findings) == 1
        assert findings[0].severity == "Critical"
        assert findings[0].confidence == 95

    def test_allowlisted_path_suppressed(self, tmp_path: Path) -> None:
        fixture = _load_fixture("trufflehog-allowlisted-path.json")
        f = tmp_path / "config.py"
        f.write_text("secret = 'abc'\n")
        cf = _make_cf([str(f)])
        allowlist = {"ai_pr_review/config.py"}
        with (
            patch("ai_pr_review.analyzers.native.trufflehog.shutil.which", return_value="/usr/bin/trufflehog"),
            patch("ai_pr_review.analyzers.native.trufflehog.subprocess.run") as mock_run,
            patch("ai_pr_review.analyzers.native.trufflehog._load_allowlist", return_value=allowlist),
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout=fixture, stderr="")
            findings = _run_trufflehog(cf, Path("/dev/null"))
        assert findings == []

    def test_allowlisted_path_cannot_suppress_verified_secret(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # A PR-supplied .trufflehog.yml allowlist must not be able to hide a
        # VERIFIED (live) secret, even when its path is allowlisted.
        f = tmp_path / "config.py"
        f.write_text("secret = 'abc'\n")
        cf = _make_cf([str(f)])
        allowlist = {"ai_pr_review/config.py"}
        ndjson = (
            '{"DetectorName":"AWS","Verified":true,"Raw":"x",'
            '"SourceMetadata":{"Data":{"Filesystem":'
            '{"file":"ai_pr_review/config.py","line":1}}}}\n'
        )
        with (
            patch("ai_pr_review.analyzers.native.trufflehog.shutil.which", return_value="/usr/bin/trufflehog"),
            patch("ai_pr_review.analyzers.native.trufflehog.subprocess.run") as mock_run,
            patch("ai_pr_review.analyzers.native.trufflehog._load_allowlist", return_value=allowlist),
            caplog.at_level("WARNING"),
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout=ndjson, stderr="")
            findings = _run_trufflehog(cf, Path("/dev/null"))
        assert len(findings) == 1
        assert findings[0].severity == "Critical"
        assert "VERIFIED" in caplog.text

    def test_non_allowlisted_path_not_suppressed(self, tmp_path: Path) -> None:
        fixture = _load_fixture("trufflehog-unverified.json")
        f = tmp_path / "deploy.sh"
        f.write_text("secret = 'abc'\n")
        cf = _make_cf([str(f)])
        allowlist = {"ai_pr_review/config.py"}  # different path
        with (
            patch("ai_pr_review.analyzers.native.trufflehog.shutil.which", return_value="/usr/bin/trufflehog"),
            patch("ai_pr_review.analyzers.native.trufflehog.subprocess.run") as mock_run,
            patch("ai_pr_review.analyzers.native.trufflehog._load_allowlist", return_value=allowlist),
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout=fixture, stderr="")
            findings = _run_trufflehog(cf, Path("/dev/null"))
        assert len(findings) == 1

    def test_malformed_json_line_skipped(self, tmp_path: Path) -> None:
        f = tmp_path / "settings.py"
        f.write_text("secret = 'abc'\n")
        cf = _make_cf([str(f)])
        ndjson = 'not-json\n{"DetectorName":"AWS","Verified":true,"Raw":"x","SourceMetadata":{"Data":{"Filesystem":{"file":"a.py","line":1}}}}\n'
        with (
            patch("ai_pr_review.analyzers.native.trufflehog.shutil.which", return_value="/usr/bin/trufflehog"),
            patch("ai_pr_review.analyzers.native.trufflehog.subprocess.run") as mock_run,
            patch("ai_pr_review.analyzers.native.trufflehog._load_allowlist", return_value=set()),
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout=ndjson, stderr="")
            findings = _run_trufflehog(cf, Path("/dev/null"))
        assert len(findings) == 1
        assert findings[0].severity == "Critical"

    def test_schema_conformance(self, tmp_path: Path) -> None:
        findings = self._run_with_fixture_simple("trufflehog-verified.json", tmp_path)
        assert len(findings) == 1
        f = findings[0]
        assert f.source == "trufflehog"
        assert f.file
        assert f.finding
        assert f.remediation

    def test_timeout_returns_empty(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        import subprocess as sp
        f = tmp_path / "settings.py"
        f.write_text("x\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.trufflehog.shutil.which", return_value="/usr/bin/trufflehog"),
            patch("ai_pr_review.analyzers.native.trufflehog.subprocess.run",
                  side_effect=sp.TimeoutExpired(cmd="trufflehog", timeout=120)),
            patch("ai_pr_review.analyzers.native.trufflehog._load_allowlist", return_value=set()),
            caplog.at_level("WARNING"),
        ):
            result = _run_trufflehog(cf, Path("/dev/null"))
        assert result == []
        assert "timed out" in caplog.text

    def test_oserror_returns_empty(self, tmp_path: Path) -> None:
        f = tmp_path / "settings.py"
        f.write_text("x\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.trufflehog.shutil.which", return_value="/usr/bin/trufflehog"),
            patch("ai_pr_review.analyzers.native.trufflehog.subprocess.run", side_effect=OSError("no binary")),
            patch("ai_pr_review.analyzers.native.trufflehog._load_allowlist", return_value=set()),
        ):
            result = _run_trufflehog(cf, Path("/dev/null"))
        assert result == []

    def test_nonzero_exit_logs_warning_but_returns_findings(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        fixture = _load_fixture("trufflehog-verified.json")
        f = tmp_path / "settings.py"
        f.write_text("x\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.trufflehog.shutil.which", return_value="/usr/bin/trufflehog"),
            patch("ai_pr_review.analyzers.native.trufflehog.subprocess.run") as mock_run,
            patch("ai_pr_review.analyzers.native.trufflehog._load_allowlist", return_value=set()),
            caplog.at_level("WARNING"),
        ):
            mock_run.return_value = MagicMock(returncode=2, stdout=fixture, stderr="err")
            findings = _run_trufflehog(cf, Path("/dev/null"))
        assert len(findings) == 1
        assert "incomplete" in caplog.text


class TestBridgeIntegration:
    @pytest.mark.anyio
    async def test_trufflehog_uses_native_fn(self, tmp_path: Path) -> None:
        from ai_pr_review.analyzers import bridge
        from ai_pr_review.analyzers.bridge import run_analyzers

        cf = ChangedFiles(all_files=["settings.py"])
        called = []

        def fake_native(changed_files: ChangedFiles, diff_file: Path) -> list:
            called.append(True)
            return []

        patched = [
            spec._replace(native_fn=fake_native) if spec.name == "trufflehog" else spec
            for spec in bridge._ANALYZERS
        ]
        with patch.object(bridge, "_ANALYZERS", patched):
            await run_analyzers(cf, "/dev/null")

        assert called, "Native fn was not called"
