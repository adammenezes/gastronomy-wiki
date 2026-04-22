"""
MultiLinkCrawler — drills through container pages to yield per-article leads.

Use this for any source whose seed URL is an index (journal homepage, archive
page, table-of-contents) rather than a direct article. Configured via
sources.yaml with an optional `container_patterns` list.

Flow:
  1. Fetch seed URL, strip structural noise, extract filtered links
  2. Partition links → containers (match container_patterns) vs article candidates
  3. Fetch each container page, extract more article candidates
  4. Confirm each candidate is a real article (OpenGraph / JSON-LD / <article> tag)
  5. Extract title, authors, date, abstract — return one Lead per confirmed article

Requires: requests, beautifulsoup4
"""

import json
import logging
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

_AGENT_DIR = Path(__file__).resolve().parent.parent.parent.parent.parent
if str(_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENT_DIR))

from ..base_crawler import BaseCrawler  # noqa: E402
from ..lead         import Lead         # noqa: E402

log = logging.getLogger("cooking-brain.procurer.multi_link_crawler")

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; CookingBrainBot/1.0; educational-research)"
    )
}
_TIMEOUT          = 15
_MAX_CONTAINERS   = 10
_MAX_ARTICLES     = 40
_PREVIEW_CHARS    = 600
_FETCH_WORKERS    = 8

_DEFAULT_CONTAINER_PATTERNS = ["volume", "issue", "archive", "category"]

# URL fragments that always indicate non-article pages
_SKIP_FRAGMENTS = [
    "/tag/", "/tags/", "/author/", "/search", "/login", "/logout",
    "/register", "/subscribe", "/contact", "/about", "/privacy", "/terms",
    "/faq", "/feed", "/rss", "mailto:", "javascript:", "/#",
]

# Structural tags to strip before extracting links
_NOISE_TAGS = ["nav", "header", "footer", "aside", "script", "style"]


class MultiLinkCrawler(BaseCrawler):
    """
    Two-level crawler for multi-link container sources.

    sources.yaml config keys used:
      url                — seed URL (homepage or issue listing)
      container_patterns — list of URL substrings marking container/index pages
                           (default: ["volume", "issue", "archive", "category"])
      access             — free | paywalled | library
      display            — human-readable source name
    """

    def discover(self, topics: list[str]) -> list[Lead]:
        url         = self.config["url"]
        source_name = self.config.get("display", self.config["name"])
        access      = self.config.get("access", "free")
        patterns    = self.config.get("container_patterns", _DEFAULT_CONTAINER_PATTERNS)

        log.info(f"  [multi_link] Seeding from: {url}")

        parsed      = urlparse(url)
        base_domain = f"{parsed.scheme}://{parsed.netloc}"

        seed_soup = _fetch_soup(url)
        if seed_soup is None:
            return []

        l1_links = _extract_article_links(seed_soup, url, base_domain)
        log.info(f"  [multi_link] L1 links after filtering: {len(l1_links)}")

        # Partition into containers and direct article candidates
        containers: list[str] = []
        candidates: list[str] = []
        seen: set[str]        = {url}

        for href in l1_links:
            if href in seen:
                continue
            seen.add(href)
            if _is_container(href, patterns):
                containers.append(href)
            else:
                candidates.append(href)

        log.info(
            f"  [multi_link] Containers: {len(containers)} | "
            f"Direct candidates: {len(candidates)}"
        )

        # Drill into each container page in parallel
        with ThreadPoolExecutor(max_workers=_FETCH_WORKERS) as pool:
            futures = {
                pool.submit(_fetch_soup, c): c
                for c in containers[:_MAX_CONTAINERS]
            }
            for fut in as_completed(futures):
                container_url = futures[fut]
                sub_soup = fut.result()
                if sub_soup is None:
                    continue
                for href in _extract_article_links(sub_soup, container_url, base_domain):
                    if href not in seen and not _is_container(href, patterns):
                        seen.add(href)
                        candidates.append(href)

        candidates = candidates[:_MAX_ARTICLES]
        log.info(f"  [multi_link] Total article candidates: {len(candidates)}")

        # Fetch and confirm each candidate in parallel
        leads: list[Lead] = []
        with ThreadPoolExecutor(max_workers=_FETCH_WORKERS) as pool:
            futures = {
                pool.submit(_fetch_article_meta, href): href
                for href in candidates
            }
            for fut in as_completed(futures):
                href = futures[fut]
                try:
                    meta = fut.result()
                except Exception as e:
                    log.debug(f"  [multi_link] Error fetching {href}: {e}")
                    continue

                if meta is None:
                    continue  # not a confirmed article

                title, authors, date, preview = meta
                if not title:
                    title = _slug_to_title(href)

                preview_text = ""
                if authors or date:
                    preview_text = f"{authors} ({date})\n\n".lstrip()
                preview_text += preview

                leads.append(Lead(
                    url             = href,
                    title           = title,
                    source_name     = source_name,
                    source_type     = "journal_article",
                    access          = access,
                    content_preview = preview_text,
                    metadata        = {
                        "authors":   authors,
                        "published": date,
                    },
                ))

        log.info(f"  [multi_link] Confirmed leads: {len(leads)} from {source_name}")
        return leads


# ── Helpers ───────────────────────────────────────────────────────────────────

