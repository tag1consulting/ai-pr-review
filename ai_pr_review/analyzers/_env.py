"""Allowlist-only environment for every analyzer, git and rg subprocess.

The engine process holds the LLM keys and ``GH_TOKEN``. A subprocess that
inherits them can leak them, and several analyzers run with a config from the
attacker's tree. ``tool_env`` builds the environment from nothing: only the
names in the allowlist below reach a launch.

Allowlist: ``PATH``, a ``HOME`` that is one fresh temp directory per run (not
per launch), ``LC_ALL=C.UTF-8``, ``PYTHONSAFEPATH=1``, the per-tool extras the
caller names, and the names in ``AI_ANALYZER_ENV_PASSTHROUGH`` (non-fork runs
only).

The passthrough list lives in module state, set once per run by
``configure_passthrough``. A contextvar does not work here: analyzers run in
``anyio.to_thread`` workers, which do not inherit the context.
"""

from __future__ import annotations

import atexit
import logging
import os
import re
import shutil
import tempfile
import threading
from collections.abc import Iterable, Mapping

logger = logging.getLogger(__name__)

# Names that never pass, even when named in AI_ANALYZER_ENV_PASSTHROUGH.
_BLOCKED_NAMES = frozenset(
    {
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "GOOGLE_API_KEY",
        "BEDROCK_API_KEY",
    }
)
_BLOCKED_PREFIXES = ("ACTIONS_",)
_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

_lock = threading.Lock()
_home: str | None = None
_passthrough: tuple[str, ...] = ()
_allow_passthrough = True


def is_blocked_name(name: str) -> bool:
    """True when *name* is a secret variable that must never reach a subprocess."""
    return name in _BLOCKED_NAMES or name.startswith(_BLOCKED_PREFIXES)


def configure_passthrough(names: Iterable[str], *, allow_passthrough: bool = True) -> None:
    """Set the run-wide passthrough list. Call once, before any launch."""
    global _passthrough, _allow_passthrough
    with _lock:
        _passthrough = tuple(names)
        _allow_passthrough = allow_passthrough


def _cleanup_home() -> None:
    global _home
    with _lock:
        path, _home = _home, None
    if path:
        shutil.rmtree(path, ignore_errors=True)


def run_home() -> str:
    """The one temp HOME of this run. Created on first use, removed at exit."""
    global _home
    with _lock:
        if _home is None or not os.path.isdir(_home):
            _home = tempfile.mkdtemp(prefix="ai-pr-review-home-")
            atexit.register(_cleanup_home)
        return _home


def tool_env(
    extra: Mapping[str, str] | None = None,
    passthrough: Iterable[str] | None = None,
    allow_passthrough: bool | None = None,
) -> dict[str, str]:
    """Return the scrubbed environment for one launch.

    *extra* holds per-tool variables (for example ``TFLINT_PLUGIN_DIR``).
    *passthrough* and *allow_passthrough* default to the run-wide values from
    ``configure_passthrough``. A fork run passes ``allow_passthrough=False``
    (story 17-9 wires this). A name that is a secret is refused with a warning.
    """
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": run_home(),
        "LC_ALL": "C.UTF-8",
        "PYTHONSAFEPATH": "1",
    }
    allow = _allow_passthrough if allow_passthrough is None else allow_passthrough
    names = _passthrough if passthrough is None else tuple(passthrough)
    if allow:
        for name in names:
            if not _NAME_RE.fullmatch(name):
                logger.warning("AI_ANALYZER_ENV_PASSTHROUGH: ignoring invalid name %r", name)
            elif is_blocked_name(name):
                logger.warning(
                    "AI_ANALYZER_ENV_PASSTHROUGH: refusing to pass secret variable %s", name
                )
            elif name in os.environ:
                env[name] = os.environ[name]
    if extra:
        env.update(extra)
    return env


def git_env() -> dict[str, str]:
    """Scrubbed environment for a git launch. No prompt, no user config."""
    env = tool_env()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_CONFIG_GLOBAL"] = "/dev/null"
    return env


# Put these first in every git argv: no repo hook runs, and the checkout owner may differ.
GIT_SAFE_ARGS = ("-c", "core.hooksPath=/dev/null", "-c", "safe.directory=*")


def gh_env() -> dict[str, str]:
    """Environment for ``gh``: the basics plus the token (``gh`` needs it)."""
    env = tool_env()
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        env["GH_TOKEN"] = token
    return env
