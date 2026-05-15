"""Ripgrep-based cross-file symbol definition lookup — E3.S2.

Uses ``rg`` (ripgrep) to locate definitions for referenced symbols across the
repo checkout.  Returns surrounding ±``lookup_lines`` lines per definition,
with a per-query 3-second timeout and a per-run 50-query cap.

Results are cached per run so the same symbol looked up by multiple agents
only triggers one ripgrep call.

Gated by ``AI_CONTEXT_ENRICHMENT=1`` — callers must check the flag.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from ai_pr_review.context.treesitter import SymbolRef

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Maps language → file extensions for ripgrep --type-add / --glob filtering.
# Keeps searches language-constrained so Python refs don't grep JS files.
# ---------------------------------------------------------------------------
_LANG_EXTENSIONS: dict[str, list[str]] = {
    "python": ["py"],
    "typescript": ["ts", "tsx"],
    "tsx": ["ts", "tsx"],
    "javascript": ["js", "jsx", "mjs", "cjs"],
    "go": ["go"],
    "php": ["php", "module", "inc", "theme"],
    "ruby": ["rb", "rake", "gemspec"],
    "rust": ["rs"],
    "bash": ["sh", "bash"],
    "shell": ["sh", "bash"],
    "java": ["java"],
    "cpp": ["cpp", "cc", "cxx", "hpp", "h"],
    "c": ["c", "h"],
}


# Proximity tiers used for sorting/truncation across the context pipeline.
# Single source of truth — budget.py imports from here.
PROXIMITY_ORDER: dict[str, int] = {"same-file": 0, "same-package": 1, "repo": 2}
PROXIMITY_DEFAULT: int = 3  # for unknown/unset proximity values


@dataclass(frozen=True)
class Definition:
    """A symbol definition found by ripgrep."""

    symbol: str
    file: str   # relative to repo_root
    line: int   # 1-based
    snippet: str  # ±lookup_lines context
    proximity: str  # "same-file" | "same-package" | "repo"


@dataclass
class _LookupCache:
    """Per-run cache so each symbol is looked up at most once."""

    _store: dict[str, list[Definition]] = field(default_factory=dict)
    _queries: int = 0

    def get(self, key: str) -> list[Definition] | None:
        return self._store.get(key)

    def set(self, key: str, defs: list[Definition]) -> None:
        self._store[key] = defs
        self._queries += 1

    @property
    def query_count(self) -> int:
        return self._queries


# Module-level cache — reset by tests via _reset_cache().
_cache = _LookupCache()


def _reset_cache() -> None:
    """Reset the per-run lookup cache (used by tests)."""
    global _cache
    _cache = _LookupCache()


def _classify_proximity(
    def_file: str, changed_files: list[str]
) -> str:
    """Classify a definition's proximity relative to the changed files."""
    if def_file in changed_files:
        return "same-file"
    # Same package = same directory
    def_dir = str(Path(def_file).parent)
    for cf in changed_files:
        if str(Path(cf).parent) == def_dir:
            return "same-package"
    return "repo"


def _read_snippet(file_path: Path, lineno: int, lookup_lines: int) -> str:
    """Read ±lookup_lines around lineno from file_path, safe against large files.

    Returns "" on OSError (file disappeared between ripgrep and read, permission
    issue, etc.) and logs at debug level.  Callers should treat an empty result
    as "no snippet available" and skip the Definition rather than emit an
    empty code fence into the <symbol-context> block.
    """
    try:
        lines = file_path.read_text(errors="replace").splitlines()
    except OSError as exc:
        logger.debug(
            "context enrichment: could not read snippet from %s: %s",
            file_path, exc,
        )
        return ""
    start = max(0, lineno - 1 - lookup_lines)
    end = min(len(lines), lineno + lookup_lines)
    return "\n".join(lines[start:end])


def _glob_patterns(language: str) -> list[str]:
    exts = _LANG_EXTENSIONS.get(language.lower(), [])
    if not exts:
        return []
    return [f"--glob=*.{ext}" for ext in exts]


