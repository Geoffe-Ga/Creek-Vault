"""Vault-side operator alerts, owed on the volume until the control plane acks (#2049).

A managed vault raises a few operator alerts of its own: the attesting pool's
five outcome alerts (ADR-0016 Decision 1 item 12, #1896) and the local spend
meter's EUR 5 alert (#2007).  Before #2049 each was written only to the vault's
own content-free ``creek.pool`` log, which nobody reads: the fleet report runs
on the control plane and cannot see a vault volume.

This module is the vault half of the channel that carries them to the
operator.  The control plane *pulls* (ADR-0013, amended by #2049): its fleet
pass reads this vault's owed-alert summary over the private network, hands the
new counts to the same :class:`~creek_mcp.provisioning.alert_relay.AlertRelay`
the fleet alerts use, and acknowledges them back.  Nothing here dials out.

**Closed and counts-only.**  An alert is one member of
:data:`VAULT_ALERT_KINDS` - a closed subset of the fleet's
``AlertKind`` vocabulary - and the only thing recorded about it is that it
happened: a per-kind count.  No subject, request id, vendor id, amount,
prompt, answer or key is ever written, so nothing else can leave.

**Owed until acknowledged, durable across restarts.**  The outbox is one JSON
document on the vault's volume (:data:`OUTBOX_RELPATH`), written atomically
under a cross-process lock.  It holds, per kind, the cumulative number
*raised* and the cumulative number the control plane has *acknowledged*; the
difference is what is owed.  Both only ever grow, so an acknowledgement is
idempotent: repeating one, or receiving an old one late, can never
un-acknowledge or over-acknowledge anything.

**An epoch names one outbox.**  A random :attr:`VaultAlertState.epoch` is
minted when the document is first created.  The control plane remembers the
counts it has already handed on *per epoch*, so a vault whose outbox was
recreated (a new volume) starts a fresh sequence instead of being mistaken for
one whose counts went backwards.

**Raising never breaks the caller.**  :func:`owe_vault_alert` is called from
request paths that are already failing for their own reason (an attestation
refusal, a vendor 429).  A full disk or a held lock is logged, content-free,
and swallowed; only an alert kind outside the closed set raises, because that
is a programming error.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

from creek._fsio import atomic_write_text
from creek._fslock import vault_lock

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping

SCHEMA: Final[str] = "creek_vault_alerts_v1"
"""The one wire and on-disk document schema name."""

VAULT_ALERT_KINDS: Final[frozenset[str]] = frozenset(
    {
        # ADR-0016 Decision 1 item 12, named by
        # ``creek.classify.llm.pool_attested.OUTCOME_ALERTS`` (#1896).
        "pool_attestation_failed",
        "pool_credential_rejected",
        "pool_admission_refused",
        "pool_vendor_rate_limited",
        "pool_quota_exhausted",
        # The local spend meter's alert at EUR 5 of the EUR 10 key cap (#2007).
        "monthly_budget_departure",
    }
)
"""Every alert kind a vault may owe the operator.

A closed subset of :class:`creek_mcp.provisioning.budget.AlertKind`, asserted
by the tests; the control plane refuses any other kind a vault reports.
"""

VAULT_ALERTS_PORT: Final[int] = 8824
"""The private listener's port, on the Machine's Fly 6PN address only.

