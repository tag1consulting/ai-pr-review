#!/usr/bin/env bash
#
# llm-call.sh — Multi-provider LLM API client.
#
# Replaces bedrock-call.sh. Dispatches to the appropriate provider based on
# AI_PROVIDER. Interface is identical to bedrock-call.sh so call_agent() in
# review.sh requires no changes beyond the filename reference.
#
# Usage:
#   ./llm-call.sh <model_id> <system_prompt_file> <user_message_file> [max_tokens]
#
# Environment:
#   AI_PROVIDER       — Required: anthropic | openai | openai-compatible | google | bedrock-proxy
#   AI_TEMPERATURE    — Optional: defaults to 0.3
#   LLM_PROMPT_CACHING — Optional: auto (default) | true | false
#       auto/true enables Anthropic/Bedrock prompt caching via cache_control
#       markers on the system prompt and user message. No effect on OpenAI
#       (automatic prefix caching is stable-prefix-based, no marker needed)
#       or Google (Gemini uses a different caching API, currently unsupported).
#
#   Provider credentials (one set required based on AI_PROVIDER):
#     anthropic:          ANTHROPIC_API_KEY
#     openai:             OPENAI_API_KEY
#     openai-compatible:  OPENAI_API_KEY, OPENAI_BASE_URL
#     google:             GOOGLE_API_KEY
#     bedrock-proxy:      BEDROCK_API_URL, BEDROCK_API_KEY
#
# Output:
#   Writes the assistant response text to stdout.
#   Emits "TOKENS: input=N output=N cache_creation=N cache_read=N model=M" to
#   stderr for usage tracking. For Anthropic/Bedrock, input is uncached tokens
#   and cache_creation/cache_read are from the cache_control markers. For
#   OpenAI, input is prompt_tokens minus cached_tokens (to match Anthropic's
#   convention), cache_creation is always 0, and cache_read is the automatic
#   prefix caching hit count. For Google, both cache fields are 0.
#   Exits non-zero on API errors.

set -euo pipefail

MODEL_ID="${1:?Usage: llm-call.sh <model_id> <system_prompt_file> <user_message_file> [max_tokens]}"
SYSTEM_PROMPT_FILE="${2:?Missing system prompt file}"
USER_MESSAGE_FILE="${3:?Missing user message file}"
MAX_TOKENS="${4:-4096}"
if ! [[ "$MAX_TOKENS" =~ ^[0-9]+$ ]]; then
  echo "ERROR: max_tokens must be a positive integer; got '${MAX_TOKENS}'" >&2
  exit 1
fi

# Fail fast on missing or empty prompt files. Anthropic rejects requests with
# system: "" (HTTP 400 "system must be a non-empty string"); other providers
# produce garbage output without a system prompt. Earlier detection here also
# avoids burning an API call on a preventable misconfiguration.
if [[ ! -s "$SYSTEM_PROMPT_FILE" ]]; then
  echo "ERROR: system prompt file is missing or empty: ${SYSTEM_PROMPT_FILE}" >&2
  exit 1
fi
if [[ ! -s "$USER_MESSAGE_FILE" ]]; then
  echo "ERROR: user message file is missing or empty: ${USER_MESSAGE_FILE}" >&2
  exit 1
fi

: "${AI_PROVIDER:?AI_PROVIDER is required (anthropic|openai|openai-compatible|google|bedrock-proxy)}"
TEMPERATURE="${AI_TEMPERATURE:-0.3}"
# Validate temperature is a number in [0, 2]; fall back to 0.3 if not.
if ! echo "$TEMPERATURE" | grep -qE '^[0-9]+(\.[0-9]+)?$'; then
  echo "WARNING: AI_TEMPERATURE '${TEMPERATURE}' is not a valid number; defaulting to 0.3." >&2
  TEMPERATURE="0.3"
elif awk "BEGIN { exit !($TEMPERATURE > 2) }"; then
  echo "WARNING: AI_TEMPERATURE '${TEMPERATURE}' exceeds maximum (2); clamping to 2." >&2
  TEMPERATURE="2"
fi

# Some models reject the temperature parameter: Claude Opus 4.7,
# OpenAI reasoning models (o-series), and GPT-5/5.5 (reasoning-capable).
model_supports_temperature() {
  case "$1" in
    *opus-4-7*|*opus-4.7*) return 1 ;;
    o1*|o3*|o4*) return 1 ;;
    gpt-5.5*|gpt-5-*|gpt-5) return 1 ;;
    *) return 0 ;;
  esac
}

