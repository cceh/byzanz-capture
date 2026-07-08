#!/usr/bin/env python3
"""Batch-migrate papyri objects to the current on-disk layout version.

Recursively scans a folder for managed objects (any dir with a `_meta.json`)
and upgrades each — at any depth, so point it at a single box, a folder of
boxes, or the whole archive. Idempotent: safe to re-run, already-current
objects are skipped. Legacy marker files (`_chosen_*.txt` / `_reference_*.txt`)
are renamed to `*.migrated`, not deleted, as a rollback net.

Run from the repo root with the venv active:
    python migrate_boxes.py /path/to/archive
    python migrate_boxes.py /path/to/archive --dry-run
"""
import argparse
import logging
import sys

from papyri.object_layout import CURRENT_LAYOUT_VERSION, migrate_tree


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("root", help="folder to scan recursively for objects")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="report what would migrate without changing anything")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    found, migrated = migrate_tree(args.root, dry_run=args.dry_run)
    verb = "would migrate" if args.dry_run else "migrated"
    print(f"\n{found} object(s) found — {verb} {migrated} "
          f"to layout v{CURRENT_LAYOUT_VERSION}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
