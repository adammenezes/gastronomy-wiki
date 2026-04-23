"""
Procurement Agent — discovers and surfaces new knowledge leads for the wiki.

Full flow:
  1. GapAnalyzer   → identifies under-covered topics (5 methods)
  2. Crawlers      → discover leads from all configured sources (parallel)
  3. Deduplicator  → removes URLs already in log.md or prior leads.md
  4. LeadScorer    → Gemini quality + relevance scoring (parallel)
  5. LeadsWriter   → writes inbox/leads.md for human review

Approve flow (--approve):
  - Free leads    → passed directly to Orchestrator.process_url()
  - Paywalled     → printed as a manual-download list
"""

import logging
import re
import yaml
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

_AGENT_DIR = Path(__file__).resolve().parent.parent.parent
if str(_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENT_DIR))

from .lead              import Lead             # noqa: E402
from .gap_analyzer      import GapAnalyzer      # noqa: E402
from .scorer            import LeadScorer       # noqa: E402
from .deduplicator      import Deduplicator     # noqa: E402
from .leads_writer      import LeadsWriter      # noqa: E402
from .crawlers.web_crawler      import WebCrawler        # noqa: E402
from .crawlers.journal_scraper  import JournalScraper    # noqa: E402
from .crawlers.multi_link_crawler import MultiLinkCrawler  # noqa: E402

log = logging.getLogger("cooking-brain.procurer")

# Map crawler names in sources.yaml → crawler classes
# Add new crawler types here when you implement them.
CRAWLER_REGISTRY: dict = {
    "WebCrawler":       WebCrawler,
    "JournalScraper":   JournalScraper,
    "MultiLinkCrawler": MultiLinkCrawler,
    # "YouTubeCrawler":  YouTubeCrawler,   # not yet implemented
    # "RedditCrawler":   RedditCrawler,    # not yet implemented
}

MAX_CRAWLER_WORKERS = 4


