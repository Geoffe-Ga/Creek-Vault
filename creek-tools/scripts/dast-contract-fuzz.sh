#!/usr/bin/env bash
# creek-tools/scripts/dast-contract-fuzz.sh
#
# Ask one question of every operation the /v1 vault API publishes about itself:
# can schema-aware but adversarial input make this endpoint break its own
# contract? Run by .github/workflows/dast-contract.yml against a throwaway
# instance started on loopback by scripts/dast_serve.py.
#
# This lives in a script rather than inline in the workflow so that it can be
# *executed* by a test. A YAML `run:` block can only be asserted about by
# grepping for strings, which cannot tell a live invocation from a commented-out
# one. tests/test_dast_contract_fuzz_script.py runs this script with a recording
# stub named `schemathesis` on PATH and asserts on the argv it actually built,
# and tests/dast_planted_bug_check.py runs it with the real pinned fuzzer
# against a deliberately broken app and requires it to fail.
#
# The document is `creek-tools-api --print-openapi` from the same checkout the
# instance runs: it is generated from creek_mcp/api/routes.py, the one table the
# Starlette adapter also routes from, so the spec this gate judges against
# cannot drift away from the code it is judging.
#
# Environment (all required; there is no default, because every default here
# would be a way to fuzz the wrong thing and report success):
#   BASE_URL     -- the running instance, e.g. http://127.0.0.1:8823
#   DAST_TOKEN   -- a consumer token already proven to open an authenticated route
#   OPENAPI_DOC  -- path to the document printed by `--print-openapi`
#   REPORT_DIR   -- where the JUnit report is written
#
# Exits with schemathesis's own exit code. Nothing runs after it, by
# construction: the fuzzer is `exec`'d, so no trailing command can ever stand
# between a failing run and a failing job.

set -euo pipefail

# No arguments. Accepting any would hand a caller a way to append a filter --
# `--exclude-method GET`, `--exclude-path-regex ...` -- that quietly stops
# operations being fuzzed.
if [ "$#" -ne 0 ]; then
  echo "usage: dast-contract-fuzz.sh (no arguments; see the header for the environment)" >&2
  exit 2
fi

: "${BASE_URL:?BASE_URL must name the running instance}"
: "${DAST_TOKEN:?DAST_TOKEN must hold a token proven to open an authenticated route}"
: "${OPENAPI_DOC:?OPENAPI_DOC must name the document printed by --print-openapi}"
: "${REPORT_DIR:?REPORT_DIR must name a directory for the JUnit report}"

SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
# Per-operation allowances for published 503 refusals; see its header.
CONFIG_FILE="$SCRIPT_DIR/dast-schemathesis.toml"

if [ ! -s "$OPENAPI_DOC" ]; then
  echo "OPENAPI_DOC ($OPENAPI_DOC) is missing or empty; refusing to fuzz nothing" >&2
  exit 2
fi

# Every /v1 route but the handshake and health refuses a request without a
# matching contract minor (creek_mcp/api/routes.py, requires_contract_version).
# Sent as a fixed, valid header so generated cases reach the handlers behind
# that gate instead of spending the budget on one refusal. Read from the code,
# never restated, so a minor bump cannot strand this gate on the old version.
CONTRACT_MINOR=$(python -c 'from creek_mcp.api.models import CONTRACT_MINOR; print(CONTRACT_MINOR)')

# The checks the run enforces, named one by one. `--checks all` would silently
# change meaning with every upgrade.
CHECKS="not_a_server_error,content_type_conformance,response_schema_conformance,status_code_conformance"

# Budget: the document publishes 22 operations. Measured against a fresh
# loopback instance at this seed, this generates 526 cases and finishes in
# seconds -- far inside the workflow's ceiling, so there is room to raise it.
MAX_EXAMPLES=25

# Fixed so a failure can be replayed byte for byte. A gate that cannot be
# replayed cannot be trusted to have failed honestly.
SEED=1337

# Truncates the evidence, never the verdict: reaching this still exits non-zero.
MAX_FAILURES=20

# The coverage phase is off because the number of cases it derives from a
# schema has no bound this comment could state, and the stateful phase because
# the document declares no OpenAPI links, so it has no transitions to follow.
PHASES="examples,fuzzing"

# There is deliberately no exclusion list: every published operation is fuzzed.
# No /v1 route destroys the credential or dials a third party from a throwaway
# instance, and a refusal the instance cannot avoid is handled per operation in
# dast-schemathesis.toml -- where the server-error check is relaxed for that
# documented 503 alone, and every other check still runs.
exec schemathesis --config-file "$CONFIG_FILE" run "$OPENAPI_DOC" \
  --url "$BASE_URL" \
  --checks "$CHECKS" \
  --header "Authorization: Bearer $DAST_TOKEN" \
  --header "X-Creek-Contract-Version: $CONTRACT_MINOR" \
  --max-examples "$MAX_EXAMPLES" \
  --seed "$SEED" \
  --phases "$PHASES" \
  --max-failures "$MAX_FAILURES" \
  --report junit \
  --report-dir "$REPORT_DIR"
