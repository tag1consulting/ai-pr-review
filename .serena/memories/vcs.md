# VCS Provider Layer

## Key files
- `ai_pr_review/vcs/protocol.py` — `VcsProvider` abstract base; `DiffContext`, `SummaryResult`, `FindingsResult`, `StaleResult`, `PostEvent`
- `ai_pr_review/vcs/github.py`, `gitlab.py`, `bitbucket.py` — concrete implementations
- `ai_pr_review/vcs/http.py` — shared async HTTP helpers
- `ai_pr_review/vcs/marker.py` — SHA watermark (tracks which commits have been reviewed)
- `ai_pr_review/vcs/_stale.py` — stale thread resolution logic
- `ai_pr_review/vcs/_inline.py` — inline comment posting
- `ai_pr_review/vcs/_body.py` — body/summary comment upsert
- `ai_pr_review/vcs/_finding_ids.py` — stable `F<n>` ID assignment for findings

## Bash equivalents
- `post-review.sh` — GitHub API layer
- `post-review-gitlab.sh` — GitLab API layer
- `post-review-bitbucket.sh` — Bitbucket Cloud API layer
- `vcs/common.sh` — shared helpers (severity_icon, format_body_finding, build_agent_prompt, parse_valid_lines, etc.)

## Key behaviors
- SHA watermark prevents re-reviewing the same commit; advanced after each successful post.
- Stale bot review threads are resolved/dismissed on each new push.
- GitHub: requires `GH_TOKEN` PAT (not `GITHUB_TOKEN`) for `resolveReviewThread` GraphQL mutation.
- Bitbucket: upserts a single summary comment containing all findings.
- GitLab: upserts summary note + posts inline MR discussions with suggestion fences.
