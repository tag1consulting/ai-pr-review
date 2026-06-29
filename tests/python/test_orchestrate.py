"""Tests for ai_pr_review.orchestrate.run_review — happy path + skip path."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anyio

from ai_pr_review.agents.dispatch import DispatchContext
from ai_pr_review.agents.roster import AgentSpec
from ai_pr_review.findings.models import Finding
from ai_pr_review.llm.base import LLMRequest, LLMResponse
from ai_pr_review.orchestrate import OrchestrationConfig, run_review
from ai_pr_review.vcs.protocol import (
    DiffContext,
    FindingsResult,
    StaleResult,
    SummaryResult,
)

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class _FakeProvider:
    """Records every method call; returns canned successes by default."""

    summary_ok: bool = True
    findings_ok: bool = True
    summary_calls: list[tuple[str, str]] = field(default_factory=list)
    findings_calls: list[dict[str, Any]] = field(default_factory=list)
    stale_calls: int = 0
    skip_calls: list[str] = field(default_factory=list)
    last_call_order: list[str] = field(default_factory=list)

    def get_last_reviewed_sha(self) -> str | None:
        return None

    def post_summary(self, summary_body: str, head_sha: str) -> SummaryResult:
        self.last_call_order.append("post_summary")
        self.summary_calls.append((summary_body, head_sha))
        if not self.summary_ok:
            return SummaryResult(
                comment_id=None, created=False, updated=False, error="boom"
            )
        return SummaryResult(comment_id=42, created=True, updated=False)

    def post_findings(
        self,
        findings: Sequence[Finding],
        diff: DiffContext,
        *,
        event: str,
        failed_agents: Sequence[str] = (),
        token_table: str = "",
        agent_prompt: str = "",
        max_inline: int = 25,
        enable_suggestions: bool = True,
    ) -> FindingsResult:
        self.last_call_order.append("post_findings")
        self.findings_calls.append(
            {
                "findings": list(findings),
                "event": event,
                "failed_agents": list(failed_agents),
                "token_table": token_table,
            }
        )
        if not self.findings_ok:
            return FindingsResult(
                review_id=None,
                inline_posted=0,
                body_findings=len(findings),
                event=event,  # type: ignore[arg-type]
                error="findings boom",
            )
        return FindingsResult(
            review_id=99,
            inline_posted=len(findings),
            body_findings=0,
            event=event,  # type: ignore[arg-type]
        )

    def resolve_stale(self, current_review_id: int | None = None) -> StaleResult:
        self.last_call_order.append("resolve_stale")
        self.stale_calls += 1
        return StaleResult()

    advance_sha_watermark_retval: bool = True

    last_watermark_sha: str = ""

    def advance_sha_watermark(self, new_sha: str) -> bool:
        self.last_call_order.append("advance_sha_watermark")
        self.last_watermark_sha = new_sha
        return self.advance_sha_watermark_retval

    def post_skip_comment(self, reason: str) -> SummaryResult:
        self.last_call_order.append("post_skip_comment")
        self.skip_calls.append(reason)
        return SummaryResult(comment_id=1, created=True, updated=False)


def _findings_block(findings: list[dict[str, Any]]) -> str:
    """Render findings as a json-findings code-fence block (extract.py format)."""
    import json

    return "Some narrative text\n\n```json-findings\n" + json.dumps(findings) + "\n```"


def _llm_call_factory(returns: dict[str, str]):
    """Build an LLMCall that maps agent prompt path → response text."""

    async def _call(req: LLMRequest) -> LLMResponse:
        # Identify the agent by a marker we embed in the system prompt
        for key, output in returns.items():
            if key in req.system_prompt:
                return LLMResponse(
                    text=output,
                    input_tokens=10,
                    output_tokens=20,
                    stop_reason="end_turn",
                )
        return LLMResponse(
            text="```json-findings\n[]\n```",
            input_tokens=10,
            output_tokens=20,
            stop_reason="end_turn",
        )

    return _call


def _make_dispatch_context(tmp_path: Path, diff_text: str = "") -> DispatchContext:
    diff_path = tmp_path / "diff.txt"
    diff_path.write_text(diff_text)
    return DispatchContext(
        script_dir=tmp_path,
        mode="full",
        diff_path=diff_path,
        provider="anthropic",
        standard_model="claude-sonnet-4-6",
        premium_model="claude-opus-4-7",
    )


# ---------------------------------------------------------------------------
# Skip path
# ---------------------------------------------------------------------------


def test_skip_path_posts_skip_comment_and_returns(tmp_path: Path) -> None:
    provider = _FakeProvider()
    ctx = _make_dispatch_context(tmp_path)

    async def _run() -> None:
        result = await run_review(
            diff=DiffContext(diff_text="", head_sha="abc1234567"),
            summary_text="",
            agents=[],
            llm_call=_llm_call_factory({}),
            dispatch_context=ctx,
            provider=provider,
            skip_reason="no changed files",
        )
        assert result.skipped is True
        assert result.skip_reason == "no changed files"
        assert result.ok is True
        # Posted only the skip; no summary/findings/stale
        assert provider.skip_calls == ["no changed files"]
        assert provider.summary_calls == []
        assert provider.findings_calls == []
        assert provider.stale_calls == 0

    anyio.run(_run)


def test_skip_path_no_agent_dispatch(tmp_path: Path) -> None:
    """Even if agents is non-empty, skip short-circuits before dispatch."""
    provider = _FakeProvider()
    ctx = _make_dispatch_context(tmp_path)
    spec = AgentSpec(
        name="blind-hunter",
        prompt_path="prompts/blind-hunter.md",
        tier=1,
        conditional_trigger=None,
        max_output_tokens=4096,
        full_mode_only=False,
        context_enrichment_eligible=False,
    )

    async def _run() -> None:
        result = await run_review(
            diff=DiffContext(diff_text="", head_sha="abc1234567"),
            summary_text="",
            agents=[spec],
            llm_call=_llm_call_factory({}),
            dispatch_context=ctx,
            provider=provider,
            skip_reason="diff too large",
        )
        assert result.skipped
        assert provider.findings_calls == []

    anyio.run(_run)


# ---------------------------------------------------------------------------
# Happy path with no agents, no findings
# ---------------------------------------------------------------------------


def test_happy_path_no_agents_no_findings_posts_in_order(tmp_path: Path) -> None:
    provider = _FakeProvider()
    ctx = _make_dispatch_context(tmp_path)

    async def _run() -> None:
        result = await run_review(
            diff=DiffContext(diff_text="diff text", head_sha="abc1234567"),
            summary_text="## Some summary",
            agents=[],
            llm_call=_llm_call_factory({}),
            dispatch_context=ctx,
            provider=provider,
        )
        assert result.skipped is False
        assert result.ok is True
        assert result.findings == []
        assert result.outcome.event == "APPROVE"
        assert result.outcome.may_approve is True

        # AC5 ordering: summary BEFORE findings BEFORE stale
        order = provider.last_call_order
        assert order == ["post_summary", "post_findings", "resolve_stale"]
        assert provider.summary_calls == [("## Some summary", "abc1234567")]
        # post_findings is called even with no findings — it renders an
        # APPROVE body. Confirm event passed through.
        assert provider.findings_calls[0]["event"] == "APPROVE"

    anyio.run(_run)


def test_failed_summary_post_skips_findings_and_stale(tmp_path: Path) -> None:
    provider = _FakeProvider(summary_ok=False)
    ctx = _make_dispatch_context(tmp_path)

    async def _run() -> None:
        result = await run_review(
            diff=DiffContext(diff_text="", head_sha="abc1234567"),
            summary_text="## Summary",
            agents=[],
            llm_call=_llm_call_factory({}),
            dispatch_context=ctx,
            provider=provider,
        )
        assert result.ok is False
        assert result.summary is not None and result.summary.error == "boom"
        # provider.post_findings must NOT have been called
        assert provider.findings_calls == []
        assert provider.stale_calls == 0
        assert provider.last_call_order == ["post_summary"]
        # findings_result must be set (not None) so callers can distinguish
        # "findings skipped due to summary failure" from "no findings"
        assert result.findings_post is not None
        assert result.findings_post.error is not None
        assert "skipped" in result.findings_post.error

    anyio.run(_run)


def test_failed_findings_post_blocks_stale(tmp_path: Path) -> None:
    provider = _FakeProvider(findings_ok=False)
    ctx = _make_dispatch_context(tmp_path)

    async def _run() -> None:
        result = await run_review(
            diff=DiffContext(diff_text="", head_sha="abc1234567"),
            summary_text="## Summary",
            agents=[],
            llm_call=_llm_call_factory({}),
            dispatch_context=ctx,
            provider=provider,
        )
        assert result.ok is False
        # Summary did succeed, findings failed → stale must NOT run
        assert provider.stale_calls == 0
        assert provider.last_call_order == ["post_summary", "post_findings"]

    anyio.run(_run)


def test_failed_findings_post_logs_warning_before_skip(
    tmp_path: Path, caplog: Any
) -> None:
    """Finding #2: when post_findings fails, a warning must be logged before skipping stale."""
    import logging

    provider = _FakeProvider(findings_ok=False)
    ctx = _make_dispatch_context(tmp_path)

    async def _run() -> None:
        with caplog.at_level(logging.WARNING, logger="ai_pr_review.orchestrate"):
            await run_review(
                diff=DiffContext(diff_text="", head_sha="abc1234567"),
                summary_text="## Summary",
                agents=[],
                llm_call=_llm_call_factory({}),
                dispatch_context=ctx,
                provider=provider,
            )

    anyio.run(_run)
    assert any("post_findings failed" in r.message for r in caplog.records)


