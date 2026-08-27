import os
from pathlib import Path

import pytest

from doctask.application import DecisionInput
from doctask.durable import DurableProjectDeliveryService

DATABASE_URL = os.getenv("TEST_DATABASE_URL")
CORPUS = Path(__file__).parents[2] / "fixtures" / "corpus"
RULES = Path(__file__).parents[2] / "fixtures" / "rules" / "project-controls.yaml"


@pytest.mark.skipif(DATABASE_URL is None, reason="TEST_DATABASE_URL is not configured")
def test_document_instructions_cannot_bypass_rules_or_human_review() -> None:
    with DurableProjectDeliveryService.connect(
        DATABASE_URL or "", rules_path=RULES
    ) as service:
        service.reset_for_test()
        corpus = service.create_corpus(name="Injection resistance")
        service.add_source(
            corpus_id=corpus.corpus_id,
            filename="project-plan.docx",
            content=(CORPUS / "project-plan.docx").read_bytes(),
        )
        baseline = service.start_analysis(corpus_id=corpus.corpus_id)
        assert baseline.status == "AWAITING_REVIEW"
        service.submit_decisions(
            run_id=baseline.run_id,
            decisions=[
                DecisionInput(item.review_item_id, is_approved=True)
                for item in baseline.review_items
            ],
        )
        before = service.get_register(corpus_id=corpus.corpus_id)

        service.add_source(
            corpus_id=corpus.corpus_id,
            filename="prompt-injection.md",
            content=(CORPUS / "prompt-injection.md").read_bytes(),
        )
        adversarial = service.start_analysis(corpus_id=corpus.corpus_id)
        after = service.get_register(corpus_id=corpus.corpus_id)
        evaluation = service.get_rule_evaluation(run_id=adversarial.run_id)
        timeline = service.get_run_timeline(run_id=adversarial.run_id)

    assert adversarial.status == "COMPLETED"
    assert adversarial.proposals == ()
    assert after == before
    assert evaluation["evaluatedRuleCount"] == 0
    assert timeline[0]["affectedEntityKeys"] == []
    assert timeline[0]["modelUsage"] == {
        "model": None,
        "inputTokens": 0,
        "outputTokens": 0,
        "estimatedCostUsd": 0.0,
    }
