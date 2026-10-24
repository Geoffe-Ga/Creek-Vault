"""Content-free stage vocabulary for long-running compositions (#1966).

``creek draft`` and the Writing Desk both run for minutes on a pinned local
model. An adapter that wants to tell its caller *how far along* a composition
is — the MCP server's ``notifications/progress``, today — needs the pipeline to
say when it crosses a stage boundary, and it needs that signal to carry nothing
the caller was not already entitled to.

So a stage is a **name from a closed enum and nothing else**. No callback here
receives a title, a fragment id, a count or a line of prose: the enum value is
the whole payload, which is what lets an adapter forward it over the network
without a per-call privacy argument. A pipeline that wants to report more must
grow the enum, in review, rather than widen the callback.

This module is deliberately dependency-free (standard library only) so the
draft generator, the Writing Desk conductor and the MCP adapter can all import
it without dragging each other's import graphs along.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, TypeAlias

if TYPE_CHECKING:
    from collections.abc import Callable


class CompositionStage(StrEnum):
    """One stage boundary a composition pipeline can report crossing.

    The order of the members is the order a ``creek draft`` composition
    crosses them; the Writing Desk crosses a subset (it selects no seed and
    assembles no skill stack).

    Attributes:
        SEED: The idea seed to draft from has been selected.
        SKILLS: The skill stack the prompt is voiced through is assembled.
        SOURCES: The source material the prompt carries is gathered.
        COMPOSED: The model has returned a composed body.
        VOICE_GUARDED: The body has been through the voice / reflection guard.
        SAVED: The draft has been written to the vault.
    """

    SEED = "seed"
    SKILLS = "skills"
    SOURCES = "sources"
    COMPOSED = "composed"
    VOICE_GUARDED = "voice_guarded"
    SAVED = "saved"


StageCallback: TypeAlias = "Callable[[CompositionStage], None]"
"""A sink told each stage boundary as the pipeline crosses it."""


def ignore_stage(stage: CompositionStage) -> None:
    """Discard *stage*: the default sink for a caller that tracks no progress.

    A real function rather than ``None`` so a pipeline can call its sink
    unconditionally, with no ``if on_stage is not None`` branch at every
    boundary.

    Args:
        stage: The boundary just crossed, ignored.
    """
    del stage


def once_per_stage(callback: StageCallback) -> StageCallback:
    """Wrap *callback* so each stage reaches it at most once, in first-seen order.

    The Writing Desk composes and reviews inside a bounded revision loop, so it
    crosses ``COMPOSED`` and ``VOICE_GUARDED`` once per round. A progress
    consumer is promised a monotonic sequence (MCP requires a notification's
    ``progress`` to increase), so the repeats are dropped here rather than with
    a round counter at each call site.

    Args:
        callback: The sink to forward first sightings to.

    Returns:
        A sink that forwards each distinct stage the first time it is seen.
    """
    seen: set[CompositionStage] = set()

    def _forward(stage: CompositionStage) -> None:
        """Forward *stage* unless it has already been reported."""
        if stage in seen:
            return
        seen.add(stage)
        callback(stage)

    return _forward
