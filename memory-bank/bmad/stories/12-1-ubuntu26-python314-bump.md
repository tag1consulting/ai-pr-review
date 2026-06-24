# Story 12.1: Bump Container Base to Ubuntu 26.04 / Python 3.14

**Epic:** 12 — Container Maintenance
**Story ID:** 12-1
**Story Key:** 12-1-ubuntu26-python314-bump
**GitHub Issue:** #349
**Status:** ready-for-dev

---

## Story

As a **maintainer**,
I want the container base image bumped from `ubuntu:24.04` (Python 3.12) to `ubuntu:26.04` (Python 3.14),
so that the action runs on the current LTS with two Python releases worth of perf and security improvements, and stays aligned with the CI ai-review workflow that already uses Python 3.14 for `mypy`/`ruff`/`pytest` setup.

---

## Acceptance Criteria

1. `Dockerfile` builder + final stages use `FROM ubuntu:26.04@sha256:53958ec7b67c2c9355df922dd08dbf0360611f8c3cdb656875e81873db9ffdba` (digest verified 2026-06-24 from `library/ubuntu:26.04` on Docker Hub).
2. Both `ARG PYTHON_VERSION=3.12` defaults change to `ARG PYTHON_VERSION=3.14`.
3. The "Ubuntu 24.04 ships Python 3.12" comment in the builder stage is updated to "Ubuntu 26.04 ships Python 3.14".
4. `THIRD-PARTY-LICENSES/NOTICE.md` line 70 (`built FROM ubuntu:24.04`) updates to `ubuntu:26.04`.
5. `docs/architecture-internals.md` line 275 (parenthetical `default 3.12 to match Ubuntu 24.04`) updates to `default 3.14 to match Ubuntu 26.04`.
6. `.github/workflows/lint.yml` matrix changes from `["3.12"]` to `["3.14"]` so CI's `mypy` + `ruff` + `pytest` run on the same Python the container ships. `[tool.ruff] target-version = "py311"` and `[tool.mypy] python_version = "3.11"` stay unchanged (the floor declared by `requires-python = ">=3.11"`).
7. The image builds locally for the linux/amd64 platform (multi-arch verification deferred to publish-image.yml CI).
8. `python3 -m ai_pr_review --help` runs successfully inside the built image.
9. End-to-end test: `Workflow({name: 'ai-pr-review-e2e', args: {mode: 'quick', platforms: ['github']}})` passes against the GitHub test PR.
10. `hadolint` is clean against the new Dockerfile (the existing CI gate enforces this).

---

## Out of Scope

- Multi-arch validation beyond what `publish-image.yml` already does on tag push.
- Bumping `requires-python` floor or `tool.ruff.target-version` — these stay at 3.11 to keep the package installable for downstream consumers on older Pythons.
- Bumping `mypy.python_version` to 3.14 — that flag tells mypy what features to allow, not what runtime to target; the floor stays 3.11.
- Refreshing apt-pinned package versions in the Dockerfile — `apt-get install` doesn't pin, and this bump deliberately picks up whatever 26.04 ships.

---

## Technical Notes

### Verified facts (2026-06-24)

- `ubuntu:26.04` digest: `sha256:53958ec7b67c2c9355df922dd08dbf0360611f8c3cdb656875e81873db9ffdba` (last updated 2026-06-19, multi-arch).
- Confirmed via `docker run` that the image ships Python 3.14.4 (one patch newer than the 3.14.3 the issue projected).
- `pip3` from `python3-pip` resolves to pip 25.1.1, supports `--break-system-packages` (PEP 668 is unchanged in 26.04).
- Issue #348 (the `ARG PYTHON_VERSION` enabling refactor) merged at `00874fc` on 2026-05-26 — the Dockerfile already has both `ARG PYTHON_VERSION` declarations wired through the COPY paths, so only the defaults need to change.

### Issue body inaccuracy to disregard

The issue says "CI matrix already covers 3.11–3.14". It does not. `lint.yml` only tests on 3.12. Fixing that gap is part of this story (AC#6) so the runtime and CI match.

The issue also lists `bats tests/*.bats` in the smoke test — that is stale; Epic 5 deleted bash entirely. Use the Python suite + e2e workflow instead.

### Risk surface

- New apt-installed package versions (`php-cli`, `php-xml`, `php-mbstring`, `git`, `ripgrep`, `curl`, `ca-certificates`). The Python engine doesn't shell out to these aside from `git` (subprocess.run for `git log` / `git rev-parse` in preflight); analyzers like phpcs/phpstan use the bundled `php-cli` only as the interpreter, not for behavior changes. Low risk.
- New glibc / openssl: `httpx` does its own TLS via the system OpenSSL; verify e2e LLM calls succeed in the built image.
- The `pip install --break-system-packages "/opt/ai-pr-review[context]"` line installs `tree-sitter-language-pack` which has compiled C extensions — those need to build for Python 3.14 ABI. `tree-sitter-language-pack >= 0.7.0` (declared in pyproject.toml) ships 3.14 wheels per its release history; if a wheel isn't available the build will fall back to a sdist compile, slower but functional. Verify by inspecting the build log.

---

## Tasks

- [ ] Update `Dockerfile`: two `FROM ubuntu:24.04@...` → `ubuntu:26.04@sha256:53958ec...`; two `ARG PYTHON_VERSION=3.12` → `=3.14`; update the "Ubuntu 24.04 ships Python 3.12" comment.
- [ ] Update `THIRD-PARTY-LICENSES/NOTICE.md`: `ubuntu:24.04` → `ubuntu:26.04`.
- [ ] Update `docs/architecture-internals.md`: parenthetical default 3.12/24.04 → 3.14/26.04.
- [ ] Update `.github/workflows/lint.yml`: matrix `["3.12"]` → `["3.14"]`; conditional `if: matrix.python-version == '3.12'` → `'3.14'`.
- [ ] Build the image locally; spot-check `python3 --version`, `python3 -m ai_pr_review --help`, and that all analyzer binaries are present (`shellcheck`, `trufflehog`, `golangci-lint`, `hadolint`, `kube-linter`, `tflint`, `ruff`, `semgrep`, `checkov`, `phpcs`, `phpstan`, `gh`, `git`).
- [ ] Run e2e workflow against GitHub test PR.
- [ ] Open PR; let `hadolint` SARIF gate validate the Dockerfile.

---

## Dev Notes

- `memory-bank/license-audit-2026-06-01.md` line 73 also references `ubuntu:24.04` — that's a dated audit artifact, not a live doc; leave it (the audit was done against 24.04, that's still the truth as of when it was written).
- `prompts/_knowledge-cutoff.md` mentions Python 3.14 only as a counter-example for the model's training cutoff confusion — leave it.
- The historical `memory-bank/bmad/implementation-artifacts/spec-gh-340-python-version.md` is a frozen spec for the prior story; do not retroactively update it.
