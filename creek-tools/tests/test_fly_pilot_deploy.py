"""Offline rendering and authorization-gated Fly pilot deploy mechanism."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tomllib
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from creek_mcp.provisioning import fly_pilot_deploy
from creek_mcp.provisioning.fly_pilot_config import (
    FlyPilotCoordinates,
    render_fly_toml,
)
from creek_mcp.provisioning.fly_pilot_deploy import CommandResult, deploy

_TEMPLATE = (
    Path(__file__).resolve().parents[2] / "deploy" / "fly-pilot" / "fly.toml.template"
)
_NOW = datetime(2026, 9, 27, 12, tzinfo=UTC)
_TOKEN = "FlyV1 fm2_synthetic+org/token,canary="
_ORG_ID = "synthetic-org-id"


def _coordinates() -> FlyPilotCoordinates:
    return FlyPilotCoordinates(
        app="creek-control-pilot",
        region="iad",
        organization="creek-vaults",
        vault_image="registry.fly.io/vault@sha256:" + "a" * 64,
        control_image="registry.fly.io/control@sha256:" + "b" * 64,
        token_expires_at="2026-09-28T12:00:00+00:00",
        handoff_url="https://adepthood.example/control/v1/handoff",
        alert_url="https://adepthood.example/internal/vault-provisioning/alerts",
        volume="creek_control_state",
    )


def _authorization(
    tmp_path: Path,
    *,
    cross_network: bool = True,
    max_spend_usd_cents: int = 2500,
) -> tuple[Path, str]:
    path = tmp_path / "private-authorization.json"
    document = {
        "schema": "creek_fly_pilot_deploy_authorization_v1",
        "app": "creek-control-pilot",
        "organization": "creek-vaults",
        "organization_id": _ORG_ID,
        "region": "iad",
        "max_spend_usd_cents": max_spend_usd_cents,
        "approved_until": "2026-09-28T12:00:00+00:00",
        "cross_network_replays_enabled": cross_network,
    }
    path.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")
    digest = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    return path, digest


def _token_file(tmp_path: Path, value: str = _TOKEN) -> Path:
    path = tmp_path / "fly-token"
    path.write_text(value, encoding="ascii")
    path.chmod(0o600)
    return path


def _org_document() -> bytes:
    return json.dumps({"slug": "creek-vaults", "id": _ORG_ID}).encode()


def _apps_document() -> bytes:
    return json.dumps(
        [{"Name": "creek-control-pilot", "Organization": "creek-vaults"}]
    ).encode()


def _machine_document(
    *,
    mount_encrypted: bool | None = True,
    provider_list_shape: bool = True,
) -> dict[str, Any]:
    mount: dict[str, Any] = {
        "volume": "private-volume",
        "path": "/data",
    }
    if mount_encrypted is not None:
        mount["encrypted"] = mount_encrypted
    if provider_list_shape:
        mount.update({"name": _coordinates().volume, "size_gb": 1})
    service: dict[str, Any] = {
        "protocol": "tcp",
        "internal_port": 8080,
        "ports": [
            {"port": 80, "handlers": ["http"]},
            {"port": 443, "handlers": ["tls", "http"]},
        ],
    }
    if provider_list_shape:
        service.update(
            {
                "autostart": True,
                "autostop": False,
                "checks": [
                    {
                        "grace_period": "30s",
                        "interval": "10s",
                        "method": "GET",
                        "path": "/__fly/health",
                        "timeout": "2s",
                        "type": "http",
                    }
                ],
                "concurrency": {
                    "hard_limit": 25,
                    "soft_limit": 20,
                    "type": "requests",
                },
                "force_instance_key": None,
                "min_machines_running": 1,
            }
        )
        service["ports"][0]["force_https"] = True
        service["ports"][1]["handlers"] = ["http", "tls"]
    return {
        "id": "private-machine",
        "region": "iad",
        "state": "started",
        "image_ref": {"digest": "sha256:" + "b" * 64},
        "config": {
            "image": _coordinates().control_image,
            "guest": {"cpu_kind": "shared", "cpus": 1, "memory_mb": 1024},
            "restart": {"policy": "on-failure", "max_retries": 3},
            "mounts": [mount],
            "services": [service],
        },
    }


def test_deploy_verifies_post_state_from_supported_json_inventory(
    tmp_path: Path,
) -> None:
    """The live Fly CLI exposes JSON on list, not on machine status."""
    authorization, digest = _authorization(tmp_path)
    token_file = _token_file(tmp_path)
    machine_lists = 0
    commands: list[list[str]] = []

    def fake_run(command: list[str], _environ: dict[str, str]) -> CommandResult:
        nonlocal machine_lists
        commands.append(command)
        if command[1:3] == ["orgs", "show"]:
            return CommandResult(_org_document())
        if command[1:3] == ["apps", "list"]:
            return CommandResult(_apps_document())
        if command[1:3] == ["machine", "list"]:
            machine_lists += 1
            machines = (
                []
                if machine_lists == 1
                else [_machine_document(provider_list_shape=True)]
            )
            return CommandResult(json.dumps(machines).encode())
        if command[1:3] == ["volumes", "list"]:
            volumes = []
            if machine_lists > 1:
                volumes = [
                    {
                        "id": "private-volume",
                        "name": _coordinates().volume,
                        "region": "iad",
                        "encrypted": True,
                        "attached_machine_id": "private-machine",
                    }
                ]
            return CommandResult(json.dumps(volumes).encode())
        if command[1:3] == ["machine", "status"]:
            raise AssertionError("fly machine status does not support --json")
        return CommandResult(b"")

    deploy(
        _TEMPLATE,
        _coordinates(),
        token_file,
        authorization,
        digest,
        run=fake_run,
        now=_NOW,
    )

    assert not any(command[1:3] == ["machine", "status"] for command in commands)


def test_enriched_provider_inventory_remains_fail_closed() -> None:
    """Fly's extra list fields are accepted only at their reviewed values."""
    machine = _machine_document(provider_list_shape=True)
    config = machine["config"]
    mount = config["mounts"][0]
    service = config["services"][0]

    assert fly_pilot_deploy._mount_is_exact(
        config["mounts"], "private-volume", _coordinates().volume
    )
    assert fly_pilot_deploy._service_is_exact(config["services"])

    mount["size_gb"] = 2
    service["autostop"] = True

    assert not fly_pilot_deploy._mount_is_exact(
        config["mounts"], "private-volume", _coordinates().volume
    )
    assert not fly_pilot_deploy._service_is_exact(config["services"])


