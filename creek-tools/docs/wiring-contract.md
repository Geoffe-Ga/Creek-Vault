# The wiring contract

`tests/test_wiring_contract.py` proves that every CLI command and every MCP
tool is **reachable and effectful**. It exists because seventeen issues in this
repo share one shape: the surface exists, the implementation exists, the wire
between them does not — and every one of them exited `0` with thousands of
tests green (#935, #649, #658, #460, #506, #580, #581, #577, #821, #1040,
#1231). Epic #1024 tracks the family; this module is the gate.

Run it with `./scripts/test.sh --integration`. It is blocking in CI.

## What the module guarantees

| Guarantee | Test |
|---|---|
| Every registered CLI command is contracted or exempt, both directions | `test_every_cli_command_is_declared` |
| Every registered MCP tool is contracted or exempt, both directions | `test_every_mcp_tool_is_declared` |
| Every value-dispatched mode (`--type`, `--method`, `--target`, `--mode`) is declared | `test_every_cli_mode_variant_is_declared` |
| `creek report`'s own valid-list equals the derived set | `test_report_error_message_lists_exactly_the_declared_types` |
| CLI and MCP expose the same link methods / report types | `test_cli_and_mcp_agree_on_*` (currently strict `xfail` — see *Known drift*) |
| Every surface produces its declared observable | `test_cli_surface_produces_its_declared_effect`, `test_mcp_surface_produces_its_declared_effect` |
| Every printed judgement vanishes in its declared contrast | `test_declared_contrast_makes_the_observable_vanish` |
| Every gate refuses with its documented reason and changes nothing | `test_declared_gate_refuses_and_leaves_the_vault_untouched` |
| The harness can still say no | the *Guarding the guard* block |

Both inventories are **derived from the live objects** — the `click` tree Typer
builds, and `await server.list_tools()`. Never retype a list of names: that is
the drift defect this epic exists to kill, and it is live in production today
(see *Known drift*).

> Note: `app.registered_commands` is the wrong door. Every command under
> `clean` / `purge` / `skills` / `compost` lives in `app.registered_groups`, so
> that attribute sees 23 of the 37 surfaces. `cli_surface_names()` walks the
> resolved `click` tree instead.

## Adding a surface

Add one entry to `CLI_CONTRACT` or `MCP_CONTRACT`. The inventory test fails
until you do, and its message says so.

```python
"lint": Surface(
    shape=Shape.WRITES,
    why="#1040: the draft-grounding check reported every vault clean while dormant",
    derived_from=("creek.lint.runner:LintRunner",),
    argv=("--vault", "{vault}"),
    effect=Effect(
        writes=("00-Creek-Meta/Processing-Log/lint-*.md",),
        contains=("draft-grounding", "broken-links"),
    ),
),
```

Field by field:

- **`why`** — which of the wiring bugs an entry of this shape would have
  caught. Must cite an issue number; a reviewer should be able to ask "which
  bug?" and get an answer. Enforced by
  `test_every_entry_says_which_wiring_bug_it_would_have_caught`.
- **`derived_from`** — `"module:symbol"` for every production constant the
  expectation is derived from. Each must import, so a citation cannot rot into
  a comment. Enforced by `test_derived_from_names_importable_production_symbols`.
- **`effect`** — the named observable. Pick the strongest field that applies:

  | Field | Means | Negative control |
  |---|---|---|
  | `writes` | glob patterns that must match nothing before and something after | free — "not run" |
  | `removes` | glob patterns that must match before and nothing after | free — "not run" |
  | `contains` | substrings in the union of the written files | free, via `writes` |
  | `frontmatter` + `frontmatter_key` | an exact `{fragment id: value}` map read off disk | free — spans three tiers, so a fail-closed default cannot pass |
  | `prints` / `absent` | substrings that must / must not appear in the output | **you must declare a `Contrast`** |

- **`contrast`** — required whenever the observable is printed. A print-only
  scanner renders "judged clean" and "found nothing to judge" identically, so a
  bare substring assertion is passed by a hardcoded banner. The contrast names
  a second world — usually the unseeded vault, sometimes a different flag or a
  different stdin — in which the same substrings must be **absent**. Enforced
  by `test_printed_effects_declare_a_contrast`.
- **`refusal`** — the closed-gate half of a two-sided gate proof: the
  documented reason appears *and* the vault is byte-identical. The entry's
  primary invocation supplies the open-gate half. One half alone is passed by a
  surface that refuses everything.
- **`strip`** — vault-relative globs the fixture deletes before the run, for a
  surface whose artefacts `creek init` already deploys (`skills sync`).
- **`prepare`** — CLI invocations to run first, for chained preconditions
  (`state-budget` needs `state`).

### Two rules that are easy to get wrong

**"A file appeared" is never enough.** Every MCP tool appends
`00-Creek-Meta/audit/mcp.jsonl` — including all five `purge.*` tools *while
refusing*. `_BOOKKEEPING_GLOBS` names those paths, and
`test_no_effect_is_satisfied_by_bookkeeping_alone` fails any entry whose
declared artefacts live entirely inside them. Name the feature's own output.

**Where a feature has a downstream reader, assert the key that reader looks
for.** This is the rule that catches #1040. `creek.draft` wrote a file, exited
`0`, and omitted `derivative_score` — so the `draft-grounding` lint check's
"scores absent → clean" branch called every vault spotless. `writes` alone
passes in both the dormant and the live world; `contains=(DERIVATIVE_FRONTMATTER_KEY,)`,
imported from `creek.generate.grounding`, does not.

## Adding an exemption

Some effects genuinely cannot be proven hermetically: a provider key, a live
OAuth flow, a Discord gateway. Those go in `EXEMPTIONS` — but a silent
exemption is the same defect one level up, so **every exemption carries an
executable staleness probe** and must declare exactly one of:

- **`blocker`** — a substring the surface must still emit (`"LLM provider
  unavailable; cannot generate draft"`). When the blocker stops appearing the
  surface has become provable and the exemption fails.
- **`delegate`** — `"tests/module.py:needle"`, a suite that already asserts
  this effect. The module must exist and must mention the needle.
- **`seam`** — `"module:name"` or `"module:callable:parameter"`, for a blocker
  that *is* the absence of an injection point. The exemption fails the day
  someone adds the seam.

The allowlist only ever shrinks, exactly as `DORMANT_CONFIG_FIELDS` does in
`tests/test_config_contract.py`.

## Known drift (strict xfails)

Two parity assertions describe real, filed drift, and are the argument for
deriving rather than retyping. They are strict `xfail`s: the day either is
fixed, the test fails and the marker must come off.

1. **`creek.link` cannot reach the threads linker.**
   `creek_mcp.tools.link._VALID_METHODS` is a retyped copy of
   `creek.cli._LINK_METHODS` that lost `"threads"`. An MCP caller asking for it
   gets *"unknown method 'threads'"*.
2. **`creek.report` exposes 6 of 11 report types.** `unnamed`, `fingerprint`,
   `paradox`, `synchronicity` and `wavelength` have no route over MCP.

By contrast `creek.ingest` (derives from `INGESTOR_REGISTRY`) and `creek.save`
(derives from the `SaveTarget` enum) have no drift at all. That is the whole
argument in one line.

## The fixture

Vaults are built by the real `creek init` code path, so the contract runs
against the tree a user actually gets — marker file, starter config and all.
`_seed_corpus` then plants five fragments spanning three privacy tiers, a
fragment with a broken wiki-link, a duplicate, an aged orphan, a stale review
queue file, and a staging tree holding both seeded PII and an ingestable note.
Every one of those is bait for a specific entry; none of it is decoration.

The corpus is deliberately multi-valued. The rules classifier fails **closed**,
so a `{id: tier}` map whose every value is `intimate` is what a *broken* wire
also produces, and a map of all `unclassified` is exactly what #935 shipped.
Only a multi-valued map tells a live classifier from a default.
