"""Alternative EPUB → Markdown-чанки converter (per-file pandoc + cleanup).

This is an ALTERNATIVE to `convert.py` for EPUBs where the standard
zip-merge → pandoc pipeline produces garbage Markdown (split headings,
leftover span markup, pagebreak markers, dropped caps, raw HTML images).

PROBLEM IT SOLVES (real case — Bastille Vs. the Evil Librarians):
  convert.py does: unzip EPUB → merge spine XHTML → pandoc HTML→Markdown.
  For some EPUBs (especially those produced by older Calibre exports)
  this produces:
    - Headings split across two lines wrapped in link markup:
        # []{#pg_7 ...}[[Chapter]{.tfhabitatexpanded_bold_b_}](contents.xhtml#c_ch1){.calibre5} {#ch1 .cn}
        # [[1]{.tfhabitatexpanded_bold_b_}](#c_ch1){.calibre5}
    - Pagebreak spans: [...]{#pg_8 .calibre6 .pagebreak aria-label=" Page 8. "}
    - Dropped caps: [S]{.minio}
    - Link spans: [[Word]{.tfhabitatexpanded_bold_b_}](#calibre_link-X)
    - Images as raw HTML: <figure><img src="../images/..."></figure>
      (fb2_builder.py expects Markdown ![alt](path))

WHAT THIS SCRIPT DOES:
  1. Extract EPUB as a zip.
  2. Read OPF spine → process XHTML files in reading order.
  3. For each XHTML file: run `pandoc html→markdown --wrap=none` SEPARATELY
     (not on a merged HTML) — preserves chapter structure.
  4. Clean each Markdown output:
     - Headings FIRST (while `tfhabitatexpanded_bold_b_` markers still
       present): collapse split headings into a single `# Chapter N`,
       using the file basename as the title.
     - Remove pagebreak spans, dropped caps, leftover link spans,
       pandoc attribute blocks `{...}`, div fences `:::`.
     - Convert `<figure><img>` and `<img>` → `![image](images/<name>)`.
     - Remove blank line right after `#` heading (so it doesn't split
       into a tiny orphan chunk).
  5. Copy all images from EPUB to `process/images/`.
  6. Reuse `convert.py`'s `split_into_chunks()` for chapter-aware chunking.
  7. Write manifest.json, chunk_sections.json, source_fingerprint.json,
     config.txt.
  8. Run post-steps: detect_epigraphs, narrator_marker, extract_footnotes.

WHEN TO USE THIS INSTEAD OF convert.py:
  - convert.py produces many tiny chunks (< 200 chars) that are just
    heading fragments.
  - chunk_sections.json contains link markup instead of real
    chapter names.
  - Images appear as raw HTML in input.md instead of Markdown.
  - Many pandoc attribute spans in the text.

USAGE:
    python3 convert_per_file.py <path_to_epub> [--temp-root <dir>] [--force]

This produces the same output structure as convert.py:
  <stem>_temp/
  ├── process/
  │   ├── input.md
  │   ├── chunk0001.md … chunkNNNN.md
  │   ├── manifest.json
  │   ├── chunk_sections.json
  │   ├── source_fingerprint.json
  │   ├── config.txt
  │   ├── structural_units.json       (from detect_epigraphs)
  │   ├── narrator_hints.json         (from narrator_marker)
  │   ├── footnotes_extracted.json    (from extract_footnotes, if any)
  │   └── images/                     (copied from EPUB)
  └── (no human-facing files yet)

After this script runs, proceed to step 3 (glossary) and beyond as usual.
"""

import json
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "shared"))
sys.path.insert(0, str(_HERE))

from common import process_dir

# ─────────────────────────────────────────────────────────────────────
# Default exclude set (publisher front/back matter)
# ─────────────────────────────────────────────────────────────────────

# Stems of XHTML files to SKIP entirely (publisher boilerplate).
# Customize via --exclude-file if your book has different names.
DEFAULT_EXCLUDE = {
    "cover",
    "title",
    "mini_toc",
    "copyrightnotice",
    "contents",
    "adcard",
    "newsletter",
    "copyright",
    "torad",
    # Common alternatives
    "toc",
    "tableofcontents",
    "imprint",
    "halftitle",
}


