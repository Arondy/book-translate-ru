#!/usr/bin/env python3
"""Detect epigraphs and footnotes in converted Markdown (deterministic).

An epigraph is a short block of text immediately preceding a chapter heading.
We find it by structural pattern, not by meaning — meaning is for the LLM
agent to interpret (see prompts/phase2_translate/эпиграф_обработка.md).

A footnote is an inline `[^N]` marker with a corresponding `[^N]: ...`
definition at the end of a section.

Outputs:
    <temp_dir>/structural_units.json

Usage:
    python3 detect_epigraphs.py <temp_dir>
"""

import re
import sys
from pathlib import Path

# Ensure UTF-8 output on Windows (cp1251 default breaks on non-ASCII)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "shared"))
from common import process_dir
from config import get_config  # Max epigraph length — loaded from config.toml [epigraphs] section

EPIGRAPH_MAX_CHARS = get_config().get("epigraphs", "epigraph_max_chars", 600)

# Patterns that look like a chapter heading
HEADING_RE = re.compile(r"^(#{1,2})\s+(.+?)\s*$")

# Headings that must NOT trigger epigraph detection. Blocks before these
# are front/back matter (title page, author block, table of contents,
# publisher boilerplate) — not chapter epigraphs.
NON_CHAPTER_HEADING_RE = re.compile(
    r"^(СОДЕРЖАНИЕ|Содержание|Contents|Table of Contents|TOC|Оглавление|"
    r"ОГЛАВЛЕНИЕ|Title Page|Copyright|Пролог(?!\b.*эпиграф)|Acknowledg"
    r"ments?|Благодарности|About the Author|Об авторе|Also by|Книги автора|"
    r"Dedication|Посвящение|Preface|Предисловие|Introduction|Введение|"
    r"Glossary|Глоссарий|Index|Указатель|Appendix|Приложение|Postscript|"
    r"Послесловие|Notes|Примечания)\b",
    re.IGNORECASE,
)

# Footnote definition: [^N]: text...
FOOTNOTE_DEF_RE = re.compile(r"^\[\^([^\]]+)\]:\s*(.+)$", re.MULTILINE)

# Footnote in-text marker: [^N] (not followed by :)
FOOTNOTE_MARKER_RE = re.compile(r"\[\^([^\]]+)\](?!:)")


def detect_epigraphs(md_text: str) -> list[dict]:
    """Find blocks of text immediately preceding H1/H2 headings.

    Heuristic: any non-empty content between two headings (or between
    start-of-file and first heading) is treated as a potential epigraph
    if it's shorter than EPIGRAPH_MAX_CHARS.

    This is intentionally conservative: it never guesses meaning. If a
    block is longer than the limit, we skip it — the agent can still
    process it if it wants.
    """
    lines = md_text.splitlines()
    found: list[dict] = []

    buffer: list[str] = []

    def flush_buffer(at_line: int, heading_match: re.Match | None):
        nonlocal buffer
        # strip leading/trailing blank lines
        while buffer and not buffer[0].strip():
            buffer.pop(0)
        while buffer and not buffer[-1].strip():
            buffer.pop()
        epigraph_text = "\n".join(buffer).strip()
        if not epigraph_text:
            buffer = []
            return

        if (
            len(epigraph_text) <= EPIGRAPH_MAX_CHARS
            and heading_match
            and not NON_CHAPTER_HEADING_RE.match(heading_match.group(2))
        ):
            # try to extract attribution: trailing "-- Name" or "— Name" line
            attribution = ""
            quoted = epigraph_text
            m = re.search(
                r"[\n\r]\s*[—–\-]\s*([^—–\-\n\r]+?)\s*$",
                epigraph_text,
            )
            if m:
                attribution = m.group(1).strip()
                quoted = epigraph_text[: m.start()].strip()

            heading_text = heading_match.group(2).strip()
            heading_level = len(heading_match.group(1))
            found.append(
                {
                    "structural_id": f"epigraph_before_line_{at_line}",
                    "before_heading_line": at_line,
                    "heading_text": heading_text,
                    "heading_level": heading_level,
                    "text": epigraph_text,
                    "quoted_text": quoted,
                    "attribution": attribution,
                    "agent_notes": "Detect-only. LLM agent decides how to translate.",
                }
            )
        else:
            # The block may belong to the previous chapter's content
            # (i.e., it's not a real epigraph). Don't record it.
            pass

        buffer = []

    for i, line in enumerate(lines):
        m = HEADING_RE.match(line)
        if m:
            flush_buffer(i, m)
        else:
            if line.strip() or buffer:
                buffer.append(line)
    flush_buffer(len(lines), None)

    return found


