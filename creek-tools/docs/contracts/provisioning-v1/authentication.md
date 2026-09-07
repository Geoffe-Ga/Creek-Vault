# Provisioning v1 authentication and handoff

The provisioning API is a backend-to-backend service. A browser never holds its
bearer token and the control plane never accepts cookies, query credentials, or
a caller-supplied identity that disagrees with the authenticated token.

Each consumer receives a high-entropy bearer in the existing Creek registry
format, `consumer=current-token,replacement-token`. The service reads that
registry from a mounted file passed as `--consumer-tokens-file`; the bearer
value is not a command argument, image layer, or environment value. Rotation
uses the existing two-token window and then removes the retired value.

Every routable deployment terminates TLS. The service reuses Creek's canonical
transport-confidentiality gate and refuses a non-loopback bind without a valid
`--tls-cert`/`--tls-key` pair. Missing, malformed, and unknown bearer values are
refused above the router, before route or contract-version disclosure.

The authenticated identity must equal `consumer_identity` on activation.
Status, retry, and delete paths resolve jobs inside that identity boundary; a
valid token cannot enumerate another consumer's handles.

The public job schema contains no provider output or secret material. A worker
delivers the resulting service endpoint and consumer secret through the
injected `OneTimeCredentialHandoff`, a backend-internal one-time handoff keyed
by durable job id. Identical crash replays are acknowledged without a second
delivery; conflicting replays fail closed. That payload is never returned to
browser clients and has no public HTTP route.

Failure responses and logs carry only the stable reason enum. Provider detail,
provider tokens, passphrases, recovery material, unwrapped keys, vault content,
and handoff payloads are excluded.
