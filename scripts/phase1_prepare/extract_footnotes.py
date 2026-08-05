"""Extract EPUB footnotes and convert them to Markdown [^N] format.

PROBLEM THIS SCRIPT SOLVES:
Some EPUBs (notably Brandon Sanderson's "Alcatraz" series from Tor) put
footnote BODIES in a separate XHTML file (e.g. OEBPS/Text/part0039.xhtml)
as `<li epub:type="footnote" id="...">...</li>`. The main text contains
anchors like `[*](part0039.xhtml#ID)` referencing these footnotes.

When `convert.py` runs pandoc on just the main HTML file (the largest
one), it loses access to the footnote bodies. The anchors get converted
to raw pandoc markup like:
  `[[*](part0039.xhtml#chapter1-1){#cha-1 .noteref epub:type="noteref"}]{.epub-sup-fn}`
which is NOT a Markdown footnote (`[^N]`). Sub-agents copy this raw
markup into Russian output verbatim, and the FB2 builder can't link
them — dead links.

WHAT THIS SCRIPT DOES:
1. Looks for `_epub_extracted/` directory (created by `convert.py` or
   by the custom `_build_input.py` script). If not found, tries to
   re-extract from the source EPUB.
2. Scans ALL XHTML files for `<li epub:type="footnote" id="...">...</li>`
   blocks — extracts footnote bodies (cleaning HTML inside).
3. Scans all `chunkNNNN.md` files for EPUB footnote anchors (multiple
   pandoc-emitted formats — see ANCHOR_PATTERNS below).
4. For each anchor found, assigns a GLOBALLY UNIQUE number (1..N across
   the whole book, not per-chunk) and replaces the anchor with `[^N]`.
5. Appends `[^N]: <footnote body>` definitions to the END of the chunk
   where the anchor appears.
6. Writes `process/footnotes_extracted.json` with the mapping for audit.

IMPORTANT: numbering is GLOBAL across the book. fb2_builder.py collects
all `[^N]:` definitions into a single <body name="notes">; per-chunk
numbering would cause `id="note_1"` collisions.

Usage:
    python3 extract_footnotes.py <temp_dir> [--epub <path_to_epub>]

    --epub is optional; if given, the script will re-extract XHTML files
    from the EPUB if `_epub_extracted/` is not found or is incomplete.

Run AFTER convert.py (it's a post-step). Idempotent: re-running on
already-processed chunks is safe (anchors already replaced won't match
the patterns again).
"""

import html
import re
import shutil
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "shared"))

from common import process_dir

# ─────────────────────────────────────────────────────────────────────
# Footnote body extraction from XHTML
# ─────────────────────────────────────────────────────────────────────


# Pattern for <li epub:type="footnote" id="...">...</li>
# We use regex (not BS4) because EPUB XHTML files use namespaces that
# make BeautifulSoup parsing fragile. The pattern is tolerant of
# attribute order and extra attributes.
FOOTNOTE_LI_RE = re.compile(
    r'<li[^>]*epub:type=["\']footnote["\'][^>]*\bid=["\']([^"\']+)["\'][^>]*>(.*?)</li>',
    re.DOTALL | re.IGNORECASE,
)
# Also try with id before epub:type (attribute order varies)
FOOTNOTE_LI_RE2 = re.compile(
    r'<li[^>]*\bid=["\']([^"\']+)["\'][^>]*epub:type=["\']footnote["\'][^>]*>(.*?)</li>',
    re.DOTALL | re.IGNORECASE,
)


