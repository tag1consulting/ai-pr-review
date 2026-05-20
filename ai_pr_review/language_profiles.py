"""Language-profile loader.

Mirrors lib/diff.sh:297-306: for each detected language label, reads the
corresponding markdown file from <script_dir>/language-profiles/<lower>.md and
concatenates the contents.  Files that do not exist are silently skipped,
matching the bash engine's behaviour.  The '+' character in labels (e.g.
"C++") is preserved through lowercasing so "c++.md" resolves correctly.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from pathlib import Path

_log = logging.getLogger(__name__)


def load_language_profiles(labels: Iterable[str], script_dir: Path) -> str:
    """Return concatenated language-profile markdown for the given labels.

    Args:
        labels: Language labels as returned by ``detect_language()``
                (e.g. ``["Python", "TypeScript"]``).
        script_dir: Root directory of the ai-pr-review installation, the
                    same path stored in ``DispatchContext.script_dir``.

    Returns:
        Newline-joined content of all found profile files, or an empty
        string when no profiles are found.
    """
    label_list = list(labels)
    parts: list[str] = []
    profiles_dir = script_dir / "language-profiles"
    for label in label_list:
        path = profiles_dir / f"{label.lower()}.md"
        if path.is_file():
            try:
                parts.append(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError) as exc:
                _log.warning("Skipping language profile for %s (%s): %s", label, path, exc, exc_info=True)
    if not parts and label_list:
        _log.warning(
            "language_profiles: no profiles loaded for %s; check AI_PR_REVIEW_SCRIPT_DIR (%s)",
            label_list, script_dir,
        )
    return "\n".join(parts)