def test_orchestration_config_defaults() -> None:
    cfg = OrchestrationConfig()
    assert cfg.mode == "full"
    assert cfg.confidence_threshold == 75
    assert cfg.max_inline == 25
    assert cfg.enable_suggestions is True
    assert cfg.semaphore_size == 4
    assert cfg.suppression_rules == ()


def test_failed_summary_logs_error(tmp_path: Path, caplog: Any) -> None:
    """B2: post_summary failure must be logged before skipping findings."""
    import logging

    provider = _FakeProvider(summary_ok=False)
    ctx = _make_dispatch_context(tmp_path)

    async def _run() -> None:
        with caplog.at_level(logging.ERROR, logger="ai_pr_review.orchestrate"):
            await run_review(
                diff=DiffContext(diff_text="", head_sha="abc1234567"),
                summary_text="## Summary",
                agents=[],
                llm_call=_llm_call_factory({}),
                dispatch_context=ctx,
                provider=provider,
            )

    anyio.run(_run)
    assert any("post_summary failed" in r.message for r in caplog.records)
    assert any("boom" in r.message for r in caplog.records)


def test_retry_exhausted_in_stale_is_captured_not_propagated(tmp_path: Path) -> None:
    """B6: RetryExhaustedError from resolve_stale must not propagate; captured in StaleResult."""
    from ai_pr_review.vcs.http import RetryExhaustedError

    @dataclass
    class _RaisingProvider(_FakeProvider):
        def resolve_stale(self, current_review_id: int | None = None) -> StaleResult:
            self.stale_calls += 1
            raise RetryExhaustedError("network down after 3 attempts")

    provider = _RaisingProvider()
    ctx = _make_dispatch_context(tmp_path)

    async def _run() -> None:
        result = await run_review(
            diff=DiffContext(diff_text="", head_sha="abc1234567"),
            summary_text="## Summary",
            agents=[],
            llm_call=_llm_call_factory({}),
            dispatch_context=ctx,
            provider=provider,
        )
        # The review itself must not fail; stale error is in StaleResult
        assert result.summary is not None and result.summary.ok
        assert result.stale is not None
        assert len(result.stale.errors) == 1
        assert "network down" in result.stale.errors[0]

    anyio.run(_run)


