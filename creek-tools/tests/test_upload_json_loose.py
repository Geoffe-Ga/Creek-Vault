"""A loose conversation-export ``.json`` is recognised by its contents (#2148).

Discord and ChatGPT hand a person ``.json`` files. Before #2148 only a ``.zip``
got content-based export detection, so the same ``conversations.json`` that
ingested correctly inside an archive was refused on its own: the #1526 format
gate answers every ``.json`` with "send the whole export as a .zip".

**The RED this was written against**: uploading the ChatGPT fixture's
``conversations.json`` as a single file returned ``status: "refused"`` with
``Creek cannot ingest a '.json' file as a single document.`` and wrote zero
fragments.

What must stay true is asserted as hard as what changed: a ``.json`` that
matches no export keeps the refusal it has always had, word for word, with
nothing written and nothing staged. adepthood maps that refusal to "Skipped".

Every claim is checked on disk in a temp vault. The parity tests upload the
same document inside a ``.zip`` into a twin vault and compare the fragment
files byte for byte, rather than restating what an ingestor should produce.
"""

from __future__ import annotations

import base64
import io
import json
import shutil
import zipfile
from typing import TYPE_CHECKING, Any, Final

import frontmatter
import pytest

from creek.ingest import STRUCTURED_EXPORT_GUIDANCE
from creek.ingest.archive import detect_document_export_type
from creek.ingest.journal_staging import (
    ARCHIVE_UNPACK_RELDIR,
    UPLOAD_STAGING_RELDIR,
)
from creek.ingest.json_loose import (
    LOOSE_DISCORD_CHANNEL,
    LOOSE_EXPORT_LAYOUT,
    lay_out_loose_export,
)
from creek.ingest.upload_receipts import read_upload_receipts
from creek_mcp.audit import MCP_AUDIT_RELPATH
from creek_mcp.tier_ceiling import TierCeiling
from creek_mcp.tools.upload import (
    ARCHIVE_NO_FRAGMENTS_REASON,
    MAX_UPLOAD_BYTES,
    upload_tool,
)
from tests.archive_export_support import (
    CHATGPT_DISCARDED_ANSWER,
    CHATGPT_FIRST_QUESTION,
    CHATGPT_KEPT_ANSWER,
    CLAUDE_QUESTION,
    DISCORD_MESSAGE,
    chatgpt_conversations,
    claude_export,
    discord_channel_messages,
)

if TYPE_CHECKING:
    from pathlib import Path

_TS: Final[str] = "2026-11-25T10:00:00+00:00"
_EXTERNAL_ID: Final[str] = "adepthood:export:2148"
_REFUSAL_KEYS: Final[set[str]] = {"status", "tool", "tier_ceiling", "reason"}

_DATA_PACKAGE_MESSAGES: Final[list[dict[str, str]]] = [
    {
        "ID": "1300000000000000001",
        "Timestamp": "2024-11-10 10:00:00",
        "Contents": f"{DISCORD_MESSAGE} (package 0)",
        "Attachments": "",
    },
    {
        "ID": "1300000000000000002",
        "Timestamp": "2024-11-10 10:01:00",
        "Contents": f"{DISCORD_MESSAGE} (package 1)",
        "Attachments": "",
    },
]
"""A ``messages.json`` in the capitalised schema Discord's data package writes."""

_UNRECOGNISED: Final[dict[str, bytes]] = {
    "package-json": json.dumps(
        {"name": "creek", "version": "1.0.0", "dependencies": {}}
    ).encode(),
    "arbitrary-object": json.dumps({"alpha": 1, "beta": [1, 2, 3]}).encode(),
    "list-of-unrelated-dicts": json.dumps([{"sku": "a1", "price": 3}]).encode(),
    "list-of-scalars": json.dumps([1, 2, 3]).encode(),
    "empty-list": b"[]",
    "scalar": b"42",
    "malformed": b'{"conversations": [',
    "not-utf8-json": b"\xff\xfe\x00not json",
    "whitespace-only": b" ",
    "nested-past-the-recursion-limit": b"[" * 100_000,
}
"""``.json`` payloads that match no export and must keep today's refusal."""


def _json(document: object) -> bytes:
    """Return *document* serialised as the bytes a loose file would hold."""
    return json.dumps(document).encode()


