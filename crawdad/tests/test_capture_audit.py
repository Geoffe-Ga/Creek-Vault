"""Tests for ``crawdad.capture_audit`` — the pre-#1052 capture remediation helper.

The tree under test always mixes directories the current gate would admit with
directories it would refuse, so every assertion is behavioural: what the audit
*reports* and what the purge *removes*, never merely that a symbol exists.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from crawdad import cli
from crawdad.capture_audit import (
    CaptureVerdict,
    apply_purge,
    audit_capture_tree,
    format_audit_report,
    format_purge_report,
    list_skipped_entries,
    plan_purge,
)
from crawdad.config import AttachmentConfig, CrawDadConfig

# Channel ids used across the fixtures. Real Discord snowflakes, not toy ints:
# the audit only resolves a digit label that could plausibly *be* a snowflake,
# so a 3-digit id would exercise a code path no real capture tree can produce.
ADMITTED_ID = 111111111111111111
INTIMATE_ID = 222222222222222222
ALL_TIER_ID = 333333333333333333
STRANGER_ID = 999999999999999999
NAMED_LABEL = "random-chat"

# A channel whose *name* is nothing but digits — legal on Discord and common
# ("2024", "420", "911"). Its directory label is that name, never its id.
NUMERIC_NAME = "2024"


def _record(msg_id: str, timestamp: str, author: str) -> dict[str, Any]:
    """Build one capture record in the writer's on-disk schema."""
    return {
        "id": msg_id,
        "timestamp": timestamp,
        "content": f"message {msg_id}",
        "author": {"name": author},
    }


def _write_channel(
    capture_dir: Path, label: str, records: list[dict[str, Any]], *, date: str
) -> Path:
    """Write *records* into ``<capture_dir>/<label>/<date>.jsonl``."""
    channel_dir = capture_dir / label
    channel_dir.mkdir(parents=True, exist_ok=True)
    (channel_dir / f"{date}.jsonl").write_text(
        "".join(json.dumps(rec) + "\n" for rec in records), encoding="utf-8"
    )
    return channel_dir


@pytest.fixture
def config(tmp_path: Path) -> CrawDadConfig:
    """A config allowlisting three channels, two of them tiered out of capture."""
    vault = tmp_path / "vault"
    vault.mkdir()
    return CrawDadConfig(
        discord_bot_token="t",
        vault_path=vault,
        allowed_user_ids=(1,),
        allowed_channel_ids=(ADMITTED_ID, INTIMATE_ID, ALL_TIER_ID),
        attachments=AttachmentConfig(
            channel_privacy_tiers={INTIMATE_ID: "intimate", ALL_TIER_ID: "all"}
        ),
    )


@pytest.fixture
def capture_dir(config: CrawDadConfig) -> Path:
    """A pre-fix capture tree: one admitted dir, three refused, one unresolved."""
    root = config.vault_path / config.capture_subpath
    _write_channel(
        root,
        str(ADMITTED_ID),
        [
            _record("1", "2026-01-02T10:00:00+00:00", "geoff"),
            _record("2", "2026-01-05T11:30:00+00:00", "geoff"),
        ],
        date="2026-01-02",
    )
    _write_channel(
        root,
        str(INTIMATE_ID),
        [_record("3", "2026-02-01T09:00:00+00:00", "geoff")],
        date="2026-02-01",
    )
    _write_channel(
        root,
        str(ALL_TIER_ID),
        [_record("4", "2026-02-02T09:00:00+00:00", "geoff")],
        date="2026-02-02",
    )
    _write_channel(
        root,
        str(STRANGER_ID),
        [
            _record("5", "2026-03-01T08:00:00+00:00", "stranger"),
            _record("6", "2026-03-04T08:00:00+00:00", "otherbot"),
        ],
        date="2026-03-01",
    )
    _write_channel(
        root,
        NAMED_LABEL,
        [_record("7", "2026-04-01T08:00:00+00:00", "stranger")],
        date="2026-04-01",
    )
    _write_channel(
        root,
        NUMERIC_NAME,
        [_record("8", "2026-05-01T08:00:00+00:00", "geoff")],
        date="2026-05-01",
    )
    return root


def _by_label(config: CrawDadConfig, capture_dir: Path) -> dict[str, Any]:
    """Audit the tree and index the results by channel label."""
    return {
        audit.label: audit
        for audit in audit_capture_tree(capture_dir=capture_dir, config=config)
    }


