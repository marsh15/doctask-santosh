import os
from pathlib import Path

import pytest

from doctask.durable import DurableProjectDeliveryService
from doctask.grounding import ClaimCandidate

DATABASE_URL = os.getenv("TEST_DATABASE_URL")
CORPUS = Path(__file__).parents[2] / "fixtures" / "corpus"


@pytest.mark.skipif(DATABASE_URL is None, reason="TEST_DATABASE_URL is not configured")
def test_entity_and_full_text_retrieval_are_ranked_and_corpus_scoped() -> None:
    with DurableProjectDeliveryService.connect(DATABASE_URL or "") as service:
        service.reset_for_test()
        active = service.create_corpus(name="Active")
        other = service.create_corpus(name="Other")
        service.add_source(
            corpus_id=active.corpus_id,
            filename="meeting-notes.md",
            content=(CORPUS / "meeting-notes.md").read_bytes(),
        )
        service.add_source(
            corpus_id=other.corpus_id,
            filename="other.txt",
            content=b"Decision D8: unrelated phased launch - status rejected.\n",
        )

        hits = service.search_evidence(
            corpus_id=active.corpus_id,
            query="D8 phased launch",
            entity_keys=("decision:D8",),
        )

    assert hits
    assert hits[0].span.text.startswith("Decision D8:")
    assert hits[0].exact_rank == 1
    assert hits[0].fts_rank == 1
    if service.retriever.vector_status == "ENABLED":
        assert hits[0].semantic_rank == 1
    else:
        assert hits[0].semantic_rank is None
    assert all(hit.span.source_id != "other" for hit in hits)


@pytest.mark.skipif(DATABASE_URL is None, reason="TEST_DATABASE_URL is not configured")
def test_grounding_rejects_a_real_citation_that_does_not_support_the_value() -> None:
    with DurableProjectDeliveryService.connect(DATABASE_URL or "") as service:
        service.reset_for_test()
        corpus = service.create_corpus(name="Grounding")
        service.add_source(
            corpus_id=corpus.corpus_id,
            filename="project-plan.docx",
            content=(CORPUS / "project-plan.docx").read_bytes(),
        )
        run = service.start_analysis(corpus_id=corpus.corpus_id)
        real_proposal = run.proposals[0]
        unsupported = dict(real_proposal.after)
        unsupported["dueDate"] = "2026-10-01"

        result = service.evidence_validator.validate(
            corpus_id=corpus.corpus_id,
            candidate=ClaimCandidate(
                record_id=real_proposal.record_id,
                record_type=real_proposal.record_type,
                value=unsupported,
                evidence=real_proposal.evidence,
            ),
        )

    assert result.is_valid is False
    assert result.reason_code == "VALUE_NOT_SUPPORTED"


@pytest.mark.skipif(DATABASE_URL is None, reason="TEST_DATABASE_URL is not configured")
def test_grounding_rejects_cross_corpus_evidence() -> None:
    with DurableProjectDeliveryService.connect(DATABASE_URL or "") as service:
        service.reset_for_test()
        first = service.create_corpus(name="First")
        second = service.create_corpus(name="Second")
        service.add_source(
            corpus_id=first.corpus_id,
            filename="project-plan.docx",
            content=(CORPUS / "project-plan.docx").read_bytes(),
        )
        run = service.start_analysis(corpus_id=first.corpus_id)
        proposal = run.proposals[0]

        result = service.evidence_validator.validate(
            corpus_id=second.corpus_id,
            candidate=ClaimCandidate(
                record_id=proposal.record_id,
                record_type=proposal.record_type,
                value=proposal.after,
                evidence=proposal.evidence,
            ),
        )

    assert result.is_valid is False
    assert result.reason_code == "SPAN_NOT_FOUND_OR_CROSS_CORPUS"
