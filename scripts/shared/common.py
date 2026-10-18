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
import unicodedata
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


# Suffixes of transient chunk files that must never be treated as the real
# `output_chunkNNNN.md`: pre-polish backups, atomic-write leftovers and the
# polish sub-agent's output (swapped in by apply_polish.py).
_CHUNK_OUTPUT_EXCLUDE = (".bak", ".tmp", ".polished.md")


def chunk_output_files(pdir: Path) -> list[Path]:
    """Return the real `output_chunkNNNN.md` files in <pdir>, sorted.

    Excludes `.bak`, `.tmp` and `.polished.md` companions so a lingering
    polish artifact cannot be merged, verified or counted twice.
    """
    files = sorted(pdir.glob("output_chunk*.md"))
    return [f for f in files if not f.name.endswith(_CHUNK_OUTPUT_EXCLUDE)]


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


# Glossary surface matching
#
# Single source of truth for "does this term appear in this text".
# Used by glossary.py (frequency count, per-chunk table) and run_state.py
# (which terms a translated chunk actually used).
#
# Word boundary that works for names with apostrophes (O'Connor) and
# hyphens (Wit-Bit). Standard \b breaks on these because ' and - are not
# \w, so the boundary fires mid-name. We treat any alphanumeric char
# (incl. Cyrillic) as "inside a word" and everything else as a boundary.


_BOUNDARY_START = r"(?<![A-Za-zА-Яа-я0-9])"
_BOUNDARY_END = r"(?![A-Za-zА-Яа-я0-9])"


def term_surface_forms(term: dict) -> list[str]:
    """Lowercased source + aliases of a term, empty forms removed."""
    forms = [str(term.get("source", "")).lower()]
    for alias in term.get("aliases", []) or []:
        forms.append(str(alias).lower())
    return [f for f in forms if f]


def surface_in_text(surface: str, text: str) -> bool:
    """True if surface appears as a whole word in text.

    For surfaces of length <= 2 we fall back to plain substring match
    because word-boundary regex on a 1-char name is noisy either way.
    """
    if not surface:
        return False
    if len(surface) <= 2:
        return surface in text
    pattern = _BOUNDARY_START + re.escape(surface) + _BOUNDARY_END
    return bool(re.search(pattern, text, re.IGNORECASE))


def relevant_terms(glossary: dict, text: str, top_n: int | None = None) -> list[dict]:
    """Glossary terms relevant to one chunk, in frequency order.

    A term is relevant when it appears in `text` (by any surface form) or
    is among the top-N most frequent terms of the book. `text` must be
    lowercased by the caller, exactly like glossary.py does.

    Shared by glossary.py (per-chunk prompt table) and prepare_batch.py so
    both build the same table.
    """
    if top_n is None:
        top_n = glossary.get("high_frequency_top_n", 20)
    sorted_terms = sorted(
        glossary.get("terms", []),
        key=lambda t: t.get("frequency", 0),
        reverse=True,
    )
    result: list[dict] = []
    for idx, term in enumerate(sorted_terms):
        if idx < top_n or any(surface_in_text(s, text) for s in term_surface_forms(term)):
            result.append(term)
    return result


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


# Words in two alphabets
#
# A translation model occasionally switches keyboards mid-word: "dошло",
# "Gород", "taкого". No LLM pass notices it — the model reads "gолосом"
# as "голосом". Detection only: the text is never changed here, the fix
# belongs to the translating sub-agent (the cause is upstream, in the
# prompt or the model).


# Target alphabet -> the alphabet whose letters inside its words are a slip.
_SLIP_ALPHABET = {"CYRILLIC": "LATIN", "GREEK": "LATIN", "LATIN": "CYRILLIC"}

_MIXED_WORD_RE = re.compile(r"[^\W\d_]+")  # letters only, Unicode-aware


def _char_alphabet(ch: str) -> str:
    """Return 'LATIN' / 'CYRILLIC' / 'GREEK' for a letter, else ''."""
    name = unicodedata.name(ch, "")
    for alphabet in ("LATIN", "CYRILLIC", "GREEK"):
        if name.startswith(alphabet):
            return alphabet
    return ""


