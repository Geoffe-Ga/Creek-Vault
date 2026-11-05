# DAST: attacking the `/v1` vault API in CI

Two GitHub Actions workflows start a **throwaway** instance of the `/v1` vault
API and attack it over a real socket. They complement the static gates
(ruff, mypy, bandit, pip-audit), which read the code but never run it. A route
that answers `500` to malformed input, a middleware change that lets a request
skip the bearer gate, a response body that contradicts the published schema:
these only show up when something sends traffic to a running server.

| Workflow | Tool | Runs on | Blocks? | Where the verdict goes |
|----------|------|---------|---------|------------------------|
| [`dast-contract.yml`](../../../.github/workflows/dast-contract.yml) | Schemathesis | Pull requests and pushes to `main` that touch `creek/`, `creek_mcp/`, the DAST scripts or the lockfile; on demand | Red check on the PR | The run log and the `dast-contract-report` JUnit artifact |
| [`dast-deep.yml`](../../../.github/workflows/dast-deep.yml) | OWASP ZAP (API scan) | Nightly at 03:17 UTC; on demand | No: findings are advisory | The repository's Security tab (category `dast-deep`), the run summary and the `dast-deep-report` artifact |

## What the instance is

- **Production-shaped where an attacker would notice.** The production CLI
  starts the server, with every one of its startup refusals.
  [`scripts/dast_serve.py`](../../scripts/dast_serve.py) only adds a log handler
  so the access log is written. The instance has the same bearer gate, the
  same `uv.lock` resolution and the same extras the single-vault image
  installs. No local embedding model is present, exactly as in the image.
- **Throwaway where it must be.** It uses a freshly scaffolded vault under
  `$RUNNER_TEMP`, a consumer token minted for the run, and a loopback bind.
  The deep scan is pinned to `127.0.0.1` by a test. Nothing here ever points
  at a deployed vault.
- **Judged against its own contract.** The OpenAPI document is
  `creek-tools-api --print-openapi` from the same checkout. It is generated
  from the route table the server itself routes from, so the two cannot drift
  apart.

## Reading a red contract gate

A failing `Contract fuzz` step names each failing operation, the check it
failed and a reproduction `curl` (with the token shown as `[Filtered]`). To
replay it locally:

```bash
cd creek-tools
uv pip install -r requirements-dast.txt
# start an instance the way the workflow does, then:
BASE_URL=http://127.0.0.1:8823 DAST_TOKEN=... OPENAPI_DOC=openapi.json \
  REPORT_DIR=/tmp/report scripts/dast-contract-fuzz.sh
```

The seed is fixed, so a run against the same code produces the same cases.

**Fix the route, not the gate.** The only operations excused from the
server-error check are listed in
[`scripts/dast-schemathesis.toml`](../../scripts/dast-schemathesis.toml), and
they are excused for `503` alone. Each one is a refusal that
[`api.md`](../api.md) publishes for a condition the throwaway instance cannot
satisfy (no model provider, Drive never authorised, job ids that must not be
enumerable). A test caps that list and rejects any other `5xx`.

## The proofs that the gates can fail

A scanner that reports clean because it never reached anything looks exactly
like a scanner that found nothing. Each lane therefore proves it can fail:

- **Contract gate.** Before it fuzzes the real API,
  [`tests/dast_planted_bug_check.py`](../../tests/dast_planted_bug_check.py)
  runs the same script and the same pinned Schemathesis against a
  deliberately broken app. The run must report a planted `500`, a planted
  schema violation and a `503` the config does not allow. The repaired twin
  must then pass with every operation tested. The file is not named `test_*`
  because the unit lane does not install Schemathesis, and the house rules
  forbid a skip.
- **Deep scan.** [`scripts/dast_scan_evidence.py`](../../scripts/dast_scan_evidence.py)
  reads only the access-log lines ZAP caused. It fails the run unless ZAP was
  answered as the minted consumer, past the bearer gate, on the probe route.
  A harness failure (instance not ready, token refused, no report written)
  turns the run red and opens or updates a single tracking issue. ZAP's own
  findings never do.

## Tuning ZAP

Rule dispositions live in
[`scripts/dast-zap-rules.tsv`](../../scripts/dast-zap-rules.tsv). The file is
tab-separated, and every line names one rule with a written reason. A test
caps the count and keeps the header rules and rule `100000` (server error)
armed. ZAP's `-I` (pass every warning) is forbidden.
[`scripts/dast_zap_sarif.py`](../../scripts/dast_zap_sarif.py) publishes alerts
graded *Low* and above. Informational alerts are mostly the API correctly
answering `4xx` to attack payloads. They are counted in the run summary and
kept in the raw artifact.
