"""``POST /v1/classifications`` and ``POST /v1/links`` — the DoD route (#1570).

The seeding epic promises fragments land *"correctly-typed, correctly-tiered —
over the network, with no CLI and no shell access"*. Before this module, ``/v1``
could ingest and nothing else: ``creek.classify`` and ``creek.link`` were
reachable over MCP but not from any ``/v1`` contract consumer, so a
network-seeded vault stayed inert — no APTITUDE frequency, no Archetypal
Wavelength phase, no threads, no eddies.

**The privacy argument runs the opposite way from the obvious reading**, and
two characterisation tests here pin why before anything new is asserted.
:data:`creek_mcp.tier_ceiling._TIER_RANK` ranks ``unclassified`` *equal to*
``personal``, so an unclassified fragment is already admitted to a remote
consumer at ceiling ``personal``. Classification is the operation that pulls
genuinely-intimate content **out** of remote reach — it is the tightening
operation, not a widening one. Refusing to classify under a ``personal``
ceiling would preserve the exposure rather than prevent it. Both facts are
properties of code this issue did not write, so they are asserted here against
the live constants rather than assumed.

**Nothing of any fragment appears in a response.** Both synchronous results and
durable job results are counts and a method name: no fragment id, path, title,
body, or tool error. That is what makes running the *whole* vault under a
``personal`` ceiling safe — the pass reads intimate content on the host and
reports nothing of it.

``rules`` remains the model-free synchronous path. The ``llm`` job path is
allowed to construct the configured provider, but the tier-before-router
ordering and ModelRouter chokepoint remain load-bearing: the end-to-end test
below gives the classification stage a cloud provider, seeds an intimate
fragment, and observes that only the local provider receives it.
"""

from __future__ import annotations

import base64
import json
import time
from functools import partial
from threading import Event
from typing import TYPE_CHECKING, Any, Final
from uuid import UUID

import frontmatter
import pytest

