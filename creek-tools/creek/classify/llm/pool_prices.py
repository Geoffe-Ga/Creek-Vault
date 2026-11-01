"""The pinned, vendor-neutral pool price table (#2007, R4-1, R4-2).

Owner decision 2026-11-01 on #2007: pool spend is metered *locally* — the
tokens Creek itself sends, priced at the vendor's documented rate — rather
than read from a vendor usage API. This module owns the "documented rate"
half, and it holds **no price of its own**. Every figure lives in
:data:`PRICE_TABLE_PATH`, a JSON file shipped beside this module in the image,
so a price is configuration that is reviewed and pinned like any other image
content, never a constant in code and never anything the writable vault config
can move.

**Vendor-neutral by construction.** The table is keyed by vendor id, then by
model id. Switching the pool to another vendor is a new entry in the file —
its own currency, monthly key cap, alert fraction and per-model prices — and
no code change. Nothing here names a vendor.

**Nothing is invented.** An entry is either ``pinned`` — and then it must
carry the ``https`` URL the prices were read from, the date they were read
and at least one priced model — or ``operator_must_fill``, which carries no
model at all. The shipped Privatemode entry is the latter, because this
change was written without access to the vendor's published prices; until an
operator fills it, the meter reports tokens and says ``price-unpinned``
instead of a euro figure (:mod:`creek.classify.llm.pool_meter`).

**Priced per model, never all or nothing (#2052).** A pinned entry prices
each counted model it can and names the ones it cannot, in a
:class:`VendorCost`. One unexpected model id therefore leaves the rest of the
month's spend measurable as a lower bound, instead of hiding it.

**Money is exact.** JSON numbers are parsed straight into :class:`Decimal`,
as ADR-0013 Decision 4 requires of every operator amount, so a price written
as ``0.1`` is exactly one tenth.

**Cached input.** ``cached_prompt_per_million`` is recorded so an entry can
state everything the vendor publishes, but the meter prices every prompt
token at the full prompt rate. The vault sends no cache field (ADR-0016
Decision 1 item 6), and any discount is the vendor's to give: pricing it at
the full rate can only over-count, which is the direction a brake must err
in.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

PRICE_TABLE_PATH: Final[Path] = Path(__file__).with_name("pool_prices.json")
"""The pinned table shipped in the image; the operator fills it by a reviewed PR."""

DEFAULT_POOL_MONTHLY_CAP: Final[Decimal] = Decimal("10.00")
"""The per-key monthly cost cap when an entry names none (owner decision R4-1)."""

DEFAULT_POOL_ALERT_FRACTION: Final[Decimal] = Decimal("0.5")
"""The fraction of the cap the spend alert fires at (owner decision R4-2)."""

PINNED: Final[str] = "pinned"
"""Entry status: prices read from a cited source on a recorded date."""

OPERATOR_MUST_FILL: Final[str] = "operator_must_fill"
"""Entry status: no price is known yet, so none may be reported."""

VENDOR_ID_PATTERN: Final[str] = r"^[a-z0-9][a-z0-9-]{0,31}$"
"""A vendor id: closed, so no free text can hide in a table key or a ledger."""

MODEL_ID_PATTERN: Final[str] = r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$"
"""A model id as a vendor's catalogue names it; closed for the same reason."""

_SOURCE_URL_PATTERN: Final[str] = r"^https://[^\s]{1,500}$"
_TOKENS_PER_MILLION: Final[Decimal] = Decimal(1_000_000)
_NOT_FOUND: Final[str] = "price table not found"
_NOT_JSON: Final[str] = "price table is invalid: not JSON"
_VALUE_ERROR: Final[str] = "value_error"

VendorId = Annotated[str, Field(pattern=VENDOR_ID_PATTERN)]
ModelId = Annotated[str, Field(pattern=MODEL_ID_PATTERN)]
Price = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]
PositiveAmount = Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]
Fraction = Annotated[Decimal, Field(gt=0, le=1, allow_inf_nan=False)]


class PriceTableError(ValueError):
    """The price table is missing or malformed; the message names fields only."""


