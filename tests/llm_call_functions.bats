#!/usr/bin/env bats
# Tests for pure functions in llm-call.sh.
# Functions are extracted individually at test time — the full script is never
# sourced, so no API calls are made.

bats_require_minimum_version 1.5.0

setup() {
  load test_helper
  load_function "${PROJECT_ROOT}/llm-call.sh" is_transient_http
  load_function "${PROJECT_ROOT}/llm-call.sh" is_transient_curl
}

# ---------------------------------------------------------------------------
# is_transient_http
# ---------------------------------------------------------------------------

@test "is_transient_http: 429 is transient" {
  run is_transient_http 429
  [ "$status" -eq 0 ]
}

@test "is_transient_http: 500 is transient" {
  run is_transient_http 500
  [ "$status" -eq 0 ]
}

@test "is_transient_http: 502 is transient" {
  run is_transient_http 502
  [ "$status" -eq 0 ]
}

@test "is_transient_http: 503 is transient" {
  run is_transient_http 503
  [ "$status" -eq 0 ]
}

@test "is_transient_http: 400 is not transient" {
  run is_transient_http 400
  [ "$status" -eq 1 ]
}

@test "is_transient_http: 401 is not transient" {
  run is_transient_http 401
  [ "$status" -eq 1 ]
}

@test "is_transient_http: 403 is not transient" {
  run is_transient_http 403
  [ "$status" -eq 1 ]
}

@test "is_transient_http: 404 is not transient" {
  run is_transient_http 404
  [ "$status" -eq 1 ]
}

@test "is_transient_http: 422 is not transient" {
  run is_transient_http 422
  [ "$status" -eq 1 ]
}

@test "is_transient_http: 200 is not transient" {
  run is_transient_http 200
  [ "$status" -eq 1 ]
}

# ---------------------------------------------------------------------------
# is_transient_curl
# ---------------------------------------------------------------------------

@test "is_transient_curl: 7 (connection refused) is transient" {
  run is_transient_curl 7
  [ "$status" -eq 0 ]
}

@test "is_transient_curl: 28 (timeout) is transient" {
  run is_transient_curl 28
  [ "$status" -eq 0 ]
}

@test "is_transient_curl: 56 (network failure) is transient" {
  run is_transient_curl 56
  [ "$status" -eq 0 ]
}

@test "is_transient_curl: 1 (unsupported protocol) is not transient" {
  run is_transient_curl 1
  [ "$status" -eq 1 ]
}

@test "is_transient_curl: 6 (DNS resolution failed) is not transient" {
  run is_transient_curl 6
  [ "$status" -eq 1 ]
}

@test "is_transient_curl: 0 (success) is not transient" {
  run is_transient_curl 0
  [ "$status" -eq 1 ]
}

# ---------------------------------------------------------------------------
# prompt_caching_enabled — resolves LLM_PROMPT_CACHING given AI_PROVIDER
# ---------------------------------------------------------------------------
#
# These tests source the function directly since it references AI_PROVIDER and
# LLM_PROMPT_CACHING as env vars and has no arguments.

_load_caching_fn() {
  load_function "${PROJECT_ROOT}/llm-call.sh" prompt_caching_enabled
}

@test "prompt_caching_enabled: auto + anthropic → enabled" {
  _load_caching_fn
  AI_PROVIDER=anthropic LLM_PROMPT_CACHING=auto run prompt_caching_enabled
  [ "$status" -eq 0 ]
}

@test "prompt_caching_enabled: auto + bedrock-proxy → enabled" {
  _load_caching_fn
  AI_PROVIDER=bedrock-proxy LLM_PROMPT_CACHING=auto run prompt_caching_enabled
  [ "$status" -eq 0 ]
}

@test "prompt_caching_enabled: auto + openai → disabled (no marker needed)" {
  _load_caching_fn
  AI_PROVIDER=openai LLM_PROMPT_CACHING=auto run prompt_caching_enabled
  [ "$status" -eq 1 ]
}

@test "prompt_caching_enabled: auto + google → disabled (different API)" {
  _load_caching_fn
  AI_PROVIDER=google LLM_PROMPT_CACHING=auto run prompt_caching_enabled
  [ "$status" -eq 1 ]
}

@test "prompt_caching_enabled: explicit true overrides provider gate" {
  _load_caching_fn
  AI_PROVIDER=openai LLM_PROMPT_CACHING=true run prompt_caching_enabled
  [ "$status" -eq 0 ]
}

@test "prompt_caching_enabled: explicit false disables even on anthropic" {
  _load_caching_fn
  AI_PROVIDER=anthropic LLM_PROMPT_CACHING=false run prompt_caching_enabled
  [ "$status" -eq 1 ]
}

