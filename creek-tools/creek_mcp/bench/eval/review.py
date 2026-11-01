"""The owner's private review file and its unblinding key (#1875).

An owner-run ``bench eval --corpus-vault`` writes three files. The ranking
(``--out``) is content-free. The candidates' texts — written in the owner's
voice, from his own prose — go to **this** module's two outputs instead:

- the **review file** (``--review-out``): every candidate's text under an
  opaque label, for the owner to read side by side and judge by ear;
- the **unblinding key** (``--unblind-out``): label to model tag, kept in a
  separate file so the owner can judge first and look second.

The labels are the existing blinding — :func:`~…blinding.blind` and
:func:`~…blinding.identity_map` over every model's candidate at the run's
seed — so the shuffle and the label derivation are the ones the judge path
already relies on, not a second scheme.

Both files are created with mode ``0600`` and never overwrite: the open uses
``O_CREAT | O_EXCL``, which also refuses a pre-planted symlink at the path.
They are personal data and must never be committed or shared; the review file
says so in its own first lines.
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING, Any, Final

from creek_mcp.bench.eval import RUBRIC_VERSION
from creek_mcp.bench.eval.blinding import blind, identity_map

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from creek_mcp.bench.eval.blinding import Candidate
    from creek_mcp.bench.eval.vault_corpus import VaultCorpus

PRIVATE_MODE: Final[int] = 0o600
"""Owner read/write only, for both private outputs."""

UNBLIND_KIND: Final[str] = "voice_fidelity_unblind_key"
"""The key file's ``kind``."""

_OPEN_FLAGS: Final[int] = os.O_WRONLY | os.O_CREAT | os.O_EXCL
_WARNING: Final[str] = (
    "PRIVATE. This file holds text generated from your own vault. Never commit "
    "it, attach it to an issue, or share it. Delete it when you are done."
)
_HOW_TO: Final[str] = (
    "Read every candidate and judge which sounds most like you before opening "
    "the unblinding key. Citations such as [[entry-0003]] refer to the sampled "
    "fragments listed at the end."
)


def render_review(
    candidates: Sequence[Candidate], *, seed: int, vault: VaultCorpus
) -> str:
    """Return the blinded review document for *candidates*.

    Args:
        candidates: Every model's candidate, in the order the models were named.
        seed: The run's seed; selects order and labels.
        vault: The sample the candidates were prompted with, for the
            alias-to-fragment table.

    Returns:
        Markdown: a warning, each candidate under ``## Candidate <label>`` in
        blinded order, then which fragment each alias stands for. No model tag
        appears anywhere in it.
    """
    lines = [
        "# Voice-fidelity review (blinded)",
        "",
        _WARNING,
        "",
        _HOW_TO,
        "",
        f"Seed {seed}, rubric {RUBRIC_VERSION}.",
        "",
    ]
    for candidate in blind(candidates, seed=seed):
        lines.extend((f"## Candidate {candidate.candidate_id}", "", candidate.text, ""))
    lines.extend(("## Sampled fragments", ""))
    lines.extend(
        f"- {alias}: {source}"
        for alias, source in zip(
            vault.corpus.fragment_ids, vault.source_ids, strict=True
        )
    )
    return "\n".join(lines) + "\n"


def unblind_key(candidates: Sequence[Candidate], *, seed: int) -> dict[str, Any]:
    """Return the label-to-model key for the review :func:`render_review` wrote.

    Args:
        candidates: The same candidates, in the same order.
        seed: The same seed.

    Returns:
        ``{"kind", "seed", "labels": {label: model_tag}}``.
    """
    return {
        "kind": UNBLIND_KIND,
        "seed": seed,
        "labels": identity_map(candidates, seed=seed),
    }


def write_private(path: Path, text: str) -> None:
    """Create *path* with mode ``0600`` and write *text*; never overwrite.

    Args:
        path: A path that must not exist yet.
        text: The content.

    Raises:
        FileExistsError: When *path* already exists (or is a symlink).
    """
    descriptor = os.open(path, _OPEN_FLAGS, PRIVATE_MODE)
    # The umask can only narrow the creation mode; this pins it exactly.
    os.fchmod(descriptor, PRIVATE_MODE)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(text)


def write_unblind_key(path: Path, key: dict[str, Any]) -> None:
    """Write *key* as JSON to a fresh ``0600`` file at *path*."""
    write_private(path, json.dumps(key, indent=2, sort_keys=True) + "\n")
