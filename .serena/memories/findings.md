# Findings Pipeline

## Key files
- `ai_pr_review/findings/models.py` — `Finding` (Pydantic), `Severity` (Literal)
- `ai_pr_review/findings/extract.py` — `extract_findings()`: parses `json-findings` blocks from LLM output
- `ai_pr_review/findings/merge.py` — `merge_findings()`: deduplication across agents
- `ai_pr_review/findings/suppress.py` — `apply_suppressions()`: verify-type handlers (npm, PyPI, Go, Cargo, Docker Hub, GitHub releases, ruby-lang.org)

## Finding schema
```python
class Finding(BaseModel):
    severity: Severity          # "high" | "medium" | "low" | "info"
    file: str | None            # null → body-level finding
    line: int | None            # null → body-level finding
    title: str
    body: str
    id: str | None              # stable F<n> ID assigned post-merge
```

## Body-level findings
Findings with `file=None` / `line=None` are body-level (not attached to a specific line). They get stable `F<n>` IDs and can be dismissed via `/ai-pr-review dismiss F<n>` from a top-level PR comment (see `mem:slash`).

## Suppression
`apply_suppressions()` can auto-verify and suppress findings when a referenced package version actually exists in the upstream registry. Verify handlers are per-ecosystem in `suppress.py`.
