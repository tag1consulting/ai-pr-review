#!/usr/bin/env bash
# lib/finding-ids.sh — Stable per-PR body-finding ID assignment.
#
# Body-level findings are assigned monotonically-increasing, fingerprint-stable
# numeric IDs (**[F1]**, **[F2]**, …). IDs are scoped to the pull request:
# the same finding keeps its ID across review cycles, and new findings get
# the next unused ID. There is no persistent ID store — IDs are reconstructed
# at render time by scanning prior bot review bodies.
#
# Functions exported:
#   finding_ids_max_from_bodies  BODIES_FILE → prints max ID seen (0 if none)
#   finding_ids_assign           SOURCE FILE LINE TEXT BODIES_FILE → prints ID
#
# The BODIES_FILE argument is a file containing all prior bot review bodies,
# one per line (newlines within a body are escaped as \n — use jq -r to write).
# Callers are responsible for fetching the bodies before calling these functions.
#
# Fingerprint contract (must match ai_pr_review/vcs/_finding_ids.py):
#   fingerprint = "${source}|${file}|${line}|${sha256_12_hex}"
# where sha256_12_hex is the first 12 hex characters of the SHA-256 of the
# finding text (after stripping leading/trailing whitespace).

set -euo pipefail

# ---------------------------------------------------------------------------
# _text_hash: first 12 hex chars of SHA-256 of stdin text.
# ---------------------------------------------------------------------------
_text_hash() {
  local text="$1"
  printf '%s' "$text" | sha256sum | cut -c1-12
}

# ---------------------------------------------------------------------------
# _make_fingerprint SOURCE FILE LINE TEXT → print fingerprint string
# ---------------------------------------------------------------------------
_make_fingerprint() {
  local source="$1" file="$2" line="$3" text="$4"
  local text_hash
  text_hash=$(_text_hash "$text")
  printf '%s|%s|%s|%s' "$source" "$file" "$line" "$text_hash"
}

# ---------------------------------------------------------------------------
# finding_ids_max_from_bodies BODIES_FILE → print max seen ID (0 if none)
#
# Scans all body text in BODIES_FILE (one escaped body per line) and returns
# the highest **[F<n>]** ID seen across any of the bodies.
# ---------------------------------------------------------------------------
finding_ids_max_from_bodies() {
  local bodies_file="${1:-}"
  if [[ -z "$bodies_file" || ! -f "$bodies_file" ]]; then
    echo 0
    return
  fi

  local max_id=0
  local id
  # Each line in bodies_file is a single review body with \n for newlines.
  while IFS= read -r encoded_body; do
    # Replace literal \n escape sequences with actual newlines for parsing.
    local body
    body=$(printf '%b' "$encoded_body")
    # Extract all **[F<n>]** tokens and track the maximum.
    while IFS= read -r id; do
      if [[ -n "$id" && "$id" =~ ^[0-9]+$ && "$id" -gt "$max_id" ]]; then
        max_id="$id"
      fi
    done < <(printf '%s\n' "$body" | grep -oP '\*\*\[F\K[0-9]+(?=\]\*\*)' || true)
  done < "$bodies_file"

  echo "$max_id"
}

