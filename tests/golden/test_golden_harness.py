"""
tests/golden/test_golden_harness.py — pytest wrapper for the golden parity harness.

Discovers all fixtures under tests/golden/fixtures/ and asserts each passes
the diff_harness payload-level checks. One pytest test per fixture.
"""
import pytest
from pathlib import Path
from .diff_harness import FIXTURES_DIR, run_fixture

_fixture_dirs = sorted(d for d in FIXTURES_DIR.iterdir() if d.is_dir()) if FIXTURES_DIR.exists() else []


@pytest.mark.parametrize("fixture_dir", _fixture_dirs, ids=lambda d: d.name)
def test_fixture_parity(fixture_dir: Path) -> None:
    result = run_fixture(fixture_dir)
    if result.error:
        pytest.fail(f"Fixture error: {result.error}")
    if not result.passed:
        failures = result.assertion_result.failures if result.assertion_result else ["unknown"]
        pytest.fail("\n".join(failures))
