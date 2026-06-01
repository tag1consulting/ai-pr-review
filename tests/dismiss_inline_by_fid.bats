#!/usr/bin/env bats
# Tests for the jq selection predicates used by the "Resolve inline finding by
# F-ID and dismiss review if last" step in .github/workflows/slash-commands.yml.
#
# That step's GitHub-API and gh-graphql plumbing cannot be unit-tested without a
# live PR, but the two pieces of pure logic that carry the correctness risk —
# (1) selecting the right inline thread by its **[F<n>]** token, scoped to our
# bot + inline marker, and (2) counting remaining unresolved bot inline threads —
# are plain jq expressions over the reviewThreads GraphQL shape. These tests pin
# those expressions against a representative fixture so a future edit to the
# workflow's jq cannot silently regress the match logic.
#
# IMPORTANT: the jq expressions below are copied verbatim from
# slash-commands.yml. If you change them in one place, change them in both.

bats_require_minimum_version 1.5.0

# File-scope so the helper functions below are self-contained and don't depend
# on setup() having populated these (PROJECT_ROOT is exported by test_helper,
# loaded in setup()).
FIXTURE_REL="tests/fixtures/dismiss-inline/review-threads.json"
BOT='github-actions[bot]'

setup() {
  command -v jq >/dev/null 2>&1 || skip "jq not available"
  load test_helper
  FIXTURE="${PROJECT_ROOT}/${FIXTURE_REL}"
}

# Selection predicate: the thread whose first comment is bot-authored, carries
# the inline marker, and contains the F-token. Returns "<id>\t<isResolved>".
select_thread() {
  local ftoken="$1"
  jq -r --arg ftoken "$ftoken" --arg bot "$BOT" \
    '.data.repository.pullRequest.reviewThreads.nodes[]
     | select(.comments.nodes[0].author.login == $bot)
     | select(.comments.nodes[0].body | contains("<!-- ai-pr-review-inline -->"))
     | select(.comments.nodes[0].body | contains($ftoken))
     | "\(.id)\t\(.isResolved)"' "$FIXTURE" | head -1
}

# Count predicate: unresolved bot inline threads across the PR.
count_unresolved() {
  jq --arg bot "$BOT" \
    '[.data.repository.pullRequest.reviewThreads.nodes[]
      | select(.comments.nodes[0].author.login == $bot)
      | select(.comments.nodes[0].body | contains("<!-- ai-pr-review-inline -->"))
      | select(.isResolved == false)] | length' "$FIXTURE"
}

# ---------------------------------------------------------------------------
# Thread selection by F-token
# ---------------------------------------------------------------------------

@test "selects the bot inline thread matching F1" {
  run select_thread "**[F1]**"
  [ "$status" -eq 0 ]
  [ "$output" = "T_F1	false" ]
}

@test "F1 token does not falsely match F11 (closing ]** delimits the id)" {
  # The fixture has both F1 and F11 bot threads. Selecting F1 must return only F1.
  run select_thread "**[F1]**"
  [ "$output" = "T_F1	false" ]
  # And selecting F11 must return only the F11 thread.
  run select_thread "**[F11]**"
  [ "$output" = "T_F11	false" ]
}

@test "does not select a non-bot (user) comment that quotes the same token" {
  # T_user quotes **[F1]** but is authored by 'someuser' and lacks our marker.
  run select_thread "**[F1]**"
  [ "$output" != "T_user	false" ]
  [ "$output" = "T_F1	false" ]
}

@test "an already-resolved bot thread is still selectable (reports isResolved=true)" {
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

@test "counts only unresolved bot inline threads (excludes user + resolved)" {
  # Fixture has 5 threads: F1(open,bot), F2(open,bot), user(open,non-bot),
  # F11(open,bot), F10(resolved,bot). Unresolved bot inline = F1,F2,F11 = 3.
  run count_unresolved
  [ "$status" -eq 0 ]
  [ "$output" = "3" ]
}
