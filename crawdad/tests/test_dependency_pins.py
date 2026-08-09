"""Guard tests for dependency pins the resolver must not undo.

Most pins here answer specific CVEs; the tests guard both the declared
floor and the resolved lock so a future relock cannot regress onto a
vulnerable release. The ``rpds-py`` pin at the end of this file is the
exception — it answers a versioning-scheme migration rather than an
advisory — but it is guarded the same two ways for the same reason.

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

``cryptography``, ``pydantic-settings``, ``python-multipart``, and
``starlette`` (issue #979): the exported lock carried eight further
advisories across these four transitive-only packages. cryptography
48.0.0 carried GHSA-537c-gmf6-5ccf (fixed in 48.0.1), reached via
google-auth and pyjwt; that floor has since been superseded — see the
#1167 paragraph below. pydantic-settings 2.14.1 carries
GHSA-4xgf-cpjx-pc3j (fixed in 2.14.2), reached via mcp.
python-multipart 0.0.29 carries PYSEC-2026-3036 and PYSEC-2026-3037
(fixed in 0.0.30) plus PYSEC-2026-3040 (fixed only in 0.0.31), so
0.0.30 is a partial fix; reached via mcp. starlette 1.1.0 carries
PYSEC-2026-248 (fixed in 1.3.0) plus PYSEC-2026-249 (fixed only in
1.3.1), so 1.3.0 is a partial fix; reached via mcp and sse-starlette.
Every upstream specifier on these four is an open floor with no
ceiling, so all four raise cleanly with zero suppressions and their
floors live in ``[tool.uv].constraint-dependencies`` alongside pyasn1
and aiohttp.

``cryptography`` (issue #1167): cryptography 49.0.0 carries
PYSEC-2026-3552 (CVE-2026-69247, GHSA-g6cj-pr64-35w5) — PKCS#7
``EnvelopedData`` decryption exposed a Bleichenbacher oracle through
distinguishable errors and timing, so an attacker able to submit
chosen ciphertexts can recover plaintext. OSV records the flaw as
introduced in 44.0.0 and fixed in 50.0.0, which makes the whole
44.0.0 through 49.x band vulnerable and retires the 48.0.1 floor set
in #979 — that floor now admits the oracle rather than excluding it,
so the guards below were raised in place instead of being duplicated.
cryptography remains transitive-only here, reached via google-genai →
google-auth and via mcp → pyjwt; both declare open floors
(``cryptography>=38.0.3`` and ``cryptography>=3.4.0``), so nothing
upstream blocks the raise and the constraint moves to ``>=50.0.0``
with zero suppressions.

``rpds-py`` (issue #1185): not a CVE. rpds-py abandoned SemVer for
CalVer at 2026.5.1 — the release line runs 0.29.0, 0.30.0, then
2026.5.1 with no 0.31 or 1.0 in between — and raised its
``requires-python`` from ``>=3.10`` to ``>=3.11`` (crawdad already
declares ``>=3.11``, so nothing here is excluded). This lock sat at
0.30.0, eight months stale, because nothing forced it off: jsonschema
declares ``rpds-py>=0.25.0`` and referencing declares
``rpds-py>=0.7.0``, both open floors, so a bare ``uv lock`` is a no-op.
rpds-py is transitive-only (mcp → jsonschema → rpds-py, and mcp →
jsonschema → referencing → rpds-py) and nothing in crawdad imports it,
so its floor lives in ``[tool.uv].constraint-dependencies`` on the
DEP-003 precedent. The floor is kept identical to creek-tools' — the
two projects share the MCP dependency path, and letting them resolve
different builds of the same native extension is the split this pin
exists to prevent.

The durable hazard is the *idiom*, not the version. A ceiling written
the way every other pin here would write one — ``rpds-py<1.0``, or a
``~=0.30`` compatible-release clause — now excludes every release from
2026 onward, permanently, because 2026.5.1 sorts above 1.0. So the
constraint is a bare CalVer floor with no ceiling, and
``test_rpds_py_constraint_admits_future_calver_releases`` fails if
anyone adds one in the SemVer shape.

Two independent guards per package:

* **pyproject floor** — the declared specifier must reject the last
  vulnerable release (for rpds-py, the last SemVer release) so a
  future relock cannot resolve back down to it.
* **locked version** — ``uv.lock`` is the reproducibility contract
  that ``uv sync`` users install and the second surface
  ``scripts/security.sh`` audits via ``uv export --locked``, so the
  resolved entry must already be at or above the patched version.
  (crawdad's own CI provisions the *installed environment* with ``pip
  install -e ".[dev]"``, which is audited separately by the bare
  ``pip-audit`` run added in #979.)

Three further guards cover the *wiring* of the security gate itself —
the regression class that produced #979. A pin is only worth as much
as the scanner that would notice its absence, and crawdad's
``scripts/security.sh`` ran bandit alone, so eight advisories sat in
the lock unreported while the gate stayed green. Those tests assert
that ``security.sh`` audits both surfaces — the installed environment
with a bare ``pip-audit``, and the exported lock via ``uv export
--locked`` fed to ``pip-audit -r`` — and that
``[project.optional-dependencies].dev`` actually provisions
``pip-audit`` and ``uv``, since the crawdad CI job installs only
``.[dev]`` before running ``check-all.sh``.
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
_SECURITY_SCRIPT = _PACKAGE_ROOT / "scripts" / "security.sh"

#: First mcp release containing the fixes for CVE-2026-52869,
#: CVE-2026-52870, and CVE-2026-59950.
_PATCHED_VERSION = Version("1.28.1")

#: First pyasn1 release containing the fixes for CVE-2026-59885 and
#: CVE-2026-59886.
_PYASN1_PATCHED_VERSION = Version("0.6.4")

#: First aiohttp release with all eleven advisories fixed; 3.14.0
#: fixed only PYSEC-2026-2104, PYSEC-2026-2105, and PYSEC-2026-2106.
_AIOHTTP_PATCHED_VERSION = Version("3.14.1")

#: First cryptography release containing the fix for PYSEC-2026-3552
#: (CVE-2026-69247 / GHSA-g6cj-pr64-35w5). The advisory range opens at
#: 44.0.0, so this supersedes the 48.0.1 floor that answered the older
#: GHSA-537c-gmf6-5ccf: 48.0.1 sits inside the vulnerable band.
_CRYPTOGRAPHY_PATCHED_VERSION = Version("50.0.0")

#: First pydantic-settings release containing the fix for
#: GHSA-4xgf-cpjx-pc3j.
_PYDANTIC_SETTINGS_PATCHED_VERSION = Version("2.14.2")

#: First python-multipart release with all three advisories fixed;
#: 0.0.30 fixed only PYSEC-2026-3036 and PYSEC-2026-3037, leaving
#: PYSEC-2026-3040 open until 0.0.31.
_PYTHON_MULTIPART_PATCHED_VERSION = Version("0.0.31")

#: First starlette release with both advisories fixed; 1.3.0 fixed
#: only PYSEC-2026-248, leaving PYSEC-2026-249 open until 1.3.1.
_STARLETTE_PATCHED_VERSION = Version("1.3.1")

#: The last rpds-py release on the abandoned SemVer line, and the
#: version both locks were frozen at before issue #1185.
_RPDS_PY_LAST_SEMVER_RELEASE = Version("0.30.0")

#: The rpds-py floor: the current CalVer release, held identical to
#: creek-tools'. No CVE — this floor exists so a conservative relock
#: cannot drop back onto the 0.x line, which the resolver would
#: otherwise be free to do (jsonschema asks only for >=0.25.0).
_RPDS_PY_CALVER_FLOOR = Version("2026.6.3")

#: A plausible future CalVer release. Nothing depends on it existing;
#: it is a probe for ceilings written in the SemVer idiom, every one of
#: which excludes it while still admitting the floor.
_RPDS_PY_FUTURE_CALVER_PROBE = Version("2027.1.1")

#: Shell builtins that only *look up* a command (``command -v
#: pip-audit``) instead of running it; a lookup must not count as an
#: audit.
_LOOKUP_BUILTINS = frozenset({"command", "hash", "type", "which"})


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


def _cryptography_constraint_specifier() -> SpecifierSet:
    """Return the ``cryptography`` specifier from uv constraints.

    Reads ``[tool.uv].constraint-dependencies`` in ``pyproject.toml``,
    the home for floors on transitive-only packages (DEP-003).

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
        The ``cryptography`` version resolved in the lockfile. Fails
        the calling test if the lock has no ``cryptography`` package
        entry.
    """
    with _UV_LOCK.open("rb") as handle:
        lock = tomllib.load(handle)
    packages: list[dict[str, object]] = lock["package"]
    for package in packages:
        if package["name"] == "cryptography":
            return Version(str(package["version"]))
    pytest.fail("cryptography has no [[package]] entry in uv.lock")


def _pydantic_settings_constraint_specifier() -> SpecifierSet:
    """Return the ``pydantic-settings`` specifier from uv constraints.

    Reads ``[tool.uv].constraint-dependencies`` in ``pyproject.toml``,
    the home for floors on transitive-only packages (DEP-003).

    Returns:
        The specifier set attached to the ``pydantic-settings``
        constraint entry. Fails the calling test if the ``[tool.uv]``
        table or the ``pydantic-settings`` entry is absent.
    """
    with _PYPROJECT.open("rb") as handle:
        pyproject = tomllib.load(handle)
    constraints: list[str] = (
        pyproject.get("tool", {}).get("uv", {}).get("constraint-dependencies", [])
    )
    for entry in constraints:
        requirement = Requirement(entry)
        if requirement.name == "pydantic-settings":
            return requirement.specifier
    pytest.fail(
        "pydantic-settings has no entry in [tool.uv].constraint-dependencies "
        "of pyproject.toml"
    )


def _locked_pydantic_settings_version() -> Version:
    """Return the resolved ``pydantic-settings`` version in ``uv.lock``.

    Returns:
        The ``pydantic-settings`` version resolved in the lockfile.
        Fails the calling test if the lock has no ``pydantic-settings``
        package entry.
    """
    with _UV_LOCK.open("rb") as handle:
        lock = tomllib.load(handle)
    packages: list[dict[str, object]] = lock["package"]
    for package in packages:
        if package["name"] == "pydantic-settings":
            return Version(str(package["version"]))
    pytest.fail("pydantic-settings has no [[package]] entry in uv.lock")


def _python_multipart_constraint_specifier() -> SpecifierSet:
    """Return the ``python-multipart`` specifier from uv constraints.

    Reads ``[tool.uv].constraint-dependencies`` in ``pyproject.toml``,
    the home for floors on transitive-only packages (DEP-003).

    Returns:
        The specifier set attached to the ``python-multipart``
        constraint entry. Fails the calling test if the ``[tool.uv]``
        table or the ``python-multipart`` entry is absent.
    """
    with _PYPROJECT.open("rb") as handle:
        pyproject = tomllib.load(handle)
    constraints: list[str] = (
        pyproject.get("tool", {}).get("uv", {}).get("constraint-dependencies", [])
    )
    for entry in constraints:
        requirement = Requirement(entry)
        if requirement.name == "python-multipart":
            return requirement.specifier
    pytest.fail(
        "python-multipart has no entry in [tool.uv].constraint-dependencies "
        "of pyproject.toml"
    )


def _locked_python_multipart_version() -> Version:
    """Return the resolved ``python-multipart`` version in ``uv.lock``.

    Returns:
        The ``python-multipart`` version resolved in the lockfile.
        Fails the calling test if the lock has no ``python-multipart``
        package entry.
    """
    with _UV_LOCK.open("rb") as handle:
        lock = tomllib.load(handle)
    packages: list[dict[str, object]] = lock["package"]
    for package in packages:
        if package["name"] == "python-multipart":
            return Version(str(package["version"]))
    pytest.fail("python-multipart has no [[package]] entry in uv.lock")


def _starlette_constraint_specifier() -> SpecifierSet:
    """Return the ``starlette`` specifier from uv constraints.

    Reads ``[tool.uv].constraint-dependencies`` in ``pyproject.toml``,
    the home for floors on transitive-only packages (DEP-003).

    Returns:
        The specifier set attached to the ``starlette`` constraint
        entry. Fails the calling test if the ``[tool.uv]`` table or the
        ``starlette`` entry is absent.
    """
    with _PYPROJECT.open("rb") as handle:
        pyproject = tomllib.load(handle)
    constraints: list[str] = (
        pyproject.get("tool", {}).get("uv", {}).get("constraint-dependencies", [])
    )
    for entry in constraints:
        requirement = Requirement(entry)
        if requirement.name == "starlette":
            return requirement.specifier
    pytest.fail(
        "starlette has no entry in [tool.uv].constraint-dependencies of pyproject.toml"
    )


def _locked_starlette_version() -> Version:
    """Return the resolved ``starlette`` version pinned in ``uv.lock``.

    Returns:
        The ``starlette`` version resolved in the lockfile. Fails the
        calling test if the lock has no ``starlette`` package entry.
    """
    with _UV_LOCK.open("rb") as handle:
        lock = tomllib.load(handle)
    packages: list[dict[str, object]] = lock["package"]
    for package in packages:
        if package["name"] == "starlette":
            return Version(str(package["version"]))
    pytest.fail("starlette has no [[package]] entry in uv.lock")


def _security_script_commands() -> list[str]:
    """Return the executable commands in ``scripts/security.sh``.

    Comment-only lines are dropped and backslash continuations are
    joined, so a multi-line invocation is inspected as the single
    command the shell actually runs.

    Returns:
        One entry per non-blank, non-comment logical line of the
        script.
    """
    text = _SECURITY_SCRIPT.read_text(encoding="utf-8")
    joined = text.replace("\\\n", " ")
    return [
        line.strip()
        for line in joined.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def _is_pip_audit_command(tokens: list[str]) -> bool:
    """Return whether a tokenized command actually runs ``pip-audit``.

    Args:
        tokens: Shell tokens of a single command.

    Returns:
        ``True`` when some token is ``pip-audit`` or a path ending in
        it, such as ``.venv/bin/pip-audit``. Existence probes such as
        ``command -v pip-audit`` do not count as audits.
    """
    if any(token in _LOOKUP_BUILTINS for token in tokens):
        return False
    return any(token.rsplit("/", maxsplit=1)[-1] == "pip-audit" for token in tokens)


def _pip_audit_invocations() -> list[list[str]]:
    """Return the tokenized ``pip-audit`` commands in ``security.sh``.

    Returns:
        Each ``pip-audit`` invocation as its list of shell tokens.
    """
    invocations: list[list[str]] = []
    for command in _security_script_commands():
        tokens = command.split()
        if _is_pip_audit_command(tokens):
            invocations.append(tokens)
    return invocations


def _targets_requirements_file(tokens: list[str]) -> bool:
    """Return whether a tokenized invocation names a requirements file.

    Args:
        tokens: Shell tokens of a single ``pip-audit`` invocation.

    Returns:
        ``True`` when the invocation carries ``-r`` / ``--requirement``
        in any accepted spelling, meaning it audits a requirements file
        rather than the installed environment.
    """
    flags = {"-r", "--requirement"}
    prefixes = ("-r", "--requirement=")
    return any(token in flags or token.startswith(prefixes) for token in tokens)


def _dev_extra_requirement_names() -> set[str]:
    """Return the names declared in ``[project.optional-dependencies].dev``.

    Returns:
        The requirement name of every entry in the ``dev`` extra. Fails
        the calling test if the extra is absent.
    """
    with _PYPROJECT.open("rb") as handle:
        pyproject = tomllib.load(handle)
    extras = pyproject["project"].get("optional-dependencies", {})
    if "dev" not in extras:
        pytest.fail("pyproject.toml declares no [project.optional-dependencies].dev")
    dev_entries: list[str] = extras["dev"]
    return {Requirement(entry).name for entry in dev_entries}


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

    The lockfile is what ``uv sync`` users install and the second
    surface ``scripts/security.sh`` audits via ``uv export --locked``
    (crawdad CI itself provisions the installed environment with ``pip
    install -e ".[dev]"``, covered by the bare ``pip-audit`` run); a
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

    The lockfile is what ``uv sync`` users install and the second
    surface ``scripts/security.sh`` audits via ``uv export --locked``
    (crawdad CI itself provisions the installed environment with ``pip
    install -e ".[dev]"``, covered by the bare ``pip-audit`` run); a
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

    The lockfile is what ``uv sync`` users install and the second
    surface ``scripts/security.sh`` audits via ``uv export --locked``
    (crawdad CI itself provisions the installed environment with ``pip
    install -e ".[dev]"``, covered by the bare ``pip-audit`` run); a
    correct constraint floor with a stale lock still ships the
    vulnerable 3.13.5.
    """
    locked = _locked_aiohttp_version()
    assert locked >= _AIOHTTP_PATCHED_VERSION, (
        f"uv.lock pins aiohttp {locked}, below the patched "
        f"{_AIOHTTP_PATCHED_VERSION} (PYSEC-2026-237 and PYSEC-2026-2104 "
        "through -2113); pip-audit inspects the lock, so relock after "
        "adding the constraint"
    )


