"""Minimal root bootstrap for Fly-injected pilot files and one state volume."""

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
_STATE_ROOT: Final[Path] = Path("/data")
_MOUNTINFO: Final[Path] = Path("/proc/self/mountinfo")
_MAX_SECRET_BYTES: Final[int] = 1024 * 1024
_SECRET_FILES: Final[tuple[Path, ...]] = (
    Path("/run/secrets/creek_control_tokens"),
    Path("/run/secrets/creek_fly_token"),
    Path("/run/secrets/creek_secret_master_key"),
    Path("/run/secrets/creek_tls_ca_certificate"),
    Path("/run/secrets/creek_tls_ca_private_key"),
    Path("/run/secrets/creek_handoff_token"),
    Path("/run/secrets/creek_fleet_policy"),
)
_FLY_SECRET_NAMES: Final[tuple[str, ...]] = (
    "CREEK_CONTROL_TOKENS_B64",
    "CREEK_FLY_TOKEN_B64",
    "CREEK_SECRET_MASTER_KEY_B64",
    "CREEK_TLS_CA_CERTIFICATE_B64",
    "CREEK_TLS_CA_PRIVATE_KEY_B64",
    "CREEK_HANDOFF_TOKEN_B64",
    "CREEK_FLEET_POLICY_B64",
)


def prepare_runtime_paths(
    *,
    state_root: Path = _STATE_ROOT,
    mountinfo: Path = _MOUNTINFO,
    secret_files: tuple[Path, ...] = _SECRET_FILES,
    uid: int = _SERVICE_UID,
    gid: int = _SERVICE_GID,
) -> None:
    """Validate exact paths, normalize them narrowly, and touch no other tree."""
    if os.geteuid() != 0:
        raise ContainerConfigurationError("Fly pilot bootstrap requires root")
    try:
        state_metadata = state_root.lstat()
    except OSError as exc:
        raise ContainerConfigurationError("pilot state volume is unavailable") from exc
    if (
        not stat.S_ISDIR(state_metadata.st_mode)
        or state_root.is_symlink()
        or not is_mounted_volume(state_root, mountinfo)
    ):
        raise ContainerConfigurationError("pilot state path must be an exact volume")
    os.chown(state_root, uid, gid, follow_symlinks=False)
    os.chmod(state_root, 0o700, follow_symlinks=False)
    runtime_state = state_root / "runtime-secrets"
    runtime_state.mkdir(mode=0o700, exist_ok=True)
    if runtime_state.is_symlink() or not runtime_state.is_dir():
        raise ContainerConfigurationError("pilot runtime state path is invalid")
    os.chown(runtime_state, uid, gid, follow_symlinks=False)
    os.chmod(runtime_state, 0o700, follow_symlinks=False)
    for path in secret_files:
        _normalize_secret(path, uid=uid, gid=gid)


def _normalize_secret(path: Path, *, uid: int, gid: int) -> None:
    """Validate one nonempty regular injected file and normalize by descriptor."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ContainerConfigurationError("pilot secret file is unavailable") from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_size <= 0
            or metadata.st_size > _MAX_SECRET_BYTES
        ):
            raise ContainerConfigurationError("pilot secret file is invalid")
        os.fchown(descriptor, uid, gid)
        os.fchmod(descriptor, 0o400)
    finally:
        os.close(descriptor)


def drop_privileges_and_run(
    argv: list[str],
    *,
    run: Callable[[list[str]], None] | None = None,
) -> None:
    """Permanently become Creek's uid/gid before importing the pilot runtime."""
    os.umask(0o077)
    os.setgroups([])
    os.setgid(_SERVICE_GID)
    os.setuid(_SERVICE_UID)
    if os.geteuid() != _SERVICE_UID or os.getegid() != _SERVICE_GID:
        raise ContainerConfigurationError("pilot privilege drop did not converge")
    (run or _run_pilot)(argv)


def _run_pilot(argv: list[str]) -> None:
    """Import the long-running composition only after privileges are gone."""
    from creek_mcp.provisioning import pilot_cli

    pilot_cli.main(argv)


def main() -> None:
    """Reject env-carried secrets, prepare exact mounts, and exec unprivileged."""
    if any(os.environ.get(name) for name in _FLY_SECRET_NAMES):
        raise SystemExit("creek-fly-pilot: secret values must be injected as files")
    try:
        prepare_runtime_paths()
        drop_privileges_and_run(sys.argv[1:])
    except ContainerConfigurationError as exc:
        print(f"creek-fly-pilot: startup refused: {exc}", file=sys.stderr)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
