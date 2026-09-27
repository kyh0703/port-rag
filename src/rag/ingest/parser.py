"""Docling document parsing, with kordoc conversion for HWP/HWPX."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from typing import Any

from docling.document_converter import DocumentConverter
from docling_core.types.doc import DocItemLabel
from docling_core.types.doc import DoclingDocument
from docling_core.types.doc import TableCell
from docling_core.types.doc import TableData

from rag.ingest.types import ParsedDocument


# These warnings mean body content was lost or recovered from a damaged container.
# Image omission, hidden text filtering and approximate page boundaries are intentional.
_KORDOC_CONTENT_LOSS_WARNINGS = {
    "PARTIAL_PARSE",
    "TRUNCATED_TABLE",
    "UNSUPPORTED_ELEMENT",
    "MALFORMED_XML",
    "BROKEN_ZIP_RECOVERY",
    "LENIENT_CFB_RECOVERY",
    "SKIPPED_OLE",
}


class DoclingParser:
    supported_suffixes = {".pdf", ".docx", ".pptx", ".xlsx", ".md", ".txt", ".hwp", ".hwpx"}

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
        if suffix in {".hwp", ".hwpx"}:
            return ParsedDocument(name=path.name, content=self._parse_hangul(path))

        result = self._converter.convert(path, raises_on_error=True)
        return ParsedDocument(name=path.name, content=result.document)

    def _parse_text(self, path: Path) -> DoclingDocument:
        document = DoclingDocument(name=path.name)
        document.add_text(label=DocItemLabel.TEXT, text=path.read_text(encoding="utf-8"))
        return document

    def _parse_hangul(self, path: Path) -> DoclingDocument:
        try:
            result = subprocess.run(
                [
                    "kordoc",
                    str(path.resolve()),
                    "--format", "json",
                    "--no-images",
                    "--silent",
                ],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=120,
                check=False,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                "kordoc is not installed; run npm ci --omit=optional --ignore-scripts "
                "and add node_modules/.bin to PATH"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise ValueError("kordoc conversion timed out after 120 seconds") from exc

        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise ValueError("kordoc conversion failed: INVALID_OUTPUT") from exc
        if not isinstance(payload, dict):
            raise ValueError("kordoc conversion failed: INVALID_OUTPUT")
        if result.returncode or payload.get("success") is not True:
            # Preserve the machine error code, not document content or stderr.
            raise ValueError(f"kordoc conversion failed: {payload.get('code') or 'PROCESS_ERROR'}")
        loss_codes = sorted({
            warning["code"]
            for warning in payload.get("warnings", [])
            if warning["code"] in _KORDOC_CONTENT_LOSS_WARNINGS
        })
        if loss_codes:
            raise ValueError(f"kordoc conversion lost content: {', '.join(loss_codes)}")

        document = DoclingDocument(name=path.name)
        _append_kordoc_blocks(document, payload["blocks"])
        if not document.texts and not document.tables:
            raise ValueError("kordoc conversion produced no text")
        return document


def _append_kordoc_blocks(document: DoclingDocument, blocks: list[dict[str, Any]]) -> None:
    for block in blocks:
        kind = block["type"]
        if kind == "table":
            table = block["table"]
            caption_text = _kordoc_caption(table)
            caption = (
                document.add_text(label=DocItemLabel.CAPTION, text=caption_text)
                if caption_text.strip() else None
            )
            document.add_table(data=_kordoc_table_data(table), caption=caption)
        elif kind in {"paragraph", "heading", "list"}:
            text = _kordoc_block_text(block)
            if text.strip():
                if kind == "heading":
                    document.add_heading(text=text, level=block.get("level", 1))
                elif kind == "list":
                    document.add_list_item(
                        text=text, enumerated=block.get("listType") == "ordered"
                    )
                else:
                    document.add_text(label=DocItemLabel.TEXT, text=text)
        elif kind not in {"image", "separator"}:
            raise ValueError(f"unsupported kordoc block: {kind}")
        _append_kordoc_blocks(document, block.get("children", []))


def _kordoc_block_text(block: dict[str, Any]) -> str:
    if block["type"] == "table":
        table = block["table"]
        # Docling table cells hold text, not nested tables. Flatten the structured
        # cells directly; kordoc's legacy cell.text already contains Markdown escapes.
        parts = [_kordoc_caption(table)]
        parts.extend(cell.text for cell in _kordoc_table_data(table).table_cells)
        return "\n".join(part for part in parts if part)
    if block["type"] in {"image", "separator"}:
        return ""
    spans = block.get("spans", [])
    text = (
        "".join(span["text"] for span in spans if not span.get("placeholder"))
        if any(span.get("placeholder") for span in spans)
        else block.get("text", "")
    )
    if block.get("href") and block["href"] != text:
        text += f" ({block['href']})"
    if block.get("footnoteText"):
        text += f" (주: {block['footnoteText']})"
    # The pinned HWP IR escapes literal dollars, but not other Markdown punctuation.
    return text.replace("\\$", "$")


def _kordoc_caption(table: dict[str, Any]) -> str:
    if table.get("captionBlocks"):
        return "\n".join(_kordoc_block_text(block) for block in table["captionBlocks"])
    return table.get("caption", "").replace("\\$", "$")


def _kordoc_table_data(table: dict[str, Any]) -> TableData:
    cells = []
    covered: set[tuple[int, int]] = set()
    for row_index, row in enumerate(table["cells"]):
        for col_index, cell in enumerate(row):
            if (row_index, col_index) in covered:
                continue
            row_end = row_index + cell["rowSpan"]
            col_end = col_index + cell["colSpan"]
            covered.update(
                (r, c)
                for r in range(row_index, row_end)
                for c in range(col_index, col_end)
                if (r, c) != (row_index, col_index)
            )
            text = (
                "\n".join(_kordoc_block_text(block) for block in cell["blocks"])
                if cell.get("blocks") else cell["text"].replace("\\$", "$")
            )
            cells.append(TableCell(
                text=text,
                row_span=cell["rowSpan"],
                col_span=cell["colSpan"],
                start_row_offset_idx=row_index,
                end_row_offset_idx=row_end,
                start_col_offset_idx=col_index,
                end_col_offset_idx=col_end,
                column_header=bool(cell.get("isHeader") or (table["hasHeader"] and row_index == 0)),
            ))
    return TableData(num_rows=table["rows"], num_cols=table["cols"], table_cells=cells)
