"""Tests for the precision eval harness and the response cache.

No test makes a network call.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest

from ai_pr_review.findings.models import Finding
from ai_pr_review.llm.base import LLMRequest, LLMResponse

_CANARY = Path(__file__).resolve().parent.parent / "canary"


def _load(name: str) -> Any:
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _CANARY / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


sys.path.insert(0, str(_CANARY))
cache_mod = _load("_llm_cache")
pe = _load("precision_eval")


def _req(**kw: Any) -> LLMRequest:
    base: dict[str, Any] = {"model_id": "claude-sonnet-5-5", "system_prompt": "sys", "user_message": "diff"}
    base.update(kw)
    return LLMRequest(**base)


def _resp(text: str = "ok") -> LLMResponse:
    return LLMResponse(text=text, input_tokens=100, output_tokens=20, cache_creation_tokens=5,
                       cache_read_tokens=7, stop_reason="end_turn", thinking_tokens=3)


def _finding(file: str = "a.py", line: int | None = 10, category: str = "edge-case",
             text: str = "unchecked value can be None") -> Finding:
    return Finding(severity="High", confidence=90, finding=text, file=file, line=line,
                   category=category)  # type: ignore[arg-type]


# --- cache key ---


def test_key_changes_with_every_field_that_can_change_the_answer() -> None:
    base = cache_mod.request_key(_req())
    changed = [
        _req(model_id="claude-haiku-5-5"), _req(system_prompt="other"), _req(user_message="other"),
        _req(max_tokens=1), _req(temperature=0.9), _req(system_prefix="x"), _req(cache_blocks=("a",)),
    ]
    keys = {cache_mod.request_key(r) for r in changed}
    assert base not in keys and len(keys) == len(changed)
    assert cache_mod.request_key(_req(), "anthropic") != base  # the namespace is part of the key


def test_key_ignores_the_prompt_caching_flag() -> None:
    assert cache_mod.request_key(_req(prompt_caching=True)) == cache_mod.request_key(_req())


# --- cache behavior ---


@pytest.mark.anyio
async def test_auto_mode_calls_once_then_replays(tmp_path: Path) -> None:
    cache = cache_mod.LLMCache(cache_dir=tmp_path, mode="auto")
    calls: list[str] = []

    async def real(_r: LLMRequest) -> LLMResponse:
        calls.append("x")
        return _resp("first")

    first = await cache.call(real, _req())
    second = await cache.call(real, _req())
    assert calls == ["x"] and first.text == second.text == "first"
    assert second.input_tokens == 100 and second.cache_read_tokens == 7 and second.thinking_tokens == 3
    assert (cache.stats.hits, cache.stats.misses, cache.stats.saved) == (1, 1, 1)


@pytest.mark.anyio
async def test_a_changed_prompt_is_a_miss(tmp_path: Path) -> None:
    cache = cache_mod.LLMCache(cache_dir=tmp_path, mode="auto")
    calls: list[str] = []

    async def real(r: LLMRequest) -> LLMResponse:
        calls.append(r.system_prompt)
        return _resp(r.system_prompt)

    await cache.call(real, _req(system_prompt="v1"))
    second = await cache.call(real, _req(system_prompt="v2"))
    assert calls == ["v1", "v2"] and second.text == "v2"


@pytest.mark.anyio
async def test_replay_mode_never_calls_and_a_miss_is_an_error(tmp_path: Path) -> None:
    cache = cache_mod.LLMCache(cache_dir=tmp_path, mode="replay")

    async def real(_r: LLMRequest) -> LLMResponse:
        raise AssertionError("a call was made in replay mode")

    with pytest.raises(cache_mod.CacheMiss, match="no saved response"):
        await cache.call(real, _req())


@pytest.mark.anyio
async def test_replay_mode_serves_what_auto_mode_saved(tmp_path: Path) -> None:
    async def real(_r: LLMRequest) -> LLMResponse:
        return _resp("saved")

    await cache_mod.LLMCache(cache_dir=tmp_path, mode="auto").call(real, _req())

    async def forbidden(_r: LLMRequest) -> LLMResponse:
        raise AssertionError("a call was made in replay mode")

    out = await cache_mod.LLMCache(cache_dir=tmp_path, mode="replay").call(forbidden, _req())
    assert out.text == "saved"


@pytest.mark.anyio
async def test_record_mode_calls_even_on_a_hit_and_overwrites(tmp_path: Path) -> None:
    texts = iter(["old", "new"])

    async def real(_r: LLMRequest) -> LLMResponse:
        return _resp(next(texts))

    await cache_mod.LLMCache(cache_dir=tmp_path, mode="auto").call(real, _req())
    await cache_mod.LLMCache(cache_dir=tmp_path, mode="record").call(real, _req())
    out = await cache_mod.LLMCache(cache_dir=tmp_path, mode="replay").call(real, _req())
    assert out.text == "new"


@pytest.mark.anyio
async def test_a_failed_call_saves_nothing(tmp_path: Path) -> None:
    cache = cache_mod.LLMCache(cache_dir=tmp_path, mode="auto")

    async def boom(_r: LLMRequest) -> LLMResponse:
        raise SystemExit(2)

    with pytest.raises(SystemExit):
        await cache.call(boom, _req())
    assert cache.get(_req()) is None


def test_a_corrupt_entry_is_an_error_not_a_miss(tmp_path: Path) -> None:
    cache = cache_mod.LLMCache(cache_dir=tmp_path, mode="auto")
    key = cache_mod.request_key(_req())
    path = tmp_path / key[:2] / f"{key}.json"
    path.parent.mkdir(parents=True)
    path.write_text("{bad")
    with pytest.raises(cache_mod.CacheError):
        cache.get(_req())


def test_an_entry_under_the_wrong_key_is_rejected(tmp_path: Path) -> None:
    cache = cache_mod.LLMCache(cache_dir=tmp_path, mode="auto")
    key = cache_mod.request_key(_req())
    path = tmp_path / key[:2] / f"{key}.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"version": 1, "key": "0" * 64}))
    with pytest.raises(cache_mod.CacheError, match="unreadable"):
        cache.get(_req())


def test_unknown_mode_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        cache_mod.LLMCache(cache_dir=tmp_path, mode="sometimes")


# --- matching and scoring ---


def _label(file: str = "a.py", a: int = 10, b: int = 12, cat: str = "edge-case",
           summary: str = "unchecked value can be None") -> Any:
    return pe.Label(file=file, line_start=a, line_end=b, category=cat, summary=summary)


def test_match_needs_the_same_file() -> None:
    assert pe.matches(_finding(file="a.py"), _label(file="a.py"))
    assert pe.matches(_finding(file="src/a.py"), _label(file="a.py"))
    assert not pe.matches(_finding(file="b.py"), _label(file="a.py"))


def test_same_file_strips_only_a_literal_dot_slash_prefix() -> None:
    assert pe._same_file("./a.py", "a.py")
    assert pe._same_file("/a.py", "a.py")
    assert not pe._same_file(".github/workflows/x.yml", "github/workflows/x.yml")


def test_a_finding_without_a_line_never_matches() -> None:
    assert not pe.matches(_finding(line=None), _label())
    s = pe.score_fixture([_finding(line=None)], _fixture([_label()]))
    assert s.bugs_found == 0 and len(s.unlabeled) == 1


def test_match_allows_a_line_window_of_five() -> None:
    assert pe.matches(_finding(line=17), _label(a=10, b=12))
    assert not pe.matches(_finding(line=18), _label(a=10, b=12))
    assert pe.matches(_finding(line=5), _label(a=10, b=12))
    assert not pe.matches(_finding(line=4), _label(a=10, b=12))


def test_match_needs_the_same_category_or_shared_words() -> None:
    other_words = "completely different wording about naming style"
    assert pe.matches(_finding(category="edge-case", text=other_words), _label(cat="edge-case"))
    assert pe.matches(_finding(category="other", text="unchecked value can be None here"), _label(cat="edge-case"))
    assert not pe.matches(_finding(category="other", text=other_words), _label(cat="edge-case"))


def _fixture(bugs: list[Any], accepted: list[Any] | None = None, clean: bool = False) -> Any:
    return pe.Fixture(name="f", diff_path=Path("f.diff"), clean=clean, source="t", evidence="strong",
                      verified_by="t", bugs=bugs, accepted_extra=accepted or [])


def test_score_counts_hits_accepted_and_unlabeled() -> None:
    fx = _fixture([_label()], [_label(a=40, b=41, cat="lint", summary="style nit")])
    findings = [_finding(line=11), _finding(line=40, category="lint", text="style nit"),
                _finding(file="z.py", line=1)]
    s = pe.score_fixture(findings, fx)
    assert (s.bugs_found, s.true_positive_findings, s.accepted_findings, len(s.unlabeled)) == (1, 1, 1, 1)
    assert s.missed == []


def test_score_reports_a_missed_bug() -> None:
    s = pe.score_fixture([], _fixture([_label()]))
    assert s.bugs_found == 0 and s.missed == [_label()]


def test_two_findings_on_one_bug_find_it_once_but_both_are_true_positives() -> None:
    s = pe.score_fixture([_finding(line=10), _finding(line=11)], _fixture([_label()]))
    assert s.bugs_found == 1 and s.true_positive_findings == 2


def test_every_finding_on_a_clean_fixture_is_unlabeled() -> None:
    s = pe.score_fixture([_finding()], _fixture([], clean=True))
    assert len(s.unlabeled) == 1


def test_summary_precision_and_recall() -> None:
    scores = [
        pe.score_fixture([_finding(line=10), _finding(file="x.py")], _fixture([_label(), _label(a=50, b=51)])),
        pe.score_fixture([], _fixture([], clean=True)),
    ]
    s = pe.summarize(scores)
    assert s.bugs_total == 2 and s.bugs_found == 1 and s.true_positives == 1 and s.unlabeled == 1
    assert s.recall[0] == 0.5 and s.precision[0] == 0.5


def test_wilson_interval_values() -> None:
    lo, hi = pe.wilson(5, 10)
    assert round(lo, 3) == 0.237 and round(hi, 3) == 0.763
    assert pe.wilson(0, 0) == (0.0, 1.0)
    lo, hi = pe.wilson(10, 10)
    assert hi == 1.0 and lo > 0.69
    assert pe.wilson(0, 10)[0] == 0.0


def test_report_lists_unlabeled_and_missed() -> None:
    s = pe.score_fixture([_finding(file="z.py", line=3)], _fixture([_label()]))
    text = pe.format_report([s])
    assert "z.py:3" in text and "MISSED" in text and "recall:" in text and "precision:" in text


# --- the real corpus ---


def _added_lines(diff: str) -> dict[str, set[int]]:
    added: dict[str, set[int]] = {}
    current = ""
    line_no = 0
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            current = line[6:]
            added.setdefault(current, set())
        elif line.startswith("+++ "):
            current = ""
        elif m := re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)", line):
            line_no = int(m.group(1))
        elif line.startswith("+") and current:
            added[current].add(line_no)
            line_no += 1
        elif line.startswith("-"):
            continue
        elif current:
            line_no += 1
    return added


def test_corpus_loads_and_has_the_planned_shape() -> None:
    fixtures = pe.load_fixtures()
    assert len(fixtures) >= 20
    assert sum(1 for f in fixtures if f.clean) >= 8
    assert sum(len(f.bugs) for f in fixtures) >= 15


def test_every_labeled_bug_lies_on_added_lines_of_its_diff() -> None:
    """Catches a wrong line range or file in a label (the seeded fixtures use
    new-file diffs, where file line = diff line - 6)."""
    problems = []
    for fx in pe.load_fixtures():
        added = _added_lines(fx.diff_path.read_text())
        for bug in fx.bugs:
            lines = added.get(bug.file, set())
            if not any(bug.line_start <= n <= bug.line_end for n in lines):
                problems.append(f"{fx.name}: {bug.file}:{bug.line_start}-{bug.line_end} touches no added line")
    assert not problems, "\n".join(problems)


def test_every_fixture_is_small_enough_for_a_cheap_eval() -> None:
    assert all(f.diff_path.stat().st_size <= 10_000 for f in pe.load_fixtures())


def test_a_fixture_without_labels_is_an_error(tmp_path: Path) -> None:
    (tmp_path / "x.diff").write_text("diff")
    with pytest.raises(ValueError, match="no x.labels.json"):
        pe.load_fixtures(tmp_path)


def test_a_clean_fixture_cannot_list_bugs_and_a_buggy_one_needs_bugs(tmp_path: Path) -> None:
    (tmp_path / "a.diff").write_text("d")
    (tmp_path / "a.labels.json").write_text(json.dumps({"clean": True, "bugs": [
        {"file": "a", "line_start": 1, "line_end": 1, "category": "other", "summary": "s"}]}))
    with pytest.raises(ValueError, match="cannot list bugs"):
        pe.load_fixtures(tmp_path)
    (tmp_path / "a.labels.json").write_text(json.dumps({"clean": False, "bugs": []}))
    with pytest.raises(ValueError, match="must set clean=true"):
        pe.load_fixtures(tmp_path)


# --- the entrypoint ---


@pytest.fixture
def state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("AI_EVAL_STATE_DIR", str(tmp_path / "state"))
    for name in ("AI_EVAL_MODELS", "AI_EVAL_AGENTS", "AI_EVAL_CACHE_MODE", "AI_EVAL_YES"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "unused")
    return tmp_path / "state"


@pytest.mark.anyio
async def test_dry_run_prints_an_estimate_and_makes_no_call(
    state: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert await pe.main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "pre-flight: up to" in out and "expected $" in out and "dry run" in out


@pytest.mark.anyio
async def test_a_live_run_without_yes_is_refused(
    state: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert await pe.main([]) == 1
    assert "--yes" in capsys.readouterr().err


@pytest.mark.anyio
async def test_replay_mode_with_an_empty_cache_fails_without_a_key_or_a_call(
    state: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("AI_EVAL_CACHE_MODE", "replay")
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    assert await pe.main([]) == 1
    err = capsys.readouterr().err
    assert "no saved response" in err


@pytest.mark.anyio
async def test_the_default_estimate_is_well_under_the_run_cap(
    state: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    await pe.main(["--dry-run"])
    line = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("spend guard:")][0]
    expected = float(re.search(r"expected \$([0-9.]+)", line).group(1))  # type: ignore[union-attr]
    assert expected < 2.0


# --- harvest script (pure helpers, no network) ---

harvest = _load("harvest_labels")


def test_harvest_decodes_the_finding_marker() -> None:
    import base64

    payload = base64.b64encode(json.dumps({"sev": "High", "cat": "edge-case", "conf": 80}).encode()).decode()
    data = harvest.decode_marker(f"text\n<!-- ai-pr-review-finding:{payload} -->")
    assert data == {"sev": "High", "cat": "edge-case", "conf": 80}
    assert harvest.decode_marker("no marker") is None
    assert harvest.decode_marker("<!-- ai-pr-review-finding:!!!! -->") is None


def test_harvest_finds_a_dismiss_command() -> None:
    assert harvest.dismiss_command(["thanks", "/ai-pr-review false-positive not a bug"]) == "false-positive"
    assert harvest.dismiss_command(["/ai-pr-review rescan"]) is None


def test_harvest_patch_touches_the_cited_line() -> None:
    patch = "@@ -10,3 +20,4 @@\n a\n+b\n"
    assert harvest.patch_touches(patch, 22)
    assert harvest.patch_touches(patch, 17)
    assert not harvest.patch_touches(patch, 5)
    assert not harvest.patch_touches("", 22)


def test_harvest_refuses_a_repo_that_is_not_allowed(capsys: pytest.CaptureFixture[str]) -> None:
    assert harvest.main(["--repo", "client-org/private-repo"]) == 1
    assert "not an allowed repository" in capsys.readouterr().err


# --- judge eval scoring (pure, no network) ---

je = _load("judge_eval")


def _judged(fx: Any, findings: list[Finding], *, demote: set[int] | None = None) -> list[Finding]:
    return [f.model_copy(update={"demoted_to_body": i in (demote or set()), "judge_verdict": "downrank" if i in (demote or set()) else "keep"})
            for i, f in enumerate(findings)]


def test_judge_eval_counts_inline_precision_and_demoted_real_findings() -> None:
    fx = _fixture([_label()])
    findings = [_finding(line=11), _finding(file="z.py", line=1)]
    none = je.score_condition("none", [(fx, findings)])
    assert (none.inline_matched, none.inline_unlabeled, none.demoted_real) == (1, 1, 0)
    assert none.inline_precision[0] == 0.5 and none.bugs_inline == 1
    # Demoting the unlabeled finding raises inline precision and loses no real bug.
    good = je.score_condition("c", [(fx, _judged(fx, findings, demote={1}))])
    assert good.inline_precision[0] == 1.0 and good.demoted_real == 0 and good.bugs_inline == 1
    # Demoting the real finding lowers bugs inline and counts as a demoted real finding.
    bad = je.score_condition("c", [(fx, _judged(fx, findings, demote={0}))])
    assert bad.demoted_real == 1 and bad.bugs_inline == 0 and bad.inline_precision[0] == 0.0


def test_judge_eval_rejects_an_unknown_condition() -> None:
    with pytest.raises(ValueError, match="unknown condition"):
        je._selected("code-sonnet,nonsense")
    assert [c.name for c in je._selected("")] == ["text-sonnet", "code-sonnet", "text-haiku", "code-haiku", "v2-sonnet"]


class _FailingGuard:
    """Stands in for the spend guard: every billed call raises the given error."""

    def __init__(self, error: Exception) -> None:
        self._error = error

    async def call(self, llm_call: Any, request: Any) -> Any:
        raise self._error


@pytest.mark.anyio
async def test_judge_eval_does_not_hide_a_cache_miss_behind_the_fail_soft_judge(tmp_path: Path) -> None:
    """judge_findings catches every call error and returns the findings unchanged. In the eval that
    would turn a replay-mode cache miss into a normal-looking report, so run_conditions must raise."""
    fx = _fixture([_label()])
    cache = cache_mod.LLMCache(cache_dir=tmp_path, mode="replay")
    with pytest.raises(cache_mod.CacheMiss):
        await je.run_conditions([(fx, [_finding()])], [je.CONDITIONS[0]], cache, None)


@pytest.mark.anyio
async def test_judge_eval_turns_a_failed_call_into_an_error_not_a_quiet_zero(tmp_path: Path) -> None:
    fx = _fixture([_label()])
    cache = cache_mod.LLMCache(cache_dir=tmp_path, mode="auto")
    with pytest.raises(RuntimeError, match="the judge call failed, so its score would be wrong"):
        await je.run_conditions([(fx, [_finding()])], [je.CONDITIONS[0]], cache, _FailingGuard(ValueError("boom")))


# --- corpus override and the fixture miner (pure helpers, no network) ---

mf = _load("mine_fixtures")


def test_corpus_dir_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / "a.diff").write_text("d")
    (tmp_path / "a.labels.json").write_text(json.dumps({"bugs": [
        {"file": "f.py", "line_start": 1, "line_end": 2, "category": "edge-case", "summary": "s"}]}))
    monkeypatch.setenv("AI_EVAL_CORPUS_DIR", str(tmp_path))
    assert [f.name for f in pe.load_fixtures()] == ["a"]
    monkeypatch.delenv("AI_EVAL_CORPUS_DIR")
    assert pe.resolve_corpus_dir() == pe.CORPUS_DIR


def test_miner_reads_removed_ranges_from_a_u0_patch() -> None:
    patch = "@@ -10,3 +10,2 @@\n-a\n@@ -20 +19,0 @@\n-b\n@@ -30,0 +30,2 @@\n+c\n"
    assert mf.removed_ranges(patch) == [(10, 3), (20, 1)]


def test_miner_parses_blame_porcelain() -> None:
    sha = "a" * 40
    other = "b" * 40
    porcelain = f"{sha} 7 12 1\nauthor x\n\tcode\n{other} 3 13\n\tmore\n"
    assert mf.parse_blame(porcelain) == [(sha, 7), (other, 3)]


def test_miner_added_line_numbers_and_summary() -> None:
    diff = "diff --git a/f b/f\n--- a/f\n+++ b/f\n@@ -1,2 +5,3 @@\n ctx\n+new\n-old\n+new2\n"
    assert mf.added_line_numbers(diff) == {6, 7}
    assert mf.summary_of("fix: subject\n\nFirst paragraph\nwraps here.\n\nSecond.\n\nThird.") == (
        "fix: subject First paragraph wraps here.")
    assert len(mf.summary_of("x " * 500)) <= mf.MAX_SUMMARY_CHARS


def test_miner_writes_labels_the_loader_accepts(tmp_path: Path) -> None:
    m = mf.Mined("abc123def", "fff000fff", "ai_pr_review/x.py", 5, 7, "fix: it", "diff --git a/f b/f\n")
    assert mf.write([m, m], tmp_path) == 1
    fixtures = pe.load_fixtures(tmp_path)
    assert len(fixtures) == 1 and fixtures[0].source == "mined" and fixtures[0].evidence == "weak"
    assert fixtures[0].bugs[0].line_start == 5


def test_fixture_slice() -> None:
    items = list(range(10))
    assert pe.apply_slice(items, "") == items  # type: ignore[arg-type]
    assert pe.apply_slice(items, "2:5") == [2, 3, 4]  # type: ignore[arg-type]
    assert pe.apply_slice(items, "7:") == [7, 8, 9]  # type: ignore[arg-type]
    assert pe.apply_slice(items, ":3") == [0, 1, 2]  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="0:35"):
        pe.apply_slice(items, "abc")


# --- review follow-ups: harvest errors, miner failures, empty slice ---


def test_harvest_summary_reports_failed_compare_calls() -> None:
    harvest.COMPARE_FAILURES.clear()
    rows = [{"outcome": "untouched"}]
    assert "compare call" not in harvest.summarize(rows)
    harvest.COMPARE_FAILURES.append("repo a...b: boom")
    assert "1 compare call(s) failed" in harvest.summarize(rows)
    harvest.COMPARE_FAILURES.clear()


def test_harvest_one_bad_pr_does_not_lose_the_others(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    def fake_gh(path: str) -> Any:
        if "pulls?state=closed" in path:
            return [{"number": 1, "merged_at": "x"}, {"number": 2, "merged_at": "x"}]
        raise AssertionError(path)

    def fake_pr(repo: str, pr: dict[str, Any]) -> list[dict[str, Any]]:
        if pr["number"] == 1:
            raise subprocess.CalledProcessError(1, "gh", stderr="rate limit")
        return [{"pr": pr["number"], "outcome": "untouched"}]

    monkeypatch.setattr(harvest, "_gh", fake_gh)
    monkeypatch.setattr(harvest, "_harvest_pr", fake_pr)
    assert harvest.harvest_repo("tag1consulting/ai-pr-review", 10) == [{"pr": 2, "outcome": "untouched"}]


def test_miner_records_a_tolerated_git_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    mf.GIT_FAILURES.clear()
    assert mf._git("rev-parse", "--verify", "--quiet", "no-such-ref-xyz", check=False) == ""
    assert mf.GIT_FAILURES and "rev-parse" in mf.GIT_FAILURES[0]
    mf.GIT_FAILURES.clear()


def test_miner_default_ref_falls_back_to_head(monkeypatch: pytest.MonkeyPatch) -> None:
    assert mf.default_ref() in ("origin/main", "HEAD")


@pytest.mark.anyio
async def test_empty_slice_is_not_reported_as_an_empty_corpus(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("AI_EVAL_FIXTURE_SLICE", "500:600")
    assert await pe.main(["--dry-run"]) == 1
    err = capsys.readouterr().err
    assert "selects none of the" in err and "no fixtures in" not in err


def test_harvest_skips_a_malformed_pr_payload_and_shows_the_stderr(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import subprocess

    def fake_gh(path: str) -> Any:
        return [{"number": n, "merged_at": "x"} for n in (1, 2, 3)]

    def fake_pr(repo: str, pr: dict[str, Any]) -> list[dict[str, Any]]:
        if pr["number"] == 1:
            raise KeyError("body")
        if pr["number"] == 2:
            raise subprocess.CalledProcessError(1, "gh", stderr="API rate limit exceeded")
        return [{"pr": pr["number"], "outcome": "untouched"}]

    monkeypatch.setattr(harvest, "_gh", fake_gh)
    monkeypatch.setattr(harvest, "_harvest_pr", fake_pr)
    assert harvest.harvest_repo("tag1consulting/ai-pr-review", 10) == [{"pr": 3, "outcome": "untouched"}]
    err = capsys.readouterr().err
    assert "KeyError" in err and "API rate limit exceeded" in err


# --- push-level recall corpus (#1088) ---

_PUSH_CORPUS = _CANARY / "eval_corpus_pushes"
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", re.MULTILINE)


def _changed_ranges(diff: str) -> dict[str, list[tuple[int, int]]]:
    """New-file line ranges of every hunk, by file, so a label can be checked."""
    out: dict[str, list[tuple[int, int]]] = {}
    current: str | None = None
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            current = line[6:]
            out[current] = []
        m = _HUNK.match(line)
        if m and current is not None:
            start, count = int(m.group(1)), int(m.group(2) or 1)
            out[current].append((start, start + max(count, 1) - 1))
    return out


def test_push_corpus_loads_and_every_fixture_has_a_verified_bug() -> None:
    fixtures = pe.load_fixtures(_PUSH_CORPUS)
    assert len(fixtures) == 9
    for f in fixtures:
        assert not f.clean and f.bugs, f.name
        assert f.source == "known-fix" and f.evidence == "strong", f.name
        assert "claude[bot]" in f.verified_by and "fixed in" in f.verified_by, f.name


def test_every_push_label_overlaps_a_changed_hunk_of_its_diff() -> None:
    """A label on code the push did not change cannot be found from the diff, so
    it would count as a miss that the reviewer could never avoid."""
    for f in pe.load_fixtures(_PUSH_CORPUS):
        ranges = _changed_ranges(f.diff_path.read_text())
        for bug in f.bugs:
            hunks = ranges.get(bug.file, [])
            assert any(not (bug.line_end < a or bug.line_start > b) for a, b in hunks), (
                f"{f.name}: {bug.file}:{bug.line_start}-{bug.line_end} is outside {hunks}"
            )


def test_push_fixtures_stay_below_a_size_that_fits_one_agent_call() -> None:
    assert all(f.diff_path.stat().st_size <= 20_000 for f in pe.load_fixtures(_PUSH_CORPUS))


def test_the_default_corpus_does_not_hold_the_push_fixtures() -> None:
    """They are larger and dearer, so the cheap default run must not pick them up."""
    default_names = {f.name for f in pe.load_fixtures(pe.CORPUS_DIR)}
    push_names = {f.name for f in pe.load_fixtures(_PUSH_CORPUS)}
    assert default_names.isdisjoint(push_names)


@pytest.mark.anyio
async def test_the_push_corpus_estimate_fits_the_run_cap(
    state: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AI_EVAL_CORPUS_DIR", str(_PUSH_CORPUS))
    await pe.main(["--dry-run"])
    line = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("spend guard:")][0]
    expected = float(re.search(r"expected \$([0-9.]+)", line).group(1))  # type: ignore[union-attr]
    worst = float(re.search(r"worst case \$([0-9.]+)", line).group(1))  # type: ignore[union-attr]
    assert expected < 2.0
    assert worst < 5.0
