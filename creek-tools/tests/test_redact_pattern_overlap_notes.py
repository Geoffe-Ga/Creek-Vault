"""Keep ``PATTERN_METADATA`` false-positive prose true to the engine.

Issue #901. ``PatternInfo.false_positive_notes`` is prose that nothing
executes: the only assertion on the field anywhere in the tree is
``tests/test_redact.py:776-781``, which checks non-emptiness. A note may
therefore assert *any* engine behaviour and no gate ever contradicts it.
That is exactly what happened to ``stripe_key``, whose note still claimed
that the overlapping ``api_key`` detector and it "both would fire" — true
before #832, false after it, because truly overlapping spans are now
unioned into one region and spliced as a **single** marker.

**Why every guard here reads the runtime value.** The stale claim has
**zero** occurrences in the source text: at HEAD it is split across an
implicit string-concatenation break (``creek/redact/patterns.py:238-239``,
``"...prefix; both " / "would fire, ..."``), so
``git grep -nE 'both would fire' -- creek-tools/ crawdad/ docs/`` returns
nothing. A guard modelled on ``tests/test_redaction_docs_drift.py``'s
Markdown sweep — or on any grep over source lines — would therefore have
shipped **vacuously green on day one against the very defect it exists to
catch**. Every sweep below reads the joined
``PATTERN_METADATA[name].false_positive_notes`` value instead.

**The tie-break these notes describe.** ``_select_marker_name``
(``creek/redact/redactor.py:200-228``) keys on
``(severity rank, span.start - span.end, span.order)`` — most severe, then
**widest**, then collection order, which for the built-in detectors is
``PATTERN_METADATA`` declaration order. For the ``sk_`` pair the framing
"the highest-severity contributor wins" is **false**: ``api_key``,
``stripe_key``, ``anthropic_key`` and ``openai_project_key`` are all
``critical`` and the spans are byte-identical, so severity *and* width tie
and declaration order alone decides. ``test_affected_severities_are_unchanged``
and ``test_api_key_is_declared_before_the_provider_specific_keys`` pin the
substrate that makes that outcome hold.

**Two known limits of the coverage guard**, stated so a reviewer does not
over-trust it:

* Naming another pattern is not the same as making a co-firing claim. Two
  of the six required pairs — ``phone_number -> ssn`` and
  ``openai_project_key -> anthropic_key`` — are forced by a *textual
  mention* ("SSN-like patterns", "Like anthropic_key, ..."), not by a
  co-firing claim. Their rows pin the measured *relation* between the two
  named patterns; they do not adjudicate the prose.
* The extractor matches exact snake_case keys only. A future note writing
  "the Stripe key pattern also fires" evades both halves of the gate:
  ``stripe_key`` is not "Stripe key", and no forbidden literal matches. It
  is a fail-closed heuristic, not a proof that an unmeasured cross-pattern
  claim is unrepresentable. ``discord_bot_token`` is caught only because
  ``jwt`` happens to be lowercase-identical to "JWT".

**Deliberately not rebuilt here** (cited, per issue #901's review):
``tests/test_redact.py:1550-1596`` already parametrises the three provider
regexes over the identical six-prefix list; ``:2211-2221`` already pins the
``email``/``email_password_combo`` merge marker; ``:2716-2730`` already pins
the equal-severity *widest-span* component; ``:2746-2780`` already pins
marker inertness for every metadata name; ``:776-781`` already pins
note non-emptiness and survives this change verbatim.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Final, NamedTuple

import pytest

from creek.config import RedactionConfig
from creek.redact.patterns import PATTERN_METADATA, PatternInfo
from creek.redact.redactor import Redactor
from creek.redact.scanner import RedactionScanner

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

# All-``A``/``B``/``C`` filler runs at roughly 1.5 bits per character, far
# under the 3.7-bit default floor, so ``high_entropy_string`` contributes no
# competing span and ``_snap_to_candidate_runs`` stays a no-op. The wrapper
# below puts a space before the key and a space after it — both outside the
# ``[A-Za-z0-9+/=_-]`` candidate class — so the exact-string assertions
# cannot drift on snapping.
_FILLER: Final[str] = "A" * 24
_LONG_FILLER: Final[str] = "A" * 30

_STRIPE_TEST_KEY: Final[str] = "sk_test_" + _FILLER  # pragma: allowlist secret
_STRIPE_LIVE_KEY: Final[str] = "sk_live_" + _FILLER  # pragma: allowlist secret
_STRIPE_PUBLISHABLE_KEY: Final[str] = "pk_live_" + _FILLER  # pragma: allowlist secret
_STRIPE_RESTRICTED_KEY: Final[str] = "rk_test_" + _FILLER  # pragma: allowlist secret
_ANTHROPIC_KEY: Final[str] = "sk-ant-api03-" + _LONG_FILLER  # pragma: allowlist secret
_OPENAI_KEY: Final[str] = "sk-proj-" + _LONG_FILLER  # pragma: allowlist secret

# Dot-separated triplets whose leading segment decides which detector owns
# them: Discord requires ``[MN]`` at a non-word boundary, ``jwt`` requires
# ``eyJ``. Measured disjoint — neither sample matches the other detector.
_DISCORD_TOKEN: Final[str] = (
    "MZ" + _LONG_FILLER + ".ABCDEF." + "B" * 30  # pragma: allowlist secret
)
_JWT_TOKEN: Final[str] = (
    "eyJ" + "A" * 20 + ".eyJ" + "B" * 20 + "." + "C" * 20  # pragma: allowlist secret
)
_PHONE_SAMPLE: Final[str] = "555-123-4567"
_SSN_SAMPLE: Final[str] = "123-45-6789"

# The one literal this sweep forbids. Prophylactic paraphrases ("both
# fire", "fire independently", "both markers") are deliberately NOT added:
# they are ordinary English, they match zero notes today, and — per the
# ratified reasoning in ``tests/test_redaction_docs_drift.py:13-19`` — a
# grep gate over ordinary English invites a future author to rephrase
# around it rather than correct the claim. The complementary half, "no
# note claims two markers are written", is enforced *behaviourally* by the
# single-marker assertions below, not lexically.
STALE_MERGE_CLAIMS: Final[tuple[str, ...]] = ("both would fire",)

_NOTES: Final[Mapping[str, str]] = {
    name: info.false_positive_notes for name, info in PATTERN_METADATA.items()
}


def _sample(key: str) -> str:
    """Wrap *key* in delimiters outside the high-entropy candidate class.

    Args:
        key: The synthetic key-shaped token to embed.

    Returns:
        The key surrounded by ordinary prose and spaces.
    """
    return f"Sample key {key} here."


def notes_containing(literal: str, notes: Mapping[str, str]) -> list[str]:
    """Names of entries whose joined note contains *literal*, case-folded.

    Reads the JOINED runtime value rather than source lines: at HEAD the
    stale claim is split across an implicit-concatenation break
    (``creek/redact/patterns.py:238-239``, ``"...prefix; both " /
    "would fire, ..."``), so a line-oriented grep gate matches nothing and
    would ship vacuous.

    Args:
        literal: The exact substring to look for, lowercase.
        notes: ``{pattern name: note text}`` to sweep.

    Returns:
        The names whose note contains *literal*, in the order supplied.
    """
    return [name for name, note in notes.items() if literal in note.casefold()]


def notes_naming_other_patterns(
    notes: Mapping[str, str],
    known_names: frozenset[str],
) -> list[tuple[str, str]]:
    """Directed ``(claimant, other)`` pairs a note text creates.

    A note that names another ``PATTERN_METADATA`` key is making a claim
    about the relationship between two detectors. Reads runtime values for
    the same reason :func:`notes_containing` does.

    Args:
        notes: ``{pattern name: note text}`` to sweep.
        known_names: The full set of real pattern names to look for.

    Returns:
        Sorted ``(claimant, other)`` pairs, self-mentions excluded.
    """
    pairs: set[tuple[str, str]] = set()
    for claimant, note in notes.items():
        folded = note.casefold()
        pairs.update(
            (claimant, other)
            for other in known_names
            if other != claimant and other in folded
        )
    return sorted(pairs)


def _redactor() -> tuple[RedactionScanner, Redactor]:
    """Build a default scanner/redactor pair sharing one session salt.

    Returns:
        The scanner and the redactor built from its salt.
    """
    config = RedactionConfig()
    scanner = RedactionScanner(config=config)
    return scanner, Redactor(config=config, salt=scanner.salt)


def _scanned_types(scanner: RedactionScanner, tmp_path: Path, text: str) -> set[str]:
    """Match types ``scan_file`` reports for *text*.

    Args:
        scanner: The scanner to run.
        tmp_path: Directory to write the sample into.
        text: File contents to scan.

    Returns:
        The distinct ``match_type`` values found.
    """
    target = tmp_path / "sample.txt"
    target.write_text(text, encoding="utf-8")
    return {match.match_type for match in scanner.scan_file(target)}


class CoMatchRow(NamedTuple):
    """One measured pair whose spans are identical and merge to one marker.

    Attributes:
        claimant: The specific detector whose note describes the overlap.
        other: The generic detector that also matches the same span.
        key: The synthetic key both detectors match.
        expected_output: The exact whole string ``redact_content`` returns.
    """

    claimant: str
    other: str
    key: str
    expected_output: str


class DisjointRow(NamedTuple):
    """One measured pair that provably never matches the same characters.

    Attributes:
        subject: The detector the sample belongs to.
        other: The detector named in prose that must NOT match it.
        sample: The synthetic token to scan.
    """

    subject: str
    other: str
    sample: str


class StripeOnlyRow(NamedTuple):
    """A Stripe prefix the generic ``api_key`` detector does not reach.

    Attributes:
        key: The synthetic Stripe key.
        expected_output: The exact whole string ``redact_content`` returns.
    """

    key: str
    expected_output: str


_MERGED_API_KEY_OUTPUT: Final[str] = "Sample key [REDACTED:api_key] here."

CO_MATCHING_NOTE_CLAIMS: Final[tuple[CoMatchRow, ...]] = (
    CoMatchRow("stripe_key", "api_key", _STRIPE_TEST_KEY, _MERGED_API_KEY_OUTPUT),
    CoMatchRow("stripe_key", "api_key", _STRIPE_LIVE_KEY, _MERGED_API_KEY_OUTPUT),
    CoMatchRow("anthropic_key", "api_key", _ANTHROPIC_KEY, _MERGED_API_KEY_OUTPUT),
    CoMatchRow("openai_project_key", "api_key", _OPENAI_KEY, _MERGED_API_KEY_OUTPUT),
)

DISJOINT_NOTE_CLAIMS: Final[tuple[DisjointRow, ...]] = (
    DisjointRow("discord_bot_token", "jwt", _DISCORD_TOKEN),
    DisjointRow("jwt", "discord_bot_token", _JWT_TOKEN),
    DisjointRow("phone_number", "ssn", _PHONE_SAMPLE),
    DisjointRow("ssn", "phone_number", _SSN_SAMPLE),
    DisjointRow("openai_project_key", "anthropic_key", _OPENAI_KEY),
    DisjointRow("anthropic_key", "openai_project_key", _ANTHROPIC_KEY),
)

STRIPE_ONLY_PREFIXES: Final[tuple[StripeOnlyRow, ...]] = (
    StripeOnlyRow(_STRIPE_PUBLISHABLE_KEY, "Sample key [REDACTED:stripe_key] here."),
    StripeOnlyRow(_STRIPE_RESTRICTED_KEY, "Sample key [REDACTED:stripe_key] here."),
)

# The provider-specific detectors whose notes describe merging into
# ``api_key``'s marker. All four must stay ``critical`` or the tie-break
# the notes assert stops being a declaration-order tie-break.
_CRITICAL_MERGE_PARTICIPANTS: Final[tuple[str, ...]] = (
    "api_key",
    "stripe_key",
    "anthropic_key",
    "openai_project_key",
)


class TestStaleMergeClaimSweep:
    """The lexical half: no note may reassert the pre-#832 both-fire claim."""

    @pytest.mark.parametrize("literal", STALE_MERGE_CLAIMS)
    def test_the_stale_claim_detector_fires_on_a_synthetic_note(
        self,
        literal: str,
    ) -> None:
        """The matcher flags a note carrying the literal and no other.

        The non-vacuity control for the sweep below. Without it, a matcher
        that had stopped matching would report a clean metadata table
        forever — and that risk is concrete here, since the first thing
        measured about this literal is that the obvious line-based matcher
        does *not* find it.

        Args:
            literal: One forbidden claim.
        """
        names_it = PatternInfo(
            pattern=re.compile(r"synthetic-never-matches-anything"),
            description="Synthetic entry carrying the forbidden claim.",
            severity="low",
            false_positive_notes=(
                f"The other pattern overlaps here; {literal}, which is fine."
            ),
        )
        clean = PatternInfo(
            pattern=re.compile(r"synthetic-never-matches-anything-either"),
            description="Synthetic entry with prose that claims nothing.",
            severity="low",
            false_positive_notes="Overlapping spans are unioned into one marker.",
        )

        flagged = notes_containing(
            literal,
            {
                "synthetic-names-it": names_it.false_positive_notes,
                "synthetic-clean": clean.false_positive_notes,
            },
        )

        assert flagged == ["synthetic-names-it"], (
            f"the detector for {literal!r} did not behave: it should flag "
            "the one synthetic note that carries the claim and leave the "
            f"clean one alone, but it returned {flagged!r}."
        )

    @pytest.mark.parametrize("literal", STALE_MERGE_CLAIMS)
    def test_no_pattern_note_claims_two_patterns_both_fire(
        self,
        literal: str,
    ) -> None:
        """No ``false_positive_notes`` value reasserts the pre-#832 claim.

        Args:
            literal: One forbidden claim.
        """
        offenders = notes_containing(literal, _NOTES)

        assert offenders == [], (
            f"{len(offenders)} PATTERN_METADATA entr(y/ies) still claim two "
            f"patterns each write their own marker: {', '.join(offenders)}. "
            "Since #832 truly overlapping matches are unioned into one "
            "region and spliced as a single marker "
            "(creek/redact/redactor.py:_merge_spans, :_splice_markers). For "
            "the sk_ pair both contributors are `critical` with identical "
            "spans, so severity AND width tie and the label falls to "
            "PATTERN_METADATA declaration order -- the marker is "
            "[REDACTED:api_key]. Correct the note to describe the union; do "
            "not change the pattern to match the prose."
        )


