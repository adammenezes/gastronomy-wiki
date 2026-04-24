"""
Lead Scorer — Gemini evaluates quality, relevance, and content type of each lead.

Quality rubric (0–10):
  - Subject matter expertise of author / institution
  - Evidence and citation density
  - Depth of technical analysis (not just "how to" but "why")
  - Source credibility (journal impact, editorial standards)
  - Logical rigour

Relevance rubric (0–10):
  - How directly the lead fills identified wiki gaps

Content type (classified by Gemini):
  - article   → substantive ingestible text
  - hub_page  → index/nav/chapter-list; real content is one level deeper
  - book_ref  → physical book or product landing page; not directly ingestible
  - podcast   → audio/video media; no text to ingest
  - unknown   → Gemini could not determine from available metadata

Combined score = (quality × 0.6) + (relevance × 0.4)
Leads below MIN_QUALITY are silently dropped in LeadsWriter.
"""

import re
import json
import logging
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlparse

_AGENT_DIR = Path(__file__).resolve().parent.parent.parent
if str(_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENT_DIR))

from gemini import call_gemini      # noqa: E402
from utils  import load_prompt      # noqa: E402
from .lead  import Lead             # noqa: E402

log = logging.getLogger("cooking-brain.procurer.scorer")

MAX_WORKERS    = 5
PREVIEW_CHARS  = 1500   # cap content sent to Gemini per lead
MAX_GAPS_CHARS = 600    # cap on gap list sent per scoring call

# Domains that are structurally paywalled regardless of Brave's flag.
# Brave's isAccessibleForFree is unreliable for these — their abstract pages
# look open but the full paper is behind an institutional login.
_STRUCTURAL_PAYWALL_DOMAINS = {
    "springer.com", "link.springer.com",
    "sciencedirect.com", "linkinghub.elsevier.com",
    "onlinelibrary.wiley.com", "wiley.com",
    "tandfonline.com",
    "jstor.org",
    "nature.com",                   # most full articles require institutional access
    "cell.com",
    "jamanetwork.com",
    "nejm.org",
    "thelancet.com",
    "oxfordjournals.org",
    "academic.oup.com",
    "journals.sagepub.com",
    "ingentaconnect.com",
    "pubs.rsc.org",
    "acs.org", "pubs.acs.org",
    "science.org", "sciencemag.org",
    "cambridge.org",
    "emerald.com",
    "informahealthcare.com",
}

# Proportion of the gap budget drawn from each signal type.
# Must sum to 1.0. Taxonomy gets the biggest slice because it represents
# the broadest set of topics the wiki should cover.
_GAP_WEIGHTS = {
    "wikilinks": 0.25,
    "lint":      0.15,
    "frequency": 0.15,
    "taxonomy":  0.45,
}

_VALID_CONTENT_TYPES = {"article", "hub_page", "book_ref", "podcast", "unknown"}


