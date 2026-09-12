# Consistency-eval corpus

Frozen diffs used by `tests/canary/consistency_eval.py` to measure run-to-run
review-verdict stability (Epic 9, issue #799 / #800). Chosen for language and
risk diversity, not for platform diversity: `DispatchContext`/`orchestrate.py`
carry no VCS-platform-specific behavior into the finding-agent content path
(confirmed by direct code reading — see the comment thread on issue #800), so
a GitHub-vs-GitLab-vs-Bitbucket dimension would add corpus slots without
adding signal. Every file here is replayed offline through the real dispatch
pipeline; nothing here is fetched live or posted anywhere.

| File | Provenance | Why it's here |
|---|---|---|
| `01_stress_python_large.diff` | Copy of `tests/canary/stress_diff.txt` | The existing "genuinely complex diff" standard used for model verification (see CLAUDE.md's model-change section). Large, real, Python-heavy. |
| `02_scolta_workflow_original_variance.diff` | Copy of `tests/canary/scolta_workflow_diff.txt` | The exact diff shape that first revealed the 10-PRs-5-verdicts variance this harness exists to measure. Historically load-bearing — keep even if it looks redundant with other Python fixtures. |
| `03_python_small_security_fix.diff` | `git show 02d098d` (real, merged) | Small (18 lines), security-adjacent (Semgrep false-positive suppression in `phpstan.py`). |
| `04_json_config_only.diff` | `git show f089e28` (real, merged) | Config-only change, zero source code — tests whether agents produce noise on a diff with nothing to review. |
| `05_python_medium_with_tests.diff` | `git show b9dcab5` (real, merged) | Medium-sized real bug fix touching source + tests + CHANGELOG — a typical well-formed PR shape. |
| `06_python_medium_fingerprint_logic.diff` | `git show c0b862f` (real, merged) | Real bug fix in fingerprint-matching logic (`slash/dismiss.py`) plus two test files — denser logic than #05. |
| `07_ci_yaml_only.diff` | `git show 1ecb645` (real, merged) | CI/YAML-only change, no Python source — tests the architecture-reviewer/infra-trigger path on a realistic small CI diff. |
| `08_python_large_security_hardening.diff` | `git show 038ece3` (real, merged) | Large (700+ lines), explicitly security-motivated (hardening analyzers against fork-controlled config), spans Python + CI + docs. |
| `09_docs_only_tiny.diff` | `git show d5d13d2` (real, merged) | Markdown-only, trivial (8 lines changed). The other end of the size spectrum from #01/#08. |
| `10_go_error_handling_synthetic.diff` | Synthetic, hand-written | This repo has no Go source of its own to draw a real diff from. New-file diff containing two documented Go anti-patterns from `language-profiles/go.md` (unchecked `Close()` error, unchecked type assertion) alongside one correctly-wrapped error path. **Content unchanged — an attempted fix did not hold under full-scale re-verification, see "Opus 5 refusal" below.** |
| `11_typescript_type_safety_synthetic.diff` | Synthetic, hand-written | New-file diff: an `as any` cast bypassing type safety before an unguarded property access, and a loose `==` equality comparison. Revised 2026-09-12 (originally `11_typescript_async_error_swallow_synthetic.diff`), fix verified 5/5 on Opus 5, see "Opus 5 refusal" below. |
| `12_php_xss_sqli_synthetic.diff` | Synthetic, hand-written | New-file diff: a correctly-parameterized PDO query directly alongside an unescaped-output XSS and a string-interpolated SQL injection in the same class — the kind of inconsistent-quality code real PRs actually contain. **Deliberately left unchanged despite failing on Opus 5 unconditionally** — see "Opus 5 refusal" below. |
| `13_terraform_security_misconfig_synthetic.diff` | Synthetic, hand-written | New-file diff: a publicly-accessible RDS instance with a hardcoded plaintext password, plus a security group open to `0.0.0.0/0` on the DB port. Revised 2026-09-12 (the public-read S3 bucket moved to #14), fix verified 5/5 on Opus 5, see "Opus 5 refusal" below. |
| `14_terraform_public_bucket_synthetic.diff` | Synthetic, hand-written | New-file diff: an S3 bucket with a `public-read` ACL. Split out of #13 2026-09-12, fix verified 5/5 on Opus 5, see "Opus 5 refusal" below. |

Synthetic diffs are grounded in the bug patterns this repo's own
`language-profiles/*.md` files document, not invented from nothing — this
keeps them representative of what the review agents' own prompts already
expect to catch, rather than testing against an ungrounded guess at what a
"typical" Go/TS/PHP/Terraform bug looks like.

## Opus 5 refusal on synthetic fixtures (issue #810)

The first full baseline run (2026-09-12) found `claude-opus-5` returning a
hard `stop_reason=refusal` on 100% of runs against all 4 original synthetic
fixtures (10-13), while `claude-sonnet-5` handled all of them cleanly and
every real-history fixture (01-09) passed on both models. An initial
diagnostic pass (~30 targeted single-diff live tests, 2-3 runs each, see
#810 for the full log) isolated content down to individual
functions/resources and produced four candidate fixes. A full 5-run
re-verification of all four (matching the harness's real protocol, not the
cheaper 2-3-run spot checks used to generate them) found:

- **TypeScript (#11) and Terraform (#13, split into #13+#14): fixed,
  confirmed 5/5 on Opus 5.** For TypeScript, the specific bug pattern
  mattered: a fire-and-forget async call with no `await`/`.catch` triggered
  the refusal on its own, in isolation, regardless of naming or domain
  framing (tested under both an "audit event" and an unrelated "cache
  invalidation" scenario) — this documented bug pattern could not be
  preserved, so it was replaced with two different documented TypeScript
  bugs (unsafe `any` cast, loose `==` equality). For Terraform, the trigger
  was a *combination* of the S3-bucket-with-RDS pairing specifically (each
  resource passed alone, and RDS+security-group passed together); moving
  the S3 bucket to its own fixture (#14) fixed both halves.
- **Go (#10): attempted fix did not hold, left unchanged.** A 2-run spot
  check of "drop the one correctly-written function, keep both bugs"
  passed 2/2 and looked like the same kind of combination-trigger as
  Terraform. It failed 5/5 on the full re-verification run. This is the
  reason the fix is not adopted here despite initially appearing to work:
  **the refusal rate is not perfectly deterministic per exact byte content,
  so a 2-3 run pass is not reliable evidence of a fix** — only a full
  5-run (or larger) confirmation should be trusted before adopting a
  content change made specifically to route around this behavior. Given
  that lesson landed *after* the TypeScript/Terraform fixes were already
  confirmed at 5/5 (i.e. those two are on solid footing), and given the
  cost of continuing to search for a Go variant that holds up, #10 is left
  in its original 3-function form: it fails on Opus 5 either way, and the
  original form keeps its full documented-bug-pattern coverage rather than
  a partial, equally-broken rewrite.
- **PHP (#12): unfixable while preserving its purpose, left unchanged.**
  A single, isolated instance of either the XSS or the SQL-injection
  pattern alone triggers the refusal unconditionally — confirmed across
  7 variants regardless of file size, surrounding class complexity, or
  dilution into a larger, more realistic file. A single *non*-injection
  PHP bug (e.g. loose equality) passes fine; it is specifically
  injection-vulnerability content that Opus 5 refuses. Since this
  fixture's entire purpose is testing injection-vulnerability detection,
  it is left unchanged rather than diluted into something else.

**Both #10 and #12 will always report zero Opus 5 data in this corpus** —
a measured, documented limitation, not a bug in the harness or the corpus.
Treat any Opus-arm result that excludes diffs #10 and #12 as expected, not
as missing data to chase.

Two separate things are true at once here, and worth not conflating: (1)
Opus 5's refusal classifier is genuinely sensitive to certain multi-element
synthetic-code shapes (confirmed removable for TS/Terraform) and
unconditionally sensitive to injection-vulnerability content specifically
(confirmed unremovable for PHP, and — on the evidence available — for Go's
exact combination too, though Go's failure mode looks more like the
"combination" class than the "content" class and may simply need a
different combination than the one tried). (2) The refusal has enough
run-to-run variance that small samples can look like a fix when they
aren't one. Point (2) is the one to remember before trusting any future
attempt to route around point (1): re-verify at the harness's real run
count, not a cheaper stand-in, before adopting a content change on the
strength of it. Neither behavior is under this repo's control, and both are
worth keeping in mind for future corpus fixture design — real production
PRs that introduce actual XSS/SQLi, or that happen to match whatever
Go/Terraform's combination trigger actually is, may see the same Opus 5
refusal independent of this corpus. See #810 for the production-facing
mitigation this motivated: `agents/dispatch.py`'s `_run_single_agent` now
retries a premium-model content-filter refusal once on the standard model
before giving up on that agent for the run.

## Regenerating a real-history entry

If a frozen real-history diff needs to be swapped (e.g. the underlying commit
is reverted or its content becomes misleading), regenerate with:

```
git show --format= <sha> > tests/canary/corpus/<NN>_<name>.diff
```

`--format=` strips the commit-message header so the file is a pure diff,
matching the format `git diff <base>...HEAD` produces (which is what
`DispatchContext.diff_path` is populated with in production).
