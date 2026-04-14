#!/usr/bin/env bash
# test_helper.bash — Extract individual function definitions from scripts for
# isolated testing without sourcing the full script (which triggers orchestration).

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Extract a function definition from a script file and eval it into the current
# shell. Uses awk with brace-depth tracking to reliably handle nested braces,
# heredocs (that don't contain unbalanced braces), and multi-line constructs.
#
# Usage: load_function <script_path> <function_name>
load_function() {
  local script="$1" func_name="$2"
  local func_body
  func_body=$(awk -v fname="${func_name}" '
    $0 ~ "^"fname"\\(\\)" { found=1; depth=0 }
    found {
      n = split($0, chars, "")
      for (i = 1; i <= n; i++) {
        if (chars[i] == "{") depth++
        if (chars[i] == "}") depth--
      }
      body = body $0 "\n"
      if (found && depth == 0 && body != "") { print body; exit }
    }
  ' "$script")

  if [[ -z "$func_body" ]]; then
    echo "ERROR: Could not extract function '${func_name}' from ${script}" >&2
    return 1
  fi

  eval "$func_body"
}
