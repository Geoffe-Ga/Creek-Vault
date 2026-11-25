"""Lay out a conversation export that arrives as one LOOSE ``.json`` file (#2148).

ChatGPT, Claude and Discord each hand a person ``.json`` files. Zipped, the
export is identified from its contents by
:func:`creek.ingest.archive.detect_export_type` and read by a directory
ingestor. Sent on its own, the same file used to be refused by the #1526 format
gate with "send the whole export as a .zip" — a remedy that asks the owner to
re-pack something they have already unpacked.

The three ingestors are directory-only and stay that way. This module gives a
single recognised document the directory its ingestor's ``discover()`` walks:

* ``chatgpt`` and ``claude`` — ``conversations.json`` at the root, the name
  both platforms give the file and the one both ingestors' ``glob("*.json")``
  finds;
* ``discord`` — ``messages/<channel>/messages.json``, with no ``channel.json``
  beside it, so the channel falls back to its directory name exactly as a
  channel directory without one does.

Which export a file is, is decided by
:func:`creek.ingest.archive.detect_document_export_type` — from the bytes,
never from the filename and never from the caller. The filename is not read
here at all: the laid-out name is fixed per family, so the same document
derives the same fragment ids loose as it does inside a ``.zip`` that holds it
as ``conversations.json``, whatever the caller happened to call the file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

LOOSE_JSON_SUFFIX: Final[str] = ".json"
"""The one extension a loose export is looked for under.

``.jsonl`` and ``.ndjson`` are not conversation exports any of these
platforms write, and keep the #1526 refusal unexamined.
"""

LOOSE_DISCORD_CHANNEL: Final[str] = "unknown"
"""Directory a loose ``messages.json`` is laid out under, and so its channel.

A loose file carries no channel identity — that lives in the ``channel.json``
it was sent without — and :class:`creek.ingest.discord.DiscordIngestor` names
a channel with no metadata after its directory. ``unknown`` is the word that
ingestor already uses for a channel it cannot name.
"""

LOOSE_EXPORT_LAYOUT: Final[dict[str, Path]] = {
    "chatgpt": Path("conversations.json"),
    "claude": Path("conversations.json"),
    "discord": Path("messages") / LOOSE_DISCORD_CHANNEL / "messages.json",
}
"""Where each family's single document sits, relative to the ingest root."""


def is_loose_json(filename: str) -> bool:
    """Return whether *filename* carries the extension a loose export has.

    Args:
        filename: The caller's filename; only its suffix is read.

    Returns:
        ``True`` for ``.json`` in any letter case.
    """
    return Path(filename).suffix.lower() == LOOSE_JSON_SUFFIX


def lay_out_loose_export(source_type: str, raw: bytes, root: Path) -> Path:
    """Write *raw* under *root* where *source_type*'s ingestor discovers it.

    Args:
        source_type: A key of :data:`LOOSE_EXPORT_LAYOUT`, as returned by
            :func:`creek.ingest.archive.detect_document_export_type`.
        raw: The export document's bytes, written verbatim.
        root: An existing scratch directory the caller owns and removes.

    Returns:
        The directory to hand the ingestor, which is *root* itself.
    """
    target = root / LOOSE_EXPORT_LAYOUT[source_type]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(raw)
    return root
