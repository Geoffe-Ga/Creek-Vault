"""Staged-filename derivation shared by the Adepthood staging tools (#1023).

``creek.journal`` and ``creek.upload`` both turn a caller-owned external id
into a stable file under an Adepthood staging directory, and both key the
source ledger on the path that results. One module owns that derivation so
the two tools cannot drift: a stem computed two ways is two idempotency
keys, and the second one orphans everything the first one staged.

The rules live on the ``creek_mcp`` side because these names exist only for
the MCP staging surface — the ingest pipeline reads whatever files it is
pointed at and has no opinion about how they were named.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Final

_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")
_SUFFIX_RE = re.compile(r"[^A-Za-z0-9.]+")

MAX_SUFFIX_CHARS: Final[int] = 16
"""Upper bound on a staged file's extension.

Long enough for every extension :func:`creek.ingest.route_to_ingestor`
recognises, short enough that a caller cannot smuggle a filename-length
attack through the one part of the name it controls.
"""


def safe_stem(external_id: str) -> str:
    """Return a filesystem-safe, *collision-free*, stable stem for *external_id*.

    A readable slug prefix aids debugging, but the trailing hash of the RAW
    external id is what guarantees the mapping is injective — two distinct ids
    that slug to the same string (e.g. ``"a/b"`` and ``"a-b"``) still get
    distinct stems, so the idempotency key never collides. Deterministic, so the
    same external id always resolves to the same staged path (hence the same
    ledger source-key → idempotent re-send / edit-in-place).
    """
    slug = _SLUG_RE.sub("-", external_id).strip("-")[:80]
    digest = hashlib.sha256(external_id.encode("utf-8")).hexdigest()[:12]
    return f"{slug}-{digest}" if slug else digest


def safe_suffix(filename: str) -> str:
    """Return the sanitised, lower-cased extension of a caller-supplied name.

    The filename is caller-controlled and is never trusted beyond **routing**:
    only its extension survives, and only to pick an ingestor. ``Path(...)``
    already reduces it to a suffix that cannot contain a path separator, the
    regex strips everything that is not alphanumeric or a dot, and the result
    is lower-cased because :func:`creek.ingest.route_to_ingestor` lower-cases
    too — so ``REPORT.PDF`` and ``report.pdf`` stage identically and route
    identically.

    Args:
        filename: The caller's original filename, of any shape.

    Returns:
        A suffix beginning with ``"."`` (e.g. ``".docx"``), or ``""`` for an
        extensionless name — which routes to the ``generic`` ingestor.
    """
    return _SUFFIX_RE.sub("", Path(filename).suffix.lower())[:MAX_SUFFIX_CHARS]
