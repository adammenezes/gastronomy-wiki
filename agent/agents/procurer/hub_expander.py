"""
Hub Expander — follows hub_page leads one level deep via HTTP (no Brave quota used).

When a lead is classified as content_type='hub_page', its URL is a navigation
or index page (e.g. /chapters/, /volumes/, /category/x). The real content is
one level deeper. This module:

  1. Fetches the hub page with BeautifulSoup
  2. Extracts same-domain article links (filters nav/asset/external links)
  3. Returns them as new Lead objects with access='verify' for scoring

Design constraints:
  - No Brave API calls — pure HTTP only
  - Capped at MAX_LINKS_PER_HUB links per hub to prevent blowup
  - Only triggers for hubs scoring above HUB_SCORE_THRESHOLD
  - Respects existing deduplicator (caller passes known URLs to skip)
"""

import logging
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse

from .lead import Lead

log = logging.getLogger("cooking-brain.procurer.hub_expander")

MAX_LINKS_PER_HUB  = 10    # max article links extracted per hub page
HUB_SCORE_THRESHOLD = 6.0  # only expand hubs with combined_score >= this
_TIMEOUT = 10

_FETCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; CookingBrainBot/1.0; "
        "+https://github.com/cooking-brain)"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# URL path fragments that indicate a link is NOT an article
_SKIP_PATH_FRAGMENTS = {
    "/login", "/register", "/signup", "/subscribe",
    "/cart", "/checkout", "/shop", "/store", "/buy",
    "/search", "/tag/", "/tags/", "/category/",
    "/author/", "/about", "/contact", "/privacy",
    "/cdn-cgi/", "/static/", "/assets/", "/images/",
    ".css", ".js", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".pdf",
    "#",
}


def _is_article_link(href: str, base_domain: str) -> bool:
    """
    Return True if href looks like an ingestible article on the same domain.
    """
    if not href or not href.startswith("http"):
        return False
    try:
        parsed = urlparse(href)
    except Exception:
        return False

    # Must be same domain
    link_domain = parsed.netloc.lower().lstrip("www.")
    if link_domain != base_domain:
        return False

    path = parsed.path.lower()

    # Skip known non-article paths
    for frag in _SKIP_PATH_FRAGMENTS:
        if frag in path:
            return False

    # Must have a meaningful path (not just the root)
    if len(path.strip("/")) < 4:
        return False

    return True


def expand_hub(hub_lead: Lead) -> list[Lead]:
    """
    Fetch a hub_page lead and extract article links from it.
    Returns a list of new Lead objects (access='verify', content_type='unknown').
    Returns [] on any fetch error or if the hub scores below threshold.
    """
    if hub_lead.combined_score < HUB_SCORE_THRESHOLD:
        log.debug(f"  [hub_expander] Skipping {hub_lead.url} — score {hub_lead.combined_score:.1f} < {HUB_SCORE_THRESHOLD}")
        return []

    log.info(f"  [hub_expander] Expanding hub: {hub_lead.url}")
    try:
        resp = requests.get(hub_lead.url, headers=_FETCH_HEADERS, timeout=_TIMEOUT)
        resp.raise_for_status()
    except Exception as e:
        log.warning(f"  [hub_expander] Could not fetch {hub_lead.url}: {e}")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    base_domain = urlparse(hub_lead.url).netloc.lower().lstrip("www.")

    # Collect candidate links
    seen_urls: set[str] = set()
    candidates: list[tuple[str, str]] = []  # (url, title)

    for tag in soup.find_all("a", href=True):
        href = tag["href"].strip()
        absolute = urljoin(hub_lead.url, href)
        # Normalise: strip fragment
        absolute = absolute.split("#")[0].rstrip("/")

        if absolute in seen_urls:
            continue
        if not _is_article_link(absolute, base_domain):
            continue

        title = tag.get_text(strip=True) or absolute
        # Skip very short anchor text (likely icons or "read more" buttons)
        if len(title) < 8:
            title = absolute

        seen_urls.add(absolute)
        candidates.append((absolute, title))

        if len(candidates) >= MAX_LINKS_PER_HUB:
            break

    if not candidates:
        log.info(f"  [hub_expander] No article links found in {hub_lead.url}")
        return []

    log.info(f"  [hub_expander] Extracted {len(candidates)} link(s) from {hub_lead.url}")

    new_leads = []
    for url, title in candidates:
        new_leads.append(Lead(
            url             = url,
            title           = title,
            source_name     = hub_lead.source_name,
            source_type     = hub_lead.source_type,
            access          = "verify",
            content_preview = f"[Extracted from hub: {hub_lead.url}]",
            content_type    = "unknown",   # will be classified by scorer
        ))

    return new_leads


def expand_all_hubs(
    leads:       list[Lead],
    known_urls:  set[str],
) -> list[Lead]:
    """
    Find all hub_page leads in the scored list, expand each one, and return
    the new child leads (deduplicated against known_urls).

    Args:
        leads:      The full scored lead list.
        known_urls: URLs already known (deduplicator set + existing hub URLs).
    Returns:
        New child leads ready for scoring.
    """
    hubs = [l for l in leads if l.content_type == "hub_page"]
    if not hubs:
        return []

    log.info(f"  [hub_expander] Expanding {len(hubs)} hub_page lead(s)…")

    child_leads: list[Lead] = []
    seen: set[str] = set(known_urls)

    for hub in hubs:
        children = expand_hub(hub)
        for child in children:
            if child.url not in seen:
                seen.add(child.url)
                child_leads.append(child)

    log.info(f"  [hub_expander] {len(child_leads)} new child lead(s) from hub expansion.")
    return child_leads