def test_retry_exhausted_in_post_summary_returns_error_not_raises(tmp_path: Path) -> None:
    """RetryExhaustedError from post_summary must not propagate; captured in SummaryResult."""
    from ai_pr_review.vcs.http import RetryExhaustedError

    @dataclass
    class _RaisingProvider(_FakeProvider):
        def post_summary(self, summary_body: str, head_sha: str) -> SummaryResult:
            raise RetryExhaustedError("summary net down after 3 attempts")

    provider = _RaisingProvider()
    ctx = _make_dispatch_context(tmp_path)

    async def _run() -> None:
        result = await run_review(
            diff=DiffContext(diff_text="", head_sha="abc1234567"),
            summary_text="## Summary",
            agents=[],
            llm_call=_llm_call_factory({}),
            dispatch_context=ctx,
            provider=provider,
        )
        assert result.ok is False
        assert result.summary is not None and result.summary.error is not None
        assert "retry exhausted" in result.summary.error
        assert provider.findings_calls == []

    anyio.run(_run)


def test_retry_exhausted_in_post_findings_returns_error_not_raises(tmp_path: Path) -> None:
    """RetryExhaustedError from post_findings must not propagate; captured in FindingsResult."""
    from ai_pr_review.vcs.http import RetryExhaustedError

    @dataclass
    class _RaisingProvider(_FakeProvider):
        def post_findings(  # type: ignore[override]
            self,
            findings: Any,
            diff: Any,
            *,
            event: str,
            **kwargs: Any,
        ) -> FindingsResult:
            raise RetryExhaustedError("findings net down after 3 attempts")

    provider = _RaisingProvider()
    ctx = _make_dispatch_context(tmp_path)

    async def _run() -> None:
        result = await run_review(
            diff=DiffContext(diff_text="", head_sha="abc1234567"),
            summary_text="## Summary",
            agents=[],
            llm_call=_llm_call_factory({}),
            dispatch_context=ctx,
            provider=provider,
        )
        assert result.ok is False
        assert result.findings_post is not None
        assert result.findings_post.error is not None
        assert "retry exhausted" in result.findings_post.error
        # stale must NOT run after findings failure
        assert provider.stale_calls == 0