@pytest.mark.parametrize(
    "field",
    [
        "autostart",
        "autostop",
        "checks",
        "concurrency",
        "force_instance_key",
        "min_machines_running",
    ],
)
def test_enriched_provider_rejects_every_missing_service_attestation(
    field: str,
) -> None:
    """A missing current-list policy field is not replaced by a safe default."""
    machine = _machine_document()
    service = machine["config"]["services"][0]
    del service[field]

    assert not fly_pilot_deploy._service_is_exact(machine["config"]["services"])


@pytest.mark.parametrize("field", ["volume", "path", "encrypted", "name", "size_gb"])
def test_enriched_provider_rejects_every_missing_mount_attestation(
    field: str,
) -> None:
    """Current-list mount enrichment must be present rather than inferred."""
    machine = _machine_document()
    mount = machine["config"]["mounts"][0]
    del mount[field]

    assert not fly_pilot_deploy._mount_is_exact(
        machine["config"]["mounts"],
        "private-volume",
        _coordinates().volume,
    )


def test_enriched_provider_rejects_missing_force_https_attestation() -> None:
    """Port 80 must explicitly attest the reviewed HTTPS redirect policy."""
    machine = _machine_document()
    del machine["config"]["services"][0]["ports"][0]["force_https"]

    assert not fly_pilot_deploy._service_is_exact(machine["config"]["services"])


def test_enriched_provider_rejects_unknown_or_duplicate_service_fields() -> None:
    """Provider enrichment cannot hide policy drift behind accepted keys."""
    machine = _machine_document(provider_list_shape=True)
    service = machine["config"]["services"][0]
    service["unreviewed_policy"] = True

    assert not fly_pilot_deploy._service_is_exact(machine["config"]["services"])

    service.pop("unreviewed_policy")
    service["ports"][1]["handlers"] = ["http", "http"]

    assert not fly_pilot_deploy._service_is_exact(machine["config"]["services"])


