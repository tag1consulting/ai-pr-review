# ai-pr-review: Core

GitHub Actions composite action + Python engine that runs LLM agents and static analyzers against PR diffs and posts structured reviews. Consumed as `uses: tag1consulting/ai-pr-review@main` or as a git submodule.

## Dual-layer architecture
The repo has **two parallel implementations**:
1. **Bash layer** (`review.sh`, `lib/`, `analyzers/`, `vcs/`, `llm-call.sh`, `post-review*.sh`) — the original orchestrator; still the GitHub Actions entry point via `action.yml`.
2. **Python engine** (`ai_pr_review/`) — the newer, richer implementation; invoked from bash via `ai-pr-review` CLI. Both layers must stay in sync for parity.

## Python package layout (`ai_pr_review/`)
- `cli.py` — Click CLI; entry point (`ai-pr-review review`, `ai-pr-review slash`)
- `orchestrate.py` — `run_review()` top-level orchestration; `ReviewResult`, `OrchestrationConfig`
- `review/runtime.py` — `ReviewRuntime`, `build_review_runtime()`; assembles all runtime state
- `config.py` — `ReviewConfig` (Pydantic); all env-var–driven config
- `agents/` — agent roster, dispatch, gates; see `mem:agents`
- `findings/` — `Finding` model, extract/merge/suppress pipeline; see `mem:findings`
- `llm/` — multi-provider LLM client; see `mem:llm`
- `vcs/` — VCS provider protocol + GitHub/GitLab/Bitbucket implementations; see `mem:vcs`
- `slash/` — `/ai-pr-review dismiss|false-positive|wont-fix` slash command handling
- `diff/` — diff computation and line mapping
- `context/` — tree-sitter budget/symbols for context enrichment
- `languages.py` — `detect_language()`, `is_test_file()`; canonical extension map
- `language_profiles.py` — loads `language-profiles/<lang>.md` prompt fragments
- `manifest.py` — `build_file_manifest()` for file categorization
- `pricing.py` — token cost estimation
- `telemetry.py` — optional telemetry emission

## Bash layer layout
- `review.sh` — main orchestrator (diff, phase sequencing, agent dispatch, context)
- `lib/agents.sh`, `lib/findings.sh`, `lib/diff.sh`, `lib/pricing.sh`, `lib/languages.sh`
- `analyzers/run-*.sh` — static analyzer wrappers (shellcheck, semgrep, trufflehog, ruff, golangci-lint, hadolint, checkov, phpcs, eslint, phpstan, kube-linter, tflint, cve-check)
- `vcs/common.sh` — shared helpers for all post-review scripts
- `post-review.sh` / `post-review-bitbucket.sh` / `post-review-gitlab.sh` — VCS API layers

## Key invariants
- `lib/languages.sh:detect_language()` is a port of `ai_pr_review/languages.py:_EXT_MAP`; must stay in sync.
- Language profile filenames (`language-profiles/<key>.md`) must match the lowercase key from `detect_language()`.
- All findings output a standard JSON schema (`json-findings` fenced block).
- Python engine requires ≥3.11; pydantic v2, click, httpx, anyio.
- `action.yml` maps inputs → env vars → `review.sh`.

## Further memories
- Agent architecture: `mem:agents`
- Findings pipeline: `mem:findings`
- LLM client layer: `mem:llm`
- VCS provider layer: `mem:vcs`
- Test/task completion: `mem:task_completion`
- Commands: `mem:suggested_commands`
- Code conventions: `mem:conventions`
