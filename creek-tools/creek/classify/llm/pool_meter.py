"""Local, per-vault metering of shared-pool spend, and its 50% alert (#2007).

Owner decision 2026-11-01 on #2007, carrying out R4-2: *meter locally* —
count the tokens Creek sends through the pool, price them at the vendor's
documented rate, and alert at EUR 5, half of the pilot key's EUR 10 monthly cap
(R4-1). No vendor usage API is read. Every pool generation and embeddings call
already passes through :mod:`creek.classify.llm.pool_boundary`, which hands each
one to :func:`meter_answer` or :func:`meter_unanswered`; nothing else here dials
anything.

**Vendor-neutral.** The meter knows a vendor only as an id read from the
Machine environment and a model only as the id in the request. Usage is read
from the OpenAI-compatible ``usage`` block (``prompt_tokens`` /
``completion_tokens``; an embeddings answer reports ``prompt_tokens``), and
prices come from the pinned table in :mod:`creek.classify.llm.pool_prices`.
Switching vendors is a new table entry.

**Never under-count.** A provider-reported count is taken as reported. When an
answer carries no usable count, the missing half is *estimated* from bytes at
:data:`ESTIMATE_BYTES_PER_TOKEN` and the call is flagged: a byte-level BPE
token always covers at least one byte of text, and the compact JSON encoding
of a request or answer is never shorter than the text inside it, so the byte
count bounds the token count from above. A dial that sent its body but got no
answer may still have been billed, so it is counted too (:func:`usage_unanswered`).
A vendor refusal (any non-200 answer) bills nothing and is not counted.
Spend is rounded **up** to the cent, and a ledger that cannot be read is
reported as unreadable — never restarted from zero.

**Per vault, per calendar month in UTC.** A managed vault is one Machine with
one volume and one activation key, so the ledger at :data:`LEDGER_RELPATH` on
that volume *is* the per-key accumulator. It is durable across restarts
(written atomically under a cross-process lock) and starts again at zero on
the first call of a new UTC month, which also re-arms the alert.

**One alert per month per vault.** The first call that takes the month's
spend to ``>= monthly_cap x alert_fraction`` writes one content-free record
naming :data:`SPEND_ALERT_KIND` to the ``creek.pool`` logger, exactly as the
five vault-side pool kinds are named today; the latch is in the ledger, so a
restart does not repeat it. Until the operator pins prices the meter reports
tokens only, with the state ``price-unpinned``, and raises no alert.

**Partly priced is still metered (#2052).** When a pinned vendor prices some
of the month's counted models but not all — a new model id, an embeddings
model not yet pinned — the priced models still sum to a figure. It is a
*lower bound*, since an unpriced model can only add to the bill, so the line
says ``price-partial``, reports ``spend_lower_bound`` and never ``spend``, and
names how many models, and which ids, are unpriced. The alert fires when the
lower bound alone reaches the line: alerting early is safe, and the month's
latch still allows exactly one. A ``price_partial`` status record names the
unpriced model ids once per process for each new set of them, so the
operator knows what to pin.

**Content-free.** The ledger and every record hold counts, money, a month,
and closed vendor and model ids — never a prompt, an answer, a request id or a
key. A vendor or model id that is not a closed identifier is stored as
:data:`UNRECOGNISED_VENDOR` or :data:`UNRECOGNISED_MODEL`.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import ROUND_CEILING, Decimal
from enum import StrEnum, unique
from pathlib import Path
from typing import TYPE_CHECKING, Final

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from creek._fslock import vault_lock
from creek.classify.llm.pool_prices import (
    MODEL_ID_PATTERN,
    PRICE_TABLE_PATH,
    VENDOR_ID_PATTERN,
    PriceTable,
    PriceTableError,
    VendorCost,
    VendorPrices,
    load_price_table,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping

TELEMETRY_LOGGER: Final[str] = "creek.pool"
"""Logger every content-free pool record goes to."""

SPEND_ALERT_KIND: Final[str] = "monthly_budget_departure"
"""The fleet alert kind the spend record names.

