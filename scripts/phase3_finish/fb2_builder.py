#!/usr/bin/env python3
"""Markdown → FB2 builder using lxml.etree.

Builds a FictionBook 2.0 (.fb2) file from Markdown with correct handling of:
  - Chapter headings (# / ##) → <section><title>...</title>...</section>
  - Paragraphs → <p>...</p>
  - Italic *text* / _text_ → <p><emphasis>text</emphasis></p>
  - Bold **text** → <p><strong>text</strong></p>
  - Blockquotes (>) before a heading → <epigraph>...<text-author>...</text-author></epigraph>
    (deferred to next section — FB2 schema requires <epigraph> right after <title>)
  - Images ![alt](path) → <image l:href="#_img_N"/> + <binary id="_img_N" content-type="...">base64</binary>
  - Footnotes [^N] + [^N]: ... → <a l:href="#note_N"><sup>N</sup></a> + <body name="notes"><section id="note_N">...</section></body>
  - Scene breaks (*** on its own line) → <empty-line/>
  - Direct speech with em-dash preserved as plain <p>

Uses lxml.etree for XML construction with automatic escaping.
Output is pretty-printed via pretty_fb2.py, then validated against
FictionBook 2.0 XSD schema. Validation errors are printed to stderr
but do not block writing the file.

Usage:
    from fb2_builder import build_fb2
    build_fb2(md_text, title="Book", author="Author", output_path=Path("book.fb2"))

    # Or as a script:
    python3 fb2_builder.py input.md output.fb2 --title "Title" --author "Author"

Requires: lxml (already in requirements.txt as part of beautifulsoup4 deps)
"""

import base64
import datetime
import hashlib
import re
import sys
from pathlib import Path

from lxml import etree

# Ensure UTF-8 output on Windows (cp1251 default breaks on non-ASCII)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "shared"))


# ─────────────────────────────────────────────────────────────────────
# Namespaces
# ─────────────────────────────────────────────────────────────────────

NS_FB = "http://www.gribuser.ru/xml/fictionbook/2.0"
NS_XLINK = "http://www.w3.org/1999/xlink"

NSMAP = {None: NS_FB, "xl": NS_XLINK, "xlink": NS_XLINK}


def _q(tag: str) -> str:
    """Qualify a tag name with the FB2 namespace."""
    return f"{{{NS_FB}}}{tag}"


def _qxl(tag: str) -> str:
    """Qualify an xlink tag name."""
    return f"{{{NS_XLINK}}}{tag}"


# ─────────────────────────────────────────────────────────────────────
# Image handling
# ─────────────────────────────────────────────────────────────────────


def _guess_content_type(path: str) -> str:
    """Guess MIME content-type from file extension."""
    ext = Path(path).suffix.lower()
    return {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".gif": "image/gif",
        ".bmp": "image/bmp",
        ".svg": "image/svg+xml",
    }.get(ext, "application/octet-stream")


def _load_image(path: Path) -> tuple[str, bytes] | None:
    """Load an image file, return (content_type, raw_bytes) or None."""
    if not path.exists():
        return None
    ct = _guess_content_type(path.name)
    try:
        return ct, path.read_bytes()
    except OSError:
        return None


# ─────────────────────────────────────────────────────────────────────
# Inline Markdown → FB2 inline markup (lxml elements)
# ─────────────────────────────────────────────────────────────────────


