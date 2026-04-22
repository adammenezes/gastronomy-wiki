"""
Deduplicator — removes leads whose URLs are already known to the system.

Checks:
  - wiki/log.md     — URLs of previously ingested content
  - inbox/leads.md  — URLs surfaced in prior procurement runs
"""

import re
import logging
from pathlib import Path

from .lead import Lead

log = logging.getLogger("cooking-brain.procurer.deduplicator")

_URL_PATTERN = re.compile(r"https?://[^\s\"'>\]]+")


class Deduplicator:
    def __init__(self, wiki_root: Path, inbox_root: Path):
        self.wiki_root  = wiki_root
        self.inbox_root = inbox_root

    def filter(self, leads: list[Lead]) -> list[Lead]:
        """Remove leads whose URLs appear in log.md or a prior leads.md."""
        known = self._collect_known_urls()
        log.info(f"  [dedup] Known URLs: {len(known)}")

        fresh    = [l for l in leads if l.url not in known]
        removed  = len(leads) - len(fresh)
        log.info(f"  [dedup] Removed {removed} duplicate(s). {len(fresh)} fresh leads remain.")
        return fresh

    def _collect_known_urls(self) -> set[str]:
        urls: set[str] = set()

        for path in (
            self.wiki_root  / "log.md",
            self.inbox_root / "leads.md",
        ):
            if path.exists():
                text = path.read_text(encoding="utf-8")
                urls.update(_URL_PATTERN.findall(text))

        return urls
