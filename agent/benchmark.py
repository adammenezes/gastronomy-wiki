"""
Cooking Brain — Benchmark
==========================
Scores the wiki against your quality expectations.

Two modes:
  Static (default) — analyzes every page already in wiki/. Zero API calls.
  Live (--live)    — runs a test file through the full pipeline step-by-step,
                     measures each step's duration and scores the final output.

Metrics reported
  ├── Link density      links per 1000 words  (target ≥ 25)
  ├── Total links       raw [[WikiLink]] count (target ≥ 30)
  ├── Entity recall     % of target culinary entities linked when present
  ├── Phrase integrity  % of multi-word entities linked as a unit (not split)
  ├── Frontmatter       % of required fields present and non-empty
  └── Sections          % of required section headers present

Usage:
  python agent/benchmark.py                          # static wiki scan
  python agent/benchmark.py --live                   # live test: carbonara
  python agent/benchmark.py --live --file inbox/examples/emulsification.txt
  python agent/benchmark.py --live --all             # all inbox/examples/
  python agent/benchmark.py --threshold-links 30     # override pass threshold
"""

from __future__ import annotations

import argparse
import re
import sys
import time
import shutil
import tempfile
import logging
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

import yaml

# Force UTF-8 output on Windows so box-drawing / unicode symbols work
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

_AGENT_DIR = Path(__file__).resolve().parent
if str(_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENT_DIR))

from utils import load_config, collect_wiki_pages   # noqa: E402
from agents.standardizer import REQUIRED_FRONTMATTER, REQUIRED_SECTIONS, _split  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(levelname)s  %(message)s")

# ── Target entity list ────────────────────────────────────────────────────────
# Every entity here that appears in a page body should be wrapped in [[...]].
# Organized by the user's explicit requirements + common culinary concepts.

TARGET_ENTITIES: list[str] = [
    # User's explicit targets (from session)
    "pasta", "fat", "crispy", "silky", "lardons", "boil", "golden", "pork",
    "toss", "residual heat", "scrambled eggs",

    # Ingredients
    "guanciale", "pancetta", "pecorino romano", "parmesan", "egg yolk",
    "spaghetti", "rigatoni", "butter", "cream", "olive oil", "garlic",
    "onion", "flour", "stock", "wine", "vinegar", "salt", "pepper",

    # Techniques
    "emulsification", "rendering", "blanching", "braising", "sautéing",
    "roasting", "poaching", "steaming", "searing", "julienne", "brunoise",
    "deglaze", "fold", "whisk", "simmer", "al dente",

    # Texture / appearance
    "creamy", "tender", "chewy", "glossy", "golden brown", "caramelised",
    "charred", "translucent", "rich", "velvety", "unctuous",

    # Science
    "protein", "starch", "lecithin", "moisture", "acid", "emulsion",
    "maillard reaction", "carry-over cooking", "pasta water",

    # Tools
    "pan", "pot", "bowl", "colander", "whisk", "thermometer",

    # Dishes & cuisines
    "carbonara", "cacio e pepe", "amatriciana", "risotto",
    "italian cuisine", "french cuisine", "roman cuisine",
]

# Multi-word entities that must be linked as a unit (not split into adj. links)
MULTI_WORD_ENTITIES: list[str] = [
    "residual heat", "scrambled eggs", "golden brown", "pasta water",
    "maillard reaction", "carry-over cooking", "pecorino romano",
    "olive oil", "egg yolk", "al dente", "cacio e pepe", "beurre blanc",
    "pasta carbonara", "italian cuisine", "french cuisine", "roman cuisine",
]

# ── Thresholds ────────────────────────────────────────────────────────────────

DEFAULT_THRESHOLDS = {
    "min_links":           30,    # total [[WikiLink]] count
    "min_link_density":    20.0,  # links per 1000 words
    "min_entity_recall":   0.70,  # 70 % of present entities must be linked
    "min_phrase_integrity":0.80,  # 80 % of multi-word entities linked as unit
    "min_frontmatter":     1.00,  # 100 % of required fields
    "min_sections":        1.00,  # 100 % of required sections
}


