import os
from pathlib import Path

import pytest

from doctask.application import DecisionInput
from doctask.durable import DurableProjectDeliveryService
from doctask.watcher import process_stable_files

DATABASE_URL = os.getenv("TEST_DATABASE_URL")
RULES = Path(__file__).parents[2] / "fixtures" / "rules" / "project-controls.yaml"


@pytest.mark.skipif(DATABASE_URL is None, reason="TEST_DATABASE_URL is not configured")
def test_initial_competing_claims_require_explicit_resolution() -> None:
    with DurableProjectDeliveryService.connect(DATABASE_URL or "") as service:
        service.reset_for_test()
        corpus = service.create_corpus(name="Initial conflict")
        service.add_source(corpus_id=corpus.corpus_id, filename="one.txt", content=b"Milestone M3: Launch - due 18 September 2026 - status Active.")
        service.add_source(corpus_id=corpus.corpus_id, filename="two.txt", content=b"Milestone M3: Launch - due 24 September 2026 - status Active.")
        run = service.start_analysis(corpus_id=corpus.corpus_id)
        assert run.proposals == ()
        assert [item.kind for item in run.review_items] == ["CONFLICT"]
        claims = run.review_items[0].payload["claims"]
        selected = next(claim["value"] for claim in claims if claim["value"]["dueDate"] == "2026-09-24")
        service.submit_decisions(run_id=run.run_id, decisions=[DecisionInput(run.review_items[0].review_item_id, True, resolution=selected)])
        register = service.get_register(corpus_id=corpus.corpus_id)
    assert register.version == 1
    assert register.records[0].value["dueDate"] == "2026-09-24"


@pytest.mark.skipif(DATABASE_URL is None, reason="TEST_DATABASE_URL is not configured")
def test_proposal_and_finding_are_independent_with_visible_timeline() -> None:
    with DurableProjectDeliveryService.connect(DATABASE_URL or "", rules_path=RULES) as service:
        service.reset_for_test()
        corpus = service.create_corpus(name="Governed items")
        service.add_source(corpus_id=corpus.corpus_id, filename="milestone.txt", content=b"Milestone M3: Launch - due 18 September 2026 - status Active.")
        run = service.start_analysis(corpus_id=corpus.corpus_id)
        assert {item.kind for item in run.review_items} == {"PROPOSED_MUTATION", "RULE_FINDING"}
        service.submit_decisions(run_id=run.run_id, decisions=[DecisionInput(item.review_item_id, item.kind == "PROPOSED_MUTATION") for item in run.review_items])
        restored = service.get_run(run_id=run.run_id)
        timeline = service.get_run_timeline(run_id=run.run_id)
    assert {item.review_state for item in restored.review_items} == {"APPROVED", "REJECTED"}
    assert [stage["stage"] for stage in timeline] == ["INGEST", "RETRIEVE_OR_PLAN", "CONFLICTS", "RULES", "HUMAN_REVIEW", "COMMIT"]


@pytest.mark.skipif(DATABASE_URL is None, reason="TEST_DATABASE_URL is not configured")
def test_watcher_batches_stable_files_into_one_idempotent_run(tmp_path: Path) -> None:
    (tmp_path / "one.txt").write_text("Risk R7: Vendor delay - status Open - severity High.")
    (tmp_path / "two.txt").write_text("Decision D8: Ship beta - status Approved.")
    with DurableProjectDeliveryService.connect(DATABASE_URL or "") as service:
        service.reset_for_test()
        corpus = service.create_corpus(name="Watched")
        results = process_stable_files(service, watcher_id="fixture", corpus_id=corpus.corpus_id, directory=tmp_path, stable_for_seconds=0)
        assert len({result["runId"] for result in results}) == 1
        run = service.get_run(run_id=str(results[0]["runId"]))
        service.submit_decisions(run_id=run.run_id, decisions=[DecisionInput(item.review_item_id, True) for item in run.review_items])
        repeated = process_stable_files(service, watcher_id="fixture", corpus_id=corpus.corpus_id, directory=tmp_path, stable_for_seconds=0)
    assert len(repeated) == 2
    assert all(result["isDuplicate"] for result in repeated)
