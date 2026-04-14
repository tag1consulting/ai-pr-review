# Code Review

This is a sample agent response where the JSON block was truncated mid-stream.

## Findings

Some discussion here.

```json-findings
[
  {
    "severity": "High",
    "confidence": 85,
    "file": "src/main.go",
    "line": 42,
    "finding": "Missing error check on file open",
    "remediation": "Check the error return value from os.Open"
  },
  {
    "severity": "Medium",
    "confidence": 78,
    "file": "src/handler.go",
    "line": 17,
    "finding": "Unclosed resource in error path",
    "remediation": "Add defer f.Close() immediately after successful open"
  },
  {
    "severity": "Low",
    "confidence": 76,
    "file": "src/utils.go",
    "line": 88,
    "finding": "Partial find
<!-- RESPONSE_TRUNCATED -->
