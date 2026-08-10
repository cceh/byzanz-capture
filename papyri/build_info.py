"""Which build is running? Git commit hash + date for the UI and the log.

Resolved once (memoisation via lru_cache) via `git log -1`.
"""
from __future__ import annotations

import logging
import subprocess
from functools import lru_cache
from pathlib import Path

_logger = logging.getLogger(__name__)

# papyri/build_info.py -> repo root. Resolved from __file__, NOT via
# get_ui_path: that helper is cwd-dependent and .git is not a UI asset.
_REPO_ROOT = Path(__file__).resolve().parent.parent


@lru_cache(maxsize=1)
def describe() -> str:
    """Short commit hash + commit date of the running checkout,
    e.g. "1a2b3c4 2026-08-10", or "unknown"."""
    # No sys.frozen guard here: py2app sets it even in alias mode, where
    # the source tree and .git are fully present. A build without git
    # metadata fails the subprocess call below and reports "unknown".
    try:
        result = subprocess.run(
            ["git", "-C", str(_REPO_ROOT), "log", "-1", "--format=%h %cs"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as err:
        _logger.warning("build_info: git invocation failed (%s) -> 'unknown'", err)
        return "unknown"
    if result.returncode != 0:
        _logger.warning("build_info: git log failed (rc=%s, %s) -> 'unknown'",
                        result.returncode, result.stderr.strip())
        return "unknown"
    return result.stdout.strip() or "unknown"
