# BUG-008: `VaultWriter._write_model` writes frontmatter only — body content is lost

**Severity:** Critical
**Category:** BUG
**Estimated complexity:** M (≤1d)
**Parallelizable with peers in same category:** yes
**Discovered by:** Reading dimension 1 — `creek/vault/writer.py:283-285`

## Files affected
- `creek/vault/writer.py:260-289`

## Dependencies
Pairs with BUG-001 (pipeline drops `parsed.metadata["markdown"]`). Both must be fixed for the vault to contain real content.

## Blockers
Without this fix, fragments in the vault are headers-only — no usable text for downstream classification (rules need keywords from the body), embeddings (need text), voice analysis (need exemplar passages), or human reading.

## Reproduction
```python
from creek.vault.writer import VaultWriter
from creek.models import Fragment, FragmentSource, SourcePlatform
w = VaultWriter(Path("/tmp/vault"))
f = Fragment(title="Test", source=FragmentSource(platform=SourcePlatform.MARKDOWN))
path = w.write_fragment(f)
print(Path(path).read_text())
# ---
# id: frag-...
# title: Test
# ...
# ---
# (empty body)
```

The body of the resulting markdown file is empty. There is no parameter to `write_fragment` for body text.

## Analysis

```python
data = model.model_dump(mode="json")
post = frontmatter.Post(content="", **data)         # <— content="" hardcoded
content = frontmatter.dumps(post)
file_path.write_text(content, encoding="utf-8")
```

The `frontmatter.Post(content="")` hardcoding means every written file has a frontmatter block but no markdown body. The Fragment Pydantic model also has no `body` / `content` field — it carries metadata only. The body lives in the `ParsedFragment.content` produced by ingestion, which is dropped per BUG-001.

This explains why most tests pass: they assert on frontmatter shape, not body. But every downstream module that needs to read body text (rule classifier, embeddings, voice exemplar extractor, draft generator, redact-on-vault, even the human reading the file in Obsidian) gets nothing.

Confidence: verified.

## Proposed remediation

Add a `body` parameter to `VaultWriter.write_fragment(fragment, body)` and propagate it through `_write_model(model, target_dir, body=...)`. For non-fragment primitives (Thread, Eddy, etc.) the body should be a generated rendering — the templates in `00-Creek-Meta/Templates/` or programmatic markdown rendering. Either way, no `frontmatter.Post(content="", ...)` should remain.

Alternative: change Fragment to carry the body. That bloats the model and complicates classification (you don't usually want the body floating around with the metadata). The first option is cleaner.

## Acceptance criteria

- After ingestion, `<vault>/01-Fragments/Conversations/<fragment>.md` contains the converted markdown body below the frontmatter.
- A round-trip test reads the file back with `frontmatter.load`, validates the metadata into a `Fragment`, and asserts `post.content` equals the source body.
- Threads and Eddies get rendered bodies (e.g., "Fragments in this thread: ...") rather than empty content.
- Existing tests that assert on body-empty fragments are updated to assert on real bodies.

## References
- `creek/vault/writer.py:283-285`
- `creek/ingest/base.py:454-456` (where the markdown body is briefly held in `parsed.metadata["markdown"]` before being discarded)
- BUG-001