# ── Score dataclass ───────────────────────────────────────────────────────────

@dataclass
class PageScore:
    path:             str
    category:         str
    title:            str
    word_count:       int
    link_count:       int
    link_density:     float           # links per 1000 words
    entity_recall:    float           # 0-1
    entities_present: int
    entities_linked:  int
    phrase_integrity: float           # 0-1
    phrases_present:  int
    phrases_split:    int
    frontmatter_pct:  float           # 0-1
    sections_pct:     float           # 0-1
    issues:           list[str] = field(default_factory=list)

    def passes(self, t: dict) -> bool:
        return all([
            self.link_count    >= t["min_links"],
            self.link_density  >= t["min_link_density"],
            self.entity_recall >= t["min_entity_recall"],
            self.phrase_integrity >= t["min_phrase_integrity"],
            self.frontmatter_pct  >= t["min_frontmatter"],
            self.sections_pct     >= t["min_sections"],
        ])


# ── Core scoring logic ────────────────────────────────────────────────────────

def score_page(content: str, category: str, title: str, path: str) -> PageScore:
    fm, body = _split(content)

    # ── Word count (body only, strip link brackets for counting)
    plain_body = re.sub(r"\[\[([^\]|]+)(?:\|[^\]]*)?\]\]", r"\1", body)
    word_count = len(plain_body.split())

    # ── Link count
    links = re.findall(r"\[\[.+?\]\]", body)
    link_count = len(links)

    # ── Link density
    link_density = (link_count / word_count * 1000) if word_count else 0.0

    # ── Entity recall (exclude self-links: the page's own title)
    entity_recall, entities_present, entities_linked = _score_entity_recall(body, skip=title)

    # ── Phrase integrity
    phrase_integrity, phrases_present, phrases_split = _score_phrase_integrity(body)

    # ── Frontmatter completeness
    req_fm = REQUIRED_FRONTMATTER.get(category, ["title"])
    fm_ok = sum(
        1 for f in req_fm
        if fm.get(f) not in (None, "", [], {})
    )
    frontmatter_pct = fm_ok / len(req_fm) if req_fm else 1.0

    fm_issues = [
        f"Missing/empty frontmatter: '{f}'"
        for f in req_fm
        if fm.get(f) in (None, "", [], {})
    ]

    # ── Section completeness
    req_sections = REQUIRED_SECTIONS.get(category, ["## Overview"])
    sec_ok = sum(
        1 for s in req_sections
        if re.search(r"^" + re.escape(s), body, re.MULTILINE | re.IGNORECASE)
    )
    sections_pct = sec_ok / len(req_sections) if req_sections else 1.0

    sec_issues = [
        f"Missing section: '{s}'"
        for s in req_sections
        if not re.search(r"^" + re.escape(s), body, re.MULTILINE | re.IGNORECASE)
    ]

    issues = fm_issues + sec_issues

    return PageScore(
        path             = path,
        category         = category,
        title            = title,
        word_count       = word_count,
        link_count       = link_count,
        link_density     = link_density,
        entity_recall    = entity_recall,
        entities_present = entities_present,
        entities_linked  = entities_linked,
        phrase_integrity = phrase_integrity,
        phrases_present  = phrases_present,
        phrases_split    = phrases_split,
        frontmatter_pct  = frontmatter_pct,
        sections_pct     = sections_pct,
        issues           = issues,
    )


def _score_entity_recall(body: str, skip: str = "") -> tuple[float, int, int]:
    """Return (recall, n_present, n_linked). Skips the page's own title."""
    # Plain body for presence detection
    plain = re.sub(r"\[\[([^\]|]+)(?:\|[^\]]*)?\]\]", r"\1", body).lower()
    skip_lower = skip.lower()

    present  = 0
    linked   = 0

    for entity in TARGET_ENTITIES:
        e_lower = entity.lower()
        if e_lower == skip_lower:
            continue   # don't penalise for not self-linking
        if e_lower not in plain:
            continue
        present += 1
        # Check if it appears wrapped in [[ ]] in the original body
        # Allow for pipe aliases: [[entity|display]] or [[Entity]]
        pattern = r"\[\[" + re.escape(e_lower) + r"(?:\|[^\]]*)?\]\]"
        if re.search(pattern, body, re.IGNORECASE):
            linked += 1

    recall = (linked / present) if present else 1.0
    return recall, present, linked


