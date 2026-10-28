"""A managed vault's LLM classification job falls back to rules, and says why.

A managed vault ships with no local model and refuses cloud consent at boot, so
an ``llm`` classification job that arrives without a writer's request-scoped
key has no route at all. Before this module every such job ended ``failed``
with a null result and nothing in the log: a production vault ran 64 of them
and never classified a single fragment, while the model-free ``rules`` pass
would have classified all of them.

Two behaviours are pinned here:

* On a managed vault, an ``llm`` job whose classifier is unavailable runs the
  ``rules`` pass instead and reports ``method: rules`` -- the field already
  documents itself as "the classifier that ran". A self-hosted vault keeps the
  old refusal: its operator configured a model and should hear that it is down.
* A failed job records *why* on its private on-disk record (``failure_reason``)
  -- the refusal's fixed text, an exception's type name, or a lost worker --
  while the public ``/v1/jobs`` body stays exactly ``failed`` plus ``null``.

The unreachable provider is real, not mocked: the vault config points Ollama at
the TCP discard port, so the engine's own availability probe refuses.
"""

from __future__ import annotations

import json
import time
from threading import Event
from typing import TYPE_CHECKING, Any, Final

import frontmatter
import pytest

from creek.classify.llm.consent import MANAGED_VAULT_ENV
from creek.models import Frequency
from creek_mcp.httpapi import pipeline as pipeline_module
from creek_mcp.httpapi.jobs import StoredJob
from creek_mcp.tier_ceiling import TierCeiling
from creek_mcp.tools.classify import classify_tool
from tests.v1_api_support import CLASSIFICATIONS_PATH, client, headers, seed_vault

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from starlette.testclient import TestClient

_UNREACHABLE_OLLAMA_CONFIG: Final[str] = (
    "llm:\n"
    "  default:\n"
    "    provider: ollama\n"
    "    model: qwen3:8b\n"
    "    ollama_url: http://127.0.0.1:9\n"
)
"""A vault config whose only model cannot be reached (port 9 is discard)."""

_F3_BODY: Final[str] = (
    "The team took power and dominance and control and conquest of the release.\n"
)
"""Prose carrying four ``F3`` rules signals, so a rules pass yields a real value."""


def _write_fragment(vault: Path, name: str, body: str = _F3_BODY) -> Path:
    """Write one personal-tier fragment straight to disk.

    Args:
        vault: The vault root.
        name: The fragment's file stem and id suffix.
        body: The fragment body.

    Returns:
        The written path.
    """
    notes = vault / "01-Fragments" / "Notes"
    notes.mkdir(parents=True, exist_ok=True)
    target = notes / f"{name}.md"
    post = frontmatter.Post(
        content=body,
        type="fragment",
        id=f"frag-{name}",
        title=name,
        source={"platform": "markdown", "author": "self"},
        privacy_tier="personal",
    )
    target.write_text(frontmatter.dumps(post), encoding="utf-8")
    return target


def _frequency_of(path: Path) -> str:
    """Return the primary frequency recorded on *path*.

    Args:
        path: A fragment file.

    Returns:
        The raw ``frequency.primary`` value, or ``"unclassified"`` when absent.
    """
    metadata = frontmatter.load(str(path)).metadata
    frequency = metadata.get("frequency") or {}
    return str(frequency.get("primary", Frequency.UNCLASSIFIED.value))


def _stored_job(vault: Path, job_id: str) -> dict[str, Any]:
    """Return the raw private record of *job_id*, as written on disk.

    Read as bytes rather than through the jobs module so a reader that
    defaulted a missing field could not make an assertion pass.

    Args:
        vault: The vault root.
        job_id: The job's UUID.

    Returns:
        The decoded record.
    """
    path = vault / "00-Creek-Meta" / "State" / "jobs" / f"{job_id}.json"
    loaded: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return loaded


def _await_terminal(api: TestClient, job_id: str) -> dict[str, Any]:
    """Poll ``/v1/jobs/{job_id}`` until it leaves ``queued``/``running``.

    Args:
        api: The client.
        job_id: The job's UUID.

    Returns:
        The terminal status body.
    """
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        body: dict[str, Any] = api.get(f"/v1/jobs/{job_id}", headers=headers()).json()
        if body["state"] in {"succeeded", "failed"}:
            return body
        time.sleep(0.01)
    pytest.fail("pipeline job did not reach a terminal state")


@pytest.fixture(name="vault")
def _vault(tmp_path: Path) -> Path:
    """Return a seeded vault whose configured model is unreachable.

    Args:
        tmp_path: pytest's per-test directory.

    Returns:
        The vault root.
    """
    root = seed_vault(tmp_path / "vault")
    (root / "00-Creek-Meta" / "creek_config.yaml").write_text(
        _UNREACHABLE_OLLAMA_CONFIG, encoding="utf-8"
    )
    return root


@pytest.fixture(name="api")
def _api(vault: Path) -> Iterator[TestClient]:
    """Yield a client over an app serving *vault*.

    Args:
        vault: The vault root.

    Yields:
        A ``TestClient`` that never opens a socket.
    """
    with client(vault_path=vault) as opened:
        yield opened


# --------------------------------------------------------------------------- #
# The tool: fallback is opt-in and only for an unavailable classifier
# --------------------------------------------------------------------------- #


