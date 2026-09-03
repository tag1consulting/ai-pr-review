"""GitLabProvider cross-run finding dedup (#710).

Covers `post_findings`'s fuzzy-match classification against prior owned
discussions, the keep-alive set it populates, and the fix for Finding 0
(`resolve_stale` immediately resolving a discussion `post_findings` either
just created or matched, since GitLab's `resolve_stale` previously had no
equivalent of GitHub's `_kept_alive_thread_ids`). `parse_gitlab_prior_thread`
itself (Tier 1/2 outdated-position rules, marker parsing, legacy fallback) is
unit-tested directly in `test_canonical.py`; this file exercises the
integration through `GitLabProvider`.
"""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx

from ai_pr_review.findings.models import Finding
from ai_pr_review.vcs._finding_ids import fingerprint
from ai_pr_review.vcs.gitlab import GitLabConfig, GitLabProvider
from ai_pr_review.vcs.http import RecordingClient, RetryPolicy, TapeRecorder
from ai_pr_review.vcs.marker import INLINE_MARKER, build_inline_meta_marker
from ai_pr_review.vcs.protocol import DiffContext
from tests.python.vcs.test_gitlab_stale import _disc, _position

_HEAD = "head1234head1234head1234head1234head1234"
_BASE = "basesha1234"

# Added (diff-eligible) lines: app.py:4 through app.py:10.
_DIFF = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1,3 +1,10 @@
 ctx_1
 ctx_2
 ctx_3
