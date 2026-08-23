"""Report frontmatter ``id`` values YAML types as something other than ``str`` (#1291).

``id: 12345`` reads to a human as an id and resolves under ``SafeLoader`` to an
``int``. Every reader of the per-directory id index requires
``isinstance(id, str)`` — :func:`creek.vault.writer._declared_id` and, through
it, both :func:`creek.vault.writer._file_declares_id` and
:meth:`creek.vault.writer.VaultWriter._rebuild_index` — so such a file is
**invisible to identity**. It is never indexed, ``find_fragment`` never
resolves it, and the next write for the logical id ``"12345"`` mints a second
file beside it. Nothing tells the operator, because from the pipeline's point
of view the file simply declares no id.

#1291 posed the choice as (a) normalise on read, or (b) report it. This module
is (b), and (a) is **rejected on the record**: ``str(id)`` silently merges two
identities the vault never said were the same, and widening what counts as
"the same fragment" is not a thing to do implicitly. The temptation was real
and immediate — a bounded byte-scan for the ``id`` key reads ``12345`` as
literal text and would have implemented (a) as a side effect of a performance
change (#1543). :func:`creek.vault.writer._typed_scalar` therefore reports an
unquoted scalar only when YAML itself would type it ``str``, and the strict
outcome stays pinned by
``tests/test_vault_writer.py::TestIdIndexVerification::test_non_str_id_in_frontmatter_is_a_mismatch``.

What the operator gets instead is a finding naming the file and the type the
id resolved to, with the one-word remedy: quote it. Deliberately **not** the
id's own text — see :mod:`creek.lint.checks.unparseable`, which makes the same
call about exception messages for the same reason. The type is the whole
actionable signal; the value is vault content this report has no need to
disclose.

A file whose header will not parse at all is *not* reported here. That is
``unparseable``'s finding, and double-counting one broken file across two
checks makes both harder to act on.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003  # used at runtime as a parameter type
from pathlib import Path  # noqa: TC003  # plain stdlib import; no lazy benefit

import frontmatter

from creek.lint._result import CheckResult
from creek.vault.reader import CORPUS_SUBDIRS, FRONTMATTER_LOAD_ERRORS


def _non_string_id_files(vault_path: Path) -> list[tuple[Path, str]]:
    """Find every corpus markdown file whose ``id`` is not a ``str``.

    Walks :data:`~creek.vault.reader.CORPUS_SUBDIRS` — the same subtrees the
    fragment reader walks — because identity is vault-wide: a numeric id under
    ``09-Reference`` is exactly as invisible as one under ``01-Fragments``.

    Args:
        vault_path: Vault root to scan.

    Returns:
        ``(path, resolved type name)`` per offending file, sorted by path so
        two runs over an unchanged vault render identically.
    """
    found: list[tuple[Path, str]] = []
    for subdir in CORPUS_SUBDIRS:
        root = vault_path / subdir
        if not root.is_dir():
            continue
        for md_file in sorted(root.rglob("*.md")):
            try:
                # Assigned, not bare: the house guard shape is one
                # Assign/Return call per bracket, so a widened bracket cannot
                # swallow a genuine programming-error TypeError.
                post = frontmatter.load(str(md_file))
            except FRONTMATTER_LOAD_ERRORS:
                # Owned by the ``unparseable`` check, which reports it once.
                continue
            declared = post.get("id")
            if declared is not None and not isinstance(declared, str):
                found.append((md_file, type(declared).__name__))
    return sorted(found, key=lambda pair: str(pair[0]))


def run(vault_path: Path, *, since: datetime | None = None) -> CheckResult:
    """Report corpus files whose ``id`` YAML does not type as a string.

    Args:
        vault_path: Vault root to scan.
        since: Accepted for registry uniformity and deliberately unused. The
            defect is a property of the file as it stands, not of when it was
            written, and a ``--since`` window could only hide findings that are
            still live.

    Returns:
        One finding per offending file: its vault-relative path and the type
        the id resolved to. Never the id's own value.
    """
    del since

    offenders = _non_string_id_files(vault_path)
    if not offenders:
        return CheckResult(
            name="nonstring-id",
            summary="No non-string frontmatter ids.",
        )

    findings = [
        f"- `{path.relative_to(vault_path)}` — `id` resolved to {type_name}, "
        f"not str (invisible to the id index; quote it)"
        for path, type_name in offenders
    ]
    noun = "file" if len(offenders) == 1 else "files"
    return CheckResult(
        name="nonstring-id",
        summary=(
            f"{len(offenders)} {noun} whose `id` is not a string — invisible "
            f"to duplicate detection, update, tomb and restore."
        ),
        findings=findings,
    )
