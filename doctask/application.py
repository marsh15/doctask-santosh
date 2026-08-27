import hashlib
import json
import re
from dataclasses import dataclass, replace
from datetime import date
from uuid import uuid4

from doctask.domain import (
    ControlRegister,
    Corpus,
    CorpusState,
    EvidenceReference,
    ProposedMutation,
    RegisterRecord,
    SourceReceipt,
    WorkflowRun,
)
from doctask.ingestion import parse_docx


@dataclass(frozen=True)
class DecisionInput:
    proposal_id: str
    is_approved: bool
    reason: str | None = None
    resolution: dict[str, object] | None = None

    @property
    def review_item_id(self) -> str:
        """Compatibility alias while transports migrate from proposalId."""
        return self.proposal_id


_MILESTONE = re.compile(
    r"^Milestone (?P<key>[A-Za-z0-9_-]+): (?P<name>.+?) - due "
    r"(?P<day>\d{1,2}) (?P<month>[A-Za-z]+) (?P<year>\d{4}) - status "
    r"(?P<status>[A-Za-z ]+)\.$"
)
_MONTHS = {
    "January": 1,
    "February": 2,
    "March": 3,
    "April": 4,
    "May": 5,
    "June": 6,
    "July": 7,
    "August": 8,
    "September": 9,
    "October": 10,
    "November": 11,
    "December": 12,
}


class OfflineMilestoneSlice:
    """Narrow, in-memory parser acceptance harness; not the deployed service."""

    def __init__(self) -> None:
        self._corpora: dict[str, CorpusState] = {}
        self._runs: dict[str, WorkflowRun] = {}

    @classmethod
    def for_offline_testing(cls) -> "OfflineMilestoneSlice":
        return cls()

    def create_corpus(self, *, name: str) -> Corpus:
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("Corpus name is required")
        corpus = Corpus(corpus_id=f"corpus:{uuid4()}", name=clean_name)
        self._corpora[corpus.corpus_id] = CorpusState(
            corpus=corpus,
            register=ControlRegister(corpus_id=corpus.corpus_id, version=0),
        )
        return corpus

    def add_source(self, *, corpus_id: str, filename: str, content: bytes) -> SourceReceipt:
        state = self._corpus(corpus_id)
        if not filename.lower().endswith(".docx"):
            raise ValueError("Slice T1-A accepts DOCX sources only")
        digest = hashlib.sha256(content).hexdigest()
        existing = state.source_digests.get(digest)
        if existing is not None:
            return SourceReceipt(
                source_id=existing.source_id,
                source_version_id=existing.source_version_id,
                is_duplicate=True,
            )
        source_id = f"source:{uuid4()}"
        receipt = SourceReceipt(
            source_id=source_id,
            source_version_id=f"source-version:{digest[:24]}",
            is_duplicate=False,
        )
        state.spans.extend(
            parse_docx(
                source_id=source_id,
                source_version_id=receipt.source_version_id,
                content=content,
            )
        )
        state.source_digests[digest] = receipt
        return receipt

    def start_analysis(self, *, corpus_id: str) -> WorkflowRun:
        state = self._corpus(corpus_id)
        proposals: list[ProposedMutation] = []
        for span in state.spans:
            match = _MILESTONE.fullmatch(span.text)
            if match is None:
                continue
            month = _MONTHS.get(match.group("month"))
            if month is None:
                continue
            due_date = date(
                int(match.group("year")), month, int(match.group("day"))
            ).isoformat()
            after: dict[str, object] = {
                "name": match.group("name"),
                "dueDate": due_date,
                "status": match.group("status").strip().upper().replace(" ", "_"),
            }
            evidence = EvidenceReference(
                span_id=span.span_id,
                source_id=span.source_id,
                source_version_id=span.source_version_id,
                quote=span.text,
                locator=span.locator,
                content_hash=span.content_hash,
            )
            record_id = f"milestone:{match.group('key')}"
            existing = self._record(state.register, record_id)
            proposals.append(
                ProposedMutation(
                    proposal_id=f"proposal:{uuid4()}",
                    record_id=record_id,
                    record_type="MILESTONE",
                    before=existing.value if existing else None,
                    after=after,
                    evidence=(evidence,),
                )
            )
        register = state.register or ControlRegister(corpus_id=corpus_id, version=0)
        run = WorkflowRun(
            run_id=f"run:{uuid4()}",
            corpus_id=corpus_id,
            base_register_version=register.version,
            status="AWAITING_REVIEW" if proposals else "COMPLETED",
            proposals=tuple(proposals),
        )
        self._runs[run.run_id] = run
        return run

    def submit_decisions(
        self, *, run_id: str, decisions: list[DecisionInput]
    ) -> WorkflowRun:
        run = self._runs.get(run_id)
        if run is None:
            raise LookupError("Workflow Run not found")
        if run.status != "AWAITING_REVIEW":
            raise ValueError("Workflow Run is not awaiting review")
        by_id = {decision.proposal_id: decision for decision in decisions}
        expected = {proposal.proposal_id for proposal in run.proposals}
        if set(by_id) != expected:
            raise ValueError("Every Proposed Mutation requires exactly one Human Decision")
        state = self._corpus(run.corpus_id)
        current = state.register or ControlRegister(corpus_id=run.corpus_id, version=0)
        if current.version != run.base_register_version:
            raise ValueError("Control Register changed after this Workflow Run began")
        records = {record.record_id: record for record in current.records}
        for proposal in run.proposals:
            if not by_id[proposal.proposal_id].is_approved:
                continue
            canonical = hashlib.sha256(
                json.dumps(proposal.after, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            records[proposal.record_id] = RegisterRecord(
                record_id=proposal.record_id,
                record_type=proposal.record_type,
                value=proposal.after,
                evidence=proposal.evidence,
                canonical_hash=canonical,
            )
        state.register = ControlRegister(
            corpus_id=run.corpus_id,
            version=current.version + 1,
            records=tuple(records[key] for key in sorted(records)),
        )
        completed = replace(run, status="COMPLETED")
        self._runs[run_id] = completed
        return completed

    def get_register(self, *, corpus_id: str) -> ControlRegister:
        state = self._corpus(corpus_id)
        return state.register or ControlRegister(corpus_id=corpus_id, version=0)

    def _corpus(self, corpus_id: str) -> CorpusState:
        state = self._corpora.get(corpus_id)
        if state is None:
            raise LookupError("Corpus not found")
        return state

    @staticmethod
    def _record(register: ControlRegister | None, record_id: str) -> RegisterRecord | None:
        if register is None:
            return None
        return next((record for record in register.records if record.record_id == record_id), None)
