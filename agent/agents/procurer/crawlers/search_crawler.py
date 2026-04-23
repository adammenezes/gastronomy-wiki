"""
SearchCrawler — discovers sources dynamically via Brave Search API.

Unlike WebCrawler and JournalScraper, this crawler has no fixed seed URL.
It constructs search queries from the gap list, queries the Brave Search API,
confirms each result is a real article, and returns leads for human review.

IMPORTANT: All leads from this crawler are marked access="verify".
They are NEVER auto-ingested. The user must review each URL and ingest
manually via: python agent/compile.py --url <url>

Requires env var: BRAVE_API_KEY (set in .env)
Free tier: 2,000 queries/month — https://api.search.brave.com

sources.yaml config keys:
  api_key_env     — env var holding the Brave API key (default: BRAVE_API_KEY)
  queries_per_gap — how many search queries to run per gap term (default: 1)
  max_results     — max URLs to fetch per search query (default: 5)
  search_suffix   — appended to every query for domain focus
  max_gaps        — how many gap terms to search (default: 25)

Requires: requests, beautifulsoup4
"""

import json
import logging
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

_AGENT_DIR = Path(__file__).resolve().parent.parent.parent.parent.parent
if str(_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENT_DIR))

from ..base_crawler import BaseCrawler  # noqa: E402
from ..lead         import Lead         # noqa: E402

log = logging.getLogger("cooking-brain.procurer.search_crawler")

_BRAVE_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"

_FETCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}
_TIMEOUT       = 15
_FETCH_WORKERS = 6
_PREVIEW_CHARS = 600
_REQUEST_DELAY = 0.5   # seconds between Brave API calls

# Domains to skip in search results
_SKIP_DOMAINS = {
    "twitter.com", "x.com", "facebook.com", "instagram.com",
    "reddit.com", "pinterest.com", "youtube.com", "tiktok.com",
    "wikipedia.org", "wikimedia.org",
    "amazon.com", "ebay.com", "etsy.com",
    "yelp.com", "tripadvisor.com",
}