Beside the vault's Fly service port (8823) and never in its service config, so
Fly Proxy never routes public traffic to it.
"""

VAULT_ALERTS_PATH: Final[str] = "/internal/vault-alerts"
"""``GET``: the owed-alert summary."""

VAULT_ALERTS_ACK_PATH: Final[str] = "/internal/vault-alerts/ack"
"""``POST``: acknowledge owed alerts up to per-kind marks."""

MAX_DOCUMENT_BYTES: Final[int] = 4096
"""Inclusive cap on any wire document, either way; a closed one is far smaller."""

OUTBOX_RELPATH: Final[Path] = Path("00-Creek-Meta/alerts/owed-alerts.json")
"""Where the owed-alert document lives, relative to the vault root."""

LOCK_RELPATH: Final[Path] = Path("00-Creek-Meta/locks/vault-alerts.lock")
"""The cross-process lock that serialises every outbox read-modify-write."""

MAX_COUNT: Final[int] = 1_000_000_000
"""Largest cumulative count per kind; raising beyond it saturates."""

EPOCH_PATTERN: Final[str] = r"[0-9a-f]{32}"
"""An epoch is 128 random bits written as lowercase hex."""

_EPOCH_RE: Final[re.Pattern[str]] = re.compile(EPOCH_PATTERN)
_STATE_KEYS: Final[frozenset[str]] = frozenset(
    {"schema", "epoch", "raised", "acknowledged"}
)
_ACK_KEYS: Final[frozenset[str]] = frozenset({"schema", "epoch", "through"})
_EPOCH_BYTES: Final[int] = 16
_LOGGER: Final[logging.Logger] = logging.getLogger(__name__)


class VaultAlertDocumentError(ValueError):
    """A vault alert document is outside the closed shape; nothing is trusted."""


class OutboxUnreadableError(OSError):
    """The outbox exists but cannot be read as a closed document.

    Reported, never repaired by starting again at zero: a reset would silently
    drop whatever was owed.
    """

    def __init__(self) -> None:
        """Carry one fixed, content-free message."""
        super().__init__("vault alert outbox is unreadable")


def _counts(value: object) -> dict[str, int]:
    """Return *value* as closed kind counts, or raise the shape error."""
    if not isinstance(value, dict):
        raise VaultAlertDocumentError
    counts: dict[str, int] = {}
    for kind, count in value.items():
        if (
            kind not in VAULT_ALERT_KINDS
            or type(count) is not int
            or not 0 < count <= MAX_COUNT
        ):
            raise VaultAlertDocumentError
        counts[kind] = count
    return counts


def _epoch(value: object) -> str:
    """Return *value* as an epoch, or raise the shape error."""
    if not isinstance(value, str) or _EPOCH_RE.fullmatch(value) is None:
        raise VaultAlertDocumentError
    return value


@dataclass(frozen=True, slots=True)
class VaultAlertState:
    """One outbox: its epoch and the cumulative raised and acknowledged counts.

    Attributes:
        epoch: The random id minted when this outbox was created.
        raised: Cumulative alerts raised, per kind (only kinds above zero).
        acknowledged: Cumulative alerts the control plane acknowledged, per
            kind; never above :attr:`raised`.
    """

    epoch: str
    raised: Mapping[str, int] = field(default_factory=dict)
    acknowledged: Mapping[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Refuse a state outside the closed shape.

        Raises:
            VaultAlertDocumentError: On any out-of-shape field.
        """
        _epoch(self.epoch)
        raised = _counts(dict(self.raised))
        acknowledged = _counts(dict(self.acknowledged))
        if any(count > raised.get(kind, 0) for kind, count in acknowledged.items()):
            raise VaultAlertDocumentError
        object.__setattr__(
            self, "raised", MappingProxyType(dict(sorted(raised.items())))
        )
        object.__setattr__(
            self, "acknowledged", MappingProxyType(dict(sorted(acknowledged.items())))
        )

    @property
    def owed(self) -> dict[str, int]:
        """Return the alerts raised and not yet acknowledged, per kind."""
        return {
            kind: count - self.acknowledged.get(kind, 0)
            for kind, count in self.raised.items()
            if count > self.acknowledged.get(kind, 0)
        }

    def to_document(self) -> dict[str, object]:
        """Return the closed wire and on-disk document."""
        return {
            "schema": SCHEMA,
            "epoch": self.epoch,
            "raised": dict(self.raised),
            "acknowledged": dict(self.acknowledged),
        }

    def raise_one(self, kind: str) -> VaultAlertState:
        """Return this state with one more *kind* raised, saturating."""
        raised = dict(self.raised)
        raised[kind] = min(raised.get(kind, 0) + 1, MAX_COUNT)
        return VaultAlertState(self.epoch, raised, self.acknowledged)

    def acknowledge(self, through: Mapping[str, int]) -> VaultAlertState:
        """Return this state with every kind acknowledged up to *through*.

        Monotonic and clamped: a mark below what is already acknowledged
        changes nothing, and one above what was raised acknowledges only what
        was raised.
        """
        acknowledged = dict(self.acknowledged)
        for kind, mark in through.items():
            ceiling = min(mark, self.raised.get(kind, 0))
            if ceiling > acknowledged.get(kind, 0):
                acknowledged[kind] = ceiling
        return VaultAlertState(self.epoch, self.raised, acknowledged)


