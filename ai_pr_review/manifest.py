"""File manifest construction — ports lib/diff.sh build_file_manifest.

Produces a typed ChangedFiles categorization and a text MANIFEST string
matching the bash output format consumed by agents.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from ai_pr_review.languages import detect_language, is_test_file

# Manifest-file basenames tracked for CVE/dependency analysis.
_MANIFEST_BASENAMES: frozenset[str] = frozenset(
    {
        "go.mod",
        "go.sum",
        "package.json",
        "package-lock.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "requirements.txt",
        "requirements-dev.txt",
        "requirements-test.txt",
        "Pipfile",
        "Pipfile.lock",
        "poetry.lock",
        "pyproject.toml",
        "uv.lock",
        "composer.json",
        "composer.lock",
        "Gemfile",
        "Gemfile.lock",
        "Cargo.toml",
        "Cargo.lock",
    }
)

_CONFIG_PATTERN = re.compile(
    r"\.(yml|yaml|json|toml|cfg|ini|env)$|Makefile$|Dockerfile$|\.github/"
)
_DOC_PATTERN = re.compile(r"\.(md|txt|rst)$")


@dataclass
class ChangedFiles:
    """Categorized sets of changed file paths."""

    all_files: list[str] = field(default_factory=list)
    source: list[str] = field(default_factory=list)
    tests: list[str] = field(default_factory=list)
    config: list[str] = field(default_factory=list)
    docs: list[str] = field(default_factory=list)
    deps: list[str] = field(default_factory=list)

    # Typed per-language sets (used by analyzer bridge for file-type gating)
    shell: list[str] = field(default_factory=list)
    python: list[str] = field(default_factory=list)
    go: list[str] = field(default_factory=list)
    php: list[str] = field(default_factory=list)
    terraform: list[str] = field(default_factory=list)
    dockerfile: list[str] = field(default_factory=list)
    iac: list[str] = field(default_factory=list)
    js_ts: list[str] = field(default_factory=list)
    manifest_lockfile: list[str] = field(default_factory=list)

    @property
    def languages(self) -> list[str]:
        seen: set[str] = set()
        result: list[str] = []
        for f in self.all_files:
            ext = Path(f).suffix.lstrip(".")
            lang = detect_language(ext)
            if lang and lang not in seen:
                seen.add(lang)
                result.append(lang)
        return result


def parse_changed_files_payload(raw: list[object]) -> ChangedFiles:
    """Normalize the raw changed_files payload from compute and build ChangedFiles.

    Payload entries may be path strings or dicts with a "path" key (forward-compatible
    compute schema). Malformed entries are skipped with a warning.
    """
    import logging
    _log = logging.getLogger(__name__)
    paths: list[str] = []
    for entry in raw:
        if entry is None:
            _log.warning("Skipping malformed changed_files entry: %r", entry)
            continue
        path = (
            str(entry.get("path") or "") if isinstance(entry, dict) else str(entry)
        )
        if path:
            paths.append(path)
        else:
            _log.warning("Skipping malformed changed_files entry: %r", entry)
    return build_changed_files(paths)


def build_changed_files(file_list: list[str]) -> ChangedFiles:
    """Categorize a list of changed file paths."""
    cf = ChangedFiles(all_files=file_list)

    for f in file_list:
        basename = os.path.basename(f)
        ext = Path(f).suffix.lstrip(".")
        lang = detect_language(ext)

        if basename in _MANIFEST_BASENAMES:
            cf.manifest_lockfile.append(f)
            cf.deps.append(f)
            continue

        # Dockerfile (no extension)
        if basename == "Dockerfile" or re.search(r"Dockerfile\.", basename):
            cf.dockerfile.append(f)
            cf.config.append(f)
            continue

        if is_test_file(f):
            cf.tests.append(f)
        elif _DOC_PATTERN.search(f):
            cf.docs.append(f)
        elif _CONFIG_PATTERN.search(f):
            cf.config.append(f)
        else:
            cf.source.append(f)

        # Per-language typed lists
        if lang == "Shell":
            cf.shell.append(f)
        elif lang == "Python":
            cf.python.append(f)
        elif lang == "Go":
            cf.go.append(f)
        elif lang == "PHP":
            cf.php.append(f)
        elif lang == "Terraform":
            cf.terraform.append(f)
        elif lang in ("JavaScript", "TypeScript"):
            cf.js_ts.append(f)

        # IaC: Kubernetes/Helm YAML heuristic
        if re.search(r"(k8s|kubernetes|helm|charts?|manifests?)/.*\.(ya?ml)$", f):
            cf.iac.append(f)

    return cf


def build_manifest_text(
    cf: ChangedFiles,
    base_ref: str,
    diff_label: str,
    diff_stat: str,
) -> str:
    """Produce the text MANIFEST string passed to agents."""
    languages_str = ", ".join(cf.languages) if cf.languages else "unknown"
    file_count = len(cf.all_files)

    lines = [
        f"BASE: {base_ref} | DIFF: {diff_label} | LANGUAGES: {languages_str} | FILES: {file_count} | {diff_stat}"
    ]

    def _fmt(label: str, items: list[str], limit: int) -> str:
        truncated = items[:limit]
        return f"{label}: {', '.join(truncated)}"

    if cf.source:
        lines.append("")
        lines.append(_fmt("Source", cf.source, 20))
    if cf.tests:
        lines.append(_fmt("Tests", cf.tests, 10))
    if cf.config:
        lines.append(_fmt("Config", cf.config, 10))
    if cf.docs:
        lines.append(_fmt("Docs", cf.docs, 10))

    return "\n".join(lines)
