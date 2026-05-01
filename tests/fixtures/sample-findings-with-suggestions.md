## Findings

Deliberate minimal output for test fixture purposes.

```json-findings
[
  {
    "severity": "High",
    "confidence": 90,
    "file": "src/main.go",
    "line": 42,
    "finding": "Missing error check on file open",
    "remediation": "Check the error return value from os.Open",
    "suggested_code": "    f, err := os.Open(path)\n    if err != nil {\n        return err\n    }"
  },
  {
    "severity": "Medium",
    "confidence": 80,
    "file": "src/handler.go",
    "line": 17,
    "start_line": 15,
    "finding": "Unclosed resource in error path",
    "remediation": "Add defer f.Close() immediately after successful open",
    "suggested_code": "    f, err := os.Open(path)\n    if err != nil {\n        return err\n    }\n    defer f.Close()"
  }
]
```
