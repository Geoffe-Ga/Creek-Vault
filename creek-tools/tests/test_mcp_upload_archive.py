"""``creek.upload`` over an export ARCHIVE — the end-to-end half of #1525.

Drives the real ledger-free ingest against a temp vault, because every claim
this issue makes is about what ends up on disk rather than about what the tool
says it did.

**The RED this was written against**, recorded on the branch before any of it
existed: uploading the ChatGPT archive built by
:func:`tests.archive_export_support.chatgpt_archive` returned

    ``{"status": "refused", …, "reason": "Creek cannot ingest a '.zip' file as
    a single document. … Unpack it and run `creek ingest …`"}``

with **0 fragments written**. Discord, Claude and Substack exports were in the
same position: all four ingestors are directory-only, and the only upload
surface Creek published took one file. Those are the four sources that carry
years of a person's own voice.

Four things here are worth reading before the tests:

* **The ChatGPT case asserts the children-walk.** Its fixture branches, and the
  short branch carries a sentinel body. A reader that ignored ``children`` and
  walked ``parent`` pointers alone produces **zero** fragments while reporting
  success — the hazard #1525 records from a live run — and one that follows
  ``children`` but takes the first fork keeps the sentinel. Asserting only "more
  than one fragment" would pass for the second reader.

* **The tier is asserted on disk, against a twin vault.** Requirement 5 is that
  a fragment from an archive carries the tier ``creek ingest`` would have given
  it. That is checked by *running* the equivalent ingest into a second vault and
  comparing, rather than by restating a constant this suite would then be free
  to be wrong about in the same direction as the code.

* **The refusals are checked against the filesystem.** A zip-slip archive's
  marker must appear in no file anywhere under the vault's parent, and the
  unpack root must be gone. Asserting the return value alone would pass for an
  extractor that wrote the escape and then noticed.

* **Nothing is left staged.** Unlike a document upload, an archive's bytes are
  never written to the vault at all, and the tree it unpacks to is deleted
  however the call ends. That is asserted on every path, success and refusal
  alike, because a staged export nobody can reach is plaintext the RTBF sweep
  cannot erase.
"""

from __future__ import annotations

import base64
import io
import json
import zipfile
from typing import TYPE_CHECKING, Any, Final

import frontmatter
import pytest

from creek.ingest import INGESTOR_REGISTRY
from creek.ingest.archive import (
    MAX_EXTRACTED_BYTES,
    TOO_LARGE_REASON,
    UNREADABLE_ARCHIVE_REASON,
    UNRECOGNISED_EXPORT_REASON,
    UNSAFE_ENTRY_REASON,
)
from creek.ingest.journal_staging import (
    ARCHIVE_UNPACK_RELDIR,
    UPLOAD_STAGING_RELDIR,
)
from creek.ingest.pipeline import run_ingest
from creek.models import PrivacyTier
from creek_mcp.audit import MCP_AUDIT_RELPATH
from creek_mcp.tier_ceiling import TierCeiling
from creek_mcp.tools.upload import TOOL_NAME, upload_tool
from tests.archive_export_support import (
    CHATGPT_DISCARDED_ANSWER,
    CHATGPT_FIRST_QUESTION,
    CHATGPT_KEPT_ANSWER,
    CLAUDE_QUESTION,
    DISCORD_MESSAGE,
    SUBSTACK_BODY,
    chatgpt_archive,
    chatgpt_conversations,
    claude_archive,
    declared_huge_archive,
    discord_archive,
    substack_archive,
    zip_slip_archive,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

_TS: Final[str] = "2026-08-19T10:00:00+00:00"
_REFUSAL_KEYS: Final[set[str]] = {"status", "tool", "tier_ceiling", "reason"}
_ESCAPE_MARKER: Final[bytes] = b"escaped-past-the-vault-1525"

_ARCHIVES: Final[dict[str, Callable[..., bytes]]] = {
    "chatgpt": chatgpt_archive,
    "claude": claude_archive,
    "discord": discord_archive,
    "substack": substack_archive,
}
"""One archive builder per export family, keyed by the ingestor it must reach."""


def _vault(tmp_path: Path) -> Path:
    """Create the minimum vault layout the writer + ledger + audit need.

    Nested under *tmp_path* rather than being it, so a zip-slip escape's target
    lands somewhere the test can assert on.

    Args:
        tmp_path: pytest's per-test directory.

    Returns:
        The vault root.
    """
    vault = tmp_path / "vault"
    for sub in ("00-Creek-Meta/State", "00-Creek-Meta/audit", "01-Fragments"):
        (vault / sub).mkdir(parents=True, exist_ok=True)
    return vault


def _fragments(vault: Path) -> list[Path]:
    """Return every fragment file the vault holds.

    Args:
        vault: Vault root.

    Returns:
        Sorted fragment paths.
    """
    return sorted((vault / "01-Fragments").rglob("*.md"))


def _bodies(vault: Path) -> str:
    """Return every fragment's raw text, concatenated, for substring checks.

    Args:
        vault: Vault root.

    Returns:
        One string holding all fragment files.
    """
    return "\n".join(path.read_text(encoding="utf-8") for path in _fragments(vault))


def _tiers(vault: Path) -> dict[str, str]:
    """Return each fragment file's on-disk ``privacy_tier``, by filename.

    Read out of the written frontmatter, never off the response: the response
    reports the tier the caller *declared*, and the whole point of requirement
    5 is what the bytes ended up carrying.

    Args:
        vault: Vault root.

    Returns:
        Filename mapped to the tier string on disk.
    """
    return {
        path.name: str(frontmatter.load(path).get("privacy_tier"))
        for path in _fragments(vault)
    }


def _audit(vault: Path) -> list[dict[str, Any]]:
    """Return parsed MCP audit-log entries.

    Args:
        vault: Vault root.

    Returns:
        One dict per audit line.
    """
    log = vault / MCP_AUDIT_RELPATH
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]


