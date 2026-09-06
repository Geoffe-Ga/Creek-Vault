"""Both network surfaces reach the same verdict on the same credential (#1267).

Creek serves two networks from one token registry: the MCP transport, whose
admission gate is the SDK's ``BearerAuthBackend.authenticate``, and ``/v1``,
whose gate is :class:`creek_mcp.httpapi.auth.BearerAuthMiddleware`. Two gates
implementing one rule is the drift the ``/v1`` epic exists to prevent, and it
had already happened: the SDK refused a credential past its ``expires_at``
while ``/v1`` never read the field, so one bearer was ``401`` on one network
and ``200`` on the other.

**Measured against the real backend, never a stand-in.** The MCP column calls
``BearerAuthBackend(token_verifier=NamedConsumerVerifier(v)).authenticate(...)``.
That composition is production-accurate but assembled from two places: the SDK
wires ``BearerAuthBackend(self._token_verifier)`` in
``mcp.server.fastmcp.server``, and Creek supplies the
:class:`~creek_mcp.remote_auth.NamedConsumerVerifier` wrap in
:func:`creek_mcp.server.build_server`. Measuring the wrapper *alone* would be
measuring something that checks the name and not the expiry, and would report
the MCP transport as broken when it is not.

It measures **authentication only**, not ``RequireAuthMiddleware``'s subsequent
scope check. Admission is the question the two gates answer differently; the
scope requirement is identical on both and is pinned elsewhere.

**An expected-verdict table, never bare agreement.** ``mcp == v1`` is satisfied
by two surfaces that are wrong together, and had the SDK not enforced expiry
that is exactly what this module would have certified. Every row therefore
states what each surface *must* answer, and the two rows where they are
allowed to differ state why.

**The divergences are one-directional and asserted as such.** Where the table
disagrees, ``/v1`` is the stricter surface: it refuses a credential the MCP
transport would serve, never the reverse. That invariant is checked
independently of the table, so a future row cannot record a leak as expected.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Final

import pytest
from mcp.server.auth.middleware.bearer_auth import BearerAuthBackend
from starlette.requests import HTTPConnection

from creek_mcp.api.models import CONTRACT_MINOR
from creek_mcp.remote_auth import ConsumerTokenVerifier, NamedConsumerVerifier
from tests.v1_api_support import (
    CAPABILITIES_PATH,
    CONSUMER,
    CONTRACT_VERSION_HEADER,
    EPOCH_ZERO,
    FAR_FUTURE,
    LONG_PAST,
    STRONG_TOKEN,
    UNKNOWN_TOKEN,
    client,
    seed_vault,
    stamped,
    verifier,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

_OK_STATUS: Final[int] = 200
"""The status a served ``GET /v1/capabilities`` returns."""


@pytest.fixture
def vault(tmp_path: Path) -> Iterator[Path]:
    """Yield a freshly seeded vault for the ``/v1`` column."""
    yield seed_vault(tmp_path)


def _live() -> ConsumerTokenVerifier:
    """Return the ordinary production verifier over the suite's consumers.

    Returns:
        The suite's :class:`ConsumerTokenVerifier`.
    """
    return verifier()


def _stamped(
    expires_at: int | None, *, client_id: str = CONSUMER
) -> Callable[[], ConsumerTokenVerifier]:
    """Return a factory for a verifier stamping *expires_at* and *client_id*.

    A factory rather than an instance, so each column builds its own and
    neither can observe state the other left behind.

    Args:
        expires_at: The instant to stamp, or ``None`` for no expiry.
        client_id: The consumer name to stamp.

    Returns:
        A zero-argument factory.
    """

    def _build() -> ConsumerTokenVerifier:
        """Build the stamped verifier.

        Returns:
            The configured verifier.
        """
        return stamped(expires_at, client_id=client_id)

    return _build


_CASES: Final[
    tuple[tuple[str, Callable[[], ConsumerTokenVerifier], str, bool, bool], ...]
] = (
    (
        "a live credential",
        _live,
        f"Bearer {STRONG_TOKEN}",
        True,
        True,
    ),
    (
        "an unconfigured token",
        _live,
        f"Bearer {UNKNOWN_TOKEN}",
        False,
        False,
    ),
    (
        "an expired credential",
        _stamped(LONG_PAST),
        f"Bearer {STRONG_TOKEN}",
        False,
        False,
    ),
    (
        "an unexpired credential",
        _stamped(FAR_FUTURE),
        f"Bearer {STRONG_TOKEN}",
        True,
        True,
    ),
    (
        "a credential with no expiry",
        _stamped(None),
        f"Bearer {STRONG_TOKEN}",
        True,
        True,
    ),
    (
        "an expiry stamped at epoch zero",
        _stamped(EPOCH_ZERO),
        f"Bearer {STRONG_TOKEN}",
        False,
        True,
    ),
    (
        "a lowercase scheme",
        _live,
        f"bearer {STRONG_TOKEN}",
        False,
        True,
    ),
    (
        "a credential naming nobody",
        _stamped(FAR_FUTURE, client_id=""),
        f"Bearer {STRONG_TOKEN}",
        False,
        False,
    ),
)
"""``(name, verifier factory, Authorization header, /v1 admits, MCP admits)``.

Two rows disagree, both with ``/v1`` the stricter surface and both deliberate:
``an expiry stamped at epoch zero`` (see
:data:`tests.v1_api_support.EPOCH_ZERO`) and ``a lowercase scheme``, which
RFC 7235 makes case-insensitive and which ``creek_mcp.httpapi.auth`` refuses on
purpose because every normalisation step is a step where two equal-looking
headers stop being equal.

