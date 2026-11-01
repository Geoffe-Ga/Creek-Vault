"""The attesting sidecar on the vault Machine: pin, custody and launch (#1896).

ADR-0016 O5 with Privatemode puts the vendor's attesting proxy beside the vault
as a loopback sidecar. These tests own the Machine half: the operator-filled
pin is digest-form and complete, ``_verify_machine`` refuses a Machine whose
sidecar is missing or differently pinned, the bootstrap keeps the vendor key
from the vault's own user, and the launcher runs only the pinned binary and
never puts the key on a command line.

Every value is synthetic. Nothing is deployed, no Fly API is called, no
vendor is contacted and no process is really spawned (Decision 4).
"""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Final

import pytest

from creek.classify.llm.pool_attestation import (
    POOL_EGRESS_ENV,
    POOL_MEASUREMENTS_ENV,
    POOL_SIGNING_IDENTITY_ENV,
    valid_egress,
)
from creek.classify.llm.pool_boundary import POOL_VENDOR_ENV
from creek_mcp.provisioning import fly_vault_bootstrap, pool_sidecar
from creek_mcp.provisioning.driver import ProviderError
from creek_mcp.provisioning.fly import FlyProviderPolicy
from creek_mcp.provisioning.models import FailureReason, RoutableAllocation
from creek_mcp.provisioning.pool_sidecar import (
    POOL_SIDECAR_IMAGE_ENV,
    POOL_SIDECAR_PORT_ENV,
    POOL_SIDECAR_SHA256_ENV,
    SIDECAR_GID,
    SIDECAR_KEY_ENV,
    SIDECAR_UID,
    PoolSidecarPolicy,
    key_file_owner,
    sidecar_argv,
    start_pool_sidecar,
)
from tests.fly_support import (
    IMAGE,
    FakeSecretManager,
    ProviderNormalizedFlyAPI,
    fly_driver,
    fly_job,
)
from tests.test_pool_runtime_bundle import _issuer

if TYPE_CHECKING:
    from pathlib import Path

    from creek_mcp.provisioning.fly import FlyProviderDriver

BINARY_BYTES: Final[bytes] = b"synthetic proxy binary"
BINARY_SHA256: Final[str] = hashlib.sha256(BINARY_BYTES).hexdigest()
KEY_CANARY: Final[str] = "pm-key-canary-5e0d"
VENDOR_IMAGE: Final[str] = "ghcr.io/example/privatemode-proxy@sha256:" + "1" * 64


def _policy(**overrides: Any) -> PoolSidecarPolicy:
    """Return a complete, valid sidecar pin with *overrides* applied."""
    fields: dict[str, Any] = {
        "image": VENDOR_IMAGE,
        "binary_sha256": BINARY_SHA256,
        "egress": "api.privatemode.example:443",
        "signing_identity": "https://github.com/example/release.yml",
        "min_version": "1.30.0",
        "measurements": ("a" * 64,),
    }
    fields.update(overrides)
    return PoolSidecarPolicy(**fields)


# --------------------------------------------------------------------------
# The pin: operator-filled, digest-form, complete.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"image": "ghcr.io/example/privatemode-proxy:latest"},
        {"image": "ghcr.io/example/privatemode-proxy@sha256:abc"},
        {"binary_sha256": "Z" * 64},
        {"egress": "https://api.privatemode.example"},
        {"egress": "10.1.2.3:443"},
        {"signing_identity": " "},
        {"min_version": ""},
        {"measurements": ()},
        {"measurements": ("a" * 64, "b" * 64, "c" * 64)},
        {"measurements": ("not-a-digest",)},
        {"port": 80},
        {"port": 70_000},
        {"rate_per_hour": 0},
    ],
)
def test_a_floating_partial_or_malformed_pin_is_refused(
    overrides: dict[str, Any],
) -> None:
    """The image is pinned by digest only; every other field is checked too."""
    with pytest.raises(ValueError, match="pool"):
        _policy(**overrides)


def test_a_mixed_case_egress_host_is_normalised_not_refused() -> None:
    """#2042: hostnames are case-insensitive, so operator casing is not an error.

    ``load_pool_pin`` lowercases what the Machine reads back; the policy
    lowercases what the operator typed, so the rendered ``env`` it is compared
    against is the same canonical form.
    """
    policy = _policy(egress="Api.PrivateMode.Example:443")

    assert policy.egress == "api.privatemode.example:443"
    assert policy.runtime_env()[POOL_EGRESS_ENV] == "api.privatemode.example:443"


