"""Pinned run and host metadata for a reproducible, content-free report.

A live run must be reproducible from its report: it names the exact model
weights by digest and the exact harness by git sha, and it refuses to start
without both. Host metadata describes capacity (cores, memory, disk) and
nothing that identifies the machine or its paths.
"""

from __future__ import annotations

import socket
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from creek_mcp.bench import HARNESS_VERSION
from creek_mcp.bench.metadata import (
    HostMetadata,
    RunMetadata,
    capture_host,
)

if TYPE_CHECKING:
    from pathlib import Path

_DIGEST = "sha256:" + "a" * 64
_SHA = "0123456789abcdef0123456789abcdef01234567"


def _run(**overrides: object) -> RunMetadata:
    """Build run metadata with valid live defaults, overridden by *overrides*."""
    fields: dict[str, object] = {
        "mode": "live",
        "provider": "ollama",
        "grounding": "default",
        "model_tag": "mistral:7b",
        "digest": _DIGEST,
        "num_ctx": 4096,
        "num_predict": 128,
        "harness_version": HARNESS_VERSION,
        "git_sha": _SHA,
    }
    fields.update(overrides)
    return RunMetadata.model_validate(fields)


def test_live_run_requires_digest() -> None:
    """A live run without a model digest is refused."""
    with pytest.raises(ValidationError, match="digest"):
        _run(digest=None)


def test_live_run_requires_git_sha() -> None:
    """A live run without the harness git sha is refused."""
    with pytest.raises(ValidationError, match="git_sha"):
        _run(git_sha=None)


def test_fake_run_allows_missing_digest() -> None:
    """The hermetic fake mode has no weights to pin."""
    run = _run(mode="fake", provider="fake", digest=None, git_sha=None)
    assert run.digest is None


@pytest.mark.parametrize("digest", ["mistral", "sha256:" + "a" * 63, "A" * 64])
def test_digest_pattern_rejects_bare_tag(digest: str) -> None:
    """Only a full ``sha256:<64 hex>`` digest pins weights."""
    with pytest.raises(ValidationError, match="digest"):
        _run(digest=digest)


def test_license_id_rejects_free_text() -> None:
    """A license *text* (with spaces) can never reach the report."""
    with pytest.raises(ValidationError, match="license_id"):
        _run(license_id="Apache License Version 2.0")


def test_num_ctx_must_be_positive() -> None:
    """The context window the run pins is a positive token count."""
    with pytest.raises(ValidationError, match="num_ctx"):
        _run(num_ctx=0)


def test_metadata_is_frozen_and_closed() -> None:
    """Unknown fields are refused and fields cannot be reassigned."""
    with pytest.raises(ValidationError, match="extra"):
        _run(prompt="anything")
    run = _run()
    with pytest.raises(ValidationError, match="frozen"):
        run.digest = None


def test_host_metadata_has_no_identifying_fields(tmp_path: Path) -> None:
    """Host capture names capacity only — no hostname, no path."""
    host = capture_host(tmp_path)
    assert set(HostMetadata.model_fields) == {
        "cpu_count",
        "cpu_arch",
        "cpu_kind",
        "kernel_release",
        "ram_bytes",
        "disk_free_bytes",
    }
    rendered = host.model_dump_json()
    assert socket.gethostname() not in rendered
    assert str(tmp_path) not in rendered
    assert host.disk_free_bytes is not None
    assert host.disk_free_bytes > 0
    assert host.cpu_kind == "unknown"


def test_capture_host_records_operator_cpu_kind(tmp_path: Path) -> None:
    """The Fly CPU kind cannot be detected locally, so the operator names it."""
    assert capture_host(tmp_path, cpu_kind="shared").cpu_kind == "shared"


def test_capture_host_degrades_unreadable_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unknowable values become ``None`` / ``unknown``, never a crash."""

    def _no_sysconf(name: str) -> int:
        raise ValueError(name)

    monkeypatch.setattr("creek_mcp.bench.metadata.os.sysconf", _no_sysconf)
    monkeypatch.setattr("creek_mcp.bench.metadata.os.cpu_count", lambda: None)
    monkeypatch.setattr(
        "creek_mcp.bench.metadata.platform.release", lambda: "has spaces in it"
    )
    monkeypatch.setattr("creek_mcp.bench.metadata.platform.machine", lambda: "")
    host = capture_host(tmp_path / "missing")
    assert host.ram_bytes is None
    assert host.cpu_count is None
    assert host.kernel_release == "unknown"
    assert host.cpu_arch == "unknown"
    assert host.disk_free_bytes is None