class TestCrossPatternClaimCoverage:
    """The structural half: a note naming another detector needs a row."""

    def test_the_cross_reference_extractor_reads_runtime_note_values(self) -> None:
        """The extractor finds a named pattern and ignores a clean note.

        The non-vacuity control for the coverage guard.
        """
        flagged = notes_naming_other_patterns(
            {
                "synthetic-names-it": "Overlaps with the api_key pattern.",
                "synthetic-clean": "Random digit sequences in changelogs.",
            },
            frozenset(PATTERN_METADATA),
        )

        assert flagged == [("synthetic-names-it", "api_key")], (
            "the cross-reference extractor did not behave: it should report "
            "exactly the synthetic note that names api_key and nothing for "
            f"the clean one, but it returned {flagged!r}."
        )

    def test_every_cross_pattern_note_claim_has_a_measured_row(self) -> None:
        """Every ``(claimant, other)`` pair in prose is measured somewhere.

        Fail-closed: adding a note that names another detector without
        adding a behaviour row reddens this test. ``required <= covered``
        rather than equality, so extra measured rows are always legal.
        """
        required = set(notes_naming_other_patterns(_NOTES, frozenset(PATTERN_METADATA)))
        covered = {(row.claimant, row.other) for row in CO_MATCHING_NOTE_CLAIMS}
        covered |= {(row.subject, row.other) for row in DISJOINT_NOTE_CLAIMS}

        missing = sorted(required - covered)

        assert not missing, (
            "these false_positive_notes name another PATTERN_METADATA "
            f"pattern with no measured behaviour row: {missing}. Add a row "
            "to CO_MATCHING_NOTE_CLAIMS (if the two detectors really do "
            "match the same span) or to DISJOINT_NOTE_CLAIMS (if they "
            "cannot), so the prose is backed by a measurement rather than "
            "by an author's recollection."
        )

    def test_the_required_pairs_are_the_six_measured_at_head(self) -> None:
        """The prose claims exactly the six pairs this module measures.

        A regression alarm on the *population*: if a note starts or stops
        naming another detector, this fires and the author must decide
        deliberately rather than letting the coverage guard absorb it.
        """
        required = notes_naming_other_patterns(_NOTES, frozenset(PATTERN_METADATA))

        assert required == [
            ("anthropic_key", "api_key"),
            ("discord_bot_token", "jwt"),
            ("openai_project_key", "anthropic_key"),
            ("openai_project_key", "api_key"),
            ("phone_number", "ssn"),
            ("stripe_key", "api_key"),
        ], (
            "the set of cross-pattern mentions in false_positive_notes has "
            f"changed; it is now {required!r}. Two of the six are forced by "
            "a textual mention rather than a co-firing claim "
            "(phone_number->ssn, openai_project_key->anthropic_key); see "
            "this module's docstring."
        )

    def test_every_table_row_names_real_patterns(self) -> None:
        """No row is a typo: every name is a real ``PATTERN_METADATA`` key."""
        named: set[str] = set()
        for co_row in CO_MATCHING_NOTE_CLAIMS:
            named.update({co_row.claimant, co_row.other})
        for disjoint_row in DISJOINT_NOTE_CLAIMS:
            named.update({disjoint_row.subject, disjoint_row.other})

        unknown = sorted(named - set(PATTERN_METADATA))

        assert not unknown, (
            f"behaviour-table rows name patterns that do not exist: "
            f"{unknown}. A typo here would silently satisfy the coverage "
            "guard while measuring nothing."
        )