def _sample_gaps(gaps_by_signal: dict[str, list[str]], max_chars: int = MAX_GAPS_CHARS) -> str:
    """
    Build a representative gap string by sampling proportionally from each
    signal type rather than just truncating the flat list.
    Stops adding gaps once max_chars is reached.
    """
    if not gaps_by_signal or not any(gaps_by_signal.values()):
        return "general culinary knowledge"

    # Estimate how many gaps fit in the budget (avg ~15 chars + ", " separator)
    budget = max(1, max_chars // 17)

    selected: list[str] = []
    seen:     set[str]  = set()

    for signal, weight in _GAP_WEIGHTS.items():
        pool  = gaps_by_signal.get(signal, [])
        quota = max(1, round(budget * weight))
        for gap in pool[:quota]:
            key = gap.lower().strip()
            if key and key not in seen:
                seen.add(key)
                selected.append(gap)

    summary = ", ".join(selected)
    return summary[:max_chars] if summary else "general culinary knowledge"


def _apply_structural_paywall(lead: Lead) -> None:
    """
    Layer 2 paywall detection: mark leads as paywalled when their domain is
    known to be structurally paywalled, even if Brave didn't flag it.
    Does nothing if the lead is already marked paywalled.
    """
    if lead.access == "paywalled":
        return
    try:
        domain = urlparse(lead.url).netloc.lower().lstrip("www.")
    except Exception:
        return
    for paywall_domain in _STRUCTURAL_PAYWALL_DOMAINS:
        if domain == paywall_domain or domain.endswith("." + paywall_domain):
            lead.access = "paywalled"
            return


class LeadScorer:
    def __init__(self, client, gemini_cfg: dict, prompts_dir: Path):
        self.client     = client
        self.gemini_cfg = gemini_cfg
        self._prompt    = load_prompt(prompts_dir, "score_lead")

    def score_all(
        self,
        leads:          list[Lead],
        gaps:           list[str],
        gaps_by_signal: dict[str, list[str]] | None = None,
    ) -> list[Lead]:
        """
        Score all leads in parallel.
        gaps_by_signal: if provided, samples proportionally across signal types.
        Returns leads sorted by combined_score descending (unscored leads dropped).
        """
        if not leads:
            return []

        # Layer 2: apply structural paywall before scoring
        paywalled_count = 0
        for lead in leads:
            before = lead.access
            _apply_structural_paywall(lead)
            if lead.access != before:
                paywalled_count += 1
        if paywalled_count:
            log.info(f"  [scorer] Structural paywall: {paywalled_count} lead(s) re-classified as paywalled.")

        if gaps_by_signal:
            gaps_summary = _sample_gaps(gaps_by_signal)
        else:
            gaps_summary = ", ".join(gaps[:40])[:MAX_GAPS_CHARS] if gaps else "general culinary knowledge"

        log.info(f"  [scorer] Scoring {len(leads)} lead(s) | gap sample: {gaps_summary[:80]}…")

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {
                pool.submit(self._score_one, lead, gaps_summary): lead
                for lead in leads
            }
            for fut in as_completed(futures):
                lead = futures[fut]
                try:
                    fut.result()
                except Exception as e:
                    log.warning(f"  [scorer] Failed to score '{lead.title}': {e}")

        scored = [l for l in leads if l.combined_score > 0]
        scored.sort(key=lambda l: l.combined_score, reverse=True)
        log.info(f"  [scorer] Scored {len(scored)}/{len(leads)} lead(s) successfully.")
        return scored

    def _score_one(self, lead: Lead, gaps_summary: str):
        """Score a single lead. Mutates lead in place."""
        payload = json.dumps({
            "title":       lead.title,
            "url":         lead.url,
            "source":      lead.source_name,
            "source_type": lead.source_type,
            "preview":     lead.content_preview[:PREVIEW_CHARS],
            "wiki_gaps":   gaps_summary,
        })

        response = call_gemini(self.client, self.gemini_cfg, self._prompt, payload)

        # Strip any markdown code fences Gemini may add
        response = re.sub(r"^```(?:json)?\s*", "", response, flags=re.MULTILINE)
        response = re.sub(r"\s*```$",           "", response, flags=re.MULTILINE)

        try:
            data = json.loads(response)
            lead.quality_score    = float(data.get("quality_score",   0))
            lead.relevance_score  = float(data.get("relevance_score", 0))
            lead.fills_gap        = data.get("fills_gap",    "")
            lead.score_justification = data.get("justification", "")
            lead.combined_score   = round(
                (lead.quality_score * 0.6) + (lead.relevance_score * 0.4), 2
            )
            # Layer 3: Gemini content type classification
            raw_ct = data.get("content_type", "unknown")
            lead.content_type = raw_ct if raw_ct in _VALID_CONTENT_TYPES else "unknown"
        except (json.JSONDecodeError, ValueError, TypeError) as e:
            log.warning(f"  [scorer] Could not parse score for '{lead.title}': {e}")
