"""At-rest volume key vault — passphrase + recovery key, no escrow (#758).

Security properties under test:

- both the passphrase and the independent recovery key unwrap the SAME volume
  master key;
- a wrong passphrase / recovery key, a malformed recovery key, or a tampered
  ciphertext fails closed with ``UnlockError`` (never returns garbage);
- **no operator escrow**: the persisted vault is ciphertext-only — neither the
  VMK nor the recovery key appears in it;
- Argon2id parameters are memory-hard (above the OWASP floor);
- the vault round-trips through its JSON form and still unlocks.

No real/production secret material is hardcoded here — passphrases are test
literals and every key is generated per test.
"""

from __future__ import annotations

import json
import stat
from typing import TYPE_CHECKING

import pytest

from creek.confidential.keyvault import (
    KeyVault,
    UnlockError,
    create_key_vault,
    load_key_vault,
    save_key_vault,
    unlock_with_passphrase,
    unlock_with_recovery,
)

if TYPE_CHECKING:
    from pathlib import Path

_PASSPHRASE = "a strong test passphrase 12345"


def test_passphrase_and_recovery_unwrap_the_same_key() -> None:
    """Both independent paths recover the identical volume master key."""
    setup = create_key_vault(_PASSPHRASE)
    from_pass = unlock_with_passphrase(setup.vault, _PASSPHRASE)
    from_recovery = unlock_with_recovery(setup.vault, setup.recovery_key)
    assert from_pass == from_recovery
    assert len(from_pass) == 32  # 256-bit VMK


def test_wrong_passphrase_fails_closed() -> None:
    """An incorrect passphrase raises ``UnlockError``, not a garbage key."""
    setup = create_key_vault(_PASSPHRASE)
    with pytest.raises(UnlockError):
        unlock_with_passphrase(setup.vault, _PASSPHRASE + "!")


def test_wrong_recovery_key_fails_closed() -> None:
    """A recovery key from a *different* vault cannot unlock this one."""
    setup = create_key_vault(_PASSPHRASE)
    other = create_key_vault(_PASSPHRASE)
    with pytest.raises(UnlockError):
        unlock_with_recovery(setup.vault, other.recovery_key)


def test_malformed_recovery_key_fails_closed() -> None:
    """A non-base32 / truncated recovery key raises ``UnlockError``."""
    setup = create_key_vault(_PASSPHRASE)
    with pytest.raises(UnlockError):
        unlock_with_recovery(setup.vault, "not a real recovery key !!!")


def test_persisted_vault_holds_no_plaintext_key_material() -> None:
    """No operator escrow: the serialised vault has neither VMK nor recovery key."""
    setup = create_key_vault(_PASSPHRASE)
    vmk = unlock_with_passphrase(setup.vault, _PASSPHRASE)
    serialized = json.dumps(setup.vault.to_dict())

    assert vmk.hex() not in serialized  # the master key never appears
    assert _PASSPHRASE not in serialized  # nor the passphrase
    assert setup.recovery_key not in serialized  # nor the recovery key
    assert setup.recovery_key.replace("-", "") not in serialized


def test_argon2_parameters_are_memory_hard() -> None:
    """The persisted Argon2id cost is above the OWASP minimum (memory-hard)."""
    vault = create_key_vault(_PASSPHRASE).vault
    assert vault.memory_kib >= 19 * 1024  # OWASP floor ~19 MiB
    assert vault.time_cost >= 2
    assert vault.lanes >= 1


def test_vault_round_trips_through_json_and_still_unlocks() -> None:
    """A vault reloaded from its JSON form unlocks by both paths."""
    setup = create_key_vault(_PASSPHRASE)
    reloaded = KeyVault.from_dict(json.loads(json.dumps(setup.vault.to_dict())))
    assert unlock_with_passphrase(reloaded, _PASSPHRASE) == unlock_with_recovery(
        reloaded, setup.recovery_key
    )