# ---------------------------------------------------------------------------
# token_table_renderer wiring
# ---------------------------------------------------------------------------


def test_token_table_renderer_forwards_to_post_findings(tmp_path: Path) -> None:
    """token_table_renderer output must be forwarded to provider.post_findings."""
    provider = _FakeProvider()
    ctx = _make_dispatch_context(tmp_path)

    def _renderer(
        successes: object, sarif_elapsed_s: object,
        judge_in: int, judge_out: int, judge_cw: int, judge_cr: int, judge_model: str,
    ) -> str:
        return "<details>test-token-table</details>"

    async def _run() -> None:
        await run_review(
            diff=DiffContext(diff_text="", head_sha="abc1234567"),
            summary_text="## Summary",
            agents=[],
            llm_call=_llm_call_factory({}),
            dispatch_context=ctx,
            provider=provider,
            token_table_renderer=_renderer,  # type: ignore[arg-type]
        )
        assert provider.findings_calls, "post_findings must have been called"
        assert provider.findings_calls[0]["token_table"] == "<details>test-token-table</details>"

    anyio.run(_run)


def test_token_table_renderer_exception_is_failsoft(tmp_path: Path) -> None:
    """A renderer that raises must not abort the review; post_findings gets token_table=''."""
    provider = _FakeProvider()
    ctx = _make_dispatch_context(tmp_path)

    def _bad_renderer(
        successes: object, sarif_elapsed_s: object,
        judge_in: int, judge_out: int, judge_cw: int, judge_cr: int, judge_model: str,
    ) -> str:
        raise RuntimeError("renderer exploded")

    async def _run() -> None:
        result = await run_review(
            diff=DiffContext(diff_text="", head_sha="abc1234567"),
            summary_text="## Summary",
            agents=[],
            llm_call=_llm_call_factory({}),
            dispatch_context=ctx,
            provider=provider,
            token_table_renderer=_bad_renderer,  # type: ignore[arg-type]
        )
        assert result.ok is True
        assert provider.findings_calls, "post_findings must still have been called"
        assert provider.findings_calls[0]["token_table"] == ""

    anyio.run(_run)


# ---------------------------------------------------------------------------
# Incremental run path (summary_text empty, no skip_reason)
# ---------------------------------------------------------------------------


