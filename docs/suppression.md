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

## Verify-gated suppressions

Suppression rules can include an optional `verify` field that instructs the
action to confirm the version actually exists by querying an authoritative
registry before suppressing the finding. If the registry says the version is
missing, the finding is restored — so a mis-scoped suppression cannot hide a
real typo or malicious downgrade.

| `verify` value | Extracts from finding | Authoritative source |
|---|---|---|
| `github-release` | `owner/repo@vN.N.N` | `gh api repos/{owner}/{repo}/git/ref/tags/{tag}` |
| `npm` | `pkg@version` or `"pkg": "version"` | `registry.npmjs.org` |
| `pypi` | `pkg==version` | `pypi.org/pypi/{pkg}/{version}/json` |
| `go-module` | `module@vX.Y.Z` | `proxy.golang.org/{module}/@v/{version}.info` |
| `cargo` | `pkg = "version"` or `pkg@version` | `crates.io` |
| `docker-hub` | `image:tag` or `ns/image:tag` | `hub.docker.com` |
| `ruby-org` | Ruby `X.Y.Z` MRI version | `cache.ruby-lang.org/pub/ruby/{MAJ.MIN}/ruby-{MAJ.MIN.PATCH}.tar.gz` |

Example:

```json
{
  "id": "allow-ruby-4.0.3",
  "reason": "Model keeps flagging Ruby 4.0.3 as unreleased; verify against ruby-lang.org",
  "match": {
    "pattern": "Ruby.*4\\.0\\.3.*not.*(valid|released|exist)"
  },
  "verify": "ruby-org"
}
```

Private registries (GHCR, GCR, ECR) are not supported because they require
authentication.

## Why the bot flagged a valid version as "unreleased"

Every LLM has a **training-data cutoff**. Anything released after that cutoff
looks to the model like a hallucination. When a reviewer agent sees `ruby/setup-ruby@v1`
or `actions/checkout@v5` or `ruby-4.0.3` that it has never seen during training,
it may incorrectly flag the version as "unreleased", "invalid", or
"not a valid release" — even though the version shipped months earlier.

The action hardens against this in two layers:

1. **Prompt-level guard.** All 7 finding-producing agents receive the
   `_knowledge-cutoff.md` shared trailer via `effective_prompt()` in
   `review.sh`. This hard constraint forbids "unreleased version" findings
   based on training-data recall. The model is instructed to omit such
   findings entirely unless the version string is malformed, explicitly
   downgraded, or covered by a cited CVE.
2. **Verify-gated suppression.** If a repeat hallucination slips through, add a
   suppression rule with a `verify` field (table above). The rule fires only
   if the authoritative registry confirms the version exists. This is
   deterministic and safe — if the suppression pattern accidentally matches a
   real typo (e.g. `ruby-9.9.9`), the registry lookup fails and the finding is
   restored.

The CVE check (`analyzers/run-cve-check.sh`) is the complement: it queries
OSV.dev against changed dependency manifests and emits a new finding when a
declared version has a known vulnerability. It does not emit "doesn't exist"
findings.
