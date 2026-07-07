# Story 14.1: Upgrade Default Anthropic Standard Model to Sonnet 5

**Epic:** 14 — Model default maintenance
**Story ID:** 14-1
**Story Key:** 14-1-sonnet-5-default
**GitHub Issue:** none (maintenance change, planned and implemented directly)
**Status:** done

---

## Story

As a **maintainer**,
I want the default `anthropic`-provider "standard" model (and the `bedrock-proxy` standard model) bumped from `claude-sonnet-4-6` to `claude-sonnet-5`,
so that reviews running on the standard tier — the tier most reviews actually use, since premium/Opus is reserved for `full` mode and heavier tasks — pick up Sonnet 5's coding/agentic quality gains, without silently 400-ing every Anthropic call or losing token-cost visibility.

---

## Acceptance Criteria

1. `ai_pr_review/config.py`'s `_PROVIDER_DEFAULTS` anthropic standard slot is `claude-sonnet-5`; bedrock-proxy standard slot is `us.anthropic.claude-sonnet-5`. Premium (Opus) defaults are unchanged.
2. `ai_pr_review/llm/_config.py::resolve_temperature()` returns `None` (temperature omitted) for any model ID containing `"sonnet-5"`, matching the existing behavior for `opus-4-7`/`opus-4-8`. Without this, every Anthropic call would send a non-default `temperature` and receive a 400, since Sonnet 5 unconditionally rejects non-default `temperature`/`top_p`/`top_k`.
3. `config/model-pricing.json` has a new entry matching `claude-sonnet-5` with non-zero rates ($3/$15 per MTok input/output — the standard sticker, not the temporary $2/$10 intro pricing that expires 2026-08-31). The existing Sonnet 4.6/4.5 entry is untouched so pinned users on the old model still get accurate pricing.
4. Provider-defaults tables in `CLAUDE.md`, `README.md`, `docs/configuration.md`, and `docs/architecture-internals.md` reflect the new default.
5. New regression tests lock in both failure modes this change could reintroduce on a future model bump:
   - `resolve_temperature()` rejects Sonnet 5, still rejects Opus 4.7/4.8, still accepts Sonnet 4.6 (unit tests in `tests/python/llm/test_config.py`).
   - The resolved anthropic standard *and* premium defaults each get a non-zero rate match against the real `config/model-pricing.json` (tests in `tests/python/test_pricing.py`) — this guards the silent `n/a`/`$0` cost-table failure for this and every future default-model bump.
   - `resolve_models()` fills the anthropic standard slot with `claude-sonnet-5` and the bedrock-proxy standard slot with `us.anthropic.claude-sonnet-5` (tests in `tests/python/test_config.py`, mirroring the existing `test_anthropic_premium_default_is_opus_4_8`).
6. `pytest tests/python -q`, `mypy ai_pr_review/`, and `ruff check ai_pr_review/ tests/python/` all pass (mypy: the one pre-existing `context/treesitter.py` overload error is unrelated to this change and present on `main` before this story).
7. `Workflow({name: 'ai-pr-review-e2e'})` passes against all three live test platforms, confirming the new default is accepted by the live Anthropic API (no 400 on temperature) and the token-cost table renders a real dollar amount — not `n/a` — for the standard-tier agent rows.

---

## Out of Scope

