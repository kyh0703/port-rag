# 문서 파싱 스택 선정 v1

## Update

- 2026-07-09: 검색 transport 결정은 REST-only로 변경됐다. 이 리서치는
  파싱 스택 선정 근거로 유지하되, gRPC/`../contracts` 관련 문구는 초기
  가정 기록이다.

## Question

PDF/텍스트·마크다운/Office 문서를 RAG 청킹 품질을 보장하며 셀프호스트로
파싱하려면 어떤 라이브러리/스택이 최선인가? Node(TS)로 api와 스택을
통일할 수 있는가?

## Context

- reg는 그린필드. 생태계는 NestJS(api), TS LiveKit 워커(voice-agent),
  buf contracts(Go+TS codegen)로 구성.
- 업로드 범위가 PDF + txt/md + Office(docx/pptx/xlsx)로 잠김.
- 검색 계약은 내부 REST로 제공한다 — 서버 언어는 자유.

## Findings

- 2026년 기준 셀프호스트 오픈소스 파서 상위권은 Docling(IBM), Marker,
  Unstructured — 전부 Python. Docling은 레이아웃/테이블 이해 기반으로
  PDF/Office를 고품질 마크다운으로 변환하고 로컬에서 완전 실행되며
  HybridChunker(구조 인지 청킹)를 내장한다.
- Unstructured는 30+ 포맷과 다양한 청킹 전략을 가진 문서 ETL 플랫폼으로
  엔터프라이즈 RAG에서 가장 많이 인용되나, v1 범위에는 Docling으로 충분.
- Node 생태계는 pdf-parse/pdfjs, mammoth(docx), officeparser 수준 —
  테이블/레이아웃 이해가 없어 파싱 품질이 낮고 pptx/xlsx가 특히 취약.
  고품질을 원하면 LlamaParse 등 유료 외부 API 의존이 필요.
- RAG 검색 품질의 상한선은 파싱/청킹 품질이 결정한다. Office 문서가
  범위에 포함된 이상 Python 파서 채택이 사실상 필수.

## Sources

- https://pdfmux.com/blog/pdfmux-vs-llamaparse-vs-docling-vs-unstructured-2026/
- https://blazedocs.io/blog/best-pdf-parser-for-rag
- https://www.firecrawl.dev/blog/best-pdf-parsers
- https://www.respan.ai/market-map/compare/docling-vs-unstructured
- https://vstorm.co/llamaindex/top-10-document-parsing-services-for-rag-pipelines-and-llm-applications/

## Implications

- reg는 Python(FastAPI) + Docling으로 확정. 스택 이질성 비용보다
  파싱 품질 이득이 크다.
- NestJS 단일 스택(옵션 B)과 NestJS+Docling 사이드카(옵션 C)는 기각 —
  B는 품질 한계, C는 v1에 과한 운영 복잡도.

## Follow-ups

- 문서량/포맷 다양성이 커지면 Unstructured 또는 docling-serve 분리 재평가.
- 스캔 PDF(OCR 필요) 유입 비율 모니터링 — Docling OCR 옵션 활성화 여부 결정.
