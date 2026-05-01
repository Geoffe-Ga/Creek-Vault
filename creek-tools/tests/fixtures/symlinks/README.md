# Symlink failure-mode fixtures

Symlinks cannot be checked into the repository portably. Tests that
need a source directory containing a symlink should construct it at
runtime under ``tmp_path``:

```python
def test_redactor_does_not_follow_symlinks(tmp_path: Path) -> None:
    secret_outside = tmp_path / "outside.txt"
    secret_outside.write_text("AKIAIOSFODNN7EXAMPLE")

    source = tmp_path / "source"
    source.mkdir()
    (source / "link.txt").symlink_to(secret_outside)

    # Run the scanner / ingest path here; the symlink target is OUTSIDE
    # the source tree and must not be silently followed.
```

This file exists so the directory is preserved in source control. It
pairs with SEC-003 (symlink-claim-not-enforced).
