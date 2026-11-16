# RCA: revoice register values were not discoverable

## Problem statement

`creek.revoice` accepted `register` as an arbitrary string in its MCP input
schema even though the adapter only admits the seven `VoiceRegister` values.
A client following a stale workflow supplied `essay`, received
`register must name a canonical voice register`, and could not discover the
valid alternatives from `tools/list`.

## Root cause

The FastMCP closure in `creek_mcp/server.py` annotated `register` as
`str | None`. Runtime validation in `creek_mcp/tools/revoice.py` correctly used
`VoiceRegister`, but that enum was absent from the public JSON Schema.

## Impact

Clients can choose invalid registers and may respond by silently dropping the
conditioning, yielding a materially weaker rewrite. Privacy and persistence
behavior are unaffected.

## Contributing factors

- Runtime validation and MCP schema used different types.
- The registration-surface test pinned the non-discoverable string schema.
- Documentation said "canonical" without listing the values near the tool.

## Fix strategy

Annotate the FastMCP argument with `VoiceRegister | None`, pin the resulting
enum in the schema regression test, and list the values in the MCP docs. Keep
runtime validation as defense in depth.

## Prevention

Prefer domain enums over plain strings at MCP registration boundaries whenever
the accepted value set is closed and already modeled internally.
