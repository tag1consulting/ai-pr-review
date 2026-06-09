"""Tests for ai_pr_review.manifest."""


from ai_pr_review.manifest import (
    ChangedFiles,
    build_changed_files,
    build_manifest_text,
    parse_changed_files_payload,
)


class TestBuildChangedFiles:
    def test_empty_list(self) -> None:
        cf = build_changed_files([])
        assert cf.all_files == []
        assert cf.source == []

    def test_python_source_file(self) -> None:
        cf = build_changed_files(["src/main.py"])
        assert "src/main.py" in cf.source
        assert "src/main.py" in cf.python
        assert cf.shell == []

    def test_shell_file(self) -> None:
        cf = build_changed_files(["review.sh"])
        assert "review.sh" in cf.shell

    def test_go_file(self) -> None:
        cf = build_changed_files(["cmd/main.go"])
        assert "cmd/main.go" in cf.go

    def test_php_file(self) -> None:
        cf = build_changed_files(["src/Controller.php"])
        assert "src/Controller.php" in cf.php

    def test_terraform_file(self) -> None:
        cf = build_changed_files(["infra/main.tf"])
        assert "infra/main.tf" in cf.terraform

    def test_js_ts_file(self) -> None:
        cf = build_changed_files(["app/index.ts", "app/util.js"])
        assert "app/index.ts" in cf.js_ts
        assert "app/util.js" in cf.js_ts

    def test_dockerfile(self) -> None:
        cf = build_changed_files(["Dockerfile"])
        assert "Dockerfile" in cf.dockerfile
        assert "Dockerfile" in cf.config

    def test_dockerfile_variant(self) -> None:
        cf = build_changed_files(["Dockerfile.prod"])
        assert "Dockerfile.prod" in cf.dockerfile

    def test_manifest_lockfile(self) -> None:
        cf = build_changed_files(["package.json", "go.mod", "composer.lock"])
        assert "package.json" in cf.manifest_lockfile
        assert "go.mod" in cf.manifest_lockfile
        assert "composer.lock" in cf.manifest_lockfile
        # manifest files go to deps, not source
        assert cf.source == []

    def test_doc_file(self) -> None:
        cf = build_changed_files(["README.md", "docs/guide.rst"])
        assert "README.md" in cf.docs
        assert "docs/guide.rst" in cf.docs

    def test_test_file_detection(self) -> None:
        cf = build_changed_files(["tests/test_foo.py"])
        assert "tests/test_foo.py" in cf.tests
        assert "tests/test_foo.py" not in cf.source

    def test_option_like_path_dropped(self) -> None:
        # Argument-injection guard: a changed-file path whose component begins
        # with '-' would be parsed as a CLI flag by an analyzer, so it must be
        # excluded from all categorized lists, including all_files.
        cf = build_changed_files(["--autoload-file=evil.php", "src/ok.py"])
        assert "--autoload-file=evil.php" not in cf.all_files
        assert cf.php == []
        assert "src/ok.py" in cf.all_files
        assert "src/ok.py" in cf.python

    def test_option_like_path_nested_component_dropped(self) -> None:
        # A dash-leading component anywhere in the path is rejected, since the
        # full path string reaches analyzer argv verbatim.
        cf = build_changed_files(["src/--plugin=x.js", "./-rf.go"])
        assert cf.all_files == []
        assert cf.js_ts == []
        assert cf.go == []

    def test_leading_dot_slash_path_kept(self) -> None:
        # A normal relative path with a leading './' is not option-like.
        cf = build_changed_files(["./src/main.py"])
        assert "./src/main.py" in cf.all_files

    def test_iac_kubernetes_yaml(self) -> None:
        cf = build_changed_files(["k8s/deployment.yaml"])
        assert "k8s/deployment.yaml" in cf.iac

    def test_iac_helm(self) -> None:
        cf = build_changed_files(["charts/values.yaml"])
        assert "charts/values.yaml" in cf.iac

    def test_non_iac_yaml(self) -> None:
        cf = build_changed_files([".github/workflows/ci.yml"])
        assert cf.iac == []

    def test_config_yaml(self) -> None:
        cf = build_changed_files([".github/workflows/ci.yml"])
        assert ".github/workflows/ci.yml" in cf.config

    def test_all_files_populated(self) -> None:
        files = ["src/main.py", "tests/test_main.py", "README.md"]
        cf = build_changed_files(files)
        assert cf.all_files == files


