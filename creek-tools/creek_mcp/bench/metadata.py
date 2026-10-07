"""Pinned run metadata and capacity-only host metadata.

Both models are frozen, refuse unknown fields, and constrain every string to an
enum or a space-free pattern. That is what keeps the report content-free by
construction: a model's license *text*, a hostname, or a filesystem path
cannot be stored in any of these fields even by mistake, because none of them
matches. ``tests/bench/test_report_schema_invariant.py`` walks the generated
JSON schema to hold that line for every string field, present and future.
"""

import contextlib
import os
import platform
import re
import shutil
from pathlib import Path
from typing import Annotated, Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, PositiveInt, model_validator

DIGEST_PATTERN: Final[str] = r"^sha256:[0-9a-f]{64}$"
"""A content digest that pins model weights exactly."""

GIT_SHA_PATTERN: Final[str] = r"^[0-9a-f]{40}$"
"""A full commit sha that pins the harness exactly."""

_MODEL_TAG_PATTERN: Final[str] = r"^[A-Za-z0-9_.:/-]{1,128}$"
_QUANTIZATION_PATTERN: Final[str] = r"^[A-Za-z0-9_]{1,32}$"
_PARAMETER_COUNT_PATTERN: Final[str] = r"^[0-9]+(\.[0-9]+)?[KMBT]?$"
_LICENSE_ID_PATTERN: Final[str] = r"^[A-Za-z0-9.+-]{1,64}$"
_VERSION_PATTERN: Final[str] = r"^[0-9]+\.[0-9]+\.[0-9]+$"
_ARCH_PATTERN: Final[str] = r"^[A-Za-z0-9_.-]{1,32}$"
_KERNEL_PATTERN: Final[str] = r"^[A-Za-z0-9_.+~-]{1,128}$"

UNKNOWN: Final[str] = "unknown"
"""The value recorded when a fact cannot be established."""

CpuKind = Literal["shared", "performance", "unknown"]
"""The Fly CPU class; not detectable from inside the guest."""

_LIVE_NEEDS_DIGEST: Final[str] = "digest is required for a live run"
_LIVE_NEEDS_SHA: Final[str] = "git_sha is required for a live run"
_SCOPE_MISMATCH: Final[str] = "endpoint_scope must be fake exactly when mode is fake"


