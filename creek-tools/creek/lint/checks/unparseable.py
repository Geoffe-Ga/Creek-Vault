"""Report corpus files the pipeline silently skips (#926).

Every guarded load in the tree converts an unreadable file into a *skip*.
:func:`creek.vault.reader.iter_vault_fragments` skips it, ``PurgeEngine``
skips it, ``HygieneEngine`` skips it, and :mod:`creek.reflect` skips it —
each at ``DEBUG``, which is off at the default ``WARNING`` level. The net
effect is that a corrupt fragment is *invisible*: it is in no scan, in no
purge, in no reflection, and nothing anywhere tells the operator it exists.

That is quiet data loss from the working set, and it cannot be fixed at the
tool surface. ``entry_ref`` resolution deliberately answers a corrupt
fragment with the ordinary "not found" refusal, indistinguishable from a
fragment that was never there, because a distinct reason would be an
existence oracle (#847). A lint pass is therefore the right — and only —
place to surface it.

**This check reports the exception class name and never ``str(exc)``.**

#926 justifies that by saying ``yaml.MarkedYAMLError.__str__`` embeds the
offending source snippet. Measured against this code path, that is not so:
through ``frontmatter.load`` PyYAML renders position only — *"did not find
expected ',' or ']' in "<unicode string>", line 3, column 5"* — because the
snippet needs a source buffer the loader has already released. Four malformed
shapes (flow sequence, tab indent, bad anchor, bad scalar) were checked and
none reproduced content.

The rule stands anyway, for reasons that do not depend on that claim:

* An exception message is **not a stable contract**. PyYAML may render a
  snippet in a future release, a different loader may already, and nothing
  would fail if it started — the leak would ship silently.
* Even position is more than the operator needs. The file's ``privacy_tier``
  is unknown *because it could not be parsed*, and "unknown" must read as
  "sensitive" here.
* The class name is the whole actionable signal: it says whether to fix YAML,
  fix permissions, or fix an encoding.

Path plus class name is the ceiling.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003  # used at runtime as a parameter type
from pathlib import Path  # noqa: TC003  # plain stdlib import; no lazy benefit

import frontmatter

from creek.lint._result import CheckResult
from creek.vault.reader import CORPUS_SUBDIRS, FRONTMATTER_LOAD_ERRORS


def _unreadable_corpus_files(vault_path: Path) -> list[tuple[Path, str]]:
    """Find every corpus markdown file whose frontmatter will not load.

    Walks the same subtrees the reader walks, in the same order, using the
    same guard tuple — :data:`~creek.vault.reader.CORPUS_SUBDIRS` and
    :data:`~creek.vault.reader.FRONTMATTER_LOAD_ERRORS`. Sharing both is the
    point: a check that guessed at either could report files the pipeline
    reads fine, or stay silent about files it drops.

    The issue asked for ``01-Fragments`` alone. Widened to the full corpus
    because a corrupt note under ``09-Reference`` or ``11-Other-Authors`` is
    exactly as invisible, and narrowing to one subtree would leave the
    operator with a clean report and a broken vault.

    Args:
        vault_path: Vault root to scan.

    Returns:
        ``(path, exception class name)`` per unreadable file, sorted by path
        so two runs over an unchanged vault render identically.
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
                # swallow a genuine programming-error TypeError. The Post
                # itself is not wanted — only whether it parses.
                _ = frontmatter.load(str(md_file))
            except FRONTMATTER_LOAD_ERRORS as exc:
                # Class name only — never str(exc). See the module docstring.
                found.append((md_file, type(exc).__name__))
    return sorted(found, key=lambda pair: str(pair[0]))


def run(vault_path: Path, *, since: datetime | None = None) -> CheckResult:
    """Report corpus files the pipeline cannot read.

    Args:
        vault_path: Vault root to scan.
        since: Accepted for registry uniformity and deliberately unused. An
            unreadable file has no parseable timestamp to filter on — reading
            it is the thing that failed — so a ``--since`` window could only
            hide the finding it exists to surface.

    Returns:
        One finding per unreadable file: its vault-relative path and the
        exception class that stopped it loading.
    """
    del since

    unreadable = _unreadable_corpus_files(vault_path)
    if not unreadable:
        return CheckResult(
            name="unparseable",
            summary="No unreadable corpus files.",
        )

    findings = [
        f"- `{path.relative_to(vault_path)}` — {error} "
        f"(skipped by every scan; repair or remove it)"
        for path, error in unreadable
    ]
    noun = "file" if len(unreadable) == 1 else "files"
    return CheckResult(
        name="unparseable",
        summary=(
            f"{len(unreadable)} unreadable corpus {noun} — invisible to "
            f"ingest, purge, hygiene and reflect."
        ),
        findings=findings,
    )
