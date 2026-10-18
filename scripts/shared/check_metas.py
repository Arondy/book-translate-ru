"""Check output_chunk*.meta.json files for valid JSON.

This is a TEMPLATE script — run as-is, or copy to <temp_dir>/process/
and customize if needed.

Usage:
    python3 check_metas.py <process_dir_or_temp_dir>
    python3 check_metas.py --file <path/to/output_chunkNNNN.meta.json>

    If given <temp_dir>, checks <temp_dir>/process/.
    If given <temp_dir>/process, checks that dir directly.
    With --file, checks a single meta file (the sub-agent self-check
    from translate_chunk.md before submitting a chunk).

Scans output_chunk*.meta.json files, tries to parse each as JSON.
Reports any malformed files with the parse error. Use after each
translation batch (step 5c) to catch broken meta.json BEFORE merge
(step 6) — merge_meta.py would quarantine malformed metas silently.

Exit codes:
    0 — all checked files are valid JSON
    1 — at least one file is malformed
    2 — nothing to check (path missing / no meta files)
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402,F401  (UTF-8 reconfigure side-effect)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="check_metas.py",
        description="Validate output_chunk*.meta.json files as JSON.",
    )
    parser.add_argument(
        "path",
        nargs="?",
        default=".",
        help="<temp_dir>, <temp_dir>/process, or a directory with metas",
    )
    parser.add_argument(
        "--file",
        dest="single_file",
        metavar="META_JSON",
        help="check one specific meta file instead of scanning a directory",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()

    if args.single_file:
        targets = [Path(args.single_file)]
        if not targets[0].exists():
            print(f"ERROR: {targets[0]} not found", file=sys.stderr)
            return 2
    else:
        arg = Path(args.path)
        pdir = arg / "process" if (arg / "process").is_dir() else arg
        if not pdir.is_dir():
            print(f"ERROR: {pdir} is not a directory", file=sys.stderr)
            return 2
        targets = sorted(pdir.glob("output_chunk*.meta.json"))
        if not targets:
            print(f"ERROR: no output_chunk*.meta.json in {pdir}", file=sys.stderr)
            return 2

    bad = []
    ok_count = 0
    for mp in targets:
        try:
            json.loads(mp.read_text(encoding="utf-8"))
            ok_count += 1
        except json.JSONDecodeError as e:
            bad.append((mp.name, str(e)))

    if bad:
        print(f"BAD metas: {len(bad)}")
        for name, err in bad:
            print(f"  {name}: {err[:120]}")
        return 1

    print(f"OK: all {ok_count} metas are valid JSON")
    return 0


if __name__ == "__main__":
    sys.exit(main())
