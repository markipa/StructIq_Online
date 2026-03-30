#!/usr/bin/env python3
"""
StructIQ deploy.py — sync source files into the PyInstaller dist folder.

Usage
─────
  python deploy.py              # sync all files, show summary
  python deploy.py --dry-run    # show what would be copied without doing it
  python deploy.py --verbose    # print every file action
  python deploy.py --force      # copy all files even if already up-to-date

What it does
────────────
Copies every source file that is newer than its counterpart in
  backend/dist/StructIQ/_internal/
so the running .exe picks up changes on the next restart without a full
PyInstaller rebuild.

Files synced
────────────
  backend/main.py                 → _internal/main.py
  backend/pmm_engine.py           → _internal/pmm_engine.py
  backend/pmm_conventions.py      → _internal/pmm_conventions.py
  backend/config.py               → _internal/config.py
  backend/database.py             → _internal/database.py
  backend/fem2d_engine.py         → _internal/fem2d_engine.py
  backend/etabs_api/actions.py    → _internal/etabs_api/actions.py
  backend/etabs_api/__init__.py   → _internal/etabs_api/__init__.py
  backend/etabs_api/connection.py → _internal/etabs_api/connection.py
  backend/frontend/app.js         → _internal/frontend/app.js
  backend/frontend/index.html     → _internal/frontend/index.html
  backend/frontend/styles.css     → _internal/frontend/styles.css
  backend/frontend/favicon.ico    → _internal/frontend/favicon.ico  (if exists)
  backend/frontend/favicon.svg    → _internal/frontend/favicon.svg  (if exists)

Exit codes
──────────
  0  — success (all files up-to-date or successfully copied)
  1  — one or more files could not be copied (error printed to stderr)
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

# ── Locate repo root (this file lives at repo root) ───────────────────────────
_HERE      = Path(__file__).resolve().parent
_BACKEND   = _HERE / "backend"
_DIST      = _BACKEND / "dist" / "StructIQ" / "_internal"
_FRONTEND  = _BACKEND / "frontend"
_ETABS_SRC = _BACKEND / "etabs_api"
_ETABS_DST = _DIST / "etabs_api"

# ── File map: source → destination ────────────────────────────────────────────
# Each entry is (source_path, dest_path).  Missing sources are skipped with a
# warning rather than treated as errors (e.g. favicon.ico may not exist).
_FILE_MAP: list[tuple[Path, Path, bool]] = [
    # (source, destination, required)
    (_BACKEND / "main.py",             _DIST / "main.py",             True),
    (_BACKEND / "pmm_engine.py",        _DIST / "pmm_engine.py",        True),
    (_BACKEND / "pmm_conventions.py",   _DIST / "pmm_conventions.py",   True),
    (_BACKEND / "config.py",            _DIST / "config.py",            False),
    (_BACKEND / "database.py",          _DIST / "database.py",          False),
    (_BACKEND / "fem2d_engine.py",      _DIST / "fem2d_engine.py",      False),
    (_ETABS_SRC / "actions.py",         _ETABS_DST / "actions.py",      True),
    (_ETABS_SRC / "__init__.py",        _ETABS_DST / "__init__.py",     False),
    (_ETABS_SRC / "connection.py",      _ETABS_DST / "connection.py",   False),
    (_FRONTEND / "app.js",              _DIST / "frontend" / "app.js",  True),
    (_FRONTEND / "index.html",          _DIST / "frontend" / "index.html", True),
    (_FRONTEND / "styles.css",          _DIST / "frontend" / "styles.css", True),
    (_FRONTEND / "favicon.ico",         _DIST / "frontend" / "favicon.ico", False),
    (_FRONTEND / "favicon.svg",         _DIST / "frontend" / "favicon.svg", False),
]

# ── Colour helpers (no external deps) ─────────────────────────────────────────
_WIN = sys.platform == "win32"

def _green(s):  return f"\033[32m{s}\033[0m" if not _WIN else s
def _yellow(s): return f"\033[33m{s}\033[0m" if not _WIN else s
def _red(s):    return f"\033[31m{s}\033[0m"  if not _WIN else s
def _bold(s):   return f"\033[1m{s}\033[0m"   if not _WIN else s

try:
    # Enable ANSI on Windows 10+
    import ctypes
    ctypes.windll.kernel32.SetConsoleMode(
        ctypes.windll.kernel32.GetStdHandle(-11), 7)
    _WIN = False   # ANSI now enabled
except Exception:
    pass


def _is_newer(src: Path, dst: Path) -> bool:
    """Return True if src is newer than dst (or dst does not exist)."""
    if not dst.exists():
        return True
    return src.stat().st_mtime > dst.stat().st_mtime


def deploy(dry_run: bool = False, verbose: bool = False,
           force: bool = False) -> int:
    """
    Sync source files to _internal/.

    Returns 0 on success, 1 if any copy failed.
    """
    if not _DIST.exists():
        print(_red(f"ERROR: dist folder not found: {_DIST}"), file=sys.stderr)
        print("Run PyInstaller once first to create the dist structure.",
              file=sys.stderr)
        return 1

    copied  = []
    skipped = []
    missing = []
    errors  = []

    for src, dst, required in _FILE_MAP:
        if not src.exists():
            if required:
                msg = f"  MISSING  {src.relative_to(_HERE)}"
                print(_red(msg), file=sys.stderr)
                errors.append(src)
            else:
                if verbose:
                    print(_yellow(f"  skip     {src.relative_to(_HERE)}  (not found)"))
                missing.append(src)
            continue

        rel = src.relative_to(_HERE)

        if not force and not _is_newer(src, dst):
            if verbose:
                print(f"  up-to-date  {rel}")
            skipped.append(src)
            continue

        if dry_run:
            print(_yellow(f"  would copy  {rel}"))
            copied.append(src)
            continue

        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)   # copy2 preserves timestamps
            print(_green(f"  copied   {rel}"))
            copied.append(src)
        except Exception as exc:
            print(_red(f"  ERROR    {rel}: {exc}"), file=sys.stderr)
            errors.append(src)

    # ── Summary ───────────────────────────────────────────────────────────────
    print()
    if dry_run:
        print(_bold("Dry run — no files were written."))
    status_parts = []
    if copied:
        label = "would copy" if dry_run else "copied"
        status_parts.append(_green(f"{len(copied)} {label}"))
    if skipped:
        status_parts.append(f"{len(skipped)} up-to-date")
    if missing:
        status_parts.append(_yellow(f"{len(missing)} optional missing"))
    if errors:
        status_parts.append(_red(f"{errors} error(s)"))

    print(_bold("Deploy summary: ") + "  |  ".join(status_parts))

    if not dry_run and not errors and copied:
        print()
        print("Restart the StructIQ .exe to pick up the changes.")

    return 1 if errors else 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sync StructIQ source files into the PyInstaller dist folder.")
    parser.add_argument("--dry-run",  action="store_true",
                        help="Show what would be copied without writing anything.")
    parser.add_argument("--verbose",  action="store_true",
                        help="Print every file action including up-to-date files.")
    parser.add_argument("--force",    action="store_true",
                        help="Copy all files even if already up-to-date.")
    args = parser.parse_args()

    print(_bold("StructIQ deploy") + f"  ->  {_DIST}")
    print()

    rc = deploy(dry_run=args.dry_run, verbose=args.verbose, force=args.force)
    sys.exit(rc)


if __name__ == "__main__":
    main()