def parse_state(value: object) -> VaultAlertState:
    """Parse one closed owed-alert document.

    Args:
        value: The decoded JSON document.

    Returns:
        The state it carries.

    Raises:
        VaultAlertDocumentError: On any field outside the closed shape.
    """
    if (
        not isinstance(value, dict)
        or set(value) != _STATE_KEYS
        or value["schema"] != SCHEMA
    ):
        raise VaultAlertDocumentError
    return VaultAlertState(
        _epoch(value["epoch"]),
        _counts(value["raised"]),
        _counts(value["acknowledged"]),
    )


def parse_summary(value: object) -> VaultAlertState | None:
    """Parse the wire summary: an outbox, or the bare schema when there is none.

    Args:
        value: The decoded JSON document.

    Returns:
        The state, or ``None`` when the vault has never raised an alert.

    Raises:
        VaultAlertDocumentError: On any field outside the closed shape.
    """
    if value == {"schema": SCHEMA}:
        return None
    return parse_state(value)


@dataclass(frozen=True, slots=True)
class Acknowledgement:
    """The control plane's acknowledgement of one outbox up to per-kind marks.

    Attributes:
        epoch: The outbox the marks belong to.
        through: Cumulative raised counts the control plane has taken
            responsibility for, per kind.
    """

    epoch: str
    through: Mapping[str, int]

    def to_document(self) -> dict[str, object]:
        """Return the closed acknowledgement document."""
        return {
            "schema": SCHEMA,
            "epoch": self.epoch,
            "through": dict(sorted(self.through.items())),
        }


def parse_acknowledgement(value: object) -> Acknowledgement:
    """Parse one closed acknowledgement document.

    Args:
        value: The decoded JSON document.

    Returns:
        The acknowledgement.

    Raises:
        VaultAlertDocumentError: On any field outside the closed shape, or an
            empty acknowledgement.
    """
    if (
        not isinstance(value, dict)
        or set(value) != _ACK_KEYS
        or value["schema"] != SCHEMA
    ):
        raise VaultAlertDocumentError
    through = _counts(value["through"])
    if not through:
        raise VaultAlertDocumentError
    return Acknowledgement(_epoch(value["epoch"]), through)


def _new_epoch() -> str:
    """Return a fresh random epoch."""
    return secrets.token_hex(_EPOCH_BYTES)


