"""Shared run-context block: PR title/description + file manifest, built once
per run and sent to every finding agent except blind-hunter (#813, closing
the remaining gap in #177 -- the Python engine never carried the PR
description over from the bash engine).

Fetched via `VcsProvider.get_pr_description()`, which is itself fail-soft
(returns None rather than raising); this module is a pure formatter that
never touches the network.
"""

from __future__ import annotations

import re

# PR/MR descriptions routinely carry HTML comments -- template boilerplate
# ("<!-- Delete sections that don't apply -->"), collapsed
# checklists/details wrappers, or hidden markers other tools left behind.
# None of that is meant for a reader (human or model); stripping it keeps
# the block's token cost tied to the actually-authored content.
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)

# A generous cap, not a token budget: real PR descriptions are typically a
# few hundred to a couple thousand characters, and this exists to bound the
# rare template-heavy or copy-pasted-log outlier, not to compete with the
# diff/manifest for space. ~1,000 tokens at this engine's own 4-chars/token
# estimate (ai_pr_review.context.budget.estimate_tokens).
DEFAULT_MAX_BODY_CHARS = 4000


def build_shared_context_block(
    *,
    manifest_text: str,
    pr_title: str,
    pr_body: str,
    max_body_chars: int = DEFAULT_MAX_BODY_CHARS,
) -> str:
    """Build the `<pr-context>` block shared across every eligible agent.

    Returns "" when there is nothing to show (no title, no body, and no
    manifest) -- callers should skip adding an empty prefix part rather than
    emit an empty wrapper.
    """
    title = pr_title.strip()
    body = _HTML_COMMENT_RE.sub("", pr_body).strip()
    if len(body) > max_body_chars:
        body = body[:max_body_chars].rstrip() + "\n\n_[description truncated]_"
    manifest = manifest_text.strip()

    sections: list[str] = []
    if title:
        sections.append(f"## PR Title\n\n{title}")
    if body:
        sections.append(f"## PR Description\n\n{body}")
    if manifest:
        sections.append(f"## Changed Files\n\n{manifest}")

    if not sections:
        return ""

    return "<pr-context>\n" + "\n\n".join(sections) + "\n</pr-context>"
