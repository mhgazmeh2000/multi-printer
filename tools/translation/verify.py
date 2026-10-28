# =============================================================================
# @file:         tools/translation/verify.py
# @brief:        Checks translated docs for leftover Persian, broken links, anchors and JS syntax.
# @details:      Responsibilities: Report Persian lines outside fenced code, relative links to missing
#                files, same-file and cross-file anchors that do not match a heading, leftover
#                placeholders, and JavaScript syntax of inline <script> blocks (via node --check).
#                Main API: main()
#                Side effects: reads repo files; writes temporary .js files under the system temp dir
# @dependencies: external: node (optional, for the JavaScript syntax check)
# @notes:        Exit status is 0 even when problems are reported; read the output.
# =============================================================================

"""Verify the docs translation. Usage: python tools/translation/verify.py"""

import os
import re
import subprocess
import tempfile
import urllib.parse

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
PERSIAN = re.compile(r"[\u0600-\u06FF\u0750-\u077F\uFB50-\uFDFF\uFE70-\uFEFF]")
FENCE = re.compile(r"^\s*(```|~~~)")
HEADING = re.compile(r"^#{1,6}\s+(.*?)\s*#*\s*$")
LINK = re.compile(r"\[([^\]]*)\]\(([^)\s]+)\)")
ZWNJ = "\u200c"


def gh_slug(heading: str) -> str:
    """Build a GitHub-style heading anchor slug."""
    h = heading.strip().lower()
    h = re.sub(r"<[^>]+>", "", h)
    h = re.sub(r"[`*~]", "", h)
    return "".join("-" if ch.isspace() else ch for ch in h if ch.isspace() or ch in "-_" or ch.isalnum())


def anchors_of(text: str) -> set:
    """Return the set of heading anchors of a Markdown document."""
    slugs, used, in_code = set(), {}, False
    for line in text.split("\n"):
        if FENCE.match(line):
            in_code = not in_code
            continue
        m = None if in_code else HEADING.match(line)
        if m:
            s = gh_slug(m.group(1))
            if s in used:
                used[s] += 1
                s = f"{s}-{used[s]}"
            else:
                used[s] = 0
            slugs.add(s)
    return slugs


def _read(rel):
    """Read a repository file as text."""
    with open(os.path.join(REPO, rel), encoding="utf-8") as f:
        return f.read()


def main():
    """Entry point: run the verification checks on the translated documents."""
    docs = os.path.join(REPO, "docs")
    files = [os.path.join("docs", n) for n in sorted(os.listdir(docs)) if n.endswith((".md", ".html"))]
    files += ["README.md", "README.en.md"]
    texts = {f: _read(f) for f in files}
    anchors = {f: anchors_of(t) for f, t in texts.items() if f.endswith(".md")}

    print("== Persian left outside fenced code blocks")
    for f, t in texts.items():
        if f.endswith(".md"):
            in_code, cnt = False, 0
            for n, line in enumerate(t.split("\n"), 1):
                if FENCE.match(line):
                    in_code = not in_code
                    continue
                if not in_code and PERSIAN.search(line):
                    cnt += 1
                    if cnt <= 3:
                        print(f"   {f}:{n}: {line[:110]}")
            if cnt:
                print(f"   -> {f}: {cnt} line(s)")
        else:
            print(f"   {f}: {len(PERSIAN.findall(t))} Persian chars (html, incl. script/style)")

    print("== broken relative links and anchors")
    problems = 0
    for f, t in texts.items():
        if not f.endswith(".md"):
            continue
        in_code = False
        for n, line in enumerate(t.split("\n"), 1):
            if FENCE.match(line):
                in_code = not in_code
                continue
            if in_code:
                continue
            for m in LINK.finditer(line):
                target = m.group(2)
                if target.startswith(("http://", "https://", "mailto:")):
                    continue
                path, _, anchor = target.partition("#")
                tfile = os.path.normpath(os.path.join(os.path.dirname(f), path)) if path else f
                if path and not os.path.exists(os.path.join(REPO, tfile)):
                    print(f"   missing file: {f}:{n} -> {target}")
                    problems += 1
                    continue
                if anchor and tfile in anchors:
                    key = urllib.parse.unquote(anchor)
                    if key not in anchors[tfile] and key.replace(ZWNJ, "") not in {
                            a.replace(ZWNJ, "") for a in anchors[tfile]}:
                        print(f"   anchor not found: {f}:{n} -> {target}")
                        problems += 1
    print("   problems:", problems)

    print("== leftover placeholders")
    for f, t in texts.items():
        if re.search(r"XQ\d+QX", t, re.IGNORECASE):
            print("   leftover placeholder in", f)

    print("== JavaScript syntax of inline <script> blocks (docs/architecture.html)")
    html = texts["docs/architecture.html"]
    with tempfile.TemporaryDirectory() as tmp:
        for i, js in enumerate(re.findall(r"<script\b[^>]*>(.*?)</script>", html, re.S | re.I)):
            path = os.path.join(tmp, f"script_{i}.js")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(js)
            try:
                r = subprocess.run(["node", "--check", path], capture_output=True, text=True)
                status = "OK" if r.returncode == 0 else "SYNTAX ERROR " + r.stderr[:300]
            except FileNotFoundError:
                status = "skipped (node not installed)"
            print(f"   script {i}: {status}")


if __name__ == "__main__":
    main()
