"""Determinism / consistency eval harness: measures how stable the review
findings are across repeated runs of the *same* diff through the *real*
dispatch pipeline.

Background: rolling the canonical workflow out to 10 byte-identical `scolta-*`
PRs produced wildly different reviews -- 6 clean, 3 a Medium, 1 a High+Medium,
1 a different High -- for the same input. That variance traces to sampling at
Anthropic's default temperature (1.0): `llm/_config.py::resolve_temperature`
returns None for `sonnet-5`/`opus-4-8`, so no temperature field is sent and the
API applies its own default of 1.0, the least-deterministic setting. Nothing in
the repo measures this today; `tests/canary/live_model_canary.py` only checks
that a call *completes*, never whether repeated calls *agree*.

This harness re-runs the same fixture diff N times through `run_tier` +
`extract_findings` (the real prompts, real dispatch, real parsing -- not a
mock), clusters each run's findings so the *same issue* reported with different
wording is treated as one finding, and reports a stability score: the fraction
of distinct findings that appear in ALL N runs versus those that flip in and
out. A perfectly deterministic pipeline scores 1.0; the temperature-1.0 status
quo is expected to score well below that.

It also prints the *effective* temperature (`resolve_temperature`'s output) for
the model under test, so a before/after comparison across a temperature change
(Tasks 4/5 of the remediation plan) produces an actual number, not an
impression. Run it once at status quo to capture a baseline, change
`resolve_temperature`, then run it again and compare.

Clustering method (chosen deliberately): two findings are "the same issue" iff
they share a coarse key (file + severity + category) AND their content-keyword
sets overlap past a Jaccard threshold (default 0.4). Keywords are lightly
stemmed (plurals and most -ing/-ed/-ly forms collapse) so common morphological
variants between runs don't dilute the overlap; the sub-0.5 threshold absorbs
the residual variants the crude stemmer misses. The coarse key alone would
collide two genuinely-different findings in the same file; a strict text match
would split mere paraphrases of one finding; keyword-set overlap handles
paraphrase and word reordering while still separating unrelated findings
(empirically real paraphrases score ~0.45-1.0, unrelated findings ~0.0).

Line number is deliberately NOT part of the coarse key: at temperature 1.0 the
model anchors the "same" finding a few lines apart between runs, and any fixed
line-bucketing has hard boundaries that split a finding straddling the edge
(line 98 and line 103 would land in different 10-line buckets despite being the
same issue). File + severity + category + keyword-overlap is a more robust
identity than adding an unreliable line signal. (The line is still shown in the
report for human context, just not used to decide sameness.)

Not a pytest suite: this makes real, billed API calls (N runs x agents x
models) and is intentionally excluded from the default `pytest tests/python`
run. Invoke directly:

    python tests/canary/consistency_eval.py                # defaults: N=5
    AI_EVAL_RUNS=8 python tests/canary/consistency_eval.py # override run count
    AI_EVAL_MODELS=claude-sonnet-5 python tests/canary/consistency_eval.py

Exit code 0 if it completed and produced a report (regardless of the score --
a low score is a finding, not a harness failure), 1 if it could not run at all
(no API key, unreadable fixture, every run errored).
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from ai_pr_review.agents.dispatch import DispatchContext, run_tier  # noqa: E402
from ai_pr_review.agents.roster import AGENTS  # noqa: E402
from ai_pr_review.findings.extract import extract_findings  # noqa: E402
from ai_pr_review.findings.models import Finding  # noqa: E402
from ai_pr_review.llm._config import resolve_temperature  # noqa: E402
from ai_pr_review.llm.base import LLMRequest, LLMResponse  # noqa: E402
from ai_pr_review.llm.client import call_llm  # noqa: E402

# The fixture the pipeline is re-run against. Reuse the canary's stress diff by
# default: it is already in-repo, large enough to elicit real findings, and
# known to exercise the demanding agents. Override with AI_EVAL_DIFF to point at
# a different fixture (e.g. a scolta-laravel-equivalent workflow-file diff, the
# exact shape that surfaced the original variance).
DEFAULT_DIFF_PATH = Path(__file__).resolve().parent / "stress_diff.txt"

# The agents to exercise. Restricting to the two finding-heavy agents that the
# original variance showed up on keeps cost bounded (cost scales as
# runs x agents x models) while still covering the agents most exposed to
# sampling jitter. Override with AI_EVAL_AGENTS (comma-separated).
DEFAULT_AGENT_NAMES = ("code-reviewer", "silent-failure-hunter")

# Models to test. Both current defaults, since both have temperature stripped
# by resolve_temperature and so both run at the API default of 1.0 today.
# Override with AI_EVAL_MODELS (comma-separated).
DEFAULT_MODELS = ("claude-sonnet-5", "claude-opus-4-8")

DEFAULT_RUNS = 5

PROVIDER = "anthropic"
API_KEY_ENV = "ANTHROPIC_API_KEY"

# Minimum Jaccard overlap of two findings' content-keyword sets for them to be
# considered the same issue (given they already share a coarse key). Empirically
# (see the offline unit checks), real paraphrases of the same finding score
# ~0.45-1.0 while genuinely different findings score ~0.0, so 0.4 separates them
# with margin on both sides. The default is intentionally a bit below 0.5 to
# absorb the crude stemmer's imperfections (it collapses plurals and most
# -ing/-ed/-ly variants, but not every morphological pair -- see _stem). Tune
# via AI_EVAL_JACCARD; raise it toward 0.6 to be stricter about what counts as
# "the same finding" (which will lower the reported stability score).
DEFAULT_JACCARD = 0.4

_WORD_RE = re.compile(r"[a-z0-9]+")
# Stop words stripped before keyword extraction so trivial wording differences
# ("a"/"the"/"this") don't dilute the overlap ratio.
_STOP_WORDS = frozenset(
    ["a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "has", "have", "had", "he", "her", "his", "i", "if", "in", "into", "is", "it", "its", "may", "might", "must", "no", "not", "of", "on", "or", "our", "so", "should", "that", "the", "their", "them", "then", "there", "these", "this", "those", "to", "was", "we", "were", "what", "when", "which", "who", "will", "with", "without", "would", "you", "your", "does", "do"]
)
# Suffixes stripped (longest-first) to collapse common morphological variants
# so plurals and most -ing/-ed/-ly forms match across runs (checks->check,
# verifying->verif, timeouts->timeout). Deliberately crude -- a real Porter
# stemmer would need a dependency this canary script intentionally avoids -- so
# it does NOT collapse every pair (reliable/reliably survive as distinct stems,
# for one). The below-0.5 default Jaccard threshold is what absorbs those
# residual mismatches; do not treat this as a correct stemmer.
_SUFFIXES = ("ability", "ibility", "ation", "ing", "edly", "ly", "ed", "es", "s")


def _stem(word: str) -> str:
    for suf in _SUFFIXES:
        if len(word) > len(suf) + 2 and word.endswith(suf):
            return word[: -len(suf)]
    return word


def _keywords(text: str) -> frozenset[str]:
    """Content-keyword set for a finding's prose: lowercase, punctuation
    stripped, stop words removed, lightly stemmed, deduped. Order-independent by
    construction, so word reordering between runs doesn't affect the overlap
    ratio; stemming keeps morphological variants (reliable/reliably) from
    diluting it."""
    return frozenset(
        _stem(w) for w in _WORD_RE.findall(text.lower()) if w not in _STOP_WORDS
    )


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    """Jaccard similarity of two keyword sets: |intersection| / |union|.
    Returns 1.0 when both are empty (two contentless findings are 'the same'),
    0.0 when exactly one is empty."""
    if not a and not b:
        return 1.0
    union = a | b
    if not union:
        return 1.0
    return len(a & b) / len(union)


def _coarse_key(finding: Finding) -> tuple[str, str, str]:
    """The classification signature two findings must share before their text is
    even compared: file + severity + category. Line is excluded (see module
    docstring: unreliable anchor across runs). Confidence and the placement
    flags (out_of_diff / demoted_to_body) are also excluded: confidence jitters
    at temperature 1.0, and placement flags are set downstream -- none is part
    of a finding's identity."""
    return (finding.file, finding.severity, finding.category)


