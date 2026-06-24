# Third-Party Licenses and Attribution

The ai-pr-review **container image** (`ghcr.io/tag1consulting/...`) redistributes the
third-party open-source tools listed below. ai-pr-review itself is MIT-licensed (see
the repository-root `LICENSE`); each bundled tool retains its own license, reproduced
in this directory.

Tools run as **separate processes** (invoked via `exec` from the analyzer wrapper
scripts in `analyzers/`). They are bundled as **unmodified upstream binaries/packages**;
ai-pr-review does not modify, link against, or derive from their source. Under the
"mere aggregation" provisions of the GPL/LGPL/AGPL, aggregating these separate programs
in one image does not place ai-pr-review under their copyleft terms.

For each copyleft tool (GPL, LGPL, AGPL, MPL), the **corresponding source** is the
unmodified upstream release at the exact version pinned in the `Dockerfile`; the source
URL is listed below and constitutes the written offer of source required by those
licenses for redistribution of unmodified binaries.

This file is generated/maintained manually. When the `Dockerfile` tool versions change,
update this manifest. See `memory-bank/license-audit-2026-06-01.md` for the full audit.

> **Note:** This directory documents license obligations as an engineering best-effort.
> It is not legal advice. The aggregation reasoning above reflects the standard FSF
> position but should be confirmed by counsel before a release, and re-reviewed before
> any hosted/SaaS offering (which changes the AGPL analysis for trufflehog).

---

## Bundled tools

| Tool | Version | License | Copyright holder | Source (corresponding source for copyleft) | License file |
|------|---------|---------|------------------|---------------------------------------------|--------------|
| shellcheck | v0.11.0 | GPL-3.0 | Vidar Holen & contributors | https://github.com/koalaman/shellcheck/releases/tag/v0.11.0 | `shellcheck.LICENSE.txt` |
| hadolint | v2.14.0 | GPL-3.0 | Lukas Martinelli, Jakub Kozłowski & contributors | https://github.com/hadolint/hadolint/releases/tag/v2.14.0 | `hadolint.LICENSE.txt` |
| golangci-lint | v2.12.2 | GPL-3.0 | golangci & contributors | https://github.com/golangci/golangci-lint/releases/tag/v2.12.2 | `golangci-lint.LICENSE.txt` |
| trufflehog | 3.95.3 | AGPL-3.0 | Truffle Security Co. & contributors | https://github.com/trufflesecurity/trufflehog/releases/tag/v3.95.3 | `trufflehog.LICENSE.txt` |
| tflint | v0.62.1 | MPL-2.0 (+ BUSL-1.1 on some `terraform/` files) | Kazuki Higashiguchi & contributors; HashiCorp (BUSL files) | https://github.com/terraform-linters/tflint/releases/tag/v0.62.1 | `tflint.LICENSE.txt`, `tflint.LICENSE-BUSL.txt` |
| semgrep (engine / CE) | 1.161.0 | LGPL-2.1 | Semgrep, Inc. & contributors | https://github.com/semgrep/semgrep/releases/tag/v1.161.0 | `semgrep.LICENSE.txt` |
| ruff | 0.15.15 | MIT | Astral Software Inc. & contributors | https://github.com/astral-sh/ruff/releases/tag/0.15.15 | `ruff.LICENSE.txt` |
| kube-linter | v0.8.3 | Apache-2.0 | StackRox / Red Hat & contributors | https://github.com/stackrox/kube-linter/releases/tag/v0.8.3 | `kube-linter.LICENSE.txt` |
| checkov | 3.2.524 | Apache-2.0 | Bridgecrew / Palo Alto Networks & contributors | https://github.com/bridgecrewio/checkov/releases/tag/3.2.524 | `checkov.LICENSE.txt` |
| gh CLI | 2.91.0 | MIT | GitHub, Inc. | https://github.com/cli/cli/releases/tag/v2.91.0 | `gh-cli.LICENSE.txt` |
| phpstan/phpstan | 2.1.51 | MIT | Ondřej Mirtes / PHPStan s.r.o. | https://github.com/phpstan/phpstan/releases/tag/2.1.51 | `phpstan.LICENSE.txt` |
| mglaman/phpstan-drupal | 2.0.15 | MIT | Matt Glaman & contributors | https://github.com/mglaman/phpstan-drupal/releases/tag/2.0.15 | `phpstan-drupal.LICENSE.txt` |
| squizlabs/php_codesniffer | 4.0.1 | BSD-3-Clause | Squiz Pty Ltd; PHPCSStandards | https://github.com/PHPCSStandards/PHP_CodeSniffer/releases/tag/4.0.1 | `php_codesniffer.LICENSE.txt` |
| drupal/coder | 9.0.0 | GPL-2.0-or-later | Drupal Coder maintainers & contributors | https://www.drupal.org/project/coder/releases/9.0.0 | `GPL-2.0.txt` |
| ripgrep | (Ubuntu apt) | MIT / Unlicense (dual) | Andrew Gallant & contributors | https://github.com/BurntSushi/ripgrep | `ripgrep.LICENSE-MIT.txt` |

### Notes per tool

- **semgrep registry rulesets are NOT bundled.** The Semgrep-maintained rules (`p/ci`,
  `p/security-audit`, etc.) are licensed under the **Semgrep Rules License v1.0**, which
  restricts use to internal/non-competing/non-SaaS contexts and is not freely
  redistributable. They are intentionally excluded from the image; `run-semgrep.sh`
  uses `--config=auto` to fetch rules at runtime instead. Only the LGPL-2.1 engine is
  bundled.
- **checkov / kube-linter** have no upstream `NOTICE` file as of the pinned versions, so
  none is reproduced here. If a future version adds one, copy it alongside the LICENSE.
- **tflint** is primarily MPL-2.0; a subset of files under its `terraform/` package are
  **BUSL-1.1** (source-available, use-restricted, time-converting to open). The released
  binary is bundled and used for its normal linting purpose; both license texts are
  included. Do not represent tflint as fully OSS.
- **trufflehog (AGPL-3.0)** runs as a local CI subprocess; users do not interact with it
  over a network, so AGPL §13 (network source-provision) is not triggered in this usage.
  **This analysis changes if ai-pr-review is ever offered as a hosted/network service** —
  re-review before any such offering.

### Base image

The image is built `FROM ubuntu:26.04`. Apt-installed runtime packages (`bash`,
`ca-certificates`, `curl`, `git`, `jq`, `php-cli` + extensions, `python3`, `ripgrep`)
are carried under Canonical's Ubuntu distribution terms; each package retains its own
upstream license (a mix of MIT/BSD/GPL/LGPL). These are standard base-distribution
components.
