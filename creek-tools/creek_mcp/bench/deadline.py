"""The deadlines a reflection must fit, and the verdicts on whether it does.

**The one budget this process enforces, derived, never restated.** A
``POST /v1/reflections`` is
shed by whichever fires first: the HTTP server's request deadline
(:data:`creek_mcp.httpapi.middleware.limits.DEFAULT_TIMEOUT_SECONDS`) or the
Ollama provider's request timeout
(:attr:`creek.classify.llm.providers.OllamaProvider.REQUEST_TIMEOUT`).
Grounding, embedding and generation all spend from that one budget — the model
has no separate allowance — so the harness judges the *whole* reflection
against the tighter of the two. Both are read through their module or class
at call time, so a change to either moves the verdict with it and a test can
patch them.

The Adepthood client imposes its own total deadline on the same call (per-try
timeout times attempts in ``creek_vault_client.py``). That lives in another
repository and is documented here rather than imported; the server-side budget
above is the one this process enforces.

Splitting the budget into per-stage allowances is an owner decision (the
sync-versus-async protocol question in D04), not something this harness
enacts. What it does provide is the evidence: each trial records the model
call's share of the latency separately (``Trial.generation_s``).

**ADR-0016 R2's named budget table** (#1895) sits beside that one enforced
budget. R2 states four budgets, and the harness reports each as its own row
with its own limit, measured value and verdict rather than collapsing them
into a single pass. Two properties keep the table honest:

- **Every budget says where it came from.** A budget this repository's code
  states is read through its module or class at call time
  (:data:`BudgetSource.CODE`), so a change to the code moves the verdict. A
  budget stated by code in *another* repository is a documented constant here
  (:data:`BudgetSource.DOCUMENTED_CONSTANT`), the way this module already
  documents adepthood's client deadline rather than importing it. A budget
  only the ADR states is :data:`BudgetSource.ADR_0016_R2`.
- **A budget production does not enforce today is reported out of force.** R2
  names its own prerequisite: adepthood must widen ``_post_reflection``'s
  phase budgets before the 25 s vault budget means anything, and until it does
  the binding ceiling is the 10 s first-byte window. So
  :attr:`BudgetRow.in_force` is false on the 25 s row, the 10 s row carries
  the table's verdict, and :attr:`BudgetTable.binding_budget` names which one
  production actually enforces. A green 25 s row cannot be read as
  "production is fine".

**No run this harness can execute today is a production run.** Every figure in
the table describes the backend the run actually drove, and none of the
backends it can drive is the production path. A fake or loopback run measures
the harness's own floor on the harness's own host. A run against a private or
remote Ollama measures somebody's dev box, which is why an endpoint scope
cannot stand in for this: ``bench --mode live`` against a LAN address reaches a
model off this host and still dials no pool, wakes no Fly Machine and includes
no attestation round trip. The live pool run that would is #1899, and ADR-0016
Decision 4 authorizes no spend for it.

So **no row here may be quoted as a production guarantee, whatever the endpoint
scope was.** :attr:`BudgetTable.model_off_harness_host` records only the narrow
fact its name states — whether the measured model ran somewhere other than the
host whose capacity the report describes — and licenses nothing beyond it. It
is the exact complement of ``host.describes_model_host``, taken from that field
rather than re-derived, so the two can never disagree.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Final

from pydantic import BaseModel, ConfigDict, NonNegativeFloat, PositiveFloat, PositiveInt

from creek.classify.llm import providers
from creek_mcp.bench.stats import LatencySummary
from creek_mcp.bench.trial import Phase, Sweep, Trial
from creek_mcp.bench.usage import TokenBasis
from creek_mcp.httpapi.middleware import limits

if TYPE_CHECKING:
    from collections.abc import Sequence

MAX_FIT_ERROR_RATE: Final[float] = 0.0
"""The error rate an envelope may show and still fit: none.

