"""Glossary management for book-translate-ru skill.

Usage:
    python3 glossary.py count-frequencies <temp_dir>
    python3 glossary.py print-terms-for-chunk <temp_dir> <chunk_file>
    python3 glossary.py validate-glossary <temp_dir> [--fix]
    python3 glossary.py validate-manifest <temp_dir>
    python3 glossary.py reset-run-state <temp_dir> [--prune-zero-freq]
    python3 glossary.py find-duplicates <temp_dir>
    python3 glossary.py inspect-manifest <temp_dir>

Commands:
    count-frequencies      Recompute term frequencies from chunks.
    print-terms-for-chunk  Print the per-chunk term table for a chunk.
    validate-glossary      Schema gate for glossary.json: valid JSON,
                           top-level "version": 2, non-empty `terms`,
                           required per-term fields, unique ids/sources.
                           RUN THIS after ANY write to glossary.json
                           (especially a write done by a sub-agent) and
                           BEFORE count-frequencies. With --fix it repairs
                           what can be repaired deterministically (missing
                           `version`, missing/duplicate `id`, missing
                           optional fields) and saves the file back.

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
                           merge_meta's exact-match dedup missed.

    inspect-manifest       Print a human-readable summary of manifest.json:
                           chunk IDs, section titles, sizes, levels. Use this
                           instead of ad-hoc python3 -c to inspect manifest.

                           Non-destructive — only REPORTS, does not merge.
                           Use after step 6 (merge) to catch terms that
                           sub-agents added as "new" but which are actually
                           variants of existing glossary entries.

Data-loss safety (see references/meta-json-schema.md, "Схема glossary.json"):
    glossary.json is a human-facing file. This script NEVER silently
    replaces a broken/unknown-schema glossary with an empty default and
    NEVER writes an empty term list over a non-empty one — it fails loudly
    instead, so a bad write by a sub-agent cannot destroy collected terms.
"""

import hashlib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "shared"))
from common import find_english_leaks, make_term_id, process_dir
from config import get_config
from glossary_io import (
    GLOSSARY_FILE,
    GLOSSARY_VERSION,
    GlossaryError,
    disk_terms_count,
    load_glossary,
    save_glossary,
)

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


MANIFEST_FILE = "manifest.json"  # lives in process/

# Quality thresholds — loaded from config.toml [quality] section
_cfg = get_config()
RATIO_MIN = _cfg.get("quality", "ratio_min", 0.6)
RATIO_MAX = _cfg.get("quality", "ratio_max", 2.0)
EN_LEAK_CHARS = _cfg.get("quality", "en_leak_chars", 80)


# Glossary I/O (GlossaryError, GLOSSARY_FILE, GLOSSARY_VERSION,
# load_glossary, save_glossary, disk_terms_count) lives in
# scripts/shared/glossary_io.py — imported and re-exported above so
# templates (edit_glossary_template.py) and prompts keep working.