**The two ``on_mcp=True`` divergence cells pin third-party behaviour.**
``pyproject.toml`` allows ``mcp>=1.28.1,<2.0.0`` and the lock resolves 1.29.0,
whose backend guards on truthiness and lowercases the scheme. An upgrade in
which the SDK tightens either rule flips those cells and reddens this table.
That is the intended alarm and not a false one — it means the two gates now
agree where they used to differ — but the fix is to update the row and this
note, never to relax the assertion.
"""

_CASE_IDS: Final[tuple[str, ...]] = tuple(case[0] for case in _CASES)
"""Readable parametrisation ids, so a failing row names itself."""


def _v1_admits(
    vault_path: Path, build: Callable[[], ConsumerTokenVerifier], authorization: str
) -> bool:
    """Return whether ``/v1`` serves ``GET /v1/capabilities`` for that header.

    Args:
        vault_path: A seeded vault.
        build: Factory for the verifier the app is built over.
        authorization: The verbatim ``Authorization`` header value.

    Returns:
        ``True`` when the request was served.
    """
    request_headers = {
        "Authorization": authorization,
        CONTRACT_VERSION_HEADER: CONTRACT_MINOR,
    }
    with client(vault_path=vault_path, verifier=build()) as test_client:
        response = test_client.get(CAPABILITIES_PATH, headers=request_headers)
    status: int = response.status_code
    return status == _OK_STATUS


def _mcp_admits(build: Callable[[], ConsumerTokenVerifier], authorization: str) -> bool:
    """Return whether the SDK's bearer backend authenticates that header.

    Args:
        build: Factory for the verifier the backend is built over.
        authorization: The verbatim ``Authorization`` header value.

    Returns:
        ``True`` when the backend produced credentials.
    """
    backend = BearerAuthBackend(token_verifier=NamedConsumerVerifier(build()))
    scope: dict[str, Any] = {
        "type": "http",
        "headers": [(b"authorization", authorization.encode("utf-8"))],
    }
    return asyncio.run(backend.authenticate(HTTPConnection(scope))) is not None


_REQUIRED_IDS: Final[frozenset[str]] = frozenset(
    {
        "an expired credential",
        "an unexpired credential",
        "a credential with no expiry",
        "an expiry stamped at epoch zero",
    }
)
"""The four rows this module was written for (#1267).

Pinned by *identity*, not by count: deleting the expired row and adding any
other agreeing one keeps every count below green, and this module would then
be named for an expiry defect while containing no expiry case at all.
"""


def test_the_parity_matrix_has_rows_to_walk() -> None:
    """A table that silently emptied would certify parity by collecting nothing."""
    assert len(_CASES) == 8
    assert len(set(_CASE_IDS)) == len(_CASES)
    assert set(_CASE_IDS) >= _REQUIRED_IDS


def test_the_matrix_states_both_agreement_and_disagreement() -> None:
    """The table is non-trivial in both directions.

    A table where every row agreed could be satisfied by two surfaces sharing
    one bug; a table where none did would not be measuring parity at all.
    """
    agreeing = [case for case in _CASES if case[3] == case[4]]
    differing = [case for case in _CASES if case[3] != case[4]]
    assert len(agreeing) == 6
    assert len(differing) == 2


_PRESENTATIONS: Final[
    tuple[tuple[str, Callable[[], ConsumerTokenVerifier], str], ...]
] = tuple((name, build, authorization) for name, build, authorization, _, _ in _CASES)
"""The same rows without their expected verdicts, for the directional invariant."""


@pytest.mark.parametrize(
    ("build", "authorization", "on_v1", "on_mcp"),
    [case[1:] for case in _CASES],
    ids=_CASE_IDS,
)
def test_each_surface_answers_as_the_table_says(
    vault: Path,
    build: Callable[[], ConsumerTokenVerifier],
    authorization: str,
    on_v1: bool,
    on_mcp: bool,
) -> None:
    """Each surface is measured against its own expected verdict, not the other's.

    Args:
        vault: A seeded vault.
        build: Factory for the verifier both columns are built over.
        authorization: The verbatim ``Authorization`` header value.
        on_v1: Whether ``/v1`` must serve it.
        on_mcp: Whether the MCP backend must authenticate it.
    """
    measured = (
        _v1_admits(vault, build, authorization),
        _mcp_admits(build, authorization),
    )
    assert measured == (on_v1, on_mcp)


@pytest.mark.parametrize(
    ("build", "authorization"),
    [case[1:] for case in _PRESENTATIONS],
    ids=_CASE_IDS,
)
def test_v1_is_never_more_permissive_than_the_mcp_surface(
    vault: Path,
    build: Callable[[], ConsumerTokenVerifier],
    authorization: str,
) -> None:
    """Measured, not read off the table, so a new row cannot record a leak.

    ``/v1`` is the surface reachable by any HTTP client. Where the two gates
    differ it must be the one that refuses; a row admitting on ``/v1`` what the
    MCP transport refuses would be a credential the scope-checked surface
    rejected and the other served.

    Args:
        vault: A seeded vault.
        build: Factory for the verifier both columns are built over.
        authorization: The verbatim ``Authorization`` header value.
    """
    on_v1 = _v1_admits(vault, build, authorization)
    on_mcp = _mcp_admits(build, authorization)
    assert not (on_v1 and not on_mcp)
