"""``source.original_file`` must not carry the operator's host path (#1575).

Every ingest wrote ``str(raw.path)`` verbatim into fragment frontmatter, so a
fragment recorded ``/Users/<name>/Documents/Therapy/2024/session-notes.md`` —
the operator's account name, their directory chain, and by extension the
subject matter of a document the vault may hold at any tier. Frontmatter is
not a private channel: ``creek.generate.indexes`` renders ``source.platform,
source.original_file, ingested`` into a generated vault index note, and a
remote MCP caller reads fragments through the tier ceiling.

**The call this module encodes, and its blast radius.**

*The key is kept, not deleted.* It has real consumers — the INC-008
right-to-be-forgotten purge, the #1329 ``--pin-source-ids`` migration, and
``--refresh-dates`` — and ``origin_key`` is not a fallback for them:
:data:`creek.ingest.pipeline.LEDGERED_SOURCES` is ``{markdown, generic,
document}``, so eight of the eleven registered ingestors write ``origin_key:
null`` and ``original_file`` is the only provenance those fragments carry.

*The value becomes the key* :func:`creek.ingest.pipeline.derive_source_key`
*already mints* — vault-relative for a source under the vault root,
``external/<sha256[:16]>/<basename>`` for one outside it. That is the #953
scheme, already in every fragment's ``origin_key`` for ledgered sources, so it
introduces no new class of disclosure. It also fixes the whole
``creek.upload`` / ``POST /v1/uploads`` surface for free, because upload
staging already lives at ``<vault>/00-Creek-Meta/adepthood/uploads/``.

*Rewriting happens at the assemble-and-write boundary*, not in the eleven
``parse`` implementations: ``Ingestor.parse`` never receives ``vault_path``
and so cannot answer "is this inside the vault". The ingestors keep recording
the real path on the in-memory :class:`~creek.ingest.base.ParsedFragment`,
which is what ``legacy_source_key`` and the ledger's adoption proof consume —
so #1577's tombing behaviour is preserved *by construction* rather than by
coincidence.

That boundary is :func:`creek.ingest.base.assemble_ingested_fragment`, and it
has **three** callers, not one. ``run_ingest`` is the loudest of them, but
``creek process`` assembles in :meth:`creek.pipeline.Pipeline._run_ingestion`
and Discord assembles in :func:`creek.ingest.discord.write_fragments`, and
both write straight to the vault. A fix wired only into ``run_ingest`` leaves
``creek process --source ~/Documents/...`` writing the full host path — the
identical disclosure, on a first-class documented command. Discord is the one
exempt caller, and exempt for a reason that is itself asserted below rather
than assumed: it records no ``source.original_file`` at all, so it has no host
path to redact and stamping one in would invent a grouping key the voice
pipeline reads. The population is enumerated from the source, not sampled.

*No vault migration ships here, and that is a deliberate call.*
``PurgeEngine.purge_source_path`` defaults to ``match="exact"``, so a rewrite
of existing frontmatter without a matching change to purge would silently
match zero fragments on a legal-compliance surface. Teaching purge to accept
either spelling — done here — removes the *need* for a migration: an
operator's existing absolute-path RTBF command keeps working, and a vault
holding both spellings at once is served correctly. Rewriting the historical
frontmatter is a separate, riskier job (ids must be pinned through the ledger
first or every fragment is re-minted and orphaned) and is tracked on its own
issue.

*The cost, stated:* ``external/<hash>/<basename>`` is not resolvable back to
disk, so ``--refresh-dates`` can no longer reopen an out-of-vault source. It
degrades through ``RefreshDatesResult.missing_source`` rather than crashing,
and the in-vault case *improves* — that path was reading a recorded relative
string as a bare ``Path`` and missing it too.
"""

from __future__ import annotations

import base64
import re
from typing import TYPE_CHECKING, Final

import frontmatter
import pytest
from typer.testing import CliRunner

from creek.cli import app
from creek.ingest import INGESTOR_REGISTRY
from creek.ingest.pipeline import derive_source_key, run_ingest
from creek.purge.engine import PurgeEngine
from creek_mcp.tier_ceiling import TierCeiling
from creek_mcp.tools.upload import upload_tool

