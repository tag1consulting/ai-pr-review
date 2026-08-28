"""Tests for the native docs-ref-check analyzer."""

from __future__ import annotations

from pathlib import Path

from ai_pr_review.analyzers.native.docs_refs import _run_docs_ref_check
from ai_pr_review.manifest import ChangedFiles


def _make_cf(docs: list[str]) -> ChangedFiles:
    return ChangedFiles(all_files=docs, docs=docs)


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


class TestScopeGuards:
    def test_empty_docs_returns_empty(self) -> None:
        cf = _make_cf([])
        assert _run_docs_ref_check(cf, Path("/dev/null")) == []

    def test_deleted_file_skipped_without_crashing(self, tmp_path: Path) -> None:
        ghost = tmp_path / "ghost.md"
        cf = _make_cf([str(ghost)])
        assert _run_docs_ref_check(cf, Path("/dev/null")) == []

    def test_non_md_doc_file_not_parsed(self, tmp_path: Path) -> None:
        txt = _write(tmp_path / "notes.txt", "[broken](nonexistent-target-xyz)\n")
        cf = _make_cf([str(txt)])
        assert _run_docs_ref_check(cf, Path("/dev/null")) == []


class TestLinkResolution:
    def test_working_relative_link_no_finding(self, tmp_path: Path) -> None:
        _write(tmp_path / "other.md", "# Other\n")
        doc = _write(tmp_path / "doc.md", "[link](other.md)\n")
        cf = _make_cf([str(doc)])
        assert _run_docs_ref_check(cf, Path("/dev/null")) == []

    def test_md_append_candidate(self, tmp_path: Path) -> None:
        # Proves append-not-replace: target has no extension at all, and the
        # real file on disk only exists once ".md" is appended.
        _write(tmp_path / "version-history" / "v2.5.0.md", "# v2.5.0\n")
        doc = _write(tmp_path / "doc.md", "[link](version-history/v2.5.0)\n")
        cf = _make_cf([str(doc)])
        assert _run_docs_ref_check(cf, Path("/dev/null")) == []

    def test_markdown_append_candidate(self, tmp_path: Path) -> None:
        _write(tmp_path / "other.markdown", "# Other\n")
        doc = _write(tmp_path / "doc.md", "[link](other)\n")
        cf = _make_cf([str(doc)])
        assert _run_docs_ref_check(cf, Path("/dev/null")) == []

    def test_index_md_candidate(self, tmp_path: Path) -> None:
        _write(tmp_path / "sub" / "index.md", "# Sub\n")
        doc = _write(tmp_path / "doc.md", "[link](sub)\n")
        cf = _make_cf([str(doc)])
        assert _run_docs_ref_check(cf, Path("/dev/null")) == []

    def test_readme_md_candidate(self, tmp_path: Path) -> None:
        _write(tmp_path / "sub" / "README.md", "# Sub\n")
        doc = _write(tmp_path / "doc.md", "[link](sub)\n")
        cf = _make_cf([str(doc)])
        assert _run_docs_ref_check(cf, Path("/dev/null")) == []

    def test_html_to_md_replace_candidate(self, tmp_path: Path) -> None:
        _write(tmp_path / "other.md", "# Other\n")
        doc = _write(tmp_path / "doc.md", "[link](other.html)\n")
        cf = _make_cf([str(doc)])
        assert _run_docs_ref_check(cf, Path("/dev/null")) == []

    def test_broken_link_emits_finding(self, tmp_path: Path) -> None:
        doc = _write(tmp_path / "doc.md", "text\n[link](nowhere.md)\n")
        cf = _make_cf([str(doc)])
        findings = _run_docs_ref_check(cf, Path("/dev/null"))
        assert len(findings) == 1
        f = findings[0]
        assert f.severity == "Medium"
        assert f.confidence == 80
        assert f.source == "docs-ref-check"
        assert f.category == "docs"
        assert f.file == str(doc)
        assert f.line == 2
        assert f.finding == "Link target 'nowhere.md' does not resolve to an existing file"
        assert f.remediation == "Fix or remove the broken link."

    def test_leading_dot_slash_stripped_from_file(self, tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.chdir(tmp_path)
        _write(tmp_path / "doc.md", "[link](nowhere.md)\n")
        cf = _make_cf(["./doc.md"])
        findings = _run_docs_ref_check(cf, Path("/dev/null"))
        assert len(findings) == 1
        assert findings[0].file == "doc.md"


class TestSkippedSpans:
    def test_link_in_fenced_code_block_skipped(self, tmp_path: Path) -> None:
        doc = _write(
            tmp_path / "doc.md",
            "```python\n[link](inside-fence.md)\n```\n[link](outside-fence.md)\n",
        )
        cf = _make_cf([str(doc)])
        findings = _run_docs_ref_check(cf, Path("/dev/null"))
        assert len(findings) == 1
        assert "outside-fence.md" in findings[0].finding
        assert findings[0].line == 4

    def test_link_in_inline_code_span_skipped(self, tmp_path: Path) -> None:
        doc = _write(
            tmp_path / "doc.md",
            "See `[link](inside-code.md)` for details.\n[link](outside-code.md)\n",
        )
        cf = _make_cf([str(doc)])
        findings = _run_docs_ref_check(cf, Path("/dev/null"))
        assert len(findings) == 1
        assert "outside-code.md" in findings[0].finding
        assert findings[0].line == 2

    def test_http_link_skipped(self, tmp_path: Path) -> None:
        doc = _write(tmp_path / "doc.md", "[link](http://example.com)\n")
        cf = _make_cf([str(doc)])
        assert _run_docs_ref_check(cf, Path("/dev/null")) == []

    def test_mailto_link_skipped(self, tmp_path: Path) -> None:
        doc = _write(tmp_path / "doc.md", "[link](mailto:x@y.com)\n")
        cf = _make_cf([str(doc)])
        assert _run_docs_ref_check(cf, Path("/dev/null")) == []

    def test_template_placeholder_skipped(self, tmp_path: Path) -> None:
        doc = _write(tmp_path / "doc.md", "[link]({{ site.baseurl }}/foo)\n")
        cf = _make_cf([str(doc)])
        assert _run_docs_ref_check(cf, Path("/dev/null")) == []


class TestAnchorResolution:
    def test_anchor_matches_auto_slug(self, tmp_path: Path) -> None:
        doc = _write(tmp_path / "doc.md", "# My Heading\n\n[link](#my-heading)\n")
        cf = _make_cf([str(doc)])
        assert _run_docs_ref_check(cf, Path("/dev/null")) == []

    def test_anchor_matches_explicit_custom_id(self, tmp_path: Path) -> None:
        # Exact live shape from docs/slash-commands.md:39.
        content = (
            "### 2. Add a GH_TOKEN secret {#pat-requirement}\n"
            "\n"
            "[link](#pat-requirement)\n"
        )
        doc = _write(tmp_path / "doc.md", content)
        cf = _make_cf([str(doc)])
        assert _run_docs_ref_check(cf, Path("/dev/null")) == []

    def test_custom_id_does_not_replace_auto_slug(self, tmp_path: Path) -> None:
        # The auto-generated slug must still be valid alongside the custom id.
        content = (
            "### 2. Add a GH_TOKEN secret {#pat-requirement}\n"
            "\n"
            "[link](#2-add-a-ghtoken-secret)\n"
        )
        doc = _write(tmp_path / "doc.md", content)
        cf = _make_cf([str(doc)])
        assert _run_docs_ref_check(cf, Path("/dev/null")) == []

    def test_broken_anchor_emits_finding(self, tmp_path: Path) -> None:
        doc = _write(tmp_path / "doc.md", "# My Heading\n\n[link](#nonexistent)\n")
        cf = _make_cf([str(doc)])
        findings = _run_docs_ref_check(cf, Path("/dev/null"))
        assert len(findings) == 1
        f = findings[0]
        assert f.severity == "Medium"
        assert f.confidence == 80
        assert f.source == "docs-ref-check"
        assert f.category == "docs"
        assert f.line == 3
        assert f.finding == "Anchor '#nonexistent' does not match any heading in the target file"
        assert f.remediation == "Fix the anchor or update the target heading."

    def test_top_anchor_always_passes(self, tmp_path: Path) -> None:
        doc = _write(tmp_path / "doc.md", "[link](#top)\n")
        cf = _make_cf([str(doc)])
        assert _run_docs_ref_check(cf, Path("/dev/null")) == []

    def test_bare_hash_always_passes(self, tmp_path: Path) -> None:
        doc = _write(tmp_path / "doc.md", "[link](#)\n")
        cf = _make_cf([str(doc)])
        assert _run_docs_ref_check(cf, Path("/dev/null")) == []

    def test_duplicate_headings_get_suffixes(self, tmp_path: Path) -> None:
        content = (
            "## Foo\n\n## Foo\n\n## Foo\n\n"
            "[a](#foo)\n[b](#foo-1)\n[c](#foo-2)\n"
        )
        doc = _write(tmp_path / "doc.md", content)
        cf = _make_cf([str(doc)])
        assert _run_docs_ref_check(cf, Path("/dev/null")) == []

    def test_duplicate_heading_third_slug_not_reused_as_first(self, tmp_path: Path) -> None:
        content = "## Foo\n\n## Foo\n\n[bad](#foo-2)\n"
        doc = _write(tmp_path / "doc.md", content)
        cf = _make_cf([str(doc)])
        findings = _run_docs_ref_check(cf, Path("/dev/null"))
        assert len(findings) == 1
        assert "#foo-2" in findings[0].finding

    def test_fragment_only_link_resolves_against_referencing_file(self, tmp_path: Path) -> None:
        # file_a lacks the "Beta" heading that only file_b has: a fragment-only
        # link in file_a must be checked against file_a, not file_b.
        _write(tmp_path / "file_b.md", "## Beta\n")
        file_a = _write(tmp_path / "file_a.md", "## Alpha\n\n[link](#beta)\n")
        cf = _make_cf([str(file_a), str(tmp_path / "file_b.md")])
        findings = _run_docs_ref_check(cf, Path("/dev/null"))
        assert len(findings) == 1
        assert findings[0].file == str(file_a)
        assert "#beta" in findings[0].finding

    def test_fragment_only_link_matches_own_heading(self, tmp_path: Path) -> None:
        doc = _write(tmp_path / "doc.md", "## Alpha\n\n[link](#alpha)\n")
        cf = _make_cf([str(doc)])
        assert _run_docs_ref_check(cf, Path("/dev/null")) == []


class TestLiveSanity:
    def test_version_history_docs_produce_few_findings(self) -> None:
        """Manual sanity check against this repo's real docs/version-history.md."""
        repo_root = Path(__file__).parent.parent.parent
        target = repo_root / "docs" / "version-history.md"
        if not target.is_file():
            return
        rel = str(target.relative_to(repo_root))
        cf = _make_cf([rel])
        import os

        cwd = os.getcwd()
        os.chdir(repo_root)
        try:
            findings = _run_docs_ref_check(cf, Path("/dev/null"))
        finally:
            os.chdir(cwd)
        assert len(findings) < 10
