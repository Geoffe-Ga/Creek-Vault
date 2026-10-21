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
* every T19 clause row carries a status from a closed vocabulary and reads
  ``Not met`` while its gate is open, so the page cannot quietly claim T19;
* every ``per_sweep`` JSON path the evidence procedure quotes resolves against
  the real report models, and the page does not call the harness
  pool-incapable once the bench's provider vocabulary admits ``pool``;
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
from creek_mcp.bench.metadata import RunMetadata
from creek_mcp.bench.report import BenchReport, PhaseSummary, SweepSummary
from creek_mcp.bench.stats import LatencySummary
from creek_mcp.bench.trial import Phase, Sweep
from creek_mcp.container_health import ProbeResult, ProbeStatus
from creek_mcp.model_package import MODEL_PACKAGE_FILE_ENV, ModelPackageManifest
from creek_mcp.provisioning.budget import AlertKind
from creek_mcp.provisioning.pool_bundle import (
    POOL_MANIFEST_GUEST_PATH,
    POOL_VENDOR_KEY_GUEST_PATH,
)

_DOCS: Final = Path(__file__).resolve().parents[1] / "docs"
_RUNBOOK_NAME: Final = "pool-measurement-pin.md"
_RUNBOOK: Final = _DOCS / _RUNBOOK_NAME
_ADR_NAME: Final = "0016-shared-confidential-warm-inference-pool.md"

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

# The four T19 clauses, keyed by the opening words of each row's Clause cell.
# ADR-0016's T19 has four, not three: the scaling minimum, the load/download
# event, the p95 equivalence, and the fleet alert.
_T19_CLAUSES: Final = (
    "the scaling policy refuses a minimum of zero",
    "the first reflection after idle shows no load or download event",
    "its p95 equals the warm p95 within measurement noise",
    "the fleet report raises",
)

# A T19 status cell may only say met or not met. Longest first, so "not met"
# is matched before the "met" it contains.
_T19_STATUSES: Final = ("not met", "met")

# Any dotted path the page roots at ``per_sweep``. The whole tail is captured
# so a path that drops a level (``per_sweep.warm.latency.p95``) is caught too,
# not just one whose leaf field is wrong.
_JSON_PATH: Final = re.compile(r"per_sweep\.([A-Za-z0-9_][A-Za-z0-9_.]*)")


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


def _cell(row: str, index: int) -> str:
    """Return one cell of a Markdown table row, lowercased and stripped.

    Scanning a whole row lets a verdict word in the *gate* column stand in for
    a missing verdict, which is how a row like ``| (g) ... | satisfied | none;
    not applicable |`` used to pass. Cells are addressed instead.

    Args:
        row: The table row, leading and trailing pipe included.
        index: The 1-based cell position.

    Returns:
        The cell's text, lowercased, or ``""`` when the row is too short.
    """
    cells = row.split("|")[1:-1]
    return cells[index - 1].strip().lower() if len(cells) >= index else ""


@pytest.mark.parametrize("letter", _CONDITIONS)
def test_every_condition_carries_a_verdict(runbook: str, letter: str) -> None:
    """Each Decision 3.3 condition (a)-(h) has a closed verdict in its own cell.

    The verdict must be in the verdict column. A page that leaves the verdict
    cell vague and happens to say "not applicable" in the gate column fails.
    """
    rows = [line for line in runbook.splitlines() if line.startswith(f"| ({letter})")]
    assert rows, f"condition ({letter}) has no verdict row"
    verdict_cell = _cell(rows[0], 2)
    assert verdict_cell, f"condition ({letter}) has no verdict cell"
    assert any(verdict in verdict_cell for verdict in _VERDICTS), (
        f"condition ({letter}) verdict cell {verdict_cell!r} "
        f"carries no verdict from {_VERDICTS}"
    )


def test_adr_options_cell_reads_vendor_asserted() -> None:
    """The record's own Options cell says vendor-asserted while (f) is open.

    Decision 3.3: "without (f) the Options table's cell reads
    'vendor-asserted'". The runbook and the record must not disagree about
    who is excluded from reading a prompt.
    """
    adr = (_DOCS / "architecture" / "ADR" / _ADR_NAME).read_text(encoding="utf-8")
    rows = [line for line in adr.splitlines() if line.startswith("| **O5**")]
    assert rows, "the Options table has no O5 row"
    assert "vendor-asserted" in rows[0].lower()


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
    """T19's four clauses are each present and named in the section."""
    assert "T19" in runbook
    assert _WARM_MINIMUM_ALERT in runbook
    lowered = runbook.lower()
    for clause in _T19_CLAUSES:
        assert clause in lowered, f"T19 clause not recorded: {clause}"


@pytest.mark.parametrize("clause", _T19_CLAUSES)
def test_every_t19_clause_is_recorded_not_met(runbook: str, clause: str) -> None:
    """Each T19 clause carries a closed status, and that status is "not met".

    This is the page's most load-bearing table: it is the only place the
    runbook says whether the warm-minimum acceptance test is satisfied. Every
    clause's gate is open (the vendor choice, a budget line, the unregistered
    alert kind), so every status must read "not met". A page rewritten to claim
    T19 met fails here rather than passing on a substring.
    """
    rows = [
        line
        for line in runbook.splitlines()
        if line.startswith("| ") and _cell(line, 1).startswith(clause)
    ]
    assert rows, f"T19 clause has no row: {clause}"
    status = _cell(rows[0], 3)
    assert status, f"T19 clause {clause!r} has no status cell"
    stated = next(
        (word for word in _T19_STATUSES if status.lstrip("*").startswith(word)),
        None,
    )
    assert stated is not None, (
        f"T19 clause {clause!r} status {status!r} is not one of {_T19_STATUSES}"
    )
    assert stated == "not met", (
        f"T19 clause {clause!r} claims {stated!r}; every T19 gate is still open"
    )


