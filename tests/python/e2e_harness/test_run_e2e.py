"""Unit tests for tests/e2e/run_e2e.py's pure/file-local helper functions
and the `cleanup` CLI subcommand.

No network calls; no real credentials. `cleanup` tests stub out
`build_adapter` so no real adapter/HTTP code runs.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from tests.e2e.models import AdapterError, PullRequest, RunCommit
from tests.e2e.run_e2e import (
    _clone_auth_env,
    _github_reviewer_token,
    _known_secret_values,
    _record_opened,
    _redact_known_values,
    _redact_known_values_in_file,
    _resolve_opened,
    cleanup,
)

# --- _github_reviewer_token ---------------------------------------------------

def test_github_reviewer_token_prefers_reviewer_token(monkeypatch):
    monkeypatch.setenv("E2E_GITHUB_REVIEWER_TOKEN", "reviewer-tok")
    monkeypatch.setenv("E2E_GITHUB_TOKEN", "seeder-tok")
    assert _github_reviewer_token() == "reviewer-tok"


def test_github_reviewer_token_falls_back_when_absent(monkeypatch):
    monkeypatch.delenv("E2E_GITHUB_REVIEWER_TOKEN", raising=False)
    monkeypatch.setenv("E2E_GITHUB_TOKEN", "seeder-tok")
    assert _github_reviewer_token() == "seeder-tok"


def test_github_reviewer_token_falls_back_when_empty_string():
    # The real bug this guards against: GitHub Actions sets an unconfigured
    # optional secret to an EMPTY STRING, not an absent env var --
    # .get(A, .get(B, "")) doesn't fall back for a present-but-empty A.
    import os
    old_reviewer = os.environ.get("E2E_GITHUB_REVIEWER_TOKEN")
    old_seeder = os.environ.get("E2E_GITHUB_TOKEN")
    try:
        os.environ["E2E_GITHUB_REVIEWER_TOKEN"] = ""
        os.environ["E2E_GITHUB_TOKEN"] = "seeder-tok"
        assert _github_reviewer_token() == "seeder-tok"
    finally:
        for key, val in (("E2E_GITHUB_REVIEWER_TOKEN", old_reviewer), ("E2E_GITHUB_TOKEN", old_seeder)):
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val


# --- _clone_auth_env -----------------------------------------------------------

def test_clone_auth_env_github(monkeypatch):
    monkeypatch.setenv("E2E_GITHUB_TOKEN", "tok-gh")
    monkeypatch.delenv("E2E_GITHUB_REVIEWER_TOKEN", raising=False)
    url, env = _clone_auth_env("github")
    assert url == "https://github.com/tag1consulting/ai-pr-review-test.git"
    assert "tok-gh" not in url  # no credentials embedded in the URL
    assert env["GIT_CONFIG_KEY_0"] == "http.extraHeader"
    assert env["GIT_CONFIG_VALUE_0"].startswith("Authorization: Basic ")
    import base64
    decoded = base64.b64decode(env["GIT_CONFIG_VALUE_0"].removeprefix("Authorization: Basic ")).decode()
    assert decoded == "x-access-token:tok-gh"


def test_clone_auth_env_gitlab(monkeypatch):
    monkeypatch.setenv("E2E_GITLAB_TOKEN", "tok-gl")
    url, env = _clone_auth_env("gitlab")
    assert url == "https://gitlab.com/tag1consulting/ai-pr-review-test.git"
    import base64
    decoded = base64.b64decode(env["GIT_CONFIG_VALUE_0"].removeprefix("Authorization: Basic ")).decode()
    assert decoded == "oauth2:tok-gl"


def test_clone_auth_env_bitbucket_uses_fixed_username_not_email(monkeypatch):
    # Confirmed live: git-over-HTTPS to Bitbucket with an API token requires
    # the fixed username "x-bitbucket-api-token-auth", NOT the account
    # email (email:token is a different auth scheme, valid only for
    # Bitbucket's REST API Basic auth).
    monkeypatch.setenv("E2E_BITBUCKET_TOKEN", "tok-bb")
    monkeypatch.setenv("E2E_BITBUCKET_EMAIL", "someone@example.com")
    url, env = _clone_auth_env("bitbucket")
    assert url == "https://bitbucket.org/gchaix-tag1/ai-pr-review-test.git"
    import base64
    decoded = base64.b64decode(env["GIT_CONFIG_VALUE_0"].removeprefix("Authorization: Basic ")).decode()
    assert decoded == "x-bitbucket-api-token-auth:tok-bb"
    assert "someone@example.com" not in decoded


# --- _known_secret_values / _redact_known_values --------------------------------

def test_known_secret_values_excludes_short_values():
    env_vars = {
        "ANTHROPIC_API_KEY": "sk-ant-1234567890",
        "GH_TOKEN": "short",  # len < 8, excluded
        "GITLAB_TOKEN": "glpat-12345678",
        "BITBUCKET_API_TOKEN": "",  # empty, excluded
        "BASE_REF": "main",  # not a secret key at all
    }
    values = _known_secret_values(env_vars)
    assert "sk-ant-1234567890" in values
    assert "glpat-12345678" in values
    assert "short" not in values
    assert "" not in values
    assert "main" not in values


def test_redact_known_values_replaces_every_occurrence():
    text = "token=sk-ant-1234567890 and again sk-ant-1234567890 at the end"
    redacted = _redact_known_values(text, ["sk-ant-1234567890"])
    assert "sk-ant-1234567890" not in redacted
    assert redacted.count("<secret-redacted>") == 2


def test_redact_known_values_in_file_rewrites_matching_file(tmp_path: Path):
    target = tmp_path / "telemetry.json"
    target.write_text('{"key": "sk-ant-1234567890"}', encoding="utf-8")
    _redact_known_values_in_file(target, ["sk-ant-1234567890"])
    assert "sk-ant-1234567890" not in target.read_text(encoding="utf-8")
    assert "<secret-redacted>" in target.read_text(encoding="utf-8")


def test_redact_known_values_in_file_missing_file_is_a_noop(tmp_path: Path):
    missing = tmp_path / "does-not-exist.json"
    _redact_known_values_in_file(missing, ["sk-ant-1234567890"])  # must not raise
    assert not missing.exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permission bits")
def test_redact_known_values_in_file_works_when_original_file_is_read_only(tmp_path: Path):
    # Regression test: an earlier version rewrote via path.write_text()
    # directly on the original file, which needs write permission on the
    # FILE itself -- exactly what's missing when telemetry.json is owned
    # by the container's fixed uid (1001) and the harness runs as a
    # different host uid. A temp-file-plus-os.replace() rewrite only needs
    # permission to create/rename within the containing directory, which
    # this test simulates by making the original file itself read-only
    # while its directory stays writable (tmp_path's default mode).
    target = tmp_path / "telemetry.json"
    target.write_text('{"key": "sk-ant-1234567890"}', encoding="utf-8")
    target.chmod(0o444)
    try:
        _redact_known_values_in_file(target, ["sk-ant-1234567890"])  # must not raise
    finally:
        target.chmod(0o644)  # restore so tmp_path's own cleanup can remove it
    assert "sk-ant-1234567890" not in target.read_text(encoding="utf-8")
    assert "<secret-redacted>" in target.read_text(encoding="utf-8")


# --- _record_opened / _resolve_opened -------------------------------------------

def _pr(platform: str, number: int) -> PullRequest:
    return PullRequest(
        platform=platform, number=number, url=f"https://example.invalid/{platform}/{number}",
        branch=f"e2e/{platform}-{number}",
        run_commit=RunCommit(platform=platform, run_id="abc123", branch=f"e2e/{platform}-{number}", commit_sha="deadbeef"),
    )


def test_record_opened_appends_and_round_trips(tmp_path: Path):
    _record_opened(tmp_path, "github", _pr("github", 1))
    _record_opened(tmp_path, "gitlab", _pr("gitlab", 2))
    entries = json.loads((tmp_path / "opened.json").read_text(encoding="utf-8"))
    assert [e["platform"] for e in entries] == ["github", "gitlab"]
    assert entries[0]["number"] == 1
    assert entries[1]["branch"] == "e2e/gitlab-2"


def test_record_opened_discards_corrupt_file_instead_of_crashing(tmp_path: Path):
    (tmp_path / "opened.json").write_text("{not valid json", encoding="utf-8")
    _record_opened(tmp_path, "github", _pr("github", 1))  # must not raise
    entries = json.loads((tmp_path / "opened.json").read_text(encoding="utf-8"))
    assert len(entries) == 1
    assert entries[0]["platform"] == "github"


def test_resolve_opened_marks_matching_entry(tmp_path: Path):
    _record_opened(tmp_path, "github", _pr("github", 1))
    _record_opened(tmp_path, "gitlab", _pr("gitlab", 2))
    _resolve_opened(tmp_path, "github", "closed")
    entries = json.loads((tmp_path / "opened.json").read_text(encoding="utf-8"))
    by_platform = {e["platform"]: e for e in entries}
    assert by_platform["github"]["resolution"] == "closed"
    assert "resolution" not in by_platform["gitlab"]


def test_resolve_opened_never_overwrites_existing_resolution(tmp_path: Path):
    _record_opened(tmp_path, "github", _pr("github", 1))
    _resolve_opened(tmp_path, "github", "left_open")
    _resolve_opened(tmp_path, "github", "closed")  # must not clobber the first decision
    entries = json.loads((tmp_path / "opened.json").read_text(encoding="utf-8"))
    assert entries[0]["resolution"] == "left_open"


def test_resolve_opened_missing_file_is_a_noop(tmp_path: Path):
    _resolve_opened(tmp_path, "github", "closed")  # must not raise
    assert not (tmp_path / "opened.json").exists()


# --- cleanup subcommand ----------------------------------------------------------

class _StubAdapter:
    def __init__(self, *, fail_close: bool = False, fail_delete: bool = False) -> None:
        self.fail_close = fail_close
        self.fail_delete = fail_delete
        self.closed: list[int] = []
        self.deleted_branches: list[str] = []

    def close(self, pr: PullRequest) -> None:
        if self.fail_close:
            raise AdapterError("close failed")
        self.closed.append(pr.number)

    def delete_branch(self, branch: str) -> None:
        if self.fail_delete:
            raise AdapterError("delete failed")
        self.deleted_branches.append(branch)


def _write_opened(tmp_path: Path, entries: list[dict[str, object]]) -> Path:
    path = tmp_path / "opened.json"
    path.write_text(json.dumps(entries), encoding="utf-8")
    return path


def test_cleanup_closes_and_deletes_unresolved_entry(tmp_path: Path, monkeypatch):
    stub = _StubAdapter()
    monkeypatch.setattr("tests.e2e.run_e2e.build_adapter", lambda name, env: stub)
    opened_file = _write_opened(tmp_path, [
        {"platform": "github", "number": 42, "branch": "e2e/run1", "url": "https://x"},
    ])
    result = CliRunner().invoke(cleanup, ["--from-file", str(opened_file)])
    assert result.exit_code == 0
    assert stub.closed == [42]
    assert stub.deleted_branches == ["e2e/run1"]


def test_cleanup_skips_entry_already_resolved(tmp_path: Path, monkeypatch):
    stub = _StubAdapter()
    monkeypatch.setattr("tests.e2e.run_e2e.build_adapter", lambda name, env: stub)
    opened_file = _write_opened(tmp_path, [
        {"platform": "github", "number": 42, "branch": "e2e/run1", "url": "https://x", "resolution": "left_open"},
    ])
    result = CliRunner().invoke(cleanup, ["--from-file", str(opened_file)])
    assert result.exit_code == 0
    # Must not touch an entry whose fate is already decided -- this is what
    # keeps e2e.yml's cancel-on-signal fallback step from closing a PR that
    # was deliberately left open for debugging (see _resolve_opened).
    assert stub.closed == []
    assert stub.deleted_branches == []


def test_cleanup_reports_failure_exit_code_when_close_fails(tmp_path: Path, monkeypatch):
    stub = _StubAdapter(fail_close=True)
    monkeypatch.setattr("tests.e2e.run_e2e.build_adapter", lambda name, env: stub)
    opened_file = _write_opened(tmp_path, [
        {"platform": "github", "number": 42, "branch": "e2e/run1", "url": "https://x"},
    ])
    result = CliRunner().invoke(cleanup, ["--from-file", str(opened_file)])
    assert result.exit_code != 0
    assert stub.deleted_branches == []  # never attempted after close failed


def test_cleanup_reports_failure_when_close_succeeds_but_delete_fails(tmp_path: Path, monkeypatch):
    stub = _StubAdapter(fail_delete=True)
    monkeypatch.setattr("tests.e2e.run_e2e.build_adapter", lambda name, env: stub)
    opened_file = _write_opened(tmp_path, [
        {"platform": "github", "number": 42, "branch": "e2e/run1", "url": "https://x"},
    ])
    result = CliRunner().invoke(cleanup, ["--from-file", str(opened_file)])
    assert result.exit_code != 0
    assert stub.closed == [42]  # close DID succeed before delete_branch failed
