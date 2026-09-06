# Adding a new remote connector

Creek pulls remote sources through a small, **read-only** abstraction:
`RemoteSourceConnector` (`creek/ingest/connectors.py`). Google Drive is the
reference implementation (`creek/ingest/gdrive.py`); this guide shows how to add
the next one (Substack, Notion, Readwise, an RSS feed, a prose git repo …).

A connector **stages** files; it does **not** ingest. After `fetch_to` drops
files into a staging directory, the regular ingest pipeline picks them up via
`route_to_ingestor` (by extension). The connector never classifies, links, or
writes fragments.

## Staging is not a promise of ingestion

`route_to_ingestor` is **partial**. It dispatches on the file's extension, and
for a structured format that the `generic` fallback would flatten into one
undifferentiated blob, it raises `creek.ingest.UnsupportedSourceError` rather
than returning an ingestor name (#1526). These are the families it refuses:

<!-- capability-set: connector-refusals -->

| Family | Extensions |
|--------|------------|
| Conversation export | `.json`, `.jsonl`, `.ndjson` |
| Archive | `.zip`, `.tar`, `.tgz`, `.gz`, `.bz2`, `.xz`, `.7z`, `.rar` |
| Legacy binary Office | `.doc`, `.xls`, `.ppt` |

<!-- /capability-set -->

That table is checked against the live routing table by
`tests/test_connector_docs_drift.py` in **both** directions — a family the code
refuses but the table omits, and a family the table invents but the code
routes, both fail the build.

An extension the table does **not** name still falls through to `generic`,
which remains the right ingestor for genuinely plain-text-ish content
(`.yaml`, `.log`, `.org`, an extensionless `README`). #1526 narrowed the
fallback; it did not retire it. Do not conclude that every unrecognised
extension is refused.

Your connector does not catch this — its **caller** does, after `fetch_to`
returns. There are two sanctioned responses:

- **Skip the item** and count it, leaving the bytes in staging for a later
  `creek ingest --type <chatgpt|claude|discord|substack> --input <export-dir>`.
- **Surface `exc.guidance`** — the remedy string the exception carries — to
  whoever asked for the fetch.

The working reference is `_ingest_downloaded` in `creek_mcp/tools/drive.py`:
it catches `(UnsupportedSourceError, KeyError)`, increments an `unsupported`
tally and continues, because a background sync has no caller to hand the
remedy to.

One surface differs, and it is worth knowing about. The MCP upload tool runs
an archive fork *above* this gate (#1525), so a `.zip` sent there is unpacked
rather than turned away — even though the routing table refuses it.

## The contract

Implement these five methods:

```python
class MySourceConnector:
    def is_available(self) -> bool:
        """Can the backend serve requests? Capability only — never reads or
        returns a credential."""

    def list_changed_since(self, cursor):
        """Return the files changed since `cursor` (an opaque, persisted
        bookmark). `cursor=None` means the first pass (everything)."""

    def fetch_to(self, staging):
        """Download the changed files into `staging`. Returns a result object;
        callers route the staged files through `route_to_ingestor`, which
        raises `UnsupportedSourceError` for a refused format — the caller
        skips that item or surfaces `exc.guidance`. An extension the routing
        table does not name still routes to `generic`."""

    def load_cursor(self):
        """Return the persisted cursor, or None if there is none yet."""

    def save_cursor(self, fetched):
        """Advance the persisted cursor past `fetched` (called after a
        successful fetch_to)."""
```

`RemoteSourceConnector` is a `runtime_checkable` Protocol, so structural
conformance is enough — no inheritance required. A connector that exposes these
methods satisfies `isinstance(conn, RemoteSourceConnector)`.

### Read-only by construction

The protocol has **no** `update`/`delete`/`trash`/`copy`/`create` method, and
your connector must not add one. A connector reads and downloads; it never
writes back to the source.

### The cursor

`list_changed_since(cursor)` is *incremental against a persisted cursor*, not
just in-process state — so a fresh process or a new host resumes correctly. The
cursor is whatever durable bookmark fits the source:

- **Drive** persists the newest `modified_time` it has fetched and returns files
  newer than it.
- A **timestamped API** (Substack, Notion) would persist a `since` timestamp.
- A **git repo** would persist the last commit SHA.

Store the cursor as **non-secret bookkeeping only** (a timestamp, id, or hash) —
see the secrets rule below.

## Secrets — read from env/disk only, never persist or log

This is non-negotiable:

- A connector reads OAuth tokens / API keys **from disk or the environment**,
  per the existing `GoogleDriveConfig` pattern. **Never inline a secret into
  config or code.**
- **Never echo or log** a token, refresh token, client secret, or API key. Any
  status output is presence-only (`is_available` reports capability, not the
  credential).
- The persisted **cursor stores only bookkeeping** (`last_seen`/ids/hashes) —
  never a credential.

## Prove it with the contract test

`tests/test_remote_connector_contract.py` is parametrised over connector
factories. Add your factory to `_FACTORIES` and the whole contract runs against
it for free:

```python
def _make_my_connector(*, staging):
    return MySourceConnector(...)

_FACTORIES = [_make_drive_connector, _make_my_connector]
```

The contract asserts: the connector conforms to the protocol, `is_available`
never raises, and `list_changed_since` is **idempotent against an advanced
cursor** (after `fetch_to` + `save_cursor`, the reloaded cursor yields nothing
new — including across a brand-new instance).
