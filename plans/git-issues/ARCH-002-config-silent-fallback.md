# ARCH-002: `load_config` silently returns defaults when `creek_config.yaml` is missing

**Severity:** Medium
**Category:** ARCH
**Estimated complexity:** S (≤2h)
**Parallelizable with peers in same category:** yes
**Discovered by:** Dimension 3 — confirmed by parallel agent

## Files affected
- `creek/config.py:498-519` — `load_config`

## Dependencies
None.

## Reproduction
```bash
creek process --source /tmp/foo --vault /tmp/missing-config-vault
# runs without ever telling the user that creek_config.yaml wasn't found
```

## Analysis

`load_config` reads `<vault>/00-Creek-Meta/creek_config.yaml`. If the file is missing, it falls through to a default `CreekConfig()` with no log message and no warning. The user might be running with the wrong defaults for hours before noticing — particularly likely if they edited the config in a *different* vault than the one they're processing.

For a system that emphasises "your data sovereignty depends on the config you wrote," silent fallback is the wrong default.

Confidence: verified.

## Proposed remediation

In `load_config`:
- If the YAML file doesn't exist and the user is running anything other than `creek init` (which doesn't exist yet — see INC-014 below), log `WARNING` with the resolved path and a hint.
- Add a `--config <path>` CLI flag for explicitness.
- Add `creek init --vault <vault>` that writes a starter `creek_config.yaml` (existing `generate_default_config` is the obvious helper).

Make `creek process` refuse to run against a vault with no config file unless `--accept-defaults` is passed (with the warning logged).

## Acceptance criteria

- Running any command in a vault with no config logs a clearly-worded warning.
- `creek init --vault <vault>` creates a starter config.
- `creek process --accept-defaults` is required to run without a config; without it, the command errors out telling the user how to fix.
- Tests cover both paths.

## References
- `creek/config.py:498-519, 522` (`generate_default_config`)
- `creek-tools/docs/getting-started.md` "Initialize the vault"
