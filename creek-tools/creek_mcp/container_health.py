"""Layered health probe for the one-vault Creek container (#1772, #1849).

``ready`` is storage readiness: the process, the mounted vault and
authenticated ``/v1``. ``model`` is a separate, fail-closed question — can the
pinned local model generate? — answered without reading the vault or its
config. ``pool`` is the same question for the shared confidential inference
pool a managed vault uses instead (ADR-0016, #1894): is the pinned destination
reachable, and does it report the manifest's runtime and model?

``pool`` makes **no generation**. The local canary is a free loopback call; a
pool canary would be a billed vendor call on every healthcheck, which the
owner's "no spend yet" decision rules out, so pool readiness stops at the
identity listing (ADR-0016 Decision 1 item 9). Attestation of the pinned
measurement set is the attesting client's own cached verification and lands
with that provider; this probe reports the identity half.

The image healthcheck keeps probing ``ready``, so neither a missing model nor
an unreachable pool ever marks the vault API unhealthy: storage-ready implies
neither model-ready nor pool-ready, and ``ready`` never consults the pool.
"""

from __future__ import annotations

import argparse
import os
import socket
import ssl
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final

import httpx

from creek.classify.llm.local_boundary import ollama_get
from creek.classify.llm.pool_boundary import (
    PoolBoundaryError,
    pool_list_models,
    pool_response_ok,
    pool_url_configured,
)
from creek.classify.llm.providers import (
    call_ollama,
    ollama_digest_matches,
    ollama_model_digest,
    pool_identity_matches,
    reported_pool_identity,
)
from creek.config import LLMConfig
from creek_mcp.container_runtime import (
    ContainerConfigurationError,
    ContainerSettings,
    is_mounted_volume,
    load_consumer_secret,
)
from creek_mcp.fly_vault_runtime import REPLAY_STATE_FILE_ENV, _load_replay_state
from creek_mcp.model_package import (
    ModelPackageError,
    ModelPackageManifest,
    configured_manifest,
)

_PROBE_TIMEOUT: Final[float] = 2.0

CANARY_PROMPT: Final[str] = "Reply with the single word: ready"
"""The fixed synthetic generation the model probe asks for; never vault data."""

_CANARY_MAX_TOKENS: Final[int] = 8
_CANARY_TIMEOUT: Final[float] = 20.0
_LOOPBACK_RUNTIME: Final[LLMConfig] = LLMConfig()
"""The in-container runtime endpoint; deliberately not read from vault config."""


class ProbeTarget(StrEnum):
    """The deepest runtime layer a caller wants to verify."""

    PROCESS = "process"
    VOLUME = "volume"
    READY = "ready"
    MODEL = "model"
    POOL = "pool"


class ProbeStatus(StrEnum):
    """Precise outcomes exposed to the container supervisor."""

    PROCESS_UP = "process-up"
    PROCESS_DOWN = "process-down"
    VOLUME_MOUNTED = "vault-mounted"
    VOLUME_UNMOUNTED = "vault-unmounted"
    V1_READY = "v1-ready"
    V1_UNREADY = "v1-unready"
    MODEL_READY = "model-ready"
    RUNTIME_DOWN = "runtime-down"
    MODEL_MISSING = "model-missing"
    MODEL_DIGEST_MISMATCH = "model-digest-mismatch"
    GENERATION_FAILED = "generation-failed"
    POOL_READY = "pool-ready"
    POOL_UNCONFIGURED = "pool-unconfigured"
    POOL_UNREACHABLE = "pool-unreachable"
    POOL_MODEL_MISSING = "pool-model-missing"
    POOL_IDENTITY_MISMATCH = "pool-identity-mismatch"


_EXIT_CODES: Final[dict[ProbeStatus, int]] = {
    ProbeStatus.PROCESS_UP: 0,
    ProbeStatus.VOLUME_MOUNTED: 0,
    ProbeStatus.V1_READY: 0,
    ProbeStatus.PROCESS_DOWN: 20,
    ProbeStatus.VOLUME_UNMOUNTED: 21,
    ProbeStatus.V1_UNREADY: 22,
    ProbeStatus.MODEL_READY: 0,
    ProbeStatus.RUNTIME_DOWN: 23,
    ProbeStatus.MODEL_MISSING: 24,
    ProbeStatus.MODEL_DIGEST_MISMATCH: 25,
    ProbeStatus.GENERATION_FAILED: 26,
    ProbeStatus.POOL_READY: 0,
    ProbeStatus.POOL_UNCONFIGURED: 30,
    ProbeStatus.POOL_UNREACHABLE: 31,
    ProbeStatus.POOL_MODEL_MISSING: 32,
    ProbeStatus.POOL_IDENTITY_MISMATCH: 33,
}

