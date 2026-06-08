#!/usr/bin/env python3
"""Context enrichment evaluation harness (issue #391).

Runs the Python review engine twice per PR diff (enrichment OFF vs ON),
captures findings and token usage, and writes a comparison report to
memory-bank/context-enrichment-eval-YYYY-MM-DD.md.

Usage:
    cd /path/to/ai-pr-review-eval-enrichment
    ANTHROPIC_API_KEY=<key> python memory-bank/eval_context_enrichment.py

The script uses ANTHROPIC_API_TEST_KEY if ANTHROPIC_API_KEY is unset.

Corpus: PRs 443, 445, 447 fetched live via `gh pr diff`.
Model:  claude-haiku-4-5 (fast/cheap for evaluation; set EVAL_MODEL to override).
Mode:   quick (all Tier-1 agents + gates; no Tier-2 full-mode agents).
        Set EVAL_MODE=full to run all agents (more cost, more signal).
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path

# ---------------------------------------------------------------------------
# Bootstrap: ensure ANTHROPIC_API_KEY and repo root on sys.path
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

if not os.environ.get("ANTHROPIC_API_KEY"):
    test_key = os.environ.get("ANTHROPIC_API_TEST_KEY", "")
    if not test_key:
        sys.exit("ERROR: set ANTHROPIC_API_KEY or ANTHROPIC_API_TEST_KEY")
    os.environ["ANTHROPIC_API_KEY"] = test_key

# ---------------------------------------------------------------------------
# Imports from the engine (after path fixup)
# ---------------------------------------------------------------------------

from ai_pr_review.agents.dispatch import DispatchContext  # noqa: E402
from ai_pr_review.agents.gates import evaluate_gates, filter_agents  # noqa: E402
from ai_pr_review.agents.roster import AGENTS  # noqa: E402
from ai_pr_review.findings.models import Finding  # noqa: E402
from ai_pr_review.llm.client import call_llm  # noqa: E402
from ai_pr_review.llm.base import LLMRequest, LLMResponse  # noqa: E402
from ai_pr_review.manifest import build_changed_files  # noqa: E402
from ai_pr_review.orchestrate import OrchestrationConfig, ReviewResult, run_review  # noqa: E402
from ai_pr_review.vcs.protocol import (  # noqa: E402
    DiffContext,
    FindingsResult,
    StaleResult,
    SummaryResult,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CORPUS_PRS = [443, 445, 447]
MODEL = os.environ.get("EVAL_MODEL", "claude-haiku-4-5-20251001")
EVAL_MODE = os.environ.get("EVAL_MODE", "quick")
PROVIDER = "anthropic"

# ---------------------------------------------------------------------------
# Null VcsProvider (no-op all posting)
# ---------------------------------------------------------------------------

class NullProvider:
    """No-op VcsProvider: records calls but never touches any API."""

    def get_last_reviewed_sha(self) -> str | None:
        return None

    def get_summary_body(self) -> str | None:
        return None

    def post_summary(self, summary_body: str, head_sha: str) -> SummaryResult:
        return SummaryResult(comment_id=None, created=False, updated=False)

    def post_findings(self, findings, diff, *, event, failed_agents=(), token_table="",
                      agent_prompt="", max_inline=25, enable_suggestions=True) -> FindingsResult:
        return FindingsResult(
            review_id=None,
            inline_posted=0,
            body_findings=0,
            event=event,
        )

    def resolve_stale(self) -> StaleResult:
        return StaleResult()

    def advance_sha_watermark(self, new_sha: str) -> bool:
        return False

    def post_skip_comment(self, reason: str) -> SummaryResult:
        return SummaryResult(comment_id=None, created=False, updated=False)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class RunResult:
    pr_number: int
    enrichment: bool
    elapsed_s: float
    finding_count: int
    findings: list[Finding]
    context_tokens_used: int
    total_input_tokens: int
    total_output_tokens: int
    failed_agents: list[str]
    agent_count: int


@dataclass
class FindingKey:
    file: str
    line: int | None
    finding: str

    def __hash__(self) -> int:
        return hash((self.file, self.line, self.finding))

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, FindingKey):
            return NotImplemented
        return (self.file, self.line, self.finding) == (other.file, other.line, other.finding)


# ---------------------------------------------------------------------------
# Core: run one review pass
# ---------------------------------------------------------------------------

async def run_one(diff_text: str, pr_number: int, enrichment: bool) -> RunResult:
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".diff", prefix=f"eval_pr{pr_number}_",
        delete=False, encoding="utf-8"
    ) as f:
        f.write(diff_text)
        diff_path = Path(f.name)

    try:
        diff_ctx = DiffContext(diff_text=diff_text, head_sha="eval000deadbeef")

        # Extract changed file paths from the diff "+++ b/..." header lines
        changed_files = [
            ln[6:]
            for ln in diff_text.splitlines()
            if ln.startswith("+++ b/")
        ]

        dispatch_ctx = DispatchContext(
            script_dir=REPO_ROOT,
            mode=EVAL_MODE,
            diff_path=diff_path,
            provider=PROVIDER,
            standard_model=MODEL,
            premium_model=MODEL,
            enable_context_enrichment=enrichment,
            context_max_tokens=8192,
            context_lookup_lines=8,
            repo_root=REPO_ROOT,
            changed_files=changed_files,
        )

        # Select agents: filter by mode, then gate-evaluate against diff
        mode_agents = [a for a in AGENTS if not a.separately_dispatched
                       and (not a.full_mode_only or EVAL_MODE == "full")]
        gate_changed_files = build_changed_files([])
        gate_results = evaluate_gates(diff_text, gate_changed_files, os.environ)
        selected = list(filter_agents(mode_agents, gate_results))

        provider = NullProvider()

        async def _llm_call(req: LLMRequest) -> LLMResponse:
            return await call_llm(req, PROVIDER)

        orch = OrchestrationConfig(
            mode=EVAL_MODE,  # type: ignore[arg-type]
            confidence_threshold=75,
            max_inline=25,
        )

        t0 = time.monotonic()
        result: ReviewResult = await run_review(
            diff=diff_ctx,
            summary_text="",
            agents=selected,
            llm_call=_llm_call,
            dispatch_context=dispatch_ctx,
            provider=provider,
            config=orch,
        )
        elapsed = time.monotonic() - t0

        ctx_tokens = max(
            (ar.context_tokens_used for ar in result.agent_results),
            default=0,
        )
        total_input = sum(
            ar.token_log.input for ar in result.agent_results if ar.token_log
        )
        total_output = sum(
            ar.token_log.output for ar in result.agent_results if ar.token_log
        )

        return RunResult(
            pr_number=pr_number,
            enrichment=enrichment,
            elapsed_s=elapsed,
            finding_count=len(result.findings),
            findings=result.findings,
            context_tokens_used=ctx_tokens,
            total_input_tokens=total_input,
            total_output_tokens=total_output,
            failed_agents=[f.name for f in result.failed_agents],
            agent_count=len(selected),
        )
    finally:
        diff_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Diff fetching
# ---------------------------------------------------------------------------

def fetch_diff(pr_number: int) -> str:
    result = subprocess.run(
        ["gh", "pr", "diff", str(pr_number)],
        capture_output=True, text=True, check=True,
        cwd=str(REPO_ROOT),
    )
    return result.stdout


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------

def _severity_breakdown(findings: list[Finding]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for f in findings:
        sev = (f.severity or "unknown").lower()
        counts[sev] = counts.get(sev, 0) + 1
    return counts


def _finding_keys(findings: list[Finding]) -> set[FindingKey]:
    return {
        FindingKey(
            file=f.file or "",
            line=f.line,
            finding=f.finding[:80],
        )
        for f in findings
    }


def build_report(results: list[RunResult]) -> str:
    today = date.today().isoformat()
    lines = [
        f"# Context Enrichment Evaluation — {today}",
        "",
        f"**Issue:** [#391](https://github.com/tag1consulting/ai-pr-review/issues/391)",
        f"**Model:** `{MODEL}`  **Mode:** `{EVAL_MODE}`  **Corpus:** PRs {', '.join(f'#{p}' for p in CORPUS_PRS)}",
        "",
        "## Summary",
        "",
        "| PR | Off findings | On findings | Delta | Off input tok | On input tok | Context tok | Off latency | On latency |",
        "|---|---|---|---|---|---|---|---|---|",
    ]

    off_map = {r.pr_number: r for r in results if not r.enrichment}
    on_map = {r.pr_number: r for r in results if r.enrichment}

    total_off_findings = 0
    total_on_findings = 0
    total_off_input = 0
    total_on_input = 0
    total_ctx_tokens = 0

    for pr in CORPUS_PRS:
        off = off_map.get(pr)
        on = on_map.get(pr)
        if not off or not on:
            lines.append(f"| #{pr} | — | — | — | — | — | — | — | — |")
            continue
        delta = on.finding_count - off.finding_count
        delta_str = f"+{delta}" if delta > 0 else str(delta)
        lines.append(
            f"| #{pr} | {off.finding_count} | {on.finding_count} | {delta_str} "
            f"| {off.total_input_tokens:,} | {on.total_input_tokens:,} "
            f"| {on.context_tokens_used:,} "
            f"| {off.elapsed_s:.1f}s | {on.elapsed_s:.1f}s |"
        )
        total_off_findings += off.finding_count
        total_on_findings += on.finding_count
        total_off_input += off.total_input_tokens
        total_on_input += on.total_input_tokens
        total_ctx_tokens += on.context_tokens_used

    input_delta_pct = (
        ((total_on_input - total_off_input) / total_off_input * 100)
        if total_off_input else 0
    )
    lines += [
        f"| **Total** | **{total_off_findings}** | **{total_on_findings}** "
        f"| **{total_on_findings - total_off_findings:+d}** "
        f"| **{total_off_input:,}** | **{total_on_input:,}** "
        f"| **{total_ctx_tokens:,}** | — | — |",
        "",
        f"Context token overhead: **{input_delta_pct:+.1f}%** input tokens ({total_ctx_tokens:,} context tokens across all runs)",
        "",
    ]

    # Per-PR detail
    lines += ["## Per-PR Detail", ""]
    for pr in CORPUS_PRS:
        off = off_map.get(pr)
        on = on_map.get(pr)
        if not off or not on:
            lines.append(f"### PR #{pr} — data unavailable\n")
            continue

        diff_size = "unknown"
        lines += [
            f"### PR #{pr}",
            "",
            f"Agents selected: {off.agent_count}  |  Context tokens used (on): {on.context_tokens_used:,}",
            "",
        ]

        # Severity breakdown
        off_sev = _severity_breakdown(off.findings)
        on_sev = _severity_breakdown(on.findings)
        all_sevs = sorted(set(list(off_sev.keys()) + list(on_sev.keys())))
        if all_sevs:
            lines += [
                "**Severity breakdown:**",
                "",
                "| Severity | Off | On |",
                "|---|---|---|",
            ]
            for sev in all_sevs:
                lines.append(f"| {sev} | {off_sev.get(sev, 0)} | {on_sev.get(sev, 0)} |")
            lines.append("")

        # Findings unique to each run
        off_keys = _finding_keys(off.findings)
        on_keys = _finding_keys(on.findings)
        only_off = off_keys - on_keys
        only_on = on_keys - off_keys
        both = off_keys & on_keys

        lines.append(f"**Overlap:** {len(both)} findings in both runs, {len(only_off)} only-off, {len(only_on)} only-on")
        lines.append("")

        if only_on:
            lines += ["**New findings with enrichment ON:**", ""]
            on_by_key = {FindingKey(f.file or "", f.line, f.finding[:80]): f for f in on.findings}
            for key in sorted(only_on, key=lambda k: (k.file, k.line or 0)):
                f = on_by_key.get(key)
                if f:
                    sev = f.severity.upper()
                    loc = f"`{f.file}:{f.line}`" if f.file else "body"
                    lines.append(f"- [{sev}] {loc} — {f.finding[:120]}")
            lines.append("")

        if only_off:
            lines += ["**Findings dropped with enrichment ON:**", ""]
            off_by_key = {FindingKey(f.file or "", f.line, f.finding[:80]): f for f in off.findings}
            for key in sorted(only_off, key=lambda k: (k.file, k.line or 0)):
                f = off_by_key.get(key)
                if f:
                    sev = f.severity.upper()
                    loc = f"`{f.file}:{f.line}`" if f.file else "body"
                    lines.append(f"- [{sev}] {loc} — {f.finding[:120]}")
            lines.append("")

        if off.failed_agents or on.failed_agents:
            lines.append(f"**Failed agents** — off: {off.failed_agents or 'none'}  on: {on.failed_agents or 'none'}")
            lines.append("")

    # Graceful-degradation confirmation
    lines += [
        "## Graceful Degradation — Non-Container Consumers",
        "",
        "When `tree-sitter-language-pack` is missing, `extract_symbol_refs()` returns `[]` ",
        "and enrichment silently no-ops (no error, no partial context). The regex fallback ",
        "`extract_symbol_refs_fallback()` exists in `context/treesitter.py` but is **not** ",
        "wired into `dispatch.py`'s `_build_user_message()` — consumers without tree-sitter ",
        "get no symbol context at all. This is an acceptable degradation path (no-op is safe), ",
        "but means non-container consumers on default-on would silently receive the same review ",
        "quality as default-off.",
        "",
        "ripgrep (`rg`) absence is also handled gracefully: `lookup_definitions()` returns `[]` ",
        "and logs a WARNING. Full degradation chain is fail-soft at every layer.",
        "",
    ]

    # Recommendation placeholder
    lines += [
        "## Recommendation",
        "",
        "_(To be filled in after manual review of findings above.)_",
        "",
        "Criteria from issue #391:",
        "- [ ] Measured review-quality comparison shows accuracy gain",
        "- [ ] Token/cost delta is acceptable for all consumers",
        "- [ ] Graceful degradation confirmed for non-container consumers",
        "- [ ] Latency overhead is acceptable",
        "",
        "Decision: **[ flip / don't flip / flip-only-for-container ]**",
        "",
        "Rationale: _(fill in)_",
        "",
    ]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    print(f"Context enrichment evaluation — model={MODEL} mode={EVAL_MODE}", flush=True)
    print(f"Corpus: PRs {CORPUS_PRS}", flush=True)
    print()

    results: list[RunResult] = []

    for pr_number in CORPUS_PRS:
        print(f"[PR #{pr_number}] Fetching diff...", flush=True)
        try:
            diff_text = fetch_diff(pr_number)
        except subprocess.CalledProcessError as exc:
            print(f"[PR #{pr_number}] ERROR fetching diff: {exc}", flush=True)
            continue

        diff_lines = len(diff_text.splitlines())
        print(f"[PR #{pr_number}] diff: {diff_lines} lines", flush=True)

        for enrichment in (False, True):
            label = "ON " if enrichment else "OFF"
            print(f"[PR #{pr_number}] enrichment {label} — running...", flush=True, end="")
            t0 = time.monotonic()
            try:
                run = await run_one(diff_text, pr_number, enrichment)
                elapsed = time.monotonic() - t0
                print(
                    f" {elapsed:.1f}s  findings={run.finding_count}  "
                    f"input_tok={run.total_input_tokens:,}  "
                    f"ctx_tok={run.context_tokens_used:,}",
                    flush=True,
                )
                results.append(run)
            except Exception as exc:
                print(f" ERROR: {exc}", flush=True)
                import traceback; traceback.print_exc()

        print()

    if not results:
        print("No results collected — check errors above.")
        return

    report = build_report(results)
    out_path = REPO_ROOT / "memory-bank" / f"context-enrichment-eval-{date.today().isoformat()}.md"
    out_path.write_text(report, encoding="utf-8")
    print(f"Report written to: {out_path}")
    print()
    # Print summary table to console too
    for line in report.splitlines():
        if line.startswith("| ") or line.startswith("Context token"):
            print(line)


if __name__ == "__main__":
    asyncio.run(main())
