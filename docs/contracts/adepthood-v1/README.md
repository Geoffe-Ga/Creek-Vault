# Adepthood `/v1` contract bundle

This directory is the published, machine-checkable contract for the
Adepthood HTTP application API. It exists so a consumer — chiefly the
Adepthood client — can validate against Creek's actual wire shapes without
running Creek's own test suite. The decision behind this surface, including
the versioning rule, the error taxonomy, and the oracle-avoidance reasoning
that shapes several fixtures, is
[`../../decisions/2026-07-31-adepthood-http-application-api.md`](../../decisions/2026-07-31-adepthood-http-application-api.md).
Read that document first; this README is the manual for the *bundle*, not a
restatement of the contract it documents.

## This file is the one hand-written thing in this directory

Every other file under `docs/contracts/adepthood-v1/` is **generated** by
`build_bundle()` in
[`creek_mcp/api/bundle.py`](../../../creek-tools/creek_mcp/api/bundle.py)
from the Pydantic models in
[`creek_mcp/api/models.py`](../../../creek-tools/creek_mcp/api/models.py).
`tests/test_adepthood_contract_models.py::test_committed_bundle_equals_a_fresh_build`
asserts the committed bytes are byte-identical to a fresh `build_bundle()`
call. If you find yourself about to hand-edit a `schemas/*.json`, an
`examples/**/*.json`, `retry-policy.json`, or `manifest.json`: don't — edit
the model or the fixture payload in `creek_mcp/api/bundle.py` instead and
regenerate. A hand-edited generated file will fail that round-trip test the
next time it runs.

## Layout

```
adepthood-v1/
├── README.md                          # this file — hand-written, not covered by the round-trip test
├── manifest.json                      # contract_version, ontology_version, and a sha256 per generated file
├── retry-policy.json                  # {code: disposition} — the whole answer to "should I retry this error?"
├── schemas/
│   └── <ModelName>.schema.json        # one JSON Schema per CONTRACT_MODELS entry
├── discovery/
│   ├── agent-mcp-ready.json           # ready vault with its public OAuth MCP resource and issuer
│   └── agent-mcp-unavailable.json     # ready vault with agent_mcp: null
└── examples/
    └── <capability>/
        └── <state>.json               # one fixture per (capability, state) cell
```

## Optional agent MCP discovery

Contract `0.24` publishes `agent_mcp` on authenticated capability discovery.
The object carries exactly `resource` and `authorization_server`, verbatim
HTTPS identifiers of at most 2048 characters; `resource` ends in `/mcp`.
`null` means this allocation cannot currently serve OAuth agents. It says
nothing about the availability of its ordinary vault features.

The files under `discovery/` demonstrate both states with `vault.available:
true`. They are generated and hashed by the same manifest, outside the fixed
capability/state matrix. The standalone shape is
[`AgentMcpDiscovery.schema.json`](schemas/AgentMcpDiscovery.schema.json).
Earlier declared minors omit the field. An unversioned handshake can discover
it while subsequent content requests retain their existing supported minor.

## The two axes of the example matrix

**Capabilities**: `capabilities`, `journal-upsert`, `reflections`,
`wheel`, `upload`, `drive-connector`, `pipeline`, `voice-drafts`,
`journal-withdraw`, `letters`, `agent-activity` — read directly off the `Capability` enum, so this
directory's subdirectory names can never name a different set of capabilities
than the server actually advertises through `GET /v1/capabilities`.

