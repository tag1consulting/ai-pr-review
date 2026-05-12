# Compute Output Schema

The Python engine (`AI_PR_REVIEW_ENGINE=python`) writes a JSON payload to
`AI_PR_REVIEW_COMPUTE_OUTPUT` at the end of the compute phase. The bash
post-review scripts read this file to perform posting.

> **Epic 1 shim** — this handoff mechanism is temporary. Epic 2 (#196)
> routes posting through Python and removes this file.

## Schema

```json
{
  "skip": false,
  "reason": "",
  "diff": "<unified diff text>",
  "changed_files": ["path/to/file.py", "..."],
  "manifest": "BASE: main | DIFF: ... | LANGUAGES: Python | FILES: 3 | ...",
  "diff_label": "full (main..abc1234)",
  "base": "main",
  "head": "abc1234def5678",
  "is_incremental": false,
  "languages": ["Python", "Shell"],
  "findings": [],
  "token_log": []
}
```

## Fields

| Field | Type | Description |
|---|---|---|
| `skip` | bool | True if the review should be skipped (e.g. diff too large, no files). |
| `reason` | string | Human-readable reason for skip. Empty when skip=false. |
| `diff` | string | Full unified diff text. Empty when skip=true. |
| `changed_files` | string[] | List of changed file paths (after lockfile exclusion). |
| `manifest` | string | Formatted manifest line for agent context. |
| `diff_label` | string | Human-readable diff description (e.g. "incremental (abc..def)"). |
| `base` | string | Base branch name. |
| `head` | string | Head commit SHA. |
| `is_incremental` | bool | True if this is an incremental (watermark) diff. |
| `languages` | string[] | Detected language labels. |
| `findings` | Finding[] | Findings from the Python findings pipeline. |
| `token_log` | TokenEntry[] | Per-agent token usage entries. |

## Finding schema

Each entry in `findings` matches the agent output schema:

```json
{
  "severity": "High",
  "confidence": 85,
  "file": "path/to/file.py",
  "line": 42,
  "start_line": 40,
  "finding": "Description",
  "remediation": "How to fix",
  "suggested_code": "replacement",
  "source": "security-reviewer",
  "sources": ["security-reviewer", "code-reviewer"]
}
```
