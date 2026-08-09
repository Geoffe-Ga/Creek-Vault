"""``creek-tools-api`` refuses to start badly, and never binds a socket here (#1074).

A second entry point now binds a listening socket in this repository, and it
authenticates with the same bearer secrets as the first. The ADR's transport
posture is therefore not "the same as MCP's" by coincidence — it is *literally*
the same code, moved to :mod:`creek_mcp.transport_posture` so the two adapters
cannot drift. These tests drive the HTTP CLI through the identical set of
refusals ``tests/test_mcp_remote.py`` already pins for the MCP one: loopback
may serve plaintext, a routable bind without TLS exits before serving, cert and
key come as a pair and must exist, no consumer tokens means no service, and a
weak token exits with the rotation recipe and never the token.

**No test in this module opens a port.** ``serve`` is stubbed with a recorder,
mirroring ``_stub_build_server`` in ``tests/test_mcp_remote.py``: a guard that
fails to fire then fails as ``DID NOT RAISE`` rather than hanging the run on a
real bind. The recorder doubles as the positive assertion — a refusal that
exits *after* serving would still raise ``SystemExit``, so "was ``serve``
called?" is the question that separates the two.

**Two entry points must not fight over one port.** ``creek-tools-mcp
--transport network`` and ``creek-tools-api`` are meant to run side by side on
one host; identical defaults would make the second one fail to bind, at
runtime, with an OS error that says nothing about the collision. Both defaults
are named constants and this module pins that they differ.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Final

import pytest

from creek_mcp import server as server_mod
from creek_mcp.api.openapi import build_openapi
from creek_mcp.httpapi import cli as cli_mod
from creek_mcp.httpapi.auth import build_verifier
from creek_mcp.httpapi.cli import DEFAULT_API_PORT, main
from creek_mcp.remote_auth import CONSUMER_TOKENS_ENV
from creek_mcp.server import DEFAULT_MCP_NETWORK_PORT
from creek_mcp.token_policy import MIN_TOKEN_LEN
from tests.v1_api_support import CONSUMER, STRONG_TOKEN

if TYPE_CHECKING:
    import argparse
    from pathlib import Path

_ARGPARSE_EXIT: Final[int] = 2
"""``ArgumentParser.error``'s exit code — the convention every guard uses."""

_MCP_LEGACY_PORT: Final[int] = 8000
"""The bare literal ``creek_mcp/server.py`` used before it was promoted."""

_LOOPBACK_HOSTS: Final[tuple[str, ...]] = ("127.0.0.1", "localhost", "::1")
_ROUTABLE_HOSTS: Final[tuple[str, ...]] = ("0.0.0.0", "192.168.1.10", "example.com")

# 7 chars, well under the floor. Test literal, not a real credential.
_WEAK_TOKEN: Final[str] = "hunter2"


class _ServeRecorder:
    """Socket-free stand-in for the CLI's serve loop.

    Routed in via :func:`_stub_serve` so a guard bug fails the test as
    ``DID NOT RAISE`` — or as "serve was called" — instead of binding a real
    port and hanging the run.
    """

    def __init__(self) -> None:
        """Start with an empty call log."""
        self.calls: list[tuple[Any, argparse.Namespace]] = []

    def __call__(self, app: Any, args: argparse.Namespace) -> None:
        """Record the app and parsed args instead of serving.

        Args:
            app: The built ASGI application.
            args: The parsed command-line namespace.
        """
        self.calls.append((app, args))


def _stub_serve(monkeypatch: pytest.MonkeyPatch) -> _ServeRecorder:
    """Route ``cli.serve`` to a :class:`_ServeRecorder`; return it.

    Args:
        monkeypatch: The active monkeypatch fixture.

    Returns:
        The recorder, so a test can assert on (or against) the call.
    """
    recorder = _ServeRecorder()
    monkeypatch.setattr(cli_mod, "serve", recorder)
    return recorder


