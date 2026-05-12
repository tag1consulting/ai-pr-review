---
adr_id: "0001"
title: "Learning-store backend: committed file on bot-managed branch"
status: accepted
date: 2026-05-11
decider: Greg Chaix
related:
  - /home/gchaix/.claude/plans/please-plan-a-major-elegant-pudding.md (Epic 3)
  - memory-bank/bmad/planning-artifacts/prd-ai-pr-review.md (3.FR-C3, 3.FR-C4)
  - memory-bank/bmad/planning-artifacts/architecture-ai-pr-review.md (Slash Commands & Learning Store)
supersedes: null
superseded_by: null
---

# ADR-0001 — Learning-store backend

## Status

**Accepted** (2026-05-11). Gating deliverable for Epic 3.

## Context

Epic 3 ships an expanded slash-command surface with a learning loop. When a user posts `/ai-pr-review false-positive <reason>` or `/wont-fix <reason>` on an inline finding, the tool must persist `{rule_id, file_pattern, snippet, reason, user, sha, event_type, timestamp}` somewhere that:

1. Can be read back on every subsequent review of the same repo to inject learnings into agent prompts (`<repo-feedback>` block).
2. Works inside the existing VCS-agnostic, container-only distribution model.
3. Does not introduce new infrastructure that Tag1 must operate.
4. Does not require every downstream consumer to provision an external service to use the feature.
5. Handles concurrent writes (two PRs triggering dismissals at the same time).
6. Has a clear privacy story — consumers must know where their data goes and who can see it.

Three candidate backends were considered:

- **A**: committed `.ai-pr-review/learnings.jsonl` on a bot-managed branch in the consumer's repo.
- **B**: per-run workflow artifact + scheduled consolidator workflow.
- **C**: external Tag1-hosted service with per-repo auth.

The plan's Open Question 1 called for this ADR to land before Epic 3 sprint planning.

## Decision

**Adopt Backend A: committed `.ai-pr-review/learnings.jsonl` on a bot-managed branch** (default: `ai-pr-review-bot`) in the consumer's repo. Writes via PyGithub (GitHub) / python-gitlab (GitLab) / httpx (Bitbucket). Concurrent writes handled by optimistic-lock retry on branch SHA.

**User-supplied `<reason>` text is treated as public by default.** The slash-command docs warn explicitly that reason text is written into a commit on the consumer's repo and is visible to anyone with repo read access. Users self-censor accordingly.

## Storage shape

### Location

- Branch: `ai-pr-review-bot` (configurable via `AI_FEEDBACK_BRANCH` env var, default as stated).
- Path within branch: `.ai-pr-review/learnings.jsonl`.
- One JSONL entry per dismissal / wont-fix / feedback event.
- Branch created lazily on first write (orphan branch with a single file).

### File format

Each line is a pydantic-validated `FeedbackEntry` serialized as JSON:

```json
{"rule_id":"code-reviewer","file_pattern":"lib/foo.py","snippet":"...","reason":"This is intentional because X","user":"gchaix","sha":"abc123","event_type":"false-positive","created_at":"2026-05-11T22:00:00Z","expires_at":null}
```

Retention (per `AI_FEEDBACK_RETENTION_COUNT` default 500, `AI_FEEDBACK_RETENTION_AGE_DAYS` default 365) is applied on write: before each append, if the file would exceed the count cap, the oldest entries are dropped; entries past `expires_at` are removed.

### Concurrent-write handling

1. Read the `ai-pr-review-bot` branch HEAD SHA.
2. Fetch the current `learnings.jsonl` content.
3. Compute the new content (append + trim).
4. Commit via the VCS's create-or-update-file API with the branch SHA as a precondition (optimistic lock).
5. On 409 conflict: re-read HEAD, re-merge the append (our entry plus any entries that arrived between our read and our write), re-attempt. Retry up to 3 times with jitter.
6. After 3 failures: log a WARNING with the dropped entry and continue the review (fail-soft per `3.FR-C4`).

## Privacy & security

