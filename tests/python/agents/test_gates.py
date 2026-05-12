"""Tests for ai_pr_review.agents.gates — conditional gate evaluation."""

from __future__ import annotations

import pytest

from ai_pr_review.agents.gates import evaluate_gates, filter_agents
from ai_pr_review.agents.roster import AgentSpec, ConditionalTrigger
from ai_pr_review.manifest import ChangedFiles

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _changed(files: list[str]) -> ChangedFiles:
    from ai_pr_review.manifest import build_changed_files
    return build_changed_files(files)


# ---------------------------------------------------------------------------
# T2.1 — has_error_patterns
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("keyword", [
    "catch",
    "if err",
    "try {",
    "rescue",
    "Result<",
    "unwrap",
    "except",
    ".catch(",
])
def test_has_error_patterns_fires_on_keyword(keyword: str) -> None:
    diff = f"+ some code with {keyword} here"
    gates = evaluate_gates(diff, _changed([]), {})
    assert "has_error_patterns" in gates


def test_has_error_patterns_absent_when_no_keywords() -> None:
    diff = "- removed line\n+ added a normal line\n context line"
    gates = evaluate_gates(diff, _changed([]), {})
    assert "has_error_patterns" not in gates


# ---------------------------------------------------------------------------
# T2.2 — has_code_or_infra
# ---------------------------------------------------------------------------

def test_has_code_or_infra_fires_for_source_file() -> None:
    gates = evaluate_gates("", _changed(["src/main.py"]), {})
    assert "has_code_or_infra" in gates


def test_has_code_or_infra_fires_for_workflow_file() -> None:
    gates = evaluate_gates("", _changed([".github/workflows/ci.yml"]), {})
    assert "has_code_or_infra" in gates


def test_has_code_or_infra_absent_for_docs_only() -> None:
    files = ["README.md", "docs/guide.rst", "notes.txt"]
    gates = evaluate_gates("", _changed(files), {})
    assert "has_code_or_infra" not in gates


def test_has_code_or_infra_absent_for_meta_dirs() -> None:
    files = [
        "docs/architecture.md",
        "memory-bank/notes.md",
        ".github/PULL_REQUEST_TEMPLATE.md",
        ".claude/settings.json",
    ]
    gates = evaluate_gates("", _changed(files), {})
    assert "has_code_or_infra" not in gates


def test_has_code_or_infra_absent_for_meta_filenames() -> None:
    files = [
        "CHANGELOG.md",
        "README.rst",
        "LICENSE",
        "NOTICE.txt",
        "AUTHORS",
        "CONTRIBUTING.md",
        "CODEOWNERS",
        "CODE_OF_CONDUCT.md",
    ]
    gates = evaluate_gates("", _changed(files), {})
    assert "has_code_or_infra" not in gates


def test_has_code_or_infra_fires_when_mixed_with_docs() -> None:
    files = ["README.md", "src/app.ts"]
    gates = evaluate_gates("", _changed(files), {})
    assert "has_code_or_infra" in gates


# ---------------------------------------------------------------------------
# T2.3 — has_security_patterns
# ---------------------------------------------------------------------------

def test_has_security_patterns_fires_on_keyword_in_diff() -> None:
    diff = "+ const token = getToken();"
    gates = evaluate_gates(diff, _changed([]), {})
    assert "has_security_patterns" in gates


def test_has_security_patterns_fires_on_auth_path() -> None:
    gates = evaluate_gates("", _changed(["src/auth/login.py"]), {})
    assert "has_security_patterns" in gates


def test_has_security_patterns_fires_on_dotenv_file() -> None:
    gates = evaluate_gates("", _changed([".env.production"]), {})
    assert "has_security_patterns" in gates


def test_has_security_patterns_fires_on_requirements_file() -> None:
    gates = evaluate_gates("", _changed(["requirements.txt"]), {})
    assert "has_security_patterns" in gates


def test_has_security_patterns_fires_on_dockerfile() -> None:
    gates = evaluate_gates("", _changed(["Dockerfile"]), {})
    assert "has_security_patterns" in gates


def test_has_security_patterns_fires_on_shell_script() -> None:
    gates = evaluate_gates("", _changed(["deploy.sh"]), {})
    assert "has_security_patterns" in gates


def test_has_security_patterns_absent_on_plain_change() -> None:
    diff = "+ def calculate_total(items):\n+     return sum(items)"
    gates = evaluate_gates(diff, _changed(["src/math_utils.py"]), {})
    assert "has_security_patterns" not in gates


# ---------------------------------------------------------------------------
# T2.4 — has_control_flow
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("keyword", [
    "if", "elif", "else", "for", "while", "do", "case", "switch", "match",
    "try", "catch", "except", "rescue", "unless", "when", "loop",
    "break", "continue", "return", "goto", "defer", "finally",
])
def test_has_control_flow_fires_on_added_line(keyword: str) -> None:
    diff = f"+ {keyword} something"
    gates = evaluate_gates(diff, _changed([]), {})
    assert "has_control_flow" in gates


def test_has_control_flow_ignores_removed_lines() -> None:
    diff = "- if old_condition:\n- for item in items:"
    gates = evaluate_gates(diff, _changed([]), {})
    assert "has_control_flow" not in gates


def test_has_control_flow_ignores_context_lines() -> None:
    diff = " if context_line:\n  for item in context:"
    gates = evaluate_gates(diff, _changed([]), {})
    assert "has_control_flow" not in gates


