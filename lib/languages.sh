#!/usr/bin/env bash
# lib/languages.sh — language detection and test-file classification for review.sh.
#
# Sourced by review.sh (via main() after SCRIPT_DIR is set). Exports:
#   detect_language  — maps file extensions to language labels used to select
#                      language-profiles/<name>.md injection into agent messages
#   is_test_file     — classifies a path as a test file for manifest grouping

detect_language() {
  local ext="$1"
  case "$ext" in
    go) echo "Go" ;;
    py) echo "Python" ;;
    js|jsx) echo "JavaScript" ;;
    ts|tsx) echo "TypeScript" ;;
    php|module|theme|inc) echo "PHP" ;;
    tf|tfvars) echo "Terraform" ;;
    sh|bash) echo "Shell" ;;
    yaml|yml) echo "YAML" ;;
    rb|rake|gemspec) echo "Ruby" ;;
    rs) echo "Rust" ;;
    java) echo "Java" ;;
    c|h|cpp|hpp|cc|cxx) echo "C++" ;;
    kt|kts) echo "Kotlin" ;;
    swift) echo "Swift" ;;
    cs) echo "CSharp" ;;
    scala|sbt) echo "Scala" ;;
    sql) echo "SQL" ;;
    lua) echo "Lua" ;;
    pl|pm) echo "Perl" ;;
    *) echo "" ;;
  esac
}

is_test_file() {
  local file="$1"
  [[ "$file" =~ _test\.go$ ]] ||
  [[ "$file" =~ test_.*\.py$ ]] ||
  [[ "$file" =~ _test\.py$ ]] ||
  [[ "$file" =~ \.test\.[jt]sx?$ ]] ||
  [[ "$file" =~ \.spec\.[jt]sx?$ ]] ||
  [[ "$file" =~ _spec\.rb$ ]] ||
  [[ "$file" =~ _test\.rb$ ]] ||
  [[ "$file" =~ Test\.java$ ]] ||
  [[ "$file" =~ TestBase\.php$ ]] ||
  [[ "$file" =~ Test\.php$ ]] ||
  [[ "$file" =~ _test\.cpp$ ]] ||
  [[ "$file" =~ _test\.cc$ ]] ||
  [[ "$file" =~ _test\.ts$ ]] ||
  [[ "$file" =~ /tests/ ]] ||
  [[ "$file" =~ /test/ ]] ||
  [[ "$file" =~ /spec/ ]]
}