if TYPE_CHECKING:
    from pathlib import Path

runner = CliRunner()

_HOST_PATH_SHAPES: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"^/"),
    re.compile(r"^[A-Za-z]:\\\\"),
    re.compile(r"^[A-Za-z]:/"),
    re.compile(r"/Users/[^/]+"),
    re.compile(r"/home/[^/]+"),
    re.compile(r"^~"),
)
"""Shapes a recorded source string must never take.

Asserted as *patterns* rather than against one fixture path on purpose: a fix
that special-cased the exact string a test happened to seed would satisfy an
equality assertion and leave every other operator's path in the clear.
"""

_BODY: Final[bytes] = b"# Ridge notes\n\nThe fog lifted at seven, and stayed up.\n"
"""Synthetic markdown. Nothing here is a real person or a real journal entry."""

_EXTERNAL_KEY_RE: Final[re.Pattern[str]] = re.compile(
    r"^external/[0-9a-f]{16}/[^/]+$",
)
"""The #953 out-of-vault key spelling: namespace, digest, bare filename."""


def _scaffold_vault(tmp_path: Path) -> Path:
    """Create the minimal vault tree the writer and the ledger need."""
    vault = tmp_path / "vault"
    for relative in (
        "00-Creek-Meta/Processing-Log",
        "00-Creek-Meta/State/ingest",
        "01-Fragments/Notes",
        "10-Liminal/Orphaned",
    ):
        (vault / relative).mkdir(parents=True, exist_ok=True)
    return vault


def _recorded_sources(vault: Path) -> list[str]:
    """Read ``source.original_file`` off every fragment the vault holds."""
    recorded: list[str] = []
    for path in sorted((vault / "01-Fragments").rglob("*.md")):
        source = frontmatter.load(str(path)).metadata.get("source")
        assert isinstance(source, dict), path
        value = source.get("original_file")
        if isinstance(value, str):
            recorded.append(value)
    return recorded


def _assert_discloses_no_host_path(recorded: list[str]) -> None:
    """Fail if any recorded source string takes a host-path shape."""
    assert recorded, "nothing was seeded, so nothing was proven"
    for value in recorded:
        for shape in _HOST_PATH_SHAPES:
            assert not shape.search(value), f"{value!r} matches {shape.pattern!r}"


def _ingest_out_of_vault_note(tmp_path: Path) -> tuple[Path, Path]:
    """Seed one fragment from a source outside the vault, via ``run_ingest``."""
    vault = _scaffold_vault(tmp_path)
    source_dir = tmp_path / "Documents" / "Ridge"
    source_dir.mkdir(parents=True)
    note = source_dir / "ridge-notes.md"
    note.write_bytes(_BODY)
    run_ingest(
        ingestor_cls=INGESTOR_REGISTRY["markdown"],
        source_type="markdown",
        input_path=source_dir,
        vault_path=vault,
    )
    return vault, note


# ---- The three seeding paths (AC#3) --------------------------------------


def test_the_cli_records_no_host_path_for_an_out_of_vault_source(
    tmp_path: Path,
) -> None:
    """``creek ingest`` must not write the operator's directory chain."""
    vault = _scaffold_vault(tmp_path)
    source_dir = tmp_path / "Documents" / "Ridge"
    source_dir.mkdir(parents=True)
    (source_dir / "ridge-notes.md").write_bytes(_BODY)

    result = runner.invoke(
        app,
        [
            "ingest",
            "--type",
            "markdown",
            "--input",
            str(source_dir),
            "--vault",
            str(vault),
            "-y",
        ],
    )

    assert result.exit_code == 0, result.output
    _assert_discloses_no_host_path(_recorded_sources(vault))


def test_the_mcp_upload_tool_records_no_host_path(tmp_path: Path) -> None:
    """``creek.upload`` stages inside the vault; the record must say so."""
    vault = _scaffold_vault(tmp_path)
    (vault / "00-Creek-Meta" / "audit").mkdir(parents=True, exist_ok=True)

    out = upload_tool(
        vault_path=vault,
        filename="session-notes.md",
        content_base64=base64.b64encode(_BODY).decode("ascii"),
        external_id="adepthood:doc:1575",
        tier="open",
        privacy_tier_ceiling=TierCeiling.OPEN,
    )

    assert out.get("error") is None, out
    _assert_discloses_no_host_path(_recorded_sources(vault))


