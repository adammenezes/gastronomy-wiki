"""
Gap Analyzer — identifies under-covered topics in the wiki.

Five detection methods in order of signal confidence:
  1. Broken WikiLinks  — [[links]] to pages that don't exist yet
  2. Lint gaps         — stubs and gaps from LintAgent report
  3. Mention frequency — terms repeated across pages but without their own page
  4. Taxonomy gaps     — master culinary topic list vs. current wiki coverage
"""

import re
import logging
import sys
from collections import Counter
from pathlib import Path

import yaml

_AGENT_DIR = Path(__file__).resolve().parent.parent.parent
if str(_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENT_DIR))

from utils import collect_wiki_pages    # noqa: E402

log = logging.getLogger("cooking-brain.procurer.gap_analyzer")

_STOP_WORDS = {
    "the", "and", "for", "with", "from", "that", "this", "are", "was",
    "were", "been", "have", "has", "had", "will", "would", "could",
    "should", "they", "their", "also", "when", "which", "then", "into",
    "about", "more", "some", "well", "very", "just", "used", "made",
    "over", "using", "like", "make", "can", "not", "but",
}

# Single-word WikiLinks that are too generic to be useful procurement targets.
# These are real culinary concepts but too broad to search for specifically.
_WIKILINK_NOISE = {
    "cook", "cooks", "cooking", "boil", "boiling", "heat", "heated",
    "pan", "pot", "water", "salt", "sauce", "dish", "dishes", "meal",
    "meals", "food", "foods", "ingredient", "ingredients", "recipe",
    "recipes", "flavor", "flavour", "taste", "method", "methods",
    "technique", "techniques", "process", "step", "steps", "time",
    "minutes", "heat", "temperature", "oil", "butter", "stock", "broth",
    "mix", "stir", "add", "serve", "season", "use", "place", "put",
    "cut", "slice", "chop", "dice", "type", "types", "form", "forms",
    "home", "kitchen", "chef", "chefs", "professional", "classic",
    "basic", "simple", "quick", "easy", "best", "good", "great",
}

_WIKILINK_MIN_CHARS   = 5    # skip links shorter than this
_WIKILINK_MIN_WORDS   = 2    # single-word links need extra vetting (see above)

_MENTION_MIN_COUNT = 3   # a phrase must appear this many times to flag as gap
_MENTION_CAP       = 20  # max gaps from mention frequency


