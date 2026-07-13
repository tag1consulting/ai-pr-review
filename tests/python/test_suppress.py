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
                json.dumps([{"id": "local", "reason": "l", "match": {"file": "vendor/.*"}}])
            )
            rules = load_rules(tmpdir, workspace=str(ws))
        ids = [r.id for r in rules]
        assert "global" in ids
        assert "local" in ids

    def test_local_catch_all_rule_rejected(self, capsys: pytest.CaptureFixture[str]) -> None:
        # A local (PR-controlled) rule with an empty/absent match object would
        # suppress every finding. It must be dropped, while a constrained local
        # rule in the same file is still accepted.
        with tempfile.TemporaryDirectory() as tmpdir:
            ws = Path(tmpdir) / "workspace"
            local_dir = ws / ".github" / "ai-pr-review"
            local_dir.mkdir(parents=True)
            (local_dir / "suppressions.json").write_text(
                json.dumps(
                    [
                        {"id": "evil", "reason": "silence all", "match": {}},
                        {"id": "nomatch", "reason": "no match key at all"},
                        {"id": "ok", "reason": "scoped", "match": {"file": "vendor/.*"}},
                    ]
                )
            )
            rules = load_rules(tmpdir, workspace=str(ws))
        ids = [r.id for r in rules]
        assert "evil" not in ids
        assert "nomatch" not in ids
        assert "ok" in ids
        assert "WARNING" in capsys.readouterr().err

    def test_global_catch_all_rule_allowed(self) -> None:
        # The global config ships with the action (trusted); a catch-all there
        # is the operator's own choice and must still load.
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir) / "config"
            config_dir.mkdir()
            (config_dir / "suppressions.json").write_text(
                json.dumps([{"id": "global-catchall", "reason": "ok", "match": {}}])
            )
            rules = load_rules(tmpdir)
        assert [r.id for r in rules] == ["global-catchall"]

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
# _rule_matches — line-range (match_line_start / match_line_end)
# ---------------------------------------------------------------------------


class TestRuleMatchesLineRange:
    def test_range_hit(self) -> None:
        """Finding inside the rule window is suppressed."""
        rule = SuppressionRule(id="r", reason="ok", match_line_start=100, match_line_end=200)
        f = _finding(line=150)
        assert _rule_matches(f, rule) is True

    def test_range_miss_below(self) -> None:
        """Finding below the window is not suppressed."""
        rule = SuppressionRule(id="r", reason="ok", match_line_start=100, match_line_end=200)
        f = _finding(line=50)
        assert _rule_matches(f, rule) is False

    def test_range_miss_above(self) -> None:
        """Finding above the window is not suppressed."""
        rule = SuppressionRule(id="r", reason="ok", match_line_start=100, match_line_end=200)
        f = _finding(line=250)
        assert _rule_matches(f, rule) is False

    def test_open_lower_bound(self) -> None:
        """line_start=0 (unset) means no lower bound — early lines are matched."""
        rule = SuppressionRule(id="r", reason="ok", match_line_start=0, match_line_end=200)
        f = _finding(line=5)
        assert _rule_matches(f, rule) is True

    def test_open_upper_bound(self) -> None:
        """line_end=0 (unset) means no upper bound — very late lines are matched."""
        rule = SuppressionRule(id="r", reason="ok", match_line_start=100, match_line_end=0)
        f = _finding(line=9999)
        assert _rule_matches(f, rule) is True

    def test_unanchored_finding_not_matched(self) -> None:
        """A finding with no line number is never matched by a range rule."""
        rule = SuppressionRule(id="r", reason="ok", match_line_start=1, match_line_end=200)
        f = _finding()  # line defaults to None
        assert _rule_matches(f, rule) is False

    def test_multiline_finding_overlap(self) -> None:
        """Multi-line finding whose span overlaps the rule window is suppressed."""
        rule = SuppressionRule(id="r", reason="ok", match_line_start=100, match_line_end=200)
        # Finding spans 190–210; overlaps with 100–200
        f = _finding(start_line=190, line=210)
        assert _rule_matches(f, rule) is True

    def test_multiline_finding_no_overlap(self) -> None:
        """Multi-line finding entirely above the window is not suppressed."""
        rule = SuppressionRule(id="r", reason="ok", match_line_start=100, match_line_end=200)
        # Finding spans 210–220; entirely above the window
        f = _finding(start_line=210, line=220)
        assert _rule_matches(f, rule) is False

    def test_misconfigured_rule_start_gt_end(self) -> None:
        """Rule with line_start > line_end never matches any finding."""
        rule = SuppressionRule(id="r", reason="ok", match_line_start=200, match_line_end=100)
        f = _finding(line=150)
        assert _rule_matches(f, rule) is False

    def test_combined_with_match_file_hit(self) -> None:
        """Rule with file + line range: matching file and line → suppressed."""
        rule = SuppressionRule(
            id="r", reason="ok", match_file=r"patches/", match_line_start=1, match_line_end=200
        )
        f = _finding(file="patches/lib.c", line=150)
        assert _rule_matches(f, rule) is True

    def test_combined_with_match_file_line_miss(self) -> None:
        """Rule with file + line range: matching file but line outside window → not suppressed."""
        rule = SuppressionRule(
            id="r", reason="ok", match_file=r"patches/", match_line_start=1, match_line_end=200
        )
        f = _finding(file="patches/lib.c", line=250)
        assert _rule_matches(f, rule) is False

    def test_exact_match_line_still_works(self) -> None:
        """match_line exact-match behaviour is unaffected by the new fields."""
        rule = SuppressionRule(id="r", reason="ok", match_line=150)
        assert _rule_matches(_finding(line=150), rule) is True
        assert _rule_matches(_finding(line=151), rule) is False


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
# Real-world regression tests against the shipped global config
# (ai-pr-review#601 + the self-action-mutable-tag semgrep false positive)
#
# These load the actual config/suppressions.json shipped with the action,
# rather than a synthetic SuppressionRule, because both bugs this section
# guards against were shipped rules whose match conditions were never
# actually reachable against the real finding text/file — a synthetic-only
# test suite did not catch either. See tag1consulting/timebot#9 and
# ai-pr-review#601.
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[2]


