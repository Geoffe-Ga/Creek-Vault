"""Root-to-uid bootstrap contract for the single Fly control Machine."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from creek_mcp.container_runtime import ContainerConfigurationError
from creek_mcp.provisioning import pilot_bootstrap

_CONTROL_DOCKERFILE = Path(__file__).resolve().parents[2] / "Dockerfile.control-plane"


def _paths(tmp_path: Path) -> tuple[Path, Path, tuple[Path, ...]]:
    state = tmp_path / "state"
    state.mkdir()
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        f"41 32 0:35 / {state} rw,nosuid - ext4 /dev/vault rw\n",
        encoding="utf-8",
    )
    secrets = tuple(tmp_path / f"secret-{index}" for index in range(3))
    for path in secrets:
        path.write_text("synthetic-secret", encoding="utf-8")
    return state, mountinfo, secrets


def test_bootstrap_normalizes_only_exact_volume_root_state_and_secret_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No recursive ownership rewrite can cross an approved pilot path."""
    state, mountinfo, secrets = _paths(tmp_path)
    chowns: list[tuple[Path, int, int]] = []
    fchowns: list[tuple[int, int]] = []
    monkeypatch.setattr(pilot_bootstrap.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        pilot_bootstrap.os,
        "chown",
        lambda path, uid, gid, **_kwargs: chowns.append((path, uid, gid)),
    )
    monkeypatch.setattr(
        pilot_bootstrap.os,
        "fchown",
        lambda _descriptor, uid, gid: fchowns.append((uid, gid)),
    )

    pilot_bootstrap.prepare_runtime_paths(
        state_root=state,
        mountinfo=mountinfo,
        secret_files=secrets,
        uid=10001,
        gid=10001,
    )

    assert chowns == [
        (state, 10001, 10001),
        (state / "runtime-secrets", 10001, 10001),
    ]
    assert fchowns == [(10001, 10001)] * len(secrets)
    assert (state.stat().st_mode & 0o777) == 0o700
    assert ((state / "runtime-secrets").stat().st_mode & 0o777) == 0o700
    assert all((path.stat().st_mode & 0o777) == 0o400 for path in secrets)


def test_bootstrap_refuses_symlinked_or_empty_secret_before_chown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A substituted Fly file cannot redirect root's narrow normalization."""
    state, mountinfo, secrets = _paths(tmp_path)
    secrets[0].unlink()
    target = tmp_path / "target"
    target.write_text("private", encoding="utf-8")
    secrets[0].symlink_to(target)
    monkeypatch.setattr(pilot_bootstrap.os, "geteuid", lambda: 0)
    monkeypatch.setattr(pilot_bootstrap.os, "chown", lambda *_args, **_kwargs: None)

    with pytest.raises(ContainerConfigurationError, match="unavailable"):
        pilot_bootstrap.prepare_runtime_paths(
            state_root=state,
            mountinfo=mountinfo,
            secret_files=secrets,
        )

    assert (target.stat().st_mode & 0o777) != 0o400


def test_privilege_drop_order_is_groups_gid_uid_then_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The long-running Python process cannot retain a root group or uid."""
    calls: list[tuple[str, object]] = []
    identity = {"uid": 0, "gid": 0}
    monkeypatch.setattr(
        pilot_bootstrap.os,
        "umask",
        lambda mask: calls.append(("umask", mask)),
    )
    monkeypatch.setattr(
        pilot_bootstrap.os,
        "setgroups",
        lambda groups: calls.append(("groups", groups)),
    )

    def setgid(gid: int) -> None:
        calls.append(("gid", gid))
        identity["gid"] = gid

    def setuid(uid: int) -> None:
        calls.append(("uid", uid))
        identity["uid"] = uid

    monkeypatch.setattr(pilot_bootstrap.os, "setgid", setgid)
    monkeypatch.setattr(pilot_bootstrap.os, "setuid", setuid)
    monkeypatch.setattr(pilot_bootstrap.os, "geteuid", lambda: identity["uid"])
    monkeypatch.setattr(pilot_bootstrap.os, "getegid", lambda: identity["gid"])
    pilot_bootstrap.drop_privileges_and_run(
        ["--disable-new-activations"],
        run=lambda argv: calls.append(("run", argv)),
    )

    assert calls[:4] == [
        ("umask", 0o077),
        ("groups", []),
        ("gid", 10001),
        ("uid", 10001),
    ]
    assert calls[4] == ("run", ["--disable-new-activations"])


def test_bootstrap_refuses_secret_values_in_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A misconfigured Fly secret never reaches Python args or a child process."""
    monkeypatch.setenv("CREEK_FLY_TOKEN_B64", "synthetic-secret")
    prepared = False

    def prepare() -> None:
        nonlocal prepared
        prepared = True

    monkeypatch.setattr(pilot_bootstrap, "prepare_runtime_paths", prepare)

    with pytest.raises(SystemExit, match="secret values"):
        pilot_bootstrap.main()

    assert prepared is False


