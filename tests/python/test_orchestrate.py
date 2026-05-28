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

    def resolve_stale(self) -> StaleResult:
        self.last_call_order.append("resolve_stale")
        self.stale_calls += 1
        return StaleResult()

    def advance_sha_watermark(self, new_sha: str) -> bool:
        self.last_call_order.append("advance_sha_watermark")
        return True

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
        def resolve_stale(self) -> StaleResult:
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

    def _renderer(successes: object, sarif_elapsed_s: object) -> str:
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

    def _bad_renderer(successes: object, sarif_elapsed_s: object) -> str:
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