def _process_inline_to_children(text: str, parent: etree._Element) -> list[etree._Element | str]:
    """Convert inline Markdown to a list of lxml children + text fragments."""
    children: list[etree._Element | str] = []
    buf = ""

    def flush():
        nonlocal buf
        if buf:
            children.append(buf)
            buf = ""

    patterns = [
        (re.compile(r"`([^`]+)`"), "code"),
        (re.compile(r"\*\*([^*]+)\*\*"), "bold"),
        (re.compile(r"__([^_]+)__"), "bold"),
        (re.compile(r"\*([^*]+)\*"), "italic"),
        (re.compile(r"(?<!\w)_([^_]+)_(?!\w)"), "italic"),
        (re.compile(r"!\[([^\]]*)\]\(([^)]+)\)"), "image"),
        (re.compile(r"\[\^([^\]]+)\](?!:)"), "footnote"),
        (re.compile(r"\[([^\]]+)\]\(([^)]+)\)"), "link"),
    ]

    i = 0
    n = len(text)
    while i < n:
        matched = False
        for pattern, kind in patterns:
            m = pattern.match(text, i)
            if not m:
                continue
            flush()
            if kind == "code":
                el = etree.SubElement(parent, _q("code"))
                el.text = m.group(1)
                children.append(el)
            elif kind == "bold":
                el = etree.SubElement(parent, _q("strong"))
                el.text = m.group(1)
                children.append(el)
            elif kind == "italic":
                el = etree.SubElement(parent, _q("emphasis"))
                el.text = m.group(1)
                children.append(el)
            elif kind == "image":
                # Inline image: ![alt](src) → <image xl:href="#_img_N"/>
                # The binary_id must be in image_id_map; we use a placeholder
                # here and the caller (_build_inline) doesn't have access to
                # image_id_map. So we store the src and resolve later.
                # Workaround: emit <image xl:href="#SRC"/> with raw src;
                # _build_body's image_id_map won't help here.
                # Better: just emit the image with the src as-is; collect_images
                # has already loaded it. We need the binary_id.
                # Since _process_inline_to_children doesn't have access to
                # image_id_map, we emit a placeholder and post-process.
                # For now, emit <image xl:href="#{src}"/> — fb2_builder's
                # collect_images uses src as key, and we can map later.
                img_src = m.group(2)
                img_el = etree.SubElement(parent, _q("image"))
                # Use src as href temporarily; will be remapped to #_img_N
                # in a post-processing step if needed. For simplicity, we
                # use the binary_id from image_id_map if available — but
                # that's not accessible here. So we use src directly.
                # The caller should pass image_id_map if needed.
                img_el.set(_qxl("href"), f"#{img_src}")
                img_el.set(_qxl("type"), "simple")
                children.append(img_el)
            elif kind == "footnote":
                fn_id = m.group(1)
                a = etree.SubElement(parent, _q("a"))
                a.set(_qxl("href"), f"#note_{fn_id}")
                a.set(_qxl("type"), "simple")
                sup = etree.SubElement(a, _q("sup"))
                sup.text = fn_id
                children.append(a)
            elif kind == "link":
                text_part = m.group(1)
                url = m.group(2)
                a = etree.SubElement(parent, _q("a"))
                a.set(_qxl("href"), url)
                a.set(_qxl("type"), "simple")
                a.text = text_part
                children.append(a)
            i = m.end()
            matched = True
            break
        if not matched:
            buf += text[i]
            i += 1
    flush()
    return children


def _build_inline(text: str, parent: etree._Element, image_id_map: dict[str, str] | None = None) -> None:
    """Build inline content into parent element.

    image_id_map: {src: binary_id} for resolving inline images.
    If None or src not in map, uses src as-is (may produce broken link).
    """
    placeholder = etree.Element("placeholder")
    children = _process_inline_to_children(text, placeholder)
    for child in list(placeholder):
        placeholder.remove(child)

    # Remap inline image hrefs from #{src} to #{binary_id}
    if image_id_map:
        for child in children:
            if isinstance(child, etree._Element) and child.tag == _q("image"):
                href = child.get(_qxl("href"), "")
                if href.startswith("#") and href[1:] in image_id_map:
                    child.set(_qxl("href"), f"#{image_id_map[href[1:]]}")

    first_text: str | None = None
    last_element: etree._Element | None = None
    for item in children:
        if isinstance(item, str):
            if last_element is None:
                if first_text is None:
                    first_text = item
                else:
                    first_text += item
            else:
                if last_element.tail is None:
                    last_element.tail = item
                else:
                    last_element.tail += item
        else:
            parent.append(item)
            last_element = item

    if first_text is not None:
        parent.text = first_text


