"""Real-shaped export archives, built in-process, for the #1525 suites.

Shared by ``tests/test_ingest_archive.py``, ``tests/test_mcp_upload_archive.py``
and ``tests/test_v1_api_upload.py`` so all three drive the *same* bytes: an
upload accepted by the tool and refused by the route, or vice versa, is exactly
the divergence three private fixture sets would hide.

Nothing here is a checked-in binary and nothing is a pasted base64 literal —
every archive is assembled from a dict at call time, so the export's shape is
readable in the diff that changes it.

**The ChatGPT fixture is the important one.** Its ``mapping`` is a real graph:
each node carries a ``children`` array, and the conversation *branches*, with
the discarded branch carrying a sentinel body. A reader that walked ``parent``
pointers alone, or that took the first child at each fork, produces a
different — and in the parent-only case empty — set of fragments, so the
assertions built on this fixture can tell those readers apart from a correct
one. That hazard is recorded on issue #1525 from a live run: parent-only
traversal yields zero fragments while looking like success.
"""

from __future__ import annotations

import io
import json
import zipfile
from typing import Any, Final

CHATGPT_KEPT_ANSWER: Final[str] = "It waits under the gravel until the rain."
"""Body on the LONGEST branch of the branching conversation — must be ingested."""

CHATGPT_DISCARDED_ANSWER: Final[str] = "short-branch-answer-that-must-not-be-kept"
"""Body on the shorter branch — must NOT be ingested.

The sentinel that makes the children-walk assertion falsifiable. A reader that
follows ``children`` but picks the first fork rather than the longest one keeps
this text instead of :data:`CHATGPT_KEPT_ANSWER`, and a reader that ignores
``children`` keeps neither.
"""

CHATGPT_FIRST_QUESTION: Final[str] = "How does a creek find its bed?"
"""The opening user turn — the owner's own words, so a voice-bearing fragment."""

CLAUDE_QUESTION: Final[str] = "What is the difference between a thread and an eddy?"
"""A human turn in the Claude fixture."""

DISCORD_MESSAGE: Final[str] = "The gravel bar moved again after the storm."
"""A message body in the Discord fixture."""

SUBSTACK_BODY: Final[str] = "The channel remembers what the water forgets."
"""A sentence in the body of every Substack post fixture."""

_EPOCH: Final[float] = 1_700_000_000.0
"""Fixed ``create_time`` base, so nothing in these fixtures reads the clock."""

_ZIP_MEMBER_DATE_TIME: Final[tuple[int, int, int, int, int, int]] = (
    1980,
    1,
    1,
    0,
    0,
    0,
)
"""Fixed DOS timestamp stamped into every member these helpers write.

The other half of the promise :data:`_EPOCH` makes. ``ZipFile.writestr`` given
a plain ``str`` name synthesises a ``ZipInfo`` from ``time.localtime()``, so
two calls a second apart return archives that differ in four header bytes.
That is invisible until an archive's bytes become a **pytest parameter id**:
``pytest-xdist`` collects independently on every worker, compares the id lists
and aborts the whole run with "Different tests were collected between gw0 and
gw1" whenever collection straddles a second boundary — a flake that reddens CI
on a diff that touched nothing near it.

1980-01-01 is the DOS epoch, the earliest a ZIP can encode.
"""


def _member(name: str, compress_type: int = zipfile.ZIP_STORED) -> zipfile.ZipInfo:
    """Return a clock-free :class:`zipfile.ZipInfo` for *name*.

    Args:
        name: The archive-relative member name.
        compress_type: The member's compression, matching what the enclosing
            :class:`zipfile.ZipFile` was opened with — ``writestr`` takes it
            from the ``ZipInfo`` rather than the file when handed one.

    Returns:
        A ``ZipInfo`` stamped with :data:`_ZIP_MEMBER_DATE_TIME`.
    """
    info = zipfile.ZipInfo(name, date_time=_ZIP_MEMBER_DATE_TIME)
    info.compress_type = compress_type
    return info


