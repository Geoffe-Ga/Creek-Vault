"""Authorization-gated Fly pilot config rendering and bounded deployment."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Final

from creek_mcp.provisioning.fly_pilot_config import (
    FlyPilotCoordinates,
    render_fly_toml,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping, Sequence

_AUTH_SCHEMA: Final[str] = "creek_fly_pilot_deploy_authorization_v1"
_AUTH_KEYS: Final[frozenset[str]] = frozenset(
    {
        "schema",
        "app",
        "organization",
        "organization_id",
        "region",
        "max_spend_usd_cents",
        "approved_until",
        "cross_network_replays_enabled",
    }
)
_HASH_PREFIX: Final[str] = "sha256:"
_MAX_AUTHORIZATION_WINDOW: Final[timedelta] = timedelta(days=7)
_PILOT_SPEND_CAP_USD_CENTS: Final[int] = 2500
_MAX_TOKEN_FILE_BYTES: Final[int] = 4096
_FLY_TOKEN_PREFIX: Final[str] = "FlyV1 "
_FLY_TOKEN_PAYLOAD_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9_+/,=-]+")
_FLY_CHILD_ENV_KEYS: Final[frozenset[str]] = frozenset(
    {"LANG", "LC_ALL", "NO_COLOR", "PATH", "TERM"}
)
_EXPECTED_HEALTH_CHECKS: Final[list[dict[str, object]]] = [
    {
        "grace_period": "30s",
        "interval": "10s",
        "method": "GET",
        "path": "/__fly/health",
        "timeout": "2s",
        "type": "http",
    }
]
_EXPECTED_CONCURRENCY: Final[dict[str, object]] = {
    "hard_limit": 25,
    "soft_limit": 20,
    "type": "requests",
}


@dataclass(frozen=True, slots=True)
class DeployAuthorization:
    """Private owner approval bound to exact non-secret provider coordinates."""

    app: str
    organization: str
    organization_id: str = field(repr=False)
    region: str
    max_spend_usd_cents: int
    approved_until: datetime


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Only the captured bytes required by post-deploy verification."""

    stdout: bytes


def load_authorization(
    path: Path,
    expected_hash: str,
    coordinates: FlyPilotCoordinates,
    *,
    now: datetime | None = None,
) -> DeployAuthorization:
    """Validate one closed authorization without publishing its file path."""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ValueError("Fly pilot authorization is unreadable") from exc
    observed_hash = _HASH_PREFIX + hashlib.sha256(raw).hexdigest()
    if observed_hash != expected_hash:
        raise ValueError("Fly pilot authorization hash does not match")
    try:
        document = json.loads(raw)
    except (UnicodeError, ValueError) as exc:
        raise ValueError("Fly pilot authorization is invalid") from exc
    if not isinstance(document, dict) or set(document) != _AUTH_KEYS:
        raise ValueError("Fly pilot authorization has an invalid shape")
    try:
        approved_until = datetime.fromisoformat(document["approved_until"])
        token_expires_at = datetime.fromisoformat(coordinates.token_expires_at)
    except (TypeError, ValueError) as exc:
        raise ValueError("Fly pilot authorization expiry is invalid") from exc
    observed_at = now or datetime.now(tz=UTC)
    if (
        document["schema"] != _AUTH_SCHEMA
        or document["app"] != coordinates.app
        or document["organization"] != coordinates.organization
        or not isinstance(document["organization_id"], str)
        or not document["organization_id"]
        or document["region"] != coordinates.region
        or type(document["max_spend_usd_cents"]) is not int
        or document["max_spend_usd_cents"] != _PILOT_SPEND_CAP_USD_CENTS
        or document["cross_network_replays_enabled"] is not True
        or approved_until.tzinfo is None
        or token_expires_at.tzinfo is None
        or approved_until <= observed_at
        or approved_until > observed_at + _MAX_AUTHORIZATION_WINDOW
        or token_expires_at <= observed_at
        or token_expires_at > observed_at + _MAX_AUTHORIZATION_WINDOW
        or approved_until > token_expires_at
    ):
        raise ValueError("Fly pilot authorization is not active for these coordinates")
    return DeployAuthorization(
        coordinates.app,
        coordinates.organization,
        document["organization_id"],
        coordinates.region,
        document["max_spend_usd_cents"],
        approved_until,
    )


