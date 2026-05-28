"""Body-finding ID assignment — stable, PR-wide, monotonic.

Body-level findings (those that cannot be anchored to a diff line) are
rendered with stable numeric IDs, e.g. ``**[F1]**``, ``**[F2]**``.

IDs are:
- **PR-wide** — scoped to the pull request, not to any single review.
- **Monotonically increasing** — new findings introduced by a later review
  get the next unused ID; IDs from earlier reviews are never recycled.
- **Fingerprint-stable** — a finding re-detected across reviews keeps its
  original ID because IDs are derived from a content fingerprint, not a
  positional counter.
- **Stateless** — no side-channel store; the ID map is reconstructed at
  render time by scanning prior bot review bodies.

Fingerprint
-----------
The fingerprint is the tuple ``(source, file, line, sha256(finding_text)[:12])``
joined with ``|``.  Only the first 12 hex digits of the text hash are used
(collision probability negligible for the volumes involved).  The fingerprint
is stable against trivial reformatting of the finding text.

ID-map assembly
---------------
``assemble_id_map(prior_bodies, current_findings)`` is a pure function:
it reads the already-fetched review bodies, parses ``**[F<n>]**`` labels and
their adjacent bullet text, rebuilds the fingerprint → ID mapping, then
assigns the next available counter to any current finding that is not yet in
the map.  The caller is responsible for fetching the review bodies (I/O is
intentionally outside this module so it can be tested without HTTP stubs).

Regex contract
--------------
A body-level bullet is rendered as:

    - {icon} **[severity]** **[F{n}]** [{source}] {text}...

The parser only requires that ``**[F{n}]**`` appears in the bullet line;
the rest of the line is used as the "text" portion of the fingerprint
reconstruction.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence

from ai_pr_review.findings.models import Finding

# Matches a body-finding ID token, e.g. **[F3]** (case-insensitive for
# robustness, though we always emit upper-case F).
_ID_RE = re.compile(r"\*\*\[F(\d+)\]\*\*", re.IGNORECASE)

# Matches the source tag, e.g. [code-reviewer] or [code-reviewer, security-reviewer]
_SOURCE_RE = re.compile(r"\[([^\]]+)\]")

# Matches file:line location at end of bullet, e.g. *(at `foo.py:10`...)*
_LOCATION_RE = re.compile(r"\*\(at `([^`]+)`")


def fingerprint(f: Finding) -> str:
    """Return a stable fingerprint for a body-level finding.

    The fingerprint encodes ``source|file|line|text_hash`` so that the same
    logical finding produces the same string across review runs, enabling
    ID stability.
    """
    source = (f.source or "").strip()
    file_ = (f.file or "").strip()
    line = str(f.line) if f.line is not None else ""
    text_hash = hashlib.sha256((f.finding or "").encode()).hexdigest()[:12]
    return f"{source}|{file_}|{line}|{text_hash}"


def _parse_existing_ids(bodies: Sequence[str]) -> dict[str, int]:
    """Scan prior review bodies and return a fingerprint → ID mapping.

    Prefers the machine-readable ``<!-- ai-pr-review-id-map: {...} -->``
    marker embedded in each review body.  Falls back to parsing ``**[F<n>]**``
    bullet tokens when the marker is absent (backward compatibility with
    reviews posted before this feature was added).

    Lines that cannot be parsed are skipped silently — partial information
    is better than aborting the render.
    """
    from ai_pr_review.vcs.marker import extract_id_map

    fp_to_id: dict[str, int] = {}
    for body in bodies:
        # Fast path: authoritative map embedded by the renderer.
        marker_map = extract_id_map(body)
        if marker_map:
            for fp, fid in marker_map.items():
                if fp not in fp_to_id:
                    fp_to_id[fp] = fid
            continue

        # Fallback: parse **[F<n>]** tokens from body-findings bullets.
        # Only covers body-level findings (inline finding IDs are not in
        # bullet form); use this path only for pre-marker reviews.
        in_body_section = False
        for line in body.splitlines():
            stripped = line.strip()
            if "### Findings not attached to specific lines" in stripped:
                in_body_section = True
                continue
            if in_body_section and stripped.startswith("###"):
                in_body_section = False
                continue
            if not in_body_section:
                continue
            if not stripped.startswith("- "):
                continue

            id_match = _ID_RE.search(stripped)
            if not id_match:
                continue
            finding_id = int(id_match.group(1))

            # Reconstruct fingerprint from bullet.
            source = ""
            after_id = stripped[id_match.end():]
            src_m = _SOURCE_RE.search(after_id)
            if src_m:
                source = src_m.group(1).split(",")[0].strip()

            file_ = ""
            line_no = ""
            loc_m = _LOCATION_RE.search(stripped)
            if loc_m:
                loc_str = loc_m.group(1)
                if ":" in loc_str:
                    file_, _, line_no = loc_str.rpartition(":")
                    if not line_no.isdigit():
                        file_ = loc_str
                        line_no = ""
                else:
                    file_ = loc_str

            text = after_id
            if src_m:
                text = after_id[src_m.end():]
            loc_idx = text.find(" *(at `")
            if loc_idx >= 0:
                text = text[:loc_idx]
            text = text.strip()
            text_hash = hashlib.sha256(text.encode()).hexdigest()[:12]

            fp = f"{source}|{file_}|{line_no}|{text_hash}"
            if fp not in fp_to_id:
                fp_to_id[fp] = finding_id

    return fp_to_id


def assemble_id_map(
    prior_bodies: Sequence[str],
    current_findings: Sequence[Finding],
) -> dict[str, int]:
    """Build a fingerprint → ID map for *current_findings*.

    Any finding whose fingerprint already appears in a prior review keeps its
    existing ID.  New findings are assigned IDs starting at
    ``max(existing_ids) + 1``, or at 1 if there are no prior IDs.

    Parameters
    ----------
    prior_bodies:
        Rendered markdown bodies of all prior ``github-actions[bot]`` reviews
        on the PR.  Pass an empty sequence if this is the first review.
    current_findings:
        The body-level findings about to be rendered in the current review.

    Returns
    -------
    A dict mapping ``fingerprint(f)`` → ``int`` for every finding in
    *current_findings*.
    """
    existing = _parse_existing_ids(prior_bodies)
    next_id = max(existing.values(), default=0) + 1

    result: dict[str, int] = {}
    for f in current_findings:
        fp = fingerprint(f)
        if fp in existing:
            result[fp] = existing[fp]
        else:
            result[fp] = next_id
            next_id += 1

    return result
