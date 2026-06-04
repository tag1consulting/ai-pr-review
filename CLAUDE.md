# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A GitHub Actions composite action that runs multiple LLM agents against a PR diff and posts a structured review (summary comment + inline findings) back to the PR. It is consumed by downstream repos either as a direct action reference (`uses: tag1consulting/ai-pr-review@main`) or as a git submodule.

## Key scripts and their roles

| Script | Role |
|--------|------|
| `review.sh` | Main orchestrator: diff computation, phase sequencing, agent dispatch (parallel tiers), context assembly, invokes the provider-specific post-review script |
| `lib/agents.sh` | LLM agent dispatch: `call_agent`, `call_agent_bg`, `wait_tier_pids`, `collect_parallel_results`, `cache_priming_effective`, `effective_prompt` |
| `lib/findings.sh` | Findings pipeline: `extract_findings`, `merge_findings`, `apply_suppressions` (with verify-type handlers for npm, PyPI, Go, Cargo, Docker Hub, GitHub releases, ruby-lang.org) |
| `lib/diff.sh` | Diff helpers: `post_skip_comment` (VCS-provider skip comment), `build_file_manifest` (language detection, file categorization, manifest building, language profiles, project context) |
| `lib/pricing.sh` | Token pricing and cost estimation: `model_pricing`, `model_display_name`, `format_cost`, `emit_token_table` |
| `lib/languages.sh` | Language detection: `detect_language`, `is_test_file` |
| `llm-call.sh` | Stateless curl-based LLM client; dispatches to the correct provider based on `AI_PROVIDER`; writes response to stdout, emits `TOKENS:` line to stderr. Anthropic and Bedrock paths enable prompt caching via `cache_control: ephemeral` markers (gated by `LLM_PROMPT_CACHING`; default `auto`). |
| `post-review.sh` | GitHub API layer: resolves/dismisses stale review threads, posts summary comment, posts findings as a PR review with inline comments, advances SHA watermark |
| `post-review-bitbucket.sh` | Bitbucket Cloud API layer: upserts one summary comment containing findings, advances SHA watermark |
| `post-review-gitlab.sh` | GitLab API layer: upserts summary note, posts inline MR discussions with suggestion fences, resolves stale bot discussions, advances SHA watermark |
| `vcs/common.sh` | Shared helpers sourced by all post-review scripts: `severity_icon`, `format_source_tag`, `classify_risk`, `format_body_finding`, `build_agent_prompt`, `parse_valid_lines`, `parse_diff_new_lines`, `mktemp_tracked`, `cleanup` |
| `analyzers/run-*.sh` | Static analyzer wrappers (shellcheck, cve-check, semgrep, trufflehog, ruff, golangci-lint, hadolint, checkov, phpcs, eslint, phpstan, kube-linter, tflint); each outputs findings in the standard JSON schema |
| `action.yml` | GitHub Actions composite action definition; maps inputs to env vars and calls `review.sh` |

## Provider model defaults

| Provider | Standard model | Premium model |
|----------|---------------|---------------|
| `anthropic` | `claude-sonnet-4-6` | `claude-opus-4-8` |
| `openai` | `gpt-5.4-mini` | `gpt-5.4` |
| `openai-compatible` | (user-specified) | same as standard |
| `google` | `gemini-2.5-flash` | `gemini-2.5-pro` |
| `bedrock-proxy` | `us.anthropic.claude-sonnet-4-6` | `global.anthropic.claude-opus-4-7` |

## Adding a new agent

1. Add a prompt file to `prompts/<agent-name>.md`. The prompt must instruct the model to output a `json-findings` block.
2. In `review.sh`, call `call_agent "<name>" "$AI_MODEL_STANDARD|PREMIUM" "${SCRIPT_DIR}/prompts/<agent-name>.md" "<msg_var>" "<output_var>" [max_tokens]` and push `<output_var>` onto `AGENT_OUTPUTS`.
3. If the agent should only run conditionally, gate it with a grep check on `$DIFF_FILE`.
4. Also add the agent to the parallel tier block (Tier 1 or Tier 2).

See [CONTRIBUTING.md](CONTRIBUTING.md) for step-by-step recipes.

## Adding a language profile

The canonical language list lives in `ai_pr_review/languages.py:_EXT_MAP`; `lib/languages.sh:detect_language()` is a port and must stay in sync with it.

Create `language-profiles/<language>.md` (filename must match the lowercase language key returned by `detect_language()` in `lib/languages.sh`).

| Extension(s) | Language key | Profile file |
|---|---|---|
| `go` | `Go` | `language-profiles/go.md` |
| `py` | `Python` | `language-profiles/python.md` |
| `js`, `jsx` | `JavaScript` | `language-profiles/javascript.md` |
| `ts`, `tsx` | `TypeScript` | `language-profiles/typescript.md` |
| `php`, `module`, `theme`, `inc` | `PHP` | `language-profiles/php.md` |
| `sh`, `bash` | `Shell` | `language-profiles/shell.md` |
| `rb`, `rake`, `gemspec` | `Ruby` | `language-profiles/ruby.md` |
| `rs` | `Rust` | `language-profiles/rust.md` |
| `java` | `Java` | `language-profiles/java.md` |
| `c`, `h`, `cpp`, `hpp`, `cc`, `cxx` | `C++` | `language-profiles/c++.md` |
| `tf`, `tfvars` | `Terraform` | `language-profiles/terraform.md` |
| `yaml`, `yml` | `YAML` | `language-profiles/yaml.md` |
| `kt`, `kts` | `Kotlin` | `language-profiles/kotlin.md` |
| `swift` | `Swift` | `language-profiles/swift.md` |
| `cs` | `CSharp` | `language-profiles/csharp.md` |
| `scala`, `sbt` | `Scala` | `language-profiles/scala.md` |
| `sql` | `SQL` | `language-profiles/sql.md` |
| `lua` | `Lua` | `language-profiles/lua.md` |
| `pl`, `pm` | `Perl` | `language-profiles/perl.md` |

## Test-file detection

`is_test_file()` in `lib/languages.sh` classifies changed files as test files for the manifest:

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

## Static analyzer mock env vars (testing only)

| Script | Mock env var | Fixture directory |
|--------|-------------|-------------------|
| `analyzers/run-cve-check.sh` | `OSV_MOCK_FILE` | `tests/fixtures/osv/` |
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
```

## Deep reference

For detailed implementation internals (findings pipeline, parallel execution, caching, suggestions, suppressions, token accounting, retry/resilience, test architecture, Dockerfile layout), see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

For contributor how-tos (adding an analyzer, agent, language profile, or VCS provider), see [CONTRIBUTING.md](CONTRIBUTING.md).

## Release process

1. **Run `/comprehensive-review`** on the release branch before tagging.
2. **Run `Workflow({name: 'ai-pr-review-e2e'})`** to build the image from the current checkout and validate it against all three test platforms (GitHub PR #1, GitLab MR !34, Bitbucket PR #2). The workflow throws on failure — do not tag until it passes. Supports `args.mode` (`quick`/`full`, default `full`) and `args.platforms` for targeted runs. Script lives at `~/.claude/workflows/ai-pr-review-e2e.js` (user-level; not committed to this repo).
3. **Tag and push** — `publish-image.yml` promotes `:dev` to `:v1.x.x` + `:latest`.

This action is consumed via direct action reference (`@main`, `@v1.0`) or as a git submodule. Breaking changes require a version bump and coordinated updates in consuming repos.
