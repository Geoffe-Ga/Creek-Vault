"""Reproducible local-model package: pinned manifest and weights verifier (#1849).

A *model package* is everything needed to reproduce one local model exactly:
the runtime (name, version, digest), the model (pinned tag, weights blob
digest, the digest the runtime's inventory reports for it), its quantization,
parameter count and size, and its licence. The manifest is a JSON file the
operator mounts **outside** the vault and names with
:data:`MODEL_PACKAGE_FILE_ENV`, so a vault-config edit can neither set nor
loosen the pin.

The module is runtime-agnostic: it chooses no model and ships no weights.
:data:`APPROVED_MODEL_LICENSES` starts empty, so every manifest is refused
until the owner approves a licence.

``python -m creek_mcp.model_package verify --weights PATH`` streams the weights
through SHA-256 and prints one content-free status. It is the hook a future
download step calls before it trusts a blob.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import os
import re
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Final, Literal
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PositiveInt,
    ValidationError,
    field_validator,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

MODEL_PACKAGE_FILE_ENV: Final[str] = "CREEK_MODEL_PACKAGE_FILE"
"""Path to the operator-mounted manifest; must live outside the vault."""

APPROVED_MODEL_LICENSES: Final[frozenset[str]] = frozenset()
"""SPDX ids the owner has approved. Empty until that decision is made."""

VERIFY_FAILED_EXIT_CODE: Final[int] = 27
"""``verify`` exit code for any outcome other than ``verified``."""

_CHUNK_BYTES: Final[int] = 1 << 20
_FLOATING_TAG: Final[str] = "latest"
_SHA256_HEX: Final[str] = r"^[0-9a-f]{64}$"
_SPDX_ID: Final[str] = r"^[A-Za-z0-9][A-Za-z0-9.+-]*$"
_MODEL_REFERENCE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][\w.:/-]*$")
_VERSION: Final[re.Pattern[str]] = re.compile(r"^[0-9A-Za-z][\w.+-]*$")

_Sha256 = Annotated[str, Field(pattern=_SHA256_HEX)]
_NonEmpty = Annotated[str, Field(min_length=1)]


class ModelPackageError(ValueError):
    """A manifest that is unreadable, not fully pinned, or not approved."""


class ModelVerification(StrEnum):
    """Closed outcome set of :func:`verify_weights`."""

    VERIFIED = "verified"
    MISSING = "missing"
    SIZE_MISMATCH = "size-mismatch"
    DIGEST_MISMATCH = "digest-mismatch"


class ModelPackageManifest(BaseModel):
    """Every pinned fact of one local model package; nothing is optional.

    ``model_blob_sha256`` is the SHA-256 of the weights file itself.
    ``runtime_inventory_digest`` is what the runtime's inventory reports for
    the model; for Ollama that is the manifest digest from ``/api/tags``, not
    the weights digest. The two are kept separate so neither stands in for
    the other.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    schema_version: Literal[1]
    runtime_name: _NonEmpty
    runtime_version: str
    runtime_digest: _Sha256
    model_name: str
    model_blob_sha256: _Sha256
    runtime_inventory_digest: _Sha256
    quantization: _NonEmpty
    parameter_count: PositiveInt
    size_bytes: PositiveInt
    license_spdx: Annotated[str, Field(pattern=_SPDX_ID)]
    license_url: str

    @field_validator("runtime_version")
    @classmethod
    def _pinned_version(cls, value: str) -> str:
        """Refuse an empty, malformed or floating runtime version."""
        if not _VERSION.fullmatch(value) or value.lower() == _FLOATING_TAG:
            msg = "runtime_version must be an exact, non-floating version"
            raise ValueError(msg)
        return value

    @field_validator("model_name")
    @classmethod
    def _pinned_tag(cls, value: str) -> str:
        """Require an explicit, non-``latest`` tag on the model reference."""
        tail = value.rsplit("/", maxsplit=1)[-1]
        _, separator, tag = tail.partition(":")
        if not _MODEL_REFERENCE.fullmatch(value) or not separator:
            msg = "model_name must carry an explicit tag"
            raise ValueError(msg)
        if not tag or tag.lower() == _FLOATING_TAG:
            msg = "model_name must carry an exact, non-floating tag"
            raise ValueError(msg)
        return value

    @field_validator("license_url")
    @classmethod
    def _https_license(cls, value: str) -> str:
        """Require an HTTPS URL with a host for the licence text."""
        parts = urlsplit(value)
        if parts.scheme != "https" or not parts.hostname:
            msg = "license_url must be an https URL"
            raise ValueError(msg)
        return value