def clean_html_to_markdown(html_str: str) -> str:
    """Convert simple HTML inside a footnote body to Markdown text.

    Handles: <i>, <em> → *...*; <b>, <strong> → **...**; <small> → text;
    <a class="reversefootnote"> → removed; <p> → paragraph break;
    everything else → text content. HTML entities are unescaped.
    """
    # Remove reversefootnote links (the "back to text" arrows)
    html_str = re.sub(
        r'<a[^>]*class=["\']reversefootnote["\'][^>]*>.*?</a>',
        "",
        html_str,
        flags=re.DOTALL | re.IGNORECASE,
    )
    html_str = re.sub(
        r'<a[^>]*rev=["\']footnote["\'][^>]*>.*?</a>',
        "",
        html_str,
        flags=re.DOTALL | re.IGNORECASE,
    )
    # <i>...</i>, <em>...</em> → *...*
    html_str = re.sub(
        r"<(?:i|em)>(.*?)</(?:i|em)>",
        r"*\1*",
        html_str,
        flags=re.DOTALL | re.IGNORECASE,
    )
    # <b>...</b>, <strong>...</strong> → **...**
    html_str = re.sub(
        r"<(?:b|strong)>(.*?)</(?:b|strong)>",
        r"**\1**",
        html_str,
        flags=re.DOTALL | re.IGNORECASE,
    )
    # <small>...</small> → just text
    html_str = re.sub(
        r"<small>(.*?)</small>",
        r"\1",
        html_str,
        flags=re.DOTALL | re.IGNORECASE,
    )
    # <p>...</p> → text + newline
    html_str = re.sub(
        r"<p[^>]*>(.*?)</p>",
        r"\1\n",
        html_str,
        flags=re.DOTALL | re.IGNORECASE,
    )
    # Remove all other tags
    html_str = re.sub(r"<[^>]+>", "", html_str)
    # Unescape HTML entities
    html_str = html.unescape(html_str)
    # Collapse whitespace
    html_str = re.sub(r"\s+", " ", html_str).strip()
    return html_str


def extract_footnote_bodies_from_xhtml(xhtml_path: Path) -> dict[str, str]:
    """Extract all footnote bodies from one XHTML file.

    Returns {footnote_id: cleaned_markdown_text}.
    """
    try:
        content = xhtml_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}

    bodies: dict[str, str] = {}
    for pattern in (FOOTNOTE_LI_RE, FOOTNOTE_LI_RE2):
        for m in pattern.finditer(content):
            fn_id = m.group(1)
            raw_body = m.group(2)
            cleaned = clean_html_to_markdown(raw_body)
            if fn_id not in bodies and cleaned:
                bodies[fn_id] = cleaned
    return bodies


def find_all_footnote_bodies(extract_dir: Path) -> dict[str, str]:
    """Scan all XHTML files in extract_dir for footnote bodies.

    Returns {footnote_id: cleaned_text}. If the same id appears in
    multiple files, the first one wins.
    """
    all_bodies: dict[str, str] = {}
    xhtml_files = sorted(extract_dir.rglob("*.xhtml")) + sorted(extract_dir.rglob("*.html"))
    for xf in xhtml_files:
        bodies = extract_footnote_bodies_from_xhtml(xf)
        for fid, body in bodies.items():
            if fid not in all_bodies:
                all_bodies[fid] = body
    return all_bodies


# ─────────────────────────────────────────────────────────────────────
# Anchor patterns in pandoc-emitted Markdown
# ─────────────────────────────────────────────────────────────────────


# Pandoc emits EPUB footnote anchors in several formats depending on
# the source EPUB and pandoc version. Patterns below cover the cases
# observed in real books (Sanderson's Alcatraz series, etc.).
#
# Format 1 (chapters — double bracket, escaped asterisk):
#   [[*](part0039.xhtml#chapter1-1){#cha-1 .noteref epub:type="noteref"}]{.epub-sup-fn}
#
# Format 2 (preface/intro — single bracket, no escape):
#   [[*](part0039.xhtml#chapter0-1){#pre-1 .noteref epub:type="noteref"}]{.epub-sup-fn}
#
# Format 3 (simple pandoc footnote — already [^N] format, leave alone):
#   [^1]
#
# Format 4 (raw pandoc link with .noteref class, no double bracket):
#   [*](part0039.xhtml#ID){.noteref ...}

ANCHOR_PATTERNS = [
    # Format 1 & 2: [[*](file.xhtml#ID){#localid ...}]{.epub-sup-fn}
    # The leading [ may be doubled ([[) or single ([); the * may be
    # escaped (\*) or not. The href MUST point to a .xhtml#ID (we use
    # that to distinguish from real Markdown links).
    re.compile(
        r"\[\[?\\?\*\]\(([^)]+\.xhtml#([\w\-]+))\)"  # group(1)=full href, group(2)=ID
        r"\{#[\w_]+[^}]*\}\]"  # {#localid ...}]
        r"\{\.epub-sup-fn\}"  # {.epub-sup-fn}
    ),
    # Format 4: [*](file.xhtml#ID){.noteref ...} (without the outer [...]{.epub-sup-fn})
    re.compile(
        r"\[\\?\*\]\(([^)]+\.xhtml#([\w\-]+))\)"
        r"\{[^}]*\.noteref[^}]*\}"
    ),
]


