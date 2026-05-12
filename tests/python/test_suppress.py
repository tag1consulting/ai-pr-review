"""Tests for ai_pr_review.findings.suppress.

All HTTP calls are mocked — no network access required.
"""

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

from ai_pr_review.findings.models import Finding
from ai_pr_review.findings.suppress import (
    SuppressionRule,
    _get,
    _rule_matches,
    _verify_version,
    apply_suppressions,
    load_rules,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _finding(**kwargs: object) -> Finding:
    defaults: dict[str, object] = {
        "severity": "Low",
        "confidence": 60,
        "finding": "test finding",
        "source": "test",
    }
    defaults.update(kwargs)
    return Finding.model_validate(defaults)


def _resp(status_code: int, body: object = None) -> MagicMock:
    r = MagicMock(spec=httpx.Response)
    r.status_code = status_code
    if body is not None:
        r.json.return_value = body
    return r


# ---------------------------------------------------------------------------
# load_rules
# ---------------------------------------------------------------------------


class TestLoadRules:
    def test_empty_when_no_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rules = load_rules(tmpdir)
        assert rules == []

    def test_loads_global_rules(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir) / "config"
            config_dir.mkdir()
            (config_dir / "suppressions.json").write_text(
                json.dumps([{"id": "r1", "reason": "test"}])
            )
            rules = load_rules(tmpdir)
        assert len(rules) == 1
        assert rules[0].id == "r1"

    def test_loads_local_rules_merged(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir) / "config"
            config_dir.mkdir()
            (config_dir / "suppressions.json").write_text(
                json.dumps([{"id": "global", "reason": "g"}])
            )
            ws = Path(tmpdir) / "workspace"
            local_dir = ws / ".github" / "ai-pr-review"
            local_dir.mkdir(parents=True)
            (local_dir / "suppressions.json").write_text(
                json.dumps([{"id": "local", "reason": "l"}])
            )
            rules = load_rules(tmpdir, workspace=str(ws))
        ids = [r.id for r in rules]
        assert "global" in ids
        assert "local" in ids

    def test_invalid_json_warns(self, capsys: pytest.CaptureFixture[str]) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir) / "config"
            config_dir.mkdir()
            (config_dir / "suppressions.json").write_text("not json")
            rules = load_rules(tmpdir)
        assert rules == []
        captured = capsys.readouterr()
        assert "WARNING" in captured.err

    def test_rule_with_match_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir) / "config"
            config_dir.mkdir()
            raw = [
                {
                    "id": "r1",
                    "reason": "ok",
                    "match": {"file": "vendor/.*", "pattern": "CVE-"},
                    "verify": "github-releases",
                }
            ]
            (config_dir / "suppressions.json").write_text(json.dumps(raw))
            rules = load_rules(tmpdir)
        assert rules[0].match_file == "vendor/.*"
        assert rules[0].match_pattern == "CVE-"
        assert rules[0].verify == "github-releases"


# ---------------------------------------------------------------------------
# _rule_matches
# ---------------------------------------------------------------------------


class TestRuleMatches:
    def test_no_conditions_matches_all(self) -> None:
        rule = SuppressionRule(id="r", reason="ok")
        assert _rule_matches(_finding(), rule) is True

    def test_file_pattern_match(self) -> None:
        rule = SuppressionRule(id="r", reason="ok", match_file=r"vendor/")
        f = _finding(file="vendor/lib.py")
        assert _rule_matches(f, rule) is True

    def test_file_pattern_no_match(self) -> None:
        rule = SuppressionRule(id="r", reason="ok", match_file=r"vendor/")
        f = _finding(file="src/main.py")
        assert _rule_matches(f, rule) is False

    def test_text_pattern_match(self) -> None:
        rule = SuppressionRule(id="r", reason="ok", match_pattern=r"SQL injection")
        f = _finding(finding="SQL injection risk found")
        assert _rule_matches(f, rule) is True

    def test_text_pattern_no_match(self) -> None:
        rule = SuppressionRule(id="r", reason="ok", match_pattern=r"SQL injection")
        f = _finding(finding="XSS risk found")
        assert _rule_matches(f, rule) is False

    def test_both_conditions_must_match(self) -> None:
        rule = SuppressionRule(id="r", reason="ok", match_file=r"vendor/", match_pattern="CVE")
        f_both = _finding(file="vendor/lib.py", finding="CVE-2024-1234 found")
        f_file_only = _finding(file="vendor/lib.py", finding="unrelated issue")
        f_pattern_only = _finding(file="src/main.py", finding="CVE-2024-1234 found")
        assert _rule_matches(f_both, rule) is True
        assert _rule_matches(f_file_only, rule) is False
        assert _rule_matches(f_pattern_only, rule) is False


# ---------------------------------------------------------------------------
# apply_suppressions
# ---------------------------------------------------------------------------


