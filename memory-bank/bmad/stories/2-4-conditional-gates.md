---
title: "E2.S4 — Conditional gates port"
epic: 2
story: 4
status: ready-for-review
github_issue: 219
branch: rework/epic-2-llm-vcs-dispatch
---

# E2.S4 — Conditional gates port

## Summary

Implement `ai_pr_review/agents/gates.py` — ports `detect_conditional_agent_triggers` from
`lib/diff.sh`. Given a diff text and a `ChangedFiles` instance, returns a
`frozenset[ConditionalTrigger]` of the gates that fire. The dispatch layer uses this set to
filter `AgentSpec` entries whose `conditional_trigger` is not in the fired set. Kill-switch env
vars (`AI_DISABLE_GATE_*`) bypass individual gates.

## PRD Reference

2.FR-4

## Acceptance Criteria

- [x] AC1: `evaluate_gates(diff_text, changed_files, env) -> frozenset[ConditionalTrigger]` returns only
  the trigger keys whose conditions match.
- [x] AC2: `has_error_patterns` fires when diff contains any of: `catch`, `if err`, `try {`, `rescue`,
  `Result<`, `unwrap`, `except`, `.catch(` — exact bash port.
- [x] AC3: `has_code_or_infra` fires when changed_files contains at least one non-docs, non-meta file.
  Workflow files under `.github/workflows/` count as infra (not docs). Docs-only: `.md`, `.rst`, `.txt`,
  `.adoc`; meta dirs: `docs/`, `memory-bank/`, `.github/` (non-workflow), `.claude/`; meta filenames:
  `CHANGELOG`, `README`, `LICENSE`, `NOTICE`, `AUTHORS`, `CONTRIBUTING`, `CODEOWNERS`,
  `CODE_OF_CONDUCT` (with any extension).
- [x] AC4: `has_security_patterns` fires when diff matches a broad security-keyword regex OR any changed
  file path matches security-sensitive path patterns — exact bash port.
- [x] AC5: `has_control_flow` fires when added lines (`+` prefix, excluding `+++`) contain any
  control-flow keyword: `if`, `elif`, `else`, `for`, `while`, `do`, `case`, `switch`, `match`, `try`,
  `catch`, `except`, `rescue`, `unless`, `when`, `loop`, `break`, `continue`, `return`, `goto`,
  `defer`, `finally`.
- [x] AC6: `no_prior_summary` fires when `last_reviewed_sha` is empty or None (incremental re-review
  suppresses pr-summarizer).
- [x] AC7: Each gate is individually bypassable: if `env["AI_DISABLE_GATE_ARCHITECTURE"]` is `"true"`,
  `has_code_or_infra` is always included in the result regardless of file list. Similarly for
  `AI_DISABLE_GATE_SECURITY` → `has_security_patterns` and `AI_DISABLE_GATE_EDGE_CASE` →
  `has_control_flow`.
- [x] AC8: `filter_agents(agents, fired_gates) -> list[AgentSpec]` returns only agents whose
  `conditional_trigger` is `None` (unconditional) or is in `fired_gates`.
- [x] AC9: `mypy --strict` and `ruff check` clean on all new code.
- [x] AC10: Unit tests cover all gate conditions, kill-switches, and `filter_agents` logic.

## Tasks/Subtasks

- [x] T1: Create `ai_pr_review/agents/gates.py`
  - [x] T1.1: Define `GateEnv` typed dict (or `Mapping[str, str]`) for the env-var interface
  - [x] T1.2: Implement `_has_error_patterns(diff_text) -> bool`
  - [x] T1.3: Implement `_has_code_or_infra(changed_files: ChangedFiles) -> bool` with exact bash logic
  - [x] T1.4: Implement `_has_security_patterns(diff_text, changed_files) -> bool`
  - [x] T1.5: Implement `_has_control_flow(diff_text) -> bool` (added lines only)
  - [x] T1.6: Implement `_no_prior_summary(last_reviewed_sha: str | None) -> bool`
  - [x] T1.7: Implement `evaluate_gates(diff_text, changed_files, env, last_reviewed_sha) -> frozenset[ConditionalTrigger]`
  - [x] T1.8: Implement `filter_agents(agents, fired_gates) -> list[AgentSpec]`

- [x] T2: Write tests in `tests/python/agents/test_gates.py`
  - [x] T2.1: Test `has_error_patterns` for each trigger keyword and absence
  - [x] T2.2: Test `has_code_or_infra` with source file, docs-only, and workflow-file inputs
  - [x] T2.3: Test `has_security_patterns` for keyword match, path match, and no-match
  - [x] T2.4: Test `has_control_flow` with added lines containing keywords, removed lines (should not fire), and no keywords
  - [x] T2.5: Test `no_prior_summary` with empty string, None, and a real SHA
  - [x] T2.6: Test kill-switches: each `AI_DISABLE_GATE_*` forces the corresponding trigger to fire
  - [x] T2.7: Test `filter_agents`: unconditional agents always included; conditional agents included only when trigger fires; conditional agents excluded when trigger absent
  - [x] T2.8: Test `evaluate_gates` end-to-end with a realistic diff snippet

- [x] T3: Run full test suite + mypy + ruff; confirm clean

## Dev Notes

### Bash Reference: lib/diff.sh `detect_conditional_agent_triggers`

The full bash implementation is at `lib/diff.sh:238–316`. Key logic:

| Gate | Trigger key | Bash variable | Kill-switch |
|---|---|---|---|
| Error patterns in diff | `has_error_patterns` | `HAS_ERROR_PATTERNS` | none (always evaluated) |
| Non-docs/non-meta files | `has_code_or_infra` | `RUN_ARCHITECTURE_REVIEWER` | `AI_DISABLE_GATE_ARCHITECTURE` |
| Sec keywords / paths | `has_security_patterns` | `RUN_SECURITY_REVIEWER` | `AI_DISABLE_GATE_SECURITY` |
| Control-flow added lines | `has_control_flow` | `RUN_EDGE_CASE_HUNTER` | `AI_DISABLE_GATE_EDGE_CASE` |
| No prior SHA (no watermark) | `no_prior_summary` | checked inline in review.sh | none |

`silent-failure-hunter` uses `has_error_patterns` (not a kill-switch gate, always evaluated).
`pr-summarizer` uses `no_prior_summary` (always skipped on incremental runs).
`architecture-reviewer` uses `has_code_or_infra`.
`security-reviewer` uses `has_security_patterns`.
`edge-case-hunter` uses `has_control_flow`.
`blind-hunter` and `adversarial-general` have `conditional_trigger=None` — always run.
`code-reviewer` has `conditional_trigger=None` — always run.

### Docs-only / meta exclusion logic (architecture gate)

Exact patterns from bash:
```
# Workflow files (infra, not docs)
grep -cE '(^|/)\.github/workflows/'

# Non-docs count:
grep -vE '(^|/)\.github/workflows/'       # keep non-workflow files
grep -vE '\.(md|markdown|txt|rst|adoc)$'  # strip doc extensions
grep -vE '(^|/)(docs|memory-bank|\.github|\.claude)/'  # strip meta dirs
grep -vE '(^|/)(CHANGELOG|README|LICENSE|NOTICE|AUTHORS|CONTRIBUTING|CODEOWNERS|CODE_OF_CONDUCT)(\..*)?$'
```
Architecture gate fires when `workflow_count > 0 OR nondocs_count > 0`.

### Security keyword regex (exact bash port)

```
auth|token|secret|password|crypt|hash|\bsign\b|verify|exec|eval|sql|
sanitize|escape|xss|csrf|cors|header|redirect|deserialize|cookie|session|
jwt|oauth|ldap|saml|rbac|acl|permission|privilege|sudo|chmod|chown|setuid|
x509|tls|ssl|cert|certificate|keystore|nonce|salt|hmac|aes|rsa|ecdsa|
pbkdf2|bcrypt|scrypt|curl|wget|\bsource\b|\bIFS\b|LD_PRELOAD|\$\{\{
```
Case-insensitive (`re.IGNORECASE`).

### Security path patterns (exact bash port)

```python
_SEC_PATH_PATTERNS = re.compile(
    r'(auth|passwords?|credentials?|tokens?|secrets?)'
    r'|(^|/)(?:api|routes?)/'
    r'|(^|/)(?:package\.json|package-lock\.json|go\.mod|go\.sum|'
    r'composer\.json|composer\.lock|requirements[^/]*\.txt|pyproject\.toml|'
    r'Pipfile(?:\.lock)?|Gemfile(?:\.lock)?|[Cc]argo\.(?:toml|lock)|'
    r'yarn\.lock|pnpm-lock\.yaml)$'
    r'|(^|/)\.env'
    r'|(^|/)settings\.(?:py|ya?ml|json|toml)$'
    r'|(^|/)(?:Dockerfile|Containerfile)'
    r'|\.(?:sh|bash)$'
    r'|(^|/)\.github/workflows/'
)
```

### Control-flow keywords

Only added lines matter (lines starting with `+` but NOT `+++`):
```
if|elif|else|for|while|do|case|switch|match|try|catch|except|rescue|
unless|when|loop|break|continue|return|goto|defer|finally
```
Word-boundary match (`\b`) required to avoid false positives inside identifiers.

### Function signatures

```python
from ai_pr_review.agents.roster import AgentSpec, ConditionalTrigger
from ai_pr_review.manifest import ChangedFiles

def evaluate_gates(
    diff_text: str,
    changed_files: ChangedFiles,
    env: Mapping[str, str],
    last_reviewed_sha: str | None = None,
) -> frozenset[ConditionalTrigger]: ...

def filter_agents(
    agents: list[AgentSpec],
    fired_gates: frozenset[ConditionalTrigger],
) -> list[AgentSpec]: ...
```

### Previous learnings from E2.S1–S3

- `from __future__ import annotations` at top of every module
- Frozen dataclasses for value objects; stdlib only (no pydantic)
- `re.compile()` at module level for patterns used repeatedly
- Tests: `pytest`, no async needed for this module
- `mypy --strict` with `from collections.abc import Mapping` (not `typing.Mapping`)

## Dev Agent Record

### Implementation Plan

_To be filled during implementation_

### Debug Log

_Empty_

### Completion Notes

Implemented `ai_pr_review/agents/gates.py` with all 5 gate functions and public API. Used `Mapping[str, str]`
for the env interface (no separate TypedDict needed). All 62 new tests pass alongside 355 total. mypy
--strict and ruff clean. Used `re.IGNORECASE` flag on security keyword pattern; `\b` word-boundary on
control-flow keywords prevents false positives from `ifdef`/`foreach`. Kill-switch values accept `"true"`
or `"1"` (case-insensitive), matching bash convention.

## File List

- `ai_pr_review/agents/gates.py` (new)
- `tests/python/agents/test_gates.py` (new)
- `memory-bank/bmad/stories/2-4-conditional-gates.md` (this file)

## Change Log

- 2026-05-12: Created E2.S4 story — Conditional gates port.
