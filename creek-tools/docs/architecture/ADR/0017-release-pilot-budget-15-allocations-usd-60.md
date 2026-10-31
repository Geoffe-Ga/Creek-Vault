# ADR-0017: The managed-vault release pilot admits 15 live allocations and alerts at USD 60/month

- **Status**: Accepted (owner decision R2-3, 2026-10-27)
- **Date**: 2026-10-31
- **Decider**: Geoff Gallinger, repository owner, acting as the managed-vault
  operations owner. The decision is copied into adepthood as R2-3 in
  [`prompts/claude-comms/2026-10-27-release-decisions.md`](https://github.com/Geoffe-Ga/adepthood/blob/main/prompts/claude-comms/2026-10-27-release-decisions.md)
  (line 51): "Release cap: 15 managed vaults, with an alert at USD
  60/month." An agent wrote this record down; it decided nothing.
- **Driving issue**: #1998, implemented by #2017 (`cd0bdead`).
- **Amends**:
  - [ADR-0013](0013-demand-provisioned-vault-lifecycle.md) Decision 4 and
    Decision 7. The text is unchanged. This record supplies the figure that
    Decision 4's "operator-set monthly budget" alert and Decision 7's
    "approved fleet budget" review trigger refer to: USD 60/month for the
    release pilot.
  - [ADR-0016](0016-shared-confidential-warm-inference-pool.md) Context and
    Options, in the pilot figures only. These are the five-allocation and USD
    25/month pilot at lines 138–139, the "× the pilot cap" basis at line 271,
    the multiples in the O2, O3 and O6 rows at lines 277, 278 and 281, and
    the "two orders of magnitude" reading at line 284. They also cover the
    "pilot's USD 25/month alert" in Decision 4 at line 817 and the
    Consequences multiples at line 967. ADR-0016's pool decision is **not**
    reopened (see Decision 3).

## Context

The bounded Fly pilot was prepared in #1817 (`d1c92371`) with five live
allocations and a USD 25 monthly alert. Before #2017 those figures were pinned
in code and in the runbook:

- `creek_mcp/provisioning/pilot_cli.py:43` read
  `_MAX_LIVE_ALLOCATIONS: Final[int] = 5`. The parser accepted only
  `choices=(5,)`, and `compose` refused any other value with
  "--maximum-live-allocations must equal five for the pilot".
- `creek_mcp/provisioning/fly_pilot_deploy.py:43` read
  `_PILOT_SPEND_CAP_USD_CENTS: Final[int] = 2500`.
- `creek_mcp/provisioning/pilot_evidence.py` bounded
  `max_live_allocations` with `le=5` and pinned
  `monthly_alert_usd_cents: Literal[2500]`.
- `docs/managed-vault-fly-pilot.md` line 10 read "bounded to five live
  allocations and a USD 25 monthly alert". Line 257 set
  `monthly_budget = 25.00`.
- The admission count was
  `SELECT COUNT(*) FROM provisioning_jobs WHERE state != 'deleted'`, so a
  failed delete or an in-flight `deleting` job also held a slot.

ADR-0013 sets no figure of its own. Decision 4 says only that the control plane
alerts on "departure from an operator-set monthly budget". Decision 7 says the
one-app/one-Machine driver is reviewed after "three rolling months above the
approved fleet budget". ADR-0016 used the USD 25 alert as its cost yardstick
for the warm pool options. It used five allocations at adepthood's
`DEFAULT_MONTHLY_CAP = 20` to set the pilot's reflection ceiling at 100 a
month.

On 2026-10-27 the owner raised the release cap and alert (R2-3). #2017 has
already landed it in code. This record states the figures and their
semantics, and recomputes the ADR-0016 figures that depended on the old ones.
Accepted ADRs are not rewritten.

## Decision

### Decision 1. The release cap is 15 live allocations

The managed-vault release admits at most **15 live allocations**. Operators may
run a tighter cap. Nothing may run a looser one. In code on `origin/main`
after PR #2017:

- `creek_mcp/provisioning/pilot_cli.py:43-45` pins the cap:

  ```python
  # Owner decision R2-3: the managed-vault release admits at most fifteen live
  # vaults. Operators may run tighter; the parser refuses anything looser.
  _RELEASE_MAX_LIVE_ALLOCATIONS: Final[int] = 15
  ```

- `pilot_cli.py:226-229` makes it the `--maximum-live-allocations` default.
  `pilot_cli.py:523-530` (`_release_bounded_allocations`) refuses zero,
  negative values and any value above 15.
- `creek_mcp/provisioning/pilot_evidence.py:128` records
  `max_live_allocations: Annotated[int, Field(ge=1, le=15)]`.

The generic control-plane CLI keeps its own default of five
(`creek_mcp/provisioning/cli.py:36`, `default=5`;
`ActivationAdmissionPolicy.maximum_live_allocations: int = 5` at
`creek_mcp/provisioning/store.py:188`). That default is not the release cap,
and this record does not change it.

### Decision 2. Only live allocations count against the cap

A job holds an admission slot while it holds, or is acquiring, a provider
allocation. `creek_mcp/provisioning/store.py:51-71` defines the count:

```python
LIVE_JOB_STATES: Final[tuple[JobState, ...]] = (
    JobState.PENDING,
    JobState.PROVISIONING,
    JobState.AWAITING_KEY_CEREMONY,
    JobState.READY,
)
...
_ADMISSION_COUNT_SQL: Final[str] = (
    "SELECT COUNT(*) FROM provisioning_jobs "
    "WHERE state IN (?, ?, ?, ?) OR (state = ? AND operation = ?)"
)
```

The final pair binds `failed` and `create`. A failed create keeps its slot
because a post-provision handoff failure leaves its Machine running, and
`retry` returns it to `pending` without a second admission check. A
`deleting` job, a failed delete and a `deleted` job free their slots. The
admission check at `store.py:449-453` refuses a new activation when the count
reaches `maximum_live_allocations`. Reconcile shares the same predicate:
`creek_mcp/provisioning/reconcile.py:61` builds `_LIVE_STATES` from
`LIVE_JOB_STATES`.

Spend from a deletion the provider has not yet confirmed is therefore outside
the cap. The USD 60 alert and the `stuck_deletion` report guard it
(`docs/managed-vault-fly-pilot.md` lines 17–24).

### Decision 3. The monthly alert is USD 60.00

The fleet alert threshold is **USD 60.00 a month**. It replaces USD 25.00.
Fly supplies no hard billing limit, so this remains an alert, not a cap:

- `creek_mcp/provisioning/fly_pilot_deploy.py:43-44` pins the spend
  authorization:

  ```python
  # Owner decision R2-3: alert at USD 60/month; the authorization must match.
  _PILOT_SPEND_CAP_USD_CENTS: Final[int] = 6000
  ```

  `fly_pilot_deploy.py:126` refuses an authorization record whose
  `max_spend_usd_cents` differs.
- `creek_mcp/provisioning/pilot_evidence.py:130` records
  `monthly_alert_usd_cents: Literal[6000]`.
- `creek_mcp/provisioning/budget.py:575` raises `monthly_budget_departure`
  when `estimate.estimated_month >= policy.monthly_budget`. The runbook's
  policy file sets `monthly_budget = 60.00`
  (`docs/managed-vault-fly-pilot.md` line 269).

For scale, use ADR-0013 Decision 4's reference model. Fifteen vaults cost
about USD 15.52 a month fully stopped (15 × 0.90 + 2.02 for the control
plane). They cost USD 19.12 at one running hour a day and USD 29.92 at four
running hours a day. Continuously started, they cost USD 102.07. USD 60
therefore covers the cap at ordinary use and alerts on sustained always-on
compute. Under the old figures, five continuously started vaults (USD 35.37)
also crossed USD 25.

### Decision 4. ADR-0016's pool decision stands; its pilot multiples are recomputed

ADR-0016's "× the pilot cap" divides a fixed monthly warm minimum by the
pilot alert, with EUR at par (line 271). Divided by USD 60 instead of USD 25:

| Option (ADR-0016 line) | Warm minimum per month | ADR-0016 (÷ USD 25) | This record (÷ USD 60) |
|---|---|---|---|
| **O2** Fly `performance-8x` / `performance-16x` (277) | USD 264.01 / 528.02 | 11–21× (10.56 / 21.12) | **4.4–8.8×** (4.40 / 8.80) |
| **O3** RunPod L40S / A100; Hetzner GEX63 / GEX131-1 (278) | USD 796 / 1,307; EUR 999 / 1,197.30 | 32–52× (31.84 / 52.28; EUR 39.96 / 47.89) | **13–22×** (13.27 / 21.78; EUR 16.65 / 19.96) |
| **O6** Phala H100 TEE / Azure `NCC40ads_H100_v5` (281) | USD 2,248 / 5,095 | 90–204× (89.92 / 203.80) | **37–85×** (37.47 / 84.92) |

O2 at Fly's 730-hour basis (USD 268 and 535, line 270) is 4.5–8.9×. Line 967
becomes "13–22 times the pilot cap (4.4–8.8 times for the O2 variant)". Line
284's "two orders of magnitude" for O6 becomes "between one and two orders of
magnitude", 37–85×.

The pilot reflection ceiling also rises. Line 139's "at most 100 reflections a
month" becomes **at most 300** (15 live allocations × `DEFAULT_MONTHLY_CAP`
20). The table's pilot column divides a fixed warm minimum by that volume. At
300 a month, the cost per 1,000 reflections becomes:

