"""Voice-fidelity eval harness: a blinded rubric over a synthetic corpus (#1898).

This is the *quality* half of the capacity harness in :mod:`creek_mcp.bench`,
filling the ``quality_score`` hook that package reserved. It scores a candidate
model's reflections and essays on the four dimensions #1875 asks for — voice
match, obedience to Writing Desk instructions, grounding accuracy, and refusal
behaviour — each reported separately, never collapsed into one number.

Four properties are load-bearing:

- **It builds the instrument; it does not take the decision.** #1875 records
  the model, the revision and the licence. Nothing here picks a model, and
  nothing here touches ``APPROVED_MODEL_LICENSES``.
- **The owner's bar is explicitly not frontier.** Per the ratification comment
  on #1849, a Sonnet 4.x-class model produced voice drafts the owner was happy
  with, so the eval starts at the cheapest catalogue model and climbs only on
  a rubric *failure*. :func:`~creek_mcp.bench.eval.rubric.next_candidate` is
  the whole escalation policy, and a pass stops the ladder — "bigger is
  better" is not expressible in it.
- **Hermetic by default.** The default judge is
  :class:`~creek_mcp.bench.eval.judge.HermeticJudge`: deterministic, offline,
  no key. A frontier judge is
  :class:`~creek_mcp.bench.eval.judge.RoutedJudge`, selected explicitly and
  resolved through :meth:`creek.classify.llm.router.ModelRouter.resolve_role`
  like every other stage, so the INTIMATE chokepoint and consent gating apply
  to it unchanged. Decision 4 on #1849 authorizes **no spend yet**, so the
  real-API path is exercised only by the ``live`` lane, which CI never runs.
- **Synthetic corpus, and two artifacts.** The corpus is invented prose written
  for this harness and confined to a temp directory. The score that rides in
  :class:`~creek_mcp.bench.report.BenchReport` is closed by construction
  (enums, numbers, a semver); the prompts and raw scores #1875 asks to be
  recorded go in the eval's own :class:`~creek_mcp.bench.eval.record.EvalRecord`
  artifact, so the report's content-free invariant is not weakened to satisfy
  them.

**A score is comparable only within one rubric version and one seed.** Both are
recorded in every artifact. Compare across a version bump and the number means
something different.
"""

from __future__ import annotations

from typing import Final

RUBRIC_VERSION: Final[str] = "1.0.0"
"""Version of the rubric and its artifact schema, recorded in every run.

Bumped whenever a dimension changes meaning, a threshold moves, or a scorer's
arithmetic changes — anything that makes two scores incomparable.
"""

PASS_THRESHOLD: Final[float] = 0.7
"""The per-dimension score at or above which a dimension passes.

Every dimension must clear it for the rubric to pass. It is the only place the
"good enough" line lives, so moving it is a rubric-version change.
"""