class TestMergedMarkerBehaviour:
    """The measured behaviour the rewritten notes describe."""

    @pytest.mark.parametrize(
        "row",
        CO_MATCHING_NOTE_CLAIMS,
        ids=lambda row: f"{row.claimant}-{row.key[:8]}",
    )
    def test_co_matching_pair_merges_into_one_marker(
        self,
        row: CoMatchRow,
        tmp_path: Path,
    ) -> None:
        """Both detectors report on ``--scan``; ``--apply`` writes one marker.

        Args:
            row: The measured pair under test.
            tmp_path: Pytest-provided scratch directory.
        """
        scanner, redactor = _redactor()
        content = _sample(row.key)

        scanned = _scanned_types(scanner, tmp_path, content)
        assert {row.claimant, row.other}.issubset(scanned), (
            f"scan_file should report both {row.claimant} and {row.other} "
            f"for this sample, but reported {sorted(scanned)}. The provider "
            "notes' report-time clause depends on this."
        )

        result = redactor.redact_content(content)

        assert result == row.expected_output, (
            f"the merged region for {row.claimant}/{row.other} should be "
            f"spliced as {row.expected_output!r}, got {result!r}."
        )
        assert result.count("[REDACTED:") == 1, (
            "the two identical spans must be unioned into exactly one "
            f"marker (#832), but {result!r} carries "
            f"{result.count('[REDACTED:')}."
        )

    @pytest.mark.parametrize(
        "row",
        STRIPE_ONLY_PREFIXES,
        ids=lambda row: row.key[:8],
    )
    def test_stripe_only_prefixes_carry_the_stripe_marker(
        self,
        row: StripeOnlyRow,
        tmp_path: Path,
    ) -> None:
        """``pk_``/``rk_`` keys do not overlap ``api_key`` at all.

        The carve-out the rewritten ``stripe_key`` note states is not
        decoration: the generic detector's ``sk[-_]`` alternation cannot
        reach these prefixes, so ``stripe_key`` labels the region alone.

        Args:
            row: The Stripe-only prefix under test.
            tmp_path: Pytest-provided scratch directory.
        """
        scanner, redactor = _redactor()
        content = _sample(row.key)

        scanned = _scanned_types(scanner, tmp_path, content)
        assert "api_key" not in scanned, (
            f"api_key must not reach the {row.key[:8]} prefix, but scan_file "
            f"reported {sorted(scanned)}."
        )

        assert redactor.redact_content(content) == row.expected_output

    @pytest.mark.parametrize(
        "row",
        DISJOINT_NOTE_CLAIMS,
        ids=lambda row: f"{row.subject}-not-{row.other}",
    )
    def test_disjoint_pair_never_co_matches(
        self,
        row: DisjointRow,
        tmp_path: Path,
    ) -> None:
        """A sample owned by one detector is invisible to the other.

        Args:
            row: The measured disjoint pair under test.
            tmp_path: Pytest-provided scratch directory.
        """
        scanner, _ = _redactor()

        scanned = _scanned_types(scanner, tmp_path, _sample(row.sample))

        assert row.subject in scanned, (
            f"{row.subject} should match its own sample, but scan_file "
            f"reported {sorted(scanned)}."
        )
        assert row.other not in scanned, (
            f"{row.subject} and {row.other} are documented as mutually "
            f"exclusive, but both matched: {sorted(scanned)}."
        )


