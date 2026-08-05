"""Common utilities shared across all phase scripts.

Import from here instead of redefining in each script:
    from common import process_dir, run_cmd, sha256_file
    from common import make_term_id, term_id_of, ensure_term_ids
"""

import hashlib
import json
import re
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path

# Ensure UTF-8 output on Windows (cp1251 default breaks on non-ASCII)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")


def stable_hash(obj) -> str:
    """Deterministic hash across Python processes (unlike built-in hash()).

    Built-in hash() for strings is randomized via PYTHONHASHSEED between
    process runs, which would make run_state.json entity_hashes unusable
    for resume: every new orchestrator session would see 'glossary changed'
    for every chunk and re-translate the whole book.
    """
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


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


def run_cmd(cmd: list[str], desc: str = "", log_desc: bool = False) -> str:
    """Run a command, return stdout. Raises on non-zero exit.

    With log_desc=True a progress line is written to stderr first
    (format preserved from convert.py: "[convert] <desc-or-cmd>"), so
    callers that previously logged the command keep their stderr output.
    """
    if log_desc:
        sys.stderr.write(f"[convert] {desc or ' '.join(cmd)}\n")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", check=True)
        return result.stdout
    except subprocess.CalledProcessError as e:
        sys.stderr.write(f"[convert] ERROR: {e}\n{e.stderr}\n")
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
    text = text.translate(str.maketrans("", "", "'\u2019\u02bc`"))

    chars: list[str] = []
    for ch in text:
        chars.append(ch if ch.isalnum() else "_")
    slug = "".join(chars)
    slug = re.sub(r"_+", "_", slug)
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


def find_english_leaks(text: str, min_chars: int = 80) -> list[tuple[int, int, str]]:
    """Find runs of pure-ASCII alphabetic characters likely to be untranslated
    English text. Returns list of (start, end, snippet).

    `min_chars` is the minimum run length; the caller decides the value
    (usually from config.toml [quality].en_leak_chars).
    """
    leaks = []
    pattern = re.compile(r"[A-Za-z][A-Za-z\s.,;:'\"!?\-\(\)\[\]]{" + str(min_chars - 1) + r",}")
    for m in pattern.finditer(text):
        # Filter out cases that look like punctuation/code/markdown directives
        snippet = m.group(0)
        # require at least one full English word > 3 letters
        if re.search(r"\b[A-Za-z]{4,}\b", snippet):
            leaks.append((m.start(), m.end(), snippet))
    return leaks
