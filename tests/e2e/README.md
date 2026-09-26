# e2e harness

Deterministic Python replacement for the old ~815-line LLM-orchestrated e2e script. It opens throwaway PRs/MRs on the three real test platforms (GitHub, GitLab, Bitbucket), runs the review container, and verifies posted output with plain code (regex/JSON parsing over telemetry and fetched comments) instead of asking an LLM to eyeball shell output.

## COST AND CHECKPOINT WARNING

**`run` and `preflight` make real API calls against real platforms and cost real money** (LLM calls billed per the configured provider, plus GitHub/GitLab/Bitbucket API usage against the shared test repos). Per this repo's CLAUDE.md checkpoint conventions, do not invoke either subcommand (locally via `run-local.sh`, or via a CI workflow's `workflow_dispatch`) without first stopping and getting explicit human confirmation for that specific invocation. A prior approval does not carry over to a later run. `plan` is the only fully offline, free subcommand: use it for anything that doesn't need real credentials.

## Subcommands

- `plan --platforms ... [--profile release]`: offline validation only, no network calls. `--profile release` requires exactly `{github, gitlab, bitbucket}`.
- `preflight --platforms ...`: verifies token and repo access per platform. No LLM calls, no writes.
- `run --platforms ... [--mode full|quick] [--out-dir DIR] [--max-cost-usd N]`: the real thing. See below.

## Exit codes

- `0`: pass (every requested platform passed)
- `1`: product failure (the review ran, but produced incorrect behavior)
- `2`: infra failure (auth, quota, rate limit, cost-ceiling skip, timeout, or a cleanup failure on the pass path)

## Required env vars

| Var | Purpose |
|---|---|
| `E2E_GITHUB_TOKEN` | Seeder PAT: create branch/commit, open PR |
| `E2E_GITHUB_REVIEWER_TOKEN` | Optional, defaults to `E2E_GITHUB_TOKEN` |
| `E2E_GITLAB_TOKEN` | GitLab personal access token |
| `E2E_BITBUCKET_EMAIL` / `E2E_BITBUCKET_TOKEN` | Bitbucket API token auth |
| `E2E_ANTHROPIC_API_KEY` | Falls back to `ANTHROPIC_API_KEY` if unset |

## Cleanup policy

On pass, the harness closes the PR/MR and deletes its branch by API, confirming each step. On any failure (product or infra), everything is left open, deliberately: there is no janitor. This is so a failed run stays available for debugging; nothing else cleans it up. A cancelled run (SIGINT/SIGTERM) closes only the PR/MR it opened, since a cancelled run has nothing worth debugging.

Every opened PR/MR is also recorded durably in `<out_dir>/opened.json` (one entry per platform: `platform`, `number`, `branch`, `url`, plus a `resolution` field -- `"closed"` or `"left_open"` -- once that platform's fate is decided). This is the record the in-process signal handler above can't fully rely on alone: its network calls can take longer to complete than a CI runner's cancel grace period, especially if a SIGINT is immediately followed by a SIGTERM. `e2e.yml`'s per-leg matrix job has an `if: cancelled()` fallback step that runs `python -m tests.e2e.run_e2e cleanup --from-file opened.json` against this same file to finish a stuck cleanup out-of-process.

The `cleanup` subcommand (`cleanup --from-file <path>`) closes and deletes the branch for every entry in an `opened.json` file that does NOT already carry a `resolution` -- an entry already marked `"closed"` or `"left_open"` is skipped untouched, so this subcommand can never override a deliberate leave-open-on-failure decision (or double-close an already-closed PR) when invoked against a run's own file after that run already decided each platform's fate. It's also safe to run manually against any `opened.json` an operator wants to clean up by hand: close()/delete_branch() already tolerate an already-gone branch or PR/MR (404/422), so a repeat invocation is a harmless no-op. Exit code 0 on full success, 2 (`EXIT_INFRA_FAILURE`) if any entry's close or branch-delete call fails.

## Cross-repo dependency: why the test repos' own CI doesn't double-review harness PRs

Each test repo's own always-on review CI (GitHub's `ai-pr-review.yml`, GitLab's `.gitlab-ci.yml`, Bitbucket's `bitbucket-pipelines.yml`) would otherwise ALSO fire on every PR/MR this harness opens, since it triggers on the same `opened`/`merge_request_event`/`pull-requests` events this harness uses -- posting a second, redundant review under the published `:dev` image on the same PR the harness's own container run (built from the actual candidate under test) is reviewing. Confirmed live (2026-09-25/26) that this does NOT currently happen: each provider evaluates its pipeline/workflow definition from the content of the PR/MR's own source ref for these event types, and each pinned seed commit's own copy of that CI config file already carries an explicit exclusion for `e2e/*`-named branches (verified by fetching each config file directly from its pinned `*_SEED_SHA` in `config.py`, not from the repo's default branch). None of the three test repos' CURRENT default-branch CI config still carries this exclusion -- it lives only in the frozen seed commit's tree, as a deliberate piece of the fixture, not as an ongoing default-branch guarantee.

