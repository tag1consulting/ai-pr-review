After your markdown output, emit a JSON block fenced with ```json-findings that
contains structured findings for inline comment posting. Each finding MUST include
its numeric `confidence` score — findings below 75 will be automatically filtered.

```json-findings
[{"severity":"High","confidence":85,"file":"path/to/file","line":42,"finding":"description","remediation":"how to fix"}]
```

`severity` must be exactly one of: `Critical`, `High`, `Medium`, `Low`.
`confidence` must be an integer 0–100. Only include findings with confidence ≥ 75.
If no findings, emit an empty array: `[]`.
