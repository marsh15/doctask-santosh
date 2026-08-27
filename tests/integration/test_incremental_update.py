import os
from pathlib import Path

import pytest

from doctask.application import DecisionInput
from doctask.durable import DurableProjectDeliveryService

DATABASE_URL = os.getenv("TEST_DATABASE_URL")
CORPUS = Path(__file__).parents[2] / "fixtures" / "corpus"
RULES = Path(__file__).parents[2] / "fixtures" / "rules" / "project-controls.yaml"


@pytest.mark.skipif(DATABASE_URL is None, reason="TEST_DATABASE_URL is not configured")
def test_new_source_recomputes_only_affected_entity_and_preserves_other_hashes() -> None:
    with DurableProjectDeliveryService.connect(
        DATABASE_URL or "", rules_path=RULES
    ) as service:
        service.reset_for_test()
        corpus = service.create_corpus(name="Project Lighthouse")
        for filename in (
            "project-plan.docx",
            "weekly-status.pdf",
            "meeting-notes.md",
            "change-request.txt",
        ):
            service.add_source(
                corpus_id=corpus.corpus_id,
                filename=filename,
                content=(CORPUS / filename).read_bytes(),
            )
        baseline = service.start_analysis(corpus_id=corpus.corpus_id)
        service.submit_decisions(
            run_id=baseline.run_id,
            decisions=[
                DecisionInput(item.review_item_id, is_approved=True)
                for item in baseline.review_items
            ],
        )
        before = {
            record.record_id: record.canonical_hash
            for record in service.get_register(corpus_id=corpus.corpus_id).records
        }

        service.add_source(
            corpus_id=corpus.corpus_id,
            filename="risk-resolved.md",
            content=(CORPUS / "risk-resolved.md").read_bytes(),
        )
        incremental = service.start_analysis(corpus_id=corpus.corpus_id)
        assert [proposal.record_id for proposal in incremental.proposals] == ["risk:R7"]
        evaluation = service.get_rule_evaluation(run_id=incremental.run_id)
        assert evaluation["evaluatedRuleCount"] == 0
        service.submit_decisions(
            run_id=incremental.run_id,
            decisions=[DecisionInput(item.review_item_id, is_approved=True) for item in incremental.review_items],
        )
        after = {
            record.record_id: record.canonical_hash
            for record in service.get_register(corpus_id=corpus.corpus_id).records
        }
        timeline = service.get_run_timeline(run_id=incremental.run_id)

    assert timeline[0]["stage"] == "INGEST"
    assert timeline[0]["affectedEntityKeys"] == ["risk:R7"]
    assert set(timeline[0]["skippedEntityKeys"]) == {
        "decision:D8",
        "milestone:M3",
        "scope_change:SC1",
    }
    assert after["risk:R7"] != before["risk:R7"]
    assert after["milestone:M3"] == before["milestone:M3"]
    assert after["decision:D8"] == before["decision:D8"]
    assert after["scope_change:SC1"] == before["scope_change:SC1"]
