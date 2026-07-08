#!/usr/bin/env python3
"""Final structural quality checks on output.md (post-polish, pre-build).

This script catches SYSTEMATIC MT bugs that survived all translation and
polish passes. It does NOT judge literary quality — only structural
artifacts that are unambiguous signals of MT failure.

Run AFTER step 8 (polish) and BEFORE step 9 (build).

Usage:
    python3 quality_check.py <temp_dir> [--strict]

Exit codes:
    0 — no issues (or only warnings without --strict)
    1 — issues found (or any warnings with --strict)
    2 — temp_dir not valid (no output.md)

Checks performed on output.md:
    1. Orphan footnote markers: [^N] without a matching [^N]: definition
    2. Long English leaks: ASCII runs > EN_LEAK_CHARS chars
    3. Per-chapter ratio outliers (uses chunk_sections.json if present)
    4. Garbage em-dash sequences: "— — —", "---", "— —", "—–—"
    5. JSON/JS leakage artifacts: "[object Object]", "[undefined]",
       "undefined", "null" (as standalone word)
    6. Markdown structural breakage: unclosed code fences (``` count is odd)
    7. Empty headings: lines like "## " or "# " with nothing after
    8. Doubled punctuation artifacts: ".,", ",.", "?.", "!."
    9. Leftover placeholder tokens: "{baseDir}", "{TERM_TABLE}",
       "{NEIGHBOR_CONTEXT}", "{CUSTOM_INSTRUCTIONS}", "{TARGET_LANGUAGE}"
   10. English dialogue quotes: "...said X" patterns where English double
       quotes wrap dialogue (should be — ... — X in Russian)
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
from common import process_dir
from config import get_config  # Tunables — loaded from config.toml [quality] section

_cfg = get_config()
EN_LEAK_CHARS = _cfg.get("quality", "en_leak_chars", 80)
RATIO_MIN = _cfg.get("quality", "ratio_min", 0.6)
RATIO_MAX = _cfg.get("quality", "ratio_max", 2.0)


# ─────────────────────────────────────────────────────────────────────
# Individual checks. Each returns list of (line_num, issue_str) tuples.
# ─────────────────────────────────────────────────────────────────────


def check_orphan_footnotes(text: str) -> list[tuple[int, str]]:
    """[^N] in text without matching [^N]: definition."""
    issues = []
    markers = re.findall(r"\[\^([^\]]+)\]", text)
    defs = re.findall(r"^\[\^([^\]]+)\]:", text, re.MULTILINE)
    defined = set(defs)
    seen_orphans = set()
    for m in markers:
        if m not in defined and m not in seen_orphans:
            # Find line number
            pattern = re.compile(r"\[\^" + re.escape(m) + r"\](?!:)")
            match = pattern.search(text)
            if match:
                line = text[: match.start()].count("\n") + 1
                issues.append((line, f"ORPHAN_FOOTNOTE: [^{m}] has no definition"))
                seen_orphans.add(m)
    return issues


def check_english_leaks(text: str) -> list[tuple[int, str]]:
    """Runs of ASCII letters/punct > EN_LEAK_CHARS chars (likely untranslated)."""
    issues = []
    pattern = re.compile(r"[A-Za-z][A-Za-z\s.,;:'\"!?\-\(\)\[\]]{" + str(EN_LEAK_CHARS - 1) + r",}")
    for m in pattern.finditer(text):
        snippet = m.group(0)
        # Require at least one full English word > 3 letters
        if re.search(r"\b[A-Za-z]{4,}\b", snippet):
            line = text[: m.start()].count("\n") + 1
            sample = snippet[:60].replace("\n", " ")
            issues.append((line, f"ENGLISH_LEAK: '{sample}...'"))
    return issues


def check_garbage_dashes(text: str) -> list[tuple[int, str]]:
    r"""Sequences like '— — —', '--- ---' (3+ SEPARATOR GROUPS with whitespace).

    IMPORTANT: Only flag 3+ dash/em-dash SEPARATOR GROUPS with WHITESPACE
    between them. Two patterns:
    1. Single-char separators with spaces: '— — —', '- - -'
    2. Multi-char separator groups with spaces: '--- ---', '---- ----'

    Inline '---' between words (e.g. 'Minsk---Dink---Wink',
    'He paused---then spoke again') is a legitimate em-dash from the
    source EPUB and should NOT be flagged — translate/polish pass
    converts inline '---' to '—' (see translate_chunk.md rule 16).
    """
    issues = []
    # Pattern 1: single-char separators with spaces: '— — —', '- - -'
    # Requires \s+ (whitespace) between repeated same single chars.
    pattern1 = re.compile(r"([\u2014\u2013\-])(\s+\1){2,}")
    # Pattern 2: multi-char separator groups with spaces: '--- ---', '---- ----'
    # Each group is 2+ same dash chars, separated by whitespace.
    pattern2 = re.compile(r"([\u2014\u2013\-]{2,})(\s+\1){1,}")
    for m in list(pattern1.finditer(text)) + list(pattern2.finditer(text)):
        line = text[: m.start()].count("\n") + 1
        issues.append((line, f"GARBAGE_DASHES: '{m.group(0)}'"))
    # Deduplicate by position (patterns may overlap)
    seen = set()
    deduped = []
    for line, msg in issues:
        if (line, msg) not in seen:
            seen.add((line, msg))
            deduped.append((line, msg))
    return deduped


def check_js_artifacts(text: str) -> list[tuple[int, str]]:
    """[object Object], [undefined], etc."""
    issues = []
    patterns = [
        (r"\[object Object\]", "[object Object]"),
        (r"\[undefined\]", "[undefined]"),
        (r"(?<![\w-])undefined(?![\w-])", "undefined"),
        (r"(?<![\w-])null(?![\w-])", "null"),
        (r"\[Function\]", "[Function]"),
        (r"NaN", "NaN"),
    ]
    for pat, name in patterns:
        for m in re.finditer(pat, text):
            line = text[: m.start()].count("\n") + 1
            issues.append((line, f"JS_ARTIFACT: '{name}'"))
    return issues


def check_code_fences(text: str) -> list[tuple[int, str]]:
    """Odd number of ``` fences = unclosed code block."""
    issues = []
    fence_count = len(re.findall(r"^```", text, re.MULTILINE))
    if fence_count % 2 != 0:
        # Find the last fence
        matches = list(re.finditer(r"^```", text, re.MULTILINE))
        if matches:
            last = matches[-1]
            line = text[: last.start()].count("\n") + 1
            issues.append((line, f"UNCLOSED_CODE_FENCE: {fence_count} fences (odd)"))
    return issues


