from doctask.domain import (
    ControlRegister,
    Corpus,
    EvidenceReference,
    ProposedMutation,
    SourceReceipt,
    WorkflowRun,
)


def evidence(value: EvidenceReference) -> dict[str, object]:
    return {
        "spanId": value.span_id,
        "sourceId": value.source_id,
        "sourceVersionId": value.source_version_id,
        "quote": value.quote,
        "locator": value.locator,
        "contentHash": value.content_hash,
    }


def proposal(value: ProposedMutation) -> dict[str, object]:
    return {
        "proposalId": value.proposal_id,
        "recordId": value.record_id,
        "recordType": value.record_type,
        "before": value.before,
        "after": value.after,
        "evidence": [evidence(item) for item in value.evidence],
    }


def run(value: WorkflowRun) -> dict[str, object]:
    return {
        "runId": value.run_id,
        "corpusId": value.corpus_id,
        "baseRegisterVersion": value.base_register_version,
        "status": value.status,
        "proposals": [proposal(item) for item in value.proposals],
        "reviewItems": [
            {
                "reviewItemId": item.review_item_id,
                "kind": item.kind,
                "subjectId": item.subject_id,
                "payload": item.payload,
                "reviewState": item.review_state,
            }
            for item in value.review_items
        ],
    }


def corpus(value: Corpus) -> dict[str, object]:
    return {"corpusId": value.corpus_id, "name": value.name}


def source_receipt(value: SourceReceipt) -> dict[str, object]:
    return {
        "sourceId": value.source_id,
        "sourceVersionId": value.source_version_id,
        "isDuplicate": value.is_duplicate,
    }


def register(value: ControlRegister) -> dict[str, object]:
    return {
        "corpusId": value.corpus_id,
        "version": value.version,
        "records": [
            {
                "recordId": record.record_id,
                "recordType": record.record_type,
                "value": record.value,
                "evidence": [evidence(item) for item in record.evidence],
                "canonicalHash": record.canonical_hash,
            }
            for record in value.records
        ],
    }
