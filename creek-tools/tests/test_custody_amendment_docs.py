"""Guard the adepthood ADR 0009 amendments against present-tense custody claims.

On 2026-10-07 the adepthood owner decided the operator must not be able to
read journal entries (adepthood ADR 0009). ADR-0005, ADR-0006 and ADR-0007
here carry appended amendments for it, and ADR-0014 is superseded for journal
content. None of that is implemented: until adepthood B13 lands user-held
keys, journal content reaching a vault stays operator-readable. An amendment
that says the target flatly ("is already user-held", "operator-blind") would
be the most authoritative false custody claim in the repository.

So each appended section must:

* say it is not implemented yet, where it describes a custody target,
* carry the "no public claim" disclaimer pointing at adepthood B24, and
* never state a custody property in a sentence without a negation or a
  target/conditional scope.

Only the appended sections are scanned. The original records above them are
history and keep their own wording.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

import pytest

_ADR_DIR: Final = Path(__file__).resolve().parents[1] / "docs" / "architecture" / "ADR"

_AMENDMENT_HEADING: Final = "## Amended by adepthood ADR 0009 (2026-10-07)"
_SUPERSESSION_HEADING: Final = (
    "## Superseded for journal content by adepthood ADR 0009 (2026-10-07)"
)

# Record file -> the heading its appended section starts at.
_AMENDED: Final = {
    "0005-confidential-volume-key-no-escrow.md": _AMENDMENT_HEADING,
    "0006-enclave-attestation-trust-model.md": _AMENDMENT_HEADING,
    "0007-confidential-per-user-hosting.md": _AMENDMENT_HEADING,
    "0014-provider-managed-custody-for-ordinary-fly.md": _SUPERSESSION_HEADING,
}

# Sections that describe a custody target, and so must say it is not built.
_DESCRIBES_A_CUSTODY_TARGET: Final = frozenset(
    {
        "0005-confidential-volume-key-no-escrow.md",
        "0007-confidential-per-user-hosting.md",
        "0014-provider-managed-custody-for-ordinary-fly.md",
    }
)

_NO_CLAIM: Final = "Nothing here is a public claim. Each claim waits for adepthood B24"
_NOT_IMPLEMENTED: Final = "Not implemented yet."

_CUSTODY_CLAIM: Final = re.compile(
    r"operator-blind|end-to-end|\bE2EE\b|already user-held|cannot read it",
    re.IGNORECASE,
)
_SCOPING_MARKER: Final = re.compile(
    r"\b(?:not|no|never|neither|nor|until|unless|once|will|would|if|whether|"
    r"target|decided|must|only while|stop|stops)\b",
    re.IGNORECASE,
)


def _flat(text: str) -> str:
    """Collapse every whitespace run so hard wrapping cannot hide a phrase."""
    return " ".join(text.split())


def _appended_section(filename: str) -> str:
    """The section appended for adepthood ADR 0009, from its heading to the end."""
    text = (_ADR_DIR / filename).read_text(encoding="utf-8")
    return text[text.index(_AMENDED[filename]) :]


def _unscoped_custody_claims(text: str) -> list[str]:
    """Sentences that state a custody property flatly, with no negation or scope."""
    blocks = re.split(r"\n\s*\n|\n(?=\s*(?:[-*] |#))", text)
    sentences = [
        sentence
        for block in blocks
        for sentence in re.split(r"(?<=[.;!?])\s+", _flat(block))
    ]
    return [
        sentence
        for sentence in sentences
        if _CUSTODY_CLAIM.search(sentence) and not _SCOPING_MARKER.search(sentence)
    ]


@pytest.mark.parametrize("filename", sorted(_AMENDED))
def test_amendment_makes_no_public_claim(filename: str) -> None:
    """Every appended section defers every claim to adepthood B24."""
    assert _NO_CLAIM in _flat(_appended_section(filename))


@pytest.mark.parametrize("filename", sorted(_DESCRIBES_A_CUSTODY_TARGET))
def test_custody_target_says_it_is_not_implemented(filename: str) -> None:
    """A section describing user-held custody says it is not built yet."""
    assert _NOT_IMPLEMENTED in _appended_section(filename)


@pytest.mark.parametrize("filename", sorted(_AMENDED))
def test_amendment_states_no_custody_property_flatly(filename: str) -> None:
    """No sentence in an appended section claims a custody property unscoped."""
    assert _unscoped_custody_claims(_appended_section(filename)) == []


def test_the_custody_claim_scan_bites() -> None:
    """A flat claim is caught; a negated or scoped one is not."""
    assert _unscoped_custody_claims("Journal content is already user-held.") != []
    assert _unscoped_custody_claims("The managed vault is operator-blind.") != []
    assert _unscoped_custody_claims("Ordinary Fly is not operator-blind.") == []
    assert _unscoped_custody_claims("It is decided to become user-held.") == []


def test_the_original_records_are_kept_above_the_amendment() -> None:
    """History is appended to, not rewritten: the original decision text stands."""
    text = (_ADR_DIR / "0014-provider-managed-custody-for-ordinary-fly.md").read_text(
        encoding="utf-8"
    )

    assert text.index("### Ordinary Fly uses one explicit provider-managed mode") < (
        text.index(_SUPERSESSION_HEADING)
    )
