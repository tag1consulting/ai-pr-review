# Story 4.10 — Release Announcing Python-as-Default

**Epic:** 4 — Soak, Observability, Default Flip
**Story ID:** 4-10
**Story Key:** 4-10-release-python-as-default
**GitHub Issue:** #250
**Status:** done
**PRD refs:** 4.FR-10
**Gated on:** Story 4-9 (default flip) merged and PR reviewed
**Completed:** 2026-06-02

---

## User Story

As a **release owner**, I want a v1.0.0 release published to GitHub with clear release notes announcing Python as the new default engine, so that consumers using `@main` or `@v1` references understand the change and can take action if needed.

---

## ⚠️ HOLD — DO NOT IMPLEMENT THIS SESSION

This story is **explicitly held** pending:
1. PR for S6/S8/S9 passing `/comprehensive-review` with no critical findings
2. PR reviewed and merged to main
3. Release checkpoint confirmed by Greg

**Do not tag, do not push a release, do not run `gh release create`** without the explicit checkpoint confirmation in the moment.

---

## Acceptance Criteria

- [x] `docs/features.md` has a `## What's new in v1.0.0` section with the default-flip announcement — completed by story 4-6 in the same PR; do not add again
- [ ] `pyproject.toml:7` version is `1.0.0` (already the case — verify before touching)
- [ ] A `v1.0.0` git tag is pushed to `origin`
- [ ] A GitHub release is created for `v1.0.0` with release notes covering: default flip, bash deprecation, escape hatch (`engine: bash` still works + deprecation warning), migration guidance
- [ ] The `publish-image.yml` workflow completes successfully, publishing the multi-arch container image to GHCR with tag `1.0.0`, `1.0`, `1`, and `latest`
- [ ] GHCR image is pullable: `docker pull ghcr.io/tag1consulting/ai-pr-review:1.0.0`

---

## Release Notes Template

```markdown
## v1.0.0 — Python Engine is Now the Default

### What changed

The Python engine (`AI_PR_REVIEW_ENGINE=python`) is now the default compute engine.
Consumers who do not set `engine:` in their workflow configuration will automatically
use the Python engine starting with this release.

### Migration for explicit bash users

If your workflow sets `engine: bash` or `AI_PR_REVIEW_ENGINE=bash`, it will continue
to work — but you'll see a deprecation warning in your workflow logs. The bash pipeline
is deprecated and will be removed in a future major release (Epic 5).

To migrate: remove `engine: bash` from your workflow (or change it to `engine: python`).
All Epic 3 capabilities (context enrichment, SARIF ingestion, learning loop) require
the Python engine and are unaffected.

### What's new in this release

- Default engine flipped to `python` (story 4-9 / issue #249)
- Bash deprecation warning emitted when `engine: bash` is selected explicitly (story 4-8 / issue #248)
- Documentation updated throughout to reflect Python as primary (story 4-6 / issue #246)
- 14-day field soak completed: 80 reviews, zero P0/P1 bugs (story 4-7 / issue #247)
```

---

## Implementation Tasks (when unblocked)

1. Verify `pyproject.toml:7` is `version = "1.0.0"` — do NOT change if already correct
2. Add `## What's new in v1.0.0` section to `docs/features.md` (top of file, before existing sections)
3. Tag: `git tag -s v1.0.0 -m "v1.0.0 — Python engine is now the default"`
4. Push tag: `git push origin v1.0.0` (**Checkpoint Trigger** — explicit confirmation required)
5. Create GitHub release: `gh release create v1.0.0 --title "v1.0.0 — Python Engine is Now the Default" --notes-file <release-notes-file>` (**Checkpoint Trigger**)
6. Monitor `publish-image.yml` run to completion
7. Verify GHCR: `docker pull ghcr.io/tag1consulting/ai-pr-review:1.0.0`

---

## Dev Agent Record

### Implementation Notes (2026-06-02)

Pre-release: comprehensive e2e validated all three VCS providers (GitHub PR #1, GitLab MR !34, Bitbucket PR #2) against the locally-built `:dev` container. Python engine confirmed default on all three paths.

Doc audit caught two gaps fixed in PRs #390 and #393 before tagging:
- `static-analyzers.md`, `learning-loop.md`, `getting-started.md`, `configuration.md`: stale "must set `engine: python`" prerequisite language updated to reflect python-as-default
- All user-facing "Epic 3/4/Story N" internal planning jargon replaced with feature names
- `docs/architecture.md`: updated for Python engine default

Release steps completed:
1. ✅ `pyproject.toml:7` already `version = "1.0.0"` — no change needed
2. ✅ `docs/features.md` `## What's new in v1.0.0` section added in PR #388
3. ✅ `git tag -s v1.0.0` signed tag created @ 3cb926c
4. ✅ `git push origin v1.0.0` — tag live
5. ✅ GitHub release created: https://github.com/tag1consulting/ai-pr-review/releases/tag/v1.0.0
6. ✅ `publish-image.yml` run 26853323826 — success
7. ✅ GHCR verified: `docker pull ghcr.io/tag1consulting/ai-pr-review:1.0.0` → sha256:a0d2d22c

### Change Log

- 2026-06-02: v1.0.0 released. Issues #247-#250 all closed. Epic 4 complete.