def find_anchors_in_text(text: str) -> list[tuple[re.Match, str, str]]:
    """Find all EPUB footnote anchors in text.

    Returns list of (match_object, full_href, footnote_id) in order
    of appearance. Deduplicates overlapping matches (Pattern 0 is
    a superset of Pattern 1, so when both match the same anchor we
    keep Pattern 0 — it captures the full {.epub-sup-fn} wrapper).
    """
    found = []
    for pattern in ANCHOR_PATTERNS:
        for m in pattern.finditer(text):
            full_href = m.group(1)
            fn_id = m.group(2)
            found.append((m, full_href, fn_id))
    # Sort by position
    found.sort(key=lambda x: (x[0].start(), -(x[0].end() - x[0].start())))
    # Deduplicate by overlap: if a match overlaps with an already-kept
    # match, skip it. We keep the FIRST (longest at that position).
    deduped = []
    kept_ranges: list[tuple[int, int]] = []
    for m, href, fid in found:
        m_start, m_end = m.start(), m.end()
        # Check overlap with any kept range
        overlaps = any(not (m_end <= ks or m_start >= ke) for ks, ke in kept_ranges)
        if not overlaps:
            deduped.append((m, href, fid))
            kept_ranges.append((m_start, m_end))
    return deduped


# ─────────────────────────────────────────────────────────────────────
# Main extraction logic
# ─────────────────────────────────────────────────────────────────────


def find_epub_extract_dir(temp_dir: Path) -> Path | None:
    """Find the directory where EPUB was extracted.

    Looks in:
      1. <temp_dir>/process/_epub_extracted/  (convert.py and convert_per_file.py)
    """
    pdir = process_dir(temp_dir)
    candidate = pdir / "_epub_extracted"
    if candidate.exists() and (any(candidate.rglob("*.xhtml")) or any(candidate.rglob("*.html"))):
        return candidate
    return None


def re_extract_epub(temp_dir: Path, epub_path: Path) -> Path:
    """Re-extract XHTML files from EPUB into _epub_extracted/."""
    pdir = process_dir(temp_dir)
    extract_dir = pdir / "_epub_extracted"
    if extract_dir.exists():
        shutil.rmtree(extract_dir)
    extract_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(epub_path) as zf:
        zf.extractall(extract_dir)
    return extract_dir


