#!/usr/bin/env bats
# Tests for parse_valid_lines in post-review.sh.
# The function reads a diff and emits "file:line" for every added line
# in the new file that is a valid inline comment target.

setup() {
  load test_helper
  load_function "${PROJECT_ROOT}/post-review.sh" parse_valid_lines
  DIFF_FILE=$(mktemp)
}

teardown() {
  rm -f "$DIFF_FILE"
}

# ---------------------------------------------------------------------------
# Basic added-line tracking
# ---------------------------------------------------------------------------

@test "parse_valid_lines: emits file:line for added lines" {
  cat > "$DIFF_FILE" <<'EOF'
diff --git a/foo.sh b/foo.sh
--- a/foo.sh
+++ b/foo.sh
@@ -1,2 +1,3 @@
 unchanged
+added line
 another unchanged
EOF
  run parse_valid_lines "$DIFF_FILE"
  [ "$status" -eq 0 ]
  # The added line is at new-file line 2
  echo "$output" | grep -qF "foo.sh:2"
}

@test "parse_valid_lines: does not emit lines for context lines" {
  cat > "$DIFF_FILE" <<'EOF'
diff --git a/foo.sh b/foo.sh
--- a/foo.sh
+++ b/foo.sh
@@ -1,2 +1,2 @@
 context line
 another context line
EOF
  run parse_valid_lines "$DIFF_FILE"
  [ "$status" -eq 0 ]
  [ -z "$output" ]
}

@test "parse_valid_lines: does not emit lines for deleted lines" {
  cat > "$DIFF_FILE" <<'EOF'
diff --git a/foo.sh b/foo.sh
--- a/foo.sh
+++ b/foo.sh
@@ -1,3 +1,2 @@
 context line
-deleted line
 another context line
EOF
  run parse_valid_lines "$DIFF_FILE"
  [ "$status" -eq 0 ]
  [ -z "$output" ]
}

# ---------------------------------------------------------------------------
# Line number tracking across hunk headers
# ---------------------------------------------------------------------------

@test "parse_valid_lines: tracks new-file line numbers from hunk header" {
  cat > "$DIFF_FILE" <<'EOF'
diff --git a/foo.sh b/foo.sh
--- a/foo.sh
+++ b/foo.sh
@@ -10,4 +10,5 @@
 line at 10
 line at 11
+added at 12
 line at 13
 line at 14
EOF
  result=$(parse_valid_lines "$DIFF_FILE")
  echo "$result" | grep -qF "foo.sh:12"
  # Context lines should not appear
  [ "$(echo "$result" | grep -cF "foo.sh:10")" -eq 0 ]
}

@test "parse_valid_lines: handles multiple hunks in the same file" {
  cat > "$DIFF_FILE" <<'EOF'
diff --git a/foo.sh b/foo.sh
--- a/foo.sh
+++ b/foo.sh
@@ -1,3 +1,4 @@
 line 1
+added line 2
 line 3
 line 4
@@ -20,2 +21,3 @@
 line 21
+added at 22
 line 23
EOF
  run parse_valid_lines "$DIFF_FILE"
  [ "$status" -eq 0 ]
  echo "$output" | grep -qF "foo.sh:2"
  echo "$output" | grep -qF "foo.sh:22"
}

# ---------------------------------------------------------------------------
# Multiple files
# ---------------------------------------------------------------------------

@test "parse_valid_lines: tracks separate files independently" {
  cat > "$DIFF_FILE" <<'EOF'
diff --git a/a.sh b/a.sh
--- a/a.sh
+++ b/a.sh
@@ -1,1 +1,2 @@
 existing
+added in a
diff --git a/b.sh b/b.sh
--- a/b.sh
+++ b/b.sh
@@ -1,1 +1,2 @@
 existing
+added in b
EOF
  run parse_valid_lines "$DIFF_FILE"
  [ "$status" -eq 0 ]
  echo "$output" | grep -qF "a.sh:2"
  echo "$output" | grep -qF "b.sh:2"
}

# ---------------------------------------------------------------------------
# Edge cases: renames, "no newline at end of file", whitespace-only hunks
# ---------------------------------------------------------------------------

@test "parse_valid_lines: handles rename (b/ path is the new name)" {
  cat > "$DIFF_FILE" <<'EOF'
diff --git a/old.sh b/new.sh
similarity index 80%
rename from old.sh
rename to new.sh
--- a/old.sh
+++ b/new.sh
@@ -1,2 +1,3 @@
 unchanged
+added
 unchanged2
EOF
  run parse_valid_lines "$DIFF_FILE"
  [ "$status" -eq 0 ]
  echo "$output" | grep -qF "new.sh:2"
}

@test "parse_valid_lines: ignores backslash no-newline marker" {
  cat > "$DIFF_FILE" <<'EOF'
diff --git a/foo.sh b/foo.sh
--- a/foo.sh
+++ b/foo.sh
@@ -1,1 +1,2 @@
 context
+added
\ No newline at end of file
EOF
  run parse_valid_lines "$DIFF_FILE"
  [ "$status" -eq 0 ]
  echo "$output" | grep -qF "foo.sh:2"
  # The backslash line itself should not appear as a file:line entry
  lines=$(echo "$output" | wc -l | tr -d ' ')
  [ "$lines" -eq 1 ]
}

@test "parse_valid_lines: empty diff produces no output" {
  echo "" > "$DIFF_FILE"
  run parse_valid_lines "$DIFF_FILE"
  [ "$status" -eq 0 ]
  [ -z "$output" ]
}