# ─────────────────────────────────────────────────────────────────────
# Title derivation from XHTML basename
# ─────────────────────────────────────────────────────────────────────


def derive_title_from_basename(basename: str) -> str:
    """Derive a human-readable chapter title from XHTML file basename.

    Override via --title-map <json_file> if your book has unusual names.
    """
    b = basename.lower()
    # chapter1, chapter12, chapter123 → "Chapter 1", "Chapter 12"
    m = re.match(r"^chapter(\d+)$", b)
    if m:
        return f"Chapter {int(m.group(1))}"
    # Common special files
    special = {
        "dedication": "Dedication",
        "authorforeword": "Author's Foreword",
        "foreword": "Foreword",
        "preface": "Preface",
        "introduction": "Introduction",
        "map": "Map",
        "prologue": "Prologue",
        "epilogue": "Epilogue",
        "afterword": "Afterword",
        "acknowledgments": "Acknowledgments",
        "acknowledgements": "Acknowledgements",
        "abouttheauthor": "About the Author",
        "abouttheauthors": "About the Authors",
        "abouttheillustrator": "About the Illustrator",
        "illustration": "About the Illustrator",
        "alsoby": "Also by",
        "copyright": "Copyright",
    }
    if b in special:
        return special[b]
    # Fallback: use basename as-is (Title Case)
    return basename.replace("_", " ").title()


# ─────────────────────────────────────────────────────────────────────
# Markdown cleanup
# ─────────────────────────────────────────────────────────────────────


