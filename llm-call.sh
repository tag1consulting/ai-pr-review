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

: "${AI_PROVIDER:?AI_PROVIDER is required (anthropic|openai|openai-compatible|google|bedrock-proxy)}"
TEMPERATURE="${AI_TEMPERATURE:-0.3}"
# Validate temperature is a number in [0, 2]; fall back to 0.3 if not.
if ! echo "$TEMPERATURE" | grep -qE '^[0-9]+(\.[0-9]+)?$'; then
  echo "WARNING: AI_TEMPERATURE '${TEMPERATURE}' is not a valid number; defaulting to 0.3." >&2
  TEMPERATURE="0.3"
fi

SYSTEM_PROMPT=$(cat "$SYSTEM_PROMPT_FILE")
USER_MESSAGE=$(cat "$USER_MESSAGE_FILE")

RESPONSE_FILE=$(mktemp /tmp/llm-response-XXXXXXXX.json)
trap 'rm -f "$RESPONSE_FILE"' EXIT

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

check_http_status() {
  local code="$1"
  if [[ "$code" -lt 200 || "$code" -ge 300 ]]; then
    echo "ERROR: LLM API returned HTTP ${code}" >&2
    cat "$RESPONSE_FILE" >&2
    exit 1
  fi
}

emit_response() {
  local response_text="$1" input_tokens="$2" output_tokens="$3"
  if [[ -z "$response_text" ]]; then
    echo "ERROR: Could not extract response text from API response" >&2
    echo "Raw response:" >&2
    cat "$RESPONSE_FILE" >&2
    exit 1
  fi
  echo "TOKENS: input=${input_tokens} output=${output_tokens} model=${MODEL_ID}" >&2
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
    --arg system "$SYSTEM_PROMPT" \
    --arg user "$USER_MESSAGE" \
    --argjson max_tokens "$MAX_TOKENS" \
    --argjson temperature "$TEMPERATURE" \
    '{model: $model, system: $system, messages: [{role: "user", content: $user}], max_tokens: $max_tokens, temperature: $temperature}') || {
    echo "ERROR: jq failed to build Anthropic request body" >&2
    exit 1
  }

  local http_code
  http_code=$(curl -s -w "%{http_code}" -o "$RESPONSE_FILE" \
    --max-time 180 \
    -X POST "https://api.anthropic.com/v1/messages" \
    -H "x-api-key: ${ANTHROPIC_API_KEY}" \
    -H "anthropic-version: 2023-06-01" \
    -H "Content-Type: application/json" \
    -d "$request_body") || {
    echo "ERROR: Anthropic API request failed" >&2
    exit 1
  }
  check_http_status "$http_code"

  local response_text input_tokens output_tokens
  response_text=$(jq -r '.content[0].text // empty' "$RESPONSE_FILE" 2>/dev/null)
  input_tokens=$(jq -r '.usage.input_tokens // "?"' "$RESPONSE_FILE" 2>/dev/null)
  output_tokens=$(jq -r '.usage.output_tokens // "?"' "$RESPONSE_FILE" 2>/dev/null)
  emit_response "$response_text" "$input_tokens" "$output_tokens"
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
    --arg system "$SYSTEM_PROMPT" \
    --arg user "$USER_MESSAGE" \
    --argjson max_tokens "$MAX_TOKENS" \
    --argjson temperature "$TEMPERATURE" \
    '{model: $model, messages: [{role: "system", content: $system}, {role: "user", content: $user}], max_tokens: $max_tokens, temperature: $temperature}') || {
    echo "ERROR: jq failed to build OpenAI request body" >&2
    exit 1
  }

  local http_code
  http_code=$(curl -s -w "%{http_code}" -o "$RESPONSE_FILE" \
    --max-time 180 \
    -X POST "${base_url}/chat/completions" \
    -H "Authorization: Bearer ${OPENAI_API_KEY}" \
    -H "Content-Type: application/json" \
    -d "$request_body") || {
    echo "ERROR: OpenAI API request failed" >&2
    exit 1
  }
  check_http_status "$http_code"

  local response_text input_tokens output_tokens
  response_text=$(jq -r '.choices[0].message.content // empty' "$RESPONSE_FILE" 2>/dev/null)
  input_tokens=$(jq -r '.usage.prompt_tokens // "?"' "$RESPONSE_FILE" 2>/dev/null)
  output_tokens=$(jq -r '.usage.completion_tokens // "?"' "$RESPONSE_FILE" 2>/dev/null)
  emit_response "$response_text" "$input_tokens" "$output_tokens"
}

# ---------------------------------------------------------------------------
# Provider: Google Gemini (generativelanguage.googleapis.com)
# ---------------------------------------------------------------------------
call_google() {
  : "${GOOGLE_API_KEY:?GOOGLE_API_KEY is required for AI_PROVIDER=google}"

  local request_body
  request_body=$(jq -n \
    --arg system "$SYSTEM_PROMPT" \
    --arg user "$USER_MESSAGE" \
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

  local http_code
  http_code=$(curl -s -w "%{http_code}" -o "$RESPONSE_FILE" \
    --max-time 180 \
    -X POST "https://generativelanguage.googleapis.com/v1beta/models/${encoded_model_id}:generateContent" \
    -H "x-goog-api-key: ${GOOGLE_API_KEY}" \
    -H "Content-Type: application/json" \
    -d "$request_body") || {
    echo "ERROR: Google Gemini API request failed" >&2
    exit 1
  }
  check_http_status "$http_code"

  local response_text input_tokens output_tokens
  response_text=$(jq -r '.candidates[0].content.parts[0].text // empty' "$RESPONSE_FILE" 2>/dev/null)
  input_tokens=$(jq -r '.usageMetadata.promptTokenCount // "?"' "$RESPONSE_FILE" 2>/dev/null)
  output_tokens=$(jq -r '.usageMetadata.candidatesTokenCount // "?"' "$RESPONSE_FILE" 2>/dev/null)
  emit_response "$response_text" "$input_tokens" "$output_tokens"
}

# ---------------------------------------------------------------------------
# Provider: Bedrock proxy (Tag1 OpenWebUI proxy or similar)
# ---------------------------------------------------------------------------
call_bedrock_proxy() {
  : "${BEDROCK_API_URL:?BEDROCK_API_URL is required for AI_PROVIDER=bedrock-proxy}"
  : "${BEDROCK_API_KEY:?BEDROCK_API_KEY is required for AI_PROVIDER=bedrock-proxy}"

  local request_body
  request_body=$(jq -n \
    --arg system "$SYSTEM_PROMPT" \
    --arg user "$USER_MESSAGE" \
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

  local http_code
  http_code=$(curl -s -w "%{http_code}" -o "$RESPONSE_FILE" \
    --max-time 180 \
    -X POST "$url" \
    -H "Authorization: Bearer ${BEDROCK_API_KEY}" \
    -H "Content-Type: application/json" \
    -d "$request_body") || {
    echo "ERROR: Bedrock proxy API request failed" >&2
    cat "$RESPONSE_FILE" >&2
    exit 1
  }
  check_http_status "$http_code"

  local response_text input_tokens output_tokens
  response_text=$(jq -r '.content[0].text // empty' "$RESPONSE_FILE" 2>/dev/null)
  input_tokens=$(jq -r '.usage.input_tokens // "?"' "$RESPONSE_FILE" 2>/dev/null)
  output_tokens=$(jq -r '.usage.output_tokens // "?"' "$RESPONSE_FILE" 2>/dev/null)
  emit_response "$response_text" "$input_tokens" "$output_tokens"
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
