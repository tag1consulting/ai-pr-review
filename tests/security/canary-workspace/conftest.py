# SECURITY CANARY — do not remove or modify.
#
# If pytest auto-discovers and imports this conftest.py, the invariant
# "never execute checked-out code" has been broken. A sentinel file is
# written to CANARY_DIR to make this detectable in tests.
#
# This file is intentionally NOT under tests/python/ (the pytest testpaths
# configured in pyproject.toml) and tests/security/canary-workspace/ is also
# listed in norecursedirs, so pytest never traverses here during normal runs.
# If you see conftest-imported in the canary dir, pytest traversal changed.
#
# Design note on the /tmp/canary fallback: the test suite sets CANARY_DIR via
# monkeypatch.setenv before exercising any workspace-reading code path. The
# /tmp/canary fallback only matters when this file is auto-collected by pytest
# without any test having set CANARY_DIR first — which is exactly the failure
# mode (unexpected pytest traversal into canary-workspace/). In that case the
# sentinel lands in /tmp/canary rather than a tmp_path-scoped dir, so
# test_security_canary.py won't catch it directly — but the collection-time
# side-effect of *importing* this file is itself the signal. If this file is
# ever imported, the action's pytest traversal guard has broken, regardless of
# where the sentinel lands.
import os
import pathlib

canary = pathlib.Path(os.environ.get("CANARY_DIR", "/tmp/canary"))
canary.mkdir(parents=True, exist_ok=True)
(canary / "conftest-imported").touch()
