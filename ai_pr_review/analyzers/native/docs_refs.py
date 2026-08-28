"""Native offline documentation-reference checker.

Catches two kinds of doc-reference rot in changed Markdown files, entirely
offline (no network calls, ever):

1. A relative link pointing at a file that doesn't exist.
2. A link's ``#anchor`` fragment pointing at a heading that doesn't exist in
   the target file.

This is hand-rolled rather than delegating to an off-the-shelf link checker
(e.g. ``lychee``) because this repo's docs are a Jekyll site using
extensionless permalinks (``[Configuration](configuration)`` resolves to
``docs/configuration.md`` at build time), and ``lychee``'s
``--fallback-extensions`` uses Rust's ``PathBuf::set_extension``, which
*replaces* an existing extension rather than appending to it — misresolving
version-numbered paths like ``version-history/v2.5.0``. See
``docs/adr/0002-hand-rolled-doc-ref-checker-not-lychee.md`` for the full
reasoning.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from ai_pr_review.findings.models import Category, Finding
from ai_pr_review.manifest import ChangedFiles

logger = logging.getLogger(__name__)

_CONFIDENCE = 80
_SOURCE = "docs-ref-check"
_CATEGORY: Category = "docs"

_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
_FENCE_RE = re.compile(r"^\s*```")
_INLINE_CODE_RE = re.compile(r"`[^`]*`")
_ATX_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_HEADING_ATTR_RE = re.compile(r"\{#([A-Za-z0-9_-]+)\}\s*$")
_URL_SKIP_RE = re.compile(r"^(https?://|mailto:)", re.IGNORECASE)

_ALWAYS_VALID_FRAGMENTS = frozenset({"top"})


def _slugify(text: str) -> str:
    """Compute a GitHub-style kebab-case slug for a heading's text."""
    slug = text.lower()
    slug = re.sub(r"[`*_\[\]()]", "", slug)
    slug = re.sub(r"\s+", "-", slug.strip())
    slug = re.sub(r"[^a-z0-9_-]", "", slug)
    return slug.strip("-")


def _normalize_anchor(value: str) -> str:
    """Normalize an anchor for lenient, case/underscore-insensitive matching."""
    return value.strip().lower().replace("_", "-")


def _collect_anchors(file_path: Path) -> set[str]:
    """Collect all valid (normalized) anchors for a Markdown file's headings.

    Includes auto-generated GitHub-style kebab slugs (with ``-1``, ``-2``, ...
    suffixes for duplicate headings, in order of appearance) plus any
    explicit ``{#custom-id}`` heading-attribute IDs.
    """
    anchors: set[str] = set()
    try:
        lines = file_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return anchors

    slug_counts: dict[str, int] = {}
    in_fence = False
    for raw_line in lines:
        if _FENCE_RE.match(raw_line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue

        heading_match = _ATX_HEADING_RE.match(raw_line)
        if not heading_match:
            continue

        heading_text = heading_match.group(2)

        custom_id: str | None = None
        attr_match = _HEADING_ATTR_RE.search(heading_text)
        if attr_match:
            custom_id = attr_match.group(1)
            heading_text = heading_text[: attr_match.start()].rstrip()

        slug = _slugify(heading_text)
        count = slug_counts.get(slug, 0)
        slug_counts[slug] = count + 1
        anchor = slug if count == 0 else f"{slug}-{count}"
        anchors.add(_normalize_anchor(anchor))

        if custom_id:
            anchors.add(_normalize_anchor(custom_id))

    return anchors


def _resolve_link_target(target_path: str, referencing_file: Path, repo_root: Path) -> Path | None:
    """Resolve a link's path portion to an existing file, or return None.

    Tries the literal target relative to the referencing file's directory
    first. Failing that, tries a fixed set of extension/index candidates —
    always by APPENDING to the target rather than replacing its existing
    suffix (the one exception being a trailing ``.html``, which represents a
    built-site suffix and is replaced with ``.md``). Each candidate is tried
    relative to the referencing file's directory first, then the repo root.
    """
    literal = referencing_file.parent / target_path
    if literal.exists():
        return literal

    candidates = [
        f"{target_path}.md",
        f"{target_path}.markdown",
        f"{target_path}/index.md",
        f"{target_path}/README.md",
    ]
    if target_path.endswith(".html"):
        candidates.append(target_path[: -len(".html")] + ".md")

    for candidate in candidates:
        resolved = referencing_file.parent / candidate
        if resolved.exists():
            return resolved

    for candidate in candidates:
        resolved = repo_root / candidate
        if resolved.exists():
            return resolved

    return None


def _run_docs_ref_check(changed_files: ChangedFiles, diff_file: Path) -> list[Finding]:
    """Check changed Markdown files for broken relative links and anchors."""
    if not changed_files.docs:
        return []

    md_files = [f for f in changed_files.docs if f.endswith(".md") and Path(f).is_file()]
    if not md_files:
        return []

    repo_root = Path.cwd()
    anchor_cache: dict[Path, set[str]] = {}
    findings: list[Finding] = []

    for doc in md_files:
        referencing_file = Path(doc)
        display_file = doc[2:] if doc.startswith("./") else doc

        try:
            lines = referencing_file.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            logger.warning("[ai-pr-review] WARNING: docs-ref-check failed to read %s: %s", doc, exc)
            continue

        in_fence = False
        for lineno, raw_line in enumerate(lines, start=1):
            if _FENCE_RE.match(raw_line):
                in_fence = not in_fence
                continue
            if in_fence:
                continue

            scan_line = _INLINE_CODE_RE.sub("", raw_line)

            for match in _LINK_RE.finditer(scan_line):
                target = match.group(1)
                if not target or _URL_SKIP_RE.match(target) or "{{" in target:
                    continue

                target_path, _, fragment = target.partition("#")

                if target_path:
                    resolved = _resolve_link_target(target_path, referencing_file, repo_root)
                    if resolved is None:
                        try:
                            findings.append(
                                Finding(
                                    severity="Medium",
                                    confidence=_CONFIDENCE,
                                    source=_SOURCE,
                                    category=_CATEGORY,
                                    file=display_file,
                                    line=lineno,
                                    finding=(
                                        f"Link target '{target_path}' does not resolve "
                                        "to an existing file"
                                    ),
                                    remediation="Fix or remove the broken link.",
                                )
                            )
                        except (ValueError, TypeError) as exc:
                            logger.warning(
                                "[ai-pr-review] WARNING: docs-ref-check dropped malformed "
                                "finding: %s",
                                exc,
                            )
                        # The link itself is broken; there's no reliable target
                        # file left to validate the anchor against.
                        continue
                    anchor_target_file = resolved
                else:
                    anchor_target_file = referencing_file

                if not fragment or fragment.lower() in _ALWAYS_VALID_FRAGMENTS:
                    continue

                if anchor_target_file not in anchor_cache:
                    anchor_cache[anchor_target_file] = _collect_anchors(anchor_target_file)

                if _normalize_anchor(fragment) not in anchor_cache[anchor_target_file]:
                    try:
                        findings.append(
                            Finding(
                                severity="Medium",
                                confidence=_CONFIDENCE,
                                source=_SOURCE,
                                category=_CATEGORY,
                                file=display_file,
                                line=lineno,
                                finding=(
                                    f"Anchor '#{fragment}' does not match any heading "
                                    "in the target file"
                                ),
                                remediation="Fix the anchor or update the target heading.",
                            )
                        )
                    except (ValueError, TypeError) as exc:
                        logger.warning(
                            "[ai-pr-review] WARNING: docs-ref-check dropped malformed "
                            "finding: %s",
                            exc,
                        )

    return findings