# Prompt caching: resolve the effective flag. "auto" (default) enables caching
# on Anthropic and Bedrock paths (both use the Anthropic API shape with
# cache_control markers). Explicit "true"/"false" override the auto-detection.
# OpenAI's automatic prefix caching does not require a marker, so this flag
# is a no-op there. Google Gemini caching uses a separate cachedContents API
# and is not currently implemented.
#
# Trim surrounding whitespace so values like " true " or "\tauto\t" work as
# expected (operators occasionally set env vars via `export VAR=" value"`
# from copy-pasted secrets). Uses bash's extglob-free idiom.
LLM_PROMPT_CACHING="${LLM_PROMPT_CACHING:-auto}"
# Strip leading whitespace
LLM_PROMPT_CACHING="${LLM_PROMPT_CACHING#"${LLM_PROMPT_CACHING%%[![:space:]]*}"}"
# Strip trailing whitespace
LLM_PROMPT_CACHING="${LLM_PROMPT_CACHING%"${LLM_PROMPT_CACHING##*[![:space:]]}"}"
prompt_caching_enabled() {
  case "$LLM_PROMPT_CACHING" in
    true|TRUE|True|1) return 0 ;;
    false|FALSE|False|0) return 1 ;;
    auto|AUTO|Auto|"")
      case "${AI_PROVIDER:-}" in
        anthropic|bedrock-proxy) return 0 ;;
        *) return 1 ;;
      esac
      ;;
    *)
      echo "WARNING: LLM_PROMPT_CACHING='${LLM_PROMPT_CACHING}' is not a valid value; defaulting to auto." >&2
      case "${AI_PROVIDER:-}" in
        anthropic|bedrock-proxy) return 0 ;;
        *) return 1 ;;
      esac
      ;;
  esac
}

# Do NOT pre-expand SYSTEM_PROMPT / USER_MESSAGE into shell variables.
# Passing large strings via --arg hits ARG_MAX on big diffs.
# All provider functions use --rawfile to let jq read the files directly.

RESPONSE_FILE=$(mktemp /tmp/llm-response-XXXXXXXX.json)
trap 'rm -f "$RESPONSE_FILE"' EXIT

# ---------------------------------------------------------------------------
# Exit codes:
#   0 — success
#   1 — permanent / configuration error (bad API key, invalid request, unknown provider)
#   2 — transient error (all retries exhausted on 429/5xx/timeout)
#   3 — content issue (response blocked by provider safety/recitation filter)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

# Retry configuration (overridable via env vars, clamped to sane ranges)
LLM_RETRY_COUNT="${LLM_RETRY_COUNT:-3}"
LLM_RETRY_BASE_DELAY="${LLM_RETRY_BASE_DELAY:-2}"
if ! [[ "$LLM_RETRY_COUNT" =~ ^[0-9]+$ ]]; then
  echo "WARNING: LLM_RETRY_COUNT '${LLM_RETRY_COUNT}' is not a valid number; defaulting to 3." >&2
  LLM_RETRY_COUNT=3
elif [[ "$LLM_RETRY_COUNT" -gt 10 ]]; then
  echo "WARNING: LLM_RETRY_COUNT '${LLM_RETRY_COUNT}' exceeds maximum (10); clamping." >&2
  LLM_RETRY_COUNT=10
fi
if ! [[ "$LLM_RETRY_BASE_DELAY" =~ ^[0-9]+$ ]]; then
  echo "WARNING: LLM_RETRY_BASE_DELAY '${LLM_RETRY_BASE_DELAY}' is not a valid number; defaulting to 2." >&2
  LLM_RETRY_BASE_DELAY=2
elif [[ "$LLM_RETRY_BASE_DELAY" -gt 30 ]]; then
  echo "WARNING: LLM_RETRY_BASE_DELAY '${LLM_RETRY_BASE_DELAY}' exceeds maximum (30); clamping." >&2
  LLM_RETRY_BASE_DELAY=30
fi

# HTTP status codes considered transient (worth retrying)
is_transient_http() {
  local code="$1"
  case "$code" in
    408|429|500|502|503|504|520|521|522|523|524) return 0 ;;
    *) return 1 ;;
  esac
}

