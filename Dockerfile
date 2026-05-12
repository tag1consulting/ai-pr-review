# syntax=docker/dockerfile:1.23@sha256:2780b5c3bab67f1f76c781860de469442999ed1a0d7992a5efdf2cffc0e3d769
#
# Multi-stage build.
#
# Binary tools with official images are pulled directly via COPY --from=<image>
# rather than curl+sha256+unzip. Docker resolves the correct arch-specific
# layer automatically; no per-arch SHA bookkeeping required.
#
# Tools without official images (gh CLI) or that are Python/PHP packages
# (semgrep, checkov, phpcs, phpstan) still use the builder stage.
#
# Stage layout:
#   shellcheck, trufflehog, golangci-lint, hadolint, kube-linter, tflint,
#   ruff          — named single-binary stages; COPY --from pulls the binary
#   builder       — pip (semgrep, checkov) + composer (phpcs, phpstan) +
#                   gh CLI (no official image) + semgrep ruleset download
#   final         — slim runtime; assembles binaries from all prior stages

# ==============================================================================
# Single-binary tool stages
# ==============================================================================

# hadolint ignore=DL3029
FROM koalaman/shellcheck:v0.11.0 AS shellcheck
# hadolint ignore=DL3029
FROM trufflesecurity/trufflehog:3.95.2 AS trufflehog
# hadolint ignore=DL3029
FROM golangci/golangci-lint:v2.11.4 AS golangci-lint
# hadolint ignore=DL3029
FROM hadolint/hadolint:v2.14.0 AS hadolint
# hadolint ignore=DL3029
FROM ghcr.io/stackrox/kube-linter:v0.8.3 AS kube-linter
# hadolint ignore=DL3029
FROM ghcr.io/terraform-linters/tflint:v0.62.0 AS tflint
# hadolint ignore=DL3029
FROM ghcr.io/astral-sh/ruff:0.15.11 AS ruff

# ==============================================================================
# Builder stage — pip packages, composer packages, gh CLI, semgrep rulesets
# ==============================================================================
FROM ubuntu:24.04@sha256:c4a8d5503dfb2a3eb8ab5f807da5bc69a85730fb49b5cfca2330194ebcc41c7b AS builder

ARG TARGETARCH

ARG GH_VERSION=2.91.0
ARG GH_SHA256_AMD64=304a0d2460f4a8847d2f192bad4e2a32cd9420d28716e7ae32198181b65b5f9c
ARG GH_SHA256_ARM64=ccbed39c472d3dc1c501d1e164a9cffd934c5f6fce1012811a1a59d24cb7d7c6

ARG SEMGREP_VERSION=1.161.0
ARG CHECKOV_VERSION=3.2.524

# Cache-buster for semgrep ruleset downloads; bump to force fresh pull.
ARG SEMGREP_RULESET_DATE=2026-05-07

ARG PHPCS_VERSION=4.0.1
ARG DRUPAL_CODER_VERSION=9.0.0
ARG PHPSTAN_VERSION=2.1.51
ARG PHPSTAN_DRUPAL_VERSION=2.0.15

