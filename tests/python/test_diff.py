"""Tests for ai_pr_review.diff — position math, eligibility oracle."""

import pytest

from ai_pr_review.diff.eligibility import eligible_inline_lines, is_eligible
from ai_pr_review.diff.linemap import LineRef, parse_added_lines, parse_new_file_lines

_SIMPLE_DIFF = """\
diff --git a/foo.py b/foo.py
index 0000000..1111111 100644
--- a/foo.py
+++ b/foo.py
@@ -1,3 +1,5 @@
 context line
+added line one
+added line two
 another context
-removed line
+replacement line
"""


def test_parse_added_lines_basic() -> None:
    added = parse_added_lines(_SIMPLE_DIFF)
    assert LineRef("foo.py", 2) in added  # "added line one"
    assert LineRef("foo.py", 3) in added  # "added line two"
    assert LineRef("foo.py", 5) in added  # "replacement line"
    # context lines must NOT be included
    assert LineRef("foo.py", 1) not in added
    assert LineRef("foo.py", 4) not in added


def test_parse_new_file_lines_includes_context() -> None:
    new_lines = parse_new_file_lines(_SIMPLE_DIFF)
    # Context lines present in new file
    assert LineRef("foo.py", 1) in new_lines
    assert LineRef("foo.py", 4) in new_lines
    # Added lines also present
    assert LineRef("foo.py", 2) in new_lines


def test_deleted_lines_not_in_added() -> None:
    diff = """\
diff --git a/bar.go b/bar.go
index 0000000..1111111 100644
--- a/bar.go
+++ b/bar.go
@@ -1,2 +1,1 @@
-deleted line
 kept line
"""
    added = parse_added_lines(diff)
    # No + lines in this diff
    assert len(added) == 0


def test_multiple_files() -> None:
    diff = """\
diff --git a/alpha.py b/alpha.py
index 0000000..1111111 100644
--- a/alpha.py
+++ b/alpha.py
@@ -1,1 +1,2 @@
 existing
+new alpha
diff --git a/beta.py b/beta.py
index 0000000..1111111 100644
--- a/beta.py
+++ b/beta.py
@@ -1,1 +1,2 @@
 existing
+new beta
"""
    added = parse_added_lines(diff)
    assert LineRef("alpha.py", 2) in added
    assert LineRef("beta.py", 2) in added


def test_is_eligible_true() -> None:
    assert is_eligible(_SIMPLE_DIFF, "foo.py", 2) is True


def test_is_eligible_false_context() -> None:
    assert is_eligible(_SIMPLE_DIFF, "foo.py", 1) is False


def test_is_eligible_wrong_file() -> None:
    assert is_eligible(_SIMPLE_DIFF, "other.py", 2) is False


def test_eligible_inline_lines_returns_set() -> None:
    lines = eligible_inline_lines(_SIMPLE_DIFF)
    assert isinstance(lines, set)
    assert all(isinstance(r, LineRef) for r in lines)


def test_hunk_with_offset() -> None:
    """Verify hunk line number parsing when @@ shows offset > 1."""
    diff = """\
diff --git a/src/main.py b/src/main.py
index 0000000..1111111 100644
--- a/src/main.py
+++ b/src/main.py
@@ -10,3 +10,4 @@
 line ten
+inserted at eleven
 line twelve
 line thirteen
"""
    added = parse_added_lines(diff)
    assert LineRef("src/main.py", 11) in added
    # context at line 10 not included
    assert LineRef("src/main.py", 10) not in added
