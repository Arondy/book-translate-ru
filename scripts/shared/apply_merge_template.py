"""TEMPLATE: Apply merge_meta decisions by calling apply_merge() directly.

This is a TEMPLATE script — COPY to <temp_dir>/process/_apply.py and
customize the `decisions` list, then run. This bypasses stdin/CLI
(which is unreliable on Windows + PowerShell with Cyrillic paths).

Usage (after copying and editing):
    1. Edit SKILL_DIR and TEMP_DIR paths below (or pass as args).
    2. Edit the `decisions` list with your merge decisions.
    3. Run: python3 _apply.py

Why this exists:
  On Windows + PowerShell, `echo '<json>' | python3 merge_meta.py apply-merge`
  is unreliable with Cyrillic paths. Calling apply_merge() directly
  via Python import is more robust. See references/windows-powershell.md.

IMPORTANT for conflicting_new_entity_proposals:
  In merge_meta.py, the field is called DIFFERENTLY for different kinds:
    - existing_entity_conflict → "proposed_variants" in prepare-merge output
    - conflicting_new_entity_proposals → "variants" in prepare-merge output
  This template auto-detects which field exists and uses it.
"""

import sys
from pathlib import Path

# ════════════════════════════════════════════════════════════════════
# EDIT THESE PATHS (or pass as command-line args)
# ════════════════════════════════════════════════════════════════════

# Path to the skill's scripts/phase2_translate/ directory
SKILL_DIR = (
    Path(sys.argv[1])
    if len(sys.argv) > 1
    else Path(r"C:\Users\User\.agents\skills\book-translate-ru\scripts\phase2_translate")
)

# Path to the book's <temp_dir>
TEMP_DIR = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(r"C:\Users\User\Downloads\Перевод-Алькатрас\BookName_temp")

# ════════════════════════════════════════════════════════════════════
# EDIT THIS LIST — your merge decisions
# ════════════════════════════════════════════════════════════════════

decisions = [
    # Example: accept a proposed better target for an existing entity
    # {
    #     "id": "conflict_Stormfather_target",
    #     "kind": "conflict",
    #     "choice": "accept_proposed",
    # },
    # Example: pick variant 1 for a conflicting new entity proposal
    # {
    #     "id": "conflicting_new_entity_SomeTerm",
    #     "kind": "conflicting_new_entity_proposals",
    #     "choice": "use_variant_1",
    # },
    # Example: keep current target for an existing entity conflict
    # {
    #     "id": "new_entity_conflict_Taig",
    #     "kind": "existing_entity_conflict",
    #     "choice": "keep_current",
    # },
]

# ════════════════════════════════════════════════════════════════════
# Main logic — do NOT edit below unless you know what you're doing
# ════════════════════════════════════════════════════════════════════

sys.path.insert(0, str(SKILL_DIR))
import merge_meta

prepared = merge_meta.prepare_merge(TEMP_DIR)

# Auto-populate variants/proposed_variants for decisions that need them
for d in decisions:
    kind = d.get("kind", "")
    dec_id = d.get("id", "")
    if not dec_id:
        continue
    # Find the matching prepared decision
    p = next((p for p in prepared["decisions_needed"] if p["id"] == dec_id), None)
    if p is None:
        print(f"WARN: decision {dec_id!r} not found in prepared decisions_needed", file=sys.stderr)
        continue

    if kind == "conflicting_new_entity_proposals":
        # This kind requires "variants" in the payload
        d["variants"] = p.get("variants") or p.get("proposed_variants")
    elif kind == "existing_entity_conflict" and "use_variant_" in d.get("choice", ""):
        # use_variant_N choices need proposed_variants round-tripped
        d["proposed_variants"] = p.get("proposed_variants") or p.get("variants")

payload = {
    "auto_apply": prepared["auto_apply"],
    "decisions": decisions,
    "consumed_chunk_ids": prepared["consumed_chunk_ids"],
}

print(f"Auto-apply items: {len(payload['auto_apply'])}")
print(f"Decisions: {len(payload['decisions'])}")
print(f"Consumed chunks: {len(payload['consumed_chunk_ids'])}")

merge_meta.apply_merge(TEMP_DIR, payload)
print("OK: apply_merge completed successfully")
