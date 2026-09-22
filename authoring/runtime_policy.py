"""Explicit, hash-bound settings for staged test creation, not final evaluation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr

from construction.llm import AzureJsonClient


class CreationPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1]
    workflow: Literal["staged", "production"] = "staged"
    grading_only_recovery: StrictBool = False
    prepare_evidence: StrictBool = True
    max_model_corrections: StrictInt = Field(default=3, ge=1, le=3)
    technical_retries: StrictInt = Field(default=1, ge=0, le=2)
    preparation_workers: StrictInt = Field(default=4, ge=1, le=12)
    writing_workers: StrictInt = Field(default=8, ge=1, le=32)
    preflight_workers: StrictInt = Field(default=8, ge=1, le=32)
    certification_workers: StrictInt = Field(default=8, ge=1, le=32)
    review_workers: StrictInt = Field(default=4, ge=1, le=16)
    model_timeout_seconds: StrictInt = Field(default=180, ge=30, le=600)
    model_output_tokens: StrictInt = Field(default=16384, ge=2048, le=32768)
    writer_reasoning: Literal["medium", "high"] = "high"
    reviewer_reasoning: Literal["medium", "high"] = "medium"
    oracle_model: StrictStr = "gpt-5.6-sol"
    oracle_reasoning: Literal["medium", "high"] = "medium"
    oracle_timeout_seconds: StrictInt = Field(default=120, ge=30, le=300)
    baseline_probe_ids: list[StrictInt] = Field(default_factory=list)
    action_batched_judge: StrictBool = False
    provider_concurrency: StrictInt = Field(default=24, ge=1, le=128)
    provider_tokens_per_minute: StrictInt = Field(default=300000, ge=10000)
    provider_requests_per_minute: StrictInt = Field(default=60, ge=1)
    provider_header_feedback: StrictBool = False
    provider_capacity_directory: StrictStr = "tmp/authoring/staged_provider_capacity"
    evidence_cache_directory: StrictStr = "tmp/authoring/staged_evidence_cache_v1"

    @classmethod
    def load(cls, path: Path) -> "CreationPolicy":
        return cls.model_validate(json.loads(path.read_text()))

    def client(self, model: str, *, writing: bool = False) -> AzureJsonClient:
        return AzureJsonClient(
            model=model,
            reasoning_effort=self.writer_reasoning if writing else self.reviewer_reasoning,
            timeout=self.model_timeout_seconds, max_retries=0,
            max_completion_tokens=self.model_output_tokens,
        )

    def oracle_settings(self) -> dict:
        return {"model": self.oracle_model, "reasoning_effort": self.oracle_reasoning,
                "timeout": self.oracle_timeout_seconds, "max_retries": 0, "api": "responses"}


def is_technical_error(exc: Exception) -> bool:
    import urllib.error
    from openai import APIConnectionError, APIStatusError
    return (isinstance(exc, (APIConnectionError, TimeoutError, ConnectionError, urllib.error.URLError))
            or isinstance(exc, APIStatusError) and exc.status_code in {408, 409, 429, 500, 502, 503, 504})
