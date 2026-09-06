"""Durable AI-attributed Voice Draft storage keyed by external id (#1727).

Voice Drafts are not uploads and are not owner-authored journal entries.  They
live in the reserved ``11-Other-Authors/ai-as-user`` subtree as real fragments,
which keeps them available to retrieval while the fixed zero voice weight and
AI authorship keep them out of the owner's voice corpus.

The caller-owned id is hashed into both the filename and fragment id.  Titles
and raw ids therefore never leak through directory listings, while the full id
remains inside tier-gated frontmatter so a hash collision or hand-edited note
cannot be mistaken for the requested resource.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Final

import frontmatter

from creek._fsio import atomic_write_text
from creek._fslock import vault_lock
from creek.models import (
    Authorship,
    Fragment,
    FragmentSource,
    PrivacyTier,
    SourcePlatform,
)
from creek.save.router import SaveTarget, target_directory
from creek.vault.reader import FRONTMATTER_LOAD_ERRORS, try_load_fragment

if TYPE_CHECKING:
    from pathlib import Path

_VOICE_DRAFT_KEY: Final[str] = "voice_draft"
_LOCK_RELPATH: Final[tuple[str, ...]] = (
    "00-Creek-Meta",
    "locks",
    "voice-drafts.lock",
)
_DEFAULT_TITLE: Final[str] = "Voice draft"
_SOURCE_KIND: Final[str] = "adepthood-voice-draft"
_SAVED_BY: Final[str] = "adepthood-v1"

ReadAdmission = Callable[[PrivacyTier], bool]
"""Predicate deciding whether a caller may touch an existing draft tier."""


class VoiceDraftAction(StrEnum):
    """The three outcomes of an idempotent Voice Draft upsert."""

    CREATED = "created"
    UPDATED = "updated"
    UNCHANGED = "unchanged"


class VoiceDraftAccessDeniedError(PermissionError):
    """The addressed draft exists above the caller's admitted ceiling."""


class VoiceDraftStorageError(RuntimeError):
    """The deterministic draft slot exists but cannot be trusted as a draft."""


class _VoiceDraftFailure(StrEnum):
    """Content-free reasons a deterministic draft slot is untrustworthy."""

    SYMLINK = "voice draft path is a symlink"
    UNREADABLE = "voice draft is unreadable"
    INVALID_FRAGMENT = "voice draft is not a valid fragment"
    IDENTITY_MISMATCH = "voice draft identity does not match its slot"
    WRONG_AUTHOR = "voice draft does not carry AI authorship"
    WRONG_NAMESPACE = "voice draft is outside the AI author namespace"
    REDIRECTED_NAMESPACE = "voice draft namespace is redirected"
    NONZERO_VOICE = "voice draft has a non-zero voice weight"
    DISAPPEARED = "voice draft disappeared after its write"


@dataclass(frozen=True, slots=True)
class VoiceDraftRecord:
    """One validated Voice Draft loaded from its deterministic vault path."""

    external_id: str
    fragment_id: str
    title: str
    content: str
    tier: PrivacyTier
    path: Path


@dataclass(frozen=True, slots=True)
class VoiceDraftDocument:
    """The mutable document fields supplied for one Voice Draft upsert."""

    content: str
    title: str | None
    tier: PrivacyTier


@dataclass(frozen=True, slots=True)
class VoiceDraftWriteResult:
    """A stored record paired with its idempotent write outcome."""

    action: VoiceDraftAction
    record: VoiceDraftRecord


def _identity_digest(external_id: str) -> str:
    """Return the full hexadecimal digest used for the private disk key."""
    return hashlib.sha256(external_id.encode("utf-8")).hexdigest()


def _draft_path(vault_path: Path, external_id: str) -> Path:
    """Return the deterministic, content-free path for *external_id*."""
    digest = _identity_digest(external_id)
    return _draft_namespace(vault_path) / f"voice-draft-{digest}.md"


def _draft_namespace(vault_path: Path) -> Path:
    """Return the canonical namespace, refusing a redirected parent path."""
    canonical_vault = vault_path.resolve(strict=False)
    expected = target_directory(canonical_vault, SaveTarget.AI_AS_USER)
    addressed = target_directory(vault_path, SaveTarget.AI_AS_USER)
    try:
        resolved = addressed.resolve(strict=False)
    except OSError as error:
        raise VoiceDraftStorageError(_VoiceDraftFailure.REDIRECTED_NAMESPACE) from error
    if resolved != expected:
        raise VoiceDraftStorageError(_VoiceDraftFailure.REDIRECTED_NAMESPACE)
    return expected


def _lock_path(vault_path: Path) -> Path:
    """Return the one cross-process lock shared by all Voice Draft mutations."""
    return vault_path.joinpath(*_LOCK_RELPATH)


def _normalise_content(content: str) -> str:
    """Return the body shape frontmatter round-trips from a Markdown note."""
    return content.rstrip()


def _voice_draft_metadata(external_id: str) -> dict[str, str]:
    """Return the private identity block stored inside the tiered note."""
    return {"external_id": external_id}


