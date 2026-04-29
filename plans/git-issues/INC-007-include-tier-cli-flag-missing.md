# INC-007: `--include-tier intimate` CLI flag does not exist

**Severity:** High
**Category:** INC
**Estimated complexity:** S (≤2h)
**Parallelizable with peers in same category:** partial — pairs with SEC-006 (filtering logic)
**Discovered by:** Dimension 5; confirmed by parallel agent

## Files affected
- `creek/cli.py:399-565` — `mine` and `draft` (no `--include-tier` flag)
- `creek-tools/docs/classification.md:100`
- `creek-tools/docs/generation.md:137`

## Dependencies
SEC-006 (the filter must exist for the flag to override). They should be implemented together.

## Blockers
A user wanting to deliberately include their own intimate material in a draft has no escape hatch.

## Reproduction
```bash
$ creek draft --vault ~/v --include-tier intimate
Usage: creek draft [OPTIONS]
Try 'creek draft --help' for help.
Error: No such option: --include-tier
```

## Analysis

Two doc strings advertise the flag:
- `docs/classification.md:100`: "Override with `--include-tier intimate` if you genuinely want intimate fragments fed to the LLM (this is logged in the audit trail)."
- `docs/generation.md:137`: "You can override with `--include-tier intimate` if you genuinely want intimate content in the prompt — the override is logged in the audit trail."

Neither command in `creek/cli.py` exposes the flag. The internal filtering helper in `creek/generate/voice.py` *does* take an `allow_intimate` parameter but it's not threaded up to the CLI.

Confidence: verified.

## Proposed remediation

Add `--include-tier {open,personal,intimate,all}` (Typer enum or `Literal[...]`) to:
- `creek mine`
- `creek draft`
- `creek report` (for `wavelength`, `synchronicity`, `unnamed`)
- `creek skills`

Default behaviour matches today's `voice.py` semantics: include open + personal-summary, exclude intimate.

When a tier higher than the default is requested, write an audit-log entry (per SEC-005 / SEC-006) capturing operator, command, fragments, timestamp.

## Acceptance criteria

- `creek mine --include-tier intimate --vault <vault>` lists candidate seeds including intimate fragments.
- An audit entry is written for that override.
- Without the flag, intimate fragments do not appear in mining output (covered by SEC-006).
- `--help` text mentions the flag and audit-log behaviour.

## References
- `creek-tools/docs/classification.md:100`
- `creek-tools/docs/generation.md:137`
- `creek/cli.py:399-565`
- SEC-006