# ---- The spelling itself --------------------------------------------------


def test_an_in_vault_source_is_recorded_relative_to_the_vault(
    tmp_path: Path,
) -> None:
    """A source under the vault root records its vault-relative path.

    Lossless: :func:`creek.ingest.pipeline.resolve_recorded_source` anchors a
    relative record to the vault, so every existing reader keeps resolving it.
    """
    vault = _scaffold_vault(tmp_path)
    inbox = vault / "00-Inbox"
    inbox.mkdir()
    (inbox / "ridge-notes.md").write_bytes(_BODY)

    run_ingest(
        ingestor_cls=INGESTOR_REGISTRY["markdown"],
        source_type="markdown",
        input_path=inbox,
        vault_path=vault,
    )

    assert _recorded_sources(vault) == ["00-Inbox/ridge-notes.md"]


def test_an_out_of_vault_source_is_recorded_under_the_external_namespace(
    tmp_path: Path,
) -> None:
    """A source outside the vault records the #953 hashed-parent key.

    The basename survives — the same disclosure the vault already accepts in
    ``origin_key`` — while the directory chain and the account name do not.
    """
    vault, note = _ingest_out_of_vault_note(tmp_path)

    recorded = _recorded_sources(vault)

    assert len(recorded) == 1, recorded
    assert _EXTERNAL_KEY_RE.match(recorded[0]), recorded[0]
    assert recorded[0].endswith("/ridge-notes.md")
    assert note.parent.name not in recorded[0]


# ---- Consumer preservation (AC#4) ----------------------------------------


def test_derive_source_key_is_idempotent_on_a_key_it_already_minted(
    tmp_path: Path,
) -> None:
    """Re-deriving a stored key must return that key, not a key of a key.

    ``pin_ids`` and ``adopt_legacy_ledger_key`` both feed the *recorded*
    string back through :func:`derive_source_key`. Without idempotency the
    second derivation resolves ``external/<hash>/name`` against the current
    directory and mints a different key, so the migration writes a ledger
    record the next ingest never looks up.
    """
    vault = _scaffold_vault(tmp_path)
    outside = tmp_path / "Documents" / "Ridge" / "ridge-notes.md"
    outside.parent.mkdir(parents=True)
    outside.write_bytes(_BODY)

    once = derive_source_key(str(outside), vault)
    twice = derive_source_key(once, vault)

    assert _EXTERNAL_KEY_RE.match(once), once
    assert twice == once


def test_pin_source_ids_keys_the_record_the_ingest_actually_wrote(
    tmp_path: Path,
) -> None:
    """``--pin-source-ids`` must pin under the same key ``run_ingest`` used.

    The migration exists to stop an id from moving. Pinning under a key the
    ingest never uses would leave the ledger looking migrated while the next
    run misses, mints a fresh id, and orphans the fragment.
    """
    from creek.ingest.ledger import SourceLedger
    from creek.ingest.pin_ids import pin_source_ids

    vault, _note = _ingest_out_of_vault_note(tmp_path)
    before = SourceLedger.load(vault, source="markdown")
    keys_from_ingest = set(before.live_keys())
    assert keys_from_ingest, "the ingest recorded no ledger key to compare against"

    pin_source_ids(vault_path=vault)

    after = SourceLedger.load(vault, source="markdown")
    assert set(after.live_keys()) == keys_from_ingest


def test_purge_by_source_path_still_finds_a_fragment_by_its_real_path(
    tmp_path: Path,
) -> None:
    """An operator's RTBF command names a real path and must still match.

    INC-008's default is ``match="exact"``. The operator types the path they
    know — the absolute one on their disk — and the vault now stores a
    derived spelling, so the engine has to reconcile the two or a
    right-to-be-forgotten request silently deletes nothing and reports
    success.
    """
    vault, note = _ingest_out_of_vault_note(tmp_path)
    engine = PurgeEngine(vault_path=vault, dry_run=True)

    assert engine.count_fragments_from_source_path(str(note)) == 1


