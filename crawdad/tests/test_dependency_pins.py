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

``idna`` (issue #1328): idna 3.14 and every release below it carry
CVE-2026-45409 (GHSA-65pc-fj4g-8rjx / PYSEC-2026-215), fixed in 3.15.
idna is transitive-only, but on three *runtime* edges rather than a
dev-only one — anyio, httpx, and yarl behind discord.py → aiohttp — so
the floor lives in ``[tool.uv].constraint-dependencies``.

**The floor here is 3.15 and creek-tools declares 3.19, and that is
correct.** 3.15 is where the advisory is fixed; creek-tools' higher
number is not a stricter reading of the same advisory but a different
kind of claim — a floor in its ``[project].dependencies`` also records
the resolution that project actually runs (#1000). Harmonising the two
strings would either assert a compatibility claim crawdad has never
tested or contradict #1000, so the difference is deliberate and
documented rather than drift to be tidied away.

``pyjwt`` (issue #1328): pyjwt 2.12.1 carries five advisories —
CVE-2026-48522 through CVE-2026-48526 (PYSEC-2026-175 through -179) —
all fixed in 2.13.0. Transitive-only via mcp → pyjwt[crypto], the same
edge that pulls cryptography, and the library that would verify a
bearer token on the MCP surface this bot consumes.

Its two OSV records disagree about the boundary: PYSEC-2026-176
records ``fixed: 2.12.1`` while its GHSA alias GHSA-jq35-7prp-9v3f
records ``2.13.0``. Both were queried live while closing #1328. The
floor takes the higher number. When two sources disagree about where a
fix lands, a security bound resolves the disagreement upward — the
cost of being one release too strict is a relock, and the cost of
being one release too loose is the advisory.

``urllib3`` (issue #1328): urllib3 2.6.3 carries CVE-2026-44431
(PYSEC-2026-141 / GHSA-qccp-gfcp-xxvc) and CVE-2026-44432
(PYSEC-2026-142 / GHSA-mf9v-mfxr-j63j), both fixed in 2.7.0. 2.6.3
cleared only the earlier CVE-2026-21441, so it sits inside the
vulnerable band rather than above it.

**Read the whole consumer list before calling an edge dev-only.** This
package was one sentence away from being *exempted* from the
cross-manifest parity guard on the reading that crawdad reaches
urllib3 only through pip-audit. It does not. ``google-genai`` is
declared in ``[project].dependencies``, and both google-genai and
google-auth pull ``requests``, which pulls urllib3 — a runtime path
through the Gemini provider. pip-audit, xenon and cachecontrol reach
it as well, and stopping at the first two of five consumers is exactly
how the wrong conclusion was reached. Enumerate every consumer from
the lock, then decide.

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

``openai`` (issue #1479): the second non-CVE pin — an OSV query for
PyPI/openai returns zero advisories, so read this bound as a
transport hold rather than the security ratchet most of this file
describes. openai 3.0.0 (published 2026-08-12) replaced its
``httpx<1,>=0.23.0`` requirement with ``httpx2<3,>=2.7.0``, and httpx2
is a *separate distribution* rather than a version bump: taking it
also drags httpcore2==2.10.0, truststore>=0.10 and idna>=3.18 into
the graph. ``mcp>=1.28.1,<2.0.0`` caps the other half of that same
httpx2 swap (issue #998), so the two majors have to be adopted
jointly or not at all, which is what the ceiling holds open. openai
2.54.0, the newest 2.x, still requires ``httpx<1,>=0.23.0`` and
offers httpx2 only behind an opt-in extra, so the whole 2.x line is
safe; the floor is 2.41.0, the release both locks already resolve,
because a floor records what this project has run and never a version
it has not — and it is asserted from both ends, admitting 2.41.0
while rejecting the release below it, so dropping the floor is as red
as dropping the ceiling.

The stake here is sharper than a future relock. Before #1501, crawdad
CI provisioned with ``pip install -e ".[dev]"`` — a *live* PyPI
resolve that honours neither ``uv.lock`` nor
``[tool.uv].constraint-dependencies`` — so an unbounded
``openai>=1.0`` was not a hypothetical: openai 3.0.0 and its
httpx2/httpcore2 stack already reached a green main build that way
(CI run 31740475473). #1501 closed that specific hole by provisioning
CI from the exported lock instead, but the ceiling still earns its
keep on two narrower grounds: ``uv lock`` IS itself a live resolve the
moment it runs, and a relock is a routine hundreds-of-line diff —
exactly where a transport swap like this one hides; and anyone who
installs crawdad from published metadata without ever touching the
lock (``pip install crawdad[dev]`` against PyPI, say) still resolves
openai live today. The bound in ``[project].dependencies`` is what
stops both, which is also where the two projects differ: crawdad
declares openai as a base dependency, while creek-tools declares it
in ``[project.optional-dependencies].openai``, so the two
``_openai_specifier()`` helpers read different TOML tables by design.

``setuptools`` (issues #1258, #1501): setuptools 81.0.0 carries
PYSEC-2026-3447 (CVE-2026-59890, GHSA-h35f-9h28-mq5c — path traversal
in the ``PackageIndex`` download path), fixed in 83.0.0. It still gets
no ``[tool.uv].constraint-dependencies`` entry, but the reason narrowed
under #1501: crawdad's RUNTIME graph still has no setuptools consumer,
so a constraint there would still tighten the resolution of a package
that is never resolved on that surface — the DEP-003 precedent
explicitly covers packages *already in the graph* — but setuptools now
DOES appear in ``uv.lock`` (``grep -c 'name = "setuptools"'
crawdad/uv.lock`` reports a non-zero count, no longer 0), because
#1501 declares it directly in
``[project.optional-dependencies].dev`` so the environment CI tests
and audits is fully described by the lock rather than left to whatever
the interpreter's ensurepip happened to seed. A direct dev-extra
dependency needs no constraint to tighten — it already carries its own
specifier. ``[build-system].requires`` is the OTHER surface that names
setuptools, governing the isolated environment the build backend runs
in, and it sat at ``>=68.0``: admitting 81.0.0 itself, the exact
release the advisory is filed against. Both surfaces are bounded to
``>=83.0.0,<85.0.0``. 84.0.0 is the vetted release — the newest one
reviewed for this band, and what both crawdad's and creek-tools' locks
now resolve. setuptools versions under SemVer, so ``<85.0.0`` still
admits every future 84.x patch, including the next security fix, while
the unreleased major waits for a deliberate adoption. Before #1501
this pin was the one documented exception to the "two independent
guards per package" convention below, because the lock carried no
setuptools entry to assert against; #1501 closed that gap, and
setuptools now conforms to the same two-guard shape as every other pin
here — a tidier story than the exception it used to be. The guard
discovers the declarations rather than naming them, so if crawdad ever
gains a RUNTIME setuptools consumer and a constraint beside it, the
new surface is covered the day it is added.

Two independent guards per package:

* **pyproject floor** — the declared specifier must reject the last
  vulnerable release (for rpds-py, the last SemVer release) so a
  future relock cannot resolve back down to it.
* **locked version** — ``uv.lock`` is the reproducibility contract
  that ``uv sync`` users install and the second surface
  ``scripts/security.sh`` audits via ``uv export --locked``, so the
  resolved entry must already be at or above the patched version.
  (Since #1501, crawdad's own CI provisions the *installed
  environment* FROM this same lock export, plus an editable install of
  crawdad itself. That environment is still audited separately by the
  bare ``pip-audit`` run added in #979 — it is what would notice a
  live resolve if #1501's provisioning fix were ever reverted.)

Three further guards cover the *wiring* of the security gate itself —
the regression class that produced #979. A pin is only worth as much
as the scanner that would notice its absence, and crawdad's
``scripts/security.sh`` ran bandit alone, so eight advisories sat in
the lock unreported while the gate stayed green. Those tests assert
that ``security.sh`` audits both surfaces — the installed environment
with a bare ``pip-audit``, and the exported lock via ``uv export
--locked`` fed to ``pip-audit -r`` — and that
``[project.optional-dependencies].dev`` actually provisions
``pip-audit`` and ``uv``, since (#1501) the exported lock CI installs
from is resolved FROM this extra (and every other extra, via
``--all-extras``) — a tool missing from ``.[dev]`` never reaches the
lock CI provisions from, whatever else declares it.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name
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

#: First idna release containing the fix for CVE-2026-45409
#: (GHSA-65pc-fj4g-8rjx / PYSEC-2026-215).
_IDNA_PATCHED_VERSION = Version("3.15")

#: The last idna release the advisory still covers. Named as a constant
#: so the boundary probe below is auditable rather than a magic string:
#: 3.14 is a real published release, so the assertion that the floor
#: rejects it is non-vacuous.
_IDNA_LAST_VULNERABLE = Version("3.14")

#: First pyjwt release containing the fixes for all five advisories
#: carried by 2.12.1 — CVE-2026-48522 (PYSEC-2026-175 /
#: GHSA-993g-76c3-p5m4), CVE-2026-48523 (PYSEC-2026-176 /
#: GHSA-jq35-7prp-9v3f), CVE-2026-48524 (PYSEC-2026-177 /
#: GHSA-fhv5-28vv-h8m8), CVE-2026-48525 (PYSEC-2026-178 /
#: GHSA-w7vc-732c-9m39) and CVE-2026-48526 (PYSEC-2026-179 /
#: GHSA-xgmm-8j9v-c9wx).
_PYJWT_PATCHED_VERSION = Version("2.13.0")

#: The last pyjwt release the advisories still cover. OSV's two records
#: for CVE-2026-48523 disagree about this one: PYSEC-2026-176 says
#: ``fixed: 2.12.1`` while its GHSA alias says ``2.13.0`` (both queried
#: live for #1328). The floor takes the higher, safer number — a
#: security bound resolves a source disagreement upward.
_PYJWT_LAST_VULNERABLE = Version("2.12.1")

#: First urllib3 release containing the fixes for BOTH CVE-2026-44431
#: (PYSEC-2026-141 / GHSA-qccp-gfcp-xxvc) and CVE-2026-44432
#: (PYSEC-2026-142 / GHSA-mf9v-mfxr-j63j). 2.6.3 cleared only the
#: earlier CVE-2026-21441 (PYSEC-2026-1996), so it sits inside this
#: band rather than above it.
_URLLIB3_PATCHED_VERSION = Version("2.7.0")

#: The last urllib3 release both advisories still cover.
_URLLIB3_LAST_VULNERABLE = Version("2.6.3")

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

#: First setuptools release containing the fix for PYSEC-2026-3447
#: (CVE-2026-59890 / GHSA-h35f-9h28-mq5c — path traversal in the
#: ``PackageIndex`` download path). Held identical to creek-tools'
#: floor: the two projects build with the same backend and letting
#: them admit different setuptools bands is the split this pin
#: prevents.
_SETUPTOOLS_PATCHED_VERSION = Version("83.0.0")

#: The release the advisory is filed against, and the one
#: ``[build-system].requires = ["setuptools>=68.0"]`` used to admit.
#: The PEP 518 isolated build environment this bound governs resolves
#: independently of ``uv.lock`` even after #1501 — ``uv lock`` never
#: sees ``[build-system].requires`` — so the pyproject band on THIS
#: surface is still the only thing standing between this release and a
#: build; there is still no lockfile assertion for this surface
#: specifically. (The separate dev-extra declaration #1501 added is a
#: different surface, and it does have its own lock guard — see
#: ``test_locked_setuptools_sits_inside_the_vetted_band``.)
_SETUPTOOLS_LAST_VULNERABLE = Version("81.0.0")

#: The vetted setuptools release: the newest one reviewed for this
#: band, and what creek-tools' lock resolves. Since #1501, it is also
#: what crawdad's OWN ``uv.lock`` resolves, for the dev-extra
#: declaration — see ``test_locked_setuptools_sits_inside_the_vetted_
#: band``. This constant is used here to assert the pyproject band
#: itself (both surfaces) from both ends, since a ceiling that excluded
#: the release the backend actually installs would break every build.
_SETUPTOOLS_LOCKED_VERSION = Version("84.0.0")

#: The next major — unreleased, so no changelog has been read and no
#: build has run against it. setuptools has removed long-deprecated
#: surfaces at majors before (``setup.py test``, ``easy_install``, the
#: bundled distutils shim), so the ceiling holds it for a deliberate
#: adoption exactly as mcp is held at <2.0.0.
_SETUPTOOLS_UNVETTED_MAJOR = Version("85.0.0")

#: A far-future major. Probes for a lazy ``!=85.0.0`` exclusion posing
#: as a ceiling: that satisfies the assertion on 85.0.0 while
#: re-admitting 85.0.1 and every later major. Only a probe well past
#: the bound tells a real upper bound from a single-release exclusion.
_SETUPTOOLS_FUTURE_MAJOR_PROBE = Version("999.0.0")

#: Dotted paths of the tables whose lists hold PEP 508 requirement
#: strings. The setuptools walk visits only these — see
#: ``_setuptools_declarations`` for why walking the whole document
#: would be a hazard rather than a thoroughness.
_DEPENDENCY_TABLE_PATHS = frozenset(
    {
        "build-system.requires",
        "project.dependencies",
        "tool.uv.constraint-dependencies",
        "tool.uv.build-constraint-dependencies",
        "tool.uv.override-dependencies",
    }
)

#: Dotted-path prefixes under which *every* child list declares
#: dependencies: one list per extra in ``[project.optional-dependencies]``
#: and one per group in ``[dependency-groups]``. Named as prefixes
#: because the extra and group names are open-ended.
_DEPENDENCY_TABLE_PREFIXES = ("project.optional-dependencies.", "dependency-groups.")

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

#: Shell builtins that only *look up* a command (``command -v
#: pip-audit``) instead of running it; a lookup must not count as an
#: audit.
_LOOKUP_BUILTINS = frozenset({"command", "hash", "type", "which"})

#: Helpers that *probe* for a tool without running it. ``security.sh``
#: guards its audits with ``crawdad_require_python_module pip_audit
#: pip-audit`` (#1671), and that line mentions pip-audit by name — so
#: without this exclusion the probe itself would satisfy the
#: "an environment audit exists" assertion below while the real audit
#: had been deleted. Same reasoning as ``_LOOKUP_BUILTINS``.
_PROBE_COMMANDS = frozenset({"crawdad_require_python_module"})

#: The module name ``pip-audit`` is imported and executed under. The
#: gate invokes it as ``python -m pip_audit`` rather than by bare name,
#: so that the audit runs in the interpreter under audit instead of
#: whichever copy PATH resolves first (#1671).
_PIP_AUDIT_MODULE = "pip_audit"


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
    """Return the ``openai`` specifier set from ``[project].dependencies``.

    crawdad declares openai as a *base* dependency — ``CRAWDAD_PROVIDER``
    selects the backend at runtime, so every install carries the SDK —
    rather than as the optional extra creek-tools declares it in
    (``[project.optional-dependencies].openai``). The two projects'
    helpers therefore read different tables.

    Returns:
        The specifier set attached to the ``openai`` entry in
        ``[project].dependencies``. Fails the calling test if the entry
        is absent.
    """
    with _PYPROJECT.open("rb") as handle:
        pyproject = tomllib.load(handle)
    dependencies: list[str] = pyproject["project"]["dependencies"]
    for entry in dependencies:
        requirement = Requirement(entry)
        if requirement.name == "openai":
            return requirement.specifier
    pytest.fail("openai is not declared in [project].dependencies of pyproject.toml")


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


def _idna_constraint_specifier() -> SpecifierSet:
    """Return the ``idna`` specifier from uv constraints.

    Reads ``[tool.uv].constraint-dependencies`` in ``pyproject.toml``,
    the home for floors on transitive-only packages (DEP-003).

    Returns:
        The specifier set attached to the ``idna`` constraint entry.
        Fails the calling test if the ``[tool.uv]`` table or the
        ``idna`` entry is absent.
    """
    with _PYPROJECT.open("rb") as handle:
        pyproject = tomllib.load(handle)
    constraints: list[str] = (
        pyproject.get("tool", {}).get("uv", {}).get("constraint-dependencies", [])
    )
    for entry in constraints:
        requirement = Requirement(entry)
        if requirement.name == "idna":
            return requirement.specifier
    pytest.fail(
        "idna has no entry in [tool.uv].constraint-dependencies of pyproject.toml"
    )


def _locked_idna_version() -> Version:
    """Return the resolved ``idna`` version pinned in ``uv.lock``.

    Returns:
        The ``idna`` version resolved in the lockfile. Fails the calling
        test if the lock has no ``idna`` package entry.
    """
    with _UV_LOCK.open("rb") as handle:
        lock = tomllib.load(handle)
    packages: list[dict[str, object]] = lock["package"]
    for package in packages:
        if package["name"] == "idna":
            return Version(str(package["version"]))
    pytest.fail("idna has no [[package]] entry in uv.lock")


def _urllib3_constraint_specifier() -> SpecifierSet:
    """Return the ``urllib3`` specifier from uv constraints.

    Reads ``[tool.uv].constraint-dependencies`` in ``pyproject.toml``,
    the home for floors on transitive-only packages (DEP-003).

    Returns:
        The specifier set attached to the ``urllib3`` constraint entry.
        Fails the calling test if the ``[tool.uv]`` table or the
        ``urllib3`` entry is absent.
    """
    with _PYPROJECT.open("rb") as handle:
        pyproject = tomllib.load(handle)
    constraints: list[str] = (
        pyproject.get("tool", {}).get("uv", {}).get("constraint-dependencies", [])
    )
    for entry in constraints:
        requirement = Requirement(entry)
        if requirement.name == "urllib3":
            return requirement.specifier
    pytest.fail(
        "urllib3 has no entry in [tool.uv].constraint-dependencies of pyproject.toml"
    )


def _locked_urllib3_version() -> Version:
    """Return the resolved ``urllib3`` version pinned in ``uv.lock``.

    Returns:
        The ``urllib3`` version resolved in the lockfile. Fails the
        calling test if the lock has no ``urllib3`` package entry.
    """
    with _UV_LOCK.open("rb") as handle:
        lock = tomllib.load(handle)
    packages: list[dict[str, object]] = lock["package"]
    for package in packages:
        if package["name"] == "urllib3":
            return Version(str(package["version"]))
    pytest.fail("urllib3 has no [[package]] entry in uv.lock")


def _pyjwt_constraint_specifier() -> SpecifierSet:
    """Return the ``pyjwt`` specifier from uv constraints.

    Reads ``[tool.uv].constraint-dependencies`` in ``pyproject.toml``,
    the home for floors on transitive-only packages (DEP-003).

    Returns:
        The specifier set attached to the ``pyjwt`` constraint entry.
        Fails the calling test if the ``[tool.uv]`` table or the
        ``pyjwt`` entry is absent.
    """
    with _PYPROJECT.open("rb") as handle:
        pyproject = tomllib.load(handle)
    constraints: list[str] = (
        pyproject.get("tool", {}).get("uv", {}).get("constraint-dependencies", [])
    )
    for entry in constraints:
        requirement = Requirement(entry)
        if requirement.name == "pyjwt":
            return requirement.specifier
    pytest.fail(
        "pyjwt has no entry in [tool.uv].constraint-dependencies of pyproject.toml"
    )


def _locked_pyjwt_version() -> Version:
    """Return the resolved ``pyjwt`` version pinned in ``uv.lock``.

    Returns:
        The ``pyjwt`` version resolved in the lockfile. Fails the
        calling test if the lock has no ``pyjwt`` package entry.
    """
    with _UV_LOCK.open("rb") as handle:
        lock = tomllib.load(handle)
    packages: list[dict[str, object]] = lock["package"]
    for package in packages:
        if package["name"] == "pyjwt":
            return Version(str(package["version"]))
    pytest.fail("pyjwt has no [[package]] entry in uv.lock")


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


def _is_dependency_declaring(path: str) -> bool:
    """Return whether a dotted TOML path names a dependency list.

    Args:
        path: Dotted path of a value in the parsed document, such as
            ``build-system.requires``.

    Returns:
        ``True`` when a list at ``path`` holds PEP 508 requirement
        strings — the fixed dependency tables, plus every extra under
        ``[project.optional-dependencies]`` and every group under
        ``[dependency-groups]``.
    """
    return path in _DEPENDENCY_TABLE_PATHS or path.startswith(
        _DEPENDENCY_TABLE_PREFIXES
    )


def _setuptools_entries_at(
    path: str, entries: list[object]
) -> list[tuple[str, SpecifierSet]]:
    """Return the ``setuptools`` requirements declared in one list.

    Args:
        path: Dotted path of the list, carried into every result pair so
            a failing assertion can name the surface at fault.
        entries: The list exactly as ``tomllib`` parsed it. Non-string
            elements are skipped (``[dependency-groups]`` may hold
            ``{include-group = "..."}`` inline tables), as is anything
            ``packaging`` refuses to parse.

    Returns:
        One ``(path, specifier)`` pair per ``setuptools`` entry, matched
        on the canonical (PEP 503) name so ``Setuptools`` or
        ``SETUPTOOLS`` cannot slip past the comparison.
    """
    found: list[tuple[str, SpecifierSet]] = []
    for entry in entries:
        if not isinstance(entry, str):
            continue
        try:
            requirement = Requirement(entry)
        except InvalidRequirement:
            continue
        if canonicalize_name(requirement.name) == "setuptools":
            found.append((path, requirement.specifier))
    return found


def _walk_for_setuptools(
    node: object, path: str, found: list[tuple[str, SpecifierSet]]
) -> None:
    """Accumulate the ``setuptools`` declarations reachable from *node*.

    List elements are walked under the *same* dotted path, because an
    array of tables (``[[tool.mypy.overrides]]``) gives every one of its
    entries that single path — which is also why the result is a list of
    pairs rather than a mapping keyed by path.

    Args:
        node: A parsed TOML value. Typed ``object`` and narrowed with
            ``isinstance`` so ``tomllib``'s ``Any`` cannot leak into the
            annotated return of ``_setuptools_declarations`` under
            mypy's ``warn_return_any``.
        path: Dotted path of ``node``; the empty string at the document
            root.
        found: Accumulator, appended to in place.
    """
    if isinstance(node, dict):
        for key, value in node.items():
            child = f"{path}.{key}" if path else str(key)
            _walk_for_setuptools(value, child, found)
        return
    if not isinstance(node, list):
        return
    if _is_dependency_declaring(path):
        found.extend(_setuptools_entries_at(path, node))
    for element in node:
        _walk_for_setuptools(element, path, found)


def _setuptools_declarations() -> list[tuple[str, SpecifierSet]]:
    """Return every ``setuptools`` requirement ``pyproject.toml`` declares.

    setuptools can be declared on three independent surfaces:
    ``[tool.uv].constraint-dependencies``, which governs the resolution
    graph behind ``uv.lock``; ``[build-system].requires``, which governs
    the isolated environment the build backend runs in; and, since
    #1501, ``[project.optional-dependencies].dev``, declared so the
    environment CI tests and audits is fully described by the lock.
    crawdad uses the second and third — nothing in its RUNTIME graph
    consumes setuptools, so the first still has no resolution to
    constrain — and nothing in this file read ``[build-system]`` before
    issue #1258, which is how ``setuptools>=68.0`` sat here admitting
    the PYSEC-2026-3447 release unremarked. Discovering the declarations
    beats naming them: if a runtime consumer and a constraint ever
    appear, the new surface is guarded the day it is added — #1501 is
    itself an example, since the dev-extra declaration was picked up by
    this same walk with no code change here.

    The walk is deliberately *restricted* to dependency-declaring table
    paths instead of reading every list in the document. An
    unrestricted walk parses every string in the file — well over a
    hundred, across two dozen table paths — including the ``module``
    lists of this pyproject's ``[[tool.mypy.overrides]]`` entries. Those
    are module patterns, not requirements: a pattern like
    ``setuptools.*`` raises ``InvalidRequirement`` and is skipped
    harmlessly, but the bare form ``module = ["setuptools"]`` parses
    cleanly into a requirement with an *empty* specifier, and an empty
    ``SpecifierSet`` admits 81.0.0. A future mypy override written that
    way would then fail the parity guard with a security verdict about
    a line that has nothing to do with dependency resolution — a false
    alarm that teaches the next reader to distrust the guard.
    Restricted, the walk finds ``build-system.requires`` and, since
    #1501, ``project.optional-dependencies.dev``.

    One boundary is worth stating because it is invisible from here:
    this walk only reads PEP 508 requirement strings, so it cannot see
    a ``[tool.uv.sources]`` entry, which redirects a dependency to a
    git/path/URL source and bypasses version specifiers entirely.
    Neither project declares that table today; if one ever does, this
    guard does not cover it and a separate assertion is needed.

    Returns:
        One ``(dotted path, specifier)`` pair per declaration, in
        document order. A list rather than a mapping because
        array-of-tables entries share a dotted path and a mapping would
        silently drop all but the last of them.
    """
    with _PYPROJECT.open("rb") as handle:
        pyproject = tomllib.load(handle)
    found: list[tuple[str, SpecifierSet]] = []
    _walk_for_setuptools(pyproject, "", found)
    return found


def _locked_setuptools_version() -> Version:
    """Return the resolved ``setuptools`` version pinned in ``uv.lock``.

    Returns:
        The ``setuptools`` version resolved in the lockfile. Fails the
        calling test if the lock has no ``setuptools`` package entry.
        Absence is a failure rather than a skip because #1501 declares
        setuptools in ``[project.optional-dependencies].dev``: the lock
        must carry it, so a missing entry means the lock is stale.
    """
    with _UV_LOCK.open("rb") as handle:
        lock = tomllib.load(handle)
    packages: list[dict[str, object]] = lock["package"]
    for package in packages:
        if package["name"] == "setuptools":
            return Version(str(package["version"]))
    pytest.fail(
        "setuptools has no [[package]] entry in uv.lock; #1501 declares "
        "setuptools in [project.optional-dependencies].dev, so an absent "
        "entry means the lock is stale rather than that setuptools is "
        "legitimately unlocked — run `uv lock`"
    )


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
    if tokens and tokens[0] in _PROBE_COMMANDS:
        return False
    if any(token.rsplit("/", maxsplit=1)[-1] == "pip-audit" for token in tokens):
        return True
    return _runs_python_module(tokens, _PIP_AUDIT_MODULE)


def _runs_python_module(tokens: list[str], module: str) -> bool:
    """Return whether a tokenized command is ``python -m <module>``.

    Args:
        tokens: Shell tokens of a single command.
        module: The importable module name, e.g. ``pip_audit``.

    Returns:
        ``True`` when the command executes ``module`` through the active
        interpreter. Any ``python``-prefixed argv[0] counts, so
        ``python3`` and an absolute interpreter path are both
        recognised.
    """
    if len(tokens) < 3:
        return False
    interpreter = tokens[0].rsplit("/", maxsplit=1)[-1]
    return (
        interpreter.startswith("python") and tokens[1] == "-m" and tokens[2] == module
    )


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
    (since #1501, CI provisions the installed environment FROM this
    same lock; the bare ``pip-audit`` run still audits that environment
    separately, since it also holds the editable install of crawdad
    itself and whatever the build backend or ensurepip left behind); a
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
    ``httpx2<3,>=2.7.0``, a separate distribution arriving with
    httpcore2, truststore and idna>=3.18. An open ceiling is not merely
    a future relock risk: that stack already landed on a green main
    build once, before #1501 moved CI off a live ``pip install -e
    ".[dev]"`` resolve. Since #1501, ``uv lock`` is where a live resolve
    now happens instead, and a relock is a routine hundreds-of-line
    diff — exactly where a transport swap like this one hides — while
    anyone installing crawdad from published metadata without ever
    touching the lock still resolves openai live today regardless. The
    ``<3.0.0`` ceiling is what stops it, and it may lift only jointly
    with the mcp cap on the same swap.

    The second assertion is the one that catches a half-measure: a bare
    ``!=3.0.0`` reads like a ceiling and is not one.
    """
    specifier = _openai_specifier()
    assert str(_OPENAI_HTTPX2_MAJOR) not in specifier, (
        f"openai specifier {specifier!r} admits {_OPENAI_HTTPX2_MAJOR}, "
        "which replaces httpx with httpx2 — a separate distribution, not "
        "a version bump — dragging httpcore2, truststore and idna>=3.18 "
        "into the resolved graph; a relock is a routine diff and exactly "
        "where a transport swap like this hides, and anyone installing "
        "without the lock resolves live regardless; the ceiling must be "
        "<3.0.0 and may move only jointly with the mcp<2.0.0 cap on the "
        "same swap (#1479, #998, #1501)"
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
    ``openai<3.0.0`` satisfies every other assertion in this file. The
    floor matters on both provisioning paths: ``uv lock`` reads it when
    resolving the lock CI installs from since #1501, and a bare ``pip
    install -e ".[dev]"`` against published metadata — the pre-#1501 CI
    shape, and still how anyone installing without the lock resolves
    today — reads it directly too, since pip honours the declared
    specifier even though it ignores ``uv.lock`` itself.
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

    ``uv.lock`` is the reproducibility contract ``uv sync`` users
    install and the surface ``scripts/security.sh`` audits via ``uv
    export --locked``. A manifest bounded to the 2.x line while the lock
    sits outside that bound is precisely the drift this pair of guards
    exists to catch — the declaration would be right and the resolved
    environment still wrong.
    """
    locked = _locked_openai_version()
    assert str(locked) in _openai_specifier(), (
        f"uv.lock pins openai {locked}, which the declared specifier "
        f"{_openai_specifier()!r} rejects; a bounded manifest with a lock "
        "outside the bound is the drift this guards — run `uv lock` after "
        "changing the openai declaration (#1479)"
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
    (since #1501, CI provisions the installed environment FROM this
    same lock; the bare ``pip-audit`` run still audits that environment
    separately, since it also holds the editable install of crawdad
    itself and whatever the build backend or ensurepip left behind); a
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
    (since #1501, CI provisions the installed environment FROM this
    same lock; the bare ``pip-audit`` run still audits that environment
    separately, since it also holds the editable install of crawdad
    itself and whatever the build backend or ensurepip left behind); a
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


def test_idna_floor_rejects_vulnerable_releases() -> None:
    """The constraint excludes 3.14, the last release the advisory covers.

    idna 3.14 carries CVE-2026-45409 (GHSA-65pc-fj4g-8rjx /
    PYSEC-2026-215), fixed in 3.15. 3.14 is a real published release —
    the line runs 3.13, 3.14, 3.15, ... — so this probe is
    non-vacuous rather than testing a version that never existed.
    """
    specifier = _idna_constraint_specifier()
    assert str(_IDNA_LAST_VULNERABLE) not in specifier, (
        f"idna constraint {specifier!r} admits {_IDNA_LAST_VULNERABLE}, the "
        "last release carrying CVE-2026-45409 / GHSA-65pc-fj4g-8rjx / "
        f"PYSEC-2026-215; the floor must be >={_IDNA_PATCHED_VERSION}"
    )


def test_idna_floor_accepts_patched_release() -> None:
    """The constraint accepts 3.15, the first patched release.

    The floor is the *advisory* number, not creek-tools' number.
    creek-tools declares ``idna>=3.19`` because a floor there also
    records the resolution that project runs (#1000); 3.15 is where the
    fix lands. The two are deliberately different and must not be
    "harmonised" — raising this floor to 3.19 would assert a
    compatibility claim crawdad has never tested.
    """
    specifier = _idna_constraint_specifier()
    assert str(_IDNA_PATCHED_VERSION) in specifier, (
        f"idna constraint {specifier!r} rejects {_IDNA_PATCHED_VERSION}; the "
        "patched release itself must satisfy the constraint"
    )


def test_locked_idna_at_or_above_patched_release() -> None:
    """``uv.lock`` resolves idna to >= 3.15.

    idna reaches this graph on three runtime edges — anyio, httpx, and
    yarl behind discord.py → aiohttp — so a stale lock ships the
    vulnerable parser into the bot's own HTTP path, not just a dev
    tool.
    """
    locked = _locked_idna_version()
    assert locked >= _IDNA_PATCHED_VERSION, (
        f"uv.lock pins idna {locked}, below the patched "
        f"{_IDNA_PATCHED_VERSION} (CVE-2026-45409 / GHSA-65pc-fj4g-8rjx); "
        "the exported lock is audited, so relock after adding the constraint"
    )


def test_urllib3_floor_rejects_vulnerable_releases() -> None:
    """The constraint excludes 2.6.3, which still carries both advisories.

    urllib3 2.6.3 cleared the earlier CVE-2026-21441 but still carries
    CVE-2026-44431 and CVE-2026-44432, both fixed in 2.7.0. Stopping at
    ``>=2.6.3`` would look like a security floor and admit two live
    advisories, so the probe pins the boundary rather than the general
    direction.
    """
    specifier = _urllib3_constraint_specifier()
    assert str(_URLLIB3_LAST_VULNERABLE) not in specifier, (
        f"urllib3 constraint {specifier!r} admits {_URLLIB3_LAST_VULNERABLE}, "
        "which still carries CVE-2026-44431 and CVE-2026-44432; the floor "
        f"must be >={_URLLIB3_PATCHED_VERSION}"
    )


def test_urllib3_floor_accepts_patched_release() -> None:
    """The constraint accepts 2.7.0, the first fully patched release."""
    specifier = _urllib3_constraint_specifier()
    assert str(_URLLIB3_PATCHED_VERSION) in specifier, (
        f"urllib3 constraint {specifier!r} rejects {_URLLIB3_PATCHED_VERSION}; "
        "the patched release itself must satisfy the constraint"
    )


def test_locked_urllib3_at_or_above_patched_release() -> None:
    """``uv.lock`` resolves urllib3 to >= 2.7.0.

    urllib3 is transitive here, but it is NOT the dev-only edge it
    first looks like. crawdad declares ``google-genai`` in
    ``[project].dependencies``, and both google-genai and google-auth
    pull ``requests``, which pulls urllib3 — so the Gemini provider
    reaches it on a runtime path. pip-audit and xenon reach it too;
    those are the dev edges, and reading only those is what made the
    package look exemptible while closing #1328.
    """
    locked = _locked_urllib3_version()
    assert locked >= _URLLIB3_PATCHED_VERSION, (
        f"uv.lock pins urllib3 {locked}, below the patched "
        f"{_URLLIB3_PATCHED_VERSION} (CVE-2026-44431 / CVE-2026-44432); the "
        "exported lock is audited, so relock after adding the constraint"
    )


def test_pyjwt_floor_rejects_vulnerable_releases() -> None:
    """The constraint excludes 2.12.1, which still carries all five advisories.

    pyjwt 2.12.1 carries CVE-2026-48522 through CVE-2026-48526, every
    one of them fixed at 2.13.0. OSV's PYSEC-2026-176 record claims
    ``fixed: 2.12.1`` while its own GHSA alias says 2.13.0; the floor
    takes the higher number, so the disagreement cannot leave a hole.
    """
    specifier = _pyjwt_constraint_specifier()
    assert str(_PYJWT_LAST_VULNERABLE) not in specifier, (
        f"pyjwt constraint {specifier!r} admits {_PYJWT_LAST_VULNERABLE}, "
        "which still carries CVE-2026-48522 through CVE-2026-48526; the "
        f"floor must be >={_PYJWT_PATCHED_VERSION}"
    )


def test_pyjwt_floor_accepts_patched_release() -> None:
    """The constraint accepts 2.13.0, the first fully patched release."""
    specifier = _pyjwt_constraint_specifier()
    assert str(_PYJWT_PATCHED_VERSION) in specifier, (
        f"pyjwt constraint {specifier!r} rejects {_PYJWT_PATCHED_VERSION}; "
        "the patched release itself must satisfy the constraint"
    )


def test_locked_pyjwt_at_or_above_patched_release() -> None:
    """``uv.lock`` resolves pyjwt to >= 2.13.0.

    pyjwt arrives through ``mcp -> pyjwt[crypto]``, the same edge that
    pulls cryptography, and it is the library that would verify a
    bearer token on the MCP surface this bot consumes. The exported
    lock is what ``scripts/security.sh`` audits, so the constraint is
    only half the guard.
    """
    locked = _locked_pyjwt_version()
    assert locked >= _PYJWT_PATCHED_VERSION, (
        f"uv.lock pins pyjwt {locked}, below the patched "
        f"{_PYJWT_PATCHED_VERSION} (CVE-2026-48522 through CVE-2026-48526); "
        "the exported lock is audited, so relock after adding the constraint"
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


def test_every_setuptools_declaration_carries_the_vetted_band() -> None:
    """Every declared setuptools surface carries the same vetted band.

    crawdad declares setuptools on two surfaces: ``[build-system].requires``
    — the isolated environment the build backend runs in — and, since
    #1501, ``[project.optional-dependencies].dev``, declared so the
    environment CI tests and audits is fully described by the lock
    rather than left to whatever the interpreter's ensurepip happened to
    seed. It still gets no ``[tool.uv].constraint-dependencies`` entry:
    nothing in crawdad's RUNTIME graph consumes setuptools, and a
    constraint tightens the resolution of a package already in the
    graph rather than putting one there, while the dev-extra declaration
    is a direct dependency that already carries its own specifier and
    needs no constraint beside it. This test walks whatever surfaces
    exist and holds every one of them to the same band — it does not
    assume there are exactly two, at ``>=68.0`` the build surface alone
    admitted 81.0.0, the PYSEC-2026-3447 release itself (#1258).

    The guard discovers the declarations rather than naming them, so a
    surface added later — a runtime consumer and its constraint, or
    another extra — is covered the day it appears; #1501 adding the
    dev-extra declaration is itself an example, picked up with no code
    change here. The count assertion is what keeps that honest: a walk
    that returned nothing would otherwise make the loop below vacuous
    and the test green.
    """
    declarations = _setuptools_declarations()
    assert len(declarations) >= 1, (
        f"the pyproject walk found {len(declarations)} setuptools "
        f"declaration(s) ({declarations!r}); crawdad declares setuptools "
        "in [build-system].requires, so an empty result means the walk is "
        "broken, and a guard iterating an empty list passes while "
        "checking nothing"
    )
    paths = {path for path, _ in declarations}
    assert {"build-system.requires"} <= paths, (
        f"the setuptools walk reached {sorted(paths)}, missing "
        "build-system.requires — the surface that selects the setuptools "
        "build backend. Since #1501 it is no longer the only setuptools "
        "surface crawdad declares (project.optional-dependencies.dev "
        "carries the same band, and its resolved uv.lock entry is "
        "asserted separately by "
        "test_locked_setuptools_sits_inside_the_vetted_band), but nothing "
        "else bounds build-system.requires itself — only this walk does "
        "(#1258, #1501)"
    )
    for path, specifier in declarations:
        assert str(_SETUPTOOLS_LAST_VULNERABLE) not in specifier, (
            f"{path} declares setuptools {specifier!r}, which admits "
            f"{_SETUPTOOLS_LAST_VULNERABLE} — the release carrying "
            "PYSEC-2026-3447 / CVE-2026-59890, path traversal in the "
            "PackageIndex download path. The build backend resolves this "
            "surface live, so the floor must be "
            f"{_SETUPTOOLS_PATCHED_VERSION} (#1258)"
        )
        assert str(_SETUPTOOLS_UNVETTED_MAJOR) not in specifier, (
            f"{path} declares setuptools {specifier!r}, which admits "
            f"{_SETUPTOOLS_UNVETTED_MAJOR}; that major is unreleased, so "
            "no changelog has been read and no build has run against it. "
            f"Carry the same <{_SETUPTOOLS_UNVETTED_MAJOR} ceiling "
            "creek-tools carries (#1258)"
        )
        assert str(_SETUPTOOLS_FUTURE_MAJOR_PROBE) not in specifier, (
            f"{path} declares setuptools {specifier!r}, which admits "
            f"{_SETUPTOOLS_FUTURE_MAJOR_PROBE}; an exclusion of the one "
            f"release (`!={_SETUPTOOLS_UNVETTED_MAJOR}`) reads like a "
            "ceiling and is not one — it re-admits 85.0.1 and every later "
            "major. Write a real upper bound (#1258)"
        )
        assert str(_SETUPTOOLS_PATCHED_VERSION) in specifier, (
            f"{path} declares setuptools {specifier!r}, which rejects "
            f"{_SETUPTOOLS_PATCHED_VERSION}, the first release carrying "
            "the PYSEC-2026-3447 fix; the floor is 83.0.0 and no ceiling "
            "may swallow it (#1258)"
        )
        assert str(_SETUPTOOLS_LOCKED_VERSION) in specifier, (
            f"{path} declares setuptools {specifier!r}, which rejects "
            f"{_SETUPTOOLS_LOCKED_VERSION}, the vetted release this band "
            "was reviewed against and the one creek-tools' lock resolves; "
            "a band that excludes the build the backend installs breaks "
            "every build instead of failing here (#1258)"
        )


def test_locked_setuptools_sits_inside_the_vetted_band() -> None:
    """``uv.lock`` resolves setuptools inside the ``>=83.0.0,<85.0.0`` band.

    This is the second of the two guards every other package in this
    file gets, and it exists because #1501 changed the fact that
    justified setuptools going without one. #1258 bounded
    ``[build-system].requires`` alone and argued the exception
    explicitly: crawdad's runtime graph had no setuptools consumer, its
    ``uv.lock`` had no setuptools entry to assert against, and so the
    build surface was the *whole* pin. #1501 declares
    ``setuptools>=83.0.0,<85.0.0`` in
    ``[project.optional-dependencies].dev`` — so that CI, which now
    installs from the lock rather than resolving live against PyPI,
    provisions a bounded setuptools — and relocking writes a real
    setuptools entry into ``uv.lock``. The moment that entry exists the
    #1258 justification is false, and without this test the resolved
    version would be *unasserted*: Phase 2 would have traded one
    unbounded surface (a live PyPI resolve) for one unguarded one (a
    locked version nothing reads). This closes exactly that gap.

    The lock is worth asserting on its own terms, not merely for
    symmetry. It is what ``uv sync`` users install, what CI installs
    after #1501, and the second surface ``scripts/security.sh`` audits
    via ``uv export --locked``; a correct declared band with a stale
    lock still ships the version the band excludes. So the band is
    checked from both ends — at or above ``_SETUPTOOLS_PATCHED_VERSION``
    (83.0.0, the first release carrying the PYSEC-2026-3447 /
    CVE-2026-59890 fix for path traversal in the ``PackageIndex``
    download path) and below ``_SETUPTOOLS_UNVETTED_MAJOR`` (85.0.0, the
    unreleased major no changelog has been read for). A lock that drifts
    past the ceiling is as red as one that regresses under the floor.

    #1501 also adds ``wheel`` to the dev extra, and it deliberately gets
    no companion guard here: wheel carries no advisory band, so there is
    nothing to assert about its resolved version beyond presence, and a
    presence-only test would pass for the wrong reason forever.
    """
    locked = _locked_setuptools_version()
    assert locked >= _SETUPTOOLS_PATCHED_VERSION, (
        f"uv.lock pins setuptools {locked}, below the patched "
        f"{_SETUPTOOLS_PATCHED_VERSION} (PYSEC-2026-3447 / CVE-2026-59890, "
        "path traversal in the PackageIndex download path). Since #1501 CI "
        "installs from this lock, so the resolved version is what actually "
        "runs — relock onto the vetted release (#1258)"
    )
    assert locked < _SETUPTOOLS_UNVETTED_MAJOR, (
        f"uv.lock pins setuptools {locked}, at or above "
        f"{_SETUPTOOLS_UNVETTED_MAJOR} — the unvetted major the declared "
        "band excludes. A lock that has drifted past the ceiling means the "
        "band in [project.optional-dependencies].dev is no longer what CI "
        "installs; adopt the major deliberately or relock (#1501, #1258)"
    )


def test_security_script_audits_installed_environment() -> None:
    """``security.sh`` runs pip-audit against the live environment.

    Since #1501, CI provisions this environment FROM the exported lock,
    so it is no longer a live PyPI resolve on a normal day — but it is
    still not the same artifact as the lock export below: the editable
    install of crawdad itself, and whatever the interpreter's own
    ensurepip seeded or a PEP 517 build backend left behind, are present
    in the installed environment and invisible to a plain lock export.
    This pass is also the regression detector for #1501 itself: pip
    honours neither ``uv.lock`` nor
    ``[tool.uv].constraint-dependencies``, so if a future edit ever
    reintroduces a live resolve, this is the pass that would notice —
    the lock export below cannot. Only an audit with no ``-r`` /
    ``--requirement`` inspects what is actually installed.

    "Bare" here means bare of a requirements file, not bare of an
    interpreter. Since #1671 the invocation is ``python -m pip_audit``:
    a bare *executable* name is resolved by PATH, which silently
    supplied Homebrew's copy when this project's environment had none,
    auditing that interpreter instead of this one.
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

    Since #1501, the crawdad CI job no longer installs ``.[dev]``
    directly — it installs the export of `uv lock`'s resolution
    (``--all-extras``, so every extra is covered) and then runs
    ``check-all.sh``. That export is resolved FROM this extra, so a
    tool the security script invokes but the extra omits still never
    reaches the lock CI installs from, and fails CI outright — or,
    worse, is silently unavailable — exactly as before #1501. This
    pins the provisioning contract to the script's actual needs.
    """
    names = _dev_extra_requirement_names()
    assert "pip-audit" in names, (
        "[project.optional-dependencies].dev omits pip-audit, the "
        "scanner scripts/security.sh runs; the exported lock CI installs "
        "from is resolved FROM this extra"
    )
    assert "uv" in names, (
        "[project.optional-dependencies].dev omits uv, which "
        "scripts/security.sh needs for `uv export --locked`; the "
        "exported lock CI installs from is resolved FROM this extra"
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
