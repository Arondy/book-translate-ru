#!/usr/bin/env python3
"""Glossary management for book-translate-ru skill.

Usage:
    python3 glossary.py count-frequencies <temp_dir>
    python3 glossary.py print-terms-for-chunk <temp_dir> <chunk_file>
    python3 glossary.py validate-manifest <temp_dir>
    python3 glossary.py reset-run-state <temp_dir> [--prune-zero-freq]
    python3 glossary.py find-duplicates <temp_dir>

Commands:
    count-frequencies      Recompute term frequencies from chunks.
    print-terms-for-chunk  Print the per-chunk term table for a chunk.
    validate-manifest      Check that all chunks have matching outputs.
    reset-run-state        Strip stale per-run metadata (applied_meta_hashes,
                           evidence_refs, notes) from glossary.json. USE THIS
                           when starting a FRESH re-run of the same book with
                           a glossary carried over from a previous run —
                           otherwise merge_meta may consider old chunks as
                           "already applied" and skip them silently.

                           With --prune-zero-freq: also remove terms whose
                           frequency is 0 (i.e. they don't appear in this
                           book). Useful when carrying over a series-wide
                           glossary to a new book in the series.

    find-duplicates        Scan glossary.json for near-duplicate terms that
                           merge_meta's exact-match dedup missed. Reports:
                           - Case-insensitive duplicates (same source, diff case)
                           - Singular/plural pairs (Lens vs Lenses)
                           - Apostrophe-s variants (Tracker vs Tracker's)
                           - Substring matches (Janci is a token-subset of
                             Janci Patterson)
                           - Short-name/long-name pairs (Sing vs Sing Sing,
                             Attica vs Attica Smedry) — common cause of
                             duplicate character translations

                           Non-destructive — only REPORTS, does not merge.
                           Use after step 6 (merge) to catch terms that
                           sub-agents added as "new" but which are actually
                           variants of existing glossary entries.
"""

import hashlib
import json
import re
import sys
from pathlib import Path

# Ensure UTF-8 output on Windows (cp1251 default breaks on non-ASCII)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "shared"))


from config import get_config


def process_dir(temp_dir: Path) -> Path:
    """Return process subdirectory (creates if needed).

    Layout:
      <temp_dir>/             - human-facing (glossary.json, voice book, book.*, reports)
      <temp_dir>/process/     - machine-facing (chunks, metas, manifests, configs)
    """
    p = temp_dir / "process"
    p.mkdir(parents=True, exist_ok=True)
    return p


def stable_hash(obj) -> str:
    """Deterministic hash across Python processes (unlike built-in hash()).

    Built-in hash() for strings is randomized via PYTHONHASHSEED between
    process runs, which would make run_state.json entity_hashes unusable
    for resume: every new orchestrator session would see 'glossary changed'
    for every chunk and re-translate the whole book.
    """
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


# Word boundary that works for names with apostrophes (O'Connor) and
# hyphens (Wit-Bit). Standard \b breaks on these because ' and - are not
# \w, so the boundary fires mid-name. We treat any alphanumeric char (incl.
# Cyrillic) as "inside a word" and everything else as a boundary.
_BOUNDARY_START = r"(?<![A-Za-zА-Яа-я0-9])"
_BOUNDARY_END = r"(?![A-Za-zА-Яа-я0-9])"


def surface_in_text(surface: str, text: str) -> bool:
    """True if surface appears as a whole word in text.

    For surfaces of length <= 2 we fall back to plain substring match
    because word-boundary regex on a 1-char name is noisy either way.
    """
    if not surface:
        return False
    if len(surface) <= 2:
        return surface in text
    pattern = _BOUNDARY_START + re.escape(surface) + _BOUNDARY_END
    return bool(re.search(pattern, text, re.IGNORECASE))


GLOSSARY_FILE = "glossary.json"  # lives in temp_dir root (human-facing)
MANIFEST_FILE = "manifest.json"  # lives in process/

# Quality thresholds — loaded from config.toml [quality] section
_cfg = get_config()
RATIO_MIN = _cfg.get("quality", "ratio_min", 0.6)
RATIO_MAX = _cfg.get("quality", "ratio_max", 2.0)
EN_LEAK_CHARS = _cfg.get("quality", "en_leak_chars", 80)