# curl exit codes considered transient (worth retrying)
is_transient_curl() {
  local code="$1"
  case "$code" in
    7|28|56) return 0 ;;  # 7=connection refused, 28=timeout, 56=network failure
    *) return 1 ;;
  esac
}

check_http_status() {
  local code="$1"
  if [[ "$code" -lt 200 || "$code" -ge 300 ]]; then
    echo "ERROR: LLM API returned HTTP ${code}" >&2
    cat "$RESPONSE_FILE" >&2
    if is_transient_http "$code"; then
      exit 2
    fi
    exit 1
  fi
}

# retry_curl — wrapper around curl that retries on transient failures.
# Usage: retry_curl <provider_label> [curl_args...]
# Retries on transient HTTP codes (408, 429, 500, 502, 503, 504, 520–524) and
# transient curl exit codes (7=connection refused, 28=timeout, 56=network failure).
# Uses exponential backoff with jitter.
retry_curl() {
  local provider_label="$1"; shift
  local attempt=0 http_code curl_exit backoff jitter

  # Capture stdin to a temp file so retries can re-read the request body.
  # Without this, piped input (echo "$body" | retry_curl ... --data-binary @-)
  # would be consumed on the first curl attempt, leaving retries with empty bodies.
  local stdin_file
  stdin_file=$(mktemp /tmp/llm-retry-stdin-XXXXXXXX)
  # Clean up on function return (covers both success return 0 and exit N paths).
  # EXIT is intentionally omitted: EXIT traps inside functions accumulate on the
  # shell's EXIT handler across calls and are never cleared on RETURN.
  # shellcheck disable=SC2064
  trap "rm -f '${stdin_file}'" RETURN
  cat > "$stdin_file"

  # Rewrite --data-binary @- to --data-binary @<file> so each retry
  # re-reads the captured body instead of exhausted stdin.
  local -a curl_args=()
  local prev=""
  for arg in "$@"; do
    if [[ "$prev" == "--data-binary" && "$arg" == "@-" ]]; then
      curl_args+=("@${stdin_file}")
    else
      curl_args+=("$arg")
    fi
    prev="$arg"
  done

  while true; do
    curl_exit=0
    http_code=$(curl "${curl_args[@]}" 2>/dev/null) || curl_exit=$?

    if [[ "$curl_exit" -ne 0 ]]; then
      # curl itself failed (not an HTTP error)
      if [[ "$attempt" -lt "$LLM_RETRY_COUNT" ]] && is_transient_curl "$curl_exit"; then
        attempt=$((attempt + 1))
        backoff=$(( LLM_RETRY_BASE_DELAY * (1 << (attempt - 1)) ))
        jitter=$(( RANDOM % 1000 ))  # milliseconds, formatted as fractional seconds for sleep
        echo "WARNING: ${provider_label} curl failed (exit ${curl_exit}), retrying in ${backoff}.${jitter}s (attempt ${attempt}/${LLM_RETRY_COUNT})..." >&2
        sleep "${backoff}.${jitter}"
        continue
      fi
      echo "ERROR: ${provider_label} API request failed (curl exit ${curl_exit})" >&2
      rm -f "$stdin_file"
      if is_transient_curl "$curl_exit"; then
        exit 2
      fi
      exit 1
    fi

    # curl succeeded — check HTTP status
    if [[ "$http_code" -ge 200 && "$http_code" -lt 300 ]]; then
      rm -f "$stdin_file"
      return 0
    fi

    # Non-2xx HTTP response
    if [[ "$attempt" -lt "$LLM_RETRY_COUNT" ]] && is_transient_http "$http_code"; then
      attempt=$((attempt + 1))
      backoff=$(( LLM_RETRY_BASE_DELAY * (1 << (attempt - 1)) ))
      jitter=$(( RANDOM % 1000 ))  # milliseconds, formatted as fractional seconds for sleep
      echo "WARNING: ${provider_label} returned HTTP ${http_code}, retrying in ${backoff}.${jitter}s (attempt ${attempt}/${LLM_RETRY_COUNT})..." >&2
      sleep "${backoff}.${jitter}"
      continue
    fi

    # Not retryable or retries exhausted — let check_http_status handle the exit
    rm -f "$stdin_file"
    check_http_status "$http_code"
  done
}

