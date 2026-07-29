"""Guard tests for security-motivated dependency pins.

Each pin here answers specific CVEs; the tests guard both the declared
floor and the resolved lock so a future relock cannot regress onto a
vulnerable release.

``mcp`` (issue #862): mcp 1.27.1 carries CVE-2026-52869 —
cross-principal session injection on the Streamable HTTP bearer-token
transport (reachable on the shared dependency: ``creek_mcp``, whose
surface this bot consumes, runs streamable-http with a
``TokenVerifier``). The same release also carries CVE-2026-52870 and
CVE-2026-59950. All three are fixed in mcp 1.28.1. mcp is a direct
dependency, so its floor lives in ``[project].dependencies`` (with the
``<2.0.0`` ceiling intact).

``pyasn1`` (issue #867): pyasn1 0.6.3 carries CVE-2026-59885
(quadratic-time OID-arc decoding — a DoS on hostile BER/DER input) and
CVE-2026-59886 (unbounded big-int construction from REAL exponents —
also a DoS). Both are fixed in pyasn1 0.6.4. pyasn1 is transitive-only:
it enters this graph via google-genai → google-auth → pyasn1-modules
and is never imported directly, so its floor lives in
``[tool.uv].constraint-dependencies`` rather than
``[project].dependencies`` — a uv constraint tightens resolution when
the package is already in the graph without declaring a dependency we
never import (precedent: creek-tools' pyjwt>=2.13.0 constraint,
DEP-003).

``aiohttp`` (issue #978): aiohttp 3.13.5 carries eleven published
advisories — PYSEC-2026-237 and PYSEC-2026-2104 through
PYSEC-2026-2113, aliased to CVE-2026-34993, CVE-2026-47265,
CVE-2026-50269, and CVE-2026-54273 through CVE-2026-54280. aiohttp is
transitive-only via discord.py, which uses it as the REST client for
every Discord call — including the user-supplied attachment downloads
``crawdad.attachments`` performs — and as the gateway websocket
client, so the surface is reachable rather than theoretical. All
eleven are fixed in 3.14.1; 3.14.0 fixes only three of them.
discord.py 2.7.1 declares ``aiohttp<4,>=3.7.4``, so nothing about
discord.py changes, and the floor lives in
``[tool.uv].constraint-dependencies`` alongside pyasn1.

Two independent guards per package:

* **pyproject floor** — the declared specifier must reject the last
  vulnerable release so a future relock cannot resolve back down to it.
* **locked version** — ``uv.lock`` is what CI installs and what
  pip-audit actually inspects, so the resolved entry must already be
  at or above the patched version.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.version import Version

_PACKAGE_ROOT = Path(__file__).resolve().parents[1]
_PYPROJECT = _PACKAGE_ROOT / "pyproject.toml"
_UV_LOCK = _PACKAGE_ROOT / "uv.lock"

#: First mcp release containing the fixes for CVE-2026-52869,
#: CVE-2026-52870, and CVE-2026-59950.
_PATCHED_VERSION = Version("1.28.1")

#: First pyasn1 release containing the fixes for CVE-2026-59885 and
#: CVE-2026-59886.
_PYASN1_PATCHED_VERSION = Version("0.6.4")

#: First aiohttp release with all eleven advisories fixed; 3.14.0
#: fixed only PYSEC-2026-2104, PYSEC-2026-2105, and PYSEC-2026-2106.
_AIOHTTP_PATCHED_VERSION = Version("3.14.1")


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


def _aiohttp_constraint_specifier() -> SpecifierSet:
    """Return the ``aiohttp`` specifier from uv constraint-dependencies.

    Reads ``[tool.uv].constraint-dependencies`` in ``pyproject.toml``,
    the home for floors on transitive-only packages (DEP-003).

    Returns:
        The specifier set attached to the ``aiohttp`` constraint entry.
        Fails the calling test if the ``[tool.uv]`` table or the
        ``aiohttp`` entry is absent.
    """
    with _PYPROJECT.open("rb") as handle:
        pyproject = tomllib.load(handle)
    constraints: list[str] = (
        pyproject.get("tool", {}).get("uv", {}).get("constraint-dependencies", [])
    )
    for entry in constraints:
        requirement = Requirement(entry)
        if requirement.name == "aiohttp":
            return requirement.specifier
    pytest.fail(
        "aiohttp has no entry in [tool.uv].constraint-dependencies of pyproject.toml"
    )


def _locked_aiohttp_version() -> Version:
    """Return the resolved ``aiohttp`` version pinned in ``uv.lock``.

    Returns:
        The ``aiohttp`` version resolved in the lockfile. Fails the
        calling test if the lock has no ``aiohttp`` package entry.
    """
    with _UV_LOCK.open("rb") as handle:
        lock = tomllib.load(handle)
    packages: list[dict[str, object]] = lock["package"]
    for package in packages:
        if package["name"] == "aiohttp":
            return Version(str(package["version"]))
    pytest.fail("aiohttp has no [[package]] entry in uv.lock")


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


def test_aiohttp_floor_rejects_vulnerable_releases() -> None:
    """The constraint excludes 3.13.5 and the partial-fix 3.14.0.

    aiohttp 3.13.5 carries all eleven advisories. 3.14.0 clears only
    three of them, so stopping the floor at ``>=3.14.0`` would still
    ship a reachable-through-discord.py vulnerability; the floor has to
    be ``>=3.14.1``, the first release OSV reports as clean.
    """
    specifier = _aiohttp_constraint_specifier()
    assert "3.13.5" not in specifier, (
        f"aiohttp constraint {specifier!r} admits 3.13.5, the release "
        "carrying PYSEC-2026-237 and PYSEC-2026-2104 through -2113; the "
        "floor must be >=3.14.1"
    )
    assert "3.14.0" not in specifier, (
        f"aiohttp constraint {specifier!r} admits 3.14.0, which still "
        "carries eight of the eleven advisories (PYSEC-2026-237 and "
        "PYSEC-2026-2107 through -2113); the floor must be >=3.14.1, "
        "not >=3.14.0"
    )


def test_aiohttp_floor_accepts_patched_release() -> None:
    """The constraint accepts 3.14.1, the first fully patched release."""
    specifier = _aiohttp_constraint_specifier()
    assert "3.14.1" in specifier, (
        f"aiohttp constraint {specifier!r} rejects 3.14.1; the patched "
        "release itself must satisfy the constraint"
    )


def test_locked_aiohttp_at_or_above_patched_release() -> None:
    """``uv.lock`` resolves aiohttp to >= 3.14.1.

    The lockfile is what ``uv sync`` installs and what pip-audit
    inspects; a correct constraint floor with a stale lock still ships
    the vulnerable 3.13.5.
    """
    locked = _locked_aiohttp_version()
    assert locked >= _AIOHTTP_PATCHED_VERSION, (
        f"uv.lock pins aiohttp {locked}, below the patched "
        f"{_AIOHTTP_PATCHED_VERSION} (PYSEC-2026-237 and PYSEC-2026-2104 "
        "through -2113); pip-audit inspects the lock, so relock after "
        "adding the constraint"
    )
