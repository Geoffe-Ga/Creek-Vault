"""``GET /v1/agent-activity`` — what each agent asked for, never what it read (#1968).

The route is a projection of the MCP audit chain, and the tests below are about
that projection's two promises more than about its plumbing:

* **Content-free under every input.** A row is ``{consumer, tool, at,
  outcome}`` and nothing else. No argument summary, no fragment id, no path, no
  hash crosses — the property test feeds the projection audit entries full of
  arbitrary text and asserts none of it reaches the response.
* **Read, never rewritten.** The log's bytes are identical before and after a
  request, the chain verifier is never run per request, and the route appends
  nothing to the trail it reads.

Everything else — the clamp, ``since``, the skipped counter, the version gate —
is the shape a consumer (adepthood's "Recent activity" card, A4) codes against,
and is pinned against the committed fixture rather than restated.
"""

from __future__ import annotations

import asyncio
import json
import string
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from mcp.server.auth.provider import AccessToken

from creek_mcp import audit as audit_mod
from creek_mcp import server as server_mod
from creek_mcp.api.models import (
    AGENT_ACTIVITY_DEFAULT_LIMIT,
    AGENT_ACTIVITY_MAX_LIMIT,
    AGENT_ACTIVITY_TOOL_PATTERN,
    CAPABILITY_SINCE_MINOR,
    CONTRACT_MINOR,
    AgentActivityOutcome,
    AgentActivityResponse,
    Capability,
    ErrorCode,
)
from creek_mcp.audit import (
    AUDIT_OUTCOME_ACCEPTED,
    AUDIT_OUTCOME_COMPLETED,
    AUDIT_OUTCOME_REFUSED,
    MCP_AUDIT_RELPATH,
    MCPAuditLog,
    verify_mcp_audit_chain,
)
from creek_mcp.httpapi import agent_activity as activity_mod
from creek_mcp.policy import UNGATED_SCOPE, Grant, Transport
from creek_mcp.remote_auth import REMOTE_SCOPE
from creek_mcp.server import UNREGISTERED_TOOL_AUDIT_NAME, build_server
from creek_mcp.tier_ceiling import TierCeiling
from tests.agent_token_support import (
    AGENT_CLIENT_ID,
    AGENT_SUBJECT,
    agent_verifier,
    mint,
)
from tests.v1_api_support import (
    CAPABILITIES_PATH,
    CONSUMER,
    OTHER_CONSUMER,
    OTHER_TOKEN,
    WHEEL_PATH,
    client,
    envelope,
    headers,
    seed_vault,
)

if TYPE_CHECKING:
    import httpx
    from mcp.server.fastmcp import FastMCP

AGENT_ACTIVITY_PATH: Final[str] = "/v1/agent-activity"
"""The route under test, spelled out rather than read off the route table."""

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
_FIXTURE_DIR: Final[Path] = (
    _REPO_ROOT / "docs" / "contracts" / "adepthood-v1" / "examples" / "agent-activity"
)

_OK: Final[int] = 200
_INCOMPATIBLE_VERSION: Final[int] = 409
_INVALID_REQUEST: Final[int] = 422
_UNAVAILABLE: Final[int] = 503
_ROW_KEYS: Final[frozenset[str]] = frozenset({"consumer", "tool", "at", "outcome"})
_ENVELOPE_KEYS: Final[frozenset[str]] = frozenset(
    {"status", "tier_ceiling", "skipped", "rows"}
)
_PREVIOUS_MINOR: Final[str] = "0.21"
_BASE_INSTANT: Final[datetime] = datetime(2026, 10, 24, 17, 0, 0, tzinfo=UTC)
_LONG_STRING: Final[str] = "a body the activity card must never show " * 4


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _log_path(vault: Path) -> Path:
    """Return *vault*'s MCP audit log path."""
    return vault / MCP_AUDIT_RELPATH


def _write_lines(vault: Path, lines: list[str]) -> None:
    """Append raw *lines* to the audit log, creating it if needed."""
    path = _log_path(vault)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.writelines(f"{line}\n" for line in lines)