def process_chunks(
    temp_dir: Path,
    footnote_bodies: dict[str, str],
) -> tuple[int, int, dict]:
    """Replace EPUB anchors in chunkNNNN.md with [^N] and append definitions.

    Numbering is GLOBAL across all chunks (1..N), assigned in order of
    first appearance (text order). If the same footnote_id is referenced
    from multiple chunks (or multiple times in one chunk), it reuses
    the same number — definition is only written to the FIRST chunk
    where it appears.

    Returns (anchors_replaced, footnotes_defined, mapping_dict).
    mapping_dict: {chunk_id: [(fn_id, assigned_number, body_preview)]}
    """
    pdir = process_dir(temp_dir)
    chunk_files = sorted(pdir.glob("chunk*.md"))
    chunk_files = [f for f in chunk_files if not f.name.startswith("output_")]

    global_counter = 0
    mapping: dict = {}
    total_replaced = 0
    total_defined = 0
    fn_id_to_number: dict[str, int] = {}

    for chunk_file in chunk_files:
        text = chunk_file.read_text(encoding="utf-8")
        anchors = find_anchors_in_text(text)
        if not anchors:
            continue

        chunk_id = chunk_file.stem
        mapping[chunk_id] = []
        definitions_to_append: list[str] = []

        # Process anchors in FORWARD order (text order) so numbering
        # follows reading order. To avoid position shifts, we replace
        # each anchor with a unique placeholder first (in REVERSE order
        # so earlier positions don't shift), then replace placeholders
        # with [^N] in a second forward pass.
        placeholders: list[tuple[str, str, str]] = []  # (placeholder, full_href, fn_id)
        for idx, (m, full_href, fn_id) in enumerate(anchors):
            placeholder = f"\x00FN_ANCHOR_{idx}\x00"
            placeholders.append((placeholder, full_href, fn_id))
        # Insert placeholders in REVERSE order so earlier m.start()/m.end()
        # positions remain valid as we modify the text.
        for idx in range(len(anchors) - 1, -1, -1):
            m, full_href, fn_id = anchors[idx]
            placeholder = placeholders[idx][0]
            text = text[: m.start()] + placeholder + text[m.end() :]

        # Now assign numbers in forward order and replace placeholders
        for placeholder, full_href, fn_id in placeholders:
            if fn_id not in footnote_bodies:
                sys.stderr.write(
                    f"[extract_footnotes] WARNING: chunk {chunk_id} references "
                    f"footnote '{fn_id}' but no body found in any XHTML file. "
                    f"Removing anchor.\n"
                )
                # Remove the placeholder entirely
                text = text.replace(placeholder, "")
                continue

            if fn_id in fn_id_to_number:
                number = fn_id_to_number[fn_id]
                # Don't re-append definition — already in another chunk
            else:
                global_counter += 1
                number = global_counter
                fn_id_to_number[fn_id] = number
                body = footnote_bodies[fn_id]
                definitions_to_append.append(f"[^{number}]: {body}")
                total_defined += 1

            text = text.replace(placeholder, f"[^{number}]")
            body_preview = footnote_bodies[fn_id]
            mapping[chunk_id].append(
                {
                    "footnote_id": fn_id,
                    "number": number,
                    "body_preview": body_preview[:80] + "..." if len(body_preview) > 80 else body_preview,
                }
            )
            total_replaced += 1

        # Append definitions at end of chunk
        if definitions_to_append:
            if not text.endswith("\n\n"):
                if text.endswith("\n"):
                    text += "\n"
                else:
                    text += "\n\n"
            text += "\n".join(definitions_to_append) + "\n"

        from config import atomic_write_text

        atomic_write_text(chunk_file, text, encoding="utf-8")

    return total_replaced, total_defined, mapping


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    temp_dir = Path(sys.argv[1])
    if not temp_dir.exists():
        print(f"ERROR: temp dir not found: {temp_dir}", file=sys.stderr)
        sys.exit(2)

    epub_path = None
    if "--epub" in sys.argv:
        idx = sys.argv.index("--epub")
        epub_path = Path(sys.argv[idx + 1])

    # Step 1: find or create extract dir
    extract_dir = find_epub_extract_dir(temp_dir)
    if extract_dir is None:
        if epub_path and epub_path.exists():
            sys.stderr.write(f"[extract_footnotes] No _epub_extracted/ found; re-extracting from {epub_path}\n")
            extract_dir = re_extract_epub(temp_dir, epub_path)
        else:
            sys.stderr.write(
                "[extract_footnotes] No extracted XHTML files found. Pass --epub <path> to extract from source EPUB.\n"
            )
            sys.exit(0)  # Not an error — maybe this book has no EPUB footnotes

    # Step 2: extract all footnote bodies from XHTML files
    sys.stderr.write(f"[extract_footnotes] Scanning XHTML files in {extract_dir}...\n")
    bodies = find_all_footnote_bodies(extract_dir)
    sys.stderr.write(f"[extract_footnotes] Found {len(bodies)} footnote bodies\n")
    if not bodies:
        sys.stderr.write(
            "[extract_footnotes] No footnote bodies found. This book may not use EPUB-style footnotes. Exiting.\n"
        )
        sys.exit(0)

    # Step 3: process chunks — replace anchors with [^N], append definitions
    replaced, defined, mapping = process_chunks(temp_dir, bodies)
    sys.stderr.write(
        f"[extract_footnotes] Replaced {replaced} anchors, defined {defined} footnotes across {len(mapping)} chunks\n"
    )

    # Step 4: write mapping JSON for audit
    pdir = process_dir(temp_dir)
    from config import atomic_write_json

    atomic_write_json(
        pdir / "footnotes_extracted.json",
        {
            "total_bodies_found": len(bodies),
            "total_anchors_replaced": replaced,
            "total_footnotes_defined": defined,
            "chunks_with_footnotes": list(mapping.keys()),
            "mapping": mapping,
        },
        indent=2,
        ensure_ascii=False,
    )

    print(f"OK: {replaced} anchors → [^N], {defined} footnotes defined")
    print(f"  Mapping: {pdir / 'footnotes_extracted.json'}")


if __name__ == "__main__":
    main()