| Option | ADR-0016 (100/month) | This record (300/month) |
|---|---|---|
| **O2** | USD 2,640–5,280 | USD 880–1,760 |
| **O3** | USD 7,960–13,070 or EUR 9,990–11,973 | USD 2,653–4,357 or EUR 3,330–3,991 |
| **O6** | USD 22,480–50,950 | USD 7,493–16,983 |

The 10,000-a-month column is unchanged. So are the metered options O4 (USD
0.24) and O5 (EUR 1.29 or USD 0.45 per 1,000), whose cost per reflection does
not depend on volume. At the new ceiling, O5's inference line is at most
about EUR 0.39 a month (300 × EUR 1.29 / 1,000).

**The pool decision is not reopened.** Every fixed-minimum option ADR-0016
did not select still costs a multiple of the new budget:

- O2's cheapest variant is USD 264.01, 4.4× USD 60.
- O3's cheapest is USD 796, 13×.
- O6's cheapest is USD 2,248, 37×.

None fits inside USD 60, so the economics that ADR-0016 Decision 4 put before
the owner keep their direction. The decision itself rested on the owner's Q1
and Q2 answers, not on the size of the multiple. O1 and O4 were rejected on
grounds that have nothing to do with the budget: O1 by the owner on
2026-10-16, O4 by adepthood ADR 0009 item 3. ADR-0016 Decision 4's sentence
at line 817 still holds with the figure changed. The pilot's USD 60/month
alert is still a storage-era number, and a warm Creek-run minimum would be a
separate, owner-authorized line.

## Consequences

- The release can host three times as many writers as the five-vault pilot
  before admission refuses a new activation. Admission refusal stays
  `503 activation_unavailable`. Status, routing, retry, export and deletion
  for existing allocations are unaffected.
- Failed and in-flight deletions no longer lock out new writers. Unconfirmed
  deletion spend is now visible only through the USD 60 alert and the
  `stuck_deletion` report.
- ADR-0013 Decision 7's "three rolling months above the approved fleet budget"
  trigger is now measured against USD 60/month.
- Readers of ADR-0016 should apply this record's multiples and pilot-column
  figures in place of the USD 25 and 100-reflection figures. ADR-0016's text
  stays as accepted.
- `creek_mcp/bench/cost.py:92` still defaults
  `PILOT_REFLECTIONS_PER_MONTH` to 100, with the docstring "ADR-0016's pilot
  ceiling: five live allocations at the default allowance". Bench volumes can
  be overridden by flag. This docs-only record does not change the default.
  Pass 300 to price the release ceiling.
- Raising either figure again needs a new owner decision and a record that
  amends this one. #2017's tests pin both figures in code.