class RunMetadata(BaseModel):
    """What was measured, pinned well enough to reproduce it.

    Attributes:
        mode: ``fake`` (hermetic, deterministic provider) or ``live``.
        provider: The provider that served the trials.
        endpoint_scope: Where live requests went, judged from the resolved
            endpoint addresses (``remote`` only with the operator's explicit
            ``--allow-remote-host``); ``fake`` for the hermetic mode.
        grounding: ``default`` drives reflect's production grounder; ``none``
            injects an empty grounder (the hermetic fake mode, which must not
            load an embedding model).
        model_tag: The model name the provider was asked for.
        digest: The weights' content digest; required live.
        quantization: Quantization level the runtime reported.
        parameter_count: Parameter size the runtime reported (``7.2B``).
        license_id: A short license identifier from a fixed table, never the
            license text.
        num_ctx: Context window pinned on every request.
        num_predict: Ceiling on output tokens sent with every request.
        harness_version: :data:`creek_mcp.bench.HARNESS_VERSION`.
        git_sha: Commit the harness ran from; required live.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: Literal["fake", "live"]
    provider: Literal["fake", "ollama"]
    endpoint_scope: Literal["fake", "loopback", "private", "remote"] = "fake"
    grounding: Literal["none", "default"]
    model_tag: Annotated[str, Field(pattern=_MODEL_TAG_PATTERN)]
    digest: Annotated[str, Field(pattern=DIGEST_PATTERN)] | None = None
    quantization: Annotated[str, Field(pattern=_QUANTIZATION_PATTERN)] | None = None
    parameter_count: Annotated[str, Field(pattern=_PARAMETER_COUNT_PATTERN)] | None = (
        None
    )
    license_id: Annotated[str, Field(pattern=_LICENSE_ID_PATTERN)] = UNKNOWN
    num_ctx: PositiveInt
    num_predict: PositiveInt
    harness_version: Annotated[str, Field(pattern=_VERSION_PATTERN)]
    git_sha: Annotated[str, Field(pattern=GIT_SHA_PATTERN)] | None = None

    @model_validator(mode="after")
    def _live_runs_are_pinned(self) -> Self:
        """Refuse a live run that could not be reproduced from its report."""
        if self.mode == "live" and self.digest is None:
            raise ValueError(_LIVE_NEEDS_DIGEST)
        if self.mode == "live" and self.git_sha is None:
            raise ValueError(_LIVE_NEEDS_SHA)
        if (self.mode == "fake") != (self.endpoint_scope == "fake"):
            raise ValueError(_SCOPE_MISMATCH)
        return self


class HostMetadata(BaseModel):
    """The capacity of the machine the *harness* ran on — and nothing else.

    These facts are read from the harness process. They describe the model's
    host only when the model ran there too (the fake mode, or a loopback
    endpoint); ``describes_model_host`` says which. For a private or remote
    endpoint they describe the client machine, not the one serving the model.

    No hostname, user, path or address: the report may be shared as evidence,
    and none of those is a capacity fact.

    Attributes:
        scope: Always ``harness``: where these facts were read.
        describes_model_host: Whether the model ran on this same host.
        disk_scope: ``model_store`` when the free-disk figure is the
            operator-named model store, ``harness_temp`` when it is the
            harness's temp directory.
        cpu_count: Logical CPUs visible to the process.
        cpu_arch: Machine architecture (``x86_64``, ``arm64``).
        cpu_kind: Fly CPU class as stated by the operator.
        kernel_release: Kernel release string.
        ram_bytes: Physical memory, ``None`` when unknowable.
        disk_free_bytes: Free bytes on the measured filesystem.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    scope: Literal["harness"] = "harness"
    describes_model_host: bool
    disk_scope: Literal["harness_temp", "model_store"]
    cpu_count: PositiveInt | None
    cpu_arch: Annotated[str, Field(pattern=_ARCH_PATTERN)]
    cpu_kind: CpuKind
    kernel_release: Annotated[str, Field(pattern=_KERNEL_PATTERN)]
    ram_bytes: PositiveInt | None
    disk_free_bytes: PositiveInt | None


def _closed(value: str, pattern: str) -> str:
    """Return *value* when it matches *pattern*, else :data:`UNKNOWN`."""
    return value if re.fullmatch(pattern, value) else UNKNOWN


def _ram_bytes() -> int | None:
    """Return physical memory in bytes, or ``None`` where sysconf cannot say."""
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (ValueError, OSError, AttributeError):
        return None


def _disk_free_bytes(path: Path) -> int | None:
    """Return free bytes on *path*'s filesystem, or ``None`` when unreadable."""
    with contextlib.suppress(OSError):
        return shutil.disk_usage(path).free
    return None


def capture_host(
    disk_path: Path,
    *,
    cpu_kind: CpuKind = "unknown",
    model_store: bool = False,
    describes_model_host: bool = False,
) -> HostMetadata:
    """Capture the capacity facts of the host the harness runs on.

    Args:
        disk_path: The directory whose filesystem's free space is reported.
        cpu_kind: The Fly CPU class, which only the operator knows.
        model_store: Whether *disk_path* is the operator-named model store
            (otherwise it is the harness's temp directory).
        describes_model_host: Whether the model ran on this host.

    Returns:
        Host metadata with unknowable values as ``None`` or ``unknown``.
    """
    return HostMetadata(
        describes_model_host=describes_model_host,
        disk_scope="model_store" if model_store else "harness_temp",
        cpu_count=os.cpu_count(),
        cpu_arch=_closed(platform.machine(), _ARCH_PATTERN),
        cpu_kind=cpu_kind,
        kernel_release=_closed(platform.release(), _KERNEL_PATTERN),
        ram_bytes=_ram_bytes(),
        disk_free_bytes=_disk_free_bytes(disk_path),
    )
