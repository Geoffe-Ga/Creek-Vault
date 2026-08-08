"""Pure policy functions for the hashtag ``tags`` pass (issue #878).

:attr:`creek.models.Fragment.tags` shipped with a ``default_factory=list``
and essentially **no producer**: the sole writer in the entire pipeline
was :mod:`creek.clean.context`, which appends the literal
``low-priority`` in one non-default clean mode. Empirically, 2000/2000
sampled fragments of the operator's 35,330-fragment vault carried ``tags:
[]`` and ``00-Creek-Meta/Tag-Garden.md`` read ``*No tags found in
vault.*``, which left three consumers structurally unreachable:
:mod:`creek.generate.tags` (the Tag Garden), :mod:`creek.lint.checks.tags`
(the orphan-tag lint) and the tag-driven branches of
:mod:`creek.generate.compost`. This module is the shared,
side-effect-free policy layer that both writers — the ingest chokepoint
:func:`creek.ingest.base.assemble_ingested_fragment` and the ``creek
classify`` engine — call to close that hole:

* :func:`extract` — pull hashtags out of a markdown body.
* :func:`merge` — union two tag lists, never losing one.
* :func:`apply_tags` — stamp the merged list onto a fragment.

A fourth function, :func:`has_unrecorded_tags`, serves the reporting
side (:func:`creek.cli._scan_fill_gaps`) rather than the writers: "would
a classify run add anything here?" has to be answered in the same
normalised space :func:`merge` works in, and that space is knowledge
only this module has.

Three rules hold across every function here.

**Precision over recall.** The extractor is free and runs over every
fragment, so an over-firing pattern does not fix the bug — it buries the
real tags under machine noise, which is strictly worse than the empty
garden. This corpus makes the danger concrete: Discord exports render
channel mentions as ``#<snowflake>``, and in a 3000-file sample of the
operator's vault ``#c762760820929069057`` appears **688** times and
``#c942223408115122187`` **539** times. Under a naive ``#\\w+`` extractor
those two strings would be the most common "tags" in the entire vault.
:data:`_MIN_ALPHA_CHARS` and :data:`_MAX_DIGIT_RUN` exist for that class
alone.

**Never lose a tag.** :func:`merge` is a union, existing-first, and it
does *not* re-apply the extractor's rejection filters to tags already on
record. ``tags`` is a field operators hand-edit in Obsidian, and both
writers run over the whole vault repeatedly; a merge that filtered its
own inputs would silently delete author-written vault content the first
time a re-ingest or a re-classify passed over it. The rule is stated
formally, in two total halves. **Preservation:** :func:`_canonical`'s
form of the recorded list is always a *prefix* of the merged result,
however long that list is. **Bounded growth:** the merge admits at most
``max(0, _MAX_TAGS - len(canonical))`` new tags on top of that prefix —
exactly that many whenever the candidates offer enough.
:data:`_MAX_TAGS` therefore ceilings what an automated pass may *add*,
never what an operator may have written — a list can only *already* be
past the ceiling, and then it gains nothing, so repeated passes are
idempotent rather than a ratchet.

Preservation is of the *tag*, not of the bytes: what comes back is
:func:`_normalise`'s spelling of the recorded entry, so an entry differs
from what the operator typed in exactly three ways, and none of them is
a lost tag. It is **re-spelled** (``Recovery`` → ``recovery``,
``Family_Business`` → ``family-business``) — the same tag under the one
spelling the garden uses. It **folds into an earlier entry** that shares
that spelling (``Recovery`` beside ``recovery`` yields a single
``recovery``) — the duplicate is lost, the tag is not. Or it
**normalises to the empty string** (``---``, ``_``, ``-``) and is
dropped, having been a separator rather than a tag in the first place.

**Never guess.** Normalisation is deliberately shallow — lowercase,
``_`` → ``-``, collapse repeated ``-``, strip the ends — and camelCase is
**not** split. ``#ArchetypalWavelength`` becomes ``archetypalwavelength``,
not ``archetypal-wavelength``, because case-splitting is a guess and a
wrong guess (``#APTITUDE`` → ``a-p-t-i-t-u-d-e``, ``#c-PTSD`` →
``c-p-t-s-d``) fragments the very vocabulary the Tag Garden exists to
consolidate. A tag over :data:`_MAX_TAG_CHARS` is dropped rather than
truncated for the same reason: truncating invents a tag the author never
wrote and collides two runaway tokens onto one garden entry.

Known residuals
---------------

* **Preserved fragments need ``--force``.** The classify engine runs this
  pass only on fragments it actually (re-)classifies; the OPS-001 resume
  short-circuit's narrow writer
  (:func:`creek.classify.classify_engine._write_tier_only`) is
  deliberately *not* widened to carry tags. So a vault already stamped
  ``classification_method: llm``/``manual`` backfills this axis only
  under an explicit, paid ``creek classify --method llm --force``. That
  is the same call #877 made for praxis, and the #876 tier exception does
  not transfer: an untiered fragment is a live cloud-egress hole, whereas
  a missing tag is a missing feature — and unlike praxis, ``tags`` is a
  field operators hand-edit, so rewriting the axis of every curated
  fragment in a mature vault is not something to do unasked. ``creek
  fill`` surfaces the outstanding count (see
  :func:`creek.cli._hint_tags_backfill`).
* **Removing a hashtag does not un-tag the fragment.** The merge only
  ever adds, so deleting ``#recovery`` from a body leaves ``recovery`` on
  the frontmatter; the operator removes it by hand. That is the price of
  the never-lose rule above, and it is the right side to err on — the
  alternative deletes hand-authored tags on every re-ingest.
* **A list already over the ceiling is frozen.** A fragment carrying
  more than :data:`_MAX_TAGS` recorded tags keeps every one of them and
  gains none until the operator prunes it by hand. That is the
  deliberate trade for never deleting hand-authored tags: nothing here
  can tell which of the tags it did not write is the one worth evicting,
  so it evicts nothing and admits nothing.
* **FEAT-023 re-atomized children are tagged one pass late.** The
  splitter and the re-atomizer build :class:`~creek.ingest.base.IngestedFragment`
  directly (``creek/atomize/split.py:264``,
  ``creek/classify/reatomize.py:525,613``), bypassing the ingest
  chokepoint, so a freshly-derived child carries ``tags: []`` until the
  next classify pass sees it.
* **Human-looking Discord channel mentions are admitted.**
  ``#living-room`` (17 occurrences) and ``#family-business`` (14) survive
  every filter here, because at classify time only the rendered form
  exists — nothing distinguishes them from a tag the author typed. The
  snowflake rules kill the 1227-occurrence machine-id class; this
  handful is indistinguishable and is let through.
* **The weighted path is unaffected.**
  :func:`creek.classify.weighted.classify_weighted` carries its own
  prompt and its own top-level key allow-list and does not run this pass.

Security note: these patterns are matched against 35,330 unbounded,
attacker-influenced fragment bodies, on both the ingest and the classify
path. Every one of them is linear-time — no nested quantifiers — and all
are compiled once at import rather than per fragment.

No vault, no I/O, no LLM: everything here is a pure function of the
fragment and its body.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from creek.models import Fragment


TAGS_KEY: Final[str] = "tags"
"""Frontmatter key carrying a fragment's :attr:`~creek.models.Fragment.tags`."""


