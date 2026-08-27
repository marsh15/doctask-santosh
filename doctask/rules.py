from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AppliesTo(StrictModel):
    record_types: list[str] = Field(min_length=1)


class FailureConfig(StrictModel):
    finding_type: str
    requires_human_review: bool = True


class Rule(StrictModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,127}$")
    name: str = Field(min_length=1)
    severity: str
    applies_to: AppliesTo
    required_fields: list[str] = Field(min_length=1)
    on_failure: FailureConfig


class RulePack(StrictModel):
    rules: list[Rule]


def load_rule_pack(path: str | Path | None) -> RulePack:
    if path is None:
        return RulePack(rules=[])
    value = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return RulePack.model_validate(value)
