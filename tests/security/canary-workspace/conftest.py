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
import os
import pathlib

canary = pathlib.Path(os.environ.get("CANARY_DIR", "/tmp/canary"))
canary.mkdir(parents=True, exist_ok=True)
(canary / "conftest-imported").touch()