def clean_md(text: str, clean_title: str | None) -> str:
    """Clean pandoc-emitted Markdown from leftover cruft.

    Operations performed in this specific order (order matters —
    heading pass must run FIRST, while `tfhabitatexpanded_bold_b_`
    markers are still present, otherwise both split heading lines
    survive and we get triple headings).
    """
    # --- 1. Heading pass FIRST (while tfhabitatexpanded markers still present) ---
    lines = text.splitlines()
    out: list[str] = []
    title_set = False
    for line in lines:
        if line.lstrip().startswith("#") and "tfhabitatexpanded_bold_b_" in line:
            if not title_set and clean_title:
                out.append("# " + clean_title)
                title_set = True
            # Skip the chapter-number / extra calibre heading line
            continue
        out.append(line)
    if clean_title and not title_set:
        out.insert(0, "# " + clean_title)
    text = "\n".join(out)

    # --- 2. Pagebreak attribute spans ---
    text = re.sub(r"\[\]\{#pg_[^}]*\}", "", text)
    text = re.sub(r"\{#pg_[^}]*\}", "", text)

    # --- 3. Dropped caps [X]{.minio} → X ---
    text = re.sub(r"\[([^\]])\]\{\.minio\}", r"\1", text)

    # --- 4. Link spans [text](#calibre_link..){.calibre2} → text ---
    # Also catch the variant WITHOUT the {.calibre2} attribute (pandoc may
    # not always emit it). Pattern: [text](#calibre_link-N) optionally
    # followed by {…} attribute block.
    text = re.sub(
        r"\[([^\]]+)\]\(#calibre_link[^)]*\)(?:\{[^}]*\})?",
        r"\1",
        text,
    )
    # Also catch the wrapped variant: [[text](#calibre_link-N)] (no {.calibre2})
    text = re.sub(
        r"\[\[([^\]]+)\]\(#calibre_link[^)]*\)(?:\{[^}]*\})?\]",
        r"\1",
        text,
    )

    # --- 5. Remaining pandoc attribute blocks {..} ---
    text = re.sub(r"\{\.calibre5\}", "", text)
    text = re.sub(r"\{\.[^}]*\}", "", text)
    text = re.sub(r"\{#[^}]*\}", "", text)

    # --- 6. Pandoc div fences ::: {..} ---
    text = re.sub(r"(?m)^\s*:{3,4}\s*(\{[^}]*\})?\s*$", "", text)

    # --- 7. Images: normalize paths to images/<name> ---
    # Several formats pandoc may emit:
    #   a) <figure><img src="../images/foo.png"></figure>  → Markdown ![alt](../images/foo.png)
    #   b) <img src="../images/foo.png">                   → Markdown ![alt](../images/foo.png)
    #   c) Already-Markdown ![alt](../images/foo.png) — normalize path
    #   d) ![alt](images/foo.png) — already correct, leave alone
    # All → ![image](images/<name>) (or keep alt if non-empty)

    def img_repl(m):
        src = m.group("src")
        alt = m.group("alt") or "image"
        name = Path(src).name
        return f"![{alt}](images/{name})"

    # (a) <figure><img src="..." alt="...">...</figure>
    text = re.sub(
        r"<figure[^>]*>\s*<img[^>]*src=\"(?P<src>[^\"]+)\"[^>]*?(?:alt=\"(?P<alt>[^\"]*)\")?[^>]*/?>\s*</figure>",
        img_repl,
        text,
        flags=re.IGNORECASE,
    )
    # (a-bis) <figure><img alt="..." src="...">...</figure> (alt before src)
    text = re.sub(
        r"<figure[^>]*>\s*<img[^>]*?alt=\"(?P<alt>[^\"]*)\"[^>]*src=\"(?P<src>[^\"]+)\"[^>]*/?>\s*</figure>",
        img_repl,
        text,
        flags=re.IGNORECASE,
    )
    # (b) <img src="..." alt="...">
    text = re.sub(
        r"<img[^>]*src=\"(?P<src>[^\"]+)\"[^>]*?(?:alt=\"(?P<alt>[^\"]*)\")?[^>]*/?>",
        img_repl,
        text,
        flags=re.IGNORECASE,
    )
    # (b-bis) <img alt="..." src="...">
    text = re.sub(
        r"<img[^>]*?alt=\"(?P<alt>[^\"]*)\"[^>]*src=\"(?P<src>[^\"]+)\"[^>]*/?>",
        img_repl,
        text,
        flags=re.IGNORECASE,
    )
    # (c) Already-Markdown ![alt](../images/foo.png) or ![alt](images/../foo.png) → normalize
    text = re.sub(
        r"!\[(?P<alt>[^\]]*)\]\((?:\.\./)*images/(?P<src>[^)]+)\)",
        lambda m: f"![{m.group('alt') or 'image'}](images/{m.group('src')})",
        text,
    )
    # (c-bis) ![alt](any/path/with/images/foo.png) → ![alt](images/foo.png)
    text = re.sub(
        r"!\[(?P<alt>[^\]]*)\]\([^)]*?/images/(?P<src>[^)/]+)\)",
        lambda m: f"![{m.group('alt') or 'image'}](images/{m.group('src')})",
        text,
    )

    # --- 8. Remove blank line immediately after a heading (so heading doesn't split into orphan chunk) ---
    lines2 = text.splitlines()
    merged: list[str] = []
    for i, line in enumerate(lines2):
        # If this line is a heading AND next line is blank, skip the blank
        if line.lstrip().startswith("#") and i + 1 < len(lines2) and lines2[i + 1].strip() == "":
            merged.append(line)
            continue
        merged.append(line)
    return "\n".join(merged)


# ─────────────────────────────────────────────────────────────────────
# EPUB reading
# ─────────────────────────────────────────────────────────────────────


def read_opf_spine(epub_path: Path, extract_dir: Path) -> tuple[list[str], dict[str, str], Path]:
    """Extract EPUB to extract_dir, read OPF spine and manifest.

    Returns (spine_ids, manifest_id_to_href, opf_dir).
    """
    with zipfile.ZipFile(epub_path) as zf:
        zf.extractall(extract_dir)

    from bs4 import BeautifulSoup

    opf_files = list(extract_dir.rglob("*.opf"))
    if not opf_files:
        raise RuntimeError(f"No OPF file found in extracted EPUB {epub_path}")
    opf_path = opf_files[0]
    opf = BeautifulSoup(opf_path.read_text(encoding="utf-8"), "lxml-xml")
    spine_ids = [it.get("idref") for it in opf.find_all("itemref")]
    manifest = {it.get("id"): it.get("href") for it in opf.find_all("item") if it.get("id") and it.get("href")}
    return spine_ids, manifest, opf_path.parent


