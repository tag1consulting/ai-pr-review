"""Pure verification functions for the fork pull request e2e profile.

Every function here takes already-fetched GitHub JSON (wrapped in `ForkEvidence`) and does no
I/O. Each scenario function (`verify_s1` to `verify_s17`) returns one `verify.Verdict`. The
checks that apply to every scenario live in `check_posted_bodies` and `check_review_states`.

Constants (banner, entry formats, spend issue title) come from the engine, so they cannot drift.
The text predicates are written independently of `ai_pr_review.output_clamp.strict_body` on
purpose: reusing the clamp as the oracle would share any bug it has.

Design: memory-bank/bmad/planning-artifacts/e2e-fork-pr-test-design.md (not tracked in git).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from ai_pr_review.output_clamp import BANNER_BLOCK, ENGINE_DOCS, ENGINE_REPO, FORK_BANNER
from ai_pr_review.review.fork_budget import (
    CAP_RUNS,
    LEDGER_ERROR_TEXT,
    LEDGER_TAG,
    PR_MAX_CENTS,
    PR_MAX_RUNS,
    Caps,
    Entry,
    LedgerError,
    allowance_of,
    pause_text,
    try_parse_entry,
)
from ai_pr_review.review.fork_spend import (
    AUTHOR_MAX_PRS,
    SPEND_ISSUE_TITLE,
    SPEND_TAG,
    STATE_RESERVED,
    STATE_SETTLED,
    TIER_SHARED,
    DailyCaps,
    SpendEntry,
    author_pause_text,
    daily_pause_text,
    parse_spend_entry,
)
from ai_pr_review.review.preflight import FORK_SUMMARY_NOTICE
from ai_pr_review.vcs.marker import SKIP_MARKER, SUMMARY_MARKER_PREFIX, extract_summary_sha

from .verify import Verdict

BOT_LOGIN: Final[str] = "github-actions[bot]"
ZWSP: Final[str] = "\u200b"
NOT_REVIEWED_PREFIX: Final[str] = "> **Not reviewed:**"
CANARY_FILE_PREFIX: Final[str] = "E2E-PWNED-"
BODY_CANARY_PREFIX: Final[str] = "E2E-BODY-CANARY-"

# Review states the REST API reports. A fork run may post only COMMENTED (event COMMENT).
STRONG_REVIEW_STATES: Final[frozenset[str]] = frozenset(
    {"APPROVED", "CHANGES_REQUESTED", "APPROVE", "REQUEST_CHANGES"}
)

KIND_LEDGER, KIND_SPEND, KIND_SKIP = "ledger", "spend", "skip"
KIND_SUMMARY, KIND_REVIEW, KIND_INLINE, KIND_OTHER = "summary", "review", "inline", "other"
# Kinds that go through the output clamp on a fork-profile run that posts a review.
CLAMPED_KINDS: Final[frozenset[str]] = frozenset({KIND_SUMMARY, KIND_REVIEW, KIND_INLINE, KIND_OTHER})


@dataclass(frozen=True)
class ForkEvidence:
    """Everything fetched for one fork pull request. Missing pieces stay empty."""

    pr_number: int
    head_sha: str
    base_repo: str
    author_login: str
    bot_login: str = BOT_LOGIN
    pr: dict[str, Any] = field(default_factory=dict)
    issue_comments: tuple[dict[str, Any], ...] = ()
    reviews: tuple[dict[str, Any], ...] = ()
    review_comments: tuple[dict[str, Any], ...] = ()
    spend_comments: tuple[dict[str, Any], ...] = ()
    spend_issues: tuple[dict[str, Any], ...] = ()
    runs: tuple[dict[str, Any], ...] = ()
    # Names of E2E-PWNED-* files the in-job listing step found. None means not collected.
    canary_listing: tuple[str, ...] | None = None


@dataclass(frozen=True)
class PostedBody:
    kind: str
    comment_id: int
    text: str


def _login(item: dict[str, Any]) -> str:
    user = item.get("user")
    login = user.get("login") if isinstance(user, dict) else None
    return login if isinstance(login, str) else ""


def is_bot(item: dict[str, Any], bot_login: str = BOT_LOGIN) -> bool:
    """True only when the author login equals the bot login. Fails closed on a missing user."""
    return _login(item).casefold() == bot_login.casefold() and bool(bot_login)


def _body(item: dict[str, Any]) -> str:
    body = item.get("body")
    return body if isinstance(body, str) else ""


def _cid(item: dict[str, Any]) -> int:
    value = item.get("id")
    return value if isinstance(value, int) else 0


def classify_bodies(ev: ForkEvidence) -> list[PostedBody]:
    """Every bot-authored body, classified. Human bodies (the hostile test input) are left out."""
    out: list[PostedBody] = []
    for c in ev.issue_comments:
        if not is_bot(c, ev.bot_login):
            continue
        text = _body(c)
        if text.startswith(f"<!-- {LEDGER_TAG}"):
            kind = KIND_LEDGER
        elif SKIP_MARKER in text:
            kind = KIND_SKIP
        elif SUMMARY_MARKER_PREFIX in text:
            kind = KIND_SUMMARY
        else:
            kind = KIND_OTHER
        out.append(PostedBody(kind, _cid(c), text))
    for c in ev.spend_comments:
        if is_bot(c, ev.bot_login):
            out.append(PostedBody(KIND_SPEND, _cid(c), _body(c)))
    for r in ev.reviews:
        if is_bot(r, ev.bot_login) and _body(r).strip():
            out.append(PostedBody(KIND_REVIEW, _cid(r), _body(r)))
    for c in ev.review_comments:
        if is_bot(c, ev.bot_login):
            out.append(PostedBody(KIND_INLINE, _cid(c), _body(c)))
    return out


def bodies_of(ev: ForkEvidence, *kinds: str) -> list[PostedBody]:
    return [b for b in classify_bodies(ev) if b.kind in kinds]


# ---------------------------------------------------------------------------
# Text predicates. Each one looks for text that is NOT neutralized. Clamped text still holds the
# words ("evil.example", "@team"), broken by a zero-width space, so presence alone is not a fault.
# ---------------------------------------------------------------------------

_MARKER_LINE_RE: Final[re.Pattern[str]] = re.compile(
    r"(?m)^(?:<!-- ai-pr-review-[^\n]*-->|\[//\]: # \(ai-pr-review-[^\n]*\))[ \t]*$"
)
_SCHEME_RE: Final[re.Pattern[str]] = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*)://([^\s<>\"')\]]*)")
_LINKED_SCHEMES: Final[frozenset[str]] = frozenset({"http", "https", "ftp"})
_LINK_DEST_RE: Final[re.Pattern[str]] = re.compile(r"\]\(\s*<?([^)\s>]*)")
_RAW_TAG_RE: Final[re.Pattern[str]] = re.compile(
    r"(?i)<\s*/?\s*(a|img|script|iframe|style|object|embed|svg|form|link|meta|base|video|audio|source)[\s/>]"
)
_XREF_RE: Final[re.Pattern[str]] = re.compile(
    r"(?<![A-Za-z0-9_./-])[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9_.-]+#\d"
)
_SECRET_RES: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    ("github token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}")),
    ("github fine-grained token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}")),
    ("anthropic key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{10,}")),
    ("openai-style key", re.compile(r"\bsk-[A-Za-z0-9]{32,}")),
    ("aws access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("google key", re.compile(r"\bAIza[0-9A-Za-z_-]{30,}")),
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("credential in url", re.compile(r"(?i)://[^/\s@:]+:[^/\s@]{6,}@")),
    ("basic auth header", re.compile(r"(?i)\b(authorization|x-access-token)\b[:=]\s*\S{8,}")),
)


def strip_markers(text: str) -> str:
    """Remove the hidden engine marker lines, so base64 payloads do not trip a text check."""
    return _MARKER_LINE_RE.sub("", text)


def _allowed_destination(rest: str, base_repo: str) -> bool:
    """True for a URL tail (after ``://``, with the scheme ``https``) into this repo, the engine repo
    or the engine docs site. Stricter than the design: any other github.com path fails."""
    low = rest.lower()
    docs = ENGINE_DOCS.split("://", 1)[1].lower()
    for repo in {base_repo, ENGINE_REPO}:
        if not repo:
            continue
        prefix = f"github.com/{repo.lower()}"
        if low.startswith(prefix) and (len(low) == len(prefix) or low[len(prefix)] in "/?#"):
            return ".." not in low
    return (low == docs or low.startswith(docs + "/") or low.startswith(docs + "?")) and ".." not in low


def text_problems(text: str, base_repo: str) -> list[str]:
    """Reasons the text is not safe for a fork run. Empty when every predicate holds."""
    body = strip_markers(text)
    problems: list[str] = []
    if "![" in body:
        problems.append("image syntax '![' present")
    if _RAW_TAG_RE.search(body):
        problems.append("raw html tag that can load or link")
    for m in _SCHEME_RE.finditer(body):
        scheme, rest = m.group(1).lower(), m.group(2)
        if scheme not in _LINKED_SCHEMES:
            continue  # GitHub renders other schemes (s3://, git://) as plain text, and so does the clamp
        if scheme != "https" or not _allowed_destination(rest, base_repo):
            host = rest.split("/", 1)[0][:60]
            problems.append(f"live url to {scheme}://{host}")
    for m in _LINK_DEST_RE.finditer(body):
        dest = m.group(1)
        if "://" in dest:
            continue  # the scheme check above covers it
        if dest.startswith("//") or re.match(r"(?i)^[a-z][a-z0-9+.-]*:", dest):
            problems.append(f"markdown link with a non-relative destination ({dest.split(':', 1)[0][:20]})")
    if re.search(r"\]:", body):
        problems.append("reference definition ']:' present")
    if re.search(r"@(?!" + ZWSP + ")", body):
        problems.append("live @ mention (no zero-width space after '@')")
    if re.search(r"(?i)\b(mailto|xmpp):(?!" + ZWSP + ")", body):
        problems.append("live mailto or xmpp scheme")
    if re.search(r"(?i)\bwww\.", body):
        problems.append("live www. autolink")
    no_urls = re.sub(r"\S*://\S*", "", body)
    if _XREF_RE.search(no_urls):
        problems.append("live owner/repo#N cross reference")
    for label, pattern in _SECRET_RES:
        if pattern.search(text):
            problems.append(f"secret-shaped string ({label})")
    return problems


def check_posted_bodies(
    ev: ForkEvidence,
    *,
    banner_required: Iterable[str] = (),
    banner_forbidden: bool = False,
    text_kinds: Iterable[str] = tuple(CLAMPED_KINDS | {KIND_SKIP}),
) -> list[str]:
    """The checks that run on every scenario, over the bot-authored bodies.

    * ``text_kinds`` get the text predicates. Ledger and spend entries get the secret check only.
    * ``banner_required`` kinds must start with the exact banner and hold it once.
    * ``banner_forbidden`` (a trusted run) means no body may carry the banner.
    * Every bot comment that starts with the ledger tag must parse as an entry, and every bot spend
      comment must parse with the engine's parser.
    """
    required, kinds = frozenset(banner_required), frozenset(text_kinds)
    problems: list[str] = []
    for b in classify_bodies(ev):
        where = f"{b.kind} {b.comment_id}"
        if b.kind in kinds:
            problems += [f"{where}: {p}" for p in text_problems(b.text, ev.base_repo)]
        else:
            problems += [f"{where}: {p}" for p in text_problems(b.text, ev.base_repo) if "secret" in p]
        if b.kind in required:
            if not b.text.startswith(BANNER_BLOCK):
                problems.append(f"{where}: the exact banner is missing at the top")
            elif b.text.count(FORK_BANNER) != 1:
                problems.append(f"{where}: the banner appears more than once")
        if banner_forbidden and FORK_BANNER in b.text:
            problems.append(f"{where}: carries the fork banner on a trusted run")
        if b.kind == KIND_LEDGER and try_parse_entry(b.comment_id, b.text) is None:
            problems.append(f"{where}: bot comment starts with the ledger tag but is not a valid entry")
        if b.kind == KIND_SPEND:
            try:
                parse_spend_entry(b.comment_id, b.text)
            except LedgerError as exc:
                problems.append(f"{where}: {exc}")
    return problems


def check_review_states(ev: ForkEvidence, *, allow_strong: bool = False) -> list[str]:
    """A fork-profile review is COMMENTED only. The REST API names it ``state``, not ``event``."""
    if allow_strong:
        return []
    problems: list[str] = []
    for r in ev.reviews:
        if not is_bot(r, ev.bot_login):
            continue
        state = str(r.get("state") or r.get("event") or "").upper()
        if state in STRONG_REVIEW_STATES:
            problems.append(f"review {_cid(r)}: state {state} (a fork run never approves or requests changes)")
    return problems


# ---------------------------------------------------------------------------
# Ledger helpers
# ---------------------------------------------------------------------------


def bot_entries(ev: ForkEvidence) -> list[Entry]:
    """Valid per-PR ledger entries written by the bot, sorted by comment ID."""
    entries = [
        e for c in ev.issue_comments
        if is_bot(c, ev.bot_login) and (e := try_parse_entry(_cid(c), _body(c))) is not None
    ]
    return sorted(entries, key=lambda e: e.comment_id)


def forged_entry_ids(ev: ForkEvidence) -> list[int]:
    """IDs of comments by someone else whose body holds the ledger tag. They must have no effect."""
    return [_cid(c) for c in ev.issue_comments if not is_bot(c, ev.bot_login) and LEDGER_TAG in _body(c)]


def bot_spend_entries(ev: ForkEvidence) -> list[SpendEntry]:
    out: list[SpendEntry] = []
    for c in ev.spend_comments:
        if is_bot(c, ev.bot_login) and SPEND_TAG in _body(c):
            try:
                out.append(parse_spend_entry(_cid(c), _body(c)))
            except LedgerError:
                continue  # check_posted_bodies reports it
    return sorted(out, key=lambda e: e.comment_id)


def spend_entries_for_pr(ev: ForkEvidence) -> list[SpendEntry]:
    return [e for e in bot_spend_entries(ev) if e.pr == ev.pr_number]


def spend_run_ids(ev: ForkEvidence) -> list[str]:
    """The ``run=`` field of each bot spend comment for this PR (the PR ledger carries no run ID)."""
    out: list[str] = []
    for c in ev.spend_comments:
        if not is_bot(c, ev.bot_login):
            continue
        m = re.search(r"\brun=([0-9]{1,20}|none)\b", _body(c))
        pr = re.search(r"\bpr=(\d+)\b", _body(c))
        if m and pr and int(pr.group(1)) == ev.pr_number:
            out.append(m.group(1))
    return out


def skip_bodies(ev: ForkEvidence) -> list[PostedBody]:
    return bodies_of(ev, KIND_SKIP)


def bot_review_count(ev: ForkEvidence) -> int:
    return sum(1 for r in ev.reviews if is_bot(r, ev.bot_login))


# ---------------------------------------------------------------------------
# Result helper
# ---------------------------------------------------------------------------


def _verdict(name: str, problems: Sequence[str], *, category: str = "product_failure") -> Verdict:
    if problems:
        return Verdict(name, False, " | ".join(problems), category=category)
    return Verdict(name, True, "ok")


def _common(
    ev: ForkEvidence,
    *,
    banner_required: Iterable[str] = (),
    banner_forbidden: bool = False,
    allow_strong: bool = False,
) -> list[str]:
    return check_posted_bodies(
        ev, banner_required=banner_required, banner_forbidden=banner_forbidden
    ) + check_review_states(ev, allow_strong=allow_strong)


def _nothing_reviewed(ev: ForkEvidence) -> list[str]:
    """No review, no inline comment and no summary comment by the bot."""
    problems: list[str] = []
    if bot_review_count(ev):
        problems.append("the bot posted a review")
    if any(is_bot(c, ev.bot_login) for c in ev.review_comments):
        problems.append("the bot posted inline comments")
    if bodies_of(ev, KIND_SUMMARY):
        problems.append("the bot posted a summary comment")
    return problems


def _last_skip(ev: ForkEvidence) -> PostedBody | None:
    skips = sorted(skip_bodies(ev), key=lambda b: b.comment_id)
    return skips[-1] if skips else None


def _paused_with(ev: ForkEvidence, fragment: str, what: str) -> list[str]:
    """A skip comment that holds *fragment*, and nothing reviewed."""
    skip = _last_skip(ev)
    if skip is None:
        return [f"no pause comment from the bot ({what})", *_nothing_reviewed(ev)]
    problems = [] if fragment in skip.text else [f"pause comment lacks the {what} text"]
    return problems + _nothing_reviewed(ev)


def _spend_for_pr(ev: ForkEvidence, state: str | None = None) -> list[SpendEntry]:
    return [e for e in spend_entries_for_pr(ev) if state is None or e.state == state]


# ---------------------------------------------------------------------------
# S1 to S17
# ---------------------------------------------------------------------------


def verify_s1(ev: ForkEvidence, *, require_run_evidence: bool = True) -> Verdict:
    """S1: a plain ``pull_request`` run on a fork does nothing. The fork workflow must be off."""
    problems: list[str] = []
    # Spend entries on the shared issue belong to other pull requests. _spend_for_pr covers this one.
    if [b for b in classify_bodies(ev) if b.kind != KIND_SPEND]:
        problems.append("the bot posted something on a plain pull_request fork run")
    if _spend_for_pr(ev):
        problems.append("a spend entry exists for this pull request")
    events = {str(r.get("event")) for r in ev.runs}
    if require_run_evidence:
        if "pull_request" not in events:
            problems.append("no pull_request workflow run was seen, so 'does nothing' proves nothing")
        if "pull_request_target" in events:
            problems.append("the fork workflow ran, disable it before S1")
    return _verdict("S1", problems)


def verify_s2(ev: ForkEvidence) -> Verdict:
    """S2: policy block. A pause comment, no review, no ledger."""
    from ai_pr_review.review.fork_gate import ForkGateFacts, decide

    head = ((ev.pr.get("head") or {}).get("repo") or {}).get("full_name", "")
    reason = decide(
        "pull_request_target", ev.head_sha,
        ForkGateFacts(head_repo=head, base_repo=ev.base_repo, approval_state=""),
        key_present=True, policy="block",
    ).reason
    problems = _paused_with(ev, reason, "block policy")
    if bot_entries(ev) or _spend_for_pr(ev):
        problems.append("a ledger or spend entry exists, but a block pause comes before any ledger")
    return _verdict("S2", problems + _common(ev))


def _not_reviewed_names(text: str) -> set[str]:
    line = next((ln for ln in text.splitlines() if ln.startswith(NOT_REVIEWED_PREFIX)), "")
    return set(re.findall(r"`([a-z][a-z-]*)`", line))


def verify_s3(
    ev: ForkEvidence, *, run_id: str, expected_skipped: Sequence[str], actual_cents: int | None = None
) -> Verdict:
    """S3 (with S4 and S17's canary check): the default auto review under the fork profile."""
    problems = _common(ev, banner_required=CLAMPED_KINDS)
    summaries = bodies_of(ev, KIND_SUMMARY)
    if len(summaries) != 1:
        problems.append(f"expected one summary comment, found {len(summaries)}")
    else:
        text = summaries[0].text
        if extract_summary_sha(text) != ev.head_sha:
            problems.append("the summary marker does not carry the head sha")
        if FORK_SUMMARY_NOTICE not in text:
            problems.append("the fixed fork summary notice is missing")
        if "walkthrough" in text.replace(FORK_SUMMARY_NOTICE, "").lower():
            problems.append("the summary holds a walkthrough")
    if not any(is_bot(r, ev.bot_login) for r in ev.reviews):
        problems.append("no review from the bot")
    all_text = "\n".join(b.text for b in classify_bodies(ev))
    if "```suggestion" in all_text:
        problems.append("a suggestion block was posted")
    if BODY_CANARY_PREFIX + run_id in all_text:
        problems.append("the PR body canary string reached posted text")
    names: set[str] = set()
    for b in classify_bodies(ev):
        names |= _not_reviewed_names(b.text)
    if names != set(expected_skipped):
        problems.append(f"not-reviewed analyzers {sorted(names)} differ from expected {sorted(expected_skipped)}")
    entries = bot_entries(ev)
    if len(entries) != 1 or (entries[0].runs, entries[0].allowance, entries[0].rescan_id) != (1, 0, None):
        problems.append("expected exactly one settled run entry in the PR ledger")
    elif entries[0].cents <= 0:
        problems.append("the PR ledger entry has no cost")
    spend = _spend_for_pr(ev)
    if len(spend) != 1:
        problems.append(f"expected one spend entry for this PR, found {len(spend)}")
    else:
        if spend[0].state != STATE_SETTLED:
            problems.append("the spend entry is not settled (settle never ran)")
        if spend[0].tier != TIER_SHARED:
            problems.append("the spend entry is not in the shared tier")
        if actual_cents is not None and (spend[0].cents != actual_cents or (entries and entries[0].cents != actual_cents)):
            problems.append(f"ledger cost differs from the actual cost {actual_cents}")
    if len(ev.spend_issues) != 1:
        problems.append(f"expected one spend issue, found {len(ev.spend_issues)}")
    problems += _canary_problems(ev)
    return _verdict("S3", problems)


def _canary_problems(ev: ForkEvidence) -> list[str]:
    if ev.canary_listing is None:
        return ["the canary listing was not collected"]
    return [f"canary file present: {n}" for n in ev.canary_listing]


def verify_s4(ev: ForkEvidence) -> Verdict:
    """S4: a hostile comment in the diff cannot get an approval. The state stays COMMENTED."""
    problems = _common(ev, banner_required=CLAMPED_KINDS)
    mine = [r for r in ev.reviews if is_bot(r, ev.bot_login)]
    if not mine:
        problems.append("no review from the bot, so the injection test did not run")
    elif not all(str(r.get("state")).upper() == "COMMENTED" for r in mine):
        problems.append("a bot review is not in state COMMENTED")
    return _verdict("S4", problems)


def verify_s5(ev: ForkEvidence) -> Verdict:
    """S5: a trusted author gets the normal pipeline. No banner, no ledger, no fork notice."""
    problems = check_posted_bodies(ev, banner_forbidden=True, text_kinds=())
    if bot_entries(ev) or _spend_for_pr(ev):
        problems.append("a trusted run wrote ledger or spend entries")
    summaries = bodies_of(ev, KIND_SUMMARY)
    if not summaries:
        problems.append("no summary comment")
    elif FORK_SUMMARY_NOTICE in summaries[-1].text:
        problems.append("the fork summary notice appears on a trusted run")
    elif extract_summary_sha(summaries[-1].text) != ev.head_sha:
        problems.append("the summary marker does not carry the head sha")
    if any(NOT_REVIEWED_PREFIX in b.text and "static analyzers" in b.text for b in classify_bodies(ev)):
        problems.append("the fork analyzer notice appears on a trusted run")
    return _verdict("S5", problems)


_KEY_CLASS_RE: Final[re.Pattern[str]] = re.compile(r"(?i)`(http\.[^`]*extraheader|credential\.[^`]*|includeif[^`]*)`")


def verify_s6(ev: ForkEvidence) -> Verdict:
    """S6: a stored checkout credential pauses the run. The text names a key class and no value."""
    skip = _last_skip(ev)
    problems: list[str] = []
    if skip is None:
        problems.append("no pause comment from the bot")
    else:
        t = skip.text
        if "persisted credential" not in t or "persist-credentials: false" not in t:
            problems.append("the pause text does not explain the stored credential")
        if not _KEY_CLASS_RE.search(t):
            problems.append("the pause text names no config key class")
        if "://" in t or re.search(r"[A-Za-z0-9+/=]{40,}", t):
            problems.append("the pause text holds a URL or a token-like value")
    problems += _nothing_reviewed(ev)
    if bot_entries(ev) or _spend_for_pr(ev):
        problems.append("the run wrote a ledger entry, but the pause comes before any analyzer or ledger")
    return _verdict("S6", problems + _common(ev))


def _entries_used(entries: Sequence[Entry]) -> tuple[int, int]:
    return sum(e.runs for e in entries), sum(e.cents for e in entries)


def verify_s7(ev: ForkEvidence, *, run_cents: int = 200) -> Verdict:
    """S7: the per-PR run cap. Four runs are on the ledger and the pause names the cap."""
    entries = bot_entries(ev)
    problems = _paused_with(ev, pause_text(CAP_RUNS, entries, Caps(run_cents=run_cents)), "run cap")
    if _entries_used(entries)[0] != PR_MAX_RUNS + allowance_of(entries):
        problems.append(f"the ledger holds {_entries_used(entries)[0]} runs, not the cap")
    skip = _last_skip(ev)
    if "/ai-pr-review rescan" not in (skip.text if skip else ""):
        problems.append("the pause does not mention rescan")
    return _verdict("S7", problems + _common(ev))


def verify_s8(ev: ForkEvidence, *, run_cents: int = 200) -> Verdict:
    """S8: the per-PR cost cap. The pause comes at reserve, so it holds the estimate."""
    entries = bot_entries(ev)
    used_runs, used_cents = _entries_used(entries)
    max_cents = PR_MAX_CENTS + allowance_of(entries) * run_cents
    skip = _last_skip(ev)
    problems = _nothing_reviewed(ev)
    if skip is None:
        problems.append("no pause comment from the bot")
    else:
        m = re.search(
            r"reached the cap of (\d+)\.(\d{2}) USD of review cost for one pull request "
            r"\((\d+)\.(\d{2}) USD used\)", skip.text)
        if m is None:
            problems.append("the pause does not name the cost cap")
        else:
            if int(m.group(1)) * 100 + int(m.group(2)) != max_cents:
                problems.append("the named cap differs from the engine cap")
            if int(m.group(3)) * 100 + int(m.group(4)) != used_cents:
                problems.append("the named used cost differs from the bot entries")
        if "estimated at" not in skip.text:
            problems.append("the pause holds no estimate, so it did not come from reserve")
    if _spend_for_pr(ev, STATE_RESERVED):
        problems.append("a daily entry for this PR was not released")
    if used_runs == 0:
        problems.append("the ledger is empty, so nothing was seeded")
    return _verdict("S8", problems + _common(ev))


def verify_s9(ev: ForkEvidence, *, cap: str = CAP_RUNS, run_cents: int = 200) -> Verdict:
    """S9: the author's own rescan earns no credit. The PR stays paused."""
    entries = bot_entries(ev)
    problems: list[str] = []
    if any(e.rescan_id is not None for e in entries):
        problems.append("a credit entry exists, but the author cannot credit their own rescan")
    if not any(
        not is_bot(c, ev.bot_login) and _login(c).casefold() == ev.author_login.casefold()
        and _body(c).lstrip().startswith("/ai-pr-review rescan")
        for c in ev.issue_comments
    ):
        problems.append("the author's rescan comment is not in the evidence")
    problems += _paused_with(ev, pause_text(cap, entries, Caps(run_cents=run_cents))[:80], "cap")
    return _verdict("S9", problems + _common(ev))


def verify_s10(
    ev: ForkEvidence, *, maintainer_login: str, expected_runs: int, expect_rebind: bool = False,
    run_cents: int = 200, forged_sha: str | None = None,
) -> Verdict:
    """S10: a maintainer rescan adds one credit and exactly one more run."""
    entries = bot_entries(ev)
    credits = [e for e in entries if e.rescan_id is not None]
    problems: list[str] = []
    if len(credits) != 1:
        problems.append(f"expected one credit entry, found {len(credits)}")
    else:
        src = next((c for c in ev.issue_comments if _cid(c) == credits[0].rescan_id), None)
        if src is None or _login(src).casefold() != maintainer_login.casefold():
            problems.append("the credit does not point at the maintainer's rescan comment")
    if allowance_of(entries) != 1:
        problems.append("the allowance is not exactly 1")
    if _entries_used(entries)[0] != expected_runs:
        problems.append(f"the ledger holds {_entries_used(entries)[0]} runs, expected {expected_runs}")
    if not bodies_of(ev, KIND_SUMMARY) and not bot_review_count(ev):
        problems.append("no review ran after the credit")
    if expect_rebind:
        # The pause comment of S7 stays on the pull request, so match the text for the post-credit
        # ledger (cap and used count), which only a new pause can hold.
        skip = _last_skip(ev)
        if skip is None or pause_text(CAP_RUNS, entries, Caps(run_cents=run_cents)) not in skip.text:
            problems.append("the cap did not bind again")
    if forged_sha and forged_sha != ev.head_sha:
        for b in bodies_of(ev, KIND_SUMMARY):
            if extract_summary_sha(b.text) == forged_sha:
                problems.append("the bot summary carries the forged watermark sha")
    return _verdict("S10", problems + _common(ev, banner_required={KIND_REVIEW, KIND_INLINE, KIND_SUMMARY}))


def verify_s11(ev: ForkEvidence, *, seeded_cents: int, caps: DailyCaps | None = None) -> Verdict:
    """S11: the daily cap. The pause names it, and this PR adds no spend entry."""
    text = daily_pause_text(TIER_SHARED, seeded_cents, caps or DailyCaps())
    problems = _paused_with(ev, text, "daily cap")
    if _spend_for_pr(ev):
        problems.append("a spend entry was added for the paused run")
    if bot_entries(ev):
        problems.append("the PR ledger has an entry, but the daily pause comes first")
    return _verdict("S11", problems + _common(ev))


def verify_s12(evs: Sequence[ForkEvidence]) -> Verdict:
    """S12: the author cap. Of three open fork PRs, the two lowest get reviews and the third pauses."""
    ordered = sorted(evs, key=lambda e: e.pr_number)
    if len(ordered) != AUTHOR_MAX_PRS + 1:
        return Verdict("S12", False, f"need {AUTHOR_MAX_PRS + 1} PRs, got {len(ordered)}", category="infra_failure")
    problems: list[str] = []
    for ev in ordered[:AUTHOR_MAX_PRS]:
        if not bodies_of(ev, KIND_SUMMARY) and not bot_review_count(ev):
            problems.append(f"PR {ev.pr_number} got no review")
        problems += [f"PR {ev.pr_number}: {p}" for p in _common(ev, banner_required=CLAMPED_KINDS)]
    third = ordered[-1]
    text = author_pause_text(third.author_login, len(ordered))
    problems += [f"PR {third.pr_number}: {p}" for p in _paused_with(third, text, "author cap")]
    if bot_entries(third) or _spend_for_pr(third):
        problems.append(f"PR {third.pr_number}: ledger or spend entry, but the author cap pauses first")
    problems += [f"PR {third.pr_number}: {p}" for p in _common(third)]
    return _verdict("S12", problems)


def verify_s13(ev: ForkEvidence) -> Verdict:
    """S13: a locked spend issue. Either answer passes, the verdict records which one happened."""
    issues = [i for i in ev.spend_issues if i.get("title") == SPEND_ISSUE_TITLE]
    if len(issues) != 1 or not issues[0].get("locked"):
        return Verdict("S13", False, "the spend issue is not locked, so the scenario did not run", category="infra_failure")
    reviewed = bool(bodies_of(ev, KIND_SUMMARY)) or bot_review_count(ev) > 0
    problems = _common(ev)
    if reviewed:
        if not _spend_for_pr(ev):
            problems.append("a review ran with no spend entry")
        note = "the bot CAN comment on a locked issue (recorded)"
    else:
        problems += _paused_with(ev, LEDGER_ERROR_TEXT, "ledger error")
        note = "the bot CANNOT comment on a locked issue, the run paused (recorded)"
    verdict = _verdict("S13", problems)
    return verdict if not verdict.ok else Verdict("S13", True, note)


def verify_s14(ev: ForkEvidence, *, cancelled_run_id: str, completed_run_id: str) -> Verdict:
    """S14: a cancelled run stays charged at its estimate. Keyed on the spend entry run ID."""
    ids = spend_run_ids(ev)
    if cancelled_run_id not in ids:
        return Verdict(
            "S14", False, "no spend entry for the cancelled run, the cancel landed before reserve, rerun",
            category="infra_failure")
    by_run = {}
    for c in ev.spend_comments:
        m = re.search(r"\brun=(\d+)\b.*?\bstate=(reserved|settled)\b", _body(c), re.S)
        if m and is_bot(c, ev.bot_login):
            by_run[m.group(1)] = m.group(2)
    problems: list[str] = []
    if by_run.get(cancelled_run_id) != STATE_RESERVED:
        problems.append("the cancelled run's spend entry is not at its estimate (state reserved)")
    if by_run.get(completed_run_id) != STATE_SETTLED:
        problems.append("the completed run's spend entry is not settled")
    entries = bot_entries(ev)
    if _entries_used(entries)[0] != 2:
        problems.append(f"the PR ledger holds {_entries_used(entries)[0]} runs, expected 2")
    for r in ev.runs:
        if str(r.get("id")) == cancelled_run_id and r.get("conclusion") != "cancelled":
            problems.append("the first run was not cancelled")
    return _verdict("S14", problems + _common(ev))


def verify_s15(
    ev: ForkEvidence, *, expected_bot_runs: int, expected_bot_cents: int
) -> Verdict:
    """S15: forged ledger entries have no effect on the caps. S10 covers the forged watermark."""
    problems: list[str] = []
    if not forged_entry_ids(ev):
        return Verdict("S15", False, "no forged entry in the evidence, the scenario did not run", category="infra_failure")
    entries = bot_entries(ev)
    if _entries_used(entries) != (expected_bot_runs, expected_bot_cents):
        problems.append(f"bot ledger totals {_entries_used(entries)} differ from {(expected_bot_runs, expected_bot_cents)}")
    if allowance_of(entries):
        problems.append("a forged credit changed the allowance")
    skip = _last_skip(ev)
    if skip is None:
        problems.append("no pause comment, the forged entries may have lifted the cap")
    elif f"({expected_bot_runs} used)" not in skip.text and f"({expected_bot_cents // 100}.{expected_bot_cents % 100:02d} USD used)" not in skip.text:
        problems.append("the pause text counts something other than the bot entries")
    problems += _nothing_reviewed(ev)
    return _verdict("S15", problems + _common(ev))


def verify_s16(ev: ForkEvidence, *, min_entries: int = 0) -> Verdict:
    """S16: exactly one spend issue, bot-created, with the exact title."""
    issues = [i for i in ev.spend_issues if i.get("title") == SPEND_ISSUE_TITLE and "pull_request" not in i]
    problems: list[str] = []
    if len(issues) != 1:
        problems.append(f"expected one spend issue, found {len(issues)}")
    elif not is_bot(issues[0], ev.bot_login):
        problems.append("the spend issue was not created by the bot")
    elif issues[0].get("state") not in (None, "open"):
        problems.append("the spend issue is closed")
    if len(bot_spend_entries(ev)) < min_entries:
        problems.append(f"fewer than {min_entries} bot spend entries")
    return _verdict("S16", problems + check_posted_bodies(ev))


def verify_s17(ev: ForkEvidence, *, expected_analyzers: Sequence[str] = ("phpcs", "ruff")) -> Verdict:
    """S17: fork-safe analyzers produced findings and no canary fired.

    The engine logs no 'analyzer ran' line, so the check is a finding tagged ``[name]`` in posted text.
    trufflehog and phpstan cannot be shown to have run without planting a secret or a PHP flaw they report.
    """
    from ai_pr_review.analyzers.bridge import FORK_RUN_ANALYZERS

    text = "\n".join(b.text for b in classify_bodies(ev))
    problems = [f"no finding tagged [{n}]" for n in expected_analyzers if f"[{n}]" not in text]
    named: set[str] = set()
    for b in classify_bodies(ev):
        named |= _not_reviewed_names(b.text)
    problems += [f"the notice names {n}, which a fork run does run" for n in sorted(named & FORK_RUN_ANALYZERS)]
    return _verdict("S17", problems + _canary_problems(ev) + _common(ev, banner_required=CLAMPED_KINDS))


VERIFIERS = {
    "S1": verify_s1, "S2": verify_s2, "S3": verify_s3, "S4": verify_s4, "S5": verify_s5,
    "S6": verify_s6, "S7": verify_s7, "S8": verify_s8, "S9": verify_s9, "S10": verify_s10,
    "S11": verify_s11, "S12": verify_s12, "S13": verify_s13, "S14": verify_s14,
    "S15": verify_s15, "S16": verify_s16, "S17": verify_s17,
}
