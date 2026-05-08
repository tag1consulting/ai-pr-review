# syntax=docker/dockerfile:1.7
#
# Multi-stage build.
#   Stage 1 (builder): installs build-time tools (curl, unzip, xz-utils, pip,
#   composer.phar), downloads every analyzer binary, pip-installs ruff/semgrep/
#   checkov, composer-installs phpcs/phpstan, and fetches semgrep rulesets.
#   Stage 2 (final):   slim runtime with only the libraries the analyzers need
#   (bash, ca-certificates, git, jq, php-cli + ext, python3). Copies only the
#   binaries, pip dist-packages, composer vendor tree, and semgrep rules from
#   the builder. Action scripts are copied LAST so a source-only change does
#   not invalidate any of the heavy builder layers.
#
# Why multi-stage: curl, unzip, xz-utils, python3-pip, and composer.phar are
# build-time-only. Dropping them from the final image shrinks the per-run
# docker pull and trims runtime attack surface.

# ==============================================================================
# Stage 1: builder
# ==============================================================================
FROM ubuntu:24.04@sha256:c4a8d5503dfb2a3eb8ab5f807da5bc69a85730fb49b5cfca2330194ebcc41c7b AS builder

ARG TARGETARCH

ARG SHELLCHECK_VERSION=v0.11.0
ARG SHELLCHECK_SHA256_AMD64=8c3be12b05d5c177a04c29e3c78ce89ac86f1595681cab149b65b97c4e227198
ARG SHELLCHECK_SHA256_ARM64=12b331c1d2db6b9eb13cfca64306b1b157a86eb69db83023e261eaa7e7c14588

ARG GH_VERSION=2.91.0
ARG GH_SHA256_AMD64=304a0d2460f4a8847d2f192bad4e2a32cd9420d28716e7ae32198181b65b5f9c
ARG GH_SHA256_ARM64=ccbed39c472d3dc1c501d1e164a9cffd934c5f6fce1012811a1a59d24cb7d7c6

ARG TRUFFLEHOG_VERSION=3.95.2
ARG TRUFFLEHOG_SHA256_AMD64=fded1c139fe4d3872d9fde65e1428d82d5556d655439e82f492d87ae8d846779
ARG TRUFFLEHOG_SHA256_ARM64=5588f09da2d52e840273b6a8c57751021709182dff42574f09dbaf81ebdf8366

ARG GOLANGCI_LINT_VERSION=2.11.4
ARG GOLANGCI_LINT_SHA256_AMD64=200c5b7503f67b59a6743ccf32133026c174e272b930ee79aa2aa6f37aca7ef1
ARG GOLANGCI_LINT_SHA256_ARM64=3bcfa2e6f3d32b2bf5cd75eaa876447507025e0303698633f722a05331988db4

ARG RUFF_VERSION=0.15.11
ARG SEMGREP_VERSION=1.161.0

# Cache-buster for semgrep ruleset downloads; bump to force fresh pull.
ARG SEMGREP_RULESET_DATE=2026-05-07

ARG HADOLINT_VERSION=v2.14.0
ARG HADOLINT_SHA256_AMD64=6bf226944684f56c84dd014e8b979d27425c0148f61b3bd99bcc6f39e9dc5a47
ARG HADOLINT_SHA256_ARM64=331f1d3511b84a4f1e3d18d52fec284723e4019552f4f47b19322a53ce9a40ed

ARG KUBELINTER_VERSION=v0.8.3
ARG KUBELINTER_SHA256_AMD64=618d299a3e2839c8ca9d86fce0db617be0fba41f0fecbbbfb7fbf1c04299fae1
ARG KUBELINTER_SHA256_ARM64=9c39d35252e0dcafb16b26197b9e93ba578e44eb402c3c6660fc94e08f94094f

ARG TFLINT_VERSION=v0.62.0
ARG TFLINT_SHA256_AMD64=000400d7f4c2236d9ed4b35fec3ee95617c3747571593cc6138169fc78cc226a
ARG TFLINT_SHA256_ARM64=064206ec85adaf90f637c880eb3cd5a8e07ddce09e4da7c813eb362cb794f95f

ARG CHECKOV_VERSION=3.2.524

ARG PHPCS_VERSION=4.0.1
ARG DRUPAL_CODER_VERSION=9.0.0
ARG PHPSTAN_VERSION=2.1.51
ARG PHPSTAN_DRUPAL_VERSION=2.0.15

