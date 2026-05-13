"""Few-shot example fixtures for LLM classification (FEAT-017).

Each ``<dimension>.yaml`` file in this package holds a list of
``{title, body, label, rationale}`` entries used by
:mod:`creek.classify.few_shot` to build rotating few-shot prompts.

The files are shipped with the wheel via ``[tool.setuptools.package-data]``
so installed copies of ``creek-tools`` can build the same prompt as a
source checkout.
"""
