"""A static consumer-token set that follows its mounted file, no restart (#1965).

:class:`~creek_mcp.credential_reload.ReloadingConsumerTokenVerifier` re-reads
its secret file when the file changes — at most once per
:data:`~creek_mcp.credential_reload.RELOAD_POLL_SECONDS` — and swaps the parsed
set in whole. A file the boot-time parser would refuse is refused here too,
named on stderr without a token value, and the previous set stays in force.

The parser these tests hand the verifier is the environment registry's own
(:func:`~creek_mcp.remote_auth.load_consumer_tokens` plus
:func:`~creek_mcp.remote_auth.load_consumer_grants`), so the issue's
multi-consumer timeline runs exactly as written; the container's
single-identity parser is tested beside the container runtime.
"""

from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING, Final

import pytest

from creek_mcp.credential_reload import (
    RELOAD_POLL_SECONDS,
    ReloadingConsumerTokenVerifier,
    content_digest,
)
from creek_mcp.policy import UNGATED_SCOPE
from creek_mcp.remote_auth import (
    CONSUMER_TOKENS_ENV,
    REMOTE_SCOPE,
    ConsumerTokenVerifier,
    load_consumer_grants,
    load_consumer_tokens,
)
from tests.agent_token_support import FakeClock

if TYPE_CHECKING:
    from pathlib import Path

    from mcp.server.auth.provider import AccessToken

_A: Final[str] = "adepthood-token-that-is-at-least-thirty-two-chars"
"""Adepthood's token. Test literal, not a real credential."""

_B: Final[str] = "openclaw-token-that-is-also-at-least-thirty-two"
"""OpenClaw's token. Test literal, not a real credential."""

_C: Final[str] = "replacement-token-for-adepthood-thirty-two-plus"
"""Adepthood's incoming token in a rotation. Test literal."""

_SHORT: Final[str] = "short"
"""A token below the 32-character floor."""

_SECRETS: Final[tuple[str, ...]] = (_A, _B, _C, _SHORT)


def _registry(raw: str) -> ConsumerTokenVerifier:
    """Parse *raw* exactly as the environment registry is parsed at boot.

    Args:
        raw: The file's text.

    Returns:
        The verifier the boot path would build.
    """
    environ = {CONSUMER_TOKENS_ENV: raw.strip()}
    return ConsumerTokenVerifier(
        load_consumer_tokens(environ), grants=load_consumer_grants(environ)
    )


class _Vault:
    """A secret file, the verifier following it, and the clock it polls on."""

    def __init__(self, tmp_path: Path, content: str) -> None:
        """Write *content* and boot a verifier from it.

        Args:
            tmp_path: Pytest's per-test directory.
            content: The file's boot-time text.
        """
        self.path = tmp_path / "consumer_tokens"
        self.path.write_text(content, encoding="utf-8")
        self.clock = FakeClock()
        booted = self.path.read_bytes()
        self.verifier = ReloadingConsumerTokenVerifier(
            self.path,
            _registry,
            initial=_registry(booted.decode("utf-8")),
            baseline=content_digest(booted),
            clock=self.clock,
        )

    def write(self, content: str) -> None:
        """Replace the file's content, as an operator editing the mount would.

        Args:
            content: The new text.
        """
        self.path.write_text(content, encoding="utf-8")

    def later(self) -> None:
        """Move the clock one poll interval forward."""
        self.clock.advance(RELOAD_POLL_SECONDS)

    def verify(self, token: str) -> AccessToken | None:
        """Present *token* to the verifier.

        Args:
            token: The bearer.

        Returns:
            The verifier's answer.
        """
        return asyncio.run(self.verifier.verify_token(token))

    def client_of(self, token: str) -> str | None:
        """Return the consumer *token* authenticates as, if any.

        Args:
            token: The bearer.

        Returns:
            The consumer name, or ``None`` when refused.
        """
        access = self.verify(token)
        return None if access is None else access.client_id


def _assert_no_secret(text: str) -> None:
    """Assert *text* carries none of the test token values.

    Args:
        text: Captured output.
    """
    for secret in _SECRETS:
        assert secret not in text


def test_the_poll_interval_is_the_issue_value() -> None:
    """The file is re-read at most once every five seconds."""
    assert RELOAD_POLL_SECONDS == 5


