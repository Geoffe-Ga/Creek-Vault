"""Redactor — replace sensitive data with safe markers.

The :class:`Redactor` re-scans content to locate match positions (since
:class:`RedactionMatch` intentionally does **not** store the matched text)
and replaces each hit with a ``[REDACTED:type]`` marker.

Replacement is a single pass over the *original* content: every in-scope
detector — the regex patterns *and* the generic high-entropy detector
alike — is matched against the untouched text, truly overlapping spans
are unioned, and the merged spans are spliced out right-to-left. Because
no detector ever anchors on partially redacted text, a ``--scan`` finding
cannot survive a ``--apply`` step (Issue #832). Keeping the entropy
detector inside that same union is what stops a regex match that covers
only *part* of a high-entropy run from leaving the remainder — now too
short for the detector to re-find — in cleartext (Issue #909).

The coverage is layered, and each layer is needed. The entropy detector
itself has two gates: it fires when the *whole* run clears the
confidence-derived threshold, and also when any contiguous
``HIGH_ENTROPY_MIN_RUN``-character window of the run does. The second
gate exists because concatenation averages — predictable filler glued to
a genuine secret used to drag the whole-run average below the bar and
hide the secret outright (Issue #942).

Even both entropy gates together are not sufficient, because a run can
fail both and still need covering. Twenty opaque hex characters glued to
a phone number — ``deadbeefdeadbeefdead-555-123-4567`` — is one 33-char
run measuring 3.327090 bits/char whole-run with a best 20-char window of
3.346439, so both gates are inert for every ``min_confidence`` above
0.4232196723355077, the shipped default of 0.6 included. The entropy
detector contributes no span at all, yet ``phone_number`` matches only
the last twelve characters and would leave a 20-character opaque token
in cleartext. Raising ``min_confidence`` to cut false positives widens
that class of run. So
spans are also **snapped to token boundaries** before merging, as the
threshold-independent backstop (Issue #909): a span whose edge falls
strictly inside a ``HIGH_ENTROPY_CANDIDATE`` run is widened to that
run's edge, making the last layer boundary-driven rather than
threshold-driven. Runs the operator explicitly allowlisted are excluded
from snapping: user intent wins, and a regex match inside such a run
still redacts its own span unwidened.

A consequence worth stating plainly: ``--apply`` may therefore redact
slightly **more** than ``--scan`` reported, because snapping widens on
token shape rather than on a reported finding. That asymmetry is
deliberate, fail-closed behaviour — a missed secret is unrecoverable
once written, whereas over-redaction is visible in the output and
fixable via ``false_positive_allowlist``.

One class of run is exempt from all three of those layers: a run that is
byte-equal to a run inside a marker *this instance's own configuration*
renders, found at that marker's exact offset. Without the exemption the
tool corrupted its own output — the run ``email_password_combo`` inside
``[REDACTED:email_password_combo]`` cleared the entropy bar at any
``min_confidence`` at or below 0.5920918598895946 and the marker was
rewritten to ``[REDACTED:[REDACTED:high_entropy_string]]``, nesting once
more on every further ``--apply``; and independently of the threshold, a
span bisecting that run snapped out onto the marker and swallowed it
(Issue #945). Both routes, and the identical one in the scanner, now go
through the single candidacy gate
:func:`~creek.redact.scanner.iter_unmarked_candidates`.

The exemption is **name-keyed, not shape-keyed**, and that distinction
is the whole security argument: nothing about the ``[REDACTED:...]``
form is privileged, so a forged ``[REDACTED:<real secret>]`` is still a
candidate and still redacted. Two consequences are deliberate non-goals
rather than oversights — the 21 regex detectors keep firing inside
marker text (suppressing them is what would let a forged marker hide a
live key), and an operator ``custom_patterns`` regex that matches its
own marker still self-matches.
"""

import bisect
import re
from typing import NamedTuple

from creek.config import RedactionConfig
from creek.redact.patterns import PATTERN_METADATA, REDACTION_PATTERNS
from creek.redact.scanner import (
    HIGH_ENTROPY_PATTERN_NAME,
    emitted_marker_runs,
    entropy_threshold,
    has_high_entropy_region,
    iter_unmarked_candidates,
    post_validate,
)

