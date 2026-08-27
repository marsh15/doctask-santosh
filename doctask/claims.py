import re
from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class ParsedClaim:
    record_id: str
    record_type: str
    value: dict[str, object]


_MILESTONE = re.compile(
    r"^Milestone (?P<key>[A-Za-z0-9_-]+): (?P<name>.+?) - due "
    r"(?P<day>\d{1,2}) (?P<month>[A-Za-z]+) (?P<year>\d{4}) - status "
    r"(?P<status>[A-Za-z ]+)\.$"
)
_RISK = re.compile(
    r"^Risk (?P<key>[A-Za-z0-9_-]+): (?P<name>.+?) - status "
    r"(?P<status>[A-Za-z ]+) - severity (?P<severity>[A-Za-z ]+)\.$"
)
_DECISION = re.compile(
    r"^Decision (?P<key>[A-Za-z0-9_-]+): (?P<name>.+?) - status "
    r"(?P<status>[A-Za-z ]+)\.$"
)
_SCOPE_CHANGE = re.compile(
    r"^Scope change (?P<key>[A-Za-z0-9_-]+): (?P<name>.+?) - status "
    r"(?P<status>[A-Za-z ]+)\.$"
)
_MONTHS = {
    name: index
    for index, name in enumerate(
        (
            "January", "February", "March", "April", "May", "June",
            "July", "August", "September", "October", "November", "December",
        ),
        start=1,
    )
}


def _enum(value: str) -> str:
    return value.strip().upper().replace(" ", "_")


def parse_claim_text(text: str) -> ParsedClaim | None:
    match = _MILESTONE.fullmatch(text)
    if match is not None and match.group("month") in _MONTHS:
        try:
            due = date(
                int(match.group("year")),
                _MONTHS[match.group("month")],
                int(match.group("day")),
            ).isoformat()
        except ValueError:
            return None
        return ParsedClaim(
            record_id=f"milestone:{match.group('key')}",
            record_type="MILESTONE",
            value={
                "name": match.group("name"),
                "dueDate": due,
                "status": _enum(match.group("status")),
            },
        )
    if (match := _RISK.fullmatch(text)) is not None:
        return ParsedClaim(
            record_id=f"risk:{match.group('key')}",
            record_type="RISK",
            value={
                "name": match.group("name"),
                "status": _enum(match.group("status")),
                "severity": _enum(match.group("severity")),
            },
        )
    if (match := _DECISION.fullmatch(text)) is not None:
        return ParsedClaim(
            record_id=f"decision:{match.group('key')}",
            record_type="DECISION",
            value={"name": match.group("name"), "status": _enum(match.group("status"))},
        )
    if (match := _SCOPE_CHANGE.fullmatch(text)) is not None:
        return ParsedClaim(
            record_id=f"scope_change:{match.group('key')}",
            record_type="SCOPE_CHANGE",
            value={"name": match.group("name"), "status": _enum(match.group("status"))},
        )
    return None