def _node(
    node_id: str,
    parent: str | None,
    children: list[str],
    role: str | None = None,
    text: str | None = None,
    offset: float = 0.0,
) -> dict[str, Any]:
    """Build one ChatGPT ``mapping`` node.

    Args:
        node_id: The node's id, and its key in the mapping.
        parent: The parent node's id, or ``None`` for the root.
        children: Child node ids, in the order the export declares them.
        role: ``user`` / ``assistant``, or ``None`` for a message-less root.
        text: The message body.
        offset: Seconds past :data:`_EPOCH` for this node's ``create_time``.

    Returns:
        The node dict.
    """
    message: dict[str, Any] | None = None
    if role is not None:
        message = {
            "id": node_id,
            "author": {"role": role},
            "create_time": _EPOCH + offset,
            "content": {"content_type": "text", "parts": [text]},
        }
    return {"id": node_id, "message": message, "parent": parent, "children": children}


def chatgpt_conversations() -> list[dict[str, Any]]:
    """Return a two-conversation ChatGPT export as Python objects.

    The first conversation branches at its third turn: ``u2`` declares two
    children, one leading to a single dead-end answer and one leading to two
    further turns. ChatGPT's own reader follows the longest branch, so the
    dead-end body must never reach the vault.

    Returns:
        The export's top-level list of conversation dicts.
    """
    branching = {
        "root": _node("root", None, ["u1"]),
        "u1": _node("u1", "root", ["a1"], "user", CHATGPT_FIRST_QUESTION, 1),
        "a1": _node("a1", "u1", ["u2"], "assistant", "By following low ground.", 2),
        "u2": _node("u2", "a1", ["short", "long"], "user", "And in a drought?", 3),
        "short": _node("short", "u2", [], "assistant", CHATGPT_DISCARDED_ANSWER, 4),
        "long": _node("long", "u2", ["u3"], "assistant", CHATGPT_KEPT_ANSWER, 5),
        "u3": _node("u3", "long", ["a3"], "user", "Does the water remember?", 6),
        "a3": _node("a3", "u3", [], "assistant", "The channel remembers for it.", 7),
    }
    second = {
        "r2": _node("r2", None, ["q2"]),
        "q2": _node("q2", "r2", ["s2"], "user", "What is an eddy?", 1),
        "s2": _node("s2", "q2", [], "assistant", "A pocket of turning water.", 2),
    }
    return [
        {
            "id": "conv-branching",
            "title": "Creek beds",
            "create_time": _EPOCH,
            "mapping": branching,
        },
        {
            "id": "conv-second",
            "title": "Eddies",
            "create_time": _EPOCH + 1000,
            "mapping": second,
        },
    ]


def chatgpt_archive(*, prefix: str = "") -> bytes:
    """Return a ``.zip`` holding a ChatGPT export.

    Args:
        prefix: Optional directory the export is nested under, as the real
            download is when a user zips the unpacked folder again.

    Returns:
        The archive's bytes.
    """
    return _zip(
        {
            f"{prefix}conversations.json": json.dumps(chatgpt_conversations()).encode(),
            f"{prefix}user.json": json.dumps({"id": "user-1"}).encode(),
        }
    )


def claude_archive(*, prefix: str = "") -> bytes:
    """Return a ``.zip`` holding a Claude conversation export.

    Args:
        prefix: Optional directory the export is nested under.

    Returns:
        The archive's bytes.
    """
    export = {
        "conversations": [
            {
                "uuid": "claude-conv-1",
                "name": "Threads and eddies",
                "created_at": "2024-11-15T10:30:00Z",
                "messages": [
                    {
                        "role": "human",
                        "content": CLAUDE_QUESTION,
                        "created_at": "2024-11-15T10:30:00Z",
                    },
                    {
                        "role": "assistant",
                        "content": "A thread runs; an eddy turns in place.",
                        "created_at": "2024-11-15T10:30:20Z",
                    },
                    {
                        "role": "human",
                        "content": "Can an eddy become a thread?",
                        "created_at": "2024-11-15T10:31:00Z",
                    },
                    {
                        "role": "assistant",
                        "content": "When the bank gives way, yes.",
                        "created_at": "2024-11-15T10:31:30Z",
                    },
                ],
            }
        ]
    }
    return _zip({f"{prefix}conversations.json": json.dumps(export).encode()})


