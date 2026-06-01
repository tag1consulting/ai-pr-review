# Suggested Commands

## Python tests
```bash
# From repo root, with venv active
pytest tests/python tests/golden          # full Python suite (~990 tests)
pytest tests/python/test_<module>.py      # single module
pytest -k "test_name"                     # filter by name
```

## Bash tests
```bash
bats tests/*.bats                         # requires bats + jq
```

## Linting / formatting
```bash
ruff check ai_pr_review/                  # lint
ruff check --fix ai_pr_review/            # auto-fix
ruff format ai_pr_review/                 # format
mypy ai_pr_review/                        # type-check
shellcheck review.sh llm-call.sh post-review.sh post-review-bitbucket.sh post-review-gitlab.sh
```

## Install / dev setup
```bash
pip install -e ".[dev,context]"           # editable install with all extras
```

## Smoke test LLM call
```bash
export AI_PROVIDER=anthropic ANTHROPIC_API_KEY=<key>
echo "hello" > /tmp/msg.txt && echo "Say hi" > /tmp/sys.txt
./llm-call.sh claude-haiku-4-5 /tmp/sys.txt /tmp/msg.txt
```

## Run the Python CLI
```bash
ai-pr-review review --help
ai-pr-review slash --help
```