emit_response() {
  local response_text="$1" input_tokens="$2" output_tokens="$3" stop_reason="${4:-}"
  local cache_creation_tokens="${5:-0}" cache_read_tokens="${6:-0}"

  # Check provider-specific content filter reasons before the empty-response check
  # so error messages are specific rather than generic "Could not extract response text"
  _dump_response_file() {
    echo "Raw response:" >&2
    if [[ -s "${RESPONSE_FILE:-}" ]]; then cat "$RESPONSE_FILE" >&2; else echo "(response body unavailable)" >&2; fi
  }

  if [[ "$stop_reason" == "SAFETY" ]]; then
    echo "ERROR: Response blocked by provider safety filter (finishReason=SAFETY)" >&2
    _dump_response_file; exit 3
  fi
  if [[ "$stop_reason" == "RECITATION" ]]; then
    echo "ERROR: Response blocked by provider recitation filter (finishReason=RECITATION)" >&2
    _dump_response_file; exit 3
  fi
  if [[ "$stop_reason" == "refusal" ]]; then
    echo "ERROR: Response blocked by model refusal (stop_reason=refusal)" >&2
    _dump_response_file; exit 3
  fi

  if [[ -z "$response_text" ]]; then
    echo "ERROR: Could not extract response text from API response" >&2
    _dump_response_file; exit 1
  fi
  # Token line format: input and output are always present; cache_creation and
  # cache_read default to 0 when caching didn't engage. Downstream parsers
  # (review.sh:emit_token_table_rows) accept the extended form and treat
  # missing fields as 0 for backward compatibility.
  echo "TOKENS: input=${input_tokens} output=${output_tokens} cache_creation=${cache_creation_tokens} cache_read=${cache_read_tokens} model=${MODEL_ID}" >&2
  # Warn and signal callers when the model hit the token limit
  if [[ "$stop_reason" == "max_tokens" || "$stop_reason" == "length" || "$stop_reason" == "MAX_TOKENS" ]]; then
    echo "WARNING: response truncated (stop_reason=${stop_reason}); output may be incomplete" >&2
    echo "TRUNCATED:true" >&2
  fi
  printf '%s\n' "$response_text"
}