def test_cryptography_floor_rejects_last_vulnerable_release() -> None:
    """The constraint excludes 49.0.0, the previously-locked release.

    cryptography 49.0.0 carries PYSEC-2026-3552 (CVE-2026-69247 — the
    PKCS#7 ``EnvelopedData`` Bleichenbacher oracle) and reaches this
    graph through google-auth and pyjwt. The fix lands AT 50.0.0, so
    the probe assertion on 49.9999 pins the floor at the patch itself:
    the retired ``>=48.0.1`` floor, or any other below ``>=50.0.0``,
    still admits vulnerable releases and fails here.
    """
    specifier = _cryptography_constraint_specifier()
    assert "49.0.0" not in specifier, (
        f"cryptography constraint {specifier!r} admits 49.0.0, the "
        "release carrying PYSEC-2026-3552 / CVE-2026-69247; the floor "
        "must be >=50.0.0"
    )
    assert "49.9999" not in specifier, (
        f"cryptography constraint {specifier!r} admits 49.9999; the fix "
        "for PYSEC-2026-3552 / CVE-2026-69247 lands at 50.0.0, so any "
        "floor below >=50.0.0 still admits vulnerable releases"
    )


def test_cryptography_floor_accepts_patched_release() -> None:
    """The constraint accepts 50.0.0, the first patched release."""
    specifier = _cryptography_constraint_specifier()
    assert "50.0.0" in specifier, (
        f"cryptography constraint {specifier!r} rejects 50.0.0; the "
        "patched release itself must satisfy the constraint"
    )