def deploy(
    template: Path,
    coordinates: FlyPilotCoordinates,
    token_file: Path,
    authorization: Path,
    authorization_hash: str,
    *,
    run: Callable[[list[str], dict[str, str]], CommandResult] | None = None,
    now: datetime | None = None,
) -> None:
    """Deploy one Machine with ``--ha=false`` then prove 1 Machine/1 volume."""
    approval = load_authorization(
        authorization,
        authorization_hash,
        coordinates,
        now=now,
    )
    rendered = render_fly_toml(template, coordinates)
    runner = run or _run_command
    with _isolated_fly_environment(token_file) as environment:

        def execute(command: list[str]) -> CommandResult:
            return runner(command, environment)

        organization = _run_fly_json(
            execute,
            ["fly", "orgs", "show", coordinates.organization, "--json"],
        )
        _verify_organization(organization, approval)
        apps = _run_fly_json(
            execute,
            [
                "fly",
                "apps",
                "list",
                "--org",
                coordinates.organization,
                "--json",
            ],
        )
        _verify_app_membership(apps, coordinates)
        _verify_preflight_state(execute, coordinates)
        with tempfile.TemporaryDirectory(prefix="creek-fly-pilot-") as temporary:
            config = Path(temporary) / "fly.toml"
            config.write_text(rendered, encoding="utf-8")
            _run_fly(
                execute,
                [
                    "fly",
                    "deploy",
                    "--ha=false",
                    "--yes",
                    "--app",
                    coordinates.app,
                    "--config",
                    str(config),
                    "--image",
                    coordinates.control_image,
                ],
            )
            machines = _run_fly_json(
                execute,
                ["fly", "machine", "list", "--app", coordinates.app, "--json"],
            )
            volumes = _run_fly_json(
                execute,
                ["fly", "volumes", "list", "--app", coordinates.app, "--json"],
            )
            machine_id, machine, volume = _cardinality(machines, volumes)
            _verify_post_state(machine, volume, machine_id, coordinates)


def _verify_preflight_state(
    run: Callable[[list[str]], CommandResult],
    coordinates: FlyPilotCoordinates,
) -> None:
    """Reject duplicate, partial, or drifted resources before any mutation."""
    machines = _run_fly_json(
        run,
        ["fly", "machine", "list", "--app", coordinates.app, "--json"],
    )
    volumes = _run_fly_json(
        run,
        ["fly", "volumes", "list", "--app", coordinates.app, "--json"],
    )
    if machines == [] and volumes == []:
        return
    if machines == []:
        _verify_precreated_volume(volumes, coordinates)
        return
    machine_id, machine, volume = _cardinality(machines, volumes)
    _verify_machine_state(
        machine,
        volume,
        machine_id,
        coordinates,
        admitted_states=frozenset({"started", "stopped", "suspended"}),
        phase="preflight",
    )


def _verify_precreated_volume(
    volumes: object,
    coordinates: FlyPilotCoordinates,
) -> None:
    """Admit the documented one exact, encrypted, unattached volume state."""
    if not isinstance(volumes, list) or len(volumes) != 1:
        raise RuntimeError("Fly pilot preflight volume count is not one")
    volume = volumes[0]
    if not isinstance(volume, dict):
        raise RuntimeError("Fly pilot preflight volume inventory is invalid")
    name = volume.get("name", volume.get("Name"))
    region = volume.get("region", volume.get("Region"))
    encrypted = volume.get("encrypted", volume.get("Encrypted"))
    attached = volume.get(
        "attached_machine_id",
        volume.get("AttachedMachineID", volume.get("AttachedMachineId")),
    )
    if (
        name != coordinates.volume
        or region != coordinates.region
        or encrypted is not True
        or attached not in {None, ""}
    ):
        raise RuntimeError("Fly pilot preflight volume drifted from reviewed policy")


