"""Model-package manifest and weights verifier (#1849).

The manifest pins every fact a reproducible local model needs — runtime
version and digest, model tag and blob digest, quantization, size and licence
— and the loader refuses any manifest that leaves one of them open. The
verifier streams the weights and never reports a partial or substituted blob
as verified.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from creek_mcp import model_package
from creek_mcp.model_package import (
    APPROVED_MODEL_LICENSES,
    MODEL_PACKAGE_FILE_ENV,
    ModelPackageError,
    ModelPackageManifest,
    ModelVerification,
    configured_manifest,
    load_manifest,
    main,
    verify_weights,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from io import BufferedReader

_BLOB = bytes(range(256)) * 16
_LICENSE = "Apache-2.0"
_APPROVED = frozenset({_LICENSE})


def _fields(blob: bytes = _BLOB) -> dict[str, Any]:
    """Return a complete, fully pinned manifest body for *blob*."""
    return {
        "schema_version": 1,
        "runtime_name": "creek-test-runtime",
        "runtime_version": "0.0.1",
        "runtime_digest": "c" * 64,
        "model_name": "creek-test-model:q4",
        "model_blob_sha256": hashlib.sha256(blob).hexdigest(),
        "runtime_inventory_digest": "d" * 64,
        "quantization": "Q4_K_M",
        "parameter_count": 1_000_000,
        "size_bytes": len(blob),
        "license_spdx": _LICENSE,
        "license_url": "https://example.test/LICENSE",
    }


def _write(tmp_path: Path, fields: dict[str, Any]) -> Path:
    """Write *fields* as a manifest file and return its path."""
    path = tmp_path / "model-package.json"
    path.write_text(json.dumps(fields), encoding="utf-8")
    return path


def _manifest(blob: bytes = _BLOB) -> ModelPackageManifest:
    """Return a validated manifest for *blob*."""
    return ModelPackageManifest.model_validate(_fields(blob))


@pytest.mark.parametrize("field", ["model_blob_sha256", "runtime_inventory_digest"])
@pytest.mark.parametrize(
    "value", [None, "abc", "g" * 64, "A" * 64, "sha256:" + "a" * 64]
)
def test_manifest_refuses_model_without_sha256_digest(
    tmp_path: Path, field: str, value: str | None
) -> None:
    """A missing, short, non-hex or prefixed digest is not a pin."""
    fields = _fields()
    if value is None:
        del fields[field]
    else:
        fields[field] = value

    with pytest.raises(ModelPackageError, match=field):
        load_manifest(_write(tmp_path, fields), approved_licenses=_APPROVED)


@pytest.mark.parametrize(
    "field",
    [
        "quantization",
        "license_spdx",
        "license_url",
        "runtime_digest",
        "runtime_name",
        "parameter_count",
        "size_bytes",
        "schema_version",
    ],
)
def test_manifest_refuses_missing_quantization_or_license(
    tmp_path: Path, field: str
) -> None:
    """Every pinned field is required; there are no defaults to fall back on."""
    fields = _fields()
    del fields[field]

    with pytest.raises(ModelPackageError, match=field):
        load_manifest(_write(tmp_path, fields), approved_licenses=_APPROVED)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("unexpected", "extra"),
        ("parameter_count", 0),
        ("size_bytes", -1),
        ("size_bytes", "4096"),
        ("parameter_count", True),
        ("schema_version", 2),
        ("license_url", "http://example.test/LICENSE"),
        ("license_url", "https:///LICENSE"),
        ("license_spdx", "Apache 2.0"),
        ("quantization", ""),
    ],
)
def test_manifest_refuses_malformed_or_unknown_fields(
    tmp_path: Path, field: str, value: object
) -> None:
    """Unknown keys, coerced strings and non-HTTPS licence URLs are refused."""
    fields = _fields()
    fields[field] = value

    with pytest.raises(ModelPackageError, match=field):
        load_manifest(_write(tmp_path, fields), approved_licenses=_APPROVED)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("model_name", "creek-test-model"),
        ("model_name", "creek-test-model:latest"),
        ("model_name", "creek-test-model:LATEST"),
        ("model_name", "registry.test:5000/ns/creek-test-model"),
        ("model_name", "creek-test-model:"),
        ("model_name", "creek test model:q4"),
        ("runtime_version", "latest"),
        ("runtime_version", "Latest"),
        ("runtime_version", ""),
        ("runtime_version", "0.0 .1"),
    ],
)
def test_manifest_refuses_unpinned_tag_or_runtime_version(
    tmp_path: Path, field: str, value: str
) -> None:
    """A floating tag or ``latest`` runtime cannot be reproduced."""
    fields = _fields()
    fields[field] = value

    with pytest.raises(ModelPackageError, match=field):
        load_manifest(_write(tmp_path, fields), approved_licenses=_APPROVED)


def test_manifest_accepts_a_registry_qualified_pinned_tag(tmp_path: Path) -> None:
    """A registry host with a port is not mistaken for the tag separator."""
    fields = _fields()
    fields["model_name"] = "registry.test:5000/ns/creek-test-model:q4"

    manifest = load_manifest(_write(tmp_path, fields), approved_licenses=_APPROVED)

    assert manifest.model_name == "registry.test:5000/ns/creek-test-model:q4"


def test_manifest_refuses_license_outside_allowlist(tmp_path: Path) -> None:
    """The licence list starts empty: the owner must approve before any use."""
    path = _write(tmp_path, _fields())

    assert frozenset() == APPROVED_MODEL_LICENSES
    with pytest.raises(ModelPackageError, match="license"):
        load_manifest(path)
    with pytest.raises(ModelPackageError, match="license"):
        load_manifest(path, approved_licenses=frozenset({"MIT"}))

    manifest = load_manifest(path, approved_licenses=_APPROVED)

    assert manifest.size_bytes == len(_BLOB)
    assert manifest.license_spdx == _LICENSE


def test_manifest_refuses_unreadable_or_non_json_file(tmp_path: Path) -> None:
    """A missing or garbled file is refused without echoing its content."""
    with pytest.raises(ModelPackageError):
        load_manifest(tmp_path / "absent.json", approved_licenses=_APPROVED)
    garbled = tmp_path / "garbled.json"
    garbled.write_text("PRIVATE-NOTE {", encoding="utf-8")

    with pytest.raises(ModelPackageError) as caught:
        load_manifest(garbled, approved_licenses=_APPROVED)

    assert "PRIVATE-NOTE" not in str(caught.value)


def test_manifest_is_frozen() -> None:
    """A loaded pin cannot be edited in memory after validation."""
    manifest = _manifest()

    with pytest.raises(ValueError, match="frozen"):
        manifest.size_bytes = 1


def test_verify_weights_rejects_truncated_blob(tmp_path: Path) -> None:
    """A half-written download is never verified."""
    weights = tmp_path / "weights.gguf"
    weights.write_bytes(_BLOB[: len(_BLOB) // 2])

    assert verify_weights(weights, _manifest()) is ModelVerification.SIZE_MISMATCH
    assert (
        verify_weights(tmp_path / "absent.gguf", _manifest())
        is ModelVerification.MISSING
    )
    assert verify_weights(tmp_path, _manifest()) is ModelVerification.MISSING


def test_verify_weights_rejects_digest_mismatch_with_same_size(
    tmp_path: Path,
) -> None:
    """A substituted blob of identical size fails on its digest."""
    weights = tmp_path / "weights.gguf"
    tampered = bytearray(_BLOB)
    tampered[100] ^= 0xFF
    weights.write_bytes(bytes(tampered))

    assert verify_weights(weights, _manifest()) is ModelVerification.DIGEST_MISMATCH


def test_verify_weights_accepts_exact_blob_and_detects_restart_corruption(
    tmp_path: Path,
) -> None:
    """Verified weights stay verified across restarts until a byte changes."""
    weights = tmp_path / "weights.gguf"
    weights.write_bytes(_BLOB)
    manifest = _manifest()

    assert verify_weights(weights, manifest) is ModelVerification.VERIFIED
    assert verify_weights(weights, manifest) is ModelVerification.VERIFIED

    corrupted = bytearray(_BLOB)
    corrupted[-1] ^= 0x01
    weights.write_bytes(bytes(corrupted))

    assert verify_weights(weights, manifest) is ModelVerification.DIGEST_MISMATCH


def test_verify_weights_streams_in_chunks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Weights are hashed in bounded reads, never loaded whole."""
    weights = tmp_path / "weights.gguf"
    weights.write_bytes(_BLOB)
    chunk = 97
    monkeypatch.setattr(model_package, "_CHUNK_BYTES", chunk)
    sizes: list[int] = []
    real_open: Callable[..., BufferedReader] = Path.open

    class _Recorder:
        """Wrap a binary handle and record every requested read size."""

        def __init__(self, handle: BufferedReader) -> None:
            self._handle = handle

        def __enter__(self) -> _Recorder:
            return self

        def __exit__(self, *_exc: object) -> None:
            self._handle.close()

        def read(self, size: int = -1) -> bytes:
            sizes.append(size)
            return self._handle.read(size)

    monkeypatch.setattr(
        Path, "open", lambda self, *a, **k: _Recorder(real_open(self, *a, **k))
    )

    assert verify_weights(weights, _manifest()) is ModelVerification.VERIFIED
    assert len(sizes) > len(_BLOB) // chunk
    assert all(0 < size <= chunk for size in sizes)