def _invalid_fields(error: ValidationError) -> str:
    """Name the offending fields without echoing any submitted value."""
    names = sorted({str(item["loc"][0]) for item in error.errors() if item["loc"]})
    return ", ".join(names) or "manifest"


def load_manifest(
    path: Path,
    *,
    approved_licenses: frozenset[str] = APPROVED_MODEL_LICENSES,
) -> ModelPackageManifest:
    """Load and validate the manifest at *path*.

    Args:
        path: The operator-mounted JSON manifest.
        approved_licenses: SPDX ids allowed to load.

    Returns:
        The fully pinned, approved manifest.

    Raises:
        ModelPackageError: When the file is unreadable, any field is missing,
            unpinned or malformed, or the licence is not approved. The message
            names fields, never their values.
    """
    try:
        raw = path.read_bytes()
    except OSError as exc:
        msg = "model package manifest is unreadable"
        raise ModelPackageError(msg) from exc
    try:
        manifest = ModelPackageManifest.model_validate_json(raw)
    except ValidationError as exc:
        msg = f"model package manifest is not fully pinned: {_invalid_fields(exc)}"
        raise ModelPackageError(msg) from exc
    if manifest.license_spdx not in approved_licenses:
        msg = "model package license is not on the approved list"
        raise ModelPackageError(msg)
    return manifest


def configured_manifest(
    environ: Mapping[str, str] | None = None,
) -> ModelPackageManifest | None:
    """Return the operator-configured manifest, or ``None`` when unset.

    Args:
        environ: Environment to read; ``os.environ`` when omitted.

    Returns:
        The validated manifest, or ``None`` when :data:`MODEL_PACKAGE_FILE_ENV`
        is unset or empty.

    Raises:
        ModelPackageError: When a manifest is configured but cannot be loaded.
    """
    source = os.environ if environ is None else environ
    raw = source.get(MODEL_PACKAGE_FILE_ENV, "").strip()
    if not raw:
        return None
    return load_manifest(Path(raw), approved_licenses=APPROVED_MODEL_LICENSES)


def _streamed_sha256(path: Path) -> tuple[str, int] | None:
    """Return the SHA-256 hex digest and byte count of *path*, read in chunks."""
    digest = hashlib.sha256()
    counted = 0
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(_CHUNK_BYTES):
                digest.update(chunk)
                counted += len(chunk)
    except OSError:
        return None
    return digest.hexdigest(), counted


def _file_size(path: Path) -> int | None:
    """Return the size of the regular file at *path*, or ``None`` if absent."""
    try:
        return path.stat().st_size if path.is_file() else None
    except OSError:
        return None


def verify_weights(path: Path, manifest: ModelPackageManifest) -> ModelVerification:
    """Verify the weights at *path* against *manifest* without loading them whole.

    The size is checked before hashing, so a truncated download is refused
    cheaply, and again after streaming, so a file that changes mid-read is
    never verified.

    Args:
        path: The weights file.
        manifest: The pinned package it must match.

    Returns:
        :attr:`ModelVerification.VERIFIED` only for an exact match.
    """
    size = _file_size(path)
    if size is None:
        return ModelVerification.MISSING
    if size != manifest.size_bytes:
        return ModelVerification.SIZE_MISMATCH
    streamed = _streamed_sha256(path)
    if streamed is None:
        return ModelVerification.MISSING
    digest, counted = streamed
    if counted != manifest.size_bytes:
        return ModelVerification.SIZE_MISMATCH
    if not hmac.compare_digest(digest, manifest.model_blob_sha256):
        return ModelVerification.DIGEST_MISMATCH
    return ModelVerification.VERIFIED


def _parser() -> argparse.ArgumentParser:
    """Build the ``verify`` command-line parser."""
    parser = argparse.ArgumentParser(prog="creek-model-package")
    commands = parser.add_subparsers(dest="command", required=True)
    verify = commands.add_parser("verify", help="verify weights against the pin")
    verify.add_argument("--weights", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> None:
    """Print one content-free verification status and exit with its code.

    Args:
        argv: Arguments; ``sys.argv[1:]`` when omitted.

    Raises:
        SystemExit: Always; ``0`` only when the weights are verified.
    """
    args = _parser().parse_args(argv)
    try:
        manifest = configured_manifest()
    except ModelPackageError:
        manifest = None
    status = (
        ModelVerification.MISSING
        if manifest is None
        else verify_weights(args.weights, manifest)
    )
    print(status.value)
    raise SystemExit(
        0 if status is ModelVerification.VERIFIED else VERIFY_FAILED_EXIT_CODE
    )


if __name__ == "__main__":  # pragma: no cover - the download step's verify hook.
    main()
