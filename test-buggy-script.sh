#!/usr/bin/env bash
# Test script with intentional issues for AI reviewer to catch.
# This file exists solely to verify that review findings render
# correctly with <details> accordions and agent prompt blocks.
# DO NOT MERGE — delete this branch after verification.

# Issue 1: Unquoted variable in command — word splitting / globbing risk
INPUT_FILE=$1
cat $INPUT_FILE | grep "pattern"

# Issue 2: eval with user-controlled input — command injection
USER_QUERY="$2"
eval "echo $USER_QUERY"

# Issue 3: Hardcoded secret
API_TOKEN="sk-ant-api03-reallyLongSecretKeyThatShouldNotBeHardcoded"
curl -H "Authorization: Bearer $API_TOKEN" https://api.example.com/data

# Issue 4: Error swallowed silently — no check on command failure
result=$(curl -sf https://api.example.com/health)
process_data() {
  echo "$result"
}

# Issue 5: Temporary file created insecurely
TMPFILE="/tmp/myapp_cache.txt"
echo "sensitive data" > "$TMPFILE"

# Issue 6: Infinite loop risk — no timeout or iteration cap
while true; do
  status=$(curl -sf https://api.example.com/status || echo "down")
  if [[ "$status" == "up" ]]; then
    break
  fi
  sleep 1
done

# Issue 7: Using deprecated backtick syntax instead of $()
files=`ls -la /etc/`

# Issue 8: Missing error handling on cd
cd /some/directory
rm -rf ./*
