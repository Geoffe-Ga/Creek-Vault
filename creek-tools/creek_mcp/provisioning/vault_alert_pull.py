"""The control plane pulls each running vault's owed alerts (#2049).

A managed vault records its own operator alerts - the attesting pool's five
outcome alerts and the spend meter's EUR 5 alert - in an outbox on its volume
(:mod:`creek.vault_alerts`).  This module is the control-plane half of the
channel that carries them to the operator, chosen as a **pull** and recorded
in ADR-0013's #2049 amendment:

1. After each fleet report pass, :meth:`VaultAlertCollector.collect` reads the
   owed-alert summary of every allocation that pass observed *running*, over
   the vault's private listener (Fly 6PN, never the public edge), presenting
   the same two secrets a replayed request carries: the allocation's replay
   state and its consumer bearer.  A stopped vault is not woken.
2. Only what lies above the durable per-epoch mark is new; it is handed to the
   same :class:`~creek_mcp.provisioning.alert_relay.AlertRelay` the fleet's
   own alerts use, so it reaches the operator through
   :class:`~creek_mcp.provisioning.pilot_alerts.HttpFleetAlertSink` as closed
   ``{kind: count}`` counts and nothing else.
3. After the relay's flush - which either delivered the counts or retained
   them durably for retry - :meth:`VaultAlertCollector.commit` raises the
   marks and only then acknowledges the vault, which stops owing them.

**Delivered once.**  The mark is recorded before the acknowledgement, so a
lost acknowledgement re-acks next pass without re-delivering.  The one
duplicate window is a control-plane crash between the relay's flush and the
mark: the alert is then delivered again, never lost.

**Never lost.**  A vault keeps an alert owed on its volume until it is
acknowledged, across restarts.  One that is stopped or unreachable is
re-read on the next pass that sees it running; until then the fleet report
shows its last-known owed counts and why it was not read.

**Admission.**  An alert a vault still owes does *not* close the activation
gate: a stopped vault holding a spend alert is the ordinary case, not an
alert-channel failure.  Owner decision R2-4 still applies to the channel
itself - if the relay cannot deliver vault alerts it closes admission exactly
as it does for fleet alerts.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, Protocol

import httpx

from creek.vault_alerts import (
    MAX_DOCUMENT_BYTES,
    VAULT_ALERTS_ACK_PATH,
    VAULT_ALERTS_PATH,
    VAULT_ALERTS_PORT,
    Acknowledgement,
    parse_summary,
)
from creek_mcp.provisioning.driver import ProviderError
from creek_mcp.provisioning.models import FailureReason
from creek_mcp.provisioning.replay_contract import is_replay_state
from creek_mcp.provisioning.vault_alert_schema import CollectionOutcome

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from creek.vault_alerts import VaultAlertState
    from creek_mcp.provisioning.fly import FlyRuntimeSecrets
    from creek_mcp.provisioning.models import RoutableAllocation
    from creek_mcp.provisioning.store import ProvisioningStore

_LOGGER = logging.getLogger(__name__)
_HTTP_OK: Final[int] = 200
_REPLAY_SOURCE_HEADER: Final[str] = "fly-replay-src"
_FLY_ALLOCATION_RE: Final[re.Pattern[str]] = re.compile(r"fly-([0-9a-f]{24})")
_APP_PREFIX_RE: Final[re.Pattern[str]] = re.compile(
    r"[a-z0-9](?:[a-z0-9-]{0,36}[a-z0-9])?"
)
DEFAULT_TIMEOUT_SECONDS: Final[float] = 5.0
"""Per-request budget for one vault read or acknowledgement."""


class VaultAlertPullError(RuntimeError):
    """One vault's owed alerts could not be read or acknowledged.

    Attributes:
        outcome: The closed reason: unreachable, or refused (any answer
            outside the closed shape).
    """

    def __init__(self, outcome: CollectionOutcome) -> None:
        """Store the closed outcome behind a fixed message."""
        super().__init__(f"vault alerts were not read: {outcome.value}")
        self.outcome = outcome


@dataclass(frozen=True, slots=True)
class VaultAlertTarget:
    """One running vault's private listener and the secrets that open it."""

    provider_allocation_id: str
    base_url: str = field(repr=False)
    replay_state: str = field(repr=False)
    bearer: str = field(repr=False)

    def __post_init__(self) -> None:
        """Refuse a target that is not a plain private origin and closed state."""
        try:
            url = httpx.URL(self.base_url)
        except httpx.InvalidURL as exc:
            raise ValueError("vault alert target is invalid") from exc
        if (
            url.scheme != "http"
            or url.host is None
            or not url.host.endswith(".internal")
            or url.userinfo
            or url.query
            or url.fragment
            or url.path not in {"", "/"}
            or not is_replay_state(self.replay_state)
            or not self.bearer
        ):
            raise ValueError("vault alert target is invalid")

    def headers(self) -> dict[str, str]:
        """Return the two secrets a replayed request carries, as headers."""
        return {
            _REPLAY_SOURCE_HEADER: f"state={self.replay_state}",
            "Authorization": f"Bearer {self.bearer}",
        }


