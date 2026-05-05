# INC-011: Documented `--rebuild` (link) and `--force` (classify) flags do not exist

**Severity:** Medium
**Category:** INC
**Estimated complexity:** S (≤2h)
**Parallelizable with peers in same category:** yes
**Discovered by:** Dimension 5

## Files affected
- `creek/cli.py:174, 187` — `classify` and `link` (both stubs today)
- `creek-tools/docs/linking.md:98` — claim `--rebuild`
- `creek-tools/docs/classification.md:71, 107` — claim `--force`

## Dependencies
INC-001 (the underlying commands are stubs). When INC-001 is repaired, add these flags.

## Blockers
None. Pair with INC-001.

## Reproduction
```bash
$ creek classify --vault ~/v --method rules --force
Usage: ...
Error: No such option: --force
$ creek link --vault ~/v --method embeddings --rebuild
Usage: ...
Error: No such option: --rebuild
```

## Analysis

`docs/classification.md:71`:
> `creek classify` will not overwrite a `method: manual` field unless you pass `--force`.

`docs/classification.md:107`:
> Edit `06-Frequencies/_keyword_atlas.yaml`, then re-run with `--force` so the rule classifier overwrites the prior decisions.

`docs/linking.md:98`:
> `creek link --vault ... --method embeddings --rebuild`

Neither flag exists. INC-001 leaves the commands as stubs anyway. When implementing the real commands, these need to be on the option list with the documented semantics:
- `classify --force`: ignore `method: manual`; overwrite anyway.
- `link --rebuild`: invalidate the embeddings cache (see INC-006) and recompute from scratch.

## Proposed remediation

Add the flags during INC-001 implementation:
- `classify(--force: bool = False)` — when True, skip the manual-preservation guard.
- `link(--rebuild: bool = False)` — when True, delete the embeddings cache (per INC-006) and recompute.

## Acceptance criteria

- `creek classify --force` overwrites manual classifications.
- `creek classify` without `--force` preserves them.
- `creek link --rebuild` recomputes embeddings even when the cache is fresh.
- `--help` mentions both flags.
- Tests exercise both code paths.

## References
- `creek-tools/docs/classification.md:71, 107`
- `creek-tools/docs/linking.md:98`
- INC-001, INC-006
