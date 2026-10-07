# Managed-vault Fly pilot

This is the production checklist for the first provider-managed Creek pilot.
It supplements the component reference in
[provisioning-control-plane.md](provisioning-control-plane.md). It is not an
authorization to spend money. Stop before the first provider mutation until
the owner records explicit cost authorization, the exact organization, region,
public domains, alert destination, cleanup window, and maximum permitted spend.

The pilot is intentionally bounded to five live allocations and a USD 25
monthly alert. Fly does not supply a hard billing limit or native billing alert,
so the Creek admission fence is the resource cap and the scheduled fleet report
plus the operator's monitoring destination is the alert. A free allowance is
not a cap.

## 1. Freeze and authorize the coordinates

1. Record the exact Creek `origin/main` SHA and require exact-main CI plus an
   independent LGTM. Build only that clean commit. Record the Adepthood main SHA
   separately.
2. Record the approved maximum spend, pilot duration, region, two TLS names,
   alert recipient, and cleanup owner. Do not infer any of them from an existing
   account.
3. Create or select a dedicated Fly organization that contains only the pilot
   control app, image-carrier app, and managed-vault apps. A personal
   organization is not a dedicated Fly organization.
4. Bootstrap a named, org-scoped deploy token with an expiry of no more than
   seven days. The service and every pilot automation use that scoped token,
   never a personal token. Store it in an owner-only mounted file; never put it
   in a checked-in file, command argument, log, evidence bundle, or long-lived
   environment value. Record only its non-secret token ID and expiry so the
   owner can rotate or revoke it with `fly tokens revoke <token-id>`.
5. Use a separately scoped read-only org token for evidence collection when a
   mutation is not required. Revoke both tokens when the pilot window closes.

