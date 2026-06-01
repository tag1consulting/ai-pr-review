# Tech Stack

## Python engine
- Python ≥3.11 (uses `match`, walrus, `Self`, etc.)
- **pydantic v2** — `ReviewConfig`, `Finding`, all models; use `model_validator`, `field_validator` v2 API
- **click ≥8.1** — CLI (`ai_pr_review/cli.py`)
- **httpx ≥0.27** — async HTTP for LLM and VCS calls
- **anyio ≥4.0** — async runtime (trio or asyncio); tests use trio backend
- **tree-sitter-language-pack** (optional, `context` extra) — AST-based context enrichment
- Build: `setuptools ≥68`; entry point: `ai-pr-review = "ai_pr_review.cli:cli"`

## Dev/test toolchain
- **pytest ≥8.0** with paths `tests/python/` and `tests/golden/`
- **respx ≥0.21** — httpx request mocking
- **ruff ≥0.9** — linter + formatter; target py311; line-length 100; selects E,F,W,I,UP,B,SIM; ignores E501
- **mypy ≥1.10** — strict mode, `ignore_missing_imports=True`

## Bash layer
- Bash (POSIX-ish); linted with **shellcheck**
- **bats** — Bash test framework; test files in `tests/*.bats`
- **jq** — required for bats tests

## VCS / CI
- GitHub Actions composite action (`action.yml`)
- Renovate for dependency updates (`renovate.json`)
- CI runs ruff, mypy, pytest, bats, shellcheck

## Container
- `Dockerfile` in repo root; `container-action/` for container-mode action variant
