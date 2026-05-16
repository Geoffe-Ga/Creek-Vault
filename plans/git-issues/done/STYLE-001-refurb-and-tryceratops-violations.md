# STYLE-001: 134 `refurb` violations and 9 `tryceratops` violations are not gated by CI

**Severity:** Low
**Category:** STYLE
**Estimated complexity:** M (≤1d)
**Parallelizable with peers in same category:** yes
**Discovered by:** Dimension 4 (style) — `refurb` and `tryceratops` are in `.pre-commit-config.yaml` but not enforced in CI

## Files affected
- 134 `FURB*` violations across `creek/classify/`, `creek/clean/`, `creek/cli.py`, `creek/config.py`, etc. (sample list available)
- 9 `TRY*` violations (`creek/classify/llm.py:553, 611`, `creek/ingest/code.py:354`, `creek/ingest/discord.py:373`, `creek/ingest/markdown.py:351`, `creek/ingest/gdrive.py:335`, `creek/clean/hygiene.py:702`, `creek/generate/voice.py:186`, `creek/generate/unnamed.py:446`)
- `creek-tools/.pre-commit-config.yaml:91, 87` — hooks configured but pre-commit only catches new code, not the existing surface
- `.github/workflows/ci.yml` — neither tool runs in CI

## Dependencies
None.

## Reproduction
```bash
refurb creek/ | wc -l   # 134
tryceratops creek/ 2>&1 | grep -c "creek/"  # 9
```

## Analysis

Both tools are configured in pre-commit but pre-commit runs only on changed files. The existing 134+9 violations slipped in over time. CI doesn't run either tool, so they're effectively voluntary.

Most refurb violations are genuine readability improvements:
- `lambda x: x[1]` → `operator.itemgetter(1)` (×5 in `classify/rules.py`)
- `list(...)` / `dict(...)` over copy-able containers → `.copy()`
- `lines.append(a); lines.append(b)` → `lines.extend((a, b))`
- `Path(".")` → `Path()`

The tryceratops findings are about exception-handling structure (`TRY300` "consider moving to else block", `TRY101` "too many try blocks", `TRY004` "prefer TypeError"). Worth fixing but lower-priority than the bug/security issues.

This isn't launch-blocking but it's noise that erodes the "MAXIMUM QUALITY" branding.

Confidence: verified by running tools.

## Proposed remediation

1. Run `refurb --quiet creek/` and apply the suggestions tool-by-tool. Group by category (FURB123, FURB138, FURB184, etc.) for review-friendly commits.
2. Address the 9 `TRY*` findings individually — some may be intentional and need `# noqa` justifications.
3. Add `refurb creek/` and `tryceratops creek/` to `scripts/lint.sh` (or a new `scripts/lint-extended.sh`) and to CI.
4. Bring the violation count to 0; gate CI to fail on any new violation.

## Acceptance criteria

- `refurb creek/` returns no findings.
- `tryceratops creek/` returns no findings (or each remaining one has a `# noqa: TRY...` with a justification comment).
- Both tools run in CI.
- A regression PR introducing a new `lambda x: x[1]` fails CI.

## References
- `creek-tools/.pre-commit-config.yaml`
- `.github/workflows/ci.yml`
- Refurb docs: <https://github.com/dosisod/refurb>
- Tryceratops docs: <https://github.com/guilatrova/tryceratops>