_SEVERITY_RANKS: dict[str, int] = {
    "critical": 0,
    "high": 1,
    "medium": 2,
    "low": 3,
}
"""Marker-selection rank per metadata severity (lower = more severe)."""

_UNKNOWN_SEVERITY_RANK: int = len(_SEVERITY_RANKS)
"""Rank for patterns without metadata — sorts below every known severity."""


class _Span(NamedTuple):
    """A single pattern match located against the original content.

    Attributes:
        start: Offset of the first matched character.
        end: Offset one past the last matched character.
        pattern_name: The pattern key that produced the match.
        order: Collection order, used as the final marker tie-break.
    """

    start: int
    end: int
    pattern_name: str
    order: int


class _MergedSpan(NamedTuple):
    """A maximal union of truly overlapping match spans.

    Attributes:
        start: Offset where the merged region begins.
        end: Offset one past the merged region's last character.
        contributors: The individual matches folded into this region.
    """

    start: int
    end: int
    contributors: list[_Span]


_Run = tuple[int, int]
"""Half-open ``(start, end)`` offsets of one high-entropy candidate run."""


def _containing_run(
    runs: list[_Run],
    starts: list[int],
    offset: int,
) -> _Run | None:
    """Find the one candidate run *offset* falls strictly inside.

    Equivalence to the linear scan this replaces is a consequence of the
    run list's shape, not a claim about it. ``re.finditer`` yields
    non-overlapping matches in document order, and the two filters
    applied downstream of it — the false-positive allowlist and the
    emitted-marker carve-out — only ever *remove* members. So ``runs``
    is sorted by start and pairwise disjoint, and therefore **at most
    one** run can satisfy ``run_start < offset < run_end``. The previous
    "scan every run, last match wins" loop and this single lookup can
    only ever disagree if two runs both contained *offset*, which
    disjointness forbids; the precondition itself is pinned by
    ``tests/test_redact.py::TestSnapCostAndCorrectness::
    test_candidate_runs_are_sorted_and_disjoint``.

    ``bisect_right`` returns the insertion point after every start at or
    below *offset*, so the run one place before it is the last run that
    could possibly contain *offset*; the strict containment test then
    rejects the case where *offset* is at or past that run's end.

    Args:
        runs: Half-open ``(start, end)`` offsets, sorted and disjoint.
        starts: The start offsets of *runs*, in the same order — built
            once per call so the bisect is O(log R) rather than O(R).
        offset: The span edge to locate.

    Returns:
        The containing run, or ``None`` when *offset* falls outside
        every run or lands exactly on a run boundary.
    """
    index = bisect.bisect_right(starts, offset) - 1
    if index < 0:
        return None
    run_start, run_end = runs[index]
    if run_start < offset < run_end:
        return (run_start, run_end)
    return None


def _snap_one(span: _Span, runs: list[_Run], starts: list[int]) -> _Span:
    """Widen *span* to the boundaries of any candidate run it bisects.

    A boundary is only moved when the span's edge falls *strictly*
    inside a run, so a span already flush with a run boundary — or one
    that fully contains the run — is returned unchanged. A zero-width
    span strictly inside a run has *both* edges moved and so becomes the
    whole run. The pattern name and collection order are preserved
    verbatim so the merged span's marker selection is unaffected.

    Args:
        span: The match span to snap.
        runs: Half-open offsets of the candidate runs to snap against,
            sorted and disjoint.
        starts: The start offsets of *runs*, in the same order.

    Returns:
        The span, widened to whole-token boundaries where it bisected a
        run; otherwise an equal copy of the input.
    """
    start_run = _containing_run(runs, starts, span.start)
    end_run = _containing_run(runs, starts, span.end)
    return _Span(
        span.start if start_run is None else start_run[0],
        span.end if end_run is None else end_run[1],
        span.pattern_name,
        span.order,
    )


def _severity_rank(pattern_name: str) -> int:
    """Rank a pattern for marker selection (lower = more severe).

    Args:
        pattern_name: The pattern key to rank.

    Returns:
        The rank of the pattern's metadata severity, or
        :data:`_UNKNOWN_SEVERITY_RANK` when the pattern (e.g. a custom
        one) has no metadata entry.
    """
    info = PATTERN_METADATA.get(pattern_name)
    if info is None:
        return _UNKNOWN_SEVERITY_RANK
    return _SEVERITY_RANKS.get(info.severity, _UNKNOWN_SEVERITY_RANK)


