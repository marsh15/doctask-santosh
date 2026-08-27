# Doctask — governed project-delivery control register

Doctask turns changing PDF, DOCX, Markdown, and TXT project documents into a cited, versioned control register. It detects supported contradictions, evaluates YAML rules, proposes item-level mutations, pauses durably for human decisions, and commits only approved changes.

The central invariant is simple: no factual register value exists without an exact stored citation that independently validates the value.

## What is implemented

- Stable mixed-format locators: DOCX paragraph, PDF page/excerpt, Markdown heading/line, and TXT line.
- Per-corpus SHA-256 duplicate detection with globally unique source-version identities.
- PostgreSQL FTS plus entity-key retrieval and deterministic rank fusion. When the
  `vector` extension is available, deterministic local embeddings are persisted and
  queried through pgvector; otherwise startup records a graceful disabled capability.
- Deterministic extraction for offline work and an opt-in OpenAI-compatible adapter.
- Independent corpus/source-version/quote/hash/value grounding validation.
- Supported-claim conflicts, strict YAML rules, and explicit zero-finding results.
- Real incremental analysis of only unanalyzed sources, affected records, and relevant rule types.
- Durable LangGraph/PostgreSQL human-review interrupts and restart recovery.
- One persisted review ledger covers proposed mutations, conflicts, and rule findings.
  Every pending item gets an independent approve/reject decision; approved conflicts
  additionally require selection of one exact cited claim.
- Dependency edges, canonical record hashes, affected/skipped entity proof, timing, token use, and estimated cost.
- REST, MCP v2 Streamable HTTP, and a three-view React review client over one service.
- Prompt-injection fixture proving document instructions remain inert.

See [ARCHITECTURE.md](ARCHITECTURE.md) for boundaries and current limitations.

## Fastest start

Requirements: Docker with Compose.

```bash
docker compose up --build
```

Then open <http://localhost:8000>. The REST OpenAPI description is at <http://localhost:8000/docs>; MCP Streamable HTTP is mounted at <http://localhost:8000/mcp/>.

The Compose PostgreSQL image includes pgvector. The semantic path uses deterministic,
key-free 32-dimensional feature-hash embeddings for reproducible offline proof. This is
useful retrieval plumbing, not a claim of production-quality semantic similarity.

## Local development

Requirements: Python 3.12, `uv`, Node.js/npm, and PostgreSQL.

```bash
cp .env.example .env
uv sync --all-extras
npm --prefix apps/web ci
```

Start the API and UI in separate terminals:

```bash
DATABASE_URL=postgresql://... RULES_PATH=fixtures/rules/project-controls.yaml \
  uv run uvicorn doctask.main:app --reload

npm --prefix apps/web run dev
```

The Vite server proxies `/v1` to `127.0.0.1:8000`.

## REST workflow

```text
POST /v1/corpora
POST /v1/corpora/{corpusId}/sources
POST /v1/corpora/{corpusId}/runs
GET  /v1/runs/{runId}
GET  /v1/runs/{runId}/timeline
GET  /v1/runs/{runId}/proposals
POST /v1/runs/{runId}/decisions
GET  /v1/corpora/{corpusId}/register
GET  /v1/corpora/{corpusId}/conflicts
GET  /v1/corpora/{corpusId}/evidence/{recordId}
POST /v1/runs/{runId}/resume
```

Equivalent MCP tools are `create_corpus`, `add_source`, `start_analysis`, `get_run_status`, `list_proposed_mutations`, `submit_decisions`, `get_project_register`, `list_conflicts`, `get_record_evidence`, and `resume_run`.

`GET /v1/runs/{runId}` returns `reviewItems`; `submit_decisions` accepts
`reviewItemId` (`proposalId` remains an input alias for older proposal-only clients).

## Watched-directory adapter

The explicit watcher waits for stable supported files, atomically reserves them in
PostgreSQL, ingests all newly stable files as one batch, and starts one focused run:

```bash
DATABASE_URL=postgresql://... uv run python -m doctask.watcher ./incoming \
  --corpus-id 'corpus:...' --watcher-id local-inbox
```

Repeated observations are idempotent. Files arriving while a review is active are
persisted as queued and started together after that review completes.

## Verification

Offline tests:

```bash
uv run pytest
npm --prefix apps/web run build
npm --prefix apps/web audit
```

PostgreSQL integration tests:

```bash
TEST_DATABASE_URL=postgresql://... uv run pytest
```

The suite includes a real MCP initialize/session/tool-call handshake, not only direct Python calls.

## Security and operational notes

- REST uploads are read in 64 KiB chunks into a 1 MiB `SpooledTemporaryFile`; the
  parser seam still ultimately receives at most 10 MiB of bytes. DOCX expanded size,
  DOCX paragraph count, PDF page count, extracted text, and span count are capped.
  Production should additionally enforce request/body/time limits at its reverse proxy.
- MCP base64 length is checked before decoding; decoding is strict.
- MCP DNS-rebinding protection requires explicit allowed hosts in production through `MCP_ALLOWED_HOSTS`.
- `LANGGRAPH_STRICT_MSGPACK=true` is set by the application and Compose.
- Model endpoints must use HTTPS except for localhost, redirects are disabled, source text is delimited as untrusted data, and every returned claim is deterministically grounded.
- No shell, unrestricted network, credential, or arbitrary URL tool is available to document content.
- Schema setup currently runs on application startup. A multi-worker production deployment should run migrations in a pre-deploy step and then start service workers.

## Honest limitations

- The durable graph currently uses three physical LangGraph nodes. INGEST,
  RETRIEVE_OR_PLAN, CONFLICTS, RULES, HUMAN_REVIEW, and COMMIT are separately visible
  persisted timeline stages, but the first four are components inside one graph node.
- The deterministic embedding proves the pgvector capability path but is deliberately
  simpler than a production semantic model.
- The live OpenAI-compatible adapter is opt-in and has no credentialed smoke result in this repository.
- Both service images were built as the non-root `app` user. Full Docker Compose runtime verification remains a separate local or deployed check; Python, PostgreSQL, MCP protocol, browser accessibility snapshots, TypeScript build, and npm audit were run directly.
- Compose binds both ports to loopback and has no application authentication; it is a
  local demonstration deployment, not an internet-facing security configuration.
