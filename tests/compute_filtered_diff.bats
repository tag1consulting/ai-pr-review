#!/usr/bin/env bats
# Tests for compute_filtered_diff() in lib/diff.sh.
# Builds a real git repo fixture in a temp directory so the function can
# exercise actual git operations (cherry-pick, worktrees, diff).

setup() {
  load test_helper

  load_function "${PROJECT_ROOT}/lib/diff.sh" compute_filtered_diff

  # mktemp_tracked used inside compute_filtered_diff — provide a simple shim.
  _TMPFILE_LIST=()
  mktemp_tracked() {
    local tmp
    tmp=$(mktemp "$@")
    _TMPFILE_LIST+=("$tmp")
    echo "$tmp"
  }

  # Build a minimal git repo:
  #   main   — upstream; two commits
  #   feature — branches from main@1, adds commits, merges main@2, adds more
  REPO_DIR=$(mktemp -d)

  (
    cd "$REPO_DIR" || exit 1
    git init -q
    git config user.email "test@example.com"
    git config user.name "Tester"
    git config commit.gpgSign false

    # Upstream commit 1
    echo "upstream v1" > upstream.txt
    git add upstream.txt
    git commit -q -m "upstream: v1"
    MAIN_SHA=$(git rev-parse HEAD)

    # Simulate origin/main pointing to MAIN_SHA
    git update-ref refs/remotes/origin/main "$MAIN_SHA"

    # Feature branch: developer commit A
    git checkout -q -b feature
    echo "dev A" > feature.txt
    git add feature.txt
    git commit -q -m "feat: dev commit A"
    DEV_A_SHA=$(git rev-parse HEAD)

    # Upstream adds commit 2 on main
    git checkout -q main
    echo "upstream v2" >> upstream.txt
    git add upstream.txt
    git commit -q -m "upstream: v2"
    UPSTREAM_V2=$(git rev-parse HEAD)
    git update-ref refs/remotes/origin/main "$UPSTREAM_V2"

    # Developer merges main into feature (the merge commit we want to filter)
    git checkout -q feature
    git merge -q --no-edit main
    MERGE_SHA=$(git rev-parse HEAD)

    # Developer commit B (after merge)
    echo "dev B" >> feature.txt
    git add feature.txt
    git commit -q -m "feat: dev commit B"
    DEV_B_SHA=$(git rev-parse HEAD)

    printf '%s\n%s\n%s\n%s\n%s\n' \
      "$MAIN_SHA" "$DEV_A_SHA" "$UPSTREAM_V2" "$MERGE_SHA" "$DEV_B_SHA" \
      > "${REPO_DIR}/shas.txt"
  )

  {
    read -r MAIN_SHA
    read -r DEV_A_SHA
    read -r UPSTREAM_V2
    read -r MERGE_SHA
    read -r DEV_B_SHA
  } < "${REPO_DIR}/shas.txt"

  export MAIN_SHA DEV_A_SHA UPSTREAM_V2 MERGE_SHA DEV_B_SHA
  export BASE_REF="main"
  export DIFF_FILE; DIFF_FILE=$(mktemp)
}

teardown() {
  rm -rf "$REPO_DIR" "$DIFF_FILE"
  for f in "${_TMPFILE_LIST[@]+"${_TMPFILE_LIST[@]}"}"; do
    rm -f "$f"
  done
}

# ---------------------------------------------------------------------------

@test "compute_filtered_diff: no merges in range — exits 0, DIFF_FILE untouched" {
  echo "sentinel" > "$DIFF_FILE"

  # Range DEV_A_SHA..DEV_A_SHA has no commits — but use MAIN_SHA..DEV_A_SHA
  # which has only non-merge dev commit A; no merge commits in range.
  cd "$REPO_DIR"
  compute_filtered_diff "$MAIN_SHA" "$DEV_A_SHA"
  local exit_code=$?

  [ "$exit_code" -eq 0 ]
  # DIFF_FILE must be untouched (function is a no-op when no merges in range)
  [ "$(cat "$DIFF_FILE")" = "sentinel" ]
}

@test "compute_filtered_diff: base-branch merge filtered — only dev changes in DIFF_FILE" {
  cd "$REPO_DIR"
  compute_filtered_diff "$MAIN_SHA" "$DEV_B_SHA"
  local exit_code=$?

  [ "$exit_code" -eq 0 ]

  local diff_content
  diff_content=$(cat "$DIFF_FILE")

  # The filtered diff should contain feature.txt changes (dev work)
  echo "$diff_content" | grep -q "feature.txt"

  # Upstream-only changes (upstream v2 line) must NOT appear in the filtered diff
  ! echo "$diff_content" | grep -q "upstream v2"
}

@test "compute_filtered_diff: intra-PR merge is preserved — exits 0, no filtering" {
  # Create a second feature branch (branch2) that the developer merges into
  # feature. branch2's tip is NOT reachable from origin/main, so this merge
  # must NOT be stripped.
  (
    cd "$REPO_DIR" || exit 1
    git config user.email "test@example.com"
    git config user.name "Tester"
    git config commit.gpgSign false

    git checkout -q -b branch2 "$MAIN_SHA"
    echo "branch2 work" > branch2.txt
    git add branch2.txt
    git commit -q -m "branch2: work"
    BRANCH2_SHA=$(git rev-parse HEAD)

    git checkout -q feature
    git merge -q --no-edit branch2
    INTRA_MERGE=$(git rev-parse HEAD)

    printf '%s\n%s\n' "$BRANCH2_SHA" "$INTRA_MERGE" > "${REPO_DIR}/intra-shas.txt"
  )

  {
    read -r BRANCH2_SHA
    read -r INTRA_MERGE
  } < "${REPO_DIR}/intra-shas.txt"

  # Use range MERGE_SHA..INTRA_MERGE (the intra-PR merge only, after origin's merge is done)
  echo "sentinel" > "$DIFF_FILE"
  cd "$REPO_DIR"
  compute_filtered_diff "$MERGE_SHA" "$INTRA_MERGE"
  local exit_code=$?

  [ "$exit_code" -eq 0 ]
  # The intra-PR merge is not from origin/main, so no filtering occurs — DIFF_FILE untouched
  [ "$(cat "$DIFF_FILE")" = "sentinel" ]
}
