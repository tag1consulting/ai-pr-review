# Story 7.2: Per-Agent Language-Profile Budget and Routing

**Epic:** 7 — Review-Quality & Perf Research
**Story ID:** 7-2
**Story Key:** 7-2-profile-routing
**GitHub Issue:** #355
**Parent Epic Issue:** #362
**Status:** ready-for-dev

---

## Story

As a **maintainer**,
I want agent prompts to receive only the language-profile sections relevant to their review focus,
so that per-agent token spend is reduced and irrelevant profile content is not wasted on agents that don't use it.

---

## Acceptance Criteria

1. Language profile files (`language-profiles/*.md`) are parsed into sections by `### ` boundaries. Each file has one `## ` title and 4-7 `### ` subsections.
2. Sections are classified into focus tags: `{security}`, `{bugs}`, `{edge}`, `{idioms}`, `{general}`. Sections tagged `{general}` go to every eligible agent; all others are routed only to agents with a matching `profile_focus`.
3. `AgentSpec` gains a `profile_focus: frozenset[str]` field. Roster assignments:
   - `security-reviewer` → `{security}`
   - `silent-failure-hunter` → `{bugs, edge}`
   - `edge-case-hunter` → `{edge, bugs}`
   - `code-reviewer`, `architecture-reviewer`, `adversarial-general` → all tags (broad reviewers)
   - `pr-summarizer` → `{general}` only (does not review code)
   - `blind-hunter`, `issue-linker` → already `context_enrichment_eligible=False`; no profile sent
4. A new `AI_PROFILE_MAX_TOKENS` config knob (default 4096) caps the profile text per agent.
   - `profile_max_tokens: int = 4096` on `ReviewConfig`
   - `AI_PROFILE_MAX_TOKENS` in `_KNOWN_AI_VARS`
   - `_int("AI_PROFILE_MAX_TOKENS", 4096)` in `from_env` (mirrors `feedback_max_tokens` pattern at `config.py:214`)
5. The token table gains a **"Language profiles"** supplementary row (parallel to the "Context enrichment" row at `pricing.py:182-194`):
   - `AgentResult` gains `profile_tokens_used: int = 0`
   - `cli.py` threads `max(ar.profile_tokens_used for ar in successes)` as a new `profile_tokens` kwarg to `emit_token_table`
   - `pricing.py` emits the supplementary row after line 194
6. Tests cover:
   - Section splitting of a representative profile
   - Section classification for all 5 tag families
   - Per-agent routing: `security-reviewer` receives only `{security, general}` sections; `code-reviewer` receives all
   - Budget truncation: a profile exceeding `profile_max_tokens` is truncated to fit
   - Token-table row is present in the rendered output
7. `mypy`, `ruff`, `pytest tests/python -q` all pass.

---

## Implementation Tasks

- [ ] **Task 1 — New module `ai_pr_review/language_profile_sections.py`** (AC: 1, 2)
  - [ ] `ProfileSection` dataclass: `heading: str`, `body: str`, `tags: frozenset[str]`
  - [ ] `split_sections(profile_text: str) -> list[ProfileSection]` — parse by `### ` boundaries; keep the `## ` title as shared header prepended to each section's body.
  - [ ] `classify_section(heading: str) -> frozenset[str]` — keyword mapping:
    - Contains `security` (case-insensitive) → `{security}`
    - Contains `bug` or `error handling` → `{bugs, edge}`
    - Contains `validation` or `do not flag` → `{idioms, edge}`
    - Contains `idiomatic` → `{idioms}`
    - Anything unmatched → `{general}`
  - [ ] `ProfileRouter` class:
    - `__init__(self, labels: list[str], script_dir: Path)` — calls `load_language_profiles` then splits each profile into sections
    - `route(self, focus: frozenset[str], max_tokens: int) -> str` — collect sections where `section.tags & (focus | {"general"})`, greedily pack under `max_tokens` (reuse `estimate_tokens` from `context/budget.py:22`), return assembled text
  - [ ] Add `from __future__ import annotations`; annotate all functions for `mypy --strict`

- [ ] **Task 2 — `agents/roster.py`: Add `profile_focus` to `AgentSpec`** (AC: 3)
  - [ ] Add `profile_focus: frozenset[str] = frozenset()` field to `AgentSpec` frozen dataclass
  - [ ] Update all 9 roster agent entries with the assignments from AC 3
  - [ ] Agents with `context_enrichment_eligible=False` (`blind-hunter`, `issue-linker`) need no `profile_focus` (default empty is correct — they never reach the routing code)

- [ ] **Task 3 — `ai_pr_review/config.py`: Add `profile_max_tokens`** (AC: 4)
  - [ ] Add `AI_PROFILE_MAX_TOKENS` to `_KNOWN_AI_VARS` frozenset
  - [ ] Add `profile_max_tokens: int = 4096` to `ReviewConfig` (near `feedback_max_tokens`)
  - [ ] Add `profile_max_tokens=_int("AI_PROFILE_MAX_TOKENS", 4096)` in `from_env`

- [ ] **Task 4 — `review/runtime.py`: Build `ProfileRouter` instead of flat text** (AC: 1, 2)
  - [ ] Replace the `load_language_profiles` call at lines 173-175 with construction of a `ProfileRouter`
  - [ ] Store the router on `DispatchContext` — add a `profile_router: ProfileRouter | None` field (default `None`; set when profiles are available) OR replace `language_profile_text: str` with it
  - [ ] Thread `profile_max_tokens` from `cfg.profile_max_tokens` into `DispatchContext`

