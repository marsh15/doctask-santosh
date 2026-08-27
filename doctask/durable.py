import hashlib
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TypedDict
from uuid import uuid4

import psycopg
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from doctask.application import DecisionInput
from doctask.claims import parse_claim_text
from doctask.domain import (
    ControlRegister,
    Corpus,
    EvidenceReference,
    ProposedMutation,
    RegisterRecord,
    ReviewItem,
    SourceReceipt,
    WorkflowRun,
)
from doctask.grounding import ClaimCandidate, EvidenceValidator
from doctask.ingestion import parse_source
from doctask.model_adapters import (
    ClaimExtractionAdapter,
    DeterministicClaimAdapter,
    ModelUsage,
)
from doctask.retrieval import (
    EMBEDDING_DIMENSIONS,
    PostgresEvidenceRetriever,
    RetrievalHit,
    deterministic_embedding,
    vector_literal,
)
from doctask.rules import RulePack, load_rule_pack

os.environ.setdefault("LANGGRAPH_STRICT_MSGPACK", "true")


class GraphState(TypedDict, total=False):
    run_id: str
    corpus_id: str
    base_register_version: int
    status: str
    proposals: list[dict[str, object]]
    review_items: list[dict[str, object]]
    decisions: list[dict[str, object]]


SCHEMA = """
CREATE TABLE IF NOT EXISTS corpora (
    id text PRIMARY KEY,
    name text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS sources (
    id text PRIMARY KEY,
    corpus_id text NOT NULL REFERENCES corpora(id) ON DELETE CASCADE,
    filename text NOT NULL,
    content_sha256 char(64) NOT NULL,
    content bytea NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (corpus_id, content_sha256)
);
CREATE TABLE IF NOT EXISTS source_versions (
    id text PRIMARY KEY,
    source_id text NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    content_sha256 char(64) NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE source_versions
    ADD COLUMN IF NOT EXISTS analyzed_in_register_version integer;
ALTER TABLE source_versions
    ADD COLUMN IF NOT EXISTS content bytea;
CREATE TABLE IF NOT EXISTS source_spans (
    id text PRIMARY KEY,
    source_id text NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    source_version_id text NOT NULL REFERENCES source_versions(id) ON DELETE CASCADE,
    ordinal integer NOT NULL,
    text text NOT NULL,
    locator jsonb NOT NULL,
    content_hash char(64) NOT NULL,
    UNIQUE (source_version_id, ordinal)
);
ALTER TABLE source_spans
    ADD COLUMN IF NOT EXISTS search_vector tsvector
    GENERATED ALWAYS AS (
        to_tsvector('english'::regconfig, coalesce(text, ''))
    ) STORED;
CREATE INDEX IF NOT EXISTS source_spans_search_vector_idx
    ON source_spans USING gin (search_vector);
CREATE TABLE IF NOT EXISTS source_span_entities (
    corpus_id text NOT NULL REFERENCES corpora(id) ON DELETE CASCADE,
    span_id text NOT NULL REFERENCES source_spans(id) ON DELETE CASCADE,
    entity_key text NOT NULL CHECK (length(entity_key) <= 128),
    PRIMARY KEY (corpus_id, span_id, entity_key)
);
CREATE INDEX IF NOT EXISTS source_span_entities_lookup_idx
    ON source_span_entities (corpus_id, entity_key);
CREATE TABLE IF NOT EXISTS registers (
    corpus_id text PRIMARY KEY REFERENCES corpora(id) ON DELETE CASCADE,
    version integer NOT NULL DEFAULT 0 CHECK (version >= 0)
);
CREATE TABLE IF NOT EXISTS register_versions (
    corpus_id text NOT NULL REFERENCES corpora(id) ON DELETE CASCADE,
    version integer NOT NULL CHECK (version >= 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (corpus_id, version)
);
CREATE TABLE IF NOT EXISTS register_records (
    corpus_id text NOT NULL,
    register_version integer NOT NULL,
    record_id text NOT NULL,
    record_type text NOT NULL,
    value jsonb NOT NULL,
    evidence jsonb NOT NULL,
    canonical_hash char(64) NOT NULL,
    PRIMARY KEY (corpus_id, register_version, record_id),
    FOREIGN KEY (corpus_id, register_version)
        REFERENCES register_versions(corpus_id, version) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS workflow_runs (
    id text PRIMARY KEY,
    corpus_id text NOT NULL REFERENCES corpora(id) ON DELETE CASCADE,
    base_register_version integer NOT NULL,
    status text NOT NULL CHECK (status IN ('RUNNING', 'AWAITING_REVIEW', 'COMPLETED')),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE workflow_runs
    ADD COLUMN IF NOT EXISTS preparation_completed_at timestamptz;
CREATE UNIQUE INDEX IF NOT EXISTS workflow_runs_one_active_per_corpus
    ON workflow_runs (corpus_id)
    WHERE status IN ('RUNNING', 'AWAITING_REVIEW');
CREATE TABLE IF NOT EXISTS workflow_run_source_versions (
    run_id text NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
    source_version_id text NOT NULL REFERENCES source_versions(id) ON DELETE CASCADE,
    PRIMARY KEY (run_id, source_version_id)
);
ALTER TABLE workflow_runs
    ADD COLUMN IF NOT EXISTS affected_entity_keys jsonb NOT NULL DEFAULT '[]';
CREATE TABLE IF NOT EXISTS proposed_mutations (
    id text PRIMARY KEY,
    run_id text NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
    record_id text NOT NULL,
    record_type text NOT NULL,
    before_value jsonb,
    after_value jsonb NOT NULL,
    evidence jsonb NOT NULL,
    review_state text NOT NULL DEFAULT 'PENDING'
        CHECK (review_state IN ('PENDING', 'APPROVED', 'REJECTED')),
    UNIQUE (run_id, record_id)
);
CREATE TABLE IF NOT EXISTS human_decisions (
    proposal_id text PRIMARY KEY REFERENCES proposed_mutations(id) ON DELETE CASCADE,
    is_approved boolean NOT NULL,
    reason text,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS review_items (
    id text PRIMARY KEY,
    run_id text NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
    kind text NOT NULL CHECK (kind IN ('PROPOSED_MUTATION', 'CONFLICT', 'RULE_FINDING')),
    subject_id text NOT NULL,
    payload jsonb NOT NULL,
    review_state text NOT NULL DEFAULT 'PENDING'
        CHECK (review_state IN ('PENDING', 'APPROVED', 'REJECTED')),
    created_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE review_items
    DROP CONSTRAINT IF EXISTS review_items_run_id_kind_subject_id_key;
CREATE TABLE IF NOT EXISTS review_item_decisions (
    review_item_id text PRIMARY KEY REFERENCES review_items(id) ON DELETE CASCADE,
    is_approved boolean NOT NULL,
    reason text,
    resolution jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS conflicts (
    id text PRIMARY KEY,
    corpus_id text NOT NULL REFERENCES corpora(id) ON DELETE CASCADE,
    run_id text NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
    record_id text NOT NULL,
    field text NOT NULL,
    claims jsonb NOT NULL,
    status text NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN', 'RESOLVED')),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS rule_evaluations (
    run_id text PRIMARY KEY REFERENCES workflow_runs(id) ON DELETE CASCADE,
    evaluated_rule_count integer NOT NULL CHECK (evaluated_rule_count >= 0),
    findings jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS stage_executions (
    id text PRIMARY KEY,
    run_id text NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
    stage text NOT NULL,
    status text NOT NULL,
    affected_entity_keys jsonb NOT NULL,
    skipped_entity_keys jsonb NOT NULL,
    started_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    UNIQUE (run_id, stage)
);
ALTER TABLE stage_executions
    ADD COLUMN IF NOT EXISTS model_name text;
ALTER TABLE stage_executions
    ADD COLUMN IF NOT EXISTS input_tokens integer NOT NULL DEFAULT 0;
ALTER TABLE stage_executions
    ADD COLUMN IF NOT EXISTS output_tokens integer NOT NULL DEFAULT 0;
ALTER TABLE stage_executions
    ADD COLUMN IF NOT EXISTS estimated_cost_usd numeric(12, 6) NOT NULL DEFAULT 0;
CREATE TABLE IF NOT EXISTS dependency_edges (
    id text PRIMARY KEY,
    corpus_id text NOT NULL REFERENCES corpora(id) ON DELETE CASCADE,
    source_span_id text NOT NULL REFERENCES source_spans(id) ON DELETE CASCADE,
    record_id text NOT NULL,
    rule_id text,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (corpus_id, source_span_id, record_id, rule_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS dependency_span_record_uq
    ON dependency_edges (corpus_id, source_span_id, record_id)
    WHERE rule_id IS NULL;
CREATE TABLE IF NOT EXISTS watched_files (
    watcher_id text NOT NULL,
    corpus_id text NOT NULL REFERENCES corpora(id) ON DELETE CASCADE,
    absolute_path text NOT NULL,
    content_sha256 char(64) NOT NULL,
    source_id text REFERENCES sources(id) ON DELETE CASCADE,
    run_id text REFERENCES workflow_runs(id) ON DELETE SET NULL,
    status text NOT NULL DEFAULT 'RESERVED'
        CHECK (status IN ('RESERVED', 'STARTED', 'ERROR')),
    error_message text,
    observed_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (watcher_id, absolute_path, content_sha256)
);
ALTER TABLE watched_files ALTER COLUMN source_id DROP NOT NULL;
ALTER TABLE watched_files ADD COLUMN IF NOT EXISTS status text NOT NULL DEFAULT 'RESERVED';
ALTER TABLE watched_files ADD COLUMN IF NOT EXISTS error_message text;
ALTER TABLE watched_files DROP CONSTRAINT IF EXISTS watched_files_status_check;
ALTER TABLE watched_files ADD CONSTRAINT watched_files_status_check
    CHECK (status IN ('RESERVED', 'STARTED', 'ERROR'));
"""


