# =============================================================================
# @file:         tools/translation/extract_manifest.py
# @brief:        Writes persian_strings.json: every Persian line in the docs translation scope.
# @details:      Responsibilities: Walk the in-scope docs, JSON examples and HTML page, and record
#                each line that contains Persian with its file, line number, text and context
#                (heading, table, code, paragraph; schema of the owner's request). Nothing is translated here.
#                Main API: build_manifest(), main()
#                Side effects: writes tools/translation/persian_strings.json
# @dependencies: internal: tools/translation/pipeline.py (scope definition)
# @notes:        Regenerate after editing the docs. Line numbers refer to the file as it is now.
# =============================================================================

"""Extract the Persian strings of the docs scope into a JSON manifest (no translation)."""

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pipeline  # noqa: E402  (scope definition lives there)

PERSIAN = pipeline.PERSIAN
FENCE = re.compile(r"^\s*(```|~~~)")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "persian_strings.json")


def _context(line: str, in_code: bool) -> str:
    """Return the manifest context (heading, paragraph, table or code) of a line."""
    if in_code:
        return "code"
    s = line.lstrip()
    if s.startswith("#"):
        return "heading"
    if s.startswith("|"):
        return "table"
    return "paragraph"


def _md_entries(rel: str):
    """Collect the translation entries from one Markdown file."""
    in_code, marker = False, None
    with open(os.path.join(pipeline.REPO, rel), encoding="utf-8") as f:
        for n, line in enumerate(f.read().split("\n"), 1):
            fm = FENCE.match(line)
            if fm:
                if not in_code:
                    in_code, marker = True, fm.group(1)
                elif fm.group(1) == marker:
                    in_code = False
                if PERSIAN.search(line):
                    yield n, line, "code"
                continue
            if PERSIAN.search(line):
                yield n, line, _context(line, in_code)


def _html_entries(rel: str):
    """Collect the translation entries from one HTML file."""
    in_script = False
    with open(os.path.join(pipeline.REPO, rel), encoding="utf-8") as f:
        for n, line in enumerate(f.read().split("\n"), 1):
            low = line.lower()
            if "<script" in low:
                in_script = True
            if PERSIAN.search(line):
                yield n, line, "code" if in_script else "paragraph"
            if "</script" in low:
                in_script = False


def _json_entries(rel: str):
    """Collect the translation entries from one JSON file."""
    with open(os.path.join(pipeline.REPO, rel), encoding="utf-8") as f:
        for n, line in enumerate(f.read().split("\n"), 1):
            if PERSIAN.search(line):
                yield n, line, "paragraph"


def build_manifest():
    """Build the full manifest of translation entries."""
    md, html, js = pipeline.in_scope()
    entries = []
    for rel in md:
        for n, text, ctx in _md_entries(rel):
            entries.append({"file": rel, "line": n, "text": text.strip(), "context": ctx})
    for rel in html:
        for n, text, ctx in _html_entries(rel):
            entries.append({"file": rel, "line": n, "text": text.strip(), "context": ctx})
    for rel in js:
        for n, text, ctx in _json_entries(rel):
            entries.append({"file": rel, "line": n, "text": text.strip(), "context": ctx})
    return entries


def main():
    """Entry point: build the manifest and write it to disk."""
    entries = build_manifest()
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(f"wrote {len(entries)} entries to {os.path.relpath(OUT, pipeline.REPO)}")


if __name__ == "__main__":
    main()
