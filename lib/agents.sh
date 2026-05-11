#!/usr/bin/env bash
# lib/agents.sh — LLM agent dispatch and parallel execution for review.sh.
#
# Sourced by review.sh (via main() after SCRIPT_DIR is set). Exports:
#   call_agent              — synchronous single-agent invocation
#   call_agent_bg           — background variant for parallel tiers
#   wait_tier_pids          — wait for a tier of background agent PIDs
#   collect_parallel_results — merge sidecar state from parallel agents
#   cache_priming_effective — resolve whether cache priming should run
#   effective_prompt        — compose shared trailers onto a base prompt
#
# Contract: caller must set SCRIPT_DIR, TMPFILES (array), FAILED_AGENTS (array),
# TOKEN_LOG (array), EFFECTIVE_PROMPT_PREFIX, and provide mktemp_tracked()
# before calling.
#
# shellcheck disable=SC2034  # TOKEN_LOG, FAILED_AGENTS are set here, read by the caller

# Intercepts TOKENS: lines from llm-call.sh stderr for usage tracking;
# forwards all other stderr to the workflow log.
call_agent() {
  local name="$1" model="$2" prompt="$3" msg="$4" output="$5" max_tokens="${6:-16384}"
  echo "$name" > "${output}.name"
  echo "Calling ${name} (${model##*.})..." >&2

  local agent_stderr
  agent_stderr=$(mktemp_tracked /tmp/ai-review-stderr-XXXXXXXX.txt)

  local exit_code=0
  "${SCRIPT_DIR}/llm-call.sh" "$model" "$prompt" "$msg" "$max_tokens" \
    > "$output" 2> "$agent_stderr" || exit_code=$?
  if [[ "$exit_code" -ne 0 ]]; then
    local last_err failure_type
    last_err=$(grep -m1 'ERROR:' "$agent_stderr" 2>/dev/null || tail -1 "$agent_stderr" 2>/dev/null)
    case "$exit_code" in
      2) failure_type="transient API error, retries exhausted" ;;
      3) failure_type="response blocked by provider content filter" ;;
      *) failure_type="configuration or request error" ;;
    esac
    echo "WARNING: ${name} failed (${failure_type}): ${last_err:-no stderr output}. Continuing without its output." >&2
    cat "$agent_stderr" >&2
    FAILED_AGENTS+=("$name")
    echo "" > "$output"
    return
  fi

  # Parse token usage and truncation lines; forward remaining stderr to workflow log
  local token_line="" was_truncated=false
  while IFS= read -r line; do
    if [[ "$line" == TOKENS:* ]]; then
      token_line="$line"
    elif [[ "$line" == "TRUNCATED:true" ]]; then
      was_truncated=true
    else
      echo "$line" >&2
    fi
  done < "$agent_stderr"
  # Write a sidecar file so extract_findings() knows to attempt JSON repair.
  # Using a separate file avoids any risk of the LLM emitting the sentinel in its prose.
  if [[ "$was_truncated" == "true" ]]; then
    touch "${output}.truncated"
  fi

  if [[ -n "$token_line" ]]; then
    local input_tokens output_tokens cache_creation cache_read model_id
    # Anchor each key on line-start OR preceding whitespace so a model_id
    # that happened to contain "cache_creation=5" couldn't shadow the real
    # cache_creation field. In practice model_id values come from a known
    # list (see config/model-pricing.json) so this is defense-in-depth.
    input_tokens=$(echo "$token_line" | grep -oE '(^| )input=[0-9]+' | sed 's/.*input=//' || echo "0")
    output_tokens=$(echo "$token_line" | grep -oE '(^| )output=[0-9]+' | sed 's/.*output=//' || echo "0")
    # cache_creation / cache_read are optional — defaults to 0 for providers
    # (OpenAI, Google) and for pre-caching llm-call.sh versions.
    cache_creation=$(echo "$token_line" | grep -oE '(^| )cache_creation=[0-9]+' | sed 's/.*cache_creation=//' || echo "")
    cache_read=$(echo "$token_line" | grep -oE '(^| )cache_read=[0-9]+' | sed 's/.*cache_read=//' || echo "")
    cache_creation="${cache_creation:-0}"
    cache_read="${cache_read:-0}"
    model_id=$(echo "$token_line" | grep -oE '(^| )model=[^ ]+' | sed 's/.*model=//' || echo "unknown")
    if [[ "$cache_creation" -gt 0 || "$cache_read" -gt 0 ]]; then
      echo "  tokens: input=${input_tokens} output=${output_tokens} cache_creation=${cache_creation} cache_read=${cache_read} model=${model_id}" >&2
    else
      echo "  tokens: input=${input_tokens} output=${output_tokens} model=${model_id}" >&2
    fi
    TOKEN_LOG+=("${name}: input=${input_tokens} output=${output_tokens} cache_creation=${cache_creation} cache_read=${cache_read} model=${model_id}")
  fi
}


