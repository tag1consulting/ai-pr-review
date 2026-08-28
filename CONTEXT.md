# ai-pr-review

GitHub Action that runs LLM agents and native static analyzers against PR diffs and posts structured findings back to the pull request.

## Language

**Analyzer** (analyzer registration):
A named, independently toggleable entry in `bridge.py`'s `_ANALYZERS` list (an `AnalyzerSpec`), the unit the `analyzers`/`exclude-analyzers` allowlist and `ANALYZER_NAMES` validation act on. One analyzer name always maps to exactly one `native_fn`.
_Avoid_: using "analyzer" to mean a finding's `source` tag when the two diverge.

**Source tag**:
The `Finding.source` string stamped on a finding. Several source tags can be emitted by code registered under one analyzer name, but only a distinct analyzer *name* is independently disableable. Suppression rules (`suppress.py`) match on `file`, `pattern`, `line`, or `code`, never on `source`, so a sub-tag under a shared analyzer cannot be turned off on its own.
_Avoid_: assuming per-source-tag suppression or allowlisting is possible. It isn't, without a separate analyzer registration.
