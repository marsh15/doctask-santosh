from doctask.claims import parse_claim_text


def test_syntactically_matching_invalid_date_is_not_a_claim() -> None:
    assert parse_claim_text(
        "Milestone M3: Impossible - due 31 February 2026 - status Active."
    ) is None
