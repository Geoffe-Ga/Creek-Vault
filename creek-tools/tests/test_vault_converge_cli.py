"""The operator's vault-converge one-shot on the worker and the pilot (#1869).

``--converge-vault-machines`` composes exactly like ``--roll-mcp-oauth``: the
worker's (or pilot's) usual options scope the run to one Fly organization and
one policy image, the run reads the store's ownership-bound allocation set,
and the process exits with a status an operator script can branch on.  It is
a dry run unless ``--converge-apply`` is given.
"""

from __future__ import annotations

import argparse
import json
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from creek_mcp.provisioning import pilot_cli, vault_converge, worker_cli
from creek_mcp.provisioning.models import CustodyMode
from creek_mcp.provisioning.store import ProvisioningStore
from tests.fly_support import FakeFlyAPI, FakeSecretManager, fly_driver

if TYPE_CHECKING:
    from pathlib import Path


def _ready(store: ProvisioningStore, activation_id: str, consumer: str) -> str:
    """Create one provider-managed ready allocation; return its allocation id."""
    job = store.submit(activation_id, consumer, requester_identity="adepthood")
    claim = store.claim_next()
    assert claim is not None
    allocation_id = f"fly-{activation_id[-3:]:0>24}"
    store.complete_create(
        job.job_id,
        claim.lease_token,
        allocation_id,
        handoff=lambda: None,
        custody_mode=CustodyMode.PROVIDER_MANAGED,
    )
    return allocation_id


def test_the_store_lists_every_routable_allocation_in_order(tmp_path: Path) -> None:
    """The operator listing is the routing query without an owner filter."""
    store = ProvisioningStore(tmp_path / "jobs.sqlite3")
    second = _ready(store, "activation-list-002", "user-002")
    first = _ready(store, "activation-list-001", "user-001")
    legacy = store.submit("activation-legacy", "user-003", requester_identity="a")
    claim = store.claim_next()
    assert claim is not None
    store.complete_create(
        legacy.job_id,
        claim.lease_token,
        "fly-legacy",
        handoff=lambda: None,
        custody_mode=CustodyMode.WRAPPED_ARTIFACT_ONLY,
    )

    listed = store.list_routable_allocations()

    assert [a.provider_allocation_id for a in listed] == [first, second]
    assert [a.consumer_identity for a in listed] == ["user-001", "user-002"]
    assert listed[0] == store.get_routable_allocation("adepthood", "user-001")


def _parser() -> argparse.ArgumentParser:
    """Return a parser carrying only the one-shot selectors."""
    parser = argparse.ArgumentParser()
    worker_cli.add_mcp_oauth_roll_argument(parser)
    return parser


def test_the_converge_is_a_dry_run_bounded_to_one_by_default() -> None:
    """Without ``--converge-apply`` nothing is mutated; the batch is one."""
    parser = _parser()
    args = parser.parse_args(["--converge-vault-machines"])

    assert worker_cli.selects_one_shot(args, parser) is True
    assert args.converge_apply is False
    assert args.converge_rollback is False
    assert args.converge_limit == 1


@pytest.mark.parametrize(
    "argv",
    [
        ["--converge-apply"],
        ["--converge-rollback"],
        ["--converge-limit", "3"],
        ["--converge-journal", "journal.json"],
        ["--converge-vault-machines", "--converge-apply"],
        ["--converge-vault-machines", "--converge-rollback"],
    ],
)
def test_incomplete_converge_options_are_refused(argv: list[str]) -> None:
    """A modifier without the selector, or a mutation without a journal, errors."""
    parser = _parser()
    args = parser.parse_args(argv)

    with pytest.raises(SystemExit):
        worker_cli.selects_one_shot(args, parser)


def test_the_converge_excludes_the_other_one_shots() -> None:
    """It cannot be combined with ``--roll-mcp-oauth``."""
    with pytest.raises(SystemExit):
        _parser().parse_args(
            ["--converge-vault-machines", "--roll-mcp-oauth", "a", "b"]
        )


def test_no_selector_runs_no_one_shot() -> None:
    """The worker's ordinary claim loop is unaffected."""
    parser = _parser()

    assert worker_cli.selects_one_shot(parser.parse_args([]), parser) is False


def test_the_run_reads_the_store_and_its_records_read_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The one-shot lists the store and converges through read-only records."""
    api = FakeFlyAPI()
    driver = fly_driver(api, FakeSecretManager(set()), fly_replay_enabled=True)
    swapped: list[object] = []

    def _read_only(state_directory: Path, *, master_key_file: Path) -> object:
        swapped.append((state_directory, master_key_file))
        return FakeSecretManager(set())

    monkeypatch.setattr(worker_cli, "ReadOnlyFlySecretManager", _read_only)
    args = argparse.Namespace(
        database=tmp_path / "jobs.sqlite3",
        secret_state_directory=tmp_path / "state",
        secret_master_key_file=tmp_path / "master.key",
        converge_rollback=False,
        converge_apply=False,
        converge_limit=1,
        converge_journal=None,
    )

    status = worker_cli.run_vault_converge(args, driver)

    assert status == vault_converge.EXIT_CURRENT
    assert swapped == [(tmp_path / "state", tmp_path / "master.key")]
    document = json.loads(capsys.readouterr().out)
    assert document == {
        "allocations": [],
        "applied": False,
        "counts": {},
        "limit_reached": False,
        "mode": "converge",
    }


class _ClosingBundle:
    """A composed bundle double that records its close and refuses to serve."""

    def __init__(self) -> None:
        self.driver: object = None
        self.secrets: object = None
        self.closed = False

    @property
    def worker(self) -> _ClosingBundle:
        return self

    @property
    def supervisor(self) -> Any:
        raise AssertionError("a one-shot converge must not start the supervisor")

    def close(self) -> None:
        self.closed = True


@pytest.mark.parametrize("module", ["worker", "pilot"])
def test_main_runs_only_the_converge_and_exits_with_its_status(
    module: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--converge-vault-machines`` converges, closes, and claims nothing."""
    bundle = _ClosingBundle()
    target = worker_cli if module == "worker" else pilot_cli
    monkeypatch.setattr(target, "build_parser", _parser)
    monkeypatch.setattr(target, "compose", lambda *_: bundle)
    monkeypatch.setattr(
        worker_cli, "run_vault_converge", lambda *_: vault_converge.EXIT_RETRY
    )

    with pytest.raises(SystemExit) as raised:
        target.main(["--converge-vault-machines"])

    assert raised.value.code == vault_converge.EXIT_RETRY
    assert bundle.closed is True


def test_the_worker_parser_offers_the_converge(tmp_path: Path) -> None:
    """The production worker composes with the converge selector."""
    from tests.test_provisioning_worker_cli import _NOW as WORKER_NOW
    from tests.test_provisioning_worker_cli import _arguments

    parser = worker_cli.build_parser()
    args = parser.parse_args(
        [
            *_arguments(tmp_path),
            "--converge-vault-machines",
            "--converge-apply",
            "--converge-journal",
            str(tmp_path / "journal.json"),
            "--converge-limit",
            "2",
        ]
    )
    assert worker_cli.selects_one_shot(args, parser) is True
    bundle = worker_cli.compose(
        args,
        parser,
        clock=lambda: WORKER_NOW,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(404, request=request)
        ),
    )
    bundle.close()
    assert args.converge_limit == 2
