# =============================================================================
# @file:         tools/translation/pipeline.py
# @brief:        Translates Persian documentation (fa -> en) while keeping markdown and HTML structure.
# @details:      Responsibilities: Protect inline code, link targets, HTML tags, URLs and entities
#                with placeholders, translate line by line, restore the placeholders, copy fenced
#                code verbatim, remap Persian heading anchors to English slugs, translate
#                <script> strings and comments, and translate the _comment values of JSON examples.
#                Main API: main(), translate_markdown(), translate_html(), anchor_map_for()
#                Side effects: network I/O (hosted translation endpoint), file writes to the repo
# @dependencies: external: none (standard library only); the endpoint is Google's public gtx API
# @notes:        The hosted endpoint is rate-limited (HTTP 429) and may be unavailable. Results are
#                cached in TR_CACHE (default ~/.cache/printguard-translation-cache.json). Files are
#                written only after every selected file has been translated. Run with file paths to
#                limit the scope, e.g. `python tools/translation/pipeline.py README.md`.
# =============================================================================

"""Persian -> English translation of docs, preserving markdown/HTML structure.

Usage:
    python tools/translation/pipeline.py                 # all in-scope docs (see SCOPE)
    python tools/translation/pipeline.py README.md docs/architecture.html
"""

import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CACHE_PATH = os.environ.get(
    "TR_CACHE", os.path.join(os.path.expanduser("~"), ".cache", "printguard-translation-cache.json"))
PERSIAN = re.compile(r"[\u0600-\u06FF\u0750-\u077F\uFB50-\uFDFF\uFE70-\uFEFF]")
ZWNJ = "\u200c"
GENERATED = {"docs/oid-matrix.md"}   # produced by tools/build_oid_matrix.py; byte-exact --check
SCOPE_EXTRA = ["README.md", "README.en.md"]
SCOPE_HTML = ["docs/architecture.html"]
SCOPE_JSON = ["config/cartridge_id_map.example.json"]

_cache = {}


def _load_cache():
    """Load the translation cache from disk."""
    global _cache
    if os.path.exists(CACHE_PATH):
        with open(CACHE_PATH, encoding="utf-8") as f:
            _cache = json.load(f)


def _save_cache():
    """Save the translation cache to disk."""
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    tmp = CACHE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(_cache, f, ensure_ascii=False)
    os.replace(tmp, CACHE_PATH)


def gtx(text: str) -> str:
    """Translate one fa -> en string with the hosted gtx endpoint (cached, retried with backoff)."""
    if not text.strip():
        return text
    if text in _cache:
        return _cache[text]
    url = ("https://translate.googleapis.com/translate_a/single?client=gtx&sl=fa&tl=en&dt=t&q="
           + urllib.parse.quote(text))
    delay = 1.5
    for _ in range(10):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            out = "".join(seg[0] for seg in data[0] if seg and seg[0])
            _cache[text] = out
            return out
        except Exception:
            time.sleep(delay)
            delay = min(delay * 2, 30)
    raise RuntimeError("translation endpoint unavailable: " + text[:60])


# ─── protection of spans that must not be translated ───
PROTECT = re.compile(
    r"(?P<code>`[^`\n]+`)"
    r"|(?P<target>(?<=\]\()[^)\s]*(?=\)))"
    r"|(?P<tag></?[A-Za-z!][^>\n]*>)"
    r"|(?P<url>https?://[^\s)\]>\"']+)"
    r"|(?P<ent>&[A-Za-z]+;|&#\d+;)"
)
TOKEN_RE = re.compile(r"XQ(\d+)QX", re.IGNORECASE)


class ProtectError(Exception):
    """Raised when a placeholder does not survive translation exactly once."""


def protect(text):
    """Replace code, links and tags with placeholders before translation."""
    store = []

    def repl(m):
        """Replace a matched token with a placeholder and remember the original text."""
        store.append(m.group(0))
        return f"XQ{len(store) - 1}QX"

    return PROTECT.sub(repl, text), store


def restore(text, store):
    """Restore the placeholders with their original text after translation."""
    found = [int(m.group(1)) for m in TOKEN_RE.finditer(text)]
    if sorted(found) != list(range(len(store))):
        raise ProtectError(f"placeholder mismatch: expected {len(store)}, found {found}: {text[:80]}")
    return TOKEN_RE.sub(lambda m: store[int(m.group(1))], text)


def translate_span(core: str) -> str:
    """Translate one text span while keeping its placeholders intact."""
    masked, store = protect(core)
    out = gtx(masked)
    out = restore(out, store)
    return re.sub(r"\]\s+\(", "](", out)


# ─── markdown ───
FENCE = re.compile(r"^\s*(```|~~~)")