def test_has_control_flow_ignores_diff_header() -> None:
    diff = "+++ b/some/file.py\n+ some normal line"
    gates = evaluate_gates(diff, _changed([]), {})
    assert "has_control_flow" not in gates


def test_has_control_flow_requires_word_boundary() -> None:
    # "ifdef" or "foreach" should not trigger standalone "if" or "for"
    diff = "+ ifdef SOME_MACRO\n+ foreach(list, callback)"
    gates = evaluate_gates(diff, _changed([]), {})
    assert "has_control_flow" not in gates


def test_has_control_flow_absent_with_no_keywords() -> None:
    diff = "+ x = 1\n+ y = x + 2"
    gates = evaluate_gates(diff, _changed([]), {})
    assert "has_control_flow" not in gates


# ---------------------------------------------------------------------------
# T2.5 — no_prior_summary
# ---------------------------------------------------------------------------

def test_no_prior_summary_fires_when_none() -> None:
    gates = evaluate_gates("", _changed([]), {}, last_reviewed_sha=None)
    assert "no_prior_summary" in gates


def test_no_prior_summary_fires_when_empty_string() -> None:
    gates = evaluate_gates("", _changed([]), {}, last_reviewed_sha="")
    assert "no_prior_summary" in gates


def test_no_prior_summary_absent_when_sha_present() -> None:
    gates = evaluate_gates("", _changed([]), {}, last_reviewed_sha="abc123def456")
    assert "no_prior_summary" not in gates


# ---------------------------------------------------------------------------
# T2.6 — kill-switches
# ---------------------------------------------------------------------------

def test_killswitch_architecture_forces_has_code_or_infra() -> None:
    # docs-only files would not normally fire — kill-switch overrides
    files = ["README.md"]
    env = {"AI_DISABLE_GATE_ARCHITECTURE": "true"}
    gates = evaluate_gates("", _changed(files), env)
    assert "has_code_or_infra" in gates


def test_killswitch_security_forces_has_security_patterns() -> None:
    diff = "+ def plain_math(x): return x + 1"
    env = {"AI_DISABLE_GATE_SECURITY": "true"}
    gates = evaluate_gates(diff, _changed(["src/math.py"]), env)
    assert "has_security_patterns" in gates


def test_killswitch_edge_case_forces_has_control_flow() -> None:
    diff = "+ x = 1"
    env = {"AI_DISABLE_GATE_EDGE_CASE": "true"}
    gates = evaluate_gates(diff, _changed([]), env)
    assert "has_control_flow" in gates


def test_killswitch_does_not_affect_other_gates() -> None:
    # Setting ARCHITECTURE kill-switch does not force security gate
    env = {"AI_DISABLE_GATE_ARCHITECTURE": "true"}
    diff = "+ def plain_math(x): return x + 1"
    gates = evaluate_gates(diff, _changed(["src/math.py"]), env)
    assert "has_security_patterns" not in gates


# ---------------------------------------------------------------------------
# T2.7 — filter_agents
# ---------------------------------------------------------------------------

def _spec(
    name: str,
    tier: int,
    trigger: ConditionalTrigger | None,
) -> AgentSpec:
    return AgentSpec(
        name=name,
        tier=tier,
        prompt_path=f"prompts/{name}.md",
        max_output_tokens=4096,
        conditional_trigger=trigger,
        full_mode_only=False,
        context_enrichment_eligible=False,
    )


def test_filter_agents_includes_unconditional() -> None:
    unconditional = _spec("code-reviewer", 1, None)
    result = filter_agents([unconditional], frozenset())
    assert result == [unconditional]


def test_filter_agents_includes_conditional_when_gate_fired() -> None:
    conditional = _spec("security-reviewer", 2, "has_security_patterns")
    result = filter_agents([conditional], frozenset({"has_security_patterns"}))
    assert result == [conditional]


def test_filter_agents_excludes_conditional_when_gate_absent() -> None:
    conditional = _spec("security-reviewer", 2, "has_security_patterns")
    result = filter_agents([conditional], frozenset({"has_error_patterns"}))
    assert result == []


def test_filter_agents_mixed_list() -> None:
    unconditional = _spec("code-reviewer", 1, None)
    included = _spec("security-reviewer", 2, "has_security_patterns")
    excluded = _spec("edge-case-hunter", 1, "has_control_flow")
    result = filter_agents(
        [unconditional, included, excluded],
        frozenset({"has_security_patterns"}),
    )
    assert result == [unconditional, included]


# ---------------------------------------------------------------------------
# T2.8 — evaluate_gates end-to-end
# ---------------------------------------------------------------------------

def test_evaluate_gates_end_to_end_realistic_diff() -> None:
    diff = """\
diff --git a/src/auth/login.py b/src/auth/login.py
--- a/src/auth/login.py
+++ b/src/auth/login.py
@@ -1,5 +1,10 @@
 def authenticate(user, password):
+    try:
+        token = generate_token(user)
+        if not token:
+            raise ValueError("no token")
+        return token
     except Exception as exc:
         raise RuntimeError("auth failed") from exc
"""
    files = ["src/auth/login.py"]
    gates = evaluate_gates(diff, _changed(files), {})
    assert "has_error_patterns" in gates
    assert "has_code_or_infra" in gates
    assert "has_security_patterns" in gates
    assert "has_control_flow" in gates
    assert "no_prior_summary" in gates  # last_reviewed_sha=None by default


def test_evaluate_gates_returns_frozenset() -> None:
    result = evaluate_gates("", _changed([]), {})
    assert isinstance(result, frozenset)