class ProcurementAgent:
    def __init__(
        self,
        client,
        gemini_cfg:   dict,
        prompts_dir:  Path,
        wiki_root:    Path,
        inbox_root:   Path,
        sources_path: Path,
        taxonomy_path: Path,
    ):
        self.wiki_root  = wiki_root
        self.inbox_root = inbox_root

        self.gap_analyzer  = GapAnalyzer(wiki_root, taxonomy_path)
        self.scorer        = LeadScorer(client, gemini_cfg, prompts_dir)
        self.deduplicator  = Deduplicator(wiki_root, inbox_root)
        self.leads_writer  = LeadsWriter(inbox_root)
        self.sources       = self._load_sources(sources_path)

    # ── Main run ──────────────────────────────────────────────────────────────

    def run(
        self,
        lint_report: dict | None  = None,
        max_leads:   int | None   = None,
        page_gaps:   list | None  = None,
    ) -> Path:
        """Full procurement run. Returns path to the written leads.md.

        If page_gaps is provided, it is used as the gap list directly,
        bypassing GapAnalyzer. All other pipeline stages are unchanged.
        """

        if page_gaps is not None:
            log.info(f"[procurer] --page mode: using {len(page_gaps)} gap(s) from specified page.")
            gaps = page_gaps
        else:
            log.info("[procurer] Analyzing wiki gaps…")
            gaps_by_signal = self.gap_analyzer.run_by_signal(lint_report)
            gaps = self.gap_analyzer._merge(gaps_by_signal)

        log.info(f"[procurer] Crawling {len(self.sources)} source(s)…")
        raw_leads = self._crawl_all(gaps)
        log.info(f"[procurer] Raw leads discovered: {len(raw_leads)}")

        fresh_leads = self.deduplicator.filter(raw_leads)

        if max_leads is not None and len(fresh_leads) > max_leads:
            log.info(f"[procurer] Capping at {max_leads} leads (--max-leads).")
            fresh_leads = fresh_leads[:max_leads]

        scored_leads = self.scorer.score_all(fresh_leads, gaps, gaps_by_signal=gaps_by_signal)

        out_path = self.leads_writer.write(scored_leads, gaps)
        log.info(f"[procurer] Done. Leads at: {out_path}")
        return out_path

    # ── Approve ───────────────────────────────────────────────────────────────

    def approve(self, orchestrator) -> int:
        """
        Parse leads.md for entries the user has marked [x], then process them.

        Free leads  → orchestrator.process_url()
        Paywalled   → printed for manual download
        Returns count of successfully ingested leads.
        """
        leads_path = self.inbox_root / "leads.md"
        if not leads_path.exists():
            log.warning("[procurer] No leads.md found — run without --approve first.")
            return 0

        text     = leads_path.read_text(encoding="utf-8")
        approved = self._parse_approved(text)

        if not approved:
            log.info("[procurer] No leads marked [x] in leads.md.")
            return 0

        log.info(f"[procurer] Approving {len(approved)} lead(s)…")
        ingested: list[dict] = []
        manual:   list[dict] = []

        for lead in approved:
            if lead["access"] == "free":
                try:
                    ok = orchestrator.process_url(lead["url"])
                    if ok:
                        ingested.append(lead)
                except Exception as e:
                    log.error(f"  [procurer] Failed to ingest {lead['url']}: {e}")
            else:
                manual.append(lead)

        if manual:
            print("\n-- Manual download required (paywalled / library) --")
            for lead in manual:
                print(f"  {lead['title']}")
                print(f"  {lead['url']}")
                print(f"  → Download PDF → drop in inbox/")
                print()

        if ingested:
            self._mark_ingested(leads_path, {l["url"] for l in ingested})

        return len(ingested)

    # ── Crawling ──────────────────────────────────────────────────────────────

    def _crawl_all(self, topics: list[str]) -> list[Lead]:
        all_leads: list[Lead] = []

        with ThreadPoolExecutor(max_workers=MAX_CRAWLER_WORKERS) as pool:
            futures = {
                pool.submit(self._crawl_one, source, topics): source
                for source in self.sources
            }
            for fut in as_completed(futures):
                source = futures[fut]
                try:
                    leads = fut.result()
                    all_leads.extend(leads)
                    log.info(
                        f"  [procurer] {source.get('name')}: {len(leads)} lead(s)"
                    )
                except Exception as e:
                    log.warning(
                        f"  [procurer] Crawler failed for {source.get('name')}: {e}"
                    )

        return all_leads

    def _crawl_one(self, source: dict, topics: list[str]) -> list[Lead]:
        crawler_name  = source.get("crawler", "WebCrawler")
        crawler_class = CRAWLER_REGISTRY.get(crawler_name)
        if not crawler_class:
            log.warning(f"  [procurer] Unknown crawler '{crawler_name}' — skipping.")
            return []
        return crawler_class(source).discover(topics)

    # ── Config ────────────────────────────────────────────────────────────────

    def _load_sources(self, sources_path: Path) -> list[dict]:
        if not sources_path.exists():
            log.warning(f"[procurer] sources.yaml not found: {sources_path}")
            return []
        with open(sources_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        sources = data.get("sources", [])
        log.info(f"[procurer] Loaded {len(sources)} source(s) from sources.yaml")
        return sources

    # ── Approve helpers ───────────────────────────────────────────────────────

    def _parse_approved(self, text: str) -> list[dict]:
        """
        Extract leads marked [x] from leads.md.
        Looks for sections (### Title) that contain '- **Status:** [x]'.
        """
        approved: list[dict] = []
        sections = re.split(r"^### ", text, flags=re.MULTILINE)

        for section in sections[1:]:   # skip header before first ###
            if not re.search(r"\- \*\*Status:\*\* \[x\]", section, re.IGNORECASE):
                continue

            title_match  = re.match(r"^(.+)", section)
            url_match    = re.search(r"\*\*URL:\*\*\s*(https?://\S+)", section)
            access_match = re.search(r"\*\*Access:\*\*\s*(.+)", section)

            if not url_match:
                continue

            access_text = (access_match.group(1) if access_match else "").lower()
            access = "free" if "free" in access_text else "paywalled"

            approved.append({
                "title":  title_match.group(1).strip() if title_match else "unknown",
                "url":    url_match.group(1).strip(),
                "access": access,
            })

        return approved

    def _mark_ingested(self, leads_path: Path, ingested_urls: set[str]):
        """
        In leads.md, change '[ ] pending' → '[x] ingested' for each
        section whose URL is in ingested_urls.
        """
        text     = leads_path.read_text(encoding="utf-8")
        sections = re.split(r"(^### )", text, flags=re.MULTILINE)
        result   = []

        i = 0
        while i < len(sections):
            if sections[i] == "### " and i + 1 < len(sections):
                body = sections[i + 1]
                url_match = re.search(r"\*\*URL:\*\*\s*(https?://\S+)", body)
                if url_match and url_match.group(1).strip() in ingested_urls:
                    body = body.replace(
                        "- **Status:** [x] pending",
                        "- **Status:** [x] ingested",
                        1,
                    )
                result.append(sections[i] + body)
                i += 2
            else:
                result.append(sections[i])
                i += 1

        leads_path.write_text("".join(result), encoding="utf-8")
        log.info(f"  [procurer] Marked {len(ingested_urls)} lead(s) as ingested in leads.md")
