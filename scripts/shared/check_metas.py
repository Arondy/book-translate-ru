"""Check all output_chunk*.meta.json files for valid JSON.

This is a TEMPLATE script — run as-is, or copy to <temp_dir>/process/
and customize if needed.

Usage:
    python3 check_metas.py <process_dir_or_temp_dir>

    If given <temp_dir>, checks <temp_dir>/process/.
    If given <temp_dir>/process, checks that dir directly.

Scans all output_chunk*.meta.json files, tries to parse each as JSON.
Reports any malformed files with the parse error. Use after each
translation batch (step 5c) to catch broken meta.json BEFORE merge
(step 6) — merge_meta.py would quarantine malformed metas silently.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402,F401  (UTF-8 reconfigure side-effect)

arg = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")
# If given a temp_dir (has process/ subdir), use process/; else use as-is
if (arg / "process").is_dir():
    pdir = arg / "process"
else:
    pdir = arg

bad = []
ok_count = 0
for mp in sorted(pdir.glob("output_chunk*.meta.json")):
    try:
        json.loads(mp.read_text(encoding="utf-8"))
        ok_count += 1
    except json.JSONDecodeError as e:
        bad.append((mp.name, str(e)))

if bad:
    print(f"BAD metas: {len(bad)}")
    for name, err in bad:
        print(f"  {name}: {err[:120]}")
else:
    print(f"OK: all {ok_count} metas are valid JSON")
