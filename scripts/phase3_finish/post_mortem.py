"""Collect data for post-mortem analysis (LLM-driven).

This script does NOT analyze quality. It assembles data and emits a prompt
that the orchestrator agent should send to a sub-agent (using
prompts/phase3_finish/пост_мортем.md). The LLM agent reasons about quality — no regex,
no heuristics in this script.

Usage:
    python3 post_mortem.py <temp_dir> [--save <output.json>]

    --save is optional; if not given, prints to stdout for one-shot use.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "shared"))
from common import process_dir
from config import get_config  # When total data is huge, aggregate per-chunk data into chapter-level stats

# Loaded from config.toml [post_mortem] section
AGGREGATE_THRESHOLD = get_config().get("post_mortem", "aggregate_threshold", 80000)


def collect(temp_dir: Path) -> dict:
    """Collect meta, qa, manifest, structural_units into one payload."""
    data: dict = {}
    pdir = process_dir(temp_dir)
    from config import read_json_safe

    manifest_path = pdir / "manifest.json"
    if manifest_path.exists():
        try:
            manifest_data = read_json_safe(manifest_path)
            chunks = manifest_data.get("chunks", {})
            data["manifest_summary"] = {"chunk_count": len(chunks)}
        except (json.JSONDecodeError, KeyError, OSError):
            data["manifest_summary"] = {"chunk_count": 0, "parse_error": True}

    sections_path = pdir / "chunk_sections.json"
    if sections_path.exists():
        try:
            data["chunk_sections"] = read_json_safe(sections_path)
        except (json.JSONDecodeError, OSError):
            pass

    structural_path = pdir / "structural_units.json"
    if structural_path.exists():
        try:
            data["structural_units"] = read_json_safe(structural_path)
        except (json.JSONDecodeError, OSError):
            pass

    narrator_path = pdir / "narrator_hints.json"
    if narrator_path.exists():
        try:
            narrator_data = read_json_safe(narrator_path)
            data["narrator_hints_summary"] = {"arc_count": narrator_data.get("arc_count", 0)}
        except (json.JSONDecodeError, OSError):
            pass

    # Per-chunk meta
    metas = {}
    for mp in sorted(pdir.glob("output_chunk*.meta.json")):
        cid = mp.name.replace("output_", "").replace(".meta.json", "")
        try:
            metas[cid] = read_json_safe(mp)
        except (json.JSONDecodeError, OSError):
            continue
    data["metas"] = metas

    # Per-chunk qa
    qas = {}
    for qp in sorted(pdir.glob("output_chunk*.qa.json")):
        cid = qp.name.replace("output_", "").replace(".qa.json", "")
        try:
            qas[cid] = read_json_safe(qp)
        except (json.JSONDecodeError, OSError):
            continue
    data["qas"] = qas

    return data


def maybe_aggregate(data: dict) -> tuple[dict, bool]:
    """If payload too large, keep only summarized counts (no per-chunk bodies)."""
    payload = json.dumps(data, ensure_ascii=False)
    if len(payload) < AGGREGATE_THRESHOLD:
        return data, False

    # Aggregate metas
    meta_counts = {}
    for cid, m in data.get("metas", {}).items():
        meta_counts[cid] = {
            "n_new_entities": len(m.get("new_entities", [])),
            "n_alias_hypotheses": len(m.get("alias_hypotheses", [])),
            "n_conflicts": len(m.get("conflicts", [])),
            "narrator_known": m.get("narrator_identification") is not None,
            "narrator_confidence": (
                m.get("narrator_identification", {}).get("confidence") if m.get("narrator_identification") else None
            ),
        }
    data["metas_summary"] = meta_counts
    data.pop("metas", None)

    # Aggregate qas
    qa_counts = {}
    for cid, q in data.get("qas", {}).items():
        issues = q.get("issues", [])
        sev = {"high": 0, "medium": 0, "low": 0}
        cats = {}
        for issue in issues:
            sev[issue.get("severity", "medium")] = sev.get(issue.get("severity", "medium"), 0) + 1
            cat = issue.get("category", "other")
            cats[cat] = cats.get(cat, 0) + 1
        qa_counts[cid] = {
            "severity_counts": sev,
            "category_counts": cats,
        }
    data["qas_summary"] = qa_counts
    data.pop("qas", None)

    data["_note"] = (
        "Per-chunk meta/qa aggregated to severity/category counts due to size. "
        "Read full files from <temp_dir>/output_chunk*.meta.json and .qa.json "
        "for chunks flagged in the report."
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
    data, aggregated = maybe_aggregate(data)

    payload = json.dumps(data, indent=2, ensure_ascii=False)

    if save_path:
        from config import atomic_write_text

        atomic_write_text(save_path, payload, encoding="utf-8")
        print(f"Saved: {save_path}")
        if aggregated:
            print("  (aggregated per-chunk data; full files in temp_dir)")
        print()
        print("Next steps:")
        print("  1. Read prompts/phase3_finish/пост_мортем.md as the sub-agent prompt")
        print(f"  2. Pass {save_path} to the sub-agent")
        print("  3. Sub-agent returns a Markdown report; save it as post_mortem.md")
    else:
        print("# Post-mortem analysis — data for sub-agent")
        print("# Sub-agent prompt: prompts/phase3_finish/пост_мортем.md")
        print()
        print("```json")
        print(payload)
        print("```")
        if aggregated:
            print(f"\n[data aggregated; full files in {temp_dir}]")


if __name__ == "__main__":
    main()
