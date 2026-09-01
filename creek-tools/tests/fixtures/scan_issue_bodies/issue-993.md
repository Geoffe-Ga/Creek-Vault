## Role
You are a senior Python engineer working in this project's codebase, following
its existing conventions (TDD via stay-green, the `cd creek-tools &&
./scripts/check-all.sh` gate, ≥90% branch coverage aggregate and ≥80% per file,
≥95% docstring coverage, complexity ≤10, mypy strict, zero lint/type
suppressions).

## Goal
Bring `creek/audit/log.py` from 88.51% to ≥95% by covering the tamper-evidence
failure paths that currently have no test: the missing-`prev_hash` rejection in
`AuditLog._verify_line`, the malformed-JSON skip in `AuditLog.iter_entries`, the
blank-line skips in both readers, and the two early returns in
`_read_last_line` — verified by `./scripts/coverage.sh` showing lines 150, 156,
270, 274, 294 and 319-320 covered.

## Context
- File(s): `creek-tools/creek/audit/log.py:149-157` (`_read_last_line`),
  `:266-277` (`iter_entries`), `:289-296` (`verify_chain` loop),
  `:313-320` (`_verify_line`)
- Scanned at commit: `317ea3d9c01d5ef59f76e6a920b19dd13789a2d8` — re-verify
  against HEAD before starting
- Evidence: `cd creek-tools && ./scripts/coverage.sh --json` at that SHA:
  ```
  Name                Stmts   Miss Branch BrPart   Cover   Missing
  creek/audit/log.py    114      9     34      8  88.51%   48-49, 150, 156, 270, 274, 294, 319-320
  ```
  Uncovered branches: `47->48`, `149->150`, `155->156`, `211->218`, `234->-189`,
  `269->270`, `293->294`, `318->319`. Mapping each to behaviour:
  - `150` — `_read_last_line` returns `None` when the log file does not exist
  - `156` — returns `None` when the file contains only blank lines
  - `270` / `294` — blank-line skip inside `iter_entries` / `verify_chain`
  - `274` — the `except json.JSONDecodeError` warn-and-skip in `iter_entries`
  - `319-320` — `raise AuditChainBrokenError(f"Audit line {index} is missing
    'prev_hash'")`. This is the *only* chain-integrity rejection with no test:
    `tests/test_audit_log.py` covers the removed-first-entry and
    modified-payload cases (the raise at `:330`) and the not-valid-JSON case
    (the raise at `:317`), but never an entry that parses cleanly yet omits the
    `prev_hash` field entirely — i.e. the exact shape a tamperer produces by
    replacing a line with a plausible-looking JSON object.
  - `211->218` / `234->-189` — the `_HAS_FCNTL is False` append path (Windows /
    no-fcntl platforms), where the flock and the matching unlock are skipped
- This is a tamper-evidence log; an untested rejection branch is an untested
  security control, not a missing number. Prefer the branch cases over the
  trivial line hits.
- Related: `creek_mcp/audit.py` (90.09%) has the parallel MCP-side chain, tested
  in `tests/test_mcp_audit.py:151,180` — those tests are the shape to mirror.

## Output Format
A single PR that: (1) adds a failing test first, (2) makes it pass, (3) passes
`cd creek-tools && ./scripts/check-all.sh`, and (4) references this issue with
"Closes #N".

## Examples
Add to `creek-tools/tests/test_audit_log.py`, alongside the existing
`test_verify_rejects_modified_payload`:

```python
def test_verify_rejects_entry_missing_prev_hash(tmp_path: Path) -> None:
    """A well-formed JSON line without prev_hash breaks the chain (log.py:318-320)."""
    log = AuditLog(tmp_path / "audit.jsonl")
    log.append({"event": "first"})
    log.append({"event": "second"})
    lines = log.path.read_text(encoding="utf-8").splitlines()
    lines[1] = json.dumps({"event": "second"})  # parses, but no prev_hash
    log.path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(AuditChainBrokenError, match="missing 'prev_hash'"):
        log.verify_chain()


def test_iter_entries_skips_malformed_line(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """iter_entries warns and skips undecodable lines rather than raising (log.py:273-277)."""
    log = AuditLog(tmp_path / "audit.jsonl")
    log.append({"event": "good"})
    with log.path.open("a", encoding="utf-8") as fh:
        fh.write("{not json\n\n")  # malformed line + a blank line (270)

    with caplog.at_level(logging.WARNING):
        entries = list(log.iter_entries())

    assert [e["event"] for e in entries] == ["good"]
    assert "Skipping malformed audit line" in caplog.text
```

For the fcntl-absent path, monkeypatch `creek.audit.log._HAS_FCNTL` to `False`
and assert an append still round-trips and still chains correctly.

## Constraints
- Do not change public API signatures unless the Goal says so
- Do not relax the chain checks to make them easier to exercise — assert on the
  raised `AuditChainBrokenError` message, which names the offending line index
- No assertion-free "coverage theater": every new test must assert on returned
  entries, raised message text, or emitted log records
- No lint/type suppressions (max-quality-no-shortcuts): fix root causes
- Scope: this issue only — `creek_mcp/audit.py` has its own gap; file a
  follow-up rather than widening this PR
- If the finding no longer reproduces at HEAD, close this issue with a comment
  explaining what changed instead of forcing a PR
