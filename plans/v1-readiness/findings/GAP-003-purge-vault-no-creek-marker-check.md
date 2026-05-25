# GAP-003 — `creek purge vault` does not verify that the target path is a Creek vault

- **Severity:** Critical
- **Prod-readiness criterion threatened:** data safety

## Evidence

`creek-tools/creek/cli.py:2687-2696` (interactive confirmation):

```python
typed = typer.prompt(
    f"Type the absolute vault path {expected!r} to continue",
    default="",
    show_default=False,
)
if typed.strip() != expected:
    return None
```

`expected` is `str(engine_vault_path.resolve())` — the path the engine
computed from `--vault`. The check verifies "did the user type back what
we printed", **not** "is this a Creek vault." If the user mistypes
`--vault` on the command line, `expected` is the mistyped path, and the
prompt echoes the mistake back for typed confirmation.

`creek-tools/creek/purge/engine.py:380-397` (`purge_vault`) begins
wiping `_VAULT_CONTENT_FOLDERS` (`01-Fragments` through `10-Liminal`,
defined at lines 37-41) immediately on confirmation, with no preflight
check for `00-Creek-Meta/`, `00-Creek-Meta/creek_config.yaml`, or any
distinctive marker placed by `creek init`.

A `find` and `grep` against `creek-tools/tests/test_purge.py` for any
test that passes a non-Creek directory to `purge_vault` returns nothing.

## Why it matters

The `--force-non-interactive` opt-out path (well-documented in
`docs/cleaning-and-purge.md:144-149`) is the obvious risk surface for
scripted misuse, and it requires both an explicit flag and a confirm-text
match. But the interactive path has a subtler hole: a `--vault` typo
silently propagates into the prompt, because the prompt is **derived
from** the typo. A user with `--vault ~/Obsidian/Creek-Vault` who
accidentally types `--vault ~/Obsidian/Creek-Old` sees a prompt for
`/Users/.../Creek-Old`, types it correctly (because that's what the
prompt asked for), and watches creek attempt to wipe `01-…` through
`10-…` inside that directory.

If `Creek-Old` happens to contain folders with those numeric prefixes
(perhaps an old vault snapshot, a backup, or a coincidentally-named
project directory), those folders are destroyed.

## Reproduction

```bash
# Create a directory that looks vault-ish but is not a Creek vault.
mkdir -p /tmp/gap003-not-a-vault/01-Fragments /tmp/gap003-not-a-vault/02-Threads
echo "important non-creek file" > /tmp/gap003-not-a-vault/01-Fragments/important.md

# No 00-Creek-Meta, no creek_config.yaml — this was never `creek init`-ed.
ls /tmp/gap003-not-a-vault/00-Creek-Meta 2>&1   # not found

# `creek purge vault` proceeds anyway, prompting for the absolute path:
creek purge vault --vault /tmp/gap003-not-a-vault
#  Type the absolute vault path '/tmp/gap003-not-a-vault' to continue: /tmp/gap003-not-a-vault
#  [success]  — 01-Fragments/important.md is gone.

# Expected post-fix:
#  Error: '/tmp/gap003-not-a-vault' does not appear to be a Creek vault
#  (no 00-Creek-Meta/creek_config.yaml found). Aborting.
```

Failing-test outline:

```python
def test_purge_vault_refuses_non_creek_directory(tmp_path):
    decoy = tmp_path / "not-a-vault"
    (decoy / "01-Fragments").mkdir(parents=True)
    (decoy / "01-Fragments" / "x.md").write_text("hello")
    engine = PurgeEngine(vault_path=decoy, confirmation=VAULT_PURGE_CONFIRMATION)
    with pytest.raises(ValueError, match="not appear to be a Creek vault"):
        engine.purge_vault(VAULT_PURGE_CONFIRMATION)
    assert (decoy / "01-Fragments" / "x.md").exists()
```

## Acceptance criteria

Closed when:

1. `purge_vault` checks for a vault marker (recommend
   `<vault>/00-Creek-Meta/creek_config.yaml`, since that file is
   created by `creek init` and survives every other purge operation)
   **before** writing the intent audit line and **before** the wipe
   loop.
2. If the marker is absent, the engine raises a clear error naming the
   marker file it looked for, and the CLI surfaces it as a typer.Exit
   with a non-zero code and an actionable message.
3. The check is byte-for-byte identical between interactive and
   `--force-non-interactive` paths — no carve-out.
4. New tests cover: (a) non-Creek directory rejected; (b) a directory
   that has `00-Creek-Meta/` but is missing `creek_config.yaml`
   rejected; (c) a freshly `creek init`-ed vault accepted.
5. `docs/cleaning-and-purge.md` updates the `creek purge vault` section
   to name the marker check.

## Files affected

- `creek-tools/creek/purge/engine.py`
- `creek-tools/creek/cli.py` (only if the marker-not-found error needs
  a typer-specific exit code path)
- `creek-tools/tests/test_purge.py`
- `creek-tools/docs/cleaning-and-purge.md`

## Dependencies / blockers

None. This is a small, self-contained change with high data-safety
payoff and no API surface impact.
