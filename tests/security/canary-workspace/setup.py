# SECURITY CANARY — do not remove or modify.
# If this file is ever imported or executed by the action, a sentinel file is
# written to CANARY_DIR to signal a security invariant violation.
import os
import pathlib

canary = pathlib.Path(os.environ.get("CANARY_DIR", "/tmp/canary"))
canary.mkdir(parents=True, exist_ok=True)
(canary / "setup-py-imported").touch()

from setuptools import setup  # noqa: E402

setup(name="canary", version="0.0.1")
