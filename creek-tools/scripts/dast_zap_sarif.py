"""Turn one ZAP JSON report into SARIF that GitHub code scanning will accept.

The nightly deep scan (``.github/workflows/dast-deep.yml``) writes a single JSON
file on a runner and then the runner goes away. Two things read it afterwards,
and neither can read the other's format: GitHub code scanning speaks SARIF only,
and a human triaging the run reads the raw artifact. This module is the bridge.
It is a port of adepthood's ``backend/scripts/dast/zap_sarif.py``, so both
repositories publish ZAP findings to their Security tabs in the same shape.

WHY NOT ZAP'S OWN SARIF TEMPLATE: the report the pinned action always writes is
``report_json.json``, unconditionally, into the workspace. Asking the container
for a second format means passing more options through the action's
``cmd_options``, where a typo produces no file and no error -- and an absent
SARIF file uploads as nothing and renders as a clean Security tab. Converting
the file that is always there keeps one report, two consumers, and one failure
mode: unreadable, which is loud.

THE FAILURE MODE THIS GUARDS: an empty SARIF run is perfectly valid, uploads
without complaint, and looks exactly like a scan that found nothing. So a report
that cannot be read -- absent, truncated, or not a ZAP report at all -- exits
:data:`EXIT_HARNESS_ERROR` rather than converting to zero results. A run that
proved nothing must never be mistaken for a run that found nothing.

Usage (from ``creek-tools/``)::

    python -m scripts.dast_zap_sarif --report report_json.json \
        --sarif report_sarif.sarif

Stdout carries a Markdown summary and nothing else -- the workflow appends it
straight to ``$GITHUB_STEP_SUMMARY`` -- so every diagnostic goes to stderr.

Exit codes:
    0 -- the report was read and converted. Findings do not change this: the
        nightly scan reports, it does not block.
    3 -- the report could not be read.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlsplit

EXIT_CLEAN: Final[int] = 0
"""The report was read and converted, whatever it found."""

EXIT_HARNESS_ERROR: Final[int] = 3
"""The report could not be read, so the scan proved nothing."""

SARIF_VERSION: Final[str] = "2.1.0"
SARIF_SCHEMA: Final[str] = "https://json.schemastore.org/sarif-2.1.0.json"

TOOL_NAME: Final[str] = "OWASP ZAP"
TOOL_URI: Final[str] = "https://www.zaproxy.org/"

# ZAP grades every alert 0..3. ``level`` is what code scanning filters on;
# ``security-severity`` is what it ranks and colours by, and a rule without one
# is displayed as the lowest severity there is.
RISK_LEVELS: Mapping[int, str] = {0: "note", 1: "note", 2: "warning", 3: "error"}
SECURITY_SEVERITY: Mapping[int, str] = {0: "1.0", 1: "3.0", 2: "5.0", 3: "8.0"}

# What an unrecognised grade reads as. Failing upwards is the only safe
# direction: a new ZAP risk band must arrive as something a human looks at.
_UNKNOWN_LEVEL = "error"
_UNKNOWN_SEVERITY = "8.0"

# ZAP writes ``-1`` -- and older builds ``0`` or an empty string -- when it knows
# no CWE for an alert. Publishing those as a tag is a link to nowhere.
_ABSENT_CWE = frozenset({"", "-1", "0"})

# The site root has an empty path, and SARIF requires a non-empty artifact uri.
_ROOT_LOCATION = "(site root)"

# ZAP grades an alert 0 when it is informational: against /v1 that is mostly
# rule 100000's "A Client Error response code was returned" -- hundreds of
# results a run, each one the API rightly refusing an attack payload. Those are
# counted in the summary and kept in the raw artifact but not published, so the
# Security tab holds what a person must act on. The rule cannot be suppressed in
# the rules file instead: its server-error variant shares the plugin id.
_INFORMATIONAL: Final[int] = 0

_RISK_NAMES: Mapping[int, str] = {0: "Informational", 1: "Low", 2: "Medium", 3: "High"}

_TAG = re.compile(r"<[^>]+>")
_WHITESPACE = re.compile(r"\s+")
# An inline tag -- ZAP writes plenty of ``<b>`` and ``<code>`` -- leaves a space
# in front of the punctuation that followed it once the tag becomes a space.
_SPACE_BEFORE_PUNCTUATION = re.compile(r"\s+([.,;:!?)\]])")


@dataclass(frozen=True)
class Instance:
    """One request that provoked an alert."""

    uri: str
    method: str
    param: str
    evidence: str


@dataclass(frozen=True)
class Alert:
    """One ZAP alert, with every request that provoked it."""

    rule_id: str
    name: str
    risk: int
    description: str
    solution: str
    reference: str
    cwe_id: str
    instances: tuple[Instance, ...]


class UnreadableReportError(Exception):
    """The file named on the command line is not a ZAP report this can convert."""


def _text(node: Mapping[str, Any], key: str) -> str:
    """Return one field of a report node as a string, however ZAP typed it.

    Args:
        node: A mapping taken from the parsed report.
        key: The field to read.

    Returns:
        The field's value as text, or the empty string when it is absent.
    """
    value = node.get(key, "")
    return value if isinstance(value, str) else str(value)


def _risk(node: Mapping[str, Any]) -> int:
    """Return an alert's numeric risk grade.

    Args:
        node: The alert node from the parsed report.

    Returns:
        The grade ZAP assigned, or ``-1`` when it is absent or not a number --
        which maps to the deliberately pessimistic unknown band.
    """
    try:
        return int(_text(node, "riskcode"))
    except ValueError:
        return -1


def _flatten(markup: str) -> str:
    """Return ZAP's HTML prose as one line of readable text.

    SARIF descriptions render as plain text, so raw markup arrives at the reader
    as literal tags.

    Args:
        markup: A description, solution or reference, as ZAP wrote it.

    Returns:
        The same prose with tags removed, entities resolved and runs of
        whitespace collapsed.
    """
    collapsed = _WHITESPACE.sub(" ", html.unescape(_TAG.sub(" ", markup)))
    return _SPACE_BEFORE_PUNCTUATION.sub(r"\1", collapsed).strip()


def _parse_instance(node: Mapping[str, Any]) -> Instance:
    """Build one :class:`Instance` from a report node.

    Args:
        node: An entry of an alert's ``instances`` list.

    Returns:
        The request that provoked the alert.
    """
    return Instance(
        uri=_text(node, "uri"),
        method=_text(node, "method"),
        param=_text(node, "param"),
        evidence=_text(node, "evidence"),
    )


def _parse_alert(node: Mapping[str, Any]) -> Alert:
    """Build one :class:`Alert` from a report node.

    Args:
        node: An entry of a site's ``alerts`` list.

    Returns:
        The alert, with every instance ZAP recorded for it.
    """
    instances = node.get("instances")
    return Alert(
        rule_id=_text(node, "alertRef") or _text(node, "pluginid"),
        name=_text(node, "name") or _text(node, "alert"),
        risk=_risk(node),
        description=_flatten(_text(node, "desc")),
        solution=_flatten(_text(node, "solution")),
        reference=_flatten(_text(node, "reference")),
        cwe_id=_text(node, "cweid"),
        instances=tuple(
            _parse_instance(entry)
            for entry in (instances if isinstance(instances, list) else [])
            if isinstance(entry, Mapping)
        ),
    )


def _sites(document: object) -> list[Mapping[str, Any]]:
    """Return the report's site nodes, refusing anything that is not one.

    Args:
        document: Whatever ``json.load`` produced.

    Returns:
        Every site node in the report.

    Raises:
        UnreadableReportError: If the document is not a ZAP report, or names no site.
            Both convert to zero results, which is indistinguishable from a clean
            scan, so neither may be treated as one.
    """
    if not isinstance(document, Mapping):
        raise UnreadableReportError("the report is not a JSON object")
    sites = document.get("site")
    if not isinstance(sites, list) or not sites:
        raise UnreadableReportError("the report names no scanned site")
    nodes = [
        site for site in sites if isinstance(site, Mapping) and _text(site, "@name")
    ]
    if not nodes:
        raise UnreadableReportError("the report's sites carry no name")
    return nodes


def alerts(document: object) -> tuple[Alert, ...]:
    """Return every alert in a parsed ZAP report.

    Args:
        document: Whatever ``json.load`` produced for the report file.

    Returns:
        Every alert across every scanned site, in report order.

    Raises:
        UnreadableReportError: If the document is not a ZAP report.
    """
    return tuple(
        _parse_alert(node) for site in _sites(document) for node in _alert_nodes(site)
    )


def _alert_nodes(site: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Return one site's alert nodes, skipping anything that is not one.

    Args:
        site: A site node from the parsed report.

    Returns:
        Every entry of the site's ``alerts`` list that is itself a mapping. A
        site with no alerts is ordinary -- ZAP writes one per scanned host --
        so an absent or malformed list is empty here rather than an error.
    """
    entries = site.get("alerts")
    if not isinstance(entries, list):
        return []
    return [node for node in entries if isinstance(node, Mapping)]


