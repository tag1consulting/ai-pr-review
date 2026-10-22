"""Tests for tests/e2e/fork_scenarios.py and the drafted test-repo files. No network calls."""

from __future__ import annotations

import dataclasses
import json
import re
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from ai_pr_review.analyzers.bridge import FORK_SKIPPED_ANALYZERS
from ai_pr_review.review.fork_budget import try_parse_entry
from ai_pr_review.review.fork_spend import parse_spend_entry
from tests.e2e import fork_scenarios as fs
from tests.e2e.fork_repo_files import e2e_ledger_tools as tools
from tests.e2e.run_e2e import cli

FILES = Path(fs.REPO_FILES_DIR)


def _replace(table, sid, **changes):
    return tuple(dataclasses.replace(s, **changes) if s.id == sid else s for s in table)


# --- the table ------------------------------------------------------------------


def test_default_table_is_valid_and_covers_s1_to_s17():
    assert fs.validate_table() == []
    assert {s.id for s in fs.SCENARIOS} == set(fs.EXPECTED_IDS)


def test_plan_report_ok_and_reports_cost_totals():
    code, text = fs.plan_report()
    assert code == fs.EXIT_PASS
    assert "nominal=6" in text and "worst=8" in text
    assert "WARNING" in text  # the worst case passes the 4 USD design budget


def test_missing_verifier_is_an_error():
    table = _replace(fs.SCENARIOS, "S5", outcome_key="S99")
    assert any("no verifier named S99" in e for e in fs.validate_table(table))


def test_missing_scenario_is_an_error():
    assert any("differ" in e for e in fs.validate_table(tuple(s for s in fs.SCENARIOS if s.id != "S9")))


def test_zero_cost_with_llm_runs_is_an_error():
    table = _replace(fs.SCENARIOS, "S7", llm_runs_nominal=1, llm_runs_worst=1)
    assert any("zero-cost" in e for e in fs.validate_table(table))


def test_unknown_action_and_missing_argument():
    bad = (fs.Step("explode"), fs.Step("set_var", {"name": "X"}))
    errors = fs.validate_table(_replace(fs.SCENARIOS, "S5", setup=bad))
    assert any("unknown action" in e for e in errors)
    assert any("lacks" in e for e in errors)


def test_unrestored_variable_workflow_and_lock_are_errors():
    errors = fs.validate_table(_replace(fs.SCENARIOS, "S2", teardown=()))
    assert any("var AI_FORK_POLICY is not restored" in e for e in errors)
    assert any("workflow" in e for e in fs.validate_table(_replace(fs.SCENARIOS, "S1", teardown=())))
    assert any("lock" in e for e in fs.validate_table(_replace(fs.SCENARIOS, "S13", teardown=())))


def test_order_rules():
    reordered = tuple(sorted(fs.SCENARIOS, key=lambda s: s.id == "S9"))  # S9 last, after S10
    assert any("S9 must come before S10" in e for e in fs.validate_table(reordered))
    s16_first = tuple(sorted(fs.SCENARIOS, key=lambda s: s.id != "S16"))
    assert any("S3 must come before S16" in e for e in fs.validate_table(s16_first))


def test_s3_must_be_the_first_review_on_its_pr():
    table = _replace(fs.SCENARIOS, "S8", pr="s3")
    assert any("not the first review" in e for e in fs.validate_table(table))


def test_author_cap_simulation_flags_leftover_prs_and_extra_open():
    leftover = _replace(fs.SCENARIOS, "S1", setup=tuple(
        s for s in fs.SCENARIOS[0].setup if s.action != "close_pr"))
    errors = fs.validate_table(leftover)
    assert any("S12: starts with open fork PRs" in e for e in errors)
    assert any("left open" in e for e in errors)


def test_clear_ledgers_must_keep_the_spend_issue():
    bad = (fs.Step("clear_ledgers", {"keep_issue": False}),)
    assert any("keep the spend issue" in e for e in fs.validate_table(_replace(fs.SCENARIOS, "S3", setup=bad)))


def test_only_s10_needs_a_second_maintainer():
    assert [s.id for s in fs.SCENARIOS if s.needs_second_maintainer] == ["S10"]


def test_s3_diff_paths_reach_every_fork_skipped_analyzer():
    assert fs.expected_skipped_for_paths(fs.S3_DIFF_PATHS) == sorted(FORK_SKIPPED_ANALYZERS)


def test_s17_style_diff_names_only_semgrep():
    """The design's S17 diff (php, terraform, python) leaves most skipped analyzers out of the notice."""
    paths = ("src/a.php", "infra/main.tf", "tools/r.py")
    assert fs.expected_skipped_for_paths(paths) == ["semgrep"]


def test_plan_cli_module_and_run_e2e_profile(capsys):
    assert fs.main(["--plan"]) == 0
    assert "plan: OK" in capsys.readouterr().out
    result = CliRunner().invoke(cli, ["plan", "--profile", "fork"])
    assert result.exit_code == 0
    assert "profile=fork" in result.output


def test_release_profile_is_unchanged():
    result = CliRunner().invoke(cli, ["plan", "--profile", "release", "--platforms", "github"])
    assert result.exit_code == 2


# --- drafted workflows -----------------------------------------------------------


