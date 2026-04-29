# SEC-003: Redactor's "refuses to write through symlinks" claim is not enforced

**Severity:** High
**Category:** SEC
**Estimated complexity:** S (≤2h)
**Parallelizable with peers in same category:** yes
**Discovered by:** Reading dimension 2; confirmed by parallel agent

## Files affected
- `creek/redact/redactor.py:152-163` — `log_redactions` write
- `creek-tools/docs/redaction.md:75` — claim "Refuses to write through symlinks (path-traversal guard)"

## Dependencies
None.

## Blockers
None.

## Reproduction
```bash
mkdir /tmp/source
echo "AKIA1234567890ABCDEF" > /tmp/source/file.txt
ln -s /etc/sensitive_target /tmp/source/.creek-redactions/queue.json
creek redact --apply --source /tmp/source -y
# Symlink is followed; the redaction queue overwrites /etc/sensitive_target
```

(Or any equivalent setup pointing the queue/log path through a symlink to outside the source tree.)

## Analysis

`docs/redaction.md` line 75 promises: "Refuses to write through symlinks (path-traversal guard)." A grep for `is_symlink`, `lstat`, `realpath`, or `os.path.realpath` in `creek/redact/` returns nothing. The actual writes use `Path.write_text` / `Path.open` directly, both of which follow symlinks.

This means a malicious or accidental symlink under the source tree (e.g., `.creek-redactions` → arbitrary path) will cause `--apply` to write to the symlinked target. With sufficient privileges, this could overwrite system files; even without privilege escalation, it could leak the redaction queue (which contains *the matched secrets, the patterns that matched, and offsets* — see `creek/redact/scanner.py`) outside the intended directory.

The symlink claim was probably aspirational. The fix is straightforward.

Confidence: verified — read redactor.py and grep'd.

## Proposed remediation

Before any write inside `--apply`:
- Resolve the target path with `Path.resolve(strict=False)`.
- Confirm the resolved path is a descendant of the source root (also resolved). If not, abort with a clear error.
- For each output file, verify `path.is_symlink() is False` AND `path.parent.is_symlink() is False` along the chain (or use the resolved-vs-source comparison instead).

Same guard should apply to: queue writing in `--scan` mode, `--review` reads from the vault, and the redacted source-file write itself.

Document a concrete attack scenario in the function docstring so the test exists for a reason.

## Acceptance criteria

- A test creates a symlink inside the source tree pointing outside it. `creek redact --apply` against the source raises a clear error and writes nothing.
- The same redaction operation against a symlink-free source proceeds normally.
- The unit-test fixture covers both `<source>/.creek-redactions/queue.json` being a symlink and a deeply nested path being a symlink.
- Documentation matches behaviour (or is removed if the project decides not to defend symlinks).

## References
- `creek-tools/docs/redaction.md:75`
- `creek/redact/redactor.py:152-163`