def test_an_all_digit_final_label_is_a_hostname() -> None:
    """#2042: a valid hostname whose last label is all digits is admitted."""
    assert valid_egress("pool.vendor.example.2049:443")
    assert _policy(egress="pool.vendor.2049:443").egress == "pool.vendor.2049:443"


@pytest.mark.parametrize(
    "egress",
    [
        "10.1.2.3:443",
        "203.0.113.7:8443",
        # Legacy shorthand the resolver still reads as an address.
        "127.1:443",
        "0x7f.1:443",
        "1.0x1:443",
        "[::1]:443",
        "[2001:db8::1]:443",
        "::1:443",
        "2001:db8::1:443",
    ],
)
def test_an_ip_literal_egress_is_still_refused(egress: str) -> None:
    """#2042: the pin names one vendor host, never an IPv4 or IPv6 literal."""
    assert not valid_egress(egress)
    with pytest.raises(ValueError, match="pool sidecar egress"):
        _policy(egress=egress)


def test_the_pin_renders_only_non_secret_env() -> None:
    """The Machine env carries the pin and no key."""
    env = _policy(measurements=("a" * 64, "e" * 64)).runtime_env()
    assert env[POOL_VENDOR_ENV] == "privatemode"
    assert env[POOL_SIDECAR_IMAGE_ENV] == VENDOR_IMAGE
    assert env[POOL_SIDECAR_SHA256_ENV] == BINARY_SHA256
    assert env[POOL_MEASUREMENTS_ENV] == "a" * 64 + "," + "e" * 64
    assert KEY_CANARY not in repr(env)


def test_the_sidecar_requires_the_replay_runtime() -> None:
    """Only the replay bootstrap can launch it and keep its key root-only."""
    with pytest.raises(ValueError, match="replay"):
        FlyProviderPolicy(organization="org", image=IMAGE, pool_sidecar=_policy())


# --------------------------------------------------------------------------
# The Machine config: present, pinned, verified — and absent without a pool.
# --------------------------------------------------------------------------


def _provisioned(
    *, with_pool: bool = True
) -> tuple[ProviderNormalizedFlyAPI, FlyProviderDriver]:
    """Provision one replay allocation under the sidecar policy."""
    api = ProviderNormalizedFlyAPI()
    secrets = FakeSecretManager(set(), issuer=_issuer() if with_pool else None)
    driver = fly_driver(api, secrets, fly_replay_enabled=True, pool_sidecar=_policy())
    driver.provision(fly_job())
    return api, driver


def _config(api: ProviderNormalizedFlyAPI) -> dict[str, Any]:
    """Return the single Machine's config."""
    (machines,) = api.machines.values()
    (machine,) = machines
    config: dict[str, Any] = machine["config"]
    return config


def _route(driver: FlyProviderDriver) -> None:
    """Route the allocation as the edge does on the replay runtime."""
    job = fly_job()
    driver.prepare_replay(
        RoutableAllocation(
            job.job_id,
            job.activation_id,
            job.requester_identity,
            job.consumer_identity,
            driver.expected_allocation_id(job.activation_id),
        )
    )


def test_a_pool_activation_carries_the_pinned_sidecar() -> None:
    """The sidecar and its pin reach the Machine with the pool bundle."""
    api, driver = _provisioned()
    env = _config(api)["env"]
    assert env | _policy().runtime_env() == env
    _route(driver)


def test_a_pool_free_activation_carries_no_sidecar() -> None:
    """A vault with no pool bundle is unaffected by the sidecar policy."""
    api, _ = _provisioned(with_pool=False)
    env = _config(api)["env"]
    assert not set(_policy().runtime_env()) & set(env)


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        (POOL_VENDOR_ENV, None),
        (POOL_SIDECAR_IMAGE_ENV, "ghcr.io/example/other@sha256:" + "2" * 64),
        (POOL_SIDECAR_SHA256_ENV, "f" * 64),
        (POOL_SIDECAR_PORT_ENV, "9999"),
        (POOL_MEASUREMENTS_ENV, "d" * 64),
        (POOL_SIGNING_IDENTITY_ENV, "https://github.com/someone-else/release.yml"),
        (POOL_EGRESS_ENV, "api.elsewhere.example:443"),
    ],
)
def test_verify_machine_refuses_a_missing_or_repinned_sidecar(
    variable: str, value: str | None
) -> None:
    """A Machine without the sidecar, or pinned otherwise, is not admitted.

    Refused retryably and left untouched, so the operator's converge (#1869)
    puts it back on policy; routing never rewrites it.
    """
    api, driver = _provisioned()
    env = _config(api)["env"]
    if value is None:
        env.pop(variable)
    else:
        env[variable] = value
    with pytest.raises(ProviderError) as caught:
        _route(driver)
    assert caught.value.reason is FailureReason.PROVIDER_UNAVAILABLE
    assert api.updates == []


