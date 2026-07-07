#!/usr/bin/env python3
"""Shared configuration loader for book-translate-ru skill.

Loads config.toml from one of these locations (first match wins):
  1. <cwd>/config.toml
  2. <cwd>/<book>_temp/config.toml
  3. <skill_dir>/config.toml (fallback — bundled defaults)

Usage in other scripts:
    from config import load_config
    cfg = load_config()
    chunk_size = cfg.get("chunking", "chunk_size", 30000)
    chapter_only = cfg.get("chunking", "chapter_only", True)

Or with section access:
    cfg = load_config()
    chunk_cfg = cfg.section("chunking")
    chunk_size = chunk_cfg.get("chunk_size", 30000)
"""

import json
import sys
import time
from pathlib import Path
from typing import Any

# Ensure UTF-8 output on Windows (cp1251 default breaks on non-ASCII)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")


def atomic_write_text(
    path: Path,
    content: str,
    *,
    encoding: str = "utf-8",
    retries: int = 5,
    retry_delay: float = 0.2,
) -> None:
    """Windows-safe atomic write of text content to a file.

    On POSIX: writes to <path>.tmp then renames over <path> (atomic).
    On Windows: same approach, but Path.replace() may fail with
    PermissionError if the target file is held open by another process
    (antivirus, editor, file watcher). We retry a few times; if still
    failing, fall back to a non-atomic write (remove + rename) which is
    at least idempotent.

    Args:
        path: target file path
        content: text to write
        encoding: file encoding (default utf-8)
        retries: how many times to retry on PermissionError
        retry_delay: seconds between retries

    Raises:
        OSError if all attempts fail.
    """
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")

    # Write to temp file first
    tmp.write_text(content, encoding=encoding)

    # Try atomic replace (POSIX rename, Windows ReplaceFile semantics)
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            tmp.replace(path)  # os.replace under the hood
            return  # success
        except PermissionError as e:
            # Windows: target may be locked by another process
            last_error = e
            if attempt < retries - 1:
                time.sleep(retry_delay)
        except OSError as e:
            last_error = e
            if attempt < retries - 1:
                time.sleep(retry_delay)

    # All retries failed — fall back to non-atomic remove + rename.
    # This is NOT atomic, but better than crashing. If remove fails too,
    # try direct write (truncate + write) as last resort.
    sys.stderr.write(
        f"[atomic_write] WARNING: atomic replace failed after {retries} attempts "
        f"({last_error}). Trying non-atomic fallback.\n"
    )
    try:
        if path.exists():
            path.unlink()
        tmp.rename(path)
    except OSError:
        # Last resort: direct write (no atomicity, but at least writes)
        try:
            tmp.unlink()  # clean up the .tmp file
        except OSError:
            pass
        # Direct write with truncation
        with open(path, "w", encoding=encoding) as f:
            f.write(content)


def atomic_write_json(
    path: Path,
    data: Any,
    *,
    indent: int = 2,
    ensure_ascii: bool = False,
    encoding: str = "utf-8",
    retries: int = 5,
    retry_delay: float = 0.2,
) -> None:
    """Windows-safe atomic write of JSON data. See atomic_write_text.

    Always sanitizes data to remove surrogate characters (U+D800-U+DFFF)
    before serialization. Surrogates appear when data was read with
    surrogateescape error handler (common on Windows with mixed encodings).
    They cannot be encoded as UTF-8 and cause UnicodeEncodeError during
    file write. Sanitizing replaces them with U+FFFD (REPLACEMENT CHARACTER).
    """
    # Always sanitize — prevents UnicodeEncodeError at write time
    sanitized = sanitize_for_json(data)
    try:
        content = json.dumps(sanitized, indent=indent, ensure_ascii=ensure_ascii)
    except (UnicodeEncodeError, ValueError) as e:
        # Last-resort fallback: escape all non-ASCII as \uXXXX
        sys.stderr.write(
            f"[atomic_write_json] WARNING: serialization failed ({e}). Falling back to ensure_ascii=True.\n"
        )
        content = json.dumps(sanitized, indent=indent, ensure_ascii=True)
    atomic_write_text(path, content, encoding=encoding, retries=retries, retry_delay=retry_delay)


