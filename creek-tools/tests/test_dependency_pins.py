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
``[project].dependencies`` (DEP-003 precedent). The band is
``>=83.0.0,<85.0.0`` (issue #1258). The floor does not move: 83.0.0 is
still the first patched release. The ceiling is new, and reads the way
the mcp ``<2.0.0`` cap reads rather than as a second advisory — 84.0.0
is the release ``uv.lock`` resolves and every build here has actually
run against, setuptools versions under SemVer so ``<85.0.0`` keeps
admitting every future 84.x patch (including the next security fix),
and 85.0.0 does not exist yet, so nothing about it has been read or
tested.

setuptools is declared on *two* independent surfaces, which is the
part worth remembering. ``[tool.uv].constraint-dependencies`` governs
the resolution graph — what lands in ``uv.lock``.
``[build-system].requires`` governs the isolated environment the build
backend runs in, and the constraint provably does not reach it:
constrained to ``<84.0.0`` a build still ran setuptools 84.0.0, while
bounding ``[build-system].requires`` produced 83.0.0. Only the second
surface was left at ``>=68.0`` by #861, which admits 81.0.0 itself —
the exact release the pin exists to exclude — because no test in this
file read ``[build-system]`` at all. Both surfaces now carry the
identical band and must move together;
``test_every_setuptools_declaration_carries_the_vetted_band``
discovers them instead of naming them, so a third declaration is
guarded the day it is added rather than the day someone remembers.

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

``python-multipart`` (issue #1328): python-multipart 0.0.29 carries
*four* advisories — PYSEC-2026-3036 (CVE-2026-53539), PYSEC-2026-3037
(CVE-2026-53538), PYSEC-2026-3041 (CVE-2026-53537) and PYSEC-2026-3040
(CVE-2026-53540). 0.0.30 clears three of them and leaves
PYSEC-2026-3040 open, so the floor is 0.0.31, the first fully patched
release. Count the advisories before trusting a summary: the older
crawdad comment names three and omits PYSEC-2026-3041, and both design
passes on #1328 inherited that undercount.

python-multipart is transitive-only, arriving through mcp →
python-multipart, so the floor lives in
``[tool.uv].constraint-dependencies``. This project's own ``/v1``
routes never parse multipart — uploads are JSON + base64 by design —
so the reachable surface is the MCP SDK's Starlette app that
``creek_mcp.server`` mounts under the streamable-http transport.

The durable lesson is the *pair*, not the version. crawdad floored
this package and creek-tools did not, and nothing failed, because
until #1328 no test compared the two manifests. The parity guard at
the end of this module does that now, in both directions, and reports
every gap in one message.

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

``pyarrow``, ``sentence-transformers`` and ``anthropic`` (issue #1001):
three bands with no advisory behind any of them, guarded together by
``_BOUNDED_EXTRAS`` because they share one failure mode — an extra
declared with a floor and no ceiling, on a load-bearing path, which a
routine ``uv lock --upgrade`` then carries across a major nobody read.

*pyarrow* is the writer and reader of the embeddings cache, the only
file this project keeps on disk in a third-party binary format:
``EmbeddingLinker.save_cache`` writes snappy parquet with ``string``,
``list_(float32())`` and ``timestamp("us", tz="UTC")`` columns,
``load_cache`` reads it back, ``purge_fragment_ids_from_cache``
rewrites it after filtering, and ``delete_embeddings_cache`` reads the
row count from its footer. At ``>=17.0.0`` a relock was free to cross
a parquet major against caches written under the old one.

Bounding pyarrow is also what made 25.0.1 get *read* rather than merely
resolved, and reading it found the reason not to take it yet: 25.0.1
ships no ``py.typed`` marker where 24.0.0 does, which under mypy strict
turns every ``pa.`` and ``pq.`` call in the cache writer into ``Any``.
So the band holds at ``>=24.0.0,<25.0.0`` — the release the lock
resolves and has always run — and #1594 owns the adoption.

``numpy`` (issue #1000): the fourth band, and the only one here whose
reason is the *interpreter matrix* rather than a major. numpy 2.5
declares ``requires-python >=3.12`` while this project declares
``>=3.11``, so an open floor makes ``uv.lock`` carry two resolutions —
2.4.6 for 3.11, 2.5.2 for 3.12+ — and the toolchain cannot straddle
that: numpy 2.5's bundled ``__init__.pyi`` uses PEP 695 ``type``
statements, which mypy rejects as a syntax error under
``python_version = "3.11"``. ``ignore_missing_imports`` cannot suppress
it, because the stub is present and unparseable rather than absent. The
ceiling keeps one numpy across the whole declared support range.

*sentence-transformers* loads the model that produces every vector in
that same cache. It sat at ``>=2.2.0`` while 6.0.0 published on
2026-08-18 — three days before this relock — and a bare ``uv lock
--upgrade`` took it. The band records the release the lock resolves
and holds the major for the vetting pass #1592 tracks.

*anthropic* is the Claude SDK behind ``creek.classify``. It sat at
``>=0.40.0`` while 1.0.0 published on 2026-08-20, one day before the
relock, so the relock would have adopted a first stable major of an
SDK on the classification path with no review at all. It is not only
an SDK major: 1.0.0 makes ``httpx2<3,>=2.0.0`` a *hard* requirement,
so taking it stands httpx2 + httpcore2 + httpx2-jsfetch + truststore
beside the declared ``httpx>=0.27.0``. That is the same swap
``mcp<2.0.0`` (#998) and ``openai<3.0.0`` (#1479) already hold open —
holding anthropic too is what keeps this environment on one HTTP
stack. Vetting is #1593; issue #999 owns the SDK line.

Each band is asserted from both ends, the way openai and setuptools
are: the floor must admit the release ``uv.lock`` resolves and reject
the open floor it replaced, and the ceiling must reject both the
unvetted major and a far-future probe, so a lazy ``!=26.0.0`` posing
as a ceiling fails.

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
import re
import sys
import tomllib
from dataclasses import dataclass
from importlib.metadata import packages_distributions
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

from tests.shell_command_support import PRE_COMMIT_CONFIG, REPO_ROOT, load_yaml

if TYPE_CHECKING:
    from collections.abc import Iterator

    from packaging.specifiers import SpecifierSet

_PACKAGE_ROOT = Path(__file__).resolve().parents[1]
_PYPROJECT = _PACKAGE_ROOT / "pyproject.toml"
_UV_LOCK = _PACKAGE_ROOT / "uv.lock"

#: The sibling subproject's manifests. A DEP-003 floor is a property of
#: the *pair* of manifests, not of either one alone, so the parity guard
#: at the end of this module reads both. Cross-subproject reads have
#: precedent here: tests/test_vulture_gate_wiring.py reaches
#: ``REPO_ROOT / "crawdad" / "scripts"`` the same way, and the repo-root
#: CI job carries no ``paths:`` filter, so this file runs on
#: crawdad-only pull requests too.
_CRAWDAD_ROOT = REPO_ROOT / "crawdad"
_CRAWDAD_PYPROJECT = _CRAWDAD_ROOT / "pyproject.toml"
_CRAWDAD_UV_LOCK = _CRAWDAD_ROOT / "uv.lock"

#: First mcp release containing the fixes for CVE-2026-52869,
#: CVE-2026-52870, and CVE-2026-59950.
_PATCHED_VERSION = Version("1.28.1")

#: First pyasn1 release containing the fixes for CVE-2026-59885 and
#: CVE-2026-59886.
_PYASN1_PATCHED_VERSION = Version("0.6.4")

#: First setuptools release containing the fix for PYSEC-2026-3447
#: (CVE-2026-59890 / GHSA-h35f-9h28-mq5c).
_SETUPTOOLS_PATCHED_VERSION = Version("83.0.0")

#: The release PYSEC-2026-3447 / CVE-2026-59890 is filed against, and
#: the one ``[build-system].requires = ["setuptools>=68.0"]`` still
#: admits today. Named separately from the floor because it is asserted
#: against *every* declared surface, not only the uv constraint the
#: #861 pin reached: a build backend that resolves 81.0.0 runs the
#: vulnerable PackageIndex download path however clean the lock is.
_SETUPTOOLS_LAST_VULNERABLE = Version("81.0.0")

#: The setuptools release ``uv.lock`` resolves and every build in this
#: project has therefore actually run against. The band is asserted
#: from both ends: a ceiling that excludes today's resolution would
#: fail at provisioning time in CI rather than here, so this probe
#: keeps a bound from overshooting what the project runs.
_SETUPTOOLS_LOCKED_VERSION = Version("84.0.0")

#: The next major — which does not exist yet, so no changelog has been
#: read and no build has run against it. setuptools has removed
#: long-deprecated surfaces at majors before (``setup.py test``,
#: ``easy_install``, the bundled distutils shim), so the ceiling holds
#: it for a deliberate adoption exactly as mcp is held at <2.0.0. This
#: is the release the ceiling exists to exclude.
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

#: First torch release containing the fix for PYSEC-2025-194
#: (CVE-2025-3000 / GHSA-rrmf-rvhw-rf47).
_TORCH_PATCHED_VERSION = Version("2.13.0")

#: First cryptography release containing the fix for PYSEC-2026-3552
#: (CVE-2026-69247 / GHSA-g6cj-pr64-35w5); the advisory range opens at
#: 44.0.0, so every release from 44.0.0 up to 49.x is vulnerable.
_CRYPTOGRAPHY_PATCHED_VERSION = Version("50.0.0")

#: First python-multipart release with all FOUR advisories fixed.
#: 0.0.29 carries PYSEC-2026-3036 (CVE-2026-53539), PYSEC-2026-3037
#: (CVE-2026-53538), PYSEC-2026-3041 (CVE-2026-53537) and
#: PYSEC-2026-3040 (CVE-2026-53540). 0.0.30 clears three of the four,
#: leaving PYSEC-2026-3040 open until 0.0.31 — so the floor is 0.0.31,
#: the first fully patched release. The count matters: the four-advisory
#: record was verified against OSV while closing #1328, and the
#: three-advisory framing carried by the older crawdad comment omits
#: PYSEC-2026-3041.
_PYTHON_MULTIPART_PATCHED_VERSION = Version("0.0.31")

#: The anyio floor. No CVE here — the floor simply records the version
#: the lock resolves, the same rule the neighbouring uvicorn
#: declaration follows. Inventing a lower bound would claim
#: compatibility with releases this project has never run. Raised from
#: 4.13.0, the resolution when anyio was first declared (#1123), to the
#: 2026-08-21 transitive relock's resolution (#1257).
_ANYIO_DECLARED_FLOOR = Version("4.14.2")

#: The anyio floor this one replaced. Probed so the assertion holds
#: from both ends: without it a floor left behind at 4.13.0 would still
#: satisfy every other anyio guard in this file.
_ANYIO_PREVIOUS_FLOOR = Version("4.13.0")

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

#: A far-future major shared by every ``_BoundedExtra`` assertion. It
#: probes for a single-release exclusion (``!=26.0.0``) written where a
#: ceiling belongs: that satisfies an assertion aimed at the next major
#: while re-admitting its first patch and every later major.
_BOUNDED_EXTRA_FUTURE_PROBE = Version("999.0.0")


@dataclass(frozen=True)
class _BoundedExtra:
    """One optional-dependency band held from both ends (issue #1001).

    Attributes:
        extra: Name of the ``[project.optional-dependencies]`` table
            declaring the distribution.
        distribution: Distribution name as declared and as locked.
        locked_floor: The release ``uv.lock`` resolves. The band must
            admit it — a floor records what this project has run.
        pre_bound_floor: The open floor this band replaced. The band
            must reject it, so removing the floor is as red as removing
            the ceiling.
        unvetted_major: The major the ceiling exists to exclude.
        rationale: One clause naming what crossing that major would
            reach, quoted into the failure message.
    """

    extra: str
    distribution: str
    locked_floor: Version
    pre_bound_floor: Version
    unvetted_major: Version
    rationale: str


#: Every extra bounded by issue #1001, and the reason each bound exists.
#: A parametrised table rather than three near-identical test bodies —
#: but emptying it would make the guards vanish behind a green gate, so
#: ``test_every_unbounded_major_the_relock_crossed_is_bounded`` asserts
#: the membership rather than trusting the parametrisation.
_BOUNDED_EXTRAS: tuple[_BoundedExtra, ...] = (
    _BoundedExtra(
        extra="embeddings",
        distribution="pyarrow",
        locked_floor=Version("24.0.0"),
        pre_bound_floor=Version("17.0.0"),
        unvetted_major=Version("25.0.0"),
        rationale=(
            "pyarrow reads and writes the embeddings cache, the only "
            "on-disk file this project keeps in a third-party binary "
            "format, so a parquet major crosses persisted data — and "
            "25.0.1 also ships no py.typed marker, which would make "
            "that writer untyped under mypy strict (#1594)"
        ),
    ),
    _BoundedExtra(
        extra="embeddings",
        distribution="numpy",
        locked_floor=Version("2.4.6"),
        pre_bound_floor=Version("2.4.4"),
        unvetted_major=Version("2.5.0"),
        rationale=(
            "numpy 2.5 requires Python >=3.12 while this project "
            "declares >=3.11, so an open floor splits the lock into two "
            "resolutions and mypy — targeting 3.11 — cannot parse 2.5's "
            "PEP 695 stubs on a 3.12 interpreter"
        ),
    ),
    _BoundedExtra(
        extra="embeddings",
        distribution="sentence-transformers",
        locked_floor=Version("5.7.0"),
        pre_bound_floor=Version("2.2.0"),
        unvetted_major=Version("6.0.0"),
        rationale=(
            "sentence-transformers loads the model that produces every "
            "vector in that cache; 6.0.0 published 2026-08-18 and its "
            "vetting against the embedding engine is #1592"
        ),
    ),
    _BoundedExtra(
        extra="anthropic",
        distribution="anthropic",
        locked_floor=Version("0.125.0"),
        pre_bound_floor=Version("0.40.0"),
        unvetted_major=Version("1.0.0"),
        rationale=(
            "anthropic is the Claude SDK on the creek.classify path; "
            "1.0.0 published 2026-08-20 and forces httpx2 as a hard "
            "requirement, so its adoption is one decision with the mcp "
            "#998 and openai #1479 ceilings — tracked in #1593"
        ),
    ),
)


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


def _extra_specifier(extra: str, distribution: str) -> SpecifierSet:
    """Return the specifier one ``[project.optional-dependencies]`` entry declares.

    Generalises ``_openai_specifier`` over the extras bounded by issue
    #1001, which live in three different tables and would otherwise
    need three copies of the same six lines.

    Args:
        extra: Name of the extra table to read.
        distribution: Distribution name to find inside that table.

    Returns:
        The specifier set attached to *distribution* inside *extra*.
        Fails the calling test if either the table or the entry is
        missing — a band that has been deleted rather than widened is
        just as much a regression.
    """
    with _PYPROJECT.open("rb") as handle:
        pyproject = tomllib.load(handle)
    extras: dict[str, list[str]] = pyproject["project"]["optional-dependencies"]
    if extra not in extras:
        pytest.fail(
            f"pyproject.toml declares no [project.optional-dependencies].{extra}"
        )
    for entry in extras[extra]:
        requirement = Requirement(entry)
        if requirement.name == distribution:
            return requirement.specifier
    pytest.fail(
        f"{distribution} is not declared in "
        f"[project.optional-dependencies].{extra} of pyproject.toml"
    )


def _locked_distribution_version(distribution: str) -> Version:
    """Return the version ``uv.lock`` resolves for *distribution*.

    Args:
        distribution: Distribution name as it appears in ``uv.lock``.

    Returns:
        The resolved version. Fails the calling test when the lock has
        no entry, which means the extra stopped resolving at all.
    """
    with _UV_LOCK.open("rb") as handle:
        lock = tomllib.load(handle)
    packages: list[dict[str, object]] = lock["package"]
    for package in packages:
        if package["name"] == distribution:
            return Version(str(package["version"]))
    pytest.fail(f"{distribution} has no [[package]] entry in uv.lock")


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
        Fails the calling test with ``python-multipart has no
        [[package]] entry in uv.lock`` if the lock has no entry, which
        would mean the mcp edge stopped resolving it at all.
    """
    return _locked_distribution_version("python-multipart")


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


def _is_dependency_declaring(path: str) -> bool:
    """Return whether a dotted TOML path names a dependency list.

    Args:
        path: Dotted path of a value in the parsed document, such as
            ``tool.uv.constraint-dependencies``.

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

    setuptools is declared on two independent surfaces —
    ``[tool.uv].constraint-dependencies``, which governs the resolution
    graph behind ``uv.lock``, and ``[build-system].requires``, which
    governs the isolated environment the build backend runs in — and no
    test in this file read the second one until issue #1258. That
    blindness is how ``setuptools>=68.0`` survived the #861 pin while
    still admitting 81.0.0. Discovering the declarations beats naming
    them: a surface added later is guarded the day it appears.

    The walk is deliberately *restricted* to dependency-declaring table
    paths instead of reading every list in the document. An
    unrestricted walk parses 105 strings across 24 table paths in this
    pyproject, among them ``tool.mypy.overrides.module``. Those are
    module patterns, not requirements: ``setuptools.*`` raises
    ``InvalidRequirement`` and is skipped harmlessly, but the bare form
    ``module = ["setuptools"]`` parses cleanly into a requirement with
    an *empty* specifier, and an empty ``SpecifierSet`` admits 81.0.0.
    A future mypy override written that way would then fail the parity
    guard with a security verdict about a line that has nothing to do
    with dependency resolution — a false alarm that teaches the next
    reader to distrust the guard. Restricted, the walk finds exactly
    ``build-system.requires`` and ``tool.uv.constraint-dependencies``.

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


@pytest.mark.parametrize("band", _BOUNDED_EXTRAS, ids=lambda b: b.distribution)
def test_bounded_extra_ceiling_rejects_the_unvetted_major(band: _BoundedExtra) -> None:
    """The band excludes the major nobody has read, and every later one.

    The second assertion is the one that catches a half-measure: an
    exclusion of the single release (``!=26.0.0``) reads like a ceiling
    and is not one — it re-admits the first patch of that same major.

    Args:
        band: The bounded extra under test.
    """
    specifier = _extra_specifier(band.extra, band.distribution)
    assert str(band.unvetted_major) not in specifier, (
        f"{band.distribution} specifier {specifier!r} admits "
        f"{band.unvetted_major}: {band.rationale}. A relock must not "
        "cross that major unreviewed — write a ceiling below it (#1001)"
    )
    assert str(_BOUNDED_EXTRA_FUTURE_PROBE) not in specifier, (
        f"{band.distribution} specifier {specifier!r} admits "
        f"{_BOUNDED_EXTRA_FUTURE_PROBE}; excluding the single release "
        f"`!={band.unvetted_major}` is not a ceiling — it re-admits the "
        "next patch and every later major. Write a real upper bound (#1001)"
    )


@pytest.mark.parametrize("band", _BOUNDED_EXTRAS, ids=lambda b: b.distribution)
def test_bounded_extra_floor_accepts_the_locked_release(band: _BoundedExtra) -> None:
    """The band admits today's resolution and rejects the open floor.

    Both ends matter. A ceiling alone satisfies every other assertion
    here while leaving a floor years below anything this project has
    run; a floor that overshoots the resolution fails at provisioning
    time in CI rather than here.

    Args:
        band: The bounded extra under test.
    """
    specifier = _extra_specifier(band.extra, band.distribution)
    assert str(band.locked_floor) in specifier, (
        f"{band.distribution} specifier {specifier!r} rejects "
        f"{band.locked_floor}, the version uv.lock resolves; a floor "
        "records what this project has actually run, never a version it "
        "has not (#1001)"
    )
    assert str(band.pre_bound_floor) not in specifier, (
        f"{band.distribution} specifier {specifier!r} still admits "
        f"{band.pre_bound_floor}, the open floor this band replaced; the "
        "ceiling is only half the bound (#1001)"
    )


@pytest.mark.parametrize("band", _BOUNDED_EXTRAS, ids=lambda b: b.distribution)
def test_locked_bounded_extra_satisfies_the_declared_band(band: _BoundedExtra) -> None:
    """``uv.lock`` resolves the distribution inside its declared band.

    The lockfile is what CI installs. A bounded manifest with a lock
    outside the bound is exactly the drift these pairs exist to catch:
    the declaration would be right and the installed environment still
    wrong.

    Args:
        band: The bounded extra under test.
    """
    locked = _locked_distribution_version(band.distribution)
    specifier = _extra_specifier(band.extra, band.distribution)
    assert str(locked) in specifier, (
        f"uv.lock pins {band.distribution} {locked}, which the declared "
        f"specifier {specifier!r} rejects; run `uv lock` after changing "
        f"the {band.extra} extra (#1001)"
    )


def test_every_unbounded_major_the_relock_crossed_is_bounded() -> None:
    """``_BOUNDED_EXTRAS`` still names all three distributions #1001 bounded.

    Parametrisation over an empty or trimmed table skips silently and
    reads as a pass, so the membership is asserted rather than assumed.
    The 2026-08-21 transitive relock (#1184) crossed exactly these three
    unbounded majors; dropping one from the table would retire its guard
    without a single red test.
    """
    bounded = {band.distribution for band in _BOUNDED_EXTRAS}
    assert bounded == {"pyarrow", "sentence-transformers", "anthropic", "numpy"}, (
        f"_BOUNDED_EXTRAS covers {sorted(bounded)}; issue #1001 bounded "
        "pyarrow, sentence-transformers and anthropic — the three "
        "unbounded majors a bare `uv lock --upgrade` crossed — plus "
        "numpy, whose open floor split the lock across the supported "
        "interpreter range. Removing an entry retires its guard silently"
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


def test_setuptools_ceiling_rejects_the_unvetted_next_major() -> None:
    """The constraint excludes setuptools 85.0.0 and everything past it.

    No advisory is involved in the ceiling — read it the way the mcp
    ``<2.0.0`` and openai ``<3.0.0`` caps read rather than as a second
    CVE. 84.0.0 is the release ``uv.lock`` resolves and every build in
    this project has actually run against; 85.0.0 does not exist yet,
    so no changelog has been read and no build has been tried, and
    setuptools has removed long-deprecated surfaces at majors before
    (``setup.py test``, ``easy_install``, the bundled distutils shim).
    Holding the band at ``>=83.0.0,<85.0.0`` costs nothing the pin was
    for: setuptools versions under SemVer, so every future 84.x patch —
    the next security fix included — still resolves.

    The 999.0.0 probe is the assertion that catches a half-measure. A
    bare ``!=85.0.0`` satisfies the first assertion, reads like a
    ceiling, and is not one: it re-admits 85.0.1 and every later major.
    """
    specifier = _setuptools_constraint_specifier()
    assert str(_SETUPTOOLS_UNVETTED_MAJOR) not in specifier, (
        f"setuptools constraint {specifier!r} admits "
        f"{_SETUPTOOLS_UNVETTED_MAJOR}, a major nobody has reviewed — it "
        "is unreleased, so no changelog has been read and no build has "
        f"run against it, while {_SETUPTOOLS_LOCKED_VERSION} is what this "
        "project actually builds with; the ceiling must be "
        f"<{_SETUPTOOLS_UNVETTED_MAJOR} (#1258)"
    )
    assert str(_SETUPTOOLS_FUTURE_MAJOR_PROBE) not in specifier, (
        f"setuptools constraint {specifier!r} admits "
        f"{_SETUPTOOLS_FUTURE_MAJOR_PROBE}; an exclusion of the single "
        f"release (`!={_SETUPTOOLS_UNVETTED_MAJOR}`) is not a ceiling — it "
        "re-admits 85.0.1 and every later major. Write a real upper "
        "bound (#1258)"
    )
    assert str(_SETUPTOOLS_LOCKED_VERSION) in specifier, (
        f"setuptools constraint {specifier!r} rejects "
        f"{_SETUPTOOLS_LOCKED_VERSION}, the release uv.lock already "
        "resolves and every build here runs on; a ceiling has to sit "
        "above today's resolution, so `<84.0.0` — one release too low — "
        "is wrong even though it does exclude 85.0.0 (#1258)"
    )


def test_locked_setuptools_at_or_above_patched_release() -> None:
    """``uv.lock`` resolves setuptools to >= 83.0.0.

    The lockfile is what CI installs and what pip-audit inspects; a
    correct constraint floor with a stale lock still ships the
    vulnerable 81.0.0.

    The floor assertion is only half of it. Once the constraint carries
    a ceiling as well (#1258), a lock can drift off the *top* of the
    declared band — a ``uv lock --upgrade`` landing 85.0.0 satisfies
    ``>= 83.0.0`` while sitting outside ``>=83.0.0,<85.0.0`` — so the
    second assertion reads the whole band, the way
    ``test_locked_openai_satisfies_the_declared_specifier`` does.
    """
    locked = _locked_setuptools_version()
    assert locked >= _SETUPTOOLS_PATCHED_VERSION, (
        f"uv.lock pins setuptools {locked}, below the CVE-patched "
        f"{_SETUPTOOLS_PATCHED_VERSION} (PYSEC-2026-3447 / "
        "CVE-2026-59890); pip-audit inspects the lock, so relock after "
        "raising the constraint"
    )
    assert str(locked) in _setuptools_constraint_specifier(), (
        f"uv.lock pins setuptools {locked}, which the declared "
        f"constraint {_setuptools_constraint_specifier()!r} rejects; a "
        "bounded manifest with a lock outside the band is the drift this "
        "pair of guards exists to catch, and the only assertion that "
        "would notice an upgrade past the ceiling — run `uv lock` after "
        "changing the constraint (#1258)"
    )


def test_every_setuptools_declaration_carries_the_vetted_band() -> None:
    """Every declared setuptools surface carries the same vetted band.

    ``[tool.uv].constraint-dependencies`` governs the resolution graph
    that produces ``uv.lock``. ``[build-system].requires`` governs the
    isolated environment the build backend runs in, and the constraint
    does not reach it: constrained to ``<84.0.0`` a build still ran
    setuptools 84.0.0, while bounding ``[build-system].requires``
    produced 83.0.0. They are two surfaces, both able to select a
    setuptools build, and only one of them was guarded — which is how
    ``setuptools>=68.0`` sat in this pyproject admitting 81.0.0, the
    PYSEC-2026-3447 release the whole pin exists to exclude, through
    #861 and every review since (#1258).

    The guard discovers the declarations rather than naming them, so a
    third surface — a uv override, a dependency group, a build
    constraint — is covered the day it is added instead of the day
    someone remembers this file exists. The count assertion is what
    keeps that honest: a walk that returned nothing would otherwise
    make the loop below vacuous and the test green.
    """
    declarations = _setuptools_declarations()
    assert len(declarations) >= 2, (
        f"the pyproject walk found {len(declarations)} setuptools "
        f"declaration(s) ({declarations!r}); this project declares "
        "setuptools twice — [build-system].requires and "
        "[tool.uv].constraint-dependencies — so a shorter result means "
        "the walk is broken, and a guard iterating an empty list passes "
        "while checking nothing"
    )
    paths = {path for path, _ in declarations}
    assert {"build-system.requires", "tool.uv.constraint-dependencies"} <= paths, (
        f"the setuptools walk reached {sorted(paths)}, missing one of the "
        "two surfaces that select a setuptools build: "
        "build-system.requires (the isolated build backend) and "
        "tool.uv.constraint-dependencies (the resolution graph). Neither "
        "can stand in for the other — a constraint bounded to <84.0.0 "
        "still built with 84.0.0 (#1258)"
    )
    for path, specifier in declarations:
        assert str(_SETUPTOOLS_LAST_VULNERABLE) not in specifier, (
            f"{path} declares setuptools {specifier!r}, which admits "
            f"{_SETUPTOOLS_LAST_VULNERABLE} — the release carrying "
            "PYSEC-2026-3447 / CVE-2026-59890, path traversal in the "
            "PackageIndex download path. Every surface that can select a "
            f"setuptools build must floor at {_SETUPTOOLS_PATCHED_VERSION}, "
            "not just the uv constraint (#1258)"
        )
        assert str(_SETUPTOOLS_UNVETTED_MAJOR) not in specifier, (
            f"{path} declares setuptools {specifier!r}, which admits "
            f"{_SETUPTOOLS_UNVETTED_MAJOR}; that major is unreleased, so "
            "no changelog has been read and no build has run against it. "
            f"Carry the same <{_SETUPTOOLS_UNVETTED_MAJOR} ceiling here as "
            "on every other setuptools surface (#1258)"
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
            "the PYSEC-2026-3447 fix; the floor is 83.0.0 on every "
            "surface and no ceiling may swallow it (#1258)"
        )
        assert str(_SETUPTOOLS_LOCKED_VERSION) in specifier, (
            f"{path} declares setuptools {specifier!r}, which rejects "
            f"{_SETUPTOOLS_LOCKED_VERSION}, the release uv.lock resolves "
            "and this project builds with; a band that excludes today's "
            "resolution fails at provisioning time instead of here, and a "
            "bound must record what the project has run (#1258)"
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


def test_python_multipart_constraint_rejects_vulnerable_releases() -> None:
    """The uv constraint excludes 0.0.29 and the partial-fix 0.0.30.

    python-multipart 0.0.29 carries four advisories: PYSEC-2026-3036,
    PYSEC-2026-3037, PYSEC-2026-3041 and PYSEC-2026-3040. 0.0.30 clears
    the first three, so stopping the floor at ``>=0.0.30`` would still
    ship PYSEC-2026-3040 (CVE-2026-53540) on a parser this project
    *serves* — the streamable-http transport and the
    ``creek_mcp.httpapi`` Starlette ``/v1`` app both feed it. The floor
    has to be ``>=0.0.31``.
    """
    specifier = _python_multipart_constraint_specifier()
    assert "0.0.29" not in specifier, (
        f"python-multipart constraint {specifier!r} admits 0.0.29, the "
        "release carrying PYSEC-2026-3036 / PYSEC-2026-3037 / "
        "PYSEC-2026-3041 / PYSEC-2026-3040; the floor must be >=0.0.31"
    )
    assert "0.0.30" not in specifier, (
        f"python-multipart constraint {specifier!r} admits 0.0.30, which "
        "still carries PYSEC-2026-3040 (CVE-2026-53540); the floor must be "
        ">=0.0.31, not >=0.0.30"
    )


def test_python_multipart_constraint_accepts_patched_release() -> None:
    """The uv constraint accepts 0.0.31, the first fully patched release."""
    specifier = _python_multipart_constraint_specifier()
    assert "0.0.31" in specifier, (
        f"python-multipart constraint {specifier!r} rejects 0.0.31; the "
        "patched release itself must satisfy the constraint"
    )


def test_locked_python_multipart_at_or_above_patched_release() -> None:
    """``uv.lock`` resolves python-multipart to >= 0.0.31.

    The lockfile is what CI installs and what pip-audit inspects, so a
    correct constraint floor over a stale lock still ships the
    vulnerable build. This one is green before the constraint exists —
    mcp already pulls 0.0.32 — which makes it a regression guard rather
    than red-first evidence: it is what stops a future relock walking
    back down.
    """
    locked = _locked_python_multipart_version()
    assert locked >= _PYTHON_MULTIPART_PATCHED_VERSION, (
        f"uv.lock pins python-multipart {locked}, below the patched "
        f"{_PYTHON_MULTIPART_PATCHED_VERSION} (PYSEC-2026-3036 / "
        "PYSEC-2026-3037 / PYSEC-2026-3041 / PYSEC-2026-3040); pip-audit "
        "inspects the lock, so relock after adding the constraint"
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
    assert str(_ANYIO_PREVIOUS_FLOOR) not in specifier, (
        f"anyio specifier {specifier!r} still admits "
        f"{_ANYIO_PREVIOUS_FLOOR}, the floor it replaced; the floor must "
        f"be >={_ANYIO_DECLARED_FLOOR}, the resolution the project "
        "actually runs (#1257)"
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


@dataclass(frozen=True)
class _LockstepTool:
    """One gate tool whose version must agree across all three install paths.

    Attributes:
        distribution: Distribution name, as declared and as locked.
        pre_commit_repo: ``repo:`` URL of the hook that runs the tool.
        superseded_version: The version the current band replaced. Probed
            to prove the *floor* moved and not only the ceiling: a
            ceiling-only widening leaves the band spanning two minor
            lines, so the three paths can silently resolve differently
            again.
        rationale: Why this tool is version-locked, quoted into failures.
    """

    distribution: str
    pre_commit_repo: str
    superseded_version: Version
    rationale: str


#: Every tool whose version is asserted across pyproject.toml, uv.lock and
#: .pre-commit-config.yaml at once (issue #1440). Only tools whose band is
#: meant to admit a *single* minor line belong here: ``bandit`` is pinned
#: three ways too, but its ``<2`` band deliberately admits the whole 1.x
#: series, so a one-minor-line assertion would be wrong for it.
#: Emptying this table would make the guard vanish behind a green gate, so
#: ``test_every_lockstep_tool_is_guarded`` asserts the membership rather
#: than trusting the parametrisation.
_LOCKSTEP_TOOLS: tuple[_LockstepTool, ...] = (
    _LockstepTool(
        distribution="ruff",
        pre_commit_repo="https://github.com/astral-sh/ruff-pre-commit",
        superseded_version=Version("0.15.13"),
        rationale=(
            "ruff is both the linter and the formatter for this "
            "repository; two minor lines disagree about formatting, so a "
            "pre-commit run and ./scripts/check-all.sh would rewrite each "
            "other's output forever"
        ),
    ),
    _LockstepTool(
        distribution="mypy",
        pre_commit_repo="https://github.com/pre-commit/mirrors-mypy",
        superseded_version=Version("2.1.0"),
        rationale=(
            "mypy runs under --strict here, and inference sharpens "
            "between minor releases; a commit hook on an older mypy "
            "passes code that ./scripts/typecheck.sh and CI reject"
        ),
    ),
)


def _pre_commit_rev(repo: str) -> Version:
    """Return the version a ``.pre-commit-config.yaml`` hook repo is pinned to.

    Args:
        repo: The ``repo:`` URL to find.

    Returns:
        The parsed ``rev``, with any leading ``v`` stripped. Parsing the
        YAML (rather than scanning text) is what keeps the explanatory
        comment above each ``rev`` from satisfying the assertion.
    """
    config = load_yaml(PRE_COMMIT_CONFIG)
    for entry in config["repos"]:
        if entry.get("repo") == repo:
            return Version(str(entry["rev"]).lstrip("v"))
    pre_commit_name = PRE_COMMIT_CONFIG.name
    pytest.fail(f"{pre_commit_name} declares no hook repo {repo!r}")


def _declared_floor(specifier: SpecifierSet, distribution: str) -> Version:
    """Return the single ``>=`` floor of a declared specifier set.

    Args:
        specifier: The specifier set declared in ``pyproject.toml``.
        distribution: Distribution the specifier belongs to, for messages.

    Returns:
        The floor version. Fails the calling test when the declaration
        carries no floor or more than one, either of which makes
        "the declared version" ambiguous.
    """
    floors = [Version(spec.version) for spec in specifier if spec.operator == ">="]
    if len(floors) != 1:
        pytest.fail(
            f"{distribution} declares {specifier!r}, which has "
            f"{len(floors)} `>=` floors; exactly one is required for the "
            "three install paths to name the same version"
        )
    return floors[0]


@pytest.mark.parametrize("tool", _LOCKSTEP_TOOLS, ids=lambda t: t.distribution)
def test_pin_agrees_across_pyproject_lock_and_precommit(tool: _LockstepTool) -> None:
    """A gate tool resolves to one version on all three install paths.

    ``uv sync``, ``pip install -e '.[dev]'`` and ``pre-commit`` each
    provision the tool independently. Nothing but this assertion keeps
    them together: bumping the Dependabot-facing specifier without the
    hook ``rev`` leaves the commit gate and CI running different builds
    of the same tool, which is how a hook passes code CI then rejects.
    """
    declared = _declared_floor(
        _extra_specifier("dev", tool.distribution), tool.distribution
    )
    locked = _locked_distribution_version(tool.distribution)
    hooked = _pre_commit_rev(tool.pre_commit_repo)
    assert declared == locked == hooked, (
        f"{tool.distribution} disagrees across the three install paths: "
        f"pyproject.toml declares >={declared}, uv.lock resolves {locked}, "
        f"and .pre-commit-config.yaml pins rev v{hooked}. {tool.rationale}. "
        f"Move all three together, then re-run `uv lock`"
    )


@pytest.mark.parametrize("tool", _LOCKSTEP_TOOLS, ids=lambda t: t.distribution)
def test_lockstep_band_admits_one_minor_line(tool: _LockstepTool) -> None:
    """A gate tool's band spans exactly the minor line it is pinned to.

    Adopting a Dependabot ceiling widening on its own -- taking
    ``<0.17`` while the floor stays at ``0.15.13`` -- reads like a bump
    and is not one: the band then admits two minor lines, so the three
    install paths are free to resolve differently again the moment one
    of them refreshes. The superseded version must be excluded from
    below and the next minor from above.
    """
    specifier = _extra_specifier("dev", tool.distribution)
    floor = _declared_floor(specifier, tool.distribution)
    next_minor = Version(f"{floor.major}.{floor.minor + 1}.0")
    assert str(floor) in specifier, (
        f"{tool.distribution} declares {specifier!r}, which rejects its "
        f"own floor {floor}"
    )
    assert str(tool.superseded_version) not in specifier, (
        f"{tool.distribution} declares {specifier!r}, which still admits "
        f"the superseded {tool.superseded_version}; the floor must move "
        f"with the ceiling, or the band spans two minor lines"
    )
    assert str(next_minor) not in specifier, (
        f"{tool.distribution} declares {specifier!r}, which admits "
        f"{next_minor}; a gate tool must not cross a minor line without "
        f"the hook rev and the lock moving with it. {tool.rationale}"
    )


def test_every_lockstep_tool_is_guarded() -> None:
    """Both three-way-pinned gate tools are present in the table.

    Deleting a row would empty its parametrisation, and pytest reports
    zero cases as a pass -- so the guard would disappear behind a green
    gate rather than fail. Asserting the membership is what makes that
    impossible.
    """
    guarded = {tool.distribution for tool in _LOCKSTEP_TOOLS}
    assert guarded == {"ruff", "mypy"}, (
        f"_LOCKSTEP_TOOLS guards {sorted(guarded)!r}; ruff and mypy are "
        "both pinned in pyproject.toml, uv.lock and "
        ".pre-commit-config.yaml, and both must stay guarded"
    )


#: An advisory identifier as manifest comments write them. Nothing in a
#: requirement string separates a security floor from an ordinary
#: compatibility floor, so the comment justifying the entry is what
#: classifies it. ``anyio>=4.14.2`` and ``uvicorn>=0.52.4`` record the
#: resolution this project runs and name no advisory; they are not
#: parity obligations and this pattern deliberately misses them.
_ADVISORY_ID = re.compile(r"\b(?:CVE|PYSEC|GHSA)-[A-Za-z0-9]+-[A-Za-z0-9-]+\b")

#: A ``"<requirement>",`` element of a TOML dependency array. The
#: optional trailing ``# ...`` matters: without it an entry written with
#: a same-line comment would not match, and an unmatched entry is a
#: floor this guard cannot see — a FALSE NEGATIVE, the one direction a
#: security guard must not fail in. The same allowance is made on the
#: table and array-opening patterns below, and
#: ``test_the_manifest_scanner_sees_every_declared_requirement``
#: cross-checks the whole scan against ``tomllib`` so a formatting the
#: patterns still miss fails a test instead of silently shrinking the
#: population.
_MANIFEST_ENTRY = re.compile(r'^\s*"(?P<requirement>[^"]+)",?\s*(?:#.*)?$')

#: A ``[table.header]`` line. The optional doubled brackets match an
#: array-of-tables header: both manifests carry several
#: ``[[tool.mypy.overrides]]`` blocks, and a pattern that did not
#: recognise them would leave the preceding table in scope across the
#: whole run, attributing their inline ``module = [...]`` arrays to
#: whatever table came last.
_MANIFEST_TABLE = re.compile(r"^\s*\[\[?(?P<table>[^\[\]]+)\]\]?\s*(?:#.*)?$")

#: A ``key = [`` line opening a multi-line array.
_MANIFEST_ARRAY = re.compile(r"^\s*(?P<key>[A-Za-z0-9_.-]+)\s*=\s*\[\s*(?:#.*)?$")

#: A whole array on one line — ``openai = ["openai>=2.41.0,<3.0.0"]``.
#: creek-tools writes five of its extras this way, and the first
#: version of this scanner understood only the multi-line form and so
#: could not see any of them. The cross-check against ``tomllib`` is
#: what caught that; the lesson is in that test's docstring.
_MANIFEST_INLINE_ARRAY = re.compile(
    r"^\s*(?P<key>[A-Za-z0-9_.-]+)\s*=\s*\[(?P<body>.*?)\]\s*,?\s*(?:#.*)?$"
)

#: A double-quoted string, for pulling requirements out of an inline array.
_QUOTED = re.compile(r'"([^"]+)"')

#: The table this repo reserves for DEP-003 floors on transitive-only
#: packages. Membership alone marks an entry as a security floor, which
#: is what covers creek-tools' comment-less ``pyjwt>=2.13.0``.
_CONSTRAINT_ARRAY = "tool.uv.constraint-dependencies"

#: Arrays a floor may be declared in, by exact name and by prefix. The
#: extras and dependency-group prefixes are load-bearing: crawdad
#: declares ``setuptools`` and ``wheel`` in its ``dev`` extra rather
#: than in a constraint table, and a union that skipped them would
#: manufacture two asymmetries for packages crawdad already floors at
#: an equal and a higher bound respectively (#1328).
_FLOOR_ARRAYS = ("project.dependencies", _CONSTRAINT_ARRAY)
_FLOOR_ARRAY_PREFIXES = ("project.optional-dependencies.", "dependency-groups.")


@dataclass(frozen=True)
class _Manifest:
    """One subproject's pair of dependency manifests.

    Attributes:
        project: Directory name, as failure messages spell it.
        pyproject: That subproject's ``pyproject.toml``.
        lockfile: That subproject's ``uv.lock``.
    """

    project: str
    pyproject: Path
    lockfile: Path


@dataclass(frozen=True)
class _AdvisoryFloor:
    """One security floor a manifest declares against a published advisory.

    Attributes:
        distribution: Canonicalised distribution name.
        requirement: The requirement string exactly as the manifest
            spells it, so a failure message quotes what a reader will
            find in the file.
        advisories: Advisory identifiers named by the comment block
            justifying the floor. Empty for a constraint-table entry
            carrying no comment.
    """

    distribution: str
    requirement: str
    advisories: tuple[str, ...]


#: The two manifests DEP-003 holds in parity. No third exists:
#: ``requirements.txt`` and ``requirements-dev.txt`` are pointer files
#: carrying zero pins and naming pyproject.toml as the source of truth.
_MANIFESTS = (
    _Manifest("creek-tools", _PYPROJECT, _UV_LOCK),
    _Manifest("crawdad", _CRAWDAD_PYPROJECT, _CRAWDAD_UV_LOCK),
)

#: Advisory floors deliberately declared on one side only, each with the
#: reason. Every row is a suppression, so every row has to keep earning
#: its place: ``test_every_documented_asymmetry_is_still_asymmetric``
#: reds on a row whose package the sibling has since floored. That guard
#: is the whole point. The scan that filed #1328 asserted crawdad
#: omitted ``pip`` months after crawdad floored it at
#: crawdad/pyproject.toml (#1527) — the failure mode a hand-maintained
#: prose table has and an executable one does not.
#:
#: **It is empty, and empty is the goal.** #1328 planned to exempt
#: ``("crawdad", "urllib3")`` on the reading that crawdad reaches
#: urllib3 only through pip-audit. That reading is false: crawdad
#: declares ``google-genai`` in ``[project].dependencies``, and
#: google-genai and google-auth both pull ``requests``, which pulls
#: urllib3 — a runtime path, not a dev-only one. The package was
#: floored instead of exempted. An exemption whose reason does not
#: survive a read of the lock is the defect this module exists to
#: catch, so prefer mirroring the floor every time.
_DOCUMENTED_ASYMMETRIES: dict[tuple[str, str], str] = {}


def _cited_advisories(block: list[str]) -> tuple[str, ...]:
    """Return the advisory ids named by a comment block, de-duplicated.

    Args:
        block: The comment lines immediately above a manifest entry.

    Returns:
        Identifiers in first-seen order.
    """
    return tuple(dict.fromkeys(_ADVISORY_ID.findall(" ".join(block))))


def _table_transition(line: str, table: str, array: str) -> tuple[str, str]:
    """Apply a structural line's effect on the current table and array.

    Args:
        line: A manifest line that is neither a comment nor an entry.
        table: The table header currently in scope.
        array: The array currently being read, or ``""``.

    Returns:
        The updated ``(table, array)`` pair.
    """
    header = _MANIFEST_TABLE.match(line)
    if header is not None:
        return header.group("table"), ""
    opening = _MANIFEST_ARRAY.match(line)
    if opening is not None:
        return table, f"{table}.{opening.group('key')}"
    if line.strip().startswith("]"):
        return table, ""
    return table, array


def _manifest_entries(pyproject: Path) -> Iterator[tuple[str, str, tuple[str, ...]]]:
    """Yield each dependency-array entry with the comment block above it.

    The manifest is read as text rather than through ``tomllib``
    because the justification for a floor lives in its comment and a
    TOML parser discards comments.

    Args:
        pyproject: Manifest to scan.

    Yields:
        ``(array, requirement, advisories)`` per entry, where *array* is
        the fully qualified array name (``project.dependencies``,
        ``tool.uv.constraint-dependencies``, ...) and *advisories* holds
        the identifiers named by the comment block immediately above the
        entry. The block resets on any blank or non-comment line, so an
        entry inherits only its own justification and never its
        neighbour's.
    """
    table = ""
    array = ""
    block: list[str] = []
    for line in pyproject.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            block.append(stripped)
            continue
        entry = _MANIFEST_ENTRY.match(line)
        if entry is not None and array:
            yield array, entry.group("requirement"), _cited_advisories(block)
            block = []
            continue
        inline = _MANIFEST_INLINE_ARRAY.match(line)
        if inline is not None:
            advisories = _cited_advisories(block)
            inline_array = f"{table}.{inline.group('key')}"
            for requirement in _QUOTED.findall(inline.group("body")):
                yield inline_array, requirement, advisories
            block = []
            continue
        table, array = _table_transition(line, table, array)
        block = []


def _advisory_floors(pyproject: Path) -> dict[str, _AdvisoryFloor]:
    """Return the security floors *pyproject* declares, by distribution.

    A floor answers an advisory when either it lives in
    ``[tool.uv].constraint-dependencies`` — the table this repo reserves
    for DEP-003 floors — or its justifying comment names a CVE, PYSEC,
    or GHSA identifier.

    Args:
        pyproject: Manifest to read.

    Returns:
        A mapping from canonicalised distribution name to its floor.
    """
    floors: dict[str, _AdvisoryFloor] = {}
    for array, requirement, advisories in _manifest_entries(pyproject):
        declared_here = array in _FLOOR_ARRAYS or array.startswith(
            _FLOOR_ARRAY_PREFIXES
        )
        if not declared_here:
            continue
        if array != _CONSTRAINT_ARRAY and not advisories:
            continue
        name = canonicalize_name(Requirement(requirement).name)
        floors[name] = _AdvisoryFloor(name, requirement, advisories)
    return floors


def _declared_floor_names(pyproject: Path) -> set[str]:
    """Return every distribution *pyproject* declares a requirement for.

    Unions ``[project].dependencies``, every
    ``[project.optional-dependencies]`` table, every
    ``[dependency-groups]`` table, and
    ``[tool.uv].constraint-dependencies``. Which table a floor lives in
    is a per-project convention, so parity has to read all of them.

    Args:
        pyproject: Manifest to read.

    Returns:
        Canonicalised distribution names.
    """
    with pyproject.open("rb") as handle:
        data = tomllib.load(handle)
    project = data.get("project", {})
    arrays: list[list[str]] = [project.get("dependencies", [])]
    arrays.extend(project.get("optional-dependencies", {}).values())
    arrays.extend(data.get("dependency-groups", {}).values())
    uv_table = data.get("tool", {}).get("uv", {})
    arrays.append(uv_table.get("constraint-dependencies", []))
    return {
        canonicalize_name(Requirement(entry).name)
        for array in arrays
        for entry in array
    }


def _locked_versions(lockfile: Path) -> dict[str, str]:
    """Return every package *lockfile* resolves, by canonical name.

    Args:
        lockfile: A ``uv.lock`` to read.

    Returns:
        A mapping from canonicalised distribution name to the resolved
        version string.
    """
    with lockfile.open("rb") as handle:
        lock = tomllib.load(handle)
    packages: list[dict[str, object]] = lock["package"]
    return {
        canonicalize_name(str(package["name"])): str(package["version"])
        for package in packages
    }


def _scanned_floor_names(pyproject: Path) -> set[str]:
    """Return the floor-array names the *text scanner* finds.

    The counterpart to :func:`_declared_floor_names`, which reads the
    same arrays through ``tomllib``. Comparing the two is what makes
    the hand-rolled parser auditable.

    Args:
        pyproject: Manifest to scan.

    Returns:
        Canonicalised distribution names.
    """
    names: set[str] = set()
    for array, requirement, _ in _manifest_entries(pyproject):
        if array in _FLOOR_ARRAYS or array.startswith(_FLOOR_ARRAY_PREFIXES):
            names.add(canonicalize_name(Requirement(requirement).name))
    return names


def _advisory_floor_gaps() -> dict[tuple[str, str], str]:
    """Return every advisory floor one manifest declares and its sibling lacks.

    A gap needs all three of: the declaring side floors the package
    against an advisory, the other side declares no requirement for it
    at all, and the other side's own lock nonetheless resolves it. The
    third condition is what keeps ``torch``, ``aiohttp`` and ``pillow``
    out — each is absent from the sibling's graph entirely, so there is
    nothing there to floor.

    Returns:
        A mapping from ``(lacking project, distribution)`` to a one-line
        explanation naming the declaring project's requirement string,
        the advisories it answers, and the version the lacking project
        already resolves.
    """
    first, second = _MANIFESTS
    gaps: dict[tuple[str, str], str] = {}
    for declaring, lacking in ((first, second), (second, first)):
        declared = _declared_floor_names(lacking.pyproject)
        locked = _locked_versions(lacking.lockfile)
        for name, floor in sorted(_advisory_floors(declaring.pyproject).items()):
            if name in declared or name not in locked:
                continue
            cited = f" ({', '.join(floor.advisories)})" if floor.advisories else ""
            gaps[lacking.project, name] = (
                f"{lacking.project} lacks {name} - {declaring.project} "
                f'declares "{floor.requirement}"{cited} and '
                f"{lacking.project}/uv.lock resolves {name} {locked[name]}"
            )
    return gaps


def test_every_advisory_floor_is_mirrored_or_documented() -> None:
    """Neither manifest floors an advisory the other silently ignores.

    creek-tools and crawdad resolve overlapping graphs — most of the
    overlap arrives through the shared ``mcp`` dependency — but each
    carries its own floors, and nothing made the two sets agree. So a
    floor added on one side stayed there: creek-tools *serves* the
    ``python-multipart`` parser, through the streamable-http transport
    and the ``creek_mcp.httpapi`` Starlette app, with no floor at all,
    while crawdad — an MCP client — floored it (#1328).

    This is the guard whose absence is the defect. A prose sweep table
    goes stale the day after it is written; this derives both sides from
    the manifests on every run and names every gap at once.
    """
    gaps = _advisory_floor_gaps()
    undocumented = sorted(set(gaps) - set(_DOCUMENTED_ASYMMETRIES))
    assert not undocumented, (
        "advisory floors declared in one manifest and missing from the "
        "sibling that resolves the package:\n"
        + "\n".join(f"  {gaps[key]}" for key in undocumented)
        + "\nMirror the floor into the sibling manifest, or add a reasoned "
        "_DOCUMENTED_ASYMMETRIES row (#1328)"
    )


def test_every_documented_asymmetry_is_still_asymmetric() -> None:
    """Every exemption still describes a gap that is really there.

    An exemption is a suppression, and a stale suppression is worse
    than none: it hides its package from the parity guard permanently.
    Requiring each row to still reproduce as a gap means a row for a
    package the sibling has since floored fails here rather than
    quietly widening the hole.
    """
    gaps = _advisory_floor_gaps()
    resolved = sorted(set(_DOCUMENTED_ASYMMETRIES) - set(gaps))
    assert not resolved, (
        "_DOCUMENTED_ASYMMETRIES rows that are no longer asymmetric — the "
        "sibling now declares the floor, or no longer resolves the "
        "package, so the exemption suppresses nothing and must be "
        "deleted:\n"
        + "\n".join(
            f"  {project} / {name}: {_DOCUMENTED_ASYMMETRIES[project, name]}"
            for project, name in resolved
        )
    )


def test_no_advisory_floor_is_currently_exempted() -> None:
    """``_DOCUMENTED_ASYMMETRIES`` is empty, and that is the goal state.

    An empty table makes the test above trivially true, and a
    collection-driven test that passes on an empty collection is how a
    guard disappears behind a green gate. Asserting the emptiness keeps
    it a fact rather than an accident: adding the first suppression has
    to edit this test and say why here, which is exactly the review
    conversation a security suppression deserves.

    #1328 closed the one candidate exemption by *flooring* the package
    rather than exempting it — see the note on
    ``_DOCUMENTED_ASYMMETRIES`` for why its proposed reason was false.
    """
    assert not _DOCUMENTED_ASYMMETRIES, (
        f"_DOCUMENTED_ASYMMETRIES now suppresses "
        f"{sorted(_DOCUMENTED_ASYMMETRIES)}; every row is a hole in the "
        "parity guard. Mirror the floor instead if you can; if the "
        "exemption really is right, update this test and record the "
        "reason with it (#1328)"
    )


def test_the_manifest_scanner_sees_every_declared_requirement() -> None:
    """The text scanner and ``tomllib`` agree on every manifest.

    :func:`_manifest_entries` hand-parses TOML because the justification
    for a floor lives in a comment and ``tomllib`` discards comments.
    That trade buys a real risk: a formatting the patterns do not match
    — an inline single-line array, an entry carrying a same-line
    comment, an unusual table header — yields *fewer* entries, and a
    floor the scanner cannot see is a floor the parity guard cannot
    require of the sibling. That is a false negative, the one direction
    a security guard must not fail in, and nothing about it is visible:
    every other test in this module still passes.

    So the parser is held to the real one. ``tomllib`` reads the same
    arrays with no regexes at all, and the two populations must match
    exactly. A regex that stops matching fails here, loudly, naming the
    entries it dropped.
    """
    for manifest in _MANIFESTS:
        scanned = _scanned_floor_names(manifest.pyproject)
        parsed = _declared_floor_names(manifest.pyproject)
        assert scanned == parsed, (
            f"the text scanner and tomllib disagree about "
            f"{manifest.project}/pyproject.toml. Missed by the scanner: "
            f"{sorted(parsed - scanned)}; seen only by the scanner: "
            f"{sorted(scanned - parsed)}. A floor the scanner cannot see "
            "is one the parity guard cannot require of the sibling"
        )