def test_incremental_run_calls_advance_sha_watermark(tmp_path: Path) -> None:
    """When summary_text is empty and there is no skip_reason, run_review must
    call advance_sha_watermark instead of post_summary, passing the correct SHA."""
    provider = _FakeProvider()
    ctx = _make_dispatch_context(tmp_path)

    async def _run() -> None:
        result = await run_review(
            diff=DiffContext(diff_text="", head_sha="abc1234567"),
            summary_text="",
            agents=[],
            llm_call=_llm_call_factory({}),
            dispatch_context=ctx,
            provider=provider,
        )
        assert result.ok is True
        assert "advance_sha_watermark" in provider.last_call_order
        assert provider.last_watermark_sha == "abc1234567", (
            f"advance_sha_watermark must receive head_sha; got {provider.last_watermark_sha!r}"
        )
        assert provider.summary_calls == [], "post_summary must NOT be called on incremental run"
        assert provider.findings_calls, "post_findings must still be called on incremental run"

    anyio.run(_run)


def test_incremental_run_skips_watermark_when_post_findings_fails(
    tmp_path: Path,
) -> None:
    """#493: on an incremental run, if post_findings returns a failed result,
    the SHA watermark must NOT advance — otherwise the next incremental run
    silently skips findings on the in-flight commits.
    """
    provider = _FakeProvider(findings_ok=False)
    ctx = _make_dispatch_context(tmp_path)

    async def _run() -> None:
        result = await run_review(
            diff=DiffContext(diff_text="", head_sha="abc1234567"),
            summary_text="",
            agents=[],
            llm_call=_llm_call_factory({}),
            dispatch_context=ctx,
            provider=provider,
        )
        assert result.ok is False, "run is not ok when post_findings fails"
        assert "post_findings" in provider.last_call_order
        assert "advance_sha_watermark" not in provider.last_call_order, (
            "watermark must not advance when post_findings failed; "
            f"call_order={provider.last_call_order!r}"
        )
        assert provider.last_watermark_sha == "", (
            "watermark SHA must remain unset; "
            f"got {provider.last_watermark_sha!r}"
        )

    anyio.run(_run)


def test_incremental_run_skips_watermark_when_post_findings_raises(
    tmp_path: Path,
) -> None:
    """#493: same guarantee for the RetryExhaustedError path — if post_findings
    raises rather than returning a failed result, the watermark still must not
    advance.
    """
    from ai_pr_review.vcs.http import RetryExhaustedError

    class _FailingFindings(_FakeProvider):
        def post_findings(self, *args: Any, **kwargs: Any) -> FindingsResult:  # type: ignore[override]
            self.last_call_order.append("post_findings")
            raise RetryExhaustedError("findings net down after 3 attempts")

    provider = _FailingFindings()
    ctx = _make_dispatch_context(tmp_path)

    async def _run() -> None:
        result = await run_review(
            diff=DiffContext(diff_text="", head_sha="abc1234567"),
            summary_text="",
            agents=[],
            llm_call=_llm_call_factory({}),
            dispatch_context=ctx,
            provider=provider,
        )
        assert result.ok is False
        assert "post_findings" in provider.last_call_order
        assert "advance_sha_watermark" not in provider.last_call_order, (
            "watermark must not advance when post_findings raised; "
            f"call_order={provider.last_call_order!r}"
        )

    anyio.run(_run)


def test_incremental_run_watermark_advances_after_findings(tmp_path: Path) -> None:
    """#493: the watermark advance must happen AFTER post_findings, not before.
    Locks in the ordering so a future refactor cannot regress to the racy form.
    """
    provider = _FakeProvider()
    ctx = _make_dispatch_context(tmp_path)

    async def _run() -> None:
        await run_review(
            diff=DiffContext(diff_text="", head_sha="abc1234567"),
            summary_text="",
            agents=[],
            llm_call=_llm_call_factory({}),
            dispatch_context=ctx,
            provider=provider,
        )
        # Must call both, in the correct order.
        assert "post_findings" in provider.last_call_order
        assert "advance_sha_watermark" in provider.last_call_order
        post_idx = provider.last_call_order.index("post_findings")
        advance_idx = provider.last_call_order.index("advance_sha_watermark")
        assert post_idx < advance_idx, (
            "advance_sha_watermark must run AFTER post_findings; "
            f"call_order={provider.last_call_order!r}"
        )

    anyio.run(_run)


