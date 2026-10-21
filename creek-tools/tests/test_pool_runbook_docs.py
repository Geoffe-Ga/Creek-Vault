"""Guard the pool measurement-pin runbook against drift and overclaim.

``docs/pool-measurement-pin.md`` is an operator runbook for a design that is
**conditionally** selected: ADR-0016 is ratified as O5 but `Accepted` takes
effect only once the adepthood ADR 0009 amendment merges, the vendor is not
chosen, condition (f) of Decision 3.3 is unverified for either candidate, and
the owner has authorized no spend. A runbook that reads as though any of that
were settled would be worse than no runbook, so the page's honesty is tested
rather than trusted:

* every Decision 3.3 condition (a)-(h) carries a verdict from a closed
  vocabulary, and none is silently absent;
* condition (f) reads ``vendor-asserted``;
* the two D05 conditions O5 cannot meet are recorded as waivers *required*,
  never granted;
* the O3-only acceptance tests are named in the dormant section;
* the constants the page quotes still match the code that defines them;
* ``pool_min_below_one`` is described exactly as registered or not, whichever
  :class:`~creek_mcp.provisioning.budget.AlertKind` currently says;
* no vendor is named, because naming one would mean the status banner above it
  went stale;
* the forbidden custody claims appear only under a negation.

The scans are heuristic tripwires, not proofs. Review remains the real check.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

import pytest

from creek.classify.llm.pool_boundary import (
    POOL_CONNECT_TIMEOUT,
    POOL_REQUEST_TIMEOUT,
    POOL_URL_ENV,
)
from creek.classify.llm.providers import POOL_IDENTITY_SCHEME
from creek_mcp.container_health import ProbeResult, ProbeStatus
from creek_mcp.model_package import MODEL_PACKAGE_FILE_ENV
from creek_mcp.provisioning.budget import AlertKind
from creek_mcp.provisioning.pool_bundle import (
    POOL_MANIFEST_GUEST_PATH,
    POOL_VENDOR_KEY_GUEST_PATH,
)

_DOCS: Final = Path(__file__).resolve().parents[1] / "docs"
_RUNBOOK_NAME: Final = "pool-measurement-pin.md"
_RUNBOOK: Final = _DOCS / _RUNBOOK_NAME

_WARM_MINIMUM_ALERT: Final = "pool_min_below_one"
_NOT_REGISTERED: Final = "not registered"

# The verdict vocabulary a condition row may carry. "Kept" and "not
# applicable" are settled; the other three each name an open owner gate.
_VERDICTS: Final = (
    "kept",
    "waived",
    "unmet-and-replaced",
    "unverified",
    "not applicable",
)

_CONDITIONS: Final = tuple("abcdefgh")

# Conditions (c) and (e) cannot be met under O5. The record replaces each and
# says an explicit owner waiver is needed; neither has been granted.
_WAIVER_CONDITIONS: Final = ("(c)", "(e)")

# Naming a vendor would contradict the page's own status banner, which records
# the vendor choice as an open owner escalation. When a vendor IS chosen,
# update the banner, the (a)-(h) verdicts and this tuple in the same change.
_UNCHOSEN_VENDORS: Final = ("privatemode", "tinfoil", "edgeless")

# Custody claims this page may not make. Each is allowed only in a sentence
# that negates it (the page's own standing bar is such a sentence).
_FORBIDDEN_CLAIMS: Final = (
    "operator-blind",
    "zero retention",
    "never leaves",
    "cannot read",
)
_NEGATION: Final = re.compile(
    r"\bno\b|\bnot\b|\bnever\b|\bneither\b|\buntil\b|\bunverified\b|"
    r"\bvendor-asserted\b|\bwould be\b",
    re.IGNORECASE,
)
_SENTENCE: Final = re.compile(r"[^.!?\n]+")

_POOL_EXIT_CODES: Final = {
    ProbeStatus.POOL_READY: 0,
    ProbeStatus.POOL_UNCONFIGURED: 30,
    ProbeStatus.POOL_UNREACHABLE: 31,
    ProbeStatus.POOL_MODEL_MISSING: 32,
    ProbeStatus.POOL_IDENTITY_MISMATCH: 33,
}

_DORMANT_HEADING: Final = "## 7. The O3 engine-host runbook, dormant"
_DORMANT_TESTS: Final = ("T4", "T17", "T22", "T25")


@pytest.fixture(scope="module")
def runbook() -> str:
    """Return the runbook's text.

    Returns:
        The whole page.
    """
    return _RUNBOOK.read_text(encoding="utf-8")


def test_runbook_exists() -> None:
    """The page the pilot checklist and the docs index point at is present."""
    assert _RUNBOOK.is_file(), f"missing {_RUNBOOK_NAME}"


def test_docs_index_lists_the_runbook() -> None:
    """The docs index links the runbook, so an operator can find it."""
    index = (_DOCS / "README.md").read_text(encoding="utf-8")
    assert f"]({_RUNBOOK_NAME})" in index


def test_pilot_checklist_links_the_runbook() -> None:
    """The pilot checklist's pool paragraph points at the runbook."""
    pilot = (_DOCS / "managed-vault-fly-pilot.md").read_text(encoding="utf-8")
    assert f"]({_RUNBOOK_NAME})" in pilot


def test_status_records_every_open_gate(runbook: str) -> None:
    """The banner names each gate that keeps O5 from being live."""
    lowered = runbook.lower()
    for gate in (
        "adr 0009 amendment",
        "o3 remains",
        "vendor is not chosen",
        "no spend",
    ):
        assert gate in lowered, f"status banner does not record: {gate}"