def test_purge_by_source_path_still_matches_a_legacy_absolute_record(
    tmp_path: Path,
) -> None:
    """A vault holding the *old* spelling keeps matching too.

    No migration ships with this change, so both spellings coexist in a real
    vault. Reconciling one direction only would break whichever half the
    operator happened to have.
    """
    vault, note = _ingest_out_of_vault_note(tmp_path)
    fragment = next((vault / "01-Fragments").rglob("*.md"))
    post = frontmatter.load(str(fragment))
    source = post.metadata["source"]
    assert isinstance(source, dict)
    source["original_file"] = str(note)
    fragment.write_text(frontmatter.dumps(post), encoding="utf-8")
    engine = PurgeEngine(vault_path=vault, dry_run=True)

    assert engine.count_fragments_from_source_path(str(note)) == 1


def test_refresh_dates_resolves_an_in_vault_source(tmp_path: Path) -> None:
    """The in-vault half of ``--refresh-dates`` keeps working, and improves.

    ``refresh`` read the recorded string as a bare ``Path``, so a
    vault-relative record only resolved when the process happened to be run
    from the vault root. Anchoring it the way every other reader does is what
    makes the vault-relative spelling safe to store.
    """
    from creek.ingest.refresh import refresh_authored_dates

    vault = _scaffold_vault(tmp_path)
    inbox = vault / "00-Inbox"
    inbox.mkdir()
    note = inbox / "ridge-notes.md"
    note.write_bytes(_BODY)
    run_ingest(
        ingestor_cls=INGESTOR_REGISTRY["markdown"],
        source_type="markdown",
        input_path=inbox,
        vault_path=vault,
    )
    note.write_text(
        "---\npublished: 2024-03-04\n---\n\n# Ridge notes\n\nThe fog lifted.\n",
        encoding="utf-8",
    )

    result = refresh_authored_dates(vault_path=vault)

    assert result.missing_source == 0, result
    assert result.updated == 1, result


def test_refresh_dates_reports_an_out_of_vault_source_as_missing(
    tmp_path: Path,
) -> None:
    """The stated cost, pinned rather than left to be discovered.

    ``external/<hash>/<basename>`` cannot be resolved back to disk, so the
    backfill can no longer reopen an out-of-vault source. It must *report*
    that through the counter built for it, never crash and never claim it
    updated something.
    """
    from creek.ingest.refresh import refresh_authored_dates

    vault, _note = _ingest_out_of_vault_note(tmp_path)

    result = refresh_authored_dates(vault_path=vault)

    assert result.errors == []
    assert result.updated == 0
    assert result.missing_source == 1, result


@pytest.mark.parametrize("shape", _HOST_PATH_SHAPES, ids=lambda p: p.pattern)
def test_the_host_path_shapes_are_a_real_filter(shape: re.Pattern[str]) -> None:
    """Each shape must reject something, so the sweep above cannot be empty.

    A parametrised guard that matched nothing would let
    :func:`_assert_discloses_no_host_path` pass over any string at all.
    """
    offenders = [
        "/Users/someone/Documents/notes.md",
        "C:\\\\Users\\\\someone\\\\notes.md",
        "C:/Users/someone/notes.md",
        "/home/someone/notes.md",
        "~/Documents/notes.md",
    ]
    assert any(shape.search(candidate) for candidate in offenders)


# ---- The writers the one-chokepoint story missed --------------------------


def test_creek_process_records_no_host_path(tmp_path: Path) -> None:
    """``creek process`` must not write the operator's directory chain either.

    ``creek process`` does not call ``run_ingest``: it assembles in
    :meth:`creek.pipeline.Pipeline._run_ingestion` and writes from
    ``_write_to_vault``. A #1575 fix wired only into
    :func:`creek.ingest.pipeline.establish_source_identity` therefore left
    this surface — a documented, first-class command — recording
    ``/Users/<name>/Documents/...`` verbatim, which is the same disclosure
    the issue exists to close.
    """
    from creek.config import CreekConfig
    from creek.pipeline import Pipeline

    vault = _scaffold_vault(tmp_path)
    source_dir = tmp_path / "Documents" / "Ridge"
    source_dir.mkdir(parents=True)
    (source_dir / "ridge-notes.md").write_bytes(_BODY)

    Pipeline(config=CreekConfig(vault_path=vault), no_llm=True).run(source_dir, vault)

    recorded = _recorded_sources(vault)
    _assert_discloses_no_host_path(recorded)
    assert all(_EXTERNAL_KEY_RE.match(value) for value in recorded), recorded


