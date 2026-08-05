"""Run-state planner for book-translate-ru skill.

Determines which chunks need to be (re-)translated based on:
  - source chunk hash vs manifest (source changed -> retranslate)
  - output file existence / size (missing or empty -> retranslate)
  - glossary entity_hashes vs recorded (glossary changed for this chunk -> retranslate)
  - run_state.json records (chunk was completed with current glossary -> unchanged)

This is a deterministic planner — no LLM, no heuristics, no semantic
decisions. The orchestrator agent reads the plan and acts on it.

Usage:
    python3 run_state.py plan <temp_dir> [--retranslate-untracked]
        Prints JSON: {translation_chunk_ids, record_only_chunk_ids,
                      unchanged_chunk_ids, failed_chunk_ids, summary}

    python3 run_state.py record <temp_dir> <chunk_id> [<chunk_id> ...]
        Record chunks as completed (writes/updates run_state.json).

    python3 run_state.py mark-failed <temp_dir> <chunk_id> <reason>
        Mark a chunk as failed (after 2 retries). Does not block pipeline.

    python3 run_state.py status <temp_dir>
        Human-readable status snapshot of the run.
"""

import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "shared"))
from common import process_dir, stable_hash, term_id_of
from glossary_io import GlossaryError
from glossary_io import load_glossary as load_glossary_io

# Helpers
# ─────────────────────────────────────────────────────────────────────


def load_json(path: Path, default=None):
    if not path.exists():
        return default
    try:
        from config import read_json_safe

        return read_json_safe(path)
    except (json.JSONDecodeError, OSError):
        return default


def load_run_state(temp_dir: Path) -> dict:
    return load_json(process_dir(temp_dir) / "run_state.json", default={"chunks": {}})


def save_run_state(temp_dir: Path, state: dict):
    # Windows-safe atomic write (retries on PermissionError, fallback)
    from config import atomic_write_json

    pdir = process_dir(temp_dir)
    atomic_write_json(pdir / "run_state.json", state, indent=2, ensure_ascii=False)


def load_glossary(temp_dir: Path) -> dict:
    """Load glossary.json. Fails loudly if the file exists but is broken.

    Returning an empty glossary for a broken file would silently mark every
    chunk as "glossary changed" and trigger a full re-translation — so an
    unreadable glossary is a hard error, not a warning.
    """
    try:
        return load_glossary_io(temp_dir)
    except GlossaryError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


def current_entity_hashes(glossary: dict) -> dict[str, str]:
    """{term_id: stable_hash(term)} for the current glossary.

    `id` is derived from `source` when missing (see common.make_term_id),
    so a hand-edited glossary without ids does not crash the planner.
    """
    return {term_id_of(t): stable_hash(t) for t in glossary.get("terms", [])}


# ─────────────────────────────────────────────────────────────────────
# plan
# ─────────────────────────────────────────────────────────────────────