def targets(document: object) -> tuple[str, ...]:
    """Return the names of the sites the report covers.

    Args:
        document: Whatever ``json.load`` produced for the report file.

    Returns:
        One name per scanned site.

    Raises:
        UnreadableReportError: If the document is not a ZAP report.
    """
    return tuple(_text(site, "@name") for site in _sites(document))


def _location_uri(instance: Instance) -> str:
    """Return the artifact uri a finding is anchored to.

    A DAST finding has no source file, so the request path is used: it is the
    axis a reader groups by, and it keeps two alerts on two endpoints from
    rendering as one.

    Args:
        instance: The request that provoked the alert.

    Returns:
        The request path without its scheme or host, or a stable placeholder for
        the site root, which has an empty path.
    """
    return urlsplit(instance.uri).path.lstrip("/") or _ROOT_LOCATION


def _fingerprint(alert: Alert, instance: Instance) -> str:
    """Return a stable identity for one finding.

    Code scanning folds results that fingerprint alike into one alert and carries
    a dismissal across runs, so this has to be identical for the same finding on
    every run and different for two findings on two endpoints.

    Args:
        alert: The alert the finding belongs to.
        instance: The request that provoked it.

    Returns:
        A hex digest over the rule, the method, the path and the parameter.
    """
    identity = "\n".join(
        [alert.rule_id, instance.method, _location_uri(instance), instance.param]
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _tags(alert: Alert) -> list[str]:
    """Return the SARIF tags for one alert's rule.

    Args:
        alert: The alert being described.

    Returns:
        ``security`` always, plus the CWE tag when ZAP knew one.
    """
    tags = ["security"]
    if alert.cwe_id not in _ABSENT_CWE:
        tags.append(f"external/cwe/cwe-{alert.cwe_id}")
    return tags


def _rule(alert: Alert) -> dict[str, Any]:
    """Return the SARIF rule descriptor for one alert.

    Args:
        alert: The alert being described.

    Returns:
        A ``reportingDescriptor`` object.
    """
    return {
        "id": alert.rule_id,
        "name": alert.name,
        "shortDescription": {"text": alert.name},
        "fullDescription": {"text": alert.description},
        "help": {"text": alert.solution or alert.description},
        "properties": {
            "tags": _tags(alert),
            "security-severity": SECURITY_SEVERITY.get(alert.risk, _UNKNOWN_SEVERITY),
            "precision": "medium",
        },
    }


def _message(alert: Alert, instance: Instance) -> str:
    """Return the one-line message a reader sees on a finding.

    Args:
        alert: The alert the finding belongs to.
        instance: The request that provoked it.

    Returns:
        The alert name, the request that produced it, and the evidence when ZAP
        recorded any.
    """
    line = f"{alert.name}: {instance.method} {instance.uri}".rstrip()
    if instance.evidence:
        line = f"{line} (evidence: {instance.evidence})"
    return line


def _result(alert: Alert, instance: Instance, rule_index: int) -> dict[str, Any]:
    """Return the SARIF result for one instance of one alert.

    Args:
        alert: The alert the finding belongs to.
        instance: The request that provoked it.
        rule_index: Position of the alert's rule in the run's rule list.

    Returns:
        A SARIF ``result`` object.
    """
    return {
        "ruleId": alert.rule_id,
        "ruleIndex": rule_index,
        "level": RISK_LEVELS.get(alert.risk, _UNKNOWN_LEVEL),
        "message": {"text": _message(alert, instance)},
        "partialFingerprints": {"zapAlertInstance/v1": _fingerprint(alert, instance)},
        "locations": [
            {
                "physicalLocation": {
                    "artifactLocation": {"uri": _location_uri(instance)},
                    "region": {"startLine": 1},
                }
            }
        ],
    }


def build_sarif(document: object) -> dict[str, Any]:
    """Convert a parsed ZAP report into a SARIF 2.1.0 log.

    Args:
        document: Whatever ``json.load`` produced for the report file.

    Returns:
        A SARIF log with one run, one rule per distinct alert graded above
        informational, and one result per request that provoked one.

    Raises:
        UnreadableReportError: If the document is not a ZAP report.
    """
    rules: list[dict[str, Any]] = []
    indices: dict[str, int] = {}
    results: list[dict[str, Any]] = []
    for alert in alerts(document):
        if alert.risk == _INFORMATIONAL:
            continue
        if alert.rule_id not in indices:
            indices[alert.rule_id] = len(rules)
            rules.append(_rule(alert))
        index = indices[alert.rule_id]
        results.extend(_result(alert, instance, index) for instance in alert.instances)
    return {
        "$schema": SARIF_SCHEMA,
        "version": SARIF_VERSION,
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": TOOL_NAME,
                        "informationUri": TOOL_URI,
                        "rules": rules,
                    }
                },
                "results": results,
            }
        ],
    }