class TestGlobalConfigRegressions:
    def test_pr_number_issue_number_false_positive_suppressed(self) -> None:
        rules = load_rules(str(_REPO_ROOT))
        f = _finding(
            source="code-reviewer",
            file=".github/workflows/ai-pr-review.yml",
            finding=(
                "For issue_comment events (the primary way slash commands are "
                "posted on PRs), github.event.pull_request does not exist in "
                "the payload — only github.event.issue.pull_request (a link "
                "object) is present. This means pr-number will resolve to "
                "empty string for all issue_comment-triggered slash commands, "
                "breaking the primary documented use case."
            ),
        )
        kept, count = apply_suppressions([f], rules)
        assert kept == []
        assert count == 1

    def test_pr_number_finding_in_unrelated_context_not_suppressed(self) -> None:
        # Guard against over-broad matching: a genuine pr-number bug unrelated
        # to the issue_comment/issue-number fallback pattern must still surface.
        rules = load_rules(str(_REPO_ROOT))
        f = _finding(
            source="code-reviewer",
            file="scripts/notify.sh",
            finding=(
                "pr-number is read from an unsanitized environment variable "
                "and interpolated directly into a shell command without "
                "quoting, allowing command injection."
            ),
        )
        kept, count = apply_suppressions([f], rules)
        assert len(kept) == 1
        assert count == 0

    def test_self_action_mutable_tag_semgrep_finding_suppressed(self) -> None:
        # Matches the exact finding shape produced by
        # ai_pr_review/analyzers/native/semgrep.py's f"{check_id}: {message}"
        # construction, which discards the source line — the action name
        # itself is never present in the finding text for this source.
        rules = load_rules(str(_REPO_ROOT))
        f = _finding(
            source="semgrep",
            file=".github/workflows/ai-pr-review.yml",
            finding=(
                "yaml.github-actions.security.github-actions-mutable-action-tag."
                "github-actions-mutable-action-tag: GitHub Actions step uses a "
                "mutable tag or branch reference. Tags and branch names can be "
                "silently repointed by the action owner, enabling supply-chain "
                "attacks — as seen in the trivy-action and kics-github-action "
                "compromises. Pin the reference to a full 40-character commit "
                "SHA instead, e.g. `uses: actions/checkout@8ade135a41bc03ea155"
                "e62e844d188df1ea18608`."
            ),
            remediation=(
                "See https://docs.github.com/en/actions/security-guides/"
                "security-hardening-for-github-actions#using-third-party-actions"
            ),
        )
        kept, count = apply_suppressions([f], rules)
        assert kept == []
        assert count == 1

    def test_mutable_tag_finding_on_unrelated_file_not_suppressed(self) -> None:
        # The same generic semgrep rule firing on a genuinely third-party
        # action in a different workflow file must NOT be suppressed — the
        # fix is scoped to ai-pr-review's own workflow file, not a blanket
        # disable of the supply-chain rule.
        rules = load_rules(str(_REPO_ROOT))
        f = _finding(
            source="semgrep",
            file=".github/workflows/deploy.yml",
            finding=(
                "yaml.github-actions.security.github-actions-mutable-action-tag."
                "github-actions-mutable-action-tag: GitHub Actions step uses a "
                "mutable tag or branch reference. Tags and branch names can be "
                "silently repointed by the action owner, enabling supply-chain "
                "attacks — as seen in the trivy-action and kics-github-action "
                "compromises. Pin the reference to a full 40-character commit "
                "SHA instead, e.g. `uses: actions/checkout@8ade135a41bc03ea155"
                "e62e844d188df1ea18608`."
            ),
        )
        kept, count = apply_suppressions([f], rules)
        assert len(kept) == 1
        assert count == 0


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

    def test_docker_hub_no_tag_warns(self, capsys: pytest.CaptureFixture[str]) -> None:
        f = _finding(finding="nginx has CVE")  # no explicit :tag
        result = _verify_version(f, "docker-hub")
        assert result is False
        captured = capsys.readouterr()
        assert "WARNING" in captured.err
        assert "image:tag" in captured.err
        assert "nginx" in captured.err  # finding text excerpt included

    def test_exception_warning_includes_repr(self, capsys: pytest.CaptureFixture[str]) -> None:
        f = _finding(finding="owner/repo@v1.0.0")
        with patch(
            "ai_pr_review.findings.suppress._verify_github_release",
            side_effect=RuntimeError(""),  # empty str(exc) — repr should still show type
        ):
            result = _verify_version(f, "github-releases")
        assert result is False
        captured = capsys.readouterr()
        assert "RuntimeError" in captured.err

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
