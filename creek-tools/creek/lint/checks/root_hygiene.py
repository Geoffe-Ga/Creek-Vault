"""Deterministic check: nothing should be littering the vault root (#883).

The vault root is the folder an operator opens in Obsidian every day, and
``creek init`` puts exactly fifteen things there. Anything else arrived by
accident, and #883 is the report of two such accidents: a
``review-queue-<timestamp>.md`` (whose writer, ``creek.classify.review``, is
fixed in the same change) and a zero-byte ``frag-f8cd9208e113.md`` deep in
``01-Fragments``.

**Why a standing check rather than only a targeted fix.** The zero-byte
fragment has *no writer in today's tree*. It is not reproducible from current
code, so a targeted fix could not be verified — whatever wrote it either no
longer exists or was never in ``creek/``. The next one will be found the same
way this one was: by an operator noticing, months later. A check turns that
into a line in a report.

The sweep behind that claim, and worth recording because the issue is wrong
about its own evidence: the grep #883 proposes, ``vault_path / f"``, returns
**zero hits** across ``creek/`` and ``creek_mcp/`` — the f-string is bound to
a variable first, so the issue's own grep would have missed the issue's own
defect. The sweep that works — every ``vault_path /``,
``vault_path.joinpath`` and ``self.vault_path /`` join site — finds
``creek/classify/review.py`` as the only root-level bare-filename *write*.
The single root-level *read*, ``skill_size_budget.py`` opening ``AGENTS.md``,
is correct, and every config-driven relpath is subdirectory-scoped.

**Reports, never deletes.** A check that tidied the root would be a check that
can delete an operator's own note filed there on purpose. Emptiness is
measured with ``stat().st_size`` rather than by reading, which is both cheaper
on a 35k-file vault and one fewer way for body text to reach a report
``creek state`` echoes verbatim.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003  # used at runtime as a parameter type
from pathlib import Path  # noqa: TC003  # plain stdlib import; no lazy benefit

from creek.lint._result import CheckResult

RULE_ROOT_STRAY = "root-stray"
RULE_ZERO_BYTE = "zero-byte"
"""Stable finding tokens, one per rule, so a report line stays greppable."""

_SCAFFOLD_DIRS: tuple[str, ...] = (
    "00-Creek-Meta",
    "01-Fragments",
    "02-Threads",
    "03-Eddies",
    "04-Praxis",
    "05-Wavelength",
    "06-Frequencies",
    "07-Voice",
    "08-Decisions",
    "09-Reference",
    "10-Liminal",
    "11-Other-Authors",
)
"""The twelve numbered directories ``creek init`` deploys.

Verified against ``creek/templates/vault/``.
"""

_ROOT_ALLOWLIST: frozenset[str] = frozenset(
    (*_SCAFFOLD_DIRS, "creek-skills", "AGENTS.md", ".obsidian"),
)
"""Every root entry ``creek init`` is responsible for.

``creek-skills/`` is the schema-skill tree, ``AGENTS.md`` the agent contract,
``.obsidian/`` Obsidian's own configuration. Dotfiles beyond ``.obsidian`` are
admitted by :func:`_is_allowed` rather than listed: Creek does not own the dot
namespace, and flagging ``.git`` or ``.DS_Store`` produces noise the operator
cannot act on.
"""


def _is_allowed(name: str) -> bool:
    """Return whether a root entry named *name* is one Creek expects to see."""
    return name.startswith(".") or name in _ROOT_ALLOWLIST


def _root_strays(vault_path: Path) -> list[str]:
    """Report every root entry outside the scaffold allowlist.

    Directories as well as files. A check scoped to files would wave through a
    whole mis-rooted tree — the failure with the largest blast radius — while
    dutifully reporting a single stray note.

    Creek's own historical output gets no exemption: a legacy
    ``review-queue-*.md`` is reported until the operator moves or deletes it.
    That is the intended nudge, because "Creek put it there" is exactly the
    excuse that let the root accumulate.
    """
    if not vault_path.is_dir():
        return []
    return [
        f"- `{entry.name}` — {RULE_ROOT_STRAY}: "
        f"{'directory' if entry.is_dir() else 'file'} at the vault root, "
        "outside the creek init scaffold"
        for entry in sorted(vault_path.iterdir())
        if not _is_allowed(entry.name)
    ]


def _zero_byte_pages(vault_path: Path) -> list[str]:
    """Report every empty ``*.md`` anywhere in the vault.

    Scoped to markdown on purpose: a zero-byte ``.gitkeep`` or a freshly
    created ``.jsonl`` log is legitimate, while an empty note is a page that
    can never carry frontmatter, can never be classified, and can never be
    linked — the ``frag-f8cd9208e113.md`` shape.

    ``stat().st_size`` answers the question exactly and never opens the file.
    """
    findings: list[str] = []
    for page in sorted(vault_path.rglob("*.md")):
        try:
            empty = page.stat().st_size == 0
        except OSError:
            continue
        if empty:
            findings.append(
                f"- `{page.relative_to(vault_path)}` — {RULE_ZERO_BYTE}: "
                "0-byte markdown file",
            )
    return findings


def run(vault_path: Path, *, since: datetime | None = None) -> CheckResult:
    """Report root-level strays and zero-byte markdown pages.

    Args:
        vault_path: Root of the Obsidian vault.
        since: Accepted for interface symmetry with the other checks and
            ignored — litter does not expire, and an mtime window would hide
            precisely the strays that have been sitting longest.

    Returns:
        A :class:`~creek.lint._result.CheckResult` naming each offender by a
        **vault-relative** path. Nothing is read and nothing is removed, so
        two successive runs report identically.
    """
    del since  # interface symmetry only
    findings = [*_root_strays(vault_path), *_zero_byte_pages(vault_path)]
    summary = f"{len(findings)} vault-root hygiene finding(s)"
    return CheckResult(name="root-hygiene", summary=summary, findings=findings)