+added_4
+added_5
+added_6
+added_7
+added_8
+added_9
+added_10
"""


def _make_provider(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    cross_run_dedup: bool = True,
) -> GitLabProvider:
    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport, base_url="https://gitlab.com/api/v4")
    client = RecordingClient(
        http=http,
        recorder=TapeRecorder(record_dir=None),
        retry_policy=RetryPolicy(
            attempts=2, base_backoff=0, jitter=False, sleep=lambda _s: None
        ),
    )
    return GitLabProvider(
        config=GitLabConfig(
            project_id_or_path="42",
            mr_iid=1,
            token="glpat-test",
            diff_base_sha=_BASE,
            bot_username="ai-bot",
            cross_run_dedup=cross_run_dedup,
        ),
        client=client,
    )


def _finding(**overrides: object) -> Finding:
    defaults: dict[str, object] = dict(
        severity="High",
        confidence=90,
        finding="unsafe call",
        source="blind",
        file="app.py",
        line=4,
        category="injection",
    )
    defaults.update(overrides)
    return Finding(**defaults)  # type: ignore[arg-type]


def _prior_disc(
    did: str,
    f: Finding,
    *,
    line: int | None = None,
    note_id: int = 1,
    head_sha: str = _HEAD,
    base_sha: str = _BASE,
    resolved: bool = False,
) -> dict:
    """A prior discussion this bot posted for finding `f` (marker + position)."""
    body = "finding\n" + INLINE_MARKER + "\n" + build_inline_meta_marker(
        fingerprint=fingerprint(f), category=f.category, severity=f.severity,
    )
    return _disc(
        did,
        body=body,
        author="ai-bot",
        resolved=resolved,
        note_id=note_id,
        note_type="DiffNote",
        position=_position(
            new_path=f.file,
            new_line=line if line is not None else f.line,
            head_sha=head_sha,
            base_sha=base_sha,
        ),
    )


def test_unchanged_finding_not_reposted_and_kept_alive() -> None:
    f = _finding(line=4)
    prior = _prior_disc("D1", f, line=4)
    posts: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and "/discussions" in str(req.url):
            return httpx.Response(200, json=[prior])
        if req.method == "POST" and "/discussions" in str(req.url):
            posts.append(json.loads(req.content))
            return httpx.Response(201, json={"id": "d_new"})
        return httpx.Response(404)

    prov = _make_provider(handler)
    result = prov.post_findings(
        [f], DiffContext(diff_text=_DIFF, head_sha=_HEAD), event="REQUEST_CHANGES"
    )
    assert posts == []
    assert result.inline_posted == 0
    assert result.body_findings == 0
    assert result.ok
    assert "D1" in prov._kept_alive_discussion_ids


def test_kept_alive_matched_discussion_survives_same_run_resolve_stale() -> None:
    """Finding 0, matched-thread half: post_findings fuzzy-matches D1 and
    keeps it alive; the very next resolve_stale() call on the same instance
    must not resolve it."""
    f = _finding(line=4)
    prior = _prior_disc("D1", f, line=4)
    resolve_calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and "/discussions" in str(req.url):
            return httpx.Response(200, json=[prior])
        if req.method == "PUT" and "/discussions/" in str(req.url):
            resolve_calls.append(str(req.url))
            return httpx.Response(200, json={})
        return httpx.Response(404)

    prov = _make_provider(handler)
    prov.post_findings(
        [f], DiffContext(diff_text=_DIFF, head_sha=_HEAD), event="REQUEST_CHANGES"
    )
    result = prov.resolve_stale()
    assert resolve_calls == []
    assert result.threads_resolved == 0


def test_newly_created_discussion_survives_same_run_resolve_stale() -> None:
    """Finding 0, freshly-posted half: a discussion this same post_findings
    call just created must also not be resolved by the immediately-following
    resolve_stale() call."""
    f = _finding(line=5)
    created: dict[str, dict] = {}
    resolve_calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and "/discussions" in str(req.url):
            return httpx.Response(200, json=list(created.values()))
        if req.method == "POST" and "/discussions" in str(req.url):
            payload = json.loads(req.content)
            created["d_new"] = {
                "id": "d_new",
                "notes": [
                    {
                        "id": 99,
                        "type": "DiffNote",
                        "body": payload["body"],
                        "author": {"username": "ai-bot"},
                        "resolvable": True,
                        "resolved": False,
                        "position": payload["position"],
                    }
                ],
            }
            return httpx.Response(201, json={"id": "d_new"})
        if req.method == "PUT" and "/discussions/" in str(req.url):
            resolve_calls.append(str(req.url))
            return httpx.Response(200, json={})
        return httpx.Response(404)

    prov = _make_provider(handler)
    post_result = prov.post_findings(
        [f], DiffContext(diff_text=_DIFF, head_sha=_HEAD), event="REQUEST_CHANGES"
    )
    assert post_result.inline_posted == 1
    result = prov.resolve_stale()
    assert resolve_calls == []
    assert result.threads_resolved == 0


def test_fuzzy_match_within_proximity_not_reposted() -> None:
    """delta=2 <= PROXIMITY_LINES(3): fuzzy match, no repost."""
    prior_f = _finding(line=4)
    prior = _prior_disc("D1", prior_f, line=4)
    new_f = _finding(line=6)
    posts: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and "/discussions" in str(req.url):
            return httpx.Response(200, json=[prior])
        if req.method == "POST" and "/discussions" in str(req.url):
            posts.append(json.loads(req.content))
            return httpx.Response(201, json={"id": "d_new"})
        return httpx.Response(404)

    prov = _make_provider(handler)
    result = prov.post_findings(
        [new_f], DiffContext(diff_text=_DIFF, head_sha=_HEAD), event="REQUEST_CHANGES"
    )
    assert posts == []
    assert result.inline_posted == 0
    assert "D1" in prov._kept_alive_discussion_ids


def test_fuzzy_match_outside_proximity_reposts() -> None:
    """delta=4 > PROXIMITY_LINES(3): no match, finding is genuinely new."""
    prior_f = _finding(line=4)
    prior = _prior_disc("D1", prior_f, line=4)
    new_f = _finding(line=8)
    posts: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and "/discussions" in str(req.url):
            return httpx.Response(200, json=[prior])
        if req.method == "POST" and "/discussions" in str(req.url):
            posts.append(json.loads(req.content))
            return httpx.Response(201, json={"id": "d_new"})
        return httpx.Response(404)

    prov = _make_provider(handler)
    result = prov.post_findings(
        [new_f], DiffContext(diff_text=_DIFF, head_sha=_HEAD), event="REQUEST_CHANGES"
    )
    assert len(posts) == 1
    assert result.inline_posted == 1
    assert "D1" not in prov._kept_alive_discussion_ids


def test_incompatible_category_not_matched_reposts() -> None:
    """Same location, incompatible real categories ('injection' vs 'secret'):
    never a match, regardless of proximity."""
    prior_f = _finding(line=4, category="injection")
    prior = _prior_disc("D1", prior_f, line=4)
    new_f = _finding(line=4, category="secret")
    posts: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and "/discussions" in str(req.url):
            return httpx.Response(200, json=[prior])
        if req.method == "POST" and "/discussions" in str(req.url):
            posts.append(json.loads(req.content))
            return httpx.Response(201, json={"id": "d_new"})
        return httpx.Response(404)

    prov = _make_provider(handler)
    result = prov.post_findings(
        [new_f], DiffContext(diff_text=_DIFF, head_sha=_HEAD), event="REQUEST_CHANGES"
    )
    assert len(posts) == 1
    assert result.inline_posted == 1


def test_resolved_prior_thread_never_matches_reposts() -> None:
    """A resolved discussion is dropped from open_threads -- never a fuzzy
    match target, even at the exact same location."""
    prior_f = _finding(line=4)
    prior = _prior_disc("D1", prior_f, line=4, resolved=True)
    new_f = _finding(line=4)
    posts: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and "/discussions" in str(req.url):
            return httpx.Response(200, json=[prior])
        if req.method == "POST" and "/discussions" in str(req.url):
            posts.append(json.loads(req.content))
            return httpx.Response(201, json={"id": "d_new"})
        return httpx.Response(404)

    prov = _make_provider(handler)
    result = prov.post_findings(
        [new_f], DiffContext(diff_text=_DIFF, head_sha=_HEAD), event="REQUEST_CHANGES"
    )
    assert len(posts) == 1
    assert result.inline_posted == 1


def test_two_new_findings_claiming_one_thread_only_higher_severity_kept_alive() -> None:
    """dedupe_thread_claims: two findings within proximity of the same open
    thread can't both claim it. The higher-severity one wins the match
    (escalate) and keeps the thread alive; the loser is demoted to "new" and
    gets its own discussion."""
    prior_f = _finding(line=5, severity="Low", category="injection")
    prior = _prior_disc("D1", prior_f, line=5)
    # Both within PROXIMITY_LINES(3) of line 5, same compatible category.
    low_claim = _finding(line=4, severity="Low", category="injection")
    high_claim = _finding(line=6, severity="Critical", category="injection")
    posts: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and "/discussions" in str(req.url):
            return httpx.Response(200, json=[prior])
        if req.method == "POST" and "/discussions" in str(req.url):
            posts.append(json.loads(req.content))
            return httpx.Response(201, json={"id": "d_new"})
        return httpx.Response(404)

    prov = _make_provider(handler)
    result = prov.post_findings(
        [low_claim, high_claim],
        DiffContext(diff_text=_DIFF, head_sha=_HEAD),
        event="REQUEST_CHANGES",
    )
    # The demoted claim (low_claim) gets its own fresh discussion; the
    # winning claim (high_claim) does not repost -- D1 stays kept alive.
    assert result.inline_posted == 1
    assert len(posts) == 1
    assert "D1" in prov._kept_alive_discussion_ids


def test_kill_switch_disables_fetch_but_still_fixes_finding_0() -> None:
    """AI_GITLAB_CROSS_RUN_DEDUP=false (cross_run_dedup=False): the fuzzy-match
    fetch is skipped entirely (no GET /discussions call from post_findings),
    so every finding still classifies "new" and posts -- but a discussion
    this call just created is still kept alive for the following
    resolve_stale() (that half of the Finding 0 fix is unconditional)."""
    get_discussions_calls: list[str] = []
    resolve_calls: list[str] = []
    created: dict[str, dict] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and "/discussions" in str(req.url):
            get_discussions_calls.append(str(req.url))
            return httpx.Response(200, json=list(created.values()))
        if req.method == "POST" and "/discussions" in str(req.url):
            payload = json.loads(req.content)
            created["d_new"] = {
                "id": "d_new",
                "notes": [
                    {
                        "id": 99,
                        "type": "DiffNote",
                        "body": payload["body"],
                        "author": {"username": "ai-bot"},
                        "resolvable": True,
                        "resolved": False,
                        "position": payload["position"],
                    }
                ],
            }
            return httpx.Response(201, json={"id": "d_new"})
        if req.method == "PUT" and "/discussions/" in str(req.url):
            resolve_calls.append(str(req.url))
            return httpx.Response(200, json={})
        return httpx.Response(404)

    prov = _make_provider(handler, cross_run_dedup=False)
    f = _finding(line=4)
    post_result = prov.post_findings(
        [f], DiffContext(diff_text=_DIFF, head_sha=_HEAD), event="REQUEST_CHANGES"
    )
    assert post_result.inline_posted == 1
    # post_findings itself never fetched prior discussions to classify against.
    assert get_discussions_calls == []

    result = prov.resolve_stale()
    # resolve_stale's own fetch is a different call and does happen; the
    # discussion just created must still survive it.
    assert resolve_calls == []
    assert result.threads_resolved == 0


def test_prior_discussions_fetch_5xx_is_failsoft_treats_all_as_new() -> None:
    """A 500 on GET /discussions during classification must not raise or
    abort the run -- it degrades to "no prior state", same as before #710."""

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and "/discussions" in str(req.url):
            return httpx.Response(500, json={"message": "unavailable"})
        if req.method == "POST" and "/discussions" in str(req.url):
            return httpx.Response(201, json={"id": "d_new"})
        return httpx.Response(404)

    prov = _make_provider(handler)
    f = _finding(line=4)
    result = prov.post_findings(
        [f], DiffContext(diff_text=_DIFF, head_sha=_HEAD), event="REQUEST_CHANGES"
    )
    assert result.inline_posted == 1
    assert result.ok


