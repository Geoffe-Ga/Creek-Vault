"""``creek.drafts.read`` MCP tool — hand a saved draft or mission note back.

``creek.author`` and ``creek.draft`` jobs save their essay and answer with a
``draft_path`` only, and ``creek.mission`` saves to ``07-Voice/Mission/``; a
remote agent had no way to fetch what it had just written. This tool lists
and reads those two folders and nothing else.

**Posture: GATED, refusing rather than excluding, on the document's own
``privacy_tier`` stamp** — the ``creek.state.read`` rule (#969), for the same
reason: the target is one caller-addressed document, and there is nothing to
partially admit. A document with no stamp, an unparsable stamp or an
unrecognised one ranks as intimate (:func:`stamped_content_tier`), so a draft
written before drafts were stamped, or written by ``creek draft`` on the CLI
under ``--include-tier``, is refused below ``ceiling=all`` rather than
guessed at. The refusal is the generic four-key payload and never names the
tier, and a path naming no document gets the identical refusal: file names
are slugs of titles, so a distinct "missing" answer would confirm that an
above-ceiling draft on a guessed topic exists. The listing applies the same
test per document and simply omits what the ceiling does not admit; a title
is content.

**Confinement.** A ``path`` must be vault-relative, POSIX, and name a ``.md``
file *directly* under ``07-Voice/Drafts`` or ``07-Voice/Mission``. Any other
shape is refused with one fixed reason before the filesystem is touched. A
symlinked document, or a symlinked folder, is treated as absent: the resolved
file must sit in the real folder inside the real vault.

**What comes back.** The body only. The frontmatter stays behind: a
``creek draft`` file carries its whole prompt there, source excerpts
included, and provenance ids are not the caller's to collect.

**Audit.** The attempt is logged first with ``has_path`` only, never the path,
so the trail cannot answer "did consumer X read draft D?".
"""

from __future__ import annotations

from operator import itemgetter
from pathlib import Path, PurePosixPath
from typing import Any, Final

import frontmatter

from creek.generate.state_tiers import stamped_content_tier
from creek.vault.reader import FRONTMATTER_LOAD_ERRORS
from creek_mcp.audit import MCPAuditLog
from creek_mcp.read_gate import GENERIC_ABOVE_CEILING_REASON, refuse_above_ceiling
from creek_mcp.tier_ceiling import TierCeiling, refusal_response, tier_allowed

TOOL_NAME: Final[str] = "creek.drafts.read"

#: The two folders this tool reads, keyed by the ``kind`` it reports.
_FOLDERS: Final[dict[str, tuple[str, str]]] = {
    "draft": ("07-Voice", "Drafts"),
    "mission": ("07-Voice", "Mission"),
}

#: Longest path accepted, in characters.
MAX_PATH_CHARS: Final[int] = 512

#: Largest body returned, in characters. An essay is a few thousand words.
MAX_BODY_CHARS: Final[int] = 200_000

#: Most documents one listing returns, newest file name first.
MAX_LISTED: Final[int] = 100

PATH_REASON: Final[str] = (
    "path must name a .md file directly under 07-Voice/Drafts or 07-Voice/Mission"
)
TOO_LARGE_REASON: Final[str] = "document too large to return"


def drafts_read_tool(
    *,
    vault_path: Path,
    path: str | None = None,
    privacy_tier_ceiling: TierCeiling = TierCeiling.OPEN,
    consumer: str = "unknown",
) -> dict[str, Any]:
    """List the admitted drafts and mission notes, or return one document's body.

    Args:
        vault_path: Root of the Obsidian vault.
        path: A vault-relative path from a listing, or ``None`` to list.
        privacy_tier_ceiling: The caller's ceiling, compared against each
            document's own ``privacy_tier`` stamp.
        consumer: Identifier recorded in the MCP audit log.

    Returns:
        A listing ``{status, tool, tier_ceiling, documents}`` (``empty`` when
        nothing is admitted); one document ``{status: "ok", path, kind, title,
        markdown}``; or a refusal carrying no content, whose reason is fixed:
        a malformed path, too large, or the generic above-ceiling reason,
        which a missing document shares so that a guessed path is no oracle.
    """
    MCPAuditLog(vault_path).append(
        tool=TOOL_NAME,
        args={"has_path": path is not None},
        tier_ceiling=privacy_tier_ceiling,
        consumer=consumer,
    )
    if path is None:
        return _listing(vault_path, privacy_tier_ceiling)
    return _read(vault_path, path, privacy_tier_ceiling)


