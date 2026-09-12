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
| `10_go_error_handling_synthetic.diff` | Synthetic, hand-written | This repo has no Go source of its own to draw a real diff from. New-file diff containing two documented Go anti-patterns from `language-profiles/go.md` (unchecked `Close()` error, unchecked type assertion) alongside one correctly-wrapped error path, so the diff isn't trivially "everything is wrong." |
| `11_typescript_async_error_swallow_synthetic.diff` | Synthetic, hand-written | New-file diff: a fire-and-forget async call with no `await`/`.catch` (silently swallows a rejection), and an `as any` cast bypassing type safety before an unguarded property access. |
| `12_php_xss_sqli_synthetic.diff` | Synthetic, hand-written | New-file diff: a correctly-parameterized PDO query directly alongside an unescaped-output XSS and a string-interpolated SQL injection in the same class — the kind of inconsistent-quality code real PRs actually contain. |
| `13_terraform_security_misconfig_synthetic.diff` | Synthetic, hand-written | New-file diff: public-read S3 ACL, a publicly-accessible RDS instance with a hardcoded plaintext password, and a security group open to `0.0.0.0/0` on the DB port. |

Synthetic diffs are grounded in the bug patterns this repo's own
`language-profiles/*.md` files document, not invented from nothing — this
keeps them representative of what the review agents' own prompts already
expect to catch, rather than testing against an ungrounded guess at what a
"typical" Go/TS/PHP/Terraform bug looks like.

## Regenerating a real-history entry

If a frozen real-history diff needs to be swapped (e.g. the underlying commit
is reverted or its content becomes misleading), regenerate with:

```
git show --format= <sha> > tests/canary/corpus/<NN>_<name>.diff
```

`--format=` strips the commit-message header so the file is a pure diff,
matching the format `git diff <base>...HEAD` produces (which is what
`DispatchContext.diff_path` is populated with in production).
