# Story 15.2: Category Coverage for Static Analyzer Findings

**Epic:** 15 — Category-field follow-ups
**Story ID:** 15-2
**Story Key:** 15-2-analyzer-category-coverage
**GitHub Issue:** [#579](https://github.com/tag1consulting/ai-pr-review/issues/579)
**Status:** done

---

## Story

As a **maintainer**,
I want each native static analyzer in `ai_pr_review/analyzers/native/` to set an explicit, defensible `category` on the findings it constructs,
so that `category` reflects real signal across the whole findings surface, not just the 5 LLM agent prompts, and downstream category-aware dedup (story 15-1 / #578) works correctly for analyzer findings too.

---

## Acceptance Criteria

1. Every analyzer with a clean, unconditional category mapping sets `category` explicitly at its `Finding(...)` construction site: `trufflehog` → `secret`, `cve_check` → `dependency-cve`, `phpcs`/`ruff`/`eslint`/`hadolint`/`shellcheck`/`golangci_lint` → `lint`.
2. `checkov` sets `category="secret"` for `CKV_SECRET_*` check IDs, `category="authz"` for `CKV2_*` check IDs (cross-resource access/exposure policy violations, already treated as High severity — post-review refinement, recorded 2026-07-07: the original plan of `lint` for all non-secret checks would have demoted these security-relevant findings out of the corroboration path), `category="lint"` for everything else.
3. `kube-linter` sets `category="authz"` for checks in its existing `_HIGH_SEVERITY_CHECKS` set (privilege/access-boundary checks), `category="lint"` for everything else. `tflint` sets `category="lint"` unconditionally.
4. `phpstan` sets `category="lint"` (type errors treated as lint-class findings for this pass — see Dev Notes for the rejected alternative).
5. `semgrep` sets `category` from a per-rule mapping using `extra.metadata.category`/`cwe`/`check_id` substring heuristics already available in the tool's JSON output, falling back to `"other"` when no metadata is present (e.g. community rules under `--config=auto`).
6. No existing analyzer test regresses; each analyzer gets at least one new test asserting the resulting `Finding.category` value(s).
7. `pytest tests/python -q`, `mypy ai_pr_review/`, `ruff check ai_pr_review/ tests/python/` all pass.

---

## Out of Scope

- Adding a new `Category` enum value (e.g. `infra-misconfig`) for infra/cloud-config findings that don't cleanly fit the existing 11-value taxonomy. Explicit decision (recorded 2026-07-07): approximate onto `authz`/`lint` instead, since the enum is shared/ported from `claude-comprehensive-review#76` and adding a value there would require cross-repo coordination beyond this story's scope.
- Any change to `merge.py`'s dedup logic — that's story 15-1 / #578, in the same epic, landing separately.

---

## Technical Notes

### Verified analyzer inventory and mapping rationale

All 13 non-empty files in `ai_pr_review/analyzers/native/`, confirmed via full-file reads; **none currently set `category`** (all fall to the `Finding` model's `"other"` default):

| Analyzer | Finding content (verified) | Category | Confidence |
|---|---|---|---|
| `trufflehog.py` | Secret/credential detection only | `secret` | High — clean fit |
| `cve_check.py` (source=`osv`) | Dependency vulnerabilities from OSV.dev | `dependency-cve` | High — clean fit |
| `phpcs.py` | PHP coding-standard violations | `lint` | High |
| `ruff.py` | Python lint | `lint` | High |
| `eslint.py` | JS/TS lint | `lint` | High |
| `hadolint.py` | Dockerfile lint | `lint` | High |
| `shellcheck.py` | Shell script lint | `lint` | High |
| `golangci_lint.py` | Go meta-linter (errcheck/govet/staticcheck flagged High, rest Medium) | `lint` | Medium — `errcheck`/`govet`/`staticcheck` are arguably correctness-risk, not style, but treated as lint-class per this repo's existing naming/severity convention |
| `checkov.py` | IaC policy violations; `CKV_SECRET_*` prefix = hardcoded secrets in IaC; `CKV2_*` prefix = cross-resource "graph checks" (exposed security groups, publicly-readable storage, etc. — access/exposure policy, not style) | `secret` (`CKV_SECRET_*`) / `authz` (`CKV2_*`) / `lint` (everything else, e.g. `CKV_AWS_*` style checks) | High for secret and CKV2/authz sub-cases (post-review refinement, recorded 2026-07-07 — see below); Medium/approximated for the `lint` remainder (see enum-gap decision above) |
| `kube-linter.py` | K8s manifest checks; `_HIGH_SEVERITY_CHECKS` = privilege-escalation/host-network/privileged-container/sensitive-host-mounts etc. | `authz` (in `_HIGH_SEVERITY_CHECKS`) / `lint` (everything else) | Medium — approximated per enum-gap decision |
| `tflint.py` | Terraform lint (aws ruleset) | `lint` | Medium — approximated per enum-gap decision |
| `phpstan.py` | PHP static type errors, severity by `PHPSTAN_LEVEL` | `lint` | Low-Medium — genuinely closer to `edge-case` (null-safety/type-narrowing) than `lint`, but `lint` chosen for consistency with the other static-analysis tools in this pass; revisit if this reads wrong in practice |
| `semgrep.py` | Multi-purpose SAST spanning injection/authz/secrets/style depending on ruleset | Per-rule via `extra.metadata` | Medium — mechanism is solid (real per-finding data already read at `semgrep.py:129-131`, just not mapped to `category` yet), exact rule-id/CWE→category table needs authoring and will likely need iteration |

### `Category` enum (unchanged, from `ai_pr_review/findings/models.py:22-35`)

`authz | injection | dependency-cve | secret | architecture-coupling | test-gap | edge-case | observability | docs | lint | other` (11 values, `CATEGORIES = get_args(Category)`). `_normalise_category` lowercases/strips and silently falls back to `"other"` on anything unrecognized — analyzers can pass the literal string values directly.

### Cross-cutting dependency on story 15-1 / #578

The wildcard rule in 15-1 (`"other"` never blocks a merge) **must land before or together with** this story's category-setting changes. If 15-2 ships first without the wildcard rule in place, analyzer+agent corroboration (`_collapse_cluster`'s `is_corroborated` check) can silently break wherever an analyzer's new real category doesn't match an agent's self-reported category for the same underlying finding. Sequence: land 15-1 first, or verify the wildcard rule is present before merging 15-2.

### `semgrep` per-rule mapping — rejected alternative

Considered: a single static category for semgrep (like every other analyzer in this story). Rejected because semgrep's ruleset is explicitly multi-purpose (the tool already reads `extra.metadata` for `references` at `semgrep.py:129-131`) and a single value would misclassify a large fraction of its findings — e.g. a `p/owasp-top-ten` SQL-injection rule and a `p/ci` style rule would both get the same category, defeating the point of this story for the analyzer most capable of fine-grained categorization.

---

## Tasks

- [x] `trufflehog.py`, `cve_check.py`, `phpcs.py`, `ruff.py`, `eslint.py`, `hadolint.py`, `shellcheck.py`, `golangci_lint.py`, `tflint.py` — set the single static `category` value at each `Finding(...)` construction site (see mapping table).
- [x] `checkov.py` — branch `category` on `CKV_SECRET_*` prefix vs. everything else.
- [x] Post-review refinement: `checkov.py` also branches `CKV2_*` (cross-resource graph checks, already High severity) to `category="authz"` instead of falling into the `lint` bucket, so these security-relevant findings stay eligible for analyzer+agent corroboration. Added `test_ckv2_maps_to_authz_category`.
- [x] `kube_linter.py` — branch `category` on membership in the existing `_HIGH_SEVERITY_CHECKS` set.
- [x] `phpstan.py` — set `category="lint"`.
- [x] `semgrep.py` — extend the existing `metadata = extra.get("metadata") or {}` read to also pull `category`/`cwe`, plus a `check_id` substring fallback (e.g. `sql-injection`/`xss` → `injection`, `secrets` → `secret`), defaulting to `"other"` when no signal is present.
- [x] Add/extend one test per analyzer (`tests/python/test_analyzer_*.py`) asserting the resulting `category`, including semgrep's metadata-present and metadata-absent cases.
- [x] Run `pytest tests/python -q`, `mypy ai_pr_review/`, `ruff check ai_pr_review/ tests/python/`.
- [ ] Open PR referencing #579; note the enum-gap approximation decision (authz/lint for infra tools) explicitly in the PR body so reviewers understand it's a deliberate tradeoff, not an oversight.

---

## Dev Notes

Scoping investigation (Explore agent, full reads of all 13 analyzer files + `models.py`) confirmed the complete construction-site line numbers and the absence of any existing category assertions in the 13 flat `tests/python/test_analyzer_*.py` files — this is genuinely new coverage, not a regression risk against existing category tests.
