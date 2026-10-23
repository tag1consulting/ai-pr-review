"""Tests for the native checkov analyzer."""

from __future__ import annotations

import json
import os
import shutil
import site
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from ai_pr_review.analyzers.native.checkov import (
    _TRUSTED_CONFIG,
    _checkov_env,
    _is_iac_file,
    _run_checkov,
)
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


class TestRunCheckovTrustedConfig:
    def test_workspace_checkov_yaml_never_auto_discovered(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A fork-committed .checkov.yaml (which could set external-checks-dir
        to import arbitrary Python check modules) must never be passed to
        checkov -- an explicit --config-file pointing at a trusted, empty
        file always wins over auto-discovery (#739)."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".checkov.yaml").write_text("external-checks-dir:\n  - ./evil-checks\n")
        tf = tmp_path / "main.tf"
        tf.write_text("resource \"x\" \"y\" {}\n")
        cf = _make_cf([str(tf)], terraform=[str(tf)])
        with (
            patch("ai_pr_review.analyzers.native.checkov.shutil.which", return_value="/usr/bin/checkov"),
            patch("ai_pr_review.analyzers.native.checkov.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            _run_checkov(cf, Path("/dev/null"))
        call_args = mock_run.call_args[0][0]
        assert "--config-file" in call_args
        configured_path = call_args[call_args.index("--config-file") + 1]
        assert configured_path != str(tmp_path / ".checkov.yaml")
        assert not configured_path.startswith(str(tmp_path))

    def test_trusted_config_file_cleaned_up(self, tmp_path: Path) -> None:
        tf = tmp_path / "main.tf"
        tf.write_text("resource \"x\" \"y\" {}\n")
        cf = _make_cf([str(tf)], terraform=[str(tf)])
        with (
            patch("ai_pr_review.analyzers.native.checkov.shutil.which", return_value="/usr/bin/checkov"),
            patch("ai_pr_review.analyzers.native.checkov.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            _run_checkov(cf, Path("/dev/null"))
        call_args = mock_run.call_args[0][0]
        configured_path = call_args[call_args.index("--config-file") + 1]
        assert not Path(configured_path).exists()


_SG_TF = (
    'resource "aws_security_group" "sg" {\n'
    '  name = "x"\n'
    "  ingress {\n"
    "    from_port   = 22\n"
    "    to_port     = 22\n"
    '    protocol    = "tcp"\n'
    '    cidr_blocks = ["0.0.0.0/0"]\n'
    "  }\n"
    "}\n"
)


class TestTrustedConfigContent:
    def test_trusted_config_is_a_yaml_mapping(self) -> None:
        """Checkov rejects a --config-file that is not a YAML mapping and
        exits 2 without a scan (#1102)."""
        assert isinstance(yaml.safe_load(_TRUSTED_CONFIG), dict)

    def test_trusted_config_names_no_workspace_option(self) -> None:
        keys = set(yaml.safe_load(_TRUSTED_CONFIG))
        forbidden = {
            "external-checks-dir", "external-checks-git", "baseline",
            "policy-metadata-filter", "directory", "file", "repo-root-for-plan-enrichment",
        }
        assert not keys & forbidden

    def test_written_config_is_the_constant_and_run_is_isolated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("CKV_CHECK", "CKV_X")
        monkeypatch.setenv("BC_API_KEY", "value")
        monkeypatch.setenv("PRISMA_API_URL", "value")
        monkeypatch.setenv("CHECKOV_MAX_IAC_FILE_SIZE", "1")
        monkeypatch.setenv("EXTERNAL_MODULES_DIR", "/x")
        monkeypatch.setenv("GITHUB_TOKEN", "secret")
        monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:3128")
        tf = tmp_path / "main.tf"
        tf.write_text("resource \"x\" \"y\" {}\n")
        cf = _make_cf(["main.tf"], terraform=["main.tf"])
        seen: dict[str, object] = {}

        def fake_run(cmd: list[str], **kwargs: object) -> MagicMock:
            seen["cmd"] = cmd
            seen["kwargs"] = kwargs
            seen["config"] = Path(cmd[cmd.index("--config-file") + 1]).read_text()
            run_cwd = Path(str(kwargs["cwd"]))
            seen["cwd_entries"] = list(run_cwd.iterdir())
            seen["cwd_exists"] = run_cwd.is_dir()
            return MagicMock(returncode=0, stdout="", stderr="")

        with (
            patch("ai_pr_review.analyzers.native.checkov.shutil.which", return_value="/usr/bin/checkov"),
            patch("ai_pr_review.analyzers.native.checkov.subprocess.run", side_effect=fake_run),
        ):
            _run_checkov(cf, Path("/dev/null"))

        assert seen["config"] == _TRUSTED_CONFIG
        assert seen["cwd_exists"] is True
        assert seen["cwd_entries"] == []
        kwargs = seen["kwargs"]
        assert isinstance(kwargs, dict)
        assert not Path(str(kwargs["cwd"])).resolve().is_relative_to(tmp_path.resolve())
        assert not Path(str(kwargs["cwd"])).exists()
        env = kwargs["env"]
        assert isinstance(env, dict)
        assert env["HOME"] == kwargs["cwd"]
        assert not {"GITHUB_TOKEN", "ANTHROPIC_API_KEY", "CKV_CHECK", "BC_API_KEY", "PRISMA_API_URL",
                    "CHECKOV_MAX_IAC_FILE_SIZE", "EXTERNAL_MODULES_DIR"} & set(env)
        assert {"HOME", "PATH", "PYTHONSAFEPATH"} <= set(env)
        assert env["HTTPS_PROXY"] == "http://proxy.example:3128"
        cmd = seen["cmd"]
        assert isinstance(cmd, list)
        assert cmd[cmd.index("--file") + 1] == str(tmp_path / "main.tf")


@pytest.mark.skipif(
    shutil.which("checkov") is None and not os.environ.get("CHECKOV_REQUIRED_IN_TESTS"),
    reason="checkov is not installed",
)
class TestRealCheckov:
    """Run the real checkov binary. These tests need no network, but checkov
    prints a warning when it cannot reach its guideline service."""

    def test_known_misconfiguration_is_reported(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "main.tf").write_text(_SG_TF)
        findings = _run_checkov(_make_cf(["main.tf"], terraform=["main.tf"]), Path("/dev/null"))
        ids = {f.finding.split(":")[0] for f in findings}
        assert {"CKV_AWS_23", "CKV_AWS_24"} <= ids
        assert {f.file for f in findings} == {"main.tf"}

    def test_workspace_config_baseline_and_external_checks_are_ignored(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A hostile .checkov.yaml/.checkov.yml with external-checks-dir and a
        baseline, in the workspace and in HOME, must not run its Python check
        and must not hide findings (#739, #1102)."""
        ws = tmp_path / "ws"
        home = tmp_path / "home"
        marker = tmp_path / "MARKER"
        (ws / "evil").mkdir(parents=True)
        home.mkdir()
        (ws / "main.tf").write_text(_SG_TF)
        (ws / "evil" / "__init__.py").write_text("")
        (ws / "evil" / "evil_check.py").write_text(
            f"import pathlib\n"
            f"pathlib.Path({str(marker)!r}).write_text('executed')\n"
            "from checkov.common.models.enums import CheckResult, CheckCategories\n"
            "from checkov.terraform.checks.resource.base_resource_check import BaseResourceCheck\n"
            "class Evil(BaseResourceCheck):\n"
            "    def __init__(self):\n"
            "        super().__init__(name='evil', id='CKV_EVIL_1',\n"
            "            categories=[CheckCategories.GENERAL_SECURITY],\n"
            "            supported_resources=['aws_security_group'])\n"
            "    def scan_resource_conf(self, conf):\n"
            "        return CheckResult.FAILED\n"
            "Evil()\n"
        )
        (ws / ".checkov.baseline").write_text(json.dumps({"failed_checks": [{
            "file": "/main.tf",
            "findings": [{"resource": "aws_security_group.sg",
                          "check_ids": ["CKV_AWS_23", "CKV_AWS_24"]}],
        }]}))
        hostile = "external-checks-dir:\n  - ./evil\nbaseline: .checkov.baseline\n"
        (ws / ".checkov.yaml").write_text(hostile)
        (ws / ".checkov.yml").write_text(hostile)
        (home / ".checkov.yaml").write_text(f"external-checks-dir:\n  - {ws / 'evil'}\n")
        monkeypatch.chdir(ws)
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("CKV_CHECK", "CKV_NOTHING")

        findings = _run_checkov(_make_cf(["main.tf"], terraform=["main.tf"]), Path("/dev/null"))

        ids = {f.finding.split(":")[0] for f in findings}
        assert not marker.exists()
        assert "CKV_EVIL_1" not in ids
        assert {"CKV_AWS_23", "CKV_AWS_24"} <= ids


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

    def test_category_is_other_for_non_secret_non_graph_check(self, tmp_path: Path) -> None:
        """Bare CKV_* checks (not CKV_SECRET_*/CKV2_*) default to "other", not
        "lint" -- "lint" is a real category under category-aware clustering
        and would block corroboration with a same-line agent finding."""
        findings = self._run_with_fixture("checkov-failed.json", tmp_path)
        assert findings[0].category == "other"

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

    def test_ckv2_maps_to_authz_category(self, tmp_path: Path) -> None:
        """CKV2_* graph checks are cross-resource access/exposure policy
        violations (e.g. an open security group), not style lint."""
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
        assert findings[0].category == "authz"

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

    def test_exit_1_with_empty_stdout_logs_warning(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        tf = tmp_path / "main.tf"
        tf.write_text("resource \"x\" \"y\" {}\n")
        cf = _make_cf([str(tf)], terraform=[str(tf)])
        with (
            patch("ai_pr_review.analyzers.native.checkov.shutil.which", return_value="/usr/bin/checkov"),
            patch("ai_pr_review.analyzers.native.checkov.subprocess.run") as mock_run,
            caplog.at_level("WARNING"),
        ):
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="ModuleNotFoundError")
            findings = _run_checkov(cf, Path("/dev/null"))
        assert findings == []
        assert "exited 1 with no output" in caplog.text

    def test_user_base_is_passed_so_a_user_install_stays_importable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("PYTHONUSERBASE", raising=False)
        env = _checkov_env("/scratch")
        assert env["HOME"] == "/scratch"
        assert env["PYTHONUSERBASE"] == site.getuserbase()

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


class TestRepoRelativePath:
    def test_inside_workspace(self) -> None:
        from ai_pr_review.analyzers.native.checkov import _repo_relative_path

        assert _repo_relative_path({"file_abs_path": "/ws/sub/main.tf"}, "/ws") == "sub/main.tf"

    def test_dotdot_prefixed_name_stays_inside(self) -> None:
        from ai_pr_review.analyzers.native.checkov import _repo_relative_path

        assert _repo_relative_path({"file_abs_path": "/ws/..x.tf"}, "/ws") == "..x.tf"

    def test_outside_workspace_is_dropped(self) -> None:
        from ai_pr_review.analyzers.native.checkov import _repo_relative_path

        assert _repo_relative_path({"file_abs_path": "/other/main.tf", "repo_file_path": "/main.tf"}, "/ws") == ""
