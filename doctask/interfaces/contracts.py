from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class CreateCorpusRequest(Contract):
    name: str = Field(min_length=1, max_length=200)


class DecisionRequest(Contract):
    review_item_id: str = Field(
        validation_alias=AliasChoices("reviewItemId", "proposalId"), min_length=1
    )
    is_approved: bool = Field(alias="isApproved")
    reason: str | None = Field(default=None, max_length=1000)
    resolution: dict[str, object] | None = None


class SubmitDecisionsRequest(Contract):
    decisions: list[DecisionRequest] = Field(min_length=1)

    @field_validator("decisions")
    @classmethod
    def review_item_ids_are_unique(
        cls, decisions: list[DecisionRequest]
    ) -> list[DecisionRequest]:
        ids = [decision.review_item_id for decision in decisions]
        if len(ids) != len(set(ids)):
            raise ValueError("reviewItemId values must be unique")
        return decisions