_MAX_TAGS: Final[int] = 32
"""Per-fragment ceiling on the tags an *automated pass* may contribute.

It plays two distinct roles, and only one of them is a truncation.

In :func:`extract` it truncates the extractor's **own** freshly-derived
output, stopping one pathological body — a hand-written tag-index note,
a scraped page whose navigation survived conversion — from offering
hundreds of entries for a single fragment's YAML frontmatter. That
truncation is first-seen order, never a set, so the same body always
yields the same 32 and a re-run does not churn the file.

In :func:`merge` it is a **growth ceiling**: the most an automated pass
may grow a recorded list *to*, i.e. at most
``max(0, _MAX_TAGS - len(recorded))`` new tags are admitted. It is never
a truncation of recorded data — a list already past it keeps every entry
and gains none (see the module docstring's never-lose rule).
"""

_MIN_ALPHA_CHARS: Final[int] = 2
"""Minimum number of alphabetic characters a candidate must contain.

The load-bearing precision rule. Discord renders a channel mention as one
letter followed by a snowflake id, and in a 3000-file sample of the
operator's vault ``#c762760820929069057`` occurs **688** times and
``#c942223408115122187`` **539** times — the two most common ``#``-tokens
in the corpus by a wide margin. Every one of them has exactly one letter,
so requiring two removes the class outright.

It is deliberately "at least two letters", not "mostly letters":
``#e2e`` and ``#i18n`` are real tags that a ratio-based rule would throw
away.
"""

