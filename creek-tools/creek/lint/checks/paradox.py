"""Semantic check: wrap :class:`creek.generate.paradox.ParadoxDetector`.

Lint **never** resolves paradoxes. Detected pairs are routed to
``10-Liminal/Paradoxes/`` via the existing
:meth:`~creek.generate.paradox.ParadoxDetector.create_paradox_note`
helper — the wrapper here only counts and summarises.

FEAT-025: by default the wrapped detector skips cross-level pairs (a
paragraph contradicting the section it sits inside is rhetorical
structure). Set ``lint.paradox_cross_level: true`` in
``creek_config.yaml`` to restore the pre-FEAT-025 behaviour.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003  # used at runtime as a parameter type
from pathlib import Path  # noqa: TC003  # plain stdlib import; no lazy benefit

from pydantic import ValidationError

from creek.config import load_config
from creek.generate.paradox import ParadoxDetector
from creek.lint._result import CheckResult
from creek.models import Fragment
from creek.vault.links import read_header_meta

_FRAGMENT_DIRS: tuple[str, ...] = ("01-Fragments", "10-Liminal")


def _load_fragments(vault_path: Path) -> list[Fragment]:
    """Best-effort load of every parseable Fragment in the fragment dirs.

    Reads each header with :func:`~creek.vault.links.read_header_meta` rather
    than ``frontmatter.load``. Two reasons, both load-bearing:

    * this check only ever validates the *header*, and a header-only read does
      not pull 35k bodies through memory to count paradox candidates; and
    * ``frontmatter.load`` ends in ``Post(content, handler, **metadata)``, so a
      note with a non-string frontmatter key (``2024-05-01:``) raised a bare
      ``TypeError`` that aborted the whole check (#1475). ``read_header_meta``
      parses with ``yaml.safe_load`` and never splats, so the crash is
      structurally impossible here rather than caught.

    Validation is now its own ``try``. Sharing one with the load would put a
    guard tuple around ``model_validate``, where a ``TypeError`` means a real
    bug in :class:`~creek.models.Fragment` and must not be swallowed.

    Header-only reading carries the same three deliberate consequences #1416
    accepted and documents in full at
    :func:`creek.generate.synchronicity._existing_synchronicity_pairs`: the
    ``---`` fence must open line 1, the 200-line / 64 KB header caps apply, and
    a note carrying a stray non-string key is tolerated rather than rejected.
    """
    fragments: list[Fragment] = []
    for sub in _FRAGMENT_DIRS:
        root = vault_path / sub
        if not root.is_dir():
            continue
        for md_file in root.rglob("*.md"):
            meta = read_header_meta(md_file)
            try:
                fragments.append(Fragment.model_validate(meta))
            except ValidationError:
                continue
    return fragments


def _resolve_cross_level(vault_path: Path) -> bool:
    """Read the FEAT-025 ``lint.paradox_cross_level`` knob, defaulting to False.

    Looks for ``00-Creek-Meta/creek_config.yaml`` inside *vault_path* —
    the same canonical location ``creek init`` writes. A missing file
    is silently treated as "use defaults"; config-load errors propagate
    so an operator notices a malformed YAML rather than silently
    getting the wrong policy.
    """
    config_path = vault_path / "00-Creek-Meta" / "creek_config.yaml"
    config = load_config(config_path, warn_on_missing=False)
    return config.lint.paradox_cross_level


def run(vault_path: Path, *, since: datetime | None = None) -> CheckResult:
    """Detect contradiction pairs without resolving any of them.

    FEAT-025 honours ``lint.paradox_cross_level`` from the vault's
    ``creek_config.yaml`` (defaults to ``False``).
    """
    del since  # ParadoxDetector does not support incremental scans today
    fragments = _load_fragments(vault_path)
    cross_level = _resolve_cross_level(vault_path)
    paradoxes = ParadoxDetector().detect_paradoxes(
        fragments,
        cross_level=cross_level,
    )
    findings = [
        f"- {pair.contradiction_type}: "
        f"`{pair.fragment_ids[0]}` ↔ `{pair.fragment_ids[1]}` "
        f"(routed to `10-Liminal/Paradoxes/`)"
        for pair in paradoxes
    ]
    summary = (
        f"{len(paradoxes)} paradox(es) detected; "
        f"all routed to `10-Liminal/Paradoxes/` (never resolved)"
    )
    return CheckResult(name="paradox", summary=summary, findings=findings)
