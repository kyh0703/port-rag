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

The eight-migration bundle is pinned to local Spec
`2506f22bee5f10c992af48f1076d900f46c23eab`. Apply it before starting the new
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

## Webpage knowledge

All webpage routes use the existing internal-server authentication and the
authenticated owner's `?userId=...`, including POST and PATCH:

- `POST /documents/webpages/discover` with `{url}` discovers at most 100 unique
  same-host/subdomain links. Navigation anchors are discovered but navigation
  text is not indexed. Discovery does not register documents.
- `POST /documents/webpages` with `{name?, knowledgeKey, urls, autoSync?}` registers
  one document containing 1–100 selected HTTP(S) URLs. It returns 201 with
  `status: processing` and durable `webpage.syncStatus: queued`; `autoSync`
  defaults to false. The worker never adds newly discovered URLs during sync.
- `GET /documents/{id}/webpage` returns `{document, content}`. `content` is the last
  successfully indexed normalized full text, not an overlapping-chunk join.
- `PATCH /documents/{id}/webpage` with `{autoSync}` enables/disables the 24-hour
  schedule. `POST /documents/{id}/sync` durably queues a manual attempt and returns
  202; an already queued/running attempt returns 409. Poll detail/list for completion.
- File document responses have `webpage: null`. Webpage responses expose `urls`,
  `autoSync`, `syncStatus`, `lastSyncedAt`, `lastCheckedAt`, `nextSyncAt`, `syncError`,
  and `lastSyncChanged`.

The worker polls database state every five seconds. Completed attempts schedule
the next automatic attempt 24 hours later; disabled schedules have no next time.
Unchanged normalized text updates timestamps without embedding or replacing any
chunk IDs. Changed content is fully fetched/embedded before atomic publication.
Failures preserve previously ready chunks and text; an initial failure instead
marks the document failed. Manual/initial work remains valid when auto-sync is
turned off; automatic claims are canceled/fenced. Claim tokens, owner admission,
and row locks prevent stale workers, deletion, or erasure from republishing data.
Abandoned claims recover after a 15-minute lease; the whole job has a ten-minute
deadline. Account erasure cancels and drains the local webpage worker before
acknowledging data removal. Other workers recheck owner/claim admission before
each page and embedding step and cannot publish after the durable erasure fence.

Fetching validates all DNS answers and pins one public address for each redirect
hop while preserving the original HTTP Host and TLS verification hostname.
Credentials, non-HTTP schemes, private/reserved addresses, and non-web ports are
rejected; proxy environment settings and cross-hop cookies are not used. Each
fetch has a 20-second total deadline, at most five redirects, and a 2 MiB wire and
decoded-body limit (identity/gzip/deflate); combined normalized document text is
limited to 4 MiB. Script/style/navigation/hidden text is excluded. Only readable
server-rendered HTML is supported: JavaScript-only pages fail explicitly with
`webpage has no readable server-rendered text`, without title/description metadata
fallbacks or browser rendering.

Revision creation freezes ordinary file chunks exactly as before. Webpages use
the dedicated immutable `knowledge_revision_webpages` membership table instead
of frozen chunk copies. Create/get/list revision responses include
`liveWebpageIds` (`[]` for existing file-only revisions). Revision search combines
frozen file chunks with the **current ready chunks of only those selected,
same-owner webpage IDs**. New/unselected documents are never added implicitly.
Deleting a source yields no stale snapshot fallback; membership remains visible
until owner erasure. Existing immutable revision rows/chunks are not rewritten.
Apply canonical migration `20261005_0008` from the pinned spec bundle before
starting the updated service; recovery is forward-only.

Focused checks (use a disposable migrated PostgreSQL/pgvector database):

```bash
uv run pytest tests/ingest/test_webpage_fetch.py tests/ingest/test_webpage_worker.py tests/http/test_webpages.py
TEST_RAG_DATABASE_URL=postgresql+asyncpg://... uv run pytest tests/db/test_webpage_sync.py
```

Local end-to-end verification used the actual Web documents screen, NestJS
document/revision modules, RAG process, disposable pgvector database and real
OpenAI embeddings. Only the smoke user's session and unrelated billing shell
were fixtures. The public Tryvox webpage guide produced 2,261 characters and two
chunks. Unchanged sync retained chunk IDs; replacing an intentionally seeded
outdated stored snapshot refreshed the same revision's search results. An added
failing source preserved all previous ready chunks. Advancing the isolated due
timestamp exercised the scheduler and verified exactly 86,400 seconds to the next
run; disabling auto-sync cleared it. Desktop and 390px mobile controls were
exercised in Chromium. These checks do not constitute a production rollout.

Discovery excludes known non-HTML download/index links (for example PDF, XML and
`llms.txt`) so they are not preselected as webpages. Selected URLs are still
validated against their actual response MIME type during synchronization.

Verification: the complete RAG suite passed **197 tests** against the disposable
migrated database, with the existing Starlette/httpx deprecation warning.
New fetch and database cases were first collected before their modules/models
existed (RED). The discovery regression then failed with PDF/XML/text-index links
in the selectable result; filtering known non-HTML links made all 16 fetch cases
pass, and a real documentation fetch returned 26 webpage candidates.

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
