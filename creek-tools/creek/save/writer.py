"""Frontmatter + body composer for ``creek save`` (FEAT-009).

Each target produces a note whose frontmatter is shaped like its
corresponding primitive (Thread, Eddy, Praxis) where one exists, with
the canonical ``saved_from`` provenance block layered on top. For
unmodelled targets — paradox / unnamed / draft — the writer composes a
small, explicit frontmatter dict so the operator and the lint pass
still see a consistent shape.

The writer is intentionally *not* funnelled through
:class:`creek.vault.writer.VaultWriter`. VaultWriter is the ingestion
path for fragments and rolled-up primitives; save needs to land notes
at additional vault locations (``10-Liminal/Paradoxes/``,
``10-Liminal/Unnamed/``, ``07-Voice/Drafts/``) that VaultWriter does
not own. Sharing the same atomic-create pattern keeps the on-disk
behaviour consistent: the raw-descriptor create itself is
:func:`creek._fsio.create_exclusive`, shared verbatim with
:mod:`creek.vault.writer`.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import frontmatter

from creek._fsio import create_exclusive
from creek.classify.privacy_filter import pre_save_filter
from creek.models import (
    Authorship,
    Eddy,
    Fragment,
    FragmentSource,
    Praxis,
    PrivacyTier,
    SourcePlatform,
    Thread,
)
from creek.save._slug import slugify_filename
from creek.save.router import SaveTarget, target_directory

if TYPE_CHECKING:
    from pathlib import Path

_MAX_FILENAME_LENGTH = 80
_MAX_COLLISION_RETRIES = 1000


@dataclass(frozen=True)
class SaveRequest:
    """Payload describing one ``creek save`` invocation.

    Attributes:
        target: Destination type chosen by the operator.
        body: Raw markdown body of the answer.
        title: Optional title; falls back to a derived slug.
        tier: Privacy tier this save is operating under.
        provenance: IDs of fragments that contributed to the answer.
        source_kind: ``discord`` / ``claude-session`` / ``manual`` / ``mcp``.
        source_id: Opaque source identifier (conversation ID, etc.).
        saved_by: Operator or MCP client name.
        full_body: When True, allow personal-tier bodies through.
    """

    target: SaveTarget
    body: str
    title: str | None = None
    tier: PrivacyTier = PrivacyTier.OPEN
    provenance: tuple[str, ...] = field(default_factory=tuple)
    source_kind: str = "manual"
    source_id: str | None = None
    saved_by: str = "cli"
    full_body: bool = False


def save_to_vault(request: SaveRequest, *, vault_path: Path) -> Path:
    """Write *request* to the vault and return the resulting markdown path.

    Honours the paradox *routing* rule — paradox always lands in
    ``10-Liminal/Paradoxes/`` — and the privacy-tier filter, including
    the intimate-body redirect to ``10-Liminal/Compost/intimate-stubs/``.

    Routing is the only thing ``--target paradox`` overrides. The tier is
    the caller's, unconditionally. Until #1491 this function substituted
    ``PrivacyTier.OPEN`` for every paradox save on the reasoning that a
    paradox preserves *the fact* of a contradiction rather than the
    contradictory content — but the fact is already carried by the note's
    location, title, tags and ``saved_from`` provenance, so forcing the
    tier bought nothing and filed intimate-derived bodies in the clear.

    Args:
        request: The save payload.
        vault_path: Vault root.

    Returns:
        Absolute path of the written vault note.
    """
    effective_tier = request.tier
    filtered = pre_save_filter(
        request.body,
        tier=effective_tier,
        title=request.title,
        full_body=request.full_body,
    )

    stub_written: Path | None = None
    intimate_pointer: str | None = None
    if filtered.stub_relpath is not None and filtered.stub_body is not None:
        # Write the intimate stub *before* the vault note so that if the
        # second write fails, the body is preserved on disk and only a
        # harmless orphan stub remains. Doing it the other way around
        # would leave the vault note pointing at a stub that never
        # existed — the intimate body would be permanently lost.
        stub_written = _write_intimate_stub(
            vault_path / filtered.stub_relpath,
            filtered.stub_body,
            request,
        )
        intimate_pointer = str(stub_written.relative_to(vault_path))

    metadata = _compose_metadata(
        request,
        effective_tier=effective_tier,
        intimate_pointer=intimate_pointer,
    )
    title = metadata.get("title", "") or "untitled"
    target_dir = target_directory(vault_path, request.target)
    target_dir.mkdir(parents=True, exist_ok=True)
    base_name = _compose_base_name(str(title))

    post = frontmatter.Post(content=filtered.vault_body.rstrip() + "\n", **metadata)
    return _atomic_create(target_dir, base_name, frontmatter.dumps(post))


def _compose_metadata(
    request: SaveRequest,
    *,
    effective_tier: PrivacyTier,
    intimate_pointer: str | None,
) -> dict[str, Any]:
    """Build the frontmatter dict for *request*."""
    metadata = _shape_for_target(request)
    # Most targets are typed by their target name; ``ai-as-user`` shapes a real
    # fragment and supplies its own ``type: fragment`` — don't clobber it.
    metadata.setdefault("type", request.target.value)
    metadata["privacy_tier"] = effective_tier.value
    metadata["saved_from"] = _saved_from_block(
        request,
        intimate_pointer=intimate_pointer,
    )
    return metadata


def _shape_for_target(request: SaveRequest) -> dict[str, Any]:
    """Return target-specific frontmatter scaffolding.

    Thread/Eddy/Praxis use their Pydantic model so the resulting
    frontmatter is round-trippable through the ingestion pipeline.
    Paradox/Unnamed/Draft are unmodelled — we shape them by hand so
    the file is still a clean Obsidian note.
    """
    title = (request.title or "").strip() or _derive_title(request.body)
    if request.target == SaveTarget.THREAD:
        return Thread(title=title).model_dump(mode="json")
    if request.target == SaveTarget.EDDY:
        return Eddy(title=title).model_dump(mode="json")
    if request.target == SaveTarget.PRAXIS:
        return Praxis(title=title).model_dump(mode="json")
    if request.target == SaveTarget.AI_AS_USER:
        return _shape_ai_as_user_fragment(title, request.body)
    return {
        "title": title,
        "tags": [request.target.value],
    }


def _shape_ai_as_user_fragment(title: str, body: str) -> dict[str, Any]:
    """Shape kept AI output as an AI-attributed, voice-neutral fragment.

    The note is a real :class:`~creek.models.Fragment` (round-trippable through
    the vault reader so the Retrieval specialist can cite it) carrying the
    FEAT-041 §7 attribution: ``author=ai`` from the ``ai-as-user`` other-author
    folder, ``voice_weight=0.0`` so it never colours the owner's generated
    voice, and ``representativeness=endorsed`` because the owner kept it on
    purpose.

    The fragment ``id`` appends a short content digest to the title slug so two
    kept outputs that share a title get distinct ids (the filename is already
    de-duplicated by the writer's atomic collision-retry; this keeps the
    in-frontmatter id unique too). Identical content under the same title yields
    the same id — a harmless idempotent re-save.
    """
    slug = slugify_filename(title, max_length=_MAX_FILENAME_LENGTH) or "untitled"
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()[:8]
    return Fragment(
        id=f"{SaveTarget.AI_AS_USER.value}-{slug}-{digest}",
        title=title,
        source=FragmentSource(
            platform=SourcePlatform.CLAUDE,
            author=Authorship.AI,
            author_slug=SaveTarget.AI_AS_USER.value,
        ),
        voice_weight=0.0,
        representativeness="endorsed",
    ).model_dump(mode="json")


def _saved_from_block(
    request: SaveRequest,
    *,
    intimate_pointer: str | None,
) -> dict[str, Any]:
    """Compose the ``saved_from`` provenance block."""
    block: dict[str, Any] = {
        "source_kind": request.source_kind,
        "source_id": request.source_id or "",
        "contributing_fragments": list(request.provenance),
        "saved_at": datetime.now(tz=UTC).isoformat().replace("+00:00", "Z"),
        "saved_by": request.saved_by,
    }
    if intimate_pointer is not None:
        block["intimate_body_pointer"] = intimate_pointer
    return block


def _derive_title(body: str) -> str:
    """Pull the first non-empty line of *body* as a fallback title."""
    for line in body.splitlines():
        stripped = line.strip().lstrip("# ").strip()
        if stripped:
            return stripped[:_MAX_FILENAME_LENGTH]
    return "untitled"


def _compose_base_name(title: str) -> str:
    """Return ``YYYY-MM-DD-<slug>`` for the filename stem."""
    date_str = datetime.now(tz=UTC).strftime("%Y-%m-%d")
    slug = slugify_filename(title, max_length=_MAX_FILENAME_LENGTH) or "untitled"
    return f"{date_str}-{slug}"


def _atomic_create(target_dir: Path, base_name: str, content: str) -> Path:
    """Create ``target_dir/{base_name}.md`` atomically; retry on collision.

    Args:
        target_dir: Directory the note will land in.
        base_name: Filename stem (without ``.md``).
        content: Full note contents to write.

    Returns:
        The path of the created note.

    Raises:
        RuntimeError: If a unique filename cannot be obtained within
            :data:`_MAX_COLLISION_RETRIES` attempts.
        OSError: If the note was created but its contents could not be
            written in full. The partial file is unlinked, so a
            truncated answer is never filed under the operator's title
            (#987).
    """
    encoded = content.encode("utf-8")
    for counter in range(_MAX_COLLISION_RETRIES):
        suffix = "" if counter == 0 else f"-{counter}"
        candidate = target_dir / f"{base_name}{suffix}.md"
        try:
            create_exclusive(candidate, encoded)
        except FileExistsError:
            continue
        return candidate
    msg = (
        f"Could not allocate a unique filename for '{base_name}.md' in "
        f"{target_dir} after {_MAX_COLLISION_RETRIES} attempts"
    )
    raise RuntimeError(msg)


def _write_intimate_stub(
    stub_path: Path,
    body: str,
    request: SaveRequest,
) -> Path:
    """Write the full intimate body atomically to the gitignored compost dir.

    Uses the same ``O_CREAT|O_EXCL`` primitive as :func:`_atomic_create`
    so a concurrent or repeat save cannot clobber an existing stub, and
    so exhaustion raises a :class:`RuntimeError` rather than silently
    overwriting the last candidate. The vault note has not been written
    yet — see :func:`save_to_vault` — so a stub failure leaves no
    dangling pointer behind.

    Returns the actual path the stub landed at (which may include a
    counter suffix if the desired filename was already in use).
    """
    stub_path.parent.mkdir(parents=True, exist_ok=True)
    post = frontmatter.Post(
        content=body.rstrip() + "\n",
        type="intimate-stub",
        title=(request.title or "untitled"),
        privacy_tier=PrivacyTier.INTIMATE.value,
        # Reuse the canonical block so the stub records ``saved_at`` and
        # ``saved_by`` alongside source / provenance — useful for
        # debugging because the stub directory is gitignored and so has
        # no git-side timestamp. ``intimate_body_pointer`` is None here:
        # the stub *is* the body, not a pointer to one.
        saved_from=_saved_from_block(request, intimate_pointer=None),
    )
    return _atomic_create(
        stub_path.parent,
        stub_path.stem,
        frontmatter.dumps(post),
    )