_MAX_DIGIT_RUN: Final[int] = 8
"""Length of a consecutive-digit run that disqualifies a candidate.

Belt to :data:`_MIN_ALPHA_CHARS`' braces, and independently useful: a run
this long is an id, a timestamp or a phone number, never a word. Seven
digits is still admissible (``#ab1234567`` is a plausible build tag).
"""

_MIN_TAG_CHARS: Final[int] = 2
"""Shortest normalised tag worth recording.

A one-character tag carries no meaning in a garden of 35k fragments and
is almost always the residue of a stray ``#`` next to an initial.

The ``len(tag) < _MIN_TAG_CHARS`` half of :func:`_accept`'s bound is
currently **unreachable** through :func:`extract`: :data:`_TAG_RE` forces
a leading ``[A-Za-z]``, :func:`_has_enough_letters` then requires two
alphabetic characters, and :func:`_normalise` never removes a letter, so
every candidate that reaches the bound is already at least two
characters long. It is kept as a defensive lower bound so that loosening
either of those rules cannot silently start admitting one-character
tags.
"""

_MAX_TAG_CHARS: Final[int] = 64
"""Longest normalised tag worth recording.

Anything longer is a sentence that lost its spaces, not a tag. Candidates
past this bound are dropped, never truncated — see the module docstring.
"""


_TAG_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:^|(?<=\s))#([A-Za-z][A-Za-z0-9_/-]*)",
    re.MULTILINE,
)
r"""Match an Obsidian-style hashtag; group 1 is the raw candidate.

Two anchors do all the negative work. The leading ``(?:^|(?<=\s))``
requires the ``#`` to start a line or follow whitespace, which is what
keeps ``https://x.com/a#anchor`` and the ``foo#bar`` identifier out. The
mandatory ``[A-Za-z]`` first character is what keeps markdown headings
out: ``# Heading`` and ``## Heading`` both put a space or a second ``#``
where a letter would have to be, and ``#123`` and ``#-leading-dash`` fail
the same test.

``re.MULTILINE`` makes ``^`` per-line rather than per-body, because real
notes put their tags on a trailing line of their own; a body-start anchor
would miss essentially every one of them.

The body class keeps ``/`` so nested Obsidian tags survive
(``#project/creek`` → ``project/creek``), and ``_`` so
:func:`_normalise` can fold it to ``-`` rather than having to split the
candidate at ingest time.
"""

_CODE_FENCE_RE: Final[re.Pattern[str]] = re.compile(
    r"```.*?(?:```|\Z)",
    re.DOTALL,
)
"""Match a fenced code block, closed or running to the end of the body.

Technical notes in this corpus are full of shell and Python fences whose
comments read exactly like hashtags (``x = 1  #comment``). Stripping the
fence before scanning is the only thing that keeps those out, and it has
to happen *before* :data:`_INLINE_CODE_RE` runs — a fence is itself made
of backticks, so an inline-span pass over unstripped text would pair the
fence markers with each other.

The ``(?:```|\\Z)`` alternative makes an unterminated fence swallow the
rest of the body, which is what CommonMark itself does with one. The
non-greedy ``.*?`` keeps the pattern linear-time (see the module
docstring's security note).
"""

_INLINE_CODE_RE: Final[re.Pattern[str]] = re.compile(r"`[^`\n]*`")
"""Match an inline code span.

``#fff`` inside backticks is a CSS colour, and it passes every other rule
here — leading letter, three alphabetic characters, no digit run — so
stripping inline spans is all that stands between the Tag Garden and
every hex colour in the vault.

Deliberately bounded to a single line (``[^`\\n]``) even though
CommonMark allows a span to wrap: one stray backtick in prose is far more
common than a wrapped span, and an unbounded version would let that
single character silently blank the rest of the note.
"""

_DIGIT_RUN_RE: Final[re.Pattern[str]] = re.compile(
    rf"\d{{{_MAX_DIGIT_RUN},}}",
)
"""Match a disqualifying run of consecutive digits (:data:`_MAX_DIGIT_RUN`).

Built from the constant rather than a literal so the two can never drift
apart — the boundary is asserted from both sides in the tests, and an
off-by-one here is the difference between rejecting a Discord snowflake
and rejecting a build tag.
"""