def test_the_issue_timeline_needs_no_restart(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """t0-t3 of the issue: add, refuse a weak edit, then revoke.

    Args:
        tmp_path: Pytest's per-test directory.
        capsys: Captures the operator notices.
    """
    vault = _Vault(tmp_path, f"adepthood={_A}")
    assert vault.client_of(_A) == "adepthood"
    assert vault.client_of(_B) is None

    vault.write(f"adepthood={_A};openclaw={_B}")
    vault.later()
    assert vault.client_of(_B) == "openclaw"

    vault.write(f"adepthood={_A};openclaw={_SHORT}")
    vault.later()
    assert vault.client_of(_B) == "openclaw"
    refused = capsys.readouterr().err
    assert "refused" in refused
    assert "'openclaw'" in refused
    assert "below the 32-char minimum" in refused
    _assert_no_secret(refused)

    vault.write(f"adepthood={_A}")
    vault.later()
    assert vault.client_of(_B) is None
    assert vault.client_of(_A) == "adepthood"
    _assert_no_secret(capsys.readouterr().err)


def test_an_edit_inside_the_poll_interval_waits_for_it(tmp_path: Path) -> None:
    """The file is not re-read on every request, only once the interval passes.

    Args:
        tmp_path: Pytest's per-test directory.
    """
    vault = _Vault(tmp_path, f"adepthood={_A}")
    vault.write(f"adepthood={_C}")
    vault.clock.advance(RELOAD_POLL_SECONDS - 1)

    assert vault.client_of(_A) == "adepthood"
    assert vault.client_of(_C) is None

    vault.clock.advance(1)

    assert vault.client_of(_C) == "adepthood"
    assert vault.client_of(_A) is None


def test_the_file_is_read_at_most_once_per_interval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A burst of requests inside one interval costs one read, not one each.

    Args:
        tmp_path: Pytest's per-test directory.
        monkeypatch: Counts reads of the secret file.
    """
    vault = _Vault(tmp_path, f"adepthood={_A}")
    reads: list[Path] = []
    original = type(vault.path).read_bytes

    def counting(path: Path) -> bytes:
        reads.append(path)
        return original(path)

    monkeypatch.setattr(type(vault.path), "read_bytes", counting)

    for _ in range(5):
        vault.verify(_A)
    vault.later()
    for _ in range(5):
        vault.verify(_A)

    assert reads == [vault.path]


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        (f"adepthood={_A};openclaw={_SHORT}", "below the 32-char minimum"),
        (f"adepthood={_A};adepthood={_B}", "configured more than once"),
        (f"adepthood={_A};openclaw={_A}", "same token value is configured twice"),
        (f"adepthood={_A}@vault:everything", "unreadable grant suffix"),
    ],
    ids=["weak-token", "duplicate-consumer", "shared-value", "bad-grant"],
)
def test_an_invalid_edit_keeps_the_previous_set_and_is_logged_once(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    content: str,
    reason: str,
) -> None:
    """Every load-time refusal is a reload refusal; nothing is half-applied.

    Args:
        tmp_path: Pytest's per-test directory.
        capsys: Captures the refusal.
        content: A file the boot-time parser refuses.
        reason: The refusal's wording.
    """
    vault = _Vault(tmp_path, f"adepthood={_C}")
    vault.write(content)

    for _ in range(3):
        vault.later()
        assert vault.client_of(_C) == "adepthood"
        assert vault.client_of(_A) is None
        assert vault.client_of(_B) is None

    err = capsys.readouterr().err
    assert err.count("refused") == 1
    assert reason in err
    assert "previous credential set stays in force" in err
    _assert_no_secret(err)
    assert vault.verifier.consumers == ("adepthood",)


def test_a_file_that_is_not_utf8_is_refused_without_echoing_a_byte(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A decode failure gets a fixed reason, never the codec's byte-quoting one.

    Args:
        tmp_path: Pytest's per-test directory.
        capsys: Captures the refusal.
    """
    vault = _Vault(tmp_path, f"adepthood={_A}")
    vault.path.write_bytes(b"adepthood=\xff\xfe" + _B.encode())
    vault.later()

    assert vault.client_of(_A) == "adepthood"
    err = capsys.readouterr().err
    assert "not UTF-8 text" in err
    assert "0xff" not in err
    _assert_no_secret(err)


def test_an_unreadable_file_keeps_the_set_and_is_logged_once(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A secret mount mid-swap is not an outage, and not a log flood.

    Args:
        tmp_path: Pytest's per-test directory.
        capsys: Captures the notice.
    """
    vault = _Vault(tmp_path, f"adepthood={_A}")
    vault.path.unlink()

    for _ in range(3):
        vault.later()
        assert vault.client_of(_A) == "adepthood"

    err = capsys.readouterr().err
    assert err.count("unreadable") == 1
    assert str(vault.path) not in err

    vault.write(f"adepthood={_C}")
    vault.later()
    assert vault.client_of(_C) == "adepthood"
    vault.path.unlink()
    vault.later()
    vault.verify(_C)
    assert capsys.readouterr().err.count("unreadable") == 1


def test_a_rewrite_that_keeps_the_mtime_is_still_picked_up(tmp_path: Path) -> None:
    """Same size, same mtime, new token: the content hash catches it.

    Args:
        tmp_path: Pytest's per-test directory.
    """
    vault = _Vault(tmp_path, f"adepthood={_A}")
    before = vault.path.stat()
    replacement = f"adepthood={_C[: len(_A)].ljust(len(_A), 'x')}"
    vault.write(replacement)
    os.utime(vault.path, ns=(before.st_atime_ns, before.st_mtime_ns))
    vault.later()

    assert vault.client_of(_A) is None


def test_a_touch_without_a_change_reloads_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A new mtime over the same bytes is not a reload and announces nothing.

    Args:
        tmp_path: Pytest's per-test directory.
        capsys: Captures any notice.
    """
    vault = _Vault(tmp_path, f"adepthood={_A},{_C}")
    vault.path.touch()
    vault.later()
    vault.verify(_A)

    assert capsys.readouterr().err == ""


def test_a_reload_announces_rotation_and_ungated_consumers_as_boot_does(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The rotation window and the ungated notice fire on reload (#895, #1964).

    Args:
        tmp_path: Pytest's per-test directory.
        capsys: Captures the notices.
    """
    vault = _Vault(tmp_path, f"adepthood={_A}@vault:read")
    vault.write(f"adepthood={_A},{_C};openclaw={_B}")
    vault.later()
    vault.verify(_A)

    err = capsys.readouterr().err
    expected = _registry(f"adepthood={_A},{_C};openclaw={_B}")
    rotation = expected.rotation_notice()
    ungated = expected.ungated_notice()
    assert rotation is not None
    assert ungated is not None
    assert rotation in err
    assert ungated in err
    assert "reloaded" in err
    _assert_no_secret(err)


def test_a_reload_carries_grants_exactly_as_the_boot_parse_does(
    tmp_path: Path,
) -> None:
    """Gating, re-gating and un-gating a consumer by editing its suffix (#1964).

    Args:
        tmp_path: Pytest's per-test directory.
    """
    vault = _Vault(tmp_path, f"adepthood={_A}")
    ungated = vault.verify(_A)
    assert ungated is not None
    assert ungated.scopes == [REMOTE_SCOPE, UNGATED_SCOPE]

    gated = f"adepthood={_A}@vault:desk+vault:read"
    vault.write(gated)
    vault.later()
    reloaded = vault.verify(_A)
    booted = asyncio.run(_registry(gated).verify_token(_A))

    assert reloaded is not None
    assert booted is not None
    assert (
        reloaded.scopes == booted.scopes == [REMOTE_SCOPE, "vault:read", "vault:desk"]
    )


def test_the_consumer_names_follow_the_file(tmp_path: Path) -> None:
    """``consumers`` reads the set in force, not the one the process booted with.

    Args:
        tmp_path: Pytest's per-test directory.
    """
    vault = _Vault(tmp_path, f"adepthood={_A}")
    vault.write(f"adepthood={_A};openclaw={_B}")
    vault.later()
    vault.verify(_A)

    assert vault.verifier.consumers == ("adepthood", "openclaw")


def test_an_unknown_baseline_adopts_the_file_at_the_first_poll(
    tmp_path: Path,
) -> None:
    """Without a boot digest the first poll reads the file and serves it.

    Args:
        tmp_path: Pytest's per-test directory.
    """
    path = tmp_path / "consumer_tokens"
    path.write_text(f"adepthood={_C}", encoding="utf-8")
    clock = FakeClock()
    verifier = ReloadingConsumerTokenVerifier(
        path,
        _registry,
        initial=_registry(f"adepthood={_A}"),
        baseline=None,
        clock=clock,
    )
    assert asyncio.run(verifier.verify_token(_A)) is not None

    clock.advance(RELOAD_POLL_SECONDS)

    assert asyncio.run(verifier.verify_token(_A)) is None
    assert asyncio.run(verifier.verify_token(_C)) is not None


def test_the_verifier_never_exposes_a_token_in_its_repr(tmp_path: Path) -> None:
    """Neither the token set nor the file's bytes surface in a repr.

    Args:
        tmp_path: Pytest's per-test directory.
    """
    vault = _Vault(tmp_path, f"adepthood={_A},{_C}")

    _assert_no_secret(repr(vault.verifier))
    _assert_no_secret(repr(vars(vault.verifier)))