# ─────────────────────────────────────────────────────────────────────
# Block-level Markdown parsing
# ─────────────────────────────────────────────────────────────────────


class Block:
    """A parsed block of Markdown content."""

    kind: str
    level: int = 0
    text: str = ""
    alt: str = ""
    src: str = ""
    footnote_id: str = ""

    def __init__(self, kind: str, **kwargs):
        self.kind = kind
        for k, v in kwargs.items():
            setattr(self, k, v)


def parse_markdown(text: str) -> list[Block]:
    """Parse Markdown into a list of Block objects."""
    lines = text.split("\n")
    blocks: list[Block] = []

    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        stripped = line.strip()

        if not stripped:
            i += 1
            continue

        if stripped.startswith("```"):
            code_lines = []
            i += 1
            while i < n and not lines[i].strip().startswith("```"):
                code_lines.append(lines[i])
                i += 1
            i += 1
            blocks.append(Block("code_block", text="\n".join(code_lines)))
            continue

        m = re.match(r"^(#{1,6})\s+(.+?)\s*$", stripped)
        if m:
            level = len(m.group(1))
            title_text = re.sub(r"\s*\{[^}]*\}\s*$", "", m.group(2)).strip()
            blocks.append(Block("heading", level=level, text=title_text))
            i += 1
            continue

        if re.match(r"^(\*\s*){3,}$", stripped) or stripped == "***" or stripped == "---":
            blocks.append(Block("scene_break"))
            i += 1
            continue

        m = re.match(r"^!\[([^\]]*)\]\(([^)]+)\)\s*$", stripped)
        if m:
            blocks.append(Block("image", alt=m.group(1), src=m.group(2)))
            i += 1
            continue

        m = re.match(r"^\[\^([^\]]+)\]:\s*(.*)$", stripped)
        if m:
            fn_id = m.group(1)
            fn_lines = [m.group(2)]
            i += 1
            while i < n and (lines[i].startswith("    ") or lines[i].startswith("\t")):
                fn_lines.append(lines[i].lstrip())
                i += 1
            blocks.append(Block("footnote_def", footnote_id=fn_id, text="\n".join(fn_lines)))
            continue

        if stripped.startswith(">"):
            bq_lines = []
            while i < n and lines[i].lstrip().startswith(">"):
                bq_lines.append(re.sub(r"^\s*>\s?", "", lines[i]))
                i += 1
            blocks.append(Block("blockquote", text="\n".join(bq_lines)))
            continue

        para_lines = [line]
        i += 1
        while i < n:
            next_stripped = lines[i].strip()
            if (
                not next_stripped
                or next_stripped.startswith("#")
                or next_stripped.startswith("```")
                or next_stripped.startswith(">")
                or next_stripped == "***"
                or next_stripped == "---"
                or re.match(r"^!\[([^\]]*)\]\(([^)]+)\)\s*$", next_stripped)
                or re.match(r"^\[\^([^\]]+)\]:\s*", next_stripped)
            ):
                break
            para_lines.append(lines[i])
            i += 1
        blocks.append(Block("paragraph", text="\n".join(para_lines).strip()))

    return blocks


# ─────────────────────────────────────────────────────────────────────
# FB2 XML construction (lxml.etree)
# ─────────────────────────────────────────────────────────────────────


