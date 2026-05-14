---
layout: default
title: Learning Loop
parent: Configuration
nav_order: 10
---

# Learning loop (Capability C)

The learning loop allows human reviewers to feed signals back to the AI agents by posting slash commands in PR review comment threads. Over time this accumulates repository-specific knowledge that future review runs can draw on.

## How it works

1. A reviewer posts `/ai-pr-review false-positive This is an intentional use of MD5 for checksums only.` in response to an AI finding.
2. The `slash-commands.yml` workflow invokes the Python `ai-pr-review slash` CLI subcommand.
3. The subcommand parses and sanitizes the comment body, then writes a `FeedbackEntry` to the **GitBranchStore** — a JSONL file on the dedicated `ai-pr-review-bot` branch.
4. On the next review run (with `AI_FEEDBACK_LOOP=true`), the store loads recent entries, ranks them by relevance (file path match, rule ID match), and injects a `<repo-feedback>` XML block into each agent's system prompt.
5. Agents use this context to avoid re-raising the same finding in similar situations.

## Storage (ADR-0001)

Feedback is persisted as `.ai-pr-review/learnings.jsonl` on the `ai-pr-review-bot` branch (configurable via `AI_FEEDBACK_BRANCH`). Each line is a JSON object:

```json
{"ts":"2026-05-14T12:00:00Z","command":"false-positive","reason":"intentional","source":"code-reviewer","file":"src/foo.py","rule_id":""}
```

The file survives PR branch deletion and repository forks. Concurrent writes use optimistic-lock (SHA-based `if-match` on the GitHub Contents API) with up to 3 retries and exponential backoff + jitter. If all retries fail, the entry is silently dropped (fail-soft) and the review still posts.

## Retention policy

| Parameter | Default | Env var |
|-----------|---------|---------|
| Max entries | 500 | `AI_FEEDBACK_RETENTION_COUNT` |
| Max age | 365 days | `AI_FEEDBACK_RETENTION_AGE_DAYS` |

Retention is applied atomically on every write. The oldest entries are dropped first. Set `AI_FEEDBACK_RETENTION_AGE_DAYS=0` to disable age-based pruning.

## Prompt injection format

The `<repo-feedback>` block is injected at the end of each agent's system prompt when `AI_FEEDBACK_LOOP=true`:

```xml
<repo-feedback>
<finding command='false-positive' source='code-reviewer' file='src/crypto.py'>intentional use of MD5 for non-security checksums</finding>
<finding command='wont-fix' source='sarif:bandit' file=''>exception swallowing in top-level error handler is by design</finding>
</repo-feedback>
```

Entries are ranked by relevance before injection:
- **+2 points** if `entry.file` appears in the PR's changed files
- **+1 point** if `entry.rule_id` is non-empty

The block is token-budget-capped (`AI_FEEDBACK_MAX_TOKENS`, default 2048 tokens).

## Supported commands

| Command | Canonical name | Writes to store |
|---------|---------------|-----------------|
| `/ai-pr-review false-positive [reason]` | `false-positive` | Yes |
| `/ai-pr-review dismiss [reason]` | `false-positive` | Yes (alias) |
| `/ai-pr-review wont-fix [reason]` | `wont-fix` | Yes |
| `/ai-pr-review feedback <text>` | `feedback` | Yes |
| `/ai-pr-review explain` | `explain` | No (stubbed) |
| `/ai-pr-review revise <hint>` | `revise` | No (stubbed) |

## Input sanitization

The `reason` text is sanitized before storage:

- Unicode normalized to NFC
- Control characters (except tab) replaced with spaces
- Newlines collapsed to single spaces
- Length capped at 1024 characters
- HTML-escaped to prevent delimiter escape in `<repo-feedback>` blocks
- Rejected (returns empty string) if it matches common secret patterns (API keys, tokens)

## Required setup

1. Set `feedback-loop: 'true'` in `action.yml` inputs.
2. Ensure `GH_TOKEN` (a PAT or GitHub App token) has `contents:write` permission on the feedback branch. The `ai-pr-review-bot` branch is created automatically on first write.
3. The `AI_PR_REVIEW_ENGINE` must be `python` for feedback injection to work.

## Provider support

| Provider | Learning loop |
|----------|---------------|
| GitHub | Full support |
| GitLab | Stub (no-op) |
| Bitbucket | Stub (no-op) |
