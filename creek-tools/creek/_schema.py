"""Shape checks for optional arrays in untrusted documents (#1004).

A parser that writes ``items = payload.get("k") or []`` has already
decided what a wrong-typed value means before its own type guard runs:
every *falsy* wrong type — ``0``, ``""``, ``false``, ``{}`` — becomes
the empty list, and the ``isinstance`` check below it never sees the
value it was written to reject.

Three call sites read an optional array out of a document nobody
validated, and each spelled that coercion for itself, so each carried
the same hole:

* ``creek.compile.engine._parse_llm_payload`` — the compile LLM's
  ``claims``/``paradoxes``, where a bypassed guard published a
  title-only page over a good one.
* ``creek.classify.calibration.load_fixture`` — the calibration
  fixture, where it turned a structurally wrong file into a silent
  zero-entry run.
* ``creek.classify.few_shot._load_all_examples`` — the per-dimension
  example fixtures, where it swallowed the warning the loader emits
  for a malformed file.

:func:`coerce_optional_list` is the single definition of the intended
rule. It *returns* the failure rather than raising it because the three
callers disagree about the response: two raise ``ValueError`` with
their own wording, one logs and moves on.

Stdlib-only by design, like :mod:`creek._fsio` and
:mod:`creek._containment`: it sits underneath the parsers that import
it and must not drag the package in behind them.
"""

from __future__ import annotations


def coerce_optional_list(value: object) -> list[object] | None:
    """Return the list *value* denotes, or ``None`` when it denotes none.

    ``None`` — an absent key, or an explicit JSON/YAML ``null`` — is the
    one coercion worth keeping: models and fixtures both write "nothing
    here" that way, and the reading is unambiguous. Every other non-list
    type is refused, *including the falsy ones*.

    Args:
        value: The raw value read out of the untrusted document.

    Returns:
        A list — ``[]`` for ``None``, otherwise *value* itself — or
        ``None`` when *value* is neither absent nor a list, which the
        caller must treat as a schema violation.
    """
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return None
