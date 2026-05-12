# Config Parity Matrix

Authoritative enumeration of every environment variable consumed by ai-pr-review.
Source: `docs/configuration.md` and `action.yml`.

**Column definitions:**
- **Name** — env var or action input name
- **Source** — how it enters the runtime (`action-input` = mapped from action.yml input; `env-var` = set directly in workflow `env:` or docker `-e`)
- **Purpose** — what it controls
- **Covered-by-fixture** — fixture name(s) that exercise this var, or `manual-test-only` with rationale
- **Python-parity-proven** — `pending` until Epic 1/2 ports the corresponding logic

---

## Action Inputs (mapped to env vars by action.yml)

| Name | Env Var | Source | Purpose | Covered-by-fixture | Python-parity-proven |
|---|---|---|---|---|---|
| `provider` | `AI_PROVIDER` | action-input | LLM provider selection | all fixtures (anthropic) | pending |
| `api-key` | `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `GOOGLE_API_KEY` / `BEDROCK_API_KEY` | action-input | Provider API credential | manual-test-only: secrets not stored in fixtures | pending |
| `base-url` | `OPENAI_BASE_URL` | action-input | OpenAI-compatible or bedrock-proxy endpoint | manual-test-only: requires live endpoint | pending |
| `model-standard` | `AI_MODEL_STANDARD` | action-input | Model for standard agents | all fixtures (claude-sonnet-4-6) | pending |
| `model-premium` | `AI_MODEL_PREMIUM` | action-input | Model for premium (full-mode) agents | gh-full-mode | pending |
| `review-mode` | `AI_REVIEW_MODE` | action-input | `quick` or `full` | gh-full-mode (full), all others (quick) | pending |
| `review-target` | `REVIEW_TARGET` | action-input | `pr` or `standalone` | all fixtures (pr) — standalone: manual-test-only | pending |
| `max-diff-lines` | `MAX_DIFF_LINES` | action-input | Max diff lines before skip comment | gh-large-diff | pending |
| `pr-number` | `PR_NUMBER` | action-input | PR/MR number | all fixtures | pending |
| `base-ref` | `BASE_REF` | action-input | Base branch for diff | all fixtures (main) | pending |
| `head-sha` | `HEAD_SHA` | action-input | Head commit SHA | all fixtures | pending |
| `github-token` | `GH_TOKEN` | action-input | GitHub API token | manual-test-only: secrets not stored in fixtures | pending |
| `parallel` | `AI_PARALLEL` | action-input | Enable parallel agent fan-out | manual-test-only: threading behavior | pending |
| `max-inline` | `AI_MAX_INLINE` | action-input | Max inline comments per run | manual-test-only: cap behavior requires many findings | pending |
| `max-tokens-per-agent` | `AI_MAX_TOKENS_PER_AGENT` | action-input | Max LLM output tokens per agent | manual-test-only: requires live LLM call | pending |
| `enable-suggestions` | `AI_ENABLE_SUGGESTIONS` | action-input | Enable suggestion fences on inline comments | manual-test-only: requires live VCS API + diff | pending |

## Core Runtime Variables (env-var only)

| Name | Source | Purpose | Covered-by-fixture | Python-parity-proven |
|---|---|---|---|---|
| `VCS_PROVIDER` | env-var | VCS provider selection (`github`/`bitbucket`/`gitlab`) | gh-docs-only (github), gl-basic (gitlab), bb-basic (bitbucket) | pending |
| `AI_TEMPERATURE` | env-var | LLM sampling temperature (0–2) | manual-test-only: output non-determinism | pending |
| `LLM_PROMPT_CACHING` | env-var | Anthropic/Bedrock prompt caching (`auto`/`true`/`false`) | manual-test-only: requires live Anthropic API | pending |
| `AI_CACHE_PRIMING` | env-var | Serialize cache-writing calls before parallel fan-out | manual-test-only: parallelism/timing | pending |
| `PHPSTAN_LEVEL` | env-var | PHPStan analysis depth (0–9) | manual-test-only: PHP analyzer | pending |

## Advanced Tuning Variables (env-var only)

| Name | Source | Purpose | Covered-by-fixture | Python-parity-proven |
|---|---|---|---|---|
| `FORCE_FULL_DIFF` | env-var | Bypass SHA watermark, review full PR diff | manual-test-only: watermark bypass | pending |
| `STANDALONE_DEPTH` | env-var | Commit depth for standalone mode diff | manual-test-only: standalone mode | pending |
| `LLM_RETRY_COUNT` | env-var | Retry attempts on transient LLM failures | gh-failed-agent (0 retries / timeout) | pending |
| `AI_CONFIDENCE_THRESHOLD` | env-var | Minimum confidence score for findings (0–100) | all fixtures (75) | pending |
| `AI_DISABLE_GATE_ARCHITECTURE` | env-var | Disable docs-only heuristic gate for architecture-reviewer | manual-test-only: gating behavior | pending |
| `AI_DISABLE_GATE_SECURITY` | env-var | Disable keyword/path heuristic gate for security-reviewer | manual-test-only: gating behavior | pending |
| `AI_DISABLE_GATE_EDGE_CASE` | env-var | Disable control-flow heuristic gate for edge-case-hunter | manual-test-only: gating behavior | pending |

## Bitbucket-specific Variables

| Name | Source | Purpose | Covered-by-fixture | Python-parity-proven |
|---|---|---|---|---|
| `BITBUCKET_EMAIL` | env-var | Atlassian account email (Basic-auth username) | bb-basic (redacted) | pending |
| `BITBUCKET_API_TOKEN` | env-var | Atlassian API token (Basic-auth password) | bb-basic (redacted) | pending |
| `BITBUCKET_WORKSPACE` | env-var | Override workspace slug from GITHUB_REPOSITORY | bb-basic | pending |
| `BITBUCKET_REPO_SLUG` | env-var | Override repo slug from GITHUB_REPOSITORY | bb-basic | pending |

## GitLab-specific Variables

| Name | Source | Purpose | Covered-by-fixture | Python-parity-proven |
|---|---|---|---|---|
| `GITLAB_TOKEN` | env-var | GitLab personal/project access token | gl-basic (redacted) | pending |
| `GITLAB_API_URL` | env-var | GitLab API base URL (self-hosted) | manual-test-only: requires self-hosted instance | pending |
| `GITLAB_PROJECT_ID` | env-var | Numeric project ID override | gl-basic (CI_PROJECT_ID used instead) | pending |
| `GITLAB_MR_DIFF_BASE_SHA` | env-var | Base SHA for inline discussion positions | gl-basic (CI_MERGE_REQUEST_DIFF_BASE_SHA) | pending |
| `GITLAB_BOT_USERNAME` | env-var | Bot username for stale thread resolution | manual-test-only: stale thread auth | pending |

## GitLab CI Variables (auto-set by GitLab CI)

These are set by GitLab CI automatically and consumed as fallbacks:

| Name | Source | Purpose | Covered-by-fixture | Python-parity-proven |
|---|---|---|---|---|
| `CI_PROJECT_ID` | env-var (GitLab CI) | Numeric project ID fallback | gl-basic | pending |
| `CI_PROJECT_PATH` | env-var (GitLab CI) | Project path fallback | gl-basic | pending |
| `CI_MERGE_REQUEST_IID` | env-var (GitLab CI) | MR number fallback | gl-basic | pending |
| `CI_MERGE_REQUEST_DIFF_BASE_SHA` | env-var (GitLab CI) | Diff base SHA fallback | gl-basic | pending |
| `CI_JOB_TOKEN` | env-var (GitLab CI) | Auth fallback when GITLAB_TOKEN unset | manual-test-only: CI auth | pending |

## Recording Variable (Epic 0)

| Name | Source | Purpose | Covered-by-fixture | Python-parity-proven |
|---|---|---|---|---|
| `AI_PR_REVIEW_RECORD_DIR` | env-var | Enable tape recording mode for golden fixture capture | manual-test-only: used at fixture-capture time | N/A — recording infra, not engine logic |

---

## Coverage Summary

- **Total variables:** 41
- **Fixture-covered:** 12 (all fixtures use a subset of vars; VCS and LLM provider selection covered across 3 providers each)
- **Manual-test-only:** 29 (secrets, live API behavior, timing/parallelism, rarely-changed tuning)
- **Python-parity-proven:** 0 (pending Epics 1–2)

### Manual-test-only rationale categories

| Category | Variables | Rationale |
|---|---|---|
| Secrets / credentials | `api-key`, `github-token`, `BITBUCKET_API_TOKEN`, `GITLAB_TOKEN`, `CI_JOB_TOKEN` | Never stored in fixtures (0.NFR-1) |
| Live API behavior | `LLM_PROMPT_CACHING`, `AI_CACHE_PRIMING`, `OPENAI_BASE_URL`, `GITLAB_API_URL`, `AI_PARALLEL`, `AI_ENABLE_SUGGESTIONS` | Requires live network call to verify |
| Heuristic gates | `AI_DISABLE_GATE_*` | Gating behavior depends on diff content heuristics; covered by bats suite |
| Sizing / caps | `max-inline`, `max-tokens-per-agent` | Requires hitting the cap (many agents/findings); not feasible in fixture corpus |
| Timing / temperature | `AI_TEMPERATURE`, `LLM_RETRY_COUNT` | Temperature affects non-deterministic output; retry count requires simulated failures |
| Modes | `FORCE_FULL_DIFF`, `STANDALONE_DEPTH`, `review-target=standalone` | Standalone mode and full-diff bypass have separate code paths; covered by bats suite |
| Platform-specific config | `PHPSTAN_LEVEL`, `GITLAB_BOT_USERNAME` | Language-specific analyzer config; stale thread auth |