def _load(name: str) -> dict:
    loaded = yaml.safe_load((FILES / name).read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _triggers(wf: dict) -> dict:
    return wf.get("on") or wf[True]  # PyYAML reads the bare key `on` as True


@pytest.mark.parametrize("name", ["ai-pr-review-fork.yml", "e2e-ledger-tools.yml"])
def test_no_expression_inside_run_scripts(name):
    wf = _load(name)
    for job in wf["jobs"].values():
        for step in job["steps"]:
            assert "${{" not in step.get("run", ""), step.get("name")


@pytest.mark.parametrize("name", ["ai-pr-review-fork.yml", "e2e-ledger-tools.yml"])
def test_third_party_actions_pinned_to_a_sha_except_the_engine_placeholder(name):
    for job in _load(name)["jobs"].values():
        for step in job["steps"]:
            uses = step.get("uses", "")
            if uses and "__ENGINE_REF__" not in uses:
                assert re.search(r"@[0-9a-f]{40}( |$)", uses), uses


def test_fork_workflow_shape():
    wf = _load("ai-pr-review-fork.yml")
    assert list(_triggers(wf)) == ["pull_request_target"]
    job = wf["jobs"]["review"]
    assert "head.repo.full_name != github.repository" in job["if"]
    assert job["concurrency"]["group"].endswith("${{ github.event.pull_request.number }}")
    checkout = job["steps"][0]
    assert checkout["with"]["ref"] == "${{ github.event.pull_request.head.sha }}"
    assert "repository" not in checkout["with"]  # origin must stay the base repository (trusted policy)
    assert checkout["with"]["persist-credentials"] == "${{ vars.E2E_PERSIST_CREDENTIALS == 'true' }}"
    review = next(s for s in job["steps"] if "container-action" in s.get("uses", ""))
    assert review["uses"].endswith("@__ENGINE_REF__")
    assert "OWNER,COLLABORATOR" in review["env"]["AI_FORK_TRUSTED_ASSOCIATIONS"]
    assert "AI_FORK_POLICY" in review["env"]
    assert wf["permissions"] == {"contents": "read", "issues": "write", "pull-requests": "write"}
    assert any(s.get("if") == "always()" for s in job["steps"])


def test_tools_workflow_options_match_the_scenario_actions():
    options = set(_triggers(_load("e2e-ledger-tools.yml"))["workflow_dispatch"]["inputs"]["action"]["options"])
    assert options == set(fs.TOOL_ACTIONS.values())


@pytest.mark.skipif(shutil.which("actionlint") is None, reason="actionlint is not installed")
def test_actionlint_passes_on_the_drafts():
    files = [str(FILES / "ai-pr-review-fork.yml"), str(FILES / "e2e-ledger-tools.yml")]
    done = subprocess.run(["actionlint", *files], capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stdout + done.stderr


# --- helper script ------------------------------------------------------------------


def test_ledger_bodies_use_the_engine_format_and_sum_exactly():
    bodies = tools.ledger_bodies(3, 100)
    entries = [try_parse_entry(i, b) for i, b in enumerate(bodies)]
    assert all(e is not None for e in entries)
    assert sorted(e.cents for e in entries if e) == [33, 33, 34]
    assert all((e.runs, e.allowance) == (1, 0) for e in entries if e)


def test_spend_body_parses_with_the_engine_parser():
    now = datetime(2026, 10, 22, tzinfo=UTC)
    entry = parse_spend_entry(1, tools.spend_body("shared", 1500, 0, now))
    assert (entry.tier, entry.cents, entry.state, entry.ts) == ("shared", 1500, "settled", now)


@pytest.mark.parametrize("bad", ["5", "5.9", "abc", "-1.00", "1.234"])
def test_parse_usd_rejects_bad_values(bad):
    with pytest.raises(ValueError):
        tools.parse_usd(bad)


def test_parse_usd_and_bad_inputs():
    assert tools.parse_usd("5.99") == 599
    with pytest.raises(ValueError):
        tools.ledger_bodies(0, 10)
    with pytest.raises(ValueError):
        tools.spend_body("other", 1, 0, datetime.now(UTC))


# --- hostile content ---------------------------------------------------------------


def test_hostile_files_are_inert_and_listed_in_the_manifest():
    manifest = json.loads((FILES / "hostile" / "MANIFEST.json").read_text(encoding="utf-8"))
    for src in manifest["files"]:
        assert src.endswith(".txt") and (FILES / "hostile" / src).is_file()
    targets = set(manifest["files"].values())
    assert set(fs.S3_DIFF_PATHS) <= targets
    assert set(manifest["canary_files"]) | set(manifest["config_only_files"]) <= targets
    assert not list((FILES / "hostile").glob("*.php")) + list((FILES / "hostile").glob("*.js"))


def test_pr_body_template_carries_every_hostile_element():
    body = (FILES / "hostile" / "pr_body.md.tmpl").read_text(encoding="utf-8")
    for needle in ("E2E-BODY-CANARY-{RUN_ID}", "](https://evil.example", "![", "@gchaix-team", "other-repo#1"):
        assert needle in body


def test_canary_files_write_the_expected_marker_name():
    for name in ("vendor__autoload.php.txt", "eslint.config.js.txt"):
        assert "/workspace/E2E-PWNED-" in (FILES / "hostile" / name).read_text(encoding="utf-8")


def test_no_real_secret_shape_in_the_drafts():
    from tests.e2e.fork_verify import text_problems

    for path in FILES.rglob("*"):
        if path.is_file() and path.suffix in {".yml", ".txt", ".tmpl", ".json"}:
            assert not [p for p in text_problems(path.read_text(encoding="utf-8"), "x/y") if "secret" in p], path
