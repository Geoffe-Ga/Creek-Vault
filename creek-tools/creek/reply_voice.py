"""Which voice a ``/v1`` letter or reflection is written in (#2126).

The framework- and author-stack-free vocabulary shared by
:mod:`creek.author.reply_voice` (which decides the style) and the letter and
reflection tools (which carry it into a prompt and a response). Kept outside
``creek.author`` because the HTTP API must cold-start without importing that
package (``tests/test_container_image_contract.py``).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

STYLE_HEADING: Final[str] = (
    "## The owner's own style (how they write; style only, never content)"
)
"""Leads the style block so the model reads it as style, not material."""


class ReplyVoice(StrEnum):
    """Which voice a letter or reflection was written in.

    Attributes:
        OWNER: The prompt carried the owner's own style.
        HIGHER_SELF: No owner evidence; the generic Higher Self prompt.
    """

    OWNER = "owner"
    HIGHER_SELF = "higher_self"


@dataclass(frozen=True)
class ReplyStyle:
    """The style one reply is written in.

    Attributes:
        voice: Which voice applies.
        sections: The source-free style sections, empty for the Higher Self.
    """

    voice: ReplyVoice
    sections: tuple[str, ...] = ()

    @property
    def block(self) -> str:
        """Return the style block for the prompt, or ``""`` when there is none."""
        if not self.sections:
            return ""
        return "\n\n".join((STYLE_HEADING, *self.sections))


HIGHER_SELF_STYLE: Final[ReplyStyle] = ReplyStyle(ReplyVoice.HIGHER_SELF)
"""The style of a reply with no owner evidence: today's prompt, unchanged."""