def _configure_tokens(monkeypatch: pytest.MonkeyPatch, value: str | None) -> None:
    """Set or clear ``CREEK_MCP_CONSUMER_TOKENS`` for one test.

    Always explicit: the operator's own exported credentials must never be
    what makes a test pass.

    Args:
        monkeypatch: The active monkeypatch fixture.
        value: The env value, or ``None`` to unset it.
    """
    if value is None:
        monkeypatch.delenv(CONSUMER_TOKENS_ENV, raising=False)
    else:
        monkeypatch.setenv(CONSUMER_TOKENS_ENV, value)


def _valid_tokens() -> str:
    """Return a well-formed ``CREEK_MCP_CONSUMER_TOKENS`` value.

    Returns:
        A single ``consumer=token`` pair clearing the length floor.
    """
    return f"{CONSUMER}={STRONG_TOKEN}"


# 44 chars each. Low-entropy test literals, not real credentials.
_ROTATION_TOKEN_A: Final[str] = "api-test-rotation-token-" + "e" * 20
_ROTATION_TOKEN_B: Final[str] = "api-test-rotation-token-" + "f" * 20


def _rotation_window() -> str:
    """Return a ``CREEK_MCP_CONSUMER_TOKENS`` value with one consumer mid-rotation.

    Returns:
        A single consumer holding two currently-valid tokens (#895).
    """
    return f"{CONSUMER}={_ROTATION_TOKEN_A},{_ROTATION_TOKEN_B}"


# --------------------------------------------------------------------------- #
# Ports
# --------------------------------------------------------------------------- #


def test_the_two_adapters_default_to_different_ports() -> None:
    """``creek-tools-api`` and ``creek-tools-mcp`` can run side by side.

    Identical defaults would surface as an ``EADDRINUSE`` at startup of
    whichever process lost the race — an error that names a port and says
    nothing about the two entry points colliding.
    """
    assert DEFAULT_API_PORT != DEFAULT_MCP_NETWORK_PORT
    assert DEFAULT_API_PORT != _MCP_LEGACY_PORT


def test_the_mcp_port_promotion_kept_its_value() -> None:
    """Promoting ``8000`` to a named constant is a rename, not a move.

    Anyone with ``--port`` unset in a systemd unit or a compose file is
    relying on the old literal; the constant has to still be it.
    """
    assert DEFAULT_MCP_NETWORK_PORT == _MCP_LEGACY_PORT


def test_the_api_default_port_is_a_real_port() -> None:
    """Sanity bound, so the constant cannot be a placeholder."""
    assert 1024 <= DEFAULT_API_PORT <= 65535


# --------------------------------------------------------------------------- #
# Transport posture
# --------------------------------------------------------------------------- #


def test_the_default_host_is_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    """``creek-tools-api`` with no ``--host`` binds ``127.0.0.1``, never ``0.0.0.0``.

    A wildcard default would put an authenticated vault surface on every
    interface the moment somebody ran the command to "try it out".

    Args:
        monkeypatch: Configures tokens and stubs the serve loop.
    """
    _configure_tokens(monkeypatch, _valid_tokens())
    recorder = _stub_serve(monkeypatch)
    main([])
    assert len(recorder.calls) == 1
    assert recorder.calls[0][1].host == "127.0.0.1"
    assert recorder.calls[0][1].host != "0.0.0.0"


