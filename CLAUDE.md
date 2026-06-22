# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A GitHub Actions composite action that runs multiple LLM agents against a PR diff and posts a structured review (summary comment + inline findings) back to the PR. It is consumed by downstream repos either as a direct action reference (`uses: tag1consulting/ai-pr-review@main`) or as a git submodule.

The action uses the Python engine in `ai_pr_review/`.

## Python engine modules

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

See [CONTRIBUTING.md](CONTRIBUTING.md) for step-by-step recipes.

## Adding a language profile

The canonical language list lives in `ai_pr_review/languages.py:_EXT_MAP`.

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

`is_test_file()` in `ai_pr_review/languages.py` classifies changed files as test files for the manifest:

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

## Testing locally

```bash
pip install -e ".[dev,context]"
pytest tests/python -q                  # < 60s
mypy ai_pr_review/                      # 82 source files, must be clean
ruff check ai_pr_review/ tests/python/  # E,F,W,I,UP,B,SIM rules
```

## Deep reference

For detailed implementation internals (findings pipeline, parallel execution, caching, suggestions, suppressions, token accounting, retry/resilience, test architecture, Dockerfile layout), see [docs/architecture-internals.md](docs/architecture-internals.md).

For contributor how-tos (adding an analyzer, agent, language profile, or VCS provider), see [CONTRIBUTING.md](CONTRIBUTING.md).

## Release process

1. **Run `/comprehensive-review`** on the release branch before tagging.
2. **Run `Workflow({name: 'ai-pr-review-e2e'})`** to build the image from the current checkout and validate it against all three test platforms (GitHub PR #1, GitLab MR !34, Bitbucket PR #2). The workflow throws on failure — do not tag until it passes. Supports `args.mode` (`quick`/`full`, default `full`) and `args.platforms` for targeted runs.
3. **Tag and push** — `publish-image.yml` promotes `:dev` to `:v1.x.x` + `:latest`.

This action is consumed via direct action reference (`@main`, `@v1.0`) or as a git submodule. Breaking changes require a version bump and coordinated updates in consuming repos.

## Addressing pull request review findings

When working on well-defined tasks that would benefit from iterative improvement, attempt to use the ralph-loop plugin to iteratively develop and improve the work.  Addressing pull request review findings is a good place to use this.  Use prompts like the following:
/ralph-loop "Please watch the ai-pr-review action and reviews.  Address findings as they are posted.  Reply to the review comments with commit SHAs or a won't fix/false positive reason.  Use the ai-pr-review slash commands whenever possible. Resolve all review threads after they have been addressed and replied to. Output <promise>APPROVED</promise> ONLY when the entire test suite passes and there are no new review processes running or review comments pending." --completion-promise "APPROVED" --max-iterations 15
