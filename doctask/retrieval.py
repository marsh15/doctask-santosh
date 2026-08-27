import hashlib
import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from psycopg.rows import dict_row

from doctask.domain import SourceSpan

VectorStatus = Literal["ENABLED", "DISABLED"]
EMBEDDING_DIMENSIONS = 32


def deterministic_embedding(text: str) -> list[float]:
    """Small, key-free feature-hash embedding used for capability/offline proof."""
    values = [0.0] * EMBEDDING_DIMENSIONS
    for token in re.findall(r"[a-z0-9]+", text.lower()):
        digest = hashlib.sha256(token.encode()).digest()
        index = int.from_bytes(digest[:2], "big") % EMBEDDING_DIMENSIONS
        values[index] += 1.0 if digest[2] & 1 else -1.0
    norm = math.sqrt(sum(value * value for value in values)) or 1.0
    return [value / norm for value in values]


def vector_literal(values: list[float]) -> str:
    return "[" + ",".join(f"{value:.8f}" for value in values) + "]"


@dataclass(frozen=True)
class RetrievalHit:
    span: SourceSpan
    exact_rank: int | None
    fts_rank: int | None
    semantic_rank: int | None
    fused_score: float


def reciprocal_rank_fusion(
    *,
    exact: list[str],
    full_text: list[str],
    semantic: list[str],
    limit: int,
) -> list[tuple[str, float]]:
    scores: dict[str, float] = {}
    for weight, ranked_ids in ((3.0, exact), (2.0, full_text), (1.0, semantic)):
        for rank, span_id in enumerate(ranked_ids, start=1):
            scores[span_id] = scores.get(span_id, 0.0) + weight / (60 + rank)
    return sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:limit]


class PostgresEvidenceRetriever:
    def __init__(self, connect: Callable, *, vector_enabled: bool = False):
        self._connect = connect
        self.vector_status: VectorStatus = "ENABLED" if vector_enabled else "DISABLED"

    def search(
        self,
        *,
        corpus_id: str,
        query: str,
        entity_keys: tuple[str, ...] = (),
        limit: int = 8,
    ) -> list[RetrievalHit]:
        clean_query = query.strip()
        if not clean_query:
            raise ValueError("Retrieval query is required")
        if not 1 <= limit <= 20:
            raise ValueError("Retrieval limit must be between 1 and 20")
        semantic_ids: list[str] = []
        with self._connect() as connection:
            connection.row_factory = dict_row
            if self.vector_status == "ENABLED":
                semantic_ids = [
                    row["span_id"]
                    for row in connection.execute(
                        """
                        SELECT e.span_id
                        FROM source_span_embeddings e
                        WHERE e.corpus_id = %s
                        ORDER BY e.embedding <=> %s::vector, e.span_id
                        LIMIT 40
                        """,
                        (corpus_id, vector_literal(deterministic_embedding(clean_query))),
                    ).fetchall()
                ]
            rows = connection.execute(
                """
                SELECT ss.id, ss.source_id, ss.source_version_id, ss.text,
                       ss.locator, ss.content_hash,
                       CASE WHEN position(lower(%s) in lower(ss.text)) > 0
                            THEN true ELSE false END AS exact_match,
                       ts_rank_cd(ss.search_vector,
                           websearch_to_tsquery('english', %s)) AS fts_score,
                       EXISTS (
                           SELECT 1 FROM source_span_entities se
                           WHERE se.span_id = ss.id
                             AND se.corpus_id = %s
                             AND se.entity_key = ANY(%s)
                       ) AS entity_match
                FROM source_spans ss
                JOIN sources s ON s.id = ss.source_id
                WHERE s.corpus_id = %s
                  AND (
                    ss.search_vector @@ websearch_to_tsquery('english', %s)
                    OR position(lower(%s) in lower(ss.text)) > 0
                    OR EXISTS (
                        SELECT 1 FROM source_span_entities se
                        WHERE se.span_id = ss.id
                          AND se.corpus_id = %s
                          AND se.entity_key = ANY(%s)
                    ) OR ss.id = ANY(%s)
                  )
                ORDER BY ss.id
                LIMIT 40
                """,
                (
                    clean_query,
                    clean_query,
                    corpus_id,
                    list(entity_keys),
                    corpus_id,
                    clean_query,
                    clean_query,
                    corpus_id,
                    list(entity_keys), semantic_ids,
                ),
            ).fetchall()
        exact_rows = sorted(
            (row for row in rows if row["exact_match"] or row["entity_match"]),
            key=lambda row: (not row["entity_match"], row["id"]),
        )
        fts_rows = sorted(
            (row for row in rows if row["fts_score"] > 0),
            key=lambda row: (-row["fts_score"], row["id"]),
        )
        exact_ids = [row["id"] for row in exact_rows]
        fts_ids = [row["id"] for row in fts_rows]
        semantic_ids = [span_id for span_id in semantic_ids if span_id in {r["id"] for r in rows}]
        fused = reciprocal_rank_fusion(
            exact=exact_ids, full_text=fts_ids, semantic=semantic_ids, limit=limit
        )
        by_id = {row["id"]: row for row in rows}
        return [
            RetrievalHit(
                span=SourceSpan(
                    span_id=span_id,
                    source_id=by_id[span_id]["source_id"],
                    source_version_id=by_id[span_id]["source_version_id"],
                    text=by_id[span_id]["text"],
                    locator=by_id[span_id]["locator"],
                    content_hash=by_id[span_id]["content_hash"],
                ),
                exact_rank=(exact_ids.index(span_id) + 1 if span_id in exact_ids else None),
                fts_rank=(fts_ids.index(span_id) + 1 if span_id in fts_ids else None),
                semantic_rank=(semantic_ids.index(span_id) + 1 if span_id in semantic_ids else None),
                fused_score=score,
            )
            for span_id, score in fused
        ]
