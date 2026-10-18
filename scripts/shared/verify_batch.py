"""Verify translation outputs and metas for a batch of chunks.

Usage:
    python3 verify_batch.py <temp_dir> [start] [end]

    start, end — optional chunk number range (inclusive). Without a range
    every chunk listed in manifest.json is checked (no phantom chunk ids).

Checks for each chunk:
  1. output_chunkNNNN.md exists
  2. output_chunkNNNN.md has at least [retry].min_output_chars CHARACTERS
     (not bytes — the config value is characters)
  3. length ratio output/source is within [quality].ratio_min..ratio_max
     (source size comes from manifest.json)
  4. output_chunkNNNN.meta.json exists

Exit codes:
  0 — every chunk passed
  1 — at least one chunk failed
  2 — temp_dir invalid / manifest.json missing or unreadable

Run after each translation batch (step 5c) to catch empty/truncated
sub-agent outputs BEFORE merge (step 6).
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import process_dir  # noqa: E402,F401  (UTF-8 reconfigure side-effect)
from config import get_config, read_json_safe  # noqa: E402

_cfg = get_config()
MIN_OUTPUT_CHARS = _cfg.get("retry", "min_output_chars", 100)
RATIO_MIN = _cfg.get("quality", "ratio_min", 0.6)
RATIO_MAX = _cfg.get("quality", "ratio_max", 2.0)


def chunk_number(chunk_id: str) -> int:
    """'chunk0042' -> 42, or -1 when the id has no digits."""
    digits = "".join(ch for ch in chunk_id if ch.isdigit())
    return int(digits) if digits else -1


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2

    temp_dir = Path(sys.argv[1])
    start = int(sys.argv[2]) if len(sys.argv) > 2 else None
    end = int(sys.argv[3]) if len(sys.argv) > 3 else None

    pdir = process_dir(temp_dir)
    manifest_path = pdir / "manifest.json"
    if not manifest_path.exists():
        print(f"ERROR: manifest.json not found in {pdir}", file=sys.stderr)
        return 2

    try:
        manifest = read_json_safe(manifest_path)
    except (json.JSONDecodeError, OSError) as e:
        print(f"ERROR: cannot parse manifest.json: {e}", file=sys.stderr)
        return 2

    chunks = manifest.get("chunks", {})
    chunk_ids = sorted(chunks.keys(), key=chunk_number)
    if start is not None or end is not None:
        lo = start if start is not None else -10**9
        hi = end if end is not None else 10**9
        chunk_ids = [c for c in chunk_ids if lo <= chunk_number(c) <= hi]

    if not chunk_ids:
        print("ERROR: no chunks in the requested range", file=sys.stderr)
        return 2

    missing, short, ratio_bad, no_meta = [], [], [], []
    for cid in chunk_ids:
        src_size = chunks.get(cid, {}).get("size", 0)
        out_path = pdir / f"output_{cid}.md"
        meta_path = pdir / f"output_{cid}.meta.json"

        if not out_path.exists():
            missing.append(cid)
            continue

        out_len = len(out_path.read_text(encoding="utf-8", errors="replace"))
        if out_len < MIN_OUTPUT_CHARS:
            short.append((cid, out_len))
        elif src_size > 0:
            ratio = out_len / src_size
            if ratio < RATIO_MIN or ratio > RATIO_MAX:
                ratio_bad.append((cid, round(ratio, 2)))

        if not meta_path.exists():
            no_meta.append(cid)

    print(f"Checked {len(chunk_ids)} chunk(s):")
    print(f"  missing output:      {missing or 'none'}")
    print(f"  too short (<{MIN_OUTPUT_CHARS} chars): {short or 'none'}")
    print(f"  ratio out of range:  {ratio_bad or 'none'}")
    print(f"  missing meta:        {no_meta or 'none'}")

    if missing or short or ratio_bad or no_meta:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
