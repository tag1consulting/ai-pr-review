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

The `source` field is optional in agent output — `extract_findings()` stamps the agent name automatically if the field is absent. Static analyzers hard-code their source values in their jq projection.

`suggested_code` and `start_line` are optional fields emitted when the `enable-suggestions` action input is `true` (the default). See [Code suggestions](#code-suggestions) below.

`review.sh` uses `extract_findings()` to parse this block and validate shape. Findings below confidence 75 are filtered out. Duplicates are deduped using proximity-based matching: findings in the same file within 3 lines of each other are merged into a single cluster, keeping the highest-severity finding. The dedup step carries a `sources` array on the surviving finding, unioning all sources from the cluster. When multiple sources are present, the post-review script renders `[first-source] *(also flagged by: other)*` attribution (sources are sorted alphabetically).

## Python engine runtime flow

When `AI_PR_REVIEW_ENGINE=python`, the entrypoint is `ai_pr_review.cli:review`. The Click command parses flags, sets up logging, and calls `_run_review_async()`, which:

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

## Findings pipeline (review.sh phases)

1. **Phase 0** — Fetch PR/MR description from VCS API. Compute diff (incremental if SHA watermark found, else full PR diff). Exclude lockfiles, vendor dirs, node_modules.
2. **Phase 1** — Build shared message files (full context, code context, blind/no-context). Call agents via `call_agent()` (sequential, default) or `call_agent_bg()` (parallel, opt-in). See [Parallel agent execution](#parallel-agent-execution) below.
3. **Phase 2** — Extract `json-findings` from each agent output. Merge with static analyzer findings. Filter by confidence >= 75. Apply `config/suppressions.json`. Deduplicate using proximity-based matching (findings within 3 lines in the same file are merged).
4. **Phase 3** — Format findings as markdown. Call the provider-specific post-review script to post everything. Findings whose line is in the diff go inline (up to `AI_MAX_INLINE`); the rest go into the review body via `format_body_finding()`, which wraps remediation in a collapsible `<details>` accordion.

## Incremental review / SHA watermark

The SHA of the last-reviewed commit is stored in an HTML comment embedded in the PR/MR summary comment (`<!-- ai-pr-review-summary sha=<sha> -->`). The provider-specific post-review script's `--get-last-sha` mode extracts it. Subsequent pushes diff from that SHA to HEAD. The watermark is advanced at the end of each run when the summary comment is posted with the new HEAD SHA.

Each post-review script keeps at most one summary comment on the PR/MR by calling `_cleanup_duplicate_summary_comments()` after each upsert. Duplicates can accumulate when two runs fire concurrently. Cleanup is non-fatal: DELETE failures emit a WARNING.

To force a full-PR diff for a single run, add the `ai-review-rescan` label to the PR. The workflow sets `FORCE_FULL_DIFF=true` via the `env:` block, which causes `review.sh` to skip the `--get-last-sha` call (leaving `LAST_REVIEWED_SHA=""`) and fall through to the full `origin/BASE_REF...HEAD_SHA` diff.

## Context message variants

Three message files are built and passed selectively to agents:

- **`FULL_CONTEXT_MSG`** — manifest + PR description (title + body) + commit log + CLAUDE.md excerpt (first 2000 chars) + language profiles + diff
- **`CODE_CONTEXT_MSG`** — manifest + PR description (title + body) + language profiles + diff (no commit log or project context)
- **`BLIND_MSG`** — raw diff only (intentional zero context for `blind-hunter`)

The PR description is fetched from the VCS provider API (`fetch_pr_description()`) early in Phase 0. It includes the PR/MR title and body (truncated to 4000 chars). HTML comment lines from PR templates are stripped. The section is omitted entirely when the title and body are both empty (e.g., standalone reviews or PRs with no description). This gives agents visibility into the author's stated intent, reducing false positives on intentional changes.

## Parallel agent execution

Phase 1 runs agents in a tiered fan-out mode by default, reducing wall-clock time from ~5-7 minutes to ~2 minutes in `full` mode.

Disable via `parallel: false` action input or `AI_PARALLEL=false` env var (default: `true`).

> **Breaking change (direct-script users):** Prior to issue #73, `review.sh` defaulted to `AI_PARALLEL=false` when the variable was unset, while `action.yml` defaulted to `true`. The default is now `true` in both invocation paths.

### Tier groupings

| Tier | Agents | When |
|------|--------|------|
| Tier 1 | `pr-summarizer` (first run only), `code-reviewer`, `silent-failure-hunter` (conditional, standard model in quick / premium in full) | Always |
| Tier 1 (static analyzers, concurrent with Tier 1) | All `analyzers/run-*.sh` scripts | Always (graceful no-op if binary absent) |
| Tier 2 | `architecture-reviewer`, `security-reviewer`, `blind-hunter`, `edge-case-hunter`, `adversarial-general` | `review-mode: full` only |

Tier 1 and Tier 2 are separated by a `wait` barrier so Tier 2 never starts until all Tier 1 agents complete.

### IPC mechanism

Backgrounded agents (via `call_agent_bg`) cannot mutate parent arrays. State is passed via sidecar files:
- `${output}.name` — agent name (written at start, read on failure)
- `${output}.tokens` — token log entry (written on success)
- `${output}.failed` — exists if the agent failed (empty sentinel file)
- `${output}.truncated` — exists if the response was truncated

`collect_parallel_results <output_files...>` reads these sidecars in roster order after each `wait` and reconstructs `FAILED_AGENTS` and `TOKEN_LOG` in the parent shell.

### Adding a new parallel agent

When `AI_PARALLEL=true`, new agents must be placed into a tier and added to both the parallel and sequential code paths:
- Add to the appropriate `TIER{1,2}_OUTPUTS+=` and `call_agent_bg ... &` block.
- Also add the sequential `call_agent` call in the `else` branch.

## Multi-provider support (GitHub / Bitbucket Cloud / GitLab)

Since v0.2.0 the same container image drives PR/MR reviews on GitHub, Bitbucket Cloud, and GitLab. The provider is selected via the `VCS_PROVIDER` env var:

| `VCS_PROVIDER` | Provider | Post-review script |
|---|---|---|
| `github` (default) | GitHub | `post-review.sh` |
| `bitbucket` | Bitbucket Cloud | `post-review-bitbucket.sh` |
| `gitlab` | GitLab | `post-review-gitlab.sh` |

`review.sh` resolves `POST_REVIEW_SCRIPT` once at startup based on `VCS_PROVIDER` and uses it at every post-review call site. Invalid provider values fail fast with a clear error.

### Shared helpers (`vcs/common.sh`)

The three post-review scripts source `vcs/common.sh` for helpers that have no provider coupling:

- `severity_icon` — severity-to-emoji mapping
- `format_source_tag` — renders `[agent] *(also flagged by: ...)*` attribution
- `classify_risk` — maps findings JSON to `<label>|<review-event>`
- `format_body_finding` — renders one body bullet with optional `<details>` remediation / suggestion block (used by GitHub + GitLab; Bitbucket uses `render_findings_markdown`)
- `build_agent_prompt` — collapsible "Prompt for AI agents" block
- `parse_valid_lines` — emits `file:line` for `+` diff lines (inline comment anchors)
- `parse_diff_new_lines` — emits `file:line` for `+` AND context lines (multi-line suggestion range validation)
- `mktemp_tracked`, `cleanup` — temp-file lifecycle

Each post-review script sources `vcs/common.sh` via a path derived from `BASH_SOURCE[0]` so it works whether invoked from the repo root or from `/opt/ai-pr-review/` inside the container.

**What stays per-script (genuinely divergent):**

- `truncate_body` — error message is provider-specific ("65,536 bytes" / "32,768 chars" / "MR note size limit")
- `MAX_BODY_SIZE` — 64000 / 32000 / 250000 per provider
- `_cleanup_duplicate_summary_comments`, `find_existing_summary_id`, `build_comment_body`, `post_summary_with_findings`, `update_sha_marker` — share names across scripts but differ in API wrapper (`gh_api_retry` vs `bb_api` vs `gl_api`), return contract, and formatting needs.

**Testing:** `tests/post_review_functions.bats`, `tests/post_review_bitbucket_functions.bats`, and `tests/post_review_gitlab_functions.bats` use `load_function` to extract the shared helpers from `vcs/common.sh` once. `truncate_body` still has 3-way parity tests.

## Code suggestions

When `enable-suggestions` is `true` (the default), eligible LLM agents emit an optional `suggested_code` field (and optional `start_line` for multi-line replacements). The post-review script wraps these in a provider-native suggestion fence.

**Eligible agents** (system prompt is augmented with `prompts/suggestion-addendum.md`): `code-reviewer`, `edge-case-hunter`, `security-reviewer`, `silent-failure-hunter`, `blind-hunter`.

Not eligible: `architecture-reviewer`, `adversarial-general`, `pr-summarizer`. Static analyzers never emit suggestions.

**Prompt composition.** `effective_prompt()` in `lib/agents.sh` composes the base prompt with up to three shared trailers at runtime:
- `prompts/_knowledge-cutoff.md` — HARD CONSTRAINT block against version-existence hallucinations. Applied to all 7 finding-producing agents (not `pr-summarizer`).
- `prompts/_trailer-findings.md` — `json-findings` schema instruction. Applied to all 7 finding-producing agents.
- `prompts/suggestion-addendum.md` — "Apply suggestion" formatting. Gated by `AI_ENABLE_SUGGESTIONS`; applied only to the 5 eligible agents.

Composition order: base prompt + knowledge-cutoff + findings-trailer + (optional) suggestion-addendum. The composed output is written to a temp file under `${EFFECTIVE_PROMPT_PREFIX}-*.md`.

**Validation guards** (applied in `post-review.sh` and `post-review-gitlab.sh`):
1. `start_line` must match `^[1-9][0-9]*$` (positive integer, no leading zeros or 0) and be <= `line`.
2. Multi-line ranges are capped at `MAX_SUGGESTION_RANGE=100` lines.
3. `suggested_code` containing triple backticks is rejected (fence escape prevention).
4. For multi-line suggestions, every line in `start_line..line` must appear in the diff's new-file side.
5. When any guard fails the suggestion is dropped with a WARNING; the finding still posts with natural-language remediation.

**Body finding rendering.** When a finding with `suggested_code` is routed to the review body, `format_body_finding()` renders the suggestion as a plain code fence inside the collapsible `<details>` accordion. The triple-backtick sanitization guard applies to body suggestions as well.

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

`TOKEN_LOG` in `review.sh` accumulates `"agent: input=N output=N cache_creation=N cache_read=N model=ID"` entries from `TOKENS:` lines emitted by `llm-call.sh` on stderr. For Google Gemini, `cache_read` reports `cachedContentTokenCount` when present; thinking tokens (`thoughtsTokenCount`) are added to the output count since they are billed at the output rate. `call_google()` also emits `THINKING: N tokens` to stderr.

`config/model-pricing.json` maps model ID patterns to display names and per-token rates. Each entry carries four rates: `input_rate`, `output_rate`, `cache_write_rate`, and `cache_read_rate` (all cost per 1M tokens). `emit_token_table()` generates the markdown table with an adaptive column layout — 6 columns when no rows have cache activity, 8 columns when any row does.

## Prompt caching

### Anthropic / Bedrock

When `AI_PROVIDER` is `anthropic` or `bedrock-proxy`, `llm-call.sh` uses Anthropic's ephemeral cache (5-minute TTL) via `cache_control: {type: "ephemeral"}` markers. Enabled by `LLM_PROMPT_CACHING` (default: `auto`):

- `auto` — enabled for `anthropic` and `bedrock-proxy`; no-op for OpenAI and Google Gemini.
- `true` — force-enable markers.
- `false` — force-disable; falls back to the legacy request layout.

#### Shared-cache layout (issue #142)

Anthropic's cache key is CUMULATIVE — a hash of the full prefix up to each `cache_control` marker. Our 5-8 agents per review split into **two cache cohorts** by user-message variant:
- `CODE_CONTEXT_MSG` cohort — code-reviewer, silent-failure-hunter, security-reviewer, edge-case-hunter, adversarial-general
- `FULL_CONTEXT_MSG` cohort — pr-summarizer, architecture-reviewer

To unlock cross-agent caching, the request is restructured when caching is enabled so the shared context becomes the FIRST system content block with a cache_control marker, and the per-agent prompt becomes the SECOND system block without a marker:

```
system: [
  { text: "<shared CODE_CONTEXT_MSG>",  cache_control: ephemeral },
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

1. `code-reviewer` (Sonnet primer for CODE_CONTEXT_MSG cohort) concurrently with
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

OpenAI provides automatic prefix caching (50% discount on cached input tokens) for prompts >= 1024 tokens. No explicit markers are needed. `call_openai()` extracts `usage.prompt_tokens_details.cached_tokens` from the response and reports it as `cache_read` in the TOKENS line. The `input` count is adjusted to exclude cached tokens (matching Anthropic's convention) so the cost formula works correctly across providers.

#### Shared-cache layout for OpenAI (issue #164)

`_build_openai_body()` restructures the request for first-party OpenAI (`AI_PROVIDER=openai`) to maximize the shared prefix across agents in the same cohort. The shared context (CODE_CONTEXT_MSG or FULL_CONTEXT_MSG) is placed first in the system message, followed by a separator and the per-agent prompt. The user message becomes a minimal sentinel (`"Please perform your review now."`). This mirrors the Anthropic shared-cache layout (issue #142) but uses string concatenation instead of a content-block array (OpenAI doesn't support system arrays). `blind-hunter` uses `BLIND_MSG` and stays on its own prefix.

`openai-compatible` endpoints keep the legacy layout (system = agent prompt, user = shared context) because third-party providers may have different caching behavior or no prefix caching at all.

### Google Gemini

Gemini uses a different caching API. `LLM_PROMPT_CACHING` has no effect on Gemini requests. Implicit caching (`cachedContentTokenCount`) is extracted when present.

## Retry and resilience

`llm-call.sh` retries transient API failures (HTTP 408, 429, 500, 502, 503, 504, and Cloudflare 520-524) and transient curl failures (exit codes 7, 28, 56) with exponential backoff and jitter.

| Variable | Default | Description |
|----------|---------|-------------|
| `LLM_RETRY_COUNT` | `3` | Number of retry attempts (set to 0 to disable) |
| `LLM_RETRY_BASE_DELAY` | `2` | Base delay in seconds (doubles each retry) |

Exit codes from `llm-call.sh`:
- **0** — success
- **1** — permanent error (bad API key, invalid request, unknown provider)
- **2** — transient error (all retries exhausted)
- **3** — content issue (provider safety/recitation filter blocked response)

`post-review.sh` wraps critical GitHub API calls with `gh_api_retry()` (3 retries, 2s/4s/8s backoff). `post-review-gitlab.sh` has equivalent retry logic in `gl_api()` (3 retries, exponential backoff + jitter).

## Graceful failure handling

When an agent call fails, `call_agent()` in `lib/agents.sh`:
1. Logs a WARNING with the failure type and last error message
2. Appends the agent name to the `FAILED_AGENTS` array
3. Writes empty output and continues to the next agent

After all agents complete:
- If **all** finding agents failed, the review aborts with exit 1
- Otherwise, `FAILED_AGENTS` is exported as `AI_REVIEW_FAILED_AGENTS` (colon-separated) to the post-review script
- The post-review script downgrades an empty-findings review from APPROVE to COMMENT

## Standalone review mode

In addition to reviewing open PRs, the action supports a **standalone** mode that reviews any branch or commit SHA and posts findings as a GitHub issue (or GitLab issue) rather than a PR review.

### Triggering

Via `workflow_dispatch` or programmatically:
```bash
export REVIEW_TARGET=standalone
export AI_PROVIDER=anthropic ANTHROPIC_API_KEY=<key> GH_TOKEN=<token>
export GITHUB_REPOSITORY=owner/repo BASE_REF=main HEAD_SHA=<sha>
bash review.sh
```

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

- **`builder`** — installs build-time tooling, downloads analyzer binaries, pip-installs ruff/semgrep/checkov, composer-installs phpcs/phpstan, fetches semgrep rulesets.
- **final stage** — slim runtime with `bash`, `ca-certificates`, `curl`, `git`, `jq`, `php-cli` + extensions, `python3`. Copies `/usr/local/bin` and `/usr/local/lib/python3.12/dist-packages` wholesale from the builder. Action scripts are copied at the end so source-only changes don't invalidate heavy builder layers.

The final stage uses `COPY post-review*.sh` (a glob) so future provider scripts are picked up without Dockerfile churn.

## Test architecture

**Bash engine:** Tests live in `tests/` and use [bats-core](https://github.com/bats-core/bats-core). Because sourcing the scripts triggers the full orchestration pipeline, `tests/test_helper.bash` extracts individual function definitions using an awk brace-depth tracker and `eval`s them into the test shell.

- Tests cover pure functions from `review.sh`, `llm-call.sh`, `vcs/common.sh`, and the provider-specific scripts
- Fixture files in `tests/fixtures/` provide sample agent output for `extract_findings` tests
- Static analyzer scripts are tested via their own `.bats` files using the mock env var pattern (set `<TOOL>_MOCK_FILE` to a fixture path)

To add a test for a new function, call `load_function "$script" "function_name"` in the `setup()` block of the relevant `.bats` file.

**Python engine:** Tests live in `tests/python/` and use pytest. Key test files:

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

Run with `python -m pytest tests/python/ -q`.

## Environment variable reference

Variables consumed by the scripts but not exposed as action inputs:

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
| `VCS_PROVIDER` | `github` | Selects the post-review script. Valid: `github`, `bitbucket`, `gitlab` |
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
| `anthropic` | `claude-sonnet-4-6` | `claude-opus-4-7` |
| `openai` | `gpt-5.4-mini` | `gpt-5.4` |
| `openai-compatible` | (user-specified) | same as standard |
| `google` | `gemini-2.5-flash` | `gemini-2.5-pro` |
| `bedrock-proxy` | `us.anthropic.claude-sonnet-4-6` | `global.anthropic.claude-opus-4-7` |
