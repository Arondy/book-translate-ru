"""Final structural quality checks on output.md (post-polish, pre-build).

This script catches SYSTEMATIC MT bugs that survived all translation and
polish passes. It does NOT judge literary quality — only structural
artifacts that are unambiguous signals of MT failure.

Run AFTER step 8 (polish) and BEFORE step 9 (build).

Usage:
    python3 quality_check.py <temp_dir> [--strict]
    python3 quality_check.py <temp_dir> --chunk chunkNNNN [chunkNNNN ...]
        Per-chunk early checks (run right after each chunk is translated
        in step 5): mixed alphabets, English dialogue quotes, untranslated
        headings. Findings are tied to a chunk id for a targeted retry.

Exit codes:
    0 — no issues (or only warnings without --strict)
    1 — issues found (or any warnings with --strict)
    2 — temp_dir not valid (no output.md / no chunk output found)

Checks performed on output.md:
    1. Orphan footnote markers: [^N] without a matching [^N]: definition
    2. Long English leaks: ASCII runs > EN_LEAK_CHARS chars
    3. Per-section ratio outliers (source sizes from manifest.json,
       output sizes from output_chunkNNNN.md)
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
   11. Words mixing Cyrillic and Latin letters ('dошло', 'Gород') — a
       two-alphabet keyboard slip no LLM pass can see; logged, never fixed
    12. Spaced hyphen where an em-dash belongs: "Москва - столица"
    13. MT word order in dates and years: "марта 5-го", "в году 2025"
    14. Clear paired-spelling slips: "в течении часа", "иметь ввиду",
        "в отличии от"
"""

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "shared"))
from common import find_english_leaks, find_mixed_words, process_dir
from config import get_config  # Tunables — loaded from config.toml [quality] section

_cfg = get_config()
EN_LEAK_CHARS = _cfg.get("quality", "en_leak_chars", 80)
RATIO_MIN = _cfg.get("quality", "ratio_min", 0.6)
RATIO_MAX = _cfg.get("quality", "ratio_max", 2.0)


# Individual checks. Each returns list of (line_num, issue_str) tuples.


def check_orphan_footnotes(text: str) -> list[tuple[int, str]]:
    """[^N] in text without matching [^N]: definition."""
    issues = []
    defs = re.findall(r"^\[\^([^\]]+)\]:", text, re.MULTILINE)
    defined = set(defs)
    seen_orphans = set()
    marker_re = re.compile(r"\[\^([^\]]+)\](?!:)")
    for m in marker_re.finditer(text):
        marker = m.group(1)
        if marker in defined or marker in seen_orphans:
            continue
        line = text[: m.start()].count("\n") + 1
        issues.append((line, f"ORPHAN_FOOTNOTE: [^{marker}] has no definition"))
        seen_orphans.add(marker)
    return issues


def check_english_leaks(text: str) -> list[tuple[int, str]]:
    """Runs of ASCII letters/punct > EN_LEAK_CHARS chars (likely untranslated)."""
    issues = []
    for start, _end, snippet in find_english_leaks(text, min_chars=EN_LEAK_CHARS):
        line = text[:start].count("\n") + 1
        sample = snippet[:60].replace("\n", " ")
        issues.append((line, f"ENGLISH_LEAK: '{sample}...'"))
    return issues


def _glossary_target_words(temp_dir: Path) -> list[str]:
    """Words of every glossary `target`, to skip intentionally mixed ones.

    A glossary may hold a deliberately half-Latin name ("eBook-ридер");
    such a word must not be reported as a two-alphabet slip. Missing or
    broken glossary.json is not an error here — just no protected words.
    """
    path = temp_dir / "glossary.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    words: list[str] = []
    for term in data.get("terms", []):
        target = term.get("target")
        if target:
            words.extend(re.findall(r"[^\W\d_]+", target))
    return words


def check_mixed_alphabets(text: str, temp_dir: Path) -> list[tuple[int, str]]:
    """Words mixing Cyrillic and Latin letters ('dошло', 'Gород').

    A two-alphabet keyboard slip: no LLM pass notices it (the model reads
    'gолосом' as 'голосом'). Detection only — the text is never changed.
    Fixing belongs to the translating sub-agent; a systematic pattern
    points at the prompt.
    """
    issues = []
    protected = _glossary_target_words(temp_dir)
    for start, word in find_mixed_words(text, "CYRILLIC", protected):
        line = text[:start].count("\n") + 1
        issues.append((line, f"MIXED_ALPHABET: '{word}'"))
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