def test_quoted_report_paths_resolve_against_the_models(runbook: str) -> None:
    """Every ``per_sweep`` path the page quotes exists in the report models.

    The evidence procedure tells an operator which JSON fields to read. A path
    naming a sweep, phase or field the models do not define sends them looking
    for something that is never emitted, so each segment is resolved against
    :class:`~creek_mcp.bench.report.BenchReport` rather than trusted.
    """
    sweeps = {sweep.value for sweep in Sweep}
    phases = {phase.value for phase in Phase}
    assert "per_sweep" in BenchReport.model_fields
    assert "per_phase" in SweepSummary.model_fields
    assert "latency" in PhaseSummary.model_fields
    found = _JSON_PATH.findall(runbook)
    assert found, "the page quotes no per_sweep path"
    # What each level under ``per_sweep`` may hold. A path may stop early (the
    # page names ``per_sweep.idle.per_phase`` as a container), but every
    # segment it does give must be the one the models define at that level.
    levels: tuple[tuple[str, set[str]], ...] = (
        ("sweep", sweeps),
        ("key", {"per_phase"}),
        ("phase", phases),
        ("key", {"latency"}),
        ("latency field", set(LatencySummary.model_fields)),
    )
    for tail in found:
        segments = tail.rstrip(".").split(".")
        assert len(segments) <= len(levels), (
            f"per_sweep.{tail} is deeper than sweep.per_phase.phase.latency.field"
        )
        for segment, (label, allowed) in zip(segments, levels, strict=False):
            assert segment in allowed, (
                f"in per_sweep.{tail}: {segment!r} is not a valid {label}; "
                f"known: {sorted(allowed)}"
            )


def test_page_does_not_call_the_harness_pool_incapable(runbook: str) -> None:
    """Once the bench admits a ``pool`` provider, the page may not deny it.

    #1913 gave the capacity harness a pool mode. The page went stale inside
    its own review, telling an operator the harness could not reach a pool, so
    the claim is tied to the bench's own provider vocabulary.
    """
    provider = RunMetadata.model_fields["provider"].annotation
    assert "pool" in getattr(provider, "__args__", ()), (
        "the bench no longer admits a pool provider; revisit this page's T19 row"
    )
    lowered = runbook.lower()
    assert "ollama-specific today" not in lowered, (
        "the harness has a pool mode; the page must not call it Ollama-specific"
    )
    assert "pool mode has landed" in lowered, (
        "the page must record that pool mode landed (#1892)"
    )


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


def test_every_required_manifest_field_is_documented(runbook: str) -> None:
    """The manifest table documents all of the schema's required fields.

    :class:`~creek_mcp.model_package.ModelPackageManifest` is ``extra="forbid"`` with
    no defaults, so a manifest built from a partial table does not load at
    all. The table is therefore checked against the model, not eyeballed.
    """
    documented = {
        match
        for line in runbook.splitlines()
        if line.startswith("| `")
        for match in re.findall(r"^\| `([a-z0-9_]+)`", line)
    }
    required = set(ModelPackageManifest.model_fields)
    missing = sorted(required - documented)
    assert not missing, f"manifest table does not document: {missing}"


def test_runtime_version_row_does_not_say_minimum(runbook: str) -> None:
    """The ``runtime_version`` row pins the exact reported version.

    ``pool_identity`` hashes ``runtime_version`` verbatim into the digest that
    ``pool_identity_matches`` compares, and ``_pinned_version`` refuses a
    floating tag, so an operator who writes the *minimum admitted* version
    gets a mismatch and exit 33. The row must not call it a minimum.
    """
    rows = [
        line for line in runbook.splitlines() if line.startswith("| `runtime_version`")
    ]
    assert rows, "the manifest table has no runtime_version row"
    assert "exact" in _cell(rows[0], 2), (
        "the runtime_version row must say the exact reported version"
    )
    assert "pinned minimum version" not in _cell(rows[0], 2), (
        "runtime_version is not the minimum admitted version"
    )


def test_page_invokes_only_registered_console_scripts(runbook: str) -> None:
    """A command the page tells an operator to run must actually exist.

    ``creek-container-health`` is argparse's ``prog``, not an entry point, so
    the procedure quotes the ``python -m`` form instead. Any ``creek-*``
    command the page puts in backticks is checked against the real
    ``[project.scripts]`` table.
    """
    pyproject = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(
        encoding="utf-8"
    )
    registered = set(re.findall(r"^(creek[a-z-]*) = \"", pyproject, re.MULTILINE))
    assert "creek-tools-mcp" in registered, "the entry-point table was not parsed"
    invoked = set(re.findall(r"`(creek-[a-z-]+) --", runbook))
    unregistered = sorted(invoked - registered)
    assert not unregistered, (
        f"the page invokes commands that are not console scripts: {unregistered}; "
        "use the python -m form"
    )


def test_relative_links_resolve(runbook: str) -> None:
    """Every relative link on the page points at a file that exists."""
    targets = re.findall(r"\]\((?!https?://)([^)#]+)", runbook)
    assert targets
    for target in targets:
        assert (_DOCS / target).resolve().exists(), f"dead link: {target}"
