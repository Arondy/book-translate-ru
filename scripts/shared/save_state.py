#!/usr/bin/env python3
"""Write progress.json after completing a step.

This is the companion to load_state.py: after the orchestrator finishes
a step, it runs this script to persist the new state. progress.json is
the ONLY thing that reliably survives context compaction with full
fidelity — every step MUST end with a save_state.py call.

Usage:
    python3 save_state.py <temp_dir> <step_number> [options]

Options:
    --note "<text>"          Append a note to the progress log
    --set <key>=<value>      Set an arbitrary key in progress.json
                             (can be repeated: --set foo=1 --set bar=2)
    --set-int <key>=<value>  Same, but coerce value to int
    --append-list <key>=<value>  Append value to a list (creates if missing)

The script:
  1. Reads existing progress.json (or initializes a new one)
  2. Adds the step to completed_steps
  3. Increments current_step
  4. Updates current_phase based on the new step
  5. Records last_completed_at timestamp
  6. Applies --set / --set-int / --append-list mutations
  7. Appends --note entries to notes[]
  8. Atomically writes back (tmp + rename)

progress.json schema (informal):
{
  "started_at": "ISO timestamp",
  "last_completed_at": "ISO timestamp",
  "current_step": int (1-10, 10 = finished),
  "current_phase": int (1, 2, or 3),
  "completed_steps": [1, 2, 3, ...],
  "book": "title or path",
  "source_lang": "en",
  "target_lang": "ru",
  "glossary_path": "<temp_dir>/glossary.json",
  "voice_path": "<temp_dir>/голос_книги.md",
  "total_chunks": int,
  "failed_chunks": [chunk_ids...],
  "notes": [{"step": int, "text": str, "at": "ISO timestamp"}]
}
"""

import datetime
import json
import sys
from pathlib import Path

# Ensure UTF-8 output on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")


PHASE_FOR_STEP = {
    1: 1,
    2: 1,
    3: 1,
    4: 2,
    5: 2,
    6: 2,
    7: 3,
    8: 3,
    9: 3,
    10: 3,  # 10 = "all done"
}


def atomic_write_json(path: Path, data: dict, **kwargs):
    """Write JSON atomically (tmp + rename). Imports from config.py."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from config import atomic_write_json as _awj

    _awj(path, data, **kwargs)


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)

    temp_dir = Path(sys.argv[1])
    try:
        step = int(sys.argv[2])
    except ValueError:
        print(f"ERROR: step_number must be an integer, got: {sys.argv[2]!r}", file=sys.stderr)
        sys.exit(1)

    if not temp_dir.exists():
        print(f"ERROR: temp dir not found: {temp_dir}", file=sys.stderr)
        sys.exit(2)

    progress_path = temp_dir / "progress.json"

    # Load existing or initialize
    if progress_path.exists():
        try:
            progress = json.loads(progress_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            print(f"WARN: could not parse progress.json, starting fresh: {e}", file=sys.stderr)
            progress = {}
    else:
        progress = {}

    # Initialize required fields
    now = datetime.datetime.now().isoformat()
    progress.setdefault("started_at", now)
    progress.setdefault("completed_steps", [])
    progress.setdefault("notes", [])
    progress.setdefault("source_lang", "en")
    progress.setdefault("target_lang", "ru")
    progress.setdefault("book", str(temp_dir.name))
    progress.setdefault("glossary_path", str(temp_dir / "glossary.json"))
    progress.setdefault("voice_path", str(temp_dir / "голос_книги.md"))

    # Add step to completed (dedup, preserve order)
    if step not in progress["completed_steps"]:
        progress["completed_steps"].append(step)
        progress["completed_steps"].sort()

    # Increment current_step (don't go beyond 10 = "all done")
    progress["current_step"] = min(step + 1, 10)
    progress["last_completed_at"] = now

    # Update phase
    progress["current_phase"] = PHASE_FOR_STEP.get(progress["current_step"], 3)

    # Parse options
    i = 3
    while i < len(sys.argv):
        arg = sys.argv[i]
        if arg == "--note" and i + 1 < len(sys.argv):
            progress["notes"].append(
                {
                    "step": step,
                    "text": sys.argv[i + 1],
                    "at": now,
                }
            )
            i += 2
        elif arg == "--set" and i + 1 < len(sys.argv):
            kv = sys.argv[i + 1].split("=", 1)
            if len(kv) == 2:
                progress[kv[0]] = kv[1]
            i += 2
        elif arg == "--set-int" and i + 1 < len(sys.argv):
            kv = sys.argv[i + 1].split("=", 1)
            if len(kv) == 2:
                try:
                    progress[kv[0]] = int(kv[1])
                except ValueError:
                    print(f"WARN: --set-int {kv[0]}={kv[1]!r} is not an int, skipping", file=sys.stderr)
            i += 2
        elif arg == "--append-list" and i + 1 < len(sys.argv):
            kv = sys.argv[i + 1].split("=", 1)
            if len(kv) == 2:
                lst = progress.setdefault(kv[0], [])
                if not isinstance(lst, list):
                    print(f"WARN: --append-list {kv[0]}: existing value is not a list, replacing", file=sys.stderr)
                    lst = []
                    progress[kv[0]] = lst
                if kv[1] not in lst:
                    lst.append(kv[1])
            i += 2
        else:
            print(f"WARN: unknown arg: {arg}", file=sys.stderr)
            i += 1

    # Atomic write
    atomic_write_json(progress_path, progress, indent=2, ensure_ascii=False)

    # Print confirmation
    print(f"Saved: {progress_path}")
    print(f"Completed steps: {progress['completed_steps']}")
    print(f"Next step: {progress['current_step']} (phase {progress['current_phase']})")
    if progress["current_step"] > 9:
        print()
        print("ALL STEPS COMPLETE. Workflow finished.")
        print(f"Final outputs in {temp_dir}: book.fb2, output.md, post_mortem.md")
    elif progress["current_step"] in (4, 7):
        print()
        print("⚠️  You just entered a new phase — recommend context compaction")
        print("    to the user before starting the next phase.")


if __name__ == "__main__":
    main()
