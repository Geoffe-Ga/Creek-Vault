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
* never state a custody property in a sentence without a target or
  conditional scope, or a negation directly governing the term. The scan is
  a heuristic tripwire, not a proof.

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

# The present-tense custody-claim tripwire, kept in step with adepthood's
# ``backend/tests/test_custody_decision_record.py``. A HEURISTIC, not a proof:
# it catches the phrasings these records have used to overclaim and the
# plain-English forms a reader would take as a promise, sentence by sentence.
# Review remains the real check.
#
# Affirmative custody terms: excused by an explicit target or condition
# marker, or by a negation directly in front of the term, never by a stray
# negation elsewhere in the sentence.
_AFFIRMATIVE_CLAIM: Final = re.compile(
    r"operator-blind|end-to-end|end to end|\bE2EE\b|already user-held",
    re.IGNORECASE,
)
# Claims that carry their own negation; only a target or condition excuses them.
_NEGATIVE_FORM_CLAIM: Final = re.compile(
    r"\bcan(?:not|'t) (?:read|decrypt|see)\b|\bnever carr(?:y|ies)\b|"
    r"\bnone of (?:their|your|the person's) data reaches\b|\bnever reach(?:es)?\b|"
    r"\bnever pass(?:es)? through\b|\bholds? no (?:key|escrow)\b",
    re.IGNORECASE,
)
# Explicit scope. Bare "not", "no" and "never" are deliberately absent.
_TARGET_OR_CONDITION: Final = re.compile(
    r"\b(?:target|decided|under the decision|will|until phase|once|when B13|"
    r"not yet|not implemented|if|unless|would|could)\b",
    re.IGNORECASE,
)
# A negation governing the affirmative term right after it (up to three words).
_NEGATION_BEFORE: Final = re.compile(
    r"\b(?:not|never|no|neither|nor)\b(?:\W+\w+){0,3}\W*$", re.IGNORECASE
)
# A double-quoted term is mentioned, not used.
_QUOTED: Final = re.compile(r'"[^"]*"')


def _flat(text: str) -> str:
    """Collapse every whitespace run so hard wrapping cannot hide a phrase."""
    return " ".join(text.split())


def _appended_section(filename: str) -> str:
    """The section appended for adepthood ADR 0009, from its heading to the end."""
    text = (_ADR_DIR / filename).read_text(encoding="utf-8")
    return text[text.index(_AMENDED[filename]) :]


def _sentences(text: str) -> list[str]:
    """Split prose and bullets into sentences, each flattened."""
    blocks = re.split(r"\n\s*\n|\n(?=\s*(?:[-*] |#))", text)
    return [
        sentence
        for block in blocks
        for sentence in re.split(r"(?<=[.;!?])\s+", _flat(block))
    ]


def _affirmative_is_negated(sentence: str) -> bool:
    """Whether every affirmative custody term is directly preceded by a negation."""
    return all(
        _NEGATION_BEFORE.search(sentence[: match.start()])
        for match in _AFFIRMATIVE_CLAIM.finditer(sentence)
    )


def _is_unscoped_claim(sentence: str) -> bool:
    """Whether a sentence states custody with no target, condition or negation."""
    if _TARGET_OR_CONDITION.search(sentence):
        return False
    if _NEGATIVE_FORM_CLAIM.search(sentence):
        return True
    return bool(_AFFIRMATIVE_CLAIM.search(sentence)) and not _affirmative_is_negated(
        sentence
    )


def _unscoped_custody_claims(text: str) -> list[str]:
    """Sentences that state a custody property flatly (heuristic; see above)."""
    return [
        sentence
        for sentence in _sentences(text)
        if _is_unscoped_claim(_QUOTED.sub("", sentence))
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


# Planted violations the scan must catch, including the reviewers' bypasses.
_PLANTED_CLAIMS: Final = (
    "Managed vault journal content is end-to-end encrypted, not provider-readable.",
    "Adepthood is end-to-end encrypted, with no operator escrow.",
    "The vault cannot read the journal it stores.",
    "The operator holds no key to the journal.",
    "Journal content never reaches a cloud model.",
    "The managed vault is operator-blind.",
    "Journal content is already user-held.",
)
_SCOPED_STATEMENTS: Final = (
    "Ordinary Fly is not operator-blind.",
    "It is decided to become user-held.",
    "Once phase (c) lands, the vault cannot read the journal.",
    "Neither is operator-blind while it runs.",
    'No support copy may say "operator-blind".',
)


@pytest.mark.parametrize("sentence", _PLANTED_CLAIMS)
def test_the_custody_claim_scan_catches_a_planted_claim(sentence: str) -> None:
    """Each planted flat claim is reported."""
    assert _unscoped_custody_claims(sentence) == [sentence]


@pytest.mark.parametrize("sentence", _SCOPED_STATEMENTS)
def test_the_custody_claim_scan_lets_scoped_statements_through(sentence: str) -> None:
    """A statement scoped to a target or condition, or directly negated, passes."""
    assert _unscoped_custody_claims(sentence) == []


def test_the_original_records_are_kept_above_the_amendment() -> None:
    """History is appended to, not rewritten: the original decision text stands."""
    text = (_ADR_DIR / "0014-provider-managed-custody-for-ordinary-fly.md").read_text(
        encoding="utf-8"
    )

    assert text.index("### Ordinary Fly uses one explicit provider-managed mode") < (
        text.index(_SUPERSESSION_HEADING)
    )


# Owner answers recorded in adepthood ADR 0009 on 2026-10-07. An amendment
# that still calls one of them open sends a reader looking for a decision
# that has already been made.
_ANSWERED_AS_OPEN: Final = (
    "open RUNTIME question",
    "mechanism is reopened",
)


@pytest.mark.parametrize("filename", sorted(_AMENDED))
def test_amendment_cites_no_answered_question_as_open(filename: str) -> None:
    """RUNTIME and the recovery factors are decided; no amendment calls them open."""
    section = _flat(_appended_section(filename))

    assert [phrase for phrase in _ANSWERED_AS_OPEN if phrase in section] == []
