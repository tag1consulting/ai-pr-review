---
layout: default
title: Suppression Rules
nav_order: 6
---

# Suppression Rules

Known false positives can be suppressed via `config/suppressions.json`. Each entry matches findings by file, line, code prefix, or regex pattern:

```json
[
  {
    "id": "descriptive-id",
    "reason": "Why this is a false positive",
    "match": {
      "file": "specific-file.sh",
      "pattern": "regex.*to.*match.*finding.*text"
    }
  }
]
```

Match fields (all optional, combined with AND logic):
- `file` — Substring match on the finding's file path
- `line` — Exact line number match
- `code` — Finding text starts with this prefix
- `pattern` — Regex matched against the finding text

## Local suppressions

Consuming repos can add their own suppression rules without modifying the action. Create `.github/ai-pr-review/suppressions.json` in your repository using the same schema:

```json
[
  {
    "id": "my-repo-specific-rule",
    "reason": "Why this finding is not relevant to this repo",
    "match": {
      "pattern": "regex.*to.*match.*finding.*text"
    }
  }
]
```

Local rules are merged with the global suppression rules at runtime — no action input or configuration is required.

## Suppressing CVE findings

To accept a specific CVE (e.g. a library used only in a test fixture), add a suppression rule matching the CVE or GHSA ID:

```json
{
  "id": "accept-risk-CVE-2025-12345",
  "reason": "Library used only in test fixtures, not production",
  "match": {
    "pattern": "CVE-2025-12345|GHSA-xxxx-yyyy-zzzz"
  }
}
```
