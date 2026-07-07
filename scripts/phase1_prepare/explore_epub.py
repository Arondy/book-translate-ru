#!/usr/bin/env python3
"""Explore EPUB structure — print contents, OPF metadata, chapter list.

This script exists so the orchestrator agent does NOT have to write ad-hoc
`python3 -c "..."` snippets to inspect an EPUB. Those snippets often fail
silently on Windows (encoding issues, wrong paths, missing imports) and
waste context tokens.

Usage:
    python3 explore_epub.py <path-to-epub>

Output (to stdout, UTF-8):
    - Total file count in the archive
    - OPF metadata (title, author, language, identifier) if found
    - NCX/Nav list (chapter titles + their file paths)
    - List of content files (HTML/XHTML) with sizes
    - Estimated total text length (chars)
"""

import re
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

# Ensure UTF-8 output on Windows (cp1251 default breaks on non-ASCII)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")


# EPUB OPF namespace
NS_OPF = "http://www.idpf.org/2007/opf"
NS_DC = "http://purl.org/dc/elements/1.1/"
NS_NCX = "http://www.daisy.org/z3986/2005/ncx/"
NS_XHTML = "http://www.w3.org/1999/xhtml"


def strip_ns(tag: str) -> str:
    """Remove XML namespace from tag."""
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def parse_opf(opf_content: str) -> dict:
    """Parse OPF metadata + manifest + spine."""
    try:
        root = ET.fromstring(opf_content)
    except ET.ParseError as e:
        return {"error": f"OPF parse error: {e}"}

    metadata = {}
    manifest = {}  # id -> (href, media-type)
    spine = []  # list of idrefs in reading order

    for elem in root.iter():
        tag = strip_ns(elem.tag)
        if tag == "metadata":
            for m in elem:
                mtag = strip_ns(m.tag)
                if mtag in (
                    "title",
                    "creator",
                    "language",
                    "identifier",
                    "publisher",
                    "date",
                    "description",
                ):
                    key = mtag
                    if mtag == "creator":
                        # could be multiple authors
                        existing = metadata.get(key, "")
                        text = (m.text or "").strip()
                        if existing:
                            metadata[key] = f"{existing}; {text}"
                        else:
                            metadata[key] = text
                    else:
                        metadata[key] = (m.text or "").strip()
        elif tag == "item":
            item_id = elem.get("id", "")
            href = elem.get("href", "")
            media_type = elem.get("media-type", "")
            manifest[item_id] = (href, media_type)
        elif tag == "itemref":
            idref = elem.get("idref", "")
            if idref:
                spine.append(idref)

    return {
        "metadata": metadata,
        "manifest": manifest,
        "spine": spine,
    }


def parse_ncx(nav_points: list, level: int = 0) -> list[tuple[int, str, str]]:
    """Recursively extract (level, title, src) from NCX navPoints."""
    result = []
    for np in nav_points:
        nl = strip_ns(np.tag)
        if nl != "navPoint":
            continue
        title_elem = None
        content_elem = None
        children = []
        for child in np:
            ctag = strip_ns(child.tag)
            if ctag == "navLabel":
                for t in child:
                    if strip_ns(t.tag) == "text":
                        title_elem = (t.text or "").strip()
            elif ctag == "content":
                content_elem = child.get("src", "")
            elif ctag == "navPoint":
                children.append(child)
        if title_elem and content_elem:
            result.append((level, title_elem, content_elem))
        # Recurse into children
        if children:
            result.extend(parse_ncx(children, level + 1))
    return result


