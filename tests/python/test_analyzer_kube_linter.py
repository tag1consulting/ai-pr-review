"""Tests for the native kube-linter analyzer."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ai_pr_review.analyzers.native.kube_linter import _HIGH_SEVERITY_CHECKS, _is_k8s_manifest, _run_kube_linter
from ai_pr_review.manifest import ChangedFiles

_FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "kubelinter"


def _load_fixture(name: str) -> str:
    return (_FIXTURE_DIR / name).read_text()


def _make_cf(iac_files: list[str]) -> ChangedFiles:
    return ChangedFiles(all_files=iac_files, iac=iac_files)


class TestIsK8sManifest:
    def test_yaml_with_apiversion_and_kind(self, tmp_path: Path) -> None:
        f = tmp_path / "deploy.yaml"
        f.write_text("apiVersion: apps/v1\nkind: Deployment\n")
        assert _is_k8s_manifest(str(f))

    def test_yaml_missing_apiversion(self, tmp_path: Path) -> None:
        f = tmp_path / "config.yaml"
        f.write_text("key: value\nkind: something\n")
        assert not _is_k8s_manifest(str(f))

    def test_yaml_missing_kind(self, tmp_path: Path) -> None:
        f = tmp_path / "config.yaml"
        f.write_text("apiVersion: v1\nnoKind: here\n")
        assert not _is_k8s_manifest(str(f))

    def test_json_manifest(self, tmp_path: Path) -> None:
        f = tmp_path / "svc.json"
        f.write_text('{"apiVersion": "v1", "kind": "Service"}\n')
        assert _is_k8s_manifest(str(f))

    def test_non_yaml_extension_rejected(self, tmp_path: Path) -> None:
        f = tmp_path / "config.tf"
        f.write_text("apiVersion: v1\nkind: Service\n")
        assert not _is_k8s_manifest(str(f))

    def test_nonexistent_file_returns_false(self) -> None:
        assert not _is_k8s_manifest("/nonexistent/deploy.yaml")


class TestRunKubeLinter:
    def test_no_iac_files_returns_empty(self) -> None:
        cf = ChangedFiles()
        result = _run_kube_linter(cf, Path("/dev/null"))
        assert result == []

    def test_no_k8s_manifests_returns_empty(self, tmp_path: Path) -> None:
        f = tmp_path / "plain.yaml"
        f.write_text("key: value\n")
        cf = _make_cf([str(f)])
        result = _run_kube_linter(cf, Path("/dev/null"))
        assert result == []

    def test_binary_absent_returns_empty(self, tmp_path: Path) -> None:
        f = tmp_path / "deploy.yaml"
        f.write_text("apiVersion: apps/v1\nkind: Deployment\n")
        cf = _make_cf([str(f)])
        with patch("ai_pr_review.analyzers.native.kube_linter.shutil.which", return_value=None):
            result = _run_kube_linter(cf, Path("/dev/null"))
        assert result == []

    def test_binary_absent_logs_warning(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        f = tmp_path / "deploy.yaml"
        f.write_text("apiVersion: apps/v1\nkind: Deployment\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.kube_linter.shutil.which", return_value=None),
            caplog.at_level("WARNING"),
        ):
            _run_kube_linter(cf, Path("/dev/null"))
        assert "kube-linter not found" in caplog.text


class TestRunKubeLinterFindings:
    def _run_with_fixture(self, fixture_name: str, tmp_path: Path) -> list:
        fixture = _load_fixture(fixture_name)
        f = tmp_path / "deploy.yaml"
        f.write_text("apiVersion: apps/v1\nkind: Deployment\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.kube_linter.shutil.which", return_value="/usr/bin/kube-linter"),
            patch("ai_pr_review.analyzers.native.kube_linter.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=1, stdout=fixture, stderr="")
            return _run_kube_linter(cf, Path("/dev/null"))

    def test_violation_finding(self, tmp_path: Path) -> None:
        findings = self._run_with_fixture("kubelinter-violations.json", tmp_path)
        assert len(findings) == 1
        f = findings[0]
        # no-read-only-root-fs is a security check → High
        assert f.severity == "High"
        assert f.confidence == 85
        assert f.source == "kube-linter"
        assert "no-read-only-root-fs" in f.finding
        assert f.file == "k8s/deployment.yaml"
        assert f.line == 22

    def test_empty_reports_returns_empty(self, tmp_path: Path) -> None:
        findings = self._run_with_fixture("kubelinter-empty.json", tmp_path)
        assert findings == []

    def test_malformed_json_returns_empty(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        f = tmp_path / "deploy.yaml"
        f.write_text("apiVersion: apps/v1\nkind: Deployment\n")
        cf = _make_cf([str(f)])
        fixture = _load_fixture("kubelinter-malformed.json")
        with (
            patch("ai_pr_review.analyzers.native.kube_linter.shutil.which", return_value="/usr/bin/kube-linter"),
            patch("ai_pr_review.analyzers.native.kube_linter.subprocess.run") as mock_run,
            caplog.at_level("WARNING"),
        ):
            mock_run.return_value = MagicMock(returncode=1, stdout=fixture, stderr="")
            findings = _run_kube_linter(cf, Path("/dev/null"))
        assert findings == []
        assert "non-JSON" in caplog.text

    def test_finding_text_format(self, tmp_path: Path) -> None:
        findings = self._run_with_fixture("kubelinter-violations.json", tmp_path)
        assert "no-read-only-root-fs" in findings[0].finding
        assert "Deployment" in findings[0].finding
        assert "nginx-deployment" in findings[0].finding

    def test_remediation_present(self, tmp_path: Path) -> None:
        findings = self._run_with_fixture("kubelinter-violations.json", tmp_path)
        assert "readOnlyRootFilesystem" in findings[0].remediation

    def test_high_severity_check_maps_to_high(self, tmp_path: Path) -> None:
        assert "no-read-only-root-fs" in _HIGH_SEVERITY_CHECKS
        findings = self._run_with_fixture("kubelinter-violations.json", tmp_path)
        assert findings[0].severity == "High"

    def test_non_security_check_maps_to_medium(self, tmp_path: Path) -> None:
        payload = json.dumps({
            "Reports": [{
                "Diagnostic": {"Message": "no liveness probe"},
                "Check": "liveness-probe",
                "Remediation": "Add livenessProbe.",
                "Object": {"Metadata": {"FilePath": "a.yaml", "LineNumber": 5},
                           "Type": {"Kind": "Deployment"}, "Name": "app"},
            }],
            "Summary": {"ChecksStatus": "FAILED"},
        })
        f = tmp_path / "deploy.yaml"
        f.write_text("apiVersion: apps/v1\nkind: Deployment\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.kube_linter.shutil.which", return_value="/usr/bin/kube-linter"),
            patch("ai_pr_review.analyzers.native.kube_linter.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=1, stdout=payload, stderr="")
            findings = _run_kube_linter(cf, Path("/dev/null"))
        assert findings[0].severity == "Medium"

    def test_timeout_returns_empty(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        import subprocess as sp
        f = tmp_path / "deploy.yaml"
        f.write_text("apiVersion: apps/v1\nkind: Deployment\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.kube_linter.shutil.which", return_value="/usr/bin/kube-linter"),
            patch("ai_pr_review.analyzers.native.kube_linter.subprocess.run",
                  side_effect=sp.TimeoutExpired(cmd="kube-linter", timeout=120)),
            caplog.at_level("WARNING"),
        ):
            findings = _run_kube_linter(cf, Path("/dev/null"))
        assert findings == []
        assert "timed out" in caplog.text

    def test_oserror_returns_empty(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        f = tmp_path / "deploy.yaml"
        f.write_text("apiVersion: apps/v1\nkind: Deployment\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.kube_linter.shutil.which", return_value="/usr/bin/kube-linter"),
            patch("ai_pr_review.analyzers.native.kube_linter.subprocess.run", side_effect=OSError("not found")),
            caplog.at_level("WARNING"),
        ):
            findings = _run_kube_linter(cf, Path("/dev/null"))
        assert findings == []

    def test_non_zero_non_one_returncode_skipped(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        f = tmp_path / "deploy.yaml"
        f.write_text("apiVersion: apps/v1\nkind: Deployment\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.kube_linter.shutil.which", return_value="/usr/bin/kube-linter"),
            patch("ai_pr_review.analyzers.native.kube_linter.subprocess.run") as mock_run,
            caplog.at_level("WARNING"),
        ):
            mock_run.return_value = MagicMock(returncode=2, stdout="", stderr="error!")
            findings = _run_kube_linter(cf, Path("/dev/null"))
        assert findings == []
        assert "exited 2" in caplog.text

    def test_missing_reports_key_returns_empty_with_warning(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        f = tmp_path / "deploy.yaml"
        f.write_text("apiVersion: apps/v1\nkind: Deployment\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.kube_linter.shutil.which", return_value="/usr/bin/kube-linter"),
            patch("ai_pr_review.analyzers.native.kube_linter.subprocess.run") as mock_run,
            caplog.at_level("WARNING"),
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout='{"summary": "ok"}', stderr="")
            findings = _run_kube_linter(cf, Path("/dev/null"))
        assert findings == []
        assert "missing 'Reports' key" in caplog.text

    def test_non_dict_output_returns_empty_with_warning(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        f = tmp_path / "deploy.yaml"
        f.write_text("apiVersion: apps/v1\nkind: Deployment\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.kube_linter.shutil.which", return_value="/usr/bin/kube-linter"),
            patch("ai_pr_review.analyzers.native.kube_linter.subprocess.run") as mock_run,
            caplog.at_level("WARNING"),
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="[]", stderr="")
            findings = _run_kube_linter(cf, Path("/dev/null"))
        assert findings == []
        assert "unexpected output structure" in caplog.text

    def test_multiple_violations(self, tmp_path: Path) -> None:
        payload = json.dumps({
            "Reports": [
                {
                    "Diagnostic": {"Message": "no read-only root fs"},
                    "Check": "no-read-only-root-fs",
                    "Remediation": "Set readOnlyRootFilesystem.",
                    "Object": {"Metadata": {"FilePath": "a.yaml", "LineNumber": 5},
                               "Type": {"Kind": "Deployment"}, "Name": "app-1"},
                },
                {
                    "Diagnostic": {"Message": "no liveness probe"},
                    "Check": "liveness-probe",
                    "Remediation": "Add livenessProbe.",
                    "Object": {"Metadata": {"FilePath": "b.yaml", "LineNumber": 10},
                               "Type": {"Kind": "Deployment"}, "Name": "app-2"},
                },
            ],
            "Summary": {"ChecksStatus": "FAILED"},
        })
        f = tmp_path / "deploy.yaml"
        f.write_text("apiVersion: apps/v1\nkind: Deployment\n")
        cf = _make_cf([str(f)])
        with (
            patch("ai_pr_review.analyzers.native.kube_linter.shutil.which", return_value="/usr/bin/kube-linter"),
            patch("ai_pr_review.analyzers.native.kube_linter.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=1, stdout=payload, stderr="")
            findings = _run_kube_linter(cf, Path("/dev/null"))
        assert len(findings) == 2
        severities = {f.file: f.severity for f in findings}
        assert severities["a.yaml"] == "High"   # no-read-only-root-fs is a security check
        assert severities["b.yaml"] == "Medium"  # liveness-probe is not


class TestBridgeIntegration:
    @pytest.mark.anyio
    async def test_kube_linter_uses_native_fn(self, tmp_path: Path) -> None:
        from ai_pr_review.analyzers import bridge
        from ai_pr_review.analyzers.bridge import run_analyzers

        cf = ChangedFiles(all_files=["k8s/deploy.yaml"], iac=["k8s/deploy.yaml"])
        called = []

        def fake_native(changed_files: ChangedFiles, diff_file: Path) -> list:
            called.append(True)
            return []

        patched = [
            spec._replace(native_fn=fake_native) if spec.name == "kube-linter" else spec
            for spec in bridge._ANALYZERS
        ]
        with patch.object(bridge, "_ANALYZERS", patched):
            await run_analyzers(cf, "/dev/null")

        assert called, "Native fn was not called"

    @pytest.mark.anyio
    async def test_kube_linter_skipped_when_no_iac_files(self, tmp_path: Path) -> None:
        from ai_pr_review.analyzers import bridge
        from ai_pr_review.analyzers.bridge import AnalyzerSpec, run_analyzers

        called = []

        def fake_native(changed_files: ChangedFiles, diff_file: Path) -> list:
            called.append(True)
            return []

        spec = AnalyzerSpec("kube-linter", ["iac"], fake_native)
        with patch.object(bridge, "_ANALYZERS", [spec]):
            await run_analyzers(ChangedFiles(), "/dev/null")

        assert not called
