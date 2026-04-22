"""
JournalScraper — discovers article metadata from academic journal pages.

Handles Tandfonline, ScienceDirect, and falls back to a generic link
extractor for other journal sites.

Full text is NOT fetched — only title + abstract preview, since journals
are paywalled. The human review step surfaces these leads; the user
downloads the PDF and drops it in inbox/ for normal ingestion.

Requires: requests, beautifulsoup4
"""

import logging
import sys
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

_AGENT_DIR = Path(__file__).resolve().parent.parent.parent.parent
if str(_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENT_DIR))

from ..base_crawler import BaseCrawler  # noqa: E402
from ..lead         import Lead         # noqa: E402

log = logging.getLogger("cooking-brain.procurer.journal_scraper")

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; CookingBrainBot/1.0; educational-research)"
    )
}
_TIMEOUT      = 15
_MAX_ARTICLES = 30
_PREVIEW_CHARS = 500


class JournalScraper(BaseCrawler):
    def discover(self, topics: list[str]) -> list[Lead]:
        url         = self.config["url"]
        source_name = self.config.get("display", self.config["name"])
        access      = self.config.get("access", "paywalled")

        log.info(f"  [journal_scraper] Scraping: {url}")

        try:
            resp = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
            resp.raise_for_status()
        except Exception as e:
            log.warning(f"  [journal_scraper] Could not fetch {url}: {e}")
            return []

        soup    = BeautifulSoup(resp.text, "html.parser")
        netloc  = urlparse(url).netloc
        base    = f"{urlparse(url).scheme}://{urlparse(url).netloc}"

        if "tandfonline" in netloc:
            articles = self._parse_tandfonline(soup, base)
        elif "sciencedirect" in netloc:
            articles = self._parse_sciencedirect(soup, base)
        else:
            articles = self._parse_generic(soup, url, base)

        log.info(f"  [journal_scraper] {len(articles)} article(s) from {source_name}")

        leads: list[Lead] = []
        for title, href, preview in articles[:_MAX_ARTICLES]:
            leads.append(Lead(
                url             = href,
                title           = title,
                source_name     = source_name,
                source_type     = "journal",
                access          = access,
                content_preview = preview,
                metadata        = {"journal_url": url},
            ))
        return leads

    # ── Parsers ───────────────────────────────────────────────────────────────

    def _parse_tandfonline(
        self, soup: BeautifulSoup, base: str
    ) -> list[tuple[str, str, str]]:
        results: list[tuple[str, str, str]] = []

        # Tandfonline issue TOC or search results
        selectors = [
            "article",
            ".art_title",
            "[class*='article-card']",
            "[class*='searchResultItem']",
            "[class*='resultItem']",
        ]
        entries = []
        for sel in selectors:
            entries = soup.select(sel)
            if entries:
                break

        for entry in entries:
            title_el    = entry.select_one("h2, h3, [class*='title'], a")
            link_el     = entry.select_one("a[href]")
            abstract_el = entry.select_one("[class*='abstract'], p")

            title    = title_el.get_text(strip=True)   if title_el    else ""
            raw_href = link_el["href"]                  if link_el     else ""
            preview  = abstract_el.get_text(strip=True)[:_PREVIEW_CHARS] if abstract_el else ""

            href = raw_href if raw_href.startswith("http") else base + raw_href

            if title and href and len(title) > 10:
                results.append((title, href, preview))

        return results

    def _parse_sciencedirect(
        self, soup: BeautifulSoup, base: str
    ) -> list[tuple[str, str, str]]:
        results: list[tuple[str, str, str]] = []

        selectors = [
            ".ResultItem",
            "article",
            "[class*='result-item']",
            "[class*='article-item']",
        ]
        entries = []
        for sel in selectors:
            entries = soup.select(sel)
            if entries:
                break

        for entry in entries:
            title_el    = entry.select_one("h2, h3, [class*='title'], a")
            link_el     = entry.select_one("a[href]")
            abstract_el = entry.select_one("[class*='abstract'], p")

            title    = title_el.get_text(strip=True)   if title_el    else ""
            raw_href = link_el["href"]                  if link_el     else ""
            preview  = abstract_el.get_text(strip=True)[:_PREVIEW_CHARS] if abstract_el else ""

            href = raw_href if raw_href.startswith("http") else base + raw_href

            if title and href and len(title) > 10:
                results.append((title, href, preview))

        return results

    def _parse_generic(
        self, soup: BeautifulSoup, original_url: str, base: str
    ) -> list[tuple[str, str, str]]:
        """
        Fallback: grab all same-domain anchor links with article-length text.
        Used for any journal site without a dedicated parser above.
        """
        results: list[tuple[str, str, str]] = []
        seen:    set[str]                    = set()
        orig_netloc = urlparse(original_url).netloc

        for a in soup.find_all("a", href=True):
            raw_href = a["href"].strip()
            text     = a.get_text(strip=True)

            href = raw_href if raw_href.startswith("http") else base + raw_href

            if urlparse(href).netloc != orig_netloc:
                continue
            if href in seen or len(text) < 15:
                continue

            seen.add(href)
            results.append((text, href, ""))

        return results
