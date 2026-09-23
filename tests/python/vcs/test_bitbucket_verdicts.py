"""Bitbucket parity Phase 4 (#874): verdict-command polling.

Covers `ai_pr_review.vcs._bitbucket_verdicts.apply_pending_verdicts` both in
isolation and wired into `BitbucketProvider.post_findings` (gated by
`BitbucketConfig.verdicts`).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable

import httpx

from ai_pr_review.findings.models import Finding
from ai_pr_review.vcs._finding_ids import fingerprint
from ai_pr_review.vcs.bitbucket import BitbucketConfig, BitbucketProvider
from ai_pr_review.vcs.http import RecordingClient, RetryPolicy, TapeRecorder
from ai_pr_review.vcs.marker import (
    SUMMARY_MARKER_PREFIX,
    build_id_map_marker,
    build_verdicts_marker,
    extract_acks,
    extract_verdicts,
)
from ai_pr_review.vcs.protocol import DiffContext

_HEAD = "abc1234def5678abc1234def5678abc1234def56"

_DIFF = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1,3 +1,6 @@
 context_line_1
 context_line_2
 context_line_3
+added_line_4
+added_line_5
+added_line_6
"""

_FINDING = Finding(
    severity="High", confidence=90, finding="hardcoded secret", file="app.py", line=4
)
_FP = fingerprint(_FINDING)


def _make_provider(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    verdicts: bool = True,
    verdict_min_role: str = "write",
) -> BitbucketProvider:
    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport, base_url="https://api.bitbucket.org/2.0")
    client = RecordingClient(
        http=http,
        recorder=TapeRecorder(record_dir=None),
        retry_policy=RetryPolicy(
            attempts=2, base_backoff=0, jitter=False, sleep=lambda _s: None
        ),
    )
    return BitbucketProvider(
        config=BitbucketConfig(
            workspace="ws", repo_slug="repo", pr_id=7, email="x@y", api_token="t",
            code_insights=True, verdicts=verdicts, verdict_min_role=verdict_min_role,
        ),
        client=client,
    )


def _summary_body(*, verdicts: dict[str, str] | None = None, acks: str = "") -> str:
    id_map_marker = build_id_map_marker({_FP: 1}, hidden=True)
    body = (
        f"{SUMMARY_MARKER_PREFIX} sha={_HEAD} -->\n## AI Review: Request Changes\n\n"
        f"- **[F1]** HIGH: hardcoded secret\n\n{id_map_marker}"
    )
    if verdicts:
        body += "\n\n" + build_verdicts_marker(verdicts, hidden=True)
    if acks:
        body += "\n\n" + acks
    return body


_BOT_ACCOUNT_ID = "bot-acct"


def _summary_comment(comment_id: int, body: str, *, account_id: str = _BOT_ACCOUNT_ID) -> dict:
    # A real bot-posted summary comment carries the bot's own account_id --
    # required so _bot_account_id()-gated authorship verification (see
    # bitbucket.py's keep_is_bot_authored) treats it as trustworthy.
    return {"id": comment_id, "content": {"raw": body}, "user": {"account_id": account_id}}


def _command_comment(
    comment_id: int, raw: str, *, account_id: str = "acct-1", display_name: str = "alice"
) -> dict:
    return {
        "id": comment_id,
        "content": {"raw": raw},
        "user": {"account_id": account_id, "display_name": display_name},
    }


