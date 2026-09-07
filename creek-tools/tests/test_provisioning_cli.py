"""Runnable provisioning API wiring and fail-closed startup tests (#1768)."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest
from starlette.testclient import TestClient

from creek_mcp.provisioning import cli

if TYPE_CHECKING:
    from pathlib import Path

    from starlette.types import ASGIApp

_TOKEN = "provisioning-cli-test-token-" + "z" * 32


def test_cli_serves_the_authenticated_app_without_secret_arguments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The installed entry point wires a mounted registry and durable database."""
    token_file = tmp_path / "consumer_tokens"
    token_file.write_text(f"adepthood={_TOKEN}\n", encoding="utf-8")
    database = tmp_path / "state" / "jobs.sqlite3"
    captured: dict[str, object] = {}

    def fake_run(app: object, **kwargs: object) -> None:
        captured["app"] = app
        captured["kwargs"] = kwargs

    monkeypatch.setattr(cli.uvicorn, "run", fake_run)
    cli.main(
        [
            "--database",
            str(database),
            "--consumer-tokens-file",
            str(token_file),
        ]
    )

    app = cast("ASGIApp", captured["app"])
    with TestClient(app) as client:
        response = client.post(
            "/control/v1/activations",
            headers={"Authorization": f"Bearer {_TOKEN}"},
            json={
                "activation_id": "activation-cli",
                "consumer_identity": "adepthood",
            },
        )

    assert response.status_code == 202
    assert database.is_file()
    assert _TOKEN not in repr(captured)


def test_cli_refuses_a_routable_plaintext_bind_before_opening_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bearer auth is never served on a routable plaintext socket."""
    served = False

    def fake_run(*args: object, **kwargs: object) -> None:
        nonlocal served
        served = True

    monkeypatch.setattr(cli.uvicorn, "run", fake_run)
    with pytest.raises(SystemExit) as caught:
        cli.main(
            [
                "--database",
                str(tmp_path / "jobs.sqlite3"),
                "--consumer-tokens-file",
                str(tmp_path / "missing"),
                "--host",
                "0.0.0.0",
            ]
        )

    assert caught.value.code == 2
    assert served is False


def test_cli_refuses_an_unreadable_or_empty_consumer_registry(
    tmp_path: Path,
) -> None:
    """Startup cannot silently create an anonymously reachable service."""
    empty = tmp_path / "empty"
    empty.write_text("\n", encoding="utf-8")

    for token_file in (tmp_path / "missing", empty):
        with pytest.raises(SystemExit) as caught:
            cli.main(
                [
                    "--database",
                    str(tmp_path / "jobs.sqlite3"),
                    "--consumer-tokens-file",
                    str(token_file),
                ]
            )
        assert caught.value.code == 2