# --- Background variant of call_agent for parallel execution ---
# Writes results to sidecar files instead of mutating parent arrays:
#   ${output}.tokens  — token log entry (one line, empty on failure)
#   ${output}.failed  — exists (empty) iff the agent failed
# The .truncated sidecar is written by the same touch as in call_agent.
# Callers must invoke collect_parallel_results() after wait to merge state.
call_agent_bg() {
  local name="$1" model="$2" prompt="$3" msg="$4" output="$5" max_tokens="${6:-16384}"
  echo "Calling ${name} (${model##*.})..." >&2
  # Write agent name so collect_parallel_results can recover it regardless of success/failure
  echo "$name" > "${output}.name"

  local agent_stderr
  agent_stderr=$(mktemp /tmp/ai-review-stderr-XXXXXXXX.txt)

  local exit_code=0
  "${SCRIPT_DIR}/llm-call.sh" "$model" "$prompt" "$msg" "$max_tokens" \
    > "$output" 2> "$agent_stderr" || exit_code=$?

  if [[ "$exit_code" -ne 0 ]]; then
    local last_err failure_type
    last_err=$(grep -m1 'ERROR:' "$agent_stderr" 2>/dev/null || tail -1 "$agent_stderr" 2>/dev/null)
    case "$exit_code" in
      2) failure_type="transient API error, retries exhausted" ;;
      3) failure_type="response blocked by provider content filter" ;;
      *) failure_type="configuration or request error" ;;
    esac
    echo "WARNING: ${name} failed (${failure_type}): ${last_err:-no stderr output}. Continuing without its output." >&2
    cat "$agent_stderr" >&2
    touch "${output}.failed"
    echo "" > "$output"
    rm -f "$agent_stderr"
    return
  fi

  # Parse token usage and truncation lines; forward remaining stderr to workflow log
  local token_line="" was_truncated=false
  while IFS= read -r line; do
    if [[ "$line" == TOKENS:* ]]; then
      token_line="$line"
    elif [[ "$line" == "TRUNCATED:true" ]]; then
      was_truncated=true
    else
      echo "$line" >&2
    fi
  done < "$agent_stderr"
  rm -f "$agent_stderr"

  if [[ "$was_truncated" == "true" ]]; then
    touch "${output}.truncated"
  fi

  if [[ -n "$token_line" ]]; then
    local input_tokens output_tokens cache_creation cache_read model_id
    input_tokens=$(echo "$token_line" | grep -oE '(^| )input=[0-9]+' | sed 's/.*input=//' || echo "0")
    output_tokens=$(echo "$token_line" | grep -oE '(^| )output=[0-9]+' | sed 's/.*output=//' || echo "0")
    # cache_creation / cache_read are optional — defaults to 0 for providers
    # (OpenAI, Google) and for pre-caching llm-call.sh versions. Parallel
    # agents must forward these fields through the .tokens sidecar so the
    # parent's emit_token_table() can render cache activity correctly.
    cache_creation=$(echo "$token_line" | grep -oE '(^| )cache_creation=[0-9]+' | sed 's/.*cache_creation=//' || echo "")
    cache_read=$(echo "$token_line" | grep -oE '(^| )cache_read=[0-9]+' | sed 's/.*cache_read=//' || echo "")
    cache_creation="${cache_creation:-0}"
    cache_read="${cache_read:-0}"
    model_id=$(echo "$token_line" | grep -oE '(^| )model=[^ ]+' | sed 's/.*model=//' || echo "unknown")
    if [[ "$cache_creation" -gt 0 || "$cache_read" -gt 0 ]]; then
      echo "  tokens: input=${input_tokens} output=${output_tokens} cache_creation=${cache_creation} cache_read=${cache_read} model=${model_id}" >&2
    else
      echo "  tokens: input=${input_tokens} output=${output_tokens} model=${model_id}" >&2
    fi
    echo "${name}: input=${input_tokens} output=${output_tokens} cache_creation=${cache_creation} cache_read=${cache_read} model=${model_id}" > "${output}.tokens"
  fi
}


# Wait for a tier of background agents identified by parallel PID and output-file arrays.
# If a subshell exits non-zero but never wrote a .failed sidecar (killed by signal before
# reaching that line), synthesize the sidecar so collect_parallel_results counts it as failed.
# Args: interleaved "pid output_file" pairs: wait_tier_pids p1 f1 p2 f2 ...
wait_tier_pids() {
  local pid f
  while [[ "$#" -ge 2 ]]; do
    pid="$1" f="$2"; shift 2
    wait "$pid" || { [[ ! -f "${f}.failed" ]] && touch "${f}.failed"; }
  done
}


