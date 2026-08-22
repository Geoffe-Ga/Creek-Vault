"""``GET /v1/wheel`` over the shared wheel tool (#1076).

The read vertical, and the one whose whole risk is *vocabulary* rather than
behaviour. Creek's wheel is a frequency distribution over the classified corpus:
how many admitted fragments sit at each APTITUDE frequency, and each frequency's
share of the classified total. Adepthood's Map validates a different ten-member
shape — per-stage aspect *fullness* from its own 36-week curriculum. Both have
ten members, which is a coincidence of cardinality and not a shared meaning, and
serving one under the other's name is how a Map renders confidently wrong
numbers.

So this module asserts the boundary as well as the numbers: the response is
``wheel_tool``'s tally verbatim, the ten canonical frequency names come off
:data:`creek.generate.indexes.CANONICAL_FREQUENCY_NAMES`, and no stage or aspect
vocabulary appears anywhere. The projection into Adepthood's domain is
Adepthood's, and stays there.

The aggregate is also the one shape where a privacy failure would be silent: a
count is not a body, so an above-ceiling fragment leaking into the tally shows up
as a number being one larger rather than as text appearing where it should not.
:func:`test_an_open_caller_never_counts_a_personal_fragment` is therefore an
equality against a *known* tally, not a "greater than zero" check.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

import frontmatter
import pytest

from creek.generate.indexes import CANONICAL_FREQUENCY_NAMES
from creek_mcp.api.models import ERROR_MESSAGES, ErrorCode
from creek_mcp.audit import MCP_AUDIT_RELPATH
from creek_mcp.httpapi import vault as vault_module
from creek_mcp.httpapi import wheel as wheel_module
from creek_mcp.httpapi.wheel import wheel_refusal_code
from creek_mcp.tier_ceiling import TierCeiling
from creek_mcp.tools.wheel import wheel_tool
from tests.adapter_parity import assert_parity, http_outcome, mcp_outcome
from tests.v1_api_support import (
    CONSUMER,
    WHEEL_PATH,
    client,
    contains_a_path,
    envelope,
    headers,
    seed_vault,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    import httpx

_OK: Final[int] = 200
_INVALID: Final[int] = 422
_INTERNAL: Final[int] = 500
_UNAVAILABLE: Final[int] = 503

_ENVELOPE_KEYS: Final[frozenset[str]] = frozenset({"code", "message", "request_id"})
"""Every key an error body may carry. A fourth is a leak channel."""

_CODES: Final[tuple[str, ...]] = tuple(
    frequency.value for frequency in CANONICAL_FREQUENCY_NAMES
)
"""The ten canonical frequency codes, in the one published order."""

_SHARE_TOLERANCE: Final[float] = 1e-9
"""How far the ten shares may sum from ``1.0``. Float addition, not slack."""

_TITLE: Final[str] = "zz-synthetic-fragment-title-1076-zz"
"""A fragment title distinctive enough that any echo of it is unmissable."""

_BODY: Final[str] = "zz-synthetic-fragment-body-1076-zz"
"""A fragment body, likewise."""

_SUCCESS_KEYS: Final[tuple[str, ...]] = (
    "tier_ceiling",
    "total_classified",
    "unclassified",
    "wheel",
)
"""The success facts the parity harness compares for this vertical."""


@pytest.fixture
def vault(tmp_path: Path) -> Iterator[Path]:
    """Yield a freshly seeded vault for the HTTP adapter."""
    yield seed_vault(tmp_path / "http")


@pytest.fixture
def mcp_vault(tmp_path: Path) -> Iterator[Path]:
    """Yield a second seeded vault, so the two adapters never share a trail."""
    yield seed_vault(tmp_path / "mcp")


# --------------------------------------------------------------------------- #
# Corpus + request helpers
# --------------------------------------------------------------------------- #


def _write_fragment(
    vault_path: Path,
    *,
    frag_id: str,
    primary: str = "F1",
    privacy_tier: str = "open",
) -> None:
    """Write one classified fragment under ``01-Fragments/Notes``.

    Args:
        vault_path: The vault root.
        frag_id: The fragment id and file stem.
        primary: The primary frequency code, or an unrecognised string.
        privacy_tier: The tier to stamp.
    """
    stamped = datetime(2026, 5, 1, tzinfo=UTC).isoformat()
    metadata: dict[str, Any] = {
        "type": "fragment",
        "id": frag_id,
        "title": _TITLE,
        "created": stamped,
        "ingested": stamped,
        "source": {"platform": "journal", "author": "self"},
        "frequency": {"primary": primary, "secondary": []},
        "privacy_tier": privacy_tier,
        "eddies": [],
    }
    folder = vault_path / "01-Fragments" / "Notes"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{frag_id}.md").write_text(
        frontmatter.dumps(frontmatter.Post(content=_BODY, **metadata)),
        encoding="utf-8",
    )


def _seed_corpus(vault_path: Path) -> None:
    """Write the three-fragment OPEN corpus the tracer describes.

    Args:
        vault_path: The vault root.
    """
    _write_fragment(vault_path, frag_id="a", primary="F1")
    _write_fragment(vault_path, frag_id="b", primary="F1")
    _write_fragment(vault_path, frag_id="c", primary="F5")


def _get(vault_path: Path, *, ceiling: str | None = None) -> httpx.Response:
    """Request the wheel and return the raw response.

    Args:
        vault_path: The vault the app is built over.
        ceiling: The declared tier ceiling, or ``None`` to send no header.

    Returns:
        The response.
    """
    with client(vault_path=vault_path) as test_client:
        return test_client.get(WHEEL_PATH, headers=headers(ceiling=ceiling))


# --------------------------------------------------------------------------- #
# Shape: ten entries, canonical names, one order
# --------------------------------------------------------------------------- #


def test_a_populated_vault_returns_all_ten_canonical_frequencies(
    vault: Path,
) -> None:
    """Ten entries, the canonical names, and the counts the corpus implies.

    Args:
        vault: A seeded vault.
    """
    _seed_corpus(vault)
    body = envelope(_get(vault))

    assert body["status"] == "ok"
    assert list(body["wheel"]) == list(_CODES)
    assert body["wheel"]["F1"] == {
        "name": CANONICAL_FREQUENCY_NAMES[next(iter(CANONICAL_FREQUENCY_NAMES))],
        "count": 2,
        "share": pytest.approx(2 / 3),
    }
    assert body["wheel"]["F5"]["count"] == 1
    assert body["wheel"]["F7"]["count"] == 0
    assert body["total_classified"] == 3


def test_the_order_is_the_same_on_every_call(vault: Path) -> None:
    """Determinism, asserted across calls rather than within one.

    A single call cannot tell a fixed order from an accidental one. Ten
    declared model fields are what make the emission order the canonical one;
    a mapping would have made it whatever the tally happened to insert first.

    Args:
        vault: A seeded vault.
    """
    _seed_corpus(vault)
    orders = {tuple(envelope(_get(vault))["wheel"]) for _ in range(3)}

    assert orders == {_CODES}


def test_the_shares_sum_to_one_when_anything_is_classified(vault: Path) -> None:
    """The ten shares are a distribution, not ten independent ratios.

    Args:
        vault: A seeded vault.
    """
    _seed_corpus(vault)
    body = envelope(_get(vault))

    total = sum(slice_["share"] for slice_ in body["wheel"].values())
    assert abs(total - 1.0) < _SHARE_TOLERANCE


@pytest.mark.parametrize("populated", [False, True], ids=["empty", "missing"])
def test_an_empty_or_missing_corpus_is_an_all_zero_wheel_and_a_200(
    vault: Path, populated: bool
) -> None:
    """A new vault is a legitimate state, not a 404 and not an error.

    Answering an unclassified corpus with an error would make every client
    treat "new" as "broken" — the exact collapse epic #1071 exists to stop.

    Args:
        vault: A seeded vault.
        populated: Whether to create the fragments directory at all.
    """
    if populated:
        (vault / "01-Fragments").mkdir(parents=True, exist_ok=True)
    response = _get(vault)
    body = envelope(response)

    assert response.status_code == _OK
    assert body["total_classified"] == 0
    assert list(body["wheel"]) == list(_CODES)
    assert all(slice_["count"] == 0 for slice_ in body["wheel"].values())
    assert all(slice_["share"] == 0.0 for slice_ in body["wheel"].values())


# --------------------------------------------------------------------------- #
# Ceiling filtering — the silent failure
# --------------------------------------------------------------------------- #


def test_an_open_caller_never_counts_a_personal_fragment(vault: Path) -> None:
    """Above-ceiling fragments are excluded from the tally, exactly.

    An equality against a known tally, not a "greater than zero" check: a
    count is not a body, so a leak here shows up only as a number being one
    larger than it should be.

    Args:
        vault: A seeded vault.
    """
    _write_fragment(vault, frag_id="open-one", primary="F1")
    _write_fragment(
        vault, frag_id="personal-one", primary="F1", privacy_tier="personal"
    )

    body = envelope(_get(vault, ceiling="open"))

    assert body["wheel"]["F1"]["count"] == 1
    assert body["total_classified"] == 1
    assert body["tier_ceiling"] == "open"


def test_a_personal_caller_counts_both(vault: Path) -> None:
    """The converse, so the exclusion above is about the ceiling and not the file.

    Args:
        vault: A seeded vault.
    """
    _write_fragment(vault, frag_id="open-one", primary="F1")
    _write_fragment(
        vault, frag_id="personal-one", primary="F1", privacy_tier="personal"
    )

    body = envelope(_get(vault, ceiling="personal"))

    assert body["wheel"]["F1"]["count"] == 2
    assert body["total_classified"] == 2


def test_a_remote_caller_declaring_an_intimate_ceiling_is_refused_at_the_edge(
    vault: Path,
) -> None:
    """No intimate-inclusive tally is obtainable over ``/v1``, at any status.

    Args:
        vault: A seeded vault.
    """
    _seed_corpus(vault)
    response = _get(vault, ceiling="intimate")

    assert response.status_code == _INVALID
    assert envelope(response)["code"] == ErrorCode.INVALID_REQUEST.value
    assert "wheel" not in response.text


# --------------------------------------------------------------------------- #
# The fail-closed frequency branch
# --------------------------------------------------------------------------- #


def test_an_unclassified_fragment_is_counted_apart_from_the_classified_total(
    vault: Path,
) -> None:
    """A fragment nobody has classified yet is ``unclassified``, not a frequency.

    :attr:`creek.models.Frequency.UNCLASSIFIED` is a *member* of the enum but
    not a member of :data:`~creek.generate.indexes.CANONICAL_FREQUENCY_NAMES`,
    which is what makes this the reachable version of the fail-closed branch:
    the fragment loads, is admitted, and is deliberately kept out of the
    denominator so the ten shares describe the classified corpus rather than
    the whole vault.

    Args:
        vault: A seeded vault.
    """
    _write_fragment(vault, frag_id="known", primary="F1")
    _write_fragment(vault, frag_id="pending", primary="unclassified")

    body = envelope(_get(vault))

    assert body["unclassified"] == 1
    assert body["total_classified"] == 1
    assert sum(slice_["count"] for slice_ in body["wheel"].values()) == 1
    assert body["wheel"]["F1"]["share"] == 1.0


def test_an_invented_frequency_never_reaches_the_tally_at_all(vault: Path) -> None:
    """#1076's fifth acceptance criterion describes an unreachable branch.

    The issue asks for a fragment with an "unrecognised frequency" to land in
    ``unclassified`` via ``_frequency_counts``' ``except ValueError`` branch
    (``tools/wheel.py:53-57``). It cannot get there. ``Fragment`` validates
    ``frequency.primary`` against :class:`creek.models.Frequency` at load, so a
    file carrying ``F11`` is schema-invalid and
    :func:`creek.vault.reader.iter_vault_fragments` drops it *before*
    ``wheel_tool`` sees anything — it is absent from the wheel, from
    ``unclassified``, and from ``total_classified`` alike.

    That is the safer of the two behaviours and is worth pinning rather than
    "fixing": an eleventh frequency is a change to the shared ontology
    vocabulary, and counting a file that claims one would let a producer expand
    the published enum by writing a fragment. The genuinely reachable
    fail-closed path is the test above.

    Args:
        vault: A seeded vault.
    """
    _write_fragment(vault, frag_id="known", primary="F1")
    _write_fragment(vault, frag_id="invented", primary="F11")

    body = envelope(_get(vault))

    assert body["total_classified"] == 1
    assert body["unclassified"] == 0
    assert "F11" not in body["wheel"]


# --------------------------------------------------------------------------- #
# The projection boundary
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "borrowed", ["stage_number", "aspect", "fullness", "aspects", "stage"]
)
def test_the_response_borrows_no_curriculum_vocabulary(
    vault: Path, borrowed: str
) -> None:
    """Creek publishes the corpus fact it owns and invents no stage/aspect words.

    Adepthood's ``{aspects: [{stage_number, aspect, fullness}]}`` is a different
    concept that happens to have ten members. Its projection is owned and tested
    in Adepthood (their #1937); adopting its vocabulary here would make a
    frequency tally look like curriculum progress to every reader of the wire.

    Args:
        vault: A seeded vault.
        borrowed: The forbidden term.
    """
    _seed_corpus(vault)

    assert borrowed not in _get(vault).text


def test_no_fragment_title_body_or_id_reaches_the_wire(vault: Path) -> None:
    """The wheel is aggregate-only.

    Args:
        vault: A seeded vault.
    """
    _seed_corpus(vault)
    text = _get(vault).text

    assert _TITLE not in text
    assert _BODY not in text
    assert not contains_a_path(text)


# --------------------------------------------------------------------------- #
# Audit
# --------------------------------------------------------------------------- #


def test_the_audit_record_carries_the_bearer_consumer(vault: Path) -> None:
    """The verified token's ``client_id`` reaches the trail, not a default.

    Args:
        vault: A seeded vault.
    """
    _seed_corpus(vault)
    _get(vault)

    log = (vault / MCP_AUDIT_RELPATH).read_text(encoding="utf-8")
    assert f'"consumer": "{CONSUMER}"' in log
    assert '"tool": "creek.wheel"' in log


# --------------------------------------------------------------------------- #
# Refusal mapping
# --------------------------------------------------------------------------- #


def test_any_wheel_refusal_fails_closed_to_internal_error() -> None:
    """``wheel_tool`` has no refusal branch, so an adapter must invent none.

    The mapping exists because the parity harness needs one and because a
    future refusal must not be narrated by an adapter that has never seen it.
    ``internal_error`` asserts nothing about the vault, which is the only honest
    answer to a refusal this adapter does not understand.
    """
    assert wheel_refusal_code("anything at all") is ErrorCode.INTERNAL_ERROR


def test_a_tool_refusal_becomes_a_500_carrying_none_of_its_reason(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refusal the adapter has never seen is a ``500`` that narrates nothing.

    ``wheel_tool`` has no refusal branch today, so the only way to reach the
    adapter's non-``ok`` arm is to substitute the tool — and the property under
    test is the *projection*, not how the tool got there. A structured refusal
    can carry a vault path in its ``reason``; the wire carries the constant
    message for ``internal_error`` and nothing else.

    Args:
        vault: A seeded vault.
        monkeypatch: Substitutes the tool for a refusing one.
    """
    leak = "/private/vault/01-Fragments/Notes/zz-secret-name-zz.md"

    def _refusing(**_kwargs: object) -> dict[str, Any]:
        """Refuse the way a future ``wheel_tool`` might.

        Args:
            **_kwargs: The real tool's keyword arguments, unused.

        Returns:
            A structured refusal whose reason names a path.
        """
        return {
            "status": "refused",
            "tool": "creek.wheel",
            "tier_ceiling": "open",
            "reason": f"tally failed: could not read {leak}",
        }

    monkeypatch.setattr(wheel_module, "wheel_tool", _refusing)
    response = _get(vault)
    body = envelope(response)

    assert response.status_code == _INTERNAL
    assert set(body) == _ENVELOPE_KEYS
    assert body["code"] == ErrorCode.INTERNAL_ERROR.value
    assert body["message"] == ERROR_MESSAGES[ErrorCode.INTERNAL_ERROR]
    assert leak not in response.text
    assert "zz-secret-name-zz" not in response.text


# --------------------------------------------------------------------------- #
# Projection: a tally that does not fit the contract
# --------------------------------------------------------------------------- #


def _ok_tally(**overrides: Any) -> dict[str, Any]:
    """Return a well-formed ``wheel_tool`` result with *overrides* applied.

    Args:
        **overrides: Keys to replace on the canonical all-zero result.

    Returns:
        The result dict the adapter would normally project onto
        :class:`~creek_mcp.api.models.WheelResponse`.
    """
    tally: dict[str, Any] = {
        "status": "ok",
        "tool": "creek.wheel",
        "tier_ceiling": "open",
        "total_classified": 0,
        "unclassified": 0,
        "wheel": {
            code.value: {"name": name, "count": 0, "share": 0.0}
            for code, name in CANONICAL_FREQUENCY_NAMES.items()
        },
    }
    tally.update(overrides)
    return tally


_ELEVENTH: Final[str] = "F11"
"""A frequency the published vocabulary does not contain."""

_MALFORMED: Final[tuple[tuple[str, dict[str, Any], str], ...]] = (
    (
        "eleventh-frequency",
        _ok_tally(
            wheel={
                **_ok_tally()["wheel"],
                _ELEVENTH: {"name": "Invention", "count": 7, "share": 1.0},
            }
        ),
        _ELEVENTH,
    ),
    (
        "missing-total",
        {key: value for key, value in _ok_tally().items() if key != "total_classified"},
        "total_classified",
    ),
    ("unparseable-total", _ok_tally(total_classified="seventeen"), "seventeen"),
    ("unknown-ceiling", _ok_tally(tier_ceiling="cosmic"), "cosmic"),
    ("null-unclassified", _ok_tally(unclassified=None), "null"),
)
"""``(id, tool result, token that must not echo)`` — one row per caught type.

In order: :class:`pydantic.ValidationError` (``extra="forbid"`` rejects an
eleventh member), :class:`KeyError` (an absent field), :class:`ValueError`
(twice — an uncoercible integer, and a ceiling outside
:class:`~creek_mcp.api.models.WireTierCeiling`) and :class:`TypeError`
(``int(None)``). Each is a member of the tuple ``_render`` catches, so a row
that stopped being refused would mean a partially-projected wheel had reached
the wire.
"""


@pytest.mark.parametrize(
    ("tally", "forbidden"),
    [(row[1], row[2]) for row in _MALFORMED],
    ids=[row[0] for row in _MALFORMED],
)
def test_a_tally_that_does_not_fit_the_contract_is_refused_not_reshaped(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
    tally: dict[str, Any],
    forbidden: str,
) -> None:
    """A result the response model rejects becomes a ``500``, never a partial 200.

    The ``eleventh-frequency`` row is the one that matters most and is the
    reason ``WheelFrequencies`` is ten declared fields rather than a mapping: an
    extra member is a change to the shared ontology vocabulary, so a producer
    must not be able to widen the published enum by emitting one. The other
    rows pin that the same closed refusal covers a missing field, an uncoercible
    count and a ceiling outside the wire vocabulary — the adapter has no
    salvage path that would serve some of the tally and quietly drop the rest.

    Args:
        vault: A seeded vault.
        monkeypatch: Substitutes the tool for one returning *tally*.
        tally: A result that cannot be projected onto ``WheelResponse``.
        forbidden: A token from *tally* that must not reach the wire.
    """

    def _malformed(**_kwargs: object) -> dict[str, Any]:
        """Return the parametrized result.

        Args:
            **_kwargs: The real tool's keyword arguments, unused.

        Returns:
            The malformed tally under test.
        """
        return tally

    monkeypatch.setattr(wheel_module, "wheel_tool", _malformed)
    response = _get(vault)
    body = envelope(response)

    assert response.status_code == _INTERNAL
    assert set(body) == _ENVELOPE_KEYS
    assert body["code"] == ErrorCode.INTERNAL_ERROR.value
    assert body["message"] == ERROR_MESSAGES[ErrorCode.INTERNAL_ERROR]
    assert forbidden not in response.text


# --------------------------------------------------------------------------- #
# Vault resolution
# --------------------------------------------------------------------------- #


def test_an_unreadable_configuration_is_unavailable_and_names_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No configured vault and no readable config is ``503 unavailable``.

    With ``vault_path=None`` the route resolves through
    :func:`~creek_mcp.httpapi.vault.configured_vault`, which degrades an
    unreadable ``creek_config.yaml`` to ``None``. The route has to refuse
    *before* calling the tool: ``wheel_tool`` over an unresolved vault answers
    an all-zero wheel, which would render a broken installation as an empty
    corpus — a ``200`` saying "you have written nothing" to someone whose
    config is simply unopenable.

    ``unavailable`` rather than ``temporarily_unavailable`` because its retry
    disposition is ``retry_after_operator_action``: backing off will not repair
    a config file.

    Args:
        monkeypatch: Substitutes the config loader for a raising one.
    """
    leak = "/private/operator/00-Creek-Meta/creek_config.yaml"

    def _unreadable() -> object:
        """Fail the way an unopenable configuration file fails.

        Returns:
            Never returns.

        Raises:
            OSError: Always, naming a path that must not travel.
        """
        raise OSError(leak)

    monkeypatch.setattr(vault_module, "load_config", _unreadable)
    with client(vault_path=None) as test_client:
        response = test_client.get(WHEEL_PATH, headers=headers())
    body = envelope(response)

    assert response.status_code == _UNAVAILABLE
    assert set(body) == _ENVELOPE_KEYS
    assert body["code"] == ErrorCode.UNAVAILABLE.value
    assert body["message"] == ERROR_MESSAGES[ErrorCode.UNAVAILABLE]
    assert leak not in response.text
    assert not contains_a_path(response.text)


# --------------------------------------------------------------------------- #
# HTTP <-> MCP parity
# --------------------------------------------------------------------------- #


_SCENARIOS: Final[tuple[tuple[str, str], ...]] = (
    ("open-ceiling", "open"),
    ("personal-ceiling", "personal"),
)
"""``(name, declared ceiling)`` rows run through both adapters."""


@pytest.mark.parametrize(
    ("name", "ceiling"), _SCENARIOS, ids=[row[0] for row in _SCENARIOS]
)
def test_http_and_mcp_agree_on_counts_shares_and_ordering(
    vault: Path, mcp_vault: Path, name: str, ceiling: str
) -> None:
    """The same mixed-tier vault, both adapters, identical tally and order.

    Args:
        vault: The HTTP adapter's vault.
        mcp_vault: The MCP adapter's vault.
        name: The scenario name, surfaced in a divergence message.
        ceiling: The declared tier ceiling.
    """
    for root in (vault, mcp_vault):
        _seed_corpus(root)
        _write_fragment(root, frag_id="p", primary="F5", privacy_tier="personal")
        _write_fragment(root, frag_id="bogus", primary="F11")

    over_http = _get(vault, ceiling=ceiling)
    over_mcp = wheel_tool(
        vault_path=mcp_vault,
        privacy_tier_ceiling=TierCeiling(ceiling),
        consumer=CONSUMER,
    )

    assert_parity(
        name,
        http_outcome(over_http, vault, keys=_SUCCESS_KEYS),
        mcp_outcome(
            over_mcp, mcp_vault, keys=_SUCCESS_KEYS, classify=wheel_refusal_code
        ),
    )
