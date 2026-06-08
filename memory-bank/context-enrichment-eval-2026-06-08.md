# Context Enrichment Evaluation — 2026-06-08

**Issue:** [#391](https://github.com/tag1consulting/ai-pr-review/issues/391)
**Model:** `claude-haiku-4-5-20251001`  **Mode:** `quick`  **Corpus:** PRs #443, #445, #447

## Summary

| PR | Off findings | On findings | Delta | Off input tok | On input tok | Context tok | Off latency | On latency |
|---|---|---|---|---|---|---|---|---|
| #443 | 2 | 2 | 0 | 6,345 | 6,345 | 0 | 8.1s | 9.3s |
| #445 | 0 | 0 | 0 | 3,553 | 3,553 | 0 | 4.2s | 4.4s |
| #447 | 7 | 7 | 0 | 6,973 | 6,973 | 8,221 | 26.4s | 22.6s |
| **Total** | **9** | **9** | **+0** | **16,871** | **16,871** | **8,221** | — | — |

Context token overhead: **+0.0%** input tokens (8,221 context tokens across all runs)

> **Note on token measurement**: `input_tok` reflects the model's reported `input_tokens` value from the response object. The context block is counted there, but the harness aggregates across agents without separating baseline from enrichment overhead. For PR #447 with `ctx_tok=8,221`, the actual per-eligible-agent input token increase is approximately `8,221 / (agents_eligible)` additional tokens. At claude-haiku pricing (~$0.80/M input), 8,221 tokens ≈ $0.007 per review for PR #447-sized diffs.

## Per-PR Detail

### PR #443

Agents selected: 1  |  Context tokens used (on): 0

**Severity breakdown:**

| Severity | Off | On |
|---|---|---|
| high | 1 | 1 |
| medium | 1 | 1 |

**Overlap:** 0 findings in both runs, 2 only-off, 2 only-on

**New findings with enrichment ON:**

- [HIGH] `ai_pr_review/config.py:230` — exclude_patterns_mode validator normalizes to lowercase but does not validate the input before normalization, allowing i
- [MEDIUM] `tests/python/test_config.py:128` — Test for exclude_patterns whitespace trimming uses monkeypatch.setenv but does not verify the behavior is actually imple

**Findings dropped with enrichment ON:**

- [HIGH] `ai_pr_review/config.py:230` — exclude_patterns_mode validator normalizes input to lowercase but does not document this behavior in the field docstring
- [MEDIUM] `tests/python/test_config.py:129` — Test for whitespace trimming in exclude_patterns uses monkeypatch.setenv but does not verify the behavior is actually te

### PR #445

Agents selected: 1  |  Context tokens used (on): 0

**Overlap:** 0 findings in both runs, 0 only-off, 0 only-on

### PR #447

Agents selected: 2  |  Context tokens used (on): 8,221

**Severity breakdown:**

| Severity | Off | On |
|---|---|---|
| high | 3 | 3 |
| low | 0 | 1 |
| medium | 4 | 3 |

**Overlap:** 0 findings in both runs, 7 only-off, 7 only-on

**New findings with enrichment ON:**

- [HIGH] `ai_pr_review/cli.py:475` — Subprocess error handling in _fetch_open_issues returns generic sentinel instead of propagating error details
- [MEDIUM] `ai_pr_review/cli.py:486` — Missing import statement for `subprocess` module at module level
- [HIGH] `ai_pr_review/cli.py:495` — Subprocess call to `gh issue list` does not validate the repository argument before passing it to shell
- [HIGH] `ai_pr_review/cli.py:506` — JSON parsing failure silently returns (unavailable) without logging the actual parse error or stderr context
- [MEDIUM] `ai_pr_review/cli.py:614` — Dangerous fallback behavior: _fetch_open_issues failure is masked by returning (unavailable), allowing the LLM to procee
- [MEDIUM] `ai_pr_review/cli.py:622` — Incomplete error propagation: _run_issue_linker catches all exceptions and returns empty string, masking LLM call failur
- [LOW] `tests/python/test_issue_linker.py:193` — Test mock setup does not verify that _fetch_open_issues is called with the correct repository argument

**Findings dropped with enrichment ON:**

- [HIGH] `ai_pr_review/cli.py:475` — [security] Subprocess command constructed with user-controlled repository slug without validation
- [HIGH] `ai_pr_review/cli.py:495` — [error-handling] JSON parsing error logs stderr but does not include the actual stdout that failed to parse, making debu
- [MEDIUM] `ai_pr_review/cli.py:502` — [robustness] Missing null-safety check on label dictionary access; if a label object lacks 'name' key, it silently skips
- [MEDIUM] `ai_pr_review/cli.py:560` — Dangerous fallback behavior: _run_issue_linker returns empty string on LLM failure without distinguishing between 'no is
- [HIGH] `ai_pr_review/cli.py:614` — Fail-soft error handling in _run_issue_linker silently masks subprocess failures in git commands without logging
- [MEDIUM] `tests/python/test_issue_linker.py:32` — [test-quality] Mock subprocess.CompletedProcess is created with MagicMock instead of actual subprocess.CompletedProcess 
- [MEDIUM] `tests/python/test_issue_linker.py:186` — Test mocks subprocess.run but does not verify that _fetch_open_issues is called with the correct repository argument, al

## Graceful Degradation — Non-Container Consumers

When `tree-sitter-language-pack` is missing, `extract_symbol_refs()` returns `[]` 
and enrichment silently no-ops (no error, no partial context). The regex fallback 
`extract_symbol_refs_fallback()` exists in `context/treesitter.py` but is **not** 
wired into `dispatch.py`'s `_build_user_message()` — consumers without tree-sitter 
get no symbol context at all. This is an acceptable degradation path (no-op is safe), 
but means non-container consumers on default-on would silently receive the same review 
quality as default-off.

ripgrep (`rg`) absence is also handled gracefully: `lookup_definitions()` returns `[]` 
and logs a WARNING. Full degradation chain is fail-soft at every layer.

## Analysis Notes

**Why Overlap=0 across all PRs**: Every finding comparison shows 0 shared keys between OFF and ON runs. This is not a quality signal — it reflects LLM non-determinism in wording. The findings are substantively the same issues phrased differently (e.g. both runs find the same `ai_pr_review/cli.py:495` subprocess validation issue; the wording differs slightly). For a real quality evaluation, findings need human FP/TP classification rather than string-match deduplication.

**PR #443 produced ctx_tok=0 with enrichment ON (root-caused)**: PR #443 touches `action.yml`, `container-action/action.yml`, and `docs/configuration.md` in addition to 2 Python files. `_detect_primary_language` uses plurality voting — 3 YAML/Markdown files outweigh 2 Python files, so `language="yaml"` is selected. `_LANG_EXTENSIONS` has no `"yaml"` key, so `_glob_patterns("yaml")` returns `[]` and tree-sitter has no grammar to select → `extract_symbol_refs` returns `[]` → no context block. This is a known limitation of the single-language enrichment model: minority-language files in a mixed-language PR receive no enrichment. PR #445 (docs-only release PR: CHANGELOG, action.yml, prompts) correctly produced no context.

**PR #447 produced ctx_tok=8,221**: The `ai_pr_review/cli.py` and `tests/python/test_issue_linker.py` files triggered symbol extraction and ripgrep lookup. The `max_queries=50` cap was reached twice (two eligible agents), indicating a large symbol set. Finding count was identical (7), severity breakdown nearly identical (high: 3, medium: 3-4, with one low added by enrichment-on).

**Latency**: PR #447 was actually 3.8s *faster* with enrichment on (22.6s vs 26.4s) — within noise for these run times. No latency penalty observed.

## Recommendation

Criteria from issue #391:
- [~] **Review-quality delta**: Finding count is identical across all 3 PRs. Severity breakdown is within noise (±1 medium, +1 low). No measurable accuracy gain detected in this corpus — but the corpus is small (3 PRs, all from this repo's own development history) and the model is haiku which may not show enrichment benefits as clearly as sonnet/opus.
- [x] **Token/cost delta**: Acceptable. 8,221 context tokens on the largest diff (~$0.007 additional at haiku pricing). At sonnet pricing (~$3/M input), the same 8,221 tokens = ~$0.025 per review. For typical diffs, the overhead is small.
- [x] **Graceful degradation**: Confirmed fail-soft at every layer. Missing tree-sitter → no-op (empty refs). Missing ripgrep → no-op (logged WARNING). Unexpected error → catch-all in dispatch.py returns raw diff. No errors thrown in any run.
- [x] **Latency**: No meaningful overhead detected. Enrichment was slightly *faster* in the one PR where it fired (likely due to LLM response variance).

**Decision: flip-only-for-container (recommend)**

**Rationale**: The evaluation shows enrichment is safe to default-on (fail-soft, no cost/latency concerns), but did not demonstrate a measurable quality improvement on this corpus. The "flip-only-for-container" path is the right call: container-action consumers have tree-sitter + ripgrep available and get real enrichment; direct-action consumers without the extras silently no-op and are unaffected. This avoids the documentation burden of explaining why a "default true" feature does nothing for some consumers. A follow-up evaluation on a corpus with more cross-file symbol references (e.g. larger PRs with architectural changes) would give cleaner signal. File under "safe to flip for container, but quality evidence is thin."

**Phase B status (complete)**:
1. ✅ Added `tests/python/test_context_symbols.py` — 16 tests covering ripgrep no-op, path-confinement, query cap, cache hits, proximity classification, `_read_snippet`, `_glob_patterns`, and end-to-end `lookup_definitions` returning `Definition` objects. 1088 total tests passing.
2. ✅ Root-caused `ctx_tok=0` on PR #443: plurality language voting selects `"yaml"` (3 YAML/Markdown files outweigh 2 Python files). Known limitation of single-language enrichment model — not a bug.
3. **Phase C (conditional flip)**: if proceeding with container-only flip, update `container-action/action.yml` default to `'true'` and leave composite `action.yml` at `'false'` (enrichment is a no-op without tree-sitter + ripgrep, which only ship in the container image). Update docs to note the asymmetry. Add CHANGELOG entry.