# Build an Anthropic-shaped request body (shared by call_anthropic and
# call_bedrock_proxy — bedrock-proxy wraps the same API with a version field).
#
# Caching strategy (issues #122 + #142):
#
# Anthropic's cache key is CUMULATIVE: a hash of the full prefix up to and
# including each cache_control marker. Two requests with identical user-message
# content but different system prompts will NOT share a cache entry if the
# marker is anywhere in or after the differing system prompt — the preceding
# bytes differ, so the hash differs.
#
# To unlock shared caching across agents that have different system prompts
# but the same user context (CODE_CONTEXT_MSG reused across 4–5 agents per
# review run), we restructure the request when caching is enabled:
#
#   system: [
#     { "text": "<USER_MESSAGE_FILE contents>", "cache_control": ephemeral },
#     { "text": "<SYSTEM_PROMPT_FILE contents>" }
#   ]
#   messages: [{ "role": "user", "content": "Please perform the review." }]
#
# Why this works:
#   * Anthropic's cache hierarchy is `tools → system → messages`. The cache
#     key at the end of system[0] covers only tools (empty) + system[0] —
#     identical across every agent because USER_MESSAGE_FILE is the shared
#     CODE_CONTEXT_MSG / FULL_CONTEXT_MSG.
#   * Even when system[1] (the agent prompt) differs per agent, Anthropic
#     walks backward looking for earlier cache entries. The cumulative hash
#     at end of system[0] IS shared, so every agent after the first hits
#     cache on the context bytes (typically the bulk of input tokens).
#   * The agent-specific system[1] is always processed fresh (it was going
#     to be per-agent anyway; no regression).
#   * The messages array carries only a trivial sentinel turn. It's small
#     enough that sending it uncached per request is negligible.
#
# This is a semantic change from the pre-#142 layout (where the diff lived
# in messages[0].content and the agent prompt in system). Prompts in this
# repo were written for the old layout — if any agent's behavior changes
# meaningfully, that's a quality issue worth benchmarking. Empirically the
# model treats late-system content as "additional instructions" and
# early-system content as "context," which matches our goal.
#
# When caching is DISABLED, the request falls back to the legacy layout
# (system: agent prompt as string, messages[0].content: user as string) —
# bit-for-bit identical to pre-#122 for providers that don't support
# prompt caching or when operators opt out via LLM_PROMPT_CACHING=false.
#
# Args:
#   $1 — extra_fields_jq  (e.g. '{anthropic_version: "bedrock-2023-05-31"}' or '{}')
#   $2 — include_model    ("true" to include .model field; Bedrock paths put
#                          the model in the URL instead)
_build_anthropic_body() {
  local extra_fields="$1" include_model="$2"
  local caching_flag include_model_flag temperature_val model_val
  if prompt_caching_enabled; then caching_flag="true"; else caching_flag="false"; fi
  if [[ "$include_model" == "true" ]]; then
    include_model_flag="true"
    model_val="$MODEL_ID"
  else
    include_model_flag="false"
    model_val=""
  fi
  # jq requires all --argjson vars to be defined; use JSON null for "not
  # applicable" cases. The filter checks for null and omits the field.
  if model_supports_temperature "$MODEL_ID"; then
    temperature_val="$TEMPERATURE"
  else
    temperature_val="null"
  fi

  # Build the body: jq-merge the caller-provided extra fields (e.g.
  # anthropic_version for Bedrock), the optional model field, then the
  # core message/system/max_tokens block, then the optional temperature.
  jq -n \
    --arg model "$model_val" \
    --rawfile system "$SYSTEM_PROMPT_FILE" \
    --rawfile user "$USER_MESSAGE_FILE" \
    --argjson max_tokens "$MAX_TOKENS" \
    --argjson temperature "$temperature_val" \
    --argjson caching "$caching_flag" \
    --argjson extra "$extra_fields" \
    --argjson include_model "$include_model_flag" \
    '
    ($extra) +
    (if $include_model then {model: $model} else {} end) +
    {
      system: (
        if $caching then
          # Shared-cache layout: the reused user content (CODE_CONTEXT_MSG)
          # becomes system[0] with a cache_control marker; the per-agent
          # system prompt becomes system[1]. Anthropic walks backward and
          # finds the shared hash at end of system[0] regardless of what
          # system[1] says, so agents 2-N hit the cache on context.
          [
            {type: "text", text: $user, cache_control: {type: "ephemeral"}},
            {type: "text", text: $system}
          ]
        else
          # Legacy layout (caching off): system is the agent prompt, full stop.
          $system
        end
      ),
      messages: (
        if $caching then
          # Sentinel user turn — the real context is in system[0] above.
          # Keep this minimal and stable so it contributes nothing meaningful
          # to the cache hash and doesn''t confuse the model about what to
          # produce.
          [{role: "user", content: "Please perform your review now."}]
        else
          # Legacy layout (caching off): user content goes in messages[0].
          [{role: "user", content: $user}]
        end
      ),
      max_tokens: $max_tokens
    } +
    (if $temperature != null then {temperature: $temperature} else {} end)
    '
}

# ---------------------------------------------------------------------------
# Provider: Anthropic direct (api.anthropic.com)
# ---------------------------------------------------------------------------
call_anthropic() {
  : "${ANTHROPIC_API_KEY:?ANTHROPIC_API_KEY is required for AI_PROVIDER=anthropic}"

  local request_body
  request_body=$(_build_anthropic_body '{}' "true") || {
    echo "ERROR: jq failed to build Anthropic request body" >&2
    exit 1
  }

  echo "$request_body" | retry_curl "Anthropic" \
    -s -w "%{http_code}" -o "$RESPONSE_FILE" \
    --max-time 180 \
    -X POST "https://api.anthropic.com/v1/messages" \
    -H "x-api-key: ${ANTHROPIC_API_KEY}" \
    -H "anthropic-version: 2023-06-01" \
    -H "Content-Type: application/json" \
    --data-binary @-

  local response_text input_tokens output_tokens stop_reason cache_creation cache_read
  response_text=$(jq -r '.content[0].text // empty' "$RESPONSE_FILE" 2>/dev/null)
  input_tokens=$(jq -r '.usage.input_tokens // "0"' "$RESPONSE_FILE" 2>/dev/null)
  output_tokens=$(jq -r '.usage.output_tokens // "0"' "$RESPONSE_FILE" 2>/dev/null)
  cache_creation=$(jq -r '.usage.cache_creation_input_tokens // "0"' "$RESPONSE_FILE" 2>/dev/null)
  cache_read=$(jq -r '.usage.cache_read_input_tokens // "0"' "$RESPONSE_FILE" 2>/dev/null)
  stop_reason=$(jq -r '.stop_reason // empty' "$RESPONSE_FILE" 2>/dev/null)
  emit_response "$response_text" "$input_tokens" "$output_tokens" "$stop_reason" "$cache_creation" "$cache_read"
}

