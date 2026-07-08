"""Common utilities shared across all phase scripts.

Import from here instead of redefining in each script:
    from common import process_dir, run_cmd, sha256_file
"""

import hashlib
import subprocess
import sys
from pathlib import Path


def process_dir(temp_dir: Path) -> Path:
    """Return process subdirectory (creates if needed).

    Layout:
      <temp_dir>/             human-facing
      <temp_dir>/process/     machine-facing
    """
    p = temp_dir / "process"
    p.mkdir(parents=True, exist_ok=True)
    return p


def sha256_file(path: Path) -> str:
    """Return SHA-256 hex digest of a file."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_cmd(cmd: list[str], desc: str = "") -> str:
    """Run a command, return stdout. Raises on non-zero exit."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", check=True)
        return result.stdout
    except subprocess.CalledProcessError as e:
        sys.stderr.write(f"ERROR: {e}\n{e.stderr}\n")
        raise