_DASH_RUN_RE: Final[re.Pattern[str]] = re.compile(r"-{2,}")
"""Match a run of repeated dashes for :func:`_normalise` to collapse.

Without the collapse, ``#foo--bar`` and ``#foo-bar`` are two distinct
entries in the garden that no reader could tell apart.
"""


def _strip_code(body: str) -> str:
    """Blank out code fences and inline spans so they are never scanned.

    Order matters: fences first, then inline spans (see
    :data:`_CODE_FENCE_RE`). Each match is replaced by whitespace rather
    than by the empty string, so removing a span cannot fuse the word
    before it onto a ``#`` after it and defeat :data:`_TAG_RE`'s
    whitespace lookbehind.

    Args:
        body: The fragment's raw markdown body.

    Returns:
        The body with every code region replaced by whitespace of the
        same role (a newline for a fence, a space for a span).
    """
    without_fences = _CODE_FENCE_RE.sub("\n", body)
    return _INLINE_CODE_RE.sub(" ", without_fences)


def _normalise(raw: str) -> str:
    """Fold *raw* to the single canonical spelling of its tag.

    Lowercase, ``_`` → ``-``, collapse repeated ``-``, strip the ends —
    and nothing else. See the module docstring for why camelCase is left
    alone.

    Args:
        raw: A hashtag candidate, or a tag already recorded on a
            fragment.

    Returns:
        The normalised tag. May be the empty string when the input was
        made entirely of separators; callers drop that rather than
        recording ``""``.
    """
    lowered = raw.lower().replace("_", "-")
    return _DASH_RUN_RE.sub("-", lowered).strip("-")


def _has_enough_letters(candidate: str) -> bool:
    """Report whether *candidate* carries :data:`_MIN_ALPHA_CHARS` letters.

    Short-circuits as soon as the quota is met so a 19-digit Discord
    snowflake is rejected after its first character rather than after its
    twentieth — this runs on every ``#``-token of every body in the
    vault.

    Args:
        candidate: The raw hashtag text, without the leading ``#``.

    Returns:
        ``True`` when at least :data:`_MIN_ALPHA_CHARS` characters are
        alphabetic.
    """
    letters = 0
    for char in candidate:
        if char.isalpha():
            letters += 1
            if letters >= _MIN_ALPHA_CHARS:
                return True
    return False


def _accept(candidate: str) -> str | None:
    """Apply the rejection filters and return the normalised tag, or ``None``.

    The filters run against the *raw* candidate and the length bound
    against the *normalised* one, because normalisation can only shorten
    a candidate (``__`` → ``-``) and a bound checked before it would
    reject spellings the garden would have merged anyway.

    Args:
        candidate: The raw hashtag text, without the leading ``#``.

    Returns:
        The normalised tag, or ``None`` when the candidate fails any
        precision rule.
    """
    if not _has_enough_letters(candidate):
        return None
    if _DIGIT_RUN_RE.search(candidate):
        return None
    tag = _normalise(candidate)
    if not _MIN_TAG_CHARS <= len(tag) <= _MAX_TAG_CHARS:
        return None
    return tag


def extract(body: str) -> list[str]:
    """Pull the hashtags out of *body*, normalised and deduplicated.

    Code fences and inline spans are stripped first, then every surviving
    ``#tag`` is run through the precision filters in :func:`_accept`.

    Order is first-seen, so the result is stable across runs and the
    :data:`_MAX_TAGS` truncation always keeps the same head — a set-based
    implementation would churn the frontmatter of an unchanged fragment
    on every ingest.

    Args:
        body: The fragment's markdown body. Hashtags are body-only: a
            title reading ``# Hello`` is a heading, not a tag.

    Returns:
        Up to :data:`_MAX_TAGS` normalised tags in body order.
    """
    found: list[str] = []
    seen: set[str] = set()
    for match in _TAG_RE.finditer(_strip_code(body)):
        tag = _accept(match.group(1))
        if tag is None or tag in seen:
            continue
        seen.add(tag)
        found.append(tag)
        if len(found) == _MAX_TAGS:
            break
    return found