def _vault(tmp_path: Path, name: str = "vault") -> Path:
    """Create the minimum vault layout the writer, ledger and audit need.

    Args:
        tmp_path: pytest's per-test directory.
        name: Directory name, so a test can build a twin.

    Returns:
        The vault root.
    """
    vault = tmp_path / name
    for sub in ("00-Creek-Meta/State", "00-Creek-Meta/audit", "01-Fragments"):
        (vault / sub).mkdir(parents=True, exist_ok=True)
    return vault


def _upload(
    vault: Path,
    payload: bytes,
    *,
    filename: str = "conversations.json",
    external_id: str = _EXTERNAL_ID,
    tier: str = "personal",
    ceiling: TierCeiling = TierCeiling.PERSONAL,
) -> dict[str, Any]:
    """Upload *payload* under *filename* and return the tool's response."""
    return upload_tool(
        vault_path=vault,
        filename=filename,
        content_base64=base64.b64encode(payload).decode("ascii"),
        external_id=external_id,
        timestamp=_TS,
        tier=tier,
        privacy_tier_ceiling=ceiling,
        consumer="adepthood",
    )


def _fragments(vault: Path) -> list[Path]:
    """Return every fragment file the vault holds, sorted."""
    return sorted((vault / "01-Fragments").rglob("*.md"))


def _corpus(vault: Path) -> dict[str, str]:
    """Return each fragment's text keyed by its vault-relative path."""
    return {
        str(path.relative_to(vault)): path.read_text(encoding="utf-8")
        for path in _fragments(vault)
    }


def _settled_corpus(vault: Path) -> dict[str, tuple[dict[str, Any], str]]:
    """Return each fragment's frontmatter and body, less its ingest instant.

    ``ingested`` is the wall-clock time of the run, the one field two runs of
    the same document legitimately disagree on.
    """
    corpus: dict[str, tuple[dict[str, Any], str]] = {}
    for path in _fragments(vault):
        post = frontmatter.load(path)
        metadata = dict(post.metadata)
        del metadata["ingested"]
        corpus[str(path.relative_to(vault))] = (metadata, post.content)
    return corpus


def _bodies(vault: Path) -> str:
    """Return every fragment's raw text, concatenated."""
    return "\n".join(_corpus(vault).values())


def _platforms(vault: Path) -> set[str]:
    """Return the ``source.platform`` of every fragment on disk."""
    return {
        str(frontmatter.load(path)["source"]["platform"]) for path in _fragments(vault)
    }


def _residue(vault: Path) -> list[Path]:
    """Return every file left in the unpack scratch root or the staging dir."""
    left: list[Path] = []
    for reldir in (ARCHIVE_UNPACK_RELDIR, UPLOAD_STAGING_RELDIR):
        root = vault / reldir
        if root.exists():
            left.extend(sorted(root.rglob("*")))
    return left


def _zip(name: str, payload: bytes) -> bytes:
    """Return a ``.zip`` holding *payload* as the single member *name*."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0)), payload
        )
    return buffer.getvalue()


# --------------------------------------------------------------------------- #
# Detection — one document, decided from its contents
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("document", "expected"),
    [
        (chatgpt_conversations(), "chatgpt"),
        (claude_export(), "claude"),
        ({"uuid": "c-1", "messages": []}, "claude"),
        ({"conversation_id": "c-1", "messages": []}, "claude"),
        (discord_channel_messages(), "discord"),
        (_DATA_PACKAGE_MESSAGES, "discord"),
    ],
)
def test_a_recognised_document_names_its_own_export(
    document: object, expected: str
) -> None:
    """Each export family's document is named from its contents alone."""
    assert detect_document_export_type(_json(document)) == expected


@pytest.mark.parametrize("case", sorted(_UNRECOGNISED))
def test_an_unrecognised_document_names_no_export(case: str) -> None:
    """Anything that is not one of the three shapes is nobody's export."""
    assert detect_document_export_type(_UNRECOGNISED[case]) is None


