## Role
You are a senior Python engineer working in this project's codebase, following
its existing conventions (TDD via stay-green, the `cd creek-tools &&
./scripts/check-all.sh` gate, ≥90% branch coverage aggregate and ≥80% per file,
≥95% docstring coverage, complexity ≤10, mypy strict, zero lint/type
suppressions).

## Goal
Cover the three uncovered guard branches in
`creek/generate/compost_embedding.py` — the non-mapping exemplar `ValueError`,
the zero-norm cosine guard, and the empty-exemplar-vectors early return —
lifting the file from 88.51% to ≥95%.

## Context
- File(s): `creek-tools/creek/generate/compost_embedding.py:89-91`, `:120-121`, `:157-158`
- Scanned at commit: `a5809aa5a853fba6e73aec2be397546eeaa1d7ed` — re-verify against HEAD before starting
- Evidence — `./scripts/coverage.sh` term-missing:
  ```
  Name                                  Stmts   Miss Branch BrPart   Cover   Missing
  creek/generate/compost_embedding.py      65      6     22      4  88.51%   29-31, 90-91, 121, 158
  ```
  `29-31` is a `TYPE_CHECKING` block (ignore). The other three are real guards:
  ```python
   88|    for index, entry in enumerate(raw):
   89|        if not isinstance(entry, dict):
   90|            msg = f"Exemplar {index} in {target} is not a mapping."
   91|            raise ValueError(msg)   # <- schema-contract rejection, untested
  ...
  120|    if 0.0 in (norm_a, norm_b):
  121|        return 0.0                  # <- zero-vector guard, untested
  122|    return dot / (norm_a * norm_b)
  ...
  157|        if not exemplar_vectors:
  158|            return 0.0              # <- no exemplars loaded, untested
  ```
  `tests/test_compost_embedding.py` exists and covers the happy path plus the
  missing-required-keys rejection, but not these three. Line 121 is the guard
  that prevents a `ZeroDivisionError` on a zero-magnitude embedding — a real
  crash if it regresses; line 158 is what makes the similarity function safe to
  call before any exemplar is configured; line 91 is a documented `ValueError`
  schema contract (it even carries a `# noqa: TRY004` justifying that contract)
  with nothing asserting the contract holds.
- Related: #996 (`creek/generate/drafts.py`), #871 (`creek/generate/unnamed.py`).

## Output Format
A single PR that: (1) adds a failing test first, (2) makes it pass, (3) passes
`cd creek-tools && ./scripts/check-all.sh`, and (4) references this issue with
"Closes #N".

## Examples
Three focused cases in `tests/test_compost_embedding.py`:

```python
def test_non_mapping_exemplar_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "compost.yaml"
    target.write_text("- ['not', 'a', 'mapping']\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Exemplar 0 .* is not a mapping"):
        load_compost_exemplars(target)


def test_cosine_similarity_returns_zero_for_zero_vector() -> None:
    assert _cosine_similarity([0.0, 0.0], [1.0, 0.0]) == 0.0
    assert _cosine_similarity([1.0, 0.0], [0.0, 0.0]) == 0.0   # both operand sides


def test_similarity_returns_zero_when_no_exemplar_vectors(monkeypatch) -> None:
    fn = make_similarity_fn(linker=..., exemplars=[])
    assert fn("some text") == 0.0
```

## Constraints
- Do not change public API signatures unless the Goal says so
- Exercise both operands of the zero-norm guard (`norm_a` and `norm_b`) so the
  `in (norm_a, norm_b)` tuple check is genuinely pinned
- Assert the `ValueError` message, not just the type — the message names the
  offending exemplar index and is part of the documented schema contract
- No lint/type suppressions (max-quality-no-shortcuts): fix root causes
- Scope: this issue only — file follow-up issues for adjacent problems
- If the finding no longer reproduces at HEAD, close this issue with a comment
  explaining what changed instead of forcing a PR
