"""``python -m creek_mcp.httpapi`` — the console script without the console script.

Same entry point as ``creek-tools-api``, for environments where the packaging
scripts directory is not on ``PATH`` (a container running the module directly,
an editable checkout invoked from a systemd unit, a debugger). It adds nothing:
the flags, the refusals and the exit codes are :func:`creek_mcp.httpapi.cli.main`'s
in their entirety, because a second front door with its own argument handling is
a second place for a startup guard to be forgotten.

The import lives inside the guard rather than at module scope so this file has
no importable side effect at all — it is only ever executed, never imported.
"""

if __name__ == "__main__":  # pragma: no cover - exercised via the entry point
    from creek_mcp.httpapi.cli import main

    main()