def _build_description(
    root: etree._Element,
    title: str,
    author: str,
    date_str: str,
    doc_id: str,
    genre: str = "prose_counter",
) -> None:
    """Build the <description> section of the FB2 document."""
    desc = etree.SubElement(root, _q("description"))

    ti = etree.SubElement(desc, _q("title-info"))
    genre_el = etree.SubElement(ti, _q("genre"))
    genre_el.text = genre
    author_el = etree.SubElement(ti, _q("author"))
    _fill_author_fields(author_el, author)
    bt = etree.SubElement(ti, _q("book-title"))
    bt.text = title
    lang = etree.SubElement(ti, _q("lang"))
    lang.text = "ru"
    src_lang = etree.SubElement(ti, _q("src-lang"))
    src_lang.text = "en"

    di = etree.SubElement(desc, _q("document-info"))
    di_author = etree.SubElement(di, _q("author"))
    nick = etree.SubElement(di_author, _q("nickname"))
    nick.text = "book-translate-ru"
    pu = etree.SubElement(di, _q("program-used"))
    pu.text = "book-translate-ru fb2_builder.py"
    date_el = etree.SubElement(di, _q("date"))
    date_el.set("value", date_str)
    date_el.text = date_str
    id_el = etree.SubElement(di, _q("id"))
    id_el.text = doc_id
    ver = etree.SubElement(di, _q("version"))
    ver.text = "1.0"


def _fill_author_fields(author_el: etree._Element, author_str: str) -> None:
    """Fill <author> with first-name / middle-name / last-name.

    Per FB2 2.0 XSD (authorType), <author> is a choice of:
      - Sequence 1: first-name (required) + middle-name? + last-name (required) + nickname? + ...
      - Sequence 2: nickname (required) + ...

    So if the user provides "Last, First" or "First Last" → use sequence 1.
    If the user provides just one token (no clear first/last) → use sequence 2 (nickname).
    """
    if not author_str or not author_str.strip():
        nick = etree.SubElement(author_el, _q("nickname"))
        nick.text = "Unknown"
        return

    author_str = author_str.strip()

    if "," in author_str:
        parts = [p.strip() for p in author_str.split(",", 1)]
        last = parts[0]
        first_parts = parts[1].split() if len(parts) > 1 and parts[1].strip() else []
    else:
        parts = author_str.split()
        if len(parts) == 1:
            # Single token — use as nickname (sequence 2)
            nick = etree.SubElement(author_el, _q("nickname"))
            nick.text = parts[0]
            return
        elif len(parts) == 2:
            first_parts = [parts[0]]
            last = parts[1]
        else:
            first_parts = [parts[0]]
            middle = " ".join(parts[1:-1])
            last = parts[-1]
            m_el = etree.SubElement(author_el, _q("middle-name"))
            m_el.text = middle

    # Sequence 1: first-name + middle-name? + last-name
    if first_parts:
        f_el = etree.SubElement(author_el, _q("first-name"))
        f_el.text = " ".join(first_parts)
    else:
        # Need first-name for sequence 1; if missing, fall back to nickname
        nick = etree.SubElement(author_el, _q("nickname"))
        nick.text = last
        return
    l_el = etree.SubElement(author_el, _q("last-name"))
    l_el.text = last


