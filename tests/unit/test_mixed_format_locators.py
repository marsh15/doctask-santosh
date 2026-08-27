from pathlib import Path

from doctask.ingestion import parse_source

CORPUS = Path(__file__).parents[2] / "fixtures" / "corpus"


def test_mixed_formats_produce_stable_exact_locators() -> None:
    expected = {
        "project-plan.docx": (
            "Milestone M3: Production readiness - due 18 September 2026 - status at risk.",
            {"kind": "DOCX_PARAGRAPH", "paragraphIndex": 2},
        ),
        "weekly-status.pdf": (
            "Risk R7: Vendor delay - status open - severity high.",
            {
                "kind": "PDF_TEXT",
                "pageNumber": 1,
                "excerpt": "Risk R7: Vendor delay - status open - severity high.",
            },
        ),
        "meeting-notes.md": (
            "Decision D8: Retain the phased launch - status accepted.",
            {
                "kind": "MARKDOWN_LINE",
                "headingPath": ["Project Lighthouse meeting notes", "Decisions"],
                "lineStart": 5,
                "lineEnd": 5,
            },
        ),
        "change-request.txt": (
            "Scope change SC1: Add regional rollout - status proposed.",
            {"kind": "TEXT_LINE", "lineStart": 2, "lineEnd": 2},
        ),
    }

    for filename, (quote, locator) in expected.items():
        content = (CORPUS / filename).read_bytes()
        first = parse_source(
            source_id=f"source:{filename}",
            source_version_id=f"version:{filename}",
            filename=filename,
            content=content,
        )
        second = parse_source(
            source_id=f"source:{filename}",
            source_version_id=f"version:{filename}",
            filename=filename,
            content=content,
        )
        selected = next(span for span in first if span.text == quote)

        assert selected.locator == locator
        assert [span.span_id for span in first] == [span.span_id for span in second]
