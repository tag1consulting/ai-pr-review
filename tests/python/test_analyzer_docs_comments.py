"""Tests for the docs-api-check native analyzer.

docs-missing-check (the sibling analyzer this module used to also back) was
removed in #815; its dedicated test classes (TestTreeSitterMissingFindings,
TestGoPath) and the two missing-check-specific TestPythonPaths tests were
removed with it. Anything below still exercises docs-api-check's own
behavior (shared tree-sitter helpers, ruff --isolated path).
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ai_pr_review.analyzers.native.docs_comments import (
    _doc_param_names,
    _run_docs_api_check,
    _tree_sitter_api_findings,
)
from ai_pr_review.manifest import ChangedFiles

# tree-sitter-language-pack is an optional dependency (the [context] extra);
# CI's lint/test workflow installs only [dev], never [context] (verified
# this session against .github/workflows/lint.yml), so classes exercising
# real tree-sitter parsing must skip rather than fail when it is absent.
# Mirrors test_context_treesitter.py's existing accommodation for the same
# optional dependency, extended here because these tests (unlike that
# file's) assert specific parsed content rather than just "returns a list".
_HAS_TREE_SITTER = importlib.util.find_spec("tree_sitter_language_pack") is not None
_requires_tree_sitter = pytest.mark.skipif(
    not _HAS_TREE_SITTER, reason="tree-sitter-language-pack not installed (optional [context] extra)"
)


def _make_cf(**kwargs: list[str]) -> ChangedFiles:
    all_files = [f for files in kwargs.values() for f in files]
    return ChangedFiles(all_files=all_files, **kwargs)


class TestDocParamNames:
    def test_jsdoc_style_with_type_before_name(self) -> None:
        names = _doc_param_names("@param {number} amount the amount")
        assert names == {"amount"}

    def test_yard_style_with_type_after_name(self) -> None:
        names = _doc_param_names("@param amount [Integer] the amount")
        assert names == {"amount"}

    def test_bare_name_no_type(self) -> None:
        names = _doc_param_names("@param amount the amount")
        assert names == {"amount"}

    def test_xml_doc_style(self) -> None:
        names = _doc_param_names('<param name="amount">the amount</param>')
        assert names == {"amount"}

    def test_multiple_tags(self) -> None:
        names = _doc_param_names("@param {number} src\n@param {number} dst")
        assert names == {"src", "dst"}

    def test_no_tags_returns_empty(self) -> None:
        assert _doc_param_names("Just a description, no tags.") == set()


@_requires_tree_sitter
class TestTreeSitterApiFindingsJavaScript:
    def _write(self, tmp_path: Path, content: str, name: str = "sample.js") -> str:
        f = tmp_path / name
        f.write_text(content)
        return str(f)

    def test_documented_param_not_in_signature(self, tmp_path: Path) -> None:
        f = self._write(tmp_path, """\
/**
 * @param {number} src
 * @param {number} extra
 */
function transfer(src) {
    return src;
}
""")
        findings = _tree_sitter_api_findings(f)
        assert len(findings) == 1
        assert findings[0].severity == "Medium"
        assert findings[0].confidence == 80
        assert findings[0].source == "docs-api-check"
        assert findings[0].category == "docs"
        assert "extra" in findings[0].finding
        assert "not in the function's signature" in findings[0].finding

    def test_signature_param_not_documented(self, tmp_path: Path) -> None:
        f = self._write(tmp_path, """\
/**
 * @param {number} src
 */
function transfer(src, amount) {
    return amount;
}
""")
        findings = _tree_sitter_api_findings(f)
        assert len(findings) == 1
        assert "amount" in findings[0].finding
        assert "is not documented" in findings[0].finding

    def test_matching_params_no_findings(self, tmp_path: Path) -> None:
        f = self._write(tmp_path, """\
/**
 * @param {number} src
 * @param {number} amount
 */
function transfer(src, amount) {
    return amount;
}
""")
        assert _tree_sitter_api_findings(f) == []

    def test_no_doc_comment_no_findings(self, tmp_path: Path) -> None:
        f = self._write(tmp_path, "function transfer(src, amount) {\n    return amount;\n}\n")
        assert _tree_sitter_api_findings(f) == []

    def test_doc_comment_with_no_param_tags_no_findings(self, tmp_path: Path) -> None:
        f = self._write(tmp_path, """\
/**
 * Move money around.
 */
function transfer(src, amount) {
    return amount;
}
""")
        assert _tree_sitter_api_findings(f) == []

    def test_destructured_param_skips_function_entirely(self, tmp_path: Path) -> None:
        # A function with an unresolvable (destructured) parameter is
        # skipped entirely for mismatch checking, even for its resolvable
        # params — a deliberate false-positive-avoidance scoping decision.
        f = self._write(tmp_path, """\
/**
 * @param {number} extra
 */
function transfer(src, {amount, currency} = {}) {
    return amount;
}
""")
        assert _tree_sitter_api_findings(f) == []

    def test_method_definition_in_class(self, tmp_path: Path) -> None:
        f = self._write(tmp_path, """\
