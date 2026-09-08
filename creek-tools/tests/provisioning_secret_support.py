"""The one definition of what provisioning must never publish (#1768, #1769).

``tests/test_provisioning_contract.py`` scans the language-neutral OpenAPI
schema for credential field names, and fleet reconciliation (#1769) needs the
same scan over the reports, divergences and inventory records it emits. Two
copies of that list is how one of them quietly stops covering a field the other
still does, so the list lives here once and both import it.

Nothing in this module knows about HTTP or about the reconciler: it is a
predicate over decoded documents and rendered text, so it works equally on a
JSON schema and on ``repr`` of a frozen dataclass.

This module is deliberately *not* named ``test_*``: pytest's ``python_files``
glob is ``test_*.py``, so it is imported, never collected.
"""

from __future__ import annotations

from typing import Final

FORBIDDEN_FIELD_NAMES: Final[tuple[str, ...]] = (
    "consumer_credential",
    "provider_token",
    "recovery_key",
    "volume_master_key",
    "vault_url",
    "passphrase",
)
"""Names that must never appear on a public schema or an operator artefact.

Each one names material that is either a credential (``consumer_credential``,
``provider_token``, ``passphrase``), a key-ceremony secret (``recovery_key``,
``volume_master_key``), or an internal provider result the consumer receives
exactly once through the one-time handoff and never through a durable read
(``vault_url``).
"""


def field_names(value: object) -> set[str]:
    """Return every exact object key reachable inside a decoded document."""
    if isinstance(value, dict):
        keys = {str(key) for key in value}
        return keys | {name for item in value.values() for name in field_names(item)}
    if isinstance(value, list):
        return {name for item in value for name in field_names(item)}
    return set()


def assert_content_free(text: str) -> None:
    """Fail when *text* mentions any credential or provider-result field."""
    for forbidden in FORBIDDEN_FIELD_NAMES:
        assert forbidden not in text, f"{forbidden!r} must not appear in {text!r}"