class VaultAlertOutbox:
    """The owed-alert document on one vault's volume."""

    def __init__(
        self,
        vault_path: Path,
        *,
        epoch_factory: Callable[[], str] = _new_epoch,
    ) -> None:
        """Bind the outbox under *vault_path*.

        Args:
            vault_path: The vault root on the Machine's volume.
            epoch_factory: Mints the epoch of a newly created outbox.
        """
        self._path = vault_path / OUTBOX_RELPATH
        self._lock_path = vault_path / LOCK_RELPATH
        self._epoch_factory = epoch_factory

    def owe(self, kind: str) -> VaultAlertState:
        """Durably record one more *kind* owed.

        Args:
            kind: A member of :data:`VAULT_ALERT_KINDS`.

        Returns:
            The outbox after the alert was recorded.

        Raises:
            ValueError: When *kind* is outside the closed set.
            OSError: When the outbox cannot be locked, read or written.
        """
        if kind not in VAULT_ALERT_KINDS:
            msg = "vault alert kind is outside the closed set"
            raise ValueError(msg)
        with vault_lock(self._lock_path):
            state = self._read() or VaultAlertState(self._epoch_factory())
            updated = state.raise_one(kind)
            self._write(updated)
        return updated

    def summary(self) -> VaultAlertState | None:
        """Return the outbox, or ``None`` when no alert was ever raised.

        Raises:
            OutboxUnreadableError: When the document exists but is unreadable.
            OSError: When the outbox cannot be locked.
        """
        with vault_lock(self._lock_path):
            return self._read()

    def acknowledge(self, acknowledgement: Acknowledgement) -> VaultAlertState | None:
        """Apply *acknowledgement* when it names this outbox's epoch.

        An acknowledgement for another epoch - an outbox this volume no longer
        holds - changes nothing.

        Args:
            acknowledgement: The control plane's per-kind marks.

        Returns:
            The outbox after the acknowledgement, or ``None`` when there is
            none.

        Raises:
            OutboxUnreadableError: When the document exists but is unreadable.
            OSError: When the outbox cannot be locked or written.
        """
        with vault_lock(self._lock_path):
            state = self._read()
            if state is None or state.epoch != acknowledgement.epoch:
                return state
            updated = state.acknowledge(acknowledgement.through)
            if updated != state:
                self._write(updated)
            return updated

    def _read(self) -> VaultAlertState | None:
        """Return the stored state, ``None`` when absent, or raise unreadable."""
        text = self._text()
        if text is None:
            return None
        try:
            return parse_state(json.loads(text))
        except (ValueError, RecursionError) as exc:
            raise OutboxUnreadableError from exc

    def _text(self) -> str | None:
        """Return the stored document's text, ``None`` when absent."""
        try:
            return self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except (OSError, UnicodeError) as exc:
            raise OutboxUnreadableError from exc

    def _write(self, state: VaultAlertState) -> None:
        """Atomically replace the stored document with *state*."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(
            self._path, json.dumps(state.to_document(), sort_keys=True) + "\n"
        )


_INSTALLED: Final[dict[str, VaultAlertOutbox]] = {}
_INSTALLED_LOCK: Final[threading.Lock] = threading.Lock()
_ACTIVE: Final[str] = "outbox"


def active_vault_alert_outbox() -> VaultAlertOutbox | None:
    """Return the outbox the serving runtime installed, if any.

    Returns:
        The process's outbox, or ``None`` outside a managed runtime, where an
        alert stays in the content-free log only.
    """
    with _INSTALLED_LOCK:
        return _INSTALLED.get(_ACTIVE)


@contextmanager
def installed_vault_alert_outbox(vault_path: Path) -> Iterator[VaultAlertOutbox]:
    """Install an outbox over *vault_path* for the block, then restore the last.

    Args:
        vault_path: The vault root on the Machine's volume.

    Yields:
        The installed outbox.
    """
    outbox = VaultAlertOutbox(vault_path)
    with _INSTALLED_LOCK:
        previous = _INSTALLED.get(_ACTIVE)
        _INSTALLED[_ACTIVE] = outbox
    try:
        yield outbox
    finally:
        with _INSTALLED_LOCK:
            if previous is None:
                _INSTALLED.pop(_ACTIVE, None)
            else:
                _INSTALLED[_ACTIVE] = previous


def owe_vault_alert(kind: str) -> None:
    """Record one *kind* owed to the operator, when a runtime installed an outbox.

    The one call a raiser makes - ``record_pool_outcome`` for the pool's
    outcome alerts, and the spend meter for its EUR 5 alert.  Outside a
    managed runtime there is no outbox and this does nothing; a storage
    failure is logged without content and swallowed, so it never turns the
    caller's own outcome into a different one.

    Args:
        kind: A member of :data:`VAULT_ALERT_KINDS`.

    Raises:
        ValueError: When *kind* is outside the closed set, installed or not.
    """
    if kind not in VAULT_ALERT_KINDS:
        msg = "vault alert kind is outside the closed set"
        raise ValueError(msg)
    outbox = active_vault_alert_outbox()
    if outbox is None:
        return
    try:
        outbox.owe(kind)
    except OSError:
        _LOGGER.warning("vault alert could not be recorded kind=%s", kind)
