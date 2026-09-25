---
layout: default
title: Version History
nav_order: 8
has_children: true
render_with_liquid: false
---

# Version History

What changed in each release, newest first. The 10 most recent versions each have their own page; everything before that is combined into [Older releases](version-history/archive).

| Version | Highlights |
|---------|-----------|
| [v2.14.0](version-history/v2.14.0) | Bitbucket learning-loop store (#906): `false-positive`/`wont-fix` verdicts now persist and feed future reviews; found and fixed several follow-up issues before and after tagging |
| [v2.13.2](version-history/v2.13.2) | Compute-phase skip crash fixed (#927); Bitbucket brand-new-summary-comment race fixed (#930) |
| [v2.13.1](version-history/v2.13.1) | Two v2.13.0 release-doc gaps fixed (stale approval-ceiling wording, missing Behavior-change label); docs-only |
| [v2.13.0](version-history/v2.13.0) | Bitbucket reaches finding-lifecycle parity (dedup, Code Insights, verdict polling); `approval-ceiling`; 5 security fixes (#886/#887/#894/#913/#914); slash-command input-forwarding gaps closed |
| [v2.12.0](version-history/v2.12.0) | PR title/description now visible to review agents; GitLab findings update in place; per-agent token budgets; pre-flight cost ceiling; judge-verdict persistence |
| [v2.11.0](version-history/v2.11.0) | GitLab now renders out-of-diff findings instead of dropping them; `REVIEW_TARGET` case-sensitivity fixed; phpstan Semgrep false positive fixed |
| [v2.10.0](version-history/v2.10.0) | Dropped-verdict bug fixed; resolved-without-verdict duplicate comments now explained instead of silently reposted; silent slash-command failures now signal clearly |
| [v2.9.0](version-history/v2.9.0) | Token usage moved out of the review comment by default; container-action env passthrough gaps closed |
| [v2.8.0](version-history/v2.8.0) | GitLab cross-run finding dedup; canonical-review empty-body/dismiss fixes; Opus 5 default |
| [v2.7.0](version-history/v2.7.0) | Canonical-review reuse (GitHub); phpstan/checkov/tflint fork-workspace hardening |

See [Older releases](version-history/archive) for v2.6.1 and earlier, back to v0.7.0.

For the underlying commit-level changelog, see [CHANGELOG.md](https://github.com/tag1consulting/ai-pr-review/blob/main/CHANGELOG.md) on GitHub.