`upload` is published at contract `0.8.0` (#1524) and is the one capability
whose advertisement depends on the caller: a client declaring a minor below
`0.8` is not shown it and is refused `POST /v1/uploads` with
`incompatible_version`. Both halves read `CAPABILITY_SINCE_MINOR`. The fixtures
here document the *current* contract, so this directory is present for every
consumer regardless of what it pins.

### Optional fields, and why `success` and `empty` differ in shape

`examples/reflections/success.json` carries `related_praxis` and
`related_eddies`; `examples/reflections/empty.json` does not. That is not an
oversight in the fixtures — it is the contract. Both fields are **optional** at
`0.9.0` (#873), and the route omits the key entirely rather than emitting an
empty list, so a consumer written against `0.8` sees an unchanged response
whenever nothing qualified. Write your parser against the `success` shape and
your default against the `empty` one.

The absence is also deliberately *ambiguous*: "this vault has no eddies" and
"the eddies this entry belongs to were compiled from fragments above your
ceiling and were withheld" are indistinguishable to a caller, because telling
them apart would be a one-bit oracle over the compiled layer.

**States** (seven), in the order a consumer meets them:

| State | Meaning | Response is |
|---|---|---|
| `success` | The canonical happy response | 200, the capability's own model |
| `empty` | The legitimately-empty answer — **never an error** | 200 |
| `refusal` | An above-ceiling / unresolvable vault-object refusal | 403, `ErrorEnvelope` |
| `care-escalation` | The acute-distress guard fired | 200, `CareEscalationResponse` (reflections only — see below) |
| `malformed-input` | The request does not satisfy the published schema | 422, `ErrorEnvelope` |
| `incompatible-version` | The requested contract minor is not served here | 409, `ErrorEnvelope` |
| `unavailable-service` | The vault is absent or unreadable | 503, `ErrorEnvelope` |

## Reading a fixture as a client engineer

1. Look up your capability's directory under `examples/`.
2. Pick the state you're building a handler for.
3. Read `manifest.json`'s matching entry for the `model` name that governs
   that cell, and validate the fixture against
   `schemas/<model>.schema.json`.
4. If you land on `capabilities/care-escalation.json`,
   `journal-upsert/care-escalation.json`, or `wheel/care-escalation.json`,
   you have found one of the four `NotApplicableExample` cells — see
   below. Do not write a handler for a care-escalation response on any
   capability except `reflections`; the server can never emit one.

## Why four cells are `NotApplicableExample`

The acute-distress care guard runs only inside `reflect_tool`. `capabilities`,
`journal-upsert`, `wheel` and `upload` therefore have **no reachable
care-escalation response shape at all** — not "rare," structurally
impossible. Those four cells hold an explicit, schema-validated
`NotApplicableExample` (`{"unreachable": true, "reason": "..."}`) rather than
either a fabricated response (which would document a shape the server can
never send) or an absent file (which would read as "undocumented" rather
than "cannot happen"). If you are generating client code from this matrix
mechanically, treat `NotApplicableExample` as "skip — this branch does not
exist," not as a normal response variant to handle.

## JSON key order is *not* the contract's ordering guarantee

Every file here is serialised with `sort_keys=True`, because byte-determinism
is what makes the `sha256` manifest and the regeneration test meaningful. So
`examples/wheel/success.json` lists `F1`, `F10`, `F2`, `F3`… — alphabetical,
not canonical. **Do not read that as the frequency order.**

The wheel's deterministic ordering is pinned in the two places that are
actually normative: `WheelFrequencies` declares ten separate fields in the
canonical `F1`…`F10` order of
`creek.generate.indexes.CANONICAL_FREQUENCY_NAMES`, and
`schemas/WheelFrequencies.schema.json`'s `required` array reproduces that
order. JSON objects are unordered by specification, so a client must key the
ten frequencies by name — never by position in the serialised example.

## Integrity: pin the manifest hash, not the files

`manifest.json` records the `contract_version`, the `ontology_version`, and
a `sha256` for every other generated file in this directory, sorted by
path. Adepthood's recommended integration is:

1. Vendor this directory into the Adepthood repository by copy, fetched from
   a `raw.githubusercontent.com` URL pinned to a specific commit sha (not a
   branch).
2. Pin `manifest.json`'s own bytes — or its hash — in Adepthood's CI, and
   fail the build if a re-fetch produces a different manifest without a
   corresponding, reviewed update on the Adepthood side.
3. At runtime, a stale vendored copy is not silently wrong: `/v1` refuses a
   contract-minor mismatch with `409 incompatible_version` (see the ADR's
   [Versioning](../../decisions/2026-07-31-adepthood-http-application-api.md#versioning)
   section), so drift surfaces as a loud, typed error rather than a shape
   mismatch discovered in production.

This is copy-and-pin, not a package. Nothing mechanically stops a vendored
copy from drifting between fetches beyond the manifest-hash check described
above; Creek-side, CI enforces that the bundle can never itself drift from
the models that generate it. Publishing this directory as a GitHub Release
asset (the existing rolling `knowledge-graph` release is the precedent) is
the natural next step and is deliberately not done as part of this bundle's
first publication.

## Version overlap policy (R2-5)

Owner decision **R2-5** (adepthood
`prompts/claude-comms/2026-10-27-release-decisions.md`): *Creek supports the
pinned contract version and the one before; Adepthood moves its pin only
after every vault reports the new version.* Compatibility is negotiated at
**minor** granularity, so "version" here means `major.minor` — a patch bump
is invisible to the consumer.

1. **Creek serves its current minor and the previous minor, at least.** A
   vault whose `contract_version` is `0.N.x` lists both `0.N` and `0.(N-1)`
   in the handshake's `supported_contract_minors`. This is a floor, not the
   current breadth: in practice the window only widens, and today it still
   holds every minor back to `0.2`. R2-5 does not authorize pruning it;
   dropping a minor remains a deliberate breaking change.
2. **Adepthood advances its pin only after every vault reports the new
   minor.** "Every vault" means every managed vault and every self-hosted
   vault on the rollout list, each reporting a `contract_version` /
   `supported_contract_minors` that includes the new minor. The check that
   enforces this (a CI or ops gate over the fleet's handshakes) belongs in
   Adepthood, not here.
3. **The order of a bump is fixed.** Creek ships minor N while still serving
   N-1; the fleet converges on the new image (#1869, rolled out per #1888);
   only then does Adepthood move its pin from N-1 to N. Adepthood never pins
   a minor that its oldest vault does not serve.
4. **A vault that has not converged keeps working.** Until the pin moves,
   Adepthood sends N-1, which both the old and the new vault images serve, so
   a lagging vault answers exactly as before. Nothing breaks during the
   window; a client pinned to a minor the vault does not list gets a typed
   `409 incompatible_version`, never a silently mismatched shape.
5. **The ADR's compatibility sentence predates negotiation.** The
   [Versioning](../../decisions/2026-07-31-adepthood-http-application-api.md#versioning)
   section of `2026-07-31-adepthood-http-application-api.md` still states
   an exact-minor match (`client_minor == server_minor`). That was written
   before `supported_contract_minors` existed; the server now accepts any
   minor in that set. The accepted ADR is left as written and this section
   is the current policy.

`test_the_immediately_previous_minor_is_always_served` in
`creek-tools/tests/test_adepthood_contract_models.py` guards point 1. It
derives the previous minor from `CONTRACT_VERSION` rather than naming it, so
it cannot go stale at a bump.

## Security invariants held across every fixture in this bundle

- No fixture contains a real journal body, a credential, or a token.
- No fixture contains `"tier": "intimate"` or `"routed_tier": "intimate"`
  anywhere — this is not merely a convention: `WireTierCeiling` has exactly
  two members (`open`, `personal`), so `intimate` is not a constructible
  value on this wire at all.
- Every `examples/*/refusal.json` fixture is the **intimate example** for
  its capability: the closest this bundle comes to showing you above-ceiling
  content is showing you the refusal it produces instead. That is the point
  of publishing them, not an accident of the state axis's naming.
- Every payload is built from the runtime constants it documents
  (`CONTRACT_VERSION`, `ONTOLOGY_VERSION`, `CANONICAL_FREQUENCY_NAMES`,
  `CARE_SIGNAL`, `ERROR_MESSAGES`) rather than from hand-copied duplicates,
  so this bundle cannot describe a server that does not exist.

## Regenerating this bundle

This directory is written by `write_bundle()` in
`creek_mcp/api/bundle.py`, called against the repository's `docs/contracts/`
path. Regenerate it whenever a model in `creek_mcp/api/models.py` or a
fixture payload in `creek_mcp/api/bundle.py` changes; the committed
round-trip test will fail the build until you do. This README is excluded
from that generation and from the round-trip check — edit it directly.