class TestApplySuppressions:
    def test_no_rules_keeps_all(self) -> None:
        findings = [_finding(), _finding(finding="another")]
        kept, count = apply_suppressions(findings, [])
        assert len(kept) == 2
        assert count == 0

    def test_matching_rule_suppresses(self) -> None:
        rule = SuppressionRule(id="r", reason="ok", match_pattern="known issue")
        f = _finding(finding="known issue here")
        kept, count = apply_suppressions([f], [rule])
        assert kept == []
        assert count == 1

    def test_non_matching_rule_keeps(self) -> None:
        rule = SuppressionRule(id="r", reason="ok", match_pattern="SQL injection")
        f = _finding(finding="XSS risk")
        kept, count = apply_suppressions([f], [rule])
        assert len(kept) == 1
        assert count == 0

    def test_verify_confirmed_suppresses(self) -> None:
        rule = SuppressionRule(id="r", reason="ok", verify="github-releases")
        f = _finding(finding="owner/repo@v1.2.3 has issue")
        with patch("ai_pr_review.findings.suppress._verify_github_release", return_value=True):
            kept, count = apply_suppressions([f], [rule])
        assert kept == []
        assert count == 1

    def test_verify_not_confirmed_keeps(self) -> None:
        rule = SuppressionRule(id="r", reason="ok", verify="github-releases")
        f = _finding(finding="owner/repo@v1.2.3 has issue")
        with patch("ai_pr_review.findings.suppress._verify_github_release", return_value=False):
            kept, count = apply_suppressions([f], [rule])
        assert len(kept) == 1
        assert count == 0

    def test_mixed_findings(self) -> None:
        rule = SuppressionRule(id="r", reason="ok", match_pattern="suppress me")
        findings = [
            _finding(finding="suppress me"),
            _finding(finding="keep me"),
        ]
        kept, count = apply_suppressions(findings, [rule])
        assert len(kept) == 1
        assert kept[0].finding == "keep me"
        assert count == 1


# ---------------------------------------------------------------------------
# _get (HTTP helper)
# ---------------------------------------------------------------------------


class TestGet:
    def test_success_on_first_attempt(self) -> None:
        with patch("httpx.Client") as mock_client_cls:
            mock_client = mock_client_cls.return_value.__enter__.return_value
            mock_client.get.return_value = _resp(200)
            resp = _get("https://example.com/api")
        assert resp.status_code == 200

    def test_retries_on_transport_error(self) -> None:
        with patch("httpx.Client") as mock_client_cls:
            mock_client = mock_client_cls.return_value.__enter__.return_value
            mock_client.get.side_effect = [
                httpx.TransportError("connection reset"),
                _resp(200),
            ]
            resp = _get("https://example.com/api")
        assert resp.status_code == 200

    def test_raises_after_max_retries(self) -> None:
        with patch("httpx.Client") as mock_client_cls:
            mock_client = mock_client_cls.return_value.__enter__.return_value
            mock_client.get.side_effect = httpx.TransportError("timeout")
            with pytest.raises(httpx.TransportError):
                _get("https://example.com/api")


# ---------------------------------------------------------------------------
# _verify_version
# ---------------------------------------------------------------------------


class TestVerifyVersion:
    def test_unknown_verify_type_returns_false(self) -> None:
        f = _finding(finding="some package 1.2.3")
        result = _verify_version(f, "unknown-registry")
        assert result is False

    def test_exception_in_verify_returns_false(self, capsys: pytest.CaptureFixture[str]) -> None:
        f = _finding(finding="owner/repo@v1.0.0")
        with patch(
            "ai_pr_review.findings.suppress._verify_github_release",
            side_effect=RuntimeError("network error"),
        ):
            result = _verify_version(f, "github-releases")
        assert result is False
        captured = capsys.readouterr()
        assert "WARNING" in captured.err

    def test_github_release_no_match_pattern(self) -> None:
        f = _finding(finding="no version info here")
        with patch("ai_pr_review.findings.suppress._get", return_value=_resp(200)):
            result = _verify_version(f, "github-releases")
        assert result is False

    def test_npm_verified(self) -> None:
        f = _finding(finding="pkg@1.2.3 vulnerability")
        with patch("ai_pr_review.findings.suppress._get", return_value=_resp(200)):
            result = _verify_version(f, "npm")
        assert result is True

    def test_pypi_verified(self) -> None:
        f = _finding(finding="requests==2.31.0 is outdated")
        with patch("ai_pr_review.findings.suppress._get", return_value=_resp(200)):
            result = _verify_version(f, "pypi")
        assert result is True

    def test_go_verified(self) -> None:
        f = _finding(finding="github.com/foo/bar@v1.2.3")
        with patch("ai_pr_review.findings.suppress._get", return_value=_resp(200)):
            result = _verify_version(f, "go")
        assert result is True

    def test_cargo_verified(self) -> None:
        f = _finding(finding="serde 1.0.197 has issue")
        with patch("ai_pr_review.findings.suppress._get", return_value=_resp(200)):
            result = _verify_version(f, "cargo")
        assert result is True

    def test_docker_hub_verified(self) -> None:
        f = _finding(finding="nginx:1.25 has CVE")
        with patch("ai_pr_review.findings.suppress._get", return_value=_resp(200)):
            result = _verify_version(f, "docker-hub")
        assert result is True

    def test_ruby_verified(self) -> None:
        f = _finding(finding="nokogiri 1.15.0 has security issue")
        body = [{"number": "1.15.0"}, {"number": "1.14.0"}]
        with patch("ai_pr_review.findings.suppress._get", return_value=_resp(200, body)):
            result = _verify_version(f, "ruby-org")
        assert result is True

    def test_ruby_not_found(self) -> None:
        f = _finding(finding="nokogiri 9.99.9 has security issue")
        body = [{"number": "1.15.0"}]
        with patch("ai_pr_review.findings.suppress._get", return_value=_resp(200, body)):
            result = _verify_version(f, "ruby-org")
        assert result is False