def test_verify_weights_fails_closed_when_the_blob_changes_while_hashing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The streamed byte count is checked, not only the pre-read ``stat``."""
    weights = tmp_path / "weights.gguf"
    weights.write_bytes(_BLOB)
    real_open: Callable[..., BufferedReader] = Path.open

    def _shrinking_open(self: Path, *args: object, **kwargs: object) -> object:
        with real_open(self, "wb") as truncate:
            truncate.write(_BLOB[:10])
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", _shrinking_open)

    assert verify_weights(weights, _manifest()) is ModelVerification.SIZE_MISMATCH


def test_verify_weights_reports_missing_when_the_blob_cannot_be_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An I/O error mid-verification is never treated as verified."""
    weights = tmp_path / "weights.gguf"
    weights.write_bytes(_BLOB)

    def _unreadable(self: Path, *args: object, **kwargs: object) -> object:
        raise PermissionError(self)

    monkeypatch.setattr(Path, "open", _unreadable)

    assert verify_weights(weights, _manifest()) is ModelVerification.MISSING


def test_configured_manifest_none_when_env_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No manifest env means no pin; a set env is loaded and validated."""
    monkeypatch.delenv(MODEL_PACKAGE_FILE_ENV, raising=False)

    assert configured_manifest() is None
    assert configured_manifest({MODEL_PACKAGE_FILE_ENV: ""}) is None

    path = _write(tmp_path, _fields())
    with pytest.raises(ModelPackageError, match="license"):
        configured_manifest({MODEL_PACKAGE_FILE_ENV: str(path)})

    monkeypatch.setattr(model_package, "APPROVED_MODEL_LICENSES", _APPROVED)
    monkeypatch.setenv(MODEL_PACKAGE_FILE_ENV, str(path))

    loaded = configured_manifest()

    assert loaded is not None
    assert loaded.model_name == "creek-test-model:q4"


def test_verify_cli_exit_codes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The verify hook prints one status and exits 0 only when verified."""
    weights = tmp_path / "weights.gguf"
    weights.write_bytes(_BLOB)
    monkeypatch.setattr(model_package, "APPROVED_MODEL_LICENSES", _APPROVED)
    monkeypatch.setenv(MODEL_PACKAGE_FILE_ENV, str(_write(tmp_path, _fields())))

    with pytest.raises(SystemExit) as ok:
        main(["verify", "--weights", str(weights)])
    assert ok.value.code == 0
    assert capsys.readouterr().out == "verified\n"

    weights.write_bytes(_BLOB[:-1] + b"\x00")
    with pytest.raises(SystemExit) as bad:
        main(["verify", "--weights", str(weights)])
    assert bad.value.code == model_package.VERIFY_FAILED_EXIT_CODE
    assert bad.value.code != 0
    assert capsys.readouterr().out == "digest-mismatch\n"


@pytest.mark.parametrize("env_value", [None, "/nonexistent/model-package.json"])
def test_verify_cli_without_a_valid_manifest_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    env_value: str | None,
) -> None:
    """No pin (or an unloadable one) can never verify any weights."""
    weights = tmp_path / "weights.gguf"
    weights.write_bytes(_BLOB)
    if env_value is None:
        monkeypatch.delenv(MODEL_PACKAGE_FILE_ENV, raising=False)
    else:
        monkeypatch.setenv(MODEL_PACKAGE_FILE_ENV, env_value)

    with pytest.raises(SystemExit) as caught:
        main(["verify", "--weights", str(weights)])

    assert caught.value.code == model_package.VERIFY_FAILED_EXIT_CODE
    assert capsys.readouterr().out == "missing\n"


def test_verify_weights_reports_missing_when_stat_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A blob that vanishes between checks is missing, never verified."""
    weights = tmp_path / "weights.gguf"
    weights.write_bytes(_BLOB)

    def _gone(self: Path, *args: object, **kwargs: object) -> object:
        raise FileNotFoundError(self)

    monkeypatch.setattr(Path, "stat", _gone)

    assert verify_weights(weights, _manifest()) is ModelVerification.MISSING