def _build_handler(
    *,
    comments: list[dict],
    permissions: dict[str, str],
    replies: list[dict] | None = None,
    put_bodies: list[str] | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        method = req.method
        if method == "GET" and req.url.path.rstrip("/").endswith("/user"):
            return httpx.Response(200, json={"account_id": _BOT_ACCOUNT_ID})
        if method == "GET" and "/permissions/repositories/" in url:
            q = req.url.params.get("q") or ""
            match = re.search(r'account_id="([^"]+)"', q)
            account_id = match.group(1) if match else ""
            perm = permissions.get(account_id)
            if perm is None:
                return httpx.Response(200, json={"values": []})
            if perm == "__error__":
                return httpx.Response(500, text="permission lookup failed")
            if perm == "__no_user_field__":
                # Simulates the q filter being ignored/unhonored, returning
                # rows this code cannot verify actually belong to the
                # queried account -- see check_authority()'s fail-closed
                # "degraded" handling for rows with no user field.
                return httpx.Response(200, json={"values": [{"permission": "admin"}]})
            return httpx.Response(
                200,
                json={
                    "values": [
                        {
                            "permission": perm,
                            "user": {"account_id": account_id},
                            # Live-verified field name (2026-09-22): Bitbucket
                            # returns full_name here, never "slug".
                            "repository": {"full_name": "ws/repo"},
                        }
                    ]
                },
            )
        if method == "GET" and req.url.path.rstrip("/").endswith("/comments"):
            return httpx.Response(200, json={"values": comments})
        if "/reports/ai-pr-review/annotations" in url and method == "POST":
            return httpx.Response(200, json={})
        if "/reports/ai-pr-review" in url and method == "DELETE":
            return httpx.Response(204)
        if "/reports/ai-pr-review" in url and method == "PUT":
            return httpx.Response(200, json={})
        if method == "POST" and req.url.path.rstrip("/").endswith("/comments"):
            payload = json.loads(req.content)
            if replies is not None:
                replies.append(payload)
            return httpx.Response(201, json={"id": 9000 + len(replies or [])})
        if method == "PUT":
            payload = json.loads(req.content)
            if put_bodies is not None:
                put_bodies.append(payload["content"]["raw"])
            return httpx.Response(200, json={"id": comments[0]["id"]})
        return httpx.Response(404)

    return handler


def test_false_positive_command_suppresses_finding() -> None:
    existing = _summary_comment(100, _summary_body())
    cmd = _command_comment(200, "/ai-pr-review false-positive F1 not exploitable")
    replies: list[dict] = []
    put_bodies: list[str] = []
    handler = _build_handler(
        comments=[existing, cmd],
        permissions={"acct-1": "write"},
        replies=replies,
        put_bodies=put_bodies,
    )
    prov = _make_provider(handler)
    result = prov.post_findings(
        [_FINDING], DiffContext(diff_text=_DIFF, head_sha=_HEAD), event="COMMENT"
    )
    assert result.ok, result.error
    assert result.suppressed == 1
    assert len(replies) == 1
    assert replies[0]["parent"]["id"] == 200
    assert "dismissed" in replies[0]["content"]["raw"] or "false-positive" in replies[0]["content"]["raw"]
    final_body = put_bodies[-1]
    assert extract_verdicts(final_body)[_FP] == "dismissed"


def test_wont_fix_command_suppresses_finding() -> None:
    existing = _summary_comment(100, _summary_body())
    cmd = _command_comment(200, "/ai-pr-review wont-fix F1 accepted risk")
    put_bodies: list[str] = []
    handler = _build_handler(
        comments=[existing, cmd], permissions={"acct-1": "write"}, replies=[], put_bodies=put_bodies,
    )
    prov = _make_provider(handler)
    result = prov.post_findings(
        [_FINDING], DiffContext(diff_text=_DIFF, head_sha=_HEAD), event="COMMENT"
    )
    assert result.ok, result.error
    assert result.suppressed == 1
    assert extract_verdicts(put_bodies[-1])[_FP] == "dismissed"


def test_dismiss_alias_suppresses_finding() -> None:
    existing = _summary_comment(100, _summary_body())
    cmd = _command_comment(200, "/ai-pr-review dismiss F1 stale finding")
    put_bodies: list[str] = []
    handler = _build_handler(
        comments=[existing, cmd], permissions={"acct-1": "write"}, replies=[], put_bodies=put_bodies,
    )
    prov = _make_provider(handler)
    result = prov.post_findings(
        [_FINDING], DiffContext(diff_text=_DIFF, head_sha=_HEAD), event="COMMENT"
    )
    assert result.ok, result.error
    assert result.suppressed == 1
    assert extract_verdicts(put_bodies[-1])[_FP] == "dismissed"


def test_fixed_command_writes_fixed_verdict_and_does_not_suppress() -> None:
    existing = _summary_comment(100, _summary_body())
    cmd = _command_comment(200, "/ai-pr-review fixed F1 abc1234")
    put_bodies: list[str] = []
    replies: list[dict] = []
    handler = _build_handler(
        comments=[existing, cmd], permissions={"acct-1": "write"}, replies=replies, put_bodies=put_bodies,
    )
    prov = _make_provider(handler)
    # The finding is still present this run (not actually removed from the
    # diff) -- "fixed" records the verdict for classify() to reconcile next
    # time the finding is or isn't seen; it is not itself a suppression.
    result = prov.post_findings(
        [_FINDING], DiffContext(diff_text=_DIFF, head_sha=_HEAD), event="COMMENT"
    )
    assert result.ok, result.error
    assert extract_verdicts(put_bodies[-1])[_FP] == "fixed"
    assert "fixed" in replies[0]["content"]["raw"]


def test_unauthorized_commenter_rejected_and_replied_to() -> None:
    existing = _summary_comment(100, _summary_body())
    cmd = _command_comment(200, "/ai-pr-review false-positive F1 nope", account_id="acct-2")
    replies: list[dict] = []
    put_bodies: list[str] = []
    handler = _build_handler(
        comments=[existing, cmd], permissions={"acct-2": "read"}, replies=replies, put_bodies=put_bodies,
    )
    prov = _make_provider(handler)
    result = prov.post_findings(
        [_FINDING], DiffContext(diff_text=_DIFF, head_sha=_HEAD), event="REQUEST_CHANGES"
    )
    assert result.ok, result.error
    assert result.suppressed == 0
    assert _FP not in extract_verdicts(put_bodies[-1])
    assert len(replies) == 1
    assert "write access" in replies[0]["content"]["raw"]


def test_failed_permission_lookup_rejected_not_accepted_with_distinguishing_reply() -> None:
    existing = _summary_comment(100, _summary_body())
    cmd = _command_comment(200, "/ai-pr-review false-positive F1 x", account_id="acct-err")
    replies: list[dict] = []
    put_bodies: list[str] = []
    handler = _build_handler(
        comments=[existing, cmd],
        permissions={"acct-err": "__error__"},
        replies=replies,
        put_bodies=put_bodies,
    )
    prov = _make_provider(handler)
    result = prov.post_findings(
        [_FINDING], DiffContext(diff_text=_DIFF, head_sha=_HEAD), event="REQUEST_CHANGES"
    )
    assert result.ok, result.error
    # Fail-closed: a broken permission lookup must never be treated as
    # authorized.
    assert result.suppressed == 0
    assert _FP not in extract_verdicts(put_bodies[-1])
    assert len(replies) == 1
    assert "could not verify" in replies[0]["content"]["raw"]
    # Distinguishable from the plain "unauthorized" wording above -- the
    # degraded case never asserts the commenter lacks access, only that it
    # couldn't be checked.
    assert "you need write access to this repository" not in replies[0]["content"]["raw"]


def test_bots_own_comment_is_never_parsed_as_a_command() -> None:
    body = _summary_body()
    existing = _summary_comment(100, body)
    # A second bot-owned comment (e.g. a stale duplicate summary) containing
    # marker text must never be mistaken for a verdict command, even if a
    # human-typed command string appeared inside it somehow.
    bot_comment = _command_comment(
        150, f"{SUMMARY_MARKER_PREFIX} sha=deadbeef -->\n/ai-pr-review dismiss F1 not-a-real-command",
        account_id="acct-bot",
    )
    replies: list[dict] = []
    put_bodies: list[str] = []
    handler = _build_handler(
        comments=[existing, bot_comment],
        permissions={"acct-bot": "admin"},
        replies=replies,
        put_bodies=put_bodies,
    )
    prov = _make_provider(handler)
    result = prov.post_findings(
        [_FINDING], DiffContext(diff_text=_DIFF, head_sha=_HEAD), event="REQUEST_CHANGES"
    )
    assert result.ok, result.error
    assert result.suppressed == 0
    assert not replies


def test_ack_idempotency_across_two_runs_exactly_one_reply() -> None:
    body = _summary_body()
    existing = _summary_comment(100, body)
    cmd = _command_comment(200, "/ai-pr-review false-positive F1 dup-check")

    # Run 1: command not yet acked.
    replies1: list[dict] = []
    put_bodies1: list[str] = []
    handler1 = _build_handler(
        comments=[existing, cmd], permissions={"acct-1": "write"}, replies=replies1, put_bodies=put_bodies1,
    )
    prov1 = _make_provider(handler1)
    r1 = prov1.post_findings(
        [_FINDING], DiffContext(diff_text=_DIFF, head_sha=_HEAD), event="COMMENT"
    )
    assert r1.ok, r1.error
    assert len(replies1) == 1
    acked_after_run1 = extract_acks(put_bodies1[-1])
    assert 200 in acked_after_run1

    # Run 2: the same command comment is still present (Bitbucket comments
    # aren't deleted after being applied), but is now in the acks marker
    # carried by the rewritten summary body.
    existing2 = _summary_comment(100, put_bodies1[-1])
    replies2: list[dict] = []
    put_bodies2: list[str] = []
    handler2 = _build_handler(
        comments=[existing2, cmd], permissions={"acct-1": "write"}, replies=replies2, put_bodies=put_bodies2,
    )
    prov2 = _make_provider(handler2)
    r2 = prov2.post_findings(
        [_FINDING], DiffContext(diff_text=_DIFF, head_sha=_HEAD), event="COMMENT"
    )
    assert r2.ok, r2.error
    assert not replies2
    # Suppression persists across the second run too.
    assert r2.suppressed == 1


def test_verdict_survives_a_full_body_rerender() -> None:
    # Simulates a verdict applied in an earlier run (already in the
    # verdicts marker) with no new command comment present this run --
    # the finding must stay suppressed purely from the marker, with
    # config.verdicts on but no comments to poll.
    body = _summary_body(verdicts={_FP: "dismissed"})
    existing = _summary_comment(100, body)
    put_bodies: list[str] = []
    handler = _build_handler(
        comments=[existing], permissions={}, replies=[], put_bodies=put_bodies,
    )
    prov = _make_provider(handler)
    result = prov.post_findings(
        [_FINDING], DiffContext(diff_text=_DIFF, head_sha=_HEAD), event="COMMENT"
    )
    assert result.ok, result.error
    assert result.suppressed == 1
    assert extract_verdicts(put_bodies[-1])[_FP] == "dismissed"


def test_malformed_line_ignored_without_blocking_sibling_command() -> None:
    existing = _summary_comment(100, _summary_body())
    raw = "/ai-pr-review not-a-real-command\n/ai-pr-review false-positive F1 real one"
    cmd = _command_comment(200, raw)
    replies: list[dict] = []
    put_bodies: list[str] = []
    handler = _build_handler(
        comments=[existing, cmd], permissions={"acct-1": "write"}, replies=replies, put_bodies=put_bodies,
    )
    prov = _make_provider(handler)
    result = prov.post_findings(
        [_FINDING], DiffContext(diff_text=_DIFF, head_sha=_HEAD), event="COMMENT"
    )
    assert result.ok, result.error
    assert result.suppressed == 1
    assert extract_verdicts(put_bodies[-1])[_FP] == "dismissed"
    # Both the parse-error reply and the valid-command reply land on the
    # same comment id, and the comment is acked exactly once regardless.
    assert all(r["parent"]["id"] == 200 for r in replies)


def test_fixed_that_recurs_gets_tombstoned_and_reposts() -> None:
    # A prior "fixed" verdict, with the same finding reappearing unchanged
    # this run and no new command comment -- classify() reaches "recurred",
    # and the tombstone must overwrite the stale "fixed" entry so it isn't
    # re-derived as "recurred" forever.
    body = _summary_body(verdicts={_FP: "fixed"})
    existing = _summary_comment(100, body)
    put_bodies: list[str] = []
    handler = _build_handler(
        comments=[existing], permissions={}, replies=[], put_bodies=put_bodies,
    )
    prov = _make_provider(handler)
    result = prov.post_findings(
        [_FINDING], DiffContext(diff_text=_DIFF, head_sha=_HEAD), event="COMMENT"
    )
    assert result.ok, result.error
    # "recurred" is treated like no verdict at all by classify() -- the
    # finding reposts (not suppressed).
    assert result.suppressed == 0
    assert extract_verdicts(put_bodies[-1])[_FP] == "recurred"


def test_verdicts_disabled_never_polls_or_applies_commands() -> None:
    existing = _summary_comment(100, _summary_body())
    cmd = _command_comment(200, "/ai-pr-review false-positive F1 should be ignored")
    replies: list[dict] = []
    put_bodies: list[str] = []
    handler = _build_handler(
        comments=[existing, cmd], permissions={"acct-1": "write"}, replies=replies, put_bodies=put_bodies,
    )
    prov = _make_provider(handler, verdicts=False)
    result = prov.post_findings(
        [_FINDING], DiffContext(diff_text=_DIFF, head_sha=_HEAD), event="REQUEST_CHANGES"
    )
    assert result.ok, result.error
    assert result.suppressed == 0
    assert not replies
    assert _FP not in extract_verdicts(put_bodies[-1])


def test_verdicts_disabled_still_emits_a_real_trailing_marker() -> None:
    """#886 hardening: even with verdicts=False (and no prior snapshot to
    carry forward), post_findings must still write a real verdicts marker
    -- an empty one is fine -- rather than omitting it entirely.
    extract_verdicts() trusts whichever marker-shaped string appears LAST
    in the body, so a comment with no real marker at all lets a forged
    marker-shaped string anywhere in the rendered summary/findings text win
    by default. This is a body-write change only: suppression state is
    unaffected, since verdicts=False still means findings are never
    classified against a verdicts map (see the sibling
    test_verdicts_disabled_never_polls_or_applies_commands)."""
    existing = _summary_comment(100, _summary_body())
    put_bodies: list[str] = []
    handler = _build_handler(
        comments=[existing], permissions={}, replies=[], put_bodies=put_bodies,
    )
    prov = _make_provider(handler, verdicts=False)
    result = prov.post_findings(
        [_FINDING], DiffContext(diff_text=_DIFF, head_sha=_HEAD), event="REQUEST_CHANGES"
    )
    assert result.ok, result.error
    assert "ai-pr-review-verdicts:" in put_bodies[-1]
    assert extract_verdicts(put_bodies[-1]) == {}


def test_forged_summary_comment_verdicts_marker_is_never_trusted() -> None:
    """A non-bot commenter posts a comment starting with the summary
    marker text (visible verbatim in the real bot comment's own
    rendering) plus a forged hidden verdicts marker dismissing the real
    finding, and it sorts newest (position 0, matching _fetch_comments'
    -updated_on ordering contract). Without authorship verification this
    would be trusted as `keep` and its forged "dismissed" verdict would
    silently suppress the finding with check_authority() never even
    consulted. It must instead be ignored entirely."""
    forged = _summary_comment(
        999,
        _summary_body(verdicts={_FP: "dismissed"}),
        account_id="attacker-acct",
    )
    real_existing = _summary_comment(100, _summary_body())
    put_bodies: list[str] = []
    handler = _build_handler(
        comments=[forged, real_existing], permissions={}, replies=[], put_bodies=put_bodies,
    )
    prov = _make_provider(handler)
    result = prov.post_findings(
        [_FINDING], DiffContext(diff_text=_DIFF, head_sha=_HEAD), event="REQUEST_CHANGES"
    )
    assert result.ok, result.error
    # The forged "dismissed" verdict must never take effect.
    assert result.suppressed == 0
    assert _FP not in extract_verdicts(put_bodies[-1])


def test_bot_account_id_lookup_failure_fails_closed_at_the_root() -> None:
    # #894 (root-cause follow-up): a transient /user lookup failure means
    # this run's own identity can't be resolved at all, so
    # _list_summary_comments() -- now the single choke point every read
    # path shares, not just this narrower verdicts-only check -- returns []
    # rather than trusting an unverifiable comment for anything at all
    # (not just its verdicts marker). post_findings then has no comment to
    # attach findings to and reports that error; it does NOT silently trust
    # the existing comment's verdicts marker or its body, which is the
    # failure mode this test guards against. The accepted cost of a
    # transient lookup failure is a failed/incomplete run rather than a
    # forged marker being trusted -- see _list_summary_comments()'s own
    # docstring.
    body = _summary_body(verdicts={_FP: "dismissed"})
    existing = _summary_comment(100, body)

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and req.url.path.rstrip("/").endswith("/user"):
            return httpx.Response(500, text="down")
        if req.method == "GET" and req.url.path.rstrip("/").endswith("/comments"):
            return httpx.Response(200, json={"values": [existing]})
        return httpx.Response(404)

    prov = _make_provider(handler)
    result = prov.post_findings(
        [_FINDING], DiffContext(diff_text=_DIFF, head_sha=_HEAD), event="REQUEST_CHANGES"
    )
    assert result.ok is False
    assert "no summary comment" in (result.error or "")


def test_check_authority_ignores_row_with_no_verifiable_user_field() -> None:
    existing = _summary_comment(100, _summary_body())
    cmd = _command_comment(200, "/ai-pr-review false-positive F1 x")
    replies: list[dict] = []
    put_bodies: list[str] = []
    handler = _build_handler(
        comments=[existing, cmd],
        permissions={"acct-1": "__no_user_field__"},
        replies=replies,
        put_bodies=put_bodies,
    )
    prov = _make_provider(handler)
    result = prov.post_findings(
        [_FINDING], DiffContext(diff_text=_DIFF, head_sha=_HEAD), event="REQUEST_CHANGES"
    )
    assert result.ok, result.error
    # A permission row that can't be verified as belonging to the
    # commenter (no user.account_id to check, simulating an ignored `q`
    # filter) must degrade, never silently grant "admin".
    assert result.suppressed == 0
    assert _FP not in extract_verdicts(put_bodies[-1])
    assert len(replies) == 1
    assert "could not verify" in replies[0]["content"]["raw"]


def test_malicious_display_name_is_sanitized_and_reply_carries_ownership_marker() -> None:
    existing = _summary_comment(100, _summary_body())
    cmd = _command_comment(
        200,
        "/ai-pr-review false-positive F1 x",
        display_name="a\nname\x07with<script>control</script> chars",
    )
    replies: list[dict] = []
    put_bodies: list[str] = []
    handler = _build_handler(
        comments=[existing, cmd], permissions={"acct-1": "write"}, replies=replies, put_bodies=put_bodies,
    )
    prov = _make_provider(handler)
    result = prov.post_findings(
        [_FINDING], DiffContext(diff_text=_DIFF, head_sha=_HEAD), event="COMMENT"
    )
    assert result.ok, result.error
    assert len(replies) == 1
    reply_text = replies[0]["content"]["raw"]
    # No embedded newline or raw control/markup character survives into the
    # bot-authored reply.
    assert "\n" not in reply_text.split("\n\n", 1)[0]
    assert "\x07" not in reply_text
    assert "<script>" not in reply_text
    # Every reply carries the ownership marker so it's never itself
    # mistaken for a candidate command comment on a later run.
    assert "[//]: # (ai-pr-review-verdict-reply)" in reply_text


def test_degraded_permission_check_leaves_comment_unacked_for_retry() -> None:
    existing = _summary_comment(100, _summary_body())
    cmd = _command_comment(200, "/ai-pr-review false-positive F1 x", account_id="acct-flaky")
    replies: list[dict] = []
    put_bodies: list[str] = []
    handler = _build_handler(
        comments=[existing, cmd],
        permissions={"acct-flaky": "__error__"},
        replies=replies,
        put_bodies=put_bodies,
    )
    prov = _make_provider(handler)
    result = prov.post_findings(
        [_FINDING], DiffContext(diff_text=_DIFF, head_sha=_HEAD), event="COMMENT"
    )
    assert result.ok, result.error
    assert len(replies) == 1
    # A "degraded" (transient) rejection must NOT be acked -- the same
    # comment should be retried on the next run rather than silently
    # dropped forever the way a durable rejection (unauthorized, malformed
    # command) correctly is.
    from ai_pr_review.vcs.marker import extract_acks
    assert 200 not in extract_acks(put_bodies[-1])


def test_check_authority_rejects_malformed_account_id_before_interpolating() -> None:
    """PR #895 review finding: account_id is interpolated into Bitbucket's
    q= filter mini-language (user.account_id="<value>"), a different
    escaping domain than HTTP query-string encoding. A value containing a
    literal `"` could break out of the quoted filter expression. account_id
    always comes from Bitbucket's own comment payload rather than commenter
    free text, so this is defense in depth -- but it must still be refused,
    not silently sent as-is."""
    from ai_pr_review.vcs._bitbucket_verdicts import check_authority

    calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and "/permissions/repositories/" in str(req.url):
            calls.append(str(req.url))
            return httpx.Response(200, json={"values": [{"permission": "admin"}]})
        return httpx.Response(404)

    client = RecordingClient(
        http=httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.bitbucket.org/2.0"),
        recorder=TapeRecorder(record_dir=None),
        retry_policy=RetryPolicy(attempts=2, base_backoff=0, jitter=False, sleep=lambda _s: None),
    )
    result = check_authority(
        client, workspace="ws", repo_slug="repo",
        account_id='557058:evil"; DROP everything --', min_role="write",
    )
    assert result == "degraded"
    assert not calls  # the malformed id must never even reach the API call


def test_verdict_polling_errors_are_logged_not_only_absorbed_into_success(caplog) -> None:
    """PR #895 review finding: verdict-polling errors were appended to
    self._errors but only ever surfaced via FindingsResult.error when the
    final comment PUT itself failed -- on an otherwise-successful run they
    were silently absorbed with no log trail at all."""
    import logging

    existing = _summary_comment(100, _summary_body())
    # A comment with no account_id at all forces apply_pending_verdicts to
    # append an error (see _bitbucket_verdicts.py's "has no account_id"
    # branch) while the overall run still succeeds.
    cmd = {
        "id": 200,
        "content": {"raw": "/ai-pr-review false-positive F1 x"},
        "user": {"display_name": "alice"},  # no account_id
    }
    handler = _build_handler(comments=[existing, cmd], permissions={})
    prov = _make_provider(handler)
    with caplog.at_level(logging.WARNING, logger="ai_pr_review.vcs.bitbucket"):
        result = prov.post_findings(
            [_FINDING], DiffContext(diff_text=_DIFF, head_sha=_HEAD), event="COMMENT"
        )
    assert result.ok, result.error
    assert any("verdict polling error" in r.message for r in caplog.records)