def _build_body(
    root: etree._Element,
    blocks: list[Block],
    image_id_map: dict[str, str],
) -> etree._Element | None:
    """Build the main <body> section.

    Per FB2 2.0 XSD, the main body does NOT have a 'name' attribute
    (the first body is main by default). Only <body name="notes"> for
    the notes body is allowed.

    Per FB2 2.0 XSD, <epigraph> must appear at the START of a <section>,
    right after <title>. So blockquotes that precede a heading (which
    become epigraphs) are deferred and attached to the NEXT section.
    """
    if not blocks:
        return None

    # Main body — NO name attribute (FB2 schema)
    body = etree.SubElement(root, _q("body"))

    current_section: etree._Element | None = None
    pending_epigraphs: list[tuple[list[str], str]] = []

    def open_new_section() -> etree._Element:
        nonlocal current_section
        sec = etree.SubElement(body, _q("section"))
        current_section = sec
        return sec

    def attach_epigraphs_to_section(sec: etree._Element) -> None:
        nonlocal pending_epigraphs
        if not pending_epigraphs:
            return
        insert_at = 0
        for i, child in enumerate(sec):
            if child.tag != _q("title"):
                insert_at = i
                break
        else:
            insert_at = len(sec)
        for i, (lines, attribution) in enumerate(pending_epigraphs):
            ep = etree.Element(_q("epigraph"))
            for line in lines:
                if line.strip():
                    p = etree.SubElement(ep, _q("p"))
                    _build_inline(line.strip(), p, image_id_map=image_id_map)
            if attribution:
                ta = etree.SubElement(ep, _q("text-author"))
                _build_inline(attribution, ta, image_id_map=image_id_map)
            sec.insert(insert_at + i, ep)
        pending_epigraphs = []

    for idx, block in enumerate(blocks):
        if block.kind == "heading":
            sec = open_new_section()
            title_el = etree.SubElement(sec, _q("title"))
            p_el = etree.SubElement(title_el, _q("p"))
            _build_inline(block.text, p_el, image_id_map=image_id_map)
            attach_epigraphs_to_section(sec)
        elif block.kind == "scene_break":
            if current_section is None:
                sec = open_new_section()
                attach_epigraphs_to_section(sec)
            etree.SubElement(current_section, _q("empty-line"))
        elif block.kind == "paragraph":
            if current_section is None:
                sec = open_new_section()
                attach_epigraphs_to_section(sec)
            p_el = etree.SubElement(current_section, _q("p"))
            _build_inline(block.text, p_el, image_id_map=image_id_map)
        elif block.kind == "blockquote":
            next_heading = False
            for j in range(idx + 1, len(blocks)):
                if blocks[j].kind == "heading":
                    next_heading = True
                    break
                if blocks[j].kind in ("paragraph", "blockquote", "image"):
                    break

            bq_lines = block.text.split("\n")
            attribution = ""
            content_lines = bq_lines
            if bq_lines and re.match(r"^\s*[—–-]\s*", bq_lines[-1]):
                attribution = re.sub(r"^\s*[—–-]\s*", "", bq_lines[-1]).strip()
                content_lines = bq_lines[:-1]

            if next_heading:
                pending_epigraphs.append((content_lines, attribution))
            else:
                if current_section is None:
                    sec = open_new_section()
                    attach_epigraphs_to_section(sec)
                cite = etree.SubElement(current_section, _q("cite"))
                for line in content_lines:
                    if line.strip():
                        p = etree.SubElement(cite, _q("p"))
                        _build_inline(line.strip(), p, image_id_map=image_id_map)
                if attribution:
                    p = etree.SubElement(cite, _q("p"))
                    emp = etree.SubElement(p, _q("emphasis"))
                    _build_inline(attribution, emp, image_id_map=image_id_map)
        elif block.kind == "image":
            if current_section is None:
                sec = open_new_section()
                attach_epigraphs_to_section(sec)
            binary_id = image_id_map.get(block.src, "_img_unknown")
            img_el = etree.SubElement(current_section, _q("image"))
            img_el.set(_qxl("href"), f"#{binary_id}")
            img_el.set(_qxl("type"), "simple")
        elif block.kind == "code_block":
            if current_section is None:
                sec = open_new_section()
                attach_epigraphs_to_section(sec)
            p = etree.SubElement(current_section, _q("p"))
            code = etree.SubElement(p, _q("code"))
            code.text = block.text

    if pending_epigraphs:
        if current_section is None:
            sec = open_new_section()
        attach_epigraphs_to_section(current_section)

    return body


def _build_notes_body(root: etree._Element, footnote_defs: list[Block]) -> etree._Element | None:
    """Build <body name="notes"> with all footnote definitions as sections."""
    if not footnote_defs:
        return None

    # Notes body — name="notes" attribute REQUIRED by FB2 schema
    body = etree.SubElement(root, _q("body"))
    body.set("name", "notes")
    for fd in footnote_defs:
        sec = etree.SubElement(body, _q("section"))
        sec.set("id", f"note_{fd.footnote_id}")
        title = etree.SubElement(sec, _q("title"))
        p = etree.SubElement(title, _q("p"))
        p.text = fd.footnote_id
        body_p = etree.SubElement(sec, _q("p"))
        _build_inline(fd.text, body_p)
    return body