def summarise(document: object) -> str:
    """Return the Markdown a reader of the Actions run list gets.

    Args:
        document: Whatever ``json.load`` produced for the report file.

    Returns:
        A heading, the targets scanned, and either a per-grade table or a line
        saying nothing was found.

    Raises:
        UnreadableReportError: If the document is not a ZAP report.
    """
    found = alerts(document)
    lines = [
        f"## {TOOL_NAME} deep API scan",
        "",
        f"Scanned: {', '.join(targets(document))}",
        "",
    ]
    if not found:
        lines.append("The scan raised no alerts.")
        return "\n".join(lines)
    lines.extend(["| Risk | Alerts | Instances |", "| --- | --- | --- |"])
    for risk in sorted(_RISK_NAMES, reverse=True):
        graded = [alert for alert in found if alert.risk == risk]
        if graded:
            instances = sum(len(alert.instances) for alert in graded)
            lines.append(f"| {_RISK_NAMES[risk]} | {len(graded)} | {instances} |")
    ungraded = [alert for alert in found if alert.risk not in _RISK_NAMES]
    if ungraded:
        lines.append(f"| Unrecognised | {len(ungraded)} | -- |")
    lines.extend(
        [
            "",
            "Findings are published to the repository's Security tab; "
            "informational alerts are counted here and kept in the raw report only.",
        ]
    )
    return "\n".join(lines)