# ---------------------------------------------------------------------------
# finding_ids_build_map BODIES_FILE MAP_FILE
#
# Parse all prior review bodies and write a "fingerprint TAB id" map to
# MAP_FILE. The caller uses this map to look up existing IDs before assigning
# new ones.
# ---------------------------------------------------------------------------
finding_ids_build_map() {
  local bodies_file="${1:-}" map_file="$2"
  : > "$map_file"  # truncate/create
  if [[ -z "$bodies_file" || ! -f "$bodies_file" ]]; then
    return
  fi

  while IFS= read -r encoded_body; do
    local body
    body=$(printf '%b' "$encoded_body")

    local in_section=false
    while IFS= read -r line; do
      if [[ "$line" == *"### Findings not attached to specific lines"* ]]; then
        in_section=true
        continue
      fi
      # Any level-3 heading ends the section.
      if $in_section && [[ "$line" =~ ^"###" ]]; then
        in_section=false
        continue
      fi
      $in_section || continue
      # Must start with a bullet.
      [[ "$line" =~ ^"- " ]] || continue

      # Extract ID.
      local finding_id
      finding_id=$(printf '%s' "$line" | grep -oP '\*\*\[F\K[0-9]+(?=\]\*\*)' | head -1 || true)
      [[ -n "$finding_id" ]] || continue

      # Extract source: first [bracketed] group after the ID token.
      local source
      source=$(printf '%s' "$line" | grep -oP '\*\*\[F[0-9]+\]\*\* \[\K[^\]]+' | head -1 || true)
      source="${source%%,*}"  # take first source if comma-separated
      source="${source// /}"  # strip spaces

      # Extract file:line from *(at `...`) or — `...` annotation.
      local file_line file part_line
      file_line=$(printf '%s' "$line" | grep -oP '`\K[^`]+(?=`[^`]*\*)' | head -1 || true)
      if [[ -z "$file_line" ]]; then
        file_line=$(printf '%s' "$line" | grep -oP '— `\K[^`]+' | head -1 || true)
      fi
      if [[ "$file_line" == *":"* ]]; then
        file="${file_line%:*}"
        part_line="${file_line##*:}"
        # Validate that part_line is numeric; otherwise treat entire string as file.
        if ! [[ "$part_line" =~ ^[0-9]+$ ]]; then
          file="$file_line"
          part_line=""
        fi
      else
        file="$file_line"
        part_line=""
      fi

      # Extract finding text: content after the source tag and before the location.
      local text
      # Remove bullet prefix, icon, severity, ID token, and source tag.
      text=$(printf '%s' "$line" \
        | sed 's/^- //' \
        | sed 's/[🚨🔴🟡🔵✅❔] //' \
        | sed 's/\*\*\[[^]]*\]\*\* //g' \
        | sed 's/\[[^]]*\] //' \
        | sed 's/ — `[^`]*`.*$//' \
        | sed 's/ \*(at `[^`]*`.*$//') || true
      text=$(printf '%s' "$text" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')

      [[ -n "$text" ]] || continue

      local fp
      fp=$(_make_fingerprint "$source" "$file" "$part_line" "$text")
      # Write to map only if not already present (first/oldest review wins).
      if ! grep -qF "$fp	" "$map_file" 2>/dev/null; then
        printf '%s\t%s\n' "$fp" "$finding_id" >> "$map_file"
      fi
    done <<< "$body"
  done < "$bodies_file"
}

# ---------------------------------------------------------------------------
# finding_ids_lookup SOURCE FILE LINE TEXT MAP_FILE MAX_ID_VAR NEXT_ID_VAR
#   → print the assigned ID for this finding
#
# Looks up the fingerprint in MAP_FILE. If found, returns the existing ID.
# If not found, returns the value of NEXT_ID_VAR and increments it.
#
# Usage: call with nameref vars is bash-4+ only; we use a simpler file-based
# approach — the caller maintains a counter file (NEXT_ID_FILE) and passes it.
# ---------------------------------------------------------------------------
finding_ids_get() {
  local source="$1" file="$2" line="$3" text="$4" map_file="$5" next_id_file="$6"
  local fp
  fp=$(_make_fingerprint "$source" "$file" "$line" "$text")

  if [[ -f "$map_file" ]]; then
    local existing_id
    existing_id=$(grep -F "$fp	" "$map_file" 2>/dev/null | head -1 | cut -f2 || true)
    if [[ -n "$existing_id" ]]; then
      echo "$existing_id"
      return
    fi
  fi

  # New finding — assign next ID.
  local next_id
  next_id=$(cat "$next_id_file" 2>/dev/null || echo 1)
  echo "$next_id"
  echo $((next_id + 1)) > "$next_id_file"
}
