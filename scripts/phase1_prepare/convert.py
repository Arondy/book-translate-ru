"""Convert EPUB/FB2 -> Markdown chunks for book-translate-ru skill.

Pipeline:
  EPUB -> (zip extract + OPF spine merge) -> HTML -> Pandoc -> Markdown
  FB2   -> Pandoc -> Markdown
  Markdown -> split by H1/H2 (chapter-aware) -> chunk by paragraphs

Usage:
    python3 convert.py <input_path>
    python3 convert.py <input_path> --temp-root <dir>
    python3 convert.py <input_path> --no-chapter-split
    python3 convert.py <input_path> --force

Requires: Pandoc, beautifulsoup4, lxml
"""

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "shared"))
from common import process_dir, run_cmd, sha256_file

# Load config from config.toml (searched in cwd, *_temp/, or skill dir)
from config import get_config

# Chunk size targets. Loaded from config.toml [chunking] section.
# 30000 chars ≈ 7500 tokens source + ~10000 tokens target = ~17500 tokens
# per chunk, leaving plenty of headroom for prompt, glossary, neighbour
# context, and meta.json schema.
_cfg = get_config()
CHUNK_SIZE = _cfg.get("chunking", "chunk_size", 30000)
SOFT_LIMIT = _cfg.get("chunking", "soft_limit", 40000)
MIN_CHUNK = _cfg.get("chunking", "min_chunk", 200)
CHAPTER_ONLY = _cfg.get("chunking", "chapter_only", True)


