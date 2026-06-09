# ISSUE #1 — Normalize `Completion` and define the `LLMProvider` Protocol (creek-tools)

**Epic:** Swappable LLM providers · **Sequence:** 1/8 · **Risk:** low (pure refactor)

## Role
You are a Python library engineer working in `creek-tools/` under its
maximum-quality bar (mypy strict, coverage ≥90%, docstrings ≥95%,
complexity ≤10, ruff/bandit clean). Use `./scripts/*`, never raw tools.

## Goal
Lay the seam for swappable providers **without changing any behavior**:
extract a provider-neutral result type and a `Protocol` the existing
Anthropic and Ollama paths can both satisfy.

## Context
- `creek-tools/creek/classify/llm/providers.py` defines
  `AnthropicCompletion` (`text`, `stop_reason`, `usage: dict|None`) —
  already the right normalized shape — plus `AnthropicProvider`,
  `call_ollama`, `check_ollama_available`.
- `creek/classify/llm/__init__.py` is the public re-export shim; external
  callers and `creek/author/client.py` import `AnthropicCompletion` /
  `AnthropicProvider` from there.
- The pipeline is **synchronous**.

## Deliverables
1. New `creek/classify/llm/completion.py`: `Completion` dataclass (frozen,
   identical fields to today's `AnthropicCompletion`). Keep
   `AnthropicCompletion = Completion` as a deprecated alias.
2. New `creek/classify/llm/base.py`: `LLMProvider` `Protocol` with
   `model: str` (property), `available: bool` (property), and
   `complete(self, prompt: str, *, max_tokens: int | None = None,
   system: str | None = None) -> Completion`.
3. `providers.py`: import `Completion` from the new module; make
   `AnthropicProvider` satisfy `LLMProvider` (it already has `model`;
   add a thin `complete(...)` that returns `Completion`, and an
   `available` property delegating to its existing init validation).
4. Update `__init__.py` re-exports so the public surface is unchanged
   (both old and new names importable).

## Examples (shape, not literal)
```python
class LLMProvider(Protocol):
    @property
    def model(self) -> str: ...
    @property
    def available(self) -> bool: ...
    def complete(self, prompt: str, *, max_tokens: int | None = None,
                 system: str | None = None) -> Completion: ...
```

## Constraints
- Zero behavior change. No new dependencies. No model-ID literals move
  yet (that's #2/#4/#5).
- TDD: add a test asserting `AnthropicCompletion is Completion` and that
  `AnthropicProvider` is a structural `LLMProvider`.

## Acceptance
- `./scripts/check-all.sh` exit 0. Existing tests unchanged and green.
- Every prior import path still resolves.