class GapAnalyzer:
    def __init__(self, wiki_root: Path, taxonomy_path: Path):
        self.wiki_root     = wiki_root
        self.taxonomy_path = taxonomy_path

    def run(self, lint_report: dict | None = None) -> list[str]:
        """
        Return a merged, deduplicated list of gap topic strings.
        Ordered by confidence: broken links first, taxonomy last.
        Call run_by_signal() when you need per-signal-type access.
        """
        by_signal = self.run_by_signal(lint_report)
        return self._merge(by_signal)

    def run_by_signal(self, lint_report: dict | None = None) -> dict[str, list[str]]:
        """
        Return gaps grouped by signal type so callers can sample proportionally.
        Keys: "wikilinks", "lint", "frequency", "taxonomy"
        """
        pages = collect_wiki_pages(self.wiki_root, include_content=True)
        existing_titles = {p["title"].lower() for p in pages}

        broken = self._broken_wikilinks(pages, existing_titles)
        log.info(f"  [gaps] Broken WikiLinks:   {len(broken)}")

        lint_gaps = self._lint_gaps(lint_report) if lint_report else []
        log.info(f"  [gaps] Lint gaps:          {len(lint_gaps)}")

        freq = self._mention_frequency(pages, existing_titles)
        log.info(f"  [gaps] Mention frequency:  {len(freq)}")

        tax = self._taxonomy_gaps(existing_titles)
        log.info(f"  [gaps] Taxonomy gaps:      {len(tax)}")

        by_signal = {
            "wikilinks": broken,
            "lint":      lint_gaps,
            "frequency": freq,
            "taxonomy":  tax,
        }
        total = len(self._merge(by_signal))
        log.info(f"  [gaps] Total unique gaps:  {total}")
        return by_signal

    def _merge(self, by_signal: dict[str, list[str]]) -> list[str]:
        """Flatten and deduplicate, preserving signal order."""
        seen:   set[str]  = set()
        result: list[str] = []
        for signal in ("wikilinks", "lint", "frequency", "taxonomy"):
            for g in by_signal.get(signal, []):
                key = g.lower().strip()
                if key and key not in seen:
                    seen.add(key)
                    result.append(g)
        return result

    # ── Method 1 ──────────────────────────────────────────────────────────────

    def _broken_wikilinks(self, pages: list[dict], existing_titles: set[str]) -> list[str]:
        """
        Find [[WikiLink]] references pointing to non-existent pages.

        Noise filters applied:
          - Skip links shorter than _WIKILINK_MIN_CHARS characters
          - Skip single-word links in _WIKILINK_NOISE (too generic to source)
          - Skip pure-digit or punctuation-only links
        """
        broken: list[str] = []
        seen:   set[str]  = set()
        for page in pages:
            content = page.get("content", "")
            links = re.findall(r"\[\[([^\]|#\n]+?)(?:\|[^\]]*)?\]\]", content)
            for link in links:
                link = link.strip()
                key  = link.lower()

                if key in existing_titles or key in seen:
                    continue
                if len(link) < _WIKILINK_MIN_CHARS:
                    continue
                if not re.search(r"[a-zA-Z]", link):   # skip pure numbers/symbols
                    continue

                words = link.split()
                if len(words) == 1 and key in _WIKILINK_NOISE:
                    continue

                seen.add(key)
                broken.append(link)
        return broken

    # ── Method 2 ──────────────────────────────────────────────────────────────

    def _lint_gaps(self, lint_report: dict) -> list[str]:
        """Extract topic strings from a LintAgent report."""
        gaps: list[str] = []

        for item in lint_report.get("gaps", []):
            if isinstance(item, str):
                gaps.append(item)
            elif isinstance(item, dict):
                topic = item.get("topic") or item.get("gap") or item.get("title", "")
                if topic:
                    gaps.append(topic)

        for stub in lint_report.get("stubs", []):
            if isinstance(stub, str):
                gaps.append(stub)
            elif isinstance(stub, dict):
                title = stub.get("title", "")
                if title:
                    gaps.append(title)

        return gaps

    # ── Method 3 ──────────────────────────────────────────────────────────────

    def _mention_frequency(
        self,
        pages:           list[dict],
        existing_titles: set[str],
    ) -> list[str]:
        """
        Count 2-gram and 3-gram phrases across all wiki pages.
        Return phrases that appear ≥ _MENTION_MIN_COUNT times but have no
        dedicated page, capped at _MENTION_CAP results.
        """
        counter: Counter = Counter()

        for page in pages:
            content = page.get("content", "")
            # Strip frontmatter and wikilink brackets
            content = re.sub(r"^---.*?---\s*", "", content, flags=re.DOTALL)
            content = re.sub(r"\[\[([^\]]+)\]\]", r"\1", content)

            words = re.findall(r"\b[a-zA-Z][a-zA-Z\-']{2,}\b", content.lower())
            words = [w for w in words if w not in _STOP_WORDS]

            for i in range(len(words) - 1):
                counter[f"{words[i]} {words[i+1]}"] += 1
            for i in range(len(words) - 2):
                counter[f"{words[i]} {words[i+1]} {words[i+2]}"] += 1

        result: list[str] = []
        for phrase, count in counter.most_common(200):
            if count < _MENTION_MIN_COUNT:
                break
            if phrase.lower() not in existing_titles:
                result.append(phrase)
            if len(result) >= _MENTION_CAP:
                break

        return result

    # ── Method 4 ──────────────────────────────────────────────────────────────

    def _taxonomy_gaps(self, existing_titles: set[str]) -> list[str]:
        """Return taxonomy leaf topics that have no corresponding wiki page."""
        if not self.taxonomy_path.exists():
            log.warning(f"  [gaps] Taxonomy file not found: {self.taxonomy_path}")
            return []

        with open(self.taxonomy_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        gaps: list[str] = []
        self._walk(data.get("taxonomy", data), existing_titles, gaps)
        return gaps

    def _walk(self, node, existing_titles: set[str], gaps: list[str]):
        if isinstance(node, list):
            for item in node:
                if isinstance(item, str):
                    if item.lower() not in existing_titles:
                        gaps.append(item)
                else:
                    self._walk(item, existing_titles, gaps)
        elif isinstance(node, dict):
            for value in node.values():
                self._walk(value, existing_titles, gaps)
