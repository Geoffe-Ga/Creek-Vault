"""Guard tests for security-motivated dependency pins.

Each pin here answers specific CVEs; the tests guard both the declared
floor and the resolved lock so a future relock cannot regress onto a
vulnerable release.

``mcp`` (issue #862): mcp 1.27.1 carries CVE-2026-52869 —
cross-principal session injection on the Streamable HTTP bearer-token
transport. That transport is reachable here: ``creek_mcp`` runs
streamable-http with a ``TokenVerifier``. The same release also carries
CVE-2026-52870 and CVE-2026-59950. All three are fixed in mcp 1.28.1.
mcp is a direct dependency, so its floor lives in
``[project].dependencies`` (with the ``<2.0.0`` ceiling intact).

``pyasn1`` (issue #867): pyasn1 0.6.3 carries CVE-2026-59885
(quadratic-time OID-arc decoding — a DoS on hostile BER/DER input) and
CVE-2026-59886 (unbounded big-int construction from REAL exponents —
also a DoS). Both are fixed in pyasn1 0.6.4. pyasn1 is transitive-only:
it enters this graph via the ``gdrive`` extra → google-auth →
pyasn1-modules and is never imported directly, so its floor lives in
``[tool.uv].constraint-dependencies`` rather than
``[project].dependencies`` — a uv constraint tightens resolution when
the package is already in the graph without declaring a dependency we
never import (precedent: the pyjwt>=2.13.0 constraint, DEP-003).

``setuptools`` (issue #861): setuptools 81.0.0 carries PYSEC-2026-3447
(CVE-2026-59890, GHSA-h35f-9h28-mq5c — path traversal in the
``PackageIndex`` download path). Fixed in setuptools 83.0.0. setuptools
is build/dev tooling — transitive, never a runtime import — so its
floor lives in ``[tool.uv].constraint-dependencies`` rather than
``[project].dependencies`` (DEP-003 precedent).

``torch`` (issue #861): torch 2.12.1 carries PYSEC-2025-194
(CVE-2025-3000, GHSA-rrmf-rvhw-rf47). Fixed in torch 2.13.0. torch is
transitive-only: it enters this graph via the ``embeddings`` extra →
sentence-transformers and is never imported as a declared dependency,
so its floor belongs in ``[tool.uv].constraint-dependencies``.

``cryptography`` (issue #1167): cryptography 49.0.0 carries
PYSEC-2026-3552 (CVE-2026-69247, GHSA-g6cj-pr64-35w5) — PKCS#7
``EnvelopedData`` decryption exposed a Bleichenbacher oracle through
distinguishable errors and timing. OSV records the flaw as introduced
in 44.0.0 and fixed in 50.0.0, so the whole 44-49 band is vulnerable.
cryptography is a *direct* dependency here — the at-rest volume key in
``creek.confidential.keyvault`` imports Argon2id, AES-GCM, and HKDF
unconditionally — so its floor lives in ``[project].dependencies``,
not left to transitive resolution. The same floor is
mirrored into ``[tool.uv].constraint-dependencies`` so the transitive
edge (mcp → pyjwt[crypto]) cannot pull a lower build into the graph.
Both surfaces are guarded independently below: dropping either one
re-opens the regression.

Two independent guards per package:

* **pyproject floor** — the declared specifier must reject the last
  vulnerable release so a future ``uv lock`` relock cannot resolve
  back down to it.
* **locked version** — ``uv.lock`` is what CI installs and what
  pip-audit actually inspects, so the resolved entry must already be
  at or above the patched version.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from packaging.requirements import Requirement
from packaging.version import Version

if TYPE_CHECKING:
    from packaging.specifiers import SpecifierSet

_PACKAGE_ROOT = Path(__file__).resolve().parents[1]
_PYPROJECT = _PACKAGE_ROOT / "pyproject.toml"
_UV_LOCK = _PACKAGE_ROOT / "uv.lock"

#: First mcp release containing the fixes for CVE-2026-52869,
#: CVE-2026-52870, and CVE-2026-59950.
_PATCHED_VERSION = Version("1.28.1")

#: First pyasn1 release containing the fixes for CVE-2026-59885 and
#: CVE-2026-59886.
_PYASN1_PATCHED_VERSION = Version("0.6.4")

#: First setuptools release containing the fix for PYSEC-2026-3447
#: (CVE-2026-59890 / GHSA-h35f-9h28-mq5c).
_SETUPTOOLS_PATCHED_VERSION = Version("83.0.0")

#: First torch release containing the fix for PYSEC-2025-194
#: (CVE-2025-3000 / GHSA-rrmf-rvhw-rf47).
_TORCH_PATCHED_VERSION = Version("2.13.0")

#: First cryptography release containing the fix for PYSEC-2026-3552
#: (CVE-2026-69247 / GHSA-g6cj-pr64-35w5); the advisory range opens at
#: 44.0.0, so every release from 44.0.0 up to 49.x is vulnerable.
_CRYPTOGRAPHY_PATCHED_VERSION = Version("50.0.0")


def _mcp_specifier() -> SpecifierSet:
    """Return the ``mcp`` specifier set from ``[project].dependencies``."""
    with _PYPROJECT.open("rb") as handle:
        pyproject = tomllib.load(handle)
    dependencies: list[str] = pyproject["project"]["dependencies"]
    for entry in dependencies:
        requirement = Requirement(entry)
        if requirement.name == "mcp":
            return requirement.specifier
    pytest.fail("mcp is not declared in [project].dependencies of pyproject.toml")


def _locked_mcp_version() -> Version:
    """Return the resolved ``mcp`` version pinned in ``uv.lock``."""
    with _UV_LOCK.open("rb") as handle:
        lock = tomllib.load(handle)
    packages: list[dict[str, object]] = lock["package"]
    for package in packages:
        if package["name"] == "mcp":
            return Version(str(package["version"]))
    pytest.fail("mcp has no [[package]] entry in uv.lock")


def _pyasn1_constraint_specifier() -> SpecifierSet:
    """Return the ``pyasn1`` specifier from uv constraint-dependencies.

    Reads ``[tool.uv].constraint-dependencies`` in ``pyproject.toml``,
    the home for floors on transitive-only packages (DEP-003).

    Returns:
        The specifier set attached to the ``pyasn1`` constraint entry.
        Fails the calling test if the ``[tool.uv]`` table or the
        ``pyasn1`` entry is absent.
    """
    with _PYPROJECT.open("rb") as handle:
        pyproject = tomllib.load(handle)
    constraints: list[str] = (
        pyproject.get("tool", {}).get("uv", {}).get("constraint-dependencies", [])
    )
    for entry in constraints:
        requirement = Requirement(entry)
        if requirement.name == "pyasn1":
            return requirement.specifier
    pytest.fail(
        "pyasn1 has no entry in [tool.uv].constraint-dependencies of pyproject.toml"
    )


def _locked_pyasn1_version() -> Version:
    """Return the resolved ``pyasn1`` version pinned in ``uv.lock``.

    Returns:
        The ``pyasn1`` version resolved in the lockfile. Fails the
        calling test if the lock has no ``pyasn1`` package entry.
    """
    with _UV_LOCK.open("rb") as handle:
        lock = tomllib.load(handle)
    packages: list[dict[str, object]] = lock["package"]
    for package in packages:
        if package["name"] == "pyasn1":
            return Version(str(package["version"]))
    pytest.fail("pyasn1 has no [[package]] entry in uv.lock")


def _setuptools_constraint_specifier() -> SpecifierSet:
    """Return the ``setuptools`` specifier from uv constraint-dependencies.

    Reads ``[tool.uv].constraint-dependencies`` in ``pyproject.toml``,
    the home for floors on transitive-only packages (DEP-003).

    Returns:
        The specifier set attached to the ``setuptools`` constraint
        entry. Fails the calling test if the ``[tool.uv]`` table or the
        ``setuptools`` entry is absent.
    """
    with _PYPROJECT.open("rb") as handle:
        pyproject = tomllib.load(handle)
    constraints: list[str] = (
        pyproject.get("tool", {}).get("uv", {}).get("constraint-dependencies", [])
    )
    for entry in constraints:
        requirement = Requirement(entry)
        if requirement.name == "setuptools":
            return requirement.specifier
    pytest.fail(
        "setuptools has no entry in [tool.uv].constraint-dependencies of pyproject.toml"
    )


def _locked_setuptools_version() -> Version:
    """Return the resolved ``setuptools`` version pinned in ``uv.lock``.

    Returns:
        The ``setuptools`` version resolved in the lockfile. Fails the
        calling test if the lock has no ``setuptools`` package entry.
    """
    with _UV_LOCK.open("rb") as handle:
        lock = tomllib.load(handle)
    packages: list[dict[str, object]] = lock["package"]
    for package in packages:
        if package["name"] == "setuptools":
            return Version(str(package["version"]))
    pytest.fail("setuptools has no [[package]] entry in uv.lock")


def _torch_constraint_specifier() -> SpecifierSet:
    """Return the ``torch`` specifier from uv constraint-dependencies.

    Reads ``[tool.uv].constraint-dependencies`` in ``pyproject.toml``,
    the home for floors on transitive-only packages (DEP-003).

    Returns:
        The specifier set attached to the ``torch`` constraint entry.
        Fails the calling test if the ``[tool.uv]`` table or the
        ``torch`` entry is absent.
    """
    with _PYPROJECT.open("rb") as handle:
        pyproject = tomllib.load(handle)
    constraints: list[str] = (
        pyproject.get("tool", {}).get("uv", {}).get("constraint-dependencies", [])
    )
    for entry in constraints:
        requirement = Requirement(entry)
        if requirement.name == "torch":
            return requirement.specifier
    pytest.fail(
        "torch has no entry in [tool.uv].constraint-dependencies of pyproject.toml"
    )


def _locked_torch_version() -> Version:
    """Return the resolved ``torch`` version pinned in ``uv.lock``.

    Returns:
        The ``torch`` version resolved in the lockfile. Fails the
        calling test if the lock has no ``torch`` package entry.
    """
    with _UV_LOCK.open("rb") as handle:
        lock = tomllib.load(handle)
    packages: list[dict[str, object]] = lock["package"]
    for package in packages:
        if package["name"] == "torch":
            return Version(str(package["version"]))
    pytest.fail("torch has no [[package]] entry in uv.lock")


def _cryptography_specifier() -> SpecifierSet:
    """Return the ``cryptography`` specifier from ``[project].dependencies``.

    cryptography is a direct dependency of this package —
    ``creek.confidential.keyvault`` imports it unconditionally — so its
    floor belongs alongside the other declared runtime dependencies
    rather than in the uv constraint table.

    Returns:
        The specifier set attached to the ``cryptography`` entry in
        ``[project].dependencies``. Fails the calling test if the entry
        is absent.
    """
    with _PYPROJECT.open("rb") as handle:
        pyproject = tomllib.load(handle)
    dependencies: list[str] = pyproject["project"]["dependencies"]
    for entry in dependencies:
        requirement = Requirement(entry)
        if requirement.name == "cryptography":
            return requirement.specifier
    pytest.fail(
        "cryptography is not declared in [project].dependencies of pyproject.toml"
    )


def _cryptography_constraint_specifier() -> SpecifierSet:
    """Return the ``cryptography`` specifier from uv constraint-dependencies.

    Reads ``[tool.uv].constraint-dependencies`` in ``pyproject.toml``.
    cryptography also arrives transitively (mcp → pyjwt[crypto]), so the
    direct floor is mirrored here to keep resolution from pulling a
    lower build into the graph (DEP-003).

    Returns:
        The specifier set attached to the ``cryptography`` constraint
        entry. Fails the calling test if the ``[tool.uv]`` table or the
        ``cryptography`` entry is absent.
    """
    with _PYPROJECT.open("rb") as handle:
        pyproject = tomllib.load(handle)
    constraints: list[str] = (
        pyproject.get("tool", {}).get("uv", {}).get("constraint-dependencies", [])
    )
    for entry in constraints:
        requirement = Requirement(entry)
        if requirement.name == "cryptography":
            return requirement.specifier
    pytest.fail(
        "cryptography has no entry in [tool.uv].constraint-dependencies of "
        "pyproject.toml"
    )


def _locked_cryptography_version() -> Version:
    """Return the resolved ``cryptography`` version pinned in ``uv.lock``.

    Returns:
        The ``cryptography`` version resolved in the lockfile. Fails the
        calling test if the lock has no ``cryptography`` package entry.
    """
    with _UV_LOCK.open("rb") as handle:
        lock = tomllib.load(handle)
    packages: list[dict[str, object]] = lock["package"]
    for package in packages:
        if package["name"] == "cryptography":
            return Version(str(package["version"]))
    pytest.fail("cryptography has no [[package]] entry in uv.lock")


def test_mcp_floor_rejects_last_vulnerable_range() -> None:
    """The pyproject floor excludes 1.28.0 (and everything below it).

    A ``>=1.0.0`` floor would let a relock resolve to 1.27.1, which is
    vulnerable to CVE-2026-52869 / CVE-2026-52870 / CVE-2026-59950. The
    specifier must genuinely be ``>=1.28.1``.
    """
    specifier = _mcp_specifier()
    assert "1.28.0" not in specifier, (
        f"mcp specifier {specifier!r} admits 1.28.0; the floor must be "
        ">=1.28.1 so relocks cannot resolve below the CVE-patched release"
    )
    assert "1.27.1" not in specifier, (
        f"mcp specifier {specifier!r} admits 1.27.1, the release carrying "
        "CVE-2026-52869 / CVE-2026-52870 / CVE-2026-59950"
    )


def test_mcp_ceiling_rejects_next_major() -> None:
    """The ``<2.0.0`` ceiling survives the floor bump.

    Raising the floor must not silently drop the major-version ceiling
    that shields us from mcp 2.x breaking changes.
    """
    specifier = _mcp_specifier()
    assert "2.0.0" not in specifier, (
        f"mcp specifier {specifier!r} admits 2.0.0; the <2.0.0 ceiling "
        "must remain in place"
    )


def test_mcp_floor_accepts_patched_release() -> None:
    """The specifier accepts 1.28.1, the first CVE-patched release."""
    specifier = _mcp_specifier()
    assert "1.28.1" in specifier, (
        f"mcp specifier {specifier!r} rejects 1.28.1; the patched release "
        "itself must satisfy the pin"
    )


def test_locked_mcp_at_or_above_patched_release() -> None:
    """``uv.lock`` resolves mcp to >= 1.28.1.

    The lockfile is what CI installs and what pip-audit inspects; a
    correct pyproject floor with a stale lock still ships the
    vulnerable 1.27.1.
    """
    locked = _locked_mcp_version()
    assert locked >= _PATCHED_VERSION, (
        f"uv.lock pins mcp {locked}, below the CVE-patched "
        f"{_PATCHED_VERSION} (CVE-2026-52869 / CVE-2026-52870 / "
        "CVE-2026-59950); run a relock after raising the pyproject floor"
    )


def test_pyasn1_floor_rejects_last_vulnerable_release() -> None:
    """The constraint excludes 0.6.3, the last vulnerable release.

    pyasn1 0.6.3 carries CVE-2026-59885 (quadratic OID-arc decode DoS)
    and CVE-2026-59886 (REAL-exponent big-int DoS). The constraint must
    genuinely be ``>=0.6.4`` so a relock cannot resolve back onto it.
    """
    specifier = _pyasn1_constraint_specifier()
    assert "0.6.3" not in specifier, (
        f"pyasn1 constraint {specifier!r} admits 0.6.3, the release "
        "carrying CVE-2026-59885 / CVE-2026-59886; the floor must be "
        ">=0.6.4"
    )


def test_pyasn1_floor_accepts_patched_release() -> None:
    """The constraint accepts 0.6.4, the first CVE-patched release."""
    specifier = _pyasn1_constraint_specifier()
    assert "0.6.4" in specifier, (
        f"pyasn1 constraint {specifier!r} rejects 0.6.4; the patched "
        "release itself must satisfy the constraint"
    )


def test_locked_pyasn1_at_or_above_patched_release() -> None:
    """``uv.lock`` resolves pyasn1 to >= 0.6.4.

    The lockfile is what CI installs and what pip-audit inspects; a
    correct constraint floor with a stale lock still ships the
    vulnerable 0.6.3.
    """
    locked = _locked_pyasn1_version()
    assert locked >= _PYASN1_PATCHED_VERSION, (
        f"uv.lock pins pyasn1 {locked}, below the CVE-patched "
        f"{_PYASN1_PATCHED_VERSION} (CVE-2026-59885 / CVE-2026-59886); "
        "pip-audit inspects the lock, so relock after adding the "
        "constraint"
    )


def test_setuptools_floor_rejects_last_vulnerable_release() -> None:
    """The constraint excludes 81.0.0, the previously-locked vulnerable release.

    setuptools 81.0.0 carries PYSEC-2026-3447 (CVE-2026-59890 — path
    traversal in the ``PackageIndex`` download path). The fix lands AT
    83.0.0, so the probe assertion on 82.9999 pins the floor at the
    patch itself: any weakened floor below ``>=83.0.0`` still admits
    vulnerable releases and fails here.
    """
    specifier = _setuptools_constraint_specifier()
    assert "81.0.0" not in specifier, (
        f"setuptools constraint {specifier!r} admits 81.0.0, the release "
        "carrying PYSEC-2026-3447 / CVE-2026-59890; the floor must be "
        ">=83.0.0"
    )
    assert "82.9999" not in specifier, (
        f"setuptools constraint {specifier!r} admits 82.9999; the fix for "
        "PYSEC-2026-3447 / CVE-2026-59890 lands at 83.0.0, so any floor "
        "below >=83.0.0 still admits vulnerable releases"
    )


def test_setuptools_floor_accepts_patched_release() -> None:
    """The constraint accepts 83.0.0, the first CVE-patched release."""
    specifier = _setuptools_constraint_specifier()
    assert "83.0.0" in specifier, (
        f"setuptools constraint {specifier!r} rejects 83.0.0; the patched "
        "release itself must satisfy the constraint"
    )


def test_locked_setuptools_at_or_above_patched_release() -> None:
    """``uv.lock`` resolves setuptools to >= 83.0.0.

    The lockfile is what CI installs and what pip-audit inspects; a
    correct constraint floor with a stale lock still ships the
    vulnerable 81.0.0.
    """
    locked = _locked_setuptools_version()
    assert locked >= _SETUPTOOLS_PATCHED_VERSION, (
        f"uv.lock pins setuptools {locked}, below the CVE-patched "
        f"{_SETUPTOOLS_PATCHED_VERSION} (PYSEC-2026-3447 / "
        "CVE-2026-59890); pip-audit inspects the lock, so relock after "
        "raising the constraint"
    )


def test_torch_floor_rejects_last_vulnerable_release() -> None:
    """The constraint excludes 2.12.1, the previously-locked vulnerable release.

    torch 2.12.1 carries PYSEC-2025-194 (CVE-2025-3000). The fix lands
    AT 2.13.0, so the probe assertion on 2.12.99 pins the floor at the
    patch itself: any weakened floor below ``>=2.13.0`` still admits
    vulnerable releases and fails here.
    """
    specifier = _torch_constraint_specifier()
    assert "2.12.1" not in specifier, (
        f"torch constraint {specifier!r} admits 2.12.1, the release "
        "carrying PYSEC-2025-194 / CVE-2025-3000; the floor must be "
        ">=2.13.0"
    )
    assert "2.12.99" not in specifier, (
        f"torch constraint {specifier!r} admits 2.12.99; the fix for "
        "PYSEC-2025-194 / CVE-2025-3000 lands at 2.13.0, so any floor "
        "below >=2.13.0 still admits vulnerable releases"
    )


def test_torch_floor_accepts_patched_release() -> None:
    """The constraint accepts 2.13.0, the first CVE-patched release."""
    specifier = _torch_constraint_specifier()
    assert "2.13.0" in specifier, (
        f"torch constraint {specifier!r} rejects 2.13.0; the patched "
        "release itself must satisfy the constraint"
    )


def test_locked_torch_at_or_above_patched_release() -> None:
    """``uv.lock`` resolves torch to >= 2.13.0.

    The lockfile is what CI installs and what pip-audit inspects; a
    correct constraint floor with a stale lock still ships the
    vulnerable 2.12.1.
    """
    locked = _locked_torch_version()
    assert locked >= _TORCH_PATCHED_VERSION, (
        f"uv.lock pins torch {locked}, below the CVE-patched "
        f"{_TORCH_PATCHED_VERSION} (PYSEC-2025-194 / CVE-2025-3000); "
        "pip-audit inspects the lock, so relock after adding the "
        "constraint"
    )


def test_cryptography_floor_rejects_last_vulnerable_release() -> None:
    """The dependency floor excludes 49.0.0, the previously-locked release.

    cryptography 49.0.0 carries PYSEC-2026-3552 (CVE-2026-69247 — a
    Bleichenbacher oracle in PKCS#7 ``EnvelopedData`` decryption). The
    fix lands AT 50.0.0, so the probe assertion on 49.9999 pins the
    floor at the patch itself: any weakened floor below ``>=50.0.0``
    still admits vulnerable releases and fails here.
    """
    specifier = _cryptography_specifier()
    assert "49.0.0" not in specifier, (
        f"cryptography specifier {specifier!r} admits 49.0.0, the release "
        "carrying PYSEC-2026-3552 / CVE-2026-69247; the floor must be "
        ">=50.0.0"
    )
    assert "49.9999" not in specifier, (
        f"cryptography specifier {specifier!r} admits 49.9999; the fix for "
        "PYSEC-2026-3552 / CVE-2026-69247 lands at 50.0.0, so any floor "
        "below >=50.0.0 still admits vulnerable releases"
    )


def test_cryptography_floor_accepts_patched_release() -> None:
    """The dependency specifier accepts 50.0.0, the first patched release."""
    specifier = _cryptography_specifier()
    assert "50.0.0" in specifier, (
        f"cryptography specifier {specifier!r} rejects 50.0.0; the patched "
        "release itself must satisfy the pin"
    )


def test_cryptography_constraint_rejects_last_vulnerable_release() -> None:
    """The uv constraint excludes 49.0.0, the previously-locked release.

    The ``[project].dependencies`` floor alone does not bind the
    transitive edge (mcp → pyjwt[crypto]) during resolution, so the
    mirrored constraint has to reject the same band. The probe on
    49.9999 pins it at 50.0.0 rather than anywhere in the vulnerable
    44.0.0 through 49.x range.
    """
    specifier = _cryptography_constraint_specifier()
    assert "49.0.0" not in specifier, (
        f"cryptography constraint {specifier!r} admits 49.0.0, the release "
        "carrying PYSEC-2026-3552 / CVE-2026-69247; the constraint must be "
        ">=50.0.0"
    )
    assert "49.9999" not in specifier, (
        f"cryptography constraint {specifier!r} admits 49.9999; the fix for "
        "PYSEC-2026-3552 / CVE-2026-69247 lands at 50.0.0, so any floor "
        "below >=50.0.0 still admits vulnerable releases"
    )


def test_cryptography_constraint_accepts_patched_release() -> None:
    """The uv constraint accepts 50.0.0, the first patched release."""
    specifier = _cryptography_constraint_specifier()
    assert "50.0.0" in specifier, (
        f"cryptography constraint {specifier!r} rejects 50.0.0; the patched "
        "release itself must satisfy the constraint"
    )


def test_locked_cryptography_at_or_above_patched_release() -> None:
    """``uv.lock`` resolves cryptography to >= 50.0.0.

    The lockfile is what CI installs and what pip-audit inspects; a
    correct pyproject floor with a stale lock still ships the vulnerable
    49.0.0.
    """
    locked = _locked_cryptography_version()
    assert locked >= _CRYPTOGRAPHY_PATCHED_VERSION, (
        f"uv.lock pins cryptography {locked}, below the CVE-patched "
        f"{_CRYPTOGRAPHY_PATCHED_VERSION} (PYSEC-2026-3552 / "
        "CVE-2026-69247); pip-audit inspects the lock, so relock after "
        "raising the floor"
    )