class Foo {
    /**
     * @param {string} extra
     */
    greet(name) {
        return name;
    }
}
""")
        findings = _tree_sitter_api_findings(f)
        assert len(findings) == 2
        messages = {f.finding for f in findings}
        assert any("extra" in m for m in messages)
        assert any("name" in m for m in messages)

    def test_unrecognized_extension_returns_empty(self, tmp_path: Path) -> None:
        f = tmp_path / "sample.unknownlang"
        f.write_text("whatever")
        assert _tree_sitter_api_findings(str(f)) == []

    def test_unparseable_file_fails_soft(self, tmp_path: Path) -> None:
        # Malformed JS still parses (tree-sitter is error-tolerant); this
        # asserts no crash rather than any specific finding count.
        f = self._write(tmp_path, "function (((( not valid js")
        _tree_sitter_api_findings(f)  # must not raise


@_requires_tree_sitter
class TestTreeSitterApiFindingsOtherLanguages:
    def test_typescript_wrapped_parameters(self, tmp_path: Path) -> None:
        f = tmp_path / "sample.ts"
        f.write_text("""\
/**
 * @param src source
 * @param extra unused
 */
function transfer(src: string, amount: number): number {
    return amount;
}
""")
        findings = _tree_sitter_api_findings(str(f))
        messages = {x.finding for x in findings}
        assert any("extra" in m for m in messages)
        assert any("amount" in m for m in messages)

    def test_java_wrapper_node_param_extraction(self, tmp_path: Path) -> None:
        f = tmp_path / "Sample.java"
        f.write_text("""\
public class Sample {
    /**
     * @param extra unused
     */
    public int transfer(int amount) {
        return amount;
    }
}
""")
        findings = _tree_sitter_api_findings(str(f))
        messages = {x.finding for x in findings}
        assert any("extra" in m for m in messages)
        assert any("amount" in m for m in messages)

    def test_csharp_xml_doc_style(self, tmp_path: Path) -> None:
        f = tmp_path / "Sample.cs"
        f.write_text("""\
class Sample {
    /// <param name="amount">the amount</param>
    /// <param name="extra">unused</param>
    public int Transfer(int amount) {
        return amount;
    }
}
""")
        findings = _tree_sitter_api_findings(str(f))
        assert len(findings) == 1
        assert "extra" in findings[0].finding

    def test_ruby_yard_style_multi_line_comment(self, tmp_path: Path) -> None:
        # Ruby splits multi-line doc comments into separate consecutive
        # `comment` sibling nodes, and wraps `def` in a `body_statement`
        # node whose sibling (not the def's) is the comment — both verified
        # against a real parse this session.
        f = tmp_path / "sample.rb"
        f.write_text("""\
class Sample
  # @param amount [Integer] the amount
  # @param extra [String] unused
  def transfer(amount)
    amount
  end
end
""")
        findings = _tree_sitter_api_findings(str(f))
        assert len(findings) == 1
        assert "extra" in findings[0].finding

    def test_cpp_wrapper_node_param_extraction(self, tmp_path: Path) -> None:
        f = tmp_path / "sample.cpp"
        f.write_text("""\
class Sample {
public:
    /**
     * @param extra unused
     */
    int transfer(int amount) {
        return amount;
    }
};
""")
        findings = _tree_sitter_api_findings(str(f))
        messages = {x.finding for x in findings}
        assert any("extra" in m for m in messages)
        assert any("amount" in m for m in messages)

    def test_kotlin_wrapper_node_param_extraction(self, tmp_path: Path) -> None:
        f = tmp_path / "sample.kt"
        f.write_text("""\
class Sample {
    /**
     * @param extra unused
     */
    fun transfer(amount: Int): Int {
        return amount
    }
}
""")
        findings = _tree_sitter_api_findings(str(f))
        messages = {x.finding for x in findings}
        assert any("extra" in m for m in messages)
        assert any("amount" in m for m in messages)

    def test_scala_wrapper_node_param_extraction(self, tmp_path: Path) -> None:
        f = tmp_path / "sample.scala"
        f.write_text("""\