# Collect results from a completed tier of parallel agents into parent arrays.
# Args: roster-ordered list of output file paths (the same $output passed to call_agent_bg).
# Reads ${f}.name, ${f}.failed, ${f}.tokens sidecars; appends to FAILED_AGENTS / TOKEN_LOG.
# Registers sidecars with TMPFILES for cleanup on EXIT.
collect_parallel_results() {
  local output_files=("$@")
  for f in "${output_files[@]}"; do
    # Register sidecars for cleanup
    for ext in name tokens failed truncated; do
      [[ -f "${f}.${ext}" ]] && TMPFILES+=("${f}.${ext}")
    done

    local agent_name
    if [[ -f "${f}.name" ]]; then
      agent_name=$(cat "${f}.name")
    else
      echo "WARNING: Missing .name sidecar for output file ${f}; agent identity unknown." >&2
      agent_name="unknown"
    fi

    if [[ -f "${f}.failed" ]]; then
      FAILED_AGENTS+=("$agent_name")
    fi
    if [[ -f "${f}.tokens" ]]; then
      TOKEN_LOG+=("$(cat "${f}.tokens")")
    fi
  done
}


# --- Resolve whether cache-priming should run for this invocation ---
# Echoes "true" or "false". Priming is effective when BOTH:
#   * AI_CACHE_PRIMING is explicitly "true" (default: false — opt-in tuning knob)
#   * Prompt caching itself is on — either AI_PROVIDER is anthropic or
#     bedrock-proxy (automatic) OR LLM_PROMPT_CACHING is explicitly true.
#
# Why default-off: live-benchmarked against a real PR on Bedrock, priming
# delivered no measurable cost win vs the unprimed baseline. Parallel
# Tier 2 agents already got the same cache hits opportunistically via
# natural start-up staggering (different agent prompts, Opus vs Sonnet TTFT).
# Priming traded +30s wall-clock latency for guaranteed (rather than
# opportunistic) hits — roughly cost-neutral but slower. Left in as an
# opt-in tuning knob for environments where cache-hit timing differs
# (strict rate-limiting, proxy serialization, etc.) where opportunistic
# hits don't happen.
cache_priming_effective() {
  # Default is "false" — opt-in only.
  case "${AI_CACHE_PRIMING:-false}" in
    true|TRUE|True|1) ;;
    *) echo "false"; return ;;
  esac
  # Priming is only meaningful when cache_control markers are actually
  # emitted — i.e., only on Anthropic-shaped request bodies. OpenAI's
  # automatic prefix caching doesn't need markers (cache key is implicit)
  # and Google Gemini uses a different API that this project doesn't mark.
  # Forcing priming on those providers would pay the +30s serialization
  # penalty for zero cache benefit. Gate the caching check on provider
  # BEFORE honoring LLM_PROMPT_CACHING, not after (the llm-call.sh
  # counterpart is looser because that function is only asking "should
  # I emit markers if building an Anthropic body" — the priming question
  # is different: "will this serialization help cache hit rate").
  case "${AI_PROVIDER:-}" in
    anthropic|bedrock-proxy) ;;
    *) echo "false"; return ;;
  esac
  # Now on an Anthropic-shaped provider. Honor explicit LLM_PROMPT_CACHING=false
  # (operator opted out of caching entirely); otherwise auto-on.
  case "${LLM_PROMPT_CACHING:-auto}" in
    false|FALSE|False|0) echo "false"; return ;;
    *) echo "true" ;;
  esac
}


# --- Build effective prompt path (composes shared trailers with the base) ---
#
# Composes three shared blocks onto the base agent prompt depending on agent
# eligibility:
#   * prompts/_knowledge-cutoff.md  — HARD CONSTRAINT against version-existence
#                                     hallucinations. Applied to all 7
#                                     finding-producing agents.
#   * prompts/_trailer-findings.md  — json-findings schema instruction.
#                                     Applied to all 7 finding-producing agents.
#   * prompts/suggestion-addendum.md — "Apply suggestion" formatting instructions.
#                                      Gated by AI_ENABLE_SUGGESTIONS; applied only
#                                      to agents that emit inline line edits
#                                      (code-reviewer, edge-case-hunter,
#                                      security-reviewer, silent-failure-hunter,
#                                      blind-hunter).
#
# pr-summarizer is NOT eligible — its prompt has a different output contract
# (no findings, no version checks) and is passed through verbatim.
#
# Composition order: base prompt + knowledge-cutoff + findings-trailer +
# (optional) suggestion-addendum. This puts agent-specific voice first and
# shared trailers last, which keeps the trailer bytes identical across the
# eligible agents — making those bytes cache-friendly when a future change
# positions the cache_control marker inside the user message.
#
# Note: the returned path is cleaned up in the parent trap via a glob match
# on EFFECTIVE_PROMPT_PREFIX since TMPFILES+= would not propagate back through
# the $(...) call site. On any missing-file or cat failure, the function falls
# back to the base prompt with a WARNING rather than silently passing a
# truncated prompt to the LLM.
#
# Fallback for a missing base prompt: the function echoes the missing path
# back verbatim (not a sentinel or empty string). This is intentional —
# call_agent → llm-call.sh already handles "file does not exist" via its own
# exit-1 path. Operators see two signals: a WARNING from this function plus
# the downstream "file not found" error from the agent invocation.

