# Architecture (Internal Reference)

This document contains deep implementation details for maintainers and AI agents working on ai-pr-review. For the high-level directory layout and data flow, see [docs/architecture.md](architecture.md). For contributor how-tos, see [CONTRIBUTING.md](../CONTRIBUTING.md).

## Agent output schema

Every agent prompt expects a `json-findings` fenced code block in the response:

```json
[
  {
    "severity": "Critical|High|Medium|Low",
    "confidence": 0-100,
    "file": "path/to/file.ext",
    "line": 42,
    "start_line": 40,
    "finding": "Description of the issue",
    "remediation": "How to fix it",
    "suggested_code": "replacement code"
  }
]
```

The `source` field is optional in agent output — the findings extractor stamps the agent name automatically if the field is absent. Static analyzers hard-code their source values in their output projection.

`suggested_code` and `start_line` are optional fields emitted when the `enable-suggestions` action input is `true` (the default). See [Code suggestions](#code-suggestions) below.

Findings below confidence 75 are filtered out. Duplicates are deduped using proximity-based matching: findings in the same file within 3 lines of each other are merged into a single cluster, keeping the highest-severity finding. The dedup step carries a `sources` array on the surviving finding, unioning all sources from the cluster. When multiple sources are present, the VCS provider renders `[first-source] *(also flagged by: other)*` attribution (sources are sorted alphabetically).

## Runtime flow

The entrypoint is `ai_pr_review.cli:review`. The Click command parses flags, sets up logging, and calls `_run_review_async()`, which:

1. Calls `build_review_runtime(config)` in `ai_pr_review/review/runtime.py`. The runtime layer:
   - Resolves provider model defaults (`ReviewConfig.resolve_models()`).
   - Builds the VCS provider via `provider_from_env`.
   - Fetches the last-reviewed SHA and computes the diff (`ai_pr_review/review/compute.py`).
   - Returns `SkipPlan` if compute reports no changes.
   - Detects changed file languages and loads language profiles once via `load_language_profiles()`. The concatenated markdown is stored in `DispatchContext.language_profile_text` so each agent dispatch reads from memory rather than disk.
   - Loads the feedback store, runs native analyzers, loads SARIF findings (via `config.sarif_paths`), loads suppression rules, evaluates gates, and builds the `DispatchContext` and `OrchestrationConfig`. All pre-computed findings are merged into `OrchestrationConfig.extra_findings`.
2. Runs `pr-summarizer` on first (non-incremental) reviews.
3. If `AI_DRY_RUN=1`, short-circuits after assembly without posting.
4. Otherwise calls `orchestrate.run_review()`, which dispatches agent tiers, merges LLM + pre-computed findings via `extra_findings`, suppresses, classifies the outcome, and posts via the provider.

**Boundary:** `run_review()` reads no environment and constructs no dependencies — it is the unit-testable core. `build_review_runtime()` is the seam between env-driven configuration and pure orchestration. Tests for the assembly layer are in `tests/python/test_runtime.py`.

## Incremental review / SHA watermark

The SHA of the last-reviewed commit is stored in an HTML comment embedded in the PR/MR summary comment (`<!-- ai-pr-review-summary sha=<sha> -->`). The VCS provider extracts it at the start of each run. Subsequent pushes diff from that SHA to HEAD. The watermark is advanced at the end of each run when the summary comment is posted with the new HEAD SHA.

The VCS provider keeps at most one summary comment on the PR/MR by cleaning up duplicates after each upsert. Duplicates can accumulate when two runs fire concurrently. Cleanup is non-fatal: DELETE failures emit a WARNING.

To force a full-PR diff for a single run, add the `ai-review-rescan` label to the PR. The workflow sets `FORCE_FULL_DIFF=true` via the `env:` block, which causes the engine to skip the last-reviewed SHA lookup and fall through to the full `origin/BASE_REF...HEAD_SHA` diff.

## Context message variants

Three context variants are assembled and passed selectively to agents:

- **full context** — manifest + PR description (title + body) + commit log + CLAUDE.md excerpt (first 2000 chars) + language profiles + diff
- **code context** — manifest + PR description (title + body) + language profiles + diff (no commit log or project context)
- **blind** — raw diff only (intentional zero context for `blind-hunter`)

The PR description is fetched from the VCS provider API early in the run. It includes the PR/MR title and body (truncated to 4000 chars). HTML comment lines from PR templates are stripped. The section is omitted entirely when the title and body are both empty (e.g., standalone reviews or PRs with no description). This gives agents visibility into the author's stated intent, reducing false positives on intentional changes.

## Parallel agent execution

Phase 1 runs agents in a tiered fan-out mode by default, reducing wall-clock time from ~5-7 minutes to ~2 minutes in `full` mode.

Disable via `parallel: false` action input or `AI_PARALLEL=false` env var (default: `true`).

> **Breaking change (direct-script users):** Prior to issue #73, the engine defaulted to `AI_PARALLEL=false` when the variable was unset, while `action.yml` defaulted to `true`. The default is now `true` in both invocation paths.

### Tier groupings

| Tier | Agents | When |
|------|--------|------|
| Tier 1 | `pr-summarizer` (first run only), `code-reviewer`, `silent-failure-hunter` (conditional, standard model in quick / premium in full) | Always |
| Tier 1 (static analyzers, concurrent with Tier 1) | All native analyzers (`ai_pr_review/analyzers/`) | Always (graceful no-op if binary absent) |
| Tier 2 | `architecture-reviewer`, `security-reviewer`, `blind-hunter`, `edge-case-hunter`, `adversarial-general` | `review-mode: full` only |

Tier 1 and Tier 2 are separated by a barrier so Tier 2 never starts until all Tier 1 agents complete.

## Multi-provider support (GitHub / Bitbucket Cloud / GitLab)

Since v0.2.0 the same container image drives PR/MR reviews on GitHub, Bitbucket Cloud, and GitLab. The provider is selected via the `VCS_PROVIDER` env var:

| `VCS_PROVIDER` | Provider | Python module |
|---|---|---|
| `github` (default) | GitHub | `ai_pr_review/vcs/github.py` |
| `bitbucket` | Bitbucket Cloud | `ai_pr_review/vcs/bitbucket.py` |
| `gitlab` | GitLab | `ai_pr_review/vcs/gitlab.py` |

The provider is resolved once at startup via `provider_from_env` and passed through `ReviewRuntime`. Invalid provider values fail fast with a clear error.

## Code suggestions

When `enable-suggestions` is `true` (the default), eligible LLM agents emit an optional `suggested_code` field (and optional `start_line` for multi-line replacements). The VCS provider posting layer wraps these in a provider-native suggestion fence.

**Eligible agents** (system prompt is augmented with `prompts/suggestion-addendum.md`): `code-reviewer`, `edge-case-hunter`, `security-reviewer`, `silent-failure-hunter`, `blind-hunter`.

Not eligible: `architecture-reviewer`, `adversarial-general`, `pr-summarizer`. Static analyzers never emit suggestions.

**Prompt composition.** The dispatch layer composes the base prompt with up to four shared trailers at runtime:
- `prompts/_governance.md` — Asimov-style severity lens, don't-reinvent-the-wheel detection, and verify-before-naming + secret-redaction posture. Applied to all 7 finding-producing agents (not `pr-summarizer`). Always-on; no env var toggle.
- `prompts/_knowledge-cutoff.md` — HARD CONSTRAINT block against version-existence hallucinations. Applied to all 7 finding-producing agents (not `pr-summarizer`).
- `prompts/_trailer-findings.md` — `json-findings` schema instruction. Applied to all 7 finding-producing agents.
- `prompts/suggestion-addendum.md` — "Apply suggestion" formatting. Gated by `AI_ENABLE_SUGGESTIONS`; applied only to the 5 eligible agents.

Composition order: base prompt + governance + knowledge-cutoff + findings-trailer + (optional) suggestion-addendum. The order is deliberate for Anthropic prompt-cache locality — the existing `_knowledge-cutoff → _trailer-findings → suggestion-addendum` byte sequence at the tail is preserved unchanged.

**Validation guards** (applied in the VCS provider posting layer):
1. `start_line` must be a positive integer and be <= `line`.
2. Multi-line ranges are capped at `MAX_SUGGESTION_RANGE=100` lines.
3. `suggested_code` containing triple backticks is rejected (fence escape prevention).
4. For multi-line suggestions, every line in `start_line..line` must appear in the diff's new-file side.
5. When any guard fails the suggestion is dropped with a WARNING; the finding still posts with natural-language remediation.

**Body finding rendering.** When a finding with `suggested_code` is routed to the review body, the provider posting layer renders the suggestion as a plain code fence inside the collapsible `<details>` accordion. The triple-backtick sanitization guard applies to body suggestions as well.

**Bitbucket** does not render suggestion fences. **GitLab** supports suggestion fences using GitLab's native `suggestion` syntax.

## Suppressions

`config/suppressions.json` is a JSON array of suppression rules evaluated against merged findings. All `match` fields are optional and ANDed:

- `file` — substring match on finding's `file`
- `line` — exact integer match
- `code` — finding text starts with this prefix
- `pattern` — regex (case-insensitive) matched against finding text

An optional `verify` field triggers pre-suppression verification:

| Value | Extracts | Checks |
|-------|----------|--------|
| `github-release` | `owner/repo@vN` | `gh api repos/{owner}/{repo}/git/ref/tags/{tag}` |
| `npm` | `pkg@version` or `"pkg": "version"` | `registry.npmjs.org/{pkg}/{version}` |
| `pypi` | `pkg==version` | `pypi.org/pypi/{pkg}/{version}/json` |
| `go-module` | `module@vX.Y.Z` | `proxy.golang.org/{module}/@v/{version}.info` |
| `cargo` | `pkg = "version"` or `pkg@version` | `crates.io/api/v1/crates/{pkg}/{version}` |
| `docker-hub` | `image:tag` or `ns/image:tag` | `hub.docker.com/v2/namespaces/{ns}/repositories/{name}/tags/{tag}` |
| `ruby-org` | Ruby MRI `X.Y.Z` | `cache.ruby-lang.org/pub/ruby/{MAJ.MIN}/ruby-{MAJ.MIN.PATCH}.tar.gz` |

If verification confirms the version exists, the suppression stands. If the API returns a non-zero exit, the finding is kept. Private registries (GHCR, GCR, ECR) are not supported.

Consuming repos can add **local suppressions** at `.github/ai-pr-review/suppressions.json`, merged with global rules at runtime.

## Token usage and cost estimation

Token counts are accumulated per agent across all LLM calls. For Google Gemini, `cache_read` reports `cachedContentTokenCount` when present; thinking tokens (`thoughtsTokenCount`) are added to the output count since they are billed at the output rate.

`config/model-pricing.json` maps model ID patterns to display names and per-token rates. Each entry carries four rates: `input_rate`, `output_rate`, `cache_write_rate`, and `cache_read_rate` (all cost per 1M tokens). The token table uses an adaptive column layout — 6 columns when no rows have cache activity, 8 columns when any row does.

## Prompt caching

### Anthropic / Bedrock

When `AI_PROVIDER` is `anthropic` or `bedrock-proxy`, the LLM client uses Anthropic's ephemeral cache (5-minute TTL) via `cache_control: {type: "ephemeral"}` markers. Enabled by `LLM_PROMPT_CACHING` (default: `auto`):

- `auto` — enabled for `anthropic` and `bedrock-proxy`; no-op for OpenAI and Google Gemini.
- `true` — force-enable markers.
- `false` — force-disable; falls back to the legacy request layout.

#### Shared-cache layout (issue #142)

Anthropic's cache key is CUMULATIVE — a hash of the full prefix up to each `cache_control` marker. Our 5-8 agents per review split into **two cache cohorts** by context variant:
- **code context** cohort — code-reviewer, silent-failure-hunter, security-reviewer, edge-case-hunter, adversarial-general
- **full context** cohort — pr-summarizer, architecture-reviewer

To unlock cross-agent caching, the request is restructured when caching is enabled so the shared context becomes the FIRST system content block with a cache_control marker, and the per-agent prompt becomes the SECOND system block without a marker:

```
system: [
  { text: "<shared code context>",  cache_control: ephemeral },
  { text: "<per-agent system prompt>" }
]
messages: [{ role: "user", content: "Please perform your review now." }]
```

**Live-benchmarked impact** (Sonnet 4.6, 5 agents, ~25 KB shared context):

| Run | input | cache_write | cache_read | est. cost | vs no cache |
|---|---:|---:|---:|---:|---:|
| A (caching off) | 56,652 | 0 | 0 | $0.189 | baseline |
| B (cold cache, first run) | 13,722 | 8,593 | 34,372 | $0.103 | **-46%** |
| C (hot cache, re-run within 5 min) | 13,722 | 0 | 42,965 | $0.073 | **-61%** |

Weighted across typical PR traffic (70% cold / 25% hot): **~47% cheaper on average**.

#### Cache priming (issues #144, #153) — opt-in tuning knob

`AI_CACHE_PRIMING=true` serializes 1-2 cache-writing calls before Tier 1 fan-out so remaining agents hit a guaranteed-warm cache:

1. `code-reviewer` (Sonnet primer for the code context cohort) concurrently with
2. `security-reviewer` (Opus primer, pulled forward from Tier 2 in full mode)

**Default is `false`.** Investigation (#153) concluded that opportunistic cache hits from the parallel fan-out are sufficient in normal environments:

- The 7-agent fan-out has ~100-500ms natural stagger between calls (different system-prompt sizes 5-35 KB, different model TTFT, HTTP connection-pool serialization), which is enough time for the first cache write to become visible before subsequent agents reach the Anthropic API.
- A single-sample benchmark (PR #137, ~1185 diff lines, 8 agents, Bedrock Sonnet/Opus proxy) showed **zero cost difference** vs unprimed, with priming adding **+30s wall-clock (+20%)** overhead from the serial barrier.
- Anthropic's cache becomes visible faster than worst-case documentation suggests; agents starting within a few hundred milliseconds typically see each other's cache writes.

Cache priming remains as an opt-in for environments where opportunistic hits fail — strict rate-limit policies that serialize concurrent requests, proxy implementations that queue rather than multiplex, or single-agent sequential dispatch modes. See [configuration reference](configuration.md) for `AI_CACHE_PRIMING`.

#### Semantic change

The shared-cache layout moves the diff/context from the user message into system[0]. Empirically Claude treats late-system content equivalently to user-turn content (verified by `claude/bench-quality.sh`). The layout is used only when prompt caching is active; `LLM_PROMPT_CACHING=false` preserves the legacy shape.

#### Cache-minimum threshold

Anthropic caches only prefixes >= 1024 tokens (Sonnet/Opus) or >= 2048 tokens (Haiku). Contexts below ~8KB may silently not cache — verify via `cache_creation_input_tokens` > 0 in the first response.

### OpenAI automatic prefix caching

OpenAI provides automatic prefix caching (50% discount on cached input tokens) for prompts >= 1024 tokens. No explicit markers are needed. The OpenAI client extracts `usage.prompt_tokens_details.cached_tokens` from the response and reports it as `cache_read`. The `input` count is adjusted to exclude cached tokens (matching Anthropic's convention) so the cost formula works correctly across providers.

#### Shared-cache layout for OpenAI (issue #164)

The OpenAI request is restructured for first-party OpenAI (`AI_PROVIDER=openai`) to maximize the shared prefix across agents in the same cohort. The shared context (code context or full context) is placed first in the system message, followed by a separator and the per-agent prompt. The user message becomes a minimal sentinel (`"Please perform your review now."`). This mirrors the Anthropic shared-cache layout (issue #142) but uses string concatenation instead of a content-block array (OpenAI doesn't support system arrays). `blind-hunter` uses the blind context and stays on its own prefix.

`openai-compatible` endpoints keep the legacy layout (system = agent prompt, user = shared context) because third-party providers may have different caching behavior or no prefix caching at all.

### Google Gemini

Gemini uses a different caching API. `LLM_PROMPT_CACHING` has no effect on Gemini requests. Implicit caching (`cachedContentTokenCount`) is extracted when present.

## Retry and resilience

The LLM client retries transient API failures (HTTP 408, 429, 500, 502, 503, 504, and Cloudflare 520-524) with exponential backoff and jitter.

| Variable | Default | Description |
|----------|---------|-------------|
| `LLM_RETRY_COUNT` | `3` | Number of retry attempts (set to 0 to disable) |
| `LLM_RETRY_BASE_DELAY` | `2` | Base delay in seconds (doubles each retry) |

The VCS provider HTTP layer wraps critical API calls with retry logic (3 retries, exponential backoff + jitter) across all three providers.

## Graceful failure handling

When an agent call fails, the dispatch layer:
1. Logs a WARNING with the failure type and last error message
2. Records the agent name as failed and continues to the next agent

After all agents complete:
- If **all** finding agents failed, the review aborts with exit 1
- Otherwise, failed agents are tracked and reported in the summary comment
- An empty-findings review is downgraded from APPROVE to COMMENT

## Standalone review mode

In addition to reviewing open PRs, the action supports a **standalone** mode that reviews any branch or commit SHA and posts findings as a GitHub issue (or GitLab issue) rather than a PR review.

### Triggering

Via `workflow_dispatch` or programmatically by setting `REVIEW_TARGET=standalone` along with the required provider credentials and repository env vars.

### Behavior differences

- No PR comment or PR review is posted
- All findings are posted as a single GitHub/GitLab issue
- SHA watermark / incremental diff is skipped (always full diff)
- `REVIEW_TARGET=standalone` is rejected for Bitbucket (no Issues product)

### Issue output format

- Title: `🚨 AI Review: High risk — abc1234 on main` (icon is severity-dependent via `severity_icon`)
- Labels: `ai-review` (always), `ai-review-action-needed` (Critical/High)

## Multi-arch container image

The `Dockerfile` builds for linux/amd64 and linux/arm64. Each binary download uses a `case "${TARGETARCH}"` block with per-arch SHA256 checksums. pip-installed tools (ruff, semgrep, checkov) and composer-installed tools (phpcs, phpstan) are arch-neutral.

### Multi-stage layout

- **`builder`** — installs build-time tooling, downloads analyzer binaries, pip-installs ruff/semgrep/checkov, composer-installs phpcs/phpstan. Semgrep registry rulesets are **not** baked into the image (they are use-restricted under the Semgrep Rules License v1.0); the semgrep analyzer uses `--config=auto` to fetch rules at runtime instead. See [Third-party licenses](#third-party-licenses).
- **final stage** — slim runtime with `bash`, `ca-certificates`, `curl`, `git`, `jq`, `php-cli` + extensions, `python3`. Copies `/usr/local/bin` and `/usr/local/lib/python${PYTHON_VERSION}/dist-packages` (parameterized via `ARG PYTHON_VERSION`, default `3.14` to match Ubuntu 26.04) wholesale from the builder. The Python package and action assets are copied at the end so source-only changes don't invalidate heavy builder layers.

## Test architecture

Tests live in `tests/python/` and use pytest. Key test files:

| File | Covers |
|---|---|
| `test_runtime.py` | Assembly boundary: `build_review_runtime()`, `SkipPlan`, SARIF routing, provider factory seam |
| `test_orchestrate.py` | `run_review()` happy path, skip path, summary/findings failure, token table |
| `test_cli.py` | `run_compute()`, `compute` command, `slash` command, `parse_changed_files_payload()`, `AI_PR_REVIEW_SCRIPT_DIR` resolution |
| `test_config.py` | `ReviewConfig.from_env()`, `resolve_models()`, unknown-var detection, deprecation warnings |
| `test_manifest.py` | `build_changed_files()`, `build_manifest_text()`, `parse_changed_files_payload()` (including None-entry guard) |
| `test_language_profiles.py` | `load_language_profiles()` happy path, OSError fail-soft, missing profile key |
| `test_suppress.py`, `test_findings.py` | Suppression pipeline and findings merge |
| `test_sarif.py`, `test_bridge.py` | SARIF parsing and static analyzer bridge |
| `test_telemetry.py`, `test_logging.py` | Telemetry sink dispatch and structured log formatting |
| `test_feedback_*.py` | Learning loop: inject, store, retention, models |
| `vcs/test_github.py`, `vcs/test_gitlab.py` | GitHub and GitLab provider unit tests |

Run with `pytest tests/python/ -q`.

## Environment variable reference

Variables consumed by the engine but not exposed as action inputs:

| Variable | Default | Description |
|----------|---------|-------------|
| `MAX_DIFF_LINES` | `5000` | Maximum diff lines before skipping review (mapped from `max-diff-lines` action input) |
| `AI_TEMPERATURE` | `0.3` | Sampling temperature for LLM calls (clamped to [0, 2]) |
| `AI_PARALLEL` | `true` | Tiered parallel agent execution |
| `AI_CONFIDENCE_THRESHOLD` | `75` | Minimum confidence score for findings |
| `AI_MAX_INLINE` | `25` | Maximum inline review comments per run |
| `AI_MAX_TOKENS_PER_AGENT` | `8192` | Max output tokens per LLM agent call; clamped to [256, 65536] |
| `AI_ENABLE_SUGGESTIONS` | `true` | Enable "Apply suggestion" buttons (GitHub and GitLab; ignored on Bitbucket) |
| `LLM_PROMPT_CACHING` | `auto` | Anthropic/Bedrock prompt caching. Valid: `auto`, `true`, `false` |
| `AI_CACHE_PRIMING` | `false` | Opt-in cache-writing serialization before parallel fan-out |
| `VCS_PROVIDER` | `github` | Selects the VCS provider. Valid: `github`, `bitbucket`, `gitlab` |
| `BITBUCKET_EMAIL` | — | Bitbucket-only. Bot user email (Basic-auth username) |
| `BITBUCKET_API_TOKEN` | — | Bitbucket-only. API token (Basic-auth password) |
| `BITBUCKET_WORKSPACE` / `BITBUCKET_REPO_SLUG` | — | Bitbucket-only. Optional explicit override |
| `GITLAB_TOKEN` | — | GitLab-only. Access token with `api` scope; falls back to `CI_JOB_TOKEN` |
| `GITLAB_API_URL` | `https://gitlab.com/api/v4` | GitLab-only. API base URL for self-hosted instances |
| `GITLAB_PROJECT_ID` | — | GitLab-only. Numeric project ID |
| `GITLAB_MR_DIFF_BASE_SHA` | — | GitLab-only. Base SHA for inline discussion positions |
| `GITLAB_BOT_USERNAME` | — | GitLab-only. Bot username for stale thread resolution |

## Provider model defaults

| Provider | Standard model | Premium model |
|----------|---------------|---------------|
| `anthropic` | `claude-sonnet-5` | `claude-opus-4-8` |
| `openai` | `gpt-5.4-mini` | `gpt-5.4` |
| `openai-compatible` | (user-specified) | same as standard |
| `google` | `gemini-2.5-flash` | `gemini-2.5-pro` |
| `bedrock-proxy` | `us.anthropic.claude-sonnet-5` | `global.anthropic.claude-opus-4-7` |