class SearchCrawler(BaseCrawler):
    """
    Dynamic source discovery via DuckDuckGo search.

    All discovered leads are marked access='verify' and require manual
    user review before ingestion. They are never auto-ingested.
    """

    def discover(self, topics: list[str]) -> list[Lead]:
        source_name     = self.config.get("display", "Web Search")
        queries_per_gap = self.config.get("queries_per_gap", 1)
        max_results     = self.config.get("max_results", 5)
        search_suffix   = self.config.get("search_suffix", "culinary food")
        max_gaps        = self.config.get("max_gaps", 20)
        api_key_env     = self.config.get("api_key_env", "BRAVE_API_KEY")

        api_key = os.environ.get(api_key_env, "").strip()
        if not api_key:
            log.warning(
                f"  [search_crawler] {api_key_env} not set — skipping search."
            )
            return []

        # Select the most informative gap terms (skip single words / stop words)
        selected = [t for t in topics if len(t) > 4][:max_gaps]
        if not selected:
            selected = topics[:max_gaps]

        log.info(
            f"  [search_crawler] Searching {len(selected)} gap term(s) "
            f"× {queries_per_gap} quer(y/ies) each"
        )

        # Build search queries
        queries: list[str] = []
        for gap in selected:
            queries.append(f"{gap} {search_suffix}")

        # Run searches, collect unique URLs
        seen_urls: set[str] = set()
        candidates: list[tuple[str, str]] = []   # (url, title)

        for query in queries[:max_gaps * queries_per_gap]:
            results = self._brave_search(query, max_results, api_key)
            for url, title in results:
                if url not in seen_urls:
                    seen_urls.add(url)
                    candidates.append((url, title))
            time.sleep(_REQUEST_DELAY)

        log.info(f"  [search_crawler] {len(candidates)} unique URL(s) from search")

        # Confirm each candidate is a real article, extract metadata
        leads: list[Lead] = []
        with ThreadPoolExecutor(max_workers=_FETCH_WORKERS) as pool:
            futures = {
                pool.submit(self._fetch_article_meta, url): (url, title)
                for url, title in candidates
            }
            for fut in as_completed(futures):
                url, fallback_title = futures[fut]
                try:
                    meta = fut.result()
                except Exception as e:
                    log.debug(f"  [search_crawler] Error fetching {url}: {e}")
                    continue

                if meta is None:
                    continue

                title, authors, pub_date, preview = meta
                if not title:
                    title = fallback_title

                preview_text = ""
                if authors or pub_date:
                    preview_text = f"{authors} ({pub_date})\n\n".lstrip()
                preview_text += preview

                leads.append(Lead(
                    url             = url,
                    title           = title,
                    source_name     = source_name,
                    source_type     = "search_result",
                    access          = "verify",      # ALWAYS — never auto-ingest
                    content_preview = preview_text,
                    metadata        = {
                        "authors":   authors,
                        "published": pub_date,
                        "search":    True,
                    },
                ))

        log.info(f"  [search_crawler] {len(leads)} confirmed article(s) from search")
        return leads

    # ── Brave Search API ──────────────────────────────────────────────────────

    def _brave_search(
        self, query: str, max_results: int, api_key: str
    ) -> list[tuple[str, str]]:
        """Query Brave Search API. Returns list of (url, title) tuples."""
        try:
            resp = requests.get(
                _BRAVE_SEARCH_URL,
                headers={
                    "X-Subscription-Token": api_key,
                    "Accept": "application/json",
                },
                params={"q": query, "count": max_results},
                timeout=_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            log.warning(f"  [search_crawler] Brave search failed for '{query}': {e}")
            return []

        results: list[tuple[str, str]] = []
        for item in data.get("web", {}).get("results", []):
            url   = item.get("url", "")
            title = item.get("title", "")
            if not url or len(title) < 10:
                continue
            domain = urlparse(url).netloc.lower().lstrip("www.")
            if any(skip in domain for skip in _SKIP_DOMAINS):
                continue
            results.append((url, title))

        return results

    # ── Article confirmation + metadata ──────────────────────────────────────

    def _fetch_article_meta(
        self, url: str
    ) -> tuple[str, str, str, str] | None:
        """
        Fetch a URL and confirm it is an article page.
        Returns (title, authors, date, preview) or None.
        """
        try:
            resp = requests.get(url, headers=_FETCH_HEADERS, timeout=_TIMEOUT)
            resp.raise_for_status()
        except Exception:
            return None

        soup = BeautifulSoup(resp.text, "html.parser")

        # Confirmation signals
        og_type = _og(soup, "og:type")
        jsonld  = _find_jsonld(soup)
        has_article_tag = bool(soup.find("article"))

        is_article = (
            (og_type and "article" in og_type.lower())
            or (jsonld and jsonld.get("@type", "").lower() in
                ("article", "scholarlyarticle", "newsarticle", "blogposting"))
            or has_article_tag
        )
        if not is_article:
            return None

        # Title
        title = (
            _og(soup, "og:title")
            or (jsonld and jsonld.get("headline"))
            or (soup.find("h1") and soup.find("h1").get_text(strip=True))
            or ""
        )

        # Authors
        authors = ""
        if jsonld:
            raw = jsonld.get("author", "")
            if isinstance(raw, list):
                authors = ", ".join(
                    a.get("name", a) if isinstance(a, dict) else str(a)
                    for a in raw
                )
            elif isinstance(raw, dict):
                authors = raw.get("name", "")
            elif isinstance(raw, str):
                authors = raw
        if not authors:
            authors = _og(soup, "article:author") or ""

        # Date
        date = (
            _og(soup, "article:published_time")
            or (jsonld and jsonld.get("datePublished"))
            or _time_tag(soup)
            or _date_from_url(url)
            or ""
        )
        if date and "T" in date:
            date = date.split("T")[0]

        # Preview
        preview = (
            _og(soup, "og:description")
            or (jsonld and jsonld.get("description"))
            or _first_paragraph(soup)
            or ""
        )
        if len(preview) > _PREVIEW_CHARS:
            preview = preview[:_PREVIEW_CHARS]

        return (str(title), str(authors), str(date), str(preview))


# ── Helpers ───────────────────────────────────────────────────────────────────

def _og(soup: BeautifulSoup, prop: str) -> str:
    tag = soup.find("meta", property=prop) or soup.find("meta", attrs={"name": prop})
    return (tag.get("content") or "").strip() if tag else ""


def _find_jsonld(soup: BeautifulSoup) -> dict | None:
    import json
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            if isinstance(data, dict) and "@graph" in data:
                for item in data["@graph"]:
                    if "article" in item.get("@type", "").lower():
                        return item
            if isinstance(data, list):
                for item in data:
                    if "article" in item.get("@type", "").lower():
                        return item
            if isinstance(data, dict):
                return data
        except Exception:
            continue
    return None


def _time_tag(soup: BeautifulSoup) -> str:
    tag = soup.find("time")
    return (tag.get("datetime") or tag.get_text(strip=True)) if tag else ""


def _date_from_url(url: str) -> str:
    m = re.search(r"/(\d{4})/(\d{2})/(\d{2})/", url)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = re.search(r"/(\d{4})/(\d{2})/", url)
    return f"{m.group(1)}-{m.group(2)}" if m else ""


def _first_paragraph(soup: BeautifulSoup) -> str:
    for tag in soup.find_all("p"):
        text = tag.get_text(strip=True)
        if len(text) > 80:
            return text
    return ""
