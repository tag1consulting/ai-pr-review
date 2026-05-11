#!/usr/bin/env bats
# Tests for fetch_pr_description() in review.sh.

setup() {
  load test_helper
  load_function "${PROJECT_ROOT}/review.sh" fetch_pr_description

  # Provide a no-op mktemp_tracked (fetch_pr_description doesn't use it, but
  # load_function may pull in adjacent code if brace-counting drifts).
  mktemp_tracked() { mktemp "$@"; }
}

# ---------------------------------------------------------------------------
# GitHub provider
# ---------------------------------------------------------------------------

@test "fetch_pr_description: GitHub — sets PR_TITLE and PR_BODY" {
  # Stub gh to return JSON
  gh() {
    echo '{"title":"Add authentication","body":"This PR adds OAuth2 support.\n\nFixes #42"}'
  }
  export -f gh
  GITHUB_REPOSITORY="owner/repo"
  export GITHUB_REPOSITORY

  run fetch_pr_description "123" "github"
  [ "$status" -eq 0 ]

  # Re-run outside of `run` to check variable side-effects
  PR_TITLE=""
  PR_BODY=""
  fetch_pr_description "123" "github"
  [ "$PR_TITLE" = "Add authentication" ]
  [[ "$PR_BODY" == *"OAuth2"* ]]
}

@test "fetch_pr_description: GitHub — handles empty body gracefully" {
  gh() {
    echo '{"title":"Quick fix","body":""}'
  }
  export -f gh
  GITHUB_REPOSITORY="owner/repo"
  export GITHUB_REPOSITORY

  PR_TITLE=""
  PR_BODY=""
  fetch_pr_description "42" "github"
  [ "$PR_TITLE" = "Quick fix" ]
  [ "$PR_BODY" = "" ]
}

@test "fetch_pr_description: GitHub — handles null body from API" {
  gh() {
    echo '{"title":"No description","body":null}'
  }
  export -f gh
  GITHUB_REPOSITORY="owner/repo"
  export GITHUB_REPOSITORY

  PR_TITLE=""
  PR_BODY=""
  fetch_pr_description "1" "github"
  [ "$PR_TITLE" = "No description" ]
  [ "$PR_BODY" = "" ]
}

@test "fetch_pr_description: GitHub — survives API failure" {
  gh() { return 1; }
  export -f gh
  GITHUB_REPOSITORY="owner/repo"
  export GITHUB_REPOSITORY

  PR_TITLE=""
  PR_BODY=""
  fetch_pr_description "999" "github"
  [ "$PR_TITLE" = "" ]
  [ "$PR_BODY" = "" ]
}

@test "fetch_pr_description: GitHub — gracefully handles missing GITHUB_REPOSITORY" {
  gh() { echo '{}'; }
  export -f gh
  unset GITHUB_REPOSITORY

  PR_TITLE=""
  PR_BODY=""
  run fetch_pr_description "1" "github"
  [ "$status" -eq 0 ]
  [[ "$output" == *"GITHUB_REPOSITORY is not set"* ]]
}