def _entry(
    *,
    tool: object = "creek.draft",
    consumer: object = "acct_91",
    at: datetime = _BASE_INSTANT,
    outcome: object = None,
    **extra: Any,
) -> str:
    """Return one serialised audit entry in the shape MCPAuditLog writes."""
    entry: dict[str, Any] = {
        "tool": tool,
        "args_summary": {"topic": "the river at dawn"},
        "tier_ceiling": "personal",
        "consumer": consumer,
        "timestamp": at.isoformat(),
        "entry_hash": "0" * 64,
        "prev_hash": "1" * 64,
        **extra,
    }
    if outcome is not None:
        entry["outcome"] = outcome
    return json.dumps(entry)


def _get(
    vault: Path | None,
    *,
    query: str = "",
    minor: str = CONTRACT_MINOR,
    ceiling: str | None = None,
    token: str | None = None,
) -> httpx.Response:
    """Call the route through the real Starlette adapter."""
    request_headers = (
        headers(minor=minor, ceiling=ceiling)
        if token is None
        else headers(minor=minor, ceiling=ceiling, token=token)
    )
    with client(vault_path=vault) as test_client:
        return test_client.get(
            f"{AGENT_ACTIVITY_PATH}{query}",
            headers=request_headers,
        )


def _rows(response: httpx.Response) -> list[dict[str, Any]]:
    """Return the rows of a ``200`` response."""
    assert response.status_code == _OK, response.text
    rows: list[dict[str, Any]] = envelope(response)["rows"]
    return rows


def _fixture(state: str) -> dict[str, Any]:
    """Return one committed agent-activity example."""
    loaded: dict[str, Any] = json.loads(
        (_FIXTURE_DIR / f"{state}.json").read_text(encoding="utf-8")
    )
    return loaded


def _remote_token(consumer: str, *scopes: str) -> AccessToken:
    """Return an access token as the verifier mints one for *consumer*."""
    return AccessToken(
        token="opaque",  # test literal, not a real credential
        client_id=consumer,
        scopes=[REMOTE_SCOPE, *scopes],
        expires_at=None,
    )


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    """Return a freshly seeded vault."""
    return seed_vault(tmp_path)


# --------------------------------------------------------------------------- #
# The envelope, and the empty log
# --------------------------------------------------------------------------- #


def test_an_empty_log_is_a_200_with_no_rows(vault: Path) -> None:
    """A vault nobody has called yet is a legitimate state, not an error."""
    assert not _log_path(vault).exists()
    response = _get(vault)
    assert response.status_code == _OK
    assert envelope(response) == {
        "status": "ok",
        "tier_ceiling": "open",
        "skipped": 0,
        "rows": [],
    }


def test_the_empty_response_is_the_committed_empty_fixture(vault: Path) -> None:
    """The ``empty`` example is the bytes an empty log really produces."""
    assert envelope(_get(vault)) == _fixture("empty")


def test_a_present_but_blank_log_is_also_empty(vault: Path) -> None:
    """Blank lines are not entries, so they are neither rows nor skipped."""
    _write_lines(vault, ["", "   "])
    body = envelope(_get(vault))
    assert body["rows"] == []
    assert body["skipped"] == 0


def test_the_response_shape_is_the_committed_success_fixture(vault: Path) -> None:
    """The ``success`` example is the bytes its own log really produces.

    The log is written in chain order — oldest first — from the fixture's rows
    reversed, so the assertion is that the route returns newest first, renders
    ``at`` in whole UTC seconds with a ``Z``, maps a missing ``outcome`` to
    ``admitted`` and drops everything else the entries carried.
    """
    fixture = _fixture("success")
    lines = [
        _entry(
            tool=row["tool"],
            consumer=row["consumer"],
            at=datetime.fromisoformat(row["at"]) + timedelta(microseconds=123_456),
            outcome=(
                AUDIT_OUTCOME_REFUSED
                if row["outcome"] == AgentActivityOutcome.REFUSED.value
                else None
            ),
            created_path="07-Voice/Drafts/river.md",
            affected_fragment_ids=["frag-3f9a1c7e40b2"],
        )
        for row in reversed(fixture["rows"])
    ]
    _write_lines(vault, lines)
    response = _get(vault, query="?limit=3")
    assert envelope(response) == fixture
    AgentActivityResponse.model_validate(envelope(response))