class VaultAlertEndpoint(Protocol):
    """Read and acknowledge one vault's owed alerts."""

    def fetch(self, target: VaultAlertTarget) -> VaultAlertState | None:
        """Return the vault's outbox, or ``None`` when it never raised one."""

    def acknowledge(
        self,
        target: VaultAlertTarget,
        acknowledgement: Acknowledgement,
    ) -> VaultAlertState | None:
        """Acknowledge up to the marks; return the outbox after it."""


class HttpVaultAlertEndpoint:
    """The production endpoint: bounded HTTP to a vault's private listener."""

    def __init__(self, client: httpx.Client | None = None) -> None:
        """Bind *client*, or a proxy-free bounded one.

        ``trust_env=False`` for the reason the router's private client has it
        (#2045): no proxy setting may move a request carrying a vault's
        secrets off Fly's private network.
        """
        self._client = client or httpx.Client(
            timeout=httpx.Timeout(DEFAULT_TIMEOUT_SECONDS), trust_env=False
        )

    def fetch(self, target: VaultAlertTarget) -> VaultAlertState | None:
        """Read the closed summary, or raise the closed pull error."""
        return self._exchange(target, "GET", VAULT_ALERTS_PATH, None)

    def acknowledge(
        self,
        target: VaultAlertTarget,
        acknowledgement: Acknowledgement,
    ) -> VaultAlertState | None:
        """Post the closed acknowledgement, or raise the closed pull error."""
        return self._exchange(
            target, "POST", VAULT_ALERTS_ACK_PATH, acknowledgement.to_document()
        )

    def close(self) -> None:
        """Release the HTTP transport."""
        self._client.close()

    def _exchange(
        self,
        target: VaultAlertTarget,
        method: str,
        path: str,
        document: dict[str, object] | None,
    ) -> VaultAlertState | None:
        """Make one bounded request and parse its closed answer."""
        try:
            with self._client.stream(
                method,
                f"{target.base_url.rstrip('/')}{path}",
                headers=target.headers(),
                json=document,
            ) as response:
                status = response.status_code
                body = _bounded(response)
        except httpx.HTTPError:
            raise VaultAlertPullError(CollectionOutcome.UNREACHABLE) from None
        if status != _HTTP_OK or body is None:
            raise VaultAlertPullError(CollectionOutcome.REFUSED)
        try:
            return parse_summary(json.loads(body))
        except (ValueError, RecursionError):
            raise VaultAlertPullError(CollectionOutcome.REFUSED) from None


def _bounded(response: httpx.Response) -> bytes | None:
    """Read at most the document cap; ``None`` when the answer is larger."""
    body = bytearray()
    for chunk in response.iter_bytes():
        body.extend(chunk)
        if len(body) > MAX_DOCUMENT_BYTES:
            return None
    return bytes(body)


def _utc_now() -> datetime:
    """Return the current timezone-aware UTC timestamp."""
    return datetime.now(tz=UTC)


def fresh_counts(
    state: VaultAlertState,
    handed: dict[str, int],
) -> Counter[str]:
    """Return what *state* raised above both the handed-on and acked marks."""
    fresh: Counter[str] = Counter()
    for kind, raised in state.raised.items():
        floor = max(handed.get(kind, 0), state.acknowledged.get(kind, 0))
        if raised > floor:
            fresh[kind] = raised - floor
    return fresh


