import os
from pathlib import Path

import pytest

from doctask.application import DecisionInput
from doctask.durable import DurableProjectDeliveryService

DATABASE_URL = os.getenv("TEST_DATABASE_URL")
FIXTURE = Path(__file__).parents[2] / "fixtures" / "corpus" / "project-plan.docx"


@pytest.mark.skipif(DATABASE_URL is None, reason="TEST_DATABASE_URL is not configured")
def test_review_survives_process_reconstruction_and_commits_once() -> None:
    with DurableProjectDeliveryService.connect(DATABASE_URL or "") as first_process:
        first_process.reset_for_test()
        corpus = first_process.create_corpus(name="Project Lighthouse")
        first_process.add_source(
            corpus_id=corpus.corpus_id,
            filename="project-plan.docx",
            content=FIXTURE.read_bytes(),
        )
        paused = first_process.start_analysis(corpus_id=corpus.corpus_id)

        assert paused.status == "AWAITING_REVIEW"
        proposal_id = paused.proposals[0].proposal_id
        run_id = paused.run_id

    with DurableProjectDeliveryService.connect(DATABASE_URL or "") as restarted_process:
        restored = restarted_process.get_run(run_id=run_id)
        assert restored.status == "AWAITING_REVIEW"
        assert restored.proposals[0].proposal_id == proposal_id

        completed = restarted_process.submit_decisions(
            run_id=run_id,
            decisions=[DecisionInput(proposal_id=proposal_id, is_approved=True)],
        )
        register = restarted_process.get_register(corpus_id=corpus.corpus_id)

        assert completed.status == "COMPLETED"
        assert register.version == 1
        assert register.records[0].record_id == "milestone:M3"
        checkpoint = restarted_process.checkpoint_state(run_id=run_id)
        assert checkpoint["status"] == "COMPLETED"


@pytest.mark.skipif(DATABASE_URL is None, reason="TEST_DATABASE_URL is not configured")
def test_rebuilds_missing_checkpoint_after_review_preparation_committed() -> None:
    with DurableProjectDeliveryService.connect(DATABASE_URL or "") as first_process:
        first_process.reset_for_test()
        corpus = first_process.create_corpus(name="Project Lighthouse")
        first_process.add_source(
            corpus_id=corpus.corpus_id,
            filename="project-plan.docx",
            content=FIXTURE.read_bytes(),
        )

        run_id = "run:checkpoint-crash-window"
        with first_process._connect() as connection, connection.transaction():
            connection.execute(
                """
                INSERT INTO workflow_runs (id, corpus_id, base_register_version, status)
                VALUES (%s, %s, 0, 'RUNNING')
                """,
                (run_id, corpus.corpus_id),
            )
            connection.execute(
                """
                INSERT INTO workflow_run_source_versions (run_id, source_version_id)
                SELECT %s, sv.id
                FROM source_versions sv
                JOIN sources s ON s.id = sv.source_id
                WHERE s.corpus_id = %s
                """,
                (run_id, corpus.corpus_id),
            )

        # This is the crash window: business rows commit before LangGraph persists the
        # node result or interrupt checkpoint.
        prepared = first_process._prepare_review(
            {
                "run_id": run_id,
                "corpus_id": corpus.corpus_id,
                "base_register_version": 0,
                "status": "RUNNING",
            }
        )
        assert prepared["status"] == "AWAITING_REVIEW"
        assert first_process.checkpoint_state(run_id=run_id) == {}
        with first_process._connect() as connection:
            preparation_counts = {
                "proposals": connection.execute(
                    "SELECT count(*) AS value FROM proposed_mutations WHERE run_id = %s",
                    (run_id,),
                ).fetchone()["value"],
                "review_items": connection.execute(
                    "SELECT count(*) AS value FROM review_items WHERE run_id = %s",
                    (run_id,),
                ).fetchone()["value"],
                "rule_evaluations": connection.execute(
                    "SELECT count(*) AS value FROM rule_evaluations WHERE run_id = %s",
                    (run_id,),
                ).fetchone()["value"],
                "preparation_stages": connection.execute(
                    """
                    SELECT count(*) AS value FROM stage_executions
                    WHERE run_id = %s AND stage <> 'COMMIT'
                    """,
                    (run_id,),
                ).fetchone()["value"],
            }

    with DurableProjectDeliveryService.connect(DATABASE_URL or "") as restarted_process:
        with pytest.raises(ValueError, match="Human decisions are required"):
            restarted_process.resume_run(run_id=run_id)

        recovered = restarted_process.get_run(run_id=run_id)
        assert recovered.status == "AWAITING_REVIEW"
        assert len(recovered.proposals) == 1

        completed = restarted_process.submit_decisions(
            run_id=run_id,
            decisions=[
                DecisionInput(
                    proposal_id=recovered.proposals[0].proposal_id,
                    is_approved=True,
                )
            ],
        )
        register = restarted_process.get_register(corpus_id=corpus.corpus_id)

        assert completed.status == "COMPLETED"
        assert register.version == 1
        assert len(register.records) == 1
        assert restarted_process.checkpoint_state(run_id=run_id)["status"] == "COMPLETED"
        with restarted_process._connect() as connection:
            assert connection.execute(
                "SELECT count(*) AS value FROM proposed_mutations WHERE run_id = %s",
                (run_id,),
            ).fetchone()["value"] == preparation_counts["proposals"]
            assert connection.execute(
                "SELECT count(*) AS value FROM review_items WHERE run_id = %s",
                (run_id,),
            ).fetchone()["value"] == preparation_counts["review_items"]
            assert connection.execute(
                "SELECT count(*) AS value FROM rule_evaluations WHERE run_id = %s",
                (run_id,),
            ).fetchone()["value"] == preparation_counts["rule_evaluations"]
            assert connection.execute(
                """
                SELECT count(*) AS value FROM stage_executions
                WHERE run_id = %s AND stage <> 'COMMIT'
                """,
                (run_id,),
            ).fetchone()["value"] == preparation_counts["preparation_stages"]
