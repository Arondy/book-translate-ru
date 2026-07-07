#!/usr/bin/env python3
"""TEMPLATE: Safely edit glossary.json via parse + serialize (not string substitution).

This is a TEMPLATE script — COPY to <temp_dir>/process/_edit_glossary.py
and customize the edit logic, then run.

Usage (after copying and editing):
    1. Edit TEMP_DIR path below.
    2. Edit the "EDIT LOGIC" section with your changes.
    3. Run: python3 _edit_glossary.py

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
"""

import json
import sys
from pathlib import Path

# ════════════════════════════════════════════════════════════════════
# EDIT THIS PATH
# ════════════════════════════════════════════════════════════════════

TEMP_DIR = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(r"C:\Users\User\Downloads\Перевод-Алькатрас\BookName_temp")
gl_path = TEMP_DIR / "glossary.json"

# ════════════════════════════════════════════════════════════════════
# LOAD
# ════════════════════════════════════════════════════════════════════

gl = json.loads(gl_path.read_text(encoding="utf-8"))
print(f"Loaded: {len(gl['terms'])} terms")

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

# Example 2: Add a new term
# gl["terms"].append({
#     "id": "NewTerm",
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
# SAVE
# ════════════════════════════════════════════════════════════════════

gl_path.write_text(
    json.dumps(gl, indent=2, ensure_ascii=False),
    encoding="utf-8",
)
print(f"Saved: {gl_path} ({len(gl['terms'])} terms)")
print("OK: glossary.json edited via parse + serialize (safe)")
