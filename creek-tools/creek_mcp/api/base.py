"""The shared closed base for published HTTP wire models."""

from pydantic import BaseModel, ConfigDict


class WireModel(BaseModel):
    """Base for every published wire model: closed, no silent passthrough.

    ``extra="forbid"`` across the whole surface is a contract property, not a
    style preference. An open model lets a producer bolt a debug key onto a
    response and a consumer start depending on it, at which point the field
    exists without ever having been negotiated — and on an error path such a
    key is, by construction, derived from material the caller may not be
    entitled to.
    """

    model_config = ConfigDict(extra="forbid")
