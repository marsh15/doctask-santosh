import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from doctask.application import DecisionInput
from doctask.durable import DurableProjectDeliveryService

DATABASE_URL = os.getenv("TEST_DATABASE_URL")
CORPUS = Path(__file__).parents[2] / "fixtures" / "corpus"


def _approve_all(service: DurableProjectDeliveryService, run_id: str) -> None:
    run = service.get_run(run_id=run_id)
    service.submit_decisions(
        run_id=run_id,
        decisions=[
            DecisionInput(proposal.proposal_id, is_approved=True)
            for proposal in run.proposals
        ],
    )


@pytest.mark.skipif(DATABASE_URL is None, reason="TEST_DATABASE_URL is not configured")
def test_duplicate_source_is_an_explicit_noop_without_a_register_version_bump() -> None:
    content = (CORPUS / "project-plan.docx").read_bytes()
    with DurableProjectDeliveryService.connect(DATABASE_URL or "") as service:
        service.reset_for_test()
        corpus = service.create_corpus(name="No-op proof")
        service.add_source(
            corpus_id=corpus.corpus_id,
            filename="project-plan.docx",
            content=content,
        )
        baseline = service.start_analysis(corpus_id=corpus.corpus_id)
        _approve_all(service, baseline.run_id)
        before = service.get_register(corpus_id=corpus.corpus_id)

        duplicate = service.add_source(
            corpus_id=corpus.corpus_id,
            filename="renamed-copy.docx",
            content=content,
        )
        noop = service.start_analysis(corpus_id=corpus.corpus_id)
        after = service.get_register(corpus_id=corpus.corpus_id)
        evaluation = service.get_rule_evaluation(run_id=noop.run_id)
        timeline = service.get_run_timeline(run_id=noop.run_id)

    assert duplicate.is_duplicate is True
    assert noop.status == "COMPLETED"
    assert noop.proposals == ()
    assert after.version == before.version
    assert after.records == before.records
    assert evaluation == {
        "evaluatedRuleCount": 0,
        "findingCount": 0,
        "findings": [],
    }
    assert timeline[0]["affectedEntityKeys"] == []
    assert timeline[0]["skippedEntityKeys"] == ["milestone:M3"]


@pytest.mark.skipif(DATABASE_URL is None, reason="TEST_DATABASE_URL is not configured")
def test_two_corpora_can_run_concurrently_without_evidence_leakage() -> None:
    with DurableProjectDeliveryService.connect(DATABASE_URL or "") as service:
        service.reset_for_test()

    def build_register(name: str, filename: str) -> tuple[str, set[str]]:
        with DurableProjectDeliveryService.connect(DATABASE_URL or "") as service:
            corpus = service.create_corpus(name=name)
            service.add_source(
                corpus_id=corpus.corpus_id,
                filename=filename,
                content=(CORPUS / filename).read_bytes(),
            )
            run = service.start_analysis(corpus_id=corpus.corpus_id)
            _approve_all(service, run.run_id)
            register = service.get_register(corpus_id=corpus.corpus_id)
            return corpus.corpus_id, {record.record_id for record in register.records}

    with ThreadPoolExecutor(max_workers=2) as executor:
        milestone_future = executor.submit(
            build_register, "Milestone corpus", "project-plan.docx"
        )
        risk_future = executor.submit(
            build_register, "Risk corpus", "weekly-status.pdf"
        )
        milestone_corpus, milestone_records = milestone_future.result()
        risk_corpus, risk_records = risk_future.result()

    assert milestone_corpus != risk_corpus
    assert milestone_records == {"milestone:M3"}
    assert risk_records == {"risk:R7"}


@pytest.mark.skipif(DATABASE_URL is None, reason="TEST_DATABASE_URL is not configured")
def test_same_source_bytes_can_be_ingested_independently_by_two_corpora() -> None:
    content = (CORPUS / "project-plan.docx").read_bytes()
    with DurableProjectDeliveryService.connect(DATABASE_URL or "") as service:
        service.reset_for_test()
        first = service.create_corpus(name="First")
        second = service.create_corpus(name="Second")

        first_receipt = service.add_source(
            corpus_id=first.corpus_id, filename="plan.docx", content=content
        )
        second_receipt = service.add_source(
            corpus_id=second.corpus_id, filename="plan.docx", content=content
        )

    assert first_receipt.is_duplicate is False
    assert second_receipt.is_duplicate is False
    assert first_receipt.source_id != second_receipt.source_id
    assert first_receipt.source_version_id != second_receipt.source_version_id


@pytest.mark.skipif(DATABASE_URL is None, reason="TEST_DATABASE_URL is not configured")
def test_same_corpus_rejects_a_second_active_run() -> None:
    with DurableProjectDeliveryService.connect(DATABASE_URL or "") as service:
        service.reset_for_test()
        corpus = service.create_corpus(name="Collision proof")
        service.add_source(
            corpus_id=corpus.corpus_id,
            filename="project-plan.docx",
            content=(CORPUS / "project-plan.docx").read_bytes(),
        )
        first = service.start_analysis(corpus_id=corpus.corpus_id)
        with pytest.raises(ValueError, match="already has an active Workflow Run"):
            service.start_analysis(corpus_id=corpus.corpus_id)

        _approve_all(service, first.run_id)
        register = service.get_register(corpus_id=corpus.corpus_id)

    assert register.version == 1
    assert {record.record_id for record in register.records} == {"milestone:M3"}