def test_bootstrap_requires_root_and_exact_mounted_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Root and the exact reviewed volume are prerequisites to any mutation."""
    state, mountinfo, secrets = _paths(tmp_path)
    monkeypatch.setattr(pilot_bootstrap.os, "geteuid", lambda: 10001)

    with pytest.raises(ContainerConfigurationError, match="requires root"):
        pilot_bootstrap.prepare_runtime_paths(
            state_root=state,
            mountinfo=mountinfo,
            secret_files=secrets,
        )

    monkeypatch.setattr(pilot_bootstrap.os, "geteuid", lambda: 0)
    mountinfo.write_text("", encoding="utf-8")
    with pytest.raises(ContainerConfigurationError, match="exact volume"):
        pilot_bootstrap.prepare_runtime_paths(
            state_root=state,
            mountinfo=mountinfo,
            secret_files=secrets,
        )


def test_bootstrap_refuses_missing_state_and_invalid_runtime_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing or substituted mutable state fails before secret normalization."""
    missing = tmp_path / "missing"
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text("", encoding="utf-8")
    monkeypatch.setattr(pilot_bootstrap.os, "geteuid", lambda: 0)

    with pytest.raises(ContainerConfigurationError, match="unavailable"):
        pilot_bootstrap.prepare_runtime_paths(
            state_root=missing,
            mountinfo=mountinfo,
            secret_files=(),
        )

    state, mountinfo, _secrets = _paths(tmp_path)
    target = tmp_path / "runtime-target"
    target.mkdir()
    (state / "runtime-secrets").symlink_to(target)
    monkeypatch.setattr(pilot_bootstrap.os, "chown", lambda *_args, **_kwargs: None)
    with pytest.raises(ContainerConfigurationError, match="runtime state"):
        pilot_bootstrap.prepare_runtime_paths(
            state_root=state,
            mountinfo=mountinfo,
            secret_files=(),
        )


def test_bootstrap_refuses_invalid_secret_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty and oversized injected files are rejected before ownership changes."""
    state, mountinfo, secrets = _paths(tmp_path)
    secrets[0].write_bytes(b"")
    monkeypatch.setattr(pilot_bootstrap.os, "geteuid", lambda: 0)
    monkeypatch.setattr(pilot_bootstrap.os, "chown", lambda *_args, **_kwargs: None)

    with pytest.raises(ContainerConfigurationError, match="secret file is invalid"):
        pilot_bootstrap.prepare_runtime_paths(
            state_root=state,
            mountinfo=mountinfo,
            secret_files=secrets,
        )


def test_privilege_drop_refuses_nonconverged_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed kernel identity transition never imports the long-running pilot."""
    monkeypatch.setattr(pilot_bootstrap.os, "umask", lambda _mask: None)
    monkeypatch.setattr(pilot_bootstrap.os, "setgroups", lambda _groups: None)
    monkeypatch.setattr(pilot_bootstrap.os, "setgid", lambda _gid: None)
    monkeypatch.setattr(pilot_bootstrap.os, "setuid", lambda _uid: None)
    monkeypatch.setattr(pilot_bootstrap.os, "geteuid", lambda: 0)
    monkeypatch.setattr(pilot_bootstrap.os, "getegid", lambda: 0)
    ran = False

    def run(_argv: list[str]) -> None:
        nonlocal ran
        ran = True

    with pytest.raises(ContainerConfigurationError, match="did not converge"):
        pilot_bootstrap.drop_privileges_and_run([], run=run)

    assert ran is False


def test_bootstrap_main_runs_exact_sequence_and_sanitizes_startup_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The module entry point sequences setup and emits no sensitive exception text."""
    calls: list[object] = []
    monkeypatch.setattr(pilot_bootstrap.sys, "argv", ["pilot", "--flag"])
    monkeypatch.setattr(
        pilot_bootstrap,
        "prepare_runtime_paths",
        lambda: calls.append("prepare"),
    )
    monkeypatch.setattr(
        pilot_bootstrap,
        "drop_privileges_and_run",
        lambda argv: calls.append(argv),
    )

    pilot_bootstrap.main()

    assert calls == ["prepare", ["--flag"]]

    def refuse() -> None:
        raise ContainerConfigurationError("content-free-refusal")

    monkeypatch.setattr(pilot_bootstrap, "prepare_runtime_paths", refuse)
    with pytest.raises(SystemExit) as caught:
        pilot_bootstrap.main()
    assert caught.value.code == 2
    assert capsys.readouterr().err == (
        "creek-fly-pilot: startup refused: content-free-refusal\n"
    )


def test_control_image_has_pinned_root_bootstrap_and_no_secret_build_inputs() -> None:
    """Only the tiny bootstrap starts as root, then the process drops privilege."""
    text = _CONTROL_DOCKERFILE.read_text(encoding="utf-8")

    assert re.search(
        r"^FROM python:3\.12\.\d+-slim-bookworm@sha256:[0-9a-f]{64}",
        text,
        re.M,
    )
    assert "uv sync --locked --no-dev --no-install-project" in text
    assert (
        'ENTRYPOINT ["python", "-m", "creek_mcp.provisioning.pilot_bootstrap"]'
    ) in text
    assert "\nUSER " not in text
    assert "chown root:creek /run/secrets" in text
    instructions = re.findall(r"^(?:ARG|ENV)\s+(.+)$", text, re.M)
    assert all(
        word not in instruction.lower()
        for instruction in instructions
        for word in ("token", "secret", "password", "private_key")
    )