def test_prior_discussions_fetch_auth_failure_is_failsoft() -> None:
    """A 401 on GET /user (bot-identity resolution) during classification
    must degrade to "no prior state", not abort post_findings."""

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and str(req.url).endswith("/user"):
            return httpx.Response(401, json={"message": "unauthorized"})
        if req.method == "GET" and "/discussions" in str(req.url):
            return httpx.Response(200, json=[])
        if req.method == "POST" and "/discussions" in str(req.url):
            return httpx.Response(201, json={"id": "d_new"})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport, base_url="https://gitlab.com/api/v4")
    client = RecordingClient(
        http=http,
        recorder=TapeRecorder(record_dir=None),
        retry_policy=RetryPolicy(
            attempts=1, base_backoff=0, jitter=False, sleep=lambda _s: None
        ),
    )
    prov = GitLabProvider(
        config=GitLabConfig(
            project_id_or_path="42",
            mr_iid=1,
            token="glpat-test",
            diff_base_sha=_BASE,
            bot_username=None,  # forces the GET /user lookup
        ),
        client=client,
    )
    f = _finding(line=4)
    result = prov.post_findings(
        [f], DiffContext(diff_text=_DIFF, head_sha=_HEAD), event="REQUEST_CHANGES"
    )
    assert result.inline_posted == 1
    assert result.ok
