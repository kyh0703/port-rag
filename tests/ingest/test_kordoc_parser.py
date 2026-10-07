from __future__ import annotations
from tests.private_data_fixture import PlainParserInputFixture

import os
import subprocess
import uuid
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from rag.ingest.chunker import HybridDoclingChunker
from rag.ingest.parser import DoclingParser
from tests.fakes import StaticFakeEmbedder
from tests.fakes import MemoryOwnerAdmission
from rag.ingest.pipeline import IngestPipeline
from rag.ingest.types import IngestJob
from tests.ingest.test_pipeline import MemoryStore


@pytest.fixture(autouse=True)
def kordoc_path(monkeypatch: pytest.MonkeyPatch) -> None:
    binaries = Path(__file__).resolve().parents[2] / "node_modules" / ".bin"
    monkeypatch.setenv("PATH", f"{binaries}{os.pathsep}{os.environ.get('PATH', '')}")


def write_hwpx(path: Path) -> None:
    section = '''<?xml version="1.0" encoding="UTF-8"?>
<hs:sec xmlns:hs="http://www.hancom.co.kr/hwpml/2011/section"
        xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph">
  <hp:p><hp:run><hp:t>고객 지원 안내</hp:t></hp:run></hp:p>
  <hp:p><hp:run><hp:t>환불 신청은 구매 후 7일 이내 가능합니다.</hp:t></hp:run></hp:p>
  <hp:tbl>
    <hp:tr>
      <hp:tc><hp:cellSpan colSpan="1" rowSpan="1"/>
        <hp:p><hp:run><hp:t>항목</hp:t></hp:run></hp:p></hp:tc>
      <hp:tc><hp:cellSpan colSpan="1" rowSpan="1"/>
        <hp:p><hp:run><hp:t>금액</hp:t></hp:run></hp:p></hp:tc>
    </hp:tr>
    <hp:tr>
      <hp:tc><hp:cellSpan colSpan="1" rowSpan="1"/>
        <hp:p><hp:run><hp:t>배송비</hp:t></hp:run></hp:p></hp:tc>
      <hp:tc><hp:cellSpan colSpan="1" rowSpan="1"/>
        <hp:p><hp:run><hp:t>3,000원</hp:t></hp:run></hp:p></hp:tc>
    </hp:tr>
  </hp:tbl>
</hs:sec>'''
    write_hwpx_sections(path, [section])


def write_hwpx_sections(path: Path, sections: list[str]) -> None:
    items = "".join(
        f'<opf:item id="s{i}" href="section{i}.xml" media-type="application/xml"/>'
        for i in range(len(sections))
    )
    spine = "".join(f'<opf:itemref idref="s{i}"/>' for i in range(len(sections)))
    manifest = (
        '<opf:package xmlns:opf="http://www.idpf.org/2007/opf">'
        f"<opf:manifest>{items}</opf:manifest><opf:spine>{spine}</opf:spine></opf:package>"
    )
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        archive.writestr("mimetype", "application/hwp+zip")
        archive.writestr("Contents/content.hpf", manifest)
        for index, section in enumerate(sections):
            archive.writestr(f"Contents/section{index}.xml", section)


@pytest.mark.parametrize("suffix", [".hwpx", ".HWPX"])
async def test_hwpx_preserves_korean_body_and_table_in_chunks(tmp_path: Path, suffix: str) -> None:
    path = tmp_path / f"고객 안내{suffix}"
    write_hwpx(path)

    parsed = await DoclingParser().parse(path)
    chunks = await HybridDoclingChunker().chunk(parsed)

    assert parsed.name == path.name
    assert any("환불 신청은 구매 후 7일 이내 가능합니다." in chunk.text for chunk in chunks)
    assert any("배송비" in chunk.text and "3,000원" in chunk.text for chunk in chunks)
    assert all(chunk.metadata["source"] == path.name for chunk in chunks)


async def test_hwp_preserves_korean_body(tmp_path: Path) -> None:
    path = tmp_path / "고객 안내.HWP"
    path.write_bytes((Path(__file__).parent / "fixtures" / "support.hwp").read_bytes())

    parsed = await DoclingParser().parse(path)
    chunks = await HybridDoclingChunker().chunk(parsed)

    assert any("환불 신청은 구매 후 7일 이내 가능합니다." in chunk.text for chunk in chunks)


