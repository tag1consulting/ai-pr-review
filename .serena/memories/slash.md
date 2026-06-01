# Slash Command Handling

## Key files
- `ai_pr_review/slash/parser.py` — parses `/ai-pr-review <cmd> [args]` from PR comment body
- `ai_pr_review/slash/handlers.py` — command handlers (dismiss, false-positive, wont-fix)
- `ai_pr_review/slash/__init__.py`

## Commands
- `/ai-pr-review dismiss F<n>` — dismiss a finding by stable ID
- `/ai-pr-review false-positive F<n>` — mark a finding as false positive
- `/ai-pr-review wont-fix F<n>` — mark a finding as wont-fix

## Body-level finding IDs
Stable `F<n>` IDs are assigned in `ai_pr_review/vcs/_finding_ids.py` after merge. Body-level findings (no file/line) use these IDs; slash commands from top-level PR comments target them by ID.

## Invocation
CLI entry point: `ai-pr-review slash` in `ai_pr_review/cli.py:slash()`.
Triggered by GitHub comment events in the Actions workflow.