class ModelPrices(BaseModel):
    """One model's prices, per million tokens, in its vendor entry's currency.

    A direction a model is never billed for may be left out: a chat model has
    no embedding price and an embedding model no completion price. A count in
    a direction that has no price makes this model unpriced (:meth:`cost`),
    never priced at zero.

    Attributes:
        prompt_per_million: Price per million prompt (input) tokens.
        completion_per_million: Price per million completion (output) tokens.
        embedding_per_million: Price per million embedded tokens.
        cached_prompt_per_million: The vendor's cached-input price, if it
            publishes one. Recorded, deliberately not applied (module
            docstring).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    prompt_per_million: Price | None = None
    completion_per_million: Price | None = None
    embedding_per_million: Price | None = None
    cached_prompt_per_million: Price | None = None

    def cost(self, prompt: int, completion: int, embedding: int) -> Decimal | None:
        """Return the exact cost of the three token counts, or ``None``.

        Args:
            prompt: Prompt tokens.
            completion: Completion tokens.
            embedding: Embedded tokens.

        Returns:
            The unrounded cost, or ``None`` when a non-zero count has no price.
        """
        total = Decimal(0)
        for tokens, rate in (
            (prompt, self.prompt_per_million),
            (completion, self.completion_per_million),
            (embedding, self.embedding_per_million),
        ):
            if not tokens:
                continue
            if rate is None:
                return None
            total += Decimal(tokens) * rate / _TOKENS_PER_MILLION
        return total


@dataclass(frozen=True, slots=True)
class VendorCost:
    """What a pinned entry could price of one month's per-model counts.

    Attributes:
        priced: The exact, unrounded cost of the models that are priced.
        priced_models: How many counted models that cost covers.
        unpriced: The sorted ids of counted models with no usable price. Any
            of them makes :attr:`priced` a lower bound on the true spend.
    """

    priced: Decimal
    priced_models: int
    unpriced: tuple[str, ...]


class VendorPrices(BaseModel):
    """One vendor's pinned prices, key cap and alert line.

    Attributes:
        status: :data:`PINNED` or :data:`OPERATOR_MUST_FILL`.
        currency: The currency every amount in this entry is quoted in.
        source_url: The ``https`` page the prices were read from.
        read_on: The date the operator read that page.
        monthly_cap: The vendor key's monthly cost cap.
        alert_fraction: The fraction of the cap the spend alert fires at.
        models: Prices per model id.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["pinned", "operator_must_fill"]
    currency: Literal["EUR", "USD"]
    source_url: Annotated[str, Field(pattern=_SOURCE_URL_PATTERN)] | None = None
    read_on: date | None = None
    monthly_cap: PositiveAmount = DEFAULT_POOL_MONTHLY_CAP
    alert_fraction: Fraction = DEFAULT_POOL_ALERT_FRACTION
    models: dict[ModelId, ModelPrices] = {}  # pydantic copies a mutable default

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        """Refuse a pinned entry missing its citation, or a filled unpinned one."""
        if self.status == OPERATOR_MUST_FILL:
            if self.models:
                msg = "models must be empty while status is operator_must_fill"
                raise ValueError(msg)
            return self
        missing = [
            name
            for name, value in (
                ("source_url", self.source_url),
                ("read_on", self.read_on),
                ("models", self.models or None),
            )
            if value is None
        ]
        if missing:
            msg = f"a pinned entry requires {', '.join(missing)}"
            raise ValueError(msg)
        return self

    @property
    def pinned(self) -> bool:
        """Whether this entry's prices may be reported."""
        return self.status == PINNED

    @property
    def alert_threshold(self) -> Decimal:
        """The spend at or above which the alert fires: cap x fraction."""
        return self.monthly_cap * self.alert_fraction

    def price(self, counts: Mapping[str, tuple[int, int, int]]) -> VendorCost | None:
        """Price per-model ``(prompt, completion, embedding)`` counts.

        A model is unpriced when the entry lists no price for it, or lists none
        for a direction it was counted in. It adds nothing to the cost and is
        named instead, so the cost of the others survives as a lower bound.

        Args:
            counts: Token counts keyed by model id.

        Returns:
            The priced cost and the unpriced model ids, or ``None`` when this
            entry is not pinned.
        """
        if not self.pinned:
            return None
        total = Decimal(0)
        unpriced: list[str] = []
        for model, (prompt, completion, embedding) in counts.items():
            prices = self.models.get(model)
            cost = (
                None if prices is None else prices.cost(prompt, completion, embedding)
            )
            if cost is None:
                unpriced.append(model)
            else:
                total += cost
        return VendorCost(total, len(counts) - len(unpriced), tuple(sorted(unpriced)))


class PriceTable(BaseModel):
    """The whole pinned table.

    Attributes:
        schema_version: The table format; only ``1`` exists.
        vendors: Entries keyed by vendor id.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1]
    vendors: dict[VendorId, VendorPrices]

    def vendor(self, name: str) -> VendorPrices | None:
        """Return *name*'s entry, or ``None`` when the table does not list it.

        Args:
            name: A vendor id.

        Returns:
            The entry, or ``None``.
        """
        return self.vendors.get(name)


def _invalid(exc: ValidationError) -> PriceTableError:
    """Name the offending fields, never the operator's values.

    A field's location is always safe to quote. A validator's own message is
    quoted too, because every one of them is written in this module and names
    fields only; pydantic's messages for other error types are not, since
    some of them repeat the input.
    """
    parts: set[str] = set()
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"])
        if location:
            parts.add(location)
        if error["type"] == _VALUE_ERROR:
            parts.add(str(error["msg"]))
    return PriceTableError(f"price table is invalid: {'; '.join(sorted(parts))}")


def load_price_table(path: Path = PRICE_TABLE_PATH) -> PriceTable:
    """Read and validate the pinned price table.

    Args:
        path: The JSON table; the shipped one by default.

    Returns:
        The validated table.

    Raises:
        PriceTableError: When the file is missing, is not JSON, or fails the
            schema. The message names fields only.
    """
    document = _read_document(path)
    try:
        return PriceTable.model_validate(document)
    except ValidationError as exc:
        raise _invalid(exc) from exc


def _read_document(path: Path) -> object:
    """Return *path* decoded as JSON, numbers as :class:`Decimal`.

    Raises:
        PriceTableError: When the file is missing or is not JSON.
    """
    if not path.is_file():
        raise PriceTableError(_NOT_FOUND)
    try:
        return json.loads(path.read_text(encoding="utf-8"), parse_float=Decimal)
    except ValueError as exc:
        raise PriceTableError(_NOT_JSON) from exc