class VaultAlertCollector:
    """Pull owed vault alerts into the relay; mark, then acknowledge.

    Not thread-safe: the one scheduler thread calls :meth:`collect`, the
    relay's ``flush``, then :meth:`commit`, in that order.
    """

    def __init__(
        self,
        store: ProvisioningStore,
        endpoint: VaultAlertEndpoint,
        targets: Callable[[], Sequence[VaultAlertTarget]],
        *,
        utc_clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        """Bind the durable store, the endpoint, and the running-target source."""
        self._store = store
        self._endpoint = endpoint
        self._targets = targets
        self._utc_clock = utc_clock
        self._staged: list[tuple[VaultAlertTarget, Acknowledgement]] = []

    def collect(self, sink: Callable[[tuple[str, ...]], None]) -> int:
        """Read every running vault and hand its new alerts to *sink*.

        Args:
            sink: The relay's ``collect``: closed kinds, one per alert.

        Returns:
            How many new alerts were handed on.
        """
        self._staged = []
        handed = 0
        for target in self._targets():
            handed += self._collect_one(target, sink)
        return handed

    def commit(self) -> None:
        """Raise the durable marks, then acknowledge each vault read this pass.

        Call only after the relay's flush, which has either delivered what was
        handed on or retained it durably.
        """
        staged, self._staged = self._staged, []
        for target, acknowledgement in staged:
            allocation = target.provider_allocation_id
            self._store.record_vault_alert_marks(
                allocation, acknowledgement.epoch, acknowledgement.through
            )
            try:
                after = self._endpoint.acknowledge(target, acknowledgement)
            except VaultAlertPullError as exc:
                _LOGGER.warning(
                    "vault alert acknowledgement failed outcome=%s",
                    exc.outcome.value,
                )
                continue
            self._store.record_vault_alert_check(
                allocation,
                CollectionOutcome.COLLECTED,
                now=self._utc_clock(),
                owed={} if after is None else after.owed,
            )

    def _collect_one(
        self,
        target: VaultAlertTarget,
        sink: Callable[[tuple[str, ...]], None],
    ) -> int:
        """Read one vault; hand on what is new; stage its acknowledgement."""
        allocation = target.provider_allocation_id
        try:
            state = self._endpoint.fetch(target)
        except VaultAlertPullError as exc:
            self._store.record_vault_alert_check(
                allocation, exc.outcome, now=self._utc_clock()
            )
            _LOGGER.warning("vault alerts not collected outcome=%s", exc.outcome.value)
            return 0
        if state is None:
            self._store.record_vault_alert_check(
                allocation, CollectionOutcome.COLLECTED, now=self._utc_clock(), owed={}
            )
            return 0
        fresh = fresh_counts(
            state, self._store.vault_alert_marks(allocation, state.epoch)
        )
        for kind, count in sorted(fresh.items()):
            # Distinct from fleet_cli's "fleet alert kind=" line: the receiver
            # sees only the kind, the operator's log says where it came from.
            _LOGGER.warning("vault alert kind=%s count=%d", kind, count)
        if fresh:
            sink(tuple(sorted(fresh.elements())))
        self._store.record_vault_alert_check(
            allocation,
            CollectionOutcome.COLLECTED,
            now=self._utc_clock(),
            owed=state.owed,
        )
        if state.owed:
            self._staged.append(
                (target, Acknowledgement(state.epoch, dict(state.raised)))
            )
        return fresh.total()


class RuntimeSecretReader(Protocol):
    """Read an allocation's existing runtime bundle; never issue one."""

    def issue(
        self,
        activation_id: str,
        consumer_identity: str,
        *,
        requester_identity: str = "",
    ) -> FlyRuntimeSecrets:
        """Return the already-issued bundle, or raise ``ProviderError``."""


class DeferredSecretReader:
    """Open the runtime-secret records on first use, not at composition.

    The pilot's worker and router already refuse a bad state directory or
    master key at start; this reader is consulted only once a pass has a
    running vault to read, and turns a reader it cannot open into the same
    refusal an unknown activation gets, so the pass reads no vault rather
    than stopping the scheduler.
    """

    def __init__(self, factory: Callable[[], RuntimeSecretReader]) -> None:
        """Bind the reader *factory*; nothing is opened yet."""
        self._factory = factory
        self._reader: RuntimeSecretReader | None = None

    def issue(
        self,
        activation_id: str,
        consumer_identity: str,
        *,
        requester_identity: str = "",
    ) -> FlyRuntimeSecrets:
        """Open the reader once, then delegate to it."""
        if self._reader is None:
            try:
                self._reader = self._factory()
            except ValueError:
                raise ProviderError(
                    FailureReason.PROVIDER_REJECTED, retryable=False
                ) from None
        return self._reader.issue(
            activation_id, consumer_identity, requester_identity=requester_identity
        )


@dataclass(frozen=True, slots=True)
class FlyVaultAlertTargets:
    """The running managed vaults, addressed by their app's 6PN name.

    Each vault app holds exactly one Machine, so ``<app>.internal`` names it;
    Fly's private DNS answers only for a started Machine, and a direct 6PN
    dial never wakes a stopped one.  Only allocations the latest fleet pass
    observed running are offered, so a stopped vault costs no timeout.
    """

    store: ProvisioningStore = field(repr=False)
    secrets: RuntimeSecretReader = field(repr=False)
    app_prefix: str
    port: int = VAULT_ALERTS_PORT

    def __post_init__(self) -> None:
        """Refuse an app prefix the provisioning driver could not have used."""
        if _APP_PREFIX_RE.fullmatch(self.app_prefix) is None:
            raise ValueError("vault alert app prefix is invalid")

    def __call__(self) -> tuple[VaultAlertTarget, ...]:
        """Return one target per running, routable allocation."""
        running = self.store.running_allocation_ids()
        targets: list[VaultAlertTarget] = []
        for allocation in self.store.list_routable_allocations():
            if allocation.provider_allocation_id not in running:
                continue
            target = self._target(allocation)
            if target is not None:
                targets.append(target)
        return tuple(targets)

    def _target(self, allocation: RoutableAllocation) -> VaultAlertTarget | None:
        """Build one target from the stored bundle, or ``None`` when unusable."""
        match = _FLY_ALLOCATION_RE.fullmatch(allocation.provider_allocation_id)
        if match is None:
            return None
        try:
            secrets = self.secrets.issue(
                allocation.activation_id,
                allocation.consumer_identity,
                requester_identity=allocation.requester_identity,
            )
        except ProviderError:
            _LOGGER.warning("vault alert target has no usable runtime bundle")
            return None
        return VaultAlertTarget(
            allocation.provider_allocation_id,
            f"http://{self.app_prefix}-{match.group(1)}.internal:{self.port}",
            secrets.replay_state,
            secrets.consumer_credential,
        )