def detect_footnotes(md_text: str) -> tuple[list[dict], list[dict]]:
    """Find footnote definitions and in-text markers.

    Returns (definitions, markers), each with positional info.
    """
    definitions = []
    for m in FOOTNOTE_DEF_RE.finditer(md_text):
        definitions.append(
            {
                "marker": m.group(1),
                "definition_text": m.group(2).strip(),
                "position": m.start(),
                "line": md_text[: m.start()].count("\n") + 1,
            }
        )

    markers = []
    for m in FOOTNOTE_MARKER_RE.finditer(md_text):
        markers.append(
            {
                "marker": m.group(1),
                "position": m.start(),
                "line": md_text[: m.start()].count("\n") + 1,
            }
        )

    return definitions, markers


def detect_in_text_citations(md_text: str) -> list[dict]:
    """Detect inline italicized or blockquote passages that may be
    quotation units (epigraph-like, but mid-chapter).

    Returns list of {structural_id, text, line}. Conservative: only
    reports blockquotes (`> ...`) of 30..EPIGRAPH_MAX_CHARS chars.

    Note: italic-span detection is intentionally NOT implemented — most
    books use blockquotes or plain text for inline quotations, and a
    reliable italic regex is hard to get right (Markdown `_` vs `*`,
    adjacency to word characters, etc.). If your source uses italics
    for epigraphs, add a heuristic here or rely on the LLM agent
    (prompts/phase2_translate/эпиграф_обработка.md) to catch them at translation time.
    """
    citations = []
    quote_re = re.compile(r"^>\s+(.+)$", re.MULTILINE)
    for m in quote_re.finditer(md_text):
        text = m.group(1).strip()
        if 30 <= len(text) <= EPIGRAPH_MAX_CHARS:
            citations.append(
                {
                    "structural_id": f"inline_quote_line_{md_text[: m.start()].count(chr(10)) + 1}",
                    "kind": "blockquote",
                    "text": text,
                    "line": md_text[: m.start()].count("\n") + 1,
                }
            )
    return citations


def main():
    if len(sys.argv) < 2:
        print("Usage: detect_epigraphs.py <temp_dir>")
        sys.exit(1)

    temp_dir = Path(sys.argv[1])
    md_path = process_dir(temp_dir) / "input.md"
    if not md_path.exists():
        print(f"ERROR: input.md not found in {process_dir(temp_dir)}")
        sys.exit(1)

    text = md_path.read_text(encoding="utf-8")
    epigraphs = detect_epigraphs(text)
    defs, markers = detect_footnotes(text)
    citations = detect_in_text_citations(text)

    defined = {d["marker"] for d in defs}
    used = {m["marker"] for m in markers}
    orphan_defs = sorted(defined - used)
    orphan_markers = sorted(used - defined)

    output = {
        "epigraph_count": len(epigraphs),
        "footnote_definitions": len(defs),
        "footnote_markers": len(markers),
        "inline_quote_count": len(citations),
        "epigraphs": epigraphs,
        "footnotes": {
            "definitions": defs,
            "markers": markers,
            "orphan_definitions": orphan_defs,
            "orphan_markers": orphan_markers,
        },
        "inline_quotations": citations,
        "notes": (
            "Deterministic detection only. All translation decisions "
            "(cultural references, attribution strategy, footnote style) "
            "go to the LLM agent via prompts/phase2_translate/эпиграф_обработка.md."
        ),
    }

    out_path = process_dir(temp_dir) / "structural_units.json"
    from config import atomic_write_json

    atomic_write_json(out_path, output, indent=2, ensure_ascii=False)

    print(f"Epigraphs detected: {len(epigraphs)}")
    print(f"Footnote definitions: {len(defs)}, in-text markers: {len(markers)}")
    print(f"Inline blockquote citations: {len(citations)}")
    if orphan_defs:
        print(f"WARNING: {len(orphan_defs)} orphan footnote definitions: {orphan_defs[:5]}")
    if orphan_markers:
        print(f"WARNING: {len(orphan_markers)} orphan footnote markers: {orphan_markers[:5]}")
    print(f"Output: {out_path}")


if __name__ == "__main__":
    main()