@pytest.mark.parametrize("host", _LOOPBACK_HOSTS)
def test_a_loopback_bind_serves_without_tls(
    monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    """Loopback traffic never leaves the machine, so plaintext is acceptable.

    Args:
        monkeypatch: Configures tokens and stubs the serve loop.
        host: The loopback host under test.
    """
    _configure_tokens(monkeypatch, _valid_tokens())
    recorder = _stub_serve(monkeypatch)
    main(["--host", host])
    assert len(recorder.calls) == 1
    assert recorder.calls[0][1].host == host


@pytest.mark.parametrize("host", _ROUTABLE_HOSTS)
def test_a_routable_bind_without_tls_refuses_to_start(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    host: str,
) -> None:
    """Bearer tokens must never transit a routable network in cleartext.

    The refusal names the flags that fix it and never the configured token —
    startup errors land in logs, terminals and process supervisors.

    Args:
        monkeypatch: Configures tokens and stubs the serve loop.
        capsys: Captures the argparse error.
        host: The routable host under test.
    """
    _configure_tokens(monkeypatch, _valid_tokens())
    recorder = _stub_serve(monkeypatch)
    with pytest.raises(SystemExit) as excinfo:
        main(["--host", host])
    assert excinfo.value.code == _ARGPARSE_EXIT
    err = capsys.readouterr().err
    assert "--tls-cert" in err
    assert "--tls-key" in err
    assert STRONG_TOKEN not in err
    assert recorder.calls == []


def test_a_routable_bind_with_tls_serves(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With cert and key, the routable bind is allowed and the paths reach serve.

    Args:
        monkeypatch: Configures tokens and stubs the serve loop.
        tmp_path: Holds the fixture cert/key files.
    """
    _configure_tokens(monkeypatch, _valid_tokens())
    recorder = _stub_serve(monkeypatch)
    cert = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"
    cert.write_text("dummy-cert")  # fixture material, not a real credential
    key.write_text("dummy-key")
    main(["--host", "0.0.0.0", "--tls-cert", str(cert), "--tls-key", str(key)])
    assert len(recorder.calls) == 1
    args = recorder.calls[0][1]
    assert args.tls_cert == cert
    assert args.tls_key == key


@pytest.mark.parametrize("omitted", ["--tls-key", "--tls-cert"])
def test_tls_cert_and_key_are_required_together(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    omitted: str,
) -> None:
    """Half a TLS configuration is refused, in both directions.

    Args:
        monkeypatch: Configures tokens and stubs the serve loop.
        capsys: Captures the argparse error.
        tmp_path: Holds the fixture file.
        omitted: The flag left out of the invocation.
    """
    _configure_tokens(monkeypatch, _valid_tokens())
    recorder = _stub_serve(monkeypatch)
    supplied = "--tls-cert" if omitted == "--tls-key" else "--tls-key"
    path = tmp_path / "material.pem"
    path.write_text("dummy")  # fixture material, not a real credential
    with pytest.raises(SystemExit) as excinfo:
        main(["--host", "127.0.0.1", supplied, str(path)])
    assert excinfo.value.code == _ARGPARSE_EXIT
    assert omitted in capsys.readouterr().err
    assert recorder.calls == []


def test_a_missing_tls_file_is_named(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """A cert path that does not exist fails at startup, naming the file.

    Discovering this at first-TLS-handshake instead would mean the process
    had already reported itself healthy.

    Args:
        monkeypatch: Configures tokens and stubs the serve loop.
        capsys: Captures the argparse error.
        tmp_path: Provides the nonexistent paths.
    """
    _configure_tokens(monkeypatch, _valid_tokens())
    recorder = _stub_serve(monkeypatch)
    missing_cert = tmp_path / "no-cert.pem"
    missing_key = tmp_path / "no-key.pem"
    with pytest.raises(SystemExit) as excinfo:
        main(
            [
                "--host",
                "127.0.0.1",
                "--tls-cert",
                str(missing_cert),
                "--tls-key",
                str(missing_key),
            ]
        )
    assert excinfo.value.code == _ARGPARSE_EXIT
    err = capsys.readouterr().err
    assert "no-cert.pem" in err
    assert "file not found" in err.lower()
    assert recorder.calls == []


# --------------------------------------------------------------------------- #
# Credentials
# --------------------------------------------------------------------------- #


def test_no_configured_tokens_means_no_service(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """There is no anonymous access, so an unconfigured server does not start.

    Refusing at startup rather than at first request is the difference between
    an operator seeing the problem and an unauthenticated vault surface being
    live until somebody notices.

    Args:
        monkeypatch: Clears the token env and stubs the serve loop.
        capsys: Captures the argparse error.
    """
    _configure_tokens(monkeypatch, None)
    recorder = _stub_serve(monkeypatch)
    with pytest.raises(SystemExit) as excinfo:
        main(["--host", "127.0.0.1"])
    assert excinfo.value.code == _ARGPARSE_EXIT
    err = capsys.readouterr().err
    assert CONSUMER_TOKENS_ENV in err
    assert "authentication" in err.lower()
    assert recorder.calls == []


def test_a_weak_token_exits_with_the_rotation_recipe(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A sub-floor token is a startup failure, and the message never echoes it.

    Args:
        monkeypatch: Configures a weak token and stubs the serve loop.
        capsys: Captures the argparse error.
    """
    _configure_tokens(monkeypatch, f"{CONSUMER}={_WEAK_TOKEN}")
    recorder = _stub_serve(monkeypatch)
    with pytest.raises(SystemExit) as excinfo:
        main(["--host", "127.0.0.1"])
    assert excinfo.value.code == _ARGPARSE_EXIT
    err = capsys.readouterr().err
    assert CONSUMER in err
    assert str(MIN_TOKEN_LEN) in err
    assert "secrets.token_urlsafe(32)" in err
    assert _WEAK_TOKEN not in err  # NEVER echo the token value
    assert recorder.calls == []


def test_a_token_on_the_floor_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    """The floor is a floor, not a ceiling — exactly ``MIN_TOKEN_LEN`` serves.

    Derived from the shared constant rather than from a literal ``32``, so a
    second, lower floor inside the HTTP adapter would not go unnoticed.

    Args:
        monkeypatch: Configures a boundary-length token and stubs serve.
    """
    boundary = "z" * MIN_TOKEN_LEN  # test literal, not a real credential
    _configure_tokens(monkeypatch, f"{CONSUMER}={boundary}")
    recorder = _stub_serve(monkeypatch)
    main(["--host", "127.0.0.1"])
    assert len(recorder.calls) == 1


# --------------------------------------------------------------------------- #
# --print-openapi
# --------------------------------------------------------------------------- #


def test_print_openapi_writes_the_document_and_does_not_serve(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--print-openapi`` emits valid JSON on stdout and binds nothing.

    Args:
        monkeypatch: Configures tokens and stubs the serve loop.
        capsys: Captures stdout.
    """
    _configure_tokens(monkeypatch, _valid_tokens())
    recorder = _stub_serve(monkeypatch)
    main(["--print-openapi"])
    printed: dict[str, Any] = json.loads(capsys.readouterr().out)
    assert printed == build_openapi()
    assert recorder.calls == []


def test_print_openapi_needs_no_credentials(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Printing the published contract is not a privileged operation.

    It reads no vault and opens no socket, so requiring
    ``CREEK_MCP_CONSUMER_TOKENS`` to dump the document would only mean that
    generating a client SDK needed production credentials.

    Args:
        monkeypatch: Clears the token env and stubs the serve loop.
        capsys: Captures stdout.
    """
    _configure_tokens(monkeypatch, None)
    recorder = _stub_serve(monkeypatch)
    main(["--print-openapi"])
    assert json.loads(capsys.readouterr().out)["openapi"].startswith("3.")
    assert recorder.calls == []


def test_print_openapi_writes_nothing_to_stderr(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The dump is machine-readable: stdout only, so a pipe stays clean.

    Args:
        monkeypatch: Configures tokens and stubs the serve loop.
        capsys: Captures both streams.
    """
    _configure_tokens(monkeypatch, _valid_tokens())
    _stub_serve(monkeypatch)
    main(["--print-openapi"])
    assert capsys.readouterr().err == ""


# --------------------------------------------------------------------------- #
# Rotation-window startup notice (#895)
#
# A window has to be closed again, and an operator who cannot see one is open
# will not close it. The notice goes to **stderr**: stdout on this entry point
# carries the ``--print-openapi`` document, which consumers pipe into code
# generators, so an operator message there breaks every one of those pipes.
# --------------------------------------------------------------------------- #


def test_serving_a_rotation_window_announces_it_on_stderr(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The operator is told which consumers are mid-rotation, and on which stream.

    Compared against the verifier's own ``rotation_notice()`` for the same
    configuration, so the CLI is pinned to emitting the shared message rather
    than a second wording free to drift from it.

    Args:
        monkeypatch: Configures a rotation window and stubs the serve loop.
        capsys: Captures both streams.
    """
    _configure_tokens(monkeypatch, _rotation_window())
    recorder = _stub_serve(monkeypatch)
    main(["--host", "127.0.0.1"])
    captured = capsys.readouterr()

    assert len(recorder.calls) == 1  # it announced and served, not instead of serving
    expected = build_verifier(
        {CONSUMER_TOKENS_ENV: _rotation_window()}
    ).rotation_notice()
    assert expected is not None
    assert expected in captured.err
    assert CONSUMER in captured.err  # the consumer mid-rotation is named
    assert "2" in captured.err  # ...with its token count
    assert _ROTATION_TOKEN_A not in captured.err  # NEVER echo a token value
    assert _ROTATION_TOKEN_B not in captured.err
    assert captured.out == ""  # stdout is the contract's channel


def test_serving_without_a_rotation_window_announces_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Steady state is silent, so the notice means something when it appears.

    Args:
        monkeypatch: Configures one token per consumer and stubs the serve loop.
        capsys: Captures both streams.
    """
    _configure_tokens(monkeypatch, _valid_tokens())
    recorder = _stub_serve(monkeypatch)
    main(["--host", "127.0.0.1"])
    captured = capsys.readouterr()

    assert len(recorder.calls) == 1
    assert captured.err == ""


def test_print_openapi_stdout_stays_pure_json_during_a_rotation_window(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An open window must not contaminate the machine-readable dump.

    ``--print-openapi`` is what a consumer pipes into a client generator. A
    notice printed to stdout would make the document unparseable exactly for
    the operators who are mid-rotation — the ones least able to afford a second
    broken thing.

    Args:
        monkeypatch: Configures a rotation window and stubs the serve loop.
        capsys: Captures stdout.
    """
    _configure_tokens(monkeypatch, _rotation_window())
    recorder = _stub_serve(monkeypatch)
    main(["--print-openapi"])
    out = capsys.readouterr().out

    assert json.loads(out) == build_openapi()
    assert _ROTATION_TOKEN_A not in out
    assert _ROTATION_TOKEN_B not in out
    assert recorder.calls == []


# --------------------------------------------------------------------------- #
# The posture gate is the shared one
# --------------------------------------------------------------------------- #


def test_the_cli_uses_the_shared_transport_posture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The HTTP CLI calls :mod:`creek_mcp.transport_posture`, not a copy of it.

    Behavioural proof to go with the AST guard in
    ``tests/test_v1_api_structure.py``: replacing the shared gate makes the
    HTTP CLI's refusal disappear. If it had its own copy, the routable bind
    below would still be refused and this test would fail.

    Args:
        monkeypatch: Neutralises the shared gate and stubs the serve loop.
    """
    monkeypatch.setattr(
        cli_mod, "require_transport_confidentiality", lambda _parser, _args: None
    )
    _configure_tokens(monkeypatch, _valid_tokens())
    recorder = _stub_serve(monkeypatch)
    main(["--host", "0.0.0.0"])
    assert len(recorder.calls) == 1


def test_the_mcp_server_still_exposes_its_posture_aliases() -> None:
    """``server._is_loopback`` / ``_require_transport_confidentiality`` survive.

    The extraction is a pure move, so the private names stay as aliases and
    the MCP suite's pins keep passing unedited. A diff to
    ``tests/test_mcp_remote.py`` across this change would itself be the
    evidence that the move was not a move.
    """
    assert server_mod._is_loopback("127.0.0.1") is True
    assert server_mod._is_loopback("example.com") is False
    assert callable(server_mod._require_transport_confidentiality)
