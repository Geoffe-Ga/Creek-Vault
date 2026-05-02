# INC-008: `creek purge source --match substring` not implemented

**Severity:** Medium
**Category:** INC
**Estimated complexity:** S (≤2h)
**Parallelizable with peers in same category:** yes
**Discovered by:** Dimension 5; confirmed by parallel agent

## Files affected
- `creek/cli.py:800-824` — `purge_source`
- `creek/purge/engine.py:purge_source` — engine method
- `creek-tools/docs/cleaning-and-purge.md:92`

## Dependencies
None.

## Reproduction
```bash
$ creek purge source --source-path /home/me/exports/foo --vault ~/v --match substring
Usage: creek purge source [OPTIONS]
Error: No such option: --match
```

## Analysis

`docs/cleaning-and-purge.md:92`:
> The source path is matched against `source.original_file` in each fragment's frontmatter — exact match by default, or substring with `--match substring`.

The CLI has only `source_type`, `vault`, `dry_run`, `yes`. No `--match` flag, no `--source-path` for that matter. Even the parameter name in the CLI is wrong if the docs are taken at face value.

Confidence: verified.

## Proposed remediation

Add `--source-path` (or `--source`) and `--match {exact,substring,regex}` to `creek purge source`. Default `exact`. Plumb through to `PurgeEngine.purge_source` which currently filters on platform string equality. Add tests for each match mode.

## Acceptance criteria

- `creek purge source --source-path foo --match substring --vault <vault>` deletes every fragment whose `source.original_file` contains "foo".
- `--match exact` (default) requires full-string equality.
- `--match regex` supports a regex; bad regex fails fast.
- Audit entry records the match mode used.

## References
- `creek-tools/docs/cleaning-and-purge.md:84-92`
- `creek/cli.py:800-824`
- `creek/purge/engine.py`
