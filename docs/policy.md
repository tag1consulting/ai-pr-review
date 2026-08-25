---
layout: default
title: Policies
nav_order: 4.5
---

# Policies

`.github/ai-pr-review/policy.yml` lets a repo route review depth (which agents/analyzers run, quick vs. full mode) by changed-file path, base branch, or head branch — without hand-rolling a GitHub Actions expression per repo, and without hard-coding a single global choice for every PR.

This solves the common case of mixed PR traffic: a repo that merges content-only changes straight to `main` but collects feature work onto a `staging` branch, tested with a full review once per batch rather than on every push.

A ready-to-copy starting point lives at [`examples/policy.yml.example`](https://github.com/tag1consulting/ai-pr-review/blob/main/examples/policy.yml.example) — copy it to `.github/ai-pr-review/policy.yml` and edit to taste.

## Example

```yaml
# .github/ai-pr-review/policy.yml
version: 1

policies:
  content:                       # near-zero cost: content-only changes
    agents: []
    analyzers: []
  feature:                       # default: today's quick mode
    extends: quick
  integration:                   # staging smoke test
    extends: quick
    agents: [code-reviewer, silent-failure-hunter, edge-case-hunter]
  deep:
    extends: full                # today's full mode, unchanged

routes:
  - when: {paths: ['src/**', '_data/**']}
    policy: content
  - when: {base-branch: 'staging-*'}
    policy: integration
  - when: {head-branch: 'feature/*'}
    policy: feature
default: feature
```

- **`policies`** — named policies. `extends` is either a built-in base (`quick` or `full` — reproducing today's roster exactly) or another policy defined in this file. `agents` / `exclude-agents` / `analyzers` / `exclude-analyzers` override the extended base's value for that field only; an unset field inherits from `extends`. Omitting `extends` implies `quick`.
- **`routes`** — an ordered list; the first route whose `when` matches the PR wins. `when.paths` matches if *any* changed file matches *any* glob (standard shell-style globs, e.g. `src/**`, `*.md`). `when.base-branch` / `when.head-branch` match a single glob against the PR's base/head branch name. A route must constrain at least one of the three — an unconstrained route would silently match every PR and shadow everything after it, so the file is rejected if one is found.
- **`default`** — the policy used when no route matches. Omit it to fall back to the engine's hard-coded default (`quick`, all agents/analyzers eligible) when nothing matches.

## Precedence

Highest to lowest — each level only applies when the level above it left a field unset:

1. **Slash command** — `/ai-pr-review review-full` always runs full mode; `/ai-pr-review rescan` uses the `AI_REVIEW_MODE_DEFAULT` repo variable if set.
2. **Explicit action input / repo variable** — the `ai-review-full` PR label, the `review-mode` input set to a real value, or `agents`/`analyzers`/etc. set to a non-empty value (via `vars.AI_REVIEW_*` in the shipped template).
3. **`policy.yml` route match** — this page.
4. **Engine default** — today's hard-coded behavior (`quick` mode, no agent/analyzer restriction). Used verbatim when no `policy.yml` exists, so **adopting this feature is opt-in and behavior-neutral until you add the file**.

A repo with no `policy.yml` sees no change at all. A repo that already sets `review-mode` (or `agents`, etc.) to an explicit value in its own workflow keeps that value regardless of any policy file — level 2 always wins over level 3.

If your existing workflow hardcodes a fallback like `... || 'quick'` at the end of a `review-mode:` expression, that hardcoded value is itself an explicit override and will always win over `policy.yml` — replace the trailing `'quick'` with `''` (or remove the branch-name special case entirely) if you want that repo to defer to policy routing. The shipped [`examples/workflows/pr-review.yml`](https://github.com/tag1consulting/ai-pr-review/blob/main/examples/workflows/pr-review.yml) template does this already.

## Security: loaded from the base ref, never the PR head

The policy file is read via `git show origin/{base-ref}:.github/ai-pr-review/policy.yml` — **never from the checked-out PR branch's working tree**. A PR that edits its own `policy.yml` to weaken its own review (e.g. dropping `security-reviewer`) has no effect; the file that governs a PR's review is whatever is committed on the *target* branch at review time. Merge a policy.yml change to your base branch, and it applies to every subsequent PR review from that point on — including the PR that introduced it, on its *next* run, once that PR's target branch state (not the PR's own branch) has the file.

A malformed or invalid `policy.yml` (bad YAML, an unknown agent/analyzer name, a cyclic `extends` chain, an unconstrained route) never blocks a review — it prints one warning to the run log and the review proceeds using the engine's hard-coded defaults, as if no policy file existed.

## Release-branch escalation (replacing a hand-rolled workflow expression)

Earlier versions of the shipped template auto-selected full mode for `release/*` branches via a hardcoded `startsWith()` expression. Express the same thing as a policy route instead:

```yaml
policies:
  release:
    extends: full
routes:
  - when: {head-branch: 'release/*'}
    policy: release
default: feature
```

This generalizes cleanly to any branch convention (`hotfix/*`, merges into a specific base branch, etc.) without editing the workflow file again.

## Requiring `head-ref`

Route matching on `when.head-branch` requires the action to know the PR's head branch name. The `head-ref` action input (optional, defaults to `''`) carries this — the shipped templates for GitHub, GitLab, and Bitbucket all wire it. If you're on an older copy of a template that predates `head-ref`, add it (see [Configuration](configuration.md)) before using `head-branch` routes; `paths` and `base-branch` routes work without it.

## Requiring a review tier before merge

A route can name a policy that must have (at least) run before merge, via `require`. This is a **manual-trigger merge gate**: the automatic push doesn't need to run that tier itself, but a required status check on the target branch blocks merge until *some* run — automatic or `/ai-pr-review review-full` — satisfies it.

```yaml
policies:
  integration:
    extends: quick
  deep:
    extends: full

routes:
  - when: {base-branch: 'staging-*'}
    policy: integration
    require: deep
```

Every automatic push to a PR targeting `staging-*` runs the (cheap) `integration` tier and posts a GitHub check run named `ai-pr-review/policy-gate`:
- **`neutral`** if only `integration` has run so far — the check summary tells the reviewer to comment `/ai-pr-review review-full` (or add the `ai-review-full` label) to satisfy the requirement. `neutral`, not `failure`, because an unmet requirement on an ordinary automatic push is not itself a defect — it's an unactioned manual step.
- **`success`** once a run at the required tier (or full mode, which satisfies any requirement) has completed for the current commit — including a later `/ai-pr-review review-full` run, which re-posts the check for the same SHA and GitHub re-evaluates branch protection automatically.

To make this a real merge gate, add branch protection on the target branch requiring the `ai-pr-review/policy-gate` check (**Settings → Branches → Branch protection rules**). The shipped GitHub templates grant the `checks: write` permission needed to post it.

**GitHub only for now.** No GitLab/Bitbucket equivalent is wired yet — `require` is silently a no-op (logged at `info` level) on those providers; routing (`policies`/`routes` without `require`) works identically everywhere.
