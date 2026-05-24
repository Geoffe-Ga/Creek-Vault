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


def test_load_config_parses_reject_on_mime_mismatch_from_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FEAT-035: ``attachments.reject_on_mime_mismatch: true`` round-trips.

    Operators flip this knob in ``crawdad.yaml`` to harden the gate
    from the documented soft-warning default. The field has to survive
    the YAML → Pydantic boundary unchanged or the runtime behaviour
    silently won't match the configured policy.
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    yaml_path = tmp_path / "crawdad.yaml"
    yaml_path.write_text(
        "vault_path: " + str(vault) + "\n"
        "allowed_user_ids: [111]\n"
        "allowed_channel_ids: [222]\n"
        "attachments:\n"
        "  reject_on_mime_mismatch: true\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "t")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")

    config = load_config(yaml_path)
    assert config.attachments.reject_on_mime_mismatch is True


def test_load_config_reject_on_mime_mismatch_defaults_false_when_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A YAML without the knob keeps the v1 soft-warning default."""
    vault = tmp_path / "vault"
    vault.mkdir()
    yaml_path = tmp_path / "crawdad.yaml"
    yaml_path.write_text(
        "vault_path: " + str(vault) + "\n"
        "allowed_user_ids: [111]\n"
        "allowed_channel_ids: [222]\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "t")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")

    config = load_config(yaml_path)
    assert config.attachments.reject_on_mime_mismatch is False


# ---------------------------------------------------------------------------
# FEAT-034 — ConsentConfig
# ---------------------------------------------------------------------------


def test_consent_config_defaults_match_module_constants() -> None:
    """The bare ``ConsentConfig`` adopts the consent-module defaults verbatim."""
    from crawdad.config import ConsentConfig
    from crawdad.consent import (
        DEFAULT_ABANDON_TOKENS,
        DEFAULT_CONSENT_TOKENS,
        DEFAULT_PENDING_BATCH_TTL_SECONDS,
    )

    cfg = ConsentConfig()

    assert cfg.consent_tokens == DEFAULT_CONSENT_TOKENS
    assert cfg.abandon_tokens == DEFAULT_ABANDON_TOKENS
    assert cfg.pending_batch_ttl_seconds == DEFAULT_PENDING_BATCH_TTL_SECONDS


def test_consent_config_normalises_tokens_to_lowercase() -> None:
    """Raw mixed-case tokens lowercase and trim before storage."""
    from crawdad.config import ConsentConfig

    cfg = ConsentConfig(
        consent_tokens=frozenset({"  INGEST ", "Yes", ""}),
        abandon_tokens=frozenset({"CANCEL"}),
    )

    assert cfg.consent_tokens == frozenset({"ingest", "yes"})
    assert cfg.abandon_tokens == frozenset({"cancel"})


def test_consent_config_rejects_non_positive_ttl(tmp_path: Path) -> None:
    """TTL must be strictly positive — zero or non-positive breaks eviction."""
    from crawdad.config import ConsentConfig

    with pytest.raises(ValidationError):
        ConsentConfig(pending_batch_ttl_seconds=0)
    with pytest.raises(ValidationError):
        ConsentConfig(pending_batch_ttl_seconds=-1.0)


def test_consent_config_rejects_token_overlap() -> None:
    """Tokens present in both consent and abandon sets surface as a clear config error.

    Without the model validator, ``classify_followup_message`` would
    silently classify the overlapping token as abandon (the abandon
    branch executes before consent), which is almost never what the
    operator meant.
    """
    from crawdad.config import ConsentConfig

    with pytest.raises(ValidationError) as exc:
        ConsentConfig(
            consent_tokens=frozenset({"ingest", "yes"}),
            abandon_tokens=frozenset({"yes", "drop"}),
        )

    assert "disjoint" in str(exc.value)
    assert "yes" in str(exc.value)


def test_crawdad_config_carries_consent_subconfig(tmp_path: Path) -> None:
    """``CrawDadConfig`` ships with a default ``ConsentConfig`` attached."""
    config = CrawDadConfig(
        discord_bot_token="t",
        anthropic_api_key="k",
        vault_path=tmp_path,
        allowed_user_ids=[111],
        allowed_channel_ids=[222],
    )

    assert "ingest" in config.consent.consent_tokens
    assert "cancel" in config.consent.abandon_tokens
    assert config.consent.pending_batch_ttl_seconds > 0


def test_load_config_parses_consent_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ``consent`` YAML block overrides the bundled defaults."""
    vault = tmp_path / "vault"
    vault.mkdir()
    yaml_path = tmp_path / "crawdad.yaml"
    yaml_path.write_text(
        "vault_path: " + str(vault) + "\n"
        "allowed_user_ids: [111]\n"
        "allowed_channel_ids: [222]\n"
        "consent:\n"
        "  consent_tokens: ['do it']\n"
        "  abandon_tokens: ['hold off']\n"
        "  pending_batch_ttl_seconds: 60\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "t")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")

    config = load_config(yaml_path)

    assert config.consent.consent_tokens == frozenset({"do it"})
    assert config.consent.abandon_tokens == frozenset({"hold off"})
    assert config.consent.pending_batch_ttl_seconds == 60.0