- [ ] **Task 5 — `agents/dispatch.py`: Per-agent routing** (AC: 2, 3)
  - [ ] In `_run_single_agent`, replace lines 564-565 (`context.language_profile_text and spec.context_enrichment_eligible`) with a call to `context.profile_router.route(spec.profile_focus, context.profile_max_tokens)` (guarded on `context_enrichment_eligible`)
  - [ ] Capture the routed text's token count via `estimate_tokens(routed_text)`; store on `AgentResult` as `profile_tokens_used`

- [ ] **Task 6 — Token table** (AC: 5)
  - [ ] Add `profile_tokens_used: int = 0` to `AgentResult` dataclass
  - [ ] In `cli.py` (`_build_token_table_accordion`): compute `profile_tokens = max((ar.profile_tokens_used for ar in successes), default=0)` and pass as new kwarg to `emit_token_table`
  - [ ] In `pricing.py` (`emit_token_table`): add `profile_tokens: int = 0` parameter; if non-zero, emit a "Language profiles" supplementary row after the existing Context enrichment row (same formatting)

- [ ] **Task 7 — Tests** (AC: 6, 7)
  - [ ] New `tests/python/test_language_profile_routing.py`:
    - `test_split_sections_basic` — split a 3-section mock profile, assert 3 `ProfileSection` objects
    - `test_classify_section_security` — heading containing "Security" → `{security}` in tags
    - `test_classify_section_bugs` — "Common Python Bugs" → `{bugs, edge}`
    - `test_classify_section_idioms` — "Python Validation Idioms (Do NOT Flag)" → `{idioms, edge}`
    - `test_classify_section_idiomatic` — "Idiomatic Python" → `{idioms}`
    - `test_classify_section_unmatched` → `{general}`
    - `test_router_security_reviewer_gets_security_and_general` — routing with `{security}` focus omits `{idioms}`-only sections
    - `test_router_code_reviewer_gets_all` — routing with all-tag focus gets all sections
    - `test_router_budget_truncation` — set `max_tokens=10`; assert result is shorter than full profile
    - `test_token_table_profile_row` — mock emit call confirms row renders when `profile_tokens > 0`
  - [ ] Run `pytest tests/python -q` — must be green

- [ ] **Task 8 — Verification** (AC: 7)
  - [ ] `mypy ai_pr_review/` — clean
  - [ ] `ruff check ai_pr_review/ tests/python/` — clean
  - [ ] `python -c "import ai_pr_review.language_profile_sections"` — no import error

---

## Dev Notes

### Critical File Locations (verified at c8ee811)

| File | Purpose | Key lines |
|------|---------|-----------|
| `ai_pr_review/language_profiles.py` | Existing flat loader | `load_language_profiles` (19-50) |
| `ai_pr_review/agents/roster.py` | `AgentSpec` dataclass | fields (31-58), agents (69-162) |
| `ai_pr_review/agents/dispatch.py` | Per-agent LLM dispatch | system_prefix build (561-566), `AgentResult` (47-51) |
| `ai_pr_review/review/runtime.py` | Runtime assembly | profile load (173-175), `DispatchContext` build (190-210) |
| `ai_pr_review/config.py` | Config | `_KNOWN_AI_VARS` (23-79), `ReviewConfig` (139+), `from_env` (368+) |
| `ai_pr_review/context/budget.py` | Token estimator | `estimate_tokens` (22) |
| `ai_pr_review/pricing.py` | Token table | `emit_token_table` (99), supplementary rows (182-194) |
| `ai_pr_review/cli.py` | Token table builder | `_build_token_table_accordion` (668-745), context_tokens (713-716) |

### Caching Trade-off (document in PR)

Today `system_prefix` is byte-identical across all agents in a run, so Anthropic/Bedrock deduplicate it at one cache-creation event for the whole run. Per-agent routing makes each agent's `system_prefix` differ, so each agent may incur its own cache-creation write. Trade-off: smaller, more relevant profile per agent vs. one shared cache hit. For multi-language PRs (common case) the profile reduction likely saves more than the extra cache writes cost. The token table's "Language profiles" row makes this measurable per-run.

### Section Classification Edge Cases

- Headings vary across languages: `### Error Handling` (Go), `### Common Python Bugs` (Python), `### Ruby on Rails-Specific Patterns` (Ruby). The keyword match must be case-insensitive and substring-based, not exact.
- Framework-specific sections (`### Pulumi Provider Patterns`, `### Drupal-Specific Patterns`) have no security/bugs/edge classification — they fall through to `{general}` and reach all eligible agents (correct: they are project-context sections).
- When a heading matches multiple patterns (e.g. "Security Edge Cases") → union of all matching tag sets: `{security, edge}`.

### What NOT to Do

- **Do not copy `_ANALYZER_PREFIXES`** or other constants — this module is self-contained.
- **Do not break the existing `language_profile_text` field on `DispatchContext`** if anything outside dispatch.py reads it — check first; if nothing external reads it, replace it; if something does, augment with a new `profile_router` field and leave the old one as `""`.
- **Do not add `profile_focus` to `blind-hunter` or `issue-linker`** — both are already `context_enrichment_eligible=False` and the routing code is gated on that flag.

---

## Dev Agent Record

### Agent Model Used

_to be filled in_

### Debug Log References

_none_

### Completion Notes List

_to be filled in_

### File List

- `ai_pr_review/language_profile_sections.py` (new)
- `ai_pr_review/agents/roster.py` (modified)
- `ai_pr_review/config.py` (modified)
- `ai_pr_review/review/runtime.py` (modified)
- `ai_pr_review/agents/dispatch.py` (modified)
- `ai_pr_review/pricing.py` (modified)
- `ai_pr_review/cli.py` (modified)
- `tests/python/test_language_profile_routing.py` (new)
