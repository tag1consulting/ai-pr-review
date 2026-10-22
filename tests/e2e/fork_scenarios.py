"""Declarative scenario table for the fork pull request e2e profile, and an offline validator.

Nothing here makes a network call. The table is data: setup steps name an action and its
arguments, and a later runner (not written yet) will map each action to a GitHub call or to a
dispatch of `e2e-ledger-tools.yml`. `validate_table` checks the table against the design rules,
and `python -m tests.e2e.fork_scenarios --plan` prints the report.

Design: memory-bank/bmad/planning-artifacts/e2e-fork-pr-test-design.md (not tracked in git).
Exit codes follow the harness: 0 pass, 2 invalid plan.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal

from ai_pr_review.analyzers.bridge import fork_skipped_eligible
from ai_pr_review.manifest import build_changed_files
from ai_pr_review.review.fork_spend import AUTHOR_MAX_PRS

from . import fork_verify

EXIT_PASS: Final[int] = 0
EXIT_INVALID: Final[int] = 2

FORK_WORKFLOW: Final[str] = "ai-pr-review-fork.yml"
TOOLS_WORKFLOW: Final[str] = "e2e-ledger-tools.yml"
REPO_FILES_DIR: Final[Path] = Path(__file__).parent / "fork_repo_files"

# Budget from the design (USD) and the per-run ceiling the drafted workflow sets.
DESIGN_BUDGET_USD: Final[float] = 4.0
RUN_CEILING_USD: Final[float] = 1.0
LLM_RUN_USD_RANGE: Final[tuple[float, float]] = (0.2, 0.6)

# The S3 fork diff. Each file type adds one analyzer to the "Not reviewed" notice, so the list
# must reach every fork-skipped analyzer (semgrep has no file gate).
S3_DIFF_PATHS: Final[tuple[str, ...]] = (
    "src/Controller/Report.php",
    "infra/main.tf",
    "tools/report.py",
    "web/app.js",
    "cmd/main.go",
    "scripts/run.sh",
    "Dockerfile",
    "k8s/deployment.yaml",
)

Cost = Literal["zero", "llm"]


@dataclass(frozen=True)
class Step:
    action: str
    args: dict[str, Any] = field(default_factory=dict)


# action -> required argument names
ACTIONS: Final[dict[str, frozenset[str]]] = {
    "disable_workflow": frozenset({"workflow"}),
    "enable_workflow": frozenset({"workflow"}),
    "set_var": frozenset({"name", "value"}),
    "unset_var": frozenset({"name"}),
    "open_fork_pr": frozenset({"pr", "draft"}),
    "mark_ready": frozenset({"pr"}),
    "push_commit": frozenset({"pr"}),
    "close_pr": frozenset({"pr"}),
    "clear_ledgers": frozenset({"keep_issue"}),
    "seed_ledger": frozenset({"pr", "runs", "usd"}),
    "seed_spend": frozenset({"tier", "usd"}),
    "lock_spend_issue": frozenset(),
    "unlock_spend_issue": frozenset(),
    "comment_as_author": frozenset({"pr", "text"}),
    "comment_as_maintainer": frozenset({"pr", "text"}),
    "forge_comment": frozenset({"pr", "kind"}),
    "wait_for_run": frozenset({"pr", "event"}),
    "collect_canary": frozenset({"pr"}),
    "list_spend_issues": frozenset(),
}
# Actions that become inputs of the `e2e-ledger-tools.yml` workflow (the bot writes them).
TOOL_ACTIONS: Final[dict[str, str]] = {
    "seed_ledger": "seed-ledger",
    "seed_spend": "seed-spend",
    "clear_ledgers": "clear-ledgers",
    "lock_spend_issue": "lock-spend-issue",
    "unlock_spend_issue": "unlock-spend-issue",
    "collect_canary": "check-canary",
}


@dataclass(frozen=True)
class Scenario:
    id: str
    title: str
    cost: Cost
    setup: tuple[Step, ...] = ()
    teardown: tuple[Step, ...] = ()
    outcome_key: str = ""  # the key in fork_verify.VERIFIERS. Defaults to the id.
    llm_runs_nominal: int = 0
    llm_runs_worst: int = 0
    needs_second_maintainer: bool = False
    pr: str = ""  # the named PR this scenario acts on. Empty for S16.
    rides_on: str = ""  # the scenario whose run this one reads, so it adds no run of its own
    first_review_on_pr: bool = False  # the fixed summary notice shows only on a first review
    expects_author_cap_pass: bool = True  # the PR must be among the two lowest open fork PRs

    @property
    def key(self) -> str:
        return self.outcome_key or self.id


def _s(action: str, **args: Any) -> Step:
    return Step(action, args)


def _open(pr: str, *, draft: bool = False) -> Step:
    return _s("open_fork_pr", pr=pr, draft=draft)


def _wait(pr: str, event: str = "pull_request_target") -> Step:
    return _s("wait_for_run", pr=pr, event=event)


_CLEAR = _s("clear_ledgers", keep_issue=True)

SCENARIOS: Final[tuple[Scenario, ...]] = (
    Scenario(
        "S1", "Plain pull_request run on a fork does nothing", "zero", pr="s1",
        setup=(_s("disable_workflow", workflow=FORK_WORKFLOW), _open("s1"), _wait("s1", "pull_request"),
               _s("close_pr", pr="s1")),
        teardown=(_s("enable_workflow", workflow=FORK_WORKFLOW),),
    ),
    Scenario(
        "S2", "Policy block", "zero", pr="s2",
        setup=(_s("set_var", name="AI_FORK_POLICY", value="block"), _open("s2"), _wait("s2"),
               _s("close_pr", pr="s2")),
        teardown=(_s("unset_var", name="AI_FORK_POLICY"),),
        expects_author_cap_pass=False,
    ),
    Scenario(
        "S6", "Stored credential pauses", "zero", pr="s6",
        setup=(_s("set_var", name="E2E_PERSIST_CREDENTIALS", value="true"), _open("s6"), _wait("s6"),
               _s("close_pr", pr="s6")),
        teardown=(_s("unset_var", name="E2E_PERSIST_CREDENTIALS"),),
        expects_author_cap_pass=False,
    ),
    Scenario(
        "S8", "Per-PR cost cap", "zero", pr="s8",
        setup=(_CLEAR, _open("s8", draft=True), _s("seed_ledger", pr="s8", runs=2, usd="5.99"),
               _s("mark_ready", pr="s8"), _wait("s8"), _s("close_pr", pr="s8")),
    ),
    Scenario(
        "S11", "Daily cap", "zero", pr="s11",
        setup=(_CLEAR, _s("seed_spend", tier="shared", usd="15.00"), _open("s11"), _wait("s11"),
               _s("close_pr", pr="s11")),
    ),
    Scenario(
        "S15", "Forged entries ignored", "zero", pr="s15",
        setup=(_CLEAR, _open("s15", draft=True), _s("seed_ledger", pr="s15", runs=4, usd="0.40"),
               _s("forge_comment", pr="s15", kind="ledger-credit"),
               _s("forge_comment", pr="s15", kind="ledger-run"),
               _s("mark_ready", pr="s15"), _wait("s15"), _s("close_pr", pr="s15")),
    ),
    Scenario(
        "S3", "Default auto, fork profile review", "llm", pr="s3", llm_runs_nominal=1, llm_runs_worst=1,
        first_review_on_pr=True,
        setup=(_CLEAR, _open("s3"), _wait("s3"), _s("collect_canary", pr="s3"), _s("close_pr", pr="s3")),
    ),
    Scenario("S4", "Hostile model output cannot approve", "llm", pr="s3", rides_on="S3"),
    Scenario("S17", "Fork analyzers ran, no canary fired", "llm", pr="s3", rides_on="S3"),
    Scenario(
        "S5", "Trusted author skips the profile", "llm", pr="s5", llm_runs_nominal=1, llm_runs_worst=1,
        setup=(_s("set_var", name="AI_FORK_TRUSTED_ASSOCIATIONS", value="MEMBER"), _CLEAR, _open("s5"),
               _wait("s5"), _s("close_pr", pr="s5")),
        teardown=(_s("unset_var", name="AI_FORK_TRUSTED_ASSOCIATIONS"),),
    ),
    Scenario(
        "S7", "Per-PR run cap", "zero", pr="capped",
        setup=(_CLEAR, _open("capped", draft=True), _s("seed_ledger", pr="capped", runs=4, usd="0.40"),
               _s("mark_ready", pr="capped"), _wait("capped")),
    ),
    Scenario(
        "S9", "Own rescan earns nothing", "zero", pr="capped",
        setup=(_s("comment_as_author", pr="capped", text="/ai-pr-review rescan"), _wait("capped", "issue_comment")),
    ),
    Scenario(
        "S10", "Maintainer rescan resumes and adds exactly one run", "llm", pr="capped",
        llm_runs_nominal=1, llm_runs_worst=1, needs_second_maintainer=True,
        setup=(_s("forge_comment", pr="capped", kind="watermark"),
               _s("comment_as_maintainer", pr="capped", text="/ai-pr-review rescan"),
               _wait("capped", "issue_comment"), _s("push_commit", pr="capped"), _wait("capped"),
               _s("close_pr", pr="capped")),
    ),
    Scenario(
        "S14", "Cancelled run stays charged", "llm", pr="s14", llm_runs_nominal=1, llm_runs_worst=2,
        setup=(_CLEAR, _open("s14"), _s("push_commit", pr="s14"), _wait("s14"), _s("close_pr", pr="s14")),
    ),
    Scenario(
        "S13", "Locked spend issue", "llm", pr="s13", llm_runs_nominal=0, llm_runs_worst=1,
        setup=(_CLEAR, _s("lock_spend_issue"), _open("s13"), _wait("s13"), _s("close_pr", pr="s13")),
        teardown=(_s("unlock_spend_issue"),),
    ),
    Scenario(
        "S12", "Author cap", "llm", pr="a1", llm_runs_nominal=2, llm_runs_worst=2,
        setup=(_CLEAR, _open("a1"), _open("a2"), _open("a3"), _wait("a1"), _wait("a2"), _wait("a3"),
               _s("close_pr", pr="a1"), _s("close_pr", pr="a2"), _s("close_pr", pr="a3")),
    ),
    Scenario("S16", "One spend issue", "zero", setup=(_s("list_spend_issues"),), expects_author_cap_pass=False),
)

# The order the runner follows. Zero-cost scenarios first (design phase P1), then the LLM ones.
EXPECTED_IDS: Final[tuple[str, ...]] = tuple(f"S{n}" for n in range(1, 18))


def expected_skipped_for_paths(paths: tuple[str, ...]) -> list[str]:
    """The analyzers the 'Not reviewed' notice names for a fork diff with these paths."""
    return fork_skipped_eligible(build_changed_files(list(paths)))


def _tool_inputs_in_workflow() -> set[str] | None:
    """The `action` options of the drafted tools workflow, or None when the file is absent."""
    path = REPO_FILES_DIR / TOOLS_WORKFLOW
    if not path.exists():
        return None
    import yaml

    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    triggers = loaded.get("on") or loaded.get(True) or {}
    options = triggers["workflow_dispatch"]["inputs"]["action"]["options"]
    return {str(o) for o in options}


def validate_table(table: tuple[Scenario, ...] = SCENARIOS) -> list[str]:
    """Check the table against the design. Returns the errors, empty when valid. No I/O but one file read."""
    errors: list[str] = []
    ids = [s.id for s in table]
    if sorted(ids, key=lambda i: int(i[1:])) != list(EXPECTED_IDS):
        errors.append(f"scenario ids differ from S1..S17: {sorted(set(ids) ^ set(EXPECTED_IDS))}")
    for s in table:
        if s.key not in fork_verify.VERIFIERS:
            errors.append(f"{s.id}: no verifier named {s.key}")
        if s.cost == "zero" and (s.llm_runs_nominal or s.llm_runs_worst):
            errors.append(f"{s.id}: a zero-cost scenario lists LLM runs")
        if s.llm_runs_worst < s.llm_runs_nominal:
            errors.append(f"{s.id}: worst case below nominal")
        if s.rides_on and (s.llm_runs_nominal or s.setup):
            errors.append(f"{s.id}: a riding scenario adds no setup and no run")
        for step in (*s.setup, *s.teardown):
            required = ACTIONS.get(step.action)
            if required is None:
                errors.append(f"{s.id}: unknown action {step.action}")
            elif not required <= set(step.args):
                errors.append(f"{s.id}: {step.action} lacks {sorted(required - set(step.args))}")
        errors += _teardown_errors(s)
    errors += _order_errors(table)
    errors += _cap_errors(table)
    errors += _tool_errors(table)
    return errors


def _teardown_errors(s: Scenario) -> list[str]:
    """Every variable, disabled workflow and lock that setup changes is restored by the end."""
    state: set[tuple[str, str]] = set()
    for step in (*s.setup, *s.teardown):
        a = step.args
        if step.action == "set_var":
            state.add(("var", a["name"]))
        elif step.action == "unset_var":
            state.discard(("var", a["name"]))
        elif step.action == "disable_workflow":
            state.add(("workflow", a["workflow"]))
        elif step.action == "enable_workflow":
            state.discard(("workflow", a["workflow"]))
        elif step.action == "lock_spend_issue":
            state.add(("lock", "spend"))
        elif step.action == "unlock_spend_issue":
            state.discard(("lock", "spend"))
    return [f"{s.id}: {kind} {name} is not restored" for kind, name in sorted(state)]


def _order_errors(table: tuple[Scenario, ...]) -> list[str]:
    errors: list[str] = []
    pos = {s.id: i for i, s in enumerate(table)}

    def before(a: str, b: str, why: str) -> None:
        if a in pos and b in pos and pos[a] >= pos[b]:
            errors.append(f"{a} must come before {b} ({why})")

    before("S7", "S9", "S9 needs the capped PR")
    before("S9", "S10", "S10 resumes the capped PR")
    before("S3", "S16", "S16 needs the spend issue")
    before("S3", "S4", "S4 rides on S3")
    before("S3", "S17", "S17 rides on S3")
    before("S13", "S12", "S12 must start from no open fork PRs, and S13 leaves the issue unlocked")
    seen_pr: dict[str, str] = {}
    for s in table:
        if not s.pr or s.rides_on:
            continue
        prior = seen_pr.get(s.pr)
        if s.first_review_on_pr and prior:
            errors.append(f"{s.id}: not the first review on PR {s.pr} (after {prior})")
        if prior and prior_cost(table, prior) == "llm" and s.cost == "zero":
            errors.append(f"{s.id}: a zero-cost scenario follows an LLM one on PR {s.pr}")
        seen_pr[s.pr] = s.id
    return errors


def prior_cost(table: tuple[Scenario, ...], sid: str) -> str:
    return next(s.cost for s in table if s.id == sid)


def _cap_errors(table: tuple[Scenario, ...]) -> list[str]:
    """Simulate the open fork PRs of one author. The author cap reviews only the two lowest numbers."""
    errors: list[str] = []
    open_prs: list[str] = []
    for s in table:
        if s.id == "S12" and open_prs:
            errors.append(f"S12: starts with open fork PRs {open_prs}, the author cap would pause earlier ones")
        for step in s.setup:
            if step.action == "open_fork_pr":
                open_prs.append(step.args["pr"])
                if s.id != "S12" and s.expects_author_cap_pass and len(open_prs) > AUTHOR_MAX_PRS:
                    errors.append(f"{s.id}: {len(open_prs)} fork PRs are open, above the author cap")
            elif step.action == "close_pr" and step.args["pr"] in open_prs:
                open_prs.remove(step.args["pr"])
        if (
            s.id != "S12" and s.pr and s.expects_author_cap_pass and s.pr in open_prs
            and open_prs.index(s.pr) >= AUTHOR_MAX_PRS
        ):
            errors.append(f"{s.id}: PR {s.pr} is not among the {AUTHOR_MAX_PRS} lowest open fork PRs")
    if open_prs:
        errors.append(f"PRs left open at the end: {open_prs}")
    return errors


def _tool_errors(table: tuple[Scenario, ...]) -> list[str]:
    errors: list[str] = []
    options = _tool_inputs_in_workflow()
    for s in table:
        for step in (*s.setup, *s.teardown):
            if step.action == "clear_ledgers" and step.args.get("keep_issue") is not True:
                errors.append(f"{s.id}: clear_ledgers must keep the spend issue (S16 counts it)")
            tool = TOOL_ACTIONS.get(step.action)
            if tool and options is not None and tool not in options:
                errors.append(f"{s.id}: {TOOL_ACTIONS[step.action]} is not an option of {TOOLS_WORKFLOW}")
    return errors


@dataclass(frozen=True)
class CostSummary:
    nominal_runs: int
    worst_runs: int
    nominal_usd: tuple[float, float]
    worst_ceiling_usd: float
    over_budget: bool


def cost_summary(table: tuple[Scenario, ...] = SCENARIOS) -> CostSummary:
    nominal = sum(s.llm_runs_nominal for s in table)
    worst = sum(s.llm_runs_worst for s in table)
    lo, hi = LLM_RUN_USD_RANGE
    return CostSummary(
        nominal, worst, (nominal * lo, nominal * hi), worst * RUN_CEILING_USD,
        worst * RUN_CEILING_USD > DESIGN_BUDGET_USD)


def plan_report(table: tuple[Scenario, ...] = SCENARIOS) -> tuple[int, str]:
    """The offline plan: exit code and text. No network."""
    errors = validate_table(table)
    cost = cost_summary(table)
    lines = ["plan: profile=fork platform=github"]
    for s in table:
        mark = "needs second maintainer" if s.needs_second_maintainer else ""
        lines.append(f"  {s.id:<4}{s.cost:<5}pr={s.pr or '-':<7}steps={len(s.setup):<2}{mark}")
    lines.append(
        f"  LLM runs nominal={cost.nominal_runs} (about {cost.nominal_usd[0]:.2f} to {cost.nominal_usd[1]:.2f} USD), "
        f"worst={cost.worst_runs}")
    lines.append(
        f"  worst case at the {RUN_CEILING_USD:.2f} USD per-run ceiling: {cost.worst_ceiling_usd:.2f} USD "
        f"(design budget {DESIGN_BUDGET_USD:.2f} USD). The ceiling is per run, not a total stop.")
    if cost.over_budget:
        lines.append("  WARNING: the worst case can pass the design budget")
    if errors:
        lines += [f"  ERROR: {e}" for e in errors]
        lines.append("plan: INVALID")
        return EXIT_INVALID, "\n".join(lines)
    lines.append("plan: OK")
    return EXIT_PASS, "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tests.e2e.fork_scenarios")
    parser.add_argument("--plan", action="store_true", help="validate the table offline and print the plan")
    args = parser.parse_args(argv)
    if not args.plan:
        parser.print_help()
        return EXIT_INVALID
    code, text = plan_report()
    print(text, file=sys.stderr if code else sys.stdout)
    return code


if __name__ == "__main__":
    sys.exit(main())