def load_glossary(temp_dir: Path) -> dict:
    path = temp_dir / GLOSSARY_FILE
    if not path.exists():
        # Default high_frequency_top_n from config.toml [glossary] section
        _top_n = get_config().get("glossary", "high_frequency_top_n", 20)
        return {
            "version": 2,
            "terms": [],
            "high_frequency_top_n": _top_n,
            "applied_meta_hashes": {},
        }
    from config import read_json_safe

    try:
        data = read_json_safe(path)
    except (json.JSONDecodeError, OSError):
        _top_n = get_config().get("glossary", "high_frequency_top_n", 20)
        return {
            "version": 2,
            "terms": [],
            "high_frequency_top_n": _top_n,
            "applied_meta_hashes": {},
        }
    if data.get("version") == 2:
        return data
    _top_n = get_config().get("glossary", "high_frequency_top_n", 20)
    return {
        "version": 2,
        "terms": [],
        "high_frequency_top_n": _top_n,
        "applied_meta_hashes": {},
    }


def save_glossary(temp_dir: Path, glossary: dict):
    """Save glossary.json using Windows-safe atomic write."""
    from config import atomic_write_json

    path = temp_dir / GLOSSARY_FILE
    atomic_write_json(path, glossary, indent=2, ensure_ascii=False)


def term_surface_forms(term: dict) -> list[str]:
    forms = [term["source"].lower()]
    for alias in term.get("aliases", []):
        forms.append(alias.lower())
    return forms


def count_frequencies(temp_dir: Path):
    glossary = load_glossary(temp_dir)
    chunks = sorted(process_dir(temp_dir).glob("chunk*.md"))

    for term in glossary["terms"]:
        term["frequency"] = 0
        surfaces = term_surface_forms(term)
        for chunk_path in chunks:
            text = chunk_path.read_text(encoding="utf-8").lower()
            for surface in surfaces:
                if surface_in_text(surface, text):
                    term["frequency"] += 1
                    break

    save_glossary(temp_dir, glossary)
    print(f"Frequencies updated: {len(glossary['terms'])} terms")
    for t in glossary["terms"]:
        print(f"  {t['source']}: {t['frequency']}")


def print_terms_for_chunk(temp_dir: Path, chunk_file: str):
    glossary = load_glossary(temp_dir)
    if not glossary["terms"]:
        print("")  # empty output = no terms
        return

    chunk_path = process_dir(temp_dir) / chunk_file
    if not chunk_path.exists():
        print(f"ERROR: chunk not found: {chunk_path}", file=sys.stderr)
        sys.exit(1)

    text = chunk_path.read_text(encoding="utf-8").lower()

    chunk_terms = []
    top_n = glossary.get("high_frequency_top_n", 20)

    # Sort terms by frequency descending. Use enumerate for O(n) index lookup
    # (list.index() is O(n) per call and buggy for duplicate-valued dicts).
    sorted_terms = sorted(glossary["terms"], key=lambda t: t.get("frequency", 0), reverse=True)

    for idx, term in enumerate(sorted_terms):
        surfaces = term_surface_forms(term)
        appears_in_chunk = any(surface_in_text(s, text) for s in surfaces)
        is_high_freq = idx < top_n

        if appears_in_chunk or is_high_freq:
            aliases_str = ", ".join(term.get("aliases", []))
            chunk_terms.append(
                {
                    "source": term["source"],
                    "alias": aliases_str,
                    "target": term["target"],
                }
            )

    if not chunk_terms:
        print("")
        return

    # Print as markdown table
    print("| Оригинал | Альтернативы | Перевод |")
    print("|----------|-------------|---------|")
    for ct in chunk_terms:
        print(f"| {ct['source']} | {ct['alias']} | {ct['target']} |")


# ─────────────────────────────────────────────────────────────────────
# Content sanity (called by validate-manifest after structural checks)
# ─────────────────────────────────────────────────────────────────────