def test_the_discord_path_records_no_source_path_at_all(tmp_path: Path) -> None:
    """Discord is exempt from the rewrite because it stores nothing to rewrite.

    :func:`creek.ingest.discord.write_fragments` is the third caller of
    ``assemble_ingested_fragment`` and, like ``creek process``, never touches
    ``run_ingest`` — so the enumeration below has to say why it needs no
    ``record_source_provenance`` call. The reason is this: ``DiscordIngestor``
    emits no ``source.original_file``, and ``run_discord_data_package`` reads
    from wherever the operator saved the package, so if it *did* emit one the
    absolute download path would be in the frontmatter of every captured
    conversation.

    Stamping the derived key in anyway is not free either: it would give every
    fragment from one channel file the same non-null ``original_file``, which
    :func:`creek.generate.voice_authenticity._conversation_key` reads as a
    sibling-pairing signal for AI-corpus quarantine. This test is the tripwire
    on both halves — it fails the day Discord starts recording a source path,
    which is the day the exemption stops being true.
    """
    import json

    from creek.ingest.discord import run_discord_data_package

    vault = _scaffold_vault(tmp_path)
    (vault / "01-Fragments" / "Messages").mkdir(parents=True, exist_ok=True)
    channel = tmp_path / "Downloads" / "discord-export" / "messages" / "general"
    channel.mkdir(parents=True)
    (channel / "channel.json").write_text(
        json.dumps({"id": "general", "name": "general"}), encoding="utf-8"
    )
    (channel / "messages.json").write_text(
        json.dumps(
            [
                {
                    "id": "1",
                    "timestamp": "2026-06-26T10:00:00+00:00",
                    "content": "a reflection long enough to be worth keeping",
                    "author": {"name": "Ada"},
                }
            ]
        ),
        encoding="utf-8",
    )

    written = run_discord_data_package(vault, channel.parent.parent)

    assert written == 1
    fragments = sorted((vault / "01-Fragments").rglob("*.md"))
    assert len(fragments) == 1, fragments
    source = frontmatter.load(str(fragments[0])).metadata["source"]
    assert isinstance(source, dict)
    assert source.get("original_file") is None, source
    # Nothing else in the source mapping may carry the download path either.
    for value in source.values():
        if isinstance(value, str):
            for shape in _HOST_PATH_SHAPES:
                assert not shape.search(value), f"{value!r} matches {shape.pattern!r}"


def test_every_assemble_and_write_path_records_provenance() -> None:
    """The population of writers is enumerated, not sampled (#1575).

    :func:`creek.ingest.base.assemble_ingested_fragment` is the only function
    that turns a ``ParsedFragment`` into a writable ``Fragment``, so its
    callers *are* the population that can persist a host path. This test fails
    when a fourth appears — which is the failure mode that produced it: the
    first pass wired ``run_ingest`` and called the job done while
    ``creek process`` kept writing absolute paths.

    Asserted by parsing the source rather than by importing, because a caller
    that leaks is a caller that *exists*, whether or not any test happens to
    exercise it. The Discord exemption is named here and justified by
    ``test_the_discord_path_records_no_source_path_at_all`` rather than
    tolerated silently.
    """
    import ast
    from pathlib import Path as RuntimePath

    import creek

    recording = {"pipeline.py", "ingest/pipeline.py"}
    exempt = {"ingest/discord.py"}

    package_root = RuntimePath(str(creek.__file__)).parent
    callers: set[str] = set()
    for module_path in sorted(package_root.rglob("*.py")):
        tree = ast.parse(module_path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "assemble_ingested_fragment"
            ):
                callers.add(module_path.relative_to(package_root).as_posix())

    assert callers == recording | exempt
    for relative in sorted(recording):
        source = (package_root / relative).read_text(encoding="utf-8")
        assert "record_source_provenance(" in source, relative
