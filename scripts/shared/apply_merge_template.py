"""DEPRECATED: use `merge_meta.py apply-merge --plan ... --decisions ...`.

This template existed only because `apply-merge` used to read stdin, which
was unreliable on Windows + PowerShell with Cyrillic paths. That is fixed:
`merge_meta.py apply-merge <temp_dir> --plan plan.json --decisions d.json`
takes files, and pulls variants/kind from the plan by id. Kept for
reference only — do not copy it for new runs.
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
