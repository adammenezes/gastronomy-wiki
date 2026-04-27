"""
Query Sub-Agent
================
Answers a natural-language question from the wiki's contents.

Retrieval strategy (hybrid, three layers):
  1. Keyword match — score pages by overlap with extracted question terms
  2. Category boost — detect question intent, upweight matching category pages
  3. Graph expansion — follow [[WikiLink]] edges from top-3 seeds one level deep,
     pulling in pages that are related but may not share keywords with the question

This means a question like "why does salt help emulsify?" can pull in
salt, emulsification, AND chemistry/technique pages linked from those seeds —
even if the chemistry pages don't mention "salt".

Cap: 15 context pages total. Gemini synthesises from this richer context.
"""

import re
import logging
import sys
from pathlib import Path

_AGENT_DIR = Path(__file__).resolve().parent.parent.parent
if str(_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENT_DIR))

from llm    import call_llm                                    # noqa: E402
from utils import load_prompt, inject_date, collect_wiki_pages   # noqa: E402

log = logging.getLogger("cooking-brain.query")

_MAX_CONTEXT_PAGES = 15   # raised from 10 to accommodate graph expansion
_GRAPH_SEEDS       = 3    # pages to expand from (top keyword matches)
_GRAPH_DEPTH_CAP   = 8    # max neighbors to add from graph expansion

# ── Stop words ────────────────────────────────────────────────────────────────
_STOP_WORDS = {
    "a","an","the","is","are","how","do","does","what","why","when","where",
    "which","can","i","to","of","in","it","for","and","or","with","this",
    "that","my","me","be","was","has","have","had","will","would","should",
    "could","make","made","making","use","using","used","get","good","best",
    "need","want","about","more","also","just","not","if","from","at","by",
    "as","so","up","on","off","its","then","than","some","any","give","tell",
}

# ── Intent → category mapping ─────────────────────────────────────────────────
# Words that signal which wiki category is most relevant to the question
_INTENT_MAP: dict[str, set[str]] = {
    "recipe":     {"recipe","recipes","dish","meal","cook","prepare","bake","fry",
                   "roast","make","serve","plate","yield","ingredient","ingredients"},
    "technique":  {"technique","method","process","chemistry","science","reaction",
                   "why","how","mechanism","works","physics","temperature","heat",
                   "chemical","molecule","protein","starch","fat","emulsion","gluten"},
    "ingredient": {"ingredient","substitute","substitution","flavour","flavor",
                   "taste","season","store","buy","fresh","dried","whole","ground",
                   "ripe","raw","aged","cured","spice","herb","acid","salt","sugar"},
    "cuisine":    {"cuisine","region","regional","culture","traditional","classic",
                   "italian","french","japanese","chinese","mexican","indian",
                   "spanish","mediterranean","asian","american","european"},
    "tool":       {"tool","equipment","pan","pot","knife","blender","whisk","spatula",
                   "oven","grill","smoker","thermometer","scale","mandoline"},
    "person":     {"chef","author","cookbook","who","michelin","starred","restaurateur"},
}


class QueryAgent:
    def __init__(self, client, llm_cfg: dict, prompts_dir: Path, wiki_root: Path):
        self.client     = client
        self.llm_cfg = llm_cfg
        self.wiki_root  = wiki_root
        self._prompt    = load_prompt(prompts_dir, "query")

    def run(self, question: str) -> dict:
        """
        Returns:
            {
                "answer":          str,
                "sources":         [str, ...],   # file paths used
                "needs_research":  str | None,   # search query if gap detected
            }
        """
        pages = collect_wiki_pages(self.wiki_root, include_content=True)

        if not pages:
            return {
                "answer":         "The wiki is empty — add some content to inbox/ first.",
                "sources":        [],
                "needs_research": None,
            }

        # ── Hybrid retrieval ──────────────────────────────────────────────────
        candidates = _hybrid_retrieve(question, pages)
        log.info(
            f"  [query] Retrieved {len(candidates)} page(s) for: {question[:60]}"
        )

        # ── Build context ─────────────────────────────────────────────────────
        context_parts = []
        source_files  = []
        for p in candidates:
            ext_source = _parse_source_field(p.get("content", ""))
            source_line = f"\nExternal Source: {ext_source}" if ext_source else ""
            context_parts.append(
                f"### {p['title']} ({p['file']}){source_line}\n\n{p.get('content', '')}"
            )
            source_files.append(p["file"])

        context      = "\n\n---\n\n".join(context_parts)
        user_content = f"QUESTION: {question}\n\nWIKI CONTEXT:\n\n{context}"

        # ── Gemini call ───────────────────────────────────────────────────────
        system = inject_date(self._prompt)
        answer = call_llm(self.client, self.llm_cfg, system, user_content)

        # ── Parse NEEDS_RESEARCH signal ───────────────────────────────────────
        needs_research = None
        if "NEEDS_RESEARCH:" in answer:
            lines = answer.splitlines()
            for line in lines:
                if line.startswith("NEEDS_RESEARCH:"):
                    needs_research = line.replace("NEEDS_RESEARCH:", "").strip()
                    break
            answer = "\n".join(
                l for l in lines if not l.startswith("NEEDS_RESEARCH:")
            ).rstrip()

        return {
            "answer":         answer,
            "sources":        source_files,
            "needs_research": needs_research,
        }

    def file_answer(
        self,
        question: str,
        answer: str,
        wiki_root: Path,
        dry_run: bool,
    ) -> Path:
        """Write the answer as a general_note wiki page."""
        from utils import slugify, inject_date  # already on path
        from datetime import date

        title = f"Q — {question[:60]}"
        slug  = slugify(title)
        content = (
            f"---\n"
            f"title: \"{title}\"\n"
            f"tags: [query, general_note]\n"
            f"date_added: {date.today().isoformat()}\n"
            f"---\n\n"
            f"# {title}\n\n"
            f"{answer}\n"
        )

        output_path = wiki_root / f"{slug}.md"
        if dry_run:
            log.info(f"[query] [DRY RUN] Would file answer as: {output_path.name}")
            return output_path

        output_path.write_text(content, encoding="utf-8")
        log.info(f"[query] Answer filed as: {output_path.name}")
        return output_path