def _canonical(tags: list[str]) -> list[str]:
    """Normalise *tags* and drop duplicates, preserving first-seen order.

    The canonical form of a recorded tag list: every entry through
    :func:`_normalise`, empties dropped, later spellings of an
    already-seen tag dropped. This — not the raw list — is what the
    never-lose guarantee in :func:`merge` is stated over.

    Args:
        tags: The tags as recorded, in whatever spelling was written.

    Returns:
        The canonical tags, in first-seen order. Never longer than
        *tags*, and never capped: this is recorded data, and
        :data:`_MAX_TAGS` bounds growth rather than length.
    """
    canonical: list[str] = []
    seen: set[str] = set()
    for raw in tags:
        tag = _normalise(raw)
        if not tag or tag in seen:
            continue
        seen.add(tag)
        canonical.append(tag)
    return canonical


def merge(current: list[str], candidate: list[str]) -> list[str]:
    """Union *current* and *candidate*, existing-first, never losing a tag.

    Both sides are put through :func:`_normalise` — frontmatter written
    by hand in Obsidian carries arbitrary case, and normalising only the
    new side would leave the garden counting ``Recovery`` and
    ``recovery`` as two tags forever. Neither side is put through the
    :func:`extract` rejection filters: *filtering is the extractor's job*,
    and re-applying it here would delete recorded tags (see the
    module docstring's never-lose rule).

    The contract has two halves and both are total.

    **Preservation.** :func:`_canonical`'s form of *current* is always a
    *prefix* of the result, however long *current* is. :data:`_MAX_TAGS`
    bounds growth, not length, so no recorded list is ever trimmed to
    fit under it.

    **Bounded growth.** At most ``max(0, _MAX_TAGS - len(canonical))``
    entries of *candidate* are admitted on top of that prefix, and
    exactly that many when *candidate* offers enough distinct new tags. A
    candidate already on record costs no room. An automated pass can
    therefore never *grow* a list past the ceiling — a list can only
    already be past it, and then it gains nothing — which makes repeated
    passes idempotent rather than a ratchet.

    Args:
        current: The tags already recorded on the fragment.
        candidate: The tags just derived from the body.

    Returns:
        The canonical form of *current*, in order, followed by the
        admitted new tags. Entries come back in :func:`_normalise`'s
        spelling rather than byte-for-byte, so an entry of *current* can
        differ from what was written in exactly three ways, none of them
        a lost tag: it is re-spelled (``"Recovery"`` → ``"recovery"``);
        it folds into an earlier entry sharing that spelling
        (``"Recovery"`` beside ``"recovery"`` yields one ``"recovery"``
        — the duplicate goes, the tag stays); or it normalises to the
        empty string (``"---"``, ``"_"``, ``"-"``) and is dropped as the
        separator it always was rather than as a tag.
    """
    kept = _canonical(current)
    seen = set(kept)
    room = max(_MAX_TAGS - len(kept), 0)
    for raw in candidate:
        if room == 0:
            break
        tag = _normalise(raw)
        if not tag or tag in seen:
            continue
        seen.add(tag)
        kept.append(tag)
        room -= 1
    return kept


def has_unrecorded_tags(current: list[str], body: str) -> bool:
    """Report whether *body* carries a hashtag *current* does not record.

    Compares against ``_canonical(current)``, not the raw list, because
    ``tags: [Recovery]`` beside a body reading ``#recovery`` records the
    tag already — reporting that as a gap would bill the operator for a
    paid ``creek classify --method llm --force`` run to fix a difference
    in case that changes nothing.

    Args:
        current: The tags already recorded on the fragment.
        body: The fragment's markdown body.

    Returns:
        ``True`` when :func:`extract` finds at least one tag the
        canonical form of *current* does not already hold.
    """
    recorded = set(_canonical(current))
    return any(tag not in recorded for tag in extract(body))


def apply_tags(fragment: Fragment, body: str) -> Fragment:
    """Stamp the merged tag list on *fragment*, union-only.

    Args:
        fragment: The fragment to tag. Never mutated.
        body: The fragment's markdown body — hashtags are body-only, and
            the :class:`~creek.models.Fragment` model does not carry the
            body.

    Returns:
        The fragment carrying the merged tags — **the same object** when
        the merge changed nothing, matching
        :func:`creek.classify.praxis_pass.apply_praxis`'s contract.
        Callers detect "did this run tag anything?" by comparing
        before/after, and both writers run this over every fragment in
        the vault, so an unconditional copy would both blur that signal
        and allocate a fresh model per fragment for nothing.
    """
    current = fragment.tags.copy()
    merged = merge(current, extract(body))
    if merged == current:
        return fragment
    return fragment.model_copy(update={TAGS_KEY: merged})
