# e2e harness

Deterministic Python replacement for the old ~815-line LLM-orchestrated e2e
script. It opens throwaway PRs/MRs on the three real test platforms
(GitHub, GitLab, Bitbucket), runs the review container, and verifies posted
output with plain code (regex/JSON parsing over telemetry and fetched
comments) instead of asking an LLM to eyeball shell output.

## COST AND CHECKPOINT WARNING

**`run` and `preflight` make real API calls against real platforms and cost
real money** (LLM calls billed per the configured provider, plus GitHub/
GitLab/Bitbucket API usage against the shared test repos). Per this repo's
CLAUDE.md checkpoint conventions, do not invoke either subcommand — locally
via `run-local.sh`, or via a CI workflow's `workflow_dispatch` — without
first stopping and getting explicit human confirmation for that specific
invocation. A prior approval does not carry over to a later run. `plan` is
the only fully offline, free subcommand; use it for anything that doesn't
need real credentials.

## Subcommands

- `plan --platforms ... [--profile release]` — offline validation only, no
  network calls. `--profile release` requires exactly `{github, gitlab,
  bitbucket}`.
- `preflight --platforms ...` — verifies token + repo access per platform.
  No LLM calls, no writes.
- `run --platforms ... [--mode full|quick] [--out-dir DIR] [--max-cost-usd N]`
  — the real thing. See below.

## Exit codes

- `0` — pass (every requested platform passed)
- `1` — product failure (the review ran, but produced incorrect behavior)
- `2` — infra failure (auth, quota, rate limit, cost-ceiling skip, timeout,
  or a cleanup failure on the pass path)

## Required env vars

| Var | Purpose |
|---|---|
| `E2E_GITHUB_TOKEN` | Seeder PAT: create branch/commit, open PR |
| `E2E_GITHUB_REVIEWER_TOKEN` | Optional; defaults to `E2E_GITHUB_TOKEN` |
| `E2E_GITLAB_TOKEN` | GitLab personal access token |
| `E2E_BITBUCKET_EMAIL` / `E2E_BITBUCKET_TOKEN` | Bitbucket API token auth |
| `E2E_ANTHROPIC_API_KEY` | Falls back to `ANTHROPIC_API_KEY` if unset |

## Cleanup policy

On pass: the harness closes the PR/MR and deletes its branch by API,
confirming each step. On any failure (product or infra): everything is left
open, deliberately — there is no janitor. This is so a failed run stays
available for debugging; nothing else cleans it up. A cancelled run (SIGINT/
SIGTERM) closes only the PR/MR it opened, since a cancelled run has nothing
worth debugging.

## Adding a new platform adapter

Implement the `PlatformAdapter` protocol in `platforms.py`
(`preflight`/`create_run_commit`/`open_pr`/`fetch_summary`/`fetch_inline`/
`fetch_annotations`/`close`/`delete_branch`), add a `PlatformConfig` entry to
`config.py`, and wire it into `build_adapter()`. Keep this module free of any
import from `ai_pr_review.vcs` — see `platforms.py`'s module docstring for
why.

## Adding a new fixture assertion

Add an `ExpectedFinding(path_substring=..., category=...)` to the relevant
platform's `PlatformConfig.expected_findings` in `config.py`, sourced from
an actual inspection of that fixture's diff — never fabricated. See the
`TODO` markers there.

## Unit tests

`tests/python/e2e_harness/test_verify.py` covers every pure function in
`verify.py` against synthetic fixtures in `tests/python/e2e_harness/
fixtures/`. Run with `pytest tests/python/e2e_harness -q`. These tests make
no network calls and cost nothing.
