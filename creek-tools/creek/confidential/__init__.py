"""Confidential-hosting data layer: at-rest volume key management (#758).

Exposes the user-held encryption key model for the vault volume: a passphrase
(Argon2id) and an independent one-time recovery key both unwrap the same volume
master key, with NO operator escrow and no reset path. See
:mod:`creek.confidential.keyvault`.
"""

from creek.confidential.keyvault import (
    KeyVault,
    SetupResult,
    UnlockError,
    create_key_vault,
    load_key_vault,
    save_key_vault,
    unlock_with_passphrase,
    unlock_with_recovery,
)

__all__ = [
    "KeyVault",
    "SetupResult",
    "UnlockError",
    "create_key_vault",
    "load_key_vault",
    "save_key_vault",
    "unlock_with_passphrase",
    "unlock_with_recovery",
]