def _iter_lines_with_offsets(content: str) -> list[tuple[int, str]]:
    """Yield each line of *content* with its document offset.

    Mirrors ``RedactionScanner``'s ``text.splitlines()`` walk exactly -- the
    line bodies are what ``splitlines()`` would return, terminators excluded --
    while also reporting where each line begins, so a per-line match can be
    stored in document coordinates (#900).

    Args:
        content: The original, untouched text.

    Returns:
        ``(offset, line)`` pairs in document order.
    """
    windows: list[tuple[int, str]] = []
    offset = 0
    for raw in content.splitlines(keepends=True):
        bodies = raw.splitlines()
        windows.append((offset, bodies[0] if bodies else ""))
        offset += len(raw)
    return windows


def _merge_spans(spans: list[_Span]) -> list[_MergedSpan]:
    """Union truly overlapping spans into maximal merged regions.

    Spans that merely touch (one starting exactly where the previous
    ends) stay separate so adjacent distinct findings keep their own
    markers; only genuine overlap (``start < previous end``) merges.

    Args:
        spans: Match spans collected against the original content.

    Returns:
        Non-overlapping merged spans ordered by start offset.
    """
    merged: list[_MergedSpan] = []
    for span in sorted(spans, key=lambda s: s.start):
        if merged and span.start < merged[-1].end:
            last = merged[-1]
            merged[-1] = _MergedSpan(
                start=last.start,
                end=max(last.end, span.end),
                contributors=[*last.contributors, span],
            )
        else:
            merged.append(_MergedSpan(span.start, span.end, [span]))
    return merged


def _select_marker_name(contributors: list[_Span]) -> str:
    """Choose which pattern name labels a merged span's marker.

    The most severe contributing pattern wins; ties fall to the widest
    individual span, then to whichever match was collected first.

    Args:
        contributors: The matches folded into one merged span.

    Returns:
        The winning pattern name for the replacement marker.
    """

    def _key(span: _Span) -> tuple[int, int, int]:
        """Build the sort key selecting the winning contributor via ``min``.

        Args:
            span: A contributing span to rank.

        Returns:
            Tuple of severity rank, negated width, and collection order.
        """
        return (
            _severity_rank(span.pattern_name),
            span.start - span.end,
            span.order,
        )

    return min(contributors, key=_key).pattern_name