class TestMarkerTieBreakSubstrate:
    """The metadata facts the rewritten notes' tie-break depends on."""

    @pytest.mark.parametrize("name", _CRITICAL_MERGE_PARTICIPANTS)
    def test_affected_severities_are_unchanged(self, name: str) -> None:
        """All four merge participants stay ``critical``.

        ``test_metadata_has_valid_severity`` only checks membership in
        ``_VALID_SEVERITIES``; the notes here need the specific value,
        because it is what makes the tie-break fall through to width and
        then to declaration order.

        Args:
            name: One participant in the ``sk_`` merge.
        """
        assert PATTERN_METADATA[name].severity == "critical", (
            f"{name} is no longer `critical`. The stripe_key, anthropic_key "
            "and openai_project_key notes state that severity TIES and the "
            "marker falls to declaration order; a different severity would "
            "make that prose false."
        )

    @pytest.mark.parametrize(
        "name",
        ("stripe_key", "anthropic_key", "openai_project_key"),
    )
    def test_api_key_is_declared_before_the_provider_specific_keys(
        self,
        name: str,
    ) -> None:
        """``api_key`` precedes each provider key in declaration order.

        Args:
            name: One provider-specific detector.
        """
        order = list(PATTERN_METADATA)

        assert order.index("api_key") < order.index(name), (
            f"api_key must be declared before {name} in PATTERN_METADATA. "
            "That relative order IS the [REDACTED:api_key] tie-break the "
            "notes assert: _select_marker_name keys on (severity, widest "
            "span, collection order), severity and width both tie for this "
            "pair, so declaration order alone decides the label."
        )