A trial that ended in timeout, out-of-memory, disk-full or an unavailable
model is a reflection a user did not get, however fast it failed.
"""


class Verdict(StrEnum):
    """Whether a group of trials fits the deadline.

    ``insufficient_data`` means nothing was measured at all; trials that ran
    and failed are ``exceeds``.
    """

    FITS = "fits"
    EXCEEDS = "exceeds"
    INSUFFICIENT_DATA = "insufficient_data"


def deadline_budget_seconds() -> float:
    """Return the seconds one reflection may take before it is shed.

    Returns:
        The tighter of the server request deadline and the Ollama request
        timeout, read at call time.
    """
    return min(limits.DEFAULT_TIMEOUT_SECONDS, providers.OllamaProvider.REQUEST_TIMEOUT)


def deadline_verdict(summary: LatencySummary, budget: float | None = None) -> Verdict:
    """Judge *summary* against the deadline.

    Args:
        summary: Aggregates over the trials being judged.
        budget: Seconds allowed; defaults to :func:`deadline_budget_seconds`.

    Returns:
        ``exceeds`` when any trial failed (including when every trial did, so
        no p95 exists) or p95 is over the budget; otherwise ``fits``. The
        budget is inclusive: a p95 exactly at it fits. A summary always holds
        at least one trial, so ``insufficient_data`` is never returned here;
        it is reserved for a report that measured nothing.
    """
    limit = deadline_budget_seconds() if budget is None else budget
    if summary.error_rate > MAX_FIT_ERROR_RATE or summary.p95_s is None:
        return Verdict.EXCEEDS
    if summary.p95_s > limit:
        return Verdict.EXCEEDS
    return Verdict.FITS


def overall_verdict(verdicts: Sequence[Verdict]) -> Verdict:
    """Combine verdicts; any failure dominates, then any gap.

    Used to roll phases up into a sweep, sweeps up into a report, and the
    in-force budget rows up into :attr:`BudgetTable.verdict`. An empty
    sequence measured nothing, so it is a gap rather than a pass.
    """
    if Verdict.EXCEEDS in verdicts:
        return Verdict.EXCEEDS
    if not verdicts or Verdict.INSUFFICIENT_DATA in verdicts:
        return Verdict.INSUFFICIENT_DATA
    return Verdict.FITS


ADEPTHOOD_FIRST_BYTE_WINDOW_SECONDS: Final[float] = 10.0
"""The first-byte window adepthood's pooled client enforces today.

``_post_reflection`` runs on the pooled client whose ``httpx.Timeout`` ``read``
phase is ``_VAULT_TIMEOUT_SECONDS = 10.0``
(``backend/src/services/creek_vault_client.py``): a vault that sends no
response byte for 10 s is a ``VaultCallTimedOutError`` whatever the 30 s total
deadline says, and ``/v1/reflections`` is not streamed. Machine wake is inside
this window, not beside it.

A documented constant rather than an import, like the client deadline this
module already documents: it is another repository's code. **It is what binds
until #1903 lands adepthood's widening**, which is why
:data:`ADEPTHOOD_PHASE_BUDGETS_WIDENED` exists.
"""

_ADEPTHOOD_VAULT_DEADLINE_PHASES: Final[int] = 3
"""Timeout phases adepthood's total vault deadline is composed of.

``_VAULT_DEADLINE_PHASE_BUDGETS = 3`` in ``creek_vault_client.py``: pool
acquisition, connect and read, each allowed the per-phase window above.

It is a **phase** count, not a retry count. R2 states no retries, and reading
this 3 as attempts would assert that adepthood tries a reflection three times,
which it does not do — a false fact about another repository's code, in the one
:data:`BudgetSource.DOCUMENTED_CONSTANT` whose whole value is recording that
code faithfully.
"""

ADEPTHOOD_TOTAL_DEADLINE_SECONDS: Final[float] = (
    ADEPTHOOD_FIRST_BYTE_WINDOW_SECONDS * _ADEPTHOOD_VAULT_DEADLINE_PHASES
)
"""adepthood's total deadline for one vault call.

``_VAULT_TOTAL_DEADLINE_SECONDS = 10.0 x 3`` in ``creek_vault_client.py``,
inside the ``run_resonance`` HTTP request. Composed here the way its source
composes it — per-phase window times phase count — rather than written as the
product, so a change to the documented per-phase window moves this with it. It
is also why this is not a restatement of ``DEFAULT_TIMEOUT_SECONDS``, which
happens to hold the same number for an unrelated reason.

R2 requires Machine wake to fit *beside* the vault's own budget inside this,
which is what :data:`R2_MACHINE_WAKE_ALLOWANCE_SECONDS` derives.
"""

ADEPTHOOD_PHASE_BUDGETS_WIDENED: Final[bool] = False
"""Whether adepthood has widened ``_post_reflection``'s phase budgets.

