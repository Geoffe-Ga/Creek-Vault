"""Shared closed provenance vocabulary for model-backed wire responses."""

from typing import Final

INFERENCE_SOURCE_PATTERN: Final[str] = (
    "^(?:local|pool:creek|pool:attested:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?|request_key:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)$"
)