def find_english_leaks(text: str, min_chars: int = EN_LEAK_CHARS) -> list[tuple[int, int, str]]:
    """Find runs of pure-ASCII alphabetic characters likely to be untranslated
    English text. Returns list of (start, end, snippet).
    """
    leaks = []
    pattern = re.compile(r"[A-Za-z][A-Za-z\s.,;:'\"!?\-\(\)\[\]]{" + str(min_chars - 1) + r",}")
    for m in pattern.finditer(text):
        # Filter out cases that look like punctuation/code/markdown directives
        snippet = m.group(0)
        # require at least one full English word > 3 letters
        if re.search(r"\b[A-Za-z]{4,}\b", snippet):
            leaks.append((m.start(), m.end(), snippet))
    return leaks


def content_sanity_report(temp_dir: Path, manifest: dict) -> dict:
    """For each manifest entry, compute ratio + leak detection.

    Returns a per-chunk dict with empty list entries for OK and list entries
    of issue strings for chunks that triggered warnings.
    """
    pdir = process_dir(temp_dir)
    from config import read_text_safe

    report: dict[str, list[str]] = {}
    for chunk_id, info in manifest.get("chunks", {}).items():
        src_path = pdir / f"{chunk_id}.md"
        out_path = pdir / f"output_{chunk_id}.md"
        issues: list[str] = []
        if not src_path.exists() or not out_path.exists():
            continue

        src_text = read_text_safe(src_path)
        out_text = read_text_safe(out_path)

        src_len = len(src_text.strip())
        out_len = len(out_text.strip())
        if src_len == 0:
            continue

        ratio = out_len / src_len
        if ratio < RATIO_MIN:
            issues.append(f"RATIO_TOO_LOW: {ratio:.2f} (< {RATIO_MIN})")
        elif ratio > RATIO_MAX:
            issues.append(f"RATIO_TOO_HIGH: {ratio:.2f} (> {RATIO_MAX})")

        leaks = find_english_leaks(out_text)
        if leaks:
            sample = leaks[0][2][:60]
            issues.append(f"ENGLISH_LEAK[{len(leaks)}]: '{sample}...'")

        report[chunk_id] = issues
    return report


def validate_manifest(temp_dir: Path):
    """Structural + content sanity check.

    Exits non-zero if:
    - manifest missing
    - any source chunk is missing
    - any output is missing or empty
    - any source hash drifted from manifest
    - any chunk has ratio outliers (warning by default; --strict to fail)
    - any chunk has suspicious English leaks (warning by default)
    """
    strict = "--strict" in sys.argv
    manifest_path = process_dir(temp_dir) / MANIFEST_FILE
    if not manifest_path.exists():
        print("ERROR: manifest.json not found", file=sys.stderr)
        sys.exit(1)

    from config import read_json_safe

    try:
        manifest = read_json_safe(manifest_path)
    except (json.JSONDecodeError, OSError) as e:
        print(f"ERROR: could not parse manifest.json: {e}", file=sys.stderr)
        sys.exit(1)
    errors: list[str] = []
    warnings: list[str] = []

    # Structural checks
    pdir = process_dir(temp_dir)
    for chunk_id, info in manifest.get("chunks", {}).items():
        source_path = pdir / f"{chunk_id}.md"
        output_path = pdir / f"output_{chunk_id}.md"

        if not source_path.exists():
            errors.append(f"MISSING SOURCE: {chunk_id}.md")
            continue
        if not output_path.exists():
            errors.append(f"MISSING OUTPUT: output_{chunk_id}.md")
            continue
        if output_path.stat().st_size == 0:
            errors.append(f"EMPTY OUTPUT: output_{chunk_id}.md")
            continue

        # Verify source hash
        current_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
        if current_hash != info.get("source", ""):
            errors.append(f"SOURCE HASH MISMATCH: {chunk_id}.md")

    # Content sanity (best-effort; only flagged if outputs exist)
    if not errors:
        sanity = content_sanity_report(temp_dir, manifest)
        warnings_count = 0
        for chunk_id, chunk_issues in sanity.items():
            for issue in chunk_issues:
                tag = "WARN" if not strict else "FAIL"
                line = f"  {tag}: {chunk_id}: {issue}"
                if strict:
                    errors.append(line.strip())
                else:
                    warnings.append(line.strip())
                warnings_count += 1

    if errors:
        print("VALIDATION FAILED:")
        for e in errors:
            print(f"  ❌ {e}")
        sys.exit(1)
    elif warnings:
        print(f"VALIDATION OK (with {len(warnings)} warnings):")
        for w in warnings[:30]:
            print(w)
        if len(warnings) > 30:
            print(f"  ... and {len(warnings) - 30} more warnings")
    else:
        total = len(manifest.get("chunks", {}))
        print(f"VALIDATION OK: {total} chunks, all outputs present and valid")