def test_every_row_carries_exactly_the_four_published_keys(vault: Path) -> None:
    """``args_summary``, ``entry_hash`` and the write-side fields never cross."""
    _write_lines(
        vault,
        [
            _entry(
                created_path="01-Fragments/Notes/secret.md",
                created_tier="personal",
                affected_fragment_ids=["frag-3f9a1c7e40b2"],
            )
        ],
    )
    response = _get(vault)
    body = envelope(response)
    assert set(body) == _ENVELOPE_KEYS
    assert [set(row) for row in body["rows"]] == [_ROW_KEYS]
    for leaked in (
        "the river at dawn",
        "frag-3f9a1c7e40b2",
        "secret.md",
        "0" * 64,
        "1" * 64,
        "args_summary",
        "entry_hash",
        "prev_hash",
    ):
        assert leaked not in response.text


def test_the_tier_ceiling_is_echoed_from_the_header(vault: Path) -> None:
    """The envelope keeps ``tier_ceiling`` for uniformity, echoing the header."""
    assert envelope(_get(vault, ceiling="personal"))["tier_ceiling"] == "personal"


def test_the_ceiling_does_not_filter_rows(vault: Path) -> None:
    """The route reads no content, so an ``open`` caller sees ``personal`` calls."""
    _write_lines(vault, [_entry()])
    assert len(_rows(_get(vault, ceiling="open"))) == 1


# --------------------------------------------------------------------------- #
# Outcomes
# --------------------------------------------------------------------------- #


def test_an_entry_without_an_outcome_is_admitted(vault: Path) -> None:
    """Tools write no ``outcome``: the call passed every pre-dispatch gate."""
    _write_lines(vault, [_entry()])
    assert _rows(_get(vault))[0]["outcome"] == "admitted"


def test_a_refused_entry_is_refused(vault: Path) -> None:
    """A pre-dispatch refusal is the one ``refused`` the chain can carry."""
    _write_lines(vault, [_entry(tool=UNREGISTERED_TOOL_AUDIT_NAME, outcome="refused")])
    assert _rows(_get(vault)) == [
        {
            "consumer": "acct_91",
            "tool": UNREGISTERED_TOOL_AUDIT_NAME,
            "at": "2026-10-24T17:00:00Z",
            "outcome": "refused",
        }
    ]


@pytest.mark.parametrize("lifecycle", [AUDIT_OUTCOME_ACCEPTED, AUDIT_OUTCOME_COMPLETED])
def test_job_lifecycle_entries_are_not_calls(vault: Path, lifecycle: str) -> None:
    """``accepted`` / ``completed`` follow a call already listed; not a second ask."""
    _write_lines(vault, [_entry(outcome=lifecycle)])
    body = envelope(_get(vault))
    assert body["rows"] == []
    assert body["skipped"] == 0


def test_a_detached_composition_is_one_row(vault: Path) -> None:
    """The tool's entry, its ``accepted`` and its ``completed`` are one call.

    The three entries a past-deadline ``creek.draft`` writes share a job id
    (#1966). The owner asked for one draft, so the card shows one.
    """
    log = MCPAuditLog(vault)
    log.append(
        tool="creek.draft",
        args={"topic": _LONG_STRING},
        tier_ceiling=TierCeiling.PERSONAL,
        consumer="openclaw",
    )
    for outcome, args in (
        (AUDIT_OUTCOME_ACCEPTED, {"job_id": "job-1"}),
        (AUDIT_OUTCOME_COMPLETED, {"job_id": "job-1", "state": "succeeded"}),
    ):
        log.append(
            tool="creek.draft",
            args=args,
            tier_ceiling=TierCeiling.PERSONAL,
            consumer="openclaw",
            outcome=outcome,
        )
    rows = _rows(_get(vault))
    assert [(row["consumer"], row["tool"], row["outcome"]) for row in rows] == [
        ("openclaw", "creek.draft", "admitted")
    ]
    assert "job-1" not in json.dumps(rows)