Fly's current token commands and scope definitions are documented in
[Access tokens](https://fly.io/docs/security/tokens/). A typical owner-driven
bootstrap is `fly tokens create org --name <name> --expiry 168h`; the token is
written directly to a mode-0400 file rather than copied into a transcript.

## 2. Publish immutable images

Build the root `Dockerfile` for vault Machines and
`Dockerfile.control-plane` for the single control Machine from the frozen main
SHA. Push both to a private registry in the dedicated organization and resolve
each registry manifest to a full
`registry.fly.io/<carrier>@sha256:<64-hex>` immutable digest. Never pass a tag
to `--fly-image` or the deploy helper. The worker, replay router validation
policy, fleet command, live Machine inspection, and restore drill must all name
the vault digest; the deployed control Machine must report the separate control
digest.

Fly documents private image publication and cross-app reuse within one
organization in
[Managing Docker images](https://fly.io/docs/blueprints/using-the-fly-docker-registry/).
After publication, compare the manifest digest with each Machine's returned
`image_ref.digest`; a matching tag is not evidence.

## 3. Deploy one supervised control Machine

The provisioning API, worker, router, and scheduled fleet command share one
durable SQLite/storage boundary and one failure domain. Deploy
`creek-managed-vault-pilot` as exactly one Fly Machine with exactly one volume;
do not place the boundaries in independent process groups or attach copies of
the volume. The process supervises its public edge, durable worker, and fleet
schedule: an early component exit makes `/__fly/health` return an empty `503`,
stops its peers, and lets the reviewed Fly restart policy replace the process.
Health remains unavailable until the edge and worker are ready and the first
provider-authoritative fleet report has completed. A stale schedule or a
provider-failure exit also returns `503`; report exit `3` is the expected
alert-delivery result and remains healthy, while exit `1` is a failure.
`SIGTERM` closes admission to new work, lets the current bounded claim settle,
stops all components, then releases provider transports. Mount
`jobs.sqlite3` and the encrypted runtime-secret directory below `/data` so one
atomic volume snapshot captures both. Keys and bearer credentials remain
separate Fly secret-backed files and are not part of that snapshot.

- The one `*.fly.dev` endpoint dispatches `/control/v1/*` to the control API
  and `/v1/*` to the replay router. It preserves the original bearer for the
  selected authentication realm and never tries either token against the
  other. It accepts application traffic only with exactly one expected `Host`,
  one `X-Forwarded-Proto: https`, and one `Fly-Forwarded-Port: 443`, then strips
  all forwarding and replay headers before dispatch. `/__fly/health` is the
  sole content-free exception for Fly service checks.
- Fly Proxy terminates public TLS for this dedicated listener and forwards
  plaintext over Fly's private WireGuard mesh. This narrow Fly-only entry point
  is allowed to bind `0.0.0.0:8080`; the generic API and router executables
  continue to reject non-loopback plaintext. Do not install a leaf TLS key in
  the control Machine.
- Build the control image from `Dockerfile.control-plane`. Its root bootstrap
  validates that `/data` is the exact mounted volume, narrows ownership and
  modes only for `/data`, `/data/runtime-secrets`, and the seven declared
  secret-backed files, then irreversibly drops groups, gid, and uid to `10001`
  before importing the long-running runtime. Secret values in environment
  variables, symlinked files, empty files, extra links, and oversized files
  are startup failures.
- A replay vault Machine explicitly starts as root only through
  `creek_mcp.provisioning.fly_vault_bootstrap`. That bootstrap validates the
  exact `/vault` mount and the two exact injected consumer/replay files,
  normalizes only those paths, then irreversibly drops groups, gid, and uid to
  `10001` before importing the plaintext-behind-Fly-Proxy vault runtime.
  The image-user override and bootstrap `exec` command share exactly one
  `config.processes` entry; Fly ignores `config.user`, and process overrides do
  not inherit `config.init.exec`. Both the process list and command are verified
  before accepting a vault. On a fresh ext4 mount, bootstrap removes `lost+found`
  only when it is the sole entry and an empty, non-symlink directory. Recovered
  data and existing vault contents are never removed or overwritten.
- Preserve the hard live-allocation cap of five and Fly replay's inclusive
  1 MiB body ceiling. The pilot journal lifecycle is supported; larger
  `/v1/uploads` requests are unavailable during the pilot and receive Creek's
  existing published `422` before any replay header is emitted.
- Schedule provider-authoritative `report` passes in the same process without
  writing raw report bodies or alert subjects to platform logs. An alert pass
  posts only the closed alert-kind counts to the configured authenticated
  HTTPS sink; only `204` after at most three bounded attempts is delivery.
  Exit `0` is clean and exit `3` means alerts were delivered; both refresh
  health. Exit `1`, alert-delivery failure, or a report older than two scheduled
  intervals makes the Machine unhealthy. The reviewed Fly config also enables
  serialized `reconcile` passes every 3,600 seconds with a 120-second accepted
  interruption window. Report and reconcile never overlap, and an over-window
  reconcile makes health fail closed. Omitting both schedule values disables
  repair; configuring only one or a window at least as long as its cadence is
  a startup refusal.

Render `deploy/fly-pilot/fly.toml.template` only through
`creek-fly-pilot-deploy render`. The renderer rejects mutable images,
placeholder drift, and delimiter-bearing coordinates and pins `app`, one
`[[http_service.checks]]`, one volume mount, `force_https`, restart policy, and
one `[[files]]` entry per base64 Fly app secret. It also pins a rolling deploy
with one unavailable Machine, `SIGTERM`, a 300-second Fly kill timeout, and
`on-failure` with three retries. The operator must first create
the named app and volume and set each declared base64 secret out of band. The
live `deploy` mode requires the SHA-256 of a private, short-lived authorization
record bound to the exact app, organization slug and private provider
organization ID, region, the exact USD 25.00 pilot spend cap, and
`cross_network_replays_enabled=true`. Pass the approved mode-0400 or mode-0600
org-token file with `--token-file`; the helper requires the effective operator
to own the regular file, admits Fly's exact `FlyV1 ` prefix separator and
provider token alphabet, rejects symlinks and every other whitespace,
multiline, control-bearing, delimiter-bearing, or oversized value, strips all
ambient credentials and Fly configuration from the child process, and creates a
disposable mode-0700 `HOME` with a sole mode-0600 flyctl config. The helper
writes the complete validated token as flyctl's unquoted `access_token` scalar;
flyctl's official parser removes the authorization scheme itself. A fresh
`last_login` value marks only the start of that disposable command session so
current flyctl does not misclassify the file-backed scoped token as an expired
interactive login; the token's provider-enforced caveats and expiry remain
authoritative. The owner-only source file remains unchanged. The token is never
placed in argv or an environment value, and the home is removed on every exit.
Runtime credentials remain owner-only mounted files. Before
mutation the helper verifies the exact organization slug/ID and the target
app's membership with read-only flyctl calls. It also inventories Machines and volumes: only an
empty pair or one already exact, admissible Machine/volume pair may proceed.
Duplicate, partial, drifted, or uninspectable state is refused before mutation.
It then runs
`fly deploy --ha=false`; the supervised command is pinned to
`--maximum-live-allocations 5` and
fails unless provider output reports exactly one started Machine and one
encrypted attached volume in the approved region, with the exact control image
digest, guest shape, restart policy, `/data` mount, and public service. It
never prints provider identifiers or response bodies.

Each vault remains on its own custom private network. Before the authorized
pilot window, the owner explicitly runs
`fly orgs cross-network-replays enable --org <dedicated-org> --yes` and records
that private fact; the deploy helper never changes this organization-wide
setting. Public routing returns Fly's documented empty-body `307` with a
closed-shape `Fly-Replay` app/Machine/state target only after exact Machine and
encrypted-volume verification. The vault accepts plaintext only in its
Fly-specific runtime, and only when a single closed-shape `Fly-Replay-Src`
state matches its mounted per-allocation secret **and** the original bearer is
valid. Source metadata fields are semicolon-separated, as emitted by Fly Proxy.
Direct, spoofed, duplicate, conflicting, comma-separated, or mixed-delimiter
headers are `401`. The ordinary single-vault Docker runtime keeps its private TLS
certificate and key requirement unchanged.

Restart the combined process with `--disable-new-activations` to close admission. An exact
existing activation ID remains replayable, while every new activation ID gets
`503 activation_unavailable`. The refusal is committed under the same SQLite
write fence as the live count, makes no provider request, and creates no alias,
job, app, Machine, root filesystem, volume, or credential. Status, routing,
export, retry, and deletion for existing allocations remain available.

## 4. Configure reporting and alert delivery

The pilot policy file uses `currency = "USD"` and
`monthly_budget = 25.00`. Fill every current Fly rate from the dated pricing
source or the actual invoice; do not copy stale example rates. Missing snapshot,
egress, or root-filesystem inputs remain unknown and unpriced in the report and
are never entered as zero merely to obtain a clean estimate.

Each scheduled report captures provider inventory and fails visibly for:

- `duplicate_resource`;
- `orphan_resource`;
- `stuck_deletion`;
- `continuous_running`;
- `incomplete_create` when a failed create remains terminal after bounded
  reconciliation;
- `monthly_budget_departure`;
- unknown and unpriced cost inputs.

Configure `/internal/vault-provisioning/alerts` on the approved Adepthood HTTPS
origin with the existing mounted handoff bearer. The supervised process sends
exactly `{"schema":"creek_fleet_alert_v1","counts":{...}}`; every key is one
closed alert kind and every value is a positive aggregate count. It never
sends an alert subject, allocation/job/provider identifier, URL, or response
body. The sink must return `204`; transport failures and transient statuses are
retried within the fixed three-attempt, five-second-per-attempt budget, and any
remaining failure makes the supervised schedule unhealthy.
Exercise alert delivery before admitting a user: temporarily set a test budget
below the observed non-zero estimate, confirm `monthly_budget_departure` exits
3 and reaches the recipient, then restore USD 25 and confirm the next scheduled
report. The transcript records the alert kind, delivery timestamp, and
acknowledgement only—not provider IDs, tokens, URLs, or response bodies. Run the
standalone report only in the approved private evidence environment when its
full subject/inventory detail is required.

The supervised schedule runs reconcile less frequently than report because it
may stop a Machine and interrupt background work. The reviewed config pins a
3,600-second cadence and a 120-second accepted interruption window; the single
scheduler thread makes overlap impossible and fails health if the pass returns
late. Record the observed duration and prove that retryable stuck deletion is
requeued while non-retryable and orphan resources remain report-only.

## 5. Reviewed bounded fault drills

These drills run only after the owner approves a private drill record naming
the exact frozen revisions, one allocation, a start/end window of at most one
hour, the operator and observer, the scoped network rule or drill proxy, and
the rollback owner. Production executables accept no fault-injection flag and
do not read the drill record. The fault mechanism is an external, temporary,
destination-scoped network rule or an isolated drill proxy created for the
window and removed immediately afterward. It must not be reused as a normal
production dependency.

Never revoke a token to create a fault, alter an organization-wide provider
policy, delete a provider resource, replace a credential, or weaken TLS. Before
each drill capture provider-authoritative allocation-scoped cardinality. Keep
the admission cap at five but admit no other activation during the drill. If
the scoped control affects another allocation, changes resource cardinality,
survives its approved window, or cannot be removed immediately, abort the drill,
disable new admissions, remove the fault, and run a clean inventory report.

### Create after provider create, before handoff

1. Pre-create a temporary rule that blocks only the pilot worker's egress to
   the exact callback TLS name. Verify provider and router egress remain healthy.
2. Submit one new activation and run one worker process with `--max-jobs 1`.
   Observe privately that the provider-authoritative app, Machine, and volume
   exist while the durable job has not completed handoff. Terminate only that
   worker process during its bounded callback wait; do not stop or edit the
   created Machine.
3. Remove the callback rule before the job lease expires. Verify the durable
   retryable job, unchanged one-of-each provider cardinality, then restart the
   reviewed worker revision. It must converge the same activation through
   idempotent provider create and handoff with zero unexpected billable
   resources. Delete through the public control contract and confirm absence.

### Provider outage

1. Apply a temporary rule or isolated proxy response that blocks only the pilot
   worker's requests to `api.machines.dev`; callback and routing traffic remain
   reachable. Do not change or revoke the org-scoped token.
2. Submit one activation and run the worker once. Require a durable retryable
   `provider_unavailable` result and provider-authoritative zero allocation
   resources. Any resource appearance fails the drill.
3. Remove the rule, verify provider inventory is readable, and retry the same
   activation. It must converge once without unexpected billable resources;
   then delete it and confirm a clean report.

### Callback outage

1. Apply a temporary rule that blocks only the worker's egress to the exact
   Adepthood callback TLS name. Provider API and public router probes stay green.
2. Submit one activation and let the worker's configured callback timeout
   expire. Require a durable retryable `handoff_failed` result and exactly one
   allocation-scoped app, Machine, volume, credential, and job.
3. Remove the rule and retry the same activation. The existing provider
   resources must be reused, handoff must converge, and no unexpected billable
   resource may appear. Finish with public deletion and clean inventory.

### Readiness timeout

1. Route only one pilot router process's Fly API traffic through an isolated
   drill proxy that passes every request except the selected Machine's bounded
   provider wait request, which it holds past the configured readiness timeout.
   The private proxy configuration may contain the Machine ID; sanitized
   evidence contains only its artifact hash.
2. Request a route for the existing stopped pilot allocation. Require the
   router to refuse the request without proxying a body or issuing a second
   create. Provider-authoritative cardinality and the Machine identity stay
   unchanged.
3. Remove the proxy and restore the reviewed direct Fly API base URL. The next
   request must start and route the same Machine. A different Machine, a leaked
   response body, or an unbounded wait fails the drill.

### Routing failure

1. Apply a temporary inbound rule that blocks only public edge traffic to the
   reviewed router deployment. Do not alter the public DNS name, TLS material,
   vault Machine, provider token, or control plane.
2. From Adepthood, require the bounded request to return the documented generic
   unavailable path; it must not report ready, expose a private address, or
   create another provider resource.
3. Remove the rule, confirm the same endpoint and certificate serve the
   authenticated health probe, then route the same allocation successfully.
   Public deletion must still converge afterward.

Each private drill record is one of the exact `fault_drills` inputs consumed by
`creek-provisioning-pilot-evidence`. The reducer accepts only passed,
reversible, non-destructive outcomes with zero unexpected billable resources;
it performs no drill and contacts no network.

## 6. Backup, restore, and stop drills

### Atomic SQLite/runtime-state snapshot

Quiesce the API, worker, and router, wait for the current bounded worker claim
to settle, then take one atomic snapshot of the storage containing both
`jobs.sqlite3` and the encrypted runtime-secret directory. The master key, Fly
token, callback bearer, public TLS key, and CA private key stay in the secret
manager and out of the snapshot. Restart the original services, restore the
snapshot to an isolated path, and start isolated API/worker/router processes
against it with network mutation disabled. Verify schema, job/allocation counts,
encrypted credential membership, deletion receipts, and idempotent restart.
That is the SQLite restore exercise; a byte copy of a live database is not.

### Stopped vault-volume snapshot

Stop the pilot vault Machine without destroying it, create an on-demand volume
snapshot, wait until the snapshot is `created`, and restore it to a new
equal-or-larger test volume in the same region. Fly documents the sequence in
[Manage volume snapshots](https://fly.io/docs/volumes/snapshots/) and
[Create and manage volumes](https://fly.io/docs/volumes/volume-manage/).
Attach the restored volume only to a temporary Machine pinned to the reviewed
immutable digest, verify a pre-authorized content-free sentinel through the
authenticated API, then remove the temporary Machine and restored volume after
separate destructive cleanup authorization. Never destroy the original volume
as part of the restore proof.

Run `creek-provisioning-fleet emergency-stop` against the live pilot. It stops
each owned Machine, continues past a single failure, and destroys no volume,
Machine, app, or credential. Prove existing routing restarts one allocation and
that deletion still converges afterward. The emergency stop is not the teardown
procedure.

## 7. Reconcile one invoice and close the pilot

From the dedicated organization's billing page, take one provider invoice or
current usage sample. Copy only the dated aggregate snapshot bytes, egress
bytes, and running seconds into the private policy input. Run `report` (or
`report --record-month YYYY-MM` for a closed month) and perform invoice
reconciliation line by line against volume storage, stopped root filesystem,
compute, snapshots, and egress. Record the estimate, provider aggregate, delta,
rate date/source, and every unpriced component. Never manufacture a zero.

Before closure, disable admission, delete the test account through the public
control contract, wait for its content-free receipt to confirm credential,
Machine, volume, and app absence, and run a clean fleet report. Revocation of
the short-lived org token is the last provider mutation after inventory is
clean. Disable cross-network replays for the dedicated organization before that
revocation; do not leave an organization-wide bridge enabled after teardown.

## 8. Evidence boundary

Keep raw provider output and billing documents in the approved private evidence
store, not Git. Publish only a sanitized checklist with exact source SHAs,
immutable image digests, UTC timestamps, test counts, alert kinds, aggregate
costs, exit statuses, and hashes of private artifacts. Production credentials,
production user identifiers, vault URLs, provider resource identifiers, corpus
content, request/response bodies, TLS keys, and secret filenames never enter a
checked-in file, issue comment, CI log, screenshot, or captured evidence.

The final record must show: exact-main CI and deployment verification green;
independent LGTM; cap and disable tests; one activation/restart/route/delete
lifecycle; a clean scheduled report; exercised alert delivery; the scheduled
reconcile interruption window; emergency-stop recovery; SQLite restore; volume
snapshot restore; invoice reconciliation; and final resource/token cleanup.

Prepare that public record with
`creek-provisioning-pilot-evidence reduce --input <private-observations.json>`.
The input is a closed private shape and the command is offline. It validates
mandatory pass facts, derives allocation-scoped cardinalities from unique
private identifiers, hashes the exact private artifacts with SHA-256, and emits
only the versioned `managed_vault_pilot_prerequisite` block. Inspect the public
schema without private input with `creek-provisioning-pilot-evidence schema`.
Never check in the input, its paths, or the raw provider/fleet/emergency output.
