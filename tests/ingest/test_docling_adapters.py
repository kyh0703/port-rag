from __future__ import annotations

from pathlib import Path
from zipfile import ZIP_DEFLATED
from zipfile import ZipFile

import pytest

from rag.ingest.chunker import HybridDoclingChunker
from rag.ingest.parser import DoclingParser


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("filename", "content"),
    [
        ("notes.md", "# Title\n\nalpha beta"),
        ("notes.txt", "alpha beta"),
    ],
)
async def test_docling_parser_and_chunker_handle_text_documents(
    tmp_path: Path,
    filename: str,
    content: str,
) -> None:
    path = tmp_path / filename
    path.write_text(content, encoding="utf-8")

    parsed = await DoclingParser().parse(path)
    chunks = await HybridDoclingChunker().chunk(parsed)

    assert parsed.name == filename
    assert chunks
    assert any("alpha" in chunk.text for chunk in chunks)


@pytest.mark.asyncio
async def test_docling_parser_and_chunker_handle_docx(tmp_path: Path) -> None:
    path = tmp_path / "notes.docx"
    _write_minimal_docx(path, "alpha beta docx fixture")

    parsed = await DoclingParser().parse(path)
    chunks = await HybridDoclingChunker().chunk(parsed)

    assert parsed.name == "notes.docx"
    assert chunks
    assert any("alpha" in chunk.text for chunk in chunks)


@pytest.mark.asyncio
async def test_docling_parser_rejects_unsupported_suffix(tmp_path: Path) -> None:
    path = tmp_path / "notes.bin"
    path.write_bytes(b"not a supported document")

    with pytest.raises(ValueError, match="unsupported document type"):
        await DoclingParser().parse(path)


def _write_minimal_docx(path: Path, text: str) -> None:
    files = {
        "[Content_Types].xml": """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>""",
        "_rels/.rels": """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>""",
        "word/document.xml": f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>""",
    }
    with ZipFile(path, "w", ZIP_DEFLATED) as docx:
        for name, content in files.items():
            docx.writestr(name, content)