def check_empty_headings(text: str) -> list[tuple[int, str]]:
    """Lines like '# ' or '## ' with no heading text."""
    issues = []
    for m in re.finditer(r"^(#{1,6})\s*$", text, re.MULTILINE):
        line = text[: m.start()].count("\n") + 1
        issues.append((line, f"EMPTY_HEADING: '{m.group(0)}'"))
    return issues


def check_doubled_punct(text: str) -> list[tuple[int, str]]:
    """Artifacts like '.,', ',.', '?.', '!.' (MT punctuation errors).

    IMPORTANT: Does NOT flag:
    - `..` inside relative paths like `../images/foo.png` (Markdown image
      links). These are legitimate.
    - `...` ellipsis (3+ dots). Legitimate Russian punctuation.
    """
    issues = []
    pattern = re.compile(r"(?<!\.)\.(?:,|\.)(?!\.)|,(?:\.)|(?<![?!])\?(?:\.)|(?<![?!])!(?:\.)")
    for m in pattern.finditer(text):
        match_str = m.group(0)
        # Filter false positives:
        # '..' followed by '/' or preceded by '/' → part of a relative path
        if match_str == "..":
            end = m.end()
            start = m.start()
            if (end < len(text) and text[end] == "/") or (start > 0 and text[start - 1] == "/"):
                continue
        line = text[: m.start()].count("\n") + 1
        issues.append((line, f"DOUBLED_PUNCT: '{match_str}'"))
    return issues


