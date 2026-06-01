# Agents

## Key files
- `ai_pr_review/agents/roster.py` — `AGENTS` list of `AgentSpec`; `get_agent(name)`
- `ai_pr_review/agents/dispatch.py` — `call_agent`, `call_agent_bg`, `wait_tier_pids`, etc.
- `ai_pr_review/agents/gates.py` — conditional trigger logic (`ConditionalTrigger`, `_VALID_TRIGGERS`)
- `ai_pr_review/agents/summarizer.py` — summarizer agent logic
- `prompts/<agent-name>.md` — system prompt for each agent; must instruct model to emit `json-findings` block

## Adding a new agent (Python path)
1. Add `prompts/<agent-name>.md` with a `json-findings` instruction.
2. Register an `AgentSpec` in `ai_pr_review/agents/roster.py:AGENTS`.
3. Gate conditionally via `ConditionalTrigger` if the agent should only run for certain languages/files.
4. Assign to Tier 1 or Tier 2 in `ai_pr_review/orchestrate.py`.

## Tier model
- **Tier 1** — runs in parallel for all reviews.
- **Tier 2** — runs after Tier 1; can use Tier 1 outputs as context.
- Bash equivalent in `review.sh` parallel tier blocks.