class Redactor:
    """Replace sensitive data in text with safe markers.

    Because :class:`RedactionMatch` never stores matched text, the
    redactor must re-scan content using the same patterns to locate
    replacement positions.

    Args:
        config: Redaction configuration (allowlist, custom patterns).
        salt: The session salt used by the scanner that produced the
            matches, kept on the instance so hashes from that same
            scan session can be correlated.
    """

    def __init__(self, config: RedactionConfig, salt: bytes) -> None:
        """Initialise the redactor with config and session salt.

        Args:
            config: Redaction configuration.
            salt: Session salt from the corresponding scanner.
        """
        self.config = config
        self.salt = salt
        self._allowlist = frozenset(config.false_positive_allowlist)
        self._patterns = self._build_patterns()
        self._marker_runs = emitted_marker_runs(
            config.replacement_template,
            frozenset(self._patterns) | {HIGH_ENTROPY_PATTERN_NAME},
        )

    def _build_patterns(self) -> dict[str, re.Pattern[str]]:
        """Merge built-in patterns with any custom patterns from config.

        Returns:
            Combined dictionary of pattern name to compiled regex.
        """
        patterns: dict[str, re.Pattern[str]] = REDACTION_PATTERNS.copy()
        for name, raw in self.config.custom_patterns.items():
            patterns[name] = re.compile(raw)
        return patterns

    def _is_allowlisted(self, text: str) -> bool:
        """Check whether *text* appears in the false-positive allowlist.

        Membership is tested against :attr:`_allowlist`, the frozenset
        snapshot taken in :meth:`__init__`, because this predicate runs
        once per regex match *and* once per candidate run: against the
        configured ``list[str]`` that was an O(allowlist) scan on every
        one of them. The snapshot is taken at construction — the same
        idiom :attr:`_patterns` and :attr:`_marker_runs` already use —
        because :class:`~creek.config.RedactionConfig` is a non-frozen
        ``BaseModel`` and could otherwise be mutated mid-scan. Semantics
        are unchanged: ``in`` on a ``frozenset`` of ``str`` is the same
        exact-equality test as ``in`` on a ``list`` of ``str``.

        The mirror of this method,
        :meth:`creek.redact.scanner.RedactionScanner._is_allowlisted`,
        snapshots identically, so ``--scan`` and ``--apply`` cannot
        disagree about the allowlist.

        Args:
            text: The matched string to check.

        Returns:
            ``True`` if the string should be excluded from redaction.
        """
        return text in self._allowlist

    def redact_content(
        self,
        content: str,
        pattern_types: list[str] | None = None,
    ) -> str:
        """Replace sensitive data in *content* with ``[REDACTED:type]`` markers.

        Re-scans *content* against the configured patterns (since matched
        text is never stored in :class:`RedactionMatch`) in a single pass
        over the original text: every in-scope detector is matched against
        the untouched content, truly overlapping spans are unioned, and
        each merged region is replaced by the marker of its most severe
        contributor. This guarantees scan/apply parity — when
        *pattern_types* is not narrowed, a ``--scan`` finding cannot
        survive ``--apply``, because no detector ever anchors on
        already-redacted text (Issue #832). Narrowing *pattern_types*
        deliberately takes the excluded detectors out of scope, so their
        findings are left untouched by design.

        The generic high-entropy detector participates in that *same*
        single-pass union rather than running as a post-pass over the
        spliced output. Otherwise a regex match covering only part of a
        high-entropy run would splice its marker over that part and leave
        a remainder too short for ``HIGH_ENTROPY_CANDIDATE`` to re-match,
        leaking the tail of a secret in cleartext (Issue #909).

        Because that union still depends on the *combined* run clearing
        the entropy threshold, spans are additionally snapped to token
        boundaries by :meth:`_snap_to_candidate_runs`: any span edge
        falling strictly inside a ``HIGH_ENTROPY_CANDIDATE`` run is
        pushed out to that run's edge, so no contiguous token is ever
        left half-redacted regardless of how ``min_confidence`` is
        tuned. Allowlisted runs are exempt — an explicitly allowlisted
        token is never widened onto.

        Snapping is shape-driven, so ``--apply`` may redact slightly
        **more** than ``--scan`` reported. That is deliberate,
        fail-closed behaviour: a missed secret is unrecoverable once
        written, while over-redaction is visible and fixable by
        allowlisting the affected token.

        This method is **idempotent** at every ``min_confidence``:
        re-running it over its own output returns that output byte for
        byte, because a run belonging to a marker this instance renders
        is not a candidate for the entropy detector or for snapping
        (Issue #945). The guarantee holds for a
        :pyattr:`RedactionConfig.replacement_template` whose ``{name}``
        is delimited on both sides by a character outside
        ``[A-Za-z0-9+/=_-]`` — the default ``[REDACTED:{name}]`` is.
        Under a degenerate template a spliced marker can end up flush
        against neighbouring token characters, forming a NEW longer run
        that is correctly *not* exempt: fail-closed, but not a fixed
        point.

        Args:
            content: The text to redact.
            pattern_types: If provided, only apply these pattern names.
                Defaults to all configured patterns.

        Returns:
            A copy of *content* with sensitive data replaced.
        """
        patterns_to_use: dict[str, re.Pattern[str]]
        if pattern_types is not None:
            patterns_to_use = {
                k: v for k, v in self._patterns.items() if k in pattern_types
            }
        else:
            patterns_to_use = self._patterns

        spans = self._collect_spans(content, patterns_to_use)
        if self._should_apply_high_entropy(pattern_types):
            # One sweep, two consumers. The entropy collector and the
            # snapper need the identical run list, and building it twice
            # cost a second full regex pass over the content as well as a
            # second chance for the two candidacy rules to drift apart.
            runs = self._candidate_runs(content)
            spans.extend(self._collect_high_entropy_spans(content, len(spans), runs))
            spans = self._snap_to_candidate_runs(spans, runs)

        return self._splice_markers(content, _merge_spans(spans))

    def _candidate_runs(self, content: str) -> list[_Run]:
        """Locate every high-entropy candidate run in *content*, once.

        This is the single place the redactor obtains candidate runs, and
        it applies both exclusions exactly once: the emitted-marker
        carve-out, which lives inside
        :func:`~creek.redact.scanner.iter_unmarked_candidates` (Issue
        #945), and the false-positive allowlist. Both the entropy
        collector and the snapping step consume the result, so neither
        can apply a different candidacy rule from the other, and
        ``redact_content`` pays one regex sweep instead of two.

        The allowlist is keyed on the **whole maximal run** here, not on
        a regex match: an allowlisted run is skipped, which is what makes
        an allowlist entry an exemption from token-boundary widening as
        well as from the entropy detector.

        Args:
            content: The original, untouched text.

        Returns:
            Half-open ``(start, end)`` offsets of every run that is not
            allowlisted and not part of a marker this configuration
            renders — sorted by start and pairwise disjoint, because
            ``re.finditer`` yields non-overlapping matches in document
            order and both filters only remove members.
        """
        return [
            (candidate.start(), candidate.end())
            for candidate in iter_unmarked_candidates(
                content, marker_runs=self._marker_runs
            )
            if not self._is_allowlisted(candidate.group())
        ]

    def _snap_to_candidate_runs(
        self,
        spans: list[_Span],
        runs: list[_Run],
    ) -> list[_Span]:
        """Widen every span that bisects a contiguous high-entropy run.

        The entropy detector emits a span when the *whole* run clears
        :func:`~creek.redact.scanner.entropy_threshold` or when any
        contiguous ``HIGH_ENTROPY_MIN_RUN``-character window of it does
        (Issue #942) — but a run can fail *both* of those gates and still
        need covering. ``deadbeefdeadbeefdead-555-123-4567`` is one
        33-char run measuring 3.327090 bits/char whole-run with a best
        20-char window of 3.346439, so both gates are inert for every
        :pyattr:`RedactionConfig.min_confidence` above
        0.4232196723355077 — the shipped default of 0.6 included. The
        detector contributes no span, and ``phone_number`` matches only
        the trailing twelve characters, so without snapping a
        20-character opaque token survives in cleartext; raising
        ``min_confidence`` widens that class of run. Snapping is the
        layer beneath both entropy gates, making that coverage
        boundary-driven instead of threshold-driven (Issue #909).

        Because the rule is boundary-driven it is deliberately **not**
        scoped to a subset of detectors. A "self-delimiting" pattern such
        as ``phone_number`` or ``ipv4`` is bounded against bisecting *its
        own* value; that says nothing about the rest of the run, which is
        where the fixture above hides its secret. Exempting such
        detectors was asked for and refused — see ADR-0014.

        Two independent exclusions apply to the run list, and both must
        hold for a run to be snapped onto:

        - runs on the false-positive allowlist, so an explicitly
          allowlisted token is never widened onto — user intent wins. A
          regex match *inside* such a run still redacts its own span,
          just unwidened;
        - runs belonging to a marker this redactor's own configuration
          renders (#945). This is the **one place** the fix reduces
          fail-closed coverage, and it is stated plainly rather than
          buried: a span that bisects a marker's run is no longer widened
          onto the marker. Before, an operator ``custom_patterns`` match
          landing inside ``[REDACTED:email_password_combo]`` snapped out
          to the whole run and swallowed the marker into
          ``[REDACTED:[REDACTED:inner]]`` — reachable even at
          ``min_confidence=1.0``, where the entropy detector is provably
          inert. The reduction is bounded by construction: the exempt
          bytes are a pattern *name* inside a literal marker at an exact
          offset, never operator data, and because
          ``HIGH_ENTROPY_CANDIDATE`` matches maximally a secret adjacent
          to a marker forms a longer run that fails byte-equality and is
          still snapped onto. See :func:`iter_unmarked_candidates`.

        Cost is O(S log R) in the number of spans and runs: the run
        starts are extracted once and each span edge is placed with a
        single :func:`bisect.bisect_right`. Comparing every span against
        every run — which is what this did before Issue #946 — made a
        token-dense file quadratic on a code path that runs on every
        ``redact --apply``.

        Args:
            spans: Match spans collected against the original content.
            runs: The shared candidate runs from :meth:`_candidate_runs`,
                sorted and disjoint. Passed in rather than rebuilt so
                this step and the entropy collector cannot diverge, and
                so the content is swept once per call.

        Returns:
            The spans with bisecting edges pushed out to whole-token
            boundaries; overlaps this creates are absorbed by
            :func:`_merge_spans`.
        """
        starts = [run_start for run_start, _ in runs]
        return [_snap_one(span, runs, starts) for span in spans]

    def _collect_spans(
        self,
        content: str,
        patterns: dict[str, re.Pattern[str]],
    ) -> list[_Span]:
        """Locate every non-allowlisted, validated match in *content*.

        Args:
            content: The original, untouched text.
            patterns: In-scope mapping of pattern name to compiled regex.

        Returns:
            Spans for every match that survives the allowlist and the
            pattern-specific post-validator (e.g. Luhn for
            ``credit_card``), in collection order.

        Two passes, unioned, because the scanner and the redactor were
        matching different things (#900). ``RedactionScanner.scan_file``
        walks ``text.splitlines()`` and matches **per line**; this method
        matched the **whole document**. Any pattern whose whitespace can
        cross a newline diverges between the two, and the divergence leaks:
        on ``"password =\\npassword = s3cret"`` the built-in ``password``
        pattern matches ``"password =\\npassword"`` whole-document -- the
        span ENDS before the secret -- so ``--scan`` reported a critical
        finding that ``--apply`` then wrote straight back out.

        It is a union rather than a swap. A per-line walk alone cannot see a
        match that legitimately spans a newline, so replacing the
        whole-document pass would trade this parity gap for the mirror-image
        one, in the direction that leaks. Duplicate matches (any single-line
        match is found by both passes) are dropped by offset+name, and
        genuine overlaps are already unioned downstream by
        :func:`_merge_spans`.
        """
        spans: list[_Span] = []
        seen: set[tuple[int, int, str]] = set()
        self._record_matches(spans, seen, content, patterns, offset=0)
        for offset, line in _iter_lines_with_offsets(content):
            self._record_matches(spans, seen, line, patterns, offset=offset)
        return spans

    def _record_matches(
        self,
        spans: list[_Span],
        seen: set[tuple[int, int, str]],
        text: str,
        patterns: dict[str, re.Pattern[str]],
        *,
        offset: int,
    ) -> None:
        """Append every surviving match in *text* to *spans*, shifted by *offset*.

        Args:
            spans: Accumulator, mutated in place.
            seen: ``(start, end, name)`` keys already recorded, so the
                whole-document and per-line passes cannot double-count the
                same match.
            text: The window to search -- the whole document, or one line.
            patterns: In-scope mapping of pattern name to compiled regex.
            offset: Document offset of *text*, added to every match position
                so a per-line match is stored in document coordinates.
        """
        for name, pattern in patterns.items():
            for match in pattern.finditer(text):
                start = offset + match.start()
                end = offset + match.end()
                key = (start, end, name)
                if key in seen:
                    continue
                matched = match.group()
                if self._is_allowlisted(matched):
                    continue
                if not post_validate(name, matched):
                    continue
                seen.add(key)
                spans.append(_Span(start, end, name, len(spans)))

    def _splice_markers(self, content: str, merged: list[_MergedSpan]) -> str:
        """Replace each merged span in *content* with its winning marker.

        Splices right-to-left so earlier offsets stay valid while later
        regions are rewritten.

        Args:
            content: The original text the spans were collected against.
            merged: Non-overlapping merged spans, ordered by start.

        Returns:
            Content with every merged span replaced by a marker.
        """
        for span in reversed(merged):
            marker = self.config.replacement_template.format(
                name=_select_marker_name(span.contributors),
            )
            start, end = span.start, span.end
            content = content[:start] + marker + content[end:]
        return content

    def _should_apply_high_entropy(
        self,
        pattern_types: list[str] | None,
    ) -> bool:
        """Decide whether the entropy detector should run for this call.

        Narrowing *pattern_types* to exclude ``high_entropy_string``
        switches off **two** things, not one: the entropy collector and
        the token-boundary snapping backstop (Issue #909), because both
        are gated here. A narrowed call therefore leaves the remainder of
        a bisected token in cleartext. That is deliberate — a caller who
        asked for one detector gets one detector — and it is pinned by
        ``tests/test_redact.py::TestHighEntropyOverlapLeak::
        test_pattern_types_without_entropy_detector_leaves_tail``, so
        read that test before "fixing" it.

        Args:
            pattern_types: Caller-supplied filter; ``None`` means *all*.

        Returns:
            ``True`` when the high-entropy detector is in scope.
        """
        if pattern_types is None:
            return True
        return HIGH_ENTROPY_PATTERN_NAME in pattern_types

    def _collect_high_entropy_spans(
        self,
        content: str,
        start_order: int,
        runs: list[_Run],
    ) -> list[_Span]:
        """Locate high-entropy substrings as spans on the original content.

        The gating mirrors
        :meth:`creek.redact.scanner.RedactionScanner._scan_high_entropy`
        *by construction* — the shared ``HIGH_ENTROPY_CANDIDATE`` regex,
        the same allowlist check, and one
        :func:`~creek.redact.scanner.has_high_entropy_region` helper
        called from both sites against the same
        :func:`~creek.redact.scanner.entropy_threshold` derived from
        :pyattr:`RedactionConfig.min_confidence`. Because the entropy
        decision lives in a single function rather than in two
        hand-synchronised copies, scan/apply parity cannot drift: a
        ``--scan`` finding cannot survive a ``--apply`` step, and
        ``--apply`` cannot start firing on runs ``--scan`` reported as
        clean. Since #945 the *candidacy* half is shared the same way,
        through :func:`~creek.redact.scanner.iter_unmarked_candidates`,
        so a run belonging to a marker this redactor itself renders is
        not a candidate here or in the scanner. Without that,
        ``redact --apply`` corrupted its own output: at any
        ``min_confidence`` at or below 0.5920918598895946 the run
        ``email_password_combo`` inside ``[REDACTED:email_password_combo]``
        cleared the bar and the marker was rewritten to
        ``[REDACTED:[REDACTED:high_entropy_string]]``, nesting again on
        every further run. The carve-out is name-keyed, not shape-keyed —
        :func:`~creek.redact.scanner.iter_unmarked_candidates` carries
        the bound and the two deliberate non-goals.

        ``post_validate`` is deliberately *not* consulted: no validator
        is registered for ``high_entropy_string`` (it would return
        ``True`` unconditionally) and ``_scan_high_entropy`` does not
        call it either, so calling it here would both diverge from the
        scanner and add a permanently-true branch.

        Offsets are taken against the whole *content* — never per line —
        so the spans can be unioned with the regex spans that
        :meth:`_collect_spans` produced (Issue #909).

        Args:
            content: The original, untouched text.
            start_order: Collection order to continue from, so entropy
                spans keep :attr:`_Span.order` globally unique against the
                already-collected regex spans and the marker tie-break
                stays deterministic.
            runs: The shared candidate runs from :meth:`_candidate_runs`.
                They have already been through the emitted-marker
                carve-out and the false-positive allowlist, so neither
                filter is re-applied here — re-applying them would be the
                second copy of a candidacy rule that #945 was created by.

        Returns:
            Spans for every candidate that is not allowlisted and that
            has a high-entropy region — the whole run, or any contiguous
            ``HIGH_ENTROPY_MIN_RUN``-character window of it — clearing
            the configured threshold. The span is always the whole
            candidate run, never the hot sub-window.
        """
        threshold = entropy_threshold(self.config.min_confidence)
        spans: list[_Span] = []
        for start, end in runs:
            if not has_high_entropy_region(content[start:end], threshold):
                continue
            spans.append(
                _Span(
                    start,
                    end,
                    HIGH_ENTROPY_PATTERN_NAME,
                    start_order + len(spans),
                )
            )
        return spans