def reset_run_state(temp_dir: Path, prune_zero_freq: bool = False) -> None:
    """Strip stale per-run metadata from glossary.json.

    Removes:
      - `applied_meta_hashes` (top-level field — tracks which meta files
        have already been merged; stale after a fresh re-run).
      - per-term `evidence_refs` and `notes` (accumulated from previous
        merge_meta runs; stale for a fresh re-run).

    Optionally also removes terms with frequency == 0 (they don't appear
    in this book — useful when carrying over a series-wide glossary to
    a new book in the series).

    USE THIS when starting a FRESH re-run of the same book with a
    glossary carried over from a previous run. Otherwise merge_meta.py
    may consider old chunks as "already applied" (because their hashes
    are in applied_meta_hashes) and skip them silently — leading to
    incomplete glossary updates.

    This function does NOT touch:
      - `terms[].source`, `target`, `aliases`, `category`, `gender`,
        `confidence` (these are user-edited / canonical).
      - `high_frequency_top_n` (config).
    """
    gl_path = temp_dir / "glossary.json"
    if not gl_path.exists():
        print(f"ERROR: glossary.json not found at {gl_path}", file=sys.stderr)
        sys.exit(1)

    glossary = load_glossary(temp_dir)
    terms = glossary.get("terms", [])
    before_count = len(terms)

    # Strip stale per-run fields
    had_amh = "applied_meta_hashes" in glossary
    glossary.pop("applied_meta_hashes", None)

    stripped_per_term = 0
    for term in terms:
        if "evidence_refs" in term:
            del term["evidence_refs"]
            stripped_per_term += 1
        if "notes" in term:
            del term["notes"]
            stripped_per_term += 1

    # Optionally prune zero-frequency terms
    pruned_sources: list[str] = []
    if prune_zero_freq:
        kept = []
        for term in terms:
            if term.get("frequency", 0) > 0:
                kept.append(term)
            else:
                pruned_sources.append(term.get("source", "?"))
        glossary["terms"] = kept
        terms = kept

    save_glossary(temp_dir, glossary)

    # Report
    print(f"Reset run-state for: {gl_path}")
    print(f"  applied_meta_hashes removed: {had_amh}")
    print(f"  per-term evidence_refs/notes stripped: {stripped_per_term} fields")
    if prune_zero_freq:
        print(f"  zero-frequency terms pruned: {before_count - len(terms)}")
        if pruned_sources:
            print(f"    removed: {', '.join(pruned_sources[:20])}")
            if len(pruned_sources) > 20:
                print(f"    ... and {len(pruned_sources) - 20} more")
    print(f"  terms remaining: {len(terms)}")
    print()
    print("Glossary is now ready for a FRESH re-run.")
    print("Next: re-run count-frequencies if you pruned zero-freq terms,")
    print("or proceed to step 4 (plan) if frequencies are already correct.")


def _normalize_for_dedup(s: str) -> str:
    """Normalize a source string for near-duplicate comparison.

    Lowercase, strip apostrophe-s endings, strip trailing plural 's'.
    Only strips 's' from the LAST word (anchored to end of string),
    not from words in the middle.

    Examples:
      "Tracker's Lenses" → "tracker's lens"  (s stripped from last word only)
      "Tracker's Lens"   → "tracker's lens"  (s stripped from last word)
      "James Bonds"      → "james bond"       (s stripped from last word)
      "Sing Sing"        → "sing sing"        (no change — last word ends in 'g')
    """
    s = s.lower().strip()
    # Strip apostrophe-s: "Tracker's" → "Tracker"
    s = re.sub(r"['\u2019]s\b", "", s)
    # Strip trailing plural 's' from the LAST word only (anchored to end)
    # Don't strip if word ends in 'ss' (e.g. "glass" → "glass", not "glas")
    s = re.sub(r"s$", "", s) if s.endswith("s") and not s.endswith("ss") else s
    return s.strip()