- **Default visibility**: public. Reason text lives in the consumer's repo. On public repos, that means globally visible.
- **Warning in docs**: `docs/slash-commands.md` and the bot's reply when a user invokes a slash command for the first time in a repo must warn that reasons are persisted to a branch in this repo and are readable by anyone with repo access.
- **No PII fields captured by the tool itself**: we capture the GitHub/GitLab/Bitbucket username (already visible in the commenting event), the SHA, and the user-supplied `reason` text. We do not capture email, IP, or any profile metadata.
- **Sanitization on write**: `reason` is capped at 1024 chars, control characters stripped, HTML-escaped. Prevents prompt-injection on read.
- **Secret-masking on write**: the existing secret-masking helpers scan `reason` and reject writes that contain known secret patterns (API keys, tokens). Logged as a WARNING with the user's event dropped.
- **Branch permissions**: the default `GITHUB_TOKEN` / pipeline token needs `contents: write` to create the branch. Documented in the slash-command setup workflow.

## Consequences

### Positive
- Zero ops for Tag1 — no service to run.
- Zero ops for consumers — no extra workflow file beyond the existing `slash-commands.yml` wrapper.
- Transparent: consumers can read, edit, and rollback the file in Git like any other source.
- Audit-friendly: every write is a commit with user attribution.
- Works offline / self-hosted: if a consumer runs a self-hosted GitHub / GitLab / Bitbucket, the store runs there too.

### Negative
- **Fork PRs cannot write.** `GITHUB_TOKEN` in a fork PR context is read-only on the base repo. Users commenting on a fork PR cannot persist learnings. Mitigation: the `author_association` guard already restricts slash-command execution to OWNER/MEMBER/COLLABORATOR on the base repo; fork contributors usually don't match, so the practical overlap is small. Documented in `docs/slash-commands.md`.
- **Public repos → public reasons.** A user who types a sensitive reason into a public repo exposes it. The docs warn explicitly; the tool does not enforce.
- **Branch clutter.** Consumers see an extra branch in their branch list. Documented and justifiable; non-blocking.
- **Concurrent-write retry latency.** In high-activity repos, optimistic-lock retries can add 1–3 seconds to a slash-command handling run. Acceptable.

### Neutral
- **Not a primitive that generalizes easily to Phase 8 (cross-repo learning).** Phase 8 may need a different backend or an aggregation layer on top. This ADR does not preclude it.

## Implementation notes for Epic 3

- `ai_pr_review/feedback/store.py` implements the `FeedbackStore` protocol with a `GitBranchStore` concrete class per VCS provider.
- `ai_pr_review/feedback/adapter.py` per-VCS adapter resolves the branch SHA, reads the file, writes with optimistic lock.
- The first dismissal in any repo triggers a bot reply comment with the privacy warning and a link to `docs/slash-commands.md#learning-store`.
- A fault-injection fixture in the payload harness forces the store into an unwritable state (e.g., 403 on branch read) and asserts the review continues and logs a WARNING.
- An adversarial fixture attempts a prompt-injection payload in `<reason>`; the harness asserts the injected text cannot escape the `<repo-feedback>` delimiter.

## Alternatives considered

### Backend B — Workflow artifact + consolidator

Rejected primarily for operational complexity: consumers must install and maintain a scheduled workflow that reads artifacts and consolidates them. One more moving part per consumer. The artifact path does solve fork-PR writes, but fork PRs are already excluded by the `author_association` guard, so the problem it solves rarely occurs in practice.

### Backend C — External Tag1-hosted service

Rejected for ongoing ops burden and the introduction of a cross-org dependency. A tool that can be self-hosted should not become one that can't. Also: privacy trade-off goes the wrong direction for some consumers (they may trust their own repo more than Tag1-hosted infra).

### Deferral

Rejected. Epic 3 ships the learning loop; shipping it with a stub that logs to stderr only means the "learning" part doesn't actually learn during the soak. We'd reach the Epic 4 default-flip moment without field data on the feedback loop's value.

## Open follow-ups (not blocking this ADR)

- **Phase 7** (Future): when GitLab and Bitbucket slash commands land, they use the same backend. Branch-based storage works identically on all three VCS providers.
- **Phase 8** (Future): cross-repo aggregation may introduce a new backend or an aggregator layer; this ADR will be revisited then.
- **Retention tuning**: defaults (500 entries / 365 days) are educated guesses. Revisit after Epic 4 soak with usage data.