@dataclass
class _Cluster:
    """A group of findings judged to be the same issue across runs.

    Tracks which run indices contributed at least one finding to it, so a
    finding reported in 3 of 5 runs counts once (in 3 runs), not three times.
    """

    coarse_key: tuple[str, str, str]
    # Union of all keyword sets merged in, used as the match target for new
    # candidates (union rather than first-seen so a cluster that has absorbed
    # several phrasings matches a new phrasing of any of them).
    keywords: frozenset[str]
    runs_seen: set[int] = field(default_factory=set)
    exemplar: str = ""  # first finding's prose, for human-readable reporting
    exemplar_line: int | None = None  # first finding's line, for report context


@dataclass
class RunOutcome:
    ok: bool
    findings: list[Finding] = field(default_factory=list)
    detail: str = ""


@dataclass
class ModelReport:
    model: str
    effective_temperature: float | None
    runs_attempted: int
    runs_ok: int
    clusters: list[_Cluster] = field(default_factory=list)
    per_run_finding_counts: list[int] = field(default_factory=list)


def _assign_to_clusters(clusters: list[_Cluster], run_index: int,
                        findings: list[Finding],
                        jaccard_threshold: float) -> None:
    """Fold one run's findings into the running cluster list.

    For each finding: find the best-matching existing cluster (same coarse key,
    highest keyword-Jaccard at or above threshold). If found, mark this run as
    seen on it and widen its keyword union. Otherwise start a new cluster. A run
    can only mark a given cluster once, even if it emits two findings that both
    match it -- runs_seen is a set of run indices.
    """
    for f in findings:
        ck = _coarse_key(f)
        kw = _keywords(f.finding)
        best: _Cluster | None = None
        best_score = jaccard_threshold  # must reach the threshold to match
        for c in clusters:
            if c.coarse_key != ck:
                continue
            score = _jaccard(c.keywords, kw)
            if score >= best_score:
                best_score = score
                best = c
        if best is None:
            clusters.append(_Cluster(
                coarse_key=ck, keywords=kw, runs_seen={run_index},
                exemplar=f.finding.strip().replace("\n", " ")[:100],
                exemplar_line=f.line,
            ))
        else:
            best.runs_seen.add(run_index)
            best.keywords = best.keywords | kw


