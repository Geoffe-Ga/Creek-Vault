# Troubleshooting: Creek CLI in this Repository

## CLI command not found

Symptoms:
- `creek: command not found`

Actions:
1. Run `pip install -e ./creek-tools`
2. Verify with `creek --help`
3. If virtualenv mismatch, run `python -m pip install -e ./creek-tools`

## Command succeeds but produced no useful output

Actions:
1. Check vault path argument and permissions.
2. Confirm expected source files exist in the vault.
3. Re-run with narrower scope flags (where supported) for diagnostics.

## Query quality appears weak

Actions:
1. Ensure compiled pages exist (`creek compile`).
2. Re-run query without bypassing compiled layer.
3. Run `creek lint` and inspect data gaps/contradictions.

## Unexpected contradictions

Actions:
1. Do not reconcile manually in place.
2. Route contradiction to paradox/liminal flow per schema skills.
3. Re-run compile/lint after adding contradiction artifact.

## Save behavior and privacy concerns

Actions:
1. Verify intended destination type (thread/eddy/praxis/paradox/etc.).
2. Confirm tier handling for personal/intimate content.
3. Never down-tier implicitly; require explicit user intent.
