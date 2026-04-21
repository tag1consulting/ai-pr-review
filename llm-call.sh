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
#   Emits "TOKENS: input=N output=N model=M" to stderr for usage tracking.
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
# Retries on transient HTTP codes (429, 500, 502, 503) and transient curl
# exit codes (7=connection refused, 28=timeout, 56=network failure).
# Uses exponential backoff with jitter.
retry_curl() {
  local provider_label="$1"; shift
  local attempt=0 http_code curl_exit backoff jitter

  # Capture stdin to a temp file so retries can re-read the request body.
  # Without this, piped input (echo "$body" | retry_curl ... --data-binary @-)
  # would be consumed on the first curl attempt, leaving retries with empty bodies.
  local stdin_file
  stdin_file=$(mktemp /tmp/llm-retry-stdin-XXXXXXXX)
  # Ensure the temp file is removed on all exit paths (success return, exit, or signal).
  # shellcheck disable=SC2064
  trap "rm -f '${stdin_file}'" RETURN EXIT
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
  echo "TOKENS: input=${input_tokens} output=${output_tokens} model=${MODEL_ID}" >&2
  # Warn and signal callers when the model hit the token limit
  if [[ "$stop_reason" == "max_tokens" || "$stop_reason" == "length" || "$stop_reason" == "MAX_TOKENS" ]]; then
    echo "WARNING: response truncated (stop_reason=${stop_reason}); output may be incomplete" >&2
    echo "TRUNCATED:true" >&2
  fi
  printf '%s\n' "$response_text"
}

# ---------------------------------------------------------------------------
# Provider: Anthropic direct (api.anthropic.com)
# ---------------------------------------------------------------------------
call_anthropic() {
  : "${ANTHROPIC_API_KEY:?ANTHROPIC_API_KEY is required for AI_PROVIDER=anthropic}"

  local request_body
  request_body=$(jq -n \
    --arg model "$MODEL_ID" \
    --rawfile system "$SYSTEM_PROMPT_FILE" \
    --rawfile user "$USER_MESSAGE_FILE" \
    --argjson max_tokens "$MAX_TOKENS" \
    --argjson temperature "$TEMPERATURE" \
    '{model: $model, system: $system, messages: [{role: "user", content: $user}], max_tokens: $max_tokens, temperature: $temperature}') || {
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

  local response_text input_tokens output_tokens stop_reason
  response_text=$(jq -r '.content[0].text // empty' "$RESPONSE_FILE" 2>/dev/null)
  input_tokens=$(jq -r '.usage.input_tokens // "0"' "$RESPONSE_FILE" 2>/dev/null)
  output_tokens=$(jq -r '.usage.output_tokens // "0"' "$RESPONSE_FILE" 2>/dev/null)
  stop_reason=$(jq -r '.stop_reason // empty' "$RESPONSE_FILE" 2>/dev/null)
  emit_response "$response_text" "$input_tokens" "$output_tokens" "$stop_reason"
}

# ---------------------------------------------------------------------------
# Provider: OpenAI (api.openai.com) or OpenAI-compatible endpoint
# ---------------------------------------------------------------------------
call_openai() {
  : "${OPENAI_API_KEY:?OPENAI_API_KEY is required for AI_PROVIDER=${AI_PROVIDER}}"
  local base_url="${OPENAI_BASE_URL:-https://api.openai.com/v1}"

  local request_body
  request_body=$(jq -n \
    --arg model "$MODEL_ID" \
    --rawfile system "$SYSTEM_PROMPT_FILE" \
    --rawfile user "$USER_MESSAGE_FILE" \
    --argjson max_tokens "$MAX_TOKENS" \
    --argjson temperature "$TEMPERATURE" \
    '{model: $model, messages: [{role: "system", content: $system}, {role: "user", content: $user}], max_tokens: $max_tokens, temperature: $temperature}') || {
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

  local response_text input_tokens output_tokens stop_reason
  response_text=$(jq -r '.choices[0].message.content // empty' "$RESPONSE_FILE" 2>/dev/null)
  input_tokens=$(jq -r '.usage.prompt_tokens // "0"' "$RESPONSE_FILE" 2>/dev/null)
  output_tokens=$(jq -r '.usage.completion_tokens // "0"' "$RESPONSE_FILE" 2>/dev/null)
  stop_reason=$(jq -r '.choices[0].finish_reason // empty' "$RESPONSE_FILE" 2>/dev/null)
  emit_response "$response_text" "$input_tokens" "$output_tokens" "$stop_reason"
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

  local request_body
  request_body=$(jq -n \
    --rawfile system "$SYSTEM_PROMPT_FILE" \
    --rawfile user "$USER_MESSAGE_FILE" \
    --argjson max_tokens "$MAX_TOKENS" \
    --argjson temperature "$TEMPERATURE" \
    '{
      anthropic_version: "bedrock-2023-05-31",
      system: $system,
      messages: [{role: "user", content: $user}],
      max_tokens: $max_tokens,
      temperature: $temperature
    }') || {
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

  local response_text input_tokens output_tokens stop_reason
  response_text=$(jq -r '.content[0].text // empty' "$RESPONSE_FILE" 2>/dev/null)
  input_tokens=$(jq -r '.usage.input_tokens // "0"' "$RESPONSE_FILE" 2>/dev/null)
  output_tokens=$(jq -r '.usage.output_tokens // "0"' "$RESPONSE_FILE" 2>/dev/null)
  stop_reason=$(jq -r '.stop_reason // empty' "$RESPONSE_FILE" 2>/dev/null)
  emit_response "$response_text" "$input_tokens" "$output_tokens" "$stop_reason"
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