def test_locked_cryptography_at_or_above_patched_release() -> None:
    """``uv.lock`` resolves cryptography to >= 50.0.0.

    The lockfile is what ``uv sync`` users install and the second
    surface ``scripts/security.sh`` audits via ``uv export --locked``;
    a correct constraint floor with a stale lock still ships the
    vulnerable 49.0.0.
    """
    locked = _locked_cryptography_version()
    assert locked >= _CRYPTOGRAPHY_PATCHED_VERSION, (
        f"uv.lock pins cryptography {locked}, below the patched "
        f"{_CRYPTOGRAPHY_PATCHED_VERSION} (PYSEC-2026-3552 / "
        "CVE-2026-69247); the exported lock is audited, so relock after "
        "raising the constraint"
    )


def test_pydantic_settings_floor_rejects_last_vulnerable_release() -> None:
    """The constraint excludes 2.14.1, the last vulnerable release.

    pydantic-settings 2.14.1 carries GHSA-4xgf-cpjx-pc3j and reaches
    this graph through mcp. The constraint must genuinely be
    ``>=2.14.2`` so a relock cannot resolve back onto it.
    """
    specifier = _pydantic_settings_constraint_specifier()
    assert "2.14.1" not in specifier, (
        f"pydantic-settings constraint {specifier!r} admits 2.14.1, the "
        "release carrying GHSA-4xgf-cpjx-pc3j; the floor must be "
        ">=2.14.2"
    )


