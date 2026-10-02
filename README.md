# rag

Python RAG ingest/search service for Port. It exposes internal HTTP document
management and search endpoints.

## Account erasure

`DELETE /users/{user_id}/data` requires the shared `X-Internal-Server` key.
It fences the UUID durably before draining queued/running ingestion, deleting
owned raw uploads, current documents/chunks, and immutable knowledge revisions.
Successful completion returns `erased: true`; failed cleanup never reports
success. Old retrieval capabilities and late uploads return 410. Other owners'
documents, immutable vectors and originals are preserved.

The seven-migration bundle is pinned to local Spec
`555e92a500c877ca3a8dd7db4f0519133bc8a266`. Apply it before starting the new
service; restricted runtime roles need explicit execution rights for the new
erasure functions (PUBLIC execution is revoked).

Set `RAG_UPLOAD_STAGING_ROOT` to persistent owner-addressable storage. Run one
instance, or mount the same staging filesystem on every replica; local-only
volumes on independent replicas cannot prove complete raw-file erasure. SQL
and filesystem fences plus owner locks prevent late saves from resurrecting
deleted data.

Owner operations require **READ COMMITTED**; the service engine sets it
explicitly. Repeatable Read and Serializable snapshots are rejected with SQLSTATE
`0A000` before admission, fencing or erasure can have effects. Advisory locking
alone cannot refresh an old snapshot and is not a replacement for this check.

Cleanup fsyncs marker and deletion directories before acknowledging success,
including retries after the live file is already absent. Marker-less abandoned
instance directories are recovered under startup/owner locks; active owners are
not removed. Staging storage must support directory fsync, with durably
provisioned pre-existing ancestors. Fault-injection/recovery tests do not certify
physical media sanitization or every power-loss/storage implementation.


Legacy anonymous upload directories cannot be attributed to one account.
Their presence fails erasure with `RAG_LEGACY_UPLOAD_CLEANUP_REQUIRED`. Before
cutover, stop all old RAG processes, drain ingest, mount every legacy volume,
and perform offline cleanup with `RAG_CLEAN_LEGACY_UPLOADS_ON_START=true`.
Keep that flag false during normal operation. Prepare all writers and storage
mounts before starting the self-service API, which automatically recovers already
confirmed erasures. Enable member final confirmation only after this cutover.

Local smoke used the actual HTTP service and pgvector database: a legacy-file
failure was retryable; success removed the target's current/immutable vectors
and original while preserving another owner. Backups, independent legacy
volumes, and external history are not certified erased.
The 2026-10-02 self-service smoke also exercised real API restart and automatic
thirty-second retry after legacy-directory removal; target document, revision,
chunk and revision-chunk rows were erased while another owner's four inventories
remained intact. No administrator approval or manual destructive retry was involved.
Independent review regression: **167 tests passed**, including actual pgvector
old-snapshot rejection, directory-sync failures and marker-less crash recovery.
Additional direct smoke used the production SQL admission and upload storage:
startup recovered a marker-less orphan while preserving a live instance;
erasure and missing-directory retry removed only the target's original, kept
another owner's bytes, and rejected a late upload through the SQL fence.


## Local Setup

Use Python 3.12+ and Node.js 22+. Install the locked document tools before
starting the service or running tests:

```bash
uv sync
npm ci --omit=optional --ignore-scripts
export PATH="$PWD/node_modules/.bin:$PATH"
cp .env.example .env
```

Register an OpenAI key in the administrator **Key Management** page (`/admin/keys`),
then configure the trusted API address:

```bash
API_INTERNAL_BASE_URL=http://api:8000/api/v1
```

RAG obtains the current key from `GET /api/v1/internal/rag/embedding-credential`
using the shared `INTERNAL_SERVER_KEY`. The API owns encrypted credential storage
and decryption; RAG needs no provider key or decryption secret in its environment.
Each OpenAI request resolves the current administrator key, so key rotation needs
no RAG restart. Unavailable credentials fail explicitly, without a fake fallback.
`OPENAI_API_KEY` is no longer read. Documents previously indexed with `fake` must
be reindexed with real embeddings before relying on semantic search.
The service always uses OpenAI; there is no runtime provider selector.
Deterministic fake embedders exist only in the test suite.

