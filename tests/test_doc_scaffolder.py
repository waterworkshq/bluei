"""Tests for bluei.engine.scaffold.doc_scaffolder — section insertion into docs."""

from pathlib import Path

from bluei.engine.scaffold.doc_scaffolder import insert_section


class TestInsertSection:
    """insert_section() — insert a markdown section before/after a heading."""

    def test_nonexistent_file_returns_false(self, tmp_path):
        assert insert_section(tmp_path / "nope.md", "Heading", "content") is False

    def test_heading_already_present_returns_false(self, tmp_path):
        doc = tmp_path / "README.md"
        doc.write_text("## Existing\n\nSome text\n")
        assert insert_section(doc, "Existing", "new content") is False

    def test_inserts_before_heading(self, tmp_path):
        doc = tmp_path / "README.md"
        doc.write_text("# Title\n\n## Existing\n\nSome text\n")
        result = insert_section(
            doc,
            "New Section",
            "## New Section\n\nContent here",
            before_heading="## Existing",
        )
        assert result is True
        text = doc.read_text()
        assert "## New Section" in text
        idx_new = text.index("## New Section")
        idx_existing = text.index("## Existing")
        assert idx_new < idx_existing

    def test_inserts_after_heading(self, tmp_path):
        doc = tmp_path / "README.md"
        doc.write_text(
            "# Title\n\n## First\n\nFirst content\n\n## Second\n\nSecond content\n"
        )
        result = insert_section(
            doc, "Inserted", "## Inserted\n\nInserted content", after_heading="## First"
        )
        assert result is True
        text = doc.read_text()
        idx_inserted = text.index("## Inserted")
        idx_second = text.index("## Second")
        assert idx_inserted < idx_second

    def test_appends_at_end_when_no_anchors(self, tmp_path):
        doc = tmp_path / "README.md"
        doc.write_text("# Title\n\nSome intro\n")
        result = insert_section(doc, "New Section", "## New Section\n\nContent here")
        assert result is True
        text = doc.read_text()
        assert text.endswith("## New Section\n\nContent here\n")

    def test_normalizes_heading_without_hash(self, tmp_path):
        doc = tmp_path / "README.md"
        doc.write_text("# Title\n\nSome intro\n")
        result = insert_section(doc, "My Heading", "## My Heading\n\nContent")
        assert result is True

    def test_before_heading_not_found_appends(self, tmp_path):
        doc = tmp_path / "README.md"
        doc.write_text("# Title\n\nContent\n")
        result = insert_section(
            doc, "New", "## New\n\nContent", before_heading="## Missing"
        )
        assert result is True

    def test_after_heading_not_found_appends(self, tmp_path):
        doc = tmp_path / "README.md"
        doc.write_text("# Title\n\nContent\n")
        result = insert_section(
            doc, "New", "## New\n\nContent", after_heading="## Missing"
        )
        assert result is True

    def test_after_heading_finds_next_heading(self, tmp_path):
        doc = tmp_path / "README.md"
        doc.write_text("## Alpha\n\nAlpha text\n\n## Beta\n\nBeta text\n")
        result = insert_section(
            doc, "Gamma", "## Gamma\n\nGamma text", after_heading="## Alpha"
        )
        assert result is True
        text = doc.read_text()
        idx_gamma = text.index("## Gamma")
        idx_beta = text.index("## Beta")
        assert idx_gamma < idx_beta
