"""Fail-closed bootstrap for the one-vault Creek container (#1772).

The image is disposable; the mounted vault is not.  This module is the only
container entry point so that first-boot, mount, identity, and TLS checks happen
before the HTTP server can bind.  Consumer tokens are parsed directly from a
secret file into the verifier and are never copied into the process environment
or command line.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass, field, replace
from enum import StrEnum
from ipaddress import IPv4Address
from pathlib import Path
from typing import TYPE_CHECKING, Final

import yaml

from creek.config import (
    CONFIG_PATH_ENV_VAR,
    VAULT_CONFIG_RELPATH,
    CreekConfig,
    generate_default_config,
)
from creek.scaffold import deploy_canonical
from creek_mcp.httpapi.app import create_app
from creek_mcp.httpapi.cli import DEFAULT_API_PORT, serve
from creek_mcp.remote_auth import (
    CONSUMER_TOKENS_ENV,
    ConsumerTokenVerifier,
    announce_rotation_window,
    load_consumer_tokens,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

VAULT_PATH_ENV: Final[str] = "CREEK_CONTAINER_VAULT_PATH"
"""Non-secret environment setting naming the durable volume mount."""

CONFIG_FILE_ENV: Final[str] = "CREEK_CONTAINER_CONFIG_FILE"
"""Non-secret environment setting naming an optional read-only config mount."""

CONSUMER_TOKENS_FILE_ENV: Final[str] = "CREEK_CONTAINER_CONSUMER_TOKENS_FILE"
"""Path to the single-consumer registry secret, never the registry itself."""

TLS_CERT_FILE_ENV: Final[str] = "CREEK_CONTAINER_TLS_CERT_FILE"
"""Path to the mounted TLS certificate."""

TLS_KEY_FILE_ENV: Final[str] = "CREEK_CONTAINER_TLS_KEY_FILE"
"""Path to the mounted TLS private key."""

MOUNTINFO_FILE_ENV: Final[str] = "CREEK_CONTAINER_MOUNTINFO_FILE"
"""Linux mount table path; configurable only to make the predicate testable."""

PORT_ENV: Final[str] = "CREEK_CONTAINER_PORT"
"""Non-secret API port setting."""

_DEFAULT_VAULT = Path("/vault")
_DEFAULT_SECRET = Path("/run/secrets/creek_consumer_tokens")
_DEFAULT_TLS_CERT = Path("/run/secrets/tls.crt")
_DEFAULT_TLS_KEY = Path("/run/secrets/tls.key")
_DEFAULT_MOUNTINFO = Path("/proc/self/mountinfo")
_CONTAINER_HOST: Final[str] = str(IPv4Address(0))
_PROBE_HOST: Final[str] = "127.0.0.1"
_MIN_PORT: Final[int] = 1
_MAX_PORT: Final[int] = 65535


class ContainerConfigurationError(RuntimeError):
    """A startup condition that must be corrected before Creek may bind."""


class BootstrapState(StrEnum):
    """Whether this boot created the vault or only validated it."""

    INITIALIZED = "initialized"
    EXISTING = "existing"


@dataclass(frozen=True)
class PreparedVault:
    """Validated container storage ready for the application server."""

    config_path: Path
    state: BootstrapState


@dataclass(frozen=True)
class SingleConsumerSecret:
    """One identity and its currently valid rotation tokens.

    The token tuple is excluded from ``repr`` so an exception, debugger, or
    structured log cannot accidentally serialize credential material.
    """

    consumer: str
    tokens: tuple[str, ...] = field(repr=False)

    def verifier(self) -> ConsumerTokenVerifier:
        """Build the shared constant-time verifier over this identity."""
        return ConsumerTokenVerifier({self.consumer: self.tokens})


@dataclass(frozen=True)
class ContainerNetwork:
    """Non-secret listener and probe coordinates for the container."""

    host: str = _CONTAINER_HOST
    probe_host: str = _PROBE_HOST
    port: int = DEFAULT_API_PORT


@dataclass(frozen=True)
class ContainerSettings:
    """Mounted paths and network coordinates allowed in configuration."""

    vault_path: Path = _DEFAULT_VAULT
    config_path: Path = _DEFAULT_VAULT / VAULT_CONFIG_RELPATH
    consumer_tokens_file: Path = _DEFAULT_SECRET
    tls_cert_file: Path = _DEFAULT_TLS_CERT
    tls_key_file: Path = _DEFAULT_TLS_KEY
    mountinfo_file: Path = _DEFAULT_MOUNTINFO
    network: ContainerNetwork = field(default_factory=ContainerNetwork)

    @property
    def host(self) -> str:
        """Return the API bind host."""
        return self.network.host

    @property
    def probe_host(self) -> str:
        """Return the loopback host used by health probes."""
        return self.network.probe_host

    @property
    def port(self) -> int:
        """Return the API listener port."""
        return self.network.port

    @classmethod
    def from_environ(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> ContainerSettings:
        """Load path-only settings and reject an environment bearer secret."""
        source = os.environ if environ is None else environ
        if source.get(CONSUMER_TOKENS_ENV, "").strip():
            raise ContainerConfigurationError(
                "consumer credentials must use the mounted secret file, "
                "not an environment value"
            )
        vault = Path(source.get(VAULT_PATH_ENV, str(_DEFAULT_VAULT)))
        config_value = source.get(CONFIG_FILE_ENV)
        config = Path(config_value) if config_value else vault / VAULT_CONFIG_RELPATH
        port = _parse_port(source.get(PORT_ENV, str(DEFAULT_API_PORT)))
        return cls(
            vault_path=vault,
            config_path=config,
            consumer_tokens_file=Path(
                source.get(CONSUMER_TOKENS_FILE_ENV, str(_DEFAULT_SECRET))
            ),
            tls_cert_file=Path(source.get(TLS_CERT_FILE_ENV, str(_DEFAULT_TLS_CERT))),
            tls_key_file=Path(source.get(TLS_KEY_FILE_ENV, str(_DEFAULT_TLS_KEY))),
            mountinfo_file=Path(
                source.get(MOUNTINFO_FILE_ENV, str(_DEFAULT_MOUNTINFO))
            ),
            network=ContainerNetwork(port=port),
        )

    def with_config_path(self, path: Path) -> ContainerSettings:
        """Return a copy using *path*, primarily for explicit-mount tests."""
        return replace(self, config_path=path)


def _parse_port(raw: str) -> int:
    """Return a valid TCP port or raise a configuration refusal."""
    try:
        port = int(raw)
    except ValueError as exc:
        raise ContainerConfigurationError("container port must be an integer") from exc
    if not _MIN_PORT <= port <= _MAX_PORT:
        raise ContainerConfigurationError("container port must be between 1 and 65535")
    return port


_MOUNT_ESCAPES: Final[dict[str, str]] = {
    r"\011": "\t",
    r"\012": "\n",
    r"\040": " ",
    r"\134": "\\",
}


def _decode_mount_path(raw: str) -> str:
    """Decode the four octal escapes permitted in Linux mountinfo paths."""
    decoded = raw
    for escaped, literal in _MOUNT_ESCAPES.items():
        decoded = decoded.replace(escaped, literal)
    return decoded


def is_mounted_volume(path: Path, mountinfo_file: Path) -> bool:
    """Return whether *path* is an exact mount point in Linux mountinfo."""
    try:
        rows = mountinfo_file.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ContainerConfigurationError(
            "Linux mount metadata is unavailable"
        ) from exc
    target = str(path.resolve())
    return any(
        len(fields) >= 5 and _decode_mount_path(fields[4]) == target
        for row in rows
        if (fields := row.split())
    )


def _canonical_config_path(vault: Path) -> Path:
    """Return the first-boot config location inside *vault*."""
    return vault / VAULT_CONFIG_RELPATH


def _validated_config(config_path: Path, vault: Path) -> None:
    """Validate config bytes without allowing environment overrides."""
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        config = CreekConfig.model_validate(raw)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise ContainerConfigurationError(
            "mounted Creek config is unreadable or invalid"
        ) from exc
    if config.vault_path.resolve() != vault.resolve():
        raise ContainerConfigurationError(
            "configured vault_path must equal the mounted persistent volume"
        )


def prepare_vault(settings: ContainerSettings) -> PreparedVault:
    """Validate or initialize exactly one durable vault without overwriting it."""
    vault = settings.vault_path
    if not vault.is_dir() or not is_mounted_volume(vault, settings.mountinfo_file):
        raise ContainerConfigurationError(
            "vault path must be an explicitly mounted volume"
        )
    config_path = settings.config_path
    canonical = _canonical_config_path(vault)
    if config_path.is_file():
        _validated_config(config_path, vault)
        return PreparedVault(config_path, BootstrapState.EXISTING)
    if config_path.resolve() != canonical.resolve():
        raise ContainerConfigurationError("explicit config mount is missing")
    if any(vault.iterdir()):
        raise ContainerConfigurationError(
            "mounted volume is nonempty but missing config; refusing to overwrite it"
        )
    deploy_canonical(vault)
    generate_default_config(config_path, vault_path=vault.resolve())
    _validated_config(config_path, vault)
    return PreparedVault(config_path, BootstrapState.INITIALIZED)


def load_consumer_secret(path: Path) -> SingleConsumerSecret:
    """Read and validate exactly one consumer registry from a secret mount."""
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ContainerConfigurationError(
            "consumer secret mount is missing or unreadable"
        ) from exc
    try:
        parsed = load_consumer_tokens({CONSUMER_TOKENS_ENV: raw})
    except ValueError as exc:
        raise ContainerConfigurationError("consumer secret is invalid") from exc
    if len(parsed) != 1:
        raise ContainerConfigurationError(
            "consumer secret must configure exactly one identity"
        )
    consumer, tokens = next(iter(parsed.items()))
    return SingleConsumerSecret(consumer, tokens)


def _require_tls_file(path: Path, label: str) -> None:
    """Refuse a missing TLS file without opening or logging its contents."""
    if not path.is_file():
        raise ContainerConfigurationError(f"mounted TLS {label} file is missing")


def _server_args(settings: ContainerSettings, config_path: Path) -> argparse.Namespace:
    """Build the existing API server's non-secret argument namespace."""
    return argparse.Namespace(
        host=settings.host,
        port=settings.port,
        config=config_path,
        tls_cert=settings.tls_cert_file,
        tls_key=settings.tls_key_file,
        print_openapi=False,
    )


def run(settings: ContainerSettings) -> None:
    """Clear every startup gate, then serve the authenticated one-vault API."""
    prepared = prepare_vault(settings)
    _require_tls_file(settings.tls_cert_file, "certificate")
    _require_tls_file(settings.tls_key_file, "private-key")
    secret = load_consumer_secret(settings.consumer_tokens_file)
    verifier = secret.verifier()
    os.environ[CONFIG_PATH_ENV_VAR] = str(prepared.config_path)
    os.environ["CREEK_VAULT_PATH"] = str(settings.vault_path)
    os.environ.pop(CONSUMER_TOKENS_ENV, None)
    announce_rotation_window(verifier)
    serve(create_app(verifier=verifier), _server_args(settings, prepared.config_path))


def main() -> None:
    """Load environment settings and exit cleanly on a startup refusal."""
    try:
        run(ContainerSettings.from_environ())
    except ContainerConfigurationError as exc:
        print(f"creek-container: startup refused: {exc}", file=sys.stderr)
        raise SystemExit(2) from None


if __name__ == "__main__":  # pragma: no cover - exercised by the image fixture.
    main()
