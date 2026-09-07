"""Framework-free wire vocabulary for the provisioning API (#1768)."""

from typing import Final

from pydantic import BaseModel, ConfigDict, Field, field_validator

CONTRACT_VERSION: Final[str] = "1.0.0"
"""Version of the language-neutral provisioning API contract."""


class ActivationRequest(BaseModel):
    """A consumer-bound idempotent activation request."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    activation_id: str = Field(min_length=1, max_length=200)
    consumer_identity: str = Field(min_length=1, max_length=200)

    @field_validator("activation_id", "consumer_identity")
    @classmethod
    def _strip_and_reject_blank(cls, value: str) -> str:
        """Normalize identifiers before their length and identity checks."""
        normalized = value.strip()
        if not normalized:
            raise ValueError("identifier must not be blank")
        return normalized
