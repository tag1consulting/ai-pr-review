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

# ruff and semgrep via pip
# --break-system-packages required on Ubuntu 24.04 (PEP 668)
RUN pip3 install --no-cache-dir --break-system-packages \
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