R2 names this as its own prerequisite: the reflection call must be widened to
adepthood's 30 s deadline with ``_widened_phase_budgets``, as the cold
embedding call already is, before the 25 s vault budget is reachable at all.
It has not happened — #1903 sequences it, and it is adepthood's own issue.

Flipping this one constant moves :attr:`BudgetTable.binding_budget` and which
rows are in force. Until then the harness reports the 25 s budget and judges
against the 10 s window, so no reader mistakes one for the other.
"""

R2_VAULT_P95_SECONDS: Final[float] = 25.0
"""R2: vault ``POST /v1/reflections`` p95, from the vault's receipt.

5 s under ``DEFAULT_TIMEOUT_SECONDS``. Stated by the ADR, not by any code, and
**not in force** until :data:`ADEPTHOOD_PHASE_BUDGETS_WIDENED` is true.
"""

R2_MACHINE_WAKE_ALLOWANCE_SECONDS: Final[float] = (
    ADEPTHOOD_TOTAL_DEADLINE_SECONDS - R2_VAULT_P95_SECONDS
)
"""What a wake-bearing reflection may take and still fit *beside* the vault.

R2: Machine wake "must fit beside that 25 s inside adepthood's 30 s". *Beside*,
not *inside* — so the allowance is the 30 s total deadline minus the 25 s the
reflection itself may spend, derived from both rather than written as 5 s so a
change to either moves it.

Judging the wake row against the full 30 s instead would let a 14 s
wake-bearing trial read "Machine wake fits inside adepthood's client deadline",
which is false the moment the reflection it precedes spends its own budget. The
measured value is the wake-bearing trial's *total* latency rather than an
isolated wake — the harness cannot separate them — so this is a conservative
proxy, and deliberately so: it cannot call a wake green that leaves the
reflection no room.
"""

R2_POOL_GENERATE_P95_SECONDS: Final[float] = 10.0
"""R2: pool generate p95, for a 2,500-token prompt and 128 output tokens."""

R2_POOL_TTFT_P95_SECONDS: Final[float] = 2.0
"""R2: pool time-to-first-token p95, attestation round trip included."""

R2_POOL_CONCURRENCY: Final[int] = 4
"""R2: the concurrency level the pool generate budget is stated at.

A level the concurrency sweep must request for that row to be measurable;
:attr:`BudgetTable.pool_level_measured` says whether it did.
"""

R2_POOL_PROMPT_TOKENS: Final[int] = 2500
"""R2: the prompt size the pool generate budget is stated at.

R2 budgets pool generate p95 "for a 2,500-token prompt and 128 output tokens at
concurrency 4" — two conditions, not one. The concurrency is something the
sweep requests, so :attr:`BudgetTable.pool_level_measured` can report it. The
prompt size is a property of the workload, and ``Workload.query_words``
defaults to 64 words, which is an order of magnitude under R2's prompt. A row
measured on that prompt is not the row R2 states, so the prompt size is
disclosed the same way the level is, by
:attr:`BudgetTable.pool_prompt_size_measured`.
"""

R2_POOL_PROMPT_TOKEN_TOLERANCE: Final[float] = 0.1
"""The fraction a measured prompt may differ from R2's size and still count.

R2 states a round 2,500 and no workload hits it exactly: entry text is whole
words and a tokenizer is not a word counter. A +/-10% band keeps the row
measurable for a workload genuinely aimed at R2's prompt while refusing one
that is nowhere near it.
"""

_WAKE_PHASES: Final[frozenset[Phase]] = frozenset({Phase.COLD, Phase.RESUME})
"""Residency phases whose latency contains a Machine wake."""

_VAULT_SWEEPS: Final[frozenset[Sweep]] = frozenset({Sweep.COLD_WARM, Sweep.IDLE})
"""Sweeps whose trials time the single reflection R2's vault budgets mean.

R2 budgets *one* reflection, cold or warm. Only these two sweeps issue one at
a time: ``cold_warm`` reflects sequentially and ``idle`` evicts, waits and
resumes once per cycle, so each trial's wall clock is that reflection's own.