def test_pydantic_settings_floor_accepts_patched_release() -> None:
    """The constraint accepts 2.14.2, the first patched release."""
    specifier = _pydantic_settings_constraint_specifier()
    assert "2.14.2" in specifier, (
        f"pydantic-settings constraint {specifier!r} rejects 2.14.2; the "
        "patched release itself must satisfy the constraint"
    )


def test_locked_pydantic_settings_at_or_above_patched_release() -> None:
    """``uv.lock`` resolves pydantic-settings to >= 2.14.2.

    The lockfile is what ``uv sync`` users install and the second
    surface ``scripts/security.sh`` audits via ``uv export --locked``;
    a correct constraint floor with a stale lock still ships the
    vulnerable 2.14.1.
    """
    locked = _locked_pydantic_settings_version()
    assert locked >= _PYDANTIC_SETTINGS_PATCHED_VERSION, (
        f"uv.lock pins pydantic-settings {locked}, below the patched "
        f"{_PYDANTIC_SETTINGS_PATCHED_VERSION} (GHSA-4xgf-cpjx-pc3j); the "
        "exported lock is audited, so relock after adding the constraint"
    )


def test_python_multipart_floor_rejects_vulnerable_releases() -> None:
    """The constraint excludes 0.0.29 and the partial-fix 0.0.30.

    python-multipart 0.0.29 carries PYSEC-2026-3036, PYSEC-2026-3037,
    and PYSEC-2026-3040. 0.0.30 clears only the first two, so stopping
    the floor at ``>=0.0.30`` would still ship a vulnerability reachable
    through mcp; the floor has to be ``>=0.0.31``.
    """
    specifier = _python_multipart_constraint_specifier()
    assert "0.0.29" not in specifier, (
        f"python-multipart constraint {specifier!r} admits 0.0.29, the "
        "release carrying PYSEC-2026-3036 / PYSEC-2026-3037 / "
        "PYSEC-2026-3040; the floor must be >=0.0.31"
    )
    assert "0.0.30" not in specifier, (
        f"python-multipart constraint {specifier!r} admits 0.0.30, which "
        "still carries PYSEC-2026-3040; the floor must be >=0.0.31, not "
        ">=0.0.30"
    )


