"""Tests for ai_pr_review.languages — mirrors lib/languages.sh bats tests."""

import pytest

from ai_pr_review.languages import detect_language, is_test_file


@pytest.mark.parametrize(
    "ext,expected",
    [
        ("go", "Go"),
        ("py", "Python"),
        ("js", "JavaScript"),
        ("jsx", "JavaScript"),
        ("ts", "TypeScript"),
        ("tsx", "TypeScript"),
        ("php", "PHP"),
        ("module", "PHP"),
        ("theme", "PHP"),
        ("inc", "PHP"),
        ("tf", "Terraform"),
        ("tfvars", "Terraform"),
        ("sh", "Shell"),
        ("bash", "Shell"),
        ("yaml", "YAML"),
        ("yml", "YAML"),
        ("rb", "Ruby"),
        ("rake", "Ruby"),
        ("gemspec", "Ruby"),
        ("rs", "Rust"),
        ("java", "Java"),
        ("c", "C++"),
        ("h", "C++"),
        ("cpp", "C++"),
        ("hpp", "C++"),
        ("cc", "C++"),
        ("cxx", "C++"),
        ("txt", ""),
        ("md", ""),
        ("", ""),
        ("unknown_ext", ""),
    ],
)
def test_detect_language(ext: str, expected: str) -> None:
    assert detect_language(ext) == expected


@pytest.mark.parametrize(
    "path,expected",
    [
        ("foo_test.go", True),
        ("pkg/foo_test.go", True),
        ("test_foo.py", True),
        ("foo_test.py", True),
        ("src/test_bar.py", True),
        ("foo.test.js", True),
        ("foo.spec.js", True),
        ("foo.test.ts", True),
        ("foo.spec.tsx", True),
        ("foo_spec.rb", True),
        ("foo_test.rb", True),
        ("FooTest.java", True),
        ("FooTestBase.php", True),
        ("FooTest.php", True),
        ("foo_test.cpp", True),
        ("foo_test.cc", True),
        ("tests/something.py", True),
        ("test/something.go", True),
        ("spec/something.rb", True),
        # non-test files
        ("foo.go", False),
        ("lib/foo.py", False),
        ("src/main.ts", False),
        ("review.sh", False),
    ],
)
def test_is_test_file(path: str, expected: bool) -> None:
    assert is_test_file(path) == expected