effective_prompt() {
  local agent_name="$1" base_prompt="$2"

  # Agents that receive the shared findings trailer + knowledge-cutoff rule.
  # pr-summarizer is deliberately excluded; it has a different output contract.
  # Agents that receive the "Apply suggestion" formatting instructions.
  # Architecture-reviewer and adversarial-general are holistic/design-level and
  # do not emit line-edit suggestions.
  #
  # Inlined here (rather than set at script top) so the function is
  # self-contained: the bats harness loads a single function via awk
  # extraction and does not see top-level variable assignments.
  local agents_with_findings_trailer='code-reviewer|silent-failure-hunter|security-reviewer|edge-case-hunter|blind-hunter|architecture-reviewer|adversarial-general'
  local agents_with_suggestion_addendum='code-reviewer|edge-case-hunter|security-reviewer|silent-failure-hunter|blind-hunter'

  if [[ ! -f "$base_prompt" ]]; then
    echo "WARNING: Base prompt missing at ${base_prompt}; cannot build effective prompt for ${agent_name}." >&2
    echo "$base_prompt"
    return
  fi

  # Decide which trailers apply.
  local want_trailers=0 want_suggestion=0
  if [[ "$agent_name" =~ ^(${agents_with_findings_trailer})$ ]]; then
    want_trailers=1
  fi
  local _enable="${AI_ENABLE_SUGGESTIONS:-true}"
  _enable="${_enable,,}"
  if [[ "$_enable" == "true" ]] \
     && [[ "$agent_name" =~ ^(${agents_with_suggestion_addendum})$ ]]; then
    want_suggestion=1
  fi

  # No trailers to compose → pass through unchanged (the pr-summarizer path
  # and the AI_ENABLE_SUGGESTIONS=false path for non-eligible-for-trailers
  # agents, though the latter is currently empty).
  if [[ "$want_trailers" -eq 0 && "$want_suggestion" -eq 0 ]]; then
    echo "$base_prompt"
    return
  fi

  # Build the list of files to concatenate. Verify each exists before use;
  # a missing shared trailer should fall back to the base prompt, not ship
  # a truncated composition.
  local -a parts=("$base_prompt")
  local kc="${SCRIPT_DIR}/prompts/_knowledge-cutoff.md"
  local ft="${SCRIPT_DIR}/prompts/_trailer-findings.md"
  local sa="${SCRIPT_DIR}/prompts/suggestion-addendum.md"
  if [[ "$want_trailers" -eq 1 ]]; then
    if [[ ! -f "$kc" ]]; then
      echo "WARNING: Shared knowledge-cutoff trailer missing at ${kc}; using base prompt for ${agent_name}." >&2
      echo "$base_prompt"; return
    fi
    if [[ ! -f "$ft" ]]; then
      echo "WARNING: Shared findings trailer missing at ${ft}; using base prompt for ${agent_name}." >&2
      echo "$base_prompt"; return
    fi
    parts+=("$kc" "$ft")
  fi
  if [[ "$want_suggestion" -eq 1 ]]; then
    if [[ ! -f "$sa" ]]; then
      echo "WARNING: Suggestion addendum missing at ${sa}; composing without it for ${agent_name}." >&2
    else
      parts+=("$sa")
    fi
  fi

  local combined
  combined=$(mktemp "${EFFECTIVE_PROMPT_PREFIX}-XXXXXXXX.md" 2>/dev/null) || {
    echo "WARNING: Failed to create temp file for ${agent_name} effective prompt; using base prompt." >&2
    echo "$base_prompt"; return
  }
  if ! cat "${parts[@]}" > "$combined" 2>/dev/null; then
    echo "WARNING: Failed to assemble effective prompt for ${agent_name}; using base prompt." >&2
    rm -f "$combined"
    echo "$base_prompt"; return
  fi
  echo "$combined"
}