ENV DEBIAN_FRONTEND=noninteractive

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# Build-time toolchain. None of these need to exist in the final stage.
# hadolint ignore=DL3008
RUN apt-get update -qq && \
    apt-get install -y -qq --no-install-recommends \
      bash \
      ca-certificates \
      curl \
      jq \
      php-cli \
      php-xml \
      php-mbstring \
      python3 \
      python3-pip \
      unzip \
      xz-utils \
    && rm -rf /var/lib/apt/lists/*

# shellcheck
RUN case "${TARGETARCH}" in \
      amd64) ARCH=x86_64;  SHA="${SHELLCHECK_SHA256_AMD64}" ;; \
      arm64) ARCH=aarch64; SHA="${SHELLCHECK_SHA256_ARM64}" ;; \
      *) echo "unsupported TARGETARCH: ${TARGETARCH}" >&2; exit 1 ;; \
    esac && \
    curl -fsSL -o /tmp/shellcheck.tar.xz \
      "https://github.com/koalaman/shellcheck/releases/download/${SHELLCHECK_VERSION}/shellcheck-${SHELLCHECK_VERSION}.linux.${ARCH}.tar.xz" && \
    echo "${SHA}  /tmp/shellcheck.tar.xz" | sha256sum -c - && \
    tar -xJ --strip-components=1 -C /usr/local/bin -f /tmp/shellcheck.tar.xz \
        "shellcheck-${SHELLCHECK_VERSION}/shellcheck" && \
    chmod +x /usr/local/bin/shellcheck && \
    rm /tmp/shellcheck.tar.xz

# gh CLI
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

# trufflehog
RUN case "${TARGETARCH}" in \
      amd64) SHA="${TRUFFLEHOG_SHA256_AMD64}" ;; \
      arm64) SHA="${TRUFFLEHOG_SHA256_ARM64}" ;; \
      *) echo "unsupported TARGETARCH: ${TARGETARCH}" >&2; exit 1 ;; \
    esac && \
    curl -fsSL -o /tmp/trufflehog.tar.gz \
      "https://github.com/trufflesecurity/trufflehog/releases/download/v${TRUFFLEHOG_VERSION}/trufflehog_${TRUFFLEHOG_VERSION}_linux_${TARGETARCH}.tar.gz" && \
    echo "${SHA}  /tmp/trufflehog.tar.gz" | sha256sum -c - && \
    tar -xz -C /usr/local/bin -f /tmp/trufflehog.tar.gz trufflehog && \
    chmod +x /usr/local/bin/trufflehog && \
    rm /tmp/trufflehog.tar.gz

# golangci-lint
RUN case "${TARGETARCH}" in \
      amd64) SHA="${GOLANGCI_LINT_SHA256_AMD64}" ;; \
      arm64) SHA="${GOLANGCI_LINT_SHA256_ARM64}" ;; \
      *) echo "unsupported TARGETARCH: ${TARGETARCH}" >&2; exit 1 ;; \
    esac && \
    curl -fsSL -o /tmp/golangci-lint.tar.gz \
      "https://github.com/golangci/golangci-lint/releases/download/v${GOLANGCI_LINT_VERSION}/golangci-lint-${GOLANGCI_LINT_VERSION}-linux-${TARGETARCH}.tar.gz" && \
    echo "${SHA}  /tmp/golangci-lint.tar.gz" | sha256sum -c - && \
    tar -xz --strip-components=1 -C /usr/local/bin -f /tmp/golangci-lint.tar.gz \
        "golangci-lint-${GOLANGCI_LINT_VERSION}-linux-${TARGETARCH}/golangci-lint" && \
    chmod +x /usr/local/bin/golangci-lint && \
    rm /tmp/golangci-lint.tar.gz

# hadolint
RUN case "${TARGETARCH}" in \
      amd64) ARCH=x86_64; SHA="${HADOLINT_SHA256_AMD64}" ;; \
      arm64) ARCH=arm64;  SHA="${HADOLINT_SHA256_ARM64}" ;; \
      *) echo "unsupported TARGETARCH: ${TARGETARCH}" >&2; exit 1 ;; \
    esac && \
    curl -fsSL -o /usr/local/bin/hadolint \
      "https://github.com/hadolint/hadolint/releases/download/${HADOLINT_VERSION}/hadolint-Linux-${ARCH}" && \
    echo "${SHA}  /usr/local/bin/hadolint" | sha256sum -c - && \
    chmod +x /usr/local/bin/hadolint

# kube-linter (amd64 asset has no arch suffix, arm64 uses underscore-separated suffix)
RUN case "${TARGETARCH}" in \
      amd64) ASSET=kube-linter-linux;        SHA="${KUBELINTER_SHA256_AMD64}" ;; \
      arm64) ASSET=kube-linter-linux_arm64;  SHA="${KUBELINTER_SHA256_ARM64}" ;; \
      *) echo "unsupported TARGETARCH: ${TARGETARCH}" >&2; exit 1 ;; \
    esac && \
    curl -fsSL -o /usr/local/bin/kube-linter \
      "https://github.com/stackrox/kube-linter/releases/download/${KUBELINTER_VERSION}/${ASSET}" && \
    echo "${SHA}  /usr/local/bin/kube-linter" | sha256sum -c - && \
    chmod +x /usr/local/bin/kube-linter

# tflint
RUN case "${TARGETARCH}" in \
      amd64) SHA="${TFLINT_SHA256_AMD64}" ;; \
      arm64) SHA="${TFLINT_SHA256_ARM64}" ;; \
      *) echo "unsupported TARGETARCH: ${TARGETARCH}" >&2; exit 1 ;; \
    esac && \
    curl -fsSL -o /tmp/tflint.zip \
      "https://github.com/terraform-linters/tflint/releases/download/${TFLINT_VERSION}/tflint_linux_${TARGETARCH}.zip" && \
    echo "${SHA}  /tmp/tflint.zip" | sha256sum -c - && \
    unzip -o /tmp/tflint.zip -d /usr/local/bin && \
    chmod +x /usr/local/bin/tflint && \
    rm /tmp/tflint.zip

# ruff, semgrep, and checkov via pip.
# --break-system-packages required on Ubuntu 24.04 (PEP 668). Installs land in
# /usr/local/lib/python3.12/dist-packages plus /usr/local/bin entry points; both
# are COPYied into the final stage below.
RUN pip3 install --no-cache-dir --break-system-packages \
      "ruff==${RUFF_VERSION}" \
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
# Stage 2: final
# ==============================================================================
FROM ubuntu:24.04@sha256:c4a8d5503dfb2a3eb8ab5f807da5bc69a85730fb49b5cfca2330194ebcc41c7b

ENV DEBIAN_FRONTEND=noninteractive

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# Runtime-only dependencies. Notably absent vs builder: unzip, xz-utils,
# python3-pip (and its large dependency tree).
#
# curl is required at runtime — it is invoked by llm-call.sh,
# post-review-{github,bitbucket,gitlab}.sh (provider API calls), and
# analyzers/run-cve-check.sh (OSV.dev queries). PR #160 initially dropped
# curl from the final stage thinking it was build-time only; the regression
# caused every LLM call and provider API call to fail with curl exit 127.
# Keep curl in the runtime stage.
#
# git is runtime-only here (the action scripts invoke `git` against the
# mounted workspace).
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

# Copy analyzer binaries + pip dist-packages from the builder.
#
# /usr/local/bin is copied wholesale because pip-installed tools bring a web
# of companion entry points (semgrep shells out to pysemgrep; checkov pulls
# in cloudsplaining, detect-secrets, policy_sentry, etc). Enumerating them
# individually is fragile — pip package updates silently add new ones.
# Copying the whole dir is simpler and correct; composer.phar is removed
# below since it's build-time-only.
#
# /usr/local/lib/python3.12/dist-packages carries the actual Python code for
# ruff, semgrep, checkov and their transitive dependencies.
COPY --from=builder /usr/local/bin                               /usr/local/bin
COPY --from=builder /usr/local/lib/python3.12/dist-packages      /usr/local/lib/python3.12/dist-packages

# Composer is build-time-only; drop its phar from the final image.
RUN rm -f /usr/local/bin/composer

# Copy the composer vendor tree and recreate the phpcs/phpstan bin symlinks.
# (A COPY of the symlinks from builder would copy them as symlinks but we
# recreate explicitly for clarity — the link target path is identical.)
COPY --from=builder /opt/composer /opt/composer
RUN ln -s /opt/composer/vendor/bin/phpcs   /usr/local/bin/phpcs && \
    ln -s /opt/composer/vendor/bin/phpstan /usr/local/bin/phpstan

# Baked semgrep rulesets (pure data).
COPY --from=builder /opt/ai-pr-review/semgrep-rules /opt/ai-pr-review/semgrep-rules

# Action scripts are copied LAST so source-only changes don't invalidate any
# of the heavy layers above. Uses a glob for post-review*.sh so sibling
# provider scripts (post-review-bitbucket.sh, post-review-gitlab.sh, future
# providers) are picked up automatically.
COPY review.sh post-review*.sh llm-call.sh \
     /opt/ai-pr-review/

COPY analyzers/         /opt/ai-pr-review/analyzers/
COPY config/            /opt/ai-pr-review/config/
COPY prompts/           /opt/ai-pr-review/prompts/
COPY language-profiles/ /opt/ai-pr-review/language-profiles/

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

# Consumers mount their repo checkout here
WORKDIR /workspace

ENTRYPOINT ["/opt/ai-pr-review/review.sh"]
