"""Vault-side fragment reader — single source of truth for loading fragments.

The classify engine, review runner, and link engine all walk
``<vault>/01-Fragments/`` looking for files that parse as Creek
fragments. They differ only in what they want back:

- the classify engine needs ``(fragment, body, raw_metadata)`` so it
  can rewrite the file with updated frontmatter while preserving any
  non-Fragment keys;
- the review runner builds a :class:`ReviewEntry` around the same
  triple plus the source path;
- the link engine just needs the :class:`Fragment` itself.

Before this module existed the three sites carried independent copies
of the same three-step validation chain (``frontmatter.load`` → check
``type == "fragment"`` → :meth:`Fragment.model_validate`). A schema
change to :class:`Fragment` or a rename of the ``type`` sentinel
would have required keeping three implementations in lock-step. The
helpers here remove that drift risk by exposing one validated load
path; callers project the result into whatever shape they need.
"""

from __future__ import annotations

import logging
from pathlib import Path  # noqa: TC003  # no issue: runtime use in type hints
from typing import Final

import frontmatter
import yaml
from pydantic import ValidationError

from creek._containment import escaping_child
from creek.models import Fragment

logger = logging.getLogger(__name__)

FRONTMATTER_LOAD_ERRORS: Final[tuple[type[Exception], ...]] = (
    OSError,
    TypeError,
    ValueError,
    yaml.YAMLError,
)
"""What a hand-edited or corrupt vault file raises out of ``frontmatter.load``.

The one definition of "this file's frontmatter is unreadable", shared by every
loader in the package. A second, drifting copy of that answer is the bug this
constant exists to end: the tree carried three different tuples —
``(OSError, ValueError)``, ``(OSError, ValueError, yaml.YAMLError)``, and
:func:`creek.vault.links.read_header_meta`'s wider set — and a note that
crashed one walk was skipped by another.

Widens the house ``(OSError, ValueError, yaml.YAMLError)`` tuple with
``TypeError``, which is load-bearing and must never be dropped:
``frontmatter.load`` ends in ``Post(content, handler, **metadata)``, so a
header carrying a **non-string key** — a bare YAML date (``2024-05-01:``), a
bool (``true:``), or an int (``1:``), all three valid ``SafeLoader`` output and
all three plausible in a hand-authored Obsidian note — raises a plain
``TypeError: keywords must be strings`` that no other tuple in the tree caught
(#1475, #924; precedent PR #927 / issue #847).

``UnicodeDecodeError`` is a ``ValueError`` subclass and is therefore covered
deliberately: a file whose *body* carries non-UTF-8 bytes fails at read time,
before its (possibly byte-clean ASCII) frontmatter is ever parsed.

**Wrap the load statement, nothing else.** ``TypeError`` is the most common
symptom of a genuine programming error, so a tuple this wide around a loop body
— or around a ``model_validate`` call — would swallow real bugs and turn them
into silent skips.

Nearly every use in this package brackets a bare
``frontmatter.load``/``loads`` call and nothing else. The exceptions are
enumerated exhaustively here — :func:`iter_vault_fragments` below,
:func:`creek.classify.review_runner._read_entry`,
:func:`creek.author.checks._scan_subtree_for_cited`, and
:func:`creek.classify.classify_engine._load_classifiable_fragment` — and they
bracket a single call to :func:`try_load_fragment` (or to
``classify_engine._read_fragment``, a thin alias for it) instead, because that
helper *is* their load. No tally of the remaining sites is written here on
purpose: a hand-counted number goes stale the moment a site moves (#1548 will
move several), and this paragraph carried a wrong one until #1546. Both
claims are instead checked mechanically by
``test_every_frontmatter_guard_brackets_exactly_one_load`` in
``tests/test_frontmatter_nonstring_key_guard.py``, which walks this package's
AST and fails if any guard brackets more than its load, or if a site outside
the four named above starts bracketing the helper. The extra surface
that buys is bounded and stated here rather than papered over: on top of the
load, ``try_load_fragment`` runs ``post.metadata.copy()``, one ``dict.get``,
and ``Fragment.model_validate``, whose own failure mode — ``ValidationError``,
a ``ValueError`` subclass — is already caught *inside* the helper and logged.
Nothing else in there can raise a member of this tuple without it being a real
bug, and no caller may widen the bracket further.

A reader that needs only the *metadata* should prefer
:func:`creek.vault.links.read_header_meta` over catching this at all: it parses
the header with ``yaml.safe_load`` and never splats, so the crash is
structurally impossible rather than merely handled.

Note that widening a guard converts a crash into a silent skip. #926 tracks
surfacing those skips to the operator, and must land after this.
"""

