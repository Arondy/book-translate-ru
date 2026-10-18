"""Merge translated chunks and build FB2 output.

Usage:
    python3 merge_and_build.py --temp-dir <temp_dir> --title "Название" [--author "Автор"]

Flags:
    --no-fb2          Skip FB2 generation (still writes output.md)
    --pandoc-fallback Use Pandoc for FB2 (fallback if fb2_builder fails)
    --genre           FB2 genre code (default: prose_counter)
"""

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "shared"))
from common import chunk_output_files, process_dir, run_cmd, strip_toc_block


def merge_chunks(temp_dir: Path) -> tuple[str, list[str]]:
    """Merge all output_chunk*.md -> single string."""
    pdir = process_dir(temp_dir)
    chunk_files = chunk_output_files(pdir)
    if not chunk_files:
        print("ERROR: No output chunks found", file=sys.stderr)
        sys.exit(1)

    parts: list[str] = []
    warnings: list[str] = []
    for cf in chunk_files:
        text = cf.read_text(encoding="utf-8").strip()
        if not text:
            warnings.append(f"EMPTY: {cf.name}")
            continue
        parts.append(text)

    merged = "\n\n".join(parts)
    merged = strip_toc_block(merged)
    output_path = temp_dir / "output.md"
    from config import atomic_write_text

    atomic_write_text(output_path, merged, encoding="utf-8")
    print(f"Merged {len(chunk_files)} chunks -> output.md ({len(merged)} chars)")
    return merged, warnings


def validate_manifest(temp_dir: Path) -> tuple[bool, list[str]]:
    """Validate that all source chunks have matching outputs."""
    manifest_path = process_dir(temp_dir) / "manifest.json"
    if not manifest_path.exists():
        print("WARNING: manifest.json not found, skipping validation")
        return True, []

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    issues: list[str] = []
    pdir = process_dir(temp_dir)

    for chunk_id, info in manifest.get("chunks", {}).items():
        output_path = pdir / f"output_{chunk_id}.md"
        if not output_path.exists():
            issues.append(f"Missing: output_{chunk_id}.md")
        elif output_path.stat().st_size == 0:
            issues.append(f"Empty: output_{chunk_id}.md")

    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "phase1_prepare"))
        import importlib

        glossary_mod = importlib.import_module("glossary")
        sanity = glossary_mod.content_sanity_report(temp_dir, manifest)
        ratio_count = leak_count = 0
        for chunk_id, chunk_issues in sanity.items():
            for issue in chunk_issues:
                if issue.startswith("RATIO_"):
                    ratio_count += 1
                elif issue.startswith("ENGLISH_LEAK"):
                    leak_count += 1
        if ratio_count:
            print(f"INFO: {ratio_count} chunks have ratio outliers (see validate-manifest).")
        if leak_count:
            print(f"INFO: {leak_count} chunks have English-leak candidates. Verify before publishing.")
    except Exception:
        pass

    if issues:
        print("WARNING: Manifest validation issues:")
        for e in issues:
            print(f"  ⚠ {e}")
        return False, issues
    return True, []


def md_to_fb2_direct(md_path: Path, fb2_path: Path, title: str, author: str, temp_dir: Path, genre: str):
    """Build FB2 via fb2_builder.py."""
    script_dir = Path(__file__).resolve().parent
    fb2_builder = script_dir / "fb2_builder.py"
    if not fb2_builder.exists():
        print(f"ERROR: fb2_builder.py not found at {fb2_builder}", file=sys.stderr)
        return False

    sys.path.insert(0, str(script_dir))
    try:
        import importlib

        mod = importlib.import_module("fb2_builder")
    except ImportError as e:
        print(f"ERROR: cannot import fb2_builder: {e}", file=sys.stderr)
        return False

    md_text = md_path.read_text(encoding="utf-8")
    try:
        ok, errors = mod.build_fb2(
            md_text=md_text,
            title=title,
            author=author,
            output_path=fb2_path,
            temp_dir=temp_dir,
            genre=genre,
        )
        if ok:
            print(f"  Built {fb2_path.name} ({fb2_path.stat().st_size:,} bytes)")
            if errors:
                print(f"  XSD validation: {len(errors)} errors (see stderr)")
            else:
                print("  XSD validation: OK")
            return True
        return False
    except Exception as e:
        print(f"ERROR: fb2_builder failed: {e}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        return False


def md_to_fb2_pandoc(md_path: Path, fb2_path: Path, title: str, author: str):
    """Fallback: build FB2 via Pandoc."""
    cmd = [
        "pandoc",
        str(md_path),
        "-f",
        "markdown",
        "-t",
        "fb2",
        "--metadata",
        f"title={title}",
    ]
    if author:
        cmd.extend(["--metadata", f"author={author}"])
    cmd.extend(["-o", str(fb2_path)])
    try:
        run_cmd(cmd, f"pandoc -> {fb2_path.name}")
        return True
    except subprocess.CalledProcessError:
        print("WARNING: FB2 generation (Pandoc) failed.", file=sys.stderr)
        return False


def read_config(temp_dir: Path) -> dict:
    config_path = process_dir(temp_dir) / "config.txt"
    result = {"original_title": "", "author": ""}
    if config_path.exists():
        for line in config_path.read_text(encoding="utf-8").splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                result[k.strip()] = v.strip()
    return result


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Merge and build translation output")
    parser.add_argument("--temp-dir", required=True, help="Temp directory (e.g., book_temp)")
    parser.add_argument("--title", required=True, help="Translated book title")
    parser.add_argument("--author", default="", help="Book author")
    parser.add_argument("--no-fb2", action="store_true", help="Skip FB2 generation")
    parser.add_argument("--pandoc-fallback", action="store_true", help="Use Pandoc for FB2 (fallback)")
    parser.add_argument("--genre", default="prose_counter", help="FB2 genre code (see FictionBookGenres.xsd)")
    parser.add_argument(
        "--no-html",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--no-epub",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args()

    temp_dir = Path(args.temp_dir)
    if not temp_dir.exists():
        print(f"ERROR: temp dir not found: {temp_dir}", file=sys.stderr)
        sys.exit(1)

    config = read_config(temp_dir)
    author = args.author or config.get("author", "")

    ok, issues = validate_manifest(temp_dir)
    if issues:
        print(f"WARNING: {len(issues)} validation issues — proceeding anyway.")

    _, merge_warnings = merge_chunks(temp_dir)
    if merge_warnings:
        print(f"WARNING: {len(merge_warnings)} empty chunks skipped at merge.")

    if not args.no_fb2:
        fb2_path = temp_dir / "book.fb2"
        md_path = temp_dir / "output.md"
        if args.pandoc_fallback:
            print("[merge_and_build] Using Pandoc fallback for FB2")
            md_to_fb2_pandoc(md_path, fb2_path, args.title, author)
        else:
            md_to_fb2_direct(md_path, fb2_path, args.title, author, temp_dir, args.genre)

    print(f"\nDone! Output in: {temp_dir}")
    for f in sorted(temp_dir.glob("book.*")):
        size = f.stat().st_size
        print(f"  {f.name} ({size:,} bytes)")


if __name__ == "__main__":
    main()
