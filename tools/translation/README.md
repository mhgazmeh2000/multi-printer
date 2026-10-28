# tools/translation — Persian → English docs tooling

Tools used to extract and translate the Persian parts of the documentation. They do not touch code, configuration, or runtime data.

| File | Purpose |
|---|---|
| `persian_strings.json` | Manifest of every Persian line in the docs scope (5,382 entries, 30 files). Original text, untranslated. |
| `extract_manifest.py` | Regenerates the manifest from the repository. Line numbers refer to the file as it is now. |
| `pipeline.py` | Translation pipeline (fa → en) that keeps markdown, HTML, links, anchors and code intact. |
| `verify.py` | Checks translated files: leftover Persian outside code fences, broken relative links and anchors, leftover placeholders, `node --check` of the inline script in `docs/architecture.html`. |

## Scope

Defined in `pipeline.py`:

- `docs/*.md` (except the generated `docs/oid-matrix.md`), plus `README.md` and `README.en.md`.
- `docs/architecture.html`.
- `config/cartridge_id_map.example.json`, only the `_comment` values (the loader ignores them).

Out of scope and never touched: `monitoring/snmp-manifest.json`, `tests/fixtures/**`, and all code.

## Manifest format

```json
{"file": "docs/tech-debt.md", "line": 1, "text": "# بدهی فنی ...", "context": "heading"}
```

`context` is one of `heading`, `paragraph`, `table`, `code`. HTML text and JSON comments are recorded as `paragraph`; Persian inside `<script>` blocks is recorded as `code`.

Regenerate with:

```
python3 tools/translation/extract_manifest.py
```

## Running the pipeline

```
python3 tools/translation/pipeline.py                      # every in-scope file
python3 tools/translation/pipeline.py README.md            # one file
```

Behaviour:

- Inline code, link targets, HTML tags, URLs and entities are replaced by placeholders before translation and restored afterwards. A placeholder count mismatch is an error.
- Fenced code blocks are copied verbatim.
- Translation runs line by line. Joining paragraphs into one request loses newlines and breaks tables.
- Heading anchors are remapped to the English heading slugs in every file that links to them.
- Files are written only if no errors were recorded for any selected file.
- Results are cached in `~/.cache/printguard-translation-cache.json` (override with `TR_CACHE`).

**Limitations.** The default backend is Google's public `gtx` endpoint, which is rate limited (HTTP 429 after roughly 20 rapid requests). Do not parallelise it or retry in a tight loop. Machine output needs human proofreading.

## Verifying

```
python3 tools/translation/verify.py
```

The script exits with status 0 even when it reports problems; read the output. Persian lines reported under a file are text the verifier still sees as untranslated.

## Dependencies

Python 3 standard library only. `node` (v18+) is optional, for the script syntax check.