def _b64(payload: bytes) -> str:
    """Return the base64 of *payload*, derived at runtime.

    Args:
        payload: The bytes to encode.

    Returns:
        The base64 string.
    """
    return base64.b64encode(payload).decode("ascii")


def _upload(
    vault: Path,
    payload: bytes,
    *,
    external_id: str = "adepthood:export:1525",
    filename: str = "export.zip",
    tier: str = "personal",
    ceiling: TierCeiling = TierCeiling.PERSONAL,
) -> dict[str, Any]:
    """Upload *payload* as an archive and return the tool's response.

    Args:
        vault: Vault root.
        payload: The archive's bytes.
        external_id: The idempotency key.
        filename: The caller's filename, whose suffix picks the archive fork.
        tier: The declared tier.
        ceiling: The caller's admission ceiling.

    Returns:
        The tool's return dict.
    """
    return upload_tool(
        vault_path=vault,
        filename=filename,
        content_base64=_b64(payload),
        external_id=external_id,
        timestamp=_TS,
        tier=tier,
        privacy_tier_ceiling=ceiling,
    )


def _unpack_residue(vault: Path) -> list[Path]:
    """Return everything left under the archive unpack root.

    Args:
        vault: Vault root.

    Returns:
        Every surviving path, empty when the root itself is gone.
    """
    root = vault / ARCHIVE_UNPACK_RELDIR
    if not root.exists():
        return []
    return sorted(root.rglob("*"))


def _staged(vault: Path) -> list[Path]:
    """Return every file under the single-document staging dir.

    Args:
        vault: Vault root.

    Returns:
        Sorted staged files.
    """
    root = vault / UPLOAD_STAGING_RELDIR
    if not root.is_dir():
        return []
    return sorted(path for path in root.rglob("*") if path.is_file())


# --------------------------------------------------------------------------- #
# The premise the whole issue rests on
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("source_type", sorted(_ARCHIVES))
def test_every_export_ingestor_is_directory_only(
    source_type: str, tmp_path: Path
) -> None:
    """Each of the four bails on a file, which is why an archive is required.

    The premise check for this whole module. If any of these ever grew a
    single-file path, the archive fork would still work but its *justification*
    would have quietly expired, and this is where that shows up rather than in
    a design document nobody re-reads.

    Args:
        source_type: The registry key under test.
        tmp_path: pytest's per-test directory.
    """
    lone_file = tmp_path / "conversations.json"
    lone_file.write_bytes(json.dumps(chatgpt_conversations()).encode())

    assert INGESTOR_REGISTRY[source_type]().discover(lone_file) == []


