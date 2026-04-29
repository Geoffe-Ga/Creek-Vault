# OPS-002: `creek purge vault` interactive prompt is bypassable via piped stdin

**Severity:** High
**Category:** OPS
**Estimated complexity:** S (≤2h)
**Parallelizable with peers in same category:** yes
**Discovered by:** Dimension 7; confirmed by parallel agent

## Files affected
- `creek/cli.py:885-916` — `purge_vault` command

## Dependencies
None.

## Blockers
None.

## Reproduction
```bash
echo "I understand this is irreversible" | creek purge vault --vault ~/v --yes
# proceeds without an interactive operator
```

The README calls `purge vault` "the nuclear option" and claims it requires `--yes` *and* an interactive prompt. `typer.prompt()` reads from stdin; piping satisfies it.

## Analysis

`README.md` line 88:
> Nuclear option: destroy every fragment, thread, and eddy. Asks for explicit confirmation.

`docs/cleaning-and-purge.md:113`:
> This is **never** undoable; it requires `--yes` *and* an interactive prompt.

Both claims rely on the prompt being a barrier. It isn't — any caller (including a misbehaving cron job, a hooked git pre-commit, or an adversary with shell access) can satisfy it programmatically.

Confidence: verified.

## Proposed remediation

For `purge vault` specifically, refuse non-interactive use. Detect with `sys.stdin.isatty()`:

```python
if not sys.stdin.isatty():
    raise typer.Exit("Refusing to purge vault from a non-interactive session.")
```

Pair with a deliberate `--force-non-interactive` escape hatch for users who really mean it (e.g., test fixtures), with a loud warning logged.

Also: make the confirmation phrase non-trivial — require the user to type the vault path or the literal string "yes I really mean it" rather than pressing Enter. Defence in depth.

## Acceptance criteria

- `echo y | creek purge vault --yes --vault <v>` exits non-zero with a clear message.
- `creek purge vault --yes --force-non-interactive --vault <v>` works (and logs a warning).
- An interactive run with the wrong confirmation phrase aborts.
- Tests cover all three paths.

## References
- `creek-tools/README.md:88`
- `creek-tools/docs/cleaning-and-purge.md:113`
- `creek/cli.py:885-916`
