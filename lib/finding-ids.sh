#!/usr/bin/env bash
# lib/finding-ids.sh — Stable per-PR body-finding ID assignment.
#
# Uses only POSIX-compatible grep (-E, not -P) so it works on macOS/BSD
# as well as Linux.  The Python side uses a machine-readable id-map marker
# in the review body as the authoritative source; this bash helper is only
# invoked for the legacy bash engine (still default until Epic 5).
#
# Functions exported:
#   finding_ids_max_from_bodies  BODIES_FILE → prints max ID seen (0 if none)
#   finding_ids_build_map        BODIES_FILE MAP_FILE
#   finding_ids_get              SOURCE FILE LINE TEXT MAP_FILE NEXT_ID_FILE → prints ID

set -euo pipefail

# ---------------------------------------------------------------------------
# _text_hash: first 12 hex chars of SHA-256 of a string.
# ---------------------------------------------------------------------------
_text_hash() {
  printf '%s' "$1" | sha256sum | cut -c1-12
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
# _extract_fid LINE → print the numeric part of **[F<n>]**, or nothing
# ---------------------------------------------------------------------------
_extract_fid() {
  # grep -oE is POSIX-compatible; extract token then strip the F prefix.
  printf '%s' "$1" \
    | grep -oE '\*\*\[F[0-9]+\]\*\*' \
    | head -1 \
    | grep -oE '[0-9]+' \
    || true
}

# ---------------------------------------------------------------------------
# finding_ids_max_from_bodies BODIES_FILE → print max seen ID (0 if none)
# ---------------------------------------------------------------------------
finding_ids_max_from_bodies() {
  local bodies_file="${1:-}"
  if [[ -z "$bodies_file" || ! -f "$bodies_file" ]]; then
    echo 0
    return
  fi

  local max_id=0
  local id
  while IFS= read -r encoded_body; do
    local body
    body=$(printf '%b' "$encoded_body")
    while IFS= read -r id; do
      if [[ -n "$id" && "$id" =~ ^[0-9]+$ && "$id" -gt "$max_id" ]]; then
        max_id="$id"
      fi
    done < <(printf '%s\n' "$body" \
      | grep -oE '\*\*\[F[0-9]+\]\*\*' \
      | grep -oE '[0-9]+' \
      || true)
  done < "$bodies_file"

  echo "$max_id"
}

# ---------------------------------------------------------------------------
# finding_ids_build_map BODIES_FILE MAP_FILE
# ---------------------------------------------------------------------------
finding_ids_build_map() {
  local bodies_file="${1:-}" map_file="$2"
  : > "$map_file"
  if [[ -z "$bodies_file" || ! -f "$bodies_file" ]]; then
    return
  fi

  while IFS= read -r encoded_body; do
    local body
    body=$(printf '%b' "$encoded_body")

    local in_section=false
    while IFS= read -r line; do
      if [[ "$line" == *"### Findings not attached to specific lines"* ]]; then
        in_section=true; continue
      fi
      if $in_section && [[ "$line" =~ ^"###" ]]; then
        in_section=false; continue
      fi
      $in_section || continue
      [[ "$line" =~ ^"- " ]] || continue

      local finding_id
      finding_id=$(_extract_fid "$line")
      [[ -n "$finding_id" ]] || continue

      # Extract source: first [bracketed] group after the **[F<n>]** token.
      # Remove the F-token first, then grab the first [...] group.
      local after_fid source
      after_fid=$(printf '%s' "$line" | sed 's/\*\*\[F[0-9]*\]\*\* //')
      source=$(printf '%s' "$after_fid" | grep -oE '\[[^]]+\]' | head -1 | tr -d '[]' || true)
      source="${source%%,*}"
      source="${source// /}"

      # Extract file:line from location annotation.
      local file_line file part_line
      # Matches: *(at `file:line`...) or — `file:line`
      file_line=$(printf '%s' "$line" | grep -oE '`[^`]+`[^`]*\*' | head -1 | tr -d '`' | sed 's/[^`]*\*$//' || true)
      if [[ -z "$file_line" ]]; then
        file_line=$(printf '%s' "$line" | grep -oE '— `[^`]+' | head -1 | sed 's/— `//' || true)
      fi
      if [[ "$file_line" == *":"* ]]; then
        file="${file_line%:*}"
        part_line="${file_line##*:}"
        if ! [[ "$part_line" =~ ^[0-9]+$ ]]; then
          file="$file_line"
          part_line=""
        fi
      else
        file="$file_line"
        part_line=""
      fi

      # Extract finding text: strip bullet, icon, severity, F-ID, source tag, location.
      local text
      text=$(printf '%s' "$line" \
        | sed 's/^- //' \
        | sed 's/[🚨🔴🟡🔵✅❔] //' \
        | sed 's/\*\*\[[^]]*\]\*\* //g' \
        | sed 's/\[[^]]*\] //' \
        | sed 's/ — `[^`]*`.*$//' \
        | sed 's/ \*(at `[^`]*`.*$//' \
        | sed 's/^[[:space:]]*//;s/[[:space:]]*$//') || true

      [[ -n "$text" ]] || continue

      local fp
      fp=$(_make_fingerprint "$source" "$file" "$part_line" "$text")
      if ! grep -qF "$fp	" "$map_file" 2>/dev/null; then
        printf '%s\t%s\n' "$fp" "$finding_id" >> "$map_file"
      fi
    done <<< "$body"
  done < "$bodies_file"
}

# ---------------------------------------------------------------------------
# finding_ids_get SOURCE FILE LINE TEXT MAP_FILE NEXT_ID_FILE → print ID
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

  # New finding — validate and consume the counter file.
  local next_id
  next_id=$(cat "$next_id_file" 2>/dev/null || true)
  if ! [[ "$next_id" =~ ^[0-9]+$ ]] || [[ "$next_id" -lt 1 ]]; then
    echo "::error::finding-ids: next_id_file '${next_id_file}' is missing or corrupt (value: '${next_id}'). Cannot safely assign body-finding IDs." >&2
    exit 1
  fi
  echo "$next_id"
  echo $((next_id + 1)) > "$next_id_file"
}
