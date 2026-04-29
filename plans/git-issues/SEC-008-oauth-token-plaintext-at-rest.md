# SEC-008: Google Drive OAuth refresh token stored as plaintext (no encryption-at-rest), with no operator hygiene guidance

**Severity:** Medium
**Category:** SEC
**Estimated complexity:** S (≤2h) — primarily a documentation issue
**Parallelizable with peers in same category:** yes
**Discovered by:** Dimension 2

## Files affected
- `creek/ingest/gdrive.py:393, 459-487` — token write path
- `creek-tools/docs/configuration.md:159` — references token file
- (Pairs with SEC-007 — overall threat model)

## Dependencies
SEC-007 (threat model).

## Reproduction
After running `creek gdrive --download` once:
```bash
ls -la token.json
# -rw------- 1 user user 4321 ... token.json   (good: 0o600)
cat token.json
# {"refresh_token": "1//0...", ...}             (plaintext)
```

The token grants long-lived `drive.readonly` access. `0o600` keeps other Unix users out, but anything running as the user (malware, an Obsidian plugin, a leaked clipboard, a backup that doesn't honour permissions) can lift the token.

## Analysis

The implementation is *correct given the design*: `0o600`, atomic write via temp + replace, no logging of contents. The gap is the design — the token is plaintext, and there's no documentation telling the user what that means or how to defend against it.

Specifically missing:
- No mention in `docs/configuration.md` of the security implication of `token_file`.
- No "rotate or revoke a leaked token" runbook.
- No advice to enable disk-level encryption (FileVault, LUKS) on the host.
- No `creek gdrive --revoke` command that invalidates the cached token (the user has to know to delete it manually and re-auth).

## Proposed remediation

1. Add a "Security considerations" subsection to `docs/configuration.md` `google_drive` section: explicit plaintext-at-rest disclaimer; advice on FileVault/LUKS; pointer to Google's revocation page; recommend deletion of `token.json` after every use if paranoid.
2. Add `creek gdrive --revoke` (delete the token file with secure-erase semantics; ideally also call Google's `oauth2.revoke` endpoint).
3. Optionally: support `keyring` (`pip install keyring`) as an alternative storage backend gated behind a config flag. Heavy lift; lower priority.
4. Cross-reference SEC-007 (threat model).

## Acceptance criteria

- The `docs/configuration.md` google_drive section explains the plaintext storage trade-off in 1-2 paragraphs.
- `creek gdrive --revoke` deletes the local token (and ideally calls Google's revocation endpoint).
- A user reading the docs would understand how to rotate / revoke a leaked token.

## References
- `creek/ingest/gdrive.py:459-487`
- `creek-tools/docs/configuration.md:148-161`
- Google OAuth token revocation: <https://developers.google.com/identity/protocols/oauth2/web-server#tokenrevoke>
- SEC-007