# --------------------------------------------------------------------------- #
# AC2 — all four export families ingest, with per-type structure asserted
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("prefix", ["", "package/"])
@pytest.mark.parametrize("source_type", sorted(_ARCHIVES))
def test_each_export_archive_produces_more_than_one_fragment(
    tmp_path: Path, source_type: str, prefix: str
) -> None:
    """All four families ingest from an archive, wrapped or not.

    "More than one" is the floor rather than the claim: a single fragment is
    what the collapse defect produced (see
    :func:`creek_mcp.tools.upload._archive_run`), so a run that wrote exactly
    one would look successful and have lost the export.

    Args:
        tmp_path: pytest's per-test directory.
        source_type: The export family under test.
        prefix: The directory the export is wrapped in.
    """
    vault = _vault(tmp_path)

    result = _upload(vault, _ARCHIVES[source_type](prefix=prefix))

    assert result["status"] == "ok", result
    assert result["source_type"] == source_type
    assert len(_fragments(vault)) > 1
    assert result["written"] == len(result["affected_fragment_ids"])
    assert result["fragment_id"] == result["affected_fragment_ids"][0]


def test_a_chatgpt_archive_reconstructs_turns_through_the_children_graph(
    tmp_path: Path,
) -> None:
    """The children-walk, asserted so a parent-only reader cannot pass.

    Three separate claims, and each one fails for a different wrong reader:

    * the opening user turn is present — a parent-only traversal finds no root
      to walk from and writes nothing at all;
    * the **longest** branch's answer is present — a reader that took the first
      child at the fork keeps the other one;
    * the short branch's sentinel is present in **no** fragment, which is what
      makes the second claim falsifiable rather than merely satisfied.

    Args:
        tmp_path: pytest's per-test directory.
    """
    vault = _vault(tmp_path)

    result = _upload(vault, chatgpt_archive())

    assert result["status"] == "ok"
    bodies = _bodies(vault)
    assert CHATGPT_FIRST_QUESTION in bodies
    assert CHATGPT_KEPT_ANSWER in bodies
    assert CHATGPT_DISCARDED_ANSWER not in bodies
    # Two conversations, three surviving turn pairs in the first and one in the
    # second, each pair split into the human turn and the AI turn.
    assert len(_fragments(vault)) == 8


def test_a_discord_archive_ingests_every_channel(tmp_path: Path) -> None:
    """Both channel directories are walked, and channel metadata survives.

    A ``messages/`` walk that stopped at the first channel would still clear
    the "more than one fragment" bar.

    Args:
        tmp_path: pytest's per-test directory.
    """
    vault = _vault(tmp_path)

    result = _upload(vault, discord_archive(prefix="package/"))

    assert result["status"] == "ok"
    assert result["source_type"] == "discord"
    bodies = _bodies(vault)
    assert DISCORD_MESSAGE in bodies
    assert "field-notes" in bodies
    assert "riverbank" in bodies


def test_a_claude_archive_ingests_both_turn_pairs(tmp_path: Path) -> None:
    """A Claude export's conversation splits into per-turn fragments.

    Args:
        tmp_path: pytest's per-test directory.
    """
    vault = _vault(tmp_path)

    result = _upload(vault, claude_archive())

    assert result["status"] == "ok"
    assert result["source_type"] == "claude"
    assert CLAUDE_QUESTION in _bodies(vault)
    # Two turn pairs, each split into the human turn and the AI turn.
    assert len(_fragments(vault)) == 4


def test_a_substack_archive_ingests_every_post_and_no_subscriber_csv(
    tmp_path: Path,
) -> None:
    """All three posts land, and the subscriber PII file reaches nothing.

    The export deliberately carries an ``email_list.csv``. Substack ships one,
    and an archive path that handed the whole tree to a less careful reader
    would file a subscriber list as vault content.

    Args:
        tmp_path: pytest's per-test directory.
    """
    vault = _vault(tmp_path)

    result = _upload(vault, substack_archive())

    assert result["status"] == "ok"
    assert result["source_type"] == "substack"
    assert len(_fragments(vault)) == 3
    assert SUBSTACK_BODY in _bodies(vault)
    assert "subscriber@example.com" not in _bodies(vault)


