"""Builders for the fork verifier tests. Every body comes from the engine's own renderers."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ai_pr_review.output_clamp import clamp_request_body, output_clamp
from ai_pr_review.review.fork_budget import render_entry
from ai_pr_review.review.fork_spend import SPEND_ISSUE_TITLE, render_spend_entry
from ai_pr_review.review.not_reviewed import build_not_reviewed_notice
from ai_pr_review.review.preflight import FORK_SUMMARY_NOTICE
from ai_pr_review.vcs.marker import SKIP_MARKER, build_summary_marker
from tests.e2e.fork_verify import BOT_LOGIN, ForkEvidence

FIXTURES = Path(__file__).parent / "fixtures" / "fork"
BASE = "tag1consulting/ai-pr-review-test"
HEAD = "a" * 40
AUTHOR = "gchaix"
MAINTAINER = "second-maintainer"
NOW = datetime(2026, 10, 22, 12, 0, 0, tzinfo=UTC)
SKIPPED = ["eslint", "golangci-lint", "hadolint", "kube-linter", "semgrep", "shellcheck"]
HOSTILE = (
    "[click](https://evil.example/phish) ![t](https://evil.example/p.png) @gchaix-team "
    "tag1consulting/other-repo#1 www.evil.example https://evil.example/raw"
)


def load(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    return data


def user(login: str) -> dict[str, str]:
    return {"login": login, "type": "Bot" if login.endswith("[bot]") else "User"}


def comment(cid: int, login: str, body: str, **extra: Any) -> dict[str, Any]:
    return {"id": cid, "user": user(login), "body": body, **extra}


def clamp(text: str) -> str:
    """The text as the engine posts it on a fork run (banner and strict pass)."""
    with output_clamp(True, repo=BASE):
        body: str = clamp_request_body({"body": text})["body"]
    return body


def summary(cid: int = 10, *, head: str = HEAD, extra: str = "", skipped: list[str] | None = None) -> dict[str, Any]:
    notice = build_not_reviewed_notice(
        excluded_patterns=[], skipped_agents=[], is_incremental=False,
        skipped_analyzers=SKIPPED if skipped is None else skipped)
    text = f"{FORK_SUMMARY_NOTICE}\n\n{notice}\n\n{extra}\n\n{build_summary_marker(head)}"
    return comment(cid, BOT_LOGIN, clamp(text))


def review(cid: int = 20, state: str = "COMMENTED", text: str = "Findings below.") -> dict[str, Any]:
    return {"id": cid, "user": user(BOT_LOGIN), "state": state, "body": clamp(text)}


def inline(cid: int = 30, text: str = "[code-reviewer] unsafe query. [phpcs] style. [ruff] unused") -> dict[str, Any]:
    return comment(cid, BOT_LOGIN, clamp(text))


def skip(cid: int, reason: str, *, banner: bool = False) -> dict[str, Any]:
    body = f"**AI Review skipped.** {reason}\n\n{SKIP_MARKER}"
    return comment(cid, BOT_LOGIN, clamp(body) if banner else body)


def ledger(cid: int, runs: int = 1, cents: int = 45, allowance: int = 0, rescan: int | None = None) -> dict[str, Any]:
    return comment(cid, BOT_LOGIN, render_entry(runs, cents, allowance, rescan))


def spend(cid: int, *, pr: int = 41, cents: int = 45, state: str = "settled", run: str = "123",
          tier: str = "shared") -> dict[str, Any]:
    return comment(cid, BOT_LOGIN, render_spend_entry(tier, run, pr, cents, state, NOW))


def spend_issue(number: int = 7, login: str = BOT_LOGIN, **extra: Any) -> dict[str, Any]:
    return {"number": number, "title": SPEND_ISSUE_TITLE, "state": "open", "locked": False,
            "user": user(login), **extra}


def evidence(**kw: Any) -> ForkEvidence:
    base: dict[str, Any] = {
        "pr_number": 41, "head_sha": HEAD, "base_repo": BASE, "author_login": AUTHOR, "pr": load("pr.json")}
    base.update(kw)
    for key in ("issue_comments", "reviews", "review_comments", "spend_comments", "spend_issues", "runs"):
        if key in base:
            base[key] = tuple(base[key])
    return ForkEvidence(**base)


def s3_evidence(**kw: Any) -> ForkEvidence:
    """A passing S3 run."""
    base: dict[str, Any] = {
        "issue_comments": [summary(extra=HOSTILE), ledger(5)],
        "reviews": [review(text=HOSTILE)],
        "review_comments": [inline(text=HOSTILE)],
        "spend_comments": [spend(100)],
        "spend_issues": [spend_issue()],
        "canary_listing": (),
    }
    base.update(kw)
    return evidence(**base)
