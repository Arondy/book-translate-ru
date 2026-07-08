#!/usr/bin/env python3
"""Verify polish contract (step 8b.1) across all chunks.

For each chunk with a `.bak` file (saved before polish), checks:
  1. output_chunkNNNN.md exists and > 0 bytes
  2. ratio = len(output) / len(.bak) is within [ratio_min, ratio_max]
  3. |out_headings - bak_headings| <= max_heading_delta

Where headings = count of `^#` and `^##` ATX headings in the file.

If any check fails: prints FAIL with details, exit code 1.
Otherwise: prints OK summary, exit code 0.

Usage:
    python3 verify_polish.py <temp_dir> [--strict]

Settings (from config.toml [polish] section):
    ratio_min = 0.5
    ratio_max = 2.0
    max_heading_delta = 2
"""

import re
import sys
from pathlib import Path

# Ensure UTF-8 output on Windows (cp1251 default breaks on non-ASCII)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "shared"))
from common import process_dir  # Load config from config.toml [polish] section

try:
    from config import get_config

    _cfg = get_config()
    RATIO_MIN = _cfg.get("polish", "ratio_min", 0.5)
    RATIO_MAX = _cfg.get("polish", "ratio_max", 2.0)
    MAX_HEADING_DELTA = _cfg.get("polish", "max_heading_delta", 2)
except Exception:
    # Fallback defaults if config.py isn't importable
    RATIO_MIN = 0.5
    RATIO_MAX = 2.0
    MAX_HEADING_DELTA = 2


def count_headings(text: str) -> int:
    """Count ATX headings (`#` and `##` only — H3+ are body text)."""
    return len(re.findall(r"^#{1,2}\s+", text, re.MULTILINE))


def check_chunk(bak_path: Path, out_path: Path) -> tuple[str, dict]:
    """Check one chunk. Returns (status, details)."""
    if not bak_path.exists():
        return "MISSING_BAK", {"bak_path": str(bak_path)}
    if not out_path.exists():
        return "MISSING_OUT", {"out_path": str(out_path)}

    bak_text = bak_path.read_text(encoding="utf-8")
    out_text = out_path.read_text(encoding="utf-8")

    bak_size = len(bak_text)
    out_size = len(out_text)

    if bak_size == 0:
        return "FAIL", {"reason": "bak is empty", "bak_size": 0, "out_size": out_size}

    ratio = out_size / bak_size
    bak_headings = count_headings(bak_text)
    out_headings = count_headings(out_text)
    headings_delta = abs(out_headings - bak_headings)

    reasons = []
    if ratio < RATIO_MIN:
        reasons.append(f"ratio={ratio:.2f} < {RATIO_MIN}")
    if ratio > RATIO_MAX:
        reasons.append(f"ratio={ratio:.2f} > {RATIO_MAX}")
    if headings_delta > MAX_HEADING_DELTA:
        reasons.append(
            f"headings_delta={headings_delta} > {MAX_HEADING_DELTA} (bak={bak_headings}, out={out_headings})"
        )

    if reasons:
        return "FAIL", {
            "reason": "; ".join(reasons),
            "ratio": ratio,
            "headings_delta": headings_delta,
            "bak_size": bak_size,
            "out_size": out_size,
            "bak_headings": bak_headings,
            "out_headings": out_headings,
        }
    return "OK", {
        "ratio": ratio,
        "headings_delta": headings_delta,
        "bak_size": bak_size,
        "out_size": out_size,
    }


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    temp_dir = Path(sys.argv[1])
    if not temp_dir.exists():
        print(f"ERROR: temp dir not found: {temp_dir}", file=sys.stderr)
        sys.exit(2)

    pdir = process_dir(temp_dir)

    # Find all output_chunk*.md files
    out_files = sorted(pdir.glob("output_chunk*.md"))
    out_files = [f for f in out_files if not f.name.endswith((".bak", ".tmp"))]

    if not out_files:
        print(f"ERROR: no output_chunk*.md files in {pdir}", file=sys.stderr)
        sys.exit(2)

    ok_count = 0
    fail_count = 0
    missing_bak = 0
    missing_out = 0
    fails: list[tuple[str, dict]] = []

    for out_path in out_files:
        chunk_id = out_path.stem.replace("output_", "")
        bak_path = pdir / f"output_{chunk_id}.md.bak"

        status, details = check_chunk(bak_path, out_path)
        if status == "OK":
            ok_count += 1
        elif status == "FAIL":
            fail_count += 1
            fails.append((chunk_id, details))
        elif status == "MISSING_BAK":
            missing_bak += 1
        elif status == "MISSING_OUT":
            missing_out += 1
            fails.append((chunk_id, {"reason": "output_chunk file missing"}))

    total = len(out_files)
    print(f"Polish contract verification: {temp_dir}")
    print(f"  Total chunks:        {total}")
    print(f"  OK:                  {ok_count}")
    print(f"  FAIL:                {fail_count}")
    print(f"  Missing .bak:        {missing_bak} (info only — polish may have been skipped)")
    print(f"  Missing output:      {missing_out}")
    print()

    if fails:
        print("FAILED chunks:")
        for cid, d in fails:
            reason = d.get("reason", "unknown")
            print(f"  {cid}: {reason}")
        print()
        print(f"Summary: {ok_count}/{total} chunks pass polish contract.")
        sys.exit(1)
    else:
        print(f"Summary: {ok_count}/{total} chunks pass polish contract.")
        if missing_bak > 0:
            print(f"  ({missing_bak} chunks had no .bak — polish not run on them)")
        sys.exit(0)


if __name__ == "__main__":
    main()
