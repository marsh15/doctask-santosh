import os
from pathlib import Path

import pytest

from doctask.application import DecisionInput
from doctask.durable import DurableProjectDeliveryService

DATABASE_URL = os.getenv("TEST_DATABASE_URL")
CORPUS = Path(__file__).parents[2] / "fixtures" / "corpus"
RULES = Path(__file__).parents[2] / "fixtures" / "rules" / "project-controls.yaml"


@pytest.mark.skipif(DATABASE_URL is None, reason="TEST_DATABASE_URL is not configured")
def test_conflict_rule_and_partial_item_decisions_remain_independent() -> None:
    with DurableProjectDeliveryService.connect(
        DATABASE_URL or "", rules_path=RULES
    ) as service:
        service.reset_for_test()
        corpus = service.create_corpus(name="Project Lighthouse")
        for filename in ("project-plan.docx", "weekly-status.pdf"):
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

        service.add_source(
            corpus_id=corpus.corpus_id,
            filename="contradictory-update.md",
            content=(CORPUS / "contradictory-update.md").read_bytes(),
        )
        update = service.start_analysis(corpus_id=corpus.corpus_id)
        conflicts = service.list_conflicts(corpus_id=corpus.corpus_id)
        evaluation = service.get_rule_evaluation(run_id=update.run_id)

        assert conflicts[0]["recordId"] == "milestone:M3"
        assert conflicts[0]["field"] == "dueDate"
        assert {claim["value"]["dueDate"] for claim in conflicts[0]["claims"]} == {
            "2026-09-18",
            "2026-09-24",
        }
        assert evaluation["evaluatedRuleCount"] == 1
        assert evaluation["findingCount"] == 1
        assert evaluation["findings"][0]["ruleId"] == "active-milestone-owner-required"
        assert evaluation["findings"][0]["status"] == "INSUFFICIENT_EVIDENCE"

        service.submit_decisions(
            run_id=update.run_id,
            decisions=[
                DecisionInput(item.review_item_id, is_approved=item.kind == "PROPOSED_MUTATION")
                for item in update.review_items
            ],
        )
        register = service.get_register(corpus_id=corpus.corpus_id)

    records = {record.record_id: record for record in register.records}
    assert register.version == 2
    assert records["milestone:M3"].value["dueDate"] == "2026-09-18"
    assert records["risk:R7"].value["status"] == "RESOLVED"