def test_rendered_config_has_one_machine_volume_and_secret_contract() -> None:
    """The reviewed template is structurally deployable without secret values."""
    rendered = render_fly_toml(_TEMPLATE, _coordinates())
    document = tomllib.loads(rendered)

    assert document["app"] == "creek-control-pilot"
    assert document["http_service"]["checks"] == [
        {
            "grace_period": "30s",
            "interval": "10s",
            "method": "GET",
            "path": "/__fly/health",
            "timeout": "2s",
        }
    ]
    assert document["mounts"] == [
        {
            "source": "creek_control_state",
            "destination": "/data",
            "initial_size": "1GB",
            "snapshot_retention": 7,
            "scheduled_snapshots": True,
        }
    ]
    assert document["restart"] == [{"policy": "on-failure", "retries": 3}]
    assert "processes" not in document
    assert len(document["files"]) == 7
    assert all(set(item) == {"guest_path", "secret_name"} for item in document["files"])
    assert "{{" not in rendered
    assert "provider-token" not in rendered


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("app", 'bad"\n[env]'),
        ("region", "iad;ord"),
        ("organization", "creek vaults"),
        ("volume", "../volume"),
        ("vault_image", "registry.fly.io/vault:latest"),
        ("control_image", "registry.fly.io/control:latest"),
        ("handoff_url", "http://adepthood.example/callback"),
    ],
)
def test_renderer_refuses_mutable_or_injectable_coordinates(
    field: str,
    value: str,
) -> None:
    """Operator values cannot escape TOML or weaken the immutable deployment."""
    values = {
        "app": "creek-control-pilot",
        "region": "iad",
        "organization": "creek-vaults",
        "vault_image": "registry.fly.io/vault@sha256:" + "a" * 64,
        "control_image": "registry.fly.io/control@sha256:" + "b" * 64,
        "token_expires_at": "2026-09-28T12:00:00+00:00",
        "handoff_url": "https://adepthood.example/callback",
        "alert_url": "https://adepthood.example/internal/vault-provisioning/alerts",
        "volume": "creek_control_state",
    }
    values[field] = value

    with pytest.raises(ValueError):
        FlyPilotCoordinates(**values)


