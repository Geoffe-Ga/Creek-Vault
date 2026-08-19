"""Single source of truth for the Adepthood staging directories (#845/#1023).

Adepthood stages the content it captures as files under the vault so the
ordinary ledger-backed ingest picks them up: ``creek.journal``
(``creek_mcp/tools/journal.py``) writes each entry's full body as markdown,
and ``creek.upload`` (``creek_mcp/tools/upload.py``) writes each uploaded
document's bytes verbatim. The purge engine (``creek/purge/engine.py``)
follows a purged fragment's ``source.origin_key`` back into these
directories to delete that staged plaintext, so the RTBF sweep is scoped
to exactly the roots declared here.

Both staging dirs are declared together, in one module, for two reasons:
the purge sweep must be able to enumerate *every* root (a root it cannot
see is plaintext it cannot erase), and every consumer must resolve the
same path or the two sides silently drift apart. They live on the
``creek`` side because the layering rule is that ``creek_mcp`` imports
``creek``, never the reverse.

The module keeps its journal-era filename: only two production modules
import it, so a rename would be cosmetic churn against a path that is
otherwise stable. That rename is deliberately deferred.

Relocating any path here is a breaking change: the source ledger keys
staged entries by it, so a move without a ledger migration would orphan
every existing staged entry.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

JOURNAL_STAGING_RELDIR = Path("00-Creek-Meta/adepthood/journal")
"""Vault-relative staging dir for Adepthood journal entry bodies.

The ``journal`` path segment makes the markdown ingestor classify staged
entries as ``SourcePlatform.JOURNAL``; the ``adepthood`` segment identifies
the source in each fragment's ``source.origin_key``.
"""

UPLOAD_STAGING_RELDIR: Final[Path] = Path("00-Creek-Meta/adepthood/uploads")
"""Vault-relative staging dir for uploaded document bytes.

The layout is **flat** — one file per ``safe_stem(external_id)``, never a
nested tree — because the vault-wide RTBF wipe iterates this directory
directly, and a nested layout would let a staged document hide from the
sweep in a subdirectory nobody enumerated.

Unlike :data:`JOURNAL_STAGING_RELDIR`, the ``uploads`` path segment
matches *none* of ``creek/ingest/markdown.py``'s journal path patterns,
so an uploaded ``.md`` lands as ``SourcePlatform.MARKDOWN`` in
``01-Fragments/Notes`` rather than as a journal entry. That is
deliberate: for an upload it is ``route_to_ingestor``, dispatching on the
file's extension, that chooses the ingestor — not the staging path.
"""

ARCHIVE_UNPACK_RELDIR: Final[Path] = Path("00-Creek-Meta/adepthood/archive-unpack")
"""Vault-relative scratch root an uploaded export ARCHIVE is unpacked into (#1525).

Deliberately **not** in :data:`ADEPTHOOD_STAGING_RELDIRS`, and the reason is
the opposite of an oversight: nothing here is staged content. An archive
upload extracts under ``<this>/<safe_stem(external_id)>/``, runs the directory
ingestor over that tree, and deletes the tree in a ``finally`` — the archive's
own bytes are never written to the vault at all, and no fragment's
``source.origin_key`` ever points inside. There is therefore no staged
plaintext for the RTBF sweep to find, which is the strongest form of the
guarantee that sweep exists to provide, not a gap in it.

Adding it to the staging tuple would in fact make things *worse*: that sweep
walks each root with a non-recursive :meth:`~pathlib.Path.iterdir` and skips
anything that is not a file, so it would count nothing here while implying
coverage.

The path is per-``external_id`` and deterministic rather than randomised
because a ledger-free ingest derives each fragment's id from its source path
(:func:`creek.ingest.base.generate_fragment_id`), so a randomised unpack
directory would mint a fresh id for every turn on every re-upload and
duplicate the whole export. Determinism here is what makes re-uploading the
same archive a no-op.

Residue after a hard kill (SIGKILL, OOM, power loss) between extraction and
the ``finally`` is reached by ``purge vault``, but **not** by a scoped purge:

* ``purge vault`` sweeps ``00-Creek-Meta/`` deny-by-default
  (:func:`creek.purge.meta.sweep_unkept_meta` destroys every unkept regular
  file, and nothing keeps ``adepthood/``), so an abandoned unpack tree is
  destroyed there. Pinned by
  ``test_purge_vault_destroys_abandoned_archive_residue``.
* ``purge_fragment`` and the other scoped entries run only
  ``_purge_scoped_tail`` and no meta sweep, so a per-person RTBF request does
  not reach an abandoned tree. Tracked as a follow-up rather than widened here.

An earlier version of this note claimed the residue was bounded because "the
whole of this root is removed before any archive is unpacked". That was true
and was itself the bug: clearing the shared parent destroyed a *concurrently
running* upload's extraction tree, since ``/v1/uploads`` runs the tool in a
threadpool. Cleanup is now scoped to this upload's own directory, which is
correct for concurrency and is exactly why the bound above had to be restated
in terms of what the purge actually reaches.

Adding this root to :data:`ADEPTHOOD_STAGING_RELDIRS` would not close the
scoped gap: that sweep walks each root with a non-recursive
:meth:`~pathlib.Path.iterdir` and skips anything that is not a file, so it
would count nothing here while implying coverage.
"""

ADEPTHOOD_STAGING_RELDIRS: Final[tuple[Path, ...]] = (
    JOURNAL_STAGING_RELDIR,
    UPLOAD_STAGING_RELDIR,
)
"""Every Adepthood staging root, for callers that must sweep all of them.

The purge engine iterates this tuple so that adding a future staging root
here is enough to bring it under the RTBF sweep; a root reachable only
through its own named constant is a root some sweep will forget.
"""

UPLOAD_LEDGER_SOURCE: Final[str] = "upload"
"""Ledger source name for staged uploads (``run_ingest(ledger_source=...)``).

Names ``00-Creek-Meta/State/ingest/upload.jsonl`` via
``SourceLedger.path_for``. That ledger is what gives an uploaded document
its stable identity — idempotent re-send, edit-in-place, and the
``source.origin_key`` the purge sweep keys on — so relocating it orphans
every staged upload.
"""