def lookup_definitions(
    refs: list[SymbolRef],
    repo_root: Path,
    changed_files: list[str],
    language: str = "",
    *,
    lookup_lines: int = 8,
    max_queries: int = 50,
    timeout_s: float = 3.0,
) -> list[Definition]:
    """Look up definitions for *refs* via ripgrep.

    Returns all found definitions, sorted by proximity (same-file first).
    Skips queries after *max_queries* to bound runtime.  All ripgrep errors
    are fail-soft: logged as WARNING, definition list truncated.
    """
    if not refs:
        return []

    rg = shutil.which("rg")
    if rg is None:
        logger.warning(
            "ripgrep (rg) not found; symbol lookup disabled. "
            "Install ripgrep to enable context enrichment."
        )
        return []

    repo_root = repo_root.resolve()
    glob_args = _glob_patterns(language)
    all_defs: list[Definition] = []
    # Track how many candidate definitions we dropped because _read_snippet
    # could not produce a snippet (file disappeared, permission denied, etc.).
    # We log a single aggregate WARNING at the end if the rate is significant.
    skipped_no_snippet = 0
    total_candidates = 0

    for ref in refs:
        if _cache.query_count >= max_queries:
            logger.warning(
                "context enrichment: max_queries=%d reached; remaining symbols skipped",
                max_queries,
            )
            break

        # Include repo_root in the cache key — without it, two repos with a
        # function named `process` would share results in a long-lived process
        # (container reuse / future server mode).  Use NUL as separator since
        # it cannot appear in identifiers, language names, or POSIX paths.
        cache_key = f"{repo_root}\x00{ref.name}\x00{language}"
        cached = _cache.get(cache_key)
        if cached is not None:
            all_defs.extend(cached)
            continue

        # Build pattern: match definition-like lines (function/class/var decl)
        # Uses a broad pattern that works across languages; tree-sitter already
        # filtered to genuine identifiers so noise is low.
        pattern = rf"\b{re.escape(ref.name)}\b"

        cmd = [
            rg,
            "--line-number",
            "--no-heading",
            "--max-count=5",  # at most 5 matches per symbol
            pattern,
            *glob_args,
            str(repo_root),
        ]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout_s,
                cwd=str(repo_root),
            )
        except subprocess.TimeoutExpired:
            logger.warning(
                "context enrichment: ripgrep timed out for symbol %r", ref.name
            )
            _cache.set(cache_key, [])
            continue
        except OSError as exc:
            logger.warning("context enrichment: ripgrep error for %r: %s", ref.name, exc)
            _cache.set(cache_key, [])
            continue

        # ripgrep exit codes: 0 = match, 1 = no match (normal), 2 = error.
        if result.returncode == 2:
            logger.warning(
                "context enrichment: ripgrep error (exit 2) for symbol %r: %s",
                ref.name,
                (result.stderr or "")[:200],
            )
            _cache.set(cache_key, [])
            continue

        defs: list[Definition] = []
        for line in result.stdout.splitlines():
            # Format: /abs/path/file.py:42:matched line content
            parts = line.split(":", 2)
            if len(parts) < 3:
                continue
            abs_file, lineno_str, _ = parts
            try:
                lineno = int(lineno_str)
            except ValueError:
                continue

            abs_path = Path(abs_file)
            # Security: ensure the file is within repo_root
            try:
                abs_path.resolve().relative_to(repo_root)
            except ValueError:
                continue

            try:
                rel_file = str(abs_path.resolve().relative_to(repo_root))
            except ValueError:
                continue

            total_candidates += 1
            snippet = _read_snippet(abs_path, lineno, lookup_lines)
            if not snippet:
                # Skip definitions with no readable snippet — emitting an
                # empty code fence into the <symbol-context> block would
                # be pure noise.  We aggregate the skip count and emit a
                # single WARNING below if the rate is high.
                skipped_no_snippet += 1
                continue
            proximity = _classify_proximity(rel_file, changed_files)
            defs.append(
                Definition(
                    symbol=ref.name,
                    file=rel_file,
                    line=lineno,
                    snippet=snippet,
                    proximity=proximity,
                )
            )

        _cache.set(cache_key, defs)
        all_defs.extend(defs)

    # If we dropped a significant fraction of candidates because their files
    # were unreadable, surface that as a single WARNING.  Per-file DEBUG logs
    # in _read_snippet already record the individual paths.
    if total_candidates >= 4 and skipped_no_snippet * 2 >= total_candidates:
        logger.warning(
            "context enrichment: dropped %d of %d candidate definitions "
            "(unreadable snippets) — context block may be sparse",
            skipped_no_snippet, total_candidates,
        )

    # Sort: same-file → same-package → repo
    all_defs.sort(key=lambda d: PROXIMITY_ORDER.get(d.proximity, PROXIMITY_DEFAULT))
    return all_defs
