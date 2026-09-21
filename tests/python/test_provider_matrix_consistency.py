"""Guards against the provider capability matrix drifting between its three
copies: README.md, docs/configuration.md, docs/getting-started.md. Each has
gone stale at least once per feature so far (see the Bitbucket parity plan's
docs section) -- this greps the Bitbucket row out of all three and asserts
the Inline/Suggestions/Approval cells agree, so a fourth drift is a test
failure instead of a doc bug someone notices later.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent

_MATRIX_FILES = {
    "README.md": _REPO_ROOT / "README.md",
    "docs/configuration.md": _REPO_ROOT / "docs" / "configuration.md",
    "docs/getting-started.md": _REPO_ROOT / "docs" / "getting-started.md",
}

_ROW_RE = re.compile(
    r"^\|\s*Bitbucket Cloud\s*\|\s*`bitbucket`\s*\|(?P<summary>[^|]*)\|(?P<inline>[^|]*)\|(?P<suggestions>[^|]*)\|(?P<approval>[^|]*)\|",
    re.MULTILINE,
)


def _bitbucket_row(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    match = _ROW_RE.search(text)
    assert match is not None, f"no Bitbucket Cloud capability-matrix row found in {path}"
    return {k: v.strip() for k, v in match.groupdict().items()}


# README.md uses ✅/❌; docs/configuration.md and docs/getting-started.md use
# Yes/No -- a pre-existing, deliberate style split between the two doc
# families, not a drift. README.md also links to docs/bitbucket-setup.md
# with the "docs/" prefix (it lives at repo root) while the other two link
# to it as a sibling file (they already live under docs/) -- also not a
# drift, just a correct relative path from two different locations. Reduce
# each cell to its leading yes/no verdict for the equality check below; the
# explanatory note's *content* (not its exact wording or link path) is
# checked separately by the second test.
_YES = {"✅", "yes"}
_NO = {"❌", "no"}


def _verdict(cell: str) -> str:
    lowered = cell.lower()
    for marker, values in (("yes", _YES), ("no", _NO)):
        for value in values:
            if lowered == value.lower() or lowered.startswith(value.lower() + " "):
                return marker
    return cell


def test_bitbucket_capability_row_matches_across_all_three_docs() -> None:
    rows = {name: _bitbucket_row(path) for name, path in _MATRIX_FILES.items()}
    verdicts = {name: {k: _verdict(v) for k, v in row.items()} for name, row in rows.items()}
    names = list(verdicts)
    first = verdicts[names[0]]
    for name in names[1:]:
        assert verdicts[name] == first, (
            f"Bitbucket capability-matrix row in {name} disagrees with "
            f"{names[0]}: {verdicts[name]!r} != {first!r}"
        )


def test_bitbucket_inline_cell_says_code_insights_not_plain_yes() -> None:
    """Phase 3 (#839/#873): the Inline cell must name the actual mechanism
    (Code Insights annotations), not a bare Yes/No that would look identical
    to GitHub/GitLab's real inline PR comments and hide that the mechanism
    differs."""
    for name, path in _MATRIX_FILES.items():
        row = _bitbucket_row(path)
        assert "code insights" in row["inline"].lower(), (
            f"{name}'s Bitbucket Inline cell doesn't mention Code Insights: "
            f"{row['inline']!r}"
        )