# --------------------------------------------------------------------------
# Audit
# --------------------------------------------------------------------------


def test_audit_covers_every_channel_directory(
    config: CrawDadConfig, capture_dir: Path
) -> None:
    """Every channel dir on disk appears in the audit, sorted by label."""
    audits = audit_capture_tree(capture_dir=capture_dir, config=config)

    assert [a.label for a in audits] == sorted(
        [
            str(ADMITTED_ID),
            str(INTIMATE_ID),
            str(ALL_TIER_ID),
            str(STRANGER_ID),
            NAMED_LABEL,
            NUMERIC_NAME,
        ]
    )


def test_audit_reports_record_count_and_date_range(
    config: CrawDadConfig, capture_dir: Path
) -> None:
    """A channel's record count and first/last timestamp come back exactly."""
    audit = _by_label(config, capture_dir)[str(ADMITTED_ID)]

    assert audit.record_count == 2
    assert audit.first_timestamp == "2026-01-02T10:00:00+00:00"
    assert audit.last_timestamp == "2026-01-05T11:30:00+00:00"


def test_audit_reports_distinct_author_names(
    config: CrawDadConfig, capture_dir: Path
) -> None:
    """Authors are the sorted distinct display names captured in the dir."""
    audit = _by_label(config, capture_dir)[str(STRANGER_ID)]

    assert audit.authors == ("otherbot", "stranger")


def test_audit_admits_an_allowlisted_default_tier_channel(
    config: CrawDadConfig, capture_dir: Path
) -> None:
    """An allowlisted channel with no tier override is ADMITTED at ``personal``."""
    audit = _by_label(config, capture_dir)[str(ADMITTED_ID)]

    assert audit.verdict is CaptureVerdict.ADMITTED
    assert audit.channel_id == ADMITTED_ID
    assert audit.declared_tier == "personal"


def test_audit_refuses_a_channel_absent_from_the_allowlist(
    config: CrawDadConfig, capture_dir: Path
) -> None:
    """A numeric dir whose id is not allowlisted is REFUSED, and says why."""
    audit = _by_label(config, capture_dir)[str(STRANGER_ID)]

    assert audit.verdict is CaptureVerdict.REFUSED
    assert "allowed_channel_ids" in audit.reason


def test_audit_refuses_an_intimate_channel(
    config: CrawDadConfig, capture_dir: Path
) -> None:
    """An allowlisted channel declared ``intimate`` is REFUSED on the tier."""
    audit = _by_label(config, capture_dir)[str(INTIMATE_ID)]

    assert audit.verdict is CaptureVerdict.REFUSED
    assert audit.declared_tier == "intimate"
    assert "intimate" in audit.reason


def test_audit_refuses_a_channel_declared_all(
    config: CrawDadConfig, capture_dir: Path
) -> None:
    """``all`` admits intimate content by definition, so it is REFUSED too."""
    audit = _by_label(config, capture_dir)[str(ALL_TIER_ID)]

    assert audit.verdict is CaptureVerdict.REFUSED
    assert audit.declared_tier == "all"


def test_audit_leaves_a_name_labelled_directory_unresolved(
    config: CrawDadConfig, capture_dir: Path
) -> None:
    """A dir named after the channel carries no id, so no verdict is derivable."""
    audit = _by_label(config, capture_dir)[NAMED_LABEL]

    assert audit.verdict is CaptureVerdict.UNRESOLVED
    assert audit.channel_id is None
    assert audit.declared_tier is None


def test_audit_leaves_an_all_digit_channel_name_unresolved(
    config: CrawDadConfig, capture_dir: Path
) -> None:
    """A channel *named* ``2024`` is a name, not id 2024 — so it gets no verdict.

    Discord snowflakes are ~17-19 digits; a short numeric name can never be a
    real channel id. Reading one as an id would look up 2024 in the allowlist,
    miss, and mark a possibly-allowlisted channel REFUSED.
    """
    audit = _by_label(config, capture_dir)[NUMERIC_NAME]

    assert audit.verdict is CaptureVerdict.UNRESOLVED
    assert audit.channel_id is None


def test_purge_never_auto_targets_an_all_digit_channel_name(
    config: CrawDadConfig, capture_dir: Path
) -> None:
    """The teeth of it: a digit-named channel survives a default ``--apply``.

    Misreading the name as an id would make it REFUSED, and refused dirs are
    purged with no ``--channel`` flag and no confirmation — silently destroying
    a legitimate channel's whole capture history.
    """
    plan = plan_purge(audits=audit_capture_tree(capture_dir=capture_dir, config=config))

    apply_purge(capture_dir=capture_dir, plan=plan)

    assert (capture_dir / NUMERIC_NAME).is_dir()
    assert NUMERIC_NAME not in {t.label for t in plan.targets}