A member of :class:`creek_mcp.provisioning.budget.AlertKind` already admitted
by adepthood's receiver, reused rather than widening the closed vocabulary:
the owner's EUR 5 line is the pool key's monthly budget, and crossing it is a
departure from it. The record's ``component`` is ``pool``, which tells it apart
from the fleet's own Fly budget departure.
"""

LEDGER_RELPATH: Final[Path] = Path("00-Creek-Meta/pool/spend-ledger.json")
"""Where, under the vault root, the month's counts live."""

LOCK_RELPATH: Final[Path] = Path("00-Creek-Meta/locks/pool-spend.lock")
"""The cross-process lock serialising ledger read-modify-write cycles."""

ESTIMATE_BYTES_PER_TOKEN: Final[int] = 1
"""Bytes per token when a count must be estimated: an upper bound on tokens.

A byte-level BPE token covers at least one byte, so a text of *n* bytes is at
most *n* tokens. Deliberately far below any typical ratio: the estimate is a
ceiling, flagged as one, never a best guess.
"""

UNANSWERED_COMPLETION_ESTIMATE: Final[int] = 1024
"""Completion tokens charged to a dispatched, unanswered generation.

Used only when the request named no ``max_tokens``, in which case no true
ceiling exists. The same figure as the closed shape's own output ceiling, and
always flagged as an estimate.
"""

CREEK_RUN_POOL_VENDOR: Final[str] = "creek"
"""The vendor id spend is booked to when no vendor is named: the O3 pool.

The table lists no price for it, because a Creek-run pool is not billed per
token, so its line always reads ``price-unpinned``.
"""

UNRECOGNISED_VENDOR: Final[str] = "unrecognised-vendor"
"""Ledger id for a vendor whose id is not a closed identifier."""

UNRECOGNISED_MODEL: Final[str] = "unrecognised-model"
"""Ledger id for a model whose id is not a closed identifier."""

_HTTP_OK: Final[int] = 200
_CENT: Final[Decimal] = Decimal("0.01")
_MONTH_FORMAT: Final[str] = "%Y-%m"
_LEDGER_SCHEMA: Final[int] = 1
_LOGGER: Final[logging.Logger] = logging.getLogger(TELEMETRY_LOGGER)


@unique
class PoolCallKind(StrEnum):
    """Which pool endpoint a metered call reached."""

    GENERATION = "generation"
    EMBEDDING = "embedding"


@unique
class SpendState(StrEnum):
    """The closed states a month's spend line can be in."""

    NO_USAGE = "no-usage"
    UNDER_THRESHOLD = "under-threshold"
    THRESHOLD_REACHED = "threshold-reached"
    PRICE_PARTIAL = "price-partial"
    PRICE_UNPINNED = "price-unpinned"
    PRICE_TABLE_INVALID = "price-table-invalid"
    LEDGER_UNREADABLE = "ledger-unreadable"


_ANNOUNCED: Final[dict[SpendState, str]] = {
    SpendState.PRICE_PARTIAL: "price_partial",
    SpendState.PRICE_UNPINNED: "price_unpinned",
    SpendState.PRICE_TABLE_INVALID: "price_table_invalid",
}
"""States worth one content-free status record per meter, by its label."""


@dataclass(frozen=True, slots=True)
class PoolUsage:
    """The tokens one pool call is charged with.

    Attributes:
        prompt_tokens: Generation prompt (input) tokens.
        completion_tokens: Generation completion (output) tokens.
        embedding_tokens: Embedded tokens.
        estimated: Whether any of the three is an estimate.
    """

    prompt_tokens: int
    completion_tokens: int
    embedding_tokens: int
    estimated: bool = False


