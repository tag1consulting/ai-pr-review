#!/usr/bin/env bats
# Tests for is_test_file() in review.sh.

bats_require_minimum_version 1.5.0

setup() {
  load test_helper
  load_function "${PROJECT_ROOT}/lib/languages.sh" is_test_file
}

# ---------------------------------------------------------------------------
# Go
# ---------------------------------------------------------------------------

@test "is_test_file: Go test file (_test.go)" {
  run is_test_file "pkg/auth/handler_test.go"
  [ "$status" -eq 0 ]
}

@test "is_test_file: Go source file is not a test" {
  run is_test_file "pkg/auth/handler.go"
  [ "$status" -ne 0 ]
}

# ---------------------------------------------------------------------------
# Python
# ---------------------------------------------------------------------------

@test "is_test_file: Python test_ prefix" {
  run is_test_file "tests/test_auth.py"
  [ "$status" -eq 0 ]
}

@test "is_test_file: Python _test suffix" {
  run is_test_file "auth_test.py"
  [ "$status" -eq 0 ]
}

@test "is_test_file: Python source file is not a test" {
  run is_test_file "auth.py"
  [ "$status" -ne 0 ]
}

# ---------------------------------------------------------------------------
# JavaScript / TypeScript
# ---------------------------------------------------------------------------

@test "is_test_file: JS .test.js" {
  run is_test_file "src/auth.test.js"
  [ "$status" -eq 0 ]
}

@test "is_test_file: JS .spec.js" {
  run is_test_file "src/auth.spec.js"
  [ "$status" -eq 0 ]
}

@test "is_test_file: TS .test.ts" {
  run is_test_file "src/auth.test.ts"
  [ "$status" -eq 0 ]
}

@test "is_test_file: TS .spec.ts" {
  run is_test_file "src/auth.spec.ts"
  [ "$status" -eq 0 ]
}

@test "is_test_file: TSX .test.tsx" {
  run is_test_file "src/Button.test.tsx"
  [ "$status" -eq 0 ]
}

@test "is_test_file: JS source file is not a test" {
  run is_test_file "src/auth.js"
  [ "$status" -ne 0 ]
}

# ---------------------------------------------------------------------------
# Ruby
# ---------------------------------------------------------------------------

@test "is_test_file: Ruby _spec.rb" {
  run is_test_file "spec/models/user_spec.rb"
  [ "$status" -eq 0 ]
}

@test "is_test_file: Ruby _test.rb" {
  run is_test_file "test/models/user_test.rb"
  [ "$status" -eq 0 ]
}

@test "is_test_file: Ruby source file is not a test" {
  run is_test_file "app/models/user.rb"
  [ "$status" -ne 0 ]
}

# ---------------------------------------------------------------------------
# Java
# ---------------------------------------------------------------------------

@test "is_test_file: Java Test suffix" {
  run is_test_file "src/test/java/com/example/UserTest.java"
  [ "$status" -eq 0 ]
}

@test "is_test_file: Java source file is not a test" {
  run is_test_file "src/main/java/com/example/User.java"
  [ "$status" -ne 0 ]
}

# ---------------------------------------------------------------------------
# PHP
# ---------------------------------------------------------------------------

@test "is_test_file: PHP Test suffix" {
  run is_test_file "tests/UserTest.php"
  [ "$status" -eq 0 ]
}

@test "is_test_file: PHP TestBase suffix" {
  run is_test_file "tests/UserTestBase.php"
  [ "$status" -eq 0 ]
}

@test "is_test_file: PHP source file is not a test" {
  run is_test_file "src/User.php"
  [ "$status" -ne 0 ]
}

# ---------------------------------------------------------------------------
# C/C++
# ---------------------------------------------------------------------------

@test "is_test_file: C++ _test.cpp" {
  run is_test_file "src/auth_test.cpp"
  [ "$status" -eq 0 ]
}

@test "is_test_file: C++ _test.cc" {
  run is_test_file "src/auth_test.cc"
  [ "$status" -eq 0 ]
}

@test "is_test_file: C++ source file is not a test" {
  run is_test_file "src/auth.cpp"
  [ "$status" -ne 0 ]
}

# ---------------------------------------------------------------------------
# Directory-based detection
# ---------------------------------------------------------------------------

@test "is_test_file: file under /tests/ directory" {
  run is_test_file "project/tests/helper.sh"
  [ "$status" -eq 0 ]
}

@test "is_test_file: file under /test/ directory" {
  run is_test_file "project/test/fixtures/data.json"
  [ "$status" -eq 0 ]
}

@test "is_test_file: file under /spec/ directory" {
  run is_test_file "project/spec/support/helpers.rb"
  [ "$status" -eq 0 ]
}

@test "is_test_file: file whose name contains 'test' but not in test path" {
  run is_test_file "src/context.go"
  [ "$status" -ne 0 ]
}
