"""The synthetic voice corpus: one invented voice, written for this harness.

#1875's first constraint is "no real journal content in the eval", and #1850's
privacy invariant says the same. So the corpus is prose written *for* the
harness: sixteen short entries in a single invented voice, chosen to be
measurably distinct — short declaratives, heavy first person, plain concrete
nouns, almost no subordination and no latinate abstractions. A candidate that
drifts into generic essay prose is then measurably further away, which is the
only reason a crude judge can rank anything at all.

The corpus is a real vault as far as the shared voice code paths are concerned:
model-valid ``open`` fragments under ``01-Fragments/Notes``, the
``00-Creek-Meta`` directory, an empty ``07-Voice/Drafts``, and a persisted
voice fingerprint built from the entries themselves — without which
:mod:`creek.lint.checks.voice_fidelity` has nothing to measure against. No
scorer writes drafts any more: #1911 retired the obedience dimension's
voice-fidelity component as a component that could not fail, and the drafts
directory now exists because a vault has one, and because the measurement
behind that decision is re-verified from it on every run.

**Confinement.** The root goes through
:func:`creek_mcp.bench.corpus.confined_root`, the same gate the capacity
corpus uses: strictly under the system temp directory, absent or empty, checked
before anything is written. A real vault is refused untouched.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from creek.config import AIStyleConfig
from creek.generate.ai_style.fingerprint import build_fingerprint, save_fingerprint
from creek_mcp.bench.corpus import FRAGMENTS_DIR, META_DIR, confined_root

VOICE_ENTRIES: Final[tuple[str, ...]] = (
    "I woke before the alarm again. The room was cold. I did not get up. I "
    "listened to the pipes tick and the gulls start. I thought about the box "
    "of my father's tools in the shed. I have not opened it. I got up and put "
    "the kettle on and stood there until it boiled.",
    "The bus was late so I walked. Two miles, maybe less. I passed the yard "
    "where they keep the broken boats. One of them has a hole you could sit "
    "in. I stopped and looked at it for a while. Then I went on and got to "
    "work five minutes early, which never happens.",
    "I cooked for four and ate alone. Not sad about it. I like the leftovers. "
    "I put the rest in the blue dish and washed the pans while the radio "
    "talked at me. I do not remember what it said. I went to bed with the "
    "window open and the street was loud and I slept fine.",
    "I finished the book. The last thirty pages were worth the first three "
    "hundred, which is a bad deal and I would take it again. I wrote one line "
    "in the front cover and put it on the shelf. I will not read it twice. I "
    "will remember the ending.",
    "I cleared the shed. Four hours. I found a bird's nest in the rafters and "
    "left it. I found the tools. I oiled the plane and put a new blade in the "
    "knife and swept the floor twice. My hands smelled of iron all evening and "
    "I did not wash them until bed.",
    "Rain all day. I did nothing I meant to do. I sat by the window and "
    "watched the gutter fail in the same place it failed last year. I should "
    "fix it. I made tea three times and drank one. I was not unhappy. I was "
    "just stopped.",
    "My sister called. We talked about the house and then about nothing for "
    "another hour. She laughed at the thing about the boats. I did not tell "
    "her about the shed. I will next time. After she hung up the flat was very "
    "quiet and I turned the radio on again.",
    "I swam at the pool before work. Eight lengths, badly. The water smelled "
    "of chlorine and the ceiling drips on lane one. An old man beat me by half "
    "a length and did not notice he was racing. I came out hungry and happy "
    "and ate two eggs standing at the counter.",
    "I counted the money twice and it was still short. Not by much. I went "
    "back through the week and found it was the taxi. I was annoyed for ten "
    "minutes and then I was not. I wrote it down so I would stop looking for "
    "it. The rest of the evening was fine.",
    "The apple tree is going. Half the branches are bare and the fruit is "
    "small and spotted. I picked what there was and made six jars. The kitchen "
    "smelled of cloves until midnight. I do not know how to save a tree. I "
    "will ask the man at the allotment.",
    "I walked to the headland and the wind took my hat into the gorse. I got "
    "it back. I sat on the bench with the dead man's name on it and watched a "
    "tanker not move for twenty minutes. Then I walked home the long way "
    "because I was not ready to be indoors.",
    "I slept badly and worked badly. By three I was reading the same paragraph "
    "for the fourth time. I went out and bought bread I did not need and "
    "walked around the block and came back and it was better. Not good. "
    "Better. I stopped at five and did not feel guilty.",
    "I mended the chair. The joint was dry so I knocked it apart and glued it "
    "and clamped it with a belt because I have no clamps. It held. I sat in it "
    "all evening to be sure. Small thing. I have been putting it off since "
    "March and now it is done.",
    "I met Ruth at the market and we had coffee at the stall with the bad "
    "chairs. She has cut her hair. She asked what I was working on and I gave "
    "her the short answer, which is a lie by omission. I will tell her "
    "properly. The coffee was good and the chairs were still bad.",
    "The frost got the beans. I knew it would and I did not cover them. I "
    "pulled them up and put them on the heap and planted garlic in the row "
    "instead. Twenty cloves. I patted the soil down and stood up and my back "
    "told me about it for the rest of the day.",
    "Last night I sat outside until I was cold. No moon. The lane was black "
    "and the hedge made noises. I was not frightened and I was not peaceful. I "
    "was just out there, being a person in a garden. Then I went in and locked "
    "the door and that was the day.",
)
"""Sixteen entries in one invented voice. Not a journal; nobody wrote these."""

MIN_ENTRIES: Final[int] = 10
"""#1875 asks for 10-20 sample entries; fewer is too thin a voice to score."""