def test_an_unknown_outcome_is_skipped_rather_than_guessed(vault: Path) -> None:
    """A value the closed enum cannot name is counted, never rendered."""
    _write_lines(vault, [_entry(outcome="partially-admitted"), _entry(outcome=7)])
    body = envelope(_get(vault))
    assert body["rows"] == []
    assert body["skipped"] == 2


def _remote_server(
    vault: Path, monkeypatch: pytest.MonkeyPatch, token: AccessToken
) -> FastMCP:
    """Build the real MCP server with every call arriving under *token*."""
    monkeypatch.setattr(server_mod, "_current_access_token", lambda: token)
    return build_server(
        transport=Transport.STDIO,
        vault_path=vault,
        draft_llm_factory=lambda tier: lambda prompt: "ignored",
    )


@pytest.mark.parametrize("ceiling", ["intimate", "all", "bogus-tier"])
def test_a_refused_tier_cap_attempt_shows_as_refused(
    vault: Path, monkeypatch: pytest.MonkeyPatch, ceiling: str
) -> None:
    """An agent reaching above ``personal`` over MCP is on the card as refused.

    Driven end to end: the real server refuses the call before dispatch, audits
    it, and the route projects that entry. The requested ceiling never reaches
    the log, so the row says what was asked for and nothing about how high.
    """
    server = _remote_server(vault, monkeypatch, _remote_token("openclaw"))
    asyncio.run(server.call_tool("creek.wheel", {"privacy_tier_ceiling": ceiling}))
    assert _rows(_get(vault)) == [
        {
            "consumer": "openclaw",
            "tool": "creek.wheel",
            "at": _rows(_get(vault))[0]["at"],
            "outcome": "refused",
        }
    ]
    assert ceiling not in _log_path(vault).read_text(encoding="utf-8")


