---
layout: default
title: Architecture
nav_order: 7
---

# Architecture

Since v1.0.0, the **Python engine** (`AI_PR_REVIEW_ENGINE=python`) is the default. It is
implemented in the `ai_pr_review/` package and runs compute, agent dispatch, and VCS posting
in a single process. The **bash engine** (`engine: bash`) is the deprecated legacy path: it
still works when explicitly set but emits a deprecation warning and will be removed in a
future major release. See [Features](features) for the v1.0.0 announcement and
[Configuration](configuration) for the `AI_PR_REVIEW_ENGINE` reference.

```
ai-pr-review/
├── action.yml              # GitHub Actions composite action definition
├── review.sh               # Engine dispatcher: delegates to python3 -m ai_pr_review review
│                           # by default; falls through to legacy bash phases only when
│                           # AI_PR_REVIEW_ENGINE=bash is explicitly set
│
├── ai_pr_review/           # Python engine (default since v1.0.0)
│   ├── cli.py              # Click entrypoint: `python3 -m ai_pr_review review`
│   ├── config.py           # Typed config (ReviewConfig.from_env(), resolve_models())
│   ├── orchestrate.py      # run_review(): agent tier dispatch, findings merge, post
│   ├── review/             # Assembly layer
│   │   ├── runtime.py      # build_review_runtime(): env → orchestration seam
│   │   ├── compute.py      # Diff computation, SHA watermark, language detection
│   │   └── outcome.py      # Review outcome classification
│   ├── agents/             # Agent roster, eligibility gates, prompt composition
│   ├── llm/                # Multi-provider LLM clients (Anthropic, OpenAI, Google, Bedrock)
│   ├── vcs/                # VCS provider clients (GitHub, GitLab, Bitbucket)
│   ├── findings/           # Findings pipeline: merge, suppress, deduplicate
│   ├── analyzers/          # Native static analyzer wrappers (13 Python implementations + bridge dispatcher)
│   ├── context/            # Context enrichment (tree-sitter symbol injection; opt-in)
│   ├── feedback/           # Learning loop: feedback store, injection (opt-in)
│   └── slash/              # Slash command handling (/ai-pr-review false-positive, etc.)
│
├── llm-call.sh             # Stateless curl LLM client — used by deprecated bash engine only
├── post-review.sh          # GitHub API posting — used by deprecated bash engine only
├── post-review-bitbucket.sh # Bitbucket posting — used by deprecated bash engine only
├── post-review-gitlab.sh   # GitLab posting — used by deprecated bash engine only
├── analyzers/              # Static analyzer shell wrappers — 13 tools; used only by the deprecated bash engine
├── prompts/                # Agent system prompts + shared trailers — see deep reference
├── config/                 # Configuration and data files
│   ├── model-pricing.json  # Per-model token pricing for cost estimation
│   └── suppressions.json   # Declarative false-positive suppression rules
├── language-profiles/      # Per-language review context (markdown, injected into prompts)
├── tests/                  # bats-core unit tests for the bash engine — see deep reference
├── tests/python/           # pytest suite for the Python engine — see deep reference
└── .github/workflows/
    ├── ai-review.yml       # Self-test: runs the action on its own PRs
    ├── lint.yml            # Shellcheck + bats + pytest
    ├── pages.yml           # GitHub Pages documentation site build
    └── publish-image.yml   # Container image build, push, and signing
```

For the full directory listing with per-file annotations, see the [internal architecture reference](https://github.com/tag1consulting/ai-pr-review/blob/main/docs/ARCHITECTURE.md).

## Data flow

### Default: Python engine (`AI_PR_REVIEW_ENGINE=python`)

1. **action.yml** passes `AI_PR_REVIEW_ENGINE=python` (the default) and calls `review.sh`
2. **review.sh** detects the engine and delegates: `python3 -m ai_pr_review review` — the rest of `review.sh` does not run
3. **`ai_pr_review/review/runtime.py`** (`build_review_runtime`) resolves config, builds the VCS provider, fetches the last-reviewed SHA, computes the diff, detects languages, runs static analyzers, loads SARIF findings, and loads suppression rules
4. **`ai_pr_review/orchestrate.py`** (`run_review`) dispatches agent tiers in parallel, merges LLM and pre-computed findings, deduplicates, applies suppressions, and posts the summary and findings via the VCS provider client — all in one process
5. Each agent prompt is composed at dispatch time by injecting shared trailers: a **governance preamble** (`prompts/_governance.md` — severity calibration, verify-before-naming, secret redaction), a knowledge-cutoff guard, a findings-schema instruction, and optionally a code-suggestion addendum

### Legacy: bash engine (`AI_PR_REVIEW_ENGINE=bash`, deprecated)

Setting `engine: bash` explicitly causes `review.sh` to emit a `::warning::` deprecation annotation and execute the legacy pipeline in-process:

1. **review.sh** computes the diff, builds a file manifest, detects languages
2. For each agent, **review.sh** assembles a context message and calls **llm-call.sh**
3. **llm-call.sh** sends the prompt to the configured LLM provider via curl
4. **review.sh** extracts JSON findings from agent responses, deduplicates, applies suppressions
5. The **provider-specific post-review script** (`post-review.sh`, `post-review-bitbucket.sh`, or `post-review-gitlab.sh` — selected by `VCS_PROVIDER`) resolves stale threads, posts the summary and findings, advances the SHA watermark

The bash engine will be removed in a future major release. To migrate, remove the `engine: bash` line from your workflow (or change it to `engine: python`).

## Dependencies

The action requires `bash`, `curl`, `jq`, `git`, `gh`, and `python3` — all pre-installed on standard GitHub-hosted runners. `shellcheck` is installed automatically if not already present.

The **container action** (recommended) ships all static analyzer binaries pre-installed at pinned versions — no runner setup needed. The **direct action reference** and **git submodule** paths do not install analyzer binaries; see [runtime dependencies](installation-direct-action#runtime-dependencies) for the optional install-in-workflow snippet.

## Deep reference

For implementation internals — findings pipeline phases, parallel agent execution, prompt caching, code suggestion validation, test architecture, Dockerfile multi-stage layout — see the [internal architecture reference](https://github.com/tag1consulting/ai-pr-review/blob/main/docs/ARCHITECTURE.md) (ARCHITECTURE.md).
