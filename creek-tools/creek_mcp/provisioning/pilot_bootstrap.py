"""Minimal root bootstrap for Fly-injected pilot files and one state volume."""

from __future__ import annotations

import os
import stat
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final

from creek_mcp.container_runtime import ContainerConfigurationError, is_mounted_volume

# ``pool_admin_key`` imports nothing first-party at module scope, so reaching
# it here does not widen this bootstrap's root import surface (#1930).
from creek_mcp.provisioning.pool_admin_key import (
    POOL_ADMIN_KEY_GUEST_PATH,
    POOL_ADMIN_KEY_SECRET_NAME,
    pool_admin_secret_files,
    verify_pool_admin_key,
)

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
"""The files every control plane carries, whatever admin-key regime it is in.

The pool admin key is deliberately *not* here: it is appended by
:func:`~creek_mcp.provisioning.pool_admin_key.pool_admin_secret_files` only
while the Machine declares it in use, so declaring the custody path cannot
make a pilot that has no key yet unbootable (#1930).
"""
_FLY_SECRET_NAMES: Final[tuple[str, ...]] = (
    "CREEK_CONTROL_TOKENS_B64",
    "CREEK_FLY_TOKEN_B64",
    "CREEK_SECRET_MASTER_KEY_B64",
    "CREEK_TLS_CA_CERTIFICATE_B64",
    "CREEK_TLS_CA_PRIVATE_KEY_B64",
    "CREEK_HANDOFF_TOKEN_B64",
    "CREEK_FLEET_POLICY_B64",
    POOL_ADMIN_KEY_SECRET_NAME,
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


def _require_custodied_pool_admin_key() -> None:
    """Refuse startup unless the organization admin key may be presented.

    ADR-0016 Decision 3.4's per-activation key lifecycle cannot reset a cap,
    extend an expiry or revoke a key without this credential, so a control
    plane that came up without a usable one would discover that only when a
    vault's key expired.  The verdict is a closed-vocabulary code on stderr
    and nothing else: never the value, its length or a digest of it (#1930).

    Which verdicts are reachable *here* is narrower than the vocabulary, and
    deliberately so:

    * ``NOT_IN_USE`` whenever the Machine declares no admin key, which is the
      default and today's only regime;
    * ``MALFORMED``, ``UNDECLARED``, ``EXPIRED`` and ``NEAR_EXPIRY`` once a
      key is declared in use;
    * ``MISSING`` only when the declaration says in use and the file is gone
      *after* ``prepare_runtime_paths`` listed it — an absent declared file is
      normally caught first by ``_normalize_secret``'s generic exit 2, which
      is a refusal either way;
    * ``REJECTED`` **never** here: it is a runtime verdict from a vendor
      response, and startup makes no vendor call.

    The check runs after ``prepare_runtime_paths`` has narrowed the file to
    mode 0400 — before that it would read a Fly-injected file as
    ``MALFORMED`` — and before any privilege drop, so a refusal lands before
    the long-running composition is imported.  The value is format-checked
    inside
    :func:`~creek_mcp.provisioning.pool_admin_key.verify_pool_admin_key`'s own
    frame and is never returned here.

    Nothing delivers this verdict to an operator yet: emission waits on
    #1896's fleet-report pass and on adepthood's receiver admitting the kinds
    (see the ``pool_admin_key`` module docstring).

    Raises:
        SystemExit: With the verdict's own exit code when the key is unusable.
    """
    check = verify_pool_admin_key(
        Path(POOL_ADMIN_KEY_GUEST_PATH),
        environ=os.environ,
        now=datetime.now(tz=UTC),
    )
    print(f"creek-fly-pilot: pool admin key: {check.status.value}", file=sys.stderr)
    if check.refused:
        raise SystemExit(check.exit_code)


def main() -> None:
    """Reject env-carried secrets, prepare exact mounts, and exec unprivileged."""
    if any(os.environ.get(name) for name in _FLY_SECRET_NAMES):
        raise SystemExit("creek-fly-pilot: secret values must be injected as files")
    try:
        prepare_runtime_paths(
            secret_files=_SECRET_FILES + pool_admin_secret_files(os.environ)
        )
        _require_custodied_pool_admin_key()
        drop_privileges_and_run(sys.argv[1:])
    except ContainerConfigurationError as exc:
        print(f"creek-fly-pilot: startup refused: {exc}", file=sys.stderr)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