ENV DEBIAN_FRONTEND=noninteractive

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# hadolint ignore=DL3008
RUN apt-get update -qq && \
    apt-get install -y -qq --no-install-recommends \
      ca-certificates \
      curl \
      git \
      php-cli \
      php-xml \
      php-mbstring \
      php-zip \
      python3 \
      python3-pip \
      unzip \
    && rm -rf /var/lib/apt/lists/*

# gh CLI — no official Docker image; curl install retained
RUN case "${TARGETARCH}" in \
      amd64) SHA="${GH_SHA256_AMD64}" ;; \
      arm64) SHA="${GH_SHA256_ARM64}" ;; \
      *) echo "unsupported TARGETARCH: ${TARGETARCH}" >&2; exit 1 ;; \
    esac && \
    curl -fsSL -o /tmp/gh.tar.gz \
      "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_${TARGETARCH}.tar.gz" && \
    echo "${SHA}  /tmp/gh.tar.gz" | sha256sum -c - && \
    tar -xz --strip-components=2 -C /usr/local/bin -f /tmp/gh.tar.gz \
        "gh_${GH_VERSION}_linux_${TARGETARCH}/bin/gh" && \
    chmod +x /usr/local/bin/gh && \
    rm /tmp/gh.tar.gz

# semgrep and checkov via pip.
# --break-system-packages required on Ubuntu 24.04 (PEP 668). Installs land in
# /usr/local/lib/python3.12/dist-packages plus /usr/local/bin entry points; both
# are COPYied into the final stage below.
RUN pip3 install --no-cache-dir --break-system-packages \
      "semgrep==${SEMGREP_VERSION}" \
      "checkov==${CHECKOV_VERSION}"

# Bake semgrep rulesets so runtime scans skip the network.
# run-semgrep.sh points --config at /opt/ai-pr-review/semgrep-rules/ and falls
# back to --config=auto if the directory is absent (e.g. when invoked outside
# the container). SEMGREP_RULESET_DATE above invalidates this layer's cache.
RUN mkdir -p /opt/ai-pr-review/semgrep-rules && \
    echo "Fetching semgrep rulesets (cache-buster: ${SEMGREP_RULESET_DATE})" && \
    curl -fsSL -o /opt/ai-pr-review/semgrep-rules/ci.yml \
      "https://semgrep.dev/c/p/ci" && \
    curl -fsSL -o /opt/ai-pr-review/semgrep-rules/security-audit.yml \
      "https://semgrep.dev/c/p/security-audit" && \
    grep -q '^rules:' /opt/ai-pr-review/semgrep-rules/ci.yml && \
    grep -q '^rules:' /opt/ai-pr-review/semgrep-rules/security-audit.yml

# phpcs with Drupal coding standards + phpstan with phpstan-drupal.
# composer.phar itself is only needed at build time, so it stays in the builder
# stage. The resulting /opt/composer vendor tree is COPYied to the final stage
# and the phpcs/phpstan symlinks are recreated there.
RUN curl -fsSL -o /usr/local/bin/composer \
      "https://getcomposer.org/download/latest-stable/composer.phar" && \
    chmod +x /usr/local/bin/composer && \
    COMPOSER_HOME=/opt/composer COMPOSER_ALLOW_SUPERUSER=1 \
      composer global config allow-plugins.dealerdirect/phpcodesniffer-composer-installer true && \
    COMPOSER_HOME=/opt/composer COMPOSER_ALLOW_SUPERUSER=1 \
      composer global require --no-interaction \
      "squizlabs/php_codesniffer:${PHPCS_VERSION}" \
      "drupal/coder:${DRUPAL_CODER_VERSION}" \
      "phpstan/phpstan:${PHPSTAN_VERSION}" \
      "mglaman/phpstan-drupal:${PHPSTAN_DRUPAL_VERSION}"

# ==============================================================================
# Final stage
# ==============================================================================
FROM ubuntu:24.04@sha256:c4a8d5503dfb2a3eb8ab5f807da5bc69a85730fb49b5cfca2330194ebcc41c7b

ENV DEBIAN_FRONTEND=noninteractive

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# Runtime-only dependencies.
#
# curl is required at runtime — it is invoked by llm-call.sh,
# post-review-{github,bitbucket,gitlab}.sh (provider API calls), and
# analyzers/run-cve-check.sh (OSV.dev queries).
#
# git is required at runtime — the action scripts invoke git against the
# mounted workspace.
# hadolint ignore=DL3008
RUN apt-get update -qq && \
    apt-get install -y -qq --no-install-recommends \
      bash \
      ca-certificates \
      curl \
      git \
      jq \
      php-cli \
      php-xml \
      php-mbstring \
      python3 \
    && rm -rf /var/lib/apt/lists/*

# Binaries from single-binary tool stages
COPY --from=shellcheck   /bin/shellcheck          /usr/local/bin/shellcheck
COPY --from=trufflehog   /usr/bin/trufflehog      /usr/local/bin/trufflehog
COPY --from=golangci-lint /usr/bin/golangci-lint  /usr/local/bin/golangci-lint
COPY --from=hadolint     /bin/hadolint            /usr/local/bin/hadolint
COPY --from=kube-linter  /kube-linter             /usr/local/bin/kube-linter
COPY --from=tflint       /usr/local/bin/tflint    /usr/local/bin/tflint
COPY --from=ruff         /ruff                    /usr/local/bin/ruff

# gh CLI + pip dist-packages (semgrep, checkov + transitive deps) from builder.
#
# /usr/local/bin is copied wholesale because pip-installed tools bring a web
# of companion entry points (semgrep shells out to pysemgrep; checkov pulls
# in cloudsplaining, detect-secrets, policy_sentry, etc). Enumerating them
# individually is fragile — pip package updates silently add new ones.
# Copying the whole dir is simpler and correct; composer.phar is removed
# below since it's build-time-only. The single-binary tools copied above are
# overwritten here — that's fine, same binaries.
#
# /usr/local/lib/python3.12/dist-packages carries the actual Python code for
# semgrep, checkov and their transitive dependencies.
COPY --from=builder /usr/local/bin                          /usr/local/bin
COPY --from=builder /usr/local/lib/python3.12/dist-packages /usr/local/lib/python3.12/dist-packages

# Composer is build-time-only; drop its phar from the final image.
RUN rm -f /usr/local/bin/composer

# Copy the composer vendor tree and recreate the phpcs/phpstan bin symlinks.
COPY --from=builder /opt/composer /opt/composer
RUN ln -s /opt/composer/vendor/bin/phpcs   /usr/local/bin/phpcs && \
    ln -s /opt/composer/vendor/bin/phpstan /usr/local/bin/phpstan

# Baked semgrep rulesets (pure data).
COPY --from=builder /opt/ai-pr-review/semgrep-rules /opt/ai-pr-review/semgrep-rules

# Action scripts are copied LAST so source-only changes don't invalidate any
# of the heavy layers above.
COPY review.sh post-review*.sh llm-call.sh \
     /opt/ai-pr-review/

COPY analyzers/         /opt/ai-pr-review/analyzers/
COPY config/            /opt/ai-pr-review/config/
COPY lib/               /opt/ai-pr-review/lib/
COPY prompts/           /opt/ai-pr-review/prompts/
COPY language-profiles/ /opt/ai-pr-review/language-profiles/
COPY vcs/               /opt/ai-pr-review/vcs/

# Python engine (Epic 1). Installed into the system site-packages so the
# non-root app user can run `python3 -m ai_pr_review` without a venv.
# --break-system-packages is required on Ubuntu 24.04 (PEP 668).
COPY ai_pr_review/      /opt/ai-pr-review/ai_pr_review/
COPY pyproject.toml     /opt/ai-pr-review/pyproject.toml
RUN pip3 install --no-cache-dir --break-system-packages /opt/ai-pr-review

RUN chmod +x /opt/ai-pr-review/*.sh /opt/ai-pr-review/analyzers/*.sh

# Run as non-root
RUN groupadd -r app --gid 1001 && \
    useradd -r -g app --uid 1001 -d /home/app -m app && \
    chown -R app:app /opt/ai-pr-review

# Allow git to operate on /workspace regardless of the host uid that owns it.
# Needed when the mounted workspace uid differs from the container's app uid
# (1001), which is common on self-hosted runners and local dev.
RUN git config --system --add safe.directory /workspace

USER 1001:1001

WORKDIR /workspace

ENTRYPOINT ["/opt/ai-pr-review/review.sh"]