def plan(temp_dir: Path, retranslate_untracked: bool = False) -> dict:
    """Build the selective re-translation plan.

    Categories:
      translation     — chunk needs (re-)translation
      record_only     — output exists and is valid, but run_state has no
                        record for it; just record without retranslating
      unchanged       — output exists, valid, glossary unchanged vs last run
      failed          — previously marked failed (do not retranslate unless
                        user explicitly forces)
    """
    manifest = load_json(process_dir(temp_dir) / "manifest.json", default=None)
    if not manifest:
        print(
            json.dumps(
                {
                    "error": "manifest.json not found or invalid",
                    "translation_chunk_ids": [],
                    "record_only_chunk_ids": [],
                    "unchanged_chunk_ids": [],
                    "failed_chunk_ids": [],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        sys.exit(1)

    chunks_in_manifest = manifest.get("chunks", {})
    state = load_run_state(temp_dir)
    state_chunks = state.get("chunks", {})

    glossary = load_glossary(temp_dir)
    cur_hashes = current_entity_hashes(glossary)

    translation_ids: list[str] = []
    record_only_ids: list[str] = []
    unchanged_ids: list[str] = []
    failed_ids: list[str] = []

    for chunk_id in sorted(chunks_in_manifest.keys()):
        chunk_meta = chunks_in_manifest[chunk_id]
        source_path = process_dir(temp_dir) / f"{chunk_id}.md"
        output_path = process_dir(temp_dir) / f"output_{chunk_id}.md"

        # 1. Failed chunks — skip unless explicitly forced
        rec = state_chunks.get(chunk_id, {})
        if rec.get("failed") and not retranslate_untracked:
            failed_ids.append(chunk_id)
            continue

        # 2. Source chunk file must exist
        if not source_path.exists():
            translation_ids.append(chunk_id)
            continue

        # 3. Source hash must match manifest
        source_sha = hashlib.sha256(source_path.read_bytes()).hexdigest()
        if source_sha != chunk_meta.get("source", ""):
            translation_ids.append(chunk_id)
            continue

        # 4. Output must exist and be non-trivial
        if not output_path.exists():
            translation_ids.append(chunk_id)
            continue
        output_size = output_path.stat().st_size
        if output_size == 0:
            translation_ids.append(chunk_id)
            continue

        # 5. Glossary hash check
        recorded_hashes = rec.get("entity_hashes", {})
        # If no record at all -> either first run or run_state was wiped
        if not rec:
            if retranslate_untracked:
                translation_ids.append(chunk_id)
            else:
                # Output exists but no run_state record — adopt it
                record_only_ids.append(chunk_id)
            continue

        # Has glossary changed for any term this chunk used?
        used_ids = rec.get("entity_ids_used", [])
        glossary_changed = False
        for tid in used_ids:
            if recorded_hashes.get(tid) != cur_hashes.get(tid):
                glossary_changed = True
                break

        if glossary_changed:
            translation_ids.append(chunk_id)
        else:
            unchanged_ids.append(chunk_id)

    summary = {
        "total_chunks": len(chunks_in_manifest),
        "translation": len(translation_ids),
        "record_only": len(record_only_ids),
        "unchanged": len(unchanged_ids),
        "failed": len(failed_ids),
    }

    return {
        "translation_chunk_ids": translation_ids,
        "record_only_chunk_ids": record_only_ids,
        "unchanged_chunk_ids": unchanged_ids,
        "failed_chunk_ids": failed_ids,
        "summary": summary,
    }


# ─────────────────────────────────────────────────────────────────────
# record
# ─────────────────────────────────────────────────────────────────────


def record(temp_dir: Path, chunk_ids: list[str]):
    """Mark chunks as completed with current glossary hashes."""
    state = load_run_state(temp_dir)
    if "chunks" not in state:
        state["chunks"] = {}

    glossary = load_glossary(temp_dir)
    cur_hashes = current_entity_hashes(glossary)

    recorded = []
    for chunk_id in chunk_ids:
        output_path = process_dir(temp_dir) / f"output_{chunk_id}.md"
        if not output_path.exists():
            print(
                f"WARN: output_{chunk_id}.md not found; recording anyway",
                file=sys.stderr,
            )
        state["chunks"][chunk_id] = {
            "glossary_version": 2,
            "entity_ids_used": [term_id_of(t) for t in glossary.get("terms", [])],
            "entity_hashes": dict(cur_hashes),
            "output_exists": output_path.exists(),
            "output_size": (output_path.stat().st_size if output_path.exists() else 0),
            "failed": False,
            "failure_reason": None,
        }
        recorded.append(chunk_id)

    save_run_state(temp_dir, state)
    print(
        json.dumps(
            {
                "recorded": recorded,
                "count": len(recorded),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


# ─────────────────────────────────────────────────────────────────────
# mark-failed
# ─────────────────────────────────────────────────────────────────────


def mark_failed(temp_dir: Path, chunk_id: str, reason: str):
    state = load_run_state(temp_dir)
    if "chunks" not in state:
        state["chunks"] = {}

    rec = state["chunks"].get(chunk_id, {})
    rec["failed"] = True
    rec["failure_reason"] = reason
    state["chunks"][chunk_id] = rec
    save_run_state(temp_dir, state)
    print(
        json.dumps(
            {
                "marked_failed": chunk_id,
                "reason": reason,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


# ─────────────────────────────────────────────────────────────────────
# status
# ─────────────────────────────────────────────────────────────────────


def status(temp_dir: Path):
    plan_data = plan(temp_dir)
    state = load_run_state(temp_dir)

    manifest = load_json(process_dir(temp_dir) / "manifest.json", default={})
    chunks = manifest.get("chunks", {})

    print(f"Run state snapshot: {temp_dir}")
    print(f"  Total chunks in manifest: {len(chunks)}")
    print(f"  Recorded in run_state:    {len(state.get('chunks', {}))}")
    print()
    print("Plan:")
    s = plan_data["summary"]
    print(f"  translation:  {s['translation']:4d}  (need work)")
    print(f"  record_only:  {s['record_only']:4d}  (output OK, just record)")
    print(f"  unchanged:    {s['unchanged']:4d}  (skip)")
    print(f"  failed:       {s['failed']:4d}  (do not retranslate)")
    print()
    if plan_data["failed_chunk_ids"]:
        print("Failed chunks (need user decision):")
        for cid in plan_data["failed_chunk_ids"]:
            reason = state.get("chunks", {}).get(cid, {}).get("failure_reason", "(no reason)")
            print(f"  {cid}: {reason}")
        print()
    if plan_data["translation_chunk_ids"]:
        print(f"Translation queue ({len(plan_data['translation_chunk_ids'])}):")
        for cid in plan_data["translation_chunk_ids"][:20]:
            print(f"  {cid}")
        rest = len(plan_data["translation_chunk_ids"]) - 20
        if rest > 0:
            print(f"  ... and {rest} more")


# ─────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "plan":
        if len(sys.argv) < 3:
            print("Usage: run_state.py plan <temp_dir> [--retranslate-untracked]")
            sys.exit(1)
        temp_dir = Path(sys.argv[2])
        retranslate_untracked = "--retranslate-untracked" in sys.argv
        result = plan(temp_dir, retranslate_untracked=retranslate_untracked)
        print(json.dumps(result, ensure_ascii=False, indent=2))

    elif cmd == "record":
        if len(sys.argv) < 4:
            print("Usage: run_state.py record <temp_dir> <chunk_id> [<chunk_id> ...]")
            sys.exit(1)
        temp_dir = Path(sys.argv[2])
        chunk_ids = sys.argv[3:]
        record(temp_dir, chunk_ids)

    elif cmd == "mark-failed":
        if len(sys.argv) < 5:
            print("Usage: run_state.py mark-failed <temp_dir> <chunk_id> <reason>")
            sys.exit(1)
        temp_dir = Path(sys.argv[2])
        chunk_id = sys.argv[3]
        reason = sys.argv[4]
        mark_failed(temp_dir, chunk_id, reason)

    elif cmd == "status":
        if len(sys.argv) < 3:
            print("Usage: run_state.py status <temp_dir>")
            sys.exit(1)
        status(Path(sys.argv[2]))

    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