def test_python_multipart_floor_accepts_patched_release() -> None:
    """The constraint accepts 0.0.31, the first fully patched release."""
    specifier = _python_multipart_constraint_specifier()
    assert "0.0.31" in specifier, (
        f"python-multipart constraint {specifier!r} rejects 0.0.31; the "
        "patched release itself must satisfy the constraint"
    )


def test_locked_python_multipart_at_or_above_patched_release() -> None:
    """``uv.lock`` resolves python-multipart to >= 0.0.31.

    The lockfile is what ``uv sync`` users install and the second
    surface ``scripts/security.sh`` audits via ``uv export --locked``;
    a correct constraint floor with a stale lock still ships the
    vulnerable 0.0.29.
    """
    locked = _locked_python_multipart_version()
    assert locked >= _PYTHON_MULTIPART_PATCHED_VERSION, (
        f"uv.lock pins python-multipart {locked}, below the patched "
        f"{_PYTHON_MULTIPART_PATCHED_VERSION} (PYSEC-2026-3036 / "
        "PYSEC-2026-3037 / PYSEC-2026-3040); the exported lock is "
        "audited, so relock after adding the constraint"
    )


def test_starlette_floor_rejects_vulnerable_releases() -> None:
    """The constraint excludes 1.1.0 and the partial-fix 1.3.0.

    starlette 1.1.0 carries PYSEC-2026-248 and PYSEC-2026-249. 1.3.0
    clears only the first, so stopping the floor at ``>=1.3.0`` would
    still ship a vulnerability reachable through mcp and sse-starlette;
    the floor has to be ``>=1.3.1``.
    """
    specifier = _starlette_constraint_specifier()
    assert "1.1.0" not in specifier, (
        f"starlette constraint {specifier!r} admits 1.1.0, the release "
        "carrying PYSEC-2026-248 / PYSEC-2026-249; the floor must be "
        ">=1.3.1"
    )
    assert "1.3.0" not in specifier, (
        f"starlette constraint {specifier!r} admits 1.3.0, which still "
        "carries PYSEC-2026-249; the floor must be >=1.3.1, not >=1.3.0"
    )


