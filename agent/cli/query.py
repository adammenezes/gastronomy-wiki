"""
Cooking Brain — Query CLI
==========================
Ask a natural-language question and get an answer synthesised from the wiki.
Optionally file the answer back as a new wiki page.

Usage:
    python agent/query.py "How do I make a proper beurre blanc?"
    python agent/query.py "What is emulsification?" --file
    python agent/query.py "Best pasta techniques?" --dry-run
"""

import sys
import logging
import argparse
from pathlib import Path

_HERE = Path(__file__).resolve().parent.parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from utils import load_config           # noqa: E402
from orchestrator import Orchestrator   # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("cooking-brain.query_cli")


def main():
    parser = argparse.ArgumentParser(description="Cooking Brain — Query")
    parser.add_argument("question", type=str, help="Question to ask the wiki")
    parser.add_argument(
        "--file", action="store_true",
        help="File the answer back into the wiki as a new page",
    )
    parser.add_argument("--dry-run", action="store_true", help="Do not write any files")
    args = parser.parse_args()

    cfg = load_config()
    if args.dry_run:
        cfg["agent"]["dry_run"] = True

    orch   = Orchestrator(cfg, dry_run=args.dry_run)
    result = orch.query(args.question, file_answer=args.file)

    print("\n" + "=" * 60)
    print(result["answer"])
    print("=" * 60)

    if result.get("needs_research"):
        print(f"\n[GAP DETECTED] Suggested research query:")
        print(f"  {result['needs_research']}")

    if result.get("sources"):
        print(f"\n[Sources used: {', '.join(result['sources'])}]")


if __name__ == "__main__":
    main()
