
## Code Suggestions

When you have a concrete code fix for a finding, include a `suggested_code` field in the
json-findings entry. This field contains the exact replacement code that will be rendered
as a GitHub "Apply suggestion" button on the pull request, allowing the author to accept
the fix with one click.

### Rules

1. **Only include `suggested_code` when you have a specific, complete code replacement.**
   Do not include it for design-level advice, architectural suggestions, or findings where
   the fix depends on broader project context you cannot see.

2. **The `suggested_code` replaces lines `start_line` through `line` (inclusive) in the file.**
   - For single-line replacements, omit `start_line` (it defaults to `line`).
   - For multi-line replacements, set `start_line` to the first line being replaced.
   - Both `start_line` and `line` must reference lines visible in the diff.

3. **Include the complete replacement text** for all lines in the range, including any
   unchanged lines within the range. Do not use ellipsis, placeholder comments, or
   partial snippets. The suggestion must be directly applicable without editing.

4. **Preserve the original indentation exactly.** Match the whitespace style (spaces vs
   tabs, indent depth) of the surrounding code in the diff.

5. **Keep suggestions minimal.** Replace only the lines necessary to fix the issue.
   Do not refactor surrounding code, rename variables, or make stylistic changes beyond
   the fix itself.

6. **Use `\n` for newlines within the `suggested_code` string.** The JSON value must be
   a single string. Multi-line replacement code is encoded with literal `\n` characters
   between lines.

7. **Do not include triple backticks (```) anywhere inside `suggested_code`.** The
   post-review layer wraps your code in a GitHub ```suggestion fence, and an
   embedded triple backtick would close the fence early. If the code you want to
   suggest legitimately contains triple backticks (rare — e.g., a README edit
   or a heredoc), omit `suggested_code` entirely and describe the fix in prose.

8. **If you are not confident in a concrete code fix, omit `suggested_code` entirely**
   and provide only the natural-language `remediation` field. A missing suggestion is
   always better than a wrong one.

### Examples

Single-line replacement (line 42 of `cmd/server.go`):
```json
{
  "severity": "High",
  "confidence": 90,
  "file": "cmd/server.go",
  "line": 42,
  "finding": "Error return from os.Open is discarded",
  "remediation": "Check the error return value and handle it before using the file handle",
  "suggested_code": "    f, err := os.Open(path)"
}
```

Multi-line replacement (lines 40-42):
```json
{
  "severity": "High",
  "confidence": 90,
  "file": "cmd/server.go",
  "start_line": 40,
  "line": 42,
  "finding": "Unsafe file handling — error discarded and file never closed",
  "remediation": "Check the error and defer Close immediately after a successful open",
  "suggested_code": "    f, err := os.Open(path)\n    if err != nil {\n        return fmt.Errorf(\"open %s: %w\", path, err)\n    }\n    defer f.Close()"
}
```

Finding without a suggestion (design-level advice):
```json
{
  "severity": "Medium",
  "confidence": 80,
  "file": "internal/auth/handler.go",
  "line": 15,
  "finding": "Authentication check should be extracted to middleware",
  "remediation": "Move the token validation into a middleware function to avoid duplicating it across handlers"
}
```
