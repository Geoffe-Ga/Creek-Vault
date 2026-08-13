"""Guard tests for dependency pins the resolver must not undo.

Most pins here answer specific CVEs; the tests guard both the declared
floor and the resolved lock so a future relock cannot regress onto a
vulnerable release. Two pins are not advisories at all — ``rpds-py``
answers a versioning-scheme migration and ``openai`` an HTTP-transport
swap — but both are guarded the same two ways for the same reason.
Read the paragraph for a pin before changing it: the reason a bound
exists decides which half of it is load-bearing.

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

``rpds-py`` (issue #1185): not a CVE. rpds-py abandoned SemVer for
CalVer at 2026.5.1 — the release line runs 0.29.0, 0.30.0, then
2026.5.1 with no 0.31 or 1.0 in between — and raised its
``requires-python`` from ``>=3.10`` to ``>=3.11``. Both locks sat at
0.30.0, eight months stale, because nothing forced them off it:
jsonschema declares ``rpds-py>=0.25.0`` and referencing declares
``rpds-py>=0.7.0``, both open floors, so a bare ``uv lock`` is a no-op.
rpds-py is transitive-only (mcp → jsonschema → rpds-py, and mcp →
jsonschema → referencing → rpds-py) and nothing in this repo imports
it, so its floor lives in ``[tool.uv].constraint-dependencies`` on the
DEP-003 precedent.

The durable hazard is the *idiom*, not the version. A ceiling written
the way every other pin here would write one — ``rpds-py<1.0``, or a
``~=0.30`` compatible-release clause — now excludes every release from
2026 onward, permanently, because 2026.5.1 sorts above 1.0. So the
constraint is a bare CalVer floor with no ceiling, and
``test_rpds_py_constraint_admits_future_calver_releases`` fails if
anyone adds one in the SemVer shape.

``openai`` (issue #1479): the second non-CVE pin — an OSV query for
PyPI/openai returns zero advisories, so read this bound as a
transport hold rather than the security ratchet most of this file
describes. openai 3.0.0 (published 2026-08-12) replaced its
``httpx<1,>=0.23.0`` requirement with ``httpx2<3,>=2.7.0``, and httpx2
is a *separate distribution* rather than a version bump: taking it
also drags httpcore2==2.10.0, truststore>=0.10 and idna>=3.18 into
the graph. This project declares ``httpx>=0.27.0`` directly as well,
so openai 3.x here would mean two independent HTTP stacks in one
environment — not a migration. ``mcp>=1.28.1,<2.0.0`` caps the other
half of the same httpx2 swap (issue #998), so the two majors have to
be adopted jointly or not at all, which is what the ceiling holds
open. openai 2.54.0, the newest 2.x, still requires
``httpx<1,>=0.23.0`` and offers httpx2 only behind an opt-in extra,
so the whole 2.x line is safe; the floor is 2.41.0, the release both
locks already resolve, because a floor records what this project has
run and never a version it has not — and it is asserted from both
ends, admitting 2.41.0 while rejecting the release below it, so
dropping the floor is as red as dropping the ceiling. Unlike every
other pin in this file, openai is declared in
``[project.optional-dependencies].openai`` — it is the cloud-LLM
extra, not a base dependency — which is why ``_openai_specifier()``
below reads a different table from crawdad's namesake, where openai
*is* declared in ``[project].dependencies``.

Two independent guards per package:

* **pyproject floor** — the declared specifier must reject the last
  vulnerable release (for rpds-py, the last SemVer release) so a
  future ``uv lock`` relock cannot resolve back down to it.
* **locked version** — ``uv.lock`` is what CI installs and what
  pip-audit actually inspects, so the resolved entry must already be
  at or above the patched version.

The file also carries the *undeclared-dependency* guard (issue #1123).
``anyio`` motivated it: ``creek_mcp.httpapi.middleware.limits`` imports
``Semaphore``, ``WouldBlock`` and ``fail_after`` from it at module scope,
yet the package appeared nowhere in ``pyproject.toml`` — it worked only
because ``starlette`` happened to drag it in, so a starlette release that
stopped depending on anyio would have broken the ``/v1`` limits
middleware with no lockfile signal at all. Rather than fix that one
import and wait for the next, ``test_every_import_time_module_is_declared``
walks every import executed when ``creek`` or ``creek_mcp`` is imported
and insists something in ``[project]`` declares it.

Import-*time* is the whole of the rule, and it is what separates a
defect from a design. ``creek.clean.semantic_dedup`` does ``import
faiss`` inside a function behind a probed, memoised availability check
and degrades to a dense matmul when it is missing; that package is
deliberately undeclared and must stay that way. An import at module
scope has no such escape — the interpreter runs it or the module does
not load — so it is a hard requirement and belongs in the manifest.
"""