@pytest.mark.parametrize("suffix", [".hwp", ".hwpx"])
async def test_corrupt_hangul_file_is_not_indexed_as_text(tmp_path: Path, suffix: str) -> None:
    path = tmp_path / f"broken{suffix}"
    path.write_bytes(b"not a document")

    with pytest.raises(ValueError, match="kordoc conversion failed"):
        await DoclingParser().parse(path)


async def test_missing_kordoc_has_actionable_error(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "guide.hwpx"
    write_hwpx(path)
    monkeypatch.setenv("PATH", str(tmp_path))

    with pytest.raises(RuntimeError, match="kordoc is not installed"):
        await DoclingParser().parse(path)


async def test_kordoc_timeout_fails_instead_of_indexing_partial_output(tmp_path: Path, monkeypatch):
    path = tmp_path / "guide.hwpx"
    write_hwpx(path)

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=120, output=b"partial text")

    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(ValueError, match="kordoc conversion timed out"):
        await DoclingParser().parse(path)


async def test_hwpx_preserves_literal_identifiers_and_pipe_table_values(tmp_path: Path) -> None:
    path = tmp_path / "literal.hwpx"
    write_hwpx(path)
    with ZipFile(path) as archive:
        section = archive.read("Contents/section0.xml").decode()
    section = section.replace(
        "고객 지원 안내", r"customer_id ACME_PRO_2026 &lt;TOKEN&gt; **별표** $5 C:\temp"
    ).replace("배송비", "A | B")
    write_hwpx_sections(path, [section])

    parsed = await DoclingParser().parse(path)
    chunks = await HybridDoclingChunker().chunk(parsed)

    assert any(
        r"customer_id ACME_PRO_2026 <TOKEN> **별표** $5 C:\temp" in chunk.text
        for chunk in chunks
    )
    assert any("A | B" in chunk.text and "3,000원" in chunk.text for chunk in chunks)
    cells = parsed.content.tables[0].data.grid
    assert cells[1][0].text == "A | B"
    assert cells[1][1].text == "3,000원"


async def test_partial_hwpx_ingest_fails_without_publishing_chunks(tmp_path: Path) -> None:
    path = tmp_path / "partial.hwpx"
    write_hwpx(path)
    with ZipFile(path) as archive:
        section = archive.read("Contents/section0.xml").decode()
    damaged = section.replace("</hs:sec>", "<broken></hs:sec>")
    write_hwpx_sections(path, [section, damaged])
    store = MemoryStore()
    document_id = uuid.uuid4()
    pipeline = IngestPipeline(
        parser=DoclingParser(),
        chunker=HybridDoclingChunker(),
        embedder=StaticFakeEmbedder(dimensions=3),
        store=store,
        owner_access=MemoryOwnerAdmission(),
        storage=PlainParserInputFixture(),
    )

    await pipeline.ingest(IngestJob(
        document_id=document_id, path=path, user_id="0197e50a-1234-7abc-8def-0123456789ab",
    ))

    assert store.statuses[document_id] == "failed"
    assert "PARTIAL_PARSE" in store.errors[document_id]
    assert document_id not in store.chunks


async def test_hwpx_rejects_unsupported_text_elements(tmp_path: Path) -> None:
    path = tmp_path / "unsupported.hwpx"
    write_hwpx(path)
    with ZipFile(path) as archive:
        section = archive.read("Contents/section0.xml").decode()
    section = section.replace(
        "</hp:run>",
        "<hp:ctrl><hp:unknown><hp:t>중요한 환불 조건</hp:t></hp:unknown></hp:ctrl></hp:run>",
        1,
    )
    write_hwpx_sections(path, [section])

    with pytest.raises(ValueError, match="UNSUPPORTED_ELEMENT"):
        await DoclingParser().parse(path)


async def test_hwpx_allows_intentionally_hidden_comments_without_indexing_them(tmp_path: Path):
    path = tmp_path / "hidden-comment.hwpx"
    write_hwpx(path)
    with ZipFile(path) as archive:
        section = archive.read("Contents/section0.xml").decode()
    section = section.replace(
        "</hp:run>",
        "<hp:ctrl><hp:hiddenComment><hp:t>공개하지 않을 메모</hp:t>"
        "</hp:hiddenComment></hp:ctrl></hp:run>",
        1,
    )
    write_hwpx_sections(path, [section])

    parsed = await DoclingParser().parse(path)
    chunks = await HybridDoclingChunker().chunk(parsed)

    assert any("환불 신청은 구매 후 7일 이내 가능합니다." in chunk.text for chunk in chunks)
    assert all("공개하지 않을 메모" not in chunk.text for chunk in chunks)


