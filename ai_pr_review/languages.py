"""Language detection and test-file classification.

Ports lib/languages.sh: detect_language() and is_test_file().
"""

from __future__ import annotations

import re

# Extension → language label (matches language-profiles/<name>.md filenames).
_EXT_MAP: dict[str, str] = {
    "go": "Go",
    "py": "Python",
    "js": "JavaScript",
    "jsx": "JavaScript",
    "ts": "TypeScript",
    "tsx": "TypeScript",
    "php": "PHP",
    "module": "PHP",
    "theme": "PHP",
    "inc": "PHP",
    "tf": "Terraform",
    "tfvars": "Terraform",
    "sh": "Shell",
    "bash": "Shell",
    "yaml": "YAML",
    "yml": "YAML",
    "rb": "Ruby",
    "rake": "Ruby",
    "gemspec": "Ruby",
    "rs": "Rust",
    "java": "Java",
    "c": "C++",
    "h": "C++",
    "cpp": "C++",
    "hpp": "C++",
    "cc": "C++",
    "cxx": "C++",
}

# Patterns for test-file classification (mirrors lib/languages.sh is_test_file).
_TEST_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"_test\.go$"),
    re.compile(r"(^|/)test_[^/]+\.py$"),
    re.compile(r"_test\.py$"),
    re.compile(r"\.(test|spec)\.[jt]sx?$"),
    re.compile(r"_spec\.rb$"),
    re.compile(r"_test\.rb$"),
    re.compile(r"Test\.java$"),
    re.compile(r"Test(Base)?\.php$"),
    re.compile(r"_test\.(cpp|cc|ts)$"),
    re.compile(r"(^|/)tests?/"),
    re.compile(r"(^|/)spec/"),
]


def detect_language(extension: str) -> str:
    """Return language label for a file extension, or '' if unknown."""
    return _EXT_MAP.get(extension.lower(), "")


def is_test_file(path: str) -> bool:
    """Return True if the path looks like a test file."""
    return any(p.search(path) for p in _TEST_PATTERNS)