def test_chatgpt_and_discord_lists_are_disjoint() -> None:
    """Both are "a JSON list of dicts"; each still reaches only its own name.

    The ambiguity ``_looks_like_chatgpt`` documents. A Discord message that
    happened to carry a ``mapping`` key is still not a ChatGPT conversation's
    twin: ``mapping`` decides, and it decides for ChatGPT.
    """
    discord = discord_channel_messages()
    hybrid = [{**discord[0], "mapping": {}}]

    assert detect_document_export_type(_json(discord)) == "discord"
    assert detect_document_export_type(_json(chatgpt_conversations())) == "chatgpt"
    assert detect_document_export_type(_json(hybrid)) == "chatgpt"


def test_a_message_missing_its_identity_is_not_a_discord_export() -> None:
    """A list of dicts needs a whole message signature, not one familiar key."""
    partial = [{"id": "1", "content": "hello"}]

    assert detect_document_export_type(_json(partial)) is None


# --------------------------------------------------------------------------- #
# Layout — where each ingestor's ``discover()`` looks
# --------------------------------------------------------------------------- #


def test_every_loose_export_has_a_layout() -> None:
    """The layout table covers exactly the families detection can name."""
    assert set(LOOSE_EXPORT_LAYOUT) == {"chatgpt", "claude", "discord"}
    assert LOOSE_EXPORT_LAYOUT["discord"].parts == (
        "messages",
        LOOSE_DISCORD_CHANNEL,
        "messages.json",
    )


def test_laying_out_writes_the_bytes_verbatim(tmp_path: Path) -> None:
    """The document lands where the ingestor discovers it, unchanged."""
    payload = _json(discord_channel_messages())

    root = lay_out_loose_export("discord", payload, tmp_path)

    assert root == tmp_path
    assert (tmp_path / LOOSE_EXPORT_LAYOUT["discord"]).read_bytes() == payload


# --------------------------------------------------------------------------- #
# Upload — each family reaches its own ingestor
# --------------------------------------------------------------------------- #


def test_a_lone_chatgpt_export_is_ingested_by_the_chatgpt_ingestor(
    tmp_path: Path,
) -> None:
    """One ``conversations.json`` in, one fragment per turn out.

    The fixture branches, so the kept and discarded answers prove the real
    ChatGPT reader ran rather than something that merely split a list.
    """
    vault = _vault(tmp_path)

    response = _upload(vault, _json(chatgpt_conversations()))

    assert response["status"] == "ok"
    assert response["source_type"] == "chatgpt"
    assert len(response["affected_fragment_ids"]) > 1
    assert len(_fragments(vault)) == len(response["affected_fragment_ids"])
    bodies = _bodies(vault)
    assert CHATGPT_FIRST_QUESTION in bodies
    assert CHATGPT_KEPT_ANSWER in bodies
    assert CHATGPT_DISCARDED_ANSWER not in bodies
    assert _platforms(vault) == {"chatgpt"}


def test_a_lone_claude_export_is_ingested_by_the_claude_ingestor(
    tmp_path: Path,
) -> None:
    """A Claude export JSON on its own reaches the Claude ingestor."""
    vault = _vault(tmp_path)

    response = _upload(vault, _json(claude_export()), filename="claude.json")

    assert response["status"] == "ok"
    assert response["source_type"] == "claude"
    assert CLAUDE_QUESTION in _bodies(vault)
    assert _platforms(vault) == {"claude"}


@pytest.mark.parametrize(
    "messages",
    [discord_channel_messages(), _DATA_PACKAGE_MESSAGES],
    ids=["lowercase-schema", "data-package-schema"],
)
def test_a_lone_discord_messages_file_is_ingested_by_the_discord_ingestor(
    tmp_path: Path, messages: list[dict[str, Any]]
) -> None:
    """A ``messages.json`` with no ``channel.json`` falls back like a bare dir.

    A channel directory without ``channel.json`` is named after the directory;
    a loose file has no directory, so it gets the one this path lays out.
    """
    vault = _vault(tmp_path)

    response = _upload(vault, _json(messages), filename="messages.json")

    assert response["status"] == "ok"
    assert response["source_type"] == "discord"
    assert DISCORD_MESSAGE in _bodies(vault)
    assert _platforms(vault) == {"discord"}
    channels = {
        str(frontmatter.load(path)["source"]["channel"]) for path in _fragments(vault)
    }
    assert channels == {LOOSE_DISCORD_CHANNEL}


