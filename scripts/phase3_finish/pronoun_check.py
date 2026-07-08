#!/usr/bin/env python3
"""Collect data for cross-chunk pronoun consistency check (LLM-driven).

This script does NOT make decisions. It assembles data and emits a prompt
that the orchestrator agent should send to a sub-agent (using
prompts/phase3_finish/сверка_местоимений.md). The LLM agent reasons about gender
consistency — no regex, no heuristics in this script.

Outputs (print): JSON payload that should be passed to the sub-agent.

Usage:
    python3 pronoun_check.py <temp_dir> [--save <output.json>]

    --save is optional; if not given, prints to stdout for one-shot use.
"""

import json
import sys
from pathlib import Path

# Ensure UTF-8 output on Windows (cp1251 default breaks on non-ASCII)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "shared"))
from common import process_dir
from config import get_config  # Snippet tunables — loaded from config.toml [pronoun_check] section

_cfg = get_config()
SNIPPET_CHARS = _cfg.get("pronoun_check", "snippet_chars", 800)
SNIPPET_TRUNCATE = _cfg.get("pronoun_check", "snippet_truncate", 200)
SNIPPET_LIMIT = _cfg.get("pronoun_check", "snippet_limit", 20)


def collect(temp_dir: Path) -> dict:
    """Read glossary + meta files + preview snippets into one payload."""
    data: dict = {}

    gl_path = temp_dir / "glossary.json"
    if gl_path.exists():
        from config import read_json_safe

        try:
            data["glossary"] = read_json_safe(gl_path)
        except (json.JSONDecodeError, OSError):
            data["glossary"] = {"terms": []}
    else:
        data["glossary"] = {"terms": []}

    pdir = process_dir(temp_dir)
    from config import read_text_safe

    metas: dict = {}
    for mp in sorted(pdir.glob("output_chunk*.meta.json")):
        cid = mp.name.replace("output_", "").replace(".meta.json", "")
        try:
            metas[cid] = json.loads(read_text_safe(mp))
        except json.JSONDecodeError:
            continue  # skip malformed meta
    data["metas"] = metas

    # Per-chunk text snippets (for the agent's evidence; full text is in
    # the file system, only samples are passed here to keep context small).
    snippets: dict = {}
    for op in sorted(pdir.glob("output_chunk*.md")):
        cid = op.name.replace("output_", "").replace(".md", "")
        if cid not in metas:
            continue
        snippets[cid] = read_text_safe(op)[:SNIPPET_CHARS]
    data["chunk_snippets"] = snippets

    return data


def maybe_truncate(data: dict, temp_dir: Path) -> tuple[dict, bool]:
    """If payload is huge, drop to snippet-only mode. Returns (data, truncated)."""
    payload = json.dumps(data, ensure_ascii=False)
    if len(payload) < 50000:
        return data, False
    # Keep all metas, drop snippets to first N + truncated length
    data["chunk_snippets"] = {k: v[:SNIPPET_TRUNCATE] for k, v in list(data["chunk_snippets"].items())[:SNIPPET_LIMIT]}
    data["_note"] = (
        f"Snippets truncated to first {SNIPPET_LIMIT} chunks × "
        f"{SNIPPET_TRUNCATE} chars. Read full chunks from {temp_dir}/output_chunk*.md"
    )
    return data, True


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    temp_dir = Path(sys.argv[1])
    if not (process_dir(temp_dir) / "manifest.json").exists():
        print(f"ERROR: not a translation temp dir: {temp_dir}")
        sys.exit(1)

    save_path: Path | None = None
    if "--save" in sys.argv:
        idx = sys.argv.index("--save")
        save_path = Path(sys.argv[idx + 1])

    data = collect(temp_dir)
    data, truncated = maybe_truncate(data, temp_dir)

    payload = json.dumps(data, indent=2, ensure_ascii=False)

    if save_path:
        from config import atomic_write_text

        atomic_write_text(save_path, payload, encoding="utf-8")
        print(f"Saved: {save_path}")
        if truncated:
            print(f"  (truncated; read full chunks from {temp_dir})")
        print()
        print("Next steps:")
        print("  1. Read prompts/phase3_finish/сверка_местоимений.md as the sub-agent prompt")
        print(f"  2. Pass {save_path} as the data input to the sub-agent")
        print("  3. Apply the sub-agent's recommended-gender decisions to glossary.json MANUALLY")
        print("     (the script does not auto-apply anything)")
    else:
        print("# Pronoun consistency check — data for sub-agent")
        print("# Sub-agent prompt: prompts/phase3_finish/сверка_местоимений.md")
        print("# The sub-agent returns recommendations; YOU apply them.")
        print()
        print("```json")
        print(payload)
        print("```")
        if truncated:
            print(f"\n[snippets truncated; full chunks in {temp_dir}]")


if __name__ == "__main__":
    main()
