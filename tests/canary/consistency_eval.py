"""Determinism / consistency eval harness: measures how stable review
verdicts are across repeated runs of the *same* diff through the *real*
dispatch + merge + suppress + (optional judge) + outcome pipeline.

Background: rolling the canonical workflow out to 10 byte-identical `scolta-*`
PRs produced wildly different reviews -- 6 clean, 3 a Medium, 1 a High+Medium,
1 a different High -- for the same input. That variance traces to sampling at
Anthropic's default temperature (1.0): `llm/_config.py::resolve_temperature`
returns None for `sonnet-5`/`opus-4-8`/`opus-5`, so no temperature field is
sent and the API applies its own default of 1.0, the least-deterministic
setting. Nothing in the repo measured this before this harness existed;
`tests/canary/live_model_canary.py` only checks that a call *completes*, never
whether repeated calls *agree*. See issue #799 (ai-pr-review) for the full
writeup and issue #800 for this harness's own scope.

This harness re-runs each fixture diff N times through `run_tier` +
`extract_findings` + `merge_findings` + `apply_suppressions` + (optionally)
`judge_findings` + `classify_review_outcome` -- the real prompts, real
dispatch, real parsing, real verdict logic, not a mock -- clusters each run's
findings so the *same issue* reported with different wording is treated as one
finding, and reports:

  - a finding-level stability score (as before): the fraction of distinct
    findings that appear in ALL N runs versus those that flip in and out.
  - a verdict-level stability readout: does the actual APPROVE / COMMENT /
    REQUEST_CHANGES event change across runs on the identical input?
  - (corpus mode) a gate-firing rate per diff: which conditional agent gates
    (`agents/gates.py`) fire, computed once per diff since gates are
    deterministic on diff content and cost nothing to evaluate.
  - (judge arm) a downrank count: how many kept findings the judge pass
    demotes (`demoted_to_body=True`) per run.

A perfectly deterministic pipeline scores 1.0 on both the finding- and
verdict-level scores; the temperature-1.0 status quo is expected to score
well below that on at least the finding-level score.

It also prints the *effective* temperature (`resolve_temperature`'s output)
for the model under test, so a before/after comparison across a temperature
change produces an actual number, not an impression.

Clustering method (chosen deliberately, unchanged from the original
single-diff design): two findings are "the same issue" iff they share a
coarse key (file + severity + category) AND their content-keyword sets
overlap past a Jaccard threshold (default 0.4). Keywords are lightly stemmed
(plurals and most -ing/-ed/-ly forms collapse) so common morphological
variants between runs don't dilute the overlap; the sub-0.5 threshold absorbs
the residual variants the crude stemmer misses. The coarse key alone would
collide two genuinely-different findings in the same file; a strict text
match would split mere paraphrases of one finding; keyword-set overlap
handles paraphrase and word reordering while still separating unrelated
findings (empirically real paraphrases score ~0.45-1.0, unrelated findings
~0.0).

Line number is deliberately NOT part of the coarse key: at temperature 1.0
the model anchors the "same" finding a few lines apart between runs, and any
fixed line-bucketing has hard boundaries that split a finding straddling the
edge (line 98 and line 103 would land in different 10-line buckets despite
being the same issue). File + severity + category + keyword-overlap is a
more robust identity than adding an unreliable line signal. (The line is
still shown in the report for human context, just not used to decide
sameness.)

Corpus mode (issue #800): set AI_EVAL_CORPUS_DIR to a directory of `*.diff`
files (defaults to `tests/canary/corpus/` if that directory exists and
AI_EVAL_DIFF was not explicitly set) to run the full protocol across every
diff in it instead of a single fixture. Single-diff mode (AI_EVAL_DIFF,
unchanged from the original harness) still works and is what runs if no
corpus directory is present -- this keeps a quick, cheap smoke-test path
available without needing the full corpus.

Arms (issue #800, judge/context-enrichment "decide on data"): set
AI_EVAL_ARMS to a comma-separated list of `baseline`, `judge`,
`context-enrichment`, `shared-context` (default: `baseline` only, to keep an
unconfigured invocation cheap). Each active arm re-runs the full N-repeat
protocol with that arm's toggle turned on relative to the baseline (judge
off, context enrichment off, shared-context block off); arms are additive
one-at-a-time, not combinatorial, to keep cost bounded, per the sequencing
decided for Epic 9. `shared-context` (#813) has no PR title/description per
corpus fixture -- see that arm's entry in _ARM_TOGGLES for what it actually
measures.

Not a pytest suite: this makes real, billed API calls (diffs x arms x runs x
agents x models) and is intentionally excluded from the default
`pytest tests/python` run. Invoke directly:

    python tests/canary/consistency_eval.py                     # single fixture, N=5, baseline arm
    AI_EVAL_RUNS=8 python tests/canary/consistency_eval.py      # override run count
    AI_EVAL_MODELS=claude-sonnet-5 python tests/canary/consistency_eval.py
    AI_EVAL_CORPUS_DIR=tests/canary/corpus python tests/canary/consistency_eval.py
    AI_EVAL_CORPUS_LIMIT=2 AI_EVAL_CORPUS_DIR=tests/canary/corpus python tests/canary/consistency_eval.py  # smoke test: first 2 corpus diffs only
    AI_EVAL_ARMS=baseline,judge,context-enrichment python tests/canary/consistency_eval.py

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
from ai_pr_review.agents.gates import evaluate_gates, filter_agents  # noqa: E402
from ai_pr_review.agents.roster import AGENTS  # noqa: E402
from ai_pr_review.findings.extract import extract_findings  # noqa: E402
from ai_pr_review.findings.merge import merge_findings  # noqa: E402
from ai_pr_review.findings.models import Finding  # noqa: E402
from ai_pr_review.findings.suppress import apply_suppressions, load_rules  # noqa: E402
from ai_pr_review.llm._config import resolve_temperature  # noqa: E402
from ai_pr_review.llm.base import LLMRequest, LLMResponse  # noqa: E402
from ai_pr_review.llm.client import call_llm  # noqa: E402
from ai_pr_review.manifest import build_changed_files, build_manifest_text  # noqa: E402
from ai_pr_review.pricing import TokenEntry, compute_totals, format_cost, load_pricing  # noqa: E402
from ai_pr_review.review.outcome import classify_review_outcome  # noqa: E402
from ai_pr_review.review.pr_context import build_shared_context_block  # noqa: E402

_PRICING_DATA = load_pricing(str(REPO_ROOT / "config" / "model-pricing.json"))

# The fixture the pipeline is re-run against in single-diff mode. Reuse the
# canary's stress diff by default: it is already in-repo, large enough to
# elicit real findings, and known to exercise the demanding agents. Override
# with AI_EVAL_DIFF to point at a different fixture. Ignored when corpus mode
# is active (AI_EVAL_CORPUS_DIR set, or the default corpus directory exists).
DEFAULT_DIFF_PATH = Path(__file__).resolve().parent / "stress_diff.txt"

# Corpus mode (issue #800): a directory of frozen *.diff files, chosen for
# language and risk diversity. See tests/canary/corpus/README.md for
# provenance. Corpus mode activates automatically if this directory exists
# and AI_EVAL_DIFF was not explicitly set, so a bare invocation after cloning
# the repo exercises the full corpus rather than silently falling back to
# the single stress-diff fixture.
DEFAULT_CORPUS_DIR = Path(__file__).resolve().parent / "corpus"

# The agents to exercise. Restricting to the two finding-heavy agents that the
# original variance showed up on keeps cost bounded (cost scales as
# diffs x arms x runs x agents x models) while still covering the agents most
# exposed to sampling jitter. Override with AI_EVAL_AGENTS (comma-separated).
DEFAULT_AGENT_NAMES = ("code-reviewer", "silent-failure-hunter")

# Models to test. Both current defaults, since both have temperature stripped
# by resolve_temperature and so both run at the API default of 1.0 today.
# Override with AI_EVAL_MODELS (comma-separated).
DEFAULT_MODELS = ("claude-sonnet-5", "claude-opus-5")

DEFAULT_RUNS = 5

PROVIDER = "anthropic"
API_KEY_ENV = "ANTHROPIC_API_KEY"

# Arms (issue #800): each toggles one production feature on relative to the
# baseline, one at a time (not combinatorial) to keep cost bounded. See the
# module docstring for the "decide on data" rationale.
_ARM_TOGGLES: dict[str, dict[str, bool]] = {
    "baseline": {"judge": False, "context_enrichment": False, "shared_context": False},
    "judge": {"judge": True, "context_enrichment": False, "shared_context": False},
    "context-enrichment": {"judge": False, "context_enrichment": True, "shared_context": False},
    # #813: the shared PR-context block (file manifest + PR title/description)
    # every finding agent except blind-hunter now receives. This harness has
    # no PR title/description per corpus fixture (each is a bare .diff file,
    # not a full PR record) -- title/body are always "" here, so this arm
    # measures the manifest-only half of #813's change, not the full effect
    # of adding real PR intent. See tests/canary/corpus/README.md.
    "shared-context": {"judge": False, "context_enrichment": False, "shared_context": True},
}
DEFAULT_ARMS = ("baseline",)

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


class _AsFindingLike:
    """Adapter so a `Finding` can be passed to outcome's `_FindingLike` Protocol.

    Outcome's Protocol declares `severity: str`; Finding's
    `Literal["Critical","High","Medium","Low"]` is a subtype but mypy treats
    Protocol attrs as invariant. Same one-line bridge `orchestrate.py` uses at
    its own `classify_review_outcome` call site.
    """

    __slots__ = ("severity",)

    def __init__(self, f: Finding) -> None:
        self.severity: str = f.severity


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
    # Populated once the run reaches classify_review_outcome (issue #800: a
    # verdict-level readout, not just raw per-agent finding clusters).
    event: str = ""
    suppressed_count: int = 0
    judge_downranked_count: int = 0
    judge_input_tokens: int = 0
    judge_output_tokens: int = 0
    # Real, directly-observed spend for this run (agent calls + judge call if
    # any), not an estimate -- this is what lets a smoke test report an actual
    # dollar figure before committing to the full corpus x arms x runs cost.
    token_entries: list[TokenEntry] = field(default_factory=list)


@dataclass
class ModelReport:
    model: str
    arm: str
    effective_temperature: float | None
    runs_attempted: int
    runs_ok: int
    clusters: list[_Cluster] = field(default_factory=list)
    per_run_finding_counts: list[int] = field(default_factory=list)
    events: list[str] = field(default_factory=list)
    total_suppressed: int = 0
    total_judge_downranked: int = 0
    total_judge_input_tokens: int = 0
    total_judge_output_tokens: int = 0
    all_token_entries: list[TokenEntry] = field(default_factory=list)


@dataclass
class DiffReport:
    """All arm/model reports for one corpus diff, plus its (arm/model
    independent) gate-firing readout."""

    diff_name: str
    fired_gates: frozenset[str]
    agents_that_would_run: list[str]
    model_reports: list[ModelReport] = field(default_factory=list)


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


def _changed_files_from_diff(diff_text: str) -> list[str]:
    """Extract the changed-file path list from raw unified-diff text.

    Reads `+++ b/<path>` lines (the post-image path), skipping `/dev/null`
    (a deleted file has no post-image to review). Good enough for a frozen
    fixture corpus; not a full diff parser (renames/quoted-path edge cases
    are not handled, since none of the corpus fixtures need them -- see
    tests/canary/corpus/README.md for what's in the corpus).
    """
    paths = []
    for line in diff_text.splitlines():
        if line.startswith("+++ ") and not line.startswith("+++ /dev/null"):
            path = line[4:].strip()
            if path.startswith("b/"):
                path = path[2:]
            paths.append(path)
    return paths


def _gate_report_for_diff(diff_path: Path) -> tuple[frozenset[str], list[str]]:
    """Evaluate which conditional agent gates fire for one diff (issue #800).

    Deterministic on diff content, costs nothing (no LLM call), so this runs
    once per corpus diff regardless of how many arms/models/runs are
    configured -- unlike the N-repeat model dispatch below, there is nothing
    to average out here.
    """
    diff_text = diff_path.read_text()
    changed_files = build_changed_files(_changed_files_from_diff(diff_text))
    fired = evaluate_gates(diff_text, changed_files, env={})
    would_run = filter_agents(list(AGENTS), fired)
    return fired, [a.name for a in would_run]


async def _one_run(model_id: str, agent_names: tuple[str, ...],
                   diff_path: Path, arm: str, script_dir: Path) -> RunOutcome:
    """Run all target agents once over the fixture, then push the results
    through the real merge/suppress/(judge)/outcome pipeline (issue #800:
    a verdict-level readout, not just raw per-agent finding clusters)."""

    async def llm_call(req: LLMRequest) -> LLMResponse:
        return await call_llm(req, PROVIDER)

    agents = [a for a in AGENTS if a.name in agent_names]
    if not agents:
        return RunOutcome(ok=False, detail=f"no agents matched {agent_names!r}")

    toggles = _ARM_TOGGLES[arm]
    shared_context_block = ""
    if toggles["shared_context"]:
        diff_text = diff_path.read_text()
        changed_files = build_changed_files(_changed_files_from_diff(diff_text))
        manifest_text = build_manifest_text(
            changed_files, base_ref="main", diff_label=diff_path.name, diff_stat="",
        )
        # No PR title/description per corpus fixture (see _ARM_TOGGLES's
        # "shared-context" comment) -- title/body are always "" here.
        shared_context_block = build_shared_context_block(
            manifest_text=manifest_text, pr_title="", pr_body="",
        )
    context = DispatchContext(
        script_dir=script_dir,
        mode="full",
        diff_path=diff_path,
        provider=PROVIDER,
        standard_model=model_id,
        premium_model=model_id,
        max_tokens_per_agent=32768,
        enable_context_enrichment=toggles["context_enrichment"],
        repo_root=script_dir,
        shared_context_block=shared_context_block,
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

    raw_findings: list[Finding] = []
    token_entries: list[TokenEntry] = []
    for s in successes:
        raw_findings.extend(extract_findings(s.output, agent_name=s.name, truncated=s.truncated))
        if s.token_log is not None:
            token_entries.append(TokenEntry(
                agent=s.name, model=s.token_log.model,
                input_tokens=s.token_log.input, output_tokens=s.token_log.output,
                cache_creation_tokens=s.token_log.cache_creation,
                cache_read_tokens=s.token_log.cache_read,
            ))

    # Real pipeline from here: merge (confidence floor + proximity dedup) ->
    # suppress (config/suppressions.json, no local workspace override for a
    # frozen fixture) -> (judge, if this arm has it on) -> classify.
    merged = merge_findings(raw_findings)
    rules = load_rules(str(script_dir))
    kept, suppressed_count = apply_suppressions(merged, rules)

    judge_downranked = 0
    judge_input_tokens = 0
    judge_output_tokens = 0
    if toggles["judge"] and kept:
        judge_prompt_path = script_dir / "prompts" / "finding-judge.md"
        if judge_prompt_path.exists():
            from ai_pr_review.findings.judge import judge_findings
            try:
                judge_result = await judge_findings(
                    kept, llm_call=llm_call, model=model_id, prompt_path=judge_prompt_path,
                )
                kept = judge_result.findings
                judge_downranked = sum(1 for f in kept if f.demoted_to_body)
                judge_input_tokens = judge_result.input_tokens
                judge_output_tokens = judge_result.output_tokens
                token_entries.append(TokenEntry(
                    agent="judge-pass", model=model_id,
                    input_tokens=judge_result.input_tokens,
                    output_tokens=judge_result.output_tokens,
                    cache_creation_tokens=judge_result.cache_creation_tokens,
                    cache_read_tokens=judge_result.cache_read_tokens,
                ))
            except Exception as exc:  # noqa: BLE001 - judge is fail-soft in production too
                print(f"    judge pass raised (fail-soft, kept pre-judge findings): {exc!r}",
                      file=sys.stderr)

    outcome = classify_review_outcome(
        [_AsFindingLike(f) for f in kept], failed_agents=[], mode="full",
    )

    return RunOutcome(
        ok=True, findings=kept, event=outcome.event,
        suppressed_count=suppressed_count,
        token_entries=token_entries,
        judge_downranked_count=judge_downranked,
        judge_input_tokens=judge_input_tokens,
        judge_output_tokens=judge_output_tokens,
    )


async def _eval_model(model_id: str, runs: int, agent_names: tuple[str, ...],
                      diff_path: Path, arm: str, script_dir: Path,
                      jaccard_threshold: float) -> ModelReport:
    report = ModelReport(
        model=model_id,
        arm=arm,
        effective_temperature=resolve_temperature(DispatchContext(
            script_dir=script_dir, mode="full", diff_path=diff_path,
            provider=PROVIDER, standard_model=model_id,
        ).temperature, model_id),
        runs_attempted=runs,
        runs_ok=0,
    )
    clusters: list[_Cluster] = []
    run_index = 0
    for i in range(runs):
        outcome = await _one_run(model_id, agent_names, diff_path, arm, script_dir)
        if outcome.ok:
            report.runs_ok += 1
            report.per_run_finding_counts.append(len(outcome.findings))
            report.events.append(outcome.event)
            report.total_suppressed += outcome.suppressed_count
            report.total_judge_downranked += outcome.judge_downranked_count
            report.total_judge_input_tokens += outcome.judge_input_tokens
            report.total_judge_output_tokens += outcome.judge_output_tokens
            report.all_token_entries.extend(outcome.token_entries)
            _assign_to_clusters(clusters, run_index, outcome.findings,
                                jaccard_threshold)
            run_index += 1
            print(f"  run {i + 1}/{runs} [{model_id}/{arm}] ok: "
                  f"{len(outcome.findings)} findings, event={outcome.event}")
        else:
            print(f"  run {i + 1}/{runs} [{model_id}/{arm}] FAIL: {outcome.detail}",
                  file=sys.stderr)
    report.clusters = clusters
    return report


def _print_model_report(report: ModelReport) -> None:
    print(f"\n--- {report.model} [{report.arm}] ---")
    temp_str = "None (API default = 1.0)" if report.effective_temperature is None \
        else str(report.effective_temperature)
    print(f"effective temperature: {temp_str}")
    print(f"runs: {report.runs_ok}/{report.runs_attempted} succeeded")
    if report.all_token_entries:
        totals = compute_totals(report.all_token_entries, _PRICING_DATA)
        cost_str = format_cost(totals.cost_units)
        unknown_note = " (some models had no pricing entry)" if totals.any_unknown else ""
        print(f"real spend this arm/model: {cost_str}{unknown_note} "
              f"({totals.grand_total} tokens across {len(report.all_token_entries)} calls)")

    n = report.runs_ok
    if n < 2:
        print("stability: n/a (need >=2 successful runs to compare)")
        return

    # Verdict-level stability (issue #800/#799): does the actual event flip?
    event_counts = Counter(report.events)
    verdict_stable = len(event_counts) == 1
    print(f"verdict events across runs: {dict(event_counts)}  "
          f"({'STABLE' if verdict_stable else 'FLIPS'})")
    if report.total_suppressed:
        print(f"total suppressed across runs: {report.total_suppressed}")
    if report.arm == "judge":
        print(f"total judge downranks across runs: {report.total_judge_downranked} "
              f"(judge tokens: {report.total_judge_input_tokens} in / "
              f"{report.total_judge_output_tokens} out)")

    distinct = len(report.clusters)
    counts = report.per_run_finding_counts
    if distinct == 0:
        # Zero findings on every run IS perfect stability: the pipeline agreed
        # completely (on "nothing to report").
        print(f"findings per run: {counts}")
        print("distinct findings across all runs: 0")
        print("finding stability score: 1.000 (every run agreed: no findings)")
        return

    stable = sum(1 for c in report.clusters if len(c.runs_seen) == n)
    flapping = distinct - stable
    score = stable / distinct

    print(f"findings per run: {counts} (min={min(counts)}, max={max(counts)})")
    print(f"distinct findings across all runs: {distinct}")
    print(f"  appeared in ALL {n} runs (stable): {stable}")
    print(f"  flip-flopped (some runs, not others): {flapping}")
    print(f"finding stability score: {score:.3f}  "
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


def _print_diff_report(report: DiffReport) -> None:
    print(f"\n=== {report.diff_name} ===")
    gates_str = ", ".join(sorted(report.fired_gates)) if report.fired_gates else "(none fired)"
    print(f"gates fired: {gates_str}")
    print(f"agents that would run in production for this diff: "
          f"{', '.join(sorted(report.agents_that_would_run))}")
    for mr in report.model_reports:
        _print_model_report(mr)


def _print_corpus_summary(reports: list[DiffReport]) -> None:
    """Aggregate readout across the whole corpus -- the actual "baseline"
    number issue #800 exists to produce: how stable are verdicts and
    findings across a realistic spread of diffs, not just one fixture."""
    print("\n" + "=" * 70)
    print("CORPUS SUMMARY")
    print("=" * 70)

    gate_counter: Counter[str] = Counter()
    for dr in reports:
        for g in dr.fired_gates:
            gate_counter[g] += 1
    if reports:
        print(f"\nGate firing rate across {len(reports)} corpus diffs:")
        for gate, count in gate_counter.most_common():
            print(f"  {gate}: {count}/{len(reports)} diffs ({100 * count / len(reports):.0f}%)")

    by_arm_model: dict[tuple[str, str], list[ModelReport]] = {}
    for dr in reports:
        for mr in dr.model_reports:
            by_arm_model.setdefault((mr.arm, mr.model), []).append(mr)

    for (arm, model), mrs in sorted(by_arm_model.items()):
        usable = [m for m in mrs if m.runs_ok >= 2]
        if not usable:
            continue
        finding_scores = []
        verdict_stable_count = 0
        for m in usable:
            distinct = len(m.clusters)
            if distinct == 0:
                finding_scores.append(1.0)
            else:
                stable = sum(1 for c in m.clusters if len(c.runs_seen) == m.runs_ok)
                finding_scores.append(stable / distinct)
            if len(set(m.events)) == 1:
                verdict_stable_count += 1
        avg_finding_score = sum(finding_scores) / len(finding_scores)
        print(f"\n[{model} / {arm}] across {len(usable)} usable diffs:")
        print(f"  mean finding-stability score: {avg_finding_score:.3f}")
        print(f"  verdict-stable diffs: {verdict_stable_count}/{len(usable)} "
              f"({100 * verdict_stable_count / len(usable):.0f}%)")
        total_downranked = sum(m.total_judge_downranked for m in usable)
        if arm == "judge" and total_downranked:
            print(f"  total judge downranks: {total_downranked}")
        all_entries = [e for m in usable for e in m.all_token_entries]
        if all_entries:
            totals = compute_totals(all_entries, _PRICING_DATA)
            print(f"  real spend: {format_cost(totals.cost_units)} "
                  f"({totals.grand_total} tokens across {len(all_entries)} calls)")

    grand_total_entries = [
        e for dr in reports for mr in dr.model_reports for e in mr.all_token_entries
    ]
    if grand_total_entries:
        grand_totals = compute_totals(grand_total_entries, _PRICING_DATA)
        print(f"\nTOTAL real spend this invocation: {format_cost(grand_totals.cost_units)} "
              f"({grand_totals.grand_total} tokens across {len(grand_total_entries)} calls)")


async def _run_one_diff(diff_path: Path, runs: int, agent_names: tuple[str, ...],
                        arms: tuple[str, ...], models: tuple[str, ...],
                        script_dir: Path, jaccard_threshold: float) -> DiffReport:
    fired_gates, would_run = _gate_report_for_diff(diff_path)
    diff_report = DiffReport(
        diff_name=diff_path.name, fired_gates=fired_gates,
        agents_that_would_run=would_run,
    )
    for arm in arms:
        for model_id in models:
            print(f"Evaluating {diff_path.name} :: {model_id} :: {arm} ...")
            mr = await _eval_model(model_id, runs, agent_names, diff_path, arm,
                                   script_dir, jaccard_threshold)
            diff_report.model_reports.append(mr)
    return diff_report


async def main() -> int:
    if not os.environ.get(API_KEY_ENV):
        print(f"ERROR: {API_KEY_ENV} not set; this harness makes real billed "
              f"API calls and cannot run without it.", file=sys.stderr)
        return 1

    runs = int(os.environ.get("AI_EVAL_RUNS", str(DEFAULT_RUNS)))
    jaccard_threshold = float(os.environ.get("AI_EVAL_JACCARD", str(DEFAULT_JACCARD)))
    agent_names = tuple(
        s.strip() for s in os.environ.get("AI_EVAL_AGENTS", ",".join(DEFAULT_AGENT_NAMES)).split(",")
        if s.strip()
    )
    models = tuple(
        s.strip() for s in os.environ.get("AI_EVAL_MODELS", ",".join(DEFAULT_MODELS)).split(",")
        if s.strip()
    )
    arms = tuple(
        s.strip() for s in os.environ.get("AI_EVAL_ARMS", ",".join(DEFAULT_ARMS)).split(",")
        if s.strip()
    )
    for arm in arms:
        if arm not in _ARM_TOGGLES:
            print(f"ERROR: unknown arm {arm!r}; valid arms: {sorted(_ARM_TOGGLES)}", file=sys.stderr)
            return 1

    explicit_diff = os.environ.get("AI_EVAL_DIFF")
    corpus_dir_env = os.environ.get("AI_EVAL_CORPUS_DIR")
    corpus_dir = Path(corpus_dir_env) if corpus_dir_env else DEFAULT_CORPUS_DIR
    use_corpus = explicit_diff is None and corpus_dir.is_dir()

    if use_corpus:
        corpus_files = sorted(corpus_dir.glob("*.diff"))
        limit = os.environ.get("AI_EVAL_CORPUS_LIMIT")
        if limit:
            corpus_files = corpus_files[: int(limit)]
        if not corpus_files:
            print(f"ERROR: no *.diff files found in corpus directory {corpus_dir}", file=sys.stderr)
            return 1

        total_calls = len(corpus_files) * len(arms) * runs * len(agent_names) * len(models)
        print(f"Consistency eval (corpus mode): {len(corpus_files)} diffs, "
              f"{runs} runs/model, models={list(models)}, agents={list(agent_names)}, "
              f"arms={list(arms)}, jaccard>={jaccard_threshold}")
        print(f"(cost ~= {len(corpus_files)} diffs x {len(arms)} arms x {runs} runs x "
              f"{len(agent_names)} agents x {len(models)} models = "
              f"{total_calls} billed agent calls, plus judge-arm judge calls)\n")

        diff_reports: list[DiffReport] = []
        for diff_path in corpus_files:
            dr = await _run_one_diff(diff_path, runs, agent_names, arms, models,
                                     REPO_ROOT, jaccard_threshold)
            diff_reports.append(dr)

        for dr in diff_reports:
            _print_diff_report(dr)
        _print_corpus_summary(diff_reports)

        any_usable = any(
            mr.runs_ok >= 2 for dr in diff_reports for mr in dr.model_reports
        )
        if not any_usable:
            print("\nERROR: no model/diff/arm combination produced >=2 successful "
                  "runs; cannot report stability.", file=sys.stderr)
            return 1
        return 0

    # Single-diff mode (original harness behavior, preserved for a cheap
    # smoke test / backward compatibility with any external caller that set
    # AI_EVAL_DIFF explicitly).
    diff_path = Path(explicit_diff) if explicit_diff else DEFAULT_DIFF_PATH
    if not diff_path.is_file():
        print(f"ERROR: fixture diff not found: {diff_path}", file=sys.stderr)
        return 1

    print(f"Consistency eval (single-diff mode): {runs} runs/model, "
          f"models={list(models)}, agents={list(agent_names)}, "
          f"fixture={diff_path.name}, arms={list(arms)}, jaccard>={jaccard_threshold}")
    print(f"(cost ~= {len(arms)} arms x {runs} x {len(agent_names)} x {len(models)} = "
          f"{len(arms) * runs * len(agent_names) * len(models)} billed agent calls, "
          f"plus one judge call per run with findings if the judge arm is active)\n")

    dr = await _run_one_diff(diff_path, runs, agent_names, arms, models,
                             REPO_ROOT, jaccard_threshold)
    _print_diff_report(dr)

    any_usable = any(mr.runs_ok >= 2 for mr in dr.model_reports)
    if not any_usable:
        print("\nERROR: no model/arm produced >=2 successful runs; cannot report "
              "stability.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
