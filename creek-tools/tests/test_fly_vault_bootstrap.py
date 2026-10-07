"""Root-to-uid bootstrap contract for a Fly-replay vault Machine."""

from __future__ import annotations

from pathlib import Path

import pytest

from creek_mcp.container_runtime import (
    MOUNTINFO_FILE_ENV,
    VAULT_PATH_ENV,
    BootstrapState,
    ContainerConfigurationError,
    ContainerSettings,
    prepare_vault,
)
from creek_mcp.provisioning import fly_vault_bootstrap


def _paths(tmp_path: Path) -> tuple[Path, Path, tuple[Path, ...]]:
    vault = tmp_path / "vault"
    vault.mkdir()
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        f"41 32 0:35 / {vault} rw,nosuid - ext4 /dev/vault rw\n",
        encoding="utf-8",
    )
    secrets = tuple(tmp_path / name for name in ("consumer", "replay"))
    for path in secrets:
        path.write_text("synthetic-secret", encoding="utf-8")
    return vault, mountinfo, secrets


def test_bootstrap_normalizes_only_exact_vault_and_declared_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fly injection becomes readable only by the irreversibly dropped uid."""
    vault, mountinfo, secrets = _paths(tmp_path)
    chowns: list[tuple[Path, int, int]] = []
    fchowns: list[tuple[int, int]] = []
    monkeypatch.setattr(fly_vault_bootstrap.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        fly_vault_bootstrap.os,
        "chown",
        lambda path, uid, gid, **_kwargs: chowns.append((path, uid, gid)),
    )
    monkeypatch.setattr(
        fly_vault_bootstrap.os,
        "fchown",
        lambda _descriptor, uid, gid: fchowns.append((uid, gid)),
    )

    fly_vault_bootstrap.prepare_runtime_paths(
        vault_root=vault,
        mountinfo=mountinfo,
        secret_files=secrets,
    )

    assert chowns == [(vault, 10001, 10001)]
    assert fchowns == [(10001, 10001), (10001, 10001)]
    assert (vault.stat().st_mode & 0o777) == 0o700
    assert all((path.stat().st_mode & 0o777) == 0o400 for path in secrets)


def test_bootstrap_removes_only_empty_filesystem_recovery_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fresh ext4 volume reaches the unchanged empty-vault initializer."""
    vault, mountinfo, secrets = _paths(tmp_path)
    (vault / "lost+found").mkdir(mode=0o700)
    monkeypatch.setattr(fly_vault_bootstrap.os, "geteuid", lambda: 0)
    monkeypatch.setattr(fly_vault_bootstrap.os, "chown", lambda *_a, **_kw: None)
    monkeypatch.setattr(fly_vault_bootstrap.os, "fchown", lambda *_a: None)

    fly_vault_bootstrap.prepare_runtime_paths(
        vault_root=vault, mountinfo=mountinfo, secret_files=secrets
    )

    assert list(vault.iterdir()) == []
    settings = ContainerSettings.from_environ(
        {VAULT_PATH_ENV: str(vault), MOUNTINFO_FILE_ENV: str(mountinfo)}
    )
    prepared = prepare_vault(settings)
    assert prepared.state is BootstrapState.INITIALIZED
    config = prepared.config_path.read_bytes()
    fly_vault_bootstrap.prepare_runtime_paths(
        vault_root=vault, mountinfo=mountinfo, secret_files=secrets
    )
    assert prepare_vault(settings).state is BootstrapState.EXISTING
    assert prepared.config_path.read_bytes() == config


