"""Build one ready-to-run task file per chunk for translation sub-agents.

The orchestrator used to run `glossary.py print-terms-for-chunk` and
`chunk_context.py` on every chunk itself and paste both outputs into the
sub-agent prompt — that inflated the orchestrator's context, which the
skill explicitly forbids. Instead this script writes, for each chunk, a
task file with:

  - absolute paths to the prompt, the chunk, the output/meta targets and
    the auxiliary JSON (голос книги, narrator hints, structural units);
  - BASE_DIR, so the relative `prompts/...` / `references/...` paths in
    the prompts resolve from the skill root (a sub-agent starts in the
    user's working directory, not the skill directory);
  - the per-chunk term table and the neighbour context, already rendered.

The orchestrator then spawns a sub-agent pointing it at the task file.

Usage:
    python3 prepare_batch.py <temp_dir> [chunkNNNN ...]
    python3 prepare_batch.py <temp_dir> --range 1 6

    Without chunk arguments every chunk in process/ is prepared.
    Chunk ids may be given as "chunk0001" or "0001".

Output: <temp_dir>/process/task_<chunk_id>.md (one per chunk).
"""

import argparse
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "shared"))
sys.path.insert(0, str(_HERE.parent / "phase1_prepare"))
sys.path.insert(0, str(_HERE))

from chunk_context import neighbour_context  # noqa: E402
from common import process_dir  # noqa: E402
from glossary import terms_table_for_chunk  # noqa: E402

SKILL_ROOT = _HERE.parent.parent


def normalize_chunk_id(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("chunk"):
        digits = raw[5:]
    else:
        digits = raw
    if not digits.isdigit():
        raise ValueError(f"bad chunk id: {raw!r}")
    return f"chunk{int(digits):04d}"


def chunk_ids_in_range(pdir: Path, start: int, end: int) -> list[str]:
    ids = sorted(p.stem for p in pdir.glob("chunk*.md"))
    return [c for c in ids if start <= int(c[5:]) <= end]


def build_task(temp_dir: Path, chunk_id: str) -> str:
    pdir = process_dir(temp_dir)
    chunk_file = f"{chunk_id}.md"

    term_table = terms_table_for_chunk(temp_dir, chunk_file) or (
        "Терм-таблица пуста — пропусти правило 2."
    )
    context = neighbour_context(temp_dir, chunk_file) or (
        "Neighbour context пуст — пропусти правило 3."
    )

    def optional(path: Path) -> str:
        return str(path) if path.exists() else "(нет — пропустить)"

    lines = [
        f"# Задача: перевод {chunk_id} (EN → RU)",
        "",
        "Ты — субагент перевода одного чанка. Полная инструкция:",
        f"{SKILL_ROOT / 'prompts' / 'phase2_translate' / 'translate_chunk.md'}",
        "",
        "Прочитай инструкцию целиком и следуй ей. Все относительные пути в",
        f"инструкции (`prompts/...`, `references/...`, `templates/...`) разрешай от BASE_DIR = {SKILL_ROOT}",
        "",
        "## Абсолютные пути",
        "",
        f"- BASE_DIR (skill root): {SKILL_ROOT}",
        f"- Исходный чанк (read-only): {pdir / chunk_file}",
        f"- Записать перевод: {pdir / f'output_{chunk_id}.md'}",
        f"- Записать meta: {pdir / f'output_{chunk_id}.meta.json'}",
        f"- Голос книги: {optional(temp_dir / 'голос_книги.md')}",
        f"- Narrator hints: {optional(pdir / 'narrator_hints.json')}",
        f"- Structural units: {optional(pdir / 'structural_units.json')}",
        f"- Эпиграфы/сноски: {SKILL_ROOT / 'prompts' / 'phase2_translate' / 'эпиграф_обработка.md'}",
        f"- Определение рассказчика: {SKILL_ROOT / 'prompts' / 'phase2_translate' / 'определение_рассказчика.md'}",
        f"- Схема meta.json: {SKILL_ROOT / 'references' / 'meta-json-schema.md'}",
        "",
        "## Терм-таблица (правило 2)",
        "",
        term_table,
        "",
        "## Neighbour context (правило 3, read-only)",
        "",
        context,
        "",
    ]
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="prepare_batch.py",
        description="Write one ready-to-run task file per chunk for translation sub-agents.",
    )
    parser.add_argument("temp_dir")
    parser.add_argument("chunk_ids", nargs="*", help="chunk ids (chunk0001 or 0001)")
    parser.add_argument(
        "--range",
        nargs=2,
        type=int,
        metavar=("START", "END"),
        help="chunk number range (inclusive)",
    )
    return parser


def main():
    args = build_parser().parse_args()
    temp_dir = Path(args.temp_dir)
    pdir = process_dir(temp_dir)

    if args.range:
        chunk_ids = chunk_ids_in_range(pdir, args.range[0], args.range[1])
    elif args.chunk_ids:
        try:
            chunk_ids = [normalize_chunk_id(c) for c in args.chunk_ids]
        except ValueError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        chunk_ids = sorted(p.stem for p in pdir.glob("chunk*.md"))

    if not chunk_ids:
        print(f"ERROR: no chunks found in {pdir}", file=sys.stderr)
        sys.exit(1)

    written = []
    for cid in chunk_ids:
        chunk_path = pdir / f"{cid}.md"
        if not chunk_path.exists():
            print(f"ERROR: {chunk_path.name} not found", file=sys.stderr)
            continue
        task_path = pdir / f"task_{cid}.md"
        task_path.write_text(build_task(temp_dir, cid), encoding="utf-8")
        written.append(task_path)

    for path in written:
        print(path)
    print(f"Prepared {len(written)} task file(s) in {pdir}")


if __name__ == "__main__":
    main()