def test_an_out_of_profile_call_shows_as_refused(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read-only agent asking to re-sort the vault is the refused row A4 shows."""
    server = _remote_server(
        vault, monkeypatch, _remote_token("openclaw", Grant.READ.value)
    )
    asyncio.run(server.call_tool("creek.link", {"stage": "temporal"}))
    rows = _rows(_get(vault))
    assert [(row["tool"], row["outcome"]) for row in rows] == [
        ("creek.link", "refused")
    ]


def test_a_dispatched_mcp_call_shows_as_admitted(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ordinary case: the call ran, so its row is admitted."""
    server = _remote_server(
        vault, monkeypatch, _remote_token("openclaw", UNGATED_SCOPE)
    )
    asyncio.run(server.call_tool("creek.wheel", {"privacy_tier_ceiling": "open"}))
    rows = _rows(_get(vault))
    assert [(row["consumer"], row["tool"], row["outcome"]) for row in rows] == [
        ("openclaw", "creek.wheel", "admitted")
    ]


def test_a_jwt_agent_is_shown_under_its_subject(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An OAuth agent (#1963) is on the card under its ``sub``, admitted and refused.

    The token is minted and verified by the real resource-server verifier, so
    the stamp is whatever that verifier hands the audit log: the ``sub``, never
    the registered OAuth ``client_id``, and never any other claim.
    """
    verified = asyncio.run(agent_verifier().verify_token(mint()))
    assert verified is not None
    server = _remote_server(vault, monkeypatch, verified)
    asyncio.run(server.call_tool("creek.wheel", {"privacy_tier_ceiling": "open"}))
    asyncio.run(server.call_tool("creek.link", {"stage": "temporal"}))
    response = _get(vault)
    assert [
        (row["consumer"], row["tool"], row["outcome"]) for row in _rows(response)
    ] == [
        (AGENT_SUBJECT, "creek.link", "refused"),
        (AGENT_SUBJECT, "creek.wheel", "admitted"),
    ]
    assert AGENT_CLIENT_ID not in response.text


# --------------------------------------------------------------------------- #
# Consumers: nothing hidden
# --------------------------------------------------------------------------- #


def test_the_apps_own_v1_calls_are_included_under_its_stamp(vault: Path) -> None:
    """The ``adepthood`` service bearer's calls are on the card, labelled as it."""
    with client(vault_path=vault) as test_client:
        assert test_client.get(WHEEL_PATH, headers=headers()).status_code == _OK
        response = test_client.get(AGENT_ACTIVITY_PATH, headers=headers())
    assert [(row["consumer"], row["tool"]) for row in _rows(response)] == [
        (CONSUMER, "creek.wheel")
    ]


def test_every_consumer_is_visible_to_every_caller(vault: Path) -> None:
    """Nothing is hidden by consumer: another bearer sees the same rows."""
    _write_lines(
        vault,
        [
            _entry(consumer=CONSUMER, tool="creek.journal"),
            _entry(consumer="openclaw", tool="creek.mine"),
        ],
    )
    as_app = _rows(_get(vault))
    as_other = _rows(_get(vault, token=OTHER_TOKEN))
    assert as_app == as_other
    assert {row["consumer"] for row in as_app} == {CONSUMER, "openclaw"}
    assert OTHER_CONSUMER not in {row["consumer"] for row in as_app}


def test_reading_the_activity_appends_nothing_to_the_trail(vault: Path) -> None:
    """The route is not itself a call the card would then show."""
    _write_lines(vault, [_entry()])
    before = _log_path(vault).read_bytes()
    _get(vault)
    _get(vault)
    assert _log_path(vault).read_bytes() == before


# --------------------------------------------------------------------------- #
# The chain is read, never rewritten, never verified per request
# --------------------------------------------------------------------------- #


def test_a_real_chain_is_byte_identical_and_still_verifies_after_a_read(
    vault: Path,
) -> None:
    """Reading leaves the hash chain exactly as the writers left it."""
    log = MCPAuditLog(vault)
    for index in range(3):
        log.append(
            tool="creek.mine",
            args={"query": f"seed {index}", "body": _LONG_STRING},
            tier_ceiling=TierCeiling.OPEN,
            consumer="openclaw",
        )
    before = _log_path(vault).read_bytes()
    assert len(_rows(_get(vault))) == 3
    assert _log_path(vault).read_bytes() == before
    verify_mcp_audit_chain(vault)


def test_the_chain_verifier_is_not_run_per_request(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verification walks and hashes every line; a display read must not."""

    def _forbidden(_vault: Path) -> None:
        raise AssertionError("verify_mcp_audit_chain ran on a display read")

    monkeypatch.setattr(audit_mod, "verify_mcp_audit_chain", _forbidden)
    _write_lines(vault, [_entry()])
    assert len(_rows(_get(vault))) == 1


def test_a_tampered_chain_is_still_projected(vault: Path) -> None:
    """No verification means a broken chain still renders; that is the trade."""
    _write_lines(vault, [_entry(), _entry(tool="creek.mine")])
    assert len(_rows(_get(vault))) == 2


# --------------------------------------------------------------------------- #
# limit
# --------------------------------------------------------------------------- #


def _many(count: int) -> list[str]:
    """Return *count* well-formed entries, one second apart, oldest first."""
    return [
        _entry(tool="creek.mine", at=_BASE_INSTANT + timedelta(seconds=index))
        for index in range(count)
    ]


def test_the_default_limit_is_fifty_newest_first(vault: Path) -> None:
    """With no ``limit`` the card gets the newest fifty, newest first."""
    _write_lines(vault, _many(AGENT_ACTIVITY_DEFAULT_LIMIT + 5))
    rows = _rows(_get(vault))
    assert len(rows) == AGENT_ACTIVITY_DEFAULT_LIMIT
    assert rows[0]["at"] == "2026-10-24T17:00:54Z"
    assert rows[-1]["at"] == "2026-10-24T17:00:05Z"


def test_a_limit_above_two_hundred_is_clamped(vault: Path) -> None:
    """``limit=500`` is two hundred rows, not a refusal."""
    _write_lines(vault, _many(AGENT_ACTIVITY_MAX_LIMIT + 10))
    assert len(_rows(_get(vault, query="?limit=500"))) == AGENT_ACTIVITY_MAX_LIMIT


def test_a_small_limit_returns_the_newest_rows(vault: Path) -> None:
    """``limit=2`` is the two most recent calls."""
    _write_lines(vault, _many(4))
    rows = _rows(_get(vault, query="?limit=2"))
    assert [row["at"] for row in rows] == [
        "2026-10-24T17:00:03Z",
        "2026-10-24T17:00:02Z",
    ]


@pytest.mark.parametrize(
    "query",
    [
        "?limit=0",
        "?limit=-1",
        "?limit=ten",
        "?limit=",
        "?limit=1&limit=2",
        "?since=yesterday",
        "?since=2026-10-24T17:00:00",
        "?since=",
        "?args_summary=1",
        "?consumer=openclaw",
    ],
    ids=[
        "zero",
        "negative",
        "word",
        "blank-limit",
        "repeated",
        "since-word",
        "since-naive",
        "since-blank",
        "unknown-key",
        "consumer-filter",
    ],
)
def test_a_malformed_query_is_invalid_request(vault: Path, query: str) -> None:
    """Every query the published schema does not admit is the one ``422``."""
    _write_lines(vault, [_entry()])
    response = _get(vault, query=query)
    assert response.status_code == _INVALID_REQUEST
    assert envelope(response)["code"] == ErrorCode.INVALID_REQUEST.value
    assert "creek.draft" not in response.text


# --------------------------------------------------------------------------- #
# since
# --------------------------------------------------------------------------- #


def test_since_older_than_the_log_returns_from_the_start(vault: Path) -> None:
    """A ``since`` before the first entry is every entry, not an error."""
    _write_lines(vault, _many(3))
    rows = _rows(_get(vault, query="?since=2001-01-01T00:00:00Z"))
    assert len(rows) == 3


def test_since_keeps_only_calls_at_or_after_it(vault: Path) -> None:
    """``since`` is an inclusive lower bound on ``at``."""
    _write_lines(vault, _many(4))
    rows = _rows(_get(vault, query="?since=2026-10-24T17:00:02Z"))
    assert [row["at"] for row in rows] == [
        "2026-10-24T17:00:03Z",
        "2026-10-24T17:00:02Z",
    ]


def test_since_compares_against_the_full_precision_instant(vault: Path) -> None:
    """A call after ``since`` within the same second is kept, not truncated away."""
    _write_lines(vault, [_entry(at=_BASE_INSTANT + timedelta(microseconds=700_000))])
    rows = _rows(_get(vault, query="?since=2026-10-24T17:00:00.500000Z"))
    assert [row["at"] for row in rows] == ["2026-10-24T17:00:00Z"]


def test_since_accepts_an_offset_and_normalises_to_utc(vault: Path) -> None:
    """An offset ``since`` names the same instant; ``at`` is always UTC."""
    _write_lines(vault, _many(3))
    rows = _rows(_get(vault, query="?since=2026-10-24T19:00:01%2B02:00"))
    assert [row["at"] for row in rows] == [
        "2026-10-24T17:00:02Z",
        "2026-10-24T17:00:01Z",
    ]


def test_since_in_the_future_is_an_empty_page(vault: Path) -> None:
    """Nothing has happened yet after a future instant."""
    _write_lines(vault, _many(2))
    assert _rows(_get(vault, query="?since=2100-01-01T00:00:00Z")) == []


def test_an_offset_timestamp_in_the_log_is_rendered_in_utc(vault: Path) -> None:
    """``at`` is UTC whatever offset the entry was stamped with."""
    offset = datetime(2026, 10, 24, 20, 30, 0, tzinfo=UTC).astimezone(
        datetime.fromisoformat("2026-10-24T00:00:00+05:30").tzinfo
    )
    _write_lines(vault, [_entry(at=offset)])
    assert _rows(_get(vault))[0]["at"] == "2026-10-24T20:30:00Z"


# --------------------------------------------------------------------------- #
# Malformed lines are skipped and counted
# --------------------------------------------------------------------------- #


_MALFORMED: Final[tuple[str, ...]] = (
    "{not json",
    "[]",
    '"a bare string"',
    json.dumps({"consumer": "acct_91", "timestamp": "2026-10-24T17:00:00+00:00"}),
    _entry(tool=7),
    _entry(tool="creek.draft please write about my divorce"),
    _entry(tool="x" * 200),
    _entry(consumer=None),
    _entry(consumer=""),
    _entry(consumer="evil\nconsumer"),
    _entry(consumer="c" * 300),
    json.dumps({"tool": "creek.draft", "consumer": "acct_91"}),
    json.dumps(
        {"tool": "creek.draft", "consumer": "acct_91", "timestamp": "yesterday"}
    ),
    json.dumps(
        {"tool": "creek.draft", "consumer": "acct_91", "timestamp": 1_700_000_000}
    ),
    json.dumps(
        {
            "tool": "creek.draft",
            "consumer": "acct_91",
            "timestamp": "2026-10-24T17:00:00",
        }
    ),
)
"""Lines the projection cannot read. Each is skipped and counted, never rendered."""


def test_malformed_lines_are_skipped_and_counted(vault: Path) -> None:
    """One bad line cannot take the card down; it is counted instead."""
    _write_lines(vault, [_entry(tool="creek.mine"), *_MALFORMED, _entry()])
    response = _get(vault)
    body = envelope(response)
    assert body["skipped"] == len(_MALFORMED)
    assert [row["tool"] for row in body["rows"]] == ["creek.draft", "creek.mine"]
    assert "divorce" not in response.text
    assert "evil" not in response.text


def test_a_line_that_is_not_utf8_is_skipped(vault: Path) -> None:
    """Undecodable bytes are one more malformed line, not a ``500``."""
    path = _log_path(vault)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xff\xfe not utf-8\n" + _entry().encode("utf-8") + b"\n")
    body = envelope(_get(vault))
    assert body["skipped"] == 1
    assert len(body["rows"]) == 1


def test_a_torn_trailing_line_is_skipped(vault: Path) -> None:
    """A line caught mid-append (no newline yet) is skipped, not fatal."""
    path = _log_path(vault)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_entry() + "\n" + _entry()[:40], encoding="utf-8")
    body = envelope(_get(vault))
    assert body["skipped"] == 1
    assert len(body["rows"]) == 1


def test_skipped_counts_the_whole_log_whatever_since_says(vault: Path) -> None:
    """A malformed line has no trustworthy instant to filter on, so it counts."""
    _write_lines(vault, ["{not json", *_many(2)])
    body = envelope(_get(vault, query="?since=2100-01-01T00:00:00Z"))
    assert body == {**body, "rows": [], "skipped": 1}


# --------------------------------------------------------------------------- #
# Availability and the version gate
# --------------------------------------------------------------------------- #


def test_an_uninitialised_vault_is_unavailable(tmp_path: Path) -> None:
    """No ``00-Creek-Meta`` means no vault to read a trail from."""
    response = _get(tmp_path / "not-a-vault")
    assert response.status_code == _UNAVAILABLE
    assert envelope(response)["code"] == ErrorCode.UNAVAILABLE.value


def test_an_unreadable_log_is_unavailable(vault: Path) -> None:
    """A log that cannot be opened needs an operator, and says so."""
    _log_path(vault).mkdir(parents=True)
    response = _get(vault)
    assert response.status_code == _UNAVAILABLE
    assert envelope(response)["code"] == ErrorCode.UNAVAILABLE.value


def test_an_unconfigured_vault_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No vault at all is the published ``unavailable``."""
    monkeypatch.setattr(activity_mod, "configured_vault", lambda _request: None)
    response = _get(None)
    assert response.status_code == _UNAVAILABLE


def test_the_capability_is_published_at_the_new_minor() -> None:
    """``agent-activity`` is a contract-minor addition, not a silent widening."""
    assert CAPABILITY_SINCE_MINOR[Capability.AGENT_ACTIVITY] == CONTRACT_MINOR
    assert Capability.AGENT_ACTIVITY.value == "agent-activity"


def test_a_caller_below_the_minor_is_refused(vault: Path) -> None:
    """A ``0.21`` consumer never negotiated the route and cannot reach it."""
    response = _get(vault, minor=_PREVIOUS_MINOR)
    assert response.status_code == _INCOMPATIBLE_VERSION
    assert envelope(response)["code"] == ErrorCode.INCOMPATIBLE_VERSION.value


@pytest.mark.parametrize(
    ("minor", "advertised"),
    [(_PREVIOUS_MINOR, False), (CONTRACT_MINOR, True)],
    ids=["previous-minor", "current-minor"],
)
def test_the_handshake_advertises_it_only_from_the_minor(
    vault: Path, minor: str, advertised: bool
) -> None:
    """Both halves of the since-minor rule: told about it iff served it."""
    with client(vault_path=vault) as test_client:
        response = test_client.get(CAPABILITIES_PATH, headers=headers(minor=minor))
    capabilities = envelope(response)["capabilities"]
    assert (Capability.AGENT_ACTIVITY.value in capabilities) is advertised


# --------------------------------------------------------------------------- #
# The content-free property
# --------------------------------------------------------------------------- #


_ARG_VALUES = st.one_of(
    st.text(min_size=8, max_size=200),
    st.integers(),
    st.lists(st.text(max_size=20), max_size=5),
    st.dictionaries(st.text(min_size=1, max_size=20), st.text(), max_size=3),
)
_ARGS = st.dictionaries(
    st.text(alphabet=f"{string.ascii_lowercase}_", min_size=8, max_size=24),
    _ARG_VALUES,
    max_size=6,
)


def _forbidden_strings(args: dict[str, Any]) -> set[str]:
    """Return every key and long string *args* could carry into a response."""
    found: set[str] = set(args)
    for value in args.values():
        if isinstance(value, str):
            found.add(value)
        elif isinstance(value, dict):
            found.update(str(key) for key in value)
    return {item for item in found if len(item) >= 8}


@settings(
    max_examples=60,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(calls=st.lists(_ARGS, min_size=1, max_size=5))
def test_no_argument_key_or_value_ever_leaks(
    tmp_path_factory: pytest.TempPathFactory, calls: list[dict[str, Any]]
) -> None:
    """Whatever a tool was called with, the response names none of it.

    The entries are written by the real :class:`MCPAuditLog`, so the
    ``args_summary`` that reaches disk is the real summariser's output: short
    strings verbatim, long ones as lengths, dict keys as key sets. The response
    is then searched for every argument key and every string that could have
    survived summarisation.
    """
    vault = seed_vault(tmp_path_factory.mktemp("vault"))
    log = MCPAuditLog(vault)
    forbidden: set[str] = set()
    for args in calls:
        log.append(
            tool="creek.mine",
            args=args,
            tier_ceiling=TierCeiling.PERSONAL,
            consumer="openclaw",
            created_path="07-Voice/Drafts/property.md",
            affected_fragment_ids=["frag-3f9a1c7e40b2"],
        )
        forbidden |= _forbidden_strings(args)
    page = activity_mod.read_activity(vault, limit=AGENT_ACTIVITY_MAX_LIMIT, since=None)
    assert page is not None
    rendered = json.dumps(
        [row.model_dump(mode="json") for row in page.rows], ensure_ascii=False
    )
    assert all(set(row.model_dump()) == _ROW_KEYS for row in page.rows)
    assert len(page.rows) == len(calls)
    for item in forbidden:
        assert json.dumps(item, ensure_ascii=False)[1:-1] not in rendered
    for leaked in ("property.md", "frag-3f9a1c7e40b2", "args_summary", "entry_hash"):
        assert leaked not in rendered


# --------------------------------------------------------------------------- #
# The published vocabulary
# --------------------------------------------------------------------------- #


def test_the_unregistered_tool_stamp_satisfies_the_published_pattern() -> None:
    """The server's ``<unregistered>`` stamp is a tool name the row can carry."""
    import re

    assert re.fullmatch(AGENT_ACTIVITY_TOOL_PATTERN, UNREGISTERED_TOOL_AUDIT_NAME)


def test_the_published_query_bounds_are_the_runtime_ones() -> None:
    """Fifty by default, two hundred at most — the numbers A4 sizes its card by."""
    assert AGENT_ACTIVITY_DEFAULT_LIMIT == 50
    assert AGENT_ACTIVITY_MAX_LIMIT == 200
