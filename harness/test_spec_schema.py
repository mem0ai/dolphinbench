"""Schema for the public DolphinBench test format."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, field_validator


class GradeSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: StrictStr
    config: dict[str, Any]


class TestSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: StrictStr | StrictInt
    narrative_anchor_date: StrictStr
    test: StrictStr
    load_bearing_facts: list[StrictInt] = Field(min_length=1)
    expected_tool_calls: list[StrictStr] = Field(min_length=1)
    grade: GradeSpec
    mock_state: dict[str, Any]

    @field_validator("test")
    @classmethod
    def _query_is_not_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("test query must not be empty")
        return value

    @field_validator("load_bearing_facts")
    @classmethod
    def _fact_ids_are_unique(cls, value: list[int]) -> list[int]:
        if len(value) != len(set(value)):
            raise ValueError("load_bearing_facts contains duplicate fact ids")
        if any(fact_id <= 0 for fact_id in value):
            raise ValueError("fact ids must be positive integers")
        return value
