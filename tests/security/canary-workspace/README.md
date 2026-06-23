# Security Canary Workspace

This directory contains security tripwire files — **do not remove, modify, or "fix" them.**

## What this is

These files are canaries for the security invariant: *the action treats the checked-out repository strictly as untrusted data and never executes any content from it.*

Each file is designed to write a sentinel marker to `$CANARY_DIR` if it is ever executed, imported, or invoked by the action:

| File | Tripwire condition |
|------|--------------------|
| `Makefile` | Default target run by `make` |
| `setup.py` | File imported by Python interpreter |
| `package.json` | `preinstall`/`postinstall` scripts run by `npm install` |
| `.pre-commit-config.yaml` | Hook run by `pre-commit` |
| `conftest.py` | File imported by pytest auto-discovery |
| `.semgrep.yml` | Custom rule loaded as executable config |

## How the guard works

`tests/python/test_security_canary.py` (marked `@pytest.mark.security`) exercises every code path that reads workspace files as data and asserts that none of the sentinel files appear in `$CANARY_DIR`.

These tests run on every PR via the `Lint & Test` CI workflow.

## If a test fails

A failure is a **security regression**, not a flaky test. See [CONTRIBUTING.md](../../../CONTRIBUTING.md) and [SECURITY.md](../../../SECURITY.md) for the reporting process. Do not skip, xfail, or delete the failing test — report it privately first.