def _fetch_soup(url: str) -> BeautifulSoup | None:
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
        resp.raise_for_status()
        return BeautifulSoup(resp.text, "html.parser")
    except Exception as e:
        log.warning(f"  [multi_link] Could not fetch {url}: {e}")
        return None


def _strip_noise(soup: BeautifulSoup) -> None:
    """Remove structural tags that never contain article links."""
    for tag in soup(_NOISE_TAGS):
        tag.decompose()


def _extract_article_links(
    soup: BeautifulSoup,
    base_url: str,
    base_domain: str,
) -> list[str]:
    """
    Five-layer filtered link extraction:
      1. Strip structural noise tags
      2. Same-domain filter
      3. Skip-fragment blocklist
      4. Link text length gate (20–200 chars)
      5. URL depth check (≥ 2 path segments)
    """
    _strip_noise(soup)

    seen:    set[str]  = set()
    results: list[str] = []

    for a in soup.find_all("a", href=True):
        raw  = a["href"].strip()
        text = a.get_text(strip=True)

        if raw.startswith("/"):
            href = base_domain + raw
        elif raw.startswith("http"):
            href = raw
        else:
            href = urljoin(base_url, raw)

        # 1. same-domain
        if urlparse(href).netloc != urlparse(base_url).netloc:
            continue
        # 2. skip fragments
        if any(frag in href for frag in _SKIP_FRAGMENTS):
            continue
        # 3. no fragment anchors or duplicates
        if "#" in href or href in seen:
            continue
        # 4. link text gate
        if not (20 <= len(text) <= 200):
            continue
        # 5. URL depth (≥ 2 non-empty path segments)
        segments = [s for s in urlparse(href).path.split("/") if s]
        if len(segments) < 2:
            continue

        seen.add(href)
        results.append(href)

    return results


def _is_container(url: str, patterns: list[str]) -> bool:
    path = urlparse(url).path.lower()
    return any(p in path for p in patterns)


def _fetch_article_meta(
    url: str,
) -> tuple[str, str, str, str] | None:
    """
    Fetch a candidate URL and confirm it is an article page.

    Confirmation: any of —
      - og:type = "article"
      - JSON-LD with @type Article or ScholarlyArticle
      - <article> HTML element

    Returns (title, authors, date, preview) or None if not confirmed.
    """
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
        resp.raise_for_status()
    except Exception as e:
        log.debug(f"  [multi_link] Fetch failed {url}: {e}")
        return None

    soup = BeautifulSoup(resp.text, "html.parser")

    # ── Confirmation ──────────────────────────────────────────────────────────
    og_type = _og(soup, "og:type")
    jsonld  = _find_jsonld(soup)
    has_article_tag = bool(soup.find("article"))

    is_article = (
        (og_type and "article" in og_type.lower())
        or (jsonld and jsonld.get("@type", "").lower() in ("article", "scholarlyarticle", "newsarticle"))
        or has_article_tag
    )

    if not is_article:
        return None

    # ── Metadata extraction ───────────────────────────────────────────────────
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
        raw_author = jsonld.get("author", "")
        if isinstance(raw_author, list):
            authors = ", ".join(
                a.get("name", a) if isinstance(a, dict) else str(a)
                for a in raw_author
            )
        elif isinstance(raw_author, dict):
            authors = raw_author.get("name", "")
        elif isinstance(raw_author, str):
            authors = raw_author
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
    # Trim to YYYY-MM-DD if ISO timestamp
    if date and "T" in date:
        date = date.split("T")[0]

    # Preview / abstract
    preview = (
        _og(soup, "og:description")
        or (jsonld and jsonld.get("description"))
        or _first_paragraph(soup)
        or ""
    )
    if len(preview) > _PREVIEW_CHARS:
        preview = preview[:_PREVIEW_CHARS]

    return (str(title), str(authors), str(date), str(preview))


def _og(soup: BeautifulSoup, prop: str) -> str:
    tag = soup.find("meta", property=prop) or soup.find("meta", attrs={"name": prop})
    if tag:
        return (tag.get("content") or "").strip()
    return ""


def _find_jsonld(soup: BeautifulSoup) -> dict | None:
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            # Handle @graph wrapper
            if isinstance(data, dict) and "@graph" in data:
                for item in data["@graph"]:
                    t = item.get("@type", "")
                    if isinstance(t, str) and "article" in t.lower():
                        return item
            if isinstance(data, list):
                for item in data:
                    t = item.get("@type", "")
                    if isinstance(t, str) and "article" in t.lower():
                        return item
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, AttributeError):
            continue
    return None


def _time_tag(soup: BeautifulSoup) -> str:
    tag = soup.find("time")
    if tag:
        return tag.get("datetime") or tag.get_text(strip=True)
    return ""


def _date_from_url(url: str) -> str:
    """Extract YYYY-MM-DD from URL paths like /2024/11/25/slug/."""
    m = re.search(r"/(\d{4})/(\d{2})/(\d{2})/", url)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = re.search(r"/(\d{4})/(\d{2})/", url)
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    return ""


def _first_paragraph(soup: BeautifulSoup) -> str:
    for tag in soup.find_all("p"):
        text = tag.get_text(strip=True)
        if len(text) > 80:
            return text
    return ""


def _slug_to_title(url: str) -> str:
    """Convert a URL slug to a readable title as last resort."""
    slug = urlparse(url).path.rstrip("/").split("/")[-1]
    return slug.replace("-", " ").replace("_", " ").title()