def _read(vault_path: Path, path: str, ceiling: TierCeiling) -> dict[str, Any]:
    """Return one document's body, or the reason it cannot be returned."""
    kind = _kind_of(path)
    if kind is None:
        return refusal_response(tool=TOOL_NAME, ceiling=ceiling, reason=PATH_REASON)
    target = _confined(vault_path, kind, PurePosixPath(path).name)
    if target is None:
        # The above-ceiling refusal, byte for byte. Draft file names are slugs
        # of their titles, so a distinct "missing" answer would let a caller
        # confirm that an above-ceiling draft on a guessed topic exists.
        return refusal_response(
            tool=TOOL_NAME, ceiling=ceiling, reason=GENERIC_ABOVE_CEILING_REASON
        )
    text = target.read_text(encoding="utf-8")
    if (
        refusal := refuse_above_ceiling(
            tool=TOOL_NAME, content_tier=stamped_content_tier(text), ceiling=ceiling
        )
    ) is not None:
        return refusal
    title, body = _split(text, fallback_title=target.stem)
    if len(body) > MAX_BODY_CHARS:
        return refusal_response(
            tool=TOOL_NAME, ceiling=ceiling, reason=TOO_LARGE_REASON
        )
    return {
        "status": "ok",
        "tool": TOOL_NAME,
        "tier_ceiling": ceiling.value,
        "path": path,
        "kind": kind,
        "title": title,
        "markdown": body,
    }


def _listing(vault_path: Path, ceiling: TierCeiling) -> dict[str, Any]:
    """Return every admitted document in both folders, newest file name first."""
    found: list[tuple[str, dict[str, str]]] = []
    for kind, parts in _FOLDERS.items():
        for name in _markdown_names(vault_path, kind):
            target = _confined(vault_path, kind, name)
            if target is None:
                continue
            text = target.read_text(encoding="utf-8")
            if not tier_allowed(stamped_content_tier(text), ceiling):
                continue
            title, _body = _split(text, fallback_title=target.stem)
            rel = "/".join((*parts, name))
            found.append((name, {"path": rel, "kind": kind, "title": title}))
    found.sort(key=itemgetter(0), reverse=True)
    documents = [entry for _name, entry in found[:MAX_LISTED]]
    return {
        "status": "ok" if documents else "empty",
        "tool": TOOL_NAME,
        "tier_ceiling": ceiling.value,
        "documents": documents,
    }


def _kind_of(path: str) -> str | None:
    """Return the folder kind a well-formed *path* names, else ``None``."""
    if not _well_formed(path):
        return None
    folder = PurePosixPath(path).parts[:2]
    for kind, parts in _FOLDERS.items():
        if folder == parts:
            return kind
    return None


def _well_formed(path: str) -> bool:
    """Return whether *path* is a short, relative, POSIX ``folder/folder/x.md``."""
    if not path or len(path) > MAX_PATH_CHARS or "\\" in path:
        return False
    pure = PurePosixPath(path)
    return (
        not pure.is_absolute()
        and len(pure.parts) == 3
        and pure.suffix == ".md"
        and ".." not in pure.parts
        and not pure.name.startswith(".")
    )


def _confined(vault_path: Path, kind: str, name: str) -> Path | None:
    """Return the real file *name* in *kind*'s folder, or ``None``.

    ``None`` covers a missing file, a symlinked file, and a folder that is
    itself a symlink: the file's resolved parent must be the folder as it
    sits, unresolved, under the resolved vault root.
    """
    folder = vault_path.resolve().joinpath(*_FOLDERS[kind])
    target = folder / name
    if target.is_symlink() or not target.is_file():
        return None
    if target.resolve().parent != folder:
        return None
    return target


def _markdown_names(vault_path: Path, kind: str) -> list[str]:
    """Return the ``.md`` file names in *kind*'s folder (none if it is absent)."""
    folder = vault_path.resolve().joinpath(*_FOLDERS[kind])
    if folder.is_symlink() or not folder.is_dir():
        return []
    return sorted(
        entry.name
        for entry in folder.iterdir()
        if entry.suffix == ".md" and not entry.name.startswith(".")
    )


def _split(text: str, *, fallback_title: str) -> tuple[str, str]:
    """Return a document's title and its body without frontmatter.

    Unparsable frontmatter (reachable only at ``ceiling=all``, since it also
    ranks the document intimate) hands back the whole text as the body. The
    guard is the package's one definition of unreadable frontmatter,
    :data:`~creek.vault.reader.FRONTMATTER_LOAD_ERRORS`, the same tuple
    :func:`stamped_content_tier` catches: a key YAML reads as an int, bool or
    date makes ``frontmatter`` raise ``TypeError``, and the two must agree.
    """
    try:
        post = frontmatter.loads(text)
    except FRONTMATTER_LOAD_ERRORS:
        return fallback_title, text.strip()
    title = post.metadata.get("title")
    return (
        title if isinstance(title, str) and title.strip() else fallback_title,
        post.content.strip(),
    )
