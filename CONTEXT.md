# ai-pr-review

Runs LLM agents and native static analyzers against PR/MR diffs and posts structured findings back — as a GitHub Action, a GitLab CI job, or a Bitbucket Pipelines step.

## Language

**Analyzer** (analyzer registration):
A named, independently toggleable entry in `bridge.py`'s `_ANALYZERS` list (an `AnalyzerSpec`), the unit the `analyzers`/`exclude-analyzers` allowlist and `ANALYZER_NAMES` validation act on. One analyzer name always maps to exactly one `native_fn`.
_Avoid_: using "analyzer" to mean a finding's `source` tag when the two diverge.

**Source tag**:
The `Finding.source` string stamped on a finding. Several source tags can be emitted by code registered under one analyzer name, but only a distinct analyzer *name* is independently disableable. Suppression rules (`suppress.py`) match on `file`, `pattern`, `line`, or `code`, never on `source`, so a sub-tag under a shared analyzer cannot be turned off on its own.
_Avoid_: assuming per-source-tag suppression or allowlisting is possible. It isn't, without a separate analyzer registration.

**Fingerprint**:
The stable identity of one finding: `source|file|line|code-hash`. It is the key verdicts, the id-map, and thread matching are all keyed on. Two occurrences of "the same" finding at a shifted line or with reworded text are different fingerprints.
_Avoid_: conflating a fingerprint with its F`<n>` display ID — the ID is a per-PR stable number that maps *to* a fingerprint, not the fingerprint itself.

**Verdict** (GitHub and Bitbucket; GitLab has no verdict system):
A human-recorded disposition — `dismissed`, `fixed`, or the `recurred` tombstone — for one finding's fingerprint. Written only by a slash command (`dismiss`/`false-positive`/`wont-fix`/`fixed`), never by the automated review pipeline itself. Bitbucket's version is polled from the PR's comment log at the start of the next run rather than applied immediately (issue #874), since Bitbucket Pipelines has no comment-triggered event.
_Avoid_: treating a `feedback`-command entry as a verdict. It is advisory learning-loop context and carries no suppression force (see `_governance.md`).

**Canonical review** (GitHub only):
The highest-id bot-authored review with a non-empty body among a PR's reviews, regardless of its current state. The one review a verdict-recording slash command patches, and the one `select_canonical` picks for review-reuse.
_Avoid_: assuming "canonical" means "currently active" — a dismissed review can still be canonical.

**Construction-time guard / read-time guard**:
Two different moments at which a review body's internal self-consistency can be checked. A construction-time guard runs inside the code about to POST a brand-new body, so it can prevent a contradiction from ever being written. A read-time guard re-derives state from bodies already posted — possibly since patched by a legitimate human command — and structurally cannot distinguish "corrupted at creation" from "correctly patched afterward," since both produce the identical shape (see issue #771, ADR 0003).
_Avoid_: calling either one "self-contradictory" without saying which — that ambiguity is what let a destructive read-time guard and a safe construction-time guard, added in the same commit for the same symptom, go undistinguished long enough for the read-time one to start discarding legitimate verdicts unnoticed.

**Severity** (of a finding):
The blocking contract: what a finding's level obliges the review verdict to do. `Critical` and `High` produce `REQUEST_CHANGES`; `Medium` and `Low` produce `APPROVE` with the finding still surfaced (`review/outcome.py`). Severity answers "must this stop the merge?" Harm is the input an agent reasons from when choosing a level, but the level itself is a commitment about the verdict, not a description of the code.
_Avoid_: deriving severity from confidence. They are independent axes, and a prompt that maps high confidence to `High` turns any assured-but-harmless observation into a merge block (see issue #798).

**Confidence** (of a finding):
An agent's self-reported certainty that the finding is real, 0-100. It gates *existence*, never severity: `findings/merge.py` drops anything below `confidence_threshold` (default 75, `AI_CONFIDENCE_THRESHOLD`) before findings reach the verdict stage. A finding that survives the floor is treated as true, and its severity is then decided by the blocking contract alone, not recomputed from confidence.
_Avoid_: reading a surviving finding's confidence as a severity signal, or expecting the judge pass's "below 60" downrank rule to fire — the confidence floor has already removed everything that rule describes.

**Learning loop**:
The whole feature: a human's `false-positive`/`wont-fix` verdict (plus a bare `feedback` note, on GitHub) gets recorded, persisted across PRs, and injected into future review prompts as `<repo-feedback>` context. Gated by `AI_FEEDBACK_LOOP`, same variable name on every supporting provider (GitHub, and Bitbucket as of issue #906).
_Avoid_: using "learning loop" to mean just the storage mechanism — that's the feedback store (see next entry). The learning loop also covers the record and inject steps, which have nothing to do with where the data lives.

**Feedback store**:
The storage part of the learning loop only: the JSONL file on the dedicated `ai-pr-review-bot` git branch, plus the `FeedbackStore` protocol (`feedback/store.py`) every provider implementation satisfies (`GitBranchStore` for GitHub, `BitbucketSrcStore` for Bitbucket, `UnsupportedVcsStore` elsewhere).
_Avoid_: using "feedback store" to mean the whole learning-loop feature — a finding suppressed on a PR via the hidden verdicts marker is not itself a feedback-store write; only a persisted `FeedbackEntry` is.
