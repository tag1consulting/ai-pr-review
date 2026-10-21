---
layout: default
title: Configuration
nav_order: 3
has_children: true
---

# Configuration

## Action inputs

This table lists the core inputs of the root `action.yml` (direct-action). Other root inputs appear in the [Repository variables](#repository-variables) table. These are `context-enrichment`, `context-max-queries`, `fail-on-findings`, `feedback-loop`, `suppress-walkthrough`, `token-usage-display`, `token-usage-warn-usd`, `max-cost-usd`, `fail-on-cost-ceiling`, `cost-ceiling-unpriced`, `finding-badges`, `context-window-margin`, and `fail-on-context-window`. Two more root inputs, `judge-pass` and `profile-max-tokens`, appear in the environment variable sections below. The container action (`container-action/action.yml`) mirrors all root inputs. It adds only `image-tag` (the container image tag to pull) and `registry-token` (deprecated and unused, because the image is public). See [`examples/workflows/pr-review.yml`](https://github.com/tag1consulting/ai-pr-review/blob/main/examples/workflows/pr-review.yml) for the container-action input set in context.

| Input | Required | Default | Description |
|-------|----------|---------|-------------|
| `provider` | No | `anthropic` | LLM provider |
| `api-key` | **Yes** | n/a | API key for the provider |
| `base-url` | No | `''` | Base URL for OpenAI-compatible or bedrock-proxy |
| `model-standard` | No | Per-provider default | Model for standard agents |
| `model-premium` | No | Per-provider default | Model for premium agents (full mode) |
| `review-mode` | No | `''` | `quick` or `full`. Empty (default): defer to `.ai-pr-review/policy.yml` route matching if present, else `quick`. See [Policies](policy). |
| `review-target` | No | `pr` | `pr` (PR review) or `standalone` (deprecated, emits a runtime warning). Standalone only disables merge-commit filtering during diff computation. It does not post findings anywhere. The bash engine removed in v2.0.0 had the issue-posting code, and the Python engine never reimplemented it. Formal removal is planned for a future major version. See [issue #623](https://github.com/tag1consulting/ai-pr-review/issues/623). |
| `max-diff-lines` | No | `5000` | Max diff lines before skipping review |
| `pr-number` | No | `''` | PR number (required for the `pr` target, unused in standalone) |
| `base-ref` | **Yes** | n/a | Base branch name |
| `head-ref` | No | `''` | Head branch name (for example `feature/foo`). Optional. Used only for head-branch route matching in `.ai-pr-review/policy.yml` (see [Policies](policy)). |
| `head-sha` | **Yes** | n/a | Head commit SHA |
| `policy-source` | No | `base-ref` | Where `.ai-pr-review/policy.yml` (or its `.github/ai-pr-review/policy.yml` fallback) is read from: `base-ref` (read with `git show`, never from the PR head) or `workspace` (the checked-out tree directly). On Bitbucket this setting does not apply to the verdict owners file `.bitbucket/CODEOWNERS`. That file is always read from the target branch (see [Bitbucket setup](bitbucket-setup#dismissing-findings)). See [Policies: Security](policy#security-loaded-from-the-base-ref-never-the-pr-head--but-only-when-your-trigger-needs-it) before setting `workspace`. |
| `github-token` | **Yes** | n/a | GitHub token with `pull-requests: write` |
| `parallel` | No | `true` | Run agents in parallel (tiered fan-out). If you hit provider rate limits, set to `false` to run agents one at a time. |
| `temperature` | No | `0.3` | Sampling temperature for LLM calls (float in [0, 2]). Lower values produce more deterministic output. The action does not send this value to models that reject or discourage a non-default value (currently Claude Opus 4.7, 4.8, 5 and 5.5, Claude Sonnet 5 and 5.5, Claude Haiku 5.5, OpenAI `o1`/`o3`/`o4`, `gpt-5`, `gpt-5.5`, `gpt-5.6-*`, `gpt-6*`, and Gemini 3), where the provider default applies. `gpt-5.4` and `gpt-5.4-mini` still receive it. The exact list is `resolve_temperature()` in `ai_pr_review/llm/_config.py`. |
| `max-inline` | No | `25` | Maximum inline review comments per run. The action moves any excess to the review body. |
| `max-tokens-per-agent` | No | `32768` | Max output tokens per LLM agent call (clamped to [256, 65536]). v1.3.0 lowered the default from 32768 to 16384. The default returned to 32768 for #642. |
| `analyzer-concurrency` | No | `4` | Maximum simultaneous native static-analyzer subprocesses. The action forces this to 1 when `parallel: false`. |
| `enable-suggestions` | No | `true` | Add "Apply suggestion" buttons to inline review comments (GitHub and GitLab). Bitbucket ignores this input. Set to `false` to disable. |
| `ignore-merge-commits` | No | `true` | Remove merge commits that pulled in upstream base-branch changes before the action computes the diff. The review covers only the commits of the PR author. If cherry-pick conflicts occur, the action uses the unfiltered diff. Set to `false` to review all commits, including upstream merges. |
| `sarif-paths` | No | `''` | Comma-separated SARIF 2.1.0 file paths (relative to workspace root) to merge into findings. |
| `exclude-patterns` | No | `''` | Comma-separated git pathspec glob patterns to exclude from the diff (for example `docs/*,*.generated.go`). The action adds the `":!"` prefix automatically. The action splits entries on commas, trims surrounding whitespace, and drops empty entries. Thus `docs/*, *.generated.go` has the same effect as `docs/*,*.generated.go`. See `exclude-patterns-mode`. |
| `exclude-patterns-mode` | No | `append` | Controls how `exclude-patterns` interacts with the built-in excludes. `append` (default): the action adds user patterns to the built-in lockfile and vendor excludes. `replace`: the action uses only user patterns and drops the built-in excludes. `replace` with an empty list falls back to the built-ins with a warning. An invalid value causes an error. |
| `analyzers` | No | `''` | Allowlist: comma-separated names of static analyzers to run. When set, only the listed analyzers run and the action ignores `exclude-analyzers`. Empty (default): all eligible analyzers run. An unknown name causes an error. See [Static analyzers](static-analyzers) for valid names. |
| `exclude-analyzers` | No | `''` | Denylist: comma-separated names of static analyzers to skip. The action ignores this input when `analyzers` is set. Empty (default): no analyzers skipped. An unknown name causes an error. |
| `agents` | No | `''` | Allowlist: comma-separated names of review agents to run. When set, only the listed agents run and the action ignores `exclude-agents`. Existing gates (mode, conditional triggers) still apply. Empty (default): all eligible agents run. An unknown name causes an error. See [Agents](agents) for valid names. |
| `exclude-agents` | No | `''` | Denylist: comma-separated names of review agents to skip. The action ignores this input when `agents` is set. If you exclude `pr-summarizer`, the action posts no PR summary comment. Empty (default): no agents skipped. An unknown name causes an error. |
| `analyzer-diff-scope` | No | `cap` | How out-of-diff native-analyzer findings are handled. `cap` (default): lower them to Low severity and collapse them into a `<details>` section, so they do not trigger `REQUEST_CHANGES`. `drop`: remove them entirely. `off`: pass them through unchanged. This input never affects LLM-agent findings. |
| `approval-ceiling` | No | `approve` | Caps the strongest review event this bot can post: `approve` (default, unchanged behavior), `request-changes`, or `comment`. See [Approval ceiling](#approval-ceiling). |

## Repository variables

These optional variables can be set in **Settings → Secrets and variables → Actions → Variables** of the consuming repository. The example workflows read them with a fallback default. Thus you do not need to edit the workflow file for routine configuration changes.

| Variable | Default | Corresponding input | Description |
|----------|---------|---------------------|-------------|
| `AI_REVIEW_API_KEY` | n/a | `api-key` | **(Secret)** API key for your LLM provider |
| `AI_REVIEW_PROVIDER` | `anthropic` | `provider` | LLM provider name |
| `AI_REVIEW_MODE_DEFAULT` | `''` | `review-mode` (main review job) / `review-mode-default` (rescan) | The mode for the automatic review job and for `/ai-pr-review rescan`, when set. Empty (default): defer to `.ai-pr-review/policy.yml` route matching if present, else `quick`. See [Policies](policy). |
| `AI_REVIEW_BASE_URL` | `''` | `base-url` | Custom endpoint URL (for `openai-compatible` or `bedrock-proxy`) |
| `AI_REVIEW_MODEL_STANDARD` | Per-provider default | `model-standard` | Override the standard agent model ID |
| `AI_REVIEW_MODEL_PREMIUM` | Per-provider default | `model-premium` | Override the premium agent model ID (full mode only) |
| `AI_REVIEW_MAX_DIFF_LINES` | `5000` | `max-diff-lines` | Skip the review when the diff exceeds this number of lines |
| `AI_REVIEW_MAX_INLINE` | `25` | `max-inline` | Maximum inline comments per run. The action moves any excess to the summary body. |
| `AI_REVIEW_MAX_TOKENS_PER_AGENT` | `32768` | `max-tokens-per-agent` | Output token budget per LLM agent call (clamped to 256–65536) |
| `AI_REVIEW_ENABLE_SUGGESTIONS` | `true` | `enable-suggestions` | Enable "Apply suggestion" buttons on inline comments |
| `AI_REVIEW_PARALLEL` | `true` | `parallel` | Run agents in parallel (tiered fan-out). If you hit provider rate limits, set to `false`. |
| `AI_REVIEW_IGNORE_MERGE_COMMITS` | `true` | `ignore-merge-commits` | Remove upstream base-branch merges from the diff before the review |
| `AI_REVIEW_IMAGE_TAG` | `latest` | `image-tag` | Container image tag to pull (for example `latest`, `1.2.3`). Pin a tag for reproducible runs. |
| `AI_REVIEW_CONTEXT_ENRICHMENT` | `true` (container), `false` (direct action) | `context-enrichment` | Add tree-sitter symbol-context blocks to agent prompts. The container image includes `tree-sitter-language-pack` and `ripgrep`, so enrichment is on by default there. Direct-action consumers without those tools get a silent no-op. |
| `AI_REVIEW_SARIF_PATHS` | `''` | `sarif-paths` | Comma-separated SARIF 2.1.0 file paths to merge into findings. |
| `AI_REVIEW_EXCLUDE_PATTERNS` | `''` | `exclude-patterns` | Comma-separated git pathspec glob patterns to exclude from the diff (for example `docs/*`). |
| `AI_REVIEW_EXCLUDE_PATTERNS_MODE` | `append` | `exclude-patterns-mode` | How `exclude-patterns` interacts with the built-in excludes: `append` (default) or `replace`. |
| `AI_REVIEW_FEEDBACK_LOOP` | `false` | `feedback-loop` / `enable-feedback-loop` | Enable the learning loop in both the main review workflow (adds a `<repo-feedback>` block) and the slash-commands workflow (allows the `/ai-pr-review false-positive`, `wont-fix`, `feedback`, `explain`, and `revise` commands). Works on GitHub and Bitbucket. On GitLab the loop does nothing. |
| `AI_REVIEW_ANALYZER_DIFF_SCOPE` | `cap` | `analyzer-diff-scope` | How out-of-diff native-analyzer findings are handled. `cap` (default): lower them to Low and collapse them under `<details>`. `drop`: remove them entirely. `off`: pass them through unchanged. |
| `AI_REVIEW_FAIL_ON_FINDINGS` | `false` (action default), `true` in the shipped example workflow | `fail-on-findings` | Exit with code 2 when the review outcome is `REQUEST_CHANGES` or `COMMENT`. Use this as a CI gate, so required status checks block auto-merge until the bot approves. Both the root action and the container action support it (see [CI gate](#ci-gate-fail-on-findings)). To make the review job non-blocking, set the `AI_REVIEW_FAIL_ON_FINDINGS` repo variable to `false`. The `rescan` and `review-full` commands read a separate variable, `AI_REVIEW_SLASH_FAIL_ON_FINDINGS` (default `false`). |
| `AI_REVIEW_APPROVAL_CEILING` | `approve` | `approval-ceiling` | Caps the strongest review event the bot may post: `approve`, `request-changes`, or `comment`. The example workflows pass it to the review job and to the slash-command jobs. You can set it as an organization-level variable, and every repository that uses the example workflows then inherits it. See [Approval ceiling](#approval-ceiling). |
| `AI_REVIEW_CONTEXT_MAX_QUERIES` | `200` | `context-max-queries` | Cap on ripgrep symbol-lookup queries shared across all agents in a run. Increase this value if logs show `context enrichment: max_queries=N reached`. Both the root action and the container action support it. |
| `AI_REVIEW_FINDING_BADGES` | `true` | `finding-badges` | Set to `false` to hide the evidence label and score beside each finding. See `AI_FINDING_BADGES`. |
| `AI_REVIEW_SUPPRESS_WALKTHROUGH` | `false` | `suppress-walkthrough` | Set to `true` to leave the Walkthrough table out of the summary comment. See `AI_SUPPRESS_WALKTHROUGH` below. |
| `AI_REVIEW_TOKEN_USAGE_DISPLAY` | `compact` | `token-usage-display` | How token-usage/cost information appears in the posted review comment: `compact` (default, a one-line summary), `full` (the pre-#758 `<details>` table), or `off` (no token-usage content in the comment). The full breakdown always appears in `GITHUB_STEP_SUMMARY` and the CI job log. See [Features: Token usage](features#token-usage). |
| `AI_REVIEW_TOKEN_USAGE_WARN_USD` | `1.00` | `token-usage-warn-usd` | Estimated-cost threshold (USD) above which a high-usage warning line is added to the comment. Set to `0` to disable. |
| `AI_REVIEW_MAX_COST_USD` | `0` | `max-cost-usd` | Maximum estimated cost (USD) for a single review run. If the pre-flight estimate (computed before any agent dispatches) exceeds this value, the run stops before any LLM call. Set to `0` (default) to disable the ceiling. See [Cost ceiling](#cost-ceiling). |
| `AI_REVIEW_FAIL_ON_COST_CEILING` | `false` | `fail-on-cost-ceiling` | When `true`, an exceeded `max-cost-usd` ceiling exits with code 2 (a CI-gate failure) instead of the default exit 0. In both cases the run stops before any LLM call. See [Cost ceiling](#cost-ceiling). |
| `AI_REVIEW_COST_CEILING_UNPRICED` | `warn` | `cost-ceiling-unpriced` | What to do when `max-cost-usd` is set but a model in the run has no pricing entry (#977). `warn` runs the review and adds a visible notice. `block` skips the review. See [Cost ceiling](#cost-ceiling). |

To set a variable via the GitHub CLI:
```bash
gh variable set AI_REVIEW_PROVIDER --body "openai" --repo owner/repo
```

## Policies

The file `.ai-pr-review/policy.yml` (the engine also reads `.github/ai-pr-review/policy.yml` as a fallback) lets a repo route the review depth by changed-file path, base-branch glob, or head-branch glob. The review depth is the set of agents and analyzers that run, and the quick or full mode. This replaces a hand-written GitHub Actions expression in each repo and one global choice for every PR. A named policy extends a built-in `quick` or `full` base (or another named policy). It overrides `agents`, `exclude-agents`, `analyzers`, and `exclude-analyzers` for that policy only. An ordered `routes` list matches on `paths`, `base-branch`, and `head-branch`. If no route matches, the review uses a configurable `default`.

Policies are opt-in and fail-soft. A repo with no `policy.yml` sees no behavior change. If the file is malformed, the engine prints one warning and uses its hard-coded defaults, and the review continues. The engine loads the file from the PR *base* ref and never from the PR head. Thus a PR cannot weaken its own review by editing its own policy file.

The `require` field of a route can turn policy routing into a merge gate (GitHub only). If an automatic push satisfies only a cheaper tier, the engine posts an `action_required` `ai-pr-review/policy-gate` check. When a run at the required tier finishes for that commit, the check changes to `success`. To make this a real gate, add branch protection that requires the check.

The precedence, from highest to lowest, is as follows:

1. The `/ai-pr-review review-full` slash command.
2. An explicit action input or repo variable (the `ai-review-full` label, `review-mode`, or `agents` and `analyzers` set to a non-empty value).
3. A `policy.yml` route match.
4. The engine default (`quick` mode, no restriction).

See [Policies](policy) for the full schema, worked examples, and the merge-gate walkthrough.

## Supported VCS providers

Select the VCS provider via the `VCS_PROVIDER` env var (default: `github`). This setting selects the VCS provider module and the way the action posts findings. No provider implements `review-target: standalone` (see [issue #623](https://github.com/tag1consulting/ai-pr-review/issues/623)), so this table omits it.

| Provider | `VCS_PROVIDER` | Summary | Inline | Suggestions | Approval |
|----------|---------------|---------|--------|-------------|----------|
| GitHub | `github` (default) | Yes | Yes | Yes | Yes |
| Bitbucket Cloud | `bitbucket` | Yes | Yes (through Code Insights annotations, not inline comments, see [Bitbucket setup](bitbucket-setup.md)) | No | Yes (real approve and request-changes calls, on by default through `AI_BITBUCKET_REVIEW_STATE`) |
| GitLab | `gitlab` | Yes | Yes | Yes | Yes |

See [Bitbucket setup](bitbucket-setup), [GitLab setup](gitlab-setup), or the [Getting Started](getting-started) page for provider-specific configuration.

## Supported LLM providers

| Provider | provider value | Required secret | Default models (standard / premium) |
|----------|-----------------|-----------------|--------------------------------------|
| Anthropic | `anthropic` | `ANTHROPIC_API_KEY` | `claude-sonnet-5-5` / `claude-opus-5-5` |
| OpenAI | `openai` | `OPENAI_API_KEY` | `gpt-6-luna` / `gpt-6.1-sol` |
| OpenAI-compatible | `openai-compatible` | `OPENAI_API_KEY` + `base-url` | Set via `model-standard` / `model-premium` inputs |
| Google | `google` | `GOOGLE_API_KEY` | `gemini-3.5-flash-lite` / `gemini-3.8-flash` |
| Bedrock proxy | `bedrock-proxy` | `BEDROCK_API_KEY` + `base-url` | `us.anthropic.claude-sonnet-5` / `global.anthropic.claude-opus-4-7` |

## Environment variables

The scripts read these variables, but the action does not expose them as inputs. Set them in your workflow `env:` block or pass them with `docker run -e`.

| Variable | Default | Description |
|----------|---------|-------------|
| `AI_TEMPERATURE` | `0.3` | Sampling temperature for LLM calls (clamped to [0, 2]). The action does not send this value to models that reject or discourage a non-default value (currently Claude Opus 4.7, 4.8, 5 and 5.5, Claude Sonnet 5 and 5.5, Claude Haiku 5.5, OpenAI `o1`/`o3`/`o4`, `gpt-5`, `gpt-5.5`, `gpt-5.6-*`, `gpt-6*`, and Gemini 3). For those models the provider default applies. `gpt-5.4` and `gpt-5.4-mini` still receive it. The exact list is `resolve_temperature()` in `ai_pr_review/llm/_config.py`. |
| `LLM_PROMPT_CACHING` | `auto` | Enable Anthropic and Bedrock prompt caching. `auto` enables caching for anthropic and bedrock-proxy. `true` forces caching on. `false` forces caching off. |
| `AI_CACHE_PRIMING` | `false` | Deprecated and ignored (#824 audit of #807). The cache-priming serialization mechanism (`cache_priming_effective()`) had no production callers, so the project deleted it as dead code. The engine accepts the variable as a no-op with a deprecation warning. Starting in v3.0.0 the engine will reject it. |
| `VCS_PROVIDER` | `github` | Selects the VCS provider module. Valid values: `github`, `bitbucket`, `gitlab`. |
| `PHPSTAN_LEVEL` | `3` | PHPStan analysis depth level (0-9). This value always applies. The analyzer runs against a trusted, empty `--configuration` file. It never finds a `phpstan.neon` or `phpstan.neon.dist` file in the analyzed repo, because that content can be untrusted fork-PR code checked out under `pull_request_target` (#739). |

### Advanced tuning (env-var only)

These settings were action inputs in earlier versions. They still work as environment variables. Set them in your workflow `env:` block. Most consumers do not need to change them.

| Variable | Default | Description |
|----------|---------|-------------|
| `FORCE_FULL_DIFF` | `false` | Bypass the SHA watermark and review the full PR diff. Use the `ai-review-rescan` PR label instead, because it sets this variable automatically. |
| `STANDALONE_DEPTH` | n/a | Deprecated and ignored (#824). The variable was reserved for a standalone review mode that the docs described but the project never implemented (see [issue #623](https://github.com/tag1consulting/ai-pr-review/issues/623)). #824 removed the last trace, an unread `ReviewConfig` field. The engine accepts the variable as a no-op with a deprecation warning. Starting in v3.0.0 the engine will reject it. |
| `LLM_RETRY_COUNT` | `3` | Retry attempts for transient LLM API failures (429, 5xx, timeouts), clamped to 0-10. Set to `0` to disable. |
| `AI_CONFIDENCE_THRESHOLD` | `75` | Minimum confidence score (0–100) for findings. The engine drops findings below this score before it applies suppressions. |
| `AI_DISABLE_GATE_ARCHITECTURE` | `false` | Disables the docs-only heuristic gate. `architecture-reviewer` always runs, whatever the diff content is. |
| `AI_DISABLE_GATE_SECURITY` | `false` | Disables the keyword and path heuristic gate. `security-reviewer` always runs, whatever the diff content is. |
| `AI_DISABLE_GATE_EDGE_CASE` | `false` | Disables the control-flow heuristic gate. `edge-case-hunter` always runs, whatever the diff content is. |

### Per-agent max-tokens overrides (env-var only)

`AI_MAX_TOKENS_PER_AGENT` (see [Action inputs](#action-inputs) and [Repository variables](#repository-variables)) sets one output-token budget for every tier-dispatched agent. `AI_MAX_TOKENS_<AGENT>` overrides that budget for one named agent. It takes precedence over both `AI_MAX_TOKENS_PER_AGENT` and the roster default of the agent. To make the variable name, write the agent name in uppercase and replace `-` with `_` (for example `code-reviewer` becomes `AI_MAX_TOKENS_CODE_REVIEWER`).

If the value is not an integer, the engine prints a warning and uses the roster default or the `AI_MAX_TOKENS_PER_AGENT` value. If the value is outside 256–65536, the engine clamps it to the nearest bound. This matches the clamp behavior of `AI_MAX_TOKENS_PER_AGENT`. Neither case fails the run. The engine validates every set `AI_MAX_TOKENS_<AGENT>` value at config-load time, even if that agent does not dispatch in this run. A malformed override thus always warns once. See [issue #191](https://github.com/tag1consulting/ai-pr-review/issues/191) and its follow-up, [issue #847](https://github.com/tag1consulting/ai-pr-review/issues/847).

The engine dispatches `pr-summarizer` and `issue-linker` separately from the other seven agents. They compose their own prompts and never use the tier-dispatch path that reads `AI_MAX_TOKENS_PER_AGENT`. Thus `AI_MAX_TOKENS_<AGENT>` is the *only* way to change their budget, and `AI_MAX_TOKENS_PER_AGENT` has no effect on either. The engine takes their default from the agent roster (`ai_pr_review/agents/roster.py`) and not from a separate hardcoded value. Before #847, the `pr-summarizer` preflight dispatch hardcoded `4096`. That value had drifted from the roster value of `16384` since #191. #847 fixed this, so the effective default without an override is now `16384`.

| Variable | Effective default | Notes |
|----------|--------------------|-------|
| `AI_MAX_TOKENS_PR_SUMMARIZER` | `16384` | Dispatched separately from `AI_MAX_TOKENS_PER_AGENT`. See above. |
| `AI_MAX_TOKENS_CODE_REVIEWER` | `32768` | Tier-dispatched. Falls back to `AI_MAX_TOKENS_PER_AGENT` when unset. |
| `AI_MAX_TOKENS_SILENT_FAILURE_HUNTER` | `32768` | Tier-dispatched. |
| `AI_MAX_TOKENS_ARCHITECTURE_REVIEWER` | `32768` | Tier-dispatched, full mode only. |
| `AI_MAX_TOKENS_SECURITY_REVIEWER` | `32768` | Tier-dispatched, full mode only. |
| `AI_MAX_TOKENS_BLIND_HUNTER` | `32768` | Tier-dispatched, full mode only. |
| `AI_MAX_TOKENS_EDGE_CASE_HUNTER` | `32768` | Tier-dispatched, full mode only. |
| `AI_MAX_TOKENS_ADVERSARIAL_GENERAL` | `32768` | Tier-dispatched, full mode only. |
| `AI_MAX_TOKENS_ISSUE_LINKER` | `4096` | Dispatched separately from `AI_MAX_TOKENS_PER_AGENT`. See above. |

Not implemented: a bulk JSON-map override form (for example `AI_MAX_TOKENS_OVERRIDES`). Issue #191 suggested it, but the project left it out on purpose. The per-agent variables above cover the same need with less to document and validate. The project can add the JSON form later if real usage needs it.

### Legacy compatibility

| Variable | Default | Description |
|----------|---------|-------------|
| `AI_PR_REVIEW_COMPUTE_OUTPUT` | `''` | **Legacy.** Originally the Python compute phase wrote its payload here and a separate posting step read it back. The Python engine handles posting from start to end, and the action does not use this handoff. Setting this variable still works for tooling that reads the JSON directly. See [Compute Output Schema](https://github.com/tag1consulting/ai-pr-review/blob/main/docs/compute-output-schema.md) (maintainer-only reference, not on the Pages site). |

### Opt-in capabilities

These variables enable optional capabilities that are off by default.

#### Context enrichment

| Variable | Default | Description |
|----------|---------|-------------|
| `AI_CONTEXT_ENRICHMENT` | `true` (container), `false` (direct action) | Enable tree-sitter and ripgrep symbol-context injection. The engine extracts symbol references from the diff and adds the relevant definitions to the prompt of each agent in a `<symbol-context>` block. This needs `tree-sitter-language-pack` (included in the container image) and `ripgrep`. If either dependency is absent, the engine does nothing and prints no error. |
| `AI_CONTEXT_MAX_TOKENS` | `8192` | Maximum token budget for the injected `<symbol-context>` block per agent call. |
| `AI_CONTEXT_LOOKUP_LINES` | `8` | Number of source lines to capture per symbol definition (snippet window). |
| `AI_CONTEXT_MAX_QUERIES` | `200` | Maximum number of ripgrep symbol-lookup queries across all agents in a run. The cap is global (Tier 1 and Tier 2 share it through the module-level cache). A multi-agent run thus uses queries faster than a single-agent run. Increase this value if you see `context enrichment: max_queries=N reached; remaining symbols skipped` in the logs. |

#### SARIF ingestion

| Variable | Default | Description |
|----------|---------|-------------|
| `AI_SARIF_PATHS` | `''` | Comma-separated list of SARIF 2.1.0 file paths (relative to workspace root) to ingest as additional findings. The engine merges these findings into the same dedup and suppress pipeline as native analyzer results. Source tag: `sarif:<driver.name>`. |

#### Diff exclude patterns

| Variable | Default | Description |
|----------|---------|-------------|
| `AI_EXCLUDE_PATTERNS` | `''` | Comma-separated list of git pathspec glob patterns to exclude from the diff before the LLM reads it (for example `docs/*,*.generated.go`). The engine adds the `":!"` prefix if it is missing. The engine splits entries on commas, trims surrounding whitespace, and drops empty entries. Thus `docs/*, *.generated.go` has the same effect as `docs/*,*.generated.go`. Use this variable to reduce LLM token costs on large generated or documentation-only files. |
| `AI_EXCLUDE_PATTERNS_MODE` | `append` | Controls how `AI_EXCLUDE_PATTERNS` interacts with the built-in exclude list (lockfiles, `vendor/`, `node_modules/`). `append` (default): the engine adds user patterns after the built-ins. `replace`: the engine uses only user patterns and drops the built-in list. If you set `replace` with an empty `AI_EXCLUDE_PATTERNS`, the engine logs a warning and uses the built-in list. This prevents a silent, unfiltered diff. Any other value causes an error at startup. |

#### Analyzer and agent selection

| Variable | Default | Description |
|----------|---------|-------------|
| `AI_ANALYZERS` | `''` | Allowlist: comma-separated names of static analyzers to run. When set, only the listed analyzers run, and the engine ignores `AI_EXCLUDE_ANALYZERS`. Empty (default): all eligible analyzers run. The engine rejects unknown names at config load. Valid names: `shellcheck`, `trufflehog`, `semgrep`, `ruff`, `golangci-lint`, `hadolint`, `checkov`, `phpcs`, `phpstan`, `eslint`, `kube-linter`, `tflint`, `cve-check`, `dep-exists`, `docs-api-check`, `docs-ref-check`, `docs-drift-check`, `suppression-check`. |
| `AI_EXCLUDE_ANALYZERS` | `''` | Denylist: comma-separated names of static analyzers to skip. The engine ignores this variable when `AI_ANALYZERS` is set. Empty (default): no analyzers skipped. The engine rejects unknown names at config load. |
| `AI_AGENTS` | `''` | Allowlist: comma-separated names of review agents to run. When set, only the listed agents run, and the engine ignores `AI_EXCLUDE_AGENTS`. Existing gates (mode, conditional triggers) still apply. Empty (default): all eligible agents run. The engine rejects unknown names at config load. Valid names: `pr-summarizer`, `code-reviewer`, `silent-failure-hunter`, `architecture-reviewer`, `security-reviewer`, `blind-hunter`, `edge-case-hunter`, `adversarial-general`, `issue-linker`. |
| `AI_EXCLUDE_AGENTS` | `''` | Denylist: comma-separated names of review agents to skip. The engine ignores this variable when `AI_AGENTS` is set. If you exclude `pr-summarizer`, the engine posts no PR summary comment. Empty (default): no agents skipped. The engine rejects unknown names at config load. |

#### Static analyzer options

| Variable | Default | Description |
|----------|---------|-------------|
| `AI_ANALYZER_CONCURRENCY` | `4` | Maximum simultaneous native static-analyzer subprocesses. The engine forces this to 1 when `AI_PARALLEL=false`. |
| `AI_ANALYZER_DIFF_SCOPE` | `cap` | Controls how the engine handles out-of-diff native static-analyzer findings. Native analyzers (phpcs, phpstan, ruff, golangci-lint, and others) lint entire files. A small change can thus produce many diagnostics on unchanged lines. `cap` (default): lower those findings to Low severity and collapse them into a `<details>` section in the summary comment. They stay visible but do not trigger `REQUEST_CHANGES`. `drop`: remove out-of-diff analyzer findings entirely. `off`: pass them through unchanged (full-file linting behavior, the default before v1.2). This setting never affects LLM-agent findings. |

#### Learning loop

| Variable | Default | Description |
|----------|---------|-------------|
| `AI_FEEDBACK_LOOP` | `false` | Enable the learning loop. The engine loads recent feedback from the feedback store and adds a `<repo-feedback>` block to agent prompts. This works on GitHub and Bitbucket. On GitLab the loop does nothing. |
| `AI_FEEDBACK_BRANCH` | `ai-pr-review-bot` | Git branch that holds the feedback JSONL file. The engine creates the branch on the first write. This needs `GH_TOKEN` with `contents:write` on this branch. |
| `AI_FEEDBACK_MAX_TOKENS` | `2048` | Maximum token budget for the injected `<repo-feedback>` block. |
| `AI_FEEDBACK_RETENTION_COUNT` | `500` | Maximum number of feedback entries to keep (a rolling window, the engine drops the oldest first). |
| `AI_FEEDBACK_RETENTION_AGE_DAYS` | `365` | The engine drops entries older than this number of days. Set to `0` to disable age-based pruning. |

#### CI gate (fail on findings)

| Variable | Default | Description |
|----------|---------|-------------|
| `AI_FAIL_ON_FINDINGS` | `false` (engine), the shipped example workflow sets `fail-on-findings: true` | When `true`, exit with code 2 if the review outcome is `REQUEST_CHANGES` or `COMMENT` (incomplete or unknown risk). Exit code 1 still signals a posting or config error. Exit code 0 means the bot approved. Use this to block auto-merge or required CI checks until the bot approves. Pair it with branch protection that requires the `review` status check. To remove the gate, set the `AI_REVIEW_FAIL_ON_FINDINGS` repo variable to `false`. **`AI_APPROVAL_CEILING` never changes this exit code** (see below). The gate also applies to a [cost-ceiling skip](#cost-ceiling) that still has analyzer or SARIF findings. A skip run is not exempt because no LLM agent ran. |

#### Approval ceiling

| Variable | Default | Description |
|----------|---------|-------------|
| `AI_APPROVAL_CEILING` | `approve` | Caps the strongest review event this bot can post. `approve` (default): unchanged behavior. `request-changes`: a clean, Low, or Medium outcome posts `COMMENT` instead of `APPROVE`. A `Critical` or `High` outcome still posts `REQUEST_CHANGES`. `comment`: every outcome posts `COMMENT`. The bot then never sets a formal review state (approve or block), and a human makes every merge decision. A value other than `approve` also disables the `/ai-pr-review dismiss` auto-approve-on-dismiss escalation (issue #590). |

`AI_APPROVAL_CEILING` and `AI_FAIL_ON_FINDINGS` are independent. The exit code always reflects the severity verdict of the classifier and never the posted event. A clean PR exits `0` and a PR with a Critical or High finding exits `2`, whatever the ceiling is. Only what the bot *posts* to the PR changes.

GitLab never posts a real approval state. On GitLab a ceiling other than `approve` changes only the rendered review text. On Bitbucket, the same capped outcome also drives the real approve and request-changes calls that `_set_review_state` makes (see `AI_BITBUCKET_REVIEW_STATE` below). Thus the ceiling caps those calls too.

**Branch protection with a ceiling other than `approve`:** if you set `request-changes` or `comment`, the bot never posts an approving review. Branch protection then cannot use "require approval from ai-pr-review" as a merge gate. Instead, set `fail-on-findings: true` and make the review job a required status check. The ceiling does not affect that exit-code path, as noted above. The policy-gate check run (`docs/policy.md`) is a separate mechanism. The ceiling does not affect it.

#### Judge pass

| Variable | Default | Description |
|----------|---------|-------------|
| `AI_FINDING_BADGES` | `true` | Show an evidence label and the confidence score beside each finding, for example `(judge kept, confidence 80)`. The label is one of `corroborated by analyzer`, `analyzer and agent, not confirmed`, `analyzer finding`, `judge kept`, `single agent, unverified`, or `single agent, not judged`. It is display text only and never changes the approval decision. Set `false` to hide it. |
| `AI_SUPPRESS_WALKTHROUGH` | `false` | When `true`, the engine leaves the Walkthrough section (the per-file table that the pr-summarizer writes) out of the summary comment. The Summary text, Type, and Effort stay. The action input is `suppress-walkthrough`. The example workflows read it from the `AI_REVIEW_SUPPRESS_WALKTHROUGH` repository variable. This setting is useful on Bitbucket, which cannot collapse the table as GitHub and GitLab can. The engine removes the section after the model writes it, so the summarizer cost does not change. On Bitbucket, a summary comment that the bot posted before you set this variable loses its table on the next run. On GitHub and GitLab, such a comment keeps its walkthrough until the summarizer next runs. The accepted true values are `true`, `1`, and `yes`. This needs image 2.19.0 or later. Older images ignore it. |
| `AI_JUDGE_PASS` | `true` | Run a cheap-model judge pass (Phase 2.75) after the engine extracts, merges, suppresses, and scopes findings. The judge makes one compact LLM call (no diff text) and returns `keep` or `downrank` for each finding. `downrank` lowers the confidence of the finding by 15 points and moves it to the review body, away from the inline comments. The engine still reports the finding at its unchanged severity. The finding still counts toward the "Overall Risk" headline and the `REQUEST_CHANGES` decision, as an inline finding does. Corroborated findings (a static analyzer and an LLM agent agree on the same file and line, with equal real categories or one real category and `other`) are exempt from `downrank`. The judge pass is always fail-soft: if the judge fails, the findings stay unchanged. The token usage of the judge call appears as a `judge-pass` row in the token usage table and counts toward the Total. Set to `false` to disable the pass and restore the behavior before v2.1. |

#### Token usage display

| Variable | Default | Description |
|----------|---------|-------------|
| `AI_TOKEN_USAGE_DISPLAY` | `compact` | How token-usage and cost information appears in the posted review comment. `compact` (default): a single summary line with the cost, tokens, and agent count. `full`: the full `<details>`-wrapped per-agent table, as posted before this input existed. `off`: no token-usage content in the comment. This setting does not change where the full breakdown appears. The engine always writes it to `GITHUB_STEP_SUMMARY` (GitHub only) and always echoes it to the CI job log (every provider). See [Features: Token usage](features#token-usage). |
| `AI_TOKEN_USAGE_WARN_USD` | `1.00` | Estimated-cost threshold (USD). Above this value, the engine adds a separate high-usage warning line to the review comment. The engine never combines this line with the table or the compact line. Set to `0` to disable. If a run includes a model with no entry in `config/model-pricing.json`, the warning (if it appears) says the figure is a floor and not a precise number, because the true cost can be higher. |

#### Cost ceiling

| Variable | Default | Description |
|----------|---------|-------------|
| `AI_MAX_COST_USD` | `0` | Maximum estimated cost (USD) for a single review run. Before any agent dispatches, the engine estimates the cost of the run. The estimate uses the diff size, the selected agent roster (including the separately dispatched `pr-summarizer` and `issue-linker` preflight agents, when they will run), and the per-model rates in `config/model-pricing.json`. The engine logs the result as a `COST_ESTIMATE` line, always, even when no ceiling is set. If a ceiling is set and the estimate exceeds it, the run stops before any LLM call. No agent dispatches and no partial spend occurs. The engine posts a skip comment (the same mechanism as the `max-diff-lines` skip) and exits 0 by default. To make this a CI-gate failure, see `AI_FAIL_ON_COST_CEILING`. Native static analyzers and SARIF findings still run before this check, because they make no billed call. The engine posts their findings (suppressed and diff-scoped) inside the same skip comment and does not discard them. See [`AI_FAIL_ON_FINDINGS`](#ci-gate-fail-on-findings) above for how those findings affect the exit code. The estimate is an approximation and not a precise invoice preview. The engine estimates input tokens from character counts (not a real tokenizer). It assumes output tokens at the configured cap of each agent, because the actual generation length is unknown in advance. Real spend is usually lower than the estimate. The engine estimates a model with no pricing entry at $0, so the ceiling cannot bound it. See `AI_COST_CEILING_UNPRICED` for what happens then. Set to `0` (default) to disable the ceiling. |
| `AI_FAIL_ON_FORK_GATE` | `false` | When `true`, a run that the fork gate paused exits with code 2 instead of 0, so a required check does not turn green with no review. See [Fork pull requests](fork-prs). |
| `AI_FORK_MAX_COST_USD` | `2.0` | Cost ceiling (USD) that the engine applies to an approved fork pull request when `AI_MAX_COST_USD` is `0`. A non-zero `AI_MAX_COST_USD` always wins. Set to `0` to switch this default off. A negative or non-finite value falls back to `2.0`. See [Fork pull requests](fork-prs). |
| `AI_FAIL_ON_COST_CEILING` | `false` | When `true`, an exceeded `AI_MAX_COST_USD` ceiling exits with code 2 and not the default 0. This mirrors the opt-in exit code of `AI_FAIL_ON_FINDINGS`, so you can gate a required CI check on it. The run always stops before any LLM call, whatever this setting is. This setting controls only the exit code. It has no effect when `AI_MAX_COST_USD` is unset or `0`. It is independent of `AI_FAIL_ON_FINDINGS`. A clean cost-ceiling skip (no analyzer or SARIF findings remain) exits 0 under this flag alone. `AI_FAIL_ON_FINDINGS` can independently exit 2 on a skip whose findings block approval, even when this flag is unset. Both flags map to the same exit code 2, so no precedence conflict exists. They are two independent reasons for a skip run to fail CI. |
| `AI_COST_CEILING_UNPRICED` | `warn` | What to do when `AI_MAX_COST_USD` is set but a model in the run (including the `pr-summarizer` and `issue-linker` preflight agents) has no entry in `config/model-pricing.json`. The engine estimates such a model at $0, so the ceiling cannot bound it. `warn` (default): the review runs, and a "Cost ceiling not enforced" notice that names the model or models appears in the review comment (whatever `AI_TOKEN_USAGE_DISPLAY` is) and in the job summary. `block`: the engine skips the review before any LLM call, as it does for an exceeded ceiling. The skip comment names the model or models. The exit code is 2 when `AI_FAIL_ON_COST_CEILING` is `true`. A skip that carries blocking analyzer findings can also exit 2 under `AI_FAIL_ON_FINDINGS`. If the run also exceeds the ceiling on its priced agents, the ordinary exceeded-ceiling skip applies in either mode. If the engine cannot load the pricing file, every model looks unpriced. In that case the engine only warns (and asks you to report it), because the problem is in the image and not in your model choice. Any value other than `warn` or `block` is an error, even when `AI_MAX_COST_USD` is unset. Otherwise this variable has no effect when `AI_MAX_COST_USD` is unset or `0`. This needs image 2.18.1 or later. Older images ignore it. |
| `AI_CONTEXT_WINDOW_MARGIN` | `0.9` | Action input `context-window-margin`. Skip the review before any LLM call when an agent's estimated prompt does not fit the model context window. The engine adds the estimated input tokens and the output cap of each agent, and compares the sum with this fraction of the `context_window` of the model in `config/model-pricing.json`. A skip comment explains the cause and names the agent. The check runs whether or not `AI_MAX_COST_USD` is set. Use a number from `0` to `1`. `0` turns the check off. A model with no `context_window` entry is not checked. The bundled file lists `context_window` for Sonnet 5.5, Opus 5.5, and Haiku 5.5. The token estimate is approximate, and the margin is the allowance for that. `MAX_DIFF_LINES` counts lines and not tokens, so it cannot protect against this failure. Set `AI_EXCLUDE_PATTERNS` to exclude generated files and build output. The estimate can still be too low. When the provider rejects a prompt as too long after the call is made, the engine handles it as follows. If every agent is rejected, the review stops and posts a skip comment with its own reason, plus the analyzer and SARIF findings. The engine does not advance the watermark. If only some agents are rejected, the review goes on, and the failed-agent note names the cause. The engine detects this rejection only for the Anthropic error text `prompt is too long`. The engine does not detect the OpenAI, Google and Bedrock errors, which stay a plain agent failure. |
| `AI_FAIL_ON_CONTEXT_WINDOW` | `false` | Action input `fail-on-context-window`. When `true`, a context-window skip exits with code 2 and not the default 0. This works like `AI_FAIL_ON_COST_CEILING`, and the two settings are independent. The run always stops before any LLM call, whatever this setting is. The same setting also covers a rejection at run time: if the provider rejects the prompt of every agent as too long, the engine posts a skip comment and exits with code 2 when this setting is `true`. See `AI_CONTEXT_WINDOW_MARGIN`. With `AI_FAIL_ON_FINDINGS=true`, a rejection at run time also exits with code 2, because no agent reviewed the PR. |

**Reading the cost estimate: a worked example.** The `COST_ESTIMATE` line ends with two fields, `input=` and `output_cap=`. They split the total into the cost of the estimated input tokens and the cost of the output cap. The two fields add up to `total=`. The output cap part is a fixed amount for each agent. It does not depend on the diff. The input part grows with the diff.

Use the rates in `config/model-pricing.json` for the model. Sonnet 5.5 costs $2.00 for each million input tokens and $10.00 for each million output tokens. The default output cap is 32,768 tokens for each agent. Take a ceiling of $1.00 and one agent:

1. The output cap costs 32,768 x $10.00 / 1,000,000 = $0.32768.
2. The input budget is $1.00 - $0.32768 = $0.67232.
3. The input budget buys $0.67232 / ($2.00 / 1,000,000) = 336,160 estimated input tokens.

Thus a ceiling of $1.00 allows about 336,000 estimated input tokens for one agent on Sonnet 5.5. Each agent that runs adds its own output cap and its own input. With two agents that read the same diff, the output caps cost 2 x $0.32768 = $0.65536. The remaining $0.34464 buys about 86,160 estimated input tokens for each agent. To get more room, lower `AI_MAX_TOKENS_PER_AGENT`, remove agents with `AI_EXCLUDE_AGENTS`, or raise the ceiling. The rates in the pricing file can change, so read the file again before you rely on these numbers.

#### Quiet reruns and cross-run finding dedup

| Variable | Default | Description |
|----------|---------|-------------|
| `AI_CANONICAL_REUSE` | `true` | GitHub only. Controls whether reruns reuse the existing "canonical" review of the bot (see [Features: Quiet reruns](features#quiet-reruns-github)) and not always post a fresh one. Set to `false` to restore the earlier behavior of always posting a new review. Use this as an escape hatch if the reuse path has a bug before a fix ships. The provider reads this variable once, at construction, from the raw environment (it is not a `Config` field), because it is specific to GitHub and not provider-neutral. |
| `AI_GITLAB_CROSS_RUN_DEDUP` | `true` | GitLab only. Controls whether reruns fuzzy-match a finding against the still-open prior discussions of the bot (same file, within 3 lines, compatible category). The engine then skips an unchanged finding and does not create a new discussion on every run (see [Features: Quiet reruns](features#quiet-reruns-github)). This is narrower than `AI_CANONICAL_REUSE`. GitLab has no dismiss, false-positive, wont-fix, or fixed verdict system. The engine tracks only "still open" and "more or less severe". GitLab does not suppress a finding that a human dismissed. Set to `false` to restore the earlier behavior of always posting a fresh discussion for every eligible finding. This flag is separate from `AI_CANONICAL_REUSE`, because GitLab has no canonical review to couple it to. You can thus roll back either feature alone. The provider reads this variable once, at construction, from the raw environment, as it does for `AI_CANONICAL_REUSE`. |
| `AI_BITBUCKET_CODE_INSIGHTS` | `true` | Bitbucket only. Controls whether inline-eligible findings appear as [Code Insights](https://support.atlassian.com/bitbucket-cloud/docs/code-insights/) annotations on the PR diff (rebuilt from scratch on every run). It also controls whether the engine honors the dismiss and verdict marker in the summary comment. When this is `false`, every finding appears flat in the comment body with no suppression. See [Bitbucket setup](bitbucket-setup#what-works). Set to `false` to restore the behavior before Code Insights, byte for byte. For example, use this as a kill switch if the plan of your workspace does not support Code Insights and you do not want to rely on the automatic per-run fallback. |
| `AI_BITBUCKET_VERDICTS` | `false` | Bitbucket only. Enables the `dismiss`, `false-positive`, `wont-fix`, `fixed`, and `feedback` comment commands. The engine polls for them at review time. The comment itself does not trigger them, because Bitbucket Pipelines has no `issue_comment` equivalent. See [Bitbucket setup: Dismissing findings](bitbucket-setup#dismissing-findings). This setting is off by default, unlike `AI_BITBUCKET_CODE_INSIGHTS`. It gates a fail-closed authorization check with real security consequences. A bug that fails open would let any commenter silence a finding. Thus it stays opt-in through its own soak period. A purely presentational feature can default to on at once. An empty or unrecognized value counts as `false`. This is an allow-list parse, the opposite of most boolean environment variables in this table, because this variable is security-gated. |
| `AI_BITBUCKET_VERDICT_MIN_ROLE` | `write` | Bitbucket only. The minimum Bitbucket repository permission (`read`, `write`, or `admin`) that a commenter must hold for the engine to apply their verdict command. The engine first checks the owners file `.bitbucket/CODEOWNERS` (a `*` line, always read from the target branch). It then checks every other commenter against the repository-permissions API of Bitbucket, which allows the lookup only for repository admins. It fails closed on any lookup failure. With `admin`, the owners file is not used. See [Bitbucket setup](bitbucket-setup#dismissing-findings). This setting matters only when `AI_BITBUCKET_VERDICTS=true`. An invalid value raises a configuration error at startup. It does not fall back to a weaker role. |
| `AI_BITBUCKET_REVIEW_STATE` | `true` | Bitbucket only. Controls whether the decided review outcome (`APPROVE`, `REQUEST_CHANGES`, or `COMMENT`) also calls the real approve and request-changes endpoints of Bitbucket through `BitbucketProvider._set_review_state`. The reviewer-state badge of the PR then reflects the decision of the review. So does any branch-restriction rule that depends on "changes requested". The decision is then more than heading text in the summary comment. This is fail-soft. The engine logs a failed call and never blocks the comment write that already succeeded. `AI_APPROVAL_CEILING` caps this outcome in the same way as GitHub and GitLab approvals. Set to `false` to restore the behavior before #918, which renders heading text only. **Behavior change on upgrade**: this setting is on by default. A repo that upgrades into this version starts to get a real approve or request-changes state on Bitbucket at once. Review your branch restrictions if they count bot approvals toward a merge requirement. |

#### Language profiles (deprecated per-agent routing knob)

Every `context_enrichment_eligible` agent receives the whole text of the profile of every detected language (#814). No per-agent routed subset and no per-agent token budget exist now.

| Variable | Default | Description |
|----------|---------|-------------|
| `AI_PROFILE_MAX_TOKENS` | `4096` | Deprecated and ignored (#814). The project removed per-agent language-profile routing. The engine accepts the variable as a no-op and prints a deprecation warning in the job log. Starting in v3.0.0 the engine will reject it. |

> **Incremental reviews and gates:** The gates evaluate the *incremental* diff (SHA watermark to HEAD) and not the full PR diff. Consider a PR where an initial commit adds security-relevant code and a later commit only updates docs. The follow-up run skips `security-reviewer`. On security-sensitive PRs, set `AI_DISABLE_GATE_SECURITY=true` (or apply the `ai-review-rescan` PR label with `FORCE_FULL_DIFF`). Then all Tier-2 agents run on every update.

### Telemetry hooks

These variables configure the telemetry system. Telemetry is fail-soft. The engine logs all I/O errors as warnings, and the review continues.

| Variable | Default | Description |
|----------|---------|-------------|
| `AI_TELEMETRY_ENABLED` | `false` | Set to `true` to emit a structured JSON event per review run. |
| `AI_TELEMETRY_SINK` | n/a | Where to send telemetry. Accepts `file:///absolute/path/events.jsonl` (the engine appends one line for each event) or an `http(s)://` endpoint (the engine sends a POST). |

Each event (schema version `"4"`) includes:

- Identity and run context: `correlation_id`, `timestamp`, `repository`, `pr_number`, `telemetry_schema_version`.
- Outcome: `outcome`, `findings_count`, `findings_by_severity`, `failed_agents`. The `outcome` value is `APPROVE`, `REQUEST_CHANGES`, or `COMMENT` for a completed review. Other values are `dry_run`, `skipped`, `skipped_cost_ceiling_failed`, and `skipped_fail_on_findings_failed`.
- Per-agent metrics: `token_usage_by_agent`, `agent_latency_ms`, `failed_agent_latency_ms`. Since schema 3, each `token_usage_by_agent` entry also has `thinking_tokens` and `stop_reason`.
- Agent yield (added in schema 4, #1088): `agent_yield` has one entry for each finding agent that returned an answer. A skipped run has no entries, and neither does a failed agent. `status` says what the output held: `ok` (a `json-findings` array, which can be empty), `no_block`, `truncated_no_block`, `invalid_json`, `truncated_invalid`, `truncated_repaired` (the output stopped at the token cap and the complete findings before the cut were kept), or `not_array`. `emitted` is the number of items in the array, or `null` when no array could be read. `parsed` counts the findings that passed validation. When `emitted` is more than `parsed`, items were dropped while reading (not an object, failed validation, or a self-refuting finding). `below_threshold` counts the parsed findings under `AI_CONFIDENCE_THRESHOLD`, which the merge drops. `output_tokens` is the output token count. Analyzer and SARIF findings are not counted. The same numbers go to the job log as one `AGENT_YIELD` line per agent.
- Configuration shape (added in schema 2): `provider`, `model_standard`, `model_premium`, `review_mode`, `is_incremental`.
- Other: `sarif_elapsed_s`, `learning_store_entries_loaded`.

All additions in schemas 2, 3, and 4 are forward-compatible. Consumers that ignore unknown keys continue to work. Consumers that switch on `telemetry_schema_version` must add `"4"` (and `"3"` and `"2"` if they read older events) to their accepted set. Consumers that switch on `outcome` must handle the values listed above.

### Structured logging

These variables configure the logging system. Set them in your workflow `env:` block.

| Variable | Default | Description |
|----------|---------|-------------|
| `AI_LOG_FORMAT` | `human` | Log output format. `human`: human-readable timestamped lines. `json`: machine-readable JSON objects with the fields `timestamp`, `level`, `logger`, `correlation_id`, and `message`. Use `json` for Datadog, CloudWatch, and similar log aggregators. |
| `AI_LOG_LEVEL` | `WARNING` | Minimum log level to emit. One of `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` (case-insensitive). |
| `AI_PR_REVIEW_CORRELATION_ID` | *(auto-generated)* | 8-character hex correlation ID that the engine adds to every log record during a review run. The engine generates it at startup. Set it explicitly to correlate logs across multiple jobs. |

Secret masking is always active. The engine redacts API keys, tokens, and other credentials from `ReviewConfig` in log output, whatever the log format or level is.

### GitHub-specific variables

| Variable | Default | Description |
|----------|---------|-------------|
| `GITHUB_BOT_USERNAME` | `github-actions[bot]` | The login that owns the prior reviews and comments of this bot. The engine uses it for the review-selection filter and for thread-ownership checks. Set this variable if your reviews post under a different identity than the default GitHub Actions bot. Examples are a custom GitHub App and (as in the e2e test harness of this project) an account that uses a personal access token. The engine ignores summary and skip comments from any other login, so a custom bot identity must set this variable. It matches `GITLAB_BOT_USERNAME` below. |

### Bitbucket-specific variables

| Variable | Default | Description |
|----------|---------|-------------|
| `BITBUCKET_EMAIL` | n/a | Atlassian account email of the bot user (Basic-auth username) |
| `BITBUCKET_API_TOKEN` | n/a | Atlassian API token (Basic-auth password) |
| `BITBUCKET_WORKSPACE` | n/a | Optional explicit override for the workspace slug. The default is the result of splitting `GITHUB_REPOSITORY`. |
| `BITBUCKET_REPO_SLUG` | n/a | Optional explicit override for repo slug |

### GitLab-specific variables

| Variable | Default | Description |
|----------|---------|-------------|
| `GITLAB_TOKEN` | n/a | Personal or project access token with `api` scope. If unset, the engine uses `CI_JOB_TOKEN`. |
| `GITLAB_API_URL` | `https://gitlab.com/api/v4` | API base URL for self-hosted instances |
| `GITLAB_PROJECT_ID` | n/a | Numeric project ID. If unset, the engine uses `CI_PROJECT_ID`, then the URL-encoded value of `CI_PROJECT_PATH` or `GITHUB_REPOSITORY`. |
| `GITLAB_DIFF_BASE_SHA` | n/a | Base SHA for inline discussion positions. If unset, the engine uses `CI_MERGE_REQUEST_DIFF_BASE_SHA`. The review stops with a configuration error if neither is set. |
| `GITLAB_BOT_USERNAME` | n/a | Username of the bot that posts reviews (used to resolve stale threads). The default is the authenticated user. |
