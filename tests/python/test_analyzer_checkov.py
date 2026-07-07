"""Tests for the native checkov analyzer."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ai_pr_review.analyzers.native.checkov import _is_iac_file, _run_checkov
from ai_pr_review.manifest import ChangedFiles

_FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "checkov"


def _load_fixture(name: str) -> str:
    return (_FIXTURE_DIR / name).read_text()


def _make_cf(files: list[str], *, terraform: list[str] | None = None, iac: list[str] | None = None, dockerfile: list[str] | None = None) -> ChangedFiles:
    return ChangedFiles(
        all_files=files,
        terraform=terraform or [],
        iac=iac or [],
        dockerfile=dockerfile or [],
    )


class TestIsIacFile:
    def test_tf_file(self, tmp_path: Path) -> None:
        f = tmp_path / "main.tf"
        f.write_text("resource \"aws_s3_bucket\" \"b\" {}\n")
        assert _is_iac_file(str(f))

    def test_tfvars_file(self, tmp_path: Path) -> None:
        f = tmp_path / "vars.tfvars"
        f.write_text("region = \"us-east-1\"\n")
        assert _is_iac_file(str(f))

    def test_dockerfile(self, tmp_path: Path) -> None:
        f = tmp_path / "Dockerfile"
        f.write_text("FROM alpine\n")
        assert _is_iac_file(str(f))

    def test_dockerfile_dot_prefix(self, tmp_path: Path) -> None:
        f = tmp_path / "Dockerfile.dev"
        f.write_text("FROM alpine\n")
        assert _is_iac_file(str(f))

    def test_dot_dockerfile_suffix(self, tmp_path: Path) -> None:
        f = tmp_path / "app.dockerfile"
        f.write_text("FROM alpine\n")
        assert _is_iac_file(str(f))

    def test_k8s_yaml(self, tmp_path: Path) -> None:
        f = tmp_path / "deploy.yaml"
        f.write_text("apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: app\n")
        assert _is_iac_file(str(f))

    def test_k8s_yaml_core_api(self, tmp_path: Path) -> None:
        f = tmp_path / "svc.yaml"
        f.write_text("apiVersion: v1\nkind: Service\nmetadata:\n  name: svc\n")
        assert _is_iac_file(str(f))

    def test_cfn_yaml(self, tmp_path: Path) -> None:
        f = tmp_path / "template.yaml"
        f.write_text("AWSTemplateFormatVersion: '2010-09-09'\nResources: {}\n")
        assert _is_iac_file(str(f))

    def test_azure_arm_yaml(self, tmp_path: Path) -> None:
        f = tmp_path / "arm.yaml"
        f.write_text("$schema: https://schema.management.azure.com/schemas/2019-04-01/deploymentTemplate.json\n")
        assert _is_iac_file(str(f))

    def test_plain_yaml_rejected(self, tmp_path: Path) -> None:
        f = tmp_path / "config.yaml"
        f.write_text("name: myapp\nversion: 1\n")
        assert not _is_iac_file(str(f))

    def test_cfn_json(self, tmp_path: Path) -> None:
        f = tmp_path / "cfn.json"
        f.write_text('{"AWSTemplateFormatVersion": "2010-09-09", "Resources": {}}\n')
        assert _is_iac_file(str(f))

    def test_azure_arm_json(self, tmp_path: Path) -> None:
        f = tmp_path / "arm.json"
        f.write_text('{"$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentTemplate.json"}\n')
        assert _is_iac_file(str(f))

    def test_plain_json_rejected(self, tmp_path: Path) -> None:
        f = tmp_path / "package.json"
        f.write_text('{"name": "myapp", "version": "1.0"}\n')
        assert not _is_iac_file(str(f))

    def test_python_file_rejected(self, tmp_path: Path) -> None:
        f = tmp_path / "main.py"
        f.write_text("x = 1\n")
        assert not _is_iac_file(str(f))


class TestRunCheckovGuards:
    def test_no_iac_files_returns_empty(self, tmp_path: Path) -> None:
        cf = ChangedFiles()
        result = _run_checkov(cf, Path("/dev/null"))
        assert result == []

    def test_binary_absent_returns_empty(self, tmp_path: Path) -> None:
        tf = tmp_path / "main.tf"
        tf.write_text("resource \"x\" \"y\" {}\n")
        cf = _make_cf([str(tf)], terraform=[str(tf)])
        with patch("ai_pr_review.analyzers.native.checkov.shutil.which", return_value=None):
            result = _run_checkov(cf, Path("/dev/null"))
        assert result == []

    def test_binary_absent_logs_warning(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        tf = tmp_path / "main.tf"
        tf.write_text("resource \"x\" \"y\" {}\n")
        cf = _make_cf([str(tf)], terraform=[str(tf)])
        with (
            patch("ai_pr_review.analyzers.native.checkov.shutil.which", return_value=None),
            caplog.at_level("WARNING"),
        ):
            _run_checkov(cf, Path("/dev/null"))
        assert "checkov not found" in caplog.text


class TestRunCheckovFindings:
    def _run_with_fixture(self, fixture_name: str, tmp_path: Path) -> list:
        fixture = _load_fixture(fixture_name)
        tf = tmp_path / "main.tf"
        tf.write_text("resource \"x\" \"y\" {}\n")
        cf = _make_cf([str(tf)], terraform=[str(tf)])
        with (
            patch("ai_pr_review.analyzers.native.checkov.shutil.which", return_value="/usr/bin/checkov"),
            patch("ai_pr_review.analyzers.native.checkov.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=1, stdout=fixture, stderr="")
            return _run_checkov(cf, Path("/dev/null"))

    def test_failed_check_medium_severity(self, tmp_path: Path) -> None:
        findings = self._run_with_fixture("checkov-failed.json", tmp_path)
        assert len(findings) == 1
        f = findings[0]
        assert f.severity == "Medium"
        assert f.confidence == 80
        assert f.source == "checkov"
        assert "CKV_AWS_57" in f.finding
        assert f.line == 10

    def test_category_is_lint_for_non_secret_check(self, tmp_path: Path) -> None:
        findings = self._run_with_fixture("checkov-failed.json", tmp_path)
        assert findings[0].category == "lint"

    def test_empty_failed_checks_returns_empty(self, tmp_path: Path) -> None:
        findings = self._run_with_fixture("checkov-empty.json", tmp_path)
        assert findings == []

    def test_malformed_json_returns_empty(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        fixture = _load_fixture("checkov-malformed.json")
        tf = tmp_path / "main.tf"
        tf.write_text("resource \"x\" \"y\" {}\n")
        cf = _make_cf([str(tf)], terraform=[str(tf)])
        with (
            patch("ai_pr_review.analyzers.native.checkov.shutil.which", return_value="/usr/bin/checkov"),
            patch("ai_pr_review.analyzers.native.checkov.subprocess.run") as mock_run,
            caplog.at_level("WARNING"),
        ):
            mock_run.return_value = MagicMock(returncode=1, stdout=fixture, stderr="")
            result = _run_checkov(cf, Path("/dev/null"))
        assert result == []
        assert "non-JSON" in caplog.text

    def test_ckv2_maps_to_high(self, tmp_path: Path) -> None:
        payload = json.dumps({
            "results": {
                "failed_checks": [{
                    "check_id": "CKV2_AWS_1",
                    "check_id_name": "Some v2 rule",
                    "repo_file_path": "main.tf",
                    "file_line_range": [1, 5],
                }],
            }
        })
        tf = tmp_path / "main.tf"
        tf.write_text("resource \"x\" \"y\" {}\n")
        cf = _make_cf([str(tf)], terraform=[str(tf)])
        with (
            patch("ai_pr_review.analyzers.native.checkov.shutil.which", return_value="/usr/bin/checkov"),
            patch("ai_pr_review.analyzers.native.checkov.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=1, stdout=payload, stderr="")
            findings = _run_checkov(cf, Path("/dev/null"))
        assert findings[0].severity == "High"

    def test_ckv_secret_maps_to_high(self, tmp_path: Path) -> None:
        payload = json.dumps({
            "results": {
                "failed_checks": [{
                    "check_id": "CKV_SECRET_6",
                    "check_id_name": "Secret found",
                    "repo_file_path": "main.tf",
                    "file_line_range": [3, 3],
                }],
            }
        })
        tf = tmp_path / "main.tf"
        tf.write_text("resource \"x\" \"y\" {}\n")
        cf = _make_cf([str(tf)], terraform=[str(tf)])
        with (
            patch("ai_pr_review.analyzers.native.checkov.shutil.which", return_value="/usr/bin/checkov"),
            patch("ai_pr_review.analyzers.native.checkov.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=1, stdout=payload, stderr="")
            findings = _run_checkov(cf, Path("/dev/null"))
        assert findings[0].severity == "High"

    def test_ckv_secret_maps_to_secret_category(self, tmp_path: Path) -> None:
        payload = json.dumps({
            "results": {
                "failed_checks": [{
                    "check_id": "CKV_SECRET_6",
                    "check_id_name": "Secret found",
                    "repo_file_path": "main.tf",
                    "file_line_range": [3, 3],
                }],
            }
        })
        tf = tmp_path / "main.tf"
        tf.write_text("resource \"x\" \"y\" {}\n")
        cf = _make_cf([str(tf)], terraform=[str(tf)])
        with (
            patch("ai_pr_review.analyzers.native.checkov.shutil.which", return_value="/usr/bin/checkov"),
            patch("ai_pr_review.analyzers.native.checkov.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=1, stdout=payload, stderr="")
            findings = _run_checkov(cf, Path("/dev/null"))
        assert findings[0].category == "secret"

    def test_array_output_normalised(self, tmp_path: Path) -> None:
        payload = json.dumps([
            {"results": {"failed_checks": [{
                "check_id": "CKV_AWS_1", "check_id_name": "rule 1",
                "repo_file_path": "a.tf", "file_line_range": [1, 2],
            }]}},
            {"results": {"failed_checks": [{
                "check_id": "CKV_AWS_2", "check_id_name": "rule 2",
                "repo_file_path": "a.tf", "file_line_range": [5, 6],
            }]}},
        ])
        tf = tmp_path / "a.tf"
        tf.write_text("resource \"x\" \"y\" {}\n")
        cf = _make_cf([str(tf)], terraform=[str(tf)])
        with (
            patch("ai_pr_review.analyzers.native.checkov.shutil.which", return_value="/usr/bin/checkov"),
            patch("ai_pr_review.analyzers.native.checkov.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=1, stdout=payload, stderr="")
            findings = _run_checkov(cf, Path("/dev/null"))
        assert len(findings) == 2

    def test_repo_file_path_leading_slash_stripped(self, tmp_path: Path) -> None:
        payload = json.dumps({
            "results": {
                "failed_checks": [{
                    "check_id": "CKV_AWS_57",
                    "check_id_name": "rule",
                    "repo_file_path": "/src/main.tf",
                    "file_line_range": [1, 1],
                }],
            }
        })
        tf = tmp_path / "main.tf"
        tf.write_text("resource \"x\" \"y\" {}\n")
        cf = _make_cf([str(tf)], terraform=[str(tf)])
        with (
            patch("ai_pr_review.analyzers.native.checkov.shutil.which", return_value="/usr/bin/checkov"),
            patch("ai_pr_review.analyzers.native.checkov.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=1, stdout=payload, stderr="")
            findings = _run_checkov(cf, Path("/dev/null"))
        assert not findings[0].file.startswith("/")
        assert findings[0].file == "src/main.tf"

    def test_guideline_used_as_remediation(self, tmp_path: Path) -> None:
        findings = self._run_with_fixture("checkov-failed.json", tmp_path)
        assert "https://docs.aws.amazon.com" in findings[0].remediation

    def test_no_guideline_uses_default_remediation(self, tmp_path: Path) -> None:
        payload = json.dumps({
            "results": {
                "failed_checks": [{
                    "check_id": "CKV_AWS_57",
                    "check_id_name": "rule",
                    "repo_file_path": "main.tf",
                    "file_line_range": [1, 1],
                }],
            }
        })
        tf = tmp_path / "main.tf"
        tf.write_text("resource \"x\" \"y\" {}\n")
        cf = _make_cf([str(tf)], terraform=[str(tf)])
        with (
            patch("ai_pr_review.analyzers.native.checkov.shutil.which", return_value="/usr/bin/checkov"),
            patch("ai_pr_review.analyzers.native.checkov.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=1, stdout=payload, stderr="")
            findings = _run_checkov(cf, Path("/dev/null"))
        assert "prismacloud" in findings[0].remediation

    def test_empty_stdout_returns_empty(self, tmp_path: Path) -> None:
        tf = tmp_path / "main.tf"
        tf.write_text("resource \"x\" \"y\" {}\n")
        cf = _make_cf([str(tf)], terraform=[str(tf)])
        with (
            patch("ai_pr_review.analyzers.native.checkov.shutil.which", return_value="/usr/bin/checkov"),
            patch("ai_pr_review.analyzers.native.checkov.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            findings = _run_checkov(cf, Path("/dev/null"))
        assert findings == []

    def test_exitcode_2_returns_empty_with_warning(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        tf = tmp_path / "main.tf"
        tf.write_text("resource \"x\" \"y\" {}\n")
        cf = _make_cf([str(tf)], terraform=[str(tf)])
        with (
            patch("ai_pr_review.analyzers.native.checkov.shutil.which", return_value="/usr/bin/checkov"),
            patch("ai_pr_review.analyzers.native.checkov.subprocess.run") as mock_run,
            caplog.at_level("WARNING"),
        ):
            mock_run.return_value = MagicMock(returncode=2, stdout="", stderr="install error")
            findings = _run_checkov(cf, Path("/dev/null"))
        assert findings == []
        assert "exited 2" in caplog.text

    def test_timeout_returns_empty(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        import subprocess as sp
        tf = tmp_path / "main.tf"
        tf.write_text("resource \"x\" \"y\" {}\n")
        cf = _make_cf([str(tf)], terraform=[str(tf)])
        with (
            patch("ai_pr_review.analyzers.native.checkov.shutil.which", return_value="/usr/bin/checkov"),
            patch("ai_pr_review.analyzers.native.checkov.subprocess.run",
                  side_effect=sp.TimeoutExpired(cmd="checkov", timeout=120)),
            caplog.at_level("WARNING"),
        ):
            findings = _run_checkov(cf, Path("/dev/null"))
        assert findings == []
        assert "timed out" in caplog.text

    def test_oserror_returns_empty(self, tmp_path: Path) -> None:
        tf = tmp_path / "main.tf"
        tf.write_text("resource \"x\" \"y\" {}\n")
        cf = _make_cf([str(tf)], terraform=[str(tf)])
        with (
            patch("ai_pr_review.analyzers.native.checkov.shutil.which", return_value="/usr/bin/checkov"),
            patch("ai_pr_review.analyzers.native.checkov.subprocess.run", side_effect=OSError("not found")),
        ):
            findings = _run_checkov(cf, Path("/dev/null"))
        assert findings == []

    def test_file_line_range_missing_defaults_to_1(self, tmp_path: Path) -> None:
        payload = json.dumps({
            "results": {
                "failed_checks": [{
                    "check_id": "CKV_AWS_57",
                    "check_id_name": "rule",
                    "repo_file_path": "main.tf",
                }],
            }
        })
        tf = tmp_path / "main.tf"
        tf.write_text("resource \"x\" \"y\" {}\n")
        cf = _make_cf([str(tf)], terraform=[str(tf)])
        with (
            patch("ai_pr_review.analyzers.native.checkov.shutil.which", return_value="/usr/bin/checkov"),
            patch("ai_pr_review.analyzers.native.checkov.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=1, stdout=payload, stderr="")
            findings = _run_checkov(cf, Path("/dev/null"))
        assert findings[0].line == 1


class TestBridgeIntegration:
    @pytest.mark.anyio
    async def test_checkov_uses_native_fn(self, tmp_path: Path) -> None:
        from ai_pr_review.analyzers import bridge
        from ai_pr_review.analyzers.bridge import run_analyzers

        cf = ChangedFiles(all_files=["main.tf"], terraform=["main.tf"])
        called = []

        def fake_native(changed_files: ChangedFiles, diff_file: Path) -> list:
            called.append(True)
            return []

        patched = [
            spec._replace(native_fn=fake_native) if spec.name == "checkov" else spec
            for spec in bridge._ANALYZERS
        ]
        with patch.object(bridge, "_ANALYZERS", patched):
            await run_analyzers(cf, "/dev/null")

        assert called, "Native fn was not called"

    @pytest.mark.anyio
    async def test_checkov_skipped_when_no_iac_files(self, tmp_path: Path) -> None:
        from ai_pr_review.analyzers import bridge
        from ai_pr_review.analyzers.bridge import AnalyzerSpec, run_analyzers

        called = []

        def fake_native(changed_files: ChangedFiles, diff_file: Path) -> list:
            called.append(True)
            return []

        spec = AnalyzerSpec("checkov", ["terraform", "iac", "dockerfile"], fake_native)
        with patch.object(bridge, "_ANALYZERS", [spec]):
            await run_analyzers(ChangedFiles(), "/dev/null")

        assert not called
