# Security Policy

## Supported versions

| Version | Supported |
|---------|-----------|
| v0.10.x (latest) | ✅ |
| < v0.10.0 | ❌ |

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
