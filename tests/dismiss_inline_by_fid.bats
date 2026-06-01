#!/usr/bin/env bats
# Tests for the jq selection predicates used by the "Resolve inline finding by
# F-ID and dismiss review if last" step in .github/workflows/slash-commands.yml.
#
# That step's GitHub-API and gh-graphql plumbing cannot be unit-tested without a
# live PR, but the two pieces of pure logic that carry the correctness risk —
# (1) selecting the right inline thread by its **[F<n>]** token and (2) counting
# remaining unresolved bot inline threads — are plain jq expressions over the
# reviewThreads GraphQL shape. These tests pin those expressions against a
# representative fixture so a future edit to the workflow's jq cannot silently
# regress the match logic.
#
# OWNERSHIP IS GATED BY THE INLINE MARKER, NOT AUTHOR LOGIN. The GraphQL API
# reports the bot author as "github-actions" (no [bot] suffix) while the REST
# API reports "github-actions[bot]"; an author-login compare is therefore
# unreliable across APIs (this was a real bug caught in live e2e testing). The
# <!-- ai-pr-review-inline --> marker is the canonical ownership signal and is
# only ever emitted on our own inline comments. The fixture deliberately uses
# the GraphQL author form ("github-actions") and includes a marker-less
# bot-authored comment to prove the marker is what gates selection.
#
# IMPORTANT: the jq expressions below are copied verbatim from
# slash-commands.yml. If you change them in one place, change them in both.

bats_require_minimum_version 1.5.0

FIXTURE_REL="tests/fixtures/dismiss-inline/review-threads.json"

setup() {
  command -v jq >/dev/null 2>&1 || skip "jq not available"
  load test_helper
  FIXTURE="${PROJECT_ROOT}/${FIXTURE_REL}"
}

# Selection predicate: the thread whose first comment carries the inline marker
# and contains the F-token. Returns "<id>\t<isResolved>".
select_thread() {
  local ftoken="$1"
  jq -r --arg ftoken "$ftoken" \
    '.data.repository.pullRequest.reviewThreads.nodes[]
     | select(.comments.nodes[0].body | contains("<!-- ai-pr-review-inline -->"))
     | select(.comments.nodes[0].body | contains($ftoken))
     | "\(.id)\t\(.isResolved)"' "$FIXTURE" | head -1
}

# Count predicate: unresolved bot inline threads (gated by the marker).
count_unresolved() {
  jq '[.data.repository.pullRequest.reviewThreads.nodes[]
       | select(.comments.nodes[0].body | contains("<!-- ai-pr-review-inline -->"))
       | select(.isResolved == false)] | length' "$FIXTURE"
}

# ---------------------------------------------------------------------------
# Thread selection by F-token
# ---------------------------------------------------------------------------

@test "selects the inline thread matching F1 (author is 'github-actions' in GraphQL)" {
  run select_thread "**[F1]**"
  [ "$status" -eq 0 ]
  [ "$output" = "T_F1	false" ]
}

@test "F1 token does not falsely match F11 (closing ]** delimits the id)" {
  run select_thread "**[F1]**"
  [ "$output" = "T_F1	false" ]
  run select_thread "**[F11]**"
  [ "$output" = "T_F11	false" ]
}

@test "does not select a non-bot comment that quotes the same token (no marker)" {
  # T_user quotes **[F1]** but lacks the inline marker.
  run select_thread "**[F1]**"
  [ "$output" != "T_user	false" ]
  [ "$output" = "T_F1	false" ]
}

@test "marker gates selection: a marker-less comment with the F-token is ignored" {
  # T_F3_nomarker is bot-authored and contains **[F3]** but has no inline marker.
  run select_thread "**[F3]**"
  [ "$status" -eq 0 ]
  [ -z "$output" ]
}

@test "an already-resolved thread is still selectable (reports isResolved=true)" {
  run select_thread "**[F10]**"
  [ "$output" = "T_F10_resolved	true" ]
}

@test "a nonexistent F-id selects nothing" {
  run select_thread "**[F999]**"
  [ "$status" -eq 0 ]
  [ -z "$output" ]
}

# ---------------------------------------------------------------------------
# Unresolved-thread count (drives the dismiss-if-last decision)
# ---------------------------------------------------------------------------

@test "counts only unresolved marked inline threads (excludes user, resolved, marker-less)" {
  # Marked inline threads: F1(open), F2(open), F11(open), F10(resolved).
  # Excluded: T_user (no marker), T_F3_nomarker (no marker).
  # Unresolved marked = F1, F2, F11 = 3.
  run count_unresolved
  [ "$status" -eq 0 ]
  [ "$output" = "3" ]
}