def translate_md_line(line: str) -> str:
    """Translate one Markdown line, keeping its indentation and trailing space."""
    m = re.match(r"^(\s*)(.*?)(\s*)$", line, re.S)
    lead, core, trail = m.group(1), m.group(2), m.group(3)
    if not PERSIAN.search(core):
        return line
    return lead + translate_span(core) + trail


def translate_markdown(text: str, errors: list, path: str) -> str:
    """Translate a Markdown document line by line, leaving fenced code unchanged."""
    out = []
    in_code = False
    fence_marker = None
    for n, line in enumerate(text.split("\n"), 1):
        fm = FENCE.match(line)
        if fm:
            if not in_code:
                in_code, fence_marker = True, fm.group(1)
            elif fm.group(1) == fence_marker:
                in_code = False
            out.append(line)
            continue
        if in_code:
            out.append(line)
            continue
        try:
            out.append(translate_md_line(line))
        except ProtectError as exc:
            errors.append(f"{path}:{n}: {exc}")
            out.append(line)
    return "\n".join(out)


# ─── heading anchors ───
HEADING = re.compile(r"^#{1,6}\s+(.*?)\s*#*\s*$")
LINK_ANCHOR = re.compile(r"\]\(([^)\s#]*)#([^)\s]*)\)")


def norm_key(s: str) -> str:
    """Normalize a string for comparison: remove ZWNJ and lowercase it."""
    return s.replace(ZWNJ, "").lower()


def gh_slug(heading: str) -> str:
    """Build a GitHub-style heading anchor slug."""
    h = heading.strip().lower()
    h = re.sub(r"<[^>]+>", "", h)
    h = re.sub(r"[`*~]", "", h)
    return "".join("-" if ch.isspace() else ch for ch in h if ch.isspace() or ch in "-_" or ch.isalnum())


def heading_pairs(old_text: str, new_text: str) -> list:
    """Pair the headings of an old and a new document by position."""
    pairs = []
    for o, n in zip(old_text.split("\n"), new_text.split("\n")):
        mo, mn = HEADING.match(o), HEADING.match(n)
        if mo and mn:
            pairs.append((mo.group(1), mn.group(1)))
    return pairs


def anchor_map_for(pairs: list) -> dict:
    """Build a map from old heading anchors to the new ones."""
    mp, used = {}, {}
    for old, new in pairs:
        key = norm_key(gh_slug(old))
        slug = gh_slug(new)
        if slug in used:
            used[slug] += 1
            slug = f"{slug}-{used[slug]}"
        else:
            used[slug] = 0
        mp.setdefault(key, slug)
    return mp


def rewrite_anchors(text: str, path: str, maps: dict, unresolved: list) -> str:
    """Rewrite links so that they point to the new heading anchors."""
    base_dir = os.path.dirname(path)

    def repl(m):
        """Rewrite one anchor link to its new target."""
        target_file, anchor = m.group(1), m.group(2)
        resolved = os.path.normpath(os.path.join(base_dir, target_file)) if target_file else path
        mp = maps.get(resolved)
        if mp is None or not PERSIAN.search(anchor):
            return m.group(0)
        key = norm_key(urllib.parse.unquote(anchor))
        if key in mp:
            return f"]({target_file}#{mp[key]})"
        unresolved.append(f"{path}: #{anchor} -> {resolved}")
        return m.group(0)

    return LINK_ANCHOR.sub(repl, text)


# ─── HTML and JSON ───
HTML_TEXT = re.compile(r">([^<>]*)<")
HTML_ATTR = re.compile(r'(\s(?:title|placeholder|alt|aria-label|name|label)=)"([^"]*)"')
JS_STRING = re.compile(r"'((?:[^'\\\n]|\\.)*)'|\"((?:[^\"\\\n]|\\.)*)\"")
JS_LINE_COMMENT = re.compile(r"//([^\n]*)")
JS_BLOCK_COMMENT = re.compile(r"/\*(.*?)\*/", re.S)


def _fix_quotes(s: str) -> str:
    """Replace straight quotes with typographic quotes."""
    return s.replace('"', "\u201d").replace("'", "\u2019")


def translate_text_piece(s: str) -> str:
    """Translate a text piece that contains Persian; other text is returned unchanged."""
    if not PERSIAN.search(s):
        return s
    m = re.match(r"^(\s*)(.*?)(\s*)$", s, re.S)
    return m.group(1) + _fix_quotes(translate_span(m.group(2))) + m.group(3)


