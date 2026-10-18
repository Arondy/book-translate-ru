"""Verify polish contract (step 8b.2) across all chunks.

For every output_chunkNNNN.md that has a `.bak` (saved before polish):
  1. output_chunkNNNN.md exists and > 0 bytes
  2. ratio = len(output) / len(.bak) is within [ratio_min, ratio_max]
  3. |ratio - median_ratio| / median_ratio <= median_tolerance
     (median_ratio is computed across the whole book — absolute bounds
     are too wide to notice a chunk that lost half its text)
  4. |out_paragraphs - bak_paragraphs| <= max(2, bak_paragraphs * 0.1)
  5. |out_headings - bak_headings| <= max_heading_delta

A chunk without a `.bak` is a FAILURE, not a silent skip: if polish ran
without leaving a backup, the contract cannot be verified at all.

If any check fails: prints FAIL with details, exit code 1.
Otherwise: prints OK summary, exit code 0.

Usage:
    python3 verify_polish.py <temp_dir> [--strict]

Settings (from config.toml [polish] section):
    ratio_min = 0.5
    ratio_max = 2.0
    median_tolerance = 0.25
    max_para_delta_ratio = 0.1
    max_heading_delta = 2
"""

import re
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "shared"))
from common import chunk_output_files, process_dir
from config import get_config  # Settings from config.toml [polish] section

_cfg = get_config()
RATIO_MIN = _cfg.get("polish", "ratio_min", 0.5)
RATIO_MAX = _cfg.get("polish", "ratio_max", 2.0)
MAX_HEADING_DELTA = _cfg.get("polish", "max_heading_delta", 2)
MEDIAN_TOLERANCE = _cfg.get("polish", "median_tolerance", 0.25)
MAX_PARA_DELTA_RATIO = _cfg.get("polish", "max_para_delta_ratio", 0.1)


def count_headings(text: str) -> int:
    """Count ATX headings (`#` and `##` only — H3+ are body text)."""
    return len(re.findall(r"^#{1,2}\s+", text, re.MULTILINE))


def count_paragraphs(text: str) -> int:
    """Count non-empty blocks separated by blank lines."""
    return len([b for b in re.split(r"\n\s*\n", text) if b.strip()])


def check_chunk(bak_path: Path, out_path: Path, median_ratio: float | None) -> tuple[str, dict]:
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

    bak_paras = count_paragraphs(bak_text)
    out_paras = count_paragraphs(out_text)
    para_delta = abs(out_paras - bak_paras)
    para_allowance = max(2, round(bak_paras * MAX_PARA_DELTA_RATIO))

    reasons = []
    if ratio < RATIO_MIN:
        reasons.append(f"ratio={ratio:.2f} < {RATIO_MIN}")
    if ratio > RATIO_MAX:
        reasons.append(f"ratio={ratio:.2f} > {RATIO_MAX}")
    if median_ratio:
        lo = median_ratio * (1 - MEDIAN_TOLERANCE)
        hi = median_ratio * (1 + MEDIAN_TOLERANCE)
        if ratio < lo or ratio > hi:
            reasons.append(
                f"ratio={ratio:.2f} deviates from book median {median_ratio:.2f} "
                f"(+/-{int(MEDIAN_TOLERANCE * 100)}%)"
            )
    if para_delta > para_allowance:
        reasons.append(
            f"paragraphs {bak_paras} -> {out_paras} (delta {para_delta} > {para_allowance})"
        )
    if headings_delta > MAX_HEADING_DELTA:
        reasons.append(
            f"headings_delta={headings_delta} > {MAX_HEADING_DELTA} (bak={bak_headings}, out={out_headings})"
        )

    if reasons:
        return "FAIL", {
            "reason": "; ".join(reasons),
            "ratio": ratio,
            "headings_delta": headings_delta,
            "para_delta": para_delta,
            "bak_size": bak_size,
            "out_size": out_size,
            "bak_paras": bak_paras,
            "out_paras": out_paras,
            "bak_headings": bak_headings,
            "out_headings": out_headings,
        }
    return "OK", {
        "ratio": ratio,
        "headings_delta": headings_delta,
        "para_delta": para_delta,
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

    # Find all output_chunk*.md files (excludes .bak / .tmp / .polished.md)
    out_files = chunk_output_files(pdir)

    if not out_files:
        print(f"ERROR: no output_chunk*.md files in {pdir}", file=sys.stderr)
        sys.exit(2)

    # Pass 1: ratios of chunks we can measure, for the book-wide median.
    ratios: list[float] = []
    for out_path in out_files:
        chunk_id = out_path.stem.replace("output_", "")
        bak_path = pdir / f"output_{chunk_id}.md.bak"
        if not bak_path.exists():
            continue
        bak_size = len(bak_path.read_text(encoding="utf-8"))
        if bak_size == 0:
            continue
        ratios.append(len(out_path.read_text(encoding="utf-8")) / bak_size)
    median_ratio = statistics.median(ratios) if ratios else None

    ok_count = 0
    fail_count = 0
    missing_bak = 0
    missing_out = 0
    fails: list[tuple[str, dict]] = []

    for out_path in out_files:
        chunk_id = out_path.stem.replace("output_", "")
        bak_path = pdir / f"output_{chunk_id}.md.bak"

        status, details = check_chunk(bak_path, out_path, median_ratio)
        if status == "OK":
            ok_count += 1
        elif status == "FAIL":
            fail_count += 1
            fails.append((chunk_id, details))
        elif status == "MISSING_BAK":
            missing_bak += 1
            fails.append((chunk_id, {"reason": "no .bak — polish contract unverifiable"}))
        elif status == "MISSING_OUT":
            missing_out += 1
            fails.append((chunk_id, {"reason": "output_chunk file missing"}))

    total = len(out_files)
    print(f"Polish contract verification: {temp_dir}")
    print(f"  Total chunks:        {total}")
    print(f"  OK:                  {ok_count}")
    print(f"  FAIL:                {fail_count}")
    print(f"  Missing .bak:        {missing_bak} (cannot verify — treated as failure)")
    print(f"  Missing output:      {missing_out}")
    if median_ratio is not None:
        print(f"  Book median ratio:   {median_ratio:.2f} (+/-{int(MEDIAN_TOLERANCE * 100)}%)")
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
        sys.exit(0)


if __name__ == "__main__":
    main()
