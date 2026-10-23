# Security Policy

## Supported versions

This project ships one rolling container image (`:latest` for the current tagged release, `:dev` for pre-release builds) rather than maintaining multiple parallel version branches. Only the latest tagged release is supported for security fixes; upgrade to `:latest` before reporting an issue that may already be fixed.

## Reporting a vulnerability

**Please do not file public GitHub issues for security vulnerabilities.**

Report vulnerabilities privately using [GitHub's private vulnerability reporting](https://github.com/tag1consulting/ai-pr-review/security/advisories/new), or email [security@tag1consulting.com](mailto:security@tag1consulting.com).

Include:
- A description of the vulnerability and its potential impact
- Steps to reproduce or a proof-of-concept
- The version(s) affected

We aim to acknowledge reports within 2 business days and provide a remediation timeline within 7 business days.

## Scope

This policy covers the `ai-pr-review` action itself. It does **not** cover:
- Third-party LLM providers (Anthropic, OpenAI, Google, AWS Bedrock)
- Third-party static analyzers bundled in the container image (semgrep, trufflehog, etc.)
- The consuming repository's own workflow configuration

## Security invariants

This action makes the following guarantees. Any change that breaks one of them is a security regression and must be tracked through the private vulnerability reporting process above.

### The action treats the checked-out repository as untrusted data, never as code.

The action reads file contents, paths, and git metadata from `$GITHUB_WORKSPACE` for diffing and analysis. It does not, at any point:

- run `make`, `npm install`, `npm ci`, `pip install`, `go build`, `bundle install`, or any other build/install step against the checked-out tree;
- execute `setup.py`, `Makefile`, `package.json` scripts, pre-commit hooks, or any other entry point shipped by the repository under review;
- `source` shell files from `$GITHUB_WORKSPACE`, run interpreters on checked-out files, or invoke binaries from the checked-out tree;
- run linters/formatters/test runners that auto-discover and execute project plugins or fixtures from `$GITHUB_WORKSPACE`.

The container image entrypoint invokes the Python engine (`python3 -m ai_pr_review review`) directly. The engine starts in the checked-out tree, so the image, the container action and the root action set `PYTHONSAFEPATH=1`. This keeps the working directory off `sys.path`, so a file in the repository under review (for example `ai_pr_review/__main__.py` or `yaml.py`) is never imported in place of the installed module. If you run `python3 -m ai_pr_review review` yourself from a checked-out tree (for example in a GitLab or Bitbucket pipeline), set `PYTHONSAFEPATH=1` first. `tests/python/test_security_canary.py` starts the real entrypoint in a hostile workspace to guard this. Static analyzers bundled in the image (semgrep, ruff, golangci-lint, shellcheck, trufflehog, phpstan, etc.) are invoked against repository **content** as data; their configuration is the analyzer's own bundled defaults plus opt-in repo-level config files where the analyzer treats the file as data (e.g., `.semgrepignore`). The phpcs analyzer reads a repo ruleset (`.phpcs.xml`, `phpcs.xml`, `.phpcs.xml.dist` or `phpcs.xml.dist`) as data only and gives phpcs a generated ruleset, because a ruleset can load PHP from the workspace (`installed_paths`, `bootstrap`, `autoload`, path-style `rule ref`). phpcs also runs from an empty directory with absolute file paths, because it resolves a rule ref or an exclude name that is not a sniff code against its working directory and loads the result as a ruleset. `tests/security/canary-workspace/phpcs.xml.dist` guards this.

### Why this matters

This invariant is the load-bearing safety control for consumers that use the action under `pull_request_target` (so the job has repository secrets in scope) while also checking out the PR head SHA. Examples include:

- [tag1consulting/pulumi-lagoon-provider#222](https://github.com/tag1consulting/pulumi-lagoon-provider/issues/222): `pull_request_target` + checkout of fork PR head with `AI_REVIEW_API_KEY` and a write-scoped token in scope.

If the action ever executed working-tree content under `pull_request_target`, a fork PR author could exfiltrate those secrets. Several Tag1 consumer repos intentionally reference `tag1consulting/ai-pr-review/container-action@main` (a mutable ref) as a dogfooding/canary policy. That choice is only safe **because** this invariant holds and is enforced here.

### Fork pull requests

The default policy is `AI_FORK_POLICY=auto`. A fork pull request gets a review at once, with no maintainer step, and the engine contains the run from the inside. See [docs/fork-prs.md](docs/fork-prs.md) for the settings. [ADR 0006](docs/adr/0006-fork-prs-reviewed-in-one-deprivileged-job.md) records the decision.

An attacker is anyone who can open a fork pull request. They control the diff, the whole tree, the history, the pull request text, the commit messages and the branch name. They can start a run. They cannot change workflow files on the base branch, labels or comments of other users. The assets are the LLM keys, `GH_TOKEN` with the write-scoped Actions token, the LLM budget, the runner and the pull request surface (comments, reviews and check runs).

The controls for a non-trusted author are these:

- **A scrubbed environment.** Every analyzer and git subprocess gets an allowlist-only environment. No LLM key, `GH_TOKEN`, `GITHUB_TOKEN` or `ACTIONS_*` value reaches it. This applies to every run.
- **A fork-safe analyzer set.** Only analyzers with a trusted or isolated configuration run. Suppression rules come from the base branch.
- **A credential check.** The review pauses when `.git/config` holds a persisted credential.
- **Dropped and neutralized text.** The prompts carry no PR body, commit message, open-issue title or branch name. The engine strips invisible characters and escapes closing tags in untrusted embeds on every run.
- **A clamped output.** The review event is COMMENT, suggestions are off, a fixed banner starts each comment, and the text has no external link or image. Mentions and cross references are neutralized on a fork run only. On every run, the engine scrubs secrets from every outbound string, and unreadable agent output forces COMMENT.
- **Bot-authored state only.** The engine reads the watermark, skip comments, ledgers and spend entries only from comments that the bot wrote.
- **Caps that fail closed.** There is a cap per run, per pull request, per author and per day. An unreadable fact, ledger or cost estimate pauses the run.

A fork pull request by a trusted author (`OWNER` and `COLLABORATOR` by default) gets the normal pipeline and the per-run cap. The `label` policy is the stricter option: the engine pauses a fork pull request until a maintainer approves one exact head commit. Use `block` to switch fork review off.

These limits remain:

- **Prompt injection is not preventable.** It can make the review say nothing, or something misleading, about the fork's own pull request. The output clamp limits what a misled review can do.
- **The review runs in the same job as the secrets.** The environment scrub covers subprocesses. A flaw in the engine process itself is not contained. [ADR 0006](docs/adr/0006-fork-prs-reviewed-in-one-deprivileged-job.md) and issue #1098 track the split into two jobs.
- **The ledgers are best effort.** A user with write access can delete or edit a ledger comment and so reset a cap. Another workflow that runs as the same bot login can write a valid-looking entry.
- **Many accounts can use up the daily pool.** Fork reviews then pause until the window moves on. Trusted pull requests are not affected.
- **A trusted author can carry untrusted commits.** The engine trusts the author of the pull request and not each commit.
- **`COLLABORATOR` can include low-privilege accounts.** Check who has that association, or remove it from `AI_FORK_TRUSTED_ASSOCIATIONS`.
- **A persisted checkout credential pauses a fork run.** The `actions/checkout` step must set `persist-credentials: false`. The credential check does not cover `core.sshCommand`, `core.askPass`, `http.cookieFile`, `http.proxy` with a user name or `config.worktree`.
- **Forks get fewer analyzers.** eslint, golangci-lint, semgrep, shellcheck, hadolint and kube-linter do not run on a fork until each one has a trusted configuration.
- **dep-exists and docs-drift-check read files from the fork tree.** A fork can silence findings about unknown packages for its own pull request and can hide references to a deleted file. ruff and trufflehog still honor inline suppressions, and trufflehog follows symlinks in changed files.
- **The engine reads SARIF files from the fork tree** (`AI_SARIF_PATHS`). `AI_POLICY_SOURCE=workspace` also reads `policy.yml` from the fork tree.
- **The gate applies to GitHub only.** GitLab and Bitbucket runs are not gated.
- **Other workflow steps are not covered.** Every other step in your workflow runs on every fork push. Keep them free of secrets.

A custom bot identity must set `GITHUB_BOT_USERNAME`. The engine ignores summary, skip and ledger comments from any other login.

### Regression guard

A canary fixture lives in `tests/security/canary-workspace/` and is enforced by `tests/python/test_security_canary.py` (marked `@pytest.mark.security`) on every PR via the `Lint & Test` CI job. A failure in those tests is a security regression; see CONTRIBUTING.md for the reporting process. Any new code path that reads from `$GITHUB_WORKSPACE` must be verified against the canary tests before merging.
