"""Fly-replay-only bootstrap for one isolated managed vault."""

from __future__ import annotations

import argparse
import os
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

from creek.classify.llm.local_boundary import LOOPBACK_ONLY_ENV
from creek.config import CONFIG_PATH_ENV_VAR
from creek_mcp.container_runtime import (
    ContainerConfigurationError,
    ContainerSettings,
    load_consumer_secret,
    prepare_vault,
)
from creek_mcp.httpapi.app import create_app
from creek_mcp.httpapi.cli import serve
from creek_mcp.provisioning.pilot_control import validate_fly_runtime
from creek_mcp.provisioning.production_secrets import read_owner_only_file
from creek_mcp.provisioning.replay_contract import is_replay_state
from creek_mcp.remote_auth import CONSUMER_TOKENS_ENV, announce_rotation_window

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

REPLAY_STATE_FILE_ENV: Final[str] = "CREEK_CONTAINER_FLY_REPLAY_STATE_FILE"
_DEFAULT_REPLAY_STATE = Path("/run/secrets/creek_replay_state")
_VAULT_PATH_ENV_VAR: Final[str] = "CREEK_VAULT_PATH"


@dataclass(frozen=True, slots=True)
class FlyVaultSettings:
    """Ordinary vault settings plus immutable Fly runtime attestation."""

    container: ContainerSettings
    replay_state_file: Path
    fly_app_name: str
    fly_machine_id: str
    fly_region: str
    fly_private_ip: str

    @classmethod
    def from_environ(cls, environ: Mapping[str, str] | None = None) -> FlyVaultSettings:
        """Load path-only settings and require Fly's injected coordinates."""
        source = os.environ if environ is None else environ
        app_name = source.get("FLY_APP_NAME", "")
        machine_id = source.get("FLY_MACHINE_ID", "")
        region = source.get("FLY_REGION", "")
        private_ip = source.get("FLY_PRIVATE_IP", "")
        expected_app = source.get("CREEK_CONTAINER_EXPECTED_FLY_APP", "")
        expected_region = source.get("CREEK_CONTAINER_EXPECTED_FLY_REGION", "")
        if app_name != expected_app:
            raise ValueError("Fly runtime attestation is invalid")
        validate_fly_runtime(
            app_name,
            machine_id,
            fly_region=region,
            expected_region=expected_region,
            fly_private_ip=private_ip,
        )
        return cls(
            ContainerSettings.from_environ(source),
            Path(source.get(REPLAY_STATE_FILE_ENV, str(_DEFAULT_REPLAY_STATE))),
            app_name,
            machine_id,
            region,
            private_ip,
        )


def _load_replay_state(path: Path) -> str:
    """Read one owner-only exact base64url replay state."""
    try:
        state: str = read_owner_only_file(path).decode("ascii")
    except (UnicodeError, ValueError) as exc:
        raise ContainerConfigurationError("Fly replay state is unreadable") from exc
    if not is_replay_state(state):
        raise ContainerConfigurationError("Fly replay state is invalid")
    return state


@contextmanager
def _runtime_environment(config_path: Path, vault_path: Path) -> Iterator[None]:
    """Install the runtime paths without leaking process state after shutdown."""
    names = (
        CONFIG_PATH_ENV_VAR,
        _VAULT_PATH_ENV_VAR,
        CONSUMER_TOKENS_ENV,
        LOOPBACK_ONLY_ENV,
    )
    previous = {name: os.environ.get(name) for name in names}
    try:
        os.environ[CONFIG_PATH_ENV_VAR] = str(config_path)
        os.environ[_VAULT_PATH_ENV_VAR] = str(vault_path)
        os.environ[LOOPBACK_ONLY_ENV] = "1"
        os.environ.pop(CONSUMER_TOKENS_ENV, None)
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def run(settings: FlyVaultSettings) -> None:
    """Serve plaintext only behind Fly Proxy with replay state plus bearer auth."""
    prepared = prepare_vault(settings.container)
    secret = load_consumer_secret(settings.container.consumer_tokens_file)
    replay_state = _load_replay_state(settings.replay_state_file)
    verifier = secret.verifier()
    args = argparse.Namespace(
        host=settings.container.host,
        port=settings.container.port,
        tls_cert=None,
        tls_key=None,
    )
    with _runtime_environment(prepared.config_path, settings.container.vault_path):
        announce_rotation_window(verifier)
        serve(
            create_app(verifier=verifier, fly_replay_state=replay_state),
            args,
        )


def main() -> None:
    """Load Fly-only settings and exit cleanly on a startup refusal."""
    try:
        run(FlyVaultSettings.from_environ())
    except (ContainerConfigurationError, ValueError) as exc:
        print(f"creek-fly-vault: startup refused: {exc}", file=sys.stderr)
        raise SystemExit(2) from None


if __name__ == "__main__":  # pragma: no cover - Machine init contract.
    main()