def test_a_chatgpt_and_a_discord_file_each_reach_their_own_ingestor(
    tmp_path: Path,
) -> None:
    """The acceptance criterion's pair: two lists of dicts, two ingestors.

    Both are sent under the *same* misleading filename, so nothing but the
    contents can have decided.
    """
    vault = _vault(tmp_path)

    chatgpt = _upload(
        vault,
        _json(chatgpt_conversations()),
        filename="data.json",
        external_id="adepthood:export:2148:a",
    )
    discord = _upload(
        vault,
        _json(discord_channel_messages()),
        filename="data.json",
        external_id="adepthood:export:2148:b",
    )

    assert chatgpt["source_type"] == "chatgpt"
    assert discord["source_type"] == "discord"
    assert _platforms(vault) == {"chatgpt", "discord"}
    assert not set(chatgpt["affected_fragment_ids"]) & set(
        discord["affected_fragment_ids"]
    )


def test_the_filename_alone_never_decides(tmp_path: Path) -> None:
    """A ``conversations.json`` that is not an export is still refused."""
    vault = _vault(tmp_path)

    response = _upload(vault, _UNRECOGNISED["package-json"])

    assert response["status"] == "refused"
    assert _fragments(vault) == []


# --------------------------------------------------------------------------- #
# Parity with the same document inside a ``.zip``
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("document", "source_type"),
    [(chatgpt_conversations(), "chatgpt"), (claude_export(), "claude")],
    ids=["chatgpt", "claude"],
)
def test_a_loose_export_lands_exactly_as_the_zipped_one_does(
    tmp_path: Path, document: object, source_type: str
) -> None:
    """Same ids, same paths, same fragments: authored dates included.

    The same vault path receives the document twice, emptied in between: once
    loose under an arbitrary filename, once as ``conversations.json`` inside a
    ``.zip``, both under one ``external_id``. The vault path is held fixed
    because fragment ids hash the absolute source path. Whole fragments are
    compared, less the one field that records when the ingest ran.
    """
    payload = _json(document)

    vault = _vault(tmp_path)
    loose = _upload(vault, payload, filename="my-export.json")
    loose_corpus = _settled_corpus(vault)
    shutil.rmtree(vault)

    vault = _vault(tmp_path)
    zipped = _upload(vault, _zip("conversations.json", payload), filename="export.zip")
    zipped_corpus = _settled_corpus(vault)

    assert loose["source_type"] == zipped["source_type"] == source_type
    assert loose["affected_fragment_ids"] == zipped["affected_fragment_ids"]
    assert loose_corpus == zipped_corpus
    assert len(loose_corpus) > 1
    assert all(
        metadata["authored_at"] is not None for metadata, _body in loose_corpus.values()
    )
    assert set(loose) == set(zipped)


# --------------------------------------------------------------------------- #
# What must not change — the unrecognised ``.json`` and its neighbours
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("case", sorted(_UNRECOGNISED))
def test_an_unrecognised_json_keeps_the_refusal_it_always_had(
    tmp_path: Path, case: str
) -> None:
    """Nothing written, nothing staged, and the #1526 reason word for word."""
    vault = _vault(tmp_path)

    response = _upload(vault, _UNRECOGNISED[case], filename="package.json")

    assert set(response) == _REFUSAL_KEYS
    assert response["status"] == "refused"
    assert response["reason"] == (
        "Creek cannot ingest a '.json' file as a single document. "
        f"{STRUCTURED_EXPORT_GUIDANCE}"
    )
    assert _fragments(vault) == []
    assert _residue(vault) == []
    assert read_upload_receipts(vault) == []


@pytest.mark.parametrize("suffix", [".jsonl", ".ndjson"])
def test_line_delimited_json_stays_refused_even_when_it_holds_an_export(
    tmp_path: Path, suffix: str
) -> None:
    """Only ``.json`` is sniffed; its line-delimited cousins keep the gate."""
    vault = _vault(tmp_path)

    response = _upload(
        vault, _json(chatgpt_conversations()), filename=f"conversations{suffix}"
    )

    assert response["status"] == "refused"
    assert response["reason"].endswith(STRUCTURED_EXPORT_GUIDANCE)
    assert _fragments(vault) == []
    assert _residue(vault) == []


def test_an_uppercase_extension_is_sniffed_too(tmp_path: Path) -> None:
    """``.JSON`` is the same extension, as it is for the format gate."""
    vault = _vault(tmp_path)

    response = _upload(
        vault, _json(chatgpt_conversations()), filename="CONVERSATIONS.JSON"
    )

    assert response["status"] == "ok"
    assert response["source_type"] == "chatgpt"