Both stress sweeps are excluded, for the same reason and symmetrically. The
``context`` sweep walks input size up towards ``num_ctx`` to find where the
window breaks, so it times entries production never sends. The
``concurrency`` sweep runs ``level`` reflections simultaneously and every
``latency_s`` is wall clock, so each one carries the queueing delay of the
others — a figure that answers "how does the server behave under load", not
"how long does a reflection take". Folding either into a row named
``vault_reflection_p95`` would publish a stress result as the production
figure. Both are still judged, in their own ``per_sweep`` verdicts and against
the deadline this process enforces.
"""


class BudgetName(StrEnum):
    """The budgets the harness reports against, each judged on its own.

    Attributes:
        SERVER_SHED_DEADLINE: The deadline this process enforces — the tighter
            of the server request deadline and the Ollama request timeout.
        VAULT_FIRST_BYTE_WINDOW: adepthood's 10 s first-byte window, which is
            what binds today.
        VAULT_REFLECTION_P95: R2's 25 s vault budget, reachable only once
            adepthood widens its phase budgets.
        MACHINE_WAKE_WITHIN_CLIENT_DEADLINE: R2's requirement that a
            wake-bearing reflection fit *beside* the vault budget inside
            adepthood's total deadline, so it is judged against
            :data:`R2_MACHINE_WAKE_ALLOWANCE_SECONDS`, not the whole 30 s.
        POOL_GENERATE_P95: R2's 10 s pool generate budget, at concurrency 4
            and R2's prompt size — both conditions, or the row is a gap.
        POOL_TTFT_P95: R2's 2 s time-to-first-token budget, on the same
            group as the generate row. **No backend this harness ships
            reports a first-token time**, so this row is a gap in every
            configuration available today; see :attr:`Trial.ttft_s`.
    """

    SERVER_SHED_DEADLINE = "server_shed_deadline"
    VAULT_FIRST_BYTE_WINDOW = "vault_first_byte_window"
    VAULT_REFLECTION_P95 = "vault_reflection_p95"
    MACHINE_WAKE_WITHIN_CLIENT_DEADLINE = "machine_wake_within_client_deadline"
    POOL_GENERATE_P95 = "pool_generate_p95"
    POOL_TTFT_P95 = "pool_ttft_p95"


class BudgetSource(StrEnum):
    """Where a budget's number came from, and so what moves it.

    Attributes:
        CODE: Read from this repository's code at call time, so a change to
            the code moves the limit and the verdict with it.
        DOCUMENTED_CONSTANT: Stated by code in another repository and recorded
            here as a constant, because this process cannot import it.
        ADR_0016_R2: Stated by ADR-0016's R2 and by no code anywhere.
    """

    CODE = "code"
    DOCUMENTED_CONSTANT = "documented_constant"
    ADR_0016_R2 = "adr_0016_r2"


class BudgetRow(BaseModel):
    """One named budget, the value judged against it, and its verdict.

    Attributes:
        name: Which budget.
        source: Where its limit came from.
        limit_seconds: The limit, inclusive — a value exactly at it fits.
        measured_seconds: The value judged against it, or ``None`` when the
            run measured nothing this row could be judged on.
        in_force: Whether production enforces this budget today. A row that is
            not in force is reported with its own verdict but left out of
            :attr:`BudgetTable.verdict`.
        verdict: ``fits``, ``exceeds``, or ``insufficient_data`` when nothing
            was measured. A group containing any failed trial is ``exceeds``
            however fast it failed, by :data:`MAX_FIT_ERROR_RATE`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: BudgetName
    source: BudgetSource
    limit_seconds: PositiveFloat
    measured_seconds: NonNegativeFloat | None
    in_force: bool
    verdict: Verdict