def _render_record(
    *,
    external_id: str,
    content: str,
    title: str,
    tier: PrivacyTier,
) -> str:
    """Render one AI-attributed fragment without deriving public text from body."""
    digest = _identity_digest(external_id)
    fragment = Fragment(
        id=f"voice-draft-{digest[:24]}",
        title=title,
        source=FragmentSource(
            platform=SourcePlatform.OTHER,
            author=Authorship.AI,
            author_slug=SaveTarget.AI_AS_USER.value,
        ),
        voice_weight=0.0,
        representativeness="endorsed",
        privacy_tier=tier,
    )
    metadata = fragment.model_dump(mode="json") | {
        _VOICE_DRAFT_KEY: _voice_draft_metadata(external_id),
        "saved_from": {
            "source_kind": _SOURCE_KIND,
            "source_id": external_id,
            "contributing_fragments": [],
            "saved_at": datetime.now(tz=UTC).isoformat().replace("+00:00", "Z"),
            "saved_by": _SAVED_BY,
        },
    }
    post = frontmatter.Post(content=f"{_normalise_content(content)}\n", **metadata)
    return frontmatter.dumps(post)


def _stored_external_id(raw: Mapping[str, object]) -> str | None:
    """Return the typed private id from raw metadata, or ``None`` if malformed."""
    identity = raw.get(_VOICE_DRAFT_KEY)
    if not isinstance(identity, Mapping):
        return None
    value = identity.get("external_id")
    return str(value) if isinstance(value, str) else None


def _load_path(path: Path, external_id: str) -> VoiceDraftRecord | None:
    """Load and validate the deterministic slot without applying policy."""
    if path.is_symlink():
        raise VoiceDraftStorageError(_VoiceDraftFailure.SYMLINK)
    if not path.exists():
        return None
    try:
        loaded = try_load_fragment(path)
    except FRONTMATTER_LOAD_ERRORS as error:
        raise VoiceDraftStorageError(_VoiceDraftFailure.UNREADABLE) from error
    if loaded is None:
        raise VoiceDraftStorageError(_VoiceDraftFailure.INVALID_FRAGMENT)
    fragment, content, raw = loaded
    if _stored_external_id(raw) != external_id:
        raise VoiceDraftStorageError(_VoiceDraftFailure.IDENTITY_MISMATCH)
    if str(fragment.source.author) != Authorship.AI.value:
        raise VoiceDraftStorageError(_VoiceDraftFailure.WRONG_AUTHOR)
    if fragment.source.author_slug != SaveTarget.AI_AS_USER.value:
        raise VoiceDraftStorageError(_VoiceDraftFailure.WRONG_NAMESPACE)
    if fragment.voice_weight != 0.0:
        raise VoiceDraftStorageError(_VoiceDraftFailure.NONZERO_VOICE)
    return VoiceDraftRecord(
        external_id=external_id,
        fragment_id=fragment.id,
        title=fragment.title,
        content=content,
        tier=PrivacyTier(str(fragment.privacy_tier)),
        path=path,
    )


def read_voice_draft(*, vault_path: Path, external_id: str) -> VoiceDraftRecord | None:
    """Return one validated draft, or ``None`` when its slot is absent."""
    return _load_path(_draft_path(vault_path, external_id), external_id)


def upsert_voice_draft(
    *,
    vault_path: Path,
    external_id: str,
    document: VoiceDraftDocument,
    existing_is_admitted: ReadAdmission,
) -> VoiceDraftWriteResult:
    """Atomically create or update one external-id-owned Voice Draft.

    The admission predicate is evaluated while the mutation lock is held, so a
    lower-ceiling caller cannot race an admitted reader and overwrite a draft
    whose existing bytes it could not have read.
    """
    effective_title = (
        document.title.strip() if document.title is not None else _DEFAULT_TITLE
    )
    effective_content = _normalise_content(document.content)
    with vault_lock(_lock_path(vault_path)):
        namespace = _draft_namespace(vault_path)
        namespace.mkdir(parents=True, exist_ok=True)
        path = _draft_path(vault_path, external_id)
        existing = _load_path(path, external_id)
        if existing is not None and not existing_is_admitted(existing.tier):
            raise VoiceDraftAccessDeniedError
        if (
            existing is not None
            and existing.title == effective_title
            and existing.content == effective_content
            and existing.tier == document.tier
        ):
            return VoiceDraftWriteResult(VoiceDraftAction.UNCHANGED, existing)
        rendered = _render_record(
            external_id=external_id,
            content=effective_content,
            title=effective_title,
            tier=document.tier,
        )
        atomic_write_text(path, rendered)
        stored = _load_path(path, external_id)
        if stored is None:
            raise VoiceDraftStorageError(_VoiceDraftFailure.DISAPPEARED)
        action = (
            VoiceDraftAction.CREATED if existing is None else VoiceDraftAction.UPDATED
        )
        return VoiceDraftWriteResult(action, stored)


def delete_voice_draft(
    *,
    vault_path: Path,
    external_id: str,
    existing_is_admitted: ReadAdmission,
) -> VoiceDraftRecord | None:
    """Delete one admitted draft and return its prior record, if present."""
    path = _draft_path(vault_path, external_id)
    with vault_lock(_lock_path(vault_path)):
        existing = _load_path(path, external_id)
        if existing is None:
            return None
        if not existing_is_admitted(existing.tier):
            raise VoiceDraftAccessDeniedError
        path.unlink()
        return existing
