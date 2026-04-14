#!/usr/bin/env bats
# Tests for pure functions in review.sh.
# Functions are extracted individually at test time — the full script is never
# sourced, so the orchestration pipeline does not run.

setup() {
  load test_helper
  load_function "${PROJECT_ROOT}/review.sh" detect_language
  load_function "${PROJECT_ROOT}/review.sh" model_pricing
  load_function "${PROJECT_ROOT}/review.sh" model_display_name
  load_function "${PROJECT_ROOT}/review.sh" format_cost
}

# ---------------------------------------------------------------------------
# detect_language
# ---------------------------------------------------------------------------

@test "detect_language: go -> Go" {
  run detect_language "go"
  [ "$status" -eq 0 ]
  [ "$output" = "Go" ]
}

@test "detect_language: py -> Python" {
  run detect_language "py"
  [ "$status" -eq 0 ]
  [ "$output" = "Python" ]
}

@test "detect_language: ts -> TypeScript" {
  run detect_language "ts"
  [ "$status" -eq 0 ]
  [ "$output" = "TypeScript" ]
}

@test "detect_language: tsx -> TypeScript" {
  run detect_language "tsx"
  [ "$status" -eq 0 ]
  [ "$output" = "TypeScript" ]
}

@test "detect_language: js -> JavaScript" {
  run detect_language "js"
  [ "$status" -eq 0 ]
  [ "$output" = "JavaScript" ]
}

@test "detect_language: sh -> Shell" {
  run detect_language "sh"
  [ "$status" -eq 0 ]
  [ "$output" = "Shell" ]
}

@test "detect_language: bash -> Shell" {
  run detect_language "bash"
  [ "$status" -eq 0 ]
  [ "$output" = "Shell" ]
}

@test "detect_language: module -> PHP" {
  run detect_language "module"
  [ "$status" -eq 0 ]
  [ "$output" = "PHP" ]
}

@test "detect_language: php -> PHP" {
  run detect_language "php"
  [ "$status" -eq 0 ]
  [ "$output" = "PHP" ]
}

@test "detect_language: tf -> Terraform" {
  run detect_language "tf"
  [ "$status" -eq 0 ]
  [ "$output" = "Terraform" ]
}

@test "detect_language: yml -> YAML" {
  run detect_language "yml"
  [ "$status" -eq 0 ]
  [ "$output" = "YAML" ]
}

@test "detect_language: unknown extension -> empty string" {
  run detect_language "xyz"
  [ "$status" -eq 0 ]
  [ "$output" = "" ]
}

@test "detect_language: empty extension -> empty string" {
  run detect_language ""
  [ "$status" -eq 0 ]
  [ "$output" = "" ]
}

# ---------------------------------------------------------------------------
# model_pricing
# ---------------------------------------------------------------------------

@test "model_pricing: claude-sonnet-4-6 returns correct rates" {
  run model_pricing "claude-sonnet-4-6-20250514"
  [ "$status" -eq 0 ]
  [ "$output" = "3000000 15000000" ]
}

@test "model_pricing: claude-opus-4-6 returns correct rates" {
  run model_pricing "claude-opus-4-6-20250514"
  [ "$status" -eq 0 ]
  [ "$output" = "15000000 75000000" ]
}

@test "model_pricing: claude-haiku-4-5 returns correct rates" {
  run model_pricing "claude-haiku-4-5-20251001"
  [ "$status" -eq 0 ]
  [ "$output" = "800000 4000000" ]
}

@test "model_pricing: gpt-4o-mini is not shadowed by gpt-4o pattern" {
  run model_pricing "gpt-4o-mini"
  [ "$status" -eq 0 ]
  [ "$output" = "150000 600000" ]
}

@test "model_pricing: gpt-4o returns correct rates" {
  run model_pricing "gpt-4o"
  [ "$status" -eq 0 ]
  [ "$output" = "2500000 10000000" ]
}

@test "model_pricing: gemini-2.5-flash returns correct rates" {
  run model_pricing "gemini-2.5-flash"
  [ "$status" -eq 0 ]
  [ "$output" = "150000 3500000" ]
}

@test "model_pricing: gemini-2.5-pro returns correct rates" {
  run model_pricing "gemini-2.5-pro"
  [ "$status" -eq 0 ]
  [ "$output" = "1250000 10000000" ]
}

@test "model_pricing: unknown model returns zero rates" {
  run model_pricing "some-unknown-model-v1"
  [ "$status" -eq 0 ]
  [ "$output" = "0 0" ]
}

@test "model_pricing: bedrock-prefixed claude-sonnet matches" {
  run model_pricing "us.anthropic.claude-sonnet-4-6"
  [ "$status" -eq 0 ]
  [ "$output" = "3000000 15000000" ]
}

@test "model_pricing: uppercase model name matched case-insensitively" {
  run model_pricing "CLAUDE-SONNET-4-6-20250514"
  [ "$status" -eq 0 ]
  [ "$output" = "3000000 15000000" ]
}

# ---------------------------------------------------------------------------
# model_display_name
# ---------------------------------------------------------------------------

@test "model_display_name: claude-sonnet-4-6 -> Sonnet 4.6" {
  run model_display_name "claude-sonnet-4-6-20250514"
  [ "$status" -eq 0 ]
  [ "$output" = "Sonnet 4.6" ]
}

@test "model_display_name: claude-opus-4-6 -> Opus 4.6" {
  run model_display_name "claude-opus-4-6-20250514"
  [ "$status" -eq 0 ]
  [ "$output" = "Opus 4.6" ]
}

@test "model_display_name: claude-haiku-4-5 -> Haiku 4.5" {
  run model_display_name "claude-haiku-4-5-20251001"
  [ "$status" -eq 0 ]
  [ "$output" = "Haiku 4.5" ]
}

@test "model_display_name: gpt-4o-mini -> GPT-4o mini" {
  run model_display_name "gpt-4o-mini"
  [ "$status" -eq 0 ]
  [ "$output" = "GPT-4o mini" ]
}

@test "model_display_name: gpt-4o -> GPT-4o" {
  run model_display_name "gpt-4o"
  [ "$status" -eq 0 ]
  [ "$output" = "GPT-4o" ]
}

@test "model_display_name: gemini-2.5-flash -> Gemini 2.5 Flash" {
  run model_display_name "gemini-2.5-flash"
  [ "$status" -eq 0 ]
  [ "$output" = "Gemini 2.5 Flash" ]
}

@test "model_display_name: unknown model passes through unchanged" {
  run model_display_name "custom-model-v1"
  [ "$status" -eq 0 ]
  [ "$output" = "custom-model-v1" ]
}

# ---------------------------------------------------------------------------
# format_cost
# ---------------------------------------------------------------------------

@test "format_cost: zero produces dollar zero" {
  run format_cost 0
  [ "$status" -eq 0 ]
  [ "$output" = '$0.0000' ]
}

@test "format_cost: 10000 produces one dollar" {
  run format_cost 10000
  [ "$status" -eq 0 ]
  [ "$output" = '$1.0000' ]
}

@test "format_cost: 1234 produces fractional dollar" {
  run format_cost 1234
  [ "$status" -eq 0 ]
  [ "$output" = '$0.1234' ]
}

@test "format_cost: 50000 produces five dollars" {
  run format_cost 50000
  [ "$status" -eq 0 ]
  [ "$output" = '$5.0000' ]
}
