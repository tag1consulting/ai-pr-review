"""Outbound scrub and size cap in RecordingClient (story 17-12, CAP-5)."""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from typing import Any

import httpx

from ai_pr_review.findings.models import Finding
from ai_pr_review.logging import setup_logging
from ai_pr_review.vcs._body import GITHUB_MAX_BODY_SIZE
from ai_pr_review.vcs.bitbucket import _MAX_BITBUCKET_BODY_SIZE
from ai_pr_review.vcs.github import GitHubConfig, GitHubProvider
from ai_pr_review.vcs.gitlab import _MAX_GITLAB_BODY_SIZE
from ai_pr_review.vcs.http import (
    MAX_OUTBOUND_STRING_BYTES,
    RecordingClient,
    RetryPolicy,
    TapeRecorder,
    cap_outbound_text,
)
from ai_pr_review.vcs.marker import build_id_map_marker
from ai_pr_review.vcs.protocol import DiffContext

# Built at run time so no secret-shaped literal sits in the source file.
CANARY_TOKEN = "ghp_" + "CanaryTokenValue0123456789abcdef"
CANARY_LITERAL = "LiteralCanarySecret-98765"
_SHA = "abc1234def5678abc1234def5678abc1234def56"
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


def _client(
    handler: Callable[[httpx.Request], httpx.Response] | None = None,
    sent: list[bytes] | None = None,
) -> RecordingClient:
    def _wrap(request: httpx.Request) -> httpx.Response:
        if sent is not None:
            sent.append(request.content)
        if handler is not None:
            return handler(request)
        if request.method == "GET":
            return httpx.Response(200, json=[])
        return httpx.Response(201, json={"id": 1})

    http = httpx.Client(transport=httpx.MockTransport(_wrap), base_url="https://api.github.com")
    return RecordingClient(
        http=http,
        recorder=TapeRecorder(record_dir=None),
        retry_policy=RetryPolicy(attempts=1, base_backoff=0, jitter=False, sleep=lambda _s: None),
    )


def _post(body: Any) -> str:
    sent: list[bytes] = []
    _client(sent=sent).request("POST", "/x", json_body=body)
    return sent[0].decode("utf-8")


# ---------------------------------------------------------------------------
# Scrub
# ---------------------------------------------------------------------------


def test_token_prefix_is_scrubbed_in_nested_body() -> None:
    payload = {
        "body": f"leaked {CANARY_TOKEN} here",
        "comments": [{"body": f"inline {CANARY_TOKEN}", "line": 4}],
        "output": {"summary": CANARY_TOKEN},
    }
    wire = _post(payload)
    assert CANARY_TOKEN not in wire
    assert "<redacted>" in wire
    assert json.loads(wire)["comments"][0]["line"] == 4


def test_registered_secret_literal_is_scrubbed() -> None:
    setup_logging("human", "WARNING", "cid", secrets=frozenset({CANARY_LITERAL}))
    wire = _post({"body": f"value {CANARY_LITERAL} end"})
    assert CANARY_LITERAL not in wire


def test_env_assignment_style_review_text_is_not_rewritten() -> None:
    text = "The code sets `password = hash(x)` and `token: str` is a type."
    assert json.loads(_post({"body": text}))["body"] == text


def test_non_string_values_and_ordinary_text_are_unchanged() -> None:
    payload = {"body": "plain text", "n": 3, "flag": True, "none": None, "list": [1, "a"]}
    assert json.loads(_post(payload)) == payload


def test_multipart_field_values_are_scrubbed() -> None:
    sent: list[bytes] = []
    client = _client(sent=sent)
    client.request_multipart("POST", "https://api.bitbucket.org/2.0/src", fields={"f": CANARY_TOKEN})
    assert CANARY_TOKEN.encode() not in sent[0]


# ---------------------------------------------------------------------------
# Size cap
# ---------------------------------------------------------------------------


def test_cap_is_above_every_provider_limit() -> None:
    assert MAX_OUTBOUND_STRING_BYTES > GITHUB_MAX_BODY_SIZE
    assert MAX_OUTBOUND_STRING_BYTES > _MAX_GITLAB_BODY_SIZE
    assert MAX_OUTBOUND_STRING_BYTES > _MAX_BITBUCKET_BODY_SIZE


def test_string_under_the_cap_is_unchanged() -> None:
    text = "a" * GITHUB_MAX_BODY_SIZE
    assert json.loads(_post({"body": text}))["body"] == text


def test_string_over_the_cap_is_cut_with_a_notice() -> None:
    body = json.loads(_post({"body": "a" * (MAX_OUTBOUND_STRING_BYTES + 5000)}))["body"]
    assert body.startswith("a" * MAX_OUTBOUND_STRING_BYTES)
    assert body.endswith("outbound size limit.*")
    assert len(body.encode("utf-8")) < MAX_OUTBOUND_STRING_BYTES + 200