def _score_phrase_integrity(body: str) -> tuple[float, int, int]:
    """
    Return (integrity, n_present, n_split).
    Split = multi-word entity whose words appear as ADJACENT separate links.
    e.g.  [[residual]] [[heat]]  instead of  [[residual heat]]
    """
    plain = re.sub(r"\[\[([^\]|]+)(?:\|[^\]]*)?\]\]", r"\1", body).lower()

    present = 0
    split   = 0

    for phrase in MULTI_WORD_ENTITIES:
        p_lower = phrase.lower()
        if p_lower not in plain:
            continue
        present += 1

        words = p_lower.split()
        # Build pattern: [[word1]] followed by optional space/punct then [[word2]] ...
        split_pattern = r"\s+".join(
            r"\[\[" + re.escape(w) + r"(?:\|[^\]]*)?\]\]" for w in words
        )
        if re.search(split_pattern, body, re.IGNORECASE):
            split += 1

    integrity = 1.0 - (split / present) if present else 1.0
    return integrity, present, split


# ── Reporting ─────────────────────────────────────────────────────────────────

PASS = "✓"
FAIL = "✗"
WARN = "~"

def _pf(value, threshold, fmt=".0f") -> str:
    symbol = PASS if value >= threshold else FAIL
    return f"{symbol} {value:{fmt}}"

def _pct(value, threshold) -> str:
    symbol = PASS if value >= threshold else FAIL
    return f"{symbol} {value*100:.0f}%"


def print_page_report(score: PageScore, thresholds: dict, verbose: bool = False):
    t = thresholds
    sep = "─" * 70

    status = PASS if score.passes(t) else FAIL
    print(f"\n{sep}")
    print(f"  {status}  {score.title}  [{score.category}]  —  {score.path}")
    print(sep)

    print(f"  Words          : {score.word_count:,}")
    print(f"  Links          : {_pf(score.link_count, t['min_links'])}  "
          f"(target ≥ {t['min_links']})")
    print(f"  Link density   : {_pf(score.link_density, t['min_link_density'], '.1f')} / 1k words  "
          f"(target ≥ {t['min_link_density']:.0f})")
    print(f"  Entity recall  : {_pct(score.entity_recall, t['min_entity_recall'])}  "
          f"({score.entities_linked}/{score.entities_present} target entities)")
    print(f"  Phrase integrity: {_pct(score.phrase_integrity, t['min_phrase_integrity'])}  "
          f"({score.phrases_split} split phrase(s) of {score.phrases_present} present)")
    print(f"  Frontmatter    : {_pct(score.frontmatter_pct, t['min_frontmatter'])}")
    print(f"  Sections       : {_pct(score.sections_pct, t['min_sections'])}")

    if score.issues:
        print(f"\n  Issues:")
        for issue in score.issues:
            print(f"    • {issue}")

    if verbose:
        _print_entity_detail(score)


def _print_entity_detail(score: PageScore):
    # Re-run to get which entities are missing
    # (we just recompute here for display)
    print()  # will be filled in by print_missing_entities


def print_missing_entities(content: str, skip: str = ""):
    _, body = _split(content)
    plain = re.sub(r"\[\[([^\]|]+)(?:\|[^\]]*)?\]\]", r"\1", body).lower()
    skip_lower = skip.lower()
    missing = []
    for entity in TARGET_ENTITIES:
        e = entity.lower()
        if e == skip_lower:
            continue
        if e not in plain:
            continue
        pattern = r"\[\[" + re.escape(e) + r"(?:\|[^\]]*)?\]\]"
        if not re.search(pattern, body, re.IGNORECASE):
            missing.append(entity)
    if missing:
        print(f"  Unlinked target entities ({len(missing)}):")
        for m in missing:
            print(f"    • {m}")


