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
                      unchanged_chunk_ids, failed_chunk_ids,
                      polish_failed_chunk_ids, summary}

    python3 run_state.py record <temp_dir> <chunk_id> [<chunk_id> ...]
        Record chunks as completed (writes/updates run_state.json).

    python3 run_state.py mark-failed <temp_dir> <chunk_id> <reason>
        Mark a chunk as failed (after 2 retries). Does not block pipeline.
        `plan` skips such chunks — use only when the TRANSLATION is broken.

    python3 run_state.py mark-polish-failed <temp_dir> <chunk_id> <reason>
        Mark a chunk whose polish pass violated the contract (the
        translation itself is fine and has been restored from `.bak`).
        `plan` does NOT skip these — they stay unchanged.

    python3 run_state.py status <temp_dir>
        Human-readable status snapshot of the run.
"""

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "shared"))
from common import (
    process_dir,
    stable_hash,
    surface_in_text,
    term_id_of,
    term_surface_forms,
)
from glossary_io import GlossaryError
from glossary_io import load_glossary as load_glossary_io

# Helpers


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


def term_fingerprint(term: dict) -> dict:
    """The fields a translation actually depends on.

    `confidence`, `notes`, `frequency`, `id` and run-bookkeeping fields
    change without affecting a single translated sentence. Hashing them
    would mark every chunk "glossary changed" whenever the user confirms
    a term or frequencies are recounted — the whole book would re-translate.
    """
    return {
        "source": term.get("source", ""),
        "target": term.get("target", ""),
        "aliases": term.get("aliases", []) or [],
        "gender": term.get("gender", ""),
    }


def current_entity_hashes(glossary: dict) -> dict[str, str]:
    """{term_id: stable_hash(fingerprint)} for the current glossary.

    `id` is derived from `source` when missing (see common.make_term_id),
    so a hand-edited glossary without ids does not crash the planner.
    """
    return {term_id_of(t): stable_hash(term_fingerprint(t)) for t in glossary.get("terms", [])}


def terms_in_chunk(glossary: dict, chunk_text: str) -> list[str]:
    """Ids of glossary terms that actually occur in this chunk.

    Uses the same surface forms as glossary.py print-terms-for-chunk
    (source + aliases, whole-word match on lowercased text). High-frequency
    terms that merely fill the prompt table are NOT included: a change to a
    term the chunk never mentions must not retranslate it.
    """
    text = chunk_text.lower()
    used: list[str] = []
    for term in glossary.get("terms", []):
        if any(surface_in_text(s, text) for s in term_surface_forms(term)):
            used.append(term_id_of(term))
    return used


# plan


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
                    "polish_failed_chunk_ids": [],
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
    polish_failed_ids: list[str] = []

    for chunk_id in sorted(chunks_in_manifest.keys()):
        chunk_meta = chunks_in_manifest[chunk_id]
        source_path = process_dir(temp_dir) / f"{chunk_id}.md"
        output_path = process_dir(temp_dir) / f"output_{chunk_id}.md"

        # 1. Failed chunks — skip unless explicitly forced
        rec = state_chunks.get(chunk_id, {})
        if rec.get("polish_failed"):
            # Translation is valid; only its polish pass failed. Do NOT skip.
            polish_failed_ids.append(chunk_id)
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
        "polish_failed": len(polish_failed_ids),
    }

    return {
        "translation_chunk_ids": translation_ids,
        "record_only_chunk_ids": record_only_ids,
        "unchanged_chunk_ids": unchanged_ids,
        "failed_chunk_ids": failed_ids,
        "polish_failed_chunk_ids": polish_failed_ids,
        "summary": summary,
    }


# record


def record(temp_dir: Path, chunk_ids: list[str]):
    """Mark chunks as completed with hashes of the terms they actually use."""
    state = load_run_state(temp_dir)
    if "chunks" not in state:
        state["chunks"] = {}

    glossary = load_glossary(temp_dir)
    cur_hashes = current_entity_hashes(glossary)

    recorded = []
    for chunk_id in chunk_ids:
        source_path = process_dir(temp_dir) / f"{chunk_id}.md"
        output_path = process_dir(temp_dir) / f"output_{chunk_id}.md"
        if not output_path.exists():
            print(
                f"WARN: output_{chunk_id}.md not found; recording anyway",
                file=sys.stderr,
            )

        if source_path.exists():
            used_ids = terms_in_chunk(glossary, source_path.read_text(encoding="utf-8"))
        else:
            # No source to inspect — fall back to the full glossary so any
            # later term change still forces a re-check (conservative).
            print(
                f"WARN: {chunk_id}.md not found; recording all glossary terms",
                file=sys.stderr,
            )
            used_ids = list(cur_hashes.keys())

        state["chunks"][chunk_id] = {
            "glossary_version": 2,
            "entity_ids_used": used_ids,
            "entity_hashes": {tid: cur_hashes[tid] for tid in used_ids if tid in cur_hashes},
            "output_exists": output_path.exists(),
            "output_size": (output_path.stat().st_size if output_path.exists() else 0),
            "failed": False,
            "failure_reason": None,
            "polish_failed": False,
            "polish_failure_reason": None,
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


# mark-failed


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


def mark_polish_failed(temp_dir: Path, chunk_id: str, reason: str):
    """Mark a chunk's polish pass as failed without blocking translation.

    Unlike mark-failed, this does NOT set `failed`, so `plan` keeps treating
    the chunk as translated. The orchestrator restores the `.bak` and can
    retry polish or ship the restored draft.
    """
    state = load_run_state(temp_dir)
    if "chunks" not in state:
        state["chunks"] = {}

    rec = state["chunks"].get(chunk_id, {})
    rec["polish_failed"] = True
    rec["polish_failure_reason"] = reason
    state["chunks"][chunk_id] = rec
    save_run_state(temp_dir, state)
    print(
        json.dumps(
            {
                "marked_polish_failed": chunk_id,
                "reason": reason,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


# status


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
    print(f"  polish_failed:{s['polish_failed']:4d}  (translation OK, polish retry needed)")
    print()
    if plan_data["failed_chunk_ids"]:
        print("Failed chunks (need user decision):")
        for cid in plan_data["failed_chunk_ids"]:
            reason = state.get("chunks", {}).get(cid, {}).get("failure_reason", "(no reason)")
            print(f"  {cid}: {reason}")
        print()
    if plan_data["polish_failed_chunk_ids"]:
        print("Polish-failed chunks (translation OK; retry polish or accept restored draft):")
        for cid in plan_data["polish_failed_chunk_ids"]:
            reason = state.get("chunks", {}).get(cid, {}).get("polish_failure_reason", "(no reason)")
            print(f"  {cid}: {reason}")
        print()
    if plan_data["translation_chunk_ids"]:
        print(f"Translation queue ({len(plan_data['translation_chunk_ids'])}):")
        for cid in plan_data["translation_chunk_ids"][:20]:
            print(f"  {cid}")
        rest = len(plan_data["translation_chunk_ids"]) - 20
        if rest > 0:
            print(f"  ... and {rest} more")


# CLI


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_state.py",
        description="Deterministic re-translation planner for book-translate-ru.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_plan = sub.add_parser("plan", help="print the selective re-translation plan")
    p_plan.add_argument("temp_dir")
    p_plan.add_argument(
        "--retranslate-untracked",
        action="store_true",
        help="also re-translate chunks with no run_state record",
    )

    p_record = sub.add_parser("record", help="record chunks as completed")
    p_record.add_argument("temp_dir")
    p_record.add_argument("chunk_ids", nargs="+")

    p_failed = sub.add_parser("mark-failed", help="mark a chunk as failed (translation broken)")
    p_failed.add_argument("temp_dir")
    p_failed.add_argument("chunk_id")
    p_failed.add_argument("reason")

    p_polish = sub.add_parser(
        "mark-polish-failed",
        help="mark a chunk's polish pass as failed (translation is fine)",
    )
    p_polish.add_argument("temp_dir")
    p_polish.add_argument("chunk_id")
    p_polish.add_argument("reason")

    p_status = sub.add_parser("status", help="human-readable status snapshot")
    p_status.add_argument("temp_dir")

    return parser


def main():
    args = build_parser().parse_args()

    if args.command == "plan":
        result = plan(Path(args.temp_dir), retranslate_untracked=args.retranslate_untracked)
        print(json.dumps(result, ensure_ascii=False, indent=2))

    elif args.command == "record":
        record(Path(args.temp_dir), args.chunk_ids)

    elif args.command == "mark-failed":
        mark_failed(Path(args.temp_dir), args.chunk_id, args.reason)

    elif args.command == "mark-polish-failed":
        mark_polish_failed(Path(args.temp_dir), args.chunk_id, args.reason)

    elif args.command == "status":
        status(Path(args.temp_dir))


if __name__ == "__main__":
    main()