# ---------------------------------------------------------------------------
# Provider: OpenAI (api.openai.com) or OpenAI-compatible endpoint
# ---------------------------------------------------------------------------
call_openai() {
  : "${OPENAI_API_KEY:?OPENAI_API_KEY is required for AI_PROVIDER=${AI_PROVIDER}}"
  local base_url="${OPENAI_BASE_URL:-https://api.openai.com/v1}"

  # OpenAI deprecated max_tokens in favor of max_completion_tokens for newer
  # models (gpt-4.1, o3, o4-mini). gpt-4o accepts both. Use the new field
  # for first-party OpenAI; keep the legacy field for openai-compatible
  # endpoints where third-party providers may not support it yet.
  local token_field="max_completion_tokens"
  if [[ "$AI_PROVIDER" == "openai-compatible" ]]; then
    token_field="max_tokens"
  fi

  local temperature_val
  if model_supports_temperature "$MODEL_ID"; then
    temperature_val="$TEMPERATURE"
  else
    temperature_val="null"
  fi

  local request_body
  request_body=$(jq -n \
    --arg model "$MODEL_ID" \
    --rawfile system "$SYSTEM_PROMPT_FILE" \
    --rawfile user "$USER_MESSAGE_FILE" \
    --argjson max_tokens "$MAX_TOKENS" \
    --argjson temperature "$temperature_val" \
    --arg token_field "$token_field" \
    '{model: $model, messages: [{role: "system", content: $system}, {role: "user", content: $user}], ($token_field): $max_tokens} + (if $temperature != null then {temperature: $temperature} else {} end)') || {
    echo "ERROR: jq failed to build OpenAI request body" >&2
    exit 1
  }

  echo "$request_body" | retry_curl "OpenAI" \
    -s -w "%{http_code}" -o "$RESPONSE_FILE" \
    --max-time 180 \
    -X POST "${base_url}/chat/completions" \
    -H "Authorization: Bearer ${OPENAI_API_KEY}" \
    -H "Content-Type: application/json" \
    --data-binary @-

  local response_text input_tokens output_tokens stop_reason cache_read
  response_text=$(jq -r '.choices[0].message.content // empty' "$RESPONSE_FILE" 2>/dev/null)
  input_tokens=$(jq -r '.usage.prompt_tokens // "0"' "$RESPONSE_FILE" 2>/dev/null)
  output_tokens=$(jq -r '.usage.completion_tokens // "0"' "$RESPONSE_FILE" 2>/dev/null)
  cache_read=$(jq -r '.usage.prompt_tokens_details.cached_tokens // "0"' "$RESPONSE_FILE" 2>/dev/null)
  stop_reason=$(jq -r '.choices[0].finish_reason // empty' "$RESPONSE_FILE" 2>/dev/null)

  # Sanitize to integers before arithmetic (defensive against unexpected
  # floats or empty strings from the API response).
  input_tokens=$(( ${input_tokens%%.*} + 0 ))
  cache_read=$(( ${cache_read%%.*} + 0 ))

  # OpenAI's prompt_tokens INCLUDES cached_tokens as a subset. Subtract to
  # match Anthropic's convention where input_tokens = uncached only, so the
  # cost formula (input*input_rate + cache_read*cache_read_rate) works
  # correctly without double-counting.
  local uncached_input=$(( input_tokens - cache_read ))
  if (( uncached_input < 0 )); then uncached_input=$input_tokens; cache_read=0; fi
  emit_response "$response_text" "$uncached_input" "$output_tokens" "$stop_reason" "0" "$cache_read"
}

