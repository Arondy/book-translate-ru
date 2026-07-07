#!/usr/bin/env python3
"""Extract neighbour excerpts for chunk translation context.

Usage:
    python3 chunk_context.py <temp_dir> <chunk_file>

Output: prompt-ready context block (empty if no neighbours).
The sub-agent must NOT translate or copy neighbour excerpts; they exist solely
to resolve pronouns, scene continuity, and entity references.
"""

import sys
from pathlib import Path

# Ensure UTF-8 output on Windows (cp1251 default breaks on non-ASCII)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "shared"))


from config import get_config


def process_dir(temp_dir: Path) -> Path:
    """Return process subdirectory (creates if needed)."""
    p = temp_dir / "process"
    p.mkdir(parents=True, exist_ok=True)
    return p


# Tunable: how many characters of each neighbour to inject.
# Loaded from config.toml [chunk_context] section.
_cfg = get_config()
CONTEXT_CHARS = _cfg.get("chunk_context", "context_chars", 300)


def extract_excerpt(text: str, position: str, length: int) -> str:
    """Extract first or last N chars from text, breaking at paragraph boundary."""
    if position == "prev":
        if len(text) <= length:
            return text.strip()
        excerpt = text[-length:].lstrip()
        idx = excerpt.find("\n\n")
        if idx != -1 and idx < len(excerpt) // 2:
            excerpt = excerpt[idx + 2 :]
        return excerpt.strip()
    else:
        if len(text) <= length:
            return text.strip()
        excerpt = text[:length].rstrip()
        idx = excerpt.rfind("\n\n")
        if idx != -1 and idx > len(excerpt) // 2:
            excerpt = excerpt[:idx]
        return excerpt.strip()


def main():
    if len(sys.argv) < 3:
        print("Usage: python3 chunk_context.py <temp_dir> <chunk_file>")
        sys.exit(1)

    temp_dir = Path(sys.argv[1])
    chunk_file = sys.argv[2]

    if not chunk_file.startswith("chunk") or not chunk_file.endswith(".md"):
        print()
        return

    num_str = chunk_file[5:-3]  # "chunk0042.md" -> "0042"
    if not num_str.isdigit():
        print()
        return

    chunk_num = int(num_str)
    prev_num = chunk_num - 1
    next_num = chunk_num + 1

    prev_file = process_dir(temp_dir) / f"chunk{prev_num:04d}.md"
    next_file = process_dir(temp_dir) / f"chunk{next_num:04d}.md"

    prev_text = ""
    next_text = ""

    if prev_file.exists():
        text = prev_file.read_text(encoding="utf-8")
        prev_text = extract_excerpt(text, "prev", CONTEXT_CHARS)

    if next_file.exists():
        text = next_file.read_text(encoding="utf-8")
        next_text = extract_excerpt(text, "next", CONTEXT_CHARS)

    if not prev_text and not next_text:
        print()
        return

    print("--- neighbour context (read-only, do not translate) ---")
    if prev_text:
        print(f"\n[КОНЕЦ ПРЕДЫДУЩЕГО ЧАНКА]:\n{prev_text}\n")
    if next_text:
        print(f"\n[НАЧАЛО СЛЕДУЮЩЕГО ЧАНКА]:\n{next_text}\n")
    print("--- end of neighbour context ---")


if __name__ == "__main__":
    main()
