from pathlib import Path

from doctask.application import DecisionInput, OfflineMilestoneSlice

FIXTURE = Path(__file__).parents[2] / "fixtures" / "corpus" / "project-plan.docx"


def test_one_docx_becomes_one_cited_human_approved_milestone() -> None:
    service = OfflineMilestoneSlice.for_offline_testing()
    corpus = service.create_corpus(name="Project Lighthouse")
    source = service.add_source(
        corpus_id=corpus.corpus_id,
        filename="project-plan.docx",
        content=FIXTURE.read_bytes(),
    )

    run = service.start_analysis(corpus_id=corpus.corpus_id)

    assert source.is_duplicate is False
    assert run.status == "AWAITING_REVIEW"
    assert len(run.proposals) == 1
    proposal = run.proposals[0]
    assert proposal.record_id == "milestone:M3"
    assert proposal.after["name"] == "Production readiness"
    assert proposal.after["dueDate"] == "2026-09-18"
    assert proposal.after["status"] == "AT_RISK"
    assert proposal.evidence[0].quote == (
        "Milestone M3: Production readiness - due 18 September 2026 - status at risk."
    )
    assert proposal.evidence[0].locator == {
        "kind": "DOCX_PARAGRAPH",
        "paragraphIndex": 2,
    }

    completed = service.submit_decisions(
        run_id=run.run_id,
        decisions=[DecisionInput(proposal_id=proposal.proposal_id, is_approved=True)],
    )
    register = service.get_register(corpus_id=corpus.corpus_id)

    assert completed.status == "COMPLETED"
    assert register.version == 1
    assert [record.record_id for record in register.records] == ["milestone:M3"]
    assert register.records[0].evidence[0].span_id == proposal.evidence[0].span_id
