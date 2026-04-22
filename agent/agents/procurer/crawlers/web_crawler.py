"""
WebCrawler — discovers article links from free web sources.

Two-level crawl strategy:
  1. Fetch the landing page → extract all same-domain links (level 1).
  2. Links that look like section/listing pages (short paths, many outbound
     links) are crawled again to extract the articles inside them (level 2).
  3. Previews are fetched only for the final article candidates (capped at
     _MAX_ARTICLES) — not during the discovery phase.

Requires: requests, beautifulsoup4
"""

import logging
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

_AGENT_DIR = Path(__file__).resolve().parent.parent.parent.parent
if str(_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENT_DIR))

from ..base_crawler import BaseCrawler  # noqa: E402
from ..lead         import Lead         # noqa: E402

log = logging.getLogger("cooking-brain.procurer.web_crawler")

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; CookingBrainBot/1.0; educational-research)"
    )
}
_TIMEOUT       = 15
_MAX_ARTICLES    = 50   # max leads returned
_MAX_L1_CRAWL    = 15   # max L1 links to follow for deeper discovery
_PREVIEW_CHARS   = 600
_PREVIEW_WORKERS = 8

# Paths containing these strings are skipped everywhere
_SKIP_FRAGMENTS = [
    "/tag/", "/tags/", "/category/", "/categories/", "/author/",
    "/search", "/cart", "/login", "/logout", "/register",
    "/contact", "/about", "/privacy", "/terms", "/faq",
    "/feed", "/rss", "mailto:", "javascript:", "/#",
]

# A link is treated as a "section page" (worth crawling deeper) if its path
# has only 1 non-empty segment, e.g. /techniques  or  /resources
_SECTION_MAX_SEGMENTS = 1


class WebCrawler(BaseCrawler):
    def discover(self, topics: list[str]) -> list[Lead]:
        url         = self.config["url"]
        source_name = self.config.get("display", self.config["name"])
        source_type = self.config.get("type", "article")
        access      = self.config.get("access", "free")

        log.info(f"  [web_crawler] Crawling: {url}")

        soup = self._fetch_soup(url)
        if soup is None:
            return []

        parsed      = urlparse(url)
        base_domain = f"{parsed.scheme}://{parsed.netloc}"

        # ── Level 1: homepage links ───────────────────────────────────────────
        l1_links = self._extract_links(soup, url, base_domain)
        log.info(f"  [web_crawler] L1 links: {len(l1_links)}")

        # ── Level 2: crawl all L1 links in parallel for deeper articles ───────
        # Don't classify sections vs articles by URL — theculinarypro and similar
        # sites use 1-segment slugs for both. Just crawl everything one level
        # deeper and deduplicate.
        seen     = {url, *[href for href, _ in l1_links]}
        articles = list(l1_links)  # L1 candidates are included too

        with ThreadPoolExecutor(max_workers=_PREVIEW_WORKERS) as pool:
            futures = {
                pool.submit(self._fetch_soup, href): (href, text)
                for href, text in l1_links[:_MAX_L1_CRAWL]
            }
            for fut in as_completed(futures):
                href, _ = futures[fut]
                sub_soup = fut.result()
                if sub_soup is None:
                    continue
                for sub_href, sub_text in self._extract_links(sub_soup, href, base_domain):
                    if sub_href not in seen:
                        seen.add(sub_href)
                        articles.append((sub_href, sub_text))

        log.info(f"  [web_crawler] Total article candidates after L2: {len(articles)}")

        # ── Fetch previews in parallel, capped at _MAX_ARTICLES ───────────────
        candidates = articles[:_MAX_ARTICLES]
        previews   = self._fetch_previews_parallel(candidates)

        leads: list[Lead] = []
        for href, text in candidates:
            leads.append(Lead(
                url             = href,
                title           = text,
                source_name     = source_name,
                source_type     = source_type,
                access          = access,
                content_preview = previews.get(href, ""),
                metadata        = {"crawled_from": url},
            ))

        log.info(f"  [web_crawler] Returning {len(leads)} lead(s) from {source_name}")
        return leads

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _fetch_soup(self, url: str) -> BeautifulSoup | None:
        try:
            resp = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
            resp.raise_for_status()
            return BeautifulSoup(resp.text, "html.parser")
        except Exception as e:
            log.warning(f"  [web_crawler] Could not fetch {url}: {e}")
            return None

    def _extract_links(
        self,
        soup:        BeautifulSoup,
        base_url:    str,
        base_domain: str,
    ) -> list[tuple[str, str]]:
        seen:    set[str]               = set()
        results: list[tuple[str, str]] = []

        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            text = a.get_text(strip=True)

            if href.startswith("/"):
                href = base_domain + href
            elif not href.startswith("http"):
                href = urljoin(base_url, href)

            if urlparse(href).netloc != urlparse(base_url).netloc:
                continue
            if "#" in href or href in seen:
                continue
            if any(frag in href for frag in _SKIP_FRAGMENTS):
                continue
            if len(text) < 8:
                continue

            seen.add(href)
            results.append((href, text))

        return results

    def _fetch_preview(self, url: str) -> str:
        try:
            resp = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")
            for tag in soup(["nav", "footer", "header", "script", "style", "aside"]):
                tag.decompose()
            return soup.get_text(separator=" ", strip=True)[:_PREVIEW_CHARS]
        except Exception:
            return ""

    def _fetch_previews_parallel(
        self, candidates: list[tuple[str, str]]
    ) -> dict[str, str]:
        results: dict[str, str] = {}
        with ThreadPoolExecutor(max_workers=_PREVIEW_WORKERS) as pool:
            futures = {pool.submit(self._fetch_preview, href): href for href, _ in candidates}
            for fut in as_completed(futures):
                href = futures[fut]
                try:
                    results[href] = fut.result()
                except Exception:
                    results[href] = ""
        return results