def test_deploy_requires_hash_bound_live_approval_and_verifies_exact_cardinality(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No provider command runs until exact coordinates and replay are approved."""
    authorization, digest = _authorization(tmp_path)
    token_file = _token_file(tmp_path)
    commands: list[list[str]] = []
    isolated_homes: list[Path] = []
    ambient = tmp_path / "ambient-home"
    (ambient / ".fly").mkdir(parents=True)
    (ambient / ".fly" / "config.yml").write_text(
        "access_token: ambient-personal-token-canary\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(ambient))
    for name in (
        "FLY_ACCESS_TOKEN",
        "FLY_API_TOKEN",
        "FLY_CONFIG_DIR",
        "FLY_TOKEN",
        "FLYCTL_ACCESS_TOKEN",
        "XDG_CONFIG_HOME",
    ):
        monkeypatch.setenv(name, f"ambient-{name.lower()}-canary")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ambient-unrelated-secret-canary")

    def fake_run(command: list[str], environ: dict[str, str]) -> CommandResult:
        commands.append(command)
        home = Path(environ["HOME"])
        isolated_homes.append(home)
        assert home != ambient
        config_lines = (
            (home / ".fly" / "config.yml").read_text(encoding="utf-8").splitlines()
        )
        assert config_lines[0] == f"access_token: {_TOKEN}"
        assert config_lines[1].startswith("last_login: ")
        assert datetime.fromisoformat(config_lines[1].removeprefix("last_login: "))
        assert (home.stat().st_mode & 0o777) == 0o700
        assert ((home / ".fly").stat().st_mode & 0o777) == 0o700
        assert ((home / ".fly" / "config.yml").stat().st_mode & 0o777) == 0o600
        assert all(
            name not in environ
            for name in (
                "FLY_ACCESS_TOKEN",
                "FLY_API_TOKEN",
                "FLY_CONFIG_DIR",
                "FLY_TOKEN",
                "FLYCTL_ACCESS_TOKEN",
                "XDG_CONFIG_HOME",
            )
        )
        assert _TOKEN not in repr(command)
        assert _TOKEN not in repr(environ)
        assert "ANTHROPIC_API_KEY" not in environ
        assert "ambient-unrelated-secret-canary" not in repr(environ)
        if command[1:3] == ["orgs", "show"]:
            return CommandResult(_org_document())
        if command[1:3] == ["apps", "list"]:
            return CommandResult(_apps_document())
        if command[1:3] == ["machine", "list"]:
            return CommandResult(json.dumps([_machine_document()]).encode())
        if command[1:3] == ["volumes", "list"]:
            return CommandResult(
                b'[{"id":"private-volume","name":"creek_control_state",'
                b'"region":"iad","encrypted":true,'
                b'"attached_machine_id":"private-machine"}]'
            )
        return CommandResult(b"")

    deploy(
        _TEMPLATE,
        _coordinates(),
        token_file,
        authorization,
        digest,
        run=fake_run,
        now=_NOW,
    )

    assert len(commands) == 7
    deploy_command = commands[4]
    assert deploy_command[:3] == ["fly", "deploy", "--ha=false"]
    assert "--image" in deploy_command
    assert _coordinates().control_image in deploy_command
    assert str(authorization) not in " ".join(
        argument for command in commands for argument in command
    )
    assert digest not in repr(commands)
    assert isolated_homes
    assert all(not path.exists() for path in isolated_homes)


def test_deploy_refuses_disabled_cross_network_replay_before_any_command(
    tmp_path: Path,
) -> None:
    """The default-disabled Fly org setting is a mandatory owner prerequisite."""
    authorization, digest = _authorization(tmp_path, cross_network=False)
    token_file = _token_file(tmp_path)
    commands: list[list[str]] = []

    with pytest.raises(ValueError, match="not active"):
        deploy(
            _TEMPLATE,
            _coordinates(),
            token_file,
            authorization,
            digest,
            run=lambda command, _environ: (
                commands.append(command) or CommandResult(b"")
            ),
            now=_NOW,
        )

    assert commands == []


@pytest.mark.parametrize(
    "expiry",
    ["2026-09-27T11:59:59+00:00", "2026-10-04T12:00:01+00:00"],
)
def test_deploy_refuses_inactive_or_overlong_runtime_token_before_provider(
    tmp_path: Path,
    expiry: str,
) -> None:
    """A deploy cannot begin outside the reviewed org-token lifetime."""
    authorization, digest = _authorization(tmp_path)
    token_file = _token_file(tmp_path)
    commands: list[list[str]] = []

    with pytest.raises(ValueError, match="not active"):
        deploy(
            _TEMPLATE,
            replace(_coordinates(), token_expires_at=expiry),
            token_file,
            authorization,
            digest,
            run=lambda command, _environ: (
                commands.append(command) or CommandResult(b"")
            ),
            now=_NOW,
        )

    assert commands == []


@pytest.mark.parametrize("max_spend_usd_cents", [0, 2499, 2501, True])
def test_deploy_requires_the_exact_reviewed_twenty_five_dollar_cap(
    tmp_path: Path,
    max_spend_usd_cents: int,
) -> None:
    """The private approval cannot silently widen or change the pilot cap."""
    authorization, digest = _authorization(
        tmp_path,
        max_spend_usd_cents=max_spend_usd_cents,
    )
    token_file = _token_file(tmp_path)
    commands: list[list[str]] = []

    with pytest.raises(ValueError, match="not active"):
        deploy(
            _TEMPLATE,
            _coordinates(),
            token_file,
            authorization,
            digest,
            run=lambda command, _environ: (
                commands.append(command) or CommandResult(b"")
            ),
            now=_NOW,
        )

    assert commands == []


def test_deploy_refuses_provider_mount_shape_without_encryption_attestation(
    tmp_path: Path,
) -> None:
    """Current list JSON must carry its own mount encryption attestation."""
    authorization, digest = _authorization(tmp_path)
    token_file = _token_file(tmp_path)
    commands: list[list[str]] = []

    def fake_run(command: list[str], _environ: dict[str, str]) -> CommandResult:
        commands.append(command)
        if command[1:3] == ["orgs", "show"]:
            return CommandResult(_org_document())
        if command[1:3] == ["apps", "list"]:
            return CommandResult(_apps_document())
        if command[1:3] == ["machine", "list"]:
            return CommandResult(
                json.dumps([_machine_document(mount_encrypted=None)]).encode()
            )
        if command[1:3] == ["volumes", "list"]:
            return CommandResult(
                b'[{"id":"private-volume","name":"creek_control_state",'
                b'"region":"iad","encrypted":true,'
                b'"attached_machine_id":"private-machine"}]'
            )
        return CommandResult(b"")

    with pytest.raises(RuntimeError, match="preflight drifted"):
        deploy(
            _TEMPLATE,
            _coordinates(),
            token_file,
            authorization,
            digest,
            run=fake_run,
            now=_NOW,
        )

    assert not any(command[1:2] == ["deploy"] for command in commands)


def test_deploy_refuses_wrong_preflight_cardinality_before_mutation(
    tmp_path: Path,
) -> None:
    """A second Machine is rejected read-only before fly deploy can mutate."""
    authorization, digest = _authorization(tmp_path)
    token_file = _token_file(tmp_path)
    commands: list[list[str]] = []

    def fake_run(command: list[str], _environ: dict[str, str]) -> CommandResult:
        commands.append(command)
        if command[1:3] == ["orgs", "show"]:
            return CommandResult(_org_document())
        if command[1:3] == ["apps", "list"]:
            return CommandResult(_apps_document())
        if command[1:3] == ["machine", "list"]:
            return CommandResult(b'[{"id":"one"},{"id":"secret-two"}]')
        return CommandResult(b"[]")

    with pytest.raises(RuntimeError, match="count is not one") as caught:
        deploy(
            _TEMPLATE,
            _coordinates(),
            token_file,
            authorization,
            digest,
            run=fake_run,
            now=_NOW,
        )

    assert "secret-two" not in str(caught.value)
    assert not any(command[1:2] == ["deploy"] for command in commands)


def test_deploy_refuses_drifted_preflight_machine_before_mutation(
    tmp_path: Path,
) -> None:
    """One resource pair is not admissible when its inspected policy drifted."""
    authorization, digest = _authorization(tmp_path)
    token_file = _token_file(tmp_path)
    commands: list[list[str]] = []

    def fake_run(command: list[str], _environ: dict[str, str]) -> CommandResult:
        commands.append(command)
        if command[1:3] == ["orgs", "show"]:
            return CommandResult(_org_document())
        if command[1:3] == ["apps", "list"]:
            return CommandResult(_apps_document())
        if command[1:3] == ["machine", "list"]:
            document = _machine_document()
            document["region"] = "wrong"
            document["state"] = "stopped"
            return CommandResult(json.dumps([document]).encode())
        if command[1:3] == ["volumes", "list"]:
            return CommandResult(
                b'[{"id":"private-volume","name":"creek_control_state",'
                b'"region":"iad","encrypted":true,'
                b'"attached_machine_id":"private-machine"}]'
            )
        return CommandResult(b"")

    with pytest.raises(RuntimeError, match="preflight drifted"):
        deploy(
            _TEMPLATE,
            _coordinates(),
            token_file,
            authorization,
            digest,
            run=fake_run,
            now=_NOW,
        )

    assert not any(command[1:2] == ["deploy"] for command in commands)


def test_deploy_admits_one_exact_precreated_unattached_volume(
    tmp_path: Path,
) -> None:
    """The documented create-volume-before-deploy path remains deployable."""
    authorization, digest = _authorization(tmp_path)
    token_file = _token_file(tmp_path)
    commands: list[list[str]] = []
    machine_lists = 0

    def fake_run(command: list[str], _environ: dict[str, str]) -> CommandResult:
        nonlocal machine_lists
        commands.append(command)
        if command[1:3] == ["orgs", "show"]:
            return CommandResult(_org_document())
        if command[1:3] == ["apps", "list"]:
            return CommandResult(_apps_document())
        if command[1:3] == ["machine", "list"]:
            machine_lists += 1
            if machine_lists == 1:
                return CommandResult(b"[]")
            return CommandResult(json.dumps([_machine_document()]).encode())
        if command[1:3] == ["volumes", "list"]:
            attached = machine_lists > 1
            return CommandResult(
                json.dumps(
                    [
                        {
                            "id": "private-volume",
                            "name": _coordinates().volume,
                            "region": "iad",
                            "encrypted": True,
                            "attached_machine_id": (
                                "private-machine" if attached else None
                            ),
                        }
                    ]
                ).encode()
            )
        return CommandResult(b"")

    deploy(
        _TEMPLATE,
        _coordinates(),
        token_file,
        authorization,
        digest,
        run=fake_run,
        now=_NOW,
    )

    assert any(command[1:2] == ["deploy"] for command in commands)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("name", "wrong-volume"),
        ("region", "ord"),
        ("encrypted", False),
        ("attached_machine_id", "private-machine"),
    ],
)
def test_deploy_refuses_drifted_precreated_volume_before_mutation(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    """Only the exact reviewed detached volume may precede Fly deployment."""
    authorization, digest = _authorization(tmp_path)
    token_file = _token_file(tmp_path)
    commands: list[list[str]] = []
    volume: dict[str, object] = {
        "id": "private-volume",
        "name": _coordinates().volume,
        "region": "iad",
        "encrypted": True,
        "attached_machine_id": None,
    }
    volume[field] = value

    def fake_run(command: list[str], _environ: dict[str, str]) -> CommandResult:
        commands.append(command)
        if command[1:3] == ["orgs", "show"]:
            return CommandResult(_org_document())
        if command[1:3] == ["apps", "list"]:
            return CommandResult(_apps_document())
        if command[1:3] == ["machine", "list"]:
            return CommandResult(b"[]")
        if command[1:3] == ["volumes", "list"]:
            return CommandResult(json.dumps([volume]).encode())
        return CommandResult(b"")

    with pytest.raises(RuntimeError, match="preflight volume drifted"):
        deploy(
            _TEMPLATE,
            _coordinates(),
            token_file,
            authorization,
            digest,
            run=fake_run,
            now=_NOW,
        )

    assert not any(command[1:2] == ["deploy"] for command in commands)


@pytest.mark.parametrize(
    ("mode", "value"),
    [(0o644, _TOKEN), (0o600, ""), (0o600, "first\nsecond")],
)
def test_deploy_refuses_unsafe_token_file_before_any_provider_command(
    tmp_path: Path,
    mode: int,
    value: str,
) -> None:
    """Only one bounded owner-only token file can create flyctl state."""
    authorization, digest = _authorization(tmp_path)
    token_file = _token_file(tmp_path, value)
    token_file.chmod(mode)
    commands: list[list[str]] = []

    with pytest.raises(ValueError, match="token file"):
        deploy(
            _TEMPLATE,
            _coordinates(),
            token_file,
            authorization,
            digest,
            run=lambda command, _environ: (
                commands.append(command) or CommandResult(b"")
            ),
            now=_NOW,
        )

    assert commands == []


def test_deploy_refuses_symlinked_token_file_before_any_provider_command(
    tmp_path: Path,
) -> None:
    """A token path substitution never creates disposable flyctl state."""
    authorization, digest = _authorization(tmp_path)
    target = _token_file(tmp_path)
    symlink = tmp_path / "fly-token-link"
    symlink.symlink_to(target)
    commands: list[list[str]] = []

    with pytest.raises(ValueError, match="token file"):
        deploy(
            _TEMPLATE,
            _coordinates(),
            symlink,
            authorization,
            digest,
            run=lambda command, _environ: (
                commands.append(command) or CommandResult(b"")
            ),
            now=_NOW,
        )

    assert commands == []


def test_token_reader_requires_the_effective_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Owner-only mode is insufficient when the file belongs to another uid."""
    token_file = _token_file(tmp_path)
    real_fstat = os.fstat

    def wrong_owner(descriptor: int) -> os.stat_result:
        values = list(real_fstat(descriptor))
        values[stat.ST_UID] = os.geteuid() + 1
        return os.stat_result(values)

    monkeypatch.setattr(fly_pilot_deploy.os, "fstat", wrong_owner)

    with pytest.raises(ValueError, match="token file"):
        fly_pilot_deploy._read_token_file(token_file)


@pytest.mark.parametrize("terminal_newline", ["", "\n"])
def test_token_reader_accepts_current_fly_org_token_shape(
    tmp_path: Path,
    terminal_newline: str,
) -> None:
    """Fly's required ``FlyV1 `` separator is data, not a multiline token."""
    token_file = _token_file(tmp_path, _TOKEN + terminal_newline)

    assert fly_pilot_deploy._read_token_file(token_file) == _TOKEN


@pytest.mark.parametrize("value", ["token\x00suffix", "token\x1fsuffix"])
def test_token_reader_rejects_ascii_control_characters(
    tmp_path: Path,
    value: str,
) -> None:
    """A one-line token still cannot smuggle controls into disposable YAML."""
    token_file = _token_file(tmp_path, value)

    with pytest.raises(ValueError, match="token file"):
        fly_pilot_deploy._read_token_file(token_file)


@pytest.mark.parametrize(
    "value",
    [
        "synthetic-org-token-canary",
        "FlyV2 payload",
        "FlyV1 ",
        "FlyV1  payload",
        "FlyV1\tpayload",
        "FlyV1 payload suffix",
        "FlyV1 payload ",
        "FlyV1 payload:unsafe",
        "FlyV1 payload#unsafe",
        'FlyV1 payload"unsafe',
        "FlyV1 payload\nsuffix",
        "FlyV1 payload\n\n",
    ],
)
def test_token_reader_rejects_near_miss_fly_token_shapes(
    tmp_path: Path,
    value: str,
) -> None:
    """Only Fly's exact one-separator, one-line token grammar is admitted."""
    token_file = _token_file(tmp_path, value)

    with pytest.raises(ValueError, match="token file"):
        fly_pilot_deploy._read_token_file(token_file)


def test_deploy_refuses_wrong_org_id_before_mutation_and_cleans_home(
    tmp_path: Path,
) -> None:
    """Slug collision or wrong provider identity cannot reach fly deploy."""
    authorization, digest = _authorization(tmp_path)
    token_file = _token_file(tmp_path)
    commands: list[list[str]] = []
    homes: list[Path] = []

    def wrong_org(command: list[str], environ: dict[str, str]) -> CommandResult:
        commands.append(command)
        homes.append(Path(environ["HOME"]))
        return CommandResult(
            json.dumps({"slug": "creek-vaults", "id": "wrong-private-id"}).encode()
        )

    with pytest.raises(RuntimeError, match="organization attestation") as caught:
        deploy(
            _TEMPLATE,
            _coordinates(),
            token_file,
            authorization,
            digest,
            run=wrong_org,
            now=_NOW,
        )

    assert len(commands) == 1
    assert "deploy" not in commands[0]
    assert "wrong-private-id" not in str(caught.value)
    assert homes and all(not path.exists() for path in homes)


def test_deploy_cleans_isolated_home_when_flyctl_cannot_start(tmp_path: Path) -> None:
    """Provider process startup failure cannot retain the short-lived token."""
    authorization, digest = _authorization(tmp_path)
    token_file = _token_file(tmp_path)
    homes: list[Path] = []

    def fail(_command: list[str], environ: dict[str, str]) -> CommandResult:
        homes.append(Path(environ["HOME"]))
        raise OSError("private-provider-detail-canary")

    with pytest.raises(RuntimeError, match="provider command failed") as caught:
        deploy(
            _TEMPLATE,
            _coordinates(),
            token_file,
            authorization,
            digest,
            run=fail,
            now=_NOW,
        )

    assert "private-provider-detail-canary" not in str(caught.value)
    assert _TOKEN not in str(caught.value)
    assert homes and all(not path.exists() for path in homes)


@pytest.mark.parametrize(
    ("contents", "expected_hash", "message"),
    [
        (b"not-json", None, "invalid"),
        (b"[]", None, "invalid shape"),
        (b"{}", None, "invalid shape"),
        (b"{}", "sha256:" + "0" * 64, "hash does not match"),
    ],
)
def test_authorization_rejects_unparseable_or_unbound_documents(
    tmp_path: Path,
    contents: bytes,
    expected_hash: str | None,
    message: str,
) -> None:
    """Malformed private approvals fail before any coordinate is accepted."""
    path = tmp_path / "authorization"
    path.write_bytes(contents)
    digest = expected_hash or "sha256:" + hashlib.sha256(contents).hexdigest()

    with pytest.raises(ValueError, match=message):
        fly_pilot_deploy.load_authorization(path, digest, _coordinates(), now=_NOW)


def test_authorization_refuses_unreadable_path_without_disclosing_it(
    tmp_path: Path,
) -> None:
    """A missing private approval yields a generic content-free refusal."""
    path = tmp_path / "private-owner-identity-canary"

    with pytest.raises(ValueError, match="unreadable") as caught:
        fly_pilot_deploy.load_authorization(
            path,
            "sha256:" + "0" * 64,
            _coordinates(),
            now=_NOW,
        )

    assert path.name not in str(caught.value)


@pytest.mark.parametrize(
    "organization",
    [
        "creek-vaults",
        {"Slug": "creek-vaults", "Name": "Creek Vaults"},
        {"slug": "creek-vaults", "name": "Creek Vaults"},
    ],
)
def test_app_membership_attestation_accepts_fly_org_shapes(
    organization: object,
) -> None:
    """Current nested Fly org objects and the legacy scalar both attest."""
    fly_pilot_deploy._verify_app_membership(
        [{"Name": "creek-control-pilot", "Organization": organization}],
        _coordinates(),
    )


@pytest.mark.parametrize(
    "document",
    [
        None,
        [],
        [{"name": "creek-control-pilot", "organization": "other-org"}],
        [
            {
                "Name": "creek-control-pilot",
                "Organization": {"Slug": "other-org"},
            }
        ],
        [
            {
                "Name": "creek-control-pilot",
                "Organization": {"Name": "Creek Vaults"},
            }
        ],
        [
            {"name": "creek-control-pilot", "organization": "creek-vaults"},
            {"Name": "creek-control-pilot", "Organization": "creek-vaults"},
        ],
    ],
)
def test_app_membership_attestation_requires_one_exact_org_match(
    document: object,
) -> None:
    """Missing, malformed, cross-org, or duplicate app membership is refused."""
    with pytest.raises(RuntimeError, match="membership attestation"):
        fly_pilot_deploy._verify_app_membership(document, _coordinates())


@pytest.mark.parametrize(
    ("machines", "volumes", "message"),
    [
        ([], [{}], "Machine count"),
        ([{"id": "machine"}], [], "volume count"),
        ([{"id": 7}], [{}], "inventory is invalid"),
        ([{"id": "machine"}], ["volume"], "inventory is invalid"),
    ],
)
def test_post_state_cardinality_rejects_malformed_provider_inventory(
    machines: object,
    volumes: object,
    message: str,
) -> None:
    """Only one typed Machine and volume can cross the post-deploy gate."""
    with pytest.raises(RuntimeError, match=message):
        fly_pilot_deploy._cardinality(machines, volumes)


def test_provider_json_and_process_failures_are_content_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Neither invalid provider output nor process stderr escapes the boundary."""
    with pytest.raises(RuntimeError, match="invalid JSON") as invalid:
        fly_pilot_deploy._run_fly_json(
            lambda _command: CommandResult(b"private-invalid-json-canary"),
            ["fly", "orgs", "show"],
        )
    assert "private-invalid-json-canary" not in str(invalid.value)

    class Process:
        returncode = 1

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"private-stdout-canary", b"private-stderr-canary"

    async def create(*_args: object, **_kwargs: object) -> Process:
        return Process()

    monkeypatch.setattr(
        fly_pilot_deploy.asyncio,
        "create_subprocess_exec",
        create,
    )
    with pytest.raises(RuntimeError, match="provider command failed") as failed:
        fly_pilot_deploy._run_command(["fly", "version"], {})
    assert "private" not in str(failed.value)


def test_provider_process_success_returns_only_stdout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The runner captures machine-readable stdout and discards provider stderr."""

    class Process:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return b'{"safe":true}', b"private-stderr-canary"

    observed: dict[str, Any] = {}

    async def create(*args: object, **kwargs: object) -> Process:
        observed["args"] = args
        observed["environment"] = kwargs["env"]
        observed["stdin"] = kwargs["stdin"]
        return Process()

    monkeypatch.setattr(
        fly_pilot_deploy.asyncio,
        "create_subprocess_exec",
        create,
    )

    result = fly_pilot_deploy._run_command(
        ["fly", "version"],
        {"HOME": "/synthetic/isolated-home"},
    )

    assert result.stdout == b'{"safe":true}'
    assert observed == {
        "args": ("fly", "version"),
        "environment": {"HOME": "/synthetic/isolated-home"},
        "stdin": fly_pilot_deploy.asyncio.subprocess.DEVNULL,
    }


def test_cli_render_is_offline_and_deploy_requires_all_private_inputs(
    tmp_path: Path,
) -> None:
    """Render cannot mutate Fly and deploy cannot infer credential inputs."""
    output = tmp_path / "fly.toml"
    common = [
        "--template",
        str(_TEMPLATE),
        "--app",
        _coordinates().app,
        "--region",
        _coordinates().region,
        "--organization",
        _coordinates().organization,
        "--vault-image",
        _coordinates().vault_image,
        "--control-image",
        _coordinates().control_image,
        "--token-expires-at",
        _coordinates().token_expires_at,
        "--handoff-url",
        _coordinates().handoff_url,
        "--alert-url",
        _coordinates().alert_url,
        "--volume",
        _coordinates().volume,
    ]

    fly_pilot_deploy.main(["render", *common, "--output", str(output)])

    assert tomllib.loads(output.read_text(encoding="utf-8"))["app"] == (
        _coordinates().app
    )
    with pytest.raises(SystemExit):
        fly_pilot_deploy.main(["deploy", *common])
