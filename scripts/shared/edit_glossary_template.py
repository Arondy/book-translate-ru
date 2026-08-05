#!/usr/bin/env python3
"""TEMPLATE: Safely edit glossary.json via parse + serialize (not string substitution).

This is a TEMPLATE script — COPY to <temp_dir>/process/_edit_glossary.py
and customize the edit logic, then run.

Usage (after copying and editing):
    1. Set SKILL_SCRIPTS_DIR below if the copy can't find the skill itself.
    2. Edit TEMP_DIR path below (or pass it as argv[1]).
    3. Edit the "EDIT LOGIC" section with your changes.
    4. Run: python3 _edit_glossary.py "<temp_dir>"

Why this exists:
  Editing glossary.json via str.replace / sed / manual text editing is
  DANGEROUS — JSON requires correct commas between array/object elements.
  String substitution easily:
    - forgets a comma after the previous entry before a new one
    - leaves a trailing comma before ] or }
    - breaks quotes inside strings
  This causes json.JSONDecodeError that surfaces only on the NEXT step
  (e.g., step 7.6 pronoun_check or merge_meta apply-merge).

  ALWAYS edit via json.loads + json.dumps. See references/phase-2-translate.md.

Schema safety (learned the hard way — a 78-term glossary was wiped twice):
  - top-level `"version": 2` is MANDATORY. A glossary written without it
    used to be read as "empty" and then overwritten with an empty default.
  - `terms` must stay a non-empty array.
  - every term needs an `id`; it is DERIVED from `source` automatically
    (make_term_id: lowercase, spaces/punctuation -> "_") — never invent it
    by hand.
  This template loads/saves through glossary.py, which enforces all three
  (and refuses to write an empty glossary over a non-empty one).
"""

import sys
from pathlib import Path

# ════════════════════════════════════════════════════════════════════
# EDIT THESE PATHS
# ════════════════════════════════════════════════════════════════════

# Path to the book's <temp_dir> (the folder that contains glossary.json)
TEMP_DIR = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(r"C:\Users\User\Downloads\Перевод-Алькатрас\BookName_temp")

# Path to <skill>/scripts/phase1_prepare — only needed if auto-detection
# fails (it fails once this template is copied outside the skill tree).
SKILL_SCRIPTS_DIR = Path(r"C:\Users\User\.agents\skills\book-translate-ru\scripts\phase1_prepare")

# ════════════════════════════════════════════════════════════════════
# LOAD (through glossary.py — strict schema, no silent empty fallback)
# ════════════════════════════════════════════════════════════════════

_auto = Path(__file__).resolve().parent.parent / "phase1_prepare"  # works inside the skill tree
GLOSSARY_PY_DIR: Path | None = None
for candidate in (_auto, SKILL_SCRIPTS_DIR):
    if (candidate / "glossary.py").exists():
        GLOSSARY_PY_DIR = candidate
        sys.path.insert(0, str(candidate))
        break
if GLOSSARY_PY_DIR is None:
    sys.exit(
        "ABORT: can't find the skill's scripts/phase1_prepare/glossary.py.\n"
        "  Set SKILL_SCRIPTS_DIR at the top of this file and re-run."
    )

import glossary as G  # noqa: E402
from common import make_term_id  # noqa: E402,F401  (used by the examples below)

try:
    gl = G.load_glossary(TEMP_DIR)
except G.GlossaryError as e:
    sys.exit(f"ABORT: {e}")

terms_before = len(gl["terms"])
print(f"Loaded: {terms_before} terms (version={gl.get('version')!r})")

# ════════════════════════════════════════════════════════════════════
# EDIT LOGIC — customize this section
# ════════════════════════════════════════════════════════════════════

# Example 1: Change the target of an existing term
# for term in gl["terms"]:
#     if term["source"] == "Firebringer's Lens":
#         term["target"] = "Линза Огненосца"
#         term["confidence"] = "high"
#         term["notes"] = "confirmed by user"
#         print(f"  Updated: {term['source']} -> {term['target']}")

# Example 2: Add a new term (id is derived from source — do not invent it)
# gl["terms"].append({
#     "id": make_term_id("NewTerm", {t.get("id") for t in gl["terms"]}),
#     "source": "NewTerm",
#     "target": "НовыйТермин",
#     "aliases": [],
#     "category": "term",
#     "gender": "unknown",
#     "confidence": "high",
#     "frequency": 0,
#     "evidence_refs": [],
#     "notes": "added by user",
# })

# Example 3: Remove a term
# gl["terms"] = [t for t in gl["terms"] if t["source"] != "UnwantedTerm"]

# Example 4: Add an alias to an existing term
# for term in gl["terms"]:
#     if term["source"] == "Janci Patterson":
#         if "Janci" not in term.get("aliases", []):
#             term.setdefault("aliases", []).append("Janci")
#         print(f"  Added alias 'Janci' to {term['source']}")

# ════════════════════════════════════════════════════════════════════
# SAVE (schema guards inside save_glossary — do not bypass)
# ════════════════════════════════════════════════════════════════════

try:
    G.save_glossary(TEMP_DIR, gl)  # forces version=2, fills ids, blocks empty overwrite
except G.GlossaryError as e:
    sys.exit(f"ABORT (nothing written): {e}")

print(f"Saved: {TEMP_DIR / 'glossary.json'} ({len(gl['terms'])} terms, was {terms_before})")
print("OK: glossary.json edited via parse + serialize (safe)")
print()
print("Now verify the file on disk (do NOT trust this report alone):")
print(f'  python3 {GLOSSARY_PY_DIR / "glossary.py"} validate-glossary "{TEMP_DIR}"')