def _build_binaries(root: etree._Element, images: dict[str, tuple[str, str, bytes]]) -> None:
    """Append <binary> elements for each loaded image."""
    for src, (binary_id, ct, raw) in images.items():
        if not raw:
            continue
        b_el = etree.SubElement(root, _q("binary"))
        b_el.set("id", binary_id)
        b_el.set("content-type", ct)
        b_el.text = base64.b64encode(raw).decode("ascii")


def collect_images(blocks: list[Block], temp_dir: Path, md_text: str = "") -> dict[str, tuple[str, str, bytes]]:
    """Find all image references in blocks AND inline text, load files.

    Scans:
    1. Standalone image blocks (Block with kind="image") — from parse_markdown
    2. Inline images in paragraph text — `![alt](src)` anywhere in md_text
    3. Images wrapped in hyperlinks — `[![alt](src)](url)` pattern

    This catches images that parse_markdown's standalone-image check
    misses (images on lines with other content, images inside links,
    multiple images on one line).
    """
    images: dict[str, tuple[str, str, bytes]] = {}
    counter = 0

    # Collect all image src references from both blocks and raw md_text
    all_srcs: list[str] = []

    # 1. From standalone image blocks
    for block in blocks:
        if block.kind == "image":
            all_srcs.append(block.src)

    # 2. From raw md_text — find ALL ![alt](src) patterns, including:
    #    - Inline in paragraphs: "text ![img](x.png) more text"
    #    - Multiple per line: "![a](1.png)![b](2.png)"
    #    - Wrapped in links: "[![alt](img.png)](https://...)"
    #    The pattern matches the inner ![alt](src) regardless of wrapping.
    if md_text:
        # Find all ![alt](src) — non-greedy, allows empty alt
        for m in re.finditer(r"!\[([^\]]*)\]\(([^)]+)\)", md_text):
            all_srcs.append(m.group(2))

    # Deduplicate while preserving order
    seen_srcs: set[str] = set()
    unique_srcs: list[str] = []
    for src in all_srcs:
        if src not in seen_srcs:
            seen_srcs.add(src)
            unique_srcs.append(src)

    for src in unique_srcs:
        candidates = [
            temp_dir / src,
            temp_dir / src.lstrip("./"),
            temp_dir / "images" / Path(src).name,
            temp_dir / "process" / src.lstrip("./"),
            temp_dir / "process" / "images" / Path(src).name,
            temp_dir / "process" / "_epub_extracted" / src.lstrip("./"),
        ]
        loaded = None
        for cand in candidates:
            loaded = _load_image(cand)
            if loaded:
                break
        counter += 1
        binary_id = f"_img_{counter}"
        if loaded:
            ct, raw = loaded
            images[src] = (binary_id, ct, raw)
        else:
            sys.stderr.write(
                f"[fb2_builder] WARNING: image not found: {src}\n  tried: {[str(c) for c in candidates]}\n"
            )
            images[src] = (binary_id, "image/png", b"")
    return images


# ─────────────────────────────────────────────────────────────────────
# XSD validation
# ─────────────────────────────────────────────────────────────────────


def _validate_fb2_xsd(fb2_path: Path) -> tuple[bool, list[str]]:
    """Validate FB2 file against FictionBook 2.0 XSD schema.

    Returns (is_valid, list_of_error_messages).
    Schema is loaded from scripts/phase3_finish/schemas/FictionBook.xsd.
    """
    schema_path = Path(__file__).resolve().parent / "schemas" / "FictionBook.xsd"
    if not schema_path.exists():
        return True, [f"XSD schema not found at {schema_path} (skipping validation)"]

    try:
        parser = etree.XMLParser(remove_blank_text=True)
        xsd_doc = etree.parse(str(schema_path), parser)
        schema = etree.XMLSchema(xsd_doc)
    except Exception as e:
        return True, [f"Failed to load XSD schema: {e} (skipping validation)"]

    try:
        fb2_doc = etree.parse(str(fb2_path), etree.XMLParser(remove_blank_text=False))
    except Exception as e:
        return False, [f"Failed to parse FB2 file: {e}"]

    is_valid = schema.validate(fb2_doc)
    if is_valid:
        return True, []
    errors = [f"line {err.line}: {err.message}" for err in schema.error_log]
    return False, errors


