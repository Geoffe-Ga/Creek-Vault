"""The bench's structural seam types match what ``reflect_tool`` accepts.

``creek_mcp.bench.protocols`` restates reflect's private ``_LLM`` /
``_LLMFactory`` shapes rather than importing private names. These assignments
are checked by mypy (the suite is type-checked) and executed here, so a drift
between the bench's factories and the protocols fails both the type gate and
this test.
"""

from __future__ import annotations

import inspect

from creek.models import PrivacyTier
from creek_mcp.bench.fake import fake_factory
from creek_mcp.bench.ollama_client import BenchOllamaClient
from creek_mcp.bench.protocols import LLMCallable, LLMFactory, Retriever
from creek_mcp.bench.runner import _no_grounding
from creek_mcp.tools import reflect


def test_bench_factories_satisfy_the_factory_protocol() -> None:
    """Both bench factories build callables of the protocol's shape."""
    client = BenchOllamaClient("http://127.0.0.1:9", "m", num_ctx=8, num_predict=8)
    factories: list[LLMFactory] = [fake_factory, client.factory()]
    for factory in factories:
        built: LLMCallable = factory(PrivacyTier.OPEN, max_tokens=8)
        assert callable(built)


def test_hermetic_grounder_satisfies_the_retriever_protocol() -> None:
    """The no-grounding retriever is a valid ``retrieve=`` seam."""
    retriever: Retriever = _no_grounding
    assert inspect.signature(retriever).parameters.keys() == {
        "query",
        "vault",
        "override",
    }


def test_protocols_mirror_reflects_factory_signature() -> None:
    """The factory protocol's call signature is reflect's, parameter for parameter."""
    ours = inspect.signature(LLMFactory.__call__).parameters
    theirs = inspect.signature(reflect._LLMFactory.__call__).parameters
    assert [(p.name, p.kind) for p in ours.values()] == [
        (p.name, p.kind) for p in theirs.values()
    ]
    ours_llm = inspect.signature(LLMCallable.__call__).parameters
    theirs_llm = inspect.signature(reflect._LLM.__call__).parameters
    assert [(p.name, p.kind) for p in ours_llm.values()] == [
        (p.name, p.kind) for p in theirs_llm.values()
    ]
