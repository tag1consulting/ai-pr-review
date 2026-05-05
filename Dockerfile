FROM ubuntu:24.04@sha256:c4a8d5503dfb2a3eb8ab5f807da5bc69a85730fb49b5cfca2330194ebcc41c7b

ARG SHELLCHECK_VERSION=v0.11.0
ARG SHELLCHECK_SHA256=8c3be12b05d5c177a04c29e3c78ce89ac86f1595681cab149b65b97c4e227198

ARG GH_VERSION=2.91.0
ARG GH_SHA256=304a0d2460f4a8847d2f192bad4e2a32cd9420d28716e7ae32198181b65b5f9c

ARG TRUFFLEHOG_VERSION=3.95.2
ARG TRUFFLEHOG_SHA256=fded1c139fe4d3872d9fde65e1428d82d5556d655439e82f492d87ae8d846779

ARG GOLANGCI_LINT_VERSION=2.11.4
ARG GOLANGCI_LINT_SHA256=200c5b7503f67b59a6743ccf32133026c174e272b930ee79aa2aa6f37aca7ef1

ARG RUFF_VERSION=0.15.11
ARG SEMGREP_VERSION=1.161.0

ARG HADOLINT_VERSION=v2.14.0
ARG HADOLINT_SHA256=6bf226944684f56c84dd014e8b979d27425c0148f61b3bd99bcc6f39e9dc5a47

ARG KUBELINTER_VERSION=v0.8.3
ARG KUBELINTER_SHA256=618d299a3e2839c8ca9d86fce0db617be0fba41f0fecbbbfb7fbf1c04299fae1

ARG TFLINT_VERSION=v0.62.0
ARG TFLINT_SHA256=000400d7f4c2236d9ed4b35fec3ee95617c3747571593cc6138169fc78cc226a

ARG CHECKOV_VERSION=3.2.524

ARG PHPCS_VERSION=4.0.1
ARG DRUPAL_CODER_VERSION=9.0.0
ARG PHPSTAN_VERSION=2.1.51
ARG PHPSTAN_DRUPAL_VERSION=2.0.15

ENV DEBIAN_FRONTEND=noninteractive

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
      python3-pip \
      unzip \
      xz-utils \
    && rm -rf /var/lib/apt/lists/*

# shellcheck
RUN curl -fsSL -o /tmp/shellcheck.tar.xz \
      "https://github.com/koalaman/shellcheck/releases/download/${SHELLCHECK_VERSION}/shellcheck-${SHELLCHECK_VERSION}.linux.x86_64.tar.xz" && \
    echo "${SHELLCHECK_SHA256}  /tmp/shellcheck.tar.xz" | sha256sum -c - && \
    tar -xJ --strip-components=1 -C /usr/local/bin -f /tmp/shellcheck.tar.xz \
        "shellcheck-${SHELLCHECK_VERSION}/shellcheck" && \
    chmod +x /usr/local/bin/shellcheck && \
    rm /tmp/shellcheck.tar.xz

# gh CLI
RUN curl -fsSL -o /tmp/gh.tar.gz \
      "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_amd64.tar.gz" && \
    echo "${GH_SHA256}  /tmp/gh.tar.gz" | sha256sum -c - && \
    tar -xz --strip-components=2 -C /usr/local/bin -f /tmp/gh.tar.gz \
        "gh_${GH_VERSION}_linux_amd64/bin/gh" && \
    chmod +x /usr/local/bin/gh && \
    rm /tmp/gh.tar.gz

# trufflehog
RUN curl -fsSL -o /tmp/trufflehog.tar.gz \
      "https://github.com/trufflesecurity/trufflehog/releases/download/v${TRUFFLEHOG_VERSION}/trufflehog_${TRUFFLEHOG_VERSION}_linux_amd64.tar.gz" && \
    echo "${TRUFFLEHOG_SHA256}  /tmp/trufflehog.tar.gz" | sha256sum -c - && \
    tar -xz -C /usr/local/bin -f /tmp/trufflehog.tar.gz trufflehog && \
    chmod +x /usr/local/bin/trufflehog && \
    rm /tmp/trufflehog.tar.gz

# golangci-lint
RUN curl -fsSL -o /tmp/golangci-lint.tar.gz \
      "https://github.com/golangci/golangci-lint/releases/download/v${GOLANGCI_LINT_VERSION}/golangci-lint-${GOLANGCI_LINT_VERSION}-linux-amd64.tar.gz" && \
    echo "${GOLANGCI_LINT_SHA256}  /tmp/golangci-lint.tar.gz" | sha256sum -c - && \
    tar -xz --strip-components=1 -C /usr/local/bin -f /tmp/golangci-lint.tar.gz \
        "golangci-lint-${GOLANGCI_LINT_VERSION}-linux-amd64/golangci-lint" && \
    chmod +x /usr/local/bin/golangci-lint && \
    rm /tmp/golangci-lint.tar.gz

# hadolint
RUN curl -fsSL -o /usr/local/bin/hadolint \
      "https://github.com/hadolint/hadolint/releases/download/${HADOLINT_VERSION}/hadolint-Linux-x86_64" && \
    echo "${HADOLINT_SHA256}  /usr/local/bin/hadolint" | sha256sum -c - && \
    chmod +x /usr/local/bin/hadolint

# kube-linter
RUN curl -fsSL -o /usr/local/bin/kube-linter \
      "https://github.com/stackrox/kube-linter/releases/download/${KUBELINTER_VERSION}/kube-linter-linux" && \
    echo "${KUBELINTER_SHA256}  /usr/local/bin/kube-linter" | sha256sum -c - && \
    chmod +x /usr/local/bin/kube-linter

# tflint
RUN curl -fsSL -o /tmp/tflint.zip \
      "https://github.com/terraform-linters/tflint/releases/download/${TFLINT_VERSION}/tflint_linux_amd64.zip" && \
    echo "${TFLINT_SHA256}  /tmp/tflint.zip" | sha256sum -c - && \
    unzip -o /tmp/tflint.zip -d /usr/local/bin && \
    chmod +x /usr/local/bin/tflint && \
    rm /tmp/tflint.zip

# ruff, semgrep, and checkov via pip
# --break-system-packages required on Ubuntu 24.04 (PEP 668)
RUN pip3 install --no-cache-dir --break-system-packages \
      "ruff==${RUFF_VERSION}" \
      "semgrep==${SEMGREP_VERSION}" \
      "checkov==${CHECKOV_VERSION}"

# phpcs with Drupal coding standards + phpstan with phpstan-drupal
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
      "mglaman/phpstan-drupal:${PHPSTAN_DRUPAL_VERSION}" && \
    ln -s /opt/composer/vendor/bin/phpcs /usr/local/bin/phpcs && \
    ln -s /opt/composer/vendor/bin/phpstan /usr/local/bin/phpstan

# Copy core scripts. Uses a glob for post-review*.sh so sibling provider
# scripts (post-review-bitbucket.sh, future providers) are picked up
# automatically without per-release Dockerfile churn.
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
# Needed when the mounted workspace uid differs from the container's app uid (1001),
# which is common on self-hosted runners and local dev.
RUN git config --system --add safe.directory /workspace

USER 1001:1001

# Consumers mount their repo checkout here
WORKDIR /workspace

ENTRYPOINT ["/opt/ai-pr-review/review.sh"]
