import os
from pathlib import Path

import pytest

from doctask.durable import DurableProjectDeliveryService

DATABASE_URL = os.getenv("TEST_DATABASE_URL")
CORPUS = Path(__file__).parents[2] / "fixtures" / "corpus"


@pytest.mark.skipif(DATABASE_URL is None, reason="TEST_DATABASE_URL is not configured")
def test_mixed_corpus_is_deduplicated_and_produces_cited_typed_proposals() -> None:
    filenames = [
        "project-plan.docx",
        "weekly-status.pdf",
        "meeting-notes.md",
        "change-request.txt",
    ]
    with DurableProjectDeliveryService.connect(DATABASE_URL or "") as service:
        service.reset_for_test()
        corpus = service.create_corpus(name="Project Lighthouse")
        receipts = [
            service.add_source(
                corpus_id=corpus.corpus_id,
                filename=filename,
                content=(CORPUS / filename).read_bytes(),
            )
            for filename in filenames
        ]
        duplicate = service.add_source(
            corpus_id=corpus.corpus_id,
            filename="project-plan-copy.docx",
            content=(CORPUS / "project-plan.docx").read_bytes(),
        )
        run = service.start_analysis(corpus_id=corpus.corpus_id)

    assert duplicate.is_duplicate is True
    assert duplicate.source_id == receipts[0].source_id
    proposals = {proposal.record_id: proposal for proposal in run.proposals}
    assert set(proposals) == {
        "milestone:M3",
        "risk:R7",
        "decision:D8",
        "scope_change:SC1",
    }
    assert proposals["risk:R7"].evidence[0].locator["pageNumber"] == 1
    assert proposals["decision:D8"].evidence[0].locator["headingPath"] == [
        "Project Lighthouse meeting notes",
        "Decisions",
    ]
    assert proposals["scope_change:SC1"].evidence[0].locator["lineStart"] == 2
