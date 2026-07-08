"""Provider-neutral cloud-egress consent gate (#606).

Centralises the "fragment content is leaving the device" acknowledgement so it
covers *any* cloud provider — Anthropic today, OpenAI / Gemini next — rather
than being hard-wired to Anthropic. Local providers (Ollama) are exempt and
never call into this module.

Consent is granted by setting :data:`CLOUD_CONSENT_ENV` (the legacy
:data:`LEGACY_CONSENT_ENV` is still honored as an alias for back-compat) to one
of :data:`CONSENT_TRUTHY`. The key itself is read from each provider's own SDK
env var and never appears here.
"""

from __future__ import annotations

import os

CONSENT_TRUTHY: frozenset[str] = frozenset({"1", "true", "yes"})
"""Environment-variable values accepted as affirmative consent."""

CLOUD_CONSENT_ENV: str = "CREEK_CLOUD_CONSENT"
"""Provider-neutral cloud-egress consent variable."""

LEGACY_CONSENT_ENV: str = "CREEK_ANTHROPIC_CONSENT"
"""Deprecated Anthropic-specific consent variable, still honored as an alias."""


def has_cloud_consent() -> bool:
    """Report whether cloud egress has been consented to.

    Reads both the neutral and legacy variables so existing
    ``CREEK_ANTHROPIC_CONSENT=1`` setups keep working unchanged.

    Returns:
        ``True`` when either variable holds an affirmative
        (:data:`CONSENT_TRUTHY`) value.
    """
    for var in (CLOUD_CONSENT_ENV, LEGACY_CONSENT_ENV):
        if os.environ.get(var, "").strip().lower() in CONSENT_TRUTHY:
            return True
    return False


def consent_error_message(provider_name: str) -> str:
    """Build the remediation message for a missing cloud-consent gate.

    Args:
        provider_name: Display name of the active cloud provider (e.g.
            ``"Anthropic"``), surfaced so the operator knows whose servers
            content would reach.

    Returns:
        A one-paragraph message naming the provider and the variable to set.
    """
    return (
        f"{provider_name} cloud classification requires explicit consent. "
        f"Set {CLOUD_CONSENT_ENV}=1 (also accepts 'true'/'yes'; the legacy "
        f"{LEGACY_CONSENT_ENV} is still honored) to confirm that fragment "
        f"content may be sent to {provider_name}'s servers."
    )


def require_cloud_consent(provider_name: str) -> None:
    """Raise unless cloud egress has been consented to.

    Args:
        provider_name: Display name of the active cloud provider, woven into
            the error so the acknowledgement is unambiguous.

    Raises:
        RuntimeError: When neither consent variable is affirmatively set.
    """
    if not has_cloud_consent():
        raise RuntimeError(consent_error_message(provider_name))


def cloud_warning(provider_name: str) -> str:
    """Build the provider-neutral cloud-egress warning banner.

    Args:
        provider_name: Display name of the active cloud provider.

    Returns:
        The warning string logged when a cloud provider is selected.
    """
    return (
        "WARNING: Cloud classification enabled. "
        f"Fragment content will be sent to {provider_name}'s servers."
    )