def find_chapter_titles_in_html(html_content: str, max_titles: int = 50) -> list[str]:
    """Extract H1/H2 headings from HTML content (for chapter detection)."""
    titles = []
    # Match <h1>...</h1> or <h2>...</h2>, case-insensitive
    pattern = re.compile(r"<h[12][^>]*>(.*?)</h[12]>", re.IGNORECASE | re.DOTALL)
    for m in pattern.finditer(html_content):
        # Strip nested tags
        title = re.sub(r"<[^>]+>", "", m.group(1)).strip()
        if title and len(title) < 200:
            titles.append(title)
        if len(titles) >= max_titles:
            break
    return titles


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    epub_path = Path(sys.argv[1])
    if not epub_path.exists():
        print(f"ERROR: File not found: {epub_path}", file=sys.stderr)
        sys.exit(1)

    if not epub_path.suffix.lower() == ".epub":
        print(f"ERROR: Not an EPUB file: {epub_path}", file=sys.stderr)
        sys.exit(1)

    try:
        with zipfile.ZipFile(epub_path, "r") as z:
            names = z.namelist()
    except zipfile.BadZipFile as e:
        print(f"ERROR: Bad zip / EPUB: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"=== EPUB: {epub_path.name} ===")
    print(f"Total entries: {len(names)}")
    print()

    # Re-open for reading content (the previous `with` block closed it)
    z = zipfile.ZipFile(epub_path, "r")

    # ── Find and parse OPF ────────────────────────────────────────────
    opf_path = None
    for n in names:
        if n.endswith(".opf"):
            opf_path = n
            break
    # If no .opf, try META-INF/container.xml
    if not opf_path:
        try:
            container = z.read("META-INF/container.xml").decode("utf-8")
            m = re.search(r'full-path="([^"]+)"', container)
            if m:
                opf_path = m.group(1)
        except KeyError:
            pass

    opf_data = None
    if opf_path:
        try:
            opf_content = z.read(opf_path).decode("utf-8")
            opf_data = parse_opf(opf_content)
        except (KeyError, UnicodeDecodeError) as e:
            print(f"WARNING: Could not read OPF {opf_path}: {e}")

    # ── Print metadata ────────────────────────────────────────────────
    print("--- Metadata ---")
    if opf_data and "metadata" in opf_data:
        for k, v in opf_data["metadata"].items():
            print(f"  {k}: {v}")
    else:
        print("  (no OPF metadata found)")
    print()

    # ── Print spine (reading order) ───────────────────────────────────
    if opf_data and "spine" in opf_data and opf_data["spine"]:
        manifest = opf_data.get("manifest", {})
        print("--- Spine (reading order) ---")
        for i, idref in enumerate(opf_data["spine"][:30]):
            href, media_type = manifest.get(idref, ("?", "?"))
            print(f"  {i + 1:3d}. {idref} -> {href} ({media_type})")
        if len(opf_data["spine"]) > 30:
            print(f"  ... and {len(opf_data['spine']) - 30} more")
        print()

    # ── Find and parse NCX (chapter list) ─────────────────────────────
    ncx_path = None
    for n in names:
        if n.endswith(".ncx"):
            ncx_path = n
            break

    if ncx_path:
        try:
            ncx_content = z.read(ncx_path).decode("utf-8")
            root = ET.fromstring(ncx_content)
            # Find all navMap > navPoint
            nav_points = []
            for elem in root.iter():
                if strip_ns(elem.tag) == "navPoint":
                    nav_points.append(elem)
            chapters = parse_ncx(nav_points)
            if chapters:
                print(f"--- Chapter list (NCX: {ncx_path}) ---")
                for level, title, src in chapters[:50]:
                    indent = "  " * level
                    print(f"  {indent}- {title}  [{src}]")
                if len(chapters) > 50:
                    print(f"  ... and {len(chapters) - 50} more")
                print()
        except (KeyError, UnicodeDecodeError, ET.ParseError) as e:
            print(f"WARNING: Could not parse NCX {ncx_path}: {e}")

    # ── List content files with sizes ─────────────────────────────────
    content_exts = (".html", ".xhtml", ".htm")
    content_files = []
    total_text_size = 0
    for n in names:
        if n.lower().endswith(content_exts):
            try:
                info = z.getinfo(n)
                content_files.append((n, info.file_size))
                total_text_size += info.file_size
            except KeyError:
                pass

    print(f"--- Content files ({len(content_files)} HTML/XHTML) ---")
    # Sort by path
    content_files.sort()
    for n, size in content_files[:40]:
        print(f"  {n}: {size:,} bytes")
    if len(content_files) > 40:
        print(f"  ... and {len(content_files) - 40} more")
    print(f"  Total content size: {total_text_size:,} bytes")
    print()

    # ── Detect chapter titles by scanning first few HTML files ────────
    print("--- Detected chapter headings (H1/H2 in content files) ---")
    detected_titles: list[tuple[str, str]] = []  # (filename, title)
    for n, _ in content_files[:20]:  # first 20 files
        try:
            html = z.read(n).decode("utf-8", errors="replace")
            titles = find_chapter_titles_in_html(html, max_titles=5)
            for t in titles:
                detected_titles.append((n, t))
        except (KeyError, UnicodeDecodeError):
            pass

    if detected_titles:
        for fn, title in detected_titles[:50]:
            print(f"  {fn}: {title}")
        if len(detected_titles) > 50:
            print(f"  ... and {len(detected_titles) - 50} more")
    else:
        print("  (no H1/H2 headings detected — book may use other structure)")
    print()

    # ── Summary ───────────────────────────────────────────────────────
    print("=== Summary ===")
    print(f"  Content files: {len(content_files)}")
    print(f"  Total HTML bytes: {total_text_size:,}")
    estimated_chars = total_text_size // 2  # rough: HTML overhead ~50%
    estimated_chunks = max(1, estimated_chars // 30000)
    print(f"  Estimated text chars: ~{estimated_chars:,}")
    print(f"  Estimated chunks (at 30000 chars/chunk): ~{estimated_chunks}")
    if opf_data and "metadata" in opf_data:
        title = opf_data["metadata"].get("title", "?")
        author = opf_data["metadata"].get("creator", "?")
        print(f"  Title: {title}")
        print(f"  Author: {author}")


if __name__ == "__main__":
    main()
