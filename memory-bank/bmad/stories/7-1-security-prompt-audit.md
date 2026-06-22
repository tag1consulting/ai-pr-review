# Story 7.1: Security-Reviewer Prompt Audit Against Anthropic Security-Guidance Plugin

**Epic:** 7 — Review-Quality & Perf Research
**Story ID:** 7-1
**Story Key:** 7-1-security-prompt-audit
**GitHub Issue:** #369
**Parent Epic Issue:** #362
**Status:** done

---

## Story

As a **maintainer**,
I want the security-reviewer prompt to reflect Anthropic's current security-review thinking,
so that our review catches vulnerability categories the plugin covers that we currently miss.

---

## Acceptance Criteria

1. The security-guidance plugin's built-in checklist is fetched from `https://github.com/anthropics/claude-plugins-official/tree/main/plugins/security-guidance` and compared against `prompts/security-reviewer.md`.
2. Gaps are documented (even if the decision is "intentionally excluded" with a reason).
3. `prompts/security-reviewer.md` is updated with any adopted improvements.
4. The existing version-hallucination guardrail (the **Do NOT flag version-related findings** block) is preserved verbatim — do not weaken or remove it.
5. `CHANGELOG.md` has an entry noting the prompt improvement.
6. All existing tests pass (no structural changes to the output format or `json-findings` contract).

---

## Implementation Tasks

- [x] **Task 1 — Fetch the plugin source** (AC: 1)
  - [x] Check the directory listing at `https://github.com/anthropics/claude-plugins-official/tree/main/plugins/security-guidance` to identify the correct prompt/checklist file name.
  - [x] WebFetch the raw content — source is in `hooks/patterns.py` (25 regex rules) and `hooks/review_api.py` (LLM prompt in `analyze_code_security`).

- [x] **Task 2 — Read our current prompt** (AC: 1)
  - [x] Read `prompts/security-reviewer.md` in full.
  - [x] Note covered sections: OWASP categories (Secrets, AuthN/AuthZ, Injection, Data Handling, Crypto, Supply Chain), per-language checks (Go/Python/TS/PHP/Shell), trust-boundary awareness, confidence scoring, version-hallucination guardrail.

- [x] **Task 3 — Produce gap analysis** (AC: 2)
  - [x] List every check in the plugin not covered by our prompt, grouped by category.
  - [x] For each gap, decide: adopt | intentionally-exclude (with written reason).
  - [x] Write the gap analysis as a comment block at the top of the Dev Agent Record Completion Notes in this story file.

- [x] **Task 4 — Update `prompts/security-reviewer.md`** (AC: 3, 4)
  - [x] Add adopted checks to the relevant section of the prompt.
  - [x] Preserve the version-hallucination guardrail block (the **Do NOT flag version-related findings** section with its four bullet exceptions) verbatim. Do not alter or remove it.
  - [x] Do not change the output format (`json-findings` block structure, severity/confidence fields, `source` must be `"security-reviewer"`).

- [x] **Task 5 — CHANGELOG entry** (AC: 5)
  - [x] Add a bullet to `CHANGELOG.md` under the Unreleased section.

- [x] **Task 6 — Verify tests** (AC: 6)
  - [x] Run `pytest tests/python -q` — 1,606 passed in 20.90s.
  - [x] Run `ruff check prompts/` — no-op for .md files, as expected.

---

## Dev Notes

### Critical File Locations

| File | Purpose |
|------|---------|
| `prompts/security-reviewer.md` | The prompt to update |
| `CHANGELOG.md` | Release notes |
| `https://github.com/anthropics/claude-plugins-official/tree/main/plugins/security-guidance` | Plugin source to compare against |

### Version-Hallucination Guardrail (MUST NOT be weakened)

The guardrail at `prompts/security-reviewer.md` (the `**Do NOT flag** any package, runtime...` block) was added to fix issue #139. Any text adopted from the plugin that would encourage flagging version existence must be excluded or reframed to exclude version-existence claims.

### What NOT to Do

- **Do not change the output format** — `json-findings` block structure, severity values, confidence field, `source: "security-reviewer"` must remain unchanged.
- **Do not remove scope boundaries** — the `## Scope Boundaries` section delegating error-handling to silent-failure-hunter and architecture to architecture-reviewer must remain.
- **Do not weaken the version guardrail** — even if the plugin says "flag suspicious dependencies," that does not override the version-hallucination fix.

---

## Dev Agent Record

### Agent Model Used

claude-opus-4-8

### Debug Log References

_none_

### Completion Notes List

**Gap analysis — completed 2026-06-22**

Sources analyzed:
- `patterns.py` (25 regex rules) in `anthropics/claude-plugins-official/plugins/security-guidance/hooks/`
- LLM review prompt in `review_api.py` (`analyze_code_security`, 10,507 chars)

**Adopted gaps** (added to `prompts/security-reviewer.md`):