from __future__ import annotations

import ast
import sys
import tomllib
from importlib.metadata import packages_distributions
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

if TYPE_CHECKING:
    from collections.abc import Iterator

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

#: The anyio floor. No CVE here — the floor simply records the version
#: the lock already resolved when anyio was declared (issue #1123), the
#: same rule the neighbouring uvicorn declaration follows. Inventing a
#: lower bound would claim compatibility with releases this project has
#: never run.
_ANYIO_DECLARED_FLOOR = Version("4.13.0")

#: The last rpds-py release on the abandoned SemVer line, and the
#: version both locks were frozen at before issue #1185.
_RPDS_PY_LAST_SEMVER_RELEASE = Version("0.30.0")

#: The rpds-py floor: the current CalVer release. No CVE — this floor
#: exists so a conservative relock cannot drop back onto the 0.x line,
#: which the resolver would otherwise be free to do (jsonschema asks
#: only for >=0.25.0).
_RPDS_PY_CALVER_FLOOR = Version("2026.6.3")

#: A plausible future CalVer release. Nothing depends on it existing;
#: it is a probe for ceilings written in the SemVer idiom, every one of
#: which excludes it while still admitting the floor.
_RPDS_PY_FUTURE_CALVER_PROBE = Version("2027.1.1")

#: The openai release both locks already resolve; the floor records
#: what this project has run, never a version it has not.
_OPENAI_LOCKED_FLOOR = Version("2.41.0")

#: openai 3.0.0 swapped httpx for httpx2 — a separate distribution,
#: not a version bump. This is the release the ceiling excludes.
_OPENAI_HTTPX2_MAJOR = Version("3.0.0")

#: A far-future major. Probes for a lazy ``!=3.0.0`` exclusion
#: masquerading as a ceiling: that admits 99.0.0, a real ceiling does
#: not.
_OPENAI_FUTURE_MAJOR_PROBE = Version("99.0.0")

#: The floor this pin replaced. Without it the ceiling alone would
#: satisfy every other assertion here, so a regression to a bare
#: ``openai<3.0.0`` — or back to ``>=1.0,<3.0.0`` — would pass. The
#: floor is half the pin and is tested from both ends.
_OPENAI_PRE_BOUND_FLOOR = Version("1.0")

#: The packages whose import-time dependencies must all be declared.
#: Both are shipped in the wheel, so anything they import at module
#: scope has to be installable from the manifest alone.
_IMPORT_ROOTS = ("creek", "creek_mcp")


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


def _openai_specifier() -> SpecifierSet:
    """Return the ``openai`` specifier set from ``[project.optional-dependencies]``.

    creek-tools declares openai as the ``openai`` *extra* — the cloud
    LLM backend selected through ``llm.provider`` — not as a base
    dependency, so the entry lives in
    ``[project.optional-dependencies].openai`` rather than in
    ``[project].dependencies`` where crawdad's namesake finds it.

    Returns:
        The specifier set attached to the ``openai`` entry in the
        ``openai`` extra. Fails the calling test if the extra or the
        entry is absent.
    """
    with _PYPROJECT.open("rb") as handle:
        pyproject = tomllib.load(handle)
    extra: list[str] = pyproject["project"]["optional-dependencies"]["openai"]
    for entry in extra:
        requirement = Requirement(entry)
        if requirement.name == "openai":
            return requirement.specifier
    pytest.fail(
        "openai is not declared in [project.optional-dependencies].openai "
        "of pyproject.toml"
    )


def _locked_openai_version() -> Version:
    """Return the resolved ``openai`` version pinned in ``uv.lock``.

    Returns:
        The ``openai`` version resolved in the lockfile. Fails the
        calling test if the lock has no ``openai`` package entry.
    """
    with _UV_LOCK.open("rb") as handle:
        lock = tomllib.load(handle)
    packages: list[dict[str, object]] = lock["package"]
    for package in packages:
        if package["name"] == "openai":
            return Version(str(package["version"]))
    pytest.fail("openai has no [[package]] entry in uv.lock")


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


