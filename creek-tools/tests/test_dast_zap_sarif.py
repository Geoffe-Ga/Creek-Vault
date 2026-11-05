"""The ZAP report only reaches a human if the conversion to SARIF is honest.

The nightly deep scan produces one JSON file on a runner and then goes away. Two
things read it afterwards: GitHub code scanning, which only speaks SARIF, and
the remediation loop, which reads the raw artifact. So the converter is the only
thing standing between a real finding and a run that reported nothing -- and its
failure mode is silent by construction, because an empty SARIF file uploads
perfectly and renders as a clean Security tab.

Hence the shape of this suite: every test either proves a finding survives the
conversion, or proves that a report which could not be read fails loudly instead
of converting to zero results.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from scripts.dast_zap_sarif import (
    EXIT_CLEAN,
    EXIT_HARNESS_ERROR,
    RISK_LEVELS,
    SARIF_VERSION,
    SECURITY_SEVERITY,
    build_sarif,
    main,
    summarise,
)

if TYPE_CHECKING:
    from pathlib import Path

_TARGET = "http://127.0.0.1:8823"


def _instance(uri: str, method: str = "GET", **extra: str) -> dict[str, str]:
    """Build one ZAP alert instance, in the shape the traditional-json report uses."""
    return {
        "uri": uri,
        "method": method,
        "param": "",
        "evidence": "",
        "otherinfo": "",
        **extra,
    }


def _alert(**overrides: object) -> dict[str, object]:
    """Build one ZAP alert, defaulting every field the converter reads."""
    alert: dict[str, object] = {
        "pluginid": "10038",
        "alertRef": "10038-1",
        "name": "Content Security Policy (CSP) Header Not Set",
        "riskcode": "2",
        "confidence": "3",
        "desc": "<p>The header was not set.</p>",
        "solution": "<p>Set it.</p>",
        "reference": "<p>https://owasp.org/csp</p>",
        "cweid": "693",
        "instances": [_instance(f"{_TARGET}/v1/wheel")],
    }
    alert.update(overrides)
    return alert


def _report(*alerts: dict[str, object]) -> dict[str, object]:
    """Build a whole ZAP report around the given alerts."""
    return {
        "@programName": "ZAP",
        "@version": "2.16.1",
        "site": [{"@name": _TARGET, "@host": "127.0.0.1", "alerts": list(alerts)}],
    }


def _results(document: object) -> list[dict[str, Any]]:
    """Return the SARIF results the converter produces for one report."""
    runs: list[dict[str, Any]] = build_sarif(document)["runs"]
    results: list[dict[str, Any]] = runs[0]["results"]
    return results


def _rules(document: object) -> list[dict[str, Any]]:
    """Return the SARIF rule descriptors the converter produces for one report."""
    runs: list[dict[str, Any]] = build_sarif(document)["runs"]
    rules: list[dict[str, Any]] = runs[0]["tool"]["driver"]["rules"]
    return rules


def test_a_report_with_no_alerts_still_produces_a_well_formed_run() -> None:
    """Uploading nothing would fail the upload rather than report a clean scan."""
    sarif = build_sarif(_report())

    assert sarif["version"] == SARIF_VERSION
    assert sarif["$schema"]
    assert len(sarif["runs"]) == 1
    assert sarif["runs"][0]["results"] == []
    assert sarif["runs"][0]["tool"]["driver"]["name"]


def test_a_named_site_that_raised_nothing_is_not_a_harness_error() -> None:
    """ZAP omits the key entirely for a host it reached and found nothing on.

    The distinction this file exists to protect: "no alerts" is a real answer,
    while "no site" means the scan never happened. Only the second is an error.
    """
    quiet = {"site": [{"@name": _TARGET, "@host": "127.0.0.1"}]}

    assert _results(quiet) == []
    assert "no alerts" in summarise(quiet).lower()


def test_every_instance_of_an_alert_becomes_its_own_result() -> None:
    """ZAP reports one alert with N instances; N endpoints are affected, not one."""
    alert = _alert(
        instances=[
            _instance(f"{_TARGET}/v1/wheel"),
            _instance(f"{_TARGET}/v1/journal-entries/a"),
        ]
    )

    results = _results(_report(alert))

    assert len(results) == 2
    uris = [
        result["locations"][0]["physicalLocation"]["artifactLocation"]["uri"]
        for result in results
    ]
    assert uris == ["v1/wheel", "v1/journal-entries/a"]


def test_the_rule_is_declared_once_however_many_instances_it_has() -> None:
    """A rule repeated per instance makes the Security tab unreadable."""
    alert = _alert(instances=[_instance(f"{_TARGET}/a"), _instance(f"{_TARGET}/b")])

    rules = _rules(_report(alert))

    assert [rule["id"] for rule in rules] == ["10038-1"]


def test_two_alerts_sharing_a_rule_share_one_descriptor() -> None:
    """ZAP repeats a rule per site; a repeated descriptor makes the tab unreadable."""
    twice = _report(
        _alert(), _alert(instances=[_instance(f"{_TARGET}/v1/journal-entries/a")])
    )

    sarif = build_sarif(twice)

    assert [rule["id"] for rule in sarif["runs"][0]["tool"]["driver"]["rules"]] == [
        "10038-1"
    ]
    assert [result["ruleIndex"] for result in sarif["runs"][0]["results"]] == [0, 0]


def test_the_rule_id_falls_back_to_the_plugin_id() -> None:
    """Older reports carry no ``alertRef``; without a fallback every alert collapses."""
    rules = _rules(_report(_alert(alertRef="")))

    assert [rule["id"] for rule in rules] == ["10038"]


@pytest.mark.parametrize("risk", [1, 2, 3])
def test_each_risk_grade_carries_both_a_level_and_a_security_severity(
    risk: int,
) -> None:
    """GitHub ranks by ``security-severity`` and filters by ``level``; it needs both."""
    sarif = build_sarif(_report(_alert(riskcode=str(risk))))
    result = sarif["runs"][0]["results"][0]
    rule = sarif["runs"][0]["tool"]["driver"]["rules"][0]

    assert result["level"] == RISK_LEVELS[risk]
    assert rule["properties"]["security-severity"] == SECURITY_SEVERITY[risk]


def test_a_high_risk_alert_is_an_error_and_an_informational_one_is_not() -> None:
    """Pin both ends of the mapping, so an edit that flattens it turns red."""
    assert RISK_LEVELS[3] == "error"
    assert RISK_LEVELS[0] == "note"
    assert float(SECURITY_SEVERITY[3]) > float(SECURITY_SEVERITY[0])


def test_an_informational_alert_is_counted_but_not_published() -> None:
    """ZAP files every 4xx the API rightly answers as an informational alert.

    Against /v1 that is hundreds of results a run -- the API refusing attack
    payloads, which is the behaviour being tested for, not a finding. They stay
    in the summary and the raw artifact; the Security tab keeps what a person
    must act on.
    """
    document = _report(
        _alert(
            alertRef="100000-2",
            riskcode="0",
            instances=[_instance(f"{_TARGET}/v1/wheel")],
        ),
        _alert(riskcode="1"),
    )

    sarif = build_sarif(document)

    assert [rule["id"] for rule in sarif["runs"][0]["tool"]["driver"]["rules"]] == [
        "10038-1"
    ]
    assert len(sarif["runs"][0]["results"]) == 1
    assert "| Informational | 1 | 1 |" in summarise(document)


def test_an_unrecognised_risk_grade_is_not_silently_downgraded() -> None:
    """A grade the table has never seen must read as serious, not as informational."""
    sarif = build_sarif(_report(_alert(riskcode="9")))

    assert sarif["runs"][0]["results"][0]["level"] == "error"


def test_a_risk_grade_that_is_not_a_number_reads_as_serious() -> None:
    """A grade that did not survive serialisation must not read as informational."""
    sarif = build_sarif(_report(_alert(riskcode="")))

    assert sarif["runs"][0]["results"][0]["level"] == "error"


def test_the_summary_counts_an_unrecognised_grade_rather_than_dropping_it() -> None:
    """An alert missing from every row is an alert the reader never learns about."""
    summary = summarise(_report(_alert(riskcode="not a number")))

    assert "Unrecognised" in summary


def test_the_evidence_zap_captured_reaches_the_message() -> None:
    """The evidence string is the difference between a finding and a claim."""
    alert = _alert(
        instances=[_instance(f"{_TARGET}/v1/wheel", evidence="X-Frame-Options")]
    )

    assert "X-Frame-Options" in _results(_report(alert))[0]["message"]["text"]


def test_the_html_zap_writes_is_flattened_into_readable_text() -> None:
    """SARIF descriptions render as plain text; raw markup arrives as literal tags."""
    alert = _alert(
        desc="<p>First.</p><p>Second &amp; last.</p>", solution="<p>Do <b>this</b>.</p>"
    )

    rule = _rules(_report(alert))[0]

    assert "<p>" not in rule["fullDescription"]["text"]
    assert "&amp;" not in rule["fullDescription"]["text"]
    assert rule["fullDescription"]["text"] == "First. Second & last."
    assert rule["help"]["text"] == "Do this."


def test_the_message_names_the_method_and_the_url_that_produced_it() -> None:
    """An alert without its request is a finding nobody can reproduce."""
    alert = _alert(instances=[_instance(f"{_TARGET}/v1/wheel", method="POST")])

    message = _results(_report(alert))[0]["message"]["text"]

    assert "POST" in message
    assert f"{_TARGET}/v1/wheel" in message


def test_a_target_root_still_has_a_location() -> None:
    """SARIF requires a non-empty uri, and the site root has an empty path."""
    alert = _alert(instances=[_instance(_TARGET)])

    uri = _results(_report(alert))[0]["locations"][0]["physicalLocation"][
        "artifactLocation"
    ]["uri"]

    assert uri


def test_two_instances_of_one_alert_fingerprint_differently() -> None:
    """A shared fingerprint makes code scanning fold two endpoints into one alert."""
    alert = _alert(instances=[_instance(f"{_TARGET}/a"), _instance(f"{_TARGET}/b")])

    prints = [result["partialFingerprints"] for result in _results(_report(alert))]

    assert prints[0] != prints[1]
    assert all(fingerprint for fingerprint in prints)


def test_one_path_reached_two_ways_fingerprints_differently() -> None:
    """One rule on one path under several methods is several separate findings.

    Fold them together and dismissing the finding on ``GET`` silently dismisses
    the un-triaged one on ``POST``.
    """
    alert = _alert(
        instances=[
            _instance(f"{_TARGET}/v1/journal-entries/a", method="GET"),
            _instance(f"{_TARGET}/v1/journal-entries/a", method="POST"),
        ]
    )

    prints = [result["partialFingerprints"] for result in _results(_report(alert))]

    assert prints[0] != prints[1]


def test_two_parameters_of_one_endpoint_fingerprint_differently() -> None:
    """An injection rule fires per parameter; two parameters are two things to fix."""
    alert = _alert(
        instances=[
            _instance(f"{_TARGET}/v1/journal-entries/a", method="POST", param="title"),
            _instance(f"{_TARGET}/v1/journal-entries/a", method="POST", param="body"),
        ]
    )

    prints = [result["partialFingerprints"] for result in _results(_report(alert))]

    assert prints[0] != prints[1]


def test_the_same_finding_fingerprints_the_same_way_on_every_run() -> None:
    """Dismissal tracking is the reason to publish SARIF rather than an artifact."""
    first = _results(_report(_alert()))[0]["partialFingerprints"]
    second = _results(_report(_alert()))[0]["partialFingerprints"]

    assert first == second


def test_a_cwe_is_published_as_a_tag_when_zap_knows_one() -> None:
    """The tag lets the Security tab group a finding with the rest of its class."""
    tags = _rules(_report(_alert(cweid="693")))[0]["properties"]["tags"]

    assert "security" in tags
    assert "external/cwe/cwe-693" in tags


@pytest.mark.parametrize("cweid", ["", "-1", "0"])
def test_an_absent_cwe_is_not_invented(cweid: str) -> None:
    """ZAP writes ``-1`` for "no CWE"; publishing ``cwe--1`` is a link to nowhere."""
    tags = _rules(_report(_alert(cweid=cweid)))[0]["properties"]["tags"]

    assert not [tag for tag in tags if tag.startswith("external/cwe/")]


def test_the_summary_counts_what_was_found() -> None:
    """A cron run's whole audience is the run summary; a colour is not a report.

    The whole rendered row is asserted, not the digits loose in the page. Every
    summary prints ``Scanned: http://127.0.0.1:8823``, so a containment check for
    a small number is answered by the target string and would stay green with
    every count in the table hard-zeroed.
    """
    document = _report(
        _alert(riskcode="3", instances=[_instance(f"{_TARGET}/v1/wheel")]),
        _alert(
            alertRef="10096-1",
            riskcode="1",
            instances=[_instance(f"{_TARGET}/v1/journal-entries/a")],
        ),
        _alert(
            alertRef="10096-2",
            riskcode="1",
            instances=[_instance(f"{_TARGET}/v1/export")],
        ),
    )

    summary = summarise(document)

    assert "| High | 1 | 1 |" in summary, summary
    assert "| Low | 2 | 2 |" in summary, summary
    assert "| Medium |" not in summary, summary


def test_the_summary_says_so_when_nothing_was_found() -> None:
    """Silence and "clean" have to be distinguishable in the run list."""
    assert "no alerts" in summarise(_report()).lower()


def test_the_summary_names_the_target_it_scanned() -> None:
    """A report that does not name its target cannot be trusted to have attacked it."""
    assert _TARGET in summarise(_report(_alert()))


def _run(tmp_path: Path, document: object, *extra: str) -> tuple[int, Path, Path]:
    """Write a report, convert it, and return the exit code and both paths."""
    report = tmp_path / "report_json.json"
    report.write_text(json.dumps(document), encoding="utf-8")
    sarif = tmp_path / "report_sarif.sarif"
    code = main(["--report", str(report), "--sarif", str(sarif), *extra])
    return code, report, sarif


def test_the_command_writes_the_sarif_and_prints_the_summary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The workflow redirects stdout into the step summary, so stdout is the report."""
    code, _, sarif = _run(tmp_path, _report(_alert()))

    assert code == EXIT_CLEAN
    assert json.loads(sarif.read_text(encoding="utf-8"))["version"] == SARIF_VERSION
    assert "ZAP" in capsys.readouterr().out