def test_audit_resolves_a_label_with_a_leading_zero_as_a_name(
    config: CrawDadConfig, capture_dir: Path
) -> None:
    """A zero-padded digit string is a name — no snowflake ever leads with 0."""
    _write_channel(
        capture_dir,
        "0" + str(STRANGER_ID)[1:],
        [_record("9", "2026-06-01T08:00:00+00:00", "stranger")],
        date="2026-06-01",
    )

    audit = _by_label(config, capture_dir)["0" + str(STRANGER_ID)[1:]]

    assert audit.verdict is CaptureVerdict.UNRESOLVED


def test_audit_of_a_missing_capture_root_is_empty(
    config: CrawDadConfig, tmp_path: Path
) -> None:
    """No capture tree on disk audits to nothing rather than raising."""
    assert audit_capture_tree(capture_dir=tmp_path / "absent", config=config) == ()


def test_audit_skips_loose_files_at_the_capture_root(
    config: CrawDadConfig, capture_dir: Path
) -> None:
    """A stray file beside the channel dirs is not reported as a channel."""
    (capture_dir / "README.txt").write_text("not a channel", encoding="utf-8")

    assert "README.txt" not in _by_label(config, capture_dir)


def test_audit_tolerates_a_malformed_jsonl_line(
    config: CrawDadConfig, capture_dir: Path
) -> None:
    """A corrupt line is skipped; the surrounding records still count."""
    (capture_dir / str(ADMITTED_ID) / "2026-01-09.jsonl").write_text(
        "{not json\n" + json.dumps(_record("8", "2026-01-09T00:00:00+00:00", "geoff")),
        encoding="utf-8",
    )

    assert _by_label(config, capture_dir)[str(ADMITTED_ID)].record_count == 3


def test_audit_does_not_modify_the_capture_tree(
    config: CrawDadConfig, capture_dir: Path
) -> None:
    """Audit is read-only — every file survives byte-identical."""
    before = {path: path.read_bytes() for path in sorted(capture_dir.rglob("*.jsonl"))}

    audit_capture_tree(capture_dir=capture_dir, config=config)

    assert {
        path: path.read_bytes() for path in sorted(capture_dir.rglob("*.jsonl"))
    } == before


def test_audit_report_names_every_channel_and_its_verdict(
    config: CrawDadConfig, capture_dir: Path
) -> None:
    """The rendered report is usable on its own to decide what to purge."""
    audits = audit_capture_tree(capture_dir=capture_dir, config=config)

    report = format_audit_report(capture_dir=capture_dir, audits=audits)

    assert str(STRANGER_ID) in report
    assert NAMED_LABEL in report
    assert "refused" in report
    assert "unresolved" in report
    assert "stranger" in report  # the author column


def test_audit_report_states_the_whole_directory_limitation(
    config: CrawDadConfig, capture_dir: Path
) -> None:
    """The report says out loud that per-record purge is not possible."""
    audits = audit_capture_tree(capture_dir=capture_dir, config=config)

    report = format_audit_report(capture_dir=capture_dir, audits=audits)

    assert "whole channel directories" in report
    assert "allowed_user_ids" in report


def test_audit_report_counts_read_naturally(
    config: CrawDadConfig, capture_dir: Path
) -> None:
    """Counts are pluralised, so a one-record dir does not read ``1 records``."""
    audits = audit_capture_tree(capture_dir=capture_dir, config=config)

    report = format_audit_report(capture_dir=capture_dir, audits=audits)

    assert "1 record " in report
    assert "1 records" not in report
    assert "6 channel directories" in report


def test_audit_report_discloses_skipped_symlinks(
    config: CrawDadConfig, capture_dir: Path, tmp_path: Path
) -> None:
    """A symlink in the tree is named in the report, not silently omitted.

    The report claims to be sufficient on its own to decide a purge; silently
    dropping an entry that exists on disk would make that claim false.
    """
    (capture_dir / "linked").symlink_to(tmp_path, target_is_directory=True)
    audits = audit_capture_tree(capture_dir=capture_dir, config=config)

    report = format_audit_report(
        capture_dir=capture_dir,
        audits=audits,
        skipped=list_skipped_entries(capture_dir),
    )

    assert "linked" in report
    assert "symlink" in report.lower()