def test_openai_ceiling_rejects_the_httpx2_major() -> None:
    """The specifier excludes openai 3.0.0, the httpx2 release.

    No advisory is involved — OSV reports nothing for PyPI/openai. What
    3.0.0 changes is the transport: ``httpx<1,>=0.23.0`` became
    ``httpx2<3,>=2.7.0``, a separate distribution that arrives with
    httpcore2, truststore and idna>=3.18 beside the ``httpx>=0.27.0``
    this project already declares. The ``<3.0.0`` ceiling holds that
    swap until it is taken deliberately and jointly with the mcp cap.

    The second assertion is the one that catches a half-measure: a bare
    ``!=3.0.0`` reads like a ceiling and is not one.
    """
    specifier = _openai_specifier()
    assert str(_OPENAI_HTTPX2_MAJOR) not in specifier, (
        f"openai specifier {specifier!r} admits {_OPENAI_HTTPX2_MAJOR}, "
        "which replaces httpx with httpx2 — a separate distribution, not "
        "a version bump — and so would stand a second HTTP stack "
        "(httpcore2, truststore, idna>=3.18) beside the declared "
        "httpx>=0.27.0; the ceiling must be <3.0.0 and may move only "
        "jointly with the mcp<2.0.0 cap on the same swap (#1479, #998)"
    )
    assert str(_OPENAI_FUTURE_MAJOR_PROBE) not in specifier, (
        f"openai specifier {specifier!r} admits "
        f"{_OPENAI_FUTURE_MAJOR_PROBE}; an exclusion of the single "
        "release (`!=3.0.0`) is not a ceiling — it re-admits 3.0.1 and "
        "every later major carrying the same httpx2 stack. Write a real "
        "upper bound (#1479, #998)"
    )


def test_openai_floor_accepts_the_locked_release() -> None:
    """The specifier accepts 2.41.0 and rejects the unbounded floor.

    Both ends matter. The first assertion keeps the bound from
    overshooting the resolution the project actually runs; the second
    keeps the floor itself in place, since a ceiling-only regression to
    ``openai<3.0.0`` satisfies every other assertion in this file.
    """
    specifier = _openai_specifier()
    assert str(_OPENAI_LOCKED_FLOOR) in specifier, (
        f"openai specifier {specifier!r} rejects {_OPENAI_LOCKED_FLOOR}, "
        "the version uv.lock already resolves; a floor records what this "
        "project has actually run, never a version it has not, so "
        "bounding openai must not exclude today's resolution (#1479)"
    )
    assert str(_OPENAI_PRE_BOUND_FLOOR) not in specifier, (
        f"openai specifier {specifier!r} admits "
        f"{_OPENAI_PRE_BOUND_FLOOR}, the unbounded floor this pin "
        "replaced; the ceiling is only half the bound, and a bare "
        f"`<{_OPENAI_HTTPX2_MAJOR}` would let a resolver drop years "
        "below what this project has ever run (#1479)"
    )