def test_findings_do_not_fail_the_command(tmp_path: Path) -> None:
    """Nightly is advisory: the verdict is published, it does not redden the run."""
    code, _, _ = _run(tmp_path, _report(_alert(riskcode="3")))

    assert code == EXIT_CLEAN


def test_a_missing_report_is_a_harness_error_not_a_clean_scan(tmp_path: Path) -> None:
    """ZAP never ran or never wrote; zero results there is the silent failure."""
    sarif = tmp_path / "report_sarif.sarif"
    code = main(["--report", str(tmp_path / "absent.json"), "--sarif", str(sarif)])

    assert code == EXIT_HARNESS_ERROR
    assert not sarif.exists()


def test_an_unparseable_report_is_a_harness_error(tmp_path: Path) -> None:
    """A truncated report is what a killed container leaves behind."""
    report = tmp_path / "report_json.json"
    report.write_text('{"site": [', encoding="utf-8")
    sarif = tmp_path / "report_sarif.sarif"

    assert main(["--report", str(report), "--sarif", str(sarif)]) == EXIT_HARNESS_ERROR


@pytest.mark.parametrize("document", [[], {}, {"site": {}}, {"site": [{}]}])
def test_a_report_that_is_not_a_zap_report_is_a_harness_error(
    tmp_path: Path, document: object
) -> None:
    """Each of these converts to zero results, indistinguishable from clean."""
    code, _, sarif = _run(tmp_path, document)

    assert code == EXIT_HARNESS_ERROR
    assert not sarif.exists()


def test_the_command_reports_the_unreadable_report_on_stderr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Stdout is the step summary; a diagnostic written there would corrupt it."""
    main(
        [
            "--report",
            str(tmp_path / "absent.json"),
            "--sarif",
            str(tmp_path / "out.sarif"),
        ]
    )
    captured = capsys.readouterr()

    assert "absent.json" in captured.err
    assert captured.out == ""