CORPUS_SUBDIRS: Final[tuple[str, ...]] = (
    "01-Fragments",
    "09-Reference",
    "11-Other-Authors",
)
"""The vault subtrees that hold Creek fragments, in walk order.

One definition with two consumers that must never disagree:

* the Writing Desk specialists gather their evidence from these subtrees
  (:func:`creek.author.agents._load_corpus`), and
* the HARD ``privacy_compliance`` leak gate resolves each cited fragment's tier
  by walking exactly the same ones
  (:func:`creek.author.checks._resolve_cited_tiers`).

While those were two separate literals the gate fell a subtree behind the
specialists it polices, and a draft could reproduce the protected body of an
``intimate`` fragment filed under ``09-Reference`` or ``11-Other-Authors`` and
still review as ``PASS`` (#1341). Anything that reads part of the corpus reads
this tuple; a second list is a second thing to forget to update.

Index 2 is the folder :data:`creek.vault.authors.OTHER_AUTHORS_DIR` already
names. That module is deliberately *not* imported here — the dependency inside
:mod:`creek.vault` runs toward the reader, not away from it — so the equality is
pinned by ``tests/test_vault_reader.py`` rather than by an import.
"""


def try_load_fragment(
    md_file: Path,
) -> tuple[Fragment, str, dict[str, object]] | None:
    """Load a single fragment file, returning ``None`` for non-fragments.

    The function distinguishes two failure modes:

    * **Real I/O / parse failures** propagate as ``OSError``,
      ``ValueError``, or :class:`yaml.YAMLError` so the caller can
      record them on its summary's ``errors`` list.
    * **Non-fragment markdown** (no ``type: fragment`` field, or a
      schema mismatch) returns ``None`` so the caller silently skips
      it. Markdown notes coexist with fragments in a vault and aren't
      errors.

    Args:
        md_file: Path to a markdown file inside ``<vault>/01-Fragments``.

    Returns:
        ``(fragment, body, raw_metadata)`` for a valid Creek fragment,
        or ``None`` when the file is well-formed YAML but not a
        fragment.

    This loader deliberately does **not** catch
    :data:`FRONTMATTER_LOAD_ERRORS` itself. It needs ``post.content``, so it
    cannot use the splat-free :func:`creek.vault.links.read_header_meta`, and
    each caller's tolerance for an unreadable file differs: a lint walk skips
    it, ``creek classify`` records it on ``errors``, and the HARD leak gate in
    :mod:`creek.author.checks` logs a warning because a fragment it cannot read
    is a fragment it cannot police. Swallowing here would impose the lint
    walk's policy on all three.

    Raises:
        OSError: When the file cannot be read.
        TypeError: When the frontmatter carries a non-string key, which
            ``frontmatter.load``'s ``**metadata`` splat rejects (#1475).
        ValueError: When the YAML cannot be parsed.
        yaml.YAMLError: When the YAML parser rejects the document.
    """
    post = frontmatter.load(str(md_file))
    metadata = post.metadata.copy()
    if metadata.get("type") != "fragment":
        return None
    try:
        fragment = Fragment.model_validate(metadata)
    except ValidationError:
        logger.debug("Skipping invalid fragment frontmatter: %s", md_file)
        return None
    return fragment, post.content, metadata


def iter_vault_fragments(
    fragments_root: Path,
) -> list[tuple[Path, Fragment, str, dict[str, object]]]:
    """Walk ``fragments_root`` and yield every valid Creek fragment.

    Convenience wrapper for callers that want every loadable fragment
    without manually distinguishing real I/O failures from
    non-fragment skips. I/O failures are logged at DEBUG level and
    skipped — callers that need to surface them to the operator
    should iterate manually with :func:`try_load_fragment`.

    **Containment (#1373).** A ``.md`` file under *fragments_root* that is
    itself a symlink resolving OUTSIDE that root is skipped and logged, via
    the one shared predicate :func:`creek._containment.escaping_child`.
    Without it, dropping a single link into ``01-Fragments/`` puts
    attacker-chosen content — and the ``privacy_tier`` it is admitted under,
    since that field is read from the planted file's own frontmatter — into
    the Writing Desk corpus, the link graph, the compiled pages and every
    cloud LLM prompt built from them.

    Three deliberate choices, each the answer to a way this could go wrong:

    * **The guard lives here, not in the caller that reported it.** This
      function is the single loader for 25-odd consumers — classify's
      privacy filter, the link engine, the compile engine, every lint check,
      the MCP read gate. Guarding :func:`creek.author.agents._load_corpus`
      alone would leave all of them reading the planted file and guarantee
      the next consumer re-invents the check, which is the drift #1294
      closed.
    * **It skips, it never raises.** Hard-erroring in the shared loader
      would turn one planted symlink into a hard failure of classify, link,
      compile and every MCP read tool — a denial of service handed to anyone
      with write access to the vault, which is worse than the injection
      being closed. Skipping is also the direction :mod:`creek._containment`
      permits: a containment helper may only ever cause a caller to read
      *less*.
    * **WARNING, not the DEBUG used by the unreadable-file skip below.** An
      unreadable markdown file is common and benign in a live Obsidian
      vault; a fragment symlinked out of the vault cannot happen by
      accident. Occurrences are ~zero, so the line costs nothing, and #1087's
      lesson was that a silent skip in a safety path is its own hazard.

    ACCEPTED NARROWING: containment is judged against the root the CALLER
    named, so ``01-Fragments/x.md -> ../09-Reference/y.md`` — inside the
    vault but outside *this* root — is skipped. Machine-written Creek
    fragments never take that shape, and an intra-root alias
    (``01-Fragments/alias.md -> 01-Fragments/real.md``) is still loaded, so
    the guard cannot quietly become "drop every symlink".

    Args:
        fragments_root: Path to ``<vault>/01-Fragments`` (or any
            directory tree containing fragment files).

    Returns:
        Sorted list of ``(path, fragment, body, raw_metadata)``
        tuples — one per valid fragment.
    """
    if not fragments_root.exists():
        return []

    # Resolved exactly once, above the loop: resolving per child would be
    # both wasteful and wrong, since the policy is resolve-the-root and
    # ``lstat``-the-leaf.
    resolved_root = fragments_root.resolve(strict=False)
    out: list[tuple[Path, Fragment, str, dict[str, object]]] = []
    for md_file in sorted(fragments_root.rglob("*.md")):
        if escaping_child(md_file, resolved_root):
            # Named as walked. The resolved target is never logged: that is
            # the exfiltration oracle #1087 closed.
            logger.warning(
                "Skipping a fragment whose symlink leaves %s: %s",
                fragments_root,
                md_file,
            )
            continue
        try:
            record = try_load_fragment(md_file)
        except FRONTMATTER_LOAD_ERRORS:
            logger.debug("Skipping unreadable markdown file: %s", md_file)
            continue
        if record is None:
            continue
        fragment, body, raw = record
        out.append((md_file, fragment, body, raw))
    return out


