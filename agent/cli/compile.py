"""
Cooking Brain — Compile CLI
============================
Process inbox files and compile them into the wiki.

Usage:
    python agent/compile.py              # process entire inbox
    python agent/compile.py --dry-run    # preview without writing
    python agent/compile.py --file PATH  # process a single file
    python agent/compile.py --reindex    # rebuild index.md only
"""

import sys
import logging
import argparse
from pathlib import Path

# Allow importing sibling modules from agent/
_HERE = Path(__file__).resolve().parent.parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from utils import load_config       # noqa: E402
from orchestrator import Orchestrator  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)


def main():
    parser = argparse.ArgumentParser(description="Cooking Brain — Compile")
    parser.add_argument("--dry-run",  action="store_true", help="Preview without writing files")
    parser.add_argument("--file",     type=str,            help="Process a single file instead of the inbox")
    parser.add_argument("--url",      type=str,            help="Process a URL (web page or YouTube video)")
    parser.add_argument("--reindex",  action="store_true", help="Regenerate wiki/index.md only")
    args = parser.parse_args()

    cfg = load_config()
    if args.dry_run:
        cfg["agent"]["dry_run"] = True

    orch = Orchestrator(cfg, dry_run=args.dry_run)

    if args.reindex:
        orch.reindex()

    elif args.url:
        orch.process_url(args.url)

    elif args.file:
        file_path = Path(args.file)
        if not file_path.exists():
            print(f"ERROR: File not found: {args.file}")
            sys.exit(1)
        ok = orch.process_file(file_path)
        if ok:
            orch.reindex()

    else:
        orch.process_inbox()
        if not args.dry_run:
            orch.sort(delete_garbled=True)


if __name__ == "__main__":
    main()