class BudgetTable(BaseModel):
    """R2's budgets as measured by one run, and what they may be read as.

    Attributes:
        rows: One row per :class:`BudgetName`.
        binding_budget: The budget production enforces today. The 10 s
            first-byte window until adepthood widens its phase budgets (#1903),
            R2's 25 s vault budget after.
        phase_budgets_widened: :data:`ADEPTHOOD_PHASE_BUDGETS_WIDENED`,
            recorded so a report says which world it was judged in.
        pool_concurrency_level: :data:`R2_POOL_CONCURRENCY`, the level R2
            states its pool generate budget at.
        pool_level_measured: Whether the concurrency sweep requested that
            level. ``False`` means the pool row measured nothing, which the
            row reports as ``insufficient_data`` rather than a pass.
        pool_prompt_size_measured: Whether the trials feeding the pool rows
            reported a prompt of :data:`R2_POOL_PROMPT_TOKENS`, within
            :data:`R2_POOL_PROMPT_TOKEN_TOLERANCE`. R2 states its pool budget
            for a 2,500-token prompt *and* concurrency 4; ``False`` means the
            second condition went unmet, so the rows report
            ``insufficient_data`` rather than a pass earned on a smaller
            prompt.
        model_off_harness_host: Whether the measured model ran somewhere other
            than the host whose capacity this report describes — the exact
            complement of ``host.describes_model_host``, taken from it rather
            than re-derived. It is a narrow fact and **licenses nothing**: in
            particular it is not a production signal, because a private or
            remote endpoint is as likely to be a LAN dev box as anything
            production uses. No run this harness can execute today dials an
            attesting pool, so **no row here is ever a production
            guarantee**; see the module docstring.
        verdict: The worst verdict among the rows **in force**. Rows out of
            force are excluded by construction, so a budget production does
            not enforce can neither pass nor fail the table.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    rows: tuple[BudgetRow, ...]
    binding_budget: BudgetName
    phase_budgets_widened: bool
    pool_concurrency_level: PositiveInt
    pool_level_measured: bool
    pool_prompt_size_measured: bool
    model_off_harness_host: bool
    verdict: Verdict


def _summary(trials: Sequence[Trial]) -> LatencySummary | None:
    """Summarise *trials*, or return ``None`` when there are none to judge."""
    return LatencySummary.from_trials(trials) if trials else None


def _judge(
    summary: LatencySummary | None, value: float | None, limit: float
) -> Verdict:
    """Judge *value* against *limit* for the group *summary* describes.

    The error-rate clause comes first and is the whole point: a group with one
    timed-out trial exceeds even when its p95 is inside every budget, because
    that trial is a reflection the user did not get. With no failures, a value
    of ``None`` is a genuine gap — nothing streamed, or the level was never
    requested — and is reported as such rather than as a pass.
    """
    if summary is None or summary.count == 0:
        return Verdict.INSUFFICIENT_DATA
    if summary.error_rate > MAX_FIT_ERROR_RATE:
        return Verdict.EXCEEDS
    if value is None:
        return Verdict.INSUFFICIENT_DATA
    return Verdict.EXCEEDS if value > limit else Verdict.FITS


def _row(
    name: BudgetName,
    source: BudgetSource,
    limit: float,
    summary: LatencySummary | None,
    value: float | None,
    *,
    in_force: bool,
) -> BudgetRow:
    """Assemble one judged budget row."""
    return BudgetRow(
        name=name,
        source=source,
        limit_seconds=limit,
        measured_seconds=value,
        in_force=in_force,
        verdict=_judge(summary, value, limit),
    )


def _pool_trials(trials: Sequence[Trial]) -> list[Trial]:
    """Return the concurrency sweep's trials at R2's stated level."""
    return [
        trial
        for trial in trials
        if trial.sweep is Sweep.CONCURRENCY and trial.concurrency == R2_POOL_CONCURRENCY
    ]


def _is_r2_prompt_size(trial: Trial) -> bool:
    """Return whether *trial* reported a prompt of R2's stated size.

    Only a provider-reported count answers this. A byte upper bound is not a
    token count, and an unreported or underived one is not a measurement, so
    either leaves the condition unmet rather than guessed at.
    """
    if trial.token_basis is not TokenBasis.PROVIDER_REPORTED:
        return False
    if trial.prompt_tokens is None:
        return False
    allowed = R2_POOL_PROMPT_TOKENS * R2_POOL_PROMPT_TOKEN_TOLERANCE
    return abs(trial.prompt_tokens - R2_POOL_PROMPT_TOKENS) <= allowed


def _vault_trials(trials: Sequence[Trial]) -> list[Trial]:
    """Return the trials that time the reflection R2's vault budgets mean.

    Scoped to :data:`_VAULT_SWEEPS`, whose docstring states why each stress
    sweep is excluded.
    """
    return [trial for trial in trials if trial.sweep in _VAULT_SWEEPS]


def _vault_rows(
    overall: LatencySummary | None,
    vault: LatencySummary | None,
    *,
    widened: bool,
) -> tuple[BudgetRow, ...]:
    """Return the shed-deadline row and R2's two vault rows.

    The shed deadline is judged over **every** trial, because it is the
    deadline this process actually sheds on: a context-sweep reflection is
    shed by it exactly like any other, and a row sourced from this
    repository's code reading ``fits`` beside a ``per_sweep.context`` verdict
    of ``exceeds`` — for the same number — is a contradiction a reader would
    quote. Scoping to the representative reflection is defensible for R2's two
    vault rows, which name a production figure; it is not defensible here.
    """
    vault_p95 = vault.p95_s if vault else None
    return (
        _row(
            BudgetName.SERVER_SHED_DEADLINE,
            BudgetSource.CODE,
            deadline_budget_seconds(),
            overall,
            overall.p95_s if overall else None,
            in_force=True,
        ),
        _row(
            BudgetName.VAULT_FIRST_BYTE_WINDOW,
            BudgetSource.DOCUMENTED_CONSTANT,
            ADEPTHOOD_FIRST_BYTE_WINDOW_SECONDS,
            vault,
            vault_p95,
            in_force=not widened,
        ),
        _row(
            BudgetName.VAULT_REFLECTION_P95,
            BudgetSource.ADR_0016_R2,
            R2_VAULT_P95_SECONDS,
            vault,
            vault_p95,
            in_force=widened,
        ),
    )


def _pool_rows(pool: Sequence[Trial]) -> tuple[BudgetRow, ...]:
    """Return R2's two pool rows, judged on the same group of trials.

    Both rows describe one claim — how the pool behaves at R2's stated level
    and prompt size — so both are judged on that group and no other. In
    particular the TTFT row is **not** judged over every trial: feeding it the
    context sweep let one deliberate out-of-memory there publish a row reading
    "limit 2.0 s, measured 1.0 s, verdict exceeds", because the error-rate
    clause fires on a group the row does not describe. Scoping the group
    scopes that clause with it.
    """
    sized = [trial for trial in pool if _is_r2_prompt_size(trial)]
    summary = _summary(sized)
    return (
        _row(
            BudgetName.POOL_GENERATE_P95,
            BudgetSource.ADR_0016_R2,
            R2_POOL_GENERATE_P95_SECONDS,
            summary,
            summary.generation_p95_s if summary else None,
            in_force=True,
        ),
        _row(
            BudgetName.POOL_TTFT_P95,
            BudgetSource.ADR_0016_R2,
            R2_POOL_TTFT_P95_SECONDS,
            summary,
            summary.ttft_p95_s if summary else None,
            in_force=True,
        ),
    )


def budget_table(trials: Sequence[Trial], *, describes_model_host: bool) -> BudgetTable:
    """Judge *trials* against every budget in ADR-0016 R2, row by row.

    Args:
        trials: Every trial the sweeps produced.
        describes_model_host: ``host.describes_model_host`` for this run.
            Recorded, complemented, as
            :attr:`BudgetTable.model_off_harness_host` so the two fields can
            never disagree. It is not a production signal; no run this harness
            can execute today measures the production path.

    Returns:
        The table, whose verdict covers only the rows in force today.
    """
    widened = ADEPTHOOD_PHASE_BUDGETS_WIDENED
    overall = _summary(trials)
    vault = _summary(_vault_trials(trials))
    wake = _summary([t for t in trials if t.phase in _WAKE_PHASES])
    pool = _pool_trials(trials)
    rows = (
        *_vault_rows(overall, vault, widened=widened),
        _row(
            BudgetName.MACHINE_WAKE_WITHIN_CLIENT_DEADLINE,
            BudgetSource.DOCUMENTED_CONSTANT,
            R2_MACHINE_WAKE_ALLOWANCE_SECONDS,
            wake,
            wake.p95_s if wake else None,
            in_force=True,
        ),
        *_pool_rows(pool),
    )
    return BudgetTable(
        rows=rows,
        binding_budget=(
            BudgetName.VAULT_REFLECTION_P95
            if widened
            else BudgetName.VAULT_FIRST_BYTE_WINDOW
        ),
        phase_budgets_widened=widened,
        pool_concurrency_level=R2_POOL_CONCURRENCY,
        pool_level_measured=bool(pool),
        pool_prompt_size_measured=any(_is_r2_prompt_size(trial) for trial in pool),
        model_off_harness_host=not describes_model_host,
        verdict=overall_verdict([row.verdict for row in rows if row.in_force]),
    )