def find_mixed_words(
    text: str,
    script: str = "CYRILLIC",
    protected: Iterable[str] = (),
) -> list[tuple[int, str]]:
    """Find words mixing the target alphabet with a foreign one.

    A word is reported when it contains letters of `script` (the alphabet
    of the target language, default "CYRILLIC" for Russian) AND of the
    alphabet that is a slip in it (Latin). All-Latin words inside Cyrillic
    text (names, acronyms like "FBI") are single-alphabet and therefore
    not reported.

    `protected` are words that are intentionally mixed (e.g. glossary
    targets); they are skipped case-insensitively.

    Returns a list of (start_offset, word) in document order.
    """
    slip = _SLIP_ALPHABET.get(script)
    if not slip or not text:
        return []
    protected_set = {w.casefold() for w in protected if w}
    mixed: list[tuple[int, str]] = []
    for m in _MIXED_WORD_RE.finditer(text):
        word = m.group()
        alphabets = {_char_alphabet(ch) for ch in word}
        if script in alphabets and slip in alphabets and word.casefold() not in protected_set:
            mixed.append((m.start(), word))
    return mixed


# Table-of-contents removal
#
# A TOC survives the EPUB -> Markdown step in several shapes: Markdown
# links, list items, or — once the phase-1 cleaner has unlinked Calibre
# anchors — a run of bare headings. Detection must not rely on links
# alone, and must stop at the first heading followed by real content
# rather than at a heading level (a same-level TOC would otherwise eat
# the book).


_TOC_HEADING_RE = re.compile(
    r"^#{1,6}\s+(СОДЕРЖАНИЕ|Содержание|Contents|Table of Contents|TOC|"
    r"Оглавление|ОГЛАВЛЕНИЕ)\s*(?:\{[^}]*\})?\s*$",
    re.IGNORECASE,
)

_HEADING_RE = re.compile(r"^#{1,6}\s+")


def _looks_like_toc_entry(text: str) -> bool:
    s = text.strip()
    if not s:
        return True
    if "](#" in s or re.search(r"\[#?[\w\-]+\]", s) or s.startswith("<a "):
        return True
    if re.match(r"^[\d\-\*+]\.?\s", s):
        return True
    if _HEADING_RE.match(s):
        return True
    # A bare short line without sentence punctuation: a chapter title or a
    # TOC entry — indistinguishable on its own, hence the lookahead below.
    if len(s) <= 120 and not s.endswith((".", "!", "?", "…", ":", ";", "»")):
        return True
    return False


def _next_nonblank(lines: list[str], start: int) -> int:
    j = start
    while j < len(lines) and not lines[j].strip():
        j += 1
    return j


def strip_toc_block(md_text: str) -> str:
    """Remove a table-of-contents block (its heading and all entries).

    Entries may be links, list items, or bare headings (after the phase-1
    cleaner unlinks Calibre anchors). The block ends at the first heading
    that is followed by non-entry content — that heading is the first real
    chapter and is kept.

    Shared by convert.py (phase 1) and merge_and_build.py (phase 3).
    """
    lines = md_text.split("\n")
    out: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if not _TOC_HEADING_RE.match(line.strip()):
            out.append(line)
            i += 1
            continue

        # The heading is a TOC only when an entry follows it.
        j = _next_nonblank(lines, i + 1)
        if j >= n or not _looks_like_toc_entry(lines[j]):
            out.append(line)
            i += 1
            continue

        # Skip the heading and its entries; stop at the first heading that
        # is followed by real content, or at the first non-entry line.
        i += 1
        while i < n:
            s = lines[i].strip()
            if not s:
                i += 1
                continue
            if _HEADING_RE.match(s):
                nxt = _next_nonblank(lines, i + 1)
                if nxt < n and _looks_like_toc_entry(lines[nxt]):
                    i += 1  # a TOC entry rendered as a heading
                    continue
                break  # the first real chapter heading
            if _looks_like_toc_entry(s):
                i += 1
                continue
            break
    return "\n".join(out)