class TestNoteProse:
    """What the three edited notes must say, and what must not move."""

    def test_stripe_note_names_both_prefixes_and_the_carve_out(self) -> None:
        """``stripe_key``'s note covers both overlapping prefixes and more.

        At HEAD the note names only ``sk_test_`` and claims both detectors
        fire. The corrected note must name ``sk_live_`` too, state the
        ``pk_``/``rk_`` carve-out, and give the real marker.
        """
        note = PATTERN_METADATA["stripe_key"].false_positive_notes

        for fragment in (
            "sk_test_",
            "sk_live_",
            "pk_",
            "rk_",
            "[REDACTED:api_key]",
            "declaration order",
        ):
            assert fragment in note, (
                f"stripe_key's note must mention {fragment!r} but does not: "
                f"{note!r}. The api_key alternation `sk[-_][a-zA-Z0-9_-]"
                "{20,}` reaches sk_live_ exactly as it reaches sk_test_, "
                "and reaches neither pk_ nor rk_."
            )

    @pytest.mark.parametrize(
        ("name", "surviving_clause"),
        (
            ("anthropic_key", "surfaces clearer telemetry in the report"),
            ("openai_project_key", "distinguishes provider in reports"),
        ),
    )
    def test_provider_notes_state_the_apply_marker(
        self,
        name: str,
        surviving_clause: str,
    ) -> None:
        """The provider notes gain the apply fact without losing the true one.

        The report-time clause is measured TRUE (``scan_file`` lists both
        names), so the edit must append rather than replace. This test
        passes at HEAD on the first assertion and fails on the second,
        which is what proves the edit was an addition.

        Args:
            name: The provider-specific detector.
            surviving_clause: The true report-time clause that must survive.
        """
        note = PATTERN_METADATA[name].false_positive_notes

        assert surviving_clause in note, (
            f"{name}'s note lost its true report-time clause "
            f"{surviving_clause!r}; scan_file really does list both names. "
            f"Note is now {note!r}."
        )
        assert "[REDACTED:api_key]" in note, (
            f"{name}'s note must also state what `--apply` writes: the two "
            "identical spans merge and a single [REDACTED:api_key] marker "
            f"is spliced. Note is now {note!r}."
        )

    def test_jwt_note_is_byte_identical_to_head(self) -> None:
        """``jwt``'s note is untouched — the issue is wrong to list it.

        It contains no pattern name, no overlap word and no firing verb,
        so #832 invalidated nothing in it.
        """
        assert (
            PATTERN_METADATA["jwt"].false_positive_notes
            == "Long base64-encoded strings with dots."
        )

    def test_discord_note_is_byte_identical_to_head(self) -> None:
        """``discord_bot_token``'s note is untouched — it claims exclusivity.

        "the JWT pattern overlaps **but uses an `eyJ` prefix instead of**
        `M`/`N`" is a mutual-exclusivity claim, never a both-fire claim,
        and ``test_disjoint_pair_never_co_matches`` measures it true.
        """
        assert PATTERN_METADATA["discord_bot_token"].false_positive_notes == (
            "Other dotted base64url-ish triplets; the JWT pattern overlaps "
            "but uses an `eyJ` prefix instead of `M`/`N`."
        )
