"""Tests for ``crawdad.config``."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from crawdad.config import CrawDadConfig, load_config


def test_config_requires_discord_token_and_anthropic_key(tmp_path: Path) -> None:
    """Construction fails clearly when required secrets are absent."""
    with pytest.raises(ValidationError):
        CrawDadConfig(
            discord_bot_token="",
            anthropic_api_key="key",
            vault_path=tmp_path,
            allowed_user_ids=[1],
            allowed_channel_ids=[2],
        )


def test_config_parses_full_payload(tmp_path: Path) -> None:
    """All documented fields land on the model in their declared types."""
    config = CrawDadConfig(
        discord_bot_token="t",
        anthropic_api_key="k",
        vault_path=tmp_path,
        mcp_server_command=("creek-tools-mcp",),
        allowed_user_ids=[111, 222],
        allowed_channel_ids=[999],
    )
    assert config.discord_bot_token == "t"
    assert config.anthropic_api_key == "k"
    assert config.vault_path == tmp_path
    assert config.mcp_server_command == ("creek-tools-mcp",)
    assert config.allowed_user_ids == (111, 222)
    assert config.allowed_channel_ids == (999,)


def test_config_rejects_empty_allowlist(tmp_path: Path) -> None:
    """An empty allowlist is a configuration error, not a silent open door."""
    with pytest.raises(ValidationError):
        CrawDadConfig(
            discord_bot_token="t",
            anthropic_api_key="k",
            vault_path=tmp_path,
            allowed_user_ids=[],
            allowed_channel_ids=[999],
        )


def test_load_config_merges_yaml_and_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Secrets come from env; vault path, allowlists, and command come from YAML."""
    vault = tmp_path / "vault"
    vault.mkdir()
    yaml_path = tmp_path / "crawdad.yaml"
    yaml_path.write_text(
        "vault_path: " + str(vault) + "\n"
        "mcp_server_command:\n"
        "  - creek-tools-mcp\n"
        "allowed_user_ids: [111]\n"
        "allowed_channel_ids: [222]\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "from-env")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "key-from-env")

    config = load_config(yaml_path)

    assert config.discord_bot_token == "from-env"
    assert config.anthropic_api_key == "key-from-env"
    assert config.vault_path == vault
    assert config.allowed_user_ids == (111,)
    assert config.allowed_channel_ids == (222,)
    assert config.mcp_server_command == ("creek-tools-mcp",)


def test_load_config_requires_discord_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Missing ``DISCORD_BOT_TOKEN`` surfaces a clear actionable error."""
    yaml_path = tmp_path / "crawdad.yaml"
    yaml_path.write_text(
        "vault_path: " + str(tmp_path) + "\n"
        "allowed_user_ids: [1]\n"
        "allowed_channel_ids: [2]\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")

    with pytest.raises(RuntimeError, match="DISCORD_BOT_TOKEN"):
        load_config(yaml_path)


def test_load_config_requires_anthropic_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Missing ``ANTHROPIC_API_KEY`` surfaces a clear actionable error."""
    yaml_path = tmp_path / "crawdad.yaml"
    yaml_path.write_text(
        "vault_path: " + str(tmp_path) + "\n"
        "allowed_user_ids: [1]\n"
        "allowed_channel_ids: [2]\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "t")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        load_config(yaml_path)


def test_is_allowed_user(tmp_path: Path) -> None:
    """``is_allowed`` returns True only for the configured user + channel pair."""
    config = CrawDadConfig(
        discord_bot_token="t",
        anthropic_api_key="k",
        vault_path=tmp_path,
        allowed_user_ids=[111],
        allowed_channel_ids=[999],
    )
    assert config.is_allowed(user_id=111, channel_id=999) is True
    assert config.is_allowed(user_id=222, channel_id=999) is False
    assert config.is_allowed(user_id=111, channel_id=888) is False


def test_config_attachments_defaults_to_25_mib_and_inbound(tmp_path: Path) -> None:
    """FEAT-027: the default attachment config matches the documented limits."""
    config = CrawDadConfig(
        discord_bot_token="t",
        anthropic_api_key="k",
        vault_path=tmp_path,
        allowed_user_ids=[1],
        allowed_channel_ids=[2],
    )
    assert config.attachments.max_size_bytes == 25 * 1024 * 1024
    assert config.attachments.staging_subpath == Path("00-Creek-Meta") / "Inbound"
    assert ".md" in config.attachments.allowed_extensions
    assert ".exe" in config.attachments.denied_extensions


def test_attachment_config_rejects_unknown_privacy_tier() -> None:
    """FEAT-027: bogus tier strings in ``channel_privacy_tiers`` are refused.

    Without this validator a typo in ``crawdad.yaml`` would surface
    as a confusing MCP error at runtime instead of a clear config
    error at startup.
    """
    from crawdad.config import AttachmentConfig

    with pytest.raises(ValidationError, match="valid tier ceiling"):
        AttachmentConfig(channel_privacy_tiers={999: "superopen"})


def test_attachment_config_accepts_all_four_tier_values() -> None:
    """All four ``TierCeiling`` values pass validation."""
    from crawdad.config import AttachmentConfig

    config = AttachmentConfig(
        channel_privacy_tiers={
            1: "open",
            2: "personal",
            3: "intimate",
            4: "all",
        },
    )
    assert config.channel_privacy_tiers[2] == "personal"


def test_load_config_parses_attachment_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FEAT-027: ``attachments:`` block in YAML overrides each field."""
    vault = tmp_path / "vault"
    vault.mkdir()
    yaml_path = tmp_path / "crawdad.yaml"
    yaml_path.write_text(
        "vault_path: " + str(vault) + "\n"
        "allowed_user_ids: [111]\n"
        "allowed_channel_ids: [222]\n"
        "attachments:\n"
        "  max_size_bytes: 10485760\n"
        "  allowed_extensions: ['.md', '.txt']\n"
        "  denied_extensions: ['.exe', '.sh']\n"
        "  channel_privacy_tiers:\n"
        "    222: intimate\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "t")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")

    config = load_config(yaml_path)
    assert config.attachments.max_size_bytes == 10 * 1024 * 1024
    assert config.attachments.allowed_extensions == frozenset({".md", ".txt"})
    assert config.attachments.denied_extensions == frozenset({".exe", ".sh"})
    assert config.attachments.channel_privacy_tiers[222] == "intimate"