def run_post_step(script_path: Path, temp_dir: Path) -> None:
    """Run a sibling post-processing script as a subprocess.

    Post-steps (detect_epigraphs, narrator_marker) are deterministic and
    pure — they read from temp_dir, write a sidecar file. They must NOT
    make semantic decisions; that's the LLM agent's job.
    """
    sys.stderr.write(f"[convert] post-step: {script_path.name}\n")
    result = subprocess.run(
        [sys.executable, str(script_path), str(temp_dir)],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.stdout:
        for line in result.stdout.splitlines():
            sys.stderr.write(f"  {line}\n")
    if result.returncode != 0:
        sys.stderr.write(f"[convert] WARNING: {script_path.name} exited {result.returncode}\n{result.stderr}\n")


def epub_to_markdown(epub_path: Path, work_dir: Path) -> Path:
    """Convert EPUB -> Markdown without Calibre.

    Steps:
      1. Extract the EPUB (it's a zip) into `work_dir/_epub_extracted/`.
         The directory is kept on disk so `extract_footnotes.py` can
         scan XHTML files for footnote bodies in a later post-step.
      2. Locate the OPF file and read its spine — the ordered list of
         XHTML files that form the reading order.
      3. Concatenate the `<body>` of each spine XHTML into one merged
         HTML document, in reading order.
      4. Run `pandoc html -> markdown` on the merged HTML and clean the
         output with `_clean_markdown_output`.

    Returns the path to the produced `input.md`.

    Falls back to "largest XHTML in the archive" when no OPF spine is
    found (rare for valid EPUBs, but defensive).
    """
    import zipfile

    from bs4 import BeautifulSoup

    extract_dir = work_dir / "_epub_extracted"
    if extract_dir.exists():
        shutil.rmtree(extract_dir)
    extract_dir.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(epub_path) as zf:
        zf.extractall(extract_dir)

    opf_files = sorted(extract_dir.rglob("*.opf"))
    if not opf_files:
        raise RuntimeError(f"No OPF file found in extracted EPUB {epub_path}")
    opf_path = opf_files[0]
    opf = BeautifulSoup(opf_path.read_text(encoding="utf-8"), "lxml-xml")
    spine_ids = [it.get("idref") for it in opf.find_all("itemref")]
    manifest = {it.get("id"): it.get("href") for it in opf.find_all("item") if it.get("id") and it.get("href")}
    opf_dir = opf_path.parent

    html_parts: list[str] = []
    for sid in spine_ids:
        href = manifest.get(sid)
        if not href:
            continue
        xhtml_path = (opf_dir / href).resolve()
        if xhtml_path.suffix.lower() not in (".html", ".xhtml", ".htm"):
            continue
        if not xhtml_path.exists():
            continue
        try:
            content = xhtml_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        soup = BeautifulSoup(content, "lxml")
        body = soup.find("body")
        html_parts.append(str(body) if body else content)

    if not html_parts:
        # Defensive fallback: largest XHTML in the archive.
        all_html = sorted(
            extract_dir.rglob("*.xhtml"),
            key=lambda f: f.stat().st_size,
            reverse=True,
        ) or sorted(
            extract_dir.rglob("*.html"),
            key=lambda f: f.stat().st_size,
            reverse=True,
        )
        if not all_html:
            raise RuntimeError(f"No XHTML content found in EPUB: {epub_path}")
        content = all_html[0].read_text(encoding="utf-8", errors="replace")
        soup = BeautifulSoup(content, "lxml")
        body = soup.find("body")
        html_parts.append(str(body) if body else content)

    merged_html = (
        "<!DOCTYPE html>\n<html><head><meta charset='utf-8'></head><body>\n"
        + "\n".join(html_parts)
        + "\n</body></html>"
    )
    merged_html_path = work_dir / "_merged.html"
    merged_html_path.write_text(merged_html, encoding="utf-8")

    md_path = work_dir / "input.md"
    html_to_markdown(merged_html_path, md_path)
    return md_path


def html_to_markdown(html_path: Path, output_path: Path):
    run_cmd(
        [
            "pandoc",
            str(html_path),
            "-f",
            "html",
            "-t",
            "markdown",
            "--wrap=none",
            "-o",
            str(output_path),
        ],
        f"pandoc {html_path.name} -> {output_path.name}",
        log_desc=True,
    )
    # Clean pandoc cruft from the output (div fences, attribute blocks,
    # TOC sections, and a few defensive patterns inherited from older
    # Calibre-based pipelines).
    _clean_markdown_output(output_path)


def _clean_markdown_output(md_path: Path):
    """Clean pandoc cruft from Markdown produced by pandoc.

    Removes:
    - Pandoc div fences (::: {…})
    - Pagebreak spans: [...]{#pg_N .pagebreak …}
    - Link spans: [[text](#calibre_link-N){.calibre2}]
    - Dropped caps: [X]{.minio}
    - Pandoc attribute blocks: {#id .class}
    - TOC sections (heading "Contents"/"Оглавление" + link entries)
    - Title-page cruft before first real chapter heading

    Most of the Calibre-specific patterns are no-ops on pandoc-only
    output but are kept defensively — they cost nothing and tolerate
    EPUBs that were re-exported through Calibre at some point.
    """
    import re as _re

    text = md_path.read_text(encoding="utf-8")
    lines = text.split("\n")
    cleaned: list[str] = []

    # State: skip TOC section
    in_toc = False
    toc_level = 0

    for line in lines:
        stripped = line.strip()

        # Skip pandoc div fences
        if _re.match(r"^\s*:{3,4}\s*(\{[^}]*\})?\s*$", stripped):
            continue

        # Skip calibre pagebreak spans
        if _re.search(r"\{#pg_\d+\s", stripped):
            # Remove just the span, keep surrounding text
            line = _re.sub(r"\[\]\{#pg_\d+[^}]*\}", "", line)
            line = _re.sub(r"\{#pg_\d+[^}]*\}", "", line)
            if not line.strip():
                continue

        # Skip dropped caps
        line = _re.sub(r"\[([^\]])\]\{\.minio\}", r"\1", line)

        # Clean calibre link spans: [[text](#calibre_link-N){.calibre2}] → text
        line = _re.sub(
            r"\[([^\]]+)\]\(#calibre_link[^)]*\)(?:\{[^}]*\})?",
            r"\1",
            line,
        )
        line = _re.sub(
            r"\[\[([^\]]+)\]\(#calibre_link[^)]*\)(?:\{[^}]*\})?\]",
            r"\1",
            line,
        )

        # Remove remaining pandoc attribute blocks on heading lines
        # but keep the heading text
        if stripped.startswith("#"):
            line = _re.sub(r"\s*\{#[^}]*\}\s*$", "", line)
            line = _re.sub(r"\s*\{\.[^}]*\}\s*$", "", line)

        # TOC detection: heading "Contents"/"Оглавление" + following link entries
        heading_match = _re.match(r"^(#{1,6})\s+(.+?)\s*$", stripped)
        if heading_match:
            level = len(heading_match.group(1))
            title = heading_match.group(2).lower()
            if title in ("contents", "оглавление", "table of contents", "toc", "содержание"):
                in_toc = True
                toc_level = level
                continue
            elif in_toc and level <= toc_level:
                in_toc = False

        if in_toc:
            # Skip TOC entries (lines that look like links to anchors)
            if _re.search(r"\]\(#", stripped) or _re.match(r"^\s*[\d\-\*]\.?\s+\[", stripped):
                continue

        cleaned.append(line)

    text = "\n".join(cleaned)

    # Remove empty attribute-only lines left after cleanup
    text = _re.sub(r"\n\{[^}]*\}\s*\n", "\n\n", text)

    # Collapse multiple blank lines
    text = _re.sub(r"\n{3,}", "\n\n", text)

    md_path.write_text(text.strip() + "\n", encoding="utf-8")


def fb2_to_markdown(fb2_path: Path, output_path: Path):
    run_cmd(
        [
            "pandoc",
            str(fb2_path),
            "-f",
            "fb2",
            "-t",
            "markdown",
            "--wrap=none",
            "-o",
            str(output_path),
        ],
        f"pandoc {fb2_path.name} -> {output_path.name}",
        log_desc=True,
    )


# ─────────────────────────────────────────────────────────────────────
# Heading-aware chunking
# ─────────────────────────────────────────────────────────────────────


def parse_heading(line: str) -> tuple[int, str] | None:
    """Detect H1 (#) or H2 (##) in a Markdown line emitted by Pandoc.

    Matches only ATX-style headings (Pandoc default for EPUB->MD).
    Rejects lower-level headings (### ... ) by design; we treat them as
    regular body text and split only at chapter (##) or book (#) level.
    """
    m = re.match(r"^(#{1,2})\s+(.+?)\s*$", line)
    if m:
        title = m.group(2).strip()
        # Drop Pandoc attribute block: "One {#calibre_link-3 .subtitle}"
        title = re.sub(r"\s*\{[^}]*\}\s*$", "", title).strip()
        return (len(m.group(1)), title)
    return None


# Headings that mark non-narrative front/back matter. These are real
# sections in the source EPUB (each often its own file) but should not
# become separate tiny chunks — they get merged into a neighbour chapter.
_FRONT_BACK_MATTER_RE = re.compile(
    r"^(СОДЕРЖАНИЕ|Содержание|Contents|Table of Contents|TOC|Оглавление|"
    r"ОГЛАВЛЕНИЕ|Title Page|Copyright|Acknowledg"
    r"ments?|Благодарности|About the Author|Об авторе|Also by|Книги автора|"
    r"Dedication|Посвящение|Preface|Предисловие|Introduction|Введение|"
    r"Glossary|Глоссарий|Index|Указатель|Appendix|Приложение|Postscript|"
    r"Послесловие|Notes?|Примечания)\b",
    re.IGNORECASE,
)

# Front/back matter sections longer than this are kept as their own chunks
# (they may be substantial standalone essays, e.g. a long Introduction).
# Everything shorter is merged into a neighbouring narrative chapter.
FRONT_BACK_MATTER_MAX_CHARS = 15000


def _section_kind(title: str) -> str:
    """Classify a section by its heading title."""
    if title == "(front matter)":
        return "front"
    if _FRONT_BACK_MATTER_RE.match(title):
        return "frontback"
    return "narrative"


def merge_front_back_matter(sections: list[dict]) -> list[dict]:
    """Merge short non-narrative (front/back matter) sections into a
    neighbouring narrative section so they don't become tiny standalone
    chunks (e.g. a 'Contents' or 'Acknowledgments' EPUB file of its own).

    Rules:
      - A `front` or `frontback` section with no body is dropped.
      - Front/back sections are kept in their original document order and
        attached to the nearest narrative section (the following one, so a
        title page / contents precedes chapter 'One'; back matter after the
        last chapter is attached to that final chapter). When there is no
        narrative section at all (e.g. a book that is only front matter),
        sections are kept as-is.
      - Sections larger than FRONT_BACK_MATTER_MAX_CHARS are treated as
        narrative even if their heading matches the front/back pattern (a
        long 'Introduction' essay, for instance), so real content is never
        silently buried.
    """
    if not sections:
        return sections

    # Tag kind, but upgrade long front/back sections to narrative.
    tagged = []
    for idx, s in enumerate(sections):
        kind = _section_kind(s["title"])
        if kind in ("front", "frontback") and len(s["text"]) >= FRONT_BACK_MATTER_MAX_CHARS:
            kind = "narrative"
        tagged.append({**s, "_kind": kind})

    merged: list[dict] = []
    pending: list[dict] = []  # accumulating front/back matter in doc order

    def flush_pending(before: dict | None = None):
        # Attach accumulated front/back matter to `before` (a narrative
        # section dict), preserving original order and placing it BEFORE
        # the chapter text (so a title page / contents precedes chapter
        # 'One'; trailing back matter after the last chapter follows it).
        nonlocal pending
        non_empty = [p for p in pending if p["text"].strip()]
        if before is not None and non_empty:
            acc = "\n\n".join(p["text"].strip() for p in non_empty)
            before["text"] = (acc + "\n\n" + before["text"]).strip()
        pending = []

    for s in tagged:
        if s["_kind"] in ("front", "frontback"):
            pending.append(s)
            continue
        # Narrative section: flush any pending front/back matter before it.
        flush_pending(before=s)
        merged.append(s)

    # Any front/back matter left after the last narrative section is
    # appended to that final narrative section (back matter). If there is
    # no narrative section at all, keep pending sections standalone.
    if merged:
        flush_pending(before=merged[-1])
    else:
        merged = [p for p in pending if p["text"].strip()]
        pending = []

    # Strip helper key.
    return [{k: v for k, v in s.items() if k != "_kind"} for s in merged]


def split_into_sections(text: str) -> list[dict]:
    """Split Markdown into sections at H1/H2 boundaries.

    Returns list of {level, title, text} dicts. The very first chunk
    preceding any heading goes into a synthetic preamble section
    (level=0, title="(front matter)") so we don't lose foreword/dedication.
    """
    lines = text.splitlines()
    sections: list[dict] = []
    current_level = 0
    current_title = "(front matter)"
    current_lines: list[str] = []

    def flush():
        body = "\n".join(current_lines).strip()
        if body:
            sections.append(
                {
                    "level": current_level,
                    "title": current_title,
                    "text": body,
                }
            )

    for line in lines:
        h = parse_heading(line)
        if h:
            flush()
            current_level, current_title = h[0], h[1]
            current_lines = [line]
        else:
            current_lines.append(line)
    flush()

    return sections


def chunk_section_text(section_text: str, chapter_only: bool = False) -> list[str]:
    """Chunk a single section at paragraph boundaries; sentence boundaries
    for enormous paragraphs (> SOFT_LIMIT).

    If chapter_only=True: return the entire section as a single chunk,
    regardless of size. Use when chapters are small enough to fit one
    sub-agent context and you want maximum coherence within a chapter.
    """
    if chapter_only:
        # One chunk per chapter — no splitting, even if very large.
        stripped = section_text.strip()
        return [stripped] if stripped else []

    paragraphs = [p.strip() for p in re.split(r"\n\n+", section_text) if p.strip()]
    if not paragraphs:
        return []

    chunks: list[str] = []
    current = ""

    for para in paragraphs:
        # unusually long paragraph -> split by sentence to avoid huge chunks
        if len(para) > SOFT_LIMIT:
            if current:
                chunks.append(current.strip())
                current = ""
            sentences = re.split(r'(?<=[.!?…»)])\s+(?=[А-ЯЁA-Z„"«(])', para)
            for sentence in sentences:
                if len(current) + len(sentence) > CHUNK_SIZE and current:
                    chunks.append(current.strip())
                    current = sentence
                else:
                    current = (current + "\n\n" + sentence) if current else sentence
            continue

        if len(current) + len(para) > CHUNK_SIZE and current:
            chunks.append(current.strip())
            current = para
        else:
            current = (current + "\n\n" + para) if current else para

    if current.strip():
        chunks.append(current.strip())
    return chunks


def split_into_chunks(
    md_path: Path,
    temp_dir: Path,
    manifest: dict,
    respect_headings: bool = True,
    chapter_only: bool = False,
) -> tuple[list[Path], dict[str, str]]:
    """Split Markdown into chunks. Returns (paths, {chunk_id: section_title}).

    When respect_headings=True (default), never crosses H1/H2 boundary.
    When chapter_only=True, each section becomes a single chunk (no
    further splitting by size). Both can be combined: respect_headings
    groups by chapter, chapter_only keeps each chapter intact.
    """
    text = md_path.read_text(encoding="utf-8")

    if respect_headings:
        sections = split_into_sections(text)
    else:
        sections = [{"level": 0, "title": "", "text": text.strip()}]

    # Merge short front/back matter sections (title page, contents,
    # dedication, acknowledgments, postscript, …) into a neighbouring
    # chapter so they don't become tiny standalone chunks. Applied in both
    # normal and --chapter-only modes.
    sections = merge_front_back_matter(sections)

    pdir = process_dir(temp_dir)
    chunk_paths: list[Path] = []
    section_for_chunk: dict[str, str] = {}
    chunk_index = 0

    for section in sections:
        section_chunks = chunk_section_text(section["text"], chapter_only=chapter_only)
        for chunk_text in section_chunks:
            chunk_index += 1
            chunk_id = f"chunk{chunk_index:04d}"
            chunk_path = pdir / f"{chunk_id}.md"
            chunk_path.write_text(chunk_text, encoding="utf-8")
            manifest["chunks"][chunk_id] = {
                "source": sha256_file(chunk_path),
                "size": len(chunk_text),
                "section_title": section["title"],
                "section_level": section["level"],
            }
            section_for_chunk[chunk_id] = section["title"]
            chunk_paths.append(chunk_path)

    return chunk_paths, section_for_chunk


# ─────────────────────────────────────────────────────────────────────
# Metadata
# ─────────────────────────────────────────────────────────────────────


def extract_metadata_from_opf(epub_path: Path) -> dict:
    """Extract title/author from the EPUB OPF package metadata.

    EPUB title and author live in the OPF file (<dc:title>, <dc:creator>),
    not in the per-XHTML <meta> tags. Returns empty strings if the OPF
    cannot be read.
    """
    try:
        import zipfile

        from bs4 import BeautifulSoup

        with zipfile.ZipFile(epub_path) as zf:
            # Find the OPF (content) file via container.xml
            opf_path = None
            try:
                container = BeautifulSoup(zf.read("META-INF/container.xml"), "lxml-xml")
                rootfile = container.find("rootfile")
                if rootfile is not None:
                    opf_path = rootfile.get("full-path")
            except (KeyError, Exception):
                opf_path = None

            if opf_path is None:
                # Fallback: guess the first .opf in the archive
                opf_candidates = [n for n in zf.namelist() if n.lower().endswith(".opf")]
                if not opf_candidates:
                    return {"original_title": "", "author": ""}
                opf_path = sorted(opf_candidates, key=lambda n: len(n))[0]

            soup = BeautifulSoup(zf.read(opf_path), "lxml-xml")
            title = ""
            author = ""

            title_tag = soup.find("dc:title")
            if title_tag is None:
                title_tag = soup.find("title")
            if title_tag:
                title = title_tag.get_text(strip=True)

            creator_tag = soup.find("dc:creator")
            if creator_tag is None:
                creator_tag = soup.find("creator")
            if creator_tag:
                author = creator_tag.get_text(strip=True)

            return {"original_title": title, "author": author}
    except Exception:
        return {"original_title": "", "author": ""}


def extract_metadata_from_html(html_path: Path) -> dict:
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html_path.read_text(encoding="utf-8"), "lxml")
        title = ""
        author = ""

        title_tag = soup.find("title")
        if title_tag:
            title = title_tag.get_text(strip=True)

        for meta in soup.find_all("meta"):
            name = meta.get("name", "").lower()
            if name == "author":
                author = meta.get("content", "")
            if name == "dc.creator":
                author = meta.get("content", "")

        return {"original_title": title, "author": author}
    except ImportError:
        return {"original_title": "", "author": ""}


