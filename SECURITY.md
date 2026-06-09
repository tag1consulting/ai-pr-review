# Security Policy

## Supported versions

| Version | Supported |
|---------|-----------|
| v1.4.x (latest) | ✅ |
| v1.3.x          | ✅ |
| < v1.3.0        | ❌ |

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

The container image entrypoint is pinned (`/opt/ai-pr-review/review.sh`). All `source` calls inside the image resolve to `${SCRIPT_DIR}/lib/...` paths inside the image, never `/workspace`. Static analyzers bundled in the image (semgrep, ruff, golangci-lint, shellcheck, trufflehog, phpstan, etc.) are invoked against repository **content** as data; their configuration is the analyzer's own bundled defaults plus opt-in repo-level config files where the analyzer treats the file as data (e.g., `.semgrepignore`).

### Why this matters

This invariant is the load-bearing safety control for consumers that use the action under `pull_request_target` (so the job has repository secrets in scope) while also checking out the PR head SHA. Examples include:

- [tag1consulting/pulumi-lagoon-provider#222](https://github.com/tag1consulting/pulumi-lagoon-provider/issues/222) — `pull_request_target` + checkout of fork PR head with `AI_REVIEW_API_KEY` and a write-scoped token in scope.

If the action ever executed working-tree content under `pull_request_target`, a fork PR author could exfiltrate those secrets. Several Tag1 consumer repos intentionally reference `tag1consulting/ai-pr-review/container-action@main` (a mutable ref) as a dogfooding/canary policy. That choice is only safe **because** this invariant holds and is enforced here.

### Regression guard

A canary fixture/test enforcing this invariant at CI time is tracked in [#494](https://github.com/tag1consulting/ai-pr-review/issues/494). Until that lands, code review on this repo treats any new exec-of-workspace-content code path as a security change requiring the private vulnerability reporting flow.
