"""Live-API verification: does Anthropic accept an explicit `temperature` on
claude-sonnet-5 / claude-opus-4-8, or does it reject it with a 400?

Background: llm/_config.py::resolve_temperature() returns None for these two
models, so llm/anthropic.py sends no `temperature` field and the API applies
its own default (1.0). That stripping is either (a) correct -- the API would
400 on an explicit temperature for these models -- or (b) ai-pr-review being
overly conservative, in which case AI_TEMPERATURE (and the judge's coded
temperature=0.0) could be made to actually apply and stabilize reviews.

This script answers that empirically instead of trusting docs or memory. For
each model it sends three otherwise-identical minimal requests -- temperature
omitted, temperature=0.0, temperature=1.0 -- straight to the real endpoint,
bypassing resolve_temperature(), and reports the HTTP status and (on failure)
the API error body for each.

Not a pytest suite: this makes real, billed API calls and is intentionally
excluded from the default `pytest tests/python` run. Invoke directly:

    ANTHROPIC_API_KEY=... python tests/canary/temperature_verify.py

Exit code 0 if every model's behavior was determined cleanly (each of the
three cases returned either a clear 200 or a clear 400 -- i.e. the question
was answered), 1 on any inconclusive result (network error, unexpected
non-200/non-400 status, or a missing key). The 400s here are the *expected,
informative* outcome if temperature is restricted -- they are reported, not
treated as script failure.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass

import httpx

_API_URL = "https://api.anthropic.com/v1/messages"
_ANTHROPIC_VERSION = "2023-06-01"

# The exact model IDs ai-pr-review defaults to (config.py) and that
# resolve_temperature() strips temperature for.
MODELS = ("claude-sonnet-5", "claude-opus-4-8")

# temperature configurations to probe. None => omit the field entirely (the
# baseline ai-pr-review currently sends); 0.0 and 1.0 => send an explicit value.
TEMPS: tuple[float | None, ...] = (None, 0.0, 1.0)


@dataclass
class ProbeResult:
    model: str
    temperature: float | None
    status: int | None  # HTTP status, or None if the request never completed
    detail: str
    # True when this probe produced a determinate answer (clean 200 or clean
    # 400). False for network errors / unexpected statuses that leave the
    # temperature question unanswered for this case.
    conclusive: bool


def _build_body(model: str, temperature: float | None) -> dict[str, object]:
    """A minimal valid Messages request. sonnet-5 has adaptive thinking on by
    default and can spend a whole small max_tokens budget on thinking (issue
    #592), so cap effort at low to keep the probe cheap and to match what
    ai-pr-review actually sends for sonnet-5 -- the effort field must not itself
    be the thing that 400s, or we'd misattribute the failure to temperature.
    Opus 4.8 must NOT receive output_config.effort here for the same reason:
    it has no default adaptive thinking, and we want temperature to be the only
    variable under test."""
    body: dict[str, object] = {
        "model": model,
        "max_tokens": 16,
        "messages": [{"role": "user", "content": "Reply with the single word: ok"}],
    }
    if "sonnet-5" in model:
        body["output_config"] = {"effort": "low"}
    if temperature is not None:
        body["temperature"] = temperature
    return body


def _probe(client: httpx.Client, api_key: str, model: str, temperature: float | None) -> ProbeResult:
    headers = {
        "x-api-key": api_key,
        "anthropic-version": _ANTHROPIC_VERSION,
        "Content-Type": "application/json",
    }
    body = _build_body(model, temperature)
    try:
        resp = client.post(_API_URL, headers=headers, json=body, timeout=60.0)
    except httpx.HTTPError as exc:
        return ProbeResult(model, temperature, None, f"request failed: {exc!r}", conclusive=False)

    if resp.status_code == 200:
        return ProbeResult(model, temperature, 200, "accepted (HTTP 200)", conclusive=True)

    # Non-200: extract the API error type/message so a 400 tells us *why*
    # (e.g. temperature is unsupported for this model) rather than just "400".
    try:
        payload = resp.json()
        err = payload.get("error", {})
        detail = f"{err.get('type', '?')}: {err.get('message', resp.text[:200])}"
    except (ValueError, json.JSONDecodeError):
        detail = resp.text[:200]

    # A 400 is a determinate answer: temperature was rejected. Any other
    # non-200 (401 bad key, 429, 5xx) is inconclusive for the temperature
    # question -- report it but don't claim we learned anything about temp.
    conclusive = resp.status_code == 400
    return ProbeResult(model, temperature, resp.status_code, detail, conclusive=conclusive)


def main() -> int:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        print("SKIP: ANTHROPIC_API_KEY not set; nothing verified", file=sys.stderr)
        return 1

    results: list[ProbeResult] = []
    with httpx.Client() as client:
        for model in MODELS:
            for temperature in TEMPS:
                r = _probe(client, api_key, model, temperature)
                results.append(r)
                temp_label = "omitted" if temperature is None else f"temperature={temperature}"
                status_label = "----" if r.status is None else str(r.status)
                print(f"[{status_label}] model={model} {temp_label}: {r.detail}")

    # Per-model verdict on the temperature question.
    print()
    for model in MODELS:
        by_temp = {r.temperature: r for r in results if r.model == model}
        omitted = by_temp.get(None)
        t0 = by_temp.get(0.0)
        t1 = by_temp.get(1.0)
        explicit_conclusive = all(p is not None and p.conclusive for p in (t0, t1))
        if not explicit_conclusive:
            print(f"VERDICT {model}: INCONCLUSIVE (an explicit-temperature probe did not return a clean 200/400)")
            continue
        assert t0 is not None and t1 is not None  # narrowed by explicit_conclusive
        if t0.status == 200 and t1.status == 200:
            print(f"VERDICT {model}: ACCEPTS explicit temperature (both 0.0 and 1.0 returned 200)")
        elif t0.status == 400 and t1.status == 400:
            print(f"VERDICT {model}: REJECTS explicit temperature (both 0.0 and 1.0 returned 400) "
                  f"-- resolve_temperature() stripping is CORRECT for this model")
        else:
            print(f"VERDICT {model}: MIXED (0.0 -> {t0.status}, 1.0 -> {t1.status}); "
                  f"omitted baseline -> {omitted.status if omitted else 'n/a'}")

    inconclusive = [r for r in results if not r.conclusive]
    total = len(results)
    print(f"\n=== {total - len(inconclusive)}/{total} probes conclusive ===")
    return 1 if inconclusive else 0


if __name__ == "__main__":
    sys.exit(main())