_RU_MONTHS = (
    "январ|феврал|март|апрел|ма|июн|июл|август|сентябр|октябр|ноябр|декабр"
)

# Words that make "в течении" a duration (→ "в течение"), not a river flow.
_DURATION_WORDS = (
    "часа|дня|года|недели|месяца|минуты|суток|"
    "часов|дней|лет|недель|месяцев|минут"
)


def check_dash_hyphen(text: str) -> list[tuple[int, str]]:
    """Spaced hyphen used where a Russian em-dash belongs.

    'Москва - столица' must be 'Москва — столица'. Numeric ranges
    ('10 - 20') are skipped here — they belong to the short dash.
    """
    issues = []
    for m in re.finditer(r"(?<=\S) - (?=\S)", text):
        left = text[m.start() - 1]
        right = text[m.end()]
        if left.isdigit() and right.isdigit():
            continue
        line = text[: m.start()].count("\n") + 1
        issues.append((line, f"DASH_HYPHEN: '{m.group(0)}' (should be '—')"))
    return issues


def check_dates_numerals(text: str) -> list[tuple[int, str]]:
    """MT word order in dates and years (see grammar category 17)."""
    issues = []
    patterns = [
        (re.compile(rf"\b(?:{_RU_MONTHS})\w*\s+\d{{1,2}}\b", re.IGNORECASE), "DATE_ORDER"),
        (re.compile(r"\bгоду\s+\d{4}\b", re.IGNORECASE), "YEAR_ORDER"),
    ]
    for pat, name in patterns:
        for m in pat.finditer(text):
            line = text[: m.start()].count("\n") + 1
            issues.append((line, f"{name}: '{m.group(0)}'"))
    return issues


def check_paired_spellings(text: str) -> list[tuple[int, str]]:
    """Clear-cut paired-spelling slips (see grammar category 18)."""
    issues = []
    patterns = [
        (re.compile(rf"\bв течении\s+(?:{_DURATION_WORDS})\b", re.IGNORECASE),
         "PAIRED_SPELLING", "'в течении' → 'в течение'"),
        (re.compile(r"\bиме\w+\s+ввиду\b", re.IGNORECASE),
         "PAIRED_SPELLING", "'иметь ввиду' → 'иметь в виду'"),
        (re.compile(r"\bв отличии от\b", re.IGNORECASE),
         "PAIRED_SPELLING", "'в отличии от' → 'в отличие от'"),
    ]
    for pat, name, hint in patterns:
        for m in pat.finditer(text):
            line = text[: m.start()].count("\n") + 1
            issues.append((line, f"{name}: '{m.group(0)}' ({hint})"))
    return issues


def check_per_chapter_ratio(temp_dir: Path) -> list[tuple[int, str]]:
    """Per-section ratio, computed from the per-chunk outputs.

    The glued output.md carries RUSSIAN headings while manifest.json keeps
    the ENGLISH section titles, so matching by heading text silently found
    nothing and a chapter truncated to 5% passed. Here we sum the manifest
    source sizes and the real `output_chunkNNNN.md` sizes per section_title
    — no heading matching involved.
    """
    issues = []
    manifest_path = process_dir(temp_dir) / "manifest.json"
    if not manifest_path.exists():
        return issues

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return issues

    section_src: dict[str, int] = {}
    section_out: dict[str, int] = {}
    for chunk_id, info in manifest.get("chunks", {}).items():
        title = info.get("section_title") or "(front matter)"
        if title == "(front matter)":
            continue
        out_path = process_dir(temp_dir) / f"output_{chunk_id}.md"
        if not out_path.exists():
            continue
        src_size = info.get("size", 0)
        if src_size == 0:
            continue
        section_src[title] = section_src.get(title, 0) + src_size
        section_out[title] = section_out.get(title, 0) + len(out_path.read_text(encoding="utf-8").strip())

    for title, src_size in section_src.items():
        out_size = section_out.get(title, 0)
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