async def _one_run(model_id: str, agent_names: tuple[str, ...],
                   diff_path: Path) -> RunOutcome:
    """Run all target agents once over the fixture, return the parsed findings."""

    async def llm_call(req: LLMRequest) -> LLMResponse:
        return await call_llm(req, PROVIDER)

    agents = [a for a in AGENTS if a.name in agent_names]
    if not agents:
        return RunOutcome(ok=False, detail=f"no agents matched {agent_names!r}")

    context = DispatchContext(
        script_dir=REPO_ROOT,
        mode="full",
        diff_path=diff_path,
        provider=PROVIDER,
        standard_model=model_id,
        premium_model=model_id,
        max_tokens_per_agent=16384,
    )

    try:
        successes, failures = await run_tier(agents, llm_call, context, semaphore_size=2)
    except Exception as exc:  # noqa: BLE001 - harness must report, not crash
        return RunOutcome(ok=False, detail=f"run_tier raised: {exc!r}")

    if failures:
        # A failed agent means this run's finding set is incomplete and would
        # skew the stability math (a missing agent looks like every one of its
        # findings "flip-flopped"). Drop the whole run rather than compare a
        # partial set against complete ones.
        reasons = "; ".join(f"{f.name} exit={f.exit_code}: {f.reason[:120]}" for f in failures)
        return RunOutcome(ok=False, detail=f"agent failure(s): {reasons}")

    findings: list[Finding] = []
    for s in successes:
        findings.extend(extract_findings(s.output, agent_name=s.name, truncated=s.truncated))
    return RunOutcome(ok=True, findings=findings)


async def _eval_model(model_id: str, runs: int, agent_names: tuple[str, ...],
                      diff_path: Path,
                      jaccard_threshold: float) -> ModelReport:
    report = ModelReport(
        model=model_id,
        effective_temperature=resolve_temperature(DispatchContext(
            script_dir=REPO_ROOT, mode="full", diff_path=diff_path,
            provider=PROVIDER, standard_model=model_id,
        ).temperature, model_id),
        runs_attempted=runs,
        runs_ok=0,
    )
    clusters: list[_Cluster] = []
    run_index = 0
    for i in range(runs):
        outcome = await _one_run(model_id, agent_names, diff_path)
        if outcome.ok:
            report.runs_ok += 1
            report.per_run_finding_counts.append(len(outcome.findings))
            _assign_to_clusters(clusters, run_index, outcome.findings,
                                jaccard_threshold)
            run_index += 1
            print(f"  run {i + 1}/{runs} [{model_id}] ok: {len(outcome.findings)} findings")
        else:
            print(f"  run {i + 1}/{runs} [{model_id}] FAIL: {outcome.detail}",
                  file=sys.stderr)
    report.clusters = clusters
    return report


