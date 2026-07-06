IMPORTANT: Emit the `json-findings` block FIRST, before your markdown analysis.
This ensures findings are captured even if your response is truncated.

Emit a JSON block fenced with ```json-findings containing structured findings for
inline comment posting. Each finding MUST include its numeric `confidence` score —
findings below 75 will be automatically filtered.

```json-findings
[{"severity":"High","confidence":85,"category":"injection","file":"path/to/file","line":42,"finding":"description","remediation":"how to fix"}]
```

`severity` must be exactly one of: `Critical`, `High`, `Medium`, `Low`.
`confidence` must be an integer 0–100. Only include findings with confidence ≥ 75.
`category` must be exactly one of: `authz`, `injection`, `dependency-cve`, `secret`,
`architecture-coupling`, `test-gap`, `edge-case`, `observability`, `docs`, `lint`, `other`.
Use `other` if none fit.
If no findings, emit an empty array: `[]`.