def confirm_terms(
    temp_dir: Path,
    *,
    all_terms: bool = False,
    term_ids: list[str] | None = None,
    sources: list[str] | None = None,
    note: str | None = None,
    force: bool = False,
) -> int:
    """Raise term confidence from low/medium to high on user confirmation.

    Canonical, scripted replacement for hand-editing `confidence`/`notes` in
    glossary.json. Agents add new terms with `confidence: "low"` (see
    merge_meta.py); this turns user feedback ("ОК, подтверди") into a safe
    write that bumps the selected terms to `"high"` and records a note.

    Selection (one or more):
      - all_terms=True                                   -> every term
      - term_ids=[...]                                   -> match term id (case-insensitive)
      - sources=[...]                                    -> match term source/alias (case-insensitive)
      - force=True                                        -> also re-confirm terms already "high"

    Terms already at `"high"` (and not --force) are skipped. Saves via
    save_glossary (atomic + schema-guarded). Returns the number changed.

    Never silently no-ops a bad selector: if nothing matches, it prints a
    hint and returns 0 without writing.
    """
    glossary = load_glossary(temp_dir)
    terms = glossary["terms"]

    selected: set[int] = set()
    if all_terms:
        selected = set(range(len(terms)))
    else:
        if term_ids:
            idset = {t.lower() for t in term_ids}
            for i, t in enumerate(terms):
                if str(t.get("id", "")).lower() in idset:
                    selected.add(i)
        if sources:
            srcset = {s.lower() for s in sources}
            for i, t in enumerate(terms):
                surfaces = {str(t.get("source", "")).lower()} | {str(a).lower() for a in (t.get("aliases") or [])}
                if surfaces & srcset:
                    selected.add(i)

    if not selected:
        print(
            "confirm-terms: ни один термин не выбран — проверь --id/--source "
            "или используй --all. glossary.json НЕ изменён.",
            file=sys.stderr,
        )
        return 0

    base_note = note or "confirmed by user"
    changed_idx: list[int] = []
    for i in sorted(selected):
        t = terms[i]
        cur = str(t.get("confidence", "low"))
        if not force and cur == "high":
            continue
        t["confidence"] = "high"
        existing = t.get("notes")
        if existing:
            if base_note not in str(existing):
                t["notes"] = f"{existing}; {base_note}"
        else:
            t["notes"] = base_note
        changed_idx.append(i)

    if not changed_idx:
        print(
            "confirm-terms: выбранные термины уже имеют confidence=\"high\" — "
            "ничего не изменено (используй --force для повторного подтверждения)."
        )
        return 0

    save_glossary(temp_dir, glossary)
    print(f"confirm-terms: подтверждено (confidence -> high) для {len(changed_idx)} термин(ов):")
    for i in changed_idx:
        t = terms[i]
        print(f"  • {t.get('source')!r} -> {t.get('target')!r}  (id={t.get('id')!r})")
    print(f'  Проверь: python3 glossary.py validate-glossary "{temp_dir}"')
    return len(changed_idx)


def term_surface_forms(term: dict) -> list[str]:
    forms = [str(term.get("source", "")).lower()]
    for alias in term.get("aliases", []) or []:
        forms.append(str(alias).lower())
    return [f for f in forms if f]


