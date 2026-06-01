# Task Completion Checklist

Run all of these before marking a coding task done or opening a PR:

```bash
# 1. Ruff lint (must be clean)
ruff check ai_pr_review/

# 2. Ruff format (must be no-op)
ruff format --check ai_pr_review/

# 3. Mypy type check (must be clean)
mypy ai_pr_review/

# 4. Full Python test suite (must pass)
pytest tests/python tests/golden

# 5. Bash test suite (must pass, requires bats + jq)
bats tests/*.bats

# 6. Shellcheck (must be clean for touched scripts)
shellcheck review.sh llm-call.sh post-review.sh post-review-bitbucket.sh post-review-gitlab.sh
```

Also required per project workflow:
- Run `/comprehensive-review` before pushing to an existing PR or opening a new one.
- If languages.py `_EXT_MAP` changed: verify `lib/languages.sh:detect_language()` is in sync.
- If a new language profile was added: verify filename matches the `detect_language()` key.