def _reported(usage: Mapping[str, object], key: str) -> int | None:
    """Return a non-negative integer count at *key*, or ``None``."""
    value = usage.get(key)
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _byte_bound(value: object) -> int:
    """Return the token ceiling for *value*: its compact JSON byte length."""
    encoded = json.dumps(value, separators=(",", ":"), default=str).encode("utf-8")
    return -(-len(encoded) // ESTIMATE_BYTES_PER_TOKEN)


def _usage_block(answer: object) -> Mapping[str, object]:
    """Return the answer's ``usage`` mapping, or an empty one."""
    usage = answer.get("usage") if isinstance(answer, dict) else None
    return usage if isinstance(usage, dict) else {}


def usage_from_answer(
    kind: PoolCallKind, body: Mapping[str, object], answer: object
) -> PoolUsage:
    """Return the usage a successful answer is charged with.

    Args:
        kind: Which endpoint answered.
        body: The request body that was sent.
        answer: The decoded answer.

    Returns:
        The reported counts; any missing half estimated from bytes, flagged.
    """
    usage = _usage_block(answer)
    if kind is PoolCallKind.EMBEDDING:
        embedded = _reported(usage, "prompt_tokens")
        if embedded is None:
            embedded = _reported(usage, "total_tokens")
        if embedded is not None:
            return PoolUsage(0, 0, embedded)
        return PoolUsage(0, 0, _byte_bound(body), estimated=True)
    prompt = _reported(usage, "prompt_tokens")
    completion = _reported(usage, "completion_tokens")
    return PoolUsage(
        _byte_bound(body) if prompt is None else prompt,
        _byte_bound(answer) if completion is None else completion,
        0,
        estimated=prompt is None or completion is None,
    )


def usage_unanswered(kind: PoolCallKind, body: Mapping[str, object]) -> PoolUsage:
    """Return the usage a dispatched call that got no answer is charged with.

    Args:
        kind: Which endpoint was dialled.
        body: The request body that was sent.

    Returns:
        An estimate: the body's byte bound, plus for a generation its
        ``max_tokens`` or :data:`UNANSWERED_COMPLETION_ESTIMATE`.
    """
    if kind is PoolCallKind.EMBEDDING:
        return PoolUsage(0, 0, _byte_bound(body), estimated=True)
    ceiling = _reported(body, "max_tokens")
    return PoolUsage(
        _byte_bound(body),
        ceiling or UNANSWERED_COMPLETION_ESTIMATE,
        0,
        estimated=True,
    )


class _ModelCounts(BaseModel):
    """One model's month-to-date counts."""

    model_config = ConfigDict(extra="forbid")

    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)
    embedding_tokens: int = Field(default=0, ge=0)
    calls: int = Field(default=0, ge=0)
    estimated_calls: int = Field(default=0, ge=0)

    def add(self, usage: PoolUsage) -> None:
        """Add one call's usage."""
        self.prompt_tokens += usage.prompt_tokens
        self.completion_tokens += usage.completion_tokens
        self.embedding_tokens += usage.embedding_tokens
        self.calls += 1
        self.estimated_calls += int(usage.estimated)


class _VendorLedger(BaseModel):
    """One vendor's month: counts per model and the alert latch."""

    model_config = ConfigDict(extra="forbid")

    alerted: bool = False
    models: dict[str, _ModelCounts] = Field(default_factory=dict)