def count_frequencies(temp_dir: Path):
    # Snapshot the disk state BEFORE loading — this is the guard against
    # overwriting a real glossary with the result of a bad/partial read.
    on_disk = disk_terms_count(temp_dir)

    glossary = load_glossary(temp_dir)
    terms = glossary["terms"]

    if not terms and on_disk:
        raise GlossaryError(
            f"ABORT: загружено 0 терминов, а в {temp_dir / GLOSSARY_FILE} их {on_disk}.\n"
            f"  Похоже на проблему со схемой (чаще всего — отсутствует top-level "
            f"\"version\": {GLOSSARY_VERSION}).\n"
            f"  Проверь glossary.json: python3 glossary.py validate-glossary \"{temp_dir}\"\n"
            f"  Частоты НЕ пересчитаны, glossary.json НЕ изменён."
        )

    chunks = sorted(process_dir(temp_dir).glob("chunk*.md"))
    if not chunks:
        print(f"WARN: в {process_dir(temp_dir)} нет chunk*.md — все частоты будут 0.", file=sys.stderr)

    # Read every chunk once and reuse the cached text for all terms
    # (per-term disk reads were O(terms x chunks)).
    chunk_texts = [c.read_text(encoding="utf-8").lower() for c in chunks]

    for term in terms:
        term["frequency"] = 0
        surfaces = term_surface_forms(term)
        for text in chunk_texts:
            for surface in surfaces:
                if surface_in_text(surface, text):
                    term["frequency"] += 1
                    break

    save_glossary(temp_dir, glossary, allow_empty=not on_disk)
    print(f"Frequencies updated: {len(terms)} terms")
    for t in terms:
        print(f"  {t['source']}: {t['frequency']}")

    zero_freq = [t["source"] for t in terms if t.get("frequency", 0) == 0]
    if zero_freq:
        print()
        print(f"WARN: {len(zero_freq)} терм(ов) не встречаются в книге ни разу: " + ", ".join(zero_freq[:20]))
        if len(zero_freq) > 20:
            print(f"      ... и ещё {len(zero_freq) - 20}")
        print("      Обычно это одноразовые имена (благодарности, вступление) или")
        print("      остатки series-wide глоссария. Проверь и удали лишние.")


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
            aliases_str = ", ".join(term.get("aliases", []) or [])
            chunk_terms.append(
                {
                    "source": term.get("source", ""),
                    "alias": aliases_str,
                    "target": term.get("target", ""),
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

        leaks = find_english_leaks(out_text, min_chars=EN_LEAK_CHARS)
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


# ─────────────────────────────────────────────────────────────────────
# validate-glossary — schema gate for glossary.json
# ─────────────────────────────────────────────────────────────────────

VALID_CATEGORIES = {"person", "place", "org", "term", "other"}
VALID_GENDERS = {"male", "female", "neutral", "unknown"}
VALID_CONFIDENCE = {"high", "medium", "low"}
REQUIRED_TERM_FIELDS = ("id", "source", "target")
OPTIONAL_TERM_DEFAULTS = {
    "aliases": list,
    "category": lambda: "other",
    "gender": lambda: "unknown",
    "confidence": lambda: "low",
    "frequency": lambda: 0,
}


def validate_glossary(temp_dir: Path, fix: bool = False) -> None:
    """Validate glossary.json against the v2 schema. Exit non-zero on errors.

    This is the gate that must run after ANY write to glossary.json —
    especially a write performed by a sub-agent, whose self-report
    ("78 terms written and verified") is not evidence.

    Checks (errors — block the pipeline):
      - file exists, is valid JSON, top-level object
      - top-level "version": 2                       (--fix: added)
      - "terms" is a non-empty array
      - each term has non-empty `source` and `target`
      - each term has an `id`                        (--fix: derived from source)
      - ids are unique                               (--fix: re-derived)
      - sources are unique (case-insensitive)

    Checks (warnings — reported, do not block):
      - unknown category / gender / confidence values
      - missing optional fields                      (--fix: filled with defaults)
      - frequency == 0 (term never occurs in the book)
      - frequency == 1 (likely one-off: acknowledgements, author's intro)
      - an alias duplicating another term's source
    """
    path = temp_dir / GLOSSARY_FILE
    if not path.exists():
        print(f"ERROR: glossary.json not found at {path}", file=sys.stderr)
        sys.exit(1)

    from config import read_json_safe

    try:
        data = read_json_safe(path)
    except (json.JSONDecodeError, OSError, ValueError) as e:
        print(f"VALIDATION FAILED: {path} is not valid JSON: {e}", file=sys.stderr)
        sys.exit(1)

    errors: list[str] = []
    warnings: list[str] = []
    fixes: list[str] = []

    if not isinstance(data, dict):
        print(f"VALIDATION FAILED: top-level JSON must be an object, got {type(data).__name__}", file=sys.stderr)
        sys.exit(1)

    # ── top-level ────────────────────────────────────────────────────
    if data.get("version") != GLOSSARY_VERSION:
        msg = f'top-level "version" must be {GLOSSARY_VERSION} (got {data.get("version")!r})'
        if fix and isinstance(data.get("terms"), list):
            data["version"] = GLOSSARY_VERSION
            fixes.append(f'set "version": {GLOSSARY_VERSION}')
        else:
            errors.append(msg + " — without it load_glossary() refuses to read the file")

    terms = data.get("terms")
    if not isinstance(terms, list):
        errors.append('"terms" is missing or is not an array')
        terms = []
    elif not terms:
        errors.append('"terms" is empty — a glossary with zero terms is almost always a bad write')

    if "high_frequency_top_n" not in data:
        if fix:
            data["high_frequency_top_n"] = get_config().get("glossary", "high_frequency_top_n", 20)
            fixes.append('added "high_frequency_top_n"')
        else:
            warnings.append('"high_frequency_top_n" missing (defaults to config value)')

    if "applied_meta_hashes" not in data:
        if fix:
            data["applied_meta_hashes"] = {}
            fixes.append('added "applied_meta_hashes"')
        else:
            warnings.append('"applied_meta_hashes" missing (will be created on first merge)')

    # ── per-term ─────────────────────────────────────────────────────
    seen_ids: dict[str, int] = {}
    seen_sources: dict[str, int] = {}
    all_sources = {str(t.get("source", "")).lower() for t in terms if isinstance(t, dict)}
    # One-off detection only makes sense on a real book, not on a 1-chunk test
    chunk_count = len(list(process_dir(temp_dir).glob("chunk*.md")))
    check_one_offs = chunk_count >= 5

    for i, term in enumerate(terms):
        label = f"terms[{i}]"
        if not isinstance(term, dict):
            errors.append(f"{label} is not an object")
            continue

        source = term.get("source")
        if not isinstance(source, str) or not source.strip():
            errors.append(f"{label}.source is missing or empty")
            source = ""
        else:
            label = f"terms[{i}] ({source})"

        target = term.get("target")
        if not isinstance(target, str) or not target.strip():
            errors.append(f"{label}.target is missing or empty")

        tid = term.get("id")
        if not isinstance(tid, str) or not tid.strip():
            if fix:
                new_id = make_term_id(source, seen_ids.keys())
                term["id"] = new_id
                fixes.append(f"{label}: id -> {new_id!r}")
                tid = new_id
            else:
                errors.append(f"{label}.id is missing (run with --fix to derive it from source)")
                tid = None
        elif tid in seen_ids:
            if fix:
                new_id = make_term_id(source, seen_ids.keys())
                term["id"] = new_id
                fixes.append(f"{label}: duplicate id {tid!r} -> {new_id!r}")
                tid = new_id
            else:
                errors.append(f"{label}.id {tid!r} duplicates terms[{seen_ids[tid]}] (run with --fix)")
                tid = None
        if tid:
            seen_ids[tid] = i

        if source:
            key = source.lower()
            if key in seen_sources:
                errors.append(f"{label}.source duplicates terms[{seen_sources[key]}] — merge them manually")
            else:
                seen_sources[key] = i

        # Optional fields
        for field, factory in OPTIONAL_TERM_DEFAULTS.items():
            if field not in term:
                if fix:
                    term[field] = factory()
                    fixes.append(f"{label}: added {field}")
                else:
                    warnings.append(f"{label}.{field} missing")

        aliases = term.get("aliases", [])
        if aliases is not None and not isinstance(aliases, list):
            errors.append(f"{label}.aliases must be an array")
        else:
            for alias in aliases or []:
                if not isinstance(alias, str) or not alias.strip():
                    errors.append(f"{label}.aliases contains an empty/non-string value")
                elif alias.lower() in all_sources and alias.lower() != str(source).lower():
                    warnings.append(f"{label}: alias {alias!r} is also a separate term's source — merge them")

        category = term.get("category")
        if category is not None and category not in VALID_CATEGORIES:
            warnings.append(f"{label}.category {category!r} not in {sorted(VALID_CATEGORIES)}")
        gender = term.get("gender")
        if gender is not None and gender not in VALID_GENDERS:
            warnings.append(f"{label}.gender {gender!r} not in {sorted(VALID_GENDERS)}")
        confidence = term.get("confidence")
        if confidence is not None and confidence not in VALID_CONFIDENCE:
            warnings.append(f"{label}.confidence {confidence!r} not in {sorted(VALID_CONFIDENCE)}")

        freq = term.get("frequency")
        if freq is not None and not isinstance(freq, int):
            warnings.append(f"{label}.frequency is not an integer ({freq!r})")
        elif isinstance(freq, int) and freq == 0:
            warnings.append(f"{label}: frequency 0 — термин не встречается в книге (run count-frequencies?)")
        elif isinstance(freq, int) and freq == 1 and check_one_offs:
            warnings.append(
                f"{label}: frequency 1 — возможно, одноразовое имя "
                f"(благодарности / вступление автора); такие в глоссарий не нужны"
            )

    # ── report ───────────────────────────────────────────────────────
    if fix and fixes and not errors:
        save_glossary(temp_dir, data, allow_empty=False)

    if fixes:
        print(f"FIXED ({len(fixes)}):")
        for f in fixes[:40]:
            print(f"  🔧 {f}")
        if len(fixes) > 40:
            print(f"  ... and {len(fixes) - 40} more")
        if errors:
            print("  (not saved — errors remain, see below)")
        print()

    if errors:
        print(f"GLOSSARY VALIDATION FAILED: {len(errors)} error(s)")
        for e in errors[:40]:
            print(f"  ❌ {e}")
        if len(errors) > 40:
            print(f"  ... and {len(errors) - 40} more")
        print()
        print("Fix glossary.json via scripts/shared/edit_glossary_template.py")
        print("(json.loads + json.dumps — never str.replace), then re-run this command.")
        sys.exit(1)

    print(f"GLOSSARY OK: {len(terms)} terms, version {data.get('version')}")
    if warnings:
        print(f"  {len(warnings)} warning(s):")
        for w in warnings[:30]:
            print(f"  ⚠️  {w}")
        if len(warnings) > 30:
            print(f"  ... and {len(warnings) - 30} more warnings")


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
        if not kept and before_count:
            print(
                "ABORT: --prune-zero-freq удалил бы ВСЕ термины "
                f"({before_count} шт., у всех frequency == 0).\n"
                "  Скорее всего частоты просто не пересчитаны: сначала запусти\n"
                f'  python3 glossary.py count-frequencies "{temp_dir}"\n'
                "  glossary.json НЕ изменён.",
                file=sys.stderr,
            )
            sys.exit(1)
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


def _inspect_manifest(temp_dir: Path) -> None:
    """Print a human-readable summary of manifest.json.

    Shows each chunk: ID, section_title, size, section_level.
    Use this instead of ad-hoc python3 -c to inspect manifest structure.
    """
    manifest_path = process_dir(temp_dir) / "manifest.json"
    if not manifest_path.exists():
        print(f"ERROR: manifest.json not found at {manifest_path}", file=sys.stderr)
        sys.exit(1)

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    chunks = manifest.get("chunks", {})

    print(f"Manifest: {manifest_path}")
    print(f"  version: {manifest.get('version', '?')}")
    print(f"  source_file: {manifest.get('source_file', '?')}")
    print(f"  chapter_split: {manifest.get('chapter_split', '?')}")
    print(f"  converter: {manifest.get('converter', '?')}")
    print(f"  total chunks: {len(chunks)}")
    print()
    print(f"{'chunk_id':<12} {'level':<6} {'size':>8}  {'section_title'}")
    print(f"{'─' * 12} {'─' * 6} {'─' * 8}  {'─' * 40}")
    for cid, info in chunks.items():
        level = info.get("section_level", "?")
        size = info.get("size", "?")
        title = info.get("section_title", "?")
        print(f"{cid:<12} {level:<6} {size:>8}  {title}")

    sizes = [info.get("size", 0) for info in chunks.values()]
    if sizes:
        print()
        print(f"  size stats: min={min(sizes)}, max={max(sizes)}, avg={sum(sizes) // len(sizes)}")


def main() -> None:
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

    elif command == "validate-glossary":
        if len(sys.argv) < 3:
            print("Usage: glossary.py validate-glossary <temp_dir> [--fix]")
            sys.exit(1)
        validate_glossary(Path(sys.argv[2]), fix="--fix" in sys.argv)

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

    elif command == "inspect-manifest":
        if len(sys.argv) < 3:
            print("Usage: glossary.py inspect-manifest <temp_dir>")
            sys.exit(1)
        _inspect_manifest(Path(sys.argv[2]))

    elif command == "confirm-terms":
        if len(sys.argv) < 3:
            print(
                "Usage: glossary.py confirm-terms <temp_dir> "
                "[--all] [--id ID ...] [--source NAME ...] [--note TEXT] [--force]"
            )
            sys.exit(1)
        temp_dir = Path(sys.argv[2])
        all_terms = "--all" in sys.argv
        force = "--force" in sys.argv
        note = sys.argv[sys.argv.index("--note") + 1] if "--note" in sys.argv else None
        term_ids: list[str] = []
        sources: list[str] = []
        if "--id" in sys.argv:
            idx = sys.argv.index("--id")
            for a in sys.argv[idx + 1 :]:
                if a.startswith("--"):
                    break
                term_ids.append(a)
        if "--source" in sys.argv:
            idx = sys.argv.index("--source")
            for a in sys.argv[idx + 1 :]:
                if a.startswith("--"):
                    break
                sources.append(a)
        if not (all_terms or term_ids or sources):
            print("ERROR: укажи --all, --id или --source", file=sys.stderr)
            sys.exit(1)
        confirm_terms(
            temp_dir,
            all_terms=all_terms,
            term_ids=(term_ids or None),
            sources=(sources or None),
            note=note,
            force=force,
        )

    else:
        print(f"Unknown command: {command}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except GlossaryError as e:
        # Never swallow a glossary problem: fail loudly with a non-zero exit
        # so the orchestrator cannot mistake data loss for success.
        print(f"GLOSSARY ERROR: {e}", file=sys.stderr)
        sys.exit(1)