_UNAVAILABLE: Final[dict[ProbeTarget, ProbeStatus]] = {
    ProbeTarget.PROCESS: ProbeStatus.V1_UNREADY,
    ProbeTarget.VOLUME: ProbeStatus.V1_UNREADY,
    ProbeTarget.READY: ProbeStatus.V1_UNREADY,
    ProbeTarget.MODEL: ProbeStatus.MODEL_MISSING,
    ProbeTarget.POOL: ProbeStatus.POOL_UNCONFIGURED,
}
"""Each target's status when the container environment itself is invalid."""


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
        replay_path = os.environ.get(REPLAY_STATE_FILE_ENV)
        if replay_path is None:
            scheme = "https"
            verify: ssl.SSLContext | bool = ssl.create_default_context(
                cafile=str(settings.tls_cert_file)
            )
            replay_headers: dict[str, str] = {}
        else:
            scheme = "http"
            verify = False
            replay_headers = {
                "Fly-Replay-Src": f"state={_load_replay_state(Path(replay_path))}"
            }
        response = httpx.get(
            f"{scheme}://{settings.probe_host}:{settings.port}/v1/health",
            headers={
                "Authorization": f"Bearer {secret.tokens[0]}",
                **replay_headers,
            },
            timeout=_PROBE_TIMEOUT,
            verify=verify,
        )
        return response.status_code == 200 and response.json() == {"status": "ok"}
    except (ContainerConfigurationError, OSError, ValueError, httpx.HTTPError):
        return False


def _inventory_status(manifest: ModelPackageManifest) -> ProbeStatus | None:
    """Check the loopback runtime serves the pinned digest; ``None`` when it does."""
    try:
        response = ollama_get(_LOOPBACK_RUNTIME, "/api/tags", timeout=_PROBE_TIMEOUT)
        payload: object = response.json() if response.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        return ProbeStatus.RUNTIME_DOWN
    if payload is None:
        return ProbeStatus.RUNTIME_DOWN
    served = ollama_model_digest(payload, manifest.model_name)
    if served is None:
        return ProbeStatus.MODEL_MISSING
    if not ollama_digest_matches(served, manifest.runtime_inventory_digest):
        return ProbeStatus.MODEL_DIGEST_MISMATCH
    return None


def _canary_succeeds(manifest: ModelPackageManifest) -> bool:
    """Return whether the pinned model completes :data:`CANARY_PROMPT`."""
    config = _LOOPBACK_RUNTIME.model_copy(update={"model": manifest.model_name})
    try:
        completion = call_ollama(
            config,
            CANARY_PROMPT,
            timeout=_CANARY_TIMEOUT,
            max_tokens=_CANARY_MAX_TOKENS,
        )
    except (httpx.HTTPError, ValueError):
        return False
    return bool(completion.text.strip())


def _model_status() -> ProbeStatus:
    """Return the pinned local model's closed readiness state.

    No manifest, or one that fails to load, is ``model-missing`` without any
    network call: an upgraded storage-only vault stays model-unavailable until
    a verified package is configured. Nothing is logged; the prompt and the
    model's output never leave this function.
    """
    try:
        manifest = configured_manifest()
    except ModelPackageError:
        return ProbeStatus.MODEL_MISSING
    if manifest is None:
        return ProbeStatus.MODEL_MISSING
    inventory = _inventory_status(manifest)
    if inventory is not None:
        return inventory
    if _canary_succeeds(manifest):
        return ProbeStatus.MODEL_READY
    return ProbeStatus.GENERATION_FAILED


def _pool_identity_status(manifest: ModelPackageManifest) -> ProbeStatus:
    """Read the pinned pool's identity listing and grade it; no generation.

    Args:
        manifest: The operator's loaded manifest.

    Returns:
        ``POOL_READY`` when the pool reports the pinned runtime and model at
        the pinned digest; otherwise the precise failure. Nothing is logged
        and no prompt exists to leak.
    """
    try:
        response = pool_list_models(budget=_PROBE_TIMEOUT)
    except PoolBoundaryError:
        return ProbeStatus.POOL_UNREACHABLE
    if not pool_response_ok(response):
        return ProbeStatus.POOL_UNREACHABLE
    if reported_pool_identity(response.payload, manifest.model_name) is None:
        return ProbeStatus.POOL_MODEL_MISSING
    if not pool_identity_matches(
        response.payload,
        model_name=manifest.model_name,
        inventory_digest=manifest.runtime_inventory_digest,
    ):
        return ProbeStatus.POOL_IDENTITY_MISMATCH
    return ProbeStatus.POOL_READY


def _pool_status() -> ProbeStatus:
    """Return the shared inference pool's closed readiness state.

    No manifest, one that fails to load, or no pinned destination in the
    Machine environment is ``pool-unconfigured`` without any network call.
    """
    try:
        manifest = configured_manifest()
    except ModelPackageError:
        return ProbeStatus.POOL_UNCONFIGURED
    if manifest is None or not pool_url_configured():
        return ProbeStatus.POOL_UNCONFIGURED
    return _pool_identity_status(manifest)


def probe(settings: ContainerSettings, target: ProbeTarget) -> ProbeResult:
    """Probe *target*, preserving which dependency made readiness fail."""
    if target is ProbeTarget.POOL:
        return ProbeResult(_pool_status())
    if target is ProbeTarget.MODEL:
        return ProbeResult(_model_status())
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
    target = ProbeTarget(args.check)
    try:
        settings = ContainerSettings.from_environ()
        result = probe(settings, target)
    except ContainerConfigurationError:
        result = ProbeResult(_UNAVAILABLE[target])
    print(result.status.value)
    raise SystemExit(result.exit_code)


if __name__ == "__main__":  # pragma: no cover - exercised by Docker HEALTHCHECK.
    main()
