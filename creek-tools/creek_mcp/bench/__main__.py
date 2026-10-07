"""Run the capacity harness: ``python -m creek_mcp.bench``.

The entry point ``scripts/bench.sh`` execs. It adds nothing: flags, refusals
and exit codes are :func:`creek_mcp.bench.cli.main`'s. As in
``creek_mcp/httpapi/__main__.py``, the import lives inside the guard so this
file has no importable side effect — it is only ever executed.
"""

if __name__ == "__main__":  # pragma: no cover - exercised via scripts/bench.sh
    from creek_mcp.bench.cli import main

    raise SystemExit(main())