def test_list_skipped_entries_names_symlinks_and_loose_files(
    config: CrawDadConfig, capture_dir: Path, tmp_path: Path
) -> None:
    """Both kinds of non-channel entry are reported, and real dirs are not."""
    (capture_dir / "linked").symlink_to(tmp_path, target_is_directory=True)
    (capture_dir / "README.txt").write_text("notes", encoding="utf-8")

    skipped = list_skipped_entries(capture_dir)

    assert sorted(skipped) == ["README.txt", "linked"]


def test_list_skipped_entries_of_a_missing_root_is_empty(tmp_path: Path) -> None:
    """No capture tree means nothing was skipped."""
    assert list_skipped_entries(tmp_path / "absent") == ()


def test_audit_report_of_an_empty_tree_says_so(
    config: CrawDadConfig, tmp_path: Path
) -> None:
    """An empty tree renders a report rather than a blank string."""
    report = format_audit_report(capture_dir=tmp_path / "absent", audits=())

    assert "no channel directories" in report.lower()


# --------------------------------------------------------------------------
# Purge planning
# --------------------------------------------------------------------------


def test_purge_plan_targets_every_refused_channel(
    config: CrawDadConfig, capture_dir: Path
) -> None:
    """Refused channels are targeted with no operator request needed."""
    plan = plan_purge(audits=audit_capture_tree(capture_dir=capture_dir, config=config))

    assert sorted(t.label for t in plan.targets) == sorted(
        [str(INTIMATE_ID), str(ALL_TIER_ID), str(STRANGER_ID)]
    )


def test_purge_plan_never_targets_an_admitted_channel(
    config: CrawDadConfig, capture_dir: Path
) -> None:
    """The gate-admitted channel is absent from the default plan."""
    plan = plan_purge(audits=audit_capture_tree(capture_dir=capture_dir, config=config))

    assert str(ADMITTED_ID) not in {t.label for t in plan.targets}


def test_purge_plan_never_targets_an_unresolved_channel_by_default(
    config: CrawDadConfig, capture_dir: Path
) -> None:
    """An unresolved dir may hold legitimate data, so it needs an explicit ask."""
    plan = plan_purge(audits=audit_capture_tree(capture_dir=capture_dir, config=config))

    assert NAMED_LABEL not in {t.label for t in plan.targets}


def test_purge_plan_accepts_an_explicit_unresolved_channel(
    config: CrawDadConfig, capture_dir: Path
) -> None:
    """Naming an unresolved dir on the command line promotes it to a target."""
    plan = plan_purge(
        audits=audit_capture_tree(capture_dir=capture_dir, config=config),
        requested_labels=(NAMED_LABEL,),
    )

    assert NAMED_LABEL in {t.label for t in plan.targets}


def test_purge_plan_declines_an_explicit_admitted_channel(
    config: CrawDadConfig, capture_dir: Path
) -> None:
    """Even named explicitly, a gate-admitted channel is refused, with a reason."""
    plan = plan_purge(
        audits=audit_capture_tree(capture_dir=capture_dir, config=config),
        requested_labels=(str(ADMITTED_ID),),
    )

    assert str(ADMITTED_ID) not in {t.label for t in plan.targets}
    assert dict(plan.declined)[str(ADMITTED_ID)].startswith("the current capture gate")


def test_purge_plan_declines_an_unknown_label(
    config: CrawDadConfig, capture_dir: Path
) -> None:
    """A label with no directory is declined rather than silently ignored."""
    audits = audit_capture_tree(capture_dir=capture_dir, config=config)

    plan = plan_purge(audits=audits, requested_labels=("no-such-channel",))

    assert [t.label for t in plan.targets] == [
        t.label for t in plan_purge(audits=audits).targets
    ]
    assert "no such channel directory" in dict(plan.declined)["no-such-channel"]


def test_purge_plan_declines_a_traversal_label(
    config: CrawDadConfig, capture_dir: Path
) -> None:
    """A ``..`` label matches no audited dir and can never become a target."""
    plan = plan_purge(
        audits=audit_capture_tree(capture_dir=capture_dir, config=config),
        requested_labels=("../../etc",),
    )

    assert "../../etc" not in {t.label for t in plan.targets}
    assert "no such channel directory" in dict(plan.declined)["../../etc"]


