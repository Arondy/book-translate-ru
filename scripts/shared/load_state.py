#!/usr/bin/env python3
"""Read progress.json and print current state for resume protocol.

This is the resume entry point: after context compaction (or at any
re-entry), the orchestrator runs this script first to learn where
the workflow currently stands. The script prints:
  - current step number (1-9)
  - current phase (1, 2, or 3)
  - completed steps
  - path to the phase instructions file the agent should read next
  - any notes saved by previous steps

Usage:
    python3 load_state.py <temp_dir>

If progress.json doesn't exist yet, prints a "fresh start" message
with phase-1 instructions path.

The progress.json file lives in <temp_dir>/ (human-facing root, NOT
in process/ — it's the orchestration state, the user can inspect it).
"""

import json
import sys
from pathlib import Path

# Ensure UTF-8 output on Windows (cp1251 default breaks on non-ASCII)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")


# Phase boundaries (which steps belong to which phase)
PHASE_FOR_STEP = {
    1: 1,
    2: 1,
    3: 1,  # Phase 1: prepare, convert, glossary
    4: 2,
    5: 2,
    6: 2,  # Phase 2: plan, translate, merge
    7: 3,
    8: 3,
    9: 3,  # Phase 3: qa, polish, build
}

PHASE_FILES = {
    1: "references/phase-1-prepare.md",
    2: "references/phase-2-translate.md",
    3: "references/phase-3-finish.md",
}

PHASE_NAMES = {
    1: "Подготовка (steps 1-3)",
    2: "Перевод (steps 4-6)",
    3: "Финал (steps 7-9)",
}


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    temp_dir = Path(sys.argv[1])
    if not temp_dir.exists():
        print(f"ERROR: temp dir not found: {temp_dir}", file=sys.stderr)
        sys.exit(2)

    progress_path = temp_dir / "progress.json"

    if not progress_path.exists():
        # Fresh start
        print("=" * 60)
        print("FRESH START — no progress.json found")
        print("=" * 60)
        print()
        print(f"temp_dir:       {temp_dir}")
        print("current_step:   1")
        print(f"current_phase:  1 ({PHASE_NAMES[1]})")
        print()
        print("NEXT ACTION:")
        print("  Read: references/phase-1-prepare.md")
        print("  Execute step 1 (Подготовка).")
        print()
        print("After completing step 1, run:")
        print(f"  python3 scripts/shared/save_state.py \"{temp_dir}\" 1 --note \"...\"")
        sys.exit(0)

    try:
        progress = json.loads(progress_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        print(f"ERROR: cannot read progress.json: {e}", file=sys.stderr)
        sys.exit(2)

    current_step = progress.get("current_step", 1)
    current_phase = progress.get("current_phase", PHASE_FOR_STEP.get(current_step, 1))
    completed_steps = progress.get("completed_steps", [])

    print("=" * 60)
    print(f"RESUME — {temp_dir}")
    print("=" * 60)
    print()
    print(f"book:           {progress.get('book', '?')}")
    print(f"current_step:   {current_step}")
    print(f"current_phase:  {current_phase} ({PHASE_NAMES.get(current_phase, '?')})")
    print(f"completed:      {completed_steps}")
    print(f"started_at:     {progress.get('started_at', '?')}")
    print(f"last_completed_at: {progress.get('last_completed_at', '?')}")
    print()

    # Book metadata
    if progress.get("glossary_path"):
        print(f"glossary:       {progress['glossary_path']}")
    if progress.get("voice_path"):
        print(f"голос_книги:    {progress['voice_path']}")
    if progress.get("total_chunks"):
        print(f"total_chunks:   {progress['total_chunks']}")
    if progress.get("failed_chunks"):
        print(f"failed_chunks:  {progress['failed_chunks']}")
    print()

    # Notes from previous steps
    notes = progress.get("notes", [])
    if notes:
        print("NOTES FROM PREVIOUS STEPS:")
        for note in notes[-5:]:  # last 5 notes
            print(f"  [step {note.get('step', '?')}] {note.get('text', '')}")
            print(f"           at: {note.get('at', '?')}")
        print()

    # Next action
    print("NEXT ACTION:")
    if current_step > 9:
        print("  All 9 steps complete. Workflow finished.")
        print(f"  Final outputs in {temp_dir}: book.fb2, output.md, post_mortem.md")
    else:
        phase_file = PHASE_FILES.get(current_phase, PHASE_FILES[1])
        print(f"  Read: {phase_file}")
        print(f"  Find step {current_step} in that file and execute it.")
        print()
        print(f"After completing step {current_step}, run:")
        print(f"  python3 scripts/shared/save_state.py \"{temp_dir}\" {current_step} \\")
        print("      --note \"what you did\" \\")
        print("      [--set key=value ...]")

    # Compaction hint
    if current_step in (4, 7):
        print()
        print(f"⚠️  You just crossed a compaction boundary (entering phase {current_phase}).")
        print("    If you haven't compacted context recently, NOW is the recommended")
        print(f"    moment to suggest it to the user (before starting phase {current_phase}).")


if __name__ == "__main__":
    main()
