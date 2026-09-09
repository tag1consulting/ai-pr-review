"""Tests for ai_pr_review.feedback.store — E3.S8."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from ai_pr_review.feedback.models import FeedbackEntry
from ai_pr_review.feedback.store import (
    GitBranchStore,
    UnsupportedVcsStore,
    make_store,
)

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


def test_make_store_rejects_non_github_vcs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GH_TOKEN", "dummy-token")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")

    for vcs in ("gitlab", "bitbucket", "", "unknown"):
        cfg = _StubConfig(vcs_provider=vcs)
        store = make_store(cfg)
        assert isinstance(store, UnsupportedVcsStore), (
            f"vcs_provider={vcs!r} should yield UnsupportedVcsStore"
        )


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
