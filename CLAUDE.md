# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A GitHub Actions composite action that runs multiple LLM agents against a PR diff and posts a structured review (summary comment + inline findings) back to the PR. It is consumed by downstream repos either as a direct action reference (`uses: tag1consulting/ai-pr-review@main`) or as a git submodule.

## Two engines: Python (default) and bash (deprecated, Epic 5 deletion)

The action ships two engine implementations. The **Python engine in `ai_pr_review/`** is the default since Epic 4 (`engine: python` in `action.yml`) and is the runtime path for every consumer. The **bash engine** (`review.sh`, `lib/*.sh`, `post-review*.sh`, `llm-call.sh`, `vcs/common.sh`) is retained only for the Epic 4 sunset window and is scheduled for deletion in Epic 5 (#199). New work goes in the Python engine; bash entries below are kept for reference only.

## Python engine modules (default path)

| Module | Role |
|--------|------|
| `ai_pr_review/cli.py` | Click CLI entry point; assembles `Config`, builds runtime, runs preflight (summarizer + issue-linker), invokes `run_review`, emits telemetry and step-summary |
| `ai_pr_review/orchestrate.py` | `run_review`: phase sequencing — dispatch agents, extract/merge/suppress/scope findings, decide outcome, post summary then findings, advance watermark only after `findings_result.ok` (#493), resolve stale |
| `ai_pr_review/review/runtime.py` | `build_review_runtime`: assembles `ReviewRuntime` (diff context, manifest, language profiles, agent roster, dispatch context, SARIF extras) consumed by `cli` |
| `ai_pr_review/review/{compute,outcome,watermark}.py` | Pre-flight diff compute / `APPROVE`/`REQUEST_CHANGES`/`COMMENT` decision / SHA-marker rewrite |
| `ai_pr_review/agents/dispatch.py` | Agent dispatch: `dispatch_tier` runs eligible agents concurrently via `LLMClient`, builds system + user prompts, applies per-agent token budgets |
| `ai_pr_review/agents/{roster,gates,summarizer}.py` | Agent specs and tier mapping / conditional eligibility / pr-summarizer wrapper |
| `ai_pr_review/llm/client.py` + `llm/{anthropic,openai,openai_compatible,google,bedrock}.py` | Provider-specific `LLMRequest`/`LLMResponse` plumbing; Anthropic + Bedrock paths use `cache_control: ephemeral` for prompt caching |
| `ai_pr_review/findings/{extract,merge,suppress,scope,models}.py` | Findings pipeline: parse `json-findings` blocks → merge across agents → apply suppressions (with verify-type registry checks) → diff-scope filter → typed `Finding` |
| `ai_pr_review/vcs/{github,gitlab,bitbucket}.py` | Provider HTTP layer: `post_summary`, `post_findings` (inline + body), `advance_sha_watermark`, `resolve_stale`, `post_skip_comment` |
| `ai_pr_review/vcs/_{body,inline,finding_ids,stale}.py`, `marker.py`, `protocol.py`, `http.py` | Shared formatting / inline-eligibility / F-ID assignment / stale-thread helpers / `VcsProvider` protocol / retrying httpx client |
| `ai_pr_review/analyzers/bridge.py` + `analyzers/native/*.py` | Native Python ports of all 13 static analyzers (Epic 8); bridge dispatches per-analyzer file-type gates |
| `ai_pr_review/analyzers/sarif.py` | Ingest external SARIF files (Capability B) and convert to `Finding` objects |
| `ai_pr_review/context/{treesitter,symbols,budget}.py` | Tree-sitter context-enrichment: parse changed files, extract symbol refs, budget per agent prompt |
| `ai_pr_review/feedback/{store,inject,models,retention}.py` | Feedback-loop store on the `ai-pr-review-bot` branch; injects recent learnings into prompts (Capability C) |
| `ai_pr_review/slash/{parser,handlers}.py` | `/ai-pr-review` slash-command dispatcher (rescan, dismiss, false-positive, wont-fix, feedback, explain, revise, help) |
| `ai_pr_review/diff/{compute,eligibility,linemap}.py` | Diff acquisition, change-line eligibility, line-number maps |
| `ai_pr_review/manifest.py`, `languages.py`, `language_profiles.py` | Changed-files manifest, language detection (canonical `_EXT_MAP`), language-profile loader |
| `ai_pr_review/{config,errors,logging,telemetry,pricing}.py` | Typed `Config`, exception hierarchy, structured logging, telemetry-event emit, model pricing |

## Key scripts and their roles (bash engine — DEPRECATED, Epic 5 deletion)

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

## Adding a new agent (Python engine)

1. Add a prompt file to `prompts/<agent-name>.md`. The prompt must instruct the model to output a `json-findings` block.
2. Add an `AgentSpec` entry to `ai_pr_review/agents/roster.py` with the agent name, prompt path, tier (1 or 2 — controls parallel dispatch group), `max_output_tokens`, `full_mode_only` flag, `conditional_trigger` (file-pattern or `None`), and `context_enrichment_eligible` flag.
3. If the agent should only run when specific files change, set `conditional_trigger` to a glob/regex pattern; the gate evaluation lives in `ai_pr_review/agents/gates.py`.
4. Add unit-test coverage in `tests/python/agents/` for any custom gate logic.

See [CONTRIBUTING.md](CONTRIBUTING.md) for step-by-step recipes (the Python recipe is the canonical one; the bash recipe is retained for Epic 5 reference).

## Adding a language profile

The canonical language list lives in `ai_pr_review/languages.py:_EXT_MAP`. Until Epic 5 deletes the bash engine, `lib/languages.sh:detect_language()` is a port and must stay in sync with it (drift-checked by parity tests).

Create `language-profiles/<language>.md` (filename must match the lowercase language key returned by `detect_language()` in `ai_pr_review/languages.py`).

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

`is_test_file()` in `ai_pr_review/languages.py` (and the parallel implementation in `lib/languages.sh` until Epic 5) classifies changed files as test files for the manifest:

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
| `analyzers/run-cve-check.sh` | `OSV_MOCK_FILE` | `tests/fixtures/cve/` |
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
# Python engine — the default and the canonical test suite
pip install -e ".[dev,context]"
pytest tests/python -q                  # ~1474 tests, < 60s
mypy ai_pr_review/                      # 82 source files, must be clean
ruff check ai_pr_review/ tests/python/  # E,F,W,I,UP,B,SIM rules

# Bash engine (Epic 5 deletion target — keep green until removed)
bats tests/*.bats                                                          # requires bats + jq
shellcheck review.sh llm-call.sh post-review.sh post-review-bitbucket.sh post-review-gitlab.sh analyzers/run-shellcheck.sh analyzers/run-cve-check.sh
```

## Deep reference

For detailed implementation internals (findings pipeline, parallel execution, caching, suggestions, suppressions, token accounting, retry/resilience, test architecture, Dockerfile layout), see [docs/architecture-internals.md](docs/architecture-internals.md).

For contributor how-tos (adding an analyzer, agent, language profile, or VCS provider), see [CONTRIBUTING.md](CONTRIBUTING.md).

For the full bash wrapper inventory — binary flags, output-field mapping, path normalization, mock env var, and Phase 11 port complexity for each of the 13 `analyzers/run-*.sh` wrappers — see [docs/analyzers-bash-inventory.md](docs/analyzers-bash-inventory.md).

## Release process

1. **Run `/comprehensive-review`** on the release branch before tagging.
2. **Run `Workflow({name: 'ai-pr-review-e2e'})`** to build the image from the current checkout and validate it against all three test platforms (GitHub PR #1, GitLab MR !34, Bitbucket PR #2). The workflow throws on failure — do not tag until it passes. Supports `args.mode` (`quick`/`full`, default `full`) and `args.platforms` for targeted runs.
3. **Tag and push** — `publish-image.yml` promotes `:dev` to `:v1.x.x` + `:latest`.

This action is consumed via direct action reference (`@main`, `@v1.0`) or as a git submodule. Breaking changes require a version bump and coordinated updates in consuming repos.

## Addressing pull request review findings

When working on well-defined tasks that would benefit from iterative improvement, attempt to use the ralph-loop plugin to iteratively develop and improve the work.  Addressing pull request review findings is a good place to use this.  Use prompts like the following:
/ralph-loop "Please watch the ai-pr-review action and reviews.  Address findings as they are posted.  Reply to the review comments with commit SHAs or a won't fix/false positive reason.  Use the ai-pr-review slash commands whenever possible. Resolve all review threads after they have been addressed and replied to. Output <promise>APPROVED</promise> ONLY when the entire test suite passes and there are no new review processes running or review comments pending." --completion-promise "APPROVED" --max-iterations 15