@pytest.mark.parametrize("letter", _CONDITIONS)
def test_every_condition_carries_a_verdict(runbook: str, letter: str) -> None:
    """Each Decision 3.3 condition (a)-(h) has a row with a closed verdict."""
    rows = [line for line in runbook.splitlines() if line.startswith(f"| ({letter})")]
    assert rows, f"condition ({letter}) has no verdict row"
    row = rows[0].lower()
    assert any(verdict in row for verdict in _VERDICTS), (
        f"condition ({letter}) carries no verdict from {_VERDICTS}"
    )


def test_condition_f_reads_vendor_asserted(runbook: str) -> None:
    """Condition (f) is unverified, so the exclusion reads vendor-asserted."""
    rows = [line for line in runbook.splitlines() if line.startswith("| (f)")]
    assert "vendor-asserted" in rows[0]
    assert "vendor-asserted" in runbook.split("### Condition (f)")[1]


@pytest.mark.parametrize("condition", _WAIVER_CONDITIONS)
def test_waivers_are_required_not_granted(runbook: str, condition: str) -> None:
    """The two D05 substitutions are recorded as awaiting an owner waiver."""
    rows = [line for line in runbook.splitlines() if line.startswith(f"| {condition}")]
    assert rows, f"condition {condition} has no verdict row"
    assert "not granted" in rows[0].lower(), (
        f"condition {condition} must record its waiver as not granted"
    )


def test_dormant_section_names_every_o3_only_test(runbook: str) -> None:
    """T4, T17, T22 and T25 are recorded as O3-only inside the O3 section."""
    assert _DORMANT_HEADING in runbook
    dormant = runbook.split(_DORMANT_HEADING)[1]
    for test_id in _DORMANT_TESTS:
        assert test_id in dormant, f"{test_id} is not recorded as dormant"


def test_warm_minimum_section_states_t19_honestly(runbook: str) -> None:
    """T19's three clauses are each recorded with what they still need."""
    assert "T19" in runbook
    assert _WARM_MINIMUM_ALERT in runbook


def test_alert_kind_status_matches_the_enum(runbook: str) -> None:
    """The page says the warm-minimum alert is unregistered iff it is.

    When the control-plane issue registers ``pool_min_below_one``, this fails
    until the runbook's T19 row is updated to match.
    """
    registered = _WARM_MINIMUM_ALERT in {kind.value for kind in AlertKind}
    says_missing = _NOT_REGISTERED in runbook
    assert says_missing is not registered, (
        f"{_WARM_MINIMUM_ALERT} registered={registered}; "
        f"runbook says {_NOT_REGISTERED!r}={says_missing}"
    )


@pytest.mark.parametrize(
    "constant",
    [
        POOL_IDENTITY_SCHEME,
        POOL_URL_ENV,
        MODEL_PACKAGE_FILE_ENV,
        POOL_MANIFEST_GUEST_PATH,
        POOL_VENDOR_KEY_GUEST_PATH,
        "runtime_inventory_digest",
        "runtime_digest",
    ],
)
def test_runbook_quotes_live_constants(runbook: str, constant: str) -> None:
    """Every name the page pins an operator to is the one the code defines."""
    assert constant in runbook, f"runbook does not quote {constant}"


@pytest.mark.parametrize("timeout", [POOL_CONNECT_TIMEOUT, POOL_REQUEST_TIMEOUT])
def test_runbook_quotes_live_timeouts(runbook: str, timeout: float) -> None:
    """The dial budgets the page quotes are the boundary's own."""
    assert f"{timeout:g} s" in runbook, f"runbook does not quote {timeout:g} s"


@pytest.mark.parametrize(("status", "code"), sorted(_POOL_EXIT_CODES.items()))
def test_runbook_quotes_pool_exit_codes(
    runbook: str, status: ProbeStatus, code: int
) -> None:
    """Each ``--check pool`` state is documented with its real exit code."""
    assert ProbeResult(status).exit_code == code
    rows = [line for line in runbook.splitlines() if status.value in line]
    assert rows, f"{status.value} is not documented"
    assert any(re.search(rf"\b{code}\b", row) for row in rows), (
        f"{status.value} is not documented with exit code {code}"
    )


@pytest.mark.parametrize("vendor", _UNCHOSEN_VENDORS)
def test_no_vendor_is_named(runbook: str, vendor: str) -> None:
    """A vendor name here would contradict the page's own status banner."""
    assert vendor not in runbook.lower(), (
        f"{vendor} is named; the vendor choice is an open owner escalation"
    )


@pytest.mark.parametrize("claim", _FORBIDDEN_CLAIMS)
def test_forbidden_claims_only_appear_negated(runbook: str, claim: str) -> None:
    """No sentence asserts a custody property the implementation lacks."""
    for sentence in _SENTENCE.findall(runbook):
        if claim in sentence.lower():
            assert _NEGATION.search(sentence), (
                f"unnegated {claim!r} claim: {sentence.strip()!r}"
            )


def test_relative_links_resolve(runbook: str) -> None:
    """Every relative link on the page points at a file that exists."""
    targets = re.findall(r"\]\((?!https?://)([^)#]+)", runbook)
    assert targets
    for target in targets:
        assert (_DOCS / target).resolve().exists(), f"dead link: {target}"