def print_summary(scores: list[PageScore], thresholds: dict):
    t = thresholds
    total  = len(scores)
    passed = sum(1 for s in scores if s.passes(t))

    avg_links    = sum(s.link_count    for s in scores) / total if total else 0
    avg_density  = sum(s.link_density  for s in scores) / total if total else 0
    avg_recall   = sum(s.entity_recall for s in scores) / total if total else 0
    avg_phrases  = sum(s.phrase_integrity for s in scores) / total if total else 0
    avg_fm       = sum(s.frontmatter_pct for s in scores) / total if total else 0
    avg_sections = sum(s.sections_pct for s in scores) / total if total else 0

    sep = "═" * 70
    print(f"\n{sep}")
    print(f"  BENCHMARK SUMMARY  —  {passed}/{total} pages PASS")
    print(sep)
    print(f"  Avg links       : {_pf(avg_links,    t['min_links'],        '.1f')}")
    print(f"  Avg density     : {_pf(avg_density,  t['min_link_density'], '.1f')} / 1k words")
    print(f"  Avg entity recall: {_pct(avg_recall,  t['min_entity_recall'])}")
    print(f"  Avg phrase integ : {_pct(avg_phrases, t['min_phrase_integrity'])}")
    print(f"  Avg frontmatter : {_pct(avg_fm,       t['min_frontmatter'])}")
    print(f"  Avg sections    : {_pct(avg_sections, t['min_sections'])}")
    print(sep)

    if passed < total:
        failing = [s for s in scores if not s.passes(t)]
        print(f"\n  Failing pages:")
        for s in failing:
            print(f"    {FAIL}  {s.title}  ({s.path})")


# ── Static mode ───────────────────────────────────────────────────────────────

def run_static(cfg: dict, thresholds: dict, verbose: bool):
    wiki_root = cfg["paths"]["wiki"]
    pages     = collect_wiki_pages(wiki_root, include_content=True)

    if not pages:
        print("No wiki pages found. Run the pipeline first.")
        return

    print(f"Scanning {len(pages)} wiki page(s)…")
    scores = []
    for page in pages:
        score = score_page(
            content  = page["content"],
            category = page["category"],
            title    = page["title"],
            path     = page["file"],
        )
        scores.append(score)
        print_page_report(score, thresholds, verbose)
        if verbose:
            print_missing_entities(page["content"], skip=page["title"])

    print_summary(scores, thresholds)


# ── Live mode ─────────────────────────────────────────────────────────────────

