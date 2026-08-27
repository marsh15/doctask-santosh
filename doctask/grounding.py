import hashlib
from collections.abc import Callable
from dataclasses import dataclass

from doctask.claims import parse_claim_text
from doctask.domain import EvidenceReference


@dataclass(frozen=True)
class ClaimCandidate:
    record_id: str
    record_type: str
    value: dict[str, object]
    evidence: tuple[EvidenceReference, ...]
    confidence: float = 1.0
    uncertainty_reason: str | None = None


@dataclass(frozen=True)
class GroundingResult:
    is_valid: bool
    reason_code: str | None = None


class EvidenceValidator:
    def __init__(self, connect: Callable):
        self._connect = connect

    def validate(self, *, corpus_id: str, candidate: ClaimCandidate) -> GroundingResult:
        if not candidate.evidence:
            return GroundingResult(False, "SPAN_NOT_FOUND")
        span_ids = [reference.span_id for reference in candidate.evidence]
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT ss.id, ss.source_id, ss.source_version_id, ss.text,
                       ss.locator, ss.content_hash
                FROM source_spans ss
                JOIN sources s ON s.id = ss.source_id
                WHERE s.corpus_id = %s AND ss.id = ANY(%s)
                """,
                (corpus_id, span_ids),
            ).fetchall()
        by_id = {row["id"]: row for row in rows}
        if len(by_id) != len(set(span_ids)):
            return GroundingResult(False, "SPAN_NOT_FOUND_OR_CROSS_CORPUS")
        support_found = False
        for reference in candidate.evidence:
            row = by_id[reference.span_id]
            if row["source_id"] != reference.source_id or row["source_version_id"] != reference.source_version_id:
                return GroundingResult(False, "SOURCE_VERSION_MISMATCH")
            if row["text"] != reference.quote:
                return GroundingResult(False, "QUOTE_MISMATCH")
            actual_hash = hashlib.sha256(row["text"].encode()).hexdigest()
            if actual_hash != row["content_hash"] or actual_hash != reference.content_hash:
                return GroundingResult(False, "HASH_MISMATCH")
            parsed = parse_claim_text(row["text"])
            if (
                parsed is not None
                and parsed.record_id == candidate.record_id
                and parsed.record_type == candidate.record_type
                and parsed.value == candidate.value
            ):
                support_found = True
        if not support_found:
            return GroundingResult(False, "VALUE_NOT_SUPPORTED")
        return GroundingResult(True)

