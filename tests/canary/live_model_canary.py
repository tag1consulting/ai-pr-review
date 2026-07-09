"""Live-API canary: exercises real model behavior against a genuinely
demanding diff, not a small representative one.

Background (#592): Claude Sonnet 5's adaptive thinking, on by default, was
able to consume an entire max_tokens budget on thinking alone, leaving no
room for a text response. Nothing in the model-onboarding process at the
time (unit tests with no network access, plus one live e2e run against a
310-line, 6-file diff) was demanding enough to trigger it. This script is
the tier of the #592 test plan meant to catch the *next* model-behavior
surprise: it runs the real dispatch path (real prompts, real
DispatchContext, real call_llm) against tests/canary/stress_diff.txt (the
actual PR #591 diff, ~1200 lines across 4 files, known to reliably demand
enough reasoning to matter) for every model this repo has a live API key
for, and asserts each call completes cleanly.

Not a pytest suite: this makes real, billed API calls and is intentionally
excluded from the default `pytest tests/python` run. Invoke directly:

    python tests/canary/live_model_canary.py

Exit code 0 on success (all models produced end_turn/text), 1 on any
failure. Intended for a scheduled GitHub Actions workflow
(.github/workflows/model-canary.yml), not per-PR CI.
"""

from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from ai_pr_review.agents.dispatch import DispatchContext, run_tier  # noqa: E402
from ai_pr_review.agents.roster import AGENTS  # noqa: E402
from ai_pr_review.llm.base import LLMRequest, LLMResponse  # noqa: E402
from ai_pr_review.llm.client import call_llm  # noqa: E402

STRESS_DIFF_PATH = Path(__file__).resolve().parent / "stress_diff.txt"

# The two agents #592 actually crashed on. Restricting the canary to these
# (rather than the full roster) keeps API cost bounded while still covering
# the agents with the largest max_output_tokens and the most demanding,
# analysis-heavy prompts -- the ones most likely to trigger a thinking-budget
# or similar model-behavior surprise on a future model.
TARGET_AGENT_NAMES = ("code-reviewer", "silent-failure-hunter")

# provider -> (env var holding the API key, standard model, premium model).
# Scoped to providers this repo can actually authenticate against in CI
# (secrets.AI_REVIEW_API_KEY is Anthropic-only as of this writing -- see
# .github/workflows/ai-review.yml). Extend this dict only alongside adding
# a real, live-tested key for the new provider; an entry with no working
# key is a false claim of coverage, not a canary.
PROVIDER_MODELS: dict[str, tuple[str, str, str]] = {
    "anthropic": ("ANTHROPIC_API_KEY", "claude-sonnet-5", "claude-opus-4-8"),
}


@dataclass
class CanaryResult:
    provider: str
    model: str
    agent: str
    ok: bool
    detail: str


async def _run_one(provider: str, model_id: str, agent_name: str) -> CanaryResult:
    async def llm_call(req: LLMRequest) -> LLMResponse:
        return await call_llm(req, provider)

    agents = [a for a in AGENTS if a.name == agent_name]
    if not agents:
        return CanaryResult(provider, model_id, agent_name, False, f"unknown agent {agent_name!r}")

    context = DispatchContext(
        script_dir=REPO_ROOT,
        mode="full",
        diff_path=STRESS_DIFF_PATH,
        provider=provider,
        standard_model=model_id,
        premium_model=model_id,
        # Match the ceiling that actually crashed in CI (#592), not a looser one.
        max_tokens_per_agent=16384,
        changed_files=[
            ".github/workflows/slash-commands.yml",
            "ai_pr_review/slash/dismiss.py",
            "ai_pr_review/vcs/github.py",
            "ai_pr_review/cli.py",
        ],
    )

    try:
        successes, failures = await run_tier(agents, llm_call, context, semaphore_size=1)
    except Exception as exc:  # noqa: BLE001 - canary must report, not crash
        return CanaryResult(provider, model_id, agent_name, False, f"run_tier raised: {exc!r}")

    if failures:
        f = failures[0]
        return CanaryResult(
            provider, model_id, agent_name, False,
            f"agent failed (exit_code={f.exit_code}): {f.reason[:300]}",
        )

    result = successes[0]
    stop_reason = result.stop_reason
    thinking_tokens = result.token_log.thinking_tokens if result.token_log else 0
    if stop_reason != "end_turn":
        # Stricter than "text is non-empty": a response that hit max_tokens
        # but still produced *some* text is a silently truncated, degraded
        # review, not a crash -- it would pass a looser check while still
        # being a real quality problem. See the #592 test plan, Tier 2.
        return CanaryResult(
            provider, model_id, agent_name, False,
            f"stop_reason={stop_reason!r} (expected end_turn), "
            f"thinking_tokens={thinking_tokens}, output_tokens="
            f"{result.token_log.output if result.token_log else 'n/a'}",
        )

    return CanaryResult(
        provider, model_id, agent_name, True,
        f"stop_reason=end_turn, thinking_tokens={thinking_tokens}",
    )


async def main() -> int:
    results: list[CanaryResult] = []
    for provider, (env_var, standard_model, premium_model) in PROVIDER_MODELS.items():
        if not os.environ.get(env_var):
            print(f"SKIP: {provider} (no {env_var} set)", file=sys.stderr)
            continue
        for model_id in {standard_model, premium_model}:
            for agent_name in TARGET_AGENT_NAMES:
                result = await _run_one(provider, model_id, agent_name)
                results.append(result)
                status = "OK  " if result.ok else "FAIL"
                print(f"{status} provider={provider} model={model_id} agent={agent_name}: {result.detail}")

    if not results:
        print("ERROR: no providers had a usable API key; nothing was tested", file=sys.stderr)
        return 1

    failed = [r for r in results if not r.ok]
    print(f"\n=== {len(results) - len(failed)}/{len(results)} passed ===")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
