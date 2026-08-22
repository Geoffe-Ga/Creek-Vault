"""The accepted check-then-act window must be written down (#1298).

Both symlink guards are two-phase, and neither holds a file descriptor across
the gap:

* the redact **read** path — ``creek/redact/scanner.py::_scannable_candidates``
  materialises a candidate list, then ``scan_batch`` opens each file;
* the redact **write** path —
  ``creek/redact/cli_commands.py::_assert_no_escaping_symlinks`` walks the tree,
  then ``_apply_redactions`` rewrites the files it found.

An attacker who can replace an admitted in-root path with a link out of the
root between those two phases is read, or written, through the swap.

#1298 proposed closing it with ``O_NOFOLLOW`` while requiring
``tests/test_cli_redact.py``'s SEC-003 block to pass unmodified. **Those two
requirements are incompatible**, and the incompatibility is measured rather
than argued — see
:func:`test_o_nofollow_cannot_express_the_shipped_containment_policy` below.
``O_NOFOLLOW`` refuses *every* symlink; SEC-003's policy deliberately ADMITS a
symlink whose target stays inside the root, which is what
``test_redact_apply_allows_internal_symlink`` pins. There is no descriptor-based
fix that keeps the admit-intra-root policy without re-implementing containment
over ``/proc``-style fd paths, which do not exist portably on darwin.

The remaining alternative — re-validating each path immediately before opening
it — narrows the window from seconds to microseconds without closing it, and a
test for it could only exercise a monkeypatched seam rather than a real race.
That is an API-shape assertion dressed as a security test.

So the decision is to accept the risk and RECORD it. The residual sits inside
the trust boundary the threat model already declares: an attacker who can swap
a symlink mid-scan already holds write access to the tree being scanned, and
``docs/security/threat-model.md`` lists multi-tenant safety and network
exposure as explicit non-goals. What was missing is only the written record —
so this file is the gate on the record existing, and the measurement below is
the gate on the reason it gives still being true.
"""

from __future__ import annotations

import errno
import os
from pathlib import Path
from typing import Final

import pytest

THREAT_MODEL: Final[Path] = (
    Path(__file__).resolve().parents[1] / "docs" / "security" / "threat-model.md"
)
"""The document that must carry the accepted-risk entry.

``Path(__file__)`` is ``<repo>/creek-tools/tests/test_threat_model_toctou.py``,
so ``parents[1]`` is the ``creek-tools`` subproject, where ``docs/security/``
lives. Anchored on ``__file__`` rather than on the working directory: the CI
``quality`` job and a local ``pytest`` invocation do not agree on cwd.
"""

_NOT_PROTECTED_HEADING: Final[str] = "## What is NOT protected"
"""The section an accepted residual belongs under.

Named as a heading rather than matched loosely: an entry filed under "What is
protected" would claim the opposite of the truth, and a substring search over
the whole file could not tell the two apart.
"""

_REQUIRED_FACTS: Final[tuple[tuple[str, str], ...]] = (
    (
        "_scannable_candidates",
        "the read path's first phase — the walk that materialises candidates",
    ),
    (
        "scan_batch",
        "the read path's second phase — the reads that happen after the walk",
    ),
    (
        "_assert_no_escaping_symlinks",
        "the write path's first phase — the guard that runs before any write",
    ),
    (
        "_apply_redactions",
        "the write path's second phase — the in-place rewrite",
    ),
    (
        "O_NOFOLLOW",
        "the hardening that was considered and measured to be unavailable",
    ),
    (
        "ELOOP",
        "the measured result of opening an ADMITTED intra-root alias with "
        "O_NOFOLLOW, which is why the hardening is unavailable",
    ),
    (
        "test_redact_apply_allows_internal_symlink",
        "the named test the hardening would break, so the next reviewer does "
        "not have to re-derive the conflict",
    ),
)
"""Facts the entry must state, each with the reason it is not optional.

Every one of these is a concrete, checkable noun — a function name, a flag, an
errno, a test name. Prose about "races" is deliberately not required: a grep
gate over ordinary English is satisfied by rephrasing rather than by recording
anything, which is the failure mode ``tests/test_redaction_docs_drift.py``'s
module docstring already documents for this repository.
"""


def _not_protected_section() -> str:
    """Return the text of the threat model's "What is NOT protected" section.

    Returns:
        Everything between that heading and the next top-level heading.
    """
    text = THREAT_MODEL.read_text(encoding="utf-8")
    assert _NOT_PROTECTED_HEADING in text, (
        f"{THREAT_MODEL} no longer has a {_NOT_PROTECTED_HEADING!r} section, "
        "so this gate cannot tell an accepted residual from a claimed defence."
    )
    after = text.split(_NOT_PROTECTED_HEADING, 1)[1]
    return after.split("\n## ", 1)[0]