# ─────────────────────────────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────────────────────────────


def build_fb2(
    md_text: str,
    title: str,
    author: str,
    output_path: Path,
    temp_dir: Path | None = None,
    date_str: str = "",
    genre: str = "prose_counter",
) -> tuple[bool, list[str]]:
    """Build a FictionBook 2.0 (.fb2) file from Markdown text.

    Returns (success, validation_errors). success is True if the file was
    written; validation_errors is a list of XSD validation errors (empty
    if valid or if validation was skipped).
    """
    if not date_str:
        date_str = datetime.date.today().isoformat()

    doc_id = "bt-ru-" + hashlib.sha1(f"{title}|{author}|{date_str}".encode("utf-8")).hexdigest()[:16]

    blocks = parse_markdown(md_text)
    body_blocks = [b for b in blocks if b.kind != "footnote_def"]
    footnote_defs = [b for b in blocks if b.kind == "footnote_def"]

    images: dict[str, tuple[str, str, bytes]] = {}
    if temp_dir is not None:
        images = collect_images(body_blocks, temp_dir, md_text=md_text)
    image_id_map = {src: info[0] for src, info in images.items()}

    root = etree.Element(_q("FictionBook"), nsmap=NSMAP)
    _build_description(root, title, author, date_str, doc_id, genre=genre)
    _build_body(root, body_blocks, image_id_map)
    _build_notes_body(root, footnote_defs)
    _build_binaries(root, images)

    tree = etree.ElementTree(root)
    xml_bytes = etree.tostring(
        tree,
        xml_declaration=True,
        encoding="utf-8",
        pretty_print=False,
    )

    from config import atomic_write_text

    atomic_write_text(output_path, xml_bytes.decode("utf-8"), encoding="utf-8")

    # Pretty-print via pretty_fb2.py (sibling script)
    try:
        import importlib

        sys.path.insert(0, str(Path(__file__).resolve().parent))
        pretty = importlib.import_module("pretty_fb2")
        pretty.format_fb2(output_path, output_path)
    except Exception as e:
        sys.stderr.write(f"[fb2_builder] WARNING: pretty_fb2 failed: {e}\n")

    # Validate against XSD
    is_valid, errors = _validate_fb2_xsd(output_path)
    if not is_valid:
        sys.stderr.write(
            f"[fb2_builder] WARNING: FB2 file failed XSD validation "
            f"({len(errors)} errors). File is still written. First errors:\n"
        )
        for err in errors[:10]:
            sys.stderr.write(f"  - {err}\n")
        if len(errors) > 10:
            sys.stderr.write(f"  ... and {len(errors) - 10} more\n")

    return True, errors


# ─────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Build FB2 from Markdown")
    parser.add_argument("input_md", help="input Markdown file")
    parser.add_argument("output_fb2", help="output .fb2 file")
    parser.add_argument("--title", required=True, help="book title")
    parser.add_argument("--author", default="", help="book author")
    parser.add_argument(
        "--temp-dir",
        default="",
        help="temp directory (for resolving relative image paths)",
    )
    parser.add_argument(
        "--genre",
        default="prose_counter",
        help="FB2 genre code (see FictionBookGenres.xsd)",
    )
    args = parser.parse_args()

    md_path = Path(args.input_md)
    out_path = Path(args.output_fb2)
    temp_dir = Path(args.temp_dir) if args.temp_dir else None

    md_text = md_path.read_text(encoding="utf-8")
    ok, errors = build_fb2(md_text, args.title, args.author, out_path, temp_dir, genre=args.genre)
    if ok:
        print(f"Wrote: {out_path} ({out_path.stat().st_size:,} bytes)")
        if errors:
            print(f"  XSD validation: {len(errors)} errors (see stderr)")
        else:
            print("  XSD validation: OK")
    else:
        print("ERROR: FB2 build failed", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