def test_converge_restores_a_drifted_sidecar_pin() -> None:
    """A stopped, drifted Machine is rewritten to policy on the worker path."""
    api, driver = _provisioned()
    _config(api)["env"][POOL_MEASUREMENTS_ENV] = "d" * 64
    driver.provision(fly_job())
    assert _config(api)["env"][POOL_MEASUREMENTS_ENV] == "a" * 64
    _route(driver)


# --------------------------------------------------------------------------
# Custody: the vault's user never owns the key file in sidecar mode.
# --------------------------------------------------------------------------


def _bootstrap_paths(tmp_path: Path) -> tuple[Path, Path, Path, tuple[Path, ...]]:
    """Return a vault volume, mountinfo, key file and the listed secrets."""
    vault = tmp_path / "vault"
    vault.mkdir()
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        f"41 32 0:35 / {vault} rw,nosuid - ext4 /dev/vault rw\n", encoding="utf-8"
    )
    consumer = tmp_path / "consumer"
    consumer.write_text("synthetic-secret", encoding="utf-8")
    key = tmp_path / "creek_pool_key"
    key.write_text(KEY_CANARY, encoding="utf-8")
    return vault, mountinfo, key, (consumer, key)


@pytest.mark.parametrize(
    ("vendor", "key_owner"),
    [("privatemode", (0, 0)), ("", (10001, 10001))],
)
def test_the_key_file_is_root_owned_only_in_sidecar_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    vendor: str,
    key_owner: tuple[int, int],
) -> None:
    """Sidecar mode: root ``0400``, so ``10001`` can never read the key."""
    vault, mountinfo, key, secrets = _bootstrap_paths(tmp_path)
    owners: dict[int, tuple[int, int]] = {}
    monkeypatch.setenv(POOL_VENDOR_ENV, vendor)
    monkeypatch.setattr(fly_vault_bootstrap.os, "geteuid", lambda: 0)
    monkeypatch.setattr(fly_vault_bootstrap.os, "chown", lambda *_a, **_k: None)
    monkeypatch.setattr(
        fly_vault_bootstrap.os,
        "fchown",
        lambda descriptor, uid, gid: owners.__setitem__(
            fly_vault_bootstrap.os.fstat(descriptor).st_ino, (uid, gid)
        ),
    )

    fly_vault_bootstrap.prepare_runtime_paths(
        vault_root=vault,
        mountinfo=mountinfo,
        secret_files=secrets,
        vendor_key_file=key,
    )

    assert owners[key.stat().st_ino] == key_owner
    assert owners[secrets[0].stat().st_ino] == (10001, 10001)
    assert (key.stat().st_mode & 0o777) == 0o400


def test_key_file_owner_is_root_only_in_sidecar_mode() -> None:
    """The rule in one place."""
    assert key_file_owner({POOL_VENDOR_ENV: "privatemode"}) == (0, 0)
    assert key_file_owner({}) is None


