import os

import pytest

from app.admin import docs


@pytest.fixture
def tree(tmp_path):
    (tmp_path / "README.md").write_text("# Title\n")
    (tmp_path / "AGENTS.md").write_text("agents\n")
    (tmp_path / "LICENSE.md").write_text("mit\n")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "GUIDE.md").write_text("guide\n")
    (tmp_path / "notes.txt").write_text("not markdown\n")
    for skipped in (".venv", ".git", "node_modules", "data", "__pycache__", "logs"):
        (tmp_path / skipped).mkdir()
        (tmp_path / skipped / "hidden.md").write_text("skip me\n")
    return tmp_path


class TestFind:
    def test_finds_markdown_with_readme_first_and_top_level_before_nested(self, tree):
        assert [str(p) for p in docs.find_markdown(tree)] == ["README.md", "AGENTS.md", "LICENSE.md", "docs/GUIDE.md"]

    def test_dependency_state_and_hidden_directories_are_skipped(self, tree):
        assert not any("hidden.md" in str(p) for p in docs.find_markdown(tree))

    def test_symlinked_directories_are_not_followed(self, tree, tmp_path_factory):
        outside = tmp_path_factory.mktemp("outside")
        (outside / "secret.md").write_text("secret\n")
        os.symlink(outside, tree / "link")
        assert not any("secret" in str(p) for p in docs.find_markdown(tree))

    def test_the_list_is_capped(self, tmp_path, monkeypatch):
        monkeypatch.setattr(docs, "MAX_FILES", 3)
        for i in range(10):
            (tmp_path / f"{i}.md").write_text("x")
        assert len(docs.find_markdown(tmp_path)) == 3

    def test_empty(self, tmp_path):
        assert docs.find_markdown(tmp_path) == []


class TestRead:
    def test_reads_a_document(self, tree):
        path, text = docs.read_doc(tree, "README.md")
        assert path == (tree / "README.md").resolve() and text == "# Title\n"

    @pytest.mark.parametrize("bad", ["../outside.md", "/etc/passwd", "docs/../../x.md", "notes.txt", "missing.md"])
    def test_refuses_anything_that_is_not_a_markdown_file_in_the_install(self, tree, bad):
        with pytest.raises(docs.DocError):
            docs.read_doc(tree, bad)

    def test_a_symlink_pointing_outside_is_refused(self, tree, tmp_path_factory):
        outside = tmp_path_factory.mktemp("outside")
        (outside / "secret.md").write_text("secret\n")
        os.symlink(outside / "secret.md", tree / "leak.md")
        with pytest.raises(docs.DocError, match="outside"):
            docs.read_doc(tree, "leak.md")

    def test_huge_files_are_refused(self, tree, monkeypatch):
        monkeypatch.setattr(docs, "MAX_BYTES", 5)
        (tree / "big.md").write_text("0123456789")
        with pytest.raises(docs.DocError, match="larger"):
            docs.read_doc(tree, "big.md")

    def test_bad_bytes_do_not_crash(self, tree):
        (tree / "bin.md").write_bytes(b"ok \xff\xfe end")
        assert "ok" in docs.read_doc(tree, "bin.md")[1]


class TestLinks:
    def test_relative_links_resolve_from_the_current_document(self, tree):
        readme = (tree / "README.md").resolve()
        guide = (tree / "docs" / "GUIDE.md").resolve()
        assert docs.link_target(tree, readme, "docs/GUIDE.md") == guide
        assert docs.link_target(tree, guide, "../README.md#install") == readme
        assert docs.link_target(tree, readme, "docs/GUIDE.md") == guide

    @pytest.mark.parametrize("href", [
        "https://example.com/x.md", "mailto:a@b.c", "#anchor", "", "../outside.md", "docs/missing.md", "notes.txt"])
    def test_everything_else_is_not_a_target(self, tree, href):
        assert docs.link_target(tree, (tree / "README.md").resolve(), href) is None