def test_starlette_floor_accepts_patched_release() -> None:
    """The constraint accepts 1.3.1, the first fully patched release."""
    specifier = _starlette_constraint_specifier()
    assert "1.3.1" in specifier, (
        f"starlette constraint {specifier!r} rejects 1.3.1; the patched "
        "release itself must satisfy the constraint"
    )


def test_locked_starlette_at_or_above_patched_release() -> None:
    """``uv.lock`` resolves starlette to >= 1.3.1.

    The lockfile is what ``uv sync`` users install and the second
    surface ``scripts/security.sh`` audits via ``uv export --locked``;
    a correct constraint floor with a stale lock still ships the
    vulnerable 1.1.0.
    """
    locked = _locked_starlette_version()
    assert locked >= _STARLETTE_PATCHED_VERSION, (
        f"uv.lock pins starlette {locked}, below the patched "
        f"{_STARLETTE_PATCHED_VERSION} (PYSEC-2026-248 / PYSEC-2026-249); "
        "the exported lock is audited, so relock after adding the "
        "constraint"
    )


def test_security_script_audits_installed_environment() -> None:
    """``security.sh`` runs pip-audit against the live environment.

    crawdad CI provisions with ``pip install -e ".[dev]"``, and pip
    honours neither ``uv.lock`` nor ``[tool.uv].constraint-dependencies``
    — both are invisible to it. The installed environment is therefore a
    distinct surface that nothing else in the gate guards: a floor can be
    correct in pyproject and the lock while CI still resolves a
    vulnerable transitive release. Only a bare ``pip-audit`` (one with no
    ``-r`` / ``--requirement``) inspects what is actually installed.
    """
    invocations = _pip_audit_invocations()
    assert invocations, (
        "scripts/security.sh invokes no pip-audit at all; bandit alone "
        "left the eight advisories of #979 unreported"
    )
    environment_audits = [
        tokens for tokens in invocations if not _targets_requirements_file(tokens)
    ]
    assert environment_audits, (
        "every pip-audit invocation in scripts/security.sh targets a "
        "requirements file; add a bare `pip-audit` so the environment CI "
        f"actually installs is audited (found: {invocations!r})"
    )


