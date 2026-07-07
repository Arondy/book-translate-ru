#!/usr/bin/env python3
"""Detect structural narrator boundaries (deterministic).

This script ONLY identifies chunks likely belonging to the same narrative
arc based on heading patterns ("Interlude 1", "Chapter 23", "Prologue",
"Part II"). It does NOT determine who the narrator is — that's an LLM
task (see prompts/phase2_translate/определение_рассказчика.md).

Outputs:
    <temp_dir>/narrator_hints.json

Usage:
    python3 narrator_marker.py <temp_dir>
"""

import json
import re
import sys
from pathlib import Path

# Ensure UTF-8 output on Windows (cp1251 default breaks on non-ASCII)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "shared"))


def process_dir(temp_dir: Path) -> Path:
    """Return process subdirectory (creates if needed)."""
    p = temp_dir / "process"
    p.mkdir(parents=True, exist_ok=True)
    return p


# Patterns that suggest a chapter / arc grouping. Order matters: more
# specific patterns first.
#
# Numeric forms covered:
#   - Arabic digits: 1, 2, 23
#   - Roman numerals (lower/upper): I, II, iv, XL
#   - English ordinal words: one, two, ..., ten, first, second, ..., tenth
#   - Russian numerals (lower/upper): пятая, пятой
#
# Keep list conservative — over-matching creates false arc boundaries.
_NUM_WORD = (
    r"(?:one|two|three|four|five|six|seven|eight|nine|ten|"
    r"first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|"
    r"one|two|three|four|five|six|seven|eight|nine|ten)"
)

PATTERNS: list[tuple[re.Pattern, str]] = [
    (
        re.compile(r"^interlude\s*([\divxlcm]+|[a-z]\b|" + _NUM_WORD + r")", re.IGNORECASE),
        "interlude",
    ),
    (re.compile(r"^prologue\b", re.IGNORECASE), "prologue"),
    (re.compile(r"^epilogue\b", re.IGNORECASE), "epilogue"),
    (re.compile(r"^epigraphs?\b", re.IGNORECASE), "epigraph_only"),
    (re.compile(r"^part\s*([\divxlcm]+|" + _NUM_WORD + r")", re.IGNORECASE), "part"),
    (re.compile(r"^глава\s*(\d+|[а-яё]+)", re.IGNORECASE), "chapter"),
    (re.compile(r"^часть\s*(\d+|[а-яё]+)", re.IGNORECASE), "part"),
    (
        re.compile(r"^chapter\s*(\d+|[a-z]+|" + _NUM_WORD + r")", re.IGNORECASE),
        "chapter",
    ),
    (re.compile(r"^book\s*([\divxlcm]+|" + _NUM_WORD + r")", re.IGNORECASE), "book"),
]


def match_arc(label: str) -> tuple[str, str] | None:
    """Match a heading title to a (kind, group_id) pair, if possible."""
    s = label.strip()
    for pat, kind in PATTERNS:
        m = pat.match(s)
        if m:
            return (kind, m.group(1).lower() if m.lastindex else "")
    return None


def main():
    if len(sys.argv) < 2:
        print("Usage: narrator_marker.py <temp_dir>")
        sys.exit(1)

    temp_dir = Path(sys.argv[1])
    sections_path = process_dir(temp_dir) / "chunk_sections.json"
    if not sections_path.exists():
        print(f"ERROR: chunk_sections.json not found in {process_dir(temp_dir)}")
        print("       Run convert.py first.")
        sys.exit(1)

    sections = json.loads(sections_path.read_text(encoding="utf-8"))

    arcs: list[dict] = []
    current_arc: dict = {"label": "main", "kind": "unknown", "chunks": []}

    for chunk_id in sorted(sections.keys()):
        title = sections[chunk_id]
        m = match_arc(title)
        if m:
            kind, group_id = m
            arc_label = f"{kind}_{group_id}" if group_id else kind
        else:
            arc_label = "main"
            kind = "unknown"

        if not current_arc["chunks"]:
            # first chunk of book — start arc
            current_arc["label"] = arc_label
            current_arc["kind"] = kind
        elif arc_label != current_arc["label"]:
            # transition — close current, open new
            arcs.append(current_arc)
            current_arc = {"label": arc_label, "kind": kind, "chunks": [chunk_id]}
            continue

        current_arc["chunks"].append(chunk_id)

    if current_arc["chunks"]:
        arcs.append(current_arc)

    # Build a per-chunk lookup (so the agent doesn't have to scan)
    chunk_to_arc = {}
    for arc in arcs:
        for cid in arc["chunks"]:
            chunk_to_arc[cid] = arc["label"]

    output = {
        "arc_count": len(arcs),
        "arcs": arcs,
        "chunk_to_arc": chunk_to_arc,
        "notes": (
            "STRUCTURAL HINTS only. These are groupings by heading patterns "
            "(e.g., all chunks under heading 'Interlude 5' share an arc label). "
            "The actual narrator identity (Kaladin, Shallan, Wit, …) is decided "
            "by the translation sub-agent via prompts/phase2_translate/определение_рассказчика.md, "
            "which fills in `narrator_identification` in output_chunk*.meta.json."
        ),
    }

    out_path = process_dir(temp_dir) / "narrator_hints.json"
    from config import atomic_write_json

    atomic_write_json(out_path, output, indent=2, ensure_ascii=False)

    print(f"Detected {len(arcs)} narrative arcs:")
    for arc in arcs:
        print(f"  {arc['label']:20s} ({arc['kind']:10s}) -> {len(arc['chunks']):3d} chunks")
    print(f"Output: {out_path}")


if __name__ == "__main__":
    main()