# ── Hybrid retrieval ──────────────────────────────────────────────────────────

def _hybrid_retrieve(question: str, pages: list[dict]) -> list[dict]:
    """
    Three-layer retrieval:
      Layer 1 — keyword score (base relevance)
      Layer 2 — category boost (intent alignment)
      Layer 3 — graph expansion (WikiLink neighbors of top seeds)

    Returns up to _MAX_CONTEXT_PAGES deduplicated pages, ranked by score.
    """
    q_lower   = question.lower()
    keywords  = _extract_keywords(q_lower)
    intents   = _detect_intents(q_lower)

    # Build a title → page lookup for graph traversal
    by_title = {p["title"].lower(): p for p in pages}

    # ── Layer 1 & 2: keyword score + category boost ───────────────────────────
    scored: list[tuple[float, dict]] = []
    for p in pages:
        kw_score = _keyword_score(p, keywords)
        boost    = 1.5 if p["category"] in intents else 1.0
        scored.append((kw_score * boost, p))

    scored.sort(key=lambda t: t[0], reverse=True)

    # Pages with any match, ranked
    matched  = [(s, p) for s, p in scored if s > 0]
    seeds    = [p for _, p in matched[:_GRAPH_SEEDS]]

    # If nothing matched at all, fall back to most recently added pages
    if not matched:
        log.info("  [query] No keyword matches — using recent pages as fallback.")
        return pages[:5]

    # ── Layer 3: graph expansion ──────────────────────────────────────────────
    seen_files = {p["file"] for _, p in matched[:_MAX_CONTEXT_PAGES]}
    neighbors: list[dict] = []

    for seed in seeds:
        linked_titles = _extract_link_targets(seed.get("content", ""))
        for title_lower in linked_titles:
            if title_lower in by_title:
                neighbor = by_title[title_lower]
                if neighbor["file"] not in seen_files:
                    seen_files.add(neighbor["file"])
                    neighbors.append(neighbor)
                    if len(neighbors) >= _GRAPH_DEPTH_CAP:
                        break
        if len(neighbors) >= _GRAPH_DEPTH_CAP:
            break

    if neighbors:
        log.info(
            f"  [query] Graph expansion added {len(neighbors)} neighbor(s) "
            f"from {len(seeds)} seed(s)."
        )

    # ── Assemble final candidate list ─────────────────────────────────────────
    # Keyword+boost results first, then graph neighbors
    result = [p for _, p in matched]
    result += neighbors

    return result[:_MAX_CONTEXT_PAGES]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _extract_keywords(q_lower: str) -> set[str]:
    """Extract meaningful terms from the question."""
    tokens = re.findall(r"[a-zA-Z']+", q_lower)
    return {t for t in tokens if t not in _STOP_WORDS and len(t) > 2}


def _detect_intents(q_lower: str) -> set[str]:
    """Return set of category names that are signalled by words in the question."""
    tokens = set(re.findall(r"[a-zA-Z']+", q_lower))
    matched_categories: set[str] = set()
    for category, signal_words in _INTENT_MAP.items():
        if tokens & signal_words:
            matched_categories.add(category)
    return matched_categories


def _keyword_score(page: dict, keywords: set[str]) -> float:
    """
    Score a page by keyword overlap.
    Title matches count double (more signal per word).
    """
    title_lower   = page["title"].lower()
    content_lower = (page.get("content") or "").lower()
    tags_lower    = " ".join(page.get("tags") or []).lower()

    score = 0.0
    for kw in keywords:
        if kw in title_lower:
            score += 2.0
        elif kw in tags_lower:
            score += 1.5
        elif kw in content_lower:
            score += 1.0
    return score


def _parse_source_field(content: str) -> str:
    """Extract the 'source' frontmatter field value, if present and not a placeholder."""
    m = re.search(r'^source:\s*(.+)$', content, re.MULTILINE)
    if not m:
        return ""
    val = m.group(1).strip().strip('"\'')
    if val.lower() in ("personal note", "personal collection", ""):
        return ""
    return val


def _extract_link_targets(content: str) -> list[str]:
    """
    Return the titles of all [[WikiLink]] targets in a page's content,
    lowercased, deduped, in order of appearance.
    Handles [[Title]] and [[Title|alias]] forms.
    """
    targets = re.findall(r"\[\[([^\]|]+)", content)
    seen: set[str] = set()
    result: list[str] = []
    for t in targets:
        key = t.strip().lower()
        if key not in seen:
            seen.add(key)
            result.append(key)
    return result
