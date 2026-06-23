# SECURITY CANARY — do not remove or modify.
# If this Makefile's default target is ever executed by the action, a sentinel
# file is written to CANARY_DIR to signal a security invariant violation.
.DEFAULT_GOAL := all
all:
	python3 -c "import os, pathlib; pathlib.Path(os.environ.get('CANARY_DIR','/tmp/canary')).mkdir(parents=True,exist_ok=True); pathlib.Path(os.environ.get('CANARY_DIR','/tmp/canary'),'makefile-executed').touch()"