def translate_js(js: str) -> str:
    """Translate the Persian strings and comments inside a JavaScript block."""
    def string_lit(m):
        """Translate one JavaScript string literal."""
        if m.group(1) is not None:
            body, q = m.group(1), "'"
        else:
            body, q = m.group(2), '"'
        if not PERSIAN.search(body):
            return m.group(0)
        if "${" in body:
            raise ProtectError("template interpolation inside string: " + body[:60])
        return q + _fix_quotes(translate_span(body)).replace(q, "\\" + q) + q

    js = JS_BLOCK_COMMENT.sub(lambda m: "/*" + translate_text_piece(m.group(1)) + "*/", js)
    js = JS_LINE_COMMENT.sub(
        lambda m: "//" + translate_text_piece(m.group(1)) if PERSIAN.search(m.group(1)) else m.group(0), js)
    return JS_STRING.sub(string_lit, js)


def translate_html(text: str, errors: list, path: str) -> str:
    """Translate the Persian text of an HTML document and keep the tags."""
    out, pos = [], 0
    for block in re.finditer(r"<(script|style)\b[^>]*>.*?</\1>", text, re.S | re.I):
        out.append(_html_text(text[pos:block.start()], errors, path))
        chunk = block.group(0)
        if re.match(r"<script", chunk, re.I):
            mm = re.match(r"(<script\b[^>]*>)(.*?)(</script>)$", chunk, re.S | re.I)
            try:
                chunk = mm.group(1) + translate_js(mm.group(2)) + mm.group(3)
            except ProtectError as exc:
                errors.append(f"{path} (script): {exc}")
        out.append(chunk)
        pos = block.end()
    out.append(_html_text(text[pos:], errors, path))
    return "".join(out)


def _html_text(chunk: str, errors: list, path: str) -> str:
    """Translate the text nodes and attributes of an HTML chunk."""
    def attr(m):
        """Translate one HTML attribute value."""
        try:
            return m.group(1) + '"' + translate_text_piece(m.group(2)).replace('"', "") + '"'
        except ProtectError as exc:
            errors.append(f"{path} (attr): {exc}")
            return m.group(0)

    def node(m):
        """Translate one HTML text node."""
        try:
            return ">" + translate_text_piece(m.group(1)) + "<"
        except ProtectError as exc:
            errors.append(f"{path}: {exc}")
            return m.group(0)

    return HTML_TEXT.sub(node, HTML_ATTR.sub(attr, chunk))


def translate_json_comments(orig: str) -> str:
    """Translate the comment values of a JSON file."""
    data = json.loads(orig)
    if isinstance(data.get("_comment"), list):
        data["_comment"] = [translate_text_piece(s) if isinstance(s, str) else s for s in data["_comment"]]
    new = json.dumps(data, indent=2, ensure_ascii=False)
    return new + "\n" if orig.endswith("\n") else new


# ─── driver ───
def in_scope():
    """Return the Markdown, HTML and JavaScript files that are in translation scope."""
    docs = os.path.join(REPO, "docs")
    md = [os.path.join("docs", n) for n in sorted(os.listdir(docs)) if n.endswith(".md")]
    md = [p for p in md if p not in GENERATED]
    return md + SCOPE_EXTRA, SCOPE_HTML, SCOPE_JSON


def main(argv=None):
    """Entry point: translate the in-scope files, or only the files given on the command line."""
    argv = list(sys.argv[1:] if argv is None else argv)
    md, html, js = in_scope()
    if argv:
        selected = {p.replace(os.sep, "/") for p in argv}
        md = [p for p in md if p in selected]
        html = [p for p in html if p in selected]
        js = [p for p in js if p in selected]
        unknown = selected - set(md) - set(html) - set(js)
        if unknown:
            sys.exit(f"not in translation scope: {sorted(unknown)}")

    _load_cache()
    originals, results, errors, unresolved = {}, {}, [], []
    try:
        for rel in md:
            originals[rel] = _read(rel)
            results[rel] = translate_markdown(originals[rel], errors, rel)
            print("translated", rel, flush=True)
        for rel in html:
            originals[rel] = _read(rel)
            results[rel] = translate_html(originals[rel], errors, rel)
            print("translated", rel, flush=True)
        for rel in js:
            originals[rel] = _read(rel)
            results[rel] = translate_json_comments(originals[rel])
            print("translated", rel, flush=True)
    finally:
        _save_cache()

    maps = {rel: anchor_map_for(heading_pairs(originals[rel], results[rel])) for rel in md}
    for rel in md:
        results[rel] = rewrite_anchors(results[rel], rel, maps, unresolved)

    if errors:
        print("errors (files NOT written):")
        for e in errors:
            print("  ", e)
        sys.exit(1)
    for rel, text in results.items():
        with open(os.path.join(REPO, rel), "w", encoding="utf-8", newline="") as f:
            f.write(text)
    print("unresolved anchors:", len(unresolved))
    for u in unresolved:
        print("  ", u)


def _read(rel: str) -> str:
    """Read a repository file as text."""
    with open(os.path.join(REPO, rel), encoding="utf-8", newline="") as f:
        return f.read()


if __name__ == "__main__":
    main()
