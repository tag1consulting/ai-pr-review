#!/usr/bin/env bats
# Tests for the confidence filter and proximity dedup pipeline in review.sh.
# These are inline jq pipelines, not named functions, so tests drive them
# directly via jq with the same expressions lifted verbatim from review.sh.

setup() {
  load test_helper
  FINDINGS_JSON_FILE=$(mktemp)
}

teardown() {
  rm -f "$FINDINGS_JSON_FILE" "${FINDINGS_JSON_FILE}.tmp"
}

# ---------------------------------------------------------------------------
# Confidence filter: .confidence >= 75
# ---------------------------------------------------------------------------

_run_confidence_filter() {
  jq '[.[] | select((.confidence // 0) >= 75)]' "$FINDINGS_JSON_FILE"
}

@test "confidence filter: keeps findings at exactly 75" {
  echo '[{"severity":"High","confidence":75,"file":"a.sh","line":1,"finding":"x","remediation":"y"}]' \
    > "$FINDINGS_JSON_FILE"
  run _run_confidence_filter
  [ "$status" -eq 0 ]
  count=$(echo "$output" | jq 'length')
  [ "$count" -eq 1 ]
}

@test "confidence filter: keeps findings above 75" {
  echo '[{"severity":"High","confidence":90,"file":"a.sh","line":1,"finding":"x","remediation":"y"}]' \
    > "$FINDINGS_JSON_FILE"
  run _run_confidence_filter
  [ "$status" -eq 0 ]
  count=$(echo "$output" | jq 'length')
  [ "$count" -eq 1 ]
}

@test "confidence filter: drops findings below 75" {
  echo '[{"severity":"High","confidence":74,"file":"a.sh","line":1,"finding":"x","remediation":"y"}]' \
    > "$FINDINGS_JSON_FILE"
  run _run_confidence_filter
  [ "$status" -eq 0 ]
  count=$(echo "$output" | jq 'length')
  [ "$count" -eq 0 ]
}

@test "confidence filter: drops findings at 0" {
  echo '[{"severity":"Low","confidence":0,"file":"a.sh","line":1,"finding":"x","remediation":"y"}]' \
    > "$FINDINGS_JSON_FILE"
  run _run_confidence_filter
  [ "$status" -eq 0 ]
  count=$(echo "$output" | jq 'length')
  [ "$count" -eq 0 ]
}

@test "confidence filter: treats missing confidence as 0 (dropped)" {
  echo '[{"severity":"High","file":"a.sh","line":1,"finding":"x","remediation":"y"}]' \
    > "$FINDINGS_JSON_FILE"
  run _run_confidence_filter
  [ "$status" -eq 0 ]
  count=$(echo "$output" | jq 'length')
  [ "$count" -eq 0 ]
}

@test "confidence filter: keeps only passing findings from a mixed array" {
  cat > "$FINDINGS_JSON_FILE" <<'EOF'
[
  {"severity":"Critical","confidence":95,"file":"a.sh","line":1,"finding":"x","remediation":"y"},
  {"severity":"High","confidence":74,"file":"a.sh","line":2,"finding":"x","remediation":"y"},
  {"severity":"Medium","confidence":80,"file":"a.sh","line":3,"finding":"x","remediation":"y"}
]
EOF
  run _run_confidence_filter
  [ "$status" -eq 0 ]
  count=$(echo "$output" | jq 'length')
  [ "$count" -eq 2 ]
}

# ---------------------------------------------------------------------------
# Proximity dedup: cluster-start-based, 3-line window, highest severity wins
# ---------------------------------------------------------------------------

