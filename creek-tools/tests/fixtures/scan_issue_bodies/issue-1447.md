## Role
You are a senior Python engineer working in this project's codebase, following
its existing conventions (TDD via stay-green, the `cd creek-tools &&
./scripts/check-all.sh` gate, ≥90% branch coverage aggregate and ≥80% per file,
≥95% docstring coverage, complexity ≤10, mypy strict, zero lint/type
suppressions).

## Goal
Cover the legacy-provenance migration failure-status matrix
(`read_failed` / empty-`ok` / non-list `parse_failed` / mid-write `OSError`
re-raise) and the entirely-untested filename-collision suffixing in
`_unique_filename`, raising `creek/vault/writer.py` from 92.66% and proving
that a second model with the same base name never silently overwrites the
first.

## Context
- File(s): `creek-tools/creek/vault/writer.py` — `_read_legacy_provenance`
  (`:207-224`), `_migrate_legacy_provenance` mid-write handler (`:295-308`),
  `_render_decision_body` (`:362`, `:366`), `_atomic_write` temp cleanup
  (`:414-415`), `find_fragment_file` missing-root (`:941`),
  `_rebuild_index` missing-dir (`:1747`), `_unique_filename`
  (`:1929-1936`), `_log_provenance` shim (`:1980-1981`).
- Scanned at commit: `c8c5131b4e9afc4e3962d976eaecc8dfb89ba919` — re-verify against HEAD before starting
- Evidence: `./scripts/coverage.sh` term-missing row for this module:

  ```
  Name                     Stmts   Miss Branch BrPart   Cover   Missing
  creek/vault/writer.py      443     27    102      9  92.66%   49-53, 209-211, 213,
    223, 295-308, 362, 366, 387->382, 414-415, 941, 1747, 1929-1936, 1980-1981
  ```

  (`49-53` is an `if TYPE_CHECKING:` block — a measurement artifact, not a
  behavioral gap. Ignore it; do not write a test for it.)

  `_unique_filename` is uncovered end-to-end — every statement in the method
  body, at the scanned SHA:

  ```python
  # writer.py:1929-1936 — ZERO coverage, including the collision loop.
  base_name = self._compute_base_name(model)
  filename = f"{base_name}.md"
  if not (target_dir / filename).exists():
      return filename
  counter = 1
  while (target_dir / f"{base_name}-{counter}.md").exists():
      counter += 1
  return f"{base_name}-{counter}.md"
  ```

  The migration status matrix returns a distinct string per failure mode, and
  three of the four are unproven:

  ```python
  # writer.py:207-224
  except OSError:
      return [], "read_failed"     # <-- 209-211, uncovered
  if not raw.strip():
      return [], "ok"              # <-- 213,     uncovered
  ...
  if not isinstance(data, list):
      return [], "parse_failed"    # <-- 223,     uncovered
  ```

  Only the `json.JSONDecodeError` → `"parse_failed"` arm (216-221) is
  exercised. The status is stamped onto the migration marker in the audit log
  (see the `_read_legacy_provenance` docstring at `:203-205`), so an operator
  reading that log currently cannot trust three of the four values.
- Related: sibling findings from this same scan run — `creek/purge/engine.py`
  (#1446), `creek/generate/decisions.py`, `creek/classify/llm/providers.py`,
  `creek/generate/mining.py`, `creek/generate/state.py`.

## Output Format
A single PR that: (1) adds a failing test first, (2) makes it pass, (3) passes
`cd creek-tools && ./scripts/check-all.sh`, and (4) references this issue with
"Closes #N".

## Examples
Test plan — assert the observable outcome, not just that a line ran:

```python
def test_unique_filename_suffixes_on_collision(tmp_path: Path) -> None:
    """A second model with the same base name gets -1, not an overwrite."""
    writer = VaultWriter(tmp_path)
    first = writer.write_model(_fragment(id="a", title="Same Title"))
    second = writer.write_model(_fragment(id="b", title="Same Title"))
    assert first != second                       # covers 1929-1932
    assert second.name.endswith("-1.md")         # covers 1933-1936
    assert first.exists() and second.exists()    # neither clobbered the other

@pytest.mark.parametrize(
    ("raw", "expected_status"),
    [(None, "read_failed"), ("", "ok"), ('{"a": 1}', "parse_failed")],
)
def test_legacy_provenance_migration_status(tmp_path, raw, expected_status):
    """Each migration failure mode stamps its own status on the marker."""
    ...
    assert marker["status"] == expected_status   # covers 209-211, 213, 223
```

Remaining cases: a three-way collision (`-2.md`, exercising the `while` at
1934-1935); a mid-migration `OSError` (assert it re-raises at 308 **and** the
legacy file still exists — the docstring at 296-301 promises no entries are
lost); `_render_decision_body` for a Decision with `options` and with
`outcome` (assert both sentences appear, 362 / 366); `_atomic_write` where
`os.replace` raises (assert the temp file is cleaned up, 414-415);
`find_fragment_file` with no `01-Fragments` dir (assert `None`, 941);
`_rebuild_index` on a missing dir (assert `{}`, 1747); the `_log_provenance`
shim (assert it appends under the lock, 1980-1981).

## Constraints
- Do not change public API signatures unless the Goal says so
- No lint/type suppressions (max-quality-no-shortcuts): fix root causes
- Scope: this issue only — file follow-up issues for adjacent problems
- Do NOT test the `if TYPE_CHECKING:` block at 49-53; excluding it is correct
- If `_unique_filename` turns out to be dead code (no production caller),
  say so in the PR and delete it rather than writing a test that props it up
- If the finding no longer reproduces at HEAD, close this issue with a comment
  explaining what changed instead of forcing a PR
