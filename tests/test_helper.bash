#!/usr/bin/env bash
# test_helper.bash — Extract individual function definitions from scripts for
# isolated testing without sourcing the full script (which triggers orchestration).

# shellcheck disable=SC2034  # used by .bats files that load this helper
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Extract a function definition from a script file and eval it into the current
# shell. Uses awk with brace-depth tracking to handle nested braces and
# multi-line constructs.
#
# Limitation: brace counting is fooled by { or } inside string literals,
# comments, or heredoc bodies. Production scripts use neither, so this is
# safe for the current test suite. Long-term, adding a main guard
# ([[ "${BASH_SOURCE[0]}" == "$0" ]]) to production scripts would allow
# using 'source script && declare -f funcname' instead.
#
# Usage: load_function <script_path> <function_name>
load_function() {
  local script="$1" func_name="$2"
  local func_body
  func_body=$(awk -v fname="${func_name}" '
    $1 == fname"()" { found=1; depth=0; started=0 }
    found {
      n = split($0, chars, "")
      in_sq = 0   # inside single-quoted string
      for (i = 1; i <= n; i++) {
        c = chars[i]
        # Toggle single-quote state (bash single quotes cannot be escaped inside them)
        if (c == "'"'"'") { in_sq = !in_sq; continue }
        if (in_sq) continue
        if (c == "{") { depth++; started=1 }
        if (c == "}") depth--
      }
      body = body $0 "\n"
      if (started && depth == 0 && body != "") { print body; exit }
    }
  ' "$script")

  if [[ -z "$func_body" ]]; then
    echo "ERROR: Could not extract function '${func_name}' from ${script}" >&2
    return 1
  fi

  eval "$func_body"
}