def test_main_starts_the_sidecar_before_the_privilege_drop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only root can read the key, so the launch precedes the drop."""
    order: list[str] = []
    monkeypatch.setattr(
        fly_vault_bootstrap, "prepare_runtime_paths", lambda: order.append("paths")
    )
    monkeypatch.setattr(
        fly_vault_bootstrap,
        "start_pool_sidecar",
        lambda _environ: order.append("sidecar"),
    )
    monkeypatch.setattr(
        fly_vault_bootstrap, "drop_privileges_and_run", lambda: order.append("drop")
    )
    fly_vault_bootstrap.main()
    assert order == ["paths", "sidecar", "drop"]


# --------------------------------------------------------------------------
# Launch: only the pinned binary, key in the child's env only, never fatal.
# --------------------------------------------------------------------------


class _Spawn:
    """Records launches instead of running them."""

    def __init__(self, *, fails: bool = False) -> None:
        """Optionally fail every launch."""
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self._fails = fails

    def __call__(self, argv: list[str], **kwargs: Any) -> object:
        """Record one launch."""
        if self._fails:
            raise OSError("synthetic spawn failure")
        self.calls.append((argv, kwargs))
        return object()


def _sidecar_env(**overrides: str) -> dict[str, str]:
    """Return a Machine environment declaring the pinned sidecar."""
    env = _policy().runtime_env()
    env.update(overrides)
    return env


def _binary(tmp_path: Path, content: bytes = BINARY_BYTES) -> Path:
    """Write a synthetic binary and return its path."""
    path = tmp_path / "privatemode-proxy"
    path.write_bytes(content)
    return path


def _key(tmp_path: Path, content: str = KEY_CANARY) -> Path:
    """Write a synthetic key file and return its path."""
    path = tmp_path / "creek_pool_key"
    path.write_text(content, encoding="utf-8")
    return path


def test_the_pinned_binary_starts_as_its_own_user_with_the_key_in_env(
    tmp_path: Path,
) -> None:
    """The key is in the child's environment and nowhere on its command line."""
    spawn = _Spawn()
    binary = _binary(tmp_path)
    started = start_pool_sidecar(
        _sidecar_env(), binary=binary, key_file=_key(tmp_path), spawn=spawn
    )
    assert started
    ((argv, kwargs),) = spawn.calls
    assert argv == [str(binary), "--port", "8085"]
    assert KEY_CANARY not in " ".join(argv)
    assert kwargs["env"] == {SIDECAR_KEY_ENV: KEY_CANARY}
    assert (kwargs["user"], kwargs["group"]) == (SIDECAR_UID, SIDECAR_GID)
    assert kwargs["extra_groups"] == []
    assert kwargs["stdout"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.DEVNULL


@pytest.mark.parametrize(
    ("env", "binary", "key", "reason"),
    [
        (_sidecar_env(), b"a different binary", KEY_CANARY, "binary_unpinned"),
        (
            _sidecar_env(**{POOL_SIDECAR_SHA256_ENV: "nope"}),
            BINARY_BYTES,
            KEY_CANARY,
            "binary_unpinned",
        ),
        (
            _sidecar_env(**{POOL_SIDECAR_PORT_ENV: "80"}),
            BINARY_BYTES,
            KEY_CANARY,
            "port_invalid",
        ),
        (_sidecar_env(), BINARY_BYTES, "", "key_unavailable"),
    ],
)
def test_an_unpinned_or_incomplete_sidecar_is_not_started(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    env: dict[str, str],
    binary: bytes,
    key: str,
    reason: str,
) -> None:
    """Each refusal is recorded content-free and leaves the vault running."""
    spawn = _Spawn()
    started = start_pool_sidecar(
        env,
        binary=_binary(tmp_path, binary),
        key_file=_key(tmp_path, key),
        spawn=spawn,
    )
    assert not started
    assert spawn.calls == []
    err = capsys.readouterr().err
    assert reason in err
    assert KEY_CANARY not in err


def test_a_missing_binary_or_key_file_is_not_started(tmp_path: Path) -> None:
    """Absent files refuse rather than raise."""
    spawn = _Spawn()
    assert not start_pool_sidecar(
        _sidecar_env(), binary=tmp_path / "absent", key_file=_key(tmp_path), spawn=spawn
    )
    assert not start_pool_sidecar(
        _sidecar_env(),
        binary=_binary(tmp_path),
        key_file=tmp_path / "absent-key",
        spawn=spawn,
    )
    assert spawn.calls == []


def test_a_failed_spawn_is_recorded_not_raised(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The vault keeps running; the pool fails closed."""
    assert not start_pool_sidecar(
        _sidecar_env(),
        binary=_binary(tmp_path),
        key_file=_key(tmp_path),
        spawn=_Spawn(fails=True),
    )
    assert "spawn_failed" in capsys.readouterr().err


def test_no_sidecar_is_started_when_none_is_declared(tmp_path: Path) -> None:
    """A pool-free or O3 vault launches nothing."""
    spawn = _Spawn()
    assert not start_pool_sidecar(
        {}, binary=_binary(tmp_path), key_file=_key(tmp_path), spawn=spawn
    )
    assert spawn.calls == []


def test_the_argv_never_carries_a_key(tmp_path: Path) -> None:
    """``/proc/<pid>/cmdline`` is world-readable; the key is never in it."""
    argv = sidecar_argv(_sidecar_env(), tmp_path / "bin")
    assert argv is not None
    assert all(KEY_CANARY not in part for part in argv)


def test_the_policy_is_a_frozen_value() -> None:
    """``replace`` re-validates, so no mutation can skip the checks."""
    with pytest.raises(ValueError, match="pool"):
        replace(_policy(), image="floating:latest")


def test_the_launcher_is_the_stdlib_popen_by_default() -> None:
    """The real launch path is ``subprocess.Popen``; no shell is involved."""
    assert pool_sidecar.start_pool_sidecar.__kwdefaults__ is not None
    assert pool_sidecar.start_pool_sidecar.__kwdefaults__["spawn"] is subprocess.Popen
