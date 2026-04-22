FROM ubuntu:24.04

ARG SHELLCHECK_VERSION=v0.11.0
ARG GH_VERSION=2.91.0
ARG TRUFFLEHOG_VERSION=3.95.2
ARG GOLANGCI_LINT_VERSION=2.11.4
ARG RUFF_VERSION=0.15.11
ARG SEMGREP_VERSION=1.161.0

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update -qq && \
    apt-get install -y -qq --no-install-recommends \
      bash \
      ca-certificates \
      curl \
      git \
      jq \
      python3 \
      python3-pip \
      xz-utils \
    && rm -rf /var/lib/apt/lists/*

# shellcheck
RUN curl -fsSL \
      "https://github.com/koalaman/shellcheck/releases/download/${SHELLCHECK_VERSION}/shellcheck-${SHELLCHECK_VERSION}.linux.x86_64.tar.xz" \
    | tar -xJ --strip-components=1 -C /usr/local/bin \
        "shellcheck-${SHELLCHECK_VERSION}/shellcheck" && \
    chmod +x /usr/local/bin/shellcheck

# gh CLI
RUN curl -fsSL \
      "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_amd64.tar.gz" \
    | tar -xz --strip-components=2 -C /usr/local/bin \
        "gh_${GH_VERSION}_linux_amd64/bin/gh" && \
    chmod +x /usr/local/bin/gh

# trufflehog
RUN curl -fsSL \
      "https://github.com/trufflesecurity/trufflehog/releases/download/v${TRUFFLEHOG_VERSION}/trufflehog_${TRUFFLEHOG_VERSION}_linux_amd64.tar.gz" \
    | tar -xz -C /usr/local/bin trufflehog && \
    chmod +x /usr/local/bin/trufflehog

# golangci-lint
RUN curl -fsSL \
      "https://github.com/golangci/golangci-lint/releases/download/v${GOLANGCI_LINT_VERSION}/golangci-lint-${GOLANGCI_LINT_VERSION}-linux-amd64.tar.gz" \
    | tar -xz --strip-components=1 -C /usr/local/bin \
        "golangci-lint-${GOLANGCI_LINT_VERSION}-linux-amd64/golangci-lint" && \
    chmod +x /usr/local/bin/golangci-lint

# ruff and semgrep via pip
RUN pip3 install --no-cache-dir \
      "ruff==${RUFF_VERSION}" \
      "semgrep==${SEMGREP_VERSION}"

# Copy action scripts and supporting files
COPY review.sh post-review.sh llm-call.sh \
     run-shellcheck.sh run-cve-check.sh run-semgrep.sh \
     run-trufflehog.sh run-ruff.sh run-golangci-lint.sh \
     /opt/ai-pr-review/

COPY prompts/           /opt/ai-pr-review/prompts/
COPY language-profiles/ /opt/ai-pr-review/language-profiles/
COPY suppressions.json model-pricing.json /opt/ai-pr-review/

RUN chmod +x /opt/ai-pr-review/*.sh

# Consumers mount their repo checkout here
WORKDIR /workspace

ENTRYPOINT ["/opt/ai-pr-review/review.sh"]