def test_an_unavailable_llm_pass_is_still_refused_without_the_fallback(
    vault: Path,
) -> None:
    """The default call keeps the refusal every existing consumer relies on."""
    fragment = _write_fragment(vault, "plain")

    result = classify_tool(
        vault_path=vault, method="llm", privacy_tier_ceiling=TierCeiling.PERSONAL
    )

    assert result["status"] == "refused"
    assert "unavailable" in str(result["reason"])
    assert _frequency_of(fragment) == Frequency.UNCLASSIFIED.value


def test_the_fallback_runs_the_rules_pass_and_says_so(vault: Path) -> None:
    """An unavailable classifier under the fallback classifies with rules."""
    fragment = _write_fragment(vault, "signal")

    result = classify_tool(
        vault_path=vault,
        method="llm",
        privacy_tier_ceiling=TierCeiling.PERSONAL,
        fallback_to_rules=True,
    )

    assert result["status"] == "ok"
    assert result["fallback_from"] == "llm"
    assert "unavailable" in str(result["fallback_reason"])
    assert result["classified"] == 1
    assert result["inference_source"] is None
    assert _frequency_of(fragment) == Frequency.F3.value
    assert frontmatter.load(str(fragment)).metadata["classification_method"] == "rules"


def test_a_successful_pass_carries_no_fallback_fields(vault: Path) -> None:
    """A rules pass that needed no fallback keeps its exact key set."""
    _write_fragment(vault, "signal")

    result = classify_tool(
        vault_path=vault,
        method="rules",
        privacy_tier_ceiling=TierCeiling.PERSONAL,
        fallback_to_rules=True,
    )

    assert result["status"] == "ok"
    assert "fallback_from" not in result
    assert "fallback_reason" not in result


# --------------------------------------------------------------------------- #
# The job runner: managed vaults fall back, self-hosted vaults record why
# --------------------------------------------------------------------------- #


def test_a_managed_vault_llm_job_classifies_with_rules(
    api: TestClient, vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The production failure: a keyless ``llm`` job on a managed vault."""
    monkeypatch.setenv(MANAGED_VAULT_ENV, "1")
    fragment = _write_fragment(vault, "signal")

    accepted = api.post(CLASSIFICATIONS_PATH, json={"method": "llm"}, headers=headers())
    body = _await_terminal(api, accepted.json()["job_id"])

    assert body["state"] == "succeeded"
    assert body["result"]["method"] == "rules"
    assert body["result"]["classified"] == 1
    assert _frequency_of(fragment) == Frequency.F3.value


def test_a_self_hosted_vault_llm_job_fails_and_records_the_refusal(
    api: TestClient, vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without the managed marker the job still fails -- but now says why."""
    monkeypatch.delenv(MANAGED_VAULT_ENV, raising=False)
    fragment = _write_fragment(vault, "signal")

    accepted = api.post(CLASSIFICATIONS_PATH, json={"method": "llm"}, headers=headers())
    job_id = accepted.json()["job_id"]
    body = _await_terminal(api, job_id)

    assert body == {
        "status": "ok",
        "job_id": job_id,
        "state": "failed",
        "result": None,
        "failure_code": None,
    }
    record = _stored_job(vault, job_id)
    assert record["state"] == "failed"
    assert record["failure_reason"].startswith("refused: ")
    assert "unavailable" in record["failure_reason"]
    assert _frequency_of(fragment) == Frequency.UNCLASSIFIED.value


def test_a_crashed_job_records_only_the_exception_type(
    api: TestClient, vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An exception's message can carry fragment text, so only its type is kept."""
    marker = "private-title-from-an-intimate-fragment"

    def _explode(**_kwargs: object) -> dict[str, object]:
        raise RuntimeError(marker)

    monkeypatch.setattr(pipeline_module, "classify_tool", _explode)
    accepted = api.post(CLASSIFICATIONS_PATH, json={"method": "llm"}, headers=headers())
    job_id = accepted.json()["job_id"]
    body = _await_terminal(api, job_id)

    record = _stored_job(vault, job_id)
    assert body["result"] is None
    assert record["failure_reason"] == "exception: RuntimeError"
    assert marker not in json.dumps(record)
    assert marker not in json.dumps(body)


def test_a_lost_worker_is_recorded_as_lost(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A job orphaned by a restart or autostop names that, not a blank."""
    started = Event()
    release = Event()

    def _blocked(**_kwargs: object) -> dict[str, object]:
        started.set()
        assert release.wait(timeout=2.0)
        return {"status": "refused", "tool": "creek.classify", "reason": "late"}

    monkeypatch.setattr(pipeline_module, "classify_tool", _blocked)
    with client(vault_path=vault) as first:
        accepted = first.post(
            CLASSIFICATIONS_PATH, json={"method": "llm"}, headers=headers()
        )
        assert started.wait(timeout=2.0)
        job_id = accepted.json()["job_id"]
        with client(vault_path=vault) as restarted:
            status = restarted.get(f"/v1/jobs/{job_id}", headers=headers())
        release.set()

    assert status.json()["state"] == "failed"
    assert _stored_job(vault, job_id)["failure_reason"] == "worker_lost"


def test_a_record_written_before_the_field_existed_still_loads() -> None:
    """The failed records already on disk carry no ``failure_reason``."""
    legacy = {
        "job_id": "78d567b6-3200-4bbb-bc14-a30724faf5b6",
        "consumer": "adepthood-user",
        "kind": "classification",
        "method": "llm",
        "retier": False,
        "ceiling": "personal",
        "state": "failed",
        "worker_id": "05ea22054d6d427f83cac562336ffb8e",
        "result": None,
    }

    record = StoredJob.model_validate(legacy)

    assert record.failure_reason is None