def test_security_script_audits_exported_lock() -> None:
    """``security.sh`` audits the lock exported by ``uv export --locked``.

    ``uv.lock`` is what ``uv sync`` users install and where all eight
    advisories in #979 lived. Auditing only the installed venv reported
    clean while the lock stayed vulnerable, so the gate must also export
    the lock (``--locked``, which fails rather than silently relocking)
    and feed it to ``pip-audit -r``.
    """
    commands = _security_script_commands()
    exports = [command for command in commands if "uv export" in command]
    assert exports, (
        "scripts/security.sh never runs `uv export`; the lockfile "
        "surface where the #979 advisories lived would go unaudited"
    )
    assert any("--locked" in command for command in exports), (
        "scripts/security.sh runs `uv export` without --locked, so a "
        "stale lock would be silently regenerated instead of failing "
        f"(found: {exports!r})"
    )
    audits = _pip_audit_invocations()
    lock_audits = [tokens for tokens in audits if _targets_requirements_file(tokens)]
    assert lock_audits, (
        "scripts/security.sh exports the lock but never feeds it to "
        "`pip-audit -r`; the export alone audits nothing"
    )


def test_dev_extras_provision_audit_toolchain() -> None:
    """The ``dev`` extra installs both tools ``security.sh`` invokes.

    The crawdad CI job installs ONLY ``.[dev]`` and then runs
    ``check-all.sh``. A tool the security script invokes but the extra
    omits fails CI outright — or, worse, is silently unavailable — so
    this pins the provisioning contract to the script's actual needs.
    """
    names = _dev_extra_requirement_names()
    assert "pip-audit" in names, (
        "[project.optional-dependencies].dev omits pip-audit, the "
        "scanner scripts/security.sh runs; CI installs only .[dev]"
    )
    assert "uv" in names, (
        "[project.optional-dependencies].dev omits uv, which "
        "scripts/security.sh needs for `uv export --locked`; CI installs "
        "only .[dev]"
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
    is exactly how this lock went eight months stale (#1185).
    """
    specifier = _rpds_py_constraint_specifier()
    assert str(_RPDS_PY_LAST_SEMVER_RELEASE) not in specifier, (
        f"rpds-py constraint {specifier!r} admits "
        f"{_RPDS_PY_LAST_SEMVER_RELEASE}, the abandoned SemVer line this "
        f"lock was frozen on; the floor must be >={_RPDS_PY_CALVER_FLOOR}"
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

    The lockfile is what ``uv sync`` users install and what
    ``uv export --locked`` feeds to pip-audit; a correct constraint
    with a stale lock still ships the 0.x native extension on the MCP
    path every bot start loads.
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