_run_dedup() {
  jq '
    def sev_rank: if . == "Critical" then 4 elif . == "High" then 3
      elif . == "Medium" then 2 else 1 end;
    def merge_cluster(cur; f):
      if (cur.best.severity | sev_rank) >= (f.severity | sev_rank)
      then {start: cur.start, best: cur.best, sources: (cur.sources + [f.source // "unknown"])}
      else {start: cur.start, best: f, sources: (cur.sources + [cur.best.source // "unknown"])}
      end;
    group_by(.file // "unknown")
    | map(
        sort_by(.line // 0) |
        reduce .[] as $f (
          {clusters: [], cur: null};
          if .cur == null then
            {clusters: .clusters, cur: {start: ($f.line // 0), best: $f, sources: [$f.source // "unknown"]}}
          elif (($f.line // 0) - .cur.start) <= 3
          then
            {clusters: .clusters, cur: (merge_cluster(.cur; $f))}
          else
            {clusters: (.clusters + [.cur.best + {sources: (.cur.sources | unique | sort)}]),
             cur: {start: ($f.line // 0), best: $f, sources: [$f.source // "unknown"]}}
          end
        )
        | if .cur != null
          then .clusters + [.cur.best + {sources: (.cur.sources | unique | sort)}]
          else .clusters end
      )
    | flatten
  ' "$FINDINGS_JSON_FILE"
}

@test "dedup: single finding passes through unchanged" {
  echo '[{"severity":"High","confidence":90,"file":"a.sh","line":10,"finding":"x","remediation":"y"}]' \
    > "$FINDINGS_JSON_FILE"
  run _run_dedup
  [ "$status" -eq 0 ]
  count=$(echo "$output" | jq 'length')
  [ "$count" -eq 1 ]
}

@test "dedup: two findings in same file exactly 3 lines apart are merged" {
  cat > "$FINDINGS_JSON_FILE" <<'EOF'
[
  {"severity":"Low","confidence":80,"file":"a.sh","line":10,"finding":"first","remediation":"y"},
  {"severity":"High","confidence":90,"file":"a.sh","line":13,"finding":"second","remediation":"y"}
]
EOF
  run _run_dedup
  [ "$status" -eq 0 ]
  count=$(echo "$output" | jq 'length')
  [ "$count" -eq 1 ]
  # High severity wins
  sev=$(echo "$output" | jq -r '.[0].severity')
  [ "$sev" = "High" ]
}

@test "dedup: two findings 4 lines apart are NOT merged" {
  cat > "$FINDINGS_JSON_FILE" <<'EOF'
[
  {"severity":"Low","confidence":80,"file":"a.sh","line":10,"finding":"first","remediation":"y"},
  {"severity":"High","confidence":90,"file":"a.sh","line":14,"finding":"second","remediation":"y"}
]
EOF
  run _run_dedup
  [ "$status" -eq 0 ]
  count=$(echo "$output" | jq 'length')
  [ "$count" -eq 2 ]
}

@test "dedup: cluster uses start line, not previous line (no single-linkage drift)" {
  # Three findings: lines 10, 12, 15. If cluster-start stays at 10:
  #   line 12: 12-10=2 <=3 → same cluster
  #   line 15: 15-10=5 >3  → new cluster
  # Single-linkage (wrong) would merge all three: 12-10=2, 15-12=3 both <=3.
  cat > "$FINDINGS_JSON_FILE" <<'EOF'
[
  {"severity":"Low","confidence":80,"file":"a.sh","line":10,"finding":"a","remediation":"y"},
  {"severity":"Low","confidence":80,"file":"a.sh","line":12,"finding":"b","remediation":"y"},
  {"severity":"High","confidence":90,"file":"a.sh","line":15,"finding":"c","remediation":"y"}
]
EOF
  run _run_dedup
  [ "$status" -eq 0 ]
  count=$(echo "$output" | jq 'length')
  [ "$count" -eq 2 ]
}

@test "dedup: findings in different files are not merged" {
  cat > "$FINDINGS_JSON_FILE" <<'EOF'
[
  {"severity":"High","confidence":90,"file":"a.sh","line":10,"finding":"x","remediation":"y"},
  {"severity":"High","confidence":90,"file":"b.sh","line":10,"finding":"x","remediation":"y"}
]
EOF
  run _run_dedup
  [ "$status" -eq 0 ]
  count=$(echo "$output" | jq 'length')
  [ "$count" -eq 2 ]
}

@test "dedup: Critical beats High in same cluster" {
  cat > "$FINDINGS_JSON_FILE" <<'EOF'
[
  {"severity":"High","confidence":90,"file":"a.sh","line":5,"finding":"high","remediation":"y"},
  {"severity":"Critical","confidence":95,"file":"a.sh","line":7,"finding":"critical","remediation":"y"}
]
EOF
  run _run_dedup
  [ "$status" -eq 0 ]
  count=$(echo "$output" | jq 'length')
  [ "$count" -eq 1 ]
  sev=$(echo "$output" | jq -r '.[0].severity')
  [ "$sev" = "Critical" ]
  # The kept finding is the Critical one
  finding=$(echo "$output" | jq -r '.[0].finding')
  [ "$finding" = "critical" ]
}

@test "dedup: findings with null line are treated as line 0 (cluster together)" {
  cat > "$FINDINGS_JSON_FILE" <<'EOF'
[
  {"severity":"Low","confidence":80,"file":"a.sh","finding":"a","remediation":"y"},
  {"severity":"High","confidence":90,"file":"a.sh","finding":"b","remediation":"y"}
]
EOF
  run _run_dedup
  [ "$status" -eq 0 ]
  count=$(echo "$output" | jq 'length')
  [ "$count" -eq 1 ]
  sev=$(echo "$output" | jq -r '.[0].severity')
  [ "$sev" = "High" ]
}

@test "dedup: empty input produces empty output" {
  echo '[]' > "$FINDINGS_JSON_FILE"
  run _run_dedup
  [ "$status" -eq 0 ]
  count=$(echo "$output" | jq 'length')
  [ "$count" -eq 0 ]
}

# ---------------------------------------------------------------------------
# merge_findings: per-element validation
# ---------------------------------------------------------------------------

_setup_merge() {
  load_function "${PROJECT_ROOT}/review.sh" merge_findings
  FINDINGS_JSON_FILE=$(mktemp)
  echo '[]' > "$FINDINGS_JSON_FILE"
}

@test "merge_findings: merges valid findings into empty accumulator" {
  _setup_merge
  incoming='[{"severity":"High","confidence":90,"file":"a.sh","line":1,"finding":"x","remediation":"y"}]'
  merge_findings "$incoming"
  count=$(jq 'length' "$FINDINGS_JSON_FILE")
  [ "$count" -eq 1 ]
}

@test "merge_findings: drops finding missing severity field" {
  _setup_merge
  # One valid, one without severity
  incoming='[
    {"severity":"High","confidence":90,"file":"a.sh","line":1,"finding":"good","remediation":"y"},
    {"confidence":80,"file":"a.sh","line":2,"finding":"bad","remediation":"y"}
  ]'
  run merge_findings "$incoming"
  # The valid one is kept; the invalid one is dropped; warning goes to stderr
  count=$(jq 'length' "$FINDINGS_JSON_FILE")
  [ "$count" -eq 1 ]
  [ "$status" -eq 0 ]
}

@test "merge_findings: drops finding missing finding field" {
  _setup_merge
  incoming='[{"severity":"High","confidence":90,"file":"a.sh","line":1,"remediation":"y"}]'
  merge_findings "$incoming"
  count=$(jq 'length' "$FINDINGS_JSON_FILE")
  [ "$count" -eq 0 ]
}

@test "merge_findings: drops finding missing confidence field" {
  _setup_merge
  incoming='[{"severity":"High","file":"a.sh","line":1,"finding":"x","remediation":"y"}]'
  merge_findings "$incoming"
  count=$(jq 'length' "$FINDINGS_JSON_FILE")
  [ "$count" -eq 0 ]
}

@test "merge_findings: accumulates across multiple calls" {
  _setup_merge
  merge_findings '[{"severity":"High","confidence":90,"file":"a.sh","line":1,"finding":"x","remediation":"y"}]'
  merge_findings '[{"severity":"Low","confidence":80,"file":"b.sh","line":2,"finding":"z","remediation":"y"}]'
  count=$(jq 'length' "$FINDINGS_JSON_FILE")
  [ "$count" -eq 2 ]
}

@test "merge_findings: empty incoming array is a no-op" {
  _setup_merge
  merge_findings '[]'
  count=$(jq 'length' "$FINDINGS_JSON_FILE")
  [ "$count" -eq 0 ]
}

# ---------------------------------------------------------------------------
# sources array: provenance tracking through dedup
# ---------------------------------------------------------------------------

@test "dedup: single finding gets sources array with its own source" {
  echo '[{"severity":"High","confidence":90,"file":"a.sh","line":10,"finding":"x","remediation":"y","source":"code-reviewer"}]' \
    > "$FINDINGS_JSON_FILE"
  run _run_dedup
  [ "$status" -eq 0 ]
  count=$(echo "$output" | jq 'length')
  [ "$count" -eq 1 ]
  sources=$(echo "$output" | jq -r '.[0].sources | length')
  [ "$sources" -eq 1 ]
  echo "$output" | jq -e '.[0].sources[0] == "code-reviewer"' > /dev/null
}

@test "dedup: same-source cluster collapses to single source entry" {
  cat > "$FINDINGS_JSON_FILE" <<'EOF'
[
  {"severity":"Low","confidence":80,"file":"a.sh","line":10,"finding":"a","remediation":"y","source":"code-reviewer"},
  {"severity":"High","confidence":90,"file":"a.sh","line":12,"finding":"b","remediation":"y","source":"code-reviewer"}
]
EOF
  run _run_dedup
  [ "$status" -eq 0 ]
  count=$(echo "$output" | jq 'length')
  [ "$count" -eq 1 ]
  # Same source deduped — sources should only contain one unique entry
  echo "$output" | jq -e '.[0].sources | length == 1' > /dev/null
  echo "$output" | jq -e '.[0].sources[0] == "code-reviewer"' > /dev/null
}

@test "dedup: cross-source cluster preserves both sources in winner" {
  cat > "$FINDINGS_JSON_FILE" <<'EOF'
[
  {"severity":"High","confidence":90,"file":"a.sh","line":10,"finding":"llm finding","remediation":"y","source":"code-reviewer"},
  {"severity":"High","confidence":95,"file":"a.sh","line":11,"finding":"shellcheck finding","remediation":"y","source":"shellcheck"}
]
EOF
  run _run_dedup
  [ "$status" -eq 0 ]
  count=$(echo "$output" | jq 'length')
  [ "$count" -eq 1 ]
  # sources must contain both
  echo "$output" | jq -e '.[0].sources | length == 2' > /dev/null
  echo "$output" | jq -e '[.[0].sources[] | select(. == "code-reviewer")] | length == 1' > /dev/null
  echo "$output" | jq -e '[.[0].sources[] | select(. == "shellcheck")] | length == 1' > /dev/null
}

@test "dedup: sources array is sorted and deduplicated" {
  cat > "$FINDINGS_JSON_FILE" <<'EOF'
[
  {"severity":"Medium","confidence":80,"file":"a.sh","line":10,"finding":"a","remediation":"y","source":"shellcheck"},
  {"severity":"Medium","confidence":80,"file":"a.sh","line":11,"finding":"b","remediation":"y","source":"code-reviewer"},
  {"severity":"High","confidence":90,"file":"a.sh","line":12,"finding":"c","remediation":"y","source":"shellcheck"}
]
EOF
  run _run_dedup
  [ "$status" -eq 0 ]
  count=$(echo "$output" | jq 'length')
  [ "$count" -eq 1 ]
  # shellcheck appears twice but should be deduped to once
  echo "$output" | jq -e '.[0].sources | length == 2' > /dev/null
}

@test "dedup: findings without source field get unknown in sources" {
  cat > "$FINDINGS_JSON_FILE" <<'EOF'
[
  {"severity":"Low","confidence":80,"file":"a.sh","line":10,"finding":"a","remediation":"y"},
  {"severity":"High","confidence":90,"file":"a.sh","line":12,"finding":"b","remediation":"y"}
]
EOF
  run _run_dedup
  [ "$status" -eq 0 ]
  count=$(echo "$output" | jq 'length')
  [ "$count" -eq 1 ]
  echo "$output" | jq -e '.[0].sources | all(. == "unknown")' > /dev/null
}
