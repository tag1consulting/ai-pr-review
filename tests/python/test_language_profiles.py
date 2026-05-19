"""Tests for ai_pr_review.language_profiles.load_language_profiles."""

from pathlib import Path

from ai_pr_review.language_profiles import load_language_profiles


def _write_profile(tmp_path: Path, filename: str, content: str) -> None:
    profiles_dir = tmp_path / "language-profiles"
    profiles_dir.mkdir(exist_ok=True)
    (profiles_dir / filename).write_text(content, encoding="utf-8")


class TestLoadLanguageProfiles:
    def test_single_profile_found(self, tmp_path: Path) -> None:
        _write_profile(tmp_path, "python.md", "## Python context\n- some rule")
        result = load_language_profiles(["Python"], tmp_path)
        assert "## Python context" in result

    def test_missing_profile_silently_skipped(self, tmp_path: Path) -> None:
        (tmp_path / "language-profiles").mkdir()
        result = load_language_profiles(["JavaScript"], tmp_path)
        assert result == ""

    def test_no_exception_on_empty_profiles_dir(self, tmp_path: Path) -> None:
        (tmp_path / "language-profiles").mkdir()
        result = load_language_profiles(["Go", "Rust"], tmp_path)
        assert result == ""

    def test_no_exception_when_profiles_dir_absent(self, tmp_path: Path) -> None:
        result = load_language_profiles(["Go"], tmp_path)
        assert result == ""

    def test_cpp_label_resolves_to_cpp_dot_md(self, tmp_path: Path) -> None:
        _write_profile(tmp_path, "c++.md", "## C++ context\n- some rule")
        result = load_language_profiles(["C++"], tmp_path)
        assert "## C++ context" in result

    def test_multiple_profiles_concatenated_in_order(self, tmp_path: Path) -> None:
        _write_profile(tmp_path, "go.md", "## Go context")
        _write_profile(tmp_path, "python.md", "## Python context")
        result = load_language_profiles(["Go", "Python"], tmp_path)
        go_pos = result.index("## Go context")
        py_pos = result.index("## Python context")
        assert go_pos < py_pos

    def test_missing_profile_in_mixed_list_skipped(self, tmp_path: Path) -> None:
        _write_profile(tmp_path, "go.md", "## Go context")
        result = load_language_profiles(["Go", "Kotlin"], tmp_path)
        assert "## Go context" in result
        assert "Kotlin" not in result

    def test_empty_labels_returns_empty_string(self, tmp_path: Path) -> None:
        (tmp_path / "language-profiles").mkdir()
        result = load_language_profiles([], tmp_path)
        assert result == ""

    def test_label_lowercasing(self, tmp_path: Path) -> None:
        _write_profile(tmp_path, "typescript.md", "## TypeScript context")
        result = load_language_profiles(["TypeScript"], tmp_path)
        assert "## TypeScript context" in result

    def test_returns_joined_by_newline(self, tmp_path: Path) -> None:
        _write_profile(tmp_path, "go.md", "block A")
        _write_profile(tmp_path, "python.md", "block B")
        result = load_language_profiles(["Go", "Python"], tmp_path)
        assert result == "block A\nblock B"