**Consequence for re-pinning a seed SHA:** if `GITHUB_SEED_SHA`/`GITLAB_SEED_SHA`/`BITBUCKET_SEED_SHA` in `config.py` is ever updated to a newer commit, whoever does that must re-verify the new seed commit's own CI config still excludes `e2e/*` branches (or re-add the exclusion to that commit before pinning it) -- otherwise every harness run silently starts double-reviewing again: extra billed API cost per run, and a live risk that the always-on `:dev`-image review's own summary marker (posted against the exact same commit SHA the harness's candidate-image review also targets) satisfies `verify_summary_marker`'s SHA match even if the candidate image under test never ran or malfunctioned, invalidating the whole point of the gate for that platform. This is exactly the kind of drift a pinned-fixture design is supposed to prevent turning into a silent gap: it's real as of this writing only because the exclusion was hand-added directly to the seed commit rather than landing on the repos' own default branches.

## Known limitation: GitHub self-review degrade

The GitHub leg currently uses the same personal token as both the seeder (opens the PR) and the reviewer (the container's `GH_TOKEN`) -- separate seeder/reviewer PATs haven't been provisioned yet. GitHub will not let an identity request changes on, or approve, its own pull request: it silently downgrades that event to a plain `COMMENT`. `verify_event_not_degraded` in `verify.py` correctly detects and fails on this (comparing telemetry's intended outcome against what was actually posted), so a live GitHub run in this configuration is expected to fail that one check -- it's a real platform restriction being correctly caught, not a harness bug and not something to work around by weakening the check.

## Adding a new platform adapter

Implement the `PlatformAdapter` protocol in `platforms.py` (`preflight`/`create_run_commit`/`open_pr`/`fetch_summary`/`fetch_inline`/`fetch_annotations`/`close`/`delete_branch`), add a `PlatformConfig` entry to `config.py`, and wire it into `build_adapter()`. Keep this module free of any import from `ai_pr_review.vcs`: see `platforms.py`'s module docstring for why. If the new platform's per-finding detail lives somewhere other than a real inline review comment (as Bitbucket's does, in Code Insights annotations), set `PlatformConfig.per_finding_surface` to that surface rather than adding another `if platform == "..."` check at each call site that cares.

## Adding a new fixture assertion

Add an `ExpectedFinding(path_substring=..., category=...)` to the relevant platform's `PlatformConfig.expected_findings` in `config.py`, sourced from an actual inspection of that fixture's diff (via a real live run's posted output), never fabricated. See the existing entries in `config.py` for the pattern and how each one was verified.

## Container requirements: workspace and targeting

The review container needs to know which PR/MR it's reviewing and needs a real git checkout mounted at `/workspace` (the product's own diff computation runs `git -C /workspace`, per `ai_pr_review/diff/compute.py`). `run_e2e.py`'s `_clone_workspace()` shallow-clones the run branch using credentials passed via `GIT_CONFIG_*` env vars (an `http.extraHeader` Basic-auth header), never embedded in the clone URL or process argv, and mounts it read-only; `_provider_env_vars()` sets the per-provider targeting env vars (`GITHUB_REPOSITORY`/`PR_NUMBER`, `CI_PROJECT_PATH`/`MR_IID`/`GITLAB_DIFF_BASE_SHA`, or `BITBUCKET_WORKSPACE`/`BITBUCKET_REPO_SLUG`/`PR_NUMBER`), verified against `ai_pr_review/vcs/__init__.py`'s documented per-provider requirements. Both this workspace wiring and the credential-injection approach have been live-verified end to end against all three platforms.

## Unit tests

`tests/python/e2e_harness/test_verify.py` covers every pure function in `verify.py` against synthetic fixtures in `tests/python/e2e_harness/fixtures/`. Run with `python -m pytest tests/python/e2e_harness -q` (module invocation, not bare `pytest`: the repo root needs to be on `sys.path` for the `tests.e2e.*` imports to resolve, matching this repo's `lint.yml` convention). These tests make no network calls and cost nothing.