@pytest.mark.parametrize("kind", ["populated", "symlink", "file", "existing-vault"])
def test_bootstrap_preserves_recovery_data_and_existing_vaults(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    """Recovery cleanup never recursively deletes or follows a substituted path."""
    vault, mountinfo, secrets = _paths(tmp_path)
    recovery = vault / "lost+found"
    target = tmp_path / "recovery-target"
    target.mkdir()
    if kind == "symlink":
        recovery.symlink_to(target, target_is_directory=True)
    elif kind == "file":
        recovery.write_text("preserve", encoding="utf-8")
    else:
        recovery.mkdir()
        if kind == "populated":
            (recovery / "recovered").write_text("preserve", encoding="utf-8")
        else:
            (vault / "existing").write_text("preserve", encoding="utf-8")
    monkeypatch.setattr(fly_vault_bootstrap.os, "geteuid", lambda: 0)
    monkeypatch.setattr(fly_vault_bootstrap.os, "chown", lambda *_a, **_kw: None)
    monkeypatch.setattr(fly_vault_bootstrap.os, "fchown", lambda *_a: None)

    if kind == "populated":
        with pytest.raises(ContainerConfigurationError, match="recovery directory"):
            fly_vault_bootstrap.prepare_runtime_paths(
                vault_root=vault, mountinfo=mountinfo, secret_files=secrets
            )
        assert (recovery / "recovered").read_text(encoding="utf-8") == "preserve"
    else:
        fly_vault_bootstrap.prepare_runtime_paths(
            vault_root=vault, mountinfo=mountinfo, secret_files=secrets
        )
    assert recovery.exists()
    assert target.is_dir()


def test_bootstrap_refuses_symlinked_or_incomplete_runtime_before_chown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A substituted or missing Fly file cannot reach root normalization."""
    vault, mountinfo, secrets = _paths(tmp_path)
    target = tmp_path / "target"
    target.write_text("private", encoding="utf-8")
    secrets[0].unlink()
    secrets[0].symlink_to(target)
    monkeypatch.setattr(fly_vault_bootstrap.os, "geteuid", lambda: 0)
    monkeypatch.setattr(fly_vault_bootstrap.os, "chown", lambda *_a, **_kw: None)

    with pytest.raises(ContainerConfigurationError, match="unavailable"):
        fly_vault_bootstrap.prepare_runtime_paths(
            vault_root=vault,
            mountinfo=mountinfo,
            secret_files=secrets,
        )

    assert (target.stat().st_mode & 0o777) != 0o400


def test_bootstrap_requires_root_and_exact_mounted_vault(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only Fly's root init may normalize the exact encrypted mount."""
    vault, mountinfo, secrets = _paths(tmp_path)
    monkeypatch.setattr(fly_vault_bootstrap.os, "geteuid", lambda: 10001)
    with pytest.raises(ContainerConfigurationError, match="requires root"):
        fly_vault_bootstrap.prepare_runtime_paths(
            vault_root=vault,
            mountinfo=mountinfo,
            secret_files=secrets,
        )

    monkeypatch.setattr(fly_vault_bootstrap.os, "geteuid", lambda: 0)
    mountinfo.write_text("", encoding="utf-8")
    with pytest.raises(ContainerConfigurationError, match="exact volume"):
        fly_vault_bootstrap.prepare_runtime_paths(
            vault_root=vault,
            mountinfo=mountinfo,
            secret_files=secrets,
        )


def test_bootstrap_refuses_a_missing_vault_and_an_empty_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Root normalization fails closed on absent storage or empty injection."""
    vault, mountinfo, secrets = _paths(tmp_path)
    monkeypatch.setattr(fly_vault_bootstrap.os, "geteuid", lambda: 0)

    with pytest.raises(ContainerConfigurationError, match="volume is unavailable"):
        fly_vault_bootstrap.prepare_runtime_paths(
            vault_root=tmp_path / "missing-vault",
            mountinfo=mountinfo,
            secret_files=secrets,
        )

    secrets[0].write_bytes(b"")
    monkeypatch.setattr(fly_vault_bootstrap.os, "chown", lambda *_a, **_kw: None)
    with pytest.raises(ContainerConfigurationError, match="secret is invalid"):
        fly_vault_bootstrap.prepare_runtime_paths(
            vault_root=vault,
            mountinfo=mountinfo,
            secret_files=secrets,
        )


def test_privilege_drop_is_irreversible_before_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No supplementary or root identity survives into the vault server."""
    calls: list[tuple[str, object]] = []
    identity = {"uid": 0, "gid": 0}
    monkeypatch.setattr(
        fly_vault_bootstrap.os,
        "umask",
        lambda mask: calls.append(("umask", mask)),
    )
    monkeypatch.setattr(
        fly_vault_bootstrap.os,
        "setgroups",
        lambda groups: calls.append(("groups", groups)),
    )

    def setgid(gid: int) -> None:
        calls.append(("gid", gid))
        identity["gid"] = gid

    def setuid(uid: int) -> None:
        calls.append(("uid", uid))
        identity["uid"] = uid

    monkeypatch.setattr(fly_vault_bootstrap.os, "setgid", setgid)
    monkeypatch.setattr(fly_vault_bootstrap.os, "setuid", setuid)
    monkeypatch.setattr(fly_vault_bootstrap.os, "geteuid", lambda: identity["uid"])
    monkeypatch.setattr(fly_vault_bootstrap.os, "getegid", lambda: identity["gid"])
    fly_vault_bootstrap.drop_privileges_and_run(run=lambda: calls.append(("run", None)))

    assert calls == [
        ("umask", 0o077),
        ("groups", []),
        ("gid", 10001),
        ("uid", 10001),
        ("run", None),
    ]


def test_privilege_drop_refuses_to_start_when_identity_does_not_converge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed kernel identity transition cannot reach the vault runtime."""
    reached_runtime = False
    monkeypatch.setattr(fly_vault_bootstrap.os, "umask", lambda _mask: None)
    monkeypatch.setattr(fly_vault_bootstrap.os, "setgroups", lambda _groups: None)
    monkeypatch.setattr(fly_vault_bootstrap.os, "setgid", lambda _gid: None)
    monkeypatch.setattr(fly_vault_bootstrap.os, "setuid", lambda _uid: None)
    monkeypatch.setattr(fly_vault_bootstrap.os, "geteuid", lambda: 0)
    monkeypatch.setattr(fly_vault_bootstrap.os, "getegid", lambda: 0)

    def run() -> None:
        nonlocal reached_runtime
        reached_runtime = True

    with pytest.raises(ContainerConfigurationError, match="did not converge"):
        fly_vault_bootstrap.drop_privileges_and_run(run=run)

    assert not reached_runtime


def test_main_emits_only_a_generic_refusal_on_configuration_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Startup failures expose no injected path or secret detail."""
    monkeypatch.setattr(
        fly_vault_bootstrap,
        "prepare_runtime_paths",
        lambda: (_ for _ in ()).throw(ContainerConfigurationError("synthetic")),
    )

    with pytest.raises(SystemExit) as exc_info:
        fly_vault_bootstrap.main()

    assert exc_info.value.code == 2
    assert capsys.readouterr().err == "creek-fly-vault: startup refused: synthetic\n"


def test_machine_runtime_uses_root_bootstrap_then_unprivileged_server() -> None:
    """The provider contract must not jump from injected files to Python as uid."""
    source = fly_vault_bootstrap.__file__
    assert source is not None
    assert (
        Path("/run/secrets/creek_consumer_tokens"),
        Path("/run/secrets/creek_replay_state"),
    ) == fly_vault_bootstrap._SECRET_FILES
