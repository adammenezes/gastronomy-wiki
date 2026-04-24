"""
Cooking Brain — Procure CLI
============================
Discover and surface new knowledge leads for the wiki.

Usage:
    python agent/procure.py                     # gap analysis + crawl + score + write leads.md
    python agent/procure.py --gaps-only         # print gaps only, skip crawling
    python agent/procure.py --lint              # run lint first, feed real gaps into procurer
    python agent/procure.py --approve           # process approved leads from leads.md
    python agent/procure.py --source NAME       # run only one source (name from sources.yaml)
    python agent/procure.py --max-leads N       # cap total leads scored (default: 300)
    python agent/procure.py --estimate          # show projected token cost, then exit
    python agent/procure.py --dry-run           # analyse gaps + crawl but don't write leads.md
"""

import re
import sys
import logging
import argparse
from pathlib import Path

_HERE = Path(__file__).resolve().parent.parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from utils        import load_config        # noqa: E402
from gemini       import init_gemini        # noqa: E402
from orchestrator import Orchestrator       # noqa: E402
from agents.procurer import ProcurementAgent  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)

# Cost constants (Gemini 2.5 Flash, non-thinking mode)
_INPUT_COST_PER_M    = 0.075   # $ per 1M input tokens
_OUTPUT_COST_PER_M   = 0.30    # $ per 1M output tokens
_TOKENS_PER_LEAD     = 1209    # input tokens per scoring call (measured)
_OUTPUT_PER_LEAD     = 100     # output tokens per scoring call (measured)
_INGEST_COST_PER_PAGE = 0.012  # cleaner+classifier+writer+wikilinker+avg 3 cross-link updates


def _print_estimate(n_leads: int, n_sources: int, approval_rate: float = 0.10):
    scoring_input  = n_leads * _TOKENS_PER_LEAD
    scoring_output = n_leads * _OUTPUT_PER_LEAD
    scoring_cost   = (scoring_input * _INPUT_COST_PER_M + scoring_output * _OUTPUT_COST_PER_M) / 1_000_000

    # Lint cost: fixed per run (wiki corpus ~60K tokens, output up to 16K)
    lint_cost = (60_000 * _INPUT_COST_PER_M + 16_000 * _OUTPUT_COST_PER_M) / 1_000_000

    # Ingest cost: expected approvals * cost per page ingested
    expected_approvals = n_leads * approval_rate
    ingest_cost        = expected_approvals * _INGEST_COST_PER_PAGE

    total = scoring_cost + lint_cost + ingest_cost

    print(f"\n-- Cost Estimate --------------------------------")
    print(f"  Sources:              {n_sources}")
    print(f"  Max leads:            {n_leads}")
    print(f"  Scoring input:        {scoring_input:,} tokens  ->  ${scoring_cost:.4f}")
    print(f"  Lint (one call):      ~76,000 tokens       ->  ${lint_cost:.4f}")
    print(f"  Ingest ({approval_rate*100:.0f}% approval):  ~{expected_approvals:.0f} pages          ->  ${ingest_cost:.4f}")
    print(f"  Total end-to-end:                               ${total:.4f}")
    print(f"  Monthly (daily runs):                           ${total * 30:.2f}")
    print(f"  Monthly (weekly runs):                          ${total * 4:.2f}")
    print(f"-------------------------------------------------\n")


def _gaps_from_page(page_path: str, wiki_root: Path) -> list[str]:
    """
    Extract [[WikiLink]] targets from a wiki page and apply the canonical
    GapAnalyzer noise filter — the same filter used in a full wiki-wide run.

    Filters applied (via GapAnalyzer.filter_wikilinks):
      - Already has a wiki page  → skip
      - Shorter than 5 chars     → skip
      - Pure numbers/symbols     → skip
      - Single-word noise term   → skip (see _WIKILINK_NOISE in gap_analyzer.py)
    """
    from agents.procurer.gap_analyzer import GapAnalyzer  # noqa: E402

    path = Path(page_path)
    if not path.exists():
        print(f"ERROR: File not found: {page_path}")
        sys.exit(1)

    text  = path.read_text(encoding="utf-8")
    raw   = re.findall(r"\[\[([^\]|]+)", text)
    links = [t.strip() for t in raw if t.strip()]

    # Build the set of existing wiki page titles (same as GapAnalyzer does)
    existing_titles: set[str] = set()
    for md in wiki_root.rglob("*.md"):
        existing_titles.add(md.stem.lower())

    # Instantiate with a dummy taxonomy path (not needed for filter_wikilinks)
    analyzer = GapAnalyzer(wiki_root, wiki_root / "taxonomy.yaml")
    filtered = analyzer.filter_wikilinks(links, existing_titles)

    return filtered