# ---------------------------------------------------------------------------
# Provider: Google Gemini (generativelanguage.googleapis.com)
# ---------------------------------------------------------------------------
call_google() {
  : "${GOOGLE_API_KEY:?GOOGLE_API_KEY is required for AI_PROVIDER=google}"

  local request_body
  request_body=$(jq -n \
    --rawfile system "$SYSTEM_PROMPT_FILE" \
    --rawfile user "$USER_MESSAGE_FILE" \
    --argjson max_tokens "$MAX_TOKENS" \
    --argjson temperature "$TEMPERATURE" \
    '{
      system_instruction: {parts: [{text: $system}]},
      contents: [{role: "user", parts: [{text: $user}]}],
      generationConfig: {maxOutputTokens: $max_tokens, temperature: $temperature}
    }') || {
    echo "ERROR: jq failed to build Google request body" >&2
    exit 1
  }

  local encoded_model_id
  encoded_model_id=$(printf '%s' "$MODEL_ID" | jq -sRr @uri)

  echo "$request_body" | retry_curl "Google Gemini" \
    -s -w "%{http_code}" -o "$RESPONSE_FILE" \
    --max-time 180 \
    -X POST "https://generativelanguage.googleapis.com/v1beta/models/${encoded_model_id}:generateContent" \
    -H "x-goog-api-key: ${GOOGLE_API_KEY}" \
    -H "Content-Type: application/json" \
    --data-binary @-

  local response_text input_tokens output_tokens stop_reason
  response_text=$(jq -r '.candidates[0].content.parts[0].text // empty' "$RESPONSE_FILE" 2>/dev/null)
  input_tokens=$(jq -r '.usageMetadata.promptTokenCount // "0"' "$RESPONSE_FILE" 2>/dev/null)
  output_tokens=$(jq -r '.usageMetadata.candidatesTokenCount // "0"' "$RESPONSE_FILE" 2>/dev/null)
  stop_reason=$(jq -r '.candidates[0].finishReason // empty' "$RESPONSE_FILE" 2>/dev/null)
  emit_response "$response_text" "$input_tokens" "$output_tokens" "$stop_reason"
}

# ---------------------------------------------------------------------------
# Provider: Bedrock proxy (Tag1 OpenWebUI proxy or similar)
# ---------------------------------------------------------------------------
call_bedrock_proxy() {
  : "${BEDROCK_API_URL:?BEDROCK_API_URL is required for AI_PROVIDER=bedrock-proxy}"
  : "${BEDROCK_API_KEY:?BEDROCK_API_KEY is required for AI_PROVIDER=bedrock-proxy}"

  # Bedrock uses Anthropic's request shape with an anthropic_version field.
  # Model is encoded into the URL path (not the request body).
  local request_body
  request_body=$(_build_anthropic_body '{"anthropic_version": "bedrock-2023-05-31"}' "false") || {
    echo "ERROR: jq failed to build Bedrock request body" >&2
    exit 1
  }

  local encoded_model_id
  encoded_model_id=$(printf '%s' "$MODEL_ID" | jq -sRr @uri)
  local url="${BEDROCK_API_URL}/model/${encoded_model_id}/invoke"

  echo "$request_body" | retry_curl "Bedrock proxy" \
    -s -w "%{http_code}" -o "$RESPONSE_FILE" \
    --max-time 180 \
    -X POST "$url" \
    -H "Authorization: Bearer ${BEDROCK_API_KEY}" \
    -H "Content-Type: application/json" \
    --data-binary @-

  local response_text input_tokens output_tokens stop_reason cache_creation cache_read
  response_text=$(jq -r '.content[0].text // empty' "$RESPONSE_FILE" 2>/dev/null)
  input_tokens=$(jq -r '.usage.input_tokens // "0"' "$RESPONSE_FILE" 2>/dev/null)
  output_tokens=$(jq -r '.usage.output_tokens // "0"' "$RESPONSE_FILE" 2>/dev/null)
  cache_creation=$(jq -r '.usage.cache_creation_input_tokens // "0"' "$RESPONSE_FILE" 2>/dev/null)
  cache_read=$(jq -r '.usage.cache_read_input_tokens // "0"' "$RESPONSE_FILE" 2>/dev/null)
  stop_reason=$(jq -r '.stop_reason // empty' "$RESPONSE_FILE" 2>/dev/null)
  emit_response "$response_text" "$input_tokens" "$output_tokens" "$stop_reason" "$cache_creation" "$cache_read"
}

# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------
case "$AI_PROVIDER" in
  anthropic)
    call_anthropic
    ;;
  openai|openai-compatible)
    call_openai
    ;;
  google)
    call_google
    ;;
  bedrock-proxy)
    call_bedrock_proxy
    ;;
  *)
    echo "ERROR: Unknown AI_PROVIDER '${AI_PROVIDER}'. Valid values: anthropic | openai | openai-compatible | google | bedrock-proxy" >&2
    exit 1
    ;;
esac
