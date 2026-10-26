"""Closed, bounded wire models for evidence-backed course recommendations."""

from __future__ import annotations

from typing import Literal

from pydantic import ConfigDict, Field, model_validator

from creek_mcp.api.base import WireModel
from creek_mcp.api.inference import INFERENCE_SOURCE_PATTERN


class _ClosedModel(WireModel):
    """Unknown fields never become prompt instructions or response extensions."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ReadingCandidate(_ClosedModel):
    """One available chapter identified by the application's stable content ID."""

    content_id: int = Field(gt=0, strict=True)
    title: str = Field(min_length=1, max_length=240)
    content: str = Field(min_length=1, max_length=1500)


class ReadingSource(_ClosedModel):
    """Current caller-owned writing, structurally excluding the intimate tier."""

    entry_id: int = Field(gt=0, strict=True)
    tier: Literal["open", "personal"]
    content: str = Field(min_length=1, max_length=2000)


class ReadingRecommendationRequest(_ClosedModel):
    """A small reading shortlist and the real writing a recommendation may cite."""

    catalog_version: str = Field(min_length=1, max_length=128)
    readings: list[ReadingCandidate] = Field(min_length=1, max_length=6)
    sources: list[ReadingSource] = Field(min_length=1, max_length=3)

    @model_validator(mode="after")
    def bounded_context(self) -> ReadingRecommendationRequest:
        """Refuse ambiguous IDs and oversized prompts before a model is constructed."""
        reading_ids = {reading.content_id for reading in self.readings}
        source_ids = {source.entry_id for source in self.sources}
        if len(reading_ids) != len(self.readings) or len(source_ids) != len(
            self.sources
        ):
            raise ValueError("duplicate recommendation coordinates")
        size = sum(len(reading.content) for reading in self.readings)
        size += sum(len(source.content) for source in self.sources)
        if size > 12_000:
            raise ValueError("recommendation context exceeds its bound")
        return self


class ReadingRecommendation(_ClosedModel):
    """A model choice accepted only after its IDs and quotation are verified."""

    content_id: int = Field(gt=0, strict=True)
    entry_id: int = Field(gt=0, strict=True)
    quote: str = Field(min_length=1, max_length=280)
    reason: str = Field(min_length=1, max_length=400)
    benefit: str = Field(min_length=1, max_length=400)


class ReadingRecommendationResponse(_ClosedModel):
    """A validated recommendation or an honest absence of supporting evidence."""

    inference_source: str | None = Field(
        default=None,
        pattern=INFERENCE_SOURCE_PATTERN,
    )
    status: Literal["ok", "empty"]
    recommendation: ReadingRecommendation | None = None
