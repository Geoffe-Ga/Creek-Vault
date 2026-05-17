"""Compost exemplar fixtures shipped as package data (FEAT-018).

This package exists so that ``creek.generate.exemplars`` is a real
Python subpackage rather than an unmanaged data directory. Setuptools
needs the ``__init__.py`` to discover the directory and ship the YAML
fixtures via ``[tool.setuptools.package-data]``; without it,
non-editable installs would silently omit the file and
:func:`creek.generate.compost_embedding.load_exemplars` would raise
``FileNotFoundError`` at runtime.

The exemplar YAMLs themselves (``compost.yaml``) are not imported as
Python modules — they are loaded by
:func:`creek.generate.compost_embedding.load_exemplars`.
"""