def _verify_organization(
    document: object,
    approval: DeployAuthorization,
) -> None:
    """Bind flyctl's active credential to the exact privately approved org."""
    if not isinstance(document, dict):
        raise RuntimeError("Fly pilot organization attestation failed")
    slug = document.get("slug", document.get("Slug"))
    identifier = document.get("id", document.get("ID"))
    if slug != approval.organization or identifier != approval.organization_id:
        raise RuntimeError("Fly pilot organization attestation failed")


def _verify_app_membership(
    document: object,
    coordinates: FlyPilotCoordinates,
) -> None:
    """Require the target app exactly once in flyctl's org-filtered inventory."""
    if not isinstance(document, list):
        raise RuntimeError("Fly pilot app membership attestation failed")
    matches = [
        item
        for item in document
        if isinstance(item, dict)
        and item.get("Name", item.get("name")) == coordinates.app
        and _app_organization_slug(item.get("Organization", item.get("organization")))
        == coordinates.organization
    ]
    if len(matches) != 1:
        raise RuntimeError("Fly pilot app membership attestation failed")


def _app_organization_slug(value: object) -> object:
    """Return Fly's org slug from its legacy scalar or current object shape."""
    if isinstance(value, dict):
        return value.get("Slug", value.get("slug"))
    return value


