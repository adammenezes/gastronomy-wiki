"""
SearchCrawler — discovers sources dynamically via Brave Search API.

Unlike WebCrawler and JournalScraper, this crawler has no fixed seed URL.
It constructs search queries from the gap list, queries the Brave Search API,
confirms each result is a real article, and returns leads for human review.

IMPORTANT: All leads from this crawler are marked access="verify" (or
"paywalled" when Brave signals isAccessibleForFree=false).
They are NEVER auto-ingested. Review each URL and ingest manually via:
    python agent/compile.py --url <url>

Requires env var: BRAVE_API_KEY (set in .env)
Free tier: 2,000 queries/month — https://api.search.brave.com

sources.yaml config keys:
  api_key_env      — env var holding the Brave API key (default: BRAVE_API_KEY)
  queries_per_gap  — how many search queries to run per gap term (default: 1)
  max_results      — max URLs to return per search query (default: 10, API max: 20)
  search_suffix    — appended to every query for domain focus
  max_gaps         — how many gap terms to search (default: 25)
  freshness        — Brave freshness filter: pd/pw/pm/py or YYYY-MM-DDtoYYYY-MM-DD
  search_lang      — language code for results (default: en)
  skip_domains     — list of domains to exclude (merged with built-in list)
  preferred_domains — list of domains that get a quality signal boost

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

# ── Built-in domain blocklist (merged with sources.yaml skip_domains) ─────────
_DEFAULT_SKIP_DOMAINS = {
    "twitter.com", "x.com", "facebook.com", "instagram.com",
    "reddit.com", "pinterest.com", "youtube.com", "tiktok.com",
    "wikipedia.org", "wikimedia.org",
    "amazon.com", "ebay.com", "etsy.com",
    "yelp.com", "tripadvisor.com",
}

_CULINARY_STOP_WORDS = {
    "cleaned", "milled", "processed", "polished", "smooth", "loss", "shine",
    "golden", "short", "medium", "long", "grain", "seen", "done", "process",
    "nutrients", "vitamins", "minerals", "spoilage", "husk", "bran", "germ",
    "hull", "drying", "soaking", "hulling", "steaming", "boiling", "rinsing",
    "fortification", "content", "value", "level", "amount", "type", "form",
    "color", "colour", "texture", "quality", "size", "shape", "weight",
    "water", "heat", "time", "rate", "ratio", "yield", "loss", "change",
}


# ── Rich result record returned by _brave_search ──────────────────────────────

class _BraveResult:
    """Lightweight container for one Brave web result."""
    __slots__ = (
        "url", "title", "description", "page_age",
        "is_article", "is_paywalled", "authors", "publisher",
        "language", "family_friendly", "extra_snippets",
        "is_preferred",
    )

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))


class SearchCrawler(BaseCrawler):
    """
    Dynamic source discovery via Brave Search API.

    All discovered leads are marked access='verify' (or 'paywalled' when
    Brave signals the content is behind a paywall) and require manual
    user review before ingestion. They are never auto-ingested.
    """

    def discover(self, topics: list[str]) -> list[Lead]:
        source_name      = self.config.get("display", "Web Search (Brave)")
        max_results      = self.config.get("max_results", 10)
        search_suffix    = self.config.get("search_suffix", "culinary food")
        max_gaps         = self.config.get("max_gaps", 25)
        queries_per_gap  = max(1, int(self.config.get("queries_per_gap", 1)))
        api_key_env      = self.config.get("api_key_env", "BRAVE_API_KEY")
        freshness        = self.config.get("freshness", None)
        search_lang      = self.config.get("search_lang", "en")

        # Domain lists — merge config lists with defaults
        skip_domains      = _DEFAULT_SKIP_DOMAINS | set(self.config.get("skip_domains", []))
        preferred_domains = set(self.config.get("preferred_domains", []))

        # Injected by ProcurementAgent for LLM filtering
        client      = self.config.get("_client")
        llm_cfg     = self.config.get("_llm_cfg")
        prompts_dir = self.config.get("_prompts_dir", "")
        wiki_root   = self.config.get("_wiki_root", "")
        page_path   = self.config.get("_page_path", "")

        api_key = os.environ.get(api_key_env, "").strip()
        if not api_key:
            log.warning(f"  [search_crawler] {api_key_env} not set — skipping search.")
            return []

        # ── 1. Collect existing wiki page titles for deduplication ────────────
        existing_titles: set[str] = set()
        if wiki_root:
            for md in Path(wiki_root).rglob("*.md"):
                existing_titles.add(md.stem.lower())

        # ── 2. Derive page title for query anchoring ──────────────────────────
        page_title = ""
        if page_path and Path(page_path).exists():
            text = Path(page_path).read_text(encoding="utf-8")
            m = re.search(r'^title:\s*["\']?(.+?)["\']?\s*$', text, re.MULTILINE)
            page_title = m.group(1).strip() if m else Path(page_path).stem.replace("-", " ")

        # ── 3. Rule-based gap filtering ───────────────────────────────────────
        # Sort: multi-word gaps first (more specific), then single-word.
        sorted_topics = sorted(topics, key=lambda t: (-len(t.split()), t))

        rule_filtered = []
        for gap in sorted_topics:
            lower = gap.lower().strip()
            if len(lower) < 4:
                continue
            if lower in _CULINARY_STOP_WORDS:
                continue
            if lower in existing_titles:
                continue
            rule_filtered.append(gap)

        # Slice to exactly max_gaps — every gap in this list is guaranteed a
        # search slot. No LLM discretion over which gaps get searched.
        gaps_to_search = rule_filtered[:max_gaps]

        if not gaps_to_search:
            log.info("  [search_crawler] No gaps remained after rule filtering — skipping.")
            return []

        log.info(
            f"  [search_crawler] {len(gaps_to_search)}/{len(topics)} gap(s) selected "
            f"(rule-filtered, capped at max_gaps={max_gaps})"
        )

        # ── 4. LLM query refinement — one refined query per gap ───────────────
        # _llm_refine_queries returns a flat list in the same order as
        # gaps_to_search, with exactly one query per gap (or queries_per_gap
        # queries per gap when configured > 1).
        if client and llm_cfg and prompts_dir:
            queries = self._llm_refine_queries(
                client, llm_cfg, prompts_dir,
                gaps_to_search, page_title, search_suffix, queries_per_gap,
            )
        else:
            anchor = f" {page_title}" if page_title else ""
            queries = [
                f"{gap}{anchor} {search_suffix}".strip()
                for gap in gaps_to_search
                for _ in range(queries_per_gap)
            ]

        log.info(f"  [search_crawler] {len(queries)} search quer(y/ies) to run ({queries_per_gap}/gap)")

        # ── 5. Run Brave searches ─────────────────────────────────────────────
        seen_urls: set[str] = set()
        candidates: list[_BraveResult] = []

        for query in queries:
            results = self._brave_search(
                query, max_results, api_key,
                freshness=freshness,
                search_lang=search_lang,
                skip_domains=skip_domains,
                preferred_domains=preferred_domains,
            )
            for r in results:
                if r.url not in seen_urls:
                    seen_urls.add(r.url)
                    candidates.append(r)
            time.sleep(_REQUEST_DELAY)

        log.info(f"  [search_crawler] {len(candidates)} unique URL(s) from search")

        # ── 6. Build leads — use Brave metadata where available ───────────────
        #
        # The Brave API already returns description, page_age, article metadata
        # (including isAccessibleForFree), and up to 5 extra_snippets per result.
        # We only fall back to an HTTP fetch when Brave gives us neither an article
        # confirmation nor any description text.
        #
        leads: list[Lead] = []
        need_fetch: list[_BraveResult] = []

        for r in candidates:
            if r.is_article or r.description:
                # Brave has enough signal — build the lead directly
                lead = self._lead_from_brave(r, source_name)
                if lead:
                    leads.append(lead)
            else:
                need_fetch.append(r)

        log.info(
            f"  [search_crawler] {len(leads)} lead(s) built from Brave metadata; "
            f"{len(need_fetch)} URL(s) need HTTP fetch"
        )

        # Fall back: HTTP fetch only for results with no Brave metadata
        if need_fetch:
            with ThreadPoolExecutor(max_workers=_FETCH_WORKERS) as pool:
                futures = {
                    pool.submit(self._fetch_article_meta, r.url): r
                    for r in need_fetch
                }
                for fut in as_completed(futures):
                    r = futures[fut]
                    try:
                        meta = fut.result()
                    except Exception as e:
                        log.debug(f"  [search_crawler] Error fetching {r.url}: {e}")
                        continue

                    if meta is None:
                        continue

                    title, authors, pub_date, preview = meta

                    preview_text = ""
                    if authors or pub_date:
                        preview_text = f"{authors} ({pub_date})\n\n".lstrip()
                    preview_text += preview

                    leads.append(Lead(
                        url             = r.url,
                        title           = title or r.title,
                        source_name     = source_name,
                        source_type     = "search_result",
                        access          = "verify",
                        content_preview = preview_text,
                        metadata        = {"authors": authors, "published": pub_date, "search": True},
                    ))

        log.info(f"  [search_crawler] {len(leads)} total confirmed lead(s) from search")
        return leads

    # ── Lead builder from Brave metadata ──────────────────────────────────────

    def _lead_from_brave(self, r: _BraveResult, source_name: str) -> Lead | None:
        """Build a Lead directly from the data already in the Brave response."""
        # Compose preview from description + extra_snippets
        snippets = r.extra_snippets or []
        preview_parts = [p for p in [r.description] + snippets if p]
        preview = " […] ".join(preview_parts)
        if len(preview) > _PREVIEW_CHARS:
            preview = preview[:_PREVIEW_CHARS]

        # Prefix with author / date if available
        preview_text = ""
        if r.authors or r.page_age:
            meta_line = " ".join(filter(None, [r.authors, f"({r.page_age})" if r.page_age else ""]))
            preview_text = meta_line + "\n\n"
        preview_text += preview

        # Paywall detection: Brave sets isAccessibleForFree=False for paywalled pages
        access = "paywalled" if r.is_paywalled else "verify"

        return Lead(
            url             = r.url,
            title           = r.title or "",
            source_name     = source_name,
            source_type     = "search_result",
            access          = access,
            content_preview = preview_text,
            metadata        = {
                "authors":   r.authors or "",
                "published": r.page_age or "",
                "publisher": r.publisher or "",
                "language":  r.language or "",
                "preferred": r.is_preferred or False,
                "search":    True,
            },
        )

    # ── LLM query refinement ──────────────────────────────────────────────────

    def _llm_refine_queries(
        self,
        client, llm_cfg: dict, prompts_dir: str,
        gaps: list[str], page_title: str, search_suffix: str, queries_per_gap: int,
    ) -> list[str]:
        """
        Single Flash call that returns one refined search query per gap
        (or queries_per_gap queries per gap when > 1).

        The LLM outputs a JSON object keyed by the original gap term:
            {"Starch Gelatinisation": ["starch gelatinization food science cooking"],
             "Arborio": ["arborio rice variety risotto culinary properties"], ...}

        This guarantees every gap in the input list gets at least one search
        slot — the LLM refines the *wording* of queries but cannot drop gaps.
        Falls back to mechanical query construction if the LLM call fails.
        """
        try:
            from llm    import call_llm  # noqa: E402
            from utils  import load_prompt  # noqa: E402

            prompt_text = load_prompt(Path(prompts_dir), "search_queries")
            prompt_text = prompt_text.replace("{queries_per_gap}", str(queries_per_gap))

            gap_list = "\n".join(f"- {g}" for g in gaps)
            user_msg = (
                f"Page: {page_title or 'General culinary wiki'}\n"
                f"Search context: {search_suffix}\n\n"
                f"Gap terms (one query required per term):\n{gap_list}"
            )

            raw = call_llm(client, llm_cfg, prompt_text, user_msg)

            # Strip markdown fences if present
            raw = re.sub(r"^```(?:json)?\s*", "", raw.strip(), flags=re.MULTILINE)
            raw = re.sub(r"\s*```$",           "", raw.strip(), flags=re.MULTILINE)

            # Expect a JSON object: {gap: query_str} or {gap: [q1, q2]}
            m = re.search(r"\{.*\}", raw, re.DOTALL)
            if not m:
                raise ValueError("No JSON object found in LLM response")

            data = json.loads(m.group(0))
            if not isinstance(data, dict):
                raise ValueError(f"Expected dict, got {type(data).__name__}")

            # Flatten in original gap order, preserving guaranteed coverage.
            # If the LLM omits a gap key, fall back to mechanical query for it.
            queries: list[str] = []
            anchor = f" {page_title}" if page_title else ""
            for gap in gaps:
                val = data.get(gap) or data.get(gap.lower())
                if val is None:
                    # LLM dropped this gap — substitute a mechanical query
                    queries.append(f"{gap}{anchor} {search_suffix}".strip())
                    log.debug(f"  [search_crawler] LLM omitted '{gap}' — using fallback query")
                elif isinstance(val, list):
                    for q in val[:queries_per_gap]:
                        if isinstance(q, str) and q.strip():
                            queries.append(q.strip())
                else:
                    queries.append(str(val).strip())

            log.info(
                f"  [search_crawler] LLM refined {len(gaps)} gap(s) → "
                f"{len(queries)} quer(y/ies) ({queries_per_gap}/gap)"
            )
            return queries

        except Exception as e:
            log.warning(f"  [search_crawler] LLM query refinement failed ({e}) — using rule-based fallback")
            anchor = f" {page_title}" if page_title else ""
            return [
                f"{gap}{anchor} {search_suffix}".strip()
                for gap in gaps
                for _ in range(queries_per_gap)
            ]

    # ── Brave Search API ──────────────────────────────────────────────────────

    def _brave_search(
        self,
        query: str,
        max_results: int,
        api_key: str,
        *,
        freshness: str | None = None,
        search_lang: str = "en",
        skip_domains: set[str] | None = None,
        preferred_domains: set[str] | None = None,
    ) -> list[_BraveResult]:
        """
        Query Brave Search API.

        Uses result_filter=web, extra_snippets=true, spellcheck=true, and
        search_lang to ensure cleaner, richer results without extra HTTP fetches.
        Extracts article metadata, page_age, paywall status, and snippets
        directly from the Brave response.
        """
        skip_domains     = skip_domains     or _DEFAULT_SKIP_DOMAINS
        preferred_domains = preferred_domains or set()

        params: dict = {
            "q":              query,
            "count":          min(max_results, 20),   # API hard max is 20
            "result_filter":  "web",                  # only web results — no news/video slots
            "extra_snippets": "true",                 # up to 5 extra excerpts per result
            "spellcheck":     "true",                 # correct culinary term misspellings
            "search_lang":    search_lang,            # restrict to English (or configured lang)
            "text_decorations": "false",              # no highlighting chars in snippets
        }
        if freshness:
            params["freshness"] = freshness           # e.g. "py" = past year

        try:
            resp = requests.get(
                _BRAVE_SEARCH_URL,
                headers={
                    "X-Subscription-Token": api_key,
                    "Accept":               "application/json",
                },
                params=params,
                timeout=_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            log.warning(f"  [search_crawler] Brave search failed for '{query}': {e}")
            return []

        results: list[_BraveResult] = []

        for item in data.get("web", {}).get("results", []):
            url   = item.get("url", "")
            title = item.get("title", "")

            if not url or len(title) < 10:
                continue

            # ── Domain filtering ──────────────────────────────────────────────
            domain = urlparse(url).netloc.lower().lstrip("www.")
            if any(skip in domain for skip in skip_domains):
                continue

            # ── Language + safety pre-filter ─────────────────────────────────
            lang = item.get("language", "")
            if lang and lang not in ("", search_lang, "en"):
                continue   # skip non-target-language results

            family_friendly = item.get("family_friendly")
            if family_friendly is False:
                continue   # explicit flag; None means unknown → allow

            # ── Article signal from Brave response ────────────────────────────
            article_obj = item.get("article") or {}
            is_article  = bool(article_obj)

            # isAccessibleForFree: True = open, False = paywalled, None = unknown
            free_flag   = article_obj.get("isAccessibleForFree")
            is_paywalled = (free_flag is False)

            # Authors from Brave article object
            authors = ""
            raw_author = article_obj.get("author", "")
            if isinstance(raw_author, list):
                authors = ", ".join(
                    a.get("name", a) if isinstance(a, dict) else str(a)
                    for a in raw_author
                )
            elif isinstance(raw_author, dict):
                authors = raw_author.get("name", "")
            elif isinstance(raw_author, str):
                authors = raw_author

            publisher = ""
            pub_obj = article_obj.get("publisher") or {}
            if isinstance(pub_obj, dict):
                publisher = pub_obj.get("name", "")
            elif isinstance(pub_obj, str):
                publisher = pub_obj

            # ── Page age ─────────────────────────────────────────────────────
            page_age = item.get("page_age", "")
            if page_age and "T" in page_age:
                page_age = page_age.split("T")[0]

            # ── Description + extra snippets ──────────────────────────────────
            description     = (item.get("description") or "").strip()
            extra_snippets  = [
                s.strip() for s in (item.get("extra_snippets") or [])
                if isinstance(s, str) and s.strip()
            ]

            # ── Preferred domain signal ───────────────────────────────────────
            is_preferred = any(pref in domain for pref in preferred_domains)

            results.append(_BraveResult(
                url            = url,
                title          = title,
                description    = description,
                page_age       = page_age,
                is_article     = is_article,
                is_paywalled   = is_paywalled,
                authors        = authors,
                publisher      = publisher,
                language       = lang,
                family_friendly= family_friendly,
                extra_snippets = extra_snippets,
                is_preferred   = is_preferred,
            ))

        return results

    # ── Article confirmation + metadata (HTTP fallback only) ──────────────────

    def _fetch_article_meta(
        self, url: str
    ) -> tuple[str, str, str, str] | None:
        """
        Fetch a URL and confirm it is an article page.
        Returns (title, authors, date, preview) or None.

        This is only called for results where Brave returned no description
        and no article object — typically paywalled or JS-rendered pages.
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


# ── HTML helpers (used by HTTP fallback path only) ────────────────────────────

def _og(soup: BeautifulSoup, prop: str) -> str:
    tag = soup.find("meta", property=prop) or soup.find("meta", attrs={"name": prop})
    return (tag.get("content") or "").strip() if tag else ""


def _find_jsonld(soup: BeautifulSoup) -> dict | None:
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
