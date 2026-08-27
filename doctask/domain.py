from dataclasses import dataclass, field
from typing import Literal

RunStatus = Literal["RUNNING", "AWAITING_REVIEW", "COMPLETED"]
ReviewItemKind = Literal["PROPOSED_MUTATION", "CONFLICT", "RULE_FINDING"]
ReviewState = Literal["PENDING", "APPROVED", "REJECTED"]


@dataclass(frozen=True)
class Corpus:
    corpus_id: str
    name: str


@dataclass(frozen=True)
class SourceReceipt:
    source_id: str
    source_version_id: str
    is_duplicate: bool


@dataclass(frozen=True)
class SourceSpan:
    span_id: str
    source_id: str
    source_version_id: str
    text: str
    locator: dict[str, object]
    content_hash: str


@dataclass(frozen=True)
class EvidenceReference:
    span_id: str
    source_id: str
    source_version_id: str
    quote: str
    locator: dict[str, object]
    content_hash: str


@dataclass(frozen=True)
class ProposedMutation:
    proposal_id: str
    record_id: str
    record_type: str
    before: dict[str, object] | None
    after: dict[str, object]
    evidence: tuple[EvidenceReference, ...]


@dataclass(frozen=True)
class ReviewItem:
    review_item_id: str
    kind: ReviewItemKind
    subject_id: str
    payload: dict[str, object]
    review_state: ReviewState = "PENDING"


@dataclass(frozen=True)
class WorkflowRun:
    run_id: str
    corpus_id: str
    base_register_version: int
    status: RunStatus
    proposals: tuple[ProposedMutation, ...] = ()
    review_items: tuple[ReviewItem, ...] = ()


@dataclass(frozen=True)
class RegisterRecord:
    record_id: str
    record_type: str
    value: dict[str, object]
    evidence: tuple[EvidenceReference, ...]
    canonical_hash: str


@dataclass(frozen=True)
class ControlRegister:
    corpus_id: str
    version: int
    records: tuple[RegisterRecord, ...] = ()


@dataclass
class CorpusState:
    corpus: Corpus
    source_digests: dict[str, SourceReceipt] = field(default_factory=dict)
    spans: list[SourceSpan] = field(default_factory=list)
    register: ControlRegister | None = None