def test_purge_plan_does_not_duplicate_an_explicit_refused_channel(
    config: CrawDadConfig, capture_dir: Path
) -> None:
    """Asking for an already-targeted refused dir is idempotent."""
    plan = plan_purge(
        audits=audit_capture_tree(capture_dir=capture_dir, config=config),
        requested_labels=(str(STRANGER_ID),),
    )

    assert [t.label for t in plan.targets].count(str(STRANGER_ID)) == 1
    assert plan.declined == ()


# --------------------------------------------------------------------------
# Purge application
# --------------------------------------------------------------------------


def test_apply_purge_removes_every_refused_directory(
    config: CrawDadConfig, capture_dir: Path
) -> None:
    """The refused dirs are gone from disk after the purge is applied."""
    plan = plan_purge(audits=audit_capture_tree(capture_dir=capture_dir, config=config))

    apply_purge(capture_dir=capture_dir, plan=plan)

    assert not (capture_dir / str(INTIMATE_ID)).exists()
    assert not (capture_dir / str(ALL_TIER_ID)).exists()
    assert not (capture_dir / str(STRANGER_ID)).exists()


def test_apply_purge_never_deletes_records_the_gate_would_admit(
    config: CrawDadConfig, capture_dir: Path
) -> None:
    """The acceptance pin: admitted records survive a purge byte-identical."""
    admitted = capture_dir / str(ADMITTED_ID)
    before = {p.name: p.read_bytes() for p in sorted(admitted.glob("*.jsonl"))}
    plan = plan_purge(
        audits=audit_capture_tree(capture_dir=capture_dir, config=config),
        requested_labels=(str(ADMITTED_ID), NAMED_LABEL),
    )

    apply_purge(capture_dir=capture_dir, plan=plan)

    assert admitted.is_dir()
    assert {p.name: p.read_bytes() for p in sorted(admitted.glob("*.jsonl"))} == before


def test_apply_purge_leaves_an_unrequested_unresolved_directory_alone(
    config: CrawDadConfig, capture_dir: Path
) -> None:
    """Unresolved data is preserved unless the operator names it."""
    plan = plan_purge(audits=audit_capture_tree(capture_dir=capture_dir, config=config))

    apply_purge(capture_dir=capture_dir, plan=plan)

    assert (capture_dir / NAMED_LABEL).is_dir()


def test_apply_purge_removes_an_explicitly_named_unresolved_directory(
    config: CrawDadConfig, capture_dir: Path
) -> None:
    """An explicitly named unresolved dir is deleted once ``--apply`` is given."""
    plan = plan_purge(
        audits=audit_capture_tree(capture_dir=capture_dir, config=config),
        requested_labels=(NAMED_LABEL,),
    )

    apply_purge(capture_dir=capture_dir, plan=plan)

    assert not (capture_dir / NAMED_LABEL).exists()


def test_apply_purge_returns_the_labels_it_deleted(
    config: CrawDadConfig, capture_dir: Path
) -> None:
    """The applied labels come back so a caller can report them exactly."""
    plan = plan_purge(audits=audit_capture_tree(capture_dir=capture_dir, config=config))

    deleted = apply_purge(capture_dir=capture_dir, plan=plan)

    assert sorted(deleted) == sorted(
        [str(INTIMATE_ID), str(ALL_TIER_ID), str(STRANGER_ID)]
    )