class TestChangedFilesLanguages:
    def test_no_files(self) -> None:
        cf = ChangedFiles()
        assert cf.languages == []

    def test_single_language(self) -> None:
        cf = ChangedFiles(all_files=["foo.py", "bar.py"])
        assert cf.languages == ["Python"]

    def test_multiple_languages_deduped(self) -> None:
        cf = ChangedFiles(all_files=["foo.py", "bar.go", "baz.py"])
        langs = cf.languages
        assert "Python" in langs
        assert "Go" in langs
        assert langs.count("Python") == 1

    def test_unknown_extension_excluded(self) -> None:
        cf = ChangedFiles(all_files=["file.unknown"])
        assert cf.languages == []


class TestBuildManifestText:
    def _make_cf(self, **kwargs: list[str]) -> ChangedFiles:
        all_files: list[str] = []
        for v in kwargs.values():
            all_files.extend(v)
        return ChangedFiles(all_files=all_files, **kwargs)

    def test_basic_output_structure(self) -> None:
        cf = build_changed_files(["src/main.py"])
        text = build_manifest_text(cf, "main", "HEAD~1..HEAD", "+10/-2")
        assert "BASE: main" in text
        assert "DIFF: HEAD~1..HEAD" in text
        assert "FILES: 1" in text
        assert "+10/-2" in text

    def test_source_section(self) -> None:
        cf = build_changed_files(["src/foo.py", "src/bar.py"])
        text = build_manifest_text(cf, "main", "HEAD~1..HEAD", "+5/-1")
        assert "Source:" in text
        assert "src/foo.py" in text

    def test_no_source_no_section(self) -> None:
        cf = build_changed_files(["README.md"])
        text = build_manifest_text(cf, "main", "HEAD~1..HEAD", "+1/-0")
        assert "Source:" not in text

    def test_tests_section(self) -> None:
        cf = build_changed_files(["tests/test_foo.py"])
        text = build_manifest_text(cf, "main", "HEAD~1..HEAD", "+5/-0")
        assert "Tests:" in text

    def test_docs_section(self) -> None:
        cf = build_changed_files(["docs/guide.md"])
        text = build_manifest_text(cf, "main", "HEAD~1..HEAD", "+3/-0")
        assert "Docs:" in text

    def test_languages_in_header(self) -> None:
        cf = build_changed_files(["src/main.py"])
        text = build_manifest_text(cf, "main", "HEAD~1..HEAD", "+5/-0")
        assert "Python" in text

    def test_unknown_language_fallback(self) -> None:
        cf = build_changed_files(["file.unknown"])
        text = build_manifest_text(cf, "main", "HEAD~1..HEAD", "+1/-0")
        assert "unknown" in text


class TestParseChangedFilesPayload:
    """parse_changed_files_payload normalisation."""

    def test_none_entry_is_skipped(self) -> None:
        """None entries must not produce a 'None' string path (regression for str(None) bug)."""
        result = parse_changed_files_payload([None, "src/main.py"])
        assert "None" not in result.all_files
        assert "src/main.py" in result.all_files

    def test_dict_entry_with_path_key(self) -> None:
        result = parse_changed_files_payload([{"path": "a.py"}, "b.py"])
        assert "a.py" in result.all_files
        assert "b.py" in result.all_files

    def test_empty_path_dict_is_skipped(self) -> None:
        result = parse_changed_files_payload([{"path": ""}, "c.py"])
        assert "" not in result.all_files
        assert "c.py" in result.all_files
