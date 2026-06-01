# Bundled Analyzer License Audit — ai-pr-review

**Date:** 2026-06-01
**Auditor:** Greg (with Claude)
**Scope:** Third-party tools redistributed in the ai-pr-review container image (`Dockerfile`), plus baked data (semgrep rulesets).
**Disclaimer:** This is an engineering compliance audit, NOT legal advice. License *interpretation* (especially the copyleft "mere aggregation" question) should be confirmed by an IP attorney before the next release.

---

## Why distribution method matters

ai-pr-review ships in two forms:
1. **Composite GitHub Action** (`action.yml`) — tools are installed at runtime *by the user's runner*. The user is the one "obtaining" the tool; ai-pr-review's obligations are minimal (you're not redistributing).
2. **Container image on GHCR** (`Dockerfile`) — **you redistribute every tool as a binary/package inside the image.** This is *distribution* under copyright law and triggers each tool's redistribution obligations: attribution, license-text inclusion, and (for copyleft tools) a written offer of source or a corresponding-source link.

**The container is the compliance-relevant artifact.** Everything below assumes the container.

---

## License inventory (verified against upstream LICENSE files / docs, June 2026)

| Tool | Version | License | Class | Redistribution obligation |
|---|---|---|---|---|
| shellcheck | v0.11.0 | **GPL-3.0** | Strong copyleft | License text + source offer |
| hadolint | v2.14.0 | **GPL-3.0** | Strong copyleft | License text + source offer |
| golangci-lint | v2.12.2 | **GPL-3.0** | Strong copyleft | License text + source offer |
| trufflehog | 3.95.3 | **AGPL-3.0** | Network copyleft | License text + source offer; **network-use clause** (see risk note) |
| tflint | v0.62.1 | **MPL-2.0** (+ BUSL-1.1 on some `terraform/` files) | File-level copyleft + source-available | License text; per-file source; **BUSL use-restriction check** |
| semgrep (CLI/CE) | 1.161.0 | **LGPL-2.1** (engine) | Weak copyleft | License text + source offer for the engine |
| semgrep rulesets (p/ci, p/security-audit) | baked 2026-05-07 | **Semgrep Rules License v1.0** | Source-available, **use-restricted** | ⚠️ see critical finding |
| ruff | 0.15.15 | MIT | Permissive | Attribution (license + copyright) |
| kube-linter | v0.8.3 | Apache-2.0 | Permissive | Attribution + NOTICE if present |
| checkov | 3.2.524 | Apache-2.0 | Permissive | Attribution + NOTICE if present |
| gh CLI | 2.91.0 | MIT | Permissive | Attribution |
| phpstan | 2.1.51 | MIT | Permissive | Attribution |
| phpstan-drupal | 2.0.15 | MIT (verify) | Permissive | Attribution |
| php_codesniffer | 4.0.1 | BSD-3-Clause | Permissive | Attribution |
| drupal/coder | 9.0.0 | **GPL-2.0-or-later** | Strong copyleft | License text + source offer |
| ripgrep | (apt) | MIT / Unlicense (dual) | Permissive | Attribution |
| jq, php, python, git, bash, curl, ca-certificates | (apt) | various (MIT/BSD/GPL/LGPL) | mixed | Carried by Ubuntu base; see note |

Sources: shellcheck GPL-3 (github.com/koalaman/shellcheck/blob/master/LICENSE); hadolint GPL-3 (embeds ShellCheck); golangci-lint GPL-3 (github.com/golangci/golangci-lint/blob/master/LICENSE — verified); trufflehog AGPL-3 (github.com/trufflesecurity/trufflehog/blob/main/LICENSE — verified); tflint MPL-2.0+BUSL (github.com/terraform-linters/tflint/blob/master/LICENSE + LICENSE-BUSL); semgrep LGPL-2.1 engine / Rules License v1.0 rules (semgrep.dev/docs/licensing); ruff MIT (github.com/astral-sh/ruff/blob/main/LICENSE); kube-linter Apache-2.0 (github.com/stackrox/kube-linter/blob/main/LICENSE); checkov Apache-2.0 (github.com/bridgecrewio/checkov); gh MIT (github.com/cli/cli); phpstan MIT (verified); php_codesniffer BSD-3 (github.com/squizlabs/PHP_CodeSniffer/blob/master/licence.txt); drupal/coder GPL-2+ (drupal.org licensing policy); ripgrep MIT/Unlicense (github.com/BurntSushi/ripgrep).

---

## Findings, by severity

### 🔴 CRITICAL — Semgrep rulesets are use-restricted, not OSS
`Dockerfile` lines 114–121 bake `p/ci` and `p/security-audit` rulesets from semgrep.dev into the image. As of late 2024, **Semgrep-maintained rules are licensed under the Semgrep Rules License v1.0**, which restricts use to "internal, non-competing, non-SaaS" contexts. The engine (LGPL-2.1) is fine; **the bundled rules are not freely redistributable** and explicitly carve out competing/SaaS use.

- **Risk:** ai-pr-review is itself a code-review tool. Redistributing Semgrep's rules inside a competing review tool's image is plausibly exactly what the Rules License prohibits. This is the **single highest-risk item** in the whole audit, and it ties directly to the earlier competitive-positioning analysis (Semgrep Assistant is a listed competitor).
- **Fix options:** (a) stop baking the rules; fall back to `--config=auto` at runtime so the *user* fetches them (shifts the obligation to the user, mirrors the composite-action model); (b) switch to **Opengrep** rules or another permissively-licensed ruleset; (c) ship only your own rules. Option (a) is the smallest change — `run-semgrep.sh` already has the `--config=auto` fallback per the Dockerfile comment.

### 🔴 HIGH — No NOTICE / THIRD-PARTY-LICENSES file exists
There is **no attribution file** in the repo or image. Every license above (even MIT/BSD/Apache) requires that the license text and copyright notice travel with the redistributed binary. Apache-2.0 (kube-linter, checkov) additionally requires preserving any upstream `NOTICE` file. Shipping a container with ~17 third-party tools and zero bundled license texts is a compliance gap across **the entire dependency set**, not just the copyleft ones.

- **Fix:** add `THIRD-PARTY-LICENSES/` containing each tool's LICENSE (and NOTICE where present), and `COPY` it into the image (e.g. `/opt/ai-pr-review/THIRD-PARTY-LICENSES/`). Reference it from the README.

### 🟠 MEDIUM — GPL/AGPL tools need a written offer of source (or link)
shellcheck, hadolint, golangci-lint (GPL-3.0), drupal/coder (GPL-2.0+), and trufflehog (AGPL-3.0) are redistributed as **unmodified upstream binaries**. GPL/AGPL §6 lets you satisfy the source requirement for unmodified binaries by pointing to the upstream source (the version-pinned upstream repos). You are **not** modifying these tools, and they run as separate processes (`exec`), so the "mere aggregation" clause means **GPL does not infect ai-pr-review's MIT license** — the container is an aggregate, not a derivative work. But you still owe:
- the full license text (covered by the NOTICE fix above), and
- a corresponding-source pointer for each (the pinned upstream URL+version suffices for unmodified binaries).

- **Confirm with counsel:** the "mere aggregation / separate process" reasoning is the standard FSF position and almost certainly correct here, but it's the one legal conclusion worth a lawyer's sign-off because it's load-bearing for keeping ai-pr-review MIT.

### 🟠 MEDIUM — TruffleHog AGPL-3.0 network clause
AGPL's §13 obligates providing source to *users who interact with the software over a network*. ai-pr-review runs trufflehog as a **local subprocess in CI** — users don't interact with it over a network — so §13 most likely does not trigger. As an unmodified binary run via `exec`, the same aggregation reasoning applies. **But AGPL is the most aggressive license in the set**, and if anyone ever wraps ai-pr-review behind a hosted/SaaS endpoint (one of the opportunities flagged in the market research!), the AGPL analysis changes materially. Flag for the commercialization decision.

### 🟡 LOW — tflint BUSL-1.1 use restriction
Most of tflint is MPL-2.0 (fine), but some `terraform/` package files are **BUSL-1.1** (Business Source License — source-available, use-restricted, converts to open after a grace period). BUSL typically restricts *production/competing use of the licensed work itself*, not tools that merely invoke it. Running the released tflint binary for linting is within normal use. Low risk, but include the LICENSE-BUSL text in attribution and don't represent tflint as fully OSS.

### 🟡 LOW — Ubuntu base-image packages
The apt-installed packages (php, python, git, jq, etc.) include GPL/LGPL components but are carried by the `ubuntu:24.04` base layer under Canonical's standard distribution terms. This is conventional and low-risk; standard practice is to note "built on Ubuntu 24.04; base-image package licenses apply."

---

## Bottom line

**None of these tools are unredistributable, and nothing here is reverse-engineering exposure** — they're all published OSS you're entitled to bundle. The gaps are *compliance hygiene*, and they're very fixable:

1. **Stop baking Semgrep's use-restricted rulesets** (highest risk; smallest fix — use the existing `--config=auto` fallback). 🔴
2. **Add a `THIRD-PARTY-LICENSES/` directory** with every tool's license text + Apache NOTICE files, copied into the image and referenced in the README. 🔴
3. **Add corresponding-source pointers** (pinned upstream URLs) for the GPL/AGPL/GPL-2 tools — the Dockerfile already pins exact versions, so this is mechanical. 🟠
4. **Get counsel to confirm** the "separate-process aggregation keeps ai-pr-review MIT" conclusion and to review the AGPL/Semgrep-Rules items before any hosted/SaaS offering. 🟠

Items 1–3 are work I can do in-repo now; item 4 is the human/legal gate.
