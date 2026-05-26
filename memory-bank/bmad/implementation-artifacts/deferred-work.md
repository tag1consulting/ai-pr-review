# Deferred Work

Findings and follow-ups identified during BMad workflows that are intentionally
not addressed in the originating story. Each entry: source story, classification,
finding, why deferred.

## From `spec-governance-preamble` (2026-05-26 review)

- **Source:** blind-hunter, Medium severity, confidence 82
- **Classification:** defer (pre-existing project-wide design choice; not caused by this story)
- **Finding:** Bash `effective_prompt()` warns and falls back to the base prompt when a required shared partial is missing; Python `effective_prompt()` raises `FileNotFoundError`. The two engines have divergent failure semantics for missing required fragments (`_governance.md`, `_knowledge-cutoff.md`, `_trailer-findings.md`).
- **Why deferred:** The behavior split predates this story — bash has always warned-and-fallen-back per the inline docstring at `lib/agents.sh:269-285` ("On any missing-file or cat failure, the function falls back to the base prompt with a WARNING rather than silently passing a truncated prompt to the LLM"). Python has always raised. The governance partial preserves the existing pattern as the spec required. Aligning the two engines is its own design conversation: should bash also raise (block the run on operator misconfiguration), or should Python also degrade (keep agents running)? Worth a separate issue.
- **Suggested next step:** File a GitHub issue describing the divergence, link to this entry, decide direction during issue triage.

- **Source:** acceptance-auditor verification step AC5
- **Classification:** defer (manual pre-merge step, not pre-step-5 blocker)
- **Finding:** Live smoke test against the seeded test repo to confirm (a) bot findings show the new posture (e.g., a `[duplication]` finding or a redacted secret in remediation text), and (b) Tier 2 cache_read tokens still appear when `AI_CACHE_PRIMING=true` on Anthropic provider. Not verifiable from diff alone.
- **Why deferred:** Manual + needs API credentials + a real PR. Belongs in the comprehensive-review pass before tagging a release, not blocking spec acceptance.
- **Suggested next step:** Run the smoke test before opening the PR (or as part of `/comprehensive-review` before release). If cache_read drops to 0, return to spec — the composition order needs re-evaluation.
