"""Layered health probe for the one-vault Creek container (#1772)."""

from __future__ import annotations

import argparse
import socket
import ssl
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

import httpx

from creek_mcp.container_runtime import (
    ContainerConfigurationError,
    ContainerSettings,
    is_mounted_volume,
    load_consumer_secret,
)

_PROBE_TIMEOUT: Final[float] = 2.0


class ProbeTarget(StrEnum):
    """The deepest runtime layer a caller wants to verify."""

    PROCESS = "process"
    VOLUME = "volume"
    READY = "ready"


class ProbeStatus(StrEnum):
    """Precise outcomes exposed to the container supervisor."""

    PROCESS_UP = "process-up"
    PROCESS_DOWN = "process-down"
    VOLUME_MOUNTED = "vault-mounted"
    VOLUME_UNMOUNTED = "vault-unmounted"
    V1_READY = "v1-ready"
    V1_UNREADY = "v1-unready"


_EXIT_CODES: Final[dict[ProbeStatus, int]] = {
    ProbeStatus.PROCESS_UP: 0,
    ProbeStatus.VOLUME_MOUNTED: 0,
    ProbeStatus.V1_READY: 0,
    ProbeStatus.PROCESS_DOWN: 20,
    ProbeStatus.VOLUME_UNMOUNTED: 21,
    ProbeStatus.V1_UNREADY: 22,
}


@dataclass(frozen=True)
class ProbeResult:
    """A content-free health state and its process exit code."""

    status: ProbeStatus

    @property
    def exit_code(self) -> int:
        """Return the stable supervisor exit code for :attr:`status`."""
        return _EXIT_CODES[self.status]


def _process_is_up(settings: ContainerSettings) -> bool:
    """Return whether the API's loopback TCP socket accepts a connection."""
    try:
        with socket.create_connection(
            (settings.probe_host, settings.port), timeout=_PROBE_TIMEOUT
        ):
            return True
    except OSError:
        return False


def _volume_is_mounted(settings: ContainerSettings) -> bool:
    """Return whether the configured vault remains an exact mount point."""
    try:
        return settings.vault_path.is_dir() and is_mounted_volume(
            settings.vault_path, settings.mountinfo_file
        )
    except ContainerConfigurationError:
        return False


def _v1_is_ready(settings: ContainerSettings) -> bool:
    """Call authenticated ``/v1/health`` using only mounted credentials."""
    try:
        secret = load_consumer_secret(settings.consumer_tokens_file)
        context = ssl.create_default_context(cafile=str(settings.tls_cert_file))
        response = httpx.get(
            f"https://{settings.probe_host}:{settings.port}/v1/health",
            headers={"Authorization": f"Bearer {secret.tokens[0]}"},
            timeout=_PROBE_TIMEOUT,
            verify=context,
        )
        return response.status_code == 200 and response.json() == {"status": "ok"}
    except (ContainerConfigurationError, OSError, ValueError, httpx.HTTPError):
        return False


def probe(settings: ContainerSettings, target: ProbeTarget) -> ProbeResult:
    """Probe *target*, preserving which dependency made readiness fail."""
    if target is ProbeTarget.PROCESS:
        status = (
            ProbeStatus.PROCESS_UP
            if _process_is_up(settings)
            else ProbeStatus.PROCESS_DOWN
        )
        return ProbeResult(status)
    if target is ProbeTarget.VOLUME:
        status = (
            ProbeStatus.VOLUME_MOUNTED
            if _volume_is_mounted(settings)
            else ProbeStatus.VOLUME_UNMOUNTED
        )
        return ProbeResult(status)
    if not _process_is_up(settings):
        return ProbeResult(ProbeStatus.PROCESS_DOWN)
    if not _volume_is_mounted(settings):
        return ProbeResult(ProbeStatus.VOLUME_UNMOUNTED)
    status = ProbeStatus.V1_READY if _v1_is_ready(settings) else ProbeStatus.V1_UNREADY
    return ProbeResult(status)


def _parser() -> argparse.ArgumentParser:
    """Build the small healthcheck argument parser."""
    parser = argparse.ArgumentParser(prog="creek-container-health")
    parser.add_argument(
        "--check",
        choices=tuple(target.value for target in ProbeTarget),
        default=ProbeTarget.READY.value,
        help="deepest runtime layer to probe",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    """Print one content-free state and exit with its stable code."""
    args = _parser().parse_args(argv)
    try:
        settings = ContainerSettings.from_environ()
        result = probe(settings, ProbeTarget(args.check))
    except ContainerConfigurationError:
        result = ProbeResult(ProbeStatus.V1_UNREADY)
    print(result.status.value)
    raise SystemExit(result.exit_code)


if __name__ == "__main__":  # pragma: no cover - exercised by Docker HEALTHCHECK.
    main()