def extract_metadata_from_fb2(fb2_path: Path) -> dict:
    try:
        from bs4 import BeautifulSoup

        content = fb2_path.read_text(encoding="utf-8", errors="replace")
        soup = BeautifulSoup(content, "lxml-xml")

        title = ""
        author = ""

        book_title = soup.find("book-title")
        if book_title:
            title = book_title.get_text(strip=True)

        first_name = soup.find("first-name")
        last_name = soup.find("last-name")
        if first_name and last_name:
            author = f"{last_name.get_text(strip=True)}, {first_name.get_text(strip=True)}"

        return {"original_title": title, "author": author}
    except ImportError:
        return {"original_title": "", "author": ""}


def write_config(temp_dir: Path, metadata: dict, source_format: str):
    config_path = process_dir(temp_dir) / "config.txt"
    with open(config_path, "w", encoding="utf-8") as f:
        f.write(f"original_title={metadata.get('original_title', '')}\n")
        f.write(f"author={metadata.get('author', '')}\n")
        f.write(f"source_format={source_format}\n")
        f.write("output_lang=ru\n")


# ─────────────────────────────────────────────────────────────────────
# Safety: protect against accidental overwrite of an in-progress run
# ─────────────────────────────────────────────────────────────────────


def check_existing_outputs(temp_dir: Path) -> int:
    return len(list(process_dir(temp_dir).glob("output_chunk*.md")))


