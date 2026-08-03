"""AI PR Review — Python compute engine."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("ai-pr-review")
except PackageNotFoundError:
    # Package not installed (e.g. running from a raw checkout without `pip install -e .`).
    __version__ = "0.0.0.dev0"
