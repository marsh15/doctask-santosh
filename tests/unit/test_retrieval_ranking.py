from doctask.retrieval import deterministic_embedding, reciprocal_rank_fusion


def test_rank_fusion_is_bounded_deterministic_and_prioritizes_exact_matches() -> None:
    ranked = reciprocal_rank_fusion(
        exact=["span:exact", "span:both"],
        full_text=["span:both", "span:fts"],
        semantic=["span:semantic"],
        limit=3,
    )

    assert [span_id for span_id, _ in ranked] == [
        "span:both",
        "span:exact",
        "span:fts",
    ]
    assert len(ranked) == 3


def test_rank_fusion_breaks_equal_scores_by_stable_span_id() -> None:
    ranked = reciprocal_rank_fusion(
        exact=[],
        full_text=[],
        semantic=["span:b", "span:a"],
        limit=2,
    )

    assert [span_id for span_id, _ in ranked] == ["span:b", "span:a"]


def test_deterministic_embedding_is_stable_and_normalized() -> None:
    first = deterministic_embedding("milestone launch September")
    assert first == deterministic_embedding("milestone launch September")
    assert abs(sum(value * value for value in first) - 1.0) < 1e-6