def _print_report(report: ModelReport) -> None:
    print(f"\n=== {report.model} ===")
    temp_str = "None (API default = 1.0)" if report.effective_temperature is None \
        else str(report.effective_temperature)
    print(f"effective temperature: {temp_str}")
    print(f"runs: {report.runs_ok}/{report.runs_attempted} succeeded")

    n = report.runs_ok
    if n < 2:
        print("stability: n/a (need >=2 successful runs to compare)")
        return

    distinct = len(report.clusters)
    counts = report.per_run_finding_counts
    if distinct == 0:
        # Zero findings on every run IS perfect stability: the pipeline agreed
        # completely (on "nothing to report").
        print(f"findings per run: {counts}")
        print("distinct findings across all runs: 0")
        print("stability score: 1.000 (every run agreed: no findings)")
        return

    stable = sum(1 for c in report.clusters if len(c.runs_seen) == n)
    flapping = distinct - stable
    score = stable / distinct

    print(f"findings per run: {counts} (min={min(counts)}, max={max(counts)})")
    print(f"distinct findings across all runs: {distinct}")
    print(f"  appeared in ALL {n} runs (stable): {stable}")
    print(f"  flip-flopped (some runs, not others): {flapping}")
    print(f"stability score: {score:.3f}  "
          f"(1.0 = every finding appeared in every run)")

    dist = Counter(len(c.runs_seen) for c in report.clusters)
    breakdown = ", ".join(f"{seen}/{n} runs: {cnt}"
                          for seen, cnt in sorted(dist.items(), reverse=True))
    print(f"  appearance breakdown -> {breakdown}")

    # List the flip-floppers explicitly -- these borderline findings are the
    # ones a temperature or threshold change should stabilize, so naming them
    # makes a before/after comparison concrete.
    if flapping:
        print("  flip-flopping findings:")
        for c in sorted(report.clusters, key=lambda c: len(c.runs_seen)):
            if len(c.runs_seen) < n:
                file_, severity, category = c.coarse_key
                loc = file_ or "(no file)"
                if c.exemplar_line is not None:
                    loc = f"{loc}:~{c.exemplar_line}"
                print(f"    [{len(c.runs_seen)}/{n}] {severity}/{category} "
                      f"{loc} -- {c.exemplar}")


async def main() -> int:
    if not os.environ.get(API_KEY_ENV):
        print(f"ERROR: {API_KEY_ENV} not set; this harness makes real billed "
              f"API calls and cannot run without it.", file=sys.stderr)
        return 1

    runs = int(os.environ.get("AI_EVAL_RUNS", str(DEFAULT_RUNS)))
    jaccard_threshold = float(os.environ.get("AI_EVAL_JACCARD", str(DEFAULT_JACCARD)))
    diff_path = Path(os.environ.get("AI_EVAL_DIFF", str(DEFAULT_DIFF_PATH)))
    agent_names = tuple(
        s.strip() for s in os.environ.get("AI_EVAL_AGENTS", ",".join(DEFAULT_AGENT_NAMES)).split(",")
        if s.strip()
    )
    models = tuple(
        s.strip() for s in os.environ.get("AI_EVAL_MODELS", ",".join(DEFAULT_MODELS)).split(",")
        if s.strip()
    )

    if not diff_path.is_file():
        print(f"ERROR: fixture diff not found: {diff_path}", file=sys.stderr)
        return 1

    print(f"Consistency eval: {runs} runs/model, models={list(models)}, "
          f"agents={list(agent_names)}, fixture={diff_path.name}, "
          f"jaccard>={jaccard_threshold}")
    print(f"(cost ~= {runs} x {len(agent_names)} x {len(models)} = "
          f"{runs * len(agent_names) * len(models)} billed agent calls)\n")

    reports: list[ModelReport] = []
    for model_id in models:
        print(f"Evaluating {model_id} ...")
        report = await _eval_model(model_id, runs, agent_names, diff_path,
                                   jaccard_threshold)
        reports.append(report)

    for report in reports:
        _print_report(report)

    # Exit 1 only if nothing ran successfully at all -- a genuinely low score is
    # the harness working as intended, not a failure.
    any_usable = any(r.runs_ok >= 2 for r in reports)
    if not any_usable:
        print("\nERROR: no model produced >=2 successful runs; cannot report "
              "stability.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