## Document formats

- PDF, DOCX, PPTX, XLSX, Markdown and UTF-8 text keep the existing Docling path.
- HWP and HWPX use [kordoc](https://github.com/chrisryugj/kordoc) **4.15.6**
  (MIT) to extract JSON blocks directly into the Docling document model, then
  the same HybridChunker, embedding provider and pgvector storage. No Markdown
  reparse occurs: literal identifiers and table delimiters remain text.
  Extensions are case-insensitive; uploads may use `application/octet-stream`.
- The Docker image includes Node.js and the locked parser. No `npx`, package
  download, or external document conversion service runs during ingestion.
  Optional kordoc PDF/OCR dependencies are excluded; existing Docling
  dependencies and tokenizer/model downloads are unchanged.
- Image OCR, password input and original HWP/HWPX page-number citations are
  not exposed by this integration. Row/column spans are preserved; nested
  tables are flattened from their structured cells into parent-cell text.
  The original visual layout is not retained.
- Conversion has a 120-second timeout. Corrupt/protected files, missing tools
  and conversion failures use the existing `failed` document state, not a
  text fallback. JSON warnings `PARTIAL_PARSE`, `TRUNCATED_TABLE`,
  `UNSUPPORTED_ELEMENT`, `MALFORMED_XML`, `BROKEN_ZIP_RECOVERY`,
  `LENIENT_CFB_RECOVERY` and `SKIPPED_OLE` also fail ingestion rather than
  publishing incomplete content. Intentional image/hidden-text omission and
  approximate page boundaries do not fail ingestion. Re-upload an unlocked
  document when password/DRM protection prevents parsing.

`tests/ingest/test_kordoc_parser.py` exercises the installed parser with
synthetic HWP 5 and HWPX documents, literal identifiers, pipe characters in
table cells, merged/nested tables, footnotes, uppercase extensions and failure
paths. Partial-section failure is checked through the ingest pipeline: it must
mark the document failed without publishing any chunks. The HWP fixture
contains only a generated FileHeader and one uncompressed body paragraph;
it has no user data.

## Configuration files

Non-secret defaults are tracked in `config/default.yaml`. Copy
`config/local.example.yaml` to the gitignored `config/local.yaml` for local
database and internal-service settings. YAML category names are organizational only;
environment variables override YAML and existing `.env`/production injection
remain supported.

## Environment

- `DATABASE_URL`: async SQLAlchemy URL, for example
  `postgresql+asyncpg://port:port@localhost:5432/port`
- `INTERNAL_SERVER_KEY`: required shared API/RAG internal authentication key
- `RAG_RETRIEVAL_CAPABILITY_SECRET`: required shared retrieval capability secret
- `API_INTERNAL_BASE_URL`: trusted API base URL, default `http://api:8000/api/v1`
- `HTTP_PORT`: default `8000`
- `EMBEDDING_MODEL`: default `text-embedding-3-small`
- `EMBEDDING_DIM`: default `1536`
- `TOP_K_DEFAULT`: default `5`
- `SENTRY_DSN`: optional Sentry DSN; enables unhandled FastAPI exception reporting

## Checks

```bash
uv run python scripts/smoke.py
uv run pytest
uv run ruff check .
```

`scripts/smoke.py` creates a uniquely named, temporary Compose project and removes
only that project's containers, network and database volume afterward. It always
uses real embeddings: privately export the API's `INTERNAL_SERVER_KEY` and set
`API_INTERNAL_BASE_URL` to an API reachable from the smoke container (on macOS,
for example `http://host.docker.internal:8000/api/v1`). Unit tests remain isolated
from provider services. For a real smoke check with alternate ports:

```bash
RAG_SMOKE_POSTGRES_PORT=55435 \
RAG_SMOKE_HTTP_PORT=18082 \
RAG_SMOKE_HTTP_BASE=http://localhost:18082 \
API_INTERNAL_BASE_URL=http://host.docker.internal:8000/api/v1 \
uv run python scripts/smoke.py
```
