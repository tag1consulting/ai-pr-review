"""
tests/golden/test_golden_harness.py — pytest wrapper for the golden parity harness
and inline eligibility oracle.

Discovers all fixtures under tests/golden/fixtures/ and asserts each passes
both the diff_harness payload-level checks and the inline_eligibility oracle.
One pytest test per fixture per check.
"""
import pytest
from pathlib import Path
from .diff_harness import FIXTURES_DIR, run_fixture
from .inline_eligibility import check_fixture_eligibility

_fixture_dirs = sorted(d for d in FIXTURES_DIR.iterdir() if d.is_dir()) if FIXTURES_DIR.exists() else []


@pytest.mark.parametrize("fixture_dir", _fixture_dirs, ids=lambda d: d.name)
def test_fixture_parity(fixture_dir: Path) -> None:
    result = run_fixture(fixture_dir)
    if result.error:
        pytest.fail(f"Fixture error: {result.error}")
    if not result.passed:
        failures = result.assertion_result.failures if result.assertion_result else ["unknown"]
        pytest.fail("\n".join(failures))


@pytest.mark.parametrize("fixture_dir", _fixture_dirs, ids=lambda d: d.name)
def test_inline_eligibility(fixture_dir: Path) -> None:
    result = check_fixture_eligibility(fixture_dir)
    if not result.ok:
        pytest.fail("\n".join(result.failures))
