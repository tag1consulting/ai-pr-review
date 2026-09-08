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
| [v2.9.0](version-history/v2.9.0) | Token usage moved out of the review comment by default; container-action env passthrough gaps closed |
| [v2.8.0](version-history/v2.8.0) | GitLab cross-run finding dedup; canonical-review empty-body/dismiss fixes; Opus 5 default |
| [v2.7.0](version-history/v2.7.0) | Canonical-review reuse (GitHub); phpstan/checkov/tflint fork-workspace hardening |
| [v2.6.1](version-history/v2.6.1) | Bitbucket ownership/watermark markers no longer render as visible text |
| [v2.6.0](version-history/v2.6.0) | Four documentation-checking static analyzers; `golangci-lint`/`phpcs` fixes |
| [v2.5.0](version-history/v2.5.0) | `policy.yml` review-depth routing + merge gate; 6 dogfooding bug fixes |
| [v2.4.9](version-history/v2.4.9) | Asimov's Three Laws stated explicitly in governance; feedback-injection security fix |
| [v2.4.8](version-history/v2.4.8) | Silent `APPROVE`→`COMMENT` review degrade fixed |
| [v2.4.7](version-history/v2.4.7) | `max_tokens_per_agent` default raised back to 32768 |
| [v2.4.6](version-history/v2.4.6) | "Overall Risk" headline no longer contradicts the review decision |

See [Older releases](version-history/archive) for v2.4.5 and earlier, back to v0.7.0.

For the underlying commit-level changelog, see [CHANGELOG.md](https://github.com/tag1consulting/ai-pr-review/blob/main/CHANGELOG.md) on GitHub.