def test_a_recognised_but_empty_export_is_refused_rather_than_called_ok(
    tmp_path: Path,
) -> None:
    """A Claude export with no conversations wrote nothing, and says so."""
    vault = _vault(tmp_path)

    response = _upload(vault, _json({"conversations": []}))

    assert response["status"] == "refused"
    assert response["reason"] == ARCHIVE_NO_FRAGMENTS_REASON
    assert _fragments(vault) == []
    assert _residue(vault) == []


# --------------------------------------------------------------------------- #
# The gates and guarantees every upload shares
# --------------------------------------------------------------------------- #


def test_a_loose_export_leaves_no_extracted_or_staged_bytes(tmp_path: Path) -> None:
    """The laid-out copy is scratch, removed before the call returns."""
    vault = _vault(tmp_path)

    response = _upload(vault, _json(chatgpt_conversations()))

    assert response["status"] == "ok"
    assert _residue(vault) == []


def test_re_sending_the_same_loose_export_duplicates_nothing(tmp_path: Path) -> None:
    """The ``external_id`` is the idempotency key, as for any upload."""
    vault = _vault(tmp_path)
    payload = _json(chatgpt_conversations())

    first = _upload(vault, payload)
    after_first = _corpus(vault)
    second = _upload(vault, payload)

    assert first["action"] == "created"
    assert second["status"] == "ok"
    assert second["action"] == "unchanged"
    assert second["affected_fragment_ids"] == first["affected_fragment_ids"]
    assert _corpus(vault) == after_first


def test_a_loose_export_above_the_ceiling_is_refused_unparsed(tmp_path: Path) -> None:
    """The write-tier gate sits above detection, as above everything else."""
    vault = _vault(tmp_path)

    response = _upload(
        vault,
        _json(chatgpt_conversations()),
        tier="intimate",
        ceiling=TierCeiling.OPEN,
    )

    assert response["status"] == "refused"
    assert "exceeds the ceiling" in response["reason"]
    assert _fragments(vault) == []
    assert _residue(vault) == []


def test_every_fragment_carries_the_declared_tier(tmp_path: Path) -> None:
    """The declared tier is stamped on each fragment, as on an archive's."""
    vault = _vault(tmp_path)

    _upload(
        vault,
        _json(chatgpt_conversations()),
        tier="intimate",
        ceiling=TierCeiling.INTIMATE,
    )

    tiers = {str(frontmatter.load(path)["privacy_tier"]) for path in _fragments(vault)}
    assert tiers == {"intimate"}


def test_an_oversize_loose_export_is_refused_by_the_shared_cap(
    tmp_path: Path,
) -> None:
    """The single-file size cap applies before a byte is parsed."""
    vault = _vault(tmp_path)
    padding = "x" * MAX_UPLOAD_BYTES
    payload = _json([{"mapping": {}, "title": padding}])

    response = _upload(vault, payload)

    assert response["status"] == "refused"
    assert response["reason"].endswith("-byte cap")
    assert _fragments(vault) == []


def test_a_loose_export_is_receipted_for_the_pipeline_status_route(
    tmp_path: Path,
) -> None:
    """Each fragment gets an upload receipt naming the caller's file."""
    vault = _vault(tmp_path)

    response = _upload(vault, _json(chatgpt_conversations()))
    receipts = read_upload_receipts(vault)

    assert [r.fragment_id for r in receipts] == response["affected_fragment_ids"]
    assert {r.filename for r in receipts} == {"conversations.json"}
    assert {r.external_id for r in receipts} == {_EXTERNAL_ID}


def test_a_loose_export_is_audited_with_every_fragment_id(tmp_path: Path) -> None:
    """The audit entry reaches every fragment, and holds no document bytes."""
    vault = _vault(tmp_path)

    response = _upload(vault, _json(chatgpt_conversations()))
    log = (vault / MCP_AUDIT_RELPATH).read_text(encoding="utf-8")
    entry = json.loads(log.splitlines()[-1])

    assert entry["affected_fragment_ids"] == response["affected_fragment_ids"]
    assert CHATGPT_FIRST_QUESTION not in log