def test_tampered_ciphertext_fails_closed() -> None:
    """Flipping a byte of the wrapped VMK is detected (AEAD integrity)."""
    setup = create_key_vault(_PASSPHRASE)
    data = setup.vault.to_dict()
    ct = bytearray.fromhex(data["passphrase_wrapped"]["ciphertext"])
    ct[0] ^= 0x01
    data["passphrase_wrapped"]["ciphertext"] = ct.hex()
    tampered = KeyVault.from_dict(data)
    with pytest.raises(UnlockError):
        unlock_with_passphrase(tampered, _PASSPHRASE)


def test_empty_passphrase_is_rejected() -> None:
    """An empty passphrase is refused before any key material is generated."""
    with pytest.raises(ValueError, match="passphrase"):
        create_key_vault("")


def test_short_passphrase_is_rejected_below_the_floor() -> None:
    """A passphrase under the strength floor is refused with a non-leaking error."""
    weak = "short11char"  # 11 chars — below the 12-char floor; a test literal
    assert len(weak) < 12
    with pytest.raises(ValueError, match="at least 12 characters") as excinfo:
        create_key_vault(weak)
    # The error states the requirement only — never the supplied passphrase.
    assert weak not in str(excinfo.value)


def test_at_floor_passphrase_is_accepted_and_unlocks_both_paths() -> None:
    """A passphrase exactly at the floor is accepted; both unwrap paths still work."""
    at_floor = "twelvechars!"  # exactly 12 chars — a test literal, not a secret
    assert len(at_floor) == 12
    setup = create_key_vault(at_floor)
    vmk = unlock_with_passphrase(setup.vault, at_floor)
    # The recovery-key path is unaffected by the passphrase floor.
    assert unlock_with_recovery(setup.vault, setup.recovery_key) == vmk


def test_each_setup_is_unique() -> None:
    """Two setups yield distinct VMKs, salts, and recovery keys (fresh randomness)."""
    a = create_key_vault(_PASSPHRASE)
    b = create_key_vault(_PASSPHRASE)
    assert a.recovery_key != b.recovery_key
    assert a.vault.salt != b.vault.salt
    assert unlock_with_passphrase(a.vault, _PASSPHRASE) != unlock_with_passphrase(
        b.vault, _PASSPHRASE
    )


def test_persisted_file_is_ciphertext_only_and_owner_readable(tmp_path: Path) -> None:
    """The on-disk artifact holds no secrets and is written owner-only (0600)."""
    setup = create_key_vault(_PASSPHRASE)
    vmk = unlock_with_passphrase(setup.vault, _PASSPHRASE)
    path = tmp_path / "vault-key.json"
    save_key_vault(setup.vault, path)

    text = path.read_text(encoding="utf-8")
    assert vmk.hex() not in text
    assert _PASSPHRASE not in text
    assert setup.recovery_key.replace("-", "") not in text
    assert stat.S_IMODE(path.stat().st_mode) == 0o600  # owner-only
    # Atomic write leaves no temp artifact behind in the directory.
    assert [p.name for p in tmp_path.iterdir()] == [path.name]
    # Loading the persisted vault still unlocks by both paths.
    reloaded = load_key_vault(path)
    assert unlock_with_passphrase(reloaded, _PASSPHRASE) == vmk
    assert unlock_with_recovery(reloaded, setup.recovery_key) == vmk


def test_recovery_key_is_grouped_and_case_insensitive() -> None:
    """The recovery key is human-storable (grouped base32) and case/space tolerant."""
    setup = create_key_vault(_PASSPHRASE)
    assert "-" in setup.recovery_key  # grouped for readability
    vmk = unlock_with_recovery(setup.vault, setup.recovery_key)
    # Lowercased with spaces instead of hyphens still unlocks.
    lax = setup.recovery_key.lower().replace("-", " ")
    assert unlock_with_recovery(setup.vault, lax) == vmk