def check_leftover_placeholders(text: str) -> list[tuple[int, str]]:
    """Unresolved {baseDir}, {TERM_TABLE}, etc. — orchestrator bug."""
    issues = []
    placeholders = [
        "{baseDir}",
        "{TERM_TABLE}",
        "{NEIGHBOR_CONTEXT}",
        "{CUSTOM_INSTRUCTIONS}",
        "{TARGET_LANGUAGE}",
        "{TEMP_DIR}",
    ]
    for ph in placeholders:
        idx = 0
        while True:
            found = text.find(ph, idx)
            if found == -1:
                break
            line = text[:found].count("\n") + 1
            issues.append((line, f"LEFTOVER_PLACEHOLDER: '{ph}'"))
            idx = found + 1
    return issues


def check_english_dialogue_quotes(text: str) -> list[tuple[int, str]]:
    """English straight double quotes wrapping dialogue — MT artifact.

    Russian prose uses em-dash for dialogue, not quotes. We don't flag
    every " (which could be inch mark or code), only patterns where an
    English straight double quote is followed by dialogue-like content
    and an attribution verb.

    Conservative: only flag clear "X," said Y / "X!" said Y patterns.
    """
    issues = []
    # Pattern: "..." followed by said/exclaimed/asked/etc (English or
    # transliterated). Looks for ASCII straight double-quotes around what
    # looks like dialogue with attribution.
    # We only flag ASCII " — Russian uses ёлочки «...» or em-dash — for
    # quotes. ASCII " in body text is almost always MT leftover.
    pattern = re.compile(
        r'"[^"\n]{2,200}"[,!?\.]?\s*'
        r"(?:said|exclaimed|asked|replied|whispered|shouted|muttered|"
        r"сказал|сказала|воскликнул|воскликнула|спросил|спросила|"
        r"ответил|ответила|прошептал|прошептала|крикнул|пробормотал)",
        re.IGNORECASE,
    )
    for m in pattern.finditer(text):
        line = text[: m.start()].count("\n") + 1
        snippet = m.group(0)[:80].replace("\n", " ")
        issues.append((line, f"ENGLISH_DIALOGUE_QUOTES: '{snippet}...'"))
    return issues


def check_untranslated_chapter_headings(text: str) -> list[tuple[int, str]]:
    """Chapter headings left as Russian number-words (Один, Два, ...).

    Sub-agents should translate "One" -> "Глава 1", "Two" -> "Глава 2",
    etc. (see translate_chunk.md rule 15). If they missed it, the
    heading remains "Один" / "Два" / etc. — this check flags those.
    """
    issues = []
    # Russian number-words that are commonly left as chapter headings
    # when the agent forgot rule 15.
    number_words = [
        "Один",
        "Два",
        "Три",
        "Четыре",
        "Пять",
        "Шесть",
        "Семь",
        "Восемь",
        "Девять",
        "Десять",
        "Одиннадцать",
        "Двенадцать",
        "Тринадцать",
        "Четырнадцать",
        "Пятнадцать",
        "Шестнадцать",
        "Семнадцать",
        "Восемнадцать",
        "Девятнадцать",
        "Двадцать",
    ]
    # Match: heading (# or ##) + number-word + end of line
    pattern = re.compile(r"^(#{1,3})\s+(" + "|".join(number_words) + r")\s*$", re.MULTILINE)
    for m in pattern.finditer(text):
        line = text[: m.start()].count("\n") + 1
        issues.append(
            (
                line,
                f"UNTRANSLATED_HEADING: '{m.group(0)}' (should be 'Глава N' — see translate_chunk.md rule 15)",
            )
        )
    return issues