def discord_archive(*, prefix: str = "") -> bytes:
    """Return a ``.zip`` holding a Discord data export.

    Two channel directories under ``messages/``, each with the ``messages.json``
    and ``channel.json`` pair
    :meth:`creek.ingest.discord.DiscordIngestor.discover` walks.

    Args:
        prefix: Optional directory the export is nested under — the real
            Discord download nests everything under ``package/``.

    Returns:
        The archive's bytes.
    """
    members: dict[str, bytes] = {}
    for index, (channel_id, channel_name) in enumerate(
        [("110011", "field-notes"), ("220022", "riverbank")]
    ):
        messages = [
            {
                "id": f"m-{channel_id}-{step}",
                "author": {"id": "user-geoff", "name": "geoff"},
                "content": f"{DISCORD_MESSAGE} ({channel_name} {step})",
                "timestamp": f"2024-11-{10 + index:02d}T1{step}:00:00Z",
            }
            for step in range(3)
        ]
        base = f"{prefix}messages/{channel_id}"
        members[f"{base}/messages.json"] = json.dumps(messages).encode()
        members[f"{base}/channel.json"] = json.dumps(
            {"id": channel_id, "name": channel_name, "type": "text"}
        ).encode()
    return _zip(members)


def substack_archive(*, prefix: str = "") -> bytes:
    """Return a ``.zip`` holding a Substack export.

    ``posts.csv`` plus three ``<post_id>.<slug>.html`` posts under ``posts/``,
    and one subscriber CSV the ingestor must filter out.

    Args:
        prefix: Optional directory the export is nested under.

    Returns:
        The archive's bytes.
    """
    rows = [
        ("111", "2024-03-15T08:30:00.000Z", "Hello World", "hello-world"),
        ("222", "2025-01-02T12:00:00.000Z", "Second Essay", "second-essay"),
        ("333", "2026-04-01T09:00:00.000Z", "Latest", "latest"),
    ]
    csv = "post_id,post_date,title,subtitle,type,audience,is_published\n" + "".join(
        f"{post_id},{date},{title},A subtitle,newsletter,everyone,true\n"
        for post_id, date, title, _slug in rows
    )
    members: dict[str, bytes] = {
        f"{prefix}posts.csv": csv.encode(),
        f"{prefix}email_list.csv": b"email\nsubscriber@example.com\n",
    }
    for post_id, _date, title, slug in rows:
        members[f"{prefix}posts/{post_id}.{slug}.html"] = (
            f"<html><body><h1>{title}</h1><p>{SUBSTACK_BODY}</p></body></html>"
        ).encode()
    return _zip(members)


def _zip(members: dict[str, bytes]) -> bytes:
    """Return a ``.zip`` holding *members*, in insertion order.

    Args:
        members: Archive-relative name mapped to its bytes.

    Returns:
        The archive's bytes.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in members.items():
            archive.writestr(_member(name, zipfile.ZIP_DEFLATED), payload)
    return buffer.getvalue()


def zip_slip_archive(escape_name: str, payload: bytes) -> bytes:
    """Return an archive whose one member tries to escape the extraction root.

    Args:
        escape_name: The crafted member name, e.g. ``"../../escaped.txt"``.
        payload: What the member would write if the escape succeeded.

    Returns:
        The archive's bytes.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(_member(escape_name), payload)
    return buffer.getvalue()


