# Push-level recall corpus

Labeled diffs for `tests/canary/precision_eval.py`, built from real pushes that another reviewer found bugs in. The format is the one in `../eval_corpus/README.md`. This directory is separate from `eval_corpus/` on purpose. Its fixtures are larger than the 10 KB limit of the cheap default corpus, and a run over them costs more. The harness reads it through `AI_EVAL_CORPUS_DIR`.

```bash
AI_EVAL_CORPUS_DIR=tests/canary/eval_corpus_pushes python tests/canary/precision_eval.py --dry-run
```

A dry run prints the estimate and makes no call. A live run needs a person's approval first (see CLAUDE.md).

## Where the fixtures come from

Each fixture is one push, `git diff C^ C` limited to `ai_pr_review/` and `prompts/`. claude[bot] (the Claude Code Review app) reviewed that commit and named the bug in an inline comment. The author confirmed it and fixed it. `verified_by` names the review comment ids and the fix commit. `bugs` holds the lines that the comment named, in the numbering of the commit. A test checks that every label overlaps a changed hunk of its diff.

| Fixture | Push | Bugs | Fixed in |
|---|---|---|---|
| 22 | 238997c (#1083) | 2 | e9f7ad0 |
| 23 | e9f7ad0 (#1083) | 2 | f8aa46e |
| 24 | f8aa46e (#1083) | 1 | dbabdfd |
| 25 | dbabdfd (#1083) | 1 | 46a767d |
| 26 | 46a767d (#1083) | 2 | 99750fe |
| 27 | 3c162ae (#1073) | 1 | f90e7c1 |
| 28 | 1069605 (#1085) | 2 | 3d42246 |
| 29 | 571ea8f (#1086) | 2 | fae8a63 |
| 30 | ba9454c (#1073) | 1 | 3d42246 |

## Limits

- This is 9 pushes and 14 bugs. Five fixtures change `findings/merge.py`, and 24 to 26 are one design problem, so the fixtures are not independent. Treat the set as a regression set for cross-file and ordering bugs, not as a sample.
- The labels come from one reviewer and one author. A bug that claude[bot] did not find is not labeled, so a finding on it counts as a false positive until a person adjudicates it.
- Some comments name code that the push did not change. Fixtures 27 and 30 use the commit that introduced the code, found with `git blame`, because the pushed diff would hide the bug. A comment on the large release diff (about 2,400 lines) has no fixture, because that diff is too large for a single-agent run.
- The diffs leave out tests and docs, as the other fixtures do. A reviewer of a real push sees both.
- The harness runs `code-reviewer` on the diff alone. A claude[bot] comment often cites code outside the diff (`orchestrate.py`, `_finding_ids.py`, `outcome.py`). A run that does not give the agent that code measures the single-shot design, and it does not show the cause of the gap in #1088.
