"""Tests for tests/canary/_spend_guard.py, the hard cap on billed harness calls.

No test here makes a network call. The harness is loaded by path (tests/canary is
not a package), the same way test_consistency_eval.py does it.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from ai_pr_review.llm.base import LLMRequest, LLMResponse
from ai_pr_review.pricing import load_pricing

_CANARY = Path(__file__).resolve().parent.parent / "canary"


def _load(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, _CANARY / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


sg = _load("_spend_guard")
ce = _load("consistency_eval")

_PRICING = load_pricing(str(Path(__file__).resolve().parents[2] / "config" / "model-pricing.json"))
SONNET = "claude-sonnet-5-5"
HAIKU = "claude-haiku-5-5"


def _guard(tmp_path: Path, *, run: float = 5.0, campaign: float = 9.0, label: str = "t") -> Any:
    return sg.SpendGuard(
        run_cap_usd=run, campaign_cap_usd=campaign, state_dir=tmp_path / "state",
        pricing_data=_PRICING, label=label,
    )


def _req(model: str = SONNET, *, user: str = "x" * 400, max_tokens: int = 1000) -> LLMRequest:
    return LLMRequest(model_id=model, system_prompt="s" * 200, user_message=user, max_tokens=max_tokens)


def _resp(**kw: int) -> LLMResponse:
    base = {"input_tokens": 1000, "output_tokens": 500}
    base.update(kw)
    return LLMResponse(text="ok", **base)


def _ledger(tmp_path: Path) -> dict[str, Any]:
    return json.loads((tmp_path / "state" / "ledger.json").read_text())  # type: ignore[no-any-return]


# --- reserve and settle ---


def test_reserve_counts_the_worst_case_then_settle_replaces_it_with_real_usage(tmp_path: Path) -> None:
    g = _guard(tmp_path)
    r = g.reserve(_req())
    assert r.units > 0
    assert _ledger(tmp_path)["spent_units"] == r.units
    actual = g.settle(r, _resp())
    # Sonnet 5.5: $2 in, $10 out per million tokens. 1000 in + 500 out = $0.002 + $0.005.
    assert actual == 70
    assert _ledger(tmp_path)["spent_units"] == 70
    assert g.run_spent_units == 70
    assert actual < r.units


def test_worst_case_prices_full_max_tokens_at_the_output_rate(tmp_path: Path) -> None:
    g = _guard(tmp_path)
    units = g.worst_case_units(_req(max_tokens=32768))
    # 32,768 output tokens at $10 per million is about $0.33, so at least 3,276 units.
    assert units >= 3276


def test_settle_charges_all_four_usage_fields(tmp_path: Path) -> None:
    g = _guard(tmp_path)
    r = g.reserve(_req())
    actual = g.settle(r, _resp(input_tokens=0, output_tokens=0, cache_creation_tokens=1_000_000,
                               cache_read_tokens=1_000_000))
    # $2.50 cache write + $0.10 cache read per million.
    assert actual == 25_000 + 1_000


def test_actual_cost_above_the_reservation_is_still_counted(tmp_path: Path) -> None:
    g = _guard(tmp_path)
    r = g.reserve(_req(user="x", max_tokens=10))
    actual = g.settle(r, _resp(input_tokens=1_000_000, output_tokens=0))
    assert actual == 20_000
    assert _ledger(tmp_path)["spent_units"] == 20_000


def test_response_without_usage_keeps_the_reservation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    g = _guard(tmp_path)
    r = g.reserve(_req())
    kept = g.settle(r, _resp(input_tokens=0, output_tokens=0))
    assert kept == r.units
    assert _ledger(tmp_path)["spent_units"] == r.units
    assert "no usage" in capsys.readouterr().err


# --- caps ---


def test_run_cap_refuses_the_call_that_would_pass_it(tmp_path: Path) -> None:
    g = _guard(tmp_path, run=0.05)
    one = g.worst_case_units(_req())
    assert one < 500  # fits once, as the test assumes
    g.reserve(_req())
    with pytest.raises(sg.SpendCapExceeded, match="run cap"):
        for _ in range(100):
            g.reserve(_req())


def test_campaign_cap_spans_guards_and_runs(tmp_path: Path) -> None:
    first = _guard(tmp_path, run=5.0, campaign=0.05, label="run1")
    first.reserve(_req())
    second = _guard(tmp_path, run=5.0, campaign=0.05, label="run2")  # a new process, same ledger
    with pytest.raises(sg.SpendCapExceeded, match="campaign cap"):
        for _ in range(100):
            second.reserve(_req())


def test_refused_reservation_changes_nothing(tmp_path: Path) -> None:
    g = _guard(tmp_path, run=0.0001, campaign=9.0)
    with pytest.raises(sg.SpendCapExceeded):
        g.reserve(_req(max_tokens=32768))
    assert not (tmp_path / "state" / "ledger.json").exists() or _ledger(tmp_path)["spent_units"] == 0
    assert g.run_spent_units == 0


def test_concurrent_reservations_cannot_both_pass_a_check_only_one_fits(tmp_path: Path) -> None:
    g = _guard(tmp_path)
    unit = g.worst_case_units(_req())
    cap_usd = (unit * 3 + unit // 2) / sg.UNITS_PER_USD  # room for exactly three
    g = _guard(tmp_path / "c", run=cap_usd, campaign=9.0)
    unit = g.worst_case_units(_req())
    ok: list[int] = []
    refused: list[int] = []
    barrier = threading.Barrier(8)

    def worker() -> None:
        barrier.wait()
        try:
            g.reserve(_req())
            ok.append(1)
        except sg.SpendCapExceeded:
            refused.append(1)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(ok) == 3 and len(refused) == 5
    assert g.run_spent_units == 3 * unit


def test_two_guards_on_one_ledger_cannot_overspend_the_campaign(tmp_path: Path) -> None:
    probe = _guard(tmp_path / "p")
    unit = probe.worst_case_units(_req())
    campaign_usd = (unit * 2 + unit // 2) / sg.UNITS_PER_USD
    a = _guard(tmp_path, campaign=campaign_usd, label="a")
    b = _guard(tmp_path, campaign=campaign_usd, label="b")
    results: list[bool] = []
    barrier = threading.Barrier(2)

    def worker(guard: Any) -> None:
        barrier.wait()
        for _ in range(3):
            try:
                guard.reserve(_req())
                results.append(True)
            except sg.SpendCapExceeded:
                results.append(False)

    threads = [threading.Thread(target=worker, args=(g,)) for g in (a, b)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(results) == 2
    assert _ledger(tmp_path)["spent_units"] == 2 * unit


# --- failure handling ---


@pytest.mark.anyio
async def test_a_failed_call_keeps_its_reservation(tmp_path: Path) -> None:
    g = _guard(tmp_path)

    async def boom(_req: LLMRequest) -> LLMResponse:
        raise SystemExit(2)  # llm/client.py maps provider errors to SystemExit

    with pytest.raises(SystemExit):
        await g.call(boom, _req())
    assert _ledger(tmp_path)["spent_units"] == g.worst_case_units(_req())
    assert g.run_spent_units == g.worst_case_units(_req())


@pytest.mark.anyio
async def test_wrap_returns_the_response_and_charges_real_usage(tmp_path: Path) -> None:
    g = _guard(tmp_path)

    async def fake(_req: LLMRequest) -> LLMResponse:
        return _resp()

    response = await g.wrap(fake)(_req())
    assert response.text == "ok"
    assert _ledger(tmp_path)["spent_units"] == 70


def test_unpriced_model_fails_closed_and_changes_nothing(tmp_path: Path) -> None:
    g = _guard(tmp_path)
    with pytest.raises(sg.UnpricedModelError, match="no pricing entry"):
        g.reserve(_req(model="claude-haiku-9-9"))
    assert not (tmp_path / "state" / "ledger.json").exists()


def test_corrupt_ledger_fails_closed_and_is_left_alone(tmp_path: Path) -> None:
    g = _guard(tmp_path)
    ledger = tmp_path / "state" / "ledger.json"
    ledger.write_text("{not json")
    with pytest.raises(sg.LedgerError, match="unreadable"):
        g.reserve(_req())
    assert ledger.read_text() == "{not json"


@pytest.mark.parametrize("bad", ['{"version": 1, "spent_units": -5}', '{"version": 9, "spent_units": 0}',
                                 '["list"]', '{"version": 1, "spent_units": "x"}'])
def test_malformed_ledger_content_fails_closed(tmp_path: Path, bad: str) -> None:
    g = _guard(tmp_path)
    (tmp_path / "state" / "ledger.json").write_text(bad)
    with pytest.raises(sg.LedgerError, match="corrupt"):
        g.reserve(_req())


def test_ledger_survives_a_new_instance_and_uses_the_state_dir_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AI_EVAL_STATE_DIR", str(tmp_path / "env-state"))
    g = sg.SpendGuard.from_env("a")
    g.reserve(_req())
    spent = g.campaign_spent_units()
    again = sg.SpendGuard.from_env("b")
    assert again.campaign_spent_units() == spent > 0
    assert (tmp_path / "env-state" / "ledger.json").exists()


def test_no_temp_files_are_left_behind(tmp_path: Path) -> None:
    g = _guard(tmp_path)
    g.settle(g.reserve(_req()), _resp())
    assert not list((tmp_path / "state").glob("ledger.*.tmp"))


# --- long-prompt tier ---


def test_haiku_reservation_uses_the_dearer_long_prompt_tier(tmp_path: Path) -> None:
    g = _guard(tmp_path)
    req = _req(model=HAIKU, user="x", max_tokens=32768)
    # Long tier output is $2.50 per million: 32,768 tokens is about $0.082, so at least 819 units.
    assert g.worst_case_units(req) >= 819
    # The base tier would give about $0.016.
    assert g.worst_case_units(req) > 200


# --- configuration ---


def test_caps_must_be_positive(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        _guard(tmp_path, run=0)
    with pytest.raises(ValueError):
        _guard(tmp_path, campaign=-1)


def test_cap_env_must_be_a_number(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AI_EVAL_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("AI_EVAL_MAX_COST_USD", "five")
    with pytest.raises(sg.SpendGuardError, match="not a number"):
        sg.SpendGuard.from_env()


def test_default_caps_are_below_ten_dollars(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AI_EVAL_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("AI_EVAL_MAX_COST_USD", raising=False)
    monkeypatch.delenv("AI_EVAL_CAMPAIGN_CAP_USD", raising=False)
    g = sg.SpendGuard.from_env()
    assert g.run_cap_units == 50_000
    assert g.campaign_cap_units == 90_000


# --- pre-flight ---


def test_preflight_requires_approval(tmp_path: Path) -> None:
    g = _guard(tmp_path)
    with pytest.raises(sg.ApprovalRequired, match="--yes"):
        g.preflight(expected_units=100, worst_case_units=500, yes=False, out=lambda _m: None)
    g.preflight(expected_units=100, worst_case_units=500, yes=True, out=lambda _m: None)


def test_preflight_refuses_an_expected_cost_over_the_run_cap(tmp_path: Path) -> None:
    g = _guard(tmp_path, run=1.0)
    with pytest.raises(sg.SpendCapExceeded, match="run cap"):
        g.preflight(expected_units=10_001, worst_case_units=20_000, yes=True, out=lambda _m: None)


def test_preflight_refuses_an_expected_cost_over_what_is_left_of_the_campaign(tmp_path: Path) -> None:
    g = _guard(tmp_path, run=5.0, campaign=1.0)
    g.reserve(_req(max_tokens=32768))
    left = g.campaign_cap_units - g.campaign_spent_units()
    with pytest.raises(sg.SpendCapExceeded, match="campaign cap left"):
        g.preflight(expected_units=left + 1, worst_case_units=left + 1, yes=True, out=lambda _m: None)


def test_preflight_prints_the_estimate(tmp_path: Path) -> None:
    lines: list[str] = []
    _guard(tmp_path).preflight(expected_units=1234, worst_case_units=5678, yes=True, out=lines.append)
    assert "expected $0.1234" in lines[0] and "worst case $0.5678" in lines[0]


# --- commands ---


def test_reset_needs_yes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                         capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("AI_EVAL_STATE_DIR", str(tmp_path))
    sg.SpendGuard.from_env().reserve(_req())
    assert sg.main(["reset"]) == 1
    assert "--yes" in capsys.readouterr().err
    assert sg.SpendGuard.from_env().campaign_spent_units() > 0
    assert sg.main(["reset", "--yes"]) == 0
    assert sg.SpendGuard.from_env().campaign_spent_units() == 0


def test_wants_yes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AI_EVAL_YES", raising=False)
    assert sg.wants_yes(["--yes"]) is True
    assert sg.wants_yes([]) is False
    monkeypatch.setenv("AI_EVAL_YES", "1")
    assert sg.wants_yes([]) is True


# --- the consistency harness is wired to the guard ---


@pytest.fixture
def no_billed_calls(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def forbidden(*_a: object, **_k: object) -> LLMResponse:
        raise AssertionError("a billed call was attempted")

    monkeypatch.setattr(ce, "call_llm", forbidden)
    monkeypatch.setenv("AI_EVAL_STATE_DIR", str(tmp_path / "state"))
    for name in ("AI_EVAL_RUNS", "AI_EVAL_MODELS", "AI_EVAL_AGENTS", "AI_EVAL_ARMS",
                 "AI_EVAL_CORPUS_LIMIT", "AI_EVAL_DIFF", "AI_EVAL_YES"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-not-used")


@pytest.mark.anyio
async def test_the_default_invocation_is_refused(
    no_billed_calls: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["consistency_eval.py", "--yes"])
    assert await ce.main() == 1
    err = capsys.readouterr().err
    assert "REFUSED" in err and "run cap" in err


@pytest.mark.anyio
async def test_a_narrow_run_without_yes_is_refused(
    no_billed_calls: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["consistency_eval.py"])
    monkeypatch.setenv("AI_EVAL_CORPUS_LIMIT", "1")
    monkeypatch.setenv("AI_EVAL_RUNS", "1")
    monkeypatch.setenv("AI_EVAL_AGENTS", "code-reviewer")
    monkeypatch.setenv("AI_EVAL_MODELS", HAIKU)
    assert await ce.main() == 1
    assert "--yes" in capsys.readouterr().err


@pytest.mark.anyio
async def test_dry_run_prints_the_estimate_and_makes_no_call(
    no_billed_calls: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["consistency_eval.py", "--dry-run"])
    monkeypatch.setenv("AI_EVAL_CORPUS_LIMIT", "2")
    monkeypatch.setenv("AI_EVAL_RUNS", "1")
    monkeypatch.setenv("AI_EVAL_AGENTS", "code-reviewer")
    monkeypatch.setenv("AI_EVAL_MODELS", HAIKU)
    monkeypatch.delenv("ANTHROPIC_API_KEY")  # a dry run needs no key
    assert await ce.main() == 0
    out = capsys.readouterr().out
    assert "pre-flight: 2 billed calls" in out and "dry run" in out


@pytest.mark.anyio
async def test_the_harness_llm_call_refuses_without_a_guard(no_billed_calls: None) -> None:
    ce._GUARD = None
    outcome = await ce._one_run(HAIKU, ("code-reviewer",), _CANARY / "corpus" / "11_typescript_type_safety_synthetic.diff",
                                "baseline", ce.REPO_ROOT)
    assert outcome.ok is False
    # The refusal came from the guard, not from the forbidden-call stub.
    assert "spend guard is not initialised" in outcome.detail
    assert "billed call was attempted" not in outcome.detail


def test_an_unpriced_model_is_refused_before_any_call(
    no_billed_calls: None, tmp_path: Path
) -> None:
    g = _guard(tmp_path)
    diff = _CANARY / "corpus" / "11_typescript_type_safety_synthetic.diff"
    with pytest.raises(sg.UnpricedModelError):
        ce._estimate_invocation([diff], 1, ("code-reviewer",), ("baseline",), ("claude-haiku-9-9",), g)


def test_preflight_worst_case_prices_the_long_prompt_tier_like_the_reservation(tmp_path: Path) -> None:
    g = _guard(tmp_path)
    kwargs = {"input_tokens": 3000, "output_tokens": 32768}
    base = g.estimate_units(HAIKU, **kwargs)
    worst = g.estimate_units(HAIKU, worst_case=True, **kwargs)
    assert worst > base  # the long tier is dearer
    # A Sonnet 5.5 call has no tier, so the two agree.
    assert g.estimate_units(SONNET, **kwargs) == g.estimate_units(SONNET, worst_case=True, **kwargs)


@pytest.mark.anyio
async def test_a_settle_failure_keeps_the_response_and_the_reservation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    guard = _guard(tmp_path)

    async def real(_r: LLMRequest) -> LLMResponse:
        return _resp()

    def boom(*_a: Any, **_k: Any) -> int:
        raise sg.LedgerError("lock timeout")

    monkeypatch.setattr(guard, "settle", boom)
    out = await guard.call(real, _req())
    assert out.text == "ok"
    err = capsys.readouterr().err
    assert "succeeded" in err and "lock timeout" in err and "reservation as spent" in err
    assert _ledger(tmp_path)["spent_units"] > 0


def test_a_broken_audit_log_warns_once_and_never_blocks(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    guard = _guard(tmp_path)
    guard._history_path = tmp_path / "no-such-dir" / "history.jsonl"
    guard.reserve(_req())
    guard.reserve(_req())
    err = capsys.readouterr().err
    assert err.count("cannot write the audit log") == 1


def test_settle_over_a_cap_warns_and_the_next_reserve_refuses(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    guard = _guard(tmp_path, run=0.05)
    reservation = guard.reserve(_req(max_tokens=100))
    guard.settle(reservation, _resp(input_tokens=2_000_000, output_tokens=0))
    assert "cap is now exceeded" in capsys.readouterr().err
    with pytest.raises(sg.SpendCapExceeded):
        guard.reserve(_req(max_tokens=100))


@pytest.mark.anyio
async def test_any_settle_failure_keeps_the_billed_response(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    guard = _guard(tmp_path)

    async def real(_r: LLMRequest) -> LLMResponse:
        return _resp()

    def boom(*_a: Any, **_k: Any) -> int:
        raise OSError("disk full")

    monkeypatch.setattr(guard, "settle", boom)
    out = await guard.call(real, _req())
    assert out.text == "ok" and "disk full" in capsys.readouterr().err


def test_dropped_audit_records_are_counted(tmp_path: Path) -> None:
    guard = _guard(tmp_path)
    guard._history_path = tmp_path / "no-such-dir" / "history.jsonl"
    guard.reserve(_req())
    guard.reserve(_req())
    assert guard.history_dropped == 2


def test_without_fcntl_the_guard_refuses_instead_of_running_unlocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sg, "fcntl", None)
    with pytest.raises(sg.LedgerError, match="fcntl"):
        _guard(tmp_path).reserve(_req())


def test_the_lock_timeout_is_long_enough_for_a_busy_ledger() -> None:
    assert sg._LOCK_TIMEOUT_S >= 60


def test_worst_case_counts_the_earlier_turns_and_tool_definitions_of_a_tool_loop(tmp_path: Path) -> None:
    g = _guard(tmp_path)
    plain = g.worst_case_units(_req(max_tokens=1))
    big_result = {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": "y" * 400_000}]}
    tool = {"name": "grep", "description": "d" * 400_000, "input_schema": {"type": "object"}}
    with_history = g.worst_case_units(LLMRequest(
        model_id=SONNET, system_prompt="s" * 200, user_message="x" * 400, max_tokens=1, messages=(big_result,)))
    with_tools = g.worst_case_units(LLMRequest(
        model_id=SONNET, system_prompt="s" * 200, user_message="x" * 400, max_tokens=1, tools=(tool,)))
    assert with_history > plain + 20  # about 100,000 more input tokens at $2 per million
    assert with_tools > plain + 20  # the tool definitions are input tokens too
    # A request with neither field is priced exactly as before.
    assert g.worst_case_units(LLMRequest(model_id=SONNET, system_prompt="s" * 200, user_message="x" * 400,
                                         max_tokens=1)) == plain
