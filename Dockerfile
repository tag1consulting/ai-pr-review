# syntax=docker/dockerfile:1.25@sha256:0adf442eae370b6087e08edc7c50b552d80ddf261576f4ebd6421006b2461f12
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
#                   gh CLI (no official image)
#   final         — slim runtime; assembles binaries from all prior stages

# ==============================================================================
# Single-binary tool stages
# ==============================================================================

# hadolint ignore=DL3029
FROM koalaman/shellcheck:v0.11.0@sha256:61862eba1fcf09a484ebcc6feea46f1782532571a34ed51fedf90dd25f925a8d AS shellcheck
# hadolint ignore=DL3029
FROM trufflesecurity/trufflehog:3.95.6@sha256:96f8429082cb2d4ae73b1096dcdb2f5aa139881d97042b0c5e5fa226a392e056 AS trufflehog
# hadolint ignore=DL3029
FROM golangci/golangci-lint:v2.12.2@sha256:5cceeef04e53efe1470638d4b4b4f5ceefd574955ab3941b2d9a68a8c9ad5240 AS golangci-lint
# hadolint ignore=DL3029
FROM hadolint/hadolint:v2.14.0@sha256:27086352fd5e1907ea2b934eb1023f217c5ae087992eb59fde121dce9c9ff21e AS hadolint
# hadolint ignore=DL3029
FROM ghcr.io/stackrox/kube-linter:v0.8.3@sha256:f2bfce7879206d32f69ab6572c376f916643f54ca291ac38cf7d01ef591ff3f9 AS kube-linter
# hadolint ignore=DL3029
FROM ghcr.io/terraform-linters/tflint:v0.63.1@sha256:890e37827d7b5e400f26137c5189c7efa581365fe9299b5b9814e5148d5978b9 AS tflint
# hadolint ignore=DL3029
FROM ghcr.io/astral-sh/ruff:0.15.18@sha256:1dd6bfd97bbeb01aa1f86ceb2064ae61dd0892e647a53a92825ef31868c0103a AS ruff

# ==============================================================================
# Builder stage — pip packages, composer packages, gh CLI, semgrep rulesets
# ==============================================================================
FROM ubuntu:24.04@sha256:786a8b558f7be160c6c8c4a54f9a57274f3b4fb1491cf65146521ae77ff1dc54 AS builder

ARG TARGETARCH

# Python minor version shipped by the base image. Declared once and propagated
# to dist-packages COPY paths below so a future base-image bump is a one-line
# change. Ubuntu 24.04 ships Python 3.12.
ARG PYTHON_VERSION=3.12

ARG GH_VERSION=2.91.0
ARG GH_SHA256_AMD64=304a0d2460f4a8847d2f192bad4e2a32cd9420d28716e7ae32198181b65b5f9c
ARG GH_SHA256_ARM64=ccbed39c472d3dc1c501d1e164a9cffd934c5f6fce1012811a1a59d24cb7d7c6

ARG SEMGREP_VERSION=1.161.0
ARG CHECKOV_VERSION=3.2.524

ARG PHPCS_VERSION=4.0.1
ARG DRUPAL_CODER_VERSION=9.0.0
ARG PHPSTAN_VERSION=2.1.51
ARG PHPSTAN_DRUPAL_VERSION=2.0.15
# composer.phar is single-file (PHP, arch-independent). Pin to an exact version
# and verify its SHA-256 against Composer's published checksum.
# To update: pick a version from https://getcomposer.org/download/, then
#   curl -fsSL https://getcomposer.org/download/<version>/composer.phar.sha256sum
ARG COMPOSER_VERSION=2.10.1
ARG COMPOSER_SHA256=345b9c6a98da5c30dcbd4b0d99fc8710bf0ae98a3898eea18f7b2ad9dec93f06

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
      ripgrep \
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

# semgrep, checkov, and the ai_pr_review Python engine via pip.
# --break-system-packages required on Ubuntu 24.04 (PEP 668). Installs land in
# /usr/local/lib/python${PYTHON_VERSION}/dist-packages plus /usr/local/bin entry
# points; both are COPYied into the final stage below alongside semgrep/checkov.
COPY ai_pr_review/ /opt/ai-pr-review/ai_pr_review/
COPY pyproject.toml /opt/ai-pr-review/pyproject.toml
RUN pip3 install --no-cache-dir --break-system-packages \
      "semgrep==${SEMGREP_VERSION}" \
      "checkov==${CHECKOV_VERSION}" \
      "/opt/ai-pr-review[context]"

# NOTE: semgrep registry rulesets are NOT baked into the image. The
# Semgrep-maintained rules (e.g. p/ci, p/security-audit) are licensed under the
# Semgrep Rules License v1.0, which restricts internal/non-competing/non-SaaS
# use and is not freely redistributable inside this tool's image. Instead,
# run-semgrep.sh falls back to `--config=auto`, which fetches rules at runtime
# (the user fetches them, mirroring the composite-action model). This keeps the
# distributed image free of use-restricted rule content. See
# memory-bank/license-audit-2026-06-01.md.

# phpcs with Drupal coding standards + phpstan with phpstan-drupal.
# composer.phar itself is only needed at build time, so it stays in the builder
# stage. The resulting /opt/composer vendor tree is COPYied to the final stage
# and the phpcs/phpstan symlinks are recreated there.
RUN curl -fsSL -o /usr/local/bin/composer \
      "https://getcomposer.org/download/${COMPOSER_VERSION}/composer.phar" && \
    echo "${COMPOSER_SHA256}  /usr/local/bin/composer" | sha256sum -c - && \
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
FROM ubuntu:24.04@sha256:786a8b558f7be160c6c8c4a54f9a57274f3b4fb1491cf65146521ae77ff1dc54

# ARG must be re-declared in this stage; multi-stage Dockerfiles do not share
# ARG scope. Default must match the builder stage above.
ARG PYTHON_VERSION=3.12

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
      ripgrep \
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
# /usr/local/lib/python${PYTHON_VERSION}/dist-packages carries the actual Python
# code for semgrep, checkov and their transitive dependencies.
COPY --from=builder /usr/local/bin                          /usr/local/bin
COPY --from=builder /usr/local/lib/python${PYTHON_VERSION}/dist-packages /usr/local/lib/python${PYTHON_VERSION}/dist-packages

# Composer is build-time-only; drop its phar from the final image.
RUN rm -f /usr/local/bin/composer

# Copy the composer vendor tree and recreate the phpcs/phpstan bin symlinks.
COPY --from=builder /opt/composer /opt/composer
RUN ln -s /opt/composer/vendor/bin/phpcs   /usr/local/bin/phpcs && \
    ln -s /opt/composer/vendor/bin/phpstan /usr/local/bin/phpstan

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
# Third-party license texts for the bundled analyzers (shellcheck, semgrep,
# trufflehog, etc.). Required for redistribution of these tools inside the image.
COPY THIRD-PARTY-LICENSES/ /opt/ai-pr-review/THIRD-PARTY-LICENSES/
# Python engine source (pip-installed from builder via dist-packages COPY above).
COPY ai_pr_review/      /opt/ai-pr-review/ai_pr_review/
COPY pyproject.toml     /opt/ai-pr-review/pyproject.toml

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

# Point the Python engine at the asset directories (prompts/, language-profiles/,
# config/) copied to /opt/ai-pr-review. Without this var, runtime.py falls back
# to a dist-packages path that does not contain these directories.
ENV AI_PR_REVIEW_SCRIPT_DIR=/opt/ai-pr-review

ENTRYPOINT ["python3", "-m", "ai_pr_review", "review"]