def check_fingerprint(temp_dir: Path, input_path: Path) -> tuple[str, str]:
    """Compare current input hash against saved fingerprint.

    Returns (current_sha256, previous_sha256_or_empty).
    """
    current = sha256_file(input_path)
    fp_path = process_dir(temp_dir) / "source_fingerprint.json"
    if not fp_path.exists():
        return current, ""
    try:
        prev = json.loads(fp_path.read_text(encoding="utf-8"))
        return current, prev.get("source_sha256", "")
    except (json.JSONDecodeError, KeyError):
        return current, ""


def write_fingerprint(temp_dir: Path, input_path: Path, sha256: str):
    from config import atomic_write_json

    fp_path = process_dir(temp_dir) / "source_fingerprint.json"
    atomic_write_json(
        fp_path,
        {
            "source_file": input_path.name,
            "source_sha256": sha256,
        },
        indent=2,
    )


# ─────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────


def main():
    if len(sys.argv) < 2:
        print(
            "Usage: python3 convert.py <input_path> [--temp-root <dir>] [--force] [--no-chapter-split] [--chapter-only]"
        )
        print()
        print("Flags:")
        print("  --temp-root <dir>    alternative parent for <name>_temp/")
        print("  --force              skip safety gates (fingerprint, existing outputs)")
        print("  --no-chapter-split   ignore H1/H2 boundaries — treat as one stream")
        print("  --chapter-only       one chunk per chapter (no size-based splitting)")
        print()
        print("Settings can also be overridden in config.toml [chunking] section.")
        sys.exit(1)

    # ── Pre-flight: required binaries ──────────────────────────────────
    if not shutil.which("pandoc"):
        print(
            "ERROR: pandoc not found in PATH. Install Pandoc.",
            file=sys.stderr,
        )
        sys.exit(1)

    input_path = Path(sys.argv[1]).resolve()
    if not input_path.exists():
        print(f"ERROR: File not found: {input_path}")
        sys.exit(1)

    temp_root = None
    if "--temp-root" in sys.argv:
        idx = sys.argv.index("--temp-root")
        temp_root = Path(sys.argv[idx + 1]).resolve()

    force = "--force" in sys.argv
    no_chapter_split = "--no-chapter-split" in sys.argv
    # --chapter-only flag overrides config.toml [chunking].chapter_only
    chapter_only_flag = "--chapter-only" in sys.argv
    # Effective: flag overrides config; config default is True
    chapter_only = chapter_only_flag or CHAPTER_ONLY

    if chapter_only and no_chapter_split:
        print(
            "ERROR: --chapter-only and --no-chapter-split are mutually exclusive.",
            file=sys.stderr,
        )
        sys.exit(1)

    stem = input_path.stem
    if temp_root:
        temp_dir = temp_root / f"{stem}_temp"
    else:
        temp_dir = input_path.parent / f"{stem}_temp"

    temp_dir.mkdir(parents=True, exist_ok=True)

    # ── Safety gate #1: source fingerprint ───────────────────────────
    current_sha, prev_sha = check_fingerprint(temp_dir, input_path)
    if prev_sha and prev_sha != current_sha:
        sys.stderr.write(
            f"[convert] WARNING: source file hash differs from previous run\n"
            f"           previous sha256: {prev_sha[:16]}...\n"
            f"           current  sha256: {current_sha[:16]}...\n"
        )
        if not force:
            sys.stderr.write(
                "[convert] Refusing to overwrite manifest based on a different\n"
                "           source file. Pass --force if you intended to retarget.\n"
            )
            sys.exit(2)

    # ── Safety gate #2: existing translations ────────────────────────
    existing_outputs = check_existing_outputs(temp_dir)
    if existing_outputs > 0 and not force:
        sys.stderr.write(
            f"[convert] ERROR: temp dir already contains {existing_outputs} "
            "output_chunk*.md files.\n"
            f"           temp: {temp_dir}\n"
            f"           Re-running will not delete outputs but the new manifest\n"
            f"           may orphan them downstream. Move them aside first or\n"
            f"           pass --force to proceed anyway.\n"
        )
        sys.exit(2)

    write_fingerprint(temp_dir, input_path, current_sha)

    ext = input_path.suffix.lower()
    manifest: dict = {
        "version": 2,
        "chunks": {},
        "source_file": input_path.name,
        "chapter_split": not no_chapter_split,
        "converter": "convert.py",
    }

    if ext == ".epub":
        sys.stderr.write("[convert] Converting EPUB -> Markdown (zip extract + pandoc)\n")
        md_path = epub_to_markdown(input_path, process_dir(temp_dir))
        # EPUB title/author live in the OPF package metadata, not in the
        # per-XHTML <meta> tags produced by pandoc conversion.
        metadata = extract_metadata_from_opf(input_path)
        source_format = "epub"

    elif ext == ".fb2":
        md_path = process_dir(temp_dir) / "input.md"
        fb2_to_markdown(input_path, md_path)
        metadata = extract_metadata_from_fb2(input_path)
        source_format = "fb2"

    else:
        print(f"ERROR: Unsupported format: {ext}. Only .epub and .fb2 are supported.")
        sys.exit(1)

    chunk_paths, section_map = split_into_chunks(
        md_path,
        temp_dir,
        manifest,
        respect_headings=not no_chapter_split,
        chapter_only=chapter_only,
    )

    if chapter_only:
        sys.stderr.write("[convert] chapter-only mode: one chunk per chapter (no size-based splitting)\n")

    # Sidecar: chunk -> section-title mapping for QA / debugging
    pdir = process_dir(temp_dir)
    from config import atomic_write_json

    atomic_write_json(pdir / "chunk_sections.json", section_map, indent=2, ensure_ascii=False)

    atomic_write_json(pdir / "manifest.json", manifest, indent=2, ensure_ascii=False)

    write_config(temp_dir, metadata, source_format)

    # ── Step 2.5: structural extraction (deterministic) ────────────
    # After markdown is finalised, extract epigraphs/footnotes and group
    # chunks into narrative arcs. Both are deterministic — semantic
    # decisions (narrator identity, cultural references) stay with the
    # LLM agent.
    script_dir = Path(__file__).resolve().parent
    try:
        run_post_step(script_dir / "detect_epigraphs.py", temp_dir)
    except Exception as e:
        sys.stderr.write(f"[convert] detect_epigraphs failed: {e}\n")
    try:
        run_post_step(script_dir / "narrator_marker.py", temp_dir)
    except Exception as e:
        sys.stderr.write(f"[convert] narrator_marker failed: {e}\n")

    # ── Step 2.6: EPUB footnote extraction (deterministic) ─────────
    # Some EPUBs put footnote bodies in a separate XHTML file (e.g.
    # part0039.xhtml) — pandoc loses them and emits raw anchor markup.
    # This step extracts bodies and rewrites anchors as [^N] with
    # globally-unique numbering. No-op for books without EPUB footnotes.
    try:
        run_post_step(script_dir / "extract_footnotes.py", temp_dir)
    except Exception as e:
        sys.stderr.write(f"[convert] extract_footnotes failed: {e}\n")

    # Quick stats for sanity
    sizes = [info["size"] for info in manifest["chunks"].values()]
    tiny = [k for k, v in manifest["chunks"].items() if v["size"] < MIN_CHUNK]
    if tiny:
        sys.stderr.write(
            f"[convert] WARNING: {len(tiny)} chunks shorter than "
            f"{MIN_CHUNK} chars (possible bad split):\n"
            f"           {', '.join(tiny[:8])}{'...' if len(tiny) > 8 else ''}\n"
        )

    print(f"Source:    {input_path.name}")
    print(f"Format:    {source_format}")
    print(f"Chunks:    {len(chunk_paths)} (chapter-aware: {not no_chapter_split})")
    if sizes:
        print(f"Size stats: min {min(sizes)}, max {max(sizes)}, avg {sum(sizes) // len(sizes)} chars")
    print(f"Temp:      {temp_dir}")
    print(f"Title:     {metadata.get('original_title', '?')}")
    print(f"Author:    {metadata.get('author', '?')}")
    print(f"Fingerprint: {current_sha[:16]}...")


if __name__ == "__main__":
    main()