def symlink_archive(link_name: str, target: str) -> bytes:
    """Return an archive carrying a SYMLINK member pointing at *target*.

    A ZIP records a symlink as an ordinary entry whose body is the link text
    and whose ``external_attr`` high bits say ``S_IFLNK``. Built by hand here
    because :mod:`zipfile` has no API for it — and because an extractor that
    ignores the mode bit writes the link *text* into a regular file and
    silently corrupts the export, while one that honours it without checking
    the target creates an escape hatch out of the staging root.

    Args:
        link_name: The member's name.
        target: The link's target, as its body.

    Returns:
        The archive's bytes.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        info = zipfile.ZipInfo(link_name)
        # 0o120777 << 16 — S_IFLNK plus permissions, exactly as an archive
        # written by `zip --symlinks` records one.
        info.external_attr = (0o120777 << 16) | 0o600
        info.create_system = 3
        archive.writestr(info, target)
    return buffer.getvalue()


_LOCAL_HEADER_SIGNATURE: Final[bytes] = b"PK\x03\x04"
"""Start of a ZIP local file header."""

_CENTRAL_HEADER_SIGNATURE: Final[bytes] = b"PK\x01\x02"
"""Start of a ZIP central-directory record."""

_LOCAL_UNCOMPRESSED_OFFSET: Final[int] = 22
"""Byte offset of the uncompressed-size field within a local file header."""

_CENTRAL_UNCOMPRESSED_OFFSET: Final[int] = 24
"""Byte offset of the uncompressed-size field within a central-directory record."""

_SIZE_FIELD_BYTES: Final[int] = 4
"""Width of a ZIP size field."""


def _restated_size(raw: bytes, declared: int) -> bytes:
    """Return *raw* with its single member's declared size rewritten.

    Patched at the two known field offsets rather than by searching for the
    old value's bytes: a four-byte search would also hit a CRC, a timestamp or
    the payload itself, and a fixture that corrupts an archive in an unrelated
    place proves nothing about the bound it was written to test.

    Args:
        raw: A ZIP holding exactly one member, written by :mod:`zipfile`.
        declared: The size to claim, in both the local header and the central
            directory, so the archive stays internally consistent.

    Returns:
        The patched archive.
    """
    patched = bytearray(raw)
    claim = declared.to_bytes(_SIZE_FIELD_BYTES, "little")
    for signature, offset in (
        (_LOCAL_HEADER_SIGNATURE, _LOCAL_UNCOMPRESSED_OFFSET),
        (_CENTRAL_HEADER_SIGNATURE, _CENTRAL_UNCOMPRESSED_OFFSET),
    ):
        start = patched.index(signature) + offset
        patched[start : start + _SIZE_FIELD_BYTES] = claim
    return bytes(patched)


def _one_member_zip(payload: bytes) -> bytes:
    """Return a one-member ZIP carrying *payload* as ``conversations.json``.

    Args:
        payload: The member's bytes.

    Returns:
        The archive's bytes.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(_member("conversations.json", zipfile.ZIP_DEFLATED), payload)
    return buffer.getvalue()


def declared_huge_archive(declared_bytes: int) -> bytes:
    """Return a SMALL archive whose header CLAIMS *declared_bytes* of content.

    This is the zip bomb's whole trick, in the form that makes the
    pre-extraction bound testable: the payload really is a kilobyte, so a
    refusal can only have come from reading the declaration. An archive that
    was genuinely huge would be refused by a byte counter too, and the test
    could not tell which guard fired.

    Args:
        declared_bytes: The uncompressed size to claim.

    Returns:
        The archive's bytes.
    """
    return _restated_size(_one_member_zip(b"0" * 1024), declared_bytes)


def underdeclared_archive(real_bytes: int, declared_bytes: int) -> bytes:
    """Return an archive whose header claims LESS than the member really holds.

    The other half of "a declared size can lie", and the one that decides
    whether the pre-extraction bound is sound: if a reader honoured the
    declaration only as a hint and then wrote everything the stream produced,
    checking the declaration would bound nothing.

    Args:
        real_bytes: How many bytes the member actually compresses.
        declared_bytes: The smaller size to claim.

    Returns:
        The archive's bytes.
    """
    return _restated_size(_one_member_zip(b"x" * real_bytes), declared_bytes)
