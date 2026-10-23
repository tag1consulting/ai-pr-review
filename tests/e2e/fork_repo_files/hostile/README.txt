Inert payloads for the fork e2e profile. Every file ends in .txt so no tool in this repository
loads it. MANIFEST.json maps each file to its path on the fork branch. A later runner strips the
.txt suffix when it commits the file to the fork. {RUN_ID} is replaced with the run ID.

Canary files try to write /workspace/E2E-PWNED-<tool> if a tool runs fork code. The two config
files (.golangci.yml, .trufflehog.yml) cannot write a file. They only show that the engine does
not read a fork config. No real secret is planted anywhere.