def test_the_threat_model_records_the_check_then_act_window() -> None:
    """RED. ``grep -ni 'toctou|race'`` over the whole document returns nothing today.

    The gap between "the guard walked the tree" and "the reader opened the
    file" is a real, accepted residual of the shipped SEC-003 design. An
    accepted risk that is not written down is indistinguishable from an
    overlooked one: the next reviewer re-derives the O_NOFOLLOW idea, spends
    the same afternoon measuring the same ELOOP, and reaches the same answer.
    """
    section = _not_protected_section()
    lowered = section.lower()

    assert "toctou" in lowered or "check-then-act" in lowered, (
        "the threat model's 'What is NOT protected' section says nothing "
        "about the check-then-act window between the containment guard and "
        "the read/write that follows it. Both redaction guards are two-phase "
        "and neither holds a descriptor across the gap."
    )
    for literal, why in _REQUIRED_FACTS:
        assert literal in section, (
            f"the entry does not name {literal!r}, which records {why}. "
            "Without it the record is a vague admission rather than "
            "something the next reviewer can act on."
        )


def test_the_sec_003_entry_cross_references_the_accepted_window() -> None:
    """RED. The residual must be reachable from the guard it belongs to.

    A reader arrives at this document through ``SEC-003`` — that is the ID the
    code annotates and the commit messages tag. An accepted residual filed 100
    lines away under a different heading, with nothing pointing at it from the
    entry describing the guard, is a record only someone who already knew it
    existed can find.
    """
    text = THREAT_MODEL.read_text(encoding="utf-8")
    assert "**SEC-003**" in text, (
        "the SEC-003 cross-reference entry is gone, so this gate no longer "
        "pins anything."
    )
    sec_003 = text.split("**SEC-003**", 1)[1].split("\n- **", 1)[0]
    lowered = sec_003.lower()
    assert "toctou" in lowered or "check-then-act" in lowered, (
        "the SEC-003 entry describes the symlink refusal across all three "
        "surfaces but never mentions that each of them is two-phase, so a "
        "reader following the ID never learns the window exists.\n\n"
        f"{sec_003}"
    )


def test_o_nofollow_cannot_express_the_shipped_containment_policy(
    tmp_path: Path,
) -> None:
    """PASSES NOW, and is the non-vacuity anchor for the document above.

    The threat-model entry claims a *measurement*, not a preference, and a
    documented measurement nobody re-runs is a claim that quietly rots. This
    test is that re-run.

    SEC-003 deliberately ADMITS a symlink whose target stays inside the root:
    ``tests/test_cli_redact.py::test_redact_apply_allows_internal_symlink``
    builds ``src/alias.md -> src/real.md`` and asserts ``exit_code == 0``, and
    ``_scannable_candidates`` returns both files with ``escaped == 0``.
    ``O_NOFOLLOW`` cannot distinguish that link from one pointing out of the
    root — it refuses both — so adopting it would fail the very test #1298
    requires to keep passing.

    If a future platform or API ever *could* express "follow only links whose
    target stays under this root", this test fails and the accepted risk is
    due for re-litigation. That is exactly the signal the record should carry.

    Args:
        tmp_path: Pytest-provided temporary directory.
    """
    from creek.redact.scanner import _scannable_candidates

    src = tmp_path / "src"
    src.mkdir()
    real = src / "real.md"
    real.write_text("Contact: alice@example.com\nSSN: 123-45-6789\n", encoding="utf-8")
    alias = src / "alias.md"
    alias.symlink_to(real)

    candidates, escaped = _scannable_candidates(src)

    assert sorted(path.name for path in candidates) == ["alias.md", "real.md"], (
        "the shipped scanner no longer admits an intra-root alias, so "
        "SEC-003's admit-intra-root policy has changed and the whole "
        f"O_NOFOLLOW argument needs revisiting.\n\n{candidates}"
    )
    assert escaped == 0, (
        f"an intra-root alias was counted as an escape.\n\nescaped={escaped}"
    )

    with pytest.raises(OSError) as excinfo:
        os.close(os.open(alias, os.O_RDONLY | os.O_NOFOLLOW))

    assert excinfo.value.errno == errno.ELOOP, (
        "os.open(..., O_NOFOLLOW) no longer refuses a symlink the containment "
        "policy admits. If the flag can now express 'follow only in-root "
        "links', the accepted TOCTOU risk recorded in the threat model should "
        f"be re-opened.\n\nerrno={excinfo.value.errno}"
    )