# --------------------------------------------------------------------------- #
# AC3 — zip slip, refused on the filesystem
# --------------------------------------------------------------------------- #


def test_a_zip_slip_archive_writes_nothing_anywhere(tmp_path: Path) -> None:
    """The crafted member escapes nothing, and the proof is a filesystem walk.

    The vault sits two levels under *tmp_path* and the unpack root two levels
    below that, so ``../../`` from the extraction directory targets a real
    place inside the test's own tree — and the assertion sweeps *every* file
    under ``tmp_path``, not merely the vault, because an escape that lands
    outside the vault is the whole failure being tested for.

    Args:
        tmp_path: pytest's per-test directory.
    """
    vault = _vault(tmp_path)

    result = _upload(vault, zip_slip_archive("../../etc/x", _ESCAPE_MARKER))

    assert result["status"] == "refused"
    assert set(result) == _REFUSAL_KEYS
    assert result["reason"] == UNSAFE_ENTRY_REASON
    leaked = [
        path
        for path in sorted(tmp_path.rglob("*"))
        if path.is_file() and _ESCAPE_MARKER in path.read_bytes()
    ]
    assert leaked == []
    assert _fragments(vault) == []
    assert _unpack_residue(vault) == []


def test_a_refusal_names_no_archive_member_or_vault_path(tmp_path: Path) -> None:
    """No refusal on this path echoes the archive's contents back.

    The reasons are module constants precisely so that this holds; asserting it
    from outside is what stops a future "helpful" refusal interpolating the
    offending member into a body other surfaces serve onward.

    Args:
        tmp_path: pytest's per-test directory.
    """
    vault = _vault(tmp_path)
    secret = "a-member-name-that-must-not-be-echoed"

    result = _upload(vault, zip_slip_archive(f"../{secret}.txt", _ESCAPE_MARKER))

    assert result["status"] == "refused"
    assert secret not in result["reason"]
    assert str(vault) not in result["reason"]


# --------------------------------------------------------------------------- #
# AC4 — the declared-size bound, and the rest of the refusal family
# --------------------------------------------------------------------------- #


def test_a_declared_huge_archive_is_refused_before_extraction(
    tmp_path: Path,
) -> None:
    """A zip bomb is refused on its header, with nothing unpacked.

    Args:
        tmp_path: pytest's per-test directory.
    """
    vault = _vault(tmp_path)

    result = _upload(vault, declared_huge_archive(MAX_EXTRACTED_BYTES * 2))

    assert result["status"] == "refused"
    assert result["reason"] == TOO_LARGE_REASON
    assert _fragments(vault) == []
    assert _unpack_residue(vault) == []


@pytest.mark.parametrize(
    ("payload", "case"),
    [
        (b"not a zip at all", "no-signature"),
        (b"PK\x03\x04 and then nothing that parses", "signature-only"),
        (json.dumps(chatgpt_conversations()).encode(), "the-export-unzipped"),
    ],
)
def test_bytes_that_are_not_an_archive_are_refused(
    tmp_path: Path, payload: bytes, case: str
) -> None:
    """A ``.zip`` filename over non-archive bytes is a caller error, named as one.

    The third case is the mistake a real consumer makes: sending the export's
    *JSON* under a ``.zip`` filename. It must land on the archive refusal, not
    be quietly routed somewhere that would file the whole export as one blob.

    Args:
        tmp_path: pytest's per-test directory.
        payload: The bytes sent.
        case: The shape's name, for the failure message.
    """
    vault = _vault(tmp_path)

    result = _upload(vault, payload)

    assert result["status"] == "refused", case
    assert result["reason"] == UNREADABLE_ARCHIVE_REASON, case
    assert _fragments(vault) == [], case
    assert _unpack_residue(vault) == [], case


