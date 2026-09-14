"""Closed, offline renderer for the reviewed Fly pilot configuration."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Final

import httpx

if TYPE_CHECKING:
    from pathlib import Path

_APP_RE: Final[re.Pattern[str]] = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_ORG_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9_-]{1,64}")
_REGION_RE: Final[re.Pattern[str]] = re.compile(r"[a-z]{3}")
_VOLUME_RE: Final[re.Pattern[str]] = re.compile(r"[a-z0-9][a-z0-9_]{0,62}")
_IMAGE_RE: Final[re.Pattern[str]] = re.compile(r"[^@\s]+@sha256:[0-9a-f]{64}")
_TOKENS: Final[tuple[str, ...]] = (
    "APP",
    "REGION",
    "ORGANIZATION",
    "VAULT_IMAGE",
    "CONTROL_IMAGE",
    "TOKEN_EXPIRES_AT",
    "HANDOFF_URL",
    "ALERT_URL",
    "VOLUME",
    "FILE_REFERENCE_KEY",
)
_SECRET_NAMES: Final[frozenset[str]] = frozenset(
    {
        "CREEK_CONTROL_TOKENS_B64",
        "CREEK_FLY_TOKEN_B64",
        "CREEK_SECRET_MASTER_KEY_B64",
        "CREEK_TLS_CA_CERTIFICATE_B64",
        "CREEK_TLS_CA_PRIVATE_KEY_B64",
        "CREEK_HANDOFF_TOKEN_B64",
        "CREEK_FLEET_POLICY_B64",
    }
)


@dataclass(frozen=True, slots=True)
class FlyPilotCoordinates:
    """Non-secret, explicitly authorized coordinates rendered into fly.toml."""

    app: str
    region: str
    organization: str
    vault_image: str
    control_image: str
    token_expires_at: str
    handoff_url: str
    alert_url: str
    volume: str

    def __post_init__(self) -> None:
        """Reject placeholders, mutable images, and TOML/shell injection."""
        if _APP_RE.fullmatch(self.app) is None:
            raise ValueError("Fly pilot app is invalid")
        if _REGION_RE.fullmatch(self.region) is None:
            raise ValueError("Fly pilot region is invalid")
        if _ORG_RE.fullmatch(self.organization) is None:
            raise ValueError("Fly pilot organization is invalid")
        if _VOLUME_RE.fullmatch(self.volume) is None:
            raise ValueError("Fly pilot volume is invalid")
        if (
            _IMAGE_RE.fullmatch(self.vault_image) is None
            or _IMAGE_RE.fullmatch(self.control_image) is None
        ):
            raise ValueError("Fly pilot images must use immutable sha256 digests")
        try:
            expiry = datetime.fromisoformat(self.token_expires_at)
        except ValueError as exc:
            raise ValueError("Fly pilot token expiry is invalid") from exc
        if expiry.tzinfo is None:
            raise ValueError("Fly pilot token expiry must include a timezone")
        try:
            handoff = httpx.URL(self.handoff_url)
            alert = httpx.URL(self.alert_url)
        except httpx.InvalidURL as exc:
            raise ValueError("Fly pilot handoff URL is invalid") from exc
        if (
            handoff.scheme != "https"
            or handoff.host is None
            or handoff.userinfo
            or handoff.query
            or handoff.fragment
            or alert.scheme != "https"
            or alert.host is None
            or alert.path != "/internal/vault-provisioning/alerts"
            or alert.userinfo
            or alert.query
            or alert.fragment
        ):
            raise ValueError("Fly pilot callback URLs are invalid")


def render_fly_toml(template: Path, coordinates: FlyPilotCoordinates) -> str:
    """Render and structurally revalidate one secret-free Fly config."""
    try:
        rendered = template.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValueError("Fly pilot template is unreadable") from exc
    values = {
        "APP": coordinates.app,
        "REGION": coordinates.region,
        "ORGANIZATION": coordinates.organization,
        "VAULT_IMAGE": coordinates.vault_image,
        "CONTROL_IMAGE": coordinates.control_image,
        "TOKEN_EXPIRES_AT": coordinates.token_expires_at,
        "HANDOFF_URL": coordinates.handoff_url,
        "ALERT_URL": coordinates.alert_url,
        "VOLUME": coordinates.volume,
        "FILE_REFERENCE_KEY": "_".join(("secret", "name")),
    }
    for token in _TOKENS:
        marker = "{{" + token + "}}"
        if rendered.count(marker) == 0:
            raise ValueError(f"Fly pilot template is missing {token}")
        rendered = rendered.replace(marker, values[token])
    if "{{" in rendered or "}}" in rendered:
        raise ValueError("Fly pilot template contains an unknown placeholder")
    try:
        document = tomllib.loads(rendered)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError("rendered Fly pilot config is invalid") from exc
    _validate_document(document, coordinates)
    return rendered


def _validate_document(
    document: dict[str, object],
    coordinates: FlyPilotCoordinates,
) -> None:
    """Pin the one-Machine edge, volume, health, restart, and file contract."""
    service = document.get("http_service")
    deploy = document.get("deploy")
    mount = document.get("mounts")
    restart = document.get("restart")
    files = document.get("files")
    environment = document.get("env")
    if (
        document.get("app") != coordinates.app
        or document.get("primary_region") != coordinates.region
        or document.get("kill_signal") != "SIGTERM"
        or document.get("kill_timeout") != 300
        or deploy != {"strategy": "rolling", "max_unavailable": 1}
        or "processes" in document
        or environment
        != {
            "CREEK_PILOT_FLY_ORGANIZATION": coordinates.organization,
            "CREEK_PILOT_EXPECTED_APP": coordinates.app,
            "CREEK_PILOT_FLY_REGION": coordinates.region,
            "CREEK_PILOT_VAULT_IMAGE": coordinates.vault_image,
            "CREEK_PILOT_CONTROL_IMAGE": coordinates.control_image,
            "CREEK_PILOT_FLY_TOKEN_EXPIRES_AT": coordinates.token_expires_at,
            "CREEK_PILOT_HANDOFF_URL": coordinates.handoff_url,
            "CREEK_PILOT_ALERT_URL": coordinates.alert_url,
            "CREEK_PILOT_RECONCILE_INTERVAL_SECONDS": "3600",
            "CREEK_PILOT_RECONCILE_WINDOW_SECONDS": "120",
        }
        or not isinstance(service, dict)
        or service.get("internal_port") != 8080
        or service.get("force_https") is not True
        or service.get("auto_start_machines") is not True
        or service.get("auto_stop_machines") != "off"
        or service.get("min_machines_running") != 1
        or service.get("concurrency")
        != {"type": "requests", "soft_limit": 20, "hard_limit": 25}
        or service.get("checks")
        != [
            {
                "grace_period": "30s",
                "interval": "10s",
                "method": "GET",
                "path": "/__fly/health",
                "timeout": "2s",
            }
        ]
        or mount
        != [
            {
                "source": coordinates.volume,
                "destination": "/data",
                "initial_size": "1GB",
                "snapshot_retention": 7,
                "scheduled_snapshots": True,
            }
        ]
        or restart != [{"policy": "on-failure", "retries": 3}]
        or not isinstance(files, list)
        or len(files) != len(_SECRET_NAMES)
        or {item.get("secret_name") for item in files if isinstance(item, dict)}
        != _SECRET_NAMES
        or any(
            set(item) != {"guest_path", "secret_name"}
            for item in files
            if isinstance(item, dict)
        )
    ):
        raise ValueError("rendered Fly pilot config drifted from reviewed policy")
