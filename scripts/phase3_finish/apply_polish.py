"""Apply per-chunk polish output (step 8b) — contract check + atomic swap.

The polish sub-agent never touches `output_chunkNNNN.md` directly and never
handles `.bak` or temp-file renaming. It only writes the polished text to
`output_chunkNNNN.polished.md`. This script then:

  1. checks the polish contract on `*.polished.md` against the current
     `output_chunkNNNN.md` (non-empty, length ratio, paragraph count,
     heading count — same settings as verify_polish.py);
  2. on success: writes `output_chunkNNNN.md.bak` (the pre-polish text),
     atomically replaces `output_chunkNNNN.md` with the polished text,
     and removes the `*.polished.md` file;
  3. on failure: leaves both files untouched, reports why, exit code 1.

Keeping the swap in a script means the LLM prompt carries no .bak/rename
instructions and the operation is atomic on Windows.

Usage:
    python3 apply_polish.py <temp_dir>                    # all *.polished.md
    python3 apply_polish.py <temp_dir> chunkNNNN [...]   # given chunks only

Exit codes:
    0 — every processed chunk applied cleanly
    1 — at least one contract check failed (or a named chunk has no polished file)
    2 — temp_dir invalid, or nothing to do

Settings (from config.toml [polish] section):
    ratio_min = 0.5
    ratio_max = 2.0
    max_para_delta_ratio = 0.1
    max_heading_delta = 2
"""

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "shared"))
from common import process_dir
from config import atomic_write_text, get_config

_cfg = get_config()
RATIO_MIN = _cfg.get("polish", "ratio_min", 0.5)
RATIO_MAX = _cfg.get("polish", "ratio_max", 2.0)
MAX_HEADING_DELTA = _cfg.get("polish", "max_heading_delta", 2)
MAX_PARA_DELTA_RATIO = _cfg.get("polish", "max_para_delta_ratio", 0.1)


def count_headings(text: str) -> int:
    return len(re.findall(r"^#{1,2}\s+", text, re.MULTILINE))


def count_paragraphs(text: str) -> int:
    return len([b for b in re.split(r"\n\s*\n", text) if b.strip()])


def contract_errors(original: str, polished: str) -> list[str]:
    """Return a list of contract violations (empty when the swap is safe)."""
    errors: list[str] = []
    if not polished.strip():
        return ["polished file is empty"]

    orig_size = len(original)
    pol_size = len(polished)
    if orig_size == 0:
        return ["original output_chunk file is empty"]

    ratio = pol_size / orig_size
    if ratio < RATIO_MIN:
        errors.append(f"ratio={ratio:.2f} < {RATIO_MIN}")
    if ratio > RATIO_MAX:
        errors.append(f"ratio={ratio:.2f} > {RATIO_MAX}")

    orig_paras = count_paragraphs(original)
    pol_paras = count_paragraphs(polished)
    para_allowance = max(2, round(orig_paras * MAX_PARA_DELTA_RATIO))
    if abs(pol_paras - orig_paras) > para_allowance:
        errors.append(
            f"paragraphs {orig_paras} -> {pol_paras} "
            f"(delta {abs(pol_paras - orig_paras)} > {para_allowance})"
        )

    orig_headings = count_headings(original)
    pol_headings = count_headings(polished)
    if abs(pol_headings - orig_headings) > MAX_HEADING_DELTA:
        errors.append(
            f"headings {orig_headings} -> {pol_headings} "
            f"(delta {abs(pol_headings - orig_headings)} > {MAX_HEADING_DELTA})"
        )

    return errors


def apply_one(pdir: Path, chunk_id: str) -> tuple[str, str]:
    """Apply one chunk. Returns (status, message)."""
    out_path = pdir / f"output_{chunk_id}.md"
    pol_path = pdir / f"output_{chunk_id}.polished.md"
    bak_path = pdir / f"output_{chunk_id}.md.bak"

    if not pol_path.exists():
        return "MISSING_POLISHED", f"{pol_path.name} not found"
    if not out_path.exists():
        return "MISSING_OUT", f"{out_path.name} not found"

    original = out_path.read_text(encoding="utf-8")
    polished = pol_path.read_text(encoding="utf-8")

    errors = contract_errors(original, polished)
    if errors:
        return "FAIL", "; ".join(errors)

    atomic_write_text(bak_path, original)
    atomic_write_text(out_path, polished)
    try:
        pol_path.unlink()
    except OSError:
        pass
    return "OK", f"applied (bak={len(original)}, out={len(polished)})"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="apply_polish.py",
        description="Check the polish contract and swap *.polished.md into place.",
    )
    parser.add_argument("temp_dir")
    parser.add_argument(
        "chunks",
        nargs="*",
        metavar="chunkNNNN",
        help="specific chunks to apply (default: all *.polished.md found)",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    temp_dir = Path(args.temp_dir)
    if not temp_dir.exists():
        print(f"ERROR: temp dir not found: {temp_dir}", file=sys.stderr)
        return 2

    pdir = process_dir(temp_dir)

    if args.chunks:
        chunk_ids = [
            c if c.startswith("chunk") else f"chunk{c}" for c in args.chunks
        ]
    else:
        chunk_ids = sorted(
            f.stem.replace("output_", "").replace(".polished", "")
            for f in pdir.glob("output_chunk*.polished.md")
        )

    if not chunk_ids:
        print(f"ERROR: no *.polished.md files in {pdir}", file=sys.stderr)
        return 2

    ok = 0
    failed: list[tuple[str, str]] = []

    for cid in chunk_ids:
        status, message = apply_one(pdir, cid)
        if status == "OK":
            ok += 1
            print(f"{cid}: OK — {message}")
        else:
            failed.append((cid, f"[{status}] {message}"))
            print(f"{cid}: {status} — {message}")

    print()
    print(f"Applied {ok}/{len(chunk_ids)} chunk(s).")

    if failed:
        print("Failed:")
        for cid, msg in failed:
            print(f"  {cid}: {msg}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