def test_an_unrecognised_archive_is_refused_with_a_remedy(tmp_path: Path) -> None:
    """An archive Creek cannot name is refused, and told what to do instead.

    Args:
        tmp_path: pytest's per-test directory.
    """
    vault = _vault(tmp_path)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("holiday/readme.txt", b"nothing Creek knows about")

    result = _upload(vault, buffer.getvalue())

    assert result["status"] == "refused"
    assert result["reason"] == UNRECOGNISED_EXPORT_REASON
    assert "creek ingest --type" in result["reason"]
    assert _fragments(vault) == []
    assert _unpack_residue(vault) == []


# --------------------------------------------------------------------------- #
# AC5 — the tier, on disk, against what `creek ingest` would have written
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("declared", ["open", "personal", "intimate"])
@pytest.mark.parametrize("source_type", sorted(_ARCHIVES))
def test_every_fragment_carries_the_declared_tier_on_disk(
    tmp_path: Path, source_type: str, declared: str
) -> None:
    """The archive path's tiers are compared against a real ``creek ingest``.

    The comparison vault is ingested by :func:`creek.ingest.pipeline.run_ingest`
    with **no** declared tier — which is exactly what the CLI does for these
    four source types — so it holds the tier the ingestors themselves produce.
    The archive path must never land *below* it, and must land at the tier the
    caller declared, because
    :func:`creek.ingest.pipeline.stamp_declared_tier` merges escalate-only.

    Restating the expected tier as a literal would have been the wrong test:
    it would pass a change that lowered both paths together, which is the one
    direction ``privacy_tier`` is a one-way ratchet against.

    Args:
        tmp_path: pytest's per-test directory.
        source_type: The export family under test.
        declared: The tier the caller declares.
    """
    payload = _ARCHIVES[source_type]()
    vault = _vault(tmp_path)
    _upload(
        vault,
        payload,
        tier=declared,
        ceiling=TierCeiling.ALL,
    )

    twin = _vault(tmp_path / "twin")
    unpacked = tmp_path / "unpacked"
    unpacked.mkdir()
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        archive.extractall(unpacked)
    run_ingest(
        ingestor_cls=INGESTOR_REGISTRY[source_type],
        source_type=source_type,
        input_path=unpacked,
        vault_path=twin,
    )

    written = _tiers(vault)
    baseline = _tiers(twin)
    assert written
    assert set(written) == set(baseline)
    ranks = {"unclassified": 0, "open": 1, "personal": 2, "intimate": 3}
    for name, tier in written.items():
        assert tier == declared, (name, tier)
        assert ranks[tier] >= ranks[baseline[name]], (name, tier, baseline[name])


def test_an_archive_above_the_ceiling_never_reaches_extraction(
    tmp_path: Path,
) -> None:
    """The write-tier gate still runs first, and it runs before any unpacking.

    An ``intimate`` archive at ``ceiling=open`` must be refused with nothing
    unpacked — the same ordering the single-document path has, asserted here
    because the archive fork sits below that gate and a future reordering would
    put a whole export on disk before the refusal.

    Args:
        tmp_path: pytest's per-test directory.
    """
    vault = _vault(tmp_path)

    result = _upload(
        vault, chatgpt_archive(), tier="intimate", ceiling=TierCeiling.OPEN
    )

    assert result["status"] == "refused"
    assert "exceeds the ceiling" in result["reason"]
    assert _fragments(vault) == []
    assert _unpack_residue(vault) == []


# --------------------------------------------------------------------------- #
# Requirements 5 and 6 — idempotency, and a partial run that says so
# --------------------------------------------------------------------------- #


def test_re_uploading_the_same_archive_duplicates_nothing(tmp_path: Path) -> None:
    """The second send writes no new fragment and names the same ids.

    Asserted on the filesystem as well as on the response, because a handler
    that re-ingested and reported the new ids would satisfy a response-only
    check — which is the defect, not the contract. The mechanism is the
    deterministic unpack directory: ledger-free ids hash the source path, so a
    randomised extraction directory would mint a fresh id per turn per upload
    and grow the corpus without bound.

    Args:
        tmp_path: pytest's per-test directory.
    """
    vault = _vault(tmp_path)

    first = _upload(vault, chatgpt_archive())
    after_first = _fragments(vault)
    second = _upload(vault, chatgpt_archive())

    assert first["status"] == "ok"
    assert second["status"] == "ok"
    assert second["affected_fragment_ids"] == first["affected_fragment_ids"]
    assert _fragments(vault) == after_first
    assert second["action"] == "unchanged"
    assert first["action"] == "created"