def _load(report: Path) -> object:
    """Read and parse the ZAP report.

    Args:
        report: Path the workflow told ZAP to write.

    Returns:
        The parsed document.

    Raises:
        UnreadableReportError: If the file is absent, unreadable, or not JSON. Each of
            those means the scan did not finish, which must never convert to an
            empty -- and therefore clean-looking -- SARIF run.
    """
    try:
        text = report.read_text(encoding="utf-8")
    except OSError as error:
        raise UnreadableReportError(f"{report} could not be read: {error}") from error
    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        raise UnreadableReportError(f"{report} is not valid JSON: {error}") from error


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """Parse the command line.

    Neither path has a default: a defaulted report path is how a run converts
    yesterday's file and reports on a scan that did not happen.

    Args:
        argv: The argument vector, or ``None`` to read ``sys.argv``.

    Returns:
        The parsed arguments.
    """
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument(
        "--report", required=True, type=Path, help="the ZAP JSON report to read"
    )
    parser.add_argument(
        "--sarif", required=True, type=Path, help="where to write the SARIF log"
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Convert the report, write the SARIF log, and print the run summary.

    Args:
        argv: The argument vector, or ``None`` to read ``sys.argv``.

    Returns:
        ``0`` when the report was converted, findings or not; ``3`` when it could
        not be read, in which case no SARIF file is written -- an empty one would
        upload cleanly and report a scan that never happened as a clean one.
    """
    args = _parse_args(argv)
    try:
        document = _load(args.report)
        sarif = build_sarif(document)
        summary = summarise(document)
    except UnreadableReportError as error:
        sys.stderr.write(f"the ZAP report could not be converted: {error}\n")
        return EXIT_HARNESS_ERROR
    args.sarif.write_text(json.dumps(sarif, indent=2), encoding="utf-8")
    sys.stdout.write(f"{summary}\n")
    return EXIT_CLEAN


if __name__ == "__main__":
    raise SystemExit(main())