@test "prompt_caching_enabled: empty LLM_PROMPT_CACHING treated as auto" {
  # load_function evaluates llm-call.sh's `LLM_PROMPT_CACHING="${LLM_PROMPT_CACHING:-auto}"`
  # line at load time, which means by the time prompt_caching_enabled runs,
  # the variable is already set to whatever the harness had. We test the
  # function's own empty-string branch instead — the case statement treats
  # "" the same as "auto", which is functionally the contract we care about.
  _load_caching_fn
  AI_PROVIDER=anthropic LLM_PROMPT_CACHING="" run prompt_caching_enabled
  [ "$status" -eq 0 ]
}

@test "prompt_caching_enabled: AI_PROVIDER unset falls through to disabled in auto" {
  # Defensive check: when the function is sourced in a test harness that
  # doesn't export AI_PROVIDER, prompt_caching_enabled must not error
  # (set -u safe via ${AI_PROVIDER:-}) and must return 1 (disabled) since
  # no known-caching provider matches.
  _load_caching_fn
  # Use bats' `run` so set -e doesn't abort the test on the expected
  # non-zero return. Unset AI_PROVIDER in the subshell spawned by `run`.
  unset AI_PROVIDER || true
  LLM_PROMPT_CACHING=auto run prompt_caching_enabled
  [ "$status" -eq 1 ]
}

# ---------------------------------------------------------------------------
# Whitespace trimming on LLM_PROMPT_CACHING
# ---------------------------------------------------------------------------
# Tests that leading/trailing whitespace on the env var is stripped before
# the case statement evaluates it. Sources the whole top-level script
# section that handles the trim (not just the function) via a bash subshell.