def test_locked_openai_satisfies_the_declared_specifier() -> None:
    """``uv.lock`` resolves openai inside the declared bound.

    The lockfile is what CI installs. A manifest bounded to the 2.x line
    while the lock sits outside that bound is precisely the drift this
    pair of guards exists to catch — the declaration would be right and
    the installed environment still wrong.
    """
    locked = _locked_openai_version()
    assert str(locked) in _openai_specifier(), (
        f"uv.lock pins openai {locked}, which the declared specifier "
        f"{_openai_specifier()!r} rejects; a bounded manifest with a lock "
        "outside the bound is the drift this guards — run `uv lock` after "
        "changing the openai extra (#1479)"
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


def _anyio_specifier() -> SpecifierSet:
    """Return the ``anyio`` specifier from ``[project].dependencies``.

    anyio is a direct dependency here —
    ``creek_mcp.httpapi.middleware.limits`` imports ``Semaphore``,
    ``WouldBlock`` and ``fail_after`` from it at module scope — so it
    belongs alongside the other declared runtime dependencies rather
    than being left to arrive transitively via starlette.

    Returns:
        The specifier set attached to the ``anyio`` entry in
        ``[project].dependencies``. Fails the calling test if the entry
        is absent.
    """
    with _PYPROJECT.open("rb") as handle:
        pyproject = tomllib.load(handle)
    dependencies: list[str] = pyproject["project"]["dependencies"]
    for entry in dependencies:
        requirement = Requirement(entry)
        if requirement.name == "anyio":
            return requirement.specifier
    pytest.fail("anyio is not declared in [project].dependencies of pyproject.toml")


def _locked_anyio_version() -> Version:
    """Return the resolved ``anyio`` version pinned in ``uv.lock``.

    Returns:
        The ``anyio`` version resolved in the lockfile. Fails the
        calling test if the lock has no ``anyio`` package entry.
    """
    with _UV_LOCK.open("rb") as handle:
        lock = tomllib.load(handle)
    packages: list[dict[str, object]] = lock["package"]
    for package in packages:
        if package["name"] == "anyio":
            return Version(str(package["version"]))
    pytest.fail("anyio has no [[package]] entry in uv.lock")


def _declared_distributions() -> set[str]:
    """Return every distribution name ``[project]`` declares, canonicalised.

    Both ``dependencies`` and every ``optional-dependencies`` extra
    count: an import reached only through an extra is still declared,
    which is exactly the arrangement ``creek.ingest.documents`` and the
    cloud LLM providers rely on.

    ``[tool.uv].constraint-dependencies`` is deliberately excluded. A uv
    constraint tightens the resolution of a package already in the graph;
    it does not put one there, so it can never satisfy a direct import
    (DEP-003).

    Returns:
        Canonical (PEP 503) names of the declared distributions.
    """
    with _PYPROJECT.open("rb") as handle:
        pyproject = tomllib.load(handle)
    project = pyproject["project"]
    entries: list[str] = list(project["dependencies"])
    extras: dict[str, list[str]] = project.get("optional-dependencies", {})
    for extra in extras.values():
        entries.extend(extra)
    return {canonicalize_name(Requirement(entry).name) for entry in entries}


def _import_time_statements(
    module: ast.Module,
) -> Iterator[ast.Import | ast.ImportFrom]:
    """Yield the import statements executed when *module* is imported.

    Descends into ``if`` (so ``if TYPE_CHECKING:`` blocks count — a
    type-only import still needs the package installed to type-check),
    ``try`` and ``with``, but never into a function or class body. An
    import nested in a function runs only if that function is called, so
    it can legitimately be optional; one at module scope cannot.

    Args:
        module: The parsed source of a single file.

    Yields:
        Each import statement reachable at module scope.
    """
    pending: list[ast.stmt] = list(module.body)
    while pending:
        node = pending.pop()
        if isinstance(node, ast.Import | ast.ImportFrom):
            yield node
        elif isinstance(node, ast.If):
            pending.extend(node.body)
            pending.extend(node.orelse)
        elif isinstance(node, ast.Try):
            pending.extend(node.body)
            pending.extend(node.orelse)
            pending.extend(node.finalbody)
            for handler in node.handlers:
                pending.extend(handler.body)
        elif isinstance(node, ast.With):
            pending.extend(node.body)


def _top_level_names(node: ast.Import | ast.ImportFrom) -> list[str]:
    """Return the top-level module names an import statement reaches for.

    Args:
        node: An ``import x.y`` or ``from x.y import z`` statement.

    Returns:
        The distinct root module names, e.g. ``["anyio"]`` for
        ``from anyio import Semaphore``. Relative imports resolve within
        this package and yield nothing.
    """
    if isinstance(node, ast.Import):
        return [alias.name.split(".", 1)[0] for alias in node.names]
    if node.level or node.module is None:
        return []
    return [node.module.split(".", 1)[0]]


def _import_time_modules() -> dict[str, set[str]]:
    """Map each import-time top-level module to the files importing it.

    Returns:
        Module name to the set of repo-relative paths that import it at
        module scope. Keys include stdlib and first-party names; the
        caller filters.
    """
    imported: dict[str, set[str]] = {}
    for root in _IMPORT_ROOTS:
        for path in sorted((_PACKAGE_ROOT / root).rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for statement in _import_time_statements(tree):
                for name in _top_level_names(statement):
                    importers = imported.setdefault(name, set())
                    importers.add(str(path.relative_to(_PACKAGE_ROOT)))
    return imported


def _providing_distributions(module: str) -> set[str]:
    """Return the canonical distributions that provide *module*.

    Import names and distribution names differ often enough that a
    string comparison would be wrong (``frontmatter`` ships in
    ``python-frontmatter``, ``yaml`` in ``PyYAML``, ``PIL`` in
    ``pillow``), so the installed metadata answers the question. Reading
    it at runtime also means a newly-added package needs no table here.

    Args:
        module: A top-level import name.

    Returns:
        Canonical names of the installed distributions providing
        ``module``. When nothing in the environment claims it, falls
        back to the module's own canonicalised name so a
        partially-installed checkout still reports something actionable
        rather than crashing.
    """
    providers = packages_distributions().get(module)
    if providers is None:
        return {canonicalize_name(module)}
    return {canonicalize_name(name) for name in providers}


def test_anyio_is_declared_as_a_direct_dependency() -> None:
    """``anyio`` appears in ``[project].dependencies``.

    ``creek_mcp.httpapi.middleware.limits`` imports it at module scope;
    before issue #1123 it reached the environment only because starlette
    happened to depend on it, so a starlette release that dropped anyio
    would have broken the ``/v1`` limits middleware with no signal from
    the lockfile.
    """
    specifier = _anyio_specifier()
    assert str(specifier), (
        "anyio is declared without a version specifier; the floor must "
        "record the version the lock resolves so a relock cannot drift "
        "silently below it"
    )


def test_anyio_floor_tracks_the_locked_resolution() -> None:
    """The declared floor is the resolved version, not a looser guess.

    The neighbouring uvicorn declaration sets the precedent: the floor
    matches what ``uv.lock`` already resolves. A lower bound would claim
    support for releases this project has never run.
    """
    specifier = _anyio_specifier()
    assert str(_ANYIO_DECLARED_FLOOR) in specifier, (
        f"anyio specifier {specifier!r} rejects {_ANYIO_DECLARED_FLOOR}, "
        "the version uv.lock resolves"
    )
    assert "4.12.0" not in specifier, (
        f"anyio specifier {specifier!r} admits 4.12.0; the floor must be "
        f">={_ANYIO_DECLARED_FLOOR}, the resolution the project actually runs"
    )


def test_locked_anyio_satisfies_the_declared_floor() -> None:
    """``uv.lock`` resolves anyio at or above the declared floor.

    A declaration added without a relock leaves the lock stale, and CI
    installs from the lock — the very drift this guard exists to stop.
    """
    locked = _locked_anyio_version()
    assert locked >= _ANYIO_DECLARED_FLOOR, (
        f"uv.lock pins anyio {locked}, below the declared floor "
        f"{_ANYIO_DECLARED_FLOOR}; run `uv lock` after changing pyproject"
    )
    assert str(locked) in _anyio_specifier(), (
        f"uv.lock pins anyio {locked}, which the declared specifier "
        f"{_anyio_specifier()!r} rejects; the lock is stale"
    )


def test_every_import_time_module_is_declared() -> None:
    """No module imported at import time is missing from ``[project]``.

    The generalisation of the anyio defect (#1123). Every third-party
    package that ``creek`` or ``creek_mcp`` imports at module scope must
    be declared in ``dependencies`` or an ``optional-dependencies``
    extra, so the next undeclared import fails here instead of surviving
    to review — or to a transitive bump in production.

    Function-local imports are exempt by construction: they are how this
    codebase expresses genuinely optional packages (``faiss`` in
    ``creek.clean.semantic_dedup``), and they cannot break at import
    time.
    """
    declared = _declared_distributions()
    undeclared: dict[str, set[str]] = {}
    for module, importers in _import_time_modules().items():
        if module in sys.stdlib_module_names or module in _IMPORT_ROOTS:
            continue
        if _providing_distributions(module) & declared:
            continue
        undeclared[module] = importers
    report = "; ".join(
        f"{module} (imported by {', '.join(sorted(paths))})"
        for module, paths in sorted(undeclared.items())
    )
    assert not undeclared, (
        f"import-time dependencies missing from pyproject.toml: {report}. "
        "Declare each in [project].dependencies (or the extra that owns "
        "it) and run `uv lock`, or move the import inside the function "
        "that needs it if the package is genuinely optional"
    )


def _rpds_py_constraint_specifier() -> SpecifierSet:
    """Return the ``rpds-py`` specifier from uv constraint-dependencies.

    Reads ``[tool.uv].constraint-dependencies`` in ``pyproject.toml``,
    the home for floors on transitive-only packages (DEP-003). rpds-py
    arrives via mcp → jsonschema (and mcp → jsonschema → referencing)
    and is never imported here, so it belongs there rather than in
    ``[project].dependencies``.

    Returns:
        The specifier set attached to the ``rpds-py`` constraint entry.
        Fails the calling test if the ``[tool.uv]`` table or the
        ``rpds-py`` entry is absent.
    """
    with _PYPROJECT.open("rb") as handle:
        pyproject = tomllib.load(handle)
    constraints: list[str] = (
        pyproject.get("tool", {}).get("uv", {}).get("constraint-dependencies", [])
    )
    for entry in constraints:
        requirement = Requirement(entry)
        if requirement.name == "rpds-py":
            return requirement.specifier
    pytest.fail(
        "rpds-py has no entry in [tool.uv].constraint-dependencies of pyproject.toml"
    )


def _locked_rpds_py_version() -> Version:
    """Return the resolved ``rpds-py`` version pinned in ``uv.lock``.

    Returns:
        The ``rpds-py`` version resolved in the lockfile. Fails the
        calling test if the lock has no ``rpds-py`` package entry.
    """
    with _UV_LOCK.open("rb") as handle:
        lock = tomllib.load(handle)
    packages: list[dict[str, object]] = lock["package"]
    for package in packages:
        if package["name"] == "rpds-py":
            return Version(str(package["version"]))
    pytest.fail("rpds-py has no [[package]] entry in uv.lock")


def test_rpds_py_constraint_rejects_the_abandoned_semver_line() -> None:
    """The constraint excludes 0.30.0, the last SemVer release.

    Both consumers declare open floors (jsonschema ``>=0.25.0``,
    referencing ``>=0.7.0``), so without a floor of our own a
    conservative relock is free to sit on the 0.x line forever — which
    is exactly how both locks went eight months stale (#1185).
    """
    specifier = _rpds_py_constraint_specifier()
    assert str(_RPDS_PY_LAST_SEMVER_RELEASE) not in specifier, (
        f"rpds-py constraint {specifier!r} admits "
        f"{_RPDS_PY_LAST_SEMVER_RELEASE}, the abandoned SemVer line both "
        f"locks were frozen on; the floor must be "
        f">={_RPDS_PY_CALVER_FLOOR}"
    )


def test_rpds_py_constraint_accepts_the_calver_floor() -> None:
    """The constraint accepts 2026.6.3, the release both locks pin."""
    specifier = _rpds_py_constraint_specifier()
    assert str(_RPDS_PY_CALVER_FLOOR) in specifier, (
        f"rpds-py constraint {specifier!r} rejects {_RPDS_PY_CALVER_FLOOR}, "
        "the version uv.lock resolves"
    )


def test_rpds_py_constraint_admits_future_calver_releases() -> None:
    """No SemVer-shaped ceiling may be attached to rpds-py.

    This is the pin's whole point. rpds-py switched from SemVer to
    CalVer at 2026.5.1, so every ceiling written in the idiom the
    neighbouring pins use — ``<1.0``, ``~=0.30``, ``<2026.7`` reasoned
    about as if it were a minor version — excludes every subsequent
    release permanently while still admitting today's floor. A probe on
    a future CalVer version is the only assertion that catches it.
    """
    specifier = _rpds_py_constraint_specifier()
    assert str(_RPDS_PY_FUTURE_CALVER_PROBE) in specifier, (
        f"rpds-py constraint {specifier!r} rejects "
        f"{_RPDS_PY_FUTURE_CALVER_PROBE}; rpds-py releases under CalVer "
        "since 2026.5.1, so a SemVer-shaped ceiling (<1.0, ~=0.30) freezes "
        "the dependency forever. Express bounds in CalVer, or use a bare "
        "floor"
    )


def test_locked_rpds_py_is_on_the_calver_line() -> None:
    """``uv.lock`` resolves rpds-py to a CalVer release.

    The lockfile is what CI installs; a correct constraint with a stale
    lock still ships the 0.x native extension on the MCP path that
    every server start loads.
    """
    locked = _locked_rpds_py_version()
    assert locked >= _RPDS_PY_CALVER_FLOOR, (
        f"uv.lock pins rpds-py {locked}, below the CalVer floor "
        f"{_RPDS_PY_CALVER_FLOOR}; run "
        "`uv lock --upgrade-package rpds-py` — a bare `uv lock` is a "
        "no-op here because 0.30.0 already satisfies jsonschema's "
        ">=0.25.0"
    )
    assert locked.major >= _RPDS_PY_CALVER_FLOOR.major, (
        f"uv.lock pins rpds-py {locked}, whose leading component "
        f"{locked.major} is not a CalVer year; the 0.x line is abandoned"
    )