def copy_images(extract_dir: Path, dest_dir: Path) -> int:
    """Copy all images from extract_dir/images/ subdirs to dest_dir/.

    Returns count of copied files.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    count = 0
    img_exts = {".gif", ".jpg", ".jpeg", ".png", ".svg", ".bmp"}
    for d in extract_dir.rglob("images"):
        if not d.is_dir():
            continue
        for f in d.iterdir():
            if f.is_file() and f.suffix.lower() in img_exts:
                shutil.copy2(f, dest_dir / f.name)
                count += 1
    return count


def pandoc_html_to_markdown(xhtml_path: Path) -> str:
    """Run pandoc on a single XHTML file → Markdown string."""
    result = subprocess.run(
        ["pandoc", str(xhtml_path), "-f", "html", "-t", "markdown", "--wrap=none"],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode != 0:
        sys.stderr.write(f"[convert_per_file] pandoc failed on {xhtml_path.name}: {result.stderr[:200]}\n")
        return ""
    return result.stdout


# ─────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    # Pre-flight: pandoc required
    if not shutil.which("pandoc"):
        print(
            "ERROR: pandoc not found in PATH. Install Pandoc.",
            file=sys.stderr,
        )
        sys.exit(1)

    input_path = Path(sys.argv[1]).resolve()
    if not input_path.exists():
        print(f"ERROR: File not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    if input_path.suffix.lower() != ".epub":
        print(
            f"ERROR: convert_per_file.py only supports EPUB. "
            f"Got: {input_path.suffix}. Use convert.py for other formats.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Parse flags
    temp_root = None
    force = False
    exclude_set = set(DEFAULT_EXCLUDE)
    title_map_path = None
    i = 2
    while i < len(sys.argv):
        arg = sys.argv[i]
        if arg == "--temp-root" and i + 1 < len(sys.argv):
            temp_root = Path(sys.argv[i + 1]).resolve()
            i += 2
        elif arg == "--force":
            force = True
            i += 1
        elif arg == "--exclude-file" and i + 1 < len(sys.argv):
            # JSON file: ["cover", "title", ...] — additional excludes
            excl = json.loads(Path(sys.argv[i + 1]).read_text(encoding="utf-8"))
            exclude_set.update(excl)
            i += 2
        elif arg == "--title-map" and i + 1 < len(sys.argv):
            title_map_path = Path(sys.argv[i + 1])
            i += 2
        else:
            print(f"WARN: unknown arg: {arg}", file=sys.stderr)
            i += 1

    # Load custom title map if provided
    custom_titles: dict[str, str] = {}
    if title_map_path and title_map_path.exists():
        custom_titles = json.loads(title_map_path.read_text(encoding="utf-8"))

    stem = input_path.stem
    if temp_root:
        temp_dir = temp_root / f"{stem}_temp"
    else:
        temp_dir = input_path.parent / f"{stem}_temp"

    temp_dir.mkdir(parents=True, exist_ok=True)
    pdir = process_dir(temp_dir)

    # Safety gate: refuse if outputs already exist (unless --force)
    existing_outputs = list(pdir.glob("output_chunk*.md"))
    if existing_outputs and not force:
        sys.stderr.write(
            f"[convert_per_file] ERROR: temp dir already contains "
            f"{len(existing_outputs)} output_chunk*.md files.\n"
            f"           Re-running will not delete outputs but may orphan them.\n"
            f"           Pass --force to proceed anyway.\n"
        )
        sys.exit(2)

    # ── Step 1: extract EPUB, read OPF ──────────────────────────────
    extract_dir = pdir / "_epub_extracted"
    if extract_dir.exists():
        shutil.rmtree(extract_dir)
    extract_dir.mkdir(parents=True)

    sys.stderr.write(f"[convert_per_file] Extracting EPUB → {extract_dir}\n")
    spine_ids, manifest, opf_dir = read_opf_spine(input_path, extract_dir)
    sys.stderr.write(f"[convert_per_file] Spine: {len(spine_ids)} items\n")

    # ── Step 2: copy images ─────────────────────────────────────────
    images_dir = pdir / "images"
    n_images = copy_images(extract_dir, images_dir)
    sys.stderr.write(f"[convert_per_file] Copied {n_images} images → {images_dir}\n")

    # ── Step 3: per-file pandoc + clean ─────────────────────────────
    parts: list[str] = []
    skipped: list[str] = []
    for sid in spine_ids:
        href = manifest.get(sid)
        if not href:
            continue
        xhtml_path = (opf_dir / href).resolve()
        if xhtml_path.suffix.lower() not in (".html", ".xhtml", ".htm"):
            continue
        if not xhtml_path.exists():
            continue

        basename = xhtml_path.stem
        if basename.lower() in exclude_set:
            skipped.append(basename)
            continue

        # Title: custom map → derived from basename
        title = custom_titles.get(basename) or derive_title_from_basename(basename)

        md = pandoc_html_to_markdown(xhtml_path)
        if not md.strip():
            continue

        cleaned = clean_md(md, title)
        if cleaned.strip():
            parts.append(cleaned)

    if skipped:
        sys.stderr.write(f"[convert_per_file] Skipped {len(skipped)} publisher files: {', '.join(skipped[:10])}\n")

    if not parts:
        sys.stderr.write("[convert_per_file] ERROR: no content extracted. Check EPUB structure.\n")
        sys.exit(1)

    # ── Step 4: write input.md ──────────────────────────────────────
    md_text = "\n\n".join(parts)
    input_md_path = pdir / "input.md"
    input_md_path.write_text(md_text, encoding="utf-8")
    sys.stderr.write(f"[convert_per_file] Wrote input.md: {len(md_text)} chars, {len(parts)} sections\n")

    # ── Step 5: chunk (reuse convert.py logic) ──────────────────────
    sys.path.insert(0, str(_HERE))  # ensure convert.py is importable
    import convert as C
    from config import atomic_write_json

    manifest_dict: dict = {
        "version": 2,
        "chunks": {},
        "source_file": input_path.name,
        "chapter_split": True,
        "converter": "convert_per_file.py",
    }

    chunk_paths, section_map = C.split_into_chunks(
        input_md_path,
        temp_dir,
        manifest_dict,
        respect_headings=True,
        chapter_only=False,
    )

    atomic_write_json(pdir / "chunk_sections.json", section_map, indent=2, ensure_ascii=False)
    atomic_write_json(pdir / "manifest.json", manifest_dict, indent=2, ensure_ascii=False)

    # ── Step 6: metadata, fingerprint, config ───────────────────────
    metadata = C.extract_metadata_from_opf(input_path)
    C.write_config(temp_dir, metadata, "epub")
    C.write_fingerprint(temp_dir, input_path, C.sha256_file(input_path))

    # ── Step 7: post-steps (detect_epigraphs, narrator_marker, extract_footnotes) ──
    for s in ("detect_epigraphs.py", "narrator_marker.py", "extract_footnotes.py"):
        try:
            C.run_post_step(_HERE / s, temp_dir)
        except Exception as e:
            sys.stderr.write(f"[convert_per_file] post-step {s} error: {e}\n")

    # ── Stats ───────────────────────────────────────────────────────
    sizes = [info["size"] for info in manifest_dict["chunks"].values()]
    tiny = [k for k, v in manifest_dict["chunks"].items() if v["size"] < 200]

    print(f"Source:        {input_path.name}")
    print("Converter:     convert_per_file.py (per-file pandoc + cleanup)")
    print(f"Chunks:        {len(chunk_paths)}")
    if sizes:
        print(f"Size stats:   min {min(sizes)}, max {max(sizes)}, avg {sum(sizes) // len(sizes)} chars")
    if tiny:
        sys.stderr.write(f"[convert_per_file] WARNING: {len(tiny)} chunks shorter than 200 chars: {tiny[:8]}\n")
    print(f"Images:        {n_images} copied to process/images/")
    print(f"Temp:          {temp_dir}")
    print(f"Title:         {metadata.get('original_title', '?')}")
    print(f"Author:        {metadata.get('author', '?')}")
    print()
    print("Next: step 3 (glossary) — proceed as in references/phase-1-prepare.md.")


if __name__ == "__main__":
    main()