def test_cut_never_splits_a_multibyte_character() -> None:
    out = cap_outbound_text("é" * 100, limit=51)
    assert out.startswith("é" * 25)
    assert "�" not in out


def test_markers_survive_a_cut_and_do_not_count() -> None:
    hidden = "[//]: # (ai-pr-review-id-map:" + base64.b64encode(b"x" * 400_000).decode() + ")"
    html = build_id_map_marker({"fp": 1})
    text = "a" * (MAX_OUTBOUND_STRING_BYTES + 10) + f"\n{html}\n{hidden}"
    out = json.loads(_post({"body": text}))["body"]
    assert html in out
    assert hidden in out
    assert "outbound size limit" in out


def test_marker_alone_over_the_cap_is_untouched() -> None:
    hidden = "[//]: # (ai-pr-review-verdicts:" + base64.b64encode(b"y" * 400_000).decode() + ")"
    assert json.loads(_post({"body": "short\n" + hidden}))["body"] == "short\n" + hidden


def test_suggestion_code_does_not_count_toward_the_cap() -> None:
    fence = "```suggestion\n" + "z" * (MAX_OUTBOUND_STRING_BYTES + 10) + "\n```"
    text = "intro\n" + fence + "\ntail"
    assert json.loads(_post({"body": text}))["body"] == text


def test_prose_around_suggestion_code_is_still_capped() -> None:
    fence = "```suggestion\nfix\n```"
    text = "p" * (MAX_OUTBOUND_STRING_BYTES + 10) + "\n" + fence
    out = json.loads(_post({"body": text}))["body"]
    assert fence in out
    assert "outbound size limit" in out


# ---------------------------------------------------------------------------
# End to end through the GitHub provider
# ---------------------------------------------------------------------------


def _provider(sent: list[bytes]) -> GitHubProvider:
    client = _client(sent=sent)
    return GitHubProvider(
        config=GitHubConfig(owner="o", repo="r", pr_number=1, token="t"), client=client
    )


def test_canary_secret_in_a_finding_never_reaches_a_posted_review() -> None:
    setup_logging("human", "WARNING", "cid", secrets=frozenset({CANARY_LITERAL}))
    findings = [
        Finding(
            severity="High",
            confidence=90,
            finding=f"inline leak {CANARY_TOKEN} and {CANARY_LITERAL}",
            remediation=f"rotate {CANARY_TOKEN}",
            file="app.py",
            line=4,
            suggested_code=f"key = '{CANARY_TOKEN}'",
        ),
        Finding(severity="Low", confidence=60, finding=f"body leak {CANARY_TOKEN}"),
    ]
    sent: list[bytes] = []
    prov = _provider(sent)
    result = prov.post_findings(
        findings,
        DiffContext(diff_text=_DIFF, head_sha=_SHA),
        event="COMMENT",
        agent_prompt=f"prompt {CANARY_TOKEN}",
    )
    assert result.ok
    assert sent
    for raw in sent:
        text = raw.decode("utf-8")
        assert CANARY_TOKEN not in text
        assert CANARY_LITERAL not in text
    assert any(b"```suggestion" in raw for raw in sent)


def test_canary_secret_in_a_summary_never_reaches_a_posted_comment() -> None:
    sent: list[bytes] = []
    prov = _provider(sent)
    prov.post_summary(f"summary mentions {CANARY_TOKEN}", _SHA)
    assert sent
    assert all(CANARY_TOKEN.encode() not in raw for raw in sent)


def test_canary_secret_in_a_check_run_summary_is_scrubbed() -> None:
    sent: list[bytes] = []
    prov = _provider(sent)
    prov.post_check_run(
        name="gate", head_sha=_SHA, conclusion="success", title="t", summary=f"s {CANARY_TOKEN}"
    )
    assert sent
    assert all(CANARY_TOKEN.encode() not in raw for raw in sent)


def test_github_feedback_store_write_is_scrubbed_before_encoding() -> None:
    from ai_pr_review.feedback.store import _GitHubContentsBackend

    sent: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request.content)
        return httpx.Response(200, json={})

    backend = _GitHubContentsBackend(
        repo="o/r",
        branch="ai-pr-review-bot",
        token="t",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    backend.write(f'{{"text": "{CANARY_TOKEN}"}}\n', None)
    decoded = base64.b64decode(json.loads(sent[0])["content"]).decode("utf-8")
    assert CANARY_TOKEN not in decoded
    assert "<redacted>" in decoded