class _Ledger(BaseModel):
    """The month's whole ledger."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(ge=_LEDGER_SCHEMA, le=_LEDGER_SCHEMA)
    month: str = Field(pattern=r"^\d{4}-\d{2}$")
    vendors: dict[str, _VendorLedger] = Field(default_factory=dict)


class LedgerUnreadableError(OSError):
    """The ledger exists but cannot be trusted; it is never overwritten."""


@dataclass(frozen=True, slots=True)
class VendorSpend:
    """One vendor's month-to-date spend line; content-free.

    Attributes:
        vendor: The vendor id, or ``None`` for a line about the whole ledger.
        month: The UTC calendar month, ``YYYY-MM``.
        state: Which closed state the line is in.
        prompt_tokens: Prompt tokens this month.
        completion_tokens: Completion tokens this month.
        embedding_tokens: Embedded tokens this month.
        calls: Calls metered this month.
        estimated_calls: How many of those carried an estimate.
        spend: Month-to-date spend rounded up to the cent, or ``None`` unless
            every counted model is priced.
        threshold: The alert line, when priced.
        cap: The key's monthly cap, when priced.
        currency: The currency of the amounts, when priced.
        spend_lower_bound: For a ``price-partial`` line only, the priced
            models' spend rounded up to the cent: a lower bound, never a total.
        unpriced_models: The sorted ids of counted models with no price, for a
            ``price-partial`` line.
        threshold_reached: Whether the exact priced spend is at or above the
            alert line.
    """

    vendor: str | None
    month: str
    state: SpendState
    prompt_tokens: int = 0
    completion_tokens: int = 0
    embedding_tokens: int = 0
    calls: int = 0
    estimated_calls: int = 0
    spend: Decimal | None = None
    threshold: Decimal | None = None
    cap: Decimal | None = None
    currency: str | None = None
    spend_lower_bound: Decimal | None = None
    unpriced_models: tuple[str, ...] = ()
    threshold_reached: bool = False

    def summary_line(self) -> str:
        """Return the line ``--check pool`` prints: closed keys and values only."""
        head = "pool-spend"
        if self.vendor is not None:
            head += f" vendor={self.vendor}"
        head += f" month={self.month} state={self.state.value}"
        if self.vendor is None:
            return head
        line = (
            f"{head} prompt_tokens={self.prompt_tokens} "
            f"completion_tokens={self.completion_tokens} "
            f"embedding_tokens={self.embedding_tokens} "
            f"calls={self.calls} estimated_calls={self.estimated_calls}"
        )
        if self.spend is not None:
            return f"{line} {self._amounts('spend', self.spend)}"
        if self.spend_lower_bound is None:
            return line
        reached = "yes" if self.threshold_reached else "no"
        return (
            f"{line} {self._amounts('spend_lower_bound', self.spend_lower_bound)} "
            f"threshold_reached={reached} "
            f"unpriced_models={len(self.unpriced_models)} "
            f"unpriced_model_ids={','.join(self.unpriced_models)}"
        )

    def _amounts(self, key: str, amount: Decimal) -> str:
        """Return the money pairs: *key*, then threshold, cap and currency."""
        return (
            f"{key}={amount} threshold={self.threshold} "
            f"cap={self.cap} currency={self.currency}"
        )

    def unpriced_fields(self) -> dict[str, object]:
        """Return the content-free fields naming unpriced models, if any.

        Returns:
            ``unpriced_models`` (a count) and ``unpriced_model_ids`` (closed
            ids) for a partly priced line; empty otherwise, so a fully priced
            or fully unpinned record keeps exactly its former keys.
        """
        if not self.unpriced_models:
            return {}
        return {
            "unpriced_models": len(self.unpriced_models),
            "unpriced_model_ids": list(self.unpriced_models),
        }


def _utc_now() -> datetime:
    """Return the current instant in UTC."""
    return datetime.now(tz=UTC)


def _month_of(instant: datetime) -> str:
    """Return the UTC calendar month containing *instant*."""
    return instant.astimezone(UTC).strftime(_MONTH_FORMAT)


def _closed_id(value: str, pattern: str, fallback: str) -> str:
    """Return *value* when it is a closed identifier, else *fallback*."""
    return value if re.fullmatch(pattern, value) else fallback


def _cents_up(amount: Decimal) -> Decimal:
    """Round *amount* up to the cent: rounding down would understate a bill."""
    return amount.quantize(_CENT, rounding=ROUND_CEILING)


def _log(record: dict[str, object], *, level: int = logging.INFO) -> None:
    """Write one content-free record to the ``creek.pool`` logger."""
    _LOGGER.log(level, json.dumps({"event": "pool_spend"} | record, sort_keys=True))


def _write_atomically(path: Path, text: str) -> None:
    """Replace *path* with *text* in one rename, flushed to disk first."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        Path(name).replace(path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            Path(name).unlink()