- No `thinking`/`budget_tokens` handling added. The codebase never sends a `thinking` parameter to any Anthropic-shaped request (confirmed by reading `ai_pr_review/llm/anthropic.py` and `_config.py`). Running Sonnet 5 with `thinking` omitted defaults to adaptive thinking automatically — a behavior improvement, not a breaking change, requiring no code edit.
- No token-budget retuning. Sonnet 5's tokenizer is ~30% denser than Sonnet 4.6's for the same text, which affects `context_max_tokens` (8192), `max_tokens_per_agent` (16384), and `profile_max_tokens` (4096) in real terms. Not retuned preemptively — observed via the e2e workflow; adjusted only if truncation is actually seen.
- No workflow/`action.yml` edits. Confirmed no hardcoded model ID exists there; defaults flow through `config.py` only.
- No judge-pass-specific wiring. The judge pass reuses `config.model_standard` (`ai_pr_review/review/runtime.py`), so it inherits the new default automatically.
- Test fixtures that use `claude-sonnet-4-6` as arbitrary sample/mock data rather than asserting the real default (e.g. `tests/python/llm/conftest.py`, the `llm/fixtures/*.json` mock response bodies, and the bulk of `test_orchestrate.py`/`test_runtime.py`/`test_telemetry.py`/`test_cli.py`) are left unchanged — they test plumbing/parsing correctness against an arbitrary model string, not "is Sonnet 4.6 the real default," so editing them is pure churn with no regression-locking value.

---

## Technical Notes

### The load-bearing risk this story exists to fix

A naive model-ID swap (editing only `config.py`) would pass all existing tests and look correct, then 400 on the first live Anthropic call: `resolve_temperature()` in `ai_pr_review/llm/_config.py` is a hardcoded substring allowlist of models that don't accept temperature. It included `opus-4-7`/`opus-4-8` but not `sonnet-5`. `ai_pr_review/llm/anthropic.py::_build_body()` unconditionally attaches `temperature` to the request body whenever `resolve_temperature()` returns non-`None`, and this repo's default `temperature` is `0.3` — a non-default value that Sonnet 5 rejects outright.

A second, quieter risk: `config/model-pricing.json` pattern-matches model IDs to pricing rates; no existing pattern matched `claude-sonnet-5`, so the token-cost table would have silently rendered `n/a` for every review post-swap, with no error and no test failure to catch it (there was no existing test asserting the resolved default has a pricing match).

### Verified facts

- Sonnet 5 model ID: `claude-sonnet-5` (bare string, no date suffix). Bedrock-proxy form: `us.anthropic.claude-sonnet-5`.
- Pricing: $3/$15 per MTok input/output — identical sticker to Sonnet 4.6. Introductory $2/$10 pricing applies only through 2026-08-31; this story uses the standard rate so the entry doesn't go stale.
- No hardcoded model ID exists in `action.yml` or any GitHub Actions workflow — confirmed by an independent code-search pass. The `model-standard`/`model-premium` action inputs default to empty string; the actual default lives solely in `config.py::resolve_models()`.
- `.serena/memories/llm.md` (now removed from the repo along with the rest of `.serena/`, per a separate decision) was already stale independent of this change, showing anthropic premium as `claude-opus-4-7` when the real default is `claude-opus-4-8`.

---

## Tasks

- [x] Update `ai_pr_review/config.py::_PROVIDER_DEFAULTS` — anthropic and bedrock-proxy standard slots.
- [x] Add `"sonnet-5"` to the reject-temperature allowlist in `ai_pr_review/llm/_config.py::resolve_temperature()`.
- [x] Add a new `claude-sonnet-5` entry to `config/model-pricing.json`, placed before the Sonnet 4.6 block.
- [x] Update provider-defaults tables in `CLAUDE.md`, `README.md`, `docs/configuration.md`, `docs/architecture-internals.md`.
- [x] Add `tests/python/llm/test_config.py` covering `resolve_temperature()` for Sonnet 5, Opus 4.7/4.8 (regression), and Sonnet 4.6 (regression).
- [x] Add pricing-parity tests in `tests/python/test_pricing.py` asserting the real resolved anthropic standard and premium defaults each match a non-zero pricing entry.
- [x] Add `test_anthropic_standard_default_is_sonnet_5` and `test_bedrock_proxy_standard_default_is_sonnet_5` in `tests/python/test_config.py`.
- [x] Run `pytest tests/python -q`, `mypy ai_pr_review/`, `ruff check ai_pr_review/ tests/python/`.
- [x] Run `Workflow({name: 'ai-pr-review-e2e'})` before merge — done, see Dev Notes below.
- [ ] Open PR; address review findings.

