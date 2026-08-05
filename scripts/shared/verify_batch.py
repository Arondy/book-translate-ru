"""Verify translation outputs and metas for a batch of chunks.

This is a TEMPLATE script — run as-is, or copy to <temp_dir>/process/
and customize if needed.

Usage:
    python3 verify_batch.py <temp_dir> [start] [end]

    start, end — chunk number range (default: 1..999 = all)

Checks for each chunk in range:
  1. output_chunkNNNN.md exists
  2. output_chunkNNNN.md > 100 bytes (not empty/truncated)
  3. output_chunkNNNN.meta.json exists (may be empty content, but file must exist)

Prints summary. Use after each translation batch (step 5c) to catch
empty/truncated sub-agent outputs BEFORE merge (step 6).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402,F401  (UTF-8 reconfigure side-effect)

temp_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")
start = int(sys.argv[2]) if len(sys.argv) > 2 else 1
end = int(sys.argv[3]) if len(sys.argv) > 3 else 999
pdir = temp_dir / "process"
missing, short, no_meta = [], [], []
for i in range(start, end + 1):
    cid = f"chunk{i:04d}"
    out = pdir / f"output_{cid}.md"
    meta = pdir / f"output_{cid}.meta.json"
    if not out.exists():
        missing.append(cid)
        continue
    if out.stat().st_size < 100:
        short.append((cid, out.stat().st_size))
    if not meta.exists():
        no_meta.append(cid)
print(f"Checked chunks {start}-{end}:")
print(f"  missing output: {missing or 'none'}")
print(f"  too short (<100b): {short or 'none'}")
print(f"  missing meta:    {no_meta or 'none'}")
