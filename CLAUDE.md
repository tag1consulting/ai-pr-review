# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A GitHub Actions composite action that runs multiple LLM agents against a PR diff and posts a structured review (summary comment + inline findings) back to the PR. It is consumed by downstream repos either as a direct action reference (`uses: tag1consulting/ai-pr-review@main`) or as a git submodule.

## Key scripts and their roles

| Script | Role |
|--------|------|
| `review.sh` | Main orchestrator: diff computation, manifest building, language detection, agent dispatch, findings merge/dedup/suppress, invokes the provider-specific post-review script |
| `llm-call.sh` | Stateless curl-based LLM client; dispatches to the correct provider based on `AI_PROVIDER`; writes response to stdout, emits `TOKENS: input=N output=N cache_creation=N cache_read=N model=M` line to stderr. Anthropic and Bedrock paths enable prompt caching via `cache_control: ephemeral` markers on the system prompt and user message (gated by `LLM_PROMPT_CACHING`; default `auto`). |
| `post-review.sh` | GitHub API layer: resolves/dismisses stale review threads, posts summary comment, posts findings as a PR review with inline comments, advances SHA watermark |
| `post-review-bitbucket.sh` | Bitbucket Cloud API layer: upserts one summary comment containing findings, advances SHA watermark (no inline comments in v0.2.0; see [Sibling-script pattern](#sibling-script-pattern)) |
| `post-review-gitlab.sh` | GitLab API layer: upserts summary note, posts inline MR discussions with suggestion fences, resolves stale bot discussions, advances SHA watermark |
| `analyzers/run-shellcheck.sh` | Wraps shellcheck for changed `.sh`/`.bash` files; outputs findings in the same JSON schema as LLM agents |
| `analyzers/run-cve-check.sh` | Queries [OSV.dev](https://osv.dev/) for known vulnerabilities in changed dependency manifests (`go.mod`, `package.json`, `requirements.txt`, `composer.json`); outputs findings in the same JSON schema as LLM agents |
| `analyzers/run-semgrep.sh` | Wraps semgrep for any changed file; `ERROR`→High, `WARNING`→Medium, else→Low; confidence 90; source `"semgrep"`. When running inside the container image, uses rulesets baked into `/opt/ai-pr-review/semgrep-rules/` (no network fetch). Outside the container, falls back to `--config=auto`. Consumers can override the ruleset directory via `SEMGREP_RULES_DIR`. |
| `analyzers/run-trufflehog.sh` | Wraps trufflehog secret scanning for any changed file; verified→Critical/95, unverified→High/85; source `"trufflehog"` |
| `analyzers/run-ruff.sh` | Wraps ruff for changed `.py` files; `F`/`E` prefix→High, `W`/`C`→Medium, else→Low; confidence 90; source `"ruff"` |
| `analyzers/run-golangci-lint.sh` | Wraps golangci-lint for changed `.go` files; `errcheck`/`govet`/`staticcheck`→High, others→Medium; confidence 90; source `"golangci-lint"` |
| `analyzers/run-hadolint.sh` | Wraps hadolint for changed Dockerfiles (`Dockerfile*`, `*.dockerfile`); `error`→High, `warning`→Medium, else→Low; confidence 90; source `"hadolint"` |
| `analyzers/run-checkov.sh` | Wraps checkov for changed IaC files. Unconditionally accepts `.tf`, `.tfvars`, `Dockerfile*`. For `.yaml`/`.yml`/`.json`, applies a content sniff to skip non-IaC files (package-lock.json, GitHub Actions workflows, docker-compose, etc.) — accepts k8s/Helm manifests (`apiVersion:`+`kind:`), CloudFormation (`AWSTemplateFormatVersion`), and Azure ARM (`$schema: …schema.management.azure.com…`). Intentionally skipped: GitHub Actions workflows (use actionlint), Serverless Framework `serverless.yml`, Helm `Chart.yaml`. `CKV2_*` (v2 rules) and `CKV_SECRET_*` (secret-detection rules)→High; all other checks→Medium; confidence 80; source `"checkov"`. |
| `analyzers/run-phpcs.sh` | Wraps phpcs for changed PHP files (`.php`, `.module`, `.inc`, `.theme`, `.install`, `.profile`); `ERROR`→High, `WARNING`→Medium; confidence 90; source `"phpcs"`. Uses Drupal,DrupalPractice standard when drupal/coder is installed, else PSR12. |
| `analyzers/run-eslint.sh` | Wraps ESLint for changed JS/TS files (`.js`, `.jsx`, `.ts`, `.tsx`, `.mjs`, `.cjs`); requires consumer's `eslint.config.*` or `.eslintrc.*` — no-op if absent; severity 2→High, 1→Medium; confidence 90; source `"eslint"` |
| `analyzers/run-phpstan.sh` | Wraps phpstan for changed PHP files (`.php`, `.module`, `.inc`, `.theme`, `.install`, `.profile`); all findings→High; confidence 85; source `"phpstan"`. Runs at `PHPSTAN_LEVEL` (default 3) unless consumer has `phpstan.neon`/`phpstan.neon.dist`. |
| `analyzers/run-kube-linter.sh` | Wraps kube-linter for changed YAML/JSON files containing `apiVersion:` + `kind:` headers (K8s manifests); all findings→Medium; confidence 85; source `"kube-linter"` |
| `analyzers/run-tflint.sh` | Wraps tflint for changed `.tf`/`.tfvars` files; runs per Terraform module directory; `error`→High, `warning`→Medium, `notice`→Low; confidence 90; source `"tflint"` |
| `action.yml` | GitHub Actions composite action definition; maps inputs to env vars and calls `review.sh` |

## Multi-provider support (GitHub / Bitbucket Cloud / GitLab)

Since v0.2.0 the same container image drives PR/MR reviews on GitHub,
Bitbucket Cloud, and GitLab. The provider is selected via the `VCS_PROVIDER`
env var:

| `VCS_PROVIDER` | Provider | Post-review script |
|---|---|---|
| `github` (default) | GitHub | `post-review.sh` |
| `bitbucket` | Bitbucket Cloud | `post-review-bitbucket.sh` |
| `gitlab` | GitLab | `post-review-gitlab.sh` |

`review.sh` resolves `POST_REVIEW_SCRIPT` once at startup based on
`VCS_PROVIDER` and uses it at every post-review call site. Invalid provider
values fail fast with a clear error. `REVIEW_TARGET=standalone` is rejected
for Bitbucket (no Issues product); GitLab standalone mode posts findings as
a GitLab Issue.

### Sibling-script pattern

`post-review-bitbucket.sh` and `post-review-gitlab.sh` are **sibling
scripts**, not refactors of `post-review.sh`. They duplicate pure helpers
(`severity_icon`, `format_source_tag`, `truncate_body`, `mktemp_tracked`,
`cleanup`, and for GitLab additionally `classify_risk`,
`format_body_finding`, `build_agent_prompt`, `parse_valid_lines`,
`parse_diff_new_lines`)
because sourcing across providers would couple unrelated concerns.

**Drift mitigation:** every duplicated helper carries a
`# keep in sync with post-review.sh:<line>` comment above it.
`tests/post_review_bitbucket_functions.bats` and
`tests/post_review_gitlab_functions.bats` contain 3-way parity tests that
evaluate all three implementations against shared fixtures — the test
fails if any copy drifts.

**Per-provider constants:** `MAX_BODY_SIZE` differs per provider
(64000 for GitHub, 32000 for Bitbucket, 250000 for GitLab). These are
defined inside each script, not shared.

**Refactor trigger:** condition (a) from the original design (third VCS
provider scoped) is now met with GitLab. The sibling pattern was retained
for this release to reduce risk — the `vcs/<provider>.sh` abstraction
refactor is planned as a separate follow-up when either script exceeds
~500 LOC of provider-specific logic.

### Bitbucket-specific feature gaps (v0.2.0)

Not yet supported on the Bitbucket path:
- Inline review comments (all findings render inside the summary comment)
- `REVIEW_TARGET=standalone` (Bitbucket Cloud has no Issues product)
- Slash-command triggers (Bitbucket Pipelines has no `issue_comment` event)
- APPROVE / REQUEST_CHANGES review events (different endpoints, optional)
- Large-diff skip comment (exits cleanly with a warning, no comment posted)

See [docs/bitbucket-setup.md](docs/bitbucket-setup.md) for the full list.

### GitLab-specific feature gaps

Not yet supported on the GitLab path:
- Slash-command triggers (GitLab CI has no `issue_comment` event equivalent)
- Incremental `start_sha` for inline discussions (on subsequent runs after
  the SHA watermark advances, `start_sha` is still set to the original MR
  diff base; GitLab anchors discussions to the full MR diff rather than the
  incremental diff, so suggestions may show stale context)

Note: APPROVE / UNAPPROVE events are supported. When no Critical/High
findings are found, the bot approves the MR via `POST .../approve`. When
Critical/High findings appear, any prior bot approval is removed via
`POST .../unapprove`. Approval failures (permissions, project approval
rules, bot-is-author) are non-fatal warnings.

See [docs/gitlab-setup.md](docs/gitlab-setup.md) for the full list.

### Multi-arch container image

The `Dockerfile` builds for linux/amd64 and linux/arm64. Each binary
download uses a `case "${TARGETARCH}"` block with per-arch SHA256
checksums (shellcheck, gh, trufflehog, golangci-lint, hadolint,
kube-linter, tflint). pip-installed tools (ruff, semgrep, checkov) and
composer-installed tools (phpcs, phpstan) are arch-neutral. Adding a new
architecture requires extending each `case` block and supplying the
corresponding `_SHA256_<ARCH>` ARG.

### Dockerfile COPY glob

`Dockerfile` uses `COPY post-review*.sh` (a glob) so future provider
scripts are picked up without per-release Dockerfile churn.

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

The `source` field is optional in agent output — `extract_findings()` stamps the agent name automatically if the field is absent. Static analyzers (`analyzers/run-shellcheck.sh`, `analyzers/run-cve-check.sh`) hard-code their source values (`"shellcheck"`, `"osv"`) in their jq projection.

`suggested_code` and `start_line` are optional fields emitted when the `enable-suggestions` action input is `true` (the default). See [Code suggestions](#code-suggestions) below.

`review.sh` uses `extract_findings()` to parse this block and validate shape. Findings below confidence 75 are filtered out. Duplicates are deduped using proximity-based matching: findings in the same file within 3 lines of each other are merged into a single cluster, keeping the highest-severity finding. The dedup step carries a `sources` array on the surviving finding, unioning all sources from the cluster. When multiple sources are present, `post-review.sh` renders `[first-source] *(also flagged by: other)*` attribution (sources are sorted alphabetically; the first is not necessarily the "winning" agent).

## Findings pipeline (review.sh phases)

1. **Phase 0** — Compute diff (incremental if SHA watermark found, else full PR diff). Exclude lockfiles, vendor dirs, node_modules.
2. **Phase 1** — Build shared message files (full context, code context, blind/no-context). Call agents via `call_agent()` (sequential, default) or `call_agent_bg()` (parallel, opt-in). See [Parallel agent execution](#parallel-agent-execution) below.
3. **Phase 2** — Extract `json-findings` from each agent output. Merge with shellcheck findings. Filter by confidence ≥ 75. Apply `config/suppressions.json`. Deduplicate using proximity-based matching (findings within 3 lines in the same file are merged).
4. **Phase 3** — Format findings as markdown. Call the provider-specific post-review script to post everything. Findings whose line is in the diff go inline (up to `AI_MAX_INLINE`); the rest go into the review body via `format_body_finding()`, which wraps remediation in a collapsible `<details>` accordion so the review stays scannable while preserving actionable detail.

## Incremental review / SHA watermark

The SHA of the last-reviewed commit is stored in an HTML comment embedded in the PR/MR summary comment (`<!-- ai-pr-review-summary sha=<sha> -->`). The provider-specific post-review script's `--get-last-sha` mode extracts it. Subsequent pushes diff from that SHA to HEAD. The watermark is advanced at the end of each run when the summary comment is posted with the new HEAD SHA.

Each post-review script keeps at most one summary comment on the PR/MR by calling `_cleanup_duplicate_summary_comments()` after each upsert. Duplicates can accumulate when two runs fire concurrently. Cleanup is non-fatal: DELETE failures emit a WARNING, but the review never blocks on cleanup.

To force a full-PR diff for a single run, add the `ai-review-rescan` label to the PR. This sets the `force-full-diff` action input to `true`, which causes `review.sh` to skip the `--get-last-sha` call (leaving `LAST_REVIEWED_SHA=""`) and fall through to the full `origin/BASE_REF...HEAD_SHA` diff. The watermark still advances normally at the end of the run.

## Context message variants

Three message files are built and passed selectively to agents:

- **`FULL_CONTEXT_MSG`** — manifest + commit log + CLAUDE.md excerpt (first 2000 chars) + language profiles + diff
- **`CODE_CONTEXT_MSG`** — manifest + language profiles + diff (no commit log or project context)
- **`BLIND_MSG`** — raw diff only (intentional zero context for `blind-hunter`)

## Parallel agent execution

Phase 1 runs agents in a tiered fan-out mode by default, reducing wall-clock time from ~5–7 minutes to ~2 minutes in `full` mode.

Disable via `parallel: false` action input or `AI_PARALLEL=false` env var (default: `true`). Set to `false` if your LLM provider's rate limits cannot sustain 3–5 concurrent requests.

> **Breaking change (direct-script users):** Prior to issue #73, `review.sh` defaulted to `AI_PARALLEL=false` when the variable was unset, while `action.yml` defaulted to `true`. The default is now `true` in both invocation paths. If you invoke `review.sh` directly and rely on sequential execution, set `AI_PARALLEL=false` explicitly.

### Tier groupings

| Tier | Agents | When |
|------|--------|------|
| Tier 1 | `pr-summarizer` (first run only), `code-reviewer`, `silent-failure-hunter` (conditional, standard model in quick / premium in full) | Always |
| Tier 1 (static analyzers, concurrent with Tier 1) | `analyzers/run-shellcheck.sh`, `analyzers/run-cve-check.sh`, `analyzers/run-semgrep.sh`, `analyzers/run-trufflehog.sh`, `analyzers/run-ruff.sh`, `analyzers/run-golangci-lint.sh`, `analyzers/run-hadolint.sh`, `analyzers/run-checkov.sh`, `analyzers/run-phpcs.sh`, `analyzers/run-eslint.sh`, `analyzers/run-phpstan.sh`, `analyzers/run-kube-linter.sh`, `analyzers/run-tflint.sh` | Always (graceful no-op if binary absent) |
| Tier 2 | `architecture-reviewer`, `security-reviewer`, `blind-hunter`, `edge-case-hunter`, `adversarial-general` | `review-mode: full` only |

Tier 1 and Tier 2 are separated by a `wait` barrier so Tier 2 never starts until all Tier 1 agents complete.

**Auto-full for releases:** Mode selection is a workflow-level concern — `review.sh` only sees `AI_REVIEW_MODE=quick|full`. The consumer's workflow expression can auto-select `full` based on branch name, PR title, or labels. See the README's "Auto-detecting release PRs" section for patterns. The internal `.github/workflows/ai-review.yml` auto-promotes `release/*` branches to full mode.

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
- Also add the sequential `call_agent` call in the `else` branch (unchanged from the existing pattern).

## Adding a new agent

1. Add a prompt file to `prompts/<agent-name>.md`. The prompt must instruct the model to output a `json-findings` block.
2. In `review.sh`, call `call_agent "<name>" "$AI_MODEL_STANDARD|PREMIUM" "${SCRIPT_DIR}/prompts/<agent-name>.md" "<msg_var>" "<output_var>" [max_tokens]` and push `<output_var>` onto `AGENT_OUTPUTS`. The optional 6th parameter `max_tokens` defaults to 16384; all current agents pass `"$AI_MAX_TOKENS_PER_AGENT"` explicitly (default 8192, configurable via the `max-tokens-per-agent` action input).
3. If the agent should only run conditionally (like `silent-failure-hunter`), gate it with a grep check on `$DIFF_FILE`.
4. Also add the agent to the parallel tier block (see "Parallel agent execution" above).

## Adding a language profile

Create `language-profiles/<language>.md` (filename must match the lowercase language key returned by `detect_language()` in `review.sh`). The file content is injected verbatim into `FULL_CONTEXT_MSG` and `CODE_CONTEXT_MSG` when that language is detected.

Supported languages and their profile files:

| Extension(s) | Language key | Profile file |
|---|---|---|
| `go` | `Go` | `language-profiles/go.md` |
| `py` | `Python` | `language-profiles/python.md` |
| `js`, `jsx` | `JavaScript` | *(no profile loaded — `detect_language()` returns `JavaScript` but no `language-profiles/javascript.md` exists; the TypeScript profile is only loaded for `.ts`/`.tsx`)* |
| `ts`, `tsx` | `TypeScript` | `language-profiles/typescript.md` |
| `php`, `module`, `theme`, `inc` | `PHP` | `language-profiles/php.md` |
| `sh`, `bash` | `Shell` | `language-profiles/shell.md` |
| `rb`, `rake`, `gemspec` | `Ruby` | `language-profiles/ruby.md` |
| `rs` | `Rust` | `language-profiles/rust.md` |
| `java` | `Java` | `language-profiles/java.md` |
| `c`, `h`, `cpp`, `hpp`, `cc`, `cxx` | `C++` | `language-profiles/c++.md` |
| `tf`, `tfvars` | `Terraform` | *(no profile)* |
| `yaml`, `yml` | `YAML` | *(no profile)* |

## Test-file detection

`is_test_file()` in `review.sh` classifies changed files as test files for the manifest. Patterns covered:

| Pattern | Language |
|---|---|
| `*_test.go` | Go |
| `test_*.py`, `*_test.py` | Python |
| `*.test.[jt]sx?`, `*.spec.[jt]sx?` | JS/TS |
| `*_spec.rb`, `*_test.rb` | Ruby |
| `*Test.java` | Java |
| `*Test.php`, `*TestBase.php` | PHP |
| `*_test.cpp`, `*_test.cc`, `*_test.ts` | C++/TS |
| Any file under `/tests/`, `/test/`, or `/spec/` | Any |

Known gap: Rust `#[cfg(test)]` modules share their source file's name — there is no filename convention to detect them without file-content inspection.

## CVE check

`analyzers/run-cve-check.sh` inspects changed dependency manifests and queries OSV.dev for known vulnerabilities affecting the declared versions. Currently supports `go.mod`, `package.json`, `requirements.txt`, and `composer.json`.

The script follows the same external-tool pattern as `analyzers/run-shellcheck.sh`: `review.sh` invokes it in Phase 1, captures a JSON findings array on stdout, and merges it through the standard pipeline (suppressions, confidence filter, dedup). Critical/High findings route to REQUEST_CHANGES through the existing severity ladder.

CVSS severity mapping:
- CVSS ≥ 9.0 → `Critical`, confidence 95
- CVSS 7.0–8.9 → `High`, confidence 95
- CVSS 4.0–6.9 → `Medium`, confidence 90
- CVSS < 4.0 or missing → `Low`, confidence 85

If OSV.dev is unreachable the script emits WARNING on stderr and returns `[]` — the review never blocks on CVE-check outage. Tests bypass the network via the `OSV_MOCK_FILE` env var which reads a canned response from a file; do not set this in production.

Suppressions work the same as for LLM findings — match on `pattern` against the finding text (e.g. `"CVE-2025-12345"` or `"GHSA-xxxx-yyyy-zzzz"`).

## Static analyzers (semgrep / trufflehog / ruff / golangci-lint)

All analyzer scripts in `analyzers/` follow the same pattern as `analyzers/run-shellcheck.sh`: accept a newline-separated `$CHANGED_FILES` argument, emit a JSON findings array on stdout, emit warnings on stderr, and return `[]` if the binary is missing or no files match their language gate. They run concurrently with shellcheck and cve-check in the parallel path.

**Binary installation** — consumers must install the binaries in their workflow. The action does not install them. If a binary is absent, the script emits `WARNING: <tool> not found` to stderr and returns `[]`; the review continues normally.

**Mock env vars for testing** (bypass binary execution in bats tests):

| Script | Mock env var | Fixture directory |
|--------|-------------|-------------------|
| `analyzers/run-semgrep.sh` | `SEMGREP_MOCK_FILE` | `tests/fixtures/semgrep/` |
| `analyzers/run-trufflehog.sh` | `TRUFFLEHOG_MOCK_FILE` | `tests/fixtures/trufflehog/` |
| `analyzers/run-ruff.sh` | `RUFF_MOCK_FILE` | `tests/fixtures/ruff/` |
| `analyzers/run-golangci-lint.sh` | `GOLANGCI_MOCK_FILE` | `tests/fixtures/golangci/` |
| `analyzers/run-hadolint.sh` | `HADOLINT_MOCK_FILE` | `tests/fixtures/hadolint/` |
| `analyzers/run-checkov.sh` | `CHECKOV_MOCK_FILE` | `tests/fixtures/checkov/` |
| `analyzers/run-phpcs.sh` | `PHPCS_MOCK_FILE` | `tests/fixtures/phpcs/` |
| `analyzers/run-eslint.sh` | `ESLINT_MOCK_FILE` | `tests/fixtures/eslint/` |
| `analyzers/run-phpstan.sh` | `PHPSTAN_MOCK_FILE` | `tests/fixtures/phpstan/` |
| `analyzers/run-kube-linter.sh` | `KUBELINTER_MOCK_FILE` | `tests/fixtures/kubelinter/` |
| `analyzers/run-tflint.sh` | `TFLINT_MOCK_FILE` | `tests/fixtures/tflint/` |

Do not set mock vars in production.

## Code suggestions

When the `enable-suggestions` action input is `true` (the default), eligible
LLM agents are instructed to emit an optional `suggested_code` field (and
optional `start_line` field for multi-line replacements) alongside each finding.
`post-review.sh` wraps `suggested_code` in a GitHub ```` ```suggestion ```` fence
inside the inline comment body, which GitHub renders as an "Apply suggestion"
button that the PR author can accept with one click. `post-review-gitlab.sh`
uses GitLab's equivalent ```` ```suggestion:-N+0 ```` syntax (where N is lines
above the anchor line for multi-line replacements).

**Eligible agents** (system prompt is augmented with `prompts/suggestion-addendum.md`):
`code-reviewer`, `edge-case-hunter`, `security-reviewer`, `silent-failure-hunter`, `blind-hunter`.

Not eligible (too design-level / holistic for concrete line-edits):
`architecture-reviewer`, `adversarial-general`, `pr-summarizer`. Static analyzers
(shellcheck, semgrep, ruff, etc.) never emit suggestions.

**Prompt injection.** `effective_prompt()` in `review.sh` composes the base
prompt with up to three shared trailers at runtime:
- `prompts/_knowledge-cutoff.md` — HARD CONSTRAINT block against
  version-existence hallucinations. Applied to all 7 finding-producing agents
  (not `pr-summarizer`).
- `prompts/_trailer-findings.md` — `json-findings` schema instruction.
  Applied to all 7 finding-producing agents.
- `prompts/suggestion-addendum.md` — "Apply suggestion" formatting. Gated by
  `AI_ENABLE_SUGGESTIONS`; applied only to the 5 line-edit-eligible agents
  listed above.

Composition order: base prompt + knowledge-cutoff + findings-trailer +
(optional) suggestion-addendum. The composed output is written to a temp
file under `${EFFECTIVE_PROMPT_PREFIX}-*.md`; `pr-summarizer` and missing-file
fallbacks return the base prompt path unchanged. Stable composition order
keeps the trailer bytes identical across eligible agents.

**Validation in `post-review.sh` and `post-review-gitlab.sh`.** The suggestion
rendering is gated on `AI_ENABLE_SUGGESTIONS=true` (case-insensitive —
`TRUE`/`True`/`true` all work). The same validation guards are duplicated in
`post-review-gitlab.sh`'s `post_inline_discussions()` function. Additional
guards applied in order:
1. `start_line` must match `^[1-9][0-9]*$` (positive integer, no leading zeros
   or 0) and be ≤ `line`. Leading zeros would trigger bash octal interpretation.
2. Multi-line ranges are capped at `MAX_SUGGESTION_RANGE=100` lines to prevent
   unbounded grep loops when an LLM emits an absurdly large `line` value.
3. `suggested_code` containing triple backticks is rejected — the embedded
   ``` would close the ```suggestion fence early and let an attacker
   (via prompt injection) inject arbitrary markdown into the review comment.
4. For multi-line suggestions, every line in `start_line..line` must appear
   in the diff's new-file side. `parse_diff_new_lines()` emits both added
   and context lines for range validation (whereas `parse_valid_lines()`
   emits only `+` lines for anchor validation).
5. When any guard fails the suggestion is dropped with a WARNING; the finding
   still posts with the natural-language remediation.

**Body finding rendering.** When a finding that carried `suggested_code` is
routed to the review body (either the `line` is not in the diff or the
`max_inline` cap was reached), `format_body_finding()` renders the suggestion
as a plain `` ``` `` code fence (not a `suggestion` fence, which only works
in inline review comments) inside the collapsible `<details>` accordion
alongside the remediation text. `post_findings()` emits a WARNING identifying
the specific reason for the overflow so operators can distinguish "agent
hallucinated a line" from "capacity limit". The triple-backtick sanitization
guard applies to body suggestions as well — `suggested_code` containing
`` ``` `` is silently dropped to prevent fence escape.

**Incremental reviews and suggestions.** The SHA watermark diffs only new
commits. A finding whose line range was in an earlier commit (and is no longer
in the incremental diff) will have its suggestion dropped during range
validation, but the finding itself still posts. To force a full-PR re-review,
add the `ai-review-rescan` label to the PR.

**Prompt for AI agents.** When findings are present, `build_agent_prompt()`
appends a collapsible "Prompt for AI agents" block to the review body. The
block contains a plain-text summary of all findings grouped by file, formatted
as instructions that can be copy-pasted into an AI coding assistant (e.g.,
Claude Code, Cursor, Copilot). The standalone review mode (GitHub Issues)
includes the same block via an inline jq equivalent. The GitLab path includes
the prompt block (GitLab renders `<details>` HTML). The Bitbucket path does
not include the prompt block (Bitbucket Cloud does not render `<details>` HTML).

**Bitbucket.** The Bitbucket path (`post-review-bitbucket.sh`) does not render
suggestion fences — findings post as markdown bullets in the summary comment.
**GitLab** supports suggestion fences using GitLab's native `suggestion` syntax.
`AI_ENABLE_SUGGESTIONS` is read in both `post-review.sh` and
`post-review-gitlab.sh`.

**Cost.** Enabling suggestions adds ~400 tokens to the system prompt of each
eligible agent and increases output token usage proportional to the number of
actionable findings. Keep `max-tokens-per-agent` (default 8192) in mind; raise
it if you see truncation warnings on large diffs with suggestions enabled.

## Suppressions

`config/suppressions.json` is a JSON array of suppression rules evaluated against merged findings. All `match` fields are optional and ANDed:

- `file` — substring match on finding's `file`
- `line` — exact integer match
- `code` — finding text starts with this prefix
- `pattern` — regex (case-insensitive) matched against finding text

An optional `verify` field triggers pre-suppression verification before accepting the suppression:

| Value | Extracts | Checks |
|-------|----------|--------|
| `github-release` | `owner/repo@vN` | `gh api repos/{owner}/{repo}/git/ref/tags/{tag}` |
| `npm` | `pkg@version` or `"pkg": "version"` | `registry.npmjs.org/{pkg}/{version}` |
| `pypi` | `pkg==version` | `pypi.org/pypi/{pkg}/{version}/json` |
| `go-module` | `module@vX.Y.Z` | `proxy.golang.org/{module}/@v/{version}.info` |
| `cargo` | `pkg = "version"` or `pkg@version` | `crates.io/api/v1/crates/{pkg}/{version}` |
| `docker-hub` | `image:tag` or `ns/image:tag` | `hub.docker.com/v2/namespaces/{ns}/repositories/{name}/tags/{tag}` |
| `ruby-org` | Ruby MRI `X.Y.Z` | `cache.ruby-lang.org/pub/ruby/{MAJ.MIN}/ruby-{MAJ.MIN.PATCH}.tar.gz` |

If verification confirms the version exists, the suppression stands. If the API returns a non-zero exit (version not found), the finding is kept — the AI reviewer may be correct. Private registries (GHCR, GCR, ECR) are not supported as they require authentication.

When adding a suppression, include an `id` and a `reason` explaining why it is a false positive.

Consuming repos can add **local suppressions** by placing a `suppressions.json` file at `.github/ai-pr-review/suppressions.json` in their repository. Local rules are merged with the global rules at runtime — no action input required. Use the same schema as the global file.

## Provider model defaults (review.sh)

| Provider | Standard model | Premium model |
|----------|---------------|---------------|
| `anthropic` | `claude-sonnet-4-6` | `claude-opus-4-7` |
| `openai` | `gpt-4o` | `gpt-4.1` |
| `openai-compatible` | (user-specified) | same as standard |
| `google` | `gemini-2.5-flash` | `gemini-2.5-pro` |
| `bedrock-proxy` | `us.anthropic.claude-sonnet-4-6` | `global.anthropic.claude-opus-4-7` |

## Retry and resilience

`llm-call.sh` retries transient API failures (HTTP 408, 429, 500, 502, 503, 504, and Cloudflare 520–524) and transient curl failures (exit codes 7, 28, 56) with exponential backoff and jitter. Configuration via env vars:

| Variable | Default | Description |
|----------|---------|-------------|
| `LLM_RETRY_COUNT` | `3` | Number of retry attempts (set to 0 to disable) |
| `LLM_RETRY_BASE_DELAY` | `2` | Base delay in seconds (doubles each retry) |

The `retry-count` input in `action.yml` maps to `LLM_RETRY_COUNT`. The `parallel` input maps to `AI_PARALLEL`. The `confidence-threshold` input maps to `AI_CONFIDENCE_THRESHOLD`. The `max-inline` input maps to `AI_MAX_INLINE`. The `max-tokens-per-agent` input maps to `AI_MAX_TOKENS_PER_AGENT`. The `enable-suggestions` input maps to `AI_ENABLE_SUGGESTIONS`.

Additional env vars consumed by the scripts (not exposed as action inputs):

| Variable | Default | Description |
|----------|---------|-------------|
| `AI_TEMPERATURE` | `0.3` | Sampling temperature for LLM calls (clamped to [0, 2]) |
| `MAX_DIFF_LINES` | `5000` | Maximum diff lines before skipping review (mapped from `max-diff-lines` action input) |
| `AI_PARALLEL` | `true` | Tiered parallel agent execution; set to `false` to disable (mapped from `parallel` action input) |
| `AI_CONFIDENCE_THRESHOLD` | `75` | Minimum confidence score for findings to be included (mapped from `confidence-threshold` action input) |
| `AI_MAX_INLINE` | `25` | Maximum inline review comments per run; excess routed to summary body (mapped from `max-inline` action input) |
| `AI_MAX_TOKENS_PER_AGENT` | `8192` | Max output tokens per LLM agent call; clamped to [256, 65536] (mapped from `max-tokens-per-agent` action input) |
| `AI_ENABLE_SUGGESTIONS` | `true` | Enable "Apply suggestion" buttons on inline review comments (mapped from `enable-suggestions` action input). See [Code suggestions](#code-suggestions). Supported on GitHub and GitLab; ignored on Bitbucket. |
| `LLM_PROMPT_CACHING` | `auto` | Enable Anthropic/Bedrock prompt caching via `cache_control: ephemeral` markers. See [Prompt caching](#prompt-caching). Valid: `auto`, `true`, `false`. |
| `AI_CACHE_PRIMING` | `false` | Opt-in. Serialize 1-2 cache-writing calls before Tier 1 fan-out so subsequent parallel agents hit the warm cache. Default off — live benchmarks showed no net cost win vs the unprimed baseline (opportunistic concurrent-start timing already captures the same cache hits). Enable (`true`) in environments where opportunistic hits don't happen (strict rate limiting, proxy serialization). Adds ~30s wall-clock when enabled. See [Cache priming](#cache-priming-issue-144--opt-in-tuning-knob). |
| `VCS_PROVIDER` | `github` | Selects the post-review script. Valid: `github`, `bitbucket`, `gitlab`. See [Multi-provider support](#multi-provider-support-github--bitbucket-cloud--gitlab). |
| `BITBUCKET_EMAIL` | — | Bitbucket-only. Atlassian account email of the bot user (Basic-auth username) |
| `BITBUCKET_API_TOKEN` | — | Bitbucket-only. Atlassian API token (Basic-auth password) |
| `BITBUCKET_WORKSPACE` / `BITBUCKET_REPO_SLUG` | — | Bitbucket-only. Optional explicit override for the repo identifier; if unset, the script splits `GITHUB_REPOSITORY` |
| `GITLAB_TOKEN` | — | GitLab-only. Personal or project access token with `api` scope; falls back to `CI_JOB_TOKEN` |
| `GITLAB_API_URL` | `https://gitlab.com/api/v4` | GitLab-only. API base URL for self-hosted instances |
| `GITLAB_PROJECT_ID` | — | GitLab-only. Numeric project ID; falls back to `CI_PROJECT_ID`, then URL-encodes `CI_PROJECT_PATH` or `GITHUB_REPOSITORY` |
| `GITLAB_MR_DIFF_BASE_SHA` | — | GitLab-only. Base SHA for inline discussion positions; falls back to `CI_MERGE_REQUEST_DIFF_BASE_SHA`. Required for inline discussions. |
| `GITLAB_BOT_USERNAME` | — | GitLab-only. Username of the bot posting reviews (for stale thread resolution); defaults to the authenticated user |

Exit codes from `llm-call.sh`:
- **0** — success
- **1** — permanent error (bad API key, invalid request, unknown provider)
- **2** — transient error (all retries exhausted)
- **3** — content issue (provider safety/recitation filter blocked response)

`post-review.sh` wraps critical GitHub API calls with `gh_api_retry()` (3 retries, 2s/4s/8s backoff) for transient errors (502, 503, 429, ETIMEDOUT). `post-review-gitlab.sh` has equivalent retry logic built into `gl_api()` (3 retries, exponential backoff + jitter) for transient errors (408, 429, 500-504) and curl failures.

## Graceful failure handling

When an agent call fails (transient API error, content filter block, configuration issue), `call_agent()` in `review.sh`:
1. Logs a WARNING with the failure type and last error message
2. Appends the agent name to the `FAILED_AGENTS` array
3. Writes empty output and continues to the next agent

After all agents complete:
- If **all** finding agents failed, the review aborts with exit 1
- Otherwise, `FAILED_AGENTS` is exported as `AI_REVIEW_FAILED_AGENTS` (colon-separated) to the post-review script
- The post-review script downgrades an empty-findings review from APPROVE to COMMENT to indicate an incomplete review

## Token usage and cost estimation

`TOKEN_LOG` in `review.sh` accumulates `"agent: input=N output=N cache_creation=N cache_read=N model=ID"` entries from `TOKENS:` lines emitted by `llm-call.sh` on stderr. The `cache_creation` and `cache_read` fields are 0 on providers where caching doesn't engage (OpenAI, Google) or runs where `LLM_PROMPT_CACHING=false` was set.

`config/model-pricing.json` maps model ID patterns to display names and per-token rates. Each entry carries four rates: `input_rate`, `output_rate`, `cache_write_rate`, and `cache_read_rate` (all cost per 1M tokens, in units of `$1e-6 / 1M tokens`). The `model_pricing()` function returns all four rates as a space-separated tuple; `emit_token_table()` generates the markdown table with an adaptive column layout — 6 columns when no rows have cache activity, 8 columns (Input / Output / Cache Write / Cache Read / Total / Est. Cost) when any row does.

## Prompt caching

When `AI_PROVIDER` is `anthropic` or `bedrock-proxy`, `llm-call.sh` uses Anthropic's ephemeral cache (5-minute TTL) via `cache_control: {type: "ephemeral"}` markers. Enabled by `LLM_PROMPT_CACHING` (default: `auto`):

- `auto` — enabled for `anthropic` and `bedrock-proxy`; no-op for OpenAI (automatic prefix caching; no marker needed) and Google Gemini (different caching API, unsupported).
- `true` — force-enable markers.
- `false` — force-disable; falls back to the legacy request layout (system = agent prompt, messages[0] = user content).

### Shared-cache layout (issue #142)

Anthropic's cache key is CUMULATIVE — a hash of the full prefix up to each `cache_control` marker. Two requests with different system prompts never share a cache entry if the marker is anywhere in or after the system prompt.

Our 5–8 agents per review split into **two cache cohorts** by the user-message variant they use:
- `CODE_CONTEXT_MSG` cohort — code-reviewer, silent-failure-hunter, security-reviewer, edge-case-hunter, adversarial-general
- `FULL_CONTEXT_MSG` cohort — pr-summarizer, architecture-reviewer

Within each cohort the agents share the same user-message bytes, so each cohort gets a single shared cache entry per run. `blind-hunter`'s `BLIND_MSG` is unique per run and stays on its own cache entry.

To unlock cross-agent caching within a cohort, we restructure the request when caching is enabled so the shared context becomes the FIRST system content block with a cache_control marker, and the per-agent prompt becomes the SECOND system block without a marker:

```
system: [
  { text: "<shared CODE_CONTEXT_MSG>",  cache_control: ephemeral },
  { text: "<per-agent system prompt>" }
]
messages: [{ role: "user", content: "Please perform your review now." }]
```

Anthropic's "walk backward" lookup finds the matching prefix hash at the end of system[0] even when system[1] differs per agent, so **every agent after the first in a run hits a cache read on the shared context bytes** (typically the majority of input tokens).

**Live-benchmarked impact** (Sonnet 4.6, 5 agents, ~25 KB shared context):

| Run | input | cache_write | cache_read | est. cost | vs no cache |
|---|---:|---:|---:|---:|---:|
| A (caching off) | 56,652 | 0 | 0 | $0.189 | baseline |
| B (cold cache, first run of a PR) | 13,722 | 8,593 | 34,372 | $0.103 | **−46%** |
| C (hot cache, re-run within 5 min) | 13,722 | 0 | 42,965 | $0.073 | **−61%** |

Weighted across typical PR traffic (70% cold / 25% hot): **~47% cheaper on average**. This replaces the pre-#142 layout which had cold runs ~23% MORE expensive (per-agent cache entries, no cross-agent sharing).

### Cache priming (issue #144) — opt-in tuning knob

Anthropic's docs say a cache entry only becomes visible **after the first response begins**, implying 5 concurrent Tier 2 agents would all miss the cache on first fan-out. In practice this turned out to be weaker than expected: live benchmarks against Bedrock showed parallel Tier 2 agents were already getting opportunistic cache hits via natural start-up staggering (different agent prompts, Opus+Sonnet TTFT differences, HTTP connection pooling).

**Priming** serializes 1-2 cache-writing calls before Tier 1 fan-out so remaining agents hit a guaranteed-warm cache. When `AI_CACHE_PRIMING=true`, `review.sh`:

1. Runs `code-reviewer` (Sonnet primer for CODE_CONTEXT_MSG cohort) concurrently with
2. `security-reviewer` (Opus primer for CODE_CONTEXT_MSG cohort, pulled forward from Tier 2 in full mode)

Both primer calls run in parallel with each other (different models → no mutual cache dependency) and complete before any other agent starts. The remaining Tier 1 agents and Tier 2 agents then fan out, every CODE_CONTEXT_MSG user seeing a warm cache on its first call.

**Default is `false`.** A single-sample live benchmark on a realistic full-mode review showed priming was cost-neutral vs the unprimed baseline (same 2-of-5 cache hits either way — the baseline's opportunistic hits matched priming's guaranteed hits) and added +30s of wall-clock latency. Not worth the default. Further investigation tracked in issue #153 to collect multi-sample data before committing to a final default.

Left in as an opt-in knob for environments where opportunistic hits fail:
- Strict per-request rate limits that serialize concurrent calls
- Proxy layers that queue rather than parallelize
- Shorter-than-expected cache-visibility windows on non-Bedrock deployments
- Cases where reducing variance matters more than median wall-clock time

`AI_CACHE_PRIMING=true` enables it. `AI_CACHE_PRIMING=false` (default) is pre-#144 full-parallel behavior.

No priming is applied to:
- `FULL_CONTEXT_MSG` cohort (`pr-summarizer` + `architecture-reviewer`) — pr-summarizer already runs in Tier 1 before the tier barrier, so architecture-reviewer in Tier 2 naturally sees the warm cache when both run.
- `BLIND_MSG` — single-agent cohort, no sharing possible.

### Semantic change

The shared-cache layout moves the diff/context from the user message into system[0]. Prompts were originally written assuming the diff arrived as a user turn. Empirically Claude treats late-system content as "additional instructions" and early-system content as "context," so the two layouts produce equivalent findings (verified by `claude/bench-quality.sh`). The layout is used only when prompt caching is active; `LLM_PROMPT_CACHING=false` preserves the legacy shape bit-for-bit.

### Cache-minimum threshold

Anthropic caches only prefixes ≥ 1024 tokens (Sonnet/Opus) or ≥ 2048 tokens (Haiku). Empirically the Sonnet floor on Bedrock is ~2048. Contexts below ~8KB may silently not cache — verify via `cache_creation_input_tokens` > 0 in the first response. Typical review runs far exceed this threshold.

**Tiny-PR regression.** Because the shared-cache layout puts the (small) CODE_CONTEXT_MSG first and the (larger) per-agent system prompt second, PRs whose diff falls below the cache threshold silently get zero cache hits. Under the pre-#142 layout the larger per-agent system prompt was the cache anchor, so per-agent retries on tiny PRs could still hit cache within the 5-minute TTL. This PR trades that edge case for the far bigger win of cross-agent sharing on normal-to-large PRs. The delta on tiny PRs is a few cents at most since token volume is low there.

### What is NOT cached

`blind-hunter` uses `BLIND_MSG` (zero-context constraint) which is much shorter and structurally distinct, so it does not share the cache with other agents.

### OpenAI automatic prefix caching

OpenAI provides automatic prefix caching (50% discount on cached input tokens) for prompts ≥ 1024 tokens. No explicit markers are needed — caching is based on the longest matching prefix. `call_openai()` extracts `usage.prompt_tokens_details.cached_tokens` from the response and reports it as `cache_read` in the TOKENS line. The `input` count is adjusted to exclude cached tokens (matching Anthropic's convention where `input` = uncached only) so the cost formula works correctly across providers.

OpenAI's cache_write_rate is 0 (no write premium — unlike Anthropic's 1.25x write cost). The discount is applied only on reads via `cache_read_rate` in `model-pricing.json`.

**Current limitation:** Our request layout puts the agent-specific system prompt first, so agents with different prompts have different prefixes, defeating cross-agent cache sharing within a review run. A follow-up optimization could restructure the OpenAI request to mirror the Anthropic shared-cache layout (shared context first, agent prompt second).

### Live benchmarks

Scripts in `claude/` (gitignored): `bench-cache3.sh` (single-agent round-trip), `bench-shared.sh` (5-agent cross-cache), `bench-analyze-shared.sh` (cost computation), `bench-quality.sh` (output-equivalence check between layouts). Results summarized in `claude/BENCHMARK-REPORT.md` and `claude/SHARED-CACHE-BENCHMARK.md`.

## Testing locally

```bash
# Run the unit test suite (requires bats and jq)
bats tests/*.bats

# Lint all shell scripts
shellcheck review.sh llm-call.sh post-review.sh post-review-bitbucket.sh post-review-gitlab.sh analyzers/run-shellcheck.sh analyzers/run-cve-check.sh

# Smoke-test llm-call.sh against a provider
export AI_PROVIDER=anthropic ANTHROPIC_API_KEY=<key>
echo "hello" > /tmp/msg.txt
echo "Say hi" > /tmp/sys.txt
./llm-call.sh claude-haiku-4-5 /tmp/sys.txt /tmp/msg.txt

# Dry-run review.sh (requires a real repo with a PR)
export AI_PROVIDER=anthropic ANTHROPIC_API_KEY=<key> GH_TOKEN=<token>
export GITHUB_REPOSITORY=owner/repo PR_NUMBER=123 BASE_REF=main HEAD_SHA=<sha>
bash review.sh
```

## Standalone review mode

In addition to reviewing open PRs, the action supports a **standalone** mode that reviews any branch or commit SHA and posts findings as a GitHub issue rather than a PR review.

### Triggering standalone mode

Via `workflow_dispatch` in the GitHub UI or CLI:
- Leave `pr_number` empty (standalone is assumed when no PR number is provided)
- Optionally set `branch` to the branch to review (defaults to the repo's default branch)
- Diffs against the repo's default branch automatically

Programmatically:
```bash
export REVIEW_TARGET=standalone
export AI_PROVIDER=anthropic ANTHROPIC_API_KEY=<key> GH_TOKEN=<token>
export GITHUB_REPOSITORY=owner/repo BASE_REF=main HEAD_SHA=<sha>
bash review.sh
```

### What changes in standalone mode

- No PR comment or PR review is posted
- All findings are posted as a single GitHub issue with severity, file:line references, and remediation notes
- The SHA watermark / incremental diff is skipped (always a full diff)
- Oversized diffs exit cleanly without posting a skip comment
- `post-review.sh` is invoked with `--standalone` instead of a PR number

### Issue output format

- Title: `🚨 AI Review: High risk — abc1234 on main`
- Body: overall risk summary, pr-summarizer output, findings sorted by severity, token usage table
- Labels: `ai-review` (always), `ai-review-action-needed` (Critical/High) — falls back gracefully if labels don't exist

## Test architecture

Tests live in `tests/` and use [bats-core](https://github.com/bats-core/bats-core). Because the scripts have no main guard (sourcing them triggers the full orchestration pipeline), `tests/test_helper.bash` extracts individual function definitions using an awk brace-depth tracker and `eval`s them into the test shell. This means:

- No production script changes are needed to make functions testable
- Tests cover pure functions from `review.sh` (`detect_language`, `is_test_file`, `model_pricing`, `model_display_name`, `format_cost`, `extract_findings`, `merge_findings`, `call_agent`, `call_agent_bg`, `collect_parallel_results`), from `llm-call.sh` (`is_transient_http`, `is_transient_curl`, `retry_curl`), from `post-review.sh` (`gh_api_retry`, `severity_icon`, `parse_valid_lines`, `format_body_finding`, `build_agent_prompt`), and from `post-review-gitlab.sh` (`severity_icon`, `format_source_tag`, `classify_risk`, `resolve_project_id`, plus 3-way parity tests)
- Fixture files in `tests/fixtures/` provide sample agent output for `extract_findings` tests

To add a test for a new function, call `load_function "$script" "function_name"` in the `setup()` block of the relevant `.bats` file.

Static analyzer scripts in `analyzers/` are tested via their own `.bats` files using the mock env var pattern — the test sets `<TOOL>_MOCK_FILE` to a fixture file path and the script reads that instead of invoking the binary. This avoids the `load_function` awk pattern entirely since the scripts are simple enough to be invoked directly.

## Release process

Run `/comprehensive-review` before tagging or releasing. This action is consumed via direct action reference (`@main`, `@v1.0`) or as a git submodule. Direct references pin to a branch or tag; submodule consumers pin to a specific commit. Breaking changes require a version bump and coordinated updates in consuming repos.