@contextmanager
def _isolated_fly_environment(token_file: Path) -> Iterator[dict[str, str]]:
    """Yield a disposable flyctl HOME containing only the approved org token."""
    token = _read_token_file(token_file)
    with tempfile.TemporaryDirectory(prefix="creek-fly-home-") as temporary:
        home = Path(temporary)
        home.chmod(0o700)
        fly_directory = home / ".fly"
        fly_directory.mkdir(mode=0o700)
        config = fly_directory / "config.yml"
        descriptor = os.open(config, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            # flyctl treats credentials loaded from its config as an interactive
            # session unless a recent timestamp accompanies them.  This timestamp
            # records the start of this isolated, one-command session; the scoped
            # token's own caveats remain the authority and expiry boundary.
            session_started_at = datetime.now(UTC).isoformat()
            value = (
                f"access_token: {token}\nlast_login: {session_started_at}\n"
            ).encode()
            os.write(descriptor, value)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        environment = {
            key: value
            for key, value in os.environ.items()
            if key in _FLY_CHILD_ENV_KEYS
        }
        environment.setdefault("PATH", os.defpath)
        environment["HOME"] = str(home)
        yield environment


def _read_token_file(path: Path) -> str:
    """Read one bounded, owner-only token without following a symlink."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ValueError("Fly pilot token file is invalid") from exc
    try:
        metadata = os.fstat(descriptor)
        raw = os.read(descriptor, _MAX_TOKEN_FILE_BYTES + 1)
    finally:
        os.close(descriptor)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) not in {0o400, 0o600}
        or not raw
        or len(raw) > _MAX_TOKEN_FILE_BYTES
    ):
        raise ValueError("Fly pilot token file is invalid")
    try:
        token = raw.decode("ascii")
    except UnicodeError as exc:
        raise ValueError("Fly pilot token file is invalid") from exc
    if token.endswith("\n"):
        token = token[:-1]
    if not token.startswith(_FLY_TOKEN_PREFIX):
        raise ValueError("Fly pilot token file is invalid")
    payload = token[len(_FLY_TOKEN_PREFIX) :]
    if _FLY_TOKEN_PAYLOAD_RE.fullmatch(payload) is None:
        raise ValueError("Fly pilot token file is invalid")
    return token


def _cardinality(
    machines: object,
    volumes: object,
) -> tuple[str, dict[str, object], dict[str, object]]:
    if not isinstance(machines, list) or len(machines) != 1:
        raise RuntimeError("Fly pilot post-state Machine count is not one")
    if not isinstance(volumes, list) or len(volumes) != 1:
        raise RuntimeError("Fly pilot post-state volume count is not one")
    machine = machines[0]
    volume = volumes[0]
    if (
        not isinstance(machine, dict)
        or not isinstance(machine.get("id"), str)
        or not isinstance(volume, dict)
    ):
        raise RuntimeError("Fly pilot post-state inventory is invalid")
    return machine["id"], machine, volume


def _verify_post_state(
    machine: object,
    volume: dict[str, object],
    machine_id: str,
    coordinates: FlyPilotCoordinates,
) -> None:
    """Verify exact region, digest, service, guest, restart, and encrypted mount."""
    _verify_machine_state(
        machine,
        volume,
        machine_id,
        coordinates,
        admitted_states=frozenset({"started"}),
        phase="post-state",
    )


def _verify_machine_state(
    machine: object,
    volume: dict[str, object],
    machine_id: str,
    coordinates: FlyPilotCoordinates,
    *,
    admitted_states: frozenset[str],
    phase: str,
) -> None:
    """Verify one exact control Machine and attached encrypted volume."""
    if not isinstance(machine, dict):
        raise RuntimeError(f"Fly pilot {phase} Machine inspection is invalid")
    config = machine.get("config")
    image_ref = machine.get("image_ref")
    services = config.get("services") if isinstance(config, dict) else None
    mounts = config.get("mounts") if isinstance(config, dict) else None
    guest = config.get("guest") if isinstance(config, dict) else None
    restart = config.get("restart") if isinstance(config, dict) else None
    volume_id = volume.get("id")
    expected_digest = coordinates.control_image.rsplit("@", 1)[1]
    if (
        machine.get("id") != machine_id
        or machine.get("region") != coordinates.region
        or machine.get("state") not in admitted_states
        or not isinstance(image_ref, dict)
        or image_ref.get("digest") != expected_digest
        or not isinstance(config, dict)
        or config.get("image") != coordinates.control_image
        or guest != {"cpu_kind": "shared", "cpus": 1, "memory_mb": 1024}
        or restart != {"policy": "on-failure", "max_retries": 3}
        or not isinstance(volume_id, str)
        or not _mount_is_exact(mounts, volume_id, coordinates.volume)
        or not _service_is_exact(services)
        or volume.get("name", volume.get("Name")) != coordinates.volume
        or volume.get("region") != coordinates.region
        or volume.get("encrypted") is not True
        or volume.get("attached_machine_id") != machine_id
    ):
        raise RuntimeError(f"Fly pilot {phase} drifted from reviewed policy")


def _mount_is_exact(
    mounts: object,
    volume_id: object,
    volume_name: str,
) -> bool:
    """Accept only Fly's current enriched list mount shape exactly."""
    if not isinstance(volume_id, str) or not isinstance(mounts, list):
        return False
    if len(mounts) != 1 or not isinstance(mounts[0], dict):
        return False
    mount = mounts[0]
    if set(mount) != {"volume", "path", "encrypted", "name", "size_gb"}:
        return False
    return (
        mount.get("volume") == volume_id
        and mount.get("path") == "/data"
        and mount.get("encrypted") is True
        and mount.get("name") == volume_name
        and mount.get("size_gb") == 1
    )


def _service_is_exact(services: object) -> bool:
    if not isinstance(services, list) or len(services) != 1:
        return False
    service = services[0]
    if not isinstance(service, dict):
        return False
    if set(service) != {
        "protocol",
        "internal_port",
        "ports",
        "autostart",
        "autostop",
        "checks",
        "concurrency",
        "force_instance_key",
        "min_machines_running",
    }:
        return False
    ports = service.get("ports")
    if not isinstance(ports, list) or len(ports) != 2:
        return False
    first, second = ports
    if not isinstance(first, dict) or not isinstance(second, dict):
        return False
    if set(first) != {"port", "handlers", "force_https"} or set(second) != {
        "port",
        "handlers",
    }:
        return False
    return (
        service.get("protocol") == "tcp"
        and service.get("internal_port") == 8080
        and first.get("port") == 80
        and first.get("handlers") == ["http"]
        and first.get("force_https") is True
        and second.get("port") == 443
        and second.get("handlers") in (["tls", "http"], ["http", "tls"])
        and service.get("autostart") is True
        and service.get("autostop") is False
        and service.get("checks") == _EXPECTED_HEALTH_CHECKS
        and service.get("concurrency") == _EXPECTED_CONCURRENCY
        and service.get("force_instance_key") is None
        and service.get("min_machines_running") == 1
    )


def _run_fly(
    run: Callable[[list[str]], CommandResult],
    command: list[str],
) -> CommandResult:
    try:
        return run(command)
    except OSError as exc:
        raise RuntimeError("Fly pilot provider command failed") from exc


def _run_fly_json(
    run: Callable[[list[str]], CommandResult],
    command: list[str],
) -> object:
    completed = _run_fly(run, command)
    try:
        return json.loads(completed.stdout)
    except (UnicodeError, ValueError) as exc:
        raise RuntimeError("Fly pilot provider returned invalid JSON") from exc


def _run_command(
    command: list[str],
    environment: Mapping[str, str],
) -> CommandResult:
    """Run one fixed-argv Fly command with both provider streams contained."""
    return asyncio.run(_run_command_async(command, environment))


async def _run_command_async(
    command: list[str],
    environment: Mapping[str, str],
) -> CommandResult:
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=environment,
        )
        stdout, _stderr = await process.communicate()
    except OSError as exc:
        raise RuntimeError("Fly pilot provider command failed") from exc
    if process.returncode != 0:
        raise RuntimeError("Fly pilot provider command failed")
    return CommandResult(stdout)


