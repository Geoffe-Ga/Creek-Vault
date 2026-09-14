"""Minimal root bootstrap for one Fly-replay vault Machine."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Final

from creek_mcp.container_runtime import ContainerConfigurationError, is_mounted_volume

if TYPE_CHECKING:
    from collections.abc import Callable

_SERVICE_UID: Final[int] = 10001
_SERVICE_GID: Final[int] = 10001
_VAULT_ROOT: Final[Path] = Path("/vault")
_MOUNTINFO: Final[Path] = Path("/proc/self/mountinfo")
_MAX_SECRET_BYTES: Final[int] = 1024 * 1024
_SECRET_FILES: Final[tuple[Path, ...]] = (
    Path("/run/secrets/creek_consumer_tokens"),
    Path("/run/secrets/creek_replay_state"),
)


def prepare_runtime_paths(
    *,
    vault_root: Path = _VAULT_ROOT,
    mountinfo: Path = _MOUNTINFO,
    secret_files: tuple[Path, ...] = _SECRET_FILES,
    uid: int = _SERVICE_UID,
    gid: int = _SERVICE_GID,
) -> None:
    """Validate and narrowly normalize the encrypted mount and injected files."""
    if os.geteuid() != 0:
        raise ContainerConfigurationError("Fly vault bootstrap requires root")
    try:
        metadata = vault_root.lstat()
    except OSError as exc:
        raise ContainerConfigurationError("Fly vault volume is unavailable") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or vault_root.is_symlink()
        or not is_mounted_volume(vault_root, mountinfo)
    ):
        raise ContainerConfigurationError("Fly vault path must be an exact volume")
    os.chown(vault_root, uid, gid, follow_symlinks=False)
    os.chmod(vault_root, 0o700, follow_symlinks=False)
    for path in secret_files:
        _normalize_secret(path, uid=uid, gid=gid)


def _normalize_secret(path: Path, *, uid: int, gid: int) -> None:
    """Validate one exact regular Fly file and normalize it by descriptor."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ContainerConfigurationError("Fly vault secret is unavailable") from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_size <= 0
            or metadata.st_size > _MAX_SECRET_BYTES
        ):
            raise ContainerConfigurationError("Fly vault secret is invalid")
        os.fchown(descriptor, uid, gid)
        os.fchmod(descriptor, 0o400)
    finally:
        os.close(descriptor)


def drop_privileges_and_run(
    *,
    run: Callable[[], None] | None = None,
) -> None:
    """Permanently become Creek's service identity before importing runtime."""
    os.umask(0o077)
    os.setgroups([])
    os.setgid(_SERVICE_GID)
    os.setuid(_SERVICE_UID)
    if os.geteuid() != _SERVICE_UID or os.getegid() != _SERVICE_GID:
        raise ContainerConfigurationError("Fly vault privilege drop did not converge")
    (run or _run_vault)()


def _run_vault() -> None:
    """Import the long-running vault only after the privilege drop."""
    from creek_mcp import fly_vault_runtime

    fly_vault_runtime.main()


def main() -> None:
    """Prepare exact Fly paths and enter the replay runtime unprivileged."""
    try:
        prepare_runtime_paths()
        drop_privileges_and_run()
    except ContainerConfigurationError as exc:
        print(f"creek-fly-vault: startup refused: {exc}", file=sys.stderr)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