class DurableProjectDeliveryService:
    def __init__(
        self,
        database_url: str,
        checkpointer: PostgresSaver,
        rule_pack: RulePack,
        model_adapter: ClaimExtractionAdapter | None = None,
    ):
        self.database_url = database_url
        self.checkpointer = checkpointer
        self.rule_pack = rule_pack
        self.model_adapter = model_adapter or DeterministicClaimAdapter()
        self._migrate()
        self.retriever = PostgresEvidenceRetriever(
            self._connect, vector_enabled=self._vector_enabled
        )
        self.evidence_validator = EvidenceValidator(self._connect)
        builder = StateGraph(GraphState)
        builder.add_node("prepare_review", self._prepare_review)
        builder.add_node("human_review", self._human_review)
        builder.add_node("commit_version", self._commit_version)
        builder.add_edge(START, "prepare_review")
        builder.add_conditional_edges(
            "prepare_review",
            self._route_after_prepare,
            {"review": "human_review", "done": END},
        )
        builder.add_edge("human_review", "commit_version")
        builder.add_edge("commit_version", END)
        self.graph = builder.compile(checkpointer=checkpointer)

    @classmethod
    @contextmanager
    def connect(
        cls,
        database_url: str,
        *,
        rules_path: str | Path | None = None,
        model_adapter: ClaimExtractionAdapter | None = None,
    ) -> Iterator["DurableProjectDeliveryService"]:
        with PostgresSaver.from_conn_string(database_url) as checkpointer:
            checkpointer.setup()
            yield cls(
                database_url,
                checkpointer,
                load_rule_pack(rules_path),
                model_adapter=model_adapter,
            )

    def _connect(self):
        return psycopg.connect(self.database_url, row_factory=dict_row)

    def _migrate(self) -> None:
        with self._connect() as connection:
            connection.execute(SCHEMA, prepare=False)
        self._vector_enabled = self._setup_vector_capability()

    def _setup_vector_capability(self) -> bool:
        try:
            with self._connect() as connection:
                # A managed PostgreSQL role may not be allowed to install extensions.
                # Bound the capability probe so startup always falls back promptly.
                connection.execute("SET LOCAL lock_timeout = '2s'")
                connection.execute("SET LOCAL statement_timeout = '5s'")
                connection.execute("CREATE EXTENSION IF NOT EXISTS vector")
                connection.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS source_span_embeddings (
                        corpus_id text NOT NULL REFERENCES corpora(id) ON DELETE CASCADE,
                        span_id text PRIMARY KEY REFERENCES source_spans(id) ON DELETE CASCADE,
                        embedding vector({EMBEDDING_DIMENSIONS}) NOT NULL
                    )
                    """
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS source_span_embeddings_corpus_idx "
                    "ON source_span_embeddings (corpus_id)"
                )
            return True
        except psycopg.Error:
            return False

    def reset_for_test(self) -> None:
        with self._connect() as connection:
            connection.execute(
                "TRUNCATE TABLE corpora CASCADE"
            )
            for table in (
                "checkpoint_writes",
                "checkpoint_blobs",
                "checkpoints",
            ):
                connection.execute(f"TRUNCATE TABLE {table}")

    def create_corpus(self, *, name: str) -> Corpus:
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("Corpus name is required")
        corpus = Corpus(corpus_id=f"corpus:{uuid4()}", name=clean_name)
        with self._connect() as connection, connection.transaction():
            connection.execute(
                "INSERT INTO corpora (id, name) VALUES (%s, %s)",
                (corpus.corpus_id, corpus.name),
            )
            connection.execute(
                "INSERT INTO registers (corpus_id, version) VALUES (%s, 0)",
                (corpus.corpus_id,),
            )
            connection.execute(
                "INSERT INTO register_versions (corpus_id, version) VALUES (%s, 0)",
                (corpus.corpus_id,),
            )
        return corpus

    def add_source(self, *, corpus_id: str, filename: str, content: bytes) -> SourceReceipt:
        if len(content) > 10_485_760:
            raise ValueError("Source exceeds the 10 MiB limit")
        digest = hashlib.sha256(content).hexdigest()
        with self._connect() as connection, connection.transaction():
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                (f"source:{corpus_id}",),
            )
            existing = connection.execute(
                """
                SELECT s.id AS source_id, sv.id AS source_version_id
                FROM sources s
                JOIN source_versions sv ON sv.source_id = s.id
                WHERE s.corpus_id = %s AND sv.content_sha256 = %s
                ORDER BY (s.filename = %s) DESC, sv.created_at DESC, sv.id
                LIMIT 1
                """,
                (corpus_id, digest, filename),
            ).fetchone()
            if existing is not None:
                return SourceReceipt(
                    existing["source_id"], existing["source_version_id"], True
                )
            lineage = connection.execute(
                "SELECT id FROM sources WHERE corpus_id = %s AND filename = %s ORDER BY created_at LIMIT 1",
                (corpus_id, filename),
            ).fetchone()
            source_id = lineage["id"] if lineage is not None else f"source:{uuid4()}"
            source_version_id = f"source-version:{uuid4()}"
            spans = parse_source(
                source_id=source_id,
                source_version_id=source_version_id,
                filename=filename,
                content=content,
            )
            if lineage is None:
                connection.execute(
                    """
                    INSERT INTO sources (id, corpus_id, filename, content_sha256, content)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (source_id, corpus_id, filename, digest, content),
                )
            else:
                connection.execute(
                    "UPDATE sources SET content_sha256 = %s, content = %s WHERE id = %s",
                    (digest, content, source_id),
                )
            connection.execute(
                """
                INSERT INTO source_versions (id, source_id, content_sha256, content)
                VALUES (%s, %s, %s, %s)
                """,
                (source_version_id, source_id, digest, content),
            )
            for ordinal, span in enumerate(spans):
                connection.execute(
                    """
                    INSERT INTO source_spans (
                        id, source_id, source_version_id, ordinal, text, locator, content_hash
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        span.span_id,
                        span.source_id,
                        span.source_version_id,
                        ordinal,
                        span.text,
                        Jsonb(span.locator),
                        span.content_hash,
                    ),
                )
                parsed_claim = parse_claim_text(span.text)
                if parsed_claim is not None:
                    connection.execute(
                        """
                        INSERT INTO source_span_entities (corpus_id, span_id, entity_key)
                        VALUES (%s, %s, %s)
                        """,
                        (corpus_id, span.span_id, parsed_claim.record_id),
                    )
                if self._vector_enabled:
                    connection.execute(
                        """
                        INSERT INTO source_span_embeddings (corpus_id, span_id, embedding)
                        VALUES (%s, %s, %s::vector)
                        """,
                        (
                            corpus_id,
                            span.span_id,
                            vector_literal(deterministic_embedding(span.text)),
                        ),
                    )
        return SourceReceipt(source_id, source_version_id, False)

    def start_analysis(self, *, corpus_id: str) -> WorkflowRun:
        run_id = f"run:{uuid4()}"
        with self._connect() as connection, connection.transaction():
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))", (corpus_id,)
            )
            register = connection.execute(
                "SELECT version FROM registers WHERE corpus_id = %s", (corpus_id,)
            ).fetchone()
            if register is None:
                raise LookupError("Corpus not found")
            active = connection.execute(
                """
                SELECT id FROM workflow_runs
                WHERE corpus_id = %s AND status IN ('RUNNING', 'AWAITING_REVIEW')
                LIMIT 1
                """,
                (corpus_id,),
            ).fetchone()
            if active is not None:
                raise ValueError("Corpus already has an active Workflow Run")
            base_version = register["version"]
            connection.execute(
                """
                INSERT INTO workflow_runs (id, corpus_id, base_register_version, status)
                VALUES (%s, %s, %s, 'RUNNING')
                """,
                (run_id, corpus_id, base_version),
            )
            connection.execute(
                """
                INSERT INTO workflow_run_source_versions (run_id, source_version_id)
                SELECT %s, sv.id
                FROM source_versions sv
                JOIN sources s ON s.id = sv.source_id
                WHERE s.corpus_id = %s
                  AND sv.analyzed_in_register_version IS NULL
                """,
                (run_id, corpus_id),
            )
        self.graph.invoke(
            {
                "run_id": run_id,
                "corpus_id": corpus_id,
                "base_register_version": base_version,
                "status": "RUNNING",
            },
            config=self._config(run_id),
        )
        return self.get_run(run_id=run_id)

    def get_run(self, *, run_id: str) -> WorkflowRun:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT corpus_id, base_register_version, status FROM workflow_runs WHERE id = %s",
                (run_id,),
            ).fetchone()
            if row is None:
                raise LookupError("Workflow Run not found")
            proposals = connection.execute(
                """
                SELECT id, record_id, record_type, before_value, after_value, evidence
                FROM proposed_mutations WHERE run_id = %s ORDER BY record_id
                """,
                (run_id,),
            ).fetchall()
            review_items = connection.execute(
                """
                SELECT id, kind, subject_id, payload, review_state
                FROM review_items WHERE run_id = %s ORDER BY created_at, id
                """,
                (run_id,),
            ).fetchall()
        return WorkflowRun(
            run_id=run_id,
            corpus_id=row["corpus_id"],
            base_register_version=row["base_register_version"],
            status=row["status"],
            proposals=tuple(self._proposal_from_row(value) for value in proposals),
            review_items=tuple(
                ReviewItem(
                    review_item_id=value["id"],
                    kind=value["kind"],
                    subject_id=value["subject_id"],
                    payload=value["payload"],
                    review_state=value["review_state"],
                )
                for value in review_items
            ),
        )

    def submit_decisions(
        self, *, run_id: str, decisions: list[DecisionInput]
    ) -> WorkflowRun:
        run = self.get_run(run_id=run_id)
        if run.status != "AWAITING_REVIEW":
            raise ValueError("Workflow Run is not awaiting review")
        expected = {
            item.review_item_id for item in run.review_items if item.review_state == "PENDING"
        }
        supplied = {decision.review_item_id for decision in decisions}
        if expected != supplied or len(supplied) != len(decisions):
            raise ValueError("Every pending review item requires exactly one Human Decision")
        self._ensure_review_interrupt(run)
        self.graph.invoke(
            Command(
                resume={
                    "decisions": [
                        {
                            "review_item_id": value.review_item_id,
                            "is_approved": value.is_approved,
                            "reason": value.reason,
                            "resolution": value.resolution,
                        }
                        for value in decisions
                    ]
                }
            ),
            config=self._config(run_id),
        )
        return self.get_run(run_id=run_id)

    def get_register(self, *, corpus_id: str) -> ControlRegister:
        with self._connect() as connection:
            current = connection.execute(
                "SELECT version FROM registers WHERE corpus_id = %s", (corpus_id,)
            ).fetchone()
            if current is None:
                raise LookupError("Corpus not found")
            rows = connection.execute(
                """
                SELECT record_id, record_type, value, evidence, canonical_hash
                FROM register_records
                WHERE corpus_id = %s AND register_version = %s
                ORDER BY record_id
                """,
                (corpus_id, current["version"]),
            ).fetchall()
        return ControlRegister(
            corpus_id=corpus_id,
            version=current["version"],
            records=tuple(
                RegisterRecord(
                    record_id=row["record_id"],
                    record_type=row["record_type"],
                    value=row["value"],
                    evidence=tuple(
                        self._evidence_from_dict(value) for value in row["evidence"]
                    ),
                    canonical_hash=row["canonical_hash"],
                )
                for row in rows
            ),
        )

    def search_evidence(
        self,
        *,
        corpus_id: str,
        query: str,
        entity_keys: tuple[str, ...] = (),
        limit: int = 8,
    ) -> list[RetrievalHit]:
        return self.retriever.search(
            corpus_id=corpus_id,
            query=query,
            entity_keys=entity_keys,
            limit=limit,
        )

    def ingest_watched_file(
        self, *, watcher_id: str, corpus_id: str, path: str | Path
    ) -> dict[str, object]:
        return self.ingest_watched_batch(
            watcher_id=watcher_id, corpus_id=corpus_id, paths=[path]
        )[0]

    def ingest_watched_batch(
        self, *, watcher_id: str, corpus_id: str, paths: list[str | Path]
    ) -> list[dict[str, object]]:
        prepared = []
        preparation_errors: list[dict[str, object]] = []
        for value in paths:
            try:
                resolved = Path(value).resolve(strict=True)
                digest_builder = hashlib.sha256()
                content = bytearray()
                with resolved.open("rb") as source:
                    while chunk := source.read(64 * 1024):
                        content.extend(chunk)
                        if len(content) > 10_485_760:
                            raise ValueError("Source exceeds the 10 MiB limit")
                        digest_builder.update(chunk)
                prepared.append((resolved, digest_builder.hexdigest(), bytes(content)))
            except (OSError, ValueError) as exc:
                preparation_errors.append(
                    {
                        "isDuplicate": False,
                        "sourceId": None,
                        "runId": None,
                        "status": "ERROR",
                        "path": str(value),
                        "error": str(exc),
                    }
                )
        results: list[dict[str, object]] = list(preparation_errors)
        pending: list[tuple[Path, str, SourceReceipt]] = []
        with self._connect() as lock_connection:
            watcher_lock_key = f"watcher:{corpus_id}"
            lock_connection.execute(
                "SELECT pg_advisory_lock(hashtext(%s))", (watcher_lock_key,)
            )
            try:
                active = lock_connection.execute(
                    """
                    SELECT id FROM workflow_runs
                    WHERE corpus_id = %s AND status IN ('RUNNING', 'AWAITING_REVIEW')
                    LIMIT 1
                    """,
                    (corpus_id,),
                ).fetchone()
                for resolved, digest, content in prepared:
                    reservation = lock_connection.execute(
                        """
                        INSERT INTO watched_files (
                            watcher_id, corpus_id, absolute_path, content_sha256,
                            source_id, run_id, status
                        ) VALUES (%s, %s, %s, %s, NULL, NULL, 'RESERVED')
                        ON CONFLICT DO NOTHING
                        RETURNING absolute_path
                        """,
                        (watcher_id, corpus_id, str(resolved), digest),
                    ).fetchone()
                    if reservation is None:
                        existing = lock_connection.execute(
                            """
                            SELECT source_id, run_id FROM watched_files
                            WHERE watcher_id = %s AND absolute_path = %s
                              AND content_sha256 = %s
                            """,
                            (watcher_id, str(resolved), digest),
                        ).fetchone()
                        results.append(
                            {
                                "isDuplicate": True,
                                "sourceId": existing["source_id"],
                                "runId": existing["run_id"],
                            }
                        )
                        continue
                    try:
                        receipt = self.add_source(
                            corpus_id=corpus_id, filename=resolved.name, content=content
                        )
                    except (ValueError, OSError) as exc:
                        lock_connection.execute(
                            """
                            UPDATE watched_files SET status = 'ERROR', error_message = %s
                            WHERE watcher_id = %s AND absolute_path = %s
                              AND content_sha256 = %s
                            """,
                            (str(exc), watcher_id, str(resolved), digest),
                        )
                        results.append(
                            {
                                "isDuplicate": False,
                                "sourceId": None,
                                "runId": None,
                                "status": "ERROR",
                                "error": str(exc),
                            }
                        )
                        continue
                    lock_connection.execute(
                        """
                        UPDATE watched_files SET source_id = %s
                        WHERE watcher_id = %s AND absolute_path = %s
                          AND content_sha256 = %s
                        """,
                        (receipt.source_id, watcher_id, str(resolved), digest),
                    )
                    pending.append((resolved, digest, receipt))
                recoverable = lock_connection.execute(
                    """
                    SELECT count(*) AS count FROM watched_files
                    WHERE watcher_id = %s AND corpus_id = %s
                      AND status = 'RESERVED' AND source_id IS NOT NULL
                    """,
                    (watcher_id, corpus_id),
                ).fetchone()["count"]
                run = None
                if recoverable and active is None:
                    try:
                        run = self.start_analysis(corpus_id=corpus_id)
                    except ValueError as exc:
                        if "active Workflow Run" not in str(exc):
                            raise
                if run is not None:
                    lock_connection.execute(
                        """
                        UPDATE watched_files SET run_id = %s, status = 'STARTED'
                        WHERE watcher_id = %s AND corpus_id = %s
                          AND status = 'RESERVED' AND source_id IS NOT NULL
                        """,
                        (run.run_id, watcher_id, corpus_id),
                    )
                    for resolved, _digest, receipt in pending:
                        results.append(
                            {
                                "isDuplicate": False,
                                "sourceId": receipt.source_id,
                                "runId": run.run_id,
                            }
                        )
                elif recoverable:
                    for resolved, _digest, receipt in pending:
                        results.append(
                            {
                                "isDuplicate": False,
                                "sourceId": receipt.source_id,
                                "runId": None,
                                "status": "QUEUED",
                            }
                        )
                return results
            finally:
                lock_connection.execute(
                    "SELECT pg_advisory_unlock(hashtext(%s))", (watcher_lock_key,)
                )

    def list_proposed_mutations(self, *, run_id: str) -> tuple[ProposedMutation, ...]:
        return self.get_run(run_id=run_id).proposals

    def list_review_items(self, *, run_id: str) -> tuple[ReviewItem, ...]:
        return self.get_run(run_id=run_id).review_items

    def get_record_evidence(
        self, *, corpus_id: str, record_id: str
    ) -> tuple[EvidenceReference, ...]:
        register = self.get_register(corpus_id=corpus_id)
        for record in register.records:
            if record.record_id == record_id:
                return record.evidence
        raise LookupError("Register Record not found")

    def resume_run(self, *, run_id: str) -> WorkflowRun:
        run = self.get_run(run_id=run_id)
        if run.status == "COMPLETED":
            return run
        if run.status == "AWAITING_REVIEW":
            raise ValueError("Human decisions are required before this run can resume")
        self._resume_graph(run)
        return self.get_run(run_id=run_id)

    def _resume_graph(self, run: WorkflowRun) -> None:
        config = self._config(run.run_id)
        initial_state: GraphState | None = None
        if self.checkpointer.get_tuple(config) is None:
            initial_state = {
                "run_id": run.run_id,
                "corpus_id": run.corpus_id,
                "base_register_version": run.base_register_version,
                "status": run.status,
            }
        self.graph.invoke(initial_state, config=config)

    def _ensure_review_interrupt(self, run: WorkflowRun) -> None:
        checkpoint = self.checkpoint_state(run_id=run.run_id)
        if "review_items" not in checkpoint:
            self._resume_graph(run)
            checkpoint = self.checkpoint_state(run_id=run.run_id)
        if "review_items" not in checkpoint:
            raise RuntimeError("Workflow Run could not restore its Human Review interrupt")

    def list_conflicts(self, *, corpus_id: str) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, record_id, field, claims, status
                FROM conflicts WHERE corpus_id = %s
                ORDER BY record_id, field, created_at, id
                """,
                (corpus_id,),
            ).fetchall()
        return [
            {
                "conflictId": row["id"],
                "recordId": row["record_id"],
                "field": row["field"],
                "claims": row["claims"],
                "status": row["status"],
            }
            for row in rows
        ]

    def get_rule_evaluation(self, *, run_id: str) -> dict[str, object]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT evaluated_rule_count, findings
                FROM rule_evaluations WHERE run_id = %s
                """,
                (run_id,),
            ).fetchone()
        if row is None:
            raise LookupError("Rule Evaluation not found")
        return {
            "evaluatedRuleCount": row["evaluated_rule_count"],
            "findingCount": len(row["findings"]),
            "findings": row["findings"],
        }

    def get_run_timeline(self, *, run_id: str) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT stage, status, affected_entity_keys, skipped_entity_keys,
                       started_at, completed_at, model_name, input_tokens,
                       output_tokens, estimated_cost_usd
                FROM stage_executions
                WHERE run_id = %s
                ORDER BY array_position(
                    ARRAY[
                        'INGEST', 'RETRIEVE_OR_PLAN', 'CONFLICTS',
                        'RULES', 'HUMAN_REVIEW', 'COMMIT'
                    ]::text[], stage
                ), started_at, id
                """,
                (run_id,),
            ).fetchall()
        return [
            {
                "stage": row["stage"],
                "status": row["status"],
                "affectedEntityKeys": row["affected_entity_keys"],
                "skippedEntityKeys": row["skipped_entity_keys"],
                "startedAt": row["started_at"].isoformat(),
                "completedAt": (
                    row["completed_at"].isoformat()
                    if row["completed_at"] is not None
                    else None
                ),
                "durationMs": (
                    round(
                        (row["completed_at"] - row["started_at"]).total_seconds()
                        * 1000,
                        3,
                    )
                    if row["completed_at"] is not None
                    else None
                ),
                "modelUsage": {
                    "model": row["model_name"],
                    "inputTokens": row["input_tokens"],
                    "outputTokens": row["output_tokens"],
                    "estimatedCostUsd": float(row["estimated_cost_usd"]),
                },
            }
            for row in rows
        ]

    def checkpoint_state(self, *, run_id: str) -> dict[str, object]:
        state = self.graph.get_state(self._config(run_id))
        return dict(state.values)

    def _prepare_review(self, state: GraphState) -> GraphState:
        with self._connect() as connection, connection.transaction():
            persisted = connection.execute(
                """
                SELECT preparation_completed_at
                FROM workflow_runs
                WHERE id = %s
                FOR UPDATE
                """,
                (state["run_id"],),
            ).fetchone()
            if persisted is None:
                raise LookupError("Workflow Run not found")
            existing_review = connection.execute(
                "SELECT EXISTS(SELECT 1 FROM review_items WHERE run_id = %s) AS present",
                (state["run_id"],),
            ).fetchone()
            if persisted["preparation_completed_at"] is not None or existing_review["present"]:
                if persisted["preparation_completed_at"] is None:
                    connection.execute(
                        """
                        UPDATE workflow_runs
                        SET preparation_completed_at = now(), updated_at = now()
                        WHERE id = %s
                        """,
                        (state["run_id"],),
                    )
                return self._persisted_preparation_state(connection, state["run_id"])

            proposals, conflict_specs, model_usage = self._build_proposals(
                run_id=state["run_id"],
                corpus_id=state["corpus_id"],
                base_version=state["base_register_version"],
            )
            existing_keys = {
                row["record_id"]
                for row in connection.execute(
                    """
                    SELECT record_id FROM register_records
                    WHERE corpus_id = %s AND register_version = %s
                    """,
                    (state["corpus_id"], state["base_register_version"]),
                ).fetchall()
            }
            affected_keys = sorted(
                {proposal.record_id for proposal in proposals}
                | {str(spec["recordId"]) for spec in conflict_specs}
            )
            skipped_keys = sorted(existing_keys - set(affected_keys))
            for proposal in proposals:
                connection.execute(
                    """
                    INSERT INTO proposed_mutations (
                        id, run_id, record_id, record_type, before_value, after_value, evidence
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        proposal.proposal_id,
                        state["run_id"],
                        proposal.record_id,
                        proposal.record_type,
                        Jsonb(proposal.before) if proposal.before is not None else None,
                        Jsonb(proposal.after),
                        Jsonb([self._evidence_dict(item) for item in proposal.evidence]),
                    ),
                )
            conflict_rows = self._persist_conflicts(connection, state, conflict_specs)
            evaluated_rule_count, findings = self._evaluate_rules(
                connection, state, proposals, conflict_specs
            )
            connection.execute(
                """
                INSERT INTO rule_evaluations (run_id, evaluated_rule_count, findings)
                VALUES (%s, %s, %s)
                """,
                (state["run_id"], evaluated_rule_count, Jsonb(findings)),
            )
            review_items: list[dict[str, object]] = []
            for proposal in proposals:
                item = {
                    "id": proposal.proposal_id,
                    "kind": "PROPOSED_MUTATION",
                    "subject_id": proposal.record_id,
                    "payload": self._proposal_dict(proposal),
                }
                review_items.append(item)
            for conflict in conflict_rows:
                review_items.append(
                    {
                        "id": f"review:{conflict['id']}",
                        "kind": "CONFLICT",
                        "subject_id": conflict["record_id"],
                        "payload": conflict["payload"],
                    }
                )
            for finding in findings:
                if finding.get("requiresHumanReview"):
                    review_items.append(
                        {
                            "id": str(finding["findingId"]),
                            "kind": "RULE_FINDING",
                            "subject_id": str(finding["recordId"]),
                            "payload": finding,
                        }
                    )
            for item in review_items:
                connection.execute(
                    """
                    INSERT INTO review_items (id, run_id, kind, subject_id, payload)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (
                        item["id"], state["run_id"], item["kind"],
                        item["subject_id"], Jsonb(item["payload"]),
                    ),
                )
            status = "AWAITING_REVIEW" if review_items else "COMPLETED"
            connection.execute(
                """
                UPDATE workflow_runs
                SET status = %s, affected_entity_keys = %s,
                    preparation_completed_at = now(), updated_at = now()
                WHERE id = %s
                """,
                (status, Jsonb(affected_keys), state["run_id"]),
            )
            stage_values = (
                ("INGEST", "COMPLETED", ModelUsage()),
                ("RETRIEVE_OR_PLAN", "COMPLETED", model_usage),
                ("CONFLICTS", "COMPLETED", ModelUsage()),
                ("RULES", "COMPLETED", ModelUsage()),
                (
                    "HUMAN_REVIEW",
                    "AWAITING_REVIEW" if review_items else "SKIPPED",
                    ModelUsage(),
                ),
            )
            for stage, stage_status, usage in stage_values:
                connection.execute(
                    """
                    INSERT INTO stage_executions (
                        id, run_id, stage, status, affected_entity_keys,
                        skipped_entity_keys, completed_at, model_name, input_tokens,
                        output_tokens, estimated_cost_usd
                    ) VALUES (%s, %s, %s, %s, %s, %s,
                        CASE WHEN %s = 'AWAITING_REVIEW' THEN NULL ELSE now() END,
                        %s, %s, %s, %s)
                    """,
                    (
                        f"stage:{uuid4()}", state["run_id"], stage, stage_status,
                        Jsonb(affected_keys), Jsonb(skipped_keys), stage_status,
                        usage.model, usage.input_tokens, usage.output_tokens,
                        usage.estimated_cost_usd,
                    ),
                )
            if not review_items:
                self._mark_sources_analyzed(
                    connection,
                    run_id=state["run_id"],
                    corpus_id=state["corpus_id"],
                    register_version=state["base_register_version"],
                )
        return {
            "proposals": [self._proposal_dict(value) for value in proposals],
            "review_items": review_items,
            "status": status,
        }

    def _persisted_preparation_state(
        self, connection: psycopg.Connection, run_id: str
    ) -> GraphState:
        run = connection.execute(
            "SELECT status FROM workflow_runs WHERE id = %s",
            (run_id,),
        ).fetchone()
        if run is None:
            raise LookupError("Workflow Run not found")
        proposals = connection.execute(
            """
            SELECT id, record_id, record_type, before_value, after_value, evidence
            FROM proposed_mutations WHERE run_id = %s ORDER BY record_id
            """,
            (run_id,),
        ).fetchall()
        review_items = connection.execute(
            """
            SELECT id, kind, subject_id, payload
            FROM review_items WHERE run_id = %s ORDER BY created_at, id
            """,
            (run_id,),
        ).fetchall()
        return {
            "proposals": [
                self._proposal_dict(self._proposal_from_row(proposal))
                for proposal in proposals
            ],
            "review_items": [
                {
                    "id": item["id"],
                    "kind": item["kind"],
                    "subject_id": item["subject_id"],
                    "payload": item["payload"],
                }
                for item in review_items
            ],
            "status": run["status"],
        }

    def _persist_conflicts(
        self,
        connection: psycopg.Connection,
        state: GraphState,
        conflict_specs: list[dict[str, object]],
    ) -> list[dict[str, object]]:
        persisted: list[dict[str, object]] = []
        for spec in conflict_specs:
            conflict_id = f"conflict:{uuid4()}"
            connection.execute(
                """
                INSERT INTO conflicts (id, corpus_id, run_id, record_id, field, claims)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    conflict_id, state["corpus_id"], state["run_id"],
                    spec["recordId"], spec["field"], Jsonb(spec["claims"]),
                ),
            )
            persisted.append(
                {
                    "id": conflict_id,
                    "record_id": spec["recordId"],
                    "payload": {"conflictId": conflict_id, **spec},
                }
            )
        return persisted

    def _evaluate_rules(
        self,
        connection: psycopg.Connection,
        state: GraphState,
        proposals: list[ProposedMutation],
        conflict_specs: list[dict[str, object]],
    ) -> tuple[int, list[dict[str, object]]]:
        affected_record_types = {proposal.record_type for proposal in proposals} | {
            str(spec["recordType"]) for spec in conflict_specs
        }
        selected_rules = [
            rule
            for rule in self.rule_pack.rules
            if affected_record_types.intersection(rule.applies_to.record_types)
        ]
        effective = {
            row["record_id"]: {
                "recordType": row["record_type"],
                "value": row["value"],
                "evidence": row["evidence"],
            }
            for row in connection.execute(
                """
                SELECT record_id, record_type, value, evidence
                FROM register_records
                WHERE corpus_id = %s AND register_version = %s
                """,
                (state["corpus_id"], state["base_register_version"]),
            ).fetchall()
        }
        for proposal in proposals:
            effective[proposal.record_id] = {
                "recordType": proposal.record_type,
                "value": proposal.after,
                "evidence": [
                    self._evidence_dict(value) for value in proposal.evidence
                ],
            }
        for spec in conflict_specs:
            # Evaluate each supported conflict candidate, not the stale base record
            # plus those same candidates a second time.
            effective.pop(str(spec["recordId"]), None)
            for index, claim in enumerate(spec["claims"]):  # type: ignore[union-attr]
                effective[f"{spec['recordId']}#conflict:{index}"] = {
                    "recordType": spec["recordType"],
                    "value": claim["value"],
                    "evidence": claim["evidence"],
                    "displayRecordId": spec["recordId"],
                }
        findings: list[dict[str, object]] = []
        finding_by_key: dict[tuple[str, str, tuple[str, ...]], dict[str, object]] = {}
        for rule in selected_rules:
            for record_id, record in effective.items():
                if record["recordType"] not in rule.applies_to.record_types:
                    continue
                value = record["value"]
                missing = [
                    field
                    for field in rule.required_fields
                    if not isinstance(value, dict) or value.get(field) in {None, ""}
                ]
                if missing:
                    display_record_id = str(record.get("displayRecordId", record_id))
                    finding_key = (rule.id, display_record_id, tuple(missing))
                    existing_finding = finding_by_key.get(finding_key)
                    if existing_finding is not None:
                        evidence = existing_finding["evidence"]
                        if not isinstance(evidence, list):
                            raise RuntimeError("Persisted rule-finding evidence is malformed")
                        known = {
                            json.dumps(item, sort_keys=True, separators=(",", ":"))
                            for item in evidence
                        }
                        for item in record["evidence"]:  # type: ignore[union-attr]
                            canonical = json.dumps(
                                item, sort_keys=True, separators=(",", ":")
                            )
                            if canonical not in known:
                                evidence.append(item)
                                known.add(canonical)
                        continue
                    finding = {
                        "findingId": f"finding:{uuid4()}",
                        "ruleId": rule.id,
                        "recordId": display_record_id,
                        "severity": rule.severity,
                        "findingType": rule.on_failure.finding_type,
                        "missingFields": missing,
                        "status": "INSUFFICIENT_EVIDENCE",
                        "evidence": list(record["evidence"]),  # type: ignore[arg-type]
                        "requiresHumanReview": rule.on_failure.requires_human_review,
                    }
                    finding_by_key[finding_key] = finding
                    findings.append(finding)
        return len(selected_rules), findings

    @staticmethod
    def _route_after_prepare(state: GraphState) -> str:
        return "review" if state.get("review_items") else "done"

    @staticmethod
    def _human_review(state: GraphState) -> GraphState:
        if not state.get("review_items"):
            return {"decisions": []}
        resumed = interrupt(
            {
                "kind": "GOVERNED_REVIEW_ITEMS",
                "runId": state["run_id"],
                "reviewItems": state["review_items"],
            }
        )
        if not isinstance(resumed, dict) or not isinstance(resumed.get("decisions"), list):
            raise TypeError("Resume payload requires decisions")
        return {"decisions": resumed["decisions"]}

    def _commit_version(self, state: GraphState) -> GraphState:
        decisions = state.get("decisions", [])
        with self._connect() as connection, connection.transaction():
            current = connection.execute(
                "SELECT version FROM registers WHERE corpus_id = %s FOR UPDATE",
                (state["corpus_id"],),
            ).fetchone()
            if current is None or current["version"] != state["base_register_version"]:
                raise RuntimeError("Control Register version conflict")
            mutations: list[dict[str, object]] = []
            for decision in decisions:
                item = connection.execute(
                    """
                    SELECT id, kind, subject_id, payload
                    FROM review_items WHERE id = %s AND run_id = %s
                    """,
                    (decision["review_item_id"], state["run_id"]),
                ).fetchone()
                if item is None:
                    raise ValueError("Decision references an unknown review item")
                resolution = decision.get("resolution")
                if item["kind"] == "CONFLICT" and decision["is_approved"]:
                    candidates = [claim["value"] for claim in item["payload"]["claims"]]
                    if resolution not in candidates or not isinstance(resolution, dict):
                        raise ValueError(
                            "An approved conflict requires resolution equal to one supported claim"
                        )
                    selected = next(
                        claim for claim in item["payload"]["claims"]
                        if claim["value"] == resolution
                    )
                    mutations.append(
                        {
                            "record_id": item["subject_id"],
                            "record_type": item["payload"]["recordType"],
                            "after_value": resolution,
                            "evidence": selected["evidence"],
                        }
                    )
                elif item["kind"] == "PROPOSED_MUTATION" and decision["is_approved"]:
                    mutations.append(
                        {
                            "record_id": item["subject_id"],
                            "record_type": item["payload"]["record_type"],
                            "after_value": item["payload"]["after"],
                            "evidence": item["payload"]["evidence"],
                        }
                    )
                connection.execute(
                    """
                    INSERT INTO review_item_decisions (
                        review_item_id, is_approved, reason, resolution
                    ) VALUES (%s, %s, %s, %s)
                    """,
                    (
                        decision["review_item_id"],
                        decision["is_approved"],
                        decision.get("reason"),
                        Jsonb(resolution) if resolution is not None else None,
                    ),
                )
                review_state = "APPROVED" if decision["is_approved"] else "REJECTED"
                connection.execute(
                    "UPDATE review_items SET review_state = %s WHERE id = %s",
                    (review_state, decision["review_item_id"]),
                )
                if item["kind"] == "PROPOSED_MUTATION":
                    connection.execute(
                        "UPDATE proposed_mutations SET review_state = %s WHERE id = %s",
                        (review_state, item["id"]),
                    )
                if item["kind"] == "CONFLICT" and decision["is_approved"]:
                    connection.execute(
                        "UPDATE conflicts SET status = 'RESOLVED' WHERE id = %s",
                        (item["payload"]["conflictId"],),
                    )
            new_version = state["base_register_version"]
            if mutations:
                new_version += 1
                connection.execute(
                    "UPDATE registers SET version = %s WHERE corpus_id = %s",
                    (new_version, state["corpus_id"]),
                )
                connection.execute(
                    "INSERT INTO register_versions (corpus_id, version) VALUES (%s, %s)",
                    (state["corpus_id"], new_version),
                )
                connection.execute(
                    """
                    INSERT INTO register_records (
                        corpus_id, register_version, record_id, record_type,
                        value, evidence, canonical_hash
                    )
                    SELECT corpus_id, %s, record_id, record_type, value, evidence, canonical_hash
                    FROM register_records
                    WHERE corpus_id = %s AND register_version = %s
                    """,
                    (new_version, state["corpus_id"], state["base_register_version"]),
                )
            for proposal in mutations:
                canonical = hashlib.sha256(
                    json.dumps(
                        proposal["after_value"], sort_keys=True, separators=(",", ":")
                    ).encode()
                ).hexdigest()
                connection.execute(
                    "DELETE FROM register_records WHERE corpus_id = %s AND register_version = %s AND record_id = %s",
                    (state["corpus_id"], new_version, proposal["record_id"]),
                )
                connection.execute(
                    """
                    INSERT INTO register_records (
                        corpus_id, register_version, record_id, record_type,
                        value, evidence, canonical_hash
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        state["corpus_id"],
                        new_version,
                        proposal["record_id"],
                        proposal["record_type"],
                        Jsonb(proposal["after_value"]),
                        Jsonb(proposal["evidence"]),
                        canonical,
                    ),
                )
                for evidence in proposal["evidence"]:
                    connection.execute(
                        """
                        INSERT INTO dependency_edges (
                            id, corpus_id, source_span_id, record_id, rule_id
                        ) VALUES (%s, %s, %s, %s, NULL)
                        ON CONFLICT DO NOTHING
                        """,
                        (
                            f"dependency:{uuid4()}",
                            state["corpus_id"],
                            evidence["span_id"],
                            proposal["record_id"],
                        ),
                    )
            self._mark_sources_analyzed(
                connection,
                run_id=state["run_id"],
                corpus_id=state["corpus_id"],
                register_version=new_version,
            )
            connection.execute(
                "UPDATE workflow_runs SET status = 'COMPLETED', updated_at = now() WHERE id = %s",
                (state["run_id"],),
            )
            connection.execute(
                """
                UPDATE stage_executions
                SET status = 'COMPLETED', completed_at = now()
                WHERE run_id = %s AND stage = 'HUMAN_REVIEW'
                """,
                (state["run_id"],),
            )
            connection.execute(
                """
                INSERT INTO stage_executions (
                    id, run_id, stage, status, affected_entity_keys,
                    skipped_entity_keys, completed_at
                ) VALUES (%s, %s, 'COMMIT', 'COMPLETED', %s, %s, now())
                """,
                (
                    f"stage:{uuid4()}", state["run_id"],
                    Jsonb(sorted({str(item["record_id"]) for item in mutations})),
                    Jsonb([]),
                ),
            )
        return {"status": "COMPLETED"}

    def _build_proposals(
        self, *, run_id: str, corpus_id: str, base_version: int
    ) -> tuple[list[ProposedMutation], list[dict[str, object]], ModelUsage]:
        with self._connect() as connection:
            spans = connection.execute(
                """
                SELECT ss.id, ss.source_id, ss.source_version_id, ss.text,
                       ss.locator, ss.content_hash
                FROM source_spans ss
                JOIN sources s ON s.id = ss.source_id
                JOIN source_versions sv ON sv.id = ss.source_version_id
                JOIN workflow_run_source_versions wrsv
                  ON wrsv.source_version_id = sv.id AND wrsv.run_id = %s
                WHERE s.corpus_id = %s
                ORDER BY s.created_at, ss.ordinal
                """,
                (run_id, corpus_id),
            ).fetchall()
            existing = {
                row["record_id"]: row
                for row in connection.execute(
                    """
                    SELECT record_id, record_type, value, evidence FROM register_records
                    WHERE corpus_id = %s AND register_version = %s
                    """,
                    (corpus_id, base_version),
                ).fetchall()
            }
        candidates: dict[str, list[dict[str, object]]] = {}
        usage = ModelUsage()
        for span in spans:
            extraction = self.model_adapter.extract_claim(span["text"])
            usage = usage.plus(extraction.usage)
            parsed_claim = extraction.claim
            if parsed_claim is None:
                continue
            record_type = parsed_claim.record_type
            record_id = parsed_claim.record_id
            after = parsed_claim.value
            evidence = EvidenceReference(
                span_id=span["id"],
                source_id=span["source_id"],
                source_version_id=span["source_version_id"],
                quote=span["text"],
                locator=span["locator"],
                content_hash=span["content_hash"],
            )
            grounding = self.evidence_validator.validate(
                corpus_id=corpus_id,
                candidate=ClaimCandidate(
                    record_id=record_id,
                    record_type=record_type,
                    value=after,
                    evidence=(evidence,),
                ),
            )
            if not grounding.is_valid:
                continue
            candidates.setdefault(record_id, []).append(
                {
                    "record_type": record_type,
                    "value": after,
                    "evidence": [evidence],
                }
            )
        proposals: list[ProposedMutation] = []
        conflicts: list[dict[str, object]] = []
        for record_id in sorted(candidates):
            grouped: dict[str, dict[str, object]] = {}
            for candidate in candidates[record_id]:
                canonical = json.dumps(
                    candidate["value"], sort_keys=True, separators=(",", ":")
                )
                if canonical not in grouped:
                    grouped[canonical] = candidate
                else:
                    grouped[canonical]["evidence"].extend(candidate["evidence"])  # type: ignore[union-attr]
            new_canonicals = set(grouped)
            current = existing.get(record_id)
            current_canonical: str | None = None
            if current is not None:
                current_canonical = json.dumps(
                    current["value"], sort_keys=True, separators=(",", ":")
                )
                grouped.setdefault(
                    current_canonical,
                    {
                        "record_type": current["record_type"],
                        "value": current["value"],
                        "evidence": [
                            self._evidence_from_dict(value) for value in current["evidence"]
                        ],
                    },
                )
            lifecycle_update = False
            selected_update: dict[str, object] | None = None
            if (
                current is not None
                and current_canonical is not None
                and len(new_canonicals) == 1
                and current_canonical not in new_canonicals
            ):
                selected_update = grouped[next(iter(new_canonicals))]
                previous_value = current["value"]
                next_value = selected_update["value"]
                if isinstance(previous_value, dict) and isinstance(next_value, dict):
                    changed_fields = {
                        key
                        for key in set(previous_value) | set(next_value)
                        if previous_value.get(key) != next_value.get(key)
                    }
                    # Status is a lifecycle update, not contradictory project truth.
                    # Structural fields such as dates, owners and scope still become
                    # explicit conflicts requiring a supported human resolution.
                    lifecycle_update = bool(changed_fields) and changed_fields <= {"status"}
            if len(grouped) > 1 and not lifecycle_update:
                values = [item["value"] for _, item in sorted(grouped.items())]
                fields = sorted(
                    {
                        key
                        for value in values
                        if isinstance(value, dict)
                        for key in value
                        if len({json.dumps(other.get(key), sort_keys=True) for other in values if isinstance(other, dict)}) > 1
                    }
                )
                conflict_candidates = []
                for _, item in sorted(grouped.items()):
                    conflict_candidates.append(
                        {
                            "value": item["value"],
                            "evidence": [
                                self._evidence_dict(value)
                                for value in item["evidence"]  # type: ignore[union-attr]
                            ],
                        }
                    )
                conflicts.append(
                    {
                        "recordId": record_id,
                        "recordType": next(iter(grouped.values()))["record_type"],
                        "field": ",".join(fields) or "*",
                        "claims": conflict_candidates,
                        "status": "OPEN",
                    }
                )
                continue
            selected = selected_update if lifecycle_update else next(iter(grouped.values()))
            if current is not None and current["value"] == selected["value"]:
                continue
            proposals.append(
                ProposedMutation(
                    proposal_id=f"proposal:{uuid4()}",
                    record_id=record_id,
                    record_type=str(selected["record_type"]),
                    before=current["value"] if current is not None else None,
                    after=selected["value"],  # type: ignore[arg-type]
                    evidence=tuple(selected["evidence"]),  # type: ignore[arg-type]
                )
            )
        return proposals, conflicts, usage

    @staticmethod
    def _mark_sources_analyzed(
        connection: psycopg.Connection,
        *,
        run_id: str,
        corpus_id: str,
        register_version: int,
    ) -> None:
        connection.execute(
            """
            UPDATE source_versions sv
            SET analyzed_in_register_version = %s
            FROM sources s, workflow_run_source_versions wrsv
            WHERE sv.source_id = s.id
              AND wrsv.source_version_id = sv.id
              AND wrsv.run_id = %s
              AND s.corpus_id = %s
              AND sv.analyzed_in_register_version IS NULL
            """,
            (register_version, run_id, corpus_id),
        )

    @staticmethod
    def _config(run_id: str) -> dict[str, dict[str, str]]:
        return {"configurable": {"thread_id": run_id}}

    @staticmethod
    def _evidence_dict(value: EvidenceReference) -> dict[str, object]:
        return {
            "span_id": value.span_id,
            "source_id": value.source_id,
            "source_version_id": value.source_version_id,
            "quote": value.quote,
            "locator": value.locator,
            "content_hash": value.content_hash,
        }

    @staticmethod
    def _evidence_from_dict(value: dict[str, object]) -> EvidenceReference:
        return EvidenceReference(
            span_id=str(value["span_id"]),
            source_id=str(value["source_id"]),
            source_version_id=str(value["source_version_id"]),
            quote=str(value["quote"]),
            locator=dict(value["locator"]),  # type: ignore[arg-type]
            content_hash=str(value["content_hash"]),
        )

    def _proposal_dict(self, value: ProposedMutation) -> dict[str, object]:
        return {
            "proposal_id": value.proposal_id,
            "record_id": value.record_id,
            "record_type": value.record_type,
            "before": value.before,
            "after": value.after,
            "evidence": [self._evidence_dict(item) for item in value.evidence],
        }

    def _proposal_from_row(self, row: dict[str, object]) -> ProposedMutation:
        return ProposedMutation(
            proposal_id=str(row["id"]),
            record_id=str(row["record_id"]),
            record_type=str(row["record_type"]),
            before=dict(row["before_value"]) if row["before_value"] is not None else None,  # type: ignore[arg-type]
            after=dict(row["after_value"]),  # type: ignore[arg-type]
            evidence=tuple(
                self._evidence_from_dict(value) for value in row["evidence"]  # type: ignore[union-attr]
            ),
        )