def run_live(cfg: dict, thresholds: dict, verbose: bool, test_files: list[Path]):
    from gemini import init_gemini
    from agents.classifier   import ClassifierAgent
    from agents.writer       import WriterAgent
    from agents.standardizer import StandardizerAgent
    from agents.wiki_linker  import WikiLinkerAgent

    client     = init_gemini(cfg)
    gemini_cfg = cfg["gemini"]
    pdir       = cfg["paths"]["prompts"]
    wiki_root  = cfg["paths"]["wiki"]
    std_cfg    = cfg.get("standardizer", {})

    classifier   = ClassifierAgent(client, gemini_cfg, pdir)
    writer       = WriterAgent(client, gemini_cfg, pdir, wiki_root)
    standardizer = StandardizerAgent(
        client, gemini_cfg, pdir,
        min_body_words = std_cfg.get("min_body_words", 80),
        min_wiki_links = std_cfg.get("min_wiki_links", 8),
    )
    wiki_linker  = WikiLinkerAgent(client, gemini_cfg, pdir)

    scores = []
    timings_all: list[dict] = []

    for test_file in test_files:
        if not test_file.exists():
            print(f"File not found: {test_file}")
            continue

        print(f"\nLive test: {test_file.name}")
        raw_text = test_file.read_text(encoding="utf-8")
        timings: dict[str, float] = {}

        # Step 1 — Classify
        t0 = time.time()
        classification = classifier.run(raw_text)
        timings["classify"] = time.time() - t0
        category = classification.get("category", "general_note")
        title    = classification.get("title_suggestion", test_file.stem)
        conf     = classification.get("confidence", 0.0)
        print(f"  Classified as: {category} — '{title}' (confidence {conf:.0%})")

        # Step 2 — Generate
        t0 = time.time()
        content = writer.generate(category, raw_text)
        timings["generate"] = time.time() - t0
        links_before_std = len(re.findall(r"\[\[.+?\]\]", content))
        words_after_write = len(re.sub(r"\[\[([^\]|]+)(?:\|[^\]]*)?\]\]", r"\1", content).split())
        print(f"  Writer:     {words_after_write} words, {links_before_std} links  "
              f"({timings['generate']:.1f}s)")

        # Step 3 — Standardize
        t0 = time.time()
        content, issues_fixed = standardizer.run(category, content)
        timings["standardize"] = time.time() - t0
        links_after_std = len(re.findall(r"\[\[.+?\]\]", content))
        label = f"fixed {len(issues_fixed)} issue(s)" if issues_fixed else "no issues"
        print(f"  Standardizer: {links_after_std} links after ({label})  "
              f"({timings['standardize']:.1f}s)")

        # Step 4 — WikiLinker
        t0 = time.time()
        content, links_added = wiki_linker.run(content)
        timings["wikilink"] = time.time() - t0
        print(f"  WikiLinker: +{links_added} links → "
              f"{len(re.findall(r'\\[\\[.+?\\]\\]', content))} total  "
              f"({timings['wikilink']:.1f}s)")

        # Score
        score = score_page(content, category, title, test_file.name)
        scores.append(score)
        timings_all.append(timings)

        print_page_report(score, thresholds, verbose)
        if verbose:
            print_missing_entities(content, skip=title)

        # Timing breakdown
        total_t = sum(timings.values())
        print(f"\n  Pipeline timing ({total_t:.1f}s total):")
        for step, dur in timings.items():
            pct = dur / total_t * 100 if total_t else 0
            bar = "█" * int(pct / 5)
            print(f"    {step:<12} {dur:5.1f}s  {bar} {pct:.0f}%")

    if len(scores) > 1:
        print_summary(scores, thresholds)


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Benchmark the Cooking Brain pipeline."
    )
    parser.add_argument("--live",    action="store_true",
                        help="Run a live end-to-end test (uses Gemini API)")
    parser.add_argument("--file",    type=Path, default=None,
                        help="File to use for live test (default: carbonara.txt)")
    parser.add_argument("--all",     action="store_true",
                        help="Live test all files in inbox/examples/")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Show unlinked entity detail per page")
    parser.add_argument("--threshold-links",    type=int,   default=DEFAULT_THRESHOLDS["min_links"],
                        help=f"Min link count (default {DEFAULT_THRESHOLDS['min_links']})")
    parser.add_argument("--threshold-density",  type=float, default=DEFAULT_THRESHOLDS["min_link_density"],
                        help=f"Min link density per 1k words (default {DEFAULT_THRESHOLDS['min_link_density']})")
    parser.add_argument("--threshold-recall",   type=float, default=DEFAULT_THRESHOLDS["min_entity_recall"],
                        help=f"Min entity recall 0-1 (default {DEFAULT_THRESHOLDS['min_entity_recall']})")
    args = parser.parse_args()

    cfg = load_config()

    thresholds = {
        "min_links":            args.threshold_links,
        "min_link_density":     args.threshold_density,
        "min_entity_recall":    args.threshold_recall,
        "min_phrase_integrity": DEFAULT_THRESHOLDS["min_phrase_integrity"],
        "min_frontmatter":      DEFAULT_THRESHOLDS["min_frontmatter"],
        "min_sections":         DEFAULT_THRESHOLDS["min_sections"],
    }

    if args.live:
        examples_dir = cfg["paths"]["inbox"].parent / "inbox" / "examples"
        if args.all:
            test_files = list(examples_dir.glob("*.txt")) + list(examples_dir.glob("*.md"))
        elif args.file:
            test_files = [args.file]
        else:
            test_files = [examples_dir / "carbonara.txt"]
        run_live(cfg, thresholds, args.verbose, test_files)
    else:
        run_static(cfg, thresholds, args.verbose)


if __name__ == "__main__":
    main()