def build_parser() -> argparse.ArgumentParser:
    """Return an explicit render/deploy CLI with no implicit live mode."""
    parser = argparse.ArgumentParser(prog="creek-fly-pilot-deploy")
    parser.add_argument("command", choices=("render", "deploy"))
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument("--app", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--organization", required=True)
    parser.add_argument("--vault-image", required=True)
    parser.add_argument("--control-image", required=True)
    parser.add_argument("--token-expires-at", required=True)
    parser.add_argument("--handoff-url", required=True)
    parser.add_argument("--alert-url", required=True)
    parser.add_argument("--volume", required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--authorization-file", type=Path)
    parser.add_argument("--authorization-sha256")
    parser.add_argument("--token-file", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    """Render offline by default; deploy only with a hash-bound approval file."""
    parser = build_parser()
    args = parser.parse_args(argv)
    coordinates = FlyPilotCoordinates(
        args.app,
        args.region,
        args.organization,
        args.vault_image,
        args.control_image,
        args.token_expires_at,
        args.handoff_url,
        args.alert_url,
        args.volume,
    )
    if args.command == "render":
        if args.output is None:
            parser.error("--output is required for render")
        args.output.write_text(
            render_fly_toml(args.template, coordinates),
            encoding="utf-8",
        )
        return
    if (
        args.authorization_file is None
        or args.authorization_sha256 is None
        or args.token_file is None
    ):
        parser.error(
            "deploy requires --token-file, --authorization-file, "
            "and --authorization-sha256"
        )
    deploy(
        args.template,
        coordinates,
        args.token_file,
        args.authorization_file,
        args.authorization_sha256,
    )
    print("Fly pilot deployment verified machine_count=1 volume_count=1")


if __name__ == "__main__":
    main(sys.argv[1:])
