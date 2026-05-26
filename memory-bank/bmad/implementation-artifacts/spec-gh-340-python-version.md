---
title: 'GH-340: Centralize Python version in Dockerfile via ARG'
type: 'chore'
created: '2026-05-26'
status: 'done'
baseline_commit: 'db5d13b'
context:
  - 'CLAUDE.md'
  - 'docs/ARCHITECTURE.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** The Dockerfile has hardcoded `python3.12` paths in two `COPY --from=builder` statements and two comments, tied implicitly to Ubuntu 24.04's system Python. The version is declared in three places and any future bump requires changing all of them in lockstep — easy to miss, easy to silently drift.

**Approach:** Introduce `ARG PYTHON_VERSION=3.12` in both the builder and final stages and reference `python${PYTHON_VERSION}` in the COPY paths and comments. No runtime behavior change; no Python version change; no base image change.

## Boundaries & Constraints

**Always:**
- Keep `ubuntu:24.04` as the base image (still ships Python 3.12 — `PYTHON_VERSION=3.12` stays correct).
- Keep all existing SHA256 digests for base and stage images.
- ARG must be declared in both the `builder` stage and the final stage (multi-stage Dockerfiles do not share ARG scope).
- Default value matches what Ubuntu 24.04 actually ships (`3.12`).

**Ask First:**
- Bumping Ubuntu base image (out of scope here — that is a separate change).
- Switching to `python:3.x-slim` base (rejected during planning; revisit only on user request).

**Never:**
- Do not upgrade Python version in this PR.
- Do not change `--break-system-packages` semantics.
- Do not modify the runtime behavior of any analyzer or the entrypoint.
- Do not change the `safe.directory`, `USER`, `WORKDIR`, or `ENTRYPOINT` directives.

</frozen-after-approval>

## Code Map

- `Dockerfile` -- only file changed; ARG declarations + `${PYTHON_VERSION}` substitution in builder comment, final-stage comment, and final-stage `COPY --from=builder` line for dist-packages.
- `docs/ARCHITECTURE.md` -- contains a documentation reference to `python3.12/dist-packages`; update to reflect the ARG-driven path so docs and code stay in sync.

## Tasks & Acceptance

**Execution:**
- [x] `Dockerfile` -- Add `ARG PYTHON_VERSION=3.12` to the builder stage (after the existing ARGs around line 49–58) and to the final stage (top, after `FROM`). Replace `python3.12` with `python${PYTHON_VERSION}` in the builder comment (line ~96), final-stage comment (line ~185), and the `COPY --from=builder /usr/local/lib/python3.12/dist-packages /usr/local/lib/python3.12/dist-packages` line (line ~188). Rationale: single source of truth for the Python minor version; future bump becomes a one-line change.
- [x] `docs/ARCHITECTURE.md` -- Update the line that mentions `/usr/local/lib/python3.12/dist-packages` to note that the path is parameterized by `PYTHON_VERSION` (default 3.12, matching Ubuntu 24.04). Rationale: prevent docs drift from code.

**Acceptance Criteria:**
- Given a fresh checkout, when `docker build .` is run, then the build succeeds on amd64 (CI + local) with no path errors.
- Given the built image, when `docker run --rm --entrypoint python3 <image> -c "import semgrep, checkov; print('ok')"` is run, then it prints `ok` (verifies pip-installed packages are still on the Python path).
- Given the built image, when `docker run --rm --entrypoint python3 <image> -c "import ai_pr_review; print(ai_pr_review.__file__)"` is run, then it prints a path under `/usr/local/lib/python3.12/...` (verifies the action's own engine is reachable).
- Given a future need to upgrade Python, when a developer changes only the `ARG PYTHON_VERSION=3.12` default (and the base image), then both COPY paths and the embedded comments stay consistent without further edits.
- Given `docker build` runs against this Dockerfile, when `hadolint` is invoked on it, then no new warnings are introduced relative to the baseline (existing `# hadolint ignore` directives remain valid).

## Verification

**Commands:**
- `cd /home/gchaix/repos/tag1/ai-pr-review-py-version && docker build -t ai-pr-review:gh-340 .` -- expected: build succeeds; final image tag created.
- `docker run --rm --entrypoint python3 ai-pr-review:gh-340 -c "import semgrep, checkov, ai_pr_review; print('ok')"` -- expected: prints `ok` and exits 0.
- `docker run --rm --entrypoint /usr/local/bin/semgrep ai-pr-review:gh-340 --version` -- expected: prints semgrep version, exits 0.
- `docker run --rm --entrypoint /usr/local/bin/checkov ai-pr-review:gh-340 --version` -- expected: prints checkov version, exits 0.
- `docker run --rm ai-pr-review:gh-340` -- expected: review.sh executes (will exit non-zero due to no env vars set, but it should reach review.sh, not fail on missing python paths).
- `bats tests/*.bats` -- expected: all tests pass (no regressions; tests do not depend on Dockerfile, but run as a sanity check).
- `shellcheck` not needed — no shell scripts changed.
- `hadolint Dockerfile` -- expected: no new warnings beyond existing baseline.

## Suggested Review Order

**ARG declaration & propagation**

- Builder-stage ARG — single source of truth for the Python minor version.
  [`Dockerfile:48`](../../../Dockerfile#L48)

- Final-stage ARG redeclaration — required because multi-stage Dockerfiles do not share ARG scope.
  [`Dockerfile:146`](../../../Dockerfile#L146)

- COPY paths now parameterized — the change that actually addresses the drift problem.
  [`Dockerfile:197`](../../../Dockerfile#L197)

**Comment / doc consistency**

- Builder comment updated to reference `${PYTHON_VERSION}` instead of literal 3.12.
  [`Dockerfile:101`](../../../Dockerfile#L101)

- Final-stage comment updated similarly, immediately above the parameterized COPY.
  [`Dockerfile:194`](../../../Dockerfile#L194)

- Architecture doc updated so it doesn't drift from the Dockerfile.
  [`ARCHITECTURE.md:333`](../../../docs/ARCHITECTURE.md#L333)