# Per-chunk early checks (step 5, right after each chunk is translated)
#
# Deterministic slips that used to be caught only on the glued output.md
# at step 8.6. Running them per chunk ties every finding to a chunk id,
# so a retry gets an exact problem list.


_PER_CHUNK_CHECKS = (
    ("mixed_alphabets", check_mixed_alphabets),
    ("english_dialogue_quotes", check_english_dialogue_quotes),
    ("untranslated_chapter_headings", check_untranslated_chapter_headings),
    ("dash_hyphen", check_dash_hyphen),
    ("dates_numerals", check_dates_numerals),
    ("paired_spellings", check_paired_spellings),
)


def run_chunk_checks(temp_dir: Path, chunk_ids: list[str]) -> int:
    """Run the per-chunk early checks on output_chunkNNNN.md files.

    Returns 0 when clean, 1 when any issue is found, 2 when no output
    file was found at all.
    """
    pdir = process_dir(temp_dir)
    any_file = False
    total = 0
    for raw in chunk_ids:
        cid = raw if raw.startswith("chunk") else f"chunk{raw}"
        out_path = pdir / f"output_{cid}.md"
        if not out_path.exists():
            print(f"ERROR: {out_path.name} not found", file=sys.stderr)
            continue
        any_file = True
        text = out_path.read_text(encoding="utf-8")
        chunk_issues: list[tuple[str, str]] = []
        for name, fn in _PER_CHUNK_CHECKS:
            if name == "mixed_alphabets":
                found = fn(text, temp_dir)
            else:
                found = fn(text)
            for _line, msg in found:
                chunk_issues.append((name, msg))
        total += len(chunk_issues)
        if chunk_issues:
            print(f"{cid}: {len(chunk_issues)} issue(s)")
            for name, msg in chunk_issues:
                print(f"  [{name}] {msg}")
        else:
            print(f"{cid}: OK")

    if not any_file:
        return 2
    if total:
        print(f"\nEARLY CHECK FAILED: {total} issue(s) across {len(chunk_ids)} chunk(s).")
        return 1
    print(f"\nEARLY CHECK OK: {len(chunk_ids)} chunk(s) clean.")
    return 0


# Main


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="quality_check.py",
        description="Structural quality checks on output.md or on single chunks.",
    )
    parser.add_argument("temp_dir")
    parser.add_argument("--strict", action="store_true", help="fail on warnings too")
    parser.add_argument(
        "--chunk",
        nargs="+",
        metavar="chunkNNNN",
        help="run only the per-chunk early checks on these chunks "
        "(mixed alphabets, English dialogue quotes, untranslated headings, "
        "dash/hyphen, dates/numerals, paired spellings)",
    )
    return parser


def main():
    args = build_parser().parse_args()
    temp_dir = Path(args.temp_dir)

    if args.chunk:
        sys.exit(run_chunk_checks(temp_dir, args.chunk))

    strict = args.strict

    output_path = temp_dir / "output.md"
    if not output_path.exists():
        print(f"ERROR: output.md not found in {temp_dir}", file=sys.stderr)
        sys.exit(2)

    text = output_path.read_text(encoding="utf-8")

    all_issues: list[tuple[str, int, str]] = []  # (check_name, line, msg)

    checks = [
        ("orphan_footnotes", check_orphan_footnotes(text)),
        ("english_leaks", check_english_leaks(text)),
        ("mixed_alphabets", check_mixed_alphabets(text, temp_dir)),
        ("garbage_dashes", check_garbage_dashes(text)),
        ("js_artifacts", check_js_artifacts(text)),
        ("code_fences", check_code_fences(text)),
        ("empty_headings", check_empty_headings(text)),
        ("doubled_punct", check_doubled_punct(text)),
        ("leftover_placeholders", check_leftover_placeholders(text)),
        ("english_dialogue_quotes", check_english_dialogue_quotes(text)),
        ("untranslated_chapter_headings", check_untranslated_chapter_headings(text)),
        ("dash_hyphen", check_dash_hyphen(text)),
        ("dates_numerals", check_dates_numerals(text)),
        ("paired_spellings", check_paired_spellings(text)),
        ("per_chapter_ratio", check_per_chapter_ratio(temp_dir)),
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