def load_post_or_raise(path: Path) -> frontmatter.Post:
    """Load *path*, re-raising an unreadable header with *path* in the message.

    The three fragment-lifecycle write paths — :meth:`VaultWriter.update_fragment`,
    :meth:`VaultWriter.tomb_fragment`, and :meth:`VaultWriter.restore_fragment` —
    each need the file's *body* as well as its header, so none of them can use
    the splat-free :func:`creek.vault.links.read_header_meta`. They also must
    not adopt the read paths' skip-and-continue policy: silently declining to
    update, tomb, or restore a fragment loses an operator's edit and reports
    success (#926 is the general form of that hazard).

    So this fails, as it did before — but it says *which* file. Previously
    these three loads were unguarded, and a hand-edited note with a bare-date
    frontmatter key surfaced as a bare ``TypeError: keywords must be strings``
    with no path anywhere in the traceback, leaving the operator to find the
    offending file by hand across a 35k-note vault (#1475).

    The window this actually covers is verify-then-load. :func:`_file_declares_id`
    now screens the same exception set, so a file already unreadable when the
    id was resolved never reaches here — it resolves to "not found" instead,
    which is its own defect and is tracked as #1543. What remains is the gap
    between that verification and this load: the writer holds ``self._lock``,
    which is a lock over *this process*, and a Creek vault is a live Obsidian
    folder. An editor, a sync client, or a sibling ``creek`` run can rewrite
    the file in between. That is exactly the class of race #1083 built the
    verifier for, and leaving the load bare would report it as an unpathed
    ``TypeError``.

    **The OS-level failures keep their ``OSError`` identity**, and that split
    is the whole reason this is two ``except`` clauses rather than one over
    :data:`~creek.vault.reader.FRONTMATTER_LOAD_ERRORS` (which contains
    ``OSError``). All three callers are reached from the per-unit loop in
    :mod:`creek.ingest.pipeline`, whose handlers are ``except (OSError,
    KeyError)``: an unreadable file is reported against *that unit* and the
    batch continues. Re-raising the race described above — a vanished or
    half-rewritten file, i.e. an ``OSError`` — as a ``ValueError`` would walk
    straight past that handler and out through ``_run_ingest``, which catches
    only ``FileNotFoundError``/``EscapingSymlinkError``, ending the whole
    ``creek ingest`` run on one file an editor happened to be touching. That
    is strictly narrower than the bare ``frontmatter.load`` this replaced. So
    ``ValueError`` is reserved for the parse-shaped failures — the non-string
    key (``TypeError``), malformed YAML, undecodable bytes — which were never
    catchable per-unit anyway, and which no concurrent editor can conjure out
    of a file that was fine a moment ago.

    Args:
        path: The live vault file to parse.

    Returns:
        The parsed document, body included.

    Raises:
        OSError: When the file itself could not be read — vanished, replaced,
            permission-denied. Re-raised as an ``OSError`` so the ingest
            loop's per-unit handler still catches it, with *path* named.
        ValueError: When the bytes were read but the frontmatter will not
            parse, naming *path* and quoting the underlying error.
    """
    try:
        return frontmatter.load(str(path))
    except OSError as exc:
        # Deliberately ahead of the wider tuple below, which also contains
        # ``OSError``: the family must stay an ``OSError`` for the ingest
        # loop's ``except (OSError, KeyError)`` per-unit handler. See above.
        msg = f"Unreadable frontmatter in {path}: {exc}"
        raise OSError(msg) from exc
    except FRONTMATTER_LOAD_ERRORS as exc:
        msg = f"Unreadable frontmatter in {path}: {exc}"
        raise ValueError(msg) from exc