1. **SSRF** — outbound HTTP with user-controlled URLs; cloud metadata endpoint coverage (169.254.x.x). `patterns.py` listed as a sink; LLM prompt treats it as Phase 1 sink. Added to Injection section.
2. **LLM prompt injection** — user-controlled content interpolated into prompts sent to language models. From LLM prompt "sinks" list. Added to Injection section (especially relevant to this tool itself).
3. **Gate/action field mismatch** — authorization check reads different field than downstream operation uses. From LLM prompt Phase 2c `GATE/ACTION FIELD MISMATCH`. Added to Auth section.
4. **IaC omitted arg** — Terraform/Pulumi/CDK module instantiation omitting security-relevant optional args. From LLM prompt Phase 2c `IaC OMITTED ARG`. Added as new "Infrastructure-as-Code Security" section.
5. **GitHub Actions workflow injection** — untrusted context expressions (`github.event.pull_request.title`, etc.) interpolated into `run:` steps. From `patterns.py` rule `github_actions_injection`. Added to language-specific checks and IaC section.
6. **XXE via Python stdlib XML parsers** — `ElementTree`, `minidom`, `xml.sax` vulnerable by default. From `patterns.py` rule `xml_unsafe_parse`. Added as new "XML External Entity (XXE)" section.
7. **DOM XSS sinks** — `outerHTML =`, `insertAdjacentHTML`, `document.write`. From `patterns.py` rules `dom_xss_*`. Added to TypeScript/JavaScript language checks. (We had `dangerouslySetInnerHTML` but not the others.)
8. **AES ECB mode** — specific `MODE_ECB`/`modes.ECB` check. From `patterns.py` rule `aes_ecb_mode`. Added to Cryptographic Issues section. (We had "ECB mode" generally; now explicit.)
9. **Node.js `createCipher`/`createDecipher`** — deprecated, no IV, removed in Node 22. From `patterns.py` rule `node_crypto_no_iv`. Added to TypeScript/JavaScript checks and Cryptographic Issues.
10. **Go shell-invocation pattern** — `exec.Command("sh", "-c", ...)`. From `patterns.py` rule `go_exec_shell`. Added to Go language checks. (We had `exec.Command` injection generally but not the specific shell-invocation form.)
11. **Extended Python deserialization** — `marshal.loads`, `shelve.open`, `joblib.load`, `pandas.read_pickle`, `numpy.load(..., allow_pickle=True)`. From `patterns.py` rules `python_marshal`/`python_shelve`/`python_pickle_wrappers`. Added new "Unsafe Deserialization" section and Python language check. (We had `pickle.loads` but not these variants.)
12. **ML model unsafe loading** — `torch.load` without `weights_only=True`. From `patterns.py` rule `ml_unsafe_load`. Added to "Unsafe Deserialization" section.
13. **Parser/validator differentials** — unanchored regexes, URL parser disagreements, encoding normalization mismatches. From LLM prompt Phase 2b. Added as new "Parser and Validator Differentials" section.
14. **PII in logs on error branches** — explicit note that happy-path redaction is often bypassed on `except` paths. From LLM prompt Phase 2c `SENSITIVE-TO-OBSERVABILITY`. Strengthened existing PII-in-logs check.

**Intentionally excluded:**

- **`pull_request_target` trigger without `branches:` filter** — `patterns.py` has `gh_actions_pwn_request` but we already partially cover this via "GitHub Actions inputs" as untrusted entry points. The specific `pull_request_target` case is now covered under the IaC/IaC section without repeating the full pattern logic.
- **SRI missing on external scripts** — `patterns.py` rule `missing_sri`. Intentionally omitted: SRI is HTML/template content, rarely in diffs this tool reviews (it focuses on application code). The signal-to-noise ratio would be poor for most PRs. Low-confidence findings are already filtered at the output stage (confidence >= 75).
- **`yaml.unsafe_load`** — already covered implicitly via `yaml.load` vs `safe_load` in the Python checks. `yaml.unsafe_load` is functionally identical; adding it would be noise.
- **IPC receivers (main/privileged process handling messages from sandboxed/renderer)** — From LLM prompt Phase 1. Very niche (Electron-style architecture). Would produce near-zero findings in typical PR reviews; intentionally omitted to avoid speculative findings.
- **Cache poisoning (cache-control/Vary headers)** — From LLM prompt Phase 1. High false-positive risk when flagging without full application context. Excluded; would require HTTP response header analysis outside typical diff scope.
- **Fail-open state drift / control regression / security registry fanout** — From LLM prompt Phase 2c. These are meta-patterns that require deep code-reading across many files to assess. At our prompt's operating context (diff + limited cross-file lookup), these would produce speculative findings. The silent-failure-hunter already covers fail-open for error handling specifically.
- **Stale identity mapping / over-broad grant** — From LLM prompt Phase 2c. Authorization design concerns; better suited to the architecture-reviewer per our scope boundaries.
- **Resource-bound placement (DoS as logic error)** — From LLM prompt Phase 2c. Excluded: complex to assess from a diff alone, and the focus is "cap on wrong accumulator" which is deeper than what a single-pass review can reliably catch.

**Version-hallucination guardrail:** preserved verbatim (lines 57-65 of original; now lines 58-68 of updated file).

### File List

- `prompts/security-reviewer.md` (modified)
- `CHANGELOG.md` (modified)
