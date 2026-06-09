"""Tests for the native cve-check analyzer."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from ai_pr_review.analyzers.native.cve_check import (
    _cvss_v3_score,
    _parse_composer_json,
    _parse_go_mod,
    _parse_package_json,
    _parse_requirements_txt,
    _run_cve_check,
)
from ai_pr_review.manifest import ChangedFiles

_FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "cve"


def _load_fixture(name: str) -> str:
    return (_FIXTURE_DIR / name).read_text()


def _make_cf(manifest_files: list[str]) -> ChangedFiles:
    return ChangedFiles(all_files=manifest_files, manifest_lockfile=manifest_files)


class TestCvssV3Score:
    def test_critical_vector(self) -> None:
        score = _cvss_v3_score("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H")
        assert score is not None
        assert score >= 9.0

    def test_medium_vector(self) -> None:
        score = _cvss_v3_score("CVSS:3.1/AV:N/AC:H/PR:L/UI:N/S:U/C:L/I:L/A:L")
        assert score is not None
        assert 4.0 <= score < 7.0

    def test_missing_metrics_returns_none(self) -> None:
        score = _cvss_v3_score("CVSS:3.1/AV:N/AC:L")
        assert score is None

    def test_zero_impact_returns_zero(self) -> None:
        score = _cvss_v3_score("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:N/A:N")
        assert score == 0.0

    def test_scope_changed_vector(self) -> None:
        score = _cvss_v3_score("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:C/C:H/I:H/A:H")
        assert score is not None
        assert score >= 9.0


class TestParseGoMod:
    def test_parses_require_block(self, tmp_path: Path) -> None:
        content = _load_fixture("go.mod.sample")
        result = _parse_go_mod(content, str(tmp_path / "go.mod"))
        names = [p.name for p in result]
        assert "github.com/gin-gonic/gin" in names
        assert "github.com/stretchr/testify" in names
        assert "golang.org/x/crypto" in names

    def test_strips_v_prefix(self, tmp_path: Path) -> None:
        content = _load_fixture("go.mod.sample")
        result = _parse_go_mod(content, str(tmp_path / "go.mod"))
        gin = next(p for p in result if p.name == "github.com/gin-gonic/gin")
        assert gin.version == "1.6.0"

    def test_ecosystem_is_go(self, tmp_path: Path) -> None:
        content = _load_fixture("go.mod.sample")
        result = _parse_go_mod(content, str(tmp_path / "go.mod"))
        assert all(p.ecosystem == "Go" for p in result)

    def test_replace_directive_applied(self, tmp_path: Path) -> None:
        content = _load_fixture("go.mod.replace.sample")
        result = _parse_go_mod(content, str(tmp_path / "go.mod"))
        names = [p.name for p in result]
        assert "github.com/old-module/lib" not in names
        assert "github.com/new-module/lib" in names

    def test_local_replace_skipped(self, tmp_path: Path) -> None:
        content = _load_fixture("go.mod.replace.sample")
        result = _parse_go_mod(content, str(tmp_path / "go.mod"))
        names = [p.name for p in result]
        assert "github.com/local-only/thing" not in names


class TestParsePackageJson:
    def test_parses_dependencies(self, tmp_path: Path) -> None:
        content = _load_fixture("package.json.sample")
        result = _parse_package_json(content, str(tmp_path / "package.json"))
        names = [p.name for p in result]
        assert "lodash" in names
        assert "express" in names

    def test_parses_dev_dependencies(self, tmp_path: Path) -> None:
        content = _load_fixture("package.json.sample")
        result = _parse_package_json(content, str(tmp_path / "package.json"))
        jest = next(p for p in result if p.name == "jest")
        assert jest.tag == "dev"

    def test_strips_semver_prefix(self, tmp_path: Path) -> None:
        content = _load_fixture("package.json.sample")
        result = _parse_package_json(content, str(tmp_path / "package.json"))
        lodash = next(p for p in result if p.name == "lodash")
        assert lodash.version.startswith("4")

    def test_ecosystem_is_npm(self, tmp_path: Path) -> None:
        content = _load_fixture("package.json.sample")
        result = _parse_package_json(content, str(tmp_path / "package.json"))
        assert all(p.ecosystem == "npm" for p in result)

    def test_malformed_json_returns_empty(self, tmp_path: Path) -> None:
        result = _parse_package_json("not json", str(tmp_path / "package.json"))
        assert result == []


class TestParseRequirementsTxt:
    def test_parses_exact_pin(self, tmp_path: Path) -> None:
        content = _load_fixture("requirements.txt.sample")
        result = _parse_requirements_txt(content, str(tmp_path / "requirements.txt"))
        names = [p.name for p in result]
        assert "Django" in names
        assert "requests" in names
        assert "pyyaml" in names

    def test_skips_unpinned(self, tmp_path: Path) -> None:
        content = "flask>=2.0.0\nDjango==4.0.0\n"
        result = _parse_requirements_txt(content, str(tmp_path / "requirements.txt"))
        names = [p.name for p in result]
        assert "flask" not in names
        assert "Django" in names

    def test_ecosystem_is_pypi(self, tmp_path: Path) -> None:
        content = _load_fixture("requirements.txt.sample")
        result = _parse_requirements_txt(content, str(tmp_path / "requirements.txt"))
        assert all(p.ecosystem == "PyPI" for p in result)


class TestParseComposerJson:
    def test_parses_require(self, tmp_path: Path) -> None:
        content = _load_fixture("composer.json.sample")
        result = _parse_composer_json(content, str(tmp_path / "composer.json"))
        names = [p.name for p in result]
        assert "symfony/http-foundation" in names
        assert "guzzlehttp/guzzle" in names

    def test_skips_php_platform(self, tmp_path: Path) -> None:
        content = _load_fixture("composer.json.sample")
        result = _parse_composer_json(content, str(tmp_path / "composer.json"))
        names = [p.name for p in result]
        assert "php" not in names
        assert "ext-json" not in names

    def test_ecosystem_is_packagist(self, tmp_path: Path) -> None:
        content = _load_fixture("composer.json.sample")
        result = _parse_composer_json(content, str(tmp_path / "composer.json"))
        assert all(p.ecosystem == "Packagist" for p in result)

    def test_malformed_json_returns_empty(self, tmp_path: Path) -> None:
        result = _parse_composer_json("not json", str(tmp_path / "composer.json"))
        assert result == []


_CRITICAL_VULN = {
    "id": "GHSA-xxxx-yyyy-zzzz",
    "aliases": ["CVE-2025-99999"],
    "summary": "Remote code execution via unsanitized input",
    "severity": [{"type": "CVSS_V3", "score": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"}],
    "affected": [{"package": {"name": "github.com/gin-gonic/gin", "ecosystem": "Go"},
                  "ranges": [{"type": "SEMVER", "events": [{"introduced": "0"}, {"fixed": "1.7.7"}]}]}],
}

_MEDIUM_VULN = {
    "id": "GHSA-aaaa-bbbb-cccc",
    "aliases": ["CVE-2024-55555"],
    "summary": "Prototype pollution in helper function",
    "severity": [{"type": "CVSS_V3", "score": "CVSS:3.1/AV:N/AC:H/PR:L/UI:N/S:U/C:L/I:L/A:L"}],
    "affected": [{"package": {"name": "lodash", "ecosystem": "npm"},
                  "ranges": [{"type": "SEMVER", "events": [{"introduced": "0"}, {"fixed": "4.17.21"}]}]}],
}


class TestRunCveCheck:
    def _run_with_batch(self, manifest_file: str, batch_results: list[dict], tmp_path: Path) -> list:
        """Run _run_cve_check with a mocked _query_osv_batch returning batch_results."""
        # Strip .sample suffix so the parser recognizes the manifest type
        base = Path(manifest_file).name.removesuffix(".sample")
        target = tmp_path / base
        target.write_text(_load_fixture(manifest_file))
        cf = _make_cf([str(target)])
        with patch("ai_pr_review.analyzers.native.cve_check._query_osv_batch", return_value=batch_results):
            return _run_cve_check(cf, Path("/dev/null"))

    def test_no_manifest_files_returns_empty(self) -> None:
        cf = ChangedFiles()
        result = _run_cve_check(cf, Path("/dev/null"))
        assert result == []

    def test_go_mod_critical_finding(self, tmp_path: Path) -> None:
        # go.mod.sample has 3 packages; provide one vuln for the first (gin)
        results = [{"vulns": [_CRITICAL_VULN]}, {}, {}]
        findings = self._run_with_batch("go.mod.sample", results, tmp_path)
        assert len(findings) >= 1
        critical = next((f for f in findings if f.severity == "Critical"), None)
        assert critical is not None
        assert critical.source == "osv"
        assert critical.agent == "dependency-check"

    def test_osv_medium_finding(self, tmp_path: Path) -> None:
        # package.json.sample has 3 packages; lodash is first
        results = [{"vulns": [_MEDIUM_VULN]}, {}, {}]
        findings = self._run_with_batch("package.json.sample", results, tmp_path)
        assert len(findings) >= 1
        assert any(f.severity == "Medium" for f in findings)

    def test_empty_osv_response_returns_empty(self, tmp_path: Path) -> None:
        results = [{}, {}, {}]
        findings = self._run_with_batch("go.mod.sample", results, tmp_path)
        assert findings == []

    def test_source_is_osv(self, tmp_path: Path) -> None:
        results = [{"vulns": [_CRITICAL_VULN]}, {}, {}]
        findings = self._run_with_batch("go.mod.sample", results, tmp_path)
        assert all(f.source == "osv" for f in findings)

    def test_agent_is_dependency_check(self, tmp_path: Path) -> None:
        results = [{"vulns": [_CRITICAL_VULN]}, {}, {}]
        findings = self._run_with_batch("go.mod.sample", results, tmp_path)
        assert all(f.agent == "dependency-check" for f in findings)

    def test_cve_id_in_finding_text(self, tmp_path: Path) -> None:
        results = [{"vulns": [_CRITICAL_VULN]}, {}, {}]
        findings = self._run_with_batch("go.mod.sample", results, tmp_path)
        assert any("CVE-" in f.finding for f in findings)

    def test_remediation_mentions_upgrade_or_monitor(self, tmp_path: Path) -> None:
        results = [{"vulns": [_CRITICAL_VULN]}, {}, {}]
        findings = self._run_with_batch("go.mod.sample", results, tmp_path)
        assert all("Upgrade" in f.remediation or "Monitor" in f.remediation for f in findings)

    def test_no_fixed_version_remediation_says_monitor(self, tmp_path: Path) -> None:
        vuln = {**_CRITICAL_VULN, "affected": []}  # no fixed version
        results = [{"vulns": [vuln]}, {}, {}]
        findings = self._run_with_batch("go.mod.sample", results, tmp_path)
        assert any("Monitor" in f.remediation for f in findings)

    def test_unknown_cvss_defaults_to_high_70(self, tmp_path: Path) -> None:
        vuln = {
            "id": "GHSA-zzzz-zzzz-zzzz",
            "aliases": [],
            "summary": "Unknown severity vuln",
            "severity": [],  # no CVSS
            "affected": [],
        }
        results = [{"vulns": [vuln]}, {}, {}]
        findings = self._run_with_batch("go.mod.sample", results, tmp_path)
        assert any(f.severity == "High" and f.confidence == 70 for f in findings)

    def test_malformed_query_batch_returns_empty(self, tmp_path: Path) -> None:
        target = tmp_path / "go.mod"
        target.write_text(_load_fixture("go.mod.sample"))
        cf = _make_cf([str(target)])
        with patch("ai_pr_review.analyzers.native.cve_check._query_osv_batch", return_value=[]):
            result = _run_cve_check(cf, Path("/dev/null"))
        assert result == []

    def test_network_timeout_returns_empty(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        import httpx

        target = tmp_path / "go.mod"
        target.write_text(_load_fixture("go.mod.sample"))
        cf = _make_cf([str(target)])

        with (
            patch("ai_pr_review.analyzers.native.cve_check.httpx.post",
                  side_effect=httpx.TimeoutException("timeout")),
            caplog.at_level("WARNING"),
        ):
            result = _run_cve_check(cf, Path("/dev/null"))
        assert result == []
        assert "timed out" in caplog.text

    def test_http_error_returns_empty(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        import httpx

        target = tmp_path / "go.mod"
        target.write_text(_load_fixture("go.mod.sample"))
        cf = _make_cf([str(target)])

        with (
            patch("ai_pr_review.analyzers.native.cve_check.httpx.post",
                  side_effect=httpx.HTTPError("connection refused")),
            caplog.at_level("WARNING"),
        ):
            result = _run_cve_check(cf, Path("/dev/null"))
        assert result == []
        assert "failed" in caplog.text

    def test_multi_package_batch_response(self, tmp_path: Path) -> None:
        target = tmp_path / "go.mod"
        target.write_text(_load_fixture("go.mod.sample"))
        cf = _make_cf([str(target)])
        # go.mod has 3 packages; first and third have vulns
        osv_batch = json.loads(_load_fixture("osv-batch-multi.json"))
        # Pad to match package count from go.mod (3 packages)
        results_raw = osv_batch.get("results", [])
        while len(results_raw) < 3:
            results_raw.append({})
        with patch("ai_pr_review.analyzers.native.cve_check._query_osv_batch", return_value=results_raw):
            findings = _run_cve_check(cf, Path("/dev/null"))
        assert len(findings) >= 1

    def test_nonexistent_manifest_skipped(self, tmp_path: Path) -> None:
        cf = _make_cf([str(tmp_path / "nonexistent.go.mod")])
        result = _run_cve_check(cf, Path("/dev/null"))
        assert result == []


class TestBridgeIntegration:
    @pytest.mark.anyio
    async def test_cve_check_uses_native_fn(self, tmp_path: Path) -> None:
        from ai_pr_review.analyzers import bridge
        from ai_pr_review.analyzers.bridge import run_analyzers

        cf = ChangedFiles(all_files=["go.mod"], manifest_lockfile=["go.mod"])
        called = []

        def fake_native(changed_files: ChangedFiles, diff_file: Path) -> list:
            called.append(True)
            return []

        patched = [
            spec._replace(native_fn=fake_native) if spec.name == "cve-check" else spec
            for spec in bridge._ANALYZERS
        ]
        with patch.object(bridge, "_ANALYZERS", patched):
            await run_analyzers(cf, "/dev/null", str(tmp_path))

        assert called, "Native fn was not called"

    @pytest.mark.anyio
    async def test_cve_check_skipped_when_no_manifest_files(self, tmp_path: Path) -> None:
        from ai_pr_review.analyzers import bridge
        from ai_pr_review.analyzers.bridge import AnalyzerSpec, run_analyzers

        called = []

        def fake_native(changed_files: ChangedFiles, diff_file: Path) -> list:
            called.append(True)
            return []

        spec = AnalyzerSpec("cve-check", "run-cve-check.sh", ["manifest_lockfile"], fake_native)
        with patch.object(bridge, "_ANALYZERS", [spec]):
            await run_analyzers(ChangedFiles(), "/dev/null", str(tmp_path))

        assert not called
