"""Helper for `e2e-ledger-tools.yml` in the test repository. DRAFT, not applied anywhere.

Runs in a workflow with the repository `GITHUB_TOKEN`, so the comments it writes are authored by
`github-actions[bot]`, the only author the engine accepts for ledger and spend entries. The
entry text comes from the engine's own render functions. Put an engine checkout (the ref under
test) on `PYTHONPATH`, so the formats cannot drift. The engine modules used here need only the
standard library.

Usage (environment: GITHUB_TOKEN, GITHUB_REPOSITORY, optional GITHUB_API_URL):
    e2e_ledger_tools.py seed-ledger --pr N --runs R --usd X.XX
    e2e_ledger_tools.py seed-spend --tier reserved|shared --usd X.XX [--pr N]
    e2e_ledger_tools.py clear-ledgers [--pr N]
    e2e_ledger_tools.py lock-spend-issue | unlock-spend-issue
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime
from typing import Any

from ai_pr_review.review.fork_budget import render_entry, try_parse_entry
from ai_pr_review.review.fork_spend import (
    SPEND_ISSUE_TITLE,
    SPEND_TAG,
    STATE_SETTLED,
    TIER_RESERVED,
    TIER_SHARED,
    render_spend_entry,
)

BOT_LOGIN = "github-actions[bot]"
SPEND_ISSUE_BODY = (
    "This issue holds the daily spend entries of automatic fork pull request reviews. "
    "The bot adds and edits the comments. Do not close, lock or edit this issue."
)


def parse_usd(text: str) -> int:
    """Whole cents from ``X.XX``. A malformed value raises ``ValueError``."""
    m = re.fullmatch(r"(\d{1,6})\.(\d{2})", text.strip())
    if m is None:
        raise ValueError("usd must look like 12.34")
    return int(m.group(1)) * 100 + int(m.group(2))


def ledger_bodies(runs: int, cents: int) -> list[str]:
    """One ledger entry per run. The cents are split so the sum is exact."""
    if runs < 1:
        raise ValueError("runs must be at least 1")
    base, extra = divmod(cents, runs)
    return [render_entry(1, base + (1 if i < extra else 0), 0) for i in range(runs)]


def spend_body(tier: str, cents: int, pr: int, now: datetime) -> str:
    if tier not in (TIER_RESERVED, TIER_SHARED):
        raise ValueError("tier must be reserved or shared")
    return render_spend_entry(tier, "none", pr, cents, STATE_SETTLED, now)


class Api:
    def __init__(self) -> None:
        self.token = os.environ["GITHUB_TOKEN"]
        self.repo = os.environ["GITHUB_REPOSITORY"]
        self.base = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")

    def call(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            f"{self.base}{path}", data=data, method=method,
            headers={
                "Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "e2e-ledger-tools",
            })
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - fixed https API host
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            raise SystemExit(f"{method} {path}: HTTP {exc.code}") from None
        return json.loads(raw) if raw else None

    def pages(self, path: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        page = 1
        while True:
            sep = "&" if "?" in path else "?"
            items = self.call("GET", f"{path}{sep}per_page=100&page={page}")
            out += items
            if len(items) < 100:
                return out
            page += 1

    def comment(self, number: int, body: str) -> int:
        return int(self.call("POST", f"/repos/{self.repo}/issues/{number}/comments", {"body": body})["id"])

    def spend_issue(self, *, create: bool) -> int | None:
        """The lowest-numbered bot-created issue with the exact title. Never creates a second one."""
        found = [
            int(i["number"]) for i in self.pages(f"/repos/{self.repo}/issues?state=all&creator={BOT_LOGIN}")
            if i.get("title") == SPEND_ISSUE_TITLE and "pull_request" not in i
        ]
        if found:
            return min(found)
        if not create:
            return None
        self.call("POST", f"/repos/{self.repo}/issues", {"title": SPEND_ISSUE_TITLE, "body": SPEND_ISSUE_BODY})
        return self.spend_issue(create=False)


def seed_ledger(api: Api, pr: int, runs: int, cents: int) -> None:
    for body in ledger_bodies(runs, cents):
        print(f"seed-ledger: created comment {api.comment(pr, body)}")


def seed_spend(api: Api, tier: str, cents: int, pr: int) -> None:
    number = api.spend_issue(create=True)
    assert number is not None
    print(f"seed-spend: comment {api.comment(number, spend_body(tier, cents, pr, datetime.now(UTC)))}")


def clear_ledgers(api: Api, pr: int | None) -> None:
    """Delete bot ledger entries (one PR or all open PRs) and every bot spend entry. The issue stays."""
    prs = [pr] if pr is not None else [
        int(p["number"]) for p in api.pages(f"/repos/{api.repo}/pulls?state=open")]
    for number in prs:
        for c in api.pages(f"/repos/{api.repo}/issues/{number}/comments"):
            if (c.get("user") or {}).get("login") == BOT_LOGIN and try_parse_entry(int(c["id"]), c.get("body") or ""):
                api.call("DELETE", f"/repos/{api.repo}/issues/comments/{c['id']}")
                print(f"clear-ledgers: deleted ledger comment {c['id']} on #{number}")
    issue = api.spend_issue(create=False)
    if issue is not None:
        for c in api.pages(f"/repos/{api.repo}/issues/{issue}/comments"):
            if (c.get("user") or {}).get("login") == BOT_LOGIN and SPEND_TAG in (c.get("body") or ""):
                api.call("DELETE", f"/repos/{api.repo}/issues/comments/{c['id']}")
                print(f"clear-ledgers: deleted spend comment {c['id']}")


def set_lock(api: Api, locked: bool) -> None:
    number = api.spend_issue(create=True)
    assert number is not None
    if locked:
        api.call("PUT", f"/repos/{api.repo}/issues/{number}/lock", {"lock_reason": "resolved"})
    else:
        api.call("DELETE", f"/repos/{api.repo}/issues/{number}/lock")
    print(f"{'lock' if locked else 'unlock'}-spend-issue: issue #{number}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="e2e_ledger_tools")
    p.add_argument("action", choices=[
        "seed-ledger", "seed-spend", "clear-ledgers", "lock-spend-issue", "unlock-spend-issue"])
    p.add_argument("--pr", type=int)
    p.add_argument("--runs", type=int, default=0)
    p.add_argument("--usd", default="")
    p.add_argument("--tier", default=TIER_SHARED)
    a = p.parse_args(argv)
    api = Api()
    if a.action == "seed-ledger":
        if a.pr is None:
            p.error("seed-ledger needs --pr")
        seed_ledger(api, a.pr, a.runs, parse_usd(a.usd))
    elif a.action == "seed-spend":
        seed_spend(api, a.tier, parse_usd(a.usd), a.pr or 0)
    elif a.action == "clear-ledgers":
        clear_ledgers(api, a.pr)
    else:
        set_lock(api, a.action == "lock-spend-issue")
    return 0


if __name__ == "__main__":
    sys.exit(main())