def check_per_chapter_ratio(temp_dir: Path, output_text: str) -> list[tuple[int, str]]:
    """If chunk_sections.json exists, check per-chapter ratio.

    Splits output_text by H1/H2 headings and compares each section's
    length to the corresponding source section length.
    """
    issues = []
    sections_path = process_dir(temp_dir) / "chunk_sections.json"
    if not sections_path.exists():
        return issues

    try:
        sections = json.loads(sections_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return issues

    # Group chunks by section_title
    section_to_chunks: dict[str, list[str]] = {}
    for chunk_id, title in sections.items():
        section_to_chunks.setdefault(title, []).append(chunk_id)

    # For each section, sum source chunk sizes from manifest
    manifest_path = process_dir(temp_dir) / "manifest.json"
    if not manifest_path.exists():
        return issues
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return issues

    # Find section in output_text by heading match
    heading_lines = {}
    for m in re.finditer(r"^(#{1,2})\s+(.+?)\s*$", output_text, re.MULTILINE):
        heading_lines[m.group(2).strip()] = m.start()

    for title, chunk_ids in section_to_chunks.items():
        # Skip empty / front matter
        if not title or title == "(front matter)":
            continue
        src_size = sum(manifest.get("chunks", {}).get(cid, {}).get("size", 0) for cid in chunk_ids)
        if src_size == 0:
            continue
        # Find corresponding heading in output
        matched_start = None
        for h_title, h_start in heading_lines.items():
            if h_title == title or title in h_title or h_title in title:
                matched_start = h_start
                break
        if matched_start is None:
            continue
        # Find next heading after matched_start
        next_heading = None
        for m in re.finditer(r"^#{1,2}\s+", output_text[matched_start + 1 :], re.MULTILINE):
            next_heading = matched_start + 1 + m.start()
            break
        section_text = output_text[matched_start:next_heading] if next_heading else output_text[matched_start:]
        out_size = len(section_text.strip())
        if out_size == 0:
            continue
        ratio = out_size / src_size
        if ratio < RATIO_MIN:
            issues.append(
                (
                    0,
                    f"RATIO_TOO_LOW for '{title}': {ratio:.2f} (src={src_size}, out={out_size})",
                )
            )
        elif ratio > RATIO_MAX:
            issues.append(
                (
                    0,
                    f"RATIO_TOO_HIGH for '{title}': {ratio:.2f} (src={src_size}, out={out_size})",
                )
            )

    return issues


# ─────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    temp_dir = Path(sys.argv[1])
    strict = "--strict" in sys.argv

    output_path = temp_dir / "output.md"
    if not output_path.exists():
        print(f"ERROR: output.md not found in {temp_dir}", file=sys.stderr)
        sys.exit(2)

    text = output_path.read_text(encoding="utf-8")

    all_issues: list[tuple[str, int, str]] = []  # (check_name, line, msg)

    checks = [
        ("orphan_footnotes", check_orphan_footnotes(text)),
        ("english_leaks", check_english_leaks(text)),
        ("garbage_dashes", check_garbage_dashes(text)),
        ("js_artifacts", check_js_artifacts(text)),
        ("code_fences", check_code_fences(text)),
        ("empty_headings", check_empty_headings(text)),
        ("doubled_punct", check_doubled_punct(text)),
        ("leftover_placeholders", check_leftover_placeholders(text)),
        ("english_dialogue_quotes", check_english_dialogue_quotes(text)),
        ("untranslated_chapter_headings", check_untranslated_chapter_headings(text)),
        ("per_chapter_ratio", check_per_chapter_ratio(temp_dir, text)),
    ]

    for name, issues in checks:
        for line, msg in issues:
            all_issues.append((name, line, msg))

    # Report
    if not all_issues:
        print("QUALITY OK: output.md passes all structural checks.")
        sys.exit(0)

    print(f"QUALITY ISSUES: {len(all_issues)} found in output.md:")
    by_check: dict[str, list] = {}
    for name, line, msg in all_issues:
        by_check.setdefault(name, []).append((line, msg))

    for name, items in by_check.items():
        print(f"\n  [{name}] — {len(items)} issue(s):")
        for line, msg in items[:10]:
            print(f"    line {line}: {msg}")
        if len(items) > 10:
            print(f"    ... and {len(items) - 10} more")

    if strict:
        sys.exit(1)
    else:
        print("\n(warnings only — pass --strict to fail on these)")
        sys.exit(0)


if __name__ == "__main__":
    main()