def _ledger_text(path: Path) -> str | None:
    """Return the ledger's text, or ``None`` when no ledger exists yet.

    Raises:
        LedgerUnreadableError: When it exists but cannot be read.
    """
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise LedgerUnreadableError(SpendState.LEDGER_UNREADABLE.value) from exc


def _sums(line: _VendorLedger) -> tuple[int, int, int, int, int]:
    """Return a vendor's ``(prompt, completion, embedding, calls, estimated)``."""
    counts = line.models.values()
    return (
        sum(model.prompt_tokens for model in counts),
        sum(model.completion_tokens for model in counts),
        sum(model.embedding_tokens for model in counts),
        sum(model.calls for model in counts),
        sum(model.estimated_calls for model in counts),
    )


class PoolSpendMeter:
    """The month-to-date spend ledger on one vault's volume."""

    def __init__(
        self,
        vault_path: Path,
        *,
        prices: PriceTable | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        """Meter into *vault_path*'s ledger.

        Args:
            vault_path: The vault root on the Machine's volume.
            prices: The price table; the shipped one, loaded lazily, by default.
            clock: The UTC clock that decides the month.
        """
        self._ledger_path = vault_path / LEDGER_RELPATH
        self._lock_path = vault_path / LOCK_RELPATH
        self._prices = prices
        self._prices_invalid = False
        self._clock = clock
        self._announced: set[tuple[str, SpendState, tuple[str, ...]]] = set()

    def record(self, vendor: str, model: str, usage: PoolUsage) -> VendorSpend:
        """Add one call's usage to the month and alert on the first crossing.

        Args:
            vendor: The vendor id from the Machine environment.
            model: The model id the request named.
            usage: The call's usage.

        Returns:
            The vendor's spend line after this call.

        Raises:
            OSError: When the ledger cannot be locked or written.
        """
        vendor = _closed_id(vendor, VENDOR_ID_PATTERN, UNRECOGNISED_VENDOR)
        model = _closed_id(model, MODEL_ID_PATTERN, UNRECOGNISED_MODEL)
        month = _month_of(self._clock())
        try:
            with vault_lock(self._lock_path):
                spend, alert = self._add(month, vendor, model, usage)
        except LedgerUnreadableError:
            _log({"status": "ledger_unreadable"}, level=logging.WARNING)
            return VendorSpend(vendor, month, SpendState.LEDGER_UNREADABLE)
        if alert:
            self._alert(spend)
        self._announce(spend)
        return spend

    def report(self) -> tuple[VendorSpend, ...]:
        """Return the month's spend lines without writing anything.

        Returns:
            One line per vendor metered this month, or a single ``no-usage``
            or ``ledger-unreadable`` line.
        """
        month = _month_of(self._clock())
        try:
            ledger = self._read(month)
        except LedgerUnreadableError:
            return (VendorSpend(None, month, SpendState.LEDGER_UNREADABLE),)
        if not ledger.vendors:
            return (VendorSpend(None, month, SpendState.NO_USAGE),)
        return tuple(
            self._evaluate(month, vendor, line)
            for vendor, line in sorted(ledger.vendors.items())
        )

    def _add(
        self, month: str, vendor: str, model: str, usage: PoolUsage
    ) -> tuple[VendorSpend, bool]:
        """Read, add, latch and write, under the caller's lock."""
        ledger = self._read(month)
        line = ledger.vendors.setdefault(vendor, _VendorLedger())
        line.models.setdefault(model, _ModelCounts()).add(usage)
        spend = self._evaluate(month, vendor, line)
        alert = spend.threshold_reached and not line.alerted
        line.alerted = line.alerted or alert
        _write_atomically(self._ledger_path, ledger.model_dump_json(indent=2))
        return spend, alert

    def _read(self, month: str) -> _Ledger:
        """Return *month*'s ledger: empty when absent or from an earlier month.

        Raises:
            LedgerUnreadableError: When the file exists but cannot be read or
                fails the schema.
        """
        text = _ledger_text(self._ledger_path)
        if text is None:
            return _Ledger(schema_version=_LEDGER_SCHEMA, month=month)
        try:
            ledger = _Ledger.model_validate_json(text)
        except ValidationError as exc:
            raise LedgerUnreadableError(SpendState.LEDGER_UNREADABLE.value) from exc
        if ledger.month != month:
            return _Ledger(schema_version=_LEDGER_SCHEMA, month=month)
        return ledger

    def _table(self) -> PriceTable | None:
        """Return the price table, loading the shipped one once; ``None`` if invalid."""
        if self._prices is None and not self._prices_invalid:
            try:
                self._prices = load_price_table(PRICE_TABLE_PATH)
            except PriceTableError:
                self._prices_invalid = True
        return self._prices

    def _evaluate(self, month: str, vendor: str, line: _VendorLedger) -> VendorSpend:
        """Price one vendor's month and grade it against its alert line."""
        unpriced = VendorSpend(vendor, month, SpendState.PRICE_UNPINNED, *_sums(line))
        table = self._table()
        if table is None:
            return replace(unpriced, state=SpendState.PRICE_TABLE_INVALID)
        entry = table.vendor(vendor)
        cost = None if entry is None else entry.price(_per_model(line))
        if entry is None or cost is None or not cost.priced_models:
            return unpriced
        return _priced(unpriced, entry, cost)

    def _alert(self, spend: VendorSpend) -> None:
        """Write the month's one spend alert record.

        A fully priced line keeps the record's original keys. A partly priced
        one reports ``spend_lower_bound`` in place of ``spend``, says
        ``price_state: price-partial``, and names the unpriced models.
        """
        if spend.state is SpendState.PRICE_PARTIAL:
            amount = {
                "spend_lower_bound": str(spend.spend_lower_bound),
                "price_state": spend.state.value,
            }
        else:
            amount = {"spend": str(spend.spend)}
        _log(
            {
                "alert": SPEND_ALERT_KIND,
                "component": "pool",
                "vendor": spend.vendor,
                "month": spend.month,
                "threshold": str(spend.threshold),
                "cap": str(spend.cap),
                "currency": spend.currency,
            }
            | amount
            | spend.unpriced_fields(),
            level=logging.WARNING,
        )

    def _announce(self, spend: VendorSpend) -> None:
        """Raise a content-free status once per meter for each unpriced set.

        An unpinned line is announced once per vendor. A partly priced one is
        announced again whenever its set of unpriced models changes, so each
        new model id the operator must pin is named.
        """
        label = _ANNOUNCED.get(spend.state)
        key = (spend.vendor or "", spend.state, spend.unpriced_models)
        if label is None or key in self._announced:
            return
        self._announced.add(key)
        _log(
            {"status": label, "vendor": spend.vendor} | spend.unpriced_fields(),
            level=logging.WARNING,
        )


def _per_model(line: _VendorLedger) -> dict[str, tuple[int, int, int]]:
    """Return ``(prompt, completion, embedding)`` per model id."""
    return {
        model: (counts.prompt_tokens, counts.completion_tokens, counts.embedding_tokens)
        for model, counts in line.models.items()
    }


def _priced(line: VendorSpend, entry: VendorPrices, cost: VendorCost) -> VendorSpend:
    """Return *line* priced at *cost*; the comparison uses the exact cost.

    With any counted model unpriced, the figure is reported only as
    ``spend_lower_bound`` under ``price-partial``, never as ``spend``.
    """
    reached = cost.priced >= entry.alert_threshold
    amount = _cents_up(cost.priced)
    priced = replace(
        line,
        threshold=_cents_up(entry.alert_threshold),
        cap=_cents_up(entry.monthly_cap),
        currency=entry.currency,
        threshold_reached=reached,
    )
    if cost.unpriced:
        return replace(
            priced,
            state=SpendState.PRICE_PARTIAL,
            spend_lower_bound=amount,
            unpriced_models=cost.unpriced,
        )
    return replace(
        priced,
        state=SpendState.THRESHOLD_REACHED if reached else SpendState.UNDER_THRESHOLD,
        spend=amount,
    )


_INSTALLED: Final[dict[str, PoolSpendMeter]] = {}
_INSTALLED_LOCK: Final[threading.Lock] = threading.Lock()
_ACTIVE: Final[str] = "meter"


def active_pool_meter() -> PoolSpendMeter | None:
    """Return the meter the serving runtime installed, if any.

    Returns:
        The process's meter, or ``None`` outside a managed runtime, where
        nothing is metered.
    """
    with _INSTALLED_LOCK:
        return _INSTALLED.get(_ACTIVE)


@contextmanager
def pool_spend_meter(
    vault_path: Path,
    *,
    prices: PriceTable | None = None,
    clock: Callable[[], datetime] = _utc_now,
) -> Iterator[PoolSpendMeter]:
    """Install a meter over *vault_path* for the block, then restore the last.

    Args:
        vault_path: The vault root on the Machine's volume.
        prices: The price table; the shipped one by default.
        clock: The UTC clock that decides the month.

    Yields:
        The installed meter.
    """
    meter = PoolSpendMeter(vault_path, prices=prices, clock=clock)
    with _INSTALLED_LOCK:
        previous = _INSTALLED.get(_ACTIVE)
        _INSTALLED[_ACTIVE] = meter
    try:
        yield meter
    finally:
        with _INSTALLED_LOCK:
            if previous is None:
                _INSTALLED.pop(_ACTIVE, None)
            else:
                _INSTALLED[_ACTIVE] = previous


def _model_of(body: Mapping[str, object]) -> str:
    """Return the request's model id, or :data:`UNRECOGNISED_MODEL`."""
    model = body.get("model")
    return model if isinstance(model, str) else UNRECOGNISED_MODEL


def _safely_record(vendor: str, body: Mapping[str, object], usage: PoolUsage) -> None:
    """Record *usage* on the active meter; a failure is logged, never raised.

    The call has already been made and possibly billed, so failing it now
    would cost the writer an answer and save nothing.
    """
    meter = active_pool_meter()
    if meter is None:
        return
    try:
        meter.record(vendor, _model_of(body), usage)
    except OSError:
        _log({"status": "meter_unavailable"}, level=logging.WARNING)


def meter_answer(
    kind: PoolCallKind,
    vendor: str,
    body: Mapping[str, object],
    *,
    status: int,
    payload: object,
) -> None:
    """Meter one answered pool call; a non-200 answer bills nothing.

    Args:
        kind: Which endpoint answered.
        vendor: The vendor id.
        body: The request body that was sent.
        status: The HTTP status of the answer.
        payload: The decoded answer.
    """
    if status != _HTTP_OK or active_pool_meter() is None:
        return
    _safely_record(vendor, body, usage_from_answer(kind, body, payload))


def meter_unanswered(
    kind: PoolCallKind, vendor: str, body: Mapping[str, object]
) -> None:
    """Meter one pool call that sent its body but got no usable answer.

    Args:
        kind: Which endpoint was dialled.
        vendor: The vendor id.
        body: The request body that was sent.
    """
    if active_pool_meter() is None:
        return
    _safely_record(vendor, body, usage_unanswered(kind, body))
