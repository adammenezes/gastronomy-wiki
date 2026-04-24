"""
Cooking Brain — Sort CLI
=========================
Move misplaced wiki root files to their correct subfolders based on
frontmatter tags. Zero Gemini calls — pure frontmatter inspection.

Usage:
    python agent/sort.py                           # dry-run preview (default)
    python agent/sort.py --apply                   # move files
    python agent/sort.py --apply --delete-garbled  # move + delete Gemini error files
"""

import sys
import logging
import argparse
from pathlib import Path

_HERE = Path(__file__).resolve().parent.parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from utils        import load_config       # noqa: E402
from orchestrator import Orchestrator      # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)


def main():
    parser = argparse.ArgumentParser(description="Cooking Brain — Sort Wiki Root")
    parser.add_argument(
        "--apply", action="store_true",
        help="Actually move files (default is dry-run preview)",
    )
    parser.add_argument(
        "--delete-garbled", action="store_true",
        help="Delete files that contain Gemini error messages instead of content",
    )
    args = parser.parse_args()

    dry_run = not args.apply

    if dry_run:
        print("\n[dry-run] No files will be moved. Pass --apply to execute.\n")

    cfg  = load_config()
    orch = Orchestrator(cfg, dry_run=dry_run)

    moved, flagged = orch.sort(dry_run=dry_run, delete_garbled=args.delete_garbled)

    print(f"\n  {'Would move' if dry_run else 'Moved'}: {moved} file(s)")
    print(f"  Flagged for review: {flagged} file(s)")

    if dry_run and moved:
        print("\n  Run with --apply to execute.")

    if not dry_run and moved:
        print("\n  Reindexing wiki…")
        orch.reindex()
        print("  Done.")


if __name__ == "__main__":
    main()
