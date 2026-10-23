"""Hard spend cap for harnesses that make billed LLM calls.

Why this exists: in September 2026 the corpus harness used about $142 of a $149
six-day quota on the shared Anthropic key, 95% of all spend (see the checkpoint
section of ``CLAUDE.md``). Each session stayed "small" and together they did
not. This guard enforces two caps:

  * a per-run cap (default $5.00, ``AI_EVAL_MAX_COST_USD``), and
  * a campaign cap (default $9.00, ``AI_EVAL_CAMPAIGN_CAP_USD``) across every
    run and session on this machine, kept in a shared ledger file.

How it works:

  1. Before a call, ``reserve()`` adds the WORST-CASE cost of that call to the
     ledger as spent, and refuses if either cap would be passed. Worst case is
     the estimated input tokens at the dearest input rate (the cache-write rate
     counts when it is higher) plus ``max_tokens`` output tokens, for the dearer
     prompt-length tier when the model has one.
  2. After the call, ``settle()`` replaces the reservation with the real cost
     from all four usage fields of the response (input, output, cache write,
     cache read).
  3. If the call raises, the reservation stays counted as spent. A crash does the
     same. The ledger can only over-count, never under-count.

The check and the update happen under an exclusive file lock, so two calls in
one process, or two processes, cannot both pass a check that only one fits.

Fail closed: an unpriced model (``token_cost_units`` returns ``None``), a
response with no usage, and an unreadable or corrupt ledger all stop the run.

The ledger lives outside the repository, so every git worktree shares it:
``~/.cache/ai-pr-review-eval/`` (override with ``AI_EVAL_STATE_DIR``). The cap
is per machine. The API key quota is shared, so run evals on one host.

Commands:

    python tests/canary/_spend_guard.py status
    python tests/canary/_spend_guard.py reset --yes   # zero the campaign total
"""

from __future__ import annotations

import contextlib

try:
    import fcntl
except ImportError:  # Windows has no fcntl. The guard then refuses to run, it never runs unlocked.
    fcntl = None  # type: ignore[assignment]
import json
import os
import sys
import tempfile
import threading
import time
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from ai_pr_review.llm.base import LLMRequest, LLMResponse  # noqa: E402
from ai_pr_review.pricing import (  # noqa: E402
    ModelRates,
    load_pricing,
    model_pricing,
    token_cost_units,
)
from ai_pr_review.review.cost_ceiling import estimate_billed_tokens  # noqa: E402

UNITS_PER_USD = 10_000  # pricing.py counts cost in $0.0001 units
DEFAULT_RUN_CAP_USD = 5.00
DEFAULT_CAMPAIGN_CAP_USD = 9.00
_LEDGER_VERSION = 1
_LOCK_TIMEOUT_S = 60.0

LLMCall = Callable[[LLMRequest], Awaitable[LLMResponse]]


class SpendGuardError(RuntimeError):
    """The guard refused or could not prove a call is within the caps."""


class SpendCapExceeded(SpendGuardError):
    """A reservation or a pre-flight estimate would pass a cap."""


class UnpricedModelError(SpendGuardError):
    """The model has no pricing entry, so its cost cannot be bounded."""


class LedgerError(SpendGuardError):
    """The ledger is unreadable, corrupt, or could not be locked."""


class ApprovalRequired(SpendGuardError):
    """A live run was not approved with ``--yes``."""


@dataclass(frozen=True)
class Reservation:
    units: int
    model_id: str


def usd(units: int) -> str:
    return f"${units / UNITS_PER_USD:.4f}"


def _units(usd_value: float) -> int:
    return int(round(usd_value * UNITS_PER_USD))


def default_state_dir() -> Path:
    override = os.environ.get("AI_EVAL_STATE_DIR", "").strip()
    return Path(override).expanduser() if override else Path.home() / ".cache" / "ai-pr-review-eval"


def _fresh_ledger() -> dict[str, object]:
    return {"version": _LEDGER_VERSION, "spent_units": 0, "calls": 0}