async def test_hwpx_preserves_merged_cells_and_nested_table_values(tmp_path: Path) -> None:
    path = tmp_path / "merged.hwpx"
    section = '''<hs:sec xmlns:hs="http://www.hancom.co.kr/hwpml/2011/section"
        xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph">
      <hp:tbl>
        <hp:tr><hp:tc><hp:cellSpan colSpan="2" rowSpan="1"/>
          <hp:p><hp:run><hp:t>요금표</hp:t></hp:run></hp:p></hp:tc></hp:tr>
        <hp:tr>
          <hp:tc><hp:cellSpan colSpan="1" rowSpan="2"/>
            <hp:p><hp:run><hp:t>A | B</hp:t></hp:run></hp:p></hp:tc>
          <hp:tc><hp:cellSpan colSpan="1" rowSpan="1"/>
            <hp:p><hp:run><hp:t>3,000원</hp:t></hp:run></hp:p></hp:tc>
        </hp:tr>
        <hp:tr><hp:tc><hp:cellSpan colSpan="1" rowSpan="1"/>
          <hp:p><hp:run><hp:t>5,000원</hp:t></hp:run></hp:p></hp:tc></hp:tr>
      </hp:tbl>
      <hp:tbl><hp:tr>
        <hp:tc><hp:cellSpan colSpan="1" rowSpan="1"/>
          <hp:p><hp:run><hp:t>옵션</hp:t></hp:run></hp:p></hp:tc>
        <hp:tc><hp:cellSpan colSpan="1" rowSpan="1"/>
          <hp:p><hp:run><hp:t>상세 시작</hp:t></hp:run></hp:p>
          <hp:tbl><hp:tr>
            <hp:tc><hp:cellSpan colSpan="1" rowSpan="1"/>
              <hp:p><hp:run><hp:t>C | D</hp:t></hp:run></hp:p></hp:tc>
            <hp:tc><hp:cellSpan colSpan="1" rowSpan="1"/>
              <hp:p><hp:run><hp:t>7,000원</hp:t></hp:run></hp:p></hp:tc>
          </hp:tr></hp:tbl>
          <hp:p><hp:run><hp:t>상세 끝</hp:t></hp:run></hp:p>
        </hp:tc>
      </hp:tr></hp:tbl>
    </hs:sec>'''
    write_hwpx_sections(path, [section])

    parsed = await DoclingParser().parse(path)
    chunks = await HybridDoclingChunker().chunk(parsed)

    merged, nested = parsed.content.tables
    grid = merged.data.grid
    assert grid[0][0].text == "요금표"
    assert grid[0][0].col_span == 2
    assert grid[1][0].text == "A | B"
    assert grid[1][0].row_span == 2
    assert grid[1][1].text == "3,000원"
    assert grid[2][1].text == "5,000원"
    assert nested.data.grid[0][1].text == "상세 시작\nC | D\n7,000원\n상세 끝"
    assert any("C | D" in chunk.text and "7,000원" in chunk.text for chunk in chunks)


async def test_hwpx_preserves_footnote_conditions_in_search_chunks(tmp_path: Path) -> None:
    path = tmp_path / "footnote.hwpx"
    write_hwpx(path)
    with ZipFile(path) as archive:
        section = archive.read("Contents/section0.xml").decode()
    section = section.replace(
        "</hp:run>",
        '<hp:ctrl><hp:footNote><hp:subList><hp:p><hp:run>'
        '<hp:t>각주: 주문 코드 ACME_PRO 제외</hp:t></hp:run></hp:p>'
        '</hp:subList></hp:footNote></hp:ctrl></hp:run>',
        1,
    )
    write_hwpx_sections(path, [section])

    parsed = await DoclingParser().parse(path)
    chunks = await HybridDoclingChunker().chunk(parsed)

    assert any("각주: 주문 코드 ACME_PRO 제외" in chunk.text for chunk in chunks)