def sanitize_for_json(obj: Any) -> Any:
    """Recursively remove/replace surrogate characters from strings in data.

    Surrogates (U+D800-U+DFFF) appear when Python reads bytes with the
    surrogateescape error handler (default for os.fsdecode on Unix, and
    can appear on Windows when files are read with mismatched encodings).
    These cannot be encoded as UTF-8 and cause UnicodeEncodeError.

    This function replaces lone surrogates with U+FFFD (REPLACEMENT CHARACTER)
    so the data can be safely serialized as UTF-8 or ASCII.
    """
    if isinstance(obj, str):
        return _replace_surrogates(obj)
    elif isinstance(obj, dict):
        return {k: sanitize_for_json(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [sanitize_for_json(item) for item in obj]
    else:
        return obj


def _replace_surrogates(s: str) -> str:
    """Replace surrogate characters (U+D800-U+DFFF) with U+FFFD."""
    result = []
    for ch in s:
        cp = ord(ch)
        if 0xD800 <= cp <= 0xDFFF:
            result.append("\ufffd")  # REPLACEMENT CHARACTER
        else:
            result.append(ch)
    return "".join(result)


def read_text_safe(path: Path, encoding: str = "utf-8") -> str:
    """Read text file with errors='replace' — never raises on bad bytes.

    On Windows with mixed encodings (cp1251 system locale, UTF-8 files
    from sub-agents), files may contain bytes that aren't valid in the
    expected encoding. This function replaces invalid bytes with U+FFFD
    instead of raising UnicodeDecodeError.

    Use this for reading meta files, glossary, and any user-edited JSON
    where encoding purity is not guaranteed.
    """
    return Path(path).read_text(encoding=encoding, errors="replace")


def read_json_safe(path: Path, encoding: str = "utf-8") -> Any:
    """Read JSON file with errors='replace' and sanitize surrogates.

    Combines read_text_safe (replace bad bytes) with sanitize_for_json
    (remove surrogates). Produces clean data that can always be serialized
    back to UTF-8.
    """
    text = read_text_safe(path, encoding=encoding)
    data = json.loads(text)
    return sanitize_for_json(data)


# Try Python 3.11+ tomllib, fall back to tomli, then to a minimal parser
try:
    import tomllib  # Python 3.11+

    def _parse_toml(text: str) -> dict:
        return tomllib.loads(text)
except ImportError:
    try:
        import tomli  # type: ignore

        def _parse_toml(text: str) -> dict:
            return tomli.loads(text)
    except ImportError:
        # Minimal TOML parser — handles only what we use:
        # [section], key = value (int, float, bool, string)
        def _parse_toml(text: str) -> dict:
            result: dict = {}
            current_section = result
            for line in text.splitlines():
                line = line.split("#", 1)[0].strip()  # strip comments
                if not line:
                    continue
                if line.startswith("[") and line.endswith("]"):
                    section_name = line[1:-1].strip()
                    current_section = result.setdefault(section_name, {})
                    continue
                if "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip()
                # Parse value
                if value.lower() in ("true", "false"):
                    parsed: Any = value.lower() == "true"
                elif value.startswith('"') and value.endswith('"'):
                    parsed = value[1:-1]
                elif value.startswith("'") and value.endswith("'"):
                    parsed = value[1:-1]
                else:
                    try:
                        parsed = int(value)
                    except ValueError:
                        try:
                            parsed = float(value)
                        except ValueError:
                            parsed = value  # leave as string
                current_section[key] = parsed
            return result


class ConfigSection:
    """Wrapper around a dict with .get(key, default) that handles missing keys."""

    def __init__(self, data: dict | None = None):
        self._data = data or {}

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __contains__(self, key: str) -> bool:
        return key in self._data

    def __repr__(self) -> str:
        return f"ConfigSection({self._data!r})"


class Config:
    """Top-level config with section access."""

    def __init__(self, data: dict | None = None, path: Path | None = None):
        self._data = data or {}
        self._path = path

    def section(self, name: str) -> ConfigSection:
        """Return a ConfigSection for [name]. Empty if section missing."""
        return ConfigSection(self._data.get(name, {}))

    def get(self, section: str, key: str, default: Any = None) -> Any:
        """Convenience: get value from section, with default."""
        return self._data.get(section, {}).get(key, default)

    @property
    def path(self) -> Path | None:
        """Path to the loaded config.toml, or None if no file found."""
        return self._path

    def __repr__(self) -> str:
        return f"Config(path={self._path!r}, sections={list(self._data.keys())})"


# Default values — used when config.toml is missing or key is absent.
# Keep in sync with config.toml.
DEFAULTS = {
    "chunking": {
        "chunk_size": 30000,
        "soft_limit": 40000,
        "min_chunk": 200,
        "chapter_only": True,
    },
    "chunk_context": {
        "context_chars": 300,
    },
    "glossary": {
        "high_frequency_top_n": 20,
    },
    "quality": {
        "ratio_min": 0.6,
        "ratio_max": 2.0,
        "en_leak_chars": 80,
    },
    "epigraphs": {
        "epigraph_max_chars": 600,
    },
    "pronoun_check": {
        "snippet_chars": 800,
        "snippet_truncate": 200,
        "snippet_limit": 20,
    },
    "post_mortem": {
        "aggregate_threshold": 80000,
    },
    "retry": {
        "min_output_chars": 100,
        "max_retries": 2,
    },
    "polish": {
        "ratio_min": 0.5,
        "ratio_max": 2.0,
        "max_heading_delta": 2,
    },
    "parallelism": {
        "batch_size": 8,
    },
}


def _find_config_toml() -> Path | None:
    """Find config.toml in standard locations.

    Search order:
      1. <cwd>/config.toml
      2. Any <cwd>/*_temp/config.toml
      3. <skill_dir>/config.toml (this file's parent)

    Returns the first match, or None.
    """
    cwd = Path.cwd()

    # 1. cwd/config.toml
    candidate = cwd / "config.toml"
    if candidate.exists():
        return candidate

    # 2. cwd/*_temp/config.toml
    for temp_dir in cwd.glob("*_temp"):
        candidate = temp_dir / "config.toml"
        if candidate.exists():
            return candidate

    # 3. skill_dir/config.toml (bundled defaults)
    skill_dir = Path(__file__).resolve().parent.parent  # scripts/ -> skill root
    candidate = skill_dir / "config.toml"
    if candidate.exists():
        return candidate

    return None


def load_config(explicit_path: Path | str | None = None) -> Config:
    """Load configuration from TOML file.

    Args:
        explicit_path: if given, use this path instead of searching.
                       Useful for tests or when the caller knows the
                       config location.

    Returns:
        Config object with .section() and .get() access. Falls back to
        DEFAULTS if no config file is found.
    """
    if explicit_path:
        path = Path(explicit_path)
    else:
        path = _find_config_toml()

    if path is None or not path.exists():
        # No config file — use DEFAULTS only
        return Config(data=dict(DEFAULTS), path=None)

    try:
        text = path.read_text(encoding="utf-8")
        user_data = _parse_toml(text)
    except (OSError, ValueError) as e:
        sys.stderr.write(f"[config] WARNING: could not parse {path}: {e}\n")
        sys.stderr.write("[config] Using defaults only.\n")
        return Config(data=dict(DEFAULTS), path=path)

    # Merge: start with DEFAULTS, override with user_data
    merged: dict = {}
    for section_name, section_defaults in DEFAULTS.items():
        merged[section_name] = dict(section_defaults)
        if section_name in user_data:
            merged[section_name].update(user_data[section_name])

    # Include any extra sections from user_data not in DEFAULTS
    for section_name, section_data in user_data.items():
        if section_name not in merged:
            merged[section_name] = dict(section_data)

    return Config(data=merged, path=path)


# Convenience: cache the loaded config per-process
_cached_config: Config | None = None


def get_config() -> Config:
    """Get cached config (loaded once per process)."""
    global _cached_config
    if _cached_config is None:
        _cached_config = load_config()
    return _cached_config


def reset_cache():
    """Reset the cached config — useful if config file changes mid-run."""
    global _cached_config
    _cached_config = None


if __name__ == "__main__":
    # Smoke test: print loaded config
    cfg = load_config()
    print(f"Config path: {cfg.path}")
    print(f"Sections: {list(cfg._data.keys())}")
    print()
    for section_name in DEFAULTS:
        s = cfg.section(section_name)
        print(f"[{section_name}]")
        for k, v in s._data.items():
            print(f"  {k} = {v!r}")
        print()
