"""Raw model text printed by the extractor cannot forge workflow commands (story 17-12)."""

from __future__ import annotations

import pytest

from ai_pr_review.findings.extract import extract_with_status

_TOKEN = "ghp_" + "CanaryTokenValue0123456789abcdef"


def test_invalid_json_preview_neutralizes_a_workflow_command(
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = f"```json-findings\n[{{broken\n::error::forged {_TOKEN}\n::add-mask::x\n```"
    result = extract_with_status(output, agent_name="agent-a")
    assert result.status == "invalid_json"
    err = capsys.readouterr().err
    assert "forged" in err
    assert _TOKEN not in err
    assert not any(line.lstrip().startswith("::") for line in err.splitlines())


def test_malformed_finding_message_masks_a_token(capsys: pytest.CaptureFixture[str]) -> None:
    output = f'```json-findings\n[{{"severity": "{_TOKEN}", "finding": "x"}}]\n```'
    result = extract_with_status(output, agent_name="agent-a")
    assert result.findings == []
    err = capsys.readouterr().err
    assert "dropped malformed finding" in err
    assert _TOKEN not in err
