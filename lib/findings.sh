#!/usr/bin/env bash
# lib/findings.sh — findings extraction, merging, and suppression for review.sh.
#
# Sourced by review.sh (via main() after SCRIPT_DIR is set). Exports:
#   extract_findings    — parse json-findings blocks from agent output files
#   merge_findings      — validate and accumulate findings into FINDINGS_JSON_FILE
#   apply_suppressions  — filter findings against declarative suppression rules
#
# Contract: caller must set SCRIPT_DIR, FINDINGS_JSON_FILE, TMPFILES (array),
# and SUPPRESSED_COUNT before calling. apply_suppressions also reads
# GITHUB_WORKSPACE for local suppression rule discovery.
#
# shellcheck disable=SC2034  # SUPPRESSED_COUNT is set here, read by the caller

extract_findings() {
  local agent_file="$1"
  local agent_name="${2:-unknown}"
  local was_truncated=false
  [[ -f "${agent_file}.truncated" ]] && was_truncated=true

  if ! grep -q '```json-findings' "$agent_file" 2>/dev/null; then
    if [[ "$was_truncated" == "true" ]]; then
      echo "WARNING: $(basename "$agent_file") was truncated before json-findings block; findings lost." >&2
    else
      echo "WARNING: $(basename "$agent_file") has no json-findings block; skipping." >&2
    fi
    echo "[]"
    return
  fi

  local extracted
  # Single quotes are intentional — $ is sed's end-of-range/last-line anchor,
  # not a shell expansion. Same rationale below at the FINDINGS_CLEAN_FILE loop.
  # shellcheck disable=SC2016
  extracted=$(sed -n '/```json-findings/,/```/p' "$agent_file" | sed '1d;$d')

  # Stamp source on findings that don't already carry one, then validate.
  # Returns the stamped array on success; exits non-zero on invalid input.
  _ef_stamp_and_validate() {
    local raw="$1"
    echo "$raw" | jq -e --arg s "$agent_name" '
      if (type == "array") and
         (if length > 0 then all(.[]; has("severity") and has("finding") and has("confidence")) else true end)
      then map(. + {source: (.source // $s)})
      else error("invalid")
      end
    ' 2>/dev/null
  }

  local stamped
  if stamped=$(_ef_stamp_and_validate "$extracted"); then
    printf '%s' "$stamped"
    return
  fi

  # Validation failed — if the response was truncated, attempt to repair the JSON.
  # Strategy: iterate over closing-brace lines from last to first, close the array
  # at each candidate, and accept the first result that passes schema validation.
  # Iterating (rather than taking the last '}') handles nested objects whose inner
  # '}' lines would otherwise produce invalid JSON.
  if [[ "$was_truncated" == "true" ]]; then
    local candidate_line repaired
    while IFS=: read -r candidate_line _; do
      repaired=$(printf '%s' "$extracted" | head -n "$candidate_line" | sed '$ s/,$//')
      repaired=$(printf '%s\n]' "$repaired")
      if stamped=$(_ef_stamp_and_validate "$repaired"); then
        local count
        count=$(printf '%s' "$stamped" | jq 'length')
        echo "NOTE: $(basename "$agent_file") was truncated; salvaged ${count} finding(s) from partial JSON." >&2
        printf '%s' "$stamped"
        return
      fi
    done < <(printf '%s' "$extracted" | grep -n '^[[:space:]]*}' | tac)
  fi

  local preview
  preview=$(set +o pipefail; printf '%s' "$extracted" | head -c 200)
  echo "WARNING: $(basename "$agent_file") produced invalid or malformed json-findings; skipping. Preview: ${preview}" >&2
  echo "[]"
}


merge_findings() {
  local incoming="$1"
  # Validate and filter incoming findings element-by-element so a single malformed
  # object doesn't discard the rest of the batch. Required fields: severity, finding,
  # confidence. Missing file or line is accepted (body-only findings).
  local valid_incoming
  valid_incoming=$(echo "$incoming" | jq '[.[] | select(
    (.severity | type) == "string" and
    (.finding  | type) == "string" and
    (.confidence | type) == "number"
  )]' 2>/dev/null) || valid_incoming="[]"

  local incoming_count valid_count
  incoming_count=$(echo "$incoming" | jq 'length' 2>/dev/null || echo "0")
  valid_count=$(echo "$valid_incoming" | jq 'length' 2>/dev/null || echo "0")
  if [[ "$valid_count" != "$incoming_count" ]]; then
    echo "WARNING: merge_findings: dropped $(( incoming_count - valid_count )) malformed finding(s) (missing required fields)." >&2
  fi

  if [[ "$valid_count" == "0" ]]; then
    return 0
  fi

  if jq -s '.[0] + .[1]' "$FINDINGS_JSON_FILE" <(echo "$valid_incoming") > "${FINDINGS_JSON_FILE}.tmp" 2>/dev/null; then
    mv "${FINDINGS_JSON_FILE}.tmp" "$FINDINGS_JSON_FILE"
  else
    echo "WARNING: Failed to merge findings JSON; skipping batch of ${valid_count} finding(s)." >&2
    rm -f "${FINDINGS_JSON_FILE}.tmp"
  fi
}


# Apply declarative suppressions from suppressions.json.
# Runs after all findings are merged, before confidence filter and dedup.
# Suppressed findings are removed from FINDINGS_JSON_FILE and logged to stderr.
# Local suppressions in ${GITHUB_WORKSPACE}/.github/ai-pr-review/suppressions.json
# are merged with the global rules (local rules take precedence in log output).
apply_suppressions() {
  local suppressions_file="${SCRIPT_DIR}/config/suppressions.json"
  local local_suppressions_file="${GITHUB_WORKSPACE:-}/.github/ai-pr-review/suppressions.json"
  local combined_rules_file
  combined_rules_file=$(mktemp /tmp/ai-review-suppressions-XXXXXXXX.json)
  TMPFILES+=("$combined_rules_file")

  if [[ ! -f "$suppressions_file" ]]; then
    return 0
  fi

  if ! jq -e 'type == "array"' "$suppressions_file" > /dev/null 2>&1; then
    echo "WARNING: suppressions.json is not a valid JSON array; skipping suppression filter." >&2
    return 0
  fi

  if [[ -n "${GITHUB_WORKSPACE:-}" && -f "$local_suppressions_file" ]]; then
    if ! jq -e 'type == "array"' "$local_suppressions_file" > /dev/null 2>&1; then
      echo "WARNING: local suppressions.json is not a valid JSON array; ignoring local suppressions." >&2
      cp "$suppressions_file" "$combined_rules_file"
    else
      jq -s 'add' "$suppressions_file" "$local_suppressions_file" > "$combined_rules_file"
      echo "Loaded local suppressions from .github/ai-pr-review/suppressions.json" >&2
    fi
  else
    cp "$suppressions_file" "$combined_rules_file"
  fi

  local result
  result=$(jq --slurpfile rules "$combined_rules_file" '
    ($rules[0]) as $active_rules |

    def rule_matches(rule):
      (if rule.match.code then
        (.finding | startswith(rule.match.code))
      else true end)
      and
      (if rule.match.file then
        (.file // "" | contains(rule.match.file))
      else true end)
      and
      (if rule.match.line then
        (.line == rule.match.line)
      else true end)
      and
      (if rule.match.pattern then
        (.finding | test(rule.match.pattern; "i"))
      else true end);

    def find_rule_id:
      . as $f |
      ([$active_rules[] | select(. as $r | $f | rule_matches($r))][0].id // "?");

    def find_verify_field:
      . as $f |
      ([$active_rules[] | select(. as $r | $f | rule_matches($r))][0].verify // "") ;

    def is_suppressed:
      . as $finding |
      any($active_rules[]; . as $rule | $finding | rule_matches($rule));

    {
      kept: [.[] | select(is_suppressed | not)],
      suppressed: [.[] | select(is_suppressed) | . + {suppression_id: find_rule_id, verify: find_verify_field}]
    }
  ' "$FINDINGS_JSON_FILE") || {
    echo "WARNING: apply_suppressions jq failed; skipping suppression filter." >&2
    return 0
  }

  # For findings suppressed by rules with a verify field, confirm the version exists
  # via the appropriate registry API before accepting the suppression. If the version
  # genuinely does not exist (404), keep the finding — the AI may be correct after all.
  local verified_suppressed
  verified_suppressed=$(echo "$result" | jq '.suppressed')

  # Restored findings (failed verification) are written to a temp file as newline-
  # delimited JSON objects, then assembled into an array once after the loop.
  local restored_tmp
  restored_tmp=$(mktemp /tmp/ai-review-restored-XXXXXXXX.jsonl)
  TMPFILES+=("$restored_tmp")

  # Namespaced helper — removes a finding from verified_suppressed.
  # (bash functions are global; the prefix prevents collisions.)
  _suppression_restore() {
    local fj="$1"
    echo "$fj" | jq 'del(.suppression_id, .verify)' >> "$restored_tmp"
    verified_suppressed=$(echo "$verified_suppressed" | jq --argjson f "$fj" \
      '[.[] | select(.finding != $f.finding or .file != $f.file or .line != $f.line)]')
  }

  while IFS= read -r finding_json; do
    local verify_type finding_text
    verify_type=$(echo "$finding_json" | jq -r '.verify // ""')
    [[ -z "$verify_type" ]] && continue
    finding_text=$(echo "$finding_json" | jq -r '.finding')

    case "$verify_type" in

      github-release)
        # Extract owner/repo@vN.N.N — captures both major tags (v6) and patch tags (v4.1.0)
        local action_ref owner_repo tag
        action_ref=$(echo "$finding_text" | grep -oE '[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+@v[0-9]+(\.[0-9]+)*' | head -1)
        if [[ -z "$action_ref" ]]; then continue; fi
        owner_repo="${action_ref%@*}"; tag="${action_ref#*@}"
        echo "Verifying GitHub release: ${owner_repo}@${tag}" >&2
        if gh api "repos/${owner_repo}/git/ref/tags/${tag}" > /dev/null 2>&1 \
           || gh api "repos/${owner_repo}/releases/tags/${tag}" > /dev/null 2>&1; then
          echo "  Confirmed tag/release exists — suppressing finding." >&2
        else
          echo "  Version not found via GitHub API — keeping finding (may be genuine)." >&2
          _suppression_restore "$finding_json"
        fi
        ;;

      npm)
        # Extract @scope/pkg@version or pkg@version, or "pkg": "version" in JSON.
        # Scoped packages (@scope/pkg) are URL-encoded as %40scope%2Fpkg for the registry.
        local npm_pkg npm_ver npm_pkg_enc
        local npm_ref
        # Try scoped package first: @scope/pkg@version
        npm_ref=$(echo "$finding_text" | grep -oE '@[a-zA-Z0-9._-]+/[a-zA-Z0-9._-]+@[0-9][0-9a-zA-Z._-]*' | head -1)
        if [[ -n "$npm_ref" ]]; then
          npm_pkg="${npm_ref%@[0-9]*}"; npm_ver="${npm_ref##*@}"
        else
          # Unscoped: pkg@version
          npm_ref=$(echo "$finding_text" | grep -oE '[a-zA-Z0-9._-]+@[0-9][0-9a-zA-Z._-]*' | head -1)
          if [[ -n "$npm_ref" ]]; then
            npm_pkg="${npm_ref%@*}"; npm_ver="${npm_ref#*@}"
          else
            # "pkg": "version" in package.json snippet
            npm_ref=$(echo "$finding_text" | grep -oP '"([a-zA-Z0-9._-]+)":\s*"([0-9][^"]+)"' | head -1)
            npm_pkg=$(echo "$npm_ref" | grep -oP '(?<=")[a-zA-Z0-9._-]+(?=":\s*")')
            npm_ver=$(echo "$npm_ref" | grep -oP '(?<=":\s*")[0-9][^"]+')
          fi
        fi
        if [[ -z "$npm_pkg" || -z "$npm_ver" ]]; then continue; fi
        # URL-encode scoped package names: @scope/pkg -> %40scope%2Fpkg
        npm_pkg_enc="${npm_pkg//@/%40}"
        npm_pkg_enc="${npm_pkg_enc//\//%2F}"
        echo "Verifying npm: ${npm_pkg}@${npm_ver}" >&2
        if curl -sf "https://registry.npmjs.org/${npm_pkg_enc}/${npm_ver}" > /dev/null 2>&1; then
          echo "  Confirmed released — suppressing finding." >&2
        else
          echo "  Version not found on npm — keeping finding (may be genuine)." >&2
          _suppression_restore "$finding_json"
        fi
        ;;

      pypi)
        # Extract pkg==version (e.g. "requests==3.0.0") or "pkg version X.Y.Z"
        local pypi_ref pypi_pkg pypi_ver
        pypi_ref=$(echo "$finding_text" | grep -oE '[a-zA-Z0-9._-]+==[0-9][0-9a-zA-Z._-]*' | head -1)
        if [[ -n "$pypi_ref" ]]; then
          pypi_pkg="${pypi_ref%%==*}"; pypi_ver="${pypi_ref##*==}"
        else
          pypi_ref=$(echo "$finding_text" | grep -oE '[a-zA-Z0-9._-]+ version [0-9][0-9a-zA-Z._-]*' | head -1)
          pypi_pkg=$(echo "$pypi_ref" | awk '{print $1}')
          pypi_ver=$(echo "$pypi_ref" | awk '{print $3}')
        fi
        if [[ -z "$pypi_pkg" || -z "$pypi_ver" ]]; then continue; fi
        echo "Verifying PyPI: ${pypi_pkg}==${pypi_ver}" >&2
        if curl -sf "https://pypi.org/pypi/${pypi_pkg}/${pypi_ver}/json" > /dev/null 2>&1; then
          echo "  Confirmed released — suppressing finding." >&2
        else
          echo "  Version not found on PyPI — keeping finding (may be genuine)." >&2
          _suppression_restore "$finding_json"
        fi
        ;;

      go-module)
        # Extract module@vX.Y.Z (e.g. "github.com/gin-gonic/gin@v1.10.0").
        # Go module proxy requires: slashes encoded as %2F, uppercase letters as
        # !lowercase (e.g. BurntSushi -> !burnt!sushi).
        local go_ref go_mod go_ver go_mod_enc go_char
        go_ref=$(echo "$finding_text" | grep -oE '[a-zA-Z0-9._-]+\.[a-zA-Z]{2,}/[a-zA-Z0-9._/@-]+@v[0-9][0-9a-zA-Z._-]*' | head -1)
        if [[ -z "$go_ref" ]]; then continue; fi
        go_mod="${go_ref%@*}"; go_ver="${go_ref#*@}"
        # Apply Go module proxy encoding: uppercase A-Z -> !lowercase, / -> %2F
        go_mod_enc=""
        while IFS= read -r -n1 go_char; do
          if [[ "$go_char" =~ [A-Z] ]]; then
            go_mod_enc+="!${go_char,,}"
          elif [[ "$go_char" == "/" ]]; then
            go_mod_enc+="%2F"
          else
            go_mod_enc+="$go_char"
          fi
        done <<< "$go_mod"
        # Remove trailing newline added by herestring
        go_mod_enc="${go_mod_enc%$'\n'}"
        echo "Verifying Go module: ${go_mod}@${go_ver}" >&2
        if curl -sf "https://proxy.golang.org/${go_mod_enc}/@v/${go_ver}.info" > /dev/null 2>&1; then
          echo "  Confirmed released — suppressing finding." >&2
        else
          echo "  Version not found on Go module proxy — keeping finding (may be genuine)." >&2
          _suppression_restore "$finding_json"
        fi
        ;;

      cargo)
        # Extract pkg = "version" or pkg@version (e.g. serde = "1.1.0" or tokio@2.0.0)
        local cargo_pkg cargo_ver cargo_ref
        cargo_ref=$(echo "$finding_text" | grep -oE '[a-zA-Z0-9_-]+ = "[0-9][^"]*"' | head -1)
        if [[ -n "$cargo_ref" ]]; then
          cargo_pkg=$(echo "$cargo_ref" | grep -oE '^[a-zA-Z0-9_-]+')
          cargo_ver=$(echo "$cargo_ref" | grep -oE '"[0-9][^"]*"' | tr -d '"')
        else
          cargo_ref=$(echo "$finding_text" | grep -oE '[a-zA-Z0-9_-]+@[0-9][0-9a-zA-Z._-]*' | head -1)
          cargo_pkg="${cargo_ref%@*}"; cargo_ver="${cargo_ref#*@}"
        fi
        if [[ -z "$cargo_pkg" || -z "$cargo_ver" ]]; then continue; fi
        echo "Verifying Cargo: ${cargo_pkg}@${cargo_ver}" >&2
        if curl -sf "https://crates.io/api/v1/crates/${cargo_pkg}/${cargo_ver}" > /dev/null 2>&1; then
          echo "  Confirmed released — suppressing finding." >&2
        else
          echo "  Version not found on crates.io — keeping finding (may be genuine)." >&2
          _suppression_restore "$finding_json"
        fi
        ;;

      docker-hub)
        # Extract image:tag (e.g. "nginx:1.27.0" or "bitnami/postgresql:17.4.0").
        # Uses /v2/namespaces/ endpoint which supports anonymous access for public images.
        local docker_ref docker_img docker_tag docker_ns docker_name
        docker_ref=$(echo "$finding_text" | grep -oE '([a-zA-Z0-9._-]+/)?[a-zA-Z0-9._-]+:[0-9][0-9a-zA-Z._-]*' | head -1)
        if [[ -z "$docker_ref" ]]; then continue; fi
        docker_img="${docker_ref%:*}"; docker_tag="${docker_ref#*:}"
        if [[ "$docker_img" == */* ]]; then
          docker_ns="${docker_img%/*}"; docker_name="${docker_img#*/}"
        else
          docker_ns="library"; docker_name="$docker_img"
        fi
        echo "Verifying Docker Hub: ${docker_ns}/${docker_name}:${docker_tag}" >&2
        if curl -sf "https://hub.docker.com/v2/namespaces/${docker_ns}/repositories/${docker_name}/tags/${docker_tag}" > /dev/null 2>&1; then
          echo "  Confirmed tag exists — suppressing finding." >&2
        else
          echo "  Tag not found on Docker Hub — keeping finding (may be genuine)." >&2
          _suppression_restore "$finding_json"
        fi
        ;;

      ruby-org)
        # Extract Ruby MRI version X.Y.Z (e.g. "4.0.3", "3.4.1") from finding text.
        # Verified against ruby-lang.org's canonical release tarball index, which
        # returns 200 for released versions and 404 otherwise.
        # URL form: https://cache.ruby-lang.org/pub/ruby/{MAJOR.MINOR}/ruby-{MAJOR.MINOR.PATCH}.tar.gz
        local ruby_ver ruby_majmin
        ruby_ver=$(echo "$finding_text" | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)
        if [[ -z "$ruby_ver" ]]; then continue; fi
        ruby_majmin="${ruby_ver%.*}"
        echo "Verifying Ruby release: ${ruby_ver}" >&2
        if curl -sfI "https://cache.ruby-lang.org/pub/ruby/${ruby_majmin}/ruby-${ruby_ver}.tar.gz" > /dev/null 2>&1; then
          echo "  Confirmed released on ruby-lang.org — suppressing finding." >&2
        else
          echo "  Version not found on ruby-lang.org — keeping finding (may be genuine)." >&2
          _suppression_restore "$finding_json"
        fi
        ;;

    esac
  done < <(echo "$result" | jq -c '.suppressed[]' 2>/dev/null)

  # Build verified_kept from the temp file (single jq pass, O(n) not O(n²))
  local verified_kept
  if [[ -s "$restored_tmp" ]]; then
    verified_kept=$(jq -s '.' "$restored_tmp")
  else
    verified_kept="[]"
  fi

  # Merge verified_kept back with the originally kept findings; // [] guards against
  # jq -s 'add' returning null when both inputs are empty arrays.
  local final_kept
  final_kept=$(jq -s 'add // []' <(echo "$result" | jq '.kept') <(echo "$verified_kept"))

  if echo "$final_kept" > "${FINDINGS_JSON_FILE}.tmp"; then
    mv "${FINDINGS_JSON_FILE}.tmp" "$FINDINGS_JSON_FILE"
  else
    echo "WARNING: apply_suppressions failed to write filtered findings; keeping original." >&2
    rm -f "${FINDINGS_JSON_FILE}.tmp"
  fi

  local suppressed_count
  suppressed_count=$(echo "$verified_suppressed" | jq 'length')
  SUPPRESSED_COUNT=$suppressed_count

  if [[ "$suppressed_count" -gt 0 ]]; then
    echo "Suppressed findings: ${suppressed_count}" >&2
    echo "$verified_suppressed" | jq -r '.[] | "  SUPPRESSED [\(.suppression_id)] \(.file // "?"):\(.line // "?") — \(.finding | .[0:80])"' >&2
  fi
}