from creek.classify.classify_engine import run_classify
from creek.classify.llm import LLMClassificationResult
from creek.classify.rules import FREQUENCY_SIGNALS, WAVELENGTH_PHASE_SIGNALS
from creek.config import load_vault_config
from creek.link import embeddings as embeddings_module
from creek.models import Frequency, Phase, PrivacyTier
from creek_mcp.api.models import (
    CONTRACT_MINOR,
    Capability,
    ErrorCode,
    minor_at_least,
)
from creek_mcp.httpapi import pipeline as pipeline_module
from creek_mcp.httpapi.pipeline import pipeline_refusal_code
from creek_mcp.tier_ceiling import TierCeiling, tier_sensitivity
from tests.v1_api_support import (
    CLASSIFICATIONS_PATH,
    LINKS_PATH,
    OTHER_TOKEN,
    UPLOAD_PATH,
    client,
    headers,
    seed_vault,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from starlette.testclient import TestClient

HTTP_OK: Final[int] = 200
"""The one success status either route answers with."""

HTTP_ACCEPTED: Final[int] = 202
"""What a long-running pipeline method returns once durably queued."""

HTTP_UNPROCESSABLE: Final[int] = 422
"""What a well-routed request outside the closed input schema earns."""

HTTP_CONFLICT: Final[int] = 409
"""What a client pinned below the minor that published this capability earns."""

HTTP_UNAVAILABLE: Final[int] = 503
"""What a request against a vault this server cannot read earns."""

HTTP_SERVER_FAULT: Final[int] = 500
"""What a tool success this contract cannot express earns."""

PIPELINE_MINOR: Final[str] = "0.10"
"""The contract minor ``pipeline`` was published at.

Spelled as the literal a consumer would send rather than imported, so a bump
that moved the capability's floor would surface here instead of agreeing with
itself.
"""

_F3_SIGNALS: Final[tuple[str, ...]] = ("power", "dominance", "control", "conquest")
"""Four :data:`creek.classify.rules.FREQUENCY_SIGNALS` keywords for ``F3``.

The rules classifier answers :attr:`creek.models.Frequency.UNCLASSIFIED` when
no keyword fires, and *still* reports the fragment as classified. A fixture of
neutral prose would therefore make this module's central assertion unreachable
without an LLM. These four are checked against the live table below, so the
fixture cannot drift from the classifier it is aimed at.
"""

_RISING_SIGNALS: Final[tuple[str, ...]] = (
    "emerging",
    "building",
    "growing",
    "momentum",
)
"""Four ``WAVELENGTH_PHASE_SIGNALS`` keywords for :attr:`creek.models.Phase.RISING`."""

_RECOVERY_BODY: Final[str] = (
    "ninety days of sobriety today and the walk home felt shorter, "
    "my relapse last spring is still close enough to touch"
)
"""Prose the rules tier pass reads as intimate.

Used only where the *tier* verdict is what is under test. It is never uploaded
through ``/v1`` in that role: an upload declares its own tier, so the untiered
starting state this exercises can only be built on disk.
"""


def _signal_body() -> str:
    """Return document prose that fires both rules tables.

    Returns:
        A sentence carrying four ``F3`` frequency signals and four ``rising``
        phase signals, so a ``method=rules`` run produces real values on both
        axes rather than the ``unclassified`` a keywordless body earns.
    """
    return (
        "The team took "
        + " and ".join(_F3_SIGNALS)
        + " of the release, and the mood is "
        + ", ".join(_RISING_SIGNALS)
        + " week over week.\n"
    )


def _upload_body(external_id: str, filename: str, text: str) -> dict[str, Any]:
    """Return a ``POST /v1/uploads`` body carrying *text*.

    Args:
        external_id: The consumer-side idempotency key.
        filename: The uploaded document's name; only its extension is trusted.
        text: The document's plaintext.

    Returns:
        A body that validates against ``UploadRequest``.
    """
    return {
        "filename": filename,
        "content_base64": base64.b64encode(text.encode("utf-8")).decode("ascii"),
        "external_id": external_id,
        "tier": "personal",
    }


def _fragments(vault: Path) -> list[Path]:
    """Return every fragment file written under ``01-Fragments``.

    Args:
        vault: The vault root.

    Returns:
        Sorted markdown paths.
    """
    return sorted((vault / "01-Fragments").rglob("*.md"))


def _write_fragment(vault: Path, name: str, body: str, tier: str | None) -> Path:
    """Write one fragment straight to disk, optionally untiered.

    The ``/v1`` upload route requires an explicit tier (#1497), so the
    *untiered* starting state — the one where the tier pass has something to
    derive — cannot be produced over the network at all. It is built here
    instead, which is also the honest shape: a vault seeded by any other route
    can hold one.

    Args:
        vault: The vault root.
        name: The fragment's file stem.
        body: The fragment's prose.
        tier: The ``privacy_tier`` to stamp, or ``None`` to omit it entirely.

    Returns:
        The written path.
    """
    notes = vault / "01-Fragments" / "Notes"
    notes.mkdir(parents=True, exist_ok=True)
    metadata: dict[str, Any] = {
        "type": "fragment",
        "id": f"frag-{name}",
        "title": name,
        "source": {"platform": "markdown", "author": "self"},
    }
    if tier is not None:
        metadata["privacy_tier"] = tier
    target = notes / f"{name}.md"
    target.write_text(
        frontmatter.dumps(frontmatter.Post(content=body, **metadata)),
        encoding="utf-8",
    )
    return target


def _tier_of(path: Path) -> str:
    """Return the ``privacy_tier`` recorded on the fragment at *path*.

    Args:
        path: A fragment file.

    Returns:
        The raw frontmatter value, or ``"unclassified"`` when absent.
    """
    return str(frontmatter.load(str(path)).metadata.get("privacy_tier", "unclassified"))


@pytest.fixture(name="vault")
def _vault(tmp_path: Path) -> Path:
    """Return a seeded, empty vault.

    Args:
        tmp_path: pytest's per-test directory.

    Returns:
        The vault root.
    """
    return seed_vault(tmp_path / "vault")


@pytest.fixture(name="api")
def _api(vault: Path) -> Iterator[TestClient]:
    """Yield a client over an app serving *vault*.

    Args:
        vault: The vault root.

    Yields:
        A ``TestClient`` that never opens a socket.
    """
    with client(vault_path=vault) as opened:
        yield opened


# --------------------------------------------------------------------------- #
# The two load-bearing premises, asserted against unmodified code
# --------------------------------------------------------------------------- #


def test_unclassified_is_admitted_at_the_personal_ceiling() -> None:
    """``unclassified`` ranks *equal to* ``personal``, so it is already remote-readable.

    This is the fact that inverts the naive privacy reading of this route.
    A network-seeded, unclassified corpus is not withheld from a ``personal``
    consumer — it is served. Classification is what can move a fragment
    *above* that line.
    """
    assert tier_sensitivity(PrivacyTier.UNCLASSIFIED) == tier_sensitivity(
        PrivacyTier.PERSONAL
    )
    assert tier_sensitivity(PrivacyTier.INTIMATE) > tier_sensitivity(
        PrivacyTier.PERSONAL
    )


def test_rules_classification_escalates_an_untiered_fragment_to_intimate(
    vault: Path,
) -> None:
    """A ``method=rules`` pass moves recovery prose from untiered to ``intimate``.

    The second premise: classification *tightens*. Refusing to run it under a
    ``personal`` ceiling would leave this fragment sitting at ``unclassified``,
    which the test above shows is served to that very consumer.
    """
    target = _write_fragment(vault, "untiered", _RECOVERY_BODY, tier=None)
    assert _tier_of(target) == PrivacyTier.UNCLASSIFIED.value

    summary = run_classify(
        vault_path=vault,
        config=load_vault_config(vault),
        method="rules",
        force=False,
    )

    assert summary.privacy_tiers_assigned == 1
    assert _tier_of(target) == PrivacyTier.INTIMATE.value


def test_the_pipeline_minor_outranks_every_single_digit_minor() -> None:
    """``0.10`` is at or above ``0.9``, compared componentwise rather than as text.

    The first double-digit minor in this contract's history. A lexicographic
    comparison would put ``"0.10"`` below ``"0.8"`` and start hiding the
    capability from the newest clients on the day it shipped.
    """
    assert minor_at_least(PIPELINE_MINOR, "0.9")
    assert minor_at_least(PIPELINE_MINOR, PIPELINE_MINOR)
    assert not minor_at_least("0.9", PIPELINE_MINOR)


def test_the_fixture_keywords_are_live_rules_signals() -> None:
    """The e2e fixture's keywords really are in the classifier's own tables.

    Without this, a renamed signal would turn the end-to-end test's
    ``frequency != unclassified`` assertion into one that can never pass, and
    the failure would read as a broken route rather than a stale fixture.
    """
    assert set(_F3_SIGNALS) <= set(FREQUENCY_SIGNALS[Frequency.F3])
    assert set(_RISING_SIGNALS) <= set(WAVELENGTH_PHASE_SIGNALS[Phase.RISING])


# --------------------------------------------------------------------------- #
# The end-to-end route the Definition of Done asks for
# --------------------------------------------------------------------------- #


def test_a_network_seeded_vault_is_classified_and_linked_over_v1(
    api: TestClient, vault: Path
) -> None:
    """Upload, classify and link a vault over ``/v1`` alone, then read the result.

    The whole issue in one test: no CLI, no shell access, no MCP client. The
    fragment lands with real ``frequency`` and ``wavelength.phase`` values
    rather than the ``unclassified`` a seeded-but-uncalled vault carries.
    """
    uploaded = api.post(
        UPLOAD_PATH,
        json=_upload_body("adepthood:doc:pipeline", "note.md", _signal_body()),
        headers=headers(ceiling="personal"),
    )
    assert uploaded.status_code == HTTP_OK

    before = frontmatter.load(str(_fragments(vault)[0])).metadata
    assert before["frequency"]["primary"] == Frequency.UNCLASSIFIED.value

    classified = api.post(
        CLASSIFICATIONS_PATH,
        json={"method": "rules"},
        headers=headers(ceiling="personal"),
    )
    assert classified.status_code == HTTP_OK
    assert classified.json()["classified"] == 1

    linked = api.post(
        LINKS_PATH, json={"method": "temporal"}, headers=headers(ceiling="personal")
    )
    assert linked.status_code == HTTP_OK
    assert linked.json()["fragment_count"] == 1

    after = frontmatter.load(str(_fragments(vault)[0])).metadata
    assert after["frequency"]["primary"] == Frequency.F3.value
    assert after["wavelength"]["phase"] == Phase.RISING.value


def test_a_classification_completes_and_says_so(api: TestClient, vault: Path) -> None:
    """The response reports completeness, which is what makes a retry safe.

    A run interrupted by the request timeout leaves the fragments it already
    stamped alone on the next pass, so ``complete`` is the client's signal to
    stop retrying rather than a decoration.
    """
    _write_fragment(vault, "signal", _signal_body(), tier="personal")

    body = api.post(
        CLASSIFICATIONS_PATH,
        json={"method": "rules"},
        headers=headers(ceiling="personal"),
    ).json()

    assert body["complete"] is True
    assert body["total"] == 1


def test_an_unreadable_fragment_makes_the_pass_incomplete_and_says_nothing_else(
    api: TestClient, vault: Path
) -> None:
    """A fragment the engine could not read flips ``complete``, and leaks no path.

    Both halves in one test, because they are one decision. The engine's own
    error string is ``unreadable fragment <absolute path>: <parser detail>`` —
    a path disclosure wearing a diagnostic hat — so the route collapses the
    whole list to a boolean. Without the boolean the caller could not tell a
    partial pass from a finished one; with the list, a remote consumer would
    learn where the vault lives.
    """
    _write_fragment(vault, "signal", _signal_body(), tier="personal")
    (vault / "01-Fragments" / "Notes" / "unreadable.md").write_text(
        "---\ntype: fragment\nid: frag-bad\ntitle: [unclosed\n---\nbody\n",
        encoding="utf-8",
    )

    response = api.post(
        CLASSIFICATIONS_PATH, json={"method": "rules"}, headers=headers()
    )

    assert response.status_code == HTTP_OK
    assert response.json()["complete"] is False
    assert response.json()["classified"] == 1
    assert "unreadable" not in response.text
    assert str(vault) not in response.text


def test_the_classification_request_defaults_to_the_rules_method(
    api: TestClient, vault: Path
) -> None:
    """An empty body classifies by rules, which is the only method this route serves.

    The default invocation is the one a consumer writes first, and it must be
    the one that is covered — not merely the explicit-flag path beside it.
    """
    _write_fragment(vault, "signal", _signal_body(), tier="personal")

    response = api.post(CLASSIFICATIONS_PATH, json={}, headers=headers())

    assert response.status_code == HTTP_OK
    assert response.json()["classified"] == 1


# --------------------------------------------------------------------------- #
# Privacy: the route tightens, discloses nothing, and never lowers a tier
# --------------------------------------------------------------------------- #


def test_classification_under_the_personal_ceiling_escalates_to_intimate(
    api: TestClient, vault: Path
) -> None:
    """Under the narrowest remote ceiling, a run may still raise a fragment to intimate.

    The inverse of the naive reading, driven through the real route. A gate
    that refused this would preserve an exposure rather than prevent one: the
    fragment starts at ``unclassified``, which this caller is served.
    """
    target = _write_fragment(vault, "untiered", _RECOVERY_BODY, tier=None)

    response = api.post(
        CLASSIFICATIONS_PATH,
        json={"method": "rules"},
        headers=headers(ceiling=TierCeiling.PERSONAL.value),
    )

    assert response.status_code == HTTP_OK
    assert response.json()["privacy_tiers_assigned"] == 1
    assert _tier_of(target) == PrivacyTier.INTIMATE.value


def test_neither_response_names_a_fragment(api: TestClient, vault: Path) -> None:
    """Counts and a method name; never an id, a path, a title or a body.

    The route runs over the *whole* vault, including material far above the
    caller's ceiling. What makes that safe is not a filter but the response
    shape: there is no field any of it could arrive in.
    """
    _write_fragment(vault, "intimate-one", _RECOVERY_BODY, tier="intimate")

    classified = api.post(
        CLASSIFICATIONS_PATH, json={"method": "rules"}, headers=headers()
    )
    linked = api.post(LINKS_PATH, json={"method": "temporal"}, headers=headers())

    for response in (classified, linked):
        rendered = response.text
        assert "intimate-one" not in rendered
        assert "sobriety" not in rendered
        assert "frag-" not in rendered
        assert "01-Fragments" not in rendered


def test_retier_corrects_a_tier_the_uploader_declared_wrongly(
    api: TestClient, vault: Path
) -> None:
    """``retier`` is what fixes a consumer that filed intimate bytes as ``personal``.

    The hole a plain classification cannot close. ``POST /v1/uploads`` requires
    an explicit tier and the tier pass does not own an already-concrete one, so
    a mis-declared upload survives every default run — ``privacy_tiers_assigned``
    stays ``0`` and the fragment stays where the caller put it. Only ``retier``
    re-derives it, and only upwards.
    """
    target = _write_fragment(vault, "misdeclared", _RECOVERY_BODY, tier="personal")

    default_run = api.post(
        CLASSIFICATIONS_PATH, json={"method": "rules"}, headers=headers()
    )
    assert default_run.json()["privacy_tiers_assigned"] == 0
    assert _tier_of(target) == PrivacyTier.PERSONAL.value

    corrected = api.post(
        CLASSIFICATIONS_PATH,
        json={"method": "rules", "retier": True},
        headers=headers(),
    )

    assert corrected.status_code == HTTP_OK
    assert corrected.json()["retiered"] == 1
    assert _tier_of(target) == PrivacyTier.INTIMATE.value


def test_an_intimate_fragment_is_never_lowered_by_either_route(
    api: TestClient, vault: Path
) -> None:
    """The one-way ratchet holds, with ``retier`` on and off, at every ceiling.

    ``retier`` re-derives a tier a caller already declared, which is the only
    way this surface can correct a consumer that filed intimate bytes as
    ``personal``. It is escalate-only by construction; this pins that it stays
    so from the outside.
    """
    target = _write_fragment(vault, "declared", "an entirely ordinary note", "intimate")

    for retier in (False, True):
        for ceiling in (TierCeiling.OPEN.value, TierCeiling.PERSONAL.value):
            response = api.post(
                CLASSIFICATIONS_PATH,
                json={"method": "rules", "retier": retier},
                headers=headers(ceiling=ceiling),
            )
            assert response.status_code == HTTP_OK
            assert _tier_of(target) == PrivacyTier.INTIMATE.value


def test_a_classification_never_constructs_an_llm_provider(
    api: TestClient, vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With provider construction made to raise, the route still answers ``200``.

    The non-vacuous proof that no byte can leave the host on this path: the
    wire enum admits ``rules`` alone, so the model layer is never reached at
    all rather than merely being configured not to be.
    """
    from creek.classify import classify_engine

    def _explode(*_args: object, **_kwargs: object) -> object:
        """Fail loudly if anything on this path reaches for a model.

        Args:
            *_args: Unread.
            **_kwargs: Unread.

        Raises:
            AssertionError: Always.
        """
        msg = "a rules classification must never construct an LLM provider"
        raise AssertionError(msg)

    monkeypatch.setattr(classify_engine, "build_tier_classifiers", _explode)
    _write_fragment(vault, "signal", _signal_body(), tier="personal")

    response = api.post(
        CLASSIFICATIONS_PATH, json={"method": "rules"}, headers=headers()
    )

    assert response.status_code == HTTP_OK
    assert response.json()["classified"] == 1


# --------------------------------------------------------------------------- #
# What this surface deliberately does not serve
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("path", "body"),
    [
        (CLASSIFICATIONS_PATH, {"method": "llm"}),
        (LINKS_PATH, {"method": "embeddings"}),
    ],
    ids=["classify-llm", "link-embeddings"],
)
def test_the_long_running_methods_are_accepted_as_durable_jobs(
    api: TestClient,
    path: str,
    body: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``llm`` and ``embeddings`` return immediately with a pollable job id.

    These methods can take minutes or hours, so they must not hold the request
    or borrow the synchronous ``200`` response. The id is opaque and contains
    no fragment or path material; UUID parsing pins that it is server-minted
    rather than derived from anything in the vault.
    """
    monkeypatch.setattr(pipeline_module, "classify_tool", _llm_success)
    monkeypatch.setattr(pipeline_module, "link_tool", _embeddings_success)

    response = api.post(path, json=body, headers=headers())

    assert response.status_code == HTTP_ACCEPTED
    assert response.json() == {
        "status": "accepted",
        "job_id": response.json()["job_id"],
        "state": "queued",
    }
    UUID(response.json()["job_id"])


def test_a_short_pipeline_request_keeps_its_exact_synchronous_bytes(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The new job branch does not alter the pre-0.14 ``rules`` response."""
    monkeypatch.setattr(pipeline_module, "classify_tool", _llm_success)

    response = api.post(
        CLASSIFICATIONS_PATH, json={"method": "rules"}, headers=headers()
    )

    assert response.status_code == HTTP_OK
    assert response.content == (
        b'{"status":"ok","tier_ceiling":"open","method":"rules",'
        b'"total":3,"classified":2,"preserved_manual":1,"preserved_llm":0,'
        b'"privacy_tiers_assigned":1,"retiered":0,"praxis_marked":1,'
        b'"tags_extracted":2,"complete":true}'
    )


def _llm_success(**_kwargs: object) -> dict[str, object]:
    """Return a complete counts-only LLM result without constructing a provider."""
    return {
        "status": "ok",
        "tier_ceiling": "open",
        "total": 3,
        "classified": 2,
        "preserved_manual": 1,
        "preserved_llm": 0,
        "privacy_tiers_assigned": 1,
        "retiered": 0,
        "praxis_marked": 1,
        "tags_extracted": 2,
        "errors": [],
    }


def _blocked_llm_success(
    started: Event,
    release: Event,
    calls: list[None],
    **_kwargs: object,
) -> dict[str, object]:
    """Hold one fake pass open so the admission boundary can be exercised."""
    calls.append(None)
    started.set()
    assert release.wait(timeout=2.0)
    return _llm_success()


def _embeddings_success(**_kwargs: object) -> dict[str, object]:
    """Return counts for an embeddings pass without loading a model."""
    return {
        "status": "ok",
        "tier_ceiling": "open",
        "method": "embeddings",
        "fragment_count": 3,
        "link_count": 2,
        "largest_cluster_fragments": 2,
        "clusters_split": 0,
        "oversized_discarded": 0,
    }


def _await_terminal(api: TestClient, job_id: str) -> dict[str, object]:
    """Poll one test job to a terminal state with a short bounded wait."""
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        response = api.get(f"/v1/jobs/{job_id}", headers=headers())
        assert response.status_code == HTTP_OK
        body: dict[str, object] = response.json()
        if body["state"] in {"succeeded", "failed"}:
            return body
        time.sleep(0.01)
    pytest.fail("pipeline job did not reach a terminal state")


def test_an_accepted_job_is_pollable_to_its_counts_only_result(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A durable handle converges to the synchronous model, never vault prose."""
    monkeypatch.setattr(pipeline_module, "classify_tool", _llm_success)

    accepted = api.post(CLASSIFICATIONS_PATH, json={"method": "llm"}, headers=headers())
    body = _await_terminal(api, accepted.json()["job_id"])

    assert body == {
        "status": "ok",
        "job_id": accepted.json()["job_id"],
        "state": "succeeded",
        "result": {
            "status": "ok",
            "tier_ceiling": "open",
            "method": "llm",
            "total": 3,
            "classified": 2,
            "preserved_manual": 1,
            "preserved_llm": 0,
            "privacy_tiers_assigned": 1,
            "retiered": 0,
            "praxis_marked": 1,
            "tags_extracted": 2,
            "complete": True,
        },
    }


def test_a_consumer_cannot_fan_out_inflight_pipeline_jobs(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One active expensive pass per consumer bounds detached worker fan-out."""
    started = Event()
    release = Event()
    calls: list[None] = []

    monkeypatch.setattr(
        pipeline_module,
        "classify_tool",
        partial(_blocked_llm_success, started, release, calls),
    )
    first = api.post(CLASSIFICATIONS_PATH, json={"method": "llm"}, headers=headers())
    assert started.wait(timeout=2.0)
    second = api.post(CLASSIFICATIONS_PATH, json={"method": "llm"}, headers=headers())
    release.set()

    assert first.status_code == HTTP_ACCEPTED
    assert second.status_code == HTTP_UNAVAILABLE
    assert _await_terminal(api, first.json()["job_id"])["state"] == "succeeded"
    assert calls == [None]

    admitted_after_completion = api.post(
        CLASSIFICATIONS_PATH, json={"method": "llm"}, headers=headers()
    )
    assert admitted_after_completion.status_code == HTTP_ACCEPTED


def test_an_embeddings_job_is_pollable_to_the_link_counts(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The second long method uses the same durable terminal contract."""
    monkeypatch.setattr(pipeline_module, "link_tool", _embeddings_success)

    accepted = api.post(LINKS_PATH, json={"method": "embeddings"}, headers=headers())
    body = _await_terminal(api, accepted.json()["job_id"])

    assert body["state"] == "succeeded"
    assert body["result"] == {
        "status": "ok",
        "tier_ceiling": "open",
        "method": "embeddings",
        "fragment_count": 3,
        "link_count": 2,
        "largest_cluster_fragments": 2,
        "clusters_split": 0,
        "oversized_discarded": 0,
    }


def test_a_job_failure_never_narrates_the_tool_error(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A detached tool exception becomes only ``failed`` plus a null result."""
    intimate_marker = "private-title-from-an-intimate-fragment"

    def _explode(**_kwargs: object) -> dict[str, object]:
        raise RuntimeError(intimate_marker)

    monkeypatch.setattr(pipeline_module, "classify_tool", _explode)
    accepted = api.post(CLASSIFICATIONS_PATH, json={"method": "llm"}, headers=headers())
    body = _await_terminal(api, accepted.json()["job_id"])

    assert body == {
        "status": "ok",
        "job_id": accepted.json()["job_id"],
        "state": "failed",
        "result": None,
    }
    assert intimate_marker not in json.dumps(body)


def test_job_status_is_consumer_bound(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another authenticated consumer cannot enumerate a job or its result."""
    monkeypatch.setattr(pipeline_module, "classify_tool", _llm_success)
    accepted = api.post(CLASSIFICATIONS_PATH, json={"method": "llm"}, headers=headers())
    job_id = accepted.json()["job_id"]

    refused = api.get(f"/v1/jobs/{job_id}", headers=headers(token=OTHER_TOKEN))
    terminal = _await_terminal(api, job_id)

    assert refused.status_code == HTTP_UNAVAILABLE
    assert set(terminal) == {"status", "job_id", "state", "result"}


def test_a_restart_marks_an_inflight_job_failed_instead_of_running_forever(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A new worker turns an orphaned queued/running record into ``failed``."""
    started = Event()
    release = Event()

    def _blocked(**_kwargs: object) -> dict[str, object]:
        started.set()
        assert release.wait(timeout=2.0)
        return _llm_success()

    monkeypatch.setattr(pipeline_module, "classify_tool", _blocked)
    with client(vault_path=vault) as first:
        accepted = first.post(
            CLASSIFICATIONS_PATH, json={"method": "llm"}, headers=headers()
        )
        assert started.wait(timeout=2.0)
        job_id = accepted.json()["job_id"]

        with client(vault_path=vault) as restarted:
            status = restarted.get(f"/v1/jobs/{job_id}", headers=headers())

        release.set()

    assert status.status_code == HTTP_OK
    assert status.json() == {
        "status": "ok",
        "job_id": job_id,
        "state": "failed",
        "result": None,
    }


def test_an_intimate_fragment_in_an_http_llm_job_never_reaches_cloud(
    api: TestClient,
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The async adapter retains ModelRouter's intimate-local chokepoint."""
    providers: list[str] = []

    class _RecordingClassifier:
        def __init__(self, config: Any) -> None:
            self.config = config
            self._provider = str(config.provider)

        @property
        def available(self) -> bool:
            return True

        def classify_with_reasoning(
            self, fragment: Any, content: str
        ) -> LLMClassificationResult:
            _ = content
            providers.append(self._provider)
            return LLMClassificationResult(fragment=fragment, reasoning="")

    monkeypatch.setattr(
        "creek.classify.classify_engine.LLMClassifier", _RecordingClassifier
    )
    (vault / "00-Creek-Meta" / "creek_config.yaml").write_text(
        "llm:\n"
        "  default:\n"
        "    provider: ollama\n"
        "    model: qwen3:8b\n"
        "  classification:\n"
        "    provider: anthropic\n"
        "    model: claude-haiku-4-5\n",
        encoding="utf-8",
    )
    _write_fragment(vault, "intimate", _RECOVERY_BODY, tier="intimate")

    accepted = api.post(CLASSIFICATIONS_PATH, json={"method": "llm"}, headers=headers())
    terminal = _await_terminal(api, accepted.json()["job_id"])

    assert terminal["state"] == "succeeded"
    assert providers == ["ollama"]
    assert _RECOVERY_BODY not in json.dumps(terminal)


@pytest.mark.parametrize(
    "path", [CLASSIFICATIONS_PATH, LINKS_PATH], ids=["classifications", "links"]
)
def test_a_client_below_the_pipeline_minor_is_refused(
    api: TestClient, path: str
) -> None:
    """A consumer pinned to ``0.9`` is neither told about nor served this capability.

    The additive half of the contract bump: nothing on the wire changes for a
    client that negotiated an earlier minor, because it cannot reach the new
    routes at all.
    """
    response = api.post(path, json={}, headers=headers(minor="0.9"))

    assert response.status_code == HTTP_CONFLICT


def test_pipeline_is_a_published_capability_at_the_current_minor() -> None:
    """The capability exists and the server's own minor is at least its floor."""
    assert Capability.PIPELINE.value == "pipeline"
    assert minor_at_least(CONTRACT_MINOR, PIPELINE_MINOR)


# --------------------------------------------------------------------------- #
# The paths a healthy vault never takes
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("unknown method 'llm'; supported: rules", ErrorCode.INVALID_REQUEST),
        ("ollama at http://127.0.0.1:11434 is unreachable", ErrorCode.INTERNAL_ERROR),
        ("", ErrorCode.INTERNAL_ERROR),
    ],
    ids=["unknown-method", "provider-detail", "empty"],
)
def test_pipeline_refusal_code_is_total_and_fails_closed(
    reason: str, expected: ErrorCode
) -> None:
    """Every refusal maps, and an unrecognised one becomes a server fault.

    The provider case is the one that matters: ``classify_tool`` renders an
    unreachable LLM provider as ``str(exc)``, which is arbitrary text from a
    provider client. Falling through to ``internal_error`` is what keeps it
    from being narrated as a plausible caller-facing refusal — and
    :func:`~creek_mcp.httpapi.errors.error_response` renders one constant per
    code, so the text itself never reaches a body.
    """
    assert pipeline_refusal_code(reason) is expected


@pytest.mark.parametrize(
    ("path", "body"),
    [(CLASSIFICATIONS_PATH, {"method": "rules"}), (LINKS_PATH, {"method": "temporal"})],
    ids=["classifications", "links"],
)
def test_an_unreadable_vault_is_unavailable_and_is_not_scaffolded(
    tmp_path: Path, path: str, body: dict[str, str]
) -> None:
    """No vault means ``503``, and the directory is left exactly as it was found.

    The probe runs *before* either tool, because both append to the audit log
    and an audit append creates its parent directories — so an unprobed call
    against a missing vault would scaffold one from the network.
    """
    absent = tmp_path / "not-a-vault"
    absent.mkdir()

    with client(vault_path=absent) as api:
        response = api.post(path, json=body, headers=headers())

    assert response.status_code == HTTP_UNAVAILABLE
    assert list(absent.iterdir()) == []


def test_a_tool_refusal_is_projected_rather_than_narrated(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refusal reaches the wire as a code, never as the tool's own words.

    Unreachable through the published enum — that is the point of excluding
    ``llm`` in the type — so the tool is substituted to produce the refusal a
    relaxed field or a new caller path would one day produce for real.
    """
    monkeypatch.setattr(
        pipeline_module,
        "classify_tool",
        lambda **_kwargs: {
            "status": "refused",
            "tool": "creek.classify",
            "reason": "unknown method 'llm'; supported: rules, llm",
        },
    )

    response = api.post(
        CLASSIFICATIONS_PATH, json={"method": "rules"}, headers=headers()
    )

    assert response.status_code == HTTP_UNPROCESSABLE
    assert response.json()["code"] == ErrorCode.INVALID_REQUEST.value
    assert "unknown method" not in response.text


def test_a_success_this_contract_cannot_express_is_a_server_fault(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tool success missing a count is a ``500``, not a zero.

    Every count is read with ``[]`` rather than ``.get(..., 0)``: defaulting a
    missing one would report a silent no-op as a completed pass, which is the
    failure this whole surface exists to prevent.
    """
    monkeypatch.setattr(
        pipeline_module,
        "link_tool",
        lambda **_kwargs: {
            "status": "ok",
            "tool": "creek.link",
            "tier_ceiling": "open",
            "method": "temporal",
            "fragment_count": 1,
        },
    )

    response = api.post(LINKS_PATH, json={"method": "temporal"}, headers=headers())

    assert response.status_code == HTTP_SERVER_FAULT
    assert response.json()["code"] == ErrorCode.INTERNAL_ERROR.value


# --------------------------------------------------------------------------- #
# What the served link stages actually cost
# --------------------------------------------------------------------------- #


class _StubEncoder:
    """A stand-in for the local sentence-transformer, recording that it loaded.

    The real model is a multi-hundred-megabyte download. What these tests need
    to pin is *which stages reach for it*, not what it returns, so the loader
    is substituted and the vectors are deterministic nonsense.

    Attributes:
        dim: Width of every vector this encoder returns.
    """

    dim: int = 4

    def encode(self, texts: list[str], **_kwargs: object) -> list[list[float]]:
        """Return one deterministic vector per text.

        Args:
            texts: The fragment texts the linker wants embedded.
            **_kwargs: ``show_progress_bar`` and ``batch_size``, both unread.

        Returns:
            One distinct vector per text, so a clustering stage has something
            to separate.
        """
        return [[float(index + 1)] * self.dim for index, _text in enumerate(texts)]


def _record_model_loads(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Substitute the sentence-transformer loader and record every call.

    Args:
        monkeypatch: pytest's patcher.

    Returns:
        A list that gains the model name each time a stage loads the model.
    """
    loaded: list[str] = []

    def _load(model_name: str, _cache_folder: str | None = None) -> _StubEncoder:
        """Record the load and return the stub.

        Args:
            model_name: The configured model name.
            _cache_folder: Unread.

        Returns:
            The stub encoder.
        """
        loaded.append(model_name)
        return _StubEncoder()

    monkeypatch.setattr(embeddings_module, "_load_sentence_transformer", _load)
    return loaded


def test_the_temporal_stage_loads_no_model_at_all(
    api: TestClient, vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``temporal`` is the one served stage that needs no vectors.

    The contrast that makes the next test mean something: without it, "the
    cluster stages embed" could be true of every stage and say nothing about
    which one a caller should reach for first.
    """
    loaded = _record_model_loads(monkeypatch)
    for index in range(3):
        _write_fragment(vault, f"f{index}", _signal_body(), tier="personal")

    response = api.post(LINKS_PATH, json={"method": "temporal"}, headers=headers())

    assert response.status_code == HTTP_OK
    assert loaded == []


@pytest.mark.parametrize("method", ["eddies", "threads"], ids=["eddies", "threads"])
def test_the_cluster_stages_run_a_local_embedding_pass(
    api: TestClient, vault: Path, monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    """``eddies`` and ``threads`` fill the embeddings cache with a **local** model.

    Excluding ``embeddings`` from the wire enum does not make what remains
    uniformly cheap, and the published documents said for one draft that it
    did. Both stages cluster over vectors, so on a cold parquet cache they run
    a sentence-transformer pass over every uncached fragment — minutes on a
    large vault, and a first call that can outrun the request deadline.

    Pinned here rather than left to prose for two reasons. The cost claim in
    ``docs/api.md``, ``docs/seeding.md`` and both ADRs is only as durable as
    something that fails when it stops being true. And the *local* half is a
    standing guarantee, not a cost note: the loader these stages reach is
    ``sentence_transformers``, so no vault byte can leave the host on the link
    route any more than it can on the classification one.
    """
    loaded = _record_model_loads(monkeypatch)
    for index in range(3):
        _write_fragment(vault, f"f{index}", _signal_body(), tier="personal")

    response = api.post(LINKS_PATH, json={"method": method}, headers=headers())

    assert response.status_code == HTTP_OK
    assert response.json()["fragment_count"] == 3
    assert loaded, f"{method} reached no embedding model at all"
