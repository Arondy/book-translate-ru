"""Shared glossary.json I/O (strict load/save) for book-translate-ru skill.

Single canonical implementation of glossary persistence, used by
glossary.py (CLI facade), merge_meta.py (transactional merge) and
run_state.py (planner). Semantics are the ones proven in glossary.py:

  - strict loading: missing file -> empty default; broken JSON / no
    `terms` / no `version` without `terms` -> GlossaryError; wrong
    `version` with `terms` -> migrate with WARN;
  - refuse to write an empty term list over a non-empty file on disk;
  - `ensure_term_ids` on load and save.

Data-loss safety (see references/meta-json-schema.md, "Схема glossary.json"):
glossary.json is a human-facing file. We NEVER silently replace a
broken/unknown-schema glossary with an empty default and NEVER write an
empty term list over a non-empty one — we fail loudly instead, so a bad
write by a sub-agent cannot destroy collected terms.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import ensure_term_ids  # noqa: E402
from config import atomic_write_json, get_config, read_json_safe  # noqa: E402

GLOSSARY_FILE = "glossary.json"  # lives in temp_dir root (human-facing)
GLOSSARY_VERSION = 2  # top-level "version" — MANDATORY, see load_glossary()


class GlossaryError(Exception):
    """Fatal problem with glossary.json — never swallowed, never auto-healed.

    Raised instead of returning an empty default glossary: an empty default
    would be silently written back over a real (non-empty) glossary by the
    next save, destroying collected terms.
    """


def _default_glossary() -> dict:
    """Fresh empty glossary — ONLY for the case 'file does not exist yet'."""
    return {
        "version": GLOSSARY_VERSION,
        "terms": [],
        "high_frequency_top_n": get_config().get("glossary", "high_frequency_top_n", 20),
        "applied_meta_hashes": {},
    }


def disk_terms_count(temp_dir: Path) -> int | None:
    """How many terms are physically on disk right now.

    Returns None when the file is missing or unreadable/unparsable.
    Deliberately schema-agnostic: it must see terms even in a file that
    load_glossary() would reject (that is the whole point of the guard).
    """
    path = temp_dir / GLOSSARY_FILE
    if not path.exists():
        return None
    try:
        data = read_json_safe(path)
    except (json.JSONDecodeError, OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    terms = data.get("terms")
    if not isinstance(terms, list):
        return None
    return len(terms)


def load_glossary(temp_dir: Path) -> dict:
    """Load glossary.json strictly.

    - file missing            -> fresh empty glossary (the only empty case)
    - unreadable/invalid JSON -> GlossaryError
    - missing "version": 2 but `terms` present -> migrated to v2 (warning)
    - anything else           -> GlossaryError

    NEVER returns an empty default for an existing file.
    """
    path = temp_dir / GLOSSARY_FILE
    if not path.exists():
        return _default_glossary()

    try:
        data = read_json_safe(path)
    except (json.JSONDecodeError, OSError, ValueError) as e:
        raise GlossaryError(
            f"{path} существует, но не читается как JSON: {e}\n"
            f"  Почини файл вручную (json.loads + json.dumps, см. "
            f"scripts/shared/edit_glossary_template.py) и повтори команду.\n"
            f"  Пустой глоссарий вместо него НЕ подставляется — иначе следующая "
            f"запись затрёт собранные термины."
        ) from e

    if not isinstance(data, dict):
        raise GlossaryError(f"{path}: top-level JSON должен быть объектом, а не {type(data).__name__}.")

    terms = data.get("terms")
    version = data.get("version")

    if version != GLOSSARY_VERSION:
        if isinstance(terms, list):
            # Migrate: the payload looks like a v2 glossary, the key is just missing.
            print(
                f"WARN: {path}: отсутствует или неверен top-level \"version\" "
                f"(got {version!r}); файл содержит {len(terms)} терминов — "
                f"мигрирую как version={GLOSSARY_VERSION}.",
                file=sys.stderr,
            )
            data["version"] = GLOSSARY_VERSION
        else:
            raise GlossaryError(
                f"{path}: нет top-level \"version\": {GLOSSARY_VERSION} и нет массива \"terms\".\n"
                f"  Это не глоссарий v2. Проверь файл: возможно, его перезаписал "
                f"субагент без обязательного ключа \"version\".\n"
                f"  Схема — references/meta-json-schema.md, раздел «Схема glossary.json (v2)».\n"
                f"  Валидация: python3 glossary.py validate-glossary \"<temp_dir>\""
            )

    if not isinstance(data.get("terms"), list):
        raise GlossaryError(
            f"{path}: \"terms\" отсутствует или не является массивом.\n"
            f"  Пустой глоссарий не подставляется. Проверь файл вручную."
        )

    data.setdefault("high_frequency_top_n", get_config().get("glossary", "high_frequency_top_n", 20))
    data.setdefault("applied_meta_hashes", {})

    # ids are derived, not hand-written: fill in whatever is missing
    ensure_term_ids(data["terms"])

    return data


def save_glossary(temp_dir: Path, glossary: dict, *, allow_empty: bool = False):
    """Save glossary.json using Windows-safe atomic write.

    Guards against the two destructive writes that already cost us a
    glossary once:
      1. writing a payload without "version": 2 (would be unreadable and
         then silently replaced by an empty default);
      2. writing an EMPTY term list over a non-empty file on disk.
    """
    if not isinstance(glossary, dict) or not isinstance(glossary.get("terms"), list):
        raise GlossaryError("save_glossary: payload не является глоссарием (нет массива \"terms\").")

    glossary["version"] = GLOSSARY_VERSION
    ensure_term_ids(glossary["terms"])

    if not glossary["terms"] and not allow_empty:
        on_disk = disk_terms_count(temp_dir)
        if on_disk:
            raise GlossaryError(
                f"ОТКАЗ ОТ ЗАПИСИ: в памяти 0 терминов, а на диске их {on_disk}.\n"
                f"  Похоже на проблему со схемой (обычно — отсутствует top-level "
                f"\"version\": {GLOSSARY_VERSION} в glossary.json).\n"
                f"  Проверь файл: python3 glossary.py validate-glossary \"{temp_dir}\"\n"
                f"  glossary.json НЕ изменён."
            )

    atomic_write_json(temp_dir / GLOSSARY_FILE, glossary, indent=2, ensure_ascii=False)
