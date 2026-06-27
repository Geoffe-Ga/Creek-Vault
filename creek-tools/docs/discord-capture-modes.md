# Discord capture modes

Creek can pull your Discord writing into the vault three ways. They trade off
**compliance**, **coverage** (especially whether DMs are reachable), and
**setup**. Pick per the table below; you can use more than one.

A hard constraint shapes all of this: **a bot cannot read DMs** (Discord API
limit). Only the user-token exporter and a manual Data Package can reach DMs.

| Mode           | Compliance              | Coverage                       | Setup                                         |
|----------------|-------------------------|--------------------------------|-----------------------------------------------|
| `data_package` | Clean                   | Complete (incl. DMs)           | Manual download, no automation                |
| `bot_capture`  | Clean (in-server)       | Servers/channels only — no DMs | Bot token (env), runs continuously            |
| `exporter`     | **ToS-gray, opt-in**    | DMs + breadth                  | User token (env) + ToS caveat, **off by default** |

## `data_package` — the clean, complete fallback

Request your data from Discord (User Settings → Privacy & Safety → Request
Data), download the package, and point Creek at it:

```bash
creek discord --mode data_package --package /path/to/discord-export
```

Ingest is **incremental**: Discord message ids are stable, so re-pointing at the
same (or an overlapping) package is a clean no-op — already-ingested messages are
not re-written and cost no LLM/link re-spend. This is the safest path and the
only clean one that includes DMs; its only downside is the manual download, which
is the chore the other two modes exist to avoid.

## `bot_capture` — continuous, clean, servers only

A bot you run logs each message in the servers/channels it is in to
`discord-capture/<channel>/<date>.jsonl`; Tier-A ingest (`creek sync`) reshapes
that into the layout the ingestor reads. Clean and ToS-compliant, but a bot
**cannot see DMs** — this covers only channels the bot is a member of. Enable it
with `discord.bot_capture` (off by default) and `capture_enabled` on the bot.

## `exporter` — DMs + breadth, opt-in and ToS-gray

A user-token exporter can reach DMs and full history, but driving a **user**
account with a token violates the Discord Terms of Service, and the real stake is
account suspension. It is therefore **off by default** and prints a ToS caveat on
enable. Use it read-only, for your own DMs, at a low request rate.

```bash
# Only after setting discord.exporter.enabled = true and discord.exporter_binary
creek discord --mode exporter
```

## Secrets — non-negotiable

For both token-bearing modes, the Discord token (user **or** bot) lives in the
**environment only** — never inlined into `creek_config.yaml`, never echoed or
logged. The `data_package` path carries no token at all. The `exporter` mode is
**off by default** and prints its Terms-of-Service caveat every time it is
enabled and run.