MAX_ENTRIES: Final[int] = 20
"""The other end of #1875's range."""

DRAFTS_DIR: Final[Path] = Path("07-Voice") / "Drafts"
"""Where the obedience dimension writes candidates, mirroring a real vault."""

_STAMP: Final[str] = "2026-01-01T00:00:00+00:00"
"""Fixed creation time, so the corpus is byte-identical across runs."""

_FENCE: Final[str] = "---"
_ID_PREFIX: Final[str] = "voice"
_BAD_ENTRIES: Final[str] = (
    f"entries must be between {MIN_ENTRIES} and {min(MAX_ENTRIES, len(VOICE_ENTRIES))}"
)


@dataclass(frozen=True, slots=True)
class VoiceCorpus:
    """A built synthetic voice corpus.

    Attributes:
        root: The resolved corpus root, usable as a ``vault_path``.
        fragment_ids: The ids of the written fragments, in order. The grounding
            dimension scores a citation against exactly this set.
        voice_sample: The corpus's own prose, which is the reference the judge
            compares a candidate against.
        entry_count: How many entries were written.
    """

    root: Path
    fragment_ids: tuple[str, ...]
    voice_sample: str
    entry_count: int


def _note(index: int, body: str) -> str:
    """Render one model-valid self-authored ``open`` journal fragment.

    Front-matter values are written as JSON, which is valid YAML flow syntax,
    so the bytes do not depend on the installed PyYAML's formatting choices
    and the corpus stays byte-stable across environments.
    """
    metadata: dict[str, Any] = {
        "type": "fragment",
        "id": f"{_ID_PREFIX}-{index:04d}",
        "title": f"voice entry {index:04d}",
        "created": _STAMP,
        "ingested": _STAMP,
        "source": {"platform": "journal", "author": "self"},
        "frequency": {"primary": "F1", "secondary": []},
        "privacy_tier": "open",
        "eddies": [],
    }
    header = "\n".join(f"{key}: {json.dumps(value)}" for key, value in metadata.items())
    return f"{_FENCE}\n{header}\n{_FENCE}\n{body}\n"


def build_voice_corpus(root: Path, *, entries: int | None = None) -> VoiceCorpus:
    """Write the synthetic voice corpus under *root* and fingerprint it.

    Shrinking the corpus takes a prefix of :data:`VOICE_ENTRIES` rather than a
    sample, so a smaller run uses a subset of the same entries with the same
    ids — two runs at different sizes stay comparable in the obvious way.

    Args:
        root: Absent or empty directory strictly under the system temp dir.
        entries: How many entries to write; defaults to all of them.

    Returns:
        The built corpus.

    Raises:
        CorpusConfinementError: When *root* is not admissible; nothing is
            written.
        ValueError: When *entries* is outside #1875's 10-20 range (or exceeds
            the entries this module ships).
    """
    count = len(VOICE_ENTRIES) if entries is None else entries
    if not MIN_ENTRIES <= count <= min(MAX_ENTRIES, len(VOICE_ENTRIES)):
        raise ValueError(_BAD_ENTRIES)
    resolved = confined_root(root)
    notes_dir = resolved / FRAGMENTS_DIR
    for directory in (notes_dir, resolved / META_DIR, resolved / DRAFTS_DIR):
        directory.mkdir(parents=True, exist_ok=True)
    bodies = VOICE_ENTRIES[:count]
    ids = tuple(f"{_ID_PREFIX}-{index:04d}" for index in range(count))
    for index, (ident, body) in enumerate(zip(ids, bodies, strict=True)):
        (notes_dir / f"{ident}.md").write_text(_note(index, body), encoding="utf-8")
    config = AIStyleConfig()
    # ``audience_weighting=None`` is the flat-average path, stated out loud for
    # the #1313 guard. It is also the only correct value here: an *enabled*
    # weighting is a membership gate on this path, and it gives a journal
    # fragment zero audience authority — which would drop every entry and leave
    # an empty fingerprint for the voice-fidelity check to measure nothing with.
    # The corpus is journal-only by construction, so there is no audience mix
    # to weight.
    save_fingerprint(
        build_fingerprint(
            resolved, config, include_intimate=False, audience_weighting=None
        ),
        resolved,
        config,
    )
    return VoiceCorpus(
        root=resolved,
        fragment_ids=ids,
        voice_sample="\n\n".join(bodies),
        entry_count=count,
    )
