"""Common utilities shared across all phase scripts.

Import from here instead of redefining in each script:
    from common import process_dir, run_cmd, sha256_file
    from common import make_term_id, term_id_of, ensure_term_ids
"""

import hashlib
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path


def process_dir(temp_dir: Path) -> Path:
    """Return process subdirectory (creates if needed).

    Layout:
      <temp_dir>/             human-facing
      <temp_dir>/process/     machine-facing
    """
    p = temp_dir / "process"
    p.mkdir(parents=True, exist_ok=True)
    return p


def sha256_file(path: Path) -> str:
    """Return SHA-256 hex digest of a file."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_cmd(cmd: list[str], desc: str = "") -> str:
    """Run a command, return stdout. Raises on non-zero exit."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", check=True)
        return result.stdout
    except subprocess.CalledProcessError as e:
        sys.stderr.write(f"ERROR: {e}\n{e.stderr}\n")
        raise


# ─────────────────────────────────────────────────────────────────────
# Glossary term ids
#
# `id` is a REQUIRED field of every glossary term (run_state.py keys
# entity_hashes by it). Nobody — neither the user nor a sub-agent —
# should have to invent ids by hand: they are derived from the English
# `source` deterministically (lowercase, spaces/punctuation -> "_").
#
#   "Sing Sing"        -> "sing_sing"
#   "Tracker's Lenses" -> "trackers_lenses"
#   "Order of the Broken Lens" -> "order_of_the_broken_lens"
#
# Collisions (two different sources normalizing to the same slug) get a
# numeric suffix: "_2", "_3", ...
# ─────────────────────────────────────────────────────────────────────


def make_term_id(source: str, existing_ids: Iterable[str] = ()) -> str:
    """Derive a stable, unique term id from the English source string.

    Apostrophes are dropped (so "Tracker's" -> "trackers"), every other
    non-alphanumeric run collapses into a single underscore.
    """
    text = (source or "").strip().lower()
    # Drop apostrophes entirely instead of turning them into separators
    for ch in ("'", "\u2019", "\u02bc", "`"):
        text = text.replace(ch, "")

    chars: list[str] = []
    for ch in text:
        chars.append(ch if ch.isalnum() else "_")
    slug = "".join(chars)
    while "__" in slug:
        slug = slug.replace("__", "_")
    slug = slug.strip("_")

    if not slug:
        slug = "term"

    taken = {i for i in existing_ids if i}
    if slug not in taken:
        return slug
    n = 2
    while f"{slug}_{n}" in taken:
        n += 1
    return f"{slug}_{n}"


def term_id_of(term: dict) -> str:
    """Return the term's id, deriving it from `source` when it is missing.

    Read-only helper: callers that need ids persisted should use
    ensure_term_ids().
    """
    tid = term.get("id")
    if isinstance(tid, str) and tid.strip():
        return tid
    return make_term_id(term.get("source", ""))


def ensure_term_ids(terms: list[dict]) -> list[tuple[str, str]]:
    """Fill in missing/duplicate ids in-place. Returns [(source, new_id)].

    Existing non-empty, unique ids are NEVER rewritten: run_state.json
    records entity hashes keyed by id, and renaming ids would make every
    already-translated chunk look "changed" and trigger a full re-run.
    Only missing, empty, or duplicated ids are (re)generated.
    """
    assigned: list[tuple[str, str]] = []
    seen: set[str] = set()

    for term in terms:
        tid = term.get("id")
        if isinstance(tid, str) and tid.strip() and tid not in seen:
            seen.add(tid)
            continue
        new_id = make_term_id(term.get("source", ""), seen)
        term["id"] = new_id
        seen.add(new_id)
        assigned.append((term.get("source", ""), new_id))

    return assigned