def main():
    parser = argparse.ArgumentParser(description="Cooking Brain — Procure")
    parser.add_argument(
        "--approve",    action="store_true",
        help="Process leads marked [x] in leads.md",
    )
    parser.add_argument(
        "--gaps-only",  action="store_true",
        help="Print identified gaps only — skip crawling",
    )
    parser.add_argument(
        "--lint",       action="store_true",
        help="Run the lint agent first and feed its gaps into the procurer",
    )
    parser.add_argument(
        "--source",     type=str, default=None,
        help="Run only this source (name field in sources.yaml)",
    )
    parser.add_argument(
        "--max-leads",  type=int, default=300,
        help="Cap total leads scored across all sources (default: 300)",
    )
    parser.add_argument(
        "--estimate",   action="store_true",
        help="Print projected token cost for this run, then exit — no API calls made",
    )
    parser.add_argument(
        "--approval-rate", type=float, default=0.10, metavar="FLOAT",
        help="Fraction of scored leads expected to be approved for ingest (default: 0.10)",
    )
    parser.add_argument(
        "--dry-run",    action="store_true",
        help="Analyse and crawl but do not write leads.md",
    )
    parser.add_argument(
        "--page",       type=str, default=None, metavar="PATH",
        help="Use [[WikiLinks]] from a specific wiki page as the gap list instead of GapAnalyzer",
    )
    args = parser.parse_args()

    cfg         = load_config()
    client      = init_gemini(cfg)
    # Strip agents sub-dict — scorer uses default model only, not per-agent routing
    gemini_cfg  = {k: v for k, v in cfg["gemini"].items() if k != "agents"}
    prompts_dir = cfg["paths"]["prompts"]
    wiki_root   = cfg["paths"]["wiki"]
    inbox_root  = cfg["paths"]["inbox"]

    sources_path  = _HERE / "sources.yaml"
    taxonomy_path = _HERE / "taxonomy.yaml"

    agent = ProcurementAgent(
        client        = client,
        gemini_cfg    = gemini_cfg,
        prompts_dir   = prompts_dir,
        wiki_root     = wiki_root,
        inbox_root    = inbox_root,
        sources_path  = sources_path,
        taxonomy_path = taxonomy_path,
    )

    # ── --source filter (applied before estimate so counts are accurate) ───────
    if args.source:
        agent.sources = [s for s in agent.sources if s["name"] == args.source]
        if not agent.sources:
            print(f"ERROR: No source named '{args.source}' in sources.yaml")
            sys.exit(1)

    # ── --estimate ────────────────────────────────────────────────────────────
    if args.estimate:
        n_sources = len(agent.sources)
        n_leads   = min(args.max_leads, 50 * n_sources)  # 50 leads/source max
        _print_estimate(n_leads, n_sources, approval_rate=args.approval_rate)
        return

    # ── --approve ─────────────────────────────────────────────────────────────
    if args.approve:
        orch  = Orchestrator(cfg)
        count = agent.approve(orch)
        print(f"\n  Ingested {count} approved lead(s).")
        if count:
            moved, flagged = orch.sort(delete_garbled=True)
            if moved:
                print(f"  Sorted {moved} misplaced file(s) to correct subfolders.")
            if flagged:
                print(f"  {flagged} file(s) flagged for manual review (run: python agent/sort.py).")
            orch.reindex()
        leads_path = inbox_root / "leads.md"
        if leads_path.exists():
            leads_path.unlink()
            print("  Cleared inbox/leads.md.")
        return

    # ── --gaps-only ───────────────────────────────────────────────────────────
    if args.gaps_only:
        by_signal = agent.gap_analyzer.run_by_signal()
        merged    = agent.gap_analyzer._merge(by_signal)
        for signal, gaps in by_signal.items():
            print(f"\n-- {signal.upper()} gaps ({len(gaps)}) ----------")
            for g in gaps[:20]:
                print(f"  - {g}")
            if len(gaps) > 20:
                print(f"  ... and {len(gaps) - 20} more")
        print(f"\n  Total unique gaps (merged): {len(merged)}")
        return

    # ── --page: extract WikiLinks as gap list ────────────────────────────────
    page_gaps = None
    if args.page:
        page_gaps = _gaps_from_page(args.page, wiki_root)
        if not page_gaps:
            print(f"ERROR: No usable [[WikiLinks]] found in '{args.page}' after noise filtering")
            sys.exit(1)
        print(f"  --page mode: {len(page_gaps)} gap(s) extracted from {args.page} (after noise filter)")


    # ── optional --lint to enrich gap list ────────────────────────────────────
    lint_report = None
    if args.lint:
        print("Running lint agent first…")
        orch        = Orchestrator(cfg)
        lint_report = orch.lint()
        gaps_count  = (
            len(lint_report.get("gaps",  [])) +
            len(lint_report.get("stubs", []))
        )
        print(f"  Lint found {gaps_count} gap/stub item(s) to feed into procurer.")

    # ── --dry-run ─────────────────────────────────────────────────────────────
    if args.dry_run:
        if page_gaps is not None:
            gaps = page_gaps
        else:
            gaps_by_signal = agent.gap_analyzer.run_by_signal(lint_report)
            gaps           = agent.gap_analyzer._merge(gaps_by_signal)
        raw_leads = agent._crawl_all(gaps, page_path=args.page or "")
        fresh     = agent.deduplicator.filter(raw_leads)
        capped    = min(len(fresh), args.max_leads)
        _print_estimate(capped, len(agent.sources), approval_rate=args.approval_rate)
        print(f"[dry-run] Gaps: {len(gaps)} | Raw leads: {len(raw_leads)} | Fresh: {len(fresh)} | Would score: {capped}")
        print("  (leads.md not written in dry-run mode)")
        return

    # ── full run ──────────────────────────────────────────────────────────────
    if args.source:
        print(f"  Running single source: {args.source}")

    out_path = agent.run(
        lint_report = lint_report,
        max_leads   = args.max_leads,
        page_gaps   = page_gaps,
        page_path   = args.page or "",
    )
    print(f"\n  Leads written to: {out_path}")
    print("  Open leads.md, mark [x] to approve, then run:")
    print("  python agent/procure.py --approve")


if __name__ == "__main__":
    main()