def test_apply_purge_refuses_a_target_outside_the_capture_root(
    config: CrawDadConfig, capture_dir: Path, tmp_path: Path
) -> None:
    """A hand-built plan pointing outside the root raises before deleting."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep", encoding="utf-8")
    audits = audit_capture_tree(capture_dir=capture_dir, config=config)
    stranger = next(a for a in audits if a.label == str(STRANGER_ID))
    escaped = replace(
        plan_purge(audits=(stranger,)),
        targets=(replace(stranger, label="../outside"),),
    )

    with pytest.raises(ValueError, match="capture root"):
        apply_purge(capture_dir=capture_dir, plan=escaped)

    assert (outside / "keep.txt").exists()
    assert (capture_dir / str(STRANGER_ID)).is_dir()


def test_apply_purge_never_follows_a_symlinked_channel_directory(
    config: CrawDadConfig, capture_dir: Path, tmp_path: Path
) -> None:
    """A symlink parked in the capture tree is neither audited nor deleted."""
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep", encoding="utf-8")
    (capture_dir / "8888").symlink_to(outside, target_is_directory=True)

    plan = plan_purge(audits=audit_capture_tree(capture_dir=capture_dir, config=config))
    apply_purge(capture_dir=capture_dir, plan=plan)

    assert (outside / "keep.txt").exists()
    assert "8888" not in {t.label for t in plan.targets}


def test_purge_report_dry_run_says_nothing_was_deleted(
    config: CrawDadConfig, capture_dir: Path
) -> None:
    """The dry-run report shows the targets and denies having deleted them."""
    plan = plan_purge(audits=audit_capture_tree(capture_dir=capture_dir, config=config))

    report = format_purge_report(capture_dir=capture_dir, plan=plan, applied=False)

    assert "DRY RUN" in report
    assert "--apply" in report
    assert str(STRANGER_ID) in report


def test_purge_report_applied_says_what_it_deleted(
    config: CrawDadConfig, capture_dir: Path
) -> None:
    """The applied report names the deleted dirs and warns it is irreversible."""
    plan = plan_purge(audits=audit_capture_tree(capture_dir=capture_dir, config=config))

    report = format_purge_report(capture_dir=capture_dir, plan=plan, applied=True)

    assert "DRY RUN" not in report
    assert "Deleted" in report
    assert str(INTIMATE_ID) in report


def test_purge_report_lists_declined_requests(
    config: CrawDadConfig, capture_dir: Path
) -> None:
    """A declined request is visible in the report, not swallowed."""
    plan = plan_purge(
        audits=audit_capture_tree(capture_dir=capture_dir, config=config),
        requested_labels=(str(ADMITTED_ID),),
    )

    report = format_purge_report(capture_dir=capture_dir, plan=plan, applied=False)

    assert "Declined" in report
    assert str(ADMITTED_ID) in report


def test_purge_report_with_no_targets_says_so(
    config: CrawDadConfig, tmp_path: Path
) -> None:
    """An already-clean tree reports nothing to do."""
    report = format_purge_report(
        capture_dir=tmp_path / "absent", plan=plan_purge(audits=()), applied=False
    )

    assert "nothing to purge" in report.lower()


# --------------------------------------------------------------------------
# CLI wiring
# --------------------------------------------------------------------------


@pytest.fixture
def patched_load(
    config: CrawDadConfig, capture_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Point ``cli.load_config`` at the fixture config; return the capture root."""
    monkeypatch.setattr(cli, "load_config", lambda _path=None: config)
    return capture_dir


def test_cli_capture_audit_prints_the_report(
    patched_load: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``crawdad capture audit`` writes the audit to stdout and deletes nothing."""
    cli.main(["capture", "audit"])

    out = capsys.readouterr().out
    assert str(STRANGER_ID) in out
    assert (patched_load / str(STRANGER_ID)).is_dir()


def test_cli_capture_purge_defaults_to_a_dry_run(
    patched_load: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``crawdad capture purge`` with no ``--apply`` deletes nothing at all."""
    cli.main(["capture", "purge"])

    assert "DRY RUN" in capsys.readouterr().out
    assert (patched_load / str(STRANGER_ID)).is_dir()
    assert (patched_load / str(INTIMATE_ID)).is_dir()


def test_cli_capture_purge_apply_deletes_the_refused_dirs(
    patched_load: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--apply`` performs the deletion the dry run described."""
    cli.main(["capture", "purge", "--apply"])

    capsys.readouterr()
    assert not (patched_load / str(STRANGER_ID)).exists()
    assert (patched_load / str(ADMITTED_ID)).is_dir()


def test_cli_capture_purge_channel_flag_targets_an_unresolved_dir(
    patched_load: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--channel`` promotes a named dir into the purge set."""
    cli.main(["capture", "purge", "--channel", NAMED_LABEL, "--apply"])

    capsys.readouterr()
    assert not (patched_load / NAMED_LABEL).exists()


def test_cli_capture_requires_a_mode(patched_load: Path) -> None:
    """Bare ``crawdad capture`` exits with usage rather than guessing."""
    with pytest.raises(SystemExit):
        cli.main(["capture"])


def test_cli_capture_purge_never_deletes_an_admitted_dir_on_request(
    patched_load: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """End to end: naming the admitted channel is declined, not obeyed."""
    cli.main(["capture", "purge", "--channel", str(ADMITTED_ID), "--apply"])

    out = capsys.readouterr().out
    assert (patched_load / str(ADMITTED_ID)).is_dir()
    assert "Declined" in out
