# Golden Harness Tolerance Rules

This document defines the tolerance rules applied when `diff_harness.py` compares
a fixture's recorded output against the expected output. When a diff is within a
documented tolerance it is NOT reported as a failure.

## Rationale

Some fields in API responses and request bodies are inherently non-deterministic:
timestamps change every run, ID fields are assigned by the server, rendered markdown
may have minor whitespace differences. Treating these as failures would make the
harness brittle. Tolerances are explicit — this file is the authoritative source.

---

## Tolerance: Timestamps

**Fields:** Any field matching ISO 8601 format (`YYYY-MM-DDTHH:MM:SSZ`) in tape
bodies — e.g. `timestamp` in tape envelopes, `created_at` in GitHub API responses.

**Rule:** Timestamps are ignored entirely during comparison. The harness does not
assert on the value of any timestamp field.

**Why:** Timestamps change every replay. They carry no parity signal.

---

## Tolerance: Opaque Server-Assigned IDs

**Fields:** `id`, `html_url`, `review_id`, `comment_id`, `node_id` in VCS tape
response bodies.

**Rule:** These fields are ignored during comparison. Only the *structure* (presence
of the field, HTTP status, URL pattern) is asserted.

**Why:** IDs are assigned by the VCS server. They are different on every API call
and cannot be predicted from the input.

---

## Tolerance: Finding Array Ordering

**Fields:** The top-level findings array in `expected.json` and in LLM tape
response bodies.

**Rule:** Findings are normalized and sorted by `(file, line, severity)` before
comparison. Ordering within the array is not asserted.

**Why:** Parallel agent execution produces findings in non-deterministic order.
The Python engine may also emit findings in a different order than bash. Only
content matters, not order.

---

## Tolerance: Markdown Whitespace

**Fields:** Comment bodies in VCS tape request/response payloads, rendered summary
text.

**Rule:** The harness checks for *presence* of required strings (e.g.
`ai-pr-review-summary`, SHA prefix) rather than exact body equality. Runs of
whitespace are normalized before string matching.

**Why:** The Python engine may use slightly different whitespace or line-break
conventions when rendering the summary comment. As long as the required structural
markers are present, parity is preserved.

---

## Tolerance: VCS Request-Body Request IDs / Nonces

**Fields:** `X-Request-ID`, `X-GitHub-Delivery`, `idempotency_key` if present in
headers or bodies.

**Rule:** Ignored during comparison.

**Why:** These are client-generated nonces that differ across runs.

---

## Non-Tolerance: The Following Are Always Asserted

The following are NOT covered by any tolerance and must match exactly (or within
the structural rules above):

- Finding count per fixture (length of the findings array after normalization)
- Finding severity for each finding
- Finding file and line number (within ±0 — exact match required)
- SHA watermark (`sha_after` must appear in at least one VCS tape request body)
- Inline comment presence: if expected, the outbound call must be present
- Outcome risk level (`None`, `Low`, `Medium`, `High`, `Critical`)
- Review event type (`APPROVE`, `COMMENT`, `REQUEST_CHANGES`, `SKIP`)