---

## Dev Notes

- `docs/features.md:156` retains its historical mention of Serena's onboarding PR (#373) as a changelog-style record of what happened at that point in time — left unchanged per the repo's convention against rewriting history, even though `.serena/` itself was removed in this same branch at the user's request (unrelated to the Sonnet 5 change, bundled here as a small housekeeping item).
- Test files explicitly identified during planning as *not* needing edits (arbitrary sample data, not default-tracking): `tests/python/llm/conftest.py`, `tests/python/llm/fixtures/anthropic_cached.json`, `anthropic_happy.json`, `bedrock_happy.json`, `tests/python/llm/test_anthropic.py`, `tests/python/llm/test_bedrock.py`, `tests/python/test_cli.py`, `tests/python/test_orchestrate.py`, `tests/python/test_runtime.py`, `tests/python/test_telemetry.py`. Each was individually inspected to confirm the `claude-sonnet-4-6` string appears as an explicit constructor argument or mock-response value that round-trips through an assertion, never as an implicit "this is the real default" check.

### E2E verification results (2026-07-07)

Ran `Workflow({name: 'ai-pr-review-e2e'})` against a Docker image built from this branch (`f113ea9`), against all three live test platforms. Also removed a hardcoded `AI_MODEL_STANDARD=claude-sonnet-4-6` line from the e2e workflow script itself (`~/.claude/workflows/ai-pr-review-e2e.js`, with explicit user approval) — as written it would have masked this exact test by always pinning the old model regardless of which checkout was built.

- **GitHub PR #1** — full end-to-end proof. Live review posted (review ID `4647613687`, submitted `2026-07-07T17:46:10Z`): `event=REQUEST_CHANGES`, 32 findings, 0 failed agents. Token-cost table shows `silent-failure-hunter | Sonnet 5 | ... | $0.2271`, `code-reviewer | Sonnet 5 | ... | $0.3597`, `judge-pass | Sonnet 5 | ... | $0.0544` — real dollar amounts, not `n/a`, and the model name reads "Sonnet 5" not "Sonnet 4.6". This directly confirms both load-bearing risks this story exists to prevent: no temperature-400 on the live Anthropic API, and the pricing table resolves correctly.
- **GitLab MR !34** — `event=REQUEST_CHANGES`, 28 findings, 0 failed agents; agents ran and returned successfully (no 400s), but the summary comment (which carries the token/cost table) was not posted. Root-caused to a pre-existing, unrelated bug: a stale review watermark note (2026-05-14) caused the run to be misclassified as incremental, which by design skips re-posting the summary. Confirmed via `git diff main...feat/sonnet-5-default -- ai_pr_review/vcs/ ai_pr_review/review/ ai_pr_review/orchestrate.py` returning empty — this story's diff does not touch that code path. Filed as [tag1consulting/ai-pr-review#581](https://github.com/tag1consulting/ai-pr-review/issues/581).
- **Bitbucket PR #2** — `event=REQUEST_CHANGES`, 37 findings, 0 failed agents; same symptom (no new comment posted, `comment_count` unchanged), presumed same bug class as GitLab (not individually root-caused).
- Also filed [tag1consulting/ai-pr-review#582](https://github.com/tag1consulting/ai-pr-review/issues/582): the e2e workflow's own pass/fail gate only parses container stdout (`Review complete: ...`) and never checks whether anything was actually posted or what model name appears in the cost table — it would have reported `allPassed: true` on GitLab/Bitbucket even though the summary silently didn't post. GitHub's independently-verified posted content is what actually closes AC#7, not the workflow's `allPassed` flag by itself.
- Bedrock-proxy's `us.anthropic.claude-sonnet-5` remains unverified by this e2e run — all three test platforms use the `anthropic` provider directly, none exercise the bedrock-proxy code path. This is disclosed in the PR body as an open gap, not something e2e can close.
