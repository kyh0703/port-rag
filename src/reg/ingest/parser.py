"""Docling-backed document parsing."""

from __future__ import annotations

import asyncio
from pathlib import Path

from docling.document_converter import DocumentConverter
from docling_core.types.doc import DocItemLabel
from docling_core.types.doc import DoclingDocument

from reg.ingest.types import ParsedDocument


class DoclingParser:
    supported_suffixes = {".pdf", ".docx", ".pptx", ".xlsx", ".md", ".txt"}

    def __init__(self, converter: DocumentConverter | None = None) -> None:
        self._converter = converter or DocumentConverter()

    async def parse(self, path: Path) -> ParsedDocument:
        return await asyncio.to_thread(self._parse_sync, path)

    def _parse_sync(self, path: Path) -> ParsedDocument:
        suffix = path.suffix.lower()
        if suffix not in self.supported_suffixes:
            raise ValueError(f"unsupported document type: {suffix or '<none>'}")
        if suffix == ".txt":
            return ParsedDocument(name=path.name, content=self._parse_text(path))

        result = self._converter.convert(path, raises_on_error=True)
        return ParsedDocument(name=path.name, content=result.document)

    def _parse_text(self, path: Path) -> DoclingDocument:
        document = DoclingDocument(name=path.name)
        document.add_text(label=DocItemLabel.TEXT, text=path.read_text(encoding="utf-8"))
        return document