def test_incremental_run_advance_sha_watermark_false_logs_warning(
    tmp_path: Path,
) -> None:
    """When advance_sha_watermark returns False, a warning must be logged."""
    import io
    import logging

    provider = _FakeProvider()
    provider.advance_sha_watermark_retval = False
    ctx = _make_dispatch_context(tmp_path)

    log_stream = io.StringIO()
    handler = logging.StreamHandler(log_stream)
    handler.setLevel(logging.WARNING)
    logger = logging.getLogger("ai_pr_review.orchestrate")
    logger.addHandler(handler)

    async def _run() -> None:
        await run_review(
            diff=DiffContext(diff_text="", head_sha="abc1234567"),
            summary_text="",
            agents=[],
            llm_call=_llm_call_factory({}),
            dispatch_context=ctx,
            provider=provider,
        )

    try:
        anyio.run(_run)
    finally:
        logger.removeHandler(handler)

    log_output = log_stream.getvalue()
    assert "advance_sha_watermark" in log_output, (
        f"Expected warning about advance_sha_watermark returning False; got: {log_output!r}"
    )
    assert "abc1234567" in log_output, (
        f"Warning must include the head_sha for correlation; got: {log_output!r}"
    )


# ---------------------------------------------------------------------------
# Judge pass (Story 7-3, #360 remainder)
# ---------------------------------------------------------------------------


def _make_agent_spec(name: str = "code-reviewer", prompt_name: str | None = None) -> AgentSpec:
    from ai_pr_review.agents.roster import get_agent
    return get_agent(name)


def _make_dispatch_context_with_prompts(tmp_path: Path, diff_text: str = "") -> DispatchContext:
    """Like _make_dispatch_context but also creates the minimal prompt files needed for agents."""
    diff_path = tmp_path / "diff.txt"
    diff_path.write_text(diff_text)
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir(exist_ok=True)
    for name in ("_governance", "_knowledge-cutoff", "_trailer-findings", "suggestion-addendum",
                 "code-reviewer", "pr-summarizer", "security-reviewer", "silent-failure-hunter",
                 "architecture-reviewer", "blind-hunter", "edge-case-hunter", "adversarial-general",
                 "issue-linker"):
        (prompts_dir / f"{name}.md").write_text(f"## {name}\n")
    return DispatchContext(
        script_dir=tmp_path,
        mode="full",
        diff_path=diff_path,
        provider="anthropic",
        standard_model="claude-sonnet-4-6",
        premium_model="claude-opus-4-7",
    )