class SpendGuard:
    def __init__(
        self,
        *,
        run_cap_usd: float = DEFAULT_RUN_CAP_USD,
        campaign_cap_usd: float = DEFAULT_CAMPAIGN_CAP_USD,
        state_dir: Path | None = None,
        pricing_data: list[dict[str, object]] | None = None,
        label: str = "",
    ) -> None:
        if run_cap_usd <= 0 or campaign_cap_usd <= 0:
            raise ValueError("spend caps must be positive")
        self.run_cap_units = _units(run_cap_usd)
        self.campaign_cap_units = _units(campaign_cap_usd)
        self.state_dir = state_dir if state_dir is not None else default_state_dir()
        self.label = label
        if pricing_data is None:
            pricing_data = load_pricing(str(REPO_ROOT / "config" / "model-pricing.json"))
        self._pricing = pricing_data
        self._run_spent_units = 0
        self._history_warned = False
        self.history_dropped = 0
        self._lock = threading.Lock()  # guards _run_spent_units in this process
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._ledger_path = self.state_dir / "ledger.json"
        self._lock_path = self.state_dir / "ledger.lock"
        self._history_path = self.state_dir / "ledger-history.jsonl"

    # -- reading ---------------------------------------------------------

    @property
    def run_spent_units(self) -> int:
        return self._run_spent_units

    def campaign_spent_units(self) -> int:
        with self._ledger() as ledger:
            return int(ledger["spent_units"])  # type: ignore[call-overload]

    # -- ledger file -----------------------------------------------------

    @contextlib.contextmanager
    def _ledger(self) -> Iterator[dict[str, object]]:
        """Yield the ledger under an exclusive lock, then write it back atomically."""
        if fcntl is None:
            raise LedgerError("the spend guard needs fcntl (POSIX file locks) and cannot run on this platform")
        deadline = time.monotonic() + _LOCK_TIMEOUT_S
        with open(self._lock_path, "a+") as lock_file:
            while True:
                try:
                    fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() > deadline:
                        raise LedgerError(
                            f"could not lock {self._lock_path} in {_LOCK_TIMEOUT_S:.0f}s"
                        ) from None
                    time.sleep(0.05)
            try:
                ledger = self._read_ledger()
                before = json.dumps(ledger, sort_keys=True)
                yield ledger
                if json.dumps(ledger, sort_keys=True) != before:
                    self._write_ledger(ledger)
            finally:
                fcntl.flock(lock_file, fcntl.LOCK_UN)

    def _read_ledger(self) -> dict[str, object]:
        if not self._ledger_path.exists():
            return _fresh_ledger()
        try:
            data = json.loads(self._ledger_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise LedgerError(f"ledger {self._ledger_path} is unreadable: {exc}") from exc
        if (
            not isinstance(data, dict)
            or data.get("version") != _LEDGER_VERSION
            or not isinstance(data.get("spent_units"), int)
            or data["spent_units"] < 0
        ):
            raise LedgerError(
                f"ledger {self._ledger_path} is corrupt. Inspect it, then run "
                "'python tests/canary/_spend_guard.py reset --yes' if you accept the loss."
            )
        return data

    def _write_ledger(self, ledger: dict[str, object]) -> None:
        fd, tmp = tempfile.mkstemp(dir=self.state_dir, prefix="ledger.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(ledger, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self._ledger_path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    def _log(self, kind: str, model_id: str, units: int, note: str = "") -> None:
        record = {
            "ts": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "label": self.label,
            "kind": kind,
            "model": model_id,
            "units": units,
            "note": note,
        }
        # The audit log never blocks a call, but a broken log is reported once.
        try:
            with open(self._history_path, "a") as handle:
                handle.write(json.dumps(record) + "\n")
        except OSError as exc:
            self.history_dropped += 1
            if not self._history_warned:
                self._history_warned = True
                print(
                    f"WARNING: spend guard: cannot write the audit log {self._history_path}: "
                    f"{exc}. Spend is still counted in the ledger, but the history is incomplete.",
                    file=sys.stderr,
                )

    # -- cost ------------------------------------------------------------

    def _rates(self, model_id: str) -> ModelRates:
        rates = model_pricing(model_id, self._pricing)
        if token_cost_units(rates, input_tokens=1, output_tokens=1) is None:
            raise UnpricedModelError(
                f"model {model_id!r} has no pricing entry in config/model-pricing.json, "
                "so its cost cannot be bounded. Add a pricing row before running it."
            )
        return rates

    def worst_case_units(self, request: LLMRequest) -> int:
        """The most this call can cost: dearest input rate, full ``max_tokens`` output."""
        rates = self._rates(request.model_id)
        text = "".join((
            request.system_prompt, request.system_prefix, *request.cache_blocks,
            request.user_message,
        ))
        # A tool-loop turn also sends the earlier turns and the tool definitions (#1088).
        # Count them, or a late turn that carries large tool results would be under-reserved.
        if request.messages:
            text += json.dumps(list(request.messages), ensure_ascii=False)
        if request.tools:
            text += json.dumps(list(request.tools), ensure_ascii=False)
        input_tokens = estimate_billed_tokens(text)

        def cost(input_rate: int, output_rate: int, cache_write_rate: int) -> int:
            return (
                input_tokens * max(input_rate, cache_write_rate)
                + request.max_tokens * output_rate
            ) // 100_000_000 + 1

        worst = cost(rates.input_rate, rates.output_rate, rates.cache_write_rate)
        long_prompt = rates.long_prompt
        if long_prompt is not None:
            # The input estimate can fall just under a threshold that the real
            # prompt passes, so always reserve at the dearer tier.
            worst = max(
                worst,
                cost(long_prompt.input_rate, long_prompt.output_rate, long_prompt.cache_write_rate),
            )
        return worst

    def estimate_units(
        self, model_id: str, *, input_tokens: int, output_tokens: int, worst_case: bool = False
    ) -> int:
        """Cost of one call with these token counts (for a pre-flight estimate).

        ``worst_case`` also prices the dearer prompt-length tier, as ``reserve()``
        does, so the pre-flight worst case matches what the guard will reserve.
        """
        rates = self._rates(model_id)
        units = token_cost_units(rates, input_tokens=input_tokens, output_tokens=output_tokens)
        assert units is not None  # _rates() raises for an unpriced model
        long_prompt = rates.long_prompt
        if worst_case and long_prompt is not None:
            units = max(units, (
                input_tokens * long_prompt.input_rate + output_tokens * long_prompt.output_rate
            ) // 100_000_000)
        return units

    # -- reserve and settle ---------------------------------------------

    def reserve(self, request: LLMRequest) -> Reservation:
        units = self.worst_case_units(request)
        with self._lock, self._ledger() as ledger:
            spent = int(ledger["spent_units"])  # type: ignore[call-overload]
            if self._run_spent_units + units > self.run_cap_units:
                raise SpendCapExceeded(
                    f"run cap {usd(self.run_cap_units)} would be passed: spent "
                    f"{usd(self._run_spent_units)} plus this call's worst case {usd(units)}"
                )
            if spent + units > self.campaign_cap_units:
                raise SpendCapExceeded(
                    f"campaign cap {usd(self.campaign_cap_units)} would be passed: "
                    f"ledger {usd(spent)} plus this call's worst case {usd(units)}"
                )
            ledger["spent_units"] = spent + units
            ledger["calls"] = int(ledger["calls"]) + 1  # type: ignore[call-overload]
            self._run_spent_units += units
        self._log("reserve", request.model_id, units)
        return Reservation(units=units, model_id=request.model_id)

    def settle(self, reservation: Reservation, response: LLMResponse) -> int:
        """Replace *reservation* with the real cost. Returns the units now counted."""
        rates = self._rates(reservation.model_id)
        no_usage = (
            response.input_tokens <= 0
            and response.output_tokens <= 0
            and response.cache_creation_tokens <= 0
            and response.cache_read_tokens <= 0
        )
        actual = None if no_usage else token_cost_units(
            rates,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            cache_creation_tokens=response.cache_creation_tokens,
            cache_read_tokens=response.cache_read_tokens,
        )
        if actual is None:
            # No usage reported: keep the reservation. Over-counting is safe.
            print(
                f"WARNING: spend guard: no usage in the response from {reservation.model_id}; "
                f"keeping the {usd(reservation.units)} reservation as spent.",
                file=sys.stderr,
            )
            self._log("settle-kept", reservation.model_id, reservation.units, "no usage")
            return reservation.units
        delta = actual - reservation.units
        with self._lock, self._ledger() as ledger:
            ledger["spent_units"] = max(int(ledger["spent_units"]) + delta, 0)  # type: ignore[call-overload]
            self._run_spent_units = max(self._run_spent_units + delta, 0)
            campaign_over = int(ledger["spent_units"]) > self.campaign_cap_units  # type: ignore[call-overload]
        self._log("settle", reservation.model_id, actual)
        if actual > reservation.units and (
            campaign_over or self._run_spent_units > self.run_cap_units
        ):
            # The real cost passed the reservation and a cap. The call is already billed.
            # Say so now. The next reserve() refuses, so the overshoot cannot grow.
            print(
                f"WARNING: spend guard: the real cost {usd(actual)} passed its "
                f"{usd(reservation.units)} reservation and a spend cap is now exceeded. "
                "The run stops at the next call.",
                file=sys.stderr,
            )
        return actual

    def abandon(self, reservation: Reservation, reason: str = "") -> None:
        """The call failed. The reservation stays counted as spent."""
        self._log("abandon", reservation.model_id, reservation.units, reason)

    async def call(self, llm_call: LLMCall, request: LLMRequest) -> LLMResponse:
        reservation = self.reserve(request)
        try:
            response = await llm_call(request)
        except BaseException as exc:
            self.abandon(reservation, type(exc).__name__)
            raise
        try:
            self.settle(reservation, response)
        except Exception as exc:  # noqa: BLE001 - any settle failure, including OSError, must keep the response
            # The call succeeded and was billed. Keep the response, keep the
            # reservation as spent (over-counting is safe), and say what happened.
            print(
                f"WARNING: spend guard: the call to {reservation.model_id} succeeded but "
                f"settling its cost failed ({exc}). Keeping the {usd(reservation.units)} "
                "reservation as spent.",
                file=sys.stderr,
            )
            self._log("settle-failed", reservation.model_id, reservation.units, str(exc))
        return response

    def wrap(self, llm_call: LLMCall) -> LLMCall:
        async def guarded(request: LLMRequest) -> LLMResponse:
            return await self.call(llm_call, request)

        return guarded

    # -- pre-flight ------------------------------------------------------

    def preflight(
        self,
        *,
        expected_units: int,
        worst_case_units: int,
        yes: bool,
        out: Callable[[str], None] = print,
    ) -> None:
        """Print the estimate. Raise unless it fits the caps and the run is approved."""
        campaign_left = self.campaign_cap_units - self.campaign_spent_units()
        out(
            "spend guard: expected "
            f"{usd(expected_units)}, worst case {usd(worst_case_units)}, "
            f"run cap {usd(self.run_cap_units)}, campaign left {usd(max(campaign_left, 0))} "
            f"of {usd(self.campaign_cap_units)}"
        )
        if expected_units > self.run_cap_units:
            raise SpendCapExceeded(
                f"expected cost {usd(expected_units)} is over the run cap "
                f"{usd(self.run_cap_units)}. Narrow the run (fewer diffs, runs, models, "
                "or agents) or raise AI_EVAL_MAX_COST_USD with approval."
            )
        if expected_units > campaign_left:
            raise SpendCapExceeded(
                f"expected cost {usd(expected_units)} is over the campaign cap left "
                f"{usd(max(campaign_left, 0))}."
            )
        if not yes:
            raise ApprovalRequired(
                "this run makes real billed API calls. Re-run with --yes (or AI_EVAL_YES=1) "
                "after the human approval that CLAUDE.md requires."
            )

    # -- ledger commands -------------------------------------------------

    def reset_campaign(self) -> None:
        with self._ledger() as ledger:
            ledger["spent_units"] = 0
            ledger["calls"] = 0
        self._log("reset", "", 0)

    @classmethod
    def from_env(cls, label: str = "") -> SpendGuard:
        def _cap(name: str, default: float) -> float:
            raw = os.environ.get(name, "").strip()
            if not raw:
                return default
            try:
                return float(raw)
            except ValueError as exc:
                raise SpendGuardError(f"{name}={raw!r} is not a number") from exc

        return cls(
            run_cap_usd=_cap("AI_EVAL_MAX_COST_USD", DEFAULT_RUN_CAP_USD),
            campaign_cap_usd=_cap("AI_EVAL_CAMPAIGN_CAP_USD", DEFAULT_CAMPAIGN_CAP_USD),
            label=label,
        )


def wants_yes(argv: list[str]) -> bool:
    return "--yes" in argv or os.environ.get("AI_EVAL_YES", "").strip() in ("1", "true", "yes")


def main(argv: list[str]) -> int:
    command = argv[0] if argv else "status"
    try:
        guard = SpendGuard.from_env(label="cli")
        if command == "status":
            print(f"state dir:      {guard.state_dir}")
            print(f"campaign spent: {usd(guard.campaign_spent_units())} of {usd(guard.campaign_cap_units)}")
            print(f"run cap:        {usd(guard.run_cap_units)}")
            return 0
        if command == "reset":
            if "--yes" not in argv:
                print("reset zeroes the campaign total. Re-run with --yes to confirm.", file=sys.stderr)
                return 1
            guard.reset_campaign()
            print("campaign total reset to $0.0000")
            return 0
    except SpendGuardError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print("usage: _spend_guard.py status | reset --yes", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
