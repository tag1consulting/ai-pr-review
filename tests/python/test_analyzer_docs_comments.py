"""Tests for the docs-api-check and docs-missing-check native analyzers."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ai_pr_review.analyzers.native.docs_comments import (
    _doc_param_names,
    _run_docs_api_check,
    _run_docs_missing_check,
    _tree_sitter_api_findings,
    _tree_sitter_missing_findings,
)
from ai_pr_review.diff.linemap import LineRef
from ai_pr_review.manifest import ChangedFiles


def _make_cf(**kwargs: list[str]) -> ChangedFiles:
    all_files = [f for files in kwargs.values() for f in files]
    return ChangedFiles(all_files=all_files, **kwargs)


def _added_lines(path: str, lines: range) -> set[LineRef]:
    return {LineRef(path, i) for i in lines}


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


class TestTreeSitterMissingFindings:
    def test_new_undocumented_public_function_flagged(self, tmp_path: Path) -> None:
        f = tmp_path / "sample.js"
        f.write_text("function transfer(amount) {\n    return amount;\n}\n")
        findings = _tree_sitter_missing_findings(str(f), _added_lines(str(f), range(1, 4)))
        assert len(findings) == 1
        assert findings[0].severity == "Low"
        assert findings[0].confidence == 80
        assert findings[0].source == "docs-missing-check"
        assert findings[0].category == "docs"

    def test_documented_function_not_flagged(self, tmp_path: Path) -> None:
        f = tmp_path / "sample.js"
        f.write_text("/** doc */\nfunction transfer(amount) {\n    return amount;\n}\n")
        findings = _tree_sitter_missing_findings(str(f), _added_lines(str(f), range(1, 5)))
        assert findings == []

    def test_out_of_diff_function_not_flagged(self, tmp_path: Path) -> None:
        # Diff-gating: an undocumented function whose def-line was NOT
        # added in this diff must not be flagged, even if it is genuinely
        # undocumented — this is what keeps the check from nagging about
        # pre-existing code.
        f = tmp_path / "sample.js"
        f.write_text("function transfer(amount) {\n    return amount;\n}\n")
        findings = _tree_sitter_missing_findings(str(f), added_lines=set())
        assert findings == []

    def test_private_convention_underscore_not_flagged(self, tmp_path: Path) -> None:
        f = tmp_path / "sample.js"
        f.write_text("function _helper(amount) {\n    return amount;\n}\n")
        findings = _tree_sitter_missing_findings(str(f), _added_lines(str(f), range(1, 4)))
        assert findings == []

    def test_java_private_modifier_not_flagged(self, tmp_path: Path) -> None:
        f = tmp_path / "Sample.java"
        f.write_text("public class Sample {\n    private int helper(int x) {\n        return x;\n    }\n}\n")
        findings = _tree_sitter_missing_findings(str(f), _added_lines(str(f), range(1, 6)))
        assert findings == []

    def test_java_public_method_flagged(self, tmp_path: Path) -> None:
        f = tmp_path / "Sample.java"
        f.write_text("public class Sample {\n    public int transfer(int x) {\n        return x;\n    }\n}\n")
        findings = _tree_sitter_missing_findings(str(f), _added_lines(str(f), range(1, 6)))
        assert len(findings) == 1

    def test_cpp_private_section_not_flagged(self, tmp_path: Path) -> None:
        f = tmp_path / "sample.cpp"
        f.write_text("class Sample {\npublic:\n    int transfer(int amount) {\n        return amount;\n    }\nprivate:\n    int helper(int x) {\n        return x;\n    }\n};\n")
        added = _added_lines(str(f), range(1, 12))
        findings = _tree_sitter_missing_findings(str(f), added)
        # transfer() is public+undocumented -> flagged; helper() is
        # private+undocumented -> not flagged.
        assert len(findings) == 1


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

    def test_missing_check_diff_gates_ruff_findings(self, tmp_path: Path) -> None:
        f = tmp_path / "a.py"
        f.write_text("def foo():\n    pass\n")
        cf = _make_cf(python=[str(f)])
        payload = json.dumps([
            {"code": "D103", "filename": str(f), "location": {"row": 1, "column": 1}, "message": "Missing docstring"},
            {"code": "D103", "filename": str(f), "location": {"row": 99, "column": 1}, "message": "Missing docstring"},
        ])
        diff_file = tmp_path / "the.diff"
        diff_file.write_text(f"diff --git a/{f} b/{f}\n--- /dev/null\n+++ b/{f}\n@@ -0,0 +1,2 @@\n+def foo():\n+    pass\n")
        with (
            patch("ai_pr_review.analyzers.native.docs_comments.shutil.which", return_value="/usr/bin/ruff"),
            patch("ai_pr_review.analyzers.native.docs_comments.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout=payload, stderr="")
            findings = _run_docs_missing_check(cf, diff_file)
        # Only the row-1 finding is on an added line; row 99 is not.
        assert len(findings) == 1
        assert findings[0].line == 1
        assert findings[0].severity == "Low"

    def test_missing_check_no_added_lines_short_circuits(self, tmp_path: Path) -> None:
        f = tmp_path / "a.py"
        f.write_text("def foo():\n    pass\n")
        cf = _make_cf(python=[str(f)])
        diff_file = tmp_path / "empty.diff"
        diff_file.write_text("")
        with patch("ai_pr_review.analyzers.native.docs_comments.subprocess.run") as mock_run:
            findings = _run_docs_missing_check(cf, diff_file)
        assert findings == []
        mock_run.assert_not_called()

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


class TestGoPath:
    def _json_path_writer(self, content: str, returncode: int = 0):
        def _run(args: list[str], **kwargs: object) -> MagicMock:
            flag = next(a for a in args if a.startswith("--output.json.path="))
            Path(flag.removeprefix("--output.json.path=")).write_text(content)
            return MagicMock(returncode=returncode, stdout="", stderr="")
        return _run

    def test_uses_enable_only_godoclint(self, tmp_path: Path) -> None:
        go_mod = tmp_path / "go.mod"
        go_mod.write_text("module test\n\ngo 1.21\n")
        go_file = tmp_path / "main.go"
        go_file.write_text("package main\n")
        cf = _make_cf(go=[str(go_file)])
        diff_file = tmp_path / "the.diff"
        diff_file.write_text(f"diff --git a/{go_file} b/{go_file}\n--- /dev/null\n+++ b/{go_file}\n@@ -0,0 +1,1 @@\n+package main\n")
        with (
            patch("ai_pr_review.analyzers.native.docs_comments.shutil.which", return_value="/usr/bin/golangci-lint"),
            patch(
                "ai_pr_review.analyzers.native.docs_comments.subprocess.run",
                side_effect=self._json_path_writer('{"Issues": []}'),
            ) as mock_run,
        ):
            _run_docs_missing_check(cf, diff_file)
        call_args = mock_run.call_args[0][0]
        assert "--enable-only=godoclint" in call_args
        assert any(a.startswith("--output.json.path=") for a in call_args)
        assert not any(a.startswith("--out-format") for a in call_args)

    def test_config_enables_require_doc_and_lives_in_module_root(self, tmp_path: Path) -> None:
        # require-doc is a "Strict"-tier godoclint rule, off even when the
        # linter itself is enabled — verified empirically this session that
        # --enable-only=godoclint alone produces zero findings on a fully
        # undocumented exported function. Also verified: golangci-lint
        # computes each issue's reported Pos.Filename relative to the
        # --config file's own directory when it differs from the module
        # root, so the config file must live INSIDE module_root or
        # diff-gating's path lookup silently breaks.
        go_mod = tmp_path / "go.mod"
        go_mod.write_text("module test\n\ngo 1.21\n")
        go_file = tmp_path / "main.go"
        go_file.write_text("package main\n")
        cf = _make_cf(go=[str(go_file)])
        diff_file = tmp_path / "the.diff"
        diff_file.write_text(f"diff --git a/{go_file} b/{go_file}\n--- /dev/null\n+++ b/{go_file}\n@@ -0,0 +1,1 @@\n+package main\n")
        captured: dict[str, object] = {}

        def _run(args: list[str], **kwargs: object) -> MagicMock:
            config_flag = next(a for a in args if a.startswith("--config="))
            config_path = Path(config_flag.removeprefix("--config="))
            # Must read the config's location and content DURING the call —
            # it is a delete-on-close NamedTemporaryFile, gone by the time
            # _run_docs_missing_check returns.
            captured["parent"] = config_path.parent
            captured["content"] = config_path.read_text()
            json_flag = next(a for a in args if a.startswith("--output.json.path="))
            Path(json_flag.removeprefix("--output.json.path=")).write_text('{"Issues": []}')
            return MagicMock(returncode=0, stdout="", stderr="")

        with (
            patch("ai_pr_review.analyzers.native.docs_comments.shutil.which", return_value="/usr/bin/golangci-lint"),
            patch("ai_pr_review.analyzers.native.docs_comments.subprocess.run", side_effect=_run),
        ):
            _run_docs_missing_check(cf, diff_file)
        assert captured["parent"] == tmp_path
        assert "require-doc" in captured["content"]

    def test_parses_and_diff_gates_godoclint_issues(self, tmp_path: Path) -> None:
        go_mod = tmp_path / "go.mod"
        go_mod.write_text("module test\n\ngo 1.21\n")
        go_file = tmp_path / "main.go"
        go_file.write_text("package main\n\nfunc Transfer() {}\n")
        cf = _make_cf(go=[str(go_file)])
        diff_file = tmp_path / "the.diff"
        diff_file.write_text(f"diff --git a/{go_file} b/{go_file}\n--- /dev/null\n+++ b/{go_file}\n@@ -0,0 +1,3 @@\n+package main\n+\n+func Transfer() {{}}\n")
        payload = json.dumps({"Issues": [{
            "FromLinter": "godoclint",
            "Text": "exported function Transfer should have a doc comment",
            "Pos": {"Filename": "main.go", "Line": 3, "Column": 1},
        }]})
        with (
            patch("ai_pr_review.analyzers.native.docs_comments.shutil.which", return_value="/usr/bin/golangci-lint"),
            patch(
                "ai_pr_review.analyzers.native.docs_comments.subprocess.run",
                side_effect=self._json_path_writer(payload),
            ),
        ):
            findings = _run_docs_missing_check(cf, diff_file)
        assert len(findings) == 1
        assert findings[0].severity == "Low"
        assert findings[0].source == "docs-missing-check"
        assert findings[0].category == "docs"

    def test_no_go_mod_skips_gracefully(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        go_file = tmp_path / "main.go"
        go_file.write_text("package main\n")
        cf = _make_cf(go=[str(go_file)])
        diff_file = tmp_path / "the.diff"
        diff_file.write_text(f"diff --git a/{go_file} b/{go_file}\n--- /dev/null\n+++ b/{go_file}\n@@ -0,0 +1,1 @@\n+package main\n")
        with (
            patch("ai_pr_review.analyzers.native.docs_comments.shutil.which", return_value="/usr/bin/golangci-lint"),
            caplog.at_level("WARNING"),
        ):
            findings = _run_docs_missing_check(cf, diff_file)
        assert findings == []
        assert "go.mod" in caplog.text

    def test_binary_absent_returns_empty(self, tmp_path: Path) -> None:
        go_mod = tmp_path / "go.mod"
        go_mod.write_text("module test\n\ngo 1.21\n")
        go_file = tmp_path / "main.go"
        go_file.write_text("package main\n")
        cf = _make_cf(go=[str(go_file)])
        diff_file = tmp_path / "the.diff"
        diff_file.write_text(f"diff --git a/{go_file} b/{go_file}\n--- /dev/null\n+++ b/{go_file}\n@@ -0,0 +1,1 @@\n+package main\n")
        with patch("ai_pr_review.analyzers.native.docs_comments.shutil.which", return_value=None):
            assert _run_docs_missing_check(cf, diff_file) == []


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
    async def test_docs_missing_check_skipped_when_no_source_files(self) -> None:
        from ai_pr_review.analyzers import bridge
        from ai_pr_review.analyzers.bridge import AnalyzerSpec, run_analyzers

        called = []

        def fake_native(changed_files: ChangedFiles, diff_file: Path) -> list:
            called.append(True)
            return []

        spec = AnalyzerSpec("docs-missing-check", ["source"], fake_native)
        with patch.object(bridge, "_ANALYZERS", [spec]):
            await run_analyzers(ChangedFiles(), "/dev/null")
        assert not called