def test_a_partly_unreadable_export_reports_the_shortfall(tmp_path: Path) -> None:
    """900 of 1000 landing is reported, not swallowed into a plain ``ok``.

    The archive carries one good conversations file and one whose ``mapping``
    is a string rather than a graph, so the ingestor's parse raises and the
    pipeline collects it. The response must still be a success — the good
    fragments are real — while saying in counts that a document was lost.

    The advisory is asserted to carry the tallies and **not** the pipeline's
    own error strings, which interpolate the failing file's path.

    Args:
        tmp_path: pytest's per-test directory.
    """
    vault = _vault(tmp_path)
    broken = json.dumps([{"title": "broken", "create_time": 1.0, "mapping": "not"}])
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("conversations.json", json.dumps(chatgpt_conversations()))
        archive.writestr("archived.json", broken)

    result = _upload(vault, buffer.getvalue())

    assert result["status"] == "ok"
    assert result["discovered"] == 2
    assert result["failed_documents"] == 1
    assert result["written"] > 1
    advisory = "\n".join(result["warnings"])
    assert "1 of them could not be read" in advisory
    assert "archived.json" not in advisory
    assert str(vault) not in advisory


def test_an_empty_but_valid_export_is_refused_rather_than_called_ok(
    tmp_path: Path,
) -> None:
    """A recognised export that yields nothing is a refusal, not a silent no-op.

    Args:
        tmp_path: pytest's per-test directory.
    """
    vault = _vault(tmp_path)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("conversations.json", json.dumps([{"mapping": {}}]))

    result = _upload(vault, buffer.getvalue())

    assert result["status"] == "refused"
    assert set(result) == _REFUSAL_KEYS
    assert _fragments(vault) == []


# --------------------------------------------------------------------------- #
# The vault is left holding fragments and nothing else
# --------------------------------------------------------------------------- #


def test_a_successful_archive_upload_leaves_no_extracted_or_staged_bytes(
    tmp_path: Path,
) -> None:
    """Neither the archive nor its unpacked tree survives the call.

    This is the archive path's whole privacy argument. A document upload leaves
    its bytes staged, reachable from each fragment's ``origin_key`` and erasable
    by the RTBF sweep. An unpacked export is a *tree*, and the staging root's
    sweep is documented flat and non-recursive — so rather than widen a sweep
    into a layout it was designed to exclude, this path leaves nothing behind
    to sweep at all. If that ever stops being true, the residue is plaintext no
    purge in the vault can reach.

    Args:
        tmp_path: pytest's per-test directory.
    """
    vault = _vault(tmp_path)

    result = _upload(vault, chatgpt_archive())

    assert result["status"] == "ok"
    assert _unpack_residue(vault) == []
    assert _staged(vault) == []
    assert CHATGPT_KEPT_ANSWER in _bodies(vault)
    # …and the conversation text survives nowhere outside 01-Fragments.
    outside = [
        path
        for path in sorted(vault.rglob("*"))
        if path.is_file()
        and not path.is_relative_to(vault / "01-Fragments")
        and CHATGPT_KEPT_ANSWER.encode() in path.read_bytes()
    ]
    assert outside == []


def test_a_successful_archive_upload_is_audited_with_every_fragment_id(
    tmp_path: Path,
) -> None:
    """The audit entry names all the ids, which is what an RTBF request needs.

    Args:
        tmp_path: pytest's per-test directory.
    """
    vault = _vault(tmp_path)

    result = _upload(vault, chatgpt_archive())

    entries = _audit(vault)
    assert len(entries) == 1
    assert entries[0]["tool"] == TOOL_NAME
    assert entries[0]["affected_fragment_ids"] == result["affected_fragment_ids"]
    assert entries[0]["created_tier"] == PrivacyTier.PERSONAL.value