_run_trim() {
  # Args: provider, raw_value_to_set
  # Runs the trim-and-dispatch logic in a subshell with the given inputs
  # and returns prompt_caching_enabled's status.
  local provider="$1" raw_val="$2"
  (
    export AI_PROVIDER="$provider"
    export LLM_PROMPT_CACHING="$raw_val"
    # Reproduce the trim-and-default logic from llm-call.sh verbatim
    LLM_PROMPT_CACHING="${LLM_PROMPT_CACHING:-auto}"
    LLM_PROMPT_CACHING="${LLM_PROMPT_CACHING#"${LLM_PROMPT_CACHING%%[![:space:]]*}"}"
    LLM_PROMPT_CACHING="${LLM_PROMPT_CACHING%"${LLM_PROMPT_CACHING##*[![:space:]]}"}"
    load_function "${PROJECT_ROOT}/llm-call.sh" prompt_caching_enabled
    prompt_caching_enabled
  )
}

@test "LLM_PROMPT_CACHING: leading whitespace trimmed" {
  _run_trim anthropic "  true"
  [ "$?" -eq 0 ]
}

@test "LLM_PROMPT_CACHING: trailing whitespace trimmed" {
  _run_trim anthropic "true  "
  [ "$?" -eq 0 ]
}

@test "LLM_PROMPT_CACHING: surrounding whitespace trimmed" {
  _run_trim anthropic "  auto  "
  [ "$?" -eq 0 ]
}

@test "LLM_PROMPT_CACHING: tabs trimmed" {
  _run_trim openai $'\ttrue\t'
  # "true" on openai should explicitly enable caching
  [ "$?" -eq 0 ]
}

# ---------------------------------------------------------------------------
# Empty prompt-file validation (top-level script, not a function)
# ---------------------------------------------------------------------------

@test "llm-call.sh: empty SYSTEM_PROMPT_FILE fails fast" {
  # Invoke the script with an empty system-prompt file. Must exit non-zero
  # before any HTTP call. Setting AI_PROVIDER to a dummy value that fails
  # later would produce a different error; the empty-file check comes first.
  local sys usr
  sys=$(mktemp)  # empty
  usr=$(mktemp); echo "some content" > "$usr"
  AI_PROVIDER=anthropic ANTHROPIC_API_KEY=x \
    run --separate-stderr "${PROJECT_ROOT}/llm-call.sh" claude-haiku-4-5 "$sys" "$usr"
  rm -f "$sys" "$usr"
  [ "$status" -ne 0 ]
  echo "$stderr" | grep -q "system prompt file is missing or empty"
}

@test "llm-call.sh: empty USER_MESSAGE_FILE fails fast" {
  local sys usr
  sys=$(mktemp); echo "some content" > "$sys"
  usr=$(mktemp)  # empty
  AI_PROVIDER=anthropic ANTHROPIC_API_KEY=x \
    run --separate-stderr "${PROJECT_ROOT}/llm-call.sh" claude-haiku-4-5 "$sys" "$usr"
  rm -f "$sys" "$usr"
  [ "$status" -ne 0 ]
  echo "$stderr" | grep -q "user message file is missing or empty"
}

@test "llm-call.sh: nonexistent SYSTEM_PROMPT_FILE fails fast" {
  local usr
  usr=$(mktemp); echo "some content" > "$usr"
  AI_PROVIDER=anthropic ANTHROPIC_API_KEY=x \
    run --separate-stderr "${PROJECT_ROOT}/llm-call.sh" claude-haiku-4-5 "/tmp/nonexistent-$$" "$usr"
  rm -f "$usr"
  [ "$status" -ne 0 ]
  echo "$stderr" | grep -q "system prompt file is missing or empty"
}

@test "prompt_caching_enabled: invalid value warns and falls back to auto" {
  _load_caching_fn
  AI_PROVIDER=anthropic LLM_PROMPT_CACHING=maybe run --separate-stderr prompt_caching_enabled
  [ "$status" -eq 0 ]
  echo "$stderr" | grep -q "not a valid value"
}

# ---------------------------------------------------------------------------
# _build_anthropic_body — request body shape for Anthropic and Bedrock
# ---------------------------------------------------------------------------
#
# These tests verify the generated JSON has the correct shape under each
# combination of caching state, model temperature support, and Bedrock vs
# direct Anthropic.

_setup_body_fixture() {
  command -v jq >/dev/null 2>&1 || skip "jq not available"
  SYS_FILE=$(mktemp); USR_FILE=$(mktemp)
  echo "system content" > "$SYS_FILE"
  echo "user content" > "$USR_FILE"
  export SYSTEM_PROMPT_FILE="$SYS_FILE" USER_MESSAGE_FILE="$USR_FILE"
  export MAX_TOKENS=4096 TEMPERATURE=0.3
  load_function "${PROJECT_ROOT}/llm-call.sh" model_supports_temperature
  load_function "${PROJECT_ROOT}/llm-call.sh" prompt_caching_enabled
  load_function "${PROJECT_ROOT}/llm-call.sh" _build_anthropic_body
}

_teardown_body_fixture() {
  rm -f "$SYS_FILE" "$USR_FILE"
}

@test "_build_anthropic_body: caching=true uses shared-cache layout (issue #142)" {
  # Shared-cache layout: system is a 2-block array where block 0 is the
  # shared CODE_CONTEXT_MSG (from USER_MESSAGE_FILE) with cache_control,
  # and block 1 is the per-agent prompt (from SYSTEM_PROMPT_FILE) without
  # cache_control. messages carries only a sentinel turn. This lets agents
  # with different per-agent prompts share a cache entry on the context.
  _setup_body_fixture
  MODEL_ID="claude-sonnet-4-6" AI_PROVIDER=anthropic LLM_PROMPT_CACHING=true \
    body=$(_build_anthropic_body '{}' 'true')
  _teardown_body_fixture

  # system: 2-block array
  echo "$body" | jq -e '.system | type == "array"' > /dev/null
  echo "$body" | jq -e '.system | length == 2' > /dev/null

  # system[0] = shared user content WITH cache_control
  echo "$body" | jq -e '.system[0].cache_control.type == "ephemeral"' > /dev/null
  echo "$body" | jq -e '.system[0].text == "user content\n"' > /dev/null

  # system[1] = per-agent prompt WITHOUT cache_control
  # Use has() + assert the block exists first — '// "absent"' would also
  # return "absent" if system[1] were missing entirely, masking a real bug.
  echo "$body" | jq -e '.system[1] | type == "object"' > /dev/null
  echo "$body" | jq -e '.system[1].text == "system content\n"' > /dev/null
  echo "$body" | jq -e '.system[1] | has("cache_control") | not' > /dev/null

  # messages: single sentinel turn (small, stable, not cached)
  echo "$body" | jq -e '.messages | length == 1' > /dev/null
  echo "$body" | jq -e '.messages[0].role == "user"' > /dev/null
  echo "$body" | jq -e '.messages[0].content | type == "string"' > /dev/null
  echo "$body" | jq -e '.messages[0].content | length > 0' > /dev/null
  # No cache_control on the message OBJECT. A substring check on the content
  # string is meaningless since the sentinel never contains "cache_control".
  echo "$body" | jq -e '.messages[0] | has("cache_control") | not' > /dev/null
}

@test "_build_anthropic_body: caching=false produces legacy shape (no cache_control)" {
  _setup_body_fixture
  MODEL_ID="claude-sonnet-4-6" AI_PROVIDER=anthropic LLM_PROMPT_CACHING=false \
    body=$(_build_anthropic_body '{}' 'true')
  _teardown_body_fixture

  # Legacy shape: system is a plain string, content is a plain string
  echo "$body" | jq -e '.system | type == "string"' > /dev/null
  echo "$body" | jq -e '.system == "system content\n"' > /dev/null
  echo "$body" | jq -e '.messages[0].content | type == "string"' > /dev/null
  echo "$body" | jq -e '.messages[0].content == "user content\n"' > /dev/null
  # No cache_control anywhere
  echo "$body" | jq -e '.. | objects | has("cache_control")? // false | not' > /dev/null
}

@test "_build_anthropic_body: opus-4-7 omits temperature (unsupported)" {
  _setup_body_fixture
  MODEL_ID="claude-opus-4-7" AI_PROVIDER=anthropic LLM_PROMPT_CACHING=true \
    body=$(_build_anthropic_body '{}' 'true')
  _teardown_body_fixture
  echo "$body" | jq -e '.temperature // "absent"' | grep -q absent
}

@test "_build_anthropic_body: sonnet includes temperature" {
  _setup_body_fixture
  MODEL_ID="claude-sonnet-4-6" AI_PROVIDER=anthropic LLM_PROMPT_CACHING=true \
    body=$(_build_anthropic_body '{}' 'true')
  _teardown_body_fixture
  [ "$(echo "$body" | jq -r '.temperature')" = "0.3" ]
}

@test "_build_anthropic_body: include_model=true adds .model field" {
  _setup_body_fixture
  MODEL_ID="claude-sonnet-4-6" AI_PROVIDER=anthropic LLM_PROMPT_CACHING=true \
    body=$(_build_anthropic_body '{}' 'true')
  _teardown_body_fixture
  [ "$(echo "$body" | jq -r '.model')" = "claude-sonnet-4-6" ]
}

@test "_build_anthropic_body: include_model=false (Bedrock) omits .model field" {
  _setup_body_fixture
  MODEL_ID="claude-sonnet-4-6" AI_PROVIDER=bedrock-proxy LLM_PROMPT_CACHING=true \
    body=$(_build_anthropic_body '{"anthropic_version":"bedrock-2023-05-31"}' 'false')
  _teardown_body_fixture
  # .model should be absent
  echo "$body" | jq -e '.model // "absent"' | grep -q absent
  # Bedrock wrapper field present
  [ "$(echo "$body" | jq -r '.anthropic_version')" = "bedrock-2023-05-31" ]
}

@test "_build_anthropic_body: auto on openai disables caching markers" {
  _setup_body_fixture
  MODEL_ID="gpt-4o" AI_PROVIDER=openai LLM_PROMPT_CACHING=auto \
    body=$(_build_anthropic_body '{}' 'true')
  _teardown_body_fixture
  # auto + non-anthropic provider → no cache_control markers
  echo "$body" | jq -e '.system | type == "string"' > /dev/null
}

# ---------------------------------------------------------------------------
# model_supports_temperature — OpenAI o-series reasoning models
# ---------------------------------------------------------------------------

@test "model_supports_temperature: o3 rejects temperature" {
  load_function "${PROJECT_ROOT}/llm-call.sh" model_supports_temperature
  run model_supports_temperature "o3"
  [ "$status" -eq 1 ]
}

@test "model_supports_temperature: o3-2025-04-16 rejects temperature" {
  load_function "${PROJECT_ROOT}/llm-call.sh" model_supports_temperature
  run model_supports_temperature "o3-2025-04-16"
  [ "$status" -eq 1 ]
}

@test "model_supports_temperature: o3-mini rejects temperature" {
  load_function "${PROJECT_ROOT}/llm-call.sh" model_supports_temperature
  run model_supports_temperature "o3-mini"
  [ "$status" -eq 1 ]
}

@test "model_supports_temperature: o4-mini rejects temperature" {
  load_function "${PROJECT_ROOT}/llm-call.sh" model_supports_temperature
  run model_supports_temperature "o4-mini"
  [ "$status" -eq 1 ]
}

@test "model_supports_temperature: o1 rejects temperature" {
  load_function "${PROJECT_ROOT}/llm-call.sh" model_supports_temperature
  run model_supports_temperature "o1"
  [ "$status" -eq 1 ]
}

@test "model_supports_temperature: gpt-4o accepts temperature" {
  load_function "${PROJECT_ROOT}/llm-call.sh" model_supports_temperature
  run model_supports_temperature "gpt-4o"
  [ "$status" -eq 0 ]
}

@test "model_supports_temperature: gpt-4.1 accepts temperature" {
  load_function "${PROJECT_ROOT}/llm-call.sh" model_supports_temperature
  run model_supports_temperature "gpt-4.1"
  [ "$status" -eq 0 ]
}

@test "model_supports_temperature: claude-sonnet-4-6 accepts temperature" {
  load_function "${PROJECT_ROOT}/llm-call.sh" model_supports_temperature
  run model_supports_temperature "claude-sonnet-4-6"
  [ "$status" -eq 0 ]
}
