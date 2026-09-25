"""Tests for ai_pr_review.feedback.store — E3.S8."""

from __future__ import annotations

import json
from dataclasses import dataclass

import httpx
import pytest

from ai_pr_review.feedback.models import FeedbackEntry
from ai_pr_review.feedback.store import (
    BitbucketSrcStore,
    GitBranchStore,
    UnsupportedVcsStore,
    make_store,
)
from ai_pr_review.vcs.http import RecordingClient, RetryPolicy, TapeRecorder

# ---------------------------------------------------------------------------
# make_store factory — guards against the make_store() critical bug
# ---------------------------------------------------------------------------

@dataclass
class _StubConfig:
    """Shape just enough of ReviewConfig to test the factory."""

    provider: str = "anthropic"
    vcs_provider: str = "github"
    feedback_branch: str = "ai-pr-review-bot"
    feedback_retention_count: int = 500
    feedback_retention_age_days: int = 365
    bitbucket_workspace: str = ""
    bitbucket_repo_slug: str = ""


def test_make_store_uses_vcs_provider_not_llm_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: make_store() must read config.vcs_provider, not config.provider.

    Pre-fix: vcs_provider=github with provider=anthropic returned UnsupportedVcsStore,
    silently disabling Capability C for every Anthropic/OpenAI/Google user.
    """
    monkeypatch.setenv("GH_TOKEN", "dummy-token")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")

    cfg = _StubConfig(provider="anthropic", vcs_provider="github")
    store = make_store(cfg)
    assert isinstance(store, GitBranchStore), (
        "GitHub vcs_provider with non-bedrock LLM provider should return GitBranchStore"
    )


def test_make_store_rejects_unsupported_vcs(monkeypatch: pytest.MonkeyPatch) -> None:
    """GitLab and unknown providers still get UnsupportedVcsStore.

    Bitbucket is no longer in this set as of issue #906 -- see the dedicated
    test_make_store_bitbucket_* tests below. Explicitly delenv's the
    Bitbucket credential vars so this test's "unknown"/"gitlab"/"" cases
    can't accidentally pass or fail based on the ambient shell environment
    (this repo's own dev environment happens to export BITBUCKET_EMAIL for
    live e2e testing -- see reference_test_credentials.md).
    """
    monkeypatch.setenv("GH_TOKEN", "dummy-token")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.delenv("BITBUCKET_EMAIL", raising=False)
    monkeypatch.delenv("BITBUCKET_API_TOKEN", raising=False)
    monkeypatch.delenv("BITBUCKET_WORKSPACE", raising=False)
    monkeypatch.delenv("BITBUCKET_REPO_SLUG", raising=False)

    for vcs in ("gitlab", "", "unknown"):
        cfg = _StubConfig(vcs_provider=vcs)
        store = make_store(cfg)
        assert isinstance(store, UnsupportedVcsStore), (
            f"vcs_provider={vcs!r} should yield UnsupportedVcsStore"
        )


# ---------------------------------------------------------------------------
# make_store factory — Bitbucket branch (issue #906)
# ---------------------------------------------------------------------------

def test_make_store_bitbucket_returns_bitbucket_src_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BITBUCKET_EMAIL", "bot@example.com")
    monkeypatch.setenv("BITBUCKET_API_TOKEN", "dummy-token")

    cfg = _StubConfig(
        vcs_provider="bitbucket",
        bitbucket_workspace="ws",
        bitbucket_repo_slug="repo",
    )
    store = make_store(cfg)
    assert isinstance(store, BitbucketSrcStore)
    assert store.workspace == "ws"
    assert store.repo_slug == "repo"
    assert store.branch == "ai-pr-review-bot"


def test_make_store_bitbucket_falls_back_to_env_for_workspace_repo_slug(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BITBUCKET_EMAIL", "bot@example.com")
    monkeypatch.setenv("BITBUCKET_API_TOKEN", "dummy-token")
    monkeypatch.setenv("BITBUCKET_WORKSPACE", "env-ws")
    monkeypatch.setenv("BITBUCKET_REPO_SLUG", "env-repo")

    cfg = _StubConfig(vcs_provider="bitbucket")  # bitbucket_workspace/repo_slug left blank
    store = make_store(cfg)
    assert isinstance(store, BitbucketSrcStore)
    assert store.workspace == "env-ws"
    assert store.repo_slug == "env-repo"


def test_make_store_bitbucket_requires_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BITBUCKET_EMAIL", raising=False)
    monkeypatch.delenv("BITBUCKET_API_TOKEN", raising=False)

    cfg = _StubConfig(
        vcs_provider="bitbucket", bitbucket_workspace="ws", bitbucket_repo_slug="repo",
    )
    assert isinstance(make_store(cfg), UnsupportedVcsStore)


def test_make_store_bitbucket_requires_workspace_and_repo_slug(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BITBUCKET_EMAIL", "bot@example.com")
    monkeypatch.setenv("BITBUCKET_API_TOKEN", "dummy-token")
    monkeypatch.delenv("BITBUCKET_WORKSPACE", raising=False)
    monkeypatch.delenv("BITBUCKET_REPO_SLUG", raising=False)

    cfg = _StubConfig(vcs_provider="bitbucket")  # no workspace/repo_slug anywhere
    assert isinstance(make_store(cfg), UnsupportedVcsStore)


def test_make_store_requires_github_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")

    cfg = _StubConfig(vcs_provider="github")
    assert isinstance(make_store(cfg), UnsupportedVcsStore)


def test_make_store_requires_github_repository(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GH_TOKEN", "dummy-token")
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)

    cfg = _StubConfig(vcs_provider="github")
    assert isinstance(make_store(cfg), UnsupportedVcsStore)


def test_make_store_strips_whitespace_from_token_and_repo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # #600: a trailing newline/whitespace in the secret/variable must not
    # reach the stored token/repo, or the resulting Authorization header.
    monkeypatch.setenv("GH_TOKEN", "\tdummy-token\n")
    monkeypatch.setenv("GITHUB_REPOSITORY", "  owner/repo  ")

    cfg = _StubConfig(vcs_provider="github")
    store = make_store(cfg)
    assert isinstance(store, GitBranchStore)
    assert store.token == "dummy-token"
    assert store.repo == "owner/repo"


def test_make_store_whitespace_only_token_treated_as_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GH_TOKEN", "   ")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")

    cfg = _StubConfig(vcs_provider="github")
    assert isinstance(make_store(cfg), UnsupportedVcsStore)


# ---------------------------------------------------------------------------
# UnsupportedVcsStore — returns False from append, [] from load_recent
# ---------------------------------------------------------------------------

def test_unsupported_store_append_returns_false() -> None:
    store = UnsupportedVcsStore()
    entry = FeedbackEntry(
        ts="2026-05-14T00:00:00Z", command="feedback", reason="r", source="s"
    )
    assert store.append(entry) is False


def test_unsupported_store_load_returns_empty() -> None:
    store = UnsupportedVcsStore()
    assert store.load_recent() == []


# ---------------------------------------------------------------------------
# GitBranchStore — token redaction
# ---------------------------------------------------------------------------

def test_gitbranchstore_repr_redacts_token() -> None:
    """Regression: dataclass repr must not leak the GitHub token."""
    store = GitBranchStore(repo="owner/repo", branch="bot", token="ghp_secret123")
    rep = repr(store)
    assert "ghp_secret123" not in rep, "token must not appear in repr output"


# ---------------------------------------------------------------------------
# JSONL ordering invariant — multi-append round-trip
# ---------------------------------------------------------------------------

def test_parse_jsonl_returns_newest_first_for_oldest_first_file() -> None:
    """File format is oldest-first; _parse_jsonl reverses to newest-first."""
    content = "\n".join([
        '{"ts":"2026-05-12T00:00:00Z","command":"feedback","reason":"old","source":"s"}',
        '{"ts":"2026-05-13T00:00:00Z","command":"feedback","reason":"mid","source":"s"}',
        '{"ts":"2026-05-14T00:00:00Z","command":"feedback","reason":"new","source":"s"}',
    ])
    entries = GitBranchStore._parse_jsonl(content)
    assert [e.reason for e in entries] == ["new", "mid", "old"]


def test_parse_jsonl_skips_malformed_lines(caplog: pytest.LogCaptureFixture) -> None:
    """Malformed lines must be skipped with a WARNING, not silently dropped."""
    import logging
    content = "\n".join([
        '{"ts":"2026-05-12T00:00:00Z","command":"feedback","reason":"good","source":"s"}',
        'not valid json',
        '{"ts":"2026-05-13T00:00:00Z","command":"feedback","reason":"also good","source":"s"}',
    ])
    with caplog.at_level(logging.WARNING, logger="ai_pr_review.feedback.store"):
        entries = GitBranchStore._parse_jsonl(content)
    assert len(entries) == 2
    assert any("malformed JSONL line" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# _fetch_file_meta — refuse to silently treat oversize files as empty
# ---------------------------------------------------------------------------

def test_fetch_file_meta_raises_on_oversize_file_omitting_content() -> None:
    """GitHub Contents API omits 'content' for files >1MB — must raise, not return ''."""
    import httpx

    class _FakeClient:
        def get(self, url: str, headers: dict) -> httpx.Response:
            # Simulate GitHub returning sha+size but no content (file too large)
            return httpx.Response(
                200,
                json={"sha": "abc123", "size": 2_000_000, "name": "learnings.jsonl"},
                request=httpx.Request("GET", url),
            )

    store = GitBranchStore(
        repo="o/r", branch="b", token="t", client=_FakeClient(),  # type: ignore[arg-type]
    )
    with pytest.raises(RuntimeError, match="may exceed 1 MB"):
        store._fetch_file_meta()


# ---------------------------------------------------------------------------
# _branch_exists — tri-state
# ---------------------------------------------------------------------------

def test_branch_exists_returns_true_on_200() -> None:
    import httpx

    class _FakeClient:
        def get(self, url: str, headers: dict) -> httpx.Response:
            return httpx.Response(200, request=httpx.Request("GET", url))

    store = GitBranchStore(
        repo="o/r", branch="b", token="t", client=_FakeClient(),  # type: ignore[arg-type]
    )
    assert store._branch_exists() is True


def test_branch_exists_returns_false_on_404() -> None:
    import httpx

    class _FakeClient:
        def get(self, url: str, headers: dict) -> httpx.Response:
            return httpx.Response(404, request=httpx.Request("GET", url))

    store = GitBranchStore(
        repo="o/r", branch="b", token="t", client=_FakeClient(),  # type: ignore[arg-type]
    )
    assert store._branch_exists() is False


def test_branch_exists_returns_none_on_transport_error() -> None:
    """Regression: transient transport errors must NOT be misclassified as 404.

    Returning False on a network blip would trigger an unnecessary bootstrap
    attempt and produce a misleading 'branch missing' log message."""
    import httpx

    class _FakeClient:
        def get(self, url: str, headers: dict) -> None:
            raise httpx.ConnectError("network down")

    store = GitBranchStore(
        repo="o/r", branch="b", token="t", client=_FakeClient(),  # type: ignore[arg-type]
    )
    assert store._branch_exists() is None


def test_branch_exists_returns_none_on_unexpected_status() -> None:
    """403 (auth failure) or 5xx must surface as None (unknown), not False."""
    import httpx

    class _FakeClient:
        def get(self, url: str, headers: dict) -> httpx.Response:
            return httpx.Response(403, request=httpx.Request("GET", url))

    store = GitBranchStore(
        repo="o/r", branch="b", token="t", client=_FakeClient(),  # type: ignore[arg-type]
    )
    assert store._branch_exists() is None


# ---------------------------------------------------------------------------
# _append_once — bootstrap trigger on 404 "Branch not found"
# ---------------------------------------------------------------------------

def test_append_once_treats_404_as_missing_branch_signal() -> None:
    """Regression: GitHub Contents API returns 404 (not 422) when the target
    branch does not exist on a PUT.  e2e testing 2026-05-15 found that the
    bootstrap path only triggered on 422, so 404+missing-branch was silently
    converted to a generic 'HTTP error' WARNING and the entry was dropped.
    """
    import httpx

    from ai_pr_review.feedback.models import FeedbackEntry
    from ai_pr_review.feedback.store import _MissingBranchError

    branches_seen: list[str] = []

    class _FakeClient:
        def get(self, url: str, headers: dict) -> httpx.Response:
            # Contents GET → 404 (file/branch absent)
            if "/contents/" in url:
                return httpx.Response(404, request=httpx.Request("GET", url))
            # Branch existence probe → 404 (confirms branch missing)
            if "/branches/" in url:
                branches_seen.append(url)
                return httpx.Response(404, request=httpx.Request("GET", url))
            return httpx.Response(404, request=httpx.Request("GET", url))

        def put(self, url: str, headers: dict, json: dict) -> httpx.Response:
            return httpx.Response(
                404,
                json={"message": "Branch test-bot not found"},
                request=httpx.Request("PUT", url),
            )

    store = GitBranchStore(
        repo="o/r", branch="test-bot", token="t", client=_FakeClient(),  # type: ignore[arg-type]
    )
    entry = FeedbackEntry(
        ts="2026-05-15T00:00:00Z", command="feedback", reason="r", source="s"
    )
    # _append_once must raise _MissingBranchError so append() can bootstrap
    with pytest.raises(_MissingBranchError):
        store._append_once(entry)
    # And the branch probe must have been called
    assert len(branches_seen) == 1, "branch existence probe should fire on 404"


# ---------------------------------------------------------------------------
# Warning format assertion (Story 4-5, path 13)
# ---------------------------------------------------------------------------


def test_append_http_error_logs_standard_warning(caplog: pytest.LogCaptureFixture) -> None:
    import logging
    from unittest.mock import patch

    import httpx

    store = GitBranchStore(repo="owner/repo", branch="ai-pr-review-bot", token="tok")
    entry = FeedbackEntry(ts="2026-05-18T00:00:00Z", command="dismiss", reason="test", source="code-reviewer", file="foo.py")

    with (
        patch.object(store.client, "get", side_effect=httpx.TransportError("conn refused")),
        caplog.at_level(logging.WARNING, logger="ai_pr_review.feedback.store"),
    ):
        result = store.append(entry)

    assert result is False
    assert any("[ai-pr-review] WARNING:" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Dedup guard (issue #769) — two independent writers producing the same
# verdict seconds apart (the double-write bug) must not both land in the
# store. Loud by design: a WARNING + ::warning:: on skip, not a silent drop,
# so a future recurrence of the underlying double-write bug still surfaces.
# ---------------------------------------------------------------------------


class _FakeAppendClient:
    """Minimal client stub: GET returns *existing_jsonl* (base64, oldest-first
    as the real file is), PUT records the call and succeeds."""

    def __init__(self, existing_jsonl: str) -> None:
        import base64 as _b64

        self._b64 = _b64.b64encode(existing_jsonl.encode()).decode()
        self.put_calls: list[dict] = []

    def get(self, url, headers):
        import httpx

        return httpx.Response(
            200, json={"sha": "abc123", "content": self._b64}, request=httpx.Request("GET", url)
        )

    def put(self, url, headers, json):
        import httpx

        self.put_calls.append(json)
        return httpx.Response(200, json={"content": {"sha": "def456"}}, request=httpx.Request("PUT", url))


def test_dedup_skips_append_within_window(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    import logging

    existing = FeedbackEntry(
        ts="2026-09-09T22:53:26Z",
        command="wont-fix",
        reason="original reason",
        source="sarif:Semgrep OSS",
        file="ai_pr_review/analyzers/native/phpstan.py",
        rule_id="sarif:Semgrep OSS",
        extras={"finding_id": 2},
    )
    client = _FakeAppendClient(existing.to_json() + "\n")
    store = GitBranchStore(repo="o/r", branch="ai-pr-review-bot", token="t", client=client)  # type: ignore[arg-type]

    duplicate = FeedbackEntry(
        ts="2026-09-09T22:54:04Z",  # 38s later -- inside the 10-minute window
        command="wont-fix",
        reason="different reason text from the other writer",  # reason excluded from the key
        source="sarif:Semgrep OSS",
        file="ai_pr_review/analyzers/native/phpstan.py",
        rule_id="sarif:Semgrep OSS",
        extras={"finding_id": 2},
    )
    with caplog.at_level(logging.WARNING, logger="ai_pr_review.feedback.store"):
        result = store.append(duplicate)

    assert result is True  # the verdict IS in the store (via the earlier write) -- honest to the caller
    assert client.put_calls == []  # no second JSONL line written
    assert any("skipped a duplicate append" in r.message for r in caplog.records)


def test_dedup_annotation_goes_to_stderr_not_stdout(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """Regression for a live stdout leak (confirmed on PR #911, comment
    https://github.com/tag1consulting/ai-pr-review/pull/911#issuecomment-5802793637):
    cli.py's dismiss/dismiss-inline commands print their actual PR reply text
    to stdout via click.echo(...) *after* this append() call returns, and the
    calling workflow step captures that stdout verbatim as the comment body.
    The `::warning::` annotation for a skipped duplicate append used a bare
    `print(...)` (defaulting to stdout) instead of `file=sys.stderr` like
    every other `::warning::` emission site in this codebase, so it landed
    ahead of the real reply and got posted to the PR glued onto its front."""
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    existing = FeedbackEntry(
        ts="2026-09-09T22:53:26Z",
        command="wont-fix",
        reason="original reason",
        source="sarif:Semgrep OSS",
        file="ai_pr_review/analyzers/native/phpstan.py",
        rule_id="sarif:Semgrep OSS",
        extras={"finding_id": 2},
    )
    client = _FakeAppendClient(existing.to_json() + "\n")
    store = GitBranchStore(repo="o/r", branch="ai-pr-review-bot", token="t", client=client)  # type: ignore[arg-type]

    duplicate = FeedbackEntry(
        ts="2026-09-09T22:54:04Z",
        command="wont-fix",
        reason="different reason text from the other writer",
        source="sarif:Semgrep OSS",
        file="ai_pr_review/analyzers/native/phpstan.py",
        rule_id="sarif:Semgrep OSS",
        extras={"finding_id": 2},
    )
    result = store.append(duplicate)
    captured = capsys.readouterr()

    assert result is True
    assert captured.out == "", (
        f"::warning:: annotation leaked to stdout, where a caller capturing "
        f"this process's output as the PR reply text would post it verbatim: {captured.out!r}"
    )
    assert "::warning::" in captured.err
    assert "skipped a duplicate append" in captured.err


def test_dedup_does_not_fire_outside_window(monkeypatch: pytest.MonkeyPatch) -> None:
    existing = FeedbackEntry(
        ts="2026-08-01T00:00:00Z",
        command="wont-fix",
        reason="original",
        source="sarif:Semgrep OSS",
        file="foo.py",
        rule_id="sarif:Semgrep OSS",
        extras={"finding_id": 2},
    )
    client = _FakeAppendClient(existing.to_json() + "\n")
    store = GitBranchStore(repo="o/r", branch="ai-pr-review-bot", token="t", client=client)  # type: ignore[arg-type]

    later = FeedbackEntry(
        ts="2026-09-09T22:54:04Z",  # weeks later -- a genuine re-dismissal, not this bug
        command="wont-fix",
        reason="recurred",
        source="sarif:Semgrep OSS",
        file="foo.py",
        rule_id="sarif:Semgrep OSS",
        extras={"finding_id": 2},
    )
    result = store.append(later)

    assert result is True
    assert len(client.put_calls) == 1  # a real new line was written


def test_dedup_does_not_fire_for_different_finding(monkeypatch: pytest.MonkeyPatch) -> None:
    existing = FeedbackEntry(
        ts="2026-09-09T22:53:26Z",
        command="wont-fix",
        reason="original",
        source="sarif:Semgrep OSS",
        file="foo.py",
        rule_id="sarif:Semgrep OSS",
        extras={"finding_id": 2},
    )
    client = _FakeAppendClient(existing.to_json() + "\n")
    store = GitBranchStore(repo="o/r", branch="ai-pr-review-bot", token="t", client=client)  # type: ignore[arg-type]

    different_finding = FeedbackEntry(
        ts="2026-09-09T22:53:28Z",
        command="wont-fix",
        reason="a different finding entirely",
        source="sarif:Semgrep OSS",
        file="foo.py",
        rule_id="sarif:Semgrep OSS",
        extras={"finding_id": 1},  # different finding_id -- must not be deduped against F2
    )
    result = store.append(different_finding)

    assert result is True
    assert len(client.put_calls) == 1


def test_dedup_boundary_exactly_at_window_still_dedupes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pins the dedup window's comparison as exclusive-greater-than: a gap of
    exactly _DEDUP_WINDOW (10 minutes) is still "within" the window and gets
    deduped. Catches a future off-by-one if the operator changes."""
    existing = FeedbackEntry(
        ts="2026-09-09T22:50:00Z",
        command="wont-fix",
        reason="original",
        source="sarif:Semgrep OSS",
        file="foo.py",
        rule_id="sarif:Semgrep OSS",
        extras={"finding_id": 2},
    )
    client = _FakeAppendClient(existing.to_json() + "\n")
    store = GitBranchStore(repo="o/r", branch="ai-pr-review-bot", token="t", client=client)  # type: ignore[arg-type]

    exactly_at_window = FeedbackEntry(
        ts="2026-09-09T23:00:00Z",  # exactly 10:00 later
        command="wont-fix",
        reason="different text",
        source="sarif:Semgrep OSS",
        file="foo.py",
        rule_id="sarif:Semgrep OSS",
        extras={"finding_id": 2},
    )
    result = store.append(exactly_at_window)

    assert result is True
    assert client.put_calls == []  # deduped -- gap is not > the window


def test_dedup_boundary_just_over_window_does_not_dedupe(monkeypatch: pytest.MonkeyPatch) -> None:
    existing = FeedbackEntry(
        ts="2026-09-09T22:50:00Z",
        command="wont-fix",
        reason="original",
        source="sarif:Semgrep OSS",
        file="foo.py",
        rule_id="sarif:Semgrep OSS",
        extras={"finding_id": 2},
    )
    client = _FakeAppendClient(existing.to_json() + "\n")
    store = GitBranchStore(repo="o/r", branch="ai-pr-review-bot", token="t", client=client)  # type: ignore[arg-type]

    just_over_window = FeedbackEntry(
        ts="2026-09-09T23:00:01Z",  # 10:01 later -- one second past the window
        command="wont-fix",
        reason="different text",
        source="sarif:Semgrep OSS",
        file="foo.py",
        rule_id="sarif:Semgrep OSS",
        extras={"finding_id": 2},
    )
    result = store.append(just_over_window)

    assert result is True
    assert len(client.put_calls) == 1  # not deduped -- gap exceeds the window


def test_dedup_skips_when_new_entry_has_malformed_timestamp(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_find_recent_duplicate` returns None immediately when the new entry's
    own `ts` can't be parsed -- matches `_parse_ts`'s "keep the entry" (never
    treat an unparseable timestamp as grounds to drop data) philosophy."""
    existing = FeedbackEntry(
        ts="2026-09-09T22:53:26Z",
        command="wont-fix",
        reason="original",
        source="sarif:Semgrep OSS",
        file="foo.py",
        rule_id="sarif:Semgrep OSS",
        extras={"finding_id": 2},
    )
    client = _FakeAppendClient(existing.to_json() + "\n")
    store = GitBranchStore(repo="o/r", branch="ai-pr-review-bot", token="t", client=client)  # type: ignore[arg-type]

    malformed_ts_entry = FeedbackEntry(
        ts="not-a-timestamp",
        command="wont-fix",
        reason="different text",
        source="sarif:Semgrep OSS",
        file="foo.py",
        rule_id="sarif:Semgrep OSS",
        extras={"finding_id": 2},
    )
    result = store.append(malformed_ts_entry)

    assert result is True
    assert len(client.put_calls) == 1  # not deduped -- can't compare against an unparseable new ts


def test_dedup_skips_when_existing_entry_has_malformed_timestamp(monkeypatch: pytest.MonkeyPatch) -> None:
    """An existing entry with an unparseable `ts` is never treated as recent
    (matches apply_retention's identical fail-safe), so a genuine duplicate
    against it is NOT deduped -- the safer failure direction (two writes
    survive) rather than silently dropping data based on an untrustworthy
    old record."""
    existing = FeedbackEntry(
        ts="also-not-a-timestamp",
        command="wont-fix",
        reason="original",
        source="sarif:Semgrep OSS",
        file="foo.py",
        rule_id="sarif:Semgrep OSS",
        extras={"finding_id": 2},
    )
    client = _FakeAppendClient(existing.to_json() + "\n")
    store = GitBranchStore(repo="o/r", branch="ai-pr-review-bot", token="t", client=client)  # type: ignore[arg-type]

    duplicate_key_entry = FeedbackEntry(
        ts="2026-09-09T22:53:28Z",
        command="wont-fix",
        reason="different text",
        source="sarif:Semgrep OSS",
        file="foo.py",
        rule_id="sarif:Semgrep OSS",
        extras={"finding_id": 2},
    )
    result = store.append(duplicate_key_entry)

    assert result is True
    assert len(client.put_calls) == 1  # existing entry's malformed ts means it's never "recent"


def test_dedup_never_fires_for_bare_feedback_command(monkeypatch: pytest.MonkeyPatch) -> None:
    """Issue #769 follow-up (found independently by two reviewers of this
    fix): the dedup guard is scoped to _DEDUP_ELIGIBLE_COMMANDS
    (false-positive/wont-fix only) precisely so two genuinely distinct
    `feedback` notes -- which have no file/source/finding_id identity beyond
    free-form `reason` text, deliberately excluded from the dedup key -- are
    never collapsed onto each other just because they share the same empty
    source/file within the window."""
    existing = FeedbackEntry(
        ts="2026-09-09T22:53:26Z",
        command="feedback",
        reason="please check the caching logic in this module",
        source="",
        file="",
        rule_id="",
    )
    client = _FakeAppendClient(existing.to_json() + "\n")
    store = GitBranchStore(repo="o/r", branch="ai-pr-review-bot", token="t", client=client)  # type: ignore[arg-type]

    another_feedback_note = FeedbackEntry(
        ts="2026-09-09T22:53:40Z",  # 14 seconds later -- well within the dedup window
        command="feedback",
        reason="unrelated: please update the docs for the new flag",
        source="",
        file="",
        rule_id="",
    )
    result = store.append(another_feedback_note)

    assert result is True
    assert len(client.put_calls) == 1  # both notes must land -- bare feedback is never deduped


# ---------------------------------------------------------------------------
# BitbucketSrcStore / _BitbucketSrcBackend (issue #906)
# ---------------------------------------------------------------------------

class _FakeBitbucketRepo:
    """In-memory Bitbucket repo simulator for `httpx.MockTransport`.

    Tracks one file's content per branch and a monotonic fake commit hash
    per branch -- enough to exercise `_BitbucketSrcBackend`'s read/write/
    branch_exists/bootstrap_branch sequence faithfully (refs/branches
    lookups, /src reads and writes, refs/branches branch creation) without
    needing a real git history.
    """

    def __init__(self, *, main_branch: str = "main") -> None:
        self.main_branch = main_branch
        self._next_hash = 1
        # branch name -> (head hash, {file_path: content})
        self.branches: dict[str, tuple[str, dict[str, str]]] = {
            main_branch: (self._new_hash(), {}),
        }
        self.calls: list[tuple[str, str]] = []  # (method, path) audit trail

    def _new_hash(self) -> str:
        h = f"{self._next_hash:040x}"
        self._next_hash += 1
        return h

    def handler(self, request: httpx.Request) -> httpx.Response:
        method = request.method
        path = request.url.path
        self.calls.append((method, path))

        if method == "GET" and path.endswith("/repositories/ws/repo"):
            return httpx.Response(200, json={"mainbranch": {"name": self.main_branch}})

        if method == "GET" and "/refs/branches/" in path:
            branch = path.rsplit("/refs/branches/", 1)[1]
            if branch not in self.branches:
                return httpx.Response(
                    404, json={"type": "error", "error": {"message": branch}},
                )
            head, _files = self.branches[branch]
            return httpx.Response(200, json={"target": {"hash": head}})

        if method == "GET" and "/src/" in path:
            # /repositories/ws/repo/src/{revision}/{path...}
            after = path.split("/src/", 1)[1]
            revision, _, file_path = after.partition("/")
            match = next(
                ((h, files) for h, files in self.branches.values() if h == revision),
                None,
            )
            if match is None:
                return httpx.Response(
                    404,
                    json={
                        "type": "error",
                        "error": {"message": "Commit not found", "data": {"shas": [revision]}},
                        "data": {"shas": [revision]},
                    },
                )
            _head, files = match
            if file_path not in files:
                return httpx.Response(
                    404,
                    json={"type": "error", "error": {"message": f"No such file or directory: {file_path}"}},
                )
            return httpx.Response(200, text=files[file_path])

        if method == "POST" and path.endswith("/refs/branches"):
            body = json.loads(request.content)
            name = body["name"]
            target_hash = body["target"]["hash"]
            if name in self.branches:
                return httpx.Response(
                    400, json={"type": "error", "error": {"message": f"Branch {name} already exists"}},
                )
            source_files: dict[str, str] = {}
            for h, files in self.branches.values():
                if h == target_hash:
                    source_files = dict(files)
                    break
            self.branches[name] = (target_hash, source_files)
            return httpx.Response(201, json={})

        if method == "POST" and path.endswith("/src"):
            return self._handle_src_post(request)

        raise AssertionError(f"unhandled request: {method} {path}")

    def _handle_src_post(self, request: httpx.Request) -> httpx.Response:
        # Parse multipart/form-data manually (httpx test requests carry the
        # raw encoded body; email.parser handles multipart robustly without
        # pulling in a new dependency).
        import email
        from email.message import Message

        content_type = request.headers.get("content-type", "")
        raw = b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + request.content
        msg: Message = email.message_from_bytes(raw)
        fields: dict[str, str] = {}
        if msg.is_multipart():
            for part in msg.get_payload():
                name = part.get_param("name", header="Content-Disposition")
                if name is not None:
                    payload = part.get_payload(decode=True)
                    fields[name] = payload.decode() if isinstance(payload, bytes) else str(payload)

        branch = fields["branch"]
        parents = fields.get("parents")
        file_fields = {
            k: v for k, v in fields.items() if k not in ("branch", "parents", "message")
        }

        if branch not in self.branches:
            if parents is None:
                return httpx.Response(404, json={"type": "error", "error": {"message": "branch not found"}})
            source_files: dict[str, str] = {}
            for h, files in self.branches.values():
                if h == parents:
                    source_files = dict(files)
                    break
        else:
            _head, source_files = self.branches[branch]
            source_files = dict(source_files)

        source_files.update(file_fields)
        new_hash = self._new_hash()
        self.branches[branch] = (new_hash, source_files)
        return httpx.Response(201, json={})


def _make_bitbucket_store(
    repo: _FakeBitbucketRepo, *, branch: str = "ai-pr-review-bot",
) -> BitbucketSrcStore:
    transport = httpx.MockTransport(repo.handler)
    http = httpx.Client(transport=transport, base_url="https://api.bitbucket.org/2.0")
    client = RecordingClient(
        http=http,
        recorder=TapeRecorder(record_dir=None),
        retry_policy=RetryPolicy(attempts=2, base_backoff=0, jitter=False, sleep=lambda _s: None),
    )
    return BitbucketSrcStore(workspace="ws", repo_slug="repo", branch=branch, client=client)


def test_bitbucket_store_bootstraps_fresh_branch_and_appends() -> None:
    repo = _FakeBitbucketRepo()
    store = _make_bitbucket_store(repo)
    entry = FeedbackEntry(
        ts="2026-09-24T00:00:00Z", command="wont-fix", reason="r", source="pylint", file="app.py",
        extras={"finding_id": 1, "source_comment_id": 100},
    )

    assert store.append(entry) is True
    assert "ai-pr-review-bot" in repo.branches
    _head, files = repo.branches["ai-pr-review-bot"]
    assert ".ai-pr-review/learnings.jsonl" in files
    assert '"finding_id": 1' in files[".ai-pr-review/learnings.jsonl"]


def test_bitbucket_store_appends_to_existing_branch() -> None:
    repo = _FakeBitbucketRepo()
    store = _make_bitbucket_store(repo)
    first = FeedbackEntry(
        ts="2026-09-24T00:00:00Z", command="wont-fix", reason="r1", source="pylint", file="a.py",
        extras={"finding_id": 1, "source_comment_id": 100},
    )
    second = FeedbackEntry(
        ts="2026-09-24T00:05:00Z", command="wont-fix", reason="r2", source="pylint", file="b.py",
        extras={"finding_id": 2, "source_comment_id": 200},
    )

    assert store.append(first) is True
    assert store.append(second) is True

    entries = store.load_recent()
    assert len(entries) == 2
    assert {e.extras.get("finding_id") for e in entries} == {1, 2}


def test_bitbucket_store_comment_id_dedup_skips_reprocessed_comment() -> None:
    """Issue #906 Q6: a comment reprocessed on a later pipeline run (because
    the summary-body ack save failed after this store's write already
    succeeded) must not append a second, identical entry -- with no time
    window, unlike the #769 dedup guard above."""
    repo = _FakeBitbucketRepo()
    store = _make_bitbucket_store(repo)
    entry = FeedbackEntry(
        ts="2026-09-24T00:00:00Z", command="wont-fix", reason="r", source="pylint", file="a.py",
        extras={"finding_id": 1, "source_comment_id": 100},
    )
    reprocessed = FeedbackEntry(
        ts="2026-09-26T12:00:00Z",  # two days later -- well outside the #769 window
        command="wont-fix", reason="r (reprocessed)", source="pylint", file="a.py",
        extras={"finding_id": 1, "source_comment_id": 100},  # same comment id
    )

    assert store.append(entry) is True
    calls_before = len(repo.calls)
    assert store.append(reprocessed) is True  # still "succeeds" -- already recorded
    calls_after = len(repo.calls)

    entries = store.load_recent()
    assert len(entries) == 1, "the reprocessed comment must not add a second line"
    # No POST /src should have happened on the second append.
    assert not any(m == "POST" and p.endswith("/src") for m, p in repo.calls[calls_before - 0:calls_after])


def test_bitbucket_store_dedup_key_includes_finding_id_not_just_comment_id() -> None:
    """Regression (found in review before merge, issue #906): a single
    Bitbucket comment can carry more than one /ai-pr-review command
    (parse_commands() supports multiple lines per comment, issue #733), so
    two entries built from the SAME comment but for DIFFERENT findings share
    one source_comment_id. Keying the dedup guard on source_comment_id alone
    made the second command's entry collide with the first's and get
    silently dropped, while append() still returned True."""
    repo = _FakeBitbucketRepo()
    store = _make_bitbucket_store(repo)
    first = FeedbackEntry(
        ts="2026-09-24T00:00:00Z", command="false-positive", reason="r1", source="pylint", file="a.py",
        extras={"finding_id": 1, "source_comment_id": 200},
    )
    second = FeedbackEntry(
        ts="2026-09-24T00:00:00Z", command="wont-fix", reason="r2", source="pylint", file="b.py",
        extras={"finding_id": 2, "source_comment_id": 200},  # same comment id, different finding
    )

    assert store.append(first) is True
    assert store.append(second) is True

    entries = store.load_recent()
    assert len(entries) == 2, "both commands from the same comment must persist"
    assert {e.extras.get("finding_id") for e in entries} == {1, 2}


def test_bitbucket_store_load_recent_on_empty_repo_returns_empty() -> None:
    repo = _FakeBitbucketRepo()  # feedback branch never created
    store = _make_bitbucket_store(repo)
    assert store.load_recent() == []


def test_bitbucket_backend_write_raises_conflict_when_verify_read_mismatches() -> None:
    """Best-effort conflict detection (issue #906 Q12): if the file's content
    immediately after a successful POST doesn't match what was just written
    (simulating an interleaved concurrent writer Bitbucket's own response
    didn't surface as an error), `write()` must raise `_ConflictError` so
    `_StoreCore.append()`'s existing retry loop redoes the read-modify-write.
    """
    from ai_pr_review.feedback.store import _BitbucketSrcBackend, _ConflictError

    repo = _FakeBitbucketRepo()
    repo.branches["ai-pr-review-bot"] = (repo._new_hash(), {})
    transport = httpx.MockTransport(repo.handler)
    http = httpx.Client(transport=transport, base_url="https://api.bitbucket.org/2.0")
    client = RecordingClient(
        http=http, recorder=TapeRecorder(record_dir=None),
        retry_policy=RetryPolicy(attempts=1, base_backoff=0, jitter=False, sleep=lambda _s: None),
    )
    backend = _BitbucketSrcBackend(workspace="ws", repo_slug="repo", branch="ai-pr-review-bot", client=client)
    _content, version = backend.read()

    # Simulate a concurrent writer landing a commit between our POST and our
    # verify re-read by mutating repo state inside a wrapped handler.
    real_handler = repo.handler
    written = {"done": False}

    def racing_handler(request: httpx.Request) -> httpx.Response:
        resp = real_handler(request)
        if request.method == "POST" and request.url.path.endswith("/src") and not written["done"]:
            written["done"] = True
            # A concurrent writer's commit lands right after ours.
            head, files = repo.branches["ai-pr-review-bot"]
            files = dict(files)
            files[".ai-pr-review/learnings.jsonl"] = "concurrent-writer-content\n"
            repo.branches["ai-pr-review-bot"] = (repo._new_hash(), files)
        return resp

    http2 = httpx.Client(transport=httpx.MockTransport(racing_handler), base_url="https://api.bitbucket.org/2.0")
    client2 = RecordingClient(
        http=http2, recorder=TapeRecorder(record_dir=None),
        retry_policy=RetryPolicy(attempts=1, base_backoff=0, jitter=False, sleep=lambda _s: None),
    )
    backend2 = _BitbucketSrcBackend(workspace="ws", repo_slug="repo", branch="ai-pr-review-bot", client=client2)

    with pytest.raises(_ConflictError):
        backend2.write("our-content\n", version)


def test_bitbucket_backend_branch_exists_tri_state() -> None:
    from ai_pr_review.feedback.store import _BitbucketSrcBackend

    repo = _FakeBitbucketRepo()
    transport = httpx.MockTransport(repo.handler)
    http = httpx.Client(transport=transport, base_url="https://api.bitbucket.org/2.0")
    client = RecordingClient(http=http, recorder=TapeRecorder(record_dir=None), retry_policy=RetryPolicy())
    backend = _BitbucketSrcBackend(workspace="ws", repo_slug="repo", branch="main", client=client)
    assert backend.branch_exists() is True

    backend_missing = _BitbucketSrcBackend(
        workspace="ws", repo_slug="repo", branch="does-not-exist", client=client,
    )
    assert backend_missing.branch_exists() is False


def test_bitbucket_backend_branch_exists_transport_error_is_none() -> None:
    from ai_pr_review.feedback.store import _BitbucketSrcBackend

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("network down")

    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport, base_url="https://api.bitbucket.org/2.0")
    client = RecordingClient(
        http=http, recorder=TapeRecorder(record_dir=None),
        retry_policy=RetryPolicy(attempts=1, base_backoff=0, jitter=False, sleep=lambda _s: None),
    )
    backend = _BitbucketSrcBackend(workspace="ws", repo_slug="repo", branch="main", client=client)
    assert backend.branch_exists() is None


# ---------------------------------------------------------------------------
# _BitbucketSrcBackend.bootstrap_branch -- failure branches
# ---------------------------------------------------------------------------

def _make_backend(handler):
    from ai_pr_review.feedback.store import _BitbucketSrcBackend

    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport, base_url="https://api.bitbucket.org/2.0")
    client = RecordingClient(
        http=http, recorder=TapeRecorder(record_dir=None),
        retry_policy=RetryPolicy(attempts=1, base_backoff=0, jitter=False, sleep=lambda _s: None),
    )
    return _BitbucketSrcBackend(workspace="ws", repo_slug="repo", branch="ai-pr-review-bot", client=client)


def test_bootstrap_branch_returns_true_on_already_exists_race() -> None:
    """Two concurrent runs both bootstrapping is an expected, harmless race
    -- the losing run's branch-create POST gets a 400 'already exists',
    which must be treated as success, not failure."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/refs/branches") and request.method == "POST":
            return httpx.Response(
                400, json={"type": "error", "error": {"message": "Branch ai-pr-review-bot already exists"}},
            )
        if "/repositories/ws/repo" in request.url.path and request.url.path.endswith("/repo"):
            return httpx.Response(200, json={"mainbranch": {"name": "main"}})
        if "/refs/branches/main" in request.url.path:
            return httpx.Response(200, json={"target": {"hash": "a" * 40}})
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    backend = _make_backend(handler)
    assert backend.bootstrap_branch() is True


def test_bootstrap_branch_returns_false_on_repo_get_non_200() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="forbidden")

    backend = _make_backend(handler)
    assert backend.bootstrap_branch() is False


def test_bootstrap_branch_returns_false_on_transport_error_fetching_repo() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("network down")

    backend = _make_backend(handler)
    assert backend.bootstrap_branch() is False


def test_bootstrap_branch_returns_false_when_default_branch_head_not_found() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/repo"):
            return httpx.Response(200, json={"mainbranch": {"name": "main"}})
        if "/refs/branches/main" in request.url.path:
            return httpx.Response(404, json={"type": "error", "error": {"message": "main"}})
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    backend = _make_backend(handler)
    assert backend.bootstrap_branch() is False


def test_bootstrap_branch_returns_false_on_unexpected_create_status() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/refs/branches") and request.method == "POST":
            return httpx.Response(500, text="internal error")
        if request.url.path.endswith("/repo"):
            return httpx.Response(200, json={"mainbranch": {"name": "main"}})
        if "/refs/branches/main" in request.url.path:
            return httpx.Response(200, json={"target": {"hash": "a" * 40}})
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    backend = _make_backend(handler)
    assert backend.bootstrap_branch() is False


# ---------------------------------------------------------------------------
# _BitbucketSrcBackend.read/write -- non-404 HTTP errors propagate
# ---------------------------------------------------------------------------

def test_backend_read_propagates_non_404_error_from_branch_lookup() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal error")

    backend = _make_backend(handler)
    with pytest.raises(httpx.HTTPStatusError):
        backend.read()


def test_backend_read_propagates_non_404_error_from_file_get() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "/refs/branches/" in request.url.path:
            return httpx.Response(200, json={"target": {"hash": "a" * 40}})
        return httpx.Response(403, text="forbidden")

    backend = _make_backend(handler)
    with pytest.raises(httpx.HTTPStatusError):
        backend.read()


def test_backend_read_raises_runtime_error_on_malformed_branch_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"target": {}})  # no "hash"

    backend = _make_backend(handler)
    with pytest.raises(RuntimeError, match="no target.hash"):
        backend.read()


def test_backend_write_propagates_non_404_error_from_post() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/src"):
            return httpx.Response(500, text="internal error")
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    backend = _make_backend(handler)
    with pytest.raises(httpx.HTTPStatusError):
        backend.write("content\n", "a" * 40)


def test_backend_write_maps_verify_read_failure_to_conflict_not_a_generic_error() -> None:
    """Regression (found in review before merge, issue #906): the POST has
    already committed by the time the best-effort verify-read runs. A
    transport/HTTP failure in that read must never surface as a generic
    error, or _StoreCore.append() would report an entry that actually
    persisted as failed."""
    from ai_pr_review.feedback.store import _ConflictError

    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        if request.method == "POST" and request.url.path.endswith("/src"):
            return httpx.Response(201)
        # Every GET after the POST (the verify read's branch-head lookup)
        # fails -- simulating a transient error on the check read itself.
        return httpx.Response(500, text="internal error")

    backend = _make_backend(handler)
    with pytest.raises(_ConflictError):
        backend.write("content\n", "a" * 40)


def test_backend_write_propagates_runtime_error_from_verify_read_instead_of_conflict() -> None:
    """Issue #906 review follow-up: a RuntimeError from the verify-read's
    branch-head lookup (malformed refs/branches response -- not a race, and
    not something a retry fixes) must propagate as-is, not be folded into
    _ConflictError. This is a different case from the transport/HTTP
    failure above: _StoreCore.append() has a dedicated RuntimeError handler
    that logs the real cause and returns False immediately, rather than
    retrying and eventually reporting a misleading "SHA conflict after N
    attempts"."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/src"):
            return httpx.Response(201)
        # The verify read's branch-head lookup succeeds with a malformed
        # body (no "hash") rather than failing transiently.
        return httpx.Response(200, json={"target": {}})

    backend = _make_backend(handler)
    with pytest.raises(RuntimeError, match="no target.hash"):
        backend.write("content\n", "a" * 40)


# ---------------------------------------------------------------------------
# Issue #941: per-run auth-failure cap on _BitbucketSrcBackend
# ---------------------------------------------------------------------------

def test_backend_write_403_sets_auth_failed_flag() -> None:
    """A 401/403 on the /src POST is the most likely permanent failure this
    backend can see (a token missing Repository:Write) -- it must be
    flagged so `BitbucketSrcStore.append()` can short-circuit further
    attempts within the same run (issue #941)."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/src"):
            return httpx.Response(403, text="forbidden")
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    backend = _make_backend(handler)
    assert backend.auth_failed is False
    with pytest.raises(httpx.HTTPStatusError):
        backend.write("content\n", "a" * 40)
    assert backend.auth_failed is True


def test_backend_write_500_does_not_set_auth_failed_flag() -> None:
    """A transient 5xx is not the same failure class as a bad token scope
    -- it must not trip the same-run short-circuit, since a retry on a
    transient failure can plausibly succeed."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/src"):
            return httpx.Response(500, text="internal error")
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    backend = _make_backend(handler)
    with pytest.raises(httpx.HTTPStatusError):
        backend.write("content\n", "a" * 40)
    assert backend.auth_failed is False


def test_backend_read_403_on_branch_head_does_not_set_auth_failed_flag() -> None:
    """Silent-failure-hunter review finding (issue #941): `_branch_head` is
    shared, undifferentiated code reached from both a write attempt and a
    pure read (`load_recent`). A 401/403 there must NOT trip the same-run
    short-circuit -- doing so would let a transient GET-only blip (a
    `load_recent()` call racing an outage, for instance) look exactly like
    a durable write-permission problem and silently suppress every later
    `append()` in the run, even though the write path itself was never
    actually denied."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="forbidden")

    backend = _make_backend(handler)
    with pytest.raises(httpx.HTTPStatusError):
        backend.read()
    assert backend.auth_failed is False


def test_backend_write_verify_read_403_does_not_set_auth_failed_flag() -> None:
    """The best-effort post-write verify-read also goes through
    `_branch_head` -- a 401/403 there is already folded into
    `_ConflictError` (retryable) by `write()`'s own docstring, and must
    not ALSO trip the separate, stronger `auth_failed` signal. The
    original POST already succeeded in this scenario, so the write itself
    was never denied."""
    from ai_pr_review.feedback.store import _ConflictError

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/src"):
            return httpx.Response(201)
        # Every GET after the POST (the verify read's branch-head lookup)
        # returns 403 -- simulating a transient auth-adjacent blip on the
        # check read itself, not a rejection of the write.
        return httpx.Response(403, text="forbidden")

    backend = _make_backend(handler)
    with pytest.raises(_ConflictError):
        backend.write("content\n", "a" * 40)
    assert backend.auth_failed is False


def test_backend_bootstrap_403_on_branch_create_sets_auth_failed_flag() -> None:
    """A read-only token can often still GET the repo and the default
    branch head, so the first 401/403 it hits is the branch-create POST --
    this must also set the flag, not just the /src write path."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/refs/branches") and request.method == "POST":
            return httpx.Response(403, text="forbidden")
        if request.url.path.endswith("/repo"):
            return httpx.Response(200, json={"mainbranch": {"name": "main"}})
        if "/refs/branches/main" in request.url.path:
            return httpx.Response(200, json={"target": {"hash": "a" * 40}})
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    backend = _make_backend(handler)
    assert backend.bootstrap_branch() is False
    assert backend.auth_failed is True


def test_backend_bootstrap_403_on_repo_get_sets_auth_failed_flag() -> None:
    """A token that can't even read the repo also sets the flag -- the
    'GET repo' step, not just branch-create."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="forbidden")

    backend = _make_backend(handler)
    assert backend.bootstrap_branch() is False
    assert backend.auth_failed is True


def test_bitbucket_store_append_short_circuits_after_auth_failure_same_run() -> None:
    """Issue #941: once one append() in a run has hit a 401/403, a second
    append() in the same run must not repeat the same doomed request
    sequence -- it should return False immediately with no further HTTP
    calls, since a token missing Repository:Write fails identically every
    time within the run. The next run gets a fresh backend instance (see
    bitbucket.py's `_get_feedback_store` caching), so this never blocks a
    fixed token from working again later."""
    from ai_pr_review.feedback.store import BitbucketSrcStore

    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        if request.url.path.endswith("/repo"):
            return httpx.Response(200, json={"mainbranch": {"name": "main"}})
        if "/refs/branches/main" in request.url.path:
            return httpx.Response(200, json={"target": {"hash": "a" * 40}})
        if request.url.path.endswith("/refs/branches") and request.method == "POST":
            return httpx.Response(403, text="forbidden")
        if request.url.path.endswith("/src") and request.method == "POST":
            return httpx.Response(403, text="forbidden")
        if "/refs/branches/ai-pr-review-bot" in request.url.path:
            return httpx.Response(404, json={"type": "error", "error": {"message": "x"}})
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport, base_url="https://api.bitbucket.org/2.0")
    client = RecordingClient(
        http=http, recorder=TapeRecorder(record_dir=None),
        retry_policy=RetryPolicy(attempts=1, base_backoff=0, jitter=False, sleep=lambda _s: None),
    )
    store = BitbucketSrcStore(workspace="ws", repo_slug="repo", branch="ai-pr-review-bot", client=client)

    entry = FeedbackEntry(
        ts="2026-09-25T00:00:00Z", command="false-positive", reason="x", source="ruff",
        file="a.py", extras={"finding_id": 1, "source_comment_id": 200},
    )
    assert store.append(entry) is False
    calls_after_first = call_count["n"]
    assert calls_after_first > 0

    entry2 = FeedbackEntry(
        ts="2026-09-25T00:00:01Z", command="false-positive", reason="y", source="ruff",
        file="b.py", extras={"finding_id": 2, "source_comment_id": 201},
    )
    assert store.append(entry2) is False
    assert call_count["n"] == calls_after_first, "no further HTTP calls after the first auth failure"