@test "fetch_pr_description: GitHub — truncates long body" {
  local long_body
  long_body=$(printf 'x%.0s' $(seq 1 5000))
  gh() {
    printf '{"title":"Big PR","body":"%s"}' "$long_body"
  }
  export -f gh
  export long_body
  GITHUB_REPOSITORY="owner/repo"
  export GITHUB_REPOSITORY

  PR_TITLE=""
  PR_BODY=""
  fetch_pr_description "5" "github"
  [ "$PR_TITLE" = "Big PR" ]
  # Body should be truncated to 4000 chars + …[truncated] suffix
  [ ${#PR_BODY} -lt 5000 ]
  [[ "$PR_BODY" == *"[truncated]" ]]
}

@test "fetch_pr_description: GitHub — strips HTML comment lines" {
  # gh api returns JSON with \n as literal two-char escape; jq -r converts to real newlines
  gh() {
    printf '{"title":"Templated","body":"<!-- template marker -->\\nActual description\\n<!-- end -->"}'
  }
  export -f gh
  GITHUB_REPOSITORY="owner/repo"
  export GITHUB_REPOSITORY

  PR_TITLE=""
  PR_BODY=""
  fetch_pr_description "7" "github"
  [ "$PR_TITLE" = "Templated" ]
  [[ "$PR_BODY" == *"Actual description"* ]]
  [[ "$PR_BODY" != *"<!-- template"* ]]
  [[ "$PR_BODY" != *"<!-- end"* ]]
}

# ---------------------------------------------------------------------------
# GitLab provider
# ---------------------------------------------------------------------------

@test "fetch_pr_description: GitLab — sets PR_TITLE and PR_BODY" {
  curl() {
    echo '{"title":"Fix pipeline","description":"Resolves flaky CI job"}'
  }
  export -f curl
  GITLAB_TOKEN="glpat-test123"
  GITLAB_PROJECT_ID="42"
  export GITLAB_TOKEN GITLAB_PROJECT_ID

  PR_TITLE=""
  PR_BODY=""
  fetch_pr_description "10" "gitlab"
  [ "$PR_TITLE" = "Fix pipeline" ]
  [ "$PR_BODY" = "Resolves flaky CI job" ]
}

@test "fetch_pr_description: GitLab — skips without token" {
  unset GITLAB_TOKEN
  unset CI_JOB_TOKEN

  PR_TITLE=""
  PR_BODY=""
  run fetch_pr_description "10" "gitlab"
  [ "$status" -eq 0 ]
  [[ "$output" == *"No GitLab token"* ]]
}

# ---------------------------------------------------------------------------
# Bitbucket provider
# ---------------------------------------------------------------------------

@test "fetch_pr_description: Bitbucket — sets PR_TITLE and PR_BODY" {
  curl() {
    echo '{"title":"Update deps","description":"Bumps lodash to 4.17.21"}'
  }
  export -f curl
  BITBUCKET_WORKSPACE="myteam"
  BITBUCKET_REPO_SLUG="myrepo"
  BITBUCKET_API_TOKEN="test-token"
  export BITBUCKET_WORKSPACE BITBUCKET_REPO_SLUG BITBUCKET_API_TOKEN

  PR_TITLE=""
  PR_BODY=""
  fetch_pr_description "55" "bitbucket"
  [ "$PR_TITLE" = "Update deps" ]
  [ "$PR_BODY" = "Bumps lodash to 4.17.21" ]
}

@test "fetch_pr_description: Bitbucket — falls back to GITHUB_REPOSITORY for workspace/slug" {
  curl() {
    echo '{"title":"Fallback test","description":"Uses GH repo var"}'
  }
  export -f curl
  unset BITBUCKET_WORKSPACE
  unset BITBUCKET_REPO_SLUG
  GITHUB_REPOSITORY="myworkspace/myslug"
  BITBUCKET_API_TOKEN="test-token"
  export GITHUB_REPOSITORY BITBUCKET_API_TOKEN

  PR_TITLE=""
  PR_BODY=""
  fetch_pr_description "1" "bitbucket"
  [ "$PR_TITLE" = "Fallback test" ]
}

@test "fetch_pr_description: Bitbucket — gracefully handles missing API token" {
  unset BITBUCKET_API_TOKEN
  BITBUCKET_WORKSPACE="myteam"
  BITBUCKET_REPO_SLUG="myrepo"
  export BITBUCKET_WORKSPACE BITBUCKET_REPO_SLUG

  PR_TITLE=""
  PR_BODY=""
  run fetch_pr_description "1" "bitbucket"
  [ "$status" -eq 0 ]
  [[ "$output" == *"BITBUCKET_API_TOKEN is not set"* ]]
}

# ---------------------------------------------------------------------------
# Unknown provider
# ---------------------------------------------------------------------------

@test "fetch_pr_description: unknown provider — warns and returns empty" {
  PR_TITLE=""
  PR_BODY=""
  run fetch_pr_description "1" "unknown-vcs"
  [ "$status" -eq 0 ]
  [[ "$output" == *"Unknown VCS provider"* ]]
}

# ---------------------------------------------------------------------------
# Standalone mode (no fetch expected)
# ---------------------------------------------------------------------------

@test "fetch_pr_description: empty pr_number — no crash" {
  gh() { echo '{}'; }
  export -f gh
  GITHUB_REPOSITORY="owner/repo"
  export GITHUB_REPOSITORY

  PR_TITLE=""
  PR_BODY=""
  fetch_pr_description "" "github"
  [ "$PR_TITLE" = "" ]
  [ "$PR_BODY" = "" ]
}