def _tokenize(s: str) -> list[str]:
    """Split into lowercase tokens for subset matching."""
    return [t for t in re.split(r"[\s\-]+", s.lower().strip()) if t]


def find_duplicates(temp_dir: Path) -> None:
    """Scan glossary for near-duplicate terms (non-destructive, report only).

    Reports these kinds of near-duplicates:
    1. Case-insensitive duplicates: "Janci" vs "janci"
    2. Singular/plural pairs: "Lens" vs "Lenses"
    3. Apostrophe-s variants: "Tracker" vs "Tracker's"
    4. Token-subset matches: "Janci" is a token-subset of "Janci Patterson"
       (one is a prefix/token of the other)

    merge_meta.py's find_existing_term() does exact case-insensitive match
    only — these near-duplicates slip through and create redundant entries.
    This function helps the user find and manually merge them.
    """
    gl_path = temp_dir / "glossary.json"
    if not gl_path.exists():
        print(f"ERROR: glossary.json not found at {gl_path}", file=sys.stderr)
        sys.exit(1)

    glossary = load_glossary(temp_dir)
    terms = glossary.get("terms", [])

    duplicates: list[dict] = []

    for i, t1 in enumerate(terms):
        s1 = t1.get("source", "")
        if not s1:
            continue
        norm1 = _normalize_for_dedup(s1)
        tokens1 = set(_tokenize(s1))
        aliases1 = [a.lower() for a in t1.get("aliases", [])]

        for j, t2 in enumerate(terms):
            if j <= i:
                continue
            s2 = t2.get("source", "")
            if not s2:
                continue
            norm2 = _normalize_for_dedup(s2)
            tokens2 = set(_tokenize(s2))
            aliases2 = [a.lower() for a in t2.get("aliases", [])]

            reason = None

            # 1. Case-insensitive exact match
            if s1.lower() == s2.lower():
                reason = "case-insensitive duplicate"

            # 2. Normalized match (singular/plural, apostrophe-s)
            if not reason and norm1 == norm2 and norm1:
                reason = f"normalized match (singular/plural or apostrophe-s): '{norm1}'"

            # 2b. Prefix match after normalization (catches "Lens" vs "Lenses")
            if not reason and norm1 and norm2 and len(norm1) > 3 and len(norm2) > 3:
                if norm1.startswith(norm2) or norm2.startswith(norm1):
                    shorter = norm1 if len(norm1) < len(norm2) else norm2
                    longer = norm2 if len(norm1) < len(norm2) else norm1
                    suffix = longer[len(shorter) :].strip()
                    if len(suffix) <= 3:
                        reason = f"prefix match after normalization: '{shorter}' ≈ '{longer}' (suffix: '{suffix}')"

            # 3. Token-subset match (one is a strict token-subset of the other)
            # Also catches "Sing" vs "Sing Sing" (where set-dedup makes tokens equal)
            if not reason and tokens1 and tokens2:
                # Special case: one is a repetition of the other
                # ("Sing" vs "Sing Sing" — same single token, different count)
                s1_lower = s1.lower().strip()
                s2_lower = s2.lower().strip()
                if s1_lower and s2_lower:
                    # Check if s2 = s1 + " " + s1 (or vice versa)
                    if s2_lower == f"{s1_lower} {s1_lower}":
                        reason = f"repeated-name: '{s2}' is '{s1}' repeated"
                    elif s1_lower == f"{s2_lower} {s2_lower}":
                        reason = f"repeated-name: '{s1}' is '{s2}' repeated"

                if not reason and tokens1 != tokens2:
                    if tokens1.issubset(tokens2):
                        # Distinguish "short name vs long name" (e.g. "Sing" ⊂ "Sing Sing")
                        # from general token-subset (e.g. "Janci" ⊂ "Janci Patterson")
                        if len(tokens1) == 1 and len(tokens2) == 2:
                            # Single-token name is prefix of two-token name
                            if list(tokens2)[0] == list(tokens1)[0] or s1.lower() == s2.split()[0].lower():
                                reason = f"short-name/long-name: '{s1}' is likely the short form of '{s2}'"
                            else:
                                reason = f"token-subset: '{s1}' is a subset of '{s2}'"
                        else:
                            reason = f"token-subset: '{s1}' is a subset of '{s2}'"
                    elif tokens2.issubset(tokens1):
                        if len(tokens2) == 1 and len(tokens1) == 2:
                            if list(tokens1)[0] == list(tokens2)[0] or s2.lower() == s1.split()[0].lower():
                                reason = f"short-name/long-name: '{s2}' is likely the short form of '{s1}'"
                            else:
                                reason = f"token-subset: '{s2}' is a subset of '{s1}'"
                        else:
                            reason = f"token-subset: '{s2}' is a subset of '{s1}'"

            # 4. Alias overlap: one term's source matches other's alias
            if not reason:
                if s1.lower() in aliases2:
                    reason = f"'{s1}' is an alias of '{s2}'"
                elif s2.lower() in aliases1:
                    reason = f"'{s2}' is an alias of '{s1}'"

            if reason:
                duplicates.append(
                    {
                        "term1": {
                            "source": s1,
                            "target": t1.get("target", ""),
                            "category": t1.get("category", ""),
                            "aliases": t1.get("aliases", []),
                        },
                        "term2": {
                            "source": s2,
                            "target": t2.get("target", ""),
                            "category": t2.get("category", ""),
                            "aliases": t2.get("aliases", []),
                        },
                        "reason": reason,
                    }
                )

    if not duplicates:
        print(f"No near-duplicates found among {len(terms)} terms.")
        return

    print(f"Found {len(duplicates)} near-duplicate pair(s) among {len(terms)} terms:")
    print()
    for idx, d in enumerate(duplicates, 1):
        t1, t2 = d["term1"], d["term2"]
        print(f"  {idx}. {d['reason']}")
        print(
            f"     A: '{t1['source']}' → '{t1['target']}' [{t1['category']}]"
            + (f" (aliases: {t1['aliases']})" if t1['aliases'] else "")
        )
        print(
            f"     B: '{t2['source']}' → '{t2['target']}' [{t2['category']}]"
            + (f" (aliases: {t2['aliases']})" if t2['aliases'] else "")
        )
        if t1["target"] == t2["target"] and t1["target"]:
            print("     → SAME target — safe to merge (add one as alias of the other).")
        elif t1["target"] != t2["target"] and t1["target"] and t2["target"]:
            print("     → DIFFERENT targets — review which translation to keep,")
            print("       then merge via edit_glossary_template.py.")
        print()

    print("To merge: use scripts/shared/edit_glossary_template.py")
    print("  (copy to <temp_dir>/process/_edit_glossary.py, edit, run).")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    command = sys.argv[1]

    if command == "count-frequencies":
        if len(sys.argv) < 3:
            print("Usage: glossary.py count-frequencies <temp_dir>")
            sys.exit(1)
        count_frequencies(Path(sys.argv[2]))

    elif command == "print-terms-for-chunk":
        if len(sys.argv) < 4:
            print("Usage: glossary.py print-terms-for-chunk <temp_dir> <chunk_file>")
            sys.exit(1)
        print_terms_for_chunk(Path(sys.argv[2]), sys.argv[3])

    elif command == "validate-manifest":
        if len(sys.argv) < 3:
            print("Usage: glossary.py validate-manifest <temp_dir> [--strict]")
            sys.exit(1)
        validate_manifest(Path(sys.argv[2]))

    elif command == "reset-run-state":
        if len(sys.argv) < 3:
            print("Usage: glossary.py reset-run-state <temp_dir> [--prune-zero-freq]")
            sys.exit(1)
        temp_dir = Path(sys.argv[2])
        prune_zero = "--prune-zero-freq" in sys.argv
        reset_run_state(temp_dir, prune_zero)

    elif command == "find-duplicates":
        if len(sys.argv) < 3:
            print("Usage: glossary.py find-duplicates <temp_dir>")
            sys.exit(1)
        find_duplicates(Path(sys.argv[2]))

    else:
        print(f"Unknown command: {command}", file=sys.stderr)
        sys.exit(1)
