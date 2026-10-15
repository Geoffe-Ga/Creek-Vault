"""Provider-neutral cloud-egress consent gate (#606).

Centralises the "fragment content is leaving the device" acknowledgement so it
covers *any* cloud provider — Anthropic today, OpenAI / Gemini next — rather
than being hard-wired to Anthropic. Local providers (Ollama) are exempt and
never call into this module.

Consent is granted by setting :data:`CLOUD_CONSENT_ENV` (the legacy
:data:`LEGACY_CONSENT_ENV` is still honored as an alias for back-compat) to one
of :data:`CONSENT_TRUTHY`. The key itself is read from each provider's own SDK
env var and never appears here.

Managed vaults (#1863) are local-only by construction. A vault that Adepthood
provisions carries :data:`MANAGED_VAULT_ENV` in its machine config, and its
runtime refuses to start while either consent variable is affirmative. As
defence in depth, :func:`has_cloud_consent` reports ``False`` whenever that
marker is present or a managed runtime holds :func:`managed_local_only`, even
if a consent variable appears after boot. Self-connected vaults never carry
the marker, so their opt-in is unchanged.
"""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

CONSENT_TRUTHY: frozenset[str] = frozenset({"1", "true", "yes"})
"""Environment-variable values accepted as affirmative consent."""

CLOUD_CONSENT_ENV: str = "CREEK_CLOUD_CONSENT"
"""Provider-neutral cloud-egress consent variable."""

LEGACY_CONSENT_ENV: str = "CREEK_ANTHROPIC_CONSENT"
"""Deprecated Anthropic-specific consent variable, still honored as an alias."""

MANAGED_VAULT_ENV: str = "CREEK_MANAGED_VAULT"
"""Marker the provisioner sets on every Adepthood-managed vault machine.

Its *presence* marks the vault as managed, whatever its value: ``0``, ``false``
or an empty string still count, so the marker fails closed and can only be
lifted by removing it from the machine config itself.
"""

CONSENT_VARIABLES: tuple[str, ...] = (CLOUD_CONSENT_ENV, LEGACY_CONSENT_ENV)
"""Every environment variable that can grant cloud-egress consent."""


class _LocalOnlyLatch:
    """Process-wide count of managed runtimes currently pinned local-only."""

    def __init__(self) -> None:
        """Start released, with no managed runtime holding the latch."""
        self._lock = threading.Lock()
        self._holders = 0

    @property
    def engaged(self) -> bool:
        """Report whether any managed runtime currently holds the latch."""
        with self._lock:
            return self._holders > 0

    @contextmanager
    def hold(self) -> Iterator[None]:
        """Hold the latch for the duration of the ``with`` block."""
        with self._lock:
            self._holders += 1
        try:
            yield
        finally:
            with self._lock:
                self._holders -= 1


_LOCAL_ONLY = _LocalOnlyLatch()


def is_managed_vault(environ: Mapping[str, str] | None = None) -> bool:
    """Report whether *environ* carries the managed-vault marker.

    Args:
        environ: Environment to inspect; defaults to :data:`os.environ`.

    Returns:
        ``True`` when :data:`MANAGED_VAULT_ENV` is present with any value.
    """
    source = os.environ if environ is None else environ
    return MANAGED_VAULT_ENV in source


def affirmative_consent_variables(
    environ: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    """Name every consent variable in *environ* that holds affirmative consent.

    Only names are returned, never values, so callers can report the
    offending variable without echoing anything an operator put in it.

    Args:
        environ: Environment to inspect; defaults to :data:`os.environ`.

    Returns:
        The affirmative variables, in :data:`CONSENT_VARIABLES` order.
    """
    source = os.environ if environ is None else environ
    return tuple(
        var
        for var in CONSENT_VARIABLES
        if source.get(var, "").strip().lower() in CONSENT_TRUTHY
    )


@contextmanager
def managed_local_only() -> Iterator[None]:
    """Pin cloud consent off for a managed runtime while it serves.

    Inside the block :func:`has_cloud_consent` reports ``False`` even when a
    consent variable is set, or the marker removed, after boot.

    Yields:
        Control to the managed runtime's serving loop.
    """
    with _LOCAL_ONLY.hold():
        yield


def has_cloud_consent() -> bool:
    """Report whether cloud egress has been consented to.

    Reads both the neutral and legacy variables so existing
    ``CREEK_ANTHROPIC_CONSENT=1`` setups keep working unchanged. A managed
    vault never has consent, whatever its environment says.

    Returns:
        ``True`` when this is not a managed vault and either variable holds an
        affirmative (:data:`CONSENT_TRUTHY`) value.
    """
    if _LOCAL_ONLY.engaged or is_managed_vault():
        return False
    return bool(affirmative_consent_variables())


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