class Sample {
  /**
   * @param extra unused
   */
  def transfer(amount: Int): Int = {
    amount
  }
}
""")
        findings = _tree_sitter_api_findings(str(f))
        messages = {x.finding for x in findings}
        assert any("extra" in m for m in messages)
        assert any("amount" in m for m in messages)

    def test_php_is_never_checked(self, tmp_path: Path) -> None:
        # PHP is deliberately excluded — phpcs already covers this via
        # Drupal.Commenting.FunctionComment / Squiz.Commenting.FunctionComment.
        f = tmp_path / "sample.php"
        f.write_text("<?php\n/**\n * @param $extra unused\n */\nfunction transfer($amount) {\n    return $amount;\n}\n")
        assert _tree_sitter_api_findings(str(f)) == []


class TestPythonPaths:
    def test_api_check_isolated_flag_present(self, tmp_path: Path) -> None:
        f = tmp_path / "a.py"
        f.write_text("def foo():\n    pass\n")
        cf = _make_cf(python=[str(f)])
        with (
            patch("ai_pr_review.analyzers.native.docs_comments.shutil.which", return_value="/usr/bin/ruff"),
            patch("ai_pr_review.analyzers.native.docs_comments.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="[]", stderr="")
            _run_docs_api_check(cf, Path("/dev/null"))
        call_args = mock_run.call_args[0][0]
        assert "--isolated" in call_args
        assert "--preview" in call_args
        assert any(a.startswith("--select=D417") for a in call_args)

    def test_api_check_binary_absent_returns_empty(self, tmp_path: Path) -> None:
        f = tmp_path / "a.py"
        f.write_text("def foo():\n    pass\n")
        cf = _make_cf(python=[str(f)])
        with patch("ai_pr_review.analyzers.native.docs_comments.shutil.which", return_value=None):
            assert _run_docs_api_check(cf, Path("/dev/null")) == []

    def test_api_check_parses_ruff_output(self, tmp_path: Path) -> None:
        f = tmp_path / "a.py"
        f.write_text("def foo():\n    pass\n")
        cf = _make_cf(python=[str(f)])
        payload = json.dumps([{
            "code": "DOC102", "filename": str(f),
            "location": {"row": 3, "column": 1},
            "message": "Documented parameter `currency` is not in the function's signature",
        }])
        with (
            patch("ai_pr_review.analyzers.native.docs_comments.shutil.which", return_value="/usr/bin/ruff"),
            patch("ai_pr_review.analyzers.native.docs_comments.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout=payload, stderr="")
            findings = _run_docs_api_check(cf, Path("/dev/null"))
        assert len(findings) == 1
        assert findings[0].severity == "Medium"
        assert findings[0].confidence == 90
        assert findings[0].source == "docs-api-check"
        assert findings[0].line == 3

    def test_ruff_timeout_returns_none_gracefully(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        import subprocess as sp
        f = tmp_path / "a.py"
        f.write_text("def foo():\n    pass\n")
        cf = _make_cf(python=[str(f)])
        with (
            patch("ai_pr_review.analyzers.native.docs_comments.shutil.which", return_value="/usr/bin/ruff"),
            patch(
                "ai_pr_review.analyzers.native.docs_comments.subprocess.run",
                side_effect=sp.TimeoutExpired(cmd="ruff", timeout=120),
            ),
            caplog.at_level("WARNING"),
        ):
            findings = _run_docs_api_check(cf, Path("/dev/null"))
        assert findings == []
        assert "timed out" in caplog.text

    def test_ruff_bad_returncode_returns_empty(self, tmp_path: Path) -> None:
        f = tmp_path / "a.py"
        f.write_text("def foo():\n    pass\n")
        cf = _make_cf(python=[str(f)])
        with (
            patch("ai_pr_review.analyzers.native.docs_comments.shutil.which", return_value="/usr/bin/ruff"),
            patch("ai_pr_review.analyzers.native.docs_comments.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=2, stdout="", stderr="bad invocation")
            findings = _run_docs_api_check(cf, Path("/dev/null"))
        assert findings == []


class TestBridgeIntegration:
    @pytest.mark.anyio
    async def test_docs_api_check_uses_native_fn(self) -> None:
        from ai_pr_review.analyzers import bridge
        from ai_pr_review.analyzers.bridge import AnalyzerSpec, run_analyzers

        called = []

        def fake_native(changed_files: ChangedFiles, diff_file: Path) -> list:
            called.append(True)
            return []

        spec = AnalyzerSpec("docs-api-check", ["source"], fake_native)
        cf = ChangedFiles(all_files=["a.js"], source=["a.js"])
        with patch.object(bridge, "_ANALYZERS", [spec]):
            await run_analyzers(cf, "/dev/null")
        assert called

    @pytest.mark.anyio
    async def test_analyzer_skipped_when_no_eligible_files(self) -> None:
        # Generic required_file_types gating (ai_pr_review.analyzers.bridge's
        # _is_eligible), not specific to any one analyzer -- exercised here
        # with a fake spec rather than a real one so this test doesn't need
        # updating whenever the real analyzer roster changes. Named
        # "docs-missing-check" until #815 removed that real analyzer; kept
        # as a fake name here since the behavior under test (an analyzer
        # gated on "source" files is skipped when ChangedFiles has none) is
        # unrelated to that removal.
        from ai_pr_review.analyzers import bridge
        from ai_pr_review.analyzers.bridge import AnalyzerSpec, run_analyzers

        called = []

        def fake_native(changed_files: ChangedFiles, diff_file: Path) -> list:
            called.append(True)
            return []

        spec = AnalyzerSpec("fake-source-gated-analyzer", ["source"], fake_native)
        with patch.object(bridge, "_ANALYZERS", [spec]):
            await run_analyzers(ChangedFiles(), "/dev/null")
        assert not called