def test_orchestrate_judge_on_downranks_weak_finding(tmp_path: Path) -> None:
    """Judge-on path: weak finding is downranked (lower confidence + out_of_diff),
    corroborated finding is kept unchanged. Both still reach post_findings."""
    import json

    provider = _FakeProvider()
    ctx = _make_dispatch_context_with_prompts(tmp_path)
    weak_finding = {
        "severity": "Medium",
        "confidence": 80,
        "file": "app.py",
        "line": 10,
        "finding": "Vague speculation",
        "remediation": "Consider reviewing",
        # source intentionally omitted; extract_findings stamps "code-reviewer"
    }
    strong_finding = {
        "severity": "High",
        "confidence": 90,
        "file": "db.py",
        "line": 42,
        "finding": "SQL injection via f-string",
        "remediation": "Use parameterized queries",
        # source intentionally omitted; extract_findings stamps "code-reviewer"
        # so that when merged with the semgrep analyzer_finding, sources become
        # ["code-reviewer", "semgrep"] and is_corroborated() returns True.
    }
    agent_output = _findings_block([weak_finding, strong_finding])

    judge_prompt_path = tmp_path / "finding-judge.md"
    judge_prompt_path.write_text("finding-quality judge")

    JUDGE_MARKER = "finding-quality judge"

    judge_verdicts = json.dumps({
        "verdicts": [
            {"id": 0, "verdict": "downrank", "reason": "vague"},
            {"id": 1, "verdict": "downrank", "reason": "would downrank"},
        ]
    })

    async def smart_llm(req: LLMRequest) -> LLMResponse:
        if JUDGE_MARKER in req.system_prompt:
            return LLMResponse(text=judge_verdicts, input_tokens=50, output_tokens=20, stop_reason="end_turn")
        return LLMResponse(text=agent_output, input_tokens=100, output_tokens=200, stop_reason="end_turn")

    # Make the strong_finding corroborated by seeding it as an extra_finding
    # from a static analyzer so merge._collapse_cluster sets corroborated=True.
    from ai_pr_review.findings.models import Finding
    analyzer_finding = Finding(
        severity="High",
        confidence=90,
        file="db.py",
        line=42,
        finding="SQL injection via f-string",
        source="semgrep",
        sources=["semgrep"],
        corroborated=False,
    )

    cfg = OrchestrationConfig(
        enable_judge_pass=True,
        judge_model="claude-test",
        judge_prompt_path=judge_prompt_path,
        extra_findings=(analyzer_finding,),
    )

    async def _run() -> None:
        # Use a minimal agent spec that will produce findings.
        from ai_pr_review.agents.roster import AGENTS
        agents = [a for a in AGENTS if a.name == "code-reviewer"]

        # Diff includes both changed lines so apply_diff_scope does not cap them.
        # app.py: line 10 is added; db.py: line 42 is added.
        # parse_added_lines() requires "diff --git" header for file detection.
        diff_text = (
            "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n"
            "@@ -8,3 +8,4 @@\n a\n b\n+evil(request.args['x'])\n c\n"
            "diff --git a/db.py b/db.py\n--- a/db.py\n+++ b/db.py\n"
            "@@ -40,3 +40,4 @@\n x\n a\n+query = f'SELECT'\n b\n"
        )
        result = await run_review(
            diff=DiffContext(diff_text=diff_text, head_sha="abc1234567"),
            summary_text="## Summary",
            agents=agents,
            llm_call=smart_llm,
            dispatch_context=ctx,
            provider=provider,
            config=cfg,
        )
        # Both findings must reach post_findings (judge never drops)
        assert result.findings is not None
        findings_by_file = {f.file: f for f in result.findings}
        # Weak finding must have been downranked (confidence lowered, routed to body)
        weak = findings_by_file.get("app.py")
        assert weak is not None, f"app.py finding missing; all findings: {result.findings}"
        assert weak.confidence == 80 - 15  # JUDGE_DOWNRANK_AMOUNT
        assert weak.out_of_diff is True
        # Corroborated finding must be kept unchanged (exempt from judge downranking)
        strong = findings_by_file.get("db.py")
        assert strong is not None, f"db.py finding missing; all findings: {result.findings}"
        assert strong.corroborated is True
        assert strong.out_of_diff is False

    anyio.run(_run)


def test_orchestrate_judge_off_leaves_findings_unchanged(tmp_path: Path) -> None:
    """Judge-off path: enable_judge_pass=False; findings reach post_findings unchanged."""

    provider = _FakeProvider()
    ctx = _make_dispatch_context_with_prompts(tmp_path)

    weak_finding = {
        "severity": "Medium",
        "confidence": 80,
        "file": "app.py",
        "line": 10,
        "finding": "Vague speculation",
        "remediation": "Consider reviewing",
        "source": "adversarial-general",
    }
    agent_output = _findings_block([weak_finding])

    judge_called = False

    async def smart_llm(req: LLMRequest) -> LLMResponse:
        nonlocal judge_called
        if "finding-quality judge" in req.system_prompt:
            judge_called = True
        return LLMResponse(text=agent_output, input_tokens=100, output_tokens=200, stop_reason="end_turn")

    cfg = OrchestrationConfig(enable_judge_pass=False)

    async def _run() -> None:
        from ai_pr_review.agents.roster import AGENTS
        agents = [a for a in AGENTS if a.name == "code-reviewer"]

        result = await run_review(
            diff=DiffContext(diff_text="diff text here", head_sha="abc1234567"),
            summary_text="## Summary",
            agents=agents,
            llm_call=smart_llm,
            dispatch_context=ctx,
            provider=provider,
            config=cfg,
        )
        # Judge must not have been called
        assert not judge_called
        # Finding must be unchanged
        assert len(result.findings) == 1
        assert result.findings[0].confidence == 80
        assert result.findings[0].out_of_diff is False

    anyio.run(_run)
