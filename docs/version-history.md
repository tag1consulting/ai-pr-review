---
layout: default
title: Version History
nav_order: 7
has_children: true
render_with_liquid: false
---

# Version History

What changed in each release, newest first. The 10 most recent versions each have their own page; everything before that is combined into [Older releases](version-history/archive).

| Version | Highlights |
|---------|-----------|
| [v2.5.0](version-history/v2.5.0) | `policy.yml` review-depth routing + merge gate; 6 dogfooding bug fixes |
| [v2.4.9](version-history/v2.4.9) | Asimov's Three Laws stated explicitly in governance; feedback-injection security fix |
| [v2.4.8](version-history/v2.4.8) | Silent `APPROVE`→`COMMENT` review degrade fixed |
| [v2.4.7](version-history/v2.4.7) | `max_tokens_per_agent` default raised back to 32768 |
| [v2.4.6](version-history/v2.4.6) | "Overall Risk" headline no longer contradicts the review decision |
| [v2.4.5](version-history/v2.4.5) | Merge-commit filter and unbounded fallback diffs fixed |
| [v2.4.4](version-history/v2.4.4) | Slash-command reviews post as `github-actions[bot]`, not the PAT owner |
| [v2.4.3](version-history/v2.4.3) | `pr-number`/`issue-number` split-input false positive fixed |
| [v2.4.2](version-history/v2.4.2) | `false-positive`/`wont-fix` dismiss the owning review; auto-approve on full resolution |
| [v2.4.1](version-history/v2.4.1) | Sonnet 5 `max_tokens` crash fix; live-API model canary added |

See [Older releases](version-history/archive) for v2.4.0 and earlier, back to v0.7.0.

For the underlying commit-level changelog, see [CHANGELOG.md](https://github.com/tag1consulting/ai-pr-review/blob/main/CHANGELOG.md) on GitHub.
